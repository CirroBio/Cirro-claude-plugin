"""Route one worker request to one handler, and render any failure as a typed error."""
from pathlib import Path
from typing import Any

from .. import errors, plans
from ..cache import SnapshotCache
from ..session import Session
from . import auth, portal, reads


def _session(request: dict) -> Session:
    return Session(base_url=request["base_url"], dir=request["session_dir"])


def do_plan(request: dict) -> dict:
    from . import mutations
    op = mutations.get_op(request["op"])
    reader = reads.Reader(request)
    draft = op.plan(reader, request.get("args") or {})
    session = _session(request)
    record = plans.create(session, op=op.name, args=draft.args, summary=draft.summary,
                          username=reader.username, risk=draft.risk or op.risk,
                          warnings=draft.warnings,
                          invalidates=draft.invalidates,
                          request_args=request.get("args") or {})
    response = {
        "ok": True,
        "plan_id": record.plan_id,
        "digest": record.digest,
        "op": op.name,
        "risk": op.risk,
        "expires_at": record.expires_at,
        "expires_in_s": int(record.expires_at - record.created_at),
        "warnings": record.warnings,
        "will_invalidate": record.invalidates,
        "cost": reader.cost.to_dict(),
        # `rendered` carries the summary, so the summary is not repeated beside it, and
        # apply's approved_summary is supplied by the gate rather than echoed here. The
        # text used to appear three times in one response and a fourth time in apply's
        # input, for one copy of information.
        "next": {
            "tool": "apply",
            "args": {"plan_id": record.plan_id, "digest": record.digest},
            "note": ("Show the rendered text to the person before calling apply. "
                     "apply prompts for approval and takes no operation arguments."),
        },
    }

    previous = request.get("renew_from")
    if previous and _same_plan(session, previous, record):
        # Nothing the person already saw has changed, and they see the prompt rendered
        # from disk either way -- so re-sending the text would buy nothing.
        response["renewed_from"] = previous
        response["unchanged"] = True
        response["note"] = ("Identical to the expired plan; its text still stands. "
                            "Re-show that text, then apply.")
        return response

    response["rendered"] = plans.render(record.to_dict())
    response["unchanged"] = False
    return response


def _same_plan(session: Session, previous_id: str, record) -> bool:
    """Would re-approving cost the person anything new to read?

    Compared on what the approval actually depends on -- the operation, the resolved
    arguments and the rendered figures -- not on the digest, which includes the plan
    id and expiry and so never matches.
    """
    try:
        stale = plans.load(session, previous_id)
    except errors.AgentError:
        return False
    return (stale.op == record.op and stale.args == record.args
            and stale.summary == record.summary and stale.warnings == record.warnings)


def _revalidated(reader: reads.Reader, op, record) -> dict:
    """Re-plan from the original request and refuse if the disclosure has changed.

    A timeout cannot tell whether the figures a person read are still true; planning
    again can. So the expiry only has to guard intent -- whether they still mean it --
    and this guards accuracy: if the same request now resolves to different arguments
    or renders different text, what they approved is not what would run.

    Returns the freshly resolved arguments, so what executes is computed from the
    world as it is now rather than from a snapshot.
    """
    if not record.request_args:
        # Plans written before request_args existed, and any op planned with no
        # arguments at all. Nothing to re-plan from; the digest still applies.
        return record.args
    try:
        draft = op.plan(reader, dict(record.request_args))
    except errors.AgentError as exc:
        raise errors.PlanStale(
            f"{record.op} no longer plans cleanly: {exc.message}",
            what="Something the approved plan depended on has changed.",
            retry_with=[{"tool": "plan", "op": record.op,
                         "renew_from": record.plan_id}],
        )
    if draft.args == record.args and draft.summary == record.summary:
        return draft.args
    raise errors.PlanStale(
        "The facts behind this plan changed since it was approved.",
        what=("Re-planning the same request produced a different result, so executing "
              "it would not be what was approved."),
        retry_with=[{"tool": "plan", "op": record.op, "renew_from": record.plan_id}],
        user_action="Re-plan and read the new figures before approving again.",
        changed=("arguments" if draft.args != record.args else "disclosure"),
    )


def do_apply(request: dict) -> dict:
    from . import mutations
    session = _session(request)
    reader = reads.Reader(request)
    record = plans.claim(
        session, request["plan_id"], digest=request.get("digest", ""),
        approved_summary=request.get("approved_summary", ""), username=reader.username)
    op = mutations.get_op(record.op)
    resolved = _revalidated(reader, op, record)

    try:
        result = op.apply(reader, resolved)
    except errors.AgentError as exc:
        plans.finish(session, record, error=f"{exc.code}: {exc.message}")
        raise
    except Exception as exc:
        wrapped = portal.wrap_upstream(exc, what=f"{record.op} failed during execution.")
        plans.finish(session, record, error=f"{wrapped.code}: {wrapped.message}")
        raise wrapped

    plans.finish(session, record, result=result)

    # Synchronous invalidation: the next read must not answer from a snapshot this
    # mutation just made wrong.
    cache = SnapshotCache(Path(request["session_dir"]), request["base_url"],
                          reader.username)
    invalidated = []
    for entry in record.invalidates:
        scope = {k: v for k, v in entry.items() if k != "kind"}
        invalidated.append(cache.invalidate(entry["kind"], **scope))
    return {
        "ok": True,
        "applied": record.op,
        "plan_id": record.plan_id,
        "result": result,
        "invalidated": invalidated,
        "audit": str((session.path / "runs").resolve()),
        "cost": reader.cost.to_dict(),
    }


HANDLERS = {
    "query": reads.do_query,
    "get": reads.do_get,
    "summarize": reads.do_summarize,
    "describe": reads.do_describe,
    "list": reads.do_list,
    "process_params": reads.do_process_params,
    "process_config": reads.do_process_config,
    "workflow_spec": reads.do_workflow_spec,
    "check_config": reads.do_check_config,
    "task_logs": reads.do_task_logs,
    "login_start": auth.login_start,
    "login_wait": auth.login_wait,
    "login_daemon": auth.login_daemon,
    "whoami": auth.whoami,
    "logout": auth.logout,
    "plan": do_plan,
    "apply": do_apply,
}


def run(request: dict) -> Any:
    # Route on `verb` alone: `op` is the mutation name for plan/apply and
    # must never be mistaken for a handler route.
    handler = HANDLERS.get(request.get("verb"))
    if handler is None:
        return errors.NotSupported(
            f"No handler for verb {request.get('verb')!r}.",
            what="Use a supported verb.",
            retry_with=[{"verb": name} for name in sorted(HANDLERS)],
        ).to_dict()
    try:
        return handler(request)
    except errors.AgentError as exc:
        return exc.to_dict()
    except Exception as exc:
        return portal.wrap_upstream(
            exc, what="The operation failed before completing.").to_dict()

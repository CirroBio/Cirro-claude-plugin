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
                          username=reader.username, risk=op.risk,
                          warnings=draft.warnings,
                          invalidates=draft.invalidates)
    rendered = plans.render(record.to_dict())
    return {
        "ok": True,
        "plan_id": record.plan_id,
        "digest": record.digest,
        "op": op.name,
        "risk": op.risk,
        "expires_at": record.expires_at,
        "expires_in_s": int(record.expires_at - record.created_at),
        "summary": record.summary,
        "rendered": rendered,
        "warnings": record.warnings,
        "will_invalidate": record.invalidates,
        "cost": reader.cost.to_dict(),
        "next": {
            "tool": "apply",
            "args": {"plan_id": record.plan_id, "digest": record.digest,
                     "approved_summary": record.summary},
            "note": ("Show the rendered text above to the person before calling apply. "
                     "apply prompts for approval and accepts no other arguments."),
        },
    }


def do_apply(request: dict) -> dict:
    from . import mutations
    session = _session(request)
    reader = reads.Reader(request)
    record = plans.claim(
        session, request["plan_id"], digest=request.get("digest", ""),
        approved_summary=request.get("approved_summary", ""), username=reader.username)
    op = mutations.get_op(record.op)

    try:
        result = op.apply(reader, record.args)
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

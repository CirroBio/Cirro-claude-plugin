"""The gated write path: plan, then apply.

plan is read-only and writes a single-use, digested record. apply is the only
thing in the plugin that mutates: it claims the record, re-plans from the stored
request to prove the disclosure still holds, executes, and records the outcome.
"""
from types import SimpleNamespace
from .. import errors, plans
from . import portal, reads
from ..session import Session


def do_plan(request: dict) -> dict:
    from . import mutations
    op = mutations.get_op(request["op"])
    reader = reads.Reader(request)
    draft = op.plan(reader, request.get("args") or {})
    session = reader.session
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
        "risk": record.risk,
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


def _same_disclosure(record, other) -> bool:
    """Do two plans show the person the same thing and execute the same thing?

    Compared on what the approval actually depends on -- the operation, the resolved
    arguments, the rendered figures and the warnings (render prints them) -- not on
    the digest, which includes the plan id and expiry and so never matches. This is
    the one definition; the renew path and the stale check both use it, so a change
    that counts as "new to read" on one cannot be invisible to the other.
    """
    return (record.op == other.op and record.args == other.args
            and record.summary == other.summary
            and list(record.warnings) == list(other.warnings))


def _same_plan(session: Session, previous_id: str, record) -> bool:
    """Would re-approving cost the person anything new to read?"""
    try:
        stale = plans.load(session, previous_id)
    except errors.AgentError:
        return False
    return _same_disclosure(stale, record)


def _revalidated(reader: reads.Reader, op, record) -> dict:
    """Re-plan from the original request and refuse if the disclosure has changed.

    A timeout cannot tell whether the figures a person read are still true; planning
    again can. So the expiry only has to guard intent -- whether they still mean it --
    and this guards accuracy: if the same request now resolves to different arguments
    or renders different text, what they approved is not what would run.

    Returns the record's arguments, which the re-plan has just shown to be what the
    same request resolves to now.
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
    fresh = SimpleNamespace(op=record.op, args=draft.args, summary=draft.summary,
                            warnings=draft.warnings)
    if _same_disclosure(record, fresh):
        return record.args
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
    reader = reads.Reader(request)
    session = reader.session
    record = plans.claim(
        session, request["plan_id"], digest=request.get("digest", ""),
        approved_summary=request.get("approved_summary", ""), username=reader.username)
    op = mutations.get_op(record.op)

    try:
        resolved = _revalidated(reader, op, record)
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
    cache = reader.cache
    invalidated = []
    touched = list(record.invalidates)
    if isinstance(result, dict):
        touched += result.get("invalidates") or []
    for entry in touched:
        scope = {k: v for k, v in entry.items() if k != "kind"}
        if scope:
            invalidated.append(cache.invalidate(entry["kind"], **scope))
        else:
            # No scope means every snapshot of this kind, whatever it was keyed by;
            # `invalidate` with an empty scope would compute a key nothing was stored
            # under and remove nothing.
            invalidated += cache.invalidate_kinds([entry["kind"]])
    return {
        "ok": True,
        "applied": record.op,
        "plan_id": record.plan_id,
        "result": result,
        "invalidated": invalidated,
        "audit": str((session.path / "runs").resolve()),
        "cost": reader.cost.to_dict(),
    }

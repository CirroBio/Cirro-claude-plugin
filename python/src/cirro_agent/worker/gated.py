"""The gated write path: plan, then apply.

plan is read-only and writes a single-use, digested record. apply is the only
thing in the plugin that mutates: it claims the record, re-plans from the stored
request to prove the disclosure still holds, executes, and records the outcome.
apply_granted is the same execution, claimed under a standing grant instead of a
per-plan approval.
"""
from types import SimpleNamespace
from typing import Optional
from .. import errors, plans
from . import portal, reads
from ..session import Session


def do_plan(request: dict) -> dict:
    # Lazy on purpose: importing mutations loads every op family, and a read
    # request should not pay for that.
    from . import mutations
    op = mutations.get_op(request["op"])
    reader = reads.Reader(request)
    draft = op.planned(reader, request.get("args") or {})
    session = reader.session
    risk = draft.risk or op.risk
    record = plans.create(session, op=op.name, args=draft.args, summary=draft.summary,
                          username=reader.username, risk=risk,
                          warnings=draft.warnings,
                          invalidates=draft.invalidates,
                          request_args=request.get("args") or {},
                          grant_scope=_grant_scope(reader, op, draft, risk))
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
        "grant_scope": record.grant_scope,
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

    if op.grantable and draft.ungrantable:
        response["not_grantable"] = draft.ungrantable
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
    arguments, the rendered figures, the warnings (render prints them), the risk (the
    headline marks it) and what a grant would cover (the prompt states it, and
    apply_granted matches on it) -- not on the digest, which includes the plan id and
    expiry and so never matches. This is the one definition; the renew path and the
    stale check both use it, so a change that counts as "new to read" on one cannot
    be invisible to the other.
    """
    return (record.op == other.op and record.args == other.args
            and record.summary == other.summary
            and list(record.warnings) == list(other.warnings)
            and record.risk == other.risk and record.grant_scope == other.grant_scope)


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

    It is also what makes a record trustworthy at all. The digest has no key, so
    anything that can write the session folder can recompute it; a record that matches
    what planning its own request produces now is a plan however it was written, and
    one that does not is refused. So nothing is exempt: a record with no
    `request_args` is re-planned from none, and must match what that produces.

    Returns the record's arguments, which the re-plan has just shown to be what the
    same request resolves to now.
    """
    try:
        draft = op.planned(reader, dict(record.request_args))
    except errors.AgentError as exc:
        raise errors.PlanStale(
            f"{record.op} no longer plans cleanly: {exc.message}",
            what="Something the approved plan depended on has changed.",
            retry_with=[{"tool": "plan", "op": record.op,
                         "renew_from": record.plan_id}],
        )
    risk = draft.risk or op.risk
    fresh = SimpleNamespace(op=record.op, args=draft.args, summary=draft.summary,
                            warnings=draft.warnings, risk=risk,
                            grant_scope=_grant_scope(reader, op, draft, risk))
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
    """Apply an approved plan.

    With `stop_asking`, the same prompt also approved every later plan of this kind,
    and the response carries the grant for the MCP server to hold. That is checked
    before the claim, so a plan that cannot be granted stays pending for a plain apply.
    """
    from . import mutations
    reader = reads.Reader(request)
    stop_asking = request.get("stop_asking") is True
    if stop_asking:
        planned = plans.load(reader.session, request["plan_id"])
        scope = _scope(mutations.get_op(planned.op), planned)
        _identified(reader)
        stated = plans.also_approves(scope)
        if request.get("also_approves") != stated:
            # The prompt is the consent, and it shows only this call's arguments; the
            # hook writes this line, but without the hook the model would.
            raise errors.PlanInvalid(
                "also_approves does not state what the grant would cover.",
                what="The prompt has to say exactly what is approved beyond this change.",
                field="also_approves", given=request.get("also_approves"),
                retry_with=[{"tool": "apply", "plan_id": planned.plan_id,
                             "digest": planned.digest, "stop_asking": True,
                             "also_approves": stated}])
    record = plans.claim(
        reader.session, request["plan_id"], digest=request.get("digest", ""),
        approved_summary=request.get("approved_summary", ""), username=reader.username)
    response = _execute(reader, mutations.get_op(record.op), record)
    if stop_asking:
        scope = record.grant_scope
        response["grant"] = _grant([record.op], scope["project_id"],
                                   scope["project_name"], reader)
    return response


def _grant_scope(reader: reads.Reader, op, draft, risk: str) -> Optional[dict]:
    """What a grant approving this plan would cover, or None if none may.

    Stored in the plan, inside its digest, because the prompt that asks the person to
    stop being asked states it. A plan with no project in its resolved arguments has
    no scope, rather than a scope of every project, and neither does one its planner
    marked `ungrantable` -- a variant of a grantable operation that removes data.
    """
    from . import mutations
    project_id = draft.args.get("project_id")
    if not (op.grantable and risk == mutations.RISK_WRITE and project_id
            and not draft.ungrantable):
        return None
    return {"op": op.name, "project_id": project_id,
            "project_name": reader.resolve_project(project_id)["name"],
            "session_dir": str(reader.session.path.resolve())}


def _identified(reader: reads.Reader) -> None:
    """Refuse a grant to a session whose user cannot be read. A grant is matched on
    username, and two unknown users would otherwise match each other."""
    if not reader.username:
        raise errors.AuthRequired(
            "This session's Cirro identity could not be read, so a grant cannot be "
            "tied to it.", what="A grant belongs to the user it was given as.",
            user_action="Sign in again with login_start, then ask again.")


def _grant(ops: list, project_id: str, project_name: str, reader: reads.Reader) -> dict:
    """A grant as the MCP server holds it, less the id and time it adds."""
    return {"ops": ops, "project_id": project_id, "project_name": project_name,
            "username": reader.username, "base_url": reader.base_url,
            "session_dir": str(reader.session.path.resolve())}


def do_grant(request: dict) -> dict:
    """Check a standing grant before the MCP server holds it.

    Holds nothing itself. The grant lives only in the server's memory, which is what
    ends it with the session; this resolves the project it names and refuses any
    operation that may not be granted, so the server never holds one that could not
    be honoured.
    """
    from . import mutations
    grantable = sorted(name for name, op in mutations.OPS.items() if op.grantable)
    ops = sorted(set(request.get("ops") or []))
    if not ops:
        raise errors.InvalidQuery(
            "No operations named.", what="A grant names the operations it covers.",
            field="ops", candidates=grantable,
            user_action="Ask which of these operations to approve for the session.")
    refused = [name for name in ops if name not in grantable]
    if refused:
        allowed = [name for name in ops if name in grantable]
        raise errors.NotGranted(
            f"{refused} cannot be approved in advance.",
            what=("Only a short list of operations may be approved in advance, never "
                  "one that deletes or changes access; the rest are approved each time "
                  "through apply."),
            field="ops", given=ops, candidates=grantable,
            retry_with=([{"tool": "grant", "ops": allowed,
                          "project": request.get("project")}] if allowed else []),
            user_action="Approve those operations one plan at a time through apply.",
        )
    reader = reads.Reader(request)
    _identified(reader)
    project = reader.resolve_project(request.get("project") or "")
    return {"ok": True, "grant": _grant(ops, project["id"], project["name"], reader)}


def _plain_apply(record) -> list:
    return [{"tool": "apply", "plan_id": record.plan_id, "digest": record.digest}]


def _scope(op, record) -> dict:
    """The plan's grant scope, or a refusal if it may not be approved in advance.

    `grantable` is asked of the operation as well as read off the record, so a scope
    on a plan whose operation has since left the grantable set grants nothing.
    """
    if not op.grantable:
        raise errors.NotGranted(
            f"{record.op} cannot be approved in advance.",
            what=("Only a short list of operations may be approved in advance, never "
                  "one that deletes or changes access; this one is approved each time."),
            retry_with=_plain_apply(record))
    if not record.grant_scope:
        raise errors.NotGranted(
            f"This {record.op} cannot run under a grant, though others can.",
            what=("It removes data or reaches past what a grant covers; the plan's "
                  "`not_grantable` says which. It is approved on its own."),
            retry_with=_plain_apply(record))
    return record.grant_scope


def _covering_grant(op, record, grants: list, username) -> dict:
    """The grant that approves this plan, or a refusal saying why none does.

    Checked before the plan is claimed, so a refusal leaves it pending and the
    ordinary apply still works on it.
    """
    scope = _scope(op, record)
    for grant in grants:
        if (scope["op"] in grant["ops"] and scope["project_id"] == grant["project_id"]
                and scope["session_dir"] == grant["session_dir"]
                and grant["username"] and grant["username"] == username):
            return grant
    raise errors.NotGranted(
        f"No grant in this session covers {record.op} in {scope['project_name']}.",
        what=("A grant covers the operations and the one project it names, in the "
              "session folder and as the user it was given under."),
        retry_with=_plain_apply(record),
        held=[{key: grant[key] for key in ("grant_id", "ops", "project_name")}
              for grant in grants],
    )


def do_apply_granted(request: dict) -> dict:
    """apply, approved by a standing grant rather than a prompt.

    The grants arrive in the request from the MCP server, which holds them in memory
    for the life of the session. Everything after the claim -- the re-plan, the
    execution, the audit -- is apply's own.
    """
    from . import mutations
    reader = reads.Reader(request)
    record = plans.load(reader.session, request["plan_id"])
    op = mutations.get_op(record.op)
    # Matched on the record's scope before the claim, so a refusal leaves the plan
    # pending. Nothing runs until `_execute` has re-planned and found that scope to be
    # exactly what planning the request produces now, so the grant has matched the
    # real one by then; a record whose scope was rewritten fails there instead.
    grant = _covering_grant(op, record, request.get("grants") or [], reader.username)
    claimed = plans.claim(reader.session, record.plan_id,
                          digest=request.get("digest", ""), username=reader.username,
                          granted=grant)
    response = _execute(reader, op, claimed)
    response["grant_id"] = grant["grant_id"]
    return response


def _execute(reader: reads.Reader, op, record) -> dict:
    """Run a claimed plan: re-plan, execute, record the outcome, drop stale caches."""
    session = reader.session
    try:
        resolved = _revalidated(reader, op, record)
        result = op.apply(reader, resolved)
    except errors.AgentError as exc:
        plans.finish(session, record, error=f"{exc.code}: {exc.message}")
        # A failed apply can still have changed something -- an upload registers its
        # dataset before the transfer that fails -- and the way forward is a fresh
        # plan, which must not answer from a snapshot taken before it.
        reader.cache.invalidate_entries(record.invalidates)
        raise
    except Exception as exc:
        # The plan is spent, so applying it again is refused.
        wrapped = portal.failed_write(
            exc, what=f"{record.op} failed during execution.",
            replan=[{"tool": "plan", "op": record.op, "renew_from": record.plan_id}])
        plans.finish(session, record, error=f"{wrapped.code}: {wrapped.message}")
        reader.cache.invalidate_entries(record.invalidates)
        raise wrapped

    plans.finish(session, record, result=result)
    if record.op == "chain" and isinstance(result, dict):
        # A chain's state -- attempts, outputs, where it stopped -- outlives this call.
        result["status_read"] = {"verb": "chain_status", "plan_id": record.plan_id,
                                 "command": f"cirro-agent read chain-status {record.plan_id}"}

    # Synchronous invalidation: the next read must not answer from a snapshot this
    # mutation just made wrong. A chain has already dropped its own steps' scopes as
    # it went -- it has to, so that a later step sees what an earlier one made -- and
    # reports them here, so this pass covers the plan's declared scopes and the
    # response names everything that went.
    by_the_op = (result.get("invalidated") or []) if isinstance(result, dict) else []
    invalidated = list(dict.fromkeys(
        list(by_the_op) + reader.cache.invalidate_entries(record.invalidates)))
    return {
        "ok": True,
        "applied": record.op,
        "plan_id": record.plan_id,
        "result": result,
        "invalidated": invalidated,
        "risk": record.risk,
        "grant_scope": record.grant_scope,
        "audit": str((session.path / "runs").resolve()),
        "cost": reader.cost.to_dict(),
    }

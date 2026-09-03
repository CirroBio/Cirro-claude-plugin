"""Where a chained workflow stands: its attempts, what each produced, where it stopped.

A multi-attempt chain leaves its state in the plan records and the audit log. Reading
that back by hand means opening files in `.cirro/`; this verb does it for the agent
and the person, and needs no session, credential or network.
"""
import time
from typing import Optional

from .. import errors, plans
from ..envelope import DEFAULT_MAX_TOKENS, NO_API_COST, wrap
from .reads import Reader


def _chain_plans(session) -> list:
    found = []
    for path in sorted((session.path / "plans").glob("plan_*.json")):
        try:
            plan = plans.load(session, path.stem)
        except errors.AgentError:
            continue
        if plan.op == "chain":
            found.append(plan)
    return found


def _outcome_or_none(session, plan) -> Optional[dict]:
    if plan.status not in (plans.DONE, plans.FAILED):
        return None
    try:
        return plans.outcome(session, plan)
    except errors.AgentError:
        return None


def _attempt_row(session, plan, number: int) -> dict:
    steps = plan.args.get("steps") or []
    row = {
        "attempt": number, "plan_id": plan.plan_id, "status": plan.status,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(plan.created_at)),
        "steps": [step.get("label") or step["op"] for step in steps],
        "reuses": len(plan.args.get("carried") or []),
    }
    outcome = _outcome_or_none(session, plan)
    if outcome is not None:
        result = outcome.get("result") or {}
        row["completed"] = [done.get("label") for done in result.get("completed") or []]
        if result.get("failed_at"):
            row["failed_at"] = {k: result["failed_at"].get(k)
                                for k in ("step", "label", "op", "code", "error")}
        elif outcome.get("error"):
            row["failed_at"] = {"error": outcome["error"]}
    return row


def do_chain_status(request: dict) -> dict:
    """One chain's lineage, or every chain in the folder when none is named."""
    reader = Reader(request)
    session = reader.session
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["query"])
    every = _chain_plans(session)
    wanted = request.get("plan_id")

    if not wanted:
        roots = [p for p in every if not p.args.get("root")]
        summaries = []
        for root in roots:
            retries = [p for p in every if p.args.get("root") == root.plan_id]
            attempts = [root] + sorted(retries, key=lambda p: p.created_at)
            latest = attempts[-1]
            summaries.append({
                "plan_id": root.plan_id, "attempts": len(attempts),
                "latest_status": latest.status,
                "steps": [s.get("label") or s["op"] for s in root.args.get("steps") or []],
                "retry_policy": bool(root.args.get("retry")),
            })
        return wrap(query={"verb": "chain_status"},
                    result={"chains": summaries, "n_chains": len(summaries),
                            "next_calls": [{"verb": "chain_status", "plan_id": s["plan_id"]}
                                           for s in summaries[:3]]},
                    cost=dict(NO_API_COST), max_tokens=max_tokens)

    by_id = {p.plan_id: p for p in every}
    if wanted not in by_id:
        raise errors.NotFound(
            f"No chain plan {wanted!r} in this session folder.",
            what="chain_status reads the plans this folder holds.", field="plan_id",
            given=wanted, candidates=[{"plan_id": p.plan_id} for p in every[-5:]],
            retry_with=[{"verb": "chain_status"}],
        )
    given = by_id[wanted]
    root = by_id.get(given.args.get("root") or given.plan_id, given)
    attempts = [root] + sorted((p for p in every if p.args.get("root") == root.plan_id),
                               key=lambda p: p.created_at)
    rows = [_attempt_row(session, p, i + 1) for i, p in enumerate(attempts)]
    policy = root.args.get("retry")
    finished = [r for r in rows if r["status"] in (plans.DONE, plans.FAILED)]
    result = {
        "root": root.plan_id,
        "attempts": rows,
        "attempts_used": len(finished),
        "attempts_allowed": policy["max_attempts"] if policy else 1,
        "may_change": policy["may_change"] if policy else [],
        "next_calls": [],
    }
    last = finished[-1] if finished else None
    if last and last.get("failed_at") and policy and len(finished) < policy["max_attempts"]:
        result["next_calls"].append({
            "why": "resume at the first re-runnable step with a fix",
            "tool": "plan", "op": "chain",
            "args": {"restart_from": {"plan_id": root.plan_id, "label": policy["redo"][0]}}})
    return wrap(query={"verb": "chain_status", "plan_id": wanted}, result=result,
                cost=dict(NO_API_COST), max_tokens=max_tokens)

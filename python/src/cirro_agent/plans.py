"""The plan/apply state machine.

`plan` resolves everything, validates it, works out the blast radius and writes a
single-use record. `apply` takes only a plan id, so the model cannot swap the
target after a human approved a specific one. Every refusal below exists because
the alternative is a mutation nobody agreed to:

  expired          an approval from an hour ago may no longer be meant; the figures
                   it showed are checked separately by re-planning at apply
  replayed         a plan is one mutation, not a reusable capability
  digest mismatch  the file was edited between plan and apply
  session/tenant   the plan was made against a different Cirro instance
  username         planned as one user, applied after re-logging in as another
"""
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Optional

from . import errors
from .session import Session, write_private

# An hour, not ten minutes. The clock only ever guarded two things: that the figures
# the person read are still true, and that their intent has not moved on. Only the
# second needs a clock -- the first is checked properly at apply time by re-planning
# and refusing if anything they read has changed, which no timeout can approximate.
TTL_SECONDS = 3600

# A plan id becomes a filesystem path, so it is validated before it is interpolated.
PLAN_ID_PATTERN = re.compile(r"^plan_\d{10,}_[0-9a-f]{8}$")

# Everything the human's approval depends on. `status` is excluded because claim()
# advances it; everything else must be immutable once the plan is written.
#
# The summary is in here deliberately. Covering only op/args/base_url left the approval
# TEXT outside the integrity check, so editing it on disk kept the digest valid and a
# human could approve a rename while a delete executed.
#
# `warnings` is here for the same reason and was missed: it is approval text too, shown
# by `approval_warnings` and counted by `headline`, so while it sat outside the digest a
# warning could be deleted on disk and the plan still verified. Adding it stales every
# plan already written, which is the safe direction -- they live an hour at most, and a
# stale one is refused rather than admitted.
DIGEST_FIELDS = ("op", "args", "request_args", "base_url", "summary", "username",
                 "expires_at", "session_dir", "risk", "warnings")

# `mutations.RISK_DESTRUCTIVE` is the definition; this is a copy, because that module
# imports cirro and this one is what `scripts/gate.py` reads a plan with. A test pins
# the two together.
DESTRUCTIVE_RISK = "cirro_destructive"

# Long enough for an operation and its object, short enough to stay a headline. It
# wraps on spaces where it is shown, so this is a budget rather than a hard width.
HEADLINE_CHARS = 200

PENDING = "pending"
APPLYING = "applying"
DONE = "done"
FAILED = "failed"

# A plan that was claimed and ran to a conclusion, either way.
FINISHED = (DONE, FAILED)

# Why a stored plan is not applicable. Both enforcement points -- the PreToolUse gate
# and claim() below -- reach a verdict with `preflight`, on their own reading of the
# plan file, and each phrases its own refusal in its own terms. Sharing the predicate
# means one definition of "applicable"; sharing no authority means two refusals that
# do not depend on each other, so neither can be talked out of its answer by the other
# having already said yes.
NOT_PENDING = "not_pending"
EXPIRED = "expired"
DIGEST_TAMPERED = "digest_tampered"
DIGEST_MISMATCH = "digest_mismatch"


def canonical_digest(record: dict) -> str:
    """Hash every field the approval depends on.

    This detects tampering with a stored plan. It does not prove provenance -- an
    unkeyed hash over a file the caller can write never could, and no secret kept in
    the session folder would help, since anything the user can read the agent can read.
    Provenance is not what protects the user here: the approval prompt renders the
    operation and its resolved arguments from the record itself, so a forged plan can
    only be approved by showing the human what it actually does.
    """
    payload = json.dumps({k: record.get(k) for k in DIGEST_FIELDS},
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _expires_at(record: dict) -> float:
    """The stored expiry, or zero if it cannot be read as a time.

    An unreadable expiry counts as expired. The clock cannot vouch for it, and only an
    edited file has one -- which the digest check refuses a moment later anyway.
    """
    try:
        return float(record.get("expires_at"))
    except (TypeError, ValueError):
        return 0.0


def preflight(record: dict, *, supplied_digest: Optional[str] = None) -> Optional[str]:
    """Why this stored plan may not be applied, or None if it may.

    Checked in this order because the cheapest and most conclusive answers come first:
    a plan that already ran cannot run again whatever its digest says.

    There is no grace on the expiry. Both callers read the same `time.time()` on the
    same machine, and the gate always reads it before the worker does, so slack could
    only ever admit a plan the worker will then refuse -- asking a person to approve
    something that cannot execute, which is the one thing an approval prompt must
    never do.

    Agreement with `supplied_digest` is only checked when one was offered; whether a
    caller is obliged to offer one belongs to that caller's own contract, not to
    whether the plan on disk is applicable.
    """
    if record.get("status") != PENDING:
        return NOT_PENDING
    if time.time() > _expires_at(record):
        return EXPIRED
    # Recomputed from the stored record, so an edit that leaves the digest field alone
    # is still caught. Comparing only the caller's digest to the stored one would pass.
    if canonical_digest(record) != record.get("digest"):
        return DIGEST_TAMPERED
    if supplied_digest and supplied_digest != record.get("digest"):
        return DIGEST_MISMATCH
    return None


@dataclass
class Plan:
    plan_id: str
    op: str
    args: dict
    summary: str
    digest: str
    base_url: str
    username: Optional[str]
    session_dir: str
    created_at: float
    expires_at: float
    # The arguments as the caller gave them, kept so apply can re-plan from the same
    # starting point and compare. `args` holds the resolved result of that planning.
    request_args: dict = field(default_factory=dict)
    status: str = PENDING
    risk: str = "cirro_write"
    warnings: list = field(default_factory=list)
    invalidates: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _plan_path(session: Session, plan_id: str) -> Path:
    if not PLAN_ID_PATTERN.match(plan_id or ""):
        raise errors.PlanInvalid(
            f"{plan_id!r} is not a valid plan id.",
            what="Use the plan_id exactly as the plan result reported it.",
            field="plan_id", given=plan_id,
            retry_with=[{"tool": "plan"}],
        )
    return session.path / "plans" / f"{plan_id}.json"


def stored_op(session: Session, plan_id: str) -> Optional[str]:
    """The operation a stored plan names, or None if there is no readable record.

    Deliberately not `load`: this only sizes the worker's timeout, and choosing a
    time budget is not an authorization decision. A plan too broken to read here must
    still reach the worker, which is what refuses it with the right error.
    """
    if not PLAN_ID_PATTERN.match(plan_id or ""):
        return None
    path = session.path / "plans" / f"{plan_id}.json"
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text())
    except ValueError:
        return None
    return record.get("op") if isinstance(record, dict) else None


def create(session: Session, *, op: str, args: dict, summary: str,
           username: Optional[str], risk: str = "cirro_write",
           warnings: Optional[list] = None,
           invalidates: Optional[list] = None,
           request_args: Optional[dict] = None) -> Plan:
    now = time.time()
    plan = Plan(
        plan_id=f"plan_{int(now)}_{secrets.token_hex(4)}",
        op=op,
        args=args,
        summary=summary,
        digest="",
        base_url=session.base_url,
        username=username,
        session_dir=str(session.path.resolve()),
        created_at=now,
        expires_at=now + TTL_SECONDS,
        request_args=dict(request_args or {}),
        risk=risk,
        warnings=list(warnings or []),
        invalidates=list(invalidates or []),
    )
    plan.digest = canonical_digest(plan.to_dict())
    path = _plan_path(session, plan.plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "w") as stream:
        json.dump(plan.to_dict(), stream, indent=2, default=str)
    return plan


def load(session: Session, plan_id: str) -> Plan:
    path = _plan_path(session, plan_id)
    if not path.is_file():
        raise errors.PlanInvalid(
            f"No plan {plan_id!r} in this session.",
            what="Create a plan first; a plan is required before any mutation.",
            field="plan_id", given=plan_id,
            retry_with=[{"tool": "plan", "note": "call plan for the operation you want"}],
        )
    try:
        raw = json.loads(path.read_text())
    except ValueError:
        raise errors.PlanInvalid(
            f"Plan {plan_id!r} is not readable JSON.",
            what="The plan file was corrupted or edited; make a new one.",
            retry_with=[{"tool": "plan"}],
        )
    known = set(Plan.__dataclass_fields__)
    try:
        return Plan(**{k: v for k, v in raw.items() if k in known})
    except TypeError as exc:
        # A truncated file is exactly what the tamper checks exist to catch, so it must
        # not surface as a retryable upstream fault.
        raise errors.PlanInvalid(
            f"Plan {plan_id!r} is missing required fields ({exc}); it is truncated or "
            "was not written by plan().",
            what="Discard it and plan again.",
            retry_with=[{"tool": "plan"}],
        )


def _save(session: Session, plan: Plan) -> None:
    write_private(_plan_path(session, plan.plan_id),
                  json.dumps(plan.to_dict(), indent=2, default=str))


def _not_applicable(plan_id: str, plan: Plan, reason: str) -> errors.PlanInvalid:
    """This layer's wording for a shared `preflight` verdict.

    The predicate is one definition; the phrasing and the error type are not. The gate
    writes plain text to a terminal and refuses on its own reading of the same file.
    """
    again = [{"tool": "plan", "op": plan.op}]
    if reason == NOT_PENDING:
        return errors.PlanInvalid(
            f"Plan {plan_id} is {plan.status}, not pending. A plan authorizes exactly "
            "one mutation.",
            what="Create a new plan for another attempt.",
            retry_with=again,
            status=plan.status,
        )
    if reason == EXPIRED:
        return errors.PlanInvalid(
            f"Plan {plan_id} expired at "
            f"{time.strftime('%H:%M:%SZ', time.gmtime(plan.expires_at))}.",
            what="Approval does not carry over; re-plan so the human sees current figures.",
            retry_with=again,
        )
    if reason == DIGEST_TAMPERED:
        return errors.PlanInvalid(
            f"Plan {plan_id} does not match its own digest; it was modified after it "
            "was created.",
            what="Discard it and plan again.",
            retry_with=again,
        )
    return errors.PlanInvalid(
        "The supplied digest does not match the stored plan.",
        what="Re-read the plan and use its digest verbatim.",
        retry_with=again,
    )


def claim(session: Session, plan_id: str, *, digest: str, approved_summary: str,
          username: Optional[str]) -> Plan:
    """Transition pending -> applying, atomically, or refuse with a reason."""
    plan = load(session, plan_id)

    refusal = preflight(plan.to_dict(), supplied_digest=digest)
    # claim takes the digest as a required argument, so an absent one is a caller that
    # cannot have read the plan -- a mismatch here, rather than the gate's "nothing was
    # offered to compare".
    if refusal is None and not digest:
        refusal = DIGEST_MISMATCH
    if refusal is not None:
        raise _not_applicable(plan_id, plan, refusal)

    if plan.base_url != session.base_url or plan.session_dir != str(session.path.resolve()):
        raise errors.TenantMismatch(
            f"Plan {plan_id} was made for {plan.base_url} in {plan.session_dir}, but this "
            f"session is {session.base_url} in {session.path}.",
            what="A plan is bound to the tenant and folder it was created in.",
            user_action="Work from the original session folder, or re-plan here.",
        )
    if plan.username and username and plan.username != username:
        raise errors.PlanInvalid(
            f"Plan {plan_id} was created by {plan.username} but the session is now "
            f"authenticated as {username}.",
            what="Re-plan as the current user so permissions and intent match.",
            retry_with=[{"tool": "plan", "op": plan.op}],
        )
    if approved_summary.strip() != plan.summary.strip():
        # The approval the human saw must be the plan being executed, byte for byte.
        raise errors.PlanInvalid(
            "approved_summary does not match the plan's summary.",
            what="Pass the plan's summary exactly as stored.",
            retry_with=[{"tool": "apply", "plan_id": plan_id,
                         "note": "copy summary verbatim from the plan result"}],
        )

    lock = _plan_path(session, plan.plan_id).with_suffix(".lock")
    try:
        os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        raise errors.PlanInvalid(
            f"Plan {plan_id} is already being applied.",
            what="Wait for the in-flight apply to finish, then check the result.",
            retry_with=[{"tool": "plan", "op": plan.op}],
        )

    plan.status = APPLYING
    _save(session, plan)
    record(session, plan, phase="intent")
    return plan


def finish(session: Session, plan: Plan, *, result: Any = None,
           error: Optional[str] = None) -> None:
    plan.status = FAILED if error else DONE
    _save(session, plan)
    record(session, plan, phase="outcome", result=result, error=error)


def _audit_path(session: Session, plan: Plan) -> Path:
    # Stamped from the plan, not from now: stamping at write time split one apply
    # across two files whenever intent and outcome straddled a second boundary,
    # leaving a file holding an intent that appeared never to have completed.
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(plan.created_at))
    return session.path / "runs" / f"{stamp}-{plan.op}-{plan.plan_id}.jsonl"


def outcome(session: Session, plan: Plan) -> dict:
    """The single outcome entry a finished plan recorded.

    The audit file feeds values back into later approvals (a resumed chain reuses
    what its earlier steps produced), so it is held to the same standard as the plan
    file: it must exist, every line must name this plan, and there must be exactly
    one outcome. A second outcome line is not a retry; it is someone appending.
    """
    path = _audit_path(session, plan)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        raise errors.PlanInvalid(
            f"Plan {plan.plan_id} has no audit record.",
            what="A finished plan always has one; this one cannot be resumed from.",
            retry_with=[{"op": "chain", "note": "plan a fresh chain"}],
        )
    outcomes = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            raise errors.PlanInvalid(
                f"The audit record for {plan.plan_id} is not valid JSON.",
                what="It has been edited or damaged.", user_action="Plan a fresh chain.",
            )
        if entry.get("plan_id") != plan.plan_id:
            raise errors.PlanInvalid(
                f"The audit record for {plan.plan_id} contains an entry for "
                f"{entry.get('plan_id')!r}.",
                what="Each plan's audit file holds only its own records.",
                user_action="Plan a fresh chain.",
            )
        if entry.get("phase") == "outcome":
            outcomes.append(entry)
    if len(outcomes) != 1:
        raise errors.PlanInvalid(
            f"The audit record for {plan.plan_id} has {len(outcomes)} outcome entries; "
            "a plan finishes exactly once.",
            what="Extra outcome lines mean the file was appended to after the fact.",
            user_action="Plan a fresh chain.",
        )
    return outcomes[0]


def outcomes(session: Session, plan: Plan) -> dict:
    """Per-step results a completed chain recorded, keyed by absolute step index."""
    found = {}
    for step in (outcome(session, plan).get("result") or {}).get("completed") or []:
        index = step.get("step")
        if isinstance(index, int) and isinstance(step.get("result"), dict):
            found[index - 1] = step["result"]
    return found


def chain_plans(session: Session) -> list:
    """Every chain plan this folder holds, in plan-id order.

    One file walk for both readers of a lineage. A neighbouring plan that will not load
    is skipped rather than failing the walk: a damaged file belongs to whoever asks for
    it by name, and asking for it by name still refuses.
    """
    found = []
    for path in sorted((session.path / "plans").glob("plan_*.json")):
        try:
            plan = load(session, path.stem)
        except errors.AgentError:
            continue
        if plan.op == "chain":
            found.append(plan)
    return found


def attempts_of(root: Plan, candidates: list, *, finished_only: bool) -> list:
    """The root's attempts, oldest first, from an already-walked set of chain plans.

    The root leads: a retry can only name a root that already exists, so the root is
    always the oldest, and only the retries need ordering among themselves.

    `finished_only` is the difference between the two callers and it is not cosmetic.
    A restart is a diff against something a person approved and applied, so it counts
    only claimed-and-finished attempts; chain_status reports what the folder holds, a
    pending attempt included, because that is the question it was asked.
    """
    retries = [plan for plan in candidates
               if plan.plan_id != root.plan_id
               and plan.args.get("root") == root.plan_id
               and (not finished_only or plan.status in FINISHED)]
    return [root] + sorted(retries, key=lambda plan: plan.created_at)


def lineage(session: Session, plan_id: str, *, finished_only: bool) -> tuple:
    """The root chain plan named plan_id descends from, and its attempts.

    Every restart plan records its root, so any plan in a lineage identifies the whole
    of it. The root is loaded by name rather than picked out of the walk, so a lineage
    whose root has gone missing refuses instead of quietly re-rooting itself on a
    retry -- a restart planned against the wrong root reuses the wrong outputs.
    """
    given = load(session, plan_id)
    root_id = given.args.get("root") or given.plan_id
    root = given if root_id == given.plan_id else load(session, root_id)
    return root, attempts_of(root, chain_plans(session), finished_only=finished_only)


def record(session: Session, plan: Plan, *, phase: str, result: Any = None,
           error: Optional[str] = None) -> None:
    """Append-only audit log. Written before the mutation and after it."""
    path = _audit_path(session, plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "phase": phase, "plan_id": plan.plan_id, "op": plan.op,
        "base_url": plan.base_url, "username": plan.username, "args": plan.args,
    }
    if result is not None:
        entry["result"] = result
    if error is not None:
        entry["error"] = error
    # 0600 like the plan files: the audit carries the same arguments, and a resumed
    # chain reads values back out of it.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as stream:
        stream.write(json.dumps(entry, default=str) + "\n")


def approval_header(plan: dict) -> list:
    """Tenant and identity on line one, operation and risk on line two, always.

    Shared with the PreToolUse gate, which renders the same two lines from the same
    stored record: this is the text a person actually reads before approving, and two
    copies of it could disagree about which tenant or which operation they described.
    Both are also inside the digest, so the bytes here are load-bearing -- changing
    one of them stales every plan already on disk.
    """
    return [
        f"Cirro {plan.get('base_url')} as {plan.get('username') or 'unknown user'}",
        f"Operation: {plan.get('op')}  (risk: {plan.get('risk') or 'cirro_write'})",
    ]


def approval_warnings(plan: dict) -> list:
    """The warnings block, or nothing at all when there are none.

    The block only; each caller places its own blank lines around it.
    """
    warnings = plan.get("warnings")
    if not isinstance(warnings, (list, tuple)) or not warnings:
        return []
    return ["Warnings:"] + [f"  - {warning}" for warning in warnings]


def headline(plan: dict) -> str:
    """One line naming what is about to happen, for the approval dialog.

    That dialog shows the tool's arguments and nothing else -- not the tool's
    description, and not the PreToolUse hook's explanation, which reaches the model
    instead of the person. So the one line saying what is being approved has to
    travel as an argument, which is the only reason `apply` accepts one.

    Single-line by construction. A newline inside an argument renders there as a
    literal `\\n`, which is what makes the summary unreadable in that dialog however
    well it reads everywhere else.

    Derived wholly from digested fields -- summary, risk and warnings are all in
    `DIGEST_FIELDS` -- so it cannot describe a plan other than the one it came from.
    """
    summary = (plan.get("summary") or "").strip()
    first = next((line.strip() for line in summary.splitlines() if line.strip()),
                 f"Apply {plan.get('op') or 'an operation'}.")
    line = f"DESTRUCTIVE -- {first}" if plan.get("risk") == DESTRUCTIVE_RISK else first
    warnings = plan.get("warnings")
    if isinstance(warnings, (list, tuple)) and warnings:
        # The count, not the prose: inlining warning text rebuilds the wall of
        # characters this line exists to replace. Saying how many there are is what
        # keeps one from being invisible, since the dialog shows no other trace of
        # them -- `approval_warnings` reaches only the hook's reason and the CLI.
        # Counting them is also what put `warnings` inside DIGEST_FIELDS.
        plural = "s" if len(warnings) > 1 else ""
        line = f"{line}  ({len(warnings)} warning{plural})"
    if len(line) > HEADLINE_CHARS:
        line = line[:HEADLINE_CHARS - 3].rstrip() + "..."
    return line


def render(plan: dict) -> str:
    """The text a human approves."""
    lines = approval_header(plan) + ["", plan["summary"].rstrip()]
    warnings = approval_warnings(plan)
    if warnings:
        lines += [""] + warnings
    return "\n".join(lines)

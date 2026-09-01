"""The plan/apply state machine.

`plan` resolves everything, validates it, works out the blast radius and writes a
single-use record. `apply` takes only a plan id, so the model cannot swap the
target after a human approved a specific one. Every refusal below exists because
the alternative is a mutation nobody agreed to:

  expired          an approval given ten minutes ago is not consent now
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
from .session import Session

TTL_SECONDS = 600

# A plan id becomes a filesystem path, so it is validated before it is interpolated.
PLAN_ID_PATTERN = re.compile(r"^plan_\d{10,}_[0-9a-f]{8}$")

# Everything the human's approval depends on. `status` is excluded because claim()
# advances it; everything else must be immutable once the plan is written.
#
# The summary is in here deliberately. Covering only op/args/base_url left the approval
# TEXT outside the integrity check, so editing it on disk kept the digest valid and a
# human could approve a rename while a delete executed.
DIGEST_FIELDS = ("op", "args", "base_url", "summary", "username", "expires_at",
                 "session_dir", "risk")

PENDING = "pending"
APPLYING = "applying"
DONE = "done"
FAILED = "failed"


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
    status: str = PENDING
    risk: str = "cirro_write"
    warnings: list = field(default_factory=list)
    invalidates: list = field(default_factory=list)

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

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


def create(session: Session, *, op: str, args: dict, summary: str,
           username: Optional[str], risk: str = "cirro_write",
           warnings: Optional[list] = None,
           invalidates: Optional[list] = None) -> Plan:
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
    from .session import _write_private
    _write_private(_plan_path(session, plan.plan_id),
                   json.dumps(plan.to_dict(), indent=2, default=str))


def claim(session: Session, plan_id: str, *, digest: str, approved_summary: str,
          username: Optional[str]) -> Plan:
    """Transition pending -> applying, atomically, or refuse with a reason."""
    plan = load(session, plan_id)

    if plan.status != PENDING:
        raise errors.PlanInvalid(
            f"Plan {plan_id} is {plan.status}, not pending. A plan authorizes exactly "
            "one mutation.",
            what="Create a new plan for another attempt.",
            retry_with=[{"tool": "plan", "op": plan.op}],
            status=plan.status,
        )
    if plan.expired:
        raise errors.PlanInvalid(
            f"Plan {plan_id} expired at "
            f"{time.strftime('%H:%M:%SZ', time.gmtime(plan.expires_at))}.",
            what="Approval does not carry over; re-plan so the human sees current figures.",
            retry_with=[{"tool": "plan", "op": plan.op}],
        )
    # Integrity of the stored record, not just agreement with the caller: recompute
    # the digest from what is on disk. Comparing the caller's digest to the stored one
    # would pass even if the args had been edited afterwards, since an editor can leave
    # the digest field untouched.
    if canonical_digest(plan.to_dict()) != plan.digest:
        raise errors.PlanInvalid(
            f"Plan {plan_id} does not match its own digest; it was modified after it "
            "was created.",
            what="Discard it and plan again.",
            retry_with=[{"tool": "plan", "op": plan.op}],
        )
    if digest != plan.digest:
        raise errors.PlanInvalid(
            "The supplied digest does not match the stored plan.",
            what="Re-read the plan and use its digest verbatim.",
            retry_with=[{"tool": "plan", "op": plan.op}],
        )
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


def record(session: Session, plan: Plan, *, phase: str, result: Any = None,
           error: Optional[str] = None) -> None:
    """Append-only audit log. Written before the mutation and after it."""
    runs = session.path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = runs / f"{stamp}-{plan.op}-{plan.plan_id}.jsonl"
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "phase": phase, "plan_id": plan.plan_id, "op": plan.op,
        "base_url": plan.base_url, "username": plan.username, "args": plan.args,
    }
    if result is not None:
        entry["result"] = result
    if error is not None:
        entry["error"] = error
    with path.open("a") as stream:
        stream.write(json.dumps(entry, default=str) + "\n")


def render(plan: dict) -> str:
    """The text a human approves. Tenant and identity on line one, always."""
    lines = [
        f"Cirro {plan['base_url']} as {plan.get('username') or 'unknown user'}",
        f"Operation: {plan['op']}  (risk: {plan.get('risk', 'cirro_write')})",
        "",
        plan["summary"].rstrip(),
    ]
    if plan.get("warnings"):
        lines += ["", "Warnings:"] + [f"  - {w}" for w in plan["warnings"]]
    return "\n".join(lines)

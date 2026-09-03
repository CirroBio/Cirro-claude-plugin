"""Typed errors carrying their own remediation.

The invariant every code must satisfy: an error tells the caller either how to
fix the call itself (`retry_with`, a runnable call) or what only a human can do
(`user_action`). Never neither -- an error with no way forward makes an agent
guess, and guessing is how the wrong dataset gets deleted.
"""
from typing import Any, Optional

# severity tells the agent who can act: itself, the human, or nobody yet.
AGENT_ACTION = "agent_action"
USER_ACTION = "user_action"
RETRYABLE = "retryable"
FATAL = "fatal"


class AgentError(Exception):
    code = "INTERNAL"
    severity = FATAL

    def __init__(
        self,
        message: str,
        *,
        what: str,
        field: Optional[str] = None,
        given: Any = None,
        candidates: Optional[list] = None,
        retry_with: Optional[list] = None,
        user_action: Optional[str] = None,
        **detail: Any,
    ):
        super().__init__(message)
        self.message = message
        self.what = what
        self.field = field
        self.given = given
        self.candidates = candidates or []
        self.retry_with = retry_with or []
        self.user_action = user_action
        self.detail = {k: v for k, v in detail.items() if v is not None}
        if not self.retry_with and not self.user_action:
            raise AssertionError(
                f"{self.code} was raised with no remediation; every error must carry "
                "retry_with or user_action"
            )

    def to_dict(self, cost: Optional[dict] = None) -> dict:
        out = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "severity": self.severity,
                "remediation": {
                    "what": self.what,
                    "candidates": self.candidates,
                    "retry_with": self.retry_with,
                    "user_action": self.user_action,
                },
            },
        }
        if self.field is not None:
            out["error"]["field"] = self.field
        if self.given is not None:
            out["error"]["given"] = self.given
        if self.detail:
            out["error"]["detail"] = self.detail
        if cost:
            out["cost"] = cost
        return out


def _err(code: str, severity: str) -> type:
    return type(code, (AgentError,), {"code": code, "severity": severity})


# Session and runtime -- the human has to act on all of these.
NoSessionFolder = _err("NO_SESSION_FOLDER", USER_ACTION)
TenantMismatch = _err("TENANT_MISMATCH", USER_ACTION)
NetworkUnavailable = _err("NETWORK_UNAVAILABLE", RETRYABLE)
RuntimeNotInstalled = _err("RUNTIME_NOT_INSTALLED", USER_ACTION)

# Auth. Never resolvable by the agent: a device-code login needs a browser.
AuthRequired = _err("AUTH_REQUIRED", USER_ACTION)
AuthExpired = _err("AUTH_EXPIRED", USER_ACTION)
Forbidden = _err("FORBIDDEN", USER_ACTION)

# Resolution. The agent can fix all of these from the payload we hand back.
AmbiguousName = _err("AMBIGUOUS_NAME", AGENT_ACTION)
NotFound = _err("NOT_FOUND", AGENT_ACTION)
ScopeRequired = _err("SCOPE_REQUIRED", AGENT_ACTION)
NameRequiredId = _err("NAME_REQUIRED_ID", AGENT_ACTION)
InvalidQuery = _err("INVALID_QUERY", AGENT_ACTION)

# Cost and completeness.
CostGate = _err("COST_GATE", AGENT_ACTION)

# Shape mismatches between what was asked and what this entity supports.
WrongExecutor = _err("WRONG_EXECUTOR", AGENT_ACTION)
WrongStatus = _err("WRONG_STATUS", AGENT_ACTION)
NotSupported = _err("NOT_SUPPORTED", FATAL)

# Plan/apply gate.
PlanInvalid = _err("PLAN_INVALID", AGENT_ACTION)
# The plan is intact and unexpired, but the facts it described have moved. Distinct
# from PLAN_INVALID because the remedy is to re-plan and show the person the new
# figures, not to fix a malformed request.
PlanStale = _err("PLAN_STALE", AGENT_ACTION)

# Upstream.
UpstreamError = _err("UPSTREAM_ERROR", RETRYABLE)

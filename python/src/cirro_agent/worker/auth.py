"""Login that never blocks a tool call.

The SDK has no way to resume a device-code flow in another process -- that would
need the private `_await_completion`. So `login_start` spawns a detached child
which owns the `DeviceCodeAuth` object for the whole flow: the child publishes the
authorization message to `login-flow.json` as soon as it has one, then blocks in
`await_completion()` and writes `login-status.json` when it resolves.

`login_wait` polls that file against its OWN deadline, independent of the device
code's lifetime, so no tool call can hang even if the human walks away.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .. import errors
from . import portal

FLOW_FILE = "login-flow.json"
STATUS_FILE = "login-status.json"
PID_FILE = "login.pid"

FLOW_PUBLISH_TIMEOUT = 25.0


def _home() -> Path:
    return Path(os.environ["CIRRO_HOME"])


def _write(path: Path, payload: dict) -> None:
    from ..session import _write_private
    _write_private(path, json.dumps(payload, default=str))


def _read(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _child_alive() -> bool:
    raw = _read(_home() / PID_FILE)
    if not raw:
        return False
    try:
        os.kill(int(raw["pid"]), 0)
        return True
    except (OSError, ValueError, KeyError):
        return False


def _verify(base_url: str) -> dict:
    """Prove the credential works with a real authenticated call.

    A stored expiry is a claim; listing projects is evidence. Reporting the claim
    alone would let a revoked or keychain-stale token look healthy.
    """
    from cirro_api_client.v1.api.projects import get_projects
    auth, config = portal.build_auth(base_url)
    username = portal.identity(auth)
    from cirro.cirro_client import CirroApi
    client = CirroApi(auth_info=auth, base_url=base_url, user_agent="cirro-claude-plugin")
    projects = get_projects.sync(client=client._api_client) or []
    return {
        "username": username,
        "base_url": base_url,
        "verified_by": f"listed {len(projects)} projects",
        "n_projects": len(projects),
        "session_dir": os.environ["CIRRO_HOME"],
        "token_file": str(portal.token_path(config)),
    }


def login_start(request: dict) -> dict:
    base_url = request["base_url"]
    home = _home()
    home.mkdir(parents=True, exist_ok=True)

    try:
        info = _verify(base_url)
        return {"ok": True, "status": "already_authenticated", **info}
    except (errors.AuthRequired, errors.AuthExpired) as exc:
        reason = exc.code
    # Network and upstream failures are not a reason to start a login.

    if _child_alive():
        flow = _read(home / FLOW_FILE)
        if flow:
            return {"ok": True, "status": "pending", "adopted_existing": True, **flow}

    for stale in (FLOW_FILE, STATUS_FILE, PID_FILE):
        (home / stale).unlink(missing_ok=True)

    child = subprocess.Popen(
        [sys.executable, "-m", "cirro_agent.worker"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=dict(os.environ),
    )
    child.stdin.write(json.dumps({"verb": "login_daemon", "base_url": base_url,
                                  "session_dir": str(home)}).encode())
    child.stdin.close()
    _write(home / PID_FILE, {"pid": child.pid, "started_at": time.time()})

    deadline = time.monotonic() + FLOW_PUBLISH_TIMEOUT
    while time.monotonic() < deadline:
        flow = _read(home / FLOW_FILE)
        if flow:
            return {"ok": True, "status": "pending", "reason": reason, **flow}
        failure = _read(home / STATUS_FILE)
        if failure and failure.get("status") == "failed":
            raise errors.AuthRequired(
                f"Could not start a login: {failure.get('error')}",
                what="Cirro did not issue a device code.",
                user_action=f"Check network access to {base_url} and try again.",
            )
        time.sleep(0.3)

    raise errors.NetworkUnavailable(
        f"Cirro at {base_url} did not issue a device code within "
        f"{int(FLOW_PUBLISH_TIMEOUT)}s.",
        what="The authorization request did not come back.",
        user_action=f"Check network access to {base_url}, then retry login.",
    )


def login_daemon(request: dict) -> dict:
    """Runs detached. Owns the DeviceCodeAuth object for the whole flow."""
    base_url = request["base_url"]
    home = _home()
    try:
        auth, _config = portal.build_auth(base_url, start_flow=True)
    except Exception as exc:
        _write(home / STATUS_FILE, {"status": "failed", "error": str(exc),
                                    "at": time.time()})
        return {"ok": False}

    if auth._token_info is not None:
        _write(home / STATUS_FILE, {"status": "ok", "username": portal.identity(auth),
                                    "at": time.time()})
        return {"ok": True}

    try:
        _write(home / FLOW_FILE, {
            "auth_message": auth.auth_message,
            "auth_message_markdown": auth.auth_message_markdown,
            "base_url": base_url,
        })
    except ValueError as exc:
        _write(home / STATUS_FILE, {"status": "failed", "error": str(exc), "at": time.time()})
        return {"ok": False}

    try:
        auth.await_completion()
    except Exception as exc:
        _write(home / STATUS_FILE, {"status": "failed", "error": str(exc), "at": time.time()})
        return {"ok": False}

    _write(home / STATUS_FILE, {"status": "ok", "username": portal.identity(auth),
                                "at": time.time()})
    (home / FLOW_FILE).unlink(missing_ok=True)
    return {"ok": True}


def login_wait(request: dict) -> dict:
    base_url = request["base_url"]
    home = _home()
    timeout = float(request.get("timeout_s") or 120)
    deadline = time.monotonic() + min(timeout, 240.0)

    while time.monotonic() < deadline:
        status = _read(home / STATUS_FILE)
        if status and status.get("status") == "ok":
            (home / PID_FILE).unlink(missing_ok=True)
            return {"ok": True, "status": "ok", **_verify(base_url)}
        if status and status.get("status") == "failed":
            (home / PID_FILE).unlink(missing_ok=True)
            error = str(status.get("error", ""))
            if "timed out" in error.lower():
                raise errors.AuthRequired(
                    "The device code expired before the login was completed.",
                    what="Device codes are short-lived.",
                    user_action="Start a new login and open the link promptly.",
                )
            raise errors.AuthRequired(
                f"Login failed: {error}",
                what="Cirro rejected or abandoned the authorization.",
                user_action="Start a new login.",
            )
        if not _child_alive() and not status:
            raise errors.AuthRequired(
                "The login helper exited without recording a result.",
                what="The background login process is gone.",
                user_action="Start a new login.",
            )
        time.sleep(1.0)

    flow = _read(home / FLOW_FILE) or {}
    return {
        "ok": True, "status": "pending",
        "waited_s": int(min(timeout, 240.0)),
        "note": ("Still waiting for the browser login. This is not an error -- relay the "
                 "link again and call login_wait once more."),
        **flow,
    }


def whoami(request: dict) -> dict:
    base_url = request["base_url"]
    import importlib.metadata as md
    try:
        version = md.version("cirro")
    except Exception:
        version = "unknown"
    import cirro
    sdk_path = Path(cirro.__file__).resolve()
    editable = ".dev-venv" not in str(sdk_path) and "site-packages" not in str(sdk_path)

    runtime = {
        "cirro_version": version,
        "cirro_path": str(sdk_path.parent),
        # A stale editable checkout must never masquerade as the pinned release.
        "cirro_is_editable_checkout": editable,
        "python": sys.version.split()[0],
        "session_dir": os.environ["CIRRO_HOME"],
        "auth_mode": portal.auth_mode(),
    }
    try:
        return {"ok": True, "authenticated": True, "runtime": runtime, **_verify(base_url)}
    except errors.AgentError as exc:
        payload = exc.to_dict()
        payload["authenticated"] = False
        payload["runtime"] = runtime
        return payload


def logout(request: dict) -> dict:
    base_url = request["base_url"]
    home = _home()
    removed = []
    try:
        config = portal.app_config(base_url)
        path = portal.token_path(config)
        if path.exists():
            path.unlink()
            removed.append(str(path))
    except errors.AgentError:
        for path in home.glob("*.token.dat"):
            path.unlink(missing_ok=True)
            removed.append(str(path))
    for name in (FLOW_FILE, STATUS_FILE, PID_FILE, "config.ini"):
        target = home / name
        if target.exists():
            target.unlink()
            removed.append(str(target))

    note = None
    if sys.platform.startswith("darwin"):
        # The token bytes live in the login Keychain, keyed by a hash of the token
        # path. Deleting the signal file forces re-auth (_load_token_info gates on
        # its existence), but the keychain item survives -- and silently editing a
        # user's keychain is not this plugin's business.
        note = ("On macOS the token itself is stored in your login Keychain, not in the "
                "session folder. Removing the file above forces a fresh login. To erase "
                "the keychain entry as well, run: "
                "security delete-generic-password -s cirro-client")
    return {"ok": True, "removed": removed, "session_dir": str(home), "note": note}

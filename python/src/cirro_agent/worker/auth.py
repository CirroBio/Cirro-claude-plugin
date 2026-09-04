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
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .. import errors
from ..session import write_private
from . import portal

FLOW_FILE = "login-flow.json"
STATUS_FILE = "login-status.json"
PID_FILE = "login.pid"

FLOW_PUBLISH_TIMEOUT = 25.0


def _home() -> Path:
    return Path(os.environ["CIRRO_HOME"])


def _write(path: Path, payload: dict) -> None:
    write_private(path, json.dumps(payload, default=str))


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


def _flow_url(auth_obj) -> Optional[str]:
    """The one-click URL, with the code already in it.

    `message` is prose meant for a human to read; the flow carries the two parts
    separately, so build the link from those and fall back to scraping the prose only
    if the shape ever changes.
    """
    flow = getattr(auth_obj, "_flow", None) or {}
    uri, code = flow.get("verification_uri"), flow.get("user_code")
    if uri and code:
        return f"{uri}?user_code={code}"
    # `or ""` rather than a default: a null message is a TypeError here, and this
    # runs inside the daemon's _write argument, where nothing would record the fault.
    found = re.search(r"https?://\S+", flow.get("message") or "")
    return found.group(0) if found else None


CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _printable(text: str) -> str:
    """Strip anything the terminal would act on rather than show.

    Both strings written below come verbatim from the tenant's /device-code
    response. This is the one channel deliberately trusted more than the agent's
    relay, which is exactly what makes forging it worth something: ANSI and OSC
    sequences would let a hostile tenant overwrite the real link with another,
    print a fake "signed in as", or drive the clipboard through OSC 52.
    """
    return CONTROL_CHARS.sub("", text or "")


def _announce(url: str, message: str) -> bool:
    """Write the link straight to the terminal, bypassing the agent entirely.

    Not every host has a controlling terminal -- a desktop app has none -- so
    failure here is normal and silent, and the agent relays the link either way.
    """
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(f"\n  Cirro login: {_printable(url)}\n"
                      f"  {_printable(message)}\n\n")
            tty.flush()
        return True
    except OSError:
        return False


def _has_credential(home: Path) -> bool:
    """Is there anything here that could possibly be verified?

    The token file is named for the tenant's client_id, which costs a round trip to
    learn. The glob costs nothing and answers the same question, so a genuine first
    login skips a GET /api/info/system it could not have used.

    Only the device-code flow keeps anything on disk -- on macOS the file is a signal
    and the secret is in the Keychain, but it is still touched on save. The headless
    modes carry their credential in the environment, so for them the answer is always
    yes and the verification below is what decides.
    """
    if portal.auth_mode() != portal.AUTH_DEVICE_CODE:
        return True
    return any(home.glob("*.token.dat"))


def _pending(home: Path, flow: dict, **extra) -> dict:
    """Print the link to the terminal, once, and build the pending envelope.

    Printing is a courtesy channel -- the host may have no terminal, and a
    full-screen TUI may redraw over it -- so the agent is told to relay the link
    regardless. `printed_to_terminal` reports what actually happened rather than
    what was attempted.

    The flow file records that it has been printed so that re-entering through an
    adopted login does not spam the terminal with the same code again.
    """
    url = flow.get("verification_url")
    if url and not flow.get("printed_at"):
        printed = _announce(url, flow.get("auth_message", ""))
        flow = {**flow, "printed_at": time.time(), "printed_to_terminal": printed}
        _write(home / FLOW_FILE, flow)
    return {
        "ok": True, "status": "pending",
        **flow,
        "printed_to_terminal": bool(flow.get("printed_to_terminal")),
        "relay_note": ("Show verification_url to the user now, in your next message, "
                       "before anything else. Printing to their terminal is a courtesy "
                       "that may not have reached them."),
        **extra,
    }


def login_start(request: dict) -> dict:
    base_url = request["base_url"]
    home = _home()
    home.mkdir(parents=True, exist_ok=True)

    reason = "no_credential"
    if _has_credential(home):
        try:
            # A stored access token that has merely expired is not a reason to log in
            # again: the SDK refreshes it against the refresh token on the first real
            # call, which is exactly what _verify makes. Only a credential that cannot
            # be refreshed reaches the branches below.
            info = _verify(base_url)
            # Sweep the remains of whatever login produced this credential. A
            # leftover {"status": "failed"} would otherwise outlive it and make a
            # later login_wait raise against a folder that is signed in.
            for stale in (FLOW_FILE, STATUS_FILE, PID_FILE):
                (home / stale).unlink(missing_ok=True)
            return {"ok": True, "status": "already_authenticated", **info}
        except (errors.AuthRequired, errors.AuthExpired) as exc:
            reason = exc.code
        except errors.AgentError:
            raise
        except Exception as exc:
            # A refusal from the refresh itself -- a revoked or rotated refresh token
            # surfaces as RuntimeError out of cirro.auth, not as a pre-flight error.
            # Catching only the pre-flight errors left this case unable to recover:
            # no login was started and the caller was told to start one.
            mapped = portal.wrap_upstream(exc, what="Verifying the stored credential.")
            if not isinstance(mapped, errors.AuthExpired):
                raise mapped
            reason = mapped.code
        # A network or upstream failure is not a reason to start a login.

    if _child_alive():
        # A live child that has not published yet is simply mid-request -- issuing the
        # device code takes a second or two. Orphaning it here and spawning a second
        # daemon put two codes in flight against one tenant: whichever the person did
        # not use kept polling to expiry, then overwrote the successful child's result
        # with "Authentication timed out".
        adopted = _await_flow(home, base_url, FLOW_PUBLISH_TIMEOUT)
        if adopted is not None:
            return {**adopted, "adopted_existing": True}

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

    published = _await_flow(home, base_url, FLOW_PUBLISH_TIMEOUT)
    if published is None:
        raise errors.NetworkUnavailable(
            f"Cirro at {base_url} did not issue a device code within "
            f"{int(FLOW_PUBLISH_TIMEOUT)}s.",
            what="The authorization request did not come back.",
            user_action=f"Check network access to {base_url}, then retry login.",
        )
    return {**published, "reason": reason}


def _await_flow(home: Path, base_url: str, timeout: float) -> Optional[dict]:
    """Watch for a daemon to publish a code, settle, or fail. None means neither.

    Shared by the spawn path and by adoption of a daemon that is already running, so
    that a child mid-request is waited on rather than replaced.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        flow = _read(home / FLOW_FILE)
        if flow:
            return _pending(home, flow)
        settled = _read(home / STATUS_FILE)
        if settled and settled.get("status") == "ok":
            # The daemon loaded a usable token instead of issuing a code, so no link
            # is coming. Without this arm the loop waited out the full timeout and
            # reported a network failure for a login that had just succeeded.
            (home / PID_FILE).unlink(missing_ok=True)
            return {"ok": True, "status": "already_authenticated", **_verify(base_url)}
        if settled and settled.get("status") == "failed":
            raise errors.AuthRequired(
                f"Could not start a login: {settled.get('error')}",
                what="Cirro did not issue a device code.",
                user_action=f"Check network access to {base_url} and try again.",
            )
        if not _child_alive():
            return None
        time.sleep(0.3)
    return None


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
        # Publishing the link is the only thing between the person and finishing, so
        # nothing else -- launching a browser above all -- happens before this write.
        _write(home / FLOW_FILE, {
            "auth_message": auth.auth_message,
            "auth_message_markdown": auth.auth_message_markdown,
            "verification_url": _flow_url(auth),
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
    requested = request.get("timeout_s")
    # `or` here would turn an explicit 0 -- "just tell me the state now" -- into a
    # two-minute block, which is the opposite of what the caller asked for.
    timeout = 120.0 if requested is None else max(0.0, min(float(requested), 240.0))
    deadline = time.monotonic() + timeout
    started = _child_alive() or (home / PID_FILE).exists()

    while True:
        # The state is read before the deadline is tested, so timeout_s=0 reports what
        # is true right now. Testing the deadline first made a finished login read as
        # still pending.
        status = _read(home / STATUS_FILE)
        if status and status.get("status") == "ok":
            (home / PID_FILE).unlink(missing_ok=True)
            return {"ok": True, "status": "ok", "was_already_logged_in": False,
                    **_verify(base_url)}
        if status and status.get("status") == "failed":
            (home / PID_FILE).unlink(missing_ok=True)
            raise _login_failed(str(status.get("error", "")))
        if not _child_alive() and not status:
            return _no_login_running(home, base_url, started)
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)

    flow = _read(home / FLOW_FILE) or {}
    return _pending(home, flow, waited_s=int(timeout),
                    note=("Still waiting for the browser login. This is not an error "
                          "-- relay the link again and call login_wait once more."))


def _login_failed(error: str) -> errors.AgentError:
    if "timed out" in error.lower():
        return errors.AuthRequired(
            "The device code expired before the login was completed.",
            what="Device codes are short-lived.",
            user_action="Start a new login and open the link promptly.",
        )
    return errors.AuthRequired(
        f"Login failed: {error}",
        what="Cirro rejected or abandoned the authorization.",
        user_action="Start a new login.",
    )


def _no_login_running(home: Path, base_url: str, started: bool) -> dict:
    """No helper, no result. Either nothing was ever started, or it died.

    Those look identical from the files alone, so `started` records whether a pidfile
    existed when the wait began. Without that distinction a helper killed before it
    could publish anything was reported as a quiet, idle session.
    """
    if started or (home / FLOW_FILE).exists():
        raise errors.AuthRequired(
            "The login helper exited without recording a result.",
            what="The background login process is gone.",
            user_action="Start a new login.",
            retry_with=[{"tool": "login_start", "base_url": base_url}],
        )
    try:
        return {"ok": True, "status": "ok", "was_already_logged_in": True,
                **_verify(base_url)}
    except (errors.AuthRequired, errors.AuthExpired):
        raise errors.AuthRequired(
            "No login is in progress and this folder is not logged in.",
            what="login_wait was called without a login to wait for.",
            user_action="Start a login first.",
            retry_with=[{"tool": "login_start", "base_url": base_url}],
        )


def whoami(request: dict) -> dict:
    base_url = request["base_url"]
    import importlib.metadata as md
    try:
        version = md.version("cirro")
    except Exception:
        version = "unknown"
    import cirro
    sdk_path = Path(cirro.__file__).resolve()
    editable = "dev-venv" not in str(sdk_path) and "site-packages" not in str(sdk_path)

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


def _stop_login(home: Path) -> bool:
    """Kill a login still in flight before discarding the credential.

    Removing the pidfile only makes the daemon invisible. It keeps polling, and on
    success writes the token straight back -- so a logout during a login handed the
    credential back moments after the user asked to be rid of it.
    """
    raw = _read(home / PID_FILE)
    if not raw:
        return False
    try:
        os.kill(int(raw["pid"]), signal.SIGTERM)
        return True
    except (OSError, ValueError, KeyError):
        return False


def logout(request: dict) -> dict:
    base_url = request["base_url"]
    home = _home()
    stopped = _stop_login(home)
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
    return {"ok": True, "removed": removed, "stopped_login": stopped,
            "session_dir": str(home), "note": note}

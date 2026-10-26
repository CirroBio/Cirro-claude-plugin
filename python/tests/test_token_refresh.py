"""Serializing the token refresh across worker processes.

The SDK refreshes under a `threading.Lock`, and this plugin runs every operation in
its own subprocess, so that lock spans nothing. Two workers sharing a session folder
therefore refresh together, and `_refresh_access_token` deletes the stored credential
on *any* ClientError -- so the throttle that concurrency provokes logs both of them
out for good. These tests cover the file lock that closes that window.
"""
import json
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from cirro_agent.worker import portal

SRC = str(Path(__file__).resolve().parents[1] / "src")


class FakeAuth:
    """The surface of DeviceCodeAuth that `renew_under_lock` drives."""

    def __init__(self, *, expires_in, persisted=None):
        self._token_info = {"access_token": "current"}
        self.access_token_expiry = datetime.now() + expires_in
        self._persisted = persisted
        self.refreshes = 0

    def _load_token_info(self):
        return self._persisted

    def _update_token_metadata(self):
        self.access_token_expiry = datetime.now() + timedelta(hours=1)

    def _refresh_access_token(self):
        self.refreshes += 1
        self._token_info = {"access_token": "renewed"}
        self.access_token_expiry = datetime.now() + timedelta(hours=1)


def test_a_token_with_time_left_is_not_touched(session_dir):
    auth = FakeAuth(expires_in=timedelta(hours=1))
    portal.renew_under_lock(auth)
    assert auth.refreshes == 0
    assert not (session_dir.path / portal.LOCK_NAME).exists(), (
        "an unnecessary renewal still took the lock, which serializes workers "
        "that had no reason to wait for each other")


def test_an_expired_token_is_renewed(session_dir):
    auth = FakeAuth(expires_in=timedelta(seconds=-1))
    portal.renew_under_lock(auth)
    assert auth.refreshes == 1
    assert auth._token_info["access_token"] == "renewed"


def test_a_token_inside_the_margin_is_renewed_before_the_operation(session_dir):
    """Otherwise it expires mid-operation and is refreshed lazily, outside the lock --
    which is the unserialized refresh this exists to prevent."""
    auth = FakeAuth(expires_in=portal.REFRESH_MARGIN / 2)
    portal.renew_under_lock(auth)
    assert auth.refreshes == 1


def test_a_renewal_another_worker_finished_is_adopted_not_repeated(session_dir):
    """The point of re-reading the store under the lock: N workers waking together
    cost one Cognito call, not N serialized ones -- and serialized calls can still
    trip the throttle that deletes the credential."""
    auth = FakeAuth(expires_in=timedelta(seconds=-1),
                    persisted={"access_token": "renewed-by-someone-else"})
    portal.renew_under_lock(auth)
    assert auth.refreshes == 0
    assert auth._token_info["access_token"] == "renewed-by-someone-else"


CHILD = textwrap.dedent("""
    import json, os, sys, time
    from datetime import datetime, timedelta
    sys.path.insert(0, {src!r})
    os.environ["CIRRO_HOME"] = {home!r}
    from cirro_agent.worker import portal

    class Auth:
        _token_info = {{"access_token": "current"}}
        access_token_expiry = datetime.now() - timedelta(seconds=1)
        def _load_token_info(self): return None
        def _update_token_metadata(self): pass
        def _refresh_access_token(self):
            entered = time.monotonic()
            time.sleep(0.4)
            with open({log!r}, "a") as handle:
                handle.write(json.dumps([entered, time.monotonic()]) + "\\n")

    portal.renew_under_lock(Auth())
""")


def test_the_lock_holds_between_processes(session_dir, tmp_path):
    """The whole premise. A threading.Lock would let these overlap."""
    log = tmp_path / "refreshes.jsonl"
    script = CHILD.format(src=SRC, home=str(session_dir.path), log=str(log))
    children = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(2)]
    for child in children:
        assert child.wait(timeout=30) == 0

    spans = sorted(json.loads(line) for line in log.read_text().splitlines())
    assert len(spans) == 2
    (first_in, first_out), (second_in, _) = spans
    assert second_in >= first_out, (
        f"the refreshes overlapped ({first_in:.3f}-{first_out:.3f} and "
        f"{second_in:.3f}-...); the lock is not serializing across processes")


class FakeKeychainError(OSError):
    """`msal_extensions.osx.KeychainError`, which is what actually reaches us.

    Reproduced rather than imported so the mapping is pinned to the shape that
    caused the bug -- `super().__init__()` with no arguments, so `str()` is empty
    and `exit_status` carries the whole diagnosis. `test_runtime_contract.py`
    holds the real class to this shape.
    """

    __module__ = "msal_extensions.osx"

    def __init__(self, exit_status):
        super().__init__()
        self.exit_status = exit_status


def test_a_locked_keychain_is_not_a_cirro_fault():
    """An 8-step chain outlives its access token, so the SDK refreshes mid-run and
    rewrites the Keychain. A login keychain that re-locked refuses with -25308, and
    that arrived as `UPSTREAM_ERROR: "KeychainError: "` -- retryable severity, an
    empty message, and advice to check Cirro's service status for a local fault."""
    wrapped = portal.wrap_upstream(FakeKeychainError(exit_status=-25308),
                                   what="run_analysis failed during execution.")
    assert wrapped.code == "CREDENTIAL_STORE_UNAVAILABLE"
    assert wrapped.severity == "user_action", "retrying cannot unlock a keychain"
    assert "-25308" in wrapped.message and "locked" in wrapped.message
    assert wrapped.detail["exit_status"] == -25308
    assert "service status" not in (wrapped.user_action or "")


def test_a_dismissed_prompt_is_not_answered_with_unlock_your_keychain():
    """-128 is the person clicking Deny, not a locked keychain, so the two cannot
    share advice: telling them to unlock what is already unlocked explains nothing."""
    wrapped = portal.wrap_upstream(FakeKeychainError(exit_status=-128), what="w")
    assert "Allow" in wrapped.user_action and "Unlock" not in wrapped.user_action


def test_a_missing_keychain_item_sends_the_person_to_log_in_again():
    """The credential is gone rather than unreachable, so unlocking cannot help."""
    wrapped = portal.wrap_upstream(FakeKeychainError(exit_status=-25300),
                                   what="w")
    assert wrapped.code == "CREDENTIAL_STORE_UNAVAILABLE"
    assert "login_start" in wrapped.user_action


def test_an_unnamed_keychain_status_still_reports_its_number():
    """The table cannot cover every MacErrors.h code, but the number is the one
    fact that lets a person look the failure up."""
    wrapped = portal.wrap_upstream(FakeKeychainError(exit_status=-26276), what="w")
    assert "-26276" in wrapped.message
    assert wrapped.detail["exit_status"] == -26276


def test_an_unnamed_keychain_status_is_not_diagnosed_as_a_locked_keychain():
    """-67701 reached a person as "unlock your login keychain" from a keychain that
    was not locked; an unknown status says what is worth checking, not what broke."""
    wrapped = portal.wrap_upstream(FakeKeychainError(exit_status=-67701), what="w")
    assert wrapped.user_action.startswith("Run this again")
    assert "locked" not in wrapped.message


def test_two_workers_saving_one_sign_in_is_a_race_to_rerun_not_a_locked_keychain():
    """-25299, a duplicate item, came back from a plan while another worker on the
    same session folder was saving the token; the plan succeeded when run again."""
    wrapped = portal.wrap_upstream(FakeKeychainError(exit_status=-25299), what="w")
    assert wrapped.code == "CREDENTIAL_STORE_UNAVAILABLE"
    assert "same moment" in wrapped.message
    assert wrapped.user_action.startswith("Run this again")
    assert "Unlock" not in wrapped.user_action


def test_an_exception_with_no_message_does_not_render_a_dangling_colon():
    """The general form of the bug, for whatever raises it next: an exception built
    with no arguments has an empty `str()`, and name-colon-nothing reads as a message
    that got truncated on the way out."""
    class Silent(RuntimeError):
        pass

    assert portal.wrap_upstream(Silent(), what="w").message == "Silent"
    assert portal.wrap_upstream(Silent("it said"), what="w").message == "Silent: it said"


def test_a_planted_lock_link_is_refused_rather_than_truncated(session_dir, tmp_path):
    """Opening the lock with "w" truncated whatever a link at its name pointed to."""
    target = tmp_path / "victim.txt"
    target.write_text("keep")
    (session_dir.path / portal.LOCK_NAME).symlink_to(target)
    with pytest.raises(OSError):
        portal.renew_under_lock(FakeAuth(expires_in=timedelta(seconds=-1)))
    assert target.read_text() == "keep"

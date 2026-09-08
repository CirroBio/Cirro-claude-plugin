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

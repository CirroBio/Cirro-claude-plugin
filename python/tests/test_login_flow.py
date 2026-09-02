"""How the login link reaches the person, and how a live session avoids one.

Nothing here is about whether a credential is valid; it is about the number of round
trips and tool calls standing between "log me in" and a URL on screen, and about not
starting a login when a refresh would have done.
"""
import json
import pathlib
import time

import pytest

from cirro_agent import errors
from cirro_agent.worker import auth

BASE_URL = "cirro.test"
SKILL = pathlib.Path(__file__).resolve().parents[2] / "skills/cirro-session/SKILL.md"

FLOW = {
    "auth_message": "To sign in, open the page https://cirro.test/authorize",
    "auth_message_markdown": "[Log in](https://cirro.test/authorize) code WXYZ-1234",
    "verification_url": "https://cirro.test/authorize?user_code=WXYZ-1234",
    "base_url": BASE_URL,
}


class _Sink:
    def write(self, _payload):
        pass

    def close(self):
        pass


class FakeChild:
    def __init__(self, *_args, **_kwargs):
        self.pid = 4242
        self.stdin = _Sink()


def _no_spawn(*_args, **_kwargs):
    raise AssertionError("a login was started when the credential already worked")


@pytest.fixture
def home(tmp_path, monkeypatch):
    session = tmp_path / ".cirro"
    session.mkdir()
    monkeypatch.setenv("CIRRO_HOME", str(session))
    monkeypatch.delenv("CIRRO_CLIENT_ID", raising=False)
    monkeypatch.delenv("CIRRO_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("CIRRO_ACCESS_TOKEN", raising=False)
    # The terminal is a courtesy channel and tests must not write to a real one.
    monkeypatch.setattr(auth, "_announce", lambda _u, _m: False)
    return session


@pytest.fixture
def logged_in(home, monkeypatch):
    """A folder holding a credential that verifies."""
    (home / "abc123.token.dat").write_text("")
    monkeypatch.setattr(auth, "_verify",
                        lambda _b: {"username": "me@example.org", "n_projects": 3})
    monkeypatch.setattr(auth.subprocess, "Popen", _no_spawn)
    return home


def _publish_flow(home, monkeypatch, **overrides):
    """Stand in for the detached daemon writing its device code out."""
    def spawn(*_args, **_kwargs):
        (home / auth.FLOW_FILE).write_text(json.dumps({**FLOW, **overrides}))
        return FakeChild()
    monkeypatch.setattr(auth.subprocess, "Popen", spawn)


# --- reaching the link ------------------------------------------------------

def test_a_first_login_does_not_pay_for_a_credential_check(home, monkeypatch):
    """Naming the token file needs the tenant's client_id, which costs a round trip.

    With no token file there is nothing that check could find, so it must not run.
    """
    def forbidden(_base_url):
        raise AssertionError("_verify made a network call it could not use")
    monkeypatch.setattr(auth, "_verify", forbidden)
    _publish_flow(home, monkeypatch)

    result = auth.login_start({"base_url": BASE_URL})

    assert result["status"] == "pending"
    assert result["reason"] == "no_credential"
    assert result["verification_url"] == FLOW["verification_url"]


def test_the_link_carries_the_code_so_nothing_has_to_be_typed():
    flow = {"verification_uri": "https://cirro.test/authorize",
            "user_code": "ZO45AYS2",
            "message": "To sign in, open https://cirro.test/authorize"}
    assert auth._flow_url(_FlowHolder(flow)) == \
        "https://cirro.test/authorize?user_code=ZO45AYS2"


def test_the_link_is_recovered_from_the_prose_if_the_flow_shape_changes():
    holder = _FlowHolder({"message": "open https://cirro.test/x?user_code=AB12 now"})
    assert auth._flow_url(holder) == "https://cirro.test/x?user_code=AB12"


def test_no_link_rather_than_a_wrong_one():
    assert auth._flow_url(_FlowHolder({"message": "something went sideways"})) is None
    assert auth._flow_url(_FlowHolder({"message": None})) is None


def test_no_browser_is_ever_launched():
    """Printing the link is the whole delivery; opening a page is not ours to do."""
    source = pathlib.Path(auth.__file__).read_text()
    for forbidden in ("webbrowser", "xdg-open", "open location"):
        assert forbidden not in source, f"{forbidden} launches something for the user"


def test_the_terminal_channel_cannot_be_used_to_forge_output():
    """Both strings come verbatim from the tenant; a terminal would act on escapes."""
    hostile = "https://evil.test/x\x1b[2K\rCirro login: https://real.test/y"
    assert auth._printable(hostile) == \
        "https://evil.test/x[2KCirro login: https://real.test/y"
    assert auth._printable("\x07\x00bell\x9b") == "bell"
    assert auth._printable(None) == ""


def test_the_link_is_printed_once_even_if_the_login_is_re_entered(home, monkeypatch):
    printed = []
    monkeypatch.setattr(auth, "_announce", lambda u, _m: printed.append(u) or True)
    (home / auth.FLOW_FILE).write_text(json.dumps(FLOW))

    first = auth._pending(home, json.loads((home / auth.FLOW_FILE).read_text()))
    second = auth._pending(home, json.loads((home / auth.FLOW_FILE).read_text()))

    assert printed == [FLOW["verification_url"]], "re-entry printed the code again"
    assert first["printed_to_terminal"] and second["printed_to_terminal"]


def test_the_agent_is_told_to_relay_whatever_the_terminal_did(home):
    result = auth._pending(home, FLOW)
    assert result["printed_to_terminal"] is False
    assert "Show verification_url to the user now" in result["relay_note"]


# --- already logged in ------------------------------------------------------

def test_a_macos_keychain_login_is_recognised(home):
    """The secret lives in the Keychain, but `save` still touches the signal file."""
    (home / "5aflubqekmde3i599hn9e6sb2u.token.dat").write_text("")
    assert auth._has_credential(home)


def test_a_headless_session_is_never_sent_to_a_device_code_login(home, monkeypatch):
    """Client credentials live in the environment; there is no file to find."""
    monkeypatch.setenv("CIRRO_CLIENT_ID", "svc-app")
    monkeypatch.setenv("CIRRO_CLIENT_SECRET", "shhh")
    assert not list(home.glob("*.token.dat"))
    assert auth._has_credential(home)

    monkeypatch.setattr(auth, "_verify", lambda _b: {"username": "svc-app"})
    monkeypatch.setattr(auth.subprocess, "Popen", _no_spawn)
    assert auth.login_start({"base_url": BASE_URL})["status"] == "already_authenticated"


def test_a_static_token_session_is_never_sent_to_a_device_code_login(home, monkeypatch):
    monkeypatch.setenv("CIRRO_ACCESS_TOKEN", "ey.not.a.real.token")
    assert auth._has_credential(home)


def test_being_logged_in_costs_one_call_and_prints_nothing(logged_in, monkeypatch):
    monkeypatch.setattr(auth, "_announce",
                        lambda _u, _m: pytest.fail("wrote to the terminal needlessly"))

    result = auth.login_start({"base_url": BASE_URL})

    assert result["status"] == "already_authenticated"
    assert result["username"] == "me@example.org"
    assert "verification_url" not in result and "relay_note" not in result


def test_an_expired_access_token_is_refreshed_rather_than_replaced(home, monkeypatch):
    """The SDK refreshes on the first real call, which is what _verify makes.

    Starting a fresh device-code login here would make the user re-authorize in a
    browser for a credential that could have renewed itself silently.
    """
    (home / "abc123.token.dat").write_text("")
    calls = []
    monkeypatch.setattr(auth, "_verify",
                        lambda b: calls.append(b) or {"username": "me@example.org"})
    monkeypatch.setattr(auth.subprocess, "Popen", _no_spawn)

    assert auth.login_start({"base_url": BASE_URL})["status"] == "already_authenticated"
    assert calls == [BASE_URL], "the refresh happens inside the one verifying call"


def test_a_refresh_that_is_refused_falls_through_to_a_new_login(home, monkeypatch):
    """A revoked refresh token surfaces from the live call, not the pre-flight.

    Catching only the pre-flight errors left this case unable to recover: no login
    was started, and the skill's recovery table sends the agent straight back here.
    """
    (home / "abc123.token.dat").write_text("")

    def revoked(_base_url):
        raise RuntimeError("Failed to refresh token, please reauthenticate")
    monkeypatch.setattr(auth, "_verify", revoked)
    monkeypatch.setattr(auth.portal, "wrap_upstream",
                        lambda exc, **_k: errors.AuthExpired(
                            str(exc), what="w", user_action="log in"))
    _publish_flow(home, monkeypatch)

    result = auth.login_start({"base_url": BASE_URL})

    assert result["status"] == "pending"
    assert result["reason"] == "AUTH_EXPIRED"


def test_a_network_failure_does_not_start_a_login(home, monkeypatch):
    (home / "abc123.token.dat").write_text("")

    def offline(_base_url):
        raise errors.NetworkUnavailable("no route", what="w", user_action="check vpn")
    monkeypatch.setattr(auth, "_verify", offline)
    monkeypatch.setattr(auth.subprocess, "Popen", _no_spawn)

    with pytest.raises(errors.AgentError) as caught:
        auth.login_start({"base_url": BASE_URL})
    assert caught.value.code == "NETWORK_UNAVAILABLE"


def test_a_daemon_that_finds_a_token_does_not_hang_the_caller(home, monkeypatch):
    """The daemon writes ok and never publishes a flow; without an arm for that the
    caller waited out the whole timeout and reported a network failure."""
    def spawn(*_args, **_kwargs):
        (home / auth.STATUS_FILE).write_text(json.dumps({"status": "ok"}))
        return FakeChild()
    monkeypatch.setattr(auth.subprocess, "Popen", spawn)
    monkeypatch.setattr(auth, "_verify", lambda _b: {"username": "me@example.org"})

    started = time.monotonic()
    result = auth.login_start({"base_url": BASE_URL})

    assert result["status"] == "already_authenticated"
    assert time.monotonic() - started < auth.FLOW_PUBLISH_TIMEOUT


# --- waiting ----------------------------------------------------------------

def test_waiting_returns_the_moment_the_browser_login_lands(home, monkeypatch):
    """The timeout is a ceiling, not a delay."""
    (home / auth.STATUS_FILE).write_text(json.dumps({"status": "ok"}))
    monkeypatch.setattr(auth, "_verify", lambda _b: {"username": "me@example.org"})
    slept = []
    monkeypatch.setattr(auth.time, "sleep", slept.append)

    result = auth.login_wait({"base_url": BASE_URL, "timeout_s": 120})

    assert result["status"] == "ok"
    assert not slept, "returned without waiting out any of the timeout"


@pytest.mark.parametrize("recorded,expected", [
    ({"status": "ok"}, "ok"),
    (None, "pending"),
])
def test_a_zero_timeout_reports_the_state_now(home, monkeypatch, recorded, expected):
    """An explicit 0 means "tell me where this stands", not "wait the default"."""
    if recorded:
        (home / auth.STATUS_FILE).write_text(json.dumps(recorded))
    (home / auth.FLOW_FILE).write_text(json.dumps(FLOW))
    (home / auth.PID_FILE).write_text(json.dumps({"pid": 1}))
    monkeypatch.setattr(auth, "_child_alive", lambda: True)
    monkeypatch.setattr(auth, "_verify", lambda _b: {"username": "me@example.org"})

    started = time.monotonic()
    result = auth.login_wait({"base_url": BASE_URL, "timeout_s": 0})

    assert result["status"] == expected
    assert time.monotonic() - started < 1.0


def test_a_pending_wait_hands_the_link_back(home, monkeypatch):
    """The person may need to see the URL again; a pending result must carry it."""
    (home / auth.FLOW_FILE).write_text(json.dumps(FLOW))
    monkeypatch.setattr(auth, "_child_alive", lambda: True)

    result = auth.login_wait({"base_url": BASE_URL, "timeout_s": 0})

    assert result["status"] == "pending"
    assert result["verification_url"] == FLOW["verification_url"]


def test_waiting_on_a_live_session_just_confirms_it(home, monkeypatch):
    """Calling the pair in order must not punish a session that needed neither."""
    monkeypatch.setattr(auth, "_verify", lambda _b: {"username": "me@example.org"})

    result = auth.login_wait({"base_url": BASE_URL, "timeout_s": 120})

    assert result["status"] == "ok"
    assert result["was_already_logged_in"]


def test_a_completed_login_is_not_reported_as_having_needed_none(home, monkeypatch):
    """`was_already_logged_in` must be present on both success paths, not just one."""
    (home / auth.STATUS_FILE).write_text(json.dumps({"status": "ok"}))
    monkeypatch.setattr(auth, "_verify", lambda _b: {"username": "me@example.org"})

    assert auth.login_wait({"base_url": BASE_URL})["was_already_logged_in"] is False


def test_waiting_with_no_login_and_no_credential_says_exactly_that(home, monkeypatch):
    def unauthenticated(_base_url):
        raise errors.AuthRequired("nope", what="w", user_action="log in")
    monkeypatch.setattr(auth, "_verify", unauthenticated)

    with pytest.raises(errors.AgentError) as caught:
        auth.login_wait({"base_url": BASE_URL, "timeout_s": 120})

    assert "No login is in progress" in caught.value.message
    assert caught.value.retry_with[0]["tool"] == "login_start"


def test_a_helper_that_died_before_publishing_is_not_an_idle_session(home, monkeypatch):
    """A crashed daemon leaves the same empty folder as one that never ran."""
    (home / auth.PID_FILE).write_text(json.dumps({"pid": 999999}))
    monkeypatch.setattr(auth, "_child_alive", lambda: False)
    monkeypatch.setattr(auth, "_verify", lambda _b: pytest.fail("should not verify"))

    with pytest.raises(errors.AgentError) as caught:
        auth.login_wait({"base_url": BASE_URL, "timeout_s": 0})

    assert "exited without recording a result" in caught.value.message


def test_a_helper_that_died_mid_login_is_still_reported_as_such(home, monkeypatch):
    (home / auth.FLOW_FILE).write_text(json.dumps(FLOW))
    monkeypatch.setattr(auth, "_child_alive", lambda: False)
    monkeypatch.setattr(auth, "_verify", lambda _b: pytest.fail("should not verify"))

    with pytest.raises(errors.AgentError) as caught:
        auth.login_wait({"base_url": BASE_URL, "timeout_s": 120})

    assert "exited without recording a result" in caught.value.message


# --- where delivery happens --------------------------------------------------

def test_publishing_the_link_precedes_every_other_delivery_step():
    """A slow or hung delivery must not be able to withhold the link.

    The daemon's only job once the code is issued is to write it where login_start
    can see it; delivery happens in the caller, which the runner bounds.
    """
    import inspect
    body = inspect.getsource(auth.login_daemon)
    assert "FLOW_FILE" in body and body.index("FLOW_FILE") < body.index(
        "await_completion"), "the link must be published before the daemon settles in"
    for delivery in ("_announce", "_pending", "_printable"):
        assert delivery not in body, f"{delivery} in the daemon delays the link"


def test_the_skill_reaches_login_start_before_anything_else():
    """Each tool call ahead of the link is an approval prompt the person must clear."""
    steps = SKILL.read_text().split("## Order of operations")[1].split("### 4.")[0]
    first = steps.index("login_start")
    for later in ("session_init", "doctor"):
        assert later not in steps[:first], f"{later} runs before the link is produced"


class _FlowHolder:
    """Stands in for the SDK's DeviceCodeAuth, which carries the flow privately."""

    def __init__(self, flow):
        self._flow = flow


def test_a_live_session_clears_the_remains_of_the_login_that_made_it(logged_in):
    """A stale failure record would outlive the credential and fail a later wait."""
    (logged_in / auth.STATUS_FILE).write_text(json.dumps({"status": "failed",
                                                          "error": "timed out"}))
    (logged_in / auth.FLOW_FILE).write_text(json.dumps(FLOW))

    assert auth.login_start({"base_url": BASE_URL})["status"] == "already_authenticated"

    assert not (logged_in / auth.STATUS_FILE).exists()
    assert not (logged_in / auth.FLOW_FILE).exists()

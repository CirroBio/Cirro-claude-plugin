"""The subprocess boundary, for real: a child worker with CIRRO_HOME in its environment.

Everything else in the suite calls dispatch.run in-process. These spawn the actual
worker the way runner does, using the one read that needs no network, so the
import-order guard, the CIRRO_HOME assertion and the JSON round trip are exercised.
"""
import json
import os
from pathlib import Path

import pytest

from cirro_agent import errors, runner, session as session_mod


@pytest.fixture
def bound(tmp_path, monkeypatch):
    monkeypatch.setenv("CIRRO_AGENT_PLUGIN_ROOT", str(Path(__file__).resolve().parents[2]))
    monkeypatch.delenv("CIRRO_AGENT_PYTHON", raising=False)
    return session_mod.init(tmp_path, base_url="app.cirro.test")


def test_a_read_round_trips_through_a_real_child_process(bound, tmp_path):
    out = runner.call({"verb": "describe", "entity": "dataset"}, start=tmp_path)
    assert out["ok"], out
    assert out["query"]["verb"] == "describe"
    assert out["cost"]["api_calls"] == 0


def test_the_child_sees_the_session_folder_as_cirro_home(bound, tmp_path):
    """The whole design rests on CIRRO_HOME being set before the child's Python
    starts; the worker asserts it took effect, so a wrong environment fails loudly."""
    env = runner.child_environment(bound)
    assert Path(env["CIRRO_HOME"]).resolve() == bound.path.resolve()
    assert env["PYTHONPATH"].split(os.pathsep)[0].endswith(str(Path("python") / "src"))


def test_a_child_that_prints_no_json_is_a_typed_upstream_error(
        bound, tmp_path, monkeypatch):
    """A crashed worker must come back as an error the agent can act on, not a
    JSON decode traceback in the parent."""
    monkeypatch.setattr(runner, "interpreter", lambda: Path("/usr/bin/false"))
    monkeypatch.setattr(runner, "check_ready", lambda: {"mode": "dev"})
    out = runner.call({"verb": "describe", "entity": "dataset"}, start=tmp_path)
    assert out["ok"] is False
    assert out["error"]["code"] == "UPSTREAM_ERROR"
    assert out["error"]["remediation"]["retry_with"]


def test_a_missing_runtime_is_reported_not_installed(bound, tmp_path, monkeypatch):
    def gone():
        raise errors.RuntimeNotInstalled("no venv", what="w", user_action="run setup")
    monkeypatch.setattr(runner, "check_ready", gone)
    out = runner.call({"verb": "describe", "entity": "dataset"}, start=tmp_path)
    assert out["error"]["code"] == "RUNTIME_NOT_INSTALLED"
    assert "setup" in out["error"]["remediation"]["user_action"]


def test_a_missing_session_folder_never_falls_back_to_the_home_directory(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CIRRO_AGENT_PLUGIN_ROOT", str(Path(__file__).resolve().parents[2]))
    empty = tmp_path / "nowhere"
    empty.mkdir()
    out = runner.call({"verb": "describe", "entity": "dataset"}, start=empty)
    assert out["ok"] is False
    assert out["error"]["code"] in ("NO_SESSION_FOLDER", "no_session_folder")
    assert json.dumps(out)  # serialisable, like every response

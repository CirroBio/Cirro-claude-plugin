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


def test_the_runtime_never_defaults_inside_the_plugin_root(tmp_path, monkeypatch):
    """A directory marketplace copies the plugin root verbatim into every install,
    honouring no .gitignore and taking no exclude list. Three virtualenvs left in
    the tree once turned a 1.2 MB plugin into a 536 MB one."""
    monkeypatch.setenv("CIRRO_AGENT_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.delenv("CIRRO_AGENT_PLUGIN_DATA", raising=False)
    data = runner.plugin_data()
    assert tmp_path != data and tmp_path not in data.parents, \
        f"{data} is inside the plugin root and would ship with the plugin"
    assert tmp_path not in (runner.plugin_data() / runner.DEV_VENV).parents


# --- the two resolvers, in the environments they are really called in --------
#
# scripts/setup builds where the shell resolver says; the worker and the MCP server
# launch from where the runner says. Asking both in one environment cannot detect the
# drift that matters, because the drift is in the environments themselves.

HOST_PLUGIN_DATA = "/tmp/cirro-host-managed-plugin-data"


def _shell_plugin_data(env: dict) -> Path:
    import subprocess
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        ["bash", "-c", f'source "{root}/scripts/_resolve-python.sh"; resolve_plugin_data'],
        capture_output=True, text=True, env=env, check=True).stdout.strip()
    return Path(out)


def _env_without_data_vars() -> dict:
    return {k: v for k, v in os.environ.items()
            if k not in ("CIRRO_AGENT_PLUGIN_DATA", "CLAUDE_PLUGIN_DATA")}


def test_a_host_managed_data_dir_cannot_split_the_two_resolvers(monkeypatch):
    """CLAUDE_PLUGIN_DATA reaches the MCP server and nothing else.

    While it was honoured, `/cirro:setup` -- which runs over Bash, where the host
    does not set it -- built the runtime under the XDG default, and the server looked
    for it under the host's path, found nothing, fell back to an interpreter that
    cannot import `mcp`, and died. A fresh install that had just run setup correctly
    reported only "MCP server failed to connect".
    """
    monkeypatch.delenv("CIRRO_AGENT_PLUGIN_DATA", raising=False)
    over_bash = _shell_plugin_data(_env_without_data_vars())

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", HOST_PLUGIN_DATA)
    in_the_mcp_server = runner.plugin_data()

    assert over_bash == in_the_mcp_server, \
        "setup installs where the shell says and the server launches from where the " \
        "runner says; a value only one of them can see puts them in different places"
    assert HOST_PLUGIN_DATA not in str(in_the_mcp_server)
    assert _shell_plugin_data(
        {**_env_without_data_vars(), "CLAUDE_PLUGIN_DATA": HOST_PLUGIN_DATA}) == over_bash


def test_the_explicit_override_still_redirects_both_resolvers(tmp_path, monkeypatch):
    """CIRRO_AGENT_PLUGIN_DATA is the override that replaced it: nothing sets it
    behind your back, so it is set for every context at once or for none."""
    monkeypatch.setenv("CIRRO_AGENT_PLUGIN_DATA", str(tmp_path))
    env = {**_env_without_data_vars(), "CIRRO_AGENT_PLUGIN_DATA": str(tmp_path)}
    assert _shell_plugin_data(env) == runner.plugin_data() == tmp_path


# --- a transfer is bounded by bandwidth, not by the API ----------------------

def test_only_a_transfer_gets_the_bandwidth_budget(bound):
    """900s is the budget for a request. A 3.4 GB upload was killed at 900s having
    moved no files, leaving an orphaned PENDING dataset to delete by hand."""
    assert runner.TRANSFER_TIMEOUT > runner.WORKER_TIMEOUT
    assert runner._transfer_op({"verb": "plan", "op": "upload_dataset"}, bound) == \
        "upload_dataset"
    assert runner._transfer_op({"verb": "plan", "op": "update_dataset"}, bound) is None
    assert runner._transfer_op({"verb": "query", "entity": "dataset"}, bound) is None


def test_an_applys_budget_comes_from_the_plan_it_names(bound):
    from cirro_agent import plans
    plan = plans.create(bound, op="upload_dataset", args={}, summary="s",
                        request_args={}, risk="cirro_write", username="sminot")
    assert runner._transfer_op({"verb": "apply", "plan_id": plan.plan_id}, bound) == \
        "upload_dataset"
    # A plan that cannot be read still reaches the worker, which is what refuses it.
    assert runner._transfer_op({"verb": "apply", "plan_id": "pl_nope"}, bound) is None


def test_the_timeout_advice_never_points_at_a_blocked_command():
    """`scripts/gate.py` blocks the upstream client, so advising it sent the reader
    at a wall."""
    for op in (None, "upload_dataset", "download_files"):
        advice = runner._timeout_advice(op)
        assert "web UI" in advice
        assert "CLI" not in advice
    assert "PENDING" in runner._timeout_advice("upload_dataset")

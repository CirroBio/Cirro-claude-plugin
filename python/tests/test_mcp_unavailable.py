"""The stand-in server that answers when the pinned runtime is missing.

`scripts/mcp-server` used to fall back to the ambient interpreter and run
`mcp_server` under it. That module imports the third-party `mcp` package at module
scope, so it died at import and the host reported a bare "failed to connect" -- the
exact silent failure the fallback was written to prevent, and the first thing every
new user saw before running /cirro:setup.
"""
import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from cirro_agent import mcp_unavailable

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def no_runtime(tmp_path, monkeypatch):
    """A data directory with no virtualenv in it, and no interpreter override."""
    monkeypatch.setenv("CIRRO_AGENT_PLUGIN_ROOT", str(ROOT))
    monkeypatch.setenv("CIRRO_AGENT_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CIRRO_AGENT_PYTHON", raising=False)
    return tmp_path


def test_the_tool_list_matches_the_server_it_stands_in_for():
    """Named in two places because importing the real one is what this module exists
    to avoid. A tool that appears in only one of them is a tool the user is told
    about and cannot call, or can call and is never told about."""
    from cirro_agent import mcp_server
    registered = [t.name for t in asyncio.run(mcp_server.server.list_tools())]
    assert list(mcp_unavailable.TOOLS) == registered


def test_it_reaches_no_third_party_package():
    """The whole point: it runs under whatever python3 is on PATH. A dependency here
    is a dependency the ambient interpreter is not required to have."""
    proc = subprocess.run(
        ["python3", "-c",
         "import sys, cirro_agent.mcp_unavailable as m; "
         "assert 'mcp' not in sys.modules; print(len(m.TOOLS))"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "python" / "src")})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "11"


def test_a_missing_runtime_answers_a_call_instead_of_killing_the_connection(no_runtime):
    handshake = mcp_unavailable.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}})
    assert handshake["result"]["protocolVersion"] == "2025-06-18"
    assert handshake["result"]["serverInfo"]["name"] == "cirro"
    assert "/cirro:setup" in handshake["result"]["instructions"]

    listed = mcp_unavailable.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == list(mcp_unavailable.TOOLS)

    called = mcp_unavailable.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "login_start", "arguments": {}}})
    assert called["result"]["isError"] is True
    payload = json.loads(called["result"]["content"][0]["text"])
    assert payload["error"]["code"] == "RUNTIME_NOT_INSTALLED"
    assert payload["error"]["remediation"]["user_action"]


def test_the_error_names_the_directories_it_searched(no_runtime):
    """The one fact that separates "never installed" from "installed elsewhere" --
    and the fact whose absence turned a path mismatch into an investigation."""
    payload = mcp_unavailable.unavailable()
    looked_in = payload["error"]["detail"]["looked_in"]
    assert [Path(p).parent for p in looked_in] == [no_runtime, no_runtime]


def test_a_runtime_that_arrives_later_is_reported_as_needing_a_reconnect(no_runtime):
    """Someone runs /cirro:setup while this process is up. It still cannot serve a
    tool, but "install it" is no longer the true answer."""
    (no_runtime / "venv" / "bin").mkdir(parents=True)
    (no_runtime / "venv" / "bin" / "python").touch()
    payload = mcp_unavailable.unavailable()
    assert "reconnect" in payload["error"]["remediation"]["user_action"].lower()


def test_a_notification_is_never_answered(no_runtime):
    assert mcp_unavailable.handle(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_an_unsupported_method_is_an_error_not_a_crash(no_runtime):
    out = mcp_unavailable.handle({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
    assert out["error"]["code"] == -32601


def test_the_launcher_serves_the_stand_in_when_the_runtime_is_missing(no_runtime):
    """End to end, through the script the host actually spawns. This is the failure
    that shipped: the launcher exec'd an interpreter that could not import `mcp`, so
    the process died mid-handshake and the user was told only that it failed."""
    request = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}}})
    proc = subprocess.run(
        [str(ROOT / "scripts" / "mcp-server")], input=request + "\n",
        capture_output=True, text=True, timeout=60,
        env={k: v for k, v in os.environ.items() if k != "CIRRO_AGENT_PYTHON"})
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.splitlines()[0])["result"]
    assert result["serverInfo"]["name"] == "cirro"
    assert "/cirro:setup" in result["instructions"]

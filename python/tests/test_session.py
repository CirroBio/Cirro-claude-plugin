"""Session folder discovery: every refusal exists to avoid the global credential."""
import json
from pathlib import Path

import pytest

from cirro_agent import errors, session as session_mod


def test_init_creates_the_layout_and_self_ignores(tmp_path):
    current = session_mod.init(tmp_path, "https://app.cirro.bio/some/path")
    assert current.base_url == "app.cirro.bio", "scheme and path must be stripped"
    for sub in ("plans", "runs", "cache/snapshots", "downloads"):
        assert (current.path / sub).is_dir()
    assert (current.path / ".gitignore").read_text().strip() == "*", \
        "the folder ignores itself so no tracked file needs editing"


def test_init_refuses_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(errors.AgentError) as caught:
        session_mod.init(tmp_path, "app.cirro.bio")
    assert caught.value.code == "NO_SESSION_FOLDER"


def test_discovery_walks_up_from_a_subdirectory(tmp_path, monkeypatch):
    session_mod.init(tmp_path, "app.cirro.bio")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert session_mod.find_session_dir(deep) == tmp_path / ".cirro"


def test_discovery_stops_at_the_project_ceiling(tmp_path, monkeypatch):
    session_mod.init(tmp_path, "app.cirro.bio")
    inner = tmp_path / "inner"
    inner.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(inner))
    with pytest.raises(errors.AgentError) as caught:
        session_mod.find_session_dir(inner)
    assert caught.value.code == "NO_SESSION_FOLDER"


def test_missing_session_is_a_typed_error_with_a_human_action(tmp_path, monkeypatch):
    with pytest.raises(errors.AgentError) as caught:
        session_mod.find_session_dir(tmp_path)
    assert caught.value.code == "NO_SESSION_FOLDER"
    assert caught.value.user_action


def test_ambient_base_url_conflict_is_refused(tmp_path, monkeypatch):
    session_mod.init(tmp_path, "app.cirro.bio")
    monkeypatch.setenv("CIRRO_BASE_URL", "other.cirro.bio")
    with pytest.raises(errors.AgentError) as caught:
        session_mod.load(tmp_path)
    assert caught.value.code == "TENANT_MISMATCH"


def test_matching_ambient_base_url_is_accepted(tmp_path, monkeypatch):
    session_mod.init(tmp_path, "app.cirro.bio")
    monkeypatch.setenv("CIRRO_BASE_URL", "https://app.cirro.bio")
    assert session_mod.load(tmp_path).base_url == "app.cirro.bio"


def test_child_env_sets_cirro_home_to_the_resolved_session(tmp_path):
    current = session_mod.init(tmp_path, "app.cirro.bio")
    env = session_mod.child_env(current)
    assert Path(env["CIRRO_HOME"]) == current.path.resolve()
    assert env["CIRRO_BASE_URL"] == "app.cirro.bio"


def test_session_file_round_trips(tmp_path):
    current = session_mod.init(tmp_path, "app.cirro.bio")
    stored = json.loads((current.path / "session.json").read_text())
    assert stored["base_url"] == "app.cirro.bio"
    assert "dir" not in stored, "the folder's own path is not stored inside it"


def test_a_second_folder_is_reachable_without_moving_the_session(tmp_path):
    """One Claude session, two bound folders.

    Every write tool used to resolve the session from the process's working
    directory with no way to redirect it, so working across two tenants meant
    moving the session. `start` is that redirect, and it is threaded through the
    MCP tools and `cirro-agent read --dir`.
    """
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()
    session_mod.init(here, "app.cirro.bio")
    session_mod.init(there, "dev.cirro.bio")
    assert session_mod.load(here).base_url == "app.cirro.bio"
    assert session_mod.load(there).base_url == "dev.cirro.bio"
    assert session_mod.load(there).path == (there / ".cirro")


def test_a_directory_that_does_not_exist_is_refused_by_name(tmp_path):
    with pytest.raises(errors.AgentError) as caught:
        session_mod.find_session_dir(tmp_path / "nowhere")
    assert caught.value.code == "NO_SESSION_FOLDER"
    assert "not a directory" in caught.value.message
    assert caught.value.user_action


def test_every_session_resolving_tool_takes_a_directory():
    """A new tool must not quietly reintroduce the working-directory-only limit."""
    import inspect
    from cirro_agent import mcp_server
    for name in ("session_init", "login_start", "login_wait", "whoami", "logout",
                 "plan", "apply"):
        tool = getattr(mcp_server, name)
        assert "directory" in inspect.signature(tool).parameters, name


def test_the_read_cli_resolves_the_directory_it_is_given(tmp_path, monkeypatch):
    from cirro_agent import cli, runner
    session_mod.init(tmp_path, "dev.cirro.bio")
    seen = {}

    def record(request, **kwargs):
        seen.update(request=request, start=kwargs.get("start"))
        return {"ok": True}

    monkeypatch.setattr(runner, "call", record)
    cli.main(["read", "folders", "-p", "X", "--dir", str(tmp_path)])
    assert seen["start"] == tmp_path
    assert session_mod.load(seen["start"]).base_url == "dev.cirro.bio"

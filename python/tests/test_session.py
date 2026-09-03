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

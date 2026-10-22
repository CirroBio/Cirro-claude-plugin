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


def test_init_tenant_creates_the_folder_outside_every_checkout(tmp_path):
    current = session_mod.init_tenant("https://app.cirro.bio/api")
    root = Path(__file__).resolve().parents[2]
    assert current.path == session_mod.tenants_root() / "app.cirro.bio" / ".cirro"
    assert (current.path / session_mod.SESSION_FILE).is_file()
    assert root not in current.path.parents, (
        "a tenant folder inside the plugin root is copied into every install by a "
        "directory marketplace, credential and all")


def test_a_directory_with_nothing_bound_falls_back_to_the_one_tenant(tmp_path):
    session_mod.init_tenant("app.cirro.bio")
    work = tmp_path / "unbound"
    work.mkdir()
    assert session_mod.find_session_dir(work) == session_mod.tenant_dir(
        "app.cirro.bio") / ".cirro"


def test_a_folder_bound_nearer_still_wins(tmp_path):
    """The fallback is a fallback. A directory that was signed in to on purpose keeps
    answering for itself."""
    session_mod.init_tenant("app.cirro.bio")
    session_mod.init(tmp_path, "other.cirro.bio")
    assert session_mod.find_session_dir(tmp_path) == tmp_path / ".cirro"


def test_two_signed_in_tenants_are_never_chosen_between(tmp_path):
    session_mod.init_tenant("app.cirro.bio")
    session_mod.init_tenant("dev.cirro.bio")
    work = tmp_path / "unbound"
    work.mkdir()
    with pytest.raises(errors.AgentError) as caught:
        session_mod.find_session_dir(work)
    assert caught.value.code == "NO_SESSION_FOLDER"
    assert [Path(c).name for c in caught.value.candidates] == [
        "app.cirro.bio", "dev.cirro.bio"], "both tenants must be offered by name"


def test_no_tenant_and_no_folder_still_says_how_to_make_one(tmp_path):
    work = tmp_path / "unbound"
    work.mkdir()
    with pytest.raises(errors.AgentError) as caught:
        session_mod.find_session_dir(work)
    assert caught.value.code == "NO_SESSION_FOLDER"
    assert not caught.value.candidates
    assert "shared" in caught.value.user_action


def test_a_tenant_folder_is_not_rebound_to_another_tenant(tmp_path):
    """`init_tenant` derives the path from the host, so this can only happen if the
    normalization ever stops distinguishing two hosts."""
    session_mod.init_tenant("app.cirro.bio")
    assert session_mod.tenant_dir("app.cirro.bio") != session_mod.tenant_dir(
        "dev.cirro.bio")


def test_session_init_shared_puts_the_folder_under_the_tenant_root(tmp_path):
    from cirro_agent import mcp_server
    result = mcp_server.session_init("app.cirro.bio", shared=True)
    assert result["ok"]
    assert Path(result["session_dir"]) == session_mod.tenant_dir("app.cirro.bio") / ".cirro"


def test_session_init_refuses_shared_and_a_directory_together(tmp_path):
    """Their locations disagree, and silently honouring one would bind a folder the
    caller did not name."""
    from cirro_agent import mcp_server
    result = mcp_server.session_init("app.cirro.bio", directory=str(tmp_path),
                                     shared=True)
    assert result["error"]["code"] == "NOT_SUPPORTED"
    assert not (tmp_path / ".cirro").exists()


def test_naming_a_second_tenant_signs_into_it_rather_than_refusing(tmp_path, monkeypatch):
    """Falling back to the one signed-in tenant must not make the second one
    unreachable: nothing bound this directory, so another host is not a conflict."""
    from cirro_agent import mcp_server
    session_mod.init_tenant("app.cirro.bio")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server.runner, "call",
                        lambda request, **kwargs: {"ok": True, "verb": request["verb"]})

    assert mcp_server.login_start("dev.cirro.bio")["ok"]
    assert [found.parent.name for found in session_mod.bound_tenants()] == [
        "app.cirro.bio", "dev.cirro.bio"]


def test_naming_another_tenant_in_a_bound_directory_still_refuses(tmp_path, monkeypatch):
    """A directory somebody bound on purpose is not overridden by a base_url."""
    from cirro_agent import mcp_server
    session_mod.init(tmp_path, "app.cirro.bio")
    monkeypatch.chdir(tmp_path)

    result = mcp_server.login_start("dev.cirro.bio")
    assert result["error"]["code"] == "TENANT_MISMATCH"
    assert result["error"]["remediation"]["retry_with"][0]["shared"] is True


def test_login_start_with_nothing_bound_signs_in_for_the_tenant(tmp_path, monkeypatch):
    """The default has to be the good one: this is the single call the session skill
    makes, and binding the working directory is how a credential lands in a checkout."""
    from cirro_agent import mcp_server
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server.runner, "call", lambda request, **kw: {"ok": True})

    assert mcp_server.login_start("app.cirro.bio")["ok"]
    assert not (tmp_path / ".cirro").exists()
    assert (session_mod.tenant_dir("app.cirro.bio") / ".cirro"
            / session_mod.SESSION_FILE).is_file()


def test_login_start_given_a_directory_still_binds_that_directory(tmp_path, monkeypatch):
    """Naming one is how you ask for a folder of its own."""
    from cirro_agent import mcp_server
    monkeypatch.setattr(mcp_server.runner, "call", lambda request, **kw: {"ok": True})

    assert mcp_server.login_start("app.cirro.bio", directory=str(tmp_path))["ok"]
    assert (tmp_path / ".cirro" / session_mod.SESSION_FILE).is_file()
    assert not session_mod.bound_tenants()


# --- a session folder that arrived with the files around it -------------------------

def _git(repo, *arguments):
    import subprocess
    subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)


def test_a_session_folder_tracked_by_git_is_refused(tmp_path):
    """The nearest folder wins, so a committed one chose the host every request went
    to -- ahead of the tenant this person had signed in to."""
    _git(tmp_path, "init", "-q")
    session_mod.init(tmp_path, "app.cirro.bio")
    _git(tmp_path, "add", "-f", ".cirro/session.json")
    with pytest.raises(errors.AgentError) as caught:
        session_mod.find_session_dir(tmp_path)
    assert caught.value.code == "NO_SESSION_FOLDER"
    assert "tracked by git" in caught.value.message
    assert "git rm -r --cached" in caught.value.user_action


def test_a_tenant_that_is_not_a_hostname_is_refused_on_load(tmp_path):
    """`app.cirro.bio@evil.example` reaches evil.example."""
    current = session_mod.init(tmp_path, "app.cirro.bio")
    (current.path / session_mod.SESSION_FILE).write_text(
        json.dumps({"base_url": "app.cirro.bio@evil.example/x"}))
    with pytest.raises(errors.AgentError) as caught:
        session_mod.load(tmp_path)
    assert caught.value.code == "NO_SESSION_FOLDER"


def test_a_trailing_newline_is_not_a_hostname():
    with pytest.raises(errors.AgentError):
        session_mod.tenant_host("app.cirro.bio\n/x")


def test_a_planted_link_is_never_written_through(tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("keep")
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "session.json.tmp").symlink_to(target)
    session_mod.write_private(folder / "session.json", "{}")
    assert target.read_text() == "keep"

    (folder / "other.json").symlink_to(target)
    session_mod.write_private(folder / "other.json", "{}")
    assert target.read_text() == "keep"
    assert not (folder / "other.json").is_symlink()


def test_init_does_not_write_through_a_planted_gitignore(tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("keep")
    (tmp_path / "work" / ".cirro").mkdir(parents=True)
    (tmp_path / "work" / ".cirro" / ".gitignore").symlink_to(target)
    session_mod.init(tmp_path / "work", "app.cirro.bio")
    assert target.read_text() == "keep"


def test_the_global_folder_is_known_under_any_spelling(tmp_path, monkeypatch):
    """On a case-insensitive volume a respelled path is the same folder, and the
    string comparison this replaced let it through."""
    home = tmp_path / "Home"
    (home / ".cirro").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    respelled = Path(str(home).swapcase())
    if not respelled.exists():
        pytest.skip("this volume is case-sensitive")
    assert session_mod.is_global_folder(respelled / ".CIRRO")
    with pytest.raises(errors.AgentError):
        session_mod.init(respelled, "app.cirro.bio")


def test_ambient_credentials_go_only_to_a_host_the_environment_names(tmp_path,
                                                                     monkeypatch):
    """With no host named, the folder's host would get them -- whichever host a
    copied folder names."""
    current = session_mod.init(tmp_path, "app.cirro.bio")
    monkeypatch.setenv("CIRRO_CLIENT_SECRET", "not-a-real-secret")
    monkeypatch.delenv("CIRRO_BASE_URL", raising=False)
    assert "CIRRO_CLIENT_SECRET" not in session_mod.child_env(current)
    monkeypatch.setenv("CIRRO_BASE_URL", "app.cirro.bio")
    assert session_mod.child_env(current)["CIRRO_CLIENT_SECRET"] == "not-a-real-secret"

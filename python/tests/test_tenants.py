"""Listing and removing tenant folders.

Discovery itself lives in `test_session.py`; this covers the two capabilities built on
it -- the read that says what is signed in, and the removal that deletes one. The
removal derives its path from a hostname, so most of what is asserted here is that a
"hostname" which is not one never becomes a path something deletes.
"""
import asyncio
import json

import pytest

from cirro_agent import errors, local_reads, session as session_mod


def test_tenants_lists_what_each_folder_holds(tmp_path):
    current = session_mod.init_tenant("app.cirro.bio")
    (current.path / "plans" / "plan_abc.json").write_text("{}")
    (current.path / "runs" / "2026-01-01-update_dataset-plan_abc.jsonl").write_text("{}")

    result = local_reads.do_tenants({})["result"]
    assert result["n_tenants"] == 1
    only = result["tenants"][0]
    assert only["host"] == "app.cirro.bio"
    assert (only["plans"], only["runs"]) == (1, 1)
    assert only["credential"] == "none"
    assert only["resolves_by_default"] is True
    assert "app.cirro.bio" in result["resolution"]


def test_tenants_reports_a_credential_by_presence_only(tmp_path):
    """The listing says whether a login is there, never anything about it."""
    current = session_mod.init_tenant("app.cirro.bio")
    (current.path / "test-client-id.token.dat").write_text("opaque-material")

    envelope = local_reads.do_tenants({})
    assert envelope["result"]["tenants"][0]["credential"] == "present"
    assert "opaque-material" not in json.dumps(envelope)


def test_tenants_says_when_nothing_resolves_by_default(tmp_path):
    session_mod.init_tenant("app.cirro.bio")
    session_mod.init_tenant("dev.cirro.bio")

    result = local_reads.do_tenants({})["result"]
    assert result["n_tenants"] == 2
    assert not any("resolves_by_default" in entry for entry in result["tenants"])
    assert "--dir" in result["resolution"]


def test_tenants_answers_with_nothing_signed_in(tmp_path):
    """The state a new install is in, and the one where a session folder was just
    removed -- neither is an error."""
    result = local_reads.do_tenants({})["result"]
    assert result["n_tenants"] == 0
    assert result["tenants"] == []
    assert "No tenant is signed in" in result["resolution"]


def test_the_listing_needs_no_session_and_no_credential(tmp_path, monkeypatch):
    """Its whole reason for being local: this is what you read when a login is broken
    or you no longer know what exists."""
    def refuse(*_args, **_kwargs):
        raise AssertionError("the tenant listing resolved a session")

    monkeypatch.setattr(session_mod, "find_session_dir", refuse)
    session_mod.init_tenant("app.cirro.bio")
    assert local_reads.do_tenants({})["result"]["n_tenants"] == 1


def test_remove_tenant_deletes_the_folder_and_reports_what_was_in_it(tmp_path):
    current = session_mod.init_tenant("app.cirro.bio")
    (current.path / "plans" / "plan_abc.json").write_text("{}")

    removed = session_mod.remove_tenant("https://app.cirro.bio")
    assert removed["host"] == "app.cirro.bio"
    assert removed["plans"] == 1, "the figures must outlive the deletion that clears them"
    assert not session_mod.tenant_dir("app.cirro.bio").exists()
    assert not session_mod.bound_tenants()


def test_remove_tenant_refuses_a_host_that_is_not_signed_in(tmp_path):
    session_mod.init_tenant("app.cirro.bio")
    with pytest.raises(errors.AgentError) as caught:
        session_mod.remove_tenant("dev.cirro.bio")
    assert caught.value.code == "NOT_FOUND"
    assert caught.value.candidates == ["app.cirro.bio"]
    assert session_mod.tenant_dir("app.cirro.bio").exists(), "nothing else was touched"


@pytest.mark.parametrize("hostile", ["../..", "..", "", "/", "https://../../..", ".",
                                     "a b", "-leading-dash"])
def test_a_host_that_is_not_a_host_never_becomes_a_path(hostile, tmp_path):
    """`_normalize_base_url` strips the scheme and path but returns `..` for `../..`,
    and as a tenant path that is the directory holding the virtualenv."""
    with pytest.raises(errors.AgentError) as caught:
        session_mod.tenant_dir(hostile)
    assert caught.value.code == "INVALID_QUERY"

    with pytest.raises(errors.AgentError):
        session_mod.remove_tenant(hostile)
    assert session_mod.tenants_root().parent.exists(), "the plugin data root survived"


def test_a_path_after_the_host_is_dropped_rather_than_refused(tmp_path):
    """Normalization keeps only the first segment, so traversal in the path portion is
    gone before the name is validated -- `host/../..` is the host, not a refusal."""
    assert session_mod.tenant_dir("host.cirro.bio/../..").name == "host.cirro.bio"


def test_remove_tenant_refuses_to_follow_a_symlinked_tenant_folder(tmp_path):
    """rmtree through a symlink deletes whatever it points at, so the resolved parent
    has to be the tenants root and not merely look like it."""
    outside = tmp_path / "somewhere-else"
    (outside / ".cirro").mkdir(parents=True)
    (outside / ".cirro" / session_mod.SESSION_FILE).write_text('{"base_url": "x"}')
    session_mod.tenants_root().mkdir(parents=True, exist_ok=True)
    (session_mod.tenants_root() / "app.cirro.bio").symlink_to(outside)

    with pytest.raises(errors.AgentError) as caught:
        session_mod.remove_tenant("app.cirro.bio")
    assert caught.value.code == "NOT_FOUND"
    assert (outside / ".cirro" / session_mod.SESSION_FILE).is_file()


def test_only_apply_session_remove_and_grant_prompt_every_time():
    """`session_remove` deletes a credential and an audit trail, so it carries the one
    enforcement primitive no allow rule or permission mode defeats -- the same one
    `apply` relies on, and the one that makes a `grant` the person's own approval
    rather than the model's. Nothing else may quietly acquire or lose it."""
    from cirro_agent import mcp_server
    tools = asyncio.run(mcp_server.server.list_tools())
    interactive = {tool.name for tool in tools
                   if (tool.meta or {}).get("anthropic/requiresUserInteraction")}
    assert interactive == {"apply", "session_remove", "grant"}


def test_session_remove_reports_what_is_left(tmp_path):
    from cirro_agent import mcp_server
    session_mod.init_tenant("app.cirro.bio")
    session_mod.init_tenant("dev.cirro.bio")

    result = mcp_server.session_remove("app.cirro.bio")
    assert result["ok"]
    assert result["remaining"] == ["dev.cirro.bio"]


def test_session_remove_returns_a_typed_error_rather_than_raising(tmp_path):
    """It is an MCP tool, so a raised AgentError would reach the caller as a transport
    failure instead of a remediable one."""
    from cirro_agent import mcp_server
    result = mcp_server.session_remove("nothing-here.cirro.bio")
    assert result["error"]["code"] == "NOT_FOUND"
    assert result["error"]["remediation"]["user_action"] or \
        result["error"]["remediation"]["retry_with"]


def test_the_read_cli_exposes_tenants_without_the_worker(tmp_path, monkeypatch):
    from cirro_agent import cli, runner
    session_mod.init_tenant("app.cirro.bio")
    monkeypatch.setattr(runner, "call",
                        lambda *_a, **_k: pytest.fail("tenants went to the worker"))
    assert cli.main(["read", "tenants"]) == 0

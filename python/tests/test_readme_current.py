"""The README must name what the plugin actually has.

Documentation drift is normally caught by nobody, so the mechanical half is enforced
here: every skill, command, read subcommand, gated operation and MCP tool has to appear
in the README, and the README may not point at files that do not exist.

This cannot tell whether the prose is still true -- see CLAUDE.md.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text()


def _read_subcommands() -> list:
    from cirro_agent import cli
    parser = cli.build_parser()
    read = parser._subparsers._group_actions[0].choices["read"]
    return sorted(read._subparsers._group_actions[0].choices)


def _skills() -> list:
    return sorted(p.name for p in (ROOT / "skills").iterdir()
                  if p.is_dir() and (p / "SKILL.md").is_file())


def _commands() -> list:
    return sorted(p.stem for p in (ROOT / "commands").glob("*.md"))


def _mcp_tools() -> list:
    import asyncio
    from cirro_agent import mcp_server
    return sorted(t.name for t in asyncio.run(mcp_server.server.list_tools()))


@pytest.mark.parametrize("skill", _skills())
def test_every_skill_is_named(skill):
    assert skill in README, f"skill {skill!r} is not mentioned in the README"


@pytest.mark.parametrize("command", _commands())
def test_every_command_is_named(command):
    slash = "/" + command.replace("cirro-", "cirro:", 1)
    assert command in README or slash in README, \
        f"command {command!r} (as {slash}) is not mentioned in the README"


@pytest.mark.parametrize("subcommand", _read_subcommands())
def test_every_read_subcommand_is_named(subcommand):
    assert f"`{subcommand}`" in README or f"read {subcommand}" in README, \
        f"read subcommand {subcommand!r} is not documented in the README"


def test_every_gated_operation_is_named():
    from cirro_agent.worker import mutations
    missing = [op for op in sorted(mutations.OPS) if f"`{op}`" not in README]
    assert not missing, (
        "these gated operations are not listed in the README's coverage table -- the "
        f"table is the user-facing statement of what the plugin can change: {missing}")


def test_every_mcp_tool_is_named():
    missing = [t for t in _mcp_tools()
               if t not in README and f"mcp__cirro__{t}" not in README]
    assert not missing, f"MCP tools absent from the README: {missing}"


def test_the_readme_points_at_files_that_exist():
    referenced = set(re.findall(r"\]\((?!https?:)([^)#]+)\)", README))
    referenced |= set(re.findall(r"`((?:python|scripts|runtime|bin|skills|commands|"
                                 r"\.claude-plugin)/[A-Za-z0-9_./-]+)`", README))
    missing = sorted(p for p in referenced if not (ROOT / p).exists())
    assert not missing, f"the README references files that do not exist: {missing}"


def test_no_root_mcp_json_and_the_reason_is_recorded():
    """A root .mcp.json is read as a project config and breaks with ENOENT.

    The reason is maintainer-facing, so it lives in CLAUDE.md rather than the README.
    """
    assert not (ROOT / ".mcp.json").exists()
    assert "plugin.json" in (ROOT / "CLAUDE.md").read_text()


def test_claude_md_exists_and_carries_the_firm_rule():
    text = (ROOT / "CLAUDE.md").read_text()
    assert "gated on user approval" in text
    assert "test_readme_current.py" in text, \
        "CLAUDE.md should point at the test that enforces its README rule"


MAINTAINER_ONLY = [
    "CIRRO_HOME", "cirro-api-client", "save_process_revision", "get_all_records",
    "requiresUserInteraction", "subprocess", "stdio",
]


@pytest.mark.parametrize("term", MAINTAINER_ONLY)
def test_the_readme_stays_user_facing(term):
    """Implementation vocabulary belongs in CLAUDE.md.

    The README describes what using the plugin is like; every one of these terms is
    something a user never needs and a maintainer always does.
    """
    assert term not in README, (
        f"{term!r} is implementation detail -- move it to CLAUDE.md and describe the "
        "user-visible consequence in the README instead")

"""The user documentation must name what the plugin actually has.

Documentation drift is normally caught by nobody, so the mechanical half is enforced
here: every skill, command, read subcommand, gated operation and MCP tool has to appear
in the docs site, and neither the docs nor the README may point at files that do not
exist. Links between the docs pages are checked by `mkdocs build --strict` instead.

This cannot tell whether the prose is still true -- see CLAUDE.md.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text()
DOCS = "\n".join(p.read_text() for p in sorted((ROOT / "docs").glob("*.md")))


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
    assert skill in DOCS, f"skill {skill!r} is not mentioned in the docs"


@pytest.mark.parametrize("command", _commands())
def test_every_command_is_named(command):
    slash = "/" + command.replace("cirro-", "cirro:", 1)
    assert command in DOCS or slash in DOCS, \
        f"command {command!r} (as {slash}) is not mentioned in the docs"


@pytest.mark.parametrize("subcommand", _read_subcommands())
def test_every_read_subcommand_is_named(subcommand):
    assert f"`{subcommand}`" in DOCS or f"read {subcommand}" in DOCS, \
        f"read subcommand {subcommand!r} is not documented in the docs"


def test_every_gated_operation_is_named():
    from cirro_agent.worker import mutations
    missing = [op for op in sorted(mutations.OPS) if f"`{op}`" not in DOCS]
    assert not missing, (
        "these gated operations are not listed in the docs' coverage table -- the "
        f"table is the user-facing statement of what the plugin can change: {missing}")


def test_the_plan_tool_lists_every_operation_it_accepts():
    """The MCP server deliberately cannot import the worker, so the `plan` tool's
    "Operations:" list is hand-maintained -- and it is the only place an agent learns
    what `plan` accepts. Nothing else checks it, so a new op family would ship
    invisible to every caller while passing the whole suite.
    """
    import asyncio
    from cirro_agent import mcp_server
    from cirro_agent.worker import mutations
    tools = asyncio.run(mcp_server.server.list_tools())
    described = next(t.description for t in tools if t.name == "plan")
    # Named anywhere in the description is enough -- `chain` earns its own paragraph
    # rather than a slot in the comma list.
    missing = [op for op in sorted(mutations.OPS) if op not in described]
    assert not missing, (
        f"the plan tool's description does not name these operations: {missing}")
    listed = described.split("Operations:", 1)[1].split(".\n", 1)[0]
    stale = [name.strip().rstrip(".") for name in listed.replace("\n", " ").split(",")]
    gone = [name for name in stale if name and name not in mutations.OPS]
    assert not gone, f"the plan tool names operations that no longer exist: {gone}"


def test_every_mcp_tool_is_named():
    missing = [t for t in _mcp_tools()
               if t not in DOCS and f"mcp__plugin_cirro_cirro__{t}" not in DOCS]
    assert not missing, f"MCP tools absent from the docs: {missing}"


def _repo_paths(text: str) -> set:
    return set(re.findall(r"`((?:python|scripts|runtime|bin|skills|commands|docs|"
                          r"\.claude-plugin|\.github)/[A-Za-z0-9_./-]+)`", text))


def test_the_readme_points_at_files_that_exist():
    referenced = set(re.findall(r"\]\((?!https?:)([^)#]+)\)", README)) | _repo_paths(README)
    missing = sorted(p for p in referenced if not (ROOT / p).exists())
    assert not missing, f"the README references files that do not exist: {missing}"


def test_the_docs_point_at_files_that_exist():
    missing = sorted(p for p in _repo_paths(DOCS) if not (ROOT / p).exists())
    assert not missing, f"the docs reference files that do not exist: {missing}"


def test_every_docs_page_is_in_the_navigation():
    """A page left out of `nav` still builds, but nobody can find it."""
    nav = (ROOT / "mkdocs.yml").read_text().split("\nnav:\n", 1)[1].split("\n\n", 1)[0]
    pages = sorted(p.name for p in (ROOT / "docs").glob("*.md"))
    missing = [p for p in pages if f": {p}" not in nav]
    assert not missing, f"docs pages missing from the mkdocs.yml nav: {missing}"


def test_no_root_mcp_json_and_the_reason_is_recorded():
    """A root .mcp.json is read as a project config and breaks with ENOENT.

    The reason is maintainer-facing, so it lives in CLAUDE.md rather than the docs.
    """
    assert not (ROOT / ".mcp.json").exists()
    assert "plugin.json" in (ROOT / "CLAUDE.md").read_text()


def test_claude_md_exists_and_carries_the_firm_rule():
    text = (ROOT / "CLAUDE.md").read_text()
    assert "gated on user approval" in text
    assert "test_docs_current.py" in text, \
        "CLAUDE.md should point at the test that enforces its docs rule"


MAINTAINER_ONLY = [
    "CIRRO_HOME", "cirro-api-client", "save_process_revision", "get_all_records",
    "requiresUserInteraction", "subprocess", "stdio",
]


@pytest.mark.parametrize("term", MAINTAINER_ONLY)
def test_the_docs_stay_user_facing(term):
    """Implementation vocabulary belongs in CLAUDE.md or the README.

    The docs describe what using the plugin is like; every one of these terms is
    something a user never needs and a maintainer always does.
    """
    assert term not in DOCS, (
        f"{term!r} is implementation detail -- keep it in CLAUDE.md or the README and "
        "describe the user-visible consequence in the docs instead")

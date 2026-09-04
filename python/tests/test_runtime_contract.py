"""What the pinned runtime must provide.

The plugin overrides cirro's own `cirro_api_client<1.6.0` pin because pipeline
configuration is impossible without the >=1.6.0 routes. That override is only safe
while cirro itself still works against 1.6.0, so assert both halves here: a future
cirro release that breaks the pairing fails the build instead of silently removing
the ability to configure pipelines.

Also the plugin's own version, which is declared in three manifests that have to agree.
"""
import importlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_the_three_manifests_declare_one_version():
    """`plugin.json` is what a marketplace installs and what the user sees, so it is
    the source of truth; the two pyprojects are what gets built into the venvs. They
    had drifted to 0.3.0 against a released 0.4.2, so the installed wheel reported a
    version the plugin had not been for two releases.
    """
    declared = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    for manifest in (ROOT / "python" / "pyproject.toml",
                     ROOT / "runtime" / "pyproject.toml"):
        found = re.search(r'^version = "([^"]+)"', manifest.read_text(), re.M)
        assert found, f"{manifest.relative_to(ROOT)} declares no version"
        assert found.group(1) == declared, (
            f"{manifest.relative_to(ROOT)} says {found.group(1)} but "
            f".claude-plugin/plugin.json says {declared} -- bump them together")


def test_client_is_at_least_1_6():
    import importlib.metadata as metadata
    version = metadata.version("cirro-api-client")
    major, minor = (int(part) for part in version.split(".")[:2])
    assert (major, minor) >= (1, 6), (
        f"cirro-api-client {version} predates the process config-revision routes; "
        "pipeline configuration cannot work. See the override in runtime/pyproject.toml."
    )


@pytest.mark.parametrize("route", [
    "create_custom_process", "update_custom_process", "sync_custom_process",
    "save_process_revision", "get_process_resource", "get_latest_process_revision",
    "list_process_revisions", "get_process_revision", "get_process_parameters",
])
def test_process_route_exists(route):
    module = importlib.import_module(f"cirro_api_client.v1.api.processes.{route}")
    assert hasattr(module, "sync_detailed"), route


def test_config_source_enum_has_all_three_delivery_modes():
    """One per supported configuration pattern."""
    from cirro_api_client.v1.models import ConfigSource
    values = {member.value for member in ConfigSource}
    assert {"STORAGE", "WITH_PROCESS", "EXTERNAL"} <= values


def test_process_detail_exposes_the_fields_the_probe_reads():
    import attr
    from cirro_api_client.v1.models import ProcessDetail
    names = {a.name for a in attr.fields(ProcessDetail)}
    assert {"config_source", "custom_settings", "owner", "pipeline_code",
            "is_archived"} <= names


def test_custom_pipeline_settings_defaults_to_the_dot_cirro_folder():
    """Pattern 2's convention is the SDK's own default, not something we invented."""
    from cirro_api_client.v1.models import CustomPipelineSettings
    settings = CustomPipelineSettings(repository="owner/repo")
    assert settings.folder == ".cirro"
    assert settings.branch == "main"


def test_cirro_still_imports_against_the_overridden_client():
    """The override's whole premise."""
    import cirro
    from cirro import CirroApi, DataPortal  # noqa: F401
    from cirro.services.process import ProcessService
    assert hasattr(ProcessService, "create_custom_process")
    assert cirro.__file__


def test_asset_slots_cover_the_files_we_generate():
    from cirro_api_client.v1.models import ProcessResource
    slots = {member.value for member in ProcessResource}
    assert {"form", "input", "output", "compute", "preprocess", "documentation"} <= slots


def test_no_root_mcp_json_shadows_the_plugin_declaration():
    """A .mcp.json at the repo root is read as a PROJECT config by anyone running
    Claude Code inside this repo, and ${CLAUDE_PLUGIN_ROOT} is undefined there -- the
    command is spawned literally and fails with ENOENT. Declare the server in
    plugin.json instead, which is only ever interpreted as plugin scope."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    assert not (root / ".mcp.json").exists(), (
        "a root .mcp.json shadows the plugin's own MCP declaration when Claude Code "
        "runs inside this repo")
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    servers = manifest.get("mcpServers") or {}
    assert "cirro" in servers, "plugin.json must declare the cirro MCP server"
    assert "${CLAUDE_PLUGIN_ROOT}" in servers["cirro"]["command"]


ENTRY_POINTS = ["cirro_agent.cli", "cirro_agent.mcp_server", "cirro_agent.runner",
                "cirro_agent.worker.dispatch", "cirro_agent.worker.reads",
                "cirro_agent.worker.mutations", "cirro_agent.worker.portal"]


def test_importing_any_entry_point_does_not_load_the_sdk():
    """The deferral is the reason the architecture works, and nothing enforced it.

    `cirro.config.Constants` captures CIRRO_HOME at import time, so if any module on
    the CLI or MCP path grows a module-level `import cirro`, the SDK loads in a process
    where CIRRO_HOME was never set and silently uses the machine-wide ~/.cirro. The
    worker's own guard cannot catch that, because these paths never reach the worker.
    """
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    imports = ";".join(f"import {m}" for m in ENTRY_POINTS)
    code = (
        f"import sys;{imports};"
        "leaked=sorted(m for m in sys.modules "
        "if m=='cirro' or m.startswith('cirro.') or m.startswith('cirro_api_client'));"
        "print(','.join(leaked))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env={"PYTHONPATH": str(src), "PATH": "/usr/bin:/bin"},
                          timeout=120)
    assert done.returncode == 0, done.stderr
    leaked = [m for m in done.stdout.strip().split(",") if m]
    assert not leaked, (
        "these SDK modules load at import time, defeating the CIRRO_HOME contract: "
        f"{leaked}")

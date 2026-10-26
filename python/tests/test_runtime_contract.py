"""What the pinned runtime must provide.

Pipeline configuration is impossible without the `cirro_api_client` >=1.6.0 routes,
and the dashboard operations without cirro's own `DashboardService`. Assert both here,
so a release that loses either fails the build instead of silently removing what the
plugin was built on.

Also the plugin's own version, which is declared in three manifests that have to agree.
"""
import importlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_every_declaration_agrees_on_one_version():
    """`plugin.json` is what a marketplace installs and what the user sees, so it is
    the source of truth; the two pyprojects are what gets built into the venvs, and
    the MCP server reports its own version in serverInfo. The pyprojects had drifted
    to 0.3.0 against a released 0.4.2, so the installed wheel reported a version the
    plugin had not been for two releases.

    The server's string is checked here for the same reason, and because checking only
    the manifests is what let it sit at 0.4.2 through the whole of 0.5.0 -- the drift
    this test exists to catch, in the one place it was not looking.
    """
    declared = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    sources = {
        ROOT / "python" / "pyproject.toml": r'^version = "([^"]+)"',
        ROOT / "runtime" / "pyproject.toml": r'^version = "([^"]+)"',
        ROOT / "python" / "src" / "cirro_agent" / "mcp_server.py": r'version="([^"]+)"',
    }
    for path, pattern in sources.items():
        found = re.search(pattern, path.read_text(), re.M)
        assert found, f"{path.relative_to(ROOT)} declares no version"
        assert found.group(1) == declared, (
            f"{path.relative_to(ROOT)} says {found.group(1)} but "
            f".claude-plugin/plugin.json says {declared} -- bump them together")


def test_client_is_at_least_1_6():
    import importlib.metadata as metadata
    version = metadata.version("cirro-api-client")
    major, minor = (int(part) for part in version.split(".")[:2])
    assert (major, minor) >= (1, 6), (
        f"cirro-api-client {version} predates the process config-revision routes; "
        "pipeline configuration cannot work."
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
            "is_archived", "allow_multiple_sources"} <= names


def test_custom_pipeline_settings_defaults_to_the_dot_cirro_folder():
    """Pattern 2's convention is the SDK's own default, not something we invented."""
    from cirro_api_client.v1.models import CustomPipelineSettings
    settings = CustomPipelineSettings(repository="owner/repo")
    assert settings.folder == ".cirro"
    assert settings.branch == "main"


def test_cirro_provides_the_services_the_plugin_uses():
    import cirro
    from cirro import CirroApi, DataPortal  # noqa: F401
    from cirro.services.process import ProcessService
    from cirro.services.dashboard import DashboardService
    assert hasattr(ProcessService, "create_custom_process")
    assert {"list", "get", "create", "update", "delete"} <= set(dir(DashboardService))
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


def test_device_code_auth_still_exposes_what_the_refresh_lock_drives():
    """`portal.renew_under_lock` serializes the SDK's own token refresh across worker
    processes, which means reaching past its public surface -- the SDK guards the
    refresh with a per-process `threading.Lock` and offers no cross-process story.

    Pin the names here so a cirro upgrade that renames one fails the build. Silently
    disarming the lock would restore the bug it exists to prevent: concurrent refreshes
    provoke a Cognito throttle, and `_refresh_access_token` deletes the stored
    credential on any ClientError.
    """
    import inspect
    from cirro.auth.device_code import DeviceCodeAuth

    for method in ("_load_token_info", "_update_token_metadata",
                   "_refresh_access_token"):
        assert callable(getattr(DeviceCodeAuth, method, None)), (
            f"DeviceCodeAuth.{method} is gone; portal.renew_under_lock drives it")

    # Set in __init__, so they are instance state rather than class attributes.
    source = inspect.getsource(DeviceCodeAuth)
    for attribute in ("_token_info", "access_token_expiry"):
        assert f"self.{attribute} =" in source, (
            f"DeviceCodeAuth no longer sets {attribute}; portal._refresh_due reads it")


def test_the_token_store_still_raises_what_the_credential_mapping_reads():
    """`portal._credential_store_failure` turns a refusal from the OS credential store
    into CREDENTIAL_STORE_UNAVAILABLE, and it has only two things to work from: the
    exception's module, and `exit_status`. Neither is public API.

    On macOS the SDK persists the token through msal_extensions' `KeychainPersistence`
    and rewrites it on every lazy refresh, so any operation outliving its access token
    writes to the Keychain mid-run -- which is where a re-locked keychain refuses. If
    `exit_status` is ever renamed the mapping still fires but reports `status None`,
    losing the only fact that distinguishes a locked keychain from a missing one.
    """
    import ast
    import importlib.util
    import inspect
    from cirro.auth import device_code

    assert "KeychainPersistence" in inspect.getsource(device_code), (
        "cirro no longer stores the token in the OS keychain on macOS")

    # Read rather than import: `msal_extensions.osx` loads Security.framework in its
    # module body, so importing it is an OSError anywhere but macOS -- and CI is Linux,
    # which is exactly where this pin has to hold.
    spec = importlib.util.find_spec("msal_extensions.osx")
    assert spec and spec.origin, "msal_extensions is gone; cirro persists through it"
    tree = ast.parse(Path(spec.origin).read_text())
    keychain_error = next(
        (node for node in tree.body
         if isinstance(node, ast.ClassDef) and node.name == "KeychainError"), None)
    assert keychain_error is not None, (
        "msal_extensions.osx no longer defines KeychainError")

    init = next(node for node in keychain_error.body
                if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    assert "exit_status" in [arg.arg for arg in init.args.args], (
        "KeychainError no longer carries exit_status; the mapping would report None")

    # The empty `str()` that made this arrive as `KeychainError: `. The double in
    # test_token_refresh.py reproduces it, so it has to stay true here.
    parent = next(call for call in ast.walk(init)
                  if isinstance(call, ast.Call)
                  and isinstance(call.func, ast.Attribute)
                  and call.func.attr == "__init__")
    assert not parent.args and not parent.keywords, (
        "KeychainError now passes a message up to OSError, so it stringifies to "
        "something -- carry the SDK's own text instead of the exit_status table")


def test_the_launch_route_a_failed_launch_is_judged_by_still_exists():
    """`analysis_ops._launch_failure` decides whether a failed launch may have started a
    run by asking whether the exception passed through the generated launch route, and
    reads the reply back from frames holding it as `response`. If either name moved,
    every failure would read as "nothing was sent, planning again is safe" -- the one
    answer that can start a second run."""
    import importlib
    import inspect
    from cirro.services import execution
    from cirro_agent.worker.analysis_ops import LAUNCH_ROUTE

    route = importlib.import_module(LAUNCH_ROUTE)
    assert "response" in inspect.signature(route._parse_response).parameters
    assert getattr(execution, "run_analysis", None) is route, (
        f"cirro's ExecutionService no longer launches through {LAUNCH_ROUTE}; "
        "analysis_ops._launch_failure would misjudge every failure")
    assert "run_analysis.sync(" in inspect.getsource(execution.ExecutionService.run_analysis)

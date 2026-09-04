"""Run one worker operation in a fresh subprocess.

This is the only place `CIRRO_HOME` is injected, and it is injected into a child's
environment rather than set in this process -- `cirro.config.Constants` freezes it
at import time, so a parent that has already imported cirro could never change it.
Nothing in this module (or anything it imports) may import cirro.
"""
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from . import errors, session as session_mod

WORKER_TIMEOUT = 900  # uploads and downloads are legitimately slow
READY_MARKER = "cirro-agent-ready.json"
# The maintainer's environment, built by the README's uv sync. It sits beside the
# installed runtime rather than in the checkout, for the reason plugin_data() gives.
DEV_VENV = "dev-venv"


def plugin_root() -> Path:
    for var in ("CIRRO_AGENT_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        value = os.environ.get(var)
        if value:
            return Path(value)
    # src/cirro_agent/runner.py -> repo root
    return Path(__file__).resolve().parents[3]


def plugin_data() -> Path:
    """Where the runtime lives. Never inside the plugin root.

    A directory marketplace copies that tree verbatim into every install -- no
    .gitignore, no exclude list -- so a virtualenv left there is shipped with the
    plugin. `scripts/_resolve-python.sh` is the other half of this and must agree.
    """
    for var in ("CIRRO_AGENT_PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        value = os.environ.get(var)
        if value:
            return Path(value)
    share = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(share) / "cirro-agent"


def interpreter() -> Path:
    """Locate the pinned runtime. Never fall back to the ambient interpreter.

    The SDK pins awscrt and s3fs, which ship per-CPython wheels; picking up
    whatever `python3` happens to be on PATH is how you get a source build of
    awscrt, or no build at all.
    """
    override = os.environ.get("CIRRO_AGENT_PYTHON")
    if override:
        return Path(override)
    for candidate in (plugin_data() / "venv" / "bin" / "python",
                      plugin_data() / DEV_VENV / "bin" / "python"):
        if candidate.exists():
            return candidate
    raise errors.RuntimeNotInstalled(
        "The Cirro runtime is not installed yet.",
        what="The plugin runs the Cirro SDK in its own pinned Python environment.",
        user_action="Run /cirro:setup (takes up to two minutes on first use).",
        looked_in=[str(plugin_data() / "venv"), str(plugin_data() / DEV_VENV)],
    )


def check_ready() -> dict:
    """Cheap stat-only probe. A real import check would cost half a second per call."""
    python = interpreter()
    marker = plugin_data() / READY_MARKER
    if marker.is_file():
        try:
            return json.loads(marker.read_text())
        except (OSError, ValueError):
            pass
    dev = plugin_data() / DEV_VENV
    if dev.exists() and str(python).startswith(str(dev)):
        return {"mode": "dev", "python": str(python)}
    raise errors.RuntimeNotInstalled(
        f"{python} exists but the runtime was never marked ready.",
        what="The dependency install did not finish.",
        user_action="Run /cirro:setup.",
    )


def child_environment(session: session_mod.Session) -> dict:
    env = session_mod.child_env(session)
    # The worker must be importable even when cirro_agent is not pip-installed.
    src = str(plugin_root() / "python" / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
    return env


def call(request: dict, *, session: Optional[session_mod.Session] = None,
         start: Optional[Path] = None, timeout: int = WORKER_TIMEOUT) -> dict:
    """Execute one operation and return its response dict."""
    try:
        session = session or session_mod.load(start)
        check_ready()
        python = interpreter()
    except errors.AgentError as exc:
        return exc.to_dict()

    payload = dict(request)
    payload["base_url"] = session.base_url
    payload["session_dir"] = str(session.path.resolve())

    try:
        completed = subprocess.run(
            [str(python), "-m", "cirro_agent.worker"],
            input=json.dumps(payload), capture_output=True, text=True,
            env=child_environment(session), timeout=timeout,
            cwd=str(plugin_root()),
        )
    except subprocess.TimeoutExpired:
        return errors.UpstreamError(
            f"The operation did not finish within {timeout}s.",
            what="The worker was terminated.",
            retry_with=[{"note": "retry, or narrow the request"}],
            user_action="For large transfers, use the Cirro CLI or web UI instead.",
        ).to_dict()

    if completed.stdout.strip():
        try:
            return json.loads(completed.stdout)
        except ValueError:
            pass
    detail = (completed.stderr or completed.stdout or "").strip()[-1500:]
    if "ModuleNotFoundError" in detail and "cirro" in detail:
        return errors.RuntimeNotInstalled(
            "The runtime environment is missing the cirro SDK.",
            what="The pinned environment is incomplete.",
            user_action="Run /cirro:setup to reinstall it.",
            detail=detail,
        ).to_dict()
    return errors.UpstreamError(
        f"The worker exited with code {completed.returncode} and no usable output.",
        what="An unexpected failure inside the worker.",
        retry_with=[{"note": "retry once"}],
        user_action="Run /cirro:doctor and report the detail below if it persists.",
        detail=detail,
    ).to_dict()

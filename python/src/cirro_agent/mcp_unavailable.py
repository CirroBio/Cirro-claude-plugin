"""The MCP server that answers when the pinned runtime is not installed.

`mcp_server` imports the third-party `mcp` package at module scope, so the ambient
interpreter cannot run it: it dies at import, and the host reports that as a bare
"failed to connect" with no cause. That is what every first install looks like before
`/cirro:setup` has run, and it is what a setup that built the runtime somewhere the
server does not look looked like too -- a whole investigation to reach a message the
CLI prints in one line.

So this stands in. Stdlib only, so any `python3` can run it; it speaks just enough of
the protocol to connect, list the same tools, and answer every call with the
RUNTIME_NOT_INSTALLED payload the CLI prints -- carrying the paths actually searched,
which is the one fact that distinguishes "never installed" from "installed elsewhere".
"""
import json
import sys
from typing import Optional

from . import errors, runner

# The tools `mcp_server` registers. Duplicated rather than imported, because importing
# that module is the single thing this one exists to avoid; `test_mcp_unavailable.py`
# pins the two lists together so a new tool cannot appear in only one of them.
TOOLS = ("session_init", "login_start", "login_wait", "whoami",
         "logout", "session_remove", "plan", "apply",
         "grant", "apply_granted", "grants")

PROTOCOL_VERSION = "2025-06-18"

INSTRUCTIONS = """\
The Cirro runtime is not installed, so no tool here can run and no read will work
either. Nothing is wrong with the tenant or the login.

Run /cirro:setup (up to two minutes on first use), then reconnect this server with
/mcp or restart Claude Code. /cirro:doctor reports what is missing.
"""

TOOL_DESCRIPTION = (
    "Unavailable: the Cirro runtime is not installed. Run /cirro:setup, then "
    "reconnect this server."
)


def plugin_version() -> str:
    """From the manifest the host read in order to launch this process.

    Its absence is not a case to handle: nothing would have spawned this server.
    """
    manifest = runner.plugin_root() / ".claude-plugin" / "plugin.json"
    return json.loads(manifest.read_text())["version"]


def unavailable() -> dict:
    """Why no tool can run -- resolved per call, not once at startup.

    Someone may run /cirro:setup while this process is up, and then the honest
    answer changes from "install it" to "reconnect".
    """
    try:
        runner.interpreter()
    except errors.RuntimeNotInstalled as exc:
        return exc.to_dict()
    return errors.RuntimeNotInstalled(
        "The Cirro runtime is installed now, but this server started before it was.",
        what="A stdio server cannot pick up a runtime that appeared after it launched.",
        user_action="Reconnect the server: /mcp, or restart Claude Code.",
    ).to_dict()


def handle(message: dict) -> Optional[dict]:
    """The response to one request, or None for a notification."""
    message_id = message.get("id")
    if message_id is None:  # a notification is never answered
        return None
    method = message.get("method")
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return _result(message_id, {
            # Echo the client's version rather than insisting on ours: this server
            # uses no feature that any of them differ over.
            "protocolVersion": requested or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "cirro", "title": "Cirro Data Platform",
                           "version": plugin_version()},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(message_id, {})
    if method == "tools/list":
        return _result(message_id, {"tools": [
            {"name": name, "description": TOOL_DESCRIPTION,
             "inputSchema": {"type": "object"}} for name in TOOLS]})
    if method == "tools/call":
        return _result(message_id, {
            "content": [{"type": "text", "text": json.dumps(unavailable(), indent=2)}],
            "isError": True,
        })
    return {"jsonrpc": "2.0", "id": message_id, "error": {
        "code": -32601,
        "message": f"{method} is not supported while the Cirro runtime is missing",
    }}


def _result(message_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue  # a frame we cannot parse has no id to answer on
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

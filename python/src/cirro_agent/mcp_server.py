"""The MCP surface: authentication, and the gated write path.

Seven tools. Six are ordinary; `apply` carries
`_meta["anthropic/requiresUserInteraction"]`, which is the one enforcement
primitive a plugin author actually has -- it prompts on every call in every
permission mode, is not skipped by an allow rule, is not suppressed by a hook
returning allow, and fails closed when prompting is impossible.

Reads deliberately live elsewhere (`cirro-agent read ...` over Bash): they are
ungated by policy, high-cardinality, and benefit from shell composition, and
keeping them out of here keeps this tool list short enough to stay in context.

This process must never import cirro. `cirro.config.Constants` freezes CIRRO_HOME
at import time, so a long-lived server could only ever serve one session folder.
Every operation is a fresh subprocess instead.
"""
from pathlib import Path
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from . import errors, runner, session as session_mod

INSTRUCTIONS = """\
Cirro data platform access.

READS ARE NOT HERE. Run them through Bash:
    cirro-agent read --help
    cirro-agent read datasets -p "Project Name" -f "status:FAILED created:last-7d"
    cirro-agent read describe dataset       # field catalog, tiers, query grammar
Listing responses are capped, faceted over the whole match set, and carry
`next_calls` with exact predicted match counts -- follow those rather than
guessing a narrower filter.

WRITES ARE TWO STEPS, ALWAYS:
    1. plan(op, args)  -- read-only. Resolves names to IDs, validates parameters,
       computes blast radius, and returns `rendered` plus a single-use plan_id.
    2. Show `rendered` to the person in your own message.
    3. apply(plan_id, digest, approved_summary) -- prompts for approval.
`apply` takes no operation arguments, so what was approved is what runs.
"""

server = MCPServer(
    name="cirro",
    title="Cirro Data Platform",
    instructions=INSTRUCTIONS,
    version="0.6.0",
)

# Every tool that resolves a session takes this. Without it the only way to work
# against a second bound folder was to move the whole Claude session, because the
# walk started at the process's working directory and nothing could redirect it.
DIRECTORY_DOC = (
    " Pass `directory` to work against a session folder outside the current working "
    "directory; the resolved folder and tenant are echoed in the response either way."
)


def _start(directory: Optional[str]) -> Optional[Path]:
    """The directory to resolve the session from, or None for the process's own."""
    return Path(directory).expanduser() if directory else None


@server.tool(
    name="session_init",
    title="Create a Cirro session folder",
    description=(
        "Create the .cirro session folder in a working directory and bind it to one "
        "Cirro tenant. The folder holds this directory's credential and cache, kept "
        "separate from the machine-wide ~/.cirro. Call this once per project directory; "
        "it is never created implicitly. Pass `directory` to bind one other than the "
        "working directory. Creates a directory and writes to it."
    ),
)
def session_init(base_url: str, directory: Optional[str] = None) -> dict:
    """Bind a working directory to a Cirro tenant."""
    try:
        target = Path(directory).expanduser().resolve() if directory else Path.cwd()
        current = session_mod.init(target, base_url)
        tracked = session_mod.is_git_tracked(current.path)
        warnings = []
        if tracked:
            warnings.append(
                f"{current.path} is tracked by git. It holds credential state and should "
                "not be committed; the folder ships its own .gitignore but an existing "
                "tracked entry overrides it.")
        return {
            "ok": True,
            "session_dir": str(current.path),
            "base_url": current.base_url,
            "auth": current.auth,
            "warnings": warnings,
            "next": {"tool": "login_start",
                     "note": "authenticate this folder, then reads become available"},
        }
    except errors.AgentError as exc:
        return exc.to_dict()


@server.tool(
    name="login_start",
    title="Begin a Cirro login",
    description=(
        "Start a device-code login and return immediately with a URL and code to relay "
        "to the person. Never blocks. Creates the session folder when given a base_url, "
        "so it needs no session_init first, and reports a missing runtime or a "
        "mismatched tenant itself, so it needs no doctor pass first -- call it first. "
        "If the folder already holds a working credential it reports the authenticated "
        "identity instead, proved by a live API call, refreshing an expired token "
        "rather than starting a new login. On a pending login it prints the link to "
        "the user's terminal and reports whether that worked in printed_to_terminal, "
        "but relay verification_url in your very next message regardless -- printing "
        "is a courtesy that may not have reached them. Nothing opens a browser. Call "
        "login_wait only after that message."
    ) + DIRECTORY_DOC,
)
def login_start(base_url: Optional[str] = None,
                directory: Optional[str] = None) -> dict:
    """Start a device-code login without waiting for it."""
    request: dict[str, Any] = {"verb": "login_start"}
    start = _start(directory)
    try:
        current = session_mod.load(start)
    except errors.NoSessionFolder as exc:
        # Only a missing folder may be created here. Anything else -- a tenant conflict
        # above all -- must surface, not be papered over by binding a new session.
        if not base_url:
            return exc.to_dict()
        try:
            current = session_mod.init(start or Path.cwd(), base_url)
        except errors.AgentError as init_error:
            return init_error.to_dict()
    except errors.AgentError as exc:
        return exc.to_dict()
    if base_url and session_mod._normalize_base_url(base_url) != current.base_url:
        # One folder serves one tenant. Silently logging in to the bound tenant while
        # the caller named a different one is the wrong kind of safe: it answers a
        # question nobody asked, and the two paths that used to surface the binding
        # (session_init, doctor) are no longer on the login path.
        return errors.TenantMismatch(
            f"This folder is bound to {current.base_url}, not {base_url}.",
            what="A session folder serves exactly one Cirro tenant.",
            user_action=(f"Work in a directory bound to {base_url}, or drop the "
                         f"base_url argument to log in to {current.base_url}."),
            retry_with=[{"tool": "login_start"}],
        ).to_dict()
    return runner.call(request, session=current)


@server.tool(
    name="login_wait",
    title="Wait for a Cirro login to complete",
    description=(
        "Poll for completion of a login started by login_start, against its own timeout "
        "rather than the device code's lifetime. Returns pending, ok, or a typed "
        "failure -- it can never hang. A pending result is not an error: relay the link "
        "again and call this once more. Only needed when login_start returned pending; "
        "on a folder that was already logged in it simply confirms the identity."
    ) + DIRECTORY_DOC,
)
def login_wait(timeout_s: int = 120, directory: Optional[str] = None) -> dict:
    """Wait, boundedly, for the sign-in to land."""
    return runner.call({"verb": "login_wait", "timeout_s": timeout_s},
                       start=_start(directory))


@server.tool(
    name="whoami",
    title="Cirro identity and runtime",
    description=(
        "Report the authenticated user, tenant, session folder, and the resolved cirro "
        "SDK version, including whether it is an editable local checkout. Read-only."
    ) + DIRECTORY_DOC,
)
def whoami(directory: Optional[str] = None) -> dict:
    """Who am I, against which tenant, with which SDK."""
    return runner.call({"verb": "whoami"}, start=_start(directory))


@server.tool(
    name="logout",
    title="Discard this folder's Cirro credential",
    description=(
        "Remove the cached credential for this session folder, forcing a fresh login. "
        "On macOS the token itself lives in the login Keychain; this reports the command "
        "to remove that entry rather than editing the keychain itself."
    ) + DIRECTORY_DOC,
)
def logout(directory: Optional[str] = None) -> dict:
    """Forget this folder's credential."""
    return runner.call({"verb": "logout"}, start=_start(directory))


@server.tool(
    name="plan",
    title="Plan a Cirro change (read-only)",
    description=(
        "Resolve and validate a change without making it. Returns `rendered` -- the text "
        "to show the person -- plus a single-use plan_id and digest, good for an hour "
        "and refused sooner if the facts behind them change. Makes no modification of "
        "any kind.\n\n"
        "Operations: run_analysis, resume_analysis, stop_analysis, upload_dataset, "
        "upload_reference, import_public, update_dataset, update_samplesheet, "
        "delete_dataset, create_project, update_project_tags, archive_project, "
        "unarchive_project, set_user_role, invite_user, update_sample, "
        "update_project_schema, create_share, delete_share, subscribe_share, "
        "unsubscribe_share, create_workspace, start_workspace, stop_workspace, "
        "delete_workspace, download_files, create_process, update_process, "
        "upload_process_config, sync_process_config, archive_process, "
        "write_process_config, commit_process_config.\n\n"
        "If a plan expired or went stale, pass its id as `renew_from` with the same op "
        "and args: an identical plan comes back as `unchanged: true` without "
        "re-sending the text, since the person has already read it.\n\n"
        "op=\"chain\" approves several build-up operations as one action. Steps are "
        "{op, args, label?}; an argument a later step cannot know yet is written "
        "{\"from_step\": N, \"field\": \"...\"} and resolved from that step's recorded "
        "output. Add `retry` = {redo: [labels], max_attempts: 2-5, may_change: "
        "[fields]} to authorize bounded reruns, then resume with `restart_from` = "
        "{plan_id, label} -- which reuses what earlier steps produced instead of "
        "repeating them, and counts the attempt itself. Destructive operations cannot "
        "go in a chain.\n\n"
        "For run_analysis, read `cirro-agent read process-params <process>` first: it is "
        "the authoritative list of accepted parameter keys.\n\n"
        "create_process and upload_process_config generate the configuration from "
        "`design_spec`, or take `assets_dir` -- a folder of process-form.json and "
        "friends written by hand, uploaded verbatim -- for form logic a spec cannot "
        "express. Exactly one of the two on an upload."
    ) + DIRECTORY_DOC,
)
def plan(op: str, args: Optional[dict] = None,
         renew_from: Optional[str] = None,
         directory: Optional[str] = None) -> dict:
    """Resolve, validate and describe a change without performing it."""
    # `verb` routes to the handler; `op` names the mutation being planned.
    return runner.call({"verb": "plan", "op": op, "args": args or {},
                        "renew_from": renew_from}, start=_start(directory))


@server.tool(
    name="apply",
    title="Apply an approved Cirro plan",
    description=(
        "Execute a plan created by `plan`. Takes no operation arguments -- everything "
        "comes from the stored plan, so what the person approved is exactly what runs. "
        "Refuses an expired, already-used, tampered, or different-user plan. Show the "
        "plan's `rendered` text to the person before calling this."
    ) + DIRECTORY_DOC,
    meta={"anthropic/requiresUserInteraction": True},
)
def apply(plan_id: str, digest: str, approved_summary: str = "",
          directory: Optional[str] = None) -> dict:
    """Perform the change a plan describes, after approval.

    `approved_summary` is supplied by the approval hook, which reads it from the
    stored plan -- so the value that proves the approved text matches the executed
    plan does not travel through the model's context. Passing it explicitly still
    works, and is the fallback when hooks are disabled.
    """
    return runner.call({"verb": "apply", "plan_id": plan_id, "digest": digest,
                        "approved_summary": approved_summary},
                       start=_start(directory))


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

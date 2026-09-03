#!/usr/bin/env python3
"""PreToolUse gate for the Cirro plugin.

Stdlib only and no virtualenv, so it stays fast enough to run on every Bash call.

Three jobs:
  1. Deny routes that reach the Cirro SDK without going through the approval gate.
  2. On mcp__cirro__apply, read the plan file itself -- server truth, not the model's
     claim -- and surface it, refusing outright if the plan is not applicable.
  3. Ask before writing a Python file that imports cirro, since that file could
     later be executed outside the gate.

Honest about its ceiling: this is string analysis of a shell command. It is
defeated by base64, by writing a script and running it, or by an interpreter it
does not know. It makes accidental bypass impossible and deliberate bypass
conspicuous. It is not a sandbox. The real boundary is a `permissions.deny` rule
in the user's own settings, which a plugin cannot ship -- see the README.
"""
import json
import os
import re
import shlex
import sys
from typing import NoReturn
import time
from pathlib import Path

# How much of one argument's value the approval prompt shows before eliding.
ARG_PREVIEW_CHARS = 160
MAX_PLAN_AGE_GRACE = 5  # seconds of clock slack when checking expiry

# The digest contract is imported from the plugin's own plans module rather than
# copied here. plans imports only stdlib (test_gate asserts it stays that way), so the
# hook still runs with a bare system Python and no virtualenv -- and there is now one
# definition of what a plan's integrity covers instead of two kept in step by a test.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "src"))
from cirro_agent.plans import PLAN_ID_PATTERN, canonical_digest  # noqa: E402

# Text that means a command is talking to the Cirro SDK.
SDK_MARKERS = re.compile(
    r"(?:^|[^\w.])(?:import\s+cirro|from\s+cirro\b|cirro\.sdk|cirro_api_client"
    r"|DataPortal|DataPortalLogin|CirroApi"
    # `python -m cirro.cli upload` reaches the upstream CLI without naming an import.
    r"|-m\s+cirro(?:[.\s]|$)|\bcirro\.cli\b)", re.I)

INTERPRETERS = re.compile(
    r"^(?:python[0-9.]*|ipython[0-9.]*|jupyter|uv|uvx|pipx|poetry|conda|hatch|pdm)$",
    re.I)

# A shell re-enters the parser: its -c payload is a command in its own right, and
# without this `bash -c "python -c 'import cirro'"` sails past every other check.
SHELLS = re.compile(r"^(?:sh|bash|zsh|dash|ksh|fish)$", re.I)

# Wrappers that take their own argument before the real program. Skipping only the
# wrapper name left `program` pointing at the argument, so `timeout 5 python ...`
# resolved to "5" and matched nothing.
# The value encodes what to skip after the wrapper before the real program name:
# 0 = nothing, 1 = one option flag, 2 = one option flag and its value. The scan
# runs once per wrapper, so `timeout -k 5 10 python` skips `-k 5` and `10` is
# then read as the argument the wrapper always takes.
WRAPPERS_WITH_ARG = {"timeout": 1, "nice": 2, "env": 0, "sudo": 0, "stdbuf": 1,
                     "ionice": 2, "setsid": 0, "nohup": 0, "time": 0}

# Runners that fetch and execute a package in one step, so the SDK never has to be
# named in code for it to run: `uv run --with cirro ...`, `uvx cirro ...`.
PACKAGE_RUNNERS = {"uv", "uvx", "pipx", "poetry", "conda", "hatch", "pdm"}
EXECUTING_SUBCOMMANDS = {"run", "exec", "tool"}

TOKEN_SNOOPING = re.compile(
    r"(?:\.token\.dat|security\s+find-generic-password|security\s+dump-keychain"
    # Any spelling of the machine-wide credential directory.
    r"|(?:~|\$HOME|\$\{HOME\}|/(?:home|Users)/[^/\s]+)/\.cirro\b)", re.I)

# The upstream CLI performs uploads and logins with no plan and no approval.
UPSTREAM_CLI = re.compile(r"^(?:cirro|cirro-cli)$")
UPSTREAM_CLI_READ_ONLY = {"list-projects", "list-datasets", "list-files", "--help",
                          "--version"}

AWS_DATA_MOVERS = {"cp", "sync", "mv", "rm", "rb"}

SPLITTERS = re.compile(r"(?:\|\||&&|[;|&\n])")

# shlex does not expand these, so a substituted command never became a segment of its
# own and its program name was never inspected.
SUBSTITUTIONS = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


def respond(decision: str, reason: str, extra: dict | None = None) -> NoReturn:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    if extra:
        payload["hookSpecificOutput"].update(extra)
    if decision == "ask":
        # The `ask` reason is documented as reaching Claude; whether it renders in the
        # human's dialog is not guaranteed, so also emit it as context.
        payload["hookSpecificOutput"]["additionalContext"] = reason
    json.dump(payload, sys.stdout)
    sys.exit(0)


def block(reason: str) -> NoReturn:
    """Exit 2: evaluated before permission rules and not overridable by an allow."""
    sys.stderr.write(reason)
    sys.exit(2)


def passthrough() -> NoReturn:
    sys.exit(0)


# --------------------------------------------------------------------------- bash


def segments(command: str) -> list:
    """Split into independently-executed commands, including substituted ones."""
    extra = [m.group(1) or m.group(2) for m in SUBSTITUTIONS.finditer(command)]
    flattened = SUBSTITUTIONS.sub(" ", command)
    parts = SPLITTERS.split(flattened) + extra
    return [s.strip() for s in parts if s and s.strip()]


def tokens_of(segment: str) -> list:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def inspect_bash(command: str) -> None:
    # A heredoc puts the payload on later lines, which segment splitting separates
    # from the interpreter that consumes it. So an interpreter anywhere in a command
    # that mentions the SDK anywhere is treated as driving it.
    whole_mentions_sdk = bool(SDK_MARKERS.search(command))

    if TOKEN_SNOOPING.search(command):
        block("Blocked by the Cirro plugin: this command reads Cirro credential storage "
              "or the machine-wide ~/.cirro. The plugin keeps credentials in a "
              "folder-local session and never exposes token material to the model.\n")

    for segment in segments(command):
        words = tokens_of(segment)
        if not words:
            continue
        # Skip wrappers so `env FOO=1 python -c ...` and `timeout 5 python ...` are
        # both seen. Wrappers that consume an argument advance past it too.
        index = 0
        while index < len(words):
            token = words[index]
            name = Path(token).name
            if "=" in token and not token.startswith("-"):
                index += 1
                continue
            if name in WRAPPERS_WITH_ARG or name in ("command", "builtin", "exec",
                                                     "xargs"):
                index += 1
                # Consume the wrapper's own options and their values.
                budget = WRAPPERS_WITH_ARG.get(name, 0)
                while index < len(words) and budget and words[index].startswith("-"):
                    index += 2 if budget > 1 else 1
                    budget = 0
                if name == "timeout" and index < len(words) and \
                        re.fullmatch(r"[\d.]+[smhd]?", words[index] or ""):
                    index += 1
                continue
            break
        if index >= len(words):
            continue
        program = Path(words[index]).name

        # A shell's -c payload is itself a command; re-enter the parser on it.
        if SHELLS.match(program):
            for offset, token in enumerate(words[index + 1:], start=index + 1):
                if token == "-c" and offset + 1 < len(words):
                    inspect_bash(words[offset + 1])
            continue

        if program in PACKAGE_RUNNERS:
            args = words[index + 1:]
            executing = (program == "uvx"
                         or any(a in EXECUTING_SUBCOMMANDS for a in args[:2]))
            if executing and any(
                    re.fullmatch(r"cirro(\[[^\]]*\])?(?:[=<>!~@].*)?", a) for a in args):
                block(
                    f"Blocked by the Cirro plugin: `{program}` is being used to fetch and "
                    "run the cirro package directly, which bypasses the approval gate on "
                    "every create, update and delete.\n\n"
                    "Use instead:\n"
                    "  reads   cirro-agent read --help\n"
                    "  writes  the plan and apply MCP tools\n")

        if INTERPRETERS.match(program) and (SDK_MARKERS.search(segment)
                                            or whole_mentions_sdk):
            block(
                f"Blocked by the Cirro plugin: `{program}` is being used to drive the "
                "Cirro SDK directly, which bypasses the approval gate on every create, "
                "update and delete.\n\n"
                "Use instead:\n"
                "  reads   cirro-agent read --help\n"
                "  writes  the plan and apply MCP tools\n")

        if UPSTREAM_CLI.match(program):
            first = next((w for w in words[index + 1:] if not w.startswith("-")), "")
            if first not in UPSTREAM_CLI_READ_ONLY:
                block(
                    "Blocked by the Cirro plugin: the upstream `cirro` CLI performs "
                    f"uploads, logins and downloads ({first or 'interactive mode'}) with "
                    "no plan and no approval, and writes credentials to the global "
                    "~/.cirro.\n\n"
                    "Use instead:\n"
                    "  reads   cirro-agent read --help\n"
                    "  writes  the plan and apply MCP tools\n")

        if program == "cirro-agent" and words[index + 1:]:
            if words[index + 1] not in ("read", "-h", "--help"):
                block(
                    "Blocked by the Cirro plugin: `cirro-agent` only has a `read` "
                    "subcommand. Mutations go through the plan and apply MCP tools.\n")

        if program == "aws":
            s3_args = words[index + 1:]
            if "s3" in s3_args:
                after = s3_args[s3_args.index("s3") + 1:]
                verb = next((w for w in after if not w.startswith("-")), "")
                if verb in AWS_DATA_MOVERS and re.search(r"cirro", segment, re.I):
                    block(
                        "Blocked by the Cirro plugin: moving data in or out of Cirro "
                        "storage with the AWS CLI bypasses the approval gate and Cirro's "
                        "own dataset bookkeeping.\n\n"
                        "Use the plan and apply MCP tools instead.\n")
    passthrough()


# --------------------------------------------------------------------------- apply


def find_session(start: Path) -> Path | None:
    here = start.resolve()
    home = Path.home().resolve()
    for candidate in [here, *here.parents]:
        if candidate == home.parent:
            break
        marker = candidate / ".cirro" / "session.json"
        if marker.is_file():
            return candidate / ".cirro"
    return None


def inspect_apply(tool_input: dict, cwd: str) -> None:
    plan_id = tool_input.get("plan_id")
    if not plan_id:
        block("Blocked by the Cirro plugin: apply requires a plan_id from a prior "
              "plan call.\n")
    if not PLAN_ID_PATTERN.match(str(plan_id)):
        # The id becomes a path, so anything else is refused before touching disk.
        block(f"Blocked by the Cirro plugin: {plan_id!r} is not a valid plan id.\n")

    session_dir = find_session(Path(cwd or os.getcwd()))
    if session_dir is None:
        # Cannot verify, so do not silently allow: make the human decide.
        respond("ask", "The Cirro plugin could not locate the session folder to verify "
                       f"plan {plan_id}. Approve only if you know what this applies.")

    path = session_dir / "plans" / f"{plan_id}.json"
    if not path.is_file():
        block(f"Blocked by the Cirro plugin: no plan {plan_id} exists in {session_dir}. "
              "Call plan first.\n")
    try:
        plan = json.loads(path.read_text())
    except (OSError, ValueError):
        block(f"Blocked by the Cirro plugin: plan {plan_id} is unreadable.\n")

    if plan.get("status") != "pending":
        block(f"Blocked by the Cirro plugin: plan {plan_id} is "
              f"'{plan.get('status')}', not pending. A plan authorizes exactly one "
              "change; create a new one.\n")
    if time.time() > float(plan.get("expires_at", 0)) + MAX_PLAN_AGE_GRACE:
        block(f"Blocked by the Cirro plugin: plan {plan_id} has expired. Re-plan so the "
              "approval reflects current figures.\n")
    # Recomputed from the stored record, so an edit that leaves the digest field alone
    # is still caught. Must match cirro_agent.plans.canonical_digest exactly.
    if canonical_digest(plan) != plan.get("digest"):
        block(f"Blocked by the Cirro plugin: plan {plan_id} does not match its own "
              "digest; it was modified after it was created.\n")
    supplied = tool_input.get("digest")
    if supplied and supplied != plan.get("digest"):
        block(f"Blocked by the Cirro plugin: the digest supplied for plan {plan_id} does "
              "not match the stored plan. The plan may have been altered.\n")

    lines = [
        f"Cirro {plan.get('base_url')} as {plan.get('username') or 'unknown user'}",
        f"Operation: {plan.get('op')}  (risk: {plan.get('risk') or 'cirro_write'})",
        "",
    ]
    # Derived from op/args, not from the plan's prose. The summary is advisory text;
    # these are the values the executor will actually use.
    args = plan.get("args")
    if plan.get("op") == "chain" and isinstance(args, dict):
        # A chain's arguments are a list of steps; one truncated preview of that list
        # could hide an operation. Every step is named, from the plan file, before the
        # generic argument dump.
        steps = args.get("steps") or []
        lines.append(f"Runs {len(steps)} step(s) in order:")
        for index, step in enumerate(steps, start=1 + len(args.get("carried") or [])):
            label = step.get("label") if isinstance(step, dict) else None
            op_name = step.get("op") if isinstance(step, dict) else "?"
            lines.append(f"  {index}. {op_name}" + (f"  [{label}]" if label else ""))
        if args.get("carried"):
            lines.append(f"  reusing {len(args['carried'])} earlier step(s) unchanged")
        lines.append("")
    args = plan.get("args")
    if isinstance(args, dict) and args:
        lines.append("Will run with:")
        for key in sorted(args):
            value = args[key]
            rendered = json.dumps(value, default=str)
            if len(rendered) > ARG_PREVIEW_CHARS:
                rendered = rendered[:ARG_PREVIEW_CHARS] + f"... ({len(rendered)} chars)"
            lines.append(f"  {key} = {rendered}")
        lines.append("")
    summary = (plan.get("summary") or "").rstrip()
    if summary:
        lines += ["Summary provided by the plan:", summary, ""]
    warnings = plan.get("warnings")
    if isinstance(warnings, (list, tuple)) and warnings:
        lines += ["Warnings:"] + [f"  - {w}" for w in warnings] + [""]
    try:
        expires = time.strftime("%H:%M:%SZ", time.gmtime(float(plan["expires_at"])))
    except (KeyError, TypeError, ValueError):
        expires = "an unreadable time"
    lines.append(f"Plan {plan_id} is single-use and expires at {expires}.")
    # Supplied from the stored plan rather than echoed by the model. apply requires
    # the approved text to match the plan byte for byte; sourcing it here means that
    # check costs nothing in context, and the value cannot be the model's paraphrase.
    respond("ask", "\n".join(lines),
            {"updatedInput": {**tool_input,
                              "plan_id": plan_id,
                              "digest": plan.get("digest") or "",
                              "approved_summary": plan.get("summary") or ""}})


# --------------------------------------------------------------------------- write


def inspect_write(tool_input: dict) -> None:
    target = tool_input.get("file_path") or ""
    if not target.endswith((".py", ".ipynb", ".sh")):
        passthrough()
    body = " ".join(str(tool_input.get(key) or "")
                    for key in ("content", "new_string", "new_source"))
    if not SDK_MARKERS.search(body):
        passthrough()
    respond("ask",
            f"This writes Cirro SDK code to {target}. Authoring a script is legitimate, "
            "but running it would bypass the plugin's approval gate on creates, updates "
            "and deletes. Approve if you intend to review and run it yourself.")


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        passthrough()

    tool = event.get("tool_name") or ""
    tool_input = event.get("tool_input") or {}

    if tool == "Bash":
        inspect_bash(tool_input.get("command") or "")
    if tool.endswith("__apply") and "cirro" in tool:
        inspect_apply(tool_input, event.get("cwd") or "")
    if tool in ("Write", "Edit", "NotebookEdit"):
        inspect_write(tool_input)
    passthrough()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- must never let the gate fail open
        block(f"Blocked by the Cirro plugin: the safety gate itself failed "
              f"({type(exc).__name__}: {exc}). Refusing rather than allowing an "
              "unchecked operation.\n")

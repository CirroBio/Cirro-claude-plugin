#!/usr/bin/env python3
"""PreToolUse gate for the Cirro plugin.

Stdlib only and no virtualenv, so it stays fast enough to run on every Bash call.

Two jobs:
  1. Deny routes that reach the Cirro SDK without going through the approval gate.
  2. On mcp__cirro__apply, read the plan file itself -- server truth, not the model's
     claim -- and surface it, refusing outright if the plan is not applicable.

It judges execution and never authoring: what a command runs is read from disk -- an
interpreter's script argument, a module, a `source`d or piped file, a bare `./x.py` --
and judged by what is in it. Writing such a file is not running it and does not prompt.

And it judges only sessions that have reached the plugin. Job 1 arms on the first use
of a plugin skill, MCP tool or `cirro-agent` and stays on for the rest of that session;
before that there is no approval to bypass, so there is nothing to protect and the
checks are pure friction for anyone whose project merely depends on the SDK. Job 2 and
the credential check never wait to be armed. See "session arming" below.

Honest about its ceiling: this is string analysis of a shell command. It is
defeated by a base64 payload, by an interpreter it does not know, and by any
route to code it cannot see as text. It makes accidental bypass impossible and
deliberate bypass conspicuous. It is not a sandbox. The real boundary is a
`permissions.deny` rule in the user's own settings, which a plugin cannot ship --
see the README.
"""
# The hook is launched by its shebang, so it runs on whatever `python3` is first on
# PATH -- on a stock macOS that is still 3.9. Without this, the `X | None` annotations
# below are evaluated at definition time and raise TypeError, which Claude Code reports
# as a hook *error* rather than a block: the gate would be silently inert and nothing
# would be gated at all.
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import NoReturn

# How much of one argument's value the approval prompt shows before eliding.
ARG_PREVIEW_CHARS = 160

# The digest contract, the applicability rules, the two lines that head the approval
# text, and the session resolver are imported from the plugin's own modules rather than
# copied here. Both import only stdlib (test_gate asserts it stays that way), so the
# hook still runs with a bare system Python and no virtualenv -- and there is one
# definition of what a plan's integrity covers, one of when a plan may still be
# applied, one of the text a person reads, and one of which folders may be resolved,
# instead of copies kept in step by a test. The gate's own copy of the walk lacked both
# safety rules: it would resolve the machine-wide ~/.cirro and would climb past the
# project root. Its copy of the pre-flight checks had drifted too, allowing five
# seconds past an expiry that the worker refuses.
#
# What is imported is the rule, never the answer: the gate reaches its own verdict from
# its own reading of the plan file and refuses on it, and the worker does the same again
# when apply runs. Neither defers to the other having said yes.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "src"))
from cirro_agent.errors import NoSessionFolder  # noqa: E402
from cirro_agent.plans import (DIGEST_MISMATCH, DIGEST_TAMPERED,  # noqa: E402
                               EXPIRED, NOT_PENDING, PLAN_ID_PATTERN,
                               approval_header, approval_warnings, headline,
                               preflight)
from cirro_agent.runner import plugin_data  # noqa: E402
from cirro_agent.session import find_session_dir  # noqa: E402

# --------------------------------------------------------------- session arming
#
# The bypass checks below judge every Bash call in every session, including ones that
# have nothing to do with Cirro. That made any project depending on the SDK -- this
# repo above all -- unpleasant to work in while the plugin was installed, for no
# safety gained: a session that never reaches the plugin has no approval to bypass.
#
# So they arm. The first use of a plugin skill, MCP tool or `cirro-agent` in a session
# writes a marker, and from then on that session is gated for as long as it lasts.
# What this deliberately does NOT gate is `apply` (a write must be approved the first
# time and every time, armed or not) and credential snooping (it protects secrets
# rather than the approval gate, and never fires in ordinary development).
#
# The marker lives beside the runtime, never inside the plugin root -- a directory
# marketplace copies that tree verbatim into every install.
ARMED_DIR = "armed-sessions"
ARMED_TTL_S = 14 * 24 * 3600
SESSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# Set from the marker in main(); the recursive helpers read it rather than threading a
# parameter through every call site.
_ARMED = True


def _marker(session_id: str) -> Path | None:
    """Where this session's arming is recorded, or None if it cannot be named."""
    if not session_id or not SESSION_ID.match(session_id):
        return None
    return plugin_data() / ARMED_DIR / session_id


def session_is_armed(session_id: str) -> bool:
    """Armed unless this session is positively known not to have used the plugin.

    Fail closed: an event with no usable session_id cannot be tracked, so it is gated
    exactly as it was before arming existed. The alternative -- treating "unknown" as
    unarmed -- would turn the whole gate off for good on any host that does not send
    the field, and it would do it silently.
    """
    path = _marker(session_id)
    if path is None:
        return True
    return path.exists()


def arm_session(session_id: str) -> None:
    """Record that this session has used the plugin. Never fails the gate."""
    path = _marker(session_id)
    if path is None or path.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        _prune(path.parent)
    except OSError:
        # An unwritable data directory must not break the tool call. The cost is that
        # the session stays unarmed, which is the same position it was already in.
        pass


def _prune(directory: Path) -> None:
    """Drop markers for sessions long finished, so this never grows without bound."""
    cutoff = time.time() - ARMED_TTL_S
    try:
        stale = [p for p in directory.iterdir() if p.stat().st_mtime < cutoff]
    except OSError:
        return
    for path in stale:
        try:
            path.unlink()
        except OSError:
            pass


def arms_the_session(tool: str, tool_input: dict) -> bool:
    """Is this tool call a use of the plugin?"""
    if tool.startswith("mcp__") and "cirro" in tool:
        return True
    if tool == "Skill" and "cirro" in str(tool_input.get("skill") or ""):
        return True
    if tool == "Bash":
        return bool(re.search(r"(?:^|[^\w./-])cirro-agent\b",
                              tool_input.get("command") or ""))
    return False


# This layer's wording for each shared pre-flight verdict, for a human reading a
# terminal. A verdict with no entry here raises, which the handler at the bottom turns
# into a block: a refusal the gate cannot phrase is still a refusal.
REFUSALS = {
    NOT_PENDING: ("plan {plan_id} is '{status}', not pending. A plan authorizes exactly "
                  "one change; create a new one."),
    EXPIRED: ("plan {plan_id} has expired. Re-plan so the approval reflects current "
              "figures."),
    DIGEST_TAMPERED: ("plan {plan_id} does not match its own digest; it was modified "
                      "after it was created."),
    DIGEST_MISMATCH: ("the digest supplied for plan {plan_id} does not match the stored "
                      "plan. The plan may have been altered."),
}

# Text that means a command is talking to the Cirro SDK.
SDK_MARKERS = re.compile(
    r"(?:^|[^\w.])(?:import\s+cirro|from\s+cirro\b|cirro\.sdk|cirro_api_client"
    r"|DataPortal|DataPortalLogin|CirroApi"
    # `python -m cirro.cli upload` reaches the upstream CLI without naming an import.
    r"|-m\s+cirro(?:[.\s]|$)|\bcirro\.cli\b)", re.I)

INTERPRETERS = re.compile(
    r"^(?:python[0-9.]*|ipython[0-9.]*|jupyter|uv|uvx|pipx|poetry|conda|hatch|pdm)$",
    re.I)

# What gets read when a command executes a file rather than inline code. The SDK is
# named in the file, not in the command, so `python analyse.py` says nothing on its
# own -- and that was the one bypass route left open.
SHELL_SUFFIXES = (".sh", ".bash", ".zsh")
SCRIPT_SUFFIXES = (".py", ".ipynb") + SHELL_SUFFIXES
SCRIPT_READ_LIMIT = 512 * 1024
# A script that runs a script that runs a script. Bounded so a self-invoking wrapper
# cannot spin the hook past its timeout.
MAX_NESTING = 3

USE_INSTEAD = ("Use instead:\n"
               "  reads   cirro-agent read --help\n"
               "  writes  the plan and apply MCP tools\n")

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

# Wrappers that hand straight through to the program named after them.
BARE_WRAPPERS = ("command", "builtin", "exec", "xargs")

# Sourcing runs the file in the current shell, so the argument is the executed code.
SOURCING = (".", "source")

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

# A backslash before a newline continues one command onto the next line; it does not
# separate two. Both splitters treat a bare newline as a separator, so without this a
# continuation line beginning with a path is read as that path being EXECUTED --
# `sed -i '' \<newline> -e 's/x/y/' \<newline> pkg/mod.py` blocked as though the .py
# were the program. Over-blocking is not a safe direction here: it is what made this
# repo unpleasant to work in with the plugin installed.
CONTINUATION = re.compile(r"\\\n")

# Everything but a pipe, so one pipeline stays whole and its stages can be related to
# each other: the file an interpreter reads from stdin is named in another stage.
PIPELINES = re.compile(r"(?:\|\||&&|[;&\n])")

# The stage separator within one pipeline, split quote-aware like every other operator.
SINGLE_PIPE = re.compile(r"\|")

# A heredoc operator, and the word that ends its body. The operator is located on the
# masked command, so one inside an argument is not an operator; the delimiter is then
# read off the original, where its own quotes are still there to be stripped. `<<<` is
# a here-string, whose operand is on the same line and is not a body at all.
HEREDOC_OPERATOR = re.compile(r"(?<!<)<<-?(?!<)")
HEREDOC_DELIMITER = re.compile(
    r"<<-?\s*(?:'([^']*)'|\"([^\"]*)\"|\\?([A-Za-z_][\w.-]*))")

# shlex expands no parameter, and unlike `$(...)` there is no substitution pass for
# one, so in `PY=python3; $PY drive.py` the program name stayed the literal `$PY`.
ASSIGNMENT = re.compile(r"^([A-Za-z_]\w*)=(.*)$")
PARAMETER = re.compile(r"^\$\{?([A-Za-z_]\w*)\}?$")

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


def unquoted(text: str) -> str:
    """The command with every quoted span blanked, character for character.

    Operators are only operators where the shell would act on them, and a `;` inside
    an argument is part of that argument. Splitting on it read the rest of the string
    as a command of its own, so `git commit -m "parser fix; scripts/gate.py reads
    heredocs"` was blocked as though the path after the semicolon were being run.
    The mask is the same length as the input, so a match's offsets index the original.
    """
    masked = []
    quote = ""
    escaped = False
    for character in text:
        if escaped:
            escaped = False
            masked.append(" ")
        elif quote != "'" and character == "\\":
            escaped = True
            masked.append(" ")
        elif quote:
            masked.append("\n" if character == "\n" else " ")
            if character == quote:
                quote = ""
        elif character in "'\"":
            quote = character
            masked.append(" ")
        else:
            masked.append(character)
    return "".join(masked)


def split_unquoted(text: str, pattern) -> list:
    """Split on `pattern`, but only where it falls outside quotes."""
    parts = []
    last = 0
    for match in pattern.finditer(unquoted(text)):
        parts.append(text[last:match.start()])
        last = match.end()
    parts.append(text[last:])
    return parts


def strip_heredocs(command: str) -> tuple:
    """Separate heredoc bodies from the command lines that carry them.

    A body is a payload, not a run of commands: text written to a file, or a program
    handed to something. Reading it as commands is the authoring/execution confusion
    this gate exists to avoid -- `cat >> test_x.py <<'PY'` whose body merely names a
    .py path was blocked as though the path were being executed.

    Returns the command with the bodies removed, and each body paired with the line
    that consumes it, so a body that really is a program can still be judged.
    """
    lines = command.split("\n")
    kept, bodies = [], []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for operator in HEREDOC_OPERATOR.finditer(unquoted(line)):
            match = HEREDOC_DELIMITER.match(line, operator.start())
            if match is None:
                continue
            delimiter = (match.group(1) or match.group(2) or match.group(3) or "")
            stripped = operator.group(0).endswith("-")
            body = []
            while index < len(lines):
                candidate = lines[index]
                index += 1
                if (candidate.strip() if stripped else candidate.rstrip()) == delimiter:
                    break
                body.append(candidate)
            bodies.append((line, "\n".join(body)))
    return "\n".join(kept), bodies


def consumes_commands(line: str) -> bool:
    """Does this line feed its heredoc body to a shell, which runs every line of it?

    `bash <<'EOF'` and `cat <<'EOF' | sh` do; `cat > notes.md <<'EOF'` writes it. A
    body fed to a Python interpreter is source rather than commands, and the SDK
    markers are searched over the whole command text before any of this.
    """
    for segment in split_unquoted(line, SPLITTERS):
        words = tokens_of(segment)
        if not words:
            continue
        index = program_index(words)
        if index < len(words) and SHELLS.match(Path(words[index]).name or ""):
            return True
    return False


def segments(command: str) -> list:
    """Split into independently-executed commands, including substituted ones."""
    command = CONTINUATION.sub(" ", command)
    extra = [m.group(1) or m.group(2) for m in SUBSTITUTIONS.finditer(command)]
    flattened = SUBSTITUTIONS.sub(" ", command)
    parts = split_unquoted(flattened, SPLITTERS) + extra
    return [s.strip() for s in parts if s and s.strip()]


def tokens_of(segment: str) -> list:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def assignments(command: str) -> dict:
    """`VAR=value` bindings made anywhere in the command."""
    found = {}
    for segment in segments(command):
        for token in tokens_of(segment):
            match = ASSIGNMENT.match(token)
            if match:
                found[match.group(1)] = match.group(2)
    return found


def expand(words: list, values: dict) -> list:
    """Substitute a whole-token `$VAR` from a binding in the same command."""
    expanded = []
    for word in words:
        match = PARAMETER.match(word)
        expanded.append(values.get(match.group(1), word) if match else word)
    return expanded


def program_index(words: list) -> int:
    """Where the real program name starts, past env assignments and wrappers.

    So `env FOO=1 python -c ...` and `timeout 5 python ...` are both seen. Skipping
    only the wrapper name left it pointing at the wrapper's own argument, so
    `timeout 5 python ...` resolved to "5" and matched nothing.
    """
    index = 0
    while index < len(words):
        token = words[index]
        name = Path(token).name
        if "=" in token and not token.startswith("-"):
            index += 1
            continue
        if name in WRAPPERS_WITH_ARG or name in BARE_WRAPPERS:
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
    return index


def module_file(arguments: list, cwd: str) -> str | None:
    """The file `python -m pkg.mod` runs, when the module is one in the directory.

    A module's file is the executed code and is named nowhere else in the command, so
    without this `python3 -m sdkmod` said nothing about what it was about to run.
    """
    name = next((a for a in arguments if not a.startswith("-")), "")
    if not name:
        return None
    stem = name.replace(".", "/")
    root = Path(cwd or ".")
    for candidate in (f"{stem}.py", f"{stem}/__main__.py"):
        if (root / candidate).is_file():
            return candidate
    return None


def redirected_file(arguments: list) -> str | None:
    """The operand of a `< file` redirect, which is not a segment of its own."""
    for offset, token in enumerate(arguments):
        if token == "<":
            return arguments[offset + 1] if offset + 1 < len(arguments) else None
        if len(token) > 1 and token.startswith("<"):
            return token[1:]
    return None


def executed_script(words: list, index: int, program: str, cwd: str = "") -> str | None:
    """The file this segment actually runs, if it runs one.

    Only the file that is executed -- never one that merely appears in the argument
    list, or `pytest test_reads.py` and `flake8 src/mutations.py` would be read as
    running the SDK code they are pointed at.
    """
    if words[index].endswith(SCRIPT_SUFFIXES):
        return words[index]
    if program in SOURCING:
        return next((a for a in words[index + 1:]
                     if not a.startswith("-") and a.endswith(SCRIPT_SUFFIXES)), None)
    if not (INTERPRETERS.match(program) or SHELLS.match(program)):
        return None
    arguments = words[index + 1:]
    # A redirect is not part of the argument list; it only supplies the program when
    # nothing on the command line already did.
    cut = next((i for i, a in enumerate(arguments) if a.startswith("<")),
               len(arguments))
    for offset, argument in enumerate(arguments[:cut]):
        # An inline program: any path after it is that program's own argv, which is
        # why `python -m pytest test_x.py` runs pytest, not test_x.
        if argument == "-c":
            return None
        if argument == "-m":
            return module_file(arguments[offset + 1:cut], cwd)
        if argument.startswith("-"):
            continue
        name = Path(argument).name
        if argument in EXECUTING_SUBCOMMANDS or INTERPRETERS.match(name) \
                or SHELLS.match(name):
            continue
        return argument if argument.endswith(SCRIPT_SUFFIXES) else None
    return redirected_file(arguments[cut:])


def read_script(target: str, cwd: str) -> str | None:
    path = Path(target)
    if not path.is_absolute():
        path = Path(cwd or ".") / path
    try:
        with open(path, "rb") as handle:
            return handle.read(SCRIPT_READ_LIMIT).decode("utf-8", "replace")
    except OSError:
        # Nothing readable at that path: there is no script here to judge, and the
        # command's own text has already been searched for inline SDK use.
        return None


def judge_script(target: str, cwd: str, depth: int) -> None:
    """Judge a file that is about to be executed by what is in it.

    Executing a file that drives the SDK is the same act as `python -c` doing it
    inline, so it gets the same answer. Editing that file is not, and no longer
    prompts. A shell script is a command payload like any other, so it is judged by
    every detector here rather than only by the SDK markers -- otherwise a one-line
    run.sh calling the upstream CLI walks straight through.
    """
    body = read_script(target, cwd) if depth < MAX_NESTING else None
    if body is None:
        return
    if target.endswith(SHELL_SUFFIXES):
        inspect_bash(body, cwd, depth + 1)
    elif SDK_MARKERS.search(body):
        block(f"Blocked by the Cirro plugin: `{target}` drives the Cirro SDK, so "
              "running it would bypass the approval gate on every create, update "
              f"and delete.\n\n{USE_INSTEAD}")


def takes_program_from_stdin(words: list, cwd: str) -> bool:
    """An interpreter handed no program of its own reads one from stdin."""
    index = program_index(words)
    if index >= len(words):
        return False
    program = Path(words[index]).name
    return (bool(INTERPRETERS.match(program))
            and not any(a in ("-c", "-m") for a in words[index + 1:])
            and executed_script(words, index, program, cwd) is None)


def piped_scripts(command: str, values: dict, cwd: str) -> list:
    """Scripts fed to an interpreter rather than named as its argument.

    `cat drive.py | python3` and `echo drive.py | xargs python3` leave the
    interpreter's own stage with no script argument at all, so the segment loop finds
    nothing to read -- while the file named in another stage of the same pipeline is
    the code that is about to run.
    """
    fed = []
    for pipeline in split_unquoted(CONTINUATION.sub(" ", command), PIPELINES):
        stages = [expand(tokens_of(s), values)
                  for s in split_unquoted(pipeline, SINGLE_PIPE)]
        if not any(takes_program_from_stdin(s, cwd) for s in stages):
            continue
        for words in stages:
            if not takes_program_from_stdin(words, cwd):
                fed += [w for w in words if w.endswith(SCRIPT_SUFFIXES)]
    return fed


def inspect_bash(command: str, cwd: str = "", depth: int = 0) -> None:
    # A heredoc puts the payload on later lines, which segment splitting separates
    # from the interpreter that consumes it. So an interpreter anywhere in a command
    # that mentions the SDK anywhere is treated as driving it.
    whole_mentions_sdk = bool(SDK_MARKERS.search(command))

    # Unconditional: this one guards secrets rather than the approval gate, and a
    # command that reads token storage is never incidental to ordinary development.
    # Searched over the raw text, heredoc bodies included, like the SDK markers above.
    if TOKEN_SNOOPING.search(command):
        block("Blocked by the Cirro plugin: this command reads Cirro credential storage "
              "or the machine-wide ~/.cirro. The plugin keeps credentials in a "
              "folder-local session and never exposes token material to the model.\n")

    if not _ARMED:
        return

    # A body handed to a shell is a command payload like a `-c` argument, so it
    # re-enters the parser. A body written to a file is not, and is left alone.
    command, heredocs = strip_heredocs(command)
    for line, body in heredocs:
        if depth < MAX_NESTING and consumes_commands(line):
            inspect_bash(body, cwd, depth + 1)

    values = assignments(command)
    for target in piped_scripts(command, values, cwd):
        judge_script(target, cwd, depth)

    for segment in segments(command):
        words = expand(tokens_of(segment), values)
        if not words:
            continue
        index = program_index(words)
        if index >= len(words):
            continue
        # `.` has no path name of its own, and it is a program: `. ./run.sh`.
        program = Path(words[index]).name or words[index]

        target = executed_script(words, index, program, cwd)
        if target:
            judge_script(target, cwd, depth)

        # A shell's -c payload is itself a command; re-enter the parser on it.
        if SHELLS.match(program):
            for offset, token in enumerate(words[index + 1:], start=index + 1):
                if token == "-c" and offset + 1 < len(words) and depth < MAX_NESTING:
                    inspect_bash(words[offset + 1], cwd, depth + 1)
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
                    f"{USE_INSTEAD}")

        if INTERPRETERS.match(program) and (SDK_MARKERS.search(segment)
                                            or whole_mentions_sdk):
            block(
                f"Blocked by the Cirro plugin: `{program}` is being used to drive the "
                "Cirro SDK directly, which bypasses the approval gate on every create, "
                "update and delete.\n\n"
                f"{USE_INSTEAD}")

        if UPSTREAM_CLI.match(program):
            first = next((w for w in words[index + 1:] if not w.startswith("-")), "")
            if first not in UPSTREAM_CLI_READ_ONLY:
                block(
                    "Blocked by the Cirro plugin: the upstream `cirro` CLI performs "
                    f"uploads, logins and downloads ({first or 'interactive mode'}) with "
                    "no plan and no approval, and writes credentials to the global "
                    "~/.cirro.\n\n"
                    f"{USE_INSTEAD}")

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


# --------------------------------------------------------------------------- apply


def inspect_apply(tool_input: dict, cwd: str) -> None:
    plan_id = tool_input.get("plan_id")
    if not plan_id:
        block("Blocked by the Cirro plugin: apply requires a plan_id from a prior "
              "plan call.\n")
    if not PLAN_ID_PATTERN.match(str(plan_id)):
        # The id becomes a path, so anything else is refused before touching disk.
        block(f"Blocked by the Cirro plugin: {plan_id!r} is not a valid plan id.\n")

    # The same start the tool itself will resolve from: every MCP tool takes
    # `directory`, and one Claude session works across several bound folders. Reading
    # only cwd looked in the wrong .cirro and hard-blocked a plan that exists.
    directory = tool_input.get("directory")
    start = Path(str(directory)).expanduser() if directory else Path(cwd or os.getcwd())
    try:
        session_dir = find_session_dir(start)
    except NoSessionFolder:
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

    # The gate's own verdict, from its own reading of the file: status, expiry, and the
    # digest recomputed over the stored record so an edit that left the digest field
    # alone is still caught. The worker reaches the same verdict again when apply runs.
    refusal = preflight(plan, supplied_digest=tool_input.get("digest"))
    if refusal is not None:
        block("Blocked by the Cirro plugin: "
              + REFUSALS[refusal].format(plan_id=plan_id, status=plan.get("status"))
              + "\n")

    lines = approval_header(plan) + [""]
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
    warnings = approval_warnings(plan)
    if warnings:
        lines += warnings + [""]
    try:
        expires = time.strftime("%H:%M:%SZ", time.gmtime(float(plan["expires_at"])))
    except (KeyError, TypeError, ValueError):
        expires = "an unreadable time"
    lines.append(f"Plan {plan_id} is single-use and expires at {expires}.")
    # Supplied from the stored plan rather than echoed by the model. apply requires
    # the approved text to match the plan byte for byte; sourcing it here means that
    # check costs nothing in context, and the value cannot be the model's paraphrase.
    #
    # `change` is first because the desktop approval dialog lists the arguments in the
    # order they appear here and shows nothing else -- none of the text above reaches
    # the person there, only the model. Everything below it is either an identifier or
    # a value too long to read, so a headline placed last is a headline nobody sees.
    #
    # `change` and `directory` are dropped from the incoming input before being
    # re-added, because re-assigning a key keeps the position of its first appearance
    # -- and for `change` the spread would also let a model-supplied value win on a key
    # whose whole point is that the person reads it from the plan. Every other key
    # keeps the order it arrived in.
    supplied = {key: value for key, value in tool_input.items()
                if key not in ("change", "directory")}
    ordered = {"change": headline(plan),
               **supplied,
               "plan_id": plan_id,
               "digest": plan.get("digest") or "",
               "approved_summary": plan.get("summary") or ""}
    # Last, because it is the longest value in the call and says nothing about what
    # will happen. Sitting between the headline and the plan text, a session folder
    # wraps over several lines in the middle of what a person is trying to read.
    if "directory" in tool_input:
        ordered["directory"] = tool_input["directory"]
    respond("ask", "\n".join(lines), {"updatedInput": ordered})


def main() -> None:
    global _ARMED
    try:
        event = json.load(sys.stdin)
    except ValueError:
        passthrough()

    tool = event.get("tool_name") or ""
    tool_input = event.get("tool_input") or {}
    session_id = str(event.get("session_id") or "")

    # Arming is checked before it is set, so the call that first reaches the plugin is
    # itself allowed through. It is a use of the plugin, not a bypass of it.
    _ARMED = session_is_armed(session_id)
    if arms_the_session(tool, tool_input):
        arm_session(session_id)

    if tool == "Bash":
        inspect_bash(tool_input.get("command") or "", event.get("cwd") or "")
    # Never conditional on arming: an apply is the approval itself.
    if tool.endswith("__apply") and "cirro" in tool:
        inspect_apply(tool_input, event.get("cwd") or "")
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

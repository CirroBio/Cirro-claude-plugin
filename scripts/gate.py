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
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "python" / "src"))
from cirro_agent.errors import NoSessionFolder  # noqa: E402
from cirro_agent.plans import (DIGEST_MISMATCH, DIGEST_TAMPERED,  # noqa: E402
                               EXPIRED, NOT_PENDING, PLAN_ID_PATTERN, also_approves,
                               approval_header, approval_warnings, grant_approves,
                               headline, preflight)
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
SESSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}\Z")

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
    """Record that this session has used the plugin. Never fails the gate.

    Touched on every use, not only the first: markers are pruned by age, so one written
    once and never refreshed disarmed a session that was still running a fortnight on.
    """
    path = _marker(session_id)
    if path is None:
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
        # By path too: `~/.claude/plugins/.../bin/cirro-agent read x` is a use.
        return bool(re.search(r"(?:^|[^\w.-])cirro-agent\b",
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
    r"|-m\s+cirro(?:[.\s]|$)|\bcirro\.cli\b"
    # The plugin's own entry points, reached from inline code rather than `-m`:
    # `from cirro_agent.worker.gated import do_apply` names no SDK at all.
    r"|cirro_agent(?:\.|\s+import\s+)(?:worker|mcp_server)\b)", re.I)

INTERPRETERS = re.compile(
    r"^(?:python[0-9.]*|ipython[0-9.]*|pypy[0-9.]*|jupyter|uv|uvx|pipx|poetry|conda"
    r"|mamba|micromamba|pixi|rye|hatch|pdm)$", re.I)

# The interpreters that read a program from stdin when given none. The package runners
# above do not, so `uv sync` beside a commit message naming the SDK is not a program
# arriving by pipe.
STDIN_INTERPRETERS = re.compile(r"^(?:python[0-9.]*|ipython[0-9.]*|pypy[0-9.]*)$", re.I)

# Options whose next word is their value, not the script: `python3 -W ignore x.py`,
# `uv run --with pandas x.py`. Read as the script, the value ended the search there.
OPTIONS_WITH_VALUE = {"-W", "-X", "--with", "--with-requirements", "--python", "-p",
                      "--env-file", "--project", "--directory", "--extra", "--group",
                      "--index-url", "--from", "--package", "--spec", "-n", "--name",
                      "-e", "--env"}

# The plugin's own entry points. Each acts on whatever request arrives on stdin, `apply`
# included, and takes the request's word for what was approved: the worker reads
# `approved_summary` from it, and the server's `requiresUserInteraction` is honoured by
# the host, not by a client on the other end of a pipe. Run by hand, either one executes
# a plan with no prompt. They are refused by name because their files are not in the
# working directory for `module_file` to read, and the server never names the SDK at
# all. The plugin starts both with subprocess, never over Bash, so this costs it nothing.
#
# Matched on a whole argument, bare or joined to its flag, because every spelling runs
# the module: `-m mod`, `-mmod`, `-Im mod`, `-X dev -m mod`, `-m runpy mod`,
# `uv run --module=mod`. The lookahead admits a redirect glued to the name, which shlex
# leaves in the same token: `-m mod<request.json`.
ENTRY_POINT_ARGUMENT = re.compile(
    r"(?:-[A-Za-z]*m|--module=)?(cirro_agent\.(?:worker(?:\.\w+)*|mcp_server))(?![\w.])")

# Where a plugin declares itself, relative to its root.
MANIFEST = Path(".claude-plugin") / "plugin.json"

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
# `-c` alone, or inside a cluster: `bash -lc '...'` is how a model most often runs a
# login shell, and only a bare `-c` used to be read as handing over a command.
SHELL_COMMAND_OPTION = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")

# Words that run the program named after them, and the options of theirs that take a
# value. Each option is skipped rather than counted, so `timeout -s KILL 5 python`,
# `sudo -u root python` and `env -i python` all reach the program; `timeout` alone
# also takes a duration before it.
WRAPPERS = {"timeout", "nice", "env", "sudo", "stdbuf", "ionice", "setsid", "nohup",
            "time", "command", "builtin", "exec", "xargs"}
WRAPPER_OPTIONS_WITH_VALUE = {"-u", "-g", "-s", "-k", "-n", "-c", "-C", "-I", "-S",
                              "-o", "--signal", "--kill-after", "--user", "--chdir"}

# Shell syntax that runs the command after it rather than being one:
# `for f in x; do python3 "$f"; done` put `do` where the program name was looked for.
SHELL_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!"}

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

# A session folder handed over whole, or the files in it that hold a credential:
# `.cirro`, `.cirro/`, `.cirro/*`, `.cirro/*.dat`, under any directory -- the tenant
# folder every unbound directory falls back to included. A named file that is not a
# credential (`.cirro/session.json`, `.cirro/runs/...`) is not matched.
SESSION_FOLDER_ARGUMENT = re.compile(
    r"(?:^|/)\.cirro/?(?:\*[^/]*)?$|(?:^|/)\.cirro/[^/]*(?:\*|token|\.dat)[^/]*$", re.I)
# Programs that name a session folder without reading what is in it. `rm` stays
# allowed because deleting a session folder is the documented safe reset, and `git`
# because untracking one is the fix for a folder that was committed.
FOLDER_HANDLERS = {"ls", "rm", "rmdir", "cd", "pushd", "du", "stat", "mkdir", "test",
                   "[", "git", "touch"}

# The upstream CLI performs uploads and logins with no plan and no approval.
UPSTREAM_CLI = re.compile(r"^(?:cirro|cirro-cli)$")
UPSTREAM_CLI_READ_ONLY = {"list-projects", "list-datasets", "list-files", "--help",
                          "--version"}

AWS_DATA_MOVERS = {"cp", "sync", "mv", "rm", "rb"}
S5CMD_DATA_MOVERS = {"cp", "sync", "mv", "rm", "pipe"}
# `aws s3api` verbs that write or delete.
S3API_WRITES = re.compile(r"^(?:put|delete|copy|upload|complete|restore)-")
# Cirro storage, named or by its bucket shape. Project buckets are `project-<uuid>`,
# which is how the plugin's own reads show them, so a path the model was handed never
# needed to say "cirro" for the check to have to catch it.
CIRRO_STORAGE = re.compile(
    r"cirro|s3://project-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I)

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


def respond(decision: str, reason: str, extra: dict | None = None,
            notice: str | None = None) -> NoReturn:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    if extra:
        payload["hookSpecificOutput"].update(extra)
    if notice:
        # Shown to the person, not the model -- the one channel a hook has to them
        # when it is not asking anything.
        payload["systemMessage"] = notice
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
        words = shlex.split(segment)
    except ValueError:
        words = segment.split()
    return [w for w in (_ungrouped(word) for word in words) if w]


def _ungrouped(word: str) -> str:
    """A word without the grouping a subshell or brace group glued to it.

    `(python3 drive.py)` split into `(python3` and `drive.py)`, so neither the program
    nor the script was recognised. `{}` is kept: it is find's and xargs' placeholder.
    """
    if word in ("(", ")", "{", "}"):
        return ""
    if word[:1] in "({" and word[1:2] not in ("}", ")"):
        word = word.lstrip("({")
    if word[-1:] in ")}" and not any(c in word for c in "({"):
        word = word.rstrip(")}")
    return word


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
    """Where the real program name starts, past env assignments, shell keywords and
    wrappers with their options.

    So `env FOO=1 python -c ...`, `timeout -s KILL 5 python ...` and
    `for f in x; do python ...` are all seen. This finds the program a segment starts
    with; an interpreter further along -- behind a wrapper this does not know -- is
    found by `code_runners` instead.
    """
    index = 0
    while index < len(words):
        token = words[index]
        name = Path(token).name
        if token in SHELL_KEYWORDS or ("=" in token and not token.startswith("-")):
            index += 1
            continue
        if name in WRAPPERS:
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 2 if words[index] in WRAPPER_OPTIONS_WITH_VALUE else 1
            if name == "timeout" and index < len(words) and \
                    re.fullmatch(r"[\d.]+[smhd]?", words[index] or ""):
                index += 1
            continue
        break
    return index


def code_runners(words: list, index: int) -> list:
    """Every position in a segment that starts an interpreter or a shell: the program,
    and any further along. A wrapper this file does not model -- `arch -arm64`,
    `find -exec`, `micromamba run -n env` -- puts the interpreter where the program
    name is not, and judging only the first program let all of them through."""
    found = [index]
    for offset, word in enumerate(words):
        name = Path(word).name
        if offset != index and (INTERPRETERS.match(name) or SHELLS.match(name)):
            found.append(offset)
    return found


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
    value_next = False
    for offset, argument in enumerate(arguments[:cut]):
        if value_next:
            value_next = False
            continue
        # An inline program: any path after it is that program's own argv, which is
        # why `python -m pytest test_x.py` runs pytest, not test_x.
        if argument == "-c":
            return None
        if argument == "-m":
            return module_file(arguments[offset + 1:cut], cwd)
        if argument in OPTIONS_WITH_VALUE:
            value_next = True
            continue
        if argument.startswith("-"):
            continue
        name = Path(argument).name
        if argument in EXECUTING_SUBCOMMANDS or INTERPRETERS.match(name) \
                or SHELLS.match(name):
            continue
        return argument if argument.endswith(SCRIPT_SUFFIXES) else None
    return redirected_file(arguments[cut:])


def local_path(target: str, cwd: str) -> Path:
    """`target` as the shell would find it: `~`, `$HOME` and `$PWD` expanded, and a
    relative path taken from `cwd`. Unexpanded, `python3 ~/drive.py` named no file."""
    home = str(Path.home())
    for prefix in ("$HOME", "${HOME}"):
        if target == prefix or target.startswith(prefix + "/"):
            target = home + target[len(prefix):]
    for prefix in ("$PWD", "${PWD}"):
        if target == prefix or target.startswith(prefix + "/"):
            target = (cwd or ".") + target[len(prefix):]
    path = Path(target).expanduser()
    return path if path.is_absolute() else Path(cwd or ".") / path


def read_script(target: str, cwd: str) -> str | None:
    path = local_path(target, cwd)
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
    elif TOKEN_SNOOPING.search(body):
        block(f"Blocked by the Cirro plugin: `{target}` reads Cirro credential storage "
              "or the machine-wide ~/.cirro. A script is held to the same rule as a "
              "command.\n")


def server_launcher(target: str, cwd: str) -> bool:
    """Is `target` the file this plugin's manifest starts its MCP server with?

    The launcher is the entry-point refusal's blind spot: it has no suffix, so it is
    never read as a script, and it starts the server as `"$PY" -m ...` with `PY` set by
    a substitution the parser cannot follow. Judging extensionless scripts by content
    is no way round that -- the plugin's own `scripts/setup` imports the SDK to report
    its version. So it is known by identity, through the manifest above it: another
    project's `scripts/mcp-server` is not this one, and this plugin installed under any
    version directory, or checked out anywhere, still is.
    """
    path = local_path(target, cwd)
    if not path.is_file():
        return False
    path = path.resolve()
    root = next((p for p in path.parents if (p / MANIFEST).is_file()), None)
    if root is None:
        return False
    try:
        declared = json.loads((root / MANIFEST).read_text())
    except (OSError, ValueError):
        return False
    servers = declared.get("mcpServers") if isinstance(declared, dict) else None
    if not isinstance(servers, dict):
        return False
    if declared.get("name") != json.loads((PLUGIN_ROOT / MANIFEST).read_text())["name"]:
        return False
    commands = (str(server.get("command") or "") for server in servers.values()
                if isinstance(server, dict))
    return any(Path(command.replace("${CLAUDE_PLUGIN_ROOT}", str(root))).resolve() == path
               for command in commands if command)


LAUNCHER_REFUSAL = (
    "Blocked by the Cirro plugin: `{path}` launches the plugin's own MCP server. Its "
    "approval prompt is shown by Claude Code's connection to it; run by hand, it "
    "executes whatever tool call it is given -- an apply included -- with no prompt "
    "at all.\n\n" + USE_INSTEAD)


def reads_stdin(words: list, index: int, program: str, cwd: str) -> bool:
    """Does the interpreter or shell at `index` take its program from stdin?"""
    arguments = words[index + 1:]
    if SHELLS.match(program):
        if any(SHELL_COMMAND_OPTION.match(a) for a in arguments):
            return False
    elif not STDIN_INTERPRETERS.match(program) or any(
            a in ("-c", "-m", "-V", "-VV", "--version", "-h", "--help")
            for a in arguments):
        return False
    return executed_script(words, index, program, cwd) is None


def takes_program_from_stdin(words: list, cwd: str) -> bool:
    """An interpreter or shell handed no program of its own reads one from stdin."""
    index = program_index(words)
    if index >= len(words):
        return False
    return reads_stdin(words, index, Path(words[index]).name, cwd)


def piped_scripts(command: str, values: dict, cwd: str) -> list:
    """Scripts fed to an interpreter rather than named as its argument.

    `cat drive.py | python3` and `echo drive.py | xargs python3` leave the
    interpreter's own stage with no script argument at all, so the segment loop finds
    nothing to read -- while the file named in another stage of the same pipeline is
    the code that is about to run. A shell reading stdin counts too: `cat run.sh | bash`.
    """
    fed = []
    for pipeline in split_unquoted(CONTINUATION.sub(" ", command), PIPELINES):
        stages = [expand(tokens_of(s), values)
                  for s in split_unquoted(pipeline, SINGLE_PIPE)]
        if not any(takes_program_from_stdin(s, cwd) for s in stages):
            continue
        for words in stages:
            if not takes_program_from_stdin(words, cwd):
                fed += [w for w in words
                        if w.endswith(SCRIPT_SUFFIXES) or server_launcher(w, cwd)]
    return fed


def reads_session_folder(command: str) -> bool:
    """Does a command hand a session folder, or a credential file in one, to a program
    that reads it? `TOKEN_SNOOPING` knew only the literal token file name and the
    machine-wide folder, so `cat .cirro/*` and `tar czf x.tgz .cirro` read the rest."""
    for segment in segments(strip_heredocs(command)[0]):
        words = tokens_of(segment)
        index = program_index(words)
        if index >= len(words) or Path(words[index]).name in FOLDER_HANDLERS:
            continue
        if any(SESSION_FOLDER_ARGUMENT.search(word) for word in words[index:]):
            return True
    return False


def moved_to(here: str, arguments: list) -> str:
    """Where `cd` or `pushd` leaves the shell. Without following it, `cd sub && python3
    only.py` read `only.py` from the wrong directory, found nothing and allowed it."""
    target = next((a for a in arguments if not a.startswith("-")), "~")
    return str(local_path(target, here))


def runs_unseen_code(words: list, index: int, program: str, places: list) -> bool:
    """Code the gate cannot read before it runs: a program arriving on stdin, or a
    script that is not there yet -- written by this same command, a moment earlier."""
    if reads_stdin(words, index, program, places[0]):
        return True
    target = executed_script(words, index, program, places[0])
    return bool(target) and all(read_script(target, place) is None for place in places)


def inspect_bash(command: str, cwd: str = "", depth: int = 0) -> None:
    # Unconditional: this one guards secrets rather than the approval gate, and a
    # command that reads token storage is never incidental to ordinary development.
    # Searched over the raw text, heredoc bodies included.
    if TOKEN_SNOOPING.search(command) or reads_session_folder(command):
        block("Blocked by the Cirro plugin: this command reads Cirro credential storage, "
              "a session folder, or the machine-wide ~/.cirro. The plugin keeps "
              "credentials in a folder-local session and never exposes token material "
              "to the model.\n")

    if not _ARMED:
        return

    # A heredoc puts its payload on later lines, which segment splitting separates from
    # the interpreter that consumes it -- so the SDK named anywhere counts against an
    # interpreter whose code the gate cannot otherwise read. Only that one: applied to
    # every interpreter it blocked `python3 -m pytest` beside a commit message naming
    # the SDK, which is the kind of friction that gets a gate turned off.
    whole_mentions_sdk = bool(SDK_MARKERS.search(command))

    # A body handed to a shell is a command payload like a `-c` argument, so it
    # re-enters the parser. A body written to a file is not, and is left alone.
    command, heredocs = strip_heredocs(command)
    for line, body in heredocs:
        if depth < MAX_NESTING and consumes_commands(line):
            inspect_bash(body, cwd, depth + 1)

    values = assignments(command)
    for target in piped_scripts(command, values, cwd):
        if server_launcher(target, cwd):
            block(LAUNCHER_REFUSAL.format(path=target))
        judge_script(target, cwd, depth)

    here = cwd
    for segment in segments(command):
        words = expand(tokens_of(segment), values)
        if not words:
            continue
        index = program_index(words)
        if index >= len(words):
            continue
        if Path(words[index]).name in ("cd", "pushd"):
            here = moved_to(here, words[index + 1:])
            continue
        # A `cd` inside a subshell does not outlast it, and the parser cannot tell
        # which one this was -- so a script is looked for in both places.
        places = [here] if here == cwd else [here, cwd]
        for start in code_runners(words, index):
            judge_program(words, start, segment, command, places, depth,
                          whole_mentions_sdk)


def judge_program(words: list, index: int, segment: str, command: str, places: list,
                  depth: int, whole_mentions_sdk: bool) -> None:
    """Refuse what the program at `index` would do, if it bypasses the gate."""
    # `.` has no path name of its own, and it is a program: `. ./run.sh`.
    program = Path(words[index]).name or words[index]

    if INTERPRETERS.match(program):
        for argument in words[index + 1:]:
            entry_point = ENTRY_POINT_ARGUMENT.match(argument)
            if entry_point:
                block(
                    f"Blocked by the Cirro plugin: `{entry_point.group(1)}` is the "
                    "plugin's own worker or MCP server. Run by hand, it executes "
                    "whatever request it is given -- an apply included -- with no "
                    "approval prompt.\n\n"
                    f"{USE_INSTEAD}")

    # The program when it is named by path, and the file a shell or `source` is
    # handed: `scripts/mcp-server`, `bash scripts/mcp-server` and
    # `. scripts/mcp-server` all start the server.
    launched = [words[index]] if "/" in words[index] else []
    if SHELLS.match(program) or program in SOURCING:
        launched += [a for a in words[index + 1:] if not a.startswith("-")][:1]
    for path in launched:
        if any(server_launcher(path, place) for place in places):
            block(LAUNCHER_REFUSAL.format(path=path))

    for place in places:
        target = executed_script(words, index, program, place)
        if target:
            judge_script(target, place, depth)

    # A shell's -c payload is itself a command; re-enter the parser on it.
    if SHELLS.match(program):
        for offset, token in enumerate(words[index + 1:], start=index + 1):
            if SHELL_COMMAND_OPTION.match(token) and offset + 1 < len(words) \
                    and depth < MAX_NESTING:
                inspect_bash(words[offset + 1], places[0], depth + 1)
        return

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

    if INTERPRETERS.match(program) and (
            SDK_MARKERS.search(segment)
            or (whole_mentions_sdk and runs_unseen_code(words, index, program, places))):
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

    if moves_storage(program, words[index + 1:]) and CIRRO_STORAGE.search(command):
        block(
            "Blocked by the Cirro plugin: moving data in or out of Cirro storage with "
            "an S3 client bypasses the approval gate and Cirro's own dataset "
            "bookkeeping.\n\n"
            "Use the plan and apply MCP tools instead.\n")


def _verb_after(arguments: list, service: str) -> str:
    if service not in arguments:
        return ""
    after = arguments[arguments.index(service) + 1:]
    return next((w for w in after if not w.startswith("-")), "")


def moves_storage(program: str, arguments: list) -> bool:
    """Is this an S3 client writing, copying or deleting? Which bucket it names is
    checked against the whole command, so a path held in a variable still counts."""
    if program == "aws":
        return (_verb_after(arguments, "s3") in AWS_DATA_MOVERS
                or bool(S3API_WRITES.match(_verb_after(arguments, "s3api"))))
    if program == "s5cmd":
        return next((w for w in arguments if not w.startswith("-")), "") \
            in S5CMD_DATA_MOVERS
    return False


# --------------------------------------------------------------------------- apply


# This plugin's own tool names. An `allow` must not reach a tool of some other server
# that happens to end the same way and mention cirro.
APPLY_GRANTED_TOOL = re.compile(r"^mcp__(?:plugin_cirro_)?cirro__apply_granted$")
GRANT_TOOL = re.compile(r"^mcp__(?:plugin_cirro_)?cirro__grant$")


def stored_plan(tool_input: dict, cwd: str, unverifiable) -> tuple:
    """(plan_id, plan) for an apply call, read and pre-flighted on the gate's own
    reading of the file, or a refusal. `unverifiable(plan_id)` decides -- and never
    returns -- when the session folder cannot be found."""
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
        unverifiable(plan_id)

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
    return plan_id, plan


def inspect_apply(tool_input: dict, cwd: str) -> None:
    # Only a real True grants: the host coerces "false" and "0" to False, so treating
    # them as set here would show a grant line for a call that makes none.
    stop_asking = tool_input.get("stop_asking") is True

    def unverifiable(plan_id):
        # Cannot verify, so do not silently allow: make the human decide -- under a
        # headline the model did not write. A grant is another matter: nothing here
        # could say what it would cover, so it is refused rather than asked about.
        if stop_asking:
            block("Blocked by the Cirro plugin: the session folder holding plan "
                  f"{plan_id} could not be found, so what stop_asking would approve "
                  "cannot be shown. Apply it without stop_asking.\n")
        respond("ask", "The Cirro plugin could not locate the session folder to verify "
                       f"plan {plan_id}. Approve only if you know what this applies.",
                {"updatedInput": {
                    "change": f"UNVERIFIED -- the Cirro plugin could not read plan {plan_id}",
                    **{k: tool_input[k] for k in ("plan_id", "digest", "approved_summary",
                                                  "directory") if k in tool_input}}})

    plan_id, plan = stored_plan(tool_input, cwd, unverifiable)

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
    # A grant asked for alongside this change is stated from the plan's own scope, which
    # is inside its digest, and refused here when the plan has none -- so nobody is asked
    # to approve a grant the worker would then refuse.
    also = {}
    if stop_asking:
        scope = plan.get("grant_scope")
        if not isinstance(scope, dict):
            block(f"Blocked by the Cirro plugin: this {plan.get('op')} cannot be approved "
                  "in advance, so apply cannot take stop_asking for it. Call apply "
                  "without it.\n")
        also = {"also_approves": also_approves(scope), "stop_asking": True}
        lines.append(f"Also approves {also['also_approves']}.")
    # Supplied from the stored plan rather than echoed by the model. apply requires
    # the approved text to match the plan byte for byte; sourcing it here means that
    # check costs nothing in context, and the value cannot be the model's paraphrase.
    #
    # `change` is first because the desktop approval dialog lists the arguments in the
    # order they appear here and shows nothing else -- none of the text above reaches
    # the person there, only the model. Everything below it is either an identifier or
    # a value too long to read, so a headline placed last is a headline nobody sees.
    #
    # Rebuilt from the keys apply takes rather than from what arrived: any other key
    # the model added would sit in the dialog right under the headline, saying
    # whatever it liked, and the server would ignore it.
    ordered = {"change": headline(plan), **also,
               "plan_id": plan_id,
               "digest": plan.get("digest") or "",
               "approved_summary": plan.get("summary") or ""}
    # Last, because it is the longest value in the call and says nothing about what
    # will happen. Sitting between the headline and the plan text, a session folder
    # wraps over several lines in the middle of what a person is trying to read.
    if "directory" in tool_input:
        ordered["directory"] = tool_input["directory"]
    respond("ask", "\n".join(lines), {"updatedInput": ordered})


def inspect_apply_granted(tool_input: dict, cwd: str) -> None:
    """Spare a granted apply the host's ordinary permission prompt, and tell the person.

    The grant was the approval, given once through its own prompt, and it is held and
    checked where this hook cannot see: in the MCP server's memory, and by the worker
    against the claimed plan. So allowing here grants nothing -- a call no grant
    covers is refused before its plan is claimed -- while asking here would make the
    grant spare the person nothing.

    What it must not do is run unseen. Whether the model shows the plan is up to the
    model, and a subagent's transcript is not shown at all, so the change's headline
    goes to the person as a notice; a plan this hook cannot read gets the ordinary
    prompt instead.
    """
    def unverifiable(plan_id):
        respond("ask", "The Cirro plugin could not locate the session folder to read "
                       f"plan {plan_id}, so it cannot show what would run unprompted.")

    plan_id, plan = stored_plan(tool_input, cwd, unverifiable)
    respond("allow", f"Plan {plan_id} runs only if a standing grant this session holds "
                     "covers it; the Cirro server refuses it otherwise.",
            notice=f"Cirro, approved in advance this session: {headline(plan)}")


def inspect_grant(tool_input: dict) -> None:
    """Put what a grant approves into its own prompt, in words the model did not write.

    The prompt shows the call's arguments, and `ops` and `project` alone say nothing
    about lasting the session or skipping prompts. The server refuses a grant whose
    `approves` is not exactly this text, so without the hook it is still shown.
    """
    ordered = {"approves": grant_approves(tool_input.get("ops"), tool_input.get("project")),
               **{k: tool_input[k] for k in ("ops", "project", "directory")
                  if k in tool_input}}
    respond("ask", f"Approves {ordered['approves']}.", {"updatedInput": ordered})


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
    # itself allowed through. It is a use of the plugin, not a bypass of it -- except a
    # Bash command, which can carry a bypass beside the use: `cirro-agent read x &&
    # python3 drive.py` passed as the call that armed. So that one is judged armed.
    _ARMED = session_is_armed(session_id)
    if arms_the_session(tool, tool_input):
        arm_session(session_id)
        _ARMED = _ARMED or tool == "Bash"

    if tool == "Bash":
        inspect_bash(tool_input.get("command") or "", event.get("cwd") or "")
    # Never conditional on arming: an apply is the approval itself.
    if tool.endswith("__apply") and "cirro" in tool:
        inspect_apply(tool_input, event.get("cwd") or "")
    if APPLY_GRANTED_TOOL.match(tool):
        inspect_apply_granted(tool_input, event.get("cwd") or "")
    if GRANT_TOOL.match(tool):
        inspect_grant(tool_input)
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

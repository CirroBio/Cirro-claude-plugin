"""The PreToolUse gate. Table-driven over a bypass corpus."""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[2] / "scripts" / "gate.py"

# The gate resolves a signed-in tenant folder under `plugin_data()`, and every test
# here runs it as a subprocess -- where conftest's in-process redirect of
# `tenants_root` cannot reach. Left on the developer's own data directory, two tests
# flip from ask to deny the moment that developer signs in to a tenant, which is the
# machine-dependence that fixture exists to prevent. CI only stayed green because it
# points the variable at a fresh runner temp directory.
ISOLATED_DATA = Path(tempfile.mkdtemp(prefix="cirro-gate-tests-"))

ALLOW, ASK, DENY = "allow", "ask", "deny"


def decide(event: dict, env: dict | None = None) -> tuple:
    """The gate's decision and the text the human would be shown."""
    child = dict(os.environ)
    # It bounds the session resolver's upward walk, so a test that cares sets it.
    child.pop("CLAUDE_PROJECT_DIR", None)
    child["CIRRO_AGENT_PLUGIN_DATA"] = str(ISOLATED_DATA)
    child.update(env or {})
    done = subprocess.run([sys.executable, str(GATE)], input=json.dumps(event),
                          capture_output=True, text=True, timeout=30, env=child)
    if done.returncode == 2:
        return DENY, done.stderr
    assert done.returncode == 0, done.stderr
    if not done.stdout.strip():
        return ALLOW, ""
    output = json.loads(done.stdout)["hookSpecificOutput"]
    return output["permissionDecision"], output["permissionDecisionReason"]


def run(event: dict) -> str:
    return decide(event)[0]


def bash(command: str, cwd: Path | None = None) -> dict:
    event = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        event["cwd"] = str(cwd)
    return event


BYPASS_ATTEMPTS = [
    'python3 -c "import cirro; cirro.DataPortal()"',
    "python -c 'from cirro import DataPortal'",
    'python3 <<EOF\nfrom cirro import DataPortal\nEOF',
    'cat <<EOF | python3\nimport cirro\nEOF',
    'env FOO=1 python3 -c "import cirro"',
    'ls -la && python3 -c "from cirro.sdk.portal import DataPortal"',
    'ipython -c "import cirro_api_client"',
    "uv run --with cirro script.py",
    "uvx cirro upload -p X",
    "cirro upload --project X --name Y",
    "cirro configure",
    "cirro download -p X -d Y",
    "cirro-cli upload -p X",
    "cat .cirro/abc.token.dat",
    "security find-generic-password -s cirro-client -w",
    "cat ~/.cirro/config.ini",
    "aws s3 cp s3://cirro-project-data/x .",
    "aws s3 sync . s3://cirro-project-data/x",
    "cirro-agent apply --plan p1",
    "cirro-agent plan run_analysis",
]

ORDINARY_WORK = [
    "ls -la && grep -rn foo .",
    "git commit -m 'x' && git push",
    "python3 -m pytest tests/ -q",
    "npm test",
    "pip install cirro",
    "uv sync --locked --project runtime",
    "cirro-agent read datasets -p 'My Project'",
    "cirro-agent read --help",
    "cirro list-projects",
    "cirro list-datasets -p X",
    "echo hello | wc -l",
    "make build && ./bin/run",
]


@pytest.mark.parametrize("command", BYPASS_ATTEMPTS, ids=lambda c: c[:40])
def test_bypass_routes_are_denied(command):
    assert run(bash(command)) == DENY


@pytest.mark.parametrize("command", ORDINARY_WORK, ids=lambda c: c[:40])
def test_ordinary_work_is_not_obstructed(command):
    assert run(bash(command)) == ALLOW


def test_editing_sdk_code_is_not_obstructed():
    """Editing a file is not running it.

    The gate used to ask on every Write or Edit of a .py mentioning the SDK, which
    made a project that uses cirro -- including this repo -- unpleasant to work in
    while the plugin was installed, and stopped nothing: `python that_file.py` was
    allowed. Enforcement lives at execution instead.
    """
    assert run({"tool_name": "Write",
                "tool_input": {"file_path": "/tmp/a.py",
                               "content": "from cirro import DataPortal\n"}}) == ALLOW
    assert run({"tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/a.py",
                               "new_string": "DataPortal().list_projects()\n"}}) == ALLOW


# --- executing a script that drives the SDK ---------------------------------

def _script(tmp_path, name, body):
    target = tmp_path / name
    target.write_text(body)
    return target


SDK_SCRIPT = "from cirro import DataPortal\nDataPortal().list_projects()\n"


@pytest.mark.parametrize("template", [
    "python3 {path}",
    "python {path} --project X",
    "{path}",
    "uv run {path}",
    "uv run python {path}",
    "env FOO=1 python3 {path}",
    "ls && python3 {path}",
    'bash -c "python3 {path}"',
    # An interpreter fed the file instead of given it as an argument: the segment has
    # no script in its argv at all, and the file is named in the pipeline's other
    # stage or in a redirect that is not a segment of its own.
    "cat {path} | python3",
    "python3 - < {path}",
    "python3 <{path}",
    "echo {path} | xargs python3",
    # shlex expands no parameter, so the program name stayed the literal `$PY`.
    "PY=python3; $PY {path}",
])
def test_running_a_script_that_drives_the_sdk_is_denied(tmp_path, template):
    path = _script(tmp_path, "analyse.py", SDK_SCRIPT)
    assert run(bash(template.format(path=path))) == DENY


def test_a_module_is_the_code_that_runs(tmp_path):
    """`python -m mod` names no file; the module's own file is what executes."""
    _script(tmp_path, "sdkmod.py", SDK_SCRIPT)
    (tmp_path / "pkg").mkdir()
    _script(tmp_path, "pkg/__main__.py", SDK_SCRIPT)
    assert run(bash("python3 -m sdkmod", cwd=tmp_path)) == DENY
    assert run(bash("python3 -m pkg", cwd=tmp_path)) == DENY
    # A module that is not a file in the directory is somebody else's program, and
    # the paths after it are its argv rather than the code being run.
    assert run(bash("python3 -m pytest sdkmod.py -q", cwd=tmp_path)) == ALLOW


def test_running_a_shell_script_that_drives_the_cli_is_denied(tmp_path):
    path = _script(tmp_path, "run.sh", "#!/bin/sh\ncirro upload -p X -n Y\n")
    assert run(bash(f"bash {path}")) == DENY
    assert run(bash(str(path))) == DENY
    # Sourcing it runs it in the current shell, which is the same act.
    assert run(bash(f"source {path}")) == DENY
    assert run(bash(f". {path}")) == DENY
    assert run(bash(f"zsh -c 'source {path}'")) == DENY


@pytest.mark.parametrize("tail", [
    'python3 -c "import cirro"',
    "cirro upload --project X --name Y",
    "cirro-agent apply --plan p1",
    "aws s3 cp s3://cirro-project-data/x .",
])
def test_a_shell_wrapper_does_not_disable_the_rest_of_the_command(tmp_path, tail):
    """inspect_bash used to exit the process with "allow" at the end of its own body.

    Called recursively on a shell script's body or a `-c` payload, that nested call
    ended the whole hook, so every segment after the first shell wrapper went
    uninspected: one benign wrapper turned off every detector below it.
    """
    benign = _script(tmp_path, "benign.sh", "#!/bin/sh\ntrue\n")
    assert run(bash(f"bash {benign} && {tail}")) == DENY
    assert run(bash(f'bash -c "true" && {tail}')) == DENY


def test_a_script_naming_no_sdk_is_allowed(tmp_path):
    path = _script(tmp_path, "plot.py", "import pandas\nprint(1)\n")
    assert run(bash(f"python3 {path}")) == ALLOW


def test_pointing_a_tool_at_sdk_code_is_not_running_it(tmp_path):
    """The distinction the old Write matcher could not make.

    pytest, flake8 and grep all take paths to files full of SDK imports. Reading the
    argument list rather than what is executed would block the repo's own test suite.
    """
    path = _script(tmp_path, "test_reads.py", SDK_SCRIPT)
    for command in (f"python3 -m pytest {path}", f"pytest {path}",
                    f"flake8 {path}", f"grep -n DataPort '{path}'",
                    f"cat {path}", f"git add {path}"):
        assert run(bash(command)) == ALLOW, command


def test_a_missing_script_path_is_not_an_error(tmp_path):
    assert run(bash(f"python3 {tmp_path / 'gone.py'}")) == ALLOW


def test_a_wrapper_script_does_not_launder_the_one_underneath(tmp_path):
    inner = _script(tmp_path, "analyse.py", SDK_SCRIPT)
    outer = _script(tmp_path, "run.sh", f"#!/bin/sh\nset -e\npython3 {inner}\n")
    assert run(bash(f"bash {outer}")) == DENY


def test_a_self_invoking_script_still_answers(tmp_path):
    """The nesting bound: a cycle must not spin the hook past its timeout."""
    path = tmp_path / "loop.sh"
    path.write_text(f"#!/bin/sh\nbash {path}\n")
    started = time.monotonic()
    assert run(bash(f"bash {path}")) == ALLOW
    assert time.monotonic() - started < 10


# --- the apply gate ---

@pytest.fixture
def workdir(tmp_path, monkeypatch):
    from cirro_agent import session as session_mod
    work = tmp_path / "proj"
    work.mkdir()
    monkeypatch.setenv("CIRRO_HOME", str(work / ".cirro"))
    return session_mod.init(work, "app.cirro.bio")


def apply_event(workdir, **tool_input) -> dict:
    return {"tool_name": "mcp__cirro__apply", "cwd": str(workdir.path.parent),
            "tool_input": tool_input}


def make_plan(workdir, **kwargs):
    from cirro_agent import plans
    kwargs.setdefault("op", "delete_dataset")
    kwargs.setdefault("args", {"dataset_id": "ds_9"})
    kwargs.setdefault("summary", "DELETE ds_9")
    kwargs.setdefault("username", "sminot")
    return plans.create(workdir, **kwargs)


def test_a_valid_plan_asks_and_shows_the_plan(workdir):
    plan = make_plan(workdir, risk="cirro_destructive")
    done = subprocess.run([sys.executable, str(GATE)],
                          input=json.dumps(apply_event(workdir, plan_id=plan.plan_id,
                                                       digest=plan.digest)),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0
    output = json.loads(done.stdout)["hookSpecificOutput"]
    assert output["permissionDecision"] == ASK
    reason = output["permissionDecisionReason"]
    assert "app.cirro.bio" in reason and "sminot" in reason
    assert "delete_dataset" in reason and "cirro_destructive" in reason
    assert "DELETE ds_9" in reason
    assert output["additionalContext"] == reason


def test_apply_without_a_plan_id_is_denied(workdir):
    assert run(apply_event(workdir)) == DENY


def test_a_nonexistent_plan_is_denied(workdir):
    assert run(apply_event(workdir, plan_id="plan_nope")) == DENY


def test_a_tampered_digest_is_denied(workdir):
    plan = make_plan(workdir)
    assert run(apply_event(workdir, plan_id=plan.plan_id, digest="deadbeef")) == DENY


def test_an_expired_plan_is_denied(workdir):
    from cirro_agent import plans
    plan = make_plan(workdir)
    plan.expires_at = time.time() - 3600
    plans._save(workdir, plan)
    assert run(apply_event(workdir, plan_id=plan.plan_id, digest=plan.digest)) == DENY


def test_an_already_applied_plan_is_denied(workdir):
    from cirro_agent import plans
    plan = make_plan(workdir)
    plan.status = plans.DONE
    plans._save(workdir, plan)
    assert run(apply_event(workdir, plan_id=plan.plan_id, digest=plan.digest)) == DENY


def test_the_gate_finds_the_session_from_a_subdirectory(workdir):
    plan = make_plan(workdir)
    deep = workdir.path.parent / "a" / "b"
    deep.mkdir(parents=True)
    event = {"tool_name": "mcp__cirro__apply", "cwd": str(deep),
             "tool_input": {"plan_id": plan.plan_id, "digest": plan.digest}}
    assert run(event) == ASK


def test_the_gate_reads_the_session_from_the_directory_argument(workdir, tmp_path):
    """Every MCP tool takes `directory`, and one Claude session works across several
    bound folders. Resolving from cwd alone looked in the wrong .cirro: an apply whose
    directory names the folder holding the plan was hard-blocked as nonexistent."""
    plan = make_plan(workdir)
    unbound = tmp_path / "unbound"
    unbound.mkdir()
    for cwd in (unbound, tmp_path):
        decision, reason = decide(
            {"tool_name": "mcp__cirro__apply", "cwd": str(cwd),
             "tool_input": {"plan_id": plan.plan_id, "digest": plan.digest,
                            "directory": str(workdir.path.parent)}})
        assert decision == ASK
        assert "delete_dataset" in reason, "the plan itself must have been read"


def _global_credential(home: Path):
    """A session folder at ~/.cirro, holding a plan, as the SDK's default would be."""
    from cirro_agent import plans, session as session_mod
    home.mkdir(parents=True)
    session = session_mod.init(home, "app.cirro.bio")
    plan = plans.create(session, op="delete_dataset", args={"dataset_id": "ds_9"},
                        summary="DELETE ds_9", username="sminot")
    return plan


def test_the_gate_refuses_the_machine_wide_credential_folder(tmp_path):
    """The gate carried its own weaker copy of the resolver, with neither safety rule:
    from any directory under $HOME it resolved the machine-wide ~/.cirro and read plans
    out of the credential every tool on the machine shares."""
    home = tmp_path / "home"
    plan = _global_credential(home)
    work = home / "work"
    work.mkdir()
    decision, reason = decide(
        {"tool_name": "mcp__cirro__apply", "cwd": str(work),
         "tool_input": {"plan_id": plan.plan_id, "digest": plan.digest}},
        {"HOME": str(home)})
    assert decision == ASK
    assert "could not locate the session folder" in reason
    assert "delete_dataset" not in reason


def test_the_gate_does_not_climb_past_the_project_root(tmp_path):
    """The other rule the gate's copy lacked: the walk stops at the project root."""
    plan = _global_credential(tmp_path / "above")
    repo = tmp_path / "above" / "repo"
    repo.mkdir()
    decision, reason = decide(
        {"tool_name": "mcp__cirro__apply", "cwd": str(repo),
         "tool_input": {"plan_id": plan.plan_id, "digest": plan.digest}},
        {"CLAUDE_PROJECT_DIR": str(repo)})
    assert decision == ASK
    assert "could not locate the session folder" in reason


def test_a_malformed_plan_id_is_refused_before_touching_disk(tmp_path):
    """The id becomes a path, so it is validated by shape first."""
    event = {"tool_name": "mcp__cirro__apply", "cwd": str(tmp_path),
             "tool_input": {"plan_id": "plan_x"}}
    assert run(event) == DENY


def test_unverifiable_plans_ask_rather_than_silently_allowing(tmp_path):
    """A well-formed id whose session folder cannot be found is unverifiable, so the
    human decides rather than the gate waving it through."""
    event = {"tool_name": "mcp__cirro__apply", "cwd": str(tmp_path),
             "tool_input": {"plan_id": "plan_1780000000_abcdef01"}}
    assert run(event) == ASK


def test_the_gate_ignores_unrelated_tools():
    assert run({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}) == ALLOW


def test_malformed_input_does_not_block_the_session():
    done = subprocess.run([sys.executable, str(GATE)], input="not json",
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0


def test_the_gate_needs_no_virtualenv():
    """It runs on every Bash call, so it must be stdlib-only and fast.

    Checked over the parsed import statements rather than by substring: the module
    legitimately quotes commands like `python -c 'import cirro'` in its patterns and
    comments, and a text search cannot tell those from a real import.
    """
    tree = ast.parse(GATE.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] == "cirro_agent":
                # The gate imports its digest contract from the plugin's own plans
                # module. That is allowed only while plans stays stdlib-only, which
                # the check below proves with a bare interpreter.
                continue
            imported.add(node.module.split(".")[0])
    third_party = imported - set(sys.stdlib_module_names)
    assert not third_party, (
        f"the gate must import only the standard library; found {sorted(third_party)}")


def test_the_gate_catches_a_plan_edited_on_disk(workdir):
    plan = make_plan(workdir)
    path = workdir.path / "plans" / f"{plan.plan_id}.json"
    stored = json.loads(path.read_text())
    stored["args"]["dataset_id"] = "ds_something_else"
    path.write_text(json.dumps(stored))
    assert run(apply_event(workdir, plan_id=plan.plan_id, digest=plan.digest)) == DENY


def test_a_line_continuation_is_one_command_not_two(tmp_path):
    """A backslash before a newline continues a command; it does not separate two.
    Both splitters treated a bare newline as a separator, so a continuation line
    starting with a path made that path look like the program being executed --
    `sed -i '' \\<nl> -e 's/a/b/' \\<nl> pkg/mod.py` was denied as though the .py file
    were being run. Editing a file is not executing it."""
    driver = tmp_path / "mod.py"
    driver.write_text("from cirro import DataPortal\n")

    edit = f"sed -i '' \\\n -e 's/a/b/' \\\n {driver.name}"
    assert decide(bash(edit, cwd=str(tmp_path)))[0] == ALLOW
    assert decide(bash(f"{edit} && sed -n '1p' {driver.name}",
                       cwd=str(tmp_path)))[0] == ALLOW

    # The same continuation must not become a way to hide a real execution.
    hidden = f"echo one \\\n && python3 {driver.name}"
    assert decide(bash(hidden, cwd=str(tmp_path)))[0] == DENY
    assert decide(bash(f"echo one \\\n && bash -c 'python3 {driver.name}'",
                       cwd=str(tmp_path)))[0] == DENY


def test_the_gate_uses_the_one_digest_implementation(workdir):
    """The gate used to carry its own copy of DIGEST_FIELDS and the sha256 over them,
    held in step with plans.py by a test. It now delegates the whole applicability
    question to `plans.preflight`, so it names no digest primitive at all: the checks
    are that it imports the shared predicate, computes nothing of its own, and -- run
    as the hook is run, with a bare interpreter -- still asks on a good plan.

    `test_a_tampered_digest_is_denied` and `test_an_expired_plan_is_denied` are the
    teeth; this test only stops a second implementation growing back beside them."""
    plan = make_plan(workdir)
    assert run(apply_event(workdir, plan_id=plan.plan_id, digest=plan.digest)) == ASK
    source = GATE.read_text()
    assert "from cirro_agent.plans import" in source
    assert "preflight" in source, "the gate must ask the shared predicate, not its own"
    assert "hashlib" not in source, "a second digest implementation crept back in"
    assert "DIGEST_FIELDS = (" not in source, "a second copy of the field list crept back in"
    assert "canonical_digest(" not in source, (
        "the gate should reach the digest only through plans.preflight, so there is "
        "one place that decides what a plan's integrity covers")


def test_the_approval_text_shows_the_real_operation_and_arguments(workdir):
    """A forged plan cannot be hidden behind friendly prose: the prompt is rendered
    from op and args, so approving it means seeing what actually runs."""
    plan = make_plan(workdir, op="delete_dataset",
                     args={"project_id": "proj_a", "dataset_id": "D-VICTIM"},
                     summary="Routine tidy-up.")
    done = subprocess.run([sys.executable, str(GATE)],
                          input=json.dumps(apply_event(workdir, plan_id=plan.plan_id,
                                                       digest=plan.digest)),
                          capture_output=True, text=True, timeout=30)
    reason = json.loads(done.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "delete_dataset" in reason
    assert "D-VICTIM" in reason, "the real target must be visible, not just the prose"


def test_the_modules_the_gate_imports_are_stdlib_only():
    """Importing them under a system Python with no cirro installed must load nothing
    but the plugin's own pure modules -- that is what lets the gate run with no venv."""
    code = ("import sys; sys.path.insert(0, 'python/src'); import cirro_agent.plans; "
            "import cirro_agent.session; import cirro_agent.runner; "
            "bad = [m for m in sys.modules if m.startswith('cirro') and "
            "not m.startswith('cirro_agent')]; assert not bad, bad; "
            "print(sorted(m for m in sys.modules if m.startswith('cirro_agent')))")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=GATE.parents[1])
    assert done.returncode == 0, done.stderr[-600:]
    assert "cirro_agent.plans" in done.stdout
    assert "cirro_agent.session" in done.stdout
    # runner joined the list when arming began resolving the data directory through
    # plugin_data() rather than growing a third copy of that rule.
    assert "cirro_agent.runner" in done.stdout


def test_the_gate_defines_itself_on_the_oldest_python_it_can_be_launched_with():
    """hooks.json launches the gate by its shebang, so it runs on whatever `python3`
    is first on PATH -- 3.9.6 on a stock macOS. PEP 604 annotations are evaluated at
    definition time, so without `from __future__ import annotations` the file raises
    TypeError before main() runs. Claude Code reports that as a hook *error*, not a
    block, so the gate would be silently inert and nothing would be gated."""
    source = GATE.read_text()
    body = source.split('"""', 2)[2]
    assert "from __future__ import annotations" in body, (
        "the gate uses `X | None` annotations and must defer their evaluation")
    compile(source, str(GATE), "exec")

    oldest = Path("/usr/bin/python3")
    if not oldest.exists():
        return
    done = subprocess.run([str(oldest), str(GATE)],
                          input=json.dumps({"tool_name": "Bash",
                                            "tool_input": {"command": "echo hi"},
                                            "cwd": "/tmp"}),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[-600:]
    assert "TypeError" not in done.stderr


def test_a_chain_approval_names_every_step_before_the_argument_dump(workdir):
    """The prompt renders from args, and a chain's args are one list of dicts -- a
    single 160-character preview could push an op past the edge. Each step is named."""
    plan = make_plan(workdir, op="chain", args={"steps": [
        {"label": "upload", "op": "upload_dataset", "args": {"source_dir": "/d"}},
        {"label": "run", "op": "run_analysis", "args": {"name": "x" * 300}},
    ], "carried": [{"dataset_id": "ds_1"}]})
    done = subprocess.run([sys.executable, str(GATE)],
                          input=json.dumps(apply_event(workdir, plan_id=plan.plan_id,
                                                       digest=plan.digest)),
                          capture_output=True, text=True, timeout=30)
    payload = json.loads(done.stdout)["hookSpecificOutput"]
    assert payload["permissionDecision"] == "ask"
    reason = payload["permissionDecisionReason"]
    assert "Runs 2 step(s) in order:" in reason
    assert "2. upload_dataset  [upload]" in reason
    assert "3. run_analysis  [run]" in reason
    assert "reusing 1 earlier step(s)" in reason


# --- the hook registration ---

PLUGIN_ROOT = GATE.parents[1]


def apply_tool_names() -> list:
    """Every name Claude Code can give the apply tool.

    A plugin's MCP tools are namespaced mcp__plugin_<plugin>_<server>__<tool>; the
    bare mcp__<server>__<tool> spelling is what a hand-configured server gets.
    """
    plugin = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
    servers = plugin["mcpServers"]
    return ([f"mcp__{server}__apply" for server in servers]
            + [f"mcp__plugin_{plugin['name']}_{server}__apply" for server in servers])


@pytest.mark.parametrize("tool_name", apply_tool_names())
def test_the_hook_matcher_covers_every_spelling_of_apply(tool_name):
    """The matcher was the literal mcp__cirro__apply, which the namespaced name never
    equals, so the hook never fired: apply received no approved_summary from the plan
    and rejected every call until the summary was retyped by hand."""
    registered = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
    matchers = [entry["matcher"] for entry in registered["hooks"]["PreToolUse"]]
    assert any(re.search(matcher, tool_name) for matcher in matchers), \
        f"no PreToolUse matcher in hooks.json fires for {tool_name}"


@pytest.mark.parametrize("tool_name", apply_tool_names())
def test_every_spelling_of_apply_reaches_the_apply_gate(workdir, tool_name):
    plan = make_plan(workdir)
    assert run({"tool_name": tool_name, "cwd": str(workdir.path.parent),
                "tool_input": {"plan_id": plan.plan_id,
                               "digest": plan.digest}}) == ASK


# --- arming: a session that never touches the plugin is not gated ---

SDK_CALL = 'python3 -c "import cirro; cirro.DataPortal()"'


def armed_env(tmp_path):
    return {"CIRRO_AGENT_PLUGIN_DATA": str(tmp_path / "data")}


def sess(event, session_id="sess-a"):
    return dict(event, session_id=session_id)


def test_an_untouched_session_may_run_the_sdk(tmp_path):
    """The bypass checks judged every Bash call in every session, so any project that
    depends on the SDK was unpleasant to work in for no safety gained: a session that
    never reaches the plugin has no approval to bypass."""
    assert decide(sess(bash(SDK_CALL)), armed_env(tmp_path))[0] == ALLOW


def test_using_a_cirro_skill_arms_the_session(tmp_path):
    env = armed_env(tmp_path)
    assert decide(sess({"tool_name": "Skill",
                        "tool_input": {"skill": "cirro:cirro-explore"}}), env)[0] == ALLOW
    assert decide(sess(bash(SDK_CALL)), env)[0] == DENY


def test_using_a_cirro_mcp_tool_arms_the_session(tmp_path):
    env = armed_env(tmp_path)
    assert decide(sess({"tool_name": "mcp__cirro__plan",
                        "tool_input": {}}), env)[0] == ALLOW
    assert decide(sess(bash(SDK_CALL)), env)[0] == DENY


def test_running_cirro_agent_arms_the_session(tmp_path):
    env = armed_env(tmp_path)
    assert decide(sess(bash("cirro-agent read whoami")), env)[0] == ALLOW
    assert decide(sess(bash(SDK_CALL)), env)[0] == DENY


def test_arming_is_per_session(tmp_path):
    env = armed_env(tmp_path)
    decide(sess({"tool_name": "Skill", "tool_input": {"skill": "cirro:cirro-explore"}},
                "sess-a"), env)
    assert decide(sess(bash(SDK_CALL), "sess-a"), env)[0] == DENY
    assert decide(sess(bash(SDK_CALL), "sess-b"), env)[0] == ALLOW


def test_an_unidentifiable_session_is_gated(tmp_path):
    """Fail closed. Treating "unknown" as unarmed would switch the gate off for good,
    and silently, on any host that does not send session_id."""
    assert decide(bash(SDK_CALL), armed_env(tmp_path))[0] == DENY
    assert decide(sess(bash(SDK_CALL), ""), armed_env(tmp_path))[0] == DENY
    assert decide(sess(bash(SDK_CALL), "../escape"), armed_env(tmp_path))[0] == DENY


def test_a_skill_from_another_plugin_does_not_arm(tmp_path):
    env = armed_env(tmp_path)
    assert decide(sess({"tool_name": "Skill",
                        "tool_input": {"skill": "docx"}}), env)[0] == ALLOW
    assert decide(sess(bash(SDK_CALL)), env)[0] == ALLOW


def test_credential_snooping_is_blocked_even_unarmed(tmp_path):
    """This one guards secrets rather than the approval gate, and never fires in
    ordinary development, so it does not wait to be armed."""
    # Assembled from parts so this file does not itself trip the pattern under test.
    snoop = "cat {}/.{}/{}.dat".format("~", "cirro", "token")
    assert decide(sess(bash(snoop)), armed_env(tmp_path))[0] == DENY


def test_the_dialog_leads_with_the_headline(workdir):
    """The desktop approval dialog renders this call's arguments and nothing else --
    not the reason above, which reaches only the model. So the readable line has to be
    an argument, and the order is what a person reads top to bottom: the headline
    first, then the plan text, and `directory` last because it is the longest value in
    the call and says nothing about what will happen."""
    plan = make_plan(workdir, risk="cirro_destructive",
                     summary="DELETE ds_9 (18 files, 39.3 MB).\n\n  irreversible",
                     warnings=["the files are not recoverable"])
    event = apply_event(workdir, plan_id=plan.plan_id, digest=plan.digest,
                        directory=str(workdir.path.parent))
    done = subprocess.run([sys.executable, str(GATE)], input=json.dumps(event),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0
    updated = json.loads(done.stdout)["hookSpecificOutput"]["updatedInput"]
    assert list(updated) == ["change", "plan_id", "digest", "approved_summary",
                             "directory"], list(updated)
    assert updated["change"] == ("DESTRUCTIVE -- DELETE ds_9 (18 files, 39.3 MB)."
                                 "  (1 warning)")
    # The functional arguments still arrive, sourced from the plan rather than echoed,
    # and `directory` is moved rather than dropped -- it still resolves the session.
    assert updated["plan_id"] == plan.plan_id
    assert updated["digest"] == plan.digest
    assert updated["approved_summary"] == plan.summary
    assert updated["directory"] == str(workdir.path.parent)


def test_the_headline_never_carries_a_newline_into_the_dialog(workdir):
    """A newline in an argument is shown as a literal \\n where the dialog renders it,
    which is what makes the summary itself unreadable there."""
    plan = make_plan(workdir, summary="Move 3 datasets.\n\n  folder\n    to  QC")
    done = subprocess.run([sys.executable, str(GATE)],
                          input=json.dumps(apply_event(workdir, plan_id=plan.plan_id,
                                                       digest=plan.digest)),
                          capture_output=True, text=True, timeout=30)
    updated = json.loads(done.stdout)["hookSpecificOutput"]["updatedInput"]
    assert "\n" not in updated["change"]
    assert updated["change"] == "Move 3 datasets."


def test_the_headline_the_model_supplies_is_overwritten(workdir):
    """It is display-only, so a value arriving from the model must not reach the
    person: the gate replaces it with the stored plan's own line."""
    plan = make_plan(workdir)
    event = apply_event(workdir, plan_id=plan.plan_id, digest=plan.digest,
                        change="Adds a harmless tag")
    done = subprocess.run([sys.executable, str(GATE)], input=json.dumps(event),
                          capture_output=True, text=True, timeout=30)
    updated = json.loads(done.stdout)["hookSpecificOutput"]["updatedInput"]
    assert updated["change"] == "DELETE ds_9"

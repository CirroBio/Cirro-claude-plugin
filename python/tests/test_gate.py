"""The PreToolUse gate. Table-driven over a bypass corpus."""
import ast
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[2] / "scripts" / "gate.py"

ALLOW, ASK, DENY = "allow", "ask", "deny"


def run(event: dict) -> str:
    done = subprocess.run([sys.executable, str(GATE)], input=json.dumps(event),
                          capture_output=True, text=True, timeout=30)
    if done.returncode == 2:
        return DENY
    assert done.returncode == 0, done.stderr
    if not done.stdout.strip():
        return ALLOW
    decision = json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"]
    return decision


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


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
])
def test_running_a_script_that_drives_the_sdk_is_denied(tmp_path, template):
    path = _script(tmp_path, "analyse.py", SDK_SCRIPT)
    assert run(bash(template.format(path=path))) == DENY


def test_running_a_shell_script_that_drives_the_cli_is_denied(tmp_path):
    path = _script(tmp_path, "run.sh", "#!/bin/sh\ncirro upload -p X -n Y\n")
    assert run(bash(f"bash {path}")) == DENY
    assert run(bash(str(path))) == DENY


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


def test_the_gate_uses_the_one_digest_implementation(workdir):
    """The gate used to carry its own copy of DIGEST_FIELDS and the sha256 over them,
    held in step with plans.py by a test. It now imports canonical_digest, so there is
    nothing left to agree: the check is that it really does import it, computes nothing
    of its own, and -- run as the hook is run, with a bare interpreter -- still asks."""
    plan = make_plan(workdir)
    assert run(apply_event(workdir, plan_id=plan.plan_id, digest=plan.digest)) == ASK
    source = GATE.read_text()
    assert "from cirro_agent.plans import" in source
    assert "canonical_digest" in source
    assert "hashlib" not in source, "a second digest implementation crept back in"
    assert "DIGEST_FIELDS = (" not in source, "a second copy of the field list crept back in"


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


def test_the_plans_module_the_gate_imports_is_stdlib_only():
    """Importing it under a system Python with no cirro installed must load nothing but
    the plugin's own pure modules -- that is what lets the gate run with no venv."""
    code = ("import sys; sys.path.insert(0, 'python/src'); import cirro_agent.plans; "
            "bad = [m for m in sys.modules if m.startswith('cirro') and "
            "not m.startswith('cirro_agent')]; assert not bad, bad; "
            "print(sorted(m for m in sys.modules if m.startswith('cirro_agent')))")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=GATE.parents[1])
    assert done.returncode == 0, done.stderr[-600:]
    assert "cirro_agent.plans" in done.stdout


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

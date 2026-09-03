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


def test_writing_sdk_code_asks_rather_than_denies():
    """Authoring a script is legitimate; running it outside the gate is not."""
    assert run({"tool_name": "Write",
                "tool_input": {"file_path": "/tmp/a.py",
                               "content": "from cirro import DataPortal\n"}}) == ASK


def test_writing_unrelated_code_is_allowed():
    assert run({"tool_name": "Write",
                "tool_input": {"file_path": "/tmp/a.py", "content": "print(1)\n"}}) == ALLOW


def test_prose_mentioning_cirro_is_allowed():
    assert run({"tool_name": "Write",
                "tool_input": {"file_path": "/tmp/a.md",
                               "content": "run `import cirro` yourself"}}) == ALLOW


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

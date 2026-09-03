"""Two reversible writes against a real tenant. Doubly opt-in, never in CI.

    CIRRO_AGENT_E2E=1 CIRRO_AGENT_E2E_WRITE_PROJECT=<project id> \\
    CIRRO_AGENT_E2E_WRITE_DATASET=<dataset id> \\
    PYTHONPATH=python/src .dev-venv/bin/python -m pytest python/tests/test_live_writes.py

No mutation has ever run against a live tenant through this plugin. Four bugs of the
kind that only a real call finds were fixed on 2026-08-27 from doubles alone. This
is the smallest real check: add a tag to a project you name and take it off again;
add a tag to a dataset you name and take it off again, proving folder membership
survives. Each half goes through plan then apply exactly as the MCP tools do; the
approval here is you setting the variables on purpose.

Point it at something disposable. It undoes what it does, but it is still a write.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

E2E = os.environ.get("CIRRO_AGENT_E2E") == "1"
PROJECT = os.environ.get("CIRRO_AGENT_E2E_WRITE_PROJECT")
DATASET = os.environ.get("CIRRO_AGENT_E2E_WRITE_DATASET")
pytestmark = pytest.mark.skipif(
    not (E2E and PROJECT),
    reason="live writes; set CIRRO_AGENT_E2E=1 and CIRRO_AGENT_E2E_WRITE_PROJECT")

ROOT = Path(__file__).resolve().parents[2]
TAG = "cirro-claude-plugin-smoke"


def _worker(request: dict) -> dict:
    """Run one worker operation the way the MCP server does: a real child process,
    from the repo root, so the session folder of this checkout is the one used."""
    import json
    code = ("import json, sys; from pathlib import Path; from cirro_agent import runner; "
            "print(json.dumps(runner.call(json.load(sys.stdin), start=Path.cwd())))")
    done = subprocess.run([sys.executable, "-c", code], input=json.dumps(request),
                          capture_output=True, text=True, timeout=300, cwd=ROOT,
                          env={**os.environ, "PYTHONPATH": str(ROOT / "python" / "src"),
                               "CIRRO_AGENT_PLUGIN_ROOT": str(ROOT)})
    assert done.stdout.strip(), done.stderr[-800:]
    return json.loads(done.stdout)


def _plan_and_apply(op: str, args: dict) -> dict:
    from cirro_agent import plans, session as session_mod
    planned = _worker({"verb": "plan", "op": op, "args": args})
    assert planned["ok"], planned
    current = session_mod.load(ROOT)
    summary = plans.load(current, planned["plan_id"]).summary
    applied = _worker({"verb": "apply", "plan_id": planned["plan_id"],
                       "digest": planned["digest"], "approved_summary": summary})
    assert applied["ok"], applied
    return applied["result"]


def _project_tags() -> list:
    out = _worker({"verb": "get", "entity": "project", "id": PROJECT})
    assert out["ok"], out
    return list(out["result"]["row"].get("tags") or [])


def test_a_project_tag_can_be_added_and_removed():
    before = _project_tags()
    assert TAG not in before, "a previous run left its mark; remove it first"
    _plan_and_apply("update_project_tags", {"project_id": PROJECT, "tags": before + [TAG]})
    assert TAG in _project_tags()
    _plan_and_apply("update_project_tags", {"project_id": PROJECT, "tags": before})
    assert _project_tags() == before


@pytest.mark.skipif(not DATASET, reason="set CIRRO_AGENT_E2E_WRITE_DATASET too")
def test_a_dataset_tag_write_preserves_its_folder():
    def row():
        out = _worker({"verb": "get", "entity": "dataset", "id": DATASET,
                       "scope": {"project_ids": [PROJECT]}, "freshness": "fresh"})
        assert out["ok"], out
        return out["result"]["row"]
    before = row()
    folders, tags = list(before.get("folders") or []), list(before.get("tags") or [])
    assert TAG not in tags
    result = _plan_and_apply("update_dataset", {"project_id": PROJECT, "dataset_id": DATASET,
                                                "tags": tags + [TAG]})
    assert result["n_updated"] == 1
    after = row()
    assert TAG in after["tags"]
    assert after["folders"] == folders, "a tag write must never move a dataset"
    _plan_and_apply("update_dataset", {"project_id": PROJECT, "dataset_id": DATASET,
                                       "tags": tags})
    final = row()
    assert final["tags"] == tags and final["folders"] == folders

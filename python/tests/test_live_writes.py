"""Three reversible writes against a real tenant. Doubly opt-in, never in CI.

    CIRRO_AGENT_E2E=1 CIRRO_AGENT_E2E_WRITE_PROJECT=<project id> \\
    CIRRO_AGENT_E2E_WRITE_DATASET=<dataset id> \\
    CIRRO_AGENT_E2E_RETYPE_TO=<ingest process id> \\
    CIRRO_AGENT_E2E_DIR=<a directory bound to the tenant you mean> \\
    PYTHONPATH=python/src "$DEV_VENV/bin/python" -m pytest python/tests/test_live_writes.py

On 2026-09-06 `upload_dataset` and `update_dataset` were run against a development
tenant for the first time, on a dataset created for the purpose; every other operation
has still only ever met a double. Four bugs of the kind that only a real call finds
were fixed on 2026-08-27 from doubles alone. This is the smallest real check: add a tag
to a project you name and take it off again; add a tag to a dataset you name and take
it off again, proving folder membership survives; retype that dataset and put it back,
proving the fields the request does not mention survive a whole-record write. Each
half goes through plan then apply exactly as the MCP tools do; the approval here is
you setting the variables on purpose.

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
# The ingest process to retype the dataset to. Named by you, like everything else here:
# the round trip only comes back if the dataset's current type is itself an ingest
# process, which is a fact about your dataset that this test cannot check for you.
RETYPE_TO = os.environ.get("CIRRO_AGENT_E2E_RETYPE_TO")
pytestmark = pytest.mark.skipif(
    not (E2E and PROJECT),
    reason="live writes; set CIRRO_AGENT_E2E=1 and CIRRO_AGENT_E2E_WRITE_PROJECT")

ROOT = Path(__file__).resolve().parents[2]
# Where the session folder is resolved from. The repo root by default, which is where
# a bound checkout keeps it; a machine with several tenants bound has no single answer,
# so point this at the tenant folder you mean.
START = Path(os.environ.get("CIRRO_AGENT_E2E_DIR") or ROOT)
TAG = "cirro-claude-plugin-smoke"


def _worker(request: dict) -> dict:
    """Run one worker operation the way the MCP server does: a real child process,
    from the repo root, so the session folder of this checkout is the one used."""
    import json
    code = ("import json, sys; from pathlib import Path; from cirro_agent import runner; "
            "print(json.dumps(runner.call(json.load(sys.stdin), start=Path.cwd())))")
    done = subprocess.run([sys.executable, "-c", code], input=json.dumps(request),
                          capture_output=True, text=True, timeout=300, cwd=START,
                          env={**os.environ, "PYTHONPATH": str(ROOT / "python" / "src"),
                               "CIRRO_AGENT_PLUGIN_ROOT": str(ROOT)})
    assert done.stdout.strip(), done.stderr[-800:]
    return json.loads(done.stdout)


def _plan_and_apply(op: str, args: dict) -> dict:
    from cirro_agent import plans, session as session_mod
    planned = _worker({"verb": "plan", "op": op, "args": args})
    assert planned["ok"], planned
    current = session_mod.load(START)
    summary = plans.load(current, planned["plan_id"]).summary
    applied = _worker({"verb": "apply", "plan_id": planned["plan_id"],
                       "digest": planned["digest"], "approved_summary": summary})
    assert applied["ok"], applied
    return applied["result"]


def _row(entity: str, id: str, **scope) -> dict:
    """The one record a `get` answers with.

    `get` returns it in `rows`, as every read verb does -- the envelope has one shape
    and a single-record read is a listing of one. This file asked for `result["row"]`
    and had been failing on its first call since; nothing in CI runs it.
    """
    out = _worker({"verb": "get", "entity": entity, "id": id, "freshness": "fresh",
                   **scope})
    assert out["ok"], out
    rows = out["result"]["rows"]
    assert len(rows) == 1, rows
    return rows[0]


def _project_tags() -> list:
    return list(_row("project", PROJECT).get("tags") or [])


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
        return _row("dataset", DATASET, scope={"project_ids": [PROJECT]})
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


@pytest.mark.skipif(not (DATASET and RETYPE_TO),
                    reason="set CIRRO_AGENT_E2E_WRITE_DATASET and _RETYPE_TO too")
def test_a_dataset_can_be_retyped_and_put_back():
    """The whole-record trap again, on the field that names what a dataset is.

    Run on 2026-09-18 against a development tenant, on a two-file dataset uploaded
    for the purpose: Paired DNAseq (FASTQ) -> Paired 16S (FASTQ) -> back. Both
    follow-ups were accepted each way. Point this at a dataset whose current type is
    an ingest process, or it will not come back: retyping refuses to write a pipeline
    id, which is exactly what an analysis output carries.
    """
    def row():
        return _row("dataset", DATASET, scope={"project_ids": [PROJECT]})

    before = row()
    was = before["process_id"]
    assert was != RETYPE_TO, "already that type; nothing would change"

    result = _plan_and_apply("update_dataset", {"project_id": PROJECT,
                                                "dataset_id": DATASET,
                                                "process": RETYPE_TO})
    updated, = result["updated"]
    assert updated["rerun_transform"] is True, "the transforms are part of a retype"
    assert updated["ingest_samples"] is True, "so is the sample ingest"
    assert "followups_failed" not in updated, updated

    after = row()
    assert after["process_id"] == RETYPE_TO
    # The request replaces the record, so these are what a missing field would blank.
    assert after["name"] == before["name"]
    assert after["description"] == before["description"]
    assert after["tags"] == before["tags"]
    assert after["folders"] == before["folders"]

    _plan_and_apply("update_dataset", {"project_id": PROJECT, "dataset_id": DATASET,
                                       "process": was})
    assert row()["process_id"] == was


@pytest.mark.skipif(not DATASET, reason="set CIRRO_AGENT_E2E_WRITE_DATASET too")
def test_a_dataset_cannot_be_retyped_to_a_pipeline():
    """Refused at plan time, before anything is written, against the real catalogue."""
    listed = _worker({"verb": "query", "entity": "process",
                      "filter": "executor:NEXTFLOW"})
    assert listed["ok"], listed
    pipeline = listed["result"]["rows"][0]["id"]
    planned = _worker({"verb": "plan", "op": "update_dataset",
                       "args": {"project_id": PROJECT, "dataset_id": DATASET,
                                "process": pipeline}})
    assert planned["ok"] is False
    assert planned["error"]["code"] == "INVALID_QUERY"
    assert "not a data type" in planned["error"]["message"]

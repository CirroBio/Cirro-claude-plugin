"""Every read verb against a real tenant. Opt-in, never in CI.

    CIRRO_AGENT_E2E=1 PYTHONPATH=python/src .dev-venv/bin/python -m pytest \
        python/tests/test_live_reads.py

Runs against whatever session folder the working directory resolves to, so it uses
the credential already there and mutates nothing. What it checks is the contract the
fake tenant cannot vouch for: that real responses satisfy the envelope invariants --
contract version, honest cost, a budget that was respected, facets that account for
every match -- and that every read subcommand at least returns.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("CIRRO_AGENT_E2E") != "1",
                                reason="live tenant; set CIRRO_AGENT_E2E=1 to run")

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "bin" / "cirro-agent"


def read(*argv, max_tokens=None) -> dict:
    cmd = [str(BIN), "read", *argv]
    if max_tokens:
        cmd += ["--max-tokens", str(max_tokens)]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                          env={**os.environ, "CIRRO_AGENT_PLUGIN_ROOT": str(ROOT)})
    assert done.stdout.strip(), done.stderr[-800:]
    return json.loads(done.stdout)


def _invariants(out: dict, max_tokens: int) -> None:
    assert out["ok"], out.get("error")
    assert out["api"]["contract"]
    assert out["cost"]["api_calls"] >= 0
    assert out["budget"]["used_est"] <= max_tokens * 1.05, "the budget was not respected"


@pytest.fixture(scope="module")
def project() -> str:
    out = read("projects", max_tokens=4000)
    _invariants(out, 4000)
    rows = out["result"]["rows"]
    assert rows, "the credential sees no projects; nothing else can be exercised"
    return rows[0]["id"]


def test_whoami_proves_the_credential_with_a_real_call():
    out = read("whoami")
    assert out["ok"] and out["authenticated"]
    assert out["verified_by"].startswith("listed"), out


def test_datasets_facets_account_for_every_match(project):
    out = read("datasets", "-p", project, max_tokens=6000)
    _invariants(out, 6000)
    result = out["result"]
    matched = result["counts"]["matched"]
    for name, facet in result["facets"].items():
        if facet.get("kind") != "terms":
            continue
        shown = sum(v["n"] for v in facet["values"])
        other = (facet.get("other") or {}).get("n", 0)
        assert shown + other == matched or facet.get("multi_valued"), \
            f"facet {name}: {shown}+{other} != {matched}"
    assert result["rows_are"] in ("page", "preview_not_page", "sample",
                                  "absent_aggregation_only")


def test_a_second_read_is_served_from_the_snapshot(project):
    read("datasets", "-p", project, max_tokens=6000)
    again = read("datasets", "-p", project, max_tokens=6000)
    assert again["cost"]["api_calls"] == 0, again["cost"]


def test_next_calls_predict_counts_that_hold(project):
    out = read("datasets", "-p", project, max_tokens=6000)
    for suggestion in out["result"]["next_calls"][:3]:
        predicted = suggestion.get("predicted_count")
        call = suggestion.get("call") or {}
        if predicted is None or "filter" not in call:
            continue
        again = read("datasets", "-p", project, "-f", json.dumps(call["filter"]),
                     max_tokens=6000)
        assert again["result"]["counts"]["matched"] == predicted, suggestion


@pytest.mark.parametrize("listing", ["folders", "reference-types", "compute-envs",
                                     "shares", "workspaces", "project-users"])
def test_every_listing_returns(project, listing):
    out = read(listing, "-p", project, max_tokens=3000)
    _invariants(out, 3000)
    assert "items" in out["result"]


def test_processes_and_a_parameter_spec(project):
    out = read("processes", max_tokens=6000)
    _invariants(out, 6000)
    pipelines = [r for r in out["result"]["rows"] if r.get("executor") != "INGEST"]
    assert pipelines, "no pipelines visible"
    spec = read("process-params", pipelines[0]["id"], max_tokens=6000)
    _invariants(spec, 6000)
    assert spec["result"]["n_parameters"] == len(spec["result"]["parameters"])


def test_a_dataset_summary_of_files(project):
    listing = read("datasets", "-p", project, "-f", "status:COMPLETED", max_tokens=6000)
    rows = listing["result"]["rows"]
    if not rows:
        pytest.skip("no completed dataset to summarize")
    out = read("files", "-p", project, "-d", rows[0]["id"], max_tokens=3000)
    _invariants(out, 3000)
    assert out["result"]["files"]["total_files"] is not None


def test_describe_and_check_config_need_no_tenant(tmp_path):
    out = read("describe", "dataset", max_tokens=2000)
    _invariants(out, 2000)
    assert out["cost"]["api_calls"] == 0
    sys.path.insert(0, str(ROOT / "python" / "tests"))
    from test_pipeline_config import SPEC
    from cirro_agent import formgen
    folder = tmp_path / "cfg"
    folder.mkdir()
    for name, text in formgen.generate(SPEC).items():
        (folder / name).write_text(text)
    checked = read("check-config", str(folder), max_tokens=3000)
    assert checked["ok"] and checked["result"]["ok"], checked["result"]

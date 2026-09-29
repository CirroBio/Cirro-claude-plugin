"""The read verbs users hit first, against the fake tenant.

These modules were the least tested in the repo (read_process 11%, read_listings 16%,
local_reads 20%), so each verb is driven end to end here through dispatch.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cirro_agent import formgen, local_reads
from cirro_agent.worker import dispatch, read_process

SRC = Path(__file__).resolve().parents[1] / "src"


def _read(tenant, session_dir, verb, **extra):
    return dispatch.run({"verb": verb, "base_url": tenant["base_url"],
                         "session_dir": str(session_dir.path), **extra})


# --- listings ---------------------------------------------------------------

def test_folders_are_listed_from_the_dataset_snapshot(fake_tenant, session_dir):
    out = _read(fake_tenant, session_dir, "list", of="folders",
                scope={"project_ids": ["proj_a"]})
    assert out["ok"], out
    names = {item["folder"] for item in out["result"]["items"]}
    assert names == {"Immuno", "Archive"}, "every folder the fixture tags, each once"
    assert all(item["datasets"] > 0 for item in out["result"]["items"])
    assert out["result"]["next_calls"], "a listing points at the query that narrows it"


def test_an_empty_listing_is_an_honest_empty_list(fake_tenant, session_dir):
    out = _read(fake_tenant, session_dir, "list", of="shares",
                scope={"project_ids": ["proj_a"]})
    assert out["ok"] and out["result"]["items"] == [] and out["result"]["count"] == 0
    assert out["cost"]["api_calls"] == 1


def test_spend_reports_dollars_by_month_newest_first(fake_tenant, session_dir):
    out = _read(fake_tenant, session_dir, "list", of="spend",
                scope={"project_ids": ["proj_a"]})
    assert out["ok"], out
    result = out["result"]
    assert [item["month"] for item in result["items"]] == ["2026-08", "2026-07"]
    assert result["items"][0]["unit"] == "$"
    # Each month's figure is the sum across services, not one service's share.
    assert result["items"][0]["total"] == 235.50
    assert result["items"][0]["by_service"]["Amazon Elastic Compute Cloud - Compute"] \
        == 210.10
    assert result["totals"] == {"unit": "$", "months": 2, "total": 404.66,
                                "latest_month": "2026-08", "latest_total": 235.50}
    assert result["next_calls"][0]["call"]["scope"] == {"projects": "*"}


def test_spend_across_every_project_is_one_call(fake_tenant, session_dir):
    out = _read(fake_tenant, session_dir, "list", of="spend",
                scope={"projects": "*"})
    assert out["ok"], out
    items = out["result"]["items"]
    assert {item["project_id"] for item in items} == {"proj_a", "proj_b", "proj_gone"}
    # Labeled, not bare IDs, and the tenant total is one call rather than one per
    # project -- which is why this scope does not go through the fan-out gate.
    assert items[0]["project"] == "Immuno-Oncology Core"
    assert out["cost"]["by_kind"].get("metrics.get_all") == 1
    assert "metrics.get" not in out["cost"]["by_kind"]
    assert out["result"]["totals"]["projects"] == 3


def test_spend_for_a_project_the_listing_does_not_carry_says_why(fake_tenant,
                                                                 session_dir):
    """A live tenant reports spend for a project not in /projects.

    An unlabeled id with no explanation is exactly what this response shape exists to
    prevent, so the row is flagged and the note says the total includes it.
    """
    out = _read(fake_tenant, session_dir, "list", of="spend", scope={"projects": "*"})
    stray = next(i for i in out["result"]["items"] if i["project_id"] == "proj_gone")
    assert stray["project"] is None and stray["unlisted"] is True
    assert stray["total"] > 0
    assert out["result"]["totals"]["unlisted"] == 1
    assert "not in your project list" in out["result"]["note"]
    named = next(i for i in out["result"]["items"] if i["project_id"] == "proj_a")
    assert named["unlisted"] is False


def test_spend_is_served_from_the_snapshot_on_a_second_read(fake_tenant, session_dir):
    first = _read(fake_tenant, session_dir, "list", of="spend",
                  scope={"project_ids": ["proj_a"]})
    second = _read(fake_tenant, session_dir, "list", of="spend",
                   scope={"project_ids": ["proj_a"]})
    assert first["cost"]["api_calls"] == 1
    assert second["cost"]["api_calls"] == 0
    assert second["result"]["totals"] == first["result"]["totals"]


def test_project_storage_sums_the_tiers_of_the_latest_record(fake_tenant, session_dir):
    """The metrics record carries a `services` map and no scalar value.

    Reading a `value` attribute the model never had made this null for every project,
    and the note still claimed records had been found.
    """
    out = _read(fake_tenant, session_dir, "get", entity="project", id="proj_a",
                include=["storage_bytes"])
    assert out["ok"], out
    row = out["result"]["rows"][0]
    assert row["storage_as_of"] == "2026-08-31", "the later of the two daily records"
    assert row["storage_amount"] == 4.0, "1.5 + 2.5 across two tiers"
    assert row["storage_unit"] == "GB"
    assert row["storage_bytes"] == 4 * 1024 ** 3
    assert "1 GB as 1073741824 bytes" in row["storage_note"], \
        "the binary convention is stated rather than left to the reader to guess"
    assert set(row["storage_by_tier"]) == {"StandardStorage",
                                           "IntelligentTieringIAStorage"}


def test_an_unconvertible_storage_unit_is_reported_not_guessed(monkeypatch):
    from cirro_agent.worker import read_query
    monkeypatch.setattr(read_query, "project_metrics", lambda reader, project_id: [
        {"series": "storage", "date": "2026-08-31", "unit": "objects",
         "total": 7.0, "by_service": {"StandardStorage": 7.0}}])
    row = read_query._project_storage(object(), "proj_a")
    assert row["storage_bytes"] is None and row["storage_amount"] == 7.0
    assert "'objects'" in row["storage_note"]


def test_a_project_with_no_metrics_says_so(monkeypatch):
    from cirro_agent.worker import read_query
    monkeypatch.setattr(read_query, "project_metrics", lambda reader, project_id: [])
    row = read_query._project_storage(object(), "proj_a")
    assert row["storage_bytes"] is None
    assert "no storage records" in row["storage_note"]


def test_an_empty_compute_listing_says_the_default_is_not_in_it(fake_tenant,
                                                                session_dir):
    """`count: 0` on a tenant whose runs plainly land on a Batch queue reads as a
    fault. The route lists registered environments; the shared default is not one."""
    out = _read(fake_tenant, session_dir, "list", of="compute_envs",
                scope={"project_ids": ["proj_a"]})
    assert out["ok"], out
    assert out["result"]["count"] == 0
    note = out["result"]["note"]
    assert "not no compute" in note and "run_analysis" in note


def test_an_unknown_listing_names_the_ones_that_exist(fake_tenant, session_dir):
    out = _read(fake_tenant, session_dir, "list", of="nonesuch",
                scope={"project_ids": ["proj_a"]})
    assert out["ok"] is False and out["error"]["code"] == "INVALID_QUERY"
    offered = {r["of"] for r in out["error"]["remediation"]["retry_with"]}
    assert {"folders", "shares", "workspaces"} <= offered


# --- process parameters -----------------------------------------------------

def test_process_params_reports_declared_keys_and_defaults(writable_tenant, session_dir):
    out = _read(writable_tenant, session_dir, "process_params", id="proc_rnaseq")
    assert out["ok"], out
    result = out["result"]
    paths = {p["path"] for p in result["parameters"]}
    assert "genome" in paths
    assert result["n_parameters"] == len(result["parameters"])
    assert result["process"]["id"] == "proc_rnaseq"
    assert result["next_calls"][0]["call"]["op"] == "run_analysis"
    # The read that decides how to launch has to say where the pipeline may run; the
    # run_analysis call it hands back is filled in from what is on this page.
    assert result["process"]["execution_modes"] == ["STANDARD", "HEALTHOMICS"]


def test_process_params_carries_the_labels_a_key_name_cannot(
        writable_tenant, session_dir):
    """For most pipelines the key names carry the meaning. For some they carry none --
    a `command` string means nothing without the description saying where inputs are
    mounted and where output must go -- and this is the only read that states it."""
    out = _read(writable_tenant, session_dir, "process_params", id="proc_rnaseq")
    by_path = {p["path"]: p for p in out["result"]["parameters"]}
    assert by_path["aligner"]["title"] == "Alignment tool"
    assert by_path["aligner"]["description"] == "Which aligner to run"
    # Absent rather than omitted, so a caller can tell "no label" from "not reported".
    assert by_path["genome"]["title"] is None


def test_process_params_accepts_a_name_and_refuses_a_stranger(writable_tenant, session_dir):
    by_name = _read(writable_tenant, session_dir, "process_params", id="nf-core/rnaseq v3.14")
    assert by_name["ok"], by_name
    # One character off: refused (never fuzzy), but the real one is offered as a
    # candidate. A name nothing resembles gets NOT_FOUND with no candidates at all.
    close = _read(writable_tenant, session_dir, "process_params", id="nf-core/rnaseq v3.15")
    assert close["error"]["code"] == "NOT_FOUND"
    assert {"name": "nf-core/rnaseq v3.14"} in close["error"]["remediation"]["candidates"]
    stranger = _read(writable_tenant, session_dir, "process_params", id="no-such-pipeline")
    assert stranger["error"]["code"] == "NOT_FOUND"
    assert stranger["error"]["remediation"]["candidates"] == []


WIDE_SPEC = {
    "form": {"type": "object",
             "properties": {f"param_{i:02d}": {"type": "string",
                                               "default": f"default_value_{i:02d}",
                                               "title": f"Parameter {i}"}
                            for i in range(40)},
             "required": ["param_00"]},
    "ui": {},
}


def test_a_truncated_process_params_says_what_it_dropped(writable_tenant, session_dir):
    """This verb halved its parameter list and recorded the loss nowhere -- neither in
    `omitted` nor in `budget.degraded`."""
    writable_tenant["router"].get(
        f"https://{writable_tenant['base_url']}/api/processes/proc_rnaseq/parameters"
    ).respond(json=WIDE_SPEC)
    out = _read(writable_tenant, session_dir, "process_params", id="proc_rnaseq",
                max_tokens=800)
    assert out["ok"], out
    result = out["result"]
    assert result["n_parameters"] == 40, "the count describes the whole spec"
    assert len(result["parameters"]) == result["parameters_truncated_to"] < 40
    entry = next(o for o in out["omitted"] if o["code"] == "LIST_TRUNCATED")
    assert entry["field"] == "parameters"
    assert entry["fix"] == {"verb": "process_params", "id": "proc_rnaseq",
                            "max_tokens": 1600}
    assert any(step["what"] == "parameters" for step in out["budget"]["degraded"])


# --- process configuration --------------------------------------------------

class _Resource:
    """A generated-client Response double: a status code and a parsed body."""

    def __init__(self, status_code=200, **parsed):
        self.status_code = status_code
        self.parsed = SimpleNamespace(**parsed) if parsed else None


REVISION_API = ("cirro_api_client.v1.api.processes.get_latest_process_revision"
                ".sync_detailed")
RESOURCE_API = ("cirro_api_client.v1.api.processes.get_process_resource"
                ".sync_detailed")


def _reader():
    reader = MagicMock()
    reader.cost.to_dict.return_value = {"api_calls": 0}
    return reader


def test_the_read_iterates_the_whole_slot_vocabulary(monkeypatch):
    """The read and the planner now share one fetch, so they cover the same slots."""
    asked = []

    def record(_process_id, resource, **_kwargs):
        asked.append(str(resource))
        return _Resource(content="{}")

    monkeypatch.setattr(REVISION_API, lambda *_a, **_k: _Resource(revision_number=3))
    monkeypatch.setattr(RESOURCE_API, record)

    out = read_process._storage_config(_reader(), "p1", {})
    assert asked == list(formgen.CONFIG_SLOTS)
    assert out["update_token"] == "3", "if_match is the revision number as a string"
    assert out["slots_present"] == sorted(formgen.CONFIG_SLOTS)


def test_a_slot_the_read_could_not_get_is_reported_not_dropped(monkeypatch):
    """`slots_present` would otherwise understate what the process holds, and the
    caller would have no way to tell an empty slot from an unread one."""
    def only_form(_process_id, resource, **_kwargs):
        return (_Resource(content='{"form": {}}') if str(resource) == "form"
                else _Resource(status_code=500))

    monkeypatch.setattr(REVISION_API, lambda *_a, **_k: _Resource(revision_number=3))
    monkeypatch.setattr(RESOURCE_API, only_form)

    out = read_process._storage_config(_reader(), "p1", {})
    assert out["slots_present"] == ["form"]
    unread = {entry["what"] for entry in out["omitted"]}
    assert "the compute slot" in unread and "the preprocess slot" in unread
    assert all(entry["fix"]["verb"] == "process_config" for entry in out["omitted"])


def test_an_unreadable_revision_is_not_reported_as_unconfigured(monkeypatch):
    """A 404 means the process really has no configuration; a failure means nobody
    knows. Reporting the second as the first told the caller a configured process was
    registered but unconfigured."""
    def unavailable(*_a, **_k):
        raise ConnectionError("connection reset by peer")

    monkeypatch.setattr(REVISION_API, unavailable)
    out = read_process._storage_config(_reader(), "p1", {})
    assert out["revision"] is None
    assert "unconfigured" not in out["note"]
    assert "connection reset by peer" in out["note"]
    assert out["omitted"][0]["fix"] == {"verb": "process_config", "id": "p1"}


def test_no_revision_at_all_still_reads_as_unconfigured(monkeypatch):
    from cirro_api_client.v1.errors import NotFoundException

    def missing(*_a, **_k):
        raise NotFoundException({"statusCode": 404, "errorCode": "NOT_FOUND",
                                 "errorDetail": "nothing in storage", "errors": []})

    monkeypatch.setattr(REVISION_API, missing)
    out = read_process._storage_config(_reader(), "p1", {})
    assert out["revision"] is None and "unconfigured" in out["note"]
    assert "omitted" not in out


def test_building_the_read_parser_does_not_import_the_worker():
    """`--slot` used to take its choices from worker.read_process, which pulled reads,
    portal, cache, session, query and summarize into the parent process on every
    invocation -- `cirro-agent --help` included."""
    code = ("import sys; from cirro_agent import cli; cli.build_parser();"
            " print([m for m in sys.modules if m.startswith('cirro_agent.worker')])")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env={**os.environ,
                               "PYTHONPATH": str(SRC) + os.pathsep
                               + os.environ.get("PYTHONPATH", "")})
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == "[]", done.stdout


def test_the_session_free_reads_do_not_import_the_worker():
    """describe, workflow_spec and check_config run in the PARENT process, which is
    why they live outside worker/. `DETAIL_BUDGET` was imported from worker.read_query,
    inverting that boundary and pulling five worker modules in to read one integer --
    two of them being where a future top-level `import cirro` would silently break the
    rule that only the child imports the SDK."""
    code = ("import sys; import cirro_agent.local_reads;"
            " print([m for m in sys.modules if m.startswith('cirro_agent.worker')],"
            " [m for m in sys.modules if m.split('.')[0] == 'cirro'])")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env={**os.environ,
                               "PYTHONPATH": str(SRC) + os.pathsep
                               + os.environ.get("PYTHONPATH", "")})
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == "[] []", done.stdout


# --- summaries --------------------------------------------------------------

def test_summarize_files_collapses_names_into_patterns(fake_tenant, session_dir):
    out = _read(fake_tenant, session_dir, "summarize", of="files", dataset_id="ds_0000",
                scope={"project_ids": ["proj_a"]})
    assert out["ok"], out
    files = out["result"]["files"]
    assert files["total_files"] >= 1
    assert files["name_patterns"], "480 filenames become one template line"
    assert out["result"]["next_calls"][0]["call"]["glob"]


def test_the_params_verbs_reach_the_keys_of_a_free_form_map(fake_tenant, session_dir):
    """Both ways to ask for a run's params, end to end.

    `DatasetDetailParams` is neither dict nor Mapping, so before `plain` learned to
    unwrap it `summarize params` died on `.items()` for every dataset on every tenant
    -- and the suite stayed green, because the unwrapping was only ever tested on
    `plain` itself. These two assert the verbs, which is what shipped broken.
    """
    summary = _read(fake_tenant, session_dir, "summarize", of="params",
                    dataset_id="ds_0000", scope={"project_ids": ["proj_a"]})
    assert summary["ok"], summary
    body = summary["result"]["params"]
    assert body["n_params"] == 2
    assert body["non_default"] == {"aligner": "star", "genome": "GRCh38"}

    got = _read(fake_tenant, session_dir, "get", entity="dataset", id="ds_0000",
                scope={"project_ids": ["proj_a"]}, include=["params"])
    assert got["ok"], got
    assert got["result"]["rows"][0]["params"] == {"aligner": "star", "genome": "GRCh38"}


def test_summarize_cost_rolls_a_fan_out_up_by_process(fake_tenant, session_dir):
    out = _read(fake_tenant, session_dir, "summarize", of="cost", dataset_id="ds_0000",
                scope={"project_ids": ["proj_a"]})
    assert out["ok"], out
    body = out["result"]["cost"]
    assert body["total_cost"] == 6.55 and body["unit"] == "$"
    assert body["is_estimate"] is True, "a modelled figure must not read as a bill"
    assert body["n_tasks"] == 26
    assert body["cost_by_status"] == {"COMPLETED": 5.8, "FAILED": 0.75, "CACHED": 0.0}
    rollup = {row["process"]: row for row in body["cost_by_process"]}
    # 24 per-sample tasks collapse to two lines, and ALIGN leads on money not on count.
    assert body["cost_by_process"][0]["process"] == "ALIGN"
    assert rollup["ALIGN"]["n"] == 4 and rollup["ALIGN"]["cost"] == 5.6
    assert rollup["FASTQC"]["n"] == 20 and rollup["FASTQC"]["cost"] == 0.4
    assert rollup["ALIGN"]["share"] == round(5.6 / 6.55, 4)
    assert len(body["most_expensive_tasks"]) == 10, "the tail never comes back in full"
    assert body["most_expensive_tasks"][0]["task_id"] == "t_align_3"


def test_summarize_cost_separates_money_that_produced_nothing(fake_tenant, session_dir):
    out = _read(fake_tenant, session_dir, "summarize", of="cost", dataset_id="ds_0000",
                scope={"project_ids": ["proj_a"]})
    body = out["result"]["cost"]
    # CACHED is free rather than wasted, so only the failure counts.
    assert body["wasted_cost"] == 0.75
    assert body["wasted_share"] == round(0.75 / 6.55, 4)
    whys = {call["id"] for call in out["result"]["next_calls"]}
    assert "nc_failed" in whys, "wasted spend points at the tasks that burned it"


def test_cost_on_a_runtime_older_than_the_pin_says_to_rebuild(fake_tenant, session_dir,
                                                              monkeypatch):
    """Nothing forces a re-sync when the pin moves, so an install still on 1.12.1 has
    no cost API. Untyped that is an AttributeError, which the dispatcher renders as
    "retry once, then check service status" -- advice that can never work."""
    from cirro.services.execution import ExecutionService
    monkeypatch.delattr(ExecutionService, "get_cost")
    out = _read(fake_tenant, session_dir, "summarize", of="cost", dataset_id="ds_0000",
                scope={"project_ids": ["proj_a"]})
    assert out["error"]["code"] == "RUNTIME_NOT_INSTALLED"
    assert "/cirro:setup" in out["error"]["remediation"]["user_action"]


@pytest.mark.parametrize("status_code, error_code", [(400, "BAD_REQUEST"),
                                                     (404, "NOT_FOUND")])
def test_cost_for_an_uploaded_dataset_is_refused_not_reported_as_zero(
        fake_tenant, session_dir, status_code, error_code):
    """A dataset no analysis produced has no cost record, and dev answers that with a
    400 rather than the 404 the shape suggests. Untyped, either one reaches the caller
    as a retryable UPSTREAM_ERROR advising them to check Cirro's service status -- and
    shaping it into a zero total instead would report an upload as a free run."""
    fake_tenant["router"].get(
        url__regex=r"https://app.cirro.test/api/projects/[^/]+/execution/[^/]+/cost$"
    ).respond(status_code=status_code,
              json={"statusCode": status_code, "errorCode": error_code,
                    "errorDetail": "This dataset does not have an execution "
                                   "associated with it", "errors": []})
    out = _read(fake_tenant, session_dir, "summarize", of="cost", dataset_id="ds_0000",
                scope={"project_ids": ["proj_a"]})
    assert out["error"]["code"] == "WRONG_EXECUTOR"
    assert out["error"]["remediation"]["retry_with"]


def test_summarize_needs_a_dataset_for_anything_but_samples(fake_tenant, session_dir):
    out = _read(fake_tenant, session_dir, "summarize", of="files",
                scope={"project_ids": ["proj_a"]})
    assert out["error"]["code"] == "SCOPE_REQUIRED"


def test_summarize_rejects_an_unknown_kind_with_the_known_ones(fake_tenant, session_dir):
    out = _read(fake_tenant, session_dir, "summarize", of="vibes",
                scope={"project_ids": ["proj_a"]})
    assert out["error"]["code"] == "INVALID_QUERY"
    offered = {r["of"] for r in out["error"]["remediation"]["retry_with"]}
    assert {"files", "logs", "tasks"} <= offered


# --- local reads: no session, no network -----------------------------------

def test_describe_is_self_documenting_and_free():
    out = local_reads.do_describe({"verb": "describe", "entity": "dataset"})
    assert out["ok"]
    tiers = {f["tier"] for f in out["result"]["fields"]}
    assert {"list", "detail"} <= tiers
    assert out["cost"]["api_calls"] == 0
    assert out["budget"]["used_est"] > 0


def test_describe_refuses_an_unknown_entity():
    from cirro_agent import errors
    with pytest.raises(errors.AgentError) as caught:
        local_reads.do_describe({"verb": "describe", "entity": "unicorn"})
    assert caught.value.code in ("INVALID_QUERY", "NOT_FOUND")


def test_check_config_passes_generated_assets_and_flags_a_dangling_mapping(tmp_path):
    from cirro_agent import formgen
    from test_pipeline_config import SPEC
    folder = tmp_path / "config"
    folder.mkdir()
    for name, text in formgen.generate(SPEC).items():
        (folder / name).write_text(text)

    clean = local_reads.do_check_config({"verb": "check_config", "path": str(folder)})
    assert clean["ok"] and clean["result"]["ok"], clean["result"]

    mapping = json.loads((folder / formgen.INPUT).read_text())
    mapping["ghost"] = "$.dataset.params.advanced.ghost"
    (folder / formgen.INPUT).write_text(json.dumps(mapping))
    flagged = local_reads.do_check_config({"verb": "check_config", "path": str(folder)})
    assert flagged["result"]["ok"] is False
    assert any("ghost" in p["message"] for p in flagged["result"]["problems"])


def test_a_truncated_workflow_spec_says_what_it_dropped(tmp_path):
    """The other local read that halves a list. It marked the cut but declared it
    nowhere a caller reading `omitted` would see."""
    body = "\n".join(f"  param_{i:02d} = 'default_value_{i:02d}'" for i in range(60))
    (tmp_path / "main.nf").write_text("workflow { }\n")
    (tmp_path / "nextflow.config").write_text(f"params {{\n{body}\n}}\n")

    out = local_reads.do_workflow_spec({"verb": "workflow_spec",
                                        "target": str(tmp_path), "max_tokens": 900})
    assert out["ok"], out
    result = out["result"]
    assert result["counts"]["params_declared"] == 60, "the count covers the whole file"
    assert len(result["params"]) == result["params_truncated_to"] < 60
    entry = next(o for o in out["omitted"] if o["code"] == "LIST_TRUNCATED")
    assert entry["field"] == "params"
    assert entry["fix"]["verb"] == "workflow_spec"
    assert entry["fix"]["target"] == str(tmp_path)
    assert any(step["what"] == "params" for step in out["budget"]["degraded"])


def test_a_truncated_check_config_says_what_it_dropped(tmp_path):
    """300 dangling mappings ran to 20,000 tokens against a 6,000 budget, with no
    truncation, no degraded step and no note -- it simply overran."""
    from test_pipeline_config import SPEC
    folder = tmp_path / "config"
    folder.mkdir()
    for name, text in formgen.generate(SPEC).items():
        (folder / name).write_text(text)
    mapping = json.loads((folder / formgen.INPUT).read_text())
    for i in range(300):
        mapping[f"ghost_{i}"] = f"$.dataset.params.advanced.ghost_{i}"
    (folder / formgen.INPUT).write_text(json.dumps(mapping))

    out = local_reads.do_check_config({"verb": "check_config", "path": str(folder)})
    result = out["result"]
    assert result["ok"] is False
    assert result["n_errors"] >= 300, "the verdict counts every problem, not the shown ones"
    assert len(result["problems"]) == result["problems_truncated_to"] < 300
    entry = next(o for o in out["omitted"] if o["code"] == "LIST_TRUNCATED")
    assert entry["field"] == "problems"
    assert entry["fix"] == {"verb": "check_config", "path": str(folder),
                            "max_tokens": 12000}
    assert any(step["what"] == "problems" for step in out["budget"]["degraded"])
    assert out["budget"]["used_est"] <= out["budget"]["max_tokens"]


def test_a_descending_sort_can_be_written_the_way_the_help_documents_it():
    """Both options say "prefix with - for descending", but argparse read the next
    token as another option and failed with "expected one argument", so only the
    `=` form worked. A real flag after --sort must still be an error."""
    from cirro_agent import cli
    parser = cli.build_parser()
    assert cli._attach_descending(
        ["read", "datasets", "--sort", "-created_at"], parser
    ) == ["read", "datasets", "--sort=-created_at"]
    assert cli._attach_descending(
        ["read", "datasets", "--order-by", "-count"], parser
    ) == ["read", "datasets", "--order-by=-count"]
    # --fresh is a real flag on the subcommand, so it is not swallowed as a value.
    assert cli._attach_descending(
        ["read", "datasets", "--sort", "--fresh"], parser
    ) == ["read", "datasets", "--sort", "--fresh"]
    args = parser.parse_args(cli._attach_descending(
        ["read", "datasets", "-p", "P", "--sort", "-created_at"], parser))
    assert args.sort == ["-created_at"]


def test_option_strings_reaches_subcommand_flags():
    """A top-level-only scan finds none of them, which made every subcommand flag
    look like a value and let `--sort --fresh` parse as sort=['--fresh']."""
    from cirro_agent import cli
    found = cli._option_strings(cli.build_parser())
    assert {"--fresh", "--stale-ok", "--dir", "--confirm-detail-cost"} <= found

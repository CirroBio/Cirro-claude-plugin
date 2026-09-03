"""The read verbs users hit first, against the fake tenant.

These modules were the least tested in the repo (read_process 11%, read_listings 16%,
local_reads 20%), so each verb is driven end to end here through dispatch.
"""
import json

import pytest

from cirro_agent import local_reads
from cirro_agent.worker import dispatch


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
    keys = {p["key"] for p in result["parameters"]}
    assert "genome" in keys
    assert result["n_parameters"] == len(result["parameters"])
    assert result["process"]["id"] == "proc_rnaseq"
    assert result["next_calls"][0]["call"]["op"] == "run_analysis"


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


# --- summaries --------------------------------------------------------------

def test_summarize_files_collapses_names_into_patterns(fake_tenant, session_dir):
    out = _read(fake_tenant, session_dir, "summarize", of="files", dataset_id="ds_0000",
                scope={"project_ids": ["proj_a"]})
    assert out["ok"], out
    files = out["result"]["files"]
    assert files["total_files"] >= 1
    assert files["name_patterns"], "480 filenames become one template line"
    assert out["result"]["next_calls"][0]["call"]["glob"]


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

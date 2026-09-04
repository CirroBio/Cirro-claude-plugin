"""The full plan -> apply cycle against a fake tenant.

Covers the property the whole gate exists for: the operation that runs is the one
whose rendered text was approved, and nothing else can be substituted for it.
"""
import json
import time


from cirro_agent import plans
from cirro_agent.worker import dispatch

from conftest import API, approved_summary


def call(tenant, session_dir, verb, **overrides):
    request = {"verb": verb, "base_url": tenant["base_url"],
               "session_dir": str(session_dir.path)}
    request.update(overrides)
    return dispatch.run(request)


def plan_run(tenant, session_dir, **arg_overrides):
    args = {"project_id": "proj_a", "process": "proc_rnaseq",
            "name": "RNAseq batch 32", "source_dataset_ids": ["ds_0000"],
            "params": {"genome": "GRCh38"}}
    args.update(arg_overrides)
    return call(tenant, session_dir, "plan", op="run_analysis", args=args)


# --- plan is read-only and informative ---

def test_plan_renders_a_summary_a_person_can_check(writable_tenant, session_dir):
    result = plan_run(writable_tenant, session_dir)
    assert result["ok"], result
    rendered = result["rendered"]
    assert "app.cirro.test" in rendered and "sminot" in rendered
    assert "nf-core/rnaseq v3.14" in rendered
    assert "RNAseq batch 32" in rendered
    assert "genome" in rendered
    assert "no cost estimate is possible" in rendered
    assert result["risk"] == "cirro_write"
    assert result["expires_in_s"] == plans.TTL_SECONDS


def test_plan_states_the_input_volume(writable_tenant, session_dir):
    result = plan_run(writable_tenant, session_dir)
    assert "482 files" in result["rendered"]
    assert "41.2 MB" in result["rendered"]


def test_plan_flags_parameters_the_pipeline_would_ignore(writable_tenant, session_dir):
    result = plan_run(writable_tenant, session_dir,
                      params={"genome": "GRCh38", "typo_param": 1})
    assert any("typo_param" in w for w in result["warnings"]), result["warnings"]


def test_plan_rejects_a_process_that_does_not_exist(writable_tenant, session_dir):
    result = plan_run(writable_tenant, session_dir, process="nf-core/nope")
    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"


def test_plan_refuses_a_dataset_name_and_hands_back_the_id(writable_tenant, session_dir):
    result = plan_run(writable_tenant, session_dir,
                      source_dataset_ids=["RNAseq_batch0_S0"])
    assert result["error"]["code"] == "NAME_REQUIRED_ID"
    assert result["error"]["remediation"]["candidates"][0]["id"] == "ds_0000"


def test_plan_requires_an_input_dataset(writable_tenant, session_dir):
    result = plan_run(writable_tenant, session_dir, source_dataset_ids=[])
    assert result["error"]["code"] == "INVALID_QUERY"


def test_plan_writes_a_private_single_use_record(writable_tenant, session_dir):
    result = plan_run(writable_tenant, session_dir)
    path = session_dir.path / "plans" / f"{result['plan_id']}.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    stored = json.loads(path.read_text())
    assert stored["status"] == "pending"
    # Only resolved IDs are stored, never the names the caller may have used.
    assert stored["args"]["process_id"] == "proc_rnaseq"
    assert stored["args"]["source_dataset_ids"] == ["ds_0000"]


# --- apply executes exactly what was approved ---

def apply_from(tenant, session_dir, plan_result, **overrides):
    # The approved text comes from the stored plan, which is where the approval hook
    # reads it -- the plan response no longer repeats it beside `rendered`.
    payload = {"plan_id": plan_result["plan_id"], "digest": plan_result["digest"],
               "approved_summary": approved_summary(session_dir,
                                                    plan_result["plan_id"])}
    payload.update(overrides)
    return call(tenant, session_dir, "apply", **payload)


def test_apply_runs_the_planned_operation(writable_tenant, session_dir):
    planned = plan_run(writable_tenant, session_dir)
    applied = apply_from(writable_tenant, session_dir, planned)
    assert applied["ok"], applied
    assert applied["applied"] == "run_analysis"
    assert applied["result"]["created_dataset_id"] == "ds_new_0001"
    assert applied["result"]["follow_with"]["freshness"] == "fresh"


def test_apply_invalidates_the_snapshot_it_made_stale(writable_tenant, session_dir):
    call(writable_tenant, session_dir, "query", entity="dataset",
         scope={"project_ids": ["proj_a"]})
    planned = plan_run(writable_tenant, session_dir)
    applied = apply_from(writable_tenant, session_dir, planned)
    assert any("proj_a" in label for label in applied["invalidated"])
    # The next read must not answer from the snapshot the mutation invalidated.
    refetched = call(writable_tenant, session_dir, "query", entity="dataset",
                     scope={"project_ids": ["proj_a"]})
    assert refetched["cost"]["by_kind"].get("datasets.list", 0) >= 1


def test_a_chain_step_can_resolve_what_the_step_before_it_created(
        writable_tenant, session_dir):
    """The chain invalidated only once it had finished, so step 2 resolved its subject
    against a listing step 1's own planner had cached -- and no listing taken before
    step 1 ran can contain what step 1 made. This is the shape that made
    create_process -> upload_process_config refuse NOT_FOUND on the new process."""
    planned = call(writable_tenant, session_dir, "plan", op="chain", args={"steps": [
        {"label": "run", "op": "run_analysis",
         "args": {"project_id": "proj_a", "process": "proc_rnaseq",
                  "name": "RNAseq batch 32", "source_dataset_ids": ["ds_0000"],
                  "params": {"genome": "GRCh38"}}},
        {"label": "file", "op": "update_dataset",
         "args": {"project_id": "proj_a",
                  "dataset_id": {"from_step": 0, "field": "created_dataset_id"},
                  "folder": "Immuno"}},
    ]})
    assert planned["ok"], planned

    applied = apply_from(writable_tenant, session_dir, planned)
    assert applied["ok"], applied
    result = applied["result"]
    assert result["n_completed"] == 2, result.get("failed_at")
    assert result["completed"][1]["op"] == "update_dataset"
    assert any("proj_a" in label for label in applied["invalidated"])


def test_apply_cannot_be_replayed(writable_tenant, session_dir):
    planned = plan_run(writable_tenant, session_dir)
    assert apply_from(writable_tenant, session_dir, planned)["ok"]
    again = apply_from(writable_tenant, session_dir, planned)
    assert again["ok"] is False
    assert again["error"]["code"] == "PLAN_INVALID"


def test_apply_refuses_a_substituted_summary(writable_tenant, session_dir):
    """The approval a person gave must match the operation being run."""
    planned = plan_run(writable_tenant, session_dir)
    result = apply_from(writable_tenant, session_dir, planned,
                        approved_summary="Launch something harmless")
    assert result["error"]["code"] == "PLAN_INVALID"


def test_apply_refuses_a_tampered_digest(writable_tenant, session_dir):
    planned = plan_run(writable_tenant, session_dir)
    result = apply_from(writable_tenant, session_dir, planned, digest="deadbeef")
    assert result["error"]["code"] == "PLAN_INVALID"


def test_apply_refuses_an_expired_plan(writable_tenant, session_dir):
    planned = plan_run(writable_tenant, session_dir)
    stored = plans.load(session_dir, planned["plan_id"])
    stored.expires_at = time.time() - 1
    plans._save(session_dir, stored)
    result = apply_from(writable_tenant, session_dir, planned)
    assert result["error"]["code"] == "PLAN_INVALID"
    assert "expired" in result["error"]["message"]


def test_apply_refuses_a_plan_edited_on_disk(writable_tenant, session_dir):
    """Editing the stored args must not change what runs under the old approval."""
    planned = plan_run(writable_tenant, session_dir)
    path = session_dir.path / "plans" / f"{planned['plan_id']}.json"
    stored = json.loads(path.read_text())
    stored["args"]["source_dataset_ids"] = ["ds_0099"]
    path.write_text(json.dumps(stored))
    result = apply_from(writable_tenant, session_dir, planned)
    assert result["error"]["code"] == "PLAN_INVALID"


def test_apply_writes_an_audit_trail(writable_tenant, session_dir):
    planned = plan_run(writable_tenant, session_dir)
    apply_from(writable_tenant, session_dir, planned)
    files = sorted((session_dir.path / "runs").glob("*.jsonl"))
    assert files
    entries = [json.loads(line) for line in files[-1].read_text().splitlines()]
    assert [e["phase"] for e in entries] == ["intent", "outcome"]
    assert entries[0]["username"] == "sminot"
    assert entries[0]["base_url"] == "app.cirro.test"
    assert entries[1]["result"]["created_dataset_id"] == "ds_new_0001"


def test_a_failed_apply_is_recorded_and_reported(writable_tenant, session_dir):
    planned = plan_run(writable_tenant, session_dir)
    writable_tenant["router"].post(
        f"{API}/projects/proj_a/execution").respond(status_code=500)
    result = apply_from(writable_tenant, session_dir, planned)
    assert result["ok"] is False
    assert plans.load(session_dir, planned["plan_id"]).status == plans.FAILED
    files = sorted((session_dir.path / "runs").glob("*.jsonl"))
    entries = [json.loads(line) for line in files[-1].read_text().splitlines()]
    assert "error" in entries[-1]


# --- destructive operations carry their blast radius ---

def test_delete_warns_about_derived_datasets(writable_tenant, session_dir):
    result = call(writable_tenant, session_dir, "plan", op="delete_dataset",
                  args={"project_id": "proj_a", "dataset_id": "ds_0000"})
    assert result["ok"], result
    assert result["risk"] == "cirro_destructive"
    assert any("derive" in w or "source" in w for w in result["warnings"]), \
        result["warnings"]
    assert "not reversible" in result["rendered"]


def test_update_dataset_shows_a_before_and_after(writable_tenant, session_dir):
    result = call(writable_tenant, session_dir, "plan", op="update_dataset",
                  args={"project_id": "proj_a", "dataset_id": "ds_0000",
                        "name": "A better name"})
    assert result["ok"], result
    assert "from" in result["rendered"] and "to" in result["rendered"]
    assert "A better name" in result["rendered"]


def test_a_no_op_update_is_refused(writable_tenant, session_dir):
    result = call(writable_tenant, session_dir, "plan", op="update_dataset",
                  args={"project_id": "proj_a", "dataset_id": "ds_0000",
                        "name": "RNAseq_batch0_S0"})
    assert result["error"]["code"] == "INVALID_QUERY"
    assert "Nothing would change" in result["error"]["message"]


def test_stop_refuses_a_finished_run(writable_tenant, session_dir):
    result = call(writable_tenant, session_dir, "plan", op="stop_analysis",
                  args={"project_id": "proj_a", "dataset_id": "ds_0002"})
    assert result["error"]["code"] == "WRONG_STATUS"
    assert result["error"]["remediation"]["retry_with"]


def test_an_unknown_operation_lists_the_supported_ones(writable_tenant, session_dir):
    result = call(writable_tenant, session_dir, "plan", op="drop_database", args={})
    assert result["error"]["code"] == "NOT_SUPPORTED"
    assert len(result["error"]["remediation"]["retry_with"]) > 20


def test_a_schema_plan_will_not_claim_nothing_was_removed(
        writable_tenant, session_dir, monkeypatch):
    """An unreadable baseline used to render as "keys removed []" with no warning."""

    def unreadable(*_args, **_kwargs):
        raise RuntimeError("schema service unavailable")
    monkeypatch.setattr(
        "cirro.services.metadata.MetadataService.get_project_schema", unreadable)

    planned = call(writable_tenant, session_dir, "plan", op="update_project_schema",
                   args={"project_id": "proj_a",
                         "schema": {"properties": {"only_key": {"type": "string"}}}})

    assert planned["ok"], planned
    shown = approved_summary(session_dir, planned["plan_id"])
    assert "keys removed  []" not in shown
    assert "unknown" in shown
    assert any("could not be read" in w for w in planned["warnings"]), planned["warnings"]


# --- the approval must still describe reality when it executes ---------------

def test_apply_refuses_when_the_facts_changed_after_approval(
        writable_tenant, session_dir, monkeypatch):
    """A ten-minute clock cannot tell whether the figures are still true.

    Re-planning can, so the expiry only has to guard intent, and accuracy is checked
    here at the moment of execution.
    """
    planned = call(writable_tenant, session_dir, "plan", op="update_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "tags": ["new-tag"]})
    assert planned["ok"], planned

    # The dataset picks up a tag from elsewhere between approval and execution, so the
    # tag list the person approved replacing is no longer the list that exists.
    from cirro_agent.worker import dataset_ops
    original = dataset_ops._dataset

    def drifted(reader, project_id, given):
        row = dict(original(reader, project_id, given))
        row["tags"] = row["tags"] + ["added-by-someone-else"]
        return row
    monkeypatch.setattr(dataset_ops, "_dataset", drifted)

    applied = apply_from(writable_tenant, session_dir, planned)

    assert applied["ok"] is False
    assert applied["error"]["code"] == "PLAN_STALE"
    assert applied["error"]["remediation"]["retry_with"][0]["renew_from"] == \
        planned["plan_id"]


def test_apply_proceeds_when_nothing_changed(writable_tenant, session_dir):
    """Re-planning is a check, not an obstacle: an unchanged world still applies."""
    planned = call(writable_tenant, session_dir, "plan", op="update_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "tags": ["new-tag"]})
    applied = apply_from(writable_tenant, session_dir, planned)
    assert applied["ok"], applied
    assert applied["result"]["n_updated"] == 1


def test_a_plan_whose_target_disappeared_is_stale_not_a_crash(
        writable_tenant, session_dir, monkeypatch):
    planned = call(writable_tenant, session_dir, "plan", op="update_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "tags": ["new-tag"]})

    from cirro_agent import errors
    from cirro_agent.worker import dataset_ops

    def gone(*_args, **_kwargs):
        raise errors.NotFound("no such dataset", what="w",
                              retry_with=[{"verb": "query"}])
    monkeypatch.setattr(dataset_ops, "_dataset", gone)

    applied = apply_from(writable_tenant, session_dir, planned)
    assert applied["error"]["code"] == "PLAN_STALE"
    assert "no longer plans cleanly" in applied["error"]["message"]


def test_the_expiry_is_generous_now_that_accuracy_is_checked_separately():
    from cirro_agent import plans
    assert plans.TTL_SECONDS == 3600


def test_a_stale_plan_is_finished_not_left_applying(
        writable_tenant, session_dir, monkeypatch):
    """Revalidation refusing after claim used to leave the plan APPLYING with an
    intent record and no outcome -- a mutation that looks like it never finished."""
    from cirro_agent import plans
    from cirro_agent.worker import dataset_ops
    planned = call(writable_tenant, session_dir, "plan", op="update_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "tags": ["new-tag"]})
    original = dataset_ops._dataset

    def drifted(reader, project_id, given):
        row = dict(original(reader, project_id, given))
        row["tags"] = row["tags"] + ["elsewhere"]
        return row
    monkeypatch.setattr(dataset_ops, "_dataset", drifted)

    applied = apply_from(writable_tenant, session_dir, planned)

    assert applied["error"]["code"] == "PLAN_STALE"
    record = plans.load(session_dir, planned["plan_id"])
    assert record.status == plans.FAILED
    assert plans.outcome(session_dir, record)["error"].startswith("PLAN_STALE")


def test_a_scope_less_invalidation_drops_every_snapshot_of_that_kind(
        writable_tenant, session_dir):
    """Snapshots are stored under a scope key, so invalidating with no scope used to
    compute a key nothing was stored under and remove nothing. Snapshot files are
    named by hash, so this is proved through the cache, not the filenames."""
    from cirro_agent.worker import reads
    request = {"base_url": writable_tenant["base_url"],
               "session_dir": str(session_dir.path)}
    warm = reads.Reader(request)
    warm.processes(include_archived=True)
    warm.processes(include_archived=False)
    assert warm.cost.to_dict()["api_calls"] > 0

    served = reads.Reader(request)
    served.processes(include_archived=True)
    assert served.cost.to_dict()["api_calls"] == 0, "answered from the snapshot"

    assert warm.cache.invalidate_kinds(["processes"]), "both archived scopes dropped"

    cold = reads.Reader(request)
    cold.processes(include_archived=True)
    assert cold.cost.to_dict()["api_calls"] > 0, "refetched after invalidation"


def test_the_plan_response_reports_the_risk_the_record_carries(
        writable_tenant, session_dir):
    planned = call(writable_tenant, session_dir, "plan", op="update_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "tags": ["new-tag"]})
    from cirro_agent import plans
    assert planned["risk"] == plans.load(session_dir, planned["plan_id"]).risk


def test_a_warnings_only_change_counts_as_stale(writable_tenant, session_dir,
                                                monkeypatch):
    """render prints warnings in the approved text, so a change to them is a change
    to what the person read; renew and apply now agree on that."""
    from cirro_agent.worker import dataset_ops
    planned = call(writable_tenant, session_dir, "plan", op="update_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "tags": ["new-tag"]})
    original = dataset_ops.plan_update_dataset

    def louder(reader, args):
        draft = original(reader, args)
        draft.warnings = list(draft.warnings) + ["something new to know"]
        return draft
    monkeypatch.setitem(dataset_ops.mutations.OPS, "update_dataset",
                        dataset_ops.mutations.Op("update_dataset", "cirro_write",
                                                 louder,
                                                 dataset_ops.apply_update_dataset))
    applied = apply_from(writable_tenant, session_dir, planned)
    assert applied["error"]["code"] == "PLAN_STALE"
    assert applied["error"]["detail"]["changed"] == "disclosure"

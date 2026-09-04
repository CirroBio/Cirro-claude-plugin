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


# The account the fake tenant's token is minted for, and the address a launch
# notifies when the caller names nobody.
SESSION_EMAIL = "sminot@cirro.test"

DEFAULT_USER = {"email": SESSION_EMAIL}


def serve_user(tenant, *, email=SESSION_EMAIL, notifications=True):
    """The authenticated account's own record, which is where its email lives.

    Built through the generated model, as the other fixtures are, so it cannot claim
    a shape UserDetail does not have. Re-registering the same pattern replaces the
    route, so a test may serve a different record before planning.
    """
    from cirro_api_client.v1.models import UserDetail, UserSettings
    payload = UserDetail(
        username="sminot", name="Sam Minot", email=email, organization="Cirro",
        project_assignments=[], global_roles=[],
        settings=UserSettings(analysis_update_notifications_enabled=notifications),
    ).to_dict()
    tenant["router"].get(f"{API}/users/sminot").respond(json=payload)


def plan_run(tenant, session_dir, notify_user=DEFAULT_USER, **arg_overrides):
    """Plan a launch. `notify_user=None` leaves the user endpoint as the test set it."""
    if notify_user is not None:
        serve_user(tenant, **notify_user)
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
    assert "Nothing prices a run before it happens" in rendered
    assert "no pricing API" not in rendered, "Cirro does report spend, after the fact"
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


# --- a launch mails whoever launched it, and says so before it does ---

def notify_line(session_dir, plan_id):
    shown = approved_summary(session_dir, plan_id)
    lines = [line for line in shown.split("\n") if line.strip().startswith("Notify")]
    assert len(lines) == 1, shown
    return lines[0]


def test_a_launch_notifies_the_authenticated_user_by_default(writable_tenant,
                                                             session_dir):
    """What the web UI does. The plugin sent notificationEmails=[] on every launch."""
    planned = plan_run(writable_tenant, session_dir)
    assert planned["ok"], planned
    record = plans.load(session_dir, planned["plan_id"])
    assert record.args["notification_emails"] == [SESSION_EMAIL]
    assert SESSION_EMAIL in notify_line(session_dir, planned["plan_id"])
    assert not [w for w in planned["warnings"] if "email" in w]


def test_the_default_is_disclosed_in_the_text_that_gets_approved(writable_tenant,
                                                                 session_dir):
    """A default that causes mail to be sent has to be in what the person reads."""
    planned = plan_run(writable_tenant, session_dir)
    line = notify_line(session_dir, planned["plan_id"])
    assert SESSION_EMAIL in line and "default" in line
    assert line in planned["rendered"]


def test_the_notified_address_reaches_the_launch_request(writable_tenant, session_dir):
    planned = plan_run(writable_tenant, session_dir)
    assert apply_from(writable_tenant, session_dir, planned)["ok"]
    posted = [c for c in writable_tenant["router"].calls
              if c.request.method == "POST" and c.request.url.path.endswith("/execution")]
    assert json.loads(posted[-1].request.content)["notificationEmails"] == [SESSION_EMAIL]


def test_an_explicit_address_is_used_as_given(writable_tenant, session_dir):
    planned = plan_run(writable_tenant, session_dir,
                       notification_emails=["ops@example.org"])
    record = plans.load(session_dir, planned["plan_id"])
    assert record.args["notification_emails"] == ["ops@example.org"]
    assert "ops@example.org" in notify_line(session_dir, planned["plan_id"])
    assert SESSION_EMAIL not in record.summary


def test_an_explicit_empty_list_is_a_real_opt_out(writable_tenant, session_dir):
    """`args.get(...) or []` could not tell "notify nobody" from silence."""
    planned = plan_run(writable_tenant, session_dir, notification_emails=[])
    record = plans.load(session_dir, planned["plan_id"])
    assert record.args["notification_emails"] == []
    line = notify_line(session_dir, planned["plan_id"])
    assert "nobody" in line and "empty" in line


def test_a_failed_email_lookup_warns_and_does_not_block_the_launch(writable_tenant,
                                                                   session_dir):
    """A notification address is not worth refusing a launch over, and no address is
    invented in its place."""
    import httpx
    writable_tenant["router"].get(f"{API}/users/sminot").mock(
        side_effect=httpx.ConnectError("users endpoint unreachable"))
    planned = plan_run(writable_tenant, session_dir, notify_user=None)
    assert planned["ok"], planned
    record = plans.load(session_dir, planned["plan_id"])
    assert record.args["notification_emails"] == []
    assert "nobody" in notify_line(session_dir, planned["plan_id"])
    assert any("Could not look up the email address" in w for w in planned["warnings"]), \
        planned["warnings"]
    assert apply_from(writable_tenant, session_dir, planned)["ok"]


def test_an_account_with_no_email_on_record_notifies_nobody(writable_tenant,
                                                            session_dir):
    planned = plan_run(writable_tenant, session_dir, notify_user={"email": ""})
    record = plans.load(session_dir, planned["plan_id"])
    assert record.args["notification_emails"] == []
    assert any("no email address on record" in w for w in planned["warnings"]), \
        planned["warnings"]


def test_a_profile_that_turned_analysis_notifications_off_is_not_opted_in(
        writable_tenant, session_dir):
    """The account's own preference, not something to default over."""
    planned = plan_run(writable_tenant, session_dir,
                       notify_user={"notifications": False})
    record = plans.load(session_dir, planned["plan_id"])
    assert record.args["notification_emails"] == []
    assert "turned off" in notify_line(session_dir, planned["plan_id"])


def test_a_resumed_run_notifies_the_authenticated_user_too(writable_tenant,
                                                           session_dir):
    """resume_analysis plans through run_analysis, so the default has to reach it."""
    serve_user(writable_tenant)
    planned = call(writable_tenant, session_dir, "plan", op="resume_analysis",
                   args={"project_id": "proj_a", "resume_dataset_id": "ds_0000",
                         "process": "proc_rnaseq",
                         "source_dataset_ids": ["ds_0000"],
                         "params": {"genome": "GRCh38"}})
    assert planned["ok"], planned
    record = plans.load(session_dir, planned["plan_id"])
    assert record.args["notification_emails"] == [SESSION_EMAIL]
    assert SESSION_EMAIL in notify_line(session_dir, planned["plan_id"])


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


# --- what an approval quotes has to be stable enough to survive a re-plan -----

def _download_plan(tenant, session_dir, destination, free, monkeypatch):
    from cirro_agent.worker import dataset_ops
    monkeypatch.setattr(dataset_ops.shutil, "disk_usage",
                        lambda _probe: type("DU", (), {"free": free})())
    return call(tenant, session_dir, "plan", op="download_files",
                args={"project_id": "proj_a", "dataset_id": "ds_0000",
                      "destination": str(destination)})


def test_ordinary_disk_churn_does_not_change_the_download_disclosure(
        writable_tenant, session_dir, tmp_path, monkeypatch):
    """Free space is reported coarsely because apply re-plans and compares.

    _same_disclosure counts any change to the summary or the warnings as new to
    read, so an exact figure made every download plan PLAN_STALE after unrelated
    disk activity: human_bytes prints one decimal, so 50 MB on a 500 GB volume
    moved it.
    """
    from cirro_agent.worker import gated

    # Chosen so the exact figure straddles a one-decimal boundary: 543.3 GB before
    # the churn, 543.2 GB after it.
    volume = 543_290_000_000
    first = _download_plan(writable_tenant, session_dir, tmp_path,
                           volume, monkeypatch)
    assert first["ok"], first
    churned = _download_plan(writable_tenant, session_dir, tmp_path,
                             volume - 50_000_000, monkeypatch)
    assert churned["ok"], churned

    before = plans.load(session_dir, first["plan_id"])
    after = plans.load(session_dir, churned["plan_id"])
    assert gated._same_disclosure(before, after), (before.summary, after.summary)


def test_the_download_approval_still_states_the_room_available(
        writable_tenant, session_dir, tmp_path, monkeypatch):
    """Coarse is not silent: the figure is in the summary, and a real change moves
    it."""
    from cirro_agent.worker import gated

    roomy = _download_plan(writable_tenant, session_dir, tmp_path,
                           543_210_000_000, monkeypatch)
    assert "540.0 GB" in roomy["rendered"]
    tight = _download_plan(writable_tenant, session_dir, tmp_path,
                           2_000_000_000, monkeypatch)
    assert "2.0 GB" in tight["rendered"]
    assert not gated._same_disclosure(plans.load(session_dir, roomy["plan_id"]),
                                      plans.load(session_dir, tight["plan_id"]))


def test_the_notification_default_survives_a_re_plan(writable_tenant, session_dir):
    """The default is resolved in the planner, so apply re-resolves it and compares.

    A lookup whose rendered text or warnings moved between the two would make every
    launch PLAN_STALE -- the shape that a coarse free-space figure was needed for on
    the download path.
    """
    from cirro_agent.worker import gated

    first = plan_run(writable_tenant, session_dir)
    second = plan_run(writable_tenant, session_dir)
    before, after = (plans.load(session_dir, first["plan_id"]),
                     plans.load(session_dir, second["plan_id"]))
    assert gated._same_disclosure(before, after), (before.summary, after.summary)
    assert apply_from(writable_tenant, session_dir, first)["ok"]


def test_a_changed_notification_address_counts_as_a_changed_disclosure(
        writable_tenant, session_dir):
    """Stable is not the same as unchecked: a different address is new to read."""
    from cirro_agent.worker import gated

    mine = plan_run(writable_tenant, session_dir)
    theirs = plan_run(writable_tenant, session_dir,
                      notify_user={"email": "someone.else@example.org"})
    assert not gated._same_disclosure(plans.load(session_dir, mine["plan_id"]),
                                      plans.load(session_dir, theirs["plan_id"]))


def test_the_samplesheet_preview_is_a_budget_of_diff_lines(
        writable_tenant, session_dir, monkeypatch):
    """The cap is on diff lines, and the remainder is stated from the same constant.

    It was named and documented as a character budget while slicing a list of lines,
    so raising it to a plausible character value would have quoted thousands of lines
    into an approval prompt; the remainder was hardcoded beside it.
    """
    from cirro_agent.worker import dataset_ops
    contents = "".join(f"sample{i},r{i}_1.fastq.gz\n" for i in range(200))

    quoted, remainder = {}, {}
    for budget in (10, 20):
        monkeypatch.setattr(dataset_ops, "DIFF_PREVIEW_LINES", budget)
        planned = call(writable_tenant, session_dir, "plan", op="update_samplesheet",
                       args={"project_id": "proj_a", "dataset_id": "ds_0000",
                             "contents": contents})
        assert planned["ok"], planned
        body = plans.load(session_dir, planned["plan_id"]).summary.split("\n")[2:]
        tail = [line for line in body if "more diff lines" in line]
        assert len(tail) == 1, body
        quoted[budget] = [line for line in body if line not in tail]
        remainder[budget] = int(tail[0].split()[1])

    assert (len(quoted[10]), len(quoted[20])) == (10, 20)
    assert remainder[10] - remainder[20] == 10

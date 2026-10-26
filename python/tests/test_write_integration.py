"""The full plan -> apply cycle against a fake tenant.

Covers the property the whole gate exists for: the operation that runs is the one
whose rendered text was approved, and nothing else can be substituted for it.
"""
import dataclasses
import json
import time

import pytest

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
    from cirro_api_client.v1.models import UserDetail, UserSettings, UserStatus
    payload = UserDetail(
        username="sminot", name="Sam Minot", email=email, organization="Cirro",
        status=UserStatus.ACTIVE, project_assignments=[], global_roles=[],
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


# A real Cirro form groups its parameters, and the group is the key submitted at the
# top level. `wes` is required inside its group and `tools` is an array -- the three
# shapes that a flat parameter surface got wrong.
GROUPED_SPEC = {
    "form": {"type": "object",
             "required": ["analysis_type"],
             "properties": {
                 "analysis_type": {
                     "type": "object",
                     "required": ["analysis_type", "wes"],
                     "properties": {
                         "analysis_type": {"type": "string"},
                         "wes": {"type": "boolean", "default": False},
                         "tools": {"type": "array", "default": ["haplotypecaller"]}}},
                 "workflow_version": {"type": "string", "default": "3.8.1"}}},
    "ui": {},
}


def serve_spec(tenant, spec):
    tenant["router"].get(f"{API}/processes/proc_rnaseq/parameters").respond(json=spec)


def test_a_nested_parameter_sent_flat_names_where_it_belongs(writable_tenant,
                                                             session_dir):
    """The form sets no additionalProperties:false, so a flat key validates, the
    pipeline ignores it, and the run proceeds on a default nobody chose. Three failed
    runs and a wrong bug report came out of exactly this."""
    serve_spec(writable_tenant, GROUPED_SPEC)
    result = plan_run(writable_tenant, session_dir,
                      params={"analysis_type": {"analysis_type": "Germline",
                                                "wes": False},
                              "wes": True})
    assert result["ok"], result
    flagged = [w for w in result["warnings"] if "wes belongs at analysis_type.wes" in w]
    assert flagged, result["warnings"]
    assert "ignore them" in flagged[0]


def test_correct_nested_parameters_raise_nothing(writable_tenant, session_dir):
    """The group containers of a correct run read as undeclared typos, because the
    declared list held only leaf names."""
    serve_spec(writable_tenant, GROUPED_SPEC)
    result = plan_run(writable_tenant, session_dir,
                      params={"analysis_type": {"analysis_type": "Germline",
                                                "wes": False,
                                                "tools": ["haplotypecaller"]}})
    assert result["ok"], result
    assert not [w for w in result["warnings"] if "typo" in w or "ignore" in w], \
        result["warnings"]


def test_a_requirement_inside_a_group_is_refused_at_plan_time(writable_tenant,
                                                              session_dir):
    """A group that is present but incomplete is a schema violation, so it is refused
    outright rather than warned about -- nothing is launched and nothing is charged."""
    serve_spec(writable_tenant, GROUPED_SPEC)
    result = plan_run(writable_tenant, session_dir,
                      params={"analysis_type": {"analysis_type": "Germline"}})
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_QUERY"
    assert "wes" in result["error"]["message"]


def test_the_declared_workflow_version_is_sent_and_disclosed(writable_tenant,
                                                             session_dir):
    """Cirro rejects a launch that omits it while the spec calls it optional, which
    stranded two completed uploads mid-chain."""
    serve_spec(writable_tenant, GROUPED_SPEC)
    result = plan_run(writable_tenant, session_dir,
                      params={"analysis_type": {"analysis_type": "Germline",
                                                "wes": False}})
    assert result["ok"], result
    assert any("workflow_version" in w and "3.8.1" in w for w in result["warnings"]), \
        result["warnings"]
    # Disclosed in the text a person approves, not only in the warning list.
    assert "workflow_version = '3.8.1'" in result["rendered"]


def test_a_supplied_workflow_version_is_left_alone(writable_tenant, session_dir):
    serve_spec(writable_tenant, GROUPED_SPEC)
    result = plan_run(writable_tenant, session_dir,
                      params={"analysis_type": {"analysis_type": "Germline",
                                                "wes": False},
                              "workflow_version": "3.9.0"})
    assert result["ok"], result
    assert not any("workflow_version" in w for w in result["warnings"])
    assert "workflow_version = '3.9.0'" in result["rendered"]


def test_plan_rejects_a_process_that_does_not_exist(writable_tenant, session_dir):
    result = plan_run(writable_tenant, session_dir, process="nf-core/nope")
    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"


def test_plan_refuses_a_dataset_name_and_hands_back_the_id(writable_tenant, session_dir):
    result = plan_run(writable_tenant, session_dir,
                      source_dataset_ids=["RNAseq_batch0_S0"])
    assert result["error"]["code"] == "NAME_REQUIRED_ID"
    assert result["error"]["remediation"]["candidates"][0]["id"] == "ds_0000"


def test_plan_requires_an_input_dataset_for_a_pipeline_something_feeds(
        writable_tenant, session_dir):
    result = plan_run(writable_tenant, session_dir, source_dataset_ids=[])
    assert result["error"]["code"] == "INVALID_QUERY"


# --- which cloud service executes the run ------------------------------------

def test_healthomics_is_named_in_the_approval_text(writable_tenant, session_dir):
    """The rendered text is the only thing the person reads. While environment_type
    was silently dropped it said "Cirro default (AWS Batch)" for a run the caller had
    asked to send to HealthOmics -- a plan that reads as approved for one thing and
    runs as another."""
    result = plan_run(writable_tenant, session_dir, environment_type="AWS_OMICS")
    assert result["ok"], result
    assert "Compute      AWS HealthOmics" in result["rendered"]
    record = json.loads(
        (session_dir.path / "plans" / f"{result['plan_id']}.json").read_text())
    assert record["args"]["environment_type"] == "AWS_OMICS"


def test_batch_is_an_omitted_field_not_an_explicit_value(writable_tenant, session_dir):
    """`resolveEnvironmentType` returns undefined for Batch rather than AWS_BATCH, and
    an explicit value interacts differently with the backend's compute-environment
    conflict check. Asking for AWS_BATCH normalizes to omitting it."""
    for given in (None, "AWS_BATCH"):
        result = plan_run(writable_tenant, session_dir, environment_type=given)
        assert result["ok"], result
        assert "Compute      Cirro default (AWS Batch)" in result["rendered"]
        record = json.loads(
            (session_dir.path / "plans" / f"{result['plan_id']}.json").read_text())
        assert record["args"]["environment_type"] is None


def test_a_pipeline_that_does_not_declare_healthomics_is_refused_in_plan(
        writable_tenant, session_dir):
    """The backend refuses it too, but after approval. Refusing in plan keeps the
    failure on the read-only side of the gate."""
    result = plan_run(writable_tenant, session_dir, process="proc_sarek",
                      environment_type="AWS_OMICS")
    assert result["error"]["code"] == "INVALID_QUERY"
    assert "HEALTHOMICS" in result["error"]["message"]


def test_the_process_mode_vocabulary_is_not_the_request_vocabulary(
        writable_tenant, session_dir):
    """A pipeline declares HEALTHOMICS; a run request asks for AWS_OMICS. Accepting
    the former quietly is how a wrong value looks plausible, so it is named and
    redirected instead."""
    result = plan_run(writable_tenant, session_dir, environment_type="HEALTHOMICS")
    assert result["error"]["code"] == "INVALID_QUERY"
    assert result["error"]["remediation"]["retry_with"] == [
        {"environment_type": "AWS_OMICS"}]


def test_an_environment_this_operation_cannot_set_up_is_refused(
        writable_tenant, session_dir):
    """NONE and LOCAL_AGENT are real EnvironmentType values; the backend rejects the
    first outright and the second is not something run_analysis can arrange."""
    for given in ("NONE", "LOCAL_AGENT", "AWS_LAMBDA"):
        result = plan_run(writable_tenant, session_dir, environment_type=given)
        assert result["error"]["code"] == "INVALID_QUERY", given


def test_an_environment_type_and_a_compute_environment_conflict_in_plan(
        writable_tenant, session_dir):
    """A customer compute environment already names where the run executes, which is
    why the portal sends no environmentType when one is selected. The backend 400s on
    the combination, and that arrives after approval."""
    result = plan_run(writable_tenant, session_dir, environment_type="AWS_OMICS",
                      compute_environment="HPC test")
    assert result["error"]["code"] == "INVALID_QUERY"
    assert "cannot both be set" in result["error"]["message"]


def test_a_process_declaring_no_modes_still_runs_on_batch(writable_tenant, session_dir):
    """The spec makes executionModes nullable and the portal reads a missing value as
    STANDARD. Treating absent as "declares nothing" would strand every process that
    predates the field."""
    result = plan_run(writable_tenant, session_dir, process="proc_sarek")
    assert result["ok"], result
    assert "Compute      Cirro default (AWS Batch)\n" in result["rendered"]


def test_a_batch_run_says_when_healthomics_was_available(writable_tenant, session_dir):
    """The approval is the only thing the person reads. Where the pipeline declares
    both, landing on Batch is a choice, and a line that does not say so reads as
    though there were none -- which is how a run ends up in the wrong place."""
    result = plan_run(writable_tenant, session_dir)
    assert result["ok"], result
    assert ("Compute      Cirro default (AWS Batch) -- this pipeline also declares "
            "HEALTHOMICS") in result["rendered"]
    # Not where the pipeline declares only STANDARD: there is nothing to disclose.
    other = plan_run(writable_tenant, session_dir, process="proc_sarek")
    assert "also declares" not in other["rendered"]


def test_a_pipeline_nothing_feeds_launches_with_no_input_dataset(
        writable_tenant, session_dir):
    """The platform allows it at every layer -- `sourceDatasetIds` is @NotNull rather
    than @NotEmpty, the only server check is `size() > 1 && !allowMultipleSources()`,
    and the portal gates submit on `hasParentProcesses && !sourceDatasets.length`.
    Demanding an input here made every parentless pipeline, `fetchngs` among them,
    unlaunchable through the plugin."""
    result = plan_run(writable_tenant, session_dir, process="proc_fetch",
                      source_dataset_ids=[], params={})
    assert result["ok"], result
    # The approval text is the only thing the person reads, so it has to say that no
    # input is the deliberate case rather than render "0 dataset(s), 0 files, 0 B".
    assert "Inputs       none" in result["rendered"]
    record = json.loads(
        (session_dir.path / "plans" / f"{result['plan_id']}.json").read_text())
    assert record["args"]["source_dataset_ids"] == []


def test_the_parentless_rule_counts_both_directions(writable_tenant, session_dir):
    """Cirro records "this process can feed that one" from either end: `proc_rnaseq`
    declares `parent_process_ids`, and `proc_sarek` is named in `proc_ingest`'s
    `child_process_ids`. The portal unions the two before deciding, and a check that
    read only the first would let an empty input through for half the pipelines."""
    for process in ("proc_rnaseq", "proc_sarek"):
        result = plan_run(writable_tenant, session_dir, process=process,
                          source_dataset_ids=[], params={})
        assert result["error"]["code"] == "INVALID_QUERY", process


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


def test_a_launch_that_emails_given_addresses_is_not_grantable(writable_tenant,
                                                               session_dir):
    """A grant would otherwise send mail on the person's behalf to any address."""
    assert plan_run(writable_tenant, session_dir)["grant_scope"]["op"] == "run_analysis"
    assert plan_run(writable_tenant, session_dir,
                    notification_emails=[])["grant_scope"] is not None
    given = plan_run(writable_tenant, session_dir,
                     notification_emails=["someone@elsewhere.example"])
    assert given["grant_scope"] is None
    assert "emails" in given["not_grantable"]


def test_a_launch_on_more_than_fifty_datasets_is_not_grantable(writable_tenant,
                                                               session_dir):
    """resume_analysis plans through the same function, so this covers it too."""
    inputs = [f"ds_{i:04d}" for i in range(0, 102, 2)]
    fifty = plan_run(writable_tenant, session_dir, source_dataset_ids=inputs[:50])
    assert fifty["grant_scope"]["op"] == "run_analysis"
    many = plan_run(writable_tenant, session_dir, source_dataset_ids=inputs)
    assert many["grant_scope"] is None
    assert "more than 50 input datasets" in many["not_grantable"]


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


def test_a_failed_apply_still_drops_the_snapshot_it_may_have_made_stale(
        writable_tenant, session_dir):
    """A failure can leave a change behind -- an upload registers its dataset before
    the transfer fails, a launch may have started -- and every error points at a fresh
    plan or read, which must not answer from a listing taken before the apply."""
    call(writable_tenant, session_dir, "query", entity="dataset",
         scope={"project_ids": ["proj_a"]})
    planned = plan_run(writable_tenant, session_dir)
    writable_tenant["router"].post(f"{API}/projects/proj_a/execution").respond(
        status_code=502, content=b"")
    assert not apply_from(writable_tenant, session_dir, planned)["ok"]
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


def forge(session_dir, plan_id, mutate):
    """Rewrite a stored plan and recompute its digest -- which needs only hashlib, so
    anything that can write the session folder can do it."""
    path = session_dir.path / "plans" / f"{plan_id}.json"
    stored = json.loads(path.read_text())
    mutate(stored)
    stored["digest"] = plans.canonical_digest(stored)
    path.write_text(json.dumps(stored))
    return stored["digest"]


def writes(tenant):
    return [c.request for c in tenant["router"].calls
            if c.request.method in ("POST", "PUT", "PATCH", "DELETE")]


def test_a_forged_record_with_no_request_is_re_planned_and_refused(writable_tenant,
                                                                   session_dir):
    """An empty `request_args` used to skip the re-plan, so whatever the record said
    ran -- here a delete approved under a rename's text."""
    planned = call(writable_tenant, session_dir, "plan", op="update_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0007",
                         "name": "Tidy name"})

    def to_a_delete(stored):
        stored.update(op="delete_dataset", request_args={},
                      args={"project_id": "proj_a", "dataset_id": "ds_0007"})
    digest = forge(session_dir, planned["plan_id"], to_a_delete)
    before = len(writes(writable_tenant))

    result = apply_from(writable_tenant, session_dir, planned, digest=digest)
    assert result["error"]["code"] == "PLAN_STALE"
    assert len(writes(writable_tenant)) == before


def test_a_relabelled_risk_is_refused(writable_tenant, session_dir):
    """The headline's DESTRUCTIVE mark comes from `risk`, so a delete relabelled as an
    ordinary write would have reached the dialog unmarked."""
    planned = call(writable_tenant, session_dir, "plan", op="delete_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000"})
    digest = forge(session_dir, planned["plan_id"],
                   lambda stored: stored.update(risk="cirro_write"))
    result = apply_from(writable_tenant, session_dir, planned, digest=digest)
    assert result["error"]["code"] == "PLAN_STALE"


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


THROTTLE = {"message": "Too Many Requests"}


def test_a_throttled_read_says_to_wait_not_to_check_service_status(
        fake_tenant, session_dir):
    """Observed live as UPSTREAM_ERROR "UnexpectedStatus: 429" with advice to check
    Cirro's service status. A throttle turns the request away; the service is up."""
    fake_tenant["router"].get(f"{API}/projects").respond(status_code=429, json=THROTTLE)
    result = call(fake_tenant, session_dir, "query", entity="project")
    error = result["error"]
    assert error["code"] == "RATE_LIMITED" and error["severity"] == "retryable"
    assert "GET /api/projects" in error["message"]
    assert "service status" not in (error["remediation"]["user_action"] or "")
    assert error["remediation"]["retry_with"][0]["wait_s"] > 0


def test_a_throttled_write_says_nothing_was_launched_and_to_plan_again(writable_tenant,
                                                                       session_dir):
    """update_samplesheet failed this way three times. The plan is spent by then, so
    the way forward is a fresh plan after a wait, never applying the same one again."""
    planned = plan_run(writable_tenant, session_dir)
    writable_tenant["router"].post(f"{API}/projects/proj_a/execution").respond(
        status_code=429, json=THROTTLE)
    error = apply_from(writable_tenant, session_dir, planned)["error"]
    assert error["code"] == "RATE_LIMITED"
    assert error["remediation"]["what"].startswith("No run was started")
    step = error["remediation"]["retry_with"][0]
    assert (step["tool"], step["op"]) == ("plan", "run_analysis") and step["wait_s"] > 0


def test_an_unreadable_read_reply_names_the_status_and_the_body(fake_tenant,
                                                                session_dir):
    """A bare "JSONDecodeError: Expecting value: line 1 column 1 (char 0)" said
    neither what was asked nor what came back."""
    fake_tenant["router"].get(f"{API}/projects").respond(status_code=200, content=b"")
    error = call(fake_tenant, session_dir, "query", entity="project")["error"]
    assert error["code"] == "UPSTREAM_ERROR"
    assert "GET /api/projects with HTTP 200 and an empty body" in error["message"]


@pytest.mark.parametrize("status,body,said", [
    (200, b"", "accepted (HTTP 200) but the reply had an empty body"),
    (200, b"<html>gateway</html>", "the reply had a body that is not JSON"),
    (502, b"", "Cirro answered HTTP 502 with an empty body"),
])
def test_a_launch_whose_reply_cannot_confirm_it_is_not_reported_as_retryable(
        writable_tenant, session_dir, status, body, said):
    """Observed on ocra twice: a launch failed with a bare JSONDecodeError, retryable,
    with nothing saying whether the run had started. A 2xx means Cirro took it and a
    5xx may follow a launch the backend finished, so planning it again could start a
    second run. The way forward is the read that settles it."""
    planned = plan_run(writable_tenant, session_dir)
    writable_tenant["router"].post(f"{API}/projects/proj_a/execution").respond(
        status_code=status, content=body)
    error = apply_from(writable_tenant, session_dir, planned)["error"]
    assert error["code"] == "OUTCOME_UNKNOWN" and error["severity"] == "agent_action"
    assert said in error["message"]
    assert "second run" in error["remediation"]["what"]
    check = error["remediation"]["retry_with"][0]
    assert check["verb"] == "query" and check["freshness"] == "fresh"
    assert check["filter"] == {"field": "name", "op": "eq", "value": "RNAseq batch 32"}
    assert plans.outcome(session_dir, plans.load(session_dir, planned["plan_id"]))[
        "error"].startswith("OUTCOME_UNKNOWN")


def test_a_launch_whose_reply_body_breaks_off_is_not_reported_as_retryable(
        writable_tenant, session_dir):
    """The headers of a 200 arrive and the connection drops in the body. Describing that
    reply read its content, which raised ResponseNotRead from inside the error handler,
    and the escape was wrapped as a retryable fault -- under a grant, a second launch
    with no prompt."""
    import httpx

    class BrokenOff(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.RemoteProtocolError("peer closed connection mid-body")

    planned = plan_run(writable_tenant, session_dir)
    writable_tenant["router"].post(f"{API}/projects/proj_a/execution").respond(
        status_code=200, stream=BrokenOff())
    error = apply_from(writable_tenant, session_dir, planned)["error"]
    assert error["code"] == "OUTCOME_UNKNOWN"
    assert "did not arrive in full" in error["message"]


def test_a_refused_launch_with_an_unreadable_body_started_nothing(writable_tenant,
                                                                  session_dir):
    """A 4xx is a refusal whatever its body, so this one may be planned again."""
    planned = plan_run(writable_tenant, session_dir)
    writable_tenant["router"].post(f"{API}/projects/proj_a/execution").respond(
        status_code=403, content=b"<html><title>403 Forbidden</title></html>")
    error = apply_from(writable_tenant, session_dir, planned)["error"]
    assert error["code"] == "UPSTREAM_ERROR"
    assert "POST /api/projects/proj_a/execution with HTTP 403" in error["message"]
    assert error["remediation"]["what"].startswith("No run was started")


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
    # Built from the real op so it differs in exactly one thing, the warnings. Stating
    # the fields by hand dropped its declared argument surface, and the re-plan then
    # failed on the arguments rather than on the disclosure this test is about.
    real = dataset_ops.mutations.OPS["update_dataset"]
    monkeypatch.setitem(dataset_ops.mutations.OPS, "update_dataset",
                        dataclasses.replace(real, plan=louder))
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
        # The quoted diff is the indented block; the summary also carries a heading
        # and a trailing statement of what happens to the project's sample metadata.
        body = [line for line in
                plans.load(session_dir, planned["plan_id"]).summary.split("\n")
                if line.startswith("  ")]
        tail = [line for line in body if "more diff lines" in line]
        assert len(tail) == 1, body
        quoted[budget] = [line for line in body if line not in tail]
        remainder[budget] = int(tail[0].split()[1])

    assert (len(quoted[10]), len(quoted[20])) == (10, 20)
    assert remainder[10] - remainder[20] == 10


def test_a_samplesheet_update_re_ingests_the_project_samples(
        writable_tenant, session_dir):
    """Writing the CSV registers nothing on its own.

    The SDK's update_samplesheet puts samplesheet.csv in the dataset prefix and stops;
    re-reading it into the project's sample metadata is a separate endpoint. Without
    it the samplesheet and the samples it describes silently disagree, which is the
    opposite of what anyone editing a samplesheet is asking for.
    """
    planned = call(writable_tenant, session_dir, "plan", op="update_samplesheet",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "contents": "sample,fastq_1\nS1,S1_R1.fastq.gz\n"})
    assert planned["ok"], planned
    assert "re-ingest" in planned["rendered"].lower()
    assert any("overwritten by the samplesheet" in w for w in planned["warnings"])
    stored = plans.load(session_dir, planned["plan_id"])
    assert stored.args["ingest_samples"] is True
    assert any(scope.get("kind") == "samples" for scope in stored.invalidates)


def test_the_samplesheet_executor_writes_the_csv_and_then_ingests(monkeypatch):
    """Driven directly: the CSV write goes to S3, which the fake tenant does not serve.

    Both halves are asserted because either alone is the bug -- writing without
    ingesting leaves the samples stale, and ingesting without writing re-reads the
    old CSV.
    """
    from unittest.mock import MagicMock
    from cirro_api_client.v1.api.datasets import ingest_samples
    from cirro_agent.worker import dataset_ops
    calls = []
    monkeypatch.setattr(ingest_samples, "sync_detailed", lambda **kw: calls.append(kw))
    reader = MagicMock()
    args = {"project_id": "proj_a", "dataset_id": "ds_1",
            "contents": "sample,fastq_1\n", "ingest_samples": True}

    result = dataset_ops.apply_update_samplesheet(reader, args)
    reader.client.datasets.update_samplesheet.assert_called_once_with(
        project_id="proj_a", dataset_id="ds_1", samplesheet="sample,fastq_1\n")
    assert [(c["project_id"], c["dataset_id"]) for c in calls] == [("proj_a", "ds_1")]
    assert result["samples_ingested"] is True

    calls.clear()
    declined = dataset_ops.apply_update_samplesheet(
        MagicMock(), dict(args, ingest_samples=False))
    assert calls == []
    assert declined["samples_ingested"] is False


def test_a_samplesheet_whose_ingest_is_refused_is_still_reported_replaced(monkeypatch):
    """A 429 on the ingest made the audit say update_samplesheet failed, when the new
    CSV had already been written; the change stands, and the ingest is what to redo."""
    from unittest.mock import MagicMock
    from cirro_api_client.v1.api.datasets import ingest_samples
    from cirro_agent.worker import dataset_ops

    def throttled(**kw):
        raise RuntimeError("Unexpected status code: 429")
    monkeypatch.setattr(ingest_samples, "sync_detailed", throttled)
    reader = MagicMock()
    result = dataset_ops.apply_update_samplesheet(reader, {
        "project_id": "proj_a", "dataset_id": "ds_1",
        "contents": "sample,fastq_1\n", "ingest_samples": True})
    reader.client.datasets.update_samplesheet.assert_called_once()
    assert result["samples_ingested"] is False
    assert "429" in result["ingest_failed"]
    assert "replaced" in result["note"]


def test_a_samplesheet_update_can_decline_the_re_ingest(writable_tenant, session_dir):
    """Opting out must say so in the text the person approves, not just omit a line."""
    planned = call(writable_tenant, session_dir, "plan", op="update_samplesheet",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "contents": "sample,fastq_1\nS1,S1_R1.fastq.gz\n",
                         "ingest_samples": False})
    assert planned["ok"], planned
    assert "will NOT be updated" in planned["rendered"]
    stored = plans.load(session_dir, planned["plan_id"])
    assert stored.args["ingest_samples"] is False
    assert not any(scope.get("kind") == "samples" for scope in stored.invalidates)


def test_a_reference_name_with_a_space_is_refused_at_plan_time(
        writable_tenant, session_dir, tmp_path):
    """The SDK raises on this at upload, so the plan read clean and apply died --
    inside a chain that spends the approval and strands the steps before it."""
    reference = tmp_path / "regions.bed"
    reference.write_text("chr20\t0\t20000000\n")
    result = call(writable_tenant, session_dir, "plan", op="upload_reference",
                  args={"project_id": "proj_a", "name": "chr20 0 20Mb",
                        "reference_type": "intervals", "files": [str(reference)]})
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_QUERY"
    assert result["error"]["field"] == "name"
    assert result["error"]["remediation"]["retry_with"][0]["args"]["name"] == \
        "chr20_0_20Mb"


def test_a_transfer_that_cannot_finish_is_flagged_before_the_approval():
    """The plan already counts the bytes, so the ceiling can be stated while the
    person is still deciding rather than discovered when the worker is killed."""
    from cirro_agent import runner
    from cirro_agent.worker import dataset_ops
    budget = runner.TRANSFER_TIMEOUT * dataset_ops.OBSERVED_TRANSFER_BYTES_PER_S
    assert dataset_ops._too_big_to_finish(int(budget * 0.9), "Upload") is None
    warning = dataset_ops._too_big_to_finish(int(budget * 3), "Upload")
    assert warning is not None
    assert "web UI" in warning and "Upload fewer files" in warning


# --- an argument the operation does not read is refused, never dropped -------

def test_an_argument_the_operation_does_not_read_is_refused(
        writable_tenant, session_dir):
    """`execution_mode` is the real case: it planned ok with no warning, rendered
    "Cirro default (AWS Batch)", and the run went to Batch. The plan read as approved
    and described something else."""
    result = plan_run(writable_tenant, session_dir, execution_mode="HEALTHOMICS")
    assert result["error"]["code"] == "INVALID_QUERY"
    assert "execution_mode" in result["error"]["message"]
    # The accepted keys are listed, so the caller can find the one it meant.
    assert "environment_type" in result["error"]["detail"]["accepted"]


def test_a_misspelled_argument_is_refused_rather_than_ignored(
        writable_tenant, session_dir):
    result = plan_run(writable_tenant, session_dir, enviroment_type="AWS_OMICS")
    assert result["error"]["code"] == "INVALID_QUERY"
    assert result["error"]["field"] == "enviroment_type"


def test_the_refusal_offers_the_call_without_the_unknown_argument(
        writable_tenant, session_dir):
    result = plan_run(writable_tenant, session_dir, nonsense=1)
    retry = result["error"]["remediation"]["retry_with"][0]
    assert "nonsense" not in retry
    assert retry["name"] == "RNAseq batch 32"


def test_a_chain_step_is_held_to_its_own_ops_arguments(writable_tenant, session_dir):
    """One approval covers several steps, so a step's arguments are checked against
    the op that step names -- not against `chain`, and not waved through because the
    chain as a whole was approved."""
    result = call(writable_tenant, session_dir, "plan", op="chain", args={"steps": [
        {"op": "update_dataset",
         "args": {"project_id": "proj_a", "dataset_id": "ds_0000",
                  "execution_mode": "HEALTHOMICS"}},
    ]})
    assert result["error"]["code"] == "INVALID_QUERY"
    assert "execution_mode" in result["error"]["message"]


def test_retyping_a_dataset_runs_the_transforms_and_the_sample_ingest(
        writable_tenant, session_dir):
    """The retype is not finished when the record is written.

    ds_0000 is typed as a pipeline, which is exactly the record someone wants to
    correct -- so the type it comes from is unrestricted even though the type it
    becomes must be an ingest process. What the new type derives from the files, and
    the samples read out of them, both have to be redone or the dataset ends up
    claiming a type that nothing about it reflects.
    """
    planned = call(writable_tenant, session_dir, "plan", op="update_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "process": "Paired DNA sequencing"})
    assert planned["ok"], planned
    rendered = planned["rendered"]
    assert "process" in rendered
    assert "nf-core/sarek v3.4 (proc_sarek)" in rendered, \
        "the approval must name the type it is leaving, not just its id"
    assert "Paired DNA sequencing (proc_ingest)" in rendered
    assert "re-run the data transforms" in rendered
    assert "re-ingest samples" in rendered

    stored = plans.load(session_dir, planned["plan_id"])
    edit, = stored.args["edits"]
    assert edit["process_id"] == "proc_ingest"
    assert edit["rerun_transform"] is True
    assert edit["ingest_samples"] is True
    assert any(scope.get("kind") == "samples" for scope in stored.invalidates)
    assert any("does not re-validate the files" in w for w in planned["warnings"])
    assert any("cannot be undone from here" in w for w in planned["warnings"]), \
        "ds_0000 is typed as a pipeline, which this operation will not write back"


def test_a_retype_between_two_ingest_types_is_not_called_one_way(
        writable_tenant, session_dir):
    """The reverse of the above: ds_0001 arrives typed as a pipeline too, so the
    warning must key off the type each dataset actually has and not off retyping."""
    planned = call(writable_tenant, session_dir, "plan", op="update_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "process": "proc_ingest"})
    assert any("cannot be undone" in w for w in planned["warnings"])

    # Retyping it back is what the warning says cannot be done.
    refused = call(writable_tenant, session_dir, "plan", op="update_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "process": "proc_sarek"})
    assert refused["ok"] is False
    assert refused["error"]["code"] == "INVALID_QUERY"


def test_a_dataset_cannot_be_retyped_to_a_pipeline(writable_tenant, session_dir):
    """A pipeline id on a dataset asserts that pipeline produced it."""
    planned = call(writable_tenant, session_dir, "plan", op="update_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "process": "nf-core/rnaseq v3.14"})
    assert planned["ok"] is False
    assert planned["error"]["code"] == "INVALID_QUERY"
    assert "not a data type" in planned["error"]["message"]


def test_the_retype_follow_ups_can_be_declined(writable_tenant, session_dir):
    """Opting out must say so in the text the person approves, not just omit a line."""
    planned = call(writable_tenant, session_dir, "plan", op="update_dataset",
                   args={"project_id": "proj_a", "dataset_id": "ds_0000",
                         "process": "proc_ingest", "ingest_samples": False,
                         "rerun_transform": False})
    assert planned["ok"], planned
    assert "data transforms will NOT be re-run" in planned["rendered"]
    assert "sample metadata will NOT be updated" in planned["rendered"]
    stored = plans.load(session_dir, planned["plan_id"])
    edit, = stored.args["edits"]
    assert edit["rerun_transform"] is False
    assert edit["ingest_samples"] is False
    assert not any(scope.get("kind") == "samples" for scope in stored.invalidates)


def test_retype_follow_ups_run_only_for_the_datasets_that_were_retyped(monkeypatch):
    """Driven directly: both follow-ups are queued routes the fake tenant does not serve.

    A batch that moves three datasets and retypes one must not re-ingest the other
    two -- their samples are correct and the ingest would overwrite hand-edited
    values.
    """
    from unittest.mock import MagicMock
    from cirro_api_client.v1.api.datasets import ingest_samples, rerun_transform
    from cirro_agent.worker import dataset_ops
    queued = []
    monkeypatch.setattr(ingest_samples, "sync_detailed",
                        lambda **kw: queued.append(("ingest", kw["dataset_id"])))
    monkeypatch.setattr(rerun_transform, "sync_detailed",
                        lambda **kw: queued.append(("transform", kw["dataset_id"])))

    def edit(dataset_id, **flags):
        return {"dataset_id": dataset_id, "dataset_name": dataset_id, "name": "n",
                "description": "d", "process_id": "proc_ingest", "folders": [],
                "tags": [], "ingest_samples": False, "rerun_transform": False, **flags}

    result = dataset_ops.apply_update_dataset(
        MagicMock(), {"project_id": "proj_a",
                      "edits": [edit("ds_1", ingest_samples=True, rerun_transform=True),
                                edit("ds_2")]})
    assert queued == [("transform", "ds_1"), ("ingest", "ds_1")], \
        "the transforms are queued before the ingest that reads what they regenerate"
    by_id = {entry["dataset_id"]: entry for entry in result["updated"]}
    assert by_id["ds_1"]["rerun_transform"] is True
    assert by_id["ds_1"]["ingest_samples"] is True
    assert "ingest_samples" not in by_id["ds_2"]
    assert "asynchronously" in result["followups_note"]


def test_a_failed_follow_up_does_not_report_the_retype_as_failed(monkeypatch):
    """The record is already written; calling the operation failed would be untrue."""
    from unittest.mock import MagicMock
    from cirro_api_client.v1.api.datasets import ingest_samples, rerun_transform
    from cirro_agent.worker import dataset_ops

    def explode(**kw):
        raise RuntimeError("503 from the ingest queue")

    monkeypatch.setattr(ingest_samples, "sync_detailed", explode)
    monkeypatch.setattr(rerun_transform, "sync_detailed", lambda **kw: None)

    result = dataset_ops.apply_update_dataset(
        MagicMock(), {"project_id": "proj_a",
                      "edits": [{"dataset_id": "ds_1", "dataset_name": "ds_1",
                                 "name": "n", "description": "d",
                                 "process_id": "proc_ingest", "folders": [], "tags": [],
                                 "ingest_samples": True, "rerun_transform": True}]})
    entry, = result["updated"]
    assert entry["rerun_transform"] is True
    assert "503" in entry["followups_failed"]["ingest_samples"]
    assert "did not start" in result["followups_note"]


def test_the_last_tag_can_be_taken_off_a_project(writable_tenant, session_dir):
    """`[]` is a value, not an omission.

    The shared required-argument check reads an empty list as a missing argument, so
    a project that had been given a tag could not be put back the way it was -- found
    on 2026-09-18 by the live smoke test, which left its own tag behind when the
    restoring half of it was refused.
    """
    planned = call(writable_tenant, session_dir, "plan", op="update_project_tags",
                   args={"project_id": "proj_a", "tags": []})
    assert planned["ok"], planned
    assert "to    []" in planned["rendered"]

    missing = call(writable_tenant, session_dir, "plan", op="update_project_tags",
                   args={"project_id": "proj_a"})
    assert missing["ok"] is False
    assert missing["error"]["code"] == "INVALID_QUERY"


@pytest.mark.parametrize("status,code,safe_to_replan", [
    (400, "UPSTREAM_ERROR", True),
    (504, "OUTCOME_UNKNOWN", False),
])
def test_a_failed_write_points_at_a_re_plan_only_when_cirro_refused_it(
        writable_tenant, session_dir, status, code, safe_to_replan):
    """A re-plan cannot see a write that went through and comes back "unchanged", so
    pointing every failed apply at one invited the same change twice -- rows inserted
    again, a run launched again, under a grant with no prompt. Only a refusal says
    nothing happened; a gateway timeout says nothing either way."""
    planned = call(writable_tenant, session_dir, "plan", op="update_project_tags",
                   args={"project_id": "proj_a", "tags": ["qc"]})
    writable_tenant["router"].put(f"{API}/projects/proj_a:tags").respond(
        status_code=status, json={"message": "no"})
    error = apply_from(writable_tenant, session_dir, planned)["error"]
    assert error["code"] == code
    replans = [step for step in error["remediation"]["retry_with"]
               if step.get("renew_from")]
    assert bool(replans) is safe_to_replan
    if not safe_to_replan:
        assert "twice" in error["remediation"]["what"]


# --- what Cirro would refuse at apply is refused before the approval ----------

def _upload_args(tmp_path, **overrides):
    source = tmp_path / "run"
    source.mkdir(exist_ok=True)
    (source / "sample_R1.fastq.gz").write_text("@read\n")
    return {"project_id": "proj_a", "process": "proc_ingest", "name": "Run 12",
            "source_dir": str(source), **overrides}


def test_tags_given_as_key_value_objects_are_refused_before_the_approval(
        writable_tenant, session_dir, tmp_path):
    """2026-10-13, inside a chain: the objects were stored as given, went onto the wire
    as {"value": {...}}, and Cirro refused the upload ("expected VALUE_STRING") after
    the approval was spent. Cirro has deprecated the tag key, so there is no faithful
    form to normalise to; the refusal hands back the values instead."""
    tags = [{"key": "batch", "value": "3"}, {"key": "site", "value": "FHCC"}]
    result = call(writable_tenant, session_dir, "plan", op="upload_dataset",
                  args=_upload_args(tmp_path, tags=tags))
    assert result["error"]["code"] == "INVALID_QUERY"
    assert result["error"]["field"] == "tags"
    assert result["error"]["remediation"]["retry_with"] == [{"tags": ["3", "FHCC"]}]

    chained = call(writable_tenant, session_dir, "plan", op="chain", args={"steps": [
        {"label": "upload", "op": "upload_dataset",
         "args": _upload_args(tmp_path, tags=tags)}]})
    assert chained["error"]["code"] == "INVALID_QUERY"
    assert chained["error"]["field"] == "tags"


def test_every_op_that_writes_tags_holds_them_to_a_list_of_strings(
        writable_tenant, session_dir):
    """update_dataset did not reach the wire with an object: it crashed planning, on
    sorting and hashing the tag list. A bare string would have been split into
    characters by `list()`."""
    edited = call(writable_tenant, session_dir, "plan", op="update_dataset",
                  args={"project_id": "proj_a", "dataset_id": "ds_0000",
                        "tags": [{"value": "qc-pass"}, {"value": "batch-3"}]})
    assert edited["error"]["code"] == "INVALID_QUERY"
    assert edited["error"]["remediation"]["retry_with"] == [
        {"tags": ["qc-pass", "batch-3"]}]

    bare = call(writable_tenant, session_dir, "plan", op="update_dataset",
                args={"project_id": "proj_a", "dataset_id": "ds_0000", "tags": "qc"})
    assert bare["error"]["remediation"]["retry_with"] == [{"tags": ["qc"]}]

    project = call(writable_tenant, session_dir, "plan", op="update_project_tags",
                   args={"project_id": "proj_a", "tags": [{"key": "k", "value": "v"}]})
    assert project["error"]["code"] == "INVALID_QUERY"

    launched = plan_run(writable_tenant, session_dir, tags=[{"value": "rnaseq"}])
    assert launched["error"]["code"] == "INVALID_QUERY"


def test_a_dataset_name_cirro_would_refuse_is_refused_before_the_approval(
        writable_tenant, session_dir, tmp_path):
    """2026-09-21: "uploadDatasetRequest.name: size must be between 0 and 100", after
    the approval. Cirro-backend puts `@Size(max = 100)` on the name of every request
    that creates or renames a dataset, so all four are held to it."""
    too_long = "RNAseq " + "x" * 94
    assert len(too_long) == 101
    for op, args in [
            ("upload_dataset", _upload_args(tmp_path)),
            ("update_dataset", {"project_id": "proj_a", "dataset_id": "ds_0000"}),
            ("import_public", {"project_id": "proj_a", "public_ids": ["SRR9984183"]}),
            ("run_analysis", {"project_id": "proj_a", "process": "proc_rnaseq",
                              "source_dataset_ids": ["ds_0000"],
                              "params": {"genome": "GRCh38"}})]:
        result = call(writable_tenant, session_dir, "plan", op=op,
                      args={**args, "name": too_long})
        assert result["error"]["code"] == "INVALID_QUERY", (op, result)
        assert result["error"]["field"] == "name", op
        suggested = result["error"]["remediation"]["retry_with"][0]["name"]
        assert len(suggested) == 100 and too_long.startswith(suggested), op

    at_the_limit = call(writable_tenant, session_dir, "plan", op="update_dataset",
                        args={"project_id": "proj_a", "dataset_id": "ds_0000",
                              "name": "x" * 100})
    assert at_the_limit["ok"], at_the_limit


def test_the_name_limit_counts_what_java_counts():
    """`@Size` counts UTF-16 code units, so a character outside the Basic Multilingual
    Plane is two of them -- a name Python measures at 100 can be 101 to Cirro."""
    from cirro_agent import errors
    from cirro_agent.worker import mutations
    mutations._check_dataset_name("x" * 100)
    with pytest.raises(errors.InvalidQuery) as raised:
        mutations._check_dataset_name("x" * 99 + "\N{DNA DOUBLE HELIX}")
    assert raised.value.retry_with == [{"name": "x" * 99}]
    with pytest.raises(errors.InvalidQuery):
        mutations._check_dataset_name("   ")


def test_the_import_download_method_is_checked_shown_and_sent(
        writable_tenant, session_dir):
    """2026-10-14: the string went onto ImportDataRequest as given, and serializing the
    request died at apply with "'str' object has no attribute 'value'". The enum cannot
    be the check either: it maps any unknown string to UNKNOWN instead of raising."""
    import httpx
    sent = []

    def accept(request):
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "ds_imported", "message": "created"})

    writable_tenant["router"].post(f"{API}/projects/proj_a/datasets/import").mock(
        side_effect=accept)
    args = {"project_id": "proj_a", "name": "SRA import", "public_ids": ["SRR9984183"]}

    refused = call(writable_tenant, session_dir, "plan", op="import_public",
                   args={**args, "download_method": "UNKNOWN"})
    assert refused["error"]["code"] == "INVALID_QUERY"
    assert refused["error"]["field"] == "download_method"
    assert {"download_method": "aspera"} in refused["error"]["remediation"]["retry_with"]

    planned = call(writable_tenant, session_dir, "plan", op="import_public",
                   args={**args, "download_method": "Aspera"})
    assert planned["ok"], planned
    assert "Download via  aspera" in approved_summary(session_dir, planned["plan_id"])
    applied = apply_from(writable_tenant, session_dir, planned)
    assert applied["ok"], applied
    assert applied["result"]["created_dataset_id"] == "ds_imported"
    assert sent[-1]["downloadMethod"] == "aspera"

    default = call(writable_tenant, session_dir, "plan", op="import_public", args=args)
    assert "Download via  sratools (the default)" in approved_summary(
        session_dir, default["plan_id"])


# --- a transfer that fails partway says what it left behind ------------------

def _uploading(tmp_path, monkeypatch, transfer):
    """A recording Reader whose `upload_files` runs the SDK's own transfer loop, with
    each file's S3 call answered by `transfer(key, attempt)` instead of by S3."""
    from unittest.mock import MagicMock
    from cirro import file_utils
    from cirro.clients.s3 import S3Client
    attempts = {}

    class FakeTransfer:
        def upload_file(self, filename, bucket, key, callback, extra_args):
            attempts[key] = attempts.get(key, 0) + 1
            transfer(key, attempts[key])

    s3 = S3Client.__new__(S3Client)
    s3._upload_args = {}
    s3._get_transfer = FakeTransfer

    def upload_files(project_id, dataset_id, directory, files):
        file_utils.upload_directory(directory, files, {}, s3, "bucket",
                                    f"datasets/{dataset_id}/data", threads=4)

    monkeypatch.setattr(file_utils.time, "sleep", lambda seconds: None)
    files = [f"run/sample_{i:02d}.wiff" for i in range(10)]
    for name in files:
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_bytes(b"x")
    reader = MagicMock()
    reader.client.datasets.create.return_value.id = "ds_partial"
    reader.client.datasets.upload_files.side_effect = upload_files
    args = {"project_id": "proj_a", "process_id": "proc_ingest", "name": "Run 12",
            "description": "", "source_dir": str(tmp_path), "files": files,
            "folders": [], "tags": [], "n_files": len(files), "total_bytes": len(files)}
    return reader, args


def test_a_failed_upload_names_the_dataset_it_left_and_why(tmp_path, monkeypatch):
    """2026-10-26: "Failed to upload 118 of 133 files:" and then 118 lines of a path and
    a colon. The SDK prints only str(exc), which a cancelled transfer leaves empty. The
    person needs the dataset the attempt left behind, how much of it arrived and what
    kind of failure stopped the rest, in a message that does not grow with the files.

    Driven through the SDK's own transfer loop, so this also fails if the SDK stops
    sending each file through `S3Client.upload_file`, which is where failures are seen.
    """
    from boto3.exceptions import S3UploadFailedError
    from cirro.clients.s3 import S3Client
    from s3transfer.exceptions import CancelledError
    from cirro_agent import errors
    from cirro_agent.worker import dataset_ops

    def transfer(key, attempt):
        if key.endswith(("_00.wiff", "_01.wiff")):
            return
        if key.endswith("_02.wiff"):
            if attempt == 1:
                raise S3UploadFailedError("throttled")  # retried by the SDK, then sent
            return
        raise CancelledError()

    reader, args = _uploading(tmp_path, monkeypatch, transfer)
    original = S3Client.upload_file
    with pytest.raises(errors.UpstreamError) as raised:
        dataset_ops.apply_upload_dataset(reader, args)
    failure = raised.value
    assert S3Client.upload_file is original, "the wrapper must not outlive the upload"
    assert "Uploaded 3 of 10 files into 'Run 12' (ds_partial); 7 failed" in \
        failure.message
    assert "7 x CancelledError, e.g. run/sample_03.wiff" in failure.message
    assert failure.message.count(".wiff") == dataset_ops.UPLOAD_FAILURE_EXAMPLES
    assert failure.detail["n_failed"] == 7
    assert failure.retry_with[0] == {
        "op": "delete_dataset",
        "args": {"project_id": "proj_a", "dataset_id": "ds_partial"}}
    assert "ds_partial" in failure.user_action and "Delete it" in failure.user_action


def test_a_failed_upload_with_no_file_to_blame_does_not_guess_a_count(
        tmp_path, monkeypatch):
    from cirro_agent import errors
    from cirro_agent.worker import dataset_ops
    reader, args = _uploading(tmp_path, monkeypatch, lambda key, attempt: None)
    reader.client.datasets.upload_files.side_effect = TimeoutError()
    with pytest.raises(errors.UpstreamError) as raised:
        dataset_ops.apply_upload_dataset(reader, args)
    assert "TimeoutError" in raised.value.message
    assert "is not known" in raised.value.message
    assert raised.value.retry_with[0]["args"]["dataset_id"] == "ds_partial"

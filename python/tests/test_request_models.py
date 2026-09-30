"""Every request object an apply path builds must actually be constructible.

The generated models are whole-record replacements with required fields, not patches.
Three apply paths were silently broken this way -- `UpdateDatasetRequest()` needs all
four of name/description/process_id/tags, `ProjectInput` needs a budget and contacts,
`ShareInput` needs classification_ids and conditions. A TypeError there only surfaces
on a live call, so exercise each apply function against a recording double instead.
"""
from unittest.mock import MagicMock

import pytest

from cirro_agent.worker import dataset_ops, mutations, project_ops, share_ops

# op -> args a planner would realistically produce (resolved, IDs only).
APPLY_CASES = {
    "update_dataset": {
        "project_id": "proj_a",
        "edits": [{"dataset_id": "ds_1", "dataset_name": "n", "name": "n",
                   "description": "d", "process_id": "proc_1",
                   "folders": ["Immuno"], "tags": ["qc-pass"]}]},
    "delete_dataset": {"project_id": "proj_a", "dataset_id": "ds_1",
                       "dataset_name": "n"},
    "update_samplesheet": {"project_id": "proj_a", "dataset_id": "ds_1",
                           "contents": "sample,fastq_1\n"},
    "run_analysis": {
        "project_id": "proj_a", "process_id": "proc_1", "name": "run",
        "source_dataset_ids": ["ds_1"], "params": {"genome": "GRCh38"},
        # The planner resolves this to the session's own address unless the caller
        # asked otherwise, so a resolved launch normally carries one.
        "description": "", "notification_emails": ["sminot@cirro.test"],
        "compute_environment_id": None, "resume_dataset_id": None,
        "environment_type": None, "source_sample_ids": []},
    # Shares apply_run_analysis, so its request construction was only ever covered
    # transitively -- and the chain guard that checks a declared output is really
    # returned skips any op absent from here, so resume_analysis' `created_dataset_id`
    # went unchecked while being declared.
    "resume_analysis": {
        "project_id": "proj_a", "process_id": "proc_1", "name": "run",
        "source_dataset_ids": ["ds_1"], "params": {"genome": "GRCh38"},
        "description": "", "notification_emails": ["sminot@cirro.test"],
        "compute_environment_id": None, "resume_dataset_id": "ds_prior",
        "source_sample_ids": []},
    "stop_analysis": {"project_id": "proj_a", "dataset_id": "ds_1"},
    "import_public": {"project_id": "proj_a", "name": "n",
                      "public_ids": ["SRR1"], "description": "",
                      "download_method": None},
    "create_project": {
        "name": "P", "description": "d", "billing_account_id": "bill_1",
        "budget_amount": 1000, "budget_period": "MONTHLY",
        "contacts": [{"name": "A", "organization": "O", "email": "e@x.org",
                      "phone": "1"}],
        "classification_ids": [], "tags": ["t"]},
    "update_project_tags": {"project_id": "proj_a", "tags": ["t"]},
    "archive_project": {"project_id": "proj_a", "archive": True},
    "set_user_role": {"project_id": "proj_a", "username": "u", "role": "CONTRIBUTOR",
                      "suppress_notification": False},
    "invite_user": {"name": "A", "email": "e@x.org", "organization": "O"},
    "update_sample": {"project_id": "proj_a", "sample_id": "smp_1", "name": "S",
                      "metadata": {"condition": "Tumor"}},
    "create_share": {
        "project_id": "proj_a", "name": "S", "description": "d",
        "shared_project_ids": ["proj_b"], "keywords": [], "classification_ids": [],
        "conditions": [{"field": "process_id", "value": "proc_1"}],
        "is_view_restricted": False},
    "delete_share": {"project_id": "proj_a", "share_id": "shr_1", "share_name": "S"},
    "subscribe_share": {"project_id": "proj_a", "share_id": "shr_1", "subscribe": True},
    "unsubscribe_share": {"project_id": "proj_a", "share_id": "shr_1",
                          "subscribe": False},
    "create_workspace": {
        "project_id": "proj_a", "name": "W", "environment_id": "env_1",
        "description": "d", "mounted_datasets": [],
        "container_image_uri": "ghcr.io/org/image:1.0", "cpu": 4, "memory_gi_b": 16,
        "gpu": None, "volume_size_gi_b": 50, "sharing_type": "PRIVATE",
        "auto_stop_timeout": None},
    "start_workspace": {"project_id": "proj_a", "workspace_id": "ws_1",
                        "action": "start", "workspace_name": "W"},
    "stop_workspace": {"project_id": "proj_a", "workspace_id": "ws_1",
                       "action": "stop", "workspace_name": "W"},
    "delete_workspace": {"project_id": "proj_a", "workspace_id": "ws_1",
                         "action": "delete", "workspace_name": "W"},
    "create_process": {
        "process_id": "process-test-1", "name": "Test Pipeline", "description": "d",
        "executor": "NEXTFLOW", "category": "Community", "config_source": "STORAGE",
        "custom_settings": None, "repository_path": "org/wf", "version": "v1.0.0",
        "entry_point": "main.nf", "executor_version": None,
        "repository_type": "GITHUB_PUBLIC", "parent_process_ids": [],
        "child_process_ids": [], "uses_sample_sheet": False,
        "allow_multiple_sources": True, "file_mapping_rules": [],
        # What the planner resolves to when nobody names a mode: say nothing and let
        # Cirro keep its own default.
        "execution_modes": None,
        "design_spec": "/tmp/design-spec.md"},
    "update_process": {
        "process_id": "process-test-1", "changes": {"version": "v2.0.0"},
        "current": {"name": "Test Pipeline", "description": "d",
                    "category": "Community", "executor": "NEXTFLOW",
                    "config_source": "WITH_PROCESS",
                    "code": {"repository_path": "org/wf", "version": "v1.0.0",
                             "repository_type": "GITHUB_PUBLIC",
                             "entry_point": "main.nf", "executor_version": None},
                    "settings": {"repository": "org/wf", "branch": "main",
                                 "folder": ".cirro"},
                    "parent_process_ids": [], "child_process_ids": [],
                    "uses_sample_sheet": False, "allow_multiple_sources": True,
                    # A HealthOmics-capable pipeline, because that is the declaration
                    # an unrelated edit must not strip.
                    "execution_modes": ["STANDARD", "HEALTHOMICS"]}},
    "sync_process_config": {"process_id": "process-test-1"},
    "archive_process": {"process_id": "process-test-1", "name": "Test Pipeline"},
}


def _reader() -> MagicMock:
    """A Reader whose client records calls instead of making them."""
    reader = MagicMock()
    reader.cost = MagicMock()
    return reader


@pytest.mark.parametrize("op", sorted(APPLY_CASES))
def test_apply_builds_a_valid_request(op):
    """A required-field omission raises TypeError inside the model constructor."""
    handler = mutations.OPS[op].apply
    handler(_reader(), dict(APPLY_CASES[op]))


def test_every_registered_op_is_covered_here():
    """So a new op cannot be added without exercising its request construction."""
    covered = set(APPLY_CASES) | {"upload_dataset", "upload_reference", "download_files",
                                  "unarchive_project", "update_project_schema",
                                  # Local writes: they touch the filesystem and git, not
                                  # the SDK, so there is no request object to build.
                                  "write_process_config", "commit_process_config",
                                  # A chain builds no request of its own; it dispatches
                                  # to other ops, and is covered in test_chain.py.
                                  "chain",
                                  # Exercised end to end in test_pipeline_config.py
                                  # against a fake tenant, where the revision API's
                                  # response shape matters more than the request.
                                  "upload_process_config",
                                  # Exercised end to end in test_dashboards.py against a
                                  # fake tenant: apply re-reads the record it writes.
                                  "create_dashboard", "edit_dashboard", "delete_dashboard",
                                  # Exercised end to end in test_sheets.py against a fake
                                  # tenant: apply re-reads the rows and sheet it writes.
                                  "create_sheet", "update_sheet", "delete_sheet",
                                  "insert_rows", "update_rows", "delete_rows",
                                  "refresh_sheet"}
    missing = set(mutations.OPS) - covered
    assert not missing, (
        f"these operations build requests that nothing exercises: {sorted(missing)}")


def test_update_dataset_sends_every_required_field():
    """The specific trap: the request replaces the record, so a missing field blanks it."""
    reader = _reader()
    dataset_ops.apply_update_dataset(reader, dict(APPLY_CASES["update_dataset"]))
    request = reader.client.datasets.update.call_args.kwargs["request"]
    assert reader.client.datasets.update.call_count == 1
    assert request.name == "n"
    assert request.description == "d"
    assert request.process_id == "proc_1", \
        "omitting process_id would change the dataset's type"
    values = [t.value for t in request.tags]
    assert "folder://Immuno" in values, "folder membership must survive a tag write"
    assert "qc-pass" in values


def test_run_analysis_sends_the_notification_address():
    """The address the approval disclosed is the one on the wire.

    `notificationEmails` is a required field of RunAnalysisRequest, and the plugin
    always sent [] -- so no launch made through it ever notified anyone, which is the
    divergence from the web UI this default fixes.
    """
    reader = _reader()
    mutations.OPS["run_analysis"].apply(reader, dict(APPLY_CASES["run_analysis"]))
    request = reader.client.execution.run_analysis.call_args.kwargs["request"]
    assert request.notification_emails == ["sminot@cirro.test"]
    assert request.to_dict()["notificationEmails"] == ["sminot@cirro.test"]


def test_run_analysis_puts_the_environment_type_on_the_wire():
    """`environmentType` is in the API spec and the pinned cirro-api-client does not
    model it on this request, so it travels as an extra property that `to_dict` merges.
    Asserting the serialized body, not the attribute, is the point: an attribute the
    model drops on the floor is exactly the silent redirect this feature exists to
    prevent."""
    reader = _reader()
    args = dict(APPLY_CASES["run_analysis"], environment_type="AWS_OMICS")
    mutations.OPS["run_analysis"].apply(reader, args)
    request = reader.client.execution.run_analysis.call_args.kwargs["request"]
    assert request.to_dict()["environmentType"] == "AWS_OMICS"


def test_run_analysis_omits_the_environment_type_for_batch():
    """Batch is an absent field, not AWS_BATCH: that is what the portal sends, and an
    explicit value interacts differently with the compute-environment conflict check."""
    reader = _reader()
    mutations.OPS["run_analysis"].apply(reader, dict(APPLY_CASES["run_analysis"]))
    request = reader.client.execution.run_analysis.call_args.kwargs["request"]
    assert "environmentType" not in request.to_dict()


def test_create_project_sends_the_budget():
    reader = _reader()
    project_ops.apply_create_project(reader, dict(APPLY_CASES["create_project"]))
    request = reader.client.projects.create.call_args.kwargs["request"]
    assert request.settings.budget_amount == 1000
    assert request.settings.budget_period == "MONTHLY"
    assert request.contacts[0].email == "e@x.org"


def test_create_share_sends_conditions():
    reader = _reader()
    share_ops.apply_create_share(reader, dict(APPLY_CASES["create_share"]))
    request = reader.client.shares.create.call_args.kwargs["share"]
    assert request.conditions[0].field == "process_id"
    assert request.classification_ids == []


def test_create_workspace_sends_the_container_image():
    reader = _reader()
    share_ops.apply_create_workspace(reader, dict(APPLY_CASES["create_workspace"]))
    request = reader.client.workspaces.create.call_args.kwargs["workspace"]
    assert request.compute_config.container_image_uri == "ghcr.io/org/image:1.0"
    assert request.compute_config.cpu == 4
    assert str(request.sharing_type) == "PRIVATE"


def test_create_process_sends_config_source_and_settings():
    """The one field that decides where a pipeline reads its configuration."""
    from cirro_api_client.v1.models import ConfigSource
    reader = _reader()
    args = dict(APPLY_CASES["create_process"])
    args["config_source"] = "WITH_PROCESS"
    args["custom_settings"] = {"repository": "org/wf", "branch": "dev",
                               "folder": "config"}
    mutations.OPS["create_process"].apply(reader, args)
    request = reader.client.processes.create_custom_process.call_args.kwargs["process"]
    assert request.config_source == ConfigSource.WITH_PROCESS
    assert request.custom_settings.repository == "org/wf"
    assert request.custom_settings.branch == "dev"
    assert request.custom_settings.folder == "config"
    assert request.pipeline_code.version == "v1.0.0"


def test_no_pinned_engine_version_omits_the_key_instead_of_sending_null():
    """PipelineCode.to_dict() drops executorVersion only when it is UNSET.

    Passing None put an explicit `"executorVersion": null` on the wire, which asks for
    no engine version rather than for the platform default. The default is to leave it
    unpinned, so the key has to be absent -- both where a create resolves it from the
    design spec and where an update carries it forward from the record.
    """
    reader = _reader()
    args = dict(APPLY_CASES["create_process"])
    assert args["executor_version"] is None, "the planner resolves it to None by default"
    mutations.OPS["create_process"].apply(reader, args)
    created = reader.client.processes.create_custom_process.call_args.kwargs["process"]
    assert "executorVersion" not in created.pipeline_code.to_dict()

    reader = _reader()
    mutations.OPS["update_process"].apply(reader, dict(APPLY_CASES["update_process"]))
    updated = reader.client.processes.update_custom_process.call_args.kwargs["process"]
    assert "executorVersion" not in updated.pipeline_code.to_dict()


def test_a_pinned_engine_version_is_still_sent():
    """Pinning is a recovery from a compilation failure, and must reach the wire."""
    reader = _reader()
    mutations.OPS["create_process"].apply(
        reader, dict(APPLY_CASES["create_process"], executor_version="24.10.5"))
    request = reader.client.processes.create_custom_process.call_args.kwargs["process"]
    assert request.pipeline_code.to_dict()["executorVersion"] == "24.10.5"

    reader = _reader()
    mutations.OPS["update_process"].apply(reader, dict(
        APPLY_CASES["update_process"],
        changes={"executor_version": "24.10.5"}))
    request = reader.client.processes.update_custom_process.call_args.kwargs["process"]
    assert request.pipeline_code.to_dict()["executorVersion"] == "24.10.5"


def test_update_process_resolves_every_field_from_the_current_record():
    """CustomProcessInput replaces the record, so an omitted field is a deletion."""
    reader = _reader()
    mutations.OPS["update_process"].apply(
        reader, dict(APPLY_CASES["update_process"]))
    request = reader.client.processes.update_custom_process.call_args.kwargs["process"]
    assert request.pipeline_code.version == "v2.0.0", "the requested change"
    assert request.name == "Test Pipeline", "unchanged fields must survive"
    assert request.pipeline_code.entry_point == "main.nf"
    assert request.custom_settings.repository == "org/wf"
    assert str(request.config_source) == "WITH_PROCESS"
    assert request.to_dict()["executionModes"] == ["STANDARD", "HEALTHOMICS"], \
        "editing the version must not strip the HealthOmics declaration"


def test_execution_modes_reach_the_wire_and_are_omitted_when_unnamed():
    """A registration that turns HealthOmics on is only one that turns it on if the key
    is really on the wire -- and one that names no mode has to leave the key out rather
    than send a null the server would read as a declaration."""
    reader = _reader()
    mutations.OPS["create_process"].apply(reader, dict(APPLY_CASES["create_process"]))
    request = reader.client.processes.create_custom_process.call_args.kwargs["process"]
    assert "executionModes" not in request.to_dict()

    reader = _reader()
    mutations.OPS["create_process"].apply(reader, dict(
        APPLY_CASES["create_process"],
        execution_modes=["STANDARD", "HEALTHOMICS"]))
    request = reader.client.processes.create_custom_process.call_args.kwargs["process"]
    assert request.to_dict()["executionModes"] == ["STANDARD", "HEALTHOMICS"]


def test_an_update_can_change_the_declared_execution_modes():
    reader = _reader()
    mutations.OPS["update_process"].apply(reader, dict(
        APPLY_CASES["update_process"],
        changes={"execution_modes": ["STANDARD"]}))
    request = reader.client.processes.update_custom_process.call_args.kwargs["process"]
    assert request.to_dict()["executionModes"] == ["STANDARD"]

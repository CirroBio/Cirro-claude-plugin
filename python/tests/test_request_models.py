"""Every request object an apply path builds must actually be constructible.

The generated models are whole-record replacements with required fields, not patches.
Three apply paths were silently broken this way -- `UpdateDatasetRequest()` needs all
four of name/description/process_id/tags, `ProjectInput` needs a budget and contacts,
`ShareInput` needs classification_ids and conditions. A TypeError there only surfaces
on a live call, so exercise each apply function against a recording double instead.
"""
from unittest.mock import MagicMock

import pytest

from cirro_agent.worker import mutations

# op -> args a planner would realistically produce (resolved, IDs only).
APPLY_CASES = {
    "update_dataset": {
        "project_id": "proj_a", "dataset_id": "ds_1", "name": "n",
        "description": "d", "process_id": "proc_1",
        "folders": ["Immuno"], "tags": ["qc-pass"]},
    "delete_dataset": {"project_id": "proj_a", "dataset_id": "ds_1",
                       "dataset_name": "n"},
    "update_samplesheet": {"project_id": "proj_a", "dataset_id": "ds_1",
                           "contents": "sample,fastq_1\n"},
    "run_analysis": {
        "project_id": "proj_a", "process_id": "proc_1", "name": "run",
        "source_dataset_ids": ["ds_1"], "params": {"genome": "GRCh38"},
        "description": "", "notification_emails": [],
        "compute_environment_id": None, "resume_dataset_id": None,
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
                                  "resume_analysis"}
    missing = set(mutations.OPS) - covered
    assert not missing, (
        f"these operations build requests that nothing exercises: {sorted(missing)}")


def test_update_dataset_sends_every_required_field():
    """The specific trap: the request replaces the record, so a missing field blanks it."""
    reader = _reader()
    mutations.apply_update_dataset(reader, dict(APPLY_CASES["update_dataset"]))
    request = reader.client.datasets.update.call_args.kwargs["request"]
    assert request.name == "n"
    assert request.description == "d"
    assert request.process_id == "proc_1", \
        "omitting process_id would change the dataset's type"
    values = [t.value for t in request.tags]
    assert "folder://Immuno" in values, "folder membership must survive a tag write"
    assert "qc-pass" in values


def test_create_project_sends_the_budget():
    reader = _reader()
    mutations.apply_create_project(reader, dict(APPLY_CASES["create_project"]))
    request = reader.client.projects.create.call_args.kwargs["request"]
    assert request.settings.budget_amount == 1000
    assert request.settings.budget_period == "MONTHLY"
    assert request.contacts[0].email == "e@x.org"


def test_create_share_sends_conditions():
    reader = _reader()
    mutations.apply_create_share(reader, dict(APPLY_CASES["create_share"]))
    request = reader.client.shares.create.call_args.kwargs["share"]
    assert request.conditions[0].field == "process_id"
    assert request.classification_ids == []


def test_create_workspace_sends_the_container_image():
    reader = _reader()
    mutations.apply_create_workspace(reader, dict(APPLY_CASES["create_workspace"]))
    request = reader.client.workspaces.create.call_args.kwargs["workspace"]
    assert request.compute_config.container_image_uri == "ghcr.io/org/image:1.0"
    assert request.compute_config.cpu == 4
    assert str(request.sharing_type) == "PRIVATE"

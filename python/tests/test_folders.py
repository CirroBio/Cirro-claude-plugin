"""Dataset folders.

Cirro stores folder membership as a tag prefixed `folder://`. That is an
implementation detail of the platform: the plugin reports `folders`, keeps folder
entries out of `tags`, and filters in folder language. The write path matters most --
the API replaces the whole tag list, so a tags-only update would silently move a
dataset out of its folder.
"""
from conftest import approved_summary

from cirro_agent import plans

from cirro_agent import query as Q, schema
from cirro_agent.worker import rows as R


class FakeTags:
    def __init__(self, values):
        self.tags = [type("T", (), {"value": v})() for v in values]


def test_folder_tags_are_split_out_of_the_tag_list():
    folders, plain = R.split_folder_tags(["folder://Immuno", "qc-pass", "clinical"])
    assert folders == ["Immuno"]
    assert plain == ["qc-pass", "clinical"]


def test_nested_folder_names_survive():
    folders, _ = R.split_folder_tags(["folder://Immuno/Batch31"])
    assert folders == ["Immuno/Batch31"]


def test_a_bare_prefix_is_not_a_folder():
    folders, plain = R.split_folder_tags(["folder://", "folder:///", "real"])
    assert folders == []
    assert plain == ["real"]


def test_folder_tag_round_trips():
    for name in ("Immuno", "Immuno/Batch31", " spaced "):
        folders, _ = R.split_folder_tags([R.folder_tag(name)])
        assert folders == [name.strip().strip("/")]


def test_tags_never_contain_a_folder_entry(fake_tenant):
    from datetime import datetime, timezone
    obj = type("D", (), {})()
    obj.id, obj.name = "ds_1", "n"
    obj.tags = [type("T", (), {"value": v})()
                for v in ("folder://Immuno", "qc-pass")]
    row = R.dataset(obj, now=datetime.now(timezone.utc))
    assert row["folders"] == ["Immuno"]
    assert row["folder"] == "Immuno"
    assert row["tags"] == ["qc-pass"]
    assert not any(str(t).startswith("folder://") for t in row["tags"])


def test_multiple_folders_leave_the_scalar_null():
    from datetime import datetime, timezone
    obj = type("D", (), {})()
    obj.id, obj.name = "ds_1", "n"
    obj.tags = [type("T", (), {"value": v})()
                for v in ("folder://A", "folder://B")]
    row = R.dataset(obj, now=datetime.now(timezone.utc))
    assert row["folders"] == ["A", "B"]
    assert row["folder"] is None, "the scalar is only for the unambiguous single case"


def test_folder_shorthand_filters_in_folder_language():
    parsed = Q.parse_shorthand("folder:Immuno")
    assert parsed == {"field": "folders", "op": "has_folder", "value": "Immuno"}


def test_has_folder_matches():
    ctx = Q.Ctx(username="u")
    data = [{"id": "a", "folders": ["Immuno"]}, {"id": "b", "folders": []},
            {"id": "c", "folders": ["Immuno", "Other"]}]
    node = Q.resolve_filter(Q.parse_shorthand("folder:Immuno"), schema.DATASET, ctx)
    assert {r["id"] for r in Q.apply_filter(data, node, schema.DATASET, ctx)} == {"a", "c"}


def test_folders_is_faceted_ahead_of_tags():
    priority = schema.DATASET.facet_priority
    assert "folders" in priority
    assert priority.index("folders") < priority.index("tags"), \
        "folders are primary organization; tags are incidental"


def test_the_schema_describes_folders_without_mentioning_the_tag_mechanism():
    meaning = schema.DATASET.fields["folders"][1]
    assert "folder" in meaning.lower()
    assert "tag" not in meaning.lower(), \
        "the tag prefix is an implementation detail, not something to surface"


# --- the write hazard ---

def _plan(fake_tenant, session_dir, op, **args):
    from cirro_agent.worker import dispatch
    return dispatch.run({"verb": "plan", "op": op,
                         "base_url": fake_tenant["base_url"],
                         "session_dir": str(session_dir.path),
                         "args": args})


def _plan_update(fake_tenant, session_dir, **args):
    return _plan(fake_tenant, session_dir, "update_dataset",
                 project_id="proj_a", dataset_id="ds_0000", **args)


def _only_edit(session_dir, planned):
    """The single dataset's resolved record out of a batch of one."""
    edits = plans.load(session_dir, planned["plan_id"]).args["edits"]
    assert len(edits) == 1, edits
    return edits[0]


def test_changing_tags_preserves_folder_membership(writable_tenant, session_dir):
    """The regression that matters: the API replaces the whole tag list."""
    planned = _plan_update(writable_tenant, session_dir, tags=["new-tag"])
    assert planned["ok"], planned
    stored = approved_summary(session_dir, planned["plan_id"])
    assert "new-tag" in stored
    # The resolved args must still carry the folder, or applying this would move it.
    edit = _only_edit(session_dir, planned)
    assert edit["folders"] == ["Immuno"], edit
    assert edit["tags"] == ["new-tag"]
    assert any("preserved" in w for w in planned["warnings"]), planned["warnings"]


def test_moving_a_dataset_preserves_its_tags(writable_tenant, session_dir):
    planned = _plan_update(writable_tenant, session_dir, folder="Archive/2026")
    assert planned["ok"], planned
    edit = _only_edit(session_dir, planned)
    assert edit["folders"] == ["Archive/2026"]
    assert edit["tags"] == ["qc-pass"], "plain tags must survive a move"
    shown = approved_summary(session_dir, planned["plan_id"])
    assert "folder" in shown
    assert "Immuno" in shown and "Archive/2026" in shown


def test_removing_from_a_folder_is_explicit_and_warned(writable_tenant, session_dir):
    planned = _plan_update(writable_tenant, session_dir, remove_from_folder=True)
    assert planned["ok"], planned
    assert _only_edit(session_dir, planned)["folders"] == []
    assert any("no longer appear under any folder" in w for w in planned["warnings"])


def test_a_no_op_folder_change_is_refused(writable_tenant, session_dir):
    planned = _plan_update(writable_tenant, session_dir, folder="Immuno")
    assert planned["ok"] is False
    assert planned["error"]["code"] == "INVALID_QUERY"


def test_the_wire_payload_recombines_folders_and_tags(writable_tenant, session_dir):
    planned = _plan_update(writable_tenant, session_dir, tags=["kept"])
    from cirro_agent.worker import dispatch
    applied = dispatch.run({"verb": "apply", "base_url": writable_tenant["base_url"],
                            "session_dir": str(session_dir.path),
                            "plan_id": planned["plan_id"], "digest": planned["digest"],
                            "approved_summary": approved_summary(
                                session_dir, planned["plan_id"])})
    assert applied["ok"], applied
    only = applied["result"]["updated"]
    assert len(only) == 1 and applied["result"]["n_updated"] == 1
    assert only[0]["folders"] == ["Immuno"]
    assert only[0]["tags"] == ["kept"]


# --- filing a dataset at the moment it is created ----------------------------

def test_an_upload_can_name_its_cirro_folder(writable_tenant, session_dir, tmp_path):
    """Filing on upload, rather than uploading and then moving, is one approval."""
    source = tmp_path / "run"
    source.mkdir()
    (source / "sample_R1.fastq.gz").write_text("@read\n")
    planned = _plan(writable_tenant, session_dir, "upload_dataset",
                    project_id="proj_a", process="proc_ingest", name="Run 12",
                    source_dir=str(source), folder="Sequencing/2026",
                    tags=["qc-pass"])
    assert planned["ok"], planned
    record = plans.load(session_dir, planned["plan_id"])
    assert record.args["folders"] == ["Sequencing/2026"]
    assert record.args["tags"] == ["qc-pass"]
    assert "Sequencing/2026" in approved_summary(session_dir, planned["plan_id"])


def test_the_local_directory_and_the_cirro_folder_are_different_arguments(
        writable_tenant, session_dir, tmp_path):
    """They were both called `folder`, which made one of them a footgun."""
    planned = _plan(writable_tenant, session_dir, "upload_dataset",
                    project_id="proj_a", process="proc_ingest", name="Run 12",
                    folder="Sequencing/2026")
    assert not planned["ok"]
    assert planned["error"]["remediation"]["what"]
    assert "source_dir" in str(planned["error"])


def test_an_analysis_can_file_its_output_folder_at_launch(writable_tenant, session_dir):
    planned = _plan(writable_tenant, session_dir, "run_analysis",
                    project_id="proj_a", process="proc_rnaseq",
                    name="RNAseq batch 32", source_dataset_ids=["ds_0000"],
                    params={"genome": "GRCh38"}, folder="Analyses/RNAseq")
    assert planned["ok"], planned
    record = plans.load(session_dir, planned["plan_id"])
    assert record.args["folders"] == ["Analyses/RNAseq"]
    assert "Analyses/RNAseq" in approved_summary(session_dir, planned["plan_id"])


def test_a_new_dataset_with_no_folder_named_is_filed_nowhere(writable_tenant,
                                                             session_dir):
    """Silence means no folder for a new dataset, and current folders for an edit."""
    from cirro_agent.worker import mutations
    assert mutations._target_folders({}, []) == []
    assert mutations._target_folders({}, ["Immuno"]) == ["Immuno"]
    assert mutations._target_folders({"folder": "A/B"}, ["Immuno"]) == ["A/B"]
    assert mutations._target_folders({"folders": ["A", "B"]}, []) == ["A", "B"]
    assert mutations._target_folders({"remove_from_folder": True}, ["Immuno"]) == []
    assert mutations._target_folders({"folder": "/Trailing/"}, []) == ["Trailing"]


def test_folders_ride_in_the_wire_tag_list_on_every_write_path():
    """One list on the wire; a tag write that forgot folders would unfile a dataset."""
    from cirro_agent.worker import mutations
    assert mutations._wire_tags(["Immuno"], ["qc-pass"]) == [
        "folder://Immuno", "qc-pass"]
    assert mutations._wire_tags(None, None) == []


# --- several datasets, one approval ------------------------------------------

def test_moving_three_datasets_is_one_plan(writable_tenant, session_dir):
    """"Move those three into QC" was three plans and three approval prompts."""
    planned = _plan(writable_tenant, session_dir, "update_dataset",
                    project_id="proj_a",
                    dataset_ids=["ds_0000", "ds_0001", "ds_0002"], folder="QC")
    assert planned["ok"], planned
    edits = plans.load(session_dir, planned["plan_id"]).args["edits"]
    assert [e["dataset_id"] for e in edits] == ["ds_0000", "ds_0001", "ds_0002"]
    assert all(e["folders"] == ["QC"] for e in edits)
    shown = approved_summary(session_dir, planned["plan_id"])
    assert "Update 3 datasets" in shown
    for one in ("ds_0000", "ds_0001", "ds_0002"):
        assert one in shown, "the approval must name every dataset it touches"


def test_each_dataset_keeps_its_own_tags_through_a_batch_move(writable_tenant,
                                                              session_dir):
    """Reading one record and reusing it would flatten the others onto its tags."""
    planned = _plan(writable_tenant, session_dir, "update_dataset",
                    project_id="proj_a", dataset_ids=["ds_0000", "ds_0001"],
                    folder="QC")
    edits = plans.load(session_dir, planned["plan_id"]).args["edits"]
    by_id = {e["dataset_id"]: e for e in edits}
    assert by_id["ds_0000"]["tags"] != by_id["ds_0001"]["tags"], \
        "the fixture must give them different tags or this proves nothing"
    for edit in by_id.values():
        assert edit["tags"] == edit["was"]["tags"]
        assert edit["folders"] == ["QC"]


def test_a_batch_will_not_give_every_dataset_the_same_name(writable_tenant,
                                                           session_dir):
    planned = _plan(writable_tenant, session_dir, "update_dataset",
                    project_id="proj_a", dataset_ids=["ds_0000", "ds_0001"],
                    name="Same name")
    assert planned["ok"] is False
    assert planned["error"]["code"] == "INVALID_QUERY"
    assert "same name" in planned["error"]["remediation"]["what"]


def test_datasets_that_already_match_are_named_and_skipped(writable_tenant,
                                                           session_dir):
    """ds_0000 is already in Immuno; moving it there again is not a change."""
    planned = _plan(writable_tenant, session_dir, "update_dataset",
                    project_id="proj_a", dataset_ids=["ds_0000", "ds_0001"],
                    folder="Immuno")
    assert planned["ok"], planned
    edits = plans.load(session_dir, planned["plan_id"]).args["edits"]
    assert [e["dataset_id"] for e in edits] == ["ds_0001"]
    assert any("already match" in w for w in planned["warnings"]), planned["warnings"]


def test_replacing_tags_across_a_batch_warns_that_unique_tags_are_lost(
        writable_tenant, session_dir):
    planned = _plan(writable_tenant, session_dir, "update_dataset",
                    project_id="proj_a", dataset_ids=["ds_0000", "ds_0001"],
                    tags=["shared"])
    assert planned["ok"], planned
    assert any("unique to one of them are dropped" in w for w in planned["warnings"])


def test_a_partly_failed_batch_reports_both_halves(writable_tenant, session_dir,
                                                   monkeypatch):
    """Raising on the first failure would report the whole batch as failed while
    some datasets had already changed, and the audit trail would say so."""
    from cirro_agent.worker import dispatch, mutations
    planned = _plan(writable_tenant, session_dir, "update_dataset",
                    project_id="proj_a", dataset_ids=["ds_0000", "ds_0001"],
                    folder="QC")

    calls = {"n": 0}
    real = mutations.reads.Reader

    def flaky_update(*_args, **kwargs):
        calls["n"] += 1
        if kwargs.get("dataset_id") == "ds_0001":
            raise RuntimeError("dataset is locked")
        return type("D", (), {"name": "kept"})()
    monkeypatch.setattr("cirro.services.dataset.DatasetService.update", flaky_update)

    applied = dispatch.run({"verb": "apply", "base_url": writable_tenant["base_url"],
                            "session_dir": str(session_dir.path),
                            "plan_id": planned["plan_id"],
                            "digest": planned["digest"],
                            "approved_summary": approved_summary(
                                session_dir, planned["plan_id"])})

    assert applied["ok"], applied
    result = applied["result"]
    assert result["n_updated"] == 1
    assert [f["dataset_id"] for f in result["failed"]] == ["ds_0001"]
    assert "dataset is locked" in result["failed"][0]["error"]
    assert "1 of 2 datasets were updated" in result["note"]
    assert real is mutations.reads.Reader


def test_a_wholly_failed_batch_is_an_error(writable_tenant, session_dir, monkeypatch):
    from cirro_agent.worker import dispatch
    planned = _plan(writable_tenant, session_dir, "update_dataset",
                    project_id="proj_a", dataset_ids=["ds_0000", "ds_0001"],
                    folder="QC")

    def always_fails(*_args, **_kwargs):
        raise RuntimeError("service down")
    monkeypatch.setattr("cirro.services.dataset.DatasetService.update", always_fails)

    applied = dispatch.run({"verb": "apply", "base_url": writable_tenant["base_url"],
                            "session_dir": str(session_dir.path),
                            "plan_id": planned["plan_id"],
                            "digest": planned["digest"],
                            "approved_summary": approved_summary(
                                session_dir, planned["plan_id"])})

    assert applied["ok"] is False
    assert applied["error"]["code"] == "UPSTREAM_ERROR"

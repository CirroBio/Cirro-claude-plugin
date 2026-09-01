"""Dataset folders.

Cirro stores folder membership as a tag prefixed `folder://`. That is an
implementation detail of the platform: the plugin reports `folders`, keeps folder
entries out of `tags`, and filters in folder language. The write path matters most --
the API replaces the whole tag list, so a tags-only update would silently move a
dataset out of its folder.
"""
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

def _plan_update(fake_tenant, session_dir, **args):
    from cirro_agent.worker import dispatch
    return dispatch.run({"verb": "plan", "op": "update_dataset",
                         "base_url": fake_tenant["base_url"],
                         "session_dir": str(session_dir.path),
                         "args": {"project_id": "proj_a", "dataset_id": "ds_0000", **args}})


def test_changing_tags_preserves_folder_membership(writable_tenant, session_dir):
    """The regression that matters: the API replaces the whole tag list."""
    planned = _plan_update(writable_tenant, session_dir, tags=["new-tag"])
    assert planned["ok"], planned
    stored = planned["summary"]
    assert "new-tag" in stored
    # The resolved args must still carry the folder, or applying this would move it.
    from cirro_agent import plans
    record = plans.load(session_dir, planned["plan_id"])
    assert record.args["folders"] == ["Immuno"], record.args
    assert record.args["tags"] == ["new-tag"]
    assert any("preserved" in w for w in planned["warnings"]), planned["warnings"]


def test_moving_a_dataset_preserves_its_tags(writable_tenant, session_dir):
    planned = _plan_update(writable_tenant, session_dir, folder="Archive/2026")
    assert planned["ok"], planned
    from cirro_agent import plans
    record = plans.load(session_dir, planned["plan_id"])
    assert record.args["folders"] == ["Archive/2026"]
    assert record.args["tags"] == ["qc-pass"], "plain tags must survive a move"
    assert "folder" in planned["summary"]
    assert "Immuno" in planned["summary"] and "Archive/2026" in planned["summary"]


def test_removing_from_a_folder_is_explicit_and_warned(writable_tenant, session_dir):
    planned = _plan_update(writable_tenant, session_dir, remove_from_folder=True)
    assert planned["ok"], planned
    from cirro_agent import plans
    record = plans.load(session_dir, planned["plan_id"])
    assert record.args["folders"] == []
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
                            "approved_summary": planned["summary"]})
    assert applied["ok"], applied
    assert applied["result"]["folders"] == ["Immuno"]
    assert applied["result"]["tags"] == ["kept"]

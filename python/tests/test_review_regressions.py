"""Regressions for defects found by the 2026-08-27 deep review.

Each test names the defect it prevents, because the fix is not self-evident from the
code alone.
"""
import json

import pytest

from cirro_agent import query as Q, schema, summarize as S
from cirro_agent import workflow_spec as WS
from cirro_agent.worker import read_query, read_summaries, reads


# --- summarize -------------------------------------------------------------

def test_log_head_and_tail_do_not_overlap():
    """A 40-line log with head=30/tail=60 used to emit 30 lines then all 40 again."""
    body = S.logs("\n".join(f"line{i}" for i in range(40)), head=30, tail=60)
    assert len(body["head"]) == 30
    # The old assertion encoded a bug: lines past the head were dropped rather than
    # shown. Head and tail must be disjoint, and together must cover the log.
    assert body["tail"], "lines past the head are shown as the tail"
    assert not set(body["head"]) & set(body["tail"])
    assert len(body["head"]) + len(body["tail"]) == 40

    body = S.logs("\n".join(f"line{i}" for i in range(200)), head=10, tail=20)
    assert body["head"][-1] == "line9"
    assert body["tail"][0] == "line180"
    assert not set(body["head"]) & set(body["tail"])


def test_hyphenated_filenames_produce_a_glob_that_matches():
    """The stem was split on both _ and - but always rejoined with _, so the emitted
    glob matched nothing while suggested_globs still reported a count."""
    import fnmatch
    paths = [f"data/SRR{i}-R1.fastq.gz" for i in range(3)]
    patterns = S.name_patterns(paths)
    glob = patterns[0]["glob"]
    matched = [p for p in paths if fnmatch.fnmatch(p, glob)]
    assert len(matched) == patterns[0]["n"] == 3, f"{glob!r} matched {matched}"


def test_mixed_separators_round_trip():
    import fnmatch
    paths = [f"d/S{i}_lane-1.fastq.gz" for i in range(4)]
    pattern = S.name_patterns(paths)[0]
    assert len([p for p in paths if fnmatch.fnmatch(p, pattern["glob"])]) == pattern["n"]


# --- sorting ---------------------------------------------------------------

@pytest.mark.parametrize("field,values", [
    ("total_size_bytes", [9000, 10000, 200]),
    ("file_count", [9, 10, 2]),
])
def test_numeric_fields_sort_numerically(field, values):
    """Keys were stringified, so 9000 sorted after 10000."""
    rows = [{"id": str(v), field: v} for v in values]
    descending = read_query._sorted(rows, [f"-{field}"], schema.DATASET)
    assert [r[field] for r in descending] == sorted(values, reverse=True)
    ascending = read_query._sorted(rows, [field], schema.DATASET)
    assert [r[field] for r in ascending] == sorted(values)


def test_missing_values_sort_last_not_first():
    rows = [{"id": "a", "file_count": 5}, {"id": "b"}, {"id": "c", "file_count": 1}]
    assert [r["id"] for r in read_query._sorted(rows, ["file_count"], schema.DATASET)] == \
        ["c", "a", "b"]


def test_string_fields_still_sort(tmp_path):
    rows = [{"id": "b", "name": "beta"}, {"id": "a", "name": "alpha"}]
    assert [r["name"] for r in read_query._sorted(rows, ["name"], schema.DATASET)] == \
        ["alpha", "beta"]


# --- workflow parsing ------------------------------------------------------

def test_an_ungrouped_schema_is_not_counted_twice():
    """The fallback synthesized a 'general' group from schema.properties and then the
    top-level pass appended the same properties again."""
    flags = []
    params, groups = WS._params_from_schema(
        {"properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}, flags)
    assert len(params) == 2, [p["name"] for p in params]
    assert [g["key"] for g in groups] == ["general"]
    assert any(f["code"] == "UNGROUPED_SCHEMA" for f in flags)


def test_a_grouped_schema_still_picks_up_top_level_properties():
    flags = []
    params, _groups = WS._params_from_schema(
        {"$defs": {"input": {"properties": {"a": {"type": "string"}}}},
         "properties": {"version": {"type": "boolean"}}}, flags)
    assert {p["name"] for p in params} == {"a", "version"}
    assert {p["group"] for p in params} == {"input", "_top_level"}


def test_output_dir_detection_groups_its_operators():
    """`A or B and C` was formatted as though it meant `(A or B) and C`."""
    hints = WS.candidates([
        {"name": "reference_dir", "format": "directory-path", "is_path": True,
         "is_infrastructure": False, "declared_required": True, "default": None},
    ])
    assert not hints["managed_output_dir"], \
        "a directory input without 'out' in its name is not the output directory"
    assert [c["name"] for c in hints["file_pickers"]] == ["reference_dir"]


# --- aggregation -----------------------------------------------------------

def test_one_bad_timestamp_does_not_abort_an_aggregation():
    """Every other date path skips unparseable rows; group_by raised INVALID_QUERY."""
    ctx = Q.Ctx(username="u")
    rows = [{"id": "a", "created_at": "2026-01-05T00:00:00Z"},
            {"id": "b", "created_at": "not-a-date"},
            {"id": "c", "created_at": "2026-01-06T00:00:00Z"}]
    groups, _tail, totals = Q.group(rows, ["created_at:month"], ["count"], ctx)
    assert totals["count"] == 3
    keys = {g["key"]["created_at:month"] for g in groups}
    assert "2026-01" in keys and None in keys


# --- raw log slicing -------------------------------------------------------

def test_a_zero_line_range_does_not_index_from_the_end():
    """start-1 turned a 0 start into -1, returning the last line or nothing."""
    text = "\n".join(f"line{i}" for i in range(10))
    sliced = read_summaries._raw_slice(text, {"line_range": [0, 3]})
    assert sliced["text"].splitlines() == ["line0", "line1", "line2"]
    assert sliced["line_range"] == [1, 3]


def test_workspace_action_cannot_be_an_arbitrary_attribute():
    from unittest.mock import MagicMock
    from cirro_agent import errors
    from cirro_agent.worker import share_ops
    reader = MagicMock()
    with pytest.raises(errors.AgentError) as caught:
        share_ops.apply_workspace_action(
            reader, {"project_id": "p", "workspace_id": "w", "action": "archive"})
    assert caught.value.code == "PLAN_INVALID"
    reader.client.workspaces.archive.assert_not_called()


def test_a_session_folder_is_not_silently_rebound_to_another_tenant(tmp_path,
                                                                    monkeypatch):
    from cirro_agent import errors, session as session_mod
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    session_mod.init(tmp_path, "app.cirro.bio")
    with pytest.raises(errors.AgentError) as caught:
        session_mod.init(tmp_path, "evil.example.org")
    assert caught.value.code == "TENANT_MISMATCH"
    stored = json.loads((tmp_path / ".cirro" / "session.json").read_text())
    assert stored["base_url"] == "app.cirro.bio", "the original binding must survive"


def test_private_session_files_are_never_world_readable(tmp_path, monkeypatch):
    from cirro_agent import plans, session as session_mod
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    sess = session_mod.init(tmp_path, "app.cirro.bio")
    plan = plans.create(sess, op="delete_dataset", args={"dataset_id": "d"},
                        summary="s", username="u")
    for path in [sess.path / "session.json",
                 sess.path / "plans" / f"{plan.plan_id}.json"]:
        assert oct(path.stat().st_mode & 0o077) == "0o0", f"{path} is group/world readable"


# --- second review pass -----------------------------------------------------

def test_ne_is_the_complement_of_eq_for_missing_fields():
    """A row lacking the field never satisfied `ne`, so ne was not eq's complement."""
    ctx = Q.Ctx(username="u")
    rows = [{"id": "a", "status": "FAILED"}, {"id": "b", "status": "COMPLETED"},
            {"id": "c"}]
    eq = Q.apply_filter(rows, {"field": "status", "op": "eq", "value": "FAILED"},
                        schema.DATASET, ctx)
    ne = Q.apply_filter(rows, {"field": "status", "op": "ne", "value": "FAILED"},
                        schema.DATASET, ctx)
    assert {r["id"] for r in eq} | {r["id"] for r in ne} == {"a", "b", "c"}
    assert not {r["id"] for r in eq} & {r["id"] for r in ne}


def test_a_bad_regex_is_a_query_error_not_an_upstream_fault():
    from cirro_agent import errors
    ctx = Q.Ctx(username="u")
    with pytest.raises(errors.AgentError) as caught:
        Q.apply_filter([{"name": "x"}], {"field": "name", "op": "regex", "value": "([a"},
                       schema.DATASET, ctx)
    assert caught.value.code == "INVALID_QUERY"


def test_comma_separated_folders_and_tags_actually_match():
    """`folder:a,b` took the `in` branch, comparing a list to scalars: never matched."""
    ctx = Q.Ctx(username="u")
    rows = [{"id": "a", "folders": ["Immuno"]}, {"id": "b", "folders": ["Other"]},
            {"id": "c", "folders": ["Third"]}]
    node = Q.resolve_filter(Q.parse_shorthand("folder:Immuno,Other"), schema.DATASET, ctx)
    assert {r["id"] for r in Q.apply_filter(rows, node, schema.DATASET, ctx)} == {"a", "b"}


def test_a_facet_counts_rows_whose_list_is_empty():
    """The folders facet dropped unfiled datasets, disagreeing with `read folders`."""
    rows = [{"id": "a", "folders": ["X"]}, {"id": "b", "folders": []},
            {"id": "c", "folders": []}]
    facet = Q.compute_facets(rows, schema.DATASET)["folders"].render(10)
    assert sum(c["n"] for c in facet["top"]) + facet["other"]["n"] == len(rows)


def test_an_unknown_metric_is_rejected_even_with_no_matches():
    from cirro_agent import errors
    with pytest.raises(errors.AgentError) as caught:
        Q.group([], ["status"], ["median:size"], Q.Ctx(username="u"))
    assert caught.value.code == "INVALID_QUERY"


def test_every_sdk_enum_unwraps_not_just_whitelisted_ones():
    """A name-suffix whitelist let RepositoryType serialize as 'RepositoryType.X'."""
    from cirro_api_client.v1.models import RepositoryType, SharingType, ProjectRole
    from cirro_agent.worker import rows as R
    for member in (RepositoryType.GITHUB_PUBLIC, SharingType.PRIVATE, ProjectRole.ADMIN):
        assert R.plain(member) == member.value
        assert "." not in str(R.plain(member))


def test_run_analysis_tags_reach_the_request():
    """The planner recorded tags and the executor dropped them."""
    from unittest.mock import MagicMock
    reader = MagicMock()
    from cirro_agent.worker import analysis_ops
    analysis_ops.apply_run_analysis(reader, {
        "project_id": "p", "process_id": "proc", "name": "n",
        "source_dataset_ids": ["ds"], "params": {}, "description": "",
        "notification_emails": [], "tags": ["keep-me"]})
    request = reader.client.execution.run_analysis.call_args.kwargs["request"]
    assert [t.value for t in request.tags] == ["keep-me"]


def test_a_truncated_plan_file_reports_a_plan_error(tmp_path, monkeypatch):
    from cirro_agent import errors, plans, session as session_mod
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    sess = session_mod.init(tmp_path, "app.cirro.bio")
    plan = plans.create(sess, op="delete_dataset", args={"dataset_id": "d"},
                        summary="s", username="u")
    path = sess.path / "plans" / f"{plan.plan_id}.json"
    path.write_text(json.dumps({"plan_id": plan.plan_id, "op": "delete_dataset"}))
    with pytest.raises(errors.AgentError) as caught:
        plans.load(sess, plan.plan_id)
    assert caught.value.code == "PLAN_INVALID"


def test_a_cold_fetch_is_not_reported_as_a_cache_hit():
    from cirro_agent.envelope import Cost
    cost = Cost()
    cost.snapshot("datasets@p", age_s=0.0, ttl_s=300, item_count=5, fresh=False)
    assert cost.to_dict()["cache"]["fraction_from_cache"] == 0.0
    cost.snapshot("processes@t", age_s=10.0, ttl_s=86400, item_count=3, fresh=True)
    assert cost.to_dict()["cache"]["fraction_from_cache"] == 0.5


# --- the form walk sees conditional fields ----------------------------------

def test_spec_shape_declares_parameters_hidden_behind_a_selector():
    """Walking `properties` alone reported every conditional parameter as undeclared,
    so run_analysis warned "usually a typo" on live, correctly mapped fields."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    form = {"type": "object", "properties": {
        "cnv_options": {"type": "object", "properties": {
            "use_qdnaseq": {"type": "boolean", "default": False}},
            "dependencies": {"use_qdnaseq": {"oneOf": [
                {"properties": {"use_qdnaseq": {"enum": [True]},
                                "qdnaseq_bin_size": {"type": "integer",
                                                     "default": 500}}},
                {"properties": {"use_qdnaseq": {"enum": [False]}}}]}}}}}
    reader = MagicMock()
    reader.client.processes.get_parameter_spec.return_value = SimpleNamespace(
        form_spec_json=form)
    defaults, declared, _required = reads.spec_shape(reader, "proc_x")
    assert "qdnaseq_bin_size" in declared
    assert defaults["qdnaseq_bin_size"] == 500
    assert "cnv_options" not in declared, "groups are containers, not parameters"


def test_the_root_cause_lookup_books_its_detail_call():
    from unittest.mock import MagicMock
    from cirro_agent.worker import read_summaries
    reader = MagicMock()
    reader.cost = MagicMock()
    read_summaries._primary_failed_index(reader, "proj_a", "ds_1", [])
    reader.cost.call.assert_called_with("datasets.get", detail=True)

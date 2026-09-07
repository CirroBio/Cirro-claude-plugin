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
    """`reverse` inverts the whole sort key, so the missing-last rank came out
    missing-FIRST on a descending sort: `-file_count` led with the rows that have
    none. Missing must be last in both directions."""
    rows = [{"id": "a", "file_count": 5}, {"id": "b"}, {"id": "c", "file_count": 1}]
    assert [r["id"] for r in read_query._sorted(rows, ["file_count"], schema.DATASET)] == \
        ["c", "a", "b"]
    assert [r["id"] for r in read_query._sorted(rows, ["-file_count"], schema.DATASET)] == \
        ["a", "c", "b"]

    # Also for a string field, where the present rows key on the third tuple slot.
    dated = [{"id": "a", "updated_at": "2026-01-05T00:00:00Z"}, {"id": "b"},
             {"id": "c", "updated_at": "2026-03-05T00:00:00Z"}]
    assert [r["id"] for r in read_query._sorted(dated, ["updated_at"],
                                                schema.DATASET)] == ["a", "c", "b"]
    assert [r["id"] for r in read_query._sorted(dated, ["-updated_at"],
                                                schema.DATASET)] == ["c", "a", "b"]


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
    rows = [{"id": "a", "created_at": "2026-01-05T00:00:00Z"},
            {"id": "b", "created_at": "not-a-date"},
            {"id": "c", "created_at": "2026-01-06T00:00:00Z"}]
    groups, _tail, totals = Q.group(rows, ["created_at:month"], ["count"])
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
    session_mod.init(tmp_path, "app.cirro.bio")
    with pytest.raises(errors.AgentError) as caught:
        session_mod.init(tmp_path, "evil.example.org")
    assert caught.value.code == "TENANT_MISMATCH"
    stored = json.loads((tmp_path / ".cirro" / "session.json").read_text())
    assert stored["base_url"] == "app.cirro.bio", "the original binding must survive"


def test_private_session_files_are_never_world_readable(tmp_path, monkeypatch):
    from cirro_agent import plans, session as session_mod
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
        Q.group([], ["status"], ["median:size"])
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
    # ParameterSpecification.form_spec_json is `form_schema.form.additional_properties`
    # -- a plain dict that is already the schema, never a model and never the
    # {form, ui} pair. A double that hands back either of those tests a shape the API
    # does not produce.
    reader.client.processes.get_parameter_spec.return_value = SimpleNamespace(
        form_spec_json=form)
    fields = {f["path"]: f for f in reads.spec_shape(reader, "proc_x")}
    assert "cnv_options.qdnaseq_bin_size" in fields
    assert fields["cnv_options.qdnaseq_bin_size"]["default"] == 500
    # Only reachable when the selector picks that branch, and the path says where the
    # value is nested -- a top-level `qdnaseq_bin_size` is silently ignored.
    assert fields["cnv_options.qdnaseq_bin_size"]["conditional"] is True
    # The group is the key actually submitted, so it is declared, not a stray.
    assert fields["cnv_options"]["is_group"] is True
    assert fields["cnv_options"]["conditional"] is False


IF_THEN_FORM = {"ui": {}, "form": {"type": "object", "properties": {
    "align": {"type": "object",
              "properties": {"aligner": {"type": "string",
                                         "enum": ["bwa", "star"]}},
              "if": {"properties": {"aligner": {"const": "star"}}},
              "then": {"properties": {
                  "star_index": {"type": "string", "default": "GRCh38",
                                 "pathType": "references",
                                 "file": "**/*.tar.gz"}}},
              "else": {"properties": {
                  "bwa_threads": {"type": "integer", "default": 4},
                  "bwa_seed": {"type": "string", "file": "**/*.bed"}}}}}}}


def test_both_form_walk_consumers_see_if_then_conditional_fields():
    """`form_paths` and `check_form` used to run two independent walks whose wrapper
    keys had drifted: only `form_paths` descended if/then, so a then-branch field was
    both reported as a dangling mapping and exempt from the picker checks."""
    from cirro_agent import formcheck

    paths = formcheck.form_paths(IF_THEN_FORM)
    assert "$.dataset.params.align.star_index" in paths
    assert "$.dataset.params.align.bwa_threads" in paths
    # The one walk answers both consumers, so they cannot disagree again.
    assert paths == {p for p, _f in
                     formcheck.iter_fields(IF_THEN_FORM["form"], "$.dataset.params")}

    # Consumer one: a mapping onto a then-branch field is live, not dangling.
    assert not formcheck.check_input_map(
        {"index": "$.dataset.params.align.star_index"}, IF_THEN_FORM)

    # Consumer two: the picker checks reach inside the branches. `bwa_seed` has a
    # `file` glob and no pathType, which renders nothing.
    problems = formcheck.check_form(IF_THEN_FORM)
    assert [(p.level, p.where) for p in problems] == \
        [("error", "form.align.bwa_seed.pathType")]


def test_spec_shape_declares_parameters_behind_an_if_then():
    """spec_shape reached for the narrower of the two walks, so an if/then form's
    conditional parameters read as undeclared -- the same wrong answer as before."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    reader = MagicMock()
    # The form half only -- see the note in the cnv_options test above.
    reader.client.processes.get_parameter_spec.return_value = SimpleNamespace(
        form_spec_json=IF_THEN_FORM["form"])
    fields = {f["path"]: f for f in reads.spec_shape(reader, "proc_y")}
    assert "align.star_index" in fields and "align.bwa_threads" in fields
    assert fields["align.star_index"]["default"] == "GRCh38"
    assert fields["align.bwa_threads"]["default"] == 4
    assert fields["align"]["is_group"] is True


def test_the_root_cause_lookup_books_its_detail_call():
    from unittest.mock import MagicMock
    from cirro_agent.worker import read_summaries
    reader = MagicMock()
    reader.cost = MagicMock()
    read_summaries._primary_failed_index(reader, "proj_a", "ds_1", [])
    reader.cost.call.assert_called_with("datasets.get", detail=True)


# --- the metadata schema is read from the field that exists ------------------

def _schema_reader(document):
    """A Reader whose metadata service returns a real generated FormSchema.

    Built through the model so the fixture cannot drift from the API: FormSchema is
    form / ui / metadataRequirements, and unknown top-level keys land in
    additional_properties.
    """
    from unittest.mock import MagicMock
    from cirro_api_client.v1.models import FormSchema
    reader = MagicMock()
    reader.cost = MagicMock()
    reader.resolve_project.return_value = {"id": "proj_a", "name": "Immuno"}
    reader.client.metadata.get_project_schema.return_value = FormSchema.from_dict(document)
    return reader


def test_a_schema_plan_names_the_keys_it_removes():
    """FormSchema has no `form_spec_json`, so the baseline was empty for every project:
    a replacement that dropped `sex` rendered as "keys removed []" and warned nothing.
    """
    from cirro_agent.worker import project_ops
    reader = _schema_reader({"ui": {}, "form": {"type": "object", "properties": {
        "condition": {"type": "string"}, "sex": {"type": "string"}}}})
    draft = project_ops.plan_update_project_schema(
        reader, {"project_id": "proj_a",
                 "schema": {"properties": {"condition": {"type": "string"}}}})
    assert "keys removed  ['sex']" in draft.summary
    assert "keys added    []" in draft.summary
    assert any("sex" in w for w in draft.warnings), draft.warnings


def test_a_schema_baseline_is_also_read_from_metadata_requirements():
    """Which half of the document a tenant fills is not fixed, and an approval that
    reads only one half understates what it removes."""
    from cirro_agent.worker import project_ops
    reader = _schema_reader({"metadataRequirements": {"type": "object", "properties": {
        "sex": {"type": "string"}}}})
    draft = project_ops.plan_update_project_schema(
        reader, {"project_id": "proj_a", "schema": {"properties": {}}})
    assert "keys removed  ['sex']" in draft.summary


def test_an_unreadable_schema_shape_is_an_unknown_baseline_not_an_empty_one():
    from cirro_agent.worker import project_ops
    reader = _schema_reader({"somethingElse": {"fields": ["sex"]}})
    draft = project_ops.plan_update_project_schema(
        reader, {"project_id": "proj_a",
                 "schema": {"properties": {"condition": {"type": "string"}}}})
    assert "keys removed  unknown" in draft.summary
    assert any("could not be read" in w for w in draft.warnings), draft.warnings


def test_declared_metadata_keys_come_from_the_schema_document():
    """schema_keys of [] reported every observed metadata key as drift."""
    reader = _schema_reader({"form": {"properties": {"condition": {}, "sex": {}}}})
    keys = read_summaries._declared_metadata_keys(reader, "proj_a")
    assert set(keys) == {"condition", "sex"}
    reader.cost.call.assert_called_with("metadata.schema")


def test_an_unreadable_schema_claims_no_drift_in_either_direction():
    from unittest.mock import MagicMock
    reader = MagicMock()
    reader.cost = MagicMock()
    reader.client.metadata.get_project_schema.side_effect = RuntimeError("no schema")
    assert read_summaries._declared_metadata_keys(reader, "proj_a") is None
    assert "schema_drift" not in S.samples([{"id": "s1", "name": "S1",
                                             "metadata": {"condition": "Tumor"}}],
                                           schema_keys=None)


# --- one summary describes one project --------------------------------------

def test_a_narrowed_summary_scope_says_which_projects_it_left_out():
    """Summarizing project_ids[0] under an echo naming every project asked for
    reported one project's figures as though they covered them all."""
    entry, = read_summaries._projects_left_out("samples", ["proj_a", "proj_b"], None)
    assert entry["code"] == "SCOPE_NARROWED"
    assert entry["projects_not_read"] == ["proj_b"]
    assert entry["fix"]["retry_with"][0]["scope"]["project_ids"] == ["proj_b"]

    # A dataset-scoped summary is reported too. It looks like nothing is omitted --
    # the dataset lives in one project -- but if it lives in proj_b the read comes
    # back empty, which the caller cannot tell from a dataset with no files.
    scoped, = read_summaries._projects_left_out("files", ["proj_a", "proj_b"], "ds_1")
    assert scoped["projects_not_read"] == ["proj_b"]
    assert "empty rather than wrong" in scoped["detail"]
    assert scoped["fix"]["retry_with"][0]["dataset_id"] == "ds_1"

    # Both read verbs answer the same scope the same way, under the same key.
    query_side, = read_query._projects_left_out("file", ["proj_a", "proj_b"], "ds_1")
    assert query_side["projects_not_read"] == scoped["projects_not_read"]
    assert query_side["code"] == scoped["code"]


def test_a_summary_echoes_only_the_project_it_read(fake_tenant, session_dir):
    from cirro_agent.worker import dispatch
    out = dispatch.run({"verb": "summarize", "of": "files", "dataset_id": "ds_0000",
                        "base_url": fake_tenant["base_url"],
                        "session_dir": str(session_dir.path),
                        "scope": {"project_ids": ["proj_a", "proj_b"]}})
    assert out["ok"], out
    assert out["query"]["scope"]["project_ids"] == ["proj_a"]
    assert out["result"]["next_calls"][0]["call"]["scope"]["project_ids"] == ["proj_a"]


# --- include only buys the calls it needs ------------------------------------

def test_a_list_tier_include_costs_no_process_detail_call(monkeypatch):
    """The process branch fetched process_detail for every matched row whenever
    `include` was non-empty, so include:["name"] -- already in the listing -- cost one
    processes.get per process. The dataset branch has always gated on the tier."""
    from unittest.mock import MagicMock
    from cirro_agent import errors
    fetched = []

    def detail(reader, process_id, base):
        fetched.append(process_id)
        return {"config_source": "STORAGE"}

    monkeypatch.setattr(read_query, "process_detail", detail)
    rows = [{"id": f"proc_{i}", "name": f"n{i}"} for i in range(4)]

    granted, _notes = read_query._apply_include(
        MagicMock(), "process", rows, ["name"], schema.PROCESS, [])
    assert granted == {"name"}
    assert fetched == [], "a list-tier include must not cost an API call per process"

    read_query._apply_include(MagicMock(), "process", rows, ["config_location"],
                              schema.PROCESS, [])
    assert fetched == [r["id"] for r in rows], \
        "config_location is only produced by the detail fetch, so it still pays for it"

    # One gate, and it still refuses the same thing: config_location reads as DERIVED
    # but costs a call per row, so it is gated like any detail-tier field.
    many = [{"id": f"proc_{i}"} for i in range(read_query.DETAIL_BUDGET + 1)]
    with pytest.raises(errors.AgentError) as caught:
        read_query._apply_include(MagicMock(), "process", many, ["config_location"],
                                  schema.PROCESS, [])
    assert caught.value.code == "COST_GATE"
    assert any(r.get("confirm_detail_cost") for r in caught.value.retry_with)


def test_a_gated_field_is_not_faceted_until_include_grants_it():
    """config_source is detail-tier but sits in PROCESS.facet_priority, so a process
    query without `include` emitted a facet whose one bucket was {value: null, n: all}
    while shape.unavailable in the same response called the field unavailable."""
    rows = [{"id": "p1", "name": "a", "executor": "NEXTFLOW"},
            {"id": "p2", "name": "b", "executor": "NEXTFLOW"}]
    facets = read_query._visible_facets(rows, schema.PROCESS, {}, set())
    assert "config_source" not in facets, "nothing fetched it, so there is nothing to count"
    shape = Q.compute_shape(rows, schema.PROCESS, Q.Ctx(username="u"), granted=set())
    assert shape["unavailable"]["config_source"]["available"] is False

    with_detail = [dict(r, config_source="STORAGE") for r in rows]
    facets = read_query._visible_facets(with_detail, schema.PROCESS, {},
                                        {"config_source"})
    assert [cell["value"] for cell in facets["config_source"].render(10)["top"]] == \
        ["STORAGE"]


# --- one dataset lives in one project ---------------------------------------

def test_a_file_read_says_which_named_projects_it_never_looked_at():
    """The manifest was read from project_ids[0] while the echo named every project
    given, so one project's files were reported as though they covered both."""
    entry, = read_query._projects_left_out("file", ["proj_a", "proj_b"], "ds_1")
    assert entry["code"] == "SCOPE_NARROWED"
    assert entry["projects_not_read"] == ["proj_b"]
    assert entry["fix"]["retry_with"][0] == {
        "entity": "file", "dataset_id": "ds_1", "scope": {"project_ids": ["proj_b"]}}
    assert read_query._projects_left_out("task", ["proj_a"], "ds_1") == []


def test_a_free_form_map_model_unwraps_to_a_dict():
    """Built through the real models, because a fixture dict is what hid this.

    SampleMetadata and DatasetDetailParams keep their keys in
    `additional_properties` and are neither dict nor Mapping, so `plain` returned
    them untouched and `samples` and `params` both died on `.items()` -- against
    every project and every dataset, while the suite stayed green.
    """
    from cirro_api_client.v1.models import DatasetDetailParams, SampleMetadata
    from cirro_agent.worker import rows as R
    metadata = SampleMetadata.from_dict({"condition": "Tumor", "age": "40"})
    assert R.plain(metadata) == {"condition": "Tumor", "age": "40"}
    params = DatasetDetailParams.from_dict({"aligner": "star", "nested": {"a": 1}})
    assert R.plain(params) == {"aligner": "star", "nested": {"a": 1}}


def test_a_real_record_is_not_mistaken_for_a_free_form_map():
    """Every generated model carries additional_properties, so the test that spots a
    free-form map has to be its ONLY field -- otherwise Dataset unwraps into a
    camelCased dict and every caller reading an attribute off one breaks."""
    from cirro_api_client.v1.models import Dataset
    from cirro_agent.worker import rows as R
    assert not R.is_freeform_map(Dataset)
    fields = {"id", "name", "project_id"}
    assert fields <= {a.name for a in Dataset.__attrs_attrs__}


def test_an_unusable_task_timestamp_pair_yields_no_duration():
    """Cirro reports a state a task never reached as the Unix epoch, so `now - 1970`
    put a duration_p95_s of 1788530505 -- 56 years -- in front of a user, and a
    stop-before-start pair produced a p50 of -99. Both fed the percentiles."""
    epoch = {"started_at": "1970-01-01T00:00:00Z", "stopped_at": "2026-09-04T14:36:25Z"}
    backwards = {"started_at": "2026-09-04T14:38:00Z",
                 "stopped_at": "2026-09-04T14:36:21Z"}
    good = {"started_at": "2026-09-04T14:36:00Z", "stopped_at": "2026-09-04T14:36:21Z"}
    assert S.duration_s(epoch) is None
    assert S.duration_s(backwards) is None
    assert S.duration_s(good) == 21.0
    # And nothing unusable reaches the aggregate.
    body = S.tasks([dict(t, name="FASTQC (s)", status="COMPLETED")
                    for t in (epoch, backwards, good)])
    assert body["counts_by_process"][0]["duration_p95_s"] == 21.0

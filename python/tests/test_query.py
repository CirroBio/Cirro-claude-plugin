"""Filter grammar, faceting and aggregation. No tenant, no network."""
from datetime import datetime, timedelta, timezone

import pytest

from cirro_agent import errors, query as Q, schema

NOW = datetime(2026, 8, 27, 18, 0, tzinfo=timezone.utc)
SPEC = schema.DATASET


def ctx(**kwargs):
    kwargs.setdefault("username", "sminot")
    kwargs.setdefault("now", NOW)
    return Q.Ctx(**kwargs)


def rows(n=200):
    out = []
    for index in range(n):
        out.append({
            "id": f"ds_{index}", "name": f"RNAseq_batch{index % 10}_S{index}",
            "status": "FAILED" if index % 25 == 0 else
                      ("RUNNING" if index % 33 == 0 else "COMPLETED"),
            "created_at": (NOW - timedelta(days=index)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "created_by": "sminot" if index % 3 else "jlee",
            "process": {"id": "proc_a" if index % 2 else "proc_b",
                        "name": "rnaseq" if index % 2 else "sarek"},
            "tags": ["qc-pass"] + (["clinical"] if index % 7 == 0 else []),
            "source_dataset_ids": [f"ds_{index - 1}"] if index else [],
            "share": None, "total_size_bytes": index * 10 ** 9,
        })
    return out


def matched(spec_filter, data=None, context=None):
    data = data if data is not None else rows()
    context = context or ctx()
    node = Q.resolve_filter(Q.parse_filter(spec_filter), SPEC, context)
    return Q.apply_filter(data, node, SPEC, context)


# --- sizes and dates ---

@pytest.mark.parametrize("text,expected", [
    ("10GB", 10_000_000_000), ("10GiB", 10 * 2 ** 30), ("1.5MB", 1_500_000),
    ("512", 512), ("2TB", 2_000_000_000_000),
])
def test_size_units(text, expected):
    assert Q.parse_size(text) == expected


def test_bad_size_is_a_typed_error():
    with pytest.raises(errors.AgentError) as caught:
        Q.parse_size("ten gigs")
    assert caught.value.code == "INVALID_QUERY"
    assert caught.value.retry_with


@pytest.mark.parametrize("text,start,end", [
    ("today", "2026-08-27", "2026-08-28"),
    ("yesterday", "2026-08-26", "2026-08-27"),
    ("last-7d", "2026-08-21", "2026-08-28"),
    ("2026-Q2", "2026-04-01", "2026-07-01"),
    ("2026-08", "2026-08-01", "2026-09-01"),
    ("2025", "2025-01-01", "2026-01-01"),
    ("2026-03-04", "2026-03-04", "2026-03-05"),
])
def test_date_expressions_resolve_to_half_open_utc_intervals(text, start, end):
    low, high = Q.resolve_interval(text, ctx())
    assert low.strftime("%Y-%m-%d") == start
    assert high.strftime("%Y-%m-%d") == end
    assert low.tzinfo == timezone.utc and high.tzinfo == timezone.utc


def test_q4_wraps_the_year():
    low, high = Q.resolve_interval("2026-Q4", ctx())
    assert (low.month, high.year, high.month) == (10, 2027, 1)


def test_within_is_echoed_so_a_transcript_stays_unambiguous():
    context = ctx()
    Q.resolve_filter({"field": "created_at", "op": "within", "value": "last-7d"},
                     SPEC, context)
    echoed = context.resolved["created_at.within"]
    assert echoed["given"] == "last-7d" and echoed["tz"] == "UTC"


# --- shorthand ---

def test_shorthand_covers_the_conjunctive_case():
    parsed = Q.parse_shorthand(
        "status:FAILED,STOPPING created:last-7d owner:@me name~*RNAseq* size:>=10GB")
    clauses = {(c["field"], c["op"]) for c in parsed["and"]}
    assert clauses == {("status", "in"), ("created_at", "within"),
                       ("created_by", "eq"), ("name", "glob"),
                       ("total_size_bytes", "gte")}


def test_bare_word_shorthand_is_free_text():
    assert Q.parse_shorthand("rnaseq") == {"field": "q", "op": "contains",
                                           "value": "rnaseq"}


def test_tag_shorthand_uses_has_tag():
    parsed = Q.parse_shorthand("tag:clinical")
    assert parsed == {"field": "tags", "op": "has_tag", "value": "clinical"}


# --- operators ---

def test_status_classes_expand_and_are_echoed():
    context = ctx()
    node = Q.resolve_filter({"field": "status", "op": "eq", "value": "@bad"},
                            SPEC, context)
    assert node == {"field": "status", "op": "in",
                    "value": ["FAILED", "SUSPENDED", "UNKNOWN"]}
    assert context.resolved["@bad"]


def test_me_requires_an_identity():
    with pytest.raises(errors.AgentError) as caught:
        Q.resolve_filter({"field": "created_by", "op": "eq", "value": "@me"},
                         SPEC, ctx(username=None))
    assert caught.value.code == "AUTH_REQUIRED"


def test_unknown_operator_lists_the_valid_ones():
    with pytest.raises(errors.AgentError) as caught:
        Q.resolve_filter({"field": "name", "op": "sounds_like", "value": "x"},
                         SPEC, ctx())
    assert caught.value.code == "INVALID_QUERY"
    assert len(caught.value.retry_with) == len(Q.OPS)


def test_glob_is_case_insensitive():
    assert len(matched("name~*rnaseq*")) == 200


def test_has_tag_and_has_all_tags():
    assert len(matched({"field": "tags", "op": "has_tag", "value": "clinical"})) == 29
    both = matched({"field": "tags", "op": "has_all_tags",
                    "value": ["qc-pass", "clinical"]})
    assert len(both) == 29


def test_size_comparison_uses_parsed_bytes():
    assert len(matched("size:>=100GB")) == 100


def test_between_is_half_open():
    data = [{"id": "a", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "b", "created_at": "2026-02-01T00:00:00Z"}]
    node = {"field": "created_at", "op": "between",
            "value": ["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"]}
    assert [r["id"] for r in Q.apply_filter(data, node, SPEC, ctx())] == ["a"]


def test_missing_field_never_matches_a_comparison():
    node = {"field": "total_size_bytes", "op": "gte", "value": 1}
    assert Q.apply_filter([{"id": "x"}], node, SPEC, ctx()) == []


def test_and_or_not_compose():
    node = Q.resolve_filter({"or": [{"field": "status", "op": "eq", "value": "FAILED"},
                                    {"field": "status", "op": "eq", "value": "RUNNING"}]},
                            SPEC, ctx())
    non_completed = sum(1 for r in rows() if r["status"] != "COMPLETED")
    assert len(Q.apply_filter(rows(), node, SPEC, ctx())) == non_completed
    negated = Q.resolve_filter({"not": {"field": "status", "op": "eq",
                                        "value": "COMPLETED"}}, SPEC, ctx())
    assert len(Q.apply_filter(rows(), negated, SPEC, ctx())) == non_completed


def test_id_field_resolves_through_a_joined_object():
    assert len(matched({"field": "process_id", "op": "eq", "value": "proc_a"})) == 100


# --- lineage ---

def test_lineage_walks_both_directions():
    lineage = {"ds_1": ["ds_0"], "ds_2": ["ds_1"], "ds_3": ["ds_2"]}
    data = [{"id": f"ds_{i}"} for i in range(4)]
    context = ctx(lineage=lineage)
    descendants = Q.apply_filter(
        data, {"field": "_lineage", "op": "descendant_of", "value": "ds_0"},
        SPEC, context)
    assert {r["id"] for r in descendants} == {"ds_1", "ds_2", "ds_3"}
    ancestors = Q.apply_filter(
        data, {"field": "_lineage", "op": "ancestor_of", "value": "ds_3"},
        SPEC, context)
    assert {r["id"] for r in ancestors} == {"ds_0", "ds_1", "ds_2"}


@pytest.mark.parametrize("op,anchor,expected", [
    ("descendant_of", "ds_0", {"ds_1", "ds_2"}),
    ("ancestor_of", "ds_5", {"ds_3", "ds_4"}),
])
def test_lineage_depth_bounds_the_walk(op, anchor, expected):
    lineage = {f"ds_{i}": [f"ds_{i - 1}"] for i in range(1, 6)}
    data = [{"id": f"ds_{i}"} for i in range(6)]
    node = {"field": "_lineage", "op": op, "value": anchor, "depth": 2}
    found = Q.apply_filter(data, node, SPEC, ctx(lineage=lineage))
    assert {r["id"] for r in found} == expected


# --- facets and shape ---

def test_facets_account_for_every_match():
    data = rows()
    facets = Q.compute_facets(data, SPEC)
    status = facets["status"].render(10)
    assert sum(c["n"] for c in status["top"]) + status["other"]["n"] == len(data)


def test_truncated_facet_reports_its_own_tail():
    data = [{"id": str(i), "created_by": f"user{i}", "name": "n", "status": "COMPLETED",
             "tags": [], "source_dataset_ids": []} for i in range(40)]
    facet = Q.compute_facets(data, SPEC)["created_by"].render(5)
    assert facet["shown"] == 5
    assert facet["cardinality"] == 40
    assert facet["other"] == {"n": 35, "distinct": 35}


def test_multi_valued_facet_says_so():
    facets = Q.compute_facets(rows(), SPEC)
    assert "multi-valued" in facets["tags"].note


def test_labels_are_attached_to_id_facets():
    facets = Q.compute_facets(rows(), SPEC,
                              labels={"process_id": {"proc_a": "rnaseq"}})
    cells = facets["process_id"].render(10)["top"]
    assert any(c.get("label") == "rnaseq" for c in cells)


@pytest.mark.parametrize("span_days,expected", [
    (10, "day"), (60, "week"), (400, "month"), (1200, "quarter"), (3000, "year")])
def test_histogram_bin_adapts_to_span(span_days, expected):
    assert Q._bin_for_span(span_days) == expected


def test_name_clusters_carry_an_executable_glob():
    clusters = Q.name_clusters(rows())
    assert clusters[0]["token"] == "RNAseq"
    assert clusters[0]["glob"] == "*RNAseq*"


def test_shape_reports_gated_fields_as_unavailable():
    shape = Q.compute_shape(rows(), SPEC, ctx())
    assert shape["unavailable"]["params"]["available"] is False


def test_granted_fields_drop_out_of_unavailable():
    shape = Q.compute_shape(rows(), SPEC, ctx(), granted={"params"})
    assert "params" not in shape.get("unavailable", {})


# --- exemplars and suggestions ---

def test_exemplars_are_labeled_and_distinct():
    data = rows()
    facets = Q.compute_facets(data, SPEC)
    picked = Q.pick_exemplars(data, SPEC, facets)
    assert all(r["_why"] for r in picked)
    assert len({r["id"] for r in picked}) == len(picked)
    assert {"newest", "oldest"} <= {r["_why"] for r in picked}


def test_suggestions_predict_counts_that_hold():
    data = rows()
    facets = Q.compute_facets(data, SPEC)
    context = ctx()
    shape = Q.compute_shape(data, SPEC, context)
    for suggestion in Q.suggest(data, {"entity": "dataset"}, SPEC, facets, context, shape):
        if "matched" not in suggestion["expect"]:
            continue
        actual = matched(suggestion["call"]["filter"], data, context)
        assert len(actual) == suggestion["expect"]["matched"], suggestion["id"]


def test_suggestions_never_restate_the_whole_set_or_nothing():
    data = rows()
    facets = Q.compute_facets(data, SPEC)
    context = ctx()
    for suggestion in Q.suggest(data, {"entity": "dataset"}, SPEC, facets, context,
                                Q.compute_shape(data, SPEC, context)):
        count = suggestion["expect"].get("matched")
        if count is not None:
            assert 0 < count < len(data)


def test_an_aggregation_suggestion_keeps_a_reserved_slot():
    data = rows()
    facets = Q.compute_facets(data, SPEC)
    context = ctx()
    ids = {s["id"] for s in Q.suggest(data, {"entity": "dataset"}, SPEC, facets, context,
                                      Q.compute_shape(data, SPEC, context))}
    assert "nc_by_process" in ids


def test_no_suggestions_for_an_empty_match_set():
    assert Q.suggest([], {"entity": "dataset"}, SPEC, {}, ctx()) == []


# --- aggregation ---

def test_group_by_joins_labels_and_totals():
    groups, tail, totals = Q.group(
        rows(), ["process_id"], ["count", "sum:total_size_bytes"],
        order_by="-count", labels={"process_id": {"proc_a": "rnaseq", "proc_b": "sarek"}})
    assert {g["key"]["process_id"] for g in groups} == {"proc_a", "proc_b"}
    assert all("process_name" in g["key"] for g in groups)
    assert totals["count"] == 200
    assert tail is None


def test_group_tail_summarizes_what_it_drops():
    data = [{"id": str(i), "created_by": f"u{i}"} for i in range(30)]
    groups, tail, totals = Q.group(data, ["created_by"], ["count"],
                                   limit_groups=5)
    assert len(groups) == 5
    assert tail["groups"] == 25 and tail["count"] == 25
    assert totals["groups"] == 30


@pytest.mark.parametrize("key,check", [
    ("created_at:month", lambda v: len(v) == 7),
    ("created_at:quarter", lambda v: "Q" in v),
    ("created_at:year", lambda v: len(v) == 4),
    ("total_size_bytes:log2", lambda v: v == "0" or v.startswith("2^")),
    ("status:class", lambda v: v.startswith("@")),
])
def test_derived_group_bins(key, check):
    groups, _tail, _totals = Q.group(rows(), [key], ["count"], limit_groups=100)
    assert all(check(str(g["key"][key])) for g in groups), groups[:3]


def test_unknown_metric_is_a_typed_error():
    with pytest.raises(errors.AgentError) as caught:
        Q.group(rows(), ["status"], ["median:total_size_bytes"])
    assert caught.value.code == "INVALID_QUERY"

"""The response envelope and its degradation ladder."""
import json

import pytest

from cirro_agent import envelope as E

STANDARD = ["id", "name", "status", "created_at", "created_by", "process", "tags"]
COMPACT = ["id", "name", "status", "created_at"]


def make_rows(n):
    return [{"id": f"ds_{i}", "name": f"RNAseq_batch{i % 20}_S{i}", "status": "COMPLETED",
             "created_at": "2026-08-01T00:00:00Z", "created_by": "sminot",
             "process": {"id": "p", "name": "nf-core/rnaseq v3.14"},
             "tags": ["qc-pass"]} for i in range(n)]


def make_facets(cardinality=40):
    return {
        "status": E.Facet(values=[("COMPLETED", 900, None), ("FAILED", 80, None),
                                  ("RUNNING", 20, None)]),
        "created_at": E.Facet(kind="histogram", bin="month",
                              values=[(f"2026-{m:02d}", 100, None)
                                      for m in range(1, 9)]),
        "created_by": E.Facet(values=[(f"user{i}", cardinality - i, None)
                                      for i in range(cardinality)]),
    }


def build(n, max_tokens, n_calls=4, **kwargs):
    data = make_rows(n)
    exemplars = [dict(data[0], _why="newest"), dict(data[-1], _why="oldest")] if data else []
    return E.build(
        entity="dataset", query={"entity": "dataset"},
        counts=E.Counts(matched=n, scanned=n, scan_complete=kwargs.pop("complete", True)),
        cost=E.Cost(strategy="test"), rows=data, facets=make_facets(),
        exemplars=exemplars,
        projections={"standard": STANDARD, "compact": COMPACT},
        facet_priority=["status", "created_at", "created_by"],
        next_calls=[{"id": f"nc{i}", "why": "w" * 50, "call": {"filter": "x"},
                     "expect": {"matched": 5}} for i in range(n_calls)],
        max_tokens=max_tokens, **kwargs)


@pytest.mark.parametrize("n,max_tokens", [
    (1, 6000), (5, 6000), (50, 6000), (1200, 6000), (1200, 2000), (1200, 900), (1200, 400),
])
def test_the_response_always_fits_or_says_it_cannot(n, max_tokens):
    result = build(n, max_tokens)
    used = result["budget"]["used_est"]
    if used > max_tokens:
        assert any("BUDGET_FLOOR" in note for note in result["notes"]), \
            "overrunning silently is not allowed"
    assert json.loads(E.compact(result)), "the payload must remain valid JSON"


def test_a_small_result_is_returned_as_a_full_page():
    result = build(20, 6000)
    counts = result["result"]["counts"]
    assert counts["rows_are"] == "page"
    assert counts["returned"] == 20


def test_a_large_result_previews_rather_than_truncating_a_page():
    result = build(1200, 6000)
    counts = result["result"]["counts"]
    assert counts["rows_are"] == "preview_not_page"
    assert all("_why" in row for row in result["result"]["rows"])
    assert any(o["code"] == "ROWS_PREVIEWED" for o in result["omitted"])


def test_counts_and_facets_describe_everything_not_the_returned_rows():
    result = build(1200, 6000)
    assert result["result"]["counts"]["matched"] == 1200
    status = result["result"]["facets"]["status"]
    assert sum(c["n"] for c in status["top"]) == 1000


def test_rows_degrade_before_facets():
    result = build(1200, 6000)
    degraded = [d["what"] for d in result["budget"]["degraded"]]
    assert degraded, "a 1200-row page cannot fit; something must have degraded"
    assert degraded[0] == "rows"
    assert set(result["result"]["facets"]) == {"status", "created_at", "created_by"}, \
        "facets should survive while rows shrink"


def test_next_calls_never_reach_zero():
    for max_tokens in (6000, 2000, 900, 400):
        result = build(1200, max_tokens)
        assert result["result"]["next_calls"], \
            f"a response at max_tokens={max_tokens} offered no way forward"


def test_facet_truncation_is_reported_and_arithmetically_honest():
    result = build(50, 6000)
    facet = result["result"]["facets"]["created_by"]
    assert facet["shown"] + facet["other"]["distinct"] == facet["cardinality"]
    assert any(o["code"] == "FACET_TRUNCATED" for o in result["omitted"])


def test_dropped_facets_are_reported():
    result = build(1200, 400)
    codes = {o["code"] for o in result["omitted"]}
    assert "FACETS_DROPPED" in codes
    assert result["result"]["facets"], "the highest-priority facet must survive"


def test_an_incomplete_scan_poisons_the_whole_response():
    result = build(50, 6000, complete=False)
    assert result["result"]["counts"]["scan_complete"] is False
    entry = next(o for o in result["omitted"] if o["code"] == "SCAN_TRUNCATED")
    assert "lower bound" in entry["detail"]


def test_budget_bookkeeping_is_counted_in_the_reported_figure():
    """Omitted entries and notes added after measuring would let a response tip over.

    used_est sits inside the payload it measures, so it under-reports by the width of
    the number -- a few tokens. Everything else must be inside the measurement.
    """
    for max_tokens in (400, 500, 700, 1000, 1500, 6000):
        result = build(1200, max_tokens)
        recomputed = E.estimate_tokens(result)
        drift = recomputed - result["budget"]["used_est"]
        assert 0 <= drift <= 6, f"max_tokens={max_tokens} drifted by {drift} tokens"


def test_aggregation_omits_rows_entirely():
    result = E.build(
        entity="dataset", query={}, counts=E.Counts(
            matched=1200, scanned=1200, scan_complete=True, rows_are=E.ROWS_ABSENT,
            groups_total=3, groups_returned=3),
        cost=E.Cost(), groups=[{"key": {"status": "COMPLETED"}, "count": 900}],
        group_extras={"totals": {"count": 1200}}, max_tokens=6000)
    assert "rows" not in result["result"]
    assert "returned" not in result["result"]["counts"]
    assert result["result"]["counts"]["rows_are"] == "absent_aggregation_only"


def test_histogram_facets_are_not_term_truncated():
    result = build(1200, 6000)
    histogram = result["result"]["facets"]["created_at"]
    assert histogram["kind"] == "histogram"
    assert len(histogram["buckets"]) == 8


def test_cost_reports_cache_use():
    cost = E.Cost(strategy="s")
    cost.call("datasets.list")
    cost.call("datasets.get", detail=True)
    cost.snapshot("datasets@project=p", age_s=30, ttl_s=300, item_count=1200, fresh=True)
    payload = cost.to_dict()
    assert payload["api_calls"] == 2
    assert payload["detail_calls"] == 1
    assert payload["cache"]["hits"] == 1
    assert payload["cache"]["snapshots_used"][0]["item_count"] == 1200


def test_minimum_budget_is_enforced():
    result = build(1200, 10)
    assert result["budget"]["max_tokens"] == E.MIN_TOKENS


# --- shrink_to_fit: the one ladder for a list inside a wrapped result -------

def wrapped(max_tokens=600, **lists):
    return E.wrap(query={"verb": "list"}, result={"of": "things", **lists},
                  cost={"api_calls": 0}, max_tokens=max_tokens)


def bulky(n):
    return [{"id": f"i_{i}", "blurb": "detail " * 8} for i in range(n)]


def test_a_shrunk_list_is_marked_by_its_own_key():
    """The marker used to be a fixed `truncated_to` on the result, which could not say
    which of two lists had been cut."""
    envelope = wrapped(items=bulky(80))
    E.shrink_to_fit(envelope, "items", floor=3, max_tokens=600,
                    fix={"verb": "list", "max_tokens": 1200})
    result = envelope["result"]
    assert 3 <= len(result["items"]) < 80
    assert result["items_truncated_to"] == len(result["items"])
    assert "truncated_to" not in result, "the marker names the list it cut"


def test_a_truncated_list_declares_the_loss_and_offers_a_fix():
    envelope = wrapped(items=bulky(80))
    E.shrink_to_fit(envelope, "items", floor=3, max_tokens=600,
                    fix={"verb": "list", "max_tokens": 1200})
    entries = [o for o in envelope["omitted"] if o["code"] == "LIST_TRUNCATED"]
    assert len(entries) == 1, "several halving passes are one omission, not several"
    assert entries[0]["field"] == "items"
    assert entries[0]["fix"] == {"verb": "list", "max_tokens": 1200}
    assert "of 80 items" in entries[0]["detail"]
    steps = envelope["budget"]["degraded"]
    assert steps and {s["what"] for s in steps} == {"items"}
    assert steps[-1]["to"] == str(len(envelope["result"]["items"]))


def test_the_declaration_is_inside_the_budget_it_explains():
    """Appended after the last measurement, the omitted entry would tip a response
    that had just been made to fit back over its own cap."""
    envelope = wrapped(items=bulky(80))
    E.shrink_to_fit(envelope, "items", floor=3, max_tokens=600,
                    fix={"verb": "list", "max_tokens": 1200})
    envelope["budget"]["used_est"] = E.estimate_tokens(envelope)
    assert envelope["budget"]["used_est"] <= 600


def test_a_result_shrinking_two_lists_says_which_one_was_cut():
    envelope = wrapped(max_tokens=300, items=bulky(100), problems=bulky(100))
    E.shrink_to_fit(envelope, "items", floor=3, max_tokens=300,
                    fix={"verb": "list", "max_tokens": 600})
    E.shrink_to_fit(envelope, "problems", floor=3, max_tokens=300,
                    fix={"verb": "check_config", "max_tokens": 600})
    result = envelope["result"]
    assert result["items_truncated_to"] == len(result["items"]) == 3
    assert result["problems_truncated_to"] == len(result["problems"])
    truncations = [o for o in envelope["omitted"] if o["code"] == "LIST_TRUNCATED"]
    assert {o["field"] for o in truncations} == {"items", "problems"}
    # Both lists are at their floor and the response still does not fit, which `wrap`
    # now declares rather than overshooting in silence.
    assert any(o["code"] == "BUDGET_EXCEEDED" for o in envelope["omitted"])


def test_a_wrapped_response_that_cannot_fit_says_so():
    """`build` had a BUDGET_FLOOR note and `wrap` had nothing, so the five verbs that
    wrap -- get, summarize, process_config, describe, chain_status -- could exceed
    max_tokens in silence. An oversized response is sometimes unavoidable; an
    undeclared one leaves the caller believing it asked for a budget and got it."""
    envelope = E.wrap(query={"verb": "get", "id": "ds_1"}, result={"rows": bulky(200)},
                      cost={"api_calls": 1}, max_tokens=200)
    assert envelope["budget"]["used_est"] > 200
    note, = [n for n in envelope["notes"] if n.startswith("BUDGET_FLOOR")]
    assert "max_tokens=200" in note
    entry, = [o for o in envelope["omitted"] if o["code"] == "BUDGET_EXCEEDED"]
    assert entry["fix"]["retry_with"][0]["max_tokens"] == 400
    assert entry["fix"]["retry_with"][0]["id"] == "ds_1", "the fix must be runnable"


def test_a_wrapped_response_within_budget_declares_nothing():
    envelope = E.wrap(query={"verb": "get"}, result={"rows": bulky(2)},
                      cost={"api_calls": 1}, max_tokens=6000)
    assert "notes" not in envelope
    assert "omitted" not in envelope


def test_a_list_that_already_fits_is_left_alone():
    envelope = wrapped(items=bulky(3))
    E.shrink_to_fit(envelope, "items", floor=3, max_tokens=6000, fix={"verb": "list"})
    assert len(envelope["result"]["items"]) == 3
    assert "items_truncated_to" not in envelope["result"]
    assert "omitted" not in envelope and envelope["budget"]["degraded"] == []


def test_shrinking_stops_at_the_floor_rather_than_emptying_the_list():
    """A budget nothing can satisfy: the list bottoms out at the floor, and the
    response still shows the shape of what it dropped."""
    envelope = wrapped(max_tokens=50, items=bulky(200))
    E.shrink_to_fit(envelope, "items", floor=5, max_tokens=50, fix={"verb": "list"})
    assert len(envelope["result"]["items"]) == 5
    assert envelope["result"]["items_truncated_to"] == 5

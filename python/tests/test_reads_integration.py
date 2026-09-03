"""The read pipeline against a fake tenant on the wire."""
from cirro_agent.worker import dispatch, read_query, reads


def _call(fake_tenant, session_dir, verb, **overrides):
    """Go through dispatch, which is the boundary the CLI and MCP tools see:
    it is what turns a raised AgentError into a response envelope."""
    request = {"verb": verb, "base_url": fake_tenant["base_url"],
               "session_dir": str(session_dir.path)}
    request.update(overrides)
    return dispatch.run(request)


def _query(fake_tenant, session_dir, **overrides):
    request = {"entity": "dataset", "scope": {"project": "Immuno-Oncology Core"}}
    request.update(overrides)
    return _call(fake_tenant, session_dir, "query", **request)


def test_unfiltered_listing_previews_and_facets_the_whole_match_set(
        fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir, max_tokens=6000)
    assert result["ok"], result
    counts = result["result"]["counts"]

    # Facets and counts cover everything; rows are exemplars, not a page.
    assert counts["matched"] == fake_tenant["n_datasets"]
    assert counts["scan_complete"] is True
    assert counts["rows_are"] == "preview_not_page"
    assert counts["returned"] < counts["matched"]
    assert all("_why" in row for row in result["result"]["rows"])

    status = result["result"]["facets"]["status"]
    assert sum(cell["n"] for cell in status["top"]) + status["other"]["n"] == \
        counts["matched"], "facet arithmetic must account for every match"

    process = result["result"]["facets"]["process_id"]
    assert all("label" in cell for cell in process["top"]), \
        "an ID facet without labels is useless to a caller"

    assert result["budget"]["used_est"] <= 6000
    assert any(entry["code"] == "ROWS_PREVIEWED" for entry in result["omitted"])


def test_next_calls_predict_exact_counts(fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir)
    suggestions = result["result"]["next_calls"]
    assert suggestions

    for suggestion in suggestions:
        if "matched" not in suggestion["expect"]:
            continue
        follow = _query(fake_tenant, session_dir,
                        filter=suggestion["call"].get("filter"))
        assert follow["result"]["counts"]["matched"] == suggestion["expect"]["matched"], \
            f"{suggestion['id']} predicted a count it did not deliver"


def test_shorthand_filter_and_status_class(fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir, filter="status:@bad")
    matched = result["result"]["counts"]["matched"]
    assert 0 < matched < fake_tenant["n_datasets"]
    assert all(row["status"] == "FAILED" for row in result["result"]["rows"])
    assert result["query"]["resolved"]["@bad"] == ["FAILED", "SUSPENDED", "UNKNOWN"]


def test_relative_date_is_resolved_to_an_explicit_interval(fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir, filter="created:last-30d")
    resolved = result["query"]["resolved"]["created_at.within"]
    assert resolved["given"] == "last-30d"
    assert resolved["tz"] == "UTC"
    assert resolved["start"] < resolved["end"]


def test_me_resolves_to_the_authenticated_user(fake_tenant, session_dir):
    unfiltered = _query(fake_tenant, session_dir)
    mine = _query(fake_tenant, session_dir, filter="owner:@me")
    assert mine["query"]["resolved"]["@me"]["username"] == "sminot"
    expected = sum(1 for d in fake_tenant["datasets"] if d["createdBy"] == "sminot")
    assert mine["result"]["counts"]["matched"] == expected
    assert expected < unfiltered["result"]["counts"]["matched"]


def test_include_is_gated_by_cost_not_silently_sampled(fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir, include=["total_size_bytes"])
    assert result["ok"] is False
    assert result["error"]["code"] == "COST_GATE"
    assert result["error"]["remediation"]["retry_with"], "a gate must offer a way forward"


def test_include_is_granted_once_the_match_set_is_small(fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir, filter="status:@bad",
                    include=["total_size_bytes", "status_message"])
    assert result["result"]["counts"]["matched"] <= reads.DETAIL_BUDGET
    assert result["ok"], result
    row = result["result"]["rows"][0]
    assert row["total_size_bytes"] == 41203118
    assert row["total_size_human"] == "41.2 MB"
    assert row["status_message"]
    assert result["cost"]["detail_calls"] == result["result"]["counts"]["matched"]


def test_gated_fields_advertise_their_own_absence(fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir)
    unavailable = result["result"]["shape"]["unavailable"]
    assert unavailable["total_size_bytes"]["reason"] == "tier:detail"
    assert "include" in unavailable["total_size_bytes"]["how"]


def test_group_by_omits_rows_entirely(fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir, group_by=["process_id"],
                    metrics=["count"], order_by="-count")
    assert "rows" not in result["result"], \
        "aggregation must not return rows; absent and empty mean different things"
    assert result["result"]["counts"]["rows_are"] == "absent_aggregation_only"
    groups = result["result"]["groups"]
    assert {g["key"]["process_id"] for g in groups} == {"proc_rnaseq", "proc_sarek"}
    assert all("process_name" in g["key"] for g in groups)
    assert result["result"]["totals"]["count"] == fake_tenant["n_datasets"]


def test_derived_date_bins_work_as_group_keys(fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir, group_by=["created_at:month"],
                    metrics=["count"])
    keys = [g["key"]["created_at:month"] for g in result["result"]["groups"]]
    assert all(len(k) == 7 and k[4] == "-" for k in keys), keys


def test_lineage_descendants_come_free_from_the_snapshot(fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir,
                    filter={"field": "_lineage", "op": "descendant_of",
                            "value": "ds_0000", "depth": 3})
    matched = {row["id"] for row in result["result"]["rows"]}
    assert matched == {"ds_0001", "ds_0002", "ds_0003"}, matched
    assert result["cost"]["api_calls"] == 0 or result["cost"]["cache"]["hits"] >= 0


def test_ambiguous_and_missing_projects_are_typed_errors(fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir, scope={"project": "Immuno Oncology"})
    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"
    assert result["error"]["remediation"]["candidates"], "near misses help the caller"


def test_scope_is_required_for_project_scoped_entities(fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir, scope={})
    assert result["error"]["code"] == "SCOPE_REQUIRED"


def test_second_query_is_served_from_the_snapshot(fake_tenant, session_dir):
    first = _query(fake_tenant, session_dir)
    assert first["cost"]["by_kind"].get("datasets.list", 0) >= 1
    second = _query(fake_tenant, session_dir, filter="status:@bad")
    assert second["cost"]["by_kind"].get("datasets.list", 0) == 0, \
        "a repeated scope must not refetch"
    assert second["cost"]["cache"]["snapshots_used"]


def test_fresh_bypasses_the_snapshot(fake_tenant, session_dir):
    _query(fake_tenant, session_dir)
    refetched = _query(fake_tenant, session_dir, freshness="fresh")
    assert refetched["cost"]["by_kind"].get("datasets.list", 0) >= 1


def test_get_one_dataset_and_its_follow_ups(fake_tenant, session_dir):
    result = _call(fake_tenant, session_dir, "get", entity="dataset", id="ds_0000",
                   scope={"project": "Immuno-Oncology Core"})
    assert result["ok"], result
    assert result["result"]["rows"][0]["id"] == "ds_0000"
    ids = {call["id"] for call in result["result"]["next_calls"]}
    assert "nc_files" in ids and "nc_lineage" in ids


def test_summarize_files_collapses_a_large_manifest(fake_tenant, session_dir):
    result = _call(fake_tenant, session_dir, "summarize", of="files",
                   dataset_id="ds_0000",
                   scope={"project": "Immuno-Oncology Core"}, max_tokens=3000)
    assert result["ok"], result
    body = result["result"]["files"]
    assert body["total_files"] == 482
    templates = {entry["template"] for entry in body["name_patterns"]}
    assert "data/{sample}_R{1,2}_001.fastq.gz" in templates
    assert body["suggested_globs"][0]["matches"] == 480
    assert "of 482 listed files" in body["bytes_known_for"]
    assert result["budget"]["used_est"] <= 3000


def test_describe_needs_no_tenant(fake_tenant, session_dir):
    result = read_query.do_describe({"entity": "dataset"})
    assert result["ok"]
    tiers = {field["tier"] for field in result["result"]["fields"]}
    assert {"list", "probe", "detail", "execution"} <= tiers
    assert result["result"]["status_classes"]["@bad"] == ["FAILED", "SUSPENDED", "UNKNOWN"]


def test_unknown_include_field_is_rejected_with_the_catalog(fake_tenant, session_dir):
    result = _query(fake_tenant, session_dir, filter="status:@bad",
                    include=["nonexistent_field"])
    assert result["error"]["code"] == "INVALID_QUERY"
    assert result["error"]["remediation"]["retry_with"][0]["verb"] == "describe"


def test_detail_cost_can_be_accepted_explicitly(fake_tenant, session_dir):
    """A deliberate survey across many rows needs a way to say yes, symmetric with
    scope.confirm_fanout -- otherwise the only option is to narrow, which a survey
    cannot do by definition."""
    gated = _query(fake_tenant, session_dir, include=["total_size_bytes"])
    assert gated["error"]["code"] == "COST_GATE"
    assert any(r.get("confirm_detail_cost") for r in
               gated["error"]["remediation"]["retry_with"])

    small = _query(fake_tenant, session_dir, filter="name~*batch1_*", max_tokens=6000)
    n = small["result"]["counts"]["matched"]
    assert n > reads.DETAIL_BUDGET, "need a set above the gate for this test to mean anything"

    allowed = _query(fake_tenant, session_dir, filter="name~*batch1_*",
                     include=["total_size_bytes"], confirm_detail_cost=True)
    assert allowed["ok"], allowed
    assert allowed["cost"]["detail_calls"] == n
    assert any("confirm_detail_cost" in note for note in allowed["notes"])

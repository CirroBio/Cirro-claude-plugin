"""Data Studio dashboards: the document rules, the change list, the data run, and the
plan -> apply cycle against a fake tenant.

`data/dashboard-spec-parity.json` was produced by the portal's own chart code
(`patchChannel` per channel, then `humanizeSpecAxisLabels`, over each catalog chart's
skeleton), so the spec builder here is checked against the thing that renders it.
Regenerate it from Cirro-portal's packages/dashboard when the chart catalog changes.
"""

import json
from pathlib import Path

import pytest

from cirro_agent import dashboards as D
from cirro_agent.worker import dashboard_data, dispatch

from conftest import API, approved_summary

PARITY = json.loads(
    (Path(__file__).parent / "data" / "dashboard-spec-parity.json").read_text())

COUNTS_CSV = "gene,sample,condition,count,log2fc,pvalue\n" + "".join(
    f"G{g},S{s},{'treated' if s % 2 else 'control'},{g * 10 + s},"
    f"{(g - 5) / 2},{0.001 * (g + 1)}\n"
    for g in range(10) for s in range(4))


@pytest.fixture
def schema():
    return D.vendored_schema()


def file_source(change):
    """A resolver standing in for the planner's: every file is the counts table."""
    ref = D.source_ref(change)
    if ref[0] != "file":
        return "dataset-source", {"sheetId": ref[1], "tableName": "samples",
                                  "sql": "SELECT * FROM samples"}
    return "dataset-file", {"datasetId": ref[1], "datasetName": "RNA", "path": ref[2],
                            "fileType": D.file_type(ref[2]),
                            "tableName": D.file_table_name(ref[2])}


def build(schema, changes, document=None):
    builder = D.Builder(schema, document or D.empty_document(schema), file_source)
    return builder.apply(changes), builder


STARTER = [
    {"op": "add_source", "id": "counts", "dataset": "ds_0001", "path": "data/counts.csv"},
    {"op": "add_query", "id": "bygene", "inputs": ["counts"],
     "sql": "SELECT gene, condition, sum(count) AS total, avg(log2fc) AS log2fc, "
            "min(pvalue) AS pvalue FROM counts WHERE condition = {cond} "
            "GROUP BY gene, condition"},
    {"op": "add_view", "id": "volcano1", "data": "bygene", "type": "scatter", "x": "log2fc",
     "y": "pvalue", "color": "gene", "title": "Fold change", "w": 6},
    {"op": "add_view", "id": "table1", "data": "bygene", "w": 6},
    {"op": "add_control", "id": "condpick", "kind": "select", "label": "Condition",
     "target": {"query": "bygene", "token": "cond"}, "options": ["treated", "control"],
     "value": "treated"}]


# --- the schema ---------------------------------------------------------------


def test_the_vendored_schema_is_the_portals_format(schema):
    assert schema.persist_version == 1
    assert {"sql-query", "dataset-file", "chart", "input"} <= set(schema.node_types)
    assert "scatter" in schema.charts and "histogram" in schema.charts


def test_dynamic_ports_follow_the_rules(schema):
    sql = {"id": "q1q1", "type": "sql-query", "config": {"sql": "SELECT {a}, {b}, {a}"}}
    assert [p["id"] for p in schema.ports(sql)[0]] == ["upstream", "param:a", "param:b"]
    view = {"id": "v1v1", "type": "chart", "config": {"viewType": "scatter"}}
    channel_ports = [p for p in schema.ports(view)[0] if p["id"].startswith("channel:")]
    assert {p["id"] for p in channel_ports} == {"channel:x", "channel:y", "channel:color",
                                                "channel:size"}
    column = {"id": "c1c1", "type": "column", "config": {"columnClass": "temporal"}}
    assert schema.ports(column)[1][0]["accepts"] == {"columnClasses": ["temporal"]}


def test_a_schema_of_another_format_is_refused():
    with pytest.raises(ValueError, match="format"):
        D.Schema({"format": 2}, source="test")


# --- chart specs match the portal's --------------------------------------------


@pytest.mark.parametrize("chart", sorted(PARITY))
def test_build_spec_matches_the_portal(schema, chart):
    expected = PARITY[chart]
    built = json.loads(D.build_spec(schema, chart, expected["bindings"]))
    assert built == expected["spec"]


@pytest.mark.parametrize("chart", sorted(PARITY))
def test_read_bindings_matches_the_portal(schema, chart):
    expected = PARITY[chart]
    assert D.read_bindings(schema, chart, json.dumps(expected["spec"])) == expected["readBack"]


def test_an_unbound_optional_channel_keeps_the_skeletons_literal(schema):
    spec = json.loads(D.build_spec(schema, "histogram", {"x": "count"}))
    assert spec["plot"][0]["fill"] == schema.charts["histogram"]["base"]["plot"][0]["fill"]
    assert D.read_bindings(schema, "histogram", json.dumps(spec)) == {"x": "count"}


# --- building from a change list -----------------------------------------------


def test_a_change_list_builds_a_valid_dashboard(schema):
    document, builder = build(schema, STARTER)
    assert D.validate(schema, document, strict_nodes=builder.touched) == []
    ids = [n["id"] for n in document["ir"]["nodes"]]
    assert ids == ["counts", "bygene", "volcano1", "table1", "condpick"]
    tiles = {t["componentId"]: t for t in document["dashboards"][0]["tiles"]}
    assert tiles["volcano1"] == {"componentId": "volcano1", "x": 0, "y": 0, "w": 6, "h": 10}
    assert tiles["table1"]["x"] == 6 and tiles["table1"]["y"] == 0, "packs beside, not below"
    assert {"from": {"node": "condpick", "port": "value"},
            "to": {"node": "bygene", "port": "param:cond"}} in document["ir"]["edges"]


def test_generated_ids_are_stable_across_plans(schema):
    changes = [{k: v for k, v in c.items() if k != "id"} for c in STARTER[:2]]
    changes[1]["inputs"] = None
    first, _ = build(schema, [changes[0]])
    again, _ = build(schema, [changes[0]])
    assert first == again
    assert D.errors_only(D.validate(schema, first)) == []


def test_a_lone_tile_fills_the_page(schema):
    document, _ = build(schema, STARTER[:2] + [{"op": "add_view", "data": "bygene"}])
    assert document["dashboards"][0]["tiles"][0]["h"] == schema.grid["soloTileHeight"]


def test_new_tiles_land_below_existing_ones(schema):
    document, _ = build(schema, STARTER)
    grown, _ = build(schema,
                     [{"op": "add_view", "id": "hist1", "data": "bygene", "type": "histogram",
                       "x": "total"}], document)
    tile = next(t for t in grown["dashboards"][0]["tiles"] if t["componentId"] == "hist1")
    assert (tile["x"], tile["y"], tile["w"]) == (0, 10, 12)
    assert (grown["dashboards"][0]["tiles"][:2]
            == document["dashboards"][0]["tiles"][:2]), "existing tiles never move"


@pytest.mark.parametrize("change,message", [
    ({"op": "add_view", "data": "bygene", "type": "scatter", "x": "log2fc"},
     "needs a column for \\['y'\\]"),
    ({"op": "add_view", "data": "bygene", "type": "pie"}, "not a chart"),
    ({"op": "add_view", "data": "bygene", "type": "histogram", "x": "total", "y": "gene"},
     "no y channel"),
    ({"op": "add_view", "data": "nope", "type": "table"}, "no node 'nope'"),
    ({"op": "add_view", "data": "bygene", "options": {"spec": "{}"}}, "options cannot set"),
    ({"op": "add_view", "data": "bygene", "colour": "x"}, "does not take \\['colour'\\]"),
    ({"op": "add_control", "kind": "select", "target": {"query": "bygene", "token": "gene"}},
     "has no \\{gene\\}"),
    ({"op": "add_control", "kind": "slider", "target": {"view": "table1", "column": "x"}},
     "kind must be"),
    ({"op": "add_source", "dataset": "ds_0001", "path": "data/counts.csv"},
     "already node 'counts'"),
    ({"op": "add_query", "id": "BadId", "inputs": ["counts"]}, "must match"),
    ({"op": "update_query", "node": "bygene", "sql": "SELECT * FROM counts"},
     "drops \\{cond\\}"),
    ({"op": "explode"}, "each change needs an op"),
])
def test_a_bad_change_is_refused_with_its_position(schema, change, message):
    with pytest.raises(D.ChangeError, match=message) as caught:
        build(schema, STARTER + [change])
    assert caught.value.index == len(STARTER)


def test_update_view_keeps_bindings_the_new_type_still_has(schema):
    document, _ = build(schema, STARTER)
    changed, builder = build(schema,
                             [{"op": "update_view", "node": "volcano1", "size": "total",
                               "color": None, "options": {"xScale": "linear"}}], document)
    config = next(n for n in changed["ir"]["nodes"] if n["id"] == "volcano1")["config"]
    assert D.read_bindings(schema, "scatter", config["spec"]) == {"x": "log2fc", "y": "pvalue",
                                                                  "size": "total"}
    assert config["xScale"] == "linear" and config["title"] == "Fold change"
    assert D.validate(schema, changed, strict_nodes=builder.touched) == []


def test_remove_takes_its_edges_and_tiles(schema):
    document, _ = build(schema, STARTER)
    changed, _ = build(schema, [{"op": "remove", "node": "volcano1"}], document)
    assert all("volcano1" not in (e["from"]["node"], e["to"]["node"])
               for e in changed["ir"]["edges"])
    assert [t["componentId"] for t in changed["dashboards"][0]["tiles"]] == ["table1"]


def test_render_changes_names_what_moves(schema):
    document, _ = build(schema, STARTER)
    lines = D.render_changes(schema, None, document)
    assert any(line.startswith("  + view volcano1 'Fold change': scatter "
                               "(x=log2fc, y=pvalue, color=gene)") for line in lines)
    assert any("+ control condpick 'Condition': select (value='treated') "
               "-> bygene.param:cond" in line for line in lines)
    changed, _ = build(schema, [{"op": "remove", "node": "table1"}], document)
    assert "  - tile table1 from page 'default'" in D.render_changes(schema, document, changed)


# --- validation catches what the portal would hide ------------------------------


def test_a_wrong_typed_field_is_an_error_because_the_portal_wipes_the_config(schema):
    document, _ = build(schema, STARTER)
    document["ir"]["nodes"][1]["config"]["sql"] = 42
    issues = D.validate(schema, document, strict_nodes={"bygene"})
    assert any(i["kind"] == "config" and i["level"] == "error" and "resets" in i["message"]
               for i in issues)


def test_problems_on_nodes_a_change_did_not_write_are_only_warnings(schema):
    """A dashboard from a newer portal build (dev ran `spatialdata` tiles before the schema
    had them, and extra chart fields) must stay editable: only what the change wrote can
    be refused."""
    document, _ = build(schema, STARTER)
    document["ir"]["nodes"].append({"id": "spat0001", "type": "futurenode", "config": {}})
    document["dashboards"][0]["tiles"].append(
        {"componentId": "spat0001", "x": 0, "y": 20, "w": 12, "h": 8})
    document["ir"]["nodes"][2]["config"]["futureField"] = True
    changed, builder = build(schema, [{"op": "update_view", "node": "volcano1",
                                       "title": "Renamed"}], document)
    issues = D.validate(schema, changed, strict_nodes=builder.touched, before=document)
    assert not D.errors_only(issues), issues
    kinds = {(i["kind"], i.get("node")) for i in issues}
    assert ("unknown-node-type", "spat0001") in kinds
    assert not any(i["kind"] == "tile" and i.get("node") == "spat0001" for i in issues), \
        "an unknown type is reported once, not again as a tile"


def test_a_field_the_change_adds_is_still_held_to_the_schema(schema):
    document, _ = build(schema, STARTER)
    changed, builder = build(schema, [{"op": "set_config", "node": "volcano1",
                                       "config": {"futureField": True}}], document)
    issues = D.validate(schema, changed, strict_nodes=builder.touched, before=document)
    assert any("no config field 'futureField'" in i["message"]
               for i in D.errors_only(issues))


def test_a_hand_edited_spec_is_its_authors(schema):
    """Seen live: a countbar hand-edited into a horizontal barX keeps its category on y,
    off the catalog's x path. The portal draws it; it is not a missing channel."""
    document, _ = build(schema, STARTER)
    view = document["ir"]["nodes"][2]
    view["config"].update({"viewType": "countbar", "specRawEdited": True, "spec": json.dumps(
        {"plot": [{"mark": "barX", "data": {"from": "query"}, "y": "gene",
                   "x": {"count": []}}]})})
    assert not D.errors_only(D.validate(schema, document))
    changed, builder = build(schema, [{"op": "update_view", "node": "volcano1",
                                       "color": "condition"}], document)
    spec = json.loads(changed["ir"]["nodes"][2]["config"]["spec"])
    assert spec["plot"][0]["mark"] == "barX" and spec["plot"][0]["y"] == "gene"
    assert spec["plot"][0]["fill"] == "condition"


def test_update_view_patches_the_spec_in_place(schema):
    document, _ = build(schema, STARTER)
    view = document["ir"]["nodes"][2]
    spec = json.loads(view["config"]["spec"])
    spec["plot"][0]["fillOpacity"] = 0.3
    spec["yLabel"] = "p (typed by hand)"
    view["config"]["spec"] = json.dumps(spec)
    changed, _ = build(schema, [{"op": "update_view", "node": "volcano1", "x": "total",
                                 "y": "log2fc"}], document)
    patched = json.loads(changed["ir"]["nodes"][2]["config"]["spec"])
    assert patched["plot"][0]["fillOpacity"] == 0.3, "other keys survive"
    assert (patched["plot"][0]["x"], patched["plot"][0]["y"]) == ("total", "log2fc")
    assert patched["xLabel"] == "Total", "a derived title follows its column"
    assert patched["yLabel"] == "p (typed by hand)", "a typed title is kept"


def test_a_change_upstream_of_a_view_is_blamed_for_breaking_it(schema, local_files):
    document, _ = build(schema, STARTER)
    changed, builder = build(schema, [{"op": "update_query", "node": "bygene",
                                       "sql": "SELECT gene, {cond} AS c FROM counts"}],
                             document)
    results = runner(schema, local_files).run(changed)
    issues = D.only_affected(dashboard_data.view_issues(schema, changed, results),
                             D.downstream(changed, builder.touched))
    assert any(i["node"] == "volcano1" and "log2fc" in i["message"]
               for i in D.errors_only(issues))


def test_unknown_fields_are_errors_only_on_nodes_the_change_wrote(schema):
    document, _ = build(schema, STARTER)
    document["ir"]["nodes"][2]["config"]["legacyThing"] = True
    assert D.errors_only(D.validate(schema, document, strict_nodes={"volcano1"}))
    assert not D.errors_only(D.validate(schema, document, strict_nodes=set()))


def test_graph_and_tile_errors(schema):
    document, _ = build(schema, STARTER)
    document["ir"]["edges"].append({"from": {"node": "counts", "port": "relation"},
                                    "to": {"node": "volcano1", "port": "data"}})
    document["dashboards"][0]["tiles"].append({"componentId": "bygene", "x": 8, "y": 0, "w": 6,
                                               "h": 4})
    kinds = {(i["kind"], i["level"]) for i in D.validate(schema, document)}
    assert ("cardinality", "error") in kinds
    assert ("tile", "error") in kinds


def test_an_oversized_document_is_refused(schema):
    document, _ = build(schema, STARTER)
    document["ir"]["nodes"][1]["config"]["sql"] = "SELECT 1 -- " + "x" * D.MAX_BLOB_BYTES
    assert any(i["kind"] == "size" for i in D.validate(schema, document))


# --- running the data ------------------------------------------------------------


@pytest.fixture
def local_files(tmp_path, monkeypatch):
    """Serve every dataset file from a local CSV instead of downloading it."""
    (tmp_path / "counts.csv").write_text(COUNTS_CSV)

    def load_file(self, table, dataset_id, path, file_type, size=None):
        self.con.execute(
            f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM "
            f"{dashboard_data._reader_expr(str(tmp_path / 'counts.csv'), file_type)}")

    monkeypatch.setattr(dashboard_data.Runner, "load_file", load_file)
    return tmp_path


def runner(schema, tmp_path):
    from unittest.mock import MagicMock

    reader = MagicMock()
    reader.session_dir = str(tmp_path)
    return dashboard_data.Runner(reader, "proj_a", schema)


def test_the_run_reports_what_each_view_draws(schema, local_files):
    document, _ = build(schema, STARTER)
    results = runner(schema, local_files).run(document)
    view = results["volcano1"]["profile"]
    assert view["rows"] == 10, "the select control's value filled {cond}"
    classes = {c["name"]: c["class"] for c in view["columns"]}
    assert classes == {"gene": "categorical", "condition": "categorical",
                       "total": "quantitative", "log2fc": "quantitative",
                       "pvalue": "quantitative"}
    assert dashboard_data.view_issues(schema, document, results) == []


def test_sql_reads_its_inputs_by_every_portal_alias(schema, local_files):
    changes = STARTER[:1] + [{
        "op": "add_query", "id": "aliases", "inputs": ["counts"],
        "sql": "SELECT (SELECT count(*) FROM query) + (SELECT count(*) FROM in1) "
               "+ (SELECT count(*) FROM upstream) + (SELECT count(*) FROM counts) AS n"}]
    document, _ = build(schema, changes)
    results = runner(schema, local_files).run(document, sample_rows=1)
    assert results["aliases"]["profile"]["sample"] == [{"n": 160}]


def test_the_data_catches_a_column_that_is_not_there(schema, local_files):
    changes = STARTER[:2] + [{"op": "add_view", "id": "badview", "data": "bygene",
                              "type": "scatter", "x": "log2fc", "y": "padj"},
                             {"op": "add_view", "id": "badclass", "data": "bygene",
                              "type": "histogram", "x": "gene"},
                             {"op": "add_control", "id": "pick", "kind": "select",
                              "target": {"query": "bygene", "token": "cond"},
                              "value": "treated"}]
    document, _ = build(schema, changes)
    issues = dashboard_data.view_issues(schema, document,
                                        runner(schema, local_files).run(document))
    messages = " | ".join(i["message"] for i in issues)
    assert "'padj', which is not a column" in messages
    assert "'gene' is categorical" in messages


def test_a_failing_query_is_reported_against_its_node(schema, local_files):
    document, _ = build(schema,
                        STARTER[:1]
                        + [{"op": "add_query", "id": "broken", "inputs": ["counts"],
                            "sql": "SELECT nope FROM counts"},
                           {"op": "add_view", "id": "after", "data": "broken"}])
    results = runner(schema, local_files).run(document)
    assert "nope" in results["broken"]["error"]
    assert "did not load" in results["after"]["skipped"]


# --- plan and apply against a fake tenant ---------------------------------------

MANIFEST = {"domain": "d", "totalFiles": 2,
            "files": [{"path": "data/counts.csv", "size": len(COUNTS_CSV)},
                      {"path": "data/reads.fastq.gz", "size": 10}],
            "artifacts": [], "viz": [], "tables": []}


@pytest.fixture
def dashboard_tenant(writable_tenant, local_files, monkeypatch):
    """Dashboard endpoints over an in-memory table, and no portal schema to fetch."""
    import httpx
    import urllib.request

    router = writable_tenant["router"]
    store = {}

    def no_portal(*a, **k):
        raise OSError("no portal in tests")

    monkeypatch.setattr(urllib.request, "urlopen", no_portal)
    router.get(url__regex=rf"{API}/projects/[^/]+/datasets/[^/]+/files").respond(json=MANIFEST)

    def listing(request):
        return httpx.Response(200,
                              json=[{k: v for k, v in r.items() if k != "dashboardData"}
                                    for r in store.values()])

    def create(request):
        body = json.loads(request.content)
        dashboard_id = f"dash{len(store) + 1:04d}"
        store[dashboard_id] = {**body, "id": dashboard_id, "createdBy": "sminot",
                               "createdAt": "2026-09-28T00:00:00Z",
                               "updatedAt": "2026-09-28T00:00:00Z"}
        return httpx.Response(201, json={"id": dashboard_id, "message": "created"})

    def one(request):
        dashboard_id = request.url.path.rsplit("/", 1)[-1]
        if dashboard_id not in store:
            return httpx.Response(404, json={"message": "not found"})
        if request.method == "GET":
            return httpx.Response(200, json=store[dashboard_id])
        if request.method == "PUT":
            store[dashboard_id].update(json.loads(request.content))
            return httpx.Response(200, json=store[dashboard_id])
        del store[dashboard_id]
        return httpx.Response(204)

    router.get(f"{API}/projects/proj_a/dashboards").mock(side_effect=listing)
    router.post(f"{API}/projects/proj_a/dashboards").mock(side_effect=create)
    router.route(url__regex=rf"{API}/projects/proj_a/dashboards/[^/]+$").mock(side_effect=one)
    return {**writable_tenant, "dashboards": store}


def call(tenant, session_dir, verb, **overrides):
    return dispatch.run({"verb": verb, "base_url": tenant["base_url"],
                         "session_dir": str(session_dir.path), **overrides})


def plan_and_apply(tenant, session_dir, op, args):
    planned = call(tenant, session_dir, "plan", op=op, args=args)
    assert planned["ok"], planned
    applied = call(tenant, session_dir, "apply", plan_id=planned["plan_id"],
                   digest=planned["digest"],
                   approved_summary=approved_summary(session_dir, planned["plan_id"]))
    return planned, applied


def test_create_renders_the_dashboard_and_its_data(dashboard_tenant, session_dir):
    planned = call(dashboard_tenant, session_dir, "plan", op="create_dashboard",
                   args={"project_id": "proj_a", "name": "Expression", "changes": STARTER})
    assert planned["ok"], planned
    text = planned["rendered"]
    assert "Create Data Studio dashboard 'Expression'" in text
    assert "+ view volcano1 'Fold change': scatter (x=log2fc, y=pvalue, color=gene)" in text
    assert "chart volcano1: 10 rows" in text
    assert "vendored schema" in text


def test_create_writes_what_the_portal_lists_and_opens(dashboard_tenant, session_dir):
    _, applied = plan_and_apply(dashboard_tenant, session_dir, "create_dashboard",
                                {"project_id": "proj_a", "name": "Expression",
                                 "changes": STARTER})
    assert applied["ok"], applied
    dashboard_id = applied["result"]["created_dashboard_id"]
    assert (applied["result"]["url"]
            == f"https://app.cirro.test/project/proj_a/studio/{dashboard_id}")
    record = dashboard_tenant["dashboards"][dashboard_id]
    assert record["criteria"]["type"] == D.SESSION_TYPE, (
        "the portal lists only sql-room records")
    assert (record["criteria"]["scope"] == "dataset"
            and record["criteria"]["datasetId"] == "ds_0001")
    assert record["criteria"]["revision"], "every blob write mints a revision"
    assert record["schemaVersion"] == D.RECORD_SCHEMA_VERSION
    assert D.validate(D.vendored_schema(), record["dashboardData"]) == []


def test_a_plan_that_would_draw_nothing_is_refused(dashboard_tenant, session_dir):
    changes = STARTER + [{"op": "add_view", "data": "bygene", "type": "scatter", "x": "log2fc",
                          "y": "padj"}]
    planned = call(dashboard_tenant, session_dir, "plan", op="create_dashboard",
                   args={"project_id": "proj_a", "name": "Broken", "changes": changes})
    assert not planned["ok"]
    assert "padj" in planned["error"]["message"]
    assert not dashboard_tenant["dashboards"]


def test_a_bad_change_is_refused_before_anything_loads(dashboard_tenant, session_dir):
    planned = call(dashboard_tenant, session_dir, "plan", op="create_dashboard",
                   args={"project_id": "proj_a", "name": "Broken",
                         "changes": [{"op": "add_source", "dataset": "ds_0001",
                                      "path": "data/missing.csv"}]})
    assert planned["error"]["code"] == "INVALID_QUERY"
    assert ("changes[0]" in planned["error"]["message"]
            and "data/counts.csv" in planned["error"]["message"])


def test_edit_applies_on_top_of_the_stored_dashboard(dashboard_tenant, session_dir):
    _, created = plan_and_apply(dashboard_tenant, session_dir, "create_dashboard",
                                {"project_id": "proj_a", "name": "Expression",
                                 "changes": STARTER})
    dashboard_id = created["result"]["created_dashboard_id"]
    first_revision = dashboard_tenant["dashboards"][dashboard_id]["criteria"]["revision"]
    planned, applied = plan_and_apply(dashboard_tenant, session_dir, "edit_dashboard",
                                      {"project_id": "proj_a", "dashboard_id": dashboard_id,
                                       "name": "Expression v2",
                                       "changes": [{"op": "add_view", "id": "hist1",
                                                    "data": "bygene", "type": "histogram",
                                                    "x": "total"}]})
    assert applied["ok"], applied
    assert "+ view hist1: histogram (x=total) of bygene" in planned["rendered"]
    assert "Name         'Expression' -> 'Expression v2'" in planned["rendered"]
    record = dashboard_tenant["dashboards"][dashboard_id]
    assert record["name"] == "Expression v2"
    assert record["criteria"]["revision"] != first_revision
    assert "hist1" in {n["id"] for n in record["dashboardData"]["ir"]["nodes"]}


def test_an_edit_refuses_to_overwrite_a_save_made_after_it_was_planned(dashboard_tenant,
                                                                       session_dir):
    _, created = plan_and_apply(dashboard_tenant, session_dir, "create_dashboard",
                                {"project_id": "proj_a", "name": "Expression",
                                 "changes": STARTER})
    dashboard_id = created["result"]["created_dashboard_id"]
    planned = call(dashboard_tenant, session_dir, "plan", op="edit_dashboard",
                   args={"project_id": "proj_a", "dashboard_id": dashboard_id,
                         "changes": [{"op": "remove", "node": "table1"}]})
    # Someone saves in the portal between plan and apply.
    stored = dashboard_tenant["dashboards"][dashboard_id]
    stored["dashboardData"]["positions"] = {"counts": {"x": 1, "y": 2}}
    stored["criteria"]["revision"] = "from-the-portal"
    applied = call(dashboard_tenant, session_dir, "apply", plan_id=planned["plan_id"],
                   digest=planned["digest"],
                   approved_summary=approved_summary(session_dir, planned["plan_id"]))
    assert applied["error"]["code"] == "PLAN_STALE"
    assert stored["criteria"]["revision"] == "from-the-portal"


def test_delete_is_destructive_and_removes_the_record(dashboard_tenant, session_dir):
    _, created = plan_and_apply(dashboard_tenant, session_dir, "create_dashboard",
                                {"project_id": "proj_a", "name": "Expression",
                                 "changes": STARTER})
    dashboard_id = created["result"]["created_dashboard_id"]
    planned, applied = plan_and_apply(dashboard_tenant, session_dir, "delete_dashboard",
                                      {"project_id": "proj_a", "dashboard_id": dashboard_id})
    assert planned["risk"] == "cirro_destructive"
    assert "5 nodes, 2 tiles" in planned["rendered"]
    assert applied["ok"], applied
    assert dashboard_id not in dashboard_tenant["dashboards"]


# --- reads -----------------------------------------------------------------------


def test_list_and_read_a_dashboard(dashboard_tenant, session_dir):
    _, created = plan_and_apply(dashboard_tenant, session_dir, "create_dashboard",
                                {"project_id": "proj_a", "name": "Expression",
                                 "changes": STARTER})
    dashboard_id = created["result"]["created_dashboard_id"]
    listed = call(dashboard_tenant, session_dir, "list", of="dashboards",
                  scope={"project_ids": ["proj_a"]})
    assert [i["name"] for i in listed["result"]["items"]] == ["Expression"]
    read = call(dashboard_tenant, session_dir, "dashboard", id=dashboard_id,
                scope={"project_ids": ["proj_a"]}, run=True)
    result = read["result"]
    assert [v["id"] for v in result["contents"]["views"]] == ["volcano1", "table1"]
    assert result["contents"]["controls"][0]["drives"] == ["bygene.param:cond"]
    assert result["data"]["volcano1"]["profile"]["rows"] == 10
    assert len(result["data"]["volcano1"]["profile"]["sample"]) == 5
    assert result["issues"] == []


def test_the_schema_read_lists_charts_options_and_changes(dashboard_tenant, session_dir):
    result = call(dashboard_tenant, session_dir, "dashboard_schema")["result"]
    scatter = next(c for c in result["charts"] if c["type"] == "scatter")
    assert {ch["channel"] for ch in scatter["channels"]} == {"x", "y", "color", "size"}
    assert "spec" not in result["view_options"] and "xScale" in result["view_options"]
    assert result["changes"]["add_view"][:3] == ["id", "data", "type"]


def serve_sheet(tenant, rows):
    """A sheet as the live API returns it: rows of plain values, which the generated
    SheetQueryResponse model cannot parse (it expects each cell to be an object)."""
    import httpx
    router = tenant["router"]
    router.get(f"{API}/projects/proj_a/sheets").respond(json=[
        {"id": "sheet-1", "name": "Samples", "tableName": "samples",
         "namespaceName": "default", "sheetType": "TABLE", "totalRowCount": len(rows),
         "status": "COMPLETED"}])

    def page(request):
        body = json.loads(request.content)
        start = (body["page"] - 1) * body["limit"]
        return httpx.Response(200, json={
            "columns": [{"name": "_row_id", "dataType": "INTEGER"},
                        {"name": "sample", "dataType": "VARCHAR"},
                        {"name": "reads", "dataType": "DOUBLE"}],
            "rows": rows[start:start + body["limit"]], "totalRowCount": len(rows)})
    router.post(f"{API}/projects/proj_a/sheets/sheet-1/data/query").mock(side_effect=page)


def test_a_sheet_source_pages_its_rows(dashboard_tenant, session_dir, monkeypatch):
    monkeypatch.setattr(dashboard_data, "SHEET_PAGE", 2)
    serve_sheet(dashboard_tenant, [[i, f"S{i}", i * 1.5] for i in range(5)])
    planned = call(dashboard_tenant, session_dir, "plan", op="create_dashboard", args={
        "project_id": "proj_a", "name": "Samples",
        "changes": [{"op": "add_source", "id": "smpl", "sheet": "sheet-1"},
                    {"op": "add_query", "id": "bigreads", "inputs": ["smpl"],
                     "sql": "SELECT sample, reads FROM samples WHERE reads > 2"},
                    {"op": "add_view", "id": "hist", "data": "bigreads", "type": "histogram",
                     "x": "reads"}]})
    assert planned["ok"], planned
    assert "chart hist: 3 rows" in planned["rendered"]
    assert "dataset-source smpl: 5 rows" in planned["rendered"]


def test_sheet_columns_are_typed_the_way_the_portal_types_them(dashboard_tenant, session_dir):
    """Seen live: a sheet's date column arrived as text, so `date_sub` over it failed here
    while the portal, which types columns from the sheet's dataType, ran it."""
    router = dashboard_tenant["router"]
    serve_sheet(dashboard_tenant, [])
    columns = [{"name": "_row_id", "dataType": "INTEGER"},
               {"name": "when", "dataType": "DATE"}, {"name": "dose", "dataType": "STRING"},
               {"name": "seen", "dataType": "STRING"}, {"name": "arm", "dataType": "STRING"},
               {"name": "ok", "dataType": "BOOLEAN"}]
    rows = [[1, "2026-01-02", "1.5", "2026-01-03T04:05:06", "A", True],
            [2, "2026-02-03", "2", "2026-02-03", "B", False]]
    router.post(f"{API}/projects/proj_a/sheets/sheet-1/data/query").respond(
        json={"columns": columns, "rows": rows, "totalRowCount": 2})
    result = call(dashboard_tenant, session_dir, "table_preview",
                  scope={"project_ids": ["proj_a"]}, sheet="sheet-1",
                  sql='SELECT date_diff(\'day\', min("when"), max("when")) AS d, '
                      "sum(dose) AS total FROM samples")["result"]
    assert result["sample"] == [{"d": 32, "total": 3.5}]
    preview = call(dashboard_tenant, session_dir, "table_preview",
                   scope={"project_ids": ["proj_a"]}, sheet="sheet-1")["result"]
    classes = {c["name"]: c["class"] for c in preview["columns"]}
    assert classes == {"when": "temporal", "dose": "quantitative", "seen": "temporal",
                       "arm": "categorical", "ok": "categorical"}, "no _row_id, promoted text"
    assert preview["sample"][0]["ok"] == "true"


def test_a_sheet_is_named_by_id(dashboard_tenant, session_dir):
    serve_sheet(dashboard_tenant, [])
    planned = call(dashboard_tenant, session_dir, "plan", op="create_dashboard", args={
        "project_id": "proj_a", "name": "Samples",
        "changes": [{"op": "add_source", "sheet": "Samples"}]})
    assert "'Samples' is the name of ['sheet-1']" in planned["error"]["message"]


def test_preview_loads_a_file_and_runs_sql_over_it(dashboard_tenant, session_dir):
    result = call(dashboard_tenant, session_dir, "table_preview",
                  scope={"project_ids": ["proj_a"]}, dataset_id="ds_0001", path="counts.csv",
                  sql="SELECT condition, count(*) AS n FROM counts "
                      "GROUP BY 1 ORDER BY 1")["result"]
    assert result["rows"] == 2
    assert result["sample"] == [{"condition": "control", "n": 20},
                                {"condition": "treated", "n": 20}]
    assert result["sql_names"] == ["query", "counts"]

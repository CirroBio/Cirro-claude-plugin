"""Sheets: the column, value and selection rules, and each operation end to end against
an in-memory sheets backend that behaves the way Cirro-backend's does."""

import json

import pytest

from cirro_agent import sheets as S
from cirro_agent.worker import dispatch

from conftest import API, approved_summary

SAMPLES = [
    {"name": "sample_id", "dataType": "STRING", "semanticType": "STANDARD", "required": True,
     "unique": True, "id": "1"},
    {"name": "status", "dataType": "STRING", "semanticType": "ENUM_SINGLE",
     "allowedValues": ["pending", "done", "failed"], "id": "2"},
    {"name": "reads", "dataType": "BIGINT", "semanticType": "STANDARD", "id": "3"},
    {"name": "collected", "dataType": "DATE", "semanticType": "STANDARD", "id": "4"},
    {"name": "sys_created_at", "dataType": "TIMESTAMP", "id": "90"},
    {"name": "sys_updated_at", "dataType": "TIMESTAMP", "id": "91"},
]


# --- columns and values ----------------------------------------------------------

def test_a_label_given_as_the_name_becomes_the_label():
    column = S.column_def({"name": "Sample ID", "required": True}, "columns[0]")
    assert column["name"] == "sample_id" and column["displayName"] == "Sample ID"
    assert column["dataType"] == "STRING" and column["required"] is True


def test_values_make_an_enum_and_a_reference_kind_holds_text():
    assert S.column_def({"name": "s", "values": ["a", "b"]}, "c")["semanticType"] == \
        "ENUM_SINGLE"
    with pytest.raises(S.SheetError, match="holds text"):
        S.column_def({"name": "f", "kind": "CIRRO_FILE", "type": "INTEGER"}, "c")
    with pytest.raises(S.SheetError, match="cannot be unique"):
        S.column_def({"name": "x", "type": "DOUBLE", "unique": True}, "c")
    with pytest.raises(S.SheetError, match="reserved"):
        S.column_def({"name": "sys_note"}, "c")


def test_columns_are_unique_ignoring_case():
    with pytest.raises(S.SheetError, match="repeats"):
        S.check_columns([{"name": "a"}, {"name": "A"}])


@pytest.mark.parametrize("column,value,stored", [
    ({"dataType": "BIGINT"}, "1,200", 1200),
    ({"dataType": "BIGINT"}, 3.0, 3),
    ({"dataType": "DOUBLE"}, "2.5", 2.5),
    ({"dataType": "BOOLEAN"}, "yes", True),
    ({"dataType": "DATE"}, "2026-09-30T00:00:00Z", "2026-09-30"),
    ({"dataType": "TIMESTAMP"}, "2026-09-30T14:05:00-07:00", "2026-09-30 21:05:00"),
    ({"dataType": "STRING"}, 12.0, "12"),
    ({"dataType": "STRING", "semanticType": "CIRRO_FILE"},
     {"dataset": "ds_1", "path": "data/reads/a.fastq.gz"},
     "cirro::proj_a:data:ds_1/reads/a.fastq.gz"),
])
def test_values_are_stored_as_their_column_holds_them(column, value, stored):
    assert S.coerce(column, value, "proj_a") == stored


@pytest.mark.parametrize("column,value,reason", [
    ({"dataType": "INTEGER"}, "3.5", "whole number"),
    ({"dataType": "INTEGER"}, 2**40, "range"),
    ({"dataType": "BIGINT"}, True, "true/false"),
    ({"dataType": "DATE"}, "2026-09-30T08:00:00", "date"),
    ({"dataType": "BOOLEAN"}, "maybe", "true or false"),
    ({"dataType": "STRING", "semanticType": "ENUM_SINGLE", "allowedValues": ["a"]}, "b",
     "allowed"),
    ({"dataType": "STRING", "semanticType": "CIRRO_DATASET"}, "cirro::p:data:ds/x",
     "dataset alone"),
    ({"dataType": "STRING", "required": True}, None, "required"),
])
def test_values_that_would_not_cast_are_refused(column, value, reason):
    with pytest.raises(ValueError, match=reason):
        S.coerce(column, value, "proj_a")


def test_row_problems_name_the_row_and_the_column():
    rows, problems = S.check_rows(SAMPLES, [{"sample_id": "a", "reads": "many"},
                                            {"status": "done", "extra": 1}], inserting=True)
    assert "rows[0].reads: 'many' is not a whole number" in problems
    assert "rows[1]: there is no column 'extra'" in problems
    assert "rows[1]: required column(s) ['sample_id'] are missing" in problems
    assert S.duplicates(SAMPLES, [{"sample_id": "a"}, {"sample_id": "a"}])


def test_where_shorthand_becomes_the_backend_filter():
    columns = S.user_columns(SAMPLES)
    assert S.row_filter({"status": "failed"}, columns) == {
        "column": "status", "operator": "EQUALS", "values": ["failed"]}
    assert S.row_filter({"reads": {">=": "10"}, "collected": None}, columns) == {
        "logicalOperator": "AND",
        "conditions": [{"column": "reads", "operator": "GREATER_THAN_OR_EQUALS",
                        "values": [10]},
                       {"column": "collected", "operator": "IS_NULL"}]}
    with pytest.raises(S.SheetError, match="no column 'nope'"):
        S.row_filter({"nope": 1}, columns)


def test_altering_columns_keeps_their_ids():
    target = S.alter_columns(SAMPLES, drop=["collected"],
                             alter={"reads": {"rename": "read_count", "label": "Reads"}},
                             add=[{"name": "notes"}])
    assert [(c["name"], c.get("id")) for c in target] == [
        ("sample_id", "1"), ("status", "2"), ("read_count", "3"), ("notes", None)]
    with pytest.raises(S.SheetError, match="cannot be made required"):
        S.alter_columns(SAMPLES, None, {"status": {"required": True}}, None)


def test_a_type_only_widens():
    """dev's dry run accepted DOUBLE -> FLOAT, and the ALTER then failed with a 500."""
    columns = SAMPLES + [{"name": "conc", "dataType": "FLOAT", "id": "5"}]
    assert S.alter_columns(columns, None, {"conc": {"type": "DOUBLE"}}, None)[-1][
        "dataType"] == "DOUBLE"
    with pytest.raises(S.SheetError, match="only widens"):
        S.alter_columns(columns, None, {"reads": {"type": "INTEGER"}}, None)


def test_a_timestamp_read_back_equals_the_one_written():
    column = {"dataType": "TIMESTAMP"}
    assert S.stored_equal(column, "2026-09-02T08:30:00Z", "2026-09-02 08:30:00")
    assert not S.stored_equal(column, None, "2026-09-02 08:30:00")


# --- a sheets backend ------------------------------------------------------------

def _matches(row, node):
    if "conditions" in node:
        results = [_matches(row, c) for c in node["conditions"]]
        return all(results) if node["logicalOperator"] == "AND" else any(results)
    value, values = row.get(node["column"]), node.get("values") or []
    return {"EQUALS": lambda: value == values[0],
            "NOT_EQUALS": lambda: value != values[0],
            "IN": lambda: value in values,
            "IS_NULL": lambda: value is None,
            "GREATER_THAN_OR_EQUALS": lambda: value is not None and value >= values[0],
            }[node["operator"]]()


@pytest.fixture
def sheets_tenant(writable_tenant):
    import httpx
    router = writable_tenant["router"]
    state = {"sheets": {}, "rows": {}, "next_row": 1, "calls": []}

    def add_sheet(sheet_id, name, table, columns, rows, **extra):
        state["sheets"][sheet_id] = {
            "id": sheet_id, "name": name, "description": "", "projectId": "proj_a",
            "namespaceName": "default", "tableName": table, "sheetType": "TABLE",
            "status": "COMPLETED", "auditReadAccess": False, "createdBy": "sminot",
            "createdAt": "2026-09-01T00:00:00Z", "updatedAt": "2026-09-01T00:00:00Z",
            "tags": [], "sheetCreationMode": "SCRATCH", "columns": columns,
            "schemaVersionId": 0, **extra}
        state["rows"][sheet_id] = []
        for row in rows:
            _insert(sheet_id, row)

    def _insert(sheet_id, values):
        row = {"_row_id": state["next_row"], **values,
               "sys_created_at": "2026-09-01 00:00:00",
               "sys_updated_at": "2026-09-01 00:00:00"}
        state["next_row"] += 1
        state["rows"][sheet_id].append(row)

    add_sheet("sheet-1", "Samples", "samples", [dict(c) for c in SAMPLES], [
        {"sample_id": "s1", "status": "pending", "reads": 100, "collected": "2026-01-01"},
        {"sample_id": "s2", "status": "failed", "reads": 5, "collected": None},
        {"sample_id": "s3", "status": "failed", "reads": 50, "collected": "2026-01-03"}])

    def detail(sheet_id):
        sheet = state["sheets"][sheet_id]
        return {**sheet, "totalRowCount": len(state["rows"][sheet_id])
                if sheet["sheetType"] == "TABLE" else None}

    def listing(request):
        return httpx.Response(200, json=[{k: v for k, v in detail(i).items()
                                          if k not in ("columns", "viewDefinition")}
                                         for i in state["sheets"]])

    def create(request):
        body = json.loads(request.content)
        state["calls"].append(("create", body))
        if any(s["name"].lower() == body["name"].lower() for s in state["sheets"].values()):
            return httpx.Response(400, json={"message": "Sheet name already exists"})
        sheet_id = f"sheet-{len(state['sheets']) + 1}"
        columns = [{**c, "id": str(i + 1)} for i, c in enumerate(body.get("columns", []))]
        add_sheet(sheet_id, body["name"], body["tableName"], columns, [],
                  sheetType=body["sheetType"], viewDefinition=body.get("viewDefinition"),
                  virtual=body.get("virtual"))
        return httpx.Response(201, json={"id": sheet_id, "message": "created"})

    def one(request):
        sheet_id = request.url.path.rsplit("/", 1)[-1]
        if sheet_id not in state["sheets"]:
            return httpx.Response(404, json={"message": "not found"})
        if request.method == "GET":
            return httpx.Response(200, json=detail(sheet_id))
        if request.method == "DELETE":
            state["calls"].append(("delete_sheet", sheet_id))
            del state["sheets"][sheet_id]
            return httpx.Response(204)
        body = json.loads(request.content)
        sheet = state["sheets"][sheet_id]
        if body.get("schemaVersionId") not in (None, sheet["schemaVersionId"]):
            return httpx.Response(409, json={"message": "Schema version is out of date"})
        ids = {c["id"] for c in sheet["columns"]}
        changes = [f"Add column '{c['name']}'" for c in body.get("columns", [])
                   if not c.get("id")]
        changes += [f"Drop column '{c['name']}'" for c in sheet["columns"]
                    if not c["name"].startswith("sys_")
                    and c["id"] not in {x.get("id") for x in body.get("columns", [])}]
        dry_run = request.url.params.get("dryRun") == "true"
        state["calls"].append(("update_sheet", dry_run, body))
        if not dry_run:
            sheet.update({k: v for k, v in body.items() if k != "schemaVersionId"})
            sheet["columns"] = ([c for c in body.get("columns", []) if c.get("id") in ids]
                                + [{**c, "id": str(100 + i)} for i, c in
                                   enumerate(body.get("columns", [])) if not c.get("id")])
            sheet["schemaVersionId"] += 1
        return httpx.Response(200, json={"sheet": detail(sheet_id), "changes": changes,
                                         "sqlStatements": [f"ALTER TABLE ... -- {c}"
                                                           for c in changes]})

    def query(request):
        sheet_id = request.url.path.split("/")[-3]
        body = json.loads(request.content)
        rows = [r for r in state["rows"][sheet_id]
                if not body.get("filter") or _matches(r, body["filter"])]
        start = (body["page"] - 1) * body["limit"]
        page = rows[start:start + body["limit"]]
        names = ["_row_id"] + [c["name"] for c in state["sheets"][sheet_id]["columns"]]
        return httpx.Response(200, json={"columns": [{"name": n} for n in names],
                                         "rows": [[r.get(n) for n in names] for r in page],
                                         "totalRowCount": len(rows)})

    def data(request):
        sheet_id = request.url.path.split("/")[-2]
        body = json.loads(request.content)
        state["calls"].append((request.method, body))
        if request.method == "POST":
            for insert in body["inserts"]:
                _insert(sheet_id, insert["values"])
            return httpx.Response(200, json={"rowsAffected": len(body["inserts"])})
        if request.method == "PATCH":
            by_id = {r["_row_id"]: r for r in state["rows"][sheet_id]}
            for update in body["updates"]:
                by_id[update["rowId"]].update(update["values"],
                                              sys_updated_at="2026-09-30 00:00:00")
            return httpx.Response(200, json={"rowsAffected": len(body["updates"])})
        before = len(state["rows"][sheet_id])
        state["rows"][sheet_id] = [r for r in state["rows"][sheet_id]
                                   if r["_row_id"] not in body["rowIds"]]
        return httpx.Response(200, json={
            "rowsAffected": before - len(state["rows"][sheet_id])})

    def raw_query(request):
        body = json.loads(request.content)
        if "broken" in body["query"]:
            return httpx.Response(400, json={"message": "Parser Error: syntax error"})
        return httpx.Response(200, json={"columns": [{"name": "status"}, {"name": "n"}],
                                         "rows": [["failed", 2], ["pending", 1]],
                                         "totalRowCount": 2})

    dashboard = {"id": "dash-1", "name": "QC", "criteria": {"type": "sql-room"},
                 "dashboardData": {"version": 1, "ir": {"nodes": [
                     {"id": "src1", "type": "dataset-source",
                      "config": {"sheetId": "sheet-1", "tableName": "samples"}}],
                     "edges": []}, "positions": {}, "dashboards": []}}
    router.get(f"{API}/projects/proj_a/dashboards").respond(
        json=[{k: v for k, v in dashboard.items() if k != "dashboardData"}])
    router.get(f"{API}/projects/proj_a/dashboards/dash-1").respond(json=dashboard)
    router.post(f"{API}/projects/proj_a/sheets/raw-query").mock(side_effect=raw_query)
    router.get(f"{API}/projects/proj_a/sheets").mock(side_effect=listing)
    router.post(f"{API}/projects/proj_a/sheets").mock(side_effect=create)
    router.post(url__regex=rf"{API}/projects/proj_a/sheets/[^/]+/data/query$").mock(
        side_effect=query)
    router.route(url__regex=rf"{API}/projects/proj_a/sheets/[^/]+/data$").mock(
        side_effect=data)
    router.put(url__regex=rf"{API}/projects/proj_a/sheets/[^/]+:refresh$").respond(
        status_code=200)
    router.get(url__regex=rf"{API}/projects/proj_a/sheets/[^/]+/jobs$").respond(json=[
        {"id": "job-1", "jobType": "MATERIALIZE_VIEW", "status": "FAILED",
         "createdAt": "2026-09-30T22:00:09Z",
         "errorMessage": "Permission Error: File system LocalFileSystem has been disabled"}])
    router.route(url__regex=rf"{API}/projects/proj_a/sheets/[^/:]+$").mock(side_effect=one)
    return {**writable_tenant, "sheets": state}


def call(tenant, session_dir, verb, **overrides):
    return dispatch.run({"verb": verb, "base_url": tenant["base_url"],
                         "session_dir": str(session_dir.path), **overrides})


def plan(tenant, session_dir, op, **args):
    return call(tenant, session_dir, "plan", op=op, args={"project_id": "proj_a", **args})


def apply(tenant, session_dir, planned):
    assert planned["ok"], planned
    return call(tenant, session_dir, "apply", plan_id=planned["plan_id"],
                digest=planned["digest"],
                approved_summary=approved_summary(session_dir, planned["plan_id"]))


def writes(state, kind):
    return [c for c in state["calls"] if c[0] == kind]


# --- create ------------------------------------------------------------------------

def test_create_a_table_with_rows(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "create_sheet", name="Plate map",
                   columns=[{"name": "Well", "required": True, "unique": True},
                            {"name": "conc", "type": "DOUBLE"},
                            {"name": "ok", "type": "BOOLEAN"}],
                   rows=[{"well": "A1", "conc": "1.5", "ok": "yes"},
                         {"well": "A2", "conc": 2, "ok": False}])
    text = planned["rendered"]
    assert "Table        default.plate_map" in text
    assert "well STRING required unique 'Well'" in text
    assert "A1    1.5   true" in text
    applied = apply(sheets_tenant, session_dir, planned)
    assert applied["ok"], applied
    result = applied["result"]
    assert result["rows_inserted"] == 2
    assert result["url"] == ("https://app.cirro.test/project/proj_a/sheets/"
                             + result["created_sheet_id"])
    state = sheets_tenant["sheets"]
    body = writes(state, "create")[0][1]
    assert body["sheetCreationMode"] == "SCRATCH" and body["tableName"] == "plate_map"
    inserted = [r for r in state["rows"][result["created_sheet_id"]]]
    assert (inserted[0]["conc"], inserted[0]["ok"]) == (1.5, True), \
        "values go over the wire as their column's type, not as given"


def test_create_from_a_local_file_infers_the_columns(sheets_tenant, session_dir, tmp_path):
    table = tmp_path / "qc.csv"
    table.write_text("Sample ID,reads,collected\ns1,100,2026-01-01\ns2,5,2026-01-02\n")
    planned = plan(sheets_tenant, session_dir, "create_sheet", name="QC metrics",
                   file=str(table))
    text = planned["rendered"]
    assert "sample_id STRING 'Sample ID'" in text
    assert "reads BIGINT" in text and "collected DATE" in text
    applied = apply(sheets_tenant, session_dir, planned)
    rows = sheets_tenant["sheets"]["rows"][applied["result"]["created_sheet_id"]]
    assert rows[1]["collected"] == "2026-01-02" and rows[1]["sample_id"] == "s2"


def test_create_refuses_a_name_the_project_already_has(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "create_sheet", name="samples",
                   columns=[{"name": "a"}])
    assert planned["error"]["code"] == "INVALID_QUERY"
    assert "sheet-1" in planned["error"]["message"]


def test_create_a_view_runs_its_sql_first(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "create_sheet", name="Failures",
                   view_sql="SELECT status, count(*) n FROM samples GROUP BY 1")
    assert "virtual view" in planned["rendered"]
    assert "Columns  status, n" in planned["rendered"]
    broken = plan(sheets_tenant, session_dir, "create_sheet", name="Broken",
                  view_sql="SELECT broken FROM")
    assert broken["error"]["code"] == "INVALID_QUERY"
    assert "syntax error" in broken["error"]["message"]


# --- rows --------------------------------------------------------------------------

def test_insert_refuses_values_that_would_not_go(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "insert_rows", sheet_id="sheet-1",
                   rows=[{"sample_id": "s9", "status": "lost"},
                         {"sample_id": "s1", "reads": 1}])
    assert planned["error"]["code"] == "INVALID_QUERY"
    message = planned["error"]["message"]
    assert "rows[0].status: 'lost' is not one of the allowed values" in message
    assert not writes(sheets_tenant["sheets"], "POST")


def test_insert_refuses_a_unique_value_the_sheet_already_holds(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "insert_rows", sheet_id="sheet-1",
                   rows=[{"sample_id": "s1"}])
    assert "sample_id 's1' is already on row 1" in planned["error"]["message"]


def test_insert_sends_batches_the_endpoint_accepts(sheets_tenant, session_dir):
    rows = [{"sample_id": f"n{i}", "reads": i} for i in range(2500)]
    planned = plan(sheets_tenant, session_dir, "insert_rows", sheet_id="sheet-1", rows=rows)
    assert "Sent as 3 requests of up to 1,000 rows" in planned["rendered"]
    applied = apply(sheets_tenant, session_dir, planned)
    assert applied["result"]["rows_inserted"] == 2500
    assert [len(c[1]["inserts"]) for c in writes(sheets_tenant["sheets"], "POST")] == \
        [1000, 1000, 500]


def test_update_rows_shows_each_change_and_writes_it(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "update_rows", sheet_id="sheet-1",
                   where={"status": "failed"}, set={"status": "pending", "reads": "0"})
    text = planned["rendered"]
    assert "Selected  2 row(s) where" in text
    assert "row 2: status 'failed' -> 'pending'; reads 5 -> 0" in text
    applied = apply(sheets_tenant, session_dir, planned)
    assert applied["result"]["rows_updated"] == 2
    statuses = [r["status"] for r in sheets_tenant["sheets"]["rows"]["sheet-1"]]
    assert statuses == ["pending", "pending", "pending"]


def test_update_rows_refuses_rows_edited_since_the_plan(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "update_rows", sheet_id="sheet-1",
                   row_ids=[2], set={"status": "done"})
    # Someone edits the cell in the portal between plan and apply.
    row = sheets_tenant["sheets"]["rows"]["sheet-1"][1]
    row.update(reads=6, sys_updated_at="2026-09-30 09:00:00")
    applied = apply(sheets_tenant, session_dir, planned)
    assert applied["error"]["code"] == "PLAN_STALE"
    assert row["status"] == "failed" and not writes(sheets_tenant["sheets"], "PATCH")


def test_rows_that_already_hold_the_values_are_nothing_to_do(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "update_rows", sheet_id="sheet-1",
                   where={"sample_id": "s1"}, set={"status": "pending"})
    assert planned["error"]["code"] == "INVALID_QUERY"
    assert "already hold those values" in planned["error"]["message"]


def test_per_row_updates_and_a_unique_column(sheets_tenant, session_dir):
    same = plan(sheets_tenant, session_dir, "update_rows", sheet_id="sheet-1",
                where={"status": "failed"}, set={"sample_id": "x"})
    assert "must be unique" in same["error"]["message"]
    planned = plan(sheets_tenant, session_dir, "update_rows", sheet_id="sheet-1",
                   updates=[{"row_id": 2, "values": {"sample_id": "s2b"}},
                            {"row_id": 3, "values": {"reads": 51}}])
    assert apply(sheets_tenant, session_dir, planned)["result"]["rows_updated"] == 2


def test_delete_rows_is_destructive_and_shows_the_rows(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "delete_rows", sheet_id="sheet-1",
                   where={"reads": {">=": 50}})
    assert planned["risk"] == "cirro_destructive"
    assert "DELETE 2 row(s)" in planned["rendered"] and "s3" in planned["rendered"]
    assert "3 rows now, 1 after" in planned["rendered"]
    applied = apply(sheets_tenant, session_dir, planned)
    assert applied["result"]["rows_deleted"] == 2
    assert [r["sample_id"] for r in sheets_tenant["sheets"]["rows"]["sheet-1"]] == ["s2"]


def test_a_selection_needs_exactly_one_of_where_and_row_ids(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "delete_rows", sheet_id="sheet-1")
    assert planned["error"]["code"] == "INVALID_QUERY"
    missing = plan(sheets_tenant, session_dir, "delete_rows", sheet_id="sheet-1",
                   row_ids=[1, 99])
    assert missing["error"]["code"] == "NOT_FOUND" and "[99]" in missing["error"]["message"]


def test_row_writes_take_an_id_not_a_name(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "insert_rows", sheet_id="Samples",
                   rows=[{"sample_id": "x"}])
    assert planned["error"]["code"] == "NAME_REQUIRED_ID"
    assert planned["error"]["remediation"]["retry_with"] == [{"sheet_id": "sheet-1"}]


# --- the sheet itself --------------------------------------------------------------

def test_update_sheet_shows_the_dry_run_and_who_reads_it(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "update_sheet", sheet_id="sheet-1",
                   add_columns=[{"name": "notes"}], drop_columns=["collected"])
    text = planned["rendered"]
    assert "Add column 'notes'" in text and "Drop column 'collected'" in text
    assert "dashboard 'QC'" in text
    dry = writes(sheets_tenant["sheets"], "update_sheet")[0]
    assert dry[1] is True, "the plan only ever sends a dry run"
    applied = apply(sheets_tenant, session_dir, planned)
    assert applied["ok"], applied
    sent = writes(sheets_tenant["sheets"], "update_sheet")[-1]
    assert sent[1] is False and sent[2]["schemaVersionId"] == 0
    names = [c["name"] for c in sheets_tenant["sheets"]["sheets"]["sheet-1"]["columns"]]
    assert "notes" in names and "collected" not in names


def test_update_sheet_refuses_a_schema_changed_since_the_plan(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "update_sheet", sheet_id="sheet-1",
                   add_columns=[{"name": "notes"}])
    sheets_tenant["sheets"]["sheets"]["sheet-1"]["schemaVersionId"] = 7
    applied = apply(sheets_tenant, session_dir, planned)
    assert applied["error"]["code"] == "PLAN_STALE"


def test_a_rename_alone_skips_the_dry_run(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "update_sheet", sheet_id="sheet-1",
                   name="Sample tracker")
    assert "Name         'Samples' -> 'Sample tracker'" in planned["rendered"]
    assert not writes(sheets_tenant["sheets"], "update_sheet")
    assert apply(sheets_tenant, session_dir, planned)["ok"]
    assert sheets_tenant["sheets"]["sheets"]["sheet-1"]["name"] == "Sample tracker"


def test_delete_sheet_names_what_reads_it(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "delete_sheet", sheet_id="sheet-1")
    assert planned["risk"] == "cirro_destructive"
    assert "Rows        3" in planned["rendered"] and "dashboard 'QC'" in planned["rendered"]
    assert apply(sheets_tenant, session_dir, planned)["ok"]
    assert "sheet-1" not in sheets_tenant["sheets"]["sheets"]


def test_delete_sheet_refuses_when_rows_were_added_since(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "delete_sheet", sheet_id="sheet-1")
    sheets_tenant["sheets"]["rows"]["sheet-1"].append({"_row_id": 50, "sample_id": "late"})
    assert apply(sheets_tenant, session_dir, planned)["error"]["code"] == "PLAN_STALE"


def test_refresh_is_for_materialized_views(sheets_tenant, session_dir):
    planned = plan(sheets_tenant, session_dir, "refresh_sheet", sheet_id="sheet-1")
    assert planned["error"]["code"] == "NOT_SUPPORTED"


def test_a_failed_sheet_reads_with_its_last_jobs_error_and_can_be_deleted(sheets_tenant,
                                                                          session_dir):
    """Observed on dev: a materialized view whose job failed answers "Sheet is not
    ready" for its rows and refuses a refresh, but the backend still deletes it."""
    sheets_tenant["sheets"]["sheets"]["sheet-1"]["status"] = "FAILED"
    read = call(sheets_tenant, session_dir, "sheet", id="sheet-1",
                scope={"project_ids": ["proj_a"]})
    assert read["result"]["rows"] == []
    assert "LocalFileSystem has been disabled" in read["result"]["notes"][0]
    inserted = plan(sheets_tenant, session_dir, "insert_rows", sheet_id="sheet-1",
                    rows=[{"sample_id": "x"}])
    assert inserted["error"]["code"] == "WRONG_STATUS"
    assert apply(sheets_tenant, session_dir,
                 plan(sheets_tenant, session_dir, "delete_sheet", sheet_id="sheet-1"))["ok"]


# --- reading -----------------------------------------------------------------------

def test_read_sheet_shows_columns_and_row_ids(sheets_tenant, session_dir):
    read = call(sheets_tenant, session_dir, "sheet", id="Samples",
                scope={"project_ids": ["proj_a"]}, where={"status": "failed"})
    result = read["result"]
    assert [c["name"] for c in result["columns"]] == [
        "sample_id", "status", "reads", "collected"]
    assert result["columns"][1]["allowed_values"] == ["pending", "done", "failed"]
    assert [(r["_row_id"], r["sample_id"]) for r in result["rows"]] == [(2, "s2"), (3, "s3")]
    assert not any(k.startswith("sys_") for r in result["rows"] for k in r)
    assert result["matched"] == 2


# --- which row writes a grant may cover -------------------------------------------

def test_inline_rows_are_grantable_and_a_local_file_is_not(sheets_tenant, session_dir,
                                                           tmp_path):
    """A file source reads this computer; under a grant nobody would see which file."""
    inline = plan(sheets_tenant, session_dir, "insert_rows", sheet_id="sheet-1",
                  rows=[{"sample_id": "n1", "reads": 1}])
    assert inline["grant_scope"]["op"] == "insert_rows"
    table = tmp_path / "more.csv"
    table.write_text("sample_id,reads\nn2,2\n")
    from_file = plan(sheets_tenant, session_dir, "insert_rows", sheet_id="sheet-1",
                     file=str(table))
    assert from_file["grant_scope"] is None
    assert "file from this computer" in from_file["not_grantable"]


def test_an_update_that_blanks_cells_is_not_grantable(sheets_tenant, session_dir):
    changed = plan(sheets_tenant, session_dir, "update_rows", sheet_id="sheet-1",
                   where={"status": "failed"}, set={"status": "pending"})
    assert changed["grant_scope"]["op"] == "update_rows"
    blanked = plan(sheets_tenant, session_dir, "update_rows", sheet_id="sheet-1",
                   where={"status": "failed"}, set={"reads": None})
    assert blanked["grant_scope"] is None
    assert "blanks cells" in blanked["not_grantable"]

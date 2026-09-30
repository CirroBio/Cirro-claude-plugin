"""Reading Data Studio dashboards: one dashboard, the authoring catalog, a table preview.

A dashboard only ever executes in a browser, so "viewing" one here means two things:
`describe` -- what it is made of, in sources, queries, views and controls -- and, with
`run`, executing its data in DuckDB exactly as the portal would and reporting what each
view would draw.
"""

from .. import dashboards as D, errors
from ..envelope import DEFAULT_MAX_TOKENS, wrap
from . import dashboard_data
from .reads import Reader, dashboard_record, dashboard_schema, dataset_manifest, sheets

SAMPLE_ROWS = 5
PREVIEW_LIMIT = 20


def _envelope(reader: Reader, request: dict, query: dict, result: dict) -> dict:
    return wrap(query=query, result=result, cost=reader.cost.to_dict(),
                max_tokens=int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["query"]))


def do_dashboard(request: dict) -> dict:
    reader = Reader(request)
    project_id = reader.scope_project_ids(request.get("scope") or {})[0]
    record = dashboard_record(reader, project_id, request["id"])
    schema, note = dashboard_schema(reader)
    document = record.get("dashboardData")
    result = {"id": record["id"], "name": record["name"],
              "description": record.get("description"), "created_by": record.get("createdBy"),
              "updated_at": record.get("updatedAt"),
              "in_data_studio": (record.get("criteria") or {}).get("type") == D.SESSION_TYPE,
              "revision": (record.get("criteria") or {}).get("revision"),
              "url": D.studio_url(reader.base_url, project_id, record["id"])}
    notes = [note] if note else []
    if not D.is_session_blob(document):
        result["empty"] = True
        notes.append(
            "the record holds no saved layout yet (the portal writes it on first save)")
    else:
        result["contents"] = D.describe(schema, document)
        issues = D.validate(schema, document, strict_nodes=set())
        if request.get("run"):
            runner = dashboard_data.Runner(reader, project_id, schema)
            results = runner.run(document, sample_rows=SAMPLE_ROWS)
            issues += dashboard_data.view_issues(schema, document, results)
            result["data"] = results
        result["issues"] = issues
        if request.get("raw"):
            result["dashboard_data"] = document
    if notes:
        result["notes"] = notes
    result["next_calls"] = [{"id": "nc_edit",
                             "why": "change it: plan edit_dashboard with a change list",
                             "call": {"tool": "plan", "op": "edit_dashboard",
                                      "args": {"project_id": project_id,
                                               "dashboard_id": record["id"], "changes": []}}},
                            {"id": "nc_schema",
                             "why": "every change, chart type and option an edit can use",
                             "call": {"verb": "dashboard_schema",
                                      "command": "cirro-agent read dashboard-schema"}}]
    if not request.get("run"):
        result["next_calls"].insert(0, {
            "id": "nc_run", "why": "execute its data and see what each view draws",
            "call": {"command": f"cirro-agent read dashboard {record['id']} "
                                f"-p {project_id} --run"}})
    return _envelope(reader, request, {"verb": "dashboard", "id": request["id"]}, result)


def do_dashboard_schema(request: dict) -> dict:
    """What an author can write: charts and their channels, view options, controls,
    node types, the change grammar and the grid. Needs the tenant host, not a login."""
    reader = Reader(request)
    schema, note = dashboard_schema(reader)
    chart_props = schema.config_properties("chart") or {}
    result = {
        "source": schema.source,
        "charts": [{"type": c["id"], "label": c["label"],
                    "channels": [{"channel": ch["channel"], "label": ch.get("label"),
                                  "required": ch["required"], "accepts": ch["accepts"],
                                  **{k: ch[k] for k in ("minDistinct", "maxDistinct",
                                                        "binAbove") if k in ch}}
                                 for ch in c["channels"]]}
                   for c in schema.raw["charts"]],
        "view_options": {k: {key: v[key] for key in ("type", "enum", "default", "description")
                             if key in v}
                         for k, v in chart_props.items() if k not in D._VIEW_MANAGED},
        "control_kinds": list(D.CONTROL_KINDS),
        "changes": D.change_grammar(),
        "node_types": [{"type": t["type"], "label": t["label"],
                        "description": t["description"], "tile": t["renderable"]}
                       for t in schema.raw["nodeTypes"] if t["addableViaMenu"]],
        "grid": schema.grid,
        "sql": ("A query reads its inputs as `query` (the first), `in1`..`inN`, and a file "
                "or sheet input by its table name. `{name}` in SQL is a token a control "
                "fills; DuckDB dialect.")}
    if note:
        result["notes"] = [note]
    return _envelope(reader, request, {"verb": "dashboard_schema"}, result)


def do_table_preview(request: dict) -> dict:
    """One file or sheet, loaded the way a dashboard source would be, optionally with a
    query run over it -- the columns, their classes and a few rows, before anything
    is built on it."""
    reader = Reader(request)
    project_id = reader.scope_project_ids(request.get("scope") or {})[0]
    schema, note = dashboard_schema(reader)
    runner = dashboard_data.Runner(reader, project_id, schema)
    limit = int(request.get("limit") or PREVIEW_LIMIT)
    if request.get("sheet"):
        rows = sheets(reader, project_id)
        sheet = next((s for s in rows
                      if request["sheet"] in (s.get("id"), s.get("name"), s.get("tableName"))),
                     None)
        if sheet is None:
            raise errors.NotFound(f"No sheet {request['sheet']!r} in this project.",
                                  what="Pick a sheet.", field="sheet",
                                  candidates=[{"id": s.get("id"), "name": s.get("name")}
                                              for s in rows],
                                  retry_with=[{"verb": "list", "of": "sheets",
                                               "scope": {"project_ids": [project_id]}}])
        name, source = sheet["tableName"], {"sheet_id": sheet["id"], "name": sheet.get("name")}
        load_note = runner.load_sheet("cell_preview_out", sheet["id"])
    else:
        if not request.get("dataset_id") or not request.get("path"):
            raise errors.InvalidQuery("A preview needs a dataset and a path, or a sheet.",
                                      what="Name the table to load.", field="path",
                                      retry_with=[{"verb": "table_preview",
                                                   "dataset_id": "...", "path": "..."}])
        entries, _, _ = dataset_manifest(reader, project_id, request["dataset_id"])
        by_path = {e["path"]: e for e in entries}
        entry = by_path.get(request["path"]) or by_path.get(f"data/{request['path']}")
        if entry is None or not D.file_type(entry["path"]):
            raise errors.NotFound(
                f"{request['path']!r} is not a tabular file in dataset "
                f"{request['dataset_id']}.",
                what="Pick a CSV, TSV, Parquet or JSON file.", field="path",
                candidates=sorted(p for p in by_path if D.file_type(p))[:30],
                retry_with=[{"verb": "summarize", "of": "files",
                             "dataset_id": request["dataset_id"]}])
        name = D.file_table_name(entry["path"])
        source = {"dataset_id": request["dataset_id"], "path": entry["path"],
                  "size": entry.get("size")}
        runner.load_file("cell_preview_out", request["dataset_id"], entry["path"],
                         D.file_type(entry["path"]), entry.get("size"))
        load_note = None
    con = runner.con
    con.execute("CREATE OR REPLACE TEMP VIEW query AS SELECT * FROM cell_preview_out")
    if D._IDENTIFIER.fullmatch(name):
        con.execute(f"CREATE OR REPLACE TEMP VIEW {name} AS SELECT * FROM cell_preview_out")
    table = "cell_preview_out"
    if request.get("sql"):
        import duckdb

        try:
            con.execute(f"CREATE OR REPLACE TABLE cell_sql_out AS {request['sql']}")
        except duckdb.Error as exc:
            raise errors.InvalidQuery(
                f"The query failed: {dashboard_data._duck_message(exc)}",
                what=f"The table is readable as `query` or `{name}`; fix the SQL "
                     "(DuckDB dialect).",
                field="sql", given=request["sql"],
                retry_with=[{"verb": "table_preview",
                             **{k: v for k, v in request.items()
                                if k in ("dataset_id", "path", "sheet")}}])
        table = "cell_sql_out"
    result = {"source": source, "sql_names": ["query", name],
              **dashboard_data.profile(con, table, sample_rows=limit)}
    notes = [n for n in (note, load_note) if n]
    if notes:
        result["notes"] = notes
    return _envelope(reader, request,
                     {"verb": "table_preview",
                      **{k: request.get(k) for k in ("dataset_id", "path", "sheet", "sql")
                         if request.get(k)}}, result)

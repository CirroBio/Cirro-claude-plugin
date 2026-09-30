"""Sheets: create, change and delete a sheet, and write its rows, each behind plan and apply.

Registers its operations into `mutations.OPS` on import; see mutations.py for why the
registration lives on this side.

A sheet is shared, live data: dashboards read it, views are built on it, and the
portal edits it cell by cell. So a plan names what the write reaches -- the rows it
selects, with their current values, and for a change to the sheet itself the
dashboards and views that read it -- and apply refuses when what was planned against
has moved:

- a row write re-reads the rows it selected and compares them, `sys_updated_at`
  included, so a cell someone edited in the portal since the plan stops it;
- a schema write sends the `schemaVersionId` it was planned against, which the
  backend checks, and first compares the sheet's names, columns and view definition.

Every value is coerced to its column's type at plan time (sheets.py): the backend
binds cells straight into DuckDB, and a cast it cannot make comes back as a 500 with
the row and column gone.
"""

import re
from pathlib import Path

from .. import dashboards as D, errors, sheets as S
from . import mutations, reads
from .mutations import (Draft, Op, RISK_WRITE, RISK_DESTRUCTIVE, _inv, _need, _project,
                        PROJECT_ARGS)

SHEET_ARGS = PROJECT_ARGS + ("sheet_id",)
SAMPLE_ROWS = 10
# The most rows one plan carries. The plan is stored until apply, and an insert past
# this is a bulk load the portal's file import does better.
MAX_PLAN_ROWS = 100_000
LOCAL_TABLE_TYPES = ("csv", "tsv", "txt", "parquet", "json")
# Local-file column types, from DuckDB's names to the sheet's.
_DUCKDB_TYPES = {"BIGINT": "BIGINT", "HUGEINT": "BIGINT", "UBIGINT": "BIGINT",
                 "UINTEGER": "BIGINT", "INTEGER": "INTEGER", "SMALLINT": "INTEGER",
                 "TINYINT": "INTEGER", "USMALLINT": "INTEGER", "UTINYINT": "INTEGER",
                 "DOUBLE": "DOUBLE", "FLOAT": "DOUBLE", "REAL": "DOUBLE",
                 "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP",
                 "TIMESTAMP WITH TIME ZONE": "TIMESTAMP", "TIMESTAMP_NS": "TIMESTAMP",
                 "TIMESTAMP_MS": "TIMESTAMP", "TIMESTAMP_S": "TIMESTAMP"}


def _invalid(exc: S.SheetError, op: str) -> errors.InvalidQuery:
    return errors.InvalidQuery(exc.message, what="Fix that argument and plan again.",
                               field=exc.field, retry_with=[{"op": op}])


def _refusals(what: str, field: str, op: str) -> dict:
    """What a sheet route's refusals mean to the caller: the backend's 400s carry the
    rule that was broken, and a 409 is a schema version that moved."""
    return {400: lambda message: errors.InvalidQuery(
                f"{what}: {message}", what="Fix the request and plan again.", field=field,
                retry_with=[{"op": op}]),
            403: lambda message: errors.Forbidden(
                f"{what}: {message}", what="Your role in this project does not allow it.",
                user_action="Ask a project admin for access, or have them make the change."),
            409: lambda message: errors.PlanStale(
                f"{what}: {message}", what="The sheet's columns changed after this plan "
                "was made.", retry_with=[{"tool": "plan", "op": op}],
                user_action="Re-plan so the change is made on the current sheet.")}


def _problems(problems: list, op: str) -> errors.InvalidQuery:
    return errors.InvalidQuery(
        f"{len(problems)} value(s) would not go into the sheet: "
        + " | ".join(problems[:12]) + (" | ..." if len(problems) > 12 else ""),
        what="Fix each value and plan again. Nothing was written.", field="rows",
        issues=problems[:100], retry_with=[{"op": op}])


def _sheet(reader: reads.Reader, project_id: str, given: str) -> dict:
    """Mutations take IDs only. A sheet's name and table name can both change."""
    try:
        return reads.sheet_detail(reader, project_id, given)
    except errors.NotFound:
        named = [s for s in reads.sheets(reader, project_id)
                 if given in (s.get("name"), s.get("tableName"))]
        if named:
            raise errors.NameRequiredId(
                f"{given!r} is a sheet name, and mutations require an ID.",
                what="Re-send with the sheet ID.", field="sheet_id", given=given,
                candidates=[{"id": s["id"], "name": s["name"], "table": s.get("tableName")}
                            for s in named],
                retry_with=[{"sheet_id": s["id"]} for s in named]) from None
        raise


def _writable(detail: dict, op: str, *, table_only: bool = True,
              statuses: tuple = ("COMPLETED", None)) -> None:
    if detail.get("sheetCreationMode") == "SYSTEM" \
            or detail.get("namespaceName") in S.RESERVED_NAMESPACES:
        raise errors.NotSupported(
            f"Sheet {detail['name']!r} is maintained by Cirro.",
            what="System sheets are written by Cirro itself, not by anyone's edits.",
            user_action="Pick a sheet created in the project.")
    if table_only and detail.get("sheetType") != "TABLE":
        raise errors.NotSupported(
            f"Sheet {detail['name']!r} is a view; {op} writes a table's rows.",
            what="A view's rows come from its SQL. Change the table it reads, or change "
                 "the view with update_sheet.",
            user_action="Pick a table sheet.")
    if detail.get("status") not in statuses:
        raise errors.WrongStatus(
            f"Sheet {detail['name']!r} is {detail.get('status')}.",
            what="Only a sheet that finished its last job can be written.",
            retry_with=[{"verb": "sheet", "id": detail["id"]}])


def _label(detail: dict) -> str:
    return (f"{detail['name']!r} ({detail['id']}, table "
            f"{detail.get('namespaceName')}.{detail.get('tableName')})")


def _url(reader, project_id, sheet_id) -> str:
    return S.sheet_url(reader.base_url, project_id, sheet_id)


# ------------------------------------------------------------------ what reads it

def _readers(reader: reads.Reader, project_id: str, detail: dict) -> list:
    """The dashboards and views that read this sheet, as plan lines. Views are found by
    their definition naming the table or the sheet; dashboards by a source on it."""
    table = detail.get("tableName") or ""
    mentions = re.compile(rf"\b{re.escape(table)}\b", re.I) if table else None
    found = []
    for row in reads.dashboards(reader, project_id):
        record = reads.dashboard_record(reader, project_id, row["id"])
        document = record.get("dashboardData")
        nodes = document["ir"]["nodes"] if D.is_session_blob(document) else []
        if any((n.get("config") or {}).get("sheetId") == detail["id"] for n in nodes) \
                or (record.get("criteria") or {}).get("sheetId") == detail["id"]:
            found.append(f"dashboard {record['name']!r} "
                         f"({D.studio_url(reader.base_url, project_id, record['id'])})")
    for sheet in reads.sheets(reader, project_id):
        if sheet.get("sheetType") != "VIEW" or sheet["id"] == detail["id"]:
            continue
        view = reads.sheet_detail(reader, project_id, sheet["id"])
        definition = str(view.get("viewDefinition") or "")
        if detail["id"] in definition or (mentions and mentions.search(definition)):
            found.append(f"view sheet {view['name']!r} ({view['id']})")
    return found


def _reader_lines(readers: list, consequence: str) -> list:
    if not readers:
        return ["", "  Read by  no dashboard or view in this project"]
    return ["", f"  Read by  {len(readers)} -- {consequence}"] + [f"    {r}" for r in readers]


# ------------------------------------------------------------------ rows

def _selection(reader, project_id: str, detail: dict, args: dict, op: str) -> tuple:
    """(rows, how) for the rows `where` or `row_ids` selects, every column included."""
    where, row_ids = args.get("where"), args.get("row_ids")
    if (where is None) == (row_ids is None):
        raise errors.InvalidQuery(
            f"{op} selects rows with `where` or `row_ids`, exactly one of them.",
            what="`read sheet` shows each row's `_row_id` and the columns to filter on.",
            field="where", retry_with=[{"verb": "sheet", "id": detail["id"]}])
    if row_ids is not None:
        if (not isinstance(row_ids, list) or not row_ids
                or not all(isinstance(i, int) and not isinstance(i, bool) for i in row_ids)):
            raise errors.InvalidQuery("`row_ids` must be a non-empty list of integers.",
                                      what="Use the `_row_id` values `read sheet` shows.",
                                      field="row_ids",
                                      retry_with=[{"verb": "sheet", "id": detail["id"]}])
        if len(row_ids) > S.MAX_SELECTED:
            raise _too_many(len(row_ids), op)
        rows = _rows_by_id(reader, project_id, detail["id"], row_ids)
        missing = sorted(set(row_ids) - {r[S.ROW_ID] for r in rows})
        if missing:
            raise errors.NotFound(f"Sheet {detail['name']!r} has no row(s) {missing[:20]}.",
                                  what="Re-read the sheet for current row ids.",
                                  field="row_ids",
                                  retry_with=[{"verb": "sheet", "id": detail["id"]}])
        return rows, f"{len(rows):,} row(s) by _row_id"
    try:
        row_filter = S.row_filter(where, S.user_columns(detail.get("columns")),
                                  project_id)
    except S.SheetError as exc:
        raise _invalid(exc, op) from None
    page = reads.sheet_page(reader, project_id, detail["id"], limit=S.MAX_SELECTED,
                            row_filter=row_filter)
    total = page.get("totalRowCount") or 0
    if total > S.MAX_SELECTED:
        raise _too_many(total, op)
    rows = S.page_rows(page)
    if not rows:
        raise errors.InvalidQuery(f"`where` matches no rows in {detail['name']!r}.",
                                  what="Check the filter against `read sheet`.",
                                  field="where", given=where,
                                  retry_with=[{"verb": "sheet", "id": detail["id"],
                                               "where": where}])
    return rows, f"{len(rows):,} row(s) where {_where_text(where)}"


def _too_many(count: int, op: str) -> errors.InvalidQuery:
    return errors.InvalidQuery(
        f"That selects {count:,} rows; one {op} changes at most {S.MAX_SELECTED:,}.",
        what="Narrow `where`, or split the change into several plans.", field="where",
        retry_with=[{"op": op}])


def _where_text(where) -> str:
    import json
    return json.dumps(where, default=str)


def _rows_by_id(reader, project_id: str, sheet_id: str, row_ids: list) -> list:
    page = reads.sheet_page(reader, project_id, sheet_id, limit=S.MAX_SELECTED,
                            row_filter=S.row_id_filter(row_ids))
    return S.page_rows(page)


def _refuse_if_rows_moved(reader, args: dict) -> None:
    rows = _rows_by_id(reader, args["project_id"], args["sheet_id"], args["row_ids"])
    if len(rows) != len(args["row_ids"]) or S.rows_digest(rows) != args["rows_digest"]:
        raise errors.PlanStale(
            "Rows this plan selected were changed or removed after it was made.",
            what="Writing now would act on values the person did not see.",
            retry_with=[{"tool": "plan", "op": args["op"]}],
            user_action="Re-plan so the change is shown against the current rows.")


def _refuse_if_sheet_moved(reader, args: dict) -> dict:
    detail = reads.sheet_detail(reader, args["project_id"], args["sheet_id"])
    if S.sheet_digest(detail) != args["sheet_digest"]:
        raise errors.PlanStale(
            f"Sheet {detail['name']!r} changed after this plan was made.",
            what="Its name, columns or definition are no longer the ones planned against.",
            retry_with=[{"tool": "plan", "op": args["op"]}],
            user_action="Re-plan against the current sheet.")
    return detail


def _unique_clashes(reader, project_id: str, detail: dict, rows: list,
                    exclude: frozenset = frozenset()) -> list:
    """Problems for unique values the sheet already holds on another row."""
    problems = []
    for column in S.user_columns(detail.get("columns")):
        if not column.get("unique"):
            continue
        values = sorted({r[column["name"]] for r in rows if r.get(column["name"]) is not None},
                        key=str)
        for start in range(0, len(values), S.INSERT_BATCH):
            chunk = values[start:start + S.INSERT_BATCH]
            page = reads.sheet_page(reader, project_id, detail["id"], limit=20,
                                    row_filter={"column": column["name"], "operator": "IN",
                                                "values": chunk})
            for row in S.page_rows(page):
                if row[S.ROW_ID] not in exclude:
                    problems.append(f"{column['name']} {row[column['name']]!r} is already "
                                    f"on row {row[S.ROW_ID]}, and the column is unique")
    return problems


def _local_rows(path_text: str) -> tuple:
    """(column specs, rows) of a local table file, typed by DuckDB over every row.

    Each header's SQL name is derived the way the portal derives one, the header kept
    as its label when they differ; the rows are keyed by the SQL name."""
    import duckdb

    path = Path(str(path_text)).expanduser()
    kind = path.suffix.lower().lstrip(".")
    if not path.is_file() or kind not in LOCAL_TABLE_TYPES:
        raise errors.InvalidQuery(
            f"{path_text!r} is not a readable {'/'.join(LOCAL_TABLE_TYPES)} file.",
            what="Give the path of a local table file.", field="file",
            retry_with=[{"file": "/path/to/table.csv"}])
    reader_sql = {"parquet": "read_parquet(?)", "json": "read_json_auto(?)"}.get(
        kind, "read_csv(?, sample_size = -1)")
    con = duckdb.connect()
    try:
        relation = con.execute(f"SELECT * FROM {reader_sql}", [str(path)])
        described = [(d[0], str(d[1]).upper()) for d in relation.description]
        values = relation.fetchall()
    except duckdb.Error as exc:
        raise errors.InvalidQuery(f"{path} could not be read: {exc}",
                                  what="Check the file is a well-formed table.",
                                  field="file", retry_with=[{"file": str(path)}]) from None
    finally:
        con.close()
    columns = []
    for header, duck_type in described:
        column = {"name": S.sql_name(header) or header,
                  "type": _DUCKDB_TYPES.get(
                      duck_type, "DOUBLE" if duck_type.startswith("DECIMAL") else "STRING")}
        if column["name"] != header:
            column["label"] = header
        columns.append(column)
    names = [c["name"] for c in columns]
    return columns, [dict(zip(names, row)) for row in values]


def _rows_argument(args: dict, op: str) -> tuple:
    """(rows, inferred column specs, where the rows came from)."""
    if args.get("rows") is not None and args.get("file"):
        raise errors.InvalidQuery(f"{op} takes `rows` or `file`, not both.",
                                  what="Pick one source of rows.", field="file",
                                  retry_with=[{"op": op}])
    if args.get("file"):
        columns, rows = _local_rows(args["file"])
        source = f"from {Path(str(args['file'])).expanduser()}"
    else:
        columns, rows, source = [], args.get("rows") or [], "given inline"
    if len(rows) > MAX_PLAN_ROWS:
        raise errors.InvalidQuery(
            f"{len(rows):,} rows is more than one plan carries ({MAX_PLAN_ROWS:,}).",
            what="Split the rows across several plans, or import the file in the portal.",
            field="rows", retry_with=[{"op": op}])
    return rows, columns, source


def _insert(reader, project_id: str, sheet_id: str, rows: list, op: str) -> int:
    """Send rows in the endpoint's batches; each batch is atomic, the whole is not, so a
    failure says how far it got."""
    from cirro_api_client.v1.api.sheets import insert_sheet_data
    from cirro_api_client.v1.models import InsertRowsRequest
    written = 0
    for start in range(0, len(rows), S.INSERT_BATCH):
        batch = rows[start:start + S.INSERT_BATCH]
        try:
            response = reads.send(
                reader, insert_sheet_data, what="Inserting rows failed.",
                cost="sheets.insert_rows", project_id=project_id, sheet_id=sheet_id,
                body=InsertRowsRequest.from_dict({"inserts": [{"values": r} for r in batch]}),
                refusals=_refusals(f"The sheet refused rows {start}..{start + len(batch) - 1}",
                                   "rows", op))
        except errors.AgentError as exc:
            if written:
                exc.message += (f" ({written:,} of {len(rows):,} rows were already written; "
                                f"re-plan with the rest.)")
                exc.detail["rows_written"] = written
            raise
        written += (response or {}).get("rowsAffected", len(batch))
    return written


def _batches_note(count: int, size: int) -> list:
    if count <= size:
        return []
    return [f"  Sent as {-(-count // size)} requests of up to {size:,} rows. Each request "
            "is all-or-nothing; if one fails, the ones before it stay written."]


# ------------------------------------------------------------------ create

CREATE_ARGS = PROJECT_ARGS + ("name", "description", "table_name", "namespace", "columns",
                              "rows", "file", "view_sql", "materialize")


def _preview_view(reader, project_id: str, sql: str, op: str) -> dict:
    from cirro_api_client.v1.api.sheets import query_namespace_data
    from cirro_api_client.v1.models import SheetQueryRequest
    return reads.send(reader, query_namespace_data, what="Running the view's SQL failed.",
                      cost="sheets.raw_query", project_id=project_id,
                      body=SheetQueryRequest(query=sql, limit=5, page=1),
                      refusals=_refusals("The view's SQL failed", "view_sql", op)) or {}


def plan_create_sheet(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "create_sheet", "name")
    project = _project(reader, args)
    name = str(args["name"]).strip()
    namespace = args.get("namespace") or S.DEFAULT_NAMESPACE
    table = args.get("table_name") or S.sql_name(name)
    problem = (f"`name` is longer than {S.MAX_NAME_LENGTH} characters"
               if len(name) > S.MAX_NAME_LENGTH else
               S.identifier_problem(namespace, "namespace")
               or S.identifier_problem(table, "table_name")
               or (f"namespace {namespace!r} is reserved" if namespace in S.RESERVED_NAMESPACES
                   else None))
    if problem:
        raise errors.InvalidQuery(problem, what="Fix the name and plan again.",
                                  field="table_name", retry_with=[{"op": "create_sheet"}])
    existing = [s for s in reads.sheets(reader, project["id"]) if s.get("status") != "DELETED"]
    clash = next((s for s in existing if s["name"].lower() == name.lower()
                  or (s.get("namespaceName") == namespace
                      and str(s.get("tableName")).lower() == table.lower())), None)
    if clash:
        raise errors.InvalidQuery(
            f"Sheet {clash['name']!r} ({clash['id']}) already has that name or table "
            f"{namespace}.{table}.",
            what="Sheet names and tables are unique in a project, ignoring case. Pick "
                 "another `name` or `table_name`, or write to the existing sheet.",
            field="name",
            retry_with=[{"op": "insert_rows", "args": {"sheet_id": clash["id"]}}])
    base = {"name": name, "description": args.get("description") or "",
            "namespaceName": namespace, "tableName": table}
    header = [f"Create sheet {name!r} in {project['name']} ({project['id']}).", "",
              f"  Table        {namespace}.{table}"]
    if base["description"]:
        header.append(f"  Description  {base['description']!r}")
    if args.get("view_sql"):
        return _plan_create_view(reader, project, args, base, header)
    rows, inferred, source = _rows_argument(args, "create_sheet")
    try:
        columns = S.check_columns(args.get("columns") or inferred)
    except S.SheetError as exc:
        raise _invalid(exc, "create_sheet") from None
    coerced, problems = [], []
    if rows:
        coerced, problems = S.check_rows(columns, rows, inserting=True,
                                         project_id=project["id"])
        problems += S.duplicates(columns, coerced)
    if problems:
        raise _problems(problems, "create_sheet")
    lines = header + ["", f"  Columns ({len(columns)})"]
    lines += [f"    {S.describe_column(c)}" for c in columns]
    if coerced:
        lines += ["", f"  Rows  {len(coerced):,} {source}, as they will be stored"]
        lines += S.render_rows(coerced, [c["name"] for c in columns], SAMPLE_ROWS)
        lines += _batches_note(len(coerced), S.INSERT_BATCH)
    else:
        lines += ["", "  Rows  none; the sheet starts empty"]
    body = {**base, "sheetType": "TABLE", "sheetCreationMode": "SCRATCH", "columns": columns}
    return Draft(args={"project_id": project["id"], "body": body, "rows": coerced},
                 summary="\n".join(lines), invalidates=[_inv("sheets", project["id"])])


def _plan_create_view(reader, project: dict, args: dict, base: dict, header: list) -> Draft:
    if args.get("columns") or args.get("rows") or args.get("file"):
        raise errors.InvalidQuery(
            "A view's columns and rows come from its SQL.",
            what="Give `view_sql` alone, or drop it to create a table.", field="view_sql",
            retry_with=[{"op": "create_sheet"}])
    sql = str(args["view_sql"]).strip()
    preview = _preview_view(reader, project["id"], sql, "create_sheet")
    virtual = not args.get("materialize")
    columns = [c.get("name") for c in preview.get("columns") or []]
    kind = "virtual view, run when read" if virtual else \
        "materialized view, refreshed on request"
    lines = header + [
        f"  Kind         {kind}",
        "", "  SQL", *[f"    {line}" for line in sql.splitlines()],
        "", f"  Columns  {', '.join(columns) or '(none)'}",
        f"  Rows     {preview.get('totalRowCount', 0):,} now; the first few:"]
    lines += S.render_rows(S.page_rows(preview), columns, 5)
    body = {**base, "sheetType": "VIEW", "virtual": virtual,
            "viewDefinition": {"viewType": "RAW", "query": sql}}
    return Draft(args={"project_id": project["id"], "body": body, "rows": []},
                 summary="\n".join(lines), invalidates=[_inv("sheets", project["id"])])


def _sheet_input(body: dict):
    from cirro_api_client.v1.models import TableSheetInput, ViewSheetInput
    return (ViewSheetInput if body["sheetType"] == "VIEW" else TableSheetInput).from_dict(body)


def apply_create_sheet(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.api.sheets import create_sheet
    created = reads.send(reader, create_sheet, what="Creating the sheet failed.",
                         cost="sheets.create", project_id=args["project_id"],
                         body=_sheet_input(args["body"]),
                         refusals=_refusals("The sheet was not created", "body",
                                            "create_sheet"))
    sheet_id = (created or {}).get("id")
    result = {"created_sheet_id": sheet_id,
              "url": _url(reader, args["project_id"], sheet_id) if sheet_id else None}
    if args["rows"]:
        try:
            result["rows_inserted"] = _insert(reader, args["project_id"], sheet_id,
                                              args["rows"], "insert_rows")
        except errors.AgentError as exc:
            exc.message = (f"Sheet {sheet_id} was created, but its rows were not all "
                           f"written: {exc.message}")
            exc.detail["created_sheet_id"] = sheet_id
            raise
    return result


# ------------------------------------------------------------------ update

UPDATE_ARGS = SHEET_ARGS + ("name", "description", "table_name", "add_columns",
                            "drop_columns", "alter_columns", "view_sql")


def plan_update_sheet(reader: reads.Reader, args: dict) -> Draft:
    from cirro_api_client.v1.api.sheets import update_sheet
    _need(args, "update_sheet", "sheet_id")
    project = _project(reader, args)
    detail = _sheet(reader, project["id"], args["sheet_id"])
    is_view = detail.get("sheetType") == "VIEW"
    _writable(detail, "update_sheet", table_only=False)
    column_args = [k for k in ("add_columns", "drop_columns", "alter_columns") if args.get(k)]
    if is_view and (column_args or args.get("table_name")):
        raise errors.InvalidQuery(
            "A view's columns come from its SQL, and its table name is fixed.",
            what="Change `view_sql` instead.", field=(column_args or ["table_name"])[0],
            retry_with=[{"op": "update_sheet", "args": {"sheet_id": detail["id"],
                                                        "view_sql": "SELECT ..."}}])
    if not is_view and args.get("view_sql"):
        raise errors.InvalidQuery("`view_sql` changes a view; this sheet is a table.",
                                  what="Use add_columns, drop_columns or alter_columns.",
                                  field="view_sql", retry_with=[{"op": "update_sheet"}])
    body = {"name": args.get("name") or detail["name"],
            "description": (args["description"] if args.get("description") is not None
                            else detail.get("description") or ""),
            "namespaceName": detail["namespaceName"],
            "tableName": args.get("table_name") or detail["tableName"],
            "sheetType": detail["sheetType"], "tags": detail.get("tags") or [],
            "auditReadAccess": bool(detail.get("auditReadAccess"))}
    renamed = [f"  {label:<12} {detail.get(key) or ''!r} -> {body[key]!r}"
               for key, label in (("name", "Name"), ("description", "Description"),
                                  ("tableName", "Table"))
               if body[key] != (detail.get(key) or "")]
    lines = [f"Update sheet {_label(detail)} in {project['name']}."]
    if renamed:
        lines += [""] + renamed
    if body["tableName"] != detail["tableName"]:
        problem = S.identifier_problem(body["tableName"], "table_name")
        if problem:
            raise errors.InvalidQuery(problem, what="Fix the name and plan again.",
                                      field="table_name", retry_with=[{"op": "update_sheet"}])
    structural = bool(column_args) or body["tableName"] != detail["tableName"]
    if is_view:
        body["virtual"] = bool(detail.get("virtual"))
        body["viewDefinition"] = detail.get("viewDefinition")
        if args.get("view_sql"):
            sql = str(args["view_sql"]).strip()
            preview = _preview_view(reader, project["id"], sql, "update_sheet")
            body["viewDefinition"] = {"viewType": "RAW", "query": sql}
            structural = True
            columns = [c.get("name") for c in preview.get("columns") or []]
            lines += ["", "  New SQL", *[f"    {line}" for line in sql.splitlines()],
                      "", f"  Columns  {', '.join(columns)}"]
            if not body["virtual"]:
                lines.append("  The view re-materializes after the change.")
    else:
        try:
            columns = (S.alter_columns(detail.get("columns"), args.get("drop_columns"),
                                       args.get("alter_columns"), args.get("add_columns"))
                       if column_args else S.user_columns(detail.get("columns")))
        except S.SheetError as exc:
            raise _invalid(exc, "update_sheet") from None
        body.update(sheetCreationMode=detail.get("sheetCreationMode"),
                    schemaVersionId=detail.get("schemaVersionId"), columns=columns)
    if not renamed and not structural:
        raise errors.InvalidQuery("Nothing to change.",
                                  what="Give a new name, description, table name, column "
                                       "change or view SQL.", field="name",
                                  retry_with=[{"verb": "sheet", "id": detail["id"]}])
    if not is_view and structural:
        dry = reads.send(reader, update_sheet, what="Previewing the change failed.",
                         cost="sheets.update_dry_run", project_id=project["id"],
                         sheet_id=detail["id"], body=_sheet_input(body), dry_run=True,
                         refusals=_refusals("The sheet refused the change", "alter_columns",
                                            "update_sheet")) or {}
        lines += ["", "  Changes, as the sheet's own dry run reports them"]
        lines += [f"    {c}" for c in dry.get("changes") or ["(none to the columns)"]]
        if dry.get("sqlStatements"):
            lines += ["", "  SQL it will run"] + [f"    {s}" for s in dry["sqlStatements"]]
    if structural:
        lines += _reader_lines(_readers(reader, project["id"], detail),
                               "SQL, charts and filters that name a changed table or "
                               "column stop working")
    return Draft(args={"op": "update_sheet", "project_id": project["id"],
                       "sheet_id": detail["id"], "body": body,
                       "sheet_digest": S.sheet_digest(detail)},
                 summary="\n".join(lines), invalidates=[_inv("sheets", project["id"])])


def apply_update_sheet(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.api.sheets import update_sheet
    _refuse_if_sheet_moved(reader, args)
    updated = reads.send(reader, update_sheet, what="Updating the sheet failed.",
                         cost="sheets.update", project_id=args["project_id"],
                         sheet_id=args["sheet_id"], body=_sheet_input(args["body"]),
                         dry_run=False,
                         refusals=_refusals("The sheet refused the change", "body",
                                            "update_sheet")) or {}
    return {"sheet_id": args["sheet_id"], "changes": updated.get("changes") or [],
            "url": _url(reader, args["project_id"], args["sheet_id"])}


# ------------------------------------------------------------------ delete

def plan_delete_sheet(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "delete_sheet", "sheet_id")
    project = _project(reader, args)
    detail = _sheet(reader, project["id"], args["sheet_id"])
    # A sheet whose last job failed can still be deleted; the backend allows it.
    _writable(detail, "delete_sheet", table_only=False, statuses=("COMPLETED", "FAILED"))
    rows = detail.get("totalRowCount")
    columns = S.user_columns(detail.get("columns"))
    lines = [f"DELETE sheet {_label(detail)} from {project['name']}.", "",
             f"  Kind        {detail.get('sheetType')}",
             f"  Rows        {'computed when read' if rows is None else f'{rows:,}'}",
             f"  Columns     {', '.join(c['name'] for c in columns) or '(none)'}",
             f"  Created by  {detail.get('createdBy')}  Updated {detail.get('updatedAt')}"]
    lines += _reader_lines(_readers(reader, project["id"], detail),
                           "each stops working once the table is gone")
    lines += ["", "The table and every row in it are dropped for everyone. It cannot be "
                  "undone."]
    return Draft(args={"op": "delete_sheet", "project_id": project["id"],
                       "sheet_id": detail["id"], "sheet_name": detail["name"],
                       "sheet_digest": S.sheet_digest(detail), "row_count": rows},
                 summary="\n".join(lines), invalidates=[_inv("sheets", project["id"])])


def apply_delete_sheet(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.api.sheets import delete_sheet
    detail = _refuse_if_sheet_moved(reader, args)
    if detail.get("totalRowCount") != args["row_count"]:
        raise errors.PlanStale(
            f"Sheet {detail['name']!r} has {detail.get('totalRowCount')} rows now, not the "
            f"{args['row_count']} the plan showed.",
            what="The person approved deleting what the plan showed.",
            retry_with=[{"tool": "plan", "op": "delete_sheet"}],
            user_action="Re-plan so the current contents are shown.")
    reads.send(reader, delete_sheet, what="Deleting the sheet failed.", cost="sheets.delete",
               project_id=args["project_id"], sheet_id=args["sheet_id"],
               refusals=_refusals("The sheet was not deleted", "sheet_id", "delete_sheet"))
    return {"deleted_sheet": args["sheet_id"], "name": args["sheet_name"]}


# ------------------------------------------------------------------ insert rows

INSERT_ARGS = SHEET_ARGS + ("rows", "file")


def plan_insert_rows(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "insert_rows", "sheet_id")
    project = _project(reader, args)
    detail = _sheet(reader, project["id"], args["sheet_id"])
    _writable(detail, "insert_rows")
    rows, _, source = _rows_argument(args, "insert_rows")
    try:
        coerced, problems = S.check_rows(detail.get("columns"), rows, inserting=True,
                                         project_id=project["id"])
    except S.SheetError as exc:
        raise _invalid(exc, "insert_rows") from None
    problems += S.duplicates(detail.get("columns"), coerced)
    if not problems:
        problems += _unique_clashes(reader, project["id"], detail, coerced)
    if problems:
        raise _problems(problems, "insert_rows")
    names = [c["name"] for c in S.user_columns(detail.get("columns"))]
    total = detail.get("totalRowCount") or 0
    lines = [f"Insert {len(coerced):,} row(s) into sheet {_label(detail)} "
             f"in {project['name']}.", "",
             f"  Rows  {len(coerced):,} {source}; the sheet has {total:,} now, "
             f"{total + len(coerced):,} after", ""]
    lines += S.render_rows(coerced, names, SAMPLE_ROWS)
    lines += _batches_note(len(coerced), S.INSERT_BATCH)
    return Draft(args={"op": "insert_rows", "project_id": project["id"],
                       "sheet_id": detail["id"], "rows": coerced,
                       "sheet_digest": S.sheet_digest(detail)},
                 summary="\n".join(lines), invalidates=[_inv("sheets", project["id"])])


def apply_insert_rows(reader: reads.Reader, args: dict) -> dict:
    _refuse_if_sheet_moved(reader, args)
    written = _insert(reader, args["project_id"], args["sheet_id"], args["rows"],
                      "insert_rows")
    return {"sheet_id": args["sheet_id"], "rows_inserted": written,
            "url": _url(reader, args["project_id"], args["sheet_id"])}


# ------------------------------------------------------------------ update rows

UPDATE_ROWS_ARGS = SHEET_ARGS + ("where", "row_ids", "set", "updates")


def _planned_updates(detail: dict, args: dict, project_id: str, reader) -> tuple:
    """(updates [{rowId, values}], the rows they touch as read now, how they were
    selected, problems with the new values)."""
    columns = detail.get("columns")
    if args.get("updates") is not None:
        if args.get("set") is not None or args.get("where") is not None \
                or args.get("row_ids") is not None:
            raise errors.InvalidQuery(
                "`updates` gives each row its own values; it replaces `set` with "
                "`where` or `row_ids`.", what="Use one form.", field="updates",
                retry_with=[{"op": "update_rows"}])
        updates = args["updates"]
        if (not isinstance(updates, list) or not updates or not all(
                isinstance(u, dict) and isinstance(u.get("row_id"), int)
                and isinstance(u.get("values"), dict) for u in updates)):
            raise errors.InvalidQuery("`updates` is a list of {\"row_id\": int, \"values\": "
                                      "{column: value}}.", what="Fix its shape.",
                                      field="updates", retry_with=[{"op": "update_rows"}])
        ids = [u["row_id"] for u in updates]
        if len(set(ids)) != len(ids):
            raise errors.InvalidQuery("`updates` names a row twice.", what="Merge them.",
                                      field="updates", retry_with=[{"op": "update_rows"}])
        rows, how = _selection(reader, project_id, detail, {"row_ids": ids}, "update_rows")
        values, problems = S.check_rows(columns, [u["values"] for u in updates],
                                        inserting=False, project_id=project_id,
                                        where="updates")
        return [{"rowId": i, "values": v} for i, v in zip(ids, values)], rows, how, problems
    if not isinstance(args.get("set"), dict) or not args["set"]:
        raise errors.InvalidQuery("update_rows needs `set` ({column: value}) with `where` "
                                  "or `row_ids`, or `updates`.", what="Say what to change.",
                                  field="set", retry_with=[{"op": "update_rows"}])
    rows, how = _selection(reader, project_id, detail, args, "update_rows")
    (values,), problems = S.check_rows(columns, [args["set"]], inserting=False,
                                       project_id=project_id, where="set")
    unique = [c["name"] for c in S.user_columns(columns)
              if c.get("unique") and values.get(c["name"]) is not None]
    if unique and len(rows) > 1:
        problems.append(f"set gives {len(rows)} rows the same {unique}, which must be unique")
    return [{"rowId": r[S.ROW_ID], "values": values} for r in rows], rows, how, problems


def plan_update_rows(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "update_rows", "sheet_id")
    project = _project(reader, args)
    detail = _sheet(reader, project["id"], args["sheet_id"])
    _writable(detail, "update_rows")
    updates, rows, how, problems = _planned_updates(detail, args, project["id"], reader)
    if problems:
        raise _problems(problems, "update_rows")
    current = {r[S.ROW_ID]: r for r in rows}
    by_name = {c["name"]: c for c in S.user_columns(detail.get("columns"))}

    def differs(row_id, name, value):
        return not S.stored_equal(by_name[name], current[row_id].get(name), value)

    changing = [u for u in updates
                if any(differs(u["rowId"], k, v) for k, v in u["values"].items())]
    if not changing:
        raise errors.InvalidQuery(f"The {how} already hold those values.",
                                  what="Nothing would change.", field="set",
                                  retry_with=[{"verb": "sheet", "id": detail["id"]}])
    touched = {u["rowId"] for u in changing}
    unique_rows = [u["values"] for u in changing]
    problems = _unique_clashes(reader, project["id"], detail, unique_rows,
                               exclude=frozenset(touched))
    if problems:
        raise _problems(problems, "update_rows")
    lines = [f"Update {len(changing):,} row(s) of sheet {_label(detail)} "
             f"in {project['name']}.", "", f"  Selected  {how}"]
    if len(changing) < len(updates):
        lines.append(f"  {len(updates) - len(changing):,} of them already hold the new "
                     "values and are left alone")
    lines += ["", "  Before -> after"]
    for update in changing[:SAMPLE_ROWS]:
        before = current[update["rowId"]]
        cells = [f"{k} {S.shown(before.get(k))} -> {S.shown(v)}"
                 for k, v in update["values"].items() if differs(update["rowId"], k, v)]
        lines.append(f"    row {update['rowId']}: " + "; ".join(cells))
    if len(changing) > SAMPLE_ROWS:
        lines.append(f"    ... and {len(changing) - SAMPLE_ROWS:,} more")
    lines += _batches_note(len(changing), S.UPDATE_BATCH)
    lines += ["", "  If any of these rows is edited before this is applied, it is refused."]
    selected = [current[i] for i in sorted(touched)]
    return Draft(args={"op": "update_rows", "project_id": project["id"],
                       "sheet_id": detail["id"], "updates": changing,
                       "row_ids": sorted(touched), "rows_digest": S.rows_digest(selected),
                       "sheet_digest": S.sheet_digest(detail)},
                 summary="\n".join(lines), invalidates=[_inv("sheets", project["id"])])


def apply_update_rows(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.api.sheets import update_sheet_data
    from cirro_api_client.v1.models import UpdateRowsRequest
    _refuse_if_sheet_moved(reader, args)
    _refuse_if_rows_moved(reader, args)
    written = 0
    updates = args["updates"]
    for start in range(0, len(updates), S.UPDATE_BATCH):
        batch = updates[start:start + S.UPDATE_BATCH]
        try:
            response = reads.send(
                reader, update_sheet_data, what="Updating rows failed.",
                cost="sheets.update_rows", project_id=args["project_id"],
                sheet_id=args["sheet_id"],
                body=UpdateRowsRequest.from_dict({"updates": batch}),
                refusals=_refusals("The sheet refused the update", "set", "update_rows"))
        except errors.AgentError as exc:
            if written:
                exc.message += f" ({written:,} of {len(updates):,} rows were already updated.)"
                exc.detail["rows_updated"] = written
            raise
        written += (response or {}).get("rowsAffected", len(batch))
    return {"sheet_id": args["sheet_id"], "rows_updated": written,
            "url": _url(reader, args["project_id"], args["sheet_id"])}


# ------------------------------------------------------------------ delete rows

DELETE_ROWS_ARGS = SHEET_ARGS + ("where", "row_ids")


def plan_delete_rows(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "delete_rows", "sheet_id")
    project = _project(reader, args)
    detail = _sheet(reader, project["id"], args["sheet_id"])
    _writable(detail, "delete_rows")
    rows, how = _selection(reader, project["id"], detail, args, "delete_rows")
    names = [S.ROW_ID] + [c["name"] for c in S.user_columns(detail.get("columns"))]
    total = detail.get("totalRowCount") or 0
    lines = [f"DELETE {len(rows):,} row(s) from sheet {_label(detail)} "
             f"in {project['name']}.", "", f"  Selected  {how}",
             f"  The sheet has {total:,} rows now, {max(total - len(rows), 0):,} after", ""]
    lines += S.render_rows(rows, names, SAMPLE_ROWS)
    lines += ["", "  The rows are removed for everyone. If any of them is edited before "
                  "this is applied, it is refused."]
    return Draft(args={"op": "delete_rows", "project_id": project["id"],
                       "sheet_id": detail["id"],
                       "row_ids": sorted(r[S.ROW_ID] for r in rows),
                       "rows_digest": S.rows_digest(rows),
                       "sheet_digest": S.sheet_digest(detail)},
                 summary="\n".join(lines), invalidates=[_inv("sheets", project["id"])])


def apply_delete_rows(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.api.sheets import delete_sheet_data
    from cirro_api_client.v1.models import DeleteRowsRequest
    _refuse_if_sheet_moved(reader, args)
    _refuse_if_rows_moved(reader, args)
    response = reads.send(reader, delete_sheet_data, what="Deleting rows failed.",
                          cost="sheets.delete_rows", project_id=args["project_id"],
                          sheet_id=args["sheet_id"],
                          body=DeleteRowsRequest(row_ids=args["row_ids"]),
                          refusals=_refusals("The sheet refused the delete", "row_ids",
                                             "delete_rows"))
    return {"sheet_id": args["sheet_id"],
            "rows_deleted": (response or {}).get("rowsAffected", len(args["row_ids"]))}


# ------------------------------------------------------------------ refresh a view

def plan_refresh_sheet(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "refresh_sheet", "sheet_id")
    project = _project(reader, args)
    detail = _sheet(reader, project["id"], args["sheet_id"])
    # The backend refuses to refresh a FAILED view too ("Sheet is not ready").
    _writable(detail, "refresh_sheet", table_only=False)
    if detail.get("sheetType") != "VIEW" or detail.get("virtual"):
        raise errors.NotSupported(
            f"Sheet {detail['name']!r} is not a materialized view.",
            what="Only a materialized view holds a copy to refresh; a table and a virtual "
                 "view are always current.",
            user_action="Nothing to refresh.")
    lines = [f"Refresh materialized view {_label(detail)} in {project['name']}.", "",
             f"  Last refreshed  {detail.get('lastRefreshedAt') or 'never'}",
             f"  Rows now        {detail.get('totalRowCount')}", "",
             "  Its SQL runs again over the current source sheets and replaces its rows. "
             "The refresh runs in the background."]
    return Draft(args={"project_id": project["id"], "sheet_id": detail["id"]},
                 summary="\n".join(lines), invalidates=[_inv("sheets", project["id"])])


def apply_refresh_sheet(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.api.sheets import refresh_view
    reads.send(reader, refresh_view, what="Refreshing the view failed.",
               cost="sheets.refresh", project_id=args["project_id"],
               sheet_id=args["sheet_id"],
               refusals=_refusals("The view was not refreshed", "sheet_id", "refresh_sheet"))
    return {"sheet_id": args["sheet_id"], "refresh": "started",
            "url": _url(reader, args["project_id"], args["sheet_id"])}


SHEET_OPS = [
    Op("create_sheet", RISK_WRITE, plan_create_sheet, apply_create_sheet,
       accepts=CREATE_ARGS, outputs=("created_sheet_id",)),
    Op("update_sheet", RISK_WRITE, plan_update_sheet, apply_update_sheet,
       accepts=UPDATE_ARGS),
    Op("delete_sheet", RISK_DESTRUCTIVE, plan_delete_sheet, apply_delete_sheet,
       accepts=SHEET_ARGS),
    Op("insert_rows", RISK_WRITE, plan_insert_rows, apply_insert_rows, accepts=INSERT_ARGS),
    Op("update_rows", RISK_WRITE, plan_update_rows, apply_update_rows,
       accepts=UPDATE_ROWS_ARGS),
    Op("delete_rows", RISK_DESTRUCTIVE, plan_delete_rows, apply_delete_rows,
       accepts=DELETE_ROWS_ARGS),
    Op("refresh_sheet", RISK_WRITE, plan_refresh_sheet, apply_refresh_sheet,
       accepts=SHEET_ARGS),
]

mutations.OPS.update({op.name: op for op in SHEET_OPS})

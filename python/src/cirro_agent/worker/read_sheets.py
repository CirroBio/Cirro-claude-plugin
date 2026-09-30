"""Reading one sheet: its columns as a write must satisfy them, and a page of its rows.

The rows carry `_row_id`, which is how `update_rows` and `delete_rows` name a row, and
`where` takes the same selection those operations do, so what a read shows is what a
write with the same filter would touch.
"""

from .. import errors, sheets as S
from ..envelope import DEFAULT_MAX_TOKENS, wrap
from .reads import Reader, sheet_detail, sheet_jobs, sheet_page, sheets

DEFAULT_LIMIT = 20
MAX_LIMIT = 500


def _resolve(reader: Reader, project_id: str, given: str) -> dict:
    listed = sheets(reader, project_id)
    match = next((s for s in listed if s.get("id") == given), None) or next(
        (s for s in listed if given in (s.get("name"), s.get("tableName"))), None)
    if match is None:
        raise errors.NotFound(f"No sheet {given!r} in this project.",
                              what="Pick a sheet.", field="id", given=given,
                              candidates=[{"id": s.get("id"), "name": s.get("name")}
                                          for s in listed],
                              retry_with=[{"verb": "list", "of": "sheets",
                                           "scope": {"project_ids": [project_id]}}])
    return sheet_detail(reader, project_id, match["id"])


def _column(column: dict) -> dict:
    out = {"name": column["name"], "type": column.get("dataType"),
           "kind": column.get("semanticType") or "STANDARD"}
    for key, source in (("label", "displayName"), ("description", "description"),
                        ("allowed_values", "allowedValues")):
        if column.get(source):
            out[key] = column[source]
    for flag in ("required", "unique", "hidden"):
        if column.get(flag):
            out[flag] = True
    return out


def do_sheet(request: dict) -> dict:
    reader = Reader(request)
    project_id = reader.scope_project_ids(request.get("scope") or {})[0]
    detail = _resolve(reader, project_id, request["id"])
    columns = S.user_columns(detail.get("columns"))
    limit = min(int(request.get("limit") or DEFAULT_LIMIT), MAX_LIMIT)
    page_number = int(request.get("page") or 1)
    row_filter = None
    if request.get("where"):
        try:
            row_filter = S.row_filter(request["where"], columns, project_id)
        except S.SheetError as exc:
            raise errors.InvalidQuery(exc.message, what="Fix the filter.", field=exc.field,
                                      given=request["where"],
                                      retry_with=[{"verb": "sheet", "id": detail["id"]}]) \
                from None
    notes = []
    if detail.get("status") == "COMPLETED":
        page = sheet_page(reader, project_id, detail["id"], limit=limit, page=page_number,
                          row_filter=row_filter, order_by=request.get("order_by"),
                          descending=bool(request.get("desc")))
    else:
        # The backend answers "Sheet is not ready" for its rows; what went wrong is on
        # the sheet's last job.
        page = {}
        last = next(iter(sheet_jobs(reader, project_id, detail["id"])), None)
        notes.append(f"The sheet is {detail.get('status')}, so it has no rows to show."
                     + (f" Its last job ({last.get('jobType')}, {last.get('status')}): "
                        f"{last.get('errorMessage') or 'no error recorded'}" if last else ""))
    rows = [{k: v for k, v in r.items() if not k.startswith(S.SYSTEM_PREFIX)}
            for r in S.page_rows(page)]
    matched = page.get("totalRowCount")
    result = {"id": detail["id"], "name": detail["name"],
              "description": detail.get("description"), "kind": detail.get("sheetType"),
              "table": f"{detail.get('namespaceName')}.{detail.get('tableName')}",
              "status": detail.get("status"), "row_count": detail.get("totalRowCount"),
              "created_by": detail.get("createdBy"), "updated_at": detail.get("updatedAt"),
              "url": S.sheet_url(reader.base_url, project_id, detail["id"]),
              "columns": [_column(c) for c in columns],
              "rows": rows, "page": page_number, "matched": matched}
    if detail.get("sheetType") == "VIEW":
        result["virtual"] = bool(detail.get("virtual"))
        result["view_definition"] = detail.get("viewDefinition")
        result["last_refreshed_at"] = detail.get("lastRefreshedAt")
    elif detail.get("sheetCreationMode") == "SYSTEM":
        result["system"] = True
    if notes:
        result["notes"] = notes
    args = {"project_id": project_id, "sheet_id": detail["id"]}
    result["next_calls"] = []
    if matched is not None and page_number * limit < matched:
        result["next_calls"].append({
            "id": "nc_next_page", "why": f"{matched - page_number * limit:,} more rows match",
            "call": {"verb": "sheet", "id": detail["id"], "page": page_number + 1,
                     "limit": limit, **({"where": request["where"]}
                                        if request.get("where") else {})}})
    if detail.get("sheetType") == "TABLE" and not result.get("system"):
        result["next_calls"] += [
            {"id": "nc_insert", "why": "add rows",
             "call": {"tool": "plan", "op": "insert_rows", "args": {**args, "rows": []}}},
            {"id": "nc_update", "why": "change cells in the rows a filter selects",
             "call": {"tool": "plan", "op": "update_rows",
                      "args": {**args, "where": {}, "set": {}}}},
            {"id": "nc_columns", "why": "add, drop, rename or retype columns",
             "call": {"tool": "plan", "op": "update_sheet",
                      "args": {**args, "add_columns": []}}}]
    return wrap(query={"verb": "sheet", "id": request["id"],
                       **({"where": request["where"]} if request.get("where") else {})},
                result=result, cost=reader.cost.to_dict(),
                max_tokens=int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["query"]))

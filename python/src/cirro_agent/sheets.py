"""Sheets: column definitions, cell values and row selections, checked before a write.

Pure and cirro-free, like dashboards.py: the worker reads the sheet and sends the
writes; everything here works on the API's plain dicts.

The checks are the backend's own rules, run first so a plan names the problem. Most of
them the backend enforces too, but not all, and not in a form an agent can act on: a
cell it cannot cast is bound straight into DuckDB, and the failure comes back as a 500
("Failed to insert rows") with the column and the value gone. So every value is
coerced here to what its column holds, and a value that will not go is refused with
its row and column.
"""

import datetime as dt
import hashlib
import json
import math
import re
from typing import Optional

# Cirro-backend lib-common Validation.SQL_IDENTIFIER: table, namespace and column names.
IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$")
# SystemEntities: namespaces the backend keeps for itself.
RESERVED_NAMESPACES = ("cirro_entity", "sheets")
# CirroSystemColumns: maintained by the backend on every write, never by a caller.
SYSTEM_PREFIX = "sys_"
ROW_ID = "_row_id"
# The namespace the portal creates sheets in (sheet.service.ts).
DEFAULT_NAMESPACE = "default"
MAX_NAME_LENGTH = 100
MAX_ALLOWED_VALUES = 1000

# Per-request limits of the row endpoints (InsertRowsRequest, UpdateRowsRequest,
# DeleteRowsRequest). A plan that writes more than one request's worth says so.
INSERT_BATCH = 1000
UPDATE_BATCH = 1000
MAX_DELETE = 10_000
# How many rows an update or delete may select. It is the delete limit, so a
# selection is always one delete request -- which is the one write that is atomic
# whatever its size.
MAX_SELECTED = MAX_DELETE

DATA_TYPES = ("STRING", "INTEGER", "BIGINT", "DOUBLE", "FLOAT", "BOOLEAN", "DATE",
              "TIMESTAMP")
# SemanticColumnType values a column may be created with. ENUM_MULTI is refused by
# the backend ("not yet supported"); FOREIGN_KEY needs a reference this module does
# not build.
KINDS = ("STANDARD", "URL", "ENUM_SINGLE", "CIRRO_FILE", "CIRRO_FOLDER", "CIRRO_DATASET")
CIRRO_KINDS = ("CIRRO_FILE", "CIRRO_FOLDER", "CIRRO_DATASET")
_STRING_KINDS = ("URL", "ENUM_SINGLE") + CIRRO_KINDS
_UNIQUE_REFUSED = ("DOUBLE", "FLOAT")
_INT32 = (-2**31, 2**31 - 1)
_INT64 = (-2**63, 2**63 - 1)

COLUMN_KEYS = ("name", "type", "label", "description", "required", "unique", "values",
               "kind", "hidden")
ALTER_KEYS = ("rename", "type", "label", "description", "values", "hidden", "required")

# The backend's FilterOperator, and the spellings `where` accepts for each.
OPERATORS = {"=": "EQUALS", "!=": "NOT_EQUALS", ">": "GREATER_THAN",
             ">=": "GREATER_THAN_OR_EQUALS", "<": "LESS_THAN",
             "<=": "LESS_THAN_OR_EQUALS", "like": "LIKE", "not like": "NOT_LIKE",
             "in": "IN", "not in": "NOT_IN"}
_LIST_OPERATORS = ("IN", "NOT_IN")
_NULL_OPERATORS = ("IS_NULL", "IS_NOT_NULL")


class SheetError(ValueError):
    """A request this module refuses, and the argument it is about."""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.field = field


def sheet_url(base_url: str, project_id: str, sheet_id: str) -> str:
    """Where the portal opens a sheet (its `project/:projectId/sheets/:sheetId` route)."""
    return f"https://{base_url}/project/{project_id}/sheets/{sheet_id}"


def sql_name(label: str) -> str:
    """The portal's `deriveSqlName`: lowercase, non-alphanumeric runs to `_`, and a
    `t_` prefix on a leading digit, because the backend refuses one."""
    name = "_".join(p for p in re.split(r"[^a-z0-9]+", label.strip().lower()) if p)
    return f"t_{name}" if name[:1].isdigit() else name


def identifier_problem(value, what: str) -> Optional[str]:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        return (f"{what} {value!r} is not a valid name: a letter or underscore, then "
                "letters, digits or underscores, at most 128 characters")
    return None


def user_columns(columns: list) -> list:
    """A sheet's columns without the ones the backend maintains."""
    return [c for c in columns or [] if not str(c.get("name", "")).startswith(SYSTEM_PREFIX)]


# ------------------------------------------------------------------ columns

def column_def(spec, where: str) -> dict:
    """An agent's column spec as the API's ColumnDef.

    `{"name", "type", "label", "description", "required", "unique", "values", "kind",
    "hidden"}` -- `values` makes an ENUM_SINGLE column, and `kind` names a URL or a
    Cirro file, folder or dataset reference, all of which the backend stores as STRING.
    """
    if not isinstance(spec, dict):
        raise SheetError(f"{where} must be an object like {{\"name\": ..., \"type\": ...}}",
                         where)
    unknown = sorted(set(spec) - set(COLUMN_KEYS))
    if unknown:
        raise SheetError(f"{where} does not take {unknown}; a column takes "
                         f"{list(COLUMN_KEYS)}", where)
    name = spec.get("name")
    if isinstance(name, str) and sql_name(name) and name != sql_name(name):
        # A label given as the name ("Sample ID", "Well"): keep it as the label and
        # derive the SQL name the way the portal does, so rows key on one spelling.
        spec = {**spec, "label": spec.get("label") or name, "name": sql_name(name)}
        name = spec["name"]
    problem = identifier_problem(name, f"{where}.name")
    if problem:
        raise SheetError(problem, f"{where}.name")
    if name.lower().startswith(SYSTEM_PREFIX) or name.lower() == ROW_ID:
        raise SheetError(f"{where}.name {name!r} is reserved for the columns the backend "
                         "maintains", f"{where}.name")
    values = spec.get("values")
    kind = str(spec.get("kind") or ("ENUM_SINGLE" if values is not None else "STANDARD"))
    kind = kind.upper()
    if kind not in KINDS:
        raise SheetError(f"{where}.kind {kind!r} is not one of {list(KINDS)}", f"{where}.kind")
    data_type = str(spec.get("type") or "STRING").upper()
    if data_type not in DATA_TYPES:
        raise SheetError(f"{where}.type {data_type!r} is not one of {list(DATA_TYPES)}",
                         f"{where}.type")
    if kind in _STRING_KINDS and data_type != "STRING":
        raise SheetError(f"{where}: a {kind} column holds text, so its type is STRING",
                         f"{where}.type")
    column = {"name": name, "dataType": data_type, "semanticType": kind,
              "required": bool(spec.get("required")), "unique": bool(spec.get("unique")),
              "hidden": bool(spec.get("hidden"))}
    if kind == "ENUM_SINGLE":
        column["allowedValues"] = _allowed_values(values, f"{where}.values")
    elif values is not None:
        raise SheetError(f"{where}: `values` makes an ENUM_SINGLE column; it cannot be "
                         f"combined with kind {kind}", f"{where}.values")
    if column["unique"] and data_type in _UNIQUE_REFUSED:
        raise SheetError(f"{where}: a {data_type} column cannot be unique", f"{where}.unique")
    for key, target in (("label", "displayName"), ("description", "description")):
        if spec.get(key) is not None:
            column[target] = str(spec[key])
    return column


def _allowed_values(values, where: str) -> list:
    if (not isinstance(values, list) or not values
            or not all(isinstance(v, (str, int, float)) and not isinstance(v, bool)
                       for v in values)):
        raise SheetError(f"{where} must be a non-empty list of values", where)
    if len(values) > MAX_ALLOWED_VALUES:
        raise SheetError(f"{where} has {len(values)} values; the most allowed is "
                         f"{MAX_ALLOWED_VALUES}", where)
    text = [str(v) for v in values]
    if len(set(text)) != len(text):
        raise SheetError(f"{where} lists a value twice", where)
    return text


def check_columns(columns: list, where: str = "columns") -> list:
    if not isinstance(columns, list) or not columns:
        raise SheetError(f"{where} must be a non-empty list of columns", where)
    built = [column_def(spec, f"{where}[{i}]") for i, spec in enumerate(columns)]
    seen = {}
    for i, column in enumerate(built):
        key = column["name"].lower()
        if key in seen:
            raise SheetError(f"{where}[{i}] repeats column {column['name']!r} "
                             f"(names are compared ignoring case)", f"{where}[{i}].name")
        seen[key] = i
    return built


def alter_columns(current: list, drop, alter, add) -> list:
    """The target column list for an update: the current columns, matched by `id`,
    with `drop` removed, `alter` applied and `add` appended. The backend diffs the two
    lists by id, so every current column keeps its id and a new one has none."""
    by_name = {c["name"]: c for c in user_columns(current)}
    drop = list(drop or [])
    alter = dict(alter or {})
    for name in drop + list(alter):
        if name not in by_name:
            raise SheetError(f"there is no column {name!r}; the columns are "
                             f"{sorted(by_name)}", "drop_columns" if name in drop
                             else "alter_columns")
    both = sorted(set(drop) & set(alter))
    if both:
        raise SheetError(f"{both} are both dropped and altered", "alter_columns")
    target = []
    for column in user_columns(current):
        if column["name"] in drop:
            continue
        change = alter.get(column["name"])
        target.append(_altered(column, change, f"alter_columns.{column['name']}")
                      if change is not None else dict(column))
    if add:
        target += check_columns(add, "add_columns")
    names = [c["name"].lower() for c in target]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise SheetError(f"the sheet would have two columns named {repeated}",
                         "add_columns")
    if not target:
        raise SheetError("a sheet needs at least one column", "drop_columns")
    return target


def _altered(column: dict, change, where: str) -> dict:
    if not isinstance(change, dict) or not change:
        raise SheetError(f"{where} must be an object of {list(ALTER_KEYS)}", where)
    unknown = sorted(set(change) - set(ALTER_KEYS))
    if unknown:
        raise SheetError(f"{where} does not take {unknown}; an alteration takes "
                         f"{list(ALTER_KEYS)}", where)
    out = dict(column)
    if "rename" in change:
        problem = identifier_problem(change["rename"], f"{where}.rename")
        if problem:
            raise SheetError(problem, f"{where}.rename")
        out["name"] = change["rename"]
    if "type" in change:
        data_type = str(change["type"]).upper()
        if data_type not in DATA_TYPES:
            raise SheetError(f"{where}.type {data_type!r} is not one of {list(DATA_TYPES)}",
                             f"{where}.type")
        out["dataType"] = data_type
    if "values" in change:
        if out.get("semanticType") != "ENUM_SINGLE":
            raise SheetError(f"{where}: only an ENUM_SINGLE column has `values`",
                             f"{where}.values")
        out["allowedValues"] = _allowed_values(change["values"], f"{where}.values")
    if "required" in change:
        if change["required"] and not column.get("required"):
            raise SheetError(f"{where}: an existing column cannot be made required",
                             f"{where}.required")
        out["required"] = bool(change["required"])
    for key, target in (("label", "displayName"), ("description", "description")):
        if key in change:
            out[target] = None if change[key] is None else str(change[key])
    if "hidden" in change:
        out["hidden"] = bool(change["hidden"])
    return out


def describe_column(column: dict) -> str:
    kind = column.get("semanticType") or "STANDARD"
    parts = [column["name"], column.get("dataType") or "?"]
    if kind != "STANDARD":
        parts.append(kind)
    parts += [flag for flag in ("required", "unique", "hidden") if column.get(flag)]
    if column.get("allowedValues"):
        shown = column["allowedValues"][:8]
        more = len(column["allowedValues"]) - len(shown)
        parts.append("{" + ", ".join(shown) + (f", +{more}" if more > 0 else "") + "}")
    if column.get("displayName") and column["displayName"] != column["name"]:
        parts.append(repr(column["displayName"]))
    return " ".join(parts)


# ------------------------------------------------------------------ values

def cirro_uri(kind: str, project_id: str, dataset_id: str, path: str = "") -> str:
    """A Cirro reference as the backend stores it,
    `cirro:<tenant>:<project>:data:<dataset>/<path>` -- the tenant segment may be empty,
    and the path is relative to the dataset's data directory."""
    path = str(path or "").lstrip("/")
    path = path[len("data/"):] if path.startswith("data/") else path
    if kind == "CIRRO_DATASET":
        return f"cirro::{project_id}:data:{dataset_id}"
    return f"cirro::{project_id}:data:{dataset_id}/{path}"


def _check_cirro_uri(kind: str, value: str) -> None:
    parts = value[len("cirro:"):].split(":", 3) if value.startswith("cirro:") else []
    if len(parts) != 4 or not parts[3]:
        raise ValueError("is not a Cirro reference (cirro:<tenant>:<project>:data:"
                         "<dataset>/<path>, or {\"dataset\": ..., \"path\": ...})")
    _, _, path = parts[3].partition("/")
    if kind == "CIRRO_FILE" and not path:
        raise ValueError("names a dataset, and a CIRRO_FILE cell needs a file path")
    if kind == "CIRRO_DATASET" and "/" in parts[3]:
        raise ValueError("has a path, and a CIRRO_DATASET cell names the dataset alone")


def coerce(column: dict, value, project_id: Optional[str] = None):
    """`value` as `column` stores it, or ValueError saying why it will not go.

    What the backend would otherwise hand to DuckDB as-is: `"5"` for an INTEGER, a
    `2026-09-30T12:00:00Z` string for a TIMESTAMP, `"yes"` for a BOOLEAN."""
    if value is None:
        if column.get("required"):
            raise ValueError("is required")
        return None
    kind = column.get("semanticType") or "STANDARD"
    data_type = column.get("dataType")
    if kind in CIRRO_KINDS:
        if isinstance(value, dict):
            if set(value) - {"dataset", "path"} or not value.get("dataset") or not project_id:
                raise ValueError("as an object takes {\"dataset\": <id>, \"path\": ...}")
            value = cirro_uri(kind, project_id, value["dataset"], value.get("path", ""))
        if not isinstance(value, str):
            raise ValueError("must be a Cirro reference string")
        _check_cirro_uri(kind, value)
        return value
    if data_type == "STRING":
        if isinstance(value, (dict, list)):
            raise ValueError("must be a single value, not a list or object")
        text = value if isinstance(value, str) else _plain_text(value)
        if kind == "ENUM_SINGLE" and text not in (column.get("allowedValues") or []):
            raise ValueError(f"is not one of the allowed values "
                             f"{(column.get('allowedValues') or [])[:20]}")
        return text
    if data_type in ("INTEGER", "BIGINT"):
        return _integer(value, _INT32 if data_type == "INTEGER" else _INT64)
    if data_type in ("DOUBLE", "FLOAT"):
        return _number(value)
    if data_type == "BOOLEAN":
        return _boolean(value)
    if data_type == "DATE":
        return _date(value)
    if data_type == "TIMESTAMP":
        return _timestamp(value)
    raise ValueError(f"is in a column of type {data_type}, which this plugin cannot write")


def _plain_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def _integer(value, bounds: tuple) -> int:
    if isinstance(value, bool):
        raise ValueError("is true/false, not a whole number")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("is not a whole number")
        value = int(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        if not re.fullmatch(r"[-+]?\d+(\.0+)?", text):
            raise ValueError("is not a whole number")
        value = int(float(text)) if "." in text else int(text)
    elif not isinstance(value, int):
        raise ValueError("is not a whole number")
    if not bounds[0] <= value <= bounds[1]:
        raise ValueError(f"is outside the column's range {bounds[0]}..{bounds[1]}")
    return value


def _number(value) -> float:
    if isinstance(value, bool):
        raise ValueError("is true/false, not a number")
    if isinstance(value, str):
        try:
            value = float(value.strip().replace(",", ""))
        except ValueError:
            raise ValueError("is not a number") from None
    if not isinstance(value, (int, float)):
        raise ValueError("is not a number")
    if not math.isfinite(value):
        raise ValueError("is not a finite number")
    return float(value)


_TRUE = ("true", "yes", "y", "1", "t")
_FALSE = ("false", "no", "n", "0", "f")


def _boolean(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in _TRUE + _FALSE:
        return value.strip().lower() in _TRUE
    raise ValueError("is not true or false")


def _date(value) -> str:
    if isinstance(value, dt.datetime):
        value = value.date()
    if isinstance(value, dt.date):
        return value.isoformat()
    # A midnight timestamp is a date; any other time of day would be silently dropped.
    match = isinstance(value, str) and re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})([T ]00:00(:00(\.0+)?)?Z?)?", value.strip())
    if match:
        try:
            return dt.date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            pass
    raise ValueError("is not a date (YYYY-MM-DD)")


def _timestamp(value) -> str:
    """ISO text in UTC without an offset: a TIMESTAMP column has no time zone, and
    DuckDB will not cast an offset-bearing string into one."""
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        value = dt.datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        text = value.strip()
        try:
            value = dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z")
                                              else text)
        except ValueError:
            raise ValueError("is not a timestamp (ISO 8601, e.g. 2026-09-30T14:05:00Z)") \
                from None
    if not isinstance(value, dt.datetime):
        raise ValueError("is not a timestamp (ISO 8601, e.g. 2026-09-30T14:05:00Z)")
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.isoformat(sep=" ")


def check_rows(columns: list, rows, *, inserting: bool,
               project_id: Optional[str] = None, where: str = "rows") -> tuple:
    """(coerced rows, problems). Each problem names its row and column; an insert also
    needs every required column present."""
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        raise SheetError(f"{where} must be a non-empty list of {{column: value}} objects",
                         where)
    by_name = {c["name"]: c for c in user_columns(columns)}
    required = [n for n, c in by_name.items() if c.get("required")]
    coerced, problems = [], []
    for index, row in enumerate(rows):
        out = {}
        for name, value in row.items():
            column = by_name.get(name)
            if column is None:
                problems.append(f"{where}[{index}]: there is no column {name!r}")
                continue
            try:
                out[name] = coerce(column, value, project_id)
            except ValueError as exc:
                problems.append(f"{where}[{index}].{name}: {_shown(value)} {exc}")
        if inserting:
            missing = [n for n in required if n not in row]
            if missing:
                problems.append(f"{where}[{index}]: required column(s) {missing} are missing")
        elif not row:
            problems.append(f"{where}[{index}] sets no columns")
        coerced.append(out)
    return coerced, problems


def duplicates(columns: list, rows: list) -> list:
    """Problems for unique columns given the same value twice in one write."""
    problems = []
    for column in user_columns(columns):
        if not column.get("unique"):
            continue
        seen = {}
        for index, row in enumerate(rows):
            value = row.get(column["name"])
            if value is None:
                continue
            if value in seen:
                problems.append(f"rows[{seen[value]}] and rows[{index}] both give "
                                f"{column['name']} {_shown(value)}, which must be unique")
            else:
                seen[value] = index
    return problems


def _shown(value) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


# ------------------------------------------------------------------ selections

def row_filter(where, columns: list, project_id: Optional[str] = None) -> dict:
    """`where` as the backend's Filter.

    Shorthand: `{"status": "failed"}` (equals), `{"batch": [1, 2]}` (in),
    `{"notes": null}` (is null), `{"score": {">=": 5}}` (any of the OPERATORS);
    several keys are ANDed. The backend's own shape --
    `{"column", "operator", "values"}` or `{"logicalOperator", "conditions"}` -- is
    passed through with its columns checked.
    """
    if not isinstance(where, dict) or not where:
        raise SheetError("`where` must be an object like {\"column\": value}", "where")
    by_name = {c["name"]: c for c in columns}
    if "conditions" in where or ("column" in where and "operator" in where):
        return _checked_filter(where, by_name, "where")
    conditions = []
    for name, test in where.items():
        column = _where_column(by_name, name)
        if test is None:
            conditions.append({"column": name, "operator": "IS_NULL"})
        elif isinstance(test, list):
            conditions.append({"column": name, "operator": "IN",
                               "values": [_where_value(column, v, name, project_id)
                                          for v in test]})
        elif isinstance(test, dict):
            for spelled, value in test.items():
                operator = OPERATORS.get(str(spelled).lower())
                if operator is None:
                    raise SheetError(f"where.{name}: {spelled!r} is not one of "
                                     f"{list(OPERATORS)}", f"where.{name}")
                values = value if isinstance(value, list) else [value]
                if operator not in _LIST_OPERATORS and len(values) != 1:
                    raise SheetError(f"where.{name}: {spelled!r} takes one value",
                                     f"where.{name}")
                conditions.append({"column": name, "operator": operator,
                                   "values": [_where_value(column, v, name, project_id,
                                                           like=operator.endswith("LIKE"))
                                              for v in values]})
        else:
            conditions.append({"column": name, "operator": "EQUALS",
                               "values": [_where_value(column, test, name, project_id)]})
    return conditions[0] if len(conditions) == 1 else {"logicalOperator": "AND",
                                                       "conditions": conditions}


def _where_column(by_name: dict, name: str) -> Optional[dict]:
    if name == ROW_ID:
        return None
    if name not in by_name:
        raise SheetError(f"where: there is no column {name!r}; the columns are "
                         f"{sorted(by_name)}", "where")
    return by_name[name]


def _where_value(column, value, name, project_id, like=False):
    if column is None:
        return _integer(value, _INT64)
    if like:
        return str(value)
    try:
        return coerce({**column, "required": False}, value, project_id)
    except ValueError as exc:
        raise SheetError(f"where.{name}: {_shown(value)} {exc}", f"where.{name}") from None


def _checked_filter(node, by_name: dict, where: str) -> dict:
    if "conditions" in node:
        operator = str(node.get("logicalOperator") or "AND").upper()
        if operator not in ("AND", "OR") or not isinstance(node["conditions"], list) \
                or not node["conditions"]:
            raise SheetError(f"{where}: a group is {{\"logicalOperator\": AND|OR, "
                             "\"conditions\": [...]}", where)
        return {"logicalOperator": operator,
                "conditions": [_checked_filter(c, by_name, f"{where}.conditions[{i}]")
                               for i, c in enumerate(node["conditions"])]}
    if not isinstance(node, dict) or "column" not in node or "operator" not in node:
        raise SheetError(f"{where}: a condition is {{\"column\", \"operator\", "
                         "\"values\"}", where)
    _where_column(by_name, node["column"])
    operator = str(node["operator"]).upper()
    if operator not in set(OPERATORS.values()) | set(_NULL_OPERATORS):
        raise SheetError(f"{where}.operator {operator!r} is not a filter operator", where)
    out = {"column": node["column"], "operator": operator}
    if operator not in _NULL_OPERATORS:
        values = node.get("values")
        if not isinstance(values, list) or not values:
            raise SheetError(f"{where}: {operator} takes `values`, a non-empty list", where)
        out["values"] = values
    return out


def row_id_filter(row_ids: list) -> dict:
    return {"column": ROW_ID, "operator": "IN", "values": list(row_ids)}


def page_rows(page: dict) -> list:
    """A query page's rows as {column: value} dicts, `_row_id` included."""
    names = [c["name"] for c in page.get("columns") or []]
    return [dict(zip(names, row)) for row in page.get("rows") or []]


def rows_digest(rows: list) -> str:
    """A fingerprint of rows as read, `sys_updated_at` included, so a row changed by
    anyone between plan and apply changes it."""
    ordered = sorted(rows, key=lambda r: r.get(ROW_ID) or 0)
    return hashlib.sha256(json.dumps(ordered, sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


def sheet_digest(detail: dict) -> str:
    """A fingerprint of what a sheet-level write is planned against: its names, its
    columns and its view definition -- not its rows, which a schema write leaves be."""
    shape = {k: detail.get(k) for k in ("name", "description", "tableName", "columns",
                                        "viewDefinition", "schemaVersionId", "status")}
    return hashlib.sha256(json.dumps(shape, sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


def render_rows(rows: list, columns: list, limit: int = 10, indent: str = "    ") -> list:
    """Rows as aligned text for a plan, system columns left out."""
    shown = rows[:limit]
    if not shown:
        return [f"{indent}(no rows)"]
    names = [n for n in columns if not n.startswith(SYSTEM_PREFIX)]
    cells = [[_cell(r.get(n)) for n in names] for r in shown]
    widths = [min(max([len(n)] + [len(row[i]) for row in cells]), 32)
              for i, n in enumerate(names)]
    lines = [indent + "  ".join(n.ljust(w)[:w] for n, w in zip(names, widths))]
    lines += [indent + "  ".join(c.ljust(w)[:w] for c, w in zip(row, widths)) for row in cells]
    if len(rows) > limit:
        lines.append(f"{indent}... and {len(rows) - limit:,} more")
    return lines


def _cell(value) -> str:
    return "null" if value is None else str(value)

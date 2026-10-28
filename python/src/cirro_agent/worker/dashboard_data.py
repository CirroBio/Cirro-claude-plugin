"""Run a dashboard's data locally, the way the portal's DuckDB-WASM runtime does.

The portal only ever runs a dashboard in a browser, so without this an agent that wrote
one could not see what it shows. Each source is loaded into DuckDB, each SQL node runs
over its upstream tables under the same aliases the portal creates (`query`, `in1..N`,
the port name, a source's table name), and every view reports the table it would draw:
row count, columns and the data class the portal would assign each one.

Only what an approval can rely on appears in `profile`: counts, columns and classes are
the same on every run, while sampled rows are not (DuckDB returns unordered rows in any
order), so rows are returned only when a read asks for them.
"""

from pathlib import Path
from typing import Optional

from .. import dashboards as D
from . import reads

# Past this a file is not downloaded to check a plan; the portal streams it instead.
MAX_FILE_BYTES = 500 * 1024 * 1024
MAX_SHEET_ROWS = 200_000
SHEET_PAGE = 5_000
SNIFF_ROWS = 10_000
# Sampled per varchar column to decide whether the portal would promote it.
REFINE_SAMPLE = 1_000

_NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT",
            "UINTEGER", "UBIGINT", "UHUGEINT", "FLOAT", "DOUBLE", "REAL", "DECIMAL")
_TEMPORAL = ("DATE", "TIME", "TIMESTAMP", "INTERVAL")


class SkipNode(Exception):
    """A node this runner cannot execute locally; its message says why."""


class Refused(Exception):
    """A node asks for something this runner will not do; reported as its failure."""


def _connect(files: Path):
    """A DuckDB that can read the files a dashboard loads and reach nothing else.

    The SQL it runs is not ours. A dashboard document is written by any member of its
    project and a preview takes whatever the caller passes, and both arrive through
    ungated paths -- a read and a plan. At DuckDB's defaults that SQL could COPY to any
    path, read_text() any file and install extensions, so the connection sees one
    directory, the download cache, and its settings are locked before any of it runs.
    `allowed_directories` can only be set once the database has started and before
    external access is turned off, which is the order below.
    """
    import duckdb

    files.mkdir(parents=True, exist_ok=True)
    # One thread: an unordered result then comes back in one order, run to run.
    con = duckdb.connect(config={"threads": 1, "autoinstall_known_extensions": False,
                                 "autoload_known_extensions": False})
    con.execute("SET allowed_directories = ?", [[str(files.resolve())]])
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


def one_select(con, sql: str) -> str:
    """`sql`, if it is exactly one SELECT; Refused otherwise.

    The connection already cannot reach a file outside the download cache, but a COPY
    can still write inside it, and nothing a view draws needs more than one query.
    """
    import duckdb

    statements = con.extract_statements(sql)
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise Refused("only a single SELECT runs here: no COPY, ATTACH, SET, INSTALL or "
                      "other statement, and nothing after a ';'. A PIVOT has to list its "
                      "values (ON column IN (...)) to stay one statement.")
    return sql


def _table(node_id: str) -> str:
    return f"cell_{node_id}_out"


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _reader_expr(path: str, file_type: str) -> str:
    """The portal's `fileReaderExpr`, over a local path."""
    literal = _quote_literal(path)
    if file_type == "parquet":
        return f"read_parquet({literal})"
    if file_type == "json":
        return f"read_json_auto({literal})"
    if file_type == "tsv":
        return f"read_csv_auto({literal}, delim='\\t', sample_size={SNIFF_ROWS})"
    return f"read_csv_auto({literal}, sample_size={SNIFF_ROWS})"


def column_class(duck_type: str) -> str:
    base = duck_type.upper().split("(")[0].strip()
    if base in _NUMERIC:
        return "quantitative"
    if any(base.startswith(t) for t in _TEMPORAL):
        return "temporal"
    return "categorical"


# The portal's `looksNumeric`: decimal or scientific only, never hex.
_NUMBER_PATTERN = r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?"


def _looks_numeric(text: str) -> bool:
    import re

    return bool(re.fullmatch(_NUMBER_PATTERN, text.strip()))


def _system_column(name: str) -> bool:
    """The portal's `isSystemColumn`: backend plumbing it never loads into a view."""
    return name == "_row_id" or name.startswith("sys_")


def profile(con, table: str, *, sample_rows: int = 0) -> dict:
    """What a view over `table` would show. Classes follow the portal: numeric is
    quantitative, dates are temporal, and a text column whose values are all numbers is
    promoted to quantitative (`refineType`)."""
    described = con.execute(f"DESCRIBE {table}").fetchall()
    rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    columns = []
    for name, duck_type, *_ in described:
        cls = column_class(duck_type)
        if cls == "categorical" and duck_type.upper() == "VARCHAR":
            values = [v for (v,) in con.execute(
                f"SELECT {_quote_ident(name)} FROM {table} "
                f"WHERE {_quote_ident(name)} IS NOT NULL LIMIT {REFINE_SAMPLE}").fetchall()]
            if values and all(_looks_numeric(v) for v in values):
                cls = "quantitative"
        entry = {"name": name, "type": duck_type, "class": cls}
        if cls == "categorical":
            entry["distinct"] = con.execute(
                f"SELECT count(DISTINCT {_quote_ident(name)}) FROM {table}").fetchone()[0]
        columns.append(entry)
    out = {"rows": rows, "columns": columns}
    if sample_rows:
        cursor = con.execute(f"SELECT * FROM {table} LIMIT {int(sample_rows)}")
        names = [d[0] for d in cursor.description]
        out["sample"] = [dict(zip(names, (_plain(v) for v in row)))
                         for row in cursor.fetchall()]
    return out


def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class Runner:
    """Loads a project's sources on demand and runs SQL the way the portal does."""

    def __init__(self, reader: reads.Reader, project_id: str, schema: D.Schema):
        self.reader = reader
        self.project_id = project_id
        self.schema = schema
        self.cache = (Path(reader.session_dir) / "cache" / "dashboard-files").resolve()
        self.con = _connect(self.cache)

    # -------------------------------------------------------------- sources

    def load_file(self, table: str, dataset_id: str, path: str, file_type: str,
                  size: Optional[int] = None) -> None:
        if size is not None and size > MAX_FILE_BYTES:
            raise SkipNode(
                f"{path} is {size / 2**20:.0f} MiB, over the {MAX_FILE_BYTES // 2**20} MiB "
                "a plan downloads to check it; the portal will still load it")
        # Both come from a document anyone in the project can write, and pathlib drops
        # everything before an absolute component -- so an absolute dataset id named a
        # file anywhere on this machine, read with no download at all.
        local = (self.cache / dataset_id / path).resolve()
        if not local.is_relative_to(self.cache):
            raise Refused(f"dataset {dataset_id!r} file {path!r} lies outside the "
                          "download folder")
        if not (local.is_file() and (size is None or local.stat().st_size == size)):
            dataset = reads.portal_dataset(self.reader, self.project_id, dataset_id)
            self.reader.cost.call("files.download")
            dataset.get_file(path).download(str(self.cache / dataset_id))
        self.con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM "
                         f"{_reader_expr(str(local), file_type)}")

    def load_sheet(self, table: str, sheet_id: str) -> Optional[str]:
        """Page a sheet's rows into `table`, typed the way the portal types them; returns
        a note when it stopped short."""
        import pandas as pd
        from cirro_api_client.v1.api.sheets import query_sheet_data
        from cirro_api_client.v1.models import SheetDataRequest

        rows, columns, page, total = [], None, 1, 0
        while True:
            body = reads.send(self.reader, query_sheet_data, what="Reading the sheet failed.",
                              cost="sheets.query", project_id=self.project_id,
                              sheet_id=sheet_id,
                              body=SheetDataRequest(limit=SHEET_PAGE, page=page))
            if body is None:
                raise SkipNode(f"sheet {sheet_id} was not found")
            columns = columns or body["columns"]
            total = body["totalRowCount"]
            rows.extend(body["rows"])
            if not body["rows"] or len(rows) >= min(total, MAX_SHEET_ROWS):
                break
            page += 1
        rows = rows[:MAX_SHEET_ROWS]
        # Every cell as text (booleans as the portal's String(true)), cast per column below.
        text = [[None if v is None else str(v).lower() if isinstance(v, bool) else str(v)
                 for v in row] for row in rows]
        frame = pd.DataFrame(text, columns=[c["name"] for c in columns], dtype=object)
        self.con.register("_sheet_frame", frame)
        casts = [f"TRY_CAST({_quote_ident(c['name'])} AS {self._sheet_type(c)}) "
                 f"AS {_quote_ident(c['name'])}"
                 for c in columns if not _system_column(c["name"])]
        self.con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT {', '.join(casts)} "
                         "FROM _sheet_frame")
        self.con.unregister("_sheet_frame")
        if total > MAX_SHEET_ROWS:
            return f"checked the first {MAX_SHEET_ROWS:,} of {total:,} rows"
        return None

    def _sheet_type(self, column: dict) -> str:
        """The portal's column typing for a sheet: its declared type classified
        (`classifyType`), then a text column promoted when its first page's values are all
        numbers or all dates (`refineType`), then that class's DuckDB type (`duckdbType`)."""
        declared = str(column.get("dataType") or "").upper()
        if declared in ("BIGINT", "DOUBLE", "FLOAT", "INTEGER"):
            return "DOUBLE"
        if declared in ("DATE", "TIMESTAMP"):
            return "TIMESTAMP"
        if declared == "BOOLEAN":
            return "VARCHAR"
        name = _quote_ident(column["name"])
        numeric, temporal, seen = self.con.execute(
            f"SELECT bool_and(regexp_full_match(trim(v), '{_NUMBER_PATTERN}')), "
            "bool_and(regexp_matches(v, '[-/:T]') "
            "AND TRY_CAST(trim(v) AS TIMESTAMP) IS NOT NULL), "
            f"count(v) FROM (SELECT {name} AS v FROM _sheet_frame WHERE {name} IS NOT NULL "
            f"LIMIT {REFINE_SAMPLE})").fetchone()
        if seen and numeric:
            return "DOUBLE"
        if seen and temporal:
            return "TIMESTAMP"
        return "VARCHAR"

    # -------------------------------------------------------------- the graph

    def run(self, document: dict, *, sample_rows: int = 0) -> dict:
        """node id -> {"profile"} | {"error"} | {"skipped"} | {"note"}, for every node
        that produces or shows a table, in dependency order."""
        nodes = {n["id"]: n for n in document["ir"]["nodes"]}
        import duckdb

        results = {}
        for node_id in _topological(document):
            node = nodes[node_id]
            upstream = [e for e in document["ir"]["edges"] if e["to"]["node"] == node_id]
            # Controls produce no table (their result is None), so they never break a node.
            broken = [e["from"]["node"] for e in upstream
                      if results.get(e["from"]["node"]) is not None
                      and "profile" not in results[e["from"]["node"]]]
            try:
                if broken:
                    raise SkipNode(f"upstream {broken} did not load")
                results[node_id] = self._run_node(document, node, upstream, sample_rows)
            except SkipNode as exc:
                results[node_id] = {"skipped": str(exc)}
            except (duckdb.Error, Refused) as exc:
                # A query that fails is the finding, reported against its node.
                results[node_id] = {"error": _duck_message(exc)}
        return {k: v for k, v in results.items() if v is not None}

    def _run_node(self, document, node, upstream, sample_rows) -> Optional[dict]:
        config = node.get("config") or {}
        kind = node["type"]
        table = _table(node["id"])
        note = None
        if kind == "dataset-file":
            self.load_file(table, config["datasetId"], config["path"],
                           config.get("fileType", "csv"), config.get("sizeBytes"))
        elif kind == "dataset-source":
            note = self.load_sheet(table, config["sheetId"])
        elif kind in ("sql-query", "join"):
            sql = config.get("sql")
            if not sql:
                raise SkipNode("no SQL")
            self._aliases(document, node, upstream)
            sql = one_select(self.con, self._substitute(document, node, sql))
            self.con.execute(f"CREATE OR REPLACE TABLE {table} AS {sql}")
        elif kind in ("chart", "result-grid", "legend", "chart-render"):
            data = next((e["from"]["node"] for e in upstream
                         if e["to"]["port"] in ("data", "relation")), None)
            if data is None:
                raise SkipNode("nothing wired into its data input")
            return {"profile": profile(self.con, _table(data), sample_rows=sample_rows),
                    "data": data}
        elif kind == "namespace-query":
            raise SkipNode("server-side queries are not run locally")
        else:
            return None
        out = {"profile": profile(self.con, table, sample_rows=sample_rows)}
        if note:
            out["note"] = note
        return out

    def _aliases(self, document, node, upstream) -> None:
        """The portal's `aliasDDLs`: by position, `query` and `in1` both the first input
        and `in2..N` the rest (there is no `in0`), one view per input port, and one per
        source table name."""
        nodes = {n["id"]: n for n in document["ir"]["nodes"]}
        relation_ports = {p["id"] for p in self.schema.ports(node)[0]
                          if p["type"] == "Relation"}
        bindings = [e for e in upstream if e["to"]["port"] in relation_ports]
        for i, edge in enumerate(bindings):
            source = _table(edge["from"]["node"])
            if i == 0:
                self.con.execute(
                    f"CREATE OR REPLACE TEMP VIEW query AS SELECT * FROM {source}")
            self.con.execute(
                f"CREATE OR REPLACE TEMP VIEW in{i + 1} AS SELECT * FROM {source}")
        for edge in bindings:
            source = _table(edge["from"]["node"])
            self.con.execute(
                f"CREATE OR REPLACE TEMP VIEW {_quote_ident(edge['to']['port'])} AS "
                f"SELECT * FROM {source}")
            upstream_node = nodes[edge["from"]["node"]]
            name = ((upstream_node.get("config") or {}).get("tableName")
                    if upstream_node["type"] in ("dataset-file", "dataset-source") else None)
            if name and name != edge["to"]["port"]:
                self.con.execute(f"CREATE OR REPLACE TEMP VIEW {_quote_ident(name)} AS "
                                 f"SELECT * FROM {source}")

    def _substitute(self, document, node, sql: str) -> str:
        """The portal's `substituteParams` over `collectParams`: each wired `{token}`
        becomes a typed literal from the control or param feeding it. Unwired tokens stay,
        and DuckDB reports them -- which is what the portal shows too."""
        nodes = {n["id"]: n for n in document["ir"]["nodes"]}
        literals = {}
        for edge in document["ir"]["edges"]:
            port = edge["to"]["port"]
            if edge["to"]["node"] != node["id"] or not port.startswith("param:"):
                continue
            name = port[len("param:"):]
            source = nodes.get(edge["from"]["node"]) or {}
            c = source.get("config") or {}
            if c.get("inputType") == "multiselect":
                values = [str(v) for v in c.get("values") or []]
                literal = ", ".join(_quote_literal(v) for v in values) if values else "NULL"
            elif c.get("inputType") == "number" or c.get("paramType") == "number":
                try:
                    literal = repr(float(c.get("value")))
                    literal = (str(int(float(literal))) if float(literal).is_integer()
                               else literal)
                except (TypeError, ValueError):
                    literal = "NULL"
            else:
                literal = _quote_literal(c.get("value") if c.get("value") is not None
                                         else c.get("selectionValue", ""))
            literals.setdefault(name, literal)
        if not literals:
            return sql
        return self.schema.token.sub(lambda m: literals.get(m.group(1), m.group(0)), sql)


def _topological(document: dict) -> list:
    order, seen = [], set()
    incoming = {}
    for e in document["ir"]["edges"]:
        incoming.setdefault(e["to"]["node"], []).append(e["from"]["node"])

    def visit(node_id, stack=()):
        if node_id in seen or node_id in stack:
            return
        for parent in incoming.get(node_id, []):
            visit(parent, stack + (node_id,))
        seen.add(node_id)
        order.append(node_id)

    for node in document["ir"]["nodes"]:
        visit(node["id"])
    return order


def _duck_message(exc: Exception) -> str:
    text = str(exc).strip()
    return text.split("\n\nLINE")[0] if text else type(exc).__name__


def view_issues(schema: D.Schema, document: dict, results: dict) -> list:
    """Problems only the data can show: a bound column that is not in the view's table,
    one of a class the channel refuses, a categorical past the channel's window, a query
    that failed, a view with nothing to draw."""
    issues = []
    nodes = {n["id"]: n for n in document["ir"]["nodes"]}
    for node_id, result in results.items():
        node = nodes[node_id]
        if "error" in result:
            issues.append(D._issue("error", "data",
                                   f"{node['type']} {node_id} failed: {result['error']}",
                                   node_id))
            continue
        if node["type"] != "chart" or "profile" not in result:
            continue
        columns = {c["name"]: c for c in result["profile"]["columns"]}
        if result["profile"]["rows"] == 0:
            issues.append(D._issue("warning", "data", f"view {node_id} has no rows to show",
                                   node_id))
        config = node.get("config") or {}
        view_type = config.get("viewType") or "table"
        chart = schema.charts.get(view_type)
        if not chart:
            continue
        channels = {ch["channel"]: ch for ch in chart["channels"]}
        for channel, column in D.read_bindings(schema, view_type, config.get("spec")).items():
            ch = channels[channel]
            found = columns.get(column)
            if found is None:
                issues.append(D._issue(
                    "error", "data",
                    f"view {node_id} binds {channel}={column!r}, which is "
                    f"not a column of its data; columns are {sorted(columns)}", node_id))
            elif found["class"] not in ch["accepts"]:
                issues.append(D._issue("error", "data",
                                       f"view {node_id}: {channel} takes {ch['accepts']} "
                                       f"columns and {column!r} is {found['class']}", node_id))
            elif found["class"] == "categorical" and "distinct" in found:
                if (ch.get("maxDistinct") and found["distinct"] > ch["maxDistinct"]
                        and not ch.get("binAbove")):
                    issues.append(D._issue(
                        "warning", "data",
                        f"view {node_id}: {column!r} has "
                        f"{found['distinct']} values, more than {view_type}'s {channel} "
                        f"reads well ({ch['maxDistinct']})", node_id))
                if ch.get("minDistinct") and found["distinct"] < ch["minDistinct"]:
                    issues.append(D._issue(
                        "warning", "data",
                        f"view {node_id}: {column!r} has only "
                        f"{found['distinct']} values; {view_type}'s {channel} expects at "
                        f"least {ch['minDistinct']}", node_id))
        for edge in document["ir"]["edges"]:
            if edge["to"]["node"] != node_id or edge["to"]["port"] != "filter":
                continue
            control = (nodes.get(edge["from"]["node"]) or {}).get("config") or {}
            if control.get("column") and control["column"] not in columns:
                issues.append(D._issue(
                    "error", "data",
                    f"control {edge['from']['node']} filters view {node_id} "
                    f"on {control['column']!r}, which is not a column of its data", node_id))
    return issues


def render_profiles(document: dict, results: dict) -> list:
    """One line per node that loaded, failed or was skipped -- for the approval text."""
    lines = []
    nodes = {n["id"]: n for n in document["ir"]["nodes"]}
    for node_id, result in results.items():
        node = nodes[node_id]
        label = f"{node['type']} {node_id}"
        if "error" in result:
            lines.append(f"  {label}: FAILED -- {result['error']}")
        elif "skipped" in result:
            lines.append(f"  {label}: not checked -- {result['skipped']}")
        else:
            p = result["profile"]
            shown = ", ".join(f"{c['name']} ({c['class'][:4]})" for c in p["columns"][:12])
            more = f", +{len(p['columns']) - 12} more" if len(p["columns"]) > 12 else ""
            lines.append(f"  {label}: {p['rows']:,} rows; {shown}{more}"
                         + (f" [{result['note']}]" if result.get("note") else ""))
    return lines

"""Data Studio dashboards: the document, its rules, and the edits an agent may make.

Pure and cirro-free. The worker resolves names and loads data; everything here works on
plain dicts, so the planner, the reads and the tests share one implementation.

A dashboard record's `dashboardData` is the portal's `PersistedDashboard`: a node graph
(`ir`), canvas positions, and one or more pages of tiles. What a valid graph is --
node types, ports, config fields, chart channels, the tile grid -- is owned by the
portal's `@cirrobio/dashboard` package and exported as `dashboard-schema.json`. A copy
is vendored in `data/`; the worker prefers the one the tenant's portal serves, so a
plan validates against the portal that will render it.

The portal does not reject a bad document. It resets a node config whose field has the
wrong type to `{}`, drops tiles that name no node, and draws a chart with an unbound
channel as nothing at all -- quietly. So the checks below are stricter than the portal:
they exist to turn those silent failures into a refused plan.
"""

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Optional

SCHEMA_FILE = Path(__file__).parent / "data" / "dashboard-schema.json"
# The shape of dashboard-schema.json this module reads (its own `format` field).
SCHEMA_FORMAT = 1

# The portal's record envelope (Cirro-portal dashboard-session-store.ts). `list` there
# shows only records whose criteria carry this type, so a record without it is invisible.
SESSION_TYPE = "sql-room"
RECORD_SCHEMA_VERSION = 1

# DynamoDB's item limit is 400 KB and the blob shares it with every other attribute.
MAX_BLOB_BYTES = 350_000

TABULAR_TYPES = ("csv", "tsv", "parquet", "json", "txt", "tab", "dsv")

# Chart-node config fields `add_view`/`update_view` set themselves, so `options` may not.
_VIEW_MANAGED = ("viewType", "spec", "title", "selectionValue", "selectionColumn")

CONTROL_KINDS = ("text", "number", "select", "multiselect", "range")

_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")


class ChangeError(Exception):
    """One change in a change list cannot be applied. `index` is its position."""

    def __init__(self, index: int, message: str, *, field: Optional[str] = None):
        super().__init__(message)
        self.index = index
        self.message = message
        self.field = field


# ------------------------------------------------------------------ the schema


class Schema:
    """dashboard-schema.json, with the lookups the rest of this module needs."""

    def __init__(self, raw: dict, *, source: str):
        if not isinstance(raw, dict) or raw.get("format") != SCHEMA_FORMAT:
            raise ValueError(f"dashboard schema from {source} has format "
                             f"{raw.get('format') if isinstance(raw, dict) else None!r}, "
                             f"expected {SCHEMA_FORMAT}")
        self.raw = raw
        self.source = source
        self.persist_version = raw["persistVersion"]
        self.node_types = {t["type"]: t for t in raw["nodeTypes"]}
        self.charts = {c["id"]: c for c in raw["charts"]}
        self.cell_id = re.compile(raw["cellIdPattern"])
        self.token = re.compile(raw["sqlTokenPattern"])
        self.grid = raw["grid"]
        self.mosaic_schema_url = raw["mosaicSchemaUrl"]

    def config_properties(self, node_type: str) -> Optional[dict]:
        schema = (self.node_types.get(node_type) or {}).get("configSchema")
        return (schema or {}).get("properties") if schema else None

    def renderable(self, node_type: str) -> bool:
        return bool((self.node_types.get(node_type) or {}).get("renderable"))

    def tokens(self, sql: str) -> list:
        return list(dict.fromkeys(self.token.findall(sql or "")))

    def ports(self, node: dict) -> tuple:
        """(inputs, outputs) for one node instance: static ports plus the manifest's
        config-derived ones, per the schema's declarative `dynamicPorts` rule. Mirrors
        the reference interpreter in the portal's dashboard-schema.test.ts."""
        spec = self.node_types.get(node.get("type"))
        if spec is None:
            return [], []
        inputs, outputs = list(spec["inputs"]), list(spec["outputs"])
        rule = spec.get("dynamicPorts")
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        if not rule:
            return inputs, outputs
        if rule["kind"] == "sql-tokens":
            port = {k: v for k, v in rule["port"].items() if k != "idPrefix"}
            for name in dict.fromkeys(re.findall(rule["pattern"],
                                                 str(config.get(rule["field"]) or ""))):
                inputs.append({**port, "id": f"{rule['port']['idPrefix']}{name}",
                               "role": name})
        elif rule["kind"] == "chart-channels":
            chart = self.charts.get(str(config.get(rule["field"]) or rule["default"]))
            for ch in (chart or {}).get("channels", []):
                inputs.append({"id": f"{rule['port']['idPrefix']}{ch['channel']}",
                               "type": rule["port"]["type"], "role": ch["channel"],
                               "cardinality": rule["port"]["cardinality"],
                               "required": ch["required"],
                               "accepts": {"columnClasses": ch["accepts"]}})
        elif rule["kind"] == "column-class":
            cls = config.get(rule["field"])
            outputs = [({**p,
                         "accepts": {"columnClasses": [cls] if isinstance(cls, str) else None}}
                        if p["id"] == rule["outputPort"] else p) for p in outputs]
        return inputs, outputs


def vendored_schema() -> Schema:
    return Schema(json.loads(SCHEMA_FILE.read_text()), source="the plugin's vendored copy")


# ------------------------------------------------------------------ documents


def empty_document(schema: Schema) -> dict:
    """What the portal's `initialGraphState` persists for a graph with no nodes."""
    return {"version": schema.persist_version, "ir": {"nodes": [], "edges": []},
            "positions": {}, "dashboards": [{"id": "default", "name": "Default", "tiles": []}],
            "activeDashboardId": "default"}


def studio_url(base_url: str, project_id: str, dashboard_id: str) -> str:
    """Where the portal opens a dashboard (its `project/:projectId/studio/:id` route)."""
    return f"https://{base_url}/project/{project_id}/studio/{dashboard_id}"


def blob_digest(document) -> str:
    """A stable fingerprint of a stored blob, so apply can tell it has not moved."""
    return hashlib.sha256(json.dumps(document, sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


def is_session_blob(document) -> bool:
    """A real snapshot has a numeric version; `{}` is the create-time placeholder."""
    return isinstance(document, dict) and isinstance(document.get("version"), int)


def infer_scope(document: dict) -> dict:
    """The portal's `inferScope`: one source pins the record to its dataset or sheet."""
    sources = [n for n in document["ir"]["nodes"]
               if n["type"] in ("dataset-source", "dataset-file")]
    if len(sources) != 1:
        return {"scope": "project"}
    config = sources[0].get("config") or {}
    if sources[0]["type"] == "dataset-source":
        return ({"scope": "sheet", "sheetId": config["sheetId"]} if config.get("sheetId")
                else {"scope": "project"})
    return ({"scope": "dataset", "datasetId": config["datasetId"]} if config.get("datasetId")
            else {"scope": "project"})


def file_table_name(path: str) -> str:
    """The portal's `fileTableName`: a SQL-safe alias from a file's basename."""
    base = re.sub(r"\.[^.]+$", "", path.split("/")[-1])
    ident = re.sub(r"^(\d)", r"_\1", re.sub(r"[^A-Za-z0-9_]", "_", base))
    return ident or "file"


def file_type(path: str) -> Optional[str]:
    ext = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    return ext if ext in TABULAR_TYPES else None


def title_case(identifier: str) -> str:
    """The portal's `titleCaseIdentifier`: `gene_name` -> `Gene Name`."""
    words = " ".join(w[:1].upper() + w[1:] for w in re.split(r"[_-]+", identifier) if w)
    return words or identifier


def _node_index(document: dict) -> dict:
    return {n["id"]: n for n in document["ir"]["nodes"]}


def _page(document: dict, page_id: Optional[str]) -> Optional[dict]:
    wanted = page_id or document.get("activeDashboardId")
    pages = document.get("dashboards") or []
    return next((p for p in pages if p["id"] == wanted),
                pages[0] if pages and not page_id else None)


# ------------------------------------------------------------------ chart specs


def _set_path(obj, path: list, value) -> None:
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value


def _get_path(obj, path: list):
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and 0 <= key < len(obj):
            obj = obj[key]
        else:
            return None
    return obj


def _mark_column(value) -> Optional[str]:
    """The column an axis channel names for labelling: `col` or `{bin: col}`, never an
    aggregate -- the portal's `channelColumn`."""
    if isinstance(value, str):
        return value
    if (isinstance(value, dict) and len(value) == 1 and "bin" in value
            and isinstance(value["bin"], str)):
        return value["bin"]
    return None


def build_spec(schema: Schema, chart_id: str, bindings: dict) -> str:
    """A Mosaic spec for a catalog chart with `bindings` (channel -> column) written at
    each channel's paths, and humanized axis titles -- what the portal's `buildSpec`
    and `patchChannel` produce, serialized the way its `serializeSpec` does."""
    chart = schema.charts[chart_id]
    spec = {"$schema": schema.mosaic_schema_url, "plot": copy.deepcopy(chart["base"]["plot"])}
    for ch in chart["channels"]:
        column = bindings.get(ch["channel"])
        if column:
            for path in ch["paths"]:
                _set_path(spec, list(path), column)
    first = next((m for m in spec["plot"] if m.get("mark") is not None), None)
    for channel, attr in (("x", "xLabel"), ("y", "yLabel"), ("fx", "fxLabel")):
        column = _mark_column((first or {}).get(channel))
        if column and title_case(column) != column:
            spec[attr] = title_case(column)
    return json.dumps(spec, indent=2) + "\n"


def read_bindings(schema: Schema, chart_id: str, spec_text) -> dict:
    """channel -> column read back from a spec's first path per channel, ignoring the
    literal the chart's own skeleton bakes in there (the portal's `tryReadBindings`)."""
    chart = schema.charts.get(chart_id)
    try:
        spec = json.loads(spec_text) if isinstance(spec_text, str) else None
    except ValueError:
        return {}
    if not chart or not isinstance(spec, dict):
        return {}
    found = {}
    for ch in chart["channels"]:
        path = list(ch["paths"][0])
        value = _get_path(spec, path)
        if not isinstance(value, str) and len(path) > 1:
            value = _get_path(spec, path[:-1])
        if isinstance(value, str) and value != _get_path(chart["base"], path):
            found[ch["channel"]] = value
    return found


def patch_spec(schema: Schema, chart_id: str, spec_text, updates: dict) -> str:
    """`updates` (channel -> column, or None to clear) written into an existing spec in
    place -- the portal's `patchChannel`. Everything else in the spec survives, hand
    edits included; a cleared channel gets back the literal its skeleton puts there. An
    axis title that still names the column being replaced follows it, while one its
    author typed is kept. A spec that does not parse is rebuilt from the skeleton."""
    chart = schema.charts[chart_id]
    try:
        spec = json.loads(spec_text) if isinstance(spec_text, str) else None
    except ValueError:
        spec = None
    if not isinstance(spec, dict) or not isinstance(spec.get("plot"), list):
        return build_spec(schema, chart_id, {k: v for k, v in updates.items() if v})
    before = read_bindings(schema, chart_id, spec_text)
    channels = {ch["channel"]: ch for ch in chart["channels"]}
    for channel, column in updates.items():
        for path in (list(p) for p in channels[channel]["paths"]):
            baked = _get_path(chart["base"], path)
            value = column if column else (baked if isinstance(baked, str) else None)
            try:
                if value is None and not isinstance(path[-1], int):
                    parent = _get_path(spec, path[:-1])
                    if isinstance(parent, dict):
                        parent.pop(path[-1], None)
                else:
                    _set_path(spec, path, value)
            except (KeyError, IndexError, TypeError):
                # An older shape at this path (a bare column where the catalog now has a
                # reducer): restore the skeleton's parent, then write -- the portal's heal.
                _set_path(spec, path[:-1], copy.deepcopy(_get_path(chart["base"], path[:-1])))
                _set_path(spec, path, value)
    for channel, attr in (("x", "xLabel"), ("y", "yLabel")):
        old = before.get(channel)
        if channel in updates and old and spec.get(attr) == title_case(old):
            if updates[channel] and title_case(updates[channel]) != updates[channel]:
                spec[attr] = title_case(updates[channel])
            else:
                spec.pop(attr, None)
    return json.dumps(spec, indent=2) + "\n"


# ------------------------------------------------------------------ validation


def _issue(level: str, kind: str, message: str, node: Optional[str] = None) -> dict:
    out = {"level": level, "kind": kind, "message": message}
    if node:
        out["node"] = node
    return out


_JSON_TYPES = {"string": (str,), "number": (int, float), "boolean": (bool,), "array": (list,),
               "object": (dict,)}


def _type_ok(value, declared: str) -> bool:
    if declared == "number" and isinstance(value, bool):
        return False
    return isinstance(value, _JSON_TYPES.get(declared, (object,)))


def _config_issues(schema: Schema, node: dict, strict: bool, prior: Optional[dict]) -> list:
    """The portal resets a config to `{}` when a declared primitive has the wrong type;
    for a node this change set wrote, anything short of the schema is refused -- except a
    field it carried over unchanged from `prior`, which is the portal's to vouch for."""
    config = node.get("config")
    issues = []
    if config is None:
        return issues
    if not isinstance(config, dict):
        return [_issue("error", "config",
                       f"config must be an object, not {type(config).__name__}; "
                       "the portal would reset it to {}", node["id"])]
    props = schema.config_properties(node["type"])
    if props is None:
        return issues
    prior = prior if isinstance(prior, dict) else {}
    for key, value in config.items():
        kept = key in prior and prior[key] == value
        level = "error" if strict and not kept else "warning"
        if key not in props:
            if level == "error":
                issues.append(_issue("error", "config",
                                     f"{node['type']} has no config field {key!r}; "
                                     f"known fields: {sorted(props)}", node["id"]))
            continue
        declared = props[key].get("type")
        if value is None:
            continue
        if isinstance(declared, str) and not _type_ok(value, declared):
            reset = (" -- the portal resets this node's whole config to {}"
                     if declared in ("string", "number", "boolean") else "")
            issues.append(_issue(
                "error" if reset else level, "config",
                f"{key} must be a {declared}, not {type(value).__name__}{reset}", node["id"]))
            continue
        if "enum" in props[key] and value not in props[key]["enum"]:
            issues.append(_issue(level, "config",
                                 f"{key}={value!r} is not one of {props[key]['enum']}",
                                 node["id"]))
        item_type = (props[key].get("items") or {}).get("type")
        if declared == "array" and isinstance(item_type, str):
            if any(not _type_ok(item, item_type) for item in value):
                issues.append(_issue(level, "config",
                                     f"every item of {key} must be a {item_type}", node["id"]))
    return issues


def _can_connect(output: dict, input_: dict) -> Optional[str]:
    if output["type"] != input_["type"]:
        return f"{output['type']} cannot feed {input_['type']}"
    if output["type"] == "ColumnRef":
        offered = (output.get("accepts") or {}).get("columnClasses")
        needed = (input_.get("accepts") or {}).get("columnClasses")
        if offered and needed and not set(offered) & set(needed):
            return f"column class {'/'.join(offered)} not accepted here"
    return None


def _has_cycle(document: dict) -> bool:
    adj = {n["id"]: [] for n in document["ir"]["nodes"]}
    for e in document["ir"]["edges"]:
        adj.setdefault(e["from"]["node"], []).append(e["to"]["node"])
    state = {}

    def visit(node_id):
        if state.get(node_id) == 1:
            return True
        if state.get(node_id) == 2:
            return False
        state[node_id] = 1
        if any(visit(n) for n in adj.get(node_id, [])):
            return True
        state[node_id] = 2
        return False

    return any(visit(n) for n in list(adj))


def _structure_issues(document) -> list:
    """What the portal's `fromPersisted` refuses to load at all."""
    if not isinstance(document, dict):
        return [_issue("error", "structure", "dashboardData must be an object")]
    ir = document.get("ir")
    if not (isinstance(ir, dict) and isinstance(ir.get("nodes"), list)
            and isinstance(ir.get("edges"), list)):
        return [_issue("error", "structure", "ir must hold nodes and edges lists")]
    if not all(isinstance(n, dict) and isinstance(n.get("id"), str)
               and isinstance(n.get("type"), str) for n in ir["nodes"]):
        return [_issue("error", "structure", "every node needs a string id and type")]
    for e in ir["edges"]:
        if not all(isinstance(e.get(end), dict) and isinstance(e[end].get("node"), str)
                   and isinstance(e[end].get("port"), str) for end in ("from", "to")):
            return [_issue("error", "structure", "every edge needs from/to {node, port}")]
    pages = document.get("dashboards", [])
    if not (isinstance(pages, list)
            and all(isinstance(p, dict) and isinstance(p.get("id"), str)
                    and isinstance(p.get("tiles"), list)
                    and all(isinstance(t, dict) and isinstance(t.get("componentId"), str)
                            for t in p["tiles"]) for p in pages)):
        return [_issue("error", "structure", "dashboards must be pages with tile lists")]
    return []


def validate(schema: Schema, document, *, strict_nodes=None, before=None) -> list:
    """Every problem with a document, as issues.

    `strict_nodes` are the ids a change set wrote, and `before` the document it started
    from. Only what the change wrote can be refused: a problem on any other node -- written
    by the portal, possibly by a newer build than this schema describes -- is reported as a
    warning, since refusing it would block every edit to that dashboard. Problems with the
    document as a whole (structure, cycles, size) stay errors. None means all nodes.
    """
    issues = _structure_issues(document)
    if issues:
        return issues
    issues = _node_issues(schema, document, strict_nodes,
                          _node_index(before) if before else {})
    return issues if strict_nodes is None else only_affected(issues, set(strict_nodes))


def _node_issues(schema: Schema, document: dict, strict_nodes, prior: dict) -> list:
    issues = []
    if document.get("version") != schema.persist_version:
        issues.append(_issue("error", "structure",
                             f"version {document.get('version')!r} is not the portal's "
                             f"{schema.persist_version}"))
    nodes = _node_index(document)
    if len(nodes) != len(document["ir"]["nodes"]):
        issues.append(_issue("error", "structure", "node ids must be unique"))
    for node in document["ir"]["nodes"]:
        strict = strict_nodes is None or node["id"] in strict_nodes
        if not schema.cell_id.match(node["id"]):
            issues.append(_issue(
                "error", "node-id",
                f"node id {node['id']!r} must match {schema.raw['cellIdPattern']}",
                node["id"]))
        if node["type"] not in schema.node_types:
            issues.append(_issue("error", "unknown-node-type",
                                 f"unknown node type {node['type']!r}", node["id"]))
            continue
        issues.extend(_config_issues(schema, node, strict,
                                     (prior.get(node["id"]) or {}).get("config")))
    incoming = {}
    for edge in document["ir"]["edges"]:
        src, dst = nodes.get(edge["from"]["node"]), nodes.get(edge["to"]["node"])
        label = (f"{edge['from']['node']}.{edge['from']['port']} -> "
                 f"{edge['to']['node']}.{edge['to']['port']}")
        if not src or not dst:
            issues.append(_issue("error", "unknown-node",
                                 f"edge {label} references a missing node"))
            continue
        if src["type"] not in schema.node_types or dst["type"] not in schema.node_types:
            continue
        out_port = next((p for p in schema.ports(src)[1] if p["id"] == edge["from"]["port"]),
                        None)
        in_port = next((p for p in schema.ports(dst)[0] if p["id"] == edge["to"]["port"]),
                       None)
        if not out_port or not in_port:
            issues.append(_issue("error", "unknown-port",
                                 f"edge {label} references a port that does not exist",
                                 dst["id"]))
            continue
        mismatch = _can_connect(out_port, in_port)
        if mismatch:
            issues.append(_issue("error", "type-mismatch", f"edge {label}: {mismatch}",
                                 dst["id"]))
        if in_port["cardinality"] == "one":
            key = (dst["id"], in_port["id"])
            incoming[key] = incoming.get(key, 0) + 1
            if incoming[key] == 2:
                issues.append(_issue("error", "cardinality",
                                     f"{dst['id']}.{in_port['id']} accepts only one edge",
                                     dst["id"]))
    wired = {(e["to"]["node"], e["to"]["port"]) for e in document["ir"]["edges"]}
    for node in document["ir"]["nodes"]:
        for port in schema.ports(node)[0]:
            if (port["required"] and port["type"] == "Relation"
                    and (node["id"], port["id"]) not in wired):
                issues.append(_issue(
                    "warning", "missing-required",
                    f"{node['type']} {node['id']} has nothing wired into {port['id']}",
                    node["id"]))
    feeding = {e["from"]["node"] for e in document["ir"]["edges"]}
    for node in document["ir"]["nodes"]:
        if (node["type"] in schema.node_types and not schema.renderable(node["type"])
                and node["type"] != "input" and node["id"] not in feeding):
            issues.append(_issue("warning", "unused",
                                 f"{node['type']} {node['id']} feeds nothing on the dashboard",
                                 node["id"]))
    if _has_cycle(document):
        issues.append(_issue("error", "cycle", "the graph contains a cycle"))
    issues.extend(_view_issues(schema, document))
    issues.extend(_tile_issues(schema, document, nodes))
    size = len(json.dumps(document))
    if size > MAX_BLOB_BYTES:
        issues.append(_issue(
            "error", "size",
            f"the document is {size:,} bytes; the record holds at most {MAX_BLOB_BYTES:,}"))
    return issues


def _view_issues(schema: Schema, document: dict) -> list:
    issues = []
    wired = {(e["to"]["node"], e["to"]["port"]) for e in document["ir"]["edges"]}
    for node in document["ir"]["nodes"]:
        if node["type"] != "chart":
            continue
        config = node.get("config") or {}
        view_type = config.get("viewType") or "table"
        if view_type == "table" or view_type not in schema.charts:
            continue
        spec = config.get("spec")
        if not spec:
            issues.append(_issue("error", "chart",
                                 f"{view_type} view has no spec, so it draws nothing",
                                 node["id"]))
            continue
        try:
            json.loads(spec)
        except (TypeError, ValueError):
            issues.append(_issue("error", "chart", "spec is not valid JSON", node["id"]))
            continue
        if config.get("specRawEdited"):
            # Hand-edited in the portal's spec editor: where a channel lives is its
            # author's choice now, not the catalog's.
            continue
        bound = read_bindings(schema, view_type, spec)
        for ch in schema.charts[view_type]["channels"]:
            if (ch["required"] and ch["channel"] not in bound
                    and (node["id"], f"channel:{ch['channel']}") not in wired):
                issues.append(_issue("error", "chart",
                                     f"{view_type} needs a column on {ch['channel']}",
                                     node["id"]))
    return issues


def _tile_issues(schema: Schema, document: dict, nodes: dict) -> list:
    issues = []
    pages = document.get("dashboards") or []
    active = document.get("activeDashboardId")
    if pages and active not in {p["id"] for p in pages}:
        issues.append(_issue(
            "warning", "tile",
            f"activeDashboardId {active!r} names no page; the first page opens"))
    cols = schema.grid["columns"]
    for page in pages:
        for tile in page["tiles"]:
            node = nodes.get(tile["componentId"])
            where = f"page {page['id']!r}"
            if node is None:
                issues.append(_issue(
                    "error", "tile",
                    f"a tile on {where} shows {tile['componentId']!r}, which is not a node"))
                continue
            if node["type"] in schema.node_types and not schema.renderable(node["type"]):
                issues.append(_issue("error", "tile", f"{node['type']} nodes cannot be tiles",
                                     node["id"]))
            dims = [tile.get(k) for k in ("x", "y", "w", "h")]
            if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                       for v in dims):
                issues.append(_issue(
                    "error", "tile",
                    f"tile {tile['componentId']} needs integer x, y, w, h >= 0", node["id"]))
            elif tile["w"] < 1 or tile["h"] < 1 or tile["x"] + tile["w"] > cols:
                issues.append(_issue(
                    "error", "tile",
                    f"tile {tile['componentId']} must fit the {cols}-column grid "
                    "with w, h >= 1", node["id"]))
    placed = {t["componentId"] for p in pages for t in p["tiles"]}
    unplaced = [n["id"] for n in nodes.values() if schema.renderable(n["type"])
                and n["type"] != "discussion" and n["id"] not in placed]
    if unplaced:
        issues.append(_issue("warning", "tile",
                             f"not on any page, so never shown: {unplaced}"))
    return issues


def downstream(document: dict, node_ids) -> set:
    """`node_ids` and everything that reads from them, transitively."""
    found, frontier = set(node_ids), list(node_ids)
    while frontier:
        current = frontier.pop()
        for e in document["ir"]["edges"]:
            if e["from"]["node"] == current and e["to"]["node"] not in found:
                found.add(e["to"]["node"])
                frontier.append(e["to"]["node"])
    return found


def only_affected(issues: list, affected: set) -> list:
    """Errors on nodes outside `affected` downgraded to warnings: what a change did not
    touch, it did not break."""
    return [{**i, "level": "warning"}
            if i["level"] == "error" and i.get("node") and i["node"] not in affected else i
            for i in issues]


def errors_only(issues: list) -> list:
    return [i for i in issues if i["level"] == "error"]


# ------------------------------------------------------------------ changes

# Every key each change may carry, so a typo is refused rather than dropped.
CHANGE_KEYS = {"add_source": ("id", "dataset", "path", "sheet"),
               "add_query": ("id", "inputs", "sql"),
               "add_view": ("id", "data", "type", "title", "x", "y", "color", "size", "detail",
                            "options", "page", "w", "h"),
               "add_control": ("id", "kind", "label", "target", "options", "options_from",
                               "value", "values", "lo", "hi"),
               "update_query": ("node", "sql"),
               "update_view": ("node", "type", "title", "x", "y", "color", "size", "detail",
                               "options", "data"),
               "update_control": ("node", "label", "value", "values", "lo", "hi", "options"),
               "set_config": ("node", "config"), "add_node": ("id", "type", "config"),
               "remove": ("node",), "connect": ("from", "to"), "disconnect": ("from", "to"),
               "place": ("node", "page", "x", "y", "w", "h"), "unplace": ("node", "page"),
               "add_page": ("id", "name"), "rename_page": ("page", "name"),
               "remove_page": ("page",)}


def change_grammar() -> dict:
    """The change vocabulary, for the schema read -- the keys, not the prose."""
    return {op: list(keys) for op, keys in CHANGE_KEYS.items()}


def source_ref(change: dict) -> tuple:
    """What an add_source names, for the planner to resolve: ("file", dataset, path)
    or ("sheet", sheet). Raises if it names neither or both."""
    has_file = change.get("dataset") is not None or change.get("path") is not None
    if (has_file and change.get("sheet") is not None
            or not has_file and change.get("sheet") is None):
        raise ValueError("add_source takes either dataset + path, or sheet")
    if has_file:
        if not change.get("dataset") or not change.get("path"):
            raise ValueError("a file source needs both dataset and path")
        return ("file", change["dataset"], change["path"])
    return ("sheet", change["sheet"])


class Builder:
    """Applies a change list to a copy of a document.

    Node ids are deterministic -- the given `id`, else derived from the change and its
    position -- because apply re-plans the same request and refuses if the result
    differs: a random id would make every plan stale.
    """

    def __init__(self, schema: Schema, document: dict, resolve_source: Callable):
        self.schema = schema
        self.doc = copy.deepcopy(document)
        self.resolve_source = resolve_source
        self.touched = set()
        # Tiles added without coordinates, placed together at the end in change order.
        self._auto_tiles = []

    # -------------------------------------------------------------- plumbing

    @property
    def nodes(self) -> dict:
        return _node_index(self.doc)

    def _node(self, index: int, node_id, *, types=None) -> dict:
        node = self.nodes.get(node_id)
        if node is None:
            raise ChangeError(index, f"no node {node_id!r}; nodes are {sorted(self.nodes)}",
                              field="node")
        if types and node["type"] not in types:
            raise ChangeError(
                index, f"{node_id!r} is a {node['type']} node, not {' or '.join(types)}",
                field="node")
        return node

    def _new_id(self, index: int, change: dict) -> str:
        given = change.get("id")
        if given is not None:
            if not isinstance(given, str) or not self.schema.cell_id.match(given):
                raise ChangeError(
                    index,
                    f"id {given!r} must match {self.schema.raw['cellIdPattern']} "
                    "(4-32 lowercase letters and digits)", field="id")
            if given in self.nodes:
                raise ChangeError(index, f"id {given!r} is already a node", field="id")
            return given
        digest = hashlib.sha1(
            f"{index}:{json.dumps(change, sort_keys=True)}".encode()).digest()
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        candidate = "".join(alphabet[b % 36] for b in digest[:8])
        while candidate in self.nodes:
            candidate = candidate[1:] + alphabet[index % 36]
        return candidate

    def _add_node(self, node_id: str, node_type: str, config: dict) -> dict:
        node = {"id": node_id, "type": node_type, "config": config}
        self.doc["ir"]["nodes"].append(node)
        self.touched.add(node_id)
        return node

    def _connect(self, index: int, src: str, src_port: str, dst: str, dst_port: str) -> None:
        edge = {"from": {"node": src, "port": src_port}, "to": {"node": dst, "port": dst_port}}
        if edge in self.doc["ir"]["edges"]:
            raise ChangeError(index, f"{src}.{src_port} is already wired to {dst}.{dst_port}")
        self.doc["ir"]["edges"].append(edge)
        self.touched.add(dst)

    def _relation_source(self, index: int, node_id, field: str) -> dict:
        node = self.nodes.get(node_id)
        if node is None:
            raise ChangeError(
                index,
                f"no node {node_id!r} to read data from; nodes are {sorted(self.nodes)}",
                field=field)
        if not any(p["type"] == "Relation" for p in self.schema.ports(node)[1]):
            raise ChangeError(index, f"{node_id!r} ({node['type']}) produces no table",
                              field=field)
        return node

    def _page_for(self, index: int, page_id) -> dict:
        page = _page(self.doc, page_id)
        if page is None:
            if page_id is not None:
                raise ChangeError(index,
                                  f"no page {page_id!r}; pages are "
                                  f"{[p['id'] for p in self.doc['dashboards']]}", field="page")
            page = {"id": "default", "name": "Default", "tiles": []}
            self.doc["dashboards"].append(page)
            self.doc["activeDashboardId"] = page["id"]
        return page

    def _tile(self, index: int, node_id: str, change: dict) -> None:
        page = self._page_for(index, change.get("page"))
        if any(t["componentId"] == node_id for t in page["tiles"]):
            raise ChangeError(
                index, f"{node_id!r} is already on page {page['id']!r}; use place to move it")
        w = change.get("w", self.schema.grid["tileWidth"])
        if not isinstance(w, int) or not 1 <= w <= self.schema.grid["columns"]:
            raise ChangeError(index, f"w must be 1-{self.schema.grid['columns']}", field="w")
        tile = {"componentId": node_id, "x": 0, "y": 0, "w": w, "h": change.get("h")}
        page["tiles"].append(tile)
        self._auto_tiles.append((page["id"], tile))

    def _place_auto_tiles(self) -> None:
        """Skyline-pack new tiles below and beside what is already on each page, in the
        order they were added -- existing tiles never move."""
        cols = self.schema.grid["columns"]
        for page in self.doc["dashboards"]:
            new = [t for pid, t in self._auto_tiles if pid == page["id"]]
            if not new:
                continue
            fixed = [t for t in page["tiles"] if not any(t is n for n in new)]
            alone = len(page["tiles"]) == 1
            heights = [0] * cols
            for t in fixed:
                for x in range(t["x"], min(cols, t["x"] + t["w"])):
                    heights[x] = max(heights[x], t["y"] + t["h"])
            for t in new:
                if t["h"] is None:
                    t["h"] = (self.schema.grid["soloTileHeight"] if alone
                              else self.schema.grid["tileHeight"])
                best_x, best_y = 0, None
                for x in range(0, cols - t["w"] + 1):
                    y = max(heights[x:x + t["w"]])
                    if best_y is None or y < best_y:
                        best_x, best_y = x, y
                t["x"], t["y"] = best_x, best_y
                for x in range(best_x, best_x + t["w"]):
                    heights[x] = best_y + t["h"]

    # -------------------------------------------------------------- entry point

    def apply(self, changes) -> dict:
        if not isinstance(changes, list):
            raise ChangeError(-1, "changes must be a list of {op: ...} objects",
                              field="changes")
        for index, change in enumerate(changes):
            if not isinstance(change, dict) or change.get("op") not in CHANGE_KEYS:
                raise ChangeError(index,
                                  f"each change needs an op, one of {sorted(CHANGE_KEYS)}",
                                  field="op")
            unknown = sorted(set(change) - {"op"} - set(CHANGE_KEYS[change["op"]]))
            if unknown:
                raise ChangeError(index,
                                  f"{change['op']} does not take {unknown}; it takes "
                                  f"{list(CHANGE_KEYS[change['op']])}", field=unknown[0])
            getattr(self, f"_op_{change['op']}")(index, change)
        self._place_auto_tiles()
        return self.doc

    # -------------------------------------------------------------- sources and SQL

    def _op_add_source(self, index: int, change: dict) -> None:
        try:
            node_type, config = self.resolve_source(change)
        except ValueError as exc:
            raise ChangeError(index, str(exc),
                              field="dataset" if change.get("dataset") else "sheet")
        for node in self.nodes.values():
            c = node.get("config") or {}
            same_file = (node_type == "dataset-file" and node["type"] == node_type
                         and (c.get("datasetId"), c.get("path"))
                         == (config["datasetId"], config["path"]))
            same_sheet = (node_type == "dataset-source" and node["type"] == node_type
                          and c.get("sheetId") == config["sheetId"])
            if same_file or same_sheet:
                raise ChangeError(
                    index, f"that source is already node {node['id']!r}; read from it instead")
        self._add_node(self._new_id(index, change), node_type, config)

    def _op_add_query(self, index: int, change: dict) -> None:
        inputs = change.get("inputs") or []
        if not isinstance(inputs, list) or not inputs:
            raise ChangeError(index, "add_query needs inputs: the node ids it reads",
                              field="inputs")
        sources = [self._relation_source(index, i, "inputs") for i in inputs]
        sql = change.get("sql")
        if sql is None:
            name = ((sources[0].get("config") or {}).get("tableName") if len(sources) == 1
                    else None)
            sql = f"SELECT * FROM {name if name and _IDENTIFIER.fullmatch(name) else 'query'}"
        if not isinstance(sql, str) or not sql.strip():
            raise ChangeError(index, "sql must be a non-empty string", field="sql")
        node = self._add_node(self._new_id(index, change), "sql-query", {"sql": sql})
        for src in sources:
            self._connect(index, src["id"], "relation", node["id"], "upstream")

    def _op_update_query(self, index: int, change: dict) -> None:
        node = self._node(index, change.get("node"), types=("sql-query", "namespace-query"))
        sql = change.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise ChangeError(index, "sql must be a non-empty string", field="sql")
        stale = (set(self.schema.tokens(node["config"].get("sql")))
                 - set(self.schema.tokens(sql)))
        wired = [e for e in self.doc["ir"]["edges"] if e["to"]["node"] == node["id"]
                 and e["to"]["port"] in {f"param:{t}" for t in stale}]
        if wired:
            raise ChangeError(
                index,
                f"the new SQL drops {{{', '.join(sorted(stale))}}}, which a control "
                "still drives; remove or retarget that control first", field="sql")
        node["config"] = {k: v for k, v in {**node["config"], "sql": sql}.items()
                          if k != "defaultLimit"}
        self.touched.add(node["id"])

    # -------------------------------------------------------------- views

    def _bindings(self, index: int, chart_id: str, change: dict, current: dict,
                  *, require: bool = True) -> dict:
        channels = {ch["channel"]: ch for ch in self.schema.charts[chart_id]["channels"]}
        bindings = {k: v for k, v in current.items() if k in channels}
        for name in ("x", "y", "color", "size", "detail"):
            if name not in change:
                continue
            if name not in channels:
                raise ChangeError(
                    index, f"{chart_id} has no {name} channel; it has {sorted(channels)}",
                    field=name)
            if change[name] is None:
                bindings.pop(name, None)
            elif not isinstance(change[name], str) or not change[name]:
                raise ChangeError(index, f"{name} must name a column", field=name)
            else:
                bindings[name] = change[name]
        missing = [c for c, ch in channels.items() if ch["required"] and c not in bindings]
        if missing and require:
            raise ChangeError(index, f"{chart_id} needs a column for {missing}",
                              field=missing[0])
        return bindings

    def _options(self, index: int, options) -> dict:
        if options is None:
            return {}
        props = self.schema.config_properties("chart") or {}
        if not isinstance(options, dict):
            raise ChangeError(index, "options must be an object", field="options")
        bad = sorted(k for k in options if k not in props or k in _VIEW_MANAGED)
        if bad:
            allowed = sorted(k for k in props if k not in _VIEW_MANAGED)
            raise ChangeError(index, f"options cannot set {bad}; they may set {allowed}",
                              field="options")
        return dict(options)

    def _view_type(self, index: int, value) -> str:
        view_type = value or "table"
        if view_type != "table" and view_type not in self.schema.charts:
            raise ChangeError(index,
                              f"type {view_type!r} is not a chart; use table or one of "
                              f"{sorted(self.schema.charts)}", field="type")
        return view_type

    def _op_add_view(self, index: int, change: dict) -> None:
        data = self._relation_source(index, change.get("data"), "data")
        view_type = self._view_type(index, change.get("type"))
        config = {"viewType": view_type, **self._options(index, change.get("options"))}
        if change.get("title"):
            config["title"] = change["title"]
        if view_type == "table":
            given = [c for c in ("x", "y", "color", "size", "detail") if c in change]
            if given:
                raise ChangeError(index, f"a table view takes no channels, got {given}",
                                  field=given[0])
        else:
            config["spec"] = build_spec(self.schema, view_type,
                                        self._bindings(index, view_type, change, {}))
        node = self._add_node(self._new_id(index, change), "chart", config)
        self._connect(index, data["id"], "relation", node["id"], "data")
        self._tile(index, node["id"], change)

    def _op_update_view(self, index: int, change: dict) -> None:
        node = self._node(index, change.get("node"), types=("chart",))
        config = dict(node.get("config") or {})
        old_type = config.get("viewType") or "table"
        view_type = self._view_type(index, change.get("type", old_type))
        current = (read_bindings(self.schema, old_type, config.get("spec"))
                   if old_type != "table" else {})
        channels = [c for c in ("x", "y", "color", "size", "detail") if c in change]
        if view_type == "table":
            if channels:
                raise ChangeError(index, f"a table view takes no channels, got {channels}",
                                  field=channels[0])
            config.pop("spec", None)
            config.pop("specRawEdited", None)
        elif view_type != old_type:
            # A new chart type regenerates the spec, as the portal's type picker does.
            config["spec"] = build_spec(self.schema, view_type,
                                        self._bindings(index, view_type, change, current))
            config.pop("specRawEdited", None)
        elif channels:
            # Same type: patch the channels in place, so hand edits and every other key
            # in the spec survive. A hand-edited spec may keep a channel off the catalog's
            # path, so it is not held to the catalog's required channels.
            self._bindings(index, view_type, change, current,
                           require=not config.get("specRawEdited"))
            config["spec"] = patch_spec(self.schema, view_type, config.get("spec"),
                                        {c: change[c] for c in channels})
        config["viewType"] = view_type
        if "title" in change:
            if change["title"]:
                config["title"] = change["title"]
            else:
                config.pop("title", None)
        for key, value in self._options(index, change.get("options")).items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        if "data" in change:
            data = self._relation_source(index, change["data"], "data")
            self.doc["ir"]["edges"] = [e for e in self.doc["ir"]["edges"]
                                       if not (e["to"]["node"] == node["id"]
                                               and e["to"]["port"] == "data")]
            self._connect(index, data["id"], "relation", node["id"], "data")
        node["config"] = config
        self.touched.add(node["id"])

    # -------------------------------------------------------------- controls

    def _op_add_control(self, index: int, change: dict) -> None:
        kind = change.get("kind")
        if kind not in CONTROL_KINDS:
            raise ChangeError(index, f"kind must be one of {list(CONTROL_KINDS)}",
                              field="kind")
        target = change.get("target")
        if (not isinstance(target, dict)
                or bool(target.get("query")) == bool(target.get("view"))):
            raise ChangeError(
                index,
                "target is {query: <sql node>, token: <name>} to fill a {token} "
                "in its SQL, or {view: <view node>, column: <name>} to filter a view's rows",
                field="target")
        config = {"inputType": kind,
                  "label": change.get("label") or target.get("token") or target.get("column")}
        if target.get("query"):
            query = self._node(index, target["query"], types=("sql-query",))
            token = target.get("token")
            tokens = self.schema.tokens(query["config"].get("sql"))
            if token not in tokens:
                raise ChangeError(
                    index,
                    f"{query['id']}'s SQL has no {{{token}}}; it has "
                    f"{['{'+t+'}' for t in tokens] or 'none -- add one with update_query'}",
                    field="target")
            dst, dst_port = query["id"], f"param:{token}"
        else:
            view = self._node(index, target["view"], types=("chart",))
            if not target.get("column"):
                raise ChangeError(index, "a view filter needs target.column", field="target")
            config["column"] = target["column"]
            if (kind in ("select", "multiselect", "text") and change.get("options") is None
                    and not change.get("options_from")):
                config["optionsColumn"] = target["column"]
            dst, dst_port = view["id"], "filter"
        self._control_values(index, kind, change, config)
        node = self._add_node(self._new_id(index, change), "input", config)
        if change.get("options_from"):
            source = change["options_from"]
            if (not isinstance(source, dict) or not source.get("data")
                    or not source.get("column")):
                raise ChangeError(index, "options_from is {data: <node>, column: <name>}",
                                  field="options_from")
            self._relation_source(index, source["data"], "options_from")
            config["optionsColumn"] = source["column"]
            self._connect(index, source["data"], "relation", node["id"], "data")
        self._connect(index, node["id"], "value", dst, dst_port)

    def _control_values(self, index: int, kind: str, change: dict, config: dict) -> None:
        if change.get("options") is not None:
            if not isinstance(change["options"], list):
                raise ChangeError(index, "options must be a list", field="options")
            config["options"] = [str(o) for o in change["options"]]
        if kind == "multiselect":
            if change.get("values") is not None:
                config["values"] = [str(v) for v in change["values"]]
        elif change.get("value") is not None:
            config["value"] = str(change["value"])
        elif kind != "range":
            config.setdefault("value", "")
        for bound in ("lo", "hi"):
            if change.get(bound) is not None:
                if (kind != "range" or not isinstance(change[bound], (int, float))
                        or isinstance(change[bound], bool)):
                    raise ChangeError(index,
                                      f"{bound} is a number, and only for a range control",
                                      field=bound)
                config[bound] = change[bound]

    def _op_update_control(self, index: int, change: dict) -> None:
        node = self._node(index, change.get("node"), types=("input",))
        config = dict(node.get("config") or {})
        if change.get("label"):
            config["label"] = change["label"]
        self._control_values(index, config.get("inputType", "text"), change, config)
        node["config"] = config
        self.touched.add(node["id"])

    # -------------------------------------------------------------- raw graph

    def _op_set_config(self, index: int, change: dict) -> None:
        node = self._node(index, change.get("node"))
        if not isinstance(change.get("config"), dict):
            raise ChangeError(index, "config must be an object; null values delete keys",
                              field="config")
        merged = {**(node.get("config") or {}), **change["config"]}
        node["config"] = {k: v for k, v in merged.items() if v is not None}
        self.touched.add(node["id"])

    def _op_add_node(self, index: int, change: dict) -> None:
        if change.get("type") not in self.schema.node_types:
            raise ChangeError(index, f"type must be one of {sorted(self.schema.node_types)}",
                              field="type")
        if change["type"] in ("dataset-file", "dataset-source"):
            raise ChangeError(index, "use add_source, which resolves the dataset or sheet",
                              field="type")
        config = change.get("config") or {}
        if not isinstance(config, dict):
            raise ChangeError(index, "config must be an object", field="config")
        node = self._add_node(self._new_id(index, change), change["type"], dict(config))
        if self.schema.renderable(node["type"]):
            self._tile(index, node["id"], {})

    def _op_remove(self, index: int, change: dict) -> None:
        node = self._node(index, change.get("node"))
        self.doc["ir"]["nodes"] = [n for n in self.doc["ir"]["nodes"] if n["id"] != node["id"]]
        self.doc["ir"]["edges"] = [e for e in self.doc["ir"]["edges"]
                                   if node["id"] not in (e["from"]["node"], e["to"]["node"])]
        for page in self.doc["dashboards"]:
            page["tiles"] = [t for t in page["tiles"] if t["componentId"] != node["id"]]
        self.doc["positions"].pop(node["id"], None)
        self.touched.discard(node["id"])

    def _endpoint(self, index: int, text, field: str) -> tuple:
        if not isinstance(text, str) or "." not in text:
            raise ChangeError(index, f"{field} is 'node.port'", field=field)
        node_id, port = text.split(".", 1)
        self._node(index, node_id)
        return node_id, port

    def _op_connect(self, index: int, change: dict) -> None:
        src, src_port = self._endpoint(index, change.get("from"), "from")
        dst, dst_port = self._endpoint(index, change.get("to"), "to")
        self._connect(index, src, src_port, dst, dst_port)

    def _op_disconnect(self, index: int, change: dict) -> None:
        src, src_port = self._endpoint(index, change.get("from"), "from")
        dst, dst_port = self._endpoint(index, change.get("to"), "to")
        edge = {"from": {"node": src, "port": src_port}, "to": {"node": dst, "port": dst_port}}
        if edge not in self.doc["ir"]["edges"]:
            raise ChangeError(index, f"{change['from']} is not wired to {change['to']}")
        self.doc["ir"]["edges"].remove(edge)
        self.touched.add(dst)

    # -------------------------------------------------------------- layout

    def _op_place(self, index: int, change: dict) -> None:
        node = self._node(index, change.get("node"))
        self.touched.add(node["id"])
        if not self.schema.renderable(node["type"]):
            raise ChangeError(index, f"{node['type']} nodes are not tiles", field="node")
        page = self._page_for(index, change.get("page"))
        tile = next((t for t in page["tiles"] if t["componentId"] == node["id"]), None)
        if tile is None:
            if "x" not in change or "y" not in change:
                self._tile(index, node["id"], change)
                return
            tile = {"componentId": node["id"], "x": 0, "y": 0,
                    "w": self.schema.grid["tileWidth"], "h": self.schema.grid["tileHeight"]}
            page["tiles"].append(tile)
        for key in ("x", "y", "w", "h"):
            if key in change:
                if (not isinstance(change[key], int) or isinstance(change[key], bool)
                        or change[key] < 0):
                    raise ChangeError(index, f"{key} must be a non-negative integer",
                                      field=key)
                tile[key] = change[key]

    def _op_unplace(self, index: int, change: dict) -> None:
        node = self._node(index, change.get("node"))
        page = self._page_for(index, change.get("page"))
        kept = [t for t in page["tiles"] if t["componentId"] != node["id"]]
        if len(kept) == len(page["tiles"]):
            raise ChangeError(index, f"{node['id']!r} is not on page {page['id']!r}")
        page["tiles"] = kept

    def _op_add_page(self, index: int, change: dict) -> None:
        name = change.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ChangeError(index, "a page needs a name", field="name")
        page_id = (change.get("id") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
                   or "page")
        if any(p["id"] == page_id for p in self.doc["dashboards"]):
            raise ChangeError(index, f"page {page_id!r} already exists", field="id")
        self.doc["dashboards"].append({"id": page_id, "name": name, "tiles": []})

    def _op_rename_page(self, index: int, change: dict) -> None:
        page = self._page_for(index, change.get("page"))
        if not isinstance(change.get("name"), str) or not change["name"].strip():
            raise ChangeError(index, "a page needs a name", field="name")
        page["name"] = change["name"]

    def _op_remove_page(self, index: int, change: dict) -> None:
        page = self._page_for(index, change.get("page"))
        if len(self.doc["dashboards"]) == 1:
            raise ChangeError(index, "a dashboard keeps at least one page", field="page")
        self.doc["dashboards"] = [p for p in self.doc["dashboards"] if p["id"] != page["id"]]
        if self.doc.get("activeDashboardId") == page["id"]:
            self.doc["activeDashboardId"] = self.doc["dashboards"][0]["id"]


# ------------------------------------------------------------------ reading


def _inputs_of(document: dict, node_id: str, port: Optional[str] = None) -> list:
    return [e["from"]["node"] for e in document["ir"]["edges"] if e["to"]["node"] == node_id
            and (port is None or e["to"]["port"] == port)]


def describe(schema: Schema, document: dict) -> dict:
    """The document as an agent reads it: what each node is, in its own terms, rather
    than the raw graph. `nodes` holds anything the named groups do not cover."""
    out = {"pages": [], "sources": [], "queries": [], "views": [], "controls": [], "nodes": []}
    for page in document.get("dashboards") or []:
        out["pages"].append({"id": page["id"], "name": page.get("name"),
                             "active": page["id"] == document.get("activeDashboardId"),
                             "tiles": [{"node": t["componentId"], "x": t.get("x"),
                                        "y": t.get("y"), "w": t.get("w"), "h": t.get("h")}
                                       for t in page["tiles"]]})
    for node in document["ir"]["nodes"]:
        config = node.get("config") or {}
        kind = node["type"]
        if kind == "dataset-file":
            out["sources"].append({"id": node["id"], "kind": "file",
                                   "dataset_id": config.get("datasetId"),
                                   "dataset": config.get("datasetName"),
                                   "path": config.get("path"),
                                   "table": config.get("tableName"),
                                   "file_type": config.get("fileType")})
        elif kind == "dataset-source":
            out["sources"].append({"id": node["id"], "kind": "sheet",
                                   "sheet_id": config.get("sheetId"),
                                   "table": config.get("tableName")})
        elif kind == "namespace-query":
            out["sources"].append({"id": node["id"], "kind": "server_query",
                                   "sql": config.get("sql"), "table": config.get("tableName")})
        elif kind in ("sql-query", "join"):
            # Its data inputs only: a control filling a {token} is listed under controls.
            relation_ports = {p["id"] for p in schema.ports(node)[0]
                              if p["type"] == "Relation"}
            inputs = [e["from"]["node"] for e in document["ir"]["edges"]
                      if e["to"]["node"] == node["id"] and e["to"]["port"] in relation_ports]
            out["queries"].append({"id": node["id"], "kind": kind, "inputs": inputs,
                                   "sql": config.get("sql"),
                                   "tokens": schema.tokens(config.get("sql"))})
        elif kind == "chart":
            view_type = config.get("viewType") or "table"
            extra = {k: v for k, v in config.items() if k not in _VIEW_MANAGED}
            view = {"id": node["id"], "type": view_type, "title": config.get("title"),
                    "data": next(iter(_inputs_of(document, node["id"], "data")), None),
                    "filters": _inputs_of(document, node["id"], "filter")}
            if view_type != "table":
                view["bindings"] = read_bindings(schema, view_type, config.get("spec"))
            if extra:
                view["options"] = extra
            out["views"].append(view)
        elif kind == "input":
            targets = [f"{e['to']['node']}.{e['to']['port']}" for e in document["ir"]["edges"]
                       if e["from"]["node"] == node["id"]]
            out["controls"].append({"id": node["id"], "kind": config.get("inputType", "text"),
                                    "label": config.get("label"),
                                    "column": config.get("column"), "drives": targets,
                                    **{k: config[k]
                                       for k in ("value", "values", "lo", "hi", "options")
                                       if k in config}})
        else:
            out["nodes"].append({"id": node["id"], "type": kind, "config": config,
                                 "inputs": _inputs_of(document, node["id"])})
    return out


def _line(kind: str, entry: dict) -> str:
    if kind == "sources":
        where = (f"{entry.get('dataset')}/{entry.get('path')}" if entry["kind"] == "file"
                 else (f"sheet {entry.get('sheet_id')}" if entry["kind"] == "sheet"
                       else "server query"))
        return f"source {entry['id']}: {where} (as {entry.get('table')})"
    if kind == "queries":
        sql = " ".join(str(entry.get("sql") or "").split())
        return (f"query {entry['id']} from {entry['inputs']}: "
                f"{sql[:160]}{'...' if len(sql) > 160 else ''}")
    if kind == "views":
        bound = ", ".join(f"{k}={v}" for k, v in (entry.get("bindings") or {}).items())
        title = f" {entry['title']!r}" if entry.get("title") else ""
        return (f"view {entry['id']}{title}: {entry['type']}"
                f"{f' ({bound})' if bound else ''} of {entry.get('data')}")
    if kind == "controls":
        state = ", ".join(f"{k}={entry[k]!r}" for k in ("value", "values", "lo", "hi")
                          if entry.get(k) not in (None, "", []))
        return (f"control {entry['id']} {entry.get('label')!r}: {entry['kind']}"
                f"{f' ({state})' if state else ''} -> {', '.join(entry['drives'])}")
    return f"{entry['type']} {entry['id']}"


def render_changes(schema: Schema, before: Optional[dict], after: dict) -> list:
    """Line-per-change text for an approval: what is added, changed and removed, in
    the vocabulary of `describe`, plus where each new tile lands."""
    old = (describe(schema, before) if before
           else {k: [] for k in ("sources", "queries", "views", "controls", "nodes", "pages")})
    new = describe(schema, after)
    lines = []
    for kind in ("sources", "queries", "views", "controls", "nodes"):
        was = {e["id"]: e for e in old[kind]}
        now = {e["id"]: e for e in new[kind]}
        for node_id, entry in now.items():
            if node_id not in was:
                lines.append(f"  + {_line(kind, entry)}")
            elif entry != was[node_id]:
                lines.append(f"  ~ {_line(kind, entry)}")
        lines.extend(f"  - {_line(kind, entry)}" for node_id, entry in was.items()
                     if node_id not in now)
    old_tiles = {(p["id"], t["node"]): t for p in old["pages"] for t in p["tiles"]}
    for page in new["pages"]:
        for tile in page["tiles"]:
            before_tile = old_tiles.pop((page["id"], tile["node"]), None)
            if before_tile != tile:
                lines.append(
                    f"  {'+' if before_tile is None else '~'} tile {tile['node']} on page "
                    f"{page.get('name')!r} at x={tile['x']} y={tile['y']} "
                    f"w={tile['w']} h={tile['h']}")
    lines.extend(f"  - tile {node} from page {page_id!r}" for (page_id, node) in old_tiles)
    old_pages = {p["id"]: p["name"] for p in old["pages"]}
    for page in new["pages"]:
        if page["id"] not in old_pages:
            lines.append(f"  + page {page['name']!r}")
        elif old_pages[page["id"]] != page["name"]:
            lines.append(f"  ~ page {page['id']!r} renamed {page['name']!r}")
    lines.extend(f"  - page {name!r}" for pid, name in old_pages.items()
                 if pid not in {p["id"] for p in new["pages"]})
    return lines

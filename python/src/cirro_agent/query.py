"""Client-side filtering, faceting and aggregation.

Cirro has no server-side filtering: `GET /datasets` accepts only limit and
next_token. Every query therefore already paid for a full fetch, which inverts
the usual economics -- rows are the expensive thing (tokens) while facets over
the complete match set and exact counts for a hypothetical follow-up filter are
free. That is why this module leans so hard on faceting and on `suggest`.

Everything here operates on plain JSON-safe row dicts, so it never imports cirro
and is testable without a tenant.
"""
import fnmatch
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import errors
from .envelope import Facet, estimate_tokens
from .schema import EntitySpec

# Status classes. An agent asks "what is broken", not "what is SUSPENDED".
STATUS_CLASSES = {
    "@active": ["PENDING", "STARTING", "RUNNING", "STOPPING", "DELETING"],
    "@terminal": ["COMPLETED", "FAILED", "ARCHIVED", "DELETED"],
    "@bad": ["FAILED", "SUSPENDED", "UNKNOWN"],
}

_SIZE_UNITS = {
    "B": 1,
    "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12, "PB": 10**15,
    "KIB": 2**10, "MIB": 2**20, "GIB": 2**30, "TIB": 2**40, "PIB": 2**50,
}
_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$")
_REL_DATE_RE = re.compile(r"^last-(\d+)([dwmy])$", re.I)
_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$", re.I)
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")

_MISSING = object()

OPS = {
    "eq", "ne", "in", "nin", "glob", "regex", "contains", "gt", "gte", "lt", "lte",
    "within", "between", "exists", "has_tag", "has_all_tags", "has_folder",
    "ancestor_of", "descendant_of",
}


class Ctx:
    """Everything resolution needs that is not in the query itself."""

    def __init__(self, username: Optional[str] = None, now: Optional[datetime] = None,
                 lineage: Optional[dict] = None):
        self.username = username
        self.now = now or datetime.now(timezone.utc)
        # dataset_id -> [parent_dataset_id, ...]
        self.lineage = lineage or {}
        self.resolved: dict = {}


# --------------------------------------------------------------------------- values


def parse_size(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    match = _SIZE_RE.match(str(value))
    if not match:
        raise errors.InvalidQuery(
            f"Cannot read {value!r} as a size.",
            what="Use a number of bytes, or a number with a unit.",
            given=value,
            retry_with=[{"value": "10GB"}, {"value": "10GiB"}, {"value": 10_000_000_000}],
        )
    number, unit = match.group(1), (match.group(2) or "B").upper()
    if unit not in _SIZE_UNITS:
        raise errors.InvalidQuery(
            f"Unknown size unit {unit!r}.",
            what="Use one of the supported units.",
            given=value,
            retry_with=[{"value": f"10{u}"} for u in ("KB", "MB", "GB", "TB", "GiB")],
        )
    return int(float(number) * _SIZE_UNITS[unit])


def parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise errors.InvalidQuery(
            f"Cannot read {value!r} as a timestamp.",
            what="Use an ISO-8601 timestamp.",
            given=value,
            retry_with=[{"value": "2026-08-01"}, {"value": "2026-08-01T00:00:00Z"}],
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def resolve_interval(value: Any, ctx: Ctx) -> tuple:
    """Turn any date expression into an explicit half-open UTC interval.

    Always echoed back in query.resolved: "last 7 days" must never stay ambiguous
    in a transcript that a human reads later.
    """
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return parse_dt(value[0]), parse_dt(value[1])

    text = str(value).strip()
    now = ctx.now
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if text.lower() == "today":
        return midnight, midnight + timedelta(days=1)
    if text.lower() == "yesterday":
        return midnight - timedelta(days=1), midnight

    match = _REL_DATE_RE.match(text)
    if match:
        count, unit = int(match.group(1)), match.group(2).lower()
        days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit] * count
        return midnight - timedelta(days=days - 1), midnight + timedelta(days=1)

    match = _QUARTER_RE.match(text)
    if match:
        year, quarter = int(match.group(1)), int(match.group(2))
        start_month = (quarter - 1) * 3 + 1
        start = datetime(year, start_month, 1, tzinfo=timezone.utc)
        end_year, end_month = (year + 1, 1) if start_month == 10 else (year, start_month + 3)
        return start, datetime(end_year, end_month, 1, tzinfo=timezone.utc)

    match = _MONTH_RE.match(text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
        return start, datetime(end_year, end_month, 1, tzinfo=timezone.utc)

    match = _YEAR_RE.match(text)
    if match:
        year = int(match.group(1))
        return (datetime(year, 1, 1, tzinfo=timezone.utc),
                datetime(year + 1, 1, 1, tzinfo=timezone.utc))

    start = parse_dt(text)
    if len(text) == 10:  # a bare date means the whole day
        return start, start + timedelta(days=1)
    return start, start + timedelta(seconds=1)


# --------------------------------------------------------------------------- filters

_SHORTHAND_KEYS = {
    "status": "status", "owner": "created_by", "created_by": "created_by",
    "created": "created_at", "updated": "updated_at", "name": "name",
    "tag": "tags", "size": "total_size_bytes", "process": "process_id",
    "folder": "folders",
    "executor": "executor", "type": "type", "ext": "ext", "q": "q",
}


def parse_shorthand(text: str) -> dict:
    """`status:FAILED created:last-7d owner:@me name~*RNA* size:>=10GB` -> filter tree."""
    clauses = []
    for token in text.split():
        if "~" in token and ":" not in token.split("~")[0]:
            key, _, value = token.partition("~")
            clauses.append({"field": _SHORTHAND_KEYS.get(key, key), "op": "glob",
                            "value": value})
            continue
        if ":" not in token:
            clauses.append({"field": "q", "op": "contains", "value": token})
            continue
        key, _, value = token.partition(":")
        field = _SHORTHAND_KEYS.get(key, key)
        for prefix, op in ((">=", "gte"), ("<=", "lte"), (">", "gt"), ("<", "lt")):
            if value.startswith(prefix):
                clauses.append({"field": field, "op": op, "value": value[len(prefix):]})
                break
        else:
            if field in ("created_at", "updated_at"):
                clauses.append({"field": field, "op": "within", "value": value})
            elif "," in value and field in ("folders", "tags"):
                # These are multi-valued on the row, so `in` compares a list against a
                # set of scalars and can never match.
                member = "has_folder" if field == "folders" else "has_tag"
                clauses.append({"or": [{"field": field, "op": member, "value": v}
                                       for v in value.split(",")]})
            elif "," in value:
                clauses.append({"field": field, "op": "in", "value": value.split(",")})
            elif field == "folders":
                clauses.append({"field": field, "op": "has_folder", "value": value})
            elif field == "tags":
                clauses.append({"field": field, "op": "has_tag", "value": value})
            else:
                clauses.append({"field": field, "op": "eq", "value": value})
    return {"and": clauses} if len(clauses) != 1 else clauses[0]


def parse_filter(spec: Any) -> Optional[dict]:
    if spec is None or spec == {} or spec == "":
        return None
    if isinstance(spec, str):
        return parse_shorthand(spec)
    if not isinstance(spec, dict):
        raise errors.InvalidQuery(
            "A filter must be an object or a shorthand string.",
            what="Pass a filter tree, or a shorthand string.",
            given=spec,
            retry_with=[{"filter": "status:FAILED created:last-7d"}],
        )
    return spec


def resolve_filter(node: Optional[dict], spec: EntitySpec, ctx: Ctx) -> Optional[dict]:
    """Normalize every leaf value and record what was resolved."""
    if node is None:
        return None
    for junction in ("and", "or"):
        if junction in node:
            return {junction: [resolve_filter(c, spec, ctx) for c in node[junction]]}
    if "not" in node:
        return {"not": resolve_filter(node["not"], spec, ctx)}

    field = node.get("field")
    op = node.get("op", "eq")
    value = node.get("value")
    if op not in OPS:
        raise errors.InvalidQuery(
            f"Unknown operator {op!r}.",
            what="Use a supported operator.",
            field="op", given=op,
            retry_with=[{"op": candidate} for candidate in sorted(OPS)],
        )
    if not field:
        raise errors.InvalidQuery(
            "A filter clause needs a field.",
            what="Name the field to filter on.",
            given=node,
            retry_with=[{"field": name, "op": "eq", "value": "..."}
                        for name in list(spec.fields)[:5]],
        )

    if field == "created_by" and value == "@me":
        if not ctx.username:
            raise errors.AuthRequired(
                "@me needs an authenticated identity and there is none.",
                what="Log in so the current username is known.",
                user_action="Run the cirro-session skill to log in.",
            )
        ctx.resolved["@me"] = {"username": ctx.username}
        value = ctx.username

    if field == "status" and op in ("eq", "in"):
        raw = value if isinstance(value, list) else [value]
        expanded = []
        for item in raw:
            key = str(item).lower()
            if key in STATUS_CLASSES:
                expanded.extend(STATUS_CLASSES[key])
                ctx.resolved[key] = STATUS_CLASSES[key]
            else:
                expanded.append(str(item).upper())
        return {"field": field, "op": "in", "value": expanded}

    if op == "within":
        start, end = resolve_interval(value, ctx)
        ctx.resolved[f"{field}.within"] = {
            "given": value, "start": _iso(start), "end": _iso(end), "tz": "UTC",
        }
        return {"field": field, "op": "between", "value": [_iso(start), _iso(end)]}

    if field in ("total_size_bytes", "size", "storage_bytes") and op in (
        "gt", "gte", "lt", "lte", "eq"
    ):
        exact = parse_size(value)
        if str(value) != str(exact):
            ctx.resolved[f"{field}.{op}"] = {"given": value, "bytes": exact}
        return {"field": field, "op": op, "value": exact}

    if op in ("ancestor_of", "descendant_of"):
        return {"field": "_lineage", "op": op, "value": value,
                "depth": node.get("depth", 0)}

    return {"field": field, "op": op, "value": value}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text_of(row: dict, spec: EntitySpec) -> str:
    parts = []
    for field in spec.text_fields:
        value = row.get(field)
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
        elif value is not None:
            parts.append(str(value))
    return " ".join(parts).lower()


def _cmp_key(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
        try:
            return parse_dt(value)
        except errors.AgentError:
            return value
    return value


def matches(row: dict, node: Optional[dict], spec: EntitySpec, ctx: Ctx) -> bool:
    if node is None:
        return True
    if "and" in node:
        return all(matches(row, c, spec, ctx) for c in node["and"])
    if "or" in node:
        return any(matches(row, c, spec, ctx) for c in node["or"])
    if "not" in node:
        return not matches(row, node["not"], spec, ctx)

    field, op, want = node.get("field"), node.get("op", "eq"), node.get("value")

    if field == "_lineage":
        return _lineage_matches(row, op, want, node.get("depth", 0), ctx)

    if field == "q":
        return str(want).lower() in _text_of(row, spec)

    have = row.get(field, _MISSING)
    if have is _MISSING:
        have = _field_value(row, field)

    if op == "exists":
        present = have not in (None, [], {}, "")
        return present == bool(want)
    if op in ("has_tag", "has_folder"):
        return str(want) in [str(t) for t in (have or [])]
    if op == "has_all_tags":
        tags = {str(t) for t in (have or [])}
        return set(str(w) for w in want) <= tags
    if op == "ne":
        # A missing field IS unequal to the value, so it matches; otherwise `ne` is not
        # the complement of `eq`.
        return have is None or str(have) != str(want)
    if op == "nin":
        return have is None or str(have) not in {str(w) for w in want}
    if have is None:
        return False
    if op == "eq":
        return str(have) == str(want)
    if op == "in":
        return str(have) in {str(w) for w in want}
    if op == "glob":
        return fnmatch.fnmatch(str(have).lower(), str(want).lower())
    if op == "regex":
        try:
            return re.search(str(want), str(have)) is not None
        except re.error as exc:
            raise errors.InvalidQuery(
                f"{want!r} is not a valid regular expression: {exc}",
                what="Fix the pattern.", field="value", given=want,
                retry_with=[{"op": "glob", "value": "*text*"},
                            {"op": "contains", "value": "text"}],
            )
    if op == "contains":
        return str(want).lower() in str(have).lower()
    if op == "between":
        low, high = _cmp_key(want[0]), _cmp_key(want[1])
        key = _cmp_key(have)
        try:
            return low <= key < high
        except TypeError:
            return False
    try:
        key, target = _cmp_key(have), _cmp_key(want)
        if op == "gt":
            return key > target
        if op == "gte":
            return key >= target
        if op == "lt":
            return key < target
        if op == "lte":
            return key <= target
    except TypeError:
        return False
    return False


def _lineage_matches(row: dict, op: str, target: str, depth: int, ctx: Ctx) -> bool:
    row_id = row.get("id")
    if not row_id:
        return False
    limit = depth or 64
    if op == "descendant_of":
        seen, frontier, hops = set(), [row_id], 0
        while frontier and hops < limit:
            parents = []
            for node in frontier:
                for parent in ctx.lineage.get(node, []):
                    if parent == target:
                        return True
                    if parent not in seen:
                        seen.add(parent)
                        parents.append(parent)
            frontier, hops = parents, hops + 1
        return False
    # ancestor_of: is `row` an ancestor of `target`?
    seen, frontier, hops = set(), [target], 0
    while frontier and hops < limit:
        parents = []
        for node in frontier:
            for parent in ctx.lineage.get(node, []):
                if parent == row_id:
                    return True
                if parent not in seen:
                    seen.add(parent)
                    parents.append(parent)
        frontier, hops = parents, hops + 1
    return False


def apply_filter(rows: list, node: Optional[dict], spec: EntitySpec, ctx: Ctx) -> list:
    if node is None:
        return list(rows)
    return [r for r in rows if matches(r, node, spec, ctx)]


# --------------------------------------------------------------------------- facets


def _bin_for_span(span_days: float) -> str:
    if span_days <= 31:
        return "day"
    if span_days <= 120:
        return "week"
    if span_days <= 800:
        return "month"
    if span_days <= 2500:
        return "quarter"
    return "year"


def _bin_key(moment: datetime, unit: str) -> str:
    if unit == "day":
        return moment.strftime("%Y-%m-%d")
    if unit == "week":
        iso = moment.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if unit == "month":
        return moment.strftime("%Y-%m")
    if unit == "quarter":
        return f"{moment.year}-Q{(moment.month - 1) // 3 + 1}"
    return str(moment.year)


def _terms(rows: list, field: str, labels: Optional[dict] = None) -> Facet:
    counts: dict = {}
    multi = False
    for row in rows:
        value = row.get(field, _MISSING)
        if value is _MISSING:
            value = _field_value(row, field)
        if isinstance(value, list):
            multi = multi or len(value) > 1
            if not value:
                # An empty list landed in no bucket, so the facet undercounted -- the
                # folders facet silently dropped every unfiled dataset.
                counts[None] = counts.get(None, 0) + 1
            for item in value:
                key = _facet_key(item)
                counts[key] = counts.get(key, 0) + 1
        else:
            key = _facet_key(value)
            counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    values = [(key, n, (labels or {}).get(key)) for key, n in ordered]
    note = None
    if multi:
        note = f"{field} is multi-valued; the counts sum to more than the match total"
    return Facet(kind="terms", values=values, note=note)


def _field_value(row: dict, field: str) -> Any:
    """Read a field, tolerating `process_id` when the row carries `process={id,name}`."""
    if field in row:
        return _facet_key(row[field])
    if field.endswith("_id"):
        base = field[:-3]
        if base in row:
            return _facet_key(row[base])
    return None


def _facet_key(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("id") or value.get("name")
    return value


def _histogram(rows: list, field: str) -> Optional[Facet]:
    moments = []
    for row in rows:
        raw = row.get(field)
        if not raw:
            continue
        try:
            moments.append(parse_dt(raw))
        except errors.AgentError:
            continue
    if not moments:
        return None
    span = (max(moments) - min(moments)).days
    unit = _bin_for_span(span)
    counts: dict = {}
    for moment in moments:
        key = _bin_key(moment, unit)
        counts[key] = counts.get(key, 0) + 1
    values = [(key, counts[key], None) for key in sorted(counts)]
    return Facet(kind="histogram", values=values, bin=unit)


def compute_facets(rows: list, spec: EntitySpec, labels: Optional[dict] = None) -> dict:
    """Facets over the full match set. Never over the rows that survive the budget."""
    labels = labels or {}
    out: dict = {}
    for field in spec.facet_priority:
        if field == spec.date_field:
            facet = _histogram(rows, field)
            if facet:
                out[field] = facet
            continue
        facet = _terms(rows, field, labels.get(field))
        if facet.cardinality:
            out[field] = facet
    return out


def name_clusters(rows: list, field: str = "name", limit: int = 4) -> list:
    counts: dict = {}
    examples: dict = {}
    for row in rows:
        value = row.get(field)
        if not value:
            continue
        for token in re.split(r"[^A-Za-z0-9]+", str(value)):
            if len(token) < 3 or token.isdigit():
                continue
            counts[token] = counts.get(token, 0) + 1
            examples.setdefault(token, str(value))
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"token": token, "n": n, "example": examples[token], "glob": f"*{token}*"}
        for token, n in ordered[:limit]
        if n > 1
    ]


def compute_shape(rows: list, spec: EntitySpec, ctx: Ctx,
                  granted: Optional[set] = None) -> dict:
    """The non-categorical portrait: ranges, clusters, lineage, and what is missing."""
    granted = granted or set()
    shape: dict = {}

    if spec.date_field:
        moments = []
        for row in rows:
            raw = row.get(spec.date_field)
            if raw:
                try:
                    moments.append(parse_dt(raw))
                except errors.AgentError:
                    pass
        if moments:
            low, high = min(moments), max(moments)
            shape[spec.date_field] = {
                "min": _iso(low), "max": _iso(high), "span_days": (high - low).days,
                "n_last_7d": sum(1 for m in moments if (ctx.now - m).days < 7),
                "n_last_30d": sum(1 for m in moments if (ctx.now - m).days < 30),
                "n_last_90d": sum(1 for m in moments if (ctx.now - m).days < 90),
            }

    if "name" in spec.fields:
        clusters = name_clusters(rows)
        if clusters:
            shape["name_clusters"] = clusters

    if spec.name == "dataset":
        with_parents = sum(1 for r in rows if r.get("source_dataset_ids"))
        depth, capped = _max_depth(rows, ctx)
        lineage = {
            "roots": len(rows) - with_parents,
            "with_parents": with_parents,
            "max_depth_observed": depth,
        }
        if capped:
            # Reporting the cap as if it were the observed depth would be a lie.
            lineage["depth_truncated_at"] = _DEPTH_CAP
            lineage["note"] = (f"lineage chains reach at least {_DEPTH_CAP} generations; "
                               "the walk stopped there")
        shape["lineage"] = lineage

    # Gated fields advertise their absence rather than being silently missing.
    for field in spec.gated_fields():
        if field in granted:
            continue
        tier = spec.tier_of(field)
        if field in ("files", "logs", "tasks", "trace"):
            continue
        shape.setdefault("unavailable", {})[field] = {
            "available": False,
            "reason": f"tier:{tier}",
            "how": f'add include:["{field}"] to a query matching few enough rows',
        }
    return shape


_DEPTH_CAP = 64


def _max_depth(rows: list, ctx: Ctx) -> tuple:
    """Deepest ancestor chain, and whether the walk hit its own cap."""
    ids = {r.get("id") for r in rows}
    best, capped = 0, False
    for row in rows:
        depth, node, seen = 0, row.get("id"), set()
        while node and node not in seen and depth < _DEPTH_CAP:
            seen.add(node)
            parents = [p for p in ctx.lineage.get(node, []) if p in ids]
            if not parents:
                break
            node = parents[0]
            depth += 1
        if depth >= _DEPTH_CAP:
            capped = True
        best = max(best, depth)
    return best, capped


# --------------------------------------------------------------------------- exemplars


def pick_exemplars(rows: list, spec: EntitySpec, facets: dict, limit: int = 5) -> list:
    """Labeled exemplars, not a page.

    A page's worth of "the first 40 by date" is a misleading sample that invites
    reasoning from it. Each exemplar instead says why it is here.
    """
    if not rows:
        return []
    chosen: dict = {}

    def take(why: str, row: Optional[dict]) -> None:
        if row is None:
            return
        key = row.get("id") or row.get("path") or row.get("name")
        if key in chosen or len(chosen) >= limit:
            return
        chosen[key] = dict(row, _why=why)

    date_field = spec.date_field
    if date_field:
        dated = [r for r in rows if r.get(date_field)]
        if dated:
            take("newest", max(dated, key=lambda r: str(r[date_field])))
            take("oldest", min(dated, key=lambda r: str(r[date_field])))

    if "status" in spec.fields:
        failed = [r for r in rows if str(r.get("status", "")).upper() == "FAILED"]
        if failed and date_field:
            take("most_recent_failure",
                 max(failed, key=lambda r: str(r.get(date_field) or "")))
        elif failed:
            take("failed", failed[0])
        active = [r for r in rows
                  if str(r.get("status", "")).upper() in STATUS_CLASSES["@active"]]
        if active:
            take("currently_active", active[0])

    status_facet = facets.get("status")
    if status_facet and status_facet.values:
        biggest = status_facet.values[0][0]
        match = next((r for r in rows if str(r.get("status")) == str(biggest)), None)
        take(f"largest_status_cell:{biggest}", match)

    shared = next((r for r in rows if r.get("share")), None)
    take("arrives_via_share", shared)

    for row in rows:
        if len(chosen) >= limit:
            break
        take("additional", row)
    return list(chosen.values())


# --------------------------------------------------------------------------- suggest


def _est_tokens_for(count: int, rows: list, budget: int = 6000) -> int:
    if not rows or count == 0:
        return 120
    per_row = max(estimate_tokens(rows[0]), 1)
    full = 250 + per_row * count
    # Beyond the budget the response becomes facets plus exemplars, which is flat.
    return full if full <= budget else 250 + per_row * 5 + 450


def suggest(rows: list, base_query: dict, spec: EntitySpec, facets: dict, ctx: Ctx,
            shape: Optional[dict] = None, limit: int = 4) -> list:
    """Follow-up calls whose match counts are exact, not estimated.

    The whole match set is already in memory, so each candidate filter can simply
    be evaluated. That is what lets an agent pick its next call knowing whether
    the result will fit, instead of probing for several rounds.
    """
    total = len(rows)
    if total == 0:
        return []
    candidates = []

    def add(cid: str, why: str, extra_filter: Optional[dict] = None, **overrides) -> None:
        call = {k: v for k, v in base_query.items()
                if k in ("entity", "scope", "sort", "include")}
        call.update(overrides)
        if extra_filter is not None:
            call["filter"] = extra_filter
            node = resolve_filter(parse_filter(extra_filter), spec,
                                  Ctx(ctx.username, ctx.now, ctx.lineage))
            matched = sum(1 for r in rows if matches(r, node, spec, ctx))
        else:
            matched = total
        if matched == 0 or (matched == total and extra_filter is not None):
            return
        candidates.append({
            "id": cid, "why": why, "call": call,
            "expect": {
                "matched": matched,
                "est_tokens": _est_tokens_for(matched, rows),
                "api_calls": 0,
                "from_cache": True,
            },
        })

    status_facet = facets.get("status")
    if status_facet:
        bad = {v for v, _, _ in status_facet.values} & set(STATUS_CLASSES["@bad"])
        if bad:
            n_bad = sum(n for v, n, _ in status_facet.values if v in bad)
            add("nc_broken",
                f"{n_bad} of {total} are in a bad state "
                f"({', '.join(sorted(bad))})",
                {"field": "status", "op": "in", "value": sorted(bad)})
        active = {v for v, _, _ in status_facet.values} & set(STATUS_CLASSES["@active"])
        if active:
            n_active = sum(n for v, n, _ in status_facet.values if v in active)
            add("nc_active", f"{n_active} of {total} are still running or pending",
                {"field": "status", "op": "in", "value": ["@active"]})

    owner_facet = facets.get("created_by")
    if owner_facet and ctx.username:
        mine = next((n for v, n, _ in owner_facet.values if v == ctx.username), 0)
        if mine:
            add("nc_mine_recent",
                f"you ({ctx.username}) own {mine} of {total}; this narrows to yours from "
                "the last 30 days",
                {"and": [{"field": "created_by", "op": "eq", "value": "@me"},
                         {"field": "created_at", "op": "within", "value": "last-30d"}]})

    recent = (shape or {}).get(spec.date_field or "", {})
    if recent.get("n_last_7d"):
        add("nc_recent", f"{recent['n_last_7d']} created in the last 7 days",
            {"field": spec.date_field, "op": "within", "value": "last-7d"})

    process_facet = facets.get("process_id")
    if process_facet and process_facet.cardinality > 1:
        value, n, label = process_facet.values[0]
        add("nc_top_process", f"{n} of {total} came from {label or value}",
            {"field": "process_id", "op": "eq", "value": value})

    clusters = (shape or {}).get("name_clusters") or []
    if clusters:
        cluster = clusters[0]
        add("nc_name_cluster",
            f"{cluster['n']} names contain {cluster['token']!r}",
            {"field": "name", "op": "glob", "value": cluster["glob"]})

    # Prefer narrowings that are informative: neither everything nor almost nothing.
    def rank(candidate: dict) -> tuple:
        matched = candidate["expect"].get("matched", total)
        return (abs(matched - total * 0.25), -matched)

    candidates.sort(key=rank)

    # An aggregation answers "what is in here" without returning any rows at all,
    # so it keeps a reserved slot rather than competing on match count.
    aggregation = None
    if process_facet and process_facet.cardinality > 1:
        agg = {k: v for k, v in base_query.items() if k in ("entity", "scope")}
        agg.update({"group_by": ["process_id"], "metrics": ["count"],
                    "order_by": "-count", "limit_groups": 20})
        aggregation = {
            "id": "nc_by_process",
            "why": "aggregation only -- no rows; answers 'what produced the data here'",
            "call": agg,
            "expect": {"groups": min(process_facet.cardinality, 20), "est_tokens": 400,
                       "api_calls": 0, "from_cache": True},
        }
    if aggregation:
        return candidates[:max(limit - 1, 1)] + [aggregation]
    return candidates[:limit]


# --------------------------------------------------------------------------- group_by

_BIN_SUFFIXES = ("month", "quarter", "year", "day", "week", "log2", "class")


def _group_key(row: dict, key: str, ctx: Ctx) -> Any:
    if ":" in key:
        field, _, unit = key.partition(":")
        raw = row.get(field)
        if unit in ("day", "week", "month", "quarter", "year"):
            if not raw:
                return None
            try:
                return _bin_key(parse_dt(raw), unit)
            except errors.AgentError:
                # Consistent with every other date path: skip the row rather than
                # failing the whole aggregation and blaming the caller.
                return None
        if unit == "log2":
            value = raw or 0
            if not value:
                return "0"
            exponent = int(math.log2(value))
            return f"2^{exponent}..2^{exponent + 1}"
        if unit == "class":
            status = str(raw or "").upper()
            for cls, members in STATUS_CLASSES.items():
                if status in members:
                    return cls
            return "@other"
        return raw
    return _field_value(row, key)


def group(rows: list, group_by: list, metrics: list, ctx: Ctx, *,
          order_by: Optional[str] = None, limit_groups: int = 20,
          labels: Optional[dict] = None) -> tuple:
    """Aggregate only. Callers must omit `rows` from the response entirely."""
    labels = labels or {}
    # Validated up front: metric() only runs per bucket, so an unknown metric over an
    # empty match set used to return a successful zero-group response.
    for name in metrics:
        aggregation = name.split(":", 1)[0]
        if name != "count" and (":" not in name or
                                aggregation not in ("sum", "avg", "min", "max")):
            raise errors.InvalidQuery(
                f"Unknown metric {name!r}.",
                what="Use count, or agg:field with sum/avg/min/max.",
                field="metrics", given=name,
                retry_with=[{"metrics": ["count"]},
                            {"metrics": ["sum:total_size_bytes"]}],
            )

    buckets: dict = {}
    for row in rows:
        key = tuple(_group_key(row, part, ctx) for part in group_by)
        buckets.setdefault(key, []).append(row)

    def metric(name: str, members: list) -> Any:
        if name == "count":
            return len(members)
        agg, _, field = name.partition(":")
        values = [m.get(field) for m in members if m.get(field) is not None]
        if agg == "sum":
            return sum(v for v in values if isinstance(v, (int, float)))
        if agg == "max":
            return max((str(v) for v in values), default=None)
        if agg == "min":
            return min((str(v) for v in values), default=None)
        if agg == "avg":
            numbers = [v for v in values if isinstance(v, (int, float))]
            return round(sum(numbers) / len(numbers), 2) if numbers else None
        raise errors.InvalidQuery(
            f"Unknown metric {name!r}.",
            what="Use count, or agg:field with sum/avg/min/max.",
            field="metrics", given=name,
            retry_with=[{"metrics": ["count"]}, {"metrics": ["sum:total_size_bytes"]}],
        )

    groups = []
    for key, members in buckets.items():
        entry = {"key": {}}
        for part, value in zip(group_by, key):
            entry["key"][part] = value
            label_map = labels.get(part) or {}
            if value in label_map:
                entry["key"][part.replace("_id", "_name")] = label_map[value]
        for name in metrics:
            entry[name.replace(":", "_")] = metric(name, members)
        groups.append(entry)

    if order_by:
        descending = order_by.startswith("-")
        field = order_by.lstrip("-").replace(":", "_")
        groups.sort(key=lambda g: (g.get(field) is None, str(g.get(field))
                                   if not isinstance(g.get(field), (int, float))
                                   else g.get(field)), reverse=descending)

    shown, tail = groups[:limit_groups], groups[limit_groups:]
    tail_summary = None
    if tail:
        tail_summary = {"groups": len(tail)}
        for name in metrics:
            column = name.replace(":", "_")
            if all(isinstance(g.get(column), (int, float)) for g in tail):
                tail_summary[column] = sum(g[column] for g in tail)
    totals = {"groups": len(groups)}
    for name in metrics:
        column = name.replace(":", "_")
        if all(isinstance(g.get(column), (int, float)) for g in groups):
            totals[column] = sum(g[column] for g in groups)
    return shown, tail_summary, totals

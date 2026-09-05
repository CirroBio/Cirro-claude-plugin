"""The query, get and describe verbs: facet-first listings over a
fully fetched match set, and single-entity reads.
"""
from typing import Tuple

from .. import errors, query as Q, schema
from ..envelope import Counts, DEFAULT_MAX_TOKENS, DETAIL_BUDGET, ROWS_ABSENT, build
from .reads import (Reader, dataset_detail, dataset_manifest, dataset_tasks, file_count,
                    is_terminal, process_detail, project_metrics, resolve_named)

# AWS reports S3 storage in binary units, so a metrics record saying "GB" means 2**30
# bytes. An unlisted unit is reported as-is rather than converted on a guess.
STORAGE_UNIT_BYTES = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
                      "TB": 1024 ** 4, "PB": 1024 ** 5}


def _lineage(rows: list) -> dict:
    return {r["id"]: r.get("source_dataset_ids") or [] for r in rows if r.get("id")}


def _projections_with(spec: schema.EntitySpec, granted: set) -> dict:
    """Add opted-in fields to every projection.

    Without this, `include` silently does nothing: the field is fetched and then
    dropped again by the projection the envelope applies.
    """
    projections = {name: list(fields) for name, fields in spec.projections.items()}
    extra = []
    for field in sorted(granted):
        extra.append(field)
        extra.extend(_DERIVED_COMPANIONS.get(field, []))
    for fields in projections.values():
        for field in extra:
            if field not in fields:
                fields.append(field)
    return projections


def _visible_facets(matched: list, spec: schema.EntitySpec, labels: dict,
                    granted: set) -> dict:
    """Facets over the whole match set, minus the fields nothing has fetched.

    A gated field can sit in facet_priority -- `config_source` does -- and unless
    `include` granted it every row's value is null, so the facet was one all-null
    bucket while `shape.unavailable` in the same response reported the field as
    unavailable. The response contradicted itself and spent budget doing it.
    """
    facets = Q.compute_facets(matched, spec, labels=labels)
    return {name: facet for name, facet in facets.items()
            if name in granted or spec.tier_of(name) not in schema.GATED_TIERS}


def _labels(reader: Reader, entity: str) -> dict:
    if entity != "dataset":
        return {}
    return {
        "process_id": reader.process_names(),
        "project_id": {p["id"]: p["name"] for p in reader.projects()},
    }


def _within_dataset(reader: Reader, entity: str, request: dict,
                    scope: dict) -> Tuple[list, bool, dict, list]:
    """Files and tasks: one dataset, which lives in exactly one project.

    `scope_project_ids` can name several projects, so only the first is read. Echoing
    every named project back reported one project's manifest as though it covered all
    of them; the echo carries the project actually read and `omitted` names the rest.
    """
    named_project_ids = reader.scope_project_ids(scope)
    dataset_id = request.get("dataset_id") or scope.get("dataset_id")
    if not dataset_id:
        raise errors.ScopeRequired(
            f"{entity} requires a dataset_id.",
            what=f"{entity.capitalize()}s belong to one dataset.",
            field="dataset_id",
            retry_with=[{"entity": "dataset",
                         "scope": {"project_ids": named_project_ids}}],
        )
    project_id = named_project_ids[0]
    echo = {"project_ids": [project_id], "dataset_id": dataset_id}
    omitted = _projects_left_out(entity, named_project_ids, dataset_id)
    if entity == "file":
        entries, total, _artifacts = dataset_manifest(
            reader, project_id, dataset_id,
            is_terminal=is_terminal(reader, project_id, dataset_id))
        return entries, len(entries) >= (total or 0), echo, omitted
    return dataset_tasks(reader, project_id, dataset_id), True, echo, omitted


def _projects_left_out(entity: str, named: list, dataset_id: str) -> list:
    """The named projects this read never looked at.

    Same shape as `read_summaries._projects_left_out`: one read answers from one
    project, so a scope naming several has to say which one it answered from.
    """
    if len(named) < 2:
        return []
    skipped = named[1:]
    return [{
        "code": "SCOPE_NARROWED",
        "projects_not_read": skipped,
        "detail": (f"{len(named)} projects were named. Dataset {dataset_id} was read "
                   f"from {named[0]} only; a dataset belongs to one project, so if it "
                   "lives in one of the others this answer is empty rather than wrong."),
        "fix": {"note": "Name the project that owns the dataset.",
                "retry_with": [{"entity": entity, "dataset_id": dataset_id,
                                "scope": {"project_ids": [pid]}}
                               for pid in skipped[:3]]},
    }]


def _collect(reader: Reader, entity: str, request: dict) -> Tuple[list, bool, dict, list]:
    """Fetch the scanned collection for an entity, plus its scope echo and omissions."""
    scope = request.get("scope") or {}
    if entity == "project":
        return reader.projects(), True, {}, []
    if entity == "process":
        archived = bool(request.get("include_archived"))
        return reader.processes(include_archived=archived), True, {}, []
    if entity in ("file", "task"):
        return _within_dataset(reader, entity, request, scope)

    project_ids = reader.scope_project_ids(scope)
    collected, complete = [], True
    for project_id in project_ids:
        if entity == "dataset":
            items, ok = reader.datasets(project_id)
        elif entity == "sample":
            items, ok = reader.samples(project_id)
        elif entity == "reference":
            items, ok = reader.references(project_id)
        else:
            raise errors.NotSupported(
                f"query does not support entity {entity!r}.",
                what="Use one of the supported entities.",
                retry_with=[{"entity": name} for name in sorted(schema.ENTITIES)],
            )
        for item in items:
            item.setdefault("project_id", project_id)
        collected.extend(items)
        complete = complete and ok
    return collected, complete, {"project_ids": project_ids}, []


def _per_row_fields(include: list, spec: schema.EntitySpec) -> list:
    """The requested fields that cost one API call per matched row."""
    return [f for f in include
            if spec.tier_of(f) in schema.GATED_TIERS
            or (spec.name == "process" and f in _PROCESS_DETAIL_DERIVED)]


def _refuse_unfetched_filter(node, spec: schema.EntitySpec) -> None:
    """A filter may only read fields a listing row actually holds.

    A gated field costs one call per row and is fetched only for rows that already
    matched, so filtering on one ran against rows that never had it and returned a
    zero indistinguishable from a real one: `size:>=10GB` over datasets said 0 not
    because none are that big but because total_size_bytes was never fetched.

    `include` is deliberately not an escape hatch here. It fetches detail for the
    matched rows, which is after this filter would have had to run, so honouring the
    filter would mean a detail call for every scanned row -- 1038 of them in one real
    project -- which is exactly the cost the tier model exists to refuse. So this
    refuses outright and points at the fields that are free to filter on.
    """
    gated = sorted(f for f in Q.filter_fields(node) - Q.PSEUDO_FIELDS
                   if spec.tier_of(f) in schema.GATED_TIERS)
    if not gated:
        return
    free = [f for f, (tier, _) in spec.fields.items()
            if tier not in schema.GATED_TIERS][:6]
    raise errors.InvalidQuery(
        f"{spec.name} cannot be filtered on {', '.join(gated)}.",
        what=(f"{gated[0]} costs one API call per row, so it is not on a listing row. "
              "Filtering on it would need every scanned row fetched first."),
        field="filter", given=gated,
        candidates=[{"field": name} for name in free],
        retry_with=[{"filter": {"field": name, "op": "eq", "value": "..."}}
                    for name in free[:3]]
        + [{"include": gated, "note": "readable per row once a filter has narrowed the "
                                      "match set"}],
    )


def _refuse_detail_cost(include: list, per_row: list, n_matched: int) -> None:
    """The one cost gate over `include`.

    Symmetric with scope.confirm_fanout: a deliberate survey across many rows is
    legitimate, so the gate offers an explicit way to accept the cost rather than
    only telling the caller to narrow.
    """
    raise errors.CostGate(
        f"include={per_row} costs one API call per row and {n_matched} rows match.",
        what=(f"Narrow the filter to at most {DETAIL_BUDGET} rows, or accept the cost "
              "explicitly."),
        field="include", given=include,
        estimated_cost={"api_calls": n_matched}, limit=DETAIL_BUDGET,
        retry_with=[
            {"note": f"add a filter so matched <= {DETAIL_BUDGET}, then re-send include"},
            {"confirm_detail_cost": True,
             "note": f"accept {n_matched} API calls for a deliberate full survey"},
            {"note": "sampling is not offered for size figures: they are heavy-tailed, "
                     "so a small-sample estimate would mislead"},
        ],
    )


def _apply_include(reader: Reader, entity: str, matched: list, include: list,
                   spec: schema.EntitySpec, project_ids: list,
                   confirm_cost: bool = False) -> Tuple[set, list]:
    """Fetch opted-in expensive fields, or refuse with the cost."""
    if not include:
        return set(), []
    unknown = [f for f in include if f not in spec.fields]
    if unknown:
        raise errors.InvalidQuery(
            f"Unknown field(s) in include: {unknown}.",
            what="Only declared fields can be included.",
            field="include", given=include,
            retry_with=[{"verb": "describe", "entity": entity}],
        )
    per_row = _per_row_fields(include, spec)
    if per_row and len(matched) > DETAIL_BUDGET and not confirm_cost:
        _refuse_detail_cost(include, per_row, len(matched))

    granted, notes = set(include), []
    if any(spec.tier_of(f) in schema.GATED_TIERS for f in include):
        reader.cost.tier_ceiling = max(
            (spec.tier_of(f) for f in include), key=schema.tier_rank, default="list")

    if entity == "process":
        # Gated the same way as the dataset branch below: an `include` of list-tier
        # fields alone asks for nothing the listing has not already got, and used to
        # buy one processes.get per matched row for it.
        if per_row:
            for row in matched:
                row.update(process_detail(reader, row["id"], row))
        return granted, notes

    if entity != "dataset":
        return granted, notes

    wants_detail = any(spec.tier_of(f) == schema.DETAIL for f in include)
    wants_probe = any(spec.tier_of(f) == schema.PROBE for f in include)
    for row in matched:
        project_id = row.get("project_id") or (project_ids[0] if project_ids else None)
        if wants_detail:
            row.update(dataset_detail(reader, project_id, row["id"], row))
        elif wants_probe and "file_count" in include:
            row["file_count"] = file_count(reader, project_id, row["id"])
    if wants_detail and wants_probe:
        notes.append("file_count came free from the same detail payload; no probe calls "
                     "were needed")
    return granted, notes


def do_query(request: dict) -> dict:
    entity = request.get("entity", "dataset")
    spec = schema.get(entity)
    reader = Reader(request)
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["query"])

    scanned, complete, scope_echo, omitted = _collect(reader, entity, request)
    lineage = _lineage(scanned) if entity == "dataset" else {}
    ctx = reader.ctx(lineage)

    node = Q.resolve_filter(Q.parse_filter(request.get("filter"), spec), spec, ctx)
    _refuse_unfetched_filter(node, spec)
    matched = Q.apply_filter(scanned, node, spec, ctx)

    sort = request.get("sort") or spec.default_sort
    matched = _sorted(matched, sort, spec)

    include = list(request.get("include") or [])
    granted, notes = _apply_include(
        reader, entity, matched, include, spec, scope_echo.get("project_ids") or [],
        confirm_cost=bool(request.get("confirm_detail_cost")))
    if reader.cost.detail_calls and len(matched) > DETAIL_BUDGET:
        notes.append(f"{reader.cost.detail_calls} detail calls were made because "
                     "confirm_detail_cost was set")

    projections = _projections_with(spec, granted)
    labels = _labels(reader, entity)
    facets = _visible_facets(matched, spec, labels, granted)

    echoed = {
        "entity": entity,
        "scope": scope_echo,
        "filter": node,
        "sort": sort,
        "include": include,
        "tier_ceiling": reader.cost.tier_ceiling,
        "resolved": ctx.resolved,
    }

    # group_by means aggregation only: `rows` is absent from the response entirely,
    # so "no matches" can never be confused with "rows were not requested".
    if request.get("group_by"):
        metrics = request.get("metrics") or ["count"]
        groups, tail, totals = Q.group(
            matched, request["group_by"], metrics,
            order_by=request.get("order_by"), labels=labels,
            limit_groups=int(request.get("limit_groups") or 20))
        echoed.update({"group_by": request["group_by"], "metrics": metrics,
                       "order_by": request.get("order_by")})
        reader.cost.strategy = "snapshot+client_aggregate"
        counts = Counts(matched=len(matched), scanned=len(scanned), scan_complete=complete,
                        rows_are=ROWS_ABSENT, groups_total=totals["groups"],
                        groups_returned=len(groups))
        extras = {"totals": totals}
        if tail:
            extras["group_tail"] = tail
        return build(entity=entity, query=echoed, counts=counts, cost=reader.cost,
                     groups=groups, group_extras=extras, facets=facets,
                     facet_priority=spec.facet_priority, omitted=omitted, notes=notes,
                     max_tokens=max_tokens)

    shape = Q.compute_shape(matched, spec, ctx, granted=granted)
    exemplars = Q.pick_exemplars(matched, spec, facets)
    next_calls = Q.suggest(matched, echoed, spec, facets, ctx, shape)
    reader.cost.strategy = "snapshot+client_filter"

    counts = Counts(matched=len(matched), scanned=len(scanned), scan_complete=complete)
    return build(entity=entity, query=echoed, counts=counts, cost=reader.cost,
                 rows=matched, facets=facets, shape=shape, next_calls=next_calls,
                 exemplars=exemplars, projections=projections,
                 facet_priority=spec.facet_priority, omitted=omitted, notes=notes,
                 max_tokens=max_tokens)


def do_get(request: dict) -> dict:
    entity = request.get("entity", "dataset")
    spec = schema.get(entity)
    reader = Reader(request)
    given = request.get("id") or request.get("name")
    if not given:
        raise errors.InvalidQuery(
            "get needs an id (or a name, for reads).",
            what="Identify the entity to fetch.", field="id",
            retry_with=[{"verb": "query", "entity": entity}],
        )

    if entity == "project":
        row = reader.resolve_project(given)
        if "storage_bytes" in (request.get("include") or []):
            row.update(_project_storage(reader, row["id"]))
        reader.cost.strategy = "snapshot"
        counts = Counts(matched=1, scanned=len(reader.projects()), scan_complete=True,
                        returned=1)
        return build(entity=entity, query={"entity": entity, "id": row["id"]},
                     counts=counts, cost=reader.cost, rows=[row],
                     projections={"standard": list(row)}, next_calls=[
                         {"id": "nc_datasets", "why": "list this project's datasets",
                          "call": {"verb": "query", "entity": "dataset",
                                   "scope": {"project_ids": [row["id"]]}}},
                         {"id": "nc_spend", "why": "what this project has cost, by month",
                          "call": {"verb": "list", "of": "spend",
                                   "scope": {"project_ids": [row["id"]]}}}],
                     max_tokens=int(request.get("max_tokens")
                                    or DEFAULT_MAX_TOKENS["get"]))

    # A get names one entity explicitly, so it must not hide archived ones the way a
    # listing does -- otherwise a valid ID reports NOT_FOUND.
    scanned, complete, scope_echo, omitted = _collect(
        reader, entity, {**request, "include_archived": True})
    ctx = reader.ctx(_lineage(scanned) if entity == "dataset" else {})
    row = _resolve_one(entity, given, scanned, spec, scope_echo)

    include = list(request.get("include") or [])
    granted, notes = _apply_include(reader, entity, [row], include, spec,
                                    scope_echo.get("project_ids") or [],
                                    confirm_cost=True)
    reader.cost.strategy = "snapshot+detail" if granted else "snapshot"

    next_calls = []
    if entity == "dataset":
        project_ids = scope_echo.get("project_ids") or []
        next_calls = [
            {"id": "nc_files", "why": "summarize the file manifest without listing it",
             "call": {"verb": "summarize", "of": "files", "dataset_id": row["id"],
                      "scope": {"project_ids": project_ids}}},
            {"id": "nc_lineage", "why": "what was produced from this dataset",
             "call": {"verb": "query", "entity": "dataset",
                      "scope": {"project_ids": project_ids},
                      "filter": {"field": "_lineage", "op": "descendant_of",
                                 "value": row["id"]}}},
        ]
        if not row.get("is_terminal"):
            next_calls.insert(0, {
                "id": "nc_poll",
                "why": "this dataset is still active; re-fetch to see current status",
                "call": {"verb": "get", "entity": "dataset", "id": row["id"],
                         "scope": {"project_ids": project_ids}, "freshness": "fresh"}})

    counts = Counts(matched=1, scanned=len(scanned), scan_complete=complete, returned=1)
    return build(entity=entity, query={"entity": entity, "id": row.get("id"),
                                       "scope": scope_echo, "include": include,
                                       "resolved": ctx.resolved},
                 counts=counts, cost=reader.cost, rows=[row],
                 projections={"standard": list(row)}, next_calls=next_calls,
                 omitted=omitted, notes=notes,
                 max_tokens=int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["get"]))


def _project_storage(reader: Reader, project_id: str) -> dict:
    try:
        records = [r for r in project_metrics(reader, project_id)
                   if r["series"] == "storage"]
    except Exception:
        return {"storage_bytes": None,
                "storage_note": "the metrics API did not return data for this project"}
    if not records:
        return {"storage_bytes": None,
                "storage_note": "the metrics API holds no storage records for this project"}
    latest = max(records, key=lambda r: str(r.get("date") or ""))
    unit = str(latest.get("unit") or "").upper()
    scale = STORAGE_UNIT_BYTES.get(unit)
    total = latest["total"]
    # Daily records that lag; saying so beats implying a live number. `storage_amount`
    # with its unit is the figure the API actually reported, so it is what a person
    # should read; `storage_bytes` is derived from it and states its own convention.
    note = (f"summed across {len(latest['by_service'])} storage tiers on the latest of"
            f" {len(records)} daily records; may lag by up to 48h")
    if scale is None:
        note = (f"{note}. The API reported the unit as {latest.get('unit')!r}, which is"
                " not a size this read can convert, so only storage_amount is given")
    elif scale > 1:
        note = f"{note}. storage_bytes takes 1 {unit} as {scale} bytes, as AWS does"
    return {
        "storage_bytes": int(total * scale) if scale else None,
        "storage_amount": total,
        "storage_unit": latest.get("unit"),
        "storage_by_tier": latest["by_service"],
        "storage_as_of": latest.get("date"),
        "storage_note": note,
    }


def _resolve_one(entity: str, given: str, scanned: list, spec: schema.EntitySpec,
                 scope_echo: dict) -> dict:
    return resolve_named(
        scanned, given, entity=entity, field="id",
        brief=lambda r: {"id": r.get("id"), "name": r.get("name"),
                         "created_at": r.get("created_at"),
                         "created_by": r.get("created_by")},
        retry=lambda r: {"verb": "get", "entity": entity, "id": r.get("id"),
                         "scope": scope_echo},
        near_retry=lambda near: ([{"verb": "query", "entity": entity, "scope": scope_echo}]
                                 + [{"verb": "get", "entity": entity, "name": n,
                                     "scope": scope_echo} for n in near[:3]]),
        missing_what="Check the name, or widen the scope.",
    )


# Fields that are free once their source field has been fetched, so they should
# travel with it rather than being requested separately.
_DERIVED_COMPANIONS = {"total_size_bytes": ["total_size_human"]}

# These read as DERIVED because they are computed, but only from the process detail
# payload, so they cost one call per row like any DETAIL field and are gated as such.
_PROCESS_DETAIL_DERIVED = ("config_location", "code_location")


def _sorted(rows: list, sort: list, spec: schema.EntitySpec) -> list:
    """Sort numerics numerically, and rows missing the field last in either direction.

    Stringifying every key made `total_size_bytes`, `file_count`, `exit_code` and
    `task_id` sort lexicographically, so 9000 came before 10000. And `reverse`
    inverts the whole key, so a fixed missing-last rank came out missing-FIRST on any
    descending sort of a partly-populated field -- `-file_count` led with the rows
    that have no file_count. The rank therefore flips with the direction.
    """
    def ordering(field: str, absent: int):
        def key(row):
            value = row.get(field)
            if value is None:
                return (absent, 0.0, "")
            if isinstance(value, bool):
                return (1, float(value), "")
            if isinstance(value, (int, float)):
                return (0, float(value), "")
            return (1, 0.0, str(value))
        return key

    for entry in reversed(sort or spec.default_sort):
        descending = entry.startswith("-")
        rows = sorted(rows, key=ordering(entry.lstrip("-"), -1 if descending else 2),
                      reverse=descending)
    return rows

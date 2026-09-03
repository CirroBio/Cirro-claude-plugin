"""The query, get and describe verbs: facet-first listings over a
fully fetched match set, and single-entity reads.
"""
from typing import Tuple

from .. import errors, query as Q, schema
from ..envelope import Counts, DEFAULT_MAX_TOKENS, ROWS_ABSENT, build
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


def _labels(reader: Reader, entity: str) -> dict:
    if entity != "dataset":
        return {}
    return {
        "process_id": reader.process_names(),
        "project_id": {p["id"]: p["name"] for p in reader.projects()},
    }


def _collect(reader: Reader, entity: str, request: dict) -> Tuple[list, bool, dict]:
    """Fetch the scanned collection for an entity, plus its scope echo."""
    scope = request.get("scope") or {}
    if entity == "project":
        return reader.projects(), True, {}
    if entity == "process":
        archived = bool(request.get("include_archived"))
        return reader.processes(include_archived=archived), True, {}

    if entity in ("file", "task"):
        project_ids = reader.scope_project_ids(scope)
        dataset_id = request.get("dataset_id") or scope.get("dataset_id")
        if not dataset_id:
            raise errors.ScopeRequired(
                f"{entity} requires a dataset_id.",
                what=f"{entity.capitalize()}s belong to one dataset.",
                field="dataset_id",
                retry_with=[{"entity": "dataset", "scope": {"project_ids": project_ids}}],
            )
        if entity == "file":
            entries, total, _artifacts = dataset_manifest(
                reader, project_ids[0], dataset_id,
                is_terminal=is_terminal(reader, project_ids[0], dataset_id))
            return entries, len(entries) >= (total or 0), {"project_ids": project_ids,
                                                           "dataset_id": dataset_id}
        task_rows = dataset_tasks(reader, project_ids[0], dataset_id)
        return task_rows, True, {"project_ids": project_ids, "dataset_id": dataset_id}

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
    return collected, complete, {"project_ids": project_ids}


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
    expensive = [f for f in include if spec.tier_of(f) in schema.GATED_TIERS]
    if expensive and len(matched) > DETAIL_BUDGET and not confirm_cost:
        # Symmetric with scope.confirm_fanout: a deliberate survey across many rows is
        # legitimate, so the gate offers an explicit way to accept the cost rather than
        # only telling the caller to narrow.
        raise errors.CostGate(
            f"include={expensive} costs one API call per row and {len(matched)} rows match.",
            what=(f"Narrow the filter to at most {DETAIL_BUDGET} rows, or accept the cost "
                  "explicitly."),
            field="include", given=include,
            estimated_cost={"api_calls": len(matched)}, limit=DETAIL_BUDGET,
            retry_with=[
                {"note": f"add a filter so matched <= {DETAIL_BUDGET}, then re-send include"},
                {"confirm_detail_cost": True,
                 "note": f"accept {len(matched)} API calls for a deliberate full survey"},
                {"note": "sampling is not offered for size figures: they are heavy-tailed, "
                         "so a small-sample estimate would mislead"},
            ],
        )
    granted, notes = set(include), []
    if expensive:
        reader.cost.tier_ceiling = max(
            (spec.tier_of(f) for f in include), key=schema.tier_rank, default="list")

    if entity == "process":
        # config_location and code_location read as DERIVED but are only produced by the
        # detail fetch, so they must count toward the cost gate like any per-row call.
        if len(matched) > DETAIL_BUDGET and not confirm_cost:
            raise errors.CostGate(
                f"include={include} needs one API call per process and {len(matched)} "
                "processes match.",
                what=(f"Narrow the filter to at most {DETAIL_BUDGET}, or accept the cost "
                      "explicitly."),
                field="include", given=include,
                estimated_cost={"api_calls": len(matched)}, limit=DETAIL_BUDGET,
                retry_with=[{"confirm_detail_cost": True}],
            )
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

    scanned, complete, scope_echo = _collect(reader, entity, request)
    lineage = _lineage(scanned) if entity == "dataset" else {}
    ctx = reader.ctx(lineage)

    node = Q.resolve_filter(Q.parse_filter(request.get("filter")), spec, ctx)
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
    facets = Q.compute_facets(matched, spec, labels=labels)

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
            matched, request["group_by"], metrics, ctx,
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
                     facet_priority=spec.facet_priority, notes=notes,
                     max_tokens=max_tokens)

    shape = Q.compute_shape(matched, spec, ctx, granted=granted)
    exemplars = Q.pick_exemplars(matched, spec, facets)
    next_calls = Q.suggest(matched, echoed, spec, facets, ctx, shape)
    reader.cost.strategy = "snapshot+client_filter"

    counts = Counts(matched=len(matched), scanned=len(scanned), scan_complete=complete)
    return build(entity=entity, query=echoed, counts=counts, cost=reader.cost,
                 rows=matched, facets=facets, shape=shape, next_calls=next_calls,
                 exemplars=exemplars, projections=projections,
                 facet_priority=spec.facet_priority, notes=notes, max_tokens=max_tokens)


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
    scanned, complete, scope_echo = _collect(reader, entity,
                                             {**request, "include_archived": True})
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
                 notes=notes,
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


# `include` on a detail-or-worse tier costs one call per row, so it is gated.
DETAIL_BUDGET = 25


def _sorted(rows: list, sort: list, spec: schema.EntitySpec) -> list:
    """Sort numerics numerically.

    Stringifying every key made `total_size_bytes`, `file_count`, `exit_code` and
    `task_id` sort lexicographically, so 9000 came before 10000.
    """
    def ordering(field: str):
        def key(row):
            value = row.get(field)
            if value is None:
                return (2, 0.0, "")
            if isinstance(value, bool):
                return (1, float(value), "")
            if isinstance(value, (int, float)):
                return (0, float(value), "")
            return (1, 0.0, str(value))
        return key

    for entry in reversed(sort or spec.default_sort):
        descending = entry.startswith("-")
        rows = sorted(rows, key=ordering(entry.lstrip("-")), reverse=descending)
    return rows

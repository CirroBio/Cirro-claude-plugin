"""The four read verbs: query, get, summarize, describe."""
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from .. import errors, query as Q, schema, summarize as S
from ..cache import SnapshotCache, FRESH_DEFAULT, manifest_ttl
from ..envelope import (CONTRACT, Cost, Counts, DEFAULT_MAX_TOKENS,
                        ROWS_ABSENT, build, estimate_tokens)
from . import portal, rows as R

# Deliberately below the SDK's silent 10,000 default: we would rather report a
# truncated scan than quietly answer from a partial one.
SCAN_LIMIT = 50_000
PAGE_SIZE = 5_000

# `include` on a detail-or-worse tier costs one call per row, so it is gated.
DETAIL_BUDGET = 25
FANOUT_LIMIT = 25
MANIFEST_LIST_LIMIT = 20_000


class Reader:
    def __init__(self, request: dict):
        self.request = request
        self.base_url = request["base_url"]
        self.session_dir = request["session_dir"]
        self.freshness = request.get("freshness", FRESH_DEFAULT)
        self.cost = Cost()
        self.now = datetime.now(timezone.utc)
        self._client = None
        self._username = None
        self._cache = None

    # ---------------------------------------------------------------- plumbing

    @property
    def client(self):
        if self._client is None:
            from cirro.cirro_client import CirroApi
            auth, _ = portal.build_auth(self.base_url)
            self._username = portal.identity(auth)
            self._client = CirroApi(auth_info=auth, base_url=self.base_url,
                                    user_agent="cirro-claude-plugin")
        return self._client

    @property
    def username(self) -> Optional[str]:
        if self._username is None:
            self.client  # noqa: B018 -- forces auth, which sets _username
        return self._username

    @property
    def cache(self) -> SnapshotCache:
        if self._cache is None:
            self._cache = SnapshotCache(Path(self.session_dir), self.base_url, self.username)
        return self._cache

    def ctx(self, lineage: Optional[dict] = None) -> Q.Ctx:
        return Q.Ctx(username=self.username, now=self.now, lineage=lineage or {})

    def _paginate(self, getter: Callable, kind: str,
                  scan_limit: int = SCAN_LIMIT) -> Tuple[list, bool]:
        """Page it ourselves.

        `get_all_records` checks its ceiling only after extending, so it overshoots
        by up to a full page and, worse, stops with no signal that more exists. Here
        an unconsumed next_token at the ceiling becomes scan_complete=False.
        """
        items, token = [], None
        while True:
            response = getter(token, PAGE_SIZE)
            self.cost.call(kind)
            if not response:
                return items, True
            items.extend(response.data or [])
            token = getattr(response, "next_token", None)
            from cirro_api_client.v1.types import Unset
            if not token or isinstance(token, Unset):
                return items, True
            if len(items) >= scan_limit:
                return items, False

    def _snapshot(self, kind: str, loader: Callable, *, ttl_s: Optional[int] = None,
                  **scope) -> Tuple[list, bool]:
        hit = self.cache.get(kind, ttl_s=ttl_s, freshness=self.freshness, **scope)
        if hit is not None:
            self.cost.snapshot(hit.key, hit.age_s, hit.ttl_s, hit.item_count, hit.fresh)
            return hit.items, hit.meta.get("scan_complete", True)
        items, complete = loader()
        snap = self.cache.put(kind, items, meta={"scan_complete": complete},
                              ttl_s=ttl_s, **scope)
        # A miss: this came from the network. Recording it as fresh booked every cold
        # fetch as a cache hit, so fraction_from_cache always read 1.0.
        self.cost.snapshot(snap.key, 0.0, snap.ttl_s, snap.item_count, fresh=False)
        return items, complete

    # ---------------------------------------------------------------- fetchers

    def processes(self, include_archived: bool = False) -> list:
        def load():
            from cirro_api_client.v1.api.processes import get_processes
            found = get_processes.sync(
                client=self.client._api_client,
                include_archived=include_archived) or []
            self.cost.call("processes.list")
            return [R.process(p) for p in found], True
        items, _ = self._snapshot("processes", load, archived=include_archived)
        return items

    def process_names(self) -> dict:
        return {p["id"]: p["name"] for p in self.processes(include_archived=True)}

    def projects(self) -> list:
        def load():
            from cirro_api_client.v1.api.projects import get_projects
            found = get_projects.sync(client=self.client._api_client) or []
            self.cost.call("projects.list")
            return [R.project(p) for p in found], True
        items, _ = self._snapshot("projects", load)
        return items

    def datasets(self, project_id: str) -> Tuple[list, bool]:
        def load():
            from cirro_api_client.v1.api.datasets import get_datasets
            from cirro_api_client.v1.api.sharing import get_shared_datasets
            from cirro_api_client.v1.models import ShareType

            raw, complete = self._paginate(
                lambda token, limit: get_datasets.sync(
                    project_id=project_id, client=self.client._api_client,
                    next_token=token, limit=limit),
                "datasets.list")
            names = self.process_names()
            out = [R.dataset(d, now=self.now, process_names=names) for d in raw]

            # A project listing is really several scopes: datasets it owns plus every
            # dataset reaching it through a subscribed share.
            try:
                shares = self.client.shares.list(project_id=project_id,
                                                 share_type=ShareType.SUBSCRIBER) or []
                self.cost.call("shares.list")
            except Exception:
                # Datasets reaching this project through a share would be missing, so
                # the scan is not complete and must not claim to be.
                shares = []
                complete = False
            for share in shares:
                shared, share_complete = self._paginate(
                    lambda token, limit, sid=share.id: get_shared_datasets.sync(
                        project_id=project_id, share_id=sid,
                        client=self.client._api_client, next_token=token, limit=limit),
                    "datasets.list_shared")
                complete = complete and share_complete
                for item in shared:
                    row = R.dataset(item, now=self.now, process_names=names)
                    row["share"] = {"id": R.plain(share.id), "name": R.plain(share.name)}
                    out.append(row)
            return out, complete
        return self._snapshot("datasets", load, project=project_id)

    def samples(self, project_id: str) -> Tuple[list, bool]:
        def load():
            found = self.client.metadata.get_project_samples(project_id=project_id) or []
            self.cost.call("samples.list")
            return [R.sample(s) for s in found], True
        return self._snapshot("samples", load, project=project_id)

    def references(self, project_id: str) -> Tuple[list, bool]:
        def load():
            found = self.client.references.get_for_project(project_id=project_id) or []
            self.cost.call("references.list")
            return [R.reference(r) for r in found], True
        return self._snapshot("references", load, project=project_id)

    # ---------------------------------------------------------------- resolution

    def resolve_project(self, given: str) -> dict:
        """ID first, then exact name, then case-insensitive. Never fuzzy."""
        available = self.projects()
        for row in available:
            if row["id"] == given:
                return row
        exact = [r for r in available if r["name"] == given]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise errors.AmbiguousName(
                f"{len(exact)} projects are named {given!r}.",
                what="Use the project ID.",
                field="scope.project", given=given,
                candidates=[{"id": r["id"], "name": r["name"],
                             "organization": r.get("organization")} for r in exact],
                retry_with=[{"scope": {"project_ids": [r["id"]]}} for r in exact],
            )
        loose = [r for r in available if r["name"].lower() == str(given).lower()]
        if len(loose) == 1:
            return loose[0]
        near = _near_misses(given, [r["name"] for r in available])
        raise errors.NotFound(
            f"No project matches {given!r}.",
            what="Pick a project you can see, by name or ID.",
            field="scope.project", given=given,
            candidates=[{"name": name} for name in near],
            retry_with=[{"entity": "project"}]
            + [{"scope": {"project": name}} for name in near[:3]],
            visible_projects=len(available),
        )

    def scope_project_ids(self, scope: dict) -> list:
        if scope.get("project_ids"):
            return list(scope["project_ids"])
        if scope.get("project"):
            return [self.resolve_project(scope["project"])["id"]]
        if scope.get("projects") == "*":
            ids = [p["id"] for p in self.projects()]
            if len(ids) > FANOUT_LIMIT and not scope.get("confirm_fanout"):
                raise errors.CostGate(
                    f"Scanning all {len(ids)} visible projects needs one listing call per "
                    "project.",
                    what="Narrow the scope, or confirm the fan-out explicitly.",
                    field="scope", given=scope,
                    estimated_cost={"api_calls": len(ids)}, limit=FANOUT_LIMIT,
                    retry_with=[
                        {"scope": {"projects": "*", "confirm_fanout": True}},
                        {"entity": "project", "note": "list projects first, then pick"},
                    ],
                )
            return ids
        if isinstance(scope.get("projects"), list):
            return [self.resolve_project(p)["id"] for p in scope["projects"]]
        raise errors.ScopeRequired(
            "This entity is scoped to a project and no project was given.",
            what="Datasets, samples and references live inside a project.",
            field="scope",
            retry_with=[{"entity": "project", "note": "list projects to pick one"},
                        {"scope": {"project": "PROJECT_NAME_OR_ID"}}],
        )


def _near_misses(given: str, names: list, limit: int = 5) -> list:
    import difflib
    close = difflib.get_close_matches(str(given), names, n=limit, cutoff=0.5)
    lowered = str(given).lower()
    substring = [n for n in names if lowered in n.lower() and n not in close]
    return (close + substring)[:limit]


# ------------------------------------------------------------------------ helpers


def _lineage(rows: list) -> dict:
    return {r["id"]: r.get("source_dataset_ids") or [] for r in rows if r.get("id")}


# Fields that are free once their source field has been fetched, so they should
# travel with it rather than being requested separately.
_DERIVED_COMPANIONS = {"total_size_bytes": ["total_size_human"]}


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
            entries, total, _artifacts = _manifest(
                reader, project_ids[0], dataset_id,
                is_terminal=_is_terminal(reader, project_ids[0], dataset_id))
            return entries, len(entries) >= (total or 0), {"project_ids": project_ids,
                                                           "dataset_id": dataset_id}
        task_rows = _tasks(reader, project_ids[0], dataset_id)
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


def _is_terminal(reader: Reader, project_id: str, dataset_id: str) -> bool:
    """Whether the dataset can still change, read from the cached project listing."""
    datasets, _ = reader.datasets(project_id)
    row = next((d for d in datasets if d["id"] == dataset_id), None)
    return bool(row and row.get("is_terminal"))


def _manifest(reader: Reader, project_id: str, dataset_id: str,
              *, file_limit: int = MANIFEST_LIST_LIMIT,
              is_terminal: bool = True) -> Tuple[list, Optional[int], list]:
    def load():
        from cirro_api_client.v1.api.datasets import get_dataset_manifest
        manifest = get_dataset_manifest.sync(
            project_id=project_id, dataset_id=dataset_id,
            client=reader.client._api_client, file_limit=file_limit)
        reader.cost.call("datasets.manifest", probe=True)
        if manifest is None:
            return [], True
        entries = [R.file_entry(f) for f in (R.plain(manifest.files) or [])]
        total = R.plain(manifest.total_files)
        artifacts = [R.plain(getattr(a, "type_", None)) for a in
                     (getattr(manifest, "artifacts", None) or [])]
        return [{"entries": entries, "total_files": total, "artifacts": artifacts}], True

    # A running dataset's file list changes, so it must not be cached for a day.
    kind = "manifest_terminal" if is_terminal else "manifest_active"
    payload, _ = reader._snapshot(
        kind, load, ttl_s=manifest_ttl(is_terminal),
        dataset=dataset_id, limit=file_limit)
    if not payload:
        return [], 0, []
    blob = payload[0]
    return blob["entries"], blob.get("total_files"), blob.get("artifacts") or []


def file_count(reader: Reader, project_id: str, dataset_id: str) -> Optional[int]:
    """One small call: get_dataset_manifest(file_limit=1) reports total_files.

    This is why `file_count` is a `probe` tier rather than `detail` -- it does not
    need the full dataset record, and the SDK's own get_assets_listing misses it.
    """
    from cirro_api_client.v1.api.datasets import get_dataset_manifest
    manifest = get_dataset_manifest.sync(project_id=project_id, dataset_id=dataset_id,
                                         client=reader.client._api_client, file_limit=1)
    reader.cost.call("datasets.manifest", probe=True)
    return R.plain(manifest.total_files) if manifest else None


def _detail(reader: Reader, project_id: str, dataset_id: str,
            base: Optional[dict] = None) -> dict:
    detail = reader.client.datasets.get(project_id=project_id, dataset_id=dataset_id)
    reader.cost.call("datasets.get", detail=True)
    if detail is None:
        raise errors.NotFound(
            f"Dataset {dataset_id!r} not found in project {project_id!r}.",
            what="Check the dataset ID and project.",
            retry_with=[{"entity": "dataset", "scope": {"project_ids": [project_id]}}],
        )
    return R.dataset_detail(detail, base, now=reader.now,
                            process_names=reader.process_names())


def _tasks(reader: Reader, project_id: str, dataset_id: str) -> list:
    detail = reader.client.datasets.get(project_id=project_id, dataset_id=dataset_id)
    reader.cost.call("datasets.get", detail=True)
    executor = None
    if detail is not None:
        process_id = R.plain(detail.process_id)
        executor = next((p["executor"] for p in reader.processes(include_archived=True)
                         if p["id"] == process_id), None)
    found = reader.client.execution.get_tasks_for_execution(project_id=project_id,
                                                            dataset_id=dataset_id)
    reader.cost.call("execution.tasks")
    if found is None:
        # The API returns None rather than [] for executors with no task model.
        # Reporting an empty list would let an agent conclude nothing failed.
        raise errors.WrongExecutor(
            f"This dataset's executor ({executor or 'unknown'}) does not expose a task list.",
            what="Task-level inspection is only available for Nextflow executions.",
            field="entity", given="task",
            executor=executor,
            retry_with=[
                {"verb": "summarize", "of": "logs", "dataset_id": dataset_id},
                {"verb": "get", "entity": "dataset", "id": dataset_id,
                 "include": ["status_message"]},
            ],
        )
    return [R.task(t, index) for index, t in enumerate(found)]


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
            row.update(_process_detail(reader, row["id"], row))
        return granted, notes

    if entity != "dataset":
        return granted, notes

    wants_detail = any(spec.tier_of(f) == schema.DETAIL for f in include)
    wants_probe = any(spec.tier_of(f) == schema.PROBE for f in include)
    for row in matched:
        project_id = row.get("project_id") or (project_ids[0] if project_ids else None)
        if wants_detail:
            row.update(_detail(reader, project_id, row["id"], row))
        elif wants_probe and "file_count" in include:
            row["file_count"] = file_count(reader, project_id, row["id"])
    if wants_detail and wants_probe:
        notes.append("file_count came free from the same detail payload; no probe calls "
                     "were needed")
    return granted, notes


def _process_detail(reader: Reader, process_id: str, base: dict) -> dict:
    """One call per process. `config_source` lives only on the detail payload."""
    detail = reader.client.processes.get(process_id=process_id)
    reader.cost.call("processes.get", detail=True)
    if detail is None:
        raise errors.NotFound(
            f"Process {process_id!r} not found.",
            what="Check the process ID.",
            retry_with=[{"verb": "query", "entity": "process"}],
        )
    return R.process_detail(detail, base)


# ------------------------------------------------------------------------- verbs


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


def _resolve_one(entity: str, given: str, scanned: list, spec: schema.EntitySpec,
                 scope_echo: dict) -> dict:
    for row in scanned:
        if row.get("id") == given:
            return row
    exact = [r for r in scanned if r.get("name") == given]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise errors.AmbiguousName(
            f"{len(exact)} {entity}s are named {given!r}. Names are not unique.",
            what="Use the ID.", field="id", given=given,
            candidates=[{"id": r.get("id"), "name": r.get("name"),
                         "created_at": r.get("created_at"),
                         "created_by": r.get("created_by")} for r in exact[:10]],
            retry_with=[{"verb": "get", "entity": entity, "id": r.get("id"),
                         "scope": scope_echo} for r in exact[:5]],
        )
    near = _near_misses(given, [r.get("name") or "" for r in scanned])
    raise errors.NotFound(
        f"No {entity} matches {given!r} in this scope.",
        what="Check the name, or widen the scope.",
        field="id", given=given,
        candidates=[{"name": name} for name in near],
        retry_with=[{"verb": "query", "entity": entity, "scope": scope_echo}]
        + [{"verb": "get", "entity": entity, "name": name, "scope": scope_echo}
           for name in near[:3]],
    )


def _project_storage(reader: Reader, project_id: str) -> dict:
    try:
        metrics = reader.client.metrics.get_for_project(project_id=project_id)
        reader.cost.call("metrics.get", probe=True)
    except Exception:
        return {"storage_bytes": None,
                "storage_note": "the metrics API did not return data for this project"}
    records = R.plain(getattr(metrics, "storage_metrics", None)) or []
    best, at = None, None
    for record in getattr(metrics, "storage_metrics", None) or []:
        value = R.plain(getattr(record, "value", None))
        stamp = R.plain(getattr(record, "date", None) or getattr(record, "timestamp", None))
        if isinstance(value, (int, float)) and (at is None or str(stamp) >= str(at)):
            best, at = value, stamp
    return {
        "storage_bytes": int(best) if best is not None else None,
        "storage_human": S.human_bytes(int(best)) if best is not None else None,
        "storage_as_of": at,
        # Daily records that lag; saying so beats implying a live number.
        "storage_note": (f"latest of {len(records)} daily storage records;"
                         " may lag by up to 48h"),
    }


SUMMARIZERS = ("files", "logs", "tasks", "samples", "params", "trace")


def do_summarize(request: dict) -> dict:
    of = request.get("of")
    if of not in SUMMARIZERS:
        raise errors.InvalidQuery(
            f"Unknown summarize target {of!r}.",
            what="Pick a supported summary.", field="of", given=of,
            retry_with=[{"verb": "summarize", "of": name} for name in SUMMARIZERS],
        )
    reader = Reader(request)
    scope = request.get("scope") or {}
    project_ids = reader.scope_project_ids(scope)
    project_id = project_ids[0]
    dataset_id = request.get("dataset_id") or scope.get("dataset_id")
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["summarize"])

    if of == "samples":
        sample_rows, complete = reader.samples(project_id)
        schema_keys = None
        try:
            form = reader.client.metadata.get_project_schema(project_id=project_id)
            reader.cost.call("metadata.schema")
            spec_json = getattr(form, "form_spec_json", None) or {}
            props = ((spec_json.get("properties")
                      if isinstance(spec_json, dict) else None) or {})
            schema_keys = list(props)
        except Exception:
            pass
        body = S.samples(sample_rows, schema_keys=schema_keys)
        reader.cost.strategy = "snapshot"
        group_keys = [f"metadata.{key}"
                      for key in body.get("candidate_group_keys", [])[:1]]
        next_calls = [{
            "id": "nc_group",
            "why": "group by a metadata key that is actually worth grouping on",
            "call": {"verb": "query", "entity": "sample",
                     "scope": {"project_ids": project_ids},
                     "group_by": group_keys or ["created_at:month"],
                     "metrics": ["count"]},
        }]
        return _summary_envelope(reader, request, of, body, complete,
                                 {"project_ids": project_ids}, max_tokens,
                                 next_calls=next_calls)

    if not dataset_id:
        raise errors.ScopeRequired(
            f"summarize {of} needs a dataset_id.",
            what="Name the dataset to summarize.", field="dataset_id",
            retry_with=[{"verb": "query", "entity": "dataset",
                         "scope": {"project_ids": project_ids}}],
        )

    if of == "files":
        limit = int(request.get("file_limit") or MANIFEST_LIST_LIMIT)
        entries, total, artifacts = _manifest(
            reader, project_id, dataset_id, file_limit=limit,
            is_terminal=_is_terminal(reader, project_id, dataset_id))
        all_entries = entries
        glob = request.get("glob")
        if glob:
            import fnmatch
            entries = [e for e in entries
                       if fnmatch.fnmatch(e["path"].lower(), glob.lower())]
        body = S.files(entries, total_files=total, artifact_types=artifacts)
        if glob:
            body["filtered_by_glob"] = glob
            body["note"] = (f"{len(entries)} of {total} files match {glob!r}; the figures "
                            "above describe the matching subset")
        reader.cost.strategy = "manifest_probe"
        # Honest completeness: the manifest is capped at file_limit.
        complete = total is None or len(all_entries) >= total
        return _summary_envelope(reader, request, of, body, complete,
                                 {"project_ids": project_ids, "dataset_id": dataset_id},
                                 max_tokens, next_calls=[{
                                     "id": "nc_read",
                                     "why": "read one file's contents without downloading",
                                     "call": {"verb": "summarize", "of": "files",
                                              "dataset_id": dataset_id,
                                              "scope": {"project_ids": project_ids},
                                              "glob": (body.get("suggested_globs") or
                                                       [{"glob": "*"}])[0]["glob"]}}])

    if of == "logs":
        text = reader.client.execution.get_execution_logs(project_id=project_id,
                                                          dataset_id=dataset_id) or ""
        reader.cost.call("execution.logs")
        body = S.logs(text, head=int(request.get("head") or 30),
                      tail=int(request.get("tail") or 60))
        if request.get("raw"):
            body["raw"] = _raw_slice(text, request)
        reader.cost.strategy = "execution_live"
        next_calls = [{
            "id": "nc_tasks",
            "why": "find the root-cause task rather than reading more log",
            "call": {"verb": "summarize", "of": "tasks", "dataset_id": dataset_id,
                     "scope": {"project_ids": project_ids}},
        }]
        return _summary_envelope(reader, request, of, body, True,
                                 {"project_ids": project_ids, "dataset_id": dataset_id},
                                 max_tokens, next_calls=next_calls)

    if of == "tasks":
        task_rows = _tasks(reader, project_id, dataset_id)
        primary = _primary_failed_index(reader, project_id, dataset_id, task_rows)
        body = S.tasks(task_rows, primary_failed_index=primary)
        reader.cost.strategy = "execution_live"
        next_calls = []
        if body.get("failed"):
            root = body["failed"][0]
            next_calls.append({
                "id": "nc_task_logs",
                "why": f"read the log of the root-cause task ({root.get('name')})",
                "call": {"verb": "summarize", "of": "logs", "dataset_id": dataset_id,
                         "scope": {"project_ids": project_ids},
                         "task_id": root.get("task_id")}})
        next_calls.append({
            "id": "nc_trace", "why": "check whether resources were the cause",
            "call": {"verb": "summarize", "of": "trace", "dataset_id": dataset_id,
                     "scope": {"project_ids": project_ids}}})
        return _summary_envelope(reader, request, of, body, True,
                                 {"project_ids": project_ids, "dataset_id": dataset_id},
                                 max_tokens, next_calls=next_calls)

    if of == "params":
        row = _detail(reader, project_id, dataset_id)
        defaults, declared, required = _spec_shape(reader, row.get("process_id"))
        body = S.params(row.get("params") or {}, defaults=defaults, declared=declared,
                        required=required)
        body["process"] = row.get("process")
        reader.cost.strategy = "detail+parameter_spec"
        next_calls = [{
            "id": "nc_spec",
            "why": "read the full parameter spec before changing params",
            "call": {"verb": "get", "entity": "process", "id": row.get("process_id"),
                     "include": ["custom_settings"]},
        }]
        return _summary_envelope(reader, request, of, body, True,
                                 {"project_ids": project_ids, "dataset_id": dataset_id},
                                 max_tokens, next_calls=next_calls)

    # trace
    body = _trace_body(reader, project_id, dataset_id)
    reader.cost.strategy = "artifact_read"
    return _summary_envelope(reader, request, of, body, True,
                             {"project_ids": project_ids, "dataset_id": dataset_id},
                             max_tokens, next_calls=[])


def _raw_slice(text: str, request: dict) -> dict:
    line_range = request.get("line_range")
    byte_range = request.get("byte_range")
    if line_range:
        lines = text.splitlines()
        # 1-based and clamped: a 0 start would index from the end of the list.
        start = max(1, int(line_range[0]))
        end = max(start, int(line_range[1]))
        return {"line_range": [start, end], "text": "\n".join(lines[start - 1:end])}
    if byte_range:
        start, end = int(byte_range[0]), int(byte_range[1])
        return {"byte_range": [start, end], "text": text[start:end]}
    raise errors.InvalidQuery(
        "raw=true needs a line_range or byte_range.",
        what="Raw log text is only served in bounded slices.",
        field="raw",
        retry_with=[{"verb": "summarize", "of": "logs", "raw": True,
                     "line_range": [1, 200]}],
    )


def _primary_failed_index(reader: Reader, project_id: str, dataset_id: str,
                          task_rows: list) -> Optional[int]:
    """Ask the SDK which task is the root cause; fall back to the earliest failure."""
    try:
        from cirro.sdk.dataset import DataPortalDataset
        detail = reader.client.datasets.get(project_id=project_id, dataset_id=dataset_id)
        wrapped = DataPortalDataset(detail, reader.client)
        primary = wrapped.primary_failed_task
        if primary is not None:
            return getattr(primary, "task_id", None)
    except Exception:
        pass
    failed = [t for t in task_rows if "FAIL" in str(t.get("status", "")).upper()]
    failed.sort(key=lambda t: str(t.get("started_at") or ""))
    return failed[0]["task_id"] if failed else None


def _spec_shape(reader: Reader, process_id: Optional[str]) -> Tuple[dict, Optional[list],
                                                                    Optional[list]]:
    if not process_id:
        return {}, None, None
    try:
        spec = reader.client.processes.get_parameter_spec(process_id=process_id)
        reader.cost.call("processes.parameter_spec")
    except Exception:
        return {}, None, None
    form = getattr(spec, "form_spec_json", None) or {}
    properties = (form.get("properties") if isinstance(form, dict) else None) or {}
    defaults, declared = {}, []
    required = list(form.get("required") or []) if isinstance(form, dict) else []

    def walk(props: dict) -> None:
        for key, value in props.items():
            if not isinstance(value, dict):
                continue
            if value.get("type") == "object" and "properties" in value:
                walk(value["properties"])
                continue
            declared.append(key)
            if "default" in value:
                defaults[key] = value["default"]

    walk(properties)
    return defaults, declared, required


def _trace_body(reader: Reader, project_id: str, dataset_id: str) -> dict:
    try:
        from cirro.sdk.dataset import DataPortalDataset
        detail = reader.client.datasets.get(project_id=project_id, dataset_id=dataset_id)
        reader.cost.call("datasets.get", detail=True)
        wrapped = DataPortalDataset(detail, reader.client)
        frame = wrapped.get_trace()
        reader.cost.call("artifact.trace")
    except errors.AgentError:
        # An auth expiry or a missing dataset must keep its own diagnosis rather than
        # being reported as a missing trace artifact.
        raise
    except Exception as exc:
        raise errors.NotFound(
            f"No workflow trace is available for this dataset: {exc}",
            what="A trace is only published by Nextflow runs that got far enough to emit one.",
            retry_with=[{"verb": "summarize", "of": "tasks", "dataset_id": dataset_id},
                        {"verb": "summarize", "of": "logs", "dataset_id": dataset_id}],
        )
    records = frame.to_dict(orient="records") if hasattr(frame, "to_dict") else list(frame)
    return S.trace(records)


def _summary_envelope(reader: Reader, request: dict, of: str, body: dict,
                      complete: bool, scope_echo: dict, max_tokens: int,
                      next_calls: list) -> dict:
    envelope = {
        "ok": True,
        "api": {"contract": CONTRACT},
        "query": {"verb": "summarize", "of": of, "scope": scope_echo,
                  "dataset_id": request.get("dataset_id")},
        "result": {"summary_of": of, "scan_complete": complete, of: body,
                   "next_calls": next_calls},
        "cost": reader.cost.to_dict(),
        "budget": {"max_tokens": max_tokens, "degraded": []},
    }
    # Summaries are already collapsed; if one still overruns, drop the most verbose
    # sections in a fixed order rather than cutting a structure open.
    shrinkable = {
        "files": ["tree", "largest", "extensions", "name_patterns"],
        "logs": ["head", "signature_counts", "tail", "error_blocks"],
        "tasks": ["retries", "counts_by_process", "failed"],
        "samples": ["examples", "inconsistencies", "keys"],
        "params": ["defaulted_keys"],
        "trace": ["processes"],
    }.get(of, [])
    for key in shrinkable:
        if estimate_tokens(envelope) <= max_tokens:
            break
        if key in body:
            body.pop(key)
            envelope["budget"]["degraded"].append({"what": f"{of}.{key}", "to": "dropped"})
    envelope["budget"]["used_est"] = estimate_tokens(envelope)
    return envelope


def do_describe(request: dict) -> dict:
    entity = request.get("entity", "dataset")
    spec = schema.get(entity)
    fields = [
        {"field": name, "tier": tier, "means": doc}
        for name, (tier, doc) in spec.fields.items()
    ]
    body = {
        "ok": True,
        "api": {"contract": CONTRACT},
        "query": {"verb": "describe", "entity": entity},
        "result": {
            "entity": entity,
            "id_prefix": spec.id_prefix,
            "scoped_by_project": spec.scoped_by_project,
            "date_field": spec.date_field,
            "fields": fields,
            "tiers": {
                "list": "free -- already in the paged listing",
                "derived": "free -- computed locally from list fields",
                "probe": "one small API call per row",
                "detail": "one full API call per row; gated above "
                          f"{DETAIL_BUDGET} matched rows",
                "manifest": "one or more calls per row, response can be enormous",
                "execution": "as manifest, and only for some executors",
            },
            "projections": spec.projections,
            "facets_computed": spec.facet_priority,
            "text_fields_for_q": spec.text_fields,
            "default_sort": spec.default_sort,
            "operators": sorted(Q.OPS),
            "status_classes": Q.STATUS_CLASSES,
            "date_expressions": ["2026-08-01", "2026-08-01T00:00:00Z", "last-7d",
                                 "last-3m", "2026-Q2", "2026-08", "2026", "today",
                                 "yesterday"],
            "person_expressions": ["@me", "a literal username"],
            "size_expressions": ["10GB", "10GiB", "10000000000"],
            "shorthand_example": "status:FAILED created:last-7d owner:@me name~*RNAseq*",
            "group_by_bins": ["created_at:day|week|month|quarter|year",
                              "total_size_bytes:log2", "status:class"],
        },
        "cost": {"api_calls": 0, "by_kind": {}, "note": "static catalog"},
    }
    body["budget"] = {"max_tokens": int(request.get("max_tokens")
                                        or DEFAULT_MAX_TOKENS["describe"]),
                      "used_est": estimate_tokens(body), "degraded": []}
    return body


# --------------------------------------------------------- small direct listings

def _flat(reader: Reader, request: dict, kind: str, items: list,
          next_calls: Optional[list] = None, note: Optional[str] = None) -> dict:
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["query"])
    envelope = {
        "ok": True, "api": {"contract": CONTRACT},
        "query": {"verb": "list", "of": kind, "scope": request.get("scope") or {}},
        "result": {"of": kind, "count": len(items), "items": items,
                   "next_calls": next_calls or [],
                   **({"note": note} if note else {})},
        "cost": reader.cost.to_dict(),
        "budget": {"max_tokens": max_tokens, "degraded": []},
    }
    while estimate_tokens(envelope) > max_tokens and len(envelope["result"]["items"]) > 3:
        keep = max(3, len(envelope["result"]["items"]) // 2)
        envelope["result"]["items"] = envelope["result"]["items"][:keep]
        envelope["result"]["truncated_to"] = keep
        envelope["budget"]["degraded"].append({"what": "items", "to": str(keep)})
    envelope["budget"]["used_est"] = estimate_tokens(envelope)
    return envelope


def do_list(request: dict) -> dict:
    of = request.get("of")
    reader = Reader(request)
    scope = request.get("scope") or {}

    if of == "folders":
        project_id = reader.scope_project_ids(scope)[0]
        datasets, complete = reader.datasets(project_id)
        UNFILED = "(not in a folder)"
        counts, contributors = {}, {}
        for row in datasets:
            names = row.get("folders") or [UNFILED]
            for name in names:
                counts[name] = counts.get(name, 0) + 1
                contributors.setdefault(name, set()).add(row.get("created_by"))
        items = [
            {"folder": name, "datasets": n,
             "contributors": len(contributors.get(name, ()))}
            for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        note = None
        if not complete:
            note = "the dataset scan was truncated, so these counts are lower bounds"
        return _flat(reader, request, of, items, next_calls=[{
            "id": "nc_in_folder",
            "why": "list the datasets in the largest folder",
            "call": {"verb": "query", "entity": "dataset",
                     "scope": {"project_ids": [project_id]},
                     "filter": {"field": "folders", "op": "has_folder",
                                "value": items[0]["folder"]} if items else None},
        }] if items else [], note=note)

    if of == "reference_types":
        found = reader.client.references.get_types() or []
        reader.cost.call("references.get_types")
        return _flat(reader, request, of, [
            {"name": R.plain(getattr(t, "name", None)),
             "description": R.plain(getattr(t, "description", None)),
             "directory": R.plain(getattr(t, "directory", None))} for t in found])

    if of == "users":
        found = reader.client.users.list() or []
        reader.cost.call("users.list")
        return _flat(reader, request, of, [
            {"username": R.plain(getattr(u, "username", None)),
             "name": R.plain(getattr(u, "name", None)),
             "email": R.plain(getattr(u, "email", None)),
             "organization": R.plain(getattr(u, "organization", None))} for u in found])

    project_id = reader.scope_project_ids(scope)[0]

    if of == "compute_envs":
        found = reader.client.compute_environments.list_environments_for_project(
            project_id=project_id) or []
        reader.cost.call("compute_environments.list")
        return _flat(reader, request, of, [
            {"id": R.plain(getattr(e, "id", None)),
             "name": R.plain(getattr(e, "name", None))} for e in found])

    if of == "shares":
        found = reader.client.shares.list(project_id=project_id) or []
        reader.cost.call("shares.list")
        return _flat(reader, request, of, [
            {"id": R.plain(getattr(s, "id", None)),
             "name": R.plain(getattr(s, "name", None)),
             "share_type": R.plain(getattr(s, "share_type", None)),
             "description": R.plain(getattr(s, "description", None))} for s in found])

    if of == "workspaces":
        found = reader.client.workspaces.list(project_id=project_id) or []
        reader.cost.call("workspaces.list")
        return _flat(reader, request, of, [
            {"id": R.plain(getattr(w, "id", None)),
             "name": R.plain(getattr(w, "name", None)),
             "status": R.plain(getattr(w, "status", None)),
             "environment_id": R.plain(getattr(w, "environment_id", None)),
             "started_at": R.plain(getattr(w, "started_at", None))} for w in found])

    if of == "project_users":
        found = reader.client.projects.get_users(project_id=project_id) or []
        reader.cost.call("projects.get_users")
        return _flat(reader, request, of, [
            {"username": R.plain(getattr(u, "username", None)),
             "role": R.plain(getattr(u, "role", None))} for u in found])

    raise errors.InvalidQuery(
        f"Unknown listing {of!r}.", what="Pick a supported listing.", field="of", given=of,
        retry_with=[{"of": name} for name in ("folders", "reference_types", "users",
                                              "compute_envs", "shares", "workspaces",
                                              "project_users")],
    )


def do_process_params(request: dict) -> dict:
    """The authoritative parameter shape for a process, summarized.

    Read this before constructing params for run_analysis: it is the only place the
    accepted keys, their defaults and which are required are stated.
    """
    reader = Reader(request)
    given = request.get("id") or request.get("name")
    if not given:
        raise errors.InvalidQuery(
            "process_params needs a process id or name.",
            what="Name the process.", field="id",
            retry_with=[{"verb": "query", "entity": "process"}],
        )
    found = reader.processes(include_archived=True)
    match = next((p for p in found if p["id"] == given), None)
    if match is None:
        named = [p for p in found if p["name"] == given]
        if len(named) > 1:
            raise errors.AmbiguousName(
                f"{len(named)} processes are named {given!r}.",
                what="Use the process ID.", field="id", given=given,
                candidates=[{"id": p["id"], "name": p["name"]} for p in named],
                retry_with=[{"verb": "process_params", "id": p["id"]} for p in named],
            )
        if not named:
            near = _near_misses(given, [p["name"] for p in found])
            raise errors.NotFound(
                f"No process matches {given!r}.", what="Pick an available process.",
                field="id", given=given, candidates=[{"name": n} for n in near],
                retry_with=[{"verb": "query", "entity": "process"}],
            )
        match = named[0]

    defaults, declared, required = _spec_shape(reader, match["id"])
    parameters = [
        {"key": key, "required": key in (required or []),
         "default": defaults.get(key, None), "has_default": key in defaults}
        for key in (declared or [])
    ]
    body = {
        "ok": True, "api": {"contract": CONTRACT},
        "query": {"verb": "process_params", "id": match["id"]},
        "result": {
            "process": {"id": match["id"], "name": match["name"],
                        "executor": match["executor"],
                        "uses_sample_sheet": match["uses_sample_sheet"],
                        "documentation_url": match["documentation_url"],
                        "file_requirements_message": match["file_requirements_message"]},
            "n_parameters": len(parameters),
            "required": [p["key"] for p in parameters if p["required"]],
            "parameters": parameters,
            "note": ("Keys not listed here are accepted by the API and ignored by the "
                     "pipeline. plan(run_analysis) reports any you pass that are not "
                     "declared."),
            "next_calls": [{
                "id": "nc_plan", "why": "plan a run once the params are chosen",
                "call": {"tool": "plan", "op": "run_analysis",
                         "args": {"process": match["id"], "name": "OUTPUT_NAME",
                                  "dataset_id": "INPUT_DATASET_ID", "params": {}}}}],
        },
        "cost": reader.cost.to_dict(),
    }
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["query"])
    result = body["result"]
    while estimate_tokens(body) > max_tokens and len(result["parameters"]) > 5:
        keep = max(5, len(result["parameters"]) // 2)
        result["parameters"] = result["parameters"][:keep]
        result["parameters_truncated_to"] = keep
    body["budget"] = {"max_tokens": max_tokens, "used_est": estimate_tokens(body),
                      "degraded": []}
    return body


def do_task_logs(request: dict) -> dict:
    reader = Reader(request)
    project_id = reader.scope_project_ids(request.get("scope") or {})[0]
    dataset_id = request.get("dataset_id")
    task_id = request.get("task_id")
    if dataset_id is None or task_id is None:
        raise errors.InvalidQuery(
            "task_logs needs dataset_id and task_id.",
            what="Identify the task.", field="task_id",
            retry_with=[{"verb": "summarize", "of": "tasks", "dataset_id": dataset_id}],
        )
    # task_id is the caller-facing 0-based index into the task list; the API wants the
    # underlying batch job id, so resolve one to the other before calling.
    task_rows = _tasks(reader, project_id, dataset_id)
    row = next((t for t in task_rows if t["task_id"] == int(task_id)), None)
    if row is None:
        raise errors.NotFound(
            f"This execution has no task {task_id}.",
            what=f"Pick a task_id from 0 to {len(task_rows) - 1}.",
            field="task_id", given=task_id,
            retry_with=[{"verb": "summarize", "of": "tasks", "dataset_id": dataset_id}],
        )
    native = row.get("native_job_id")
    if not native:
        raise errors.NotFound(
            f"Task {task_id} ({row.get('name')}) has no batch job id, so its log cannot "
            "be fetched.",
            what="The task never reached the executor.",
            retry_with=[{"verb": "summarize", "of": "logs", "dataset_id": dataset_id}],
        )
    text = reader.client.execution.get_task_logs(
        project_id=project_id, dataset_id=dataset_id, task_id=str(native)) or ""
    reader.cost.call("execution.task_logs")
    body = S.logs(text)
    if request.get("raw"):
        body["raw"] = _raw_slice(text, request)
    return _summary_envelope(reader, request, "logs", body, True,
                             {"project_ids": [project_id], "dataset_id": dataset_id,
                              "task_id": task_id},
                             int(request.get("max_tokens")
                                 or DEFAULT_MAX_TOKENS["summarize"]), next_calls=[])


CONFIG_SLOTS = ("form", "input", "output", "compute", "preprocess", "documentation",
                "metadata")


def do_process_config(request: dict) -> dict:
    """Read a process's configuration assets, wherever they live.

    For STORAGE the bytes are in Cirro and come back over the revision API. For the
    repo-backed sources the bytes are in git, so report where and let the caller read
    the checkout -- this command never guesses at file contents it cannot see.
    """
    reader = Reader(request)
    given = request.get("id")
    if not given:
        raise errors.InvalidQuery(
            "process_config needs a process id.",
            what="Name the process.", field="id",
            retry_with=[{"verb": "query", "entity": "process"}],
        )

    found = reader.processes(include_archived=True)
    row = next((p for p in found if p["id"] == given), None)
    if row is None:
        near = _near_misses(given, [p["name"] for p in found])
        raise errors.NotFound(
            f"No process {given!r}.", what="Check the process ID.", field="id",
            given=given, candidates=[{"name": n} for n in near],
            retry_with=[{"verb": "query", "entity": "process"}],
        )
    row = _process_detail(reader, row["id"], row)
    source = row.get("config_source")

    result = {
        "process": {"id": row["id"], "name": row["name"], "executor": row["executor"],
                    "is_archived": row.get("is_archived"), "owner": row.get("owner")},
        "config_source": source,
        "config_location": row.get("config_location"),
        "code_location": row.get("code_location"),
        "pipeline_code": row.get("pipeline_code"),
    }

    if source == "STORAGE":
        result.update(_storage_config(reader, row["id"], request))
    elif source in ("WITH_PROCESS", "EXTERNAL"):
        result.update(_repo_config(row))
    else:
        result["note"] = (
            f"config_source is {source!r}. That is only meaningful for a process with "
            "workflow code; ingest data types carry no configuration.")

    envelope = {
        "ok": True, "api": {"contract": CONTRACT},
        "query": {"verb": "process_config", "id": row["id"],
                  "slot": request.get("slot"), "raw": bool(request.get("raw"))},
        "result": result,
        "cost": reader.cost.to_dict(),
    }
    max_tokens = int(request.get("max_tokens") or 8000)
    envelope["budget"] = {"max_tokens": max_tokens,
                          "used_est": estimate_tokens(envelope), "degraded": []}
    return envelope


def _storage_config(reader: Reader, process_id: str, request: dict) -> dict:
    from cirro_api_client.v1.api.processes import (get_latest_process_revision,
                                                   get_process_resource)
    from cirro_api_client.v1.models import ProcessResource

    out: dict = {}
    try:
        response = get_latest_process_revision.sync_detailed(
            process_id, client=reader.client._api_client)
        reader.cost.call("processes.latest_revision")
        revision = response.parsed
        out["revision"] = {
            "number": R.plain(getattr(revision, "revision_number", None)),
            "committed_at": R.plain(getattr(revision, "created_at", None)),
            "commit_message": R.plain(getattr(revision, "commit_message", None)),
        }
        # The token an update must pass as if_match is the revision number as a string,
        # not the per-slot content hash.
        out["update_token"] = str(out["revision"]["number"])
    except Exception as exc:
        out["revision"] = None
        out["note"] = (f"no configuration revisions exist yet ({type(exc).__name__}); "
                       "the process is registered but unconfigured")
        return out

    wanted = [request["slot"]] if request.get("slot") else list(CONFIG_SLOTS)
    raw = bool(request.get("raw"))
    assets = {}
    for slot in wanted:
        try:
            response = get_process_resource.sync_detailed(
                process_id, ProcessResource(slot), client=reader.client._api_client)
            reader.cost.call("processes.get_resource")
            content = R.plain(getattr(response.parsed, "content", None))
        except Exception:
            content = None
        if not content:
            continue
        assets[slot] = _describe_asset(slot, content, raw)
    out["assets"] = assets
    out["slots_present"] = sorted(assets)
    if not raw and assets:
        out["how_to_read_contents"] = (
            "add --raw (optionally with --slot <name>) to get the verbatim file text")
    return out


def _describe_asset(slot: str, content: str, raw: bool) -> dict:
    """Summarize an asset, or hand back its exact bytes when asked."""
    import hashlib
    entry = {
        "bytes": len(content),
        "lines": content.count("\n") + 1,
        "sha256": hashlib.sha256(content.encode()).hexdigest()[:16],
    }
    if slot in ("form", "input", "output", "metadata"):
        try:
            parsed = json.loads(content)
            entry["valid_json"] = True
            entry["shape"] = _json_shape(slot, parsed)
        except ValueError as exc:
            entry["valid_json"] = False
            entry["json_error"] = str(exc)
    if raw:
        entry["content"] = content
    return entry


def _json_shape(slot: str, parsed) -> dict:
    if slot == "form" and isinstance(parsed, dict):
        form = parsed.get("form") or {}
        groups = form.get("properties") or {}
        return {
            "groups": list(groups),
            "n_groups": len(groups),
            "n_fields": sum(len((g or {}).get("properties") or {})
                            for g in groups.values() if isinstance(g, dict)),
            "has_ui_block": bool(parsed.get("ui")),
        }
    if slot == "input" and isinstance(parsed, dict):
        return {"n_mappings": len(parsed), "keys": sorted(parsed)[:40]}
    if slot == "output" and isinstance(parsed, dict):
        return {"n_commands": len(parsed.get("commands") or [])}
    return {"top_level_keys": sorted(parsed)[:20] if isinstance(parsed, dict) else None}


def _repo_config(row: dict) -> dict:
    settings = row.get("custom_settings") or {}
    repo = settings.get("repository")
    branch = settings.get("branch") or "main"
    folder = settings.get("folder") or ".cirro"
    out = {
        "repo": {"repository": repo, "branch": branch, "folder": folder,
                 "repository_type": settings.get("repository_type")},
        "sync": {"status": settings.get("sync_status"),
                 "last_sync": settings.get("last_sync"),
                 "commit_hash": settings.get("commit_hash"),
                 "is_authorized": settings.get("is_authorized")},
        "browse": (f"https://github.com/{repo}/tree/{branch}/{folder}"
                   if repo else None),
        "note": ("the configuration lives in git, not in Cirro -- read it from a "
                 "checkout of that repo, folder and branch"),
    }
    if settings.get("sync_status") == "FAILED":
        out["warning"] = ("the last sync from this repo FAILED, so the configuration "
                          "Cirro is running may be older than what the repo contains")
    if settings.get("is_authorized") is False:
        out["warning"] = ("Cirro is not authorized to read this repository, so it cannot "
                          "sync the configuration at all")
    return out


def do_workflow_spec(request: dict) -> dict:
    """Parse a workflow's declared inputs and parameters. Touches no Cirro API."""
    from pathlib import Path
    from . import workflow_spec as WS

    target = request.get("target")
    if not target:
        raise errors.InvalidQuery(
            "workflow_spec needs a target: a local checkout path or a repository.",
            what="Name the workflow to parse.", field="target",
            retry_with=[{"target": "epi2me-labs/wf-human-variation", "ref": "v2.8.1"},
                        {"target": "/path/to/local/checkout"}],
        )
    cache_root = Path(request.get("cache_root") or ".")
    source = WS.resolve_source(target, request.get("ref"), cache_root)
    root = Path(source["local_path"])
    engine = WS.detect_engine(root)
    parsed = WS.parse(root, engine)
    hints = WS.candidates(parsed["params"])

    wanted_group = request.get("group")
    params = parsed["params"]
    if wanted_group:
        params = [p for p in params if p["group"] == wanted_group]
    if not request.get("include_hidden"):
        shown = [p for p in params if not p["hidden"] and not p["is_infrastructure"]]
    else:
        shown = params

    counts = {
        "params_declared": len(parsed["params"]),
        "params_visible": sum(1 for p in parsed["params"]
                              if not p["hidden"] and not p["is_infrastructure"]),
        "params_hidden": sum(1 for p in parsed["params"] if p["hidden"]),
        "params_infrastructure": sum(1 for p in parsed["params"]
                                     if p["is_infrastructure"]),
        "params_returned": len(shown),
        "groups": len(parsed["groups"]),
        "must_set": sum(1 for p in parsed["params"]
                        if p["declared_required"] and not p["has_default"]),
    }

    result = {
        "engine": parsed["engine"],
        "source": source,
        "entrypoint": parsed["entrypoint"],
        "declared_engine_version": parsed["declared_engine_version"],
        "parse_confidence": parsed["parse_confidence"],
        "counts": counts,
        "groups": parsed["groups"],
        "candidates": hints,
        "params": shown,
        "samplesheet_columns": parsed["samplesheet_columns"],
        "outputs": parsed["outputs"],
        "test_profiles": parsed["test_profiles"],
        "flags": parsed["flags"],
        "note": ("candidates are hints with reasons, not decisions -- classify the "
                 "primary data input, managed parameters and file pickers yourself, "
                 "and leave executor_version unpinned unless a run fails to compile"),
    }
    envelope = {"ok": True, "api": {"contract": CONTRACT},
                "query": {"verb": "workflow_spec", "target": target,
                          "ref": request.get("ref"), "group": wanted_group},
                "result": result,
                "cost": {"api_calls": 0, "by_kind": {}, "note": "local parse only"}}

    max_tokens = int(request.get("max_tokens") or 12000)
    degraded = []
    # Enum lists dominate the payload for pipelines like this one (a 34-value basecaller
    # list); summarize them before dropping anything a caller cannot recover.
    if estimate_tokens(envelope) > max_tokens:
        for entry in result["params"]:
            values = entry.get("enum")
            if values and len(values) > 8:
                entry["enum"] = list(values[:8])
                entry["enum_truncated_to"] = 8
                entry["enum_total"] = len(values)
        degraded.append({"what": "params.enum", "to": "first 8 values"})
    if estimate_tokens(envelope) > max_tokens:
        for entry in result["params"]:
            entry.pop("help_text", None)
        degraded.append({"what": "params.help_text", "to": "dropped"})
    if estimate_tokens(envelope) > max_tokens:
        keep = max(10, len(result["params"]) // 2)
        result["params"] = result["params"][:keep]
        result["params_truncated_to"] = keep
        result["how_to_see_the_rest"] = "re-run with --group <key> to page by group"
        degraded.append({"what": "params", "to": str(keep)})
    envelope["budget"] = {"max_tokens": max_tokens,
                          "used_est": estimate_tokens(envelope), "degraded": degraded}
    return envelope

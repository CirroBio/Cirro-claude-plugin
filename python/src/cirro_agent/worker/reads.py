"""The four read verbs: query, get, summarize, describe.
"""
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple
from .. import errors, formcheck, query as Q, schema
from ..cache import SnapshotCache, FRESH_DEFAULT, manifest_ttl
from ..envelope import CONTRACT, Cost, estimate_tokens
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


# Fields that are free once their source field has been fetched, so they should
# travel with it rather than being requested separately.
_DERIVED_COMPANIONS = {"total_size_bytes": ["total_size_human"]}


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


SUMMARIZERS = ("files", "logs", "tasks", "samples", "params", "trace")


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
    if not isinstance(form, dict):
        return {}, None, None
    # formcheck owns the one correct walk over a form: conditional fields live inside
    # dependencies.<selector>.oneOf[].properties, and walking `properties` alone --
    # as this used to -- reported every conditional parameter as undeclared.
    defaults, declared = {}, []
    for path, field in formcheck._iter_fields(form):
        if isinstance(field, dict) and field.get("type") == "object" \
                and "properties" in field:
            continue
        key = path.rsplit(".", 1)[-1]
        if key not in declared:
            declared.append(key)
        if isinstance(field, dict) and "default" in field and key not in defaults:
            defaults[key] = field["default"]
    required = list(form.get("required") or [])
    return defaults, declared, required


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


CONFIG_SLOTS = ("form", "input", "output", "compute", "preprocess", "documentation",
                "metadata")

"""The shared core every read verb is built on -- no verb lives here.

`Reader` holds what a read needs and nothing a verb decides: the authenticated
client, the snapshot cache backing every listing, the cost accounting the envelope
reports, and the name-to-id resolution a filter or a planner asks for
(`resolve_project`, `resolve_process`, `resolve_named`, `pick`, `near_misses`).

Beside it sits the single implementation of each expensive fetch -- `dataset_detail`,
`dataset_manifest`, `dataset_tasks`, `process_detail`, `process_revision`,
`process_assets`, `spec_shape`, `file_count`, `dataset_cost`, `project_metrics`,
`all_project_metrics` -- so that two verbs asking Cirro the same question pay for it
once and shape the answer identically.

The verbs themselves live in `read_query` (query, get), `read_summaries` (summarize,
task_logs), `read_listings` (list), `read_process` (process_params, process_config)
and `read_chains` (chain_status); each imports from here, and nothing imports them but
`dispatch`. A fetch two callers need belongs here -- the op families plan against this
layer as well, so "two callers" is not always two verbs; a verb does not.
"""
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from .. import errors, formcheck, formgen, query as Q
from ..session import Session
from ..cache import SnapshotCache, FRESH_DEFAULT, manifest_ttl
from ..envelope import Cost
from . import portal, rows as R


# Deliberately below the SDK's silent 10,000 default: we would rather report a
# truncated scan than quietly answer from a partial one.
SCAN_LIMIT = 50_000


PAGE_SIZE = 5_000


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

    @property
    def session(self) -> Session:
        """The one place the request's tenant and folder become a Session."""
        return Session(base_url=self.base_url, dir=self.session_dir)

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
        from cirro_api_client.v1.types import Unset
        while True:
            response = getter(token, PAGE_SIZE)
            self.cost.call(kind)
            if not response:
                return items, True
            items.extend(response.data or [])
            token = getattr(response, "next_token", None)
            if not token or isinstance(token, Unset):
                return items, True
            if len(items) >= scan_limit:
                return items, False

    def _snapshot(self, kind: str, loader: Callable, *, ttl_s: Optional[int] = None,
                  **scope) -> Tuple[list, bool]:
        hit = self.cache.get(kind, ttl_s=ttl_s, freshness=self.freshness, **scope)
        if hit is not None:
            self.cost.snapshot(hit.label, hit.age_s, hit.ttl_s, hit.item_count, hit.fresh)
            return hit.items, hit.meta.get("scan_complete", True)
        items, complete = loader()
        snap = self.cache.put(kind, items, meta={"scan_complete": complete},
                              ttl_s=ttl_s, **scope)
        # A miss: this came from the network. Recording it as fresh booked every cold
        # fetch as a cache hit, so fraction_from_cache always read 1.0.
        self.cost.snapshot(snap.label, 0.0, snap.ttl_s, snap.item_count, fresh=False)
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
            return [R.reference(r, project_id) for r in found], True
        return self._snapshot("references", load, project=project_id)

    # ---------------------------------------------------------------- resolution

    def resolve_project(self, given: str) -> dict:
        """ID first, then exact name, then case-insensitive. Never fuzzy."""
        available = self.projects()

        def brief(row):
            return {"id": row["id"], "name": row["name"],
                    "organization": row.get("organization")}

        def near_retry(near):
            return [{"entity": "project"}] + [{"scope": {"project": n}} for n in near[:3]]

        return resolve_named(
            available, given, entity="project", field="scope.project", loose=True,
            brief=brief, retry=lambda row: {"scope": {"project_ids": [row["id"]]}},
            near_retry=near_retry,
            missing_what="Pick a project you can see, by name or ID.",
            visible_projects=len(available),
        )

    def resolve_process(self, given: str, *, ingest=None) -> dict:
        """ID first, then unique exact name. `ingest` narrows to data types or pipelines."""
        found = self.processes(include_archived=True)
        pool = found if ingest is None else [
            p for p in found if (p["executor"] == "INGEST") == ingest]

        def brief(row):
            return {"id": row["id"], "name": row["name"], "executor": row["executor"],
                    "pipeline_type": row["pipeline_type"]}

        return resolve_named(
            pool, given, entity="process", field="process", brief=brief,
            retry=lambda row: {"process_id": row["id"]},
            near_retry=lambda near: [{"verb": "query", "entity": "process"}],
            missing_what="Pick an available process.",
        )

    def has_parent_processes(self, process_id: str) -> bool:
        """Whether anything can feed this process -- the portal's `hasParentProcesses`.

        Two halves, and both are load-bearing: a process may declare its own
        `parent_process_ids`, or be named in another process's `child_process_ids`.
        `pipeline.context.tsx` unions the two and gates submit on
        `hasParentProcesses && !sourceDatasets.length`, so a process with neither is
        launched from the UI with an empty source list.

        Deliberately not the invokability rule from `pipeline-row-data.tsx`, which
        additionally drops parents carrying no `dataType`. That one decides whether to
        offer a pipeline in a list; this one decides whether an empty input is legal,
        and reading the looser of the two would accept a launch the platform gates.

        The listing is already fetched by `resolve_process`, so this costs no call.
        """
        for row in self.processes(include_archived=True):
            if row["id"] == process_id and row["parent_process_ids"]:
                return True
            if process_id in row["child_process_ids"]:
                return True
        return False

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

    # Collections that the read side listed and the planners re-fetched by hand, each
    # with its own projection. One fetch, one projection, both sides.
    def shares(self, project_id: str) -> list:
        found = self.client.shares.list(project_id=project_id) or []
        self.cost.call("shares.list")
        return [R.share(s) for s in found]

    def workspaces(self, project_id: str) -> list:
        found = self.client.workspaces.list(project_id=project_id) or []
        self.cost.call("workspaces.list")
        return [R.workspace(w) for w in found]

    def compute_environments(self, project_id: str) -> list:
        found = self.client.compute_environments.list_environments_for_project(
            project_id=project_id) or []
        self.cost.call("compute_environments.list")
        return [R.compute_environment(e) for e in found]

    def reference_types(self) -> list:
        found = self.client.references.get_types() or []
        self.cost.call("references.get_types")
        return [R.reference_type(t) for t in found]

    def users(self) -> list:
        found = self.client.users.list() or []
        self.cost.call("users.list")
        return [R.user(u) for u in found]

    def project_users(self, project_id: str) -> list:
        found = self.client.projects.get_users(project_id=project_id) or []
        self.cost.call("projects.get_users")
        return [R.project_user(u) for u in found]


def near_misses(given: str, names: list, limit: int = 5) -> list:
    import difflib
    close = difflib.get_close_matches(str(given), names, n=limit, cutoff=0.5)
    lowered = str(given).lower()
    substring = [n for n in names if lowered in n.lower() and n not in close]
    return (close + substring)[:limit]


def resolve_named(rows: list, given: str, *, entity: str, field: str, brief, retry,
                  near_retry, missing_what: str, loose: bool = False, **detail) -> dict:
    """ID first, then unique exact name, then (optionally) unique case-insensitive name.

    Never fuzzy: a close match appears only as a candidate inside NotFound. This is the
    one implementation -- it existed four times and had drifted, so a name the
    process_params read accepted could be one the run_analysis planner refused.
    """
    for row in rows:
        if row.get("id") == given:
            return row
    exact = [r for r in rows if r.get("name") == given]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise errors.AmbiguousName(
            f"{len(exact)} {entity}s are named {given!r}.",
            what=f"Use the {entity} ID.", field=field, given=given,
            candidates=[brief(r) for r in exact[:10]],
            retry_with=[retry(r) for r in exact[:5]],
        )
    if loose:
        relaxed = [r for r in rows
                   if str(r.get("name") or "").lower() == str(given).lower()]
        if len(relaxed) == 1:
            return relaxed[0]
    near = near_misses(given, [r.get("name") or "" for r in rows])
    raise errors.NotFound(
        f"No {entity} matches {given!r}.",
        what=missing_what, field=field, given=given,
        candidates=[{"name": name} for name in near],
        retry_with=near_retry(near),
        **detail,
    )


def project_metrics(reader: Reader, project_id: str) -> list:
    """Both metrics series for one project, from one cached call.

    The endpoint returns dollar cost by service by month and storage by tier by day
    together, and two reads want one series each, so it is fetched once and tagged
    with the series it came from.
    """
    def load():
        found = reader.client.metrics.get_for_project(project_id=project_id)
        reader.cost.call("metrics.get", probe=True)
        return _series_rows(found, project_id), True

    items, _ = reader._snapshot("metrics", load, project=project_id)
    return items


def all_project_metrics(reader: Reader) -> list:
    """Every visible project's metrics in a single call.

    The only cross-project read in the plugin that does not fan out: one request
    covers every project, so spend across a tenant costs no more than one project's.
    """
    def load():
        found = reader.client.metrics.get_all_metrics() or []
        reader.cost.call("metrics.get_all")
        rows = []
        for record in found:
            rows.extend(_series_rows(record, R.plain(getattr(record, "project_id", None))))
        return rows, True

    items, _ = reader._snapshot("metrics", load, project="*")
    return items


def _series_rows(metrics, project_id: Optional[str]) -> list:
    if metrics is None:
        return []
    rows = []
    for attribute, series in (("costs", "cost"), ("storage_metrics", "storage")):
        for record in getattr(metrics, attribute, None) or []:
            rows.append({"series": series, "project_id": project_id,
                         **R.metric_record(record)})
    return rows


def is_terminal(reader: Reader, project_id: str, dataset_id: str) -> bool:
    """Whether the dataset can still change, read from the cached project listing."""
    datasets, _ = reader.datasets(project_id)
    row = next((d for d in datasets if d["id"] == dataset_id), None)
    return bool(row and row.get("is_terminal"))


def dataset_manifest(reader: Reader, project_id: str, dataset_id: str,
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


def dataset_detail(reader: Reader, project_id: str, dataset_id: str,
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


def dataset_tasks(reader: Reader, project_id: str, dataset_id: str) -> list:
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


def dataset_cost(reader: Reader, project_id: str, dataset_id: str) -> dict:
    """The compute cost of the analysis that produced a dataset.

    Never cached: a running analysis's cost grows as its tasks finish, so a snapshot
    would answer "what did this run cost" with a figure from earlier in the same run.
    A dataset nobody computed -- an upload, or an executor with no task-level
    accounting -- has no cost record. Observed on dev, the endpoint refuses that with
    400 "This dataset does not have an execution associated with it", which the
    dispatcher would otherwise render as a retryable UPSTREAM_ERROR telling the reader
    to check Cirro's service status. It is neither retryable nor a service fault, so
    it becomes the typed refusal `dataset_tasks` raises for the same situation -- and
    never a zero total, which would read as a run that cost nothing.
    """
    from cirro_api_client.v1.errors import BadRequestException, NotFoundException
    if not hasattr(reader.client.execution, "get_cost"):
        # An install predating the pin move has no cost API at all. Left to itself
        # that is an AttributeError, which the dispatcher renders as "retry once,
        # then check service status" -- advice that can never work.
        import importlib.metadata as metadata
        raise errors.RuntimeNotInstalled(
            "The installed Cirro runtime is too old to read dataset costs.",
            what=f"Per-dataset cost arrived in cirro 1.13.1; this runtime has "
                 f"{metadata.version('cirro')}.",
            user_action="Run /cirro:setup to rebuild the runtime against the "
                        "current pin.",
        )
    try:
        found = reader.client.execution.get_cost(project_id=project_id,
                                                 dataset_id=dataset_id)
    except (BadRequestException, NotFoundException):
        found = None
    reader.cost.call("execution.cost")
    if found is None:
        raise errors.WrongExecutor(
            "No compute cost is recorded for this dataset.",
            what="Cost is accounted per analysis task, so a dataset that was uploaded "
                 "rather than produced by a run has none.",
            field="dataset_id", given=dataset_id,
            retry_with=[
                {"verb": "get", "entity": "dataset", "id": dataset_id},
                {"verb": "list", "of": "spend"},
            ],
        )
    return R.dataset_cost(found)


def process_detail(reader: Reader, process_id: str, base: dict) -> dict:
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


def process_revision(reader: Reader,
                     process_id: str) -> Tuple[Optional[dict], Optional[dict]]:
    """The configuration revision a process is serving now, from Cirro storage.

    Three outcomes, and nothing may conflate them. `(revision, None)` is a revision;
    `(None, None)` is a 404, which is the API's answer for a process with no
    configuration in tenant storage, so there genuinely is none; `(None, failure)` is
    anything else, meaning the current revision is *unknown*. Reporting an unknown
    revision as "none" both understates what an upload would replace and sends the
    write with no if_match, dropping the guard against overwriting a revision nobody
    looked at. Only the caller knows what that costs it, so it phrases the refusal.

    `failure` is {"reason", "status_code"}; the revision is projected to plain values.
    """
    from cirro_api_client.v1.api.processes import get_latest_process_revision
    from cirro_api_client.v1.errors import NotFoundException
    try:
        response = get_latest_process_revision.sync_detailed(
            process_id, client=reader.client._api_client)
    except NotFoundException:
        reader.cost.call("processes.latest_revision")
        return None, None
    except Exception as exc:
        reader.cost.call("processes.latest_revision")
        return None, {"reason": str(exc) or exc.__class__.__name__,
                      "status_code": None}
    reader.cost.call("processes.latest_revision")
    if response.status_code >= 400:
        return None, {"reason": f"HTTP {response.status_code}",
                      "status_code": response.status_code}
    number = R.plain(getattr(response.parsed, "revision_number", None))
    return {
        "number": number,
        "committed_at": R.plain(getattr(response.parsed, "created_at", None)),
        "commit_message": R.plain(getattr(response.parsed, "commit_message", None)),
        # The token an update sends as if_match is the revision number as a string,
        # not the per-slot content hash.
        "token": str(number) if number is not None else None,
    }, None


def process_assets(reader: Reader, process_id: str,
                   slots: Optional[list] = None) -> Tuple[dict, dict]:
    """The configuration assets Cirro is serving now, by slot.

    Returns `({slot: content}, {slot: failure})`. A 404 means the slot holds nothing,
    which is ordinary -- most processes have no preprocess.py -- and that slot appears
    in neither map. Any other failure leaves the slot's content unknown and puts it in
    the second map, because an unknown slot silently reported as empty lets a caller
    call the asset New and understate what an upload replaces.

    The default slot list is the whole `formgen.CONFIG_SLOTS` vocabulary: a caller
    iterating a narrower one sees less than Cirro holds.
    """
    from cirro_api_client.v1.api.processes import get_process_resource
    from cirro_api_client.v1.errors import NotFoundException
    from cirro_api_client.v1.models import ProcessResource
    content: dict = {}
    unreadable: dict = {}
    for slot in (slots or formgen.CONFIG_SLOTS):
        try:
            response = get_process_resource.sync_detailed(
                process_id, ProcessResource(slot), client=reader.client._api_client)
        except NotFoundException:
            reader.cost.call("processes.get_resource")
            continue
        except Exception as exc:
            reader.cost.call("processes.get_resource")
            unreadable[slot] = {"reason": str(exc) or exc.__class__.__name__,
                                "status_code": None}
            continue
        reader.cost.call("processes.get_resource")
        if response.status_code >= 400:
            unreadable[slot] = {"reason": f"HTTP {response.status_code}",
                                "status_code": response.status_code}
            continue
        text = R.plain(getattr(response.parsed, "content", None))
        if text:
            content[slot] = text
    return content, unreadable


def spec_shape(reader: Reader, process_id: Optional[str]) -> Optional[list]:
    """Every parameter a process declares, each with the path it must be nested at.

    Returns None when the spec cannot be read, which callers must not confuse with a
    process that declares nothing: the first means "unknown", the second "no
    parameters", and only the first should suppress an undeclared-key warning.

    A record is {"path", "type", "title", "description", "required", "conditional",
    "is_group", "default", "has_default"}. The path is what makes this usable: real
    Cirro forms group their parameters, so `wes` is submitted as `analysis_type.wes`
    and never at the top level. Reporting the leaf name alone described a flat
    parameter surface that no form has -- and because a form does not set
    `additionalProperties: false`, the flat object a caller then built validated
    cleanly, was ignored by the pipeline, and surfaced minutes into a billed run as
    defaults nobody chose.

    Groups are records too, typed `object` and flagged `is_group`. They are the keys
    actually submitted at the top level, so omitting them made a correct run's
    parameters read as undeclared -- the same wrong answer from the other direction.
    """
    if not process_id:
        return None
    try:
        spec = reader.client.processes.get_parameter_spec(process_id=process_id)
        reader.cost.call("processes.parameter_spec")
    except Exception:
        return None
    # `get_parameter_spec` returns the hand-written ParameterSpecification, whose
    # `form_spec_json` is `form_schema.form.additional_properties` -- already the
    # JSONSchema, with `properties` at its root. It is a plain dict, never a generated
    # model, and never the {form, ui} pair. Note this is the OPPOSITE shape from
    # `metadata.get_project_schema`, which does hand back the whole FormSchema; the two
    # look alike and are not, so `declared_schema_keys` exists separately for that one.
    # Unwrapping a `form` key here would descend into any parameter that happens to be
    # named `form` and analyse the wrong subtree.
    form = getattr(spec, "form_spec_json", None)
    if not isinstance(form, dict):
        return None
    # formcheck.walk_form is the one correct walk over a form: conditional fields live
    # inside dependencies.<selector>.oneOf[] and then/else, and walking `properties`
    # alone reported every conditional parameter as undeclared. The empty prefix makes
    # the paths relative to the params object a caller actually submits.
    fields, seen = [], set()
    for record in formcheck.walk_form(form, ""):
        field = record["field"] if isinstance(record["field"], dict) else {}
        # A selector is restated inside each of its own branches, so the same path
        # arrives several times; the first occurrence carries the declaration.
        if record["path"] in seen:
            continue
        seen.add(record["path"])
        is_group = field.get("type") == "object" and "properties" in field
        fields.append({
            "path": record["path"],
            "type": field.get("type") or ("object" if is_group else None),
            # The key name carries the meaning for most parameters and carries none
            # for some: a `command` string means nothing without the description
            # saying where the inputs are mounted and where output must be written.
            "title": field.get("title"),
            "description": field.get("description"),
            "required": record["required"],
            "conditional": record["conditional"],
            "is_group": is_group,
            "default": field.get("default"),
            "has_default": "default" in field,
        })
    return fields


def project_schema_document(reader: Reader, project_id: str) -> Optional[dict]:
    """A project's metadata schema as a plain document, or None if it is not one.

    Exceptions propagate: a summary treats an unreadable schema as "no opinion", while
    a gated schema replacement has to treat it as an unknown baseline and say so, so
    the two callers cannot share one answer to a failed fetch.
    """
    current = reader.client.metadata.get_project_schema(project_id=project_id)
    reader.cost.call("metadata.schema")
    document = current.to_dict() if hasattr(current, "to_dict") else current
    return document if isinstance(document, dict) else None


def declared_schema_keys(document: Optional[dict]) -> Optional[list]:
    """Every metadata key a FormSchema document declares, or None if it declares them
    in no shape this recognises.

    `metadata.get_project_schema` returns the generated FormSchema, whose fields are
    form / ui / metadataRequirements -- there is NO `form_spec_json` on it, and reading
    that absent attribute is what made a schema replacement render "keys removed []"
    while dropping keys, and made every observed metadata key report as drift. Any
    unrecognised top-level key stays in additional_properties, so the bare JSONSchema
    the update operation itself sends round-trips at the document root.

    Which half a tenant populates is not fixed, so both are read and the union
    reported: an approval that must not understate a removal has to treat a key seen
    anywhere as one that could be dropped. Callers distinguish an empty document (which
    declares nothing) from an unrecognised one (which is unknown) themselves.
    """
    if not isinstance(document, dict):
        return None
    found, keys = False, []
    for block in (document.get("form"), document.get("metadataRequirements"), document):
        props = block.get("properties") if isinstance(block, dict) else None
        if isinstance(props, dict):
            found = True
            keys.extend(key for key in props if key not in keys)
    return sorted(keys) if found else None


def portal_dataset(reader: Reader, project_id: str, dataset_id: str):
    """The SDK's DataPortalDataset for one dataset, with the detail fetch booked.

    Call sites kept building this by hand and one of them forgot the cost call; a
    fourth hand-built copy had grown back in `plan_update_samplesheet` by the time
    anyone looked. Three callers now, all through here.
    """
    from cirro.sdk.dataset import DataPortalDataset
    detail = reader.client.datasets.get(project_id=project_id, dataset_id=dataset_id)
    reader.cost.call("datasets.get", detail=True)
    return DataPortalDataset(detail, reader.client)


def pick(rows: list, given: str, *, kind: str, field: str, brief=None) -> dict:
    """The row with this id, or NotFound listing every row there is.

    Six planners did this by hand with their own candidate shapes. `brief` trims a row
    to what the candidate list should show; by default the whole projected row.
    """
    for row in rows:
        if row.get("id") == given:
            return row
    show = brief or (lambda row: row)
    raise errors.NotFound(
        f"No {kind} {given!r} in this project.",
        what=f"Check the {kind} ID.", field=field, given=given,
        candidates=[show(row) for row in rows],
        retry_with=[{"verb": "list", "of": f"{kind}s"}],
    )

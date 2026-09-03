"""Gated operations: one planner and one executor each.

The planner is read-only. It resolves every name to an ID, validates what can be
validated before anything happens, works out the blast radius, and renders the
text a human will approve. The executor receives only what the planner stored.

Deliberately absent: a cost estimate. Cirro's SDK exposes no pricing API -- the
billing service only lists accounts and metrics are project-level -- so a dollar
figure here would be invented. Plans say what will run instead.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

from .. import errors
from . import reads, rows as R

RISK_WRITE = "cirro_write"
RISK_DESTRUCTIVE = "cirro_destructive"
RISK_LOCAL = "local_write"
# An operation that changes Cirro *and* the local disk. The risk string is what the
# approval prompt shows, so an op that does both must not present as doing one.
RISK_WRITE_LOCAL = "cirro_write+local_write"


@dataclass
class Draft:
    args: dict
    summary: str
    warnings: list = field(default_factory=list)
    invalidates: list = field(default_factory=list)
    # Set only where the risk depends on what was planned rather than on the operation
    # -- a chain's risk is the widest risk among its steps.
    risk: Optional[str] = None


@dataclass
class Op:
    name: str
    risk: str
    plan: Callable
    apply: Callable
    # Result keys a later step in a chain may reference. Declared rather than
    # inferred, so a reference to a field an operation does not produce is refused at
    # plan time instead of failing halfway through an approved chain.
    outputs: tuple = ()


def _inv(kind: str, project_id: Optional[str] = None) -> dict:
    """A cache scope this mutation makes stale.

    Structured rather than a display string so that apply can actually drop the
    snapshot instead of the plan merely claiming it will.
    """
    entry = {"kind": kind}
    if project_id:
        entry["project"] = project_id
    return entry


def _need(args: dict, op: str, *keys: str) -> None:
    missing = [k for k in keys if args.get(k) in (None, "", [])]
    if missing:
        raise errors.InvalidQuery(
            f"{op} requires {missing}.",
            what="Supply every required argument.",
            field=missing[0], given=args,
            retry_with=[{"op": op, "args": {k: "..." for k in keys}}],
        )


def _project(reader: reads.Reader, args: dict) -> dict:
    given = args.get("project_id") or args.get("project")
    if not given:
        raise errors.ScopeRequired(
            "No project given.",
            what="Every mutation happens inside a project.", field="project_id",
            retry_with=[{"verb": "query", "entity": "project"}],
        )
    return reader.resolve_project(given)


def _target_folders(args: dict, current: list) -> list:
    """The folder list a dataset should end up in.

    `folder` names one, `folders` names several, `remove_from_folder` clears them, and
    silence leaves them alone -- which for an edit means the current membership and
    for a new dataset means none. Every operation that writes a tag list goes through
    here, because Cirro stores folders in that same list and replaces it wholesale:
    an operation that set tags without re-including folders would move the dataset
    out of its folder as a side effect.
    """
    if args.get("remove_from_folder"):
        return []
    if args.get("folders") is not None:
        return [str(f).strip().strip("/") for f in args["folders"] if str(f).strip()]
    if args.get("folder") is not None:
        named = str(args["folder"]).strip().strip("/")
        return [named] if named else []
    return list(current)


def _wire_tags(folders: list, tags: list) -> list:
    """The single list Cirro actually stores: folder entries plus plain tags."""
    return [R.folder_tag(f) for f in (folders or [])] + list(tags or [])


def _dataset(reader: reads.Reader, project_id: str, given: str) -> dict:
    """Mutations take IDs only. Names collide and are mutable."""
    datasets, _ = reader.datasets(project_id)
    exact_id = next((d for d in datasets if d["id"] == given), None)
    if exact_id:
        return exact_id
    named = [d for d in datasets if d["name"] == given]
    if named:
        raise errors.NameRequiredId(
            f"{given!r} is a dataset name, and mutations require an ID.",
            what="Re-send with the dataset ID.",
            field="dataset_id", given=given,
            candidates=[{"id": d["id"], "name": d["name"], "status": d["status"],
                         "created_at": d["created_at"]} for d in named],
            retry_with=[{"dataset_id": d["id"]} for d in named],
        )
    raise errors.NotFound(
        f"No dataset {given!r} in project {project_id}.",
        what="Check the dataset ID.", field="dataset_id", given=given,
        retry_with=[{"verb": "query", "entity": "dataset",
                     "scope": {"project_ids": [project_id]}}],
    )


def _derived_from(reader: reads.Reader, project_id: str, dataset_id: str) -> list:
    """Reverse-scan the project for datasets that list this one as a parent."""
    datasets, _ = reader.datasets(project_id)
    return [{"id": d["id"], "name": d["name"]}
            for d in datasets if dataset_id in (d.get("source_dataset_ids") or [])]


def _process(reader: reads.Reader, given: str, *, ingest: Optional[bool] = None) -> dict:
    found = reader.processes(include_archived=True)
    if ingest is not None:
        pool = [p for p in found
                if (p["executor"] == "INGEST") == ingest]
    else:
        pool = found
    by_id = next((p for p in pool if p["id"] == given), None)
    if by_id:
        return by_id
    named = [p for p in pool if p["name"] == given]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise errors.AmbiguousName(
            f"{len(named)} processes are named {given!r}.",
            what="Use the process ID.", field="process", given=given,
            candidates=[{"id": p["id"], "name": p["name"], "executor": p["executor"],
                         "pipeline_type": p["pipeline_type"]} for p in named],
            retry_with=[{"process_id": p["id"]} for p in named],
        )
    near = reads._near_misses(given, [p["name"] for p in pool])
    raise errors.NotFound(
        f"No process matches {given!r}.",
        what="Pick an available process.", field="process", given=given,
        candidates=[{"name": n} for n in near],
        retry_with=[{"verb": "query", "entity": "process"}],
    )


def _validate_params(reader: reads.Reader, process_id: str, params: dict) -> list:
    """Validate against the spec, and report undeclared keys separately.

    jsonschema will not flag an unknown key unless the schema sets
    additionalProperties:false, so a typo silently does nothing. Finding that out
    six hours into a pipeline is the expensive version of this check.
    """
    warnings = []
    try:
        spec = reader.client.processes.get_parameter_spec(process_id=process_id)
        reader.cost.call("processes.parameter_spec")
    except Exception as exc:
        return [f"Could not load the parameter spec for validation: {exc}"]
    try:
        spec.validate_params(params or {})
    except Exception as exc:
        raise errors.InvalidQuery(
            f"Parameters rejected by the process spec: {exc}",
            what="Fix the parameters before planning again.",
            field="params", given=params,
            retry_with=[{"verb": "get", "entity": "process", "id": process_id},
                        {"verb": "summarize", "of": "params",
                         "note": "inspect a previous run's params for a working shape"}],
        )
    defaults, declared, required = reads._spec_shape(reader, process_id)
    if declared is not None:
        unknown = [k for k in (params or {}) if k not in set(declared)]
        if unknown:
            warnings.append(
                f"Parameters not declared by this process and therefore ignored: "
                f"{sorted(unknown)} -- usually a typo")
    missing = [k for k in (required or []) if k not in (params or {})]
    if missing:
        warnings.append(f"Required parameters absent: {sorted(missing)}")
    return warnings


# ------------------------------------------------------------------ registry

# Each op family registers itself into OPS as it imports. OPS and get_op are
# defined before the imports below, and the registration sits on the module
# side, so any op module can be imported first without a partially initialized
# mutations in the way.
OPS: dict = {}


def get_op(name: str) -> Op:
    if name not in OPS:
        raise errors.NotSupported(
            f"Unknown operation {name!r}.",
            what="Use a supported operation.",
            retry_with=[{"op": key} for key in sorted(OPS)],
        )
    return OPS[name]


from . import (analysis_ops, chain_ops, dataset_ops,  # noqa: E402,F401
               pipeline_ops, project_ops, share_ops)

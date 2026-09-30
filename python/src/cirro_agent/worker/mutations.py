"""The shared core of the gated operations.

The operations themselves live in analysis_ops, dataset_ops, project_ops, share_ops,
chain_ops, pipeline_ops and dashboard_ops. This module holds what every family needs
-- Draft, Op, the risk classes, the resolvers, the folder/tag rule -- and the registry
they all register into.

Each operation is one planner and one executor.

The planner is read-only. It resolves every name to an ID, validates what can be
validated before anything happens, works out the blast radius, and renders the
text a human will approve. The executor receives only what the planner stored.

Deliberately absent: a cost estimate. Cirro reports spend that has already happened
-- `metrics.get_for_project().costs` is real dollars by service by month, and the
`spend` read surfaces it -- but nothing prices a run before it starts, so a figure
here would be invented. Plans say what will run instead.
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


# The argument keys shared resolvers read, named once so an op that uses a resolver
# cannot declare half of it. `_project` takes either spelling; `_target_folders` reads
# all three, because a tag write that forgets one silently drops folder membership.
PROJECT_ARGS = ("project", "project_id")
FOLDER_ARGS = ("folder", "folders", "remove_from_folder")


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
    # Every top-level argument key the planner reads. Declared rather than inferred
    # because a planner reads what it knows by name and nothing looked at the rest:
    # `execution_mode`, or a typo'd `enviroment_type`, vanished without a trace and
    # the run went somewhere the approved text did not say. The plan/apply split
    # exists so a person approves exactly what runs, and an argument dropped in
    # silence defeats it. `test_write_coverage.py` requires every op to declare this.
    accepts: tuple = ()

    def planned(self, reader, args: dict) -> Draft:
        """Check the argument surface, then plan. Every planner call comes through
        here -- `do_plan`, the re-plan at apply, and both chain-step paths -- so a
        step buried in an approved chain is held to the same rule as a bare plan."""
        unknown = sorted(set(args) - set(self.accepts))
        if unknown:
            raise errors.InvalidQuery(
                f"{self.name} does not accept {unknown}.",
                what="Remove the argument, or correct its spelling. An argument this "
                     "operation does not read would be dropped in silence, and the "
                     "plan you approved would not describe the run.",
                field=unknown[0], given=args, accepted=sorted(self.accepts),
                retry_with=[{k: v for k, v in args.items() if k not in unknown}],
            )
        return self.plan(reader, args)


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


def _tag_models(values: list) -> list:
    """Wire-format Tag objects; the import stays lazy because this module is an entry
    point that must not load the SDK."""
    from cirro_api_client.v1.models import Tag
    return [Tag(value=value) for value in values]


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


from . import (analysis_ops, chain_ops, dashboard_ops, dataset_ops,  # noqa: E402,F401
               pipeline_ops, project_ops, share_ops)

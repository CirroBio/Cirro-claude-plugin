"""The shared core of the gated operations.

The operations themselves live in analysis_ops, dataset_ops, project_ops, share_ops,
chain_ops, pipeline_ops, dashboard_ops and sheet_ops. This module holds what every family needs
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
    # Why this plan needs its own prompt although its operation is grantable: an
    # operation is granted as a kind, but a kind still has variants that remove data.
    ungrantable: Optional[str] = None


# Past this many datasets or rows, a plan of a grantable operation needs its own prompt.
GRANT_MAX_TARGETS = 50


def ungrantable(*reasons) -> Optional[str]:
    """The reasons that apply, joined for `Draft.ungrantable`, or None. Each reason is
    written `condition and "text"`, so the ones whose condition is false drop out."""
    return "; ".join(r for r in reasons if r) or None


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
    # May a person approve this operation once for the rest of a session, through
    # `grant`, rather than each time? Opt-in by hand and never for a destructive op:
    # a grant spares the prompt for every later plan of this kind in its project,
    # including ones steered by something Claude read, so an operation belongs here
    # only if it deletes nothing and changes no one's access.
    # `test_write_coverage.py` pins the exact set.
    grantable: bool = False

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


def _check_tags(args: dict) -> None:
    """Refuse a `tags` argument that Cirro would reject, or would store as something else.

    A tag here is a plain string, wrapped as `Tag(value=...)` on the wire. Cirro's Tag
    also has a `key`, but Cirro-backend marks it deprecated and nothing here reads it
    back, so a {"key", "value"} object has no faithful form: passed through, it reached
    the wire as {"value": {...}} and the upload was refused at apply, after approval
    ("expected VALUE_STRING"). A bare string is refused too, since `list("qc")` is two
    tags. A `folder://` entry is folder membership, which `folder`/`folders` set and the
    approval states (see `_target_folders`); in `tags` it would file the dataset without
    saying so.
    """
    tags = args.get("tags")
    if tags is None:
        return
    entries = tags if isinstance(tags, list) else [tags]
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in entries):
        raise errors.InvalidQuery(
            "tags must be a list of strings.",
            what="Each tag is plain text, e.g. [\"qc-pass\", \"batch-3\"]. Cirro's tag "
                 "key is deprecated and is not written; if a key carries meaning, put "
                 "it in the text.",
            field="tags", given=tags,
            retry_with=[{"tags": [str(t["value"]) if isinstance(t, dict) and "value" in t
                                  else str(t) for t in entries if t is not None]}],
        )
    folder_entries = [t for t in entries if t.startswith(R.FOLDER_TAG_PREFIX)]
    if folder_entries:
        raise errors.InvalidQuery(
            f"tags holds folder entries: {folder_entries}.",
            what="Folder membership is not a tag here. Name a dataset's Cirro folder "
                 "with `folder` or `folders`, which the approval shows as a folder.",
            field="tags", given=tags,
            retry_with=[{"tags": [t for t in entries if t not in folder_entries]}],
        )


# Cirro-backend declares the name on every request that creates or renames a dataset --
# UploadDatasetRequest, UpdateDatasetRequest, ImportDataRequest, RunAnalysisRequest --
# `@NotBlank @Size(max = 100)`, and refuses anything else only once the change is applied.
DATASET_NAME_MAX = 100


def _check_dataset_name(name) -> None:
    """Refuse a dataset name Cirro would reject after the approval had been spent."""
    if not isinstance(name, str) or not name.strip():
        raise errors.InvalidQuery(
            "A dataset name must be non-blank text.",
            what="Name the dataset.", field="name", given=name,
            retry_with=[{"name": "..."}],
        )

    # `@Size` counts what Java's String.length() counts: UTF-16 code units, so a
    # character outside the Basic Multilingual Plane, such as an emoji, is two.
    def units(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    if units(name) <= DATASET_NAME_MAX:
        return
    shorter = name
    while units(shorter) > DATASET_NAME_MAX:
        shorter = shorter[:-1]
    raise errors.InvalidQuery(
        f"The dataset name is {units(name)} characters long; Cirro accepts at most "
        f"{DATASET_NAME_MAX}.",
        what="Choose a shorter name.", field="name", given=name,
        retry_with=[{"name": shorter.rstrip()}],
    )


def _enum_choice(enum_cls, given, *, field: str, what: str) -> str:
    """The value of the member of a generated enum that `given` names, case aside.

    Checked at plan time because the enum cannot check it: `_missing_` turns an unknown
    string into UNKNOWN, which the models say never to send, and a plain string where
    a member belongs fails only as the request is serialized, after the approval.
    """
    choices = [member.value for member in enum_cls if member.name != "UNKNOWN"]
    wanted = str(given).strip().lower()
    match = next((choice for choice in choices if choice.lower() == wanted), None)
    if match is None:
        raise errors.InvalidQuery(
            f"{given!r} is not a valid {field}.",
            what=f"{what} Choose one of {choices}.", field=field, given=given,
            retry_with=[{field: choice} for choice in choices],
        )
    return match


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
               pipeline_ops, project_ops, share_ops, sheet_ops)

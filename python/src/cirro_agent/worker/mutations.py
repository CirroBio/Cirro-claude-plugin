"""Gated operations: one planner and one executor each.

The planner is read-only. It resolves every name to an ID, validates what can be
validated before anything happens, works out the blast radius, and renders the
text a human will approve. The executor receives only what the planner stored.

Deliberately absent: a cost estimate. Cirro's SDK exposes no pricing API -- the
billing service only lists accounts and metrics are project-level -- so a dollar
figure here would be invented. Plans say what will run instead.
"""
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .. import errors, summarize as S
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


@dataclass
class Op:
    name: str
    risk: str
    plan: Callable
    apply: Callable


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


# ------------------------------------------------------------------ run analysis


def plan_run_analysis(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "run_analysis", "name", "process")
    project = _project(reader, args)
    process = _process(reader, args["process"], ingest=False)
    if process["is_archived"]:
        raise errors.WrongStatus(
            f"Process {process['name']!r} is archived and cannot be run.",
            what="Pick a current process.", field="process",
            retry_with=[{"verb": "query", "entity": "process",
                         "filter": {"field": "is_archived", "op": "eq", "value": False}}],
        )

    source_ids = args.get("source_dataset_ids") or []
    if not source_ids and args.get("dataset_id"):
        source_ids = [args["dataset_id"]]
    if not source_ids:
        raise errors.InvalidQuery(
            "run_analysis needs at least one input dataset.",
            what="Name the dataset(s) to analyze.", field="source_dataset_ids",
            retry_with=[{"verb": "query", "entity": "dataset",
                         "scope": {"project_ids": [project["id"]]}}],
        )

    inputs, total_files, total_bytes = [], 0, 0
    for dataset_id in source_ids:
        row = _dataset(reader, project["id"], dataset_id)
        count = reads.file_count(reader, project["id"], row["id"])
        detail = reads._detail(reader, project["id"], row["id"], row)
        total_files += count or 0
        total_bytes += detail.get("total_size_bytes") or 0
        inputs.append({"id": row["id"], "name": row["name"], "file_count": count,
                       "size_human": detail.get("total_size_human")})

    params = args.get("params") or {}
    warnings = _validate_params(reader, process["id"], params)

    compute = args.get("compute_environment")
    compute_label = "Cirro default (AWS Batch)"
    if compute:
        environments = reader.client.compute_environments.list_environments_for_project(
            project_id=project["id"]) or []
        reader.cost.call("compute_environments.list")
        match = next((e for e in environments
                      if R.plain(getattr(e, "id", None)) == compute
                      or R.plain(getattr(e, "name", None)) == compute), None)
        if match is None:
            raise errors.NotFound(
                f"No compute environment {compute!r} in this project.",
                what="Use an available compute environment, or omit it for the default.",
                field="compute_environment", given=compute,
                candidates=[{"id": R.plain(getattr(e, "id", None)),
                             "name": R.plain(getattr(e, "name", None))}
                            for e in environments],
                retry_with=[{"compute_environment": None}],
            )
        compute = R.plain(getattr(match, "id", None))
        compute_label = f"{R.plain(getattr(match, 'name', None))} ({compute})"

    resolved = {
        "project_id": project["id"], "project_name": project["name"],
        "process_id": process["id"], "process_name": process["name"],
        "name": args["name"], "description": args.get("description", ""),
        "source_dataset_ids": [i["id"] for i in inputs],
        "params": params,
        "notification_emails": args.get("notification_emails") or [],
        "compute_environment_id": compute,
        "resume_dataset_id": args.get("resume_dataset_id"),
        "source_sample_ids": args.get("source_sample_ids") or [],
        "tags": args.get("tags") or [],
    }

    lines = [
        f"Launch an analysis in project {project['name']} ({project['id']}).",
        "",
        f"  Pipeline     {process['name']} [{process['executor']}] ({process['id']})",
        f"  Output name  {args['name']}",
        f"  Compute      {compute_label}",
        f"  Inputs       {len(inputs)} dataset(s), {total_files} files, "
        f"{S.human_bytes(total_bytes)}",
    ]
    for item in inputs:
        lines.append(f"                 - {item['name']} ({item['id']}, "
                     f"{item['file_count']} files, {item['size_human']})")
    if resolved["resume_dataset_id"]:
        lines.append(f"  Resume from  {resolved['resume_dataset_id']}")
    if resolved["notification_emails"]:
        lines.append(f"  Notify       {', '.join(resolved['notification_emails'])}")
    lines += ["", f"  Parameters ({len(params)}):"]
    lines += [f"    {k} = {v!r}" for k, v in sorted(params.items())] or ["    (none)"]
    lines += ["", "This creates a new dataset and consumes compute. Cirro's SDK exposes no "
                  "pricing API, so no cost estimate is possible here."]

    return Draft(args=resolved, summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("datasets", project["id"])])


def apply_run_analysis(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import RunAnalysisRequest, RunAnalysisRequestParams
    request = RunAnalysisRequest(
        name=args["name"], process_id=args["process_id"],
        source_dataset_ids=args["source_dataset_ids"],
        params=RunAnalysisRequestParams.from_dict(args.get("params") or {}),
        description=args.get("description") or "",
        notification_emails=args.get("notification_emails") or [],
    )
    for field_name, key in (("compute_environment_id", "compute_environment_id"),
                            ("resume_dataset_id", "resume_dataset_id"),
                            ("source_sample_ids", "source_sample_ids")):
        value = args.get(key)
        if value:
            setattr(request, field_name, value)
    if args.get("tags"):
        from cirro_api_client.v1.models import Tag
        request.tags = [Tag(value=t) for t in args["tags"]]
    response = reader.client.execution.run_analysis(project_id=args["project_id"],
                                                    request=request)
    created = R.plain(getattr(response, "id", None))
    return {"created_dataset_id": created,
            "message": R.plain(getattr(response, "message", None)),
            "follow_with": {"verb": "get", "entity": "dataset", "id": created,
                            "scope": {"project_ids": [args["project_id"]]},
                            "freshness": "fresh"}}


def plan_resume_analysis(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "resume_analysis", "resume_dataset_id")
    project = _project(reader, args)
    previous = _dataset(reader, project["id"], args["resume_dataset_id"])
    detail = reads._detail(reader, project["id"], previous["id"], previous)
    merged = dict(args)
    merged.setdefault("name", f"{previous['name']} (resumed)")
    merged.setdefault("process", detail.get("process_id"))
    merged.setdefault("source_dataset_ids", detail.get("source_dataset_ids") or [])
    merged.setdefault("params", detail.get("params") or {})
    draft = plan_run_analysis(reader, merged)
    draft.args["resume_dataset_id"] = previous["id"]
    draft.summary = (f"Resume the failed run {previous['name']} ({previous['id']}, "
                     f"status {previous['status']}).\n\n" + draft.summary)
    if str(previous["status"]).upper() not in ("FAILED", "STOPPING", "SUSPENDED"):
        draft.warnings.append(
            f"{previous['id']} is {previous['status']}, not a failed run; resuming it may "
            "duplicate work that already succeeded.")
    return draft


def plan_stop_analysis(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "stop_analysis", "dataset_id")
    project = _project(reader, args)
    row = _dataset(reader, project["id"], args["dataset_id"])
    if row["is_terminal"]:
        raise errors.WrongStatus(
            f"Dataset {row['id']} is {row['status']} and is not running.",
            what="Only an active analysis can be stopped.",
            current_status=row["status"],
            retry_with=[{"verb": "query", "entity": "dataset",
                         "scope": {"project_ids": [project["id"]]},
                         "filter": {"field": "status", "op": "in", "value": ["@active"]}}],
        )
    return Draft(
        args={"project_id": project["id"], "dataset_id": row["id"]},
        summary=(f"Stop the running analysis {row['name']} ({row['id']}) in "
                 f"{project['name']}.\n\n  Current status  {row['status']}\n\n"
                 "Partial output already written is kept; the run cannot be un-stopped, "
                 "only resumed as a new run."),
        invalidates=[_inv("datasets", project["id"])],
    )


def apply_stop_analysis(reader: reads.Reader, args: dict) -> dict:
    reader.client.execution.stop_analysis(project_id=args["project_id"],
                                          dataset_id=args["dataset_id"])
    return {"stopped": args["dataset_id"]}


# ------------------------------------------------------------------ dataset edits


def plan_update_dataset(reader: reads.Reader, args: dict) -> Draft:
    """Rename, re-describe, retag, or move a dataset between folders.

    Folder membership and tags share one underlying list, and the API replaces that
    list wholesale. Writing tags alone would therefore silently move a dataset out of
    its folder, so the folder is always resolved explicitly here and carried through.
    """
    _need(args, "update_dataset", "dataset_id")
    project = _project(reader, args)
    row = _dataset(reader, project["id"], args["dataset_id"])

    current_folders = list(row.get("folders") or [])
    current_tags = list(row.get("tags") or [])

    if args.get("remove_from_folder"):
        target_folders = []
    elif args.get("folder") is not None:
        target_folders = [str(args["folder"]).strip().strip("/")]
    else:
        target_folders = current_folders

    target_tags = list(args["tags"]) if args.get("tags") is not None else current_tags

    changes = {}
    for key in ("name", "description"):
        if args.get(key) is not None and args[key] != row.get(key):
            changes[key] = args[key]
    folder_changed = target_folders != current_folders
    tags_changed = sorted(target_tags) != sorted(current_tags)

    if not changes and not folder_changed and not tags_changed:
        raise errors.InvalidQuery(
            "Nothing would change.",
            what="Supply a new name, description, folder, or tags.",
            given=args,
            retry_with=[
                {"op": "update_dataset", "args": {"dataset_id": row["id"],
                                                  "folder": "Some Folder"}},
                {"op": "update_dataset", "args": {"dataset_id": row["id"],
                                                  "tags": ["example"]}},
            ],
        )

    lines = [f"Update dataset {row['name']} ({row['id']}) in {project['name']}.", ""]
    for key, value in changes.items():
        lines.append(f"  {key}")
        lines.append(f"    from  {row.get(key)!r}")
        lines.append(f"    to    {value!r}")
    if folder_changed:
        was = ", ".join(current_folders) or "(not in a folder)"
        now = ", ".join(target_folders) or "(not in a folder)"
        lines += ["  folder", f"    from  {was}", f"    to    {now}"]
    if tags_changed:
        lines += ["  tags", f"    from  {current_tags}", f"    to    {target_tags}"]

    warnings = []
    if tags_changed and not folder_changed and current_folders:
        warnings.append(
            f"Folder membership ({', '.join(current_folders)}) is preserved by this "
            "change.")
    if args.get("remove_from_folder") and current_folders:
        warnings.append(
            f"This takes the dataset out of {', '.join(current_folders)}; it will no "
            "longer appear under any folder.")

    # UpdateDatasetRequest requires name, description, process_id AND tags -- it is a
    # whole-record replacement. Omitting a field blanks it, so every one is resolved
    # here from the current record and only the requested changes are applied.
    return Draft(
        args={"project_id": project["id"], "dataset_id": row["id"],
              "name": changes.get("name", row.get("name")),
              "description": changes.get("description", row.get("description") or ""),
              "process_id": row.get("process_id"),
              "folders": target_folders, "tags": target_tags},
        summary="\n".join(lines), warnings=warnings,
        invalidates=[_inv("datasets", project["id"])],
    )


def apply_update_dataset(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import UpdateDatasetRequest, Tag
    # One list on the wire: folder membership plus plain tags. The planner already
    # decided the full contents, so nothing is dropped here.
    combined = [R.folder_tag(f) for f in (args.get("folders") or [])]
    combined += list(args.get("tags") or [])
    request = UpdateDatasetRequest(
        name=args["name"],
        description=args.get("description") or "",
        process_id=args["process_id"],
        tags=[Tag(value=value) for value in combined],
    )
    detail = reader.client.datasets.update(project_id=args["project_id"],
                                           dataset_id=args["dataset_id"], request=request)
    return {"updated": args["dataset_id"], "name": R.plain(getattr(detail, "name", None)),
            "folders": args.get("folders") or [], "tags": args.get("tags") or []}


def plan_delete_dataset(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "delete_dataset", "dataset_id")
    project = _project(reader, args)
    row = _dataset(reader, project["id"], args["dataset_id"])
    count = reads.file_count(reader, project["id"], row["id"])
    detail = reads._detail(reader, project["id"], row["id"], row)
    derived = _derived_from(reader, project["id"], row["id"])

    warnings = []
    if derived:
        warnings.append(
            f"{len(derived)} dataset(s) in this project list {row['id']} as a source: "
            + ", ".join(f"{d['name']} ({d['id']})" for d in derived[:5])
            + (" ..." if len(derived) > 5 else "")
            + ". Their lineage will point at a dataset that no longer exists.")
    if row.get("share"):
        warnings.append("This dataset arrives through a subscribed share; it may not be "
                        "yours to delete.")

    lines = [
        f"DELETE dataset {row['name']} ({row['id']}) from {project['name']} "
        f"({project['id']}).",
        "",
        f"  Status       {row['status']}",
        f"  Created      {row['created_at']} by {row['created_by']}",
        f"  Contents     {count} files, {detail.get('total_size_human')}",
        f"  Produced by  {(row.get('process') or {}).get('name')}",
        "",
        "This is not reversible from here.",
    ]
    return Draft(args={"project_id": project["id"], "dataset_id": row["id"],
                       "dataset_name": row["name"]},
                 summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("datasets", project["id"])])


def apply_delete_dataset(reader: reads.Reader, args: dict) -> dict:
    reader.client.datasets.delete(project_id=args["project_id"],
                                  dataset_id=args["dataset_id"])
    return {"deleted": args["dataset_id"], "name": args.get("dataset_name")}


def plan_update_samplesheet(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "update_samplesheet", "dataset_id")
    project = _project(reader, args)
    row = _dataset(reader, project["id"], args["dataset_id"])
    contents = args.get("contents")
    if contents is None and args.get("file_path"):
        contents = Path(args["file_path"]).read_text()
    if contents is None:
        raise errors.InvalidQuery(
            "update_samplesheet needs contents or file_path.",
            what="Provide the new samplesheet text.", field="contents",
            retry_with=[{"op": "update_samplesheet",
                         "args": {"dataset_id": row["id"], "contents": "sample,fastq_1\n"}}],
        )

    before = ""
    try:
        from cirro.sdk.dataset import DataPortalDataset
        from cirro_api_client.v1.models import ArtifactType
        detail = reader.client.datasets.get(project_id=project["id"], dataset_id=row["id"])
        reader.cost.call("datasets.get", detail=True)
        wrapped = DataPortalDataset(detail, reader.client)
        before = wrapped.get_artifact(ArtifactType.SAMPLE_SHEET).read()
        reader.cost.call("artifact.samplesheet")
    except Exception:
        before = ""

    import difflib
    diff = list(difflib.unified_diff(
        before.splitlines(), contents.splitlines(),
        fromfile="current samplesheet", tofile="proposed samplesheet", lineterm="", n=2))
    shown = diff[:60]
    if len(diff) > 60:
        shown.append(f"... {len(diff) - 60} more diff lines")

    lines = [f"Replace the samplesheet of {row['name']} ({row['id']}) in "
             f"{project['name']}.", ""]
    lines += ["  " + line for line in (shown or ["(no existing samplesheet to compare)"])]
    warnings = []
    if not before:
        warnings.append("No current samplesheet could be read, so this diff is against "
                        "an empty file.")
    return Draft(args={"project_id": project["id"], "dataset_id": row["id"],
                       "contents": contents},
                 summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("datasets", project["id"])])


def apply_update_samplesheet(reader: reads.Reader, args: dict) -> dict:
    reader.client.datasets.update_samplesheet(project_id=args["project_id"],
                                              dataset_id=args["dataset_id"],
                                              samplesheet=args["contents"])
    return {"updated_samplesheet_for": args["dataset_id"],
            "bytes": len(args["contents"])}


# ------------------------------------------------------------------ uploads


def _scan_folder(folder: Path, include_hidden: bool = False) -> tuple:
    from cirro.file_utils import is_hidden_file
    found, total = [], 0
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(folder).as_posix()
        if not include_hidden and is_hidden_file(Path(relative)):
            continue
        found.append(relative)
        total += path.stat().st_size
    return found, total


def plan_upload_dataset(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "upload_dataset", "name", "process", "folder")
    project = _project(reader, args)
    process = _process(reader, args["process"], ingest=True)
    folder = Path(args["folder"]).expanduser().resolve()
    if not folder.is_dir():
        raise errors.InvalidQuery(
            f"{folder} is not a directory.",
            what="Point folder at a local directory of files to upload.",
            field="folder", given=str(folder),
            retry_with=[{"op": "upload_dataset", "args": {"folder": str(Path.cwd())}}],
        )
    files, total_bytes = _scan_folder(folder, bool(args.get("include_hidden")))
    if not files:
        raise errors.InvalidQuery(
            f"No files found under {folder}.",
            what="Check the folder, or pass include_hidden.",
            field="folder", given=str(folder),
            retry_with=[{"op": "upload_dataset",
                         "args": {"folder": str(folder), "include_hidden": True}}],
        )

    warnings = []
    try:
        reader.client.processes.check_dataset_files(
            files=files, process_id=process["id"], directory=str(folder))
        reader.cost.call("processes.check_dataset_files")
    except Exception as exc:
        # The SDK runs this check before uploading anyway, so surfacing it now costs
        # nothing and turns a failed upload into a pre-approval warning.
        warnings.append(f"File-name validation for {process['name']!r} reports: {exc}")

    patterns = S.name_patterns(files, limit=5)
    lines = [
        f"Upload {len(files)} files ({S.human_bytes(total_bytes)}) into "
        f"{project['name']} ({project['id']}) as a new dataset.",
        "",
        f"  Dataset name  {args['name']}",
        f"  Data type     {process['name']} ({process['id']})",
        f"  Source folder {folder}",
        "",
        "  Files:",
    ]
    lines += [f"    {p['template']}  x{p['n']}" for p in patterns]
    return Draft(
        args={"project_id": project["id"], "process_id": process["id"],
              "name": args["name"], "description": args.get("description", ""),
              "folder": str(folder), "files": files, "tags": args.get("tags") or [],
              "n_files": len(files), "total_bytes": total_bytes},
        summary="\n".join(lines), warnings=warnings,
        invalidates=[_inv("datasets", project["id"])],
    )


def apply_upload_dataset(reader: reads.Reader, args: dict) -> dict:
    from cirro.sdk.project import DataPortalProject
    detail = reader.client.projects.get(project_id=args["project_id"])
    wrapped = DataPortalProject(detail, reader.client)
    wrapped.upload_dataset(
        name=args["name"], description=args.get("description") or "",
        process=args["process_id"], upload_folder=args["folder"],
        files=args.get("files") or None, tags=args.get("tags") or None)
    return {"uploaded_to_project": args["project_id"], "name": args["name"],
            "n_files": args.get("n_files"),
            "bytes": args.get("total_bytes"),
            "follow_with": {"verb": "query", "entity": "dataset",
                            "scope": {"project_ids": [args["project_id"]]},
                            "filter": {"field": "name", "op": "eq",
                                       "value": args["name"]},
                            "freshness": "fresh"}}


def plan_upload_reference(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "upload_reference", "name", "reference_type", "files")
    project = _project(reader, args)
    types = reader.client.references.get_types() or []
    reader.cost.call("references.get_types")
    wanted = args["reference_type"]
    match = next((t for t in types if R.plain(getattr(t, "name", None)) == wanted), None)
    if match is None:
        raise errors.NotFound(
            f"No reference type {wanted!r} in this deployment.",
            what="Reference types are deployment-specific; pick one that exists.",
            field="reference_type", given=wanted,
            candidates=[{"name": R.plain(getattr(t, "name", None)),
                         "description": R.plain(getattr(t, "description", None))}
                        for t in types],
            retry_with=[{"verb": "query", "entity": "reference"}],
        )
    paths = [Path(f).expanduser().resolve() for f in args["files"]]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise errors.InvalidQuery(
            f"Files not found: {missing}.",
            what="Every reference file must exist locally.", field="files",
            given=args["files"], retry_with=[{"op": "upload_reference"}],
        )
    total = sum(p.stat().st_size for p in paths)
    lines = [
        f"Upload a reference into {project['name']} ({project['id']}).",
        "",
        f"  Reference name  {args['name']}",
        f"  Reference type  {wanted}",
        f"  Files           {len(paths)}, {S.human_bytes(total)}",
    ]
    lines += [f"                    {p}" for p in paths]
    return Draft(args={"project_id": project["id"], "name": args["name"],
                       "reference_type": wanted, "files": [str(p) for p in paths]},
                 summary="\n".join(lines),
                 invalidates=[_inv("references", project["id"])])


def apply_upload_reference(reader: reads.Reader, args: dict) -> dict:
    ref_type = reader.client.references.get_type(
        reference_type_name=args["reference_type"])
    reader.client.references.upload_reference(
        name=args["name"], reference_files=[Path(f) for f in args["files"]],
        project_id=args["project_id"], ref_type=ref_type)
    return {"uploaded_reference": args["name"], "project_id": args["project_id"]}


def plan_import_public(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "import_public", "name", "public_ids")
    project = _project(reader, args)
    ids = args["public_ids"]
    lines = [
        f"Import public data into {project['name']} ({project['id']}).",
        "",
        f"  Dataset name  {args['name']}",
        f"  Accessions    {len(ids)}: {', '.join(map(str, ids[:10]))}"
        + (" ..." if len(ids) > 10 else ""),
        "",
        "Cirro will download these from the public repository. Volume is not known "
        "until the transfer starts.",
    ]
    return Draft(args={"project_id": project["id"], "name": args["name"],
                       "public_ids": ids, "description": args.get("description", ""),
                       "download_method": args.get("download_method")},
                 summary="\n".join(lines),
                 invalidates=[_inv("datasets", project["id"])])


def apply_import_public(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import ImportDataRequest
    request = ImportDataRequest(name=args["name"], public_ids=args["public_ids"],
                                description=args.get("description") or "")
    if args.get("download_method"):
        request.download_method = args["download_method"]
    response = reader.client.datasets.import_public(
        project_id=args["project_id"], import_request=request)
    return {"created_dataset_id": R.plain(getattr(response, "id", None))}


# ------------------------------------------------------------------ projects, people


def plan_create_project(reader: reads.Reader, args: dict) -> Draft:
    # ProjectSettings requires a budget, so there is no way to create a project
    # without stating one -- and it belongs in the approval text regardless.
    _need(args, "create_project", "name", "billing_account_id", "budget_amount",
          "budget_period")
    accounts = reader.client.billing.list() or []
    reader.cost.call("billing.list")
    account = next((a for a in accounts
                    if R.plain(getattr(a, "id", None)) == args["billing_account_id"]), None)
    if account is None:
        raise errors.NotFound(
            f"No billing account {args['billing_account_id']!r} available to you.",
            what="A project must be attached to a billing account you can use.",
            field="billing_account_id", given=args["billing_account_id"],
            candidates=[{"id": R.plain(getattr(a, "id", None)),
                         "name": R.plain(getattr(a, "name", None))} for a in accounts],
            retry_with=[{"op": "create_project",
                         "args": {"billing_account_id": R.plain(getattr(a, "id", None))}}
                        for a in accounts[:3]],
        )
    contacts = args.get("contacts") or []
    for contact in contacts:
        missing = [k for k in ("name", "organization", "email", "phone")
                   if not contact.get(k)]
        if missing:
            raise errors.InvalidQuery(
                f"Each contact needs name, organization, email and phone; missing "
                f"{missing}.",
                what="Complete the contact entries.", field="contacts", given=contacts,
                retry_with=[{"op": "create_project", "args": {"contacts": [
                    {"name": "...", "organization": "...", "email": "...",
                     "phone": "..."}]}}],
            )
    lines = [
        "Create a new Cirro project.",
        "",
        f"  Name             {args['name']}",
        f"  Description      {args.get('description', '')!r}",
        f"  Billing account  {R.plain(getattr(account, 'name', None))} "
        f"({args['billing_account_id']})",
        f"  Budget           {args['budget_amount']} per {args['budget_period']}",
        f"  Contacts         {len(contacts)}",
        "",
        "A project provisions cloud infrastructure and begins accruing storage charges "
        "against that billing account.",
    ]
    return Draft(args={"name": args["name"], "description": args.get("description", ""),
                       "billing_account_id": args["billing_account_id"],
                       "budget_amount": args["budget_amount"],
                       "budget_period": args["budget_period"],
                       "contacts": contacts,
                       "classification_ids": args.get("classification_ids") or [],
                       "tags": args.get("tags") or []},
                 summary="\n".join(lines), invalidates=[_inv("projects")])


def apply_create_project(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import (ProjectInput, ProjectSettings, Contact, Tag)
    request = ProjectInput(
        name=args["name"], description=args.get("description") or "",
        billing_account_id=args["billing_account_id"],
        settings=ProjectSettings(budget_amount=args["budget_amount"],
                                 budget_period=args["budget_period"]),
        contacts=[Contact(name=c["name"], organization=c["organization"],
                          email=c["email"], phone=c["phone"])
                  for c in (args.get("contacts") or [])],
    )
    if args.get("classification_ids"):
        request.classification_ids = args["classification_ids"]
    if args.get("tags"):
        request.tags = [Tag(value=t) for t in args["tags"]]
    response = reader.client.projects.create(request=request)
    return {"created_project_id": R.plain(getattr(response, "id", None))}


def plan_update_project_tags(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "update_project_tags", "tags")
    project = _project(reader, args)
    lines = [f"Replace the tags on project {project['name']} ({project['id']}).", "",
             f"  from  {project.get('tags')}", f"  to    {args['tags']}"]
    return Draft(args={"project_id": project["id"], "tags": args["tags"]},
                 summary="\n".join(lines), invalidates=[_inv("projects")])


def apply_update_project_tags(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import Tag
    reader.client.projects.update_tags(project_id=args["project_id"],
                                       tags=[Tag(value=t) for t in args["tags"]])
    return {"updated_tags_for": args["project_id"], "tags": args["tags"]}


def _plan_project_archive(reader: reads.Reader, args: dict, *, archive: bool) -> Draft:
    project = _project(reader, args)
    verb = "Archive" if archive else "Unarchive"
    datasets, _ = reader.datasets(project["id"]) if archive else ([], True)
    lines = [f"{verb} project {project['name']} ({project['id']}).", "",
             f"  Current status  {project['status']}"]
    if archive:
        lines.append(f"  Contains        {len(datasets)} datasets")
        lines.append("")
        lines.append("Archiving moves the project's data to cold storage and blocks new "
                     "analyses until it is unarchived.")
    return Draft(args={"project_id": project["id"], "archive": archive},
                 summary="\n".join(lines), invalidates=[_inv("projects")])


def plan_archive_project(reader: reads.Reader, args: dict) -> Draft:
    return _plan_project_archive(reader, args, archive=True)


def plan_unarchive_project(reader: reads.Reader, args: dict) -> Draft:
    return _plan_project_archive(reader, args, archive=False)


def apply_archive_project(reader: reads.Reader, args: dict) -> dict:
    if args["archive"]:
        reader.client.projects.archive(project_id=args["project_id"])
    else:
        reader.client.projects.unarchive(project_id=args["project_id"])
    return {"project_id": args["project_id"],
            "archived" if args["archive"] else "unarchived": True}


def plan_set_user_role(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "set_user_role", "username", "role")
    from cirro_api_client.v1.models import ProjectRole
    project = _project(reader, args)
    role = str(args["role"]).upper()
    valid = [r.name for r in ProjectRole]
    if role not in valid:
        raise errors.InvalidQuery(
            f"{args['role']!r} is not a project role.",
            what="Use a valid role.", field="role", given=args["role"],
            retry_with=[{"op": "set_user_role", "args": {"role": r}} for r in valid],
        )
    current = reader.client.projects.get_users(project_id=project["id"]) or []
    reader.cost.call("projects.get_users")
    existing = next((u for u in current
                     if R.plain(getattr(u, "username", None)) == args["username"]), None)
    was = R.plain(getattr(existing, "role", None)) if existing else "not a member"
    lines = [
        f"Change project access for {args['username']} on {project['name']} "
        f"({project['id']}).",
        "", f"  from  {was}", f"  to    {role}",
    ]
    warnings = []
    if role in ("OWNER", "ADMIN"):
        warnings.append(f"{role} can change project settings and other members' access.")
    if role == "NONE":
        warnings.append(f"This removes {args['username']}'s access to the project.")
    return Draft(args={"project_id": project["id"], "username": args["username"],
                       "role": role,
                       "suppress_notification": bool(args.get("suppress_notification"))},
                 summary="\n".join(lines), warnings=warnings)


def apply_set_user_role(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import ProjectRole
    reader.client.projects.set_user_role(
        project_id=args["project_id"], username=args["username"],
        role=ProjectRole[args["role"]],
        suppress_notification=args.get("suppress_notification", False))
    return {"project_id": args["project_id"], "username": args["username"],
            "role": args["role"]}


def plan_invite_user(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "invite_user", "name", "email", "organization")
    lines = [
        "Invite a new user to this Cirro tenant.",
        "", f"  Name          {args['name']}",
        f"  Email         {args['email']}",
        f"  Organization  {args['organization']}",
        "",
        "Cirro emails this address. The invitation cannot be recalled from here.",
    ]
    return Draft(args={"name": args["name"], "email": args["email"],
                       "organization": args["organization"]},
                 summary="\n".join(lines),
                 warnings=["This sends an email to an external address."],
                 invalidates=[_inv("users")])


def apply_invite_user(reader: reads.Reader, args: dict) -> dict:
    reader.client.users.invite_user(
        name=args["name"], organization=args["organization"], email=args["email"])
    return {"invited": args["email"]}


def plan_update_sample(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "update_sample", "sample_id", "metadata")
    project = _project(reader, args)
    existing, _ = reader.samples(project["id"])
    row = next((s for s in existing if s["id"] == args["sample_id"]), None)
    if row is None:
        raise errors.NotFound(
            f"No sample {args['sample_id']!r} in {project['name']}.",
            what="Check the sample ID.", field="sample_id", given=args["sample_id"],
            retry_with=[{"verb": "summarize", "of": "samples",
                         "scope": {"project_ids": [project["id"]]}}],
        )
    before = row.get("metadata") or {}
    supplied = args["metadata"]
    # SampleRequest replaces the whole metadata object, so a partial dict would delete
    # every key it omits. Merge unless the caller explicitly asks to replace.
    replace = bool(args.get("replace_metadata"))
    after = dict(supplied) if replace else {**before, **supplied}
    changed = {k: (before.get(k), v) for k, v in after.items() if before.get(k) != v}
    dropped = sorted(set(before) - set(after))

    lines = [f"Update metadata on sample {row['name']} ({row['id']}) in "
             f"{project['name']}.", ""]
    for key, (old, new) in sorted(changed.items()):
        lines.append(f"  {key}: {old!r} -> {new!r}")
    if dropped:
        lines += ["", f"  REMOVED: {dropped}"]
    if not changed and not dropped:
        lines.append("  (no values differ from what is stored)")

    warnings = []
    if dropped:
        warnings.append(f"replace_metadata was set, so these keys are deleted: {dropped}")
    elif not replace and set(before) - set(supplied):
        warnings.append(
            f"{len(set(before) - set(supplied))} existing metadata key(s) not named here "
            "are preserved; pass replace_metadata to delete them instead.")
    return Draft(args={"project_id": project["id"], "sample_id": row["id"],
                       "name": args.get("name") or row["name"], "metadata": after},
                 summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("samples", project["id"])])


def apply_update_sample(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import SampleRequest, SampleRequestMetadata
    reader.client.metadata.update_sample(
        project_id=args["project_id"], sample_id=args["sample_id"],
        sample=SampleRequest(name=args["name"],
                             metadata=SampleRequestMetadata.from_dict(args["metadata"])))
    return {"updated_sample": args["sample_id"]}


def plan_update_project_schema(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "update_project_schema", "schema")
    project = _project(reader, args)
    schema_payload = args["schema"]
    new_keys = sorted((schema_payload.get("properties") or {}).keys()) \
        if isinstance(schema_payload, dict) else []
    old_keys = []
    try:
        current = reader.client.metadata.get_project_schema(project_id=project["id"])
        reader.cost.call("metadata.schema")
        spec = getattr(current, "form_spec_json", None) or {}
        old_keys = sorted((spec.get("properties") or {}).keys()) \
            if isinstance(spec, dict) else []
    except Exception:
        pass
    lines = [
        f"Replace the sample-metadata schema of {project['name']} ({project['id']}).",
        "", f"  keys removed  {sorted(set(old_keys) - set(new_keys))}",
        f"  keys added    {sorted(set(new_keys) - set(old_keys))}",
        f"  keys kept     {len(set(old_keys) & set(new_keys))}",
    ]
    warnings = []
    removed = set(old_keys) - set(new_keys)
    if removed:
        warnings.append(f"Removing schema keys {sorted(removed)} affects how existing "
                        "sample metadata validates.")
    return Draft(args={"project_id": project["id"], "schema": schema_payload},
                 summary="\n".join(lines), warnings=warnings)


def apply_update_project_schema(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import FormSchema
    schema_obj = FormSchema.from_dict(args["schema"]) \
        if isinstance(args["schema"], dict) else args["schema"]
    reader.client.metadata.update_project_schema(project_id=args["project_id"],
                                                 schema=schema_obj)
    return {"updated_schema_for": args["project_id"]}


# ------------------------------------------------------------------ shares, workspaces


def _share(reader: reads.Reader, project_id: str, share_id: str) -> dict:
    found = reader.client.shares.list(project_id=project_id) or []
    reader.cost.call("shares.list")
    match = next((s for s in found if R.plain(getattr(s, "id", None)) == share_id), None)
    if match is None:
        raise errors.NotFound(
            f"No share {share_id!r} in this project.",
            what="Check the share ID.", field="share_id", given=share_id,
            candidates=[{"id": R.plain(getattr(s, "id", None)),
                         "name": R.plain(getattr(s, "name", None)),
                         "share_type": R.plain(getattr(s, "share_type", None))}
                        for s in found],
            retry_with=[{"verb": "query", "entity": "share"}],
        )
    return {"id": R.plain(match.id), "name": R.plain(match.name),
            "share_type": R.plain(getattr(match, "share_type", None))}


def plan_create_share(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "create_share", "name")
    project = _project(reader, args)
    targets = args.get("shared_project_ids") or []
    lines = [
        f"Create a data share published from {project['name']} ({project['id']}).",
        "", f"  Share name       {args['name']}",
        f"  Description      {args.get('description', '')!r}",
        f"  Shared with      {len(targets)} project(s): {targets}",
        f"  View restricted  {bool(args.get('is_view_restricted'))}",
        "",
        "A share exposes matching datasets to other projects. Anyone with access to "
        "those projects can read what it selects.",
    ]
    conditions = args.get("conditions") or []
    warnings = ["This makes data readable outside its originating project."]
    if not conditions:
        warnings.append(
            "No dataset conditions were given, so which datasets this exposes is decided "
            "entirely by Cirro's defaults -- confirm the scope in the UI after creating "
            "it.")
    return Draft(args={"project_id": project["id"], "name": args["name"],
                       "description": args.get("description", ""),
                       "shared_project_ids": targets,
                       "keywords": args.get("keywords") or [],
                       "classification_ids": args.get("classification_ids") or [],
                       "conditions": conditions,
                       "is_view_restricted": bool(args.get("is_view_restricted"))},
                 summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("shares", project["id"])])


def apply_create_share(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import ShareInput, DatasetCondition
    request = ShareInput(
        name=args["name"], description=args.get("description") or "",
        classification_ids=args.get("classification_ids") or [],
        conditions=[DatasetCondition(field=c["field"], value=c["value"])
                    for c in (args.get("conditions") or [])],
    )
    for attribute in ("shared_project_ids", "keywords", "is_view_restricted"):
        if args.get(attribute):
            setattr(request, attribute, args[attribute])
    response = reader.client.shares.create(project_id=args["project_id"], share=request)
    return {"created_share_id": R.plain(getattr(response, "id", None))}


def plan_delete_share(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "delete_share", "share_id")
    project = _project(reader, args)
    share = _share(reader, project["id"], args["share_id"])
    lines = [f"DELETE share {share['name']} ({share['id']}) from {project['name']}.", "",
             f"  Share type  {share['share_type']}", "",
             "Subscribers lose access to every dataset this share exposed."]
    return Draft(args={"project_id": project["id"], "share_id": share["id"],
                       "share_name": share["name"]},
                 summary="\n".join(lines),
                 invalidates=[_inv("shares", project["id"])])


def apply_delete_share(reader: reads.Reader, args: dict) -> dict:
    reader.client.shares.delete(project_id=args["project_id"], share_id=args["share_id"])
    return {"deleted_share": args["share_id"], "name": args.get("share_name")}


def _plan_share_subscription(reader: reads.Reader, args: dict, *, subscribe: bool) -> Draft:
    _need(args, "subscribe_share" if subscribe else "unsubscribe_share", "share_id")
    project = _project(reader, args)
    share = _share(reader, project["id"], args["share_id"])
    verb = "Subscribe to" if subscribe else "Unsubscribe from"
    effect = ("Datasets from this share become visible in the project."
              if subscribe else
              "Datasets from this share stop appearing in the project.")
    lines = [f"{verb} share {share['name']} ({share['id']}) for {project['name']}.", "",
             effect]
    return Draft(args={"project_id": project["id"], "share_id": share["id"],
                       "subscribe": subscribe},
                 summary="\n".join(lines),
                 invalidates=[_inv("datasets", project["id"]), _inv("shares", project["id"])])


def plan_subscribe_share(reader: reads.Reader, args: dict) -> Draft:
    return _plan_share_subscription(reader, args, subscribe=True)


def plan_unsubscribe_share(reader: reads.Reader, args: dict) -> Draft:
    return _plan_share_subscription(reader, args, subscribe=False)


def apply_share_subscription(reader: reads.Reader, args: dict) -> dict:
    if args["subscribe"]:
        reader.client.shares.subscribe(project_id=args["project_id"],
                                       share_id=args["share_id"])
    else:
        reader.client.shares.unsubscribe(project_id=args["project_id"],
                                         share_id=args["share_id"])
    return {"share_id": args["share_id"], "subscribed": args["subscribe"]}


def _workspace(reader: reads.Reader, project_id: str, workspace_id: str) -> dict:
    found = reader.client.workspaces.list(project_id=project_id) or []
    reader.cost.call("workspaces.list")
    match = next((w for w in found
                  if R.plain(getattr(w, "id", None)) == workspace_id), None)
    if match is None:
        raise errors.NotFound(
            f"No workspace {workspace_id!r} in this project.",
            what="Check the workspace ID.", field="workspace_id", given=workspace_id,
            candidates=[{"id": R.plain(getattr(w, "id", None)),
                         "name": R.plain(getattr(w, "name", None)),
                         "status": R.plain(getattr(w, "status", None))} for w in found],
            retry_with=[{"verb": "query", "entity": "workspace"}],
        )
    return {"id": R.plain(match.id), "name": R.plain(match.name),
            "status": R.plain(getattr(match, "status", None))}


def plan_create_workspace(reader: reads.Reader, args: dict) -> Draft:
    # WorkspaceComputeConfig requires the container image, so there is no valid
    # workspace without one -- and which image runs is exactly what an approver checks.
    _need(args, "create_workspace", "name", "environment_id", "container_image_uri")
    project = _project(reader, args)
    environments = reader.client.workspaces.list_environments() or []
    reader.cost.call("workspaces.list_environments")
    match = next((e for e in environments
                  if R.plain(getattr(e, "id", None)) == args["environment_id"]), None)
    if match is None:
        raise errors.NotFound(
            f"No workspace environment {args['environment_id']!r}.",
            what="Pick an available environment.", field="environment_id",
            given=args["environment_id"],
            candidates=[{"id": R.plain(getattr(e, "id", None)),
                         "name": R.plain(getattr(e, "name", None))}
                        for e in environments],
            retry_with=[{"op": "create_workspace",
                         "args": {"environment_id": R.plain(getattr(e, "id", None))}}
                        for e in environments[:3]],
        )
    mounted = args.get("mounted_datasets") or []
    sharing = str(args.get("sharing_type") or "PRIVATE").upper()
    valid_sharing = {"PRIVATE", "READ_WRITE", "READ_WRITE_CONTROL"}
    if sharing not in valid_sharing:
        raise errors.InvalidQuery(
            f"{args.get('sharing_type')!r} is not a workspace sharing mode.",
            what="Use a valid sharing mode.", field="sharing_type",
            given=args.get("sharing_type"),
            retry_with=[{"op": "create_workspace", "args": {"sharing_type": mode}}
                        for mode in sorted(valid_sharing)],
        )
    lines = [
        f"Create a workspace in {project['name']} ({project['id']}).",
        "", f"  Name              {args['name']}",
        f"  Environment       {R.plain(getattr(match, 'name', None))} "
        f"({args['environment_id']})",
        f"  Container image   {args['container_image_uri']}",
        f"  Resources         {args.get('cpu') or 'default'} vCPU, "
        f"{args.get('memory_gi_b') or 'default'} GiB"
        + (f", GPU {args['gpu']}" if args.get("gpu") else ""),
        f"  Mounted datasets  {len(mounted)}: {mounted}",
        f"  Sharing           {sharing}",
        f"  Auto-stop         {args.get('auto_stop_timeout') or 'environment default'}",
        "",
        "A running workspace accrues compute charges until it stops.",
    ]
    warnings = []
    if sharing != "PRIVATE":
        warnings.append(f"Sharing is {sharing}: others in the project can reach this "
                        "workspace.")
    return Draft(args={"project_id": project["id"], "name": args["name"],
                       "environment_id": args["environment_id"],
                       "description": args.get("description", ""),
                       "mounted_datasets": mounted,
                       "container_image_uri": args["container_image_uri"],
                       "cpu": args.get("cpu"), "memory_gi_b": args.get("memory_gi_b"),
                       "gpu": args.get("gpu"),
                       "volume_size_gi_b": args.get("volume_size_gi_b"),
                       "sharing_type": sharing,
                       "auto_stop_timeout": args.get("auto_stop_timeout")},
                 summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("workspaces", project["id"])])


def apply_create_workspace(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import (WorkspaceInput, WorkspaceComputeConfig,
                                            SharingType)
    compute = WorkspaceComputeConfig(container_image_uri=args["container_image_uri"])
    for key, attribute in (("cpu", "cpu"), ("memory_gi_b", "memory_gi_b"),
                           ("gpu", "gpu"), ("volume_size_gi_b", "volume_size_gi_b")):
        if args.get(key) is not None:
            setattr(compute, attribute, args[key])
    request = WorkspaceInput(
        name=args["name"], mounted_datasets=args.get("mounted_datasets") or [],
        compute_config=compute,
        sharing_type=SharingType(args.get("sharing_type") or "PRIVATE"))
    request.environment_id = args["environment_id"]
    if args.get("description"):
        request.description = args["description"]
    if args.get("auto_stop_timeout"):
        request.auto_stop_timeout = args["auto_stop_timeout"]
    response = reader.client.workspaces.create(project_id=args["project_id"],
                                               workspace=request)
    return {"created_workspace_id": R.plain(getattr(response, "id", None))}


def _plan_workspace_action(reader: reads.Reader, args: dict, action: str) -> Draft:
    _need(args, f"{action}_workspace", "workspace_id")
    project = _project(reader, args)
    workspace = _workspace(reader, project["id"], args["workspace_id"])
    effects = {
        "start": "The workspace boots and begins accruing compute charges.",
        "stop": "Running processes are terminated; persisted files are kept.",
        "delete": "The workspace and anything stored only inside it are destroyed.",
    }
    lines = [
        f"{action.capitalize()} workspace {workspace['name']} ({workspace['id']}) in "
        f"{project['name']}.",
        "", f"  Current status  {workspace['status']}", "", effects[action],
    ]
    return Draft(args={"project_id": project["id"], "workspace_id": workspace["id"],
                       "action": action, "workspace_name": workspace["name"]},
                 summary="\n".join(lines),
                 invalidates=[_inv("workspaces", project["id"])])


def plan_start_workspace(reader, args):
    return _plan_workspace_action(reader, args, "start")


def plan_stop_workspace(reader, args):
    return _plan_workspace_action(reader, args, "stop")


def plan_delete_workspace(reader, args):
    return _plan_workspace_action(reader, args, "delete")


# An explicit map, so a tampered `action` is a rejected plan rather than an arbitrary
# method call on the workspaces client.
_WORKSPACE_ACTIONS = ("start", "stop", "delete")


def apply_workspace_action(reader: reads.Reader, args: dict) -> dict:
    action = args.get("action")
    if action not in _WORKSPACE_ACTIONS:
        raise errors.PlanInvalid(
            f"{action!r} is not a workspace action.",
            what="The stored plan names an operation that does not exist.",
            retry_with=[{"tool": "plan", "op": f"{a}_workspace"}
                        for a in _WORKSPACE_ACTIONS],
        )
    getattr(reader.client.workspaces, action)(project_id=args["project_id"],
                                              workspace_id=args["workspace_id"])
    return {"workspace_id": args["workspace_id"], "action": action}


# ------------------------------------------------------------------ local writes


def plan_download_files(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "download_files", "dataset_id", "destination")
    project = _project(reader, args)
    row = _dataset(reader, project["id"], args["dataset_id"])
    entries, total, _artifacts = reads._manifest(
        reader, project["id"], row["id"],
        is_terminal=bool(row.get("is_terminal")))
    capped = total is not None and len(entries) < total
    glob = args.get("glob")
    if glob:
        import fnmatch
        entries = [e for e in entries if fnmatch.fnmatch(e["path"].lower(), glob.lower())]
    total_bytes = sum(e.get("size") or 0 for e in entries)

    destination = Path(args["destination"]).expanduser().resolve()
    free = shutil.disk_usage(destination if destination.exists()
                             else destination.parent).free
    warnings = []
    if total_bytes > free * 0.9:
        warnings.append(
            f"This needs {S.human_bytes(total_bytes)} but only {S.human_bytes(free)} is "
            "free at the destination.")
    if capped and not glob:
        # Without a glob apply downloads everything, so approving a capped figure would
        # understate the transfer.
        warnings.append(
            f"This dataset has {total} files but the manifest was read only to "
            f"{len(entries)}, so the counts above are a lower bound and the actual "
            "download will be larger.")

    patterns = S.name_patterns([e["path"] for e in entries], limit=5)
    lines = [
        f"Download {len(entries)} files ({S.human_bytes(total_bytes)}) from "
        f"{row['name']} ({row['id']}) to local disk.",
        "", f"  Destination  {destination}",
        f"  Free space   {S.human_bytes(free)}",
        f"  Filter       {glob or '(all files)'}",
        "", "  Files:",
    ]
    lines += [f"    {p['template']}  x{p['n']}" for p in patterns]
    lines += ["", "This writes to the local filesystem, not to Cirro."]
    return Draft(args={"project_id": project["id"], "dataset_id": row["id"],
                       "destination": str(destination),
                       "files": [e["path"] for e in entries] if glob else None,
                       "n_files": len(entries), "total_bytes": total_bytes},
                 summary="\n".join(lines), warnings=warnings)


def apply_download_files(reader: reads.Reader, args: dict) -> dict:
    destination = Path(args["destination"])
    destination.mkdir(parents=True, exist_ok=True)
    reader.client.datasets.download_files(
        project_id=args["project_id"], dataset_id=args["dataset_id"],
        download_location=str(destination), files=args.get("files"))
    return {"downloaded_to": str(destination), "n_files": args.get("n_files"),
            "bytes": args.get("total_bytes")}


# ------------------------------------------------------------------ registry


OPS = {op.name: op for op in [
    Op("run_analysis", RISK_WRITE, plan_run_analysis, apply_run_analysis),
    Op("resume_analysis", RISK_WRITE, plan_resume_analysis, apply_run_analysis),
    Op("stop_analysis", RISK_WRITE, plan_stop_analysis, apply_stop_analysis),
    Op("upload_dataset", RISK_WRITE, plan_upload_dataset, apply_upload_dataset),
    Op("upload_reference", RISK_WRITE, plan_upload_reference, apply_upload_reference),
    Op("import_public", RISK_WRITE, plan_import_public, apply_import_public),
    Op("update_dataset", RISK_WRITE, plan_update_dataset, apply_update_dataset),
    Op("update_samplesheet", RISK_WRITE, plan_update_samplesheet, apply_update_samplesheet),
    Op("delete_dataset", RISK_DESTRUCTIVE, plan_delete_dataset, apply_delete_dataset),
    Op("create_project", RISK_WRITE, plan_create_project, apply_create_project),
    Op("update_project_tags", RISK_WRITE, plan_update_project_tags,
       apply_update_project_tags),
    Op("archive_project", RISK_DESTRUCTIVE, plan_archive_project, apply_archive_project),
    Op("unarchive_project", RISK_WRITE, plan_unarchive_project, apply_archive_project),
    Op("set_user_role", RISK_WRITE, plan_set_user_role, apply_set_user_role),
    Op("invite_user", RISK_WRITE, plan_invite_user, apply_invite_user),
    Op("update_sample", RISK_WRITE, plan_update_sample, apply_update_sample),
    Op("update_project_schema", RISK_WRITE, plan_update_project_schema,
       apply_update_project_schema),
    Op("create_share", RISK_WRITE, plan_create_share, apply_create_share),
    Op("delete_share", RISK_DESTRUCTIVE, plan_delete_share, apply_delete_share),
    Op("subscribe_share", RISK_WRITE, plan_subscribe_share, apply_share_subscription),
    Op("unsubscribe_share", RISK_WRITE, plan_unsubscribe_share, apply_share_subscription),
    Op("create_workspace", RISK_WRITE, plan_create_workspace, apply_create_workspace),
    Op("start_workspace", RISK_WRITE, plan_start_workspace, apply_workspace_action),
    Op("stop_workspace", RISK_WRITE, plan_stop_workspace, apply_workspace_action),
    Op("delete_workspace", RISK_DESTRUCTIVE, plan_delete_workspace,
       apply_workspace_action),
    Op("download_files", RISK_LOCAL, plan_download_files, apply_download_files),
]}


# Pipeline configuration lives in its own module and registers itself on import.
# The import is last, and the registration is on that side, so the two modules can be
# imported in either order.
from . import pipeline_ops  # noqa: E402,F401


def get_op(name: str) -> Op:
    if name not in OPS:
        raise errors.NotSupported(
            f"Unknown operation {name!r}.",
            what="Use a supported operation.",
            retry_with=[{"op": key} for key in sorted(OPS)],
        )
    return OPS[name]

"""Launching, resuming and stopping pipeline runs.

Registers its operations into `mutations.OPS` on import; see mutations.py for
why the registration lives on this side.

A launch notifies the account the session is authenticated as unless the caller
says otherwise, which is what the web UI does; `_notify` explains the mechanics.
"""

from typing import Optional, Tuple

from .. import errors, summarize as S
from . import mutations, reads, rows as R
from .mutations import (_tag_models, Draft, Op, RISK_WRITE, _inv, _need, _project, _dataset,
                        _target_folders, _wire_tags)


def plan_run_analysis(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "run_analysis", "name", "process")
    project = _project(reader, args)
    process = reader.resolve_process(args["process"], ingest=False)
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
        detail = reads.dataset_detail(reader, project["id"], row["id"], row)
        total_files += count or 0
        total_bytes += detail.get("total_size_bytes") or 0
        inputs.append({"id": row["id"], "name": row["name"], "file_count": count,
                       "size_human": detail.get("total_size_human")})

    params, warnings = _resolve_params(reader, process["id"], args.get("params") or {})

    notify, notify_shown, notify_warnings = _notify(reader, args)
    warnings += notify_warnings

    compute = args.get("compute_environment")
    compute_label = "Cirro default (AWS Batch)"
    if compute:
        environments = reader.compute_environments(project["id"])
        match = next((e for e in environments if compute in (e["id"], e["name"])), None)
        if match is None:
            raise errors.NotFound(
                f"No compute environment {compute!r} in this project.",
                what="Use an available compute environment, or omit it for the default.",
                field="compute_environment", given=compute,
                candidates=environments,
                retry_with=[{"compute_environment": None}],
            )
        compute = match["id"]
        compute_label = f"{match['name']} ({compute})"

    resolved = {
        "project_id": project["id"], "project_name": project["name"],
        "process_id": process["id"], "process_name": process["name"],
        "name": args["name"], "description": args.get("description", ""),
        "source_dataset_ids": [i["id"] for i in inputs],
        "params": params,
        "notification_emails": notify,
        "compute_environment_id": compute,
        "resume_dataset_id": args.get("resume_dataset_id"),
        "source_sample_ids": args.get("source_sample_ids") or [],
        "tags": args.get("tags") or [],
        # A new analysis has no folder of its own yet, so silence means none rather
        # than "keep what is there".
        "folders": _target_folders(args, []),
    }

    lines = [
        f"Launch an analysis in project {project['name']} ({project['id']}).",
        "",
        f"  Pipeline     {process['name']} [{process['executor']}] ({process['id']})",
        f"  Output name  {args['name']}",
        f"  Into folder  {', '.join(resolved['folders']) or '(none)'}",
        f"  Compute      {compute_label}",
        f"  Inputs       {len(inputs)} dataset(s), {total_files} files, "
        f"{S.human_bytes(total_bytes)}",
    ]
    for item in inputs:
        lines.append(f"                 - {item['name']} ({item['id']}, "
                     f"{item['file_count']} files, {item['size_human']})")
    if resolved["resume_dataset_id"]:
        lines.append(f"  Resume from  {resolved['resume_dataset_id']}")
    # Printed unconditionally: mail is sent by default now, and a default nobody reads
    # is a surprise. "(nobody)" is stated rather than left blank.
    lines.append(f"  Notify       {notify_shown}")
    lines += ["", f"  Parameters ({len(params)}):"]
    lines += [f"    {k} = {v!r}" for k, v in sorted(params.items())] or ["    (none)"]
    lines += ["", "This creates a new dataset and consumes compute. Nothing prices a run "
                  "before it happens, so there is no cost estimate here; the `spend` read "
                  "reports the dollars once they are incurred."]

    return Draft(args=resolved, summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("datasets", project["id"])])


def apply_run_analysis(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import RunAnalysisRequest, RunAnalysisRequestParams
    request = RunAnalysisRequest(
        name=args["name"], process_id=args["process_id"],
        source_dataset_ids=args["source_dataset_ids"],
        params=RunAnalysisRequestParams.from_dict(args.get("params") or {}),
        description=args.get("description") or "",
        # Already resolved and disclosed by the planner -- apply sends what was
        # approved and never defaults an address of its own.
        notification_emails=args.get("notification_emails") or [],
    )
    for optional in ("compute_environment_id", "resume_dataset_id",
                     "source_sample_ids"):
        if args.get(optional):
            setattr(request, optional, args[optional])
    # Folders are tags, and RunAnalysisRequest carries tags -- so the output dataset
    # is filed at launch, in the same call, instead of needing an update afterwards.
    combined = _wire_tags(args.get("folders"), args.get("tags"))
    if combined:
        request.tags = _tag_models(combined)
    response = reader.client.execution.run_analysis(project_id=args["project_id"],
                                                    request=request)
    created = R.plain(getattr(response, "id", None))
    return {"created_dataset_id": created,
            "message": R.plain(getattr(response, "message", None)),
            "folders": args.get("folders") or [],
            "follow_with": {"verb": "get", "entity": "dataset", "id": created,
                            "scope": {"project_ids": [args["project_id"]]},
                            "freshness": "fresh"}}


def plan_resume_analysis(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "resume_analysis", "resume_dataset_id")
    project = _project(reader, args)
    previous = _dataset(reader, project["id"], args["resume_dataset_id"])
    detail = reads.dataset_detail(reader, project["id"], previous["id"], previous)
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


ANALYSIS_OPS = [
    Op("run_analysis", RISK_WRITE, plan_run_analysis, apply_run_analysis,
       outputs=("created_dataset_id",)),
    # Shares run_analysis's executor, so a resume records a created dataset too.
    Op("resume_analysis", RISK_WRITE, plan_resume_analysis, apply_run_analysis,
       outputs=("created_dataset_id",)),
    Op("stop_analysis", RISK_WRITE, plan_stop_analysis, apply_stop_analysis),
]

mutations.OPS.update({op.name: op for op in ANALYSIS_OPS})


NOTIFY_HINT = "Pass notification_emails explicitly to choose who is told."


def _notify(reader: reads.Reader, args: dict):
    """Who gets mail about this run, and the line the approval shows for it.

    The web UI mails whoever launched a run and the plugin mailed nobody, so the same
    launch behaved differently depending on where it was made from. The session's
    identity is a Cognito-style username rather than an address, so the address has to
    be looked up; that happens here, in the planner, because the approval must state
    who will be mailed before anything is sent and apply's re-plan has to render the
    same text.

    An explicit list is used as given, and an explicit empty list is a genuine "tell
    nobody" -- `args.get(...) or []` could not tell that apart from silence, which is
    why the default could not be expressed before. A lookup that fails costs the
    notification, never the launch.

    Returns (emails, what the summary shows for them, warnings).
    """
    given = args.get("notification_emails")
    if given is not None:
        addresses = [str(a) for a in given]
        return addresses, (", ".join(addresses) or
                           "(nobody -- notification_emails was given as empty)"), []

    username = reader.username
    if not username:
        return [], "(nobody -- this session's identity could not be read)", [
            "Could not read the authenticated username, so this run notifies "
            f"nobody. {NOTIFY_HINT}"]
    try:
        detail = reader.client.users.get(username)
        reader.cost.call("users.get", detail=True)
    except Exception as exc:
        # Deliberately without the exception's text: apply re-plans and refuses on any
        # change to the rendered warnings, and an upstream message carrying a request
        # id would make every such plan PLAN_STALE.
        return [], "(nobody -- your email address could not be looked up)", [
            f"Could not look up the email address for {username!r} "
            f"({type(exc).__name__}), so this run notifies nobody. {NOTIFY_HINT}"]

    email = R.plain(getattr(detail, "email", None))
    if not email:
        return [], "(nobody -- no email address on your Cirro account)", [
            f"The Cirro account {username!r} has no email address on record, so this "
            f"run notifies nobody. {NOTIFY_HINT}"]
    # The account's own "analysis update notifications" preference. Defaulting someone
    # into mail they have turned off in their profile is not what the UI does.
    settings = getattr(detail, "settings", None)
    if getattr(settings, "analysis_update_notifications_enabled", True) is False:
        return [], (f"(nobody -- {email} has analysis notifications turned off in "
                    "their Cirro profile)"), []
    return [email], f"{email} (default: the account this session is logged in as)", []


# Required by the execution API rather than by any form: a process whose pipeline code
# is not pinned to a branch rejects a launch that omits it, with a 400 the schema gives
# no warning of -- `required` is empty and the field carries a default. Omitting it
# stranded two completed uploads mid-chain. The spec states the value, so materialising
# it is safe whether or not this particular process enforces it.
SERVER_REQUIRED_PARAM = "workflow_version"


def _resolve_params(reader: reads.Reader, process_id: str,
                    params: dict) -> Tuple[dict, list]:
    """The params a launch will actually send, and what to disclose about them.

    Validates against the spec and reports undeclared keys separately: jsonschema will
    not flag an unknown key unless the schema sets additionalProperties:false, so a
    typo silently does nothing. Finding that out six hours into a pipeline is the
    expensive version of this check.
    """
    warnings = []
    params = dict(params or {})
    try:
        spec = reader.client.processes.get_parameter_spec(process_id=process_id)
        reader.cost.call("processes.parameter_spec")
    except Exception as exc:
        # The exception TYPE, never its text: this warning is part of the approved
        # disclosure, and `_same_disclosure` compares it verbatim at apply. An upstream
        # body carrying a request id or a timestamp would differ between the plan and
        # its re-plan and make every affected plan PLAN_STALE.
        return params, [f"Could not load the parameter spec for validation "
                        f"({type(exc).__name__}), so the parameters below were not "
                        f"checked."]
    fields = reads.spec_shape(reader, process_id)
    injected = _fill_server_required(params, fields)
    if injected is not None:
        warnings.append(
            f"{SERVER_REQUIRED_PARAM} was not supplied and the process declares a "
            f"default, so the launch will send {injected!r}. Cirro rejects a launch "
            f"that omits it.")
    try:
        spec.validate_params(params)
    except Exception as exc:
        raise errors.InvalidQuery(
            f"Parameters rejected by the process spec: {exc}",
            what="Fix the parameters before planning again.",
            field="params", given=params,
            retry_with=[{"verb": "get", "entity": "process", "id": process_id},
                        {"verb": "summarize", "of": "params",
                         "note": "inspect a previous run's params for a working shape"}],
        )
    if fields is None:
        return params, warnings
    body = S.params(params, fields)
    unknown = body["unknown"]
    if unknown:
        # A misplaced key is the expensive one: it validates, the pipeline ignores it,
        # and the run proceeds on the default the caller thought they had overridden.
        misplaced = body.get("misplaced") or {}
        if misplaced:
            warnings.append(
                "Parameters sent at the top level that this process nests, so the "
                "pipeline will ignore them and use its defaults: "
                + ", ".join(f"{flat} belongs at {where}"
                            for flat, where in sorted(misplaced.items())))
        rest = sorted(set(unknown) - set(misplaced))
        if rest:
            warnings.append(
                f"Parameters not declared by this process and therefore ignored: "
                f"{rest} -- usually a typo")
    if body["missing_required"]:
        warnings.append(f"Required parameters absent: {sorted(body['missing_required'])}")
    return params, warnings


def _fill_server_required(params: dict, fields: Optional[list]) -> Optional[str]:
    """Materialise the declared default for the one parameter the API demands.

    Mutates `params` and returns what it set, or None if it set nothing. A declared
    default is a value the caller already has; leaving it unsent is not a choice they
    made, and the API states no default of its own.
    """
    if fields is None or SERVER_REQUIRED_PARAM in params:
        return None
    declared = next((f for f in fields if f["path"] == SERVER_REQUIRED_PARAM), None)
    if declared is None or not declared["has_default"]:
        return None
    params[SERVER_REQUIRED_PARAM] = declared["default"]
    return declared["default"]

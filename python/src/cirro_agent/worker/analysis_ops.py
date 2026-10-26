"""Launching, resuming and stopping pipeline runs.

Registers its operations into `mutations.OPS` on import; see mutations.py for
why the registration lives on this side.

A launch notifies the account the session is authenticated as unless the caller
says otherwise, which is what the web UI does; `_notify` explains the mechanics.
"""

from typing import Optional, Tuple

from .. import errors, summarize as S
from . import mutations, portal, reads, rows as R
from .mutations import (_tag_models, Draft, Op, RISK_WRITE, _check_dataset_name, _check_tags,
                        _inv, _need, _project, _dataset, _target_folders, _wire_tags,
                        FOLDER_ARGS, PROJECT_ARGS)

# run_analysis' whole argument surface, shared with resume_analysis: that planner
# copies the caller's args, fills in whatever the previous run implies, and hands the
# merged dict to plan_run_analysis -- so anything a launch accepts, a resume accepts.
RUN_ANALYSIS_ARGS = PROJECT_ARGS + FOLDER_ARGS + (
    "name", "process", "description", "params", "tags",
    "source_dataset_ids", "dataset_id", "source_sample_ids",
    "notification_emails", "compute_environment", "environment_type",
    "resume_dataset_id",
)


# Where a run executes. These are `EnvironmentType` values from the API, and they are
# deliberately not the `executionModes` vocabulary a process declares: a pipeline says
# HEALTHOMICS, a request says AWS_OMICS. Conflating the two is what makes a wrong value
# look plausible, so `HEALTHOMICS` is rejected by name rather than quietly accepted.
# The mode half is imported rather than restated, so the modes a registration may
# declare and the modes a run may ask for cannot drift apart.
AWS_OMICS = "AWS_OMICS"
AWS_BATCH = "AWS_BATCH"
ENVIRONMENT_TYPES = (AWS_OMICS, AWS_BATCH)


def _environment_type(args: dict, process: dict) -> Optional[str]:
    """Which execution environment to ask for, or None to let Cirro choose.

    None is what the portal sends for Batch -- `resolveEnvironmentType` returns
    undefined rather than AWS_BATCH -- so an explicit AWS_BATCH is normalized to it.
    """
    given = args.get("environment_type")
    if not given:
        return None
    value = str(given).strip().upper()
    if value == R.HEALTHOMICS_MODE:
        raise errors.InvalidQuery(
            f"{R.HEALTHOMICS_MODE!r} is a process execution mode, not an environment "
            f"type; the run request wants {AWS_OMICS!r}.",
            what="Name the environment the run executes in.", field="environment_type",
            given=given, retry_with=[{"environment_type": AWS_OMICS}],
        )
    if value not in ENVIRONMENT_TYPES:
        # NONE and LOCAL_AGENT are real EnvironmentType values and are still refused:
        # the backend rejects NONE outright, and a local-agent run is not something
        # this operation can set up.
        raise errors.InvalidQuery(
            f"environment_type {given!r} is not one this operation accepts.",
            what=f"Use one of {list(ENVIRONMENT_TYPES)}, or omit it for Cirro's "
                 "default.",
            field="environment_type", given=given,
            retry_with=[{"environment_type": v} for v in ENVIRONMENT_TYPES],
        )
    if value == AWS_BATCH:
        return None
    if args.get("compute_environment"):
        # The backend 400s on this combination, and that arrives after approval. A
        # customer compute environment already names where it runs, which is why the
        # portal sends no environmentType whenever one is selected.
        raise errors.InvalidQuery(
            "environment_type and compute_environment cannot both be set.",
            what="A compute environment already names where the run executes; drop "
                 "one of the two.",
            field="environment_type", given=given,
            retry_with=[{"compute_environment": None, "environment_type": AWS_OMICS},
                        {"environment_type": None}],
        )
    modes = process.get("execution_modes") or []
    if R.HEALTHOMICS_MODE not in modes:
        # The backend refuses this too, but refusing it here keeps the failure on the
        # read-only side of the approval gate.
        raise errors.InvalidQuery(
            f"{process['name']!r} does not declare {R.HEALTHOMICS_MODE} and cannot run "
            "on AWS HealthOmics.",
            what="Pick a pipeline that declares HEALTHOMICS, or omit environment_type "
                 "to run on Cirro's default.",
            field="environment_type", given=given, declared_modes=modes,
            retry_with=[{"environment_type": None}],
        )
    return AWS_OMICS


def plan_run_analysis(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "run_analysis", "name", "process")
    _check_dataset_name(args["name"])
    _check_tags(args)
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
    # A pipeline nothing can feed takes no input dataset, and the platform supports
    # that at every layer: `sourceDatasetIds` is @NotNull rather than @NotEmpty, the
    # only server-side check is `size() > 1 && !allowMultipleSources()`, and the portal
    # gates submit on `hasParentProcesses && !sourceDatasets.length`. Demanding one
    # here made every parentless pipeline unlaunchable through the plugin -- including
    # the shipped `process-nf-core-fetchngs`, which downloads from SRA and has no input
    # by design. Where the process does have parents an empty selection is still a
    # mistake, and still refused.
    if not source_ids and reader.has_parent_processes(process["id"]):
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

    environment_type = _environment_type(args, process)

    compute = args.get("compute_environment")
    if environment_type == AWS_OMICS:
        compute_label = "AWS HealthOmics"
    else:
        compute_label = "Cirro default (AWS Batch)"
        if R.HEALTHOMICS_MODE in (process.get("execution_modes") or []):
            # The pipeline declares both, so Batch is a choice here rather than the
            # only place this could run. An approval that says nothing reads as though
            # there were no choice, which is how a run lands on the wrong one.
            compute_label += " -- this pipeline also declares HEALTHOMICS"
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
        # None means "omit", which is how the portal asks for Batch. Sending AWS_BATCH
        # explicitly is a different request and interacts differently with the
        # backend's compute-environment conflict check.
        "environment_type": environment_type,
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
    ]
    if inputs:
        lines.append(f"  Inputs       {len(inputs)} dataset(s), {total_files} files, "
                     f"{S.human_bytes(total_bytes)}")
        for item in inputs:
            lines.append(f"                 - {item['name']} ({item['id']}, "
                         f"{item['file_count']} files, {item['size_human']})")
    else:
        # "0 dataset(s), 0 files, 0 B" reads like a mistake. This is the deliberate
        # case, and the approval should say so.
        lines.append("  Inputs       none -- nothing feeds this pipeline, so it runs "
                     "with no input dataset, as it would from the web UI")
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
                 invalidates=[_inv("datasets", project["id"])],
                 ungrantable=mutations.ungrantable(
                     args.get("notification_emails")
                     and "it emails addresses it was given",
                     len(inputs) > mutations.GRANT_MAX_TARGETS
                     and f"it reads more than {mutations.GRANT_MAX_TARGETS} input datasets"))


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
    if args.get("environment_type"):
        from cirro_api_client.v1.models import EnvironmentType
        request.environment_type = EnvironmentType(args["environment_type"])
    # Folders are tags, and RunAnalysisRequest carries tags -- so the output dataset
    # is filed at launch, in the same call, instead of needing an update afterwards.
    combined = _wire_tags(args.get("folders"), args.get("tags"))
    if combined:
        request.tags = _tag_models(combined)
    execution = reader.client.execution
    try:
        response = execution.run_analysis(project_id=args["project_id"], request=request)
    except Exception as exc:
        raise _launch_failure(exc, args) from exc
    created = R.plain(getattr(response, "id", None))
    return {"created_dataset_id": created,
            "message": R.plain(getattr(response, "message", None)),
            "folders": args.get("folders") or [],
            "follow_with": {"verb": "get", "entity": "dataset", "id": created,
                            "scope": {"project_ids": [args["project_id"]]},
                            "freshness": "fresh"}}


# The generated route a launch is posted through. The SDK reads the parameter spec
# before it posts, so a failure that never passed through here came before the launch.
LAUNCH_ROUTE = "cirro_api_client.v1.api.execution.run_analysis"


def _launch_failure(exc: Exception, args: dict) -> errors.AgentError:
    """What a failed launch call means for whether a run now exists.

    A refusal (a 4xx) or a failure before the launch was sent started nothing, so
    planning again is safe. Anything else may follow a launch Cirro went ahead with: a
    2xx whose body could not be read, a 5xx (a gateway gives up on a request the
    backend goes on to finish), or a connection that ended before the reply. Reported
    as a retryable upstream fault, any of these invites a second launch -- a bare
    `JSONDecodeError` reached people that way, with nothing saying whether the run had
    started.
    """
    reply = portal.http_response(exc)
    # A token is fetched, and refreshed if due, inside the route's own frames but
    # before the request leaves; failing there sent nothing, and is a sign-in problem
    # rather than an unknown launch.
    signing_in = portal.raised_in(exc, "cirro.auth") or portal.raised_in(
        exc, "msal_extensions")
    if signing_in or not portal.raised_in(exc, LAUNCH_ROUTE) or (
            reply is not None and reply.is_client_error):
        return portal.wrap_upstream(
            exc, what="No run was started: Cirro refused the launch, or the failure "
                      "came before it was sent. Planning again is safe.",
            retry_with=[{"tool": "plan", "op": "run_analysis"}])
    if reply is not None and reply.is_success:
        said = (f"The launch of {args['name']!r} was accepted (HTTP {reply.status_code}) "
                f"but the reply had {portal.describe_body(reply)}, so the run has most "
                "likely started and its dataset id is not known.")
    else:
        answer = ("the connection ended before any reply" if reply is None else
                  f"Cirro answered HTTP {reply.status_code} with "
                  f"{portal.describe_body(reply)}")
        said = (f"The launch of {args['name']!r} was sent and {answer}, so whether the "
                "run started is not known.")
    return errors.OutcomeUnknown(
        said,
        what="Cirro may have started it, and planning the launch again could start a "
             "second run.",
        retry_with=[{"verb": "query", "entity": "dataset",
                     "scope": {"project_ids": [args["project_id"]]},
                     "filter": {"field": "name", "op": "eq", "value": args["name"]},
                     "freshness": "fresh"}],
        user_action=("Check the project for a dataset of this name created just now "
                     "before launching again; launch again only if there is none."),
        http_status=None if reply is None else reply.status_code,
        raised=f"{type(exc).__name__}: {exc}",
    )


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
       outputs=("created_dataset_id",), accepts=RUN_ANALYSIS_ARGS, grantable=True),
    # Shares run_analysis's executor, so a resume records a created dataset too.
    Op("resume_analysis", RISK_WRITE, plan_resume_analysis, apply_run_analysis,
       outputs=("created_dataset_id",), accepts=RUN_ANALYSIS_ARGS, grantable=True),
    Op("stop_analysis", RISK_WRITE, plan_stop_analysis, apply_stop_analysis,
       accepts=PROJECT_ARGS + ("dataset_id",)),
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
    if _fill_server_required(params, fields):
        warnings.append(
            f"{SERVER_REQUIRED_PARAM} was not supplied and the process declares a "
            f"default, so the launch will send {params[SERVER_REQUIRED_PARAM]!r}. "
            f"Cirro rejects a launch that omits it.")
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


def _fill_server_required(params: dict, fields: Optional[list]) -> bool:
    """Materialise the declared default for the one parameter the API demands.

    Mutates `params`; True if it set the value. A boolean rather than the value it
    set, because a schema is free to declare a default of null and "set nothing" has
    to stay distinguishable from it. A declared default is a value the caller already
    has; leaving it unsent is not a choice they made, and the API states no default of
    its own.
    """
    if fields is None or SERVER_REQUIRED_PARAM in params:
        return False
    declared = next((f for f in fields if f["path"] == SERVER_REQUIRED_PARAM), None)
    if declared is None or not declared["has_default"]:
        return False
    params[SERVER_REQUIRED_PARAM] = declared["default"]
    return True

"""Launching, resuming and stopping pipeline runs.

Registers its operations into `mutations.OPS` on import; see mutations.py for
why the registration lives on this side.

A launch notifies the account the session is authenticated as unless the caller
says otherwise, which is what the web UI does; `_notify` explains the mechanics.

A run's input datasets are the ones it names and the ones its picked paths point into.
A form with path pickers or a samplesheet (`formcheck.browse_mode`) has no input-dataset
selector in the portal: the person picks files from the whole project, and the portal
and the run API both read the inputs off the Cirro URIs in the params and the rows.
The planner reads them the same way, so the approval names every dataset the run reads.
"""
import re
from typing import Optional, Tuple

from .. import errors, formcheck, sheets, summarize as S
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
    "resume_dataset_id", "structured_inputs",
)

# Rows one samplesheet may carry; Cirro refuses a launch with more.
MAX_STRUCTURED_ROWS = 10_000

# The path formats whose value has to be a file rather than a folder.
FILE_FORMATS = ("file-path", "file-path-pattern")

# A value a form leaves blank.
_BLANK = (None, "")


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

    None is what the web app sends for Batch -- it omits the field rather than sending
    AWS_BATCH -- so an explicit AWS_BATCH is normalized to it.
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
        # Cirro rejects NONE outright, and a local-agent run is not something
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
        # Cirro 400s on this combination, and that arrives after approval. A
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
        # Cirro refuses this too, but refusing it here keeps the failure on the
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

    document, warnings = _run_form(reader, process["id"])
    if args.get("structured_inputs") and document is None:
        raise errors.UpstreamError(
            "The run form could not be read, so the samplesheet's file format is not "
            "known and nothing can be sent for it.",
            what="Planning again reads the form again.",
            retry_with=[{"tool": "plan", "op": "run_analysis"}],
        )
    # Every dataset a picked path points into, with where it was picked.
    picked_from: dict = {}
    params = _picked_params(reader, project["id"], args.get("params") or {}, document,
                            picked_from, warnings)
    tables = _tables(reader, project["id"], document, args.get("structured_inputs"),
                     picked_from, warnings)

    source_ids = args.get("source_dataset_ids") or []
    if not source_ids and args.get("dataset_id"):
        source_ids = [args["dataset_id"]]
    # In Cirro's order: named first, then what the rows and params point into.
    # dict.fromkeys rather than a set, whose order changes between the plan and the
    # re-plan at apply and would make every such plan stale.
    input_ids = list(dict.fromkeys([*source_ids, *picked_from]))
    # A pipeline nothing can feed takes no input dataset, and Cirro accepts that: the
    # launch's `source_dataset_ids` is required but may be empty, a process's
    # `allow_multiple_sources` limits it only from above, and the portal submits such a
    # run for a pipeline with no parent process. Demanding one here made every
    # parentless pipeline unlaunchable through the plugin -- including the shipped
    # `process-nf-core-fetchngs`, which downloads from SRA and has no input by design.
    # Where the process does have parents an empty selection is still a mistake, and
    # still refused -- but a picked path counts as one, as in the portal.
    if not input_ids and reader.has_parent_processes(process["id"]):
        raise errors.InvalidQuery(
            "run_analysis needs at least one input dataset.",
            what="Name the dataset(s) to analyze, or pick files in them through the "
                 "form's path parameters or samplesheet.",
            field="source_dataset_ids",
            retry_with=[{"verb": "query", "entity": "dataset",
                         "scope": {"project_ids": [project["id"]]}},
                        {"verb": "process_params", "id": process["id"]}],
        )

    inputs, total_files, total_bytes = [], 0, 0
    for dataset_id in input_ids:
        row = _dataset(reader, project["id"], dataset_id)
        count = reads.file_count(reader, project["id"], row["id"])
        detail = reads.dataset_detail(reader, project["id"], row["id"], row)
        total_files += count or 0
        total_bytes += detail.get("total_size_bytes") or 0
        inputs.append({"id": row["id"], "name": row["name"], "file_count": count,
                       "size_human": detail.get("total_size_human"),
                       "via": None if dataset_id in source_ids else picked_from[dataset_id]})

    params, param_warnings = _resolve_params(document, params)
    warnings += param_warnings

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
        "structured_inputs": tables,
        "notification_emails": notify,
        "compute_environment_id": compute,
        # None means "omit", which is how the portal asks for Batch. Sending AWS_BATCH
        # explicitly is a different request and interacts differently with
        # Cirro's compute-environment conflict check.
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
                         f"{item['file_count']} files, {item['size_human']})"
                         + (f", picked in {item['via']}" if item["via"] else ""))
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
    for name, table in tables.items():
        rows = table["rows"]
        lines += ["", f"  Samplesheet {name} ({table['file_format']}, {len(rows)} "
                      f"row{'' if len(rows) == 1 else 's'}), written to a file the "
                      "pipeline reads:"]
        lines += [f"    {index}. " + ", ".join(f"{k}={v!r}" for k, v in row.items())
                  for index, row in enumerate(rows[:mutations.GRANT_MAX_TARGETS], 1)]
        more = len(rows) - mutations.GRANT_MAX_TARGETS
        if more > 0:
            lines.append(f"    ... and {more} more row{'' if more == 1 else 's'}")
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
                     and f"it reads more than {mutations.GRANT_MAX_TARGETS} input datasets",
                     any(len(t["rows"]) > mutations.GRANT_MAX_TARGETS
                         for t in tables.values())
                     and f"its samplesheet has more than {mutations.GRANT_MAX_TARGETS} "
                         "rows"))


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
    tables = args.get("structured_inputs") or {}
    if tables:
        from cirro_api_client.v1.models import RunAnalysisRequestStructuredFileInputs
        request.structured_inputs = RunAnalysisRequestStructuredFileInputs.from_dict({
            name: {"data": table["rows"], "fileFormat": table["file_format"],
                   "importSourceUris": []}
            for name, table in tables.items()})
    from cirro_api_client.v1.api.execution import run_analysis
    try:
        response = run_analysis.sync_detailed(
            project_id=args["project_id"], client=reader.client._api_client, body=request)
    except Exception as exc:
        raise _launch_failure(exc, args) from exc
    created = R.plain(getattr(response.parsed, "id", None))
    return {"created_dataset_id": created,
            "message": R.plain(getattr(response.parsed, "message", None)),
            "folders": args.get("folders") or [],
            "follow_with": {"verb": "get", "entity": "dataset", "id": created,
                            "scope": {"project_ids": [args["project_id"]]},
                            "freshness": "fresh"}}


# The generated route a launch is posted through, called directly rather than through
# the SDK's ExecutionService: that one validates params against the form as served,
# which refuses the list a multi-select picker sends. The client raises from inside the
# route on any reply but a 200, so a failure that never passed through here came
# before the launch.
LAUNCH_ROUTE = "cirro_api_client.v1.api.execution.run_analysis"


def _launch_failure(exc: Exception, args: dict) -> errors.AgentError:
    """What a failed launch call means for whether a run now exists.

    A refusal (a 4xx) or a failure before the launch was sent started nothing, so
    planning again is safe. Anything else may follow a launch Cirro went ahead with: a
    2xx whose body could not be read, a 5xx (a gateway gives up on a request
    Cirro goes on to finish), or a connection that ended before the reply. Reported
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
        # A grant is for cancelling the person's own runs. Someone else's run is
        # their compute and their wait, and a resume does not give either back.
        ungrantable=mutations.ungrantable(
            row["created_by"] != reader.username
            and "it stops a run another user started"),
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
       accepts=PROJECT_ARGS + ("dataset_id",), grantable=True),
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


def _run_form(reader: reads.Reader, process_id: str) -> Tuple[Optional[dict], list]:
    """The process's run form, or None and the warning that says it went unchecked."""
    try:
        document = reads.process_parameters(reader, process_id)
    except Exception as exc:
        document, failure = None, type(exc).__name__
    else:
        failure = "NotFound"
    if isinstance(document, dict):
        return document, []
    # The exception TYPE, never its text: this warning is part of the approved
    # disclosure, and `_same_disclosure` compares it verbatim at apply. An upstream body
    # carrying a request id or a timestamp would differ between the plan and its
    # re-plan and make every affected plan PLAN_STALE.
    return None, [f"Could not load the parameter spec for validation ({failure}), so "
                  "the parameters below were not checked."]


def _at(params: dict, path: str):
    """The value at a dotted params path, or None where there is none."""
    node = params
    for step in path.split("."):
        if not isinstance(node, dict) or step not in node:
            return None
        node = node[step]
    return node


def _uris(value, where: str) -> list:
    """(where, value) for every string in `value` that is a dataset reference -- what the
    run API scans params and rows for to find a run's inputs."""
    if isinstance(value, str):
        return [(where, value)] if sheets.parse_dataset_uri(value) else []
    if isinstance(value, dict):
        return [hit for key, item in value.items() for hit in _uris(item, f"{where}.{key}")]
    if isinstance(value, list):
        return [hit for index, item in enumerate(value)
                for hit in _uris(item, f"{where}[{index}]")]
    return []


def _referenced(reader: reads.Reader, project_id: str, value: str, where: str) -> str:
    """The id of the dataset a reference names, refused where Cirro would refuse.

    Cirro resolves a reference only if it names the dataset's own project, or
    none; a shared dataset's own project is the one it was shared from.
    """
    uri = sheets.parse_dataset_uri(value)
    row = _dataset(reader, project_id, uri["dataset_id"])
    owner = row.get("project_id") or project_id
    if uri["project_id"] and uri["project_id"] != owner:
        raise errors.InvalidQuery(
            f"{where} names dataset {row['id']} in project {uri['project_id']}, but it "
            f"belongs to {owner}; Cirro refuses a reference that names another project.",
            what="Name the dataset's own project, or leave the project out.",
            field=where, given=value,
            retry_with=[{"value": sheets.dataset_uri(owner, row["id"], uri["path"])}],
        )
    return row["id"]


def _picked(reader: reads.Reader, project_id: str, value, field: dict, where: str,
            warnings: list, pattern: Optional[str] = None):
    """A picked path as the reference the launch sends.

    `{"dataset": <id>, "path": <path in its data folder>}` becomes a reference to the
    dataset's own project, so a chain step can feed a dataset an earlier step created;
    a reference is checked against what the picker accepts. Anything else passes
    through as it is, as a typed S3 path always has.
    """
    if isinstance(value, dict):
        if set(value) - {"dataset", "path"} or not value.get("dataset"):
            raise errors.InvalidQuery(
                f"{where} as an object takes {{\"dataset\": <id>, \"path\": <path in its "
                "data folder>}.",
                what="Name the dataset by id and the path within it; an empty path is "
                     "its whole data folder.",
                field=where, given=value,
                retry_with=[{"value": {"dataset": "DATASET_ID", "path": ""}}],
            )
        row = _dataset(reader, project_id, value["dataset"])
        value = sheets.dataset_uri(row.get("project_id") or project_id, row["id"],
                                   str(value.get("path") or ""))
    uri = sheets.parse_dataset_uri(value)
    if uri is None:
        return value
    if uri["path"] is None or (field.get("format") in FILE_FORMATS and not uri["path"]):
        raise errors.InvalidQuery(
            f"{where} picks a {'file' if field.get('format') in FILE_FORMATS else 'path'}"
            f", and {value!r} names {'the dataset' if uri['path'] is None else 'a folder'}.",
            what="Name a path inside the dataset's data folder.",
            field=where, given=value,
            retry_with=[{"verb": "get", "entity": "dataset", "id": uri["dataset_id"],
                         "include": ["files"]}],
        )
    if pattern:
        try:
            fits = re.search(pattern, uri["path"]) is not None
        except re.error:
            note = (f"{where} has a pattern this check cannot read, so the path picked "
                    "for it was not checked against it.")
            if note not in warnings:
                warnings.append(note)
            fits = True
        if not fits:
            raise errors.InvalidQuery(
                f"{where}: {uri['path']!r} does not match the form's pattern "
                f"{pattern!r}, so the portal would not offer it.",
                what="Pick a path the form accepts.", field=where, given=value,
                retry_with=[{"verb": "process_params", "note": "see what it accepts"}],
            )
    return value


def _picked_params(reader: reads.Reader, project_id: str, params: dict,
                   document: Optional[dict], picked_from: dict, warnings: list) -> dict:
    """`params` with each picked path as the reference the launch sends, recording in
    `picked_from` every dataset a reference in them points into."""
    import copy
    params = copy.deepcopy(params or {})
    form = (document or {}).get("form") or {}
    for record in formcheck.walk_form(form, ""):
        field = record["field"]
        if not isinstance(field, dict) or field.get("format") not in formcheck.PATH_FORMATS:
            continue
        holder = _at(params, record["owner"]) if record["owner"] else params
        if not isinstance(holder, dict) or holder.get(record["name"]) in _BLANK:
            continue
        where = f"params.{record['path']}"
        value = holder[record["name"]]
        picks = value if isinstance(value, list) else [value]
        resolved = [_picked(reader, project_id, item, field, where, warnings,
                            pattern=field.get("pattern")) for item in picks]
        holder[record["name"]] = resolved if isinstance(value, list) else resolved[0]
    for where, value in _uris(params, "params"):
        dataset_id = _referenced(reader, project_id, value, where)
        picked_from.setdefault(dataset_id, f"parameter {where[len('params.'):]}")
    return params


def _tables(reader: reads.Reader, project_id: str, document: Optional[dict], given,
            picked_from: dict, warnings: list) -> dict:
    """Each samplesheet as the launch sends it: {name: {"file_format", "rows"}}.

    Cirro writes the rows as they arrive -- it checks none of them against the
    form -- so this is the only check a row gets before a pipeline reads it.
    """
    definitions = reads.structured_inputs(document)
    given = given or {}
    if not isinstance(given, dict):
        raise errors.InvalidQuery(
            "structured_inputs takes {<samplesheet name>: [<row>, ...]}.",
            what="Key each samplesheet's rows by its name.", field="structured_inputs",
            given=given,
            retry_with=[{"verb": "process_params", "note": "names its samplesheets"}],
        )
    unknown = sorted(set(given) - set(definitions))
    if unknown:
        raise errors.InvalidQuery(
            f"This process has no samplesheet {', '.join(map(repr, unknown))}.",
            what=("Use a samplesheet the process defines." if definitions else
                  "This process defines no samplesheet; drop structured_inputs."),
            field="structured_inputs", given=unknown, candidates=sorted(definitions),
            retry_with=[{"verb": "process_params", "note": "names its samplesheets"}],
        )
    tables = {}
    for name, definition in definitions.items():
        rows = given.get(name) or []
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise errors.InvalidQuery(
                f"structured_inputs.{name} must be a list of rows, each an object of "
                "column values.",
                what="Send one object per row.", field=f"structured_inputs.{name}",
                given=rows, retry_with=[{"verb": "process_params"}],
            )
        if not rows:
            if definition["required"]:
                raise errors.InvalidQuery(
                    f"The form requires the samplesheet {name!r}, and no rows were given "
                    "for it.",
                    what=f"Send its rows as structured_inputs.{name}.",
                    field=f"structured_inputs.{name}",
                    retry_with=[{"verb": "process_params", "note": "lists its columns"}],
                )
            continue
        if len(rows) > MAX_STRUCTURED_ROWS:
            raise errors.InvalidQuery(
                f"structured_inputs.{name} has {len(rows)} rows; a launch carries at most "
                f"{MAX_STRUCTURED_ROWS}.",
                what="Split the run.", field=f"structured_inputs.{name}",
                retry_with=[{"note": "launch it as several runs"}],
            )
        tables[name] = {"file_format": definition["file_format"],
                        "rows": [_row(reader, project_id, name, definition, row, index,
                                      picked_from, warnings)
                                 for index, row in enumerate(rows, 1)]}
    return tables


def _row(reader: reads.Reader, project_id: str, name: str, definition: dict, row: dict,
         index: int, picked_from: dict, warnings: list) -> dict:
    where = f"structured_inputs.{name}[{index}]"
    columns = [c["name"] for c in definition["columns"]]
    extra = [key for key in row if key not in columns]
    if extra:
        raise errors.InvalidQuery(
            f"{where} has {', '.join(map(repr, extra))}, which is not a column of {name!r}.",
            what="Send only the samplesheet's columns.", field=where, given=row,
            candidates=columns, retry_with=[{"verb": "process_params"}],
        )
    specs = definition["row_schema"].get("properties") or {}
    cells = {}
    for column in columns:
        value = row.get(column)
        spec = specs.get(column) if isinstance(specs.get(column), dict) else {}
        if spec.get("format") in formcheck.PATH_FORMATS and value not in _BLANK:
            # The column's pattern is checked with the rest of the row below.
            value = _picked(reader, project_id, value, spec, f"{where}.{column}", warnings)
        cells[column] = value
    # Every reference in a row is an input to the run, whatever its column.
    for found, value in _uris(cells, where):
        picked_from.setdefault(_referenced(reader, project_id, value, found),
                               f"samplesheet {name}")
    _check_row(definition, cells, where, warnings)
    if definition["file_format"] == "JSON":
        return {column: cells[column] for column in columns if column in row}
    # A CSV or TSV takes its header from the order keys first appear across the rows,
    # so a row without an optional column would move that column to the end.
    return cells


def _check_row(definition: dict, cells: dict, where: str, warnings: list) -> None:
    """The row against the samplesheet's schema, as the portal checks a row it shows:
    blanks left out, and a file cell checked as the path it picks."""
    import jsonschema
    schema = definition["row_schema"]
    paths = {c["name"] for c in definition["columns"] if c["format"] in formcheck.PATH_FORMATS}
    payload = {}
    for column, value in cells.items():
        if value in _BLANK:
            continue
        uri = sheets.parse_dataset_uri(value) if column in paths else None
        payload[column] = uri["path"] if uri else value
    try:
        validator = jsonschema.validators.validator_for(schema)(schema)
        problems = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    except (jsonschema.SchemaError, re.error):
        note = ("A samplesheet's column rules could not be read, so its rows were not "
                "checked against them.")
        if note not in warnings:
            warnings.append(note)
        return
    if not problems:
        return
    specs = schema.get("properties") or {}
    said = []
    for problem in problems[:5]:
        column = problem.path[0] if problem.path else None
        spec = specs.get(column) if column is not None else None
        custom = spec.get("errorMessage") if isinstance(spec, dict) else None
        said.append(f"{column}: {custom or problem.message}" if column else problem.message)
    raise errors.InvalidQuery(
        f"{where} does not fit the samplesheet: {'; '.join(said)}",
        what="Fix the row; Cirro writes it into the file exactly as given.",
        field=where, given=cells, retry_with=[{"verb": "process_params",
                                               "note": "lists each column's rules"}],
    )


def _resolve_params(document: Optional[dict], params: dict) -> Tuple[dict, list]:
    """The params a launch will actually send, and what to disclose about them.

    Validates against the form as the portal submits it, and reports undeclared keys
    separately: jsonschema will not flag an unknown key unless the schema sets
    additionalProperties:false, so a typo silently does nothing. Finding that out six
    hours into a pipeline is the expensive version of this check.
    """
    import jsonschema
    warnings = []
    params = dict(params or {})
    if document is None:
        return params, warnings
    for name, table in reads.structured_inputs(document).items():
        if _at(params, table["field"]) is not None:
            raise errors.InvalidQuery(
                f"params.{table['field']} is the samplesheet {name!r}; its rows go in "
                f"structured_inputs.{name}, and a value for it in params is never sent.",
                what="Move the rows to structured_inputs.", field=f"params.{table['field']}",
                retry_with=[{"structured_inputs": {name: []}}],
            )
    form = reads.launch_form(document)
    fields = reads.form_fields(form)
    if _fill_server_required(params, fields):
        warnings.append(
            f"{SERVER_REQUIRED_PARAM} was not supplied and the process declares a "
            f"default, so the launch will send {params[SERVER_REQUIRED_PARAM]!r}. "
            f"Cirro rejects a launch that omits it.")
    try:
        # The same call the SDK's own check made, so a form is held to the draft
        # launches have always been checked under. A draft-07 validator would begin
        # enforcing `dependencies`, which this one ignores, and refuse launches of
        # conditional forms that go through today.
        jsonschema.validate(instance=params, schema=form)
    except jsonschema.ValidationError as exc:
        raise errors.InvalidQuery(
            f"Parameters rejected by the process spec: Parameter at {exc.json_path} "
            f"error: {exc.message}",
            what="Fix the parameters before planning again.",
            field="params", given=params,
            retry_with=[{"verb": "process_params",
                         "note": "the parameter shape the process accepts"},
                        {"verb": "summarize", "of": "params",
                         "note": "inspect a previous run's params for a working shape"}],
        )
    except (jsonschema.SchemaError, re.error):
        warnings.append("The form's own rules could not be read, so the parameters below "
                        "were checked only for undeclared and missing keys.")
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

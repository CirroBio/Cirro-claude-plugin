"""Summaries of the things that would otherwise flood the context:
files, logs, tasks, samples, parameters, trace, cost.
"""
from typing import Optional, Tuple

from .. import errors, summarize as S
from ..envelope import DEFAULT_MAX_TOKENS, estimate_tokens, wrap
from .reads import (MANIFEST_LIST_LIMIT, Reader, dataset_cost, dataset_detail,
                    dataset_manifest, dataset_tasks, declared_schema_keys, is_terminal,
                    portal_dataset, project_schema_document, spec_shape)


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
    named_project_ids = reader.scope_project_ids(scope)
    # A summary describes one project, so the echo, the builders and their follow-up
    # calls all carry the project actually summarized rather than everything named.
    project_id = named_project_ids[0]
    project_ids = [project_id]
    dataset_id = request.get("dataset_id") or scope.get("dataset_id")
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["summarize"])

    if of != "samples" and not dataset_id:
        raise errors.ScopeRequired(
            f"summarize {of} needs a dataset_id.",
            what="Name the dataset to summarize.", field="dataset_id",
            retry_with=[{"verb": "query", "entity": "dataset",
                         "scope": {"project_ids": named_project_ids}}],
        )
    scope_echo = {"project_ids": project_ids}
    if dataset_id:
        scope_echo["dataset_id"] = dataset_id
    build = _BUILDERS[of]
    body, complete, next_calls = build(reader, request, project_ids, project_id, dataset_id)
    omitted = _projects_left_out(of, named_project_ids, dataset_id)
    if of == "logs":
        omitted += _log_middle_left_out(body, dataset_id, project_ids)
    return _summary_envelope(reader, request, of, body, complete, scope_echo,
                             max_tokens, next_calls=next_calls, omitted=omitted)


def _log_middle_left_out(body: dict, dataset_id: Optional[str],
                         project_ids: list) -> list:
    """The span between the head and the tail, declared rather than silently dropped.

    `error_blocks` reaches into that span, but only where a line carries an error
    marker. A preprocess trace that carries none is in the response nowhere -- and on
    a 167-line log the middle is where it lives.
    """
    gap = body.get("elided")
    if not gap:
        return []
    return [{
        "code": "LOG_MIDDLE_ELIDED",
        "detail": (f"lines {gap['range'][0]}-{gap['range'][1]} ({gap['lines']} of "
                   f"{body['lines']}) sit between the head and tail shown. "
                   "`error_blocks` covers the marked errors in that span; anything "
                   "carrying no error marker is not in this response."),
        "fix": {"note": "read that span verbatim",
                "retry_with": [{"verb": "summarize", "of": "logs",
                                "dataset_id": dataset_id,
                                "scope": {"project_ids": project_ids},
                                "raw": True, "line_range": gap["range"]}]},
    }]


def _samples_body(reader, request, project_ids, project_id,
                  dataset_id) -> Tuple[dict, bool, list]:
    sample_rows, complete = reader.samples(project_id)
    body = S.samples(sample_rows,
                     schema_keys=_declared_metadata_keys(reader, project_id))
    reader.cost.strategy = "snapshot"
    group_keys = [f"metadata.{key}" for key in body.get("candidate_group_keys", [])[:1]]
    return body, complete, [{
        "id": "nc_group",
        "why": "group by a metadata key that is actually worth grouping on",
        "call": {"verb": "query", "entity": "sample",
                 "scope": {"project_ids": project_ids},
                 "group_by": group_keys or ["created_at:month"],
                 "metrics": ["count"]},
    }]


def _files_body(reader, request, project_ids, project_id,
                dataset_id) -> Tuple[dict, bool, list]:
    limit = int(request.get("file_limit") or MANIFEST_LIST_LIMIT)
    entries, total, artifacts = dataset_manifest(
        reader, project_id, dataset_id, file_limit=limit,
        is_terminal=is_terminal(reader, project_id, dataset_id))
    all_entries = entries
    glob = request.get("glob")
    if glob:
        import fnmatch
        entries = [e for e in entries if fnmatch.fnmatch(e["path"].lower(), glob.lower())]
    body = S.files(entries, total_files=total, artifact_types=artifacts)
    if glob:
        body["filtered_by_glob"] = glob
        body["note"] = (f"{len(entries)} of {total} files match {glob!r}; the figures "
                        "above describe the matching subset")
    reader.cost.strategy = "manifest_probe"
    # Honest completeness: the manifest is capped at file_limit.
    complete = total is None or len(all_entries) >= total
    return body, complete, [{
        "id": "nc_read",
        "why": "read one file's contents without downloading",
        "call": {"verb": "summarize", "of": "files", "dataset_id": dataset_id,
                 "scope": {"project_ids": project_ids},
                 "glob": (body.get("suggested_globs") or [{"glob": "*"}])[0]["glob"]}}]


def _logs_body(reader, request, project_ids, project_id,
               dataset_id) -> Tuple[dict, bool, list]:
    text = reader.client.execution.get_execution_logs(project_id=project_id,
                                                      dataset_id=dataset_id) or ""
    reader.cost.call("execution.logs")
    body = S.logs(text, head=int(request.get("head") or 30),
                  tail=int(request.get("tail") or 60))
    if request.get("raw"):
        body["raw"] = _raw_slice(text, request)
    reader.cost.strategy = "execution_live"
    return body, True, [{
        "id": "nc_tasks",
        "why": "find the root-cause task rather than reading more log",
        "call": {"verb": "summarize", "of": "tasks", "dataset_id": dataset_id,
                 "scope": {"project_ids": project_ids}},
    }]


def _tasks_body(reader, request, project_ids, project_id,
                dataset_id) -> Tuple[dict, bool, list]:
    task_rows = dataset_tasks(reader, project_id, dataset_id)
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
    return body, True, next_calls


def _cost_body(reader, request, project_ids, project_id,
               dataset_id) -> Tuple[dict, bool, list]:
    body = S.cost(dataset_cost(reader, project_id, dataset_id))
    reader.cost.strategy = "execution_live"
    next_calls = []
    if body.get("wasted_cost"):
        next_calls.append({
            "id": "nc_failed",
            "why": "see which tasks burned the money that produced nothing",
            "call": {"verb": "summarize", "of": "tasks", "dataset_id": dataset_id,
                     "scope": {"project_ids": project_ids}}})
    next_calls.append({
        "id": "nc_trace",
        "why": "check whether the expensive processes were over-provisioned",
        "call": {"verb": "summarize", "of": "trace", "dataset_id": dataset_id,
                 "scope": {"project_ids": project_ids}}})
    return body, True, next_calls


def _params_body(reader, request, project_ids, project_id,
                 dataset_id) -> Tuple[dict, bool, list]:
    row = dataset_detail(reader, project_id, dataset_id)
    body = S.params(row.get("params") or {}, spec_shape(reader, row.get("process_id")))
    body["process"] = row.get("process")
    reader.cost.strategy = "detail+parameter_spec"
    return body, True, [{
        "id": "nc_spec",
        "why": "read the full parameter spec before changing params",
        "call": {"verb": "get", "entity": "process", "id": row.get("process_id"),
                 "include": ["custom_settings"]},
    }]


def _trace_body(reader, request, project_ids, project_id,
                dataset_id) -> Tuple[dict, bool, list]:
    body = _read_trace(reader, project_id, dataset_id)
    reader.cost.strategy = "artifact_read"
    return body, True, []


# One builder per summary kind, all named _<kind>_body and all with the same
# signature, so do_summarize is the validation and the envelope and nothing else.
_BUILDERS = {"samples": _samples_body, "files": _files_body, "logs": _logs_body,
             "tasks": _tasks_body, "params": _params_body, "trace": _trace_body,
             "cost": _cost_body}


def _projects_left_out(of: str, named: list, dataset_id) -> list:
    """What a narrowed scope did not cover.

    A summary reads `named[0]`; echoing every project_id the caller gave reported one
    project's figures as though they covered all of them.

    A dataset-scoped summary is reported too, and that is deliberate. It looks like
    nothing is omitted -- the dataset lives in exactly one project -- but if it lives
    in one of the projects that were not consulted, the read comes back EMPTY and the
    caller has no way to tell that from a dataset with no samples. Same rule and same
    key as `read_query._projects_left_out`, so `summarize files` and `query file`
    answer the same way for the same scope.
    """
    if len(named) < 2:
        return []
    skipped = named[1:]
    if dataset_id:
        detail = (f"{len(named)} projects were named. Dataset {dataset_id} was read "
                  f"from {named[0]} only; a dataset belongs to one project, so if it "
                  "lives in one of the others this answer is empty rather than wrong.")
        fix = {"note": "Name the project that owns the dataset.",
               "retry_with": [{"verb": "summarize", "of": of, "dataset_id": dataset_id,
                               "scope": {"project_ids": [pid]}} for pid in skipped[:3]]}
    else:
        detail = (f"{len(named)} projects were named. Every figure here describes "
                  f"{named[0]} only; one summary covers one project.")
        fix = {"note": "Summarize one project at a time.",
               "retry_with": [{"verb": "summarize", "of": of,
                               "scope": {"project_ids": [pid]}} for pid in skipped[:3]]}
    return [{"code": "SCOPE_NARROWED", "projects_not_read": skipped,
             "detail": detail, "fix": fix}]


def _declared_metadata_keys(reader: Reader, project_id: str) -> Optional[list]:
    """The metadata keys the project's schema declares, or None if it cannot be read.

    `get_project_schema` returns the generated FormSchema -- form / ui /
    metadataRequirements, with any unrecognised top-level key kept in
    additional_properties. It has no `form_spec_json`; reading that absent attribute
    declared no keys at all, so every observed key was reported as drift. Both halves
    are read because which one a tenant populates is not fixed, and an unreadable
    schema stays None: S.samples then omits schema_drift instead of reporting the
    whole match set as undeclared.
    """
    try:
        document = project_schema_document(reader, project_id)
    except Exception:
        return None
    if document is None:
        return None
    keys = declared_schema_keys(document)
    # An empty document declares nothing; one in an unrecognised shape is unknown.
    if keys is not None:
        return keys
    return [] if not document else None


def _primary_failed_index(reader: Reader, project_id: str, dataset_id: str,
                          task_rows: list) -> Optional[int]:
    """Ask the SDK which task is the root cause; fall back to the earliest failure."""
    try:
        wrapped = portal_dataset(reader, project_id, dataset_id)
        primary = wrapped.primary_failed_task
        if primary is not None:
            return getattr(primary, "task_id", None)
    except Exception:
        pass
    failed = [t for t in task_rows if "FAIL" in str(t.get("status", "")).upper()]
    failed.sort(key=lambda t: str(t.get("started_at") or ""))
    return failed[0]["task_id"] if failed else None


# What S3 and STS say when the credential may not read the object or list the bucket.
_DENIAL_MARKERS = ("accessdenied", "not authorized", "forbidden", "403")


def _is_denial(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _DENIAL_MARKERS)


def _read_trace(reader: Reader, project_id: str, dataset_id: str) -> dict:
    try:
        wrapped = portal_dataset(reader, project_id, dataset_id)
        frame = wrapped.get_trace()
        reader.cost.call("artifact.trace")
    except errors.AgentError:
        # An auth expiry or a missing dataset must keep its own diagnosis rather than
        # being reported as a missing trace artifact.
        raise
    except Exception as exc:
        alternatives = [{"verb": "summarize", "of": "tasks", "dataset_id": dataset_id},
                        {"verb": "summarize", "of": "logs", "dataset_id": dataset_id}]
        if _is_denial(exc):
            # Reporting a refusal as an absence sent the reader to look for a run that
            # "did not get far enough", when the run may well have published a trace
            # this credential is not allowed to read.
            raise errors.Forbidden(
                "Not allowed to read this project's workflow trace.",
                what="The trace is stored in the project bucket and access was denied, "
                     "so whether one was published is unknown.",
                user_action="Ask a project administrator for data access, then retry.",
                retry_with=alternatives,
            )
        raise errors.NotFound(
            f"No workflow trace is available for this dataset: {exc}",
            what="A trace is only published by Nextflow runs that got far enough to emit one.",
            retry_with=alternatives,
        )
    records = frame.to_dict(orient="records") if hasattr(frame, "to_dict") else list(frame)
    return S.trace(records)


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
    task_rows = dataset_tasks(reader, project_id, dataset_id)
    try:
        wanted = int(task_id)
    except (TypeError, ValueError):
        raise errors.InvalidQuery(
            f"task_id must be the integer index from `summarize tasks`, not {task_id!r}.",
            what="Tasks are addressed by their position in the task summary.",
            field="task_id", given=task_id,
            retry_with=[{"verb": "summarize", "of": "tasks"}],
        )
    row = next((t for t in task_rows if t["task_id"] == wanted), None)
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


# Derived, not a second list: the two had to be edited together to add a summary.
SUMMARIZERS = tuple(_BUILDERS)


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


def _summary_envelope(reader: Reader, request: dict, of: str, body: dict,
                      complete: bool, scope_echo: dict, max_tokens: int,
                      next_calls: list, omitted: Optional[list] = None) -> dict:
    envelope = wrap(
        query={"verb": "summarize", "of": of, "scope": scope_echo,
               "dataset_id": request.get("dataset_id")},
        result={"summary_of": of, "scan_complete": complete, of: body,
                "next_calls": next_calls},
        cost=reader.cost.to_dict(), max_tokens=max_tokens)
    # Set before the ladder measures, so what the omission costs is inside the budget.
    if omitted:
        envelope["omitted"] = list(omitted)
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
    dropped = []
    fix = _retry_bigger(request, of, scope_echo, max_tokens)
    for key in shrinkable:
        if estimate_tokens(envelope) <= max_tokens:
            break
        if key in body:
            body.pop(key)
            envelope["budget"]["degraded"].append({"what": f"{of}.{key}", "to": "dropped"})
            dropped.append(key)
            # Declared inside the loop, so what the declaration costs is inside the
            # budget it explains -- as in envelope.shrink_to_fit.
            _declare_dropped(envelope, body, of, dropped, fix, max_tokens)
    envelope["budget"]["used_est"] = estimate_tokens(envelope)
    return envelope


def _declare_dropped(envelope: dict, body: dict, of: str, dropped: list, fix: dict,
                     max_tokens: int) -> None:
    """Name the sections the ladder popped, in `omitted` and in the body itself.

    `budget.degraded` records that a step happened, but the channel the skills tell an
    agent to read is `omitted`: a summary that lost its `error_blocks` has to say so
    there, with a call that gets them back, and the surviving body has to carry a
    marker naming what is gone. One entry, rewritten as the ladder goes.
    """
    body["sections_dropped"] = list(dropped)
    entry = next((o for o in envelope.get("omitted", [])
                  if o.get("code") == "SECTIONS_DROPPED"), None)
    if entry is None:
        entry = {"code": "SECTIONS_DROPPED", "fix": fix}
        envelope.setdefault("omitted", []).append(entry)
    entry["sections"] = [f"{of}.{key}" for key in dropped]
    entry["detail"] = (
        f"{len(dropped)} section(s) of this {of} summary were dropped whole to fit "
        f"max_tokens={max_tokens}: {', '.join(dropped)}. What remains still describes "
        f"the whole {of}; the dropped sections were its detail, not its counts.")


def _retry_bigger(request: dict, of: str, scope_echo: dict, max_tokens: int) -> dict:
    """The same read again with room for what the budget cost.

    Built from the request rather than the echoed query because `task_logs` is its own
    verb, addressed by dataset_id and task_id, and offering it back as `summarize logs`
    would not run.
    """
    verb = request.get("verb") or "summarize"
    fix = {"verb": verb, "scope": {"project_ids": list(scope_echo["project_ids"])},
           "max_tokens": max_tokens * 2}
    if verb == "summarize":
        fix["of"] = of
    for key in ("dataset_id", "task_id"):
        if scope_echo.get(key) is not None:
            fix[key] = scope_echo[key]
    return fix

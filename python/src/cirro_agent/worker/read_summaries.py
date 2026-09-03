"""Summaries of the things that would otherwise flood the context:
files, logs, tasks, samples, parameters, trace.
"""
from typing import Optional

from .. import errors, summarize as S
from ..envelope import DEFAULT_MAX_TOKENS, estimate_tokens, wrap
from .reads import (MANIFEST_LIST_LIMIT, Reader, dataset_detail, dataset_manifest,
                    dataset_tasks, is_terminal, portal_dataset, spec_shape)


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
    project_ids = reader.scope_project_ids(scope)
    project_id = project_ids[0]
    dataset_id = request.get("dataset_id") or scope.get("dataset_id")
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["summarize"])

    if of != "samples" and not dataset_id:
        raise errors.ScopeRequired(
            f"summarize {of} needs a dataset_id.",
            what="Name the dataset to summarize.", field="dataset_id",
            retry_with=[{"verb": "query", "entity": "dataset",
                         "scope": {"project_ids": project_ids}}],
        )
    scope_echo = {"project_ids": project_ids}
    if dataset_id:
        scope_echo["dataset_id"] = dataset_id
    build = _BUILDERS[of]
    body, complete, next_calls = build(reader, request, project_ids, project_id, dataset_id)
    return _summary_envelope(reader, request, of, body, complete, scope_echo,
                             max_tokens, next_calls=next_calls)


def _samples_body(reader, request, project_ids, project_id, dataset_id) -> tuple:
    sample_rows, complete = reader.samples(project_id)
    schema_keys = None
    try:
        form = reader.client.metadata.get_project_schema(project_id=project_id)
        reader.cost.call("metadata.schema")
        spec_json = getattr(form, "form_spec_json", None) or {}
        props = ((spec_json.get("properties")
                  if isinstance(spec_json, dict) else None) or {})
        schema_keys = list(props)
    except Exception:
        pass
    body = S.samples(sample_rows, schema_keys=schema_keys)
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


def _files_body(reader, request, project_ids, project_id, dataset_id) -> tuple:
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


def _logs_body(reader, request, project_ids, project_id, dataset_id) -> tuple:
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


def _tasks_body(reader, request, project_ids, project_id, dataset_id) -> tuple:
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


def _params_body(reader, request, project_ids, project_id, dataset_id) -> tuple:
    row = dataset_detail(reader, project_id, dataset_id)
    defaults, declared, required = spec_shape(reader, row.get("process_id"))
    body = S.params(row.get("params") or {}, defaults=defaults, declared=declared,
                    required=required)
    body["process"] = row.get("process")
    reader.cost.strategy = "detail+parameter_spec"
    return body, True, [{
        "id": "nc_spec",
        "why": "read the full parameter spec before changing params",
        "call": {"verb": "get", "entity": "process", "id": row.get("process_id"),
                 "include": ["custom_settings"]},
    }]


def _trace_summary(reader, request, project_ids, project_id, dataset_id) -> tuple:
    body = _trace_body(reader, project_id, dataset_id)
    reader.cost.strategy = "artifact_read"
    return body, True, []


# One builder per summary kind, all with the same signature, so do_summarize is the
# validation and the envelope and nothing else.
_BUILDERS = {"samples": _samples_body, "files": _files_body, "logs": _logs_body,
             "tasks": _tasks_body, "params": _params_body, "trace": _trace_summary}


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


def _trace_body(reader: Reader, project_id: str, dataset_id: str) -> dict:
    try:
        wrapped = portal_dataset(reader, project_id, dataset_id)
        frame = wrapped.get_trace()
        reader.cost.call("artifact.trace")
    except errors.AgentError:
        # An auth expiry or a missing dataset must keep its own diagnosis rather than
        # being reported as a missing trace artifact.
        raise
    except Exception as exc:
        raise errors.NotFound(
            f"No workflow trace is available for this dataset: {exc}",
            what="A trace is only published by Nextflow runs that got far enough to emit one.",
            retry_with=[{"verb": "summarize", "of": "tasks", "dataset_id": dataset_id},
                        {"verb": "summarize", "of": "logs", "dataset_id": dataset_id}],
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


def _summary_envelope(reader: Reader, request: dict, of: str, body: dict,
                      complete: bool, scope_echo: dict, max_tokens: int,
                      next_calls: list) -> dict:
    envelope = wrap(
        query={"verb": "summarize", "of": of, "scope": scope_echo,
               "dataset_id": request.get("dataset_id")},
        result={"summary_of": of, "scan_complete": complete, of: body,
                "next_calls": next_calls},
        cost=reader.cost.to_dict(), max_tokens=max_tokens)
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

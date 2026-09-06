"""Turn SDK/API objects into plain JSON-safe row dicts.

Everything downstream (filtering, faceting, aggregation, the envelope) operates on
these dicts and never on SDK objects. That keeps the query layer free of any cirro
import and testable without a tenant, and it is also where the API's `Unset`
sentinel is dealt with once instead of everywhere.
"""
import enum
from datetime import date, datetime, timezone
from typing import Any, Optional

from .. import errors
from ..query import STATUS_CLASSES as _STATUS_CLASSES, parse_dt
from ..summarize import duration_s, human_bytes, split_ext, task_process

# Single source of truth: `is_terminal` on a row and the `@terminal` filter class must
# never disagree about what counts as finished.
TERMINAL_STATUSES = frozenset(_STATUS_CLASSES["@terminal"])

# Cirro implements dataset folders as tags carrying this prefix. That is an
# implementation detail: everything above this module works in terms of `folders`, and
# `tags` never contains a folder entry.
FOLDER_TAG_PREFIX = "folder://"

# Where Cirro reads a repository-backed configuration from when the process says nothing.
DEFAULT_CONFIG_FOLDER = ".cirro"
DEFAULT_BRANCH = "main"

# How a dataset with no folder is described wherever folders are listed.
UNFILED = "(not in a folder)"


def split_folder_tags(values: list) -> tuple:
    """(folders, plain_tags) from a raw Cirro tag list."""
    folders, plain = [], []
    for value in values:
        text = str(value)
        if text.startswith(FOLDER_TAG_PREFIX):
            name = text[len(FOLDER_TAG_PREFIX):].strip("/")
            if name:
                folders.append(name)
        else:
            plain.append(text)
    return folders, plain


def folder_tag(name: str) -> str:
    return f"{FOLDER_TAG_PREFIX}{name.strip().strip('/')}"


def plain(value: Any) -> Any:
    """Unwrap Unset, enums, dates and datetimes into JSON-safe values."""
    from cirro_api_client.v1.types import Unset
    if isinstance(value, Unset) or value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # datetime is a subclass of date, so this must come second. The metrics series
    # are dated with a bare date, which otherwise reached the envelope as an object.
    if isinstance(value, date):
        return value.isoformat()
    # isinstance, not a name whitelist: RepositoryType, SharingType and ProjectRole
    # were falling through and serializing as "RepositoryType.GITHUB_PUBLIC".
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if is_freeform_map(value):
        return {k: plain(v) for k, v in value.additional_properties.items()}
    return value


def is_freeform_map(value: Any) -> bool:
    """Is this a generated model for a free-form map, rather than a real record?

    SampleMetadata, DatasetDetailParams and MetricRecordServices are attrs classes
    whose only field is `additional_properties` -- they model an open JSON object, so
    the keys ARE the data. They are neither dict nor Mapping, so they used to fall
    through `plain` untouched and reach code that treats them as mappings: `samples`
    and `params` both died on `.items()`, and spec_shape declared nothing.

    The single-field test matters. Every generated model carries
    `additional_properties`, so testing for that alone would also unwrap real records
    like Dataset (12 fields) into camelCased dicts and break every caller that reads
    an attribute off one.
    """
    fields = getattr(type(value), "__attrs_attrs__", None)
    return (fields is not None and len(fields) == 1
            and fields[0].name == "additional_properties"
            and isinstance(getattr(value, "additional_properties", None), dict))


def _age_days(created_at: Optional[str], now: datetime) -> Optional[int]:
    if not created_at:
        return None
    try:
        return (now - parse_dt(created_at)).days
    except errors.AgentError:
        return None


def _tag_values(obj) -> list:
    return [plain(getattr(t, "value", t))
            for t in (plain(getattr(obj, "tags", None)) or [])]


def dataset(obj, *, now: datetime, process_names: Optional[dict] = None) -> dict:
    """A Dataset or DatasetWithShare from a listing."""
    status = plain(getattr(obj, "status", None))
    created_at = plain(getattr(obj, "created_at", None))
    process_id = plain(getattr(obj, "process_id", None))
    share = getattr(obj, "share", None)
    folders, plain_tags = split_folder_tags(_tag_values(obj))
    row = {
        "id": plain(obj.id),
        "name": plain(obj.name),
        "project_id": plain(getattr(obj, "project_id", None)),
        "process_id": process_id,
        "process": {"id": process_id,
                    "name": (process_names or {}).get(process_id)} if process_id else None,
        "status": status,
        "folders": folders,
        "folder": folders[0] if len(folders) == 1 else None,
        "tags": plain_tags,
        "created_by": plain(getattr(obj, "created_by", None)),
        "created_at": created_at,
        "updated_at": plain(getattr(obj, "updated_at", None)),
        "description": plain(getattr(obj, "description", None)),
        "source_dataset_ids": plain(getattr(obj, "source_dataset_ids", None)) or [],
        "share": {"id": plain(share.id), "name": plain(share.name)} if share else None,
        "age_days": _age_days(created_at, now),
        "is_terminal": str(status).upper() in TERMINAL_STATUSES,
    }
    return row


def dataset_detail(detail, base: Optional[dict] = None, *, now: datetime,
                   process_names: Optional[dict] = None) -> dict:
    """Merge detail-tier fields onto a list-tier row."""
    row = dict(base or dataset(detail, now=now, process_names=process_names))
    size = plain(getattr(detail, "total_size_bytes", None))
    row.update({
        "total_size_bytes": size,
        "total_size_human": human_bytes(size) if size is not None else None,
        "file_count": plain(getattr(detail, "file_count", None)),
        "params": plain(getattr(detail, "params", None)) or {},
        "info": plain(getattr(detail, "info", None)) or {},
        "status_message": plain(getattr(detail, "status_message", None)),
        "source_sample_ids": plain(getattr(detail, "source_sample_ids", None)) or [],
        "s3": plain(getattr(detail, "s3", None)),
        "is_view_restricted": bool(plain(getattr(detail, "is_view_restricted", None))),
    })
    sources = plain(getattr(detail, "source_datasets", None)) or []
    if sources:
        row["source_datasets"] = [
            {"id": getattr(s, "id", None) or s.get("id"),
             "name": getattr(s, "name", None) or s.get("name")}
            for s in (getattr(detail, "source_datasets", None) or [])
        ]
    return row


def project(obj) -> dict:
    return {
        "id": plain(obj.id),
        "name": plain(obj.name),
        "description": plain(getattr(obj, "description", None)),
        "status": plain(getattr(obj, "status", None)),
        "tags": _tag_values(obj),
        "organization": plain(getattr(obj, "organization", None)),
        "classification_ids": plain(getattr(obj, "classification_ids", None)) or [],
        "billing_account_id": plain(getattr(obj, "billing_account_id", None)),
    }


def process(obj) -> dict:
    owner = plain(getattr(obj, "owner", None))
    return {
        "owner": owner,
        # A blank owner means Cirro curated it; anything else was user-registered.
        "is_custom": bool(owner),
        "is_tenant_wide": bool(plain(getattr(obj, "is_tenant_wide", None))),
        "created_at": plain(getattr(obj, "created_at", None)),
        "updated_at": plain(getattr(obj, "updated_at", None)),
        "id": plain(obj.id),
        "name": plain(obj.name),
        "description": plain(getattr(obj, "description", None)),
        "data_type": plain(getattr(obj, "data_type", None)),
        "executor": plain(getattr(obj, "executor", None)),
        "category": plain(getattr(obj, "category", None)),
        "pipeline_type": plain(getattr(obj, "pipeline_type", None)),
        "is_archived": bool(plain(getattr(obj, "is_archived", None))),
        "uses_sample_sheet": bool(plain(getattr(obj, "uses_sample_sheet", None))),
        "documentation_url": plain(getattr(obj, "documentation_url", None)),
        "child_process_ids": plain(getattr(obj, "child_process_ids", None)) or [],
        "parent_process_ids": plain(getattr(obj, "parent_process_ids", None)) or [],
        "file_requirements_message": plain(getattr(obj, "file_requirements_message", None)),
    }


def process_detail(detail, base: Optional[dict] = None) -> dict:
    """Merge the detail-tier config fields onto a list-tier process row.

    `config_source` is the field that says which of the three configuration patterns a
    process uses, and it is absent from the list payload -- so surveying it costs one
    call per process.
    """
    row = dict(base or process(detail))
    source = plain(getattr(detail, "config_source", None))
    settings = getattr(detail, "custom_settings", None)
    code = getattr(detail, "pipeline_code", None)

    row["config_source"] = source
    if settings is not None and plain(settings) is not None:
        row["custom_settings"] = {
            "repository": plain(getattr(settings, "repository", None)),
            "branch": plain(getattr(settings, "branch", None)),
            "folder": plain(getattr(settings, "folder", None)),
            "repository_type": plain(getattr(settings, "repository_type", None)),
            "sync_status": plain(getattr(settings, "sync_status", None)),
            "last_sync": plain(getattr(settings, "last_sync", None)),
            "commit_hash": plain(getattr(settings, "commit_hash", None)),
            "is_authorized": bool(plain(getattr(settings, "is_authorized", None))),
        }
    else:
        row["custom_settings"] = None

    if code is not None and plain(code) is not None:
        row["pipeline_code"] = {
            "repository_path": plain(getattr(code, "repository_path", None)),
            "version": plain(getattr(code, "version", None)),
            "repository_type": plain(getattr(code, "repository_type", None)),
            "entry_point": plain(getattr(code, "entry_point", None)),
            "executor_version": plain(getattr(code, "executor_version", None)),
        }
    else:
        row["pipeline_code"] = None

    # Only on the detail payload, and an update replaces the whole record, so without
    # it here every edit had to guess the flag and could flip it by accident.
    row["allow_multiple_sources"] = bool(
        plain(getattr(detail, "allow_multiple_sources", None)))

    row["config_location"] = _config_location(row)
    row["code_location"] = _code_location(row.get("pipeline_code"))
    rules = plain(getattr(detail, "file_mapping_rules", None)) or []
    row["file_mapping_rules"] = rules
    return row


def _config_location(row: dict) -> str:
    """Say in one line where a process's config actually comes from."""
    source = row.get("config_source")
    settings = row.get("custom_settings") or {}
    repo = settings.get("repository")
    if source == "STORAGE":
        return "Cirro storage (uploaded config revisions)"
    if source in ("WITH_PROCESS", "EXTERNAL") and repo:
        folder = settings.get("folder") or DEFAULT_CONFIG_FOLDER
        branch = settings.get("branch") or DEFAULT_BRANCH
        same = (row.get("pipeline_code") or {}).get("repository_path") == repo
        which = "same repo as the code" if same else "separate repo"
        return f"{repo}@{branch}:{folder} ({which})"
    if source == "EXTERNAL":
        return "derived from the workflow repo's own schema (no config repo set)"
    return f"{source or 'unknown'}"


def _code_location(code: Optional[dict]) -> Optional[str]:
    if not code or not code.get("repository_path"):
        return None
    entry = code.get("entry_point") or "?"
    return f"{code['repository_path']}@{code.get('version') or '?'}:{entry}"


def sample(obj) -> dict:
    return {
        "id": plain(obj.id),
        "name": plain(obj.name),
        "metadata": plain(getattr(obj, "metadata", None)) or {},
        "dataset_ids": plain(getattr(obj, "dataset_ids", None)) or [],
        "created_at": plain(getattr(obj, "created_at", None)),
        "updated_at": plain(getattr(obj, "updated_at", None)),
    }


def reference(obj) -> dict:
    files = plain(getattr(obj, "files", None)) or []
    return {
        "id": plain(getattr(obj, "id", None)),
        "name": plain(obj.name),
        "type": plain(getattr(obj, "type_", None) or getattr(obj, "type", None)),
        "description": plain(getattr(obj, "description", None)),
        "created_by": plain(getattr(obj, "created_by", None)),
        "created_at": plain(getattr(obj, "created_at", None)),
        "n_files": len(files),
    }


def file_entry(entry) -> dict:
    size = plain(getattr(entry, "size", None))
    path = plain(getattr(entry, "path", None) or getattr(entry, "relative_path", None))
    return {
        "path": path,
        "size": size,
        "size_human": human_bytes(size) if size is not None else None,
        "ext": split_ext(path or "") or "(none)",
        "metadata": plain(getattr(entry, "metadata", None)) or {},
    }


def task(obj, index: int) -> dict:
    name = plain(getattr(obj, "name", None))
    started = plain(getattr(obj, "started_at", None))
    stopped = plain(getattr(obj, "stopped_at", None))
    row = {
        "task_id": index,
        "name": name,
        "process": task_process(name or ""),
        # Task.status is a free-form string in the API, not the Status enum.
        "status": plain(getattr(obj, "status", None)),
        "native_job_id": plain(getattr(obj, "native_job_id", None)),
        "status_message": plain(getattr(obj, "status_message", None)),
        "exit_code": plain(getattr(obj, "exit_code", None)),
        "container_image": plain(getattr(obj, "container_image", None)),
        "work_dir": plain(getattr(obj, "work_dir", None)),
        "command_line": plain(getattr(obj, "command_line", None)),
        "requested_at": plain(getattr(obj, "requested_at", None)),
        "started_at": started,
        "stopped_at": stopped,
    }
    if started and stopped:
        row["duration_s"] = duration_s(row)
    return row


def cost_task(obj) -> dict:
    """One TaskCost. `process` is the step the task belongs to, as on `task`."""
    name = plain(getattr(obj, "name", None))
    return {"task_id": plain(getattr(obj, "task_id", None)), "name": name,
            "process": task_process(name or ""),
            "status": plain(getattr(obj, "status", None)),
            "cost": plain(getattr(obj, "cost", None))}


def dataset_cost(obj) -> dict:
    """CostResponse flattened. `is_estimate` says whether the figure is billed or modelled."""
    # Unset is falsy, so `or []` covers both an absent field and a null one.
    return {
        "total_cost": plain(getattr(obj, "total_cost", None)),
        "is_estimate": plain(getattr(obj, "is_estimate", None)),
        "groups": [{"name": plain(getattr(g, "name", None)),
                    "cost": plain(getattr(g, "cost", None))}
                   for g in getattr(obj, "groups", None) or []],
        "tasks": [cost_task(t) for t in getattr(obj, "tasks", None) or []],
    }


def share(obj) -> dict:
    return {"id": plain(getattr(obj, "id", None)), "name": plain(getattr(obj, "name", None)),
            "share_type": plain(getattr(obj, "share_type", None)),
            "description": plain(getattr(obj, "description", None))}


def workspace(obj) -> dict:
    return {"id": plain(getattr(obj, "id", None)), "name": plain(getattr(obj, "name", None)),
            "status": plain(getattr(obj, "status", None)),
            "environment_id": plain(getattr(obj, "environment_id", None)),
            "started_at": plain(getattr(obj, "started_at", None))}


def compute_environment(obj) -> dict:
    return {"id": plain(getattr(obj, "id", None)), "name": plain(getattr(obj, "name", None))}


def reference_type(obj) -> dict:
    return {"name": plain(getattr(obj, "name", None)),
            "description": plain(getattr(obj, "description", None)),
            "directory": plain(getattr(obj, "directory", None))}


def user(obj) -> dict:
    return {"username": plain(getattr(obj, "username", None)),
            "name": plain(getattr(obj, "name", None)),
            "email": plain(getattr(obj, "email", None)),
            "organization": plain(getattr(obj, "organization", None))}


def project_user(obj) -> dict:
    return {"username": plain(getattr(obj, "username", None)),
            "role": plain(getattr(obj, "role", None))}


def metric_record(obj) -> dict:
    """One dated point of a metrics series.

    `services` maps a service or storage-tier name to its number, and the unit is on
    the record -- so the day's or month's figure is the sum across that map, and
    nothing here assumes dollars or bytes. Reading a `value` attribute the model has
    never had is what made project storage report null for every project.
    """
    # `services` is a generated model whose keys live in additional_properties, not a
    # dict -- the same wrapper that made spec_shape declare nothing.
    raw = getattr(obj, "services", None)
    services = plain(raw.to_dict() if hasattr(raw, "to_dict") else raw) or {}
    numbers = {k: v for k, v in services.items() if isinstance(v, (int, float))}
    return {"date": plain(getattr(obj, "date", None)),
            "unit": plain(getattr(obj, "unit", None)),
            "total": round(sum(numbers.values()), 2),
            "by_service": {k: round(v, 2) for k, v in
                           sorted(numbers.items(), key=lambda kv: -kv[1])}}

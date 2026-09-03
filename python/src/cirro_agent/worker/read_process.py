"""Reading pipeline definitions: parameter specs, configuration
assets, workflow sources, and the validator.
"""
import json

from .. import errors
from ..envelope import DEFAULT_MAX_TOKENS, estimate_tokens, wrap
from . import rows as R
from .reads import Reader, near_misses, process_detail, spec_shape


def do_process_params(request: dict) -> dict:
    """The authoritative parameter shape for a process, summarized.

    Read this before constructing params for run_analysis: it is the only place the
    accepted keys, their defaults and which are required are stated.
    """
    reader = Reader(request)
    given = request.get("id") or request.get("name")
    if not given:
        raise errors.InvalidQuery(
            "process_params needs a process id or name.",
            what="Name the process.", field="id",
            retry_with=[{"verb": "query", "entity": "process"}],
        )
    match = reader.resolve_process(given)

    defaults, declared, required = spec_shape(reader, match["id"])
    parameters = [
        {"key": key, "required": key in (required or []),
         "default": defaults.get(key, None), "has_default": key in defaults}
        for key in (declared or [])
    ]
    process = {key: match[key] for key in (
        "id", "name", "executor", "uses_sample_sheet", "documentation_url",
        "file_requirements_message")}
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["query"])
    envelope = wrap(
        query={"verb": "process_params", "id": match["id"]},
        result={
                    "process": process,
                    "n_parameters": len(parameters),
                    "required": [p["key"] for p in parameters if p["required"]],
                    "parameters": parameters,
                    "note": ("Keys not listed here are accepted by the API and ignored by the "
                             "pipeline. plan(run_analysis) reports any you pass that are not "
                             "declared."),
                    "next_calls": [{
                        "id": "nc_plan", "why": "plan a run once the params are chosen",
                        "call": {"tool": "plan", "op": "run_analysis",
                                 "args": {"process": match["id"], "name": "OUTPUT_NAME",
                                          "dataset_id": "INPUT_DATASET_ID", "params": {}}}}],
                },
        cost=reader.cost.to_dict(),
        max_tokens=max_tokens)
    result = envelope["result"]
    while estimate_tokens(envelope) > max_tokens and len(result["parameters"]) > 5:
        keep = max(5, len(result["parameters"]) // 2)
        result["parameters"] = result["parameters"][:keep]
        result["parameters_truncated_to"] = keep
    envelope["budget"]["used_est"] = estimate_tokens(envelope)
    return envelope


def do_process_config(request: dict) -> dict:
    """Read a process's configuration assets, wherever they live.

    For STORAGE the bytes are in Cirro and come back over the revision API. For the
    repo-backed sources the bytes are in git, so report where and let the caller read
    the checkout -- this command never guesses at file contents it cannot see.
    """
    reader = Reader(request)
    given = request.get("id")
    if not given:
        raise errors.InvalidQuery(
            "process_config needs a process id.",
            what="Name the process.", field="id",
            retry_with=[{"verb": "query", "entity": "process"}],
        )

    found = reader.processes(include_archived=True)
    row = next((p for p in found if p["id"] == given), None)
    if row is None:
        near = near_misses(given, [p["name"] for p in found])
        raise errors.NotFound(
            f"No process {given!r}.", what="Check the process ID.", field="id",
            given=given, candidates=[{"name": n} for n in near],
            retry_with=[{"verb": "query", "entity": "process"}],
        )
    row = process_detail(reader, row["id"], row)
    source = row.get("config_source")

    result = {
        "process": {"id": row["id"], "name": row["name"], "executor": row["executor"],
                    "is_archived": row.get("is_archived"), "owner": row.get("owner")},
        "config_source": source,
        "config_location": row.get("config_location"),
        "code_location": row.get("code_location"),
        "pipeline_code": row.get("pipeline_code"),
    }

    if source == "STORAGE":
        result.update(_storage_config(reader, row["id"], request))
    elif source in ("WITH_PROCESS", "EXTERNAL"):
        result.update(_repo_config(row))
    else:
        result["note"] = (
            f"config_source is {source!r}. That is only meaningful for a process with "
            "workflow code; ingest data types carry no configuration.")

    return wrap(query={"verb": "process_config", "id": row["id"],
                       "slot": request.get("slot"), "raw": bool(request.get("raw"))},
                result=result, cost=reader.cost.to_dict(),
                max_tokens=int(request.get("max_tokens")
                               or DEFAULT_MAX_TOKENS["process_config"]))


def _storage_config(reader: Reader, process_id: str, request: dict) -> dict:
    from cirro_api_client.v1.api.processes import (get_latest_process_revision,
                                                   get_process_resource)
    from cirro_api_client.v1.models import ProcessResource

    out: dict = {}
    try:
        response = get_latest_process_revision.sync_detailed(
            process_id, client=reader.client._api_client)
        reader.cost.call("processes.latest_revision")
        revision = response.parsed
        out["revision"] = {
            "number": R.plain(getattr(revision, "revision_number", None)),
            "committed_at": R.plain(getattr(revision, "created_at", None)),
            "commit_message": R.plain(getattr(revision, "commit_message", None)),
        }
        # The token an update must pass as if_match is the revision number as a string,
        # not the per-slot content hash.
        out["update_token"] = str(out["revision"]["number"])
    except Exception as exc:
        out["revision"] = None
        out["note"] = (f"no configuration revisions exist yet ({type(exc).__name__}); "
                       "the process is registered but unconfigured")
        return out

    wanted = [request["slot"]] if request.get("slot") else list(CONFIG_SLOTS)
    raw = bool(request.get("raw"))
    assets = {}
    for slot in wanted:
        try:
            response = get_process_resource.sync_detailed(
                process_id, ProcessResource(slot), client=reader.client._api_client)
            reader.cost.call("processes.get_resource")
            content = R.plain(getattr(response.parsed, "content", None))
        except Exception:
            content = None
        if not content:
            continue
        assets[slot] = _describe_asset(slot, content, raw)
    out["assets"] = assets
    out["slots_present"] = sorted(assets)
    if not raw and assets:
        out["how_to_read_contents"] = (
            "add --raw (optionally with --slot <name>) to get the verbatim file text")
    return out


def _describe_asset(slot: str, content: str, raw: bool) -> dict:
    """Summarize an asset, or hand back its exact bytes when asked."""
    import hashlib
    entry = {
        "bytes": len(content),
        "lines": content.count("\n") + 1,
        "sha256": hashlib.sha256(content.encode()).hexdigest()[:16],
    }
    if slot in ("form", "input", "output", "metadata"):
        try:
            parsed = json.loads(content)
            entry["valid_json"] = True
            entry["shape"] = _json_shape(slot, parsed)
        except ValueError as exc:
            entry["valid_json"] = False
            entry["json_error"] = str(exc)
    if raw:
        entry["content"] = content
    return entry


def _json_shape(slot: str, parsed) -> dict:
    if slot == "form" and isinstance(parsed, dict):
        form = parsed.get("form") or {}
        groups = form.get("properties") or {}
        return {
            "groups": list(groups),
            "n_groups": len(groups),
            "n_fields": sum(len((g or {}).get("properties") or {})
                            for g in groups.values() if isinstance(g, dict)),
            "has_ui_block": bool(parsed.get("ui")),
        }
    if slot == "input" and isinstance(parsed, dict):
        return {"n_mappings": len(parsed), "keys": sorted(parsed)[:40]}
    if slot == "output" and isinstance(parsed, dict):
        return {"n_commands": len(parsed.get("commands") or [])}
    return {"top_level_keys": sorted(parsed)[:20] if isinstance(parsed, dict) else None}


def _repo_config(row: dict) -> dict:
    settings = row.get("custom_settings") or {}
    repo = settings.get("repository")
    branch = settings.get("branch") or R.DEFAULT_BRANCH
    folder = settings.get("folder") or R.DEFAULT_CONFIG_FOLDER
    out = {
        "repo": {"repository": repo, "branch": branch, "folder": folder,
                 "repository_type": settings.get("repository_type")},
        "sync": {"status": settings.get("sync_status"),
                 "last_sync": settings.get("last_sync"),
                 "commit_hash": settings.get("commit_hash"),
                 "is_authorized": settings.get("is_authorized")},
        "browse": (f"https://github.com/{repo}/tree/{branch}/{folder}"
                   if repo else None),
        "note": ("the configuration lives in git, not in Cirro -- read it from a "
                 "checkout of that repo, folder and branch"),
    }
    if settings.get("sync_status") == "FAILED":
        out["warning"] = ("the last sync from this repo FAILED, so the configuration "
                          "Cirro is running may be older than what the repo contains")
    if settings.get("is_authorized") is False:
        out["warning"] = ("Cirro is not authorized to read this repository, so it cannot "
                          "sync the configuration at all")
    return out


CONFIG_SLOTS = ("form", "input", "output", "compute", "preprocess", "documentation",
                "metadata")

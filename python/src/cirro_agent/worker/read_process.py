"""Reading pipeline definitions: parameter specs, configuration
assets, workflow sources, and the validator.
"""
import json
from typing import Optional

from .. import errors, formgen
from ..envelope import DEFAULT_MAX_TOKENS, estimate_tokens, shrink_to_fit, wrap
from . import rows as R
from .reads import (Reader, near_misses, process_assets, process_detail,
                    process_revision, spec_shape)


_PARAMS_NOTE = (
    "`path` is dotted because Cirro forms group their parameters: a path with a dot is "
    "submitted nested, so `analysis_type.wes` is {'analysis_type': {'wes': ...}} and "
    "never a top-level `wes`. Send each value at its declared `type` -- an `array` is a "
    "list, not a comma-separated string. A flat key validates cleanly and is then "
    "ignored by the pipeline, so it fails as defaults nobody chose rather than as an "
    "error. `conditional` fields only apply under one branch of a selector. Keys not "
    "listed here are accepted by the API and ignored; plan(run_analysis) reports any "
    "you pass that are not declared."
)

_SPEC_UNREADABLE = (
    "The parameter spec could not be read, so nothing here is declared and no "
    "parameter can be checked against it."
)


def _skeleton(fields: list) -> dict:
    """The smallest params object that satisfies the spec, nested as it must be sent.

    Required fields only, at their declared defaults; a required field with no default
    is left as a `<type>` placeholder rather than invented. This is the shape, not a
    recommendation -- every other parameter is optional and takes its own default.
    """
    required = {f["path"] for f in fields if f["required"]}
    skeleton: dict = {}
    for field in sorted(fields, key=lambda f: f["path"]):
        steps = field["path"].split(".")
        # A field required inside an optional group is required only once that group
        # is sent, so the smallest valid object leaves the whole group out.
        owners = [".".join(steps[:n]) for n in range(1, len(steps) + 1)]
        if not all(owner in required for owner in owners):
            continue
        node = skeleton
        for step in steps[:-1]:
            node = node.setdefault(step, {})
        if field["is_group"]:
            node.setdefault(steps[-1], {})
        elif field["has_default"]:
            node[steps[-1]] = field["default"]
        else:
            node[steps[-1]] = f"<{field['type'] or 'value'}>"
    return skeleton


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

    fields = spec_shape(reader, match["id"])
    parameters = [
        {"path": f["path"], "type": f["type"], "required": f["required"],
         "conditional": f["conditional"], "is_group": f["is_group"],
         "default": f["default"], "has_default": f["has_default"]}
        for f in (fields or [])
    ]
    required = [f["path"] for f in parameters if f["required"]]
    process = {key: match[key] for key in (
        "id", "name", "executor", "uses_sample_sheet", "documentation_url",
        "file_requirements_message")}
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["query"])
    envelope = wrap(
        query={"verb": "process_params", "id": match["id"]},
        result={
                    "process": process,
                    "n_parameters": len(parameters),
                    "required": required,
                    "parameters": parameters,
                    "skeleton": _skeleton(fields or []),
                    "note": _PARAMS_NOTE if fields is not None else _SPEC_UNREADABLE,
                    "next_calls": [{
                        "id": "nc_plan", "why": "plan a run once the params are chosen",
                        "call": {"tool": "plan", "op": "run_analysis",
                                 "args": {"process": match["id"], "name": "OUTPUT_NAME",
                                          "dataset_id": "INPUT_DATASET_ID", "params": {}}}}],
                },
        cost=reader.cost.to_dict(),
        max_tokens=max_tokens)
    shrink_to_fit(envelope, "parameters", floor=5, max_tokens=max_tokens,
                  fix={"verb": "process_params", "id": match["id"],
                       "max_tokens": max_tokens * 2})
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
    revision, failure = process_revision(reader, process_id)
    if failure:
        return {"revision": None,
                "note": ("Cirro would not say which configuration revision this "
                         f"process is serving ({failure['reason']}), so nothing here "
                         "describes its configuration -- which is not the same as it "
                         "having none"),
                "omitted": [{"what": "the configuration revision and its assets",
                             "why": failure["reason"],
                             "fix": {"verb": "process_config", "id": process_id}}]}
    if revision is None:
        return {"revision": None,
                "note": ("no configuration revisions exist yet; the process is "
                         "registered but unconfigured")}

    out = {"revision": {key: revision[key] for key in
                        ("number", "committed_at", "commit_message")},
           "update_token": revision["token"]}
    raw = bool(request.get("raw"))
    wanted = [request["slot"]] if request.get("slot") else None
    content, unreadable = process_assets(reader, process_id, wanted)
    out["assets"] = {slot: _describe_asset(slot, text, raw)
                     for slot, text in content.items()}
    out["slots_present"] = sorted(out["assets"])
    if unreadable:
        # slots_present would otherwise understate what the process holds.
        out["omitted"] = [{"what": f"the {slot} slot", "why": why["reason"],
                           "fix": {"verb": "process_config", "id": process_id,
                                   "slot": slot}}
                          for slot, why in sorted(unreadable.items())]
    if not raw and out["assets"]:
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
    if slot in formgen.JSON_SLOTS:
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
    repository_type = settings.get("repository_type")
    out = {
        "repo": {"repository": repo, "branch": branch, "folder": folder,
                 "repository_type": repository_type},
        "sync": {"status": settings.get("sync_status"),
                 "last_sync": settings.get("last_sync"),
                 "commit_hash": settings.get("commit_hash"),
                 "is_authorized": settings.get("is_authorized")},
        "browse": _browse_url(repo, branch, folder, repository_type),
        "note": ("the configuration lives in git, not in Cirro -- read it from a "
                 "checkout of that repo, folder and branch"),
    }
    if repo and out["browse"] is None:
        out["note"] += (f"; no browse link because repository_type {repository_type!r} "
                        "does not say which host serves this repository")
    # Both, not the later one: a repo can have failed its last sync AND have had its
    # authorization revoked, and reporting only the second hid the stale configuration.
    warnings = []
    if settings.get("sync_status") == "FAILED":
        warnings.append("the last sync from this repo FAILED, so the configuration "
                        "Cirro is running may be older than what the repo contains")
    if settings.get("is_authorized") is False:
        warnings.append("Cirro is not authorized to read this repository, so it cannot "
                        "sync the configuration at all")
    if warnings:
        out["warnings"] = warnings
    return out


def _browse_url(repo, branch: str, folder: str, repository_type) -> Optional[str]:
    """A link only where the repository type says which host to link to.

    RepositoryType is GITHUB_PUBLIC | GITHUB_PRIVATE | AWS | NONE | UNKNOWN. Only the
    GitHub pair names a host; a CodeCommit or self-hosted repo was being handed a
    github.com URL that points at someone else's repository, or at nothing.
    """
    if not repo or not str(repository_type or "").startswith("GITHUB"):
        return None
    return f"https://github.com/{repo}/tree/{branch}/{folder}"

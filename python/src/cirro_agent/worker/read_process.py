"""Reading pipeline definitions: parameter specs, configuration
assets, workflow sources, and the validator.
"""
import json
from .. import errors
from ..envelope import CONTRACT, DEFAULT_MAX_TOKENS, estimate_tokens
from . import rows as R
from .reads import CONFIG_SLOTS, Reader, _near_misses, _process_detail, _spec_shape


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
    found = reader.processes(include_archived=True)
    match = next((p for p in found if p["id"] == given), None)
    if match is None:
        named = [p for p in found if p["name"] == given]
        if len(named) > 1:
            raise errors.AmbiguousName(
                f"{len(named)} processes are named {given!r}.",
                what="Use the process ID.", field="id", given=given,
                candidates=[{"id": p["id"], "name": p["name"]} for p in named],
                retry_with=[{"verb": "process_params", "id": p["id"]} for p in named],
            )
        if not named:
            near = _near_misses(given, [p["name"] for p in found])
            raise errors.NotFound(
                f"No process matches {given!r}.", what="Pick an available process.",
                field="id", given=given, candidates=[{"name": n} for n in near],
                retry_with=[{"verb": "query", "entity": "process"}],
            )
        match = named[0]

    defaults, declared, required = _spec_shape(reader, match["id"])
    parameters = [
        {"key": key, "required": key in (required or []),
         "default": defaults.get(key, None), "has_default": key in defaults}
        for key in (declared or [])
    ]
    body = {
        "ok": True, "api": {"contract": CONTRACT},
        "query": {"verb": "process_params", "id": match["id"]},
        "result": {
            "process": {"id": match["id"], "name": match["name"],
                        "executor": match["executor"],
                        "uses_sample_sheet": match["uses_sample_sheet"],
                        "documentation_url": match["documentation_url"],
                        "file_requirements_message": match["file_requirements_message"]},
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
        "cost": reader.cost.to_dict(),
    }
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["query"])
    result = body["result"]
    while estimate_tokens(body) > max_tokens and len(result["parameters"]) > 5:
        keep = max(5, len(result["parameters"]) // 2)
        result["parameters"] = result["parameters"][:keep]
        result["parameters_truncated_to"] = keep
    body["budget"] = {"max_tokens": max_tokens, "used_est": estimate_tokens(body),
                      "degraded": []}
    return body


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
        near = _near_misses(given, [p["name"] for p in found])
        raise errors.NotFound(
            f"No process {given!r}.", what="Check the process ID.", field="id",
            given=given, candidates=[{"name": n} for n in near],
            retry_with=[{"verb": "query", "entity": "process"}],
        )
    row = _process_detail(reader, row["id"], row)
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

    envelope = {
        "ok": True, "api": {"contract": CONTRACT},
        "query": {"verb": "process_config", "id": row["id"],
                  "slot": request.get("slot"), "raw": bool(request.get("raw"))},
        "result": result,
        "cost": reader.cost.to_dict(),
    }
    max_tokens = int(request.get("max_tokens") or 8000)
    envelope["budget"] = {"max_tokens": max_tokens,
                          "used_est": estimate_tokens(envelope), "degraded": []}
    return envelope


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
    branch = settings.get("branch") or "main"
    folder = settings.get("folder") or ".cirro"
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


def do_workflow_spec(request: dict) -> dict:
    """Parse a workflow's declared inputs and parameters. Touches no Cirro API."""
    from pathlib import Path
    from . import workflow_spec as WS

    target = request.get("target")
    if not target:
        raise errors.InvalidQuery(
            "workflow_spec needs a target: a local checkout path or a repository.",
            what="Name the workflow to parse.", field="target",
            retry_with=[{"target": "epi2me-labs/wf-human-variation", "ref": "v2.8.1"},
                        {"target": "/path/to/local/checkout"}],
        )
    cache_root = Path(request.get("cache_root") or ".")
    source = WS.resolve_source(target, request.get("ref"), cache_root)
    root = Path(source["local_path"])
    engine = WS.detect_engine(root)
    parsed = WS.parse(root, engine)
    hints = WS.candidates(parsed["params"])

    wanted_group = request.get("group")
    params = parsed["params"]
    if wanted_group:
        params = [p for p in params if p["group"] == wanted_group]
    if not request.get("include_hidden"):
        shown = [p for p in params if not p["hidden"] and not p["is_infrastructure"]]
    else:
        shown = params

    counts = {
        "params_declared": len(parsed["params"]),
        "params_visible": sum(1 for p in parsed["params"]
                              if not p["hidden"] and not p["is_infrastructure"]),
        "params_hidden": sum(1 for p in parsed["params"] if p["hidden"]),
        "params_infrastructure": sum(1 for p in parsed["params"]
                                     if p["is_infrastructure"]),
        "params_returned": len(shown),
        "groups": len(parsed["groups"]),
        "must_set": sum(1 for p in parsed["params"]
                        if p["declared_required"] and not p["has_default"]),
    }

    result = {
        "engine": parsed["engine"],
        "source": source,
        "entrypoint": parsed["entrypoint"],
        "declared_engine_version": parsed["declared_engine_version"],
        "parse_confidence": parsed["parse_confidence"],
        "counts": counts,
        "groups": parsed["groups"],
        "candidates": hints,
        "params": shown,
        "samplesheet_columns": parsed["samplesheet_columns"],
        "outputs": parsed["outputs"],
        "test_profiles": parsed["test_profiles"],
        "flags": parsed["flags"],
        "note": ("candidates are hints with reasons, not decisions -- classify the "
                 "primary data input, managed parameters and file pickers yourself, "
                 "and leave executor_version unpinned unless a run fails to compile"),
    }
    envelope = {"ok": True, "api": {"contract": CONTRACT},
                "query": {"verb": "workflow_spec", "target": target,
                          "ref": request.get("ref"), "group": wanted_group},
                "result": result,
                "cost": {"api_calls": 0, "by_kind": {}, "note": "local parse only"}}

    max_tokens = int(request.get("max_tokens") or 12000)
    degraded = []
    # Enum lists dominate the payload for pipelines like this one (a 34-value basecaller
    # list); summarize them before dropping anything a caller cannot recover.
    if estimate_tokens(envelope) > max_tokens:
        for entry in result["params"]:
            values = entry.get("enum")
            if values and len(values) > 8:
                entry["enum"] = list(values[:8])
                entry["enum_truncated_to"] = 8
                entry["enum_total"] = len(values)
        degraded.append({"what": "params.enum", "to": "first 8 values"})
    if estimate_tokens(envelope) > max_tokens:
        for entry in result["params"]:
            entry.pop("help_text", None)
        degraded.append({"what": "params.help_text", "to": "dropped"})
    if estimate_tokens(envelope) > max_tokens:
        keep = max(10, len(result["params"]) // 2)
        result["params"] = result["params"][:keep]
        result["params_truncated_to"] = keep
        result["how_to_see_the_rest"] = "re-run with --group <key> to page by group"
        degraded.append({"what": "params", "to": str(keep)})
    envelope["budget"] = {"max_tokens": max_tokens,
                          "used_est": estimate_tokens(envelope), "degraded": degraded}
    return envelope


def do_check_config(request: dict) -> dict:
    """Validate a folder of configuration assets before it goes anywhere.

    Local only: no session, no auth, no network. Catches the failures that are
    expensive to discover later -- an unknown form keyword the portal rejects, a
    mapping to a form field that does not exist, and the `../` literal that Cirro's
    WAF blocks before the request reaches the app.
    """
    from pathlib import Path
    from ..envelope import CONTRACT, estimate_tokens
    from .. import formcheck, formgen

    target = request.get("path")
    if not target:
        raise errors.InvalidQuery(
            "check_config needs a path to a configuration folder.",
            what="Point at the folder holding the generated assets.", field="path",
            retry_with=[{"path": "./.cirro"}],
        )
    folder = Path(target).expanduser()
    if not folder.is_dir():
        raise errors.NotFound(
            f"{folder} is not a directory.",
            what="Point at the folder holding the assets.", field="path", given=target,
            retry_with=[{"path": str(folder.parent)}],
        )

    assets = {}
    for name in formgen.SLOTS:
        candidate = folder / name
        if candidate.is_file():
            assets[name] = candidate.read_text()
    extra = sorted(p.name for p in folder.iterdir()
                   if p.is_file() and p.name not in formgen.SLOTS)

    report = formcheck.check_assets(assets) if assets else {
        "ok": False, "n_errors": 1, "n_warnings": 0, "checked": [],
        "problems": [{"level": "error", "where": str(folder),
                      "message": "no Cirro configuration assets found here",
                      "fix": f"expected one or more of: {', '.join(formgen.SLOTS)}"}]}

    missing = [n for n in ("process-form.json", "process-input.json",
                           "process-output.json", "process-compute.config")
               if n not in assets]
    if missing:
        report["problems"].append({
            "level": "warning", "where": str(folder),
            "message": f"not present: {', '.join(missing)}",
            "fix": "a pipeline normally needs all four core assets"})
        report["n_warnings"] += 1

    if "preprocess.py" in assets and not (folder / "test_preprocess.py").is_file():
        report["problems"].append({
            "level": "error", "where": "preprocess.py",
            "message": ("a preprocess script must ship with an offline unit test; "
                        "there is no test_preprocess.py beside it"),
            "fix": "extract a pure make_manifest() and test it with unittest"})
        report["n_errors"] += 1
        report["ok"] = False

    body = {"ok": True, "api": {"contract": CONTRACT},
            "query": {"verb": "check_config", "path": str(folder)},
            "result": {"folder": str(folder), "assets": sorted(assets),
                       "other_files": extra, **report},
            "cost": {"api_calls": 0, "by_kind": {}, "note": "local validation only"}}
    body["budget"] = {"max_tokens": int(request.get("max_tokens") or 6000),
                      "used_est": estimate_tokens(body), "degraded": []}
    return body

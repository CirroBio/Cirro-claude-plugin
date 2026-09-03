"""Reads that need no session, no credential and no network.

describe answers from the static catalog, workflow_spec parses a checkout on disk,
check_config validates a configuration folder. They run in the parent process, so
they live outside worker/ -- the boundary that says only the child imports cirro.
"""
from pathlib import Path
from . import errors
from . import formcheck, schema
from .worker.read_query import DETAIL_BUDGET
from . import query as Q
from . import formgen
from .envelope import DEFAULT_MAX_TOKENS, estimate_tokens, wrap


def do_describe(request: dict) -> dict:
    entity = request.get("entity", "dataset")
    spec = schema.get(entity)
    fields = [
        {"field": name, "tier": tier, "means": doc}
        for name, (tier, doc) in spec.fields.items()
    ]
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["describe"])
    envelope = wrap(
        query={"verb": "describe", "entity": entity},
        result={
                    "entity": entity,
                    "id_prefix": spec.id_prefix,
                    "scoped_by_project": spec.scoped_by_project,
                    "date_field": spec.date_field,
                    "fields": fields,
                    "tiers": {
                        "list": "free -- already in the paged listing",
                        "derived": "free -- computed locally from list fields",
                        "probe": "one small API call per row",
                        "detail": "one full API call per row; gated above "
                                  f"{DETAIL_BUDGET} matched rows",
                        "manifest": "one or more calls per row, response can be enormous",
                        "execution": "as manifest, and only for some executors",
                    },
                    "projections": spec.projections,
                    "facets_computed": spec.facet_priority,
                    "text_fields_for_q": spec.text_fields,
                    "default_sort": spec.default_sort,
                    "operators": sorted(Q.OPS),
                    "status_classes": Q.STATUS_CLASSES,
                    "date_expressions": ["2026-08-01", "2026-08-01T00:00:00Z", "last-7d",
                                         "last-3m", "2026-Q2", "2026-08", "2026", "today",
                                         "yesterday"],
                    "person_expressions": ["@me", "a literal username"],
                    "size_expressions": ["10GB", "10GiB", "10000000000"],
                    "shorthand_example": ("status:FAILED created:last-7d owner:@me "
                                          "name~*RNAseq*"),
                    "group_by_bins": ["created_at:day|week|month|quarter|year",
                                      "total_size_bytes:log2", "status:class"],
                },
        cost={"api_calls": 0, "by_kind": {}, "note": "static catalog"},
        max_tokens=max_tokens)
    envelope["budget"]["used_est"] = estimate_tokens(envelope)
    return envelope


def do_workflow_spec(request: dict) -> dict:
    """Parse a workflow's declared inputs and parameters. Touches no Cirro API."""
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
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["workflow_spec"])
    envelope = wrap(
        query={"verb": "workflow_spec", "target": target,
               "ref": request.get("ref"), "group": wanted_group},
        result=result,
        cost={"api_calls": 0, "by_kind": {}, "note": "local parse only"},
        max_tokens=max_tokens)
    degraded = envelope["budget"]["degraded"]
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
    envelope["budget"]["used_est"] = estimate_tokens(envelope)
    return envelope


def do_check_config(request: dict) -> dict:
    """Validate a folder of configuration assets before it goes anywhere.

    Local only: no session, no auth, no network. Catches the failures that are
    expensive to discover later -- an unknown form keyword the portal rejects, a
    mapping to a form field that does not exist, and the `../` literal that Cirro's
    WAF blocks before the request reaches the app.
    """

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

    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["check_config"])
    envelope = wrap(
        query={"verb": "check_config", "path": str(folder)},
        result={"folder": str(folder), "assets": sorted(assets),
                "other_files": extra, **report},
        cost={"api_calls": 0, "by_kind": {}, "note": "local validation only"},
        max_tokens=max_tokens)
    envelope["budget"]["used_est"] = estimate_tokens(envelope)
    return envelope

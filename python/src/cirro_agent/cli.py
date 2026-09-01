"""`cirro-agent read ...` -- the ungated read surface.

Read-only by construction, not by convention: this module never imports the
mutations registry, and every request it can build carries a verb from READ_VERBS.
That is what makes a single broad `Bash(cirro-agent *)` permission safe to grant,
which in turn removes the pressure that otherwise produces an over-broad rule.
"""
import argparse
import json
import sys

from . import errors, runner

# The complete set of verbs this binary may ask the worker for.
READ_VERBS = frozenset({"query", "get", "summarize", "describe", "list",
                        "process_params", "process_config", "workflow_spec",
                        "task_logs", "whoami"})


def reads_config_slots() -> tuple:
    """Asset slot names, from the worker's own list so the two cannot drift."""
    from .worker.reads import CONFIG_SLOTS
    return CONFIG_SLOTS


def _emit(payload: dict) -> int:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if payload.get("ok") else 1


def _scope(args) -> dict:
    scope = {}
    if getattr(args, "project", None):
        scope["project"] = args.project
    if getattr(args, "all_projects", False):
        scope["projects"] = "*"
        scope["confirm_fanout"] = bool(getattr(args, "confirm_fanout", False))
    return scope


def _common(request: dict, args) -> dict:
    if getattr(args, "max_tokens", None):
        request["max_tokens"] = args.max_tokens
    if getattr(args, "fresh", False):
        request["freshness"] = "fresh"
    elif getattr(args, "stale_ok", False):
        request["freshness"] = "any"
    return request


def _run(request: dict, args) -> int:
    verb = request.get("verb")
    if verb not in READ_VERBS:
        # Unreachable via the parsers below; kept as a hard stop so a future
        # subcommand cannot quietly introduce a write path here.
        return _emit(errors.NotSupported(
            f"{verb!r} is not a read verb and this binary performs no writes.",
            what="Mutations go through the plan and apply MCP tools.",
            retry_with=[{"tool": "plan"}],
        ).to_dict())
    return _emit(runner.call(_common(request, args)))


def _doctor(args) -> int:
    from . import session as session_mod
    report = {"ok": True, "checks": []}

    def check(name, status, **detail):
        report["checks"].append({"check": name, "status": status, **detail})
        if status == "fail":
            report["ok"] = False

    try:
        found = session_mod.find_session_dir()
        check("session_folder", "ok", path=str(found),
              gitignored=(found / ".gitignore").is_file(),
              git_tracked=session_mod.is_git_tracked(found))
    except errors.AgentError as exc:
        check("session_folder", "fail", detail=exc.message, remedy=exc.user_action)
        return _emit(report)

    try:
        current = session_mod.load()
        check("session_config", "ok", base_url=current.base_url, auth=current.auth)
    except errors.AgentError as exc:
        check("session_config", "fail", detail=exc.message, remedy=exc.user_action)
        return _emit(report)

    try:
        check("runtime", "ok", **runner.check_ready(),
              interpreter=str(runner.interpreter()))
    except errors.AgentError as exc:
        check("runtime", "fail", detail=exc.message, remedy=exc.user_action)
        return _emit(report)

    identity = runner.call({"verb": "whoami"})
    if identity.get("ok") and identity.get("authenticated"):
        check("authentication", "ok", username=identity.get("username"),
              verified_by=identity.get("verified_by"))
        check("sdk", "ok", **identity.get("runtime", {}))
    else:
        check("authentication", "fail",
              detail=(identity.get("error") or {}).get("message", "not authenticated"),
              remedy="Run the cirro-session skill to log in.")
        check("sdk", "ok", **identity.get("runtime", {}))
    return _emit(report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cirro-agent",
        description="Read-only access to the Cirro data platform. Every response is "
                    "capped, faceted over the full match set, and carries executable "
                    "follow-up calls. Mutations are not available here -- they go "
                    "through the plan and apply MCP tools, which require approval.")
    top = parser.add_subparsers(dest="group", required=True)
    read = top.add_parser("read", help="read operations").add_subparsers(
        dest="command", required=True)

    def add(name, help_text, *, project=False, dataset=False, filters=False):
        sub = read.add_parser(name, help=help_text)
        sub.add_argument("--max-tokens", type=int,
                         help="response budget (default 6000 for listings)")
        sub.add_argument("--fresh", action="store_true", help="bypass the snapshot cache")
        sub.add_argument("--stale-ok", action="store_true",
                         help="serve an expired snapshot (reported in notes)")
        if project:
            sub.add_argument("-p", "--project", help="project name or ID")
            sub.add_argument("--all-projects", action="store_true",
                             help="scan every visible project")
            sub.add_argument("--confirm-fanout", action="store_true",
                             help="accept the per-project call cost of --all-projects")
        if dataset:
            sub.add_argument("-d", "--dataset-id", required=True, help="dataset ID")
        if filters:
            sub.add_argument(
                "-f", "--filter",
                help='shorthand or JSON, e.g. "status:FAILED created:last-7d"')
            sub.add_argument(
                "--sort", action="append",
                help="field to sort by; prefix with - for descending")
            sub.add_argument(
                "--include", action="append",
                help="opt into an expensive field (see describe)")
            sub.add_argument(
                "--confirm-detail-cost", action="store_true",
                help="accept one API call per matched row for --include (surveys)")
            sub.add_argument(
                "--group-by", action="append",
                help="aggregate instead of returning rows")
            sub.add_argument(
                "--metrics", action="append",
                help="count, or agg:field (sum/avg/min/max)")
            sub.add_argument("--order-by", help="group ordering, e.g. -count")
            sub.add_argument("--limit-groups", type=int, default=20)
        return sub

    add("projects", "list projects", filters=True)
    pr = add("project", "one project", project=False)
    pr.add_argument("id", help="name or ID")
    pr.add_argument("--include", action="append", help="expensive fields to fetch")
    add("datasets", "list datasets, faceted", project=True, filters=True)
    ds = add("dataset", "one dataset", project=True)
    ds.add_argument("id", help="dataset ID or name")
    ds.add_argument("--include", action="append", help="expensive fields to fetch")
    add("processes", "list pipelines and ingest types", filters=True).add_argument(
        "--include-archived", action="store_true")
    proc = add("process", "one process", project=False)
    proc.add_argument("id", help="name or ID")
    proc.add_argument("--include", action="append",
                      help="expensive fields, e.g. config_source custom_settings"
                           " pipeline_code")
    add("process-params", "the parameter spec for a process").add_argument(
        "id", help="process name or ID")
    pc = add("process-config", "a process's configuration assets and where they live")
    pc.add_argument("id", help="process ID")
    pc.add_argument("--slot", choices=list(reads_config_slots()),
                    help="limit to one asset slot")
    pc.add_argument("--raw", action="store_true",
                    help="include verbatim file contents")
    files = add("files", "summarize a dataset's file manifest", project=True, dataset=True)
    files.add_argument("--glob", help="restrict to matching paths")
    files.add_argument("--file-limit", type=int, default=20000)
    add("samples", "summarize project sample metadata", project=True)
    add("references", "list reference data in a project", project=True, filters=True)
    add("folders", "folders datasets are organized into, with counts",
        project=True)
    add("reference-types", "reference types this deployment accepts")
    add("compute-envs", "compute environments available to a project", project=True)
    add("shares", "data shares in a project", project=True)
    add("workspaces", "interactive workspaces in a project", project=True)
    add("users", "users in this tenant")
    add("project-users", "members of a project and their roles", project=True)
    add("analysis-status", "current status of a run, always re-fetched",
        project=True, dataset=True)
    logs = add("logs", "summarize the workflow log", project=True, dataset=True)
    logs.add_argument("--raw", action="store_true", help="needs --line-range")
    logs.add_argument("--line-range", nargs=2, type=int, metavar=("START", "END"))
    add("tasks", "summarize execution tasks, root cause first",
        project=True, dataset=True)
    task_logs = add("task-logs", "log of one task", project=True, dataset=True)
    task_logs.add_argument("--task-id", required=True)
    task_logs.add_argument("--raw", action="store_true")
    task_logs.add_argument("--line-range", nargs=2, type=int, metavar=("START", "END"))
    add("trace", "resource usage per process", project=True, dataset=True)
    add("params", "diff a run's params against the process defaults",
        project=True, dataset=True)
    ws = add("workflow-spec", "parse a Nextflow or WDL workflow's inputs and parameters")
    ws.add_argument("target", help="a local checkout path, or org/repo, or a git URL")
    ws.add_argument("--ref", help="tag, branch or commit to clone")
    ws.add_argument("--group", help="limit to one schema group")
    ws.add_argument("--include-hidden", action="store_true",
                    help="also return hidden and infrastructure parameters")
    add("describe", "field catalog, tiers and query grammar for an entity").add_argument(
        "entity", nargs="?", default="dataset")
    add("whoami", "identity, tenant and runtime")
    read.add_parser("doctor", help="diagnose session, runtime and auth")
    return parser


def _query_request(entity: str, args) -> dict:
    request = {"verb": "query", "entity": entity, "scope": _scope(args)}
    if getattr(args, "filter", None):
        raw = args.filter.strip()
        request["filter"] = json.loads(raw) if raw.startswith("{") else raw
    for source, target in (("sort", "sort"), ("include", "include"),
                           ("group_by", "group_by"), ("metrics", "metrics"),
                           ("order_by", "order_by"), ("limit_groups", "limit_groups"),
                           ("confirm_detail_cost", "confirm_detail_cost")):
        value = getattr(args, source, None)
        if value:
            request[target] = value
    if getattr(args, "include_archived", False):
        request["include_archived"] = True
    return request


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    command = getattr(args, "command", None)

    if command == "doctor":
        return _doctor(args)

    simple_query = {"projects": "project", "datasets": "dataset",
                    "processes": "process", "references": "reference"}
    if command in simple_query:
        return _run(_query_request(simple_query[command], args), args)

    if command in ("project", "process"):
        return _run({"verb": "get", "entity": command, "id": args.id,
                     "scope": _scope(args),
                     "include": getattr(args, "include", None) or []}, args)

    if command == "dataset":
        return _run({"verb": "get", "entity": "dataset", "id": args.id,
                     "scope": _scope(args), "include": args.include or []}, args)

    if command == "analysis-status":
        request = {"verb": "get", "entity": "dataset", "id": args.dataset_id,
                   "scope": _scope(args), "include": ["status_message"]}
        # A run's status is the one thing never worth serving from cache.
        args.fresh = True
        return _run(request, args)

    if command == "process-params":
        return _run({"verb": "process_params", "id": args.id}, args)

    if command == "process-config":
        request = {"verb": "process_config", "id": args.id}
        if args.slot:
            request["slot"] = args.slot
        if args.raw:
            request["raw"] = True
        return _run(request, args)

    summaries = {"files": "files", "samples": "samples", "logs": "logs",
                 "tasks": "tasks", "trace": "trace", "params": "params"}
    if command in summaries:
        request = {"verb": "summarize", "of": summaries[command], "scope": _scope(args)}
        if getattr(args, "dataset_id", None):
            request["dataset_id"] = args.dataset_id
        for extra in ("glob", "file_limit", "raw"):
            value = getattr(args, extra, None)
            if value:
                request[extra] = value
        if getattr(args, "line_range", None):
            request["line_range"] = args.line_range
        return _run(request, args)

    if command == "task-logs":
        request = {"verb": "task_logs", "scope": _scope(args),
                   "dataset_id": args.dataset_id, "task_id": args.task_id}
        if args.raw:
            request["raw"] = True
        if args.line_range:
            request["line_range"] = args.line_range
        return _run(request, args)

    listings = {"folders": "folders", "reference-types": "reference_types",
                "users": "users",
                "compute-envs": "compute_envs", "shares": "shares",
                "workspaces": "workspaces", "project-users": "project_users"}
    if command in listings:
        return _run({"verb": "list", "of": listings[command], "scope": _scope(args)}, args)

    if command == "workflow-spec":
        # A local parse: no session, auth or network needed for a checkout on disk.
        from .worker import reads
        request = _common({"verb": "workflow_spec", "target": args.target,
                           "ref": args.ref, "group": args.group,
                           "include_hidden": args.include_hidden,
                           "cache_root": str(runner.plugin_data())}, args)
        try:
            return _emit(reads.do_workflow_spec(request))
        except errors.AgentError as exc:
            return _emit(exc.to_dict())

    if command == "describe":
        # Static catalog: no session, auth or network needed, so answer directly.
        from .worker import reads
        request = _common({"verb": "describe", "entity": args.entity}, args)
        try:
            return _emit(reads.do_describe(request))
        except errors.AgentError as exc:
            return _emit(exc.to_dict())

    if command == "whoami":
        return _run({"verb": "whoami"}, args)

    return _emit(errors.NotSupported(
        f"Unhandled command {command!r}.", what="This is a bug in the CLI.",
        user_action="Report it.").to_dict())


if __name__ == "__main__":
    sys.exit(main())

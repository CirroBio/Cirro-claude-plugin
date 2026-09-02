"""Gated operations for configuring a workflow as a Cirro pipeline.

A configured pipeline is two things: a process record created through the API, and a
set of config assets. The record is always created the same way; only where the assets
live differs, and `config_source` names that choice. Verified against 102 custom
processes on a live tenant, with no counter-examples:

    STORAGE       assets uploaded into Cirro through the revision API
    WITH_PROCESS  assets in a folder of the workflow's own repository
    EXTERNAL      assets in a separate configuration repository

No pattern uses a `process-definition.json`; that file belongs to an older sync model.

Repo-backed patterns never push. This module writes files and, separately, will commit
them when asked -- pushing and opening a pull request stay the user's to do.
"""
import re
import subprocess
from pathlib import Path
from typing import Optional

from .. import errors, formcheck, formgen
from . import reads, rows as R
from . import mutations
from .mutations import (Draft, Op, RISK_DESTRUCTIVE, RISK_LOCAL, RISK_WRITE,
                        RISK_WRITE_LOCAL,
                        _inv, _need)

STORAGE = "STORAGE"
WITH_PROCESS = "WITH_PROCESS"
EXTERNAL = "EXTERNAL"
CONFIG_SOURCES = (STORAGE, WITH_PROCESS, EXTERNAL)

# Verified live: the API rejects other characters in a process name with HTTP 400.
NAME_CHARSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .&\-@!#'()/]*$")

# The server-side regex engine wants (?<name>...); Python's (?P<name>...) 400s.
PYTHON_NAMED_GROUP = re.compile(r"\(\?P<")

DEFAULT_CONFIG_FOLDER = ".cirro"
DEFAULT_BRANCH = "main"


# --------------------------------------------------------------------- helpers


def _load_spec(path: str) -> dict:
    """Read a design spec's YAML frontmatter."""
    import yaml
    source = Path(path).expanduser()
    if not source.is_file():
        raise errors.NotFound(
            f"No design spec at {source}.",
            what="Point at the design-spec.md written by the spec-workflow step.",
            field="design_spec", given=path,
            retry_with=[{"note": "run `cirro-agent read workflow-spec` and write a spec"}],
        )
    text = source.read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        raise errors.InvalidQuery(
            f"{source} has no YAML frontmatter.",
            what="A design spec is frontmatter plus prose.",
            field="design_spec", given=path,
            retry_with=[{"note": "the file must start with a --- delimited block"}],
        )
    try:
        spec = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise errors.InvalidQuery(
            f"The frontmatter in {source} is not valid YAML: {exc}",
            what="Fix the frontmatter.", field="design_spec", given=path,
            retry_with=[{"note": "check quoting around shell snippets in constraints"}],
        )
    for required in ("workflow", "cirro"):
        if required not in spec:
            raise errors.InvalidQuery(
                f"The design spec has no `{required}` block.",
                what="Both workflow and cirro are required.",
                field="design_spec", given=path,
                retry_with=[{"note": "see docs in the cirro-spec-workflow skill"}],
            )
    return spec


def _validated_assets(spec: dict, preprocess: Optional[str] = None) -> tuple:
    """Generate the assets and refuse to go further if they will not render."""
    assets = formgen.generate(spec, preprocess=preprocess)
    report = formcheck.check_assets(assets)
    if not report["ok"]:
        detail = "; ".join(f"{p['where']}: {p['message']}"
                           for p in report["problems"] if p["level"] == "error")
        raise errors.InvalidQuery(
            f"The generated configuration would not render: {detail}",
            what="Fix the design spec so the assets validate.",
            field="design_spec",
            retry_with=[{"note": "run `cirro-agent read check-config <dir>` after editing"}],
        )
    return assets, report


def _owned_process(reader: reads.Reader, process_id: str) -> dict:
    """Fetch a process and refuse to mutate one belonging to someone else.

    The manage grant is tenant-wide, so the credential can technically edit any custom
    pipeline. Ownership is not enforced by the platform, so it is enforced here.
    """
    found = reader.processes(include_archived=True)
    row = next((p for p in found if p["id"] == process_id), None)
    if row is None:
        near = reads._near_misses(process_id, [p["id"] for p in found])
        raise errors.NotFound(
            f"No process {process_id!r} on this tenant.",
            what="Check the process ID.", field="process_id", given=process_id,
            candidates=[{"id": n} for n in near],
            retry_with=[{"verb": "query", "entity": "process"}],
        )
    owner = row.get("owner")
    me = reader.username
    if not owner:
        raise errors.Forbidden(
            f"{process_id} is a Cirro-curated process, not a custom one.",
            what="Curated pipelines are maintained by Cirro, not by users.",
            user_action="Register your own custom process instead of editing this one.",
        )
    if me and owner != me:
        raise errors.Forbidden(
            f"{process_id} belongs to {owner}, not to you ({me}).",
            what="This plugin only modifies pipelines you own.",
            user_action=(f"Ask {owner} to make the change, or register your own process."),
        )
    return reads._process_detail(reader, process_id, row)


def _check_name(name: str) -> None:
    if not NAME_CHARSET.match(name or ""):
        raise errors.InvalidQuery(
            f"{name!r} is not an acceptable process name.",
            what=("Cirro requires a name starting with a letter or digit, containing "
                  "only letters, digits, spaces and . & - @ ! # ' ( ) / -- other "
                  "characters are rejected with an HTTP 400."),
            field="name", given=name,
            retry_with=[{"name": re.sub(r"[^A-Za-z0-9 .&\-@!#'()/]", " ",
                                        name or "").strip()}],
        )


def _repository_slug(url_or_slug: str) -> str:
    text = (url_or_slug or "").strip().rstrip("/")
    if text.startswith(("http://", "https://", "git@")):
        return re.sub(r"^.*[:/]([^/]+/[^/]+?)(\.git)?$", r"\1", text)
    return text


def _file_mapping_rules(spec: dict) -> list:
    """Only ingest-style processes describe the files they accept."""
    rules = (spec.get("cirro") or {}).get("file_mapping_rules") or []
    for rule in rules:
        for pattern in rule.get("file_name_patterns") or []:
            expression = pattern.get("sample_matching_pattern") or ""
            if PYTHON_NAMED_GROUP.search(expression):
                raise errors.InvalidQuery(
                    f"{expression!r} uses Python's (?P<name>...) named group.",
                    what=("The API's regex engine wants (?<name>...) and rejects the "
                          "Python spelling with an HTTP 400."),
                    field="file_mapping_rules",
                    retry_with=[{"sample_matching_pattern":
                                 PYTHON_NAMED_GROUP.sub("(?<", expression)}],
                )
    return rules


# ------------------------------------------------------- register a process


def plan_create_process(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "create_process", "design_spec", "config_source")
    spec = _load_spec(args["design_spec"])
    workflow = spec["workflow"]
    cirro = spec.get("cirro") or {}

    source = str(args["config_source"]).upper()
    if source not in CONFIG_SOURCES:
        raise errors.InvalidQuery(
            f"{args['config_source']!r} is not a configuration source.",
            what=("STORAGE keeps the config in Cirro; WITH_PROCESS keeps it in the "
                  "workflow's own repository; EXTERNAL keeps it in a separate one."),
            field="config_source", given=args["config_source"],
            retry_with=[{"config_source": value} for value in CONFIG_SOURCES],
        )

    process_id = args.get("process_id") or cirro.get("process_id")
    name = args.get("name") or workflow.get("name")
    _need({"process_id": process_id, "name": name}, "create_process",
          "process_id", "name")
    _check_name(name)

    existing = next((p for p in reader.processes(include_archived=True)
                     if p["id"] == process_id), None)
    if existing:
        raise errors.InvalidQuery(
            f"A process {process_id!r} already exists (owner {existing.get('owner')}).",
            what="Process IDs are unique; update the existing one or choose another id.",
            field="process_id", given=process_id,
            retry_with=[{"op": "update_process", "args": {"process_id": process_id}},
                        {"op": "create_process", "args": {"process_id":
                                                          f"{process_id}-2"}}],
        )

    code_repo = _repository_slug(workflow.get("repository", ""))
    settings = None
    if source in (WITH_PROCESS, EXTERNAL):
        repo = _repository_slug(args.get("config_repository")
                                or (code_repo if source == WITH_PROCESS else ""))
        if not repo:
            raise errors.InvalidQuery(
                "EXTERNAL needs config_repository: the repository holding the assets.",
                what="A separate configuration repository must be named.",
                field="config_repository",
                retry_with=[{"config_repository": "OrgName/workflow-configs"},
                            {"config_source": WITH_PROCESS,
                             "note": "if the config lives in the workflow's own repo"}],
            )
        if source == WITH_PROCESS and repo != code_repo:
            raise errors.InvalidQuery(
                f"WITH_PROCESS means the config lives in the workflow's own repository, "
                f"but {repo!r} is not {code_repo!r}.",
                what="Use EXTERNAL for a separate configuration repository.",
                field="config_repository", given=repo,
                retry_with=[{"config_source": EXTERNAL, "config_repository": repo}],
            )
        settings = {
            "repository": repo,
            "branch": args.get("config_branch") or DEFAULT_BRANCH,
            "folder": args.get("config_folder") or DEFAULT_CONFIG_FOLDER,
        }

    assets, report = _validated_assets(spec)
    executor = str(workflow.get("executor", "NEXTFLOW")).upper()
    if executor not in ("NEXTFLOW", "CROMWELL"):
        raise errors.NotSupported(
            f"Cirro runs Nextflow and WDL/Cromwell; the spec says {executor!r}.",
            what="Only those two executors can be registered here.",
            user_action="Check workflow.executor in the design spec.",
        )

    resolved = {
        "process_id": process_id,
        "name": name,
        "description": args.get("description") or spec.get("description")
        or f"{workflow.get('name')} {workflow.get('version', '')}".strip(),
        "executor": executor,
        "category": args.get("category") or cirro.get("category") or "Community",
        "config_source": source,
        "custom_settings": settings,
        "repository_path": code_repo,
        "version": str(workflow.get("version")),
        "entry_point": workflow.get("entrypoint") or "main.nf",
        "executor_version": workflow.get("executor_version"),
        "repository_type": args.get("repository_type") or "GITHUB_PUBLIC",
        "parent_process_ids": cirro.get("parent_process_ids") or [],
        "child_process_ids": cirro.get("child_process_ids") or [],
        "uses_sample_sheet": bool(args.get("uses_sample_sheet")),
        "allow_multiple_sources": bool(args.get("allow_multiple_sources", True)),
        "file_mapping_rules": _file_mapping_rules(spec),
        "design_spec": str(Path(args["design_spec"]).expanduser().resolve()),
    }

    where = {
        STORAGE: "uploaded into Cirro storage (a separate upload step after this)",
        WITH_PROCESS: (f"read from {settings['repository']}@{settings['branch']} in "
                       f"{settings['folder']}/ (the workflow's own repository)"
                       if settings else ""),
        EXTERNAL: (f"read from {settings['repository']}@{settings['branch']} in "
                   f"{settings['folder']}/ (a separate configuration repository)"
                   if settings else ""),
    }[source]

    lines = [
        f"Register a new Cirro pipeline on {reader.base_url}.",
        "",
        f"  Process id      {process_id}",
        f"  Name            {name}",
        f"  Category        {resolved['category']}",
        f"  Workflow code   {code_repo}@{resolved['version']}:{resolved['entry_point']}"
        f" [{executor}]",
        f"  Engine version  {resolved['executor_version'] or 'Cirro default'}",
        f"  Configuration   {source} — {where}",
        "",
        f"  Form            {len(assets)} assets generated, validated clean"
        + (f" ({report['n_warnings']} warnings)" if report["n_warnings"] else ""),
    ]
    warnings = [p["message"] for p in report["problems"] if p["level"] == "warning"]
    if resolved["executor_version"]:
        warnings.append(
            "executor_version is pinned. Cirro's default engine is right for most "
            "pipelines; pin only to recover from a compilation failure.")
    if source == STORAGE:
        lines += ["", "Registering does not upload the configuration. Follow this with "
                      "upload_process_config."]
    else:
        lines += ["", "Registering does not fetch the configuration. Write the assets "
                      "into the repository, commit and push them, then run "
                      "sync_process_config."]

    return Draft(args=resolved, summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("processes")])


def apply_create_process(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import (ConfigSource, CustomPipelineSettings,
                                            CustomProcessInput, Executor, FileMappingRule,
                                            FileNamePattern, PipelineCode, RepositoryType)
    settings = args.get("custom_settings")
    request = CustomProcessInput(
        id=args["process_id"], name=args["name"], description=args["description"],
        executor=Executor(args["executor"]),
        child_process_ids=args["child_process_ids"],
        parent_process_ids=args["parent_process_ids"],
        linked_project_ids=[],
        category=args["category"],
        pipeline_code=PipelineCode(
            repository_path=args["repository_path"], version=args["version"],
            repository_type=RepositoryType(args["repository_type"]),
            entry_point=args["entry_point"],
            executor_version=args.get("executor_version")),
        uses_sample_sheet=args["uses_sample_sheet"],
        allow_multiple_sources=args["allow_multiple_sources"],
        config_source=ConfigSource(args["config_source"]),
    )
    if settings:
        request.custom_settings = CustomPipelineSettings(
            repository=settings["repository"], branch=settings["branch"],
            folder=settings["folder"])
    rules = args.get("file_mapping_rules") or []
    if rules:
        request.file_mapping_rules = [
            FileMappingRule(
                description=rule["description"],
                file_name_patterns=[FileNamePattern(
                    example_name=p["example_name"], description=p["description"],
                    sample_matching_pattern=p["sample_matching_pattern"])
                    for p in rule.get("file_name_patterns") or []],
                min_=rule.get("min", 1), max_=rule.get("max", 5000),
                is_sample=rule.get("is_sample", False))
            for rule in rules]

    response = reader.client.processes.create_custom_process(process=request)
    created = R.plain(getattr(response, "id", None)) or args["process_id"]
    follow = ({"tool": "plan", "op": "upload_process_config",
               "args": {"process_id": created, "design_spec": args["design_spec"]}}
              if args["config_source"] == STORAGE else
              {"tool": "plan", "op": "sync_process_config",
               "args": {"process_id": created}})
    return {"created_process_id": created, "config_source": args["config_source"],
            "next": follow}


# ------------------------------------------------------- upload / sync config


STAGING_DIR = "pipeline-config"


def _staging_dir(args: dict) -> Path:
    """Where a copy of the uploaded configuration is kept for the user to read.

    Uploading into Cirro storage otherwise leaves nothing on disk: the assets are
    generated, sent and forgotten, and the only way to see what a pipeline is running
    is to ask Cirro for it back. The copy goes beside the design spec that produced it,
    next to the test plan -- not into the session folder, which is hidden, gitignored
    and full of credentials.
    """
    beside_spec = Path(args["design_spec"]).expanduser().resolve().parent
    override = args.get("stage_dir")
    if not override:
        return beside_spec / STAGING_DIR
    chosen = Path(override).expanduser()
    # The worker runs with cwd set to the plugin installation, not the user's
    # directory, so a bare relative path would land inside the plugin. Anchor it to
    # the spec instead, which is the only user-chosen location this op knows.
    return chosen.resolve() if chosen.is_absolute() else (beside_spec / chosen).resolve()


def _stage(directory: Path, assets: dict) -> list:
    """Write the generated assets into a directory, returning the names written."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in sorted(assets.items()):
        (directory / name).write_text(text, encoding="utf-8")
    return sorted(assets)


def plan_upload_process_config(reader: reads.Reader, args: dict) -> Draft:
    """STORAGE only: push the assets into Cirro as a new config revision."""
    _need(args, "upload_process_config", "process_id", "design_spec")
    detail = _owned_process(reader, args["process_id"])
    if detail.get("config_source") != STORAGE:
        raise errors.InvalidQuery(
            f"{args['process_id']} reads its configuration from "
            f"{detail.get('config_source')}, not from Cirro storage.",
            what="Only a STORAGE process accepts uploaded revisions.",
            field="process_id",
            retry_with=[{"op": "sync_process_config",
                         "args": {"process_id": args["process_id"]}}],
        )

    spec = _load_spec(args["design_spec"])
    assets, report = _validated_assets(spec, preprocess=_read_optional(
        args.get("preprocess_path")))

    token, current = None, {}
    try:
        from cirro_api_client.v1.api.processes import get_latest_process_revision
        response = get_latest_process_revision.sync_detailed(
            args["process_id"], client=reader.client._api_client)
        reader.cost.call("processes.latest_revision")
        number = R.plain(getattr(response.parsed, "revision_number", None))
        # The update token is the revision number as a string, not the per-slot ETag.
        token = str(number) if number is not None else None
        current = _current_assets(reader, args["process_id"])
    except Exception:
        token = None

    staging = _staging_dir(args)
    replaced = sorted(name for name in assets if (staging / name).exists()
                      and (staging / name).read_text() != assets[name])
    changed, added = [], []
    for name, text in sorted(assets.items()):
        slot = formgen.SLOTS[name]
        if slot not in current:
            added.append(name)
        elif current[slot].strip() != text.strip():
            changed.append(name)

    lines = [
        f"Upload configuration to {detail['name']} ({args['process_id']}) on "
        f"{reader.base_url}.",
        "",
        f"  Current revision  {token or 'none — this would be the first'}",
        f"  Assets            {len(assets)}: {', '.join(sorted(assets))}",
        f"  New               {', '.join(added) or 'none'}",
        f"  Changed           {', '.join(changed) or 'none'}",
        f"  Staged to         {staging}",
        f"  Replaced locally  {', '.join(replaced) or 'none'}",
        "",
        "All slots go up in one revision, so the configuration is never half-applied.",
        "A copy is kept in the folder above so you can read it later without asking "
        "Cirro for it back.",
    ]
    warnings = [p["message"] for p in report["problems"] if p["level"] == "warning"]
    if replaced:
        warnings.append(f"{len(replaced)} file(s) in {staging} will be overwritten: "
                        f"{', '.join(replaced)}")
    if token and not changed and not added:
        warnings.append("Nothing differs from the current revision; this would be a "
                        "no-op revision.")
    return Draft(
        args={"process_id": args["process_id"], "assets": assets, "if_match": token,
              "stage_dir": str(staging),
              "commit_message": args.get("commit_message")
              or f"Configuration from {Path(args['design_spec']).name}"},
        summary="\n".join(lines), warnings=warnings,
        invalidates=[_inv("processes")])


def _read_optional(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    source = Path(path).expanduser()
    if not source.is_file():
        raise errors.NotFound(
            f"No preprocess script at {source}.",
            what="Point at the preprocess.py to upload, or omit it.",
            field="preprocess_path", given=path,
            retry_with=[{"preprocess_path": None}],
        )
    return source.read_text()


def _current_assets(reader: reads.Reader, process_id: str) -> dict:
    from cirro_api_client.v1.api.processes import get_process_resource
    from cirro_api_client.v1.models import ProcessResource
    out = {}
    for slot in set(formgen.SLOTS.values()):
        try:
            response = get_process_resource.sync_detailed(
                process_id, ProcessResource(slot), client=reader.client._api_client)
            reader.cost.call("processes.get_resource")
            content = R.plain(getattr(response.parsed, "content", None))
            if content:
                out[slot] = content
        except Exception:
            continue
    return out


def apply_upload_process_config(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.api.processes import save_process_revision
    from cirro_api_client.v1.models import (ProcessResource, ProcessRevisionSaveFile,
                                            ProcessRevisionSaveRequest)
    from cirro_api_client.v1.types import UNSET

    files = [ProcessRevisionSaveFile(type_=ProcessResource(formgen.SLOTS[name]),
                                     content=text)
             for name, text in sorted(args["assets"].items())]
    body = ProcessRevisionSaveRequest(files=files,
                                      commit_message=args["commit_message"])
    response = save_process_revision.sync_detailed(
        args["process_id"], client=reader.client._api_client, body=body,
        if_match=args.get("if_match") or UNSET)
    if response.status_code >= 400:
        raise errors.UpstreamError(
            f"Cirro rejected the configuration upload with HTTP {response.status_code}.",
            what=("A 412 means the revision moved since the plan was made; a 403 means "
                  "the credential lacks manage_custom_pipelines, or the body tripped "
                  "the WAF."),
            retry_with=[{"op": "upload_process_config", "note": "re-plan to pick up the "
                                                                "current revision"}],
            status_code=response.status_code,
        )
    token = R.plain(getattr(response.parsed, "revision_token", None))
    result = {"process_id": args["process_id"], "revision_token": token,
              "slots": sorted(formgen.SLOTS[n] for n in args["assets"]),
              "follow_with": {"verb": "process_config", "id": args["process_id"]}}
    # Staged only after Cirro accepts, so the folder records what is live rather than
    # what was attempted -- and never by raising: the revision is already committed
    # upstream, so a local copy that cannot be written must not turn a successful
    # upload into a reported failure the caller would retry.
    stage_dir = args.get("stage_dir")
    try:
        result["staged"] = _stage(Path(stage_dir), args["assets"]) if stage_dir else []
        result["staged_to"] = stage_dir
    except OSError as exc:
        result["staged"] = []
        result["omitted"] = [{
            "what": "the local copy of the uploaded configuration",
            "why": f"{stage_dir}: {exc}",
            "note": "the revision itself was accepted and is live",
            "fix": {"verb": "process_config", "id": args["process_id"]}}]
    return result


def plan_sync_process_config(reader: reads.Reader, args: dict) -> Draft:
    """Repo-backed patterns: tell Cirro to re-read the configuration from git."""
    _need(args, "sync_process_config", "process_id")
    detail = _owned_process(reader, args["process_id"])
    source = detail.get("config_source")
    if source == STORAGE:
        raise errors.InvalidQuery(
            f"{args['process_id']} keeps its configuration in Cirro storage, so there "
            "is no repository to sync from.",
            what="Upload a new revision instead.", field="process_id",
            retry_with=[{"op": "upload_process_config",
                         "args": {"process_id": args["process_id"]}}],
        )
    settings = detail.get("custom_settings") or {}
    if not settings.get("repository"):
        raise errors.InvalidQuery(
            f"{args['process_id']} is {source} but names no configuration repository.",
            what="The process record is incomplete.",
            retry_with=[{"op": "update_process",
                         "args": {"process_id": args["process_id"],
                                  "config_repository": "Org/repo"}}],
        )

    lines = [
        f"Sync configuration for {detail['name']} ({args['process_id']}) from git.",
        "",
        f"  Repository    {settings['repository']}@{settings.get('branch') or 'main'}",
        f"  Folder        {settings.get('folder') or DEFAULT_CONFIG_FOLDER}/",
        f"  Last sync     {settings.get('last_sync') or 'never'}"
        f" ({settings.get('sync_status') or 'unknown'})",
        f"  Last commit   {settings.get('commit_hash') or 'none'}",
        "",
        "Cirro reads whatever is on that branch now, so push your changes first.",
    ]
    warnings = []
    if settings.get("is_authorized") is False:
        warnings.append("Cirro is not authorized to read this repository; the sync will "
                        "fail until the Cirro GitHub app is granted access.")
    if settings.get("sync_status") == "FAILED":
        warnings.append("The last sync failed, so the running configuration may be "
                        "older than what the repository contains.")
    return Draft(args={"process_id": args["process_id"]}, summary="\n".join(lines),
                 warnings=warnings, invalidates=[_inv("processes")])


def apply_sync_process_config(reader: reads.Reader, args: dict) -> dict:
    settings = reader.client.processes.sync_custom_process(
        process_id=args["process_id"])
    status = R.plain(getattr(settings, "sync_status", None))
    result = {
        "process_id": args["process_id"],
        "sync_status": status,
        "commit_hash": R.plain(getattr(settings, "commit_hash", None)),
        "last_sync": R.plain(getattr(settings, "last_sync", None)),
    }
    if status == "FAILED":
        result["note"] = ("Cirro could not read the configuration from the repository. "
                          "Check the branch, the folder path, and that the Cirro GitHub "
                          "app can read the repo.")
    return result


# ------------------------------------------------------- update / archive


def plan_update_process(reader: reads.Reader, args: dict) -> Draft:
    """Change the process record: version, category, wiring, or where config lives."""
    _need(args, "update_process", "process_id")
    detail = _owned_process(reader, args["process_id"])
    code = detail.get("pipeline_code") or {}
    settings = detail.get("custom_settings") or {}

    changes = {}
    for key, current in (("name", detail.get("name")),
                         ("description", detail.get("description")),
                         ("category", detail.get("category"))):
        if args.get(key) is not None and args[key] != current:
            changes[key] = args[key]
    if changes.get("name"):
        _check_name(changes["name"])
    for key, current in (("version", code.get("version")),
                         ("entry_point", code.get("entry_point")),
                         ("executor_version", code.get("executor_version"))):
        if key in args and args[key] != current:
            changes[key] = args[key]

    config_repo = args.get("config_repository")
    if config_repo and _repository_slug(config_repo) != settings.get("repository"):
        changes["config_repository"] = _repository_slug(config_repo)
    for key, current in (("config_branch", settings.get("branch")),
                         ("config_folder", settings.get("folder"))):
        if args.get(key) is not None and args[key] != current:
            changes[key] = args[key]

    if not changes:
        raise errors.InvalidQuery(
            "Nothing would change.",
            what="Supply a new value for at least one field.", given=args,
            retry_with=[{"op": "update_process",
                         "args": {"process_id": args["process_id"], "version": "v2.0.0"}}],
        )

    lines = [f"Update the pipeline record for {detail['name']} ({args['process_id']}).",
             ""]
    for key, value in sorted(changes.items()):
        was = {"version": code.get("version"), "entry_point": code.get("entry_point"),
               "executor_version": code.get("executor_version"),
               "config_repository": settings.get("repository"),
               "config_branch": settings.get("branch"),
               "config_folder": settings.get("folder")}.get(key, detail.get(key))
        lines += [f"  {key}", f"    from  {was!r}", f"    to    {value!r}"]

    warnings = []
    if "version" in changes:
        warnings.append("Changing the version changes which workflow code runs; the "
                        "configuration is not re-validated against it automatically.")
    if changes.get("executor_version"):
        warnings.append("Pinning the engine version should be a recovery from a "
                        "compilation failure, not a default.")

    resolved = {"process_id": args["process_id"], "changes": changes,
                "current": {"name": detail.get("name"),
                            "description": detail.get("description"),
                            "category": detail.get("category"),
                            "executor": detail.get("executor"),
                            "config_source": detail.get("config_source"),
                            "code": code, "settings": settings,
                            "parent_process_ids": detail.get("parent_process_ids") or [],
                            "child_process_ids": detail.get("child_process_ids") or [],
                            "uses_sample_sheet": bool(detail.get("uses_sample_sheet")),
                            "allow_multiple_sources":
                                bool(detail.get("allow_multiple_sources", True))}}
    return Draft(args=resolved, summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("processes")])


def apply_update_process(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import (ConfigSource, CustomPipelineSettings,
                                            CustomProcessInput, Executor, PipelineCode,
                                            RepositoryType)
    current, changes = args["current"], args["changes"]
    code = current["code"] or {}
    settings = current["settings"] or {}

    # CustomProcessInput replaces the record, so every field is resolved from the
    # current one and only the requested changes applied.
    request = CustomProcessInput(
        id=args["process_id"],
        name=changes.get("name", current["name"]),
        description=changes.get("description", current["description"]) or "",
        executor=Executor(current["executor"]),
        child_process_ids=current["child_process_ids"],
        parent_process_ids=current["parent_process_ids"],
        linked_project_ids=[],
        category=changes.get("category", current["category"]),
        uses_sample_sheet=current["uses_sample_sheet"],
        allow_multiple_sources=current["allow_multiple_sources"],
    )
    if current.get("config_source"):
        request.config_source = ConfigSource(current["config_source"])
    if code.get("repository_path"):
        request.pipeline_code = PipelineCode(
            repository_path=code["repository_path"],
            version=changes.get("version", code.get("version")),
            repository_type=RepositoryType(code.get("repository_type")
                                           or "GITHUB_PUBLIC"),
            entry_point=changes.get("entry_point", code.get("entry_point")),
            executor_version=changes.get("executor_version",
                                         code.get("executor_version")))
    if settings.get("repository"):
        request.custom_settings = CustomPipelineSettings(
            repository=changes.get("config_repository", settings["repository"]),
            branch=changes.get("config_branch", settings.get("branch")
                               or DEFAULT_BRANCH),
            folder=changes.get("config_folder", settings.get("folder")
                               or DEFAULT_CONFIG_FOLDER))

    reader.client.processes.update_custom_process(process_id=args["process_id"],
                                                  process=request)
    return {"updated_process_id": args["process_id"], "changed": sorted(changes)}


def plan_archive_process(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "archive_process", "process_id")
    detail = _owned_process(reader, args["process_id"])
    if detail.get("is_archived"):
        raise errors.WrongStatus(
            f"{args['process_id']} is already archived.",
            what="Nothing to do.", current_status="archived",
            retry_with=[{"verb": "query", "entity": "process"}],
        )
    lines = [f"Archive the pipeline {detail['name']} ({args['process_id']}).", "",
             f"  Category   {detail.get('category')}",
             f"  Code       {detail.get('code_location')}",
             "",
             "An archived pipeline cannot be launched. Datasets it already produced are "
             "unaffected."]
    return Draft(args={"process_id": args["process_id"], "name": detail["name"]},
                 summary="\n".join(lines), invalidates=[_inv("processes")])


def apply_archive_process(reader: reads.Reader, args: dict) -> dict:
    reader.client.processes.archive(process_id=args["process_id"])
    return {"archived_process_id": args["process_id"], "name": args.get("name")}


# ------------------------------------------------------- writing into a repo


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *arguments],
                          capture_output=True, text=True, timeout=60)


def _repo_root(path: Path) -> Optional[Path]:
    done = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, timeout=30)
    return Path(done.stdout.strip()) if done.returncode == 0 else None


def plan_write_process_config(reader: reads.Reader, args: dict) -> Draft:
    """Write the generated assets into a folder on local disk.

    A local write, and for the repo-backed patterns the folder is inside a git
    repository the user owns -- so it shows the exact paths and whether each one is new
    or would be overwritten.
    """
    _need(args, "write_process_config", "design_spec", "destination")
    spec = _load_spec(args["design_spec"])
    assets, report = _validated_assets(spec, preprocess=_read_optional(
        args.get("preprocess_path")))

    destination = Path(args["destination"]).expanduser().resolve()
    root = _repo_root(destination if destination.exists() else destination.parent)
    if root is None and not args.get("allow_outside_repo"):
        raise errors.InvalidQuery(
            f"{destination} is not inside a git repository.",
            what=("The repo-backed configuration patterns write into a checkout so you "
                  "can review and commit the change."),
            field="destination", given=str(destination),
            retry_with=[{"destination": "<path inside a clone>"},
                        {"allow_outside_repo": True,
                         "note": "write to a plain directory anyway"}],
        )

    new, overwritten, unchanged = [], [], []
    for name, text in sorted(assets.items()):
        target = destination / name
        if not target.exists():
            new.append(name)
        elif target.read_text() == text:
            unchanged.append(name)
        else:
            overwritten.append(name)

    relative = destination.relative_to(root) if root else destination
    lines = [
        f"Write {len(assets)} configuration files to {destination}.",
        "",
        f"  Repository    {root.name if root else '(not a git repository)'}",
        f"  Folder        {relative}",
        f"  New           {', '.join(new) or 'none'}",
        f"  Overwritten   {', '.join(overwritten) or 'none'}",
        f"  Unchanged     {', '.join(unchanged) or 'none'}",
        "",
        "This writes to your local disk only. Nothing reaches Cirro or git until you "
        "commit, push and sync.",
    ]
    warnings = [p["message"] for p in report["problems"] if p["level"] == "warning"]
    if overwritten:
        warnings.append(f"{len(overwritten)} existing file(s) will be replaced: "
                        f"{', '.join(overwritten)}")
    return Draft(args={"destination": str(destination), "assets": assets,
                       "repo_root": str(root) if root else None},
                 summary="\n".join(lines), warnings=warnings)


def apply_write_process_config(reader: reads.Reader, args: dict) -> dict:
    destination = Path(args["destination"])
    written = _stage(destination, args["assets"])
    follow = {"tool": "plan", "op": "commit_process_config",
              "args": {"destination": args["destination"]}} if args.get("repo_root") \
        else None
    return {"destination": args["destination"], "written": written, "next": follow}


def plan_commit_process_config(reader: reads.Reader, args: dict) -> Draft:
    """Commit the written assets locally. Never pushes.

    Pushing and opening a pull request stay the user's to do: this plugin does not put
    anything on a shared remote on its own.
    """
    _need(args, "commit_process_config", "destination")
    destination = Path(args["destination"]).expanduser().resolve()
    root = _repo_root(destination)
    if root is None:
        raise errors.InvalidQuery(
            f"{destination} is not inside a git repository.",
            what="There is nothing to commit to.", field="destination",
            retry_with=[{"note": "write the assets into a checkout first"}],
        )

    status = _git(root, "status", "--porcelain", "--", str(destination))
    entries = [line for line in status.stdout.splitlines() if line.strip()]
    if not entries:
        raise errors.InvalidQuery(
            f"Nothing to commit under {destination}.",
            what="The working tree is clean there.", field="destination",
            retry_with=[{"op": "write_process_config"}],
        )
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    default = branch in ("main", "master")

    lines = [
        f"Commit the pipeline configuration in {root.name}.",
        "",
        f"  Repository  {root}",
        f"  Branch      {branch}",
        f"  Folder      {destination.relative_to(root)}",
        "",
        "  Changes:",
    ]
    lines += [f"    {line}" for line in entries[:20]]
    if len(entries) > 20:
        lines.append(f"    ... {len(entries) - 20} more")
    lines += ["", "This commits locally only. Nothing is pushed; the remote is "
                  "untouched until you push it yourself."]

    warnings = []
    if default:
        warnings.append(f"You are on {branch}. Consider a branch so the change can be "
                        "reviewed before it reaches the default branch.")
    return Draft(
        args={"repo_root": str(root), "destination": str(destination),
              "branch": branch, "n_changes": len(entries),
              "message": args.get("message")
              or "Add Cirro pipeline configuration"},
        summary="\n".join(lines), warnings=warnings)


def apply_commit_process_config(reader: reads.Reader, args: dict) -> dict:
    root = Path(args["repo_root"])
    staged = _git(root, "add", "--", args["destination"])
    if staged.returncode != 0:
        raise errors.UpstreamError(
            f"git add failed: {staged.stderr.strip()[:200]}",
            what="The files could not be staged.",
            retry_with=[{"note": "check the path and repository state"}],
        )
    done = _git(root, "commit", "-m", args["message"], "--", args["destination"])
    if done.returncode != 0:
        raise errors.UpstreamError(
            f"git commit failed: {(done.stderr or done.stdout).strip()[:200]}",
            what="Nothing was committed.",
            retry_with=[{"note": "resolve the git state and plan again"}],
        )
    sha = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    return {"repository": str(root), "branch": args["branch"], "commit": sha,
            "pushed": False,
            "next": f"push it yourself when ready: git -C {root} push origin "
                    f"{args['branch']}"}


PIPELINE_OPS = [
    Op("create_process", RISK_WRITE, plan_create_process, apply_create_process),
    Op("update_process", RISK_WRITE, plan_update_process, apply_update_process),
    Op("upload_process_config", RISK_WRITE_LOCAL, plan_upload_process_config,
       apply_upload_process_config),
    Op("sync_process_config", RISK_WRITE, plan_sync_process_config,
       apply_sync_process_config),
    Op("archive_process", RISK_DESTRUCTIVE, plan_archive_process, apply_archive_process),
    Op("write_process_config", RISK_LOCAL, plan_write_process_config,
       apply_write_process_config),
    Op("commit_process_config", RISK_LOCAL, plan_commit_process_config,
       apply_commit_process_config),
]

mutations.OPS.update({op.name: op for op in PIPELINE_OPS})

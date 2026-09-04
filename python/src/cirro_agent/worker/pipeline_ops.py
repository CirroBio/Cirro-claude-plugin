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

from .. import errors, formcheck, formgen, session, workflow_spec
from . import mutations, reads, rows as R
from .mutations import (Draft, Op, RISK_DESTRUCTIVE, RISK_LOCAL, RISK_WRITE,
                        RISK_WRITE_LOCAL,
                        _inv, _need)

STORAGE = "STORAGE"
WITH_PROCESS = "WITH_PROCESS"
EXTERNAL = "EXTERNAL"
CONFIG_SOURCES = (STORAGE, WITH_PROCESS, EXTERNAL)

# Assumed when neither the caller nor the existing record names one. Every other
# RepositoryType describes a repository Cirro needs extra credentials to reach.
DEFAULT_REPOSITORY_TYPE = "GITHUB_PUBLIC"

# Verified live: the API rejects other characters in a process name with HTTP 400.
NAME_CHARSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .&\-@!#'()/]*$")

# The server-side regex engine wants (?<name>...); Python's (?P<name>...) 400s.
PYTHON_NAMED_GROUP = re.compile(r"\(\?P<")


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


def _warnings(report: dict) -> list:
    """The warning messages of a validation report, as the approval prompt lists them."""
    return [p["message"] for p in formcheck.warnings(report)]


def _refuse_if_broken(assets: dict, *, subject: str, what: str, field: str,
                      retry_with: list, given=None) -> dict:
    """Validate the assets and refuse to go further if they will not render."""
    report = formcheck.check_assets(assets)
    if not report["ok"]:
        detail = "; ".join(f"{p['where']}: {p['message']}"
                           for p in formcheck.errors(report))
        raise errors.InvalidQuery(
            f"{subject} would not render: {detail}",
            what=what, field=field, given=given, retry_with=retry_with,
        )
    return report


def _validated_assets(spec: dict, preprocess: Optional[str] = None) -> tuple:
    """Generate the assets and refuse to go further if they will not render."""
    assets = formgen.generate(spec, preprocess=preprocess)
    report = _refuse_if_broken(
        assets, subject="The generated configuration",
        what="Fix the design spec so the assets validate.", field="design_spec",
        retry_with=[{"note": "run `cirro-agent read check-config <dir>` after editing"}])
    return assets, report


def _authored_assets(path: str) -> tuple:
    """Read configuration assets a person wrote, and upload them exactly as written.

    Generation cannot express every form. Conditional blocks driven by more than one
    selector, custom widgets, and input maps tuned by hand are all legitimate and all
    lost in a round trip through a design spec -- so a folder of files is a first-class
    alternative to one, not a fallback. Nothing here rewrites or reformats them; they
    are validated by the same checker `check-config` runs and sent verbatim.
    """
    directory = Path(path).expanduser()
    if not directory.is_dir():
        raise errors.NotFound(
            f"No configuration folder at {directory}.",
            what="assets_dir is a folder holding the process-*.json files you wrote.",
            field="assets_dir", given=path,
            retry_with=[{"note": "check the path, or pass design_spec to generate them"}],
        )
    assets = {name: (directory / name).read_text(encoding="utf-8")
              for name in formgen.SLOTS if (directory / name).is_file()}
    if formgen.FORM not in assets:
        raise errors.InvalidQuery(
            f"{directory} holds no {formgen.FORM}.",
            what=f"The form is the one required asset. Recognized names: "
                 f"{', '.join(sorted(formgen.SLOTS))}.",
            field="assets_dir", given=path,
            found=sorted(p.name for p in directory.iterdir() if p.is_file()),
            retry_with=[{"note": f"name the form file {formgen.FORM}"}],
        )
    report = _refuse_if_broken(
        assets, subject=f"The configuration in {directory}",
        what="Fix the files; these are defects Cirro would reject or mis-render.",
        field="assets_dir", given=path,
        retry_with=[{"verb": "check_config", "path": str(directory)}])
    return assets, report


def _assets_for(args: dict, spec: Optional[dict] = None, *,
                preprocess: Optional[str] = None) -> tuple:
    """The assets an operation will act on: authored by hand, or generated from a spec."""
    if args.get("assets_dir"):
        if preprocess:
            raise errors.InvalidQuery(
                "assets_dir and preprocess_path conflict.",
                what="An authored folder supplies its own preprocess.py.",
                field="preprocess_path", given=args.get("preprocess_path"),
                retry_with=[{"note": "drop preprocess_path; put the script in the "
                                     "folder as preprocess.py"}],
            )
        return _authored_assets(args["assets_dir"])
    if spec is None:
        spec = _load_spec(args["design_spec"])
    return _validated_assets(spec, preprocess=preprocess)


def _asset_origin(args: dict) -> str:
    return (f"hand-authored in {Path(args['assets_dir']).expanduser()}"
            if args.get("assets_dir")
            else f"generated from {Path(args['design_spec']).name}")


def _one_asset_source(args: dict, op: str) -> None:
    """Exactly one of the two: a spec to generate from, or a folder already written."""
    given = [key for key in ("design_spec", "assets_dir") if args.get(key)]
    if len(given) == 1:
        return
    raise errors.InvalidQuery(
        f"{op} needs either design_spec or assets_dir"
        + (", not both." if given else ", and got neither."),
        what=("design_spec generates the assets; assets_dir uploads files you wrote, "
              "verbatim, for form logic a spec cannot express."),
        field="assets_dir", given=given or None,
        retry_with=[{"op": op, "args": {"design_spec": "path/to/design-spec.md"}},
                    {"op": op, "args": {"assets_dir": "path/to/config"}}],
    )


def _owned_process(reader: reads.Reader, process_id: str) -> dict:
    """Fetch a process and refuse to mutate one belonging to someone else.

    The manage grant is tenant-wide, so the credential can technically edit any custom
    pipeline. Ownership is not enforced by the platform, so it is enforced here.
    """
    found = reader.processes(include_archived=True)
    row = next((p for p in found if p["id"] == process_id), None)
    if row is None:
        near = reads.near_misses(process_id, [p["id"] for p in found])
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
    return reads.process_detail(reader, process_id, row)


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


def _config_source(args: dict) -> str:
    """One of the three patterns verified live; anything else is a typo, not a variant."""
    source = str(args["config_source"]).upper()
    if source not in CONFIG_SOURCES:
        raise errors.InvalidQuery(
            f"{args['config_source']!r} is not a configuration source.",
            what=("STORAGE keeps the config in Cirro; WITH_PROCESS keeps it in the "
                  "workflow's own repository; EXTERNAL keeps it in a separate one."),
            field="config_source", given=args["config_source"],
            retry_with=[{"config_source": value} for value in CONFIG_SOURCES],
        )
    return source


def _refuse_if_registered(reader: reads.Reader, process_id: str) -> None:
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


def _workflow_version(args: dict, workflow: dict) -> str:
    """The ref of the workflow repository the record is pinned to.

    A process is registered against one ref, and version is required on
    PipelineCode. Stringifying a missing one registered a pipeline pinned to the
    literal ref "None", which resolves to nothing.
    """
    version = workflow.get("version")
    if not version:
        raise errors.InvalidQuery(
            "The design spec's workflow block names no version.",
            what=("Cirro runs a specific ref of the workflow repository -- a tag, "
                  "branch or commit -- and the record cannot be registered without "
                  "one."),
            field="design_spec", given=args["design_spec"],
            retry_with=[{"note": "add workflow.version to the spec frontmatter, e.g. "
                                 "the release tag the pipeline is pinned to"}],
        )
    return str(version)


def _config_settings(args: dict, source: str, code_repo: str,
                     code_repository_type: str) -> Optional[dict]:
    """Where a repo-backed configuration lives, or None when Cirro stores it.

    repository_type is required. Without it Cirro cannot tell how to reach the
    configuration repository and rejects the create -- as a 500 NullPointerException
    for some folder names, and as a 400 for others. A WITH_PROCESS config is in the
    workflow's own repository, so it always shares the code repository's type; an
    EXTERNAL one may be a repository of different visibility and can be overridden.
    """
    if source == STORAGE:
        return None
    repo = workflow_spec.repository_slug(
        args.get("config_repository") or (code_repo if source == WITH_PROCESS else ""))
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
    return {
        "repository": repo,
        "branch": args.get("config_branch") or R.DEFAULT_BRANCH,
        "folder": args.get("config_folder") or R.DEFAULT_CONFIG_FOLDER,
        "repository_type": (code_repository_type if source == WITH_PROCESS else
                            (args.get("config_repository_type")
                             or code_repository_type)),
    }


def _validated_registration(reader: reads.Reader, args: dict, spec: dict,
                            source: str) -> dict:
    """Refuse every request that cannot be registered, and resolve what remains.

    Nothing is generated here, so a request naming a taken id, an unrunnable ref or
    a configuration repository that contradicts its own pattern is refused before
    any asset is read or built.
    """
    workflow = spec["workflow"]
    process_id = args.get("process_id") or (spec.get("cirro") or {}).get("process_id")
    name = args.get("name") or workflow.get("name")
    _need({"process_id": process_id, "name": name}, "create_process",
          "process_id", "name")
    _check_name(name)
    _refuse_if_registered(reader, process_id)
    version = _workflow_version(args, workflow)
    code_repo = workflow_spec.repository_slug(workflow.get("repository", ""))
    code_repository_type = args.get("repository_type") or DEFAULT_REPOSITORY_TYPE
    return {
        "process_id": process_id,
        "name": name,
        "version": version,
        "code_repo": code_repo,
        "repository_type": code_repository_type,
        "settings": _config_settings(args, source, code_repo, code_repository_type),
    }


def _executor(workflow: dict) -> str:
    executor = str(workflow.get("executor", "NEXTFLOW")).upper()
    if executor not in ("NEXTFLOW", "CROMWELL"):
        raise errors.NotSupported(
            f"Cirro runs Nextflow and WDL/Cromwell; the spec says {executor!r}.",
            what="Only those two executors can be registered here.",
            user_action="Check workflow.executor in the design spec.",
        )
    return executor


def _registration_record(args: dict, spec: dict, source: str, checked: dict,
                         executor: str) -> dict:
    """The whole record: what the approval describes and what apply sends."""
    workflow = spec["workflow"]
    cirro = spec.get("cirro") or {}
    resolved = {
        "process_id": checked["process_id"],
        "name": checked["name"],
        "description": args.get("description") or spec.get("description")
        or f"{workflow.get('name')} {workflow.get('version', '')}".strip(),
        "executor": executor,
        "category": args.get("category") or cirro.get("category") or "Community",
        "config_source": source,
        "custom_settings": checked["settings"],
        "repository_path": checked["code_repo"],
        "version": checked["version"],
        "entry_point": workflow.get("entrypoint") or "main.nf",
        "executor_version": workflow.get("executor_version"),
        "repository_type": checked["repository_type"],
        "parent_process_ids": cirro.get("parent_process_ids") or [],
        "child_process_ids": cirro.get("child_process_ids") or [],
        "uses_sample_sheet": bool(args.get("uses_sample_sheet")),
        "allow_multiple_sources": bool(args.get("allow_multiple_sources", True)),
        "file_mapping_rules": _file_mapping_rules(spec),
        "design_spec": str(Path(args["design_spec"]).expanduser().resolve()),
    }
    if args.get("assets_dir"):
        resolved["assets_dir"] = str(Path(args["assets_dir"]).expanduser().resolve())
    return resolved


def _where_config_lives(source: str, settings: Optional[dict]) -> str:
    """The half-sentence naming where Cirro will read the configuration from."""
    if source == STORAGE:
        return "uploaded into Cirro storage (a separate upload step after this)"
    whose = ("the workflow's own repository" if source == WITH_PROCESS
             else "a separate configuration repository")
    return (f"read from {settings['repository']}@{settings['branch']} in "
            f"{settings['folder']}/ [{settings['repository_type']}] ({whose})")


def _registration_summary(reader: reads.Reader, args: dict, resolved: dict,
                          assets: dict, report: dict) -> str:
    source = resolved["config_source"]
    lines = [
        f"Register a new Cirro pipeline on {reader.base_url}.",
        "",
        f"  Process id      {resolved['process_id']}",
        f"  Name            {resolved['name']}",
        f"  Category        {resolved['category']}",
        f"  Workflow code   {resolved['repository_path']}@{resolved['version']}"
        f":{resolved['entry_point']} [{resolved['executor']}]",
        f"  Engine version  {resolved['executor_version'] or 'Cirro default'}",
        f"  Configuration   {source} — "
        f"{_where_config_lives(source, resolved['custom_settings'])}",
        "",
        f"  Form            {len(assets)} assets, validated clean"
        + (f" ({report['n_warnings']} warnings)" if report["n_warnings"] else ""),
        f"  Assets          {_asset_origin(args)}",
    ]
    if source == STORAGE:
        follow = "upload_process_config"
        if args.get("assets_dir"):
            follow += f" with the same assets_dir ({resolved['assets_dir']})"
        lines += ["", "Registering does not upload the configuration. Follow this with "
                      f"{follow}."]
    else:
        lines += ["", "Registering does not fetch the configuration. Write the assets "
                      "into the repository, commit and push them, then run "
                      "sync_process_config."]
    return "\n".join(lines)


def _registration_warnings(resolved: dict, report: dict) -> list:
    warnings = _warnings(report)
    if resolved["executor_version"]:
        warnings.append(
            "executor_version is pinned. Cirro's default engine is right for most "
            "pipelines; pin only to recover from a compilation failure.")
    return warnings


def plan_create_process(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "create_process", "design_spec", "config_source")
    spec = _load_spec(args["design_spec"])
    source = _config_source(args)
    # Ordered the way the refusals should arrive: the record's identity and its
    # repositories first, so a request that cannot be registered is refused without
    # generating anything, then the assets the pre-flight vouches for, then the
    # executor the record will carry.
    checked = _validated_registration(reader, args, spec, source)
    assets, report = _assets_for(args, spec)
    resolved = _registration_record(args, spec, source, checked,
                                    _executor(spec["workflow"]))
    return Draft(args=resolved,
                 summary=_registration_summary(reader, args, resolved, assets, report),
                 warnings=_registration_warnings(resolved, report),
                 invalidates=[_inv("processes")])


def _process_input(*, process_id: str, name: str, description: str, executor: str,
                   category: Optional[str], parent_process_ids: list,
                   child_process_ids: list,
                   uses_sample_sheet: bool, allow_multiple_sources: bool):
    """The whole-record field set every process write has to fill.

    CustomProcessInput replaces the record rather than patching it, so a field left
    out is a field blanked. Built in one place so that a field the model gains is
    gained by create and update together, instead of one path quietly erasing what
    the other sets.
    """
    from cirro_api_client.v1.models import CustomProcessInput, Executor
    return CustomProcessInput(
        id=process_id, name=name, description=description,
        executor=Executor(executor), category=category,
        parent_process_ids=parent_process_ids, child_process_ids=child_process_ids,
        linked_project_ids=[],
        uses_sample_sheet=uses_sample_sheet,
        allow_multiple_sources=allow_multiple_sources,
    )


def _pipeline_code(*, repository_path: str, version: str, repository_type: str,
                   entry_point: str, executor_version: Optional[str]):
    """Where the workflow code lives, with no engine version unless one is pinned.

    PipelineCode.to_dict() omits executorVersion only when it is UNSET; passing None
    puts an explicit `"executorVersion": null` on the wire, which is a request to have
    no engine version rather than a request to use the platform's default.
    """
    from cirro_api_client.v1.models import PipelineCode, RepositoryType
    from cirro_api_client.v1.types import UNSET
    return PipelineCode(
        repository_path=repository_path, version=version,
        repository_type=RepositoryType(repository_type), entry_point=entry_point,
        executor_version=executor_version or UNSET)


def apply_create_process(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import (ConfigSource, CustomPipelineSettings,
                                            FileMappingRule, FileNamePattern,
                                            RepositoryType)
    settings = args.get("custom_settings")
    request = _process_input(
        process_id=args["process_id"], name=args["name"],
        description=args["description"], executor=args["executor"],
        category=args["category"],
        parent_process_ids=args["parent_process_ids"],
        child_process_ids=args["child_process_ids"],
        uses_sample_sheet=args["uses_sample_sheet"],
        allow_multiple_sources=args["allow_multiple_sources"])
    request.config_source = ConfigSource(args["config_source"])
    request.pipeline_code = _pipeline_code(
        repository_path=args["repository_path"], version=args["version"],
        repository_type=args["repository_type"], entry_point=args["entry_point"],
        executor_version=args.get("executor_version"))
    if settings:
        request.custom_settings = CustomPipelineSettings(
            repository=settings["repository"], branch=settings["branch"],
            folder=settings["folder"],
            repository_type=RepositoryType(settings.get("repository_type")
                                           or args["repository_type"]))
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
    # The generated client returns the parsed body for 400 and 401 instead of raising,
    # so a rejected create arrives here as a response carrying no id. Falling back to
    # the requested id would report a process that does not exist -- the caller then
    # believes it registered something, and only finds out when every later read 404s.
    created = R.plain(getattr(response, "id", None))
    if not created:
        raise errors.UpstreamError(
            f"Cirro did not create process {args['process_id']!r}: the API accepted the "
            f"request but returned no process id.",
            what="A create that returns no id did not register anything.",
            user_action=("Check the process in the Cirro UI before retrying. If this "
                         "persists, the request was rejected without an error body."),
        )
    follow = ({"tool": "plan", "op": "upload_process_config",
               "args": {"process_id": created, "design_spec": args["design_spec"]}}
              if args["config_source"] == STORAGE else
              {"tool": "plan", "op": "sync_process_config",
               "args": {"process_id": created}})
    return {"created_process_id": created, "config_source": args["config_source"],
            "next": follow}


# ------------------------------------------------------- upload / sync config


STAGING_DIR = "pipeline-config"
# How many changed files an approval lists before summarizing the rest.
ENTRY_PREVIEW = 20


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
    _need(args, "upload_process_config", "process_id")
    _one_asset_source(args, "upload_process_config")
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

    assets, report = _assets_for(args, preprocess=_read_optional(
        args.get("preprocess_path")))

    token = _latest_revision_token(reader, args["process_id"])
    # Only the comparison degrades if the live slots cannot be read: the revision
    # number is already in hand and still guards the write, so it is not discarded.
    current, unread = {}, None
    try:
        current = _current_assets(reader, args["process_id"])
    except Exception as exc:
        unread = str(exc) or exc.__class__.__name__

    # Authored assets are already on disk where the person put them; there is nothing
    # to stage, and writing over their own source files would be a surprise.
    staging = None if args.get("assets_dir") else _staging_dir(args)
    replaced = sorted(name for name in assets if staging and (staging / name).exists()
                      and (staging / name).read_text() != assets[name])
    changed, added = [], []
    for name, text in sorted(assets.items()):
        slot = formgen.SLOTS[name]
        if slot not in current:
            added.append(name)
        elif current[slot].strip() != text.strip():
            changed.append(name)
    # A live slot this upload has no asset for. `metadata` is the standing case: it is
    # in the slot vocabulary but nothing generates or authors one, so an upload that
    # leaves it out must say so rather than let the approver read New and Changed as
    # the whole of what the revision touches.
    absent = sorted(set(current) - {formgen.SLOTS[name] for name in assets})
    # Reporting an unread slot as New would understate what the upload replaces, so
    # the whole comparison is withheld rather than half of it presented as fact.
    unknown = "unknown — the live configuration could not be read"

    lines = [
        f"Upload configuration to {detail['name']} ({args['process_id']}) on "
        f"{reader.base_url}.",
        "",
        f"  Current revision  {token or 'none — this would be the first'}",
        f"  Assets            {len(assets)}: {', '.join(sorted(assets))}",
        f"  Source            {_asset_origin(args)}",
        f"  New               {unknown if unread else ', '.join(added) or 'none'}",
        f"  Changed           {unknown if unread else ', '.join(changed) or 'none'}",
    ]
    if absent:
        lines.append(f"  Not uploaded      {', '.join(absent)} "
                     f"(live now, no asset here)")
    if staging:
        lines += [f"  Staged to         {staging}",
                  f"  Replaced locally  {', '.join(replaced) or 'none'}"]
    lines += [
        "",
        # "All slots" once read as though it covered the live slots this upload carries
        # no asset for, which sit on the `Not uploaded` line above. It means the slots
        # being uploaded, so say that.
        "The assets listed above go up together in one revision, so the configuration "
        "is never half-applied.",
    ]
    if absent:
        lines.append(f"What becomes of the live {'slots' if len(absent) > 1 else 'slot'} "
                     f"this upload carries no asset for ({', '.join(absent)}) is Cirro's "
                     "to decide; this plan does not send them.")
    lines.append("The files go up exactly as written; nothing is reformatted."
                 if args.get("assets_dir") else
                 "A copy is kept in the folder above so you can read it later without "
                 "asking Cirro for it back.")
    warnings = _warnings(report)
    if replaced:
        warnings.append(f"{len(replaced)} file(s) in {staging} will be overwritten: "
                        f"{', '.join(replaced)}")
    if absent:
        warnings.append(
            f"the configuration live now also holds {', '.join(absent)}, which this "
            f"upload carries no asset for -- read it with process_config before "
            f"approving if that slot matters")
    if unread:
        warnings.append(
            f"Cirro did not return the configuration {args['process_id']} is serving "
            f"now ({unread}), so this plan cannot say which slots the upload would "
            f"replace. The write is still guarded: it carries the revision above and "
            f"Cirro rejects it if that has moved.")
    elif token and not changed and not added:
        warnings.append("Nothing differs from the current revision; this would be a "
                        "no-op revision.")
    source = args.get("design_spec") or args["assets_dir"]
    return Draft(
        args={"process_id": args["process_id"], "assets": assets, "if_match": token,
              "stage_dir": str(staging) if staging else None,
              "assets_dir": (str(Path(args["assets_dir"]).expanduser().resolve())
                             if args.get("assets_dir") else None),
              "commit_message": args.get("commit_message")
              or f"Configuration from {Path(source).name}"},
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


def _latest_revision_token(reader: reads.Reader, process_id: str) -> Optional[str]:
    """The revision number to send as if_match, or None when there is no revision yet.

    The two outcomes must not be confused. A 404 is the API's answer for a process
    with no configuration in tenant storage, so it really is a first upload. Anything
    else means the current revision is unknown -- and an unknown revision reported as
    "none" both understates what the approval is agreeing to replace and sends the
    write with no if_match, dropping the guard that stops it overwriting a revision
    nobody looked at.
    """
    revision, failure = reads.process_revision(reader, process_id)
    if failure:
        raise errors.UpstreamError(
            f"Could not read the current configuration revision of {process_id}: "
            f"{failure['reason']}",
            what=("The upload replaces whatever revision is live, and the revision "
                  "number is what stops it replacing one nobody has seen. Without it "
                  "the plan cannot describe the write honestly."),
            retry_with=[{"op": "upload_process_config",
                         "args": {"process_id": process_id},
                         "note": "plan again once the tenant answers"}],
            status_code=failure["status_code"],
        )
    return revision["token"] if revision else None


def _current_assets(reader: reads.Reader, process_id: str) -> dict:
    """The configuration Cirro is serving now, by slot.

    An unreadable slot is raised rather than left out of the map. The planner catches
    that and withholds the whole comparison; a slot merely absent here reads as empty,
    and the plan would then call the asset New and understate what the upload replaces.
    """
    content, unreadable = reads.process_assets(reader, process_id)
    if unreadable:
        slot, failure = sorted(unreadable.items())[0]
        raise errors.UpstreamError(
            f"Cirro would not return the {slot} slot of {process_id} "
            f"({failure['reason']}).",
            what="The live configuration cannot be compared against the new one.",
            retry_with=[{"op": "upload_process_config",
                         "args": {"process_id": process_id}}],
            status_code=failure["status_code"],
        )
    return content


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
    if args.get("assets_dir"):
        result["authored_from"] = args["assets_dir"]
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
        f"  Folder        {settings.get('folder') or R.DEFAULT_CONFIG_FOLDER}/",
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

    # Booleans are compared against None, not truthiness: turning one off is the
    # commoner edit, and `if args.get(key)` would silently ignore it. Both are on the
    # process record and already sent by apply, but were unreachable through an update,
    # so a process registered with the wrong flag could not be corrected here at all.
    for key, current in (("uses_sample_sheet", detail.get("uses_sample_sheet")),
                         ("allow_multiple_sources",
                          detail.get("allow_multiple_sources"))):
        if args.get(key) is not None and bool(args[key]) != bool(current):
            changes[key] = bool(args[key])

    config_repo = args.get("config_repository")
    if config_repo and workflow_spec.repository_slug(config_repo) != \
            settings.get("repository"):
        changes["config_repository"] = workflow_spec.repository_slug(config_repo)
    # config_repository_type is recorded here, not only read at apply: a change that
    # never reaches `changes` is absent from the diff the person approves, and the
    # apply-side read of it can then never fire. It is the one field this path exists
    # to protect -- dropping it leaves Cirro unable to reach the configuration at all.
    for key, current in (("config_branch", settings.get("branch")),
                         ("config_folder", settings.get("folder")),
                         ("config_repository_type", settings.get("repository_type"))):
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
               "config_folder": settings.get("folder"),
               "config_repository_type": settings.get("repository_type"),
               }.get(key, detail.get(key))
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
                                            RepositoryType)
    current, changes = args["current"], args["changes"]
    code = current["code"] or {}
    settings = current["settings"] or {}

    # CustomProcessInput replaces the record, so every field is resolved from the
    # current one and only the requested changes applied.
    request = _process_input(
        process_id=args["process_id"],
        name=changes.get("name", current["name"]),
        description=changes.get("description", current["description"]) or "",
        executor=current["executor"],
        category=changes.get("category", current["category"]),
        parent_process_ids=current["parent_process_ids"],
        child_process_ids=current["child_process_ids"],
        uses_sample_sheet=changes.get("uses_sample_sheet",
                                      current["uses_sample_sheet"]),
        allow_multiple_sources=changes.get("allow_multiple_sources",
                                           current["allow_multiple_sources"]))
    if current.get("config_source"):
        request.config_source = ConfigSource(current["config_source"])
    if code.get("repository_path"):
        request.pipeline_code = _pipeline_code(
            repository_path=code["repository_path"],
            version=changes.get("version", code.get("version")),
            repository_type=code.get("repository_type") or DEFAULT_REPOSITORY_TYPE,
            entry_point=changes.get("entry_point", code.get("entry_point")),
            executor_version=changes.get("executor_version",
                                         code.get("executor_version")))
    if settings.get("repository"):
        # Carried through for the same reason it is set on create: Cirro needs the
        # config repository's type to reach it, and an update that drops it turns a
        # working configuration into one the server can no longer resolve.
        request.custom_settings = CustomPipelineSettings(
            repository=changes.get("config_repository", settings["repository"]),
            branch=changes.get("config_branch", settings.get("branch")
                               or R.DEFAULT_BRANCH),
            folder=changes.get("config_folder", settings.get("folder")
                               or R.DEFAULT_CONFIG_FOLDER),
            repository_type=RepositoryType(
                changes.get("config_repository_type")
                or settings.get("repository_type")
                or code.get("repository_type")
                or DEFAULT_REPOSITORY_TYPE))

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
    """For the calls whose failure this module has to report.

    `session.git_output` answers questions and returns None for every kind of "no
    answer", which is right for reads. `add` and `commit` are not questions: their
    returncode and stderr are what the apply path tells the person went wrong, so
    those callers need the CompletedProcess itself.
    """
    return subprocess.run(["git", "-C", str(repo), *arguments],
                          capture_output=True, text=True, timeout=60)


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
    root = session.git_root(destination if destination.exists()
                            else destination.parent)
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
    warnings = _warnings(report)
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
    root = session.git_root(destination)
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
    lines += [f"    {line}" for line in entries[:ENTRY_PREVIEW]]
    if len(entries) > ENTRY_PREVIEW:
        lines.append(f"    ... {len(entries) - ENTRY_PREVIEW} more")
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
    Op("create_process", RISK_WRITE, plan_create_process, apply_create_process,
       outputs=("created_process_id",)),
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

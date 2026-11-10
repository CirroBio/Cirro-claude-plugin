"""Extract a workflow's declared inputs and parameters.

Deterministic reading only. This module reports what a workflow's own schema says,
plus *candidate* classifications with the reason for each -- it never decides which
parameter is the primary data input or which one Cirro manages. Those calls need
judgment about the pipeline's intent and belong to the agent, which is also why every
uncertainty comes back as an explicit flag rather than a silent guess.

Touches no Cirro API.
"""
import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from . import errors, formcheck, session

NEXTFLOW = "NEXTFLOW"
CROMWELL = "CROMWELL"

# How long to wait on git for a fact about a clone. Longer than session resolution's
# bound: `describe --tags` on a large history is not instant.
GIT_TIMEOUT = 30

# The spellings of a git remote a slug can be recovered from. Anything else is
# already a slug, or a bare pipeline name.
REMOTE_PREFIXES = ("http://", "https://", "git@")

# `owner/name` out of https://host/owner/name.git and git@host:owner/name.git alike:
# the last colon-or-slash separated pair, with any .git suffix dropped.
REPOSITORY_SLUG = re.compile(r"^.*[:/]([^/]+/[^/]+?)(\.git)?$")

# nf-core / epi2me schema keys this parser understands. Anything else on a property is
# reported under `unknown_keys` rather than dropped silently.
KNOWN_PARAM_KEYS = {
    "type", "description", "default", "help_text", "hidden", "title", "format", "enum",
    "fa_icon", "minimum", "maximum", "pattern", "mimetype", "exists", "schema",
    "overrides", "errorMessage", "enumNames", "items", "uniqueItems",
}

# JSON-Schema `format` values that mean "this is a path, not a scalar": the ones Cirro's
# form reads, plus two spellings older nf-core schemas use that it does not.
PATH_FORMATS = set(formcheck.PATH_FORMATS) | {"file", "directory"}

# Samplesheet column keys carried through, each as the schema spells it and as the
# design spec does, so a column is reported ready to copy into the spec.
COLUMN_KEYS = (("title", "title"), ("description", "description"),
               ("pattern", "pattern"), ("enum", "enum"),
               ("minimum", "minimum"), ("maximum", "maximum"),
               ("errorMessage", "error_message"))

# A column's path format as the design spec's column `type` names its picker, with the
# two older nf-core spellings PATH_FORMATS also accepts.
COLUMN_TYPE_BY_FORMAT = {
    **{fmt: kind for kind, fmt in formcheck.PATH_FORMAT_BY_TYPE.items()},
    "file-path-pattern": "file", "file": "file", "directory": "folder"}

# Evidence that a path parameter names a table the workflow reads row by row.
TABLE_MIMETYPES = {"text/csv", "text/tsv", "text/tab-separated-values"}
TABLE_IN_PATTERN = re.compile(r"\b[ct]sv\b", re.I)
TABLE_SUFFIX = re.compile(r"\.[ct]sv(\.gz)?$", re.I)

# Where nf-core keeps a samplesheet's columns. Pipelines from before nf-schema keep
# them here without any parameter naming the file.
DEFAULT_TABLE_SCHEMA = "assets/schema_input.json"

# Names that conventionally denote Cirro's managed output directory.
OUTPUT_DIR_NAMES = {"outdir", "out_dir", "output_dir", "outputdir", "publish_dir",
                    "outputDir"}

# Names that conventionally denote the primary data input.
PRIMARY_INPUT_NAMES = {"input", "bam", "fastq", "fastq_dir", "reads", "cram", "ubam",
                       "input_dir", "samplesheet", "fast5", "pod5", "bam_dir"}

# Engine knobs that are infrastructure, not science: never user-facing.
INFRA_NAMES = {"aws_image_prefix", "aws_queue", "monochrome_logs", "validate_params",
               "show_hidden_params", "version", "help", "tracedir", "publish_dir_mode",
               "max_cpus", "max_memory", "max_time", "config_profile_name",
               "config_profile_description", "config_profile_contact",
               "config_profile_url", "custom_config_version", "custom_config_base",
               "hook_url", "email", "email_on_fail", "plaintext_email",
               "monochromeLogs", "disable_ping"}


def resolve_source(target: str, ref: Optional[str], cache_root: Path) -> dict:
    """Accept a local checkout or a git URL; return where the tree is."""
    candidate = Path(target).expanduser()
    if candidate.is_dir():
        return {"local_path": str(candidate.resolve()), "cloned": False,
                **_git_identity(candidate)}

    url, slug = _normalize_repo(target)
    into = cache_root / "workflows" / f"{slug.replace('/', '-')}-{ref or 'default'}"
    if not (into / ".git").is_dir():
        into.parent.mkdir(parents=True, exist_ok=True)
        command = ["git", "clone", "--depth", "1"]
        if ref:
            command += ["--branch", ref]
        command += [url, str(into)]
        done = subprocess.run(command, capture_output=True, text=True, timeout=600)
        if done.returncode != 0:
            raise errors.NotFound(
                f"Could not clone {url}"
                + (f" at {ref!r}" if ref else "")
                + f": {done.stderr.strip()[-300:]}",
                what="Check the repository, and that the tag or branch exists.",
                field="target", given=target,
                retry_with=[{"target": url, "ref": "main"},
                            {"target": "a local checkout path instead"}],
            )
    return {"local_path": str(into.resolve()), "cloned": True, "repository": slug,
            "url": url, **_git_identity(into)}


def repository_slug(url_or_slug: str) -> str:
    """`owner/name` from a git remote URL; an already-bare slug passes through.

    One implementation, shared with the pipeline planner, because the two callers
    have to agree: `create_process` records the slug for a repository the spec
    reader identified, and a parse that disagreed would register a pipeline
    against a repository nobody named.
    """
    text = (url_or_slug or "").strip().rstrip("/")
    if text.startswith(REMOTE_PREFIXES):
        return REPOSITORY_SLUG.sub(r"\1", text)
    return text


def _normalize_repo(target: str) -> tuple:
    text = target.strip().rstrip("/")
    if text.startswith(REMOTE_PREFIXES):
        return text, repository_slug(text)
    if "/" in text:
        return f"https://github.com/{text}.git", text
    # A bare name is an nf-core pipeline by convention.
    return f"https://github.com/nf-core/{text}.git", f"nf-core/{text}"


def _git_identity(path: Path) -> dict:
    out = {}
    for key, arguments in (("version", ["describe", "--tags", "--always"]),
                           ("commit", ["rev-parse", "HEAD"])):
        found = session.git_output(path, *arguments, timeout=GIT_TIMEOUT)
        if found:
            out[key] = found
    origin = session.git_output(path, "remote", "get-url", "origin",
                                timeout=GIT_TIMEOUT)
    if origin:
        out["repository"] = _normalize_repo(origin)[1]
    return out


def detect_engine(root: Path) -> str:
    if (root / "nextflow.config").is_file() or (root / "main.nf").is_file():
        return NEXTFLOW
    if list(root.glob("*.wdl")) or list(root.glob("workflows/*.wdl")):
        return CROMWELL
    raise errors.NotSupported(
        f"{root} looks like neither a Nextflow nor a WDL workflow.",
        what="Cirro runs Nextflow and WDL/Cromwell workflows.",
        retry_with=[{"note": "point at the repository root, not a subdirectory"}],
        user_action="Confirm this is a workflow repository.",
    )


def parse(root: Path, engine: str) -> dict:
    if engine == NEXTFLOW:
        return _parse_nextflow(root)
    return _parse_wdl(root)


# ------------------------------------------------------------------------- nextflow


def _parse_nextflow(root: Path) -> dict:
    flags = []
    schema_path = root / "nextflow_schema.json"
    if not schema_path.is_file():
        flags.append({
            "code": "NO_SCHEMA",
            "detail": ("This pipeline has no nextflow_schema.json, so parameters must be "
                       "read from the nextflow.config params block and the source. "
                       "Grouping and types will be lower confidence."),
        })
        params, groups = _params_from_config(root, flags)
        confidence = "low"
    else:
        try:
            schema = json.loads(schema_path.read_text())
        except ValueError as exc:
            raise errors.InvalidQuery(
                f"nextflow_schema.json is not valid JSON: {exc}",
                what="The workflow's own schema is malformed.",
                retry_with=[{"note": "report this upstream; parse from config instead"}],
            )
        params, groups = _params_from_schema(schema, flags)
        confidence = "high"

    engine_version = None
    config = root / "nextflow.config"
    if config.is_file():
        match = re.search(r"nextflowVersion\s*=\s*['\"]([^'\"]+)['\"]",
                          config.read_text(errors="replace"))
        if match:
            engine_version = match.group(1)

    return {
        "engine": NEXTFLOW,
        "entrypoint": "main.nf" if (root / "main.nf").is_file() else None,
        "declared_engine_version": engine_version,
        "parse_confidence": confidence,
        "groups": groups,
        "params": params,
        "tables": _tables(root, params, flags),
        "outputs": _nextflow_outputs(root, flags),
        "test_profiles": sorted(p.name for p in (root / "conf").glob("test*.config"))
        if (root / "conf").is_dir() else [],
        "flags": flags,
    }


def _params_from_schema(schema: dict, flags: list) -> tuple:
    blocks = schema.get("$defs") or schema.get("definitions") or {}
    ungrouped = not blocks
    if ungrouped:
        blocks = {"general": {"title": "Parameters", "properties":
                              schema.get("properties") or {}}}
        flags.append({
            "code": "UNGROUPED_SCHEMA",
            "detail": ("The schema declares no groups, so every parameter falls into a "
                       "single 'general' group. The form will be one flat section."),
        })

    params, groups = [], []
    for key, block in blocks.items():
        properties = block.get("properties") or {}
        required = set(block.get("required") or [])
        visible = 0
        for name, spec in properties.items():
            if not isinstance(spec, dict):
                continue
            entry = _param_entry(name, spec, key, name in required)
            if not entry["hidden"]:
                visible += 1
            params.append(entry)
        groups.append({"key": key, "title": block.get("title") or key,
                       "description": block.get("description"),
                       "n_params": len(properties), "n_visible": visible})

    # Top-level properties sit outside every group; nf-core uses them for engine knobs.
    # Skipped when the fallback above already consumed them as the synthesized group,
    # which otherwise counted every parameter twice.
    for name, spec in ({} if ungrouped else (schema.get("properties") or {})).items():
        if not isinstance(spec, dict):
            continue
        params.append(_param_entry(name, spec, "_top_level",
                                   name in set(schema.get("required") or [])))
    return params, groups


def _param_entry(name: str, spec: dict, group: str, required: bool) -> dict:
    unknown = sorted(set(spec) - KNOWN_PARAM_KEYS)
    table_evidence = _table_evidence(spec)
    entry = {
        "name": name,
        "group": group,
        "type": spec.get("type"),
        "description": spec.get("description"),
        "help_text": spec.get("help_text"),
        "title": spec.get("title"),
        "format": spec.get("format"),
        "enum": spec.get("enum"),
        "minimum": spec.get("minimum"),
        "maximum": spec.get("maximum"),
        "pattern": spec.get("pattern"),
        # Absence of a default is the must-set signal, so distinguish it from a null
        # default rather than collapsing both to None.
        "has_default": "default" in spec,
        "default": spec.get("default"),
        "hidden": bool(spec.get("hidden")),
        "declared_required": required,
        "table_evidence": table_evidence,
        "is_path": spec.get("format") in PATH_FORMATS or bool(table_evidence),
        "is_infrastructure": name in INFRA_NAMES,
    }
    # nf-schema's pointer to the samplesheet schema a path parameter reads.
    if spec.get("schema"):
        entry["schema"] = spec["schema"]
    if unknown:
        entry["unknown_keys"] = unknown
    return entry


def _table_evidence(spec: dict) -> Optional[str]:
    """Why a parameter names a table the workflow parses, or None if nothing says so."""
    if spec.get("schema"):
        return f"it declares the schema of its rows in {spec['schema']}"
    if spec.get("mimetype") in TABLE_MIMETYPES:
        return f"its mimetype is {spec['mimetype']}"
    if isinstance(spec.get("pattern"), str) and TABLE_IN_PATTERN.search(spec["pattern"]):
        return f"its pattern {spec['pattern']!r} names a CSV or TSV"
    if isinstance(spec.get("default"), str) and TABLE_SUFFIX.search(spec["default"]):
        return f"its default {spec['default']!r} is a CSV or TSV"
    return None


def _params_from_config(root: Path, flags: list) -> tuple:
    """Fallback: the params block in nextflow.config."""
    config = root / "nextflow.config"
    params = []
    if not config.is_file():
        flags.append({"code": "NO_PARAMS_SOURCE",
                      "detail": "Neither nextflow_schema.json nor nextflow.config exists."})
        return params, []
    text = config.read_text(errors="replace")
    match = re.search(r"\bparams\s*\{", text)
    if not match:
        flags.append({"code": "NO_PARAMS_BLOCK",
                      "detail": "nextflow.config declares no params block."})
        return params, []
    depth, close = 0, None
    for index in range(match.end() - 1, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                close = index
                break
    if close is None:
        flags.append({
            "code": "PARAMS_BLOCK_UNTERMINATED",
            "detail": ("The params block in nextflow.config has no closing brace, so no "
                       "parameters could be read from it. The file is probably truncated."),
        })
        return params, []
    body = text[match.end():close]
    for line in body.splitlines():
        line = line.split("//")[0].strip()
        assignment = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$", line)
        if not assignment:
            continue
        name, raw = assignment.group(1), assignment.group(2).rstrip(",")
        table_evidence = _table_evidence({"default": _literal(raw)})
        params.append({
            "name": name, "group": "general", "type": _infer_type(raw),
            "description": None, "help_text": None, "title": None, "format": None,
            "enum": None, "minimum": None, "maximum": None, "pattern": None,
            "has_default": True, "default": _literal(raw), "hidden": False,
            "declared_required": False, "table_evidence": table_evidence,
            "is_path": bool(table_evidence), "is_infrastructure": name in INFRA_NAMES,
        })
    flags.append({
        "code": "PARAMS_FROM_CONFIG",
        "detail": (f"{len(params)} parameters were read from the nextflow.config params "
                   "block. Types are inferred from the default literal and there are no "
                   "descriptions or groups -- verify each before building a form."),
    })
    return params, [{"key": "general", "title": "Parameters", "description": None,
                     "n_params": len(params), "n_visible": len(params)}]


def _infer_type(raw: str) -> str:
    text = raw.strip()
    if text in ("true", "false"):
        return "boolean"
    if re.fullmatch(r"-?\d+", text):
        return "integer"
    if re.fullmatch(r"-?\d*\.\d+", text):
        return "number"
    return "string"


def _literal(raw: str):
    text = raw.strip().strip("'\"")
    if text in ("true", "false"):
        return text == "true"
    if text == "null":
        return None
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def _tabular(entry: dict) -> bool:
    return bool(entry.get("table_evidence")) or (
        entry["is_path"] and "sheet" in entry["name"].lower())


def _tables(root: Path, params: list, flags: list) -> dict:
    """Each tabular parameter's columns, read from the schema that parameter names.

    {param: {"columns_from", "columns", "assumed"?}}. A parameter naming no schema takes
    DEFAULT_TABLE_SCHEMA when it is the only such parameter and nothing else names that
    file; `assumed` says the pairing is a guess.
    """
    tables = {}
    for entry in params:
        if entry.get("schema"):
            columns = _table_columns(root, entry["schema"], flags)
            if columns is not None:
                tables[entry["name"]] = {"columns_from": entry["schema"],
                                         "columns": columns}
    named = {Path(e["schema"].lstrip("/")) for e in params if e.get("schema")}
    unnamed = [e for e in params if _tabular(e) and not e.get("schema")]
    if len(unnamed) == 1 and Path(DEFAULT_TABLE_SCHEMA) not in named \
            and (root / DEFAULT_TABLE_SCHEMA).is_file():
        columns = _table_columns(root, DEFAULT_TABLE_SCHEMA, flags)
        if columns is not None:
            tables[unnamed[0]["name"]] = {"columns_from": DEFAULT_TABLE_SCHEMA,
                                          "columns": columns, "assumed": True}
    return tables


def _table_columns(root: Path, relative: str, flags: list) -> Optional[list]:
    base = root.resolve()
    schema = (base / relative.lstrip("/")).resolve()
    if not schema.is_relative_to(base) or not schema.is_file():
        flags.append({"code": "SAMPLESHEET_SCHEMA_MISSING",
                      "detail": f"{relative} is named as a samplesheet schema but is not "
                                "a file in the repository."})
        return None
    try:
        doc = json.loads(schema.read_text())
    except ValueError:
        flags.append({"code": "SAMPLESHEET_SCHEMA_UNREADABLE",
                      "detail": f"{relative} is not valid JSON."})
        return None
    items = doc.get("items") or {}
    required = set(items.get("required") or [])
    columns = []
    for name, spec in (items.get("properties") or {}).items():
        spec = spec or {}
        column = {"name": name, "type": _column_type(spec), "required": name in required}
        column.update({ours: spec[theirs] for theirs, ours in COLUMN_KEYS
                       if theirs in spec})
        # A structured input's cell holds one value, so such a column cannot be built
        # in the run form as it stands.
        if column["type"] in ("object", "array") or "items" in spec:
            column["unsupported"] = "holds more than one value"
        columns.append(column)
    return columns


def _column_type(spec: dict) -> str:
    """A schema column's kind in the design spec's vocabulary (formgen.COLUMN_TYPES)."""
    if spec.get("format") in COLUMN_TYPE_BY_FORMAT:
        return COLUMN_TYPE_BY_FORMAT[spec["format"]]
    declared = spec.get("type") or "string"
    if isinstance(declared, list):
        # A cell that may be empty says ["integer", "null"]; one taking either of two
        # scalars can only be text.
        scalars = [t for t in declared if t != "null"]
        return scalars[0] if len(scalars) == 1 else "string"
    return declared


def _nextflow_outputs(root: Path, flags: list) -> list:
    """Prefer a machine-readable output definition over prose."""
    definition = root / "output_definition.json"
    if definition.is_file():
        try:
            doc = json.loads(definition.read_text())
        except ValueError:
            doc = None
        if doc:
            files = (doc.get("files") or doc) if isinstance(doc, dict) else {}
            outputs = []
            for label, spec in (files.items() if isinstance(files, dict) else []):
                if not isinstance(spec, dict):
                    continue
                outputs.append({
                    "path": spec.get("filepath") or label,
                    "description": spec.get("title") or spec.get("description"),
                    "optional": bool(spec.get("optional")),
                    "source": "output_definition.json",
                })
            if outputs:
                return outputs
    if (root / "docs" / "output.md").is_file():
        flags.append({
            "code": "OUTPUTS_FROM_PROSE",
            "detail": ("No machine-readable output definition; docs/output.md exists but "
                       "must be read by hand to enumerate expected outputs."),
        })
    else:
        flags.append({"code": "NO_OUTPUT_DEFINITION",
                      "detail": "The workflow documents no outputs; derive them from a run."})
    return []


# ----------------------------------------------------------------------------- wdl


def _parse_wdl(root: Path) -> dict:
    """Best-effort only, and says so."""
    candidates = sorted(root.glob("*.wdl")) + sorted(root.glob("workflows/*.wdl"))
    imported = set()
    for path in candidates:
        for match in re.finditer(r'^\s*import\s+"([^"]+)"',
                                 path.read_text(errors="replace"), re.M):
            imported.add(Path(match.group(1)).name)
    entrypoints = [p for p in candidates if p.name not in imported]
    flags = [{
        "code": "WDL_TEXT_PARSE",
        "detail": ("WDL is parsed by regex over the top-level blocks, not by a real WDL "
                   "parser. Struct-typed and expression-default inputs are unreliable; "
                   "verify every input by hand before configuring."),
    }]
    if len(entrypoints) != 1:
        flags.append({
            "code": "AMBIGUOUS_ENTRYPOINT",
            "detail": (f"{len(entrypoints)} candidate top-level .wdl files "
                       f"({[p.name for p in entrypoints][:6]}); confirm which one Cirro "
                       "should invoke."),
        })
    return {
        "engine": CROMWELL,
        "entrypoint": entrypoints[0].name if len(entrypoints) == 1 else None,
        "declared_engine_version": None,
        "parse_confidence": "low",
        "groups": [{"key": "general", "title": "Parameters", "description": None,
                    "n_params": 0, "n_visible": 0}],
        "params": [],
        "tables": {},
        "outputs": [],
        "test_profiles": [],
        "flags": flags + [{
            "code": "WDL_NOT_IMPLEMENTED",
            "detail": ("Structured WDL input extraction is not implemented in this tool "
                       "yet. Read the workflow's input {} and parameter_meta {} blocks "
                       "directly."),
        }],
    }


# ---------------------------------------------------------------- candidate hints


def candidates(params: list, tables: dict) -> dict:
    """Suggest classifications, each with its reason. Never decide.

    `tables` is the parse's columns per tabular parameter (see `_tables`).
    """
    primary, managed, pickers, infra = [], [], [], []
    structured = []
    for entry in params:
        name, low = entry["name"], entry["name"].lower()
        if entry["is_infrastructure"]:
            infra.append({"name": name, "why": "a known engine or platform knob"})
            continue
        if low in OUTPUT_DIR_NAMES or (entry.get("format") == "directory-path"
                                       and "out" in low):
            managed.append({
                "name": name,
                "why": ("names the output directory, which Cirro supplies at launch"
                        f" (format={entry.get('format')}, default={entry.get('default')!r})"),
            })
            continue
        if not entry["is_path"]:
            continue
        if _tabular(entry):
            structured.append(_structured_hint(entry, tables.get(name)))
        elif low in PRIMARY_INPUT_NAMES and entry["declared_required"]:
            primary.append({
                "name": name,
                "why": (f"a required path input named {name!r}, which conventionally "
                        "carries the dataset's own data"),
            })
        else:
            pickers.append({
                "name": name,
                "why": (f"a path input (format={entry.get('format')}) that is not the "
                        "primary data, so a user selects it from a dataset or reference"),
                "required": entry["declared_required"],
            })
    return {"primary_data_input": primary, "managed_output_dir": managed,
            "file_pickers": pickers, "structured_input": structured,
            "infrastructure": infra}


def _structured_hint(entry: dict, table: Optional[dict]) -> dict:
    evidence = entry.get("table_evidence") or "its name denotes a sample sheet"
    why = (f"{evidence}, so it is a table the workflow reads: the run form builds it as "
           "a structured input and Cirro writes the file")
    if table is None:
        why += (" -- no schema declares its columns, so read them from the code that "
                "parses it and confirm them with the person")
    elif table.get("assumed"):
        why += (f" -- its columns are taken from {table['columns_from']}, which no "
                "parameter names; confirm against the code that parses it")
    if entry["declared_required"]:
        why += "; it is required, so it is likely the primary input"
    hint = {"name": entry["name"], "why": why}
    if table is not None:
        hint.update({"columns_from": table["columns_from"], "columns": table["columns"]})
    return hint

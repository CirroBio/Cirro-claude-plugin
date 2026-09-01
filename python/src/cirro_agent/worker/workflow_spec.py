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

from .. import errors

NEXTFLOW = "NEXTFLOW"
CROMWELL = "CROMWELL"

# nf-core / epi2me schema keys this parser understands. Anything else on a property is
# reported under `unknown_keys` rather than dropped silently.
KNOWN_PARAM_KEYS = {
    "type", "description", "default", "help_text", "hidden", "title", "format", "enum",
    "fa_icon", "minimum", "maximum", "pattern", "mimetype", "exists", "schema",
    "overrides", "errorMessage", "enumNames", "items", "uniqueItems",
}

# JSON-Schema `format` values that mean "this is a path, not a scalar".
PATH_FORMATS = {"path", "file-path", "directory-path", "file", "directory"}

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


def _normalize_repo(target: str) -> tuple:
    text = target.strip().rstrip("/")
    if text.startswith(("http://", "https://", "git@")):
        slug = re.sub(r"^.*[:/]([^/]+/[^/]+?)(\.git)?$", r"\1", text)
        return text, slug
    if "/" in text:
        return f"https://github.com/{text}.git", text
    # A bare name is an nf-core pipeline by convention.
    return f"https://github.com/nf-core/{text}.git", f"nf-core/{text}"


def _git_identity(path: Path) -> dict:
    out = {}
    for key, args in (("version", ["describe", "--tags", "--always"]),
                      ("commit", ["rev-parse", "HEAD"])):
        try:
            done = subprocess.run(["git", "-C", str(path)] + args,
                                  capture_output=True, text=True, timeout=30)
            if done.returncode == 0:
                out[key] = done.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        done = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=30)
        if done.returncode == 0 and done.stdout.strip():
            out["repository"] = _normalize_repo(done.stdout.strip())[1]
    except (OSError, subprocess.SubprocessError):
        pass
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
        "samplesheet_columns": _samplesheet_columns(root, flags),
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
        "is_path": (spec.get("format") in PATH_FORMATS),
        "is_infrastructure": name in INFRA_NAMES,
    }
    if unknown:
        entry["unknown_keys"] = unknown
    return entry


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
    depth = 0
    for index in range(match.end() - 1, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                break
    body = text[match.end():index]
    for line in body.splitlines():
        line = line.split("//")[0].strip()
        assignment = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$", line)
        if not assignment:
            continue
        name, raw = assignment.group(1), assignment.group(2).rstrip(",")
        params.append({
            "name": name, "group": "general", "type": _infer_type(raw),
            "description": None, "help_text": None, "title": None, "format": None,
            "enum": None, "minimum": None, "maximum": None, "pattern": None,
            "has_default": True, "default": _literal(raw), "hidden": False,
            "declared_required": False,
            "is_path": False, "is_infrastructure": name in INFRA_NAMES,
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


def _samplesheet_columns(root: Path, flags: list) -> list:
    schema = root / "assets" / "schema_input.json"
    if not schema.is_file():
        return []
    try:
        doc = json.loads(schema.read_text())
    except ValueError:
        flags.append({"code": "SAMPLESHEET_SCHEMA_UNREADABLE",
                      "detail": "assets/schema_input.json is not valid JSON."})
        return []
    items = doc.get("items") or {}
    required = set(items.get("required") or [])
    return [{"name": name, "required": name in required,
             "description": (spec or {}).get("description"),
             "type": (spec or {}).get("type")}
            for name, spec in (items.get("properties") or {}).items()]


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
        "samplesheet_columns": [],
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


def candidates(params: list) -> dict:
    """Suggest classifications, each with its reason. Never decide."""
    primary, managed, pickers, samplesheets, infra = [], [], [], [], []
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
        if "sheet" in low:
            samplesheets.append({"name": name, "why": "the name denotes a sample sheet"})
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
            "file_pickers": pickers, "samplesheet": samplesheets,
            "infrastructure": infra}

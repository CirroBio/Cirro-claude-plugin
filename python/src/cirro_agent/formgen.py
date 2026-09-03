"""Turn a design spec into the configuration assets Cirro renders and runs.

Deterministic: the judgment lives upstream, in classifying inputs and parameters when
the spec is written. Given a spec, the asset set follows from it.

Three conventions the spec encodes and this module obeys:
  * An input with no `group` is the primary data input. It is not a form field; it maps
    to `$.inputs[0].dataPath`, the selected dataset's data folder.
  * `cirro.managed_parameters` are supplied by Cirro at launch and never rendered. The
    output directory maps to `$.dataset.dataPath`.
  * Everything else is a form field under its `group`, and reads back as
    `$.dataset.params.<group>.<field>`.

No `process-definition.json`: under every configuration source the process record is
created through the API, and only these assets vary by destination.
"""
import json
import re
from typing import Optional

FORM = "process-form.json"
INPUT = "process-input.json"
OUTPUT = "process-output.json"
COMPUTE = "process-compute.config"
PREPROCESS = "preprocess.py"
DOCUMENTATION = "README.md"

# Which library a picker resolves from. A genome or annotation belongs to the shared
# reference library; anything sample-specific comes from the input dataset.
REFERENCE_HINTS = ("genome", "reference", "ref", "annotation", "index", "gtf", "gff",
                   "fasta", "dbsnp", "known_sites", "intervals", "tr_bed")

# Globs by file kind, matched against the input's name and description.
GLOBS = (
    (("fasta", "genome", "\\bref\\b"), "**/*.{fa,fasta,fna}{,.gz}"),
    (("gtf", "gff", "annotation"), "**/*.{gtf,gff,gff3}{,.gz}"),
    (("bed",), "**/*.bed"),
    (("vcf",), "**/*.vcf{,.gz}"),
    (("bam", "cram"), "**/*.{bam,cram}"),
    (("fastq", "reads"), "**/*.{fastq,fq}{,.gz}"),
    (("samplesheet", "sample sheet", "csv"), "**/*.csv"),
    (("tsv",), "**/*.tsv"),
    (("json",), "**/*.json"),
)

DEFAULT_COMPUTE = """\
// Compute configuration for {name} {version}.
// Executor, queue and instance sizing come from the Cirro compute environment chosen
// at launch; the workflow's own config declares per-process cpu and memory requests.

process {{
    errorStrategy = {{ task.exitStatus in [143, 137, 104, 134, 139] ? 'retry' : 'finish' }}
    maxRetries = 2
}}
"""


def humanize(name: str) -> str:
    return re.sub(r"[_\-]+", " ", name).strip().capitalize()


def infer_path_type(entry: dict) -> str:
    haystack = f"{entry.get('name', '')} {entry.get('description', '')}".lower()
    return "references" if any(h in haystack for h in REFERENCE_HINTS) else "dataset"


def infer_glob(entry: dict) -> str:
    haystack = f"{entry.get('name', '')} {entry.get('description', '')}".lower()
    for needles, glob in GLOBS:
        for needle in needles:
            if re.search(needle, haystack):
                return glob
    return "**/*"


def split_inputs(spec: dict) -> tuple:
    """(primary, pickers) -- the primary is the input carrying no form group."""
    primary, pickers = None, []
    for entry in spec.get("inputs") or []:
        if entry.get("group"):
            pickers.append(entry)
        elif primary is None:
            primary = entry
        else:
            # More than one ungrouped input: only the first can be $.inputs[0].dataPath.
            pickers.append({**entry, "group": "input_options"})
    return primary, pickers


MANAGED_PATHS = {"output_directory": "$.dataset.dataPath"}


def managed_names(spec: dict) -> dict:
    return {m["name"]: m.get("role", "output_directory")
            for m in ((spec.get("cirro") or {}).get("managed_parameters") or [])}


def _field_for_parameter(entry: dict) -> dict:
    declared = entry.get("type") or "string"
    # `enum` is a design-spec type, not a JSON Schema one: the schema type stays
    # whatever the values actually are, or a numeric dropdown renders as text.
    field: dict = {"type": "string" if declared == "enum" else declared}
    if entry.get("choices"):
        values = list(entry["choices"])
        field["enum"] = values
        if declared == "enum":
            if all(isinstance(v, bool) for v in values):
                field["type"] = "boolean"
            elif all(isinstance(v, int) for v in values):
                field["type"] = "integer"
            elif all(isinstance(v, (int, float)) for v in values):
                field["type"] = "number"
    field["title"] = entry.get("title") or humanize(entry["name"])
    if entry.get("description"):
        field["description"] = entry["description"]
    if entry.get("help_text"):
        field["help_text"] = entry["help_text"]
    if "default" in entry and entry["default"] is not None:
        field["default"] = entry["default"]
    for key in ("minimum", "maximum", "pattern"):
        if entry.get(key) is not None:
            field[key] = entry[key]
    if entry.get("type") == "array":
        field["type"] = "array"
        items: dict = {"type": entry.get("items") or "string"}
        if entry.get("choices"):
            items["enum"] = list(entry["choices"])
            field.pop("enum", None)
        field["items"] = items
        field["uniqueItems"] = True
    return field


def _field_for_picker(entry: dict) -> dict:
    field = {
        "type": "string",
        "title": entry.get("title") or humanize(entry["name"]),
        "pathType": entry.get("path_type") or infer_path_type(entry),
        "file": entry.get("glob") or infer_glob(entry),
    }
    if entry.get("description"):
        field["description"] = entry["description"]
    if entry.get("multiple"):
        field["multiple"] = True
    return field


WHEN_EQUALS = re.compile(r"^\s*(\w+)\s*(?:==|=|is)\s*(.+?)\s*$")
WHEN_PROVIDED = re.compile(r"^\s*(\w+)\s+is\s+provided\s*$", re.I)


def parse_when(predicate: str) -> Optional[tuple]:
    """`mode == image` -> ("mode", "image"). Returns None if unparseable.

    The design spec calls `when` a plain-language predicate interpreted by the
    consumer, so anything this cannot read is left as an always-visible field rather
    than guessed at.
    """
    if not predicate:
        return None
    provided = WHEN_PROVIDED.match(predicate)
    if provided:
        return provided.group(1), True
    match = WHEN_EQUALS.match(predicate)
    if not match:
        return None
    selector, raw = match.group(1), match.group(2).strip().strip("\'\"")
    if raw.lower() in ("true", "yes", "enabled"):
        return selector, True
    if raw.lower() in ("false", "no", "disabled"):
        return selector, False
    for cast in (int, float):
        try:
            return selector, cast(raw)
        except ValueError:
            pass
    return selector, raw


def _conditional(selector_field: dict, selector: str, enabling, revealed: dict) -> dict:
    """The shape Cirro's renderer expects for a conditionally-shown field.

    Two branches: one where the selector holds the enabling value and the extra fields
    appear, one where it does not and they are absent.
    """
    if selector_field.get("type") == "boolean":
        others = [not bool(enabling)]
    else:
        values = list(selector_field.get("enum") or [])
        others = [v for v in values if v != enabling]
    branches = [{"properties": {selector: {"enum": [enabling]}, **revealed}}]
    if others:
        branches.append({"properties": {selector: {"enum": others}}})
    return {"oneOf": branches}


def build_form(spec: dict) -> dict:
    """One object per group, in the order the groups first appear."""
    _primary, pickers = split_inputs(spec)
    managed = managed_names(spec)

    groups: dict = {}
    required: dict = {}

    def ensure(group: str) -> dict:
        if group not in groups:
            groups[group] = {"type": "object", "title": humanize(group), "properties": {}}
            required[group] = []
        return groups[group]

    for entry in pickers:
        group = ensure(entry["group"])
        group["properties"][entry["name"]] = _field_for_picker(entry)
        if entry.get("required"):
            required[entry["group"]].append(entry["name"])

    conditional: dict = {}
    for entry in spec.get("parameters") or []:
        if entry["name"] in managed or entry.get("hidden"):
            continue
        group_name = entry.get("group") or "general"
        group = ensure(group_name)
        parsed = parse_when(entry.get("when") or "")
        if parsed:
            # Held back until the selector is placed, so the branch can carry the
            # selector's own value set.
            conditional.setdefault((group_name, parsed[0]), []).append((parsed[1], entry))
            continue
        group["properties"][entry["name"]] = _field_for_parameter(entry)
        # required-with-a-default is pre-filled and safe; only a must-set field is
        # marked required so the form does not block on values Cirro already has.
        if entry.get("required") and "default" not in entry:
            required[entry.get("group") or "general"].append(entry["name"])

    for (group_name, selector), pending in conditional.items():
        block = groups.get(group_name)
        selector_field = (block or {}).get("properties", {}).get(selector)
        if block is None or selector_field is None:
            # The selector is not a field in this group, so the condition cannot be
            # expressed; show the fields unconditionally rather than losing them.
            block = ensure(group_name)
            for _value, entry in pending:
                block["properties"][entry["name"]] = _field_for_parameter(entry)
            continue
        by_value: dict = {}
        for value, entry in pending:
            by_value.setdefault(value, {})[entry["name"]] = _field_for_parameter(entry)
        dependencies = block.setdefault("dependencies", {})
        for value, revealed in by_value.items():
            existing = dependencies.get(selector)
            branch = _conditional(selector_field, selector, value, revealed)
            if existing is None:
                dependencies[selector] = branch
            else:
                existing["oneOf"] = branch["oneOf"][:1] + existing["oneOf"]

    for name, block in groups.items():
        if required.get(name):
            block["required"] = required[name]

    return {"ui": {}, "form": {"type": "object", "properties": groups}}


def build_input_map(spec: dict) -> dict:
    primary, pickers = split_inputs(spec)
    managed = managed_names(spec)
    mapping: dict = {}

    if primary:
        mapping[primary["name"]] = "$.inputs[0].dataPath"
    # Every managed parameter Cirro supplies today is the output location, so every
    # role maps to the dataset path. Keyed by role so a second role has a place to
    # land, rather than a conditional whose branches were identical.
    for name, role in managed.items():
        mapping[name] = MANAGED_PATHS[role]
    for entry in pickers:
        mapping[entry["name"]] = f"$.dataset.params.{entry['group']}.{entry['name']}"
    for entry in spec.get("parameters") or []:
        if entry["name"] in managed or entry.get("hidden"):
            continue
        group = entry.get("group") or "general"
        mapping[entry["name"]] = f"$.dataset.params.{group}.{entry['name']}"
    return mapping


def build_output(spec: dict) -> dict:
    """Only web-optimization steps go here; most pipelines need none."""
    return {"commands": (spec.get("cirro") or {}).get("output_commands") or []}


def build_compute(spec: dict) -> str:
    supplied = (spec.get("compute") or {}).get("config")
    if supplied:
        return supplied if supplied.endswith("\n") else supplied + "\n"
    workflow = spec.get("workflow") or {}
    return DEFAULT_COMPUTE.format(name=workflow.get("name", "the workflow"),
                                  version=workflow.get("version", ""))


def build_documentation(spec: dict) -> str:
    workflow = spec.get("workflow") or {}
    lines = [f"# {workflow.get('name', 'Pipeline')} {workflow.get('version', '')}".strip(),
             ""]
    if workflow.get("repository"):
        lines += [f"Source: {workflow['repository']} at `{workflow.get('version')}`", ""]
    inputs = spec.get("inputs") or []
    if inputs:
        lines += ["## Inputs", ""]
        for entry in inputs:
            lines.append(f"- **{entry['name']}** — {entry.get('description', '')}")
        lines.append("")
    constraints = spec.get("constraints") or []
    if constraints:
        lines += ["## Requirements on your input data", ""]
        for entry in constraints:
            text = " ".join((entry.get("description") or "").split())
            lines.append(f"- **{entry.get('applies_to')}** — {text}")
        lines.append("")
    return "\n".join(lines)


def generate(spec: dict, *,
             preprocess: Optional[str] = None) -> dict:
    """Return {filename: text} for the whole asset set."""
    assets = {
        FORM: json.dumps(build_form(spec), indent=2) + "\n",
        INPUT: json.dumps(build_input_map(spec), indent=2) + "\n",
        OUTPUT: json.dumps(build_output(spec), indent=2) + "\n",
        COMPUTE: build_compute(spec),
    }
    if preprocess:
        assets[PREPROCESS] = preprocess
    assets[DOCUMENTATION] = build_documentation(spec)
    return assets


# Filename -> the ProcessResource slot it uploads to.
SLOTS = {FORM: "form", INPUT: "input", OUTPUT: "output", COMPUTE: "compute",
         PREPROCESS: "preprocess", DOCUMENTATION: "documentation"}

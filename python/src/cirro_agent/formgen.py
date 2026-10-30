"""Turn a design spec into the configuration assets Cirro renders and runs.

Deterministic: the judgment lives upstream, in classifying inputs and parameters when
the spec is written. Given a spec, the asset set follows from it.

Three conventions the spec encodes and this module obeys:
  * An input with no `group` is the primary data input.
  * `cirro.managed_parameters` are supplied by Cirro at launch and never rendered. The
    output directory maps to `$.dataset.dataPath`.
  * Everything else is a form field under its `group`, and reads back as
    `$.dataset.params.<group>.<field>`.

`cirro.input_mode` picks between the portal's two ways of running a form, which one form
cannot mix (see `formcheck.PATH_FORMATS`):
  * `select`, the default. The user chooses the input datasets first; the primary input
    is not a form field and maps to `$.inputs[0].dataPath`, and pickers offer files
    from the chosen datasets through `pathType` and a `file` glob.
  * `browse`. The user picks paths from the whole project and the run's inputs are the
    datasets those paths point into, so the primary input is a form field too. Pickers
    use nf-schema's `format` and `pattern`, and an input carrying `structured` becomes
    a samplesheet the user fills in (`structuredInputs`).

No `process-definition.json`: under every configuration source the process record is
created through the API, and only these assets vary by destination.
"""
import json
import re
from typing import Optional

from . import errors, formcheck

FORM = "process-form.json"
INPUT = "process-input.json"
OUTPUT = "process-output.json"
COMPUTE = "process-compute.config"
PREPROCESS = "preprocess.py"
DOCUMENTATION = "README.md"

# The one declaration of the ProcessResource slot vocabulary: every slot a process's
# configuration can occupy in Cirro storage, and the filename each is written as.
#
# This mirrors `cirro_api_client.v1.models.ProcessResource` minus its UNKNOWN fallback,
# and is mirrored rather than imported because both this module and the CLI parser that
# offers these as `--slot` choices must stay free of the SDK -- importing it would pull
# the whole generated model package into the parent process on every `cirro-agent
# --help`. The enum also maps an unrecognized value onto UNKNOWN instead of raising, so
# a drifted mirror would fail silently on the wire; test_pipeline_config checks it.
_SLOT_FILES = {"compute": COMPUTE, "documentation": DOCUMENTATION, "form": FORM,
               "input": INPUT, "output": OUTPUT, "preprocess": PREPROCESS}

CONFIG_SLOTS = tuple(_SLOT_FILES)

# Filename -> the ProcessResource slot it uploads to.
SLOTS = {filename: slot for slot, filename in _SLOT_FILES.items()}

# Slots holding a JSON document rather than free text.
JSON_SLOTS = ("form", "input", "output")

# Which library a picker resolves from. A genome or annotation belongs to the shared
# reference library; anything sample-specific comes from the input dataset.
REFERENCE_HINTS = ("genome", "reference", "ref", "annotation", "index", "gtf", "gff",
                   "fasta", "dbsnp", "known_sites", "intervals", "tr_bed")

# File kinds, matched against the input's name and description: the `file` glob a
# select-mode picker narrows with, and the `pattern` a browse-mode one does. A pattern
# is matched against a file's path within its dataset, anywhere in it.
GLOBS = (
    (("fasta", "genome", "\\bref\\b"), "**/*.{fa,fasta,fna}{,.gz}",
     r"\.(fa|fasta|fna)(\.gz)?$"),
    (("gtf", "gff", "annotation"), "**/*.{gtf,gff,gff3}{,.gz}",
     r"\.(gtf|gff|gff3)(\.gz)?$"),
    (("bed",), "**/*.bed", r"\.bed$"),
    (("vcf",), "**/*.vcf{,.gz}", r"\.vcf(\.gz)?$"),
    (("bam", "cram"), "**/*.{bam,cram}", r"\.(bam|cram)$"),
    (("fastq", "reads"), "**/*.{fastq,fq}{,.gz}", r"\.(fastq|fq)(\.gz)?$"),
    (("samplesheet", "sample sheet", "csv"), "**/*.csv", r"\.csv$"),
    (("tsv",), "**/*.tsv", r"\.tsv$"),
    (("json",), "**/*.json", r"\.json$"),
)

SELECT, BROWSE = "select", "browse"
INPUT_MODES = (SELECT, BROWSE)

# A browse-mode picker's `format`, from the input's `type`.
PATH_FORMAT_BY_TYPE = {"file": "file-path", "folder": "directory-path", "path": "path"}

# The group a browse-mode primary input is shown in. It has none of its own: having none
# is what marks it as the primary.
PRIMARY_GROUP = "input"

# Design-spec column keys and the JSON Schema keywords they become.
COLUMN_KEYWORDS = (("title", "title"), ("description", "description"),
                   ("format", "format"), ("pattern", "pattern"), ("enum", "enum"),
                   ("choices", "enum"), ("minimum", "minimum"), ("maximum", "maximum"),
                   ("error_message", "errorMessage"))

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


GROUP_KEY = re.compile(r"[A-Za-z0-9_\-]+")


def group_key(name: str) -> str:
    """The form key for a spec group, which is also a segment of its JSONPath.

    A spec can name a group by its nf-schema title ("Process skipping options")
    rather than its key, and Cirro cannot read a path segment holding a space: the
    run fails at launch. A name that is already a key is returned unchanged, so a
    regenerated form never moves a group that worked.
    """
    if GROUP_KEY.fullmatch(name):
        return name
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower() or "general"


def group_title(name: str) -> str:
    return humanize(name) if GROUP_KEY.fullmatch(name) else name.strip()


def infer_path_type(entry: dict) -> str:
    haystack = f"{entry.get('name', '')} {entry.get('description', '')}".lower()
    return "references" if any(h in haystack for h in REFERENCE_HINTS) else "dataset"


def _file_kind(entry: dict) -> Optional[tuple]:
    haystack = f"{entry.get('name', '')} {entry.get('description', '')}".lower()
    for kind in GLOBS:
        if any(re.search(needle, haystack) for needle in kind[0]):
            return kind
    return None


def infer_glob(entry: dict) -> str:
    kind = _file_kind(entry)
    return kind[1] if kind else "**/*"


def infer_pattern(entry: dict) -> Optional[str]:
    kind = _file_kind(entry)
    return kind[2] if kind else None


def input_mode(spec: dict) -> str:
    """The spec's input mode, refused if it is unknown or cannot hold the spec's inputs.

    A structured input puts the portal in browse mode whatever else the form says, so a
    select-mode spec with one describes a form that would not behave as written.
    """
    mode = (spec.get("cirro") or {}).get("input_mode") or SELECT
    if mode not in INPUT_MODES:
        raise errors.InvalidQuery(
            f"cirro.input_mode is {mode!r}; it must be one of {', '.join(INPUT_MODES)}.",
            what="select keeps the portal's input-dataset selector; browse picks paths "
                 "from the whole project and is the only mode with samplesheets.",
            field="cirro.input_mode", given=mode, candidates=list(INPUT_MODES),
            user_action="Correct input_mode in the design spec.",
        )
    tables = [e["name"] for e in spec.get("inputs") or [] if e.get("structured")]
    if mode == SELECT and tables:
        raise errors.InvalidQuery(
            f"{', '.join(tables)} is a structured input, which needs "
            "cirro.input_mode: browse.",
            what="A samplesheet built in the run form puts the portal in browse mode, "
                 "so the rest of the form has to be written for it.",
            field="cirro.input_mode", given=mode, candidates=[BROWSE],
            user_action="Set cirro.input_mode to browse, or drop `structured` from "
                        "the input.",
        )
    return mode


def structured_name(entry: dict) -> str:
    return entry["structured"].get("name") or entry["name"]


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


def _field_for_browse_picker(entry: dict) -> dict:
    """A picker over the whole project: nf-schema's `format`, narrowed by `pattern`.

    Reference files are not in any dataset, so a reference picker keeps `pathType` and a
    `file` glob, under the singular spelling the docs now use.
    """
    field = {"type": "string", "title": entry.get("title") or humanize(entry["name"])}
    if (entry.get("path_type") or infer_path_type(entry)) in formcheck.REFERENCE_PATH_TYPES:
        field["pathType"] = "reference"
        field["file"] = entry.get("glob") or infer_glob(entry)
    else:
        field["format"] = PATH_FORMAT_BY_TYPE.get(entry.get("type"), "file-path")
        # The pattern is checked against the folder picked as much as a file, so an
        # extension guessed from a folder's name would refuse every folder.
        pattern = entry.get("pattern") or (
            None if field["format"] == "directory-path" else infer_pattern(entry))
        if pattern:
            field["pattern"] = pattern
    if entry.get("description"):
        field["description"] = entry["description"]
    if entry.get("multiple"):
        field["multiple"] = True
    return field


def _field_for_table(entry: dict) -> dict:
    field = {"type": "string", "title": entry.get("title") or humanize(entry["name"]),
             "schema": structured_name(entry)}
    if entry.get("description"):
        field["description"] = entry["description"]
    return field


def build_structured_input(entry: dict) -> dict:
    """A samplesheet's definition: its file format and one schema property per column.

    Columns keep the order the spec lists them in, which is the order the portal shows
    them; the schema's title and description are what the portal shows over the table.
    """
    structured = entry["structured"]
    columns, required = {}, []
    for column in structured.get("columns") or []:
        spec = {"type": column.get("type") or "string"}
        for key, keyword in COLUMN_KEYWORDS:
            if column.get(key) is not None:
                spec[keyword] = column[key]
        columns[column["name"]] = spec
        if column.get("required"):
            required.append(column["name"])
    items = {"type": "object", "properties": columns}
    if required:
        items["required"] = required
    schema = {"title": structured.get("title") or entry.get("title")
              or humanize(entry["name"])}
    description = structured.get("description") or entry.get("description")
    if description:
        schema["description"] = description
    schema.update({"type": "array", "items": items})
    definition = {"fileFormat": str(structured.get("file_format") or "CSV").upper(),
                  "schema": schema}
    if structured.get("default_source_file"):
        definition["defaultSourceFile"] = structured["default_source_file"]
    return definition


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


def _conditional(selector_field: dict, selector: str, by_value: dict) -> dict:
    """The shape Cirro's renderer expects for fields shown by one selector.

    One branch per enabling value, carrying the fields that value reveals, then a
    single branch holding every remaining value where none of them appear.

    The negative branch has to exclude *all* the enabling values at once. Building a
    positive/negative pair per value instead leaves each negative enum still holding
    the other enabling values, so a selector value that enables one field satisfies
    both its own branch and another's negative branch, and `oneOf` matches twice.
    """
    if selector_field.get("type") == "boolean":
        values = [True, False]
    else:
        values = list(selector_field.get("enum") or [])
    branches = [{"properties": {selector: {"enum": [value]}, **revealed}}
                for value, revealed in by_value.items()]
    others = [v for v in values if v not in by_value]
    if others:
        branches.append({"properties": {selector: {"enum": others}}})
    return {"oneOf": branches}


def build_form(spec: dict) -> dict:
    """One object per group, in the order the groups first appear."""
    mode = input_mode(spec)
    primary, pickers = split_inputs(spec)
    managed = managed_names(spec)

    groups: dict = {}
    required: dict = {}
    tables: dict = {}
    titles: dict = {}

    def key_of(name: str) -> str:
        key = group_key(name)
        titles.setdefault(key, group_title(name))
        return key

    def ensure(group: str) -> dict:
        if group not in groups:
            groups[group] = {"type": "object",
                             "title": titles.get(group) or humanize(group),
                             "properties": {}}
            required[group] = []
        return groups[group]

    placed = [(key_of(entry["group"]), entry) for entry in pickers]
    if mode == BROWSE and primary:
        # Nothing selects the dataset up front in browse mode, so the primary input is
        # picked like any other, and the run cannot start without it.
        placed.insert(0, (PRIMARY_GROUP, {**primary, "required": True}))
    for group_name, entry in placed:
        if entry.get("structured"):
            field = _field_for_table(entry)
            tables[structured_name(entry)] = build_structured_input(entry)
        elif mode == BROWSE:
            field = _field_for_browse_picker(entry)
        else:
            field = _field_for_picker(entry)
        ensure(group_name)["properties"][entry["name"]] = field
        if entry.get("required"):
            required[group_name].append(entry["name"])

    conditional: dict = {}
    for entry in spec.get("parameters") or []:
        if entry["name"] in managed or entry.get("hidden"):
            continue
        group_name = key_of(entry.get("group") or "general")
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
            required[group_name].append(entry["name"])

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
        # `conditional` is keyed by (group, selector), so each selector's whole value
        # set is in hand here and the dependency is written once.
        dependencies = block.setdefault("dependencies", {})
        dependencies[selector] = _conditional(selector_field, selector, by_value)

    for name, block in groups.items():
        if required.get(name):
            block["required"] = required[name]

    document = {"ui": {}, "form": {"type": "object", "properties": groups}}
    if tables:
        document["structuredInputs"] = tables
    return document


def _picked_path(entry: dict, group: str) -> str:
    if entry.get("structured"):
        return f"$.structuredInputs.{structured_name(entry)}.path"
    path = f"$.dataset.params.{group}.{entry['name']}"
    # The portal sends a multi-select as a list; `[*]` gives the workflow the
    # comma-joined string it was sent before (as Cirro-pipelines#132 did by hand).
    return path + "[*]" if entry.get("multiple") else path


def build_input_map(spec: dict) -> dict:
    mode = input_mode(spec)
    primary, pickers = split_inputs(spec)
    managed = managed_names(spec)
    mapping: dict = {}

    if primary:
        mapping[primary["name"]] = ("$.inputs[0].dataPath" if mode == SELECT
                                    else _picked_path(primary, PRIMARY_GROUP))
    # Every managed parameter Cirro supplies today is the output location, so every
    # role maps to the dataset path. Keyed by role so a second role has a place to
    # land, rather than a conditional whose branches were identical.
    for name, role in managed.items():
        if role not in MANAGED_PATHS:
            raise errors.InvalidQuery(
                f"managed parameter {name!r} declares role {role!r}, which Cirro does "
                f"not supply. Known roles: {', '.join(sorted(MANAGED_PATHS))}.",
                what="Give the parameter a role Cirro supplies, or drop it from "
                     "managed_parameters and let it render as a form field.",
                field="cirro.managed_parameters",
                given=role,
                candidates=sorted(MANAGED_PATHS),
                user_action="Correct the role in the design spec; a re-run will not "
                            "change the outcome.",
            )
        mapping[name] = MANAGED_PATHS[role]
    for entry in pickers:
        mapping[entry["name"]] = _picked_path(entry, group_key(entry["group"]))
    for entry in spec.get("parameters") or []:
        if entry["name"] in managed or entry.get("hidden"):
            continue
        group = group_key(entry.get("group") or "general")
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

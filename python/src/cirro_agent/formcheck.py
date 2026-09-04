"""Validate generated pipeline-configuration assets before they reach Cirro.

The form is JSON Schema draft-07 plus a small set of Cirro extension keywords that
only the portal's renderer understands. There is no published allowlist, so this one
is derived from what 213 production forms in Cirro-pipelines actually use -- which is
considerably wider than the three keys previously assumed, and includes `help_text`
and `enumNames`, the two most common of all.

Three tiers, because "unknown" and "documented but never used" are different risks.
"""
import json
import re
from typing import Optional

# Standard draft-07 vocabulary, always allowed.
STANDARD = {
    "type", "title", "description", "default", "enum", "items", "properties", "required",
    "dependencies", "oneOf", "allOf", "anyOf", "not", "minimum", "maximum", "pattern",
    "format", "additionalProperties", "minItems", "maxItems", "uniqueItems", "const",
    "examples", "if", "then", "else", "readOnly", "multipleOf", "exclusiveMinimum",
    "exclusiveMaximum", "minLength", "maxLength", "patternProperties", "propertyNames",
    "contains", "deprecated", "errorMessage",
}

# Cirro extensions observed in production, with how many of the 213 forms use each.
PROVEN_EXTENSIONS = {
    "help_text": "longer explanation shown under the field",
    "pathType": "where a file picker resolves from: dataset or references",
    "enumNames": "display labels parallel to enum values",
    "file": "glob restricting a file picker",
    "process": "restrict a dataset picker to datasets from these process IDs",
    "multiple": "allow selecting more than one file",
    "useS3Path": "also accept a typed S3 path",
    "mm": "file-matching options, e.g. {matchBase: true}",
    "fa_icon": "FontAwesome icon, carried over from nf-core schemas",
    "name": "display name at the root of a form",
    "help": "shorter variant of help_text",
    "value": "fixed value for a field the user does not set",
}

# Keys inside these are chosen by the pipeline author, not the renderer, so they are
# never keywords: `properties` and `dependencies` hold field names, `mm` holds
# matcher options.
NAME_BEARING = ("properties", "dependencies")
OPAQUE = ("mm",)

# Keys whose contents are fields of the object that owns them rather than fields of a
# new nesting level. `iter_fields` treats each as transparent.
WRAPPERS = ("dependencies", "oneOf", "allOf", "anyOf", "then", "else")

# The two levels a Problem carries. Callers outside this module read them through
# `errors()` and `warnings()` rather than comparing the strings themselves.
ERROR = "error"
WARNING = "warning"

# Documented by Cirro-pipelines but used by none of the 213 production forms, and a
# `folder` picker was rejected by the portal in a real incident.
UNPROVEN_EXTENSIONS = {
    "folder": "no production form uses it and the portal has rejected it; select files "
              "with a `file` glob instead",
    "schema": "no production form uses it",
    "mimetype": "no production form uses it",
}

PATH_TYPES = ("dataset", "references")

# The upstream API's regex engine wants (?<name>...); the JSON assets in Cirro-pipelines
# use Python's (?P<name>...). Passing the wrong dialect to the wrong side 400s.
API_NAMED_GROUP = re.compile(r"\(\?P<")

# Cirro sits behind a WAF that rejects any request body containing this literal.
PATH_TRAVERSAL = ".." + "/"


class Problem:
    def __init__(self, level: str, where: str, message: str, fix: Optional[str] = None):
        self.level, self.where, self.message, self.fix = level, where, message, fix

    def to_dict(self) -> dict:
        out = {"level": self.level, "where": self.where, "message": self.message}
        if self.fix:
            out["fix"] = self.fix
        return out


def errors(report: dict) -> list:
    """The problems of a report that stop the assets working, as problem dicts."""
    return [p for p in report["problems"] if p["level"] == ERROR]


def warnings(report: dict) -> list:
    """The problems of a report worth saying out loud but not worth refusing over."""
    return [p for p in report["problems"] if p["level"] == WARNING]


def check_form(document: dict) -> list:
    """Walk the form, flagging unknown keywords and malformed pickers."""
    problems = []
    if not isinstance(document, dict) or not document:
        return [Problem("error", "form", "the asset must be "
                                         '{"ui": {...}, "form": {...}}')]
    if "form" not in document:
        # A handful of older configs carry a bare nf-core JSON Schema instead of the
        # wrapper. It renders, so this is not an error, but new assets should not.
        if any(k in document for k in ("$schema", "definitions", "properties")):
            return [Problem(
                "warning", "form",
                "this is a bare JSON Schema, not the {ui, form} wrapper Cirro expects; "
                "some older configs are shaped this way",
                fix='wrap it as {"ui": {}, "form": <schema>}')]
        return [Problem("error", "form", "the asset must be "
                                         '{"ui": {...}, "form": {...}}')]
    form = document.get("form") or {}
    misplaced = set()
    if form and "properties" not in form:
        # An empty form is legitimate: the pipeline simply takes no parameters.
        problems.append(Problem("error", "form",
                                "the form has content but no properties block"))
    for key, value in form.items():
        # A field declared on `form` instead of inside `form.properties` never renders.
        if isinstance(value, dict) and "type" in value and key not in STANDARD \
                and key not in PROVEN_EXTENSIONS and key not in UNPROVEN_EXTENSIONS:
            problems.append(Problem(
                "error", f"form.{key}",
                f"`{key}` looks like a field declared directly on `form`; it will not "
                "render",
                fix="move it inside form.properties.<group>.properties"))
            misplaced.add(key)

    def walk(node, where: str, in_properties: bool = False) -> None:  # noqa: C901
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{where}[{index}]", in_properties)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if in_properties:
                # Keys here are user-chosen field names, not keywords.
                walk(value, f"{where}.{key}")
                continue
            if key in OPAQUE:
                continue
            if key in misplaced:
                continue  # already reported as a misplaced field
            if key not in STANDARD and key not in PROVEN_EXTENSIONS:
                if key in UNPROVEN_EXTENSIONS:
                    problems.append(Problem(
                        "warning", f"{where}.{key}",
                        f"`{key}` is documented but unproven: "
                        f"{UNPROVEN_EXTENSIONS[key]}"))
                else:
                    problems.append(Problem(
                        "error", f"{where}.{key}",
                        f"`{key}` is not a keyword the Cirro form renderer understands; "
                        "an unknown keyword produces a form that errors in the portal",
                        fix=f"remove `{key}`, or use one of: "
                            f"{', '.join(sorted(PROVEN_EXTENSIONS))}"))
            walk(value, f"{where}.{key}", in_properties=(key in NAME_BEARING))

    walk(form, "form")

    for where, field in iter_fields(form):
        if not isinstance(field, dict):
            continue  # draft-07 allows a bare `true`/`false` schema; it declares nothing
        path_type = field.get("pathType")
        if path_type is not None:
            if path_type not in PATH_TYPES:
                problems.append(Problem(
                    "error", f"{where}.pathType",
                    f"pathType must be one of {PATH_TYPES}, not {path_type!r}"))
            # `process` makes it a dataset picker -- the user selects a whole dataset
            # produced by those processes, so no file glob applies.
            if not any(field.get(k) for k in ("file", "folder", "process")):
                problems.append(Problem(
                    "error", f"{where}.file",
                    "a picker needs a `file` glob to select files, or `process` to "
                    "select a whole dataset; with neither it offers nothing"))
            declared = field.get("type")
            # Some production pickers omit `type` entirely, and a multi-select picker
            # is an array; only an outright wrong scalar type is an error.
            if declared not in (None, "string", "array"):
                problems.append(Problem(
                    "error", f"{where}.type",
                    f"a file picker cannot be type {declared!r}"))
        if field.get("file") and path_type is None:
            problems.append(Problem(
                "error", f"{where}.pathType",
                "a `file` glob without pathType does not render a picker"))
        names, values = field.get("enumNames"), field.get("enum")
        if names is not None and (values is None or len(names) != len(values)):
            problems.append(Problem(
                "error", f"{where}.enumNames",
                "enumNames must be parallel to enum"))
    return problems + check_branch_overlap(form)


def iter_dependency_branches(form: dict, prefix: str = "form") -> list:
    """Every `dependencies.<selector>.oneOf` in the form, as (where, selector, branches).

    A second traversal, and deliberately so. `iter_fields` is the one walk over a
    form's *fields*, and it makes a conditional wrapper transparent -- which is
    precisely the information a branch-level check needs. This one walks the
    wrappers and yields no fields at all, so the two cannot drift into disagreeing
    about what a field is.
    """
    found = []

    def descend(node, path: str) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                descend(item, f"{path}[{index}]")
            return
        if not isinstance(node, dict):
            return
        for selector, clause in (node.get("dependencies") or {}).items():
            branches = clause.get("oneOf") if isinstance(clause, dict) else None
            if isinstance(branches, list):
                found.append((f"{path}.dependencies.{selector}.oneOf", selector,
                              branches))
        for key, value in node.items():
            descend(value, f"{path}.{key}")

    descend(form, prefix)
    return found


def _branch_selector_values(branch, selector: str) -> Optional[list]:
    """The selector values one `oneOf` branch accepts, or None if it constrains none.

    A branch that says nothing about the selector matches every value, but so does
    a branch constrained some other way this check cannot read, so `None` means
    "not comparable" and takes the whole selector out of the comparison.
    """
    if not isinstance(branch, dict):
        return None
    field = (branch.get("properties") or {}).get(selector)
    if not isinstance(field, dict):
        return None
    if isinstance(field.get("enum"), list):
        return field["enum"]
    if "const" in field:
        return [field["const"]]
    return None


def check_branch_overlap(form: dict, prefix: str = "form") -> list:
    """Flag a selector value that satisfies more than one of its `oneOf` branches.

    `oneOf` means exactly one branch may match, so a value two branches accept
    fails validation in the portal and the conditional fields behind it never
    render. Generation can no longer produce such a form; a hand-authored one can,
    and used to validate clean here.

    Warning, not error, for two reasons. A branch pair sharing a selector value can
    be told apart by another field constrained inside those branches -- a second
    selector, which is one of the reasons authored assets exist at all -- and this
    check cannot tell whether the portal resolves that. And it is new since the
    213-form calibration, which cannot be re-measured from this repository, so it
    says its piece without refusing an upload.
    """
    problems = []
    for where, selector, branches in iter_dependency_branches(form, prefix):
        accepted = [_branch_selector_values(branch, selector) for branch in branches]
        if any(values is None for values in accepted):
            continue
        # Serialized because an enum member need not be hashable, and because it
        # keeps `true` and `1` apart where a set would not.
        seen = {}
        for values in accepted:
            for value in values:
                key = json.dumps(value, sort_keys=True)
                seen[key] = seen.get(key, 0) + 1
        for key, count in seen.items():
            if count > 1:
                problems.append(Problem(
                    WARNING, where,
                    f"selecting {json.loads(key)!r} satisfies {count} of the "
                    f"{len(branches)} branches; `oneOf` allows exactly one, so the "
                    "form fails validation in the portal unless another field "
                    "constrained inside those branches tells them apart",
                    fix=f"give each branch a distinct set of `{selector}` values"))
    return problems


def iter_fields(form: dict, prefix: str = "form") -> list:
    """Every field the form declares, as (path, field) pairs in declaration order.

    This is the one walk over a form's structure. A form's fields are not just its
    `properties`: conditional fields live inside `dependencies.<selector>.oneOf[]`
    and inside `then`/`else`, and any analysis that walks `properties` alone reports
    live, correctly-mapped fields as dangling.

    Conditional wrappers -- `dependencies`, `oneOf`, `allOf`, `anyOf`, `then`, `else`
    -- are transparent: a field inside one keeps the path of the object that owns the
    wrapper, because that is where the portal renders it and where an input map
    addresses it. `dependencies` keys are the names of the *selector* fields depended
    upon, not a nesting level, so they are skipped rather than appended.

    Two consequences for callers: a path can repeat, because a selector is normally
    restated inside each of its own branches, and `field` can be a bare `true`/`false`
    as draft-07 permits rather than a dict. `if` is not walked -- its `properties`
    state conditions on fields declared elsewhere, so it declares nothing of its own.
    """
    def descend(node, path: str):
        if isinstance(node, list):
            for item in node:
                descend(item, path)
            return
        if not isinstance(node, dict):
            return
        for name, field in (node.get("properties") or {}).items():
            here = f"{path}.{name}"
            fields.append((here, field))
            descend(field, here)
        for wrapper in WRAPPERS:
            branch = node.get(wrapper)
            if branch is None:
                continue
            if wrapper == "dependencies" and isinstance(branch, dict):
                for sub in branch.values():
                    descend(sub, path)
            else:
                descend(branch, path)

    fields = []
    descend(form, prefix)
    return fields


def form_paths(document: dict) -> set:
    """Every `$.dataset.params....` path the form can produce.

    The JSONPath mirrors the form's `properties` nesting exactly, at whatever depth --
    an ungrouped form yields `$.dataset.params.<field>`, a grouped one
    `$.dataset.params.<group>.<field>`, and nested groups go deeper. Conditional
    wrappers are transparent; `iter_fields` documents that rule and implements it.
    """
    form = (document or {}).get("form") or {}
    return {path for path, _field in iter_fields(form, "$.dataset.params")}


def check_input_map(document: dict, form: Optional[dict] = None) -> list:
    """Every mapping must point at a field the form actually collects."""
    problems = []
    if not isinstance(document, dict):
        return [Problem("error", "input", "the input map must be a JSON object")]

    # A bare JSON Schema (no {ui, form} wrapper) yields no paths, so cross-checking
    # against it would flag every mapping.
    present = form_paths(form) if (form and "form" in form) else set()

    for name, value in document.items():
        # A value may be a JSONPath, a literal the workflow always receives, or a
        # literal prefix concatenated with a path using `|` -- all three are used in
        # production, so only the path portion is checked.
        if not isinstance(value, str):
            if not isinstance(value, (bool, int, float)):
                problems.append(Problem(
                    "error", f"input.{name}",
                    f"a mapping must be a JSONPath or a scalar literal, not "
                    f"{type(value).__name__}"))
            continue
        if "$." not in value:
            continue  # a literal string passed through to the workflow
        base = value[value.index("$."):].split("|")[0]
        # Array selectors address the same field: skip_tools[*] is skip_tools.
        base = re.sub(r"\[[^\]]*\]", "", base).rstrip(".")
        if present and base.startswith("$.dataset.params.") and base not in present:
            problems.append(Problem(
                "error", f"input.{name}",
                f"maps to {base}, but the form has no such field",
                fix="add the field to the form, or drop the mapping -- a dangling "
                    "mapping silently resolves to nothing at launch"))

    # No check for the reverse direction: a form field with no mapping is routine --
    # selectors that only gate conditional sections are never workflow parameters.
    return problems


def check_assets(assets: dict) -> dict:
    """Validate a whole generated asset set. `assets` maps filename -> text."""
    problems = []
    parsed = {}
    for name, text in assets.items():
        if PATH_TRAVERSAL in text:
            problems.append(Problem(
                "error", name,
                f"contains the literal {PATH_TRAVERSAL!r}, which Cirro's WAF rejects "
                "with a 403 before the request reaches the app -- even inside a comment",
                fix="use repo-relative paths"))
        if name.endswith(".json"):
            try:
                parsed[name] = json.loads(text)
            except ValueError as exc:
                problems.append(Problem("error", name, f"invalid JSON: {exc}"))

    form = parsed.get("process-form.json")
    if form is not None:
        problems += check_form(form)
    if "process-input.json" in parsed:
        problems += check_input_map(parsed["process-input.json"], form)
    if "process-output.json" in parsed:
        output = parsed["process-output.json"]
        if not isinstance(output, dict) or "commands" not in output:
            problems.append(Problem("error", "process-output.json",
                                    'must be {"commands": [...]}'))

    for name, text in assets.items():
        if name.endswith(".py") and API_NAMED_GROUP.search(text):
            problems.append(Problem(
                "warning", name,
                "uses Python's (?P<name>...) named group; the Cirro API's regex engine "
                "wants (?<name>...) and rejects the Python form with a 400"))

    blocking = [p for p in problems if p.level == ERROR]
    return {
        "ok": not blocking,
        "n_errors": len(blocking),
        "n_warnings": len(problems) - len(blocking),
        "problems": [p.to_dict() for p in problems],
        "checked": sorted(assets),
    }

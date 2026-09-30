"""Generating, validating and delivering pipeline configuration.

The generator and validator are calibrated against the 213 production forms and 72
input maps in Cirro-pipelines, so the expectations here are what real Cirro
configuration looks like, not what seemed reasonable.
"""
import json

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cirro_agent import errors, formcheck, formgen
from cirro_agent.worker import pipeline_ops, rows

SPEC = {
    "workflow": {"name": "epi2me-labs/wf-demo", "repository":
                 "https://github.com/epi2me-labs/wf-demo", "entrypoint": "main.nf",
                 "version": "v1.2.3", "executor": "NEXTFLOW", "executor_version": None},
    "inputs": [
        # No group: this is the primary data input.
        {"name": "bam", "description": "BAM files for the sample", "type": "folder",
         "required": True},
        {"name": "ref", "description": "Path to a reference FASTA file", "type": "file",
         "required": True, "group": "input"},
        {"name": "bed", "description": "Optional BED of regions", "type": "file",
         "required": False, "group": "input"},
    ],
    "parameters": [
        {"name": "sample_name", "type": "string", "group": "input", "required": False,
         "description": "Sample name"},
        {"name": "snp", "type": "boolean", "group": "workflow", "required": False,
         "default": False, "description": "Call small variants"},
        {"name": "depth", "type": "integer", "group": "advanced", "required": True,
         "description": "Minimum depth"},
        {"name": "aligner", "type": "enum", "group": "advanced", "required": False,
         "default": "minimap2", "choices": ["minimap2", "bwa"],
         "description": "Aligner"},
        {"name": "outdir", "type": "string", "group": "advanced", "required": True,
         "description": "Output directory"},
    ],
    "outputs": [{"path": "report.html", "description": "Report", "required": True}],
    "cirro": {"process_id": "process-wf-demo-1", "category": "Variant Calling",
              "parent_process_ids": [], "child_process_ids": [],
              "managed_parameters": [{"name": "outdir", "role": "output_directory",
                                      "note": "set by Cirro"}]},
}


@pytest.fixture
def assets():
    return formgen.generate(SPEC)


@pytest.fixture
def form(assets):
    return json.loads(assets[formgen.FORM])


@pytest.fixture
def input_map(assets):
    return json.loads(assets[formgen.INPUT])


# --- what gets generated ----------------------------------------------------

def test_the_asset_set_has_no_process_definition_json(assets):
    """Older config repos carry one; no configuration source uses it."""
    assert "process-definition.json" not in assets
    assert set(assets) >= {formgen.FORM, formgen.INPUT, formgen.OUTPUT, formgen.COMPUTE}


def test_the_primary_input_is_not_a_form_field(form, input_map):
    fields = {f for group in form["form"]["properties"].values()
              for f in group["properties"]}
    assert "bam" not in fields, "the dataset itself is not something the user picks"
    assert input_map["bam"] == "$.inputs[0].dataPath"


def test_a_managed_parameter_is_not_a_form_field(form, input_map):
    fields = {f for group in form["form"]["properties"].values()
              for f in group["properties"]}
    assert "outdir" not in fields
    assert input_map["outdir"] == "$.dataset.dataPath"


def test_an_unknown_managed_role_names_the_field_and_the_valid_roles():
    """A KeyError here surfaced as UPSTREAM_ERROR advising a retry that cannot help."""
    spec = json.loads(json.dumps(SPEC))
    spec["cirro"]["managed_parameters"][0]["role"] = "scratch"
    with pytest.raises(errors.AgentError) as caught:
        formgen.build_input_map(spec)
    assert caught.value.code == "INVALID_QUERY"
    assert caught.value.field == "cirro.managed_parameters"
    assert caught.value.given == "scratch"
    assert caught.value.candidates == ["output_directory"]
    assert caught.value.user_action


def test_secondary_file_inputs_become_pickers(form):
    inputs = form["form"]["properties"]["input"]["properties"]
    assert inputs["ref"]["pathType"] == "references", "a genome is reference data"
    assert inputs["bed"]["pathType"] == "dataset", "a sample-specific BED is not"
    assert inputs["ref"]["file"].endswith("{,.gz}")
    assert inputs["bed"]["file"] == "**/*.bed"
    assert all(f["type"] == "string" for f in (inputs["ref"], inputs["bed"]))


def test_every_form_field_is_mapped(form, input_map):
    groups = {f"$.dataset.params.{g}" for g in form["form"]["properties"]}
    leaves = formcheck.form_paths(form) - groups
    mapped = {v for v in input_map.values() if isinstance(v, str) and v.startswith("$.")}
    assert leaves <= mapped, f"unmapped: {sorted(leaves - mapped)}"


def test_only_must_set_parameters_are_required(form):
    advanced = form["form"]["properties"]["advanced"]
    assert advanced["required"] == ["depth"], \
        "a required parameter that has a default is pre-filled, not blocking"


def test_generated_assets_validate(assets):
    report = formcheck.check_assets(assets)
    assert report["ok"], report["problems"]


# --- conditional fields ------------------------------------------------------

def test_a_when_predicate_becomes_a_conditional_field():
    spec = json.loads(json.dumps(SPEC))
    spec["parameters"].append(
        {"name": "snp_model", "type": "string", "group": "workflow", "required": False,
         "when": "snp == true", "description": "Model"})
    form = json.loads(formgen.generate(spec)[formgen.FORM])
    workflow = form["form"]["properties"]["workflow"]
    assert "snp_model" not in workflow["properties"], "hidden until the switch is on"
    branch = workflow["dependencies"]["snp"]["oneOf"][0]["properties"]
    assert branch["snp"]["enum"] == [True]
    assert "snp_model" in branch
    # It still reads back under its group, so the mapping is unchanged.
    assert "$.dataset.params.workflow.snp_model" in formcheck.form_paths(form)


def test_two_conditional_values_on_one_selector_match_exactly_one_branch():
    """Splicing a positive/negative pair in per value made `oneOf` match twice.

    The second value's positive branch was prepended but the first value's negative
    branch kept its enum, so selecting the second value satisfied both and the form
    failed oneOf validation. formcheck does not detect the overlap.
    """
    spec = json.loads(json.dumps(SPEC))
    spec["parameters"] += [
        {"name": "mode", "type": "enum", "group": "workflow", "required": False,
         "default": "image", "choices": ["image", "text", "audio"],
         "description": "Input kind"},
        {"name": "dpi", "type": "integer", "group": "workflow", "required": False,
         "when": "mode == image", "description": "Raster density"},
        {"name": "font", "type": "string", "group": "workflow", "required": False,
         "when": "mode == text", "description": "Font family"},
    ]
    form = json.loads(formgen.generate(spec)[formgen.FORM])
    branches = form["form"]["properties"]["workflow"]["dependencies"]["mode"]["oneOf"]
    for value in ("image", "text", "audio"):
        matched = [b for b in branches if value in b["properties"]["mode"]["enum"]]
        assert len(matched) == 1, f"{value} satisfies {len(matched)} branches"
    revealed = {value: sorted(set(b["properties"]) - {"mode"})
                for value in ("image", "text", "audio")
                for b in branches if value in b["properties"]["mode"]["enum"]}
    assert revealed == {"image": ["dpi"], "text": ["font"], "audio": []}


def test_an_unparseable_when_leaves_the_field_visible():
    """Better an always-visible field than a lost one."""
    spec = json.loads(json.dumps(SPEC))
    spec["parameters"].append(
        {"name": "odd", "type": "string", "group": "workflow", "required": False,
         "when": "only when the moon is full", "description": "d"})
    form = json.loads(formgen.generate(spec)[formgen.FORM])
    assert "odd" in form["form"]["properties"]["workflow"]["properties"]


@pytest.mark.parametrize("predicate,expected", [
    ("snp == true", ("snp", True)),
    ("mode == image", ("mode", "image")),
    ("bins = 500", ("bins", 500)),
    ("ref is provided", ("ref", True)),
    ("nonsense", None),
])
def test_when_parsing(predicate, expected):
    assert formgen.parse_when(predicate) == expected


def test_a_numeric_enum_keeps_its_numeric_type():
    """A numeric dropdown declared as a string renders as free text."""
    spec = json.loads(json.dumps(SPEC))
    spec["parameters"].append(
        {"name": "bins", "type": "enum", "group": "advanced", "required": False,
         "choices": [1, 5, 500], "description": "d"})
    form = json.loads(formgen.generate(spec)[formgen.FORM])
    field = form["form"]["properties"]["advanced"]["properties"]["bins"]
    assert field["type"] == "integer"
    assert field["enum"] == [1, 5, 500]


# --- the validator ----------------------------------------------------------

def test_an_unknown_form_keyword_is_an_error():
    bad = {"ui": {}, "form": {"type": "object", "properties": {
        "g": {"type": "object", "properties": {
            "x": {"type": "string", "widget": "fancy"}}}}}}
    problems = formcheck.check_form(bad)
    assert any(p.level == "error" and "widget" in p.message for p in problems)


def test_folder_is_warned_not_accepted_silently():
    """Documented by Cirro but used by none of the 213 production forms, and the
    portal has rejected it."""
    doc = {"ui": {}, "form": {"type": "object", "properties": {
        "g": {"type": "object", "properties": {
            "d": {"type": "string", "pathType": "dataset", "folder": "**/*"}}}}}}
    problems = formcheck.check_form(doc)
    assert any(p.level == "warning" and "folder" in p.message for p in problems)
    assert not [p for p in problems if p.level == "error"]


def test_production_extensions_are_accepted():
    doc = {"ui": {"x": {"ui:widget": "radio"}}, "form": {"type": "object", "properties": {
        "g": {"type": "object", "properties": {
            "x": {"type": "string", "enum": ["a"], "enumNames": ["A"],
                  "help_text": "h", "fa_icon": "fa-dna"},
            "d": {"type": "string", "pathType": "dataset", "process": ["p1"],
                  "useS3Path": True, "mm": {"matchBase": True}}}}}}}
    assert not [p for p in formcheck.check_form(doc) if p.level == "error"]


def test_a_references_picker_with_no_glob_offers_nothing():
    doc = {"ui": {}, "form": {"type": "object", "properties": {
        "g": {"type": "object", "properties": {
            "x": {"type": "string", "pathType": "references"}}}}}}
    assert any(p.level == "error" for p in formcheck.check_form(doc))


def test_an_unfiltered_dataset_picker_is_a_warning_not_an_error():
    """`process` narrows a dataset picker; its absence widens it to every completed
    dataset in the project, which the portal renders and handles. Erroring here made
    a legal hand-written config impossible to upload -- `upload_process_config`
    refuses on errors -- while no production form exercised the case, so the
    calibration corpus never caught it."""
    doc = {"ui": {}, "form": {"type": "object", "properties": {
        "g": {"type": "object", "properties": {
            "x": {"type": "string", "pathType": "dataset"}}}}}}
    problems = formcheck.check_form(doc)
    assert not [p for p in problems if p.level == "error"]
    assert any(p.level == "warning" and "every completed dataset" in p.message
               for p in problems)


def test_a_dataset_picker_needs_no_glob():
    """`process` selects a whole dataset rather than a file inside one."""
    doc = {"ui": {}, "form": {"type": "object", "properties": {
        "g": {"type": "object", "properties": {
            "x": {"type": "string", "pathType": "dataset", "process": ["p1"]}}}}}}
    assert not [p for p in formcheck.check_form(doc) if p.level == "error"]


def test_an_empty_form_is_valid():
    """A pipeline may simply take no parameters."""
    assert not [p for p in formcheck.check_form({"ui": {}, "form": {}})
                if p.level == "error"]


def test_a_dangling_input_mapping_is_an_error():
    form = {"ui": {}, "form": {"type": "object", "properties": {
        "g": {"type": "object", "properties": {"x": {"type": "string"}}}}}}
    problems = formcheck.check_input_map(
        {"x": "$.dataset.params.g.x", "y": "$.dataset.params.g.y"}, form)
    assert [p.level for p in problems] == ["error"]
    assert "g.y" in problems[0].message


def test_literal_values_in_the_input_map_are_accepted():
    """116 literal strings and 8 literal bools appear across production maps."""
    form = {"ui": {}, "form": {"type": "object", "properties": {
        "g": {"type": "object", "properties": {"x": {"type": "string"}}}}}}
    assert not formcheck.check_input_map(
        {"a": True, "b": "samplesheet.csv", "c": 5,
         "d": "s3://bucket/prefix|$.dataset.params.g.x"}, form)


def test_the_waf_path_literal_is_refused():
    report = formcheck.check_assets(
        {"process-compute.config": "// see ../design-spec.md\n"})
    assert not report["ok"]
    assert any("WAF" in p["message"] for p in report["problems"])


def test_python_named_groups_are_flagged_in_uploaded_python():
    report = formcheck.check_assets(
        {"preprocess.py": "PATTERN = r'(?P<sampleName>.*)\\.fastq'\n"})
    assert any("(?<name>" in p["message"] for p in report["problems"])


# --- registration planning ---------------------------------------------------

def _write_spec(tmp_path, spec=None):
    import yaml
    text = "---\n" + yaml.safe_dump(spec or SPEC, sort_keys=False) + "---\n\n# Spec\n"
    path = tmp_path / "design-spec.md"
    path.write_text(text)
    return str(path)


def test_with_process_requires_the_config_repo_to_be_the_code_repo(tmp_path):
    reader = MagicMock()
    reader.processes.return_value = []
    reader.base_url = "app.cirro.test"
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.plan_create_process(reader, {
            "design_spec": _write_spec(tmp_path), "config_source": "WITH_PROCESS",
            "config_repository": "SomeoneElse/other-repo"})
    assert caught.value.code == "INVALID_QUERY"
    assert any(r.get("config_source") == "EXTERNAL"
               for r in caught.value.retry_with)


def test_external_requires_a_config_repository(tmp_path):
    reader = MagicMock()
    reader.processes.return_value = []
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.plan_create_process(reader, {
            "design_spec": _write_spec(tmp_path), "config_source": "EXTERNAL"})
    assert "config_repository" in caught.value.message


def test_with_process_defaults_to_the_workflow_repo_and_dot_cirro(tmp_path):
    reader = MagicMock()
    reader.processes.return_value = []
    reader.base_url = "app.cirro.test"
    draft = pipeline_ops.plan_create_process(reader, {
        "design_spec": _write_spec(tmp_path), "config_source": "WITH_PROCESS"})
    settings = draft.args["custom_settings"]
    assert settings == {"repository": "epi2me-labs/wf-demo", "branch": "main",
                        "folder": ".cirro", "repository_type": "GITHUB_PUBLIC"}
    assert "epi2me-labs/wf-demo" in draft.summary


def test_config_settings_carry_a_repository_type(tmp_path):
    """Cirro cannot reach the configuration repository without knowing its type.

    Omitting it is not a soft failure: the API rejects the create, as a 500
    NullPointerException for some folder names and a 400 for others.
    """
    reader = MagicMock()
    reader.processes.return_value = []
    reader.base_url = "app.cirro.test"
    draft = pipeline_ops.plan_create_process(reader, {
        "design_spec": _write_spec(tmp_path), "config_source": "WITH_PROCESS",
        "config_folder": "cirro"})
    assert draft.args["custom_settings"]["repository_type"] == "GITHUB_PUBLIC"
    assert "GITHUB_PUBLIC" in draft.summary


def test_with_process_config_shares_the_code_repository_type(tmp_path):
    """The config is in the workflow's own repo, so it cannot have a different type."""
    reader = MagicMock()
    reader.processes.return_value = []
    reader.base_url = "app.cirro.test"
    draft = pipeline_ops.plan_create_process(reader, {
        "design_spec": _write_spec(tmp_path), "config_source": "WITH_PROCESS",
        "repository_type": "GITHUB_PRIVATE"})
    assert draft.args["repository_type"] == "GITHUB_PRIVATE"
    assert draft.args["custom_settings"]["repository_type"] == "GITHUB_PRIVATE"


def test_external_config_repository_type_can_differ_from_the_code(tmp_path):
    """A separate config repository may be public while the workflow is private."""
    reader = MagicMock()
    reader.processes.return_value = []
    reader.base_url = "app.cirro.test"
    draft = pipeline_ops.plan_create_process(reader, {
        "design_spec": _write_spec(tmp_path), "config_source": "EXTERNAL",
        "config_repository": "OrgName/workflow-configs",
        "repository_type": "GITHUB_PRIVATE",
        "config_repository_type": "GITHUB_PUBLIC"})
    assert draft.args["repository_type"] == "GITHUB_PRIVATE"
    assert draft.args["custom_settings"]["repository_type"] == "GITHUB_PUBLIC"


def test_an_unacceptable_process_name_is_refused(tmp_path):
    reader = MagicMock()
    reader.processes.return_value = []
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.plan_create_process(reader, {
            "design_spec": _write_spec(tmp_path), "config_source": "STORAGE",
            "name": "Pipeline [beta]"})
    assert caught.value.code == "INVALID_QUERY"
    assert caught.value.retry_with[0]["name"] == "Pipeline  beta"


def test_registering_over_an_existing_id_is_refused(tmp_path):
    reader = MagicMock()
    reader.processes.return_value = [{"id": "process-wf-demo-1", "owner": "someone"}]
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.plan_create_process(reader, {
            "design_spec": _write_spec(tmp_path), "config_source": "STORAGE"})
    assert "already exists" in caught.value.message


def test_a_spec_without_a_workflow_version_is_refused(tmp_path):
    """`str(workflow.get("version"))` registered a pipeline pinned to the ref "None".

    version is required on PipelineCode, so there is nothing to omit: the spec has to
    name a ref, and the approval must never render one that resolves to nothing.
    """
    spec = json.loads(json.dumps(SPEC))
    del spec["workflow"]["version"]
    reader = MagicMock()
    reader.processes.return_value = []
    reader.base_url = "app.cirro.test"
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.plan_create_process(reader, {
            "design_spec": _write_spec(tmp_path, spec), "config_source": "STORAGE"})
    assert caught.value.code == "INVALID_QUERY"
    assert "version" in caught.value.message
    assert caught.value.field == "design_spec"


def test_a_new_config_repository_type_reaches_the_approved_diff(monkeypatch):
    """apply read it out of `changes`, but the planner never put it there.

    So the one field the update path exists to protect could not be changed, and the
    change was absent from the diff the person approved.
    """
    monkeypatch.setattr(pipeline_ops, "_owned_process", lambda _r, _p: {
        "name": "Demo", "id": "p1", "config_source": pipeline_ops.EXTERNAL,
        "pipeline_code": {"repository_path": "org/wf", "version": "v1",
                          "entry_point": "main.nf",
                          "repository_type": "GITHUB_PUBLIC"},
        "custom_settings": {"repository": "Org/configs", "branch": "main",
                            "folder": ".cirro",
                            "repository_type": "GITHUB_PUBLIC"}})
    draft = pipeline_ops.plan_update_process(
        MagicMock(), {"process_id": "p1", "config_repository_type": "GITHUB_PRIVATE"})
    assert draft.args["changes"]["config_repository_type"] == "GITHUB_PRIVATE"
    assert "GITHUB_PRIVATE" in draft.summary
    assert "GITHUB_PUBLIC" in draft.summary, "the diff names what it changes from"


def test_a_spec_without_frontmatter_is_refused(tmp_path):
    path = tmp_path / "design-spec.md"
    path.write_text("# just prose\n")
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops._load_spec(str(path))
    assert "frontmatter" in caught.value.message


def test_python_named_groups_in_file_mapping_rules_are_refused(tmp_path):
    spec = json.loads(json.dumps(SPEC))
    spec["cirro"]["file_mapping_rules"] = [{
        "description": "Reads",
        "file_name_patterns": [{"example_name": "r.fastq.gz", "description": "FASTQ",
                                "sample_matching_pattern": r"(?P<sampleName>.*)\.fastq"}]}]
    reader = MagicMock()
    reader.processes.return_value = []
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.plan_create_process(reader, {
            "design_spec": _write_spec(tmp_path, spec), "config_source": "STORAGE"})
    assert "(?P<name>" in caught.value.message
    assert caught.value.retry_with[0]["sample_matching_pattern"] == \
        r"(?<sampleName>.*)\.fastq", "the fix is the API dialect, ready to paste back"


def test_a_process_owned_by_someone_else_is_refused():
    reader = MagicMock()
    reader.username = "me@example.org"
    reader.processes.return_value = [{"id": "p1", "owner": "them@example.org",
                                      "name": "Theirs"}]
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops._owned_process(reader, "p1")
    assert caught.value.code == "FORBIDDEN"
    assert "them@example.org" in caught.value.message


def test_a_cirro_curated_process_is_refused():
    reader = MagicMock()
    reader.username = "me@example.org"
    reader.processes.return_value = [{"id": "p1", "owner": None, "name": "Curated"}]
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops._owned_process(reader, "p1")
    assert caught.value.code == "FORBIDDEN"


OP_MODULES = ("analysis_ops", "chain_ops", "dashboard_ops", "dataset_ops", "pipeline_ops",
              "project_ops", "share_ops", "sheet_ops")


@pytest.mark.parametrize("first", OP_MODULES + ("mutations",))
def test_any_op_module_can_be_imported_first(first):
    """Each family registers itself, so none of them has to be imported before another.

    Run in a fresh interpreter, because the failure this guards -- a module reading
    from a partially initialized mutations -- only shows on first import.
    """
    import subprocess
    import sys
    code = (f"from cirro_agent.worker import {first};"
            "from cirro_agent.worker.mutations import OPS;"
            "assert len(OPS) == 44, len(OPS);"
            "assert 'create_process' in OPS and 'run_analysis' in OPS and 'chain' in OPS")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-800:]


def test_commit_never_pushes():
    """The plugin writes and commits; putting it on a remote stays the user's to do."""
    import inspect
    source = inspect.getsource(pipeline_ops.apply_commit_process_config)
    assert '"push"' not in source and "'push'" not in source


# --- the revision the upload would replace -----------------------------------

REVISION_API = ("cirro_api_client.v1.api.processes.get_latest_process_revision"
                ".sync_detailed")
RESOURCE_API = ("cirro_api_client.v1.api.processes.get_process_resource"
                ".sync_detailed")


class _Response:
    """A generated-client Response double: a status code and a parsed body."""

    def __init__(self, status_code=200, **parsed):
        self.status_code = status_code
        self.parsed = SimpleNamespace(**parsed) if parsed else None


def _not_found(*_args, **_kwargs):
    """What the generated client raises for a 404 -- and 404 is how the revision
    endpoint answers for a process with no configuration in tenant storage."""
    from cirro_api_client.v1.errors import NotFoundException
    raise NotFoundException({"statusCode": 404, "errorCode": "NOT_FOUND",
                             "errorDetail": "no configuration in storage",
                             "errors": []})


def _field(summary: str, label: str) -> str:
    """The value on one aligned `  Label   value` line of an approval summary."""
    for line in summary.splitlines():
        if line.strip().startswith(label):
            return line.strip()[len(label):].strip()
    raise AssertionError(f"no {label!r} line in:\n{summary}")


@pytest.fixture
def storage_process(monkeypatch):
    """A STORAGE process this user owns, so planning reaches the revision read."""
    monkeypatch.setattr(pipeline_ops, "_owned_process",
                        lambda _r, _p: {"name": "Demo", "id": "p1",
                                        "config_source": pipeline_ops.STORAGE})
    reader = MagicMock()
    reader.base_url = "app.cirro.test"
    return reader


@pytest.fixture
def first_upload(monkeypatch):
    """Nothing in storage yet: both revision and resource endpoints answer 404."""
    monkeypatch.setattr(REVISION_API, _not_found)
    monkeypatch.setattr(RESOURCE_API, _not_found)


def test_an_unreadable_revision_is_refused_not_called_a_first_upload(
        tmp_path, monkeypatch, storage_process):
    """One `except Exception` covered the revision read, so a transient failure was
    indistinguishable from having no revision at all: the approval said "none -- this
    would be the first", reported every asset as New, suppressed the no-op warning,
    and left if_match None so the write went up UNSET. The optimistic-concurrency
    guard was silently dropped and a live revision overwritten unseen."""
    def unavailable(*_a, **_k):
        raise ConnectionError("connection reset by peer")
    monkeypatch.setattr(REVISION_API, unavailable)

    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.plan_upload_process_config(
            storage_process, {"process_id": "p1",
                              "design_spec": _write_spec(tmp_path)})
    assert caught.value.code == "UPSTREAM_ERROR"
    assert "connection reset by peer" in caught.value.message
    assert caught.value.retry_with


def test_no_revision_at_all_is_still_reported_as_the_first_upload(
        tmp_path, storage_process, first_upload):
    """The other half of the distinction: a 404 really does mean nothing is there."""
    draft = pipeline_ops.plan_upload_process_config(
        storage_process, {"process_id": "p1", "design_spec": _write_spec(tmp_path)})
    assert draft.args["if_match"] is None
    assert _field(draft.summary, "Current revision") == "none — this would be the first"
    assert "process-form.json" in _field(draft.summary, "New")


def test_an_unreadable_live_slot_keeps_the_revision_guard(tmp_path, monkeypatch,
                                                          storage_process):
    """A failure reading the live slots must not throw away a token already obtained.

    Nor may an unread slot be listed as New: that understates what the upload
    replaces, which is the one thing this approval cannot get wrong.
    """
    monkeypatch.setattr(REVISION_API, lambda *_a, **_k: _Response(revision_number=4))
    monkeypatch.setattr(RESOURCE_API, lambda *_a, **_k: _Response(status_code=500))

    draft = pipeline_ops.plan_upload_process_config(
        storage_process, {"process_id": "p1", "design_spec": _write_spec(tmp_path)})

    assert draft.args["if_match"] == "4", "the guard was obtained; it must still be sent"
    assert _field(draft.summary, "Current revision") == "4"
    assert _field(draft.summary, "New").startswith("unknown")
    assert _field(draft.summary, "Changed").startswith("unknown")
    assert any("cannot say which slots" in w for w in draft.warnings)
    assert not any("no-op" in w for w in draft.warnings), \
        "a comparison that did not happen cannot conclude nothing changed"


# --- one slot vocabulary, shared by the read and the write -------------------

def test_the_slot_vocabulary_is_the_sdk_enum_minus_its_fallback():
    """The vocabulary is a mirror of a generated enum, so it is checked, not trusted.

    `ProcessResource._missing_` maps an unrecognized value onto UNKNOWN instead of
    raising, so a slot name that has drifted from the enum would go out on the wire as
    UNKNOWN rather than failing loudly here.
    """
    from cirro_api_client.v1.models import ProcessResource
    declared = tuple(sorted(m.value for m in ProcessResource
                            if m is not ProcessResource.UNKNOWN))
    assert formgen.CONFIG_SLOTS == declared
    assert set(formgen.JSON_SLOTS) <= set(formgen.CONFIG_SLOTS)


def test_the_filename_map_covers_every_slot():
    """`SLOTS` is derived from the vocabulary, so it cannot name a slot that is not in
    it -- the drift this replaces had `metadata` in one list and not the other."""
    assert set(formgen.SLOTS.values()) == set(formgen.CONFIG_SLOTS)


def test_the_planner_sees_a_live_slot_no_asset_of_its_own_covers(
        tmp_path, monkeypatch, storage_process):
    """The planner once read fewer slots than the read verb, so a live slot this upload
    carries nothing for was invisible to the approval: New and Changed named only what
    the upload carries, and nothing said the revision holds a slot this one does not.
    A generated upload has no preprocess script, so a live one is that case."""
    def only_preprocess(_process_id, resource, **_kwargs):
        if str(resource) == "preprocess":
            return _Response(content='print("kept")')
        _not_found()

    monkeypatch.setattr(REVISION_API, lambda *_a, **_k: _Response(revision_number=4))
    monkeypatch.setattr(RESOURCE_API, only_preprocess)

    draft = pipeline_ops.plan_upload_process_config(
        storage_process, {"process_id": "p1", "design_spec": _write_spec(tmp_path)})

    assert _field(draft.summary, "Not uploaded") == "preprocess (live now, no asset here)"
    assert any("preprocess" in w for w in draft.warnings)
    assert "process-form.json" in _field(draft.summary, "New")


# --- the user keeps a copy of what was uploaded ------------------------------

def test_uploaded_configuration_is_staged_beside_the_design_spec(tmp_path):
    """Uploading into Cirro storage must still leave something to read locally."""
    spec = _write_spec(tmp_path)
    assert pipeline_ops._staging_dir({"design_spec": spec}) == \
        tmp_path / pipeline_ops.STAGING_DIR


def test_the_staging_folder_can_be_pointed_elsewhere(tmp_path):
    elsewhere = tmp_path / "somewhere" / "else"
    assert pipeline_ops._staging_dir(
        {"design_spec": _write_spec(tmp_path), "stage_dir": str(elsewhere)}) == elsewhere


def test_a_relative_stage_dir_lands_beside_the_spec_not_in_the_plugin(tmp_path):
    """The worker's cwd is the plugin installation, so a bare relative path would
    otherwise write the user's configuration inside the plugin."""
    staged = pipeline_ops._staging_dir(
        {"design_spec": _write_spec(tmp_path), "stage_dir": "config"})
    assert staged == tmp_path / "config"


def test_the_plan_says_where_the_copy_will_land(tmp_path, monkeypatch, first_upload):
    reader = MagicMock()
    reader.base_url = "app.cirro.test"
    reader.username = "me@example.org"
    reader.processes.return_value = [{"id": "p1", "owner": "me@example.org"}]
    monkeypatch.setattr(pipeline_ops, "_owned_process",
                        lambda _r, _p: {"name": "Demo", "id": "p1",
                                        "config_source": pipeline_ops.STORAGE})
    draft = pipeline_ops.plan_upload_process_config(
        reader, {"process_id": "p1", "design_spec": _write_spec(tmp_path)})
    assert str(tmp_path / pipeline_ops.STAGING_DIR) in draft.summary
    assert draft.args["stage_dir"] == str(tmp_path / pipeline_ops.STAGING_DIR)


def test_the_copy_is_written_only_after_cirro_accepts_it(tmp_path, monkeypatch):
    """A staged copy is a record of what is live, not of what was attempted."""
    stage = tmp_path / pipeline_ops.STAGING_DIR
    assets = {"process-form.json": '{"form": {}, "ui": {}}'}

    reader = MagicMock()
    rejected = MagicMock()
    rejected.status_code = 403
    monkeypatch.setattr(
        "cirro_api_client.v1.api.processes.save_process_revision.sync_detailed",
        lambda *a, **k: rejected)
    with pytest.raises(errors.AgentError):
        pipeline_ops.apply_upload_process_config(
            reader, {"process_id": "p1", "assets": assets,
                     "stage_dir": str(stage), "commit_message": "m"})
    assert not stage.exists(), "a rejected upload must not leave a misleading copy"

    accepted = MagicMock()
    accepted.status_code = 200
    accepted.parsed.revision_token = "7"
    monkeypatch.setattr(
        "cirro_api_client.v1.api.processes.save_process_revision.sync_detailed",
        lambda *a, **k: accepted)
    result = pipeline_ops.apply_upload_process_config(
        reader, {"process_id": "p1", "assets": assets,
                 "stage_dir": str(stage), "commit_message": "m"})
    assert result["staged_to"] == str(stage)
    assert (stage / "process-form.json").read_text() == assets["process-form.json"]


def test_a_staging_failure_does_not_report_a_live_revision_as_failed(tmp_path,
                                                                     monkeypatch):
    """The revision is already committed upstream when staging runs.

    Raising here made dispatch mark the plan failed and consume it, so the agent's
    recovery was to re-plan and push a duplicate revision.
    """
    blocked = tmp_path / "unwritable"
    blocked.write_text("this is a file, so mkdir underneath it cannot work")

    accepted = MagicMock()
    accepted.status_code = 200
    accepted.parsed.revision_token = "7"
    monkeypatch.setattr(
        "cirro_api_client.v1.api.processes.save_process_revision.sync_detailed",
        lambda *a, **k: accepted)

    result = pipeline_ops.apply_upload_process_config(
        MagicMock(), {"process_id": "p1", "assets": {"process-form.json": "{}"},
                      "stage_dir": str(blocked / "nested"), "commit_message": "m"})

    assert result["revision_token"] == "7", "the upload succeeded and must say so"
    assert result["staged"] == []
    assert "the revision itself was accepted and is live" in result["omitted"][0]["note"]


def test_an_upload_that_also_writes_local_disk_says_so_in_the_approval():
    """The risk string is what the approval prompt shows.

    Presenting an operation that creates directories and overwrites files as a
    plain remote write understates what the user is agreeing to.
    """
    from cirro_agent.worker import mutations
    assert mutations.OPS["upload_process_config"].risk == mutations.RISK_WRITE_LOCAL
    assert mutations.OPS["update_process"].risk == mutations.RISK_WRITE


def test_the_plan_names_the_local_files_it_would_replace(tmp_path, monkeypatch,
                                                         first_upload):
    """write_process_config already holds this standard; both paths now match."""
    spec = _write_spec(tmp_path)
    stale = tmp_path / pipeline_ops.STAGING_DIR
    stale.mkdir()
    (stale / "process-form.json").write_text("{}")

    reader = MagicMock()
    reader.base_url = "app.cirro.test"
    reader.processes.return_value = [{"id": "p1", "owner": "me@example.org"}]
    monkeypatch.setattr(pipeline_ops, "_owned_process",
                        lambda _r, _p: {"name": "Demo", "id": "p1",
                                        "config_source": pipeline_ops.STORAGE})

    draft = pipeline_ops.plan_upload_process_config(
        reader, {"process_id": "p1", "design_spec": spec})

    assert "process-form.json" in draft.summary.split("Replaced locally")[1]
    assert any("will be overwritten" in w for w in draft.warnings)


# --- hand-authored assets ---------------------------------------------------

# A form a design spec cannot express: two selectors each revealing their own block,
# which is the reason authored assets exist at all.
AUTHORED_FORM = {
    "form": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["fast", "thorough"], "title": "Mode"},
            "call_variants": {"type": "boolean", "title": "Call variants"},
        },
        "dependencies": {
            "mode": {"oneOf": [
                {"properties": {"mode": {"enum": ["fast"]}}},
                {"properties": {"mode": {"enum": ["thorough"]},
                                "passes": {"type": "integer", "title": "Passes"}}},
            ]},
            "call_variants": {"oneOf": [
                {"properties": {"call_variants": {"enum": [False]}}},
                {"properties": {"call_variants": {"enum": [True]},
                                "min_depth": {"type": "integer", "title": "Depth"}}},
            ]},
        },
    },
    "ui": {},
}

AUTHORED_INPUT = {
    "mode": "$.dataset.params.mode",
    "passes": "$.dataset.params.passes",
    "call_variants": "$.dataset.params.call_variants",
    "min_depth": "$.dataset.params.min_depth",
}


def _authored(tmp_path, form=None, extra=None):
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "process-form.json").write_text(
        json.dumps(form if form is not None else AUTHORED_FORM, indent=2))
    (directory / "process-input.json").write_text(json.dumps(AUTHORED_INPUT, indent=2))
    (directory / "process-output.json").write_text(json.dumps({"commands": []}))
    for name, text in (extra or {}).items():
        (directory / name).write_text(text)
    return directory


def test_authored_assets_are_accepted_verbatim(tmp_path):
    directory = _authored(tmp_path)
    assets, report = pipeline_ops._authored_assets(str(directory))
    assert report["ok"], report["problems"]
    assert set(assets) == {"process-form.json", "process-input.json",
                           "process-output.json"}
    # Byte-for-byte: the whole point is that nothing regenerates or reformats them.
    assert assets["process-form.json"] == \
        (directory / "process-form.json").read_text()
    assert "min_depth" in assets["process-form.json"]


def test_conditional_fields_from_two_selectors_are_not_reported_dangling(tmp_path):
    """The walk that only looked at `properties` called these live fields dangling."""
    _, report = pipeline_ops._authored_assets(str(_authored(tmp_path)))
    assert report["n_errors"] == 0
    assert not any("min_depth" in p["message"] or "passes" in p["message"]
                   for p in report["problems"])


def test_authored_assets_that_would_not_render_are_refused(tmp_path):
    directory = _authored(tmp_path)
    (directory / "process-form.json").write_text("{not json")
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops._authored_assets(str(directory))
    assert caught.value.code == "INVALID_QUERY"
    assert caught.value.retry_with[0]["verb"] == "check_config"


def test_a_folder_without_a_form_names_what_it_found(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "form.json").write_text("{}")
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops._authored_assets(str(directory))
    assert caught.value.code == "INVALID_QUERY"
    assert "process-form.json" in caught.value.message


def test_unrecognized_files_in_the_folder_are_left_alone(tmp_path):
    directory = _authored(tmp_path, extra={"notes.md": "scratch", "old.json.bak": "{}"})
    assets, _ = pipeline_ops._authored_assets(str(directory))
    assert "notes.md" not in assets and "old.json.bak" not in assets


def test_upload_takes_authored_assets_instead_of_a_spec(tmp_path, monkeypatch,
                                                        first_upload):
    directory = _authored(tmp_path, extra={"preprocess.py": "def x(): pass\n"})
    reader = MagicMock()
    reader.base_url = "app.cirro.test"
    monkeypatch.setattr(pipeline_ops, "_owned_process",
                        lambda _r, _p: {"name": "Demo", "id": "p1",
                                        "config_source": pipeline_ops.STORAGE})
    draft = pipeline_ops.plan_upload_process_config(reader, {
        "process_id": "process-abc", "assets_dir": str(directory)})
    assert "process-form.json" in draft.args["assets"]
    assert draft.args["assets_dir"] == str(directory.resolve())
    # Nothing is staged: the files already live where the person put them, and
    # rewriting their own source would be a surprise.
    assert draft.args["stage_dir"] is None
    assert "Staged to" not in draft.summary
    assert f"hand-authored in {directory}" in draft.summary
    assert "exactly as written" in draft.summary


def test_upload_needs_exactly_one_asset_source(tmp_path):
    """Refused before the process is even fetched: it is a malformed request."""
    reader = MagicMock()
    for args in ({},
                 {"design_spec": _write_spec(tmp_path),
                  "assets_dir": str(_authored(tmp_path))}):
        with pytest.raises(errors.AgentError) as caught:
            pipeline_ops.plan_upload_process_config(
                reader, {"process_id": "process-abc", **args})
        assert caught.value.code == "INVALID_QUERY"
        assert {"design_spec", "assets_dir"} <= {
            key for retry in caught.value.retry_with for key in retry.get("args", {})}


def test_a_preprocess_path_conflicts_with_an_authored_folder(tmp_path):
    script = tmp_path / "pre.py"
    script.write_text("x = 1\n")
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops._assets_for({"assets_dir": str(_authored(tmp_path))},
                                 preprocess=script.read_text())
    assert caught.value.field == "preprocess_path"


def test_create_process_validates_the_authored_assets_it_will_upload(tmp_path):
    """Otherwise the pre-flight vouches for assets that are never uploaded."""
    directory = _authored(tmp_path)
    reader = MagicMock()
    reader.processes.return_value = []
    reader.base_url = "app.cirro.test"
    draft = pipeline_ops.plan_create_process(reader, {
        "design_spec": _write_spec(tmp_path), "config_source": "STORAGE",
        "assets_dir": str(directory)})
    assert draft.args["assets_dir"] == str(directory.resolve())
    assert f"hand-authored in {directory}" in draft.summary
    assert "3 assets, validated clean" in draft.summary
    assert str(directory.resolve()) in draft.summary.split("Registering does not")[1]


def test_a_create_returning_no_process_id_is_an_error():
    """A 400 or 401 arrives as a parsed body, not an exception.

    The generated client returns the response for those statuses instead of raising.
    Reporting the requested id back as though it were created leaves the caller
    believing a process exists, and every later read 404s instead.
    """
    reader = MagicMock()
    reader.client.processes.create_custom_process.return_value = MagicMock(id=None)
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.apply_create_process(reader, {
            "process_id": "org-demo", "name": "Demo", "description": "d",
            "executor": "NEXTFLOW", "child_process_ids": [], "parent_process_ids": [],
            "category": "Community", "repository_path": "epi2me-labs/wf-demo",
            "version": "v1", "repository_type": "GITHUB_PUBLIC",
            "entry_point": "main.nf", "executor_version": None,
            "uses_sample_sheet": False, "allow_multiple_sources": True,
            "config_source": "STORAGE", "custom_settings": None,
            "file_mapping_rules": [], "design_spec": "spec.md"})
    assert caught.value.code == "UPSTREAM_ERROR"
    assert "org-demo" in str(caught.value)


def _owned(**overrides):
    """A process record as the reader returns it, owned by the caller."""
    record = {
        "id": "org-demo", "name": "Demo", "description": "d", "category": "Community",
        "executor": "NEXTFLOW", "owner": "me@example.test", "is_custom": True,
        "config_source": "STORAGE", "custom_settings": None,
        "pipeline_code": {"repository_path": "org/demo", "version": "v1",
                          "entry_point": "main.nf", "executor_version": None,
                          "repository_type": "GITHUB_PUBLIC"},
        "uses_sample_sheet": True, "allow_multiple_sources": False,
        "parent_process_ids": [], "child_process_ids": [],
        "execution_modes": ["STANDARD"],
    }
    record.update(overrides)
    return record


def _reader_for(record):
    """A reader whose signed-in user owns the record; edits are refused otherwise."""
    reader = MagicMock()
    reader.base_url = "app.cirro.test"
    reader.username = record["owner"]
    reader.processes.return_value = [record]
    reader.process.return_value = record
    # The detail payload, not a bare MagicMock: every attribute of a mock is truthy,
    # so the row mapping would read the record's booleans back as True whatever the
    # fixture says, and a diff against them would test nothing.

    def namespace(value):
        return SimpleNamespace(**value) if isinstance(value, dict) else value
    reader.client.processes.get.return_value = SimpleNamespace(
        allow_multiple_sources=record.get("allow_multiple_sources", False),
        config_source=record.get("config_source"),
        custom_settings=namespace(record.get("custom_settings")),
        pipeline_code=namespace(record.get("pipeline_code")),
        file_mapping_rules=[])
    return reader


def test_uses_sample_sheet_can_be_turned_off():
    """A process registered with the wrong flag was previously uncorrectable here.

    apply already sent the field, but plan diffed only name, version and config
    location, so update_process reported that nothing would change.
    """
    reader = _reader_for(_owned())
    draft = pipeline_ops.plan_update_process(
        reader, {"process_id": "org-demo", "uses_sample_sheet": False})
    assert draft.args["changes"]["uses_sample_sheet"] is False


def test_a_flag_already_at_the_requested_value_is_not_a_change():
    reader = _reader_for(_owned())
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.plan_update_process(
            reader, {"process_id": "org-demo", "uses_sample_sheet": True})
    assert caught.value.code == "INVALID_QUERY"


def test_allow_multiple_sources_can_be_turned_on():
    """False is a real value, so the check is against None, not truthiness."""
    reader = _reader_for(_owned())
    draft = pipeline_ops.plan_update_process(
        reader, {"process_id": "org-demo", "allow_multiple_sources": True})
    assert draft.args["changes"]["allow_multiple_sources"] is True


def test_documentation_url_can_be_cleared():
    reader = _reader_for(_owned(documentation_url="https://docs.example.test"))
    draft = pipeline_ops.plan_update_process(
        reader, {"process_id": "org-demo", "documentation_url": ""})
    assert draft.args["changes"]["documentation_url"] is None


def test_an_unrelated_update_keeps_the_documentation_url():
    reader = _reader_for(_owned(documentation_url="https://docs.example.test"))
    draft = pipeline_ops.plan_update_process(
        reader, {"process_id": "org-demo", "uses_sample_sheet": False})
    assert "documentation_url" not in draft.args["changes"]
    assert draft.args["current"]["documentation_url"] == "https://docs.example.test"


def test_omitting_documentation_url_is_not_a_request_to_clear_it():
    reader = _reader_for(_owned(documentation_url="https://docs.example.test"))
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.plan_update_process(reader, {"process_id": "org-demo"})
    assert caught.value.code == "INVALID_QUERY"


# Every field plan_update_process diffs, with a value the fixture record does not
# already hold. Driven through `Op.planned` rather than the planner, because the
# declaration is what was wrong: `accepts` named three of these and the other eleven
# were refused before the planner ran -- including `version`, which the planner's own
# "Nothing would change" error suggests as the retry.
UPDATE_PROCESS_FIELDS = {
    "name": "Demo Two",
    "description": "a new description",
    "category": "Genomics",
    "version": "v2.0.0",
    "entry_point": "workflows/main.nf",
    "executor_version": "23.10.1",
    "uses_sample_sheet": False,
    "allow_multiple_sources": True,
    "execution_modes": ["STANDARD", "HEALTHOMICS"],
    "documentation_url": "https://docs.example.test",
    "config_repository": "org/config",
    "config_repository_type": "GITHUB_PRIVATE",
    "config_branch": "release",
    "config_folder": "processes/demo",
}


@pytest.mark.parametrize("field", sorted(UPDATE_PROCESS_FIELDS))
def test_update_process_accepts_every_field_it_diffs(field):
    from cirro_agent.worker import mutations
    op = mutations.OPS["update_process"]
    draft = op.planned(_reader_for(_owned()),
                       {"process_id": "org-demo", field: UPDATE_PROCESS_FIELDS[field]})
    assert field in draft.args["changes"]


def test_allow_multiple_sources_is_read_from_the_detail_payload():
    """It is absent from the list payload, so an update that guessed could flip it."""
    detail = SimpleNamespace(allow_multiple_sources=False, config_source="STORAGE",
                             custom_settings=None, pipeline_code=None,
                             file_mapping_rules=[])
    row = rows.process_detail(detail, base=_owned(allow_multiple_sources=True))
    assert row["allow_multiple_sources"] is False


# --- execution modes ---------------------------------------------------------
#
# The vocabulary a process declares, not the one a run request speaks. A pipeline that
# declares only STANDARD cannot be launched on HealthOmics however the run is phrased,
# so registration is where the capability is turned on.

def test_a_registration_declares_the_modes_it_was_given(tmp_path):
    reader = MagicMock()
    reader.processes.return_value = []
    reader.base_url = "app.cirro.test"
    draft = pipeline_ops.plan_create_process(reader, {
        "design_spec": _write_spec(tmp_path), "config_source": "STORAGE",
        "execution_modes": ["HEALTHOMICS", "standard"]})
    # Canonical order and case, however the caller spelled it: apply re-plans and
    # refuses a plan whose rendered text has changed.
    assert draft.args["execution_modes"] == ["STANDARD", "HEALTHOMICS"]
    assert "Runs on         STANDARD, HEALTHOMICS" in draft.summary


def test_a_registration_that_names_no_mode_declares_nothing(tmp_path):
    """None is "say nothing", so Cirro keeps its own default rather than being told."""
    reader = MagicMock()
    reader.processes.return_value = []
    reader.base_url = "app.cirro.test"
    draft = pipeline_ops.plan_create_process(reader, {
        "design_spec": _write_spec(tmp_path), "config_source": "STORAGE"})
    assert draft.args["execution_modes"] is None
    assert "Cirro default (STANDARD -- AWS Batch)" in draft.summary


def test_the_design_spec_can_declare_the_modes(tmp_path):
    import copy
    spec = copy.deepcopy(SPEC)
    spec["cirro"]["execution_modes"] = ["HEALTHOMICS"]
    reader = MagicMock()
    reader.processes.return_value = []
    reader.base_url = "app.cirro.test"
    draft = pipeline_ops.plan_create_process(reader, {
        "design_spec": _write_spec(tmp_path, spec), "config_source": "STORAGE"})
    assert draft.args["execution_modes"] == ["HEALTHOMICS"]


def test_an_environment_type_is_refused_where_a_mode_belongs(tmp_path):
    """The mirror of the run path's refusal: a request says AWS_OMICS, a process says
    HEALTHOMICS, and accepting either name for the other is what makes a wrong value
    look plausible."""
    reader = MagicMock()
    reader.processes.return_value = []
    reader.base_url = "app.cirro.test"
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.plan_create_process(reader, {
            "design_spec": _write_spec(tmp_path), "config_source": "STORAGE",
            "execution_modes": ["AWS_OMICS"]})
    assert caught.value.code == "INVALID_QUERY"
    assert any(r["execution_modes"] == ["STANDARD", "HEALTHOMICS"]
               for r in caught.value.retry_with)


def test_an_empty_mode_list_is_refused_rather_than_sent(tmp_path):
    """The array is nullable with minItems 1, so [] is not a way to say "default"."""
    reader = MagicMock()
    reader.processes.return_value = []
    reader.base_url = "app.cirro.test"
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.plan_create_process(reader, {
            "design_spec": _write_spec(tmp_path), "config_source": "STORAGE",
            "execution_modes": []})
    assert caught.value.code == "INVALID_QUERY"


def test_healthomics_can_be_turned_on_for_an_existing_pipeline():
    reader = _reader_for(_owned())
    draft = pipeline_ops.plan_update_process(
        reader, {"process_id": "org-demo",
                 "execution_modes": ["STANDARD", "HEALTHOMICS"]})
    assert draft.args["changes"]["execution_modes"] == ["STANDARD", "HEALTHOMICS"]


def test_an_unrelated_update_carries_the_declared_modes_forward():
    """CustomProcessInput replaces the record, so a declaration this plan does not
    name has to be in `current` or the edit strips it."""
    reader = _reader_for(_owned(execution_modes=["STANDARD", "HEALTHOMICS"]))
    draft = pipeline_ops.plan_update_process(
        reader, {"process_id": "org-demo", "documentation_url": "https://x.test"})
    assert "execution_modes" not in draft.args["changes"]
    assert draft.args["current"]["execution_modes"] == ["STANDARD", "HEALTHOMICS"]


def test_dropping_healthomics_warns_that_the_capability_goes_with_it():
    reader = _reader_for(_owned(execution_modes=["STANDARD", "HEALTHOMICS"]))
    draft = pipeline_ops.plan_update_process(
        reader, {"process_id": "org-demo", "execution_modes": ["STANDARD"]})
    assert draft.args["changes"]["execution_modes"] == ["STANDARD"]
    assert any("HealthOmics" in w for w in draft.warnings)


def test_declaring_the_modes_a_pipeline_already_has_is_not_a_change():
    reader = _reader_for(_owned())
    with pytest.raises(errors.AgentError) as caught:
        pipeline_ops.plan_update_process(
            reader, {"process_id": "org-demo", "execution_modes": ["STANDARD"]})
    assert caught.value.code == "INVALID_QUERY"

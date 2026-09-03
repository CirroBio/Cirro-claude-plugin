"""Generating, validating and delivering pipeline configuration.

The generator and validator are calibrated against the 213 production forms and 72
input maps in Cirro-pipelines, so the expectations here are what real Cirro
configuration looks like, not what seemed reasonable.
"""
import json

from unittest.mock import MagicMock

import pytest

from cirro_agent import errors, formcheck, formgen
from cirro_agent.worker import pipeline_ops

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


def test_a_picker_with_neither_glob_nor_process_offers_nothing():
    doc = {"ui": {}, "form": {"type": "object", "properties": {
        "g": {"type": "object", "properties": {
            "x": {"type": "string", "pathType": "dataset"}}}}}}
    assert any(p.level == "error" for p in formcheck.check_form(doc))


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
                        "folder": ".cirro"}
    assert "epi2me-labs/wf-demo" in draft.summary


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


OP_MODULES = ("analysis_ops", "chain_ops", "dataset_ops", "pipeline_ops",
              "project_ops", "share_ops")


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
            "assert len(OPS) == 34, len(OPS);"
            "assert 'create_process' in OPS and 'run_analysis' in OPS and 'chain' in OPS")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-800:]


def test_commit_never_pushes():
    """The plugin writes and commits; putting it on a remote stays the user's to do."""
    import inspect
    source = inspect.getsource(pipeline_ops.apply_commit_process_config)
    assert '"push"' not in source and "'push'" not in source


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


def test_the_plan_says_where_the_copy_will_land(tmp_path, monkeypatch):
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


def test_the_plan_names_the_local_files_it_would_replace(tmp_path, monkeypatch):
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

"""Reading a workflow's own declarations. No tenant, no network."""
import json

import pytest

from cirro_agent import workflow_spec as WS


def test_a_balanced_params_block_reads_its_assignments(tmp_path):
    (tmp_path / "nextflow.config").write_text(
        "params {\n  input = 'in.csv'\n  threads = 4\n}\nprocess { cpus = 1 }\n")
    flags = []
    params, groups = WS._params_from_config(tmp_path, flags)
    assert {p["name"] for p in params} == {"input", "threads"}
    assert groups[0]["n_params"] == 2
    assert [f["code"] for f in flags] == ["PARAMS_FROM_CONFIG"]


def test_a_nested_block_does_not_end_the_params_block_early(tmp_path):
    (tmp_path / "nextflow.config").write_text(
        "params {\n  fastp { qualified_quality_phred = 15 }\n  threads = 4\n}\n")
    params, _groups = WS._params_from_config(tmp_path, [])
    assert {p["name"] for p in params} == {"threads"}


def test_an_unterminated_params_block_is_reported_not_guessed(tmp_path):
    """Without an explicit check this read the rest of the file as the block body."""
    (tmp_path / "nextflow.config").write_text("params {\n  input = 'in.csv'\n")
    flags = []
    assert WS._params_from_config(tmp_path, flags) == ([], [])
    assert [f["code"] for f in flags] == ["PARAMS_BLOCK_UNTERMINATED"]


def test_a_params_block_at_the_very_end_of_the_file_is_not_a_crash(tmp_path):
    (tmp_path / "nextflow.config").write_text("params {")
    flags = []
    assert WS._params_from_config(tmp_path, flags) == ([], [])
    assert [f["code"] for f in flags] == ["PARAMS_BLOCK_UNTERMINATED"]


SCHEMA_INPUT = {
    "type": "array", "items": {"type": "object", "required": ["sample"], "properties": {
        "sample": {"type": "string", "pattern": "^\\S+$",
                   "errorMessage": "Sample names cannot contain spaces"},
        "fastq_1": {"type": "string", "format": "file-path", "exists": True},
        "run_dir": {"type": "string", "format": "directory-path"},
        "expected_cells": {"type": ["integer", "null"], "minimum": 1},
        "strandedness": {"type": "string", "enum": ["auto", "forward"]},
        "lanes": {"type": "array", "items": {"type": "integer"}}}}}


def _repo(tmp_path, *, schema_at="assets/schema_input.json"):
    (tmp_path / schema_at).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / schema_at).write_text(json.dumps(SCHEMA_INPUT))
    return tmp_path


def test_samplesheet_columns_are_reported_in_the_design_specs_vocabulary(tmp_path):
    """The columns become a structured input's schema, so their pickers, patterns and
    choices have to survive the read, spelled as the spec spells them -- and a column
    holding a list cannot be built."""
    columns = {c["name"]: c for c in WS._table_columns(
        _repo(tmp_path), "assets/schema_input.json", [])}
    assert columns["sample"]["required"] is True
    assert columns["sample"]["pattern"] == "^\\S+$"
    assert columns["sample"]["error_message"] == "Sample names cannot contain spaces"
    assert columns["fastq_1"]["type"] == "file"
    assert "format" not in columns["fastq_1"] and "exists" not in columns["fastq_1"]
    assert columns["run_dir"]["type"] == "folder"
    assert columns["expected_cells"]["type"] == "integer", "null only makes it optional"
    assert columns["expected_cells"]["minimum"] == 1
    assert columns["strandedness"]["enum"] == ["auto", "forward"]
    assert columns["lanes"]["unsupported"]
    assert "unsupported" not in columns["sample"]


def test_a_parameter_declaring_its_samplesheet_schema_carries_that_schemas_columns(
        tmp_path):
    """Each parameter's own schema, not one fixed path: a second table would otherwise
    have been handed the first one's columns."""
    root = _repo(tmp_path, schema_at="assets/contrasts_schema.json")
    entry = WS._param_entry("contrasts", {"type": "string", "format": "file-path",
                                          "schema": "assets/contrasts_schema.json"},
                            "input_output_options", True)
    assert entry["schema"] == "assets/contrasts_schema.json"
    hints = WS.candidates([entry], WS._tables(root, [entry], []))
    [hint] = hints["structured_input"]
    assert hint["name"] == "contrasts"
    assert hint["columns_from"] == "assets/contrasts_schema.json"
    assert [c["name"] for c in hint["columns"]][:2] == ["sample", "fastq_1"]
    assert "primary input" in hint["why"]
    assert not hints["primary_data_input"], "one parameter, one suggestion"


@pytest.mark.parametrize("spec", [
    {"type": "string", "format": "file-path", "mimetype": "text/csv"},
    {"type": "string", "format": "file-path", "pattern": "^\\S+\\.tsv$"},
    {"type": "string", "default": "design.csv"},
])
def test_a_csv_parameter_with_no_schema_is_still_a_structured_input(spec):
    """Without a schema it used to be offered as a plain file picker, and the person
    had to build the CSV by hand."""
    entry = WS._param_entry("design", spec, "g", False)
    assert entry["is_path"] and entry["table_evidence"]
    hints = WS.candidates([entry], {})
    [hint] = hints["structured_input"]
    assert "read them from the code" in hint["why"]
    assert "columns" not in hint
    assert not hints["file_pickers"]


def test_a_table_named_by_no_schema_takes_nf_cores_default_schema_as_a_guess(tmp_path):
    """nf-core pipelines from before nf-schema keep the columns there and name nothing."""
    root = _repo(tmp_path)
    entry = WS._param_entry("input", {"type": "string", "format": "file-path",
                                      "mimetype": "text/csv"}, "g", True)
    [hint] = WS.candidates([entry], WS._tables(root, [entry], []))["structured_input"]
    assert hint["columns_from"] == "assets/schema_input.json" and hint["columns"]
    assert "confirm" in hint["why"]

    other = WS._param_entry("contrasts", {"type": "string", "format": "file-path",
                                          "mimetype": "text/csv"}, "g", False)
    assert WS._tables(root, [entry, other], []) == {}, \
        "with two tables, which one the file describes is not a guess worth making"


def test_a_samplesheet_schema_outside_the_repository_is_not_read(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _repo(tmp_path, schema_at="outside.json")
    flags = []
    assert WS._table_columns(root, "../outside.json", flags) is None
    assert [f["code"] for f in flags] == ["SAMPLESHEET_SCHEMA_MISSING"]


def test_a_path_named_like_a_sheet_is_a_structured_input():
    entry = WS._param_entry("sample_sheet", {"type": "string", "format": "file-path"},
                            "g", False)
    [hint] = WS.candidates([entry], {})["structured_input"]
    assert "name denotes a sample sheet" in hint["why"]


def test_every_path_format_cirro_reads_is_a_path():
    for fmt in ("file-path", "file-path-pattern", "directory-path", "path"):
        assert WS._param_entry("x", {"type": "string", "format": fmt}, "g",
                               False)["is_path"], fmt

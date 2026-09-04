"""Reading a workflow's own declarations. No tenant, no network."""
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

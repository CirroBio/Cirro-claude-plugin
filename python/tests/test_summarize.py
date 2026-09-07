"""Summarizers: the shapes that keep large things out of a context window."""
import pytest

from cirro_agent import summarize as S


# --- files ---

def fastq_manifest(n_samples=240):
    entries = [{"path": f"data/SAMP{i:03d}_R{r}_001.fastq.gz", "size": 3_000_000_000 + i}
               for i in range(n_samples) for r in (1, 2)]
    entries.append({"path": "multiqc/multiqc_report.html", "size": 4_200_000})
    entries.append({"path": "star/Log.final.out"})  # size absent, as the API allows
    return entries


def test_name_patterns_collapse_a_manifest_into_templates():
    patterns = S.name_patterns([e["path"] for e in fastq_manifest()])
    top = patterns[0]
    assert top["template"] == "data/{sample}_R{1,2}_001.fastq.gz"
    assert top["n"] == 480
    assert top["glob"] == "data/*_R*_001.fastq.gz"


def test_a_small_varying_token_set_is_enumerated_not_generalized():
    patterns = S.name_patterns(["a_R1.fq", "a_R2.fq"])
    assert patterns[0]["template"] == "a_R{1,2}.fq", \
        "a token that never varies stays literal"
    many = S.name_patterns([f"s{i}_R{r}.fq" for i in range(9) for r in (1, 2)])
    assert many[0]["template"] == "{sample}_R{1,2}.fq", \
        "a wide varying token generalizes; a narrow one enumerates"


def test_files_never_implies_a_complete_byte_total():
    body = S.files(fastq_manifest(), total_files=482)
    assert body["bytes_known_for"] == "481 of 482 listed files"


def test_files_reports_what_it_did_not_list():
    body = S.files(fastq_manifest()[:10], total_files=482)
    assert "472 files beyond the 10 listed" in body["note"]


def test_extensions_are_ranked_and_compound_suffixes_preserved():
    body = S.files(fastq_manifest())
    assert body["extensions"][0]["ext"] == ".fastq.gz"
    assert body["extensions"][0]["n"] == 480


@pytest.mark.parametrize("path,ext", [
    ("a/b.fastq.gz", ".fastq.gz"), ("a/b.vcf.bgz", ".vcf.bgz"), ("a/b.txt", ".txt"),
    ("a/b.tar.gz", ".tar.gz"), ("a/Log.final.out", ".out"), ("a/README", ""),
])
def test_split_ext(path, ext):
    assert S.split_ext(path) == ext


def test_suggested_globs_carry_their_match_counts():
    body = S.files(fastq_manifest())
    assert body["suggested_globs"][0]["matches"] == 480


# --- logs ---

def noisy_log(repeats=4000):
    lines = [f"[{i}] Submitted process > FASTQC (sample_{i})" for i in range(repeats)]
    lines += ["ERROR ~ Error executing process > 'FASTQC (sample_41)'",
              "Caused by:", "  Process exceeded memory limit (16 GB)",
              "Command exit status: 137"]
    return "\n".join(lines)


def test_repeated_lines_collapse_to_one_row_with_a_count():
    body = S.logs(noisy_log())
    signatures = {row["signature"]: row["n"] for row in body["signature_counts"]}
    assert 4000 in signatures.values()


def test_digits_fold_even_after_an_underscore():
    """A word-boundary pattern would leave sample_41 unfolded and every line unique."""
    first = S.log_signature("Submitted process > FASTQC (sample_41)")
    second = S.log_signature("Submitted process > FASTQC (sample_42)")
    assert first == second


def test_error_blocks_are_extracted_with_line_numbers():
    body = S.logs(noisy_log())
    assert body["error_blocks"]
    block = body["error_blocks"][0]
    assert block["first_line_no"] == 4001
    assert "ERROR ~" in block["excerpt"][0]


def test_an_empty_log_warns_about_the_sdk_cache():
    body = S.logs("")
    assert body["empty"] is True
    assert "EMPTY_LOG_MAY_BE_TRANSIENT" in body["note"]


def test_tail_is_kept_for_failures():
    body = S.logs(noisy_log(), head=5, tail=10)
    assert len(body["head"]) == 5
    assert "Command exit status: 137" in body["tail"][-1]


# --- tasks ---

def fanout_tasks(n=480, failures=(0,)):
    return [{"task_id": i, "name": f"FASTQC (sample_{i})",
             "status": "FAILED" if i in failures else "COMPLETED",
             "exit_code": 137 if i in failures else 0,
             "started_at": "2026-08-24T04:00:00Z", "stopped_at": "2026-08-24T04:05:00Z",
             "status_message": "OOM" if i in failures else None} for i in range(n)]


def test_per_sample_tasks_collapse_by_process():
    body = S.tasks(fanout_tasks())
    assert len(body["counts_by_process"]) == 1
    assert body["counts_by_process"][0] == {
        "process": "FASTQC", "n": 480, "failed": 1,
        "duration_p50_s": 300.0, "duration_p95_s": 300.0}


def test_the_root_cause_task_is_listed_first():
    body = S.tasks(fanout_tasks(failures=(5, 9)), primary_failed_index=9)
    assert body["failed"][0]["task_id"] == 9
    assert body["failed"][0]["is_root_cause"] is True


def test_task_process_strips_the_tag_suffix():
    assert S.task_process("STAR_ALIGN (sample_1)") == "STAR_ALIGN"
    assert S.task_process("MULTIQC") == "MULTIQC"


def test_no_tasks_is_stated_not_implied():
    assert S.tasks([])["n_tasks"] == 0
    assert "no tasks yet" in S.tasks([])["note"]


# --- samples ---

def messy_samples(n=40):
    return [{"id": f"s{i}", "name": f"S{i}",
             "metadata": {"condition": ["Tumor", "tumor", " Tumor", "Normal"][i % 4],
                          "age": str(20 + i % 40), "batch": f"b{i % 3}"}}
            for i in range(n)]


def test_group_key_candidates_are_the_ones_worth_grouping_on():
    body = S.samples(messy_samples())
    assert set(body["candidate_group_keys"]) == {"batch", "condition"}


def test_case_and_whitespace_variants_are_surfaced():
    body = S.samples(messy_samples())
    kinds = {(i["key"], i["kind"]) for i in body["inconsistencies"]}
    assert ("condition", "case_or_whitespace_variants") in kinds


def test_schema_drift_is_reported_in_both_directions():
    body = S.samples(messy_samples(), schema_keys=["condition", "age", "sex"])
    assert body["schema_drift"]["declared_not_observed"] == ["sex"]
    assert body["schema_drift"]["observed_not_declared"] == ["batch"]


def test_numeric_keys_get_a_numeric_summary():
    body = S.samples(messy_samples())
    age = next(k for k in body["keys"] if k["key"] == "age")
    assert age["dtype_inferred"] == "numeric"
    assert age["numeric_summary"]["min"] == 20.0


def test_samples_returns_examples_not_the_table():
    body = S.samples(messy_samples(400))
    assert len(body["examples"]) == 3
    assert body["n_samples"] == 400


# --- params ---

def _field(path, **over):
    field = {"path": path, "type": "string", "required": False, "conditional": False,
             "is_group": False, "default": None, "has_default": False}
    field.update(over)
    return field


def test_only_departures_from_default_are_shown_in_full():
    body = S.params({"aligner": "star", "genome": "GRCh38", "skip_qc": False},
                    [_field("aligner", default="star", has_default=True),
                     _field("genome"),
                     _field("skip_qc", type="boolean", default=False,
                            has_default=True)])
    assert body["non_default"] == {"genome": "GRCh38"}
    assert body["n_defaulted"] == 2


def test_undeclared_params_are_flagged_because_they_do_nothing():
    body = S.params({"typo_param": 1}, [_field("aligner")])
    assert body["unknown"] == ["typo_param"]
    assert "typo" in body["unknown_note"]


def test_missing_required_params_are_listed():
    body = S.params({"genome": "GRCh38"},
                    [_field("genome", required=True), _field("outdir", required=True)])
    assert body["missing_required"] == ["outdir"]


def test_a_groups_own_key_is_not_an_undeclared_typo():
    """The group containers of a successful run were reported as typos the pipeline
    ignores, because the declared list held only leaf names."""
    fields = [_field("qc_options", type="object", is_group=True),
              _field("qc_options.skip_fastqc", type="boolean", default=False,
                     has_default=True)]
    body = S.params({"qc_options": {"skip_fastqc": True}}, fields)
    assert body["unknown"] == []
    assert body["non_default"] == {"qc_options.skip_fastqc": True}


def test_a_nested_parameter_sent_flat_is_named_with_where_it_belongs():
    """The form does not set additionalProperties:false, so a flat key validates and
    is then ignored -- the run silently uses the default instead."""
    fields = [_field("qc_options", type="object", is_group=True),
              _field("qc_options.skip_fastqc", type="boolean", default=False,
                     has_default=True)]
    body = S.params({"skip_fastqc": True}, fields)
    assert body["unknown"] == ["skip_fastqc"]
    assert body["misplaced"] == {"skip_fastqc": "qc_options.skip_fastqc"}


def test_a_free_form_object_parameter_is_one_value_not_one_per_key():
    """Only a declared group is descended into; a parameter that legitimately takes an
    object is a single value."""
    body = S.params({"extra": {"a": 1, "b": 2}}, [_field("extra", type="object")])
    assert body["unknown"] == []
    assert body["non_default"] == {"extra": {"a": 1, "b": 2}}


def test_a_requirement_inside_an_absent_group_is_not_reported_missing():
    fields = [_field("opts", type="object", is_group=True),
              _field("opts.mode", required=True)]
    assert S.params({}, fields)["missing_required"] == []
    assert S.params({"opts": {}}, fields)["missing_required"] == ["opts.mode"]


def test_secret_shaped_keys_are_redacted():
    body = S.params({"api_key": "abc", "password": "x", "aws_secret": "y"})
    assert set(body["non_default"].values()) == {"<redacted>"}


def test_long_values_are_clipped_with_their_true_length():
    body = S.params({"x": "y" * 5000}, [_field("x")])
    assert body["non_default"]["x"]["truncated"] is True
    assert body["non_default"]["x"]["full_len"] == 5000


# --- trace ---

def test_trace_flags_over_provisioning_and_headroom():
    rows = ([{"name": "STAR (s)", "realtime": 600, "peak_rss": 30_000_000_000,
              "memory": 32_000_000_000, "%cpu": 780}] * 5
            + [{"name": "FASTQC (s)", "realtime": 30, "peak_rss": 500_000_000,
                "memory": 8_000_000_000, "%cpu": 95}] * 5)
    body = S.trace(rows)
    flags = {p["process"]: p.get("flag") for p in body["processes"]}
    assert flags["STAR"] == "near_limit"
    assert flags["FASTQC"] == "over_provisioned"


def test_no_trace_is_stated_not_implied():
    assert "no trace file" in S.trace([])["note"]


@pytest.mark.parametrize("size,text", [
    (0, "0 B"), (512, "512 B"), (4_200_000, "4.2 MB"),
    (41203118, "41.2 MB"), (3_000_000_000, "3.0 GB"),
])
def test_human_bytes(size, text):
    assert S.human_bytes(size) == text


def test_a_log_between_head_and_head_plus_tail_loses_nothing():
    """Fifty lines with head=30, tail=60 used to show 30 lines and an empty tail."""
    text = "\n".join(f"line {i}" for i in range(50))
    body = S.logs(text, head=30, tail=60)
    shown = body["head"] + body["tail"]
    assert len(shown) == 50, f"{len(shown)} of 50 lines shown"
    assert shown[-1] == "line 49"

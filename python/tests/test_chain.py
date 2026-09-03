"""Approving several build-up operations as one action.

The property under test is that a chain does not widen what the person authorized:
arguments a later step cannot know yet are references to an earlier step's recorded
output, never wildcards, and nothing the caller supplies at execution time reaches an
approved argument.
"""
import pytest

from cirro_agent import errors
from cirro_agent.worker import mutations


def _steps(**overrides):
    steps = [
        {"label": "register", "op": "create_process",
         "args": {"design_spec": "/tmp/spec.md", "config_source": "STORAGE"}},
        {"label": "upload", "op": "upload_dataset",
         "args": {"project_id": "proj_a", "process": "proc_ingest",
                  "name": "Test data", "source_dir": "/tmp/data"}},
        {"label": "run", "op": "run_analysis",
         "args": {"project_id": "proj_a",
                  "process": {"from_step": 0, "field": "created_process_id"},
                  "name": "Test run",
                  "source_dataset_ids": [{"from_step": 1, "field": "dataset_id"}]}},
    ]
    return {"steps": steps, **overrides}


def _plan(**overrides):
    from unittest.mock import MagicMock
    return mutations.plan_chain(MagicMock(), _steps(**overrides))


# --- references are dataflow edges, not wildcards ----------------------------

def test_a_reference_is_resolved_from_the_recorded_output_of_an_earlier_step():
    outputs = [{"created_process_id": "process-abc"}, {"dataset_id": "ds_new"}]
    resolved = mutations._resolve_references(
        {"process": {"from_step": 0, "field": "created_process_id"},
         "source_dataset_ids": [{"from_step": 1, "field": "dataset_id"}],
         "name": "Test run"}, outputs)
    assert resolved == {"process": "process-abc",
                        "source_dataset_ids": ["ds_new"], "name": "Test run"}


def test_a_reference_may_only_read_an_earlier_step():
    with pytest.raises(errors.AgentError) as caught:
        mutations._chain_steps({"steps": [
            {"op": "run_analysis", "args": {
                "source_dataset_ids": [{"from_step": 1, "field": "dataset_id"}]}},
            {"op": "upload_dataset", "args": {}},
        ]})
    assert "not an earlier step" in caught.value.message


def test_a_reference_must_name_a_field_the_earlier_step_produces():
    """Refused at plan time, rather than failing halfway through an approved chain."""
    with pytest.raises(errors.AgentError) as caught:
        mutations._chain_steps({"steps": [
            {"op": "upload_dataset", "args": {}},
            {"op": "run_analysis", "args": {
                "source_dataset_ids": [{"from_step": 0, "field": "nonesuch"}]}},
        ]})
    assert "does not produce 'nonesuch'" in caught.value.message
    assert {"field": "dataset_id"} in caught.value.candidates


def test_every_declared_output_is_a_field_the_operation_really_returns():
    """A declaration nothing checks is a promise a chain would break at step 3."""
    from test_request_models import APPLY_CASES
    from unittest.mock import MagicMock
    for name, op in sorted(mutations.OPS.items()):
        if not op.outputs or name not in APPLY_CASES:
            continue
        result = op.apply(MagicMock(), dict(APPLY_CASES[name]))
        for field in op.outputs:
            assert field in result, f"{name} declares {field} but does not return it"


# --- what the person reads ---------------------------------------------------

def test_the_approval_names_every_derivation_for_what_it_derives_from():
    draft = _plan()
    assert "<created_process_id of step 1: register>" in draft.summary
    assert "<dataset_id of step 2: upload>" in draft.summary
    assert "'/tmp/data'" in draft.summary, "concrete values stay concrete"


def test_steps_with_deferred_figures_are_called_out():
    draft = _plan()
    assert any("re-resolved and re-validated" in w for w in draft.warnings)


def test_the_chain_risk_widens_when_a_step_writes_local_disk():
    plain = _plan()
    assert plain.risk == mutations.RISK_WRITE
    with_local = mutations.plan_chain(None, {"steps": [
        {"op": "upload_dataset", "args": {"source_dir": "/tmp/d"}},
        {"op": "download_files", "args": {"dataset_id": "ds_1"}},
    ]})
    assert with_local.risk == mutations.RISK_WRITE_LOCAL


# --- what a chain refuses ----------------------------------------------------

def test_a_destructive_step_cannot_hide_inside_a_chain():
    """One "yes" must never authorize a deletion the person was not looking at."""
    with pytest.raises(errors.AgentError) as caught:
        mutations._chain_steps({"steps": [
            {"op": "upload_dataset", "args": {}},
            {"op": "delete_dataset", "args": {"dataset_id": "ds_1"}},
        ]})
    assert caught.value.code == "INVALID_QUERY"
    assert "destructive" in caught.value.message


def test_a_chain_cannot_contain_a_chain():
    with pytest.raises(errors.AgentError):
        mutations._chain_steps({"steps": [{"op": "chain", "args": {}}]})


def test_an_empty_chain_is_refused():
    with pytest.raises(errors.AgentError) as caught:
        mutations._chain_steps({"steps": []})
    assert "at least one step" in caught.value.message


def test_a_chain_has_a_step_ceiling():
    many = [{"op": "upload_dataset", "args": {}}] * (mutations.CHAIN_MAX_STEPS + 1)
    with pytest.raises(errors.AgentError) as caught:
        mutations._chain_steps({"steps": many})
    assert "at most" in caught.value.message


def test_duplicate_labels_are_refused():
    with pytest.raises(errors.AgentError) as caught:
        mutations._chain_steps({"steps": [
            {"label": "same", "op": "upload_dataset", "args": {}},
            {"label": "same", "op": "run_analysis", "args": {}},
        ]})
    assert "labelled" in caught.value.message


# --- the retry policy is a policy, not a value -------------------------------

def test_a_retry_policy_is_bounded_and_says_what_may_change():
    draft = _plan(retry={"redo": ["run"], "max_attempts": 3,
                         "may_change": ["design_spec"]})
    assert "up to 3 attempts" in draft.summary
    assert "may change only: design_spec" in draft.summary
    assert "everything else stays exactly as approved" in draft.summary
    assert any("consumes compute" in w for w in draft.warnings)


def test_an_unbounded_retry_is_refused():
    for attempts in (None, 1, 99):
        with pytest.raises(errors.AgentError) as caught:
            _plan(retry={"redo": ["run"], "max_attempts": attempts,
                         "may_change": ["design_spec"]})
        assert "max_attempts" in str(caught.value.field)


def test_a_retry_that_may_change_anything_is_refused():
    with pytest.raises(errors.AgentError) as caught:
        _plan(retry={"redo": ["run"], "max_attempts": 3})
    assert "may_change" in str(caught.value.field)


def test_retry_redo_must_name_labelled_steps():
    with pytest.raises(errors.AgentError) as caught:
        _plan(retry={"redo": ["nonesuch"], "max_attempts": 3,
                     "may_change": ["design_spec"]})
    assert "nonesuch" in caught.value.message


def test_halting_on_a_repeated_failure_is_the_default():
    draft = _plan(retry={"redo": ["run"], "max_attempts": 3,
                         "may_change": ["design_spec"]})
    assert "fails the same way twice" in draft.summary


# --- resuming a chain inside its approved policy -----------------------------

def _prior(tmp_path, steps=None, retry=None, completed=None):
    """A completed chain on disk, with the outputs its steps recorded."""
    from cirro_agent import plans
    from cirro_agent.session import Session
    session = Session(base_url="app.cirro.test", dir=str(tmp_path))
    (tmp_path / "plans").mkdir(parents=True, exist_ok=True)
    plan = plans.create(
        session, op="chain",
        args={"steps": steps if steps is not None else _steps()["steps"],
              "retry": retry if retry is not None else
              {"redo": ["configure", "run"], "max_attempts": 3,
               "may_change": ["design_spec"]}},
        summary="prior chain", username="sminot", risk="cirro_write")
    plans.claim(session, plan.plan_id, digest=plan.digest,
                approved_summary=plan.summary, username="sminot")
    plans.finish(session, plan, result={"completed": completed or []})
    return session, plan


def _labelled_steps():
    return [
        {"label": "upload", "op": "upload_dataset",
         "args": {"project_id": "proj_a", "process": "proc_ingest",
                  "name": "Test data", "source_dir": "/tmp/data"}},
        {"label": "configure", "op": "upload_process_config",
         "args": {"process_id": "process-x", "design_spec": "/work/spec.md"}},
        {"label": "run", "op": "run_analysis",
         "args": {"project_id": "proj_a", "process": "process-x", "name": "run",
                  "source_dataset_ids": [{"from_step": 0, "field": "dataset_id"}]}},
    ]


def _reader_for(session):
    from unittest.mock import MagicMock
    reader = MagicMock()
    reader.base_url = session.base_url
    reader.session_dir = str(session.path)
    return reader


def test_a_resumed_chain_reuses_what_earlier_steps_produced(tmp_path):
    """The point of resuming: attempt 2 does not upload the test data again."""
    session, plan = _prior(
        tmp_path, steps=_labelled_steps(),
        completed=[{"step": 1, "label": "upload",
                    "result": {"dataset_id": "ds_uploaded"}},
                   {"step": 2, "label": "configure", "result": {}},
                   {"step": 3, "label": "run", "result": {}}])
    draft = mutations.plan_chain(_reader_for(session), {
        "restart_from": {"plan_id": plan.plan_id, "label": "configure", "attempt": 2}})
    assert [s["label"] for s in draft.args["steps"]] == ["configure", "run"]
    assert draft.args["carried"] == [{"dataset_id": "ds_uploaded"}]
    assert "Reusing:    1 earlier step(s)" in draft.summary
    assert "Attempt 2 of 3" in draft.summary


def test_a_resumed_chain_still_resolves_references_into_carried_outputs(tmp_path):
    """Step 3 references step 1, which is not being re-run."""
    resolved = mutations._resolve_references(
        {"source_dataset_ids": [{"from_step": 0, "field": "dataset_id"}]},
        [{"dataset_id": "ds_uploaded"}])
    assert resolved == {"source_dataset_ids": ["ds_uploaded"]}


def test_the_retry_shows_only_what_changed(tmp_path):
    session, plan = _prior(
        tmp_path, steps=_labelled_steps(),
        completed=[{"step": 1, "label": "upload",
                    "result": {"dataset_id": "ds_uploaded"}}])
    changed = [dict(step) for step in _labelled_steps()]
    changed[1]["args"] = {**changed[1]["args"], "design_spec": "/work/spec-v2.md"}
    draft = mutations.plan_chain(_reader_for(session), {
        "restart_from": {"plan_id": plan.plan_id, "label": "configure", "attempt": 2},
        "steps": changed})
    assert "Changed since the last attempt:" in draft.summary
    assert "configure.design_spec" in draft.summary
    assert "'/work/spec-v2.md'" in draft.summary
    assert "upload" not in draft.summary.split("Changed since")[1]


def test_a_retry_cannot_change_a_field_the_policy_pinned(tmp_path):
    """The whole point of may_change: discretion is bounded, not implied."""
    session, plan = _prior(
        tmp_path, steps=_labelled_steps(),
        completed=[{"step": 1, "label": "upload",
                    "result": {"dataset_id": "ds_uploaded"}}])
    changed = [dict(step) for step in _labelled_steps()]
    changed[2]["args"] = {**changed[2]["args"], "project_id": "proj_somewhere_else"}
    with pytest.raises(errors.AgentError) as caught:
        mutations.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "configure",
                             "attempt": 2},
            "steps": changed})
    assert "project_id" in caught.value.message
    assert "does not allow" in caught.value.message


def test_the_attempt_ceiling_is_enforced_on_resume(tmp_path):
    session, plan = _prior(tmp_path, steps=_labelled_steps())
    with pytest.raises(errors.AgentError) as caught:
        mutations.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "configure",
                             "attempt": 4}})
    assert "allows 3" in caught.value.message


def test_resuming_at_a_step_the_policy_does_not_cover_is_refused(tmp_path):
    session, plan = _prior(tmp_path, steps=_labelled_steps())
    with pytest.raises(errors.AgentError) as caught:
        mutations.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "upload", "attempt": 2}})
    assert "re-runs configure, run" in caught.value.message


def test_a_chain_approved_without_a_retry_policy_cannot_be_resumed(tmp_path):
    # `{}` rather than None: the helper reads None as "use the usual policy".
    session, plan = _prior(tmp_path, steps=_labelled_steps(), retry={})
    with pytest.raises(errors.AgentError) as caught:
        mutations.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "configure"}})
    assert "without a retry policy" in caught.value.message


def test_an_identical_retry_is_flagged_as_pointless(tmp_path):
    session, plan = _prior(
        tmp_path, steps=_labelled_steps(),
        completed=[{"step": 1, "label": "upload",
                    "result": {"dataset_id": "ds_uploaded"}}])
    draft = mutations.plan_chain(_reader_for(session), {
        "restart_from": {"plan_id": plan.plan_id, "label": "configure", "attempt": 2}})
    assert any("will fail the same way" in w for w in draft.warnings)


def test_resuming_a_step_whose_predecessors_never_ran_is_refused(tmp_path):
    session, plan = _prior(tmp_path, steps=_labelled_steps(), completed=[])
    with pytest.raises(errors.AgentError) as caught:
        mutations.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "run", "attempt": 2}})
    assert "never completed" in caught.value.message


# --- the executor ------------------------------------------------------------

def _fake_ops(monkeypatch, log, *, fail_at=None, fail_with=None):
    """Two chainable ops whose apply records what it received.

    `producer` returns an id; `consumer` takes it. Both run their planner first, as
    the real executor does, so a reference that failed to resolve would show up as
    a planner refusal rather than as garbage handed to apply.
    """
    def plan_producer(reader, args):
        return mutations.Draft(args=dict(args), summary="produce")

    def apply_producer(reader, args):
        log.append(("producer", args))
        return {"made_id": f"made-{args['tag']}"}

    def plan_consumer(reader, args):
        if not isinstance(args.get("uses"), str) or not args["uses"].startswith("made-"):
            raise errors.InvalidQuery("uses must be a produced id", what="w",
                                      retry_with=[{"note": "n"}])
        return mutations.Draft(args=dict(args), summary="consume")

    def apply_consumer(reader, args):
        log.append(("consumer", args))
        if fail_at == "consumer":
            raise fail_with or RuntimeError("consumer exploded")
        return {"consumed": args["uses"]}

    monkeypatch.setitem(mutations.OPS, "producer", mutations.Op(
        "producer", mutations.RISK_WRITE, plan_producer, apply_producer,
        outputs=("made_id",)))
    monkeypatch.setitem(mutations.OPS, "consumer", mutations.Op(
        "consumer", mutations.RISK_WRITE, plan_consumer, apply_consumer))


def test_the_executor_feeds_each_step_what_the_one_before_it_produced(monkeypatch):
    log = []
    _fake_ops(monkeypatch, log)
    result = mutations.apply_chain(None, {"steps": [
        {"label": "make", "op": "producer", "args": {"tag": "alpha"}},
        {"label": "use", "op": "consumer",
         "args": {"uses": {"from_step": 0, "field": "made_id"}}},
    ]})
    assert log == [("producer", {"tag": "alpha"}), ("consumer", {"uses": "made-alpha"})]
    assert result["n_completed"] == 2 and result["n_steps"] == 2
    assert [s["label"] for s in result["completed"]] == ["make", "use"]
    assert result["completed"][1]["result"] == {"consumed": "made-alpha"}
    assert "failed_at" not in result


def test_a_chain_that_fails_partway_reports_both_halves(monkeypatch):
    """Raising would report the whole chain as failed while step 1 had already run."""
    log = []
    _fake_ops(monkeypatch, log, fail_at="consumer")
    result = mutations.apply_chain(None, {"steps": [
        {"label": "make", "op": "producer", "args": {"tag": "beta"}},
        {"label": "use", "op": "consumer",
         "args": {"uses": {"from_step": 0, "field": "made_id"}}},
        {"label": "never", "op": "producer", "args": {"tag": "gamma"}},
    ]})
    assert result["n_completed"] == 1
    assert result["failed_at"]["step"] == 2 and result["failed_at"]["label"] == "use"
    assert "consumer exploded" in result["failed_at"]["error"]
    assert "steps 3-3 did not run" in result["note"]
    assert [entry[0] for entry in log] == ["producer", "consumer"], \
        "the step after the failure must not run"


def test_a_chain_that_fails_at_its_first_step_is_an_error_because_nothing_changed(
        monkeypatch):
    log = []
    _fake_ops(monkeypatch, log)
    with pytest.raises(errors.AgentError) as caught:
        mutations.apply_chain(None, {"steps": [
            {"label": "use", "op": "consumer", "args": {"uses": "not-a-produced-id"}},
        ]})
    assert caught.value.code == "UPSTREAM_ERROR"
    assert caught.value.user_action == "Nothing was changed."
    assert log == [], "the planner refused, so apply never ran"


def test_a_typed_refusal_mid_chain_keeps_its_code(monkeypatch):
    log = []
    _fake_ops(monkeypatch, log,
              fail_at="consumer",
              fail_with=errors.Forbidden("not yours", what="w", user_action="ask"))
    result = mutations.apply_chain(None, {"steps": [
        {"op": "producer", "args": {"tag": "d"}},
        {"op": "consumer", "args": {"uses": {"from_step": 0, "field": "made_id"}}},
    ]})
    assert result["failed_at"]["code"] == "FORBIDDEN"


def test_a_resumed_chain_starts_from_the_carried_outputs(monkeypatch):
    """Step numbering and references both continue from where the earlier run stopped."""
    log = []
    _fake_ops(monkeypatch, log)
    result = mutations.apply_chain(None, {
        "carried": [{"made_id": "made-from-before"}],
        "steps": [
            {"label": "use", "op": "consumer",
             "args": {"uses": {"from_step": 0, "field": "made_id"}}},
        ]})
    assert log == [("consumer", {"uses": "made-from-before"})]
    assert result["completed"][0]["step"] == 2, "numbered after the carried step"


def test_the_executor_never_takes_a_value_from_the_caller_for_a_reference(monkeypatch):
    """The substitution source is the recorded output list and nothing else."""
    log = []
    _fake_ops(monkeypatch, log)
    result = mutations.apply_chain(None, {"steps": [
        {"op": "producer", "args": {"tag": "real"}},
        {"op": "consumer", "args": {"uses": {"from_step": 0, "field": "made_id",
                                             "value": "made-injected"}}},
    ]})
    assert log[1] == ("consumer", {"uses": "made-real"})
    assert result["n_completed"] == 2

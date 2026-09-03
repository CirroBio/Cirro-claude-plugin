"""Approving several build-up operations as one action.

The property under test is that a chain does not widen what the person authorized:
arguments a later step cannot know yet are references to an earlier step's recorded
output, never wildcards, and nothing the caller supplies at execution time reaches an
approved argument.
"""
import pytest

from cirro_agent import errors
from cirro_agent.worker import chain_ops, mutations


def _steps(**overrides):
    steps = [
        {"label": "register", "op": "producer", "args": {"tag": "proc"}},
        {"label": "upload", "op": "producer", "args": {"tag": "data"}},
        {"label": "run", "op": "consumer",
         "args": {"uses": {"from_step": 1, "field": "made_id"},
                  "also": {"from_step": 0, "field": "made_id"}}},
    ]
    return {"steps": steps, **overrides}


def _plan(**overrides):
    return chain_ops.plan_chain(None, _steps(**overrides))


@pytest.fixture(autouse=True)
def fake_ops(monkeypatch):
    """Two chainable ops plus a local-write one, registered for every test here."""
    log = []
    _fake_ops(monkeypatch, log)
    monkeypatch.setitem(mutations.OPS, "local_producer", mutations.Op(
        "local_producer", mutations.RISK_WRITE_LOCAL,
        lambda reader, a: mutations.Draft(args=dict(a), summary="write locally"),
        lambda reader, a: {"made_id": "local"}, outputs=("made_id",)))
    return log


# --- references are dataflow edges, not wildcards ----------------------------

def test_a_reference_is_resolved_from_the_recorded_output_of_an_earlier_step():
    outputs = [{"created_process_id": "process-abc"}, {"dataset_id": "ds_new"}]
    resolved = chain_ops._resolve_references(
        {"process": {"from_step": 0, "field": "created_process_id"},
         "source_dataset_ids": [{"from_step": 1, "field": "dataset_id"}],
         "name": "Test run"}, outputs)
    assert resolved == {"process": "process-abc",
                        "source_dataset_ids": ["ds_new"], "name": "Test run"}


def test_a_reference_may_only_read_an_earlier_step():
    with pytest.raises(errors.AgentError) as caught:
        chain_ops._chain_steps({"steps": [
            {"op": "run_analysis", "args": {
                "source_dataset_ids": [{"from_step": 1, "field": "dataset_id"}]}},
            {"op": "upload_dataset", "args": {}},
        ]})
    assert "not an earlier step" in caught.value.message


def test_a_reference_must_name_a_field_the_earlier_step_produces():
    """Refused at plan time, rather than failing halfway through an approved chain."""
    with pytest.raises(errors.AgentError) as caught:
        chain_ops._chain_steps({"steps": [
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
    assert "<made_id of step 1: register>" in draft.summary
    assert "<made_id of step 2: upload>" in draft.summary
    assert "produce" in draft.summary, "a planned step shows its planner's text"


def test_steps_with_deferred_figures_are_called_out():
    draft = _plan()
    assert "resolved when this step runs" in draft.summary


def test_the_chain_risk_widens_when_a_step_writes_local_disk():
    plain = _plan()
    assert plain.risk == mutations.RISK_WRITE
    with_local = chain_ops.plan_chain(None, {"steps": [
        {"op": "producer", "args": {"tag": "a"}},
        {"op": "local_producer", "args": {"tag": "b"}},
    ]})
    assert with_local.risk == mutations.RISK_WRITE_LOCAL, \
        "an op that is itself cirro_write+local_write must widen the chain"


# --- what a chain refuses ----------------------------------------------------

def test_a_destructive_step_cannot_hide_inside_a_chain():
    """One "yes" must never authorize a deletion the person was not looking at."""
    with pytest.raises(errors.AgentError) as caught:
        chain_ops._chain_steps({"steps": [
            {"op": "upload_dataset", "args": {}},
            {"op": "delete_dataset", "args": {"dataset_id": "ds_1"}},
        ]})
    assert caught.value.code == "INVALID_QUERY"
    assert "destructive" in caught.value.message


def test_a_chain_cannot_contain_a_chain():
    with pytest.raises(errors.AgentError):
        chain_ops._chain_steps({"steps": [{"op": "chain", "args": {}}]})


def test_an_empty_chain_is_refused():
    with pytest.raises(errors.AgentError) as caught:
        chain_ops._chain_steps({"steps": []})
    assert "at least one step" in caught.value.message


def test_a_chain_has_a_step_ceiling():
    many = [{"op": "upload_dataset", "args": {}}] * (chain_ops.CHAIN_MAX_STEPS + 1)
    with pytest.raises(errors.AgentError) as caught:
        chain_ops._chain_steps({"steps": many})
    assert "at most" in caught.value.message


def test_duplicate_labels_are_refused():
    with pytest.raises(errors.AgentError) as caught:
        chain_ops._chain_steps({"steps": [
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
    draft = chain_ops.plan_chain(_reader_for(session), {
        "restart_from": {"plan_id": plan.plan_id, "label": "configure", "attempt": 2}})
    assert [s["label"] for s in draft.args["steps"]] == ["configure", "run"]
    assert draft.args["carried"] == [{"dataset_id": "ds_uploaded"}]
    assert "Reusing from the approved run:" in draft.summary
    assert "dataset_id = 'ds_uploaded'" in draft.summary
    assert "Attempt 2 of 3" in draft.summary


def test_a_resumed_chain_still_resolves_references_into_carried_outputs(tmp_path):
    """Step 3 references step 1, which is not being re-run."""
    resolved = chain_ops._resolve_references(
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
    draft = chain_ops.plan_chain(_reader_for(session), {
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
        chain_ops.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "configure",
                             "attempt": 2},
            "steps": changed})
    assert "project_id" in caught.value.message
    assert "does not allow" in caught.value.message


def test_a_caller_supplied_attempt_number_is_ignored(tmp_path):
    """The count is derived from finished attempts; the caller's figure is not consulted."""
    session, plan = _prior(
        tmp_path, steps=_labelled_steps(),
        completed=[{"step": 1, "label": "upload",
                    "result": {"dataset_id": "ds_uploaded"}}])
    draft = chain_ops.plan_chain(_reader_for(session), {
        "restart_from": {"plan_id": plan.plan_id, "label": "configure", "attempt": 4}})
    assert draft.args["attempt"] == 2
    assert "Attempt 2 of 3" in draft.summary


def test_resuming_at_a_step_the_policy_does_not_cover_is_refused(tmp_path):
    session, plan = _prior(tmp_path, steps=_labelled_steps())
    with pytest.raises(errors.AgentError) as caught:
        chain_ops.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "upload", "attempt": 2}})
    assert "re-runs configure, run" in caught.value.message


def test_a_chain_approved_without_a_retry_policy_cannot_be_resumed(tmp_path):
    # `{}` rather than None: the helper reads None as "use the usual policy".
    session, plan = _prior(tmp_path, steps=_labelled_steps(), retry={})
    with pytest.raises(errors.AgentError) as caught:
        chain_ops.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "configure"}})
    assert "without a retry policy" in caught.value.message


def test_an_identical_retry_is_flagged_as_pointless(tmp_path):
    session, plan = _prior(
        tmp_path, steps=_labelled_steps(),
        completed=[{"step": 1, "label": "upload",
                    "result": {"dataset_id": "ds_uploaded"}}])
    draft = chain_ops.plan_chain(_reader_for(session), {
        "restart_from": {"plan_id": plan.plan_id, "label": "configure", "attempt": 2}})
    assert any("will fail the same way" in w for w in draft.warnings)


def test_resuming_a_step_whose_predecessors_never_ran_is_refused(tmp_path):
    session, plan = _prior(tmp_path, steps=_labelled_steps(), completed=[])
    with pytest.raises(errors.AgentError) as caught:
        chain_ops.plan_chain(_reader_for(session), {
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


def test_the_executor_feeds_each_step_what_the_one_before_it_produced(fake_ops):
    log = fake_ops
    result = chain_ops.apply_chain(None, {"steps": [
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
    result = chain_ops.apply_chain(None, {"steps": [
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
        fake_ops):
    log = fake_ops
    with pytest.raises(errors.AgentError) as caught:
        chain_ops.apply_chain(None, {"steps": [
            {"label": "use", "op": "consumer", "args": {"uses": "not-a-produced-id"}},
        ]})
    assert caught.value.code == "INVALID_QUERY", "the planner's refusal, not RETRYABLE"
    assert caught.value.detail["failed_at"]["step"] == 1
    assert log == [], "the planner refused, so apply never ran"


def test_a_typed_refusal_mid_chain_keeps_its_code(monkeypatch):
    log = []
    _fake_ops(monkeypatch, log,
              fail_at="consumer",
              fail_with=errors.Forbidden("not yours", what="w", user_action="ask"))
    result = chain_ops.apply_chain(None, {"steps": [
        {"op": "producer", "args": {"tag": "d"}},
        {"op": "consumer", "args": {"uses": {"from_step": 0, "field": "made_id"}}},
    ]})
    assert result["failed_at"]["code"] == "FORBIDDEN"


def test_a_resumed_chain_starts_from_the_carried_outputs(fake_ops):
    """Step numbering and references both continue from where the earlier run stopped."""
    log = fake_ops
    result = chain_ops.apply_chain(None, {
        "carried": [{"made_id": "made-from-before"}],
        "steps": [
            {"label": "use", "op": "consumer",
             "args": {"uses": {"from_step": 0, "field": "made_id"}}},
        ]})
    assert log == [("consumer", {"uses": "made-from-before"})]
    assert result["completed"][0]["step"] == 2, "numbered after the carried step"


def test_the_executor_never_takes_a_value_from_the_caller_for_a_reference(fake_ops):
    """The substitution source is the recorded output list and nothing else."""
    log = fake_ops
    result = chain_ops.apply_chain(None, {"steps": [
        {"op": "producer", "args": {"tag": "real"}},
        {"op": "consumer", "args": {"uses": {"from_step": 0, "field": "made_id",
                                             "value": "made-injected"}}},
    ]})
    assert log[1] == ("consumer", {"uses": "made-real"})
    assert result["n_completed"] == 2


# --- a restart can widen nothing --------------------------------------------

def _finished_two_step(tmp_path, first_op="upload_dataset", second_op="update_dataset"):
    steps = [
        {"label": "upload", "op": first_op,
         "args": {"project_id": "p", "process": "x", "name": "n", "source_dir": "/d"}},
        {"label": "annotate", "op": second_op,
         "args": {"project_id": "p",
                  "dataset_id": {"from_step": 0, "field": "dataset_id"},
                  "tags": ["t"]}},
    ]
    session, plan = _prior(
        tmp_path, steps=steps,
        retry={"redo": ["annotate"], "max_attempts": 3, "may_change": ["tags"]},
        completed=[{"step": 1, "label": "upload", "result": {"dataset_id": "ds_real"}}])
    return session, plan, steps


def test_a_restart_cannot_swap_the_operation_behind_a_label(tmp_path):
    """The diff compared args only, so update_dataset could become delete_dataset
    under a "retry" approval whose text never named the new op."""
    session, plan, steps = _finished_two_step(tmp_path)
    swapped = [dict(steps[0]), {"label": "annotate", "op": "delete_dataset",
                                "args": {**steps[1]["args"], "tags": ["t2"]}}]
    with pytest.raises(errors.AgentError) as caught:
        chain_ops.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "annotate"},
            "steps": swapped})
    assert "delete_dataset" in caught.value.message
    assert "update_dataset" in caught.value.message


def test_a_restart_runs_the_same_checks_as_a_fresh_chain(tmp_path):
    """Destructive ops, nesting and reference validity are refused on restart too."""
    session, plan, steps = _finished_two_step(tmp_path, second_op="update_dataset")
    bad_reference = [dict(steps[0]), {"label": "annotate", "op": "update_dataset",
                                      "args": {**steps[1]["args"],
                                               "tags": {"from_step": 0,
                                                        "field": "not_declared"}}}]
    with pytest.raises(errors.AgentError) as caught:
        chain_ops.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "annotate"},
            "steps": bad_reference})
    assert "does not produce 'not_declared'" in caught.value.message


def test_a_chain_that_was_never_approved_cannot_be_restarted(tmp_path):
    """A pending plan has been shown to nobody; its restart view is diff-only."""
    from cirro_agent import plans
    from cirro_agent.session import Session
    session = Session(base_url="app.cirro.test", dir=str(tmp_path))
    (tmp_path / "plans").mkdir(parents=True, exist_ok=True)
    pending = plans.create(
        session, op="chain", summary="s", username="u", risk="cirro_write",
        args={"steps": [{"label": "go", "op": "update_dataset",
                         "args": {"project_id": "p", "dataset_id": "d"}}],
              "retry": {"redo": ["go"], "max_attempts": 3, "may_change": ["tags"]}})
    with pytest.raises(errors.AgentError) as caught:
        chain_ops.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": pending.plan_id, "label": "go"}})
    assert "never approved" in caught.value.message


def test_the_restart_approval_names_every_op_and_every_carried_value(tmp_path):
    """What will run, and what will be substituted, both enter the approved text --
    and therefore the digest -- rather than being summarized as counts."""
    session, plan, steps = _finished_two_step(tmp_path)
    draft = chain_ops.plan_chain(_reader_for(session), {
        "restart_from": {"plan_id": plan.plan_id, "label": "annotate"},
        "steps": [dict(steps[0]), {**steps[1], "args": {**steps[1]["args"],
                                                        "tags": ["t2"]}}]})
    assert "update_dataset" in draft.summary
    assert "ds_real" in draft.summary, "the carried dataset id must be shown"


def _finished_retry(session, root, steps, failed_code="UPSTREAM_ERROR", **extra):
    from cirro_agent import plans
    again = plans.create(session, op="chain", summary="s", username="u",
                         risk="cirro_write",
                         args={"steps": steps[1:], "retry": root.args["retry"],
                               "root": root.plan_id, "restarted_from": root.plan_id,
                               "carried": [{"dataset_id": "ds_real"}], **extra})
    plans.claim(session, again.plan_id, digest=again.digest, approved_summary="s",
                username="u")
    plans.finish(session, again, result={"completed": [],
                                         "failed_at": {"step": 2, "code": failed_code}})
    return again


def test_the_attempt_number_is_counted_not_claimed(tmp_path):
    """The caller said "attempt 2" forever; the count comes from finished attempts."""
    session, plan, steps = _finished_two_step(tmp_path)
    _finished_retry(session, plan, steps, failed_code="A")
    _finished_retry(session, plan, steps, failed_code="B")
    with pytest.raises(errors.AgentError) as caught:
        chain_ops.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "annotate", "attempt": 2},
            "steps": [{**steps[1], "args": {**steps[1]["args"], "tags": ["t9"]}}]})
    assert "attempt 4" in caught.value.message.lower()
    assert "allows 3" in caught.value.message


def test_a_restart_of_a_restart_resumes_the_root_chain(tmp_path):
    """A restart plan's steps are a tail; resuming from it must not treat that tail as
    the whole chain or drop what the root produced."""
    session, plan, steps = _finished_two_step(tmp_path)
    second = _finished_retry(session, plan, steps, failed_code="NOT_FOUND")
    draft = chain_ops.plan_chain(_reader_for(session), {
        "restart_from": {"plan_id": second.plan_id, "label": "annotate"},
        "steps": [{**steps[1], "args": {**steps[1]["args"], "tags": ["t3"]}}]})
    assert draft.args["root"] == plan.plan_id
    assert draft.args["restarted_from"] == second.plan_id
    assert draft.args["carried"] == [{"dataset_id": "ds_real"}]
    assert "Attempt 3 of 3" in draft.summary


def test_two_identical_failures_halt_the_policy(tmp_path):
    session, plan, steps = _finished_two_step(tmp_path)
    from cirro_agent import plans
    root = plans.load(session, plan.plan_id)
    root.args["retry"]["max_attempts"] = 5
    plans._save(session, root)
    _finished_retry(session, root, steps, failed_code="SAME")
    _finished_retry(session, root, steps, failed_code="SAME")
    with pytest.raises(errors.AgentError) as caught:
        chain_ops.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "annotate"},
            "steps": [{**steps[1], "args": {**steps[1]["args"], "tags": ["t9"]}}]})
    assert "failed the same way" in caught.value.message


def test_an_unlabelled_replacement_is_refused_not_applied_everywhere(tmp_path):
    """An unlabelled replacement used to match every unlabelled step and skip the diff."""
    session, plan, steps = _finished_two_step(tmp_path)
    with pytest.raises(errors.AgentError) as caught:
        chain_ops.plan_chain(_reader_for(session), {
            "restart_from": {"plan_id": plan.plan_id, "label": "annotate"},
            "steps": [{"op": "delete_dataset", "args": {"dataset_id": "x"}}]})
    assert "does not match a step being re-run" in caught.value.message


def test_a_forged_outcome_line_in_the_audit_is_refused(tmp_path):
    """The audit file feeds carried outputs; a second outcome entry is tampering."""
    session, plan, steps = _finished_two_step(tmp_path)
    from cirro_agent import plans
    import json
    import time
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(plan.created_at))
    audit = session.path / "runs" / f"{stamp}-chain-{plan.plan_id}.jsonl"
    with audit.open("a") as f:
        f.write(json.dumps({"phase": "outcome", "plan_id": plan.plan_id,
                            "result": {"completed": [{"step": 1, "result": {
                                "dataset_id": "ds_VICTIM"}}]}}) + "\n")
    with pytest.raises(errors.AgentError) as caught:
        plans.outcomes(session, plan)
    assert "outcome" in caught.value.message.lower()


def test_the_audit_file_is_private(tmp_path):
    session, plan, _ = _finished_two_step(tmp_path)
    import time
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(plan.created_at))
    audit = session.path / "runs" / f"{stamp}-chain-{plan.plan_id}.jsonl"
    assert oct(audit.stat().st_mode & 0o777) == "0o600"

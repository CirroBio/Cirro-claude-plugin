"""Reading where a chained workflow stands, from the plan records and the audit."""
from cirro_agent import plans
from cirro_agent.worker import read_chains


def _session(tmp_path):
    from cirro_agent.session import Session
    (tmp_path / "plans").mkdir(parents=True, exist_ok=True)
    return Session(base_url="app.cirro.test", dir=str(tmp_path))


def _request(session, **extra):
    return {"base_url": session.base_url, "session_dir": str(session.path), **extra}


def _chain(session, *, root=None, result=None, error=None, steps=None, retry=None):
    plan = plans.create(session, op="chain", summary="s", username="u", risk="cirro_write",
                        args={"steps": steps or [{"label": "upload", "op": "upload_dataset",
                                                  "args": {}},
                                                 {"label": "run", "op": "run_analysis",
                                                  "args": {}}],
                              "retry": retry,
                              **({"root": root, "carried": [{}]} if root else {})})
    if result is not None or error is not None:
        plans.claim(session, plan.plan_id, digest=plan.digest, approved_summary="s",
                    username="u")
        plans.finish(session, plan, result=result, error=error)
    return plan


def test_every_chain_in_the_folder_is_listed_with_its_attempt_count(tmp_path):
    session = _session(tmp_path)
    root = _chain(session, result={"completed": []},
                  retry={"redo": ["run"], "max_attempts": 3, "may_change": ["params"]})
    _chain(session, root=root.plan_id, result={"completed": [], "failed_at": {
        "step": 2, "label": "run", "op": "run_analysis", "code": "UPSTREAM_ERROR",
        "error": "boom"}})
    _chain(session)  # a second, pending root

    out = read_chains.do_chain_status(_request(session))

    assert out["ok"] and out["result"]["n_chains"] == 2
    by_id = {c["plan_id"]: c for c in out["result"]["chains"]}
    assert by_id[root.plan_id]["attempts"] == 2
    assert by_id[root.plan_id]["latest_status"] == plans.DONE
    assert by_id[root.plan_id]["retry_policy"] is True
    assert out["cost"]["api_calls"] == 0, "local read, no network"


def test_one_chain_shows_each_attempt_and_where_it_stopped(tmp_path):
    session = _session(tmp_path)
    root = _chain(session, result={"completed": [
        {"step": 1, "label": "upload", "result": {"dataset_id": "ds_1"}}],
        "failed_at": {"step": 2, "label": "run", "op": "run_analysis",
                      "code": "INVALID_QUERY", "error": "bad params"}},
        retry={"redo": ["run"], "max_attempts": 3, "may_change": ["params"]})
    second = _chain(session, root=root.plan_id, result={"completed": [
        {"step": 2, "label": "run", "result": {"created_dataset_id": "ds_2"}}]})

    out = read_chains.do_chain_status(_request(session, plan_id=second.plan_id))

    result = out["result"]
    assert result["root"] == root.plan_id, "any plan in the lineage names the whole of it"
    assert [a["attempt"] for a in result["attempts"]] == [1, 2]
    assert result["attempts"][0]["failed_at"]["label"] == "run"
    assert result["attempts"][0]["completed"] == ["upload"]
    assert result["attempts"][1]["completed"] == ["run"]
    assert result["attempts"][1]["reuses"] == 1
    assert result["attempts_used"] == 2 and result["attempts_allowed"] == 3
    assert result["next_calls"] == [], "the latest attempt succeeded; nothing to resume"


def test_a_failed_latest_attempt_offers_the_resume_call(tmp_path):
    session = _session(tmp_path)
    root = _chain(session, result={"completed": [], "failed_at": {
        "step": 2, "label": "run", "op": "run_analysis", "code": "UPSTREAM_ERROR",
        "error": "x"}}, retry={"redo": ["run"], "max_attempts": 3, "may_change": ["p"]})
    out = read_chains.do_chain_status(_request(session, plan_id=root.plan_id))
    assert out["result"]["next_calls"][0]["args"]["restart_from"] == {
        "plan_id": root.plan_id, "label": "run"}


def test_an_unknown_plan_id_is_a_typed_not_found(tmp_path):
    import pytest
    from cirro_agent import errors
    session = _session(tmp_path)
    with pytest.raises(errors.AgentError) as caught:
        read_chains.do_chain_status(_request(session, plan_id="plan_0000000000_deadbeef"))
    assert caught.value.code == "NOT_FOUND"

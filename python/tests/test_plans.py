"""The plan/apply state machine. Every refusal prevents an unapproved mutation."""
import json
import time

import pytest

from cirro_agent import errors, plans, session as session_mod


@pytest.fixture
def sess(tmp_path, monkeypatch):
    return session_mod.init(tmp_path, "app.cirro.bio")


def make(sess, **kwargs):
    kwargs.setdefault("op", "delete_dataset")
    kwargs.setdefault("args", {"project_id": "proj_1", "dataset_id": "ds_9"})
    kwargs.setdefault("summary", "DELETE dataset ds_9 (18 files, 39.3 MB).")
    kwargs.setdefault("username", "sminot")
    return plans.create(sess, **kwargs)


def claim(sess, plan, **overrides):
    payload = {"digest": plan.digest, "approved_summary": plan.summary,
               "username": plan.username}
    payload.update(overrides)
    return plans.claim(sess, plan.plan_id, **payload)


def test_a_plan_is_written_private(sess):
    plan = make(sess)
    path = sess.path / "plans" / f"{plan.plan_id}.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_a_valid_plan_can_be_claimed_once(sess):
    plan = make(sess)
    assert claim(sess, plan).status == plans.APPLYING


def test_a_plan_cannot_be_replayed(sess):
    plan = make(sess)
    claim(sess, plan)
    with pytest.raises(errors.AgentError) as caught:
        claim(sess, plan)
    assert caught.value.code == "PLAN_INVALID"


def test_an_expired_plan_is_refused(sess):
    plan = make(sess)
    plan.expires_at = time.time() - 1
    plans._save(sess, plan)
    with pytest.raises(errors.AgentError) as caught:
        claim(sess, plan)
    assert "expired" in caught.value.message


def test_a_tampered_digest_is_refused(sess):
    plan = make(sess)
    with pytest.raises(errors.AgentError):
        claim(sess, plan, digest="deadbeef")


def test_a_tampered_summary_is_refused(sess):
    """The text the human approved must be the text being executed."""
    plan = make(sess)
    with pytest.raises(errors.AgentError) as caught:
        claim(sess, plan, approved_summary="Delete something harmless")
    assert "approved_summary" in caught.value.message


def test_a_different_user_is_refused(sess):
    plan = make(sess, username="alice")
    with pytest.raises(errors.AgentError) as caught:
        claim(sess, plan, username="bob")
    assert "alice" in caught.value.message and "bob" in caught.value.message


def test_a_different_tenant_is_refused(sess):
    plan = make(sess)
    other = session_mod.Session(base_url="other.cirro.bio", dir=str(sess.path))
    with pytest.raises(errors.AgentError) as caught:
        plans.claim(other, plan.plan_id, digest=plan.digest,
                    approved_summary=plan.summary, username="sminot")
    assert caught.value.code == "TENANT_MISMATCH"


def test_a_missing_plan_is_refused(sess):
    with pytest.raises(errors.AgentError) as caught:
        plans.claim(sess, "plan_nope", digest="x", approved_summary="y",
                    username="sminot")
    assert caught.value.code == "PLAN_INVALID"


def test_a_concurrent_claim_loses_the_race(sess):
    plan = make(sess)
    claim(sess, plan)
    # Reset status but leave the lockfile, as a crashed-mid-apply process would.
    reloaded = plans.load(sess, plan.plan_id)
    reloaded.status = plans.PENDING
    plans._save(sess, reloaded)
    with pytest.raises(errors.AgentError) as caught:
        claim(sess, plan)
    assert "already being applied" in caught.value.message


def _record(**overrides):
    base = {"op": "delete_dataset", "args": {"a": 1}, "base_url": "app.cirro.bio",
            "summary": "Delete it.", "username": "sminot", "expires_at": 1.0,
            "session_dir": "/s", "risk": "cirro_destructive"}
    base.update(overrides)
    return base


def test_the_digest_covers_everything_the_approval_depends_on():
    base = plans.canonical_digest(_record())
    assert base == plans.canonical_digest(_record()), "must be stable"
    for field, value in [("args", {"a": 2}), ("op", "update_dataset"),
                         ("base_url", "other.cirro.bio"),
                         ("summary", "Rename it."), ("username", "someone-else"),
                         ("expires_at", 2.0), ("session_dir", "/elsewhere"),
                         ("risk", "cirro_write")]:
        assert base != plans.canonical_digest(_record(**{field: value})), (
            f"{field} is outside the digest, so it can be tampered with freely")


def test_status_is_outside_the_digest_because_claim_advances_it(sess):
    assert plans.canonical_digest(_record()) == \
        plans.canonical_digest({**_record(), "status": "applying"})


@pytest.mark.parametrize("bad", ["plan_x", "../../outside", "plan_1_ab", "",
                                 "plan_1780000000_ZZZZZZZZ", "plan_1780000000_abcdef01/x"])
def test_a_malformed_plan_id_is_refused(sess, bad):
    with pytest.raises(errors.AgentError) as caught:
        plans.load(sess, bad)
    assert caught.value.code == "PLAN_INVALID"


def test_a_summary_edited_on_disk_is_caught(sess):
    """The text the human approved is inside the integrity check."""
    plan = make(sess, summary="DELETE dataset D1. Not reversible.")
    path = sess.path / "plans" / f"{plan.plan_id}.json"
    stored = json.loads(path.read_text())
    stored["summary"] = "Rename D1 to archive-2026."
    path.write_text(json.dumps(stored))
    with pytest.raises(errors.AgentError) as caught:
        claim(sess, plan, approved_summary="Rename D1 to archive-2026.")
    assert "does not match its own digest" in caught.value.message


def test_the_audit_log_records_intent_before_outcome(sess):
    plan = make(sess)
    claimed = claim(sess, plan)
    plans.finish(sess, claimed, result={"deleted": "ds_9"})
    files = sorted((sess.path / "runs").glob("*.jsonl"))
    assert files
    entries = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert [e["phase"] for e in entries] == ["intent", "outcome"]
    assert entries[0]["op"] == "delete_dataset"
    assert entries[1]["result"] == {"deleted": "ds_9"}


def test_a_failed_apply_is_recorded_too(sess):
    plan = make(sess)
    claimed = claim(sess, plan)
    plans.finish(sess, claimed, error="UPSTREAM_ERROR: boom")
    assert plans.load(sess, plan.plan_id).status == plans.FAILED
    files = sorted((sess.path / "runs").glob("*.jsonl"))
    entries = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert entries[-1]["error"].startswith("UPSTREAM_ERROR")


def test_render_leads_with_tenant_and_identity(sess):
    plan = make(sess, warnings=["2 datasets derive from this one"])
    text = plans.render(plan.to_dict())
    first = text.splitlines()[0]
    assert "app.cirro.bio" in first and "sminot" in first
    assert "delete_dataset" in text
    assert "2 datasets derive" in text


def test_risk_is_carried_into_the_rendered_text(sess):
    plan = make(sess, risk="cirro_destructive")
    assert "cirro_destructive" in plans.render(plan.to_dict())


def test_a_plan_edited_on_disk_fails_its_own_digest(sess):
    """Comparing the caller's digest to the stored one would not catch this: an editor
    can change `args` and leave `digest` untouched."""
    plan = make(sess)
    path = sess.path / "plans" / f"{plan.plan_id}.json"
    stored = json.loads(path.read_text())
    stored["args"]["dataset_id"] = "ds_something_else"
    path.write_text(json.dumps(stored))
    with pytest.raises(errors.AgentError) as caught:
        claim(sess, plan)
    assert "does not match its own digest" in caught.value.message


def test_both_phases_of_one_apply_land_in_one_file(session_dir, monkeypatch):
    """A file holding an intent with no outcome reads as a mutation that never
    finished. The two records must not be separable by a clock tick."""
    plan = make(session_dir)
    plans.claim(session_dir, plan.plan_id, digest=plan.digest,
                approved_summary=plan.summary, username="sminot")

    # Advance the wall clock only for a bare gmtime() -- the shape the old code used.
    # A stamp taken from the plan passes an explicit time and is left alone, so this
    # separates "named for the plan" from "named for whenever the write happened".
    # `plans.time` is the stdlib module itself, so capture the real one first.
    real_gmtime = time.gmtime
    monkeypatch.setattr(plans.time, "gmtime",
                        lambda *args: real_gmtime(args[0] if args
                                                  else time.time() + 3600))
    plans.finish(session_dir, plan, result={"ok": True})

    files = sorted((session_dir.path / "runs").glob("*.jsonl"))
    assert len(files) == 1, [f.name for f in files]
    phases = [json.loads(line)["phase"] for line in files[0].read_text().splitlines()]
    assert phases == ["intent", "outcome"]

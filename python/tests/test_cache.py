"""The snapshot cache: keyed per principal, honest about staleness."""
import time

import pytest

from cirro_agent import cache as C


@pytest.fixture
def store(tmp_path):
    (tmp_path / ".cirro").mkdir()
    return C.SnapshotCache(tmp_path / ".cirro", "app.cirro.bio", "sminot")


def test_a_snapshot_round_trips(store):
    store.put("datasets", [{"id": "ds_1"}], project="proj_a")
    hit = store.get("datasets", project="proj_a")
    assert hit is not None
    assert hit.items == [{"id": "ds_1"}]
    assert hit.item_count == 1
    assert hit.fresh is True


def test_the_key_includes_the_principal_because_visibility_is_per_user(tmp_path):
    (tmp_path / ".cirro").mkdir()
    mine = C.SnapshotCache(tmp_path / ".cirro", "app.cirro.bio", "sminot")
    theirs = C.SnapshotCache(tmp_path / ".cirro", "app.cirro.bio", "jlee")
    mine.put("datasets", [{"id": "secret"}], project="proj_a")
    assert theirs.get("datasets", project="proj_a") is None


def test_the_key_includes_the_tenant(tmp_path):
    (tmp_path / ".cirro").mkdir()
    one = C.SnapshotCache(tmp_path / ".cirro", "a.cirro.bio", "sminot")
    two = C.SnapshotCache(tmp_path / ".cirro", "b.cirro.bio", "sminot")
    one.put("datasets", [{"id": "x"}], project="proj_a")
    assert two.get("datasets", project="proj_a") is None


def test_different_scopes_do_not_collide(store):
    store.put("datasets", [{"id": "a"}], project="proj_a")
    store.put("datasets", [{"id": "b"}], project="proj_b")
    assert store.get("datasets", project="proj_a").items == [{"id": "a"}]
    assert store.get("datasets", project="proj_b").items == [{"id": "b"}]


def test_an_expired_snapshot_is_not_served_by_default(store):
    store.put("datasets", [{"id": "a"}], ttl_s=0, project="proj_a")
    time.sleep(0.01)
    assert store.get("datasets", ttl_s=0, project="proj_a") is None


def test_stale_is_served_only_when_asked_and_reports_its_age(store):
    store.put("datasets", [{"id": "a"}], ttl_s=0, project="proj_a")
    time.sleep(0.01)
    hit = store.get("datasets", ttl_s=0, freshness=C.FRESH_ANY, project="proj_a")
    assert hit is not None
    assert hit.fresh is False
    assert hit.age_s >= 0


def test_fresh_required_always_refetches(store):
    store.put("datasets", [{"id": "a"}], project="proj_a")
    assert store.get("datasets", freshness=C.FRESH_REQUIRED, project="proj_a") is None


def test_no_store_content_never_reaches_disk(store):
    snapshot = store.put("datasets", [{"id": "restricted"}], no_store=True,
                         project="proj_a")
    assert snapshot.items == [{"id": "restricted"}]
    assert store.get("datasets", project="proj_a") is None


def test_invalidate_drops_one_scope_and_names_it(store):
    store.put("datasets", [{"id": "a"}], project="proj_a")
    store.put("datasets", [{"id": "b"}], project="proj_b")
    label = store.invalidate("datasets", project="proj_a")
    assert "proj_a" in label
    assert store.get("datasets", project="proj_a") is None
    assert store.get("datasets", project="proj_b") is not None


def test_invalidate_kinds_drops_every_scope_of_a_kind(store):
    store.put("datasets", [{"id": "a"}], project="proj_a")
    store.put("datasets", [{"id": "b"}], project="proj_b")
    store.put("projects", [{"id": "p"}])
    dropped = store.invalidate_kinds(["datasets"])
    assert len(dropped) == 2
    assert store.get("projects") is not None


def test_ttls_follow_entity_lifecycle():
    assert C.TTL["processes"] > C.TTL["projects"] > C.TTL["datasets"]
    assert C.manifest_ttl(True) > C.manifest_ttl(False)
    assert C.execution_ttl(True) > C.execution_ttl(False)


def test_eviction_bounds_the_store(store, monkeypatch):
    monkeypatch.setattr(C, "MAX_ENTRIES", 5)
    for index in range(12):
        store.put("datasets", [{"id": index}], project=f"proj_{index}")
    remaining = list((store.root).glob("*.json"))
    assert len(remaining) <= 5


def test_a_corrupt_snapshot_is_treated_as_a_miss(store):
    store.put("datasets", [{"id": "a"}], project="proj_a")
    path = next(store.root.glob("*.json"))
    path.write_text("{not json")
    assert store.get("datasets", project="proj_a") is None

"""Snapshot cache for whole collections.

Because filtering is client-side, the useful unit of caching is not a page but a
complete fetched collection -- the "snapshot". One snapshot then serves any number
of different filters, facet computations and exact follow-up counts for free.

Two rules:
  * The principal is part of the key. Cirro visibility is per-user, so a snapshot
    shared between users would leak.
  * Staleness is reported, never hidden. This is stale-while-INFORMING: an expired
    snapshot may be served, but the response says so. A background refresh whose
    result the agent never sees would just be a source of confusing answers.
"""
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

CONTRACT_VERSION = "1"

# TTL follows the entity's own lifecycle: terminal things cache long, active
# things cache briefly. Seconds.
TTL = {
    "processes": 24 * 3600,
    "reference_types": 24 * 3600,
    "projects": 3600,
    "users": 3600,
    "metrics": 6 * 3600,
    "datasets": 300,
    "samples": 900,
    "references": 900,
    "compute_envs": 3600,
    "shares": 900,
    "workspaces": 120,
    "manifest_terminal": 24 * 3600,
    "manifest_active": 60,
}

# A kind with no entry in TTL caches as briefly as the most volatile listed kind.
DEFAULT_TTL_S = 300

MAX_ENTRIES = 200

FRESH_DEFAULT = "default"   # serve if within TTL, else refetch
FRESH_REQUIRED = "fresh"    # always refetch
FRESH_ANY = "any"           # serve even if expired, and say so


@dataclass
class Snapshot:
    # The human-readable scope label, not the storage key: this is what cost
    # accounting shows a person. SnapshotCache.key() is the hashed key on disk.
    label: str
    kind: str
    items: list
    created_at: float
    ttl_s: int
    meta: dict

    @property
    def age_s(self) -> float:
        return max(time.time() - self.created_at, 0.0)

    @property
    def fresh(self) -> bool:
        return self.age_s <= self.ttl_s

    @property
    def item_count(self) -> int:
        return len(self.items)


class SnapshotCache:
    def __init__(self, session_dir: Path, tenant: str, principal: Optional[str]):
        self.root = Path(session_dir) / "cache" / "snapshots"
        self.tenant = tenant
        self.principal = principal or "anonymous"

    def _scope_label(self, kind: str, scope: dict) -> str:
        parts = ",".join(f"{k}={scope[k]}" for k in sorted(scope) if scope[k] is not None)
        return f"{kind}@{parts}" if parts else f"{kind}@{self.tenant}"

    def key(self, kind: str, **scope: Any) -> str:
        raw = "|".join([
            self.tenant, self.principal, kind, CONTRACT_VERSION,
            json.dumps({k: scope[k] for k in sorted(scope)}, sort_keys=True, default=str),
        ])
        return hashlib.sha256(raw.encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, kind: str, *, ttl_s: Optional[int] = None,
            freshness: str = FRESH_DEFAULT, **scope: Any) -> Optional[Snapshot]:
        if freshness == FRESH_REQUIRED:
            return None
        key = self.key(kind, **scope)
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        snapshot = Snapshot(
            label=self._scope_label(kind, scope), kind=kind, items=raw.get("items", []),
            created_at=raw.get("created_at", 0.0),
            ttl_s=ttl_s if ttl_s is not None else TTL.get(kind, DEFAULT_TTL_S),
            meta=raw.get("meta", {}),
        )
        if not snapshot.fresh and freshness != FRESH_ANY:
            return None
        return snapshot

    def put(self, kind: str, items: list, *, meta: Optional[dict] = None,
            ttl_s: Optional[int] = None, **scope: Any) -> Snapshot:
        snapshot = Snapshot(
            label=self._scope_label(kind, scope), kind=kind, items=items,
            created_at=time.time(),
            ttl_s=ttl_s if ttl_s is not None else TTL.get(kind, DEFAULT_TTL_S),
            meta=meta or {},
        )
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(self.key(kind, **scope))
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "label": snapshot.label, "kind": kind, "created_at": snapshot.created_at,
            "meta": snapshot.meta, "items": items,
        }, default=str))
        tmp.replace(path)
        self._evict()
        return snapshot

    def invalidate(self, kind: str, **scope: Any) -> str:
        """Drop one snapshot. Every mutation calls this synchronously for the
        scopes it touched, and reports the labels it dropped."""
        path = self._path(self.key(kind, **scope))
        path.unlink(missing_ok=True)
        return self._scope_label(kind, scope)

    def invalidate_kinds(self, kinds: list) -> list:
        """Drop every snapshot of the given kinds, whatever their scope."""
        dropped = []
        if not self.root.is_dir():
            return dropped
        for path in self.root.glob("*.json"):
            try:
                raw = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if raw.get("kind") in kinds:
                dropped.append(raw.get("label", path.stem))
                path.unlink(missing_ok=True)
        return dropped

    def invalidate_entries(self, entries: list) -> list:
        """Drop every snapshot the given scopes cover, and name what went.

        An entry carrying nothing but its kind means every snapshot of that kind,
        whatever it was keyed by: `invalidate` with an empty scope would compute a
        key nothing was stored under and remove nothing.
        """
        dropped = []
        for entry in entries:
            scope = {k: v for k, v in entry.items() if k != "kind"}
            if scope:
                dropped.append(self.invalidate(entry["kind"], **scope))
            else:
                dropped += self.invalidate_kinds([entry["kind"]])
        return dropped

    def _evict(self) -> None:
        if not self.root.is_dir():
            return
        entries = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for path in entries[:-MAX_ENTRIES]:
            path.unlink(missing_ok=True)


def manifest_ttl(is_terminal: bool) -> int:
    return TTL["manifest_terminal"] if is_terminal else TTL["manifest_active"]

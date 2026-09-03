"""The uniform read response, and the ladder it shrinks down.

Two rules the whole design rests on:

1. Counts and facets are computed over the FULL match set, never over the rows
   that survive the budget. A facet over a truncated scan is a lie, so
   `scan_complete` rides along with every count and poisons the response when
   false.

2. Rows degrade before facets. Facets are lossy but complete; rows are complete
   but lossy. Under pressure, the shape of all 4,127 datasets beats the detail of
   40 of them -- and 40 arbitrary rows invite an agent to reason from a
   misleading sample, which is worse than not sending them.
"""
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

CONTRACT = "cirro.agent.v1"

# Chars per token for compact JSON. Refining this is not worth the code.
CHARS_PER_TOKEN = 3.7

# Anything below this and the response cannot carry its own remediation.
MIN_TOKENS = 400

DEFAULT_MAX_TOKENS = {
    "query": 6000,
    "summarize": 3000,
    "get": 1500,
    "describe": 1500,
    # Local, no-network reads carry their own budgets; these used to be bare
    # literals in each verb.
    "process_config": 8000,
    "workflow_spec": 12000,
    "check_config": 6000,
}

ROWS_PAGE = "page"
ROWS_PREVIEW = "preview_not_page"
ROWS_SAMPLE = "sample"
ROWS_ABSENT = "absent_aggregation_only"


def compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def estimate_tokens(obj: Any) -> int:
    return int(len(compact(obj)) / CHARS_PER_TOKEN)


@dataclass
class Facet:
    """A facet before budget truncation. `values` covers the entire match set."""
    kind: str = "terms"
    values: list = field(default_factory=list)   # [(value, n, label_or_None)]
    bin: Optional[str] = None                    # histograms only
    note: Optional[str] = None

    @property
    def cardinality(self) -> int:
        return len(self.values)

    def render(self, top_k: int) -> dict:
        if self.kind == "histogram":
            out = {"kind": "histogram", "bin": self.bin,
                   "buckets": [{"key": v, "n": n} for v, n, _ in self.values]}
            if self.note:
                out["note"] = self.note
            return out

        shown = self.values[:top_k]
        tail = self.values[top_k:]
        top = []
        for value, n, label in shown:
            cell = {"value": value, "n": n}
            if label is not None:
                cell["label"] = label
            top.append(cell)
        out = {
            "cardinality": self.cardinality,
            "shown": len(shown),
            "top": top,
            # shown + other lets the agent see how much tail it is missing, so a
            # truncated facet stays arithmetically honest.
            "other": {"n": sum(n for _, n, _ in tail), "distinct": len(tail)},
        }
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class Cost:
    by_kind: dict = field(default_factory=dict)
    detail_calls: int = 0
    probe_calls: int = 0
    tier_ceiling: str = "list"
    strategy: str = ""
    cache_hits: int = 0
    cache_misses: int = 0
    snapshots_used: list = field(default_factory=list)
    _t0: float = field(default_factory=time.monotonic)
    _wall_ms: Optional[int] = None

    def call(self, kind: str, n: int = 1, *, detail: bool = False,
             probe: bool = False) -> None:
        self.by_kind[kind] = self.by_kind.get(kind, 0) + n
        if detail:
            self.detail_calls += n
        if probe:
            self.probe_calls += n

    def snapshot(self, key: str, age_s: float, ttl_s: int, item_count: int,
                 fresh: bool) -> None:
        self.snapshots_used.append({
            "key": key, "age_s": round(age_s), "ttl_s": ttl_s,
            "fresh": fresh, "item_count": item_count,
        })
        if fresh:
            self.cache_hits += 1
        else:
            self.cache_misses += 1

    def to_dict(self) -> dict:
        if self._wall_ms is None:
            self._wall_ms = int((time.monotonic() - self._t0) * 1000)
        total = sum(self.by_kind.values())
        looked = self.cache_hits + self.cache_misses
        out = {
            "api_calls": total,
            "by_kind": self.by_kind,
            "detail_calls": self.detail_calls,
            "probe_calls": self.probe_calls,
            "wall_ms": self._wall_ms,
            "cache": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "fraction_from_cache": round(self.cache_hits / looked, 2) if looked else None,
                "snapshots_used": self.snapshots_used,
            },
            "tier_ceiling": self.tier_ceiling,
            "strategy": self.strategy,
        }
        return out


@dataclass
class Counts:
    matched: int
    scanned: int
    scan_complete: bool
    returned: int = 0
    rows_are: str = ROWS_PAGE
    groups_total: Optional[int] = None
    groups_returned: Optional[int] = None

    def to_dict(self) -> dict:
        out = {
            "matched": self.matched,
            "scanned": self.scanned,
            "scan_complete": self.scan_complete,
            "rows_are": self.rows_are,
        }
        if self.rows_are != ROWS_ABSENT:
            out["returned"] = self.returned
        if self.groups_total is not None:
            out["groups_total"] = self.groups_total
            out["groups_returned"] = self.groups_returned
        return out


def _project(rows: list, keys: list) -> list:
    return [{k: r[k] for k in keys if k in r} for r in rows]


def build(
    *,
    entity: str,
    query: dict,
    counts: Counts,
    cost: Cost,
    rows: Optional[list] = None,
    groups: Optional[list] = None,
    group_extras: Optional[dict] = None,
    facets: Optional[dict] = None,
    shape: Optional[dict] = None,
    next_calls: Optional[list] = None,
    exemplars: Optional[list] = None,
    projections: Optional[dict] = None,
    facet_priority: Optional[list] = None,
    omitted: Optional[list] = None,
    notes: Optional[list] = None,
    max_tokens: int = 6000,
) -> dict:
    """Assemble a response, shrinking it to fit `max_tokens`."""
    max_tokens = max(int(max_tokens), MIN_TOKENS)
    omitted = list(omitted or [])
    notes = list(notes or [])
    facets = dict(facets or {})
    next_calls = list(next_calls or [])
    projections = projections or {}
    facet_priority = list(facet_priority or list(facets))
    degraded = []

    if not counts.scan_complete:
        omitted.append({
            "code": "SCAN_TRUNCATED",
            "detail": (
                f"The scan stopped at {counts.scanned} records before exhausting the "
                "collection. Every count, facet and shape figure below is a lower "
                "bound, not a total."
            ),
            "fix": {"note": "Narrow the scope, or raise scan_limit, then re-run."},
        })

    facet_state = {"top_k": 10, "dropped": []}
    rows_state = {"step": 0}

    # rows: page/standard -> page/compact -> exemplars/standard -> exemplars/compact
    #       -> ids only -> nothing. Whole rows and whole objects only; a structure
    #       is never cut in half.
    is_full_page = rows is not None and counts.matched == len(rows)
    row_steps = []
    if rows is not None:
        std = projections.get("standard") or (list(rows[0]) if rows else [])
        cmp_ = projections.get("compact") or std
        if is_full_page:
            row_steps.append(("page/standard", ROWS_PAGE, lambda: _project(rows, std)))
            if cmp_ != std:
                row_steps.append(("page/compact", ROWS_PAGE, lambda: _project(rows, cmp_)))
        ex = exemplars if exemplars is not None else rows[:5]
        row_steps.append(("preview/standard", ROWS_PREVIEW,
                          lambda: _project(ex, ["_why"] + std)))
        row_steps.append(("preview/compact", ROWS_PREVIEW,
                          lambda: _project(ex, ["_why"] + cmp_)))
        row_steps.append((
            "ids_only", ROWS_SAMPLE,
            lambda: [{k: r[k] for k in ("id", "name", "path") if k in r} for r in ex]))
        row_steps.append(("none", ROWS_SAMPLE, lambda: []))

    def assemble() -> dict:
        result: dict = {"entity": entity, "counts": counts.to_dict()}
        if groups is not None:
            result["groups"] = groups
            for key, value in (group_extras or {}).items():
                result[key] = value
        if rows is not None:
            label, rows_are, make = row_steps[rows_state["step"]]
            body = make()
            counts.returned = len(body)
            counts.rows_are = rows_are
            result["counts"] = counts.to_dict()
            result["rows"] = body
        rendered = {}
        for name in facet_priority:
            if name in facet_state["dropped"] or name not in facets:
                continue
            rendered[name] = facets[name].render(facet_state["top_k"])
        if rendered:
            result["facets"] = rendered
        if shape:
            result["shape"] = shape
        result["next_calls"] = next_calls
        envelope = {
            "ok": True,
            "api": {"contract": CONTRACT},
            "query": query,
            "result": result,
            "cost": cost.to_dict(),
            "budget": {"max_tokens": max_tokens, "degraded": degraded},
        }
        all_omitted = omitted + budget_omissions()
        if all_omitted:
            envelope["omitted"] = all_omitted
        if notes:
            envelope["notes"] = notes
        return envelope

    def budget_omissions() -> list:
        """What the budget cost, computed from current state.

        Derived inside assemble() rather than appended afterwards, so that every
        measurement the ladder takes already includes its own bookkeeping. Adding
        these at the end would let a response that just fit tip back over.
        """
        out = []
        if rows is not None and counts.rows_are == ROWS_PREVIEW:
            out.append({
                "code": "ROWS_PREVIEWED",
                "detail": (
                    f"{counts.returned} labeled exemplars returned instead of a page. All "
                    f"counts and facets above are computed over the full {counts.matched} "
                    "matches, not over these exemplars -- do not treat them as a sample."
                ),
                "fix": {"note": "Narrow the filter (see next_calls) so the match set fits."},
            })
        if facet_state["dropped"]:
            out.append({
                "code": "FACETS_DROPPED",
                "fields": list(facet_state["dropped"]),
                "detail": ("Dropped to fit the budget; the counts they"
                           " summarize are unchanged."),
                "fix": {"note": "Re-run with a higher max_tokens, or a narrower filter."},
            })
        truncated = {
            name: {"shown": facet_state["top_k"], "cardinality": facets[name].cardinality}
            for name in facets
            if facets[name].kind == "terms"
            and name not in facet_state["dropped"]
            and facets[name].cardinality > facet_state["top_k"]
        }
        if truncated:
            out.append({"code": "FACET_TRUNCATED", "fields": truncated})
        return out

    def fits(env: dict) -> bool:
        return estimate_tokens(env) <= max_tokens

    def finalize() -> dict:
        final = assemble()
        used = estimate_tokens(final)
        if used > max_tokens:
            # Every degradation step is exhausted and the skeleton still exceeds the
            # cap. Say so rather than cutting a structure in half. Added before the
            # figure is recorded, so the note counts toward the total it explains.
            final.setdefault("notes", []).append(
                f"BUDGET_FLOOR: every degradation step is exhausted and this response "
                f"still exceeds max_tokens={max_tokens}. Nothing was truncated "
                f"mid-structure."
            )
            used = estimate_tokens(final)
        # used_est is part of the payload it measures, so it under-reports by the width
        # of the number itself -- a handful of tokens. Accurate, not exact.
        final["budget"]["used_est"] = used
        return final

    env = assemble()
    if fits(env):
        return finalize()

    # 1. Rows.
    while rows is not None and rows_state["step"] < len(row_steps) - 1:
        before = row_steps[rows_state["step"]][0]
        rows_state["step"] += 1
        degraded.append({"what": "rows", "from": before,
                         "to": row_steps[rows_state["step"]][0]})
        env = assemble()
        if fits(env):
            break

    # 2. Facets: shallower, then narrower.
    if not fits(env):
        for top_k in (5, 3):
            facet_state["top_k"] = top_k
            degraded.append({"what": "facets", "to": f"top{top_k}"})
            env = assemble()
            if fits(env):
                break

    if not fits(env):
        # Drop from the least informative end of the entity's declared priority.
        for name in reversed(facet_priority[1:]):
            if name not in facets:
                continue
            facet_state["dropped"].append(name)
            degraded.append({"what": f"facets.{name}", "to": "dropped"})
            env = assemble()
            if fits(env):
                break

    # 3. next_calls, never to zero -- a response must always offer a way forward.
    if not fits(env):
        for keep in (3, 1):
            if len(next_calls) <= keep:
                continue
            del next_calls[keep:]
            degraded.append({"what": "next_calls", "to": f"{keep}"})
            env = assemble()
            if fits(env):
                break

    return finalize()


def shrink_to_fit(container: dict, key: str, *, floor: int, max_tokens: int,
                  degraded: list) -> None:
    """Halve a list inside `container` until the container fits the budget.

    Records what was done in `degraded` and leaves `<key>_truncated_to` beside the
    list, so the caller's envelope says what it dropped. Three verbs implemented this
    ladder by hand with different floors; this is the one implementation.
    """
    while estimate_tokens(container) > max_tokens and len(container.get(key) or []) > floor:
        items = container[key]
        keep = max(floor, len(items) // 2)
        container[key] = items[:keep]
        container["truncated_to"] = keep
        degraded.append({"what": key, "to": str(keep)})


NO_API_COST = {"api_calls": 0, "by_kind": {}, "note": "local computation only"}


def wrap(*, query: dict, result: dict, cost: dict, max_tokens: int,
         degraded: Optional[list] = None) -> dict:
    """The response skeleton every non-query verb returns.

    CLAUDE.md's response discipline -- contract, echoed query, cost, budget with
    `used_est` -- was hand-spelled in seven verbs. `build()` covers the faceted query
    shape; this covers everything else, so the contract is kept true in one place.
    """
    envelope = {
        "ok": True, "api": {"contract": CONTRACT}, "query": query, "result": result,
        "cost": cost, "budget": {"max_tokens": max_tokens, "degraded": list(degraded or [])},
    }
    envelope["budget"]["used_est"] = estimate_tokens(envelope)
    return envelope

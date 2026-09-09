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
    "tenants": 1500,
}

# `include` on a detail-or-worse tier costs one call per row, so it is gated. Here
# rather than in a read verb because the session-free local reads quote it too, and
# importing it from `worker.read_query` pulled five worker modules into the parent
# process to read one integer -- inverting the layering that keeps those reads free of
# the worker package.
DETAIL_BUDGET = 25

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


def _row_steps(rows: list, exemplars: Optional[list], projections: dict,
               is_full_page: bool) -> list:
    """The rows ladder, worst case last.

    page/standard -> page/compact -> preview/standard -> preview/compact -> ids only
    -> nothing. Whole rows and whole objects only; a structure is never cut in half.

    Each step's renderer stays a closure: the ladder usually stops on its first or
    second rung, and a step that is never reached is never paid for.
    """
    standard = projections.get("standard") or (list(rows[0]) if rows else [])
    compact_keys = projections.get("compact") or standard
    steps = []
    if is_full_page:
        steps.append(("page/standard", ROWS_PAGE, lambda: _project(rows, standard)))
        if compact_keys != standard:
            steps.append(("page/compact", ROWS_PAGE,
                          lambda: _project(rows, compact_keys)))
    preview = exemplars if exemplars is not None else rows[:5]
    steps.append(("preview/standard", ROWS_PREVIEW,
                  lambda: _project(preview, ["_why"] + standard)))
    steps.append(("preview/compact", ROWS_PREVIEW,
                  lambda: _project(preview, ["_why"] + compact_keys)))
    steps.append((
        "ids_only", ROWS_SAMPLE,
        lambda: [{k: r[k] for k in ("id", "name", "path") if k in r} for r in preview]))
    steps.append(("none", ROWS_SAMPLE, lambda: []))
    return steps


@dataclass
class _Assembly:
    """A response mid-assembly, and how far down the ladder it has come.

    No stage of the ladder can judge its own step without re-rendering the whole
    envelope and measuring it, so every stage needs the same inputs plus the three
    pieces of degradation state -- `row_step`, `top_k` and `dropped`. Holding them
    here is what lets a stage be read on its own; as closures over `build()`'s frame
    they shared fifteen captured names and nothing could be understood in isolation.

    A stage takes the last measured envelope and returns the one it settled on, so
    a stage with nothing to do returns what it was given untouched.
    """
    entity: str
    query: dict
    counts: Counts
    cost: Cost
    rows: Optional[list]
    groups: Optional[list]
    group_extras: Optional[dict]
    facets: dict
    shape: Optional[dict]
    next_calls: list
    facet_priority: list
    omitted: list
    notes: list
    max_tokens: int
    row_steps: list
    degraded: list = field(default_factory=list)
    row_step: int = 0
    top_k: int = 10
    dropped: list = field(default_factory=list)

    def assemble(self) -> dict:
        result: dict = {"entity": self.entity, "counts": self.counts.to_dict()}
        if self.groups is not None:
            result["groups"] = self.groups
            for key, value in (self.group_extras or {}).items():
                result[key] = value
        if self.rows is not None:
            _, rows_are, render = self.row_steps[self.row_step]
            body = render()
            self.counts.returned = len(body)
            self.counts.rows_are = rows_are
            result["counts"] = self.counts.to_dict()
            result["rows"] = body
        rendered = {}
        for name in self.facet_priority:
            if name in self.dropped or name not in self.facets:
                continue
            rendered[name] = self.facets[name].render(self.top_k)
        if rendered:
            result["facets"] = rendered
        if self.shape:
            result["shape"] = self.shape
        result["next_calls"] = self.next_calls
        envelope = {
            "ok": True,
            "api": {"contract": CONTRACT},
            "query": self.query,
            "result": result,
            "cost": self.cost.to_dict(),
            "budget": {"max_tokens": self.max_tokens, "degraded": self.degraded},
        }
        all_omitted = self.omitted + self.budget_omissions()
        if all_omitted:
            envelope["omitted"] = all_omitted
        if self.notes:
            envelope["notes"] = self.notes
        return envelope

    def budget_omissions(self) -> list:
        """What the budget cost, computed from current state.

        Derived inside assemble() rather than appended afterwards, so that every
        measurement the ladder takes already includes its own bookkeeping. Adding
        these at the end would let a response that just fit tip back over.
        """
        out = []
        if self.rows is not None and self.counts.rows_are == ROWS_PREVIEW:
            out.append({
                "code": "ROWS_PREVIEWED",
                "detail": (
                    f"{self.counts.returned} labeled exemplars returned instead of a "
                    f"page. All counts and facets above are computed over the full "
                    f"{self.counts.matched} matches, not over these exemplars -- do not "
                    "treat them as a sample."
                ),
                "fix": {"note": "Narrow the filter (see next_calls) so the match set fits."},
            })
        if self.dropped:
            out.append({
                "code": "FACETS_DROPPED",
                "fields": list(self.dropped),
                "detail": ("Dropped to fit the budget; the counts they"
                           " summarize are unchanged."),
                "fix": {"note": "Re-run with a higher max_tokens, or a narrower filter."},
            })
        truncated = {
            name: {"shown": self.top_k, "cardinality": self.facets[name].cardinality}
            for name in self.facets
            if self.facets[name].kind == "terms"
            and name not in self.dropped
            and self.facets[name].cardinality > self.top_k
        }
        if truncated:
            out.append({"code": "FACET_TRUNCATED", "fields": truncated})
        return out

    def fits(self, envelope: dict) -> bool:
        return estimate_tokens(envelope) <= self.max_tokens

    def shorter_rows(self, envelope: dict) -> dict:
        while self.row_step < len(self.row_steps) - 1:
            before = self.row_steps[self.row_step][0]
            self.row_step += 1
            self.degraded.append({"what": "rows", "from": before,
                                  "to": self.row_steps[self.row_step][0]})
            envelope = self.assemble()
            if self.fits(envelope):
                break
        return envelope

    def shallower_facets(self, envelope: dict) -> dict:
        for top_k in (5, 3):
            self.top_k = top_k
            self.degraded.append({"what": "facets", "to": f"top{top_k}"})
            envelope = self.assemble()
            if self.fits(envelope):
                break
        return envelope

    def fewer_facets(self, envelope: dict) -> dict:
        # Drop from the least informative end of the entity's declared priority. The
        # first facet is never dropped; a response keeps the shape of the match set.
        for name in reversed(self.facet_priority[1:]):
            if name not in self.facets:
                continue
            self.dropped.append(name)
            self.degraded.append({"what": f"facets.{name}", "to": "dropped"})
            envelope = self.assemble()
            if self.fits(envelope):
                break
        return envelope

    def fewer_next_calls(self, envelope: dict) -> dict:
        # Never to zero -- a response must always offer a way forward.
        for keep in (3, 1):
            if len(self.next_calls) <= keep:
                continue
            del self.next_calls[keep:]
            self.degraded.append({"what": "next_calls", "to": f"{keep}"})
            envelope = self.assemble()
            if self.fits(envelope):
                break
        return envelope

    def finalize(self) -> dict:
        final = self.assemble()
        used = estimate_tokens(final)
        if used > self.max_tokens:
            # Every degradation step is exhausted and the skeleton still exceeds the
            # cap. Say so rather than cutting a structure in half. Added before the
            # figure is recorded, so the note counts toward the total it explains.
            final.setdefault("notes", []).append(
                f"BUDGET_FLOOR: every degradation step is exhausted and this response "
                f"still exceeds max_tokens={self.max_tokens}. Nothing was truncated "
                f"mid-structure."
            )
            used = estimate_tokens(final)
        # used_est is part of the payload it measures, so it under-reports by the width
        # of the number itself -- a handful of tokens. Accurate, not exact.
        final["budget"]["used_est"] = used
        return final


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
    """Assemble a response, shrinking it to fit `max_tokens`.

    The ladder runs in one order and stops as soon as the envelope fits: rows, then
    facets (shallower, then narrower), then next_calls. Rows go first because facets
    are lossy but complete while rows are complete but lossy -- see the module
    docstring. Each stage is a method on `_Assembly`.
    """
    max_tokens = max(int(max_tokens), MIN_TOKENS)
    facets = dict(facets or {})
    omitted = list(omitted or [])

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

    assembly = _Assembly(
        entity=entity, query=query, counts=counts, cost=cost, rows=rows,
        groups=groups, group_extras=group_extras, facets=facets, shape=shape,
        next_calls=list(next_calls or []),
        facet_priority=list(facet_priority or list(facets)),
        omitted=omitted, notes=list(notes or []), max_tokens=max_tokens,
        row_steps=[] if rows is None else _row_steps(
            rows, exemplars, projections or {}, counts.matched == len(rows)),
    )

    envelope = assembly.assemble()
    for stage in (assembly.shorter_rows, assembly.shallower_facets,
                  assembly.fewer_facets, assembly.fewer_next_calls):
        if assembly.fits(envelope):
            break
        envelope = stage(envelope)
    return assembly.finalize()


def shrink_to_fit(envelope: dict, key: str, *, floor: int, max_tokens: int,
                  fix: dict) -> None:
    """Halve `envelope["result"][key]` until the whole envelope fits `max_tokens`.

    Three verbs implemented this loop by hand -- list, process_params and
    workflow_spec -- with three markers for the same event (`truncated_to`,
    `parameters_truncated_to`, `params_truncated_to`) and, in process_params, no record
    of the loss at all. A fourth, check_config, had no loop and simply overran its
    budget. This is the one implementation.

    The marker is `<key>_truncated_to` beside the list rather than a fixed key on the
    result, so a result that shrinks two lists says which one was cut.

    Declaring the loss is not left to the caller: each pass records a `degraded` step
    and keeps one `LIST_TRUNCATED` entry in `omitted` carrying the caller's `fix`, and
    that entry is in place before the next measurement, so what the declaration costs
    is inside the budget it explains.

    Whole items only -- the list gets shorter, an element is never cut open. `floor` is
    the length shrinking stops at, so a truncated response still shows the shape of
    what it dropped. `summarize` degrades by dropping whole named sections in a fixed
    priority order instead; that is a different ladder and is not folded in here.
    """
    result = envelope["result"]
    total = len(result.get(key) or [])
    entry = None
    while estimate_tokens(envelope) > max_tokens and len(result.get(key) or []) > floor:
        keep = max(floor, len(result[key]) // 2)
        result[key] = result[key][:keep]
        result[f"{key}_truncated_to"] = keep
        envelope["budget"]["degraded"].append({"what": key, "to": str(keep)})
        if entry is None:
            entry = {"code": "LIST_TRUNCATED", "field": key, "fix": fix}
            envelope.setdefault("omitted", []).append(entry)
        entry["detail"] = (
            f"{total - keep} of {total} {key} were dropped to fit "
            f"max_tokens={max_tokens}; the first {keep} remain, in the order they "
            f"were already in. `{key}_truncated_to` records the cut.")


NO_API_COST = {"api_calls": 0, "by_kind": {}, "note": "local computation only"}


def wrap(*, query: dict, result: dict, cost: dict, max_tokens: int,
         degraded: Optional[list] = None) -> dict:
    """The response skeleton every non-query verb returns.

    CLAUDE.md's response discipline -- contract, echoed query, cost, budget with
    `used_est` -- was hand-spelled in seven verbs. `build()` covers the faceted query
    shape; this covers everything else, so the contract is kept true in one place.

    Like `build`, it declares when it has overshot. `build` had the BUDGET_FLOOR note
    and this did not, so a verb wrapped here could exceed `max_tokens` in silence --
    either after its own list ladder bottomed out, or entirely, for the verbs that
    have no ladder at all. An oversized response is sometimes unavoidable; an
    undeclared one leaves the caller believing it asked for a budget and got it.
    """
    envelope = {
        "ok": True, "api": {"contract": CONTRACT}, "query": query, "result": result,
        "cost": cost, "budget": {"max_tokens": max_tokens, "degraded": list(degraded or [])},
    }
    used = estimate_tokens(envelope)
    if used > max_tokens:
        # Added before used_est is recorded, so the note counts toward the total it
        # explains -- the same ordering build() relies on.
        envelope.setdefault("notes", []).append(
            f"BUDGET_FLOOR: this response is {used} estimated tokens against "
            f"max_tokens={max_tokens} and could not be reduced further. Nothing was "
            f"truncated mid-structure.")
        envelope.setdefault("omitted", []).append({
            "code": "BUDGET_EXCEEDED",
            "detail": (f"the response did not fit max_tokens={max_tokens}; it is "
                       "returned whole rather than cut mid-structure"),
            "fix": {"note": "ask for a larger budget, or narrow what was requested",
                    "retry_with": [{**query, "max_tokens": max_tokens * 2}]},
        })
        used = estimate_tokens(envelope)
    envelope["budget"]["used_est"] = used
    return envelope

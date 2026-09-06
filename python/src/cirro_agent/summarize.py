"""Summarizers for the things whose natural form does not fit in a context window.

Each takes plain data (a file list, log text, task dicts) and returns a compact
structure, so all of it is testable without a tenant. The recurring trick is to
collapse repetition into a template plus a count: 480 filenames become one line,
and 4,000 identical log lines become one row with n=4000.
"""
import math
import re
from collections import Counter, defaultdict
from typing import Any, Optional

from . import errors
from .query import parse_dt

# Suffixes that are a compression wrapper rather than the real file type.
_WRAPPERS = {".gz", ".bz2", ".zst", ".xz", ".z", ".bgz"}

_HUMAN_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_bytes(size: Optional[int]) -> Optional[str]:
    if size is None:
        return None
    if size <= 0:
        return "0 B"
    exponent = min(int(math.log(size, 1000)), len(_HUMAN_UNITS) - 1)
    scaled = size / (1000 ** exponent)
    return f"{scaled:.1f} {_HUMAN_UNITS[exponent]}" if exponent else f"{size} B"


def split_ext(path: str) -> str:
    parts = path.rsplit("/", 1)[-1].split(".")
    if len(parts) == 1:
        return ""
    suffixes = ["." + p.lower() for p in parts[1:]]
    if len(suffixes) >= 2 and suffixes[-1] in _WRAPPERS:
        return suffixes[-2] + suffixes[-1]
    return suffixes[-1]


def percentile(values: list, fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


# --------------------------------------------------------------------------- files

_TRAILING_DIGITS = re.compile(r"^(.*?)(\d+)$")


def _token_shape(token: str) -> str:
    if token.isdigit():
        return "d"
    if token.isalpha():
        return "a"
    return "m"


def _collapse_values(values: list) -> str:
    """Render a varying token position as compactly as it can honestly be shown."""
    distinct = sorted(set(values))
    if len(distinct) == 1:
        return distinct[0]
    if len(distinct) <= 4:
        # R1/R2 -> R{1,2}: a shared alpha prefix with differing numeric suffixes.
        matches = [_TRAILING_DIGITS.match(v) for v in distinct]
        if all(matches):
            prefixes = {m.group(1) for m in matches}
            if len(prefixes) == 1 and prefixes != {""}:
                numbers = ",".join(m.group(2) for m in matches)
                return f"{prefixes.pop()}{{{numbers}}}"
        return "{" + ",".join(distinct) + "}"
    if all(v.isdigit() for v in distinct):
        return "{N}"
    return "{sample}"


_SEPARATOR = re.compile(r"([_\-])")


def name_patterns(paths: list, limit: int = 8) -> list:
    """Fold filenames into templates.

    The single highest-leverage summarizer: it turns a 480-line manifest into one
    line, and the template it produces IS the glob the caller needs next.
    """
    groups: dict = defaultdict(list)
    for path in paths:
        directory, _, basename = path.rpartition("/")
        ext = split_ext(basename)
        stem = basename[: len(basename) - len(ext)] if ext else basename
        pieces = _SEPARATOR.split(stem)
        tokens = pieces[::2]
        separators = tuple(pieces[1::2])
        shape = tuple(_token_shape(t) for t in tokens)
        # Separators are part of the key so the template can rejoin with the ones the
        # filenames actually used.
        groups[(directory, ext, shape, separators)].append(tokens)

    out = []
    for (directory, ext, _shape, separators), token_lists in groups.items():
        width = len(token_lists[0])
        rendered = [
            _collapse_values([tokens[i] for tokens in token_lists]) for i in range(width)
        ]
        joined = rendered[0] if rendered else ""
        for index, separator in enumerate(separators):
            joined += separator + rendered[index + 1]
        template = joined + ext
        if directory:
            template = f"{directory}/{template}"
        glob = re.sub(r"\{[^}]*\}", "*", template)
        out.append({"template": template, "n": len(token_lists), "glob": glob})
    out.sort(key=lambda entry: -entry["n"])
    return out[:limit]


def directory_tree(entries: list, depth: int = 2, limit: int = 12) -> list:
    rollup: dict = defaultdict(lambda: {"files": 0, "bytes": 0})
    for entry in entries:
        parts = entry["path"].split("/")[:-1][:depth]
        key = "/".join(parts) if parts else "."
        rollup[key]["files"] += 1
        rollup[key]["bytes"] += entry.get("size") or 0
    nodes = [
        {"path": path, "files": data["files"], "bytes": data["bytes"],
         "bytes_human": human_bytes(data["bytes"])}
        for path, data in rollup.items()
    ]
    nodes.sort(key=lambda node: -node["files"])
    truncated = len(nodes) > limit
    nodes = nodes[:limit]
    if truncated:
        nodes.append({"path": "...", "note": f"{len(rollup) - limit} more directories"})
    return nodes


def files(entries: list, *, total_files: Optional[int] = None,
          artifact_types: Optional[list] = None, top_k: int = 8) -> dict:
    """Summarize a file manifest that may hold 100,000 entries."""
    paths = [e["path"] for e in entries]
    sizes = [e.get("size") for e in entries]
    known = [s for s in sizes if isinstance(s, (int, float))]

    by_ext: dict = defaultdict(lambda: {"n": 0, "bytes": 0, "sizes": []})
    for entry in entries:
        ext = split_ext(entry["path"]) or "(none)"
        bucket = by_ext[ext]
        bucket["n"] += 1
        if isinstance(entry.get("size"), (int, float)):
            bucket["bytes"] += entry["size"]
            bucket["sizes"].append(entry["size"])

    extensions = sorted(
        (
            {"ext": ext, "n": data["n"], "bytes": data["bytes"],
             "bytes_human": human_bytes(data["bytes"]),
             "p50": human_bytes(percentile(data["sizes"], 0.5)),
             "max": human_bytes(max(data["sizes"])) if data["sizes"] else None}
            for ext, data in by_ext.items()
        ),
        key=lambda item: -item["n"],
    )

    largest = sorted(
        (e for e in entries if isinstance(e.get("size"), (int, float))),
        key=lambda e: -e["size"],
    )[:5]

    patterns = name_patterns(paths)
    out = {
        "total_files": total_files if total_files is not None else len(entries),
        "listed_files": len(entries),
        "bytes_known": sum(known),
        "bytes_known_human": human_bytes(sum(known)),
        # FileEntry.size can be absent from the API, so never imply a complete total.
        "bytes_known_for": f"{len(known)} of {len(entries)} listed files",
        "tree": directory_tree(entries),
        "extensions": extensions[:top_k],
        "name_patterns": patterns,
        "largest": [{"path": e["path"], "size_human": human_bytes(e["size"])}
                    for e in largest],
        "suggested_globs": [
            {"glob": p["glob"], "matches": p["n"]} for p in patterns[:5]
        ],
    }
    if artifact_types:
        out["artifact_types"] = artifact_types
    if total_files is not None and total_files > len(entries):
        out["note"] = (
            f"{total_files - len(entries)} files beyond the {len(entries)} listed were not "
            "read; counts above cover the listed subset only"
        )
    return out


# --------------------------------------------------------------------------- logs

_ERROR_MARKERS = (
    "ERROR ~", "Caused by:", "Command error:", "Command exit status:",
    "Execution aborted", "Traceback (most recent call last)",
    ".command.sh: line", "OutOfMemoryError", "Killed",
)

_SIG_PATTERNS = (
    (re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{2}/[0-9a-f]{6,}\b", re.I), "<hash>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<ts>"),
    (re.compile(r"\bs3://\S+"), "<s3>"),
    (re.compile(r"/\S{8,}"), "<path>"),
    # No \b here: an underscore is a word character, so \b\d+\b would leave
    # `sample_41` unfolded and every per-sample line would look distinct.
    (re.compile(r"\d+"), "<n>"),
)


def log_signature(line: str) -> str:
    text = line.strip()
    for pattern, token in _SIG_PATTERNS:
        text = pattern.sub(token, text)
    return text[:160]


def logs(text: str, *, head: int = 30, tail: int = 60, max_blocks: int = 5) -> dict:
    if not text:
        return {
            "bytes": 0, "lines": 0, "empty": True,
            "note": (
                "EMPTY_LOG_MAY_BE_TRANSIENT: the SDK caches this value on the dataset "
                "object, so an empty read taken during startup stays empty forever. "
                "Re-fetch the dataset and read again."
            ),
        }
    lines = text.splitlines()

    signatures = Counter(log_signature(line) for line in lines if line.strip())
    repeated = [
        {"signature": sig, "n": n}
        for sig, n in signatures.most_common(8) if n > 2
    ]

    blocks = []
    for index, line in enumerate(lines):
        if not any(marker in line for marker in _ERROR_MARKERS):
            continue
        if blocks and index - blocks[-1]["_end"] < 3:
            continue
        excerpt = lines[index: index + 25]
        signature = log_signature(line)
        blocks.append({
            "first_line_no": index + 1,
            "excerpt": excerpt,
            "_end": index + len(excerpt),
            "n_similar": signatures.get(signature, 1),
        })
        if len(blocks) >= max_blocks:
            break
    for block in blocks:
        block.pop("_end", None)

    return {
        "bytes": len(text),
        "lines": len(lines),
        "head": lines[:head],
        # Compared against head + tail, not head: otherwise a 40-line log with
        # head=30/tail=60 returns the first 30 lines and then all 40 again.
        "tail": lines[head:][-tail:],
        "error_blocks": blocks,
        "signature_counts": repeated,
        "raw_available": {
            "note": "pass raw=true with line_range or byte_range to read verbatim text",
            "max_line": len(lines),
        },
    }


# --------------------------------------------------------------------------- tasks

_TAG_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def task_process(name: str) -> str:
    """`FASTQC (sample_41)` -> `FASTQC`, so 480 per-sample tasks collapse to one row."""
    return _TAG_SUFFIX.sub("", name or "").strip()


def duration_s(task: dict) -> Optional[float]:
    """A task's wall time, or None when the timestamps cannot support one.

    Cirro reports a task that never reached a given state with the Unix epoch rather
    than a null, so a task that never started came back as `now - 1970` -- one such
    task put a duration_p95_s of 1788530505 (56 years) in front of a user, and a
    stop-before-start pair produced a p50 of -99. Both flowed into the percentiles
    people read to judge a run, so an unusable pair has to become None here rather
    than a number. This is the one implementation: rows.task stores what it returns.
    """
    start, stop = task.get("started_at"), task.get("stopped_at")
    if not start or not stop:
        return None
    try:
        began, ended = parse_dt(start), parse_dt(stop)
    except errors.AgentError:
        return None
    if began.timestamp() <= 0 or ended.timestamp() <= 0:
        return None  # an epoch timestamp is "never", not 1970
    seconds = (ended - began).total_seconds()
    return seconds if seconds >= 0 else None


def tasks(task_dicts: list, *, primary_failed_index: Optional[int] = None,
          detail_limit: int = 5) -> dict:
    if not task_dicts:
        return {"n_tasks": 0, "note": "the execution reported no tasks yet"}

    by_status = Counter(str(t.get("status", "UNKNOWN")).upper() for t in task_dicts)
    by_process: dict = defaultdict(lambda: {"n": 0, "failed": 0, "durations": []})
    for task in task_dicts:
        bucket = by_process[task_process(task.get("name", ""))]
        bucket["n"] += 1
        if "FAIL" in str(task.get("status", "")).upper():
            bucket["failed"] += 1
        duration = duration_s(task)
        if duration is not None:
            bucket["durations"].append(duration)

    processes = sorted(
        (
            {"process": name, "n": data["n"], "failed": data["failed"],
             "duration_p50_s": percentile(data["durations"], 0.5),
             "duration_p95_s": percentile(data["durations"], 0.95)}
            for name, data in by_process.items()
        ),
        key=lambda item: (-item["failed"], -item["n"]),
    )

    failed = [t for t in task_dicts if "FAIL" in str(t.get("status", "")).upper()]
    # The root cause first: in a fan-out failure the other entries are consequences.
    if primary_failed_index is not None:
        failed.sort(key=lambda t: t.get("task_id") != primary_failed_index)

    detail = []
    for task in failed[:detail_limit]:
        detail.append({
            "task_id": task.get("task_id"),
            "name": task.get("name"),
            "status": task.get("status"),
            "exit_code": task.get("exit_code"),
            "status_message": task.get("status_message"),
            "container_image": task.get("container_image"),
            "duration_s": duration_s(task),
            "is_root_cause": task.get("task_id") == primary_failed_index,
        })

    names = Counter(task.get("name", "") for task in task_dicts)
    retries = [{"name": name, "attempts": n} for name, n in names.most_common(5) if n > 1]

    return {
        "n_tasks": len(task_dicts),
        "counts_by_status": dict(by_status),
        "counts_by_process": processes[:12],
        "failed": detail,
        "n_failed": len(failed),
        "exit_codes": dict(Counter(
            str(t.get("exit_code")) for t in task_dicts if t.get("exit_code") is not None
        )),
        "retries": retries,
    }


# --------------------------------------------------------------------------- cost

# What the run paid for without producing anything: a task that failed or was
# abandoned still burned the instance it was running on. `CACHED` is the opposite --
# a resumed run pays nothing for it -- so it is neither wasted nor spent.
_WASTED_STATUSES = ("FAILED", "ABORTED", "CANCELLED", "KILLED")


def cost(record: dict, *, detail_limit: int = 10) -> dict:
    """Where an analysis's money went, by task status and by process.

    A 500-task run has 500 task costs, which is the thing not to return. What a reader
    actually acts on is the per-process rollup -- one line saying ALIGN cost 80% of the
    run -- so the individual tasks appear only as the few most expensive.
    """
    task_costs = record.get("tasks") or []
    total = record.get("total_cost")

    by_status = {}
    for group in record.get("groups") or []:
        name = str(group.get("name") or "UNKNOWN").upper()
        if isinstance(group.get("cost"), (int, float)):
            by_status[name] = round(group["cost"], 4)

    by_process: dict = defaultdict(lambda: {"n": 0, "cost": 0.0})
    for entry in task_costs:
        bucket = by_process[entry.get("process") or "(unnamed)"]
        bucket["n"] += 1
        if isinstance(entry.get("cost"), (int, float)):
            bucket["cost"] += entry["cost"]

    processes = sorted(
        ({"process": name, "n": data["n"], "cost": round(data["cost"], 4),
          "share": round(data["cost"] / total, 4) if total else None}
         for name, data in by_process.items()),
        key=lambda item: -item["cost"],
    )

    priciest = sorted(
        (t for t in task_costs if isinstance(t.get("cost"), (int, float))),
        key=lambda t: -t["cost"],
    )[:detail_limit]

    body = {
        "total_cost": round(total, 4) if isinstance(total, (int, float)) else None,
        "unit": "$",
        # Not a forecast and not a bill: an estimate is modelled from instance
        # pricing, so say which one this is rather than leaving it to be assumed.
        "is_estimate": record.get("is_estimate"),
        "n_tasks": len(task_costs),
        "cost_by_status": by_status,
        "cost_by_process": processes[:15],
        "most_expensive_tasks": [
            {"task_id": t.get("task_id"), "name": t.get("name"),
             "status": t.get("status"), "cost": round(t["cost"], 4)}
            for t in priciest
        ],
    }
    wasted = sum(value for name, value in by_status.items()
                 if any(marker in name for marker in _WASTED_STATUSES))
    if wasted:
        body["wasted_cost"] = round(wasted, 4)
        body["wasted_share"] = round(wasted / total, 4) if total else None
    if not task_costs:
        body["note"] = ("the run reported no per-task costs; whatever the total is, "
                        "it is the head node alone")
    return body


# --------------------------------------------------------------------------- samples

def _infer_dtype(values: list) -> str:
    non_null = [v for v in values if v not in (None, "")]
    if not non_null:
        return "empty"
    if all(isinstance(v, bool) for v in non_null):
        return "bool"
    numeric = 0
    for value in non_null:
        try:
            float(value)
            numeric += 1
        except (TypeError, ValueError):
            pass
    if numeric == len(non_null):
        return "numeric"
    return "string"


def samples(sample_dicts: list, *, schema_keys: Optional[list] = None,
            example_rows: int = 3) -> dict:
    n = len(sample_dicts)
    if not n:
        return {"n_samples": 0, "note": "no samples registered in this project"}

    columns: dict = defaultdict(list)
    for sample in sample_dicts:
        metadata = sample.get("metadata") or {}
        for key, value in metadata.items():
            columns[key].append(value)

    keys = []
    inconsistencies = []
    for key, values in sorted(columns.items()):
        filled = [v for v in values if v not in (None, "")]
        distinct = {str(v) for v in filled}
        dtype = _infer_dtype(values)
        entry = {
            "key": key,
            "fill_rate": round(len(filled) / n, 2),
            "dtype_inferred": dtype,
            "cardinality": len(distinct),
        }
        if dtype == "numeric" and filled:
            numbers = [float(v) for v in filled]
            entry["numeric_summary"] = {
                "min": min(numbers), "p50": percentile(numbers, 0.5), "max": max(numbers),
            }
        elif len(distinct) <= 12:
            entry["top_values"] = [
                {"value": value, "n": count}
                for value, count in Counter(str(v) for v in filled).most_common(12)
            ]
        keys.append(entry)

        # Variants that would collapse under normalization are what actually breaks
        # a downstream group-by, and they are cheap to detect here.
        normalized: dict = defaultdict(set)
        for value in distinct:
            normalized[value.strip().lower()].add(value)
        collisions = {k: sorted(v) for k, v in normalized.items() if len(v) > 1}
        if collisions:
            inconsistencies.append({
                "key": key, "kind": "case_or_whitespace_variants",
                "examples": list(collisions.values())[:3],
            })
        if len(values) < n:
            inconsistencies.append({
                "key": key, "kind": "missing_on_some_samples",
                "present_on": len(values), "of": n,
            })

    out = {
        "n_samples": n,
        "keys": keys,
        # Exactly the keys worth passing to group_by.
        "candidate_group_keys": [
            k["key"] for k in keys
            if 2 <= k["cardinality"] <= 20 and k["fill_rate"] >= 0.8
        ],
        "candidate_id_keys": [k["key"] for k in keys if k["cardinality"] == n],
        "inconsistencies": inconsistencies[:10],
        "examples": [
            {"id": s.get("id"), "name": s.get("name"), "metadata": s.get("metadata")}
            for s in sample_dicts[:example_rows]
        ],
    }
    if schema_keys is not None:
        observed = set(columns)
        declared = set(schema_keys)
        out["schema_drift"] = {
            "declared_not_observed": sorted(declared - observed),
            "observed_not_declared": sorted(observed - declared),
        }
    return out


# --------------------------------------------------------------------------- params

_SECRET_HINT = re.compile(r"(secret|password|token|api[_-]?key|credential)", re.I)
_MAX_VALUE_CHARS = 300


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return {"truncated": True, "full_len": len(value),
                "head": value[:_MAX_VALUE_CHARS]}
    return value


def params(actual: dict, *, defaults: Optional[dict] = None,
           declared: Optional[list] = None, required: Optional[list] = None) -> dict:
    """Diff the params a run used against the process spec.

    Only the departures from default are interesting, and `unknown` is the one that
    matters most: a mistyped parameter silently does nothing, which is an expensive
    way to discover a typo six hours into a pipeline.
    """
    actual = actual or {}
    defaults = defaults or {}
    declared_set = set(declared) if declared is not None else None

    non_default, defaulted, unknown = {}, [], []
    for key, value in sorted(actual.items()):
        if _SECRET_HINT.search(key):
            non_default[key] = "<redacted>"
            continue
        if declared_set is not None and key not in declared_set:
            unknown.append(key)
            continue
        if key in defaults and defaults[key] == value:
            defaulted.append(key)
            continue
        non_default[key] = _clip(value)

    out = {
        "non_default": non_default,
        "n_defaulted": len(defaulted),
        "defaulted_keys": defaulted if len(defaulted) <= 15 else defaulted[:15] + ["..."],
        "unknown": unknown,
        "n_params": len(actual),
    }
    if unknown:
        out["unknown_note"] = (
            "These keys are not declared by the process parameter spec. Cirro will "
            "accept them and the pipeline will ignore them -- usually a typo."
        )
    if required:
        out["missing_required"] = [key for key in required if key not in actual]
    return out


# --------------------------------------------------------------------------- trace


def trace(rows: list) -> dict:
    """Per-process resource rollup from the Nextflow trace. Never the raw frame."""
    if not rows:
        return {"n_tasks": 0, "note": "no trace file was published for this run"}

    by_process: dict = defaultdict(lambda: {
        "n": 0, "durations": [], "peak_rss": [], "cpu_pct": [], "allocated": [],
    })
    for row in rows:
        bucket = by_process[task_process(str(row.get("name") or row.get("process") or ""))]
        bucket["n"] += 1
        for source, target in (("realtime", "durations"), ("peak_rss", "peak_rss"),
                               ("%cpu", "cpu_pct"), ("memory", "allocated")):
            value = row.get(source)
            if isinstance(value, (int, float)):
                bucket[target].append(value)

    processes = []
    for name, data in by_process.items():
        peak = max(data["peak_rss"]) if data["peak_rss"] else None
        allocated = max(data["allocated"]) if data["allocated"] else None
        entry = {
            "process": name, "n": data["n"],
            "duration_p50_s": percentile(data["durations"], 0.5),
            "duration_p95_s": percentile(data["durations"], 0.95),
            "peak_rss_human": human_bytes(int(peak)) if peak else None,
            "allocated_human": human_bytes(int(allocated)) if allocated else None,
            "cpu_p50_pct": percentile(data["cpu_pct"], 0.5),
        }
        if peak and allocated:
            ratio = peak / allocated
            entry["headroom_ratio"] = round(ratio, 2)
            if ratio < 0.35:
                entry["flag"] = "over_provisioned"
            elif ratio > 0.9:
                entry["flag"] = "near_limit"
        processes.append(entry)

    processes.sort(key=lambda item: -(item["duration_p95_s"] or 0))
    return {"n_tasks": len(rows), "processes": processes[:15]}

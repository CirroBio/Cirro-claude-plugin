---
name: cirro-monitor-analysis
description: Check whether a Cirro analysis finished, and diagnose why one failed. Use when the user asks about the status of a run, whether something is done, why a pipeline failed, what went wrong, or to look at logs, tasks, resource usage, or what a run cost for a dataset. Read-only.
---

# Track and diagnose a Cirro run

Read-only throughout. Nothing here needs approval.

## The trap to know about first

SDK objects hold a snapshot from when they were built, and `logs` and `tasks` are
cached on them. **A status you already have will never change on its own.** Every
check must re-fetch:

```bash
cirro-agent read analysis-status -p "My Project" -d ds_123
```

That subcommand always bypasses the cache, which is why it exists as its own command
rather than being a flag on `dataset`.

If a log comes back empty, that may be transient — a log read during startup stays
empty forever on the object that read it. The response says
`EMPTY_LOG_MAY_BE_TRANSIENT` when this could be the cause. Re-fetch rather than
concluding there is no log.

## Polling

When the user asks you to wait for a run, poll deliberately: check, wait a sensible
interval for the pipeline's timescale (bioinformatics runs take minutes to hours, so
30–60 seconds between checks is reasonable and every 2 seconds is not), then check
again. Tell the user the current status each time rather than going quiet.

## What is running right now

```bash
cirro-agent read datasets -p "My Project" -f "status:@active"
cirro-agent read datasets --all-projects --confirm-fanout -f "status:@active"
```

## Diagnosing a failure

Work in this order. Each step is cheap and narrows the next.

### 1. The failure message

```bash
cirro-agent read datasets -p "My Project" -f "status:FAILED created:last-7d" \
  --include status_message
```

`status_message` is often the whole answer. Note that failed runs share causes: if
several failures list the same parent dataset or the same message, treat them as one
problem, not several. The response's `shape.lineage` points out shared parents.

### 2. The root-cause task

```bash
cirro-agent read tasks -p "My Project" -d ds_failed
```

This collapses a 480-way fan-out into one row per process, and lists the failed tasks
with the **root cause first**, flagged `is_root_cause`. In a fan-out failure the other
entries are usually consequences of that one — read it before reading any logs.

`WRONG_EXECUTOR` here means the pipeline is not Nextflow and has no task model. That
is a typed error rather than an empty list precisely so it cannot be misread as
"nothing failed". Fall back to logs.

### 3. That task's log

```bash
cirro-agent read task-logs -p "My Project" -d ds_failed --task-id 41
```

### 4. The workflow log

```bash
cirro-agent read logs -p "My Project" -d ds_failed
```

Never expect the whole log. You get the head, the tail, extracted error blocks with
line numbers, and repeated lines collapsed into one row with a count — 4,000
`Submitted process >` lines become a single entry. If you need verbatim text, use the
line numbers from an error block:

```bash
cirro-agent read logs -p P -d ds_failed --raw --line-range 4180 4260
```

### 5. Resources, if it looks like a limit

```bash
cirro-agent read trace -p "My Project" -d ds_failed
```

Per-process peak memory against what was allocated, flagged `near_limit` or
`over_provisioned`. An exit code of 137 plus `near_limit` is an out-of-memory kill.

### 6. Parameters, if it looks like a configuration problem

```bash
cirro-agent read params -p "My Project" -d ds_failed
```

Shows what departed from the defaults, and flags `unknown` keys — parameters the
pipeline silently ignored. A typo there produces a run that "worked" but did the wrong
thing.

### 7. What the run cost, if that is the question

```bash
cirro-agent read cost -p "My Project" -d ds_123
```

Per-task compute cost, grouped by task status and rolled up by pipeline step with each
step's `share` of the total. On a failed run `wasted_cost` is the money spent on tasks
that failed or were aborted — worth reporting alongside the diagnosis, because it is
what a retry saves. `CACHED` tasks cost nothing, so a resume is cheaper than the first
attempt for exactly that reason.

`is_estimate: true` means the figure is modelled from instance pricing rather than a
settled bill; say so rather than quoting it as the invoice. An uploaded dataset has no
execution and so no cost — that comes back as `WRONG_EXECUTOR`, which is the answer
rather than something to retry.

## Reporting back

Say what failed, at which task, and why, in plain terms. Distinguish what you observed
from what you infer. Common shapes:

- exit 137 or `near_limit` in the trace → out of memory; needs more allocation
- "No such file" in `status_message` → a missing or misnamed input
- container or registry errors → infrastructure, not the user's data
- one failed task in a large fan-out → often one bad sample, not a broken pipeline

Then offer the next step — usually resuming with a change, which the
**cirro-run-analysis** skill handles.

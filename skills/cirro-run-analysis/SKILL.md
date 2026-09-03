---
name: cirro-run-analysis
description: Launch, rerun, resume, or stop a Cirro pipeline on a dataset. Use when the user wants to run an analysis, process data through a pipeline, re-run something that failed, resume a partial run, or cancel a run in progress. Requires the user's approval before anything is launched.
---

# Run a Cirro pipeline

Launching an analysis creates a dataset and consumes compute, so it goes through the
approval gate. Never skip a step below to save a round trip.

## 1. Resolve the inputs

```bash
cirro-agent read datasets -p "My Project" -f "name~*something*"
```

Get the **dataset ID**, not just the name. Mutations refuse names — names collide and
change — and the error will hand you the ID anyway, so ask for it now.

## 2. Choose the pipeline

```bash
cirro-agent read processes -f "executor:NEXTFLOW"
cirro-agent read processes -f "q:rnaseq"
```

## 3. Read the parameter spec — always

```bash
cirro-agent read process-params "nf-core/rnaseq v3.14"
```

This is the authoritative list of accepted keys, which are required, and their
defaults. **Do not construct `params` from memory or from another pipeline's shape.**

A key that is not in this list will be accepted by the API and silently ignored by the
pipeline. Discovering that after a six-hour run is the expensive way. `plan` warns
about undeclared keys, but reading the spec first is how you avoid them.

If a previous run of the same pipeline worked, its parameters are a good starting
point:

```bash
cirro-agent read params -p "My Project" -d ds_previous
```

That shows only what departed from the defaults, which is the interesting part.

## 4. Plan

Call the `plan` MCP tool:

```
plan(op="run_analysis", args={
  "project_id": "proj_...",
  "process": "proc_...",
  "name": "RNAseq batch 32",
  "source_dataset_ids": ["ds_..."],
  "params": {...},
  "folder": "Analyses/RNAseq",                        # optional Cirro folder
  "notification_emails": ["someone@example.org"]      # optional
})
```

`plan` changes nothing. It resolves names to IDs, validates the parameters against the
spec, counts the input files and bytes, resolves the compute environment, and returns
a `rendered` summary plus a single-use `plan_id` that expires in ten minutes.

If `plan` returns an error, fix the cause and plan again. Do not attempt `apply`.

**Filing the output.** Pass `folder` (or `folders`) in this same call to put the
output dataset straight into a Cirro folder. Folder membership lives in the dataset's
tag list and the launch request carries tags, so it costs no extra call and no second
approval. Launching and then moving is two approvals for one intention.

## 5. Show the plan to the user, then apply

**Put the `rendered` text in your own message to the user.** Do not summarize it, do
not paraphrase the parameters, and do not call `apply` in the same breath as if
approval were a formality. The person needs to see the pipeline, the inputs, and the
full parameter list before they agree.

Then:

```
apply(plan_id=..., digest=..., approved_summary=...)
```

Copy `digest` and `approved_summary` verbatim from the plan result. `apply` takes no
operation arguments at all — that is the point: what was approved is what runs.

`apply` prompts the user for approval every time. That is expected; it cannot be
turned off, and you should not try.

## 6. Hand off to monitoring

`apply` returns `created_dataset_id` and a `follow_with` call. Report the ID and offer
to track it. Do not poll in a loop here — the **cirro-monitor-analysis** skill owns
that, including the re-fetch discipline a running dataset requires.

## Running on data that is about to be uploaded

If the data is not in Cirro yet, do not upload, wait, then plan the run as a second
approval. Chain them:

```
plan(op="chain", args={"steps": [
  {"label": "upload", "op": "upload_dataset",
   "args": {"project_id": "proj_...", "process": "proc_ingest_...",
            "name": "...", "source_dir": "/local/path", "folder": "..."}},
  {"label": "run", "op": "run_analysis",
   "args": {"project_id": "proj_...", "process": "proc_...", "name": "...",
            "params": {...},
            "source_dataset_ids": [{"from_step": 0, "field": "dataset_id"}]}},
]})
```

The run's input is `<dataset_id of step 1: upload>` — resolved by the server from the
upload's actual result, never supplied by you. One prompt instead of two, and the
approval text still names every concrete value.

## Resuming a failed run

```
plan(op="resume_analysis", args={"project_id": "...", "resume_dataset_id": "ds_failed"})
```

This inherits the original pipeline, inputs and parameters, so you only need to
override what should change. Diagnose the failure first with
**cirro-monitor-analysis** — resuming an out-of-memory failure without changing
anything just fails again more slowly.

## Stopping a run

```
plan(op="stop_analysis", args={"project_id": "...", "dataset_id": "ds_running"})
```

Refused with `WRONG_STATUS` if the run has already finished. Partial output is kept; a
stopped run cannot be un-stopped, only resumed as a new run.

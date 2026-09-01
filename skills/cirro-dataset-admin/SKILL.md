---
name: cirro-dataset-admin
description: Rename, retag, or delete a Cirro dataset, or replace its samplesheet. Use when the user wants to change a dataset's name, description or tags, fix or update a samplesheet, or remove a dataset. Requires the user's approval before any change.
---

# Modify or remove a Cirro dataset

Every operation here goes through `plan` → show the user → `apply`.

## Always resolve the ID first

```bash
cirro-agent read datasets -p "My Project" -f "name~*batch31*"
```

These operations refuse names. If you pass one you get `NAME_REQUIRED_ID` with the
candidates and their IDs — which is a hint, not a failure to work around. The explicit
ID also lands in the transcript, which is the audit trail.

## Rename, describe, retag

```
plan(op="update_dataset", args={
  "project_id": "proj_...", "dataset_id": "ds_...",
  "name": "...", "description": "...", "tags": ["..."]})
```

Supply only the fields that should change. Tags are **replaced wholesale**, not
merged: read the current tags first and include the ones being kept.

The plan shows each field as `from` → `to`. If nothing would actually change, the plan
refuses rather than doing a no-op write.

## Samplesheet

```
plan(op="update_samplesheet", args={
  "project_id": "proj_...", "dataset_id": "ds_...",
  "contents": "sample,fastq_1,fastq_2\n..."})     # or "file_path": "/path/to/sheet.csv"
```

The plan renders a **unified diff** against the current samplesheet. Show that diff to
the user — it is the only thing that makes this change reviewable. If the plan warns
that no current samplesheet could be read, the diff is against an empty file and you
are effectively creating one; say so.

A samplesheet governs how a pipeline interprets the dataset's files, so a wrong one
produces a run that succeeds and is wrong.

## Delete

```
plan(op="delete_dataset", args={"project_id": "proj_...", "dataset_id": "ds_..."})
```

The plan reports the file count, the byte total, who created it and when, and — the
part that matters most — **which datasets in the project list this one as a source**.
Those are analyses derived from it, and their lineage will point at something that no
longer exists.

Read the warnings out to the user explicitly. Do not present a delete as routine, and
do not batch several deletes into one message as though they were a single decision.

There is no undo from here.

## Sample metadata

```bash
cirro-agent read samples -p "My Project"
```

Read this first. It reports fill rates, candidate grouping keys, and
**inconsistencies** — `"Tumor"` versus `"tumor"` versus `" Tumor"` are three distinct
values that will split any group-by, and that is usually the real problem when
metadata "looks fine" but analyses disagree.

```
plan(op="update_sample", args={
  "project_id": "proj_...", "sample_id": "smp_...", "metadata": {...}})
```

The plan shows only the keys whose values differ.

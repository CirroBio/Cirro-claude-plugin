---
name: cirro-dataset-admin
description: Rename, retag, or delete a Cirro dataset, or replace its samplesheet. Use when the user wants to change a dataset's name, description or tags, fix or update a samplesheet, or remove a dataset. Requires the user's approval before any change.
---

# Modify or remove a Cirro dataset

Every operation here goes through `plan` → show the user → `apply`.

## Several datasets, one approval

`update_dataset` takes `dataset_ids` as well as `dataset_id`. "Move those three into
QC" is one plan and one approval prompt that names all three, not three of each.

Each dataset's current record is read separately, so a batch move preserves each one's
own tags rather than flattening them onto whichever record was read first.

Two things it will not do:

- **`name` and `description` are refused for a batch** — they would give every dataset
  the same name. Edit those one at a time.
- **`tags` replaces the list on every dataset in the batch**, so tags unique to one of
  them are dropped. The plan warns about this; read the warning to the user rather
  than past it.

Datasets that already match are named in a warning and left alone, so re-running a
move is not an error.

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

### This also updates the project's sample metadata

Two things happen, and the second is the one people do not expect:

1. `samplesheet.csv` is replaced on the dataset.
2. The dataset's samples are **re-ingested**, so the project's sample metadata is
   rebuilt from the new CSV.

Writing the CSV does not register anything by itself — the ingest is a separate call.
Without it the samplesheet and the sample records it describes silently disagree, and
`cirro-agent read samples -p ...` keeps answering with the old values. So step 2 is on
by default. It is queued rather than immediate: apply returns as soon as Cirro accepts
it, and the project's samples catch up shortly after, so a `samples` read taken right
away may still show the old values.

Re-ingest **overwrites** sample fields from the samplesheet, including values someone
edited by hand since the last ingest. The plan says so in its warnings; pass that on.

To replace the file and leave the sample records alone:

```
plan(op="update_samplesheet", args={..., "ingest_samples": False})
```

The rendered plan states which of the two you are getting, in both directions. Read it
rather than assuming the default.

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

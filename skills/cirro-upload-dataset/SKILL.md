---
name: cirro-upload-dataset
description: Upload local files into Cirro as a new dataset, or upload reference data such as a genome or annotation. Use when the user wants to upload, ingest, import, or add data to a Cirro project from their own machine. Requires the user's approval before anything is transferred.
---

# Upload into Cirro

## 1. Know what is being uploaded

```bash
ls -la /path/to/folder
```

Uploads take a **folder**, and everything under it goes up. Confirm with the user
which directory they mean before planning, especially if it contains more than they
probably intend.

## 2. Pick the data type

An upload is registered against an **ingest process** — Cirro's term for a data type.
This is not the same list as the analysis pipelines:

```bash
cirro-agent read processes -f "executor:INGEST"
```

Getting this wrong is the most common upload failure: the data type determines the
file-name rules Cirro enforces and which pipelines can later consume the dataset. If
the right one is not obvious from the filenames, ask the user rather than guessing.

Look at `file_requirements_message` on the process — it states the naming
convention in plain language.

## 3. Plan

```
plan(op="upload_dataset", args={
  "project_id": "proj_...",
  "process": "proc_ingest_...",
  "name": "Sequencing run 2026-08-27",
  "source_dir": "/path/to/local/files",
  "folder": "Sequencing/2026",   # optional: the Cirro folder to file it under
  "description": "...",          # optional
  "include_hidden": false        # optional
})
```

`source_dir` is the local directory to read from; `folder` is the Cirro folder the
dataset lands in. Pass `folder` here rather than uploading and then moving it — filing
happens as part of the upload, and a separate move is a second approval for no reason.

`plan` counts the files and bytes, collapses the filenames into templates so you can
see at a glance what is going up, and runs Cirro's own file-name validation for that
data type. Validation problems come back as **warnings on the plan**, which is much
better than a rejected upload halfway through a large transfer — read them.

## 4. Show the plan, then apply

Put the `rendered` text in your own message. The file count, the byte total and the
destination project are exactly what a person needs to check. Then `apply` with the
verbatim `digest` and `approved_summary`.

Large uploads take a while. The tool waits; do not re-issue it if it seems slow, and
never re-run `apply` on the same plan — it is single-use and will refuse.

## 5. Confirm

`apply` returns a `follow_with` call that lists the new dataset with the cache
bypassed. Use it to confirm the dataset exists and report its ID.

## Reference data

Genomes, annotations and indexes are references, not datasets:

```bash
cirro-agent read reference-types      # what this deployment accepts
cirro-agent read references -p P      # what the project already has
```

```
plan(op="upload_reference", args={
  "project_id": "proj_...",
  "name": "GRCh38 primary",
  "reference_type": "<exact name from reference-types>",
  "files": ["/path/to/genome.fa"]
})
```

Reference types are deployment-specific. Always read `reference-types` first; do not
assume a name.

## Public data

To pull from a public repository instead of local disk:

```
plan(op="import_public", args={
  "project_id": "proj_...", "name": "...", "public_ids": ["SRR123456"]})
```

Cirro performs the download. The volume is not known until the transfer starts, so the
plan cannot state it.

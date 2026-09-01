---
name: cirro-download
description: Download files from a Cirro dataset to the local filesystem. Use when the user wants to download, fetch, retrieve, or copy dataset files onto their own machine. Requires the user's approval because it writes to local disk and can be large.
---

# Download from Cirro

Writing to the user's disk is a different risk class from changing Cirro, but it is
still gated: a careless download can be terabytes.

## Read the manifest before deciding anything

```bash
cirro-agent read files -p "My Project" -d ds_123
```

This returns the total file count, a directory rollup, extensions with byte totals,
and **name templates** with ready-made globs:

```
data/{sample}_R{1,2}_001.fastq.gz   x480   glob: data/*_R*_001.fastq.gz
multiqc/multiqc_report.html         x1
```

Use those globs. In almost every case the user wants a specific subset — a QC report,
one sample's outputs, the count matrices — not 1.4 TB of FASTQs. Ask which, rather
than defaulting to everything.

## Often you do not need to download at all

Files can be read straight out of Cirro without landing on disk. If the user wants to
*look at* a result — a QC table, a metrics file, a small CSV — say so and use
**cirro-explore** instead. Downloading a 4 MB report to read one number is wasted time
and disk.

Downloading is right when the user needs the files locally: for another tool, for
archiving, or to work offline.

## Plan and apply

```
plan(op="download_files", args={
  "project_id": "proj_...", "dataset_id": "ds_...",
  "destination": "/path/to/local/dir",
  "glob": "data/*_R1_001.fastq.gz"})       # omit for everything
```

The plan states the file count, the byte total, the destination, and the free space
there — and **warns if it will not fit**. Take that warning seriously; a failed
transfer partway through a full disk is worse than not starting.

Show the `rendered` text to the user, then `apply` with the verbatim `digest` and
`approved_summary`.

Large transfers take a long time. Do not re-issue `apply` — a plan is single-use and
will refuse the second attempt.

## Afterwards

Report where the files landed and how many arrived. If the user's next step is a local
tool, the destination path is the useful thing to hand them.

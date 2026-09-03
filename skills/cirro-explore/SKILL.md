---
name: cirro-explore
description: Answer questions about what data exists in Cirro — projects, datasets, samples, files, pipelines, references, storage. Use when the user asks what data they have, where something is, to find or filter datasets, to summarize a project, or to look up which pipelines are available. Read-only; nothing here modifies anything.
---

# Explore Cirro

All reads go through one Bash binary. It performs no writes, so it is safe to run
freely.

```bash
cirro-agent read --help
```

## The one thing to understand first

Cirro has no server-side filtering, so a listing already fetched everything before it
answered. That makes facets over the **complete** match set free, and it means the
response tells you the shape of all the data even when it only shows you a handful of
rows.

So a broad question is a good first move, not a wasteful one:

```bash
cirro-agent read datasets -p "Immuno-Oncology Core"
```

Read the response in this order:

1. **`counts`** — `matched` is the real total. `rows_are` tells you what the rows
   are. If it says `preview_not_page`, the rows are a handful of **labeled
   exemplars**, each with a `_why`. They are *not* a sample: do not count them,
   average them, or infer distributions from them. `counts` and `facets` are the
   authority.
2. **`facets`** — computed over every match. Statuses, owners, pipelines, tags, and a
   date histogram. This is the answer to "what's in here".
3. **`shape`** — date range, name clusters (each with a ready-made glob), lineage
   depth, and `unavailable`, which names fields that cost extra and how to opt in.
4. **`next_calls`** — runnable follow-ups whose `expect.matched` is **exact**, not a
   guess, and which usually cost zero API calls because the snapshot is cached.
   Prefer one of these over inventing a filter.

Never ask for a bigger `--max-tokens` to get more rows when the facets already answer
the question.

## Filtering

Shorthand covers most cases:

```bash
cirro-agent read datasets -p "My Project" -f "status:FAILED created:last-7d owner:@me"
cirro-agent read datasets -p "My Project" -f "name~*RNAseq* status:@active"
cirro-agent read datasets -p "My Project" -f "tag:clinical size:>=10GB"
```

- Dates: `last-7d`, `last-3m`, `2026-Q2`, `2026-08`, `today`, or ISO. Always echoed
  back as an explicit UTC interval in `query.resolved`.
- People: `@me`, or a username.
- Status classes: `@active`, `@terminal`, `@bad`. Use these rather than listing enum
  members — `@bad` is what "what's broken" means.
- Sizes: `10GB` or `10GiB`.

For the full grammar and the field catalog, including which fields cost extra:

```bash
cirro-agent read describe dataset
```

This needs no login and no network. Read it before constructing anything unusual.

## Aggregate instead of listing

When the question is "how much" or "how many", ask for an aggregation and no rows
come back at all:

```bash
cirro-agent read datasets -p "My Project" \
  --group-by process_id --metrics count --order-by -count
cirro-agent read datasets --all-projects --confirm-fanout \
  --group-by created_at:quarter --metrics count
```

## Expensive fields

Size, parameters and failure messages need one API call per dataset, so they are not
returned by default. Opt in only once the match set is small:

```bash
cirro-agent read datasets -p "My Project" -f "status:FAILED created:last-7d" \
  --include total_size_bytes --include status_message
```

Over 25 matched rows this returns `COST_GATE` with the exact cost. That is a real
limit, not a suggestion: narrow the filter. **Do not estimate a total from a sample** —
dataset sizes are heavy-tailed and the plugin deliberately refuses to do it.

For whole-project storage there is a cheap path with no per-dataset cost:

```bash
cirro-agent read project "My Project" --include storage_bytes
```

It comes from the daily metrics record, so it lags by up to 48h and reports the sum
across storage tiers. Read `storage_amount` with `storage_unit` (the figures Cirro
gave); `storage_bytes` is derived from them.

## Money

Cirro reports what has **already** been spent, by month and by AWS service:

```bash
cirro-agent read spend -p "My Project"     # months, newest first, plus by_service
cirro-agent read spend --all-projects      # every project, in ONE api call
```

`--all-projects` here does not fan out — it is a single request, so it is the right
answer to "which project is costing us the most" and needs no `--confirm-fanout`.

Nothing prices a run *before* it happens. There is no such API, so never estimate what
an analysis will cost; say what will run instead, and offer this read for what similar
work has cost so far.

## The other things worth knowing

```bash
cirro-agent read projects                              # what can I see
cirro-agent read files -p P -d ds_123                  # manifest, collapsed to templates
cirro-agent read samples -p P                           # metadata columns, group keys, drift
cirro-agent read processes -f "executor:NEXTFLOW"       # available pipelines
cirro-agent read process-params "nf-core/rnaseq v3.14"  # accepted parameters
cirro-agent read references -p P                        # genomes and annotations
cirro-agent read params -p P -d ds_123                  # what a run actually used
```

`files` never dumps a manifest. It returns directory rollups, extensions by count and
bytes, and **name templates** — 480 FASTQs collapse to
`data/{sample}_R{1,2}_001.fastq.gz  x480`, and that template is the glob you need
next.

## Ambiguity

Names are not unique and are never fuzzy-matched. A near miss returns `NOT_FOUND` with
candidates; two exact matches return `AMBIGUOUS_NAME` with both IDs. In either case
the error carries runnable retries — use one, and prefer the ID afterwards.

## Reporting back

Summarize in prose. Quote counts from `counts` and `facets`, not from the rows. If the
response carried an `omitted` entry, say what was left out — especially
`SCAN_TRUNCATED`, which means every number was a lower bound.

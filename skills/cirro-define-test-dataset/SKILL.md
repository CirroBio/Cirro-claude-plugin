---
name: cirro-define-test-dataset
description: Choose a small test dataset for a workflow, pick the parameters to run it with, and write down how to tell whether the run succeeded. Use when the user wants to define test data, success criteria, or a test plan for a Cirro pipeline — the second step between speccing a workflow and configuring it.
---

# Define a test dataset

Second of three steps: **spec → test plan → configure**. Reads `design-spec.md`, writes
`test-plan.md`. It stages nothing in Cirro; uploading happens when the test actually
runs.

## Where the data comes from

In order of preference:

1. **Data the user gives you.** Ask what they have before going looking.
2. **The workflow's own test data.** Nextflow pipelines declare it in
   `conf/test.config`; WDL repositories keep input JSONs under `tests/`. This is data
   the authors know works, which is worth a lot.
3. **A minimal set you assemble** from a public source. For SRA/ENA accessions, Cirro
   can pull them directly at run time via `import_public` rather than you downloading
   and re-uploading.

Check every URL still resolves (`curl -fsI`). Test data rots, and a 404 discovered
mid-run is a wasted hour.

## Check the constraints before you commit to the data

The design spec's `constraints[]` exist for exactly this moment. Run each `verify`
snippet against the data you picked.

A violated constraint does not fail fast — it fails *deep*, after the pipeline has
spent real compute getting there. A multi-contig reference where the workflow needs one,
a BED file missing its fourth column, reads without the tags a subworkflow requires:
each costs a full run to discover. Fix them now, and say in the plan what you fixed.

## Make it small, but not so small it proves nothing

Subsample to the least data that still exercises the path being tested — usually a
single sample and a single small chromosome. `seqkit head -n 100000 in.fastq.gz` or
slicing a BAM to one region is normally enough.

The trap is coverage gates. Many pipelines skip a sample below a minimum depth, so an
aggressively subsampled input produces a run that completes and calls nothing. If the
spec has a parameter like `bam_min_coverage`, lower it for the test and note why.

## Choose every parameter deliberately

Give a value for every must-set parameter and say why you chose it. Honor `when`
predicates — a parameter that only applies in a mode you have not enabled should not be
set at all.

Look hard at the mode switches. Some pipelines default every analysis mode to *off*, so
a run at defaults does setup work and produces nothing. The design spec should have
flagged that as a constraint; if it did not, enable at least one and note it.

Never set a `cirro.managed_parameters` entry. Cirro supplies the output directory.

## Success criteria have to be checkable

Always include:

- the run reaching `COMPLETED`
- every `outputs[].required: true` file existing and being non-empty
- at least one **content** check with a real threshold — a row count, a minimum number
  of records, a value within a range

"The report looks right" is not a criterion. "`multiqc_report.html` exists and is over
10 KB" is. Match output paths by suffix or glob: Cirro nests outputs under a run-specific
prefix, and many workflows template the sample name into the filename.

## Write the plan

Write `test-plan.md` beside the design spec, in the working folder — not into the
workflow repository. Frontmatter carries `dataset` (source, provenance, files),
`run.parameters` (scalars, nested by the design spec's `group`), `run.file_inputs`
(secondary pickers, paths relative to the dataset root), `run.expected_runtime_minutes`,
and `success_criteria`.

Validate that the frontmatter parses before finishing, and confirm every file named in
`file_inputs` actually exists in `dataset.files`.

## Then hand off

Step 3 is **cirro-configure-pipeline**. Once the pipeline is registered, the existing
**cirro-run-analysis** skill launches it on this data and **cirro-monitor-analysis**
follows it and diagnoses failures — this plan's `success_criteria` is what they check
against.

Before finishing, surface for the user: where the data came from, what you subsampled
and by how much, every must-set value you chose, and the thresholds — those are the
numbers they will want to adjust.

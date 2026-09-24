---
name: cirro-spec-workflow
description: Read a Nextflow or WDL workflow and write a design spec describing its inputs, parameters, outputs and constraints. Use when the user wants to onboard, document, spec out, or analyze a workflow before configuring it as a Cirro pipeline — the first step of turning a repository into something runnable in Cirro.
---

# Spec a workflow

First of three steps: **spec → test plan → configure**. This one produces
`design-spec.md`, the contract the other two read.

## Let the parser do the reading

```bash
cirro-agent read workflow-spec epi2me-labs/wf-human-variation --ref v2.8.1
cirro-agent read workflow-spec /path/to/local/checkout --group input
```

It clones (or reads a checkout), extracts every declared parameter with its type,
default, group, enum and description, pulls samplesheet columns and machine-readable
outputs, and reports a `parse_confidence`. It needs no Cirro login.

It deliberately does **not** classify. It returns `candidates` — each with the reason
it was suggested — and that is where your judgment starts.

## What you decide

**The primary data input.** One input is the dataset itself. It is not a form field; it
maps to `$.inputs[0].dataPath`, the selected dataset's data folder. In the spec it is
the input with **no `group`** — that absence is what marks it.

Check what the parameter actually accepts before choosing. If the workflow takes a
*folder* of files, or several shapes (a single file, a folder, an instrument run
directory), it has to be the primary input: Cirro has no folder picker, so a form field
could only ever offer one file. If it genuinely takes one file the user should choose
from a dataset, a picker is right — but that decision narrows what the pipeline can
accept, so make it deliberately and say so.

**Secondary file inputs** become form pickers. Give each one a `group`. A genome or
annotation belongs to the shared reference library (`path_type: references`); anything
sample-specific comes from the input dataset.

**Managed parameters.** The output directory is set by Cirro at launch and must never be
a form field. Put it in `cirro.managed_parameters` with `role: output_directory`. This
overrides must-set: even if the schema marks it required, it is not the user's to fill.

**Must-set versus defaulted.** `required: true` *with* a default is pre-filled and safe.
`required: true` with **no** `default` means the caller has to supply a value — the
absence of the key is the signal, so do not invent a default to tidy it up.

**Hidden and infrastructure parameters** stay out. The parser flags engine knobs
(`aws_queue`, `monochrome_logs`, `validate_params`) for you.

**Conditional parameters.** If a parameter only applies when another has a particular
value, record it as `when: other_param == value`. The generator turns that into a
conditional field, so a 60-parameter pipeline does not flatten into one wall of inputs.

**Constraints** are requirements on the *shape* of an input that cause a runtime failure
if violated — a single-contig reference, paired reads, a fourth BED column. Read the
workflow's README and parameter help text for these; they are the difference between a
test that fails in ten seconds and one that fails after an hour of compute. Give each a
`verify` shell snippet the next step can actually run.

## Leave the engine version alone

`workflow.executor_version` stays `null`. Record what the manifest declares in the body
as reference, but do not pin it. Cirro's default engine is right for most pipelines, and
pinning pre-emptively causes its own failures. Pinning is a recovery step, used only
after a run actually fails at script compilation.

## Write the spec

Write `design-spec.md` into the working folder the user named — **not** into the
workflow's repository. It is YAML frontmatter plus prose:

```yaml
workflow:      name, repository, entrypoint, version, executor, executor_version
inputs[]:      name, description, type, required, group (omit group on the primary)
parameters[]:  name, type, group, required, default, choices, when, description
outputs[]:     path, description, required
constraints[]: applies_to, description, verify
cirro:         process_id, category, parent_process_ids, child_process_ids,
               execution_modes, managed_parameters[{name, role, note}]
```

`execution_modes` is optional and usually left out: omitting it registers the pipeline
the way Cirro registers everything by default, which is AWS Batch. Name
`[STANDARD, HEALTHOMICS]` only when the pipeline is meant to be runnable on AWS
HealthOmics — it is a capability of the *process*, decided once at registration, not a
per-run choice, and a pipeline that does not declare it cannot be sent there later.

Two things that will bite you when writing it:

- **Validate the frontmatter parses** before you finish:
  `python3 -c "import re,sys,yaml; yaml.safe_load(re.match(r'^---\n(.*?)\n---\n', open(sys.argv[1]).read(), re.S).group(1))" design-spec.md`
  Use the regex form, not `split('---')`, which breaks on a `# --- ---` comment line.
- **Shell snippets in `verify` need block scalars** (`verify: |-`), or a backslash in an
  `awk -F'\t'` will fail YAML double-quote parsing.

## Check yourself against what already exists

If a version of this pipeline is already configured in Cirro, read it — it is the
cheapest review you will get:

```bash
cirro-agent read processes -f "q:<workflow name>"
cirro-agent read process-config <process_id>
```

Compare your classification against the live form. A disagreement is worth explaining
either way: their config may be wrong, or it may know something you do not.

## Finish by telling the user what you decided

Report the counts (declared, visible, hidden, must-set), the primary input you chose and
why, anything the parser flagged, and any judgment call that could reasonably have gone
the other way. Then point at **cirro-define-test-dataset** for step 2.

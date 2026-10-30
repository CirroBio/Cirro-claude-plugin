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

**The input mode.** Write `cirro.input_mode: browse` in every new spec. The run form then
lets the person pick files and folders from anywhere in the project, a samplesheet can be
built in the form itself, and the run's input datasets are whichever datasets the picks
point into. The other mode, `select`, has the person choose input datasets first and pick
files from those alone; it is what a spec without the key means, which is how pipelines
configured before browse mode keep their form when their configuration is regenerated.
Do not switch an existing pipeline's spec to `browse` unless the person asks — it changes
how everyone runs it.

**The primary data input.** One input is the dataset's data itself. In the spec it is the
input with **no `group`** — that absence is what marks it. In `browse` mode it is a
required field the person picks, typed by its `type`: `folder` for a folder of files,
`file` for one file, `path` for either. In `select` mode it is not a form field at all
but the chosen dataset's data folder.

Check what the parameter actually accepts before choosing. A workflow that takes a
*folder* of files, or several shapes (a file, a folder, an instrument run directory),
wants `type: folder` or `type: path`.

**A samplesheet the workflow reads** becomes a structured input: the person fills in a
table in the run form, Cirro writes the file and passes its path, and no preprocess
script has to build it. Give the input a `structured` block. The parser's
`samplesheet_columns` (from `assets/schema_input.json`) are its columns, and its
`structured_input` candidates name the parameter that reads it. Copy each column's
`type`, `format` (a `file-path` column lets the person pick a file), `pattern`, `enum`
and `required`; `errorMessage` becomes `error_message`. A column the parser marks
`unsupported` holds a list, which a cell cannot — say so rather than dropping it.
Structured inputs need `input_mode: browse`.

**Secondary file inputs** become form pickers. Give each one a `group`. A genome or
annotation belongs to the shared reference library (`path_type: reference`); anything
sample-specific is picked from a dataset. In `browse` mode a picker narrows what it
offers with `pattern`, a regular expression matched against a file's path within its
dataset (`\.bed$`); the generator infers one from the input's name when you give none.
`multiple: true` lets the person pick several.

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
inputs[]:      name, description, type, required, group (omit group on the primary),
               path_type, pattern, multiple,
               structured{name, file_format, default_source_file,
                          columns[{name, type, format, pattern, enum, required,
                                   title, description, error_message}]}
parameters[]:  name, type, group, required, default, choices, when, description
outputs[]:     path, description, required
constraints[]: applies_to, description, verify
cirro:         process_id, category, parent_process_ids, child_process_ids,
               input_mode, execution_modes, managed_parameters[{name, role, note}]
```

A `group` is the group's **key** as `workflow-spec` reports it (`input_output_options`),
not its title ("Input/output options"). The key becomes a segment of the parameter's
path at launch, and the form shows the title on its own.

A structured input's `name` is the samplesheet's file name, 1-50 letters, digits, `_`
or `-`; it defaults to the input's own name. `file_format` is `CSV`, `TSV` or `JSON`.
`default_source_file` names a file — `samplesheet.csv` — that the run form fills the
table from when the person starts the run from a dataset holding one.

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

Report the counts (declared, visible, hidden, must-set), the input mode, the primary input
you chose and why, any samplesheet you made a structured input and its columns, anything
the parser flagged, and any judgment call that could reasonably have gone the other way. Then point at **cirro-define-test-dataset** for step 2.

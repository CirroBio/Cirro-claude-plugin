---
name: cirro-configure-pipeline
description: Configure a workflow as a runnable Cirro pipeline from a design spec — generate the form and parameter mapping, choose where the configuration lives, register the process, and deliver the config. Use when the user wants to register, configure, onboard or set up a workflow as a Cirro pipeline, or to update the configuration of one that already exists.
---

# Configure a Cirro pipeline

Third of three steps: **spec → test plan → configure**. Reads `design-spec.md` and
produces a registered, runnable pipeline.

## First: does it already exist?

```bash
cirro-agent read processes --include-archived -f "q:<workflow name>"
cirro-agent read process-config <process_id>
```

Configuring again usually means one of three different things, and they are not
interchangeable — settle it with the user before generating anything:

- **update an existing pipeline** — new config revision, same process id and history
- **register a new one** — a new id, leaving the old one working
- **move where the config lives** — same pipeline, different delivery pattern

The plugin will only modify pipelines **you own**. A process owned by someone else, or
curated by Cirro, is refused rather than edited.

## Choose where the configuration lives

This is the substantive decision. Cirro supports three, and `config_source` names the
choice:

| | `config_source` | Where the assets live | Suits |
|---|---|---|---|
| 1 | `STORAGE` | Uploaded into Cirro | Iterating fast; no repository to hold config |
| 2 | `WITH_PROCESS` | A folder in the **workflow's own** repo, `.cirro/` by default | You control the workflow repo; config and code version together |
| 3 | `EXTERNAL` | A folder in a **separate** config repo | Configuring workflows you do not control; central management |

Ask which the user wants. Useful signals: do they own the workflow repository? Is there
already a configuration repo their team uses? Do they want the config reviewed through
pull requests?

For 2 and 3 the branch is not always `main`, and the folder is not always `.cirro` —
real deployments use paths like `workflows/<name>/<version>`. Ask rather than assume.

## Generate and check

Generation is deterministic from the spec, so there is nothing to author by hand. What
you must do is **look at the result**:

```bash
cirro-agent read check-config <folder>
```

This enforces what the portal will not tell you kindly: only keywords the renderer
understands, pickers that actually offer something, every input mapping pointing at a
field the form really has, and no `../` anywhere — Cirro sits behind a WAF that rejects
any request body containing that literal, even inside a comment.

Read the generated form for sense, not just validity. Groups in a sane order, titles a
biologist would recognize, pickers pointing at the right library, conditional fields
where a large parameter set would otherwise be a wall.

## When generation is not enough

Some forms cannot be described by a spec: a field revealed by two independent
selectors, a custom widget, an input map tuned by hand. Do not contort the spec to
approximate one. Write the files and pass `assets_dir` instead of `design_spec`:

```
plan(op="upload_process_config",
     args={"process_id": "...", "assets_dir": "path/to/config"})
```

`assets_dir` is a folder holding `process-form.json` and any of `process-input.json`,
`process-output.json`, `process-compute.config`, `preprocess.py`, `README.md`. They go
up **exactly as written** — validated by the same checker, never reformatted, never
regenerated. Anything else in the folder is ignored. Nothing is staged, because the
files are already where the user put them.

Exactly one of `design_spec` and `assets_dir` on an upload. `create_process` still
takes the spec for the process record's own fields — name, version, repository — and
accepts `assets_dir` alongside it so its pre-flight validates the files that will
actually be uploaded rather than ones nobody will send.

Run `cirro-agent read check-config <dir>` on a hand-written folder before planning.
Its errors are defects Cirro would reject or mis-render, and the upload refuses on
them.

## Register, then deliver

Registering the process and delivering the configuration are **separate steps**. The
process record is always created through the API; only the assets differ by pattern.

In every pattern the generated files end up somewhere the user can read later. Say
where, in the message where you report the result — the point of a configuration is
that someone can go back and look at it.

**Pattern 1 — Cirro storage**

```
plan(op="create_process", args={"design_spec": "...", "config_source": "STORAGE"})
plan(op="upload_process_config", args={"process_id": "...", "design_spec": "..."})
```

All slots go up in one revision, so the configuration is never half-applied. Updating
later requires the current revision number as the update token; the plan reads it for
you, and a stale one is refused rather than silently overwriting.

The upload also drops a copy of exactly what it sent into `pipeline-config/` beside the
design spec, written only once Cirro has accepted the revision, so the folder is never
a record of something that failed. Without it this pattern leaves nothing on disk and
the only way to see a pipeline's configuration is to ask Cirro for it back. Pass
`stage_dir` to put it elsewhere. It is an ordinary folder, not the hidden `.cirro/`
session directory, so it can be committed alongside the spec if the user wants that.

**Patterns 2 and 3 — a git repository**

```
plan(op="create_process", args={"design_spec": "...", "config_source": "WITH_PROCESS"})
plan(op="write_process_config", args={"design_spec": "...", "destination": "<repo>/.cirro"})
plan(op="commit_process_config", args={"destination": "<repo>/.cirro"})
```

Then **the user pushes**. This plugin does not push and does not open pull requests —
it writes files and, when asked, commits them locally. Say so plainly rather than
leaving them to discover it.

Once the change is on the branch Cirro reads:

```
plan(op="sync_process_config", args={"process_id": "..."})
```

Sync reports whether it succeeded and which commit it landed on. Check that: roughly
one in seven repo-backed configurations on a live tenant is sitting on a failed sync or
an unauthorized repository, and a failed sync means Cirro is quietly running older
configuration than the repo contains. A private repository needs the Cirro GitHub app
authorized — a user action, not something the plugin can do.

## One approval for the whole thing

Registering, uploading test data and launching the first run is one intention, so ask
for it once. `plan(op="chain", ...)` approves the sequence as one action:

```
plan(op="chain", args={"steps": [
  {"label": "register",  "op": "create_process",
   "args": {"design_spec": "design-spec.md", "config_source": "STORAGE"}},
  {"label": "configure", "op": "upload_process_config",
   "args": {"process_id": {"from_step": 0, "field": "created_process_id"},
            "design_spec": "design-spec.md"}},
  {"label": "upload",    "op": "upload_dataset",
   "args": {"project_id": "proj_...", "process": "proc_ingest_...",
            "name": "wf test data", "source_dir": "test-data/", "folder": "Test"}},
  {"label": "run",       "op": "run_analysis",
   "args": {"project_id": "proj_...",
            "process": {"from_step": 0, "field": "created_process_id"},
            "name": "wf test run", "params": {...},
            "source_dataset_ids": [{"from_step": 2, "field": "dataset_id"}]}},
 ],
 "retry": {"redo": ["configure", "run"], "max_attempts": 3,
           "may_change": ["design_spec"]}})
```

A value a later step cannot know yet — the process id step 1 creates, the dataset id
step 3 creates — is written as `{"from_step": N, "field": "..."}`. The server fills it
in from what that step actually returned. It is not a wildcard, and the approval shows
it as exactly what it is: `<dataset_id of step 3: upload>`.

Waiting for the run is free: `analysis-status`, `tasks` and `logs` are ungated reads.
Downloading results afterwards is an ordinary plan, because by then the dataset exists.

**To see where a chain stands** — which attempt this is, what each produced, where it
stopped, and whether a resume is open:

```bash
cirro-agent read chain-status <plan_id>
```

**When the run fails**, do not re-plan the whole chain. Diagnose from `tasks` and
`logs`, fix the design spec, validate it with `check-config`, then resume:

```
plan(op="chain", args={
  "restart_from": {"plan_id": "<the chain's plan_id>", "label": "configure"},
  "steps": [<the same steps, with the changed design_spec>]})
```

That reuses the uploaded test data and shows the user only what changed since the
attempt they approved. The `retry` policy bounds it: only `may_change` fields may
differ, `max_attempts` is a ceiling, and an attempt identical to the last one is
flagged as pointless before anyone spends compute on it. Each attempt still asks —
that click is the one place a human should be, in front of a decision that costs money.

A chain refuses `archive_process` and every other destructive operation. Tearing
something down is its own approval, always.

## Preprocess scripts

Only if the workflow needs its inputs transformed before launch — and always for
WDL/Cromwell, which needs an `inputs.json` built.

If you write one, it **must** ship with an offline unit test beside it. Extract a pure
function that takes files and parameters and returns the manifest, keep the `cirro`
import inside `__main__`, and test the pure function with in-memory fixtures.
`check-config` fails a folder containing `preprocess.py` with no `test_preprocess.py`,
because a preprocess bug only surfaces after a run has been submitted.

## Verify before calling it done

```bash
cirro-agent read process-params <process_id>
cirro-agent read process-config <process_id>
```

The parameter spec is what Cirro will actually accept — compare it against the design
spec. Then ask the user to open the pipeline in the Cirro UI and check that the form
renders, conditionals toggle and pickers list real files. Nothing here proves the form
looks right to a human.

Then hand to **cirro-define-test-dataset** if there is no test plan yet, or straight to
**cirro-run-analysis** to launch it on one.

## Things that will trip you

- **Leave `executor_version` unset.** Pinning is a recovery step after a run fails at
  script compilation, not a precaution.
- **Process names are restricted** to letters, digits, spaces and `. & - @ ! # ' ( ) /`.
  Anything else is rejected with an HTTP 400.
- **Two regex dialects.** Sample-matching patterns sent through the API need
  `(?<name>...)`; the same patterns written into JSON assets use `(?P<name>...)`.
- **No `process-definition.json`.** Older configuration repos contain one; the API path
  does not use it, and the process record is created through `create_process` in every
  pattern.
- **Config management needs a manage-capable identity.** An OAuth application typically
  lacks `manage_custom_pipelines`; a signed-in user has it.

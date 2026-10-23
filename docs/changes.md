# Making changes

Every change goes through the same two steps. There is no way around them and no way to
turn them off. What you can relax, for a few kinds of change and only when you ask, is
how often step 2 asks you — see [Approving a kind of change](grants.md).

**1. Claude works out exactly what would happen.** This step touches nothing. It resolves
names to IDs, checks parameters against what the pipeline actually accepts, counts the
files and bytes involved, and looks for consequences you might not have considered. An
instruction the operation does not understand stops it here rather than being quietly
ignored, so what you read is what would run.

**2. You see it, then approve it.** Something like:

```
Cirro app.cirro.bio as you@example.org
Operation: delete_dataset  (risk: cirro_destructive)

DELETE dataset RNAseq_batch31 (ds_9e41c0b7) from Immuno-Oncology Core.

  Status       COMPLETED
  Created      2026-08-24 by you@example.org
  Contents     18 files, 39.3 MB
  Produced by  nf-core/rnaseq v3.14

This is not reversible from here.

Warnings:
  - 2 datasets in this project were produced from this one; their history
    will point at a dataset that no longer exists.
```

Only if you approve does anything happen. What runs is exactly what you read — the
approved plan is the only input to the change, so nothing can be substituted after you
say yes. A plan is good for one change, and if the underlying facts move between
planning and approving, it is refused rather than applied against figures you never
saw.

## Reading the approval prompt

Claude shows you that text in the conversation. The approval prompt itself is drawn by
Claude Code, and what it can show is the arguments of the call it is asking about — so
the plan arrives there as one long argument with its line breaks escaped, which is
readable in the terminal and cramped in the desktop app. The first argument, `change`,
is a one-line version for that reason: the operation, what it acts on, `DESTRUCTIVE`
where that applies, and a count of any warnings. It is filled in from the stored plan,
not written by Claude, and it authorizes nothing — the full text above it is still what
binds the approval. Read the plan in the conversation; treat `change` as the label on
the prompt, and if the two ever disagree, refuse.

## Multi-step work gets one prompt

Configuring a pipeline, uploading test data and launching a run can be approved together
as a chain. Values a later step cannot know yet — the ID of a dataset the upload is
about to create — appear as exactly that, not as a blank cheque. The approval names
every step's operation in order, so nothing can hide inside a long argument list.
Deletions are never allowed inside a chain.

`cirro-agent read chain-status` shows where a chained workflow stands — each attempt,
what it produced, where it stopped — without opening anything in `.cirro/`.

If a run fails and you approved a retry policy up front, the retry reuses what already
succeeded and shows you only what changed:

```
Attempt 2 of 3 on the chain approved as plan_1788384902_446399a9.

  Re-running: configure, run
  Reusing:    1 earlier step(s), unchanged

  Changed since the last attempt:
       configure.design_spec
         from  '/work/spec.md'
         to    '/work/spec-v2.md'
```

Every change is written to a log in `.cirro/runs/` — what was done, when, and as whom.

## Worth knowing

- **Nothing prices a run before it happens**, so a plan tells you what will run and on
  what rather than inventing an estimate. What has already been spent is readable —
  see [Looking at things](reading.md#what-has-been-spent).
- **Folders are safe, and they come along for the ride.** Cirro stores folder
  membership alongside tags, so a careless tag edit could quietly move a dataset out of
  its folder. Every plan resolves folder membership explicitly and tells you it is
  preserving it. You can also name the folder when you upload data or launch an
  analysis, so filing happens as part of that change rather than as a second one to
  approve.
- **A launch emails you when it finishes**, as it does when you start one in the web
  UI. `run_analysis` and `resume_analysis` default the notification address to the
  account the session is signed in as, and the plan names that address before you
  approve it. Pass `notification_emails` to notify someone else, or an empty list to
  notify nobody. If your address cannot be looked up the plan says so and notifies
  nobody, rather than holding up the launch.
- **A run can target AWS HealthOmics** instead of Cirro's default AWS Batch, if the
  pipeline supports it. Ask for it and the plan's `Compute` line names the target
  before you approve; where the pipeline could have gone either way, that line says so
  even when the run is heading for Batch. A pipeline that does not declare HealthOmics
  is refused while the plan is still read-only, rather than after you have said yes.
- **Which of those a pipeline may use is decided when you register it.** Pass
  `execution_modes` to `create_process` or `update_process` to declare `STANDARD` (AWS
  Batch), `HEALTHOMICS`, or both, and the plan's `Runs on` line states it before you
  approve. Leave it out and Cirro's own default stands. Editing a pipeline for any
  other reason keeps the declaration it already had — the API replaces the whole
  record, so a quiet omission there would cost a pipeline its HealthOmics capability
  as a side effect of renaming it.
- **Some things are deliberately not offered** — renaming or re-billing a project,
  editing a live share's conditions, billing changes. Those belong in the Cirro web UI
  where their effects are visible.

## What Claude can change

| Area | Operations |
|---|---|
| Analyses | `run_analysis`, `resume_analysis`, `stop_analysis` |
| Adding data | `upload_dataset`, `upload_reference`, `import_public` |
| Datasets | `update_dataset` (one or many at once; changing a dataset's data type also re-runs its data transforms and re-ingests its samples), `update_samplesheet` (also re-ingests the project's sample metadata), `delete_dataset` |
| Projects | `create_project`, `update_project_tags`, `archive_project`, `unarchive_project` |
| People | `set_user_role`, `invite_user` |
| Metadata | `update_sample`, `update_project_schema` |
| Sharing | `create_share`, `delete_share`, `subscribe_share`, `unsubscribe_share` |
| Workspaces | `create_workspace`, `start_workspace`, `stop_workspace`, `delete_workspace` |
| Pipelines | `create_process`, `update_process`, `upload_process_config`, `sync_process_config`, `archive_process` |
| Dashboards | `create_dashboard`, `edit_dashboard`, `delete_dashboard` |
| Sheets | `create_sheet` (a table, optionally with rows or from a local file, or a view over SQL), `update_sheet` (rename it, add, drop, rename or retype columns, change a view's SQL), `delete_sheet`, `insert_rows`, `update_rows`, `delete_rows`, `refresh_sheet` (a materialized view) |
| Your computer | `write_process_config`, `commit_process_config`, `download_files` |
| Several at once | `chain` — approve a sequence of the above as one action |

Downloading is gated too. It does not change anything in Cirro, but it can fill a disk,
so the plan tells you how many bytes are coming and how much space you have. Transfers
also have a time ceiling: if the bytes could not move within it, the plan says so before
you approve rather than after the transfer is cut off.

Deleting a reference is not on this list. The API route the plugin has available
identifies a project but not which reference to remove, so there is no way to delete one
safely from here — use the web UI.

Not every operation in this table has run against a real Cirro tenant yet.
[Status](status.md) says which have.

# Making changes

Every change goes through the same two steps. There is no way around them and no way to
turn them off. What you can relax, for a few kinds of change and only when you ask, is
how often step 2 asks you; see [Approving a kind of change](grants.md).

**1. Claude works out exactly what would happen.** This step changes nothing. It
resolves names to IDs, checks parameters against what the pipeline accepts, counts the
files and bytes involved, and looks for consequences you might not have considered. An
argument the operation does not accept stops it here rather than being quietly ignored.
A pipeline parameter the pipeline does not declare, or a required one that is missing,
is flagged as a warning in the plan.

**2. You see it, then approve it.** Claude shows you the plan in the conversation. A
deletion, for example, looks like this:

```
Cirro app.cirro.bio as you@example.org
Operation: delete_dataset  (risk: cirro_destructive)

DELETE dataset RNAseq_batch31 (ds_9e41c0b7) from Immuno-Oncology Core (proj_8a2c).

  Status       COMPLETED
  Created      2026-08-24 by you@example.org
  Contents     18 files, 39.3 MB
  Produced by  nf-core/rnaseq v3.14

This is not reversible from here.

Warnings:
  - 2 dataset(s) in this project list ds_9e41c0b7 as a source: DE_batch31 (ds_41aa02),
    QC_batch31 (ds_77c1e9). Their lineage will point at a dataset that no longer exists.
```

Only if you approve does anything happen, and what runs is exactly what you read: the
approved plan is the only input to the change. If you want something different, say no
and tell Claude what to change; it makes a new plan.

A plan is good for one change and for one hour. When you approve it, it is worked out
again, and if anything in Cirro has moved since you read it, it is refused rather than
applied against figures you never saw.

## Reading the approval prompt

The approval prompt is drawn by Claude Code, and it shows the arguments of the call it
is asking about. Its first line, `change`, is a one-line summary filled in from the
stored plan: the operation, what it acts on, `DESTRUCTIVE` where that applies, and a
count of any warnings. The full plan, shown in the conversation, is what you are
approving. If the two ever disagree, decline.

## Several steps, one approval

Configuring a pipeline, uploading test data and launching a run can be approved
together as a *chain*. A value a later step cannot know yet, such as the ID of the
dataset the upload is about to create, appears as exactly that rather than as a blank to
be filled in later. The approval names every step's operation in order, so nothing can
hide inside a long argument list. Deletions are never allowed inside a chain.

When you approve a chain you can also approve a retry policy: how many attempts, and
which fields may change between them. If a step fails, the retry is a new plan, which
you approve again. It reuses what already succeeded and shows exactly what changed:

```
Attempt 2 of 3 on the chain approved as plan_1788384902_446399a9.

  Re-running:
       2. upload_process_config  [configure]
       3. run_analysis  [run]
  Reusing from the approved run:
       1. upload_dataset  [upload]
           dataset_id = 'ds_7f3a91c2'
           uploaded_to_project = 'proj_8a2c'

  Changed since the last attempt:
       configure.design_spec
         from  '/work/spec.md'
         to    '/work/spec-v2.md'

Everything the approval pinned is unchanged. Launching consumes compute again.
```

A change to a field the policy does not allow is refused. Ask Claude where a chain
stands and it shows each attempt, what it produced and where it stopped.

Every change is recorded in your login folder, in `.cirro/runs/`: what was done, when,
and as whom. Ask Claude which tenants you are signed in to and it names the folder.

## Worth knowing

- **Nothing prices a run before it happens**, so a plan tells you what will run and on
  what rather than inventing an estimate. What has already been spent is readable; see
  [Looking at things](reading.md#what-has-been-spent).
- **Folders are safe, and they come along for the ride.** Cirro stores folder
  membership alongside tags, so a careless tag edit could quietly move a dataset out of
  its folder. Every plan resolves folder membership explicitly and tells you it is
  keeping it. You can also name a folder when you upload data or launch an analysis, so
  filing happens as part of that change rather than as a second one to approve.
- **A launch emails you when it finishes**, as it does when you start one in the Cirro
  portal. The plan names the address before you approve. Ask Claude to notify someone
  else, or nobody. If your Cirro profile has analysis notifications turned off, or your
  address cannot be looked up, nobody is emailed and the plan says so.
- **A run can target AWS HealthOmics** instead of Cirro's default AWS Batch, if the
  pipeline supports it. Ask for it and the plan's `Compute` line names the target before
  you approve. A pipeline that does not declare HealthOmics is refused while the plan is
  still read-only, rather than after you have said yes.
- **Which of those a pipeline may use is decided when you register it.** Registering a
  pipeline can declare AWS Batch, HealthOmics or both; the plan's `Runs on` line states
  it. Left unsaid, it comes from the pipeline's design spec, and then from Cirro's own
  default. Editing a pipeline for any other reason keeps the declaration it already had.
- **Changing a dataset's data type re-runs its data transforms and re-ingests its
  samples** by default, as replacing a samplesheet re-ingests the project's sample
  metadata. Ask Claude to skip either.
- **Some things are deliberately not offered**: renaming or re-billing a project,
  editing a live share's conditions, billing changes. Those belong in the Cirro portal,
  where their effects are visible.

## What Claude can change

| Area | Operations |
|---|---|
| Analyses | `run_analysis`, `resume_analysis`, `stop_analysis` |
| Adding data | `upload_dataset`, `upload_reference`, `import_public` |
| Datasets | `update_dataset` (one or many at once: name, description, tags, folders, data type), `update_samplesheet`, `delete_dataset` |
| Projects | `create_project`, `update_project_tags`, `archive_project`, `unarchive_project` |
| People | `set_user_role`, `invite_user` |
| Metadata | `update_sample`, `update_project_schema` |
| Sharing | `create_share`, `delete_share`, `subscribe_share`, `unsubscribe_share` |
| Workspaces | `create_workspace`, `start_workspace`, `stop_workspace`, `delete_workspace` |
| Pipelines | `create_process`, `update_process`, `upload_process_config`, `sync_process_config`, `archive_process` |
| Dashboards | `create_dashboard`, `edit_dashboard`, `delete_dashboard` |
| Sheets | `create_sheet` (a table, optionally with rows or from a local file, or a view over SQL), `update_sheet` (rename it, add, drop, rename or retype columns, change a view's SQL), `delete_sheet`, `insert_rows`, `update_rows`, `delete_rows`, `refresh_sheet` (a materialized view) |
| Your computer | `write_process_config`, `commit_process_config`, `download_files` |
| Several at once | `chain`: approve a sequence of the above as one action |

Downloading is gated too. It does not change anything in Cirro, but it can fill a disk,
so the plan tells you how many bytes are coming and how much space you have. Transfers
also have a time limit: if the bytes could not move within it, the plan says so before
you approve rather than after the transfer is cut off.

Deleting a reference is not supported yet; use the Cirro portal.

Not every operation in this table has run against a real Cirro tenant yet.
[Status](status.md) says which have.

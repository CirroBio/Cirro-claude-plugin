# Status

Every read has been checked against a real Cirro tenant. The changes have been tested
against a simulated tenant, and the ones below have also run against a real one.

!!! warning "Try new things on something disposable"
    Four bugs found on 2026-08-27 were exactly the kind that surface only on a real call.
    Treat your first use of any change not marked as run live as untested, and try it on
    a project, dataset or sheet you would not mind losing.

| Area | Run against a real tenant? | When |
|---|---|---|
| Reading | Yes, every read | |
| Uploading a dataset; renaming, retagging and describing one | Yes | 2026-09-06 |
| Changing a dataset's data type | Yes | 2026-09-18 |
| Launching analyses, alone and in chains | Yes, launches only | 2026-09-09 |
| Dashboards: create, edit, delete | Yes | 2026-09-29 |
| Sheets: create, insert, update, change columns, delete | Yes | 2026-09-30 |
| Refreshing a materialized view | Tried, failed inside the tenant | 2026-09-30 |
| Signing in for a tenant | Yes | 2026-09-09 |
| Removing a tenant login | No | |
| Registering a pipeline | No | |
| Targeting AWS HealthOmics | No | |
| Approving a kind of change (grants) | No | |
| Everything else in [What Claude can change](changes.md#what-claude-can-change) | No | |

The sections below give the detail.

## Run against a live tenant

**The change path** — on 2026-09-06, against a development tenant, in a project and
folder set aside for it. What ran was `upload_dataset` (a new two-file dataset into its
own folder) and four `update_dataset` writes on that dataset alone: a rename, a tag
write, and a description edit, each restored afterwards. Nothing that already existed
was touched, and nothing was deleted. Alongside the writes, four refusals were confirmed
to fire: a forged digest, an altered approval text, a re-used plan, and a plan whose
facts had moved underneath it. Folder membership survived every tag write, and fields
left out of a request — a description the caller never mentioned — survived rather than
being blanked.

**Retyping a dataset** — on 2026-09-18, against a development tenant, on a two-file
dataset uploaded for the purpose and left in place. `update_dataset` moved it from
`Paired DNAseq (FASTQ)` to `Paired 16S (FASTQ)` and back, with the data-transform re-run
and the sample ingest accepted each way; the name, description, tags and folders it was
not asked about survived both writes. The refusal of a pipeline id as the target type
was confirmed against the real process catalogue. Whether the queued transforms and
ingests then completed is not recorded here. Two bugs surfaced that only a live run
finds: the `get` verb's envelope had moved under the live smoke test, and
`update_project_tags` refused `[]`, so the tag that test adds could not be taken off
again.

**`run_analysis` and `chain`** — on 2026-09-09, against a development tenant. Several
chains of up to eight `run_analysis` steps were approved and applied, and the launches
were accepted. Whether the resulting analyses ran to completion is not recorded here,
so treat the launch path as exercised and the rest as untested.

**Dashboards** — on 2026-09-29, against a development tenant, in a project set aside for
it. `create_dashboard` made two throwaway dashboards — one over a dataset file
(scatter, histogram, box plot, table, a number control feeding the SQL and a
multi-select filter), one over a sheet — and both rendered in the portal as planned.
`edit_dashboard` restyled a chart, changed a control, added a tile and moved tiles; an
edit planned before a save in the portal was refused as stale, and the portal's save
survived. `delete_dashboard` removed both. The reads, including `dashboard --run`, were
checked against all 40 dashboards on that tenant. The portal half — an open dashboard
reloading to show a plugin's edit, and refusing to save over it with unsaved edits —
was exercised with an unreleased portal build; until that build is deployed, an open
tab can still overwrite an edit.

**Sheets** — on 2026-09-30, against a development tenant, in a project set aside for it.
`create_sheet` made a table with rows across eight column types (an enum, a URL, dates
and timestamps among them) and two views over it; `insert_rows` added rows inline and
from a local CSV; `update_rows` changed rows by filter and by `_row_id`, and a plan made
before one of its rows was edited was refused as stale; `update_sheet` added, dropped
and renamed columns and changed a view's SQL; `delete_rows` and `delete_sheet` removed
them all. A delete plan for a sheet three dashboards read listed all three. That run
also found that the backend's dry run accepts narrowing a column's type which the table
then refuses, so the plan now refuses it first.

`refresh_sheet` is the exception: materializing a view failed inside that tenant's
backend (`LocalFileSystem has been disabled by configuration`), and the backend will
not refresh a view in that state, so no refresh has succeeded live.

**Signing in for a tenant** — on 2026-09-09 a device-code login landed in a tenant folder
against a development tenant, and plans were created and changes applied from it.
`session_remove` is the half that remains untested: it has only ever been called
directly, never through the plugin's server, so deleting a tenant folder is still a
path nobody has walked. Signing in for a single directory is the older path and is
unaffected. If a tenant login misbehaves, `cirro-agent read tenants` will show you what
exists without needing a credential, and deleting the folder it names is always a safe
reset.

## Not yet run against a live tenant

**Registering a pipeline.** Pipeline configuration is built, and its generated output is
checked by a validator calibrated on the configuration of 213 pipelines already running
in Cirro, but no registration has yet been done against a live tenant.

**Targeting AWS HealthOmics** is new in 0.10.0 and has not been through a real run. The
argument is validated, the approval text names the target, and tests check that the
field is on the request the plugin sends, but no launch has yet reached HealthOmics
through the plugin. The Batch default is the older path and is unchanged. After a run lands,
`cirro-agent read dataset <id> -p <project> --include info` is the reliable check:
`info.omicsRunUuid` and `info.jobId` are present exactly when the run reached
HealthOmics, and dataset status is not a substitute.

**Declaring which modes a pipeline may use** is new in 0.12.0 and has not been sent to a
live tenant. The tests check that `executionModes` is on the request and that editing a
pipeline for any other reason carries it forward, but no registration carrying it has
been accepted by a real server. Registering a pipeline at all is untested live, as
above, so this inherits that caveat rather than adding a new one.

**Session grants** are new in 0.14.0 and have not run against a live tenant. `grant`,
`apply_granted`, `grants` and `apply`'s `stop_asking` are tested against the simulated
tenant only, and the `also_approves` line has not yet been seen in a real approval
prompt. A grant lasts as long as the plugin's server for the session, and that lifetime
has not been checked against every way a session can be cleared or resumed.

**Every other operation** in [What Claude can change](changes.md#what-claude-can-change)
has only run against a simulated tenant.

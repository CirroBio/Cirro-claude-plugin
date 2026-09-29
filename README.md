# Cirro plugin for Claude Code

Work with your data on the [Cirro](https://cirro.bio) platform by asking Claude, using
your own Cirro identity.

**Looking at things is free. Changing things asks you first — every time.**

You can let Claude explore your projects, datasets, runs and logs as freely as you like,
because nothing on that path can alter anything. The moment something would create,
modify or delete, it stops and shows you exactly what it is about to do.

## Getting started

```bash
claude plugin marketplace add CirroBio/Cirro-claude-plugin
claude plugin install cirro@cirro
```

Then, once, install the runtime:

```
/cirro:setup
```

This builds an isolated Python environment containing the Cirro SDK. It needs
[`uv`](https://docs.astral.sh/uv/) and takes up to two minutes the first time. Nothing
runs it behind your back — a silent two-minute install inside something that looks
instant is a bad surprise.

Until it has run, the Cirro tools load but decline every call, saying the runtime is
missing and naming this command. That is what a fresh install looks like, not a broken
one.

Now go to whatever directory you want to work in and ask Claude to connect to Cirro.
It will ask for your Cirro hostname (something like `app.cirro.bio`), then hand you a
link to sign in with. After that, just ask for what you want:

> *"What failed in the Immuno-Oncology project this week?"*
> *"Why did that run fail?"*
> *"Move those three datasets into the QC folder."*

`/cirro:doctor` tells you where things stand if anything seems off.

### Signing in

Ask Claude to connect and it prints a sign-in link straight to your terminal as soon
as it has one, as well as showing it in the conversation. Open it yourself and approve.
Nothing launches a browser on your behalf.

If the directory is already signed in, none of that happens — an expired token is
refreshed silently and Claude just tells you who you are.

### Where your credentials live

A `.cirro/` folder holds a login, a cache, and a log of every change made. It ignores
itself in git, and it is separate from the `~/.cirro` used by the standalone `cirro`
CLI — the plugin never touches that.

There are two places it can go, and the difference is worth a minute.

**Signed in for the tenant.** Ask Claude to sign in and this is what you want. The
folder is created outside every checkout, one per Cirro hostname, and any directory
with nothing bound nearer resolves to it — so you sign in once and every project on
the machine can read Cirro. It survives plugin installs and updates, which a folder
inside a checkout does not.

**Signed in for one directory.** A `.cirro/` created inside a working directory answers
for that directory and everything under it, ahead of any tenant login. Use it when a
particular project genuinely needs a different tenant or a different account from your
usual one.

One folder, one Cirro tenant, either way. If two tenants are signed in and the
directory you are working in has nothing bound nearer, Claude will not pick between
them — it names both and asks. You can always say which to use, without moving:
`cirro-agent read ... --dir <path>` does the same on the command line.

#### Seeing and removing what is signed in

```bash
cirro-agent read tenants
```

Lists every tenant you are signed in to, where its folder is, whether it still holds a
credential, when it was last used, and how many plans and change records it has kept —
and says which one an unbound directory will resolve to, or that there are too many to
choose from. It reads the filesystem and nothing else, so it still answers when a login
has expired.

To get rid of one, ask Claude to remove it. That deletes the folder outright — the
credential, the cache, and the record of every change made from it — so it asks first
and tells you what was in it. Removing the credential but keeping the folder and its
history is what signing out does instead. Folders bound to a single directory are not
listed and are not removable this way; delete those yourself.

#### If you sign in for a directory

Two things to avoid, both of which have bitten people.

**Not a parent of your work.** The search walks up from wherever Claude is working,
stopping at the project directory or the enclosing git checkout — and at your home
directory when it is neither. A `.cirro/` high up therefore captures everything
beneath it, so signing in at `~` or `~/projects` quietly binds every project under it
to that one tenant. This is the usual cause of a session talking to the wrong Cirro,
and signing in for the tenant instead avoids it entirely.

**Not inside anything that gets copied or replaced.** Installing or updating a plugin
copies its whole directory, and a signed-in `.cirro/` goes with it — tenant, username
and, where your OS keychain is unavailable, the token itself in plain text. The same
applies to a git worktree you intend to delete.

**Sharing one with another session is fine.** Two Claude sessions can work against
the same signed-in directory at once. If both find an expired token in the same
moment, one renews it and the other waits and picks up the result, rather than both
asking Cirro at once — which used to risk being throttled, and a throttled refresh
discards the stored login. A token that expires in the middle of a long upload is
still renewed the old way, so a transfer running for hours alongside another session
remains the one case worth staggering. That renewal rewrites the token in your OS
keychain, so an operation long enough to outlive its token — the longest chains, and
uploads — fails if the keychain has re-locked meanwhile. That reports as
`CREDENTIAL_STORE_UNAVAILABLE` naming the OS status code, and it is not a Cirro
outage: unlock the login keychain and run it again.

## Looking at things

Ask in plain language and Claude handles the rest. Underneath it uses a read-only
command you can also run yourself:

```bash
cirro-agent read --help
cirro-agent read datasets -p "My Project" -f "status:FAILED created:last-7d owner:@me"
cirro-agent read folders -p "My Project"
cirro-agent read tasks -p "My Project" -d ds_123
```

| Area | What you can read |
|---|---|
| Structure | `projects`, `project`, `folders`, `describe`, `whoami`, `tenants`, `doctor` |
| Datasets | `datasets`, `dataset`, `files`, `params`, `analysis-status` |
| Runs | `logs`, `tasks`, `task-logs`, `trace`, `chain-status` |
| Pipelines | `processes`, `process`, `process-params`, `process-config`, `workflow-spec`, `check-config` |
| Project resources | `samples`, `references`, `reference-types`, `compute-envs`, `shares`, `workspaces`, `project-users`, `users` |
| Money | `spend`, `cost` |
| Dashboards | `dashboards`, `dashboard` (add `--run` to execute its data), `dashboard-schema`, `sheets`, `preview` |

**Big answers stay useful.** Ask about a project with 4,000 datasets and you will not get
4,000 rows, and you will not get an arbitrary 40 either. You get the shape of all of them
— how many failed, who made them, which pipelines produced them, when — plus a handful
of labeled examples and some suggested follow-up questions that already know how many
results they will return.

The same applies to anything large: a 500-file dataset comes back as file patterns rather
than 500 filenames, and a huge workflow log comes back as its errors plus a count of the
repetitive lines.

**Some details cost extra**, like dataset sizes, which need a separate lookup per
dataset. Claude will tell you when it is skipping those and how to ask for them.

## Making changes

Every change goes through the same two steps. There is no way around it and no way to
turn it off.

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

Claude shows you that text in the conversation. The approval prompt itself is drawn by
Claude Code, and what it can show is the arguments of the call it is asking about — so
the plan arrives there as one long argument with its line breaks escaped, which is
readable in the terminal and cramped in the desktop app. The first argument, `change`,
is a one-line version for that reason: the operation, what it acts on, `DESTRUCTIVE`
where that applies, and a count of any warnings. It is filled in from the stored plan,
not written by Claude, and it authorizes nothing — the full text above it is still what
binds the approval. Read the plan in the conversation; treat `change` as the label on
the prompt, and if the two ever disagree, refuse.

**Multi-step work gets one prompt.** Configuring a pipeline, uploading test data and
launching a run can be approved together as a chain. Values a later step cannot know
yet — the ID of a dataset the upload is about to create — appear as exactly that, not
as a blank cheque. The approval names every step's operation in order, so nothing can
hide inside a long argument list. Deletions are never allowed inside a chain.

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

A few things worth knowing:

- **Nothing prices a run before it happens**, so a plan tells you what will run and on
  what rather than inventing an estimate. What has *already* been spent is real and
  readable at two scales: `cirro-agent read spend -p "My Project"` breaks a project
  down by month and AWS service, and `--all-projects` compares every project you can
  see in a single call; `cirro-agent read cost -p "My Project" -d ds_123` gives one
  analysis's compute bill, split by task status and rolled up by pipeline step, so you
  can see which step took the money and how much of it went on tasks that failed.
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

### What Claude can change

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
| Your computer | `write_process_config`, `commit_process_config` |
| Several at once | `chain` — approve a sequence of the above as one action |
| Your computer | `download_files` |

Downloading is gated too. It does not change anything in Cirro, but it can fill a disk,
so the plan tells you how many bytes are coming and how much space you have. Transfers
also have a time ceiling: if the bytes could not move within it, the plan says so before
you approve rather than after the transfer is cut off.

Deleting a reference is not on this list. The API route the plugin has available
identifies a project but not which reference to remove, so there is no way to delete one
safely from here — use the web UI.

## What Claude brings to each task

| Skill | For |
|---|---|
| `cirro-session` | Connecting a directory to Cirro, signing in, fixing a broken session |
| `cirro-explore` | Finding data, summarizing a project, answering "what do we have?" |
| `cirro-run-analysis` | Launching, resuming and stopping pipeline runs |
| `cirro-monitor-analysis` | Whether a run finished, and why one failed |
| `cirro-upload-dataset` | Uploading your files and reference data |
| `cirro-download` | Bringing dataset files to your computer |
| `cirro-dataset-admin` | Renaming, retagging, moving between folders, deleting |
| `cirro-project-admin` | Projects, access, shares, workspaces |
| `cirro-spec-workflow` | Reading a Nextflow or WDL workflow and writing down what it needs |
| `cirro-define-test-dataset` | Picking test data, parameters and success criteria |
| `cirro-configure-pipeline` | Turning that into a registered, runnable Cirro pipeline |
| `cirro-dashboards` | Building, reading and editing Data Studio dashboards |
| `cirro-write-python` | Writing you a standalone script to run yourself |

Commands: `/cirro:setup` and `/cirro:doctor`.

Behind the approval gate sit eight tools: `session_init`, `login_start`, `login_wait`,
`whoami`, `logout`, `session_remove`, `plan` and `apply`. You will rarely name them —
Claude uses them — and only two can change anything: `apply`, which makes every change
in Cirro, and `session_remove`, which deletes a tenant's login folder on your own
machine. Both prompt every time.

## Adding a pipeline to Cirro

Getting a workflow — one of your own, or something off GitHub — running as a Cirro
pipeline is three steps, and Claude will walk you through them in order:

1. **Read the workflow.** Claude parses the repository and writes down every input and
   parameter it takes: which are required, which have defaults, which are just plumbing
   Cirro supplies for you. You get a `design-spec.md` to correct — this is the step where
   your knowledge of the science matters most, because the parser can only report what
   the code says, not which of 60 parameters a user should actually see.
2. **Pick something to test it on.** A small dataset, the parameters to run it with, and
   the specific things that must be true for the run to count as working.
3. **Configure and register it.** Claude generates the form your colleagues will fill in,
   the mapping from that form to the workflow's parameters, and the compute settings —
   then registers the pipeline.

You choose where the configuration lives: uploaded into Cirro, kept in the workflow's own
repository, or kept in a separate configuration repository. If it lives in a repository,
Claude writes and commits the files and **you** push them — it does not push, and it does
not open pull requests.

Either way you keep the files. Uploading into Cirro also saves a copy in a
`pipeline-config/` folder next to the design spec, so months later you can read what a
pipeline is actually configured to do without asking Cirro for it back.

**You can write the configuration yourself.** Generating it from a spec is the quick
path, but some forms cannot be described in one — a field revealed by two different
selectors, a custom widget, an input map tuned by hand. Point Claude at a folder of
`process-form.json` and friends instead of a spec and those files go up exactly as
written: validated, never reformatted, never regenerated. No copy is staged in that
case, because the files are already yours and already where you put them.

Two things worth knowing:

- **Claude only edits pipelines you own.** One belonging to a colleague, or curated by
  Cirro, is refused rather than changed.
- **Configuration is checked before it goes anywhere**, whether generated or written by
  hand. `cirro-agent read
  check-config` catches the failures the portal reports unhelpfully or not at all — form
  fields the renderer will ignore, file pickers that will show an empty list, parameters
  wired to fields that do not exist. Nothing in that check can tell you whether the form
  makes sense to a biologist, so open it in the Cirro UI once before sharing it.

## Building dashboards

Claude can build a Data Studio dashboard from a description — *"a volcano plot of the
DE results with a p-value cutoff I can change, and the significant genes beside it"* —
and read or change the ones you already have.

It works in the same terms you do: a table to read from, a query over it, a chart or
table of the result, a filter. Before anything is saved, the plan **runs the
dashboard's data on your machine** with the same engine the portal uses (DuckDB), so
the approval tells you how many rows each chart will show and which columns it is
drawing — and a dashboard that would come out blank, because a column is missing or a
chart was given the wrong kind of column, is refused with the reason instead of saved.
Files over 500 MiB are not downloaded for that check; the plan says so.

The dashboard's structure is checked against the schema your Cirro portal publishes at
`/schemas/dashboard-schema.json`, so it matches the portal that will draw it. A portal
that does not publish one yet is checked against a copy bundled with the plugin, and
the plan says which was used.

**If you have the dashboard open while Claude edits it**, your tab picks up the change
when you have no unsaved edits, and warns you instead of overwriting it when you do.
The other direction holds too: if you save in the portal after Claude has planned an
edit, the edit is refused as stale rather than written over your work.

## Locking it down further

The plugin already blocks the obvious ways around the gate: driving the Cirro SDK
directly from a Python one-liner, running a script or shell script that drives it, using
the standalone `cirro` CLI's upload or configure commands, or moving data with the AWS
CLI. Be clear about the limit, though — that check reads commands, and the scripts they
launch, as text. It makes accidental bypass impossible and deliberate bypass obvious. It
is not a sandbox.

Editing files is never blocked and never prompts. Writing Python that uses the Cirro SDK
is ordinary work; only running it is gated.

Nor does that check apply to a session that has not used the plugin. It arms the first
time a session invokes a Cirro skill, one of the MCP tools, or `cirro-agent`, and stays
on for the rest of that session. Until then there is no approval to bypass, so a project
that merely depends on the Cirro SDK is unaffected by having this plugin installed. Two
things never wait to be armed: `apply` always shows the plan and asks, and a command
that reads Cirro credential storage or the machine-wide `~/.cirro` is always refused.
If a session cannot be identified, the checks apply as though it were armed.

Plugins are not allowed to grant themselves permissions, so if you want a hard boundary,
add this to your own Claude Code settings:

```json
{
  "permissions": {
    "deny": [
      "Bash(cirro upload:*)",
      "Bash(cirro download:*)",
      "Bash(cirro configure:*)",
      "Bash(cirro-cli:*)"
    ],
    "allow": [
      "Bash(cirro-agent read:*)",
      "mcp__cirro__whoami",
      "mcp__cirro__login_start",
      "mcp__cirro__login_wait",
      "mcp__cirro__plan"
    ]
  }
}
```

Allowing `cirro-agent read` outright is safe — that command has no ability to change
anything, and a test fails if it ever gains one. `apply` is missing from the allow list
on purpose, and could not be added anyway: it prompts regardless.

## Status

Reading is complete and verified against a live Cirro tenant.

**The change path has now been exercised against a real Cirro tenant** — on 2026-09-06,
against a development tenant, in a project and folder set aside for it. What ran was
`upload_dataset` (a new two-file dataset into its own folder) and four `update_dataset`
writes on that dataset alone: a rename, a tag write, and a description edit, each
restored afterwards. Nothing that already existed was touched, and nothing was deleted.
Alongside the writes, four refusals were confirmed to fire: a forged digest, an altered
approval text, a re-used plan, and a plan whose facts had moved underneath it. Folder
membership survived every tag write, and fields left out of a request — a description
the caller never mentioned — survived rather than being blanked.

**Retyping a dataset has now run against a live tenant** — on 2026-09-18, against a
development tenant, on a two-file dataset uploaded for the purpose and left in place.
`update_dataset` moved it from `Paired DNAseq (FASTQ)` to `Paired 16S (FASTQ)` and back,
with the data-transform re-run and the sample ingest accepted each way; the name,
description, tags and folders it was not asked about survived both writes. The refusal
of a pipeline id as the target type was confirmed against the real process catalogue.
Whether the queued transforms and ingests then completed is not recorded here. Two bugs
surfaced that only a live run finds: the `get` verb's envelope had moved under the live
smoke test, and `update_project_tags` refused `[]`, so the tag that test adds could not
be taken off again.

**`run_analysis` and `chain` have now launched against a live tenant** — on 2026-09-09,
against a development tenant. Several chains of up to eight `run_analysis` steps were
approved and applied, and the launches were accepted. Whether the resulting analyses ran
to completion is not recorded here, so treat the launch path as exercised and the rest as
untested.

That is four operations out of the coverage table, on one tenant, plus the `chain`
wrapper that carried the third. **Every other operation has still only run against a
simulated tenant.** Four bugs found on 2026-08-27 were
exactly the kind that surface only on a real call, so treat your first use of any
operation not named above as untested and pick something disposable.

Pipeline configuration is built and its generated output is checked against the
configuration of 213 pipelines already running in Cirro, but the same caveat applies:
registering a pipeline has not yet been done against a live tenant.

**Targeting AWS HealthOmics is new in 0.10.0 and has not been through a real run.**
The argument is validated, the approval text names the target, and the field is
asserted on the serialized request -- but no launch has yet reached HealthOmics through
the plugin. The Batch default is the older path and is unchanged. After a run lands,
`cirro-agent read dataset <id> -p <project> --include info` is the reliable check:
`info.omicsRunUuid` and `info.jobId` are present exactly when the run reached
HealthOmics, and dataset status is not a substitute.

**Declaring which modes a pipeline may use is new in 0.12.0 and has not been sent to a
live tenant.** `executionModes` is absent from the pinned API client's model, so the
plugin puts it on the request itself; the tests assert it is on the serialized body and
that editing a pipeline for any other reason carries it forward, but no registration
carrying it has been accepted by a real server. Registering a pipeline at all is
untested live, as above, so this inherits that caveat rather than adding a new one.

**Dashboards have run against a live tenant** — on 2026-09-29, against a development
tenant, in a project set aside for it. `create_dashboard` made two throwaway
dashboards — one over a dataset file (scatter, histogram, box plot, table, a number
control feeding the SQL and a multi-select filter), one over a sheet — and both
rendered in the portal as planned. `edit_dashboard` restyled a chart, changed a
control, added a tile and moved tiles; an edit planned before a save in the portal was
refused as stale, and the portal's save survived. `delete_dashboard` removed both. The
reads, including `dashboard --run`, were checked against all 40 dashboards on that
tenant. The portal half — an open dashboard reloading to show a plugin's edit, and
refusing to save over it with unsaved edits — was exercised with an unreleased portal
build; until that build is deployed, an open tab can still overwrite an edit.

**Signing in for a tenant has now been through a real login.** On 2026-09-09 a
device-code login landed in a tenant folder against a development tenant, and plans
were created and changes applied from it. `session_remove` is the half that remains
untested: it has only ever been called directly, never through the MCP server, so
deleting a tenant folder is still a path nobody has walked. Signing in for a single
directory is the older path and is unaffected. If a tenant login misbehaves,
`cirro-agent read tenants` will show you what exists without needing a credential, and
deleting the folder it names is always a safe reset.

## For maintainers

See [CLAUDE.md](CLAUDE.md) for the standing rules, the architecture and the traps.

```bash
DEV_VENV="${XDG_DATA_HOME:-$HOME/.local/share}/cirro-agent/dev-venv"
UV_PROJECT_ENVIRONMENT="$DEV_VENV" uv sync --python 3.12 --project runtime --group dev
PYTHONPATH=python/src "$DEV_VENV/bin/python" -m pytest python/tests -q
claude --plugin-dir ./
```

The environment lives outside the checkout deliberately. A directory marketplace
copies the plugin root verbatim into every install — it honours no `.gitignore` and
takes no exclude list — so a virtualenv inside the tree is shipped with the plugin,
and a second one appears in every git worktree you open.

`python/tests/test_readme_current.py` fails when this file falls out of step with the
code, so drift breaks the build rather than accumulating.

`python/tests/test_live_reads.py` runs every read against a real tenant, using the
session folder of the directory you run it from. It is skipped unless you set
`CIRRO_AGENT_E2E=1`, and it changes nothing. `python/tests/test_live_writes.py` is
the smallest real check of the change path — add a tag, take it off again — and runs
only when you also name a disposable project in `CIRRO_AGENT_E2E_WRITE_PROJECT`.

CI (`.github/workflows/ci.yml`) runs the tests, the lint, and `claude plugin validate`
on every push, and again weekly so a new `cirro` release that breaks the pinned client
is caught by `test_runtime_contract.py` rather than by a user. Coverage has a floor
that only moves up.

## License

MIT — see [LICENSE](LICENSE).

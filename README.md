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

Now go to whatever directory you want to work in and ask Claude to connect to Cirro.
It will ask for your Cirro hostname (something like `app.cirro.bio`), then hand you a
link to sign in with. After that, just ask for what you want:

> *"What failed in the Immuno-Oncology project this week?"*
> *"Why did that run fail?"*
> *"Move those three datasets into the QC folder."*

`/cirro:doctor` tells you where things stand if anything seems off.

### Where your credentials live

Each directory you work in gets its own `.cirro/` folder holding that directory's login,
a cache, and a log of every change made. It ignores itself in git, and it is separate
from the `~/.cirro` used by the standalone `cirro` CLI — the plugin never touches that.

One directory, one Cirro tenant. If you work with two, use two directories; the plugin
will not guess between them.

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
| Structure | `projects`, `project`, `folders`, `describe`, `whoami`, `doctor` |
| Datasets | `datasets`, `dataset`, `files`, `params`, `analysis-status` |
| Runs | `logs`, `tasks`, `task-logs`, `trace` |
| Pipelines | `processes`, `process`, `process-params`, `process-config`, `workflow-spec`, `check-config` |
| Project resources | `samples`, `references`, `reference-types`, `compute-envs`, `shares`, `workspaces`, `project-users`, `users` |

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
files and bytes involved, and looks for consequences you might not have considered.

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
say yes. A plan is good for one change and expires after ten minutes, so a stale
approval cannot be reused later.

Every change is written to a log in `.cirro/runs/` — what was done, when, and as whom.

A few things worth knowing:

- **Cirro has no pricing API**, so a plan tells you what will run and on what, rather
  than inventing a cost estimate.
- **Folders are safe.** Cirro stores folder membership alongside tags, so a careless tag
  edit could quietly move a dataset out of its folder. The plan always resolves folder
  membership explicitly and tells you it is preserving it.
- **Some things are deliberately not offered** — renaming or re-billing a project,
  editing a live share's conditions, billing changes. Those belong in the Cirro web UI
  where their effects are visible.

### What Claude can change

| Area | Operations |
|---|---|
| Analyses | `run_analysis`, `resume_analysis`, `stop_analysis` |
| Adding data | `upload_dataset`, `upload_reference`, `import_public` |
| Datasets | `update_dataset`, `update_samplesheet`, `delete_dataset` |
| Projects | `create_project`, `update_project_tags`, `archive_project`, `unarchive_project` |
| People | `set_user_role`, `invite_user` |
| Metadata | `update_sample`, `update_project_schema` |
| Sharing | `create_share`, `delete_share`, `subscribe_share`, `unsubscribe_share` |
| Workspaces | `create_workspace`, `start_workspace`, `stop_workspace`, `delete_workspace` |
| Pipelines | `create_process`, `update_process`, `upload_process_config`, `sync_process_config`, `archive_process` |
| Your computer | `write_process_config`, `commit_process_config` |
| Your computer | `download_files` |

Downloading is gated too. It does not change anything in Cirro, but it can fill a disk,
so the plan tells you how many bytes are coming and how much space you have.

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
| `cirro-write-python` | Writing you a standalone script to run yourself |

Commands: `/cirro:setup` and `/cirro:doctor`.

Behind the approval gate sit seven tools: `session_init`, `login_start`, `login_wait`,
`whoami`, `logout`, `plan` and `apply`. You will rarely name them — Claude uses them —
but `apply` is the only one that can change anything, and it always prompts.

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

Two things worth knowing:

- **Claude only edits pipelines you own.** One belonging to a colleague, or curated by
  Cirro, is refused rather than changed.
- **The generated configuration is checked before it goes anywhere.** `cirro-agent read
  check-config` catches the failures the portal reports unhelpfully or not at all — form
  fields the renderer will ignore, file pickers that will show an empty list, parameters
  wired to fields that do not exist. Nothing in that check can tell you whether the form
  makes sense to a biologist, so open it in the Cirro UI once before sharing it.

## Locking it down further

The plugin already blocks the obvious ways around the gate: driving the Cirro SDK
directly from a Python one-liner, using the standalone `cirro` CLI's upload or configure
commands, or moving data with the AWS CLI. Be clear about the limit, though — that check
reads shell commands as text. It makes accidental bypass impossible and deliberate bypass
obvious. It is not a sandbox.

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

The change path is built and tested, but **no change has yet been applied to a real Cirro
tenant** — only to a simulated one. Four bugs found on 2026-08-27 were exactly the kind
that surface only on a real call, so treat your first use of any given operation as
untested and pick something disposable.

Pipeline configuration is built and its generated output is checked against the
configuration of 213 pipelines already running in Cirro, but the same caveat applies:
registering a pipeline has not yet been done against a live tenant.

## For maintainers

See [CLAUDE.md](CLAUDE.md) for the standing rules, the architecture and the traps.

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.dev-venv" uv sync --python 3.12 --project runtime --group dev
PYTHONPATH=python/src .dev-venv/bin/python -m pytest python/tests -q
claude --plugin-dir ./
```

`python/tests/test_readme_current.py` fails when this file falls out of step with the
code, so drift breaks the build rather than accumulating.

## License

MIT — see [LICENSE](LICENSE).

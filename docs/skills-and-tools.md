# Skills and tools

You will rarely name any of these — Claude picks them up from what you ask. They are
listed here so you know what is there.

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
| `cirro-sheets` | Creating sheets and editing their columns and rows |
| `cirro-write-python` | Writing you a standalone script to run yourself |

## Commands

| Command | Does |
|---|---|
| `/cirro:setup` | Builds the plugin's Python runtime, once per machine |
| `/cirro:doctor` | Reports which tenant this directory uses, whether the runtime is installed, and who you are signed in as |

## Tools behind the approval gate

Reading goes through `cirro-agent read` (see [Looking at things](reading.md)). Signing in
and changing things go through eleven tools: `session_init`, `login_start`,
`login_wait`, `whoami`, `logout`, `session_remove`, `plan`, `apply`, `grant`,
`apply_granted` and `grants`.

Three of them can change anything:

- `apply` makes a change in Cirro and prompts every time — and, when you accept Claude's
  offer to stop asking, approves later changes like it in the same prompt.
- `apply_granted` makes only the changes a grant you approved covers.
- `session_remove` deletes a tenant's login folder on your own machine and prompts every
  time.

`grant` prompts every time as well, and `grants` lists the grants you have given or
revokes them. `plan` only works out what a change would do; it changes nothing.

# Looking at things

Ask in plain language and Claude finds the answer. Reading never changes anything and
never needs your approval, so Claude can look around as much as a question needs.
(Claude Code still asks before running the read command until you allow it; see
[Two kinds of prompts](getting-started.md#two-kinds-of-prompts).)

## What to ask

| Area | For example |
|---|---|
| Projects and folders | *"Summarize the Immuno-Oncology Core project."* |
| Datasets | *"Which datasets failed this week, and who made them?"* |
| Runs | *"Why did ds_123 fail? Show me the task that broke."* |
| Pipelines | *"What parameters does nf-core/rnaseq take in Cirro?"* |
| Samples and references | *"What metadata do the samples in this project have?"* |
| Spend | *"What did this project spend last month, and on what?"* |
| Dashboards and sheets | *"What does the QC dashboard show?"* |

## Big answers stay useful

Ask about a project with 4,000 datasets and you will not get 4,000 rows, and you will
not get an arbitrary 40 either. You get the shape of all of them, such as how many
failed, who made them, which pipelines produced them and when, plus a handful of
labeled examples and some suggested follow-up questions that already know how many
results they will return.

The same applies to anything large: a 500-file dataset comes back as file patterns rather
than 500 filenames, and a huge workflow log comes back as its errors plus a count of the
repetitive lines.

**Some details cost extra**, like dataset sizes, which need a separate lookup per
dataset. Claude tells you when it is skipping those and how to ask for them.

## What has been spent

Nothing prices a run before it happens, but what has already been spent is readable at
two scales:

- **A project's spend**, by month and by AWS service, or every project you can see
  compared in a single call.
- **One analysis's compute cost**, split by task status and rolled up by pipeline step,
  so you can see which step took the money and how much went on tasks that failed. It
  is an estimate until the run's costs settle, and a dataset you uploaded rather than
  produced with a pipeline has no compute cost to show.

## The read command

Underneath, Claude reads with `cirro-agent read`, a command with no ability to change
anything; a test fails if it ever gains one. Claude runs it for you inside Claude Code,
so you can see exactly what it ran. It is not installed on your own `PATH`.

```bash
cirro-agent read datasets -p "My Project" -f "status:FAILED created:last-7d owner:@me"
cirro-agent read folders -p "My Project"
cirro-agent read tasks -p "My Project" -d ds_123
```

| Area | What it can read |
|---|---|
| Structure | `projects`, `project`, `folders`, `describe`, `whoami`, `tenants`, `doctor` |
| Datasets | `datasets`, `dataset`, `files`, `params`, `analysis-status` |
| Runs | `logs`, `tasks`, `task-logs`, `trace`, `chain-status` |
| Pipelines | `processes`, `process`, `process-params`, `process-config`, `workflow-spec`, `check-config` |
| Project resources | `samples`, `references`, `reference-types`, `compute-envs`, `shares`, `workspaces`, `project-users`, `users` |
| Money | `spend`, `cost` |
| Dashboards | `dashboards`, `dashboard` (add `--run` to execute its data), `dashboard-schema`, `preview` |
| Sheets | `sheets`, `sheet` (its columns and a page of rows with their `_row_id`; `--where` filters them) |

`cirro-agent read describe dataset` lists the fields a listing can be filtered on, and
the filter grammar.

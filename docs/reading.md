# Looking at things

Ask in plain language and Claude handles the rest. Reading never prompts and never
changes anything. Underneath it uses a read-only command you can also run yourself:

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
| Dashboards | `dashboards`, `dashboard` (add `--run` to execute its data), `dashboard-schema`, `preview` |
| Sheets | `sheets`, `sheet` (its columns and a page of rows with their `_row_id`; `--where` filters them) |

`cirro-agent read describe dataset` lists the fields you can filter on and the query
grammar.

## Big answers stay useful

Ask about a project with 4,000 datasets and you will not get 4,000 rows, and you will
not get an arbitrary 40 either. You get the shape of all of them — how many failed, who
made them, which pipelines produced them, when — plus a handful of labeled examples and
some suggested follow-up questions that already know how many results they will return.

The same applies to anything large: a 500-file dataset comes back as file patterns rather
than 500 filenames, and a huge workflow log comes back as its errors plus a count of the
repetitive lines.

**Some details cost extra**, like dataset sizes, which need a separate lookup per
dataset. Claude will tell you when it is skipping those and how to ask for them.

## What has been spent

Nothing prices a run before it happens, but what has *already* been spent is real and
readable at two scales:

- `cirro-agent read spend -p "My Project"` breaks a project down by month and AWS
  service, and `--all-projects` compares every project you can see in a single call.
- `cirro-agent read cost -p "My Project" -d ds_123` gives one analysis's compute bill,
  split by task status and rolled up by pipeline step, so you can see which step took
  the money and how much of it went on tasks that failed.

## Letting reads through without a prompt

Claude Code asks before running a shell command it has not been allowed to run. Reading
Cirro is safe to allow outright, because `cirro-agent read` has no ability to change
anything. [Locking it down](locking-down.md) has the settings.

# Cirro plugin for Claude Code

A Claude Code plugin for the [Cirro](https://cirro.bio) data platform: Claude reads a
person's projects, datasets, runs and logs freely, and every change it would make waits
for that person's approval.

**Using the plugin?** Read the [documentation site](https://solid-carnival-1g9egwp.pages.github.io/).
It is built from [`docs/`](docs/) and published to GitHub Pages by
`.github/workflows/docs.yml`; for now it is visible only to people with access to this
repository. To install:

```bash
claude plugin marketplace add CirroBio/Cirro-claude-plugin
claude plugin install cirro@cirro
```

The rest of this file is for working on the plugin.

## How it is built

One rule shapes everything: **every capability that creates, updates or deletes data is
gated on the user's approval, and reads are not gated.** The plugin therefore has two
surfaces, split by that gate:

```
reads   (ungated) ──▶ bin/cirro-agent ─┐
                                        ├──▶ worker subprocess ──▶ .cirro/ (CIRRO_HOME)
writes  (gated)   ──▶ MCP server (11) ─┘         the only code importing cirro
```

- **Reads** run over Bash through `bin/cirro-agent read`. It has no write path, and two
  tests keep it that way, which is what makes a single broad `Bash(cirro-agent *)`
  permission safe. Reads never prompt, cost no context when unused, and compose with
  the shell.
- **Writes** go through the MCP server as `plan` then `apply`. `plan` is read-only and
  records exactly what would happen; `apply` takes no operation arguments, re-plans,
  and carries `requiresUserInteraction`, which no allow rule, permission mode or hook
  can answer for the user. `grant` and `apply` with `stop_asking` let a person approve
  one kind of change in one project in advance, for the life of the server process.
- **The MCP server never imports `cirro`.** It spawns a fresh worker per operation over
  stdio, because the SDK reads `CIRRO_HOME` once at import time and a long-lived
  process could serve only one session folder.
- **Session folders** (`.cirro/`) hold the login, a cache, plan records and an audit
  log. One is found by walking up from the working directory, falling back to a single
  signed-in tenant folder outside every checkout.
- **A PreToolUse hook** (`scripts/gate.py`) reads the Bash commands Claude runs, and the
  scripts they execute, and refuses ones that would drive the SDK, start the worker or
  server by hand, or read a session folder's credentials.

[CLAUDE.md](CLAUDE.md) is the long form: the standing rules, why each piece is shaped
the way it is, and the traps that have already bitten. Read it before changing the
approval path, the gate, session resolution or the request models. The user-facing
account of the same design is the docs site's
[How it works](https://solid-carnival-1g9egwp.pages.github.io/how-it-works/).

## Where things are

| Path | What it is |
|---|---|
| `.claude-plugin/` | The plugin manifest (which declares the MCP server) and the marketplace entry |
| `bin/cirro-agent` | The read-only CLI that reads run through |
| `scripts/mcp-server`, `scripts/setup` | The MCP server launcher, and the runtime installer `/cirro:setup` runs |
| `scripts/gate.py`, `hooks/hooks.json` | The hook that keeps Bash from going around the approval |
| `skills/`, `commands/` | The skills and slash commands Claude Code loads |
| `python/src/cirro_agent/` | The package: `mcp_server.py`, `cli.py`, `session.py`, `plans.py`, and `worker/` |
| `python/src/cirro_agent/worker/` | The only code that imports `cirro`: reads, the gated path, and one module per family of operations |
| `python/tests/` | The suite, run against a simulated tenant over recorded HTTP |
| `runtime/` | The pinned runtime: `pyproject.toml` and `uv.lock` |
| `docs/`, `mkdocs.yml` | The user documentation site |

Everything in the plugin root is copied into every install of it, so nothing belongs
here that is not meant to ship. Virtualenvs and built sites go outside the checkout.

## Development

```bash
DEV_VENV="${XDG_DATA_HOME:-$HOME/.local/share}/cirro-agent/dev-venv"
UV_PROJECT_ENVIRONMENT="$DEV_VENV" uv sync --python 3.12 --project runtime --group dev
PYTHONPATH=python/src "$DEV_VENV/bin/python" -m pytest python/tests -q
"$DEV_VENV/bin/python" -m flake8 python/src python/tests scripts/gate.py
claude --plugin-dir ./
```

The environment lives outside the checkout deliberately. A directory marketplace
copies the plugin root verbatim into every install — it honours no `.gitignore` and
takes no exclude list — so a virtualenv inside the tree is shipped with the plugin,
and a second one appears in every git worktree you open.

Bind a scratch directory to a tenant for testing, not the checkout: a `.cirro/` in the
plugin root would be copied, credential and all, into the plugin cache on every
install.

### Running an unreleased `main` everywhere

`--plugin-dir` loads the checkout into one terminal session. To run an unreleased
`main` everywhere instead — every session, the desktop app included — install it in
place of the published plugin. Install from an export, not the checkout, for the same
reason as above: the checkout holds `.claude/worktrees/`, and a directory install
would copy every worktree in it.

```bash
EXPORT="${XDG_DATA_HOME:-$HOME/.local/share}/cirro-claude-plugin-main"
rm -rf "$EXPORT" && mkdir -p "$EXPORT" && git archive main | tar -x -C "$EXPORT"
claude plugin uninstall cirro@cirro --keep-data
claude plugin marketplace remove cirro
claude plugin marketplace add "$EXPORT"
claude plugin install cirro@cirro
```

The export is a snapshot. When `main` moves, export it again, then uninstall and
reinstall the plugin. `main` carries the last release's version number until the next
release, so the number cannot tell you which code a session is running. Sessions
already open keep the MCP server they started with, so test in a new one. To go back to
the published plugin, uninstall it, remove the `cirro` marketplace, and add it again
from `CirroBio/Cirro-claude-plugin` as at the top of this file. Tenant folders and the
runtime live outside the plugin and survive both.

### Live checks

The suite needs no live Cirro. Two opt-in checks do, and are skipped without their
variables. `python/tests/test_live_reads.py` runs every read against the tenant the
working directory is bound to, and changes nothing; set `CIRRO_AGENT_E2E=1`.
`python/tests/test_live_writes.py` is the smallest real check of the change path — add
a tag, take it off again — and runs only when you also name a disposable project in
`CIRRO_AGENT_E2E_WRITE_PROJECT`. Naming a dataset in `CIRRO_AGENT_E2E_WRITE_DATASET`
adds a tag round trip on it, and `CIRRO_AGENT_E2E_RETYPE_TO` a retype and back. It
resolves the session folder from the checkout unless `CIRRO_AGENT_E2E_DIR` names a
directory bound to the tenant you mean, which is what you want on a machine signed in
to more than one. Never point it at anything that is not disposable.

## Contributing

**A new mutating capability goes through `plan` and `apply`.** There is no other way to
add one, and the tests are built to make forgetting fail:

1. Add the operation to the family module it belongs to in
   `python/src/cirro_agent/worker/` (`dataset_ops.py`, `sheet_ops.py` and so on), which
   registers it into `mutations.OPS`. Declare every argument it reads in `accepts`; a
   test walks each planner and fails on an undeclared key.
2. Add the SDK methods it calls to `GATED` in `python/tests/test_write_coverage.py`.
   That test fails on any SDK write method that is in neither `GATED` nor
   `UNSUPPORTED`, so a `cirro` upgrade cannot quietly introduce an ungated mutation.
3. Exercise its request construction in `python/tests/test_request_models.py`. The
   generated request models replace whole records, so an omitted field is blanked.
4. Name it in the `plan` tool's description in `mcp_server.py` and in the coverage table
   in `docs/changes.md`. Tests check that both name it.

`bin/cirro-agent` must never gain a write path. Making an operation grantable widens
what a person can approve in advance; it is a decision about the approval rule, pinned
to an exact set in `test_write_coverage.py`, and never for an operation that deletes or
changes access. CLAUDE.md says what a grantable operation's variants need.

**The docs are part of the change.** A skill, command, read subcommand, operation or
MCP tool added, removed or renamed; a change to the approval mechanism or the session
folder; a file the docs name moving — update `docs/` in the same commit.
`python/tests/test_docs_current.py` checks the mechanical half: everything the plugin
has is named in the docs, no page is missing from the navigation, and neither the docs
nor this file names a file that does not exist. It cannot tell whether the prose is
still true, so read the affected page.

**Before you push**, run the suite and the lint as above, and `claude plugin validate
./`. CI (`.github/workflows/ci.yml`) runs all three on every pull request and every push to
`main`, and again weekly so a new `cirro` release that breaks the pinned client is caught by
`test_runtime_contract.py` rather than by a user. Coverage has a floor that only moves
up.

**`main` takes no merge commits.** Rebase a branch onto `main`, or squash it, before it
lands.

## The documentation site

The site is [MkDocs](https://www.mkdocs.org/) with the Material theme, matching the
Cirro docs. To preview it:

```bash
uv run --no-project --with-requirements docs/requirements.txt mkdocs serve
```

`mkdocs serve` builds into a temporary directory. To build the site yourself, pass
`--strict --site-dir` with a path outside the checkout, for the reason given under
Development. `.github/workflows/docs.yml` builds it with `--strict` on every pull request
that touches it, and publishes it from `main`.

[How it works](docs/how-it-works.md) is a hover diagram on the pattern of the Cirro data
governance diagram. The drawing and its notes are inline in the page;
`docs/assets/hover-diagram.css` and `docs/assets/hover-diagram.js` are shared. A part
marked `data-key="x"` shows the note `data-note="x"` and dims every element whose
`data-r` list does not include `x`.

## Releasing

1. Bump the version in `.claude-plugin/plugin.json`, `python/pyproject.toml`, the
   `version=` in `python/src/cirro_agent/mcp_server.py`, and `runtime/pyproject.toml`,
   then run `uv lock --project runtime` and check it with
   `uv lock --check --project runtime`.
2. Commit that as `Release X.Y.Z` and push `main`.
3. Tag it `vX.Y.Z` with an annotated tag and push the tag — never in the same push as
   `main`.
4. Publish the GitHub release from the tag (`gh release create vX.Y.Z --verify-tag`),
   with notes written for users: what changed for them, any known limit it leaves, and
   whether they need to update and start a new session.

## License

MIT — see [LICENSE](LICENSE).

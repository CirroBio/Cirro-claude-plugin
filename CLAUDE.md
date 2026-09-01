# Working in this repo

Standing rules for the Cirro Claude Code plugin. The codebase's own conventions win
where these are silent.

## The firm rule

**Every capability that creates, updates or deletes data is gated on user approval.
Reads are not gated.** This is not a preference to trade off against convenience.

A new mutating capability goes through `plan` → `apply` and is registered in
`python/src/cirro_agent/worker/mutations.py`. It must appear in `test_write_coverage.py`
— either in `GATED` with the operation that exposes it, or in `UNSUPPORTED` with a
reason. That test fails on any SDK write method that is in neither, so a `cirro` upgrade
cannot quietly introduce an ungated mutation. Do not work around it; update it.

`bin/cirro-agent` must never gain a write path. Its safety argument is that a single
broad `Bash(cirro-agent *)` permission is harmless, and that only holds while the binary
cannot mutate anything. Two tests enforce it.

## Keep the README current

**The README is part of the change, not follow-up work.** On any change that makes it
wrong, update it in the same commit:

- a skill added, removed or renamed
- a command added or removed
- a `cirro-agent read` subcommand added or removed
- a gated operation added or removed
- an MCP tool added or removed
- a change to how reads and writes are separated, or to the approval mechanism
- a change to the session folder layout, the runtime pins, or the dependency override
- a file named in the README moving or disappearing

`python/tests/test_readme_current.py` checks the mechanical half of this — every skill,
command, read subcommand, gated operation and MCP tool is named in the README, and the
README references no file that does not exist. It cannot check whether the prose is
still *true*, so read the affected section rather than only satisfying the test.

Keep the Status section honest. It currently records that no mutation has run against a
live tenant; when that changes, change it.

## Why the architecture is shaped this way

The README is for users and deliberately does not carry this. Keep it here.

```
reads   (ungated) ──▶ bin/cirro-agent ─┐
                                        ├──▶ worker subprocess ──▶ .cirro/ (CIRRO_HOME)
writes  (gated)   ──▶ MCP server (7) ──┘         the only code importing cirro
```

**Two surfaces, split by the gate.** Writes must prompt every time, which only an MCP
tool can guarantee — `_meta["anthropic/requiresUserInteraction"]` is not defeated by an
allow rule, any permission mode, or a hook returning `allow`. Bash has no equivalent.
Reads must never prompt, must not occupy context when unused, and benefit from shell
composition. Keeping `bin/cirro-agent` free of any write path is what makes one broad
`Bash(cirro-agent *)` rule safe to grant; two tests enforce that.

**The MCP server never imports cirro.** It is a thin dispatcher over stdio that spawns a
fresh worker per operation. Forced by the `CIRRO_HOME` import freeze below: a persistent
process could only ever serve one session folder. It also keeps credentials out of the
agent's context — they live on disk and a worker picks them up for one operation.

**Declare the MCP server in `.claude-plugin/plugin.json`, never a root `.mcp.json`.** A
file at that path is also read as a project-scoped config by anyone running Claude Code
inside this repo, where `${CLAUDE_PLUGIN_ROOT}` is undefined and the spawn fails with
`ENOENT`. `test_readme_current.py` guards this.

**The runtime overrides cirro's own `cirro-api-client<1.6.0` pin.** The process
config-revision routes and the `ConfigSource` enum exist only in >=1.6.0, and pipeline
configuration is impossible without them. `test_runtime_contract.py` fails if a future
`cirro` release stops working against that client.

**Reads are facet-first because Cirro has no server-side filtering.** A listing has
already fetched everything before it answers, so facets over the whole match set and
exact predicted counts for follow-up filters are free, while rows are the expensive
thing. That is the whole justification for the envelope shape.

## Things that will bite you

**`CIRRO_HOME` is frozen at import time.** `cirro.config.Constants` reads it in a class
body, so it must be in a child process's environment *before* Python starts. Setting
`os.environ` after `import cirro` silently writes to the user's real `~/.cirro`. Only
`session.child_env()` sets it, the worker asserts it took effect, and
`test_cirro_home.py` encodes both directions.

**The generated request models are whole-record replacements, not patches.**
`UpdateDatasetRequest` requires all four of name/description/process_id/tags; omitting
one blanks it. Resolve every field from the current record in the planner. Four apply
paths were broken this way. `test_request_models.py` exercises every operation's request
construction against a recording double — a new operation must be added there.

**Dataset folders are `folder://` tags.** Report them as `folders`, never surface the
prefix, and never write a tag list without re-including folder membership.

**A form's fields are not just its `properties`.** Conditional fields live inside
`dependencies.<selector>.oneOf[].properties`, and `dependencies` keys are *selector field
names*, not a nesting level. Any analysis that walks only `properties` will report live,
correctly-mapped fields as dangling — this has already produced one wrong answer to a
user. Use `formcheck.form_paths()`, which is the one implementation of that walk.

**The form validator is calibrated, not invented.** Its three tiers come from measuring
the 213 forms and 72 input maps in Cirro-pipelines: 2 errors and 5 errors respectively,
each a genuine defect. If a change to `formcheck.py` starts flagging production
configuration, the validator is wrong, not the configuration. `folder` is documented by
Cirro but used by none of the 213, so it warns rather than errors.

**`pipeline_ops` registers itself into `mutations.OPS`.** The import sits at the bottom
of `mutations.py` and the registration at the bottom of `pipeline_ops.py`, so the two
import in either order; putting the registration on the `mutations` side reintroduces a
circular-import failure for anyone importing `pipeline_ops` first.

**Never call `DataPortal()` bare.** With no cached token it enters a device-code login
and blocks until the code expires. Build auth explicitly via `worker/portal.py`.

**`get_all_records` truncates silently** and overshoots its own ceiling. Paginate
locally so truncation is detectable and `scan_complete` can be honest.

## Response discipline

Facets and counts describe the whole match set, never the rows returned. A response that
leaves something out says so in `omitted` with a runnable fix. Every error carries either
`retry_with` or `user_action` — never neither. Structures are never truncated
mid-object; the budget ladder degrades whole rows, then facet depth, then suggestions,
and never to zero suggestions.

## Verification

`pytest python/tests -q` and `flake8 python/src python/tests scripts/gate.py` before
finishing. Tests use a fake tenant over recorded HTTP; nothing in the suite needs a live
Cirro. When a live check is genuinely required, say so rather than marking work verified.

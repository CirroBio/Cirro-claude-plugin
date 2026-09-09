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

**One capability is gated without being an op, and it is the only one.**
`session_remove` deletes a tenant's session folder -- credential, cache and audit
trail. It cannot be a `plan` operation, because `do_plan` reads `reader.username` and
so authenticates: a folder would become unremovable exactly once its credential
stopped working, which is the state that makes people want to remove one. It uses the
same enforcement primitive `apply` does, `requiresUserInteraction`, which no allow
rule or permission mode defeats, and it belongs to the session-folder lifecycle
(`session_init`, `login_start`, `logout`) that has always sat outside the plan path.
`test_tenants.py` asserts those two tools are the *only* ones carrying that flag, so
neither the exception nor the rule can widen unnoticed. Nothing that touches the Cirro
API may follow this precedent -- `test_write_coverage.py` still fails on any SDK write
method that is not in `GATED` or `UNSUPPORTED`, and this reaches no SDK method at all.

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
writes  (gated)   ──▶ MCP server (8) ──┘         the only code importing cirro
```

**The gate judges execution, never authoring.** A `Write|Edit` matcher used to `ask` on
any .py mentioning the SDK. It made every project that uses cirro -- this repo above all
-- unpleasant to work in while the plugin was installed, and it stopped nothing:
`python that_file.py` was allowed, because only the command's own text was searched. The
hook now reads the file a command actually executes -- an interpreter's script argument,
a bare `./x.py`, or a shell script, which re-enters the parser as the command payload it
is -- and blocks on that instead. Do not reintroduce a write-time prompt; if a new
bypass appears, it is an execution the parser is not reading yet.

**The bypass checks arm; they do not run from the start of every session.** The first
use of a Cirro skill, MCP tool or `cirro-agent` writes a marker under
`plugin_data()/armed-sessions/<session_id>`, and that session is gated from then on.
Before that there is no approval to bypass, so the checks bought nothing and cost
everyone whose project merely depends on the SDK -- the same complaint that killed the
`Write|Edit` matcher, arriving by a different route. Three rules keep this from becoming
a hole, and each has a test: `apply` is never conditional on arming, credential snooping
is never conditional on arming, and a session with no usable `session_id` is treated as
**armed**, not unarmed -- otherwise a host that stops sending the field switches the
whole gate off silently. The marker is per session and pruned after 14 days.

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

**Reads are split by verb, with one shared fetch layer.** `worker/reads.py` holds
`Reader` -- the client, cache, cost accounting and name resolution -- plus the single
implementation of each expensive fetch (`dataset_detail`, `dataset_manifest`,
`dataset_tasks`, `dataset_cost`, `process_detail`, `process_revision`,
`process_assets`, `spec_shape`, `file_count`, `project_metrics`, `all_project_metrics`,
`portal_dataset`). The op
families plan against this layer too, so "a fetch two verbs need" includes a verb and a
planner: `process_revision` and `process_assets` are here because the read path and
`plan_upload_process_config` had each grown their own copy, and the two had drifted to
different slot counts -- so the approval understated what a revision would replace. The
verbs live in `read_query` (query, get),
`read_summaries` (summarize, task_logs), `read_listings` (list), `read_process`
(process_params, process_config) and `read_chains` (chain_status, which reads plan
records and the audit and touches no API); each imports from `reads` and nothing
imports them but `dispatch`. The four reads that need no session, credential or network --
describe, workflow_spec, check_config, tenants -- live in `cirro_agent/local_reads.py`, outside
`worker/`, and the CLI runs them in the parent process; that is the only read path that
does not go through the subprocess, and it must stay cirro-free. The gated write path
(`do_plan`, `do_apply`, re-planning at apply) is `worker/gated.py`; `dispatch.py` is
only the routing table. A new fetch
that two verbs need goes in `reads.py`; a new verb goes in the family it belongs to.

**Reads are facet-first because Cirro has no server-side filtering.** A listing has
already fetched everything before it answers, so facets over the whole match set and
exact predicted counts for follow-up filters are free, while rows are the expensive
thing. That is the whole justification for the envelope shape.

## Things that will bite you

**Session resolution starts wherever it is told to.** `find_session_dir(start)` walks up
from `start`, defaulting to the process's working directory. Every MCP tool that
resolves a session takes `directory`, and every read subcommand takes `--dir`, so one
Claude session can work across several bound folders without being moved -- which is
what people actually do. A test asserts each tool still takes it. The safety rules are
unchanged and do the work: the machine-wide global credential folder is still refused,
nothing is created implicitly, and `session_dir` is inside `DIGEST_FIELDS`.

**When the walk finds nothing, one signed-in tenant answers; two never do.** A tenant
folder is `plugin_data()/tenants/<host>/.cirro`, created by `init_tenant` and reached
by every directory with nothing bound nearer, so a person signs in once rather than
once per project. It lives under `plugin_data()` for the reason that function gives --
inside the plugin root it would be copied into every install, credential and all --
and that is the whole point: the two failures it exists to fix are a `.cirro/` in a
checkout that an install overwrites, and a `.cirro/` high in `$HOME` that silently
captures every project below it, because `_search_ceiling` falls back to home.

The fallback finds and never creates, the walk-up still wins, and `bound_tenants()`
returning more than one is an error listing them in `candidates` -- choosing between
tenants is the one thing this resolver has never done.

`tenant_host` is where a "host" that is not a host stops. `_normalize_base_url` strips
the scheme and the path but returns `..` for `../..`, and a tenant path is used to both
create and delete a directory -- so `tenants_root()/".."` is the folder holding the
virtualenv, and `remove_tenant` would have handed it to `rmtree`. Validating the name
in the one place both callers derive their path from is why the removal takes a
hostname and never a path. `remove_tenant` then re-checks that the resolved parent is
the tenants root, which is what stops it following a symlinked tenant folder out of
the tree. `tenant_contents` is the one implementation of what a folder holds, so the
figures the `tenants` read shows are the figures the removal discloses. One consequence to keep in
view: two projects sharing a tenant folder share a `session_dir`, so a plan made while
in one can be applied while in the other. That was already true of any two directories
under one bound folder; the tenant folder makes it the common case rather than the
accidental one, and the approval text still names the tenant and the objects.

**Nothing may live in the plugin root that is not meant to ship.** A directory
marketplace copies the whole tree into every install, verbatim: it consults no
`.gitignore`, takes no exclude list, and skips only `.git` and `node_modules` from a
list inside Claude Code. There is a `mode: "link"` source that copies nothing, but it
exists only for `command` sources, not the relative path a marketplace entry uses. So
both virtualenvs default outside the root -- `${XDG_DATA_HOME:-~/.local/share}/cirro-agent`,
holding `venv` and `dev-venv` -- and `test_runner.py` asserts the default is outside
the root. They had drifted inside it once: 1.2 MB of tracked content installed as
536 MB, three copies of one virtualenv, one of them in a git worktree nobody had
removed.

The same rule covers the session folder, and there the stake is not disk. Binding this
repo's own directory to a tenant puts `.cirro/` in the plugin root, so the tenant URL,
the username and the SDK's token cache are copied into the plugin cache on every
install. The token file happened to be empty when this was found; it is not always.
Bind a scratch directory for testing, not the checkout.

**One data directory, named identically in every context.** `runner.plugin_data()`
and the shell's `resolve_plugin_data` locate the runtime, and `tenants_root()` hangs
off the same answer. Neither consults `CLAUDE_PLUGIN_DATA`, though the host sets it,
and that is the whole point: the host sets it *for the MCP server and for nothing
else*. `scripts/setup`, `bin/cirro-agent` and the hook all run over Bash, where it is
absent. While it was honoured the plugin resolved to two different directories at
once -- setup built the runtime under the XDG default, the server looked for it under
the host's path, found nothing, fell back to an interpreter that cannot import `mcp`
and died, so a fresh install that had just run setup *correctly* reported nothing but
"MCP server failed to connect". The credential half had not been reached yet and was
worse: a login through `login_start` would have written the tenant folder under the
host's path, where no read over Bash would ever look, and every one of them would
have said no tenant was signed in. `CIRRO_AGENT_PLUGIN_DATA` stays as the override
precisely because nothing sets it behind your back -- it is set for every context at
once or for none. `test_runner.py` now asks the two resolvers *in the environments
they are really called in*; the test it replaces asked both in one environment and
therefore could not fail.

**The missing-runtime fallback cannot be `mcp_server` under the ambient interpreter.**
That module imports the third-party `mcp` package at module scope, so it dies before
a single tool exists and the host reports only a connection failure -- the exact
outcome the fallback was written to prevent, and what every install looks like between
`claude plugin install` and `/cirro:setup`. `mcp_unavailable` stands in instead:
stdlib only, so any `python3` can run it, advertising the same eight tools and
answering every call with the RUNTIME_NOT_INSTALLED payload -- carrying `looked_in`,
which is the one fact separating "never installed" from "installed somewhere else".
Its tool list is duplicated from `mcp_server` because importing that module is what it
exists to avoid; `test_mcp_unavailable.py` pins the two together and drives
`scripts/mcp-server` end to end against a data directory with no virtualenv in it.

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
`mutations._target_folders` and `_tag_values` are the one implementation of both halves
of that rule; every op that writes a tag list goes through them. `folder` means the
Cirro folder in every operation -- `upload_dataset`'s local directory is `source_dir`,
because when both were called `folder` one of them was a footgun.

**A form's fields are not just its `properties`.** Conditional fields live inside
`dependencies.<selector>.oneOf[].properties`, and `dependencies` keys are *selector field
names*, not a nesting level. Any analysis that walks only `properties` will report live,
correctly-mapped fields as dangling — this has already produced one wrong answer to a
user. Use `formcheck.walk_form()`, which is the one implementation of that walk; it
yields a record per field carrying its path, its owner, whether the owner requires it
and whether it exists only inside a branch, covers every wrapper in `formcheck.WRAPPERS`
(`dependencies`, `oneOf`, `allOf`, `anyOf`, `then`, `else`) and holds the
selector-is-transparent rule in one place. `iter_fields()` is its `(path, field)` view
and `form_paths()` its path-only one -- views, never re-traversals. There were two walks
once, and they had already drifted apart over `then`/`else` -- which is how the wrong
answer above got out.

A field's `required` counts only the `required` arrays reached without passing through a
wrapper. A requirement stated inside a branch binds only when that branch is selected,
so reporting it flatly would demand a parameter nobody has to supply -- and both
`missing_required` warnings and `process_params` read that flag.

**Configuration assets may be authored, not only generated.** `assets_dir` is a
first-class alternative to `design_spec` on `create_process` and
`upload_process_config`, because a form with two independent selectors, a custom widget
or a hand-tuned input map cannot survive a round trip through a spec. Authored files go
up byte-for-byte: `_authored_assets` reads them, `formcheck` validates them, and nothing
rewrites or reformats them. Nothing is staged either -- the files are already where the
person put them, so `stage_dir` is None and apply does not write over their source.
`_one_asset_source` enforces exactly one of the two on upload.

**The form validator is calibrated, not invented.** Its three tiers come from measuring
the 213 forms and 72 input maps in Cirro-pipelines: 2 errors and 5 errors respectively,
each a genuine defect. If a change to `formcheck.py` starts flagging production
configuration, the validator is wrong, not the configuration. `folder` is documented by
Cirro but used by none of the 213, so it warns rather than errors.

**Every op family registers itself into `mutations.OPS`.** `mutations.py` holds the
shared core -- `Draft`, `Op`, the risk classes, the resolvers, `OPS` and `get_op` -- and
nothing else. The operations live in `analysis_ops`, `dataset_ops`, `project_ops`,
`share_ops`, `chain_ops` and `pipeline_ops`, each ending in `mutations.OPS.update(...)`.
The imports sit at the bottom of `mutations.py`, after `OPS` and `get_op` are defined,
and the registration sits on the module side, so any of them can be imported first.
Putting the registration on the `mutations` side reintroduces a circular-import failure
for whoever imports an op module before `mutations`. A new op goes in the family it
belongs to, not in `mutations.py`.

**Cirro reports spend, just not in advance.** `metrics.get_for_project` returns two
series in one call -- dollars by AWS service by month, and storage by tier by day -- and
`get_all_metrics` returns them for every visible project in a single request, the one
cross-project read that does not fan out. A `MetricRecord` carries `unit`, `date` and a
`services` map and **no scalar value**; reading a `value` attribute it never had made
project storage null for every project, undetected because no fixture served the
endpoint. Storage arrives in GB, so `storage_amount` with `storage_unit` is the figure
to read and `storage_bytes` states the 2**30 convention it derives from. None of this is
a forecast, so plans still carry no estimate -- say "nothing prices a run before it
happens", never "there is no pricing API".

`execution.get_cost` (cirro >=1.13.1) is the same statement one scale down: what one
analysis has already burned, per task, with an `is_estimate` flag saying whether it is
modelled from instance pricing or settled. It is never snapshotted -- a running run's
cost grows as its tasks finish, so a cached figure answers "what did this cost" with a
number from earlier in the same run. A dataset no analysis produced has no cost record
and dev refuses it with **400**, not the 404 the shape suggests; both are caught in
`dataset_cost` and become `WRONG_EXECUTOR`, because untyped either one reaches the
reader as a retryable UPSTREAM_ERROR telling them to check Cirro's service status.
Shaping the absence into a zero total is the other wrong answer: it reads as a run
that was free.

**There is no safe way to delete one reference.** The generated
`delete_project_reference` route is `DELETE /projects/{project_id}/references` and takes
the project id alone -- nothing in it names the reference to remove, though its own
docstring says "Deletes a reference". 1.6.0 is the newest published `cirro-api-client`
and it bundles no spec to check against, so whether the server takes a parameter the
generator dropped cannot be settled from here. Built on that route as it stands, a
`delete_reference` operation would be a destructive op whose blast radius is every
reference in the project. It stays unimplemented, and the README says to use the web UI.

**Never call `DataPortal()` bare.** With no cached token it enters a device-code login
and blocks until the code expires. Build auth explicitly via `worker/portal.py`.

**The SDK's token refresh is not safe across processes.** `DeviceCodeAuth` guards it
with a `threading.Lock` built per client, and every operation here is a fresh
subprocess, so two workers on one session folder refresh in the same moment. The
losing call does not merely fail: `_refresh_access_token` answers *any* `ClientError`
-- a throttle above all -- by calling `_clear_token_info()`, which deletes the shared
credential for both workers and every later one, unrecoverably. `renew_under_lock`
takes an exclusive `flock` on `<session>/auth.lock`, re-reads the store under it so N
workers waking together cost one Cognito call rather than N, and renews within
`REFRESH_MARGIN` of expiry rather than waiting for the SDK to notice. It is held
across the renewal alone, never the operation: blocking a read behind an unrelated
four-hour upload would be worse than the bug it fixes. A token that expires *during* a
transfer is still refreshed lazily, outside the lock. The SDK internals this drives
are pinned in `test_runtime_contract.py`.

**`get_all_records` truncates silently** and overshoots its own ceiling. Paginate
locally so truncation is detectable and `scan_complete` can be honest.

**A chain widens the approval, never the authorization.** `chain` runs several
build-up ops under one approval. An argument a later step cannot know yet is a
reference to an earlier step's recorded output, resolved by the server -- never a
wildcard, and never a value the caller supplies at apply time. `Op.outputs` declares
what is referenceable and a test asserts each declaration is really returned.
Destructive ops are refused inside a chain, so one "yes" can never authorize a
deletion nobody was looking at. Retries are a policy the person approves once:
bounded attempts, and a `may_change` allowlist that is enforced by refusing a diff
outside it rather than by rendering it and hoping. A step being re-run sheds the
`resolved_args` the first attempt recorded and is planned again when it runs: apply
prefers `resolved_args` over `args`, so carrying them forward rendered a changed field
in the retry's diff and then executed the old value. A chain drops each step's declared
cache scopes as that step finishes rather than when the chain ends, because a later
step plans against listings an earlier one has already changed -- deferring it is why
`create_process` -> `upload_process_config` refused `NOT_FOUND` on the process the step
before it had just created. `SnapshotCache.invalidate_entries` is the one place a
declared scope becomes a dropped snapshot; `do_apply` and the chain executor share it.

**The plan expiry only guards intent; accuracy is guarded by re-planning.** `apply`
re-plans from the stored `request_args` and refuses with `PLAN_STALE` if the same
request now resolves differently or renders different text. That is why `TTL_SECONDS`
is an hour rather than ten minutes -- a clock cannot tell whether the figures a person
read are still true, and this can. `request_args` is inside `DIGEST_FIELDS`, so it is
covered by the same tamper check as everything else.

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

Two opt-in live checks exist and are skipped without their variables.
`test_live_reads.py` (`CIRRO_AGENT_E2E=1`) runs every read against the tenant the
working directory is bound to and changes nothing; run it after touching the read
layer. `test_live_writes.py` adds `CIRRO_AGENT_E2E_WRITE_PROJECT` (and optionally
`..._WRITE_DATASET`) and performs two reversible tag writes through plan and apply.
Never point it at anything that is not disposable, and never choose the project
yourself -- the person names it.

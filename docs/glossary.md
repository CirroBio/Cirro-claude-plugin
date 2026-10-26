# Words you will see

**Tenant.** Your organization's Cirro site, such as `app.cirro.bio`. The Cirro
documentation also calls it an organization or a Cirro instance.

**Hostname.** A tenant's address without `https://`, such as `app.cirro.bio`. Claude
asks for it when you sign in.

**Login folder (`.cirro/`).** Holds your sign-in to one tenant, a cache, plans waiting
for your approval, and a record of every change made from it. Normally there is one per
tenant, kept outside your projects. Claude and the plugin's messages call it the
*session folder*. See [Signing in](signing-in.md).

**Runtime.** The private copy of Python, with the Cirro SDK, that `/cirro:setup` builds.

**Session.** One Claude Code conversation, from starting Claude Code until you quit it.
Grants last for one session.

**Skill.** Instructions Claude loads by itself for a kind of task, such as running an
analysis. You never need to name one.

**Plugin server (MCP server).** The part of the plugin that gives Claude its sign-in and
change tools. `/mcp` in Claude Code shows whether it is connected.

**`cirro-agent read`.** The read-only command Claude uses to look things up.

**Plan.** A description of exactly what a change would do, worked out without changing
anything. See [Making changes](changes.md).

**Approval prompt.** Claude Code's prompt to apply one plan. Only you can answer it.

**Operation.** The kind of change a plan makes, such as `update_dataset` or
`run_analysis`. [Making changes](changes.md#what-claude-can-change) lists them all.

**Chain.** Several changes approved in one prompt.

**Grant.** Approval given in advance for the kinds of change you name, in one project,
for the rest of the session. See [Approving a kind of change](grants.md).

**Command check.** The plugin's check on the shell commands Claude runs, which stops
them going around the approval prompt. See [Locking it down](locking-down.md).

**Pipeline, process.** A workflow registered in Cirro. Cirro's API calls pipelines
*processes*, so you will see both words.

**Cirro portal.** The Cirro web app you use in a browser.

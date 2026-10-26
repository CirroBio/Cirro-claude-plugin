# Getting started

## Before you start

You need:

- **A Cirro account**, and the address you sign in to Cirro at, such as `app.cirro.bio`
  or your institution's own. Each Cirro site is a *tenant*, and its address is the
  *hostname* Claude will ask for.
- **[Claude Code](https://claude.com/claude-code)**, signed in, on **macOS or Linux**.
  The plugin works in Claude Code in a terminal and in the Claude Code desktop app; the
  install commands below run in a terminal.
- **[`uv`](https://docs.astral.sh/uv/)**, which builds the plugin's own copy of Python.
  Check with `uv --version`. If it is missing, `brew install uv` or
  `curl -LsSf https://astral.sh/uv/install.sh | sh` installs it. You also need
  `python3` on your `PATH`, which macOS and most Linux systems already have.

## 1. Install the plugin

In a terminal:

```bash
claude plugin marketplace add CirroBio/Cirro-claude-plugin
claude plugin install cirro@cirro
```

Then start a new Claude Code session. A session that was already open does not see the
plugin.

## 2. Install the runtime

In Claude Code, once:

```
/cirro:setup
```

This builds the isolated Python environment the plugin runs in, with the Cirro SDK in
it. It takes up to two minutes the first time, and nothing runs it for you.

When it finishes, reconnect the plugin's server: run `/mcp`, choose the **cirro**
server and reconnect it, or restart Claude Code. Until you do, the Cirro tools keep saying the
runtime is missing, because the server started before the runtime existed.

## 3. Sign in

Ask Claude to connect to Cirro. It asks for your Cirro address; paste the address from
your browser's address bar if that is easier, since only the hostname is kept. Claude
then gives you a sign-in link. Open it in your browser and approve. Nothing opens a
browser for you.

The link appears in the conversation. When Claude Code runs in a terminal, the plugin
also prints it there directly.

You sign in once per tenant, and every directory on the machine can use that login.
[Signing in](signing-in.md) explains where the login is kept and when you would want a
separate one for a single directory.

## 4. Ask

A safe first question is *"List my Cirro projects."* Then ask for what you want:

> *"What failed in Immuno-Oncology Core this week?"*
>
> *"Why did that run fail?"*
>
> *"Move those three datasets into the QC folder."*

## Two kinds of prompts

You will see two kinds of prompt from Claude Code, and they mean different things.

| You see | What it is | What to do |
|---|---|---|
| Claude Code asking to run `cirro-agent read …`, or to use a Cirro sign-in tool | Claude Code's ordinary permission to use a tool | Allow it, and choose not to be asked again if you like. These can only read Cirro or sign you in. |
| Claude Code asking to approve `apply`, with a plan above it in the conversation | The plugin asking you to approve one change in Cirro | Read the plan, then approve or decline. This one cannot be turned off. |

To stop the routine prompts for reading and signing in, add this to your Claude Code
settings: `~/.claude/settings.json` for every project, or `.claude/settings.json`
inside one project.

```json
{
  "permissions": {
    "allow": [
      "Bash(cirro-agent read:*)",
      "mcp__plugin_cirro_cirro__whoami",
      "mcp__plugin_cirro_cirro__login_start",
      "mcp__plugin_cirro_cirro__login_wait",
      "mcp__plugin_cirro_cirro__plan"
    ]
  }
}
```

None of these can change anything in Cirro. [Locking it down](locking-down.md) has the
matching rules for blocking the ways around the plugin.

## What Claude sees

What Claude reads from Cirro to answer you, such as dataset names, sample metadata,
sheet rows and run logs, becomes part of the conversation and is sent to the Claude
model like anything you type. Your sign-in token is not. Check your organization's
policy before using the plugin on restricted data.

## If something seems off

```
/cirro:doctor
```

reports which tenant the directory you are working in uses, whether the runtime is
installed, and who you are signed in as, checked with a live call. It stops at the
first problem and says how to fix it. [Troubleshooting](troubleshooting.md) covers the
messages you are most likely to meet.

## Updating

In a terminal:

```bash
claude plugin marketplace update cirro
claude plugin update cirro@cirro
```

Then start a new Claude Code session and run `/cirro:setup` again, followed by `/mcp`
to reconnect. A release can change the versions the runtime pins, and setup changes
nothing when they have not changed. Your logins live outside the plugin and survive
updates.

## Uninstalling

```bash
claude plugin uninstall cirro@cirro
```

The runtime and your logins live outside the plugin, in
`~/.local/share/cirro-agent/` (or under `$XDG_DATA_HOME` if you set it). Delete that
folder to remove both. On macOS the login token itself is kept in your login Keychain;
`security delete-generic-password -s cirro-client` removes it; run it once for each
login you had.

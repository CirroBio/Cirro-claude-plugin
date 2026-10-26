# Locking it down

This page is for administrators and anyone who wants to know exactly where the edges
are. You do not need it to use the plugin.

## What the plugin already blocks

Once a session has used Cirro, the plugin checks every shell command Claude runs, and
refuses ones that would go around the approval prompt:

- running Python that drives the Cirro SDK, as a one-liner, a script, or from a shell
  script
- starting the plugin's own worker or MCP server by hand (`python -m cirro_agent.worker`,
  `scripts/mcp-server`) to apply a plan without the approval prompt
- the standalone `cirro` command-line tool, apart from its list commands
- moving data in or out of Cirro storage with the AWS CLI or `s5cmd`
- handing a `.cirro/` folder, or the credential files in one, to anything that reads
  them

It finds the program wherever a command puts it: behind `env`, `timeout` or `sudo`,
inside a `for` loop or a subshell, in a `bash -lc` string, or after a `cd`.

Be clear about the limit. This check reads commands, and the scripts they run, as text.
It makes accidental bypass unlikely and deliberate bypass obvious; it is not a sandbox.
An encoded payload, or a language it does not recognize, can get past it.

The plugin never blocks or prompts on editing a file. Writing Python that uses the Cirro
SDK is ordinary work; only running it through Claude is checked.

## When the check applies

The check switches on the first time a session uses a Cirro skill, one of the plugin's
tools, or `cirro-agent`, and stays on for the rest of that session. Before that there is
no approval to go around, so a project that merely depends on the Cirro SDK is not
affected by having the plugin installed.

Some things never wait for that:

- `apply` always shows the plan and asks.
- A command that names Cirro's credential storage or the machine-wide `~/.cirro` is
  always refused, even one that only mentions it, such as a commit message.
- Handing a `.cirro/` folder or its credential files to anything that reads them is
  always refused.

If a session cannot be identified, the checks apply as though it had used Cirro.

## A hard boundary in your own settings

A plugin cannot add standing allow or deny rules to your settings; it can only
pre-approve tools while its own commands run. For a hard boundary, add rules to your
Claude Code settings: `~/.claude/settings.json` for every project, or
`.claude/settings.json` inside one project.

```json
{
  "permissions": {
    "deny": [
      "Bash(cirro upload:*)",
      "Bash(cirro upload-reference:*)",
      "Bash(cirro resume-upload:*)",
      "Bash(cirro download:*)",
      "Bash(cirro configure:*)",
      "Bash(cirro-cli:*)"
    ],
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

Those are the names Claude Code gives the plugin's tools when it is installed from the
marketplace. The rules govern what Claude runs, not what you run yourself in your own
terminal.

Allowing `cirro-agent read` outright is safe: that command has no ability to change
anything, and a test fails if it ever gains one. `apply` is missing from the allow list
on purpose, and could not be added anyway: it asks regardless. The same is true of
`grant`. `apply_granted` needs no rule: the plugin lets it through itself, and the
plugin's server refuses any plan that no grant you approved covers.

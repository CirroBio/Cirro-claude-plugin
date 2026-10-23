# Locking it down

## What the plugin already blocks

The plugin blocks the obvious ways around the approval gate: driving the Cirro SDK
directly from a Python one-liner, running a script or shell script that drives it,
starting the plugin's own worker or MCP server by hand (`python -m cirro_agent.worker`,
`scripts/mcp-server`) to apply a plan without the approval prompt, using the standalone
`cirro` CLI's upload or configure commands, or writing to Cirro storage with the AWS CLI
or `s5cmd`. It finds the interpreter wherever a command puts it — behind `env`,
`timeout` or `sudo` options, inside a `for` loop or a subshell, in a `bash -lc` string,
after a `cd` — and it refuses a command that hands a `.cirro/` folder or the credential
files in one to anything that reads them.

Be clear about the limit, though — that check reads commands, and the scripts they
launch, as text. It makes accidental bypass impossible and deliberate bypass obvious.
It is not a sandbox.

Editing files is never blocked and never prompts. Writing Python that uses the Cirro SDK
is ordinary work; only running it is gated.

## When the check applies

It does not apply to a session that has not used the plugin. It arms the first time a
session invokes a Cirro skill, one of the MCP tools, or `cirro-agent`, and stays on for
the rest of that session. Until then there is no approval to bypass, so a project that
merely depends on the Cirro SDK is unaffected by having this plugin installed.

Two things never wait to be armed: `apply` always shows the plan and asks, and a command
that reads Cirro credential storage or the machine-wide `~/.cirro` is always refused.
If a session cannot be identified, the checks apply as though it were armed.

## A hard boundary in your own settings

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
      "mcp__plugin_cirro_cirro__whoami",
      "mcp__plugin_cirro_cirro__login_start",
      "mcp__plugin_cirro_cirro__login_wait",
      "mcp__plugin_cirro_cirro__plan"
    ]
  }
}
```

Those are the names Claude Code gives the plugin's tools when it is installed from the
marketplace.

Allowing `cirro-agent read` outright is safe — that command has no ability to change
anything, and a test fails if it ever gains one. `apply` is missing from the allow list
on purpose, and could not be added anyway: it prompts regardless. The same is true of
`grant`. `apply_granted` needs no rule: the plugin's own hook lets it through, and the
server refuses any plan that no grant you approved covers.

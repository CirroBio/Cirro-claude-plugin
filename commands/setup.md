---
description: Install the pinned Python runtime the Cirro plugin needs
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/setup:*)
---

Install the Cirro plugin's runtime by running:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/setup
```

This builds an isolated Python 3.12 environment holding the `cirro` SDK, in the
plugin's own data directory. It takes up to two minutes the first time because it
downloads an interpreter and compiled dependencies; afterwards it is a no-op.

It requires `uv` on PATH. If `uv` is missing, the script says how to install it and
stops — do not install it on the user's behalf.

When it finishes, report the `cirro` version it installed. Then tell the user that the
next step is a session folder for whichever directory they want to work in, which the
**cirro-session** skill handles and which needs their Cirro hostname (something like
`app.cirro.bio`).

If it fails, relay the error and its stated remedy verbatim rather than trying
alternative install routes.

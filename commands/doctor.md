---
description: Diagnose the Cirro plugin's session, runtime and authentication
allowed-tools: Bash(cirro-agent read doctor:*), Bash(cirro-agent read whoami:*)
---

Diagnose this directory's Cirro setup:

```bash
cirro-agent read doctor
```

It checks, in order, and reports a remedy for whichever fails first:

- **session_folder** — is there a `.cirro/` for this directory, is it gitignored, and
  is it accidentally tracked by git
- **session_config** — which Cirro tenant this folder is bound to
- **runtime** — the pinned interpreter and whether it was installed
- **authentication** — the logged-in user, proved with a live API call
- **sdk** — the resolved `cirro` version, and whether it is an editable local checkout
  rather than the pinned release

Summarize the result for the user in plain terms: which tenant, which identity, and
whether anything needs doing. Work the failures in the order reported — a missing
runtime makes the authentication check meaningless.

If `sdk` reports `cirro_is_editable_checkout: true`, mention it. That means a local SDK
checkout is in use instead of the pinned version, which is intended during development
but is worth stating so a stale checkout is not mistaken for the release.

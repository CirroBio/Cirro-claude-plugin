---
name: cirro-session
description: Set up a Cirro session folder and authenticate, or repair a broken one. Use when the user asks to connect to Cirro, log in, switch tenants, or check who they are logged in as — and whenever any Cirro operation returns no_session_folder, not_authenticated, AUTH_REQUIRED, AUTH_EXPIRED, TENANT_MISMATCH, or RUNTIME_NOT_INSTALLED.
---

# Connect to Cirro

Every Cirro operation runs against a **session folder**: a `.cirro/` directory in the
user's working directory that holds that directory's credential, cache and audit log.
It is deliberately separate from the machine-wide `~/.cirro`, which the plugin never
touches.

## Order of operations

### 1. Is the runtime installed?

```bash
cirro-agent read doctor
```

`doctor` reports the session folder, the tenant, the runtime, and the authenticated
identity, each with its own remedy. Work down the failures it reports; do not guess.

If it reports `RUNTIME_NOT_INSTALLED`, the user runs:

```bash
/cirro:setup
```

That builds a pinned Python environment and takes up to two minutes on first use.
It is not run automatically, so tell the user it is needed rather than waiting.

### 2. Is there a session folder?

If `doctor` reports `no_session_folder`, you need the Cirro hostname. **Ask the user
for it — never guess a tenant.** It looks like `app.cirro.bio` or
`cirro.your-institution.org`.

Then call the `session_init` MCP tool with that `base_url`. Run it from the directory
the user is working in; the folder is created there.

### 3. Log in

Call `login_start`. One of two things comes back:

- `status: "already_authenticated"` — you are done. It reports the username and
  proves it with a live API call, so trust it.
- `status: "pending"` — it returns `auth_message_markdown`. **Relay that verbatim to
  the user in your own message.** It contains a URL and a short code they must open
  in a browser. Nothing proceeds until they do.

Then call `login_wait`. It returns `ok`, `pending`, or a typed failure.

**`pending` is not an error.** It means the person has not finished in the browser
yet. Say so, offer the link again, and call `login_wait` once more. Never treat it as
a failure and never retry `login_start` while a login is pending — that invalidates
the code the user is looking at.

### 4. Confirm

```bash
cirro-agent read whoami
```

Report the username, tenant and session folder back to the user. From here on, reads
work through `cirro-agent read` and writes through `plan` / `apply`.

## Recovery

| What you saw | What it means | What to do |
|---|---|---|
| `AUTH_EXPIRED` | The SDK discards a cached token once its refresh token is within 12h of expiring | `login_start`, relay the link, `login_wait` |
| `TENANT_MISMATCH` | `CIRRO_BASE_URL` is set to a different host than this folder is bound to, or a plan was made elsewhere | One folder serves one tenant. Either unset the variable or work in a directory with its own session folder |
| `NETWORK_UNAVAILABLE` | DNS or TCP to the Cirro host failed | A VPN or hostname problem. The message distinguishes "cannot resolve" from "cannot connect" — pass that on |
| `no_session_folder` | You are running outside any directory that has one | `session_init` here, or change to the right directory |

## Multiple tenants

The plugin refuses to guess. Two tenants means two directories, each with its own
`.cirro/`. Do not attempt to repoint an existing folder at a different host.

## Logging out

`logout` removes this folder's credential. On macOS the token itself lives in the
login Keychain, so `logout` reports the `security delete-generic-password` command for
the user to run if they want it gone entirely — pass that on rather than running it.

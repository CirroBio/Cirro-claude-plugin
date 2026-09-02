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

Getting the login link in front of the user quickly is the whole job here. Every extra
tool call before it is an approval prompt they have to answer first, so do not probe
for problems that the login itself will report.

### 1. Ask for the hostname

You need the Cirro hostname, and you must **ask for it — never guess a tenant.** It
looks like `app.cirro.bio` or `cirro.your-institution.org`. If the directory already
has a session folder, it is already bound to one; `login_start` will use it and you do
not need to ask at all.

### 2. Call `login_start`

```
login_start(base_url="app.cirro.bio")
```

It creates the session folder if there is none, so `session_init` is not a separate
step in this flow. Do not run `cirro-agent read doctor` first: `login_start` returns
the same typed errors, and a diagnostic pass before the thing you are diagnosing just
puts another approval prompt between the user and their link.

One of three things comes back:

- `status: "pending"` — a login is under way. `verification_url` is a one-click link
  with the code already in it. **Put it in your very next message, before anything
  else.** Nothing proceeds until the person opens it.

  The tool also prints the link to the user's terminal, and `printed_to_terminal`
  says whether that worked. Treat it as a courtesy — a desktop host has no terminal,
  and a full-screen interface may redraw over it — and your own message as the
  delivery. Nothing opens a browser for them; they follow the link themselves.
- `status: "already_authenticated"` — **you are done. Stop here.** It reports the
  username and proves it with a live API call, so trust it. Do not go on to
  `login_wait`, do not offer a link, and do not announce a login the person did not
  need to do: say who they are signed in as and get on with what they actually asked
  for. This is the common case in any directory that has been used before.
- a typed error — act on its `user_action`. `RUNTIME_NOT_INSTALLED` means the user runs
  `/cirro:setup`, which takes up to two minutes on first use; tell them rather than
  waiting. Only run `cirro-agent read doctor` when an error is ambiguous enough to
  need a full diagnostic.

### 3. Wait — only if a login actually started

Reached only from `pending`. Call `login_wait` **after** the message carrying the link, never bundled ahead of it.
It returns as soon as the sign-in lands, so a prompt reply is not slowed down by
the timeout — but it will sit for the full `timeout_s` if the person steps away.

It returns `ok`, `pending`, or a typed failure. **`pending` is not an error.** It means
the person has not finished signing in yet. Say so, offer the link again, and call
`login_wait` once more. Never treat it as a failure and never retry `login_start`
while a login is pending — that invalidates the code the user is looking at.

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
| `no_session_folder` | You are running outside any directory that has one | `login_start` with the hostname creates it, or change to the right directory |
| `TENANT_MISMATCH` from `login_start` | You passed a hostname this folder is not bound to | One folder serves one tenant. Drop the argument to use the bound one, or move to a directory bound to the host you meant |

## Multiple tenants

The plugin refuses to guess. Two tenants means two directories, each with its own
`.cirro/`. Do not attempt to repoint an existing folder at a different host.

## Logging out

`logout` removes this folder's credential. On macOS the token itself lives in the
login Keychain, so `logout` reports the `security delete-generic-password` command for
the user to run if they want it gone entirely — pass that on rather than running it.

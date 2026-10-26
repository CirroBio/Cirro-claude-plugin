# Signing in

You sign in by asking Claude to connect to Cirro; [Getting started](getting-started.md)
walks through the first time. This page covers what happens after that.

## Who am I signed in as?

Ask Claude *"Who am I signed in to Cirro as?"* It answers with the tenant and your
account, checked with a live call. If the access token has expired it is renewed
without asking you. If the login itself has lapsed, Claude gives you a new sign-in
link instead.

## Where your login lives

Your login is kept in a *login folder* named `.cirro/`. It holds your sign-in, a cache,
plans waiting for your approval, and a record of every change made from it (in
`.cirro/runs/`). It ignores itself in git, and it is separate from the `~/.cirro` that
the standalone `cirro` command-line tool uses; the plugin never touches that one.

**Signed in for the tenant.** This is what you get when you ask Claude to sign in. The
folder is created outside all of your projects, one per Cirro hostname, at
`~/.local/share/cirro-agent/tenants/<hostname>/.cirro` (under `$XDG_DATA_HOME` if you
set it). Every directory with no login of its own uses it, so you sign in once and
every project on the machine can reach Cirro. It survives plugin updates.

**Signed in for one directory.** A `.cirro/` inside a working directory answers for that
directory and the folders beneath it, ahead of your tenant login. Use one when a
particular project needs a different tenant or a different account from your usual one.
Ask Claude to sign in *for this directory*.

A folder serves one Cirro tenant. To find the login for a request, the plugin starts in
the directory Claude is working in and looks upward for the nearest `.cirro/`, stopping
at the top of the git repository or Claude Code project. If it finds none, your tenant
login answers.

## Signed in to more than one tenant

If you are signed in to two tenants and the directory you are working in has no login
of its own, Claude does not guess: it names both and asks which you mean. Tell it
(*"use app.cirro.bio"*), or remove the login you no longer need.

To see every tenant you are signed in to, ask Claude *"Which Cirro tenants am I signed
in to?"* It lists each one, where its folder is, whether it still holds a credential,
when it was last used, and which one a directory without its own login will use. This
reads only your computer, so it still answers when a login has expired.

## Signing out and removing a login

**Signing out** removes the credential and keeps the folder, with its cache and its
record of past changes. Ask Claude to sign out.

**Removing a login** deletes the tenant's folder outright: the credential, the cache
and the record of every change made from it. Ask Claude to remove it; it asks you first
and tells you what was in the folder. Logins for a single directory are not removed
this way; delete their `.cirro/` folder yourself.

On macOS the token itself is stored in your login Keychain, and neither of these erases
it. To remove it as well, run `security delete-generic-password -s cirro-client`, once
for each login you had.

## If you sign in for one directory

**Choose the directory you mean.** A `.cirro/` answers for every folder beneath it that
is not inside a git repository or Claude Code project of its own. One placed high up,
such as in `~/projects`, can therefore quietly bind loose folders beneath it to that
tenant. Signing in for the tenant avoids this. A `.cirro/` directly in your home
directory is refused, since that is where the standalone `cirro` tool keeps its own.

**Not inside a folder you will delete or copy.** Deleting a git worktree deletes the
login in it, and a copied folder takes the login with it: your tenant, your username
and, on Linux, the token itself in plain text.

**A `.cirro/` that git tracks is refused.** It arrived with the repository rather than
from a sign-in on your machine, and the nearest folder decides which host every request
goes to. If it is yours, untrack it with `git rm -r --cached .cirro`; its own
`.gitignore` keeps it out after that. A folder naming anything but a plain hostname is
refused too.

**Credentials in your environment.** A Cirro credential in your environment
(`CIRRO_ACCESS_TOKEN`, or `CIRRO_CLIENT_ID` with `CIRRO_CLIENT_SECRET`) is used only
when `CIRRO_BASE_URL` names the host it is for.

## Two sessions at once

Two Claude Code sessions can use the same login at once. If both find an expired token
in the same moment, one renews it and the other waits for the result. A token that
expires in the middle of a long upload is renewed separately, so a transfer running for
hours beside another busy session is the one case worth staggering.

That renewal writes the new token to your keychain. On macOS, if the login Keychain has
locked in the meantime, the operation stops with `CREDENTIAL_STORE_UNAVAILABLE`. That is
not a Cirro outage: unlock the login Keychain and run it again.

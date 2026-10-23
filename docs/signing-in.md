# Signing in

## Where your credentials live

A `.cirro/` folder holds a login, a cache, and a log of every change made. It ignores
itself in git, and it is separate from the `~/.cirro` used by the standalone `cirro`
CLI — the plugin never touches that.

There are two places it can go, and the difference is worth a minute.

**Signed in for the tenant.** Ask Claude to sign in and this is what you want. The
folder is created outside every checkout, one per Cirro hostname, and any directory
with nothing bound nearer resolves to it — so you sign in once and every project on
the machine can read Cirro. It survives plugin installs and updates, which a folder
inside a checkout does not.

**Signed in for one directory.** A `.cirro/` created inside a working directory answers
for that directory and everything under it, ahead of any tenant login. Use it when a
particular project genuinely needs a different tenant or a different account from your
usual one.

One folder, one Cirro tenant, either way. If two tenants are signed in and the
directory you are working in has nothing bound nearer, Claude will not pick between
them — it names both and asks. You can always say which to use, without moving:
`cirro-agent read ... --dir <path>` does the same on the command line.

A `.cirro/` that is tracked by git is refused: it arrived with the repository rather
than from a sign-in on your machine, and the nearest folder decides which host every
request goes to. If it is yours, untrack it with `git rm -r --cached .cirro` — its own
`.gitignore` keeps it out after that. A folder naming anything but a plain hostname is
refused too. And a Cirro credential in your environment (`CIRRO_ACCESS_TOKEN`, or
`CIRRO_CLIENT_ID` with `CIRRO_CLIENT_SECRET`) is used only when `CIRRO_BASE_URL` says
which host it is for.

## Seeing and removing what is signed in

```bash
cirro-agent read tenants
```

Lists every tenant you are signed in to, where its folder is, whether it still holds a
credential, when it was last used, and how many plans and change records it has kept —
and says which one an unbound directory will resolve to, or that there are too many to
choose from. It reads the filesystem and nothing else, so it still answers when a login
has expired.

To get rid of one, ask Claude to remove it. That deletes the folder outright — the
credential, the cache, and the record of every change made from it — so it asks first
and tells you what was in it. Removing the credential but keeping the folder and its
history is what signing out does instead. Folders bound to a single directory are not
listed and are not removable this way; delete those yourself.

## If you sign in for a directory

Two things to avoid, both of which have bitten people.

**Not a parent of your work.** The search walks up from wherever Claude is working,
stopping at the project directory or the enclosing git checkout — and at your home
directory when it is neither. A `.cirro/` high up therefore captures everything
beneath it, so signing in at `~` or `~/projects` quietly binds every project under it
to that one tenant. This is the usual cause of a session talking to the wrong Cirro,
and signing in for the tenant instead avoids it entirely.

**Not inside anything that gets copied or replaced.** Installing or updating a plugin
copies its whole directory, and a signed-in `.cirro/` goes with it — tenant, username
and, where your OS keychain is unavailable, the token itself in plain text. The same
applies to a git worktree you intend to delete.

## Sharing a login with another session

Two Claude sessions can work against the same signed-in directory at once. If both find
an expired token in the same moment, one renews it and the other waits and picks up the
result, rather than both asking Cirro at once — which used to risk being throttled, and
a throttled refresh discards the stored login. A token that expires in the middle of a
long upload is still renewed the old way, so a transfer running for hours alongside
another session remains the one case worth staggering.

That renewal rewrites the token in your OS keychain, so an operation long enough to
outlive its token — the longest chains, and uploads — fails if the keychain has
re-locked meanwhile. That reports as `CREDENTIAL_STORE_UNAVAILABLE` naming the OS status
code, and it is not a Cirro outage: unlock the login keychain and run it again.

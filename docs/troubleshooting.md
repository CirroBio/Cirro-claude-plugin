# Troubleshooting

Start with `/cirro:doctor`. It checks the login folder, the tenant, the runtime and your
sign-in in that order, and stops at the first problem with a fix. The table below
covers what you are most likely to see. The codes in capitals are what the plugin
reports; Claude usually explains them in its own words.

## Installing and setting up

| You see | What it means | What to do |
|---|---|---|
| `uv is required and was not found` | `uv` is not installed | Install it ([Getting started](getting-started.md#before-you-start)), open a new terminal, and run `/cirro:setup` again |
| Every Cirro tool says the runtime is not installed (`RUNTIME_NOT_INSTALLED`) | `/cirro:setup` has not run | Run `/cirro:setup` |
| *The Cirro runtime is installed now, but this server started before it was* | The plugin's server started before setup finished | Run `/mcp` and reconnect the cirro server, or restart Claude Code |
| `/mcp` shows the cirro server failed to connect | Usually no `python3` on your `PATH` | Install Python 3, then restart Claude Code |
| `/cirro:setup` is not recognized | The plugin is not installed, or this session started before it was | Check `claude plugin list` in a terminal, then start a new session |

## Signing in

| You see | What it means | What to do |
|---|---|---|
| Not signed in (`NO_SESSION_FOLDER`) | No login was found for the directory you are working in | Ask Claude to connect to Cirro |
| `AUTH_REQUIRED` or `AUTH_EXPIRED` | The login has lapsed | Ask Claude to sign in again |
| Claude names two tenants and asks which | You are signed in to two, and this directory has no login of its own | Name one, or remove the login you no longer need |
| Claude is talking to the wrong Cirro | A `.cirro/` in a folder above you is answering first | Run `/cirro:doctor` to see which folder; delete it if you did not mean it |
| `TENANT_MISMATCH` | `CIRRO_BASE_URL` in your environment names a different Cirro, or a plan was made against a different tenant | Unset `CIRRO_BASE_URL`, or work in a directory signed in to that tenant |
| A `.cirro/` *tracked by git* is refused | The folder came with a repository | If it is yours, `git rm -r --cached .cirro` |
| `CREDENTIAL_STORE_UNAVAILABLE` | On macOS, the login Keychain locked during a long operation | Unlock the login Keychain and run it again |
| `NETWORK_UNAVAILABLE` | Cirro could not be reached | Check your connection, any VPN, and the hostname |

## Using it

| You see | What it means | What to do |
|---|---|---|
| Claude Code asks every time Claude reads | Reading is not allowed in your settings yet | Allow `cirro-agent read`; see [Two kinds of prompts](getting-started.md#two-kinds-of-prompts) |
| `FORBIDDEN` | Your project role does not allow this | Ask a project owner |
| `PLAN_STALE` | Something in Cirro changed between the plan and your approval | Ask Claude to plan it again; nothing was changed |
| A change still asks for approval under a grant | That variant of the change can never be approved in advance | See [what can be granted](grants.md#what-can-be-granted) |
| Claude says running a script is blocked | Once a session uses Cirro, scripts that drive the Cirro SDK cannot run through Claude | Run the script yourself in a terminal; see [Locking it down](locking-down.md) |

## Starting over

Ask Claude which tenants you are signed in to; it names each login folder. Removing
one, or deleting a directory's `.cirro/`, is always a safe reset: the next request asks
you to sign in again. [Uninstalling](getting-started.md#uninstalling) covers removing
everything.

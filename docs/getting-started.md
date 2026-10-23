# Getting started

## Install the plugin

```bash
claude plugin marketplace add CirroBio/Cirro-claude-plugin
claude plugin install cirro@cirro
```

## Install the runtime

Then, once, in Claude Code:

```
/cirro:setup
```

This builds an isolated Python environment containing the Cirro SDK. It needs
[`uv`](https://docs.astral.sh/uv/) and takes up to two minutes the first time. Nothing
runs it behind your back — a silent two-minute install inside something that looks
instant is a bad surprise.

Until it has run, the Cirro tools load but decline every call, saying the runtime is
missing and naming this command. That is what a fresh install looks like, not a broken
one.

## Connect to Cirro

Go to whatever directory you want to work in and ask Claude to connect to Cirro. It will
ask for your Cirro hostname (something like `app.cirro.bio`), then hand you a link to
sign in with.

Claude prints the sign-in link straight to your terminal as soon as it has one, as well
as showing it in the conversation. Open it yourself and approve. Nothing launches a
browser on your behalf.

If the directory is already signed in, none of that happens — an expired token is
refreshed silently and Claude just tells you who you are.

By default you sign in once for the tenant, and every directory on the machine can use
that login. [Signing in](signing-in.md) explains where the login is kept and when you
would want one for a single directory instead.

## Ask for what you want

> *"What failed in the Immuno-Oncology project this week?"*
>
> *"Why did that run fail?"*
>
> *"Move those three datasets into the QC folder."*

Reading never prompts. Anything that would change something is shown to you first and
waits for your approval — see [Making changes](changes.md).

## If something seems off

```
/cirro:doctor
```

tells you where things stand: which tenant this directory is bound to, whether the
runtime is installed, and who you are signed in as, proved with a live call. It reports
a remedy for whichever check fails first.

## Updating

```bash
claude plugin marketplace update cirro
claude plugin update cirro@cirro
```

Start a new Claude Code session afterwards: a session that is already open keeps the
plugin it started with. Your logins live outside the plugin and survive updates.

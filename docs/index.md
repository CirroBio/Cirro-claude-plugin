# Cirro plugin for Claude Code

Work with your data on the [Cirro](https://cirro.bio) platform by asking Claude, using
your own Cirro identity.

**Looking at things is free. Changing things asks you first.**

You can let Claude explore your projects, datasets, runs and logs as freely as you like,
because nothing on that path can alter anything. The moment something would create,
modify or delete, it stops and shows you exactly what it is about to do. For a few kinds
of change you can tell it to stop asking for the rest of a session; anything that
deletes always asks.

```bash
claude plugin marketplace add CirroBio/Cirro-claude-plugin
claude plugin install cirro@cirro
```

Then follow [Getting started](getting-started.md) to install the runtime and sign in.
After that, just ask for what you want:

> *"What failed in the Immuno-Oncology project this week?"*
>
> *"Why did that run fail?"*
>
> *"Move those three datasets into the QC folder."*

## Where to go next

- **[How it works](how-it-works.md)**: an interactive diagram of the read path, the
  approval gate and the sign-in that every request goes through.
- **[Signing in](signing-in.md)**: where your login lives and how a directory finds it.
- **[Looking at things](reading.md)** and **[Making changes](changes.md)**: what Claude
  can read and change, and what approving a change looks like.
- **[Approving a kind of change](grants.md)**: how to stop being asked for repetitive
  changes of one kind, and what you give up when you do.
- **[Status](status.md)**: which parts have run against a real Cirro tenant and which
  have not.

The plugin is developed in
[CirroBio/Cirro-claude-plugin](https://github.com/CirroBio/Cirro-claude-plugin). Its
README covers how it is built and how to contribute.

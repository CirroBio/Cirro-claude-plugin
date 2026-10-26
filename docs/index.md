# Cirro plugin for Claude Code

Work with your data on the [Cirro](https://cirro.bio) platform by asking Claude, using
your own Cirro account.

**Looking never needs your approval. Every change is shown to you as a plan first, and
waits for you to approve it.**

Claude can explore your projects, datasets, runs and logs as freely as you like, because
nothing on that path can alter anything. The moment something would create, change or
delete, it stops and shows you exactly what it is about to do. For a few kinds of
repetitive change you can tell it to stop asking for the rest of a session; anything
that deletes always asks.

## What you can ask

| You want to | Try asking | More |
|---|---|---|
| Find data | *"Which datasets in Immuno-Oncology Core failed this week?"* | [Looking at things](reading.md) |
| Understand a run | *"Why did that run fail, and what did it cost?"* | [Looking at things](reading.md) |
| Run a pipeline | *"Run nf-core/rnaseq on these three datasets."* | [Making changes](changes.md) |
| Organize data | *"Move those three datasets into the QC folder."* | [Making changes](changes.md) |
| Keep a table | *"Add these twelve samples to the Plate map sheet."* | [Working with sheets](sheets.md) |
| Make a figure | *"Plot the DE results as a volcano plot with a p-value slider."* | [Building dashboards](dashboards.md) |
| Add a pipeline | *"Turn this Nextflow workflow into a Cirro pipeline."* | [Adding a pipeline](pipelines.md) |

## What you need

- A Cirro account, and the address you sign in to Cirro at, such as `app.cirro.bio`.
- [Claude Code](https://claude.com/claude-code) on macOS or Linux, in a
  terminal or the desktop app.
- [`uv`](https://docs.astral.sh/uv/), which the plugin uses to build its own copy of
  Python.

**[Getting started](getting-started.md)** takes you from installing to your first
answer in a few minutes.

## Safe by design

Claude reads Cirro through a command that has no way to change anything. Every change
goes through a plan that you read and an approval prompt that Claude cannot answer for
you, and Claude works as you, with your project roles and nothing more.
[How it works](how-it-works.md) shows the whole path in one diagram.

This is an early release. Reading has been checked against a real Cirro tenant, and so
have the most common changes, but not every one has; [Status](status.md) says which.

## The plugin and the Cirro MCP server

Cirro also has an MCP server for AI assistants such as Claude Desktop, described in the
[Cirro documentation](https://docs.cirro.bio/cli-sdk/mcp/). This plugin is for Claude
Code: it runs on your computer and can also make changes, each one approved by you.

The plugin is developed in
[CirroBio/Cirro-claude-plugin](https://github.com/CirroBio/Cirro-claude-plugin). Its
README covers how it is built and how to contribute.

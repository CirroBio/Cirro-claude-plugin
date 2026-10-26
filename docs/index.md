# Cirro plugin for Claude Code

Work with your data on the [Cirro](https://cirro.bio) platform by asking Claude, using
your own Cirro account.

<div class="hover-diagram">
<svg viewBox="0 0 1000 670" role="group" aria-label="What asks for your approval and what does not. Select a part of the diagram to read about it.">
  <defs>
    <marker id="home-arrow-ink" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="11" markerHeight="11" markerUnits="userSpaceOnUse" orient="auto">
      <path d="M1,1 L9,5 L1,9" fill="none" stroke="#1b1e3a" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    </marker>
    <marker id="home-arrow-teal" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="12" markerHeight="12" markerUnits="userSpaceOnUse" orient="auto">
      <path d="M1,1 L9,5 L1,9" fill="none" stroke="#0e7d9f" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    </marker>
  </defs>
  <g class="hd-hot" data-key="change" tabindex="0" aria-label="Changes ask you first">
    <g data-r="change grant">
      <rect x="190" y="110" width="480" height="245" rx="22" fill="#f7f8fb" stroke="#5b6070" stroke-width="1.5" stroke-dasharray="8 7"/>
      <text x="214" y="146" class="hd-strong">Changes ask you first</text>
      <text x="214" y="170" class="hd-sub">uploads, runs, edits, deletes</text>
      <circle class="hd-node" cx="300" cy="245" r="38"/>
      <g class="hd-icon" transform="translate(300,245)"><rect x="-12" y="-16" width="24" height="32" rx="3"/><path d="M-6,-8 H6 M-6,-1 H6 M-6,6 H2"/></g>
      <text x="300" y="312" text-anchor="middle">Plan</text>
      <text x="300" y="336" text-anchor="middle" class="hd-sub">nothing changes yet</text>
    </g>
    <g data-r="change">
      <circle class="hd-node" cx="470" cy="245" r="18"/>
      <g transform="translate(470,245)"><rect x="-11" y="-6" width="22" height="12" rx="6" fill="none" stroke="#1b1e3a" stroke-width="2"/><circle cx="5" cy="0" r="3.5" fill="#1b1e3a"/></g>
      <text x="470" y="292" text-anchor="middle">Your approval</text>
      <text x="470" y="314" text-anchor="middle" class="hd-sub">each change</text>
    </g>
  </g>
  <g class="hd-hot" data-key="read" tabindex="0" aria-label="Reading never asks">
    <g data-r="read">
      <rect x="190" y="395" width="480" height="200" rx="22" fill="#f7f8fb" stroke="#5b6070" stroke-width="1.5" stroke-dasharray="8 7"/>
      <text x="214" y="431" class="hd-strong">Reading never asks</text>
      <text x="214" y="455" class="hd-sub">projects, datasets, runs, logs, costs</text>
      <circle class="hd-node" cx="330" cy="510" r="38"/>
      <g class="hd-icon" transform="translate(330,510)"><circle cx="-4" cy="-4" r="10"/><path d="M3.5,3.5 L13,13"/></g>
      <text x="330" y="577" text-anchor="middle">cirro-agent read</text>
    </g>
  </g>
  <path class="hd-dotted" data-r="change" d="M128,70 H470 V227"/>
  <path class="hd-dotted" data-r="grant" d="M128,70 H600 V142"/>
  <path class="hd-dotted" data-r="grant" d="M600,178 V245"/>
  <path class="hd-line" data-r="read change grant" d="M90,152 V336" marker-end="url(#home-arrow-ink)"/>
  <path class="hd-path" data-r="change grant" d="M128,378 H160 V245 H262"/>
  <path class="hd-path" data-r="change grant" d="M338,245 H452"/>
  <path class="hd-path" data-r="change" d="M488,245 H600"/>
  <path class="hd-path" data-r="change grant login" d="M600,245 H700 C750,245 750,378 786,378"/>
  <path class="hd-path" data-r="read" d="M128,378 H160 V510 H292"/>
  <path class="hd-path" data-r="read login" d="M368,510 H700 C750,510 750,378 786,378"/>
  <path class="hd-path" data-r="login read change grant" d="M838,378 H874" marker-end="url(#home-arrow-teal)"/>
  <g data-r="login read change grant">
    <circle class="hd-node" cx="90" cy="378" r="38"/>
    <g class="hd-icon" transform="translate(90,378)">
      <path d="M-16,-13 H16 A3,3 0 0 1 19,-10 V7 A3,3 0 0 1 16,10 H-2 L-10,17 V10 H-16 A3,3 0 0 1 -19,7 V-10 A3,3 0 0 1 -16,-13 Z"/>
      <path d="M-9,-2 H9"/>
    </g>
    <text x="90" y="445" text-anchor="middle">Claude</text>
    <text x="90" y="469" text-anchor="middle" class="hd-sub">in Claude Code</text>
  </g>
  <g class="hd-hot" data-key="change" tabindex="0" aria-label="You">
    <rect class="hd-hit" x="30" y="16" width="120" height="132" rx="12"/>
    <g data-r="change grant">
      <circle class="hd-node" cx="90" cy="70" r="38"/>
      <g class="hd-icon" transform="translate(90,70)"><circle cx="0" cy="-9" r="8"/><path d="M-15,18 a15,15 0 0 1 30,0"/></g>
      <text x="90" y="137" text-anchor="middle">You</text>
    </g>
  </g>
  <g class="hd-hot" data-key="grant" tabindex="0" aria-label="Or approve one kind of change for this session">
    <rect class="hd-hit" x="476" y="128" width="164" height="70" rx="12"/>
    <g data-r="grant">
      <circle class="hd-node" cx="600" cy="160" r="18"/>
      <g class="hd-icon-sm" transform="translate(600,160)"><circle cx="-4" cy="0" r="4.5"/><path d="M0.5,0 H10 M7,0 V4"/></g>
      <text x="576" y="156" text-anchor="end">Grant</text>
      <text x="576" y="178" text-anchor="end" class="hd-sub">this session</text>
    </g>
  </g>
  <g class="hd-hot" data-key="login" tabindex="0" aria-label="Your login and your Cirro tenant">
    <rect class="hd-hit" x="776" y="344" width="72" height="94" rx="12"/>
    <rect class="hd-hit" x="852" y="255" width="136" height="246" rx="22" stroke="#0e7d9f" stroke-width="1.5" stroke-dasharray="8 7"/>
    <g data-r="login read change grant">
      <circle class="hd-node" cx="812" cy="378" r="26"/>
      <g class="hd-icon-sm" transform="translate(812,378)"><path d="M-12,-8 H-4 L-1,-5 H12 V9 H-12 Z"/></g>
      <text x="812" y="426" text-anchor="middle" class="hd-sub">your login</text>
      <text x="920" y="291" text-anchor="middle" class="hd-strong">Cirro</text>
      <text x="920" y="315" text-anchor="middle" class="hd-sub">your tenant</text>
      <circle class="hd-node-teal" cx="920" cy="378" r="44"/>
      <g class="hd-icon-teal" transform="translate(920,378)">
        <ellipse cx="0" cy="-12" rx="16" ry="6"/>
        <path d="M-16,-12 V12 A16,6 0 0 0 16,12 V-12"/>
        <path d="M-16,0 A16,6 0 0 0 16,0"/>
      </g>
      <text x="920" y="448" text-anchor="middle" class="hd-sub">your data</text>
    </g>
  </g>
  <g aria-hidden="true">
    <circle class="hd-node" cx="70" cy="640" r="18"/>
    <g transform="translate(70,640)"><rect x="-11" y="-6" width="22" height="12" rx="6" fill="none" stroke="#1b1e3a" stroke-width="2"/><circle cx="5" cy="0" r="3.5" fill="#1b1e3a"/></g>
    <text x="98" y="646" class="hd-sub">Your approval</text>
    <circle class="hd-node" cx="270" cy="640" r="18"/>
    <g class="hd-icon-sm" transform="translate(270,640)"><path d="M-9,-6 H-3 L-1,-4 H9 V7 H-9 Z"/></g>
    <text x="298" y="646" class="hd-sub">Your login</text>
    <path class="hd-dotted" d="M450,640 H492"/>
    <text x="506" y="646" class="hd-sub">Your decision</text>
    <path class="hd-path" d="M680,640 H726"/>
    <text x="740" y="646" class="hd-sub">Path to your data</text>
  </g>
</svg>
<div class="hd-panel" aria-live="polite">
  <div class="hd-note is-shown" data-note="">
    <p class="hd-kicker">The plugin</p>
    <p class="hd-title">What asks for your approval, and what does not</p>
    <p>Teal lines are the paths from Claude to your data. Anything in the top box changes something in Cirro, so it waits for your approval. Anything in the bottom box only reads, so it never asks. Hover over or tap any part of the diagram to learn more.</p>
  </div>
  <div class="hd-note" data-note="change">
    <p class="hd-kicker">Changes</p>
    <p class="hd-title">Every change waits for your approval</p>
    <p>Uploading, launching a run, editing or deleting: anything that creates, changes or deletes is first worked out as a plan, without touching anything, and shown to you. It runs only when you approve it in Claude Code's own prompt, which Claude cannot answer for you. What runs is what you read: the plan is checked again as it is applied, used once, and logged.</p>
  </div>
  <div class="hd-note" data-note="grant">
    <p class="hd-kicker">In advance</p>
    <p class="hd-title">Or approve one kind of change for this session</p>
    <p>For repetitive work, such as filling in a sheet or launching runs one after another, you can approve one kind of change in one project for the rest of the session. Each change is still planned, shown and logged, with a notice as it is made. Nothing that deletes or changes who has access can be approved this way.</p>
  </div>
  <div class="hd-note" data-note="read">
    <p class="hd-kicker">Reading</p>
    <p class="hd-title">Looking never asks</p>
    <p>Questions about your projects, datasets, runs, logs and costs go through cirro-agent read, a command with no way to change anything. It never needs your approval, and you can let Claude Code run it without asking at all.</p>
  </div>
  <div class="hd-note" data-note="login">
    <p class="hd-kicker">Your login</p>
    <p class="hd-title">Claude works as you</p>
    <p>You sign in once for each Cirro tenant you use, on Cirro's own page in your browser, and the plugin keeps the login on your computer. Every request, a read or a change, goes with your login, so Claude can see and do what your account can and nothing more.</p>
  </div>
</div>
</div>

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
[How it works](how-it-works.md) shows the same path in more detail, including how a
login is found for the directory you work in and the check on Claude's shell commands.

## The plugin and the Cirro MCP server

Cirro also has an MCP server for AI assistants such as Claude Desktop, described in the
[Cirro documentation](https://docs.cirro.bio/cli-sdk/mcp/). This plugin is for Claude
Code: it runs on your computer and can also make changes, each one approved by you.

The plugin is developed in
[CirroBio/Cirro-claude-plugin](https://github.com/CirroBio/Cirro-claude-plugin). Its
README covers how it is built and how to contribute.

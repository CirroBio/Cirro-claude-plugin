# Cirro plugin for Claude Code

Work with your data on the [Cirro](https://cirro.bio) platform by asking Claude, using
your own Cirro account.

<div class="hover-diagram">
<svg viewBox="0 0 1000 670" role="group" aria-label="How the plugin signs you in, reads and makes changes. Select a part of the diagram to read about it.">
  <defs>
    <marker id="home-arrow-ink" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="11" markerHeight="11" markerUnits="userSpaceOnUse" orient="auto">
      <path d="M1,1 L9,5 L1,9" fill="none" stroke="#1b1e3a" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    </marker>
    <marker id="home-arrow-teal" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="12" markerHeight="12" markerUnits="userSpaceOnUse" orient="auto">
      <path d="M1,1 L9,5 L1,9" fill="none" stroke="#0e7d9f" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    </marker>
  </defs>
  <path class="hd-dotted" data-r="change" d="M128,90 H430 V212"/>
  <path class="hd-dotted" data-r="grant" d="M128,90 H560 V112"/>
  <path class="hd-dotted" data-r="grant" d="M560,148 V230"/>
  <path class="hd-line" data-r="read change grant" d="M90,172 V318" marker-end="url(#home-arrow-ink)"/>
  <path class="hd-path" data-r="change grant" d="M128,360 H160 V230 H222"/>
  <path class="hd-path" data-r="change grant" d="M298,230 H412"/>
  <path class="hd-path" data-r="change" d="M448,230 H560"/>
  <path class="hd-path" data-r="change grant" d="M560,230 H620"/>
  <path class="hd-path" data-r="read" d="M128,360 H160 V480 H262"/>
  <path class="hd-path" data-r="read" d="M338,480 H620"/>
  <path class="hd-path" data-r="login read change grant" d="M620,230 V480"/>
  <path class="hd-path" data-r="login read change grant" d="M620,230 H669 M721,230 H831" marker-end="url(#home-arrow-teal)"/>
  <path class="hd-path" data-r="login read change grant" d="M620,480 H669 M721,480 H831" marker-end="url(#home-arrow-teal)"/>
  <g data-r="login read change grant">
    <circle class="hd-node" cx="90" cy="360" r="38"/>
    <g class="hd-icon" transform="translate(90,360)">
      <path d="M-16,-13 H16 A3,3 0 0 1 19,-10 V7 A3,3 0 0 1 16,10 H-2 L-10,17 V10 H-16 A3,3 0 0 1 -19,7 V-10 A3,3 0 0 1 -16,-13 Z"/>
      <path d="M-9,-2 H9"/>
    </g>
    <text x="90" y="427" text-anchor="middle">Claude</text>
    <text x="90" y="451" text-anchor="middle" class="hd-sub">in Claude Code</text>
  </g>
  <g class="hd-hot" data-key="change" tabindex="0" aria-label="You">
    <rect class="hd-hit" x="30" y="36" width="120" height="132" rx="12"/>
    <g data-r="change grant">
      <circle class="hd-node" cx="90" cy="90" r="38"/>
      <g class="hd-icon" transform="translate(90,90)"><circle cx="0" cy="-9" r="8"/><path d="M-15,18 a15,15 0 0 1 30,0"/></g>
      <text x="90" y="157" text-anchor="middle">You</text>
    </g>
  </g>
  <g class="hd-hot" data-key="change" tabindex="0" aria-label="Changing: a plan, then your approval">
    <rect class="hd-hit" x="182" y="186" width="318" height="146" rx="12"/>
    <g data-r="change grant">
      <circle class="hd-node" cx="260" cy="230" r="38"/>
      <g class="hd-icon" transform="translate(260,230)"><rect x="-12" y="-16" width="24" height="32" rx="3"/><path d="M-6,-8 H6 M-6,-1 H6 M-6,6 H2"/></g>
      <text x="260" y="297" text-anchor="middle">Plan</text>
      <text x="260" y="321" text-anchor="middle" class="hd-sub">nothing changes yet</text>
    </g>
    <g data-r="change">
      <circle class="hd-node" cx="430" cy="230" r="18"/>
      <g transform="translate(430,230)"><rect x="-11" y="-6" width="22" height="12" rx="6" fill="none" stroke="#1b1e3a" stroke-width="2"/><circle cx="5" cy="0" r="3.5" fill="#1b1e3a"/></g>
      <text x="430" y="277" text-anchor="middle" class="hd-sub">Your approval</text>
    </g>
  </g>
  <g class="hd-hot" data-key="grant" tabindex="0" aria-label="Approving a kind of change for a session">
    <rect class="hd-hit" x="530" y="100" width="170" height="64" rx="12"/>
    <g data-r="grant">
      <circle class="hd-node" cx="560" cy="130" r="18"/>
      <g class="hd-icon-sm" transform="translate(560,130)"><circle cx="-4" cy="0" r="4.5"/><path d="M0.5,0 H10 M7,0 V4"/></g>
      <text x="584" y="126">Grant</text>
      <text x="584" y="150" class="hd-sub">this session</text>
    </g>
  </g>
  <g class="hd-hot" data-key="read" tabindex="0" aria-label="Reading: cirro-agent read">
    <rect class="hd-hit" x="205" y="436" width="190" height="146" rx="12"/>
    <g data-r="read">
      <circle class="hd-node" cx="300" cy="480" r="38"/>
      <g class="hd-icon" transform="translate(300,480)"><circle cx="-4" cy="-4" r="10"/><path d="M3.5,3.5 L13,13"/></g>
      <text x="300" y="547" text-anchor="middle">cirro-agent read</text>
      <text x="300" y="571" text-anchor="middle" class="hd-sub">never asks</text>
    </g>
  </g>
  <g class="hd-hot" data-key="login" tabindex="0" aria-label="Your logins, one for each tenant">
    <rect class="hd-hit" x="660" y="172" width="70" height="340" rx="12"/>
    <rect class="hd-hit" x="760" y="112" width="232" height="476" rx="12"/>
    <g data-r="login read change grant">
      <rect x="770" y="120" width="215" height="210" rx="26" fill="none" stroke="#0e7d9f" stroke-width="1.5" stroke-dasharray="8 7"/>
      <text x="877" y="156" text-anchor="middle" class="hd-strong">app.cirro.bio</text>
      <circle class="hd-node-teal" cx="877" cy="230" r="44"/>
      <g class="hd-icon-teal" transform="translate(877,230)">
        <ellipse cx="0" cy="-12" rx="16" ry="6"/>
        <path d="M-16,-12 V12 A16,6 0 0 0 16,12 V-12"/>
        <path d="M-16,0 A16,6 0 0 0 16,0"/>
      </g>
      <text x="877" y="304" text-anchor="middle" class="hd-sub">projects and data</text>
      <rect x="770" y="370" width="215" height="210" rx="26" fill="none" stroke="#0e7d9f" stroke-width="1.5" stroke-dasharray="8 7"/>
      <text x="877" y="406" text-anchor="middle" class="hd-strong">institute.cirro.bio</text>
      <circle class="hd-node-teal" cx="877" cy="480" r="44"/>
      <g class="hd-icon-teal" transform="translate(877,480)">
        <ellipse cx="0" cy="-12" rx="16" ry="6"/>
        <path d="M-16,-12 V12 A16,6 0 0 0 16,12 V-12"/>
        <path d="M-16,0 A16,6 0 0 0 16,0"/>
      </g>
      <text x="877" y="554" text-anchor="middle" class="hd-sub">projects and data</text>
    </g>
    <g data-r="login read change grant">
      <circle class="hd-node" cx="695" cy="230" r="26"/>
      <g class="hd-icon-sm" transform="translate(695,230)"><path d="M-12,-8 H-4 L-1,-5 H12 V9 H-12 Z"/></g>
      <text x="695" y="190" text-anchor="middle" class="hd-sub">your login</text>
      <circle class="hd-node" cx="695" cy="480" r="26"/>
      <g class="hd-icon-sm" transform="translate(695,480)"><path d="M-12,-8 H-4 L-1,-5 H12 V9 H-12 Z"/></g>
      <text x="695" y="440" text-anchor="middle" class="hd-sub">your login</text>
    </g>
  </g>
  <g aria-hidden="true">
    <circle class="hd-node" cx="70" cy="640" r="18"/>
    <g transform="translate(70,640)"><rect x="-11" y="-6" width="22" height="12" rx="6" fill="none" stroke="#1b1e3a" stroke-width="2"/><circle cx="5" cy="0" r="3.5" fill="#1b1e3a"/></g>
    <text x="98" y="646" class="hd-sub">Your approval</text>
    <circle class="hd-node" cx="270" cy="640" r="18"/>
    <g class="hd-icon-sm" transform="translate(270,640)"><path d="M-9,-6 H-3 L-1,-4 H9 V7 H-9 Z"/></g>
    <text x="298" y="646" class="hd-sub">Your login to one tenant</text>
    <path class="hd-dotted" d="M540,640 H582"/>
    <text x="596" y="646" class="hd-sub">Your decision</text>
    <path class="hd-path" d="M760,640 H806"/>
    <text x="820" y="646" class="hd-sub">Path to your data</text>
  </g>
</svg>
<div class="hd-panel" aria-live="polite">
  <div class="hd-note is-shown" data-note="">
    <p class="hd-kicker">The plugin</p>
    <p class="hd-title">How Claude reaches your data in Cirro</p>
    <p>Teal lines are the paths from Claude to your data: changes along the top, reads along the bottom. Both reach every tenant you are signed in to, with your own login. The switch is an approval only you can give, and the dotted lines are your decisions. Hover over or tap any part of the diagram to learn more.</p>
  </div>
  <div class="hd-note" data-note="login">
    <p class="hd-kicker">Your logins</p>
    <p class="hd-title">Sign in once for each tenant</p>
    <p>Ask Claude to connect to a tenant, such as app.cirro.bio, and it gives you a sign-in link to approve in your own browser. The plugin keeps that login on your computer, outside your projects, and every request to that tenant goes as you, with its login. Signed in to both app.cirro.bio and institute.cirro.bio, Claude asks which one you mean, unless the directory you are working in has a login of its own.</p>
  </div>
  <div class="hd-note" data-note="read">
    <p class="hd-kicker">Reading</p>
    <p class="hd-title">Looking never asks</p>
    <p>Questions about your data go through cirro-agent read, a command with no way to change anything, so it never needs your approval and you can let Claude Code run it without asking. Claude can look around as much as a question needs.</p>
  </div>
  <div class="hd-note" data-note="change">
    <p class="hd-kicker">Changing</p>
    <p class="hd-title">Every change waits for you</p>
    <p>Anything that creates, changes or deletes starts as a plan, worked out without touching anything and shown to you in the conversation. It runs only when you approve it in Claude Code's own prompt, which Claude cannot answer for you. What runs is what you read: the plan is checked again as it is applied, used once, and logged.</p>
  </div>
  <div class="hd-note" data-note="grant">
    <p class="hd-kicker">In advance</p>
    <p class="hd-title">Stop being asked about one kind of change</p>
    <p>For repetitive work, such as filling in a sheet or launching runs one after another, you can approve one kind of change in one project for the rest of the session. Each change is still planned, shown and logged, with a notice as it is made. Nothing that deletes or changes who has access can be approved this way.</p>
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

Reading, and the changes people make most often, have run many times against real
Cirro tenants; [Status](status.md) lists what has.

## The plugin and the Cirro MCP server

Cirro also has an MCP server for AI assistants such as Claude Desktop, described in the
[Cirro documentation](https://docs.cirro.bio/cli-sdk/mcp/). This plugin is for Claude
Code: it runs on your computer and can also make changes, each one approved by you.

The plugin is developed in
[CirroBio/Cirro-claude-plugin](https://github.com/CirroBio/Cirro-claude-plugin). Its
README covers how it is built and how to contribute.

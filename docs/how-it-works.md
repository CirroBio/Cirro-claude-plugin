# How it works

Everything the plugin does goes through one of two doors. Changing goes through a plan
you read and an approval only you can give. Reading goes through a command that cannot
change anything, so it never asks. Both use a login kept on your computer, found from
the directory you are working in.

<div class="hover-diagram">
<svg viewBox="0 0 1000 860" role="group" aria-label="How the Cirro plugin works. Select a part of the diagram to read about it.">
  <defs>
    <marker id="hd-arrow-ink" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="11" markerHeight="11" markerUnits="userSpaceOnUse" orient="auto">
      <path d="M1,1 L9,5 L1,9" fill="none" stroke="#1b1e3a" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    </marker>
    <marker id="hd-arrow-teal" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="12" markerHeight="12" markerUnits="userSpaceOnUse" orient="auto">
      <path d="M1,1 L9,5 L1,9" fill="none" stroke="#0e7d9f" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    </marker>
  </defs>
  <rect x="740" y="140" width="245" height="470" rx="26" fill="none" stroke="#0e7d9f" stroke-width="1.5" stroke-dasharray="8 7"/>
  <text x="764" y="176" class="hd-strong">Cirro</text>
  <text x="764" y="200" class="hd-sub">your tenant</text>
  <path class="hd-dotted" data-r="approve" d="M128,90 H430 V232"/>
  <path class="hd-dotted" data-r="grant" d="M128,90 H600 V152"/>
  <path class="hd-dotted" data-r="grant" d="M600,188 V250"/>
  <path class="hd-line" data-r="read plan approve grant gate" d="M90,170 V288" marker-end="url(#hd-arrow-ink)"/>
  <path class="hd-path" data-r="plan approve grant" d="M128,330 H170 V250 H228"/>
  <path class="hd-path" data-r="approve grant tenant" d="M308,250 H412 M448,250 H680 C740,250 750,360 816,360" marker-end="url(#hd-arrow-teal)"/>
  <path class="hd-path" data-r="read gate" d="M170,330 V470 H248"/>
  <path class="hd-path" data-r="read gate" d="M288,470 H388"/>
  <path class="hd-path" data-r="read tenant" d="M468,470 H680 C740,470 750,360 816,360" marker-end="url(#hd-arrow-teal)"/>
  <path class="hd-line" data-r="find" d="M438,680 C480,680 510,650 510,610"/>
  <path class="hd-line" data-r="find signin" d="M582,680 C540,680 510,650 510,610"/>
  <path class="hd-line" data-r="find tenant" d="M510,610 V592 Q510,580 522,580 H736" marker-end="url(#hd-arrow-ink)"/>
  <text x="510" y="704" text-anchor="middle" class="hd-sub" data-r="find">or</text>
  <text x="628" y="570" text-anchor="middle" class="hd-sub" data-r="find tenant">as you</text>
  <path class="hd-line" data-r="signin" d="M862,576 V670 Q862,680 852,680 H662" marker-end="url(#hd-arrow-ink)"/>
  <text x="760" y="670" text-anchor="middle" class="hd-sub" data-r="signin">your login</text>
  <g data-r="read plan approve grant gate">
    <circle class="hd-node" cx="90" cy="330" r="38"/>
    <g class="hd-icon" transform="translate(90,330)">
      <path d="M-16,-13 H16 A3,3 0 0 1 19,-10 V7 A3,3 0 0 1 16,10 H-2 L-10,17 V10 H-16 A3,3 0 0 1 -19,7 V-10 A3,3 0 0 1 -16,-13 Z"/>
      <path d="M-9,-2 H9"/>
    </g>
    <text x="90" y="397" text-anchor="middle">Claude</text>
    <text x="90" y="421" text-anchor="middle" class="hd-sub">in Claude Code</text>
  </g>
  <g class="hd-hot" data-key="approve" tabindex="0" aria-label="You">
    <rect class="hd-hit" x="30" y="36" width="120" height="132" rx="12"/>
    <g data-r="signin approve grant">
      <circle class="hd-node" cx="90" cy="90" r="38"/>
      <g class="hd-icon" transform="translate(90,90)"><circle cx="0" cy="-9" r="8"/><path d="M-15,18 a15,15 0 0 1 30,0"/></g>
      <text x="90" y="157" text-anchor="middle">You</text>
    </g>
  </g>
  <g class="hd-hot" data-key="signin" tabindex="0" aria-label="Signing in">
    <rect class="hd-hit" x="796" y="476" width="132" height="108" rx="12"/>
    <g data-r="signin">
      <text x="862" y="502" text-anchor="middle">Sign-in page</text>
      <circle class="hd-node-teal" cx="862" cy="546" r="30"/>
      <g class="hd-icon-teal" transform="translate(862,546)"><rect x="-10" y="-3" width="20" height="15" rx="3"/><path d="M-6,-3 V-7 A6,6 0 0 1 6,-7 V-3"/></g>
    </g>
  </g>
  <g class="hd-hot" data-key="find" tabindex="0" aria-label="A .cirro folder in a directory above you">
    <rect class="hd-hit" x="292" y="634" width="214" height="146" rx="12"/>
    <g data-r="find">
      <circle class="hd-node" cx="400" cy="680" r="38"/>
      <g class="hd-icon" transform="translate(400,680)"><path d="M-17,-12 H-5 L-1,-8 H17 V12 H-17 Z"/></g>
      <text x="400" y="747" text-anchor="middle">A .cirro/ folder</text>
      <text x="400" y="771" text-anchor="middle" class="hd-sub">in a directory above you</text>
    </g>
  </g>
  <g class="hd-hot" data-key="find" tabindex="0" aria-label="Your tenant login">
    <rect class="hd-hit" x="512" y="634" width="220" height="146" rx="12"/>
    <g data-r="find signin">
      <circle class="hd-node" cx="620" cy="680" r="38"/>
      <g class="hd-icon" transform="translate(620,680)"><path d="M-17,-12 H-5 L-1,-8 H17 V12 H-17 Z"/><circle cx="0" cy="1" r="4"/></g>
      <text x="620" y="747" text-anchor="middle">Tenant login</text>
      <text x="620" y="771" text-anchor="middle" class="hd-sub">for every other directory</text>
    </g>
  </g>
  <g class="hd-hot" data-key="read" tabindex="0" aria-label="Reading: cirro-agent read">
    <rect class="hd-hit" x="334" y="424" width="192" height="146" rx="12"/>
    <g data-r="read gate">
      <circle class="hd-node" cx="430" cy="470" r="38"/>
      <g class="hd-icon" transform="translate(430,470)"><circle cx="-4" cy="-4" r="10"/><path d="M3.5,3.5 L13,13"/></g>
      <text x="430" y="537" text-anchor="middle">cirro-agent read</text>
      <text x="430" y="561" text-anchor="middle" class="hd-sub">read-only, never asks</text>
    </g>
  </g>
  <g class="hd-hot" data-key="plan" tabindex="0" aria-label="Changing, step 1: the plan">
    <rect class="hd-hit" x="185" y="206" width="170" height="146" rx="12"/>
    <g data-r="plan approve grant">
      <circle class="hd-node" cx="270" cy="250" r="38"/>
      <g class="hd-icon" transform="translate(270,250)"><rect x="-12" y="-16" width="24" height="32" rx="3"/><path d="M-6,-8 H6 M-6,-1 H6 M-6,6 H2"/></g>
      <text x="270" y="317" text-anchor="middle">Plan</text>
      <text x="270" y="341" text-anchor="middle" class="hd-sub">nothing changes yet</text>
    </g>
  </g>
  <g class="hd-hot" data-key="approve" tabindex="0" aria-label="Changing, step 2: your approval">
    <rect class="hd-hit" x="372" y="222" width="116" height="84" rx="12"/>
    <g data-r="approve grant">
      <circle class="hd-node" cx="430" cy="250" r="18"/>
      <g transform="translate(430,250)"><rect x="-11" y="-6" width="22" height="12" rx="6" fill="none" stroke="#1b1e3a" stroke-width="2"/><circle cx="5" cy="0" r="3.5" fill="#1b1e3a"/></g>
      <text x="430" y="297" text-anchor="middle" class="hd-sub">Your approval</text>
    </g>
  </g>
  <g class="hd-hot" data-key="grant" tabindex="0" aria-label="Approving a kind of change for a session">
    <rect class="hd-hit" x="466" y="140" width="162" height="60" rx="12"/>
    <g data-r="grant">
      <circle class="hd-node" cx="600" cy="170" r="18"/>
      <g class="hd-icon-sm" transform="translate(600,170)"><circle cx="-4" cy="0" r="4.5"/><path d="M0.5,0 H10 M7,0 V4"/></g>
      <text x="578" y="166" text-anchor="end">Grant</text>
      <text x="578" y="188" text-anchor="end" class="hd-sub">this session</text>
    </g>
  </g>
  <g class="hd-hot" data-key="gate" tabindex="0" aria-label="The Bash check">
    <rect class="hd-hit" x="222" y="442" width="96" height="84" rx="12"/>
    <g data-r="read gate">
      <circle class="hd-node" cx="270" cy="470" r="18"/>
      <g transform="translate(270,470)"><rect x="-11" y="-6" width="22" height="12" rx="6" fill="none" stroke="#1b1e3a" stroke-width="2"/><circle cx="5" cy="0" r="3.5" fill="#1b1e3a"/></g>
      <text x="270" y="517" text-anchor="middle" class="hd-sub">Bash check</text>
    </g>
  </g>
  <g class="hd-hot" data-key="tenant" tabindex="0" aria-label="Your projects and data in Cirro">
    <rect class="hd-hit" x="756" y="310" width="222" height="152" rx="12"/>
    <g data-r="read plan approve grant tenant">
      <circle class="hd-node-teal" cx="862" cy="360" r="44"/>
      <g class="hd-icon-teal" transform="translate(862,360)">
        <ellipse cx="0" cy="-12" rx="16" ry="6"/>
        <path d="M-16,-12 V12 A16,6 0 0 0 16,12 V-12"/>
        <path d="M-16,0 A16,6 0 0 0 16,0"/>
      </g>
      <text x="862" y="427" text-anchor="middle">Projects and data</text>
      <text x="862" y="451" text-anchor="middle" class="hd-sub">as your account sees them</text>
    </g>
  </g>
  <g aria-hidden="true">
    <circle class="hd-node" cx="70" cy="830" r="18"/>
    <g transform="translate(70,830)"><rect x="-11" y="-6" width="22" height="12" rx="6" fill="none" stroke="#1b1e3a" stroke-width="2"/><circle cx="5" cy="0" r="3.5" fill="#1b1e3a"/></g>
    <text x="98" y="836" class="hd-sub">A check on the way</text>
    <path class="hd-dotted" d="M400,830 H442"/>
    <text x="456" y="836" class="hd-sub">Your decision</text>
    <path class="hd-path" d="M610,830 H656"/>
    <text x="670" y="836" class="hd-sub">Path to your data</text>
  </g>
</svg>
<div class="hd-panel" aria-live="polite">
  <div class="hd-note is-shown" data-note="">
    <p class="hd-kicker">The plugin</p>
    <p class="hd-title">How Claude reaches your data in Cirro</p>
    <p>Teal lines are the paths from Claude to your data: changes along the top, reads along the bottom. Each switch is a check a request must pass, and the dotted lines are decisions only you make. Hover over or tap any part of the diagram to learn more.</p>
  </div>
  <div class="hd-note" data-note="signin">
    <p class="hd-kicker">Signing in</p>
    <p class="hd-title">Sign in once, on Cirro's own page</p>
    <p>Ask Claude to connect and it asks for your Cirro hostname, then prints a sign-in link. You open it in your own browser and approve; nothing opens one for you. The login comes back to a tenant folder on your computer, outside every project, so one sign-in serves every directory.</p>
  </div>
  <div class="hd-note" data-note="find">
    <p class="hd-kicker">Your login</p>
    <p class="hd-title">Each request finds its login from where you work</p>
    <p>The plugin walks up from the directory Claude is working in to the nearest .cirro/ folder. If there is none, the tenant you signed in to answers. If you are signed in to two and nothing nearer decides, Claude names both and asks. A folder also keeps a cache, plans waiting for your approval and a log of every change.</p>
  </div>
  <div class="hd-note" data-note="read">
    <p class="hd-kicker">Reading</p>
    <p class="hd-title">Looking never asks</p>
    <p>Questions about your data go through cirro-agent read, a command with no way to change anything, so it needs no approval and you can allow it outright. A large answer comes back as the shape of everything that matched, with a few examples and follow-up questions that already know how many results they will return.</p>
  </div>
  <div class="hd-note" data-note="plan">
    <p class="hd-kicker">Changing, step 1 of 2</p>
    <p class="hd-title">A change starts as a plan</p>
    <p>Anything that creates, changes or deletes is first worked out without touching anything: names resolved to IDs, parameters checked against what the pipeline accepts, what would be affected counted, and consequences flagged. An instruction the operation does not understand stops it here. Claude shows you the plan in the conversation.</p>
  </div>
  <div class="hd-note" data-note="approve">
    <p class="hd-kicker">Changing, step 2 of 2</p>
    <p class="hd-title">Only you can approve it</p>
    <p>Applying a plan always brings up Claude Code's own approval prompt. Claude cannot answer it, and no setting or permission mode turns it off. What runs is what you read: the plan is worked out again as it is applied and refused if anything moved, it is good for one change, and the change is logged.</p>
  </div>
  <div class="hd-note" data-note="grant">
    <p class="hd-kicker">In advance</p>
    <p class="hd-title">Approve a kind of change for a session</p>
    <p>For repetitive work you can approve every later change of one kind, in one project, until the session ends. Only six kinds qualify, and none that deletes or changes who has access. Each change is still planned, shown, checked again and logged, and Claude Code shows a notice for every one.</p>
  </div>
  <div class="hd-note" data-note="gate">
    <p class="hd-kicker">The Bash check</p>
    <p class="hd-title">No way around the approval</p>
    <p>Once a session has used Cirro, every shell command is checked. Running a script that drives the Cirro SDK, starting the plugin's own worker or server by hand, or handing a .cirro/ folder's login to anything that reads it is refused. Reads pass, and editing files is never blocked. It stops accidents; it is not a sandbox.</p>
  </div>
  <div class="hd-note" data-note="tenant">
    <p class="hd-kicker">Your tenant</p>
    <p class="hd-title">Claude works as you</p>
    <p>Every request is made with your own Cirro login, so Claude can see and do what your account can, and nothing more. Projects, datasets, runs, pipelines, sheets and dashboards are all reached the same way.</p>
  </div>
</div>
</div>

## What this means for you

- **Reading never changes anything and never asks.** You can allow it outright — see
  [Looking at things](reading.md).
- **Every change is shown to you as a plan first**, and applied only after you approve it
  in a prompt Claude cannot answer — see [Making changes](changes.md).
- **What runs is what you read.** A plan is checked again when it is applied, used once,
  and logged.
- **Approving in advance is narrow**: one kind of change, one project, this session, and
  never a deletion or a change to who has access — see
  [Approving a kind of change](grants.md).
- **Your login stays on your computer**, in a folder the plugin finds from where you are
  working — see [Signing in](signing-in.md).
- **The way around the approval is closed off**, as far as reading commands as text
  allows — see [Locking it down](locking-down.md).

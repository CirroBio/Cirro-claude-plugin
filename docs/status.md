# Status

Every change the plugin makes goes through one path: a plan, your approval, and the
plan worked out again as it is applied. That path has run against real Cirro tenants
many times, across the kinds of change below. The tests run every operation against a
simulated tenant as well.

| Area | Run against a real tenant | Most recently |
|---|---|---|
| Reading: every read | Yes | 2026-10-26 |
| Uploading datasets and reference data, and importing public data | Yes | 2026-10-25 |
| Editing datasets: names, descriptions, tags, folders, data type, samplesheets | Yes | 2026-10-26 |
| Deleting datasets | Yes | 2026-10-26 |
| Downloading dataset files | Yes | 2026-10-22 |
| Launching, resuming and stopping analyses, alone and in chains | Yes | 2026-10-26 |
| Registering and editing pipelines, including where they may run, and uploading their configuration | Yes | 2026-10-22 |
| Dashboards: create, edit, delete | Yes | 2026-10-16 |
| Sheets: create, change columns, insert, update and delete rows, delete | Yes | 2026-10-16 |
| Workspaces: start, stop, delete | Yes | 2026-10-11 |
| Approving a kind of change for a session, by offer and by asking | Yes | 2026-10-26 |
| Signing in for a tenant, and listing tenants | Yes | 2026-10-23 |

## What the live runs confirmed

**The approval holds.** A forged plan, an altered approval text, a plan used twice and a
plan whose facts had moved were each refused against a real tenant. A plan refused as
stale because something changed in Cirro between the plan and the approval is an
everyday occurrence in real use, and nothing was changed in any of those cases.

**Nothing is lost on the way.** Folder membership survives every tag edit, and the
fields a change does not mention, such as a description nobody asked about, survive
rather than being blanked. Changing a dataset's data type and back kept its name,
description, tags and folders each way.

**Grants stay narrow.** A grant made a change of its kind with no prompt; a change of
the same kind that removed a tag still asked; revoking the grant ended it. A grant
survives `/clear`, and reconnecting the plugin's server with `/mcp` ends it.

**Dashboards and the portal agree.** An edit planned before a save in the portal is
refused as stale, and the portal keeps your save. An open tab picks up an edit made
through the plugin, and warns rather than overwrites when it has unsaved changes of its
own.

## Known limits

- **Refreshing a materialized view** has not yet succeeded against a real tenant: the
  tenants it was tried on refused it in their own backend, before anything the plugin
  controls.
- **Configuration kept in a repository** reaches Cirro only once the pipeline's GitHub
  connection is authorized in the Cirro portal; until then Cirro refuses to sync it.
- **Deleting a reference** is not offered; use the Cirro portal.

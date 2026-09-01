---
name: cirro-project-admin
description: Manage Cirro projects, access, data shares, and interactive workspaces. Use when the user wants to create a project, change who can access one, archive it, share data with another project, invite a user, or start, stop or create a workspace. Requires the user's approval before any change.
---

# Project, access, share and workspace administration

These operations affect other people's access to data and can incur charges. Every one
goes through `plan` → show the user → `apply`, and several deserve more caution than
the mechanics suggest.

## Look before changing

```bash
cirro-agent read projects
cirro-agent read project "My Project" --include storage_bytes
cirro-agent read project-users -p "My Project"
cirro-agent read shares -p "My Project"
cirro-agent read workspaces -p "My Project"
cirro-agent read compute-envs -p "My Project"
cirro-agent read users
```

## Projects

```
plan(op="create_project", args={
  "name": "...", "description": "...", "billing_account_id": "bill_..."})
```

A project provisions cloud infrastructure and begins accruing storage charges. The
plan validates the billing account against the ones the user can actually use, and
lists them if the given one is wrong.

```
plan(op="update_project_tags", args={"project_id": "...", "tags": ["..."]})
plan(op="archive_project",     args={"project_id": "..."})
plan(op="unarchive_project",   args={"project_id": "..."})
```

Archiving moves data to cold storage and blocks new analyses until reversed. The plan
reports how many datasets are affected.

Renaming a project or changing its billing is deliberately **not** available here —
those have contractual consequences and belong in the Cirro web UI.

## Access

```
plan(op="set_user_role", args={
  "project_id": "...", "username": "...", "role": "CONTRIBUTOR"})
```

Roles are `OWNER`, `ADMIN`, `CONTRIBUTOR`, `COLLABORATOR`, `NONE`. The plan shows the
current role and the new one, and warns when the change grants administrative power or
removes access entirely.

`NONE` removes someone's access to the project. Confirm the user means that specific
person before planning it.

```
plan(op="invite_user", args={"name": "...", "email": "...", "organization": "..."})
```

This sends email to an external address and cannot be recalled. Confirm the address
character by character with the user before planning.

## Shares

A share makes datasets readable outside the project that owns them. Treat it as a
disclosure decision, not a configuration change.

```
plan(op="create_share", args={
  "project_id": "...", "name": "...", "shared_project_ids": ["proj_..."]})
plan(op="subscribe_share",   args={"project_id": "...", "share_id": "..."})
plan(op="unsubscribe_share", args={"project_id": "...", "share_id": "..."})
plan(op="delete_share",      args={"project_id": "...", "share_id": "..."})
```

Deleting a share removes subscribers' access to everything it exposed. Editing a live
share's conditions is not available here — it silently changes who can read what, so
it belongs in the web UI where the effect is visible.

Datasets arriving through a subscribed share show a `share` field and are read-only to
the subscribing project; do not try to modify or delete them there.

## Workspaces

```bash
cirro-agent read workspaces -p "My Project"
```

```
plan(op="create_workspace", args={
  "project_id": "...", "name": "...", "environment_id": "...",
  "mounted_datasets": ["ds_..."]})
plan(op="start_workspace",  args={"project_id": "...", "workspace_id": "..."})
plan(op="stop_workspace",   args={"project_id": "...", "workspace_id": "..."})
plan(op="delete_workspace", args={"project_id": "...", "workspace_id": "..."})
```

A **running** workspace accrues compute charges until it stops, so if the user is
finished with one, offer to stop it. Deleting destroys anything stored only inside it.

## Sample metadata schema

```
plan(op="update_project_schema", args={"project_id": "...", "schema": {...}})
```

The plan diffs the keys being added, removed and kept, and warns about removals, which
change how existing sample metadata validates. Read
`cirro-agent read samples -p P` first to see what is actually in use — removing a key
that samples populate is how metadata quietly stops validating.

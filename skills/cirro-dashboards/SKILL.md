---
name: cirro-dashboards
description: Build, read and edit Data Studio dashboards in Cirro -- charts, tables and filter controls over dataset files and sheets. Use when the user wants to create a dashboard, visualize or chart a dataset's results, see what a dashboard shows, add or change a chart, table or filter on one, rearrange its tiles, or delete one. Creating, editing and deleting require the user's approval.
---

# Data Studio dashboards

A dashboard is a small dataflow graph that the portal runs in the browser:

```
source (a dataset file or a sheet) -> query (DuckDB SQL) -> view (table or chart) -> tile
                                            ^
                              control (filter / {token} input)
```

You never write that graph by hand. `create_dashboard` and `edit_dashboard` take a list
of **changes** in plain terms, build the document the portal expects, validate it
against the portal's own schema, and **run its data locally in DuckDB** before showing
the plan. A plan that would render nothing -- a column that is not there, a chart given
the wrong kind of column, SQL that fails -- is refused with the reason, not written.

## 1. Look at the data first

```bash
cirro-agent read dashboards -p "My Project"                 # what already exists
cirro-agent read files -p "My Project" -d ds_...            # find a table to chart
cirro-agent read sheets -p "My Project"                     # sheets are sources too
cirro-agent read preview -p "My Project" -d ds_... --path results/de.csv
cirro-agent read preview -p "My Project" -d ds_... --path results/de.csv \
    --sql "SELECT condition, count(*) FROM de GROUP BY 1"
cirro-agent read dashboard-schema                           # charts, channels, options
```

`preview` loads a file exactly as a dashboard source would and reports each column's
**class** -- `quantitative`, `categorical` or `temporal`. Chart channels accept only
certain classes (a histogram's x must be quantitative; a bar's x categorical), so read
the classes before choosing a chart. The table is readable in SQL as `query` or by its
file's base name (`results/de.csv` -> `de`).

## 2. Create

```
plan(op="create_dashboard", args={
  "project_id": "proj_...", "name": "DE results", "description": "...",
  "changes": [
    {"op": "add_source", "id": "de", "dataset": "ds_...", "path": "results/de.csv"},
    {"op": "add_query", "id": "sig", "inputs": ["de"],
     "sql": "SELECT * FROM de WHERE padj < {alpha}"},
    {"op": "add_control", "id": "alpha", "kind": "number", "label": "Adjusted p",
     "target": {"query": "sig", "token": "alpha"}, "value": "0.05"},
    {"op": "add_view", "id": "volcano", "data": "sig", "type": "volcano",
     "x": "log2FoldChange", "y": "padj", "title": "Volcano", "w": 8,
     "options": {"pValueScale": "apply"}},
    {"op": "add_view", "id": "genes", "data": "sig", "title": "Significant genes", "w": 4},
    {"op": "add_control", "id": "cond", "kind": "select", "label": "Condition",
     "target": {"view": "genes", "column": "condition"}}
  ]})
```

- **ids** are optional, 4-32 lowercase letters/digits. Give them when later changes
  refer to the node; otherwise one is derived.
- **add_source** takes `dataset` (an ID -- names are refused) + `path`, or `sheet` (an
  ID). A source is a table, not a tile.
- **add_query** reads its `inputs` as `query` (the first), `in1`..`inN`, or a source by
  its table name. `{name}` is a token a control fills. Omit `sql` for `SELECT *`.
- **add_view** `type` is `table` (default) or a chart from `dashboard-schema`; bind
  columns with `x`, `y`, `color`, `size`, `detail`. `w` (1-12) and `h` size its tile;
  tiles pack left-to-right, top-to-bottom in the order given. `options` sets chart
  options (`xScale`, `showPoints`, `significanceP`, ...). A volcano's y is read as
  already -log10 unless `pValueScale` says otherwise -- pass `"apply"` for raw p-values.
- **add_control** `kind` is `text`, `number`, `select`, `multiselect` or `range`.
  `target` is `{query, token}` to fill a SQL token, or `{view, column}` to filter a
  view's rows. A select over a view column offers that column's values. Controls sit in
  the Filters bar at the top, not in tiles.

The plan text lists every node and tile it adds and, per view, the rows and column
classes it will show. Show it to the user as-is, then `apply`. The result carries the
dashboard's `url` -- give it to the user.

## 3. Read one

```bash
cirro-agent read dashboard <dashboard_id> -p "My Project"        # what it is made of
cirro-agent read dashboard <dashboard_id> -p "My Project" --run  # what each view draws
```

`--run` executes every query and reports each view's rows, columns and a few sample
rows. That is how you "see" a dashboard -- the portal only renders it in a browser. If
the user has a browser open to the dashboard, a screenshot shows the rest.

## 4. Edit

```
plan(op="edit_dashboard", args={
  "project_id": "proj_...", "dashboard_id": "...", "name": "optional new name",
  "changes": [
    {"op": "update_view", "node": "volcano", "color": "gene_biotype"},
    {"op": "update_query", "node": "sig", "sql": "SELECT * FROM de WHERE padj < {alpha} AND baseMean > 10"},
    {"op": "add_view", "data": "sig", "type": "histogram", "x": "log2FoldChange"},
    {"op": "place", "node": "genes", "x": 0, "y": 20, "w": 12, "h": 8},
    {"op": "remove", "node": "old1234"}
  ]})
```

Read the dashboard first so you refer to its real node ids. Also available:
`update_control`, `set_config` (merge raw config; null deletes a key), `connect` /
`disconnect` (`"node.port"`), `add_node`, `unplace`, `add_page`, `rename_page`,
`remove_page`. In `update_view`, a channel set to `null` is cleared, and changing `type`
keeps whichever bindings the new chart still has.

**Someone may have it open.** The portal auto-saves. If the dashboard is saved after the
plan was made, `apply` refuses with `PLAN_STALE` rather than discarding their work --
re-plan and show the new text. Conversely, an open portal tab reloads to show your
change when it has no unsaved edits of its own, and refuses to save over it when it does.

## 5. Delete

`plan(op="delete_dashboard", args={"project_id": ..., "dashboard_id": ...})` removes the
record for everyone. The data it reads is not touched.

## When a plan is refused

`INVALID_QUERY` names the change (`changes[3]`) and what is wrong with it; `issues`
lists every problem the validator or the data run found. Fix the change list and plan
again. Warnings (an empty view, a column with too many categories for a chart) do not
block the plan -- read them to the user.

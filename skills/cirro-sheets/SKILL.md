---
name: cirro-sheets
description: Create Cirro sheets and edit their columns and rows -- tables of samples, plate maps, tracking lists, metadata. Use when the user wants to make a sheet (from a description, from rows, or from a local CSV/TSV/Parquet/JSON file), add, change or delete rows in one, add, drop, rename or retype a column, rename a sheet, define a view over SQL, or delete a sheet. Every write requires the user's approval.
---

# Sheets

A sheet is a table the whole project shares: the portal edits it cell by cell,
dashboards chart it, and views are built on it. Every row has a `_row_id`, which is how
a write names a row. Cirro also keeps `sys_created_at` and `sys_updated_at` on every
row; nothing writes those but Cirro.

## 1. Read it first

```bash
cirro-agent read sheets -p "My Project"
cirro-agent read sheet <sheet_id> -p "My Project"                      # columns + 20 rows
cirro-agent read sheet <sheet_id> -p "My Project" --where '{"status": "failed"}' --limit 200
```

`read sheet` gives each column's type, whether it is required or unique, and an enum's
allowed values -- the rules a write must satisfy -- plus the rows with their `_row_id`.
`--where` takes the same filter the row operations do, so read with the filter you mean
to write with and you see exactly the rows it selects.

## 2. Create

```
plan(op="create_sheet", args={
  "project_id": "proj_...", "name": "Plate map", "description": "...",
  "columns": [
    {"name": "Well", "required": true, "unique": true},
    {"name": "sample_id"},
    {"name": "conc_ng_ul", "type": "DOUBLE", "label": "Concentration (ng/uL)"},
    {"name": "status", "values": ["pending", "done", "failed"]},
    {"name": "fastq", "kind": "CIRRO_FILE"}
  ],
  "rows": [{"well": "A1", "sample_id": "s1", "conc_ng_ul": 12.5, "status": "pending",
            "fastq": {"dataset": "ds_...", "path": "data/s1_R1.fastq.gz"}}]})
```

- **Column names** are SQL names: lowercase letters, digits and `_`. Give a label as the
  name (`"Well"`, `"Sample ID"`) and it is kept as the label while the name is derived
  (`well`, `sample_id`) -- rows use the derived name. The table name is derived from the
  sheet name the same way unless you pass `table_name`.
- **Types**: `STRING` (default), `INTEGER`, `BIGINT`, `DOUBLE`, `FLOAT`, `BOOLEAN`,
  `DATE`, `TIMESTAMP`. `values` makes an enum. `kind` is `URL`, `CIRRO_FILE`,
  `CIRRO_FOLDER` or `CIRRO_DATASET`; a Cirro reference cell takes
  `{"dataset": "ds_...", "path": "..."}` and becomes the URI Cirro stores.
- **From a file**: pass `"file": "/path/to/table.csv"` instead of `columns` and `rows`.
  Columns and types are read from every row; give `columns` as well to override them.
- **A view**: pass `view_sql` (a SELECT over the project's sheets) instead of columns.
  The plan runs it and shows the first rows. `"materialize": true` stores a copy that
  `refresh_sheet` updates; otherwise it runs on every read.

## 3. Rows

```
plan(op="insert_rows", args={"project_id": ..., "sheet_id": ..., "rows": [{...}, ...]})
plan(op="insert_rows", args={"project_id": ..., "sheet_id": ..., "file": "~/new.csv"})

plan(op="update_rows", args={"project_id": ..., "sheet_id": ...,
                             "where": {"status": "failed"}, "set": {"status": "pending"}})
plan(op="update_rows", args={"project_id": ..., "sheet_id": ...,
                             "updates": [{"row_id": 42, "values": {"conc_ng_ul": 9.1}},
                                         {"row_id": 43, "values": {"status": "done"}}]})

plan(op="delete_rows", args={"project_id": ..., "sheet_id": ..., "row_ids": [42, 43]})
```

- **`where`**: `{"col": value}` equals, `{"col": [a, b]}` in, `{"col": null}` is null,
  `{"col": {">=": 5}}` (`=`, `!=`, `>`, `>=`, `<`, `<=`, `like`, `not like`, `in`,
  `not in`). Several keys are ANDed. For OR, pass the backend's own shape:
  `{"logicalOperator": "OR", "conditions": [{"column": ..., "operator": "EQUALS",
  "values": [...]}, ...]}`. Or name rows with `row_ids`.
- One update or delete selects at most 10,000 rows. Narrow the filter or split the work.
- The plan lists each row's values before and after, or the rows it deletes. Show it to
  the user as-is. Rows that already hold the new values are left out.
- **Values are checked at plan time** and each is shown as it will be stored. A refused
  plan names the row and column of every value that would not fit -- fix those and plan
  again; nothing was written.
- **Someone may be editing.** If a selected row changes between plan and apply, `apply`
  refuses with `PLAN_STALE`. Re-plan and show the new text.

## 4. Change the sheet

```
plan(op="update_sheet", args={"project_id": ..., "sheet_id": ...,
  "name": "new name", "description": "...", "table_name": "new_table",
  "add_columns": [{"name": "notes"}],
  "drop_columns": ["old_col"],
  "alter_columns": {"conc": {"rename": "conc_ng_ul", "type": "DOUBLE",
                             "label": "Concentration", "values": [...], "hidden": true}}})
```

A new column cannot be required, and an existing one cannot become required or unique;
Cirro refuses both. For a view, change `view_sql` instead. The plan shows Cirro's own
dry run of the change and **lists the dashboards and views that read the sheet** --
tell the user that SQL, charts and filters naming a dropped or renamed column will stop
working. If the columns change between plan and apply, it is refused as stale.

## 5. Delete

`plan(op="delete_sheet", args={"project_id": ..., "sheet_id": ...})` drops the table
and every row for everyone, and lists what reads it. It cannot be undone.

Sheets Cirro maintains itself (system sheets) cannot be written here. To chart a sheet,
use cirro-dashboards (`add_source` with `sheet`).

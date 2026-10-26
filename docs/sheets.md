# Working with sheets

Claude can create a sheet, from a list of columns, from rows it has worked out, or from
a CSV, TSV, Parquet or JSON file on your computer, and change one: add, fix or remove
rows, and add, drop, rename or retype columns. It can also create a view, a sheet
defined by SQL over other sheets, and refresh one that is stored as a table.

**Every value is checked against its column before you approve.** The plan shows each
value as the sheet will store it — `"1,200"` as `1200`, `yes` as `true`, a timestamp in
UTC — and refuses one that would not fit, naming its row and column: a word in a number
column, a status that is not one of the allowed values, a duplicate in a unique column.
Cirro checks some of this too, but a value it cannot store comes back as a bare server
error that names neither.

**A change to rows shows the rows.** An update shows the values before and after for
the first ten rows it changes and counts the rest; a delete shows the first ten rows it
removes and counts the rest. If anyone edits one of those rows before you approve, the
change is refused rather than applied to values you did not see.

**A change to the sheet itself**, its columns or its table name, shows Cirro's own
preview of what it will do, and lists the dashboards and views that read the sheet,
since SQL or a chart that names a dropped column stops working. A column can only be
widened, from INTEGER to BIGINT or from FLOAT to DOUBLE; any other type change is
refused before you are asked. To change a column's type otherwise, add a new column and
copy the values across.

One plan can insert up to 100,000 rows, or update or delete up to 10,000. Inserts and
updates of more than 1,000 rows go in several requests. Each request is all or nothing,
but the whole is not: the plan says so, and a failure part-way says how many rows were
written. A delete always goes in one request.

Filling in a sheet row by row is the usual reason to
[approve a kind of change for a session](grants.md): `insert_rows` and `update_rows` can
both be granted.

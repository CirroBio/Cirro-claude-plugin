# Building dashboards

Claude can build a Data Studio dashboard from a description — *"a volcano plot of the
DE results with a p-value cutoff I can change, and the significant genes beside it"* —
and read or change the ones you already have.

It works in the same terms you do: a table to read from, a query over it, a chart or
table of the result, a filter. Before anything is saved, the plan **runs the
dashboard's data on your machine** with the same engine the portal uses (DuckDB), so
the approval tells you how many rows each chart will show and which columns it is
drawing — and a dashboard that would come out blank, because a column is missing or a
chart was given the wrong kind of column, is refused with the reason instead of saved.
Files over 500 MiB are not downloaded for that check; the plan says so.

A dashboard's queries are written by anyone in the project, so they run locked down: a
single SELECT against the files already downloaded for it, with no other file and no
network.

The dashboard's structure is checked against the schema your Cirro portal publishes at
`/schemas/dashboard-schema.json`, so it matches the portal that will draw it. A portal
that does not publish one yet is checked against a copy bundled with the plugin, and
the plan says which was used.

## If you have the dashboard open

Your tab picks up Claude's change when you have no unsaved edits, and warns you instead
of overwriting it when you do. The other direction holds too: if you save in the portal
after Claude has planned an edit, the edit is refused as stale rather than written over
your work.

The portal half of this needs a portal build that is not yet deployed everywhere; until
it reaches yours, an open tab can still overwrite an edit. See [Status](status.md).

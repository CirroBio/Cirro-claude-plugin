# Building dashboards

Claude can build a Data Studio dashboard from a description, such as *"a volcano plot
of the DE results with a p-value cutoff I can change, and the significant genes beside
it"*, and read or change the ones you already have.

It works in the same terms you do: a table to read from, a query over it, a chart or
table of the result, a filter. Before anything is saved, the plan **runs the
dashboard's data on your machine** with the same engine the portal uses (DuckDB), so
the approval tells you how many rows each chart will show and which columns it is
drawing. A dashboard that would come out blank, because a column is missing or a chart
was given the wrong kind of column, is refused with the reason instead of saved. You can
ask Claude to skip this check, and the plan then says it was skipped.

To run that check, and whenever Claude runs a dashboard's data to show you what it
draws, the plugin downloads the dashboard's source files into a cache in your login
folder, without asking. Files over 500 MiB are skipped, and the plan says so.

A dashboard's queries are written by anyone in the project, so they run locked down: a
single SELECT that can read only that download cache, with no other file and no
network.

The dashboard's structure is checked against what your Cirro portal says it can draw,
so it matches the portal that will draw it. A portal too old to say is checked against a
copy bundled with the plugin, and the plan says so.

## If you have the dashboard open

Your tab picks up Claude's change when you have no unsaved edits, and warns you instead
of overwriting it when you do. The other direction holds too: if you save in the portal
after Claude has planned an edit, the edit is refused as stale rather than written over
your work.

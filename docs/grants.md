# Approving a kind of change

When you are making many changes of one kind — filling in a sheet, renaming a batch of
datasets, launching runs one after another — you can stop being asked about that kind
of change in one project for the rest of the session. For a series of sheet inserts it
goes like this:

1. **The first insert** prompts as usual.
2. **The second** comes with one extra line from Claude under the plan: whether to stop
   asking about `insert_rows` in this project. Say no and nothing changes, and Claude
   will not ask again. Say yes and the prompt for this insert carries a second line,
   `also_approves`, naming exactly what else you are approving:

    ```
    change         insert_rows: Insert 12 row(s) into sheet 'Plate map' (sheet_8c1f,
                   table lab.plate_map) in Immuno-Oncology Core.
    also_approves  every later insert_rows in project 'Immuno-Oncology Core'
                   (proj_8a2c), without asking, until this session ends
    ```

3. **Every insert after that** is still planned and shown to you, but is made without a
   prompt. Claude Code also shows a notice for each one —
   `Cirro, approved in advance this session: insert_rows: Insert 3 row(s) ...` — so a
   change made under a grant is never silent, even when a subagent made it. Everything
   else still asks.

You do not have to wait for the offer. Tell Claude at any point — "stop asking me about
new sheet rows in Immuno-Oncology Core" — and it asks for a grant directly, with one
prompt that says what it approves and for how long.

## What can be granted

Only six operations can be granted: `insert_rows`, `update_rows`, `update_dataset`,
`update_sample`, `run_analysis` and `resume_analysis`. Nothing that deletes anything or
changes who has access can be, however you ask. Approve one deletion and ask for
another, and Claude tells you once that deletions always ask, rather than leaving you
to wonder where the offer went.

Even among those six, a change that removes something or reaches far still prompts
under a grant, and the plan says why:

- retyping a dataset (it re-derives the project's sample records), removing tags, or
  taking datasets out of a folder
- deleting a sample's metadata keys or blanking their values
- blanking sheet cells, or inserting rows from a file on your computer
- a launch that emails addresses it was given rather than you
- one plan touching more than 50 datasets or rows

## How long a grant lasts

A grant covers the operations and the one project it names, as the Cirro user and in the
session folder it was given under. It lasts until the Claude session ends — it is held
in memory by the plugin's server for this session and never written to disk — or until
you ask Claude to revoke it. Reconnecting the plugin's server with `/mcp` also ends it.
Ask Claude what you have granted at any point and it will list them.

Each change a grant approves is still re-checked against current facts when it is made,
is still single-use, and is logged in `.cirro/runs/` together with the grant that
approved it.

!!! warning "Know what you are giving up"
    While a grant is in place, a change of that kind in that project runs without asking
    even if something Claude read — a dataset description, a cell in a sheet — is what
    steered it there. Grant narrowly, and revoke when you are done.

Session grants are new in 0.14.0 and have not yet run against a live tenant — see
[Status](status.md).

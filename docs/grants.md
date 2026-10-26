# Approving a kind of change

When you are making many changes of one kind, such as filling in a sheet, renaming a
batch of datasets or launching runs one after another, you can stop being asked about
that kind of change in one project for the rest of the Claude Code session. This is
called a *grant*. For a series of sheet inserts it goes like this:

1. **The first insert** asks for your approval as usual.
2. **The second** comes with an offer from Claude under the plan: should it stop asking
   about `insert_rows` in this project? Say no and nothing changes, and Claude will not
   offer again. Say yes and the approval prompt for this insert carries a second line,
   `also_approves`, naming exactly what else you are approving:

    ```
    change         insert_rows: Insert 12 row(s) into sheet 'Plate map' (sheet_8c1f,
                   table lab.plate_map) in Immuno-Oncology Core.
    also_approves  every later insert_rows in project 'Immuno-Oncology Core'
                   (proj_8a2c), without asking, until this session ends
    ```

3. **Every insert after that** is still planned and shown to you, but is made without a
   prompt. Claude Code also shows a notice for each one,
   `Cirro, approved in advance this session: insert_rows: Insert 3 row(s) ...`, so a
   change made under a grant is never silent. The notice appears as the change starts,
   so a change that is then refused still shows one. Everything else still asks.

You do not have to wait for the offer. Tell Claude at any point, *"stop asking me about
new sheet rows in Immuno-Oncology Core"*, and it asks for a grant directly, with one
prompt that says what it approves and for how long. One grant can name several kinds of
change.

## What can be granted

Only six kinds of change can be granted: `insert_rows`, `update_rows`,
`update_dataset`, `update_sample`, `run_analysis` and `resume_analysis`. Nothing that
deletes anything or changes who has access can be, however you ask, and a chain of
steps is never covered by a grant. Approve one deletion and ask for another, and Claude
tells you once that deletions always ask, rather than leaving you to wonder where the
offer went.

Even among those six, some changes still ask for your approval under a grant, and
Claude tells you why:

- an edit to more than 50 datasets, or an update to more than 50 sheet rows, in one plan
- changing a dataset's data type (it re-derives the project's sample records), removing
  tags, or taking datasets out of a folder
- deleting a sample's metadata keys or blanking their values
- blanking sheet cells, or inserting rows from a file on your computer
- a launch given its own list of addresses to email; leaving the default, which emails
  you, is covered

Inserting rows and launching analyses have no size limit under a grant: one plan can
insert many rows, or launch on many datasets, without a prompt. Grant those narrowly.

## How long a grant lasts

A grant covers the kinds of change and the one project it names, as your Cirro account
and with the login it was given under. It lasts until the Claude Code session ends, or
until you ask Claude to revoke it. Ask Claude what you have granted at any point and it
lists them. Reconnecting the plugin's server with `/mcp` also ends every grant, because
grants are held in that server's memory and never stored on disk as an approval.
Whether clearing the conversation with `/clear` ends them has not been checked; if it
matters, revoke them first.

Each change a grant approves is still checked again against current facts when it is
made, is still single use, and is recorded in `.cirro/runs/` together with the grant
that approved it.

!!! warning "Know what you are giving up"
    While a grant is in place, a change of that kind in that project runs without asking
    even if something Claude read, such as a dataset description or a cell in a sheet, is
    what steered it there. Grant narrowly, and revoke when you are done.

Grants are new in 0.14.0 and have not yet run against a live tenant; see
[Status](status.md).

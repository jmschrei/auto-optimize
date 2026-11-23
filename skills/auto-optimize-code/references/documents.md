# The GitHub documents: what every one carries

Step 5 (`references/step5-ship.md`) can open three documents on the
original's GitHub repository, each from its own folder under `$WORK/ship/`:

| Document | Folder | Rendered by | Opened by | Reference |
|---|---|---|---|---|
| the optimization PR | `pr-optimization/` | `opt_pr_body.py` | `autoopt.py pr` | `references/pr-optimization.md` |
| the unit-test PR | `pr-tests/` | `tests_pr_body.py` | `tests_pr_push.py` | `references/pr-tests.md` |
| the bug report | `issue-bugs/` | `issue_body.py` | `issue_body.py file` | `references/issue-bugs.md` |

`ship.py rehearse` and `ship.py post` run each opener; none is run on its
own except to repair one post.

## What every document holds

- **Only what the search recorded.** Every number comes from a table a script
  wrote from the search's records: medians and ranges of runs, the paired
  timing at the end, each output check's worst value, the test counts, the
  coverage step 1 measured. No document mentions a measurement the search did
  not make.
- **A title** of at most 72 characters, starting with a capital letter, with
  no closing period, stored beside the body as `body.title`. Nothing is put in
  front of it.
- **A body that is rendered, never edited.** A script writes the tables, the
  writer writes `prose.md` (fields that each start with `=== <field>`), and
  the renderer joins them; the same inputs give the same bytes. A change goes
  to the tables or the prose and is rendered again. Prose writes a
  `{{token}}` where it needs a number a table gives; a renderer refuses the
  number typed.
- **The closing line** `🤖 Generated with [Claude Code](https://claude.com/claude-code)`,
  added by the renderer. The bug report's first paragraph also says that
  Claude wrote it.

## What no document says

`lint_text.py` refuses these in every title, body, commit message and comment
a branch adds, and so does every renderer:

- the tool or the lab: no document names auto-optimize or the laboratory that
  made it;
- the search's own words: winner, winning or kept iteration, baseline, gate,
  round N, "the search", "the base" for the original branch, and iteration
  labels such as A12. Say what the change does, name the target branch
  (`main`), and say what a check compares;
- a path on this machine (`~/`, `/home/`, `/tmp/`, `$WORK`).

## Numbers that do not exist yet

The bug report is posted first and the unit-test PR next, so a document
drafted before them holds the stand-in `#<bug issue>` or `#<tests PR>`. A
renderer fills each from `issue-bugs/filed.json` or `pr-tests/pr.json` once
it exists, and `ship.py post` renders each document again just before posting
it. A preview accepts a stand-in; a post refuses one. A document that was not
chosen (`after.ship_documents`) is never named: a renderer leaves out the
sentence that would name it and refuses a stand-in for it.

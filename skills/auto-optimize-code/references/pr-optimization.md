# The optimization PR

One PR on the original's branch, with one commit per kept change, read by
reviewers who never saw the search. Its folder is
`D=$WORK/ship/pr-optimization`. `references/documents.md` has what every
document holds and what none may say; posting is `ship.py post`
(`references/step5-ship.md`).

## The tables

```bash
python3 $AO/opt_pr_tables.py $WORK          # --id A7 for another iteration than the winner
```

It writes, from the search's records only:

| File | Holds |
|---|---|
| `branch.json` | the target branch, the commit the PR builds on, the kept changes in order, whether a unit-test suite sits under the search, the documents chosen at step 5 |
| `changes.tsv` | one row per kept change: id, title, files, +, - |
| `files.tsv` | one row per file the PR changes, the suite's files left out |
| `performance.tsv` | the objective and each observer: median, min, max and runs of the first measurement (the target branch) and of the kept code |
| `gates.tsv` | each output check: name, op, threshold, worst value, runs |
| `checks.json` | the separate output comparison's summary, and the first call's time on both |
| `paired.json`, `machine.json` | the paired timing `end` made, and the machine it ran on; null when the search has none |
| `tests.json` | the unit tests' result on the kept code, with the counts read from their log |
| `fragments.json` | what each kept change's notes say about it (`## Change`), for the template |
| `messages/<id>.txt` | each kept change's commit message, from its `## Commit message` with `after.commit_trailer`; a missing one is a skeleton to write |

The counts come from a pytest, testthat or "N passed" summary in the log.
Another runner's summary needs `--tests-format KEY=REGEX` per count (KEY one
of `passed`, `failed`, `skipped`, `errors` or `total`; one group, the
count). Without one the PR says the tests passed and gives no counts; never
type them.

## The prose

```bash
python3 $AO/opt_pr_body.py $D --template > $D/prose.md    # once
python3 $AO/opt_pr_body.py $D --title '<imperative>' --out $D/body.md
```

`--template` explains each field it lists:

| Field | Holds |
|---|---|
| `summary` | one or two sentences: what the PR changes in the code |
| `input` | the main input, as it follows "On" |
| `row <measure>` | the Measure cell of each row of the Performance table |
| `timed` | what one run measures |
| `machine` | only when the search recorded none |
| `outliers` | each row made worse beyond the run-to-run spread, and why; required then |
| `cold` | why the first call takes longer; required when it takes more than twice as long |
| `portability` | the OS-specific calls and the constants fitted to one machine, or that there are none |
| `vendored` | code from another project, or `none` |
| `change <name>` | one entry per change: a first line `iterations: A4, A11` (never printed), then what it does |
| `behaviour` | every difference a user can see, or that there is none |
| `tests` | the test suite and how it was run, as the subject of "passed on this branch" |
| `gate <name>` | what each output check compares |
| `compare` | the inputs of the separate output comparison |

**What the reader sees.** The opening paragraph is the summary, then the
measured change on the main input (and the paired timing, when made), then
a link to *How the output was checked*. Then portability, what the PR
targets and holds, *Performance* (the table, the first call, and a collapsed
*How it was measured* with the machine, the ranges and the paired runs),
*Changes* (each change's name and commits; its files and description
collapsed), *Behaviour changes*, *Tests*, and *How the output was checked*:
each output check with its worst value, the separate comparison, the unit
tests, and the bug report when it is chosen.

**Each `change` entry is one change as it stands in this branch.** Read the
diff of every kept change it covers (`git -C $WORK/autooptimize/repo.git
diff autoopt/<parent>..autoopt/<id>`) and the net diff of its files.
Changes that refine one idea share an entry; one later undone is described
where it ended up. Every kept change is in exactly one entry and every file
the PR changes is named by one. Where a README claims something no diff
shows, write what the diff shows and tell the user.

**Prose types no number a table gives.** Write `{{first}}`, `{{kept}}`
(the objective's median on the target and on this branch), `{{speedup}}`,
`{{runs}}`, `{{commits}}`, `{{files}}` or `{{target}}` instead; the renderer
refuses a number of three or more digits it computed.

## The commit messages

One file per kept change, `messages/<id>.txt`, in the repository's own style
(`git -C $WORK/autooptimize/repo.git log --oneline -20`): an imperative
subject of at most 72 characters with no closing period, a blank line, then
what the change does and what stays the same, wrapped at 72 columns, ending
with `after.commit_trailer` when one is set. `autoopt.py pr` refuses a
message that breaks this or names an iteration label.

## The preview

```bash
python3 $AO/autoopt.py pr $WORK          # what `ship.py rehearse` runs
```

It checks that the winner passed every check, that its branch is the commit
the runner measured, that `body.md` is what the tables and prose render to,
and the commit messages; makes the commits in the search's `repo.git` (left
at `refs/pr/<id>`); and prints the commits, the diffstat, the push target,
the title, every message and the body. The branch it pushes is named from the
title, after `auto-optimize/` when the user chose that prefix
(`after.branch_prefix`; `ship.py rehearse` and `post` with `--branch optimization=NAME` for another name). Read the diffstat: a file no change should
have touched means scratch work was committed; stop and tell the user. With a
unit-test suite the commits sit on the original's commit
(`references/step5-ship.md`, *Separate from the unit tests*).

`--post` (run by `ship.py post`) pushes the branch, opens the PR with `gh`,
and records it in `$D/pr.json`; a second post opens nothing. Once the PR
is open, the plain preview prints the title and body that `--edit` would put
on it when they changed; show the user, and on their yes `--edit` replaces
them and pushes nothing. `--edit` refuses when the winner changed after the
PR was opened, since the PR would describe commits it does not carry.

## When it refuses

| Refusal | Do |
|---|---|
| `body.md is not what the tables and prose render to` | render it again; never edit `body.md` |
| `no "=== change" field covers A4`, `no change entry names <file>` | add the change to an entry |
| `types 1.51, which the tables give` | the token, or leave the number to the table |
| `the commit messages are refused` | fix each file it names |
| `the commits … are not the measured change outside the unit-test suite's files`, `changes …, which the unit-test suite adds` | the optimization cannot be separated from the suite: tell the user, and offer the unit-test PR alone, or a local branch (`ship.py branch`, which has the same rule) built by hand from the search's `autoopt/<id>` |
| `is not a GitHub URL` | the original is not on GitHub: `ship.py branch` |

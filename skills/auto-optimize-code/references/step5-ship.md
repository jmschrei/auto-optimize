# Step 5 — shipping: the change back where it came from

The search is over once `end` has run (`references/step4-final.md`). Nothing
of the winner has reached the user's code yet: the original was never written
to. Shipping is a step of its own, and it starts with a question to the user.
Step 6 (`references/step6-finish.md`) follows whatever they choose.

## Ask how to ship

```bash
python3 $AO/ship.py plan $WORK
```

prints what this search can ship (plain or repository mode, the GitHub
repository and whether `gh` can push there, whether step 1 wrote a unit-test
suite, the bugs step 1 recorded) and the options to offer. Ask in one
AskUserQuestion call; on GitHub, add the checklist as a second question of
the same call:

| Mode | "How do you want to ship?" |
|---|---|
| plain (`init --copy`) | *Apply the change to the original file(s)* (Recommended); *Not now* |
| repository, on GitHub | *Open GitHub documents* (Recommended); *A branch in your local repository*; *Not now* |
| repository, elsewhere | *A branch in your local repository*; *Not now* |

"Which documents?" (multiSelect), on *Open GitHub documents*: *The
optimization PR*; *The unit-test PR*, only when step 1 wrote a suite; *The
bug report*, only when step 1 recorded bugs.

Record the answers in the user's words, then follow the matching section:

```bash
python3 $AO/autoopt.py config $WORK --set after.ship=<apply|branch|github|none> --reason "<their words>"
python3 $AO/autoopt.py config $WORK --set after.ship_documents=optimization,tests,bugs --reason "<their words>"   # GitHub only, what they checked
```

*Not now* (`none`) goes straight to step 6; the user can ask later, and
`ship.py plan` asks again. `stages.py next` names the move at every point of
this step.

## Apply the change (plain mode)

```bash
python3 $AO/ship.py apply $WORK            # preview: the files, the diffstat; writes nothing
python3 $AO/ship.py apply $WORK --yes      # on the user's yes
```

Each file the winner changed is written with its measured content where it
came from (`init` records each `--copy` source in `config.json` as
`copied`); a file the change adds goes beside them. Files the search wrote
itself, such as the benchmark, are left out and named. It refuses, writing
nothing, when a file changed since the search copied it (the change was
measured on the copy), a file it would add exists, or one it would change is
missing; a file that already holds the change is reported as applied. A
search made before `init` kept the record needs `--to DIR`, the folder the
copied files are in. `ship/applied.json` and `ship/applied.patch` record what
was written.

## A branch in the local repository

```bash
python3 $AO/ship.py branch $WORK [--name N]          # preview: the commits, the diffstat
python3 $AO/ship.py branch $WORK [--name N] --yes    # on the user's yes
```

One commit per kept change, each with the `## Commit message` its notes
hold (its title when it has none), made in the search's `repo.git` and
fetched into the original as a new branch (`optimized-<date>` by default). It
refuses a name that exists and never checks the branch out. With a suite from
step 1, the commits sit on the original's commit and leave the suite out
(*Separate from the unit tests*, below). `--repo PATH` names the original
when the search does not record it. `ship/branch.json` records it.

## GitHub documents

| Document | Reference | Folder |
|---|---|---|
| the optimization PR | `references/pr-optimization.md` | `$WORK/ship/pr-optimization` |
| the unit-test PR | `references/pr-tests.md` | `$WORK/ship/pr-tests` |
| the bug report | `references/issue-bugs.md` | `$WORK/ship/issue-bugs` |

What every document carries, and what none may say:
`references/documents.md`.

1. **The writer.** Spawn one subagent with the prompt `python3 $AO/ship.py
   brief $WORK` prints. It writes the inputs of every chosen document (tables,
   prose, commit messages; bugs.tsv and the verify runs for a bug report),
   renders each with a title, and ends when `ship.py rehearse` exits 0.
2. **Review, then post all.** All documents are reviewed together and posted
   together; none is posted on its own.

   ```bash
   python3 $AO/ship.py rehearse $WORK      # renders each, runs each one's preview, prints the digest
   python3 $AO/ship.py status $WORK        # where each stands
   ```

   Show the user every body (`ship/<folder>/body.md`) and preview
   (`ship/rehearsal/<document>.txt`) together: titles, the repository and
   branch each opens on, the commits. A change they ask for goes to the
   writer (SendMessage while it lives); rehearse again and show the new
   batch, saying what changed. Only once they ask for posting:

   ```bash
   python3 $AO/ship.py post $WORK all --approved --digest <digest> [--other-owner]
   ```

   It refuses a digest other than the last rehearsal's, a rehearsal with
   problems, and inputs changed since. It posts in a fixed order, the bug
   report, the unit-test PR, then the optimization PR, rendering each again
   first so it holds the numbers of the ones before it. `--other-owner` is
   given only once the user has confirmed a repository another account owns
   (an upstream project; the bug report always opens there). When a post
   stops, fix the cause and run the same command: what was posted is
   skipped.

Each posted document holds the search's `end --reopen` (`reopen-lock.json`),
and so do `apply --yes` and `branch --yes`.

### From a fork

When `ship.py plan` says `gh` cannot push to the repository, the PRs come
from the user's fork. Ask for it (create it with `gh repo fork OWNER/REPO
--clone=false` on their yes), then record it once:

```bash
python3 $AO/autoopt.py config $WORK --set after.pr_remote=<remote name or URL> --set after.pr_head_owner=<their account> --reason "<their words>"
```

Branches are pushed to the fork and the PRs open on the original with head
`OWNER:BRANCH`. The bug report opens on the original either way.

### Separate from the unit tests

With a suite from step 1, the search's code is the original plus the suite.
The optimization PR (and a local branch) holds only the optimization: each
kept change is applied to the original's commit, a kept change that touches
a file the suite adds is refused, and the commits' change must equal the
measured change outside the suite's files. Both PRs open on the original's
branch and merge in either order; the optimization PR says it was measured
with the tests in place.

### When the branch moved

The commits sit on the commit the search copied. When the target branch has
moved since, the preview says how far; GitHub reports any conflict. Do not
rebase before posting: the rebased code was never measured. Tell the user,
who may rebase the open PR themselves and run the repository's tests and
benchmark on the result.

## Refusals

| Refusal | Do |
|---|---|
| `has not ended` | step 4 first |
| `is gate-failed …`, `something was committed after the measurement`, `uncommitted changes that were never measured` | ship the winner (`--id best`); a change made after the measurement is measured as an iteration made with `--after-end` |
| `changed since the search copied it` (apply) | ask the user: their edit stays, and the change is applied by hand (`ship/applied.patch` is written only after a write; `git -C $WORK/autooptimize/repo.git diff autoopt/0 autoopt/<id>` prints it) |
| `is not on <owner/repo>/<branch> … push it first` | the user pushes their branch; the search never writes to it |
| `is not <login>'s own account` | show the user the repository, then `--other-owner` |
| `branch … already exists` | `--branch` (PR) or `--name` (local branch) with another name |
| `the inputs changed since the rehearsal` | rehearse again and show the user the new batch |
| a document's own refusal | its reference, *When it refuses* or the renderer's list |

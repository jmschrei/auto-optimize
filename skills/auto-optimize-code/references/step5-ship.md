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
suite, the bugs step 1 recorded) and the questions to ask (`--json` prints
the same for a program). Ask them in one
AskUserQuestion call: how to ship, then, for a repository search, the
documents and the branch names, whose answers apply only to the route that
uses them:

| Mode | "How do you want to ship?" |
|---|---|
| plain (`init --copy`) | *Apply the change to the original file(s)* (Recommended); *Not now* |
| repository, on GitHub | *Open GitHub documents* (Recommended); *A branch in your local repository*; *Not now* |
| repository, elsewhere | *A branch in your local repository*; *Not now* |

"Which documents?" (multiSelect), on *Open GitHub documents*: *The
optimization PR*; *The unit-test PR*, only when step 1 wrote a suite; *The
bug report*, only when step 1 recorded bugs.

"Start the branch names with `auto-optimize/`?", for a branch or a PR: *No*
or *Yes*, each option showing the names it gives (`ship.py plan` prints
them). It names every branch shipping makes or pushes:

| Branch | No | Yes |
|---|---|---|
| the optimization PR's | from its title (`vectorize-column-scoring`) | `auto-optimize/vectorize-column-scoring` |
| the unit-test PR's | `unit-tests-<date>` | step 1's own name, `auto-optimize/<date>-unit-tests` |
| a local branch | `optimized-<date>` | `auto-optimize/optimized-<date>` |

Record the answers in the user's words, then follow the matching section:

```bash
python3 $AO/autoopt.py config $WORK --set after.ship=<apply|branch|github|none> --reason "<their words>"
python3 $AO/autoopt.py config $WORK --set after.ship_documents=optimization,tests,bugs --reason "<their words>"   # GitHub only, what they checked
python3 $AO/autoopt.py config $WORK --set after.branch_prefix=<no|yes> --reason "<their words>"   # a branch or GitHub
```

`ship.py branch` and `ship.py rehearse` refuse until `after.branch_prefix`
is answered. An answer this search cannot ship (a route its mode does not
have, a document with nothing behind it) counts as no answer: `stages.py next`
asks again.

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
missing. A file that already holds the change is reported as applied, and
`--yes` records it so (applied by hand, say). After an iteration made with
`--after-end` becomes the winner, `apply` runs again over the files the
first apply wrote, and puts back the ones the new winner leaves alone. A
search made before `init` kept the record needs `--to DIR`, the folder the
copied files are in. `ship/applied.json` records what was written and what
held the change already, and `ship/applied.patch` is the whole change the
original files now hold; applying the same winner again keeps that record. `apply` and `branch` ship the winner; `--id` names another
iteration, which the user must have asked for.

## A branch in the local repository

```bash
python3 $AO/ship.py branch $WORK [--name N]          # preview: the commits, the diffstat
python3 $AO/ship.py branch $WORK [--name N] --yes    # on the user's yes
```

One commit per kept change, each with the `## Commit message` its notes
hold (its title when it has none), made in the search's `repo.git` and
fetched into the original as a new branch (`optimized-<date>` by default,
`auto-optimize/optimized-<date>` with the prefix). It
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

The route has two parts, in this order: the document writer drafts every
chosen document, then the user reviews them all and they are posted
together; none is posted on its own.

### The document writer

Spawn one subagent, the document writer, with the prompt `python3
$AO/ship.py brief $WORK` prints. It writes the inputs of every chosen
document (tables, prose, commit messages; for a bug report, bugs.tsv and each
reproduction rerun by `issue_body.py verify`), renders each with a title, and
ends when `ship.py rehearse` exits 0. Its report gives each title and the
rehearsal's digest. When it is gone before it reported (a closed session), spawn
a new one from the same prompt: the prompt has it keep and complete the inputs
already written in each document's folder.

### Review, then post all

```bash
python3 $AO/ship.py rehearse $WORK      # renders each, runs each one's preview, prints the digest
python3 $AO/ship.py status $WORK        # where each stands, and the last rehearsal's digest
python3 $AO/ship.py render $WORK [--only DOCUMENT]   # render again without the previews
```

Show the user every body (`ship/<folder>/body.md`) and preview
(`ship/rehearsal/<document>.txt`) together: titles, the repository and
branch each opens on, the commits. A change they ask for goes to the
document writer (SendMessage while it lives); rehearse again and show the new
batch, saying what changed. Only once they ask for posting:

```bash
python3 $AO/ship.py post $WORK all --approved --digest <digest> [--branch DOC=NAME …] [--other-owner]
```

The digest covers the winner, every document's inputs and the answers that
name what is pushed (`after.branch_prefix`, `after.pr_remote`, `after.pr_head_owner`, and
`--branch optimization=NAME` or `tests=NAME`, a PR branch named by hand,
given to `rehearse` and then to `post` the same). It
refuses a digest other than the last rehearsal's, a rehearsal with problems,
and inputs changed since. It posts in a fixed order, the bug report, the
unit-test PR, then the optimization PR, rendering each again first so it
holds the numbers of the ones before it. `--other-owner` is given only once
the user has confirmed a repository another account owns (an upstream
project; the bug report always opens there). When a post stops, fix the
cause and run the same command: what was posted is skipped.

### What holds the search closed

Each posted document holds the search's `end --reopen` (a hold in
`reopen-lock.json`), and so do `apply --yes` and `branch --yes`. A change to a
shipped winner is an iteration made with `--after-end`
(`references/step4-final.md`, *An iteration made after the end*); once it is
the winner, the ship stage reads `running` again until it is applied or
branched too.

### From a fork

When `ship.py plan` says `gh` cannot push to the repository, the PRs come
from the user's fork. Ask for it (create it with `gh repo fork OWNER/REPO
--clone=false` on their yes), then record it once:

```bash
python3 $AO/autoopt.py config $WORK --set after.pr_remote=<remote name or URL> --set after.pr_head_owner=<their account> --reason "<their words>"
```

Record the fork's URL rather than a remote name: `gh repo fork
--clone=false` adds no remote, and a name added to one clone is unknown to
the others. Branches are pushed to the fork and the PRs open on the original
with head `OWNER:BRANCH`. The bug report opens on the original either way.

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
| `changed since the search copied it` (apply) | ask the user: their edit stays, and the change is applied by hand (`git -C $WORK/autooptimize/repo.git diff autoopt/0 autoopt/<id>` prints it). Once they say each file holds it, `apply --by-hand` previews the files as applied by hand and `apply --by-hand --yes` records them, writing nothing |
| `exists already, and the change adds it` (apply) | show the user the file; the change is applied by hand as above |
| `is missing; the search copied it from there` (apply) | show the user where the file was; they restore it, or apply the change elsewhere and pass `--to DIR` |
| `is new, and the copied files came from more than one folder` (apply) | ask the user which folder the new file belongs in: `--to DIR` |
| `the change touches none of the copied files` (apply) | the winner changed only files the search wrote itself (the benchmark); tell the user there is nothing to ship |
| `after.ship is …: the original files are written only on the user's choice` (apply), `… a branch is made in the original repository only on the user's choice` (branch) | ask with `ship.py plan` and record the route (`config --set after.ship=…`) |
| `this is a repository search`, `this search was made with init --copy` | the route does not fit the mode: ask again with `ship.py plan` |
| `the user has not said whether the branch names start with auto-optimize/` | ask the third question of `ship.py plan` and record `after.branch_prefix`; a branch name the user gave (`--name`) needs no answer |
| `holds the tables of …, not of …` (optimization PR) | the winner changed since the tables were written (an iteration made with `--after-end`): run `opt_pr_tables.py` again and have the writer render the body again |
| `after.ship is …: the GitHub documents are for a user who chose them`, `after.ship_documents names no document`, `after.ship_documents names …, which this search does not offer` | ask with `ship.py plan` and record the answers |
| `is not on <owner/repo>/<branch>, so the PR would carry commits nobody measured. Push … first` | the user pushes their branch; the search never writes to it |
| `is not <login>'s own account` | show the user the repository, then `--other-owner` |
| `already has a branch <name>: pass --name` (local branch) | `--name` with a name that is not taken |
| `branch … already exists on <owner/repo>; nothing was pushed` (a PR) | another name: `rehearse --branch optimization=NAME` (or `tests=NAME`), show the user, then `post` with the same flag. A branch that already holds this PR's commits is opened, not refused, so a post that stopped after its push runs again cleanly |
| `--branch takes optimization=NAME or tests=NAME`; `--branch … is not what the rehearsal the user reviewed pushed` | give `post` the `--branch` flags the rehearsal had, or rehearse again with the new ones |
| `no rehearsal`, `is not the last rehearsal's`, `the last rehearsal had problems` | `ship.py rehearse`, then show the user the batch it rendered and use its digest |
| `the documents' inputs changed since the rehearsal the user reviewed` (their files, or the branch and fork answers), `the chosen documents changed since the rehearsal` | rehearse again and show the user the new batch |
| `was not posted; fix the cause and run the same command` | fix what the line above it names, then the same `post`: what was posted is skipped |
| a document's own refusal | its reference: `references/pr-optimization.md`, *When it refuses*; for the others, the renderer's list of problems on stderr |

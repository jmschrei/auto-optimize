# Step 5 — finishing a search

Step 5 follows `end` (`references/step4-final.md`). Section 3 waits while
another tool still works in the search directory.

## 1. Copy the winner out

The winner is what `--id best` names: `ended.json`'s, which an iteration made
with `--after-end` and promoted replaces. It is the code on its branch. Copy
the change and the code to `$WORK/finish/archive/` (or outside `$WORK`, when
the user wants it there), because section 3 deletes most of what is in
`autooptimize/`:

```bash
python3 $AO/autoopt.py archive $WORK        # --id <it> for another iteration; --dest DIR outside $WORK; --force to write over an archive
```

It writes `idea.patch`, `code/`, the README and `result.json`, and the report's skeleton (section 2) with *What was optimized*, *Result* and *The path* filled in and `<<FILL: …>>` marks left.

Quote the iteration's median, never the single run in `best_run`.

## 2. Write the final report

`$WORK/finish/final-report.md`, outside `autooptimize/`, self-contained.
`archive` wrote the skeleton: headings *What was optimized*, *Result*, *The path*, *What produced the gains*, *What was tried and failed*, *Compute*, *Where things are*, in that order.

| Heading | Holds |
|---|---|
| *What was optimized* | written by `archive` |
| *Result* | the baseline's and the winner's median with the spread across seeds on both, and the change in the objective's units; `final.json`'s verdict beside them, labeled "paired", when the search has a timing probe |
| *The path* | written by `archive` from `path --markdown` |
| *What produced the gains* | the gain by category, and one line per iteration on the path; the category breakdown goes in `$WORK/finish/speedup-breakdown/` |
| *What was tried and failed* | the closed ideas worth knowing, from `ideas.md` and the ledger's `[closed]` entries |
| *Compute* | iterations, runs, wall clock, and GPU or CPU hours; the dashboard's sidebar has throughput and per-category numbers |
| *Where things are* | the copied code and patch, the search directory, and anything published from it |

`rounds/*.md` and the READMEs of the frontier path are the material.

## 3. Reclaim the disk

`autoopt.py end` already did the automatic clean-up, and `references/step4-final.md`, *The clean-up*, lists what goes and what stays. What is left is what `--all` prunes once the winner is copied out:

```bash
python3 $AO/autoopt.py prune $WORK --all --worktrees         # report only
```

Show the user the report (what goes, the disk it frees, how many checkouts go). Run it again with `--apply` only after they agree; there is no undo. The report and the apply MUST carry the same flags: without `--worktrees` the report omits the checkouts the apply removes.

Without `--all`, the frontier path, every root, anything running and the 20 most recent (`--keep-recent N`) are protected; with it only a running iteration is spared. Each run keeps `metrics.json`, `log.txt` and `commit`; its artifacts, the iteration's `cache/` and the trees under `check/` go. `--worktrees` also removes the checkouts and carries the record (`meta.json`, `result.json`, `README.md`, `notes.md`, kept run files) out and back, so `status` and the dashboard still show every iteration; `restore --id A7` brings a checkout back. The branch still holds a pruned iteration's code. `prune` touches only `autooptimize/`.

**Saved output arrays are deleted from every settled iteration, protected ones included**: the files ending in `--outputs-ext` (default `.npy,.npz`; add `.h5` or `.parquet` for a benchmark that saves those), except:

| Kept | Why |
|---|---|
| a file git tracks in a plain-mode iteration, and anything in a repository-mode `src/` | code or test data |
| a running iteration's files | a subagent is writing them |
| `autooptimize/reference/` | the gates compare every run against it |

`--keep-outputs` keeps them all.

Stop the dashboard publisher last, naming the search: `python3 $AO/dashboard/autoopt_monitor.py $WORK/autooptimize --stop`. `$WORK/scratch/` holds anything disposable: remove it, with the user's yes, once the winner is copied out.

`repo.git/` plus the records reproduce any iteration (`restore --id N`, then `run_iteration.py`); the saved outputs and checkouts that are gone regenerate from them.

## 4. Tell the user how to bring the change back

The search never writes to the user's code, so the change reaches it only
when they apply it. Close step 5 with the commands for this search, with its
paths and the winner's id written in. `prune` leaves both routes working: the
archive is outside `autooptimize/`, and `repo.git/` keeps every branch.

| Mode | Give the user |
|---|---|
| plain | `git apply --check <archive>/idea.patch`, then `git apply <archive>/idea.patch`, run in the folder that held the files the search copied; or the files in `<archive>/code/` |
| repository | in the original: `git fetch $WORK/autooptimize/repo.git autoopt/<id>:autoopt-winner`, then a branch of their own from the branch the search started from (`config.json["repo"]`) with `git merge --squash autoopt-winner`, or a rebased copy for one commit per change |

The branch's commits are named for the search's labels (`A7: …`). The
messages to publish are the `## Commit message` sections of the kept
iterations' `notes.md`, in the order `path --markdown` lists them. Name any
dependency the winner added and what its observers and notes say it costs
outside the benchmark. Applying the change, pushing a branch or opening a
pull request is the user's to ask for: the original is not written to
without it.

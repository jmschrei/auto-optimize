# Step 4 — end the search

The search ends on the record, once, by a command. Run it when *When to stop*
(`references/step3-rounds.md`) is reached and the user does not go on, or when
the user says the search is over. Nothing after it needs the search to run:
shipping (step 5) and finishing (step 6) read `ended.json`.

```bash
python3 $AO/autoopt.py end $WORK --reason <stalled|variations|budget|noise|floor|"<the user's words>"> --background
python3 $AO/autoopt.py wait $WORK/autooptimize/end.log   # again, until it prints `wait: finished`
```

`--reason` is the word of the *When to stop* criterion reached (`references/step3-rounds.md`) or the user's own words. Without it, `end` records `floor` when the root is at the floor, `stalled` when the open round holds a phase's worth of settled iterations, and `none stated` otherwise. `--id` names another winner than `best`.

## Refusals

`end` changes nothing and says why while:

- an iteration is in flight: running (no `result.json`), finishing (measured,
  no `README.md` yet) or briefed and not started. Let it settle; no new idea
  starts. A brief that will never be spawned is withdrawn by deleting the
  `briefs/<id>.md` the refusal names;
- a phase of the orchestrator subagent is open (the orchestrator closes it
  when told to stop; `loop --force` after one that died);
- the search already ended (`end --reopen`);
- the winner is A0: nothing was kept.

## What it does, in order

1. Runs `final`: the winner against A0 by paired timing (and at
   `checks.threads`), written to `final.json`. A search with no timing probe
   (`checks.time`) has none; its medians are the result. A paired run that
   errors stops the ending before anything is written or deleted. Its two
   unrecorded runs price it (`priced:` in `final/paired.log`): when the least
   pairs would pass `after.pair_cap_hours` (4) it is `over cap` and the ending
   stops there, to raise the cap or to point `checks.time` at a smaller input
   the user agrees to. A recorded run past ten times its side's unrecorded
   run plus 60 s is killed (`timed out`), which also stops the ending. The
   `checks.threads` comparison runs only after the main one was made; `over
   cap` or `timed out` there is recorded and does not stop the ending. On a GPU search
   `end` and `final` take their lease through `gpu_command`.
2. Writes the summary of the round still open, when its iterations all have
   READMEs (`references/step3-rounds.md`, *The round summary*).
3. Writes `autooptimize/ended.json` (below), then `ended.md`, and prints it: the median from A0 to the winner with each gate's status; the paired line, labeled "paired, on the search's own input"; the winning iterations by category with each one's change from its parent. Every number is read from `result.json` and `final.json`.
4. Deletes the regenerable files (below) and records what it freed.
5. Writes a static snapshot of the dashboard, `autooptimize/final/dashboard.html`,
   and leaves the publisher running. Step 6 stops it. A missing `plotly`
   leaves no snapshot and says so.
6. Prints the next step: step 5, shipping, which starts by asking the user
   how to ship (`references/step5-ship.md`).

Show the user `ended.md`, the paired verdict beside the medians, and quote the
paired line in the final report.

## ended.json

Read `autooptimize/ended.json` for the reason, the winner, the paired result, the medians, the clean-up counts and `winner_changes` (`[]`; one entry for each iteration made with `--after-end` and promoted).

After `end`, `--id best` means `ended.json`'s winner in every command.

## The clean-up

Automatic, and the same as `prune --worktrees` with the winner protected:

| Goes | Stays |
|---|---|
| each run's output files, except `metrics.json`, `log.txt` and `commit`, in every iteration off the winner's path | `config.json`, `spec.md`, `state.json`, `ideas.md`, `knowledge.md`, `rounds/`, `briefs/`, every `README.md`, `meta.json`, `result.json`, `report.txt`, `notes.md`, `unit-tests/`, `probe/` files other than arrays |
| the compiler `cache/` and the exported `check/` trees of those iterations | `repo.git/`: every iteration's code, one branch each |
| the code checkout (worktree) of every iteration off the winner's path; `restore --id` brings one back | the winner's path in full: A0 and every iteration from it to the winner, with checkouts and run files; every root |
| saved output arrays (`--outputs-ext`, default `.npy,.npz`) in every settled iteration, as `prune` does | `harness/`, `checks/`, `reference/`, `tools/`, `final/`, `ended.*`, and everything outside `autooptimize/` (`tests/`, `interview/`, `inputs/`) |

What stays is what a reader of the ended search needs: the branches in
`repo.git/`, every `result.json` and `meta.json`, the winner's `unit-tests/`,
`reference/`, `harness/`, `checks/` and `final.json`. Cleanup is not undone by
reopening.

## An iteration made after the end

A change the ended winner still needs (a notice, a fix, a clean-up
such as a comments-only iteration): after `end`, `new` and `brief` refuse
without `--after-end`, which stamps the iteration (`meta.json` `after_end`). It
is measured like any iteration (`run_iteration.py`). Its runner does not
promote it; `promote --id` runs the preflight on it
(`references/promotion.md`, *The preflight*), appends it to `winner_changes`
and makes it the winner in `ended.json` and `ended.md`. Anything checked on
or built from the earlier winner is then stale. `paired` in `ended.json` stays
the earlier winner's; `final` measures the new one. An iteration made without
`--after-end` is never promoted after `end`.

## Reopening

```bash
python3 $AO/autoopt.py end $WORK --reopen
```

Archives `ended.json` as `ended-<n>.json` (and `ended.md`), restarts the
dashboard publisher and lets `loop`, `brief`, `new` and `mine` work again.
Reopen when the user wants the search itself to go on, then end again. The
clean-up stays done. `after.ship` goes back to `ask` (it answered for the
winner set aside), so the next `end` leads to step 5's question again; the
other `after` answers stay.

It is refused while `autooptimize/reopen-lock.json` holds a hold
(`{"holds": [{"by", "reason", "at"}]}`), printing each: step 5 writes one when it applies the change, makes a
branch or posts a document (`autoopt.hold_reopen(root, by, reason)`; a
`reason` of None withdraws it). A shipped winner changes only through an iteration made with
`--after-end` (above).

## Paused, not finished

A search that is only paused is not ended: stop at *When to stop*, keep
`ideas.md` current and prune nothing. `status` and the round summaries hold
the state, and `references/resume.md` is what the next session reads.

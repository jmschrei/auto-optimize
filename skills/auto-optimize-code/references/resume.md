# Picking up a search already in progress

The session that started a search is rarely the one that finishes it. None of
its context survives; everything needed is on disk.

This file is for continuing the search: it starts the dashboard publisher and
hands unfinished iterations to new subagents. To answer a question about the
search without continuing it, read `references/reading-a-search.md` instead.

## First: has the search ended?

`autooptimize/ended.json` means it has (`references/step4-final.md`). Read
`ended.md`, put the publisher back up if it died (`end` leaves it running
and writes `final/dashboard.html`; step 5 stops it), and go on with step 5
(`references/step5-finish.md`).
`end --reopen` is only for a user who wants the search itself to go on, and
is refused once the winner is published. Otherwise:

## Then put the dashboard publisher back up

The publisher may have died with the old session. Start it before reading
anything, so it scans while you read; it prints `already running` if the old
one is still up.

```bash
python3 $AO/dashboard/autoopt_monitor.py $WORK/autooptimize --start
```

## Then read where it got to, in one call

```bash
python3 $AO/autoopt.py status $WORK --resume
```

`spec.md` holds what `config.json` has no field for: the keep rule, the rules added during the search, what is off limits. `autooptimize/briefs/<id>.md` holds what each subagent was told.

## Then act on each unfinished iteration

**Step 2, when `status` prints a `setup (step 2):` line.** Its setup subagent
died with the old session: a question waiting for the user goes to them, and
`setup --answer` prints the prompt for a new subagent; otherwise `setup
--force` prints one that continues from the notes
(`references/step0-interview.md`, *Handing off step 2*). Step 3 starts once
`status` stops printing `first iteration: waiting for …`.

**Iterations**, by the verdict `--resume` gives each:

| Verdict | Do |
|---|---|
| its runner is alive | leave it: resuming it runs the same seeds twice |
| its subagent wrote recently | it may still be working (a resume after a compaction keeps its subagent): look again before handing it on |
| an empty fork, never edited | set it aside with the two commands it names; its branch `autoopt/<id>` stays and keeps the id taken, and `new` without `--id` skips it |
| edited, not measured; measured, no README; partial | hand it to a new subagent with the prompt `autoopt.py brief $WORK --prompt <id>` prints, which carries the line for where the last one stopped. Seeds already measured on its current commit are not run again |

A `partial` row is already excluded from the frontier, and may instead be set
aside.

**GPUs still held.** A live runner holds its GPUs. Check what is free before
briefing the next iteration.

## Rebuild the idea list if it needs it

`autooptimize/ideas.md` is the queue the idea miner fills, with tried ideas
struck through and the decisions taken while the user was away. Do not
rebuild it yourself: when `status` says fewer than two untried ideas are left, or the
file is missing, run `autoopt.py mine $WORK` and spawn the miner, which reads
the READMEs and the ledger in its own context. A stage `status` shows as
running whose miner died with the old session needs `mine --force`.

Before briefing the next iteration, tell the user what changed since they last looked and
the plan; the search may have reached *When to stop*
(`references/step3-rounds.md`). With iterations still in flight, keep the
queue full while you wait for the reply (*Questions while the search runs*,
same file).

## Then continue

You are at step 3, once step 2 is done. The main session goes on with
`references/main-session.md`: `status` names a phase still open from the old
session, whose subagent died with it, and `loop $WORK --force` replaces it. As the
orchestrator subagent, you act on the verdicts above and go on with *The
loop*.

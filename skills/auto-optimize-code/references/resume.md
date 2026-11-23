# Picking up a search already in progress

The session that started a search is rarely the one that finishes it. None of
its context survives; everything needed is on disk.

This file is for continuing the search. To answer a question about the
search without continuing it, read `references/reading-a-search.md` instead.

## First: has the search ended?

`autooptimize/ended.json` means it has (`references/step4-final.md`). Read
`ended.md`, put the publisher back up if it died (`end` leaves it running
and writes `final/dashboard.html`; step 6 stops it), and go on with step 5
(`references/step5-ship.md`): `stages.py next $WORK` says whether the user has
been asked how to ship and what is left, then step 6
(`references/step6-finish.md`).
`end --reopen` is only for a user who wants the search itself to go on, and
is refused once step 5 has shipped the winner (applied it, made a branch or
posted a document). Otherwise:

## The main session

**Put the dashboard publisher back up.** It may have died with the old
session. Start it before reading anything, so it scans while you read; it
prints `already running` if the old one is still up.

```bash
python3 $AO/dashboard/autoopt_monitor.py $WORK/autooptimize --start
```

**Read where it got to** with `python3 $AO/autoopt.py status $WORK --brief`,
then act on what it names:

- **a `setup (step 2):` line**: the setup subagent died with the old session.
  A question waiting for the user goes to them, and `setup --answer` prints
  the prompt for a new subagent; otherwise `setup --force` prints one that
  continues from the notes (`references/step0-interview.md`, *Handing off
  step 2*). Step 3 starts once `status` stops printing `first iteration:
  waiting for …`;
- **a phase still open**: its orchestrator subagent died with the old
  session. `loop $WORK --force` replaces it; the new phase acts on the
  unfinished iterations (below).

Tell the user what changed since they last looked and the plan before the
next phase starts; the search may have reached *When to stop*
(`references/step3-rounds.md`). Then go on with `references/main-session.md`.

## The orchestrator subagent

Your first call, `python3 $AO/autoopt.py status $WORK --resume`, says where the
search got to. `spec.md` holds what `config.json` has no field for: the keep
rule, the rules added during the search, what is off limits.
`autooptimize/briefs/<id>.md` holds what each subagent was told.

## Then act on each unfinished iteration

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

Then go on with *The loop* (`references/step3-rounds.md`).

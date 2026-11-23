# Step 3 — running the rounds

The orchestrator's part is the loop below; what it never does is in
`SKILL.md`, *Rules that hold everywhere*. The idea miner fills `ideas.md`
(`references/idea-miner.md`), each iteration's runner promotes on the keep
rule unless `promote` is `orchestrator` (`references/promotion.md`), and
`autoopt.py` assembles each round's summary. Every file under `iterations/`
is written by a subagent, `run_iteration.py` or `autoopt.py`. The one runner
call the orchestrator makes is re-judging stored runs after a gate is changed
(`references/changes.md`).

**Read little.** Everything the orchestrator reads stays in its context, and
that context is rewritten in full at every wake-up after the prompt cache
expires. After each report, read `autoopt.py status --brief` and
the subagent's short report; do not read its logs, run outputs, probe files
or README, and do not read code. Cap any other output (`| tail -n 40`).

A0 carries **round 0**, which holds no ideas. Rounds of ideas are numbered
from 1, and a round closes when an iteration is promoted, so `summary --round 1` covers the iterations forked from A0 up to the first
promotion. A same-code control (`brief --control`, run by a subagent) is a row
in its round but tries no idea.

## The orchestrator subagent

From the first iteration on, *The loop* is run by an orchestrator subagent, not
the main session. The main session carries the whole conversation, and every
wake-up after a quiet spell writes all of it to the prompt cache again, so
the rounds run in a context that holds only the search. What the main session
does from the first iteration on is `references/main-session.md`.

The orchestrator subagent reads its prompt (`briefs/loop-<n>.md`), `SKILL.md`
and this file, and runs *The loop*. Where this file says to tell or ask the
user, it puts that in its report, and asks nothing itself.

## The loop

One iteration runs at a time. It is measured against the root the previous
one left, and when it passes the keep rule it becomes the root, so the next
iteration starts from it.

```
start:
    once status stops printing `first iteration: waiting for …`: the setup
      subagent and the idea miner's S1 are both done
      (references/step0-interview.md, *Handing off step 2*)
    autoopt.py brief $WORK --next   # a brief and a spawn prompt for the one
                                    #   slot, from the top untried idea
                                    #   (references/writing-briefs.md)
    spawn the prompt it printed with the Agent tool, with the description and
      model its stderr line names (`subagent_model`; the miner keeps the
      session's). Every later iteration is spawned the same way
    start the dashboard publisher (references/dashboard.md)

never end a turn with work in flight and nothing to wake the session:
    a subagent's completion notification wakes it. The fallback, for one
    that stalls without reporting: when `status` or `brief --next` prints
    `watch: none running`, start `autoopt.py watch $WORK` with the Bash tool's
    run_in_background. It returns only when status would flag something
    (a subagent STALLED, a measured iteration with no README after an
    hour), after 3 hours, or once nothing has been in flight for 30
    minutes. Never a `sleep` timer: a wake-up after a quiet spell
    rewrites the whole context, and a timer wakes the session to find
    nothing to do. Inside /loop, also ScheduleWakeup at 3600 s

whenever the subagent reports, and at every wake-up:
    autoopt.py brief $WORK --next   # status --brief (working, STALLED,
                                    #   READMEs never written, dashboard,
                                    #   machine, ideas left, the miner's
                                    #   stage); the table of each round
                                    #   summary written since the last call,
                                    #   which you show the user; then a brief
                                    #   and a prompt for the free slot
    spawn what it printed, as at the start
    a STALLED subagent: hand its iteration to a new subagent (*A subagent that
      goes quiet*); `brief --next` counts its slot as taken
    `promote: auto`: the iteration's runner promoted it if it passed the keep
      rule while its parent was still the root, and ran the closed round's
      approved validation (its `keep:` line). Nothing to do, except for a
      report whose `keep:` line reads `not promoted: …`: `promote --id <it>`
      before the next `brief --next`
    `promote: orchestrator` (a keep rule in the user's own words): read
      `status --brief` first, promote, then `brief --next`, so the new brief
      forks from the new root:
        autoopt.py promote --id <it>          # when it passes that rule and its
                                              #   parent is still the root
        a refusal naming `run_iteration.py <dir> --full-tests` (a subset
          test pass): ask its subagent (SendMessage), or a new one with the
          prompt `brief --prompt <it>` prints, to run that command, then promote
        autoopt.py validate --round N         # only if configured and approved
    status says fewer than 2 untried ideas, and no miner runs:
      autoopt.py mine, and spawn the idea miner with what it prints
      (*Keeping the ideas*); keep briefing from what is left meanwhile
    a GPU search with GPU indices rather than an allocation command, or a
      busy machine (*GPUs for each iteration*, *The machine during the
      search*): `brief --next --gpus <its indices>`, or no brief yet
```

The loop hands back after `loop_handback` settled iterations
(`autoopt.py loop`), and at *When to stop*.

## Questions while the search runs

Nothing is spawned while a question waits, so one asked at night stalls the
search until morning. Once the first iteration is in flight:

1. **Do not end a turn waiting for a reply.** What this skill already decides
   is not a question: spawning goes on as usual, and the runner keeps
   promoting on the keep rule.
2. Take the answer that does the least damage if it is wrong: the reversible,
   narrower, cheaper one, often not the one you would mark recommended. When
   the question is whether to promote, delete, ship or change a gate, the
   answer is no until the user replies; hold that one decision and keep
   everything else moving.
3. Write the question, the answer taken and why in `ideas.md` under
   *Decisions taken while the user was away*, and say it in the next message.
4. When the user answers, apply it and re-run what the default decided
   wrongly. A changed setting goes through `autoopt.py config --set …
   --reason "<their words>"`; any other new rule goes under *Rules added during
   the search* in `spec.md`, dated and in their words, before it is used.

Claude Code's `askUserQuestionTimeout` setting (`60s`, `5m`, `10m`, or
`never`, in the user's `settings.json` or `/config`) lets an unanswered
question continue on its own. Offer it to the user before a long search; do
not change their settings yourself. A question that continued unanswered gets
the least-damage answer.

## A subagent that goes quiet

`status` lists every subagent still working (`running` or `finishing`) with
the time since its last write, and marks one silent for over 15 minutes
`STALLED`; its `no README:` line names measured iterations nobody has written
up for over an hour. A subagent can write its logs and then go silent while
nothing runs. Do not wait for its report: its `report.txt`, the runner's
block, holds the `keep:` line it would have sent. The iteration goes to a new
subagent with the prompt `autoopt.py brief $WORK --prompt <id>` prints, which
tells it where the last one stopped (measured or not).

A stalled subagent's runner may still be running and holding its GPUs:
`status --resume` names a live runner by its pid
(`references/resume.md`). Leave a live one to finish, since resuming it runs
the same seeds twice. A round's summary waits for every README.

## GPUs for each iteration

**Two iterations MUST NOT hold the same GPU** (`references/step2-baseline.md`,
*GPU leases*, has both rules). With `gpu_command` recorded, `brief` puts it in
the brief by itself and the runner leases through it. With none, you are the
allocator: read `nvidia-smi` once, give each brief its own indices (`brief
--gpus 2`), and reuse an index only after that iteration settles; a subagent
never picks one, since two launches seconds apart take the same free card.

## The machine during the search

`spec.md`'s *Machine and environment* line says how many cores and how much
memory the search may use: keep the runs at once, and each run's threads,
inside it. Before every iteration, read `status`'s `machine:` line (`256 cores,
86% idle, ~219 free over 0.1 s`), a short sample of the CPUs' idle share with
this search's runs included, never the load average, which counts threads a
container's quota holds back: an iteration that starts quiet can
finish contended. `gpus_per_agent` processes at about 100% are the iteration
working; anything above that is somebody else (`autoopt.py busy $WORK` names
them).

**The exact test is a same-code control.** The idle share is a hint; the objective is
the measurement. Brief the root unchanged and spawn a subagent with it, like
any iteration; you measure nothing yourself:

```bash
python3 $AO/autoopt.py brief $WORK --id <next free> --control --title 'control: unchanged root' \
	--category MISC --idea 'Change nothing: measure the root as it is.' --tried 'none'
```

A median outside the noise band means the machine is not in the state the
frontier was measured in. Never `--rerun` the root itself: that overwrites
the measurement the frontier rests on. The control stays in the table, never
holds the frontier however it reads, and cannot be promoted.

**The machine is busy** when, on a CPU benchmark, any process outside this
search runs above `busy_percent` (110% of one core by default), or the control's median falls outside the
noise band. **When it is busy before the first iteration**, put the choice to the
user with `AskUserQuestion`, giving the idle share, what is running and, once there
is one, the control's median against the band. Do not narrow the runs
quietly.

| Option | What it means |
|---|---|
| Hold off for now | brief nothing until the machine is quiet; say what you will watch and check back |
| Proceed with one run at a time | `gpus_per_agent` set to 1 (`config --set`), which minimizes the contention this search adds; each iteration takes longer |
| Proceed as planned | the runs at once as configured; the round's numbers are inflated and not comparable to other rounds |

**Once iterations are in flight, do not ask:** take *Proceed with one
run at a time*, record it in `ideas.md` under *Decisions taken while the user was
away*, and tell the user. Either way, tell the user with the round's summary
that it was measured under contention.

## Keeping the ideas

`autooptimize/ideas.md` holds one line per idea, in the form at its top
(`- [CATEGORY] where: what. Why: … Gain: … Exact: … (S<n>)`), so `status`
can count what is left. `brief --next` takes the top of the miner's latest
stage and, when `--count` briefs more than one, keeps them on different
functions while it can. Ideas briefed together must be **different from each
other**, and an idea that is one parameter swept over four values is one
iteration, not four. Pass `--idea-line` with text only that line holds (a
number shifts when lines are added above it) to choose another, and strike a
line whose premise is gone with `autoopt.py drop $WORK --idea-line <text>
--reason <why>`, or `brief --next` picks it again. The subagent's `finish` strikes the line with its label
and what became of it ("- ~~[MICRO] … (S1)~~ A12: kept out: its median is
within the noise band …").

**The idea miner writes the ideas.** It reviews the code, the ledger, the
gates and the READMEs' *What to try next* in a context of its own, so none of
that reading lands in the orchestrator's. It runs:

- **once setup has closed**, started by the main session: it prices ideas
  against A0's profile (`references/step0-interview.md`, *Handing off step 2*);
- **whenever `status` says fewer than 2 untried ideas are left** and no
  miner is running: `autoopt.py mine $WORK` opens the next stage and prints
  the miner's prompt; spawn one subagent with it, unedited. Keep briefing from
  what is left while it works. One miner runs at a time; if its subagent
  died, `mine --force` opens the next stage.
- **never past the user's stage budget**, `after.budget_stages`: once that
  many stages have closed, `mine` refuses, `status` says the budget is spent,
  and the ideas left are briefed until none is; then the search stops for
  `budget` (*When to stop*).

Each stage is an `S<n>`: `new` stamps the last finished one on every
iteration, and the dashboard's stage boundaries mark where a fresh batch of
ideas begins, so a bump after a boundary is visible.

The orchestrator adds a line itself only for what a report hands it: a
follow-up to a gate that failed while the objective jumped, an idea a report
says should be split. A category that keeps winning deserves more of the next
briefs; one silent for two rounds, fewer. A *Gain* taken on an older root is stale
(`references/writing-briefs.md`, *What makes the supplied parts survive
contact*).

## Ids, parents and briefs

Choose each iteration's id when you brief it: `status` prints the next free
one, and `brief` refuses a taken id and names the next free. The brief holds
the exact `new` command, the subagent runs it, and `new` takes a lock, so
two calls at the same moment are safe. Do not run `new`
yourself for a subagent's iteration: a second `new` makes a duplicate.

The parent is the current root, which `status` prints. A brief names another
parent only for a deliberate fork from an earlier iteration; `promote` refuses
an iteration that does not descend from the root.

**Never fork from an iteration that is not `ok`**, even when its failure looks
like a measurement artifact the user is still deciding about: everything built
on it inherits whatever broke the gate, and iterations built on a held iteration
may be rejected whole. A gate-failed change that is still wanted either waits,
held, until the user decides the gate, and is then promoted by the usual
rules, or is re-implemented from the root in a form that passes. `new` and
`brief` refuse it as the parent:

> `error: parent A13 is gate-failed: an iteration that is not ok (a gate failed, a run crashed, or it is held for the user) is never forked from; fork from the current root (A10) …`

## Reading what comes back

The subagent returns what `references/subagent.md`, *What to report back*,
lists: its `report.txt` block, then a correction or a split first among at
most two sentences. Do not re-derive numbers from it:
`autoopt.py status` reads every `result.json`.

- **`keep: no`** is a result. Nothing promotes such an iteration, even one
  with the best median so far: its gain is inside the noise band.
- **`status: error`.** When the report says the failure was environmental
  (a full disk, a killed process), the idea is still untried: put it back on
  the list with a note.
- **A gate failed but the objective jumped.** A follow-up that pays the cost
  down is worth a slot.
- **A corrected premise.** Fix the `ideas.md` lines and later briefs that
  quote it. The subagent corrected the ledger itself (`autoopt.py ledger`,
  `references/shared-knowledge.md`); check none of the ledger or `tools/`.
- **A missing dependency.** The subagent installed nothing. If `spec.md`'s
  *Off limits* rules out new dependencies, the idea is closed. Otherwise
  install the package once into the search's environment, while nothing is
  timing, and read what the installer changed: if it upgraded or removed
  anything the benchmark imports, brief a same-code control of the root
  (*The machine during the search*) before comparing across the install. Then put the idea back on the list.

When the user is away, the summary and anything decided go in the next
message; nothing waits for them to read it.

## What an iteration and a round cost

An iteration is judged against its parent and the frontier as they stood when
it started. Each iteration costs `runs_per_iteration` runs, and a round
costs as many iterations as it takes to find one that passes the keep rule.

**Subagent time is usually the larger share of an iteration.** It takes
several times as long as its runs, reading code, building variants and
running its own checks, so more GPUs per iteration shorten the runs, not the
rest. The dashboard's iteration log breaks each iteration's time down.

## When the objective moves by an order of magnitude

Two parts of the search are calibrated to the scale it started at, and an idea
that moves the objective tenfold or more leaves both behind:

- **The noise band's reference spread** is the baseline's until the first
  phase, and the root's after: `autoopt.py loop` re-measures it from the
  root's own runs each time a phase opens and holds it for the phase, so a
  large speedup narrows the band by the next phase. Within a phase the band
  stays as it was; if the objective moved tenfold inside one, hand back and
  tell the user, and the next phase measures it again.
- **A timed call below about 10 ms** is mostly clock noise when the benchmark
  times it once (`references/step2-baseline.md`, *One run is a warmup and one
  timed call*). Changing the benchmark is a new search; propose one, seeded
  from the current best, whose benchmark repeats the call.

## The round summary

The `finish` that writes the last README of a closed round writes that
round's summary, `rounds/round-<N>.md`, as
`promote` does; nobody writes one by hand. It holds the table
with a column per gate and observer, the best of the round and overall, each
prediction beside its measured change, the round's validation, and three
sections assembled from the records: *What this round tested* (each
iteration's title and the first sentence of its README's *The idea*), *What
landed and why* (what became of each, from its keep verdict, and the first
sentence of *What happened*), *Next round* (the next untried ideas). Show the
user the table, with anything decided while they were away.

`summary overdue` in `status` names a closed round whose iterations have all
settled and not all written a README (*A subagent that goes quiet*).
`python3 $AO/autoopt.py summary $WORK --round 3` writes it at once, with
`(no README)` for an iteration that has none.

`no settled iteration carries round N` means none of the round's iterations
has settled yet.

## When to stop

Stop and tell the user when one of these is reached. `autoopt.py end
--reason` records which, by the word in the first column:

| Word | Reached when |
|---|---|
| `stalled` | the root has not moved through a phase's worth of settled iterations (`loop_handback`) |
| `variations` | the remaining ideas are variations of ones that failed |
| `budget` | the compute spent has passed what the user agreed to: every idea from the `after.budget_stages` idea-miner stages is tried or dropped (`status` says so, and `mine` refuses a further stage), or the wall clock since step 2 began has passed `after.budget_hours` |
| `noise` | the frontier's improvements are smaller than the noise band |
| `floor` | a minimized objective's root is at or under `floor`, the smallest value the user called worth measuring (step 0). `stages.py next` stops for the user even under `after.stop=auto`: end, or a new search on a larger input seeded from this root (`references/changes.md`) |

The user's own words are a reason too, and so is a target they named at
step 0 (`spec.md`, *Goal and scope*) once the root reaches it. Then step 4, which ends the search on
the record (`references/step4-final.md`).

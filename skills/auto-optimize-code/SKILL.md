---
name: auto-optimize-code
description: >-
  Search for a faster, leaner or smaller implementation of code by running
  many small experiments and keeping what survives. Use when asked to
  optimize, speed up or slim a function, a script, a command-line tool or a
  library, or to search over implementations of it for speed, memory or output
  size; to improve one number while another must not move ("faster, but
  outputs match to 1e-5"); to check on a running search or read what one
  found ("how is the search going", "what is on the frontier", "what did A7
  try"); to end, resume or finish one; or to ship the winner ("ship it",
  "open a PR with the result", "apply the change to my script"): written
  into the original files, as a branch, or as an optimization PR, a
  unit-test PR and a bug report on GitHub, reviewed before posting. Covers
  the objective/gate/observer interview, a git worktree per iteration,
  optimizing a copy of a GitHub repository, new unit tests as a gate, a setup
  subagent for the benchmark and baseline, median-of-N-seeds against a noise
  band, a tree search that promotes each kept change, generated briefs and
  READMEs, the paired timing of the winner, shipping, the final report, and
  the dashboard. Router skill: read the step's reference file under `references/`
  before doing that step's work.
---

# auto-optimize-code

Turn "make this better" into a search that runs itself: a benchmark that
emits numbers, a baseline, then one subagent at a time, each trying one
idea and reporting a median over seeds.

Developed at the [Programmable Genomics Laboratory](https://programmable-genomics.github.io) @ UMass Chan Medical School.

MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are as in RFC 2119.

## Only asked about a search?

A question about a search this session is not running, whether another
session started it or it has ended ("what won", "how far did it get", "what
did A7 try"), is answered from `references/reading-a-search.md` alone: its
commands only read, and nothing below applies. The main session of a running
search follows `references/main-session.md` for what it reads.

## The three words

Used exactly, in this skill and with the user. Every other term (root,
frontier, keep rule, noise band, …) is defined in
`references/glossary.md`.

| Word | What it is | Who does it |
|---|---|---|
| **run** | one execution of the benchmark with one seed | `run_iteration.py` |
| **iteration** | one idea, measured by several runs, reduced to a median | one subagent |
| **round** | the iterations forked from one root, closed by a promotion; one summary each | the orchestrator |

"3 seeds" means 3 runs per iteration. One iteration is in flight at a time: the
next starts when its subagent reports, on the root the last promotion left.
A round ends when an iteration is promoted.

## When this is the wrong tool

- **One measurement, not a search.** Timing a function or comparing two
  implementations once needs a benchmark alone.
- **No measurable objective.** If no script can emit it as a number, fix that
  first.
- **Improving a machine learning model's accuracy or training recipe.** This
  skill optimizes code: speed, memory, output size, with the outputs gated.

## Requirements

| For | Needs |
|---|---|
| everything | Linux, Python 3.9 or later, git. The core scripts use only the standard library |
| the orchestration | Claude Code's Agent (subagents) and AskUserQuestion tools; SendMessage and Monitor where available |
| a GPU search | `CUDA_VISIBLE_DEVICES`, set by the machine's GPU allocator, or by `env CUDA_VISIBLE_DEVICES=<index>` on a machine with none; either is recorded as `gpu_command` |
| the dashboard in a notebook | JupyterLab, IPython and plotly; psutil for the host and process panels |
| a static dashboard (`--export`) | plotly |
| the task's checks and the preflight, which export an iteration's tree | `tar` |

Linux-only commands (`flock`, `/proc`, `/sys`) are marked where used.

## Paths in every command

```bash
AO=${CLAUDE_SKILL_DIR}/scripts   # ${CLAUDE_SKILL_DIR}: the directory holding this SKILL.md
WORK=<the search's directory: the one holding autooptimize/>
```

Every reference file uses these. If `${CLAUDE_SKILL_DIR}` is not expanded,
write this file's directory as an absolute path. A brief gives both, absolute.

## The steps

Steps 0 to 6 run in order. Read each step's file just before the step, not in
advance.

| Step | Do | Read |
|---|---|---|
| 0 | **REQUIRED first:** interview the user, choose the search directory, `init`, fill in `spec.md`, then spawn the setup subagent (`autoopt.py setup`); the idea miner follows when setup is done (`autoopt.py mine`), because it prices ideas against A0's profile | `references/step0-interview.md` |
| 1 | Repository mode, on a yes at step 0 only: write the new unit tests in a clone, passing on the base in every CI configuration, then `init` on it and spawn the setup subagent, and the miner once setup is done | `references/step1-unit-tests.md` |
| 2 | The setup subagent's: write the benchmark, check its seeding, prove its gates and checks, measure the width, run the baseline A0, seed the ledger, write A0's README, close with `setup --done`. The main session relays its questions to the user | the setup subagent: `references/step2-baseline.md`; the main session: `references/main-session.md` |
| 3 | Once setup and the miner's S1 are done, `autoopt.py loop` starts the orchestrator subagent, which runs the rounds and hands back with the round tables to show the user; start the next phase | `references/step3-rounds.md`, *The orchestrator subagent* |
| 4 | End the search on the record: `autoopt.py end` refuses while anything is in flight, pairs the winner with A0, writes the last round's summary, `ended.json` and `ended.md`, deletes the regenerable files and snapshots the dashboard. | `references/step4-final.md` |
| 5 | Ship, once the search is over: ask the user how (`ship.py plan`), then apply the change to the original files (plain mode), make a branch in the original repository, or open the GitHub documents they check (the optimization PR, the unit-test PR, the bug report), reviewed together and posted together | `references/step5-ship.md` |
| 6 | Finish: copy the winner out, write the final report, reclaim the disk once the user agrees | `references/step6-finish.md` |

| Not a step | Read |
|---|---|
| You are the setup subagent | `references/step2-baseline.md` |
| You are the main session, from step 2 on: setup questions, phases, the user's messages, ending the search, the cost | `references/main-session.md` |
| You are the orchestrator subagent | your prompt, then `references/step3-rounds.md` |
| You are a subagent running one iteration | `references/subagent.md` |
| You are the idea miner | `references/idea-miner.md` |
| The dashboard: start it, read it, fix it | `references/dashboard.md` |
| A search a previous session started | `references/resume.md` |
| A question about a search, not continuing it | `references/reading-a-search.md` |

## By topic

| Topic | Read |
|---|---|
| What a term means | `references/glossary.md` |
| An error message, a refusal, or something that looks wrong | `references/symptoms.md` |
| Writing a subagent's brief: `autoopt.py brief` | `references/writing-briefs.md` |
| Where ideas come from: the idea miner, `autoopt.py mine`, the categories, `ideas.md` | `references/step3-rounds.md`, *Keeping the ideas*; the miner's own protocol, `references/idea-miner.md` |
| The task's checks: writing them at step 2, A0's cached outputs, `autoopt.py check`, the order the runner runs them in | `references/step2-baseline.md`, *Write the checks*; `references/subagent.md`, *Checks beyond the runner* |
| The knowledge ledger, shared tools, the checker library, a cached scratch build | `references/shared-knowledge.md` |
| Promotion (the runner's keep verdict, `promote: auto`), predicting what a change is worth | `references/promotion.md` |
| A relaxed gate, a changed objective or gate, starting the ideas over (`reset`) | `references/changes.md` |
| Checking the winner on inputs the search never measures | `references/external-validation.md` |
| Telling whether a change helped: profiling, paired timing (`scripts/paired.py`), compiler caches and `{cache}` | `references/measuring.md`; sweeps of variants: `references/subagent.md`, *Timing variants* |
| Speeding up or slimming an algorithm implementation: time and memory, synthetic input sizes, thread counts | `references/algorithm.md` |
| Optimizing code in a git repository without touching it: the copy, the harness, the unit-test gate | `references/repo-mode.md` |
| The checks `promote` makes without measuring: text, the `## Change` and `## Commit message` sections of `notes.md`, indentation, license markers, the oldest toolchain (`preflight.py`, `lint_text.py`) | `references/promotion.md`, *The preflight* |
| What is on disk, what the JSON files hold, the version fields | `references/layout.md` |
| Changing what the skill does: every setting, environment variable and hook, and where to edit the code when none fits | `references/extending.md` |
| Every script flag (the dashboard's are in `scripts/dashboard/README.md`) | `references/cli.md` |
| Reading a search without changing it: `autoopt.py table` and `show`, which file answers which question | `references/reading-a-search.md` |
| Ending the search, reopening it, an iteration made after the end (`--after-end`), what `end` leaves on disk | `references/step4-final.md` |
| What the GitHub documents carry and may not say; the stand-in numbers | `references/documents.md` |
| The optimization PR: its tables, prose, commit messages, `autoopt.py pr` | `references/pr-optimization.md` |
| The unit-test PR: its tables from step 1's files, the push | `references/pr-tests.md` |
| The bug report: bugs.tsv, verifying each reproduction, filing it | `references/issue-bugs.md` |
| Code compiled by numba: speed, exactness classes, compile time | `references/algorithm.md`, *Numba* |

## Rules that hold everywhere

**Step 0 MUST come first.** Interview the user before writing a benchmark,
and ask what a gate means rather than guess: "the outputs must not change"
is bitwise identity, a tolerance, or a correlation.

**`$WORK/NOTES.md` is generated, never written.** `python3 $AO/stages.py
status $WORK` prints the six stages (interview, setup, search, final, ship,
finish)
as `not started`, `running` or `complete`, with the next command; `render`
writes the file, and `end` calls it.

**The search directory is chosen at step 0 and never moved.** Ask, offering
`~/auto-optimize/<YYYY-MM-DD>-<name>/` as the default. Moving it breaks
the absolute paths in git's worktree records, `config.json` and the harness.

**A running search MUST NOT wait on the user.** Once iterations are in
flight, do not end a turn on a question. Take the answer that does the least
damage if it is wrong, record it, and keep briefing the next iteration
(`references/step3-rounds.md`, *Questions while the search runs*).

**Rules live in `spec.md`, and briefs come from `autoopt.py brief`.** Record a
rule decided mid-search before using it: a setting with `autoopt.py config
--set … --reason "<the user's words>"`, which writes the dated line itself,
anything else as a dated line in the user's words under *Rules added during
the search*. `setup --answer` writes the line for a setup question's answer.

**Name an iteration with its label, everywhere: `A107`, never `107` or
`#107`.** Directories and branches keep the bare number (`iterations/107/`,
`autoopt/107`).

**The search ends once, on the record.** `autoopt.py end` (step 4) refuses
while an iteration is in flight or a phase is open, writes `ended.json`,
and deletes the regenerable files.
Afterwards `new`, `brief`, `loop` and `mine` refuse, except an iteration made
with `--after-end` (a notice, a fix, a clean-up), and
`--id best` is `ended.json`'s winner. `end --reopen` undoes the ending until
step 5 ships the winner (a hold in `reopen-lock.json`), never the clean-up
(`references/step4-final.md`).

**One subagent, one iteration.** A subagent implements one idea, measures
it, writes its README and stops. The orchestrator edits no iteration's code.

**The main session relays; it does not work the search.** It spawns the setup
subagent and the first idea miner; the orchestrator
subagent spawns iteration subagents and later miners and
runs the rounds in phases the main session starts (`references/step3-rounds.md`,
*The orchestrator subagent*). The main session reads only `status --brief` and
the phases' reports, relays questions to the user and shows round summaries.
It mines no ideas, measures nothing, promotes nothing, reads no code, logs,
READMEs or outputs, and writes no summary prose: its context is rewritten at
every wake-up. An iteration whose subagent is
gone goes to a new one (`references/step3-rounds.md`, *A subagent that goes
quiet*).

**No call lasts longer than four minutes**, for any agent: the prompt cache
lives five, and the turn after a longer call writes the whole context to it
again, at many times the price of reading it. A foreground command takes the Bash tool's `timeout: 240000`; one that may
pass that starts in the background and is watched with `autoopt.py wait <log>`
(it returns within 200 s with the new lines), repeated until it prints `wait:
finished`.
`end`, `final`, `validate`, `run_iteration.py`, `width` and `prove` take
`--background` (under a `gpu_command`, `validate` and `width` go through `bg`
whole, allocator included, since their own `--background` drops the lease); anything else is `autoopt.py bg $WORK <log> '<command>'` in the
main session and `(<command>; echo "exit $?") > <log> 2>&1 &` in a subagent.
**A turn never ends on a background command that nothing waits for**: the
next call is `autoopt.py wait <log>`.

**The dashboard publisher MUST start with the first iteration**, asked for or not
(`references/dashboard.md`), and the user is told how to see it: the notebook,
`$WORK/dashboard.ipynb`, or what that file gives for a machine without
JupyterLab.

**Every iteration of a GPU search holds its own GPUs for its whole length**,
and two iterations never share one (`references/step3-rounds.md`, *GPUs for
each iteration*).

**Only the runner's median is a result, and numbers are never retyped.**
Anything timed inside one process ranks variants and measures no gain. The
winner against A0 (`autoopt.py final`, run by `autoopt.py end`) is reported
beside the median, labeled "paired" (`references/measuring.md`).

**The README is the only channel for an iteration; `knowledge.md` is the
search's.** What a subagent noticed is lost unless it is in its `notes.md`,
and rediscovered by a later one unless it is also in
`autooptimize/knowledge.md` (`references/shared-knowledge.md`).

**External validation is optional, runs once a round, and gates nothing.**
Propose one; it MUST NOT run without the user's recorded sign-off, and a bad
result goes to them (`references/external-validation.md`).

**In repository mode, step 0 MUST ask "Do you want to add new unit tests?"**
and, on a yes, the scope (`references/step1-unit-tests.md`).

**Anything needed later lives under `$WORK`.** Scripts, scratch builds and
probe inputs go in `$WORK`, never in `/tmp` or a session's scratchpad, which
are not part of the search.

**The original repository MUST NOT be written to.** A repository search
copies it with `init --repo` (`references/repo-mode.md`). The one exception
is step 5, on the user's own choice there: `ship.py apply --yes` writes the
original files, `ship.py branch --yes` adds a branch to the original
repository, and `ship.py post` pushes and opens what they reviewed.

**Shipping starts with the user's answer, after the search is over.** Step 5
asks how to ship and records it (`after.ship`); nothing is applied, made or
posted before that answer, and GitHub documents are posted only together,
after the user reviewed all of them (`references/step5-ship.md`).

# Step 0 — the interview

Everything here is expensive to change later: a gate written down wrong
invalidates every iteration measured under it. Ask with `AskUserQuestion`; do
not infer an answer the user has not given. A default the user accepted is
written "(default, accepted by the user)"; a default nobody was asked about is
written "(default, not asked: <why>)", and the user sees it in the read-back.

## Before asking

1. **Read `references/algorithm.md`**, and when the code lives in a git
   repository, `references/repo-mode.md`. Each adds rows to the table below.
2. **Collect what the user already said.** A question they answered is not
   asked again; its answer goes into the read-back.

## The questions

The questions come in six groups, asked in this order. A group is as many
`AskUserQuestion` calls as it needs, at most four questions each, in table
order; a question that depends on another's answer goes in a later call than
the one that asks it, and a question that does not apply (a GPU question in a
search with no GPU, a repository question in plain mode) is skipped. Put the
recommended option first and label it "(Recommended)"; where the *Default*
column says none, the answer is the user's alone, and no option is marked.
One exception: the unit-test scope row in `references/repo-mode.md`
recommends the functions being optimized, and is still always asked. Group 6
opens with one question, "Everything after the search: take the recommended
answers (list them) or choose each?": on "recommended" record every default.

A cost the table asks you to quote (a tracking dataset, an external
validation) is not known until the benchmark exists: say so, and the setup
subagent prices it at step 2 and brings back any that is large
(`references/step2-baseline.md`, *The price of the base*).

| Group | Question | Options to offer | Default | Recorded in |
|---|---|---|---|---|
| 1, the goal | Where should the search live? | `~/auto-optimize/<YYYY-MM-DD>-<name>/` (Recommended); another directory, on a disk with room for a copy of the code per iteration | the recommended one | the directory given to `init` |
| 1 | What is the objective, and is higher or lower better? | the timings, memory or sizes the code already reports | none: never guess | `spec.md` Objective |
| 1 | What does a representative run look like: which command or call, on which input? | an existing benchmark or script; a command and an input the user names; a proposal from the tests, examples and documentation, which setup writes and reports | none: never guess | Goal and scope; the *Benchmark* line setup writes at step 2 |
| 1 | What must not move, and by how much? One question per gate, in the forms of *Gates* below | the readings in the *Gates* table; when the user said the output must not change, bitwise identity against the baseline's output (Recommended) | the outputs match the baseline's, in a form the user chooses: a search with no output gate is the user's explicit choice | Gates, and each one's reference |
| 1 | Is there a result at which the search should stop? | none: run until a stop criterion is reached (Recommended); a value in the objective's unit, or a ratio of the baseline | none | Goal and scope: the root reaching it is a reason to stop (`references/step3-rounds.md`, *When to stop*) |
| 1 | A minimized objective: what is the smallest value still worth measuring? | a value in the objective's unit; none | none | `config.json` `floor` (`init --floor`): the search hands back at it, and step 2 asks again when A0 is under 20 times it |
| 2, the machine | Does the benchmark need a GPU? | yes; no | what the code uses | `config.json` `gpu` (`init --no-gpu`) |
| 2 | Whose machine is this, and how much of it may the search use? | at most half of the cores and memory idle when a step starts (Recommended on a machine shared with other jobs); a fixed number of cores and GB; the whole machine | half of what is idle | Machine and environment |
| 2 | Which environment runs the code, and may the search install packages into it? | the environment active in this session, nothing installed (Recommended); an environment or activation command the user names; a new one built under `$WORK/inputs/env/` | the session's, nothing installed | Machine and environment; the start of `--command` and `--tests` |
| 2 | Does the task name a worker, rank or thread count (`-p 4`, 8 threads)? An algorithm search asks its thread-count row instead, and this is that answer | the count; none | none | `config.json` `workers` (`init --workers`): A0 must show that many threads running at once |
| 2 | Is the code compiled? How is it built, and which compiler and flags must every build use? They are fixed for A0 and every iteration | the project's own build command and defaults, no variables (Recommended); name them (`CC=…`, `CFLAGS=…`) | the project's own | `init --build-env VAR=VALUE` (`config.json` `build_env`); the build at the start of `--command` |
| 2 | GPU search: how many GPUs does one iteration hold? Otherwise: how many of an iteration's runs go at once? | 1 (Recommended); 2; 4; the shapes in *Runs and GPUs* | 1 | Plan (`--gpus-per-agent`) |
| 2 | GPU search: which command gives a run its GPUs? | where the machine has an allocator or a scheduler, its words before a command, such as `srun --gres=gpu:1` (Recommended there); on a machine the user has to themselves, `env CUDA_VISIBLE_DEVICES=<index>` with the index they name (Recommended there); none, in plain mode only. On a machine shared with other jobs, never offer a fixed index | none: always asked in a GPU search | `config.json` `gpu_command` (`init --gpu-command`) |
| 3, the rules | How many runs per iteration? | 5 (Recommended); 1; 3; 10 | 5 | Plan |
| 3 | Which observers should every run record? | your proposed list (Recommended); fewer | your proposal | Observers |
| 3 | What earns a promotion, and what breaks a tie? | a median beyond the noise band of the parent's (Recommended); plus a tie rule such as "compile time decides a runtime tie" | no tie rule | Keep rule |
| 3 | Is anything off limits? | none (Recommended); named files, flags, functions | none | Off limits |
| 3 | May a change alter the public interface (signatures, command-line options, file formats), add a dependency, or change the build files? | none of them (Recommended); a new dependency, named or any; an interface change the user describes; the build, when tuning it is the task | none of them | Off limits; a yes to the build is `config --set allow_build_changes=yes` |
| 3 | Must the optimizations work on every common OS, or may they rely on this machine's? | every common OS: Linux, macOS and Windows (Recommended); this machine's OS only | every common OS | Portability |
| 3 | May the proposed external validation run once a round? | the proposal, with its command (Recommended); none | none until signed off | External validation |
| 4, the kind of search | the rows `references/algorithm.md` adds, then in repository mode the rows `references/repo-mode.md` adds | | | |
| 4 | Plain mode: is there a test suite to run on every iteration? | its command, with thread pools pinned; none | none | `init --tests` |
| 4 | Are there bugs you already know of in this code? Do the inputs or model weights carry license terms that restrict their use? | none (Recommended); the bugs, in the user's words; the terms, or where they are | none | `$WORK/interview/known-bugs/`; `$WORK/interview/TERMS.md` |
| 5, running it | Setup's own questions (a busy machine, a tolerance, a faster documented flag): ask each, or take the recommended answer and report it? | ask (Recommended); take the recommended answers | ask | `config --set after.setup_questions=recommended` |
| 5 | Which model runs the iteration subagents? | opus (Recommended); sonnet; haiku; fable | opus | `config.json` `subagent_model` (`init --subagent-model`) |
| 5 | How will you watch the search? | the notebook, where JupyterLab runs on this machine (Recommended); a static HTML page; the terminal only | the notebook where JupyterLab is found, else the terminal | which line of the first table of `references/dashboard.md` the user is told; the publisher starts either way |
| 5 | How much may the search spend? | no limit: the stop criteria decide (Recommended); wall-clock hours; a number of iterations | no limit | hours: `after.budget_hours`; iterations: Goal and scope. Either is the `budget` reason to stop |
| 5 | Will you be away while it runs? | answering when asked (Recommended); away: unanswered questions continue on the answer that does the least damage | answering | Goal and scope; when away, offer Claude Code's `askUserQuestionTimeout` setting (`references/step3-rounds.md`, *Questions while the search runs*) |
| 6, after the search | End the search on the stop criteria, or ask you? Reclaim the disk at the end? | `stop`: `ask` (Recommended) or `auto`; `disk`: `ask` (Recommended), `keep` or `prune`; `pair_cap_hours`: 4 (Recommended), the most the paired timing at `end` may take | `ask`, `ask`, 4 | `config.json` `after` (`init --after JSON`, `config --set after.X=V`) |
| 6 (repository mode) | The oldest toolchain the repository supports? May the new unit tests edit the repository's test driver? The trailer line every commit message ends with? | `oldest_toolchain_command`: a command that builds and tests a tree, `{src}`, with the oldest compiler or library the repository supports, or null when there is none (`references/promotion.md`, *The preflight*); `tests_driver_edit` `yes`, `no` or `ask`; `commit_trailer` from the main session's instructions, verbatim | null; `ask`; no trailer | the same `after` block |

The `after` answers are asked here only: every later stage reads `after`
(`autoopt.py config`) and asks only a field still `ask`. `after.build_outputs`
(default `[]`) lists files the build writes into the repository that the
runner committed, as paths relative to the repository's root or globs: an
untracked or edited file there is no unmeasured edit. Set it when
such files appear (`config --set 'after.build_outputs=["*.log"]' --reason …`).
`autoopt.py` keeps an `after` key that is not its own as it is, and refuses
`config --set` on one.

Then **read everything back in one paragraph, in this shape**, and go on to
`init` in the same turn: the read-back is not a question. A correction the
user makes to it changes the `init` flags or the `spec.md` line before they
are written; a later one is `config --set` for a setting and a dated rule in
`spec.md` for anything else.

> Kind: `<algorithm implementation>`, `<repository | plain>` mode. Objective:
> `<name>`, `<maximized|minimized>` (`<unit>`), measured on `<the command or
> call and its input>`; target `<value, or none>`; floor `<value, or none>`.
> Gates: `<name> <op> <threshold>` against `<reference>`, … Keep rule: "<…>".
> Observers: `<…>`. Thread counts timed, tracking inputs: `<…>`. Plan:
> `<runs>` runs per iteration; one iteration at a time on `<gpus>` GPU(s)
> through `<gpu command>`, or with no GPU and `<n>` runs at once. Unit tests:
> `<the command, or none>`, and new ones `<no | yes, scope>`. External
> validation: `<proposal, or none>`. Off limits: `<…>`; interface,
> dependencies and build `<unchanged | what may change>`. Portability:
> `<every common OS | this OS only, named>`. Machine: `<cores and memory the
> search may use>`; environment `<…>`, installs `<no | yes>`; compiler and
> flags `<the project's own | named>`. Known bugs and license terms: `<…, or
> none>`. Directory: `<path>`. Running it: setup's questions `<ask |
> recommended>`, subagent model `<…>`, watched in `<…>`, budget `<…, or
> none>`, user `<answering | away>`. After the search: stop `<auto|ask>`,
> disk `<…>`, paired timing capped at `<hours>`; in repository mode also
> oldest toolchain `<command, or none>`, test driver `<…>`, trailer `<…>`.
> Defaults taken without asking: `<…>`, or none.

## The objective is one number

One number per run, maximized or minimized. If the user names two, one of
them is a gate or an observer. Work out which and say so:

> "Faster and more accurate" is not one objective. Which one is the thing
> being improved, and what is the other one allowed to do?

A benchmark that cannot emit the objective as a finite number is fixed at
step 2, not worked around.

## Gates

A gate is a hard constraint checked on **every run**. One violating run fails
the iteration however good its median. The user's words almost never
determine the gate, so read back the exact form before writing it down.

| What the user says | What it could be | Ask |
|---|---|---|
| "the predictions must not change" | bitwise identity; max abs difference ≤ 1e-5; correlation ≥ 0.999 | which, and against what reference |
| "the output must be identical", for a dict, list or string | equal as values; also in the same order (dict insertion order, list order) | whether order counts |
| "must stay fast" | median latency; p99 latency; throughput | which statistic, measured over what |
| "must not use more memory" | peak allocated; peak reserved; resident | which, and measured when |
| "the error must not grow" | not above baseline; not above baseline + 1e-6 | the exact ceiling, as a number |
| "under 5000 allocations" | `<` or `<=` | which: for an integer count they differ by one |

A bitwise gate counts what differs and requires zero:
`{"name": "n_mismatch_vs_base", "op": "==", "threshold": 0}`.

Every gate ends up as `{"name", "op", "threshold"}`, with `op` one of `<=`,
`<`, `>=`, `>`, `==`, and an optional `tol`. On `==`, `tol` makes a band,
\|value − threshold\| ≤ tol. On the other four it loosens the threshold by
`tol`, so leave it off unless the user asked for that. Write the reference
into the name when there is one: `max_abs_diff_vs_base` keeps its meaning when
read alone; `diff` does not.

**A gate needs a reference that cannot move**: a file on disk written once,
the baseline's outputs, not "whatever the current best iteration produces".
The setup subagent wires it up at step 2 and proves the gate against three
kinds of change before the baseline runs (`references/step2-baseline.md`,
*Prove the gate*).

A numeric tolerance agreed here is provisional until step 2 computes the error
distributions it has to separate (*Compute a numeric tolerance against real
data*, same file), and the user decides again from them. Say so when the user
names one.

## Observers

Recorded on every run, never optimized and never gated, observers show what a
move in the objective cost: peak memory, compile time, records per second,
the thread count reached, a secondary timing.

Propose more rather than fewer. Each is one more key in `metrics.json`, and
an observer can become a gate later, while a number nobody recorded can only
be recovered by re-running every iteration.

The group-3 proposal leaves out the observers that rows from `algorithm.md`
and the mode's file ask about in group 4 (the memory measure, the
tracking datasets, a second thread count).
Those answers join the list in the read-back, so no group-3 answer depends on
a later one.

## Runs and GPUs

**Runs per iteration** default to 5: every iteration, A0 included, runs that
many, one seed each, and the median of five resists two bad seeds. One is
honest only for a deterministic benchmark, such as a timing harness with a
fixed input. Quote the cost as runs per iteration times the iterations, A0
included.

**One iteration runs at a time.** Each is measured against the root the
previous one left, and an iteration that passes the keep rule becomes the
root, so gains accumulate. A regression in a promoted root is inherited by every
iteration after it; the paired comparison against A0 at the end
(`references/step4-final.md`) is what shows it.

**GPUs per iteration** default to 1: the iteration holds one card and runs
its seeds one after another. Raise it and the iteration holds that many and
runs that many seeds at once, so each iteration finishes sooner. `gpus_per_agent`
must fit in the free GPUs; check what is free before promising a number, with
whatever allocates GPUs on the machine, or `nvidia-smi` where nothing does.
Without a GPU, `gpus_per_agent` is how many of an iteration's runs go at once.

**For a CPU benchmark, the concurrency agreed here is provisional.** Runs at
once (`gpus_per_agent`, or the runs an iteration starts at once without a GPU)
can push a memory-bound objective outside the noise band while most cores sit
idle, so step 2 measures how many concurrent runs the benchmark tolerates
before the baseline (`references/step2-baseline.md`, *Measure what concurrency
does to the objective*), and a narrower answer comes back to the user.

**Promotion.** Each promotion closes a round, and an iteration's runner
promotes it the moment it passes the keep rule. That is the recommended rule
only: a keep rule with a tie rule or a second measure is `init --promote
orchestrator`, and the orchestrator promotes by hand
(`references/promotion.md`).

## Writing it down

`<name>` in the directory is what the user called the search, or a short slug
of the task; the date is the day the search starts. `init` creates
`autooptimize/` inside it, and the harness, data, notes and later work on the
search all stay under it.

In repository mode with a new unit-test suite, `init` waits: step 1 creates
the clone that `init --repo` copies, runs `init` on it, and the TODOs below are
filled then (`references/step1-unit-tests.md`).

```bash
WORK=~/auto-optimize/2026-10-01-latency   # the directory the user chose

python3 $AO/autoopt.py init $WORK \
	--task 'Cut single-request latency without moving the outputs.' \
	--command "AUTOOPT_REF=$WORK/autooptimize/reference python3 bench.py --iteration {iteration} --seed {seed} --out {out}" \
	--objective latency_ms --direction min --unit ms \
	--gates '[{"name": "max_abs_diff_vs_base", "op": "<=", "threshold": 1e-5}]' \
	--observers peak_mem_gb,requests_per_s \
	--runs 5 \
	--gpus-per-agent 1 \
	--copy /path/to/bench.py --copy /path/to/kernels.py
```

These flags are decided here, not later:

- **`--copy`** puts a file or directory into the baseline worktree; repeat it.
  Without it the baseline is empty and the benchmark is written into it by
  hand. In repository mode `--copy` fills `harness/` instead, and `--repo`
  and `--tests` are set here too (`references/repo-mode.md`, *Setting it up*).
- **`--no-gpu`** sets `"gpu": false` for a benchmark that needs no GPU, which
  lifts the lease check.
- **`--gpu-command`** records `gpu_command`, the allocator's words (`references/step2-baseline.md`, *GPU leases*).
- **`--command`** in double quotes, so the shell writes `$WORK`'s value into
  `config.json`; placeholders stay in braces (`references/layout.md`,
  *config.json*).

`init` validates as it writes: a `command` without `{seed}` and `{out}`, or a
gate without a numeric threshold, fails at once. It writes
`autooptimize/spec.md` with a TODO line for each answer, and creates
`reference/`, `ideas.md`, `knowledge.md` and the `tools/` READMEs.

**Replace every TODO in `spec.md` from the interview** (the machine and
environment answers go on its *Machine and environment* line, the
representative run and any target under *Goal and scope*), except three that the
setup subagent answers at step 2: *Gate proof*, *Width measured* and
*Benchmark*. Until all are
gone, `new`, `brief` and `run_iteration.py` refuse (`refusing to fork an
iteration: step 0 is not finished`, `refusing to run A0: …`) and print the
remaining TODO lines. An existing `spec.md` is never overwritten, even by
`init --force`.

Step 0 also leaves what is not `spec.md` in the search directory
(`references/layout.md`, *The search directory*): the questions and the
user's answers, in their words, as `$WORK/interview/interview.md`; the
license terms the user named for the inputs and the model weights, copied
verbatim (`TERMS.md`, `terms_raw/`); and `known-bugs/`, the bugs the user
already knows, so later steps do not report them as new. What the benchmark
reads (data, the environment, toolchains) goes in `$WORK/inputs/` (`data/`,
`env/`, `toolchains/`), not at the top of `$WORK`.

**Every rule is in `spec.md` before it is used.** A changed setting goes
through `autoopt.py config --set … --reason "<the user's words>"`, which also
writes the dated rule; any other rule is written under *Rules added during the
search* with the date and the user's words, **before** acting on it.

If the surrounding project is a git repository, add `autooptimize/` to its
ignore list: it holds a git repository of its own and every run's outputs.

## Handing off step 2

Step 2 belongs to a setup subagent. The idea miner starts after it, not
beside it: stage S1 prices every idea against A0's profile, which setup writes
after the baseline, and a miner started earlier prices blind. `setup`
refuses while a TODO other than *Gate proof*, *Width measured* and *Benchmark* is left (`refusing to start …:
the interview is not written down`):

```bash
python3 $AO/autoopt.py setup $WORK   # opens step 2; prints the setup subagent's prompt (briefs/setup.md)
```

Spawn it with the printed prompt unedited, then end your turn: its report
wakes you, a setup question included, so start no timer, watch or polling
loop. With a new unit-test suite, `init` comes at the end of step 1, and it
starts then.

When the setup subagent reports done, open the miner and spawn it:

```bash
python3 $AO/autoopt.py mine $WORK    # opens stage S1; prints the idea miner's prompt
```

`mine` refuses stage S1 while `checks.profile` is registered and `iterations/0/profile.txt`
does not exist; `--without-profile` starts it blind.

The setup subagent does all of step 2 (`references/step2-baseline.md`) and
reports its *Gate proof* and *Width measured* lines, A0's runner block and
each check's proof. While it works:

- **Questions for the user** (`references/step2-baseline.md`, *Decisions
  that are the user's*). The subagent records them with `setup --ask`, does
  everything that does not depend on them, and stops once; `status` prints
  them on its `setup (step 2): waiting for the user:` line. Ask them in one
  AskUserQuestion call, then run `python3 $AO/autoopt.py setup $WORK --answer
  "<their words>"`, which writes the dated line in `spec.md` (a setting they
  change is also `config --set … --reason`), and send the message it prints
  to the setup subagent (SendMessage); if that subagent is gone, spawn a new
  one with the prompt it prints after the message.
- **A setup subagent that died** without reporting: `setup --force` prints a
  prompt for a new subagent that continues from its notes,
  `autooptimize/setup/notes.md`. `setup` alone refuses while step 2 is open
  (`step 2 is already with a setup subagent`), and `--ask`, `--answer`,
  `--force` and `--done` refuse when none is (`no setup is open`).
- **`brief` refuses** (`step 2 is still with the setup subagent`) until the
  subagent closes step 2 with `setup --done`.

`status` prints `first iteration: waiting for setup (step 2) and the idea
miner's S1`, naming whichever is not done, until both are done. Then start the
orchestrator subagent; from here on, `references/main-session.md` lists what
the main session does at each event.

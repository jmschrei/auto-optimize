# Glossary

Every term with a fixed meaning. Other files use them exactly as defined here;
two words that look interchangeable are not.

## The unit of work

| Term | Meaning |
|---|---|
| **run** | one execution of the benchmark with one seed. Writes one `metrics.json` |
| **iteration** | one idea, measured by several runs and reduced to their median. One subagent, one worktree, one branch |
| **round** | the iterations forked from one root, closed by `promote`. One summary each. Numbered from 1; A0 carries round 0 |
| **width** | the concurrent runs one iteration can start, `gpus_per_agent` (without a GPU, the runs it starts at once): the concurrency step 2 measures. One iteration is in flight at a time |
| **settled** | has a `result.json`, whatever its status, `partial` included. The opposite is **in flight** |
| `running`, `finishing`, `STALLED` | `status`'s list of subagents still working, from which `brief --next` counts the free slot (one): `running` has no `result.json`; `finishing` is measured, has no `README.md`, and was written to within the hour; either is also `STALLED` after 15 minutes without a write, and still holds its slot until it is handed over |

## Who does what

| Term | Meaning |
|---|---|
| **orchestrator** | the session running the search, and from the first iteration on the **orchestrator subagent** it starts in **phases** (`autoopt.py loop`; each hands back after `loop_handback` settled iterations, at *When to stop*, or with a question). The main session spawns the setup subagent and the first idea miner; the orchestrator subagent spawns iteration subagents and later miners, briefs from `ideas.md`, reads `status --brief` and short reports, relays questions to the user, shows the user round summaries. Measures, promotes (except with `promote: orchestrator`), and strikes nothing, checks no ledger, writes no summary prose, and reads no code, logs, READMEs or outputs |
| **phase** | one run of the orchestrator subagent, opened by `autoopt.py loop` and numbered from 1. It ends after `loop_handback` settled iterations, at *When to stop*, or with a question for the user. The noise band's reference spread is re-measured from the root when a phase opens and held for it |
| **setup subagent** | the subagent that does step 2, by `references/step2-baseline.md`: the benchmark, the gate proof, the checks, the width, A0, the first ledger entries and A0's README. Started by `autoopt.py setup`; asks the user through `setup --ask`; closed by `setup --done` |
| **idea miner** | a subagent that reviews the code, the ledger and the READMEs and appends ideas to `ideas.md`, by `references/idea-miner.md`. Started by `autoopt.py mine` once setup has closed (A0's profile exists) and whenever fewer than two untried ideas are left; one at a time |
| **stage**, `S<n>` | one run of the idea miner. `new` stamps the last finished stage on each iteration (`mining_stage`); the dashboard's stage boundaries fall where it changes. Not `result.json`'s `stage` |
| **check** | the task's own checks, written once at step 2 into `checks/`: the cold first call and the compare against A0's cached outputs, run by the runner among its stages (`references/subagent.md`, *Checks beyond the runner*) or by hand with `autoopt.py check`. Its `check:` line is in the report block |
| **subagent model** | `config.json`'s `subagent_model` (`opus`, `sonnet`, `haiku` or `fable`), the model every iteration subagent is spawned with; the miner and the orchestrator keep the session's |
| **subagent** | runs one iteration by `references/subagent.md`, writes its README and stops |
| **brief** | everything a subagent knows about its iteration, written by `autoopt.py brief` into `briefs/<id>.md`; the Agent call carries only a short prompt that has the subagent read it (`references/writing-briefs.md`) |
| **lease** | the GPUs one command holds while it runs, named by `CUDA_VISIBLE_DEVICES`: taken by the scripts themselves through `gpu_command`, the machine's allocator, or given as indices where there is none (`references/step2-baseline.md`, *GPU leases*) |
| **`gpu_command`** | the machine's GPU allocator, as the words that go before a command and run it with `CUDA_VISIBLE_DEVICES` set (`srun --gres=gpu:1`); see **lease** |
| **`workers`** | the worker, rank or thread count the task names; A0's runs must show that many threads running at once (`references/step2-baseline.md`, *The price of the base*) |
| **floor** | a minimized objective's smallest value worth measuring (`config.json` `floor`, the user's at step 0): at it the search stops (`references/step3-rounds.md`, *When to stop*) |
| **price of the base** | what each later step that runs the base costs, from A0's seconds a run (`autoopt.py price`; `references/step2-baseline.md`, *The price of the base*) |
| **the user** | the person who asked for the search. Decides gates, tolerances, the keep rule, external validation and anything off limits |

## Places

| Term | Meaning |
|---|---|
| `<work>`, `$WORK` | the search's directory, chosen at step 0 (default offered: `~/auto-optimize/<YYYY-MM-DD>-<name>/`). Holds `autooptimize/` and later work on the search (`references/layout.md`, *The search directory*) |
| `$AO` | this skill's `scripts/` directory |
| `autooptimize/` | the search's record: `config.json`, `spec.md`, `state.json`, `ideas.md`, `knowledge.md`, `tools/`, `briefs/`, `setup/`, `reference/`, `repo.git/`, `iterations/`, `rounds/` (`references/layout.md`). `autoopt.py` accepts `<work>` or this directory |
| `$DIR` | one iteration's directory, `autooptimize/iterations/<id>/`, which `autoopt.py new` prints |
| **worktree**, `$SRC` | the iteration's code checkout: `$DIR` in plain mode, `$DIR/src` in repository mode. The brief gives both paths |
| **branch** | `autoopt/<id>` in `repo.git`: the parent's branch plus this iteration's commit |
| **plain mode** / **repository mode** | the benchmark and code copied in with `init --copy`, or an existing git repository copied with `init --repo`, which is never written to (`references/repo-mode.md`) |
| **benchmark** | the script one run executes; its output is `metrics.json` |
| **harness** | the benchmark in repository mode: `autooptimize/harness/`, outside every worktree, fixed for the whole search |
| **reference** | the baseline's saved outputs in `autooptimize/reference/`, which an identity gate compares every run to. Written only by A0 |
| **notes** | `$DIR/notes.md`, the subagent's prose; `autoopt.py finish` assembles it with the numbers into `README.md` |
| **publisher**, **writer** | the background process that scans the search and publishes the dashboard's data (`references/dashboard.md`) |
| **probe** | a subagent's own script for a check the runner does not make (a fallback path, a sweep of variants), kept under `$DIR/probe/` |

## Names

| Spelling | Names |
|---|---|
| `auto-optimize` | this skill; the unit-test branch `auto-optimize/<date>-unit-tests` |
| `autooptimize/` | the search's record directory |
| `autoopt` | the scripts (`autoopt.py`), iteration branches (`autoopt/<id>`), the environment variable `AUTOOPT_REF`, the dashboard's files (`autoopt_monitor.py`, `~/autoopt-monitor/`) |

## Measures

| Term | Meaning |
|---|---|
| **objective** | the one number per run being improved, with a direction (`max` or `min`) |
| **gate** | a hard constraint checked on every run; one violating run fails the iteration. `{"name", "op", "threshold", "tol"}` |
| **observer** | a number recorded on every run, never optimized and never gated |
| **tracking measure** | an observer that is the objective on other inputs (`seconds_tiny`, the seconds on a second input) |
| **external validation** | a check on inputs the search never measures, run once a round, or by a subagent on its own iteration when a brief asks. Chooses nothing; runs only after the user signs off (`references/external-validation.md`) |
| **unit-test gate** | the package's tests, run once per iteration after its first run passes its gates. A failure is `gate-failed`. With `{selected}`, the runner runs a **subset**, the files for the changed modules, and then the full suite when the median passes the keep rule; `promote` refuses a subset-only pass |
| **noise band** | the wider of the reference spread (the standard deviation across the root's runs, re-measured each time a phase opens; A0's before the first) and this iteration's: the margin the default keep rule asks a median to beat its parent's by |
| **paired** | two versions compared in alternating separate processes, stopped by a sign test: `scripts/paired.py`. A search runs it once, at the end: `autoopt.py final`, A0 as A, the winner as B. Verdicts `B better`, `B worse`, `no difference resolved` |
| **control** | the unchanged parent, re-measured as its own iteration (briefed to a subagent with `brief --control`) or as a variant, to show what a measurement reads for no change. Never on the frontier and never promoted |
| **cold** | the first call in a fresh process with an empty compiler cache, timed by the runner with `scripts/cold.py` before each iteration's runs (`checks.first_call`) |
| **prediction** | the change an iteration is expected to make to its parent's median, in the objective's units: `--predict` |

## Measuring an iteration

| Term | Meaning |
|---|---|
| **runs per iteration** | `runs_per_iteration`, chosen at step 0 (default 5): every iteration, A0 included, runs that many, one seed each, and its median is its result |
| **depth** | how many runs an iteration has; at full depth it has `runs_per_iteration` |
| `status` | `ok` (every run passed every gate, and the tests); `gate-failed`; `error` (a run crashed or wrote nothing usable); `partial` (fewer runs than `runs_per_iteration`: never compared to anything). **Passing** means `ok` |

## The tree

| Term | Meaning |
|---|---|
| **label** | the letter `A` and an iteration's number, `A107`, used wherever an id is written; a bare `107` names the same iteration. Directories and branches carry the bare number |
| **baseline** | A0: the unmodified code, measured like any iteration by the setup subagent at step 2 |
| **parent** | the iteration a new one forks from; the current root unless a brief names another |
| **root** | the iteration new ones fork from: the last promoted iteration, or A0 before any |
| **promote** | make an `ok` iteration the root (`autoopt.py promote`), closing the round |
| **keep rule** | what earns a promotion, written in `spec.md` before the first iteration: by default, a median beyond the noise band of the parent's, plus any **tie rule**, what the user said decides a tie (a compile-time cut, say). The runner applies the default rule and records its verdict as `keep`; it promotes on it with `promote: auto` |
| **frontier** | the best `ok` median so far, and the iteration holding it. Never a single run. The keep rule judges a child against its parent, the root, so briefs, `status` and reports name the root as the reference and show a better median outside the root's path as "best median, not in the root" (kept out by the noise band, or passed but not promoted) |
| **frontier path** | the iterations that each beat everything before them, in id order: a record of progress, not a lineage. "On the frontier" in `status` and the dashboard means on this path |
| **path** to N | every iteration from A0 to N following parents, N included: `autoopt.py path --id N` |
| `best_run` | the single best run inside one iteration: one sample, never a score |
| **category** | what an idea changes: `MICRO`, `NUMBA`, `CACHING`, `CACHE_MISS`, `PRECISION`, `PARALLEL`, `REORGANIZATION`, `IO`, `ALGORITHM`, `OPTIMIZATION` (each defined in `references/idea-miner.md`). `MISC` is the catch-all; `ROOT` is the baseline's alone |
| **topic** | the block of ideas a subagent owns when the user divided work by topic |
| **toolchain-specific** | a change whose gain depends on a compiler's, version's, flag's or pass's decisions. The miner proposes none; a subagent that answers `yes` in `notes.md`'s `## Toolchain` closes it, and `promote` refuses it (`references/subagent.md`, *Portability*) |

## Combining changes

| Term | Meaning |
|---|---|
| **transplant** | a change moved from one search into another; priced again there, never assumed |

## After the search

| Term | Meaning |
|---|---|
| **the base** | the original code the winner is compared against and merged onto: a branch of a git repository (`init --repo`) or stand-alone code (`init --copy`). Not the baseline, which is A0, the iteration that measures the same code |
| **winner** | the chosen path: the best median among the root and the iterations descending from it that passed the keep rule (their code holds every kept change; a better median outside the root's line does not). Each iteration on it is a **winning iteration**. `best` is the literal value of `--id`, the default of `final`, `end`, `path` and `archive` |
| **ended** | the state after `autoopt.py end`: `ended.json` records it (`references/step4-final.md`); `end --reopen` undoes it until the winner is published (`reopen-lock.json`) |
| **winning iteration** | an iteration on the winner that changes the code |
| **preflight** | `preflight.py`: the checks of a change's added lines that need no measurement, at promotion (`references/promotion.md`, *The preflight*) |
| **`--after-end`** | the flag of `new` and `brief` for an iteration made after `end`, the only kind made then; promoted, it becomes the winner (`references/step4-final.md`, *An iteration made after the end*) |
| **move** | one line of `stages.py next`, with its kind: `run` (a script; `--run` starts it), `wait` (`end` is finishing), `agent` (the session does it), `stop` (the user answers) |
| **prune** | delete regenerable artifacts, keeping every record (`autoopt.py prune`) |
| **reset** | delete every iteration but the kept ones, records included (`autoopt.py reset`) |
| `<<FILL: …>>` | the placeholder a generated skeleton leaves where prose goes (`notes.md`, the final report). The command that consumes the file refuses while one is left |

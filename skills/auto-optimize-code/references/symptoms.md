# By symptom: an error, a refusal, or something that looks wrong

| Symptom | Read |
|---|---|
| `commits …, which the unit tests create`; a test's output file (an index, a cache) left in a worktree | `references/repo-mode.md`, *The unit-test gate* (Artifacts) |
| `CUDA_VISIBLE_DEVICES is unset, so this run holds no GPU lease` | `references/step2-baseline.md`, *GPU leases* |
| `metrics.json has no numeric value for the gate "…"` | `references/layout.md`, *metrics.json — the contract every benchmark meets* |
| `config.json "command" must contain both {seed} and {out}`, `has {…}, which is not a placeholder` | `references/layout.md`, *config.json* |
| `copied file(s) match the plain-mode exclude list` at `init` | `references/layout.md`, *The git model* |
| `… is not valid JSON … repair it or move it aside` | `references/layout.md`, *The `autooptimize/` tree* |
| `this search was made by the … skill` | `references/layout.md`, *Version fields*: read that search with the skill it names |
| `fewer than 2 untried ideas`; `stage S<n> of the idea miner is still open` | `references/step3-rounds.md`, *Keeping the ideas* |
| `refusing to start step 2` or `refusing to start the idea miner: the interview is not written down`; `step 2 is still with the setup subagent`; `step 2 is already with a setup subagent`; `no setup is open`; `setup (step 2): waiting for the user`; `first iteration: waiting for …` | `references/step0-interview.md`, *Handing off step 2* |
| `step 2 is not finished` | `references/step2-baseline.md`, *Closing step 2* |
| `has no passing check`; a `check:` line reading `compare differ` or `error`; `failed_check` | `references/subagent.md`, *Checks beyond the runner* |
| `compare could not cache A0's outputs`, `wrote nothing into`, `does not pass A0 against its own cached outputs` | `references/step2-baseline.md`, *Write the checks* |
| `refusing to fork an iteration` / `refusing to run A0: step 0 is not finished` | `references/step0-interview.md`, *Writing it down*; for `Gate proof` and `Width measured`, `references/step2-baseline.md`, *In this order* |
| A question for the user while iterations are in flight; the search stops while the user is away | `references/step3-rounds.md`, *Questions while the search runs* |
| `parent … is gate-failed` | `references/step3-rounds.md`, *Ids, parents and briefs* |
| `brief` refuses: `is taken`, `this search uses GPUs: pass --gpus` | `references/writing-briefs.md` |
| `finish` or `readme` refuses: `placeholder`, `lacks`, `no …notes.md`, `a checker must pass`, `(corrects: …)" matches` | `references/subagent.md`, *Before you stop* |
| `status` shows `STALLED`, `no README:` or `summary overdue`; `summary` says `no settled iteration … carries round N` | `references/step3-rounds.md`, *A subagent that goes quiet*; *The round summary* |
| `promote` refuses: `is gate-failed`, `does not descend from the root`, `passed only the unit tests for the modules it changed` | `references/promotion.md`, *Promotion follows the keep rule* |
| The frontier moved tenfold and the keep rule's margin cannot be met, or the timed call fell below 10 ms | `references/step3-rounds.md`, *When the objective moves by an order of magnitude* |
| `validate` refuses: `has not been approved` | `references/external-validation.md` |
| `iteration N has no result.json`, `has no meta.json` / an iteration never finished | `references/subagent.md`, *When a run fails* |
| The dashboard is empty, shows another search's numbers, reads **publisher stopped** or **re-run the cell**, or is stuck on `Loading the dashboard…` | `references/dashboard.md`, *When it looks wrong* |
| `status` says `dashboard: no publisher is running` | iterations are in flight and nothing publishes: run the `--start` command it prints (`references/dashboard.md`) |
| The user asks for the dashboard's URL or port | there is none: they open `$WORK/dashboard.ipynb` in the JupyterLab they already reach and run its cell, or you send them an `--export` file (`references/dashboard.md`) |
| Share a finished search as one HTML file | `references/dashboard.md`, *Managing publishers* (`--export`) |
| The external validation reads **needs attention** | `references/external-validation.md` |
| Timings jumped between rounds, or an unchanged control got slower | `references/step3-rounds.md`, *The machine during the search* |
| Runs inside one iteration read slower than one run alone | `references/step2-baseline.md`, *Measure what concurrency does to the objective* |
| A run takes several times one call's cost | `references/step2-baseline.md`, *One run is a warmup and one timed call* |
| A run's `log.txt` ends `no reference for seed …` | `references/step2-baseline.md`, *A gate that compares against the baseline's output* |
| A tolerance nothing can satisfy, or one nothing can fail | `references/step2-baseline.md`, *Compute a numeric tolerance against real data* |
| The same edit measured two values on two roots, a change came in short of its prediction, or a gain was predicted in percent | `references/promotion.md`, *Predicting what a change is worth* |
| A timing harness or an in-process sweep gives a different answer each time | `references/subagent.md`, *Timing variants*; `references/measuring.md` for `paired.py` |
| A brief aimed at a kernel that does not dominate the profile | `references/measuring.md`, *Profile the root you are actually on* |
| A variant is suspiciously fast | `references/measuring.md`, *A suspiciously large speedup is usually dead code* |
| Assembly-instruction counts read 0 for everything, on both branches | `references/measuring.md`, *Reading the generated code* |
| A numba error or warning: `TypingError`, `missing Environment`, `Cannot cache compiled function`, a segfault with no message | `references/algorithm.md`, *Numba* |
| A brief's premise turns out false | `references/subagent.md`, *Check your brief before you follow it* |
| A change passes every run here but breaks the package | `references/subagent.md`, *Off-benchmark callers of the code you are changing* |
| `has uncommitted changes to tracked files`, `detached HEAD` at `init --repo` | `references/repo-mode.md`, *Setting it up* |
| Every iteration matches the baseline to the last digit | `references/repo-mode.md`, *Import from the worktree, never the installed package* |
| `unit tests: FAIL`; a `tests` column reading `FAIL` or `pass sub`; `promote` refuses `passed only the unit tests for the modules it changed` | `references/repo-mode.md`, *The unit-test gate* |
| `fatal: … already registered worktree`, `a branch named autoopt/N already exists` | `references/repo-mode.md`, *Common errors* |
| `init` refuses `commits ahead of its base: the unit-test suite is one commit` | `references/step1-unit-tests.md`, *Where it lives*, step 2 |
| A unit test that passes here and fails in the repository's CI (`--runxfail`, macOS, another Python, an older Cython) | `references/step1-unit-tests.md` |
| `no gpu_command`, or `CUDA_VISIBLE_DEVICES is unset … no gpu_command is recorded` | `references/step2-baseline.md`, *GPU leases* |
| `the root reached the floor` (`stages.py next`) | `references/step3-rounds.md`, *When to stop*: end, a new search on a larger input, or `config --set floor=<lower, or 0>` |
| `the task names N workers and A0's runs never had more than M thread(s) running` (`setup --done`) | `references/step2-baseline.md`, *The price of the base*: the worker count is not in effect in the benchmark |
| `timed out` from the paired timing: one run hung and was killed | read `final/paired.log` and run that side's timing command by hand; `end` again once it returns |
| `over the cap` or `over cap` from the paired timing in `end` or `final` | `references/step4-final.md`, *What it does, in order* |
| `readme` or `finish`: `notes.md lacks ## Change` / `## Commit message` | an iteration whose `keep:` line reads `promoted` or `passes` writes both: `references/subagent.md`, *The README* |
| `promote`: `fails the preflight` | `references/promotion.md`, *The preflight* |
| `config --set after.X=V` refuses: `'after.X' cannot be set here` for a key this skill does not define; a search with no `after` block | `references/layout.md`, *config.json*, `after` row: `config --set` creates the block, never `init --force` for it; a key this skill does not define is kept as it is |
| `has uncommitted changes that were never measured` naming a file the build writes (`configure.log`, a stamp) | `after.build_outputs`: `references/step0-interview.md`, *The questions* |
| `ship.py` refuses (`apply`, `branch`, `rehearse`, `post`) | `references/step5-ship.md`, *Refusals* |
| `end --reopen`: `the search cannot be reopened` | `references/step4-final.md`, *Reopening* |
| `the search ended on …: only an iteration made after the end`; `refusing to fork an iteration` after `end` | `references/step4-final.md`, *An iteration made after the end* |
| A search is running and this session knows nothing about it | `references/resume.md` |
| `has no git checkout: it was pruned, or made by hand`; the search directory has grown to tens of GB | `references/step6-finish.md`, *3. Reclaim the disk* |
| `the harness changed since …`, `the winner relies on the toolchain` | `references/subagent.md`, *Portability* (Toolchain); `autoopt.py harness --accept --reason` for a change that is meant |
| Start the ideas over without re-running the baseline | `references/changes.md`, *Starting the ideas over, keeping the baseline* |

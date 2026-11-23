# Changing what the skill does

Most changes a user wants are settings a search already reads; a few need
the code edited. This file lists both, each with the file that explains it.
A setting changed while a search runs is recorded with the user's words
(`config --set … --reason`), and some change how later iterations are judged
(`references/changes.md`).

## Settings a search reads

Every one is a `config.json` key, set at `init` or with `autoopt.py config
$WORK --set KEY=VALUE --reason "<the user's words>"`; `references/layout.md`,
*config.json*, defines each. `config --set` creates the `checks`, `after` and
`keep` blocks when they are absent; the `tests` and `external_validation`
blocks are added only with `init --force`, and `config --set` then edits
them. When it refuses a key, it lists every key it accepts.

| To change | Set | Read |
|---|---|---|
| The run plan: runs, the time limit, the GPUs, the floor, the worker count, a gate's threshold, the build's variables, the task | `runs_per_iteration`, `confirm_runs`, `staged`, `timeout_seconds`, `gpu`, `gpus_per_agent`, `floor`, `workers`, `gates.<name>.threshold` (`.op`, `.tol`), `build_env.<VAR>`, `task` | `references/layout.md`, *config.json*; a gate: `references/changes.md`, *Changing a gate in place* |
| What one run measures and how it is launched | the benchmark, which writes `metrics.json` (any objective, gates and observers); `command` until the baseline has run | `references/layout.md`, *metrics.json — the contract every benchmark meets* |
| A number recorded from now on, never optimized | `config --add-observer NAME`; it gets its own dashboard chart | `references/changes.md` |
| How outputs are compared, the timing probe, the first call, the build, the profile | `checks.compare`, `checks.time`, `checks.first_call`, `checks.build`, `checks.profile`, `checks.threads` | `references/step2-baseline.md`, *Write the checks* |
| The package's tests as a gate, a subset first | `tests`, with `{selected}` | `references/repo-mode.md`, *The unit-test gate* |
| A wider margin, or a least gain, for keeping a change | `keep.band_k` (noise bands, default 1), `keep.min_gain_pct` (percent of the parent's median, default 0); the runner still promotes | `references/promotion.md` |
| Any other keep rule (a tie rule, a second measure that must not regress) | `promote: orchestrator`, and the rule in `spec.md` | `references/promotion.md` |
| The machine's GPU allocator | `gpu_command`, the words before a command that set `CUDA_VISIBLE_DEVICES` | `references/step2-baseline.md`, *GPU leases* |
| When another process counts as busy | `busy_percent` (default 110, percent of one core) | `references/step2-baseline.md`, *Check the machine is quiet* |
| A check on inputs the search never measures | `external_validation`, run only after the user approves | `references/external-validation.md` |
| How long an orchestrator phase runs before it hands back | `loop_handback` (settled iterations, default 16) | `references/step3-rounds.md`, *The orchestrator subagent* |
| The model the iteration and setup subagents run on | `subagent_model` | `references/step0-interview.md`, *The questions* |
| Build files, binaries or vendored code as fair game | `allow_build_changes` | `references/subagent.md`, *Portability* |
| When to stop, the budget, the disk, the paired timing's cap, the commit trailer | `after.stop`, `after.budget_iterations`, `after.budget_hours`, `after.budget_stages`, `after.disk`, `after.pair_cap_hours`, `after.commit_trailer`, … | `references/step0-interview.md`, *The questions* |
| How the winner ships, and the branch names | `after.ship`, `after.ship_documents`, `after.branch_prefix`, `after.pr_remote`, `after.pr_head_owner`; `--branch DOC=NAME` on `ship.py rehearse` and `post` | `references/step5-ship.md` |

## Code a search shares

`autooptimize/tools/` holds helpers later iterations reuse, and
`tools/checkers/` the checkers; each is listed in its folder's `README.md`
(`references/shared-knowledge.md`). A benchmark reads the baseline's saved
outputs from `AUTOOPT_REF`, which its command sets
(`references/step2-baseline.md`).

## Environment variables

| Variable | Does |
|---|---|
| `AUTOOPT_TMPDIR` | the folder the scripts put temporary files in |
| `AUTOOPT_BUSY_PERCENT` | overrides `busy_percent` |
| `AUTOOPT_SKIP_QUIET_CHECK=1` | skips the busy check before A0, in `width` and in `busy`; the way past it on a machine without `/proc` |
| `AUTOOPT_SKIP_SPEC_CHECK=1` | skips the refusals while `spec.md` has a TODO line, for tests and scripted searches |
| `CUDA_VISIBLE_DEVICES` | when set, taken as the GPUs this process already holds under a lease, so `gpu_command` is not run |
| `CLAUDE_CODE_SESSION_ID` | recorded by `init` as `origin.session`; `tokens` adds the project folder holding that session's transcript to the folders it reads |
| `GPU_LEASE_DIR` | a folder of GPU lease files the dashboard reads lease holders from (`references/dashboard.md`) |
| `GPU_SCAN_NVML` | the NVML library file the dashboard loads in place of `libnvidia-ml.so.1` |
| `JUPYTER_RUNTIME_DIR` | one more folder the dashboard looks in for running Jupyter servers |

`AUTOOPT_LEASE_MARK`, `AUTOOPT_DETACHED` and `AUTOOPT_WRITE_REFERENCE` are set
by the scripts for their own children; do not set them.

## Hooks for a skill built on this one

A skill that writes the same layout under other rules (`auto-optimize-ml`)
imports `autoopt` and adds to it, rather than copying it:

| Hook | Does |
|---|---|
| `config.json` `skill.name` | the skill a search belongs to; every script refuses a search another skill made (`references/layout.md`, *Version fields*) |
| `autoopt.register_after(defaults, kinds, choices, whole, nonneg, checks)` | adds that skill's `after.*` answers, which `config --set` then accepts and checks |
| `autoopt.EXTRA_PARSERS` | a list of `add(subparsers)` functions run when `autoopt.main()` builds its parser: subcommands of that skill's own |

## When no setting fits: where to edit

In a clone of the repository, edit the file, then run the suites named
(`python3 tests/code/run_tests.py <suite>`; the dashboard's are
`tests/code/dashboard/test_frontend.py` and `test_notebook.py`).

| To change | Edit | Suites |
|---|---|---|
| The idea categories | `CATEGORIES` and `SHORT` in `scripts/autoopt.py`, the list in `references/idea-miner.md`, `CATEGORY_ORDER` and `SHORT` in `scripts/dashboard/static/dashboard.js` | `test_runner`, `test_frontend` |
| The words a shipped document may not use | `WORDS`, `STRICT` and `PATHS` in `scripts/lint_text.py` | `test_lint_text` |
| The optimization PR's sections | `scripts/opt_pr_body.py`, and `references/pr-optimization.md` | `test_ship` |
| A ship route | `AFTER_CHOICES['ship']` in `scripts/autoopt.py`, `SHIP_NEXT` in `scripts/stages.py`, `scripts/ship.py`, and `references/step5-ship.md` | `test_ship`, `test_stages` |
| The dashboard's colors | the tokens on `:root` in `scripts/dashboard/static/dashboard.css` | `test_frontend` |

A changed rule goes in the reference file that states it, in the same
commit, and a new term in `references/glossary.md`.

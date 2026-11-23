# Every script flag

`$AO` is the skill's `scripts/` directory (`SKILL.md`, *Paths in every
command*). What the commands read and write on disk is in
`references/layout.md`; the read-only commands and what each answers are in
`references/reading-a-search.md`. The dashboard's flags are in
`scripts/dashboard/README.md`.

## `autoopt.py` — the search tree

`--id` names an existing iteration; `new` and `brief` take `--new-id` (or `--id`) for the one they create. `--id` defaults to the winner (`best`) in `path`, `final`, `end` and `archive`, to the root in `check`, `profile` and `validate`, and is required where one iteration is acted on (`finish`, `readme`, `promote`, `variant`, `restore`, `checkers`).

```bash
python3 $AO/autoopt.py init <work> --task … --command … --objective … --direction max|min [--unit U] [--gates JSON] [--observers a,b]
                              [--runs 5] [--staged [--confirm-runs 10]] [--seed-base N] [--gpus-per-agent 1] [--timeout S]
                              [--no-gpu | --gpu-command '<allocator> --'] [--floor V] [--workers N] [--build-env VAR=VALUE …]
                              [--subagent-model opus|sonnet|haiku|fable] [--promote auto|orchestrator] [--after JSON]
                              [--validation-command CMD --validation-measures a,b --validation-note TEXT]
                              [--copy PATH …] [--repo REPO [--allow-dirty]] [--tests CMD [--tests-path tests] [--tests-timeout S]] [--force]
python3 $AO/autoopt.py tests-branch <original> --work <work> [--date YYYY-MM-DD]     # step 1: check, clone, branch
python3 $AO/autoopt.py tests-branch --work <work> --squash -m '<message>'
python3 $AO/autoopt.py spec <work> --set FIELD VALUE …
python3 $AO/autoopt.py config <work> [--set KEY=VALUE … --add-observer NAME … --reason '…']
python3 $AO/autoopt.py setup <work> [--ask Q [--recommend A] | --answer A | --force | --done]
python3 $AO/autoopt.py prove <work> [--make NAMES] [--dir DIR] [--compare CMD] [--background]
python3 $AO/autoopt.py prove <work> --id A7 --checker PATH --checker-row FILE --faults FILE [--timeout 240]
python3 $AO/autoopt.py width <work> [--counts 1,2,4] [--reps N] [--write] [--background]
python3 $AO/autoopt.py busy <work> [--seconds 2]
python3 $AO/autoopt.py price <work>                       # after A0: what the later steps that run the base cost
python3 $AO/autoopt.py harness <work> [--accept --reason '…']
python3 $AO/autoopt.py mine <work> [--done] [--force] [--without-profile]
python3 $AO/autoopt.py drop <work> --idea-line N|TEXT --reason '…'
python3 $AO/autoopt.py loop <work> [--force | --done]
python3 $AO/autoopt.py brief <work> --new-id A3 --title '…' --category CACHING --idea '…'|@file --tried none|'…'|@file --gpus '…'
                              [--idea-line N|TEXT] [--parent A0] [--topic T] [--predict D --predict-basis '…'] [--sections tags]
                              [--checks '…'] [--note '…' …] [--validate] [--control] [--after-end] [--replace]
python3 $AO/autoopt.py brief <work> --next [--count N]    # status --brief, new summaries, then a brief for each free slot
python3 $AO/autoopt.py brief <work> --prompt A3           # an existing brief's spawn prompt again
python3 $AO/autoopt.py new <work> --brief A3              # the iteration a brief describes
python3 $AO/autoopt.py new <work> --new-id A3 --title '…' --category CACHING [--parent A0] [--topic T]
                              [--predict D --predict-basis '…'] [--control] [--idea-key KEY] [--after-end]
python3 $AO/autoopt.py finish <work> --id A3 [--ledger FILE] [--checker PATH --checker-row FILE]
python3 $AO/autoopt.py readme <work> --id A3 [--template]
python3 $AO/autoopt.py ledger <work> --by A7 --add FILE
python3 $AO/autoopt.py check <work> [--id A7 | --reference]
python3 $AO/autoopt.py checkers <work> --id A7 [--name NAME … | --touched] [--timeout 1800]
python3 $AO/autoopt.py profile <work> [--id N]
python3 $AO/autoopt.py variant <work> --id A7 NAME [--parent] [--no-build]
python3 $AO/autoopt.py status <work> [--round N] [--json] [--brief|--header [--last N]] [--resume]
python3 $AO/autoopt.py summary <work> --round N
python3 $AO/autoopt.py promote <work> --id A7
python3 $AO/autoopt.py validate <work> [--round N] [--id A7] [--approve] [--dry-run] [--out DIR] [--background]
python3 $AO/autoopt.py watch <work> [--cap 10800] [--every 60]
python3 $AO/autoopt.py bg <work> <log> '<command>'       # a long command of the main session, detached, with its log
python3 $AO/autoopt.py wait LOG [--pid N] [--max 200]
python3 $AO/autoopt.py tokens <work> [--since T] [--subagents-only] [--session ID …] [--exclude-session ID …] [--projects DIR] [--json]
python3 $AO/autoopt.py final <work> [--id best] [--json] [--background]
python3 $AO/autoopt.py end <work> [--reason WORD|TEXT] [--id N] [--background | --reopen]
python3 $AO/autoopt.py path <work> [--id N|best] [--markdown|--json]
python3 $AO/autoopt.py table <work> [--fields a,b|all] [--status S,…] [--category C,…] [--round N] [--parent A5]
                              [--frontier] [--promoted] [--sort [-]FIELD|best] [--limit N] [--tsv|--json]   # read-only
python3 $AO/autoopt.py show <work> [A7|best] [--chars 600] [--json]   # read-only
python3 $AO/autoopt.py pr <work> [--id best] [--dir D] [--branch NAME] [--remote R --head OWNER:BRANCH] [--other-owner] [--post | --edit]
python3 $AO/autoopt.py archive <work> [--id N] [--dest DIR] [--force]
python3 $AO/autoopt.py prune <work> [--keep-recent 20] [--worktrees] [--all] [--outputs-ext .npy,.npz] [--keep-outputs] [--apply]
python3 $AO/autoopt.py restore <work> --id A7 [--branch NAME]
python3 $AO/autoopt.py reset <work> [--keep 0,7] [--new-root 7] [--force] [--apply]
```

`<work>` may be the search's directory or its `autooptimize/` subdirectory. `--help` of each subcommand explains every flag. `new` takes a lock around id allocation, so concurrent calls cannot take the same id. `prune` and `reset` only report without `--apply`, which is given after the user agrees. `brief --gpus` is required unless `gpu` is false or `gpu_command` is set.

## `stages.py` — the stages and the next move

```bash
python3 $AO/stages.py status $WORK        # the six stages: interview, setup, search, final, ship, finish
python3 $AO/stages.py render $WORK        # the same table, written to $WORK/NOTES.md
python3 $AO/stages.py next $WORK [--run]  # the one command or question that is next
```

Each stage is `not started`, `running` or `complete`, read from `state.json`,
`ended.json`, `after.ship`, `ship/` and `finish/`. `next` prints the move and its kind (`run`,
`agent`, `wait`, `stop`: `references/main-session.md`); `--run` starts a `run`
move.

## `ship.py` — step 5

```bash
python3 $AO/ship.py plan <work> [--json]                 # what this search can ship, and the question to ask
python3 $AO/ship.py apply <work> [--id best] [--to DIR] [--yes]
python3 $AO/ship.py branch <work> [--id best] [--name N] [--repo PATH] [--yes]
python3 $AO/ship.py brief <work>                         # the writer agent's prompt
python3 $AO/ship.py status <work>
python3 $AO/ship.py render <work> [--only bugs|tests|optimization]
python3 $AO/ship.py rehearse <work>
python3 $AO/ship.py post <work> all --approved --digest D [--other-owner]
```

`apply` and `branch` preview until `--yes`. `post` refuses any digest but the
last rehearsal's, and inputs changed since (`references/step5-ship.md`).

The documents' own scripts (`references/pr-optimization.md`,
`references/pr-tests.md`, `references/issue-bugs.md`):

```bash
python3 $AO/opt_pr_tables.py <work> [--id best] [--out D] [--tests-format KEY=REGEX …]
python3 $AO/opt_pr_body.py D (--template | --title T --out D/body.md)
python3 $AO/tests_pr_tables.py collection|files|coverage|names …
python3 $AO/tests_pr_body.py D (--template | --title T --out D/body.md) [--config FILE] [--allow-source PATH …]
python3 $AO/tests_pr_push.py D --repo OWNER/REPO [--post | --edit] [--other-owner]
python3 $AO/issue_body.py template|dupes|verify|status|render|renumber|file|link D …
python3 $AO/attribution.py check --kind optimization|tests|issue --title T --body FILE
```

## `run_iteration.py` — one iteration

```bash
python3 $AO/run_iteration.py $WORK/autooptimize/iterations/3
```

| Flag | Meaning |
|---|---|
| `--runs N` | run N seeds instead of `runs_per_iteration`; below it, the iteration is `partial` |
| `--confirm-runs N`, `--stage {screen,confirm,all}` | a search made with `init --staged` only: override `confirm_runs`; stop after the screening runs, go straight to `confirm_runs`, or screen then confirm (the default) |
| `--rerun` | re-run seeds already measured on the current commit |
| `--no-promote` | never promote, whatever `promote` says, and run no external validation: a re-measurement of a settled iteration |
| `--full-tests` | run the whole unit-test suite instead of the subset; reuses finished runs, so on a settled iteration it runs only the suite |
| `--dry-run` | print the runs that would happen, and stop |
| `--background` | start the same call detached, writing `<iteration>/probe/runner.log` (ending `exit <code>`), and return: watch it with `autoopt.py wait` |

It refuses while `spec.md` has a TODO line (`--dry-run` excepted), and never on a busy machine: before A0's runs on a CPU benchmark it names each process outside the search above about 100% CPU over two seconds in its report (`busy at start:`) and `result.json` (`busy_at_start`). Exit code 2
means `status == "error"`; 0 means the iteration produced a verdict, including
`gate-failed`. Each run is in its own process group, so Ctrl-C or a timeout
kills the whole benchmark, not only its shell. A seed already measured on the
current commit is skipped, so an interrupted iteration resumes where it
stopped; one measured on an older commit runs again. Run on a settled
iteration after a gate changed, it re-judges the stored runs under the new
gate. When `notes.md` exists, it re-assembles `README.md` at the end.

## `check_seeding.py` — does the seed control what it should

```bash
python3 $AO/check_seeding.py $WORK/autooptimize/iterations/0
```

`--seed N` is the seed to repeat (default: the search's first), `--repeats N` the runs on it (2), `--others N` the runs on further seeds (2); `--scan-only` reads the source for unseeded randomness and stops; `--keep` keeps the `runs/seedcheck-*` directories and the compiler cache.

Exit codes: 0 OK, 1 WARNING (the same seed moved the objective by half or more
of what changing it did), 2 FAIL (different seeds gave identical answers), 3
refused or could not run. Its runs, in `runs/seedcheck-*`, leave the
iteration's own results untouched, and share one compiler cache, `{cache}`, at
`<iteration>/cache/seedcheck`, emptied before the check and removed after it
unless `--keep`. What to do about each verdict:
`references/step2-baseline.md`, *Seeding*.

## `paired.py` — paired separate-process timing, with a stopping rule

```bash
python3 $AO/paired.py --a DIR_A --b DIR_B --cmd "python3 probe.py --src {src}" [--env K=V …] [--min-pairs 8] [--max-pairs 24] [--alpha 0.005]
                       [--direction min|max] [--regex PATTERN] [--cap-hours H] [--run-timeout S] [--log FILE] [--json]
```

`--regex` is a pattern whose first group is the seconds on the command's last line, for output whose last number is not the time. `--cap-hours` exits 3 before the first pair when `--min-pairs` would take longer; a recorded run past `--run-timeout` seconds (default: ten times that side's unrecorded run, plus 60) is killed, and that exits 4. `--cmd` runs once per timing in a fresh shell with `{src}` and `{side}` (`a`/`b`) substituted; it prints the seconds as the last number on its last line and exits non-zero when its output is wrong. Each side gets `<dir>/.paired-cache` as its compiler cache. The stop rule is the exact two-sided sign test.

`verdict` is `B better`, `B worse`, or `no difference resolved` when 24 pairs
end with p ≥ alpha. It exits non-zero, having decided nothing, if any run
fails. Why the stopping rule is safe: `references/measuring.md`, *Paired
separate processes: `paired.py`*.

## `cold.py` — cold first-call time on empty caches

```bash
python3 $AO/cold.py --side NAME=DIR [--side NAME=DIR ...] --cmd "python3 first_call.py --src {src}" --scratch DIR [--reps 2] [--env K=V …] [--regex PATTERN] [--json]
```

Each run gets a new empty directory under `--scratch` as `NUMBA_CACHE_DIR`
(and the Triton and Inductor caches), created with `mkdtemp` and never
deleted. Sides alternate order each repetition. In `--cmd`, `{src}` is the
side's `DIR` and `{side}` its `NAME`; the command prints the first call's
seconds as the last number on its last line. Keep `--scratch` inside
the iteration directory.

## `preflight.py` and `lint_text.py` — checks without measuring

```bash
python3 $AO/preflight.py $WORK [--id A12]         # the iteration's own change, from its parent; default best
python3 $AO/preflight.py $WORK --id best --path   # the whole path, from the commit the search copied
python3 $AO/lint_text.py body FILE [--labels A0,A1]
python3 $AO/lint_text.py title STRING [--labels …]
python3 $AO/lint_text.py commits REPO REF BASE [--labels …]
python3 $AO/lint_text.py diff-comments REPO REF BASE [--labels …]
```

`preflight.py` exits 0 with no problem, 1 with problems (one line each,
`check: text`), 2 when refused (not a repository search, no such iteration).
`--path` also writes `$WORK/autooptimize/preflight.json` (`{winner, problems}`).
`lint_text.py` exits 1 listing every hit with its line. The checks and the
vocabulary: `references/promotion.md`, *The preflight*.

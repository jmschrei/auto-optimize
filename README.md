# auto-optimize-code

A Claude Code skill that turns "make this better" into a search that runs
itself: a benchmark that emits numbers, a baseline, then one subagent at a
time, each trying one idea in its own git worktree and reporting a median
over seeds. It covers the interview that pins down the objective and the
constraints, a setup subagent that builds the benchmark and measures the
baseline, a keep rule against a noise band, promotion of each kept change, a
live dashboard, the paired timing of the winner against the baseline, and the
final report.

This file is for people. The skill is `skills/auto-optimize-code/`: Claude
reads its `SKILL.md`, which routes to the files in its `references/`.

## Install

```bash
git clone https://github.com/jmschrei/auto-optimize
ln -s "$PWD/auto-optimize/skills/auto-optimize-code" ~/.claude/skills/auto-optimize-code
```

That installs it for every project; link it into `.claude/skills/` inside a
project to install it there alone. Claude Code finds it by its
`SKILL.md` and starts it when a request matches its description, such as
"optimize this function, but the output must not change".

Requirements are in the skill's `SKILL.md`, *Requirements*: Linux, Python 3.8 or later and
git for the core; JupyterLab, IPython and plotly for the dashboard.

## Test

```bash
python3 tests/code/run_tests.py                     # every script suite, one line each
python3 tests/code/dashboard/test_frontend.py       # the dashboard, in headless Chromium (needs playwright)
```

`tests/code/dashboard/README.md` describes the dashboard's suites.

## Credit

Developed at the [Programmable Genomics Laboratory](https://programmable-genomics.github.io) @ UMass Chan Medical School.

## License

MIT (`LICENSE`).

# auto-optimize

A Claude Code skill that turns "make this better" into a search that runs
itself: a benchmark that emits numbers, a baseline, then one subagent at a
a time, each trying one idea in its own git worktree and reporting a median
over seeds. It covers the interview that pins down the objective and the
constraints, a setup subagent that builds the benchmark and measures the
baseline, a keep rule against a noise band, promotion of each kept change, a
live dashboard, the paired timing of the winner against the baseline, and the
final report.

This file is for people. Claude reads `SKILL.md`, which routes to the files in
`references/`.

## Install

```bash
git clone https://github.com/jmschrei/auto-optimize ~/.claude/skills/auto-optimize
```

That installs it for every project; clone into `.claude/skills/auto-optimize/`
inside a project to install it there alone. Claude Code finds it by its
`SKILL.md` and starts it when a request matches its description, such as
"optimize this function, but the output must not change".

Requirements are in `SKILL.md`, *Requirements*: Linux, Python 3.8 or later and
git for the core; JupyterLab, IPython and plotly for the dashboard.

## Test

```bash
python3 scripts/test_runner.py                  # the scripts' decisions
python3 scripts/dashboard/test_frontend.py      # the dashboard, in headless Chromium
```

`references/layout.md`, *Testing the scripts*, lists every suite.

## License

MIT (`LICENSE`).

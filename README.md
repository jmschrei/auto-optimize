# auto-optimize

`auto-optimize-code` is a Claude Code skill that turns "make this faster"
into a search that runs itself. It interviews you about what to improve and
what must not change, builds a benchmark and measures a baseline, then hands
one idea at a time to a subagent, each in its own git worktree, and keeps a
change only when its median over several seeds beats its parent by more than
the run-to-run noise. A live dashboard follows the search, and at the end the
winner is timed against the baseline in paired runs and written up.

It optimizes code: wall time, memory, output size or any other number a
script can emit, with the outputs held fixed by gates you choose ("faster,
but the output must stay byte-identical", "smaller, but within 1e-5"). It
works on a function, a script, a command-line tool, or a copy of a whole git
repository, whose original it never writes to.

Developed at the [Programmable Genomics Laboratory](https://programmable-genomics.github.io) @ UMass Chan Medical School.

## Requirements

- Claude Code. The skill spawns subagents and asks you questions, so it needs
  Claude Code's Agent and AskUserQuestion tools.
- Linux, Python 3.9 or later as `python3`, git and tar. The skill's scripts use
  only the standard library.
- For the dashboard in a notebook: JupyterLab, IPython and plotly; psutil for
  the host and process panels.
- For a benchmark that uses GPUs: `CUDA_VISIBLE_DEVICES` set by the machine's
  GPU allocator, which the interview asks about.

`auto-optimize doctor` checks these once the installer is available (below).

## Install

The skill is one folder, `skills/auto-optimize-code/`. Each method below puts
that folder where Claude Code looks for skills; use one of them.

**As a Claude Code plugin.** In Claude Code:

```
/plugin install auto-optimize --marketplace jmschrei/auto-optimize
```

or, in two steps, `/plugin marketplace add jmschrei/auto-optimize` and then
`/plugin install auto-optimize@auto-optimize`. From a shell, `claude plugin
marketplace add jmschrei/auto-optimize` and then `claude plugin install
auto-optimize@auto-optimize`. The skill shows as
`auto-optimize:auto-optimize-code`. Update it with `claude plugin update
auto-optimize@auto-optimize`.

**With the installer, no clone.** With [uv](https://docs.astral.sh/uv/):

```bash
uvx auto-optimize install              # into ~/.claude/skills/, for every project
uvx auto-optimize install --project    # into ./.claude/skills/, for this project only
```

or `pipx run auto-optimize install`, or `pip install auto-optimize` and then
`auto-optimize install`. Run `install` again with a newer version to update.
The installer writes nothing until you run `install`, and replaces only a
copy it made itself; `--force` replaces anything else at that path.

| Command | Does |
|---|---|
| `auto-optimize install [--project [DIR] \| -d DIR] [--symlink] [-f]` | copy (or link) the skill into a skills folder |
| `auto-optimize uninstall [--project [DIR] \| -d DIR] [-f]` | remove a copy the installer made |
| `auto-optimize list` | every copy Claude Code would load from here: user, project and plugin |
| `auto-optimize path` | the installed skill's folder |
| `auto-optimize doctor` | check Linux, `python3`, git, tar, the dashboard's modules and the installed copies |

When `CLAUDE_CONFIG_DIR` is set, the installer uses its `skills/` folder in
place of `~/.claude/skills/`.

**From a clone, linked.** Edits in the clone and `git pull` reach Claude Code
at once:

```bash
git clone https://github.com/jmschrei/auto-optimize
cd auto-optimize
uv run auto-optimize install --symlink      # or: ln -s "$PWD/skills/auto-optimize-code" ~/.claude/skills/
```

**By hand.** Copy `skills/auto-optimize-code/` into any skills folder. It is
self-contained and carries its own `LICENSE`.

Install it one way only: Claude Code loads every copy it finds, and two copies
of one skill compete for the same requests. `auto-optimize list` shows them.

## A first search

Ask in Claude Code, in plain words:

> Optimize `count_kmers` in `kmers.py` for speed. The output must stay
> identical.

The skill starts with an interview: what number to improve and in which
direction, what must not change and how strictly (bitwise, a tolerance, a
correlation), what to record without optimizing, how many runs per
iteration, the GPUs, which model runs the iteration subagents, the budget,
and where the search lives. The default is
`~/auto-optimize/<YYYY-MM-DD>-<name>/`, which needs room for a copy of the
code per iteration. Then, without you:

1. a setup subagent writes the benchmark, checks that its seed controls what
   it should, proves that each gate catches a planted fault, measures the
   noise and runs the baseline, A0;
2. an idea miner reads A0's profile and lists ideas by category;
3. iteration subagents each take one idea, implement it in a fresh worktree
   forked from the root (the last change kept), measure it and write a
   README; a change that passes the keep rule becomes the next root;
4. the search ends on your word or at the budget you set: the winner is timed
   against A0 in paired runs and summarized in `ended.md`;
5. the winner is copied out with a final report, and the disk is reclaimed
   once you agree.

`examples/word-count/` is a slow, correct word counter to try this on.

**What it costs.** Every iteration is a subagent session, and so is each
setup, mining and orchestration phase, so a search of a few dozen iterations
runs that many agent sessions. `autoopt.py tokens <work>` adds up what a
search's agents spent, from Claude Code's transcripts.

## Watching it

The skill starts the dashboard with the first iteration and tells you where
it is: `<work>/dashboard.ipynb`, a live page in JupyterLab with the objective
by iteration, the frontier, the search tree, every run, the GPUs, the running
processes, and each iteration's code change against its parent. At the end it
writes a self-contained snapshot, `<work>/autooptimize/final/dashboard.html`,
which opens in any browser.

## Reading the results

Ask Claude ("what did the search find?", "what did A7 try?"); the skill
answers from read-only commands. Or run them yourself:

```bash
AO="$(auto-optimize path)/scripts"     # or the skills folder you installed into
python3 $AO/autoopt.py status <work> --brief                 # where it stands
python3 $AO/autoopt.py table <work> --status ok --sort best  # every passing iteration, best first
python3 $AO/autoopt.py show <work> A7                        # one iteration: numbers, verdict, change, notes
python3 $AO/autoopt.py path <work> --markdown                # the kept changes from A0 to the winner
```

`<work>/autooptimize/ended.md` summarizes an ended search, and
`<work>/finish/final-report.md` is the final report. `table --json` and
`show --json` print raw values for a program; the record files are described
in `skills/auto-optimize-code/references/layout.md`.

## How it works

`skills/auto-optimize-code/SKILL.md` is what Claude reads. It defines the
words (a **run** is one seed, an **iteration** one idea measured over several
runs, a **round** the iterations forked from one root), the rules that hold
throughout, and routes each step to a file in `references/`. The scripts in
`scripts/` keep every number: `run_iteration.py` measures an iteration and
judges it against its gates and the noise band, and `autoopt.py` keeps the
search tree, the briefs, the promotions and the records. Only a median the
runner computed counts as a result; no agent types a number in.

## When it is the wrong tool

- One measurement, not a search: timing a function once needs a benchmark
  alone.
- No measurable objective: if no script can emit it as a number, that comes
  first.
- Improving a model's accuracy or training recipe: this skill optimizes code,
  with its outputs gated.

## Repository layout

```
.claude-plugin/          the plugin manifest and its one-plugin marketplace
skills/
  auto-optimize-code/    the skill: SKILL.md, references/, scripts/, LICENSE, VERSION
src/auto_optimize/       the installer (standard library only)
tests/
  code/                  the skill's script suites; dashboard/ holds the browser tests
  package/               the installer and the shipped files (pytest)
examples/word-count/     a slow program to try a first search on
```

## Development

```bash
python3 tests/code/run_tests.py                  # every script suite, one line each (a few minutes)
python3 tests/code/run_tests.py test_reading     # one suite
uv run pytest                                    # the installer and the shipped files
python3 tests/code/dashboard/test_frontend.py    # the dashboard in headless Chromium (needs playwright)
python3 tests/code/dashboard/test_notebook.py    # the dashboard mounted in JupyterLab
```

`tests/code/dashboard/README.md` describes the dashboard's suites. The
version is `__version__` in `src/auto_optimize/__init__.py`; the skill's
`VERSION` file and both plugin manifests carry the same number, which
`tests/package/test_skill_files.py` checks.

## License

MIT (`LICENSE`).

# Changelog

## [1.0.0]

The first release: `auto-optimize-code`, a Claude Code skill that searches
for a faster, leaner or smaller implementation of code by running many small
measured experiments and keeping what survives; a Claude Code plugin that
carries it; and `auto-optimize`, a command that installs it. It runs on
Linux with Python 3.9 or later, and the skill's scripts use only the
standard library.

### Installation

- The skill is one self-contained folder, `skills/auto-optimize-code/`, with
  its own `LICENSE` and `VERSION`.
- The repository is a Claude Code plugin and its own marketplace:
  `/plugin marketplace add jmschrei/auto-optimize`, then
  `/plugin install auto-optimize@auto-optimize`.
- `uvx auto-optimize install` installs the skill without a clone: into
  `~/.claude/skills/` (`$CLAUDE_CONFIG_DIR/skills/` when set), a project's
  `.claude/skills/` with `--project`, or any folder with `-d`, copied or, with
  `--symlink`, linked. It replaces only a copy it made itself unless given
  `--force`, and a failed install leaves the previous copy in place.
- `auto-optimize uninstall` removes a copy the installer made; `list` shows
  every copy Claude Code would load (user, project and plugin) and warns when
  the skill is installed twice; `path` prints the installed folder; `doctor`
  checks Linux, `python3`, git, tar and the dashboard's optional modules in
  the `python3` the skill runs.

### The search

- **Interview (step 0).** The number to improve and its direction; what must
  not change and how strictly (bitwise, a tolerance or a correlation); what
  to record without optimizing; runs per iteration; GPUs and the allocator
  command; the iteration subagents' model; the budget; and where the search
  lives. Every answer, and each default taken, goes into `spec.md`, which
  every subagent reads.
- **Repository mode.** A copy of a git repository is searched, optionally
  after step 1 writes a new unit-test suite for it and records the bugs that
  suite finds. The original is never written to during the search.
- **Setup (step 2).** A setup subagent writes the benchmark, checks that the
  seed controls what it should, proves that each gate catches a planted
  fault, measures whether runs at once disturb each other, and runs the
  baseline, A0.
- **Rounds (step 3).** An idea miner reads the profile and writes ideas by
  category. An orchestrator subagent briefs one iteration subagent per idea,
  each in its own git worktree forked from the current root, and the miner
  refills the ideas as they run out.
- **Measurement and the keep rule.** `run_iteration.py` measures each
  iteration over several seeded runs and judges it against its gates. A
  change is kept when its median beats its parent's by more than the noise
  band (`keep.band_k`, `keep.min_gain_pct`), or by a rule written in the
  user's words (`promote=orchestrator`), and a kept change becomes the root
  the next ideas build on. The package's own tests can gate every iteration
  (`tests.command`), and an external validation can run once a round on
  inputs the search never measures. Observers record other measures, such
  as peak memory, the first call or tiny and very large inputs, without
  deciding anything.
- **Budgets.** A number of iterations (`after.budget_iterations`), idea-miner
  stages (`after.budget_stages`) or wall-clock hours (`after.budget_hours`).
- **The end (step 4).** The winner is timed against A0 in alternating,
  separate processes, and `ended.md` gives the medians from A0 to the
  winner, each gate's status and the paired verdict. A search can be
  reopened with `end --reopen`.
- **Shipping (step 5).** The skill asks how to ship the winner and writes
  nothing before the user approves a preview: the winner's files written
  back where they were copied from (`ship.py apply`), a branch in the
  original repository with one commit per kept change (`ship.py branch`), or
  GitHub documents (the optimization PR, the unit-test PR and the bug
  report), drafted from the search's own measurements, rendered together by
  `ship.py rehearse` and posted by `ship.py post` only as the batch the user
  reviewed. A fork is used for a repository the user cannot push to
  (`after.pr_remote`, `after.pr_head_owner`).
- **Finish (step 6).** The winner is copied out with a final report, and the
  disk is reclaimed on the user's word.
- **Resuming.** Everything a search needs is on disk: `NOTES.md` and
  `stages.py` give the six stages and the next move, and a new session
  resumes from there.
- **Reading a search.** Read-only commands answer questions about it:
  `autoopt.py status`; `table`, every iteration filtered, sorted and cut as
  text, TSV or JSON; `show`, one iteration on one screen; `path`, the kept
  changes from A0 to the winner; and `tokens`, what the search's agents
  spent.
- **The machine.** Busy checks read the CPUs' idle share from `/proc/stat`
  (`busy_percent` sets when another process counts as busy), and GPU runs
  start under the configured allocator (`gpu_command`).
- **Records.** `config.json`, `state.json`, `result.json` and `ended.json`
  carry `schema_version` 1, and `config.json` records the skill that made the
  search.

### Dashboard

- A live dashboard in a JupyterLab notebook, `<work>/dashboard.ipynb`: the
  objective by iteration with the frontier, the search tree, every run, the
  GPUs and running processes, and each iteration's code change against its
  parent and its README, opened from the charts and the tree.
- `final/dashboard.html`, a self-contained snapshot written when the search
  ends, which opens in any browser (`autoopt_monitor.py --export`).

### Documentation and examples

- The README walks through a real search on `examples/one-hot/`, a one-hot
  encoder made 1,070 times faster, with screenshots in `docs/walkthrough/`
  and its dashboard online.
- `examples/fasta-stats/` (a command-line tool, output byte-identical),
  `examples/kmer-memory/` (peak memory as the objective, time observed) and
  `examples/smooth-coverage/` (a 1e-9 tolerance gate) are shorter searches,
  each with its prompt, its dashboard at the end and what it found.
- `references/extending.md` lists every setting, environment variable and
  code hook; `references/reading-a-search.md` maps questions about a search
  to the commands that answer them; `references/glossary.md` defines the
  terms.

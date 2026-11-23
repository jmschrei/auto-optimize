# Changelog

## [0.1.0]

The first public release: the `auto-optimize-code` skill, a Claude Code
plugin that carries it, and `auto-optimize` on PyPI, a command that installs
it.

### Packaging

- The skill lives in `skills/auto-optimize-code/`, a self-contained folder
  with its own `LICENSE` and `VERSION`. Every install method places that
  folder and nothing else; the repository root held the skill before, which a
  Claude Code plugin cannot load. A clone of this repository made inside a
  skills folder, as the earlier README said to install it, stops loading
  once it pulls this change, since `SKILL.md` is no longer at its top:
  remove that clone and install with one of the methods in the README.
- The repository is a Claude Code plugin and its own marketplace
  (`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`):
  `/plugin marketplace add jmschrei/auto-optimize`, then
  `/plugin install auto-optimize@auto-optimize`.
- `auto-optimize` on PyPI installs the skill without a clone
  (`uvx auto-optimize install`). `install` copies it into
  `~/.claude/skills/` (`$CLAUDE_CONFIG_DIR/skills/` when set), a project's
  `.claude/skills/` with `--project`, or any folder with `-d`; `--symlink`
  links instead of copying. A copy carries an `.install.json` marker, so a
  later `install` replaces it without `--force`, while a folder the installer
  did not create needs `--force`. The new copy is built beside the old one
  and renamed into place, so a failed install leaves the previous one.
  `uninstall` removes only what it installed; `list` shows every copy Claude
  Code would load (user, project, plugin) and warns when one skill is
  installed twice; `path` prints the installed folder; `doctor` checks Linux,
  `python3` 3.9 or later on PATH, git, tar and the dashboard's optional
  modules in that `python3`, which is the one the skill runs.
- The script suites, the dashboard's browser tests and their fixtures move
  out of the skill to `tests/code/`, so they are not installed. The installer
  and the shipped files are tested in `tests/package/` with pytest.
- The Python floor is 3.9.

### Documentation

- The README walks through a real search on `examples/one-hot/`, a for-loop
  one-hot encoder made about 1,070 times faster: the prompt, the interview's
  answers, setup, the dashboard, an iteration's code change opened from the
  chart, an iteration's README from the search tree, the end and what the
  search cost. The screenshots are in `docs/walkthrough/`.
- `examples/one-hot/` replaces `examples/word-count/` as the example to try.
- The README starts a first search with the commands that run the example,
  says what to set in Claude Code before leaving a search to run (tool
  permissions, `askUserQuestionTimeout`) and what the subagents run on the
  machine, explains the ways step 5 brings the winner back into your code,
  and lists how to resume, stop or reopen a search.

### Claude Code skill

- **Step 5, shipping**, between ending the search (step 4) and finishing it
  (step 6, which was step 5). `end` now says the search is over and sends the
  main session to ask the user how to ship; nothing is applied, made or
  posted before that answer, recorded as `after.ship`. The public skill had
  no way to bring a winner back: the kept iterations' commit messages and
  step 1's suite and bug folders were left for "whatever publishes", which it
  did not include.
  - `ship.py apply` (plain mode) writes the winner's version of each copied
    file back where it came from, after a preview. It refuses a file edited
    since the copy and leaves out files the search wrote itself. `init
    --copy` now records each source (`config.json` `copied`); a search made
    before needs `--to DIR`.
  - `ship.py branch` (repository mode) makes a branch in the original with
    one commit per kept change, each with its `## Commit message`.
  - On GitHub, the user checks which documents to open: the optimization PR,
    the unit-test PR (when step 1 wrote a suite) and the bug report (when
    step 1 found bugs). One writer subagent drafts them (`ship.py brief`),
    `ship.py rehearse` renders and previews all of them, and `ship.py post`
    posts them only with the digest of the batch the user reviewed, filling
    each document's number into the next.
  - The documents are ported from an earlier, private version of this skill
    and rewritten to hold only what the search recorded. They carry no
    opening that names the tool or the lab and no title prefix, and
    `lint_text.py` now refuses both in published text. Each ends with the
    Claude Code line.
  - The optimization PR (`opt_pr_tables.py`, `opt_pr_body.py`, `autoopt.py
    pr`) reports the medians and ranges of the objective and every
    observer, the paired timing, the first call, each output check's worst
    value, the separate comparison and the unit tests.
  - The unit-test PR (`tests_pr_tables.py`, `tests_pr_body.py`,
    `tests_pr_push.py`) keeps the coverage before and after, and drops the
    option logging and the local configuration runs.
  - The bug report (`issue_body.py`) reruns each reproduction on the
    original before filing.
  - With a unit-test suite, the optimization PR and the local branch hold only
    the optimization: each kept change is applied to the original's commit, a
    change to a suite file is refused, and the commits must equal the
    measured change outside the suite's files.
  - Forks: `after.pr_remote` and `after.pr_head_owner`.
  - `tests-branch` records the original's path, branch, commit and remote in
    `tests/branch.json`; with a suite, `config.json` describes the clone.
  - Remote URLs are recorded as configured, before `insteadOf` rewrites.
  - `ended.json` records the machine the search ran on (`machine`).
  - `stages.py` reports six stages, `ship` between `final` and `finish`.
- `autoopt.py table` prints one row per iteration, in-flight ones included,
  filtered by status, category, round, parent, frontier or promotion, sorted
  and cut, as text, TSV or JSON with the columns asked for. `autoopt.py show`
  puts one iteration on one screen: its numbers against its parent and A0,
  the keep verdict, gates, observers, the size of its code change with the
  git command for the whole diff, its notes, and the paths to read further.
  Both only read. An agent asked about a search it did not run had only
  `status --json`, which prints every `result.json` whole, or
  `references/resume.md`, which starts the dashboard publisher.
- `references/reading-a-search.md` maps each question about a search to the
  command or file that answers it, and `SKILL.md` routes such a question
  there first. Every script flag moves from `references/layout.md` to
  `references/cli.md`.
- `config.json` records the skill that made the search, `skill: {name,
  version, commit}`, and every script refuses a search another skill made
  (`this search was made by the … skill`). A search with no name predates the
  field and is read as before.
- `config.json`, `state.json`, `result.json` and `ended.json` carry
  `schema_version` (1), for tools that read them.
- A search's budget can be a number of idea-miner stages
  (`after.budget_stages`), in place of the number of iterations the
  interview offered, which no script enforced: `stages.py next` called for
  another phase after such a budget was spent. Once that many stages have
  closed, `mine` refuses a further one, `status` says so, and the search
  stops for `budget` when every idea from those stages is tried or dropped.
  A budget in wall-clock hours (`after.budget_hours`) is unchanged.
- `config --set command=…` works until the baseline has run. It was refused
  outright, so a command written without `{iteration}` at step 0 left the
  setup subagent to work around it.
- Step 2 names the profiling check `checks/profile_call.py`. The name it gave,
  `profile.py`, shadows the standard library's `profile` module, which
  `cProfile` imports, so the check failed on its first run.
- The dashboard opens an iteration's diff against its parent when its point
  on the objective chart is clicked.

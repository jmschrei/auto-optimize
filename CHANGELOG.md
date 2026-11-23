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

- **Security and privacy.**
  - The static dashboard (`final/dashboard.html`, `--export`) leaves out the
    hostname, the GPUs' serial numbers and GPU lease holders' names and
    notes, and writes the home directory as `~`. It included all of them, so
    sharing the file shared the machine's name and what other users of it
    were running.
  - The publishers' shared directory (`/dev/shm/dashboard-monitor-<uid>`)
    has a predictable name. One made first by another user, or a symlink in
    its place, was used as it was, so that user could redirect a publisher's
    writes onto any file the searching user can write. Such a directory is
    now passed over for one under `~/.cache`.
  - The mark that says a GPU allocator started the script is made in a
    private temporary directory, not at a predictable name in `/tmp`.
  - CI runs with read-only repository permissions.
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
    step 1 found bugs). One subagent, the document writer, drafts them (`ship.py brief`),
    `ship.py rehearse` renders and previews all of them, and `ship.py post`
    posts them only with the digest of the batch the user reviewed, filling
    each document's number into the next.
  - The documents are ported from an earlier, private version of this skill
    and rewritten to hold only what the search recorded. They carry no
    opening that names the tool or the lab and no title prefix, and
    `lint_text.py` now refuses both in posted text. Nor do they say how
    they were written: no line names the tool, the model or the lab.
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
  - The same questions ask whether the branches shipping makes or pushes
    start with `auto-optimize/` (`after.branch_prefix`): the optimization
    PR's, the unit-test PR's (pushed as `unit-tests-<date>` without it) and a
    local branch. `ship.py branch` and `rehearse` refuse until it is
    answered.
  - `tests-branch` records the original's path, branch, commit and remote in
    `tests/branch.json`; with a suite, `config.json` describes the clone.
  - Remote URLs are recorded as configured, before `insteadOf` rewrites.
  - `ended.json` records the machine the search ran on (`machine`).
  - `stages.py` reports six stages, `ship` between `final` and `finish`.
  - `stages.py next` names the move at every point of step 5: the document
    writer's brief, rehearsing, showing the user the batch and posting with
    its digest, or running a stopped post again. It started `autoopt.py
    archive` without `python3` under `--run`, which failed, and called
    step 6 complete as soon as `archive` wrote the report's skeleton; the
    finish stage now stays `running` while a `<<FILL` mark is left.
  - An answer the search cannot ship (a route its mode lacks, a document
    with nothing behind it) reads as no answer, and `next` asks again
    rather than naming a command that refuses. `end --reopen` sets
    `after.ship` back to `ask`.
  - `apply --yes` records files that already hold the change, which left
    the stage open for good, and applies an `--after-end` winner over an
    earlier apply; the ship stage reopens when the winner changes after an
    apply or a branch.
  - The rehearsal's digest also covers the winner, `after.branch_prefix`,
    `after.pr_remote` and `after.pr_head_owner`. `rehearse` and `post` take
    `--branch optimization=NAME` or `tests=NAME` for a PR branch whose name
    is taken, and a post that stopped after its push runs again cleanly.
  - `tests_pr_push.py` finds `after.pr_remote` given as a remote of the
    original, as `autoopt.py pr` does.
  - `archive` fills the final report's *Where things are* from step 5's
    records, and every step-5 command refreshes `NOTES.md`.
  - A replacement document writer keeps and completes the inputs already
    written.
- The glossary defines every term the shipping step added (main session,
  the original, step-1 suite, document writer, hold, `after`, target
  branch, kept change, the six stages) and corrects three that disagreed
  with the scripts: "in flight" includes a finishing iteration, which is
  settled; the winner is an iteration, not a path; a winning iteration
  changes the code. The step-5 subagent is "the document writer" everywhere,
  so "writer" alone stays the dashboard's publisher, and shipping applies,
  branches or posts; it no longer "publishes". The dashboard's stage strip
  uses `stages.py`'s words (complete, running, not started).
- `tests/package/test_skill_files.py` checks that every cited section,
  `references/x.md`, *Heading*, in the skill's Markdown and in the scripts'
  strings names a heading of that file. Nine citations named none.
- `references/extending.md` lists every setting, environment variable and
  code hook, and where to edit the code when none fits; the README gains a
  short version and a map of the search directory.
- The default keep rule takes two settings, `keep.band_k` (noise bands to
  beat, default 1) and `keep.min_gain_pct` (least gain, default 0), so "keep
  only a 2% gain" no longer needs `promote: orchestrator`. The defaults give
  the same verdicts as before.
- The machine checks read a short sample of the CPUs' idle share from
  `/proc/stat` in place of the load average, which on a shared host counts
  threads a container's quota holds back: `status`'s `machine:` line, each
  run's `idle` in the report and `result.json`, `width`, and the dashboard's
  host. `busy_percent` (default 110, percent of one core) sets when another
  process counts as busy. Without `/proc` the busy and thread checks stop
  with `needs /proc (Linux)`; they read a quiet machine before.
- Past 15 earlier iterations on the same code, `brief`'s *Already tried*
  lists the root path, the latest 15 on the idea's function and the latest
  15 overall, and counts the rest by fate; at 500 iterations the brief went
  from 57 KB to 5 KB. The files each change touched are cached in
  `autooptimize/touched.json` (one `git diff-tree` call, not one `git diff`
  per iteration), and each record is parsed once per process: `brief --next`
  on 500 iterations went from 0.84 s to 0.37 s. `wait` reads only a log's
  tail. `tokens` reads only the search's own transcripts (`--all-projects`
  for every one), from `config.json`'s new `origin`.
- The README opens with a banner (`docs/banner/`, drawn by
  `make_banner.py`) and links to the walkthrough's dashboard as a web page.
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
- A search's budget can be a number of iterations
  (`after.budget_iterations`; A0, same-code controls and iterations made
  after the end do not count), a number of idea-miner stages
  (`after.budget_stages`) or wall-clock hours (`after.budget_hours`). The
  interview offered a number of iterations before, which no script
  enforced: `stages.py next` called for another phase after such a budget
  was spent. Now `brief --next` briefs nothing once the iterations are used
  or the hours have passed, so the orchestrator hands back at the budget
  rather than at the end of its phase; `mine` refuses once the iterations
  are used or the stages have closed; `status` shows what is used; and the
  search stops for `budget`.
- `config --set command=…` works until the baseline has run. It was refused
  outright, so a command written without `{iteration}` at step 0 left the
  setup subagent to work around it.
- Step 2 names the profiling check `checks/profile_call.py`. The name it gave,
  `profile.py`, shadows the standard library's `profile` module, which
  `cProfile` imports, so the check failed on its first run.
- The dashboard opens an iteration's diff against its parent when its point
  on the objective chart is clicked.
- A search that kept nothing ends: `end` refused when the winner was A0, so
  such a search could not reach steps 5 and 6, and under `after.stop=auto`
  `stages.py next --run` started the refused `end` again and again. It now
  ends on A0 with no paired timing, `ended.md` says no change was kept, and
  the ship stage is complete with nothing to ship. `end --id 0` is still
  refused when a change was kept. `stages.py next` stops and shows a failed
  `end` (its log's last lines) rather than starting it again.
- An iteration made with `--after-end` is never promoted by its runner, as
  `references/step4-final.md` said; it becomes the winner only by
  `promote --id`.
- `setup --done` requires `gpu_command` in every GPU search, as `SKILL.md`
  said; only repository mode required it, and `end` then refused for want
  of a GPU lease.
- `autoopt.py loop --stop` asks the open orchestrator phase to stop: its
  next `brief --next` briefs nothing and tells it to hand back. A session
  without SendMessage had no way to stop a running phase.
- `autoopt.py busy` honors `AUTOOPT_SKIP_QUIET_CHECK=1`, which its own
  refusal on a machine without `/proc` named.
- Step 5's safety checks: `ship.py apply --yes` and `branch --yes` refuse
  until the user's choice is recorded (`after.ship`). `apply --by-hand`
  records a file the user merged the change into by hand, which `apply`
  refused for good before, leaving the stage open. `branch --name` needs no
  answer to the prefix question, so the README's command works. Once a PR is
  open, `autoopt.py pr` previews the title and body `--edit` would put on it,
  and `--edit` refuses when the winner changed after the PR was opened. The
  rehearsal's digest covers the unit-test branch's tip commit, so a commit
  made after the review is never pushed unreviewed, and the optimization
  PR's body reads the chosen documents from the live config, so a document
  the user dropped is not cited.
- `config --set after.ship=none`, the command `ship.py plan` prints for "not
  now", was refused: the setter read `none` as null for every `after` key.
  `none` is now the choice where a key offers it, and still unsets the rest.
- `ended.md` names the paired timing as seconds per call when the objective
  is not a time, where it printed a bare number under a memory result.
- The dashboard's run table and iteration log give the smallest value of a
  column four significant figures: after a 600-fold speedup the root's
  0.002741 s printed as 0.003 beside the baseline's 1.671. The summary tiles
  write the unit in parentheses (`seconds (s)`, not `seconds s`), and an
  iteration axis spanning 15 or fewer iterations ticks at whole numbers.
- `status` says the watch is the orchestrator's to start, since the main
  session starts none; `ship.py plan` rounds the medians it quotes.
- `docs/walkthrough/capture_dashboard.py` takes `--redact OLD=NEW` for a
  path to write differently in the pictures.
- The final report's *Result* keeps a `<<FILL` mark for the spreads and the
  paired verdict, which `references/step6-finish.md` asks for and `archive`
  did not leave.
- The installer's `doctor` checks the `python3` Claude Code runs, passing
  over the environment `uvx`, `pipx` or `uv run` put first on `PATH`, where
  it reported the tool's own interpreter and suggested installing the
  dashboard's modules into it. `list` and `doctor` show only this package's
  skills, not another author's that share the `auto-optimize-` prefix, and
  flag a clone from before 0.1.0 named `auto-optimize`, which answers the
  same requests.
- `examples/` gains three short searches with their prompts, dashboards and
  results: `fasta-stats/` (a command-line tool, output byte-identical),
  `kmer-memory/` (peak memory as the objective, time observed) and
  `smooth-coverage/` (a 1e-9 tolerance gate).

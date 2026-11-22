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

### Claude Code skill

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
- The dashboard opens an iteration's diff against its parent when its point
  on the objective chart is clicked.

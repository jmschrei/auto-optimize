# Reading a search without changing it

For a question about a search: one this session runs, one another session
started, or one that ended. Every command here only reads, and none starts a
process that outlives it. To continue a search instead, follow
`references/resume.md`, which starts the dashboard publisher and acts on what
it finds; answering a question needs neither.

## Find it

`$WORK` is the folder that holds `autooptimize/`. Searches go in
`~/auto-optimize/<YYYY-MM-DD>-<name>/` unless the user chose another place at
step 0; `ls ~/auto-optimize/` lists them.

`$WORK/autooptimize/config.json`'s `skill.name` says which skill made the
search. These scripts read a search made by `auto-optimize-code` or by an
earlier version with no name; for any other name they refuse with `this search
was made by the … skill`, and the search is read with that skill's scripts.

## Two calls first

```bash
python3 $AO/stages.py status $WORK          # the five stages and the next move
python3 $AO/autoopt.py status $WORK --brief # round, root, counts, what is in flight, the last six settled
```

## Question to command

| Question | Command or file |
|---|---|
| Has it ended, and what won? | `$WORK/autooptimize/ended.md`, present only after `end`; the paired timing of the winner against A0 is `final.json` beside it |
| What is the best so far, and by how much over A0? | `autoopt.py table $WORK --status ok --sort best --limit 5 --fields id,title,median,vs_base,vs_base_pct` |
| Which iterations improved on everything before them? | `autoopt.py table $WORK --frontier` |
| Every iteration, or one slice | `autoopt.py table $WORK` with `--status`, `--category`, `--round`, `--parent A5`, `--promoted`, `--sort`, `--limit`, `--fields` (`references/cli.md`) |
| What did A7 try, and how did it do? | `autoopt.py show $WORK A7`: numbers, both differences, the keep verdict, gates, observers, the code change, the notes, and the paths to read further |
| Why did A12 fail? | `autoopt.py show $WORK A12` prints the status, the gate that broke, the failed check or each failed seed's error; then its `README.md`; a run's `runs/seed-<n>/log.txt` only for the full error |
| What changed in the code? | `show` prints the change's size and the command for the whole diff: `git -C $WORK/autooptimize/repo.git diff autoopt/<parent>...autoopt/<id>` |
| The path from A0 to the winner | `autoopt.py path $WORK --markdown` |
| What happened in round N | `$WORK/autooptimize/rounds/round-<N>.md` |
| What the search has learned | `$WORK/autooptimize/knowledge.md` |
| Which ideas are left, and which were tried | `$WORK/autooptimize/ideas.md` |
| The objective, the gates, the keep rule, what is off limits | `$WORK/autooptimize/spec.md`; the numbers in `config.json` |
| What a subagent was told | `$WORK/autooptimize/briefs/<id>.md` (bare number: `briefs/7.md`) |
| The report to the user | `$WORK/finish/final-report.md`, written at step 5 |
| What it cost in tokens | `autoopt.py tokens $WORK`, from Claude Code's transcripts |

## For a program

`table --json`, `show --json` and `path --json` print raw values, and
`table --tsv` a header and tab-separated rows. The record files and their
fields are in `references/layout.md`; each carries `schema_version`
(*Version fields*). Read `result.json` and `meta.json` rather than recomputing
anything from `runs/`.

## What not to open

- `status --json` prints every `result.json` whole, every seed included, and
  grows with the search. `table --json --fields …` gives the same rows in the
  columns asked for.
- `runs/*/log.txt`, unless a run's error is the question.
- `repo.git/`, `reference/` and `cache/`: git objects, cached outputs and
  compiler caches, none of it prose.
- An iteration's code, unless the question is about the code: `show` and the
  diff say what changed.

# External validation

One or more measures computed **once a round** on inputs the search does not
otherwise touch: against the new root after each promotion. It may be slow.
It is **not a gate and not an observer**: it chooses nothing, rejects nothing,
and its numbers need not be comparable to the objective.

A search measures one input, and an optimization can fit itself to it: a
bitwise gate proved on one file says nothing about a larger input, an empty
one, or one of size 1.

`$AO` and `$WORK` are as in `SKILL.md`.

## Three rules

- **The user signs off before it ever runs.** Infer a good validation and
  propose it: the command, what it measures, and what it costs per round.
  Only after they say yes, run `validate $WORK --approve`; `validate`
  refuses until then. Inferring the validation is your job; approving it is
  not.
- **A bad result goes to the user, and nothing else changes.** Tell them
  with the round's summary, which carries the record. Do not revert the root, do not turn the check into a gate
  mid-search, and do not carry on as if it passed. Bit-exact on the
  benchmark and wrong on a wider set is the finding.
- **Name the round and the iteration it ran against.**

## Choosing one

| Search | A validation that catches a win tuned to the benchmark's input |
|---|---|
| kernel speed, bitwise gate | the same bit-identity check over many inputs: bigger, smaller, a single element, degenerate shapes, other dtypes |
| a command-line tool's speed | real files of other sizes and formats, other flag combinations, the malformed inputs it must still reject |
| a latency change | the other entry points and batch shapes, the shapes production sends |

## Setting it up, at `init` (or `init --force` later)

```bash
--validation-command 'python3 validate_all.py --iteration {iteration} --out {out}' \
--validation-measures n_inputs,n_bit_mismatches_all,largest_input \
--validation-note '12 input files, 40 to 5000 records, sizes 1 to 60'
```

The command must contain `{iteration}` and `{out}` and name at least one
measure. `{src}` (the worktree, as in `command`), `{round}`, `{id}`,
`{out_abs}` and `{cache}` (of the validated iteration's commit) are also
substituted. It runs with the iteration's directory as its working directory.
In plain mode that directory is the worktree, so a script copied in with
`--copy` resolves relatively, as above. In repository mode it holds only
records, and `--copy` puts the script in `autooptimize/harness/`: give its
absolute path in double quotes, and have it import from `{src}`:

```bash
--validation-command "python3 $WORK/autooptimize/harness/validate_all.py --src {src} --iteration {iteration} --out {out}" \
```

It writes `<out>/validation.json`:

```json
{"measures": {"n_inputs": 12, "n_bit_mismatches_all": 0},
	"units": {"n_bit_mismatches_all": "elements"},
	"ok": true, "note": "one input has a record of size 60"}
```

Every key is optional. A missing `ok` falls back to the script's exit code.
Name a unit for every measure that has one, here or in `config.json`'s
`external_validation.units` (added by hand; `init` has no flag). The
dashboard merges the two, the newest round's winning; a measure with no unit
is shown bare.

## Running it

```bash
python3 $AO/autoopt.py validate $WORK --round 3              # promote: orchestrator; with auto the runner runs it
python3 $AO/autoopt.py validate $WORK --dry-run              # print the command
python3 $AO/autoopt.py validate $WORK --id A57 --out $DIR/validation   # a subagent, on its own iteration
```

| Flag | Meaning |
|---|---|
| `--round N` | the round to file it under (default: the current one) |
| `--id` | the iteration to validate (default: the root); a label or a number, never a path |
| `--approve` | record the user's sign-off in `config.json` with the date, and run nothing |
| `--dry-run` | print the command and stop |
| `--out DIR` | with `--id`: write the output and `record.json` into `DIR`, and file no round record |

`validate` **always exits 0**, whatever it found, and never touches a
`result.json`, a status, or the frontier. A round's record lands in
`validations/round-<N>.json` (`round`, `iteration`, `command`, `seconds`,
`exit_code`, `measures`, `ok`, `note`, `created`). `summary --round N`
prints it under a heading saying it gated nothing, and the dashboard draws
each numeric measure over rounds (`references/dashboard.md`, *What to
look at*).

To have a subagent validate its own iteration, brief it with `brief
--validate`, which writes the `validate` command with its label and `$DIR`
filled in, as in the last line above. A brief never gives the validation
script itself: `validate` substitutes `{iteration}` as the path the script
expects.

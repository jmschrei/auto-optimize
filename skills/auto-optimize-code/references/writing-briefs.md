# Writing a brief

The brief is everything a subagent knows about the search. `autoopt.py brief`
writes it from the record; the orchestrator supplies only what the record
cannot know.

## `brief --next`, the usual call

```bash
python3 $AO/autoopt.py brief $WORK --next                                  # a brief when the slot is free
python3 $AO/autoopt.py brief $WORK --next --count 1 --gpus 2 --note '…'    # one, with a note
```

It prints `status --brief`, then the table of each round summary written
since the last call, then a brief and its spawn prompt when the one slot is
free: one iteration runs at a time, so the slot is free when no iteration is
still worked on (a STALLED one included) and no brief is waiting to start.
It takes the next untried line of `ideas.md`, the latest miner stage first
and top to bottom. `--count N` briefs N lines regardless of the slot. It
fills:

| Flag | From |
|---|---|
| `--id` | the next free id |
| `--idea-line`, `--category` | the line, and its tag |
| `--title` | the line's function and the first clause of what it changes, cut to 70 characters |
| `--tried` | every iteration whose diff changed any file the line's *where* names: its median against its parent's, and whether it is in the root, passed the keep rule but is not in the root, was not kept and why, or is in flight. Past 15, it lists the root path, the latest 15 whose title names the line's function and the latest 15 overall, and counts the rest by fate in one line. The files each change touched are cached in `autooptimize/touched.json` |
| `--sections` | `code:<the file's name>` when the ledger has that tag, and `harness`, `machine` and `closed` when it has them |

A flag given beside `--next` replaces what it would fill (with `--count 1`,
since it applies to every brief written); `--note`, `--checks`, `--gpus`,
`--validate` and `--predict` pass through. `--count 0` prints the status and
the new summaries and briefs nothing. Add a `--note` for what the records
do not hold, such as a premise a report corrected.

## Every flag

```bash
python3 $AO/autoopt.py brief $WORK --id A7 \
	--title 'prefetch the next block in read_blocks' \
	--category CACHE_MISS --topic io \
	--idea @$WORK/scratch/idea-7.md \
	--tried 'A3 (larger read buffer): -0.002 s, inside the band. Nothing else touched read_blocks.' \
	--sections code:reader,closed \
	--gpus 2
```

It writes the brief to `autooptimize/briefs/7.md`, and what `new --brief 7`
creates the iteration from to `briefs/7.json`. It prints a short prompt that
has the subagent read the brief, `references/subagent.md` and the ledger in
its first turn, so the brief never enters your context. Spawn the subagent
with that prompt, unedited, and the description and model on the stderr line.
`brief --prompt <id>` prints the prompt of an existing brief again, for a
subagent that takes the iteration over.

## What it fills in

| From | In the brief |
|---|---|
| the skill and the search | absolute `AO`, `WORK`, `DIR`, `SRC`; the prompt gives the paths of the brief, `references/subagent.md`, the ledger, `tools/README.md` and `tools/checkers/README.md` |
| the flags | label, parent (the current root by default), category, topic, title, prediction; `briefs/<id>.json` holds them for `new --brief` |
| the parent's `result.json` | how long its runner call took, and whether the subagent runs the runner in the foreground or the background |
| `config.json` and `status` | the objective and its direction, where the search stands (the root and its median, the keep rule's reference, and any better median outside the root's path), every gate with its threshold, the observers, the runs per iteration, the unit-test command, the GPUs (for every command that runs the code) or "needs no GPU" |
| `spec.md`, verbatim | the keep rule, the off-limits line, the thread counts timed, the portability line, every rule under *Rules added during the search*, and the path of `spec.md` itself |
| the mode and the code | a *Checks* section: what the runner runs, when the search has the task's checks, and the checks the subagent owes (`references/subagent.md`, *Checks beyond the runner*); with `promote: auto`, a *Promotion* section; the repository-mode limits; a numba note when the parent's branch imports numba |
| `config.json`'s `subagent_model` | not in the prompt: a stderr line naming the Agent call's description and model |

It makes `new`'s checks before writing anything (the parent exists and is
`ok`) and refuses:

| Refusal | Do |
|---|---|
| `id N is taken (an iteration, a branch, or a directory set aside)` | use the next free id it names; `status` prints it too |
| `id N is taken (a brief not yet started …)` | another brief holds that id: take the next one, or pass `--replace` to rewrite that brief |
| `this search uses GPUs and records no gpu_command: pass --gpus …` | give the indices this iteration holds, or the command that allocates them on this machine |
| `parent … is gate-failed` | `references/step3-rounds.md`, *Ids, parents and briefs* |
| `refusing to brief an iteration: step 0 is not finished` | `references/step0-interview.md`, *Writing it down* |

## What the orchestrator supplies

| Flag | Holds |
|---|---|
| `--idea-line` | the `ideas.md` line the idea comes from, by text only that line holds or by number: the line is the idea when `--idea` is not given, and the subagent's `finish` strikes it with its label and outcome |
| `--idea` | required without `--idea-line`: two or three sentences, what to change, in which file and function, and why it might help; with it, the text that replaces the line's |
| `--tried` (required) | the iterations that already touched this code and what each measured, or "none" |
| `--sections` | the ledger tags that matter for this idea (`code:trunk`, `closed`) |
| `--checks` | checks owed beyond that table, such as a guard path on an input the compare lacks |
| `--predict`, `--predict-basis` | the expected change against the parent, in the objective's units, and where the number came from (`references/promotion.md`) |
| `--gpus` | required unless `"gpu"` is false or `gpu_command` is set, which it then takes |
| `--validate` | ask for the approved external validation on this iteration |
| `--note` | one more line; repeatable |

`--idea`, `--tried`, `--checks` and `--note` take text, or `@file`.

## What makes the supplied parts survive contact

- **State premises as things to confirm.** `references/subagent.md`, *Check
  your brief before you follow it*, lists premises that turned out false.
- **List what was already tried, with its measurement.** Naming the
  iterations that touched this code, and lost, buys exploration instead of
  repetition.
- **Point at the ledger rather than retyping it.** `--sections` names the
  entries; a fact the ledger lacks (this root's per-case medians, say) goes in
  `--idea`.
- **Give the reason for each extra limit.** "No `fastmath`" is followed; with
  the reason ("it reassociates the accumulation and the gate is bitwise"), it
  is also generalized to the next temptation.
- **Do not quote a stale *Gain*.** Profile shares go stale within a few
  roots: for a line whose *Gain* was taken on an older root, pass `--idea`
  with the line's text minus the *Gain*.
- **Ask for the license notice with any translation.** A brief that lets the
  subagent port a routine from another project (numpy, scipy, a C library)
  adds a `--note` that the project's notice goes above the result
  (`references/promotion.md`, *Code from another project*). Subagents leave it out otherwise.
- **Ask for findings in the reply when the subagent cannot write them.** Some
  environments refuse a subagent's write to a `.md` file; a `--note` asks it
  to return the text in its reply, for the orchestrator to save.

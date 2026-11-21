# A second search, changing the terms, and starting over

| Situation | Do |
|---|---|
| A different premise or a rewrite, whose numbers should not be compared with the iterations so far | a **new search** seeded from the current winner (`init` it with the winner's code as its baseline) |
| The root reached `floor`: the benchmark is too small to resolve a further gain | a **new search** on a larger input, sized so that this root takes 20 to 50 times the floor; its baseline is this root's code, or the original code when the user wants the whole gain measured on the new input (ask which) |
| The objective moved tenfold, so the noise band no longer fits | nothing: the next phase re-measures the band from the root (`references/step3-rounds.md`, *When the objective moves by an order of magnitude*) |
| A gate that may be too strict, explored while the strict search goes on | run a **second search** beside it |
| The objective, or how the benchmark computes it, turns out wrong, a benchmark bug included | a **new search** |
| A gate's threshold the user wants changed in place | `config --set`, then re-judge the stored runs (*Changing a gate in place*) |
| An observer added, or `runs_per_iteration` raised | `config --set` or `--add-observer`, and tell the user |
| The iterations so far are not worth keeping, the baseline is | **reset** (*Starting the ideas over*) |

## A second search beside the first

When the constraint itself is the question, do not change the gate
mid-search. `init` a second search whose baseline is the first's current winner,
and run both:

| | |
|---|---|
| Each keeps its own frontier, spec and dashboard | neither contaminates the other |
| Both gate against the **same original reference** | their numbers stay commensurable |
| The strict one keeps producing a result that can be used | while the looser one explores |

- **Wins can flow back.** A change developed under the relaxed gate may turn
  out bitwise exact and transplant into the strict search; check each looser
  win for whether it is gate-clean.
- **A transplant is priced again, never assumed.** The same change can help
  in one tree and hurt in the other; brief it with "measured at X there;
  measure it here".
- **Halve each search's runs at once** (`gpus_per_agent`), so the total
  concurrency is unchanged
  (`references/step2-baseline.md`, *Measure what concurrency does to the
  objective*).

## Changing the objective or a gate

Every comparison already made was made under the old definition.

| Change | What it does to the record |
|---|---|
| a gate's threshold | every earlier `pass` and `FAIL` was judged differently; an iteration that failed may now pass and is still recorded as failed |
| the objective, or how the benchmark computes it | every median is a different measurement; the frontier means nothing |
| an observer added | harmless; earlier iterations have no value for it |
| `runs_per_iteration` raised | earlier iterations are `ok` with fewer runs; comparisons still hold. A gate against A0's output needs A0 to run the new seeds (`references/step2-baseline.md`, *A gate that compares against the baseline's output*) |

**A changed objective is a new search**: `init` a new `$WORK`, copy the
current winner's code in as its baseline, and tell the user the old search was
superseded, and why. When the new search keeps an
identity gate, copy the old search's `reference/` into it and compare against
that, so outputs are still checked against the original's rather than
drifting one search at a time. The only alternative is re-running every old
iteration, at what the search cost. `config --set` refuses the objective, the
command and `seed_base` for this reason.

### Changing a gate in place

When the user asks for it:

```bash
python3 $AO/autoopt.py config $WORK --set gates.max_abs_diff_vs_base.threshold=1e-4 \
	--reason '<the user's words>'
python3 $AO/run_iteration.py $WORK/autooptimize/iterations/<id>
```

`config` validates the change, writes `config.json` and appends the dated rule
with the reason to `spec.md`. A threshold changes no measurement, so nothing is
re-measured: running `run_iteration.py` on an iteration without `--rerun`
re-judges its stored runs under the new gate. Do it for the baseline and for
every iteration whose verdict the change could flip; one whose runs now pass
the gates runs the unit tests and the compare it had skipped. Iterations in
flight read `config.json` when they started and keep the old gate; re-judge
them once they settle. Tell the user.

With the task's checks, change `compare.py`'s rule to the new gate in the same
step. The next run of `run_iteration.py` caches A0's outputs again before any
compare, and every iteration it re-judges is compared again.

## Starting the ideas over, keeping the baseline

The search is worth continuing but its iterations are not, such as a topic
split that turned out wrong or a round built on a premise the profile refuted.
The baseline is not paid for again. A bug in the
benchmark is not this case: it is in A0's code too, so it is a new search.

```bash
python3 $AO/autoopt.py reset $WORK                   # report, change nothing
```

Show the user the report and run `reset $WORK --apply` only after they agree:
it deletes records, not only artifacts. Copy anything worth keeping out
first, since a branch that reset drops takes its code with it:

```bash
git -C $WORK/autooptimize/repo.git format-patch autoopt/0..autoopt/7 -o $WORK/finish/kept-ideas/
```

| Kept | Removed |
|---|---|
| A0: worktree, branch, `result.json`, `runs/`, `README.md` | every other iteration's directory and branch |
| `config.json`, `spec.md`, `ideas.md`, `knowledge.md` | `rounds/*.md`, `validations/` |
| `reference/`, and anything else beside `iterations/` | the round counter and root history, reset to round 1 |
| | unreachable git objects, pruned with `gc --prune=now` |

What it puts back in order:

- **The root moves to `--new-root`**, and the round counter and root history
  restart at round 1.
- **Every kept iteration's `meta.json` and `result.json` go back to round 1**;
  A0 keeps round 0.
- **Unreachable git objects are pruned**, so the directory actually shrinks;
  the bytes reclaimed are reported.

What it finds and leaves to the user:

- **A dashboard publisher watching this search** keeps serving the removed
  iterations until it rescans. `reset` lists each pid, its output directory
  and the command that stops it, and does not stop it: the window may be
  somebody else's.
- **References in `ideas.md` and `knowledge.md` to iterations that will not
  exist.** The files are kept, but a struck idea reading `A12: promoted`
  against a missing A12 is a trap for the next reader, and once ids are reused it names
  a new, unrelated iteration, so the ids are listed.

The objective, the gates and the reference are untouched, so iterations
measured before and after a reset are still comparable. Reset is not for a
changed objective: the measurements it keeps would be incommensurable with
every new one.

| Flag | Meaning |
|---|---|
| `--apply` | actually remove; without it, nothing is deleted |
| `--keep 0,7` | keep more than the baseline, comma separated |
| `--new-root 7` | which kept iteration becomes the root. Default: the lowest id in `--keep`; point it at a promoted winning iteration to carry the winner across |
| `--force` | remove iterations that have no `result.json`. They are running now or died mid-iteration, and removing a directory a live subagent writes into breaks that subagent |

It refuses, before touching anything, an id that does not exist and a `--keep`
naming an iteration with no `result.json`. Ids are reused afterwards: the next
free id is one past the highest kept id or directory set aside (`<id>.abandoned`),
and the report prints it.

**`reset` is not `prune`.** Prune deletes artifacts and keeps the record, so
the tables and the dashboard still show every iteration. Reset deletes the
record.

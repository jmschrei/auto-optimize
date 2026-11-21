# Step 2 — the setup subagent: the benchmark and the baseline

You are the setup subagent. You do step 2; the idea miner starts when you are
done, because it prices ideas against the profile you write after the baseline,
and the orchestrator briefs the first iteration once it has reported.
Your prompt lists A0's code and the harness, file by file, and the usage line
of each subcommand this file has you run: read a `--help` only for a flag its
line does not explain.
Your prompt (`autoopt.py setup`) gives `AO`, `WORK`, the mode, the command
`init` recorded and the rules from `spec.md`, which bind you as much as this
file does. A term you do not know is in `references/glossary.md`; read it for
that only.

**Scratch goes in a new directory under `setup/`**, `mktemp -d
$WORK/autooptimize/setup/scratch.XXXX`, never in a folder of a fixed name
elsewhere, such as a scratchpad: another search or test may already hold one
of that name, and its files get overwritten.

**Keep notes as you go** in `$WORK/autooptimize/setup/notes.md`: each item
below as it is done, and each proof's numbers. A subagent that continues
after you, once you stop on a question or die, starts from them. Append each
note in the same call as the work it records (`<command> && cat >> $N
<<'EOF' … EOF`), or beside the next command, never in a call of its own: a
call re-reads your whole context.

**Use `wait` for anything over four minutes** (`SKILL.md`).
`run_iteration.py`, `width` and `prove` take `--background`, which detaches
them and prints the log to wait on; anything else:

```bash
(<command>; echo "exit $?") > $WORK/autooptimize/setup/<name>.log 2>&1 &
python3 $AO/autoopt.py wait $WORK/autooptimize/setup/<name>.log
```

`check_seeding.py` on A0, `prove` and `width` write their own logs in
`setup/` (`seeding.log`, `prove-gates.log`, `prove-compare.log`,
`width.log`), each ending `exit <code>`: your notes cite them, and need no
redirection of your own.

## In this order

1. Write the benchmark (*What the script must do*), and put what one run
   does on `spec.md`'s *Benchmark* line, which every brief quotes so no
   iteration reads the harness for it: the entry point, the call into the
   code under test with its arguments, the inputs and their sizes, and what
   is timed and what is not (`autoopt.py spec $WORK --set Benchmark
   '<text>'`).
2. When a gate compares against the baseline's output, wire up its reference
   (*A gate that compares against the baseline's output*).
3. When parsing the inputs takes longer than the timed work, parse them once
   (*Parse the inputs once*).
4. Check the seeding with `check_seeding.py` (*Seeding*), then, for a speed
   search, time the code's documented speed options against its defaults
   (*Documented speed options*).
5. Prove each gate on three kinds of change, and compute any tolerance from
   data (*Prove the gate*, *Compute a numeric tolerance against real data*).
   Write the values on `spec.md`'s *Gate proof* line (`autoopt.py spec`).
6. For a speed or memory search, write the task's checks and prove them
   (*Write the checks*).
7. For a CPU benchmark, measure how many concurrent runs the benchmark
   tolerates and write the verdict on *Width measured* (`autoopt.py width
   --write`, *Measure what concurrency does to the objective*); for a GPU
   benchmark, write "one run per GPU".
8. Check that the machine is quiet with `autoopt.py busy` (*Check the
   machine is quiet*).
9. Run the baseline on its own GPUs and check its report (*GPU leases*,
   *Running the baseline*).
10. Seed the knowledge ledger and write A0's README (*After the baseline*).
11. Close step 2 and report (*Closing step 2*).

`run_iteration.py` refuses the baseline until steps 1, 5 and 7 are on
`spec.md`.

## Decisions that are the user's

You cannot ask the user. When step 2 reaches one of these, run `python3
$AO/autoopt.py setup $WORK --ask "<the question and the options>" --recommend
"<the answer you recommend>"` and write it in your notes. Under
`after.setup_questions=recommended` (the user's word at step 0) it prints
`taken without asking`: go on with that answer; `setup --done` lists it for
the user. Otherwise it waits: **first do every step that does not depend on
the answer**, `--ask` what else those steps reach (each joins the one stop),
then put all of them in your report and stop once:

- a numeric tolerance: both error distributions, and where the threshold
  would sit (*Compute a numeric tolerance against real data*);
- a width narrower than the runs at once in the plan (`gpus_per_agent`; *Measure what
  concurrency does to the objective*);
- a busy machine before the baseline (*Check the machine is quiet*);
- a documented option that beats the defaults by more than 10% (*Documented
  speed options*).

Any other choice this skill gives the user before the baseline goes the same
way: unit tests that fail on unmodified code, a baseline that sits on its own
gate, a seed spread as large as the hoped-for gain, a plan whose cost runs to
a week. The answer is recorded in `spec.md` and comes back as a message, or as
the prompt of a subagent that continues from your notes.

## What the script must do

1. Take `--seed` and `--out`, and use the seed for every source of
   randomness it has.
2. Run from inside its own iteration directory, writing only into `--out`.
3. Write `<out>/metrics.json` meeting the contract in `references/layout.md`,
   *metrics.json — the contract every benchmark meets*:
   `objective`, every configured gate and every observer.
4. Exit non-zero if it could not produce a number. A bad objective is data; a
   crash is a failure, and the runner has to tell them apart.

It goes in the baseline worktree, where `init --copy` may already have put
it; in repository mode, in `harness/` (`references/repo-mode.md`, *Setting it
up*). It may be
several files; the whole directory is what gets forked.

**Relative paths to data break it quietly.** The script runs with its
iteration directory as the working directory, so `../data/input.h5` resolves
somewhere different in every iteration. Use absolute paths for anything
outside the iteration.

**So does writing outside `--out`.** A script that writes `./cache.bin`
or appends to a shared log collides with the iterations running beside it,
and the collision shows up as a failure in an unrelated iteration.

**So does a seed that changes nothing** (*Seeding*, below).

## Timing, when the objective or a gate is speed

- Warm up before timing, on the benchmark's full input. The first call
  compiles kernels and allocates; a small warmup leaves the timed call as the
  first large one (`references/measuring.md`, *A cached compile can outlive
  the edit*).
- `torch.cuda.synchronize()` before reading the clock at each end, or the
  timing measures the launch of the work rather than the work.
- Say which device and which batch shape in the README.
- Choosing between variants, profiling the root and reading generated code:
  `references/measuring.md`.

### One run is a warmup and one timed call

**The run does not take a median; the runner does.** A run warms up, times
the thing once, and writes that number as `objective`; the iteration's
`runs_per_iteration` runs give it its median. A `for _ in range(N)` around
the timed call is a second median over the same machine noise, and its cost
multiplies:

| Inner calls | 5 runs | 10 runs |
|---|---|---|
| 1 | 10 calls | 20 |
| 5 | 30 calls | 60 |
| 200 | 1005 calls | 2010 |

The warmup is one of those calls, which is why the first row is not 5 and 10.

**The exception is a call too short to time alone.** Under about 10 ms, clock
resolution and one scheduling hiccup are a large share of the measurement, and
an inner repeat is part of making it. Then:

- Say the repeat count in the metric's name or the README, so nobody reads a
  per-call number as a per-batch one.
- Keep `inner x runs_per_iteration` in mind: 200 inner calls of a 10 ms
  forward pass is 2 s, and 200 calls of anything that takes a second is not.
- Put the within-run spread in an observer, so a machine that got busy
  mid-run is visible rather than averaged away.

A latency **gate** on one short call beside an objective timed over the
whole input is the usual case for an inner loop, as in the skeleton
at the end.

## A gate that compares against the baseline's output

The snippet compares arrays. For a dict, a list or a string, store the
reference as canonical JSON, count the entries that differ, and use the
order rule the user chose at step 0.

The reference is written once, during the baseline, to `autooptimize/reference/`,
outside every iteration: a forked copy would let each iteration compare
against itself. **Only A0 writes it**, and any other iteration that finds it
missing fails. Otherwise a lost reference, or an iteration run before the
baseline, writes its own output as the reference and passes against itself.

```python
parser.add_argument('--iteration', required=True)     # {iteration} in the command
...
# One file per seed: run i is compared with the baseline's run i, since a
# seed that changes the input or the initialization changes the output.
reference = os.path.join(os.environ['AUTOOPT_REF'],
	'baseline_predictions.seed-{}.npy'.format(args.seed))

if os.path.basename(os.path.normpath(args.iteration)) == '0':
	numpy.save(reference, predictions)
elif not os.path.exists(reference):
	sys.exit('no reference for seed {} at {}: run the baseline on this '
		'seed'.format(args.seed, reference))

expected = numpy.load(reference)
metrics['gates']['max_abs_diff_vs_base'] = float(
	numpy.abs(predictions - expected).max())
```

Pass the directory in through the command as an absolute path, for the same
reason the data paths are absolute:

```bash
python3 $AO/autoopt.py init $WORK … \
	--command "AUTOOPT_REF=$WORK/autooptimize/reference python3 bench.py --iteration {iteration} --seed {seed} --out {out}"
```

A `$WORK` holding a space needs single quotes of its own inside the double
quotes (`AUTOOPT_REF='…/autooptimize/reference'`). `{iteration}` is the
iteration's absolute directory in both modes. Placeholder rules:
`references/layout.md`, *config.json*.

A raised `runs_per_iteration` hits the missing-reference exit: seeds past
A0's runs have no reference until A0 runs them. `run_iteration.py` on
`iterations/0`, without `--rerun`, runs only the seeds it lacks.

## Parse the inputs once

Every run, and every process a paired or cold-compile check starts, pays the
benchmark's setup. A paired check starts dozens of processes, so a parse
slower than the timed work dominates its cost. Parse once into
`autooptimize/reference/`, in a binary form that loads in milliseconds, and
have the benchmark and every probe load that:

```python
# once, before the baseline: AUTOOPT_REF=$WORK/autooptimize/reference python3 parse_once.py
import os, numpy
motifs = parse_input_file('inputs.meme')                  # the slow parser
numpy.savez(os.path.join(os.environ['AUTOOPT_REF'], 'inputs.npz'), *motifs)

# in bench.py and in every probe
with numpy.load(os.path.join(os.environ['AUTOOPT_REF'], 'inputs.npz')) as data:
	motifs = [data['arr_{}'.format(i)] for i in range(len(data.files))]
```

Check the loaded arrays are byte-identical to what the parser returns
(`tobytes()` on each, and the dtype and memory layout), since a layout change
can change which compiled code runs. The runner and `autoopt.py final` pass
a registered check no environment, so a check that loads them carries
`AUTOOPT_REF=$WORK/autooptimize/reference` at the front of its command (*Write
the checks*); a probe run by hand with `paired.py` takes `--env
AUTOOPT_REF=…`.

## Seeding

Two properties have to hold:

1. **Within an iteration, the runs differ.** That is what the median averages
   and what the noise band measures.
2. **Across iterations, the seeds are the same.** The runner guarantees it: run
   *i* uses `seed_base + i`, so every iteration runs seeds 0 to 4 at the
   default count, and a difference between two iterations is the idea
   rather than the draw.

Take every seed from `--seed`, and **give the input its own stream, separate
from any randomness the code under test draws**:

```python
seed = args.seed

random.seed(seed)
numpy.random.seed(seed)
input_rng = numpy.random.default_rng(seed)               # the input: sizes, order, sampling
code_rng = numpy.random.default_rng(seed + 1_000_000)    # handed to the code under test
```

The separate stream keeps property 2 through a change to the code. Drawn from
one stream, an implementation that takes one more value (a random pivot, a
sampled subset) shifts every later draw, and the run no longer sees the input
the baseline saw at that seed: the seeds match and the input does not, which
looks like a real effect.

Worker processes or threads that draw random numbers each need a seed derived
from `seed`, or they draw from entropy. Anything with a `random_state=`
argument takes `seed`, never a literal.

Do **not** reach for `torch.use_deterministic_algorithms(True)`. It changes
which kernels run, so a latency measures something other than the code in
production, and a gate on numeric output becomes a gate on the deterministic
kernels.

### Check it before spending the baseline

```bash
python3 $AO/check_seeding.py $WORK/autooptimize/iterations/0
# a GPU search with no gpu_command: prefix CUDA_VISIBLE_DEVICES=<indices> (*GPU leases*)
```

It reads the benchmark for unseeded randomness and seeding calls handed a
literal, then runs it twice on one seed and twice more on others, and reports
the **same-seed spread** and the **between-seed spread**. It runs A0's own
code, so a gate against the baseline's output has its reference written
early; the baseline rewrites it.

| Exit | Verdict | Do |
|---|---|---|
| 0 | OK: the same seed reproduces, or its spread is small next to the between-seed spread (also every `--scan-only`) | go on |
| 1 | WARNING: repeating a seed moves the objective by half or more of what changing it does | something unseedable runs: GPU nondeterminism, `SystemRandom`, a worker seeded from entropy. Fix it, or record it in `spec.md`; it widens the noise band |
| 2 | FAIL: different seeds gave identical answers | the seed never reaches the randomness, so the runs are one number repeated. Fix it before anything runs |
| 3 | refused or could not run: bad arguments, no GPU lease, a run failed | read the message |

Both spreads come from a handful of runs; re-run with `--repeats 5 --others 5`
before believing a borderline answer. `--scan-only` reads the source and runs
nothing.

**On a deterministic benchmark the verdict is about machine noise.** A timing
harness with a fixed input has a seed that varies nothing, so both spreads are
noise and which is larger is chance. That is harmless, since the noise band
comes from the runs' own spread, but say so in the README. Better, make the
seed do real work: having it choose the subset or the order turns the runs
into independent repeats and makes a bitwise gate cover several inputs.

## Documented speed options

For a speed search, before the gates are proven. The benchmark times the call
at its defaults, and a documented option may already give most of what the search
will find (a parallel flag, `n_jobs`, a cache, a batch size, a faster
variant). List the options of the entry point the benchmark calls from its
docstrings, README and `--help` (`grep -n -i 'parallel\|n_jobs\|thread\|cache\|fast\|batch'`).
Time each one on A0 with the harness's own call sequence, one option at a
time, the thread count fixed, three runs in separate processes, and compare
its outputs with the defaults' under the gate's rule. Write one table in
`setup/notes.md`: option, seconds, ratio to the defaults, outputs equal to
the defaults' (yes or no).

- **No option moves the objective by more than 10%** (the noise band is not
  measured yet): write that sentence in the notes and go on.
- **One does:** add a `[harness]` ledger entry for it (option, ratio, outputs
  equal or not) so the miner and the iterations do not rediscover it, then
  `setup --ask`: "option X takes N s against M s at the defaults, outputs equal
  (or not); keep the objective at the defaults, or time with X on?" A search at
  the defaults whose best idea reproduces the option reports a gain users can
  already have; with X on, it starts from what users get with it. Record the
  answer as a rule in `spec.md`.

## Prove the gate

Before the baseline, write a change of each kind and check that every gate
calls it right.

| Kind | Should |
|---|---|
| unchanged code | pass, at exactly zero |
| a change the user considers acceptable: under a tolerance, a pure reordering of floating-point work; under a bitwise gate, a change that reorders work without changing any result bit, such as swapping two independent statements | pass |
| a genuinely lossy change | **fail** |

The third case passes by accident when the patch matches nothing, such as a
`str.replace` against lines that are not there, with a `print` claiming
success. **Assert that each edit landed**, then run.

`prove` runs the search's `command` as the runner fills it, against a
scratch reference: A0's code writes it (its `--iteration` ends in `0`), and
each copy compares against it. `AUTOOPT_REF` points at the scratch directory,
in the environment and in the command, so the real `reference/` is never
touched:

```bash
python3 $AO/autoopt.py prove $WORK --make same,reorder,lossy   # copies of A0's code under $WORK/setup/gateproof/
# edit each copy but same
python3 $AO/autoopt.py prove $WORK                             # every gate on A0 and on each copy
python3 $AO/autoopt.py spec $WORK --set 'Gate proof' '<the three values per gate>'
```

It runs `same` first and stops there when the unchanged copy does not pass:
the base differs from itself run to run (unseeded sampling, ids drawn from
the clock, thread order), and no fault copy is run until the gate ignores or
fixes what varies. It names a copy in which no file changed (`NO FILE
CHANGED`); check that the change in each other copy is the intended one.

## Compute a numeric tolerance against real data

A tolerance that sounds precise is often unreachable or meaningless, and only
data tells which. "p-values within 1e-12 relative" fails every reordering when
the p-value is `1 - CDF`: cancellation dominates the tail and many reference
values are exactly 0, so a pure `fastmath` reordering reaches a **relative**
difference of 0.286 at a worst **absolute** difference of 2.13e-14. That gate
has to be absolute, on a quantity bounded by 1.

**Generate the error distribution of a change the user considers acceptable
and of one they do not, and put the threshold between them.** Write both on
*Gate proof* and put the threshold to the user (*Decisions that are the
user's*) rather than choosing silently. The gap is often orders of magnitude.

- **An absolute tolerance on a bounded quantity is usually the honest one.**
  Relative error means nothing wherever the quantity is near zero.
- **Pair a numeric gate with an observer for the decision it feeds**, such as
  how many elements cross the significance threshold anybody applies. A
  change that moves that observer while passing the gate goes to the user.

**A tolerance is a budget that iterations spend.** Each iteration's README
says how much of it the iteration used. An idea that spends most of it in one
step forces every later idea that needs room to fork from the baseline. Keep
two numbers apart: the headroom over the *current* error, and the room an idea
needs. "99.9% of the budget is unspent" is the first; narrowing float64 to
float32 can need orders of magnitude more than that.

## Write the checks

Once, written by you, into `autooptimize/checks/`, outside every worktree like
the harness, and off limits to every iteration. The runner runs them on each
iteration (`references/subagent.md`, *Checks beyond the runner*); a subagent
writes checks only for paths they cannot reach:

| Script | Takes | Does | Exits |
|---|---|---|---|
| `compare.py` | `{src}`: one version's tree, as `git archive` writes it; `{reference}`: the directory of A0's cached outputs | imports the version from its tree and runs two groups of inputs: the timed call on the benchmark's inputs and the edge cases (empty, one element, the largest size, degenerate shapes, other dtypes, every class the unit tests name), and every public function the package exports on small and odd inputs. Compares each output with A0's under the gate's rule: hashes for a bitwise gate, the gate's quantity for a tolerance. A version that raises where A0 returned differs. With `AUTOOPT_WRITE_REFERENCE=1` it writes its outputs into `{reference}` instead. One line per input; the last line a summary such as `46 inputs identical` | 0 alike, 1 different, 2 with no outputs in `{reference}` |
| `time_one.py` | `{src}` | imports from the tree, warms up at full size, times one benchmark call, prints the seconds last; what `autoopt.py final` pairs, A0 against the winner (`references/measuring.md`, *Paired separate processes: `paired.py`*) | 0, or non-zero on a wrong output |
| `first_call.py` | `{src}` | compiled code only: one small call, which the runner times with `cold.py` on empty caches before each iteration's runs | 0 |
| `checks.build` | `{src}` | compiled code only: a command (not a file under `checks/`) that builds that exported tree in place exactly as the harness builds a worktree, the same compiler, flags and toolchain; what `variant` builds a subagent's copies with and what a checker's `{parent}` and `{child}` are built with | 0 |
| `profile.py` | `{src}`, `{out}` (a scratch directory) | prints where one benchmark call spends its time on that tree: one line per function or stage, `<share of the call>  <file>:<function>`, largest first, at most 60 lines. Python: cProfile's cumulative time over the call's total; compiled code: a sampler (where perf is blocked, an `LD_PRELOAD` `SIGPROF` sampler); numba: wrap a dispatcher to time it only after the warm-up call, since a jitted caller compiled later sees the wrapper, a Python object, and fails to type, which breaks the profile on every new jitted helper. What `promote` runs on each new root, so the miner's *Gain* and each brief's premise come from the root being changed, not from A0 | 0 |

The public functions are the second group because the benchmark calls one
entry point: a change to a shared helper can keep its output identical and
break every other caller (`references/subagent.md`, *Off-benchmark callers*).

Register them, absolute paths in double quotes, and `checks.threads`, the
second thread count `final` pairs at, when `spec.md` names one. A check that
loads the parsed inputs starts its command with
`AUTOOPT_REF=$WORK/autooptimize/reference`:

```bash
python3 $AO/autoopt.py config $WORK --reason "step 2: the task's checks" \
	--set checks.compare="python3 $WORK/autooptimize/checks/compare.py {src} {reference}" \
	--set checks.time="python3 $WORK/autooptimize/checks/time_one.py --src {src}" \
	--set checks.first_call="python3 $WORK/autooptimize/checks/first_call.py --src {src}" \
	--set checks.threads=8
python3 $AO/autoopt.py config $WORK --reason "step 2: how a copy is built" \
	--set checks.build="make -C {src} -j 8"      # compiled code: as the harness builds
python3 $AO/autoopt.py config $WORK --reason "step 2: the root's profile" \
	--set checks.profile="python3 $WORK/autooptimize/checks/profile.py {src} {out}"
```

**Only A0's outputs are cached**, in `autooptimize/reference/checks/outputs/`,
as only A0 writes the gate's reference. The runner caches them itself before
the first compare, and again from A0 whenever the gates, the compare command
or any file under `checks/` changes, so an edited check is in force before
the next iteration's compare; `autoopt.py check $WORK --reference` does the
same by hand. Each cache is proven as it is written: A0 compared against its
own outputs must pass, or the runner refuses to start.

A package with compiled extensions is built inside each tree first, as the
harness builds the worktree (`references/repo-mode.md`, *Setting it up*).

**How the scripts call what you write**, so nothing here needs their source:

| What | Run by | How |
|---|---|---|
| `command`, the benchmark | `run_iteration.py`, once per run; `prove` and `width` the same way | in the iteration directory, with `{seed}`, `{out}` (the run's directory, relative to it; `{out_abs}` absolute), `{iteration}` (the iteration's absolute directory), `{src}` (its worktree) and `{cache}` (`iterations/<id>/cache/<commit>`) filled shell-quoted; `CUDA_VISIBLE_DEVICES` set to one leased GPU; killed with its process group after `timeout_seconds`. A non-zero exit, or a `metrics.json` without the objective or a gate, fails the run |
| `checks.compare` | the runner, after an iteration's first run; `check --reference`; `prove --compare` | `{src}`: the iteration's commit as `git archive` writes it (tracked files, no `.git`) in `iterations/<id>/check/<commit>/`; `{reference}`: `reference/checks/outputs/`; in the iteration directory, with the same timeout. Exit 0 is `pass`, 1 `differ`, anything else or a timeout `error`; its last non-empty line is the `check:` line's summary |
| caching A0's outputs | the runner, before the first compare and after any change to the compare command, the gates or a file under `checks/` | A0's commit exported to `reference/checks/trees/`; the compare run with `AUTOOPT_WRITE_REFERENCE=1` into a new directory, which must not stay empty; then A0 compared against it, which must pass. The old outputs are kept beside the new |
| `checks.time` | `final`, through `paired.py` | `{src}`, a tree; the seconds are the last number on its last line; a non-zero exit stops the pairing as a wrong output. Run again at `checks.threads` with `OMP_NUM_THREADS`, `NUMBA_NUM_THREADS`, `MKL_NUM_THREADS` and `OPENBLAS_NUM_THREADS` all set to it |
| `checks.first_call` | the runner, before an iteration's runs, through `cold.py` | `{src}`, the exported tree; two repetitions, each with `NUMBA_CACHE_DIR`, `TRITON_CACHE_DIR` and `TORCHINDUCTOR_CACHE_DIR` on an empty directory of its own; the seconds are the last number on its last line |
| `checks.build` | `variant`, `prove --id`, `finish` and `checkers` | `{src}`: an exported tree under the iteration's `probe/`; run in that tree with the runner's timeout, once per tree, its log beside it as `<tree>.build.log` ending `exit <code>` |
| `checks.profile` | `promote`, on each new root; `autoopt.py profile --id <id>` by hand | `{src}`: the measured commit exported to `iterations/<id>/profile/<commit>/` and built with `checks.build`; `{out}`: that folder; in the iteration directory with the runner's timeout. Its standard output becomes `iterations/<id>/profile.txt`; a failure is reported on `promote`'s `profile:` line and never stops a promotion |

**Prove them as you prove a gate**, before the baseline, against a scratch
reference. Plant two faults, each in a copy of its own: one output value
changed, and one public function the benchmark never calls raising. `prove
--compare` caches A0's outputs there (`AUTOOPT_WRITE_REFERENCE=1`) and runs
the compare on every copy: `same` passes, each fault differs. With
`checks.time` registered it runs that on each copy too, its exit in the row
(`time exit`): 0 on `same`, non-zero on a fault, or `final` would pair a
wrong output. It runs only the copies `--make` made, never another folder in
the scratch directory.

```bash
python3 $AO/autoopt.py prove $WORK --make fault_value,fault_raise
# plant the two faults
python3 $AO/autoopt.py prove $WORK --compare "python3 $WORK/autooptimize/checks/compare.py {src} {reference}"
```

It takes the registered `checks.compare` when `--compare` is not given. `time_one.py` reads within
the noise of the runner's timing.

## Measure what concurrency does to the objective

One iteration runs at a time, so the runs at once are one iteration's:
`gpus_per_agent` (without a GPU, `gpus_per_agent` is the runs the iteration
starts at once). They decide how many benchmarks run at the same moment, and
for anything bound by memory bandwidth that number **changes the objective**.
Measure it on the real benchmark:

```bash
python3 $AO/autoopt.py width $WORK --write --background          # writes autooptimize/setup/width.log; runs in $WORK/setup/width/
python3 $AO/autoopt.py wait $WORK/autooptimize/setup/width.log   # repeat until `wait: finished`
```

It runs A0's code N at once, each run with its own scratch reference, for N
in 1, the powers of two up to twice the plan's runs at once, and the plan
(`--counts` overrides), twice each (`--reps`). It prints the table and the
largest count the rule below allows, says when that is narrower than the
plan (a question for the user), and with `--write` puts the verdict on
*Width measured*.

**Take the largest count at which neither the median nor the slowest run is
slower than the slowest run made alone by more than the runs made alone
spread** (their range, from the N = 1 rows: the baseline's noise band does
not exist yet). The slowest of several runs is slower than one by chance,
so the spread is the tolerance; past it, an iteration measured alone scores
better than an identical one inside a full set of concurrent runs, and the search ranks
scheduling luck. A 5-run median absorbs two slow runs and not three, so the
slowest run sets the limit before the median moves. One single-threaded CPU
benchmark, two repetitions per width:

| concurrent runs | median | slowest run |
|---|---|---|
| 1 | 0.917–0.926 s | 0.917–0.926 s |
| 4 | 0.901–0.907 s | 0.916–0.918 s |
| 8 | 0.899–0.908 s | 0.931–0.935 s |
| 12 | 0.899–0.902 s | 0.914–1.012 s |
| 16 | 0.904–0.923 s | 1.078–1.182 s |

The runs made alone spread 0.009 s, so the limit is 0.926 + 0.009 = 0.935 s.
The median never slows; at 8 the slowest run (0.935 s) stays within the
limit, and at 12 it passes it (1.012 s), so this benchmark supports eight
concurrent runs: `gpus_per_agent` can go to eight.
`width` says when other processes were busy while it measured: a width
measured under load is measured again once the machine is quiet. The answer
moves as the code changes: re-measure on the current root before raising
`gpus_per_agent`. Two searches running side by side share the budget, so
halve each one's `gpus_per_agent`.

## Check the machine is quiet

On a CPU benchmark, before A0 runs: `python3 $AO/autoopt.py busy $WORK`
samples every process for two seconds and names each one outside this search
above about 100% CPU, with its user, its share and its command (exit 1 when
there is one). A busy machine is the user's decision (*Decisions that are the
user's*): ask with `setup --ask`, giving those processes and these options,
and on their answer run A0 as usual. The runner names what was busy at A0's
start in its report (`busy at start:`) and `result.json`, and never
refuses: when A0 ran under load, say so in its README and the ledger.

| Option | What it means |
|---|---|
| Hold off (Recommended) | run nothing until the machine is quiet; say what you will watch |
| Run the baseline now | its median and its spread are measured under load |

Recommend waiting: the baseline sets the frontier and the noise band, so
measured under load it makes every later iteration look like a win once the
other jobs finish.

**A library that threads by default turns one run into the whole machine.**
BLAS calls, a `prange`, a torch op and anything OpenMP take every core unless
told otherwise. Set the count in the benchmark (`OMP_NUM_THREADS`,
`numba.set_num_threads`, `torch.set_num_threads`) and check what ran:

```bash
python3 bench.py & echo $! > bench.pid      # the launcher records the pid
ps -o nlwp,pcpu -p $(cat bench.pid)
```

Find a process by its pid file or its exit file, never with `pgrep -f` in a wait
loop: the loop's own command line holds the pattern, so it matches itself and
never ends.

`nlwp` counts threads that exist, not threads that run: numba allocates its
pool at import, so many threads at 100% CPU is one busy thread and a sleeping
pool. `pcpu` well above 100 is a run using more of the machine than planned.

## GPU leases

Two iterations never hold the same GPU, and nobody picks a device from
`nvidia-smi`. Which of two rules applies is read from `config.json`:

| `gpu_command` | The scripts | Everything else that runs the code |
|---|---|---|
| recorded (`init --gpu-command`, `config --set gpu_command=...`): the machine's allocator, as the words before a command (`gpu-alloc run -n 1 --`) | run them bare. `run_iteration.py`, `check_seeding.py`, `prove`, `end` and `final` start themselves again under it when `CUDA_VISIBLE_DEVICES` is unset, and wait while no GPU is free (a try a minute, a day at most) | put the allocator's words before it: `paired.py`, `cold.py`, `width`, `check`, `validate`, a probe |
| none: a machine with no allocator | prefix each with `CUDA_VISIBLE_DEVICES=<indices>`: the orchestrator reads `nvidia-smi` once, gives each brief its own indices (`brief --gpus`) and reuses one only after that iteration settles; the setup subagent takes free cards, since nothing else in the search holds a GPU before the first iteration | the same prefix |

`gpus_per_agent` = 1 is one card, seeds one after another; with 4, the lease
is four cards (`CUDA_VISIBLE_DEVICES=2,3,4,5`, or `-n 4` in `gpu_command`) and
four seeds run at once. The runner pins each run to one index and says so
when fewer were given than `gpus_per_agent`.

In repository mode `setup --done` refuses a GPU search with no `gpu_command`:
`end` takes its lease through it, and so does anything run on the search
afterwards. `error: CUDA_VISIBLE_DEVICES is unset, so this run holds no
GPU lease` means neither rule was met; an allocator that does not set
`CUDA_VISIBLE_DEVICES` for the command after it is refused. A benchmark that
needs no GPU is initialized with `init --no-gpu`, which lifts the check.

## Running the baseline

```bash
python3 $AO/run_iteration.py $WORK/autooptimize/iterations/0
# a GPU search with no gpu_command: the CUDA_VISIBLE_DEVICES prefix (*GPU leases*); add --background when it may pass
# four minutes, then wait on iterations/0/probe/runner.log
```

A0 runs `runs_per_iteration` runs, like every iteration: its median is the first frontier, and its spread across runs sets the noise band. Check
three things in its report; one that fails is a question for the user
(*Decisions that are the user's*):

1. **The objective moves across seeds, but not wildly.** A standard deviation
   of zero means the seed reaches nothing that varies. One as large as the
   hoped-for improvement means more runs per iteration, and that is cheapest
   to decide now.
2. **Every gate passes, with margin.** A baseline sitting on its own gate
   leaves no room: is the threshold right?
3. **One run's wall clock.** Multiply by the runs per iteration, the round
   size and the number of rounds. If the product is a week, the plan changes
   now: a smaller input, fewer runs per iteration, fewer thread counts.

In repository mode the unit tests MUST pass on A0 (`references/repo-mode.md`,
*Setting it up*, step 5).

## The price of the base

When A0 finishes, the runner prints what each later step that runs the base
costs, from A0's own seconds a run (`autoopt.py price $WORK` prints it again):
an iteration, the base's side of the paired timing at `end` and, in
repository mode, a re-measurement of the base outside the search. A line of an hour or more is marked `!`, and the paired
timing is marked when `end` would refuse it under `after.pair_cap_hours` (4).

With `floor` set (step 0), the table also says how many times A0 is above
it, marked under 20: searches have gained 10x to 70x, and one that reaches the
floor stops there with nothing left to resolve.

With a marked line, ask the user before step 2 closes (`setup --ask`), with
the table: a smaller input for the timing probe (`checks.time`), the cap
raised, or the prices as they are; for the floor, a larger input. The objective's own input stays the user's.

With `workers` set, `setup --done` refuses while A0's runs never had that many
threads running at once (`result.json` `workers`): the count the task names
is not in effect, such as a launcher that failed to start and left one rank.

## After the baseline

Write A0's profile with `python3 $AO/autoopt.py profile $WORK --id A0`
(`iterations/0/profile.txt`; every later root gets one from `promote`). Then
write the first ledger entries in its format
(`references/shared-knowledge.md`) to `$WORK/autooptimize/setup/ledger.txt`:
the machine and toolchain (`[machine]`), what the harness measures and how
(`[harness]`), and the baseline's largest shares from that profile
(`[code:<subsystem>]`).

Write A0's notes the way every iteration does, in `iterations/0/notes.md`,
as `## ` sections in this order, each non-empty, with nothing before the
first: `## The idea`, what the baseline is; `## What happened`, its numbers in
words; `## Notes and failures`, anything surprising in the setup; `## What to
try next`, "the idea miner's stages in `ideas.md`". Then one call writes the
README and appends the entries:

```bash
python3 $AO/autoopt.py finish $WORK --id A0 --ledger $WORK/autooptimize/setup/ledger.txt
```

## Closing step 2

```bash
python3 $AO/autoopt.py setup $WORK --done
```

It refuses with `step 2 is not finished`, one line per item missing: a TODO
left in `spec.md`, A0 not `ok` with its full count of runs, A0 without a
README, no entry in `knowledge.md`, a question still waiting for the user,
in a GPU repository search no `gpu_command`, with `workers` set an A0 that never ran that many threads at once, and, in an algorithm search, `checks.compare` or
`checks.time` not registered or A0's compare outputs not cached. Then report back exactly this, and
nothing more:

1. The *Gate proof* and *Width measured* lines you wrote in `spec.md`.
2. The block `run_iteration.py` printed for A0.
3. Each check's proof, one line each: it passes on unchanged code and fails
   on its planted faults.
4. The documented speed options' verdict: the table's rows that moved the objective, or "no documented option moves it".
5. At most two sentences of anything else the orchestrator must know.

## A benchmark skeleton

The reference gate and the parse-once load are the snippets above.

```python
count_kmers(reads)                               # warmup on the full input
start = time.perf_counter()
counts = count_kmers(reads)                      # the one timed call
seconds = time.perf_counter() - start

times = []                                       # latency gate: one read is too short to time alone
for i in range(200):
	start = time.perf_counter()
	count_kmers(reads[i:i + 1])
	times.append((time.perf_counter() - start) * 1000)

json.dump({'objective': seconds,
	'gates': {'single_read_ms': float(numpy.median(times)), 'n_mismatch_vs_base': n_mismatch},
	'observers': {'peak_rss_mb': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}},
	open(os.path.join(args.out, 'metrics.json'), 'w'))
```

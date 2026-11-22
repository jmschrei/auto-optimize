# Optimizing an algorithm implementation

The search changes how a function computes its answer, not the answer itself:
a faster parser, a leaner aggregation, a kernel rewritten in numba. When
the code is in a git repository, set the search up in repository mode first
(`references/repo-mode.md`). `$AO` and `$WORK` are as in `SKILL.md`.

**When the algorithm uses numba (`@njit`, `prange`), read *Numba*, below,
before briefing the first subagent.** Its
optimizations are labeled bit-identical, machine-precision or close, so a
bitwise gate rules options out before they cost an iteration. `autoopt.py
brief` adds the numba note to every brief whose parent imports numba.

## The objective is time or memory, and the other is always recorded

**Whichever of wall-clock seconds and peak memory is the objective, record
the other as an observer.** A 30% speedup that doubles peak memory is a
trade the user has to see, and an observer never recorded can be recovered
only by re-running every iteration.

| Objective | Observer | Gate |
|---|---|---|
| `seconds`, min | `peak_mb` | the outputs match the baseline's, in the form the user chose (`references/step0-interview.md`, *Gates*) |
| `peak_mb`, min | `seconds` | the same |

Every algorithm search also needs the output-identity gate: speed is easy to
buy by computing something else (`references/measuring.md`, *A suspiciously
large speedup is usually dead code*).

## Measuring both in one run

Take time and memory from **separate calls**. `tracemalloc` slows Python-level
allocation several-fold on a loop of small numpy allocations, so a call
timed under it measures the tracer.

```python
import time, tracemalloc

run(inputs)                                   # warm up: JIT compiles, caches fill

start = time.perf_counter()
result = run(inputs)                          # the timed call, untraced
seconds = time.perf_counter() - start

tracemalloc.start()
run(inputs)                                   # the memory call
peak_mb = tracemalloc.get_traced_memory()[1] / 1e6
tracemalloc.stop()
```

| Measure | Sees | Misses |
|---|---|---|
| `tracemalloc` peak | Python and numpy allocations, and arrays numba allocates inside `@njit`, including per-thread scratch in a `prange` | memory allocated by C, C++ or Fortran libraries outside Python's allocator |
| `resource.getrusage(RUSAGE_SELF).ru_maxrss` | everything the process ever held, in kB on Linux | cannot be reset; it includes imports and numba's compiler, so it cannot isolate one call |
| `torch.cuda.max_memory_allocated()` | GPU tensors, after `torch.cuda.reset_peak_memory_stats()` | the caching allocator's reserve (`max_memory_reserved`) |

Name the measure in the observer (`peak_traced_mb`, `max_rss_mb`): two of
these on the same call can differ by the size of the interpreter.

## Numba

**Decide the exactness class the gate allows before trying anything.** Under a
bitwise gate, try only bit-identical changes: anything else cannot pass, and a
pass on the benchmark's data alone is a coincidence.

| Class | Meaning | Typical sources |
|---|---|---|
| bit-identical | every output byte equal on every input | layout and loop-nest changes that keep each output's operations in order; integer regrouping; exact caching; skipping work proven to be +0.0 |
| machine-precision | differs only by floating-point rounding, which can accumulate | reordering or reassociating a float sum; `fastmath` without approximate functions; FMA contraction; multiplying by a precomputed reciprocal |
| close | an approximation with a bounded but visible error | float32 in place of float64; lookup tables; early termination; approximate `exp` or `log` |

A bit-identical label is often conditional ("no NaN or -0.0 in the data",
"while sums fit in int16"): prove the condition on all the data, or guard it
at runtime with a fallback (`references/measuring.md`, *Properties no gate
can see*).

- **A fresh `NUMBA_CACHE_DIR` for every variant and every cold measurement**,
  never a deleted one: `cache=True` keys a library on the decorated function's
  own file, so an edit to a callee in another file is served stale
  (`references/measuring.md`, *A cached compile can outlive the edit*).
- **Every new argument type, dimensionality or memory layout is a new
  signature**, compiled on its first call unless the cache already holds it;
  warm up once per signature the benchmark uses.
- **Time the parallel path as well as one thread.** False sharing in
  per-thread scratch can make the parallel path many times slower while every
  single-thread gate passes (*Fix the thread count*, below).
- **Assert which copy of the code was imported**
  (`sys.modules['<pkg>.<module>'].__file__`) before believing any timing.

## Fix the thread count

For a parallel kernel (`prange`, BLAS, a thread pool), the thread count used
by the timed call is part of the objective's definition. Set it explicitly in
the harness (`n_jobs=8`, never a default like `-1` that means every core),
ask the user for it at step 0, and write it on `spec.md`'s *Thread counts
timed, tracking inputs* line. An iteration that changes it is a different search.

**When the objective is timed at one thread count, ask whether to observe
another.** A search timed at `n_jobs=1` only cannot see a change that makes
the parallel path many times slower; it passes every gate and every unit
test. If the code has a parallel path, propose an observer timed at the
thread count users run (`seconds_8_threads`), from a separate call in the
same run, and gate nothing on it.

## Cold compile time

For code compiled at first call (numba, Triton, `torch.compile`), the
compile is paid once per machine and install, and no warmed-up timing sees
it. A cold first call can double over 100 iterations without anyone
noticing, and the unit-test gate slows with it: every iteration's tests
start from an empty cache, so they compile everything.

Step 2 writes `checks/first_call.py`, one call on a small input so the
compile dominates (`references/step2-baseline.md`, *Write the checks*). The
runner times it with `cold.py` on empty caches before each iteration's runs,
beside the parent's recorded value, on the `check:` line; a crash fails the
iteration before any run. A compile cost is the user's trade: bring a large
jump to them rather than rejecting the iteration. For numba, *Numba*, above,
says what invalidates the cache and what forces a second compile.

## Track the objective across a range of inputs

At the end of every run, time the same function on several synthetic
datasets that span what the package actually receives, each recorded as an
**observer**, not a gate. The search optimizes one input size; these
observers show what each change did to small, large and irregular inputs. A
change that wins at the benchmark size and loses 3x on a single-element
input shows up here and nowhere else.

**Mine the unit tests for the cases**: they hold the inputs the package's authors
worried about. Read `tests/` for fixtures, parametrize lists and regression
cases, and turn each class into a generated dataset:

| Class | What it covers | Example: comparing queries with targets |
|---|---|---|
| tiny | the smallest input the API accepts | one query, one short target |
| much larger | several times the benchmark along each axis that scales: 10x for a linear algorithm, less when the baseline is super-linear | 10x the queries or the targets |
| irregular | sizes at both extremes, and mismatched shapes | query lengths 1 and 30+, targets shorter than the query |
| degenerate | inputs that create ties or repeated work | all-identical queries, queries of one repeated symbol |

A class the API rejects (an empty target that raises) is a unit test's job,
not a timing: leave it out and say so in `spec.md`.

- Generate the datasets once, before the baseline, from a fixed seed, and
  store them in `autooptimize/reference/`.
- Warm up once per dataset when the code is JIT-compiled: a new dtype or
  number of dimensions is a new numba signature, whose first call includes
  the compile.
- Name each observer `<objective>_<dataset>` (`seconds_tiny`,
  `seconds_wide_inputs`) and put the dataset's shape in `spec.md`.
- Budget it: the suite runs on every run, so its cost multiplies by
  `runs_per_iteration`. Keep the large cases no larger than the question
  needs; setup reports the per-run cost at step 2.
- Put the same identity gate on each dataset's output only if the user
  wants the gate to cover all of them; ask at step 0.

These observers do not replace the external validation
(`references/external-validation.md`), which runs once a round and can be
slow.

## The unit tests are a gate

When the package has tests, pass them to `init --tests`. The suite runs once
per iteration and a failure fails the iteration. Setup, the baseline rule and
the thread pinning a timing search needs are in `references/repo-mode.md`,
*The unit-test gate*.

## What this adds to the interview

Rows for group 4 of `references/step0-interview.md`, *The questions*; record
"algorithm implementation" on `spec.md`'s *Kind of search* line, which makes
`setup --done` require the task's checks.

| Group | Question | Options to offer | Default | Recorded in `spec.md` |
|---|---|---|---|---|
| 4 | Which memory measure is recorded: as the observer when the objective is time, as the objective when it is memory? | `max_rss_mb`, which any program has (Recommended for compiled code); `peak_traced_mb` (Recommended for Python) | the recommended one for the language | Objective, Observers |
| 4 | At what thread count is the objective timed? This is also group 2's worker count: ask once | the count users run; 1 | none: always asked, since the code's own default is never taken | Thread counts timed, tracking inputs; `init --workers` |
| 4 | Track the synthetic dataset classes above, and gate their outputs too? | the classes proposed from the tests (Recommended); fewer; gate them as well | observe, do not gate | Thread counts timed, tracking inputs |
| 4 | Observe a second thread count? | yes, where the code has a parallel path (Recommended); no | yes, where it applies | Observers; Thread counts timed, tracking inputs |

The unit-test command and whether the baseline passes it belong to repository
mode (`references/repo-mode.md`), or to `init --tests` in plain mode.

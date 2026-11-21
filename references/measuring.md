# Telling whether a change actually helped

## The runner's median is the number

`run_iteration.py`'s median, over separate processes, is the result.
Anything measured inside one process is for **ranking variants**
(`references/subagent.md`, *Timing variants*), never for claiming a gain.

Say which produced each figure. A sweep's delta can be several times the
runner's, and only the runner's number is in `result.json`: quoting the two
as one measurement leaves a frontier nobody can reproduce.

## Paired separate processes: `paired.py`

A search pairs once, at the end: `autoopt.py final` times the winner
against A0 on the task's timing probe, `checks.time`
(`references/step4-final.md`). No iteration is paired with its parent. Late
in a search a gain is smaller than the runner's noise band, and in-process
sweeps can split two identical copies; pairs of separate processes resolve
it.

| It does | Why |
|---|---|
| a fresh process per timing, alternating which side runs first | the side run first in a pair is slower, so a fixed order hands the second a win |
| a compiler cache per side, warmed before the first pair | no pair times a compile, and neither side loads the other's code |
| stops on an exact sign test, checked after every pair from `--min-pairs` | a unanimous result stops at 9 pairs and a close call runs to `--max-pairs`. Looking after every pair raises the false-positive rate above `--alpha`: 1.2% at the defaults, under no real difference (`paired.py --help` gives the simulation) |
| aborts on a non-zero exit | the probe exits non-zero when its output differs from the reference, so a wrong variant is never timed as a fast one |

The probe (`checks/time_one.py`) imports the package from `{src}`, asserts
the import path (`sys.modules['<pkg>.<module>'].__file__`), loads the cached
inputs (`references/step2-baseline.md`, *Parse the inputs once*), warms up
at full size, times one call, compares its output with the reference bytes,
and prints the seconds as the last number on its last line. `final` runs it
again at `checks.threads`, with every common thread-pool variable set to it.

Report the verdict, the median paired difference and the count, labeled
"paired", beside the runner's median, which stays the number in
`result.json`: "paired: B better, −0.037 s, 9/9, p = 0.0039".

A subagent choosing between two of its own variants can pair them the same
way, on two exported trees: `paired.py --a $DIR/probe/v1 --b $DIR/probe/v2
--cmd "python3 $DIR/probe/time_one.py --src {src}"` (exporting:
`references/subagent.md`, *Rules*).

## Profile the root you are actually on

Shares move every time something lands: a profile from a few roots back can
name a kernel that no longer dominates, and every brief written from it aims
at the wrong place. With `checks.profile`, `promote` writes each new root's
`iterations/<id>/profile.txt`, and a brief quotes the lines naming its idea's
function: read that before profiling by hand.

Three traps, all silent:

- **A profiler written against the baseline segfaults on a later root.** If
  it allocates scratch in the baseline's shapes and calls the kernels
  directly, a root that changed a workspace shape makes it write out of
  bounds, and numba does not bounds-check. Mark such a script
  `BASELINE ONLY` and profile from inside the current root's own top-level
  function instead.
- **Timing a loop nest's parts separately can cost more than the whole, and
  the parts then sum to a lie.** Split into passes, the loops re-read scratch
  from memory that the fused version keeps in cache, and a decomposition
  validated on one root stops being valid when a layout changes under it.
  Price one part by replacing a **single call** inside an otherwise
  verbatim copy of the whole function, and re-validate the decomposition
  against an unsplit control every time you reuse it.
- **Cumulative truncation can be invalid and silent.** Timing a prefix of a
  kernel to attribute cost can give *every* prefix as slower than the whole
  kernel while a verbatim copy compiled in the probe module times correctly:
  the compile is faithful and the decomposition is not. Time the sub-kernel
  directly.

## Reading the generated code

Asm is evidence about what to try, never proof that a change worked: a change
with identical instruction counts can be a regression. For numba:

```python
kernel(out, x)                                  # compile for the real argument types first
asm = kernel.inspect_asm(kernel.signatures[0])
lines = asm.splitlines()
print(len(lines), 'lines', sum('%zmm' in l for l in lines), 'zmm',
	sum('%ymm' in l for l in lines), 'ymm')
```

- **Dump with a fresh `NUMBA_CACHE_DIR`.** A function loaded from the cache
  returns a stub.
- **Arguments of the wrong dimensionality return a stub of about 45 lines
  with every count at 0, on both versions, without raising.** Check the line
  count before believing "0 before, 0 after".
- **A callee that is not inlined is compiled on its own**: dump the callee to
  see the code that runs.
- **What to count:** lines with `%zmm` or `%ymm` show how much of the work
  became wide; scalar `vmulsd` or `vaddsd` in a hot loop means it did not
  vectorize; a vectorized float `max` or `min` shows as `vcmp*pd` with a
  blend or masked move, not `vmaxpd`, because numba keeps NaN semantics.
- **A loop compiles to several bodies, each behind a minimum trip count**
  (`cmpq $N` before it). Read each guard against the real trip counts: the
  widest body can sit behind a guard the data never reaches.

## A cached compile can outlive the edit

For numba's `cache=True`, Triton, and `torch.compile`/Inductor.

numba's `cache=True` writes a compiled library keyed on the **decorated
function's own source file**. A callee's code is linked into its caller's
library, so editing only a callee in another file leaves the key unchanged
and the stale library is served. Without `NUMBA_CACHE_DIR` the cache is
`__pycache__/` beside the source, inside the worktree, so it survives from a
run to the edit after it.

It bites hardest when every kernel under search is a callee of one
`@njit(parallel=True, cache=True)` driver and each iteration edits a callee
alone, and it can segfault. **The symptom is a variant that measures
exactly at its control**; `inspect_asm()` on a stale library shows the old
instructions.

Triton (`@triton.jit`, `@triton.autotune(cache_results=True)` or
`TRITON_CACHE_AUTOTUNING=1`) and Inductor key their caches on the kernel's
source and its dependencies, so an edit is not served stale. Their defaults,
`~/.triton/cache` and `/tmp/torchinductor_<user>`, are shared by every process
on the machine: an iteration can load a compile or an **autotuning choice**
another iteration or session timed under its own load.

**Point every compiler cache at `{cache}` in `command` and `--tests`:**

```
NUMBA_CACHE_DIR={cache}/numba TRITON_CACHE_DIR={cache}/triton TORCHINDUCTOR_CACHE_DIR={cache}/inductor python3 bench.py ...
```

`{cache}` is the absolute path `iterations/<id>/cache/<first 12 characters
of the commit>`, the same for the runs and the tests. The runner commits
before any run, so the runs of one commit share compiled kernels, and an
edit after a run gets a new commit and an empty cache. Only the first
run compiles and autotunes, so every seed uses that run's autotuning choice:
one sample, made in whatever state the machine was in. Inductor's
`max-autotune` choices are cached on disk by default; a plain
`@triton.autotune` without `cache_results` keeps its choice in memory, so
each run autotunes afresh in its own process. `prune` deletes `cache/`
outside its protected set, and it is never committed.

**A cache removes the compile that was warming the process.** A long numba
compile leaves the allocator's pages mapped and the clocks up, so a small
warmup suffices. Loaded from a cache, the same code's timed call can run
slower, enough to make an iteration read short of its prediction; a warmup
call at full size brings the gap inside the noise. **Warm up at full size**,
and never compare medians across a switch to or from a shared cache without
re-measuring the root. `scripts/test_runner.py`, *shared compiler cache*,
reproduces the numba case, with a control showing that a cache kept across
the edit is stale.

For a hand-timed sweep or an asm dump, give each variant its own new
`NUMBA_CACHE_DIR` inside the iteration directory. Never delete a cache to
force a recompile; `references/subagent.md`, *Rules*.

A compiler cache is one case of a general rule: anything long-lived holds a
copy of your code and will serve the old one.

## A suspiciously large speedup is usually dead code

An edit that loses its write-backs lets the compiler delete the loop being
timed: a variant reads much faster while millions of elements mismatch.
Check the gate before believing a number, and be most suspicious when the
number is best. Inverted, such a bogus speedup is a real ceiling for
*removing* the work entirely, which tells the next iteration what is
available.

## A harness error is not a result

Before a mismatch, a failure or a slow run is attributed to the code, and
before any count is written into a summary or a report, scan every
stderr for errors the harness itself raised: a process that never started,
a timeout the runner imposed, a missing input, an illegal combination of
options. One unstarted run otherwise reads as an output
difference. Give those their own outcome category and re-run them once
fixed; never count them as `mismatch` or as the program's failure.

## Properties no gate can see

A gate compares outputs, so a change that alters the output only on inputs
the benchmark does not use is invisible to it. The subagent checks it
directly:

| Invisible to the gate | How to check it |
|---|---|
| `int16` overflow, since integer reassociation is exact mod 2^16 | an instrumented build carrying an `int64` shadow over the full data; report the range every value stays in |
| whether a skipped region really is all zeros | replay the loop bounds over every distinct shape pair the data produces |
| whether `min`/`max` reassociation is bitwise free | scan every value for NaN, inf and `-0.0` |
| an entry point the compare does not call | compile-check it and run it separately (`references/subagent.md`, *Off-benchmark callers of the code you are changing*) |

Name the premise the change rests on, then measure it over the real data
rather than asserting it.

## Break the check before believing it

**A shadow check that re-narrows before comparing cannot fail.** An `int64`
shadow carried alongside an `int16` accumulator and compared as
`numpy.int16(shadow[k]) != t_sums[k]` wraps the shadow back into the type
whose overflow it exists to detect, and reports 0 disagreements even for a
value forced past 32,767. Compare the wide value raw. The same holds for
anything that clamps, saturates or rounds on the way to the comparison: a
survival probability clamped by `b if b > 0 else 0.0` swallows NaN, so a NaN
count downstream of it cannot fail either.

Show every verification failing once: corrupt the checker's input on
purpose (a skip mask, say), or run an unrelated branch as a negative control
to prove a table of assembly-instruction counts can differ.

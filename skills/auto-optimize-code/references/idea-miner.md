# The idea miner: reviewing the code for ideas

You read code and write ideas. You edit no code, run no benchmark and time
nothing: the baseline or the iterations are timing on this machine while you
work. Write no code to test an idea either: the iteration that takes it
measures the speed and checks the output, so *Gain* and *Exact* are your
reading of the code. Do not read the skill's scripts or probe the machine;
the setup subagent proves the harness at step 2. Your prompt (`autoopt.py
mine`) gives the paths, your stage `S<n>`, the code to review, the spec's
rules and the categories to use. A term you do not know is in
`references/glossary.md`; read it for that only.

## Read first

Your first turn reads your prompt, this file, `knowledge.md` and `ideas.md`
together, as your prompt lists.

1. `knowledge.md`: settled facts and `[closed]` ideas. Propose nothing it
   closed. Take *Gain* from your prompt's *The profile of* section, the root
   you review (or its nearest ancestor with one), and from the ledger's
   `[code:<subsystem>]` entries only without it.
2. `ideas.md`: what is queued or tried. Repeat nothing; a variant of a tried
   idea needs a reason the first attempt did not cover.
3. From stage S2 on, your prompt's *Settled since the last stage*: what became
   of each iteration, and its README's *What to try next*, which becomes
   ideas. Read a README's *Notes and failures* only for an idea you build
   on.
4. The code, read only, starting from what one run calls: your prompt's
   *What one run does* line, and the harness (repository mode:
   `autooptimize/harness/`; plain mode: the benchmark in the baseline's
   directory) only for what the line leaves out. At S1 the benchmark may not
   exist yet: start from the function the spec's task names. Your prompt's
   *The code* lists each source file with its line count, and each Python
   function with its line, the profile's files first: go from it straight to
   `sed -n` ranges, not to `ls`, `find` or whole files. Follow the calls the objective's time is spent in.
   Read with `grep -n` and `sed -n` ranges, not whole large files.

## The categories

| Category | Look for |
|---|---|
| `MICRO` | a small local change with a large effect: a hoisted invariant, a removed copy or conversion, a cheaper operation in a hot loop |
| `NUMBA` | multi-step Python or numpy code (several passes, temporaries, a Python loop) that one custom numba function does in a single pass. In compiled code, file the same fusion under `MICRO` or `REORGANIZATION` |
| `CACHING` | an intermediate result computed more than once, or on every call, that can be stored and reused later |
| `CACHE_MISS` | operations reordered to avoid cache misses: strided or scattered access, a loop order against the memory layout, an array of structs where a struct of arrays fits, a working set past L2 |
| `PRECISION` | a wider dtype than the values need: `int64` for counts under 128 fits `int8`, `float64` for an approximate quantity |
| `PARALLEL` | a serial loop or map whose steps are independent, which threads or processes can split with little change |
| `REORGANIZATION` | a broad restructuring that enables caching or parallelism the current shape blocks: batching calls, splitting a pass, a data structure changed across modules |
| `IO` | time in reading, writing, parsing, decompressing or formatting: fewer passes over a file, larger or batched reads and writes, a parser that skips fields the call never uses, a cheaper intermediate format, memory mapping, overlapping I/O with compute |
| `ALGORITHM` | a different algorithm or data structure for a step, with less work asymptotically or in the constant (a sort, a search, a lookup, an index, a pruned search space, an approximation that keeps the output), more than a local tweak |
| `OPTIMIZATION` | any other optimization |

`MISC` is a change that is not an optimization (a fix); the miner files none under it.

## What makes an idea usable

- **It can pass the gates.** Under a bitwise gate, a lower precision or a
  reordered floating-point reduction changes the output: write that under
  *Exact*, and drop an idea the gate rules out unless a guard with a fallback
  keeps the output (say so). Respect *Off limits* and *Portability*.
- **It works on any OS, compiler and CPU.** Propose nothing that needs one: a compiler-specific flag or pragma, `-march=native` or an intrinsic, a syscall or `/proc` read, a size or thread count fit to this machine, a GPU-generation feature; even where *Portability* names this machine's OS only. A run-time choice with an identical-output fallback is allowed.
- **It is not toolchain-specific.** No change whose gain depends on a compiler's decisions: a hint, pragma, flag, LTO or source order that steers inlining, or a gain explained by a compiler or pass; hints on a new function are allowed, even where *Off limits* lists compiler flags (`references/subagent.md`, *Portability*, Toolchain).
- **It vectorizes portably** (`references/subagent.md`, *Portability*, Vectorizing): numba in Python, else restructure for the compiler; never raw intrinsics.
- **It names its place**: `file:function`, or a line range.
- **Gain comes from the profile** as the share of the call the idea could
  remove, or reads "unmeasured". Never invent a number. A function the
  profile does not name is probably not on the timed path: say so in the
  line, or drop it.
- **One change per idea.** Changes that each stand alone are separate lines;
  four settings of one parameter are one idea, swept by one iteration.
- **Most promising first**: expected gain, times the chance it holds, against
  the work.

## Writing them down

1. Write your stage file (its path is in your prompt): the idea lines, in
   `ideas.md`'s form, then a `## Notes` section with anything longer an idea
   needs (a sketch, the evidence, the lines read):

   ```
   - [CATEGORY] where (file:function): what to change. Why: why it should help. Gain: the share of the call it could remove, or unmeasured. Exact: bit-identical, within tolerance, or changes outputs. (S<n>)
   ```

2. A fact you established while reading (where the time goes, a caller the
   benchmark never exercises) is a ledger entry, with `(S<n>, root <label>)`
   in place of an iteration's label: write the entries, one a line, to the
   entries file your prompt names (`mining/ledger-S<n>.txt`).
3. Close the stage: `python3 $AO/autoopt.py mine $WORK --done`. It appends
   the stage file's idea lines to `ideas.md` and the entries file to
   `knowledge.md`, each under its lock; the notes stay in the stage file. It
   refuses, with nothing written, an idea under no known category or a
   correction that matches no entry; fix the file and call it again, since
   nothing is appended twice.

## What to report back

Exactly this, and nothing more:

1. The number of ideas per category.
2. The three you would brief first, one line each.
3. At most two sentences: a ledger entry or README premise you found false
   first, then anything else the orchestrator must know.

If writing a file was refused, return the idea lines in your report instead:
the main session writes them to the stage's file and closes the stage (`mine
--done`).

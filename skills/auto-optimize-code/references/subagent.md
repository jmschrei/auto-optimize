# Running one iteration: the subagent's protocol

You implement **one** idea, measure it, write it up, and stop.

Your brief gives every path absolute: `AO` (the scripts), `WORK` (holds
`autooptimize/`), `DIR` (your records) and `SRC` (your worktree: `DIR` in
plain mode, `DIR/src` in repository mode). The rules it quotes from `spec.md`
bind you as much as this file does. A term you do not know is in
`references/glossary.md`; read it for that only.

**GPUs.** Your brief's GPU line is one of two. *An allocator command*: run
`run_iteration.py` bare, it takes and waits for the lease itself, and put the
allocator's words before every other command that runs the code (`paired.py`,
`cold.py`, `checkers`, `validate`, your probes). *Indices*: prefix every
command that runs the code with `CUDA_VISIBLE_DEVICES=<them>`. Never read
`nvidia-smi` and pick a device yourself (`references/step2-baseline.md`, *GPU
leases*).

## The sequence

**1. Your first turn** makes the calls your prompt lists, together: it
reads your brief, this file, the ledger (`knowledge.md`) and the `tools/` and
`tools/checkers/` READMEs, and runs `new --brief`, which prints `$DIR` and
creates a git worktree on branch `autoopt/<id>` holding exactly the code the
parent ran. A premise the ledger settled is not measured again; a helper or
checker in `tools/` is reused, not rewritten.

**2. Implement the idea.** Edit code only in `$SRC`, after reading the
parent's code, which differs from the baseline once anything is promoted. What
one run calls, with which inputs, is your brief's *What one run does* line. In
repository mode the benchmark is `harness/`, which no iteration edits, and the
brief lists its files and functions, which a probe or checker may import: read
the harness only for what those leave out. For code compiled by numba, read
`references/algorithm.md`, *Numba*, first.

Change **one thing**. If the idea needs three changes to work at all, it is
one idea; if it splits into three that each stand alone, it is three
iterations: implement the first and say so in your report (*What to report
back*).

Keep the benchmark's contract intact: it still takes `--seed` and `--out`,
and still writes `metrics.json` with the objective, every gate and every
observer. An idea that changes what the objective means is a different
search.

**3. Run the iteration.** Your brief says whether the runner goes in the
foreground or the background:

```bash
python3 $AO/run_iteration.py $DIR    # foreground, timeout: 240000

python3 $AO/run_iteration.py $DIR --background   # writes probe/runner.log
python3 $AO/autoopt.py wait $DIR/probe/runner.log                  # repeat until `wait: finished`
```

The runner commits your edit and runs the stages in *Checks beyond the
runner*, each only when the ones before it passed: one run, then the unit
tests and the compare, then the other runs. A broken edit therefore costs one
run. It writes `result.json` and `report.txt`, whose `run <seed>`
rows give each run's objective and observers beside your parent's run of the
same seed, any gate it broke, and the load when it started: read those rows,
not the runs' `metrics.json`. Do not loop over seeds yourself. After you fix
the code, run it again: the new commit runs every seed afresh, while seeds
already measured on an unchanged commit are reused (`--rerun` runs them
again).

**When `"tests"` is configured**, the runner runs the unit tests once, after
the first run passes its gates. A failure is `gate-failed`. Read
`$DIR/unit-tests/log.txt` and fix the code, never the test: editing or
deselecting a test changes what the gate checks. If the idea changes behavior
a test pins, stop and report that.

**4. Run the checks your brief asks for** (*Checks beyond the runner*).

**5. Write the README** (*The README*), append what you established to the
ledger (*Before you stop*), and report back.

## Checks beyond the runner

**The runner's stages**, each run only when the ones before it passed; with
the task's checks (your brief's *Checks* section says so) it prints a
`check:` line:

| Stage | Runs | A failure |
|---|---|---|
| 0. before the runs | with compiled code (`first_call`), the cold first call on empty caches, beside your parent's recorded value | a crash is `error`, and nothing runs |
| 1. the first run | one seed, every gate and observer | `error` or `gate-failed`, and nothing more runs |
| 2. the code's checks | the unit tests (with `{selected}`, only the files for the modules you changed), then the compare: your code against A0's cached outputs, on the benchmark's inputs, the edge cases and every public function | `gate-failed` (`failed_check: compare` for the compare) |
| 3. the other runs | the rest of `runs_per_iteration`, one seed each, every gate and observer on every run; on one GPU or none they run one at a time and stop at the first that fails | `error` or `gate-failed` |
| 4. the whole suite | with `{selected}`, only when your median passes the keep rule: only such an iteration can be promoted | `gate-failed` |
| 5. the keep rule | the verdict against your parent, your median beyond the noise band of your parent's, recorded as `keep` and on the `keep:` line | none: a `keep: no` is a result |
| 6. promotion | with `promote: auto`: a pass while your parent is still the root promotes you, then runs the closed round's approved validation | none. You promote nothing |

**Do not time your version against the parent yourself** (`paired.py`,
`cold.py` or a probe of your own): the runner's `check:` and `keep:` lines are
the verdict. Timing variants to choose between implementations before the
runner runs is yours (*Timing variants*). Your own checks:

| Check | Yours |
|---|---|
| forced-fallback and guard-breaking checks | for a path your change adds that the compare's inputs cannot reach (a fallback, a guard); in a search without the task's checks, for every path it adds |
| external validation | when the brief asks |

**A checker in `tools/checkers/` that guards a file you change** is run, not
read: `python3 $AO/autoopt.py checkers $WORK --id A7 --touched` runs every
one whose `files` your edit touches, on your parent and your code, and says
which passed (`--name <dir>` runs one; with no flag it lists them). Start it
in the background with `wait` when a checker's run time passes four
minutes. Read a checker's `ROW.md` only for what its index line leaves out.

A compare that fails is a result, like a gate: read `check/compare.log` in
`$DIR`, fix the code, and `--rerun`. Never edit the checks or the cached
outputs in `reference/`, which are off limits like the harness.

**Run your checks together, in the background, never while you time
variants**: a check beside a timing takes its cores and voids it.

```bash
(python3 $AO/autoopt.py validate $WORK --id A7 --out $DIR/validation > $DIR/validation.log 2>&1 &
 python3 $DIR/probe/fallbacks.py > $DIR/probe/fallbacks.log 2>&1 &
 wait; echo "exit $?") > $DIR/probe/checks.log 2>&1 &
python3 $AO/autoopt.py wait $DIR/probe/checks.log
```

**Prove a checker you wrote on planted faults in one call.** Write each fault
to `$DIR/probe/faults.txt`, the old text copied exactly:

```
## nomirror
file: textstats/distance.py
<<<
        G[start:end, :start] = G[:start, start:end].T
===
        pass
>>>
```

```bash
python3 $AO/autoopt.py prove $WORK --id A7 --checker $DIR/probe/guards \
	--checker-row $DIR/probe/checker.txt --faults $DIR/probe/faults.txt
```

It runs the checker's `run` line on your edit against the parent, which must
pass, then on a copy of your edit per fault (refusing one whose old text is
not in the file exactly once), and prints which it caught. `finish` takes the
row's `faults` from that table. A fault it missed goes in the row as missed,
or makes the checker stronger. One run per fault: start it in the background
with `wait` when that may pass four minutes.

## Timing variants

Choosing between implementations before the runner runs is the one timing
that is yours. A sweep inside one process ranks variants; it never measures
a gain, and its delta can be several times the runner's.

- **Rotate the variants' order on every repetition**: a fixed order gives the
  first one a cache-warmth edge, enough for a byte-identical copy to win.
- **Include a byte-identical copy of the parent** as a variant, and claim
  nothing that this control also shows.
- **Rank by wins, not by the mean delta**: report "fastest in 15 of 15".
  Nine rotated repetitions separate most things; run fifteen for a tie. Two
  variants inside each other's noise are a tie: say so and pick either.
- **Compare only within one sweep.** Separately compiled copies land in
  different places in memory, so one copy reads faster, level and slower
  across three sweeps.
- **Never time A once, then B**: drift between the two can exceed the change.
  Interleave, and report each repetition.

Before pricing the parts of the root (`references/measuring.md`, *Profile the
root you are actually on*), reading generated code (*Reading the generated
code*) or timing code a compiler caches (*A cached compile can outlive the
edit*), read that section of `references/measuring.md`.

## The README

`$DIR/README.md` is the only thing that outlives you: the orchestrator reads
your report once; whoever returns in three months reads this file. You write
its prose in `$DIR/notes.md`; `autoopt.py finish` (*Before you stop*) adds
the header and every number from `result.json`, so none is typed by hand.

`notes.md` holds these four `## ` sections, non-empty. Any others you add (a
*Variants* section, say) follow the numbers in the order written:

| Section | Holds |
|---|---|
| *The idea* | what was changed, where (file and function), and why it might help |
| *What happened* | the result in words |
| *Notes and failures* | anything surprising: a seed that differed, a dependency pinned, a setting changed to make the idea run at all, a false premise in the brief, the share of a tolerance this spent |
| *What to try next* | the follow-ups this result suggests, or "closed" and the number that closes it |

```markdown
## The idea

Prefetch the next block of records on a second thread in `scan.py:read_blocks`,
so the parser never waits on the disk: the hope was the same throughput at a
lower per-call latency.

## What happened

Throughput rose, the best of the round, but every run broke the 10 ms latency
gate: the prefetch thread starts cold on each call, and a short call pays its
startup. Per record it is cheaper; per call it is not.

## Notes and failures

- The first attempt used a queue of depth 8 and was 3 ms slower again; the
  measured version has depth 2.

## What to try next

A prefetch thread kept alive across calls, or prefetching only for inputs
past one block, where the startup is amortised.
```

**When the runner's `keep:` line reads `promoted` or `passes`, in a repository
search, add two more sections; `finish` refuses the README without them.** They
are the text published with your change, so nobody has to read your diff again
to describe it. An iteration made with `--after-end` writes them before
`promote`. `finish` and `promote` lint both (`references/promotion.md`, *The
preflight*): name nothing of the search (no winner, baseline, round, label such
as A12, or path on this machine), and call the original code by its branch
name.

| Section | Holds |
|---|---|
| *Change* | first line `name: <three to six words naming the change>`; then what the change does as it stands in the code, in complete sentences, with no number a table computes |
| *Commit message* | the commit's message: an imperative subject of at most 72 characters with no closing period, a blank line, then the body: what this change does and what stays the same (output, defaults, the plain build), wrapped at 72 columns, in the repository's own style (`git -C $WORK/autooptimize/repo.git log --oneline -20`). Leave out the `after.commit_trailer` line here, though the commits you make in the worktree end with it: step 5 adds it when it makes the commits the user ships (`references/pr-optimization.md`) |

```markdown
## Change

name: Reuse the parsed header across records
The record parser parses a block's header once and passes it to each record,
where it parsed the header again for every record.

## Commit message

Parse a block's header once

parse_record() took the raw header and parsed it for each record of the
block. read_block() parses it once and hands the result down.
```

The README's *Numbers*
section gives the per-seed table, the median and its range, the frontier and
noise band it was judged against, and each gate's worst value. The median is
the iteration's result; quoting the best single run (`best_run`) overstates
the idea by about the seed spread. A re-run re-assembles `README.md` from the same
`notes.md`.

An iteration whose README says only "did not help" is an iteration somebody
will run again: a negative result is welcome, with its number.

## Before you stop

Write up to three files, then make one call:

- `$DIR/notes.md` (*The README*);
- `$DIR/probe/ledger.txt`: what you established, and a correction for
  anything you disproved, one entry a line in the ledger's format (`- [tag]
  fact (label, root <label>)`); a correction ends `(corrects: <words only the
  old entry has>)`;
- for a checker you wrote (`references/shared-knowledge.md`, *The checker
  library*), `$DIR/probe/checker.txt`, one `key: value` line each: `name` (the
  subsystem, such as `distance`), `files` (the files it guards, relative to
  the code's root, comma-separated; a directory or a glob covers what is
  under it), `compares` (its first sentence is the index line), `built`,
  `run` (a shell
  command, run in `$DIR`, so `&&` and pipes work, with `{checker}` for its
  directory and `{parent}` and `{child}` for the two trees, substituted
  shell-quoted: the two worktrees, or, when the search registers
  `checks.build`, copies built with it), `faults` (each planted fault and
  what it caught; left out after `prove --id`), `inputs`.

```bash
python3 $AO/autoopt.py finish $WORK --id A7 --ledger $DIR/probe/ledger.txt \
	--checker $DIR/probe/guards --checker-row $DIR/probe/checker.txt
```

It checks everything before it writes anything, and refuses, naming what to
fix, when there is no `result.json` or no `notes.md`, a placeholder is left,
a required section is missing, empty or twice, text comes before the first
`## ` heading, a correction's words match no entry or several, the row lacks
a key, or the checker does not exit 0 on your parent and your code (its log
is `probe/finish-checker.log`). Then it writes `README.md`, strikes the
`ideas.md` line you were briefed from, copies the checker into
`tools/checkers/<name>-<label>/` with its row and its measured run time,
appends the entries under the ledger's lock, and prints the block to report.
After a fix, make the same call: nothing is appended twice. A checker that
cannot finish in four minutes gives `time:` (its run time) in place of
`run:`. If writing a `.md` file is refused, your notes and entries go in your
report (*What to report back*).

## Check your brief before you follow it

Your brief was written from a profile of another root and a memory of what
was tried, so check its premises before relying on them. When the search profiles its roots, *The idea*
quotes your parent's profile on the idea's function; one that says the
profile does not name it is the first premise to check, before building
anything. Premises that turned out false:

| The brief said | It was |
|---|---|
| "this argument is always −1, so the branch is dead" | false: another entry point passes a real index, and deleting it would have passed every run here and broken the package |
| "each `(k, l)` writes a distinct target, so reordering is safe" | false: it is a convolution, up to 100 additions land on one element, and the change was exact only because it kept the loop order |
| "this clear is a large memset, worth attacking" | a small fraction of that; the buffer never left L1 |
| "this is worth X on top of the root" | well short of X: the headline was a variant's, and the parent already had part of it |

**When a brief states a premise, measure it**, and **put a correction first
among your report's sentences** (*What to report back*, item 2): a premise
wrong in a brief is usually wrong in `ideas.md` and the ledger too. Correct
the ledger entry yourself (*Before you stop*); the orchestrator fixes
`ideas.md`.

## Off-benchmark callers of the code you are changing

A package usually has more than one entry point into the kernels you edit,
and the benchmark exercises one. **A missed update passes every run here and
breaks the installed package**, at import or compile time, on a path this search
never runs. Find the other callers before changing a shared function's
signature or an array's shape, and compile-check them afterwards.

If such an entry point is **nondeterministic on unmodified code**, such as one
returning uninitialized scratch that differs between processes, comparing its
output between your branch and the parent proves nothing. Say so, check only
that it compiles and returns the right shape, and record the bug as a
`[bug]` ledger entry rather than spending your iteration fixing it.

## Portability

Your brief quotes `spec.md`'s *Portability* line. **By default the change must
work on every common OS: Linux, macOS and Windows.** Code that relies on one OS
(reading `/proc`, `os.sched_setaffinity`, the `fork` start method, `madvise`,
a POSIX-only call, a shell command) is allowed only behind a check that falls
back, on the other systems, to a path with identical output; that fallback is
a path the change adds, so it gets the forced-fallback check (*Checks beyond
the runner*). When the line names this machine's OS only, such code is
allowed as it is. Either way, say in the README which OS-specific code the
change uses.

**Vectorizing.** Python: numba. Otherwise restructure the code first so the
compiler vectorizes it (contiguous arrays, a struct of arrays, no aliasing,
simple loops). Only then use the language's own general-purpose route to SIMD,
one every compiler or runtime for it accepts (a standard pragma, a portable
vector type). Never raw intrinsics, `-march=native` or a flag for one
instruction set.

**Toolchain.** The change must not depend on a compiler's decisions. Before
`notes.md`, ask whether the gain exists because of how one compiler, version,
flag or optimization level treats the code: an attribute, pragma, inline hint,
build flag or source order that steers it, or a cause that names a compiler or
pass. Pragmas and attributes on a new function you add are allowed. Start a
`## Toolchain` section with `Relies on a compiler decision: no` or `yes`, then
the mechanism that makes the change faster. `yes` rejects the idea: `finish`
records it, `promote` refuses it, and the ledger closes it (`[closed]`, the
compiler named). `finish` also scans the diff against the parent and refuses
without the section when an existing function's hint or pragma changed (or a
lone one was added) or a build flag changed; `promote` refuses until the
answer is `no`. With `promote: auto` the runner then prints `keep: passes (…);
not promoted: <reason>`: fix what the reason names, run `finish`, and put that
line first in item 2 of your report; the orchestrator promotes it (`promote
--id`). The scan also flags every build
file (Makefile and `*.inc`, `CMakePresets.json`, `build.sh`, `config.toml`, an
`include` of a new file), a compiler or flag variable set in one, an optimize
or target pragma or attribute wherever it sits, a new assembly file, a binary
or library, and a file under `vendor/` or `third_party/`. The runner scrubs
`CC`, `CFLAGS` and the other build variables from every run and test and sets
those in `config.json` `build_env`, so exporting one changes nothing. The
harness is hashed when step 2 closes; a run refuses once it differs
(`autoopt.py harness`). A new binary is flagged by content, not suffix, so a
build's output committed by accident is refused too: ignore build outputs in
the repository's `.gitignore`. A changed build file, a binary or library, a
vendored tree and an assembly file are refused whatever the answer is, unless
`config --set allow_build_changes=yes` (tuning the build is the task).

**Constants fit to this machine.** A number you chose by timing here (a block
or tile size, a thread count, a buffer length, a prefetch distance, the cutoff
between two code paths) is derived at run time from the input or a query of the
machine, or the README gives its value, the sizes and core counts you timed
and what the other values did. A value timed at one size only is a guess on
another machine: time a second size before you keep it.

## Keep the branch to the code change

`git diff <parent>..<yours>` should be the idea and nothing else. Probes,
sweeps, asm dumps and scratch variants go in `$DIR/probe/`, which git never
commits: the plain-mode exclude list ignores `probe/`, and in repository mode
`$DIR` is outside the worktree. Anything written elsewhere in a plain-mode
worktree is committed by the runner. Do not edit `repo.git/info/exclude`; it
is every iteration's ignore list at once.

## When a run fails

`status: error` means a run crashed, timed out, or wrote no usable
`metrics.json`. The runner names the seed and the reason, and the run's
`log.txt` is in its directory. Two errors come from the bookkeeping:
`<dir> has no meta.json` means the directory was made by hand instead of by
`new`; `iteration N has no result.json` means the iteration never finished,
and re-running `run_iteration.py` on it resumes from the seeds already on
disk.

| Cause | Do |
|---|---|
| A bug in your edit | fix it and `--rerun`; the iteration is not measured until it runs |
| Out of memory | if the idea is inherently larger, that is the result: record it; if another job contended, re-run |
| A missing dependency | install nothing: every iteration shares one environment, and an install can upgrade what the others run. Write the README, name the package in report item 2, and stop. An idea that adds a dependency to the package itself also declares it in the diff (`pyproject.toml`, `requirements.txt`) |
| The idea cannot work at all | stop, write the README saying why, report back |

**Never weaken the idea to make a gate pass.** Shrinking the prefetch buffer
until it fits the latency gate turns your iteration into a different idea with
the same title. A reduced form that fits is a separate iteration with its own id and
title.

## What to report back

Exactly this, in this order, and nothing more:

1. The block `finish` printed (`$DIR/report.txt`).
2. At most two sentences: a false premise in the brief first, then an idea
   that should be split, then anything else surprising.
3. Only when writing a `.md` file was refused: your `notes.md` text and your
   ledger entries, so the orchestrator can save them.

A number not in `result.json` is not a result; everything else goes in your
README.

## Rules

- **Use `wait` for anything over four minutes** (`SKILL.md`): the runner when
  your brief says so, a sweep, a long probe:

  ```bash
  (python3 $DIR/probe/sweep.py; echo "exit $?") > $DIR/probe/sweep.log 2>&1 &
  python3 $AO/autoopt.py wait $DIR/probe/sweep.log
  ```
- Work only inside `$DIR`, apart from appending to `knowledge.md` and adding
  to `tools/`. Never edit another iteration, `config.json`, `state.json`, or
  the parent's worktree.
- Scratch (exported copies, sweeps, caches, probes) goes in a new directory
  under `$DIR/probe/`, never under `/tmp`, which the OS may clean mid-run;
  the search's one shared compiler cache in `tools/` is the exception.
- Export code rather than copying the worktree, which carries a `.git`
  pointer back into `repo.git`: `python3 $AO/autoopt.py variant $WORK --id A7
  v1` writes your edit as it stands (before the runner commits, `HEAD` is the
  parent's code) to `$DIR/probe/v1/`, and `--parent` the parent's code. In a
  search with `checks.build` it also builds the copy as the harness builds a
  worktree (log `probe/v1.build.log`), so no build flags need finding.
- **Never search the filesystem** with `find /` or over a home directory:
  on a large or network filesystem such a scan outlives the iteration. Locate a jar or library through the build tool's cache or your
  brief.
- **Delete nothing**: no `rm -rf` on your worktree, scratch copies,
  `__pycache__` or compiler caches, and never on a path built from a
  variable. To force a recompile, point `NUMBA_CACHE_DIR` (or the Triton or
  Inductor cache) at a new directory; to start a copy over, export to a new
  directory. Removing a worktree is `git worktree remove`, and the
  orchestrator's call.
- **Compare outputs by hash, not by saving them.** Hash each output's bytes
  (`hashlib.blake2b(numpy.ascontiguousarray(a).tobytes())`) in the process
  that made it, or compare two outputs in one process. Arrays saved per
  variant and per check soon fill the search directory; save one only to look
  inside it, and only a slice.
- In every probe that imports the code, assert the path before timing:
  `assert sys.modules['<pkg>.<module>'].__file__.startswith(<copy dir>)`.
  Name the submodule: a package that re-exports a function under the same
  name (`from .score import score`) makes `<pkg>.score` the function.
- `runs/`, `result.json`, `README.md`, `notes.md` and saved arrays are
  excluded from git on purpose; do not add them.

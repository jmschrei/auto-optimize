# What one subagent learns, the next one reads

Subagents start cold: unless the search keeps what each learned, every
iteration re-reads the same code, re-measures the same premises and rewrites
the same helpers: the same alternate build, the same side-by-side checker.

`init` starts three shared files under `autooptimize/`, outside every
worktree. None of them is the harness, and none changes a measurement.

| What | Path | Who writes it |
|---|---|---|
| knowledge ledger | `knowledge.md` | every subagent, the setup subagent and the idea miner included (*The knowledge ledger*) |
| shared tools | `tools/`, listed in `tools/README.md` | a subagent (*Shared tools*) |
| checker library | `tools/checkers/<subsystem>-<label>/`, listed in `tools/checkers/README.md` | the subagent that wrote the checker, every time (sources and scripts, not build trees) |

These are the only places a subagent writes outside its iteration directory.

## The knowledge ledger

Append only, so the miner and the iteration subagents cannot clobber each other: each writes
its entries to `$DIR/probe/ledger.txt` and appends them under the ledger's
lock with `python3 $AO/autoopt.py ledger $WORK --by <label> --add
$DIR/probe/ledger.txt` (Linux `flock`). An entry is one to three lines:

```markdown
- [machine] AVX-512 VNNI present; 32 MB L3 per 8-core group (A0, root A0).
- [code:scoring] the inner loop is 71% of the timed call (A0, root A0).
- [closed] Score-only pre-pass refuted: 77% of cases still need the full pass (A1, root A0).
- [code:scoring] the inner loop is 38% of the timed call after A6's hoist (A8, root A7) (corrects: inner loop is 71%)
```

The last entry is a correction: `ledger --add` strikes the one older entry
holding the words after `corrects:` ("- ~~… 71% of the timed call (A0, root
A0)~~ corrected by A8") and appends it. When the words match no entry or
several, it writes nothing and says so.

| Tag | For |
|---|---|
| `machine` | hardware, toolchain, versions |
| `harness` | what the benchmark measures, and how |
| `code:<subsystem>` | facts about one part of the code: costs, shapes, invariants |
| `closed` | ideas refuted or closed, with the number that closed them |
| `bug` | bugs found in the code or the base, not fixed by the search |

Every entry names the label that established it, its number, and the root it
was measured on.

| Who | Does |
|---|---|
| setup subagent, after the baseline | appends the machine, the toolchain, what the harness measures, and a profile of the baseline, with `ledger --by A0` |
| `autoopt.py brief` | points every brief at the ledger; `--sections` names the tags that matter for the idea |
| subagent, first | reads it before reading code or planning; a premise it settles is not measured again |
| subagent, last | appends what it established and a correction for what it disproved, with `finish --ledger`; if writing a `.md` file is refused, returns the entries in its reply |
| orchestrator | checks nothing in it; it saves only the entries a reply returned because writing was refused |

A fact without its root goes stale silently: costs and shares move as the
code changes. A brief that quotes a number from an older root names that
root, so the subagent measures it again (`references/measuring.md`,
*Profile the root you are actually on*).

## Shared tools

A helper goes in `tools/` the second time anyone writes it, or the first time
when the brief or the ledger says later iterations will need it. Usual
candidates:

- the alternate builds the checks need (debug, sanitized, another ISA level,
  CI's variants) and the test run against them;
- a profiler wrapper that knows the platform's pitfalls;
- a cached scratch build (below).

Each tool gets a row in `tools/README.md`, appended under a lock
(`T=$WORK/autooptimize/tools/README.md; flock "$T" -c "cat >> '$T'" <
$DIR/probe/row.md`, Linux): what it does, typical use, and its measured
cost. A tool's interface is documented
there, not only in a docstring.

## The checker library

A checker for a change that must keep outputs identical runs the parent's and
the new code side by side on every call and compares the full result. It is
specific to a subsystem and costly to write, and the next change there needs
it again. `autoopt.py finish --checker` runs it on the parent and the new
code, then copies it into `tools/checkers/<subsystem>-<label>/` with its
full row in `ROW.md` there (the files it guards, what it compares, how it is
built, the `run` line, its planted-fault controls, its inputs and its measured
run time) and one line in `tools/checkers/README.md`, the index every
iteration reads (`references/subagent.md`, *Before you stop*). `autoopt.py
checkers --touched` runs every checker guarding a file an edit changes, so
nobody reads a checker's code to learn how to run it.

## A cached scratch build

When the build compiles every source of a binary in one compiler command
(common in Makefiles), every variant or checker build pays the whole
compile. A tool can take the build's exact commands (`make -n -B <goal>`),
compile each source separately under ccache with the same flags, and link
with the same command, so a one-file change rebuilds in seconds. Pitfalls:

- Point `CCACHE_DIR` at one cache for the whole search (`tools/ccache/`), the
  one scratch area outside `$DIR/probe`; a cache per iteration reuses nothing.
- Set `CCACHE_BASEDIR` to the copied tree and `CCACHE_NOHASHDIR=1`, or `-g`
  makes every directory a cache miss.
- The link command takes the objects before the libraries (`--as-needed` drops
  a library named first) and none of the preprocessor options; a separated
  `-I <dir>` is two tokens and is dropped as a pair.
- `make -n` still runs recipe lines that use `$(MAKE)` or start with `+`.
- Re-read the commands (`make -n -B`) from each tree, so a changed Makefile or
  flag is picked up.

Check that its binaries' outputs are byte-identical to the real build's on a
few cases before anyone uses it, and again after any change to the build
files. It is for subagents' scratch builds only: the runner and the unit-test
gate keep the repository's own build, which is what its users run.

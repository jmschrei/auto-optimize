# Step 1 — a new unit-test suite, before the search

Repository mode only, on a yes to "Do you want to add new unit tests?" at
step 0, with the scope the user chose: the functions being optimized, or the
whole repository. The suite is written before the baseline because every
iteration is gated on it.

The main session spawns one step-1 agent with this file as its brief; "you"
below is that agent. It may split the writing across subagents of its own, and
it reports once: the squashed commit, the bugs found, and every question for
the user together. The main session then runs `init` on the clone.

## Where it lives

Everything step 1 writes is in `$WORK/tests/`: `suite/` (the clone below),
`analysis/` (the reading of the existing tests: what they cover, what they
pin) and `work/` (scripts, coverage inputs and outputs, and `bugs/`). Nothing
there is pruned: step 5 builds the unit-test PR and the bug report from
these folders (`references/pr-tests.md`, `references/issue-bugs.md`).

The suite is written in a clone, which `init --repo` copies: `autoopt/0` is
then the original plus the suite, and every iteration's branch is built on it.

1. **Check the original, clone it and branch**: `python3 $AO/autoopt.py tests-branch <original> --work $WORK`
   (`--date YYYY-MM-DD` to override today). It refuses uncommitted changes, a detached HEAD
   and a branch that is not pushed (origin's commit must equal the branch's; it only
   reads, never fetches), so the user settles those first. It clones into `$WORK/tests/suite`,
   points the clone's `origin` at the original's GitHub remote, not its local path
   (`init` records it as `repo.remote_url`), and branches `auto-optimize/<YYYY-MM-DD>-unit-tests`
   (`references/repo-mode.md`, *Setting it up*, step 1).
2. **Write the suite, run it, and as the last step squash the whole branch
   into one commit** (the rest of this file covers the suite). Commit as often
   as you like while writing; when the suite passes in every configuration
   (*It passes on the base*, below), collapse everything the branch holds
   above the original's commit into one commit, with a message that could be
   posted as it is: `python3 $AO/autoopt.py tests-branch --work $WORK --squash -m '<message>'`
   (it refuses unless exactly one commit is left above `origin/HEAD`).
   `init --repo` refuses a clone on `auto-optimize/<YYYY-MM-DD>-unit-tests`
   that is more than one commit ahead of `origin/HEAD`.
3. **Init on the clone**: `python3 $AO/autoopt.py init $WORK --repo
   $WORK/tests/suite …` (`references/repo-mode.md`, *Setting it up*).
4. **Fill `spec.md`** from the interview (`references/step0-interview.md`,
   *Writing it down*). Its *New unit tests* line records the scope, the clone,
   the branch, and the original's remote URL and branch.

## What it covers

| Scope | Targets |
|---|---|
| the whole repository | every public function, class and method, and every command-line option |
| the optimized functions | those functions and everything they call, from every public entry point that reaches them |

List the targets before writing a test: walk the source for public names
(`ast` for Python, the `cpdef` and `def` names of Cython modules) and the
argument parser for options, and keep the list as a file. A test that only
checks that a call does not raise is not coverage. Hundreds or thousands of
tests are normal for a repository: split the writing across subagents by
module, then have separate reviewer subagents audit the expected values.

## Where expected values come from

Derive them independently wherever practical: inputs small enough to work by
hand, a short reference implementation inside the test, a library function
(`scipy.stats` for probabilities). A test's input file is small, written by
hand or copied into the repository's test data under a license that allows
it: a test that reads a file from outside the repository fails in its CI. A test that only records the current output says so in its
docstring ("pins the current output"), in those words, so they can be
counted. Keep these few, because they also pin the bugs.

## It passes on the base, in every configuration the repository's CI runs

- **No known-bug tests.** A test asserting behavior the base gets wrong is a
  `strict` xfail at best, and many CIs run `pytest --runxfail`, which turns
  every xfail into a failure. Leave such tests out and keep each bug in
  `$WORK/tests/work/bugs/<short-name>/`: what the base does, what it should
  do, a command that reproduces it, and the removed tests. List the bugs in
  your report.
- **No dependence on the platform, the Python version or the build.**
  `ru_maxrss` is bytes on macOS and KiB on Linux; `math` domain-error messages
  changed wording in Python 3.14; how argparse quotes choices in its error
  messages changed between Python 3.12 patch releases; a Cython 3.0 build can
  behave differently from a 3.1 build of the same source. A test that fails in
  one CI configuration is dropped or made to hold in all of them.
- **Optional dependencies skip, never error.** A helper that needs an optional package uses
  `pytest.importorskip`, so a CI that installs only `pytest` and
  `requirements.txt` skips those tests. Run the suite once with the dependency
  uninstalled, the way CI lacks it: since pytest 8.2, `importorskip` warns when
  the import fails with an `ImportError` other than `ModuleNotFoundError`, and
  the warning says a later release will make it an error.
- **Copy the CI before calling it done.** Read the repository's workflow
  files and run each job's install and test commands locally against the
  suite's branch, in each Python or compiler version the matrix names that
  this machine can provide; record the commands and results in
  `$WORK/tests/work/`.

## Report its coverage

Measure line coverage of the package with the new suite against the
repository's own tests, and list the public names no test calls directly.
Leave the coverage tools' JSON and the list in `$WORK/tests/work/`, with the
commands that made them, and give the totals in your report.

**Coverage is measured before and after, per part of the repository** (a file,
or a directory when there are many; vendored or generated code is excluded
from every total). "Before" is the repository's own suite at the base commit,
which is 0 tests and 0% where it has none; "after" adds the new suite.

- **Tests per part** counts the tests that execute at least one line of that
  part, from per-test coverage: coverage.py's `dynamic_context =
  test_function` (or `pytest --cov-context=test`), saved with `coverage json
  --show-contexts`, or, for compiled code, one gcov or llvm-cov run per test
  with the counters reset between them, each test's gcovr JSON in one
  directory. When a per-test run is too slow, or the tests reach the code
  through a program run as a subprocess, count the tests that exercise each
  part (a test of a command-line option counts for every part the program
  runs) and write the counts as a table with columns `part`, `tests_before`,
  `tests_after`.
- **Line coverage** uses the same tool and flags before and after; record the
  tool and its version.
- Measure "before" on the base commit itself, not on the suite's branch with the
  new tests deselected: a conftest or fixture the new suite adds can change what
  the repository's own tests reach.

Compiled Cython needs a traced build to be measured at all:
`cythonize(..., compiler_directives={"linetrace": True})`, compiled with
`-DCYTHON_TRACE=1 -DCYTHON_TRACE_NOGIL=1`, and on Python 3.13 or later also
`-DCYTHON_USE_SYS_MONITORING=0`, without which coverage records nothing in the
compiled modules. Use the `Cython.Coverage` plugin with `parallel = true` and
`patch = subprocess` to count the command-line tests. A `coverage combine`
that fails part way has already folded some data files into `.coverage`;
re-run it with `--append` on a copy, or the result silently drops them.

Then hand off step 2 (`references/step0-interview.md`, *Handing off step
2*). The baseline's unit-test run is the suite's first check inside the
search: it MUST pass there (`references/repo-mode.md`, *Setting it up*, step
5).

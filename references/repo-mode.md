# Repository mode: optimizing code in a git repository

Use it when the code lives in a git repository. Plain mode (`init --copy`) is for a benchmark with no
git repository as its base.

## What it guarantees

- **The original is never written to.** `init --repo` makes a bare clone at
  `autooptimize/repo.git`, its objects hardlinked when on the same filesystem
  (almost no disk), and removes its `origin` remote, which is the original's
  own directory. No command of this skill writes outside the search.
- **Each branch is the path.** `autoopt/<id>` is the original's history up to
  the copied commit, followed by one commit per iteration on the path to
  `<id>` (*path* in `references/glossary.md`).

## Layout

```
autooptimize/
  repo.git/              bare clone of the original; no remotes
  harness/               the benchmark: --copy lands here; fixed for the search
  iterations/
    0/
      src/               worktree on autoopt/0: the original at the copied commit
      meta.json  result.json  notes.md  README.md  runs/  unit-tests/
    7/
      src/               worktree on autoopt/7
      …                  records beside src/, never inside it
```

Records sit beside `src/` so the iteration's `README.md` cannot overwrite the
repository's own. `repo.git/info/exclude` hides only `__pycache__/`, `*.pyc`,
`.pytest_cache/` and `*.egg-info/`, so a new `README.md` or `.npz` inside the
package is committed like any other file.

## Setting it up

**1. Check the original.** Its checked-out branch is where the copy starts and
what the winner is later compared with and merged onto; `config.json["repo"]` records it, with the
commit and the remote. When step 0 settled on a new unit-test suite, the
original here is the suite's clone, and `init` runs at the end of step 1
(`references/step1-unit-tests.md`).

| Found | What `init` does | Do |
|---|---|---|
| uncommitted changes to tracked files | refuses, and lists them | ask the user: commit them in their repository, or re-run with `--allow-dirty` to start from the last commit without them |
| untracked files | copies nothing untracked, and says nothing | check `git -C <original> --no-optional-locks status` (a plain `status` rewrites the original's index) for data the benchmark needs; pass it with `--copy` (into `harness/`) or put it in `autooptimize/reference/`, never into `src/` |
| detached HEAD | refuses | check out the branch the search should start from |

**2. Write the harness.** It takes `--src`, imports the package from that
directory, and meets the `metrics.json` contract in `references/layout.md`.
It lives in `harness/`, outside every worktree, so no iteration can change
what is measured; a change to the harness is a new search. Pass it to `init`
with `--copy` from outside `autooptimize/`, or the setup subagent writes it
into `harness/` at step 2.

```python
"""One run, importing the package from the worktree it is pointed at."""

import os, sys, json, argparse

parser = argparse.ArgumentParser()
parser.add_argument('--src', required=True)
parser.add_argument('--iteration', required=True)   # for a reference gate: references/step2-baseline.md
parser.add_argument('--seed', type=int, required=True)
parser.add_argument('--out', required=True)
args = parser.parse_args()

src = os.path.realpath(args.src)
sys.path.insert(0, src)

import mypackage                       # the package the search optimizes

# Without this, an installed copy can win and every iteration measures it.
assert os.path.realpath(mypackage.__file__).startswith(src + os.sep), \
	mypackage.__file__

os.makedirs(args.out, exist_ok=True)
# ... measure, then write metrics.json with 'note': mypackage.__file__
```

**3. Init.** `{src}` expands to the iteration's worktree. Write the harness
path absolute, in double quotes so the shell expands `$WORK` (placeholder
rules: `references/layout.md`, *config.json*). `AUTOOPT_REF` and
`--iteration {iteration}` serve a gate against the baseline's output
(`references/step2-baseline.md`, *A gate that compares against the baseline's
output*); drop both when there is none.

```bash
python3 $AO/autoopt.py init $WORK \
	--repo /path/to/<repository> \
	--copy /path/to/bench.py \
	--command "AUTOOPT_REF=$WORK/autooptimize/reference PYTHONPATH={src} python3 $WORK/autooptimize/harness/bench.py --src {src} --iteration {iteration} --seed {seed} --out {out}" \
	--tests 'PYTHONPATH={src} OMP_NUM_THREADS=4 python3 -m pytest -q tests' \
	--tests-timeout 1800 \
	--objective seconds --direction min --unit s --observers peak_mb …
```

Code compiled by numba, Triton or `torch.compile` also sets
`NUMBA_CACHE_DIR={cache}/numba TRITON_CACHE_DIR={cache}/triton
TORCHINDUCTOR_CACHE_DIR={cache}/inductor` in both strings
(`references/measuring.md`, *A cached compile can outlive the edit*). The rest
of `init` is as in `references/step0-interview.md`. Add `autooptimize/` to the
ignore list of the project that holds the search, not of the copied
repository.

**4. Find the oldest toolchain the repository supports** (its pinned Cython or
compiler) and record the command that builds and tests a tree with it:
`autoopt.py config $WORK --set 'after.oldest_toolchain_command=<command with {src}>'
--reason "..."`. `promote` then runs it on every iteration it promotes
(`references/promotion.md`, *The preflight*). The search's environment usually
has the newest compiler, and newer syntax compiles in every iteration and
fails in the repository's CI: `requirements.txt` may pin `Cython<3.1` while
`pyproject.toml` asks for `Cython>=3.0`, and Cython 3.0 lacks
`cython.pointer[T]` (write `cython.pointer(T)`) and `cython.p_const_char`.

**5. Run the baseline** (the setup subagent, `references/step2-baseline.md`).
**The unit tests MUST pass on it.** If some fail on unmodified code, the
failing test ids go to the user (`setup --ask`). With their agreement, the
`--tests` command deselects them (`--deselect tests/test_x.py::test_y`, set
with `config --set tests.command=…`) and `spec.md` lists them. Never deselect
a test that an iteration broke.

## Import from the worktree, never the installed package

A package under development is usually an editable install of the original.
Without `PYTHONPATH`, `import <package>` resolves there from any directory,
and the runner starts each run in `iterations/<id>/`, not in `src/`, so the
working directory does not rescue it. **The symptom is every iteration
matching the baseline to the last digit.** A non-editable installed copy
fails the same way.

- The harness's `sys.path.insert` covers its own import. Set
  `PYTHONPATH={src}` as well, in the command and in `--tests`: it comes before
  site-packages and a subprocess inherits it, so the package's command-line
  entry points and the test suite also import the worktree.
- Keep the `__file__` assertion in the harness and record `__file__` in
  `note`.
- **Never run `pip install` or `uv pip install -e` on a worktree.** That
  points the shared environment at one iteration, for every iteration and every
  other search that uses it.
- **A package with compiled extensions is built in place inside each
  worktree**, as a prefix to both `--command` and `--tests`. For setuptools:
  `(cd {src} && python3 setup.py build_ext --inplace -q) && …`, which skips
  up-to-date extensions, so after a commit's first run it costs seconds. With
  `gpus_per_agent` above 1 several runs start at once and race on the build;
  put `flock {cache}/build.lock` (Linux) in front of it.
- **A project that is built, not installed** (C, C++, Rust; no `pyproject.toml`
  or `setup.py`) is the same with its own build: `(cd {src} && make -j8 -s) && …`
  in `--command`, and `make -C {src} test` (or the repository's test target) as
  `--tests`. The build alone is also `checks.build`
  (`references/step2-baseline.md`, *Write the checks*).

## The unit-test gate

`--tests` gives a command that runs **once per iteration**, in `src/`, after
the first run passes its gates and before the other runs (and a second time,
whole, when it uses `{selected}` and the median passes the keep rule). It also works in plain mode, where it
runs in the iteration directory.

| | |
|---|---|
| Pass | exit code 0. Anything else fails, including pytest's 5, "no tests collected", which almost always means the wrong directory |
| Failure | `status: gate-failed`, off the frontier. The table's `tests` column reads `FAIL` |
| Record | `result.json["tests"]` (fields: `references/layout.md`); output in `unit-tests/log.txt` |
| Not run | when the first run crashed or broke a gate. The column reads `-` |
| Resume | a result for the same commit is reused; `--rerun` runs the suite again |
| Placeholders | `{src}`, `{iteration}`, `{out}` (the `unit-tests/` directory), `{cache}` (shared with the runs of the same commit), `{selected}` (below) |
| GPU | inherits the iteration's GPUs |
| Artifacts | untracked files the run writes into the worktree (an index such as `rna.fasta.fai`) are removed after it, listed in the log's last line and `result.json["tests"]["removed"]`, and recorded in `autooptimize/test-artifacts.txt`. The runner's commit leaves a recorded artifact out, a recorded artifact is no unmeasured change, and `promote` refuses a commit that adds one: `git rm --cached` it and measure again. The runner's own `.autoopt-build.lock` and `.autoopt-build.log` (`autoopt.RUNNER_FILES`) are never committed or counted as unmeasured; any other untracked file outside `after.build_outputs` (`references/step0-interview.md`, *The questions*) is an unmeasured change |

**Run only the tests for the changed code, and the whole suite only on a
possible winner.** Put `{selected}` where the test paths go:

```
--tests 'PYTHONPATH={src} NUMBA_CACHE_DIR={cache}/numba python3 -m pytest -x -q {selected}' --tests-path tests
```

| Changed against the parent's commit | `{selected}` becomes |
|---|---|
| `pkg/foo.py` | every `test_foo.py` under `--tests-path` |
| `tests/test_foo.py` | itself |
| `__init__.py`, `conftest.py`, any other file under the tests path, a non-Python file, a module with no `test_<name>.py`, nothing at all, or an iteration with no parent | the whole tests path |

The runner runs the whole suite on an iteration whose median passes the
default keep rule, since only such an iteration can be promoted.
`promote` refuses one whose last pass was a subset and prints the command
that fixes it, `run_iteration.py <dir> --full-tests`: with `promote:
orchestrator`, an iteration promoted on the user's own rule may need it. The
table's `tests` column reads `pass sub` for a subset pass.

**In a timing search, the suite runs on a machine other jobs time on.** Pin
every thread pool the package uses (`OMP_NUM_THREADS` above; also
`NUMBA_NUM_THREADS`, `MKL_NUM_THREADS` or a torch setting where they apply):
an unpinned suite spreads over every core and inflates the objective of
whatever else is timing there. **Pin to the smallest count the suite needs, not to 1**,
after grepping the tests for skips on thread count: a suite that skips every
test with `n_jobs > numba.config.NUMBA_NUM_THREADS` under
`NUMBA_NUM_THREADS=1` silently drops the tests of the parallel path a speed
search is most likely to break.

## What this mode adds to the interview

Rows for call 4 of `references/step0-interview.md`, *The questions*. The
first is asked in these words, and never assumed.

| Call | Question | Options to offer | Default | Fills, in `spec.md` |
|---|---|---|---|---|
| 4 | "Do you want to add new unit tests?" | no; yes | none: always asked | New unit tests |
| 4 | On a yes: for the functions being optimized, or the whole repository? | the functions being optimized (Recommended); the whole repository | none: always asked, though one option is recommended (step 0's one exception) | New unit tests |
| 4 | Which command runs the repository's tests, and with how many threads? | the command its CI runs, with thread pools pinned | from CI | the `--tests` flag |

## Common errors

| Message or symptom | Cause | Do |
|---|---|---|
| `has uncommitted changes to tracked files` | the original is dirty | ask the user; commit there, or `--allow-dirty` |
| `unit tests: FAIL` on A0 | tests that already failed | ask the user; deselect those tests and record them in `spec.md` |
| `fatal: '…/src' is a missing but already registered worktree` | a `src/` was deleted by hand | `git -C repo.git worktree prune`, then re-run `new` or `restore` |
| `a branch named autoopt/7 already exists` | `new --new-id 7` named an id whose iteration was moved aside but whose branch remains | brief the next free id; `reset` removes both |
| `fatal: invalid reference: autoopt/9` | the parent's branch does not exist | check `--parent` against `autoopt.py status` |
| `has no git checkout: it was pruned` | `prune --worktrees` removed `src/` | `autoopt.py restore $WORK --id N` |
| a new file in the package is missing from the branch | it matches the repository's own `.gitignore` | say so in the README; `git add -f` inside `src/` if it belongs in the change |

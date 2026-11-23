This PR adds a pytest suite for `pkg` and its command-line programs, `tool` and `tool-index`. It holds eight tests in three modules under `tests/`, with fixtures in `conftest.py`. It changes no source, build or workflow file.

## Tests and line coverage by part

| Part | Tests before | Tests after | Line coverage before | Line coverage after |
|---|---:|---:|---:|---:|
| `pkg/` | 3 | 9 | 37.0% | 80.2% |
| `pkg/cli/` | 2 | 6 | 30.0% | 91.7% |
| `vendor/` (excluded) | 1 | 2 | 16.7% | 33.3% |
| **Total** (excluded parts left out) | 3 | 10 | 35.1% | 83.3% |

"Before" is the existing tests, and "after" adds this suite. Tests per part count the tests that ran at least one line of the part, from per-test coverage; a test counts once per part, so the rows do not sum to the total.

## Running it

```
pip install -e .
python3 -m pytest tests
```

- The suite takes about ten seconds on one core, and its 8 tests need Python 3.8 or later and pytest.
- `PKG_BIN_DIR` points the suite at programs installed elsewhere.
- One test skips when `zstd` is not installed.

The suite is not added to CI here.

The suite passed in each configuration the CI workflow runs: Python 3.8 and Python 3.12 on Linux, each with `pip install -e .` and `python3 -m pytest tests`, and Python 3.12 without `numpy`, where two more tests skip.

## How expected values are derived

Expected values come from the documentation:

- **Inputs:** written by hand, small enough to check on paper.
- **Where the documentation is silent:** 2 of the 6 test functions or methods pin the current output instead, and each says so in its documentation or a comment.

## What it covers

| Module | Tests | What it checks |
|---|---:|---|
| `test_cli.py` | 4 | The options of `tool` and `tool-index`: `--threads`, `-v`, `-k`, `--help`. |
| `test_io.py` | 3 | Reading and writing every documented format. |
| `test_core.py` | 1 | `score` against a short reference implementation. |

## Line coverage

How it was measured:

- **Tool:** coverage.py 7.6.1, with `--cov-context=test`.
- **Lines counted:** each source line counts once: 222 executable lines in five files; one file of vendored or generated code (30 lines) is counted apart and left out of every total.

| Tests | Lines covered |
|---|---:|
| `pytest tests/old` | 71 (32.0%) |
| `make doctest` | 24 (10.8%) |
| **Existing tests together** | **78 (35.1%)** |
| This suite alone | 168 (75.7%) |
| **Existing tests and this suite** | **185 (83.3%)** |

The files that gain the most:

| File | Lines | Existing tests | With this suite |
|---|---:|---:|---:|
| `pkg/io.py` | 50 | 0.0% | 90.0% (+45 lines) |
| `pkg/cli/main.py` | 40 | 25.0% | 90.0% (+26) |
| `pkg/core.py` | 100 | 60.0% | 85.0% (+25) |
| `pkg/cli/args.py` | 20 | 40.0% | 95.0% (+11) |

## What the suite does not cover

**Behaviour that contradicts the documentation is left untested**, so that fixing it will not break the suite. `tests/KNOWN.md` names each case, and the test that would cover it says so in a comment. One case, `tool -v` printing to stdout where the documentation says stderr, is listed in #<bug issue>.

**Public names no test file names (2 of 4):** `Reader.close` and `unused_helper`.

**Lines no test reaches:**

- **One file, `pkg/unused.py` (12 lines), runs in no test at all.** Without it, coverage is 88.1%.
- **The largest gaps in files that are reached** are in `pkg/core.py` (15 lines not run, 85.0% covered), `pkg/io.py` (5, 90.0% covered), `pkg/cli/main.py` (4, 90.0% covered) and `pkg/cli/args.py` (1, 95.0% covered).

**Platforms:** Linux only; macOS, which CI also runs, was not tried.

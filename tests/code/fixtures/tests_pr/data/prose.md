=== opening
This PR adds a pytest suite for `pkg` and its command-line programs, `tool` and `tool-index`.

=== support
fixtures in `conftest.py`

=== docs
the documentation

=== running.commands
pip install -e .
python3 -m pytest tests

=== running.notes
- The suite takes about ten seconds on one core, and its {{tests}} tests
  need Python 3.8 or later and pytest.
- `PKG_BIN_DIR` points the suite at programs installed elsewhere.
- One test skips when `zstd` is not installed.

=== running.ci
The suite is not added to CI here.

=== checked
The suite passed in each configuration the CI workflow runs: Python 3.8 and
Python 3.12 on Linux, each with `pip install -e .` and `python3 -m pytest
tests`, and Python 3.12 without `numpy`, where two more tests skip.

=== derivation
Expected values come from the documentation:

- **Inputs:** written by hand, small enough to check on paper.

=== module tests/test_io.py
Reading and writing every documented format.

=== module tests/test_cli.py
The options of `tool` and `tool-index`: `--threads`, `-v`, `-k`, `--help`.

=== module tests/test_core.py
`score` against a short reference implementation.

=== coverage.method
- **Tool:** coverage.py 7.6.1, with `--cov-context=test`.

=== contradictions
One case, `tool -v` printing to stdout where the documentation says stderr, is
listed in #<bug issue>.

=== contradictions.marked
`tests/KNOWN.md` names each case, and the test that would cover it says so
in a comment.

=== platforms
Linux only; macOS, which CI also runs, was not tried.

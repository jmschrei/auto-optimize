# The unit-test PR

One of the documents step 5 can publish, when step 1 wrote a suite and the
user chose this PR (`after.ship_documents` holds `tests`). It is opened from
step 1's branch, its commits as they are, and holds only test files: at most one dependency
manifest that declares a test dependency, the repository's test driver when
the user allowed it, and files the user approved by name may join them. It
changes no source, build or workflow file, so it can be merged whether or not
the optimization is.

| | |
|---|---|
| Branch | step 1's `auto-optimize/<YYYY-MM-DD>-unit-tests` in `$WORK/tests/suite`, one commit above the original's, pushed under the same name |
| Targets | the original's branch: `branch` in `$WORK/tests/branch.json` |
| Title | "Add unit tests for `<scope>`", passed to `tests_pr_body.py --title`, which refuses a title that does not start with a capital letter, ends with a period or is longer than 72 characters, and writes it to `$WORK/ship/pr-tests/body.title` |
| Body | `$WORK/ship/pr-tests/body.md`, rendered by `tests_pr_body.py` from the tables in `$WORK/ship/pr-tests/` and the prose in `$WORK/ship/pr-tests/prose.md` |

`tests_pr_tables.py` writes the tables from the files step 1 left in
`$WORK/tests/work/` and from the clone, the agent writes only what no table
can say, and `tests_pr_body.py` renders the two: every count, total,
percentage and ranking is computed from the tables. The body says what the
suite is and what it checks. It does not name the tool that wrote it, and it
does not describe any check other than the ones step 1 ran.

## Its shape

Every unit-test PR has the layout and the thoroughness of
[BenLangmead/bowtie2#535](https://github.com/BenLangmead/bowtie2/pull/535),
which added a pytest suite for a C++ repository's three command-line
programs: a summary paragraph, coverage by part, how to run the suite and
where it passed, how its expected values were derived, a table of its
modules, line coverage before and after, and an account of what it leaves
out. Its numbers describe that repository and nothing else. The sections, in
order; `tests_pr_body.py --template` prints each prose field with what it
holds:

| Section | Computed from | Prose fields |
|---|---|---|
| (opening paragraph) | `collection.tsv`; `files.tsv` checks "It changes no source, build or workflow file." (below) | `opening`, `support`, `declares`, `driver <path>` |
| *Tests and line coverage by part* | `coverage.tsv`, `parts.tsv` | `parts.tests` (only when tests per part were not counted) |
| *Running it* | nothing | `running.commands`, `running.notes`, `running.ci`, `checked` (its last paragraph) |
| *How expected values are derived* | the `pinned` column of `collection.tsv` | `docs`, `derivation` |
| *What it covers* | `collection.tsv` | `module <path>`, one per module |
| *Line coverage* | `coverage.tsv` | `coverage.method` |
| *What the suite does not cover* | `names.tsv`, `coverage.tsv` | `contradictions`, `contradictions.marked`, `platforms` |

The opening paragraph starts with the `opening` field, such as "This PR adds
a pytest suite for `pkg` and its command-line programs.", and the renderer
adds the number of tests and modules and the sentence about changed files.
A file outside the suite's directory is refused, with three exceptions:

- A dependency manifest (`DESCRIPTION`, `requirements-dev.txt`,
  `requirements-test.txt`, `test-requirements.txt`, `dev-requirements.txt`)
  with the `declares` field: the sentence becomes "It changes no source or
  workflow file. Its one change outside the tests is <declares>."
- The repository's own test driver, with `=== driver <path>` (a clause after
  "which"), once `after.tests_driver_edit` is `yes` in
  `$WORK/autooptimize/config.json`. `ask`, `no` or no value refuses it.
  `--config` names another config.json.
- A file the user approved by name, with `--allow-source` (below).

**A section is left out only when it does not apply to the repository**: a
package with no public Python names has no names line. The renderer drops a
section whose tables are absent and names each one on stderr. A table that
applies but was not made is not a reason to drop its section: make it, or
tell the user which section is missing and why before the PR is posted.

## Making the tables

All of them go into one folder, `T=$WORK/ship/pr-tests`, which also holds the
prose, the body and, once posted, `pr.json`. What is specific to the
repository (how it was built with coverage, which tests are its own) comes
from step 1's record in `$WORK/tests/work/`; the generators do the counting.

1. **The collection and the changed files**, against the original's commit,
   which `$WORK/tests/branch.json` records:

   ```bash
   T=$WORK/ship/pr-tests; mkdir -p $T
   BASE=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["commit"])' $WORK/tests/branch.json)
   BRANCH=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["tests_branch"])' $WORK/tests/branch.json)
   (cd $WORK/tests/suite && python3 -m pytest --collect-only -q <the suite's directory>) > $T/collect.txt
   python3 $AO/tests_pr_tables.py collection --collect-output $T/collect.txt --root $WORK/tests/suite --out $T
   python3 $AO/tests_pr_tables.py files --repo $WORK/tests/suite --base $BASE --head $BRANCH --out $T
   ```

   `files` also writes `$T/branch.json` (the clone, the branch and `$BASE`),
   which the push reads. A test counts as pinning the current output when its
   docstring or a comment says so in step 1's words, "pins the current
   output".
2. **Line coverage**, from the JSON step 1 left in `$WORK/tests/work/`
   (`references/step1-unit-tests.md`, *Report its coverage*): one file for
   the existing tests on the original's commit (or one per CI step, each
   with its own label) and one for the new test files alone on the branch.
   When step 1 kept no JSON, or kept it without per-test data, measure it
   again the same way:
   - Python: `pytest --cov=<package> --cov-context=test`, then `coverage json
     --show-contexts -o <file>`. `--cov=<package>` lists the files no test
     imports, which would otherwise be missing. Tests that run the program
     as a subprocess need `parallel = true` and `patch = subprocess` in the
     coverage config and a `coverage combine` before the JSON; their lines
     carry no per-test context, so give `coverage` a `--part-tests` file.
   - Compiled code: a build with `--coverage -O0` (and
     `-fprofile-update=atomic` for a threaded program, whose counters
     otherwise race), one build or one counter reset per suite, then `gcovr
     -r <source> --json <file>`.
   - Numba code: coverage.py cannot trace compiled functions. Run both
     coverage runs with `NUMBA_DISABLE_JIT=1`, which runs them as Python, and
     say in `coverage.method` that the figures are lower bounds when a test
     had to be dropped from them because it fails only without the compiler.

   ```bash
   python3 $AO/tests_pr_tables.py coverage --existing '`make test`=base.json' --suite suite.json \
   	--strip <build prefix> --drop 'tests/*' --exclude 'third_party/*' --out $T
   ```

   The existing tests' columns and the suite's are combined; a test run in
   both is counted once and `coverage` warns, but the "this suite alone" row
   then includes the existing tests. A repository whose sources sit in one
   flat directory needs `--part-map`, a table of globs and the part each
   names.
3. **Public names**, for a Python package: `python3 $AO/tests_pr_tables.py
   names --source <package> --tests <the suite's directory> --out $T`.

## The prose, and rendering

```bash
python3 $AO/tests_pr_body.py $T --template > $T/prose.md    # once: one <<FILL per field the tables call for
python3 $AO/tests_pr_body.py $T --title 'Add unit tests for <scope>' --out $T/body.md
```

Each field starts with a line `=== <field>`, and `module` and `driver` name
what they describe after it (`=== module tests/test_io.py`). Fields hold
plain, complete sentences and no headings. **Prose MUST NOT type a number a
table computes**: it writes a token instead, which the renderer fills.

| Token | Value |
|---|---|
| `{{tests}}`, `{{modules}}`, `{{functions}}`, `{{pinned}}` | from `collection.tsv` |

**Where the suite was run** is the `checked` field: one paragraph, the last
of *Running it*, naming the CI configurations step 1 ran the suite in (each
workflow job's install and test commands, in each Python or compiler
version it could provide) and saying that the suite passed in each. Take it
from step 1's record of those runs in `$WORK/tests/work/`, and name only the
configurations that record shows; a configuration step 1 did not run is not
named, and one CI runs that step 1 could not is said under `platforms`.

**Where the untested cases are listed** is the `contradictions` field, one
paragraph or the single word `none`. When the bug report is among the
documents chosen at step 5, it names the report as `#<bug issue>`, which is
replaced by the report's number once the report is filed. When the bug
report is not chosen, that issue will never exist: `#<bug issue>` anywhere in
the prose is refused, and `contradictions` names an issue number, a URL, or a
file in the repository in backticks (`tests/KNOWN.md`), which must be in
`files.tsv` or on the tests branch.

**Where the suite records each case it leaves untested** is prose, in
`contradictions.marked`, because suites differ, and it MUST be true of this
suite. When every affected test says so itself, the field is "Each affected
test says so in a docstring or comment." When the cases are named in a
document instead (a `TARGETS.md` with a row per function or option, say), the
field names the document and says how many of the cases it and the tests
name; count them before writing the number, since no table holds it. The
renderer puts the field, one paragraph, between "… is left untested, so that
fixing it will not break the suite." and the `contradictions` paragraph. It
is required whenever `contradictions` is not `none`.

**Source files the user chose to ship in this PR.** A file that is neither a
test file, a dependency manifest nor the test driver (a one-line source fix,
the build file that registers a new test target) is refused by default. When
the user chose to ship it here, write their words in `spec.md` first, then
name each file with `--allow-source PATH` (repeatable) on `tests_pr_body.py`
and give `tests_pr_push.py` the same flags. Also list each path, one per
line, in `$T/allow-source.txt`: `ship.py` renders and pushes with the flags
that file gives. The `declares` field MUST also
name every such file, in backticks, and say what changed; a file named by the
flag but not by `declares`, or by the flag but not changed, is refused. The
opening then says "It changes no workflow file. It changes `a` and `b`
outside the tests: <declares>." in place of the claim about source files.

`tests_pr_body.py` exits 1, writes nothing and lists every problem on stderr:
a prose problem (a placeholder, an unknown field or token, a missing field,
an `opening` that does not start with a capital letter, a `checked` or
`contradictions` that is a list, a `contradictions` that says nowhere the
cases are listed, a module cell over 25 words), tables that disagree with
each other (coverage that does not add up, a changed file outside the suite's
directory), a title that breaks the rule above, or a word of the search's own
vocabulary or a local path in the body or title (`lint_text.py`). A prose
field no section uses is named on stderr.

## The hand-written inputs

Every other table is written by a generator and read by the renderer; none is
edited by hand.

| File | What it holds |
|---|---|
| `prose.md` | the prose fields, from `--template` |
| a part map, a `--part-tests` file | `tests_pr_tables.py coverage --help` |

## Pushing it

```bash
python3 $AO/tests_pr_push.py $T --repo <owner/repo>    # preview: pushes and opens nothing
```

The preview refuses, listing every problem, when the title or body breaks a
rule, the body is not what the tables and prose render to now, `files.tsv` is
not the branch's files, the clone has uncommitted changes, a commit lacks
`after.commit_trailer`, or `pr.json` names another repository. Otherwise it
prints the repository, the target branch, the remote and the head, the
title, the sections, the commit's files and what the post needs.

Posting is done for every chosen document together, after the user has
reviewed them all: `python3 $AO/ship.py rehearse $WORK`,
then `python3 $AO/ship.py post $WORK all --approved --digest D`
(`references/step5-ship.md`). No document is posted on its own. The post
pushes step 1's branch, under the name the user's answer about branch
names gives (`references/step5-ship.md`; `--remote-branch` for another),
opens the PR with `gh pr create`, and
records it in `$T/pr.json` (repository, URL, number, branch, title, a digest
of title and body, and the time); `ship.py post` then renders the other
documents again so that they hold its number. A second `--post` is refused.

Each default gives way to its flag:

| | Default | Flag |
|---|---|---|
| clone, branch, base commit | `$T/branch.json` | `--suite`, `--branch`, `--base` |
| target branch | `branch` in `$WORK/tests/branch.json`; for a search made before that file existed, the original's branch as the search's config.json records it | `--target-branch` |
| remote | `after.pr_remote`, else `origin` | `--remote` |
| head | `after.pr_head_owner:<branch>` when the remote is `after.pr_remote`, else the branch | `--head OWNER:BRANCH` |

**When the original belongs to another account** (an open-source upstream),
the PR comes from the user's fork, and `tests-branch` leaves the clone's
`origin` pointing at the upstream. A search that has ended records the fork
as `after.pr_remote` and `after.pr_head_owner`, and `ship.py post` uses it.
For a unit-test PR posted without an ended search, after the user approves
the preview and says to create the fork:

```bash
gh repo fork <owner/repo> --clone=false       # prints the fork's URL
git -C $WORK/tests/suite remote add fork https://github.com/<you>/<name>.git
python3 $AO/tests_pr_push.py $T --repo <owner/repo> --remote fork --head <you>:<the tests branch> --other-owner --post
```

Without `--other-owner` the post prints `refused, nothing done` and exits 1;
the line comes **below** the preview, so read the exit code, not only the
first lines. Add the flag only once the user has confirmed the target
repository.

**A change to the tests branch after the search** (a test removed because
CI fails it, say) is committed on the branch, which may hold any number of
commits over its base, and pushed with `git push --force-with-lease` if it
rewrites history, once the user agrees: a force push rewrites a branch the
PR already shows. It is not in the measured optimization branch. Say so in
the optimization PR, and run the optimized code's tests against the updated
branch before the user merges either. Run `tests_pr_tables.py collection`
and `files` again, and `coverage` when the change could move it, render the
body again, and replace the PR's title and body with `tests_pr_push.py $T
--repo <owner/repo> --edit`, which pushes nothing.

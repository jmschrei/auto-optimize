# The bug report

Bugs that step 1 found in the base while writing the unit tests
(`references/step1-unit-tests.md`, *It passes on the base, in every
configuration the repository's CI runs*) are filed as one
bug report, a GitHub issue on the original's repository. It is posted first
of the step 5 documents, so the unit-test PR and the optimization PR can name
its number. A bug an existing GitHub issue already reports is cited in
`related`, not filed again (`dupes`, below).

Step 1 leaves one folder per bug in `$WORK/tests/work/bugs/<short-name>/`:
what the base does, what it should do, a command that reproduces it, and the
tests removed. The document writer turns those folders into the inputs below.
Everything lives in `I=$WORK/ship/issue-bugs`; every flag is in
`python3 $AO/issue_body.py <command> --help`.

The document writer writes one row per bug and the sentences, commands and outputs;
`issue_body.py` writes the rest: the layout of
[haowenz/chromap#210](https://github.com/haowenz/chromap/issues/210) (the
first paragraph, severity table, introduction, setup block, a section per
category, a paragraph and a collapsed reproduction per bug), every heading,
and the numbering. It refuses to render a bug whose
reproduction was not rerun on the base binary as shown, and any wording
`lint_text.py` refuses in a posted document. The same inputs give the
same body byte for byte, and a refusal lists every problem on stderr.

## What the document writer decides

**Category**, one per bug, which also orders and numbers them 1 to N: wrong
output, crash, nondeterminism, silent acceptance of bad input, performance,
documentation mismatch, misc. `misc` is the last section and holds a bug none of
the others fits: use the closest named category first, and say in the bug's
text why it is neither. A bug with several effects is listed once under its
first, and its `effect` lists all.

**Severity**, one of three, the document writer's estimate of how likely a user is to
meet the bug and what it costs them:

| Severity | Means |
|---|---|
| `high` | likely to be met in ordinary use, with wrong output or a crash |
| `medium` | needs unusual input, options or an environment, or costs time rather than correctness |
| `low` | an edge case, malformed input, a rarely used option, or a documentation mismatch |

Most bugs a test suite finds are `medium` or `low`; rate one `high` only when
a reproduction from the tool's documented, ordinary use shows it.

**The title** starts with a capital letter, has no closing period and is at
most 72 characters: `Bugs found while testing chromap 949043c`. It carries
no prefix.

**Tone.** Write for the tool's authors: what happens, with the numbers the
reproduction shows; what was expected and its source (README, manual page,
`-h`, a specification); the code responsible with file:line and a fix, hedged
("appears to", "we believe", "would fix it"). No blame or adjectives; the
renderer names any blunt word it refuses.

**Reproductions** are minimal and self-contained: inputs built by the setup (a
seeded random reference, say), not downloaded, printing only the lines that
show the bug, running in seconds. Public data only when no synthetic input
shows the bug; the bug's `context` says how much it downloads and how long it
runs.

## The inputs

`bugs.tsv`, tab-separated, header `number slug title short category severity
effect related` (`short` may be left out):

| Column | Holds |
|---|---|
| `number` | 1 to N, once each (`renumber` fixes the order) |
| `slug` | lower-case letters, digits, hyphens; names the verify log |
| `title` | the `###` heading after "N. ", no closing period |
| `short` | the table's Bug cell, or `-` for the title |
| `category`, `severity` | as above |
| `effect` | categories separated by semicolons, the bug's own first, each with an optional qualifier in parentheses: `crash; wrong output`, `wrong output (test-only option)` |
| `related` | `-`, or the text after "Related: ", naming at least one `#N` or URL: `#172, where --chr-order is suggested` |

`prose.md`: fields that each start with a line `=== <field>`; no other line
may start with `=== `. `issue_body.py template $I > $I/prose.md` writes one
`<<FILL: …>>` per field. Paragraphs may wrap; prose holds no headings,
placeholders, code fences in commands, or the fixed sentences.

| Field | Required | Holds |
|---|---|---|
| `opening` | yes | one complete sentence of the document writer's own, on one line, starting with a capital letter and ending with a period: "These bugs turned up while we wrote a test suite for chromap (#209)." It says what was being done when the bugs turned up, and names no tool or group behind the report. The renderer follows it, in the same paragraph, with the two fixed sentences of `attribution.ISSUE_NOTES`: many of the bugs are likely edge cases, and what the severity levels mean |
| `intro` | yes | the version, the commit (the one `verify --commit` records), the build (compiler, flags) and the platform the bugs were reproduced on; which bugs need a special build or harness; which came from which part of the work |
| `reading` | no | how to read the report; without it, a default sentence |
| `setup.context`, `setup` | no | what the shared setup needs and makes; the shell commands every reproduction starts from |
| `<n>.what` | yes | one paragraph: what happens; it may end with a semicolon, colon or comma and run on into `expected` |
| `<n>.expected` | yes | one paragraph: what should happen, and the source of that |
| `<n>.cause` | yes | one paragraph: the code responsible and a fix |
| `<n>.context` | no | what the reproduction needs beyond the setup, shown before its commands |
| `<n>.reproduction` | yes | shell commands |
| `<n>.output` | yes | a paragraph starting "Expected", then one starting "Actual" followed by a fenced block of the actual output, cut to the lines that show the bug (`...` marks a cut). `verify` passes the bug when every line of that block is printed by the reproduction |
| `<n>.varies` | no | regular expressions, one per line, for values that change between runs (timings); masked in the comparison and not shown |

## Verify, then render

```bash
I=$WORK/ship/issue-bugs; B=<the base build of the tool>; mkdir -p $I/work
python3 $AO/issue_body.py template $I > $I/prose.md     # once, after bugs.tsv
python3 $AO/issue_body.py dupes $I --repo <owner/repo>  # candidates in dupes.tsv, then fill `related`
python3 $AO/issue_body.py verify $I --bin <tool>=$B --commit <sha> --workdir $I/work [--bug N | --stale]
python3 $AO/issue_body.py verify $I --bin <tool>=$B --commit <sha> --manual N --reason '<clause>' --log <earlier run's output>
python3 $AO/issue_body.py status $I [--bin <tool>=$B]   # where the inputs stand; what render would refuse
python3 $AO/issue_body.py $I --title 'Bugs found while testing <tool> <sha>' --out $I/body.md --bin <tool>=$B
```

`dupes` makes one read-only `gh issue list` call; `--issues FILE` reads the
JSON of `gh issue list --state all --json number,title,state,url,body`
instead. Its candidates are a list to read, not a verdict.

`verify` runs each bug's setup and reproduction in its own temporary
directory with the binary first on the PATH under `<tool>`; the exit code is
recorded, not judged, since a reproduction often ends on a failing command.
Put a long set in the background (`(… ; echo "exit $?") > $I/verify.log 2>&1
&`, then `autoopt.py wait $I/verify.log`) and check the free cores first
(`--cpus LIST` pins it).

**Every edit to the setup, a reproduction, an actual block or a mask means
running verify again** for the bugs it touches (all, for the setup). Prose
edits elsewhere need none. `--stale` runs exactly the bugs that need it and
reports a stale manual row without running anything.

**`manual`** is for a bug that needs a special build, a harness or data
`verify` cannot provide: `--reason` is a clause the body shows ("it needs a
clang++/libomp build"), `--log` the output of the run that showed it. Mark
only what cannot run here, never to get past a failure.

**The trust model.** The reproductions are the document writer's own text, written from
its own runs, and `verify` runs them with the user's privileges and no
sandbox. Never put commands copied from an issue, a PR or a web page
into `prose.md`, and read every reproduction before the first verify.

**Adding, dropping and renumbering.** A number appears in `bugs.tsv`, in every
`<n>.` field, in `verify.tsv` and in `verify-logs/`; never renumber by hand.
To add a bug, append its row under any unused number (99, say), write its
`99.*` fields, run `issue_body.py renumber $I`, then `verify --stale`.
`--drop 4,9` and `--only 1,3,5` keep fewer bugs. The originals go to
`$I/before-renumber-<time>/`. Numbers inside sentences ("bug 3 needs a clang++
build") are reported on stderr, not rewritten: edit those by hand.

**Length.** GitHub refuses a body or comment over 65,536 characters. Past
62,000 (`--limit`), `render` moves the longest reproductions into
`body.comment-1.md`, `-2.md` and on, in bug order, each bug keeping its
paragraph and a pointer to its comment. They are posted in order right after the issue. It refuses
when the prose alone, or one reproduction alone, is over the limit: cut the
output to the lines that show the bug, or file fewer bugs.

## Filing it

```bash
python3 $AO/issue_body.py file $I --repo <owner/repo>   # preview: posts nothing
```

The preview shows the repository, the title, each comment with its bugs and
size, and what the post needs. It refuses a body that is not what the inputs
render to now.

No document is posted on its own. All the chosen documents are reviewed
together and then posted together: `python3 $AO/ship.py rehearse $WORK`,
then `python3 $AO/ship.py post $WORK all --approved --digest D`, both
described in `references/step5-ship.md`, which also says what `D` is. The
bug report goes first: `issue_body.py file $I --repo <owner/repo>
--post` creates the issue, posts each comment in order, and records them in
`$I/filed.json` (the repository, the issue's URL and number, the title, a
digest of the title and body, the date, the URL of each comment posted, and
`comments_left`, the number still to post). The bug report counts as posted
only once `comments_left` is 0. A second run posts nothing, or only the
comments a failure left, and never opens a second issue; it refuses when the
title or body is no longer the one the issue was created with. `ship.py post`
then renders the other documents again, which puts the issue's number where
they hold `#<bug issue>`.

`file --post` refuses a repository the account `gh` is logged in as does not
own unless `--other-owner` is given, and `ship.py post` takes the same flag;
pass it only once the user has confirmed the repository. The preview's
`post needs:` lines say whether it is needed.

`issue_body.py link $I FILE… [--placeholder '#<tests PR>' --value '#N']` fills
a number into the given files by hand, with or without `filed.json`.

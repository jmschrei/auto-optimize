# Promoting, and predicting what a change is worth

## Promotion follows the keep rule

With `promote: auto`, `init`'s default, each iteration's runner
applies the default keep rule against the iteration's parent: a median that
beats the parent's by more than the noise band. It records the verdict as `keep` in `result.json` and prints a `keep:`
line. When the verdict passes and the parent is still the
root, the runner promotes the iteration, the round closes, and the runner runs
that round's external validation if the user approved one. The orchestrator
does nothing.

| `keep:` line | Means |
|---|---|
| `promoted to the root (…); round N closed` | it is the root; every iteration briefed after it forks from it |
| `no, its median is within the noise band …` | kept out, for the reason given |
| `passes (…); promotion is the orchestrator's (config "promote")` | `promote: orchestrator` |
| `passes (…); not promoted: <reason>` | `promote` refused it (*The preflight*, the toolchain scan, a root that moved): the subagent fixes what the reason names and runs `finish`; the orchestrator then runs `promote --id <it>` |
| `passes (…); not promoted (--no-promote)` | the runner was told not to: a re-measurement |

**A keep rule in the user's own words**, such as a tie rule or a second
measure that must not regress, is beyond the runner. Set it at step 0 with
`init --promote orchestrator` (later, `config --set promote=orchestrator`),
and promote by hand on that rule
(`references/step3-rounds.md`, *The loop*):

```bash
python3 $AO/autoopt.py promote $WORK --id A7
```

- `promote` refuses anything not `ok`; a same-code control; with a `{selected}` test command, one whose last test
  pass was a subset (it prints the `run_iteration.py <dir> --full-tests`
  command that fixes it); an iteration that does not descend from the current
  root, whose promotion would drop the root's change; and, when the search has
  checks, one with no passing compare, such as an iteration measured before
  the checks existed (`autoopt.py check $WORK --id A7` runs it by hand).
  Promoting the current root again changes nothing.
- **The keep rule compares an iteration with the root it forked from**, its
  parent. A `NEW FRONTIER` inside the noise band is not a promotion.
- **A root that nothing beats may be a lucky median.** If a whole round
  produces nothing above it, brief a same-code control of the root before
  blaming the ideas (`references/step3-rounds.md`, *The machine during the
  search*). Never `--rerun` the root itself.

## The preflight

In repository mode `promote` also runs `python3 $AO/preflight.py $WORK --id
<id>` on the iteration's own change and refuses with every problem listed.
Fix each in the iteration's worktree and run `run_iteration.py` again (it
commits the edit and measures every seed afresh; the new median decides the
keep rule again), then `finish` and promote. This is for an iteration being
promoted; a problem found in the winner after `end` is fixed by a new
`--after-end` iteration (`references/step4-final.md`, *An iteration made after
the end*). The repository's CI is not part of it. `--id best --path` checks
the whole path from the commit the search copied and writes
`$WORK/autooptimize/preflight.json` (`{winner, problems}`).

| Check | A problem is |
|---|---|
| `text` | an added comment that names the search (`lint_text.py`: winner, baseline, a label such as A12, a path on this machine); the same in the iteration's `notes.md` `## Change`; a `## Commit message` that breaks the message rules; for an iteration made with `--after-end`, either section missing (an ordinary iteration writes them once its `keep:` line reads `promoted` or `passes`, and `finish` refuses its README without them) |
| `indent` | an added line indented with tabs in a file indented with spaces, or the reverse. A new file is compared with the repository's files of the same extension. Four fifths of the indented lines decide a file's style; a file with fewer than five decides nothing |
| `license` | an added line with a marker of a license a permissive repository cannot take: GPL, LGPL, AGPL, MPL, non-commercial, share-alike (*Code from another project*, below). Code copied without any marker is not found: say what you copied in `notes.md` |
| `toolchain` | `after.oldest_toolchain_command` (`{src}` is an export of the tree) exits non-zero. Skipped while the key is unset |

Exit 0 no problem, 1 a problem (`check: path:line: text`), 2 refused.

### Code from another project

Code copied or translated from another project (numpy, scipy, a C library)
keeps that project's license notice above it. A line-by-line translation is a
derivative and carries the source's license; calls into a public API, and an
implementation written from a paper or a documented interface, need no
notice. For a repository under a permissive license (MIT, BSD, Apache-2.0):

| Source license | Copy it in? |
|---|---|
| MIT, BSD-2-Clause, BSD-3-Clause, ISC, zlib, Unlicense, CC0 | yes, with its copyright notice, conditions and disclaimer above the code, verbatim from its license file |
| Apache-2.0 | yes, keeping its license text and any `NOTICE` file, and marking what changed |
| MPL-2.0 | only as separate files that stay MPL-2.0 |
| LGPL, GPL, AGPL; non-commercial or academic-only; no license at all; CC-BY-SA or CC-BY-NC on code | no |

A file's own header governs it. Anything else, or a destination under another
license, goes to the user.

## Predicting what a change is worth

**Changes in different kernels add in the objective's units, and only when
independent.** An edit carries about the same absolute gain from one root to
the next; its percentage grows as the root shrinks, so the same edit on a
faster root looks bigger while contributing the same amount.

**Predict in the objective's units and record it**:
`brief … --predict -0.03 --predict-basis "A88 measured -0.026 on A86"`,
against the parent's median. It lands in `meta.json`, `result.json` and the
README, and the round summary prints it beside the measured change. A
prediction that lands inside the noise band is evidence the changes are
independent; a miss is a finding.

**Quote the increment over the actual parent, not a variant's headline.** If
the root already holds part of the change, the headline overstates it and the
subagent reports a shortfall that was never real.

**An idea's value belongs to the pair of change and root.** The same edit can
be worth several times more on a later root, once an earlier change removes
the bottleneck that hid it. Name known dependencies in the brief and sequence
them; when an idea fails, ask whether it was merely early.

**A shortfall larger than the noise band has a mechanism; find it.** One
inside the band is the prediction holding.

| Mechanism | Example |
|---|---|
| the root already removed much of the work this edit accelerates | another edit deleted most of the loop this one sped up |
| the compiler already did part of the edit | under `fastmath` the compiler already turns a divide by a loop-invariant value into a multiply by its reciprocal, so hoisting the divide by hand gains little |
| two edits compete for the same hardware | both land in one kernel and contend for front-end and store-forwarding capacity |

Record the prediction regardless.

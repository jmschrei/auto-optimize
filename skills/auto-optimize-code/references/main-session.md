# The main session, from step 2 to the end

The main session talks to the user, and agents do the rest:
- the setup subagent does step 2;
- the idea miner writes the ideas;
- an orchestrator subagent runs step 3 in phases and spawns the
  iterations, one at a time.

Every wake-up after a quiet spell rewrites the main session's whole context,
so it reads `status --brief` and the agents' reports, and nothing else: no
code, logs, READMEs or iteration files. Its commands are below; `$AO` and
`$WORK` are as in step 0.

**At every wake-up and after every report, run `python3 $AO/stages.py next $WORK`.** It prints the next move
and its kind: `agent` (do it; the table below says how), `run` (add `--run`: the script starts it, then run `next` again, until the kind is not `run`), `wait`
(`end` is finishing), or `stop` (the user answers). It ends the search under `after.stop=auto`.

| When | Do |
|---|---|
| step 0 is written down | `autoopt.py setup $WORK`, then spawn its prompt, unedited (`references/step0-interview.md`, *Handing off step 2*). End the turn: the report wakes you, a setup question included, so start no timer, watch or polling loop |
| the idea miner's report carries idea lines because its file write was refused | write them to the stage's file in `ideas.md` and `autoopt.py mine $WORK --done` |
| the setup subagent reports done | tell the user the `answers taken without asking` that `setup --done` listed, if any. `autoopt.py mine $WORK`, then spawn the idea miner from its prompt. It starts here, not with setup, because it prices ideas against A0's profile, which setup writes after the baseline. End the turn; its report wakes you |
| the setup subagent asks (its report, or `status`'s `setup (step 2): waiting for the user:`) | ask the user. Record the answer: `config --set … --reason "<their words>"`, or a dated line under `spec.md`'s *Rules added during the search*. Then `setup $WORK --answer "<their words>"`, and send the message it prints to that subagent (SendMessage), or spawn the prompt it prints after the message if the subagent is gone |
| `status` stops printing `first iteration: waiting for …` | `autoopt.py busy $WORK`: when it exits 1, apply the answer setup recorded in `spec.md` for a busy machine, and ask the user only when setup recorded none (`references/step3-rounds.md`, *The machine during the search*). Then `autoopt.py loop $WORK`, spawn its prompt in the background with the description and model its stderr line names, and end the turn. Start no watch: the phase's report wakes you |
| a phase reports | show the user its round tables and decisions, and ask its questions. Then, by why it stopped (the report's reason outranks `stages.py next`): after `loop_handback` settled iterations, `loop $WORK` again; at *When to stop*, the row below at once under `after.stop=auto`, and under `ask` only when the user does not go on |
| an iteration's report reaches you, or a phase ends without its report | the phase died: `loop $WORK --force`, and spawn what it prints. The new phase resumes from the records (`status --resume`) |
| the user writes | answer from `status --brief`. A rule or a setting they give: record it as above, then tell the orchestrator subagent in one line (SendMessage) |
| the user is away and a question waits | take the least-damage answer (`references/step3-rounds.md`, *Questions while the search runs*), record it under *Decisions taken while the user was away* in `ideas.md`, and say so in your next message |
| the user ends the search, or a phase reached *When to stop* and the search is not to go on | with a phase open, tell the orchestrator subagent to stop (SendMessage): it lets the iteration in flight settle, closes the phase and reports. Then `autoopt.py end $WORK --reason <word or the user's words> --background`, then `autoopt.py wait $WORK/autooptimize/end.log`, again until it prints `wait: finished`. Show the user `ended.md`, with the paired verdict beside the medians, labeled "paired" (`references/step4-final.md`), then go to step 5 (`references/step5-finish.md`) |
| a line starting `NOTICE the auto-optimize-code skill changed` (from `stages.py next` or `stages.py status`) | tell the user in one line, with the commits the line's `git log` lists; do not stop or redo anything for it |
| the user asks what the search cost | `autoopt.py tokens $WORK`: each agent's and each role's weighted tokens, from the transcripts |

## No timed updates

Nothing wakes the main session on a timer: an agent's report does, or the
`autoopt.py wait` it ends a turn on.

A claim in a brief (a count, a cause, a result) must come from a log the main session has read, not from memory of an
earlier turn or another agent's summary; when it has not read the log, it reads it first or leaves the claim out.

A resumed session starts with `references/resume.md`. `status` names a phase
still open from the old session, and `loop $WORK --force` replaces it.

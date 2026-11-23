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
(`end` is finishing), or `stop` (the user answers); once every stage is complete it prints `every stage is complete` and nothing else. It ends the search under `after.stop=auto`.

| When | Do |
|---|---|
| step 0 is written down | `autoopt.py setup $WORK`, then spawn its prompt, unedited, with the description and model its stderr line names: the setup subagent runs on `subagent_model` (`references/step0-interview.md`, *Handing off step 2*). End the turn: the report wakes you, a setup question included, so start no timer, watch or polling loop |
| the idea miner's report carries idea lines because its file write was refused | write them to the stage's file (`mining/stage-<n>.md`) and `autoopt.py mine $WORK --done` |
| the setup subagent reports done | tell the user the `answers taken without asking` that `setup --done` listed, if any. `autoopt.py mine $WORK`, then spawn the idea miner from its prompt. It starts here, not with setup, because it prices ideas against A0's profile, which setup writes after the baseline. End the turn; its report wakes you |
| the setup subagent asks (its report, or `status`'s `setup (step 2): waiting for the user:`) | ask the user. An answer that changes a setting: `config --set … --reason "<their words>"`. Then `setup $WORK --answer "<their words>"`, which writes the dated line in `spec.md`, and send the message it prints to that subagent (SendMessage), or spawn the prompt it prints after the message if the subagent is gone |
| `status` stops printing `first iteration: waiting for …` | `autoopt.py busy $WORK`: when it exits 1, apply the answer setup recorded in `spec.md` for a busy machine, and ask the user only when setup recorded none (`references/step3-rounds.md`, *The machine during the search*). Then `autoopt.py loop $WORK`, spawn its prompt in the background with the description and model its stderr line names, and end the turn. Start no watch: the phase's report wakes you |
| a phase reports | show the user its round tables and decisions, and ask its questions. Then, by why it stopped. After `loop_handback` settled iterations, follow `stages.py next`: after a phase that promoted, it prints `loop $WORK`; after one that promoted nothing, the search has reached `stalled`, which it ends under `after.stop=auto` and asks about under `ask`. At another *When to stop* criterion the report names (the report's reason outranks `stages.py next`), the row below at once under `after.stop=auto`, and under `ask` only when the user does not go on |
| an iteration's report reaches you, or a phase ends without its report | the phase died: `loop $WORK --force`, and spawn what it prints. The new phase resumes from the records (`status --resume`) |
| the user writes | answer from `status --brief`. A rule or a setting they give: record it (`SKILL.md`, *Rules that hold everywhere*), then tell the orchestrator subagent in one line (SendMessage) |
| the user is away and a question waits | take the least-damage answer (`references/step3-rounds.md`, *Questions while the search runs*), record it under *Decisions taken while the user was away* in `ideas.md`, and say so in your next message |
| the user ends the search, or a phase reached *When to stop* and the search is not to go on | with a phase open, tell the orchestrator subagent to stop (SendMessage), or, without SendMessage, `autoopt.py loop $WORK --stop`, after which its next `brief --next` briefs nothing and tells it to hand back. Either way it lets the iteration in flight settle, closes the phase and reports. Then `autoopt.py end $WORK --reason <word or the user's words> --background`, then `autoopt.py wait $WORK/autooptimize/end.log`, again until it prints `wait: finished`. Show the user `ended.md`, with the paired verdict beside the medians, labeled "paired" (`references/step4-final.md`). The search is over: go to step 5, which asks the user how to ship (`references/step5-ship.md`), then step 6 (`references/step6-finish.md`) |
| step 5 starts (the search has ended) | `ship.py plan $WORK`; ask its questions in one AskUserQuestion call and record each answer with `config --set after.… --reason "<their words>"` (`references/step5-ship.md`, *Ask how to ship*). Show every preview before its `--yes`. On the GitHub route, spawn the document writer from `ship.py brief $WORK` and end the turn; its report wakes you |
| the document writer reports | show the user every document and preview together (`references/step5-ship.md`, *Review, then post all*); a change they ask for goes back to the writer. Post only on their word, with the digest of the batch they saw |
| a post stops part way | fix what its last line names, then run the same `post` command: what was posted is skipped |
| a line starting `NOTICE the auto-optimize-code skill changed` (from `stages.py next` or `stages.py status`) | tell the user in one line, with the commits the line's `git log` lists; do not stop or redo anything for it |
| the user asks what the search cost | `autoopt.py tokens $WORK`: each agent's and each role's weighted tokens, from the transcripts |

## No timed updates

Nothing wakes the main session on a timer: an agent's report does, or the
`autoopt.py wait` it ends a turn on.

A claim in a brief (a count, a cause, a result) must come from a log the main session has read, not from memory of an
earlier turn or another agent's summary; when it has not read the log, it reads it first or leaves the claim out.

A resumed session starts with `references/resume.md`. `status` names a phase
still open from the old session, and `loop $WORK --force` replaces it.

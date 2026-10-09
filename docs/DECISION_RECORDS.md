# Decision records

**Goal:** after an unattended run, a person who doesn't read code can understand what was built, why it was built that way, and what was given up. They can then steer the next round by proposing a different trade-off or naming an edge case, without opening a source file.

Each issue the runner completes leaves one **decision record**: a short Markdown file committed in the target repo, next to the code it explains.

```
docs/decisions/0066-appointments-model.md
```

- **Name:** the issue number, padded to 4 digits, then a short slug.
- **Retries:** a retry of the same issue updates the file rather than adding a new one.
- **Rollback:** reverting the issue's commit reverts its record too, so the docs never describe code that's gone.

## Two parts, kept apart

A record has two parts with different levels of trust. Readers should always be able to tell them apart.

| Part | Written by | Trust |
|---|---|---|
| **Rationale:** what was done, why, alternatives, trade-offs, assumptions, edge cases | The agent, as part of its change | A claim. Useful, but it can be wrong or tidied up after the fact |
| **Verified facts:** components changed and their tags, tests run, scope and refs verdicts, commit | The tool, after validation | Checked independently |

*Status:* the agent-written rationale is in place (via the templates' `project_rules.md`). The verified-facts block, the consistency check between the two parts, the run digest and the per-component history are the next steps (see the [README roadmap](../README.md#roadmap)).

## The rationale format

```markdown
# 0066: Appointments model rewrite

Status: implemented | partial | blocked · Issue: #66 · Date: 2026-10-09

## In short
Two or three sentences a non-programmer can follow: what exists now that didn't before.

## Problem and constraints
What the issue required, and what limited the options: project rules, the existing design,
the environment, performance or cost limits.

## Approach
How it works, in plain language. Name the parts of the code as `path::Name`
(e.g. `clinic-base/models/appointments.php::find_appointment`) so tools can link them.

## Alternatives considered
| Option | Why not chosen |
|---|---|
| ... | ... |
(Write "None considered" if that's the truth. Never invent alternatives after the fact.)

## Trade-offs
What this approach gives up (speed, simplicity, flexibility, accuracy, cost) and the
situation in which that would start to matter.

## Assumptions
- What was taken as true without checking, why, and what goes wrong if it isn't.

## Edge cases
- Handled: ...
- Not handled: ... and the symptom someone would see if it happens.

## Changes outside the scope
Each change outside the issue's **Scope:** line and why it was needed (the same as SCOPE_NOTES).

## How it was verified
Tests run or added, what they show, and what remains untested.

## To change this
The levers: "to favour X over Y, change `path::Name`"; the likeliest places for a bug to come from.

## Rollback
What depends on this change, and what to check if it's reverted.
```

**Length:** aim for 300–700 words. A small issue can leave sections out, but should say so ("No assumptions beyond the issue text").

## Rules for the agent

- **Write for a reader who won't open the code.** Explain mechanisms, not syntax.
- **Only claim what happened.** Don't list tests you didn't run, alternatives you didn't weigh, or edge cases you didn't check. "Not checked" is a valid and useful answer.
- **Name components exactly** (`path::Name`). The verified-facts step will cross-check them against the diff.
- **The record is part of the change.** It's committed with the code, and a missing record means the work is unfinished.
- `docs/**` is allowed by the scope gate by default, so the record never counts as an out-of-scope change.

## How people use it

1. **Coming back after a run:** read the run digest, then the records it flags.
2. **Disagreeing with a trade-off, or spotting an edge case:** file a new issue that cites the record ("Revisit 0066: favour …, because …"). The next agent then knows what it is revising and why.
3. **Investigating a bug:** read the records for the affected components. "Not handled" edge cases and the assumptions are the first suspects.
4. **Rolling back:** the record's Rollback section, together with the dangling reference check, says what else must change.

## How it's evaluated

See [EVALUATION.md](EVALUATION.md#study-4-documentation-for-a-human-in-the-loop). The main measures are whether the rationale's claims hold up against the code, and whether a person working from the records alone can answer design questions and steer changes correctly.

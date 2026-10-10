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

*Status:* the agent-written rationale (via the templates' `project_rules.md`), the verified-facts block and the consistency check between the two parts are built (`integrity/integrity.py record`). The run digest and the per-component history are the next steps (see the [README roadmap](../README.md#roadmap)).

## Verified facts and the consistency check

`integrity/integrity.py record` runs as a gate after the tests, the scope check and the dangling reference check, and before the adversary. It finds the issue's record and appends a **Verified facts** block containing:

- the components changed, with their tags;
- symbols removed;
- the issue's scope, and which out-of-scope changes the record explains and which it doesn't;
- the supervisor checks that passed before it;
- any consistency warnings.

The block sits between markers and is regenerated on every attempt. Don't edit it by hand.

Problems come in two levels:

- **ERROR** (a missing record, a missing section, a bad `Status:`) blocks the commit in `enforce` mode.
- **WARN** (changes the rationale doesn't mention, references to components that don't exist, unexplained out-of-scope changes, removals it doesn't mention, tests it names that don't exist) never blocks. Warnings are written into the facts block, where the reader and the adversary both see them. The adversary also checks the rationale against the diff, and can veto.

`--commit <sha>` checks the record of a past commit retrospectively. Settings are in [REFERENCE.md](REFERENCE.md#decision-record-check).

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

## Key parameters
| Parameter | Value | How chosen | Raise it / lower it |
|---|---|---|---|
| `path::NAME` | 50 mm | ... | higher: fewer, later alerts; lower: more false alarms |
Every threshold, constant, default or limit chosen in this change. Write "None" if there
are no tunable values.

## Assumptions
- What was taken as true without checking, why, and what goes wrong if it isn't.

## Edge cases
- Handled: ...
- Not handled: ... and the symptom a user would see if it happens.

## Changes outside the scope
Each change outside the issue's **Scope:** line and why it was needed (the same as SCOPE_NOTES).

## How it was verified
Tests run or added, what they show, and what remains untested.

## To change this
Concrete options: "to favour X, change Y from A to B; cost: Z". Then the likeliest places
for a bug to come from.

## Rollback
What depends on this change, and what to check if it's reverted.
```

**Why Key parameters:** the numbers are where most trade-offs actually live. Listing each one
with its value, its origin and what moving it does lets a non-programmer weigh and retune
the trade-offs without reading the code.

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

## Without the adversary

The adversary is optional, and off by default for software projects. Without it, records are still required and still cross-checked against the diff deterministically: missing sections block the commit, and mismatches appear as warnings in the Verified facts block. What's lost is a check that the rationale is *true* (for example, that an edge case it says is handled really is), not just consistent by name. Turn it on for claim-heavy or risky work, and for ML.

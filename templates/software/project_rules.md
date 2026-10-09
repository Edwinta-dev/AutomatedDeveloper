The repo's AGENTS.md (or CLAUDE.md), if present, is binding and overrides the issue text. If satisfying the issue would break a rule, stop, report STATUS: BLOCKED, and explain why in NOTES.

## Project rules

TODO: the project's non-negotiable rules, one bullet each. For example: the stack and banned dependencies, naming and schema conventions, which files must never change, how to run the tests.

## Always

- Implement exactly the one issue. No drive-by refactors.
- Stay inside the issue's **Scope:** line; explain any unavoidable change outside it under SCOPE_NOTES.
- Write or update tests in the same change as the code. Never weaken, skip or delete tests to go green.
- Never commit secrets (`.env`, keys, local config) or dependency and build folders.

## Decision record

Every issue ends with a decision record committed alongside the code: `docs/decisions/<issue number, 4 digits>-<short-slug>.md` (update it if it already exists). Write it for someone who will never open the code. Only state what actually happened: don't list tests you didn't run, alternatives you didn't weigh, or edge cases you didn't check ("Not checked" is fine). Name code as `path::Name`. Aim for 300-700 words; leave out a section only by saying why.

```
# <NNNN>: <title>
Status: implemented | partial | blocked · Issue: #<N> · Date: <YYYY-MM-DD>
## In short            (2-3 plain sentences: what exists now that didn't before)
## Problem and constraints
## Approach             (how it works, plainly; name components as path::Name)
## Alternatives considered   (table: option | why not chosen; or "None considered")
## Trade-offs           (what this gives up, and when that would start to matter)
## Assumptions          (what was taken as true, and what breaks if it isn't)
## Edge cases           (handled / not handled + the symptom someone would see)
## Changes outside the scope (same as SCOPE_NOTES)
## How it was verified  (tests run or added; what remains untested)
## To change this       (the levers for a different trade-off; likely bug sources)
## Rollback             (what depends on this; what to check if reverted)
```

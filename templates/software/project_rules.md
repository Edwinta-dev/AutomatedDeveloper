The repo's AGENTS.md (or CLAUDE.md), if present, is binding and overrides the issue text. If satisfying the issue would break a rule, stop, report STATUS: BLOCKED, and explain why in NOTES.

## Project rules

TODO: the project's non-negotiable rules, one bullet each. For example: the stack and banned dependencies, naming and schema conventions, which files must never change, how to run the tests.

## Always

- Implement exactly the one issue. No drive-by refactors.
- Stay inside the issue's **Scope:** line; explain any unavoidable change outside it under SCOPE_NOTES.
- Write or update tests in the same change as the code. Never weaken, skip or delete tests to go green.
- Never commit secrets (`.env`, keys, local config) or dependency and build folders.

## Decision record

Every issue ends with a decision record committed alongside the code: `docs/decisions/<issue number, 4 digits>-<short-slug>.md` (update it if it already exists). Records are claims to check against the code, not proof: write them so a later agent can test each claim quickly. Only state what actually happened: don't list tests you didn't run, alternatives you didn't weigh, or edge cases you didn't check; put anything you believe but did not test or measure under Not verified. Name code as `path::Name`. Every numeric or timing claim cites its source, a constant `path::NAME` or a config key, e.g. "resyncs every ~2 h (`fw/config.h::RESYNC_EVERY` x `CYCLE_S`)". Do not restate what the code comments already say. Aim for 600-1200 words (150-2000 accepted); leave out a section only by saying why ("None found" is fine). A change that breaks or degrades existing behaviour goes under Breaks or needs follow-up, not under Trade-offs; file or describe the follow-up. Do not mark your own claims [checked] or [tested]: the tool sets evidence levels.

```
# <NNNN>: <title>
Status: implemented | partial | blocked · Issue: #<N> · Date: <YYYY-MM-DD>
## In short            (2-3 plain sentences: what exists now that didn't before)
## Traps: why the obvious fix is wrong   (non-obvious constraints a later agent might "simplify" back into a bug, each with its reason; or "None found")
## Components           (bullets, one per line: `path::Name` - its role in this change)
## Problem and constraints
## Alternatives considered   (option - why not chosen; or "None considered")
## Trade-offs           (what this gives up, and when that would start to matter)
## Key parameters       (each threshold/constant/default: value with its source `path::NAME`, how chosen, effect of raising/lowering; "None")
## Assumptions          (what was taken as true, and what breaks if it isn't)
## Edge cases           (- Handled: ... / - Not handled: ... with the symptom a user would see)
## Not verified         (claims or behaviours you did not test or measure; "None")
## Breaks or needs follow-up (existing behaviour this change breaks or degrades - an app or caller that no longer works, a removed field, a migration someone must run - each with the follow-up filed or described; "None")
## Changes outside the scope (same as SCOPE_NOTES)
## How it was verified  (tests run or added, with names)
## To change this       (concrete options as "to favour X, change Y from A to B; cost: Z"; likely bug sources)
## Rollback             (what depends on this; what to check if reverted)
```

The repo's AGENTS.md (or CLAUDE.md), if present, is binding and overrides the issue text. If satisfying the issue would break a rule, stop, report STATUS: BLOCKED, and explain why in NOTES.

## Project rules

TODO: the project's non-negotiable rules, one bullet each. For example: the stack and banned dependencies, naming and schema conventions, which files must never change, how to run the tests.

## Always

- Implement exactly the one issue. No drive-by refactors.
- Write or update tests in the same change as the code. Never weaken, skip or delete tests to go green.
- Never commit secrets (`.env`, keys, local config) or dependency and build folders.

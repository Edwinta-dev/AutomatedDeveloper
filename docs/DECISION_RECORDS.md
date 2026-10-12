# Decision records

**Goal:** after an unattended run, a person who doesn't read code can understand what was built, why it was built that way, and what was given up. They can then steer the next round by proposing a different trade-off or naming an edge case, without opening a source file. A later agent debugging or extending the same code gets the non-obvious constraints, the untested claims and the "how to change this" handoff in a few hundred words.

> **Records are claims to check against the code, not proof.** Treat every sentence in a rationale as a hypothesis. In practice the most useful parts are the ones that tell you what to test (Traps, Assumptions, Not verified), and the worst failure is taking a wrong claim as fact.

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
| **Rationale:** traps, components, alternatives, trade-offs, parameters, assumptions, edge cases, what was not verified | The agent, as part of its change | A claim. Useful, but it can be wrong or tidied up after the fact |
| **Verified facts:** change summary by area, removed symbols, scope verdict, checks passed, evidence levels, warnings | The tool, after validation | Checked independently |

## The verify banner: claims are hypotheses

Every record (v1 and v2) gets a short banner right under its title, written and replaced by the tool on every attempt, between `<!-- verify-banner:begin -->` and `<!-- verify-banner:end -->`:

> ⚠ Agent-written record: its claims are hypotheses to verify against the code, not ground truth. Evidence levels in Verified facts: [checked] confirmed by the tool · [tested] cited test exists and passed in this run · [claimed] not verified.

It is outside the agent's control: the agent cannot remove or reword it (the next attempt rewrites it), and it does not count as a change to the rationale. The record's encoding, BOM and newline style are kept; a record in a code page without the warning sign gets `WARNING:` instead.

## Evidence levels: how much to trust each claim

The facts block has an **Evidence** part. The tool, never the agent, gives each item of the rationale a level:

| Level | Meaning | Where it can come from |
|---|---|---|
| **[checked]** | the tool confirmed it: every `path::Name` the item names exists and changed in this commit, or the value it states matches the constant it cites (`` `growth_k` 3 `` with `growth_k: float = 3.0`) | Components, Key parameters, Edge cases (handled) |
| **[tested]** | the cited test file or test name exists **and** a test gate (a supervisor check whose label says test/tests/suite/pytest/jest/flutter; config `record.test_gate_re`) passed in this attempt (`AGENT_GATES_PASSED`) | How it was verified, Edge cases, Key parameters |
| **[claimed]** | not verified | everything else; Traps, Assumptions, Trade-offs, Not verified and Breaks or needs follow-up are always [claimed] |

The facts block shows the totals, per-section counts and up to 10 items with the reason for their level; the JSON record (`evidence.items`) has every claim. If the agent writes `[checked]` or `[tested]` itself, the tool ignores it and adds a WARN.

**How to weight claims.** [checked] means the code named exists and moved, or a number matches its constant; it does not mean the reasoning is right. [tested] means a test with that name exists and the test gate passed; it does not mean the test asserts what the record says. [claimed] is a hypothesis: Traps, Assumptions and Not verified are the first things to test, and the share of [claimed] items is the part of the record nobody has confirmed. In the OutdoorKoi records about 40% of claims were [checked] or [tested] (0092: 15 checked, 2 tested, 19 claimed of 36; 0069: 8, 4, 20 of 32).

## Breaks or needs follow-up

A v2 section (after Not verified): existing behaviour this change breaks or degrades (an app or caller that no longer works, a removed field, a migration someone must run), each with the follow-up filed or described, or "None". A breaking change belongs here, not under Trade-offs: in the #92 review "the app no longer reads `current_gcc`/`gcc`" was written up as a trade-off and nobody noticed.

When the committed record's section is not None/N/A, or its Status is partial/blocked, or its Status disagrees with the agent's STATUS, the runner commits with `Refs #N.` instead of `Closes #N.` (the issue stays open), prints why, and the digest lists the issue first. The section is required for records dated 2026-10-13 or later; an older v2 record without it gets a WARN.

## Verified facts and the consistency check

`integrity/integrity.py record` runs as a gate after the tests, the scope check and the dangling reference check, and before the adversary. It finds the issue's record and appends a compact **Verified facts** block:

- a change summary: files, components and lines changed per area (top-level folder), new and deleted files;
- removed symbols, grouped by whether anything still references them;
- the issue's scope, and which out-of-scope changes the record explains;
- the supervisor checks that passed before it;
- evidence levels (above): checked / tested / claimed counts per section and up to 10 items;
- at most 10 warnings (the rest are counted; the full list is in the JSON record).

The block sits between markers and is regenerated on every attempt. Don't edit it by hand.

Problems come in two levels:

- **ERROR** (a missing record, a missing section, a bad `Status:`) blocks the commit in `enforce` mode.
- **WARN** never blocks. The check is tuned to warn only where a reader would want to know:
  - a changed public component the record never mentions. Private helpers (Python/Dart `_name`, C/C++ `static` functions, JS/TS names that are not exported) and components with 5 or fewer changed lines are skipped; a folder the record names (`Embedded/sensor_bench/`) covers every file under it, deleted ones included. `...::name` / `…::name` abbreviations are resolved when exactly one changed file defines `name`. A **same-file helper** (in the same file as a component the record or the Scope names, and private or used by no other non-test file) is neither an uncovered change nor out of scope;
  - a reference to a file or component that doesn't exist (URL-like references such as `/v1/readings`, `rpc/http_get` or `<int:id>` are ignored; git-ignored scratch paths such as `build/proto.py` give an INFO; a suffix path that matches several files is resolved when the record names the leading folder elsewhere, when only one of them changed, or when the others are under `archive/` or `vendor/`);
  - a removed symbol the record doesn't mention, **only when the dangling reference check says something still uses it**;
  - unexplained out-of-scope changes (module-level code and imports of a file the record names anywhere count as explained) and tests named in How it was verified that don't exist;
  - a **numeric claim** that contradicts the constant it cites (below).

**Numeric claims.** For each `` `path::NAME` `` cited in the same sentence or bullet as a number, the check looks up `NAME` in the new tree (Python, C/C++ `#define`/`const`, JS/Dart `const`/`final`; a literal, or a product/sum of literals and other constants in the same file) and adds an INFO "claim cites NAME = value". It warns only when one single-literal constant is cited alone next to exactly one number that matches it under no common unit factor (1, 60, 1000, 3600, ...). It is deliberately conservative: it catches "timeout 9 s (`TIMEOUT_MS`)" when `TIMEOUT_MS = 5000`, not every loose approximation.

`--commit <sha>` checks the record of a past commit retrospectively. Settings are in [REFERENCE.md](REFERENCE.md#decision-record-check).

## The rationale format (v2)

```markdown
# 0066: Appointments model rewrite

Status: implemented | partial | blocked · Issue: #66 · Date: 2026-10-12

## In short
Two or three sentences a non-programmer can follow: what exists now that didn't before.

## Traps: why the obvious fix is wrong
- Non-obvious constraints a later agent might "simplify" back into a bug, each with its
  reason. E.g. "The retry key includes `boot_id`: `cycle_seq` restarts after power loss,
  so (device, cycle_seq) alone collides." Write "None found" if there are none.

## Components
- `clinic-base/models/appointments.php::find_appointment` - looks up by slot, not id.
- `clinic-base/api/routes.php::book` - validates and calls find_appointment.
(One line per component on its role. This replaces the old Approach prose: the code and
its comments say *how*; the record says *which parts* and *why*.)

## Problem and constraints
What the issue required, and what limited the options.

## Alternatives considered
- Option - why not chosen. ("None considered" if that's the truth.)

## Trade-offs
What this approach gives up, and the situation in which that would start to matter.

## Key parameters
- Alert threshold 50 mm (`sensors/config.py::ALERT_MM`): from the issue; higher gives
  fewer, later alerts. Every value cites its source constant or config key.

## Assumptions
- What was taken as true without checking, and what goes wrong if it isn't.

## Edge cases
- Handled: ...
- Not handled: ... and the symptom a user would see if it happens.

## Not verified
- Claims or behaviours not tested or measured (e.g. "deep-sleep clock drift was not
  measured"; "resync interval inferred from constants, not observed").

## Breaks or needs follow-up
- Existing behaviour this change breaks or degrades, with the follow-up filed or described
  (e.g. "The app still reads `current_green_ratio`; its algae chart is wrong until it reads
  `current_gcc` - follow-up #95"). "None" if nothing breaks. Not under Trade-offs.

## Changes outside the scope
Each change outside the issue's **Scope:** line and why (the same as SCOPE_NOTES).

## How it was verified
Tests run or added, by name, and what they show.

## To change this
Concrete options: "to favour X, change Y from A to B; cost: Z". Then the likeliest places
for a bug to come from.

## Rollback
What depends on this change, and what to check if it's reverted.
```

**Every numeric or timing claim cites its source**, a constant `path::NAME` or a config key: "resyncs every ~2 h (`fw/config.h::RESYNC_EVERY` × `CYCLE_S`)". In the OutdoorKoi records, "resyncs about hourly" was actually about 2 hours from the cycle constants; a citation makes such errors checkable.

**Length:** aim for 600–1200 words (150–2000 accepted; about 1,300 words was fine in review). Leave a section empty only by saying why ("None found").

**v1 records.** Records with an `## Approach` heading and no `## Components`, or dated before 2026-10-12, are checked against the v1 section list (In short, Problem and constraints, Approach, Alternatives, Trade-offs, Key parameters, Assumptions, Edge cases, Changes outside the scope, How it was verified, To change this, Rollback; up to 3000 words). They get one INFO "v1 record" and no ERROR for the new sections.

## Rules for the agent

- **Write the traps first.** What would a later agent "fix" back into a bug? That is the most valuable part of a record.
- **Only claim what happened.** Anything believed but not tested or measured goes under Not verified. "Not checked" is a valid and useful answer.
- **Breaking changes go under Breaks or needs follow-up**, not Trade-offs, with the follow-up filed or described. It keeps the issue open (`Refs`, not `Closes`).
- **Never mark your own claims** `[checked]` or `[tested]`: only the tool sets evidence levels.
- **Cite the source of every number** and **name components exactly** (`path::Name`). The check cross-checks them against the diff and the code.
- **Don't duplicate code comments.** The Components list says what each part is for; the code says how.
- **The record is part of the change.** It's committed with the code, and a missing record means the work is unfinished.
- `docs/**` is allowed by the scope gate by default, so the record never counts as an out-of-scope change.

## Record slices for agents: `integrity brief`

```
python integrity/integrity.py brief --repo R (--components "a.py::f, b/**" | --issue-file F | --paths ...)
                                    --mode debug|feature [--max-words 600] [--ref <branch>]
```

`brief` finds the records whose Components, facts block or other mentions touch the given components (path-suffix aware: `koi/x.py` matches `Backend/koi/x.py`; globs and folders work), keeps the relevant ones, and prints only the sections that matter for the task, most relevant first, each under its record id and title.

**Relevance (IDF-style).** A record scores 1/(number of records naming the file) for each target it matches through a file. Files named by more than 40% of the records (`settings.py`, the worker's poll loop) add nothing, unless the match is at component level and that component is rare (then 1/(records naming the component)). Records of issues the issue text references (`#91` → `docs/decisions/0091-*`) always qualify; the issue's own record is skipped. Below `--min-score` (default 0.5) nothing is printed, and the runner inserts no brief and logs "no specific records". With fewer than 5 records every file counts. On OutdoorKoi, #92 now gets 0091 (named in the issue) and no longer 0036/0033/0032/0027 (matched only through `settings.py` and `poller.py::_poll_user`); #69 gets 0055.

Sections per mode:

| Mode | Sections |
|---|---|
| `debug` | Traps, Assumptions, Edge cases (Not handled only), Key parameters, Not verified |
| `feature` | Traps, Alternatives considered, Trade-offs, To change this |

The output starts with "Hypotheses from earlier decision records — check them against the code; they are not proof." and is cut to `--max-words` on whole bullets. v1 records contribute what they have; in debug mode their "Not verified" lines are taken from How it was verified.

The runner can insert the brief into the prompt just after the issue text (config `"brief": {"enabled": true, "mode": "auto", "max_words": 600, "min_score": 0.5}`; `auto` = debug when the issue title or labels say bug/fix/regression, else feature). It runs for issues with a **Scope:** line or a `#N` reference, as a subprocess, and a failure just skips it. In the run dir, `issue_N.md` holds the issue text it was given and `brief_issue_N.md` the brief (or a "no specific records" note).

**How agents should use a slice:**

1. Treat each bullet as a hypothesis. Before relying on it, check it against the code (open the cited `path::Name` or constant).
2. In debug mode, start from Not verified, Not handled and Assumptions: in the OutdoorKoi review both real findings came from testing such claims.
3. Keep the Traps: if your fix removes something a trap explains, the trap is the reason the obvious fix is wrong.
4. In feature mode, start from To change this: it is the previous agent's handoff.

## How people use it

1. **Coming back after a run:** read the run digest. "Needs your attention" ranks environment/preflight blockers, then **Breaks or needs follow-up** items, then records with Status partial/blocked or a status mismatch with the agent, set-aside issues, other review items, then Not verified, Not handled and Assumptions. "Config changed mid-run" is a footnote. What was built shows each issue's evidence counts (checked/tested/claimed). Consistency warnings are shown only as a count per issue, next to the checks that passed.
2. **Disagreeing with a trade-off, or spotting an edge case:** file a new issue that cites the record ("Revisit 0066: favour …, because …").
3. **Investigating a bug:** run `brief --mode debug` for the affected components, then test the claims it lists.
4. **Rolling back:** the record's Rollback section, together with the dangling reference check, says what else must change.

## How it's evaluated

See [EVALUATION.md](EVALUATION.md#study-4-documentation-for-a-human-in-the-loop). The main measures are whether the rationale's claims hold up against the code (including cited numeric claims), whether a person working from the records alone can answer design questions and steer changes correctly, and whether a debugging agent reaches the root cause in fewer tokens with the brief than without it.

## Without the adversary

The adversary is optional, and off by default for software projects. Without it, records are still required and still cross-checked against the diff deterministically: missing sections block the commit, and mismatches appear as warnings in the Verified facts block. What's lost is a check that the rationale is *true* (for example, that an edge case it says is handled really is), not just consistent by name. Turn it on for claim-heavy or risky work, and for ML.

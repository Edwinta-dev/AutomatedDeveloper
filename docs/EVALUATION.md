# Evaluating the scope gate and component index

**Question:** does declaring a scope per issue, checking it deterministically, and offering a tagged component index make the runner better than it is today, at a cost worth paying?

"Better" means the four goals the enhancement was proposed for:

1. **Isolation.** Edits stay inside what the issue asked for.
2. **Human documentation.** A person can understand what was built and why, faster.
3. **Token efficiency.** Less context is spent finding the right code.
4. **Deterministic unattended development.** Runs finish with fewer retries, fewer deferrals and fewer regressions, decided by rules rather than by the model.

Every claim here is measured against the current runner. None of it is assumed.

---

## What is being compared

The new system is three separable pieces. Each is measured on its own, so a gain (or a cost) can be traced to the piece that caused it.

| Arm | Issues carry `**Scope:**` | Scope gate | Agent told about `lookup` | What it isolates |
|---|---|---|---|---|
| **A: baseline** | no | off | no | The current runner |
| **B: declared** | yes | `report` | no | The effect of *telling* the agent its scope, with nothing blocked |
| **C: enforced** | yes | `enforce` | no | The effect of blocking out-of-scope changes |
| **D: indexed** | yes | `report` | yes | The effect of the component index on navigation and tokens |

B vs A is the cheapest and most informative comparison, so run it first. C is only worth running once the retrospective study (below) shows the scope model has few false positives. Otherwise enforce mode will just cause deferrals.

## Three studies, cheapest first

### Study 1: detection accuracy (offline, no agent, minutes)

Does the gate correctly tell in-scope from out-of-scope changes? `integrity.py bench` mutates random components of a real repository, declares a random scope, and checks the classifier against the known answer.

| Metric | Target |
|---|---|
| Recall of out-of-scope changes | ≥ 0.99 |
| Precision (out-of-scope flags that really are out of scope) | ≥ 0.95 |
| In-scope edits wrongly flagged | ≤ 1% |
| Whitespace-only changes recognised | ≥ 0.99 |
| Gate run time on the target repo | ≤ 10 s per attempt |

Run it on every repo the runner works on: this repo, IE4727 and OutdoorKoi. If recall misses its target, nothing else in this plan is trustworthy, so fix that first.

### Study 2: retrospective baseline (offline, no agent, an afternoon)

How often does the *current* runner make changes outside an issue's scope? Every committed issue is a single commit, so the gate can check history directly (`integrity.py gate --commit <sha> --scope "..."`).

1. Pick at least 30 committed issues across two projects (IE4727 and OutdoorKoi have the most history). Include small, medium and large issues.
2. **Write each scope from the issue text only, without looking at the diff.** That's what a real backlog author would know. Record how long each one takes.
3. Run the gate on each commit in report mode.
4. Review every out-of-scope finding by hand and label it:
   - **necessary:** the issue couldn't be done without it (a caller had to change, a shared helper needed a parameter);
   - **gratuitous:** a drive-by refactor, reformatting, an unrelated fix;
   - **harmful:** it broke or changed behaviour that was not asked for.

| Metric | Definition |
|---|---|
| Drift rate | % of commits with ≥ 1 out-of-scope finding |
| Gratuitous drift rate | % of commits with ≥ 1 *gratuitous or harmful* finding |
| Out-of-scope line share | Out-of-scope changed lines ÷ all changed lines |
| Scope false-positive rate | % of commits whose *only* findings are *necessary*. This is what enforce mode would wrongly block |
| Scope authoring cost | Median minutes to write one scope |

This study alone answers whether the problem is real at all. If the gratuitous drift rate is under about 5%, the scope gate is solving a problem this runner barely has, and the effort should go to documentation and the index instead.

### Study 3: prospective A/B runs (live agent, a few nights)

1. Take a backlog of **at least 20 new issues**, all written with `scope:`. For arm A, strip the Scope lines (or point arm A at a copy of the backlog without them).
2. Run each arm from the **same base commit**, on its own work branch, with the same agent, model and machine. Alternate which arm runs first each night, so time of day and quota state don't favour one arm.
3. Model output varies from run to run. If the budget allows, run each arm twice and compare per-issue averages. Always report raw counts as well as percentages, since the samples are small.

## Metrics by goal

| Goal | Metric | Source | Better is |
|---|---|---|---|
| **1. Isolation** | Drift rate and gratuitous drift rate (as in Study 2) | integrity records + manual labels | lower |
| | Out-of-scope lines per committed issue | integrity records | lower |
| | % of out-of-scope findings with a `SCOPE_NOTES` justification | integrity records | higher |
| | Scope false-positive rate (arm C) | integrity records + manual labels | lower |
| **2. Documentation** | Report/diff agreement: every changed component appears in the report | integrity records vs `git show` | 100% (checked automatically) |
| | Reviewer comprehension: given a commit, answer *what changed, why, and where you'd start debugging*. Diff only vs diff + report. Time and correctness scored | timed review of 10 commits | faster, more correct |
| | Reviewer rating of the report (1–5: "I understand what was built") | same session | higher |
| **3. Tokens** | Input tokens (total **and** uncached) per *committed* issue | `turn.completed` usage in attempt logs | lower |
| | Output tokens per committed issue | same | lower |
| | Navigation commands per attempt (`rg`, `grep`, `cat`, `sed -n`, `ls`, `Get-Content` ...) | `command_execution` items in attempt logs | lower |
| | Distinct files read per attempt | same | lower |
| **4. Unattended reliability** | Completion rate: committed ÷ attempted issues | run state, git log | higher |
| | Attempts per committed issue | run state | lower |
| | Deferral rate, and deferrals *caused by the scope gate* | run state, validation files | lower |
| | Follow-up fixes: later commits within the next 10 issues that modify a component an earlier issue touched *outside its scope* | integrity records + git | lower |
| | Tests on the merged branch still passing after the run | re-run the suite on the final commit | 100% |
| **Overhead** | Gate time per attempt, time to write scopes, time to keep tags up to date | timings | small next to the savings |

Count tokens per *committed* issue, not per attempt. An arm that fails cheaply and often shouldn't look efficient. Report uncached input separately, because prompt caching hides most of the real context reading (one past attempt logged 920k input tokens, 869k of them cached).

## Decision rules

Decide on these before running anything, so the results can't be argued into a conclusion afterwards.

| Decision | Go if |
|---|---|
| **Keep the scope gate in report mode** | Study 2 gratuitous drift rate ≥ 10%, **or** B cuts gratuitous drift by ≥ 50% vs A with completion rate no more than 5 points lower |
| **Switch to enforce mode** | Study 1 meets its targets **and** Study 2 scope false-positive rate ≤ 10% **and** C's completion rate is within 5 points of B's |
| **Keep the component index** | D uses ≥ 15% fewer input tokens per committed issue than B (total and uncached), with completion rate no lower |
| **Keep the run reports** | Reviewers answer the comprehension questions faster **or** more accurately with the report, and rate it ≥ 4/5 on average |
| **Start office-format adapters** | The scope gate earned "keep" on code first. A tool that can't show value where diffs exist won't do better where they don't |
| **Stop / roll back a piece** | It lowers completion rate by > 10 points, or raises attempts per committed issue by > 30%, or its overhead exceeds its measured savings |

## Threats to validity

- **Run-to-run variation.** The same issue can succeed once and fail the next time. Use paired, per-issue comparisons and repeat runs where you can.
- **Issue mix.** A/B arms run the same issues, so difficulty is controlled. Across the retrospective study, report by issue size.
- **Who writes the scope.** If the person writing scopes has already seen the diff, drift will look artificially low. Write scopes blind (Study 2) or before the run (Study 3).
- **Caching.** Total input tokens are dominated by cache hits. Always report uncached input too.
- **Prompt effect vs gate effect.** Arm B changes both the issue text (a Scope line) and the agent's instructions (SCOPE_NOTES). That's intended: B measures "declaring scope", and C adds blocking on top.
- **Small samples.** 20–30 issues will show large effects, not small ones. Treat a difference under about 10 points as "no evidence either way".

## Data collection

Everything needed is already written to disk by a run:

| Data | Where |
|---|---|
| Per-attempt prompt, log (tokens, commands), agent result, validation output | `%LOCALAPPDATA%\issue-runner\runs\<run>\attempt_*` |
| Issue outcomes, retries, deferrals, blockers | `<run>\supervisor_state.json`, `SUPERVISOR_SUMMARY.md` |
| Scope verdicts and findings | `<run>\integrity\scope_issue-<N>_attempt-<K>.json` (and `.md`) |
| What was committed | `git log --grep "Closes #"` on the work branch |

`engine/compare_runs.py` collects these into one table per arm (see its `--help`). Manual labels (necessary / gratuitous / harmful, and the comprehension scores) go in a CSV next to the results.

---

## Results so far

### Study 2: retrospective drift (2026-10-09)

**Data:** 230 runner commits from two projects, one mostly PHP/JS and one mostly Python/Dart.
- Scopes were written blind from the issue text and the pre-change codebase.
- The gate ran in report mode, and every out-of-scope finding was labelled by hand.
- Scopes and labels were produced by LLM subagents, one labeller per batch.

| Metric | Result | Decision threshold |
|---|---|---|
| Commits with any out-of-scope change | 49% | |
| Commits with a gratuitous or harmful change | **4%** (9 commits) | keep the gate if ≥ 10% |
| Commits with a harmful change | 1% (3 commits) | |
| Commits enforce mode would block wrongly (every finding necessary or harmless) | **45%** | enforce only if ≤ 10% |
| Out-of-scope share of changed lines | 6% | |

| Out-of-scope finding was... | Share |
|---|---|
| Required, discoverable only while implementing (a caller, a shared helper, a migration table) | 40% |
| Harmless and on-topic (mostly docs updated for the same change) | 34% |
| Required, and the scope writer should have listed it | 22% |
| Gratuitous | 3% |
| Harmful | 1% |

**Conclusions:**
- **Don't enforce scope.** It would have blocked half of all commits, almost all of them wrongly.
- **Scope drift is rare here.** At 4% of commits, gratuitous drift is below the bar for keeping a drift gate.
- **The gate is a poor detector of bad changes.** Even with docs allowed, only about 9% of flagged commits contained a gratuitous or harmful change.
- **The harmful changes had one shape.** A file was rewritten and functions or constants were deleted that other files still used, and the test suites didn't catch it. A targeted check finds that with no scope at all.

**Revised position.** Scope is kept as a **record and explanation of change**. It tells the agent where to start, and it says why the boundary moved: a ripple effect, a doc update, or something unexplained. Safety comes from **narrow deterministic checks** for specific failure modes, starting with the dangling reference check (`integrity.py refs`).

**Hypothesis for Study 3:** among out-of-scope changes, the ones the agent did *not* explain under `SCOPE_NOTES` will hold most of the gratuitous and harmful ones. If that holds, "unexplained out-of-scope" becomes a useful review signal without blocking anything.

**Changes made from these results:**
- Docs and `.env.example`, plus test files in other languages, are allowed by default.
- Agent configuration files (`CLAUDE.md`, `AGENTS.md`, `.claude/`, `.mcp.json`) and backup files are always flagged, and the runner refuses to commit `.claude/`, `.mcp.json` and backups.
- Module-level constants are now components that a scope can name.
- The dangling reference check was added.

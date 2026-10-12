This is an ML experiment repo. Each issue is **one experiment**. It is committed only when the deterministic ML gate approves it and the adversarial reviewer does not veto it. The repo's AGENTS.md, if present, is also binding.

## The eval protocol is frozen

- Once they exist, **never edit** `src/evaluate.py`, `src/metrics.py`, `configs/split.json`, or anything under `data/test/` or `data/splits/`. The gate rejects changes to them and hash-checks the test data. (The first "frozen protocol" issue creates them.)
- **Never let validation or test rows reach training.** Fit every transform (scalers, imputers, target or mean encoding, feature selection, vocabularies) on the training fold only. Split grouped or time-ordered data by group or by time.
- **Never tune against the validation score in a loop,** and never report the best of many seeds. Take settings from the issue or from training-fold CV.
- Set every random seed. Keep the change scoped to this experiment.
- Stay inside the issue's **Scope:** line; explain any unavoidable change outside it under SCOPE_NOTES.

## Every experiment writes its result

Run the experiment end to end, then write `results/latest.json` from **that** run:

```json
{"metric": "roc_auc", "value": 0.8312, "seed": 42, "split": "val", "notes": "lgbm + target enc"}
```

Also save validation predictions to `results/val_predictions.csv`, so the gate can re-score them with the frozen eval (`python src/evaluate.py --predictions <csv> --json` prints `{"value": x}`). Add `data/`, `results/` and `models/` to `.gitignore`. Never commit data, weights or run logs.

## What passes

- The metric beats the best in `experiments/ledger.json` (or the baseline) by the configured margin. The gate maintains the ledger; never edit it.
- A score above the plausible ceiling, or a bigger one-step jump than allowed, is treated as **leakage** and rejected. Find the leak; do not tune around the gate.
- If the idea does not help, finish with `STATUS: INCOMPLETE` and put the number in `NOTES`. A clean negative result is useful; a result tuned until it passes is not.

## Project-specific notes

<!-- Optional: dataset quirks, allowed libraries, compute limits, the Kaggle competition link. -->

## Decision record

Every issue ends with a decision record committed alongside the code: `docs/decisions/<issue number, 4 digits>-<short-slug>.md` (update it if it already exists). It has two readers: a person jumping in (In short, How it works, Main design choice) and a later agent jumping back in (Main design choice, Traps, Components, Breaks, Not verified). Records are claims to check against the code, not proof: write them so a later agent can test each claim quickly. Only state what actually happened: don't list tests you didn't run, alternatives you didn't weigh, or edge cases you didn't check; put anything you believe but did not test or measure under Not verified. How it works is for people: plain-language numbered steps, no function names or `code`. Everywhere else name code as `path::Name`. Every numeric or timing claim cites its source, a constant `path::NAME` or a config key, e.g. "resyncs every ~2 h (`fw/config.h::RESYNC_EVERY` x `CYCLE_S`)". Do not restate what the code comments already say. Aim for 600-1200 words (150-2000 accepted): pay for How it works and Main design choice by keeping Components to a few words per line and Traps to the real ones. Leave out a section only by saying why ("None found" is fine). A change that breaks or degrades existing behaviour goes under Breaks or needs follow-up, not under Trade-offs; file or describe the follow-up. Do not mark your own claims [checked] or [tested]: the tool sets evidence levels.

```
# <NNNN>: <title>
Status: implemented | partial | blocked · Issue: #<N> · Date: <YYYY-MM-DD>
## In short            (2-3 plain sentences: what exists now that didn't before)
## How it works        (for people: 100-200 words, numbered steps of the whole pipeline end to end, in plain language; no function names, no `code`)
## Main design choice  (the key decision; the 1-2 real alternatives that were seriously possible; why this one won; what would make the other better)
## Traps: why the obvious fix is wrong   (non-obvious constraints a later agent might "simplify" back into a bug, each with its reason; or "None found")
## Components           (reference list: `path::Name` - a few words each; no prose)
## Breaks or needs follow-up (existing behaviour this change breaks or degrades - an app or caller that no longer works, a removed field, a migration someone must run - each with the follow-up filed or described; "None")
## Problem and constraints
## Alternatives considered   (other, smaller choices: option - why not chosen; or "None considered")
## Trade-offs           (what this gives up, and when that would start to matter)
## Key parameters       (each threshold/constant/default: value with its source `path::NAME`, how chosen, effect of raising/lowering; "None")
## Assumptions          (what was taken as true, and what breaks if it isn't)
## Edge cases           (- Handled: ... / - Not handled: ... with the symptom a user would see)
## Not verified         (claims or behaviours you did not test or measure; "None")
## Changes outside the scope (same as SCOPE_NOTES)
## How it was verified  (tests run or added, with names)
## To change this       (concrete options as "to favour X, change Y from A to B; cost: Z"; likely bug sources)
## Rollback             (what depends on this; what to check if reverted)
```

For an experiment, In short states the hypothesis, and How it was verified gives the result against the frozen eval, including negative results.

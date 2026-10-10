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

Every issue ends with a decision record committed alongside the code: `docs/decisions/<issue number, 4 digits>-<short-slug>.md` (update it if it already exists). Write it for someone who will never open the code. Only state what actually happened: don't list tests you didn't run, alternatives you didn't weigh, or edge cases you didn't check ("Not checked" is fine). Name code as `path::Name`. Aim for 300-700 words; leave out a section only by saying why.

```
# <NNNN>: <title>
Status: implemented | partial | blocked · Issue: #<N> · Date: <YYYY-MM-DD>
## In short            (2-3 plain sentences: what exists now that didn't before)
## Problem and constraints
## Approach             (how it works, plainly; name components as path::Name)
## Alternatives considered   (table: option | why not chosen; or "None considered")
## Trade-offs           (what this gives up, and when that would start to matter)
## Key parameters       (every threshold, constant, default or limit you chose: value, how chosen, effect of raising/lowering; "None" if no tunable values)
## Assumptions          (what was taken as true, and what breaks if it isn't)
## Edge cases           (handled / not handled; give the symptom a user would see)
## Changes outside the scope (same as SCOPE_NOTES)
## How it was verified  (tests run or added; what remains untested)
## To change this       (concrete options as "to favour X, change Y from A to B; cost: Z"; likely bug sources)
## Rollback             (what depends on this; what to check if reverted)
```

For an experiment, Approach states the hypothesis, and How it was verified gives the result against the frozen eval, including negative results.

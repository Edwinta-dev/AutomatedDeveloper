This is an ML experiment repo. Each issue is **one experiment**. It is committed only when the deterministic ML gate approves it and the adversarial reviewer does not veto it. The repo's AGENTS.md, if present, is also binding.

## The eval protocol is frozen

- Once they exist, **never edit** `src/evaluate.py`, `src/metrics.py`, `configs/split.json`, or anything under `data/test/` or `data/splits/`. The gate rejects changes to them and hash-checks the test data. (The first "frozen protocol" issue creates them.)
- **Never let validation or test rows reach training.** Fit every transform (scalers, imputers, target or mean encoding, feature selection, vocabularies) on the training fold only. Split grouped or time-ordered data by group or by time.
- **Never tune against the validation score in a loop,** and never report the best of many seeds. Take settings from the issue or from training-fold CV.
- Set every random seed. Keep the change scoped to this experiment.

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

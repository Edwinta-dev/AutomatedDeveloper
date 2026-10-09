#!/usr/bin/env python3
"""
ml_gate.py — deterministic gate for ML experiment issues (the role that CAN approve).

In the three-role model the adversary (adversary.py) can only veto. For ML the
things that must never slip through are checked here, in code, so no LLM
judgement is needed to catch them:

  1. Protected paths    eval script, metric code, split manifests: once they exist
                        at HEAD, any change or deletion fails. (Creating them is
                        allowed, so the first "build the protocol" issue can run.)
  2. Protected data     test/holdout data, usually gitignored: sha256-locked on the
                        first passing attempt (or `v2.py run` / `--lock`), then
                        re-verified on every attempt.
  3. Results contract   results_file exists, names the configured metric, holds a
                        finite value and the required fields (e.g. seed).
  4. Freshness          results were written during THIS attempt, not reused.
  5. Reproduction       optional: the gate re-runs the frozen eval itself and the
                        claimed value must match within tolerance.
  6. Plausibility       absolute bounds (e.g. above the public leaderboard best is
                        almost surely leakage) and a max single-step jump.
  7. Improvement        must beat the best committed result (ledger at HEAD, else
                        the baseline) by min_delta, if require_improvement.

On PASS the gate writes the ledger (best + history) into the working tree, so it
is committed with the experiment. The ledger is always rebuilt from HEAD, so an
agent editing it, or a vetoed attempt, cannot inflate the best score.

Settings live in the "ml" section of the project config (a standalone file of
the same keys also works). Wire it into the --validate file BEFORE the adversary:
    {"label": "ML gate (deterministic)",
     "argv": ["__PY__", "__HARNESS__/ml_gate.py", "--config", "__PROJECT_CONFIG__"]}

Exit 0 = pass, 1 = fail (reasons printed; run_issues.py feeds them to the agent's
next attempt), 2 = gate misconfigured.

    python engine/ml_gate.py --config <cfg> --repo <repo> --lock    # (re)hash protected data
    python engine/ml_gate.py --config <cfg> --repo <repo> --status  # show the ledger best
    python engine/ml_gate.py --self-test
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_issues import glob_match  # noqa: E402  (one glob dialect across the harness)

PASS, FAIL, MISCONFIGURED = 0, 1, 2

DEFAULTS = {
    "metric": "",
    "direction": "maximize",          # or "minimize" (rmse, logloss, ...)
    "results_file": "results/latest.json",
    "metric_key": "metric",
    "value_key": "value",
    "required_fields": ["seed"],
    "require_fresh": True,
    "baseline": None,
    "require_improvement": True,
    "min_delta": 0.0,
    "max_jump": None,
    "plausible_min": None,
    "plausible_max": None,
    "protected_paths": [],
    "protected_data": [],
    "lock_file": "",
    "ledger_file": "experiments/ledger.json",
    "reproduce": None,                # {"argv": [...], "tolerance": 1e-6, "timeout_minutes": 30}
}


class GateError(Exception):
    """The gate itself is misconfigured (exit 2), as opposed to a failed check."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        raise GateError(f"--config not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    section = "ml" in data and isinstance(data["ml"], dict)
    if section:
        data = data["ml"]
    cfg = {**DEFAULTS, **{k: v for k, v in data.items() if not k.startswith("//")}}
    if not cfg["metric"] or str(cfg["metric"]).startswith("TODO"):
        raise GateError("config must name the 'metric' the results file reports")
    if cfg["direction"] not in ("maximize", "minimize"):
        raise GateError("direction must be 'maximize' or 'minimize'")
    # The lock lives beside the config, outside the repo the agent can edit.
    default_lock = "ml_gate.lock.json" if section else f"{path.stem}.lock.json"
    cfg["lock_file"] = str(path.parent / (cfg["lock_file"] or default_lock))
    return cfg


# ---------------------------------------------------------------------------
# git + filesystem helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), text=True, encoding="utf-8",
                          errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def changed_files(repo: Path) -> list[str]:
    head = _git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode == 0
    tracked = (_git(repo, "diff", "--name-only", "HEAD") if head
               else _git(repo, "ls-files")).stdout
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard").stdout
    return sorted({x.strip() for x in (tracked + "\n" + untracked).splitlines() if x.strip()})


def _glob_base(pattern: str) -> str:
    parts = []
    for part in pattern.replace("\\", "/").split("/"):
        if any(c in part for c in "*?["):
            break
        parts.append(part)
    return "/".join(parts)


def expand_globs(repo: Path, patterns: list[str]) -> list[str]:
    """Files under repo matching any pattern. Walks only each pattern's literal
    prefix, so big trees (.venv, data lakes) elsewhere are never scanned."""
    found: set[str] = set()
    for pat in patterns:
        base = repo / _glob_base(pat)
        if base.is_file():
            cands = [base]
        elif base.is_dir():
            cands = [p for p in base.rglob("*") if p.is_file()]
        else:
            cands = []
        for p in cands:
            rel = p.relative_to(repo).as_posix()
            if glob_match(rel, pat):
                found.add(rel)
    return sorted(found)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Checks (each returns a list of failure strings)
# ---------------------------------------------------------------------------

def check_protected_paths(repo: Path, changed: list[str], cfg: dict) -> list[str]:
    """Changing or deleting a protected file that exists at HEAD fails. Creating
    one is allowed: that is the setup issue building the frozen protocol."""
    ledger = cfg["ledger_file"].replace("\\", "/")
    hits = [f for f in changed if f != ledger
            and any(glob_match(f, g) for g in cfg["protected_paths"])
            and _git(repo, "cat-file", "-e", f"HEAD:{f}").returncode == 0]
    return [f"protected path modified: {f} (the eval protocol is frozen; revert it)"
            for f in hits]


def make_lock(repo: Path, cfg: dict) -> dict:
    files = expand_globs(repo, cfg["protected_data"])
    lock = {"created": dt.datetime.now(dt.timezone.utc).isoformat(),
            "patterns": cfg["protected_data"],
            "files": {f: sha256(repo / f) for f in files}}
    Path(cfg["lock_file"]).write_text(json.dumps(lock, indent=2), encoding="utf-8")
    return lock


def check_protected_data(repo: Path, cfg: dict) -> Optional[list[str]]:
    """Failures, or None when there is no lock yet (the gate locks on PASS)."""
    if not cfg["protected_data"]:
        return []
    lock_path = Path(cfg["lock_file"])
    if not lock_path.exists():
        return None
    locked: dict = json.loads(lock_path.read_text(encoding="utf-8")).get("files", {})
    now = expand_globs(repo, cfg["protected_data"])
    out = []
    for f, digest in locked.items():
        if not (repo / f).is_file():
            out.append(f"protected data missing: {f}")
        elif sha256(repo / f) != digest:
            out.append(f"protected data changed: {f} (test/holdout data must not be touched)")
    out += [f"new file under protected data: {f}" for f in now if f not in locked]
    return out


def read_results(repo: Path, cfg: dict) -> tuple[Optional[float], dict, list[str]]:
    path = repo / cfg["results_file"]
    if not path.is_file():
        return None, {}, [f"results file missing: {cfg['results_file']} "
                          "(the experiment must write it; see the project's AGENTS.md)"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, {}, [f"results file is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, {}, ["results file must hold a JSON object"]
    errs = []
    if data.get(cfg["metric_key"]) != cfg["metric"]:
        errs.append(f"results report metric {data.get(cfg['metric_key'])!r}, expected "
                    f"{cfg['metric']!r} (the metric is fixed by the gate config)")
    raw = data.get(cfg["value_key"])
    value = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw):
        value = float(raw)
    else:
        errs.append(f"results {cfg['value_key']!r} must be a finite number, got {raw!r}")
    errs += [f"results missing required field {k!r}" for k in cfg["required_fields"]
             if k not in data]
    if cfg["require_fresh"]:
        started = float(os.environ.get("AGENT_ATTEMPT_STARTED") or 0)
        if started and path.stat().st_mtime < started - 1:
            errs.append("results file predates this attempt; re-run the experiment so the "
                        "reported number comes from the code being committed")
    return value, data, errs


def _last_json(text: str):
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") or _is_number(line):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def reproduce(repo: Path, cfg: dict, claimed: float) -> tuple[Optional[float], list[str]]:
    rep = cfg.get("reproduce")
    if not rep:
        return None, []
    argv = [sys.executable if a == "__PY__" else str(a) for a in rep["argv"]]
    timeout = float(rep.get("timeout_minutes", 30)) * 60
    try:
        cp = subprocess.run(argv, cwd=str(repo), text=True, encoding="utf-8", errors="replace",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, [f"reproduction timed out after {timeout / 60:g} min"]
    except FileNotFoundError:
        raise GateError(f"reproduce command not found: {argv[0]}")
    if cp.returncode != 0:
        return None, [f"reproduction command failed (exit {cp.returncode}):\n{cp.stdout[-2000:]}"]
    got = _last_json(cp.stdout)
    if isinstance(got, dict):
        got = got.get(cfg["value_key"])
    if not isinstance(got, (int, float)) or isinstance(got, bool) or not math.isfinite(got):
        return None, ["reproduction printed no metric (last stdout line must be a number or "
                      f"a JSON object with {cfg['value_key']!r})"]
    tol = float(rep.get("tolerance", 1e-6))
    if abs(float(got) - claimed) > tol:
        return float(got), [f"claimed {cfg['metric']}={claimed} but the frozen eval reproduces "
                            f"{got} (tolerance {tol})"]
    return float(got), []


def ledger_at_head(repo: Path, cfg: dict) -> dict:
    cp = _git(repo, "show", f"HEAD:{cfg['ledger_file'].replace(chr(92), '/')}")
    if cp.returncode != 0:
        return {}
    try:
        data = json.loads(cp.stdout)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def judge(value: float, best: Optional[float], cfg: dict) -> tuple[list[str], Optional[float], bool]:
    """Plausibility + improvement. Returns (failures, delta, improved)."""
    errs = []
    lo, hi = cfg["plausible_min"], cfg["plausible_max"]
    if hi is not None and value > hi:
        errs.append(f"{cfg['metric']}={value} is above plausible_max={hi}: almost certainly "
                    "leakage or an eval bug. Find it; do not tune around it.")
    if lo is not None and value < lo:
        errs.append(f"{cfg['metric']}={value} is below plausible_min={lo}: the pipeline is "
                    "probably broken.")
    if best is None:
        return errs, None, True
    sign = 1.0 if cfg["direction"] == "maximize" else -1.0
    delta = sign * (value - best)
    improved = delta > 0 and delta >= float(cfg["min_delta"])
    if cfg["max_jump"] is not None and delta > float(cfg["max_jump"]):
        errs.append(f"one-step gain {delta:+.6g} exceeds max_jump={cfg['max_jump']}: "
                    "treat as leakage until proven otherwise.")
    if cfg["require_improvement"] and not improved:
        errs.append(f"no improvement: {cfg['metric']}={value} vs best {best} "
                    f"(needs {'+' if sign > 0 else '-'}{cfg['min_delta']} "
                    f"in the {cfg['direction']} direction)")
    return errs, delta, improved


def write_ledger(repo: Path, cfg: dict, head: dict, value: float, delta: Optional[float],
                 improved: bool, data: dict) -> dict:
    entry = {
        "issue": os.environ.get("AGENT_ISSUE_NUMBER") or None,
        "title": os.environ.get("AGENT_ISSUE_TITLE") or None,
        "value": value, "delta": delta, "improved": improved,
        "seed": data.get("seed"),
        "parent": _git(repo, "rev-parse", "--short", "HEAD").stdout.strip() or None,
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "results": {k: v for k, v in data.items() if not isinstance(v, (list, dict))},
    }
    ledger = {"metric": cfg["metric"], "direction": cfg["direction"],
              "best": head.get("best"), "history": list(head.get("history", [])) + [entry]}
    if improved or not ledger["best"]:
        ledger["best"] = entry
    path = repo / cfg["ledger_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    return ledger


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def gate(repo: Path, cfg: dict) -> tuple[int, list[str]]:
    report: list[str] = []
    fails: list[str] = []
    fails += check_protected_paths(repo, changed_files(repo), cfg)
    data_fails = check_protected_data(repo, cfg)
    fails += data_fails or []

    value, data, errs = read_results(repo, cfg)
    fails += errs
    if value is not None and not errs:
        got, errs = reproduce(repo, cfg, value)
        fails += errs
        if got is not None and not errs:
            report.append(f"reproduced {cfg['metric']}={got}")
            value = got                      # the gate's own number is the one of record

    head = ledger_at_head(repo, cfg)
    best_entry = head.get("best") or {}
    best = best_entry.get("value", cfg["baseline"])
    report.append(f"best so far: {best if best is not None else '(none)'}"
                  f"{' (baseline)' if not best_entry and best is not None else ''}")

    delta, improved = None, False
    if value is not None:
        errs, delta, improved = judge(value, best, cfg)
        fails += errs
        report.append(f"this attempt: {cfg['metric']}={value}"
                      + (f" (delta {delta:+.6g})" if delta is not None else ""))

    if fails:
        return FAIL, report + [f"FAIL: {f}" for f in fails] + ["ML_GATE: FAIL"]
    write_ledger(repo, cfg, head, value, delta, improved, data)
    if data_fails is None and expand_globs(repo, cfg["protected_data"]):
        lock = make_lock(repo, cfg)
        report.append(f"locked {len(lock['files'])} protected data file(s) -> {cfg['lock_file']} "
                      "(first pass; later attempts must leave them unchanged)")
    report.append(f"ledger updated: {cfg['ledger_file']}"
                  + (" (new best)" if improved else " (recorded, not a new best)"))
    return PASS, report + ["ML_GATE: PASS"]


# ---------------------------------------------------------------------------
# Self-test (offline: a throwaway git repo, no network)
# ---------------------------------------------------------------------------

def self_test() -> int:
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}")
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        conf_dir = Path(d) / "conf"
        repo.mkdir(); conf_dir.mkdir()
        g = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (repo / "src").mkdir(); (repo / "data" / "test").mkdir(parents=True)
        (repo / "src" / "evaluate.py").write_text("# frozen eval\n", encoding="utf-8")
        (repo / "data" / "test" / "y.csv").write_text("id,y\n1,0\n", encoding="utf-8")
        (repo / ".gitignore").write_text("data/\n", encoding="utf-8")
        g("add", "-A"); g("commit", "-qm", "init")

        cfg_path = conf_dir / "ml_gate.json"
        cfg_path.write_text(json.dumps({
            "metric": "auc", "baseline": 0.70, "min_delta": 0.001, "max_jump": 0.05,
            "plausible_max": 0.90, "protected_paths": ["src/evaluate.py"],
            "protected_data": ["data/test/**"]}), encoding="utf-8")
        cfg = load_config(cfg_path)

        def results(v, **extra):
            (repo / "results").mkdir(exist_ok=True)
            (repo / "results" / "latest.json").write_text(
                json.dumps({"metric": "auc", "value": v, "seed": 1, **extra}), encoding="utf-8")

        def commit():
            g("add", "-A"); g("commit", "-qm", "x")

        results(0.60)
        rc, _ = gate(repo, cfg)
        check("failing attempt does not lock data", rc == FAIL and not Path(cfg["lock_file"]).exists())

        results(0.72)
        (repo / "src" / "new_protected.py").write_text("x = 1\n", encoding="utf-8")
        rc, rep = gate(repo, {**cfg, "protected_paths": ["src/evaluate.py", "src/new_protected.py"]})
        check("beats baseline -> pass + ledger", rc == PASS
              and json.loads((repo / cfg["ledger_file"]).read_text())["best"]["value"] == 0.72)
        check("creating a protected file is allowed (setup issue)", rc == PASS)
        check("first pass locks protected data", Path(cfg["lock_file"]).exists())
        commit()

        results(0.7201)
        rc, rep = gate(repo, cfg)
        check("gain below min_delta -> fail", rc == FAIL and any("no improvement" in r for r in rep))

        results(0.73)
        (repo / cfg["ledger_file"]).write_text('{"best": {"value": 0.1}}', encoding="utf-8")
        rc, _ = gate(repo, cfg)
        led = json.loads((repo / cfg["ledger_file"]).read_text())
        check("tampered ledger rebuilt from HEAD",
              rc == PASS and len(led["history"]) == 2 and led["best"]["value"] == 0.73)
        commit()

        results(0.95)
        rc, rep = gate(repo, cfg)
        check("above plausible_max -> fail",
              rc == FAIL and any("plausible_max" in r for r in rep))
        results(0.80)
        rc, rep = gate(repo, cfg)
        check("jump over max_jump -> fail", rc == FAIL and any("max_jump" in r for r in rep))

        results(0.74)
        (repo / "src" / "evaluate.py").write_text("# hacked\n", encoding="utf-8")
        rc, rep = gate(repo, cfg)
        check("protected eval edit -> fail", rc == FAIL and any("evaluate.py" in r for r in rep))
        g("checkout", "--", "src/evaluate.py")

        (repo / "data" / "test" / "y.csv").write_text("id,y\n1,1\n", encoding="utf-8")
        rc, rep = gate(repo, cfg)
        check("locked test data edit -> fail", rc == FAIL and any("data changed" in r for r in rep))
        (repo / "data" / "test" / "y.csv").write_text("id,y\n1,0\n", encoding="utf-8")

        (repo / "results" / "latest.json").write_text(
            json.dumps({"metric": "accuracy", "value": 0.74, "seed": 1}), encoding="utf-8")
        rc, rep = gate(repo, cfg)
        check("metric switched -> fail", rc == FAIL and any("expected 'auc'" in r for r in rep))

        results(0.74)
        os.environ["AGENT_ATTEMPT_STARTED"] = str(9e9)
        try:
            rc, rep = gate(repo, cfg)
        finally:
            os.environ.pop("AGENT_ATTEMPT_STARTED", None)
        check("stale results -> fail", rc == FAIL and any("predates" in r for r in rep))

        cfg_rep = {**cfg, "reproduce": {"argv": ["__PY__", "-c", "print('{\"value\": 0.7399}')"],
                                         "tolerance": 1e-6}}
        rc, rep = gate(repo, cfg_rep)
        check("reproduction mismatch -> fail", rc == FAIL and any("reproduces" in r for r in rep))
        cfg_rep["reproduce"]["tolerance"] = 1e-3
        rc, rep = gate(repo, cfg_rep)
        check("reproduced value is the one of record", rc == PASS and json.loads(
            (repo / cfg["ledger_file"]).read_text())["best"]["value"] == 0.7399)

        cfg_min = {**cfg, "direction": "minimize", "baseline": 0.5, "max_jump": None,
                   "plausible_max": None, "ledger_file": "experiments/rmse.json"}
        results(0.45)
        check("minimize: lower beats baseline", gate(repo, cfg_min)[0] == PASS)

    check("glob base prefix", _glob_base("data/test/**/*.csv") == "data/test")

    with tempfile.TemporaryDirectory() as d:
        pc = Path(d) / "issue-automation.config.json"
        pc.write_text(json.dumps({"repo": ".", "ml": {"metric": "rmse", "direction": "minimize"}}),
                      encoding="utf-8")
        c = load_config(pc)
        check("reads the project config's ml section",
              c["metric"] == "rmse" and c["lock_file"].endswith("ml_gate.lock.json"))
        pc.write_text(json.dumps({"ml": {"metric": "TODO e.g. roc_auc"}}), encoding="utf-8")
        try:
            load_config(pc)
            check("TODO metric rejected", False)
        except GateError:
            check("TODO metric rejected", True)
    print("\n" + ("ALL ML GATE SELF-TESTS PASSED" if ok else "SOME ML GATE SELF-TESTS FAILED"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic ML experiment gate (exit 0 pass, 1 fail).")
    ap.add_argument("--config", help="ml_gate config (.json/.yaml)")
    ap.add_argument("--repo", default=".", help="repository (default: cwd, as run_issues.py sets)")
    ap.add_argument("--lock", action="store_true", help="(re)hash protected_data into the lock file")
    ap.add_argument("--status", action="store_true", help="print the committed ledger best")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.config:
        ap.error("--config is required (or use --self-test)")
    repo = Path(args.repo).expanduser().resolve()
    try:
        cfg = load_config(Path(args.config).expanduser().resolve())
        if args.lock:
            lock = make_lock(repo, cfg)
            print(f"locked {len(lock['files'])} file(s) -> {cfg['lock_file']}")
            return PASS
        if args.status:
            print(json.dumps(ledger_at_head(repo, cfg).get("best"), indent=2))
            return PASS
        rc, report = gate(repo, cfg)
    except GateError as exc:
        print(f"ML_GATE: MISCONFIGURED — {exc}")
        return MISCONFIGURED
    print("\n".join(report))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

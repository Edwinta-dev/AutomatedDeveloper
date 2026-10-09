#!/usr/bin/env python3
"""
session.py — agenticworkflow_v2 unattended session for ONE project.

Runs a single project's per-issue supervisor (run_issues.py, "v1") through the
night and — the reason this layer exists — makes it survive usage limits:

  * v1 runs in slices (default 50 min). Every slice continues the SAME v1 run
    (--resume-or-new), so retry counts, deferrals and a half-finished issue
    carry across slices instead of restarting.
  * v1 is told to EXIT on a usage limit (--on-usage-limit exit) and reports the
    parsed reset in its RUN_RESULT line. This wrapper then PARKS the provider,
    sleeps until the reset, and resumes. An optional probe command can park
    proactively before a slice starts.
  * Stops when the project has no remaining work, when everything left is
    deferred or blocked (a human is needed), after repeated errors, or when the
    optional total time budget is hit.

Run several projects by opening several terminals, each:
    python session.py --project ProjectA --provider claude
    python session.py --config path/to/projectB.config.json --provider codex
No multi-project scheduling lives here on purpose — that's just more processes.

The three-role model (deterministic gate can approve · adversary can only veto ·
expensive agent proposes) is configured in the PROJECT's --validate file: list
your gate commands AND adversary.py there. This wrapper only keeps the run alive.

`python session.py --self-test` runs offline checks (no subprocess, no network).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from usage import UsageManager, now_utc
from run_issues import (RUN_RESULT_PREFIX, USAGE_LIMIT_EXIT, SupervisorError,
                        load_config_file, platform_root, project_config_path)

IS_WINDOWS = os.name == "nt"

# RUN_RESULT reasons (see run_issues.finalize) and what the session does next.
CONTINUE_REASONS = {"max_hours", "all_closed"}      # all_closed: next slice freezes a new run
DONE_REASONS = {"no_work"}
NEEDS_HUMAN_REASONS = {"all_deferred", "all_blocked", "env_blocked"}
MAX_CONSECUTIVE_ERRORS = 3


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested; no subprocess)
# ---------------------------------------------------------------------------

def parse_run_result(text: str) -> Optional[dict]:
    """The last RUN_RESULT line v1 printed, or None (crash / killed)."""
    found = None
    for line in (text or "").splitlines():
        if line.startswith(RUN_RESULT_PREFIX):
            try:
                found = json.loads(line[len(RUN_RESULT_PREFIX):])
            except json.JSONDecodeError:
                pass
    return found


def parse_committed(text: str) -> list[int]:
    return [int(n) for n in re.findall(r"^Committed issue #(\d+) as ", text or "", re.MULTILINE)]


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        return None
    return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)


def decide(result: Optional[dict], returncode: int, killed: bool) -> tuple[str, str]:
    """What to do after a slice. Returns (action, why):
    continue | park | done | needs_human | stop | error."""
    if result is None:
        if killed:
            return "continue", "slice overran and was killed; the next slice resumes it"
        return "error", f"run_issues.py exited {returncode} without a RUN_RESULT"
    reason = result.get("reason", "")
    if reason == "usage_limit" or returncode == USAGE_LIMIT_EXIT:
        return "park", "provider usage limit"
    if reason in DONE_REASONS:
        return "done", result.get("detail") or "no matching open issues"
    if reason in NEEDS_HUMAN_REASONS:
        why = (f"{reason.replace('_', ' ')}: deferred "
               f"{result.get('deferred') or []}, {result.get('blocked', 0)} blocked")
        for key, info in (result.get("env_blockers") or {}).items():
            why += f"\n  {key}: {info.get('hint', '')}"
        return "needs_human", why
    if reason in CONTINUE_REASONS:
        return "continue", reason
    return "stop", reason or "stopped"


def next_action(usage: UsageManager, provider: str, *, done: bool, once: bool,
                ref: Optional[datetime] = None, max_sleep: int = 3600
                ) -> tuple[str, Optional[int]]:
    """Decide the next step without side effects.
    Returns ("run", None) | ("sleep", seconds) | ("stop", None)."""
    ref = ref or now_utc()
    if done:
        return "stop", None
    if usage.available(provider, ref):
        return "run", None
    if once:
        return "stop", None
    target = usage.state(provider).cooling_until
    secs = max(int((target - ref).total_seconds()), 15) if target else 60
    return "sleep", min(secs, max_sleep)


def recheck_cooldown(usage: UsageManager, provider: str, probe: Optional[list[str]],
                     *, cwd: Optional[str] = None) -> bool:
    """While parked, ask the probe again. True (and the cooldown is cleared) when
    the provider is really available; False keeps or refreshes the park.
    Without a probe there is nothing better than the saved time."""
    if not probe:
        return False
    return usage.probe(provider, probe, cwd=cwd)


def default_grace_minutes(config: dict) -> int:
    """A slice only checks its budget between issues, so one worker plus its
    validation can run past it. Allow max_session_minutes + 15 before killing."""
    merged = {**config, **(config.get("run") or {})}
    return int(merged.get("max_session_minutes", 45)) + 15


# ---------------------------------------------------------------------------
# Running one slice (subprocess)
# ---------------------------------------------------------------------------

@dataclass
class SliceResult:
    returncode: int
    result: Optional[dict]
    committed: list[int]
    killed: bool


def _popen(argv: list[str], cwd: Optional[str]):
    kwargs = dict(cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                  errors="replace", bufsize=1, shell=False,
                  env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([str(a) for a in argv], **kwargs)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            time.sleep(3)
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def slice_argv(runner: list[str], config: str, extra: list[str], slice_minutes: int) -> list[str]:
    slice_h = max(slice_minutes, 1) / 60.0
    return (list(runner) + ["--config", config, "--max-hours", f"{slice_h:.4f}",
                            "--resume-or-new", "--on-usage-limit", "exit"] + extra)


def run_slice(argv: list[str], *, slice_minutes: int, grace_minutes: int,
              cwd: Optional[str]) -> SliceResult:
    print(f"\n=== slice {slice_minutes}m :: {' '.join(map(str, argv))}", flush=True)
    deadline = time.monotonic() + (slice_minutes + grace_minutes) * 60
    tail: deque[str] = deque(maxlen=2000)
    killed = False
    proc = _popen(argv, cwd)
    try:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if line:
                sys.stdout.write(line); sys.stdout.flush()
                tail.append(line)
            elif proc.poll() is not None:
                break
            if time.monotonic() > deadline and proc.poll() is None:
                print("--- slice overran; killing subtree (the next slice resumes it)", flush=True)
                _kill_tree(proc); killed = True; break
        proc.wait(timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"--- slice error: {exc}", flush=True)
        _kill_tree(proc); killed = True
    finally:
        if proc.poll() is None:
            _kill_tree(proc); killed = True

    text = "".join(tail)
    return SliceResult(returncode=proc.returncode if proc.returncode is not None else -1,
                       result=parse_run_result(text), committed=parse_committed(text),
                       killed=killed)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def default_state_path(config: str) -> Path:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    d = Path(base) / "agenticworkflow_v2"
    d.mkdir(parents=True, exist_ok=True)
    resolved = Path(config).expanduser().resolve()
    # Projects all name their file issue-automation.config.json, so key by the
    # folder name plus a hash of the full path, never by the file stem alone.
    digest = hashlib.sha1(str(resolved).lower().encode("utf-8")).hexdigest()[:8]
    return d / f"{resolved.parent.name}-{digest}.session.json"


def save_state(path: Path, usage: UsageManager, provider: str, stats: dict) -> None:
    path.write_text(json.dumps({"provider": provider, "cooldowns": usage.snapshot(),
                                "stats": stats}, indent=2), encoding="utf-8")


def load_state(path: Path, usage: UsageManager) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    usage.restore(data.get("cooldowns", {}))
    return data.get("stats", {})


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(*, runner: list[str], config: str, extra: list[str], provider: str,
        probe: Optional[list[str]], slice_minutes: int, grace_minutes: int,
        usage_fallback_minutes: int, max_hours: float, once: bool,
        cwd: Optional[str], state_path: Path, fresh: bool) -> int:
    usage = UsageManager(fallback_minutes=usage_fallback_minutes)
    if fresh and state_path.exists():
        state_path.unlink()
    stats = load_state(state_path, usage)
    stats.setdefault("slices", 0)
    stats.setdefault("committed", [])
    done = False
    exit_code = 0
    errors = 0
    hard_deadline = time.monotonic() + max_hours * 3600 if max_hours > 0 else None
    print(f"session: config={config} provider={provider} slice={slice_minutes}m "
          f"grace={grace_minutes}m state={state_path}", flush=True)

    while True:
        if hard_deadline and time.monotonic() > hard_deadline:
            print("session: total time budget reached; stopping.", flush=True)
            break

        action, secs = next_action(usage, provider, done=done, once=once)
        if action == "stop":
            break
        if action == "sleep":
            # A saved cooldown can be stale or mis-parsed; real usage beats it.
            if recheck_cooldown(usage, provider, probe, cwd=cwd):
                print(f"session: probe reports {provider} available "
                      f"({usage.state(provider).last_detail or 'ok'}); "
                      "clearing the saved cooldown.", flush=True)
                save_state(state_path, usage, provider, stats)
                continue
            target = usage.state(provider).cooling_until
            print(f"session: {provider} parked; sleeping {secs}s "
                  f"(reset {target.isoformat() if target else '?'}).", flush=True)
            time.sleep(secs)
            continue

        # proactive probe right before spending the expensive agent
        if not usage.probe(provider, probe, cwd=cwd):
            t = usage.state(provider).cooling_until
            print(f"session: probe parked {provider} until {t.isoformat() if t else '?'}.", flush=True)
            continue

        res = run_slice(slice_argv(runner, config, extra, slice_minutes),
                        slice_minutes=slice_minutes, grace_minutes=grace_minutes, cwd=cwd)
        stats["slices"] += 1
        stats["committed"] = sorted(set(stats["committed"]) | set(res.committed))
        verdict, why = decide(res.result, res.returncode, res.killed)

        if verdict == "park":
            reset = parse_iso((res.result or {}).get("usage_reset"))
            until = usage.park(provider, reset, detail=why)
            print(f"session: usage limit; parked {provider} until {until.isoformat()}"
                  f"{'' if reset else ' (no reset time given; fallback cooldown)'}. "
                  "Will resume.", flush=True)
        elif verdict == "error":
            errors += 1
            print(f"session: {why} ({errors}/{MAX_CONSECUTIVE_ERRORS}).", flush=True)
            if errors >= MAX_CONSECUTIVE_ERRORS:
                print("session: giving up after repeated errors; fix the cause above and rerun.",
                      flush=True)
                exit_code = 1
                done = True
            elif not once:
                time.sleep(60 * errors)
        else:
            errors = 0
            print(f"session: slice ended ({why}).", flush=True)
            if verdict == "done":
                done = True
                print("session: no remaining work; project complete.", flush=True)
            elif verdict == "needs_human":
                done = True
                exit_code = 2
                print("session: everything left needs a human; see the run summary.", flush=True)
            elif verdict == "stop":
                done = True
        save_state(state_path, usage, provider, stats)
        if once:
            break

    print(f"\nsession: done — {stats['slices']} slice(s), "
          f"{len(stats['committed'])} issue(s) committed"
          f"{': ' + ', '.join(f'#{n}' for n in stats['committed']) if stats['committed'] else ''}")
    return exit_code


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def self_test() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}")
        ok &= bool(cond)

    um = UsageManager()
    check("run when available", next_action(um, "claude", done=False, once=False) == ("run", None))
    check("stop when done", next_action(um, "claude", done=True, once=False) == ("stop", None))

    ref = now_utc()   # park() measures against the real clock; a fixed date goes stale
    um.park("claude", ref + timedelta(minutes=20))
    act, secs = next_action(um, "claude", done=False, once=False, ref=ref)
    check("sleep when parked", act == "sleep" and abs(secs - 20 * 60) < 5)
    check("sleep capped at max", next_action(um, "claude", done=False, once=False, ref=ref,
          max_sleep=300)[1] == 300)
    check("parked + once -> stop", next_action(um, "claude", done=False, once=True, ref=ref) == ("stop", None))
    check("other provider still runs",
          next_action(um, "gemini", done=False, once=False, ref=ref) == ("run", None))

    py = sys.executable
    stale = UsageManager(); stale.park("codex", now_utc() + timedelta(hours=2))
    check("parked + probe ok -> cooldown cleared",
          recheck_cooldown(stale, "codex", [py, "-c", "print('{\"ok\": true}')"])
          and stale.available("codex"))
    held = UsageManager(); held.park("codex", now_utc() + timedelta(hours=2))
    check("parked + probe not ok -> still parked",
          not recheck_cooldown(held, "codex", [py, "-c", "print('{\"ok\": false, \"reset\": \"in 30 minutes\"}')"])
          and not held.available("codex"))
    check("parked + no probe -> trust saved time",
          not recheck_cooldown(held, "claude", None))

    out = ("Committed issue #4 as abc123\nnoise\nCommitted issue #5 as def456\n"
           "the agent wrote: rate limit handling, quota, 429\n"
           'RUN_RESULT {"reason": "max_hours", "status": "stopped"}\n')
    check("commits counted from v1 output", parse_committed(out) == [4, 5])
    check("RUN_RESULT parsed", parse_run_result(out) == {"reason": "max_hours", "status": "stopped"})
    check("no RUN_RESULT -> None", parse_run_result("Closing #7") is None)
    check("limit words in agent text do not park",
          decide(parse_run_result(out), 0, False)[0] == "continue")
    check("usage_limit parks",
          decide({"reason": "usage_limit", "usage_reset": None}, USAGE_LIMIT_EXIT, False)[0] == "park")
    check("no_work is done", decide({"reason": "no_work"}, 1, False)[0] == "done")
    check("all deferred needs a human",
          decide({"reason": "all_deferred", "deferred": [3]}, 0, False)[0] == "needs_human")
    check("all_closed continues (next slice freezes new issues)",
          decide({"reason": "all_closed"}, 0, False)[0] == "continue")
    check("killed slice continues", decide(None, -1, True)[0] == "continue")
    check("crash without result is an error", decide(None, 1, False)[0] == "error")
    check("iso reset parsed", parse_iso("2026-10-01T03:00:00+08:00")
          == datetime(2026, 9, 30, 19, 0, tzinfo=timezone.utc))

    argv = slice_argv(["py", "run_issues.py"], "c.json", ["--max-issue", "20"], 50)
    check("slice resumes the same run", "--resume-or-new" in argv
          and argv[argv.index("--on-usage-limit") + 1] == "exit" and argv[-2:] == ["--max-issue", "20"])
    check("grace from config", default_grace_minutes({"run": {"max_session_minutes": 120}}) == 135)

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        a = Path(d) / "A" / "issue-automation.config.json"
        b = Path(d) / "B" / "issue-automation.config.json"
        check("per-project state files differ", default_state_path(str(a)) != default_state_path(str(b)))
        sp = Path(d) / "s.json"
        um2 = UsageManager(); um2.park("codex", now_utc() + timedelta(minutes=10))
        save_state(sp, um2, "codex", {"slices": 2, "committed": [3]})
        um3 = UsageManager(); stats = load_state(sp, um3)
        check("state round-trips", stats["committed"] == [3] and not um3.available("codex"))

    print("\n" + ("ALL SESSION SELF-TESTS PASSED" if ok else "SOME SESSION SELF-TESTS FAILED"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Unattended session for ONE project: runs run_issues.py and "
                    "pauses/resumes across provider usage limits. Unknown options are "
                    "passed straight to run_issues.py (e.g. --max-issue 20).")
    ap.add_argument("--config", help="the project's run_issues.py config file")
    ap.add_argument("--project", help="project folder beside run_issues.py (like run_issues.py --project)")
    ap.add_argument("--runner", default=None,
                    help="how to launch v1 (default: this Python + run_issues.py next to this script)")
    ap.add_argument("--provider", default="claude",
                    help="improver provider name, for usage bookkeeping (claude/codex/...)")
    ap.add_argument("--provider-probe", default=None,
                    help="optional command that prints usage JSON (quoted like a shell command)")
    ap.add_argument("--slice-minutes", type=int, default=50, help="usage-check cadence / slice length")
    ap.add_argument("--grace-minutes", type=int, default=None,
                    help="kill a slice this long past its budget (default: the config's "
                         "max_session_minutes + 15)")
    ap.add_argument("--usage-fallback-minutes", type=int, default=60,
                    help="cooldown when a limit gives no parseable reset time")
    ap.add_argument("--max-hours", type=float, default=0.0, help="total wall-clock budget (0=unlimited)")
    ap.add_argument("--once", action="store_true", help="run a single slice, then stop")
    ap.add_argument("--runner-cwd", default=None, help="working dir to launch the runner from")
    ap.add_argument("--state", default=None, help="state file (default: OS state dir, keyed by config path)")
    ap.add_argument("--fresh", action="store_true", help="clear prior session state and start over")
    ap.add_argument("--self-test", action="store_true")
    args, extra = ap.parse_known_args()

    if args.self_test:
        return self_test()
    if bool(args.config) == bool(args.project):
        ap.error("give exactly one of --config / --project (or use --self-test)")
    if args.project:
        try:
            args.config = str(project_config_path(platform_root(), args.project))
        except SupervisorError as exc:
            ap.error(str(exc))
    config = str(Path(args.config).expanduser().resolve())
    if not Path(config).is_file():
        ap.error(f"config not found: {config}")

    here = Path(__file__).resolve().parent
    split = (lambda s: shlex.split(s, posix=not IS_WINDOWS))
    runner = split(args.runner) if args.runner else [sys.executable, str(here / "run_issues.py")]
    probe = split(args.provider_probe) if args.provider_probe else None
    state_path = Path(args.state).expanduser() if args.state else default_state_path(config)
    grace = (args.grace_minutes if args.grace_minutes is not None
             else default_grace_minutes(load_config_file(Path(config))))

    return run(runner=runner, config=config, extra=extra, provider=args.provider,
               probe=probe, slice_minutes=args.slice_minutes, grace_minutes=grace,
               usage_fallback_minutes=args.usage_fallback_minutes, max_hours=args.max_hours,
               once=args.once, cwd=args.runner_cwd, state_path=state_path, fresh=args.fresh)


if __name__ == "__main__":
    raise SystemExit(main())

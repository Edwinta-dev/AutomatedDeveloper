#!/usr/bin/env python3
"""
usage.py — provider usage / rate-limit manager for agenticworkflow_v2.

The orchestrator runs coding agents (Claude, Codex, ...) that live behind
per-account usage windows. This module answers two questions:

  1. Is provider X available right now, or is it cooling down until a reset?
  2. Given some agent output text (or a probe command), when does X reset?

It is provider-agnostic. A "provider" is just a name string. How you check a
provider is pluggable:

  * PROACTIVE: a probe command you configure, printing JSON on stdout:
        {"ok": true|false, "reset": "<iso8601|unix|human>", "detail": "..."}
    If you have no real quota API, leave the probe empty and rely on...
  * REACTIVE: note_output() scans agent stdout for rate-limit signatures and
    parks the provider until the parsed reset time (or a fallback).

Nothing here talks to a network by itself; the probe command does (if any).
Run `python engine/usage.py --self-test` for offline checks.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Detecting a usage / rate limit in free-form agent output
# ---------------------------------------------------------------------------

_LIMIT_PATTERNS = [
    r"rate[\s_-]?limit",
    r"usage[\s_-]?limit",
    r"quota",
    r"resource[\s_-]?exhausted",
    r"too many requests",
    r"\b429\b",
    r"overloaded",
    r"insufficient[_\s]?quota",
    r"you have hit your",
    r"limit reached",
    r"try again (later|in|at)",
]
_LIMIT_RE = re.compile("|".join(_LIMIT_PATTERNS), re.IGNORECASE)


def detect_usage_limit(text: str) -> bool:
    """True if the text looks like a provider usage / rate-limit message."""
    return bool(text) and _LIMIT_RE.search(text) is not None


# ---------------------------------------------------------------------------
# Parsing a reset time out of free-form text
# ---------------------------------------------------------------------------

_REL_RE = re.compile(
    r"(?:in|after|wait)\s+(?:about\s+)?(\d+)\s*(second|sec|s|minute|min|m|hour|hr|h)s?\b",
    re.IGNORECASE,
)
_CLOCK_RE = re.compile(
    r"(?:try again at|resets? at|available at|retry at)\s+"
    r"(\d{1,2}):(\d{2})\s*(am|pm)?\s*(utc|gmt)?",
    re.IGNORECASE,
)
_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)\b")
_EPOCH_RE = re.compile(r"\b(1[6-9]\d{8}|20\d{8})\b")  # plausible unix seconds ~2023-2033


def parse_reset_datetime(text: str, *, ref: Optional[datetime] = None) -> Optional[datetime]:
    """Best-effort parse of when a limit resets. Returns tz-aware UTC or None."""
    if not text:
        return None
    ref = ref or now_utc()

    m = _ISO_RE.search(text)
    if m:
        raw = m.group(1).replace(" ", "T")
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    m = _EPOCH_RE.search(text)
    if m:
        try:
            return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
        except (ValueError, OSError):
            pass

    m = _REL_RE.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("s"):
            delta = timedelta(seconds=n)
        elif unit.startswith("h"):
            delta = timedelta(hours=n)
        else:
            delta = timedelta(minutes=n)
        return ref + delta

    m = _CLOCK_RE.search(text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and hh < 12:
            hh += 12
        elif ampm == "am" and hh == 12:
            hh = 0
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            cand = ref.astimezone(timezone.utc).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand <= ref:                      # already passed today -> tomorrow
                cand += timedelta(days=1)
            return cand
    return None


# ---------------------------------------------------------------------------
# Provider state + manager
# ---------------------------------------------------------------------------

@dataclass
class ProviderState:
    name: str
    cooling_until: Optional[datetime] = None
    last_detail: str = ""

    def available(self, ref: Optional[datetime] = None) -> bool:
        if self.cooling_until is None:
            return True
        return (ref or now_utc()) >= self.cooling_until


@dataclass
class UsageManager:
    fallback_minutes: int = 60
    states: dict[str, ProviderState] = field(default_factory=dict)

    def state(self, provider: str) -> ProviderState:
        return self.states.setdefault(provider, ProviderState(provider))

    def available(self, provider: str, ref: Optional[datetime] = None) -> bool:
        return self.state(provider).available(ref)

    def park(self, provider: str, until: Optional[datetime], detail: str = "") -> datetime:
        ref = now_utc()
        if until is None or until <= ref:
            until = ref + timedelta(minutes=self.fallback_minutes)
        st = self.state(provider)
        st.cooling_until = until
        st.last_detail = detail
        return until

    def clear(self, provider: str) -> None:
        st = self.state(provider)
        st.cooling_until = None
        st.last_detail = ""

    def note_output(self, provider: str, text: str) -> Optional[datetime]:
        """Inspect agent output; park the provider if it hit a limit.
        Returns the cooldown deadline if parked, else None."""
        if not detect_usage_limit(text):
            return None
        reset = parse_reset_datetime(text)
        return self.park(provider, reset, detail=_first_limit_line(text))

    def probe(self, provider: str, probe_cmd: Optional[list[str]],
              *, cwd: Optional[str] = None, timeout: float = 60.0) -> bool:
        """Run a configured probe command that prints JSON. Returns availability.
        No command -> unknown -> treated as available (rely on reactive)."""
        if not probe_cmd:
            return self.available(provider)
        try:
            cp = subprocess.run([str(a) for a in probe_cmd], cwd=cwd, shell=False,
                                text=True, encoding="utf-8", errors="replace",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return self.available(provider)          # probe broken -> don't block
        data = _safe_json(cp.stdout)
        if not isinstance(data, dict):
            # Fall back to scanning the probe's own text for a limit signature.
            if detect_usage_limit(cp.stdout):
                self.park(provider, parse_reset_datetime(cp.stdout))
                return False
            return self.available(provider)
        if data.get("ok") is False:
            self.park(provider, parse_reset_datetime(str(data.get("reset", ""))),
                      detail=str(data.get("detail", "")))
            return False
        self.clear(provider)
        return True

    def earliest_reset(self, providers: Optional[list[str]] = None) -> Optional[datetime]:
        names = providers or list(self.states)
        times = [self.states[n].cooling_until for n in names
                 if n in self.states and self.states[n].cooling_until]
        return min(times) if times else None

    def snapshot(self) -> dict:
        return {n: (s.cooling_until.isoformat() if s.cooling_until else None)
                for n, s in self.states.items()}

    def restore(self, snap: dict) -> None:
        for n, iso in (snap or {}).items():
            st = self.state(n)
            st.cooling_until = datetime.fromisoformat(iso) if iso else None


# ---------------------------------------------------------------------------
# Reading real usage from the agents' local logs (no network, no API)
# ---------------------------------------------------------------------------
#
# Codex writes its plan limits into every session log (~/.codex/sessions/.../
# rollout-*.jsonl, "token_count" events): primary = 5-hour window, secondary =
# weekly, each with used_percent and resets_at. That makes a real proactive probe.
#
# Claude Code's local transcripts (~/.claude/projects/*/*.jsonl) record tokens
# per message but NOT plan-limit percentages; those are only shown interactively
# (/usage) and to status-line scripts. So for Claude we report tokens, and the
# limit itself is handled reactively (run_issues.py exits on the limit message).

def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _find_key(obj, key: str):
    if isinstance(obj, dict):
        if key in obj:
            return obj
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def _tail_lines(path: Path, max_bytes: int = 1 << 20) -> list[str]:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        return fh.read().decode("utf-8", "replace").splitlines()


def codex_limits(home: Optional[Path] = None) -> Optional[dict]:
    """Latest Codex rate-limit snapshot: {"primary": {...}, "secondary": {...},
    "plan_type", "tokens", "file"} or None if Codex has no session logs yet."""
    sessions = (home or codex_home()) / "sessions"
    files = sorted(sessions.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime,
                   reverse=True)[:10] if sessions.exists() else []
    for f in files:
        for line in reversed(_tail_lines(f)):
            if '"rate_limits"' not in line:
                continue
            try:
                holder = _find_key(json.loads(line), "rate_limits")
            except json.JSONDecodeError:
                continue
            rl = (holder or {}).get("rate_limits") or {}
            if not rl.get("primary") and not rl.get("secondary"):
                continue
            info = (holder or {}).get("info") or {}
            return {"primary": rl.get("primary"), "secondary": rl.get("secondary"),
                    "plan_type": rl.get("plan_type"),
                    "tokens": (info.get("total_token_usage") or {}), "file": str(f)}
    return None


def _window_used(win: Optional[dict], ref: datetime) -> tuple[float, Optional[datetime]]:
    """(used_percent, resets_at); a window whose reset has passed counts as 0% used."""
    if not win:
        return 0.0, None
    reset = datetime.fromtimestamp(int(win["resets_at"]), tz=timezone.utc) if win.get("resets_at") else None
    if reset and reset <= ref:
        return 0.0, None
    return float(win.get("used_percent") or 0.0), reset


def codex_probe(max_percent: float = 97.0, home: Optional[Path] = None,
                ref: Optional[datetime] = None) -> dict:
    """{"ok", "reset", "detail"} in the shape UsageManager.probe() expects."""
    ref = ref or now_utc()
    lim = codex_limits(home)
    if lim is None:
        return {"ok": True, "detail": "no codex session logs yet"}
    p_used, p_reset = _window_used(lim["primary"], ref)
    s_used, s_reset = _window_used(lim["secondary"], ref)
    detail = f"codex 5h {p_used:.0f}% / week {s_used:.0f}% ({lim.get('plan_type') or '?'})"
    blocked = [(u, r) for u, r in ((p_used, p_reset), (s_used, s_reset)) if u >= max_percent]
    if not blocked:
        return {"ok": True, "detail": detail}
    reset = max(r for _, r in blocked if r) if any(r for _, r in blocked) else None
    return {"ok": False, "reset": reset.isoformat() if reset else "", "detail": detail}


def claude_tokens(hours: float, home: Optional[Path] = None,
                  ref: Optional[datetime] = None) -> dict:
    """Token totals from Claude Code transcripts over the last `hours`."""
    ref = ref or now_utc()
    since = ref - timedelta(hours=hours)
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "messages": 0}
    seen: set[str] = set()
    projects = (home or claude_home()) / "projects"
    if not projects.exists():
        return totals
    for f in projects.glob("**/*.jsonl"):
        if datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) < since:
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = row.get("message") or {}
                usage = msg.get("usage")
                ts = row.get("timestamp")
                if row.get("type") != "assistant" or not usage or not ts:
                    continue
                try:
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                key = msg.get("id") or row.get("uuid") or line[:200]
                if when < since or key in seen:
                    continue
                seen.add(key)
                totals["input"] += int(usage.get("input_tokens") or 0)
                totals["output"] += int(usage.get("output_tokens") or 0)
                totals["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
                totals["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
                totals["messages"] += 1
    return totals


def usage_report() -> str:
    ref = now_utc()
    out = []
    lim = codex_limits()
    if lim is None:
        out.append("Codex:  no session logs yet")
    else:
        parts = []
        for name, win in (("5h", lim["primary"]), ("week", lim["secondary"])):
            used, reset = _window_used(win, ref)
            parts.append(f"{name} {used:.0f}% used" + (f", resets {reset.astimezone():%a %H:%M}"
                                                       if reset else ""))
        out.append(f"Codex ({lim.get('plan_type') or '?'} plan):  " + " | ".join(parts))
        out.append("        (as of its last session; live view: `codex` then /status)")
    for label, hours in (("last 5h", 5), ("last 7d", 24 * 7)):
        t = claude_tokens(hours, ref=ref)
        out.append(f"Claude {label}:  {t['messages']} replies, {t['input'] + t['cache_write']:,} input "
                   f"+ {t['cache_read']:,} cache-read, {t['output']:,} output tokens")
    out.append("        (plan-limit %: `claude` then /usage; tokens only are logged locally)")
    return "\n".join(out)


def _first_limit_line(text: str) -> str:
    for line in text.splitlines():
        if _LIMIT_RE.search(line):
            return line.strip()[:200]
    return ""


def _safe_json(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # tolerate a JSON object embedded in other log noise
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def self_test() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}")
        ok &= bool(cond)

    check("detect plain rate limit", detect_usage_limit("Error: rate limit exceeded"))
    check("detect 429", detect_usage_limit("HTTP 429 Too Many Requests"))
    check("detect resource_exhausted", detect_usage_limit("gemini: RESOURCE_EXHAUSTED"))
    check("no false positive", not detect_usage_limit("all tests passed, committed #12"))

    ref = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    check("relative minutes", parse_reset_datetime("try again in 15 minutes", ref=ref)
          == ref + timedelta(minutes=15))
    check("relative hours", parse_reset_datetime("wait 2 hours", ref=ref)
          == ref + timedelta(hours=2))
    iso = parse_reset_datetime("resets at 2026-10-01T13:30:00Z", ref=ref)
    check("iso parse", iso == datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc))
    clk = parse_reset_datetime("try again at 12:30 UTC", ref=ref)
    check("clock today", clk == ref.replace(hour=12, minute=30))
    clk2 = parse_reset_datetime("try again at 11:00", ref=ref)
    check("clock rolls to tomorrow", clk2 == ref.replace(hour=11, minute=0) + timedelta(days=1))
    check("epoch parse",
          parse_reset_datetime("x 1790000000 y") == datetime.fromtimestamp(1790000000, tz=timezone.utc))

    um = UsageManager(fallback_minutes=30)
    check("available by default", um.available("claude"))
    deadline = um.note_output("claude", "usage limit reached, try again in 45 minutes")
    check("parked after limit", not um.available("claude"))
    check("parked ~45m", deadline is not None and abs((deadline - now_utc()).total_seconds() - 45 * 60) < 120)
    um2 = UsageManager(fallback_minutes=30)
    d2 = um2.note_output("codex", "usage limit reached")  # no time -> fallback
    check("fallback when no reset", d2 is not None and abs((d2 - now_utc()).total_seconds() - 30 * 60) < 120)
    check("other provider unaffected", um.available("gemini"))
    check("earliest_reset picks claude", um.earliest_reset(["claude", "gemini"]) == um.state("claude").cooling_until)

    # probe: JSON not-ok parks; ok clears
    um3 = UsageManager()
    py = sys.executable
    ok_cmd = [py, "-c", "print('{\"ok\": true}')"]
    bad_cmd = [py, "-c", "print('{\"ok\": false, \"reset\": \"in 10 minutes\"}')"]
    check("probe ok -> available", um3.probe("claude", ok_cmd) is True)
    check("probe not-ok -> parked", um3.probe("claude", bad_cmd) is False and not um3.available("claude"))
    check("missing probe tool -> not blocked", um3.probe("gemini", ["definitely-not-a-real-bin-xyz"]) is True)

    snap = um.snapshot()
    um4 = UsageManager()
    um4.restore(snap)
    check("snapshot/restore round-trips", um4.available("claude") == um.available("claude"))

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        sdir = home / "sessions" / "2026" / "10" / "01"
        sdir.mkdir(parents=True)
        later = int((ref + timedelta(hours=2)).timestamp())
        evt = {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 100}}, "rate_limits": {
            "primary": {"used_percent": 98.0, "window_minutes": 300, "resets_at": later},
            "secondary": {"used_percent": 8.0, "window_minutes": 10080, "resets_at": later + 9999},
            "plan_type": "plus"}}}
        (sdir / "rollout-x.jsonl").write_text('{"type":"other"}\n' + json.dumps(evt) + "\n",
                                              encoding="utf-8")
        check("codex limits read from session log", codex_limits(home)["primary"]["used_percent"] == 98.0)
        pr = codex_probe(97, home, ref)
        check("codex probe parks at the 5h window reset",
              pr["ok"] is False and parse_reset_datetime(pr["reset"]).timestamp() == later)
        check("codex probe ok under threshold", codex_probe(99, home, ref)["ok"] is True)
        check("expired window counts as unused",
              codex_probe(97, home, ref + timedelta(hours=3))["ok"] is True)
        check("no codex logs -> ok", codex_probe(97, home / "none")["ok"] is True)

        pdir = home / "projects" / "p"
        pdir.mkdir(parents=True)
        now_iso = now_utc().isoformat()
        row = {"type": "assistant", "timestamp": now_iso, "message": {"id": "m1", "usage": {
            "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}}}
        (pdir / "s.jsonl").write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
        t = claude_tokens(5, home)
        check("claude tokens summed, duplicates dropped",
              t["messages"] == 1 and t["output"] == 5 and t["cache_read"] == 100)

    print("\n" + ("ALL USAGE SELF-TESTS PASSED" if ok else "SOME USAGE SELF-TESTS FAILED"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Provider usage / rate-limit manager.")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--detect", metavar="TEXT", help="print whether TEXT is a usage-limit message + parsed reset")
    ap.add_argument("--report", action="store_true",
                    help="show Codex plan-limit usage and Claude Code token usage from local logs")
    ap.add_argument("--probe", choices=["codex"],
                    help="print probe JSON for session.py --provider-probe")
    ap.add_argument("--max-percent", type=float, default=97.0,
                    help="probe: park when a limit window is at least this full (default 97)")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if args.report:
        print(usage_report())
        return 0
    if args.probe == "codex":
        print(json.dumps(codex_probe(args.max_percent)))
        return 0
    if args.detect is not None:
        print(json.dumps({
            "is_limit": detect_usage_limit(args.detect),
            "reset": (lambda d: d.isoformat() if d else None)(parse_reset_datetime(args.detect)),
        }, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

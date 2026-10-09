#!/usr/bin/env python3
"""Compare issue-runner runs, grouped into arms, for the scope-gate A/B evaluation.

Turns what a run leaves on disk (supervisor_state.json, attempt logs, validation
output, integrity scope records) plus the work branch's git history into one
metrics table per arm. See docs/EVALUATION.md ("Metrics by goal", "Data collection").

    python engine/compare_runs.py --arm A=<run-id>[,<run-id>...] --arm B=<run-id> \
        [--issues 158-170] [--labels labels.csv] [--format md|csv|json] [--out FILE]
    python engine/compare_runs.py --list [--project-repo SUBSTRING]
    python engine/compare_runs.py --self-test

A run id is a directory name under the runner's runs directory
(%LOCALAPPDATA%\\issue-runner\\runs on Windows, $XDG_STATE_HOME/issue-runner/runs
elsewhere) or a full path to a run directory.

Units: arm totals count (run, issue) pairs, so an arm made of two repeat runs
of the same backlog counts each issue twice. The paired per-issue table
averages over the runs of each arm.

Tokens are counted per *committed* issue: all attempts' tokens (including failed
attempts) divided by the number of committed issues.

Standard library only. Does not import the integrity package; it reads its JSON
records as plain files.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

IS_WINDOWS = os.name == "nt"
APP_DIR_NAME = "issue-runner"
RUNS_DIR_NAME = "runs"

ATTEMPT_LOG_RE = re.compile(r"^attempt_(\d+)_issue_(\d+)_(\d{8}-\d{6})\.log$")
INTEGRITY_RE = re.compile(r"^scope_issue-(\d+)_attempt-(\d+)\.json$")
CLOSES_RE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b")
SUMMARY_CLOSES_RE = re.compile(r"(?im)^\s*Closes\s+#(\d+)\b")
SUMMARY_FINISHED_RE = re.compile(r"(?m)^\*\*Finished:\*\*\s*(\S+)")
SCOPE_FAIL_RE = re.compile(r"(?i)scope[ _-]?(?:check|gate)")
# Commits authored up to this long after the last file in the run dir still count.
RUN_END_SLACK = dt.timedelta(minutes=20)

NAV_COMMANDS = {
    "rg", "grep", "egrep", "fgrep", "findstr", "cat", "type", "get-content", "gc",
    "sed", "head", "tail", "ls", "dir", "get-childitem", "gci", "find",
    "select-string", "sls", "more", "less", "tree",
}
READ_COMMANDS = {"cat", "type", "get-content", "gc", "sed", "head", "tail", "more", "less"}
SHELL_NAMES = {"powershell", "pwsh", "bash", "sh", "zsh", "cmd"}
SHELL_FLAGS = {"-noprofile", "-noninteractive", "-nologo", "-executionpolicy", "bypass",
               "-command", "-c", "-lc", "/c", "/s", "-l", "-i", "-file"}
# Options of read commands that take a value (skip the value when collecting paths).
READ_VALUE_OPTS = {"-n", "-c", "-totalcount", "-head", "-tail", "-first", "-last",
                   "-encoding", "-delimiter", "-readcount", "-e", "-f"}
CLAUDE_READ_TOOLS = {"read", "notebookread"}
CLAUDE_NAV_TOOLS = {"read", "grep", "glob", "ls", "notebookread"}


def runs_root() -> Path:
    """Same location as engine/run_issues.py's runs_root()."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP_DIR_NAME / RUNS_DIR_NAME


# ===========================================================================
# Data model
# ===========================================================================

@dataclass
class Attempt:
    number: int
    issue: int
    started: Optional[dt.datetime]
    ended: Optional[dt.datetime]
    log: Path
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    has_usage: bool = False
    commands: int = 0
    nav_commands: int = 0
    files_read: set = field(default_factory=set)
    validation_text: str = ""

    @property
    def uncached(self) -> int:
        return max(0, self.input_tokens - self.cached_tokens)

    @property
    def seconds(self) -> Optional[float]:
        if self.started and self.ended:
            return max(0.0, (self.ended - self.started).total_seconds())
        return None


@dataclass
class IssueResult:
    run_id: str
    issue: int
    attempts: list = field(default_factory=list)
    committed: Optional[bool] = None   # None = unknown
    deferred: bool = False
    deferred_reason: str = ""
    scope_deferral: bool = False
    integrity: list = field(default_factory=list)  # records, sorted by attempt

    @property
    def final_record(self) -> Optional[dict]:
        return self.integrity[-1] if self.integrity else None


@dataclass
class Run:
    run_id: str
    path: Path
    state: dict
    issues: dict = field(default_factory=dict)  # issue -> IssueResult
    gaps: list = field(default_factory=list)
    commit_source: str = "git"


# ===========================================================================
# Helpers
# ===========================================================================

def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def load_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def parse_iso(s: object) -> Optional[dt.datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


def parse_ts(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(s, "%Y%m%d-%H%M%S").astimezone()
    except ValueError:
        return None


def mtime(path: Path) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return None


def parse_issue_range(spec: Optional[str]) -> Optional[set]:
    if not spec:
        return None
    out: set = set()
    for part in spec.split(","):
        part = part.strip().lstrip("#")
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a.strip().lstrip("#")), int(b.strip().lstrip("#"))
            out.update(range(min(lo, hi), max(lo, hi) + 1))
        else:
            out.add(int(part))
    return out


def resolve_run_dir(run_id: str) -> Path:
    p = Path(run_id)
    if p.is_dir() and (p / "supervisor_state.json").exists():
        return p
    q = runs_root() / run_id
    if q.is_dir():
        return q
    if p.is_dir():
        return p
    raise SystemExit(f"run not found: {run_id} (looked in {runs_root()})")


def git_out(repo: Path, *args: str) -> Optional[str]:
    try:
        cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout if cp.returncode == 0 else None


def norm_path(p: str, repo: Optional[str]) -> str:
    p = p.strip().strip("'\"`").replace("\\", "/")
    if repo:
        r = repo.replace("\\", "/").rstrip("/") + "/"
        if p.lower().startswith(r.lower()):
            p = p[len(r):]
    if p.startswith("./"):
        p = p[2:]
    return p.lower() if IS_WINDOWS else p


# ===========================================================================
# Shell command classification
# ===========================================================================

def _cmd_name(token: str) -> str:
    t = token.strip().strip("'\"`").lstrip("&(").strip("'\"`")
    t = t.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat"):
        if t.endswith(ext):
            t = t[: -len(ext)]
    return t


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=False)
    except ValueError:
        return segment.split()


def unwrap_shell(command: str) -> str:
    """Strip a leading shell wrapper (powershell -Command '...', bash -lc '...')."""
    cmd = command.strip()
    for _ in range(3):
        toks = _tokens(cmd)
        if not toks or _cmd_name(toks[0]) not in SHELL_NAMES:
            break
        # Find the position after the wrapper and its flags in the raw string.
        rest = cmd
        first = toks[0]
        rest = rest[rest.find(first) + len(first):] if first in rest else rest
        i = 1
        while i < len(toks) and toks[i].lower() in SHELL_FLAGS:
            pos = rest.find(toks[i])
            rest = rest[pos + len(toks[i]):] if pos >= 0 else rest
            i += 1
        rest = rest.strip()
        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "'\"":
            inner = rest[1:-1]
            inner = inner.replace("''", "'") if rest[0] == "'" else inner.replace('\\"', '"')
            rest = inner
        if rest == cmd:
            break
        cmd = rest
    return cmd


def split_pipelines(command: str) -> list[list[str]]:
    """Split into pipelines (on ; && || newlines), each a list of piped segments."""
    out = []
    for chunk in re.split(r"\r?\n|;|&&|\|\|", command):
        chunk = chunk.strip()
        if chunk:
            out.append([s.strip() for s in chunk.split("|") if s.strip()])
    return out


def classify_command(command: str, repo: Optional[str] = None) -> tuple[int, int, set]:
    """Return (pipelines, navigation pipelines, files read) for one shell command."""
    inner = unwrap_shell(command)
    pipes = split_pipelines(inner)
    nav = 0
    files: set = set()
    for pipe in pipes:
        toks = _tokens(pipe[0])
        if not toks:
            continue
        name = _cmd_name(toks[0])
        if name == "sed" and not any(t in ("-n", "--quiet") for t in toks[1:]):
            continue
        if name not in NAV_COMMANDS:
            continue
        nav += 1
        if name in READ_COMMANDS:
            skip = False
            for t in toks[1:]:
                if skip:
                    skip = False
                    continue
                tl = t.lower()
                if tl.startswith("-"):
                    if tl in READ_VALUE_OPTS:
                        skip = True
                    continue
                if name == "sed" and re.fullmatch(r"['\"]?[\d,$]+p?['\"]?", t):
                    continue
                if re.fullmatch(r"\d+", t) or t in (">", "2>", "<"):
                    continue
                if tl in ("-path", "-literalpath"):
                    continue
                p = norm_path(t, repo)
                if p and not p.startswith("$"):
                    files.add(p)
    return len(pipes), nav, files


# ===========================================================================
# Log parsing (codex JSONL and Claude Code stream-json)
# ===========================================================================

def parse_log(att: Attempt, repo: Optional[str]) -> None:
    claude_result_usage: Optional[dict] = None
    claude_msg_usage: dict = {}
    try:
        fh = att.log.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            t = ev.get("type")
            # --- codex ---
            if t == "turn.completed" and isinstance(ev.get("usage"), dict):
                u = ev["usage"]
                att.input_tokens += int(u.get("input_tokens") or 0)
                att.cached_tokens += int(u.get("cached_input_tokens") or 0)
                att.output_tokens += int(u.get("output_tokens") or 0)
                att.reasoning_tokens += int(u.get("reasoning_output_tokens") or 0)
                att.has_usage = True
            elif t == "item.completed" and isinstance(ev.get("item"), dict):
                item = ev["item"]
                if item.get("type") == "command_execution" and isinstance(item.get("command"), str):
                    n, nav, files = classify_command(item["command"], repo)
                    att.commands += max(1, n)
                    att.nav_commands += nav
                    att.files_read |= files
            # --- Claude Code stream-json ---
            elif t == "result" and isinstance(ev.get("usage"), dict):
                claude_result_usage = ev["usage"]
            elif t == "assistant" and isinstance(ev.get("message"), dict):
                msg = ev["message"]
                if isinstance(msg.get("usage"), dict):
                    claude_msg_usage[msg.get("id") or len(claude_msg_usage)] = msg["usage"]
                for c in msg.get("content") or []:
                    if not isinstance(c, dict) or c.get("type") != "tool_use":
                        continue
                    name = str(c.get("name") or "").lower()
                    inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                    if name == "bash" and isinstance(inp.get("command"), str):
                        n, nav, files = classify_command(inp["command"], repo)
                        att.commands += max(1, n)
                        att.nav_commands += nav
                        att.files_read |= files
                    elif name in ("powershell",) and isinstance(inp.get("command"), str):
                        n, nav, files = classify_command("powershell -Command " + json.dumps(inp["command"]), repo)
                        att.commands += max(1, n)
                        att.nav_commands += nav
                        att.files_read |= files
                    elif name in CLAUDE_NAV_TOOLS:
                        att.commands += 1
                        att.nav_commands += 1
                        if name in CLAUDE_READ_TOOLS:
                            fp = inp.get("file_path") or inp.get("notebook_path")
                            if isinstance(fp, str):
                                att.files_read.add(norm_path(fp, repo))
    usages = [claude_result_usage] if claude_result_usage else list(claude_msg_usage.values())
    for u in usages:
        cache_read = int(u.get("cache_read_input_tokens") or 0)
        cache_create = int(u.get("cache_creation_input_tokens") or 0)
        att.input_tokens += int(u.get("input_tokens") or 0) + cache_read + cache_create
        att.cached_tokens += cache_read
        att.output_tokens += int(u.get("output_tokens") or 0)
        att.has_usage = True


# ===========================================================================
# Loading a run
# ===========================================================================

def committed_from_git(repo: Path, branch: str, issues: set, start: Optional[dt.datetime],
                       end: Optional[dt.datetime]) -> Optional[set]:
    fmt = "--format=%H%x09%aI%x09%s%x09%b%x1e"
    out = None
    for ref in (branch, f"origin/{branch}"):
        if ref and git_out(repo, "rev-parse", "--verify", "--quiet", ref) is not None:
            out = git_out(repo, "log", ref, "-i", "--grep", "Closes #", fmt)
            if out is not None:
                break
    if out is None:
        return None
    found: set = set()
    for rec in out.split("\x1e"):
        parts = rec.strip().split("\t", 3)
        if len(parts) < 3:
            continue
        when = parse_iso(parts[1])
        if when and start and when < start:
            continue
        if when and end and when > end + RUN_END_SLACK:
            continue
        text = "\n".join(parts[2:])
        for n in CLOSES_RE.findall(text):
            if int(n) in issues:
                found.add(int(n))
    return found


def load_run(run_id: str, issue_filter: Optional[set] = None) -> Run:
    path = resolve_run_dir(run_id)
    state = load_json(path / "supervisor_state.json") or {}
    run = Run(run_id=path.name, path=path, state=state)
    if not state:
        run.gaps.append(f"{run.run_id}: supervisor_state.json missing or unreadable")
    repo_path = state.get("repo_path") or ""

    # Attempts from log file names.
    attempts: list[Attempt] = []
    for f in sorted(path.iterdir()):
        m = ATTEMPT_LOG_RE.match(f.name)
        if not m:
            continue
        num, issue = int(m.group(1)), int(m.group(2))
        if issue_filter is not None and issue not in issue_filter:
            continue
        a = Attempt(number=num, issue=issue, started=parse_ts(m.group(3)), ended=mtime(f), log=f)
        parse_log(a, repo_path)
        if not a.has_usage:
            run.gaps.append(f"{run.run_id}: attempt {num} (#{issue}) log has no token usage")
        stem = f.name[:-4]
        a.validation_text = read_text(path / f"{stem}_validation.txt")
        attempts.append(a)
    for a in attempts:
        run.issues.setdefault(a.issue, IssueResult(run.run_id, a.issue)).attempts.append(a)
    if not attempts:
        run.gaps.append(f"{run.run_id}: no attempt logs" + (" in --issues range" if issue_filter else ""))

    # Deferrals.
    deferred = state.get("deferred") or {}
    for k, reason in deferred.items():
        try:
            n = int(k)
        except (TypeError, ValueError):
            continue
        if n in run.issues:
            ir = run.issues[n]
            ir.deferred = True
            ir.deferred_reason = str(reason)

    # Integrity records.
    idir = path / "integrity"
    if idir.is_dir():
        for f in idir.iterdir():
            m = INTEGRITY_RE.match(f.name)
            if not m:
                continue
            n = int(m.group(1))
            rec = load_json(f)
            if rec is None:
                run.gaps.append(f"{run.run_id}: unreadable integrity record {f.name}")
                continue
            rec.setdefault("attempt", int(m.group(2)))
            if n in run.issues:
                run.issues[n].integrity.append(rec)
        for ir in run.issues.values():
            ir.integrity.sort(key=lambda r: int(r.get("attempt") or 0))

    # Committed issues.
    summary = read_text(path / "SUPERVISOR_SUMMARY.md")
    start = parse_iso(state.get("started_at"))
    end = parse_iso((SUMMARY_FINISHED_RE.search(summary) or [None, None])[1]) if summary else None
    mtimes = [mtime(f) for f in path.iterdir() if f.is_file()]
    mtimes = [m for m in mtimes if m]
    if mtimes and (end is None or max(mtimes) > end):
        end = max(mtimes)
    committed = None
    repo = Path(repo_path) if repo_path else None
    if repo and repo.is_dir():
        committed = committed_from_git(repo, state.get("branch") or "", set(run.issues), start, end)
        if committed is None:
            run.gaps.append(f"{run.run_id}: git log failed for branch {state.get('branch')!r} in {repo}")
    else:
        run.gaps.append(f"{run.run_id}: repo path {repo_path!r} not found")
    if committed is None:
        run.commit_source = "summary (unverified)"
        if summary:
            in_summary = {int(n) for n in SUMMARY_CLOSES_RE.findall(summary)}
            for n, ir in run.issues.items():
                ir.committed = True if n in in_summary else None
        run.gaps.append(f"{run.run_id}: committed status from SUPERVISOR_SUMMARY.md; "
                        "issues not listed there are 'unknown'")
    else:
        for n, ir in run.issues.items():
            ir.committed = n in committed

    for ir in run.issues.values():
        if ir.committed:
            ir.deferred = False
        if ir.deferred:
            text = ir.deferred_reason + "\n" + "\n".join(a.validation_text for a in ir.attempts[-1:])
            for line in text.splitlines():
                if SCOPE_FAIL_RE.search(line) and re.search(r"(?i)\b(fail|violation|blocked|out[ _-]of[ _-]scope)", line):
                    ir.scope_deferral = True
                    break
    return run


# ===========================================================================
# Metrics
# ===========================================================================

def _pct(num: float, den: float) -> str:
    if not den:
        return "n/a"
    return f"{100.0 * num / den:.1f}% ({_n(num)}/{_n(den)})"


def _n(x: float) -> str:
    return f"{int(x):,}" if float(x).is_integer() else f"{x:,.1f}"


def _per(num: float, den: float, unit: str = "") -> str:
    if not den:
        return "n/a"
    v = num / den
    s = f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}"
    return f"{s}{unit} ({_n(num)}/{_n(den)})"


def load_labels(path: Optional[str]) -> dict:
    """(run_id or '', issue) -> list of labels."""
    out: dict = {}
    if not path:
        return out
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            try:
                n = int(row.get("issue", "").lstrip("#"))
            except ValueError:
                continue
            lab = row.get("label", "").lower()
            if lab not in ("necessary", "gratuitous", "harmful"):
                continue
            out.setdefault((row.get("run_id", ""), n), []).append(lab)
    return out


def arm_metrics(name: str, runs: list[Run], labels: dict) -> dict:
    units = [ir for r in runs for ir in r.issues.values()]
    attempts = [a for ir in units for a in ir.attempts]
    committed = [ir for ir in units if ir.committed]
    unknown = [ir for ir in units if ir.committed is None]
    deferred = [ir for ir in units if ir.deferred]
    n_att, n_com = len(units), len(committed)
    m: dict = {"arm": name, "runs": [r.run_id for r in runs], "raw": {}}
    raw = m["raw"]
    raw.update(issues_attempted=n_att, issues_committed=n_com, committed_unknown=len(unknown),
               attempts=len(attempts), deferrals=len(deferred),
               scope_deferrals=sum(1 for ir in deferred if ir.scope_deferral))
    with_usage = [a for a in attempts if a.has_usage]
    tin = sum(a.input_tokens for a in attempts)
    tcache = sum(a.cached_tokens for a in attempts)
    tout = sum(a.output_tokens for a in attempts)
    tunc = sum(a.uncached for a in attempts)
    nav = sum(a.nav_commands for a in attempts)
    cmds = sum(a.commands for a in attempts)
    files = sum(len(a.files_read) for a in attempts)
    secs = [a.seconds for a in attempts if a.seconds is not None]
    raw.update(input_tokens=tin, cached_input_tokens=tcache, uncached_input_tokens=tunc,
               output_tokens=tout, attempts_with_usage=len(with_usage), commands=cmds,
               navigation_commands=nav, files_read=files, wall_seconds=sum(secs))

    rows: list = []
    add = lambda label, val: rows.append((label, val))
    add("Runs", ", ".join(r.run_id for r in runs))
    add("Issues attempted", _n(n_att))
    add("Issues committed", _n(n_com) + (f" (+{len(unknown)} unknown)" if unknown else ""))
    add("Completion rate", _pct(n_com, n_att - len(unknown)))
    add("Attempts (total)", _n(len(attempts)))
    add("Attempts per committed issue", _per(len(attempts), n_com))
    add("Deferral rate", _pct(len(deferred), n_att))
    add("Deferrals caused by scope check", _n(raw["scope_deferrals"]))
    add("Input tokens per committed issue", _per(tin, n_com) if with_usage else "n/a")
    add("Uncached input tokens per committed issue", _per(tunc, n_com) if with_usage else "n/a")
    add("Output tokens per committed issue", _per(tout, n_com) if with_usage else "n/a")
    add("Cached share of input", _pct(tcache, tin) if with_usage else "n/a")
    add("Shell commands per attempt", _per(cmds, len(attempts)))
    add("Navigation commands per attempt", _per(nav, len(attempts)))
    add("Distinct files read per attempt", _per(files, len(attempts)))
    add("Wall-clock minutes per committed issue", _per(sum(secs) / 60.0, n_com) if secs else "n/a")

    # Integrity records.
    com_rec = [ir for ir in committed if ir.final_record]
    all_recs = [rec for ir in units for rec in ir.integrity]
    if com_rec:
        def c(rec, k):
            return int((rec.get("counts") or {}).get(k) or 0)

        def ln(rec, k):
            return int((rec.get("lines_changed") or {}).get(k) or 0)
        drift = sum(1 for ir in com_rec if c(ir.final_record, "out_of_scope") > 0)
        oos_lines = sum(ln(ir.final_record, "out_of_scope") for ir in com_rec)
        in_lines = sum(ln(ir.final_record, "in_scope") for ir in com_rec)
        oos = sum(c(ir.final_record, "out_of_scope") for ir in com_rec)
        just = sum(c(ir.final_record, "justified") for ir in com_rec)
        raw.update(drift_issues=drift, oos_lines=oos_lines, in_scope_lines=in_lines,
                   oos_findings=oos, justified_findings=just, committed_with_record=len(com_rec))
        add("Drift rate (committed, final attempt OOS > 0)", _pct(drift, len(com_rec)))
        add("Out-of-scope lines per committed issue", _per(oos_lines, len(com_rec)))
        add("Out-of-scope line share", _pct(oos_lines, oos_lines + in_lines))
        add("% out-of-scope findings justified", _pct(just, oos))
    else:
        for k in ("Drift rate (committed, final attempt OOS > 0)", "Out-of-scope lines per committed issue",
                  "Out-of-scope line share", "% out-of-scope findings justified"):
            add(k, "n/a")
    if all_recs:
        verdicts: dict = {}
        for rec in all_recs:
            v = str(rec.get("verdict") or "?")
            verdicts[v] = verdicts.get(v, 0) + 1
        raw["verdicts"] = verdicts
        add("Scope-gate verdicts (all attempts)",
            ", ".join(f"{k} {verdicts[k]}" for k in ("pass", "violation", "skipped", "unverified") if k in verdicts)
            + "".join(f", {k} {v}" for k, v in verdicts.items() if k not in ("pass", "violation", "skipped", "unverified")))
    else:
        add("Scope-gate verdicts (all attempts)", "n/a")

    # Manual labels.
    if labels:
        grat = fp = labelled = 0
        for ir in committed:
            labs = labels.get((ir.run_id, ir.issue), []) + labels.get(("", ir.issue), [])
            if labs:
                labelled += 1
            if any(l in ("gratuitous", "harmful") for l in labs):
                grat += 1
            elif labs and all(l == "necessary" for l in labs):
                fp += 1
        raw.update(gratuitous_issues=grat, false_positive_issues=fp, labelled_issues=labelled)
        add("Gratuitous drift rate (labels)", _pct(grat, n_com))
        add("Scope false-positive rate (labels)", _pct(fp, n_com))
    else:
        add("Gratuitous drift rate (labels)", "n/a")
        add("Scope false-positive rate (labels)", "n/a")
    m["rows"] = rows
    return m


def paired_rows(arms: dict) -> tuple[list, list]:
    """Per-issue rows over issues that every arm attempted."""
    names = list(arms)
    per_arm: dict = {}
    for name, runs in arms.items():
        d: dict = {}
        for r in runs:
            for n, ir in r.issues.items():
                d.setdefault(n, []).append(ir)
        per_arm[name] = d
    shared = set.intersection(*(set(d) for d in per_arm.values())) if per_arm else set()
    header = ["Issue"]
    for name in names:
        header += [f"{name} committed", f"{name} attempts", f"{name} uncached in", f"{name} OOS"]
    rows = []
    for n in sorted(shared):
        row = [f"#{n}"]
        for name in names:
            irs = per_arm[name][n]
            k = len(irs)
            com = sum(1 for ir in irs if ir.committed)
            unk = sum(1 for ir in irs if ir.committed is None)
            ctext = ("yes" if com else "no") if k == 1 and not unk else f"{com}/{k}"
            if unk:
                ctext += f" ({unk} unknown)" if k > 1 else "unknown"
                ctext = ctext.replace("nounknown", "unknown").replace("yesunknown", "unknown")
            atts = sum(len(ir.attempts) for ir in irs) / k
            unc = sum(a.uncached for ir in irs for a in ir.attempts) / k
            recs = [ir.final_record for ir in irs if ir.final_record]
            oos = (sum(int((r.get("counts") or {}).get("out_of_scope") or 0) for r in recs) / len(recs)
                   if recs else None)
            row += [ctext, f"{atts:g}", f"{unc:,.0f}", "n/a" if oos is None else f"{oos:g}"]
        rows.append(row)
    return header, rows


# ===========================================================================
# Rendering
# ===========================================================================

def md_table(header: list, rows: list) -> str:
    esc = lambda s: str(s).replace("|", "\\|")
    out = ["| " + " | ".join(esc(h) for h in header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def render(arms: dict, metrics: list, fmt: str, issue_filter: Optional[set]) -> str:
    names = [m["arm"] for m in metrics]
    labels = [lab for lab, _ in metrics[0]["rows"]]
    table = [[lab] + [dict(m["rows"])[lab] for m in metrics] for lab in labels]
    p_header, p_rows = paired_rows(arms) if len(arms) >= 2 else ([], [])
    gaps = [g for runs in arms.values() for r in runs for g in r.gaps]
    if not any(r.issues and any(ir.integrity for ir in r.issues.values())
               for runs in arms.values() for r in runs):
        gaps.append("no integrity/scope_issue-*.json records in any run: isolation metrics are n/a")

    if fmt == "json":
        return json.dumps({
            "issues": sorted(issue_filter) if issue_filter else None,
            "arms": [{"arm": m["arm"], "runs": m["runs"], "metrics": dict(m["rows"]), "raw": m["raw"]}
                     for m in metrics],
            "paired": [dict(zip(p_header, r)) for r in p_rows],
            "gaps": gaps,
        }, indent=2)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["metric"] + names)
        w.writerows(table)
        if p_rows:
            w.writerow([])
            w.writerow(p_header)
            w.writerows(p_rows)
        return buf.getvalue()
    out = ["# Run comparison", ""]
    if issue_filter:
        out += [f"Issues restricted to: {min(issue_filter)}-{max(issue_filter)}", ""]
    out += [md_table(["Metric"] + names, table), ""]
    if p_rows:
        out += ["## Per-issue (issues attempted by every arm)", "", md_table(p_header, p_rows), ""]
    elif len(arms) >= 2:
        out += ["_No issue was attempted by every arm; no paired table._", ""]
    if gaps:
        out += ["## Data gaps", ""] + [f"- {g}" for g in gaps] + [""]
    out += ["_Tokens, attempts and wall-clock are summed over all attempts (including failed ones) "
            "and divided by committed issues. Navigation = pipelines starting with rg/grep/findstr/cat/"
            "type/Get-Content/sed -n/head/tail/ls/dir/Get-ChildItem/find/Select-String after stripping "
            "shell wrappers; files read are best-effort from read-command arguments._"]
    return "\n".join(out) + "\n"


# ===========================================================================
# --list
# ===========================================================================

def list_runs(project: Optional[str]) -> str:
    root = runs_root()
    rows = []
    if root.is_dir():
        for d in sorted(root.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            st = load_json(d / "supervisor_state.json") or {}
            repo = st.get("repo_name") or st.get("repo_path") or "?"
            if project and project.lower() not in (str(st.get("repo_name")) + " " + str(st.get("repo_path"))).lower():
                continue
            n_att = len({m.group(2) for f in d.iterdir() if (m := ATTEMPT_LOG_RE.match(f.name))})
            rows.append([d.name, repo, st.get("branch") or "?", st.get("started_at") or "?",
                         f"{len(st.get('frozen_numbers') or [])} ({n_att} attempted)",
                         st.get("status") or "?"])
    return f"Runs in {root}\n\n" + md_table(["Run id", "Repo", "Branch", "Started", "#Issues", "Status"], rows) + "\n"


# ===========================================================================
# Self-test
# ===========================================================================

def self_test() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool):
        print(f"{'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    # Command classification.
    ps = ('"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '
          "'Get-Content AGENTS.md; Get-Content src/a.py -TotalCount 40; git status'")
    n, nav, files = classify_command(ps)
    check("powershell wrapper: 3 pipelines", n == 3)
    check("powershell wrapper: 2 navigation", nav == 2)
    check("powershell wrapper: files read", files == {norm_path("AGENTS.md", None), norm_path("src/a.py", None)})
    n, nav, files = classify_command("bash -lc \"sed -n '1,80p' lib/x.py && rg -n foo src | head -5 && pytest -q\"")
    check("bash wrapper: sed -n + rg nav, pytest not", (n, nav) == (3, 2))
    check("bash wrapper: sed file", files == {norm_path("lib/x.py", None)})
    check("sed without -n is not navigation", classify_command("sed -i s/a/b/ f.txt")[1] == 0)
    check("issue range parse", parse_issue_range("158-160,170") == {158, 159, 160, 170})

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        repo = tmp / "repo"
        repo.mkdir()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@e")

        def g(*a, date=None):
            e = dict(env)
            if date:
                e["GIT_AUTHOR_DATE"] = e["GIT_COMMITTER_DATE"] = date
            subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, env=e)
        g("init", "-q", "-b", "work")
        (repo / "f.txt").write_text("0\n")
        g("add", ".")
        g("commit", "-q", "-m", "Issue #5 old\n\nCloses #5.", date="2026-01-01T09:00:00+00:00")
        (repo / "f.txt").write_text("1\n")
        g("commit", "-qam", "Issue #10: thing\n\nCloses #10.", date="2026-01-01T10:20:00+00:00")
        (repo / "f.txt").write_text("2\n")
        g("commit", "-qam", "Issue #12: other\n\nCloses #12.", date="2026-01-01T10:40:00+00:00")

        run = tmp / "runs" / "20260101-100000-000001"
        run.mkdir(parents=True)
        state = {"repo_path": str(repo), "repo_name": "o/r", "branch": "work",
                 "frozen_numbers": [5, 10, 11, 12], "attempt": 4,
                 "retry_counts": {"11": 2}, "deferred": {"11": "validation failed"},
                 "status": "finished", "started_at": "2026-01-01T10:00:00+00:00"}
        (run / "supervisor_state.json").write_text(json.dumps(state))

        def ts(h, mi):
            return dt.datetime(2026, 1, 1, h, mi, tzinfo=dt.timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")

        def cmd_item(c):
            return json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": c}})

        def usage(i, c, o):
            return json.dumps({"type": "turn.completed", "usage": {"input_tokens": i, "cached_input_tokens": c,
                                                                     "output_tokens": o, "reasoning_output_tokens": 1}})
        logs = [
            (1, 10, (10, 5), (10, 15), [cmd_item("bash -lc 'cat a.py; rg foo'"), usage(1000, 600, 50)]),
            (2, 11, (10, 15), (10, 25), [cmd_item("bash -lc 'ls'"), usage(2000, 1000, 100)]),
            (3, 11, (10, 25), (10, 35), [usage(3000, 1000, 100), cmd_item("bash -lc 'pytest'")]),
            (4, 12, (10, 35), (10, 40), [cmd_item("bash -lc 'head -20 b.py; cat a.py'"),
                                         usage(500, 100, 25), usage(500, 100, 25)]),
        ]
        for num, issue, s, e, lines in logs:
            stem = f"attempt_{num:03d}_issue_{issue}_{ts(*s)}"
            p = run / f"{stem}.log"
            p.write_text("\n".join(["{\"type\":\"thread.started\"}", *lines, "not json"]) + "\n")
            end = dt.datetime(2026, 1, 1, *e, tzinfo=dt.timezone.utc).timestamp()
            os.utime(p, (end, end))
            if num == 3:
                (run / f"{stem}_validation.txt").write_text("PASS: tests\nFAIL: scope check (exit 1)\n")
        idir = run / "integrity"
        idir.mkdir()
        (idir / "scope_issue-10_attempt-1.json").write_text(json.dumps({
            "schema_version": 1, "issue": 10, "attempt": 1, "mode": "report", "verdict": "violation",
            "counts": {"in_scope": 3, "allowed": 1, "out_of_scope": 2, "unverified": 0, "justified": 1,
                       "whitespace_only": 0}, "lines_changed": {"in_scope": 30, "out_of_scope": 10}}))
        (idir / "scope_issue-12_attempt-4.json").write_text(json.dumps({
            "schema_version": 1, "issue": 12, "attempt": 4, "mode": "report", "verdict": "pass",
            "counts": {"in_scope": 2, "out_of_scope": 0, "justified": 0}, "lines_changed": {"in_scope": 20,
                                                                                       "out_of_scope": 0}}))
        for f in run.iterdir():
            if f.is_file() and not f.name.endswith(".log"):
                t = dt.datetime(2026, 1, 1, 10, 41, tzinfo=dt.timezone.utc).timestamp()
                os.utime(f, (t, t))

        labels_csv = tmp / "labels.csv"
        labels_csv.write_text("run_id,issue,finding,label\n20260101-100000-000001,10,x.py::f,gratuitous\n"
                              "20260101-100000-000001,10,y.py,necessary\n")
        r = load_run(str(run))
        check("issues attempted = 3 (#5 has no attempt)", sorted(r.issues) == [10, 11, 12])
        check("committed from git within run window", {n for n, ir in r.issues.items() if ir.committed} == {10, 12})
        check("deferred #11", r.issues[11].deferred and not r.issues[10].deferred)
        check("scope deferral detected", r.issues[11].scope_deferral)
        a4 = r.issues[12].attempts[0]
        check("multi-turn usage summed", (a4.input_tokens, a4.cached_tokens, a4.output_tokens) == (1000, 200, 50))
        m = arm_metrics("A", [r], load_labels(str(labels_csv)))
        raw, rows = m["raw"], dict(m["rows"])
        check("token totals", (raw["input_tokens"], raw["uncached_input_tokens"], raw["output_tokens"])
              == (7000, 4200, 300))
        check("completion rate row", rows["Completion rate"] == "66.7% (2/3)")
        check("attempts per committed", rows["Attempts per committed issue"] == "2.00 (4/2)")
        check("uncached per committed", rows["Uncached input tokens per committed issue"] == "2,100 (4,200/2)")
        check("navigation: 5 nav of 6 pipelines", (raw["navigation_commands"], raw["commands"]) == (5, 6))
        check("files read per attempt", raw["files_read"] == 3)
        check("wall-clock 35 min", abs(raw["wall_seconds"] - 2100) < 1)
        check("deferral row", rows["Deferral rate"] == "33.3% (1/3)" and raw["scope_deferrals"] == 1)
        check("drift rate", rows["Drift rate (committed, final attempt OOS > 0)"] == "50.0% (1/2)")
        check("oos lines per committed", rows["Out-of-scope lines per committed issue"] == "5.00 (10/2)")
        check("justified", rows["% out-of-scope findings justified"] == "50.0% (1/2)")
        check("verdicts", raw["verdicts"] == {"violation": 1, "pass": 1})
        check("gratuitous drift via labels", rows["Gratuitous drift rate (labels)"] == "50.0% (1/2)")
        check("false-positive rate via labels", rows["Scope false-positive rate (labels)"] == "0.0% (0/2)")
        r2 = load_run(str(run), parse_issue_range("10-11"))
        check("--issues filter", sorted(r2.issues) == [10, 11])
        text = render({"A": [r], "B": [r2]}, [m, arm_metrics("B", [r2], {})], "md", None)
        check("markdown has paired table", "## Per-issue" in text and "| #10 | yes | 1 |" in text)
        check("json renders", json.loads(render({"A": [r]}, [m], "json", None))["arms"][0]["raw"]["attempts"] == 4)
        # Missing repo -> summary fallback, committed unknown.
        state["repo_path"] = str(tmp / "nope")
        (run / "supervisor_state.json").write_text(json.dumps(state))
        (run / "SUPERVISOR_SUMMARY.md").write_text("## Committed\nCloses #10\n")
        r3 = load_run(str(run))
        check("missing repo: summary fallback", r3.issues[10].committed is True and r3.issues[12].committed is None)
        # Claude stream-json.
        cl = run / f"attempt_005_issue_13_{ts(10, 41)}.log"
        cl.write_text("\n".join(json.dumps(e) for e in [
            {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 5}, "content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "src/z.py"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "grep -r x . ; make"}}]}},
            {"type": "result", "usage": {"input_tokens": 10, "cache_read_input_tokens": 90,
                                         "cache_creation_input_tokens": 20, "output_tokens": 7}}]) + "\n")
        a = load_run(str(run)).issues[13].attempts[0]
        check("claude usage from result event", (a.input_tokens, a.cached_tokens, a.output_tokens) == (120, 90, 7))
        check("claude tool navigation", (a.commands, a.nav_commands, a.files_read) == (3, 2, {norm_path("src/z.py", None)}))

    ok = not failures
    print("\n" + ("ALL COMPARE SELF-TESTS PASSED" if ok else "SOME COMPARE SELF-TESTS FAILED: " + ", ".join(failures)))
    return 0 if ok else 1


# ===========================================================================
# CLI
# ===========================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Metrics are defined in docs/EVALUATION.md.")
    p.add_argument("--arm", action="append", default=[], metavar="NAME=RUN[,RUN...]",
                   help="an arm: a name and one or more run ids or run-dir paths (repeatable)")
    p.add_argument("--issues", help="restrict to issues, e.g. 158-170 or 158,160-165")
    p.add_argument("--labels", help="CSV with run_id,issue,finding,label (necessary|gratuitous|harmful)")
    p.add_argument("--format", choices=("md", "csv", "json"), default="md")
    p.add_argument("--out", help="write output to this file instead of stdout")
    p.add_argument("--list", action="store_true", help="list run directories")
    p.add_argument("--project-repo", help="with --list: only runs whose repo contains this substring")
    p.add_argument("--self-test", action="store_true")
    return p


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    if args.self_test:
        return self_test()
    if args.list:
        text = list_runs(args.project_repo)
    else:
        if not args.arm:
            build_arg_parser().error("give at least one --arm NAME=RUN[,RUN...] (or --list / --self-test)")
        issue_filter = parse_issue_range(args.issues)
        arms: dict = {}
        for spec in args.arm:
            if "=" not in spec:
                raise SystemExit(f"--arm must be NAME=RUN[,RUN...]: {spec}")
            name, ids = spec.split("=", 1)
            arms[name.strip()] = [load_run(i.strip(), issue_filter) for i in ids.split(",") if i.strip()]
        labels = load_labels(args.labels)
        metrics = [arm_metrics(name, runs, labels) for name, runs in arms.items()]
        text = render(arms, metrics, args.format, issue_filter)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

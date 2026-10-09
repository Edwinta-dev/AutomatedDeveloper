#!/usr/bin/env python3
"""
digest.py: one-page "what happened while you were away" summary of a runner run.

Writes <run dir>/DIGEST.md for a non-programmer steering the project: outcome, what
needs their judgement (ranked), what was built, decisions by area, how to respond.

    python engine/digest.py --run <run-id | run-dir>
    python engine/digest.py --latest [--project-repo SUBSTR]
    python engine/digest.py --self-test

Inputs: supervisor_state.json + attempt files (parsed by compare_runs.load_run),
<run>/integrity/{scope,refs,record}_issue-N_attempt-K.json, SUPERVISOR_SUMMARY.md, git
commits on the run's branch ("Closes #N"), and the decision records
(docs/decisions/NNNN-*.md) at the branch tip. Missing inputs degrade to "data gaps";
older runs that predate decision/integrity records still get a digest.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare_runs as cr  # noqa: E402

DIGEST_NAME = "DIGEST.md"
DECISIONS_DIR = "docs/decisions"
INTEGRITY_ANY_RE = re.compile(r"^(scope|refs|record)_issue-(\d+)_attempt-(\d+)\.json$")
FACTS_BEGIN = "<!-- verified-facts:begin"
FACTS_END = "<!-- verified-facts:end -->"
VETO_RE = re.compile(r"(?im)^\s*VERDICT:\s*VETO\b")
STATUS_RE = re.compile(r"(?im)^\s*Status:\s*\**\s*(implemented|partial|blocked)\b")


# ===========================================================================
# Data gathering
# ===========================================================================

@dataclass
class Decision:
    path: str
    status: str = ""
    sections: dict = field(default_factory=dict)   # lower-case heading -> body
    tags: list = field(default_factory=list)
    warnings: list = field(default_factory=list)   # WARN lines from the facts block

    def section(self, prefix: str) -> str:
        for k, v in self.sections.items():
            if k.startswith(prefix):
                return v
        return ""


@dataclass
class IssueView:
    number: int
    title: str = ""
    committed: Optional[bool] = None
    sha: str = ""
    subject: str = ""
    files: list = field(default_factory=list)
    deferred_reason: str = ""
    blocker: Optional[dict] = None
    attempts: int = 0
    vetoes: int = 0
    decision: Optional[Decision] = None
    integ: dict = field(default_factory=dict)      # kind -> latest record dict
    integ_paths: dict = field(default_factory=dict)  # kind -> file path


def _first_line(text: str, limit: int = 160) -> str:
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-*").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "..."
    return ""


def _short(text: str, limit: int = 220) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "..."


def _dict_field(state: dict, key: str) -> dict:
    v = state.get(key)
    if isinstance(v, str):          # tolerate stringified dicts in odd states
        try:
            import ast
            v = ast.literal_eval(v)
        except (ValueError, SyntaxError):
            v = {}
    return v if isinstance(v, dict) else {}


def parse_decision(path: str, text: str) -> Decision:
    d = Decision(path=path)
    facts = ""
    if FACTS_BEGIN in text:
        head, _, rest = text.partition(FACTS_BEGIN)
        facts, _, tail = rest.partition(FACTS_END)
        text = head + tail
    m = STATUS_RE.search(text.split("\n## ", 1)[0])
    d.status = m.group(1).lower() if m else ""
    for chunk in re.split(r"(?m)^##\s+", text)[1:]:
        heading, _, body = chunk.partition("\n")
        d.sections[heading.strip().lower()] = body.strip()
    tags: list = []
    for line in facts.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.lstrip().startswith("|") and len(cells) >= 5 and cells[0].startswith("`"):
            for t in cells[3].split(","):
                t = t.strip().lower()
                if t and t not in tags:
                    tags.append(t)
        elif line.strip().startswith("- WARN:"):
            d.warnings.append(line.strip()[len("- WARN:"):].strip())
    d.tags = tags
    return d


def commits_by_issue(repo: Path, branch: str, issues: set, start, end) -> dict:
    """issue -> (sha, subject, [files]); newest commit wins. Same window as compare_runs."""
    out = None
    for ref in (branch, f"origin/{branch}"):
        if ref and cr.git_out(repo, "rev-parse", "--verify", "--quiet", ref) is not None:
            out = cr.git_out(repo, "log", ref, "-i", "--grep", "Closes #",
                             "--format=%H%x09%aI%x09%s%x09%b%x1e")
            if out is not None:
                break
    found: dict = {}
    for rec in (out or "").split("\x1e"):
        parts = rec.strip().split("\t", 3)
        if len(parts) < 3:
            continue
        when = cr.parse_iso(parts[1])
        if when and start and when < start:
            continue
        if when and end and when > end + cr.RUN_END_SLACK:
            continue
        for n in cr.CLOSES_RE.findall("\n".join(parts[2:])):
            n = int(n)
            if n in issues and n not in found:
                files = (cr.git_out(repo, "diff-tree", "--no-commit-id", "--name-only", "-r",
                                    parts[0]) or "").split()
                found[n] = (parts[0], parts[2], files)
    return found


def decisions_at_tip(repo: Path, branch: str, rec_dir: str = DECISIONS_DIR) -> dict:
    """issue -> (path, text) for docs/decisions/NNNN-*.md at the branch tip."""
    out: dict = {}
    for ref in (branch, f"origin/{branch}"):
        listing = cr.git_out(repo, "ls-tree", "-r", "--name-only", ref, "--", rec_dir)
        if listing is None:
            continue
        for p in listing.splitlines():
            m = re.match(r"^(\d+)-.*\.md$", p.rsplit("/", 1)[-1])
            if m and int(m.group(1)) not in out:
                text = cr.git_out(repo, "show", f"{ref}:{p}")
                if text is not None:
                    out[int(m.group(1))] = (p, text)
        break
    return out


def summary_dependency_blocks(summary: str) -> dict:
    out = {}
    sec = summary.split("## Blocked by open dependencies", 1)
    if len(sec) == 2:
        for line in sec[1].split("\n## ", 1)[0].splitlines():
            m = re.match(r"^- #(\d+) waits on (.+)$", line.strip())
            if m:
                out[int(m.group(1))] = m.group(2)
    return out


@dataclass
class Digest:
    run: object
    state: dict
    issues: dict
    env_blockers: dict
    dep_blocked: dict
    window: tuple
    gaps: list


def gather(run_ref: str) -> Digest:
    run = cr.load_run(run_ref)
    state = run.state
    gaps = list(run.gaps)
    titles = {int(k): str(v) for k, v in _dict_field(state, "frozen_titles").items()}
    deferred = {int(k): str(v) for k, v in _dict_field(state, "deferred").items()}
    blockers = {int(k): v for k, v in _dict_field(state, "deferred_blockers").items()}
    frozen = [int(n) for n in state.get("frozen_numbers") or []]
    numbers = sorted(set(frozen) | set(run.issues) | set(deferred))

    views: dict = {}
    for n in numbers:
        ir = run.issues.get(n)
        v = IssueView(number=n, title=titles.get(n, ""))
        if ir is not None:
            v.committed = ir.committed
            v.attempts = len(ir.attempts)
            v.vetoes = sum(1 for a in ir.attempts if VETO_RE.search(a.validation_text or ""))
        if n in deferred and not v.committed:
            v.deferred_reason = deferred[n]
            v.blocker = blockers.get(n)
        views[n] = v

    # integrity records (latest attempt per kind)
    idir = run.path / "integrity"
    if idir.is_dir():
        best: dict = {}
        for f in idir.iterdir():
            m = INTEGRITY_ANY_RE.match(f.name)
            if not m:
                continue
            kind, n, k = m.group(1), int(m.group(2)), int(m.group(3))
            if n in views and k >= best.get((n, kind), (-1, None))[0]:
                best[(n, kind)] = (k, f)
        for (n, kind), (_, f) in best.items():
            rec = cr.load_json(f)
            if rec is None:
                gaps.append(f"unreadable integrity record {f.name}")
                continue
            views[n].integ[kind] = rec
            views[n].integ_paths[kind] = str(f)

    # window
    start = cr.parse_iso(state.get("started_at"))
    summary = cr.read_text(run.path / "SUPERVISOR_SUMMARY.md")
    m = cr.SUMMARY_FINISHED_RE.search(summary)
    end = cr.parse_iso(m.group(1)) if m else None
    mt = [cr.mtime(f) for f in run.path.iterdir() if f.is_file() and f.name != DIGEST_NAME]
    mt = [x for x in mt if x]
    if mt and (end is None or max(mt) > end):
        end = max(mt)

    repo = Path(state.get("repo_path") or "")
    branch = state.get("branch") or ""
    if state.get("repo_path") and repo.is_dir():
        for n, (sha, subj, files) in commits_by_issue(repo, branch, set(views), start, end).items():
            views[n].sha, views[n].subject, views[n].files = sha, subj, files
            views[n].committed = True
            views[n].deferred_reason = ""
        recs = decisions_at_tip(repo, branch)
        for n, v in views.items():
            path = (v.integ.get("record") or {}).get("record_path")
            if n in recs:
                v.decision = parse_decision(recs[n][0], recs[n][1])
            elif path:
                text = cr.git_out(repo, "show", f"{branch}:{str(path).replace(chr(92), '/')}")
                if text:
                    v.decision = parse_decision(str(path), text)
    return Digest(run=run, state=state, issues=views,
                  env_blockers=_dict_field(state, "env_blockers"),
                  dep_blocked=summary_dependency_blocks(summary),
                  window=(start, end), gaps=gaps)


# ===========================================================================
# Rendering
# ===========================================================================

def _fmt_time(t) -> str:
    return t.strftime("%Y-%m-%d %H:%M") if t else "?"


def _rec_link(v: IssueView) -> str:
    if v.decision:
        return f"`{v.decision.path}`"
    if "record" in v.integ_paths:
        return f"`{v.integ_paths['record']}`"
    return ""


def attention_items(d: Digest) -> list:
    """(rank, text) items; lower rank = more urgent."""
    items = []
    for key, b in sorted(d.env_blockers.items()):
        nums = ", ".join(f"#{n}" for n in b.get("issues") or [])
        items.append((0, f"**Set up the environment** ({b.get('kind', 'tool')}): "
                         f"{_short(b.get('subject') or key, 140)}. Blocks {nums or 'issues'}. "
                         f"To do: {_short(b.get('hint') or 'install/configure it, then resume the run', 180)}"))
    for n, v in sorted(d.issues.items()):
        if v.deferred_reason:
            if v.blocker:
                todo = "fix the environment item above, then resume the run"
            elif "no validated result" in v.deferred_reason.lower():
                todo = "the agent could not finish it within its retries; consider splitting the issue or clarifying it"
            else:
                todo = "read the reason, clarify or split the issue, then resume"
            items.append((1, f"**#{n} {v.title} was set aside.** Why: "
                             f"{_short(v.deferred_reason, 200)} To do: {todo}."))
    for n, deps in sorted(d.dep_blocked.items()):
        v = d.issues.get(n)
        items.append((2, f"**#{n} {v.title if v else ''}** is waiting on {deps}; it starts once those are done."))
    for n, v in sorted(d.issues.items()):
        if not v.committed:
            continue
        link = _rec_link(v)
        reasons = []
        status = (v.decision.status if v.decision else "") or (v.integ.get("record") or {}).get("status", "")
        if status in ("partial", "blocked"):
            reasons.append(f"the record says the work is **{status}**")
        warns = list(v.decision.warnings) if v.decision else []
        if not warns:
            warns = [f.get("message", "") for f in (v.integ.get("record") or {}).get("findings") or []
                     if f.get("severity") == "WARN"]
        if warns:
            reasons.append(f"{len(warns)} consistency warning(s), e.g. \"{_short(warns[0], 110)}\"")
        oos = (v.integ.get("record") or {}).get("out_of_scope") or {}
        unexpl = [i for i in oos.get("items") or [] if isinstance(i, dict) and not i.get("explained")]
        sc = v.integ.get("scope") or {}
        if not unexpl and sc:
            unexpl = [f for fl in sc.get("files") or [] for f in fl.get("findings") or []
                      if f.get("category") == "out_of_scope" and not f.get("justified")]
        if unexpl:
            reasons.append(f"{len(unexpl)} change(s) outside the issue's scope not explained")
        if sc.get("verdict") == "unverified" or (sc.get("counts") or {}).get("unverified"):
            reasons.append("some changes could not be verified by the scope check")
        rs = v.integ.get("refs") or {}
        rem = (rs.get("counts") or {}).get("removed_symbols") or \
            ((v.integ.get("record") or {}).get("removed_symbols") or {}).get("total") or 0
        if rem:
            dang = (rs.get("counts") or {}).get("dangling") or 0
            reasons.append(f"{rem} thing(s) removed" + (f", {dang} still referenced elsewhere" if dang else ""))
        if v.vetoes:
            reasons.append(f"the reviewer vetoed {v.vetoes} attempt(s) before it passed")
        if reasons:
            items.append((3, f"**Review #{n} {v.title}**: " + "; ".join(reasons) + "."
                             + (f" Record: {link}" if link else "")))
        if v.decision:
            nh = [ln.strip().lstrip("-* ").strip() for ln in v.decision.section("edge cases").splitlines()
                  if re.match(r"(?i)^\s*[-*]?\s*not handled", ln)]
            asm = _first_line(v.decision.section("assumptions"), 130)
            bits = []
            if nh:
                nh0 = _short(re.sub(r"(?i)^not handled:?\s*", "", nh[0]), 130)
                bits.append(f"not handled: \"{nh0}\"")
            if asm and not re.match(r"(?i)^(none|no assumptions)", asm):
                bits.append(f"assumes: \"{asm}\"")
            if bits:
                items.append((4, f"#{n}: " + "; ".join(bits) + f" ({link})"))
    items.sort(key=lambda x: x[0])
    return items


def render(d: Digest) -> str:
    st = d.state
    views = d.issues
    total = len(views)
    done = [v for v in views.values() if v.committed]
    deferred = [v for v in views.values() if v.deferred_reason]
    env_n = sum(1 for v in deferred if v.blocker)
    not_reached = [v for v in views.values() if not v.committed and not v.deferred_reason
                   and v.attempts == 0]
    in_progress = [v for v in views.values() if not v.committed and not v.deferred_reason and v.attempts]
    parts = [f"{len(done)} of {total} issues done"]
    if len(deferred) - env_n:
        parts.append(f"{len(deferred) - env_n} need you")
    if env_n:
        parts.append(f"{env_n} blocked by a missing tool or permission")
    if in_progress:
        parts.append(f"{len(in_progress)} tried but not finished")
    if not_reached:
        parts.append(f"{len(not_reached)} not reached")
    L = [f"# Run digest: {st.get('repo_name') or '?'}", "",
         f"- **Branch:** `{st.get('branch', '?')}`, **Run:** `{d.run.run_id}` "
         f"({st.get('status', '?')})",
         f"- **Window:** {_fmt_time(d.window[0])} to {_fmt_time(d.window[1])}",
         f"- **Outcome:** {'; '.join(parts)}.", ""]

    L += ["## Needs your attention", ""]
    items = attention_items(d)
    if items:
        L += [f"{i}. {t}" for i, (_, t) in enumerate(items[:25], 1)]
        if len(items) > 25:
            L.append(f"- ...and {len(items) - 25} more lower-priority notes.")
    else:
        L.append("Nothing flagged. Skim *What was built* below.")
    L.append("")

    L += ["## What was built", ""]
    if done:
        L += ["| # | Title | In short | Trade-off | Record | Commit |", "|---|---|---|---|---|---|"]
        for v in sorted(done, key=lambda x: x.number):
            dec = v.decision
            short = _short(dec.section("in short"), 230) if dec else ""
            if not short:
                subj = re.sub(r"(?i)^issue #\d+:\s*", "", v.subject or "").strip()
                short = "(no record)" if not subj or subj == v.title else f"(no record) {subj}"
            trade = _first_line(dec.section("trade-off"), 140) if dec else ""
            L.append(f"| #{v.number} | {cr_cell(v.title)} | {cr_cell(short)} | {cr_cell(trade) or '-'} | "
                     f"{cr_cell(_rec_link(v)) or '-'} | `{v.sha[:8] or '?'}` |")
    else:
        L.append("Nothing was committed in this run.")
    L.append("")

    L += ["## Decisions by area", ""]
    areas: dict = {}
    for v in done:
        tags = v.decision.tags if v.decision and v.decision.tags else []
        if not tags:
            tops = sorted({f.split("/", 1)[0] if "/" in f else "(top level)" for f in v.files
                           if not f.startswith(DECISIONS_DIR)})
            tags = tops or ["(unknown)"]
        for t in tags:
            areas.setdefault(t, []).append(v.number)
    if areas:
        for t, ns in sorted(areas.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:20]:
            L.append(f"- **{t}:** " + ", ".join(f"#{n}" for n in sorted(ns)))
        if not any(v.decision and v.decision.tags for v in done):
            L.append("- *(Grouped by top-level folder: no component tags were recorded.)*")
    else:
        L.append("No committed issues to group.")
    L.append("")

    L += ["## How to respond", "",
          "- **Disagree with a trade-off:** file an issue titled "
          "\"Revisit NNNN: favour X because Y\", citing the record.",
          "- **Report a bug:** name the record and the edge case or assumption it breaks; "
          "\"Not handled\" items above are the first suspects.",
          "- **Roll back an issue:** `git revert <commit>` on the branch; the dangling "
          "reference check will flag anything that still uses what it removed.", ""]

    atts = [a for ir in d.run.issues.values() for a in ir.attempts]
    tin = sum(a.input_tokens for a in atts)
    tout = sum(a.output_tokens for a in atts)
    unc = sum(a.uncached for a in atts)
    L += ["---", "",
          f"**Cost:** {len(atts)} attempt(s); tokens in {tin:,} ({unc:,} uncached), out {tout:,}.", ""]
    gaps = []
    no_rec = [v.number for v in done if not v.decision]
    if no_rec:
        gaps.append(f"No decision record for {', '.join(f'#{n}' for n in no_rec)} "
                    "(runs before decision records existed have none; titles and commit "
                    "subjects are shown instead).")
    if done and not any(v.integ for v in done):
        gaps.append("No integrity records (scope/refs/record) in this run dir; "
                    "scope and consistency could not be checked here.")
    gaps += [g.split(": ", 1)[-1] for g in d.gaps if "no token usage" not in g][:5]
    nousage = sum(1 for g in d.gaps if "no token usage" in g)
    if nousage:
        gaps.append(f"{nousage} attempt log(s) had no token usage (token totals are a lower bound).")
    L.append("**Data gaps:** " + (" ".join(f"\n- {g}" for g in gaps) if gaps else "none."))
    return "\n".join(L).rstrip() + "\n"


def cr_cell(s: str) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ").strip()


def write_digest(run_ref: str) -> Path:
    d = gather(run_ref)
    out = d.run.path / DIGEST_NAME
    out.write_text(render(d), encoding="utf-8")
    return out


def latest_run(project_repo: Optional[str] = None) -> Optional[Path]:
    root = cr.runs_root()
    best = None
    for sp in root.glob("*/supervisor_state.json") if root.exists() else []:
        if project_repo:
            data = cr.load_json(sp) or {}
            hay = f"{data.get('repo_name', '')} {data.get('repo_path', '')}".lower()
            if project_repo.lower() not in hay:
                continue
        if best is None or sp.stat().st_mtime > best.stat().st_mtime:
            best = sp
    return best.parent if best else None


# ===========================================================================
# Self-test
# ===========================================================================

def self_test() -> int:
    import subprocess
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}")
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        repo, rd = tmp / "repo", tmp / "run"
        repo.mkdir(); rd.mkdir(); (rd / "integrity").mkdir()

        def g(*a):
            subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (repo / "README").write_text("x\n"); g("add", "."); g("commit", "-q", "-m", "init")
        g("checkout", "-q", "-b", "automation/x")
        facts1 = ("<!-- verified-facts:begin (generated by integrity record; do not edit) -->\n"
                  "## Verified facts\n\n| Component | Change | Lines +/- | Tags | Description |\n"
                  "|---|---|---|---|---|\n| `src/sensors/imu.py::read` | modified | +3/-1 | "
                  "sensors, embedded | Read IMU. |\n\n**Consistency**\n\n0 error(s), 1 warning(s)\n"
                  "- WARN: `src/sensors/imu.py::calib` changed but not mentioned\n"
                  "<!-- verified-facts:end -->\n")
        rec1 = ("# 0001: IMU reader\n\nStatus: partial · Issue: #1 · Date: 2026-10-09\n\n"
                "## In short\nThe robot now reads its tilt sensor ten times a second.\n\n"
                "## Trade-offs\nPolling is simpler than interrupts but wastes some power.\n\n"
                "## Assumptions\n- The sensor is always on the I2C bus at address 0x68.\n\n"
                "## Edge cases\n- Handled: sensor unplugged at start.\n"
                "- Not handled: sensor unplugged mid-run; readings freeze silently.\n\n" + facts1)
        rec2 = ("# 0002: Motor limits\n\nStatus: implemented · Issue: #2\n\n## In short\n"
                "Motors now stop at a safe speed limit.\n\n## Trade-offs\nNone.\n\n"
                "## Assumptions\nNone.\n\n## Edge cases\n- Handled: all.\n\n"
                "<!-- verified-facts:begin (x) -->\n| Component | Change | Lines +/- | Tags | Description |\n"
                "|---|---|---|---|---|\n| `motors/limit.py::cap` | added | +9/-0 | motors | Cap. |\n"
                "<!-- verified-facts:end -->\n")
        for n, (sub, txt, code) in {1: ("0001-imu.md", rec1, "src/sensors/imu.py"),
                                    2: ("0002-motors.md", rec2, "motors/limit.py")}.items():
            p = repo / "docs" / "decisions" / sub
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(txt, encoding="utf-8")
            (repo / code).parent.mkdir(parents=True, exist_ok=True)
            (repo / code).write_text("x = 1\n")
            g("add", "."); g("commit", "-q", "-m", f"Issue {n} work\n\nCloses #{n}")
        started = (dt.datetime.now().astimezone() - dt.timedelta(hours=1)).isoformat()
        state = {"repo_path": str(repo), "repo_name": "me/robot", "branch": "automation/x",
                 "run_dir": str(rd), "frozen_numbers": [1, 2, 3, 4],
                 "frozen_titles": {"1": "IMU reader", "2": "Motor limits", "3": "Camera",
                                   "4": "Docs"},
                 "started_at": started, "status": "finished",
                 "deferred": {"3": "agent declared BLOCKED (MISSING_TOOL): opencv not installed"},
                 "deferred_blockers": {"3": {"kind": "MISSING_TOOL", "subject": "opencv",
                                             "hint": "pip install opencv-python"}},
                 "env_blockers": {"MISSING_TOOL:opencv": {"kind": "MISSING_TOOL", "subject": "opencv",
                                                          "hint": "pip install opencv-python",
                                                          "issues": [3]}}}
        (rd / "supervisor_state.json").write_text(json.dumps(state), encoding="utf-8")
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        usage = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000,
                            "cached_input_tokens": 400, "output_tokens": 50}})
        for k, n in ((1, 1), (2, 1), (3, 2), (4, 3)):
            (rd / f"attempt_{k:03d}_issue_{n}_{ts}.log").write_text(usage + "\n")
        (rd / f"attempt_001_issue_1_{ts}_validation.txt").write_text("VERDICT: VETO\nreason\n")
        (rd / "integrity" / "record_issue-1_attempt-2.json").write_text(json.dumps({
            "verdict": "pass", "status": "partial", "record_path": "docs/decisions/0001-imu.md",
            "findings": [{"severity": "WARN", "code": "oos_unexplained", "message": "x"}],
            "out_of_scope": {"total": 1, "explained": 0,
                             "items": [{"target": "src/util.py", "explained": False}]}}))
        (rd / "integrity" / "refs_issue-1_attempt-2.json").write_text(json.dumps({
            "verdict": "dangling", "counts": {"removed_symbols": 2, "dangling": 1}}))
        (rd / "integrity" / "scope_issue-2_attempt-1.json").write_text(json.dumps({
            "verdict": "pass", "files": [], "counts": {"unverified": 0}}))

        out = write_digest(str(rd))
        text = out.read_text(encoding="utf-8")
        att = text.split("## Needs your attention", 1)[-1].split("## What was built", 1)[0]
        for sec in ("# Run digest", "## Needs your attention", "## What was built",
                    "## Decisions by area", "## How to respond", "**Data gaps:**"):
            check(f"section {sec!r}", sec in text)
        check("outcome line", "2 of 4 issues done" in text and "1 blocked by a missing tool" in text
              and "1 not reached" in text)
        check("env blocker first", att.strip().startswith("1. **Set up the environment**")
              and "pip install opencv-python" in att)
        check("deferred issue listed", "#3 Camera was set aside" in att)
        check("#1 flagged: partial, warn, oos, removed, veto",
              all(s in att for s in ("**partial**", "consistency warning", "outside the issue's scope",
                                     "2 thing(s) removed, 1 still referenced", "vetoed 1")))
        check("#2 not flagged", "Review #2" not in att)
        check("not handled quoted", "sensor unplugged mid-run" in att and "0x68" in att)
        check("built rows", "ten times a second" in text and "Polling is simpler" in text
              and "| #2 |" in text)
        check("areas from tags", "**sensors:** #1" in text and "**motors:** #2" in text)
        check("cost footer", "4 attempt(s)" in text and "4,000" in text)
        check("revert hint", "git revert" in text)
        check("short", len(text.splitlines()) < 80)
    print("\n" + ("ALL DIGEST SELF-TESTS PASSED" if ok else "SOME DIGEST SELF-TESTS FAILED"))
    return 0 if ok else 1


# ===========================================================================
# CLI
# ===========================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write <run dir>/DIGEST.md: a one-page run summary.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", help="run id (under the runs root) or run directory")
    g.add_argument("--latest", action="store_true", help="most recent run")
    g.add_argument("--self-test", action="store_true")
    ap.add_argument("--project-repo", help="with --latest: substring of the repo name/path")
    ap.add_argument("--print", action="store_true", help="also print the digest")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    ref = a.run
    if a.latest:
        p = latest_run(a.project_repo)
        if p is None:
            print("no matching run found", file=sys.stderr)
            return 1
        ref = str(p)
    out = write_digest(ref)
    if a.print:
        print(out.read_text(encoding="utf-8"))
    print(f"Digest: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

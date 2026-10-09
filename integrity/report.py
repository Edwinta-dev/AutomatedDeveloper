"""
report.py — turn classification results into the JSON record, the Markdown report
for humans, and the plain stdout summary the agent sees when the gate fails.
"""
from __future__ import annotations

from typing import Optional

from scope import (ALLOWED, DIFF_LINES_PER_FINDING, IN_SCOPE, OUT_OF_SCOPE, UNVERIFIED,
                   FileResult, Finding)

SCHEMA_VERSION = 1
DIFF_LINES_TOTAL = 300


def all_findings(files: list[FileResult]) -> list[Finding]:
    return [f for fr in files for f in fr.findings]


def verdict_of(files: Optional[list[FileResult]]) -> str:
    if files is None:
        return "skipped"
    cats = {f.category for f in all_findings(files)}
    if OUT_OF_SCOPE in cats:
        return "violation"
    if UNVERIFIED in cats:
        return "unverified"
    return "pass"


def build_record(*, files: Optional[list[FileResult]], scope_declared: Optional[list[str]],
                 mode: str, issue: Optional[int], attempt: Optional[int], commit: Optional[str],
                 compared: str, started: str, finished: str, version: str,
                 skipped_reason: str = "") -> dict:
    fs = all_findings(files or [])
    counts = {k: sum(1 for f in fs if f.category == k)
              for k in (IN_SCOPE, ALLOWED, OUT_OF_SCOPE, UNVERIFIED)}
    counts["justified"] = sum(1 for f in fs if f.justified)
    counts["whitespace_only"] = sum(1 for f in fs if f.whitespace_only)
    lines = {k: sum(f.lines_added + f.lines_removed for f in fs if f.category == k)
             for k in (IN_SCOPE, OUT_OF_SCOPE)}
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "integrity scope gate", "tool_version": version,
        "issue": issue, "attempt": attempt, "commit": commit, "compared": compared,
        "mode": mode, "verdict": verdict_of(files),
        "skipped_reason": skipped_reason,
        "scope_declared": scope_declared or [],
        "files": [{"path": fr.path, "old_path": fr.old_path, "change": fr.change,
                   "adapter": fr.adapter, "error": fr.error,
                   "findings": [f.to_dict(with_diff=f.category in (OUT_OF_SCOPE, UNVERIFIED))
                                for f in fr.findings]}
                  for fr in (files or [])],
        "counts": counts, "lines_changed": lines,
        "started": started, "finished": finished,
    }


def _summary_line(rec: dict) -> str:
    c = rec["counts"]
    head = f"SCOPE GATE: {rec['verdict'].upper()} ({rec['mode']} mode)"
    if rec["verdict"] == "skipped":
        return f"{head} - {rec['skipped_reason'] or 'no scope declared: skipped'}"
    extra = []
    if c["justified"]:
        extra.append(f"{c['justified']} justified")
    if c["whitespace_only"]:
        extra.append(f"{c['whitespace_only']} whitespace-only")
    return (f"{head} - {c['in_scope']} in scope, {c['allowed']} allowed, "
            f"{c['out_of_scope']} out of scope, {c['unverified']} unverified"
            + (f" ({', '.join(extra)})" if extra else "") + f"  [{rec['compared']}]")


def _target(f: dict) -> str:
    return f["path"] if f["component"] == "<file>" else f"{f['path']}::{f['component']}"


def _by_cat(rec: dict, cat: str) -> list[dict]:
    return [f for fr in rec["files"] for f in fr["findings"] if f["category"] == cat]


def render_stdout(rec: dict) -> str:
    out = [_summary_line(rec)]
    if rec["verdict"] == "skipped":
        return "\n".join(out)
    out.append("Declared scope: " + (", ".join(rec["scope_declared"]) or "(empty)"))
    oos, unv = _by_cat(rec, OUT_OF_SCOPE), _by_cat(rec, UNVERIFIED)
    if not oos and not unv:
        for f in _by_cat(rec, IN_SCOPE) + _by_cat(rec, ALLOWED):
            out.append(f"  {f['category']:<9} {_target(f)} ({f['change']}"
                       + (f", {f['reason']}" if f["category"] == ALLOWED else "") + ")")
        return "\n".join(out)

    budget = DIFF_LINES_TOTAL
    if oos:
        out += ["", f"OUT-OF-SCOPE CHANGES ({len(oos)}): these edits are outside the issue's "
                    "declared **Scope:**"]
        for i, f in enumerate(oos, 1):
            flags = [f["change"], f"+{f['lines_added']}/-{f['lines_removed']} lines"]
            if f["whitespace_only"]:
                flags.append("WHITESPACE-ONLY")
            if f["justified"]:
                flags.append("explained in SCOPE_NOTES")
            out.append(f"[{i}] {_target(f)}  ({', '.join(flags)}) - {f['reason']}")
            diff = f.get("diff", [])[:max(0, min(DIFF_LINES_PER_FINDING, budget))]
            budget -= len(diff)
            out += ["    " + d for d in diff]
            if len(diff) < len(f.get("diff", [])) or f.get("diff_truncated"):
                out.append("    ... (diff truncated)")
    if unv:
        out += ["", f"UNVERIFIED FILES ({len(unv)}): scope cannot be checked on these"]
        for f in unv:
            out.append(f"  - {f['path']}: {f['reason']}")
    out += ["", "WHAT TO DO:"]
    if oos:
        out += [
            "  1. Revert every change listed under OUT-OF-SCOPE CHANGES so those files/"
            "functions match HEAD again",
            "     (whole file: `git checkout HEAD -- <path>`; one function: undo just that edit;"
            " restore deleted/renamed files).",
            "     Whitespace-only and reformatting edits count too - do not touch code outside "
            "the scope.",
            "  2. Only if a change is truly required to complete this issue, keep it and explain "
            "it in your result block:",
            "         SCOPE_NOTES:",
            "         - <path>::<component>: <why this change is required>",
        ]
        if rec["mode"] == "enforce":
            out.append("     NOTE: in enforce mode SCOPE_NOTES are recorded for the reviewer but "
                       "do NOT lift this block;")
            out.append("     a human must widen the issue's **Scope:** line. Prefer reverting.")
        else:
            out.append("     (report mode: this attempt is not blocked, but every out-of-scope "
                       "change is recorded for review.)")
    if unv:
        out.append("  - Fix the parse errors above (the file must be valid source) so the scope "
                   "can be verified.")
    return "\n".join(out)


def _esc(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def render_markdown(rec: dict) -> str:
    title = (f"issue #{rec['issue']}" if rec.get("issue") is not None
             else f"commit {rec.get('commit', '')[:10]}" if rec.get("commit") else "working tree")
    if rec.get("attempt") is not None:
        title += f", attempt {rec['attempt']}"
    md = [f"# Scope report — {title}", "", f"**{_summary_line(rec)}**", ""]
    md.append(f"- Verdict: `{rec['verdict']}` · mode: `{rec['mode']}` · compared: "
              f"`{rec['compared']}`")
    md.append(f"- Declared scope: " + (", ".join(f"`{s}`" for s in rec["scope_declared"])
                                      or "_none_"))
    lc = rec["lines_changed"]
    md.append(f"- Lines changed: {lc[IN_SCOPE]} in scope, {lc[OUT_OF_SCOPE]} out of scope")
    md.append(f"- Run: {rec['started']} → {rec['finished']} ({rec['tool']} "
              f"{rec['tool_version']})")
    if rec["verdict"] == "skipped":
        return "\n".join(md) + "\n"
    md += ["", "## Changes", "",
           "| File | Component | Change | Category | Reason | Tags | Description |",
           "|---|---|---|---|---|---|---|"]
    for fr in rec["files"]:
        if not fr["findings"]:
            md.append(f"| `{fr['path']}` | — | {fr['change']} | (no component changes) | "
                      f"| | |")
        for f in fr["findings"]:
            cat = f["category"] + (" (whitespace-only)" if f["whitespace_only"] else "")
            if f["justified"]:
                cat += " (justified)"
            md.append(f"| `{f['path']}` | `{f['component']}` | {f['change']} | {cat} | "
                      f"{_esc(f['reason'])} | {_esc(', '.join(f['tags']))} | "
                      f"{_esc(f['description'])} |")
    oos = _by_cat(rec, OUT_OF_SCOPE)
    if oos:
        md += ["", "## Out-of-scope changes", ""]
        for f in oos:
            md.append(f"### `{_target(f)}` — {f['change']}"
                      + (" (whitespace-only)" if f["whitespace_only"] else ""))
            md.append("")
            md.append(f"Reason: {f['reason']}")
            md.append("")
            md.append("Agent justification (SCOPE_NOTES, recorded only — does not change the "
                      "verdict): " + (f"_{f['justification']}_" if f["justified"] else "none"))
            md += ["", "```diff", *f.get("diff", []),
                   *(["... (truncated)"] if f.get("diff_truncated") else []), "```", ""]
    unv = _by_cat(rec, UNVERIFIED)
    if unv:
        md += ["", "## Unverified", ""]
        md += [f"- `{f['path']}`: {f['reason']}" for f in unv]
    return "\n".join(md) + "\n"

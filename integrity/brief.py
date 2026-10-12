"""
brief.py — slices of earlier decision records for the components an issue touches.
Standard library only.

    integrity.py brief --repo R (--components "a.py::f, b/**" | --issue-file F | --paths ...)
                       --mode debug|feature [--max-words 600] [--ref <branch>]

Finds the records under the record dir (config "record.dir", default docs/decisions) whose
Components, verified-facts block or other mentions touch the given components (path-suffix
aware, using record.py's resolution), and prints selected sections, newest first:

    debug:   Traps, Assumptions, Edge cases (Not handled only), Key parameters, Not verified
    feature: Traps, Alternatives considered, Trade-offs, To change this

v1 records (no Traps / Not verified) contribute the sections they have. The output is
hypotheses, not proof: it opens with a header saying so. Truncated to --max-words on whole
bullets. Knows nothing about the runner (engine/).

Relevance is IDF-weighted (score_records): a file matched by a target adds 1/(records naming
it); a file more than 40% of the records name adds nothing unless the match is a component
only a few records name; records of issues the issue text references (`#N`) always count.
Below --min-score (default 0.5) nothing is printed.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Optional

from record import (BEGIN, END, Trees, _record_text, comp_refs, find_section, parse_markdown,
                    path_refs)
from scope import EMPTY_TREE, ensure_repo, git, git_text, norm_path, parse_scope

HEADER = ("Hypotheses from earlier decision records — check them against the code; "
          "they are not proof.")
MODES = {
    "debug": ["Traps", "Assumptions", "Edge cases", "Key parameters", "Not verified"],
    "feature": ["Traps", "Alternatives considered", "Trade-offs", "To change this"],
}
UNVERIFIED_RE = re.compile(r"\bnot (?:verified|measured|tested|checked)\b|\buntested\b",
                           re.IGNORECASE)
LABELS = {"Edge cases": "Edge cases not handled"}
COMMON_SHARE = 0.4     # a file named by more than this share of the records adds nothing
MIN_CORPUS = 5         # below this many records every file counts (no "common" files)
REF_BONUS = 1.0        # a record of an issue the issue text references (#N)
MIN_SCORE = 0.5        # CLI default: below this, print nothing ("no specific records")


# ---------------------------------------------------------------------------
# "Not handled" edge cases (bullet / bold / sub-bullet / table variants)
# ---------------------------------------------------------------------------

NH_RE = re.compile(r"\bnot\s+handled\b", re.IGNORECASE)
H_RE = re.compile(r"^\W*handled\b", re.IGNORECASE)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _strip_bullet(line: str) -> str:
    return re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()


def _after_label(s: str) -> str:
    """Text after a 'Not handled' label: '**Not handled:** x', 'Not handled - x', ..."""
    m = re.search(r"not\s+handled\W*?(?:\*\*|__)?\s*[:\-–—]?\s*(?:\*\*|__)?\s*(.*)$", s,
                  re.IGNORECASE)
    return m.group(1).strip(" *_:") if m else ""


def not_handled(body: str) -> list[str]:
    """Items of an Edge cases section marked not handled. Accepts
    `- Not handled: x`, `- **Not handled:**` / `**Not handled**` / `### Not handled` followed
    by sub-bullets or bullets, and table rows with a 'not handled' cell."""
    out: list[str] = []
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if any(NH_RE.search(c) for c in cells) and not all(set(c) <= set("-: ")
                                                               for c in cells):
                rest = [c for c in cells if c and not NH_RE.fullmatch(c.strip("* _"))]
                rest = [c if not NH_RE.search(c) else _after_label(c) for c in rest]
                rest = [c for c in rest if c]
                if rest:
                    out.append(" — ".join(rest))
            i += 1
            continue
        if NH_RE.search(s) and (re.match(r"^(?:[-*+]|\d+[.)])?\s*(?:#+\s*)?(?:\*\*|__)?\s*not\s+"
                                         r"handled", s, re.IGNORECASE)):
            head = _after_label(_strip_bullet(s).lstrip("# "))
            base = _indent(line)
            is_bullet = bool(re.match(r"^\s*(?:[-*+]|\d+[.)])\s", line))
            if head:
                out.append(head)
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                ns = nxt.strip()
                if not ns:
                    # a blank line ends a bullet's sub-list unless more sub-bullets follow
                    k = j + 1
                    while k < len(lines) and not lines[k].strip():
                        k += 1
                    if k < len(lines) and re.match(r"^\s*(?:[-*+]|\d+[.)])\s", lines[k]) and \
                            (_indent(lines[k]) > base or not is_bullet) and \
                            not H_RE.match(_strip_bullet(lines[k]).strip("*_ ")):
                        j = k
                        continue
                    break
                if is_bullet and _indent(nxt) <= base:
                    break
                if not is_bullet and not re.match(r"^\s*(?:[-*+]|\d+[.)])\s", nxt):
                    break
                item = _strip_bullet(nxt)
                if H_RE.match(item.strip("*_ ")) or ns.startswith("#"):
                    break
                if re.match(r"^\s*(?:[-*+]|\d+[.)])\s", nxt):
                    out.append(item)
                elif out:
                    out[-1] += " " + ns
                j += 1
            i = j
            continue
        i += 1
    return [re.sub(r"\s+", " ", x).strip() for x in out if x.strip()]


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

def _targets(components: str) -> list[tuple[str, Optional[str]]]:
    out = []
    for part in re.split(r"[,\n]", components or ""):
        raw = part.strip().strip("`").strip()
        if not raw:
            continue
        p, sep, q = raw.partition("::")
        out.append((norm_path(p), q.strip() or None if sep else None))
    return out


def _list_records(repo: Path, ref: Optional[str], rdir: str) -> list[str]:
    rdir = rdir.rstrip("/")
    if ref is None:
        base = repo / rdir
        files = sorted(f"{rdir}/{x.name}" for x in base.glob("*.md")) if base.is_dir() else []
    else:
        out = git_text(repo, "ls-tree", "--name-only", ref, "--", rdir + "/")
        files = sorted(norm_path(x) for x in out.splitlines() if x.endswith(".md"))
    return [f for f in files if re.match(r"^\d+-", f.rsplit("/", 1)[-1])]


def _mentions(text: str, trees: Trees) -> set[str]:
    """Repo paths a record (rationale and facts block) names, resolved."""
    raw = [p for p, _ in comp_refs(text)] + path_refs(text)
    out: set[str] = set()
    for r in dict.fromkeys(raw):
        st, ps = trees.resolve(r)
        out.update(ps if st in ("exact", "suffix", "ambiguous") else [norm_path(r).rstrip("/")])
    return out


def _touches(paths: set[str], target: str) -> bool:
    t = target.rstrip("/")
    glob = any(c in t for c in "*?[")
    for p in paths:
        p = p.rstrip("/")
        if glob:
            if fnmatch.fnmatch(p, t) or fnmatch.fnmatch(p, t.rstrip("*").rstrip("/") + "/*") \
                    or p.startswith(t.split("*")[0].rstrip("/") + "/") and t.split("*")[0]:
                return True
            continue
        if p == t or p.endswith("/" + t) or t.endswith("/" + p) and "/" in p \
                or p.startswith(t + "/") or t.startswith(p + "/"):
            return True
    return False


def _bullets(body: str) -> list[str]:
    """Split a section body into whole items (bullets with their sub-lines, paragraphs,
    table rows)."""
    items: list[str] = []
    for line in body.split("\n"):
        if not line.strip():
            if items and items[-1] != "":
                items.append("")
            continue
        if re.match(r"^(?:[-*+]|\d+[.)])\s", line) or line.lstrip().startswith("|") \
                or not items or items[-1] == "":
            items.append(line.rstrip())
        else:
            items[-1] += "\n" + line.rstrip()
    sep = re.compile(r"\|[\s|:\-]*\|?")
    out = []
    for i, x in enumerate(items):
        if not x.strip() or sep.fullmatch(x.strip()):
            continue
        nxt = items[i + 1].strip() if i + 1 < len(items) else ""
        if x.lstrip().startswith("|") and nxt and sep.fullmatch(nxt):
            continue                                  # a table's header row
        out.append(x)
    return out


def issue_refs(issue_text: str) -> tuple[Optional[int], set[int]]:
    """(the issue's own number from a `# #N` header, other `#N` numbers in the text)."""
    t = issue_text or ""
    m = re.match(r"^\s*#\s*#(\d+)", t)
    own = int(m.group(1)) if m else None
    nums = {int(x) for x in re.findall(r"(?<![\w&/#])#(\d{1,5})\b", t)}
    return own, nums - ({own} if own is not None else set())


def _comp_hit(mq: str, q: str) -> bool:
    return mq == q or mq.startswith(q + ".") or q.startswith(mq + ".")


def score_records(repo: Path, trees: Trees, ref: Optional[str], rdir: str,
                  targets: list[tuple[str, Optional[str]]], issue_text: str = "",
                  min_score: float = 0.0) -> list[dict]:
    """IDF-style relevance. A target matched through a file adds 1/(records naming that
    file); files named by more than COMMON_SHARE of the records add nothing unless the
    match is at component level and that component is rare (1/(records naming it)).
    Records of issues the issue text references (`#N`) always qualify. Kept: score >=
    min_score. Sorted by score, then newest."""
    own, refs_ = issue_refs(issue_text)
    recs = []
    for path in _list_records(repo, ref, rdir):
        num = int(re.match(r"^(\d+)", path.rsplit("/", 1)[-1]).group(1))
        if own is not None and num == own:
            continue                                   # the issue's own record
        text = _record_text(trees.read(path, "new")) or ""
        files = _mentions(text, trees)
        comps = set()
        for p, q in comp_refs(text):
            st, ps = trees.resolve(p, q)
            for x in (ps if st in ("exact", "suffix", "ambiguous") else [norm_path(p)]):
                comps.add((x, q))
        recs.append({"path": path, "num": num, "text": text, "files": files, "comps": comps})
    n = max(len(recs), 1)
    df: dict = {}
    for r in recs:
        for f in r["files"]:
            df[f] = df.get(f, 0) + 1
    cdf: dict = {}
    for t, q in targets:
        if q:
            cdf[(t, q)] = sum(1 for r in recs if any(_touches({p}, t) and _comp_hit(mq, q)
                                                    for p, mq in r["comps"]))
    out = []
    for r in recs:
        score, why = 0.0, []
        for t, q in targets:
            fs = [p for p in r["files"] if _touches({p}, t)]
            if not fs:
                continue
            s_file = max((1.0 / df[p] for p in fs
                          if df.get(p) and (n < MIN_CORPUS or df[p] / n <= COMMON_SHARE)),
                         default=0.0)
            s_comp = 0.0
            if q and any(_touches({p}, t) and _comp_hit(mq, q) for p, mq in r["comps"]):
                c = cdf.get((t, q), 0)
                if c and (n < MIN_CORPUS or c / n <= COMMON_SHARE):
                    s_comp = 1.0 / c
            s = max(s_file, s_comp)
            if s > 0:
                score += s
                why.append(f"{t}{'::' + q if q and s_comp >= s_file else ''} ({s:.2f})")
        if r["num"] in refs_:
            score += REF_BONUS
            why.append(f"#{r['num']} referenced in the issue")
        if score <= 0 or (score < min_score and r["num"] not in refs_):
            continue
        text = r["text"]
        before = text.split(BEGIN)[0] + (text.split(END, 1)[1] if END in text else "")
        parsed = parse_markdown(before)
        out.append({"path": r["path"], "num": r["num"], "title": parsed.title or r["path"],
                    "date": parsed.date or "", "status": parsed.status, "parsed": parsed,
                    "score": round(score, 3), "why": why})
    out.sort(key=lambda r: (r["score"], r["date"], r["num"]), reverse=True)
    return out


def brief(repo: Path, *, targets: list[tuple[str, Optional[str]]], mode: str,
          max_words: int = 600, ref: Optional[str] = None,
          record_dir: str = "docs/decisions", issue_text: str = "",
          min_score: float = 0.0) -> tuple[str, list[dict]]:
    """-> (text, matched records). Empty text when nothing scores at least min_score."""
    if mode not in MODES:
        raise ValueError(f"mode must be debug|feature, not {mode!r}")
    repo = ensure_repo(Path(repo))
    if ref is not None:
        p = git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
        if p.returncode != 0:
            raise ValueError(f"unknown ref: {ref}")
        ref = p.stdout.decode().strip()
    trees = Trees(repo, ref if ref else EMPTY_TREE, ref)
    matched = score_records(repo, trees, ref, norm_path(record_dir), targets, issue_text,
                            min_score)
    if not matched:
        return "", []
    lines = [HEADER, ""]
    budget = max_words - len(HEADER.split())
    used = 0
    truncated = False
    blocks = []
    for r in matched:
        head = f"### {r['title']} ({r['path']}" + (f", {r['status']}" if r["status"] else "") + ")"
        block: list[tuple[str, str]] = []           # (section label, item)
        for name in MODES[mode]:
            sec = find_section(r["parsed"], name)
            if sec is None:
                continue
            items = [f"- {x}" for x in not_handled(sec[1])] if name == "Edge cases" \
                else _bullets(sec[1])
            items = [x for x in items if x.strip()]
            if not items or (len(items) == 1 and re.match(
                    r"^[-*]?\s*(none|n/?a|not applicable)\b", items[0].strip(), re.I)
                    and len(items[0].split()) <= 6):
                continue
            block += [(LABELS.get(name, name), x) for x in items]
        if mode == "debug" and find_section(r["parsed"], "Not verified") is None:
            ver = find_section(r["parsed"], "How it was verified")   # v1: inline claims
            nv = [x for x in _bullets(ver[1]) if UNVERIFIED_RE.search(x)] if ver else []
            block += [("Not verified (from How it was verified)", x) for x in nv]
        if block:
            blocks.append((head, block))
    # Share the budget between records (newest first); words a record leaves unused
    # carry over to the next one, so one long record cannot crowd out the rest.
    left = budget
    for k, (head, block) in enumerate(blocks):
        share = max(left // (len(blocks) - k), 40)
        cap = min(share, left)
        # pick items round-robin across sections (each section's first item, then each
        # one's second, ...) so every section is represented, then print them grouped
        by_sec: dict = {}
        for lab, item in block:
            by_sec.setdefault(lab, []).append(item)
        order = []
        depth = 0
        while any(depth < len(v) for v in by_sec.values()):
            order += [(lab, depth) for lab, v in by_sec.items() if depth < len(v)]
            depth += 1
        w_used = len(head.split())
        keep: set = set()
        for lab, i in order:
            w = len(by_sec[lab][i].split()) + (len(lab.split()) if not any(
                x[0] == lab for x in keep) else 0)
            if w_used + w <= cap:
                keep.add((lab, i))
                w_used += w
        dropped = len(block) - len(keep)
        out = [head]
        for lab, items in by_sec.items():
            idx = [i for i in range(len(items)) if (lab, i) in keep]
            if idx:
                out.append(f"**{lab}**")
                out += [items[i] for i in idx]
        if len(out) == 1:
            truncated = True
            continue
        if dropped:
            truncated = True
            out.append(f"(+{dropped} more item(s) in the record)")
        lines += out + [""]
        used += w_used
        left -= w_used
    if len(lines) == 2 and not truncated:
        return "", matched
    if truncated:
        lines.append(f"(truncated to {max_words} words; read the full records under "
                     f"{record_dir}/)")
    for r in matched:
        r.pop("parsed", None)
    return "\n".join(lines).rstrip() + "\n", matched


def targets_from(components: Optional[str], issue_text: Optional[str],
                 paths: Optional[list[str]]) -> list[tuple[str, Optional[str]]]:
    if components:
        return _targets(components)
    if paths:
        return [(norm_path(p), None) for p in paths]
    sc = parse_scope(issue_text or "")
    if sc is None:
        if issue_refs(issue_text or "")[1]:
            return []                      # no Scope line: referenced records only
        raise ValueError("no **Scope:** line and no #N reference in the issue file")
    return [(e.path, e.qualname) for e in sc.entries]


# ---------------------------------------------------------------------------
# self-tests
# ---------------------------------------------------------------------------

NH_CASES = [
    ("- Handled: a\n- Not handled: b gives a 500\n", ["b gives a 500"]),
    ("- **Handled:**\n  - a\n- **Not handled:**\n  - b;\n  - c.\n\n## x", ["b;", "c."]),
    ("**Not handled**\n\n- b\n- c\n\n**Handled**\n- a\n", ["b", "c"]),
    ("- Handled:\n  - a\n- Not handled:\n  - b\n  - c\n", ["b", "c"]),
    ("| Case | Status | Symptom |\n|---|---|---|\n| x | handled | - |\n"
     "| y | not handled | stale value |\n", ["y — stale value"]),
    ("### Not handled\n- b\n", ["b"]),
    ("- No station: status insufficient.\n", []),
]


def self_test(check, integrity_py: str) -> None:
    import os
    import subprocess
    import sys
    import tempfile
    for i, (body, want) in enumerate(NH_CASES):
        got = not_handled(body)
        check(f"brief: Not handled parser variant {i}", got == want, (got, want))
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}

        def g(*a):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo,
                           check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def w(rel, text):
            f = repo / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")

        g("init", "-q")
        w("fw/node/retry.h", "#define RETRY 3\n")
        w("Backend/koi/api/x.py", "def f():\n    return 1\n")
        w("docs/decisions/0003-old.md", "# 0003: Old retry\nStatus: implemented · Date: 2026-09-01"
          "\n## Approach\n`node/retry.h` retries.\n## Assumptions\n- Clock is set.\n"
          "## Edge cases\n- Not handled: clock unset gives 1970 rows.\n## Trade-offs\n- Slow.\n")
        w("docs/decisions/0009-new.md", "# 0009: New retry\nStatus: partial · Date: 2026-10-12\n"
          "## Traps: why the obvious fix is wrong\n- Key needs boot_id: cycle_seq restarts.\n"
          "## Components\n- `fw/node/retry.h::RETRY` - attempts\n## Edge cases\n"
          "- **Handled:** dupes\n- **Not handled:**\n  - two nodes one pond\n"
          "## Not verified\n- Drift over deep sleep.\n## Alternatives considered\n- None.\n")
        w("docs/decisions/0010-other.md", "# 0010: Other\nStatus: implemented\n"
          "## Traps\n- unrelated `Backend/koi/api/x.py`\n")
        g("add", "-A")
        g("commit", "-qm", "init")
        text, m = brief(repo, targets=_targets("retry.h::RETRY"), mode="debug")
        check("brief debug: matching records newest first, header, sections",
              text.startswith(HEADER) and [r["num"] for r in m] == [9, 3]
              and text.index("0009") < text.index("0003") and "boot_id" in text
              and "two nodes one pond" in text and "dupes" not in text
              and "Drift over deep sleep" in text and "Clock is set" in text
              and "0010" not in text, text)
        text, _ = brief(repo, targets=_targets("fw/node/**"), mode="feature")
        check("brief feature: glob target, Traps + Trade-offs, waiver sections dropped",
              "boot_id" in text and "Slow." in text and "Alternatives" not in text
              and "Assumptions" not in text, text)
        text, _ = brief(repo, targets=_targets("retry.h"), mode="debug", max_words=40)
        check("brief: truncated on whole bullets with a note",
              "(truncated to 40 words" in text and "boot_id" in text
              and "0003" not in text and len(text.split()) < 60, text)
        text, m = brief(repo, targets=_targets("nothing/here.py"), mode="debug")
        check("brief: no match -> empty", text == "" and m == [], text)
        w("issue.md", "# #12 Fix retry\n\n**Scope:** `fw/node/retry.h`\n")
        cp = subprocess.run([sys.executable, integrity_py, "brief", "--repo", str(repo),
                             "--issue-file", str(repo / "issue.md"), "--mode", "debug",
                             "--ref", "HEAD"], stdout=subprocess.PIPE, text=True, env=env,
                            encoding="utf-8")
        check("brief CLI: --issue-file Scope line, --ref, exit 0",
              cp.returncode == 0 and "boot_id" in cp.stdout, cp.stdout)

        # relevance: a file most records name adds nothing; a rare component does;
        # an issue the text references always qualifies; below the threshold -> nothing
        w("app/settings.py", "class Settings:\n    x = 1\n")
        w("app/worker.py", "def poll():\n    return 1\n\n\ndef rare():\n    return 2\n")
        for k in range(20, 26):
            extra = "`app/worker.py::rare` " if k == 21 else ""
            w(f"docs/decisions/00{k}-r{k}.md", f"# 00{k}: R{k}\nStatus: implemented · "
              f"Date: 2026-10-0{k - 19}\n## Traps\n- trap {k} in `app/settings.py` and "
              f"`app/worker.py::poll` {extra}\n")
        w("docs/decisions/0030-regions.md", "# 0030: Regions\nStatus: implemented · "
          "Date: 2026-10-09\n## Traps\n- region trap\n")
        g("add", "-A")
        g("commit", "-qm", "more")
        own = "# #31 Engine\n\nUses the regions of #30.\n\n**Scope:** `app/settings.py`, " \
              "`app/worker.py::poll`\n"
        _, m = brief(repo, targets=targets_from(None, own, None), mode="debug",
                     issue_text=own, min_score=MIN_SCORE)
        check("brief relevance: common file/component excluded, referenced #30 included",
              [r["num"] for r in m] == [30], [(r["num"], r["score"]) for r in m])
        rare = own.replace("`app/worker.py::poll`", "`app/worker.py::rare`")
        _, m = brief(repo, targets=targets_from(None, rare, None), mode="debug",
                     issue_text=rare, min_score=MIN_SCORE)
        check("brief relevance: a rare component in a common file still matches",
              21 in [r["num"] for r in m] and 22 not in [r["num"] for r in m],
              [(r["num"], r["score"]) for r in m])
        none_ = "# #31 Engine\n\n**Scope:** `app/settings.py`\n"
        text, m = brief(repo, targets=targets_from(None, none_, None), mode="debug",
                        issue_text=none_, min_score=MIN_SCORE)
        check("brief relevance: nothing specific -> empty output", text == "" and m == [],
              [(r["num"], r["score"]) for r in m])

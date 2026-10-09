#!/usr/bin/env python3
"""
integrity.py — deterministic SCOPE GATE + component index (standard library only).

Each issue declares which components it may change, in its body:

    **Scope:** `src/hsv.py::analyse_frame`, `src/hsv.py::HSV.threshold`, `src/vision/**`

The gate compares HEAD with the working tree (the agent's uncommitted edits) — or,
with --commit, a commit with its parent — splits every changed file into
components (functions/classes/methods for Python, the whole file otherwise) and
classifies each changed component as in_scope / allowed / out_of_scope /
unverified. It writes a JSON record + Markdown report and prints a short summary;
on a violation the summary lists the offending diffs and tells the agent what to do.

This package is deliberately independent of the runner (engine/): it talks to it
only through CLI args, AGENT_* env vars, files and exit codes.

    python integrity/integrity.py gate [--repo R] [--config project.json] [--mode enforce]
                                       [--scope "a.py::f, b/**"] [--commit SHA]
    python integrity/integrity.py index  --repo R [--out idx.json] [--paths GLOB ...]
    python integrity/integrity.py lookup --repo R [--tag T ...] [--any] [--name S] [--path GLOB]
    python integrity/integrity.py bench  --repo R [--samples 200] [--seed 1]
    python integrity/integrity.py refs   [--repo R] [--commit SHA] [--mode enforce]  (refs.py)
    python integrity/integrity.py record [--repo R] [--issue N] [--commit SHA] [--no-write]  (record.py)
    python integrity/integrity.py self-test

Wire into validate.json (runs with cwd = target repo):
    {"label": "Scope gate",
     "argv": ["__PY__", "<path>/integrity/integrity.py", "gate", "--config", "__PROJECT_CONFIG__"]}

Exit codes (gate): 0 = pass / skipped / report mode, 1 = violation or unverified in
enforce mode, 2 = misconfigured (not a git repo, bad args, bad config).
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import io
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tokenize
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from components import PythonAdapter, adapter_for, FALLBACK, parse_tags  # noqa: E402
from report import build_record, render_markdown, render_stdout  # noqa: E402
from scope import (OUT_OF_SCOPE, ALLOWED, IN_SCOPE, GitError, Scope, classify,  # noqa: E402
                   collect_changes, ensure_repo, git, git_text, glob_match, load_config,
                   norm_path, parse_entries, parse_scope, parse_scope_notes)

VERSION = "0.1.0"
PASS, FAIL, MISCONFIGURED = 0, 1, 2
MAX_INDEX_BYTES = 1_000_000


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _int_or_none(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _read(path: Optional[str]) -> str:
    if not path:
        return ""
    p = Path(path)
    return p.read_text(encoding="utf-8-sig", errors="replace") if p.is_file() else ""


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

def run_gate(repo: Path, *, cfg: dict, issue_text: str = "", scope_text: Optional[str] = None,
             result_text: str = "", mode: Optional[str] = None, commit: Optional[str] = None,
             issue: Optional[int] = None, attempt: Optional[int] = None,
             record_dir: Optional[Path] = None) -> tuple[int, dict, str]:
    """-> (exit code, record, stdout text). Raises GitError / ValueError when misconfigured."""
    started = _now()
    mode = mode or cfg["mode"]
    if mode not in ("report", "enforce"):
        raise ValueError(f"mode must be report|enforce, not {mode!r}")
    if issue is None:
        m = re.match(r"^\s*#\s*#(\d+)", issue_text or "")
        issue = int(m.group(1)) if m else None
    repo = ensure_repo(repo)
    scope: Optional[Scope] = (Scope(parse_entries(scope_text)) if scope_text is not None
                              else parse_scope(issue_text or ""))
    files = None
    compared = "HEAD..working tree" if not commit else f"{commit}^..{commit}"
    commit_sha = None
    if commit:
        commit_sha = git_text(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()
    if scope is not None:
        exclude = []
        if record_dir is not None:
            try:
                exclude.append(Path(record_dir).resolve().relative_to(repo.resolve()).as_posix())
            except ValueError:
                pass
        cs = collect_changes(repo, commit, tuple(exclude))
        compared = cs.label
        files = classify(cs, scope, cfg, parse_scope_notes(result_text))
    rec = build_record(files=files, scope_declared=scope.as_list() if scope else None,
                      mode=mode, issue=issue, attempt=attempt, commit=commit_sha,
                      compared=compared, started=started, finished=_now(), version=VERSION,
                      skipped_reason="" if scope else "no scope declared: skipped")
    text = render_stdout(rec)
    if record_dir is not None:
        record_dir = Path(record_dir)
        record_dir.mkdir(parents=True, exist_ok=True)
        if issue is not None:
            stem = f"scope_issue-{issue}_attempt-{attempt if attempt is not None else 0}"
        elif commit_sha:
            stem = f"scope_commit-{commit_sha[:10]}"
        else:
            stem = "scope_worktree-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        (record_dir / f"{stem}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
        (record_dir / f"{stem}.md").write_text(render_markdown(rec), encoding="utf-8")
        text += f"\n(record: {record_dir / (stem + '.json')})"
    bad = rec["verdict"] in ("violation", "unverified")
    return (FAIL if bad and mode == "enforce" else PASS), rec, text


def cmd_gate(args) -> int:
    try:
        cfg = load_config(Path(args.config).expanduser() if args.config else None)
        issue_file = args.issue_file or os.environ.get("AGENT_ISSUE_FILE")
        if args.issue_file and not Path(args.issue_file).is_file():
            raise ValueError(f"issue file not found: {args.issue_file}")
        result_file = args.result_file or os.environ.get("AGENT_RESULT_FILE")
        record_dir = args.record_dir
        if record_dir is None and os.environ.get("AGENT_RUN_DIR"):
            record_dir = str(Path(os.environ["AGENT_RUN_DIR"]) / "integrity")
        rc, _, text = run_gate(
            Path(args.repo).expanduser(), cfg=cfg, issue_text=_read(issue_file),
            scope_text=args.scope, result_text=_read(result_file), mode=args.mode,
            commit=args.commit,
            issue=_int_or_none(args.issue if args.issue is not None
                               else os.environ.get("AGENT_ISSUE_NUMBER")),
            attempt=_int_or_none(args.attempt if args.attempt is not None
                                 else os.environ.get("AGENT_ATTEMPT")),
            record_dir=Path(record_dir) if record_dir else None)
    except (GitError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"SCOPE GATE: MISCONFIGURED - {exc}")
        return MISCONFIGURED
    print(text)
    return rc


# ---------------------------------------------------------------------------
# index / lookup
# ---------------------------------------------------------------------------

def index_repo(repo: Path, paths: Optional[list[str]] = None) -> list[dict]:
    repo = ensure_repo(repo)
    raw = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    out = []
    for p in sorted({norm_path(t) for t in raw.decode("utf-8", "replace").split("\0") if t}):
        if paths and not any(glob_match(g, p) for g in paths):
            continue
        adapter = adapter_for(p)
        if adapter is FALLBACK:
            continue
        f = repo / p
        try:
            if not f.is_file() or f.stat().st_size > MAX_INDEX_BYTES:
                continue
            data = f.read_bytes()
        except OSError:
            continue
        parsed = adapter.parse(data, p)
        if parsed.error:
            out.append({"id": f"{p}::<file>", "path": p, "qualname": "<file>", "kind": "error",
                        "lines": [0, 0], "tags": [], "description": parsed.error,
                        "fingerprint": ""})
            continue
        for c in parsed.components:
            out.append({"id": f"{p}::{c.qualname}", "path": p, "qualname": c.qualname,
                        "kind": c.kind, "lines": list(c.span), "tags": c.tags,
                        "description": c.description, "fingerprint": c.fingerprint})
    return out


def _line(e: dict) -> str:
    tags = f"[{', '.join(e['tags'])}]" if e["tags"] else "[]"
    return f"{e['id']}  {tags}  L{e['lines'][0]}-{e['lines'][1]}  {e['description']}".rstrip()


def cmd_index(args) -> int:
    try:
        idx = index_repo(Path(args.repo).expanduser(), args.paths)
    except GitError as exc:
        print(f"INDEX: MISCONFIGURED - {exc}")
        return MISCONFIGURED
    if args.out:
        Path(args.out).write_text(json.dumps({"schema_version": 1, "tool_version": VERSION,
                                              "generated": _now(), "components": idx},
                                             indent=2), encoding="utf-8")
        print(f"indexed {len(idx)} component(s) -> {args.out}")
    else:
        for e in idx:
            print(f"{_line(e)}  ({e['kind']})")
    return PASS


def lookup(idx: list[dict], tags=None, any_tag=False, name=None, path=None) -> list[dict]:
    tags = [t.lower() for t in (tags or [])]
    res = []
    for e in idx:
        if tags:
            have = set(e["tags"])
            if (any_tag and not have.intersection(tags)) or (not any_tag and not have.issuperset(tags)):
                continue
        if name and name.lower() not in e["qualname"].lower():
            continue
        if path and not glob_match(path, e["path"]):
            continue
        res.append(e)
    return res


def cmd_lookup(args) -> int:
    try:
        idx = index_repo(Path(args.repo).expanduser())
    except GitError as exc:
        print(f"LOOKUP: MISCONFIGURED - {exc}")
        return MISCONFIGURED
    res = lookup(idx, args.tag, args.any, args.name, args.path)
    for e in res:
        print(_line(e))
    if not res:
        print("(no matching components)")
    return PASS


# ---------------------------------------------------------------------------
# bench: deterministic detection benchmark (no LLM)
# ---------------------------------------------------------------------------

PROBE = "_integrity_probe = 0"
PROBE_IMPORT = "import _integrity_probe_mod"


def _def_node(tree: ast.Module, span: tuple[int, int]):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            if start == span[0] and node.end_lineno == span[1]:
                return node
    return None


def mutate_body(text: str, comp) -> Optional[str]:
    """Insert PROBE as the first body statement after any docstring."""
    lines = text.split("\n")
    tree = ast.parse(text)
    node = _def_node(tree, comp.span)
    if node is None:
        return None
    body = node.body
    has_doc = (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
               and isinstance(body[0].value.value, str))
    idx = 1 if has_doc else 0
    if idx < len(body):
        st = body[idx]
        line = min([d.lineno for d in getattr(st, "decorator_list", [])] + [st.lineno])
        at = line - 1
        # keep a header comment block (e.g. `# @tags:`) attached to the statement below it
        ind = lines[line - 1][:len(lines[line - 1]) - len(lines[line - 1].lstrip())]
        while at > node.lineno and lines[at - 1].startswith(ind + "#"):
            at -= 1
    else:
        st = body[0]
        line = st.lineno
        at = st.end_lineno
    if line == node.lineno:
        return None
    indent = lines[line - 1][:len(lines[line - 1]) - len(lines[line - 1].lstrip())]
    if len(indent) != st.col_offset:
        return None
    new = "\n".join(lines[:at] + [indent + PROBE] + lines[at:])
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return new


def mutate_whitespace(text: str, comp, rng: random.Random) -> Optional[str]:
    """Insert one space before an operator inside the component's own lines (AST unchanged)."""
    own = {ln for ln, _ in comp.own_lines}
    cands = []
    depth = 0
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            tname = tokenize.tok_name.get(tok.type, "")
            if tname in ("FSTRING_START", "TSTRING_START"):
                depth += 1
            elif tname in ("FSTRING_END", "TSTRING_END"):
                depth -= 1
            elif tok.type == tokenize.OP and depth == 0 and tok.start[0] in own:
                cands.append(tok.start)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    lines = text.split("\n")
    rng.shuffle(cands)
    base = ast.dump(ast.parse(text))
    for ln, col in cands[:10]:
        line = lines[ln - 1]
        if col <= len(line) - len(line.lstrip()):
            continue
        new_lines = list(lines)
        new_lines[ln - 1] = line[:col] + " " + line[col:]
        new = "\n".join(new_lines)
        try:
            if ast.dump(ast.parse(new)) == base:
                return new
        except SyntaxError:
            continue
    return None


def mutate_import(text: str) -> Optional[str]:
    lines = text.split("\n")
    tree = ast.parse(text)
    at = 0
    imps = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if imps:
        at = imps[-1].end_lineno
    elif tree.body and isinstance(tree.body[0], ast.Expr) and \
            isinstance(getattr(tree.body[0], "value", None), ast.Constant):
        at = tree.body[0].end_lineno
    new = "\n".join(lines[:at] + [PROBE_IMPORT] + lines[at:])
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return new


def _ancestors(comp, by_q: dict) -> list[str]:
    out, p = [], comp.parent
    while p:
        out.append(p)
        p = by_q[p].parent if p in by_q else None
    return out


def run_bench(repo: Path, samples: int = 200, seed: int = 1, quiet: bool = False) -> dict:
    repo = ensure_repo(repo)
    rng = random.Random(seed)
    tmp = Path(tempfile.mkdtemp(prefix="integrity_bench_"))
    wt = tmp / "wt"
    cfg = load_config(None)
    adapter = PythonAdapter()
    files: dict = {}
    misses: list = []
    stats = {"samples": 0, "skipped_mutations": 0, "tp": 0, "fp": 0, "fn": 0, "tn": 0,
             "localised": 0, "spurious_findings": 0, "ws_total": 0, "ws_flagged": 0,
             "import_total": 0, "import_ok": 0, "in_scope_total": 0, "in_scope_fp": 0,
             "times": []}
    try:
        git(repo, "worktree", "add", "--detach", str(wt), "HEAD")
        for p in git_text(wt, "ls-files").splitlines():
            if not p.endswith(".py") or any(glob_match(gl, p) for gl in cfg["allow_globs"]):
                continue                     # test files are allowed wholesale: no signal
            data = (wt / p).read_bytes()
            if len(data) > MAX_INDEX_BYTES:
                continue
            parsed = adapter.parse(data, p)
            if parsed.error or not parsed.components:
                continue
            files[p] = (data, parsed)
        if not files:
            raise GitError("no parsable Python files with components at HEAD")
        paths = sorted(files)
        attempts = 0
        while stats["samples"] < samples and attempts < samples * 5:
            attempts += 1
            p = rng.choice(paths)
            data, parsed = files[p]
            nl = "\r\n" if b"\r\n" in data else "\n"
            text = data.decode("utf-8-sig", "replace").replace("\r\n", "\n")
            comp = rng.choice(parsed.components)
            by_q = {c.qualname: c for c in parsed.components}
            anc = _ancestors(comp, by_q)
            r = rng.random()
            kind = "body" if r < 0.6 else "whitespace" if r < 0.8 else "import"
            if kind == "body":
                new = mutate_body(text, comp)
            elif kind == "whitespace":
                new = mutate_whitespace(text, comp, rng)
            else:
                new = mutate_import(text)
            if new is None:
                stats["skipped_mutations"] += 1
                continue
            truth_in = rng.random() < 0.5
            entries = []
            if truth_in:
                entries.append(f"{p}::{rng.choice([comp.qualname] + anc)}")
            others = [c.qualname for c in parsed.components
                      if c.qualname != comp.qualname and c.qualname not in anc]
            for q in rng.sample(others, min(len(others), rng.randint(0 if truth_in else 1, 3))):
                entries.append(f"{p}::{q}")
            for op in rng.sample(paths, min(len(paths), rng.randint(0, 2))):
                if op == p:
                    continue
                oc = files[op][1].components
                entries.append(op if rng.random() < 0.3 else f"{op}::{rng.choice(oc).qualname}")
            rng.shuffle(entries)
            (wt / p).write_bytes(new.replace("\n", nl).encode("utf-8"))
            t0 = time.perf_counter()
            cs = collect_changes(wt)
            results = classify(cs, Scope(parse_entries(", ".join(entries))), cfg)
            stats["times"].append(time.perf_counter() - t0)
            findings = [f for fr in results for f in fr.findings]
            oos = [f for f in findings if f.category == OUT_OF_SCOPE]
            stats["samples"] += 1
            if kind == "import":
                stats["import_total"] += 1
                if not oos and any(f.category == ALLOWED and f.reason == "import"
                                   for f in findings):
                    stats["import_ok"] += 1
            else:
                flagged = bool(oos)
                if truth_in:
                    stats["in_scope_total"] += 1
                    stats["fp" if flagged else "tn"] += 1
                    if flagged:
                        stats["in_scope_fp"] += 1
                else:
                    stats["tp" if flagged else "fn"] += 1
                    if not flagged and len(misses) < 10:
                        misses.append({"kind": kind, "target": f"{p}::{comp.qualname}",
                                       "scope": entries,
                                       "findings": [(f.target, f.category) for f in findings]})
                    if [f.component for f in oos] == [comp.qualname]:
                        stats["localised"] += 1
                    stats["spurious_findings"] += sum(1 for f in oos
                                                      if f.component != comp.qualname)
                if kind == "whitespace" and not truth_in:
                    stats["ws_total"] += 1
                    if any(f.whitespace_only for f in oos if f.component == comp.qualname):
                        stats["ws_flagged"] += 1
            git(wt, "checkout", "--", ".")
            git(wt, "clean", "-fdq")
    finally:
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
        git(repo, "worktree", "prune", check=False)
        shutil.rmtree(tmp, ignore_errors=True)
    t = stats.pop("times")
    tp, fp, fn = stats["tp"], stats["fp"], stats["fn"]
    stats["precision"] = round(tp / (tp + fp), 4) if tp + fp else None
    stats["recall"] = round(tp / (tp + fn), 4) if tp + fn else None
    stats["localisation_rate"] = round(stats["localised"] / tp, 4) if tp else None
    stats["in_scope_false_positive_rate"] = (round(stats["in_scope_fp"] / stats["in_scope_total"], 4)
                                             if stats["in_scope_total"] else None)
    stats["whitespace_only_detection"] = (round(stats["ws_flagged"] / stats["ws_total"], 4)
                                          if stats["ws_total"] else None)
    stats["import_allowed_rate"] = (round(stats["import_ok"] / stats["import_total"], 4)
                                    if stats["import_total"] else None)
    stats["gate_ms"] = ({"mean": round(1000 * statistics.mean(t), 1),
                         "median": round(1000 * statistics.median(t), 1),
                         "max": round(1000 * max(t), 1)} if t else None)
    stats["seed"] = seed
    stats["python_files"] = len(files)
    stats["missed_examples"] = misses
    return stats


def cmd_bench(args) -> int:
    try:
        repo = ensure_repo(Path(args.repo).expanduser())
        res = run_bench(repo, args.samples, args.seed)
    except GitError as exc:
        print(f"BENCH: MISCONFIGURED - {exc}")
        return MISCONFIGURED
    print(json.dumps(res, indent=2))
    return PASS


# ---------------------------------------------------------------------------
# self-test (offline, temp git repos)
# ---------------------------------------------------------------------------

HSV_SRC = '''"""HSV helpers."""
import os

LIMIT = 10


# @tags: vision, color
def analyse_frame(frame):
    """Analyse one frame.

    More text."""
    return frame


class HSV:
    """HSV thresholds."""

    # @tags: vision
    @staticmethod
    def threshold(x):
        """Threshold x."""
        return x > 1

    def other(self):
        return 2


def helper():
    return 3
'''

UTIL_SRC = '''def clamp(x):
    return x


def unused():
    return None
'''


def self_test() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if not cond and detail
                                                         else ""))
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        repo.mkdir()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

        def g(*a):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                  cwd=repo, check=True, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE).stdout.decode()

        def w(rel, text, crlf=False):
            f = repo / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            data = text.replace("\n", "\r\n") if crlf else text
            f.write_bytes(data.encode("utf-8"))

        def edit(rel, old, new):
            f = repo / rel
            s = f.read_bytes().decode("utf-8")
            assert old in s, (rel, old)
            f.write_bytes(s.replace(old, new, 1).encode("utf-8"))

        def reset():
            g("checkout", "--", ".")
            g("clean", "-fdq")

        g("init", "-q")
        g("config", "core.autocrlf", "false")
        w("src/hsv.py", HSV_SRC)
        w("src/util.py", UTIL_SRC)
        w("src/crlf.py", "def a():\n    return 1\n\n\ndef b():\n    return 2\n", crlf=True)
        w("README.txt", "hello\n")
        w("tests/test_hsv.py", "def test_x():\n    assert True\n")
        g("add", "-A")
        g("commit", "-qm", "init")
        cfg = load_config(None)

        def gate(scope, mode="enforce", result="", issue_text=None, **kw):
            it = issue_text if issue_text is not None else f"# #7 t\n\n**Scope:** {scope}\n"
            rc, rec, text = run_gate(repo, cfg=cfg, issue_text=it, result_text=result,
                                     mode=mode, **kw)
            return rc, rec, text

        def finds(rec, cat=None):
            return [(f["path"], f["component"], f["category"], f["reason"])
                    for fr in rec["files"] for f in fr["findings"]
                    if cat is None or f["category"] == cat]

        S1 = "`src/hsv.py::HSV.threshold`, `tests/test_hsv.py`"

        # 1/3. in-scope method edit -> pass; class not flagged
        edit("src/hsv.py", "return x > 1", "return x > 2")
        rc, rec, _ = gate(S1)
        check("in-scope method edit -> pass", rc == 0 and rec["verdict"] == "pass"
              and ("src/hsv.py", "HSV.threshold", "in_scope", "component in declared scope")
              in finds(rec), finds(rec))
        check("editing a method does not flag its class",
              not any(c == "HSV" for _, c, _, _ in finds(rec)), finds(rec))
        reset()

        # 2. out-of-scope method edit
        edit("src/hsv.py", "return 2", "return 22")
        rc, rec, text = gate(S1)
        check("out-of-scope method edit -> violation (enforce, exit 1)",
              rc == 1 and rec["verdict"] == "violation"
              and finds(rec, "out_of_scope")[0][1] == "HSV.other", finds(rec))
        check("violation output shows diff + instructions",
              "+        return 22" in text and "SCOPE_NOTES" in text and "Revert" in text, text)
        rc, rec, _ = gate(S1, mode="report")
        check("out-of-scope edit in report mode -> exit 0, still violation",
              rc == 0 and rec["verdict"] == "violation")

        # 4. class scope covers its methods
        rc, rec, _ = gate("`src/hsv.py::HSV`")
        check("Class scope covers its methods", rc == 0 and rec["verdict"] == "pass", finds(rec))
        reset()

        # 5. import addition allowed
        edit("src/hsv.py", "import os\n", "import os\nimport sys\nfrom typing import (\n    Any,\n)\n")
        edit("src/hsv.py", "return x > 1", "return x > 3")
        rc, rec, _ = gate(S1)
        check("import addition allowed", rc == 0 and ("src/hsv.py", "<imports>", "allowed",
                                                      "import") in finds(rec), finds(rec))
        c2 = dict(cfg, allow_imports=False)
        rc, rec, _ = run_gate(repo, cfg=c2, scope_text=S1, mode="enforce")
        check("allow_imports=false flags imports", rc == 1, finds(rec))
        reset()

        # 6. new test file / new non-test file
        w("tests/test_new.py", "def test_y():\n    pass\n")
        w("src/brand_new.py", "def z():\n    pass\n")
        rc, rec, _ = gate(S1)
        fs = finds(rec)
        check("new test file allowed (allow_glob)", ("tests/test_new.py", "<file>", "allowed",
                                                     "allow_glob") in fs, fs)
        check("new non-test file allowed (new_file)", ("src/brand_new.py", "<file>", "allowed",
                                                       "new_file") in fs and rc == 0, fs)
        rc, rec, _ = run_gate(repo, cfg=dict(cfg, allow_new_files=False), scope_text=S1,
                              mode="enforce")
        check("allow_new_files=false flags new file", rc == 1)
        reset()

        # 7. new helper in scoped file -> allowed(new_component); in unscoped file -> out
        edit("src/hsv.py", "def helper():", "def new_helper(v):\n    return v\n\n\ndef helper():")
        edit("src/hsv.py", "return x > 1", "return new_helper(x) > 1")
        rc, rec, _ = gate(S1)
        check("new helper in scoped file -> allowed(new_component)",
              rc == 0 and ("src/hsv.py", "new_helper", "allowed", "new_component") in finds(rec),
              finds(rec))
        edit("src/util.py", "def unused():", "def extra():\n    return 1\n\n\ndef unused():")
        rc, rec, _ = gate(S1)
        check("new function in file without scoped components -> out_of_scope",
              rc == 1 and finds(rec, "out_of_scope")[0][:2] == ("src/util.py", "extra"),
              finds(rec))
        reset()

        # 8. removed out-of-scope function
        edit("src/util.py", "\n\ndef unused():\n    return None\n", "")
        rc, rec, _ = gate(S1)
        f = [x for fr in rec["files"] for x in fr["findings"]]
        check("removed out-of-scope function flagged",
              rc == 1 and f and f[0]["component"] == "unused" and f[0]["change"] == "removed", f)
        reset()

        # 9. whitespace-only change
        edit("src/hsv.py", "return 2", "return  2")
        rc, rec, _ = gate(S1)
        f = [x for fr in rec["files"] for x in fr["findings"]]
        check("whitespace-only change flagged with whitespace_only",
              rc == 1 and f[0]["whitespace_only"] and f[0]["category"] == "out_of_scope"
              and rec["counts"]["whitespace_only"] == 1, f)
        reset()

        # 10. syntax error -> unverified
        edit("src/hsv.py", "return x > 1", "return x >")
        rc, rec, text = gate(S1)
        check("syntax error -> unverified (enforce exit 1)",
              rc == 1 and rec["verdict"] == "unverified" and "SyntaxError" in text, text)
        rc, rec, _ = gate(S1, mode="report")
        check("syntax error in report mode -> exit 0", rc == 0 and rec["verdict"] == "unverified")
        reset()

        # 11. non-.py file outside scope; module-level change
        edit("README.txt", "hello", "hello world")
        edit("src/hsv.py", '"""HSV helpers."""', '"""HSV helpers!"""\nprint(1)')
        rc, rec, _ = gate(S1)
        fs = finds(rec, "out_of_scope")
        check("non-.py file outside scope flagged at <file>",
              ("README.txt", "<file>", "out_of_scope", "file not in declared scope") in fs, fs)
        check("module-level code change flagged at <module>",
              any(p == "src/hsv.py" and c == "<module>" for p, c, _, _ in fs), fs)

        # 12. glob scope
        rc, rec, _ = gate("`src/**`, README.txt")
        check("glob scope covers files", rc == 0 and rec["verdict"] == "pass", finds(rec))
        reset()

        # 13. no scope line
        edit("src/util.py", "return x", "return -x")
        rc, rec, text = gate(None, issue_text="# #7 t\n\nNo scope here.\n")
        check("no Scope line -> skipped, exit 0",
              rc == 0 and rec["verdict"] == "skipped" and "no scope declared: skipped" in text)

        # 14. SCOPE_NOTES
        res = ("===AGENT_ISSUE_RESULT_BEGIN===\nSTATUS: done\nSCOPE_NOTES:\n"
               "- `src/util.py::clamp`: needed to accept float input\n"
               "NEXT: nothing\n===AGENT_ISSUE_RESULT_END===\n")
        rc, rec, _ = gate(S1, result=res)
        f = [x for fr in rec["files"] for x in fr["findings"]]
        check("SCOPE_NOTES marks justified but verdict unchanged",
              rc == 1 and rec["verdict"] == "violation" and f[0]["justified"]
              and "float" in f[0]["justification"] and rec["counts"]["justified"] == 1, f)
        check("scope_notes parser", parse_scope_notes(res) == {
            "src/util.py::clamp": "needed to accept float input"}, parse_scope_notes(res))
        reset()

        # 15. tags + description
        p = PythonAdapter().parse(HSV_SRC.encode())
        by = p.by_qualname()
        check("tags + description parsed",
              by["analyse_frame"].tags == ["vision", "color"]
              and by["analyse_frame"].description == "Analyse one frame."
              and by["HSV.threshold"].tags == ["vision"] and by["HSV.threshold"].kind == "method"
              and by["HSV.threshold"].span[0] == 19 and by["HSV"].description == "HSV thresholds.",
              {k: (v.tags, v.description, v.span) for k, v in by.items()})
        check("// @tags helper", parse_tags("  // @tags: Excel, Sheets") == ["excel", "sheets"])

        # 16. CRLF
        (repo / "src/crlf.py").write_bytes(b"def a():\r\n    return 10\r\n\r\n\r\ndef b():\r\n"
                                           b"    return 2\r\n")
        rc, rec, _ = gate("`src/crlf.py::a`")
        check("CRLF file: in-scope edit passes", rc == 0 and ("src/crlf.py", "a", "in_scope",
                                                               "component in declared scope")
              in finds(rec), finds(rec))
        (repo / "src/crlf.py").write_bytes(b"def a():\n    return 1\n\n\ndef b():\n    return 2\n")
        rc, rec, _ = gate("`src/crlf.py::a`")
        check("CRLF->LF only conversion is not a component change",
              rc == 0 and finds(rec) == [], finds(rec))
        reset()

        # 17. records via CLI + env vars
        rundir = Path(d) / "run"
        (Path(d) / "issue.md").write_text(f"# #12 thing\n\n**Scope:** {S1}\n", encoding="utf-8")
        (Path(d) / "result.txt").write_text("SCOPE_NOTES:\n- src/hsv.py::HSV.other: because\n",
                                            encoding="utf-8")
        (Path(d) / "proj.json").write_text(json.dumps({"scope": {"// note": "x",
                                                                 "mode": "enforce"}}),
                                           encoding="utf-8")
        edit("src/hsv.py", "return 2", "return 3")
        cenv = dict(os.environ, AGENT_ISSUE_FILE=str(Path(d) / "issue.md"),
                    AGENT_RESULT_FILE=str(Path(d) / "result.txt"), AGENT_RUN_DIR=str(rundir),
                    AGENT_ISSUE_NUMBER="12", AGENT_ATTEMPT="2")
        cp = subprocess.run([sys.executable, str(Path(__file__).resolve()), "gate", "--config",
                             str(Path(d) / "proj.json")], cwd=repo, env=cenv,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        jf = rundir / "integrity" / "scope_issue-12_attempt-2.json"
        mf = rundir / "integrity" / "scope_issue-12_attempt-2.md"
        recj = json.loads(jf.read_text(encoding="utf-8")) if jf.is_file() else {}
        keys = {"schema_version", "issue", "attempt", "mode", "verdict", "scope_declared",
                "files", "counts", "lines_changed", "started", "finished", "tool_version"}
        check("CLI gate via env vars: exit 1 in enforce", cp.returncode == 1, cp.stdout + cp.stderr)
        check("records JSON + MD written with schema keys",
              jf.is_file() and mf.is_file() and keys <= set(recj) and recj["schema_version"] == 1
              and set(recj["counts"]) == {"in_scope", "allowed", "out_of_scope", "unverified",
                                          "justified", "whitespace_only"}
              and set(recj["lines_changed"]) == {"in_scope", "out_of_scope"}
              and recj["issue"] == 12 and recj["attempt"] == 2 and recj["counts"]["justified"] == 1,
              str(recj)[:300])
        md = mf.read_text(encoding="utf-8") if mf.is_file() else ""
        check("markdown report has table, tags/descriptions, justification",
              "| File | Component" in md and "Out-of-scope changes" in md and "because" in md
              and "Threshold x." not in md or "HSV.other" in md, md[:300])
        reset()

        # 18. --commit mode
        edit("src/util.py", "return x", "return abs(x)")
        edit("src/hsv.py", "return x > 1", "return x >= 1")
        g("commit", "-qam", "agent commit")
        sha = g("rev-parse", "HEAD").strip()
        w("src/util.py", "garbage (\n")          # dirty working tree must be ignored
        rc, rec, _ = run_gate(repo, cfg=cfg, scope_text="src/hsv.py::HSV.threshold",
                              commit=sha, mode="enforce", record_dir=Path(d) / "crec")
        check("--commit: compares <sha>^ vs <sha>, ignores working tree",
              rc == 1 and rec["commit"] == sha and finds(rec, "out_of_scope")
              == [("src/util.py", "clamp", "out_of_scope", "component not in declared scope")]
              and finds(rec, "in_scope")[0][1] == "HSV.threshold", finds(rec))
        check("--commit: record named by short sha",
              (Path(d) / "crec" / f"scope_commit-{sha[:10]}.json").is_file())
        cp = subprocess.run([sys.executable, str(Path(__file__).resolve()), "gate", "--repo",
                             str(repo), "--commit", sha[:8], "--scope",
                             "src/hsv.py::HSV, src/util.py", "--mode", "enforce"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={k: v for k, v in os.environ.items()
                                 if not k.startswith("AGENT_")})
        check("--commit via CLI with --scope -> pass",
              cp.returncode == 0 and "PASS" in cp.stdout, cp.stdout)
        check("--commit leaves working tree untouched",
              (repo / "src/util.py").read_text() == "garbage (\n")
        reset()

        # 19. deleted + renamed files; misconfigured
        g("mv", "src/util.py", "src/utils2.py")
        (repo / "README.txt").unlink()
        rc, rec, _ = gate(S1)
        ch = {fr["path"]: fr["change"] for fr in rec["files"]}
        check("deleted and renamed files flagged",
              rc == 1 and ch.get("README.txt") == "removed" and ch.get("src/utils2.py") == "renamed"
              and len(finds(rec, "out_of_scope")) == 2, rec["files"])
        g("reset", "-q", "--hard")
        cp = subprocess.run([sys.executable, str(Path(__file__).resolve()), "gate", "--repo",
                             d, "--scope", "x.py"], stdout=subprocess.PIPE, text=True)
        check("not a git repo -> exit 2", cp.returncode == 2, cp.stdout)
        (Path(d) / "bad.json").write_text('{"scope": {"mode": "strict"}}', encoding="utf-8")
        cp = subprocess.run([sys.executable, str(Path(__file__).resolve()), "gate", "--repo",
                             str(repo), "--config", str(Path(d) / "bad.json")],
                            stdout=subprocess.PIPE, text=True)
        check("bad config -> exit 2", cp.returncode == 2, cp.stdout)

        # 20. scope parsing variants
        sc = parse_scope("x\n```\n**Scope:** fake.py\n```\n- **Scope:** a.py::f,`b/**` , c.py\n")
        check("scope line parsing (fences skipped, backticks optional)",
              sc is not None and sc.as_list() == ["a.py::f", "b/**", "c.py"]
              and sc.covers_file("b/x/y.py") and not sc.covers_file("a.py")
              and sc.covers_component("a.py", "f.inner") and not sc.covers_component("a.py", "g"))

        # 21. index / lookup
        idx = index_repo(repo)
        hits = lookup(idx, ["vision"])
        check("index + lookup by tag", {e["id"] for e in hits} ==
              {"src/hsv.py::analyse_frame", "src/hsv.py::HSV.threshold"}, [e["id"] for e in hits])
        check("lookup all-vs-any tags",
              len(lookup(idx, ["vision", "color"])) == 1
              and len(lookup(idx, ["vision", "color"], any_tag=True)) == 2)
        check("lookup by name + path", [e["id"] for e in lookup(idx, name="clamp", path="src/**")]
              == ["src/util.py::clamp"])

        # 22. bench smoke test (worktree is removed afterwards)
        before = g("status", "--porcelain")
        b = run_bench(repo, samples=20, seed=3)
        check("bench smoke: perfect detection on toy repo",
              b["samples"] == 20 and b["fn"] == 0 and b["fp"] == 0, b)
        check("bench leaves repo untouched + worktree removed",
              g("status", "--porcelain") == before and len(g("worktree", "list").splitlines()) == 1)

        # 23. module constants, import blocks, doc/test allowances, protected paths
        reset()
        w("src/consts.py", '"""C."""\nfrom typing import TYPE_CHECKING\n\n__all__ = ["A"]\n'
          'A = 1\nB: int = 2\nCOLS = (\n    "x",\n    "y",\n)\na, b = 1, 2\n\n\n'
          'def f():\n    return A\n')
        w("docs/GUIDE.md", "guide\n")
        w("CLAUDE.md", "rules\n")
        w("app/test/helpers.dart", "void h() {}\n")
        g("add", "-A")
        g("commit", "-qm", "consts")
        edit("src/consts.py", '    "y",\n', '    "y",\n    "z",\n')
        rc, rec, _ = gate("`src/consts.py::f`")
        f = [x for fr in rec["files"] for x in fr["findings"]]
        check("editing one module constant flags only that constant",
              [(x["component"], x["kind"], x["category"]) for x in f]
              == [("COLS", "constant", "out_of_scope")], f)
        rc, rec, _ = gate("`src/consts.py::COLS`")
        check("constant in scope via path::NAME", rc == 0 and rec["verdict"] == "pass", finds(rec))
        reset()
        edit("src/consts.py", "TYPE_CHECKING\n", "TYPE_CHECKING\nif TYPE_CHECKING:\n"
             "    from os import PathLike\ntry:\n    import json\nexcept ImportError:\n"
             "    pass\n")
        rc, rec, _ = gate("`src/consts.py::f`")
        check("TYPE_CHECKING / try import blocks -> <imports> allowed",
              rc == 0 and finds(rec) == [("src/consts.py", "<imports>", "allowed", "import")],
              finds(rec))
        reset()
        edit("docs/GUIDE.md", "guide", "guide v2")
        edit("app/test/helpers.dart", "{}", "{ }")
        edit("CLAUDE.md", "rules", "rules, defer #12")
        w(".mcp.json", "{}\n")
        rc, rec, _ = gate("`src/consts.py::f`")
        fs = finds(rec)
        check(".md doc change allowed (allow_glob)",
              ("docs/GUIDE.md", "<file>", "allowed", "allow_glob") in fs, fs)
        check("Dart test/ file allowed (allow_glob)",
              ("app/test/helpers.dart", "<file>", "allowed", "allow_glob") in fs, fs)
        check("CLAUDE.md flagged as protected despite **/*.md allowance",
              ("CLAUDE.md", "<file>", "out_of_scope",
               "protected path (agent/tool configuration or backup)") in fs and rc == 1, fs)
        check(".mcp.json new file flagged despite allow_new_files",
              (".mcp.json", "<file>", "out_of_scope",
               "protected path (agent/tool configuration or backup)") in fs, fs)
        rc, rec, _ = gate("`src/consts.py::f`, `CLAUDE.md`, `.mcp.json`")
        check("explicitly scoped protected files -> in_scope, pass",
              rc == 0 and ("CLAUDE.md", "<file>", "in_scope", "file in declared scope")
              in finds(rec), finds(rec))
        reset()

    import refs                                    # dangling-reference check (refs.py)
    refs.self_test(check, __file__)
    import record                                  # decision record check (record.py)
    record.self_test(check, __file__)
    print("\n" + ("ALL INTEGRITY SELF-TESTS PASSED" if ok else "SOME INTEGRITY SELF-TESTS FAILED"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_refs(args) -> int:
    import refs
    try:
        cfg = refs.load_config(Path(args.config).expanduser() if args.config else None)
        record_dir = args.record_dir
        if record_dir is None and os.environ.get("AGENT_RUN_DIR"):
            record_dir = str(Path(os.environ["AGENT_RUN_DIR"]) / "integrity")
        rc, _, text = refs.check_refs(
            Path(args.repo).expanduser(), cfg=cfg, mode=args.mode, commit=args.commit,
            issue=_int_or_none(args.issue if args.issue is not None
                               else os.environ.get("AGENT_ISSUE_NUMBER")),
            attempt=_int_or_none(args.attempt if args.attempt is not None
                                 else os.environ.get("AGENT_ATTEMPT")),
            record_dir=Path(record_dir) if record_dir else None, version=VERSION)
    except (refs.RefsError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"REFS CHECK: MISCONFIGURED - {exc}")
        return MISCONFIGURED
    print(text)
    return rc


def cmd_record(args) -> int:
    import record
    import refs
    try:
        cfg_path = Path(args.config).expanduser() if args.config else None
        cfg = record.load_config(cfg_path)
        issue_file = args.issue_file or os.environ.get("AGENT_ISSUE_FILE")
        if args.issue_file and not Path(args.issue_file).is_file():
            raise ValueError(f"issue file not found: {args.issue_file}")
        result_file = args.result_file or os.environ.get("AGENT_RESULT_FILE")
        record_dir = args.record_dir
        if record_dir is None and os.environ.get("AGENT_RUN_DIR"):
            record_dir = str(Path(os.environ["AGENT_RUN_DIR"]) / "integrity")
        rc, _, text = record.check_record(
            Path(args.repo).expanduser(), cfg=cfg, mode=args.mode, commit=args.commit,
            issue=_int_or_none(args.issue if args.issue is not None
                               else os.environ.get("AGENT_ISSUE_NUMBER")),
            attempt=_int_or_none(args.attempt if args.attempt is not None
                                 else os.environ.get("AGENT_ATTEMPT")),
            issue_text=_read(issue_file), result_text=_read(result_file),
            record_dir=Path(record_dir) if record_dir else None, write=not args.no_write,
            config_path=cfg_path, version=VERSION)
    except (GitError, refs.RefsError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"DECISION RECORD: MISCONFIGURED - {exc}")
        return MISCONFIGURED
    print(text)
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="integrity", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    g = sub.add_parser("gate", help="scope gate (exit 0 pass/skipped/report, 1 block, 2 misconfig)")
    g.add_argument("--repo", default=".", help="target repo (default: cwd, as the runner sets)")
    g.add_argument("--config", help='project config JSON; its "scope" section is used')
    g.add_argument("--mode", choices=["report", "enforce"], help="override config mode")
    g.add_argument("--scope", help='scope entries directly, e.g. "a.py::f, b/**" (overrides issue)')
    g.add_argument("--commit", help="check <sha>^..<sha> instead of HEAD..working tree")
    g.add_argument("--issue-file", help="default: $AGENT_ISSUE_FILE")
    g.add_argument("--result-file", help="agent result (SCOPE_NOTES); default $AGENT_RESULT_FILE")
    g.add_argument("--issue", help="issue number; default $AGENT_ISSUE_NUMBER or issue header")
    g.add_argument("--attempt", help="attempt number; default $AGENT_ATTEMPT")
    g.add_argument("--record-dir", help="default $AGENT_RUN_DIR/integrity (else no records)")

    i = sub.add_parser("index", help="list components of a repo")
    i.add_argument("--repo", default=".")
    i.add_argument("--out", help="write JSON here instead of printing")
    i.add_argument("--paths", nargs="*", help="only paths matching these globs")

    lk = sub.add_parser("lookup", help="find components by tag/name/path")
    lk.add_argument("--repo", default=".")
    lk.add_argument("--tag", action="append", default=[], help="repeatable; ALL must match")
    lk.add_argument("--any", action="store_true", help="match ANY tag instead of ALL")
    lk.add_argument("--name", help="case-insensitive qualname substring")
    lk.add_argument("--path", help="path glob")

    b = sub.add_parser("bench", help="deterministic detection benchmark (no LLM)")
    b.add_argument("--repo", default=".")
    b.add_argument("--samples", type=int, default=200)
    b.add_argument("--seed", type=int, default=1)

    r = sub.add_parser("refs", help="removed-but-still-referenced symbols (exit 1 only in enforce)")
    r.add_argument("--repo", default=".", help="target repo (default: cwd)")
    r.add_argument("--config", help='project config JSON; its "refs" section is used')
    r.add_argument("--mode", choices=["report", "enforce"], help="override config mode")
    r.add_argument("--commit", help="check <sha>^..<sha> instead of HEAD..working tree")
    r.add_argument("--issue", help="issue number; default $AGENT_ISSUE_NUMBER")
    r.add_argument("--attempt", help="attempt number; default $AGENT_ATTEMPT")
    r.add_argument("--record-dir", help="default $AGENT_RUN_DIR/integrity (else no records)")

    rc_ = sub.add_parser("record", help="decision record check + verified-facts block "
                                        "(exit 1 only in enforce)")
    rc_.add_argument("--repo", default=".", help="target repo (default: cwd)")
    rc_.add_argument("--config", help='project config JSON; its "record" section is used')
    rc_.add_argument("--mode", choices=["report", "enforce"], help="override config mode")
    rc_.add_argument("--issue", help="issue number; default $AGENT_ISSUE_NUMBER or issue header")
    rc_.add_argument("--attempt", help="attempt number; default $AGENT_ATTEMPT")
    rc_.add_argument("--issue-file", help="default: $AGENT_ISSUE_FILE (for the Scope line)")
    rc_.add_argument("--result-file", help="agent result (STATUS, SCOPE_NOTES); "
                                           "default $AGENT_RESULT_FILE")
    rc_.add_argument("--record-dir", help="default $AGENT_RUN_DIR/integrity (else no records)")
    rc_.add_argument("--commit", help="check <sha>^..<sha>; never writes, prints the facts")
    rc_.add_argument("--no-write", action="store_true", help="do not write the facts block")

    sub.add_parser("self-test", help="offline self-tests in temp git repos")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # never crash printing a diff on cp1252
        except (AttributeError, ValueError):
            pass
    if args.cmd is None:
        ap.print_help()
        return MISCONFIGURED
    return {"gate": cmd_gate, "index": cmd_index, "lookup": cmd_lookup, "bench": cmd_bench, "refs": cmd_refs,
            "record": cmd_record, "self-test": lambda _a: self_test()}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())

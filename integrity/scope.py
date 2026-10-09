"""
scope.py — scope declarations, change collection via git, and classification.

    parse_scope(issue_text)      -> Scope | None   (None = no **Scope:** line)
    parse_scope_notes(result)    -> {target: reason}
    collect_changes(repo, commit)-> ChangeSet      (HEAD vs working tree, or <sha>^ vs <sha>)
    classify(changeset, scope, cfg, notes) -> [FileResult]

Talks to git through subprocess only; knows nothing about the runner.
"""
from __future__ import annotations

import difflib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from components import (FILE, IMPORTS, MODULE, PSEUDO, Component, Parsed, adapter_for,
                        base_qualname, decode_text)

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

DEFAULT_CONFIG = {
    "mode": "report",
    "allow_globs": [
        # tests (Python)
        "tests/**", "**/test_*.py", "**/*_test.py", "**/conftest.py",
        # tests (other languages)
        "**/test/**", "**/__tests__/**", "**/*.test.*", "**/*.spec.*", "**/*_test.dart",
        "**/*Test.php", "**/*_test.go",
        # docs
        "**/*.md", "docs/**", "**/.env.example", "**/*.env.example",
    ],
    # agent/tool configuration and backup files: always out_of_scope unless a scope
    # entry names them; beats allow_globs and allow_new_files
    "protect_globs": ["**/AGENTS.md", "**/CLAUDE.md", "**/.claude/**", "**/.mcp.json",
                      "**/.codex/**", "**/*.bak", "**/*.bak-*", "**/*.orig"],
    "allow_imports": True,
    "allow_new_files": True,
    "allow_new_components": True,
}

IN_SCOPE, ALLOWED, OUT_OF_SCOPE, UNVERIFIED = "in_scope", "allowed", "out_of_scope", "unverified"
PROTECTED_REASON = "protected path (agent/tool configuration or backup)"
DIFF_LINES_PER_FINDING = 60


class GitError(Exception):
    pass


# ---------------------------------------------------------------------------
# globs (posix, `**` crosses directories, `*`/`?` do not)
# ---------------------------------------------------------------------------

_GLOB_CACHE: dict[str, re.Pattern] = {}


def norm_path(p: str) -> str:
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def glob_to_regex(pattern: str) -> re.Pattern:
    if pattern in _GLOB_CACHE:
        return _GLOB_CACHE[pattern]
    pat = norm_path(pattern)
    if pat.endswith("/"):
        pat += "**"
    i, out = 0, []
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pat.startswith("**", i):
            out.append(".*"); i += 2
        elif pat[i] == "*":
            out.append("[^/]*"); i += 1
        elif pat[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(pat[i])); i += 1
    rx = re.compile("".join(out) + "(?:/.*)?$")   # a bare directory name covers its contents
    _GLOB_CACHE[pattern] = rx
    return rx


def glob_match(pattern: str, path: str) -> bool:
    return bool(glob_to_regex(pattern).match(norm_path(path)))


# ---------------------------------------------------------------------------
# scope + SCOPE_NOTES parsing
# ---------------------------------------------------------------------------

SCOPE_LINE_RE = re.compile(r"^\s*(?:[-*>]\s*)?\*\*\s*scope\s*:?\s*\*\*\s*:?\s*(.*)$", re.IGNORECASE)
NONE_WORDS = {"none", "(none)", "-", "n/a", "nothing"}


@dataclass
class ScopeEntry:
    raw: str
    path: str
    qualname: Optional[str] = None

    @property
    def is_glob(self) -> bool:
        return any(ch in self.path for ch in "*?")


@dataclass
class Scope:
    entries: list[ScopeEntry]

    def covers_file(self, path: str) -> bool:
        return any(e.qualname is None and glob_match(e.path, path) for e in self.entries)

    def has_components(self, path: str) -> bool:
        return any(e.qualname and glob_match(e.path, path) for e in self.entries)

    def covers_component(self, path: str, qualname: str) -> bool:
        if qualname in PSEUDO:
            return self.covers_file(path)
        q = base_qualname(qualname)
        for e in self.entries:
            if e.qualname and glob_match(e.path, path):
                eq = base_qualname(e.qualname)
                if q == eq or q.startswith(eq + "."):
                    return True
        return self.covers_file(path)

    def as_list(self) -> list[str]:
        return [e.raw for e in self.entries]


def parse_entries(text: str) -> list[ScopeEntry]:
    out = []
    for part in text.split(","):
        raw = part.strip().strip("`").strip()
        if not raw or raw.lower() in NONE_WORDS:
            continue
        path, sep, q = raw.partition("::")
        out.append(ScopeEntry(raw, norm_path(path), q.strip() or None if sep else None))
    return out


def parse_scope(issue_text: str) -> Optional[Scope]:
    """First `**Scope:** ...` line outside fenced code blocks, or None."""
    fenced = False
    for line in issue_text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = SCOPE_LINE_RE.match(line)
        if m:
            return Scope(parse_entries(m.group(1)))
    return None


NOTES_HEAD_RE = re.compile(r"^\s*\**\s*SCOPE_NOTES\s*\**\s*:\s*\**\s*(.*)$")
NOTE_RE = re.compile(r"^\s*[-*]\s*`?([^`\s]+?)`?\s*(?::\s|:$|\s—|\s–|\s-\s)\s*(.*)$")
SECTION_RE = re.compile(r"^\s*\**[A-Z][A-Z0-9_ ]{2,}\**\s*:")


def parse_scope_notes(result_text: str) -> dict[str, str]:
    notes: dict[str, str] = {}
    active = False
    for line in (result_text or "").splitlines():
        m = NOTES_HEAD_RE.match(line)
        if m:
            active = True
            rest = m.group(1).strip()
            if rest:
                nm = NOTE_RE.match("- " + rest)
                if nm:
                    notes[norm_path(nm.group(1))] = nm.group(2).strip()
            continue
        if not active:
            continue
        if not line.strip():
            continue
        nm = NOTE_RE.match(line)
        if nm:
            notes[norm_path(nm.group(1))] = nm.group(2).strip()
        elif SECTION_RE.match(line) or not line.startswith((" ", "\t")):
            active = False
    return notes


def note_for(notes: dict[str, str], path: str, component: str) -> Optional[str]:
    for target, reason in notes.items():
        tpath, sep, tq = target.partition("::")
        if norm_path(tpath) != path:
            continue
        if not sep or not tq:
            return reason
        if component == tq or (component not in PSEUDO and (
                base_qualname(component) == base_qualname(tq)
                or base_qualname(component).startswith(base_qualname(tq) + "."))):
            return reason
    return None


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=str(repo),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: "
                       f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc


def git_text(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.decode("utf-8", "replace")


@dataclass
class Change:
    path: str
    change: str                      # added | modified | removed | renamed
    old_path: Optional[str] = None


@dataclass
class ChangeSet:
    repo: Path
    old_rev: str                     # tree-ish for the old side
    new_rev: Optional[str]           # None = working tree
    changes: list[Change]
    label: str                       # "HEAD..working tree" / "abc1234^..abc1234"

    def old_bytes(self, path: str) -> Optional[bytes]:
        if self.old_rev == EMPTY_TREE:
            return None
        p = git(self.repo, "show", f"{self.old_rev}:{path}", check=False)
        return p.stdout if p.returncode == 0 else None

    def new_bytes(self, path: str) -> Optional[bytes]:
        if self.new_rev is None:
            f = self.repo / path
            return f.read_bytes() if f.is_file() else None
        p = git(self.repo, "show", f"{self.new_rev}:{path}", check=False)
        return p.stdout if p.returncode == 0 else None


_STATUS = {"A": "added", "C": "added", "M": "modified", "T": "modified", "D": "removed",
           "R": "renamed"}


def _parse_name_status(raw: bytes) -> list[Change]:
    toks = raw.decode("utf-8", "replace").split("\0")
    out, i = [], 0
    while i < len(toks):
        st = toks[i].strip()
        if not st:
            i += 1
            continue
        kind = _STATUS.get(st[0], "modified")
        if st[0] in "RC":
            old, new = toks[i + 1], toks[i + 2]
            i += 3
            out.append(Change(norm_path(new), kind, norm_path(old) if kind == "renamed" else None))
        else:
            out.append(Change(norm_path(toks[i + 1]), kind))
            i += 2
    return out


def ensure_repo(repo: Path) -> Path:
    if not repo.is_dir():
        raise GitError(f"repo {repo} is not a directory")
    p = git(repo, "rev-parse", "--show-toplevel", check=False)
    if p.returncode != 0:
        raise GitError(f"{repo} is not a git repository")
    return Path(p.stdout.decode("utf-8", "replace").strip())


def collect_changes(repo: Path, commit: Optional[str] = None,
                    exclude: tuple[str, ...] = ()) -> ChangeSet:
    """Changed files: HEAD vs working tree (+ untracked), or <commit>^ vs <commit>."""
    repo = ensure_repo(repo)
    if commit:
        p = git(repo, "rev-parse", "--verify", "-q", f"{commit}^{{commit}}", check=False)
        if p.returncode != 0:
            raise GitError(f"unknown commit {commit!r}")
        new = p.stdout.decode().strip()
        pp = git(repo, "rev-parse", "--verify", "-q", f"{new}^", check=False)
        old = pp.stdout.decode().strip() if pp.returncode == 0 else EMPTY_TREE
        raw = git(repo, "diff", "--name-status", "-M", "-z", old, new).stdout
        changes = _parse_name_status(raw)
        cs = ChangeSet(repo, old, new, changes, f"{new[:10]}^..{new[:10]}")
    else:
        p = git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
        old = "HEAD" if p.returncode == 0 else EMPTY_TREE
        raw = git(repo, "diff", "--name-status", "-M", "-z", old).stdout
        changes = _parse_name_status(raw)
        known = {c.path for c in changes}
        unt = git(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout
        for t in unt.decode("utf-8", "replace").split("\0"):
            if t.strip() and norm_path(t) not in known:
                changes.append(Change(norm_path(t), "added"))
        cs = ChangeSet(repo, old, None, changes, f"{old}..working tree")
    ex = [norm_path(e).rstrip("/") + "/" for e in exclude if e]
    cs.changes = sorted((c for c in cs.changes if not any(c.path.startswith(e) for e in ex)),
                        key=lambda c: c.path)
    return cs


# ---------------------------------------------------------------------------
# diffs with real line numbers
# ---------------------------------------------------------------------------

def numbered_diff(a: list[tuple[int, str]], b: list[tuple[int, str]],
                  context: int = 2) -> tuple[list[str], int, int]:
    """Unified-style diff of two numbered line lists -> (lines, n_added, n_removed)."""
    sa = [t.rstrip() for _, t in a]
    sb = [t.rstrip() for _, t in b]
    sm = difflib.SequenceMatcher(None, sa, sb, autojunk=False)
    out: list[str] = []
    added = removed = 0
    for group in sm.get_grouped_opcodes(context):
        i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
        aln = a[i1][0] if i1 < len(a) else (a[-1][0] + 1 if a else 0)
        bln = b[j1][0] if j1 < len(b) else (b[-1][0] + 1 if b else 0)
        out.append(f"@@ -{aln},{i2 - i1} +{bln},{j2 - j1} @@")
        for tag, x1, x2, y1, y2 in group:
            if tag == "equal":
                out.extend(" " + s for s in sa[x1:x2])
                continue
            if tag in ("replace", "delete"):
                out.extend("-" + s for s in sa[x1:x2]); removed += x2 - x1
            if tag in ("replace", "insert"):
                out.extend("+" + s for s in sb[y1:y2]); added += y2 - y1
    return out, added, removed


def _numbered(data: Optional[bytes]) -> Optional[list[tuple[int, str]]]:
    if data is None:
        return []
    text = decode_text(data)
    if text is None:
        return None
    if text.endswith("\n"):
        text = text[:-1]
    return list(enumerate(text.split("\n"), 1)) if text else []


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    path: str
    component: str
    change: str
    category: str
    reason: str
    whitespace_only: bool = False
    justified: bool = False
    justification: str = ""
    kind: str = ""
    lines: Optional[list[int]] = None
    tags: list[str] = field(default_factory=list)
    description: str = ""
    lines_added: int = 0
    lines_removed: int = 0
    diff: list[str] = field(default_factory=list)

    @property
    def target(self) -> str:
        return self.path if self.component == FILE else f"{self.path}::{self.component}"

    def to_dict(self, with_diff: bool) -> dict:
        d = {"path": self.path, "component": self.component, "change": self.change,
             "category": self.category, "reason": self.reason,
             "whitespace_only": self.whitespace_only, "justified": self.justified,
             "justification": self.justification, "kind": self.kind, "lines": self.lines,
             "tags": self.tags, "description": self.description,
             "lines_added": self.lines_added, "lines_removed": self.lines_removed}
        if with_diff:
            d["diff"] = self.diff[:DIFF_LINES_PER_FINDING]
            d["diff_truncated"] = len(self.diff) > DIFF_LINES_PER_FINDING
        return d


@dataclass
class FileResult:
    path: str
    change: str
    adapter: str
    old_path: Optional[str] = None
    findings: list[Finding] = field(default_factory=list)
    error: Optional[str] = None


def load_config(path: Optional[Path]) -> dict:
    """The "scope" section of a project config JSON; missing file/section -> defaults."""
    import json
    cfg = dict(DEFAULT_CONFIG)
    if path is None or not Path(path).is_file():
        return cfg
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object")
    sec = data.get("scope", {})
    if not isinstance(sec, dict):
        raise ValueError('"scope" section must be an object')
    for k, v in sec.items():
        if k.startswith("//") or k.startswith("_"):
            continue
        if k not in DEFAULT_CONFIG:
            raise ValueError(f"unknown scope config key {k!r}")
        cfg[k] = v
    if cfg["mode"] not in ("report", "enforce"):
        raise ValueError(f"scope.mode must be 'report' or 'enforce', not {cfg['mode']!r}")
    for k in ("allow_globs", "protect_globs"):
        if not isinstance(cfg[k], list):
            raise ValueError(f"scope.{k} must be a list")
    return cfg


def _file_finding(path, change, category, reason, old_b, new_b, ws_only=False) -> Finding:
    f = Finding(path, FILE, change, category, reason, whitespace_only=ws_only, kind="file")
    a, b = _numbered(old_b), _numbered(new_b)
    if a is None or b is None:
        f.diff = ["(binary file differs)"]
    else:
        f.diff, f.lines_added, f.lines_removed = numbered_diff(a, b)
    return f


def _comp_finding(path: str, q: str, change: str, category: str, reason: str,
                  o: Optional[Component], n: Optional[Component]) -> Finding:
    ref = n or o
    f = Finding(path, q, change, category, reason, kind=ref.kind,
                lines=list(ref.span) if ref.qualname not in PSEUDO else None,
                tags=list(ref.tags), description=ref.description)
    if o is not None and n is not None:
        f.whitespace_only = (o.own_fingerprint != n.own_fingerprint
                             and o.ws_fingerprint == n.ws_fingerprint)
    f.diff, f.lines_added, f.lines_removed = numbered_diff(o.own_lines if o else [],
                                                           n.own_lines if n else [])
    return f


def classify_file(ch: Change, old_b: Optional[bytes], new_b: Optional[bytes],
                  scope: Scope, cfg: dict) -> FileResult:
    path = ch.path
    adapter = adapter_for(path)
    fr = FileResult(path, ch.change, adapter.name, ch.old_path)
    add = fr.findings.append

    # 1. whole-file / glob scope
    if scope.covers_file(path):
        add(_file_finding(path, ch.change, IN_SCOPE, "file in declared scope", old_b, new_b))
        return fr
    # 1b. protected paths (agent/tool config, backups) - only an explicit scope entry lifts it
    if any(glob_match(g, path) for g in cfg.get("protect_globs", ())) or (
            ch.old_path and any(glob_match(g, ch.old_path) for g in cfg.get("protect_globs", ()))):
        add(_file_finding(path, ch.change, OUT_OF_SCOPE, PROTECTED_REASON, old_b, new_b))
        return fr
    # 2. allow_globs
    if any(glob_match(g, path) for g in cfg["allow_globs"]):
        f = _file_finding(path, ch.change, ALLOWED, "allow_glob", old_b, new_b)
        add(f)
        return fr
    # 3. new files
    if ch.change == "added":
        if cfg["allow_new_files"]:
            add(_file_finding(path, ch.change, ALLOWED, "new_file", old_b, new_b))
        else:
            add(_file_finding(path, ch.change, OUT_OF_SCOPE,
                              "new file outside scope (allow_new_files is off)", old_b, new_b))
        return fr
    # 4. deleted / renamed
    if ch.change in ("removed", "renamed"):
        what = "deleted" if ch.change == "removed" else f"renamed from {ch.old_path}"
        add(_file_finding(path, ch.change, OUT_OF_SCOPE, f"file {what} outside scope",
                          old_b, new_b))
        return fr

    # 5./6. component-level comparison
    old_p: Parsed = adapter.parse(old_b or b"", path)
    new_p: Parsed = adapter.parse(new_b or b"", path)
    if old_p.error or new_p.error:
        side = "working tree/new" if new_p.error else "HEAD/old"
        fr.error = new_p.error or old_p.error
        add(_file_finding(path, ch.change, UNVERIFIED,
                          f"could not parse {side} version: {fr.error}", old_b, new_b))
        return fr
    if new_p.whole_file:
        o, n = old_p.remainder[0], new_p.remainder[0]
        if o.own_fingerprint != n.own_fingerprint:
            add(_file_finding(path, ch.change, OUT_OF_SCOPE, "file not in declared scope",
                              old_b, new_b, ws_only=o.ws_fingerprint == n.ws_fingerprint))
        return fr

    oc, nc = old_p.by_qualname(), new_p.by_qualname()
    order = [c.qualname for c in new_p.components] + \
            [c.qualname for c in old_p.components if c.qualname not in nc]
    file_scoped = scope.has_components(path)
    for q in order:
        o, n = oc.get(q), nc.get(q)
        covered = scope.covers_component(path, q)
        if o is not None and n is not None:
            if o.own_fingerprint == n.own_fingerprint:
                continue
            add(_comp_finding(path, q, "modified", IN_SCOPE if covered else OUT_OF_SCOPE,
                              "component in declared scope" if covered
                              else "component not in declared scope", o, n))
        elif n is not None:
            if covered:
                add(_comp_finding(path, q, "added", IN_SCOPE, "component in declared scope",
                                  None, n))
            elif cfg["allow_new_components"] and file_scoped:
                add(_comp_finding(path, q, "added", ALLOWED, "new_component", None, n))
            else:
                why = ("new component in a file with no scoped components" if
                       cfg["allow_new_components"] else
                       "new component outside scope (allow_new_components is off)")
                add(_comp_finding(path, q, "added", OUT_OF_SCOPE, why, None, n))
        else:
            add(_comp_finding(path, q, "removed", IN_SCOPE if covered else OUT_OF_SCOPE,
                              "component in declared scope" if covered
                              else "removed component not in declared scope", o, None))
    for name in (IMPORTS, MODULE):
        o, n = oc[name], nc[name]
        if o.own_fingerprint == n.own_fingerprint:
            continue
        f = _comp_finding(path, name, "modified", OUT_OF_SCOPE, "", o, n)
        f.change = ("added" if f.lines_added and not f.lines_removed else
                    "removed" if f.lines_removed and not f.lines_added else "modified")
        if name == IMPORTS and cfg["allow_imports"]:
            f.category, f.reason = ALLOWED, "import"
        elif name == IMPORTS:
            f.reason = "import change (allow_imports is off)"
        else:
            f.reason = "module-level code outside any scoped component"
        add(f)
    return fr


def classify(cs: ChangeSet, scope: Scope, cfg: dict,
             notes: Optional[dict[str, str]] = None) -> list[FileResult]:
    results = []
    for ch in cs.changes:
        old_b = None if ch.change == "added" else cs.old_bytes(ch.old_path or ch.path)
        new_b = None if ch.change == "removed" else cs.new_bytes(ch.path)
        fr = classify_file(ch, old_b, new_b, scope, cfg)
        for f in fr.findings:
            if f.category == OUT_OF_SCOPE and notes:
                why = note_for(notes, f.path, f.component)
                if why is None and ch.old_path:
                    why = note_for(notes, ch.old_path, f.component)
                if why is not None:
                    f.justified, f.justification = True, why
        results.append(fr)
    return results

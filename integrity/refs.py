"""
refs.py — "removed but still referenced" (dangling reference) check. Standard library only.

For every modified / deleted / renamed file in a change (HEAD vs working tree, or
<sha>^ vs <sha>), extract the symbols the OLD version defines and the NEW version no
longer defines. A symbol counts as removed only if no file in the NEW tree defines it
(so moves between files are fine). Every remaining whole-word use of a removed symbol in
the NEW tree (`git grep -w`) is a dangling reference.

    check_refs(repo, cfg=..., commit=None, ...) -> (exit code, record, stdout text)

No scope declaration needed. Knows nothing about the runner (engine/); talks to git via
subprocess only. Symbol extractors are deliberately simple (regex, `ast` for Python).
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

SCHEMA_VERSION = 1
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
PASS, FAIL, MISCONFIGURED = 0, 1, 2
MAX_HITS_PER_SYMBOL = 10
MAX_FILE_BYTES = 2_000_000

# files never searched and never checked: archived / vendored / generated code
DEFAULT_EXCLUDE = ["archive/**", "**/archive/**", "vendor/**", "**/vendor/**",
                   "**/node_modules/**", "third_party/**", "**/third_party/**", "dist/**",
                   "build/**", "**/.dart_tool/**", "**/*.min.js", "**/*.g.dart",
                   "**/*.lock", "**/package-lock.json"]
DEFAULT_CONFIG = {"mode": "report", "min_length": 4, "ignore": [], "exclude": DEFAULT_EXCLUDE}

STOPLIST = {
    "main", "init", "run", "get", "set", "test", "setup", "index", "data", "name", "value",
    "id", "type", "item", "items", "list", "dict", "self", "this", "args", "kwargs", "config",
    "options", "result", "results", "error", "errors", "handler", "callback", "default",
    "start", "stop", "update", "create", "delete", "remove", "load", "save", "open", "close",
    "read", "write", "render", "parse", "format", "check", "reset", "clear", "build", "make",
    "process", "execute", "handle", "apply", "call", "send", "fetch", "query", "count",
    "size", "length", "text", "title", "body", "content", "state", "props", "user", "users",
    "date", "time", "path", "file", "files", "line", "lines", "key", "keys", "values",
    "true", "false", "null", "none", "string", "number", "object", "array", "status",
    "message", "response", "request", "label", "input", "output", "loop", "tearDown",
    "setUp", "toString", "constructor", "Exception", "Error",
}

CODE_EXTS = {".py", ".php", ".phtml", ".inc", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
             ".dart", ".sql", ".ino", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh",
             ".html", ".htm", ".vue", ".svelte", ".sh", ".bash", ".ps1", ".rb", ".go",
             ".java", ".kt", ".cs", ".rs", ".swift"}
DOC_EXTS = {".md", ".markdown", ".txt", ".rst", ".adoc"}
# a reference only counts from the same language family as the definition (a Python
# constant and a C global of the same name are different symbols). SQL objects (tables,
# views, ...) are referenced from any language, inside string literals.
FAMILY = {".py": "py", ".php": "php", ".phtml": "php", ".inc": "php",
          ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js", ".ts": "js", ".tsx": "js",
          ".html": "js", ".htm": "js", ".vue": "js", ".svelte": "js",
          ".dart": "dart", ".sql": "sql",
          ".ino": "c", ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".cxx": "c",
          ".hpp": "c", ".hh": "c"}
SQL_KINDS = {"table", "view", "function_sql", "procedure", "index", "trigger"}


class RefsError(Exception):
    pass


# ---------------------------------------------------------------------------
# git helpers (own copies: integrity/refs.py must not depend on the gate's internals)
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True, stdin: Optional[bytes] = None
         ) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=str(repo),
                       input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise RefsError(f"git {' '.join(args[:3])} failed: "
                        f"{p.stderr.decode('utf-8', 'replace').strip()}")
    return p


def _norm(p: str) -> str:
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _decode(data: Optional[bytes]) -> Optional[str]:
    if data is None or b"\x00" in data[:8192]:
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def ensure_repo(repo: Path) -> Path:
    if not Path(repo).is_dir():
        raise RefsError(f"repo {repo} is not a directory")
    p = _git(Path(repo), "rev-parse", "--show-toplevel", check=False)
    if p.returncode != 0:
        raise RefsError(f"{repo} is not a git repository")
    return Path(p.stdout.decode("utf-8", "replace").strip())


@dataclass
class Tree:
    """One side of the comparison: a commit, or the working tree (rev=None)."""
    repo: Path
    rev: Optional[str]
    _cache: dict = field(default_factory=dict)

    def prefetch(self, paths) -> None:
        """Load many blobs with one `git cat-file --batch` (process start-up dominates)."""
        todo = [p for p in dict.fromkeys(paths) if p not in self._cache]
        if self.rev is None or self.rev == EMPTY_TREE or not todo:
            return
        req = "".join(f"{self.rev}:{p}\n" for p in todo).encode("utf-8")
        out = _git(self.repo, "cat-file", "--batch", stdin=req, check=False).stdout
        pos = 0
        for p in todo:
            nl = out.find(b"\n", pos)
            if nl < 0:
                break
            head = out[pos:nl].split()
            pos = nl + 1
            if len(head) != 3 or head[1] != b"blob":
                self._cache[p] = None
                if len(head) == 3:
                    pos += int(head[2]) + 1
                continue
            size = int(head[2])
            data = out[pos:pos + size]
            pos += size + 1
            self._cache[p] = _decode(data) if size <= MAX_FILE_BYTES else None

    def read(self, path: str) -> Optional[str]:
        if path in self._cache:
            return self._cache[path]
        if self.rev is None:
            f = self.repo / path
            data = f.read_bytes() if f.is_file() and f.stat().st_size <= MAX_FILE_BYTES else None
        elif self.rev == EMPTY_TREE:
            data = None
        else:
            p = _git(self.repo, "show", f"{self.rev}:{path}", check=False)
            data = p.stdout if p.returncode == 0 and len(p.stdout) <= MAX_FILE_BYTES else None
        self._cache[path] = _decode(data)
        return self._cache[path]


def _name_status(raw: bytes) -> list[tuple[str, str, Optional[str]]]:
    """-> [(status letter, new path, old path or None)]"""
    toks = raw.decode("utf-8", "replace").split("\0")
    out, i = [], 0
    while i < len(toks):
        st = toks[i].strip()
        if not st:
            i += 1
            continue
        if st[0] in "RC":
            out.append((st[0], _norm(toks[i + 2]), _norm(toks[i + 1])))
            i += 3
        else:
            out.append((st[0], _norm(toks[i + 1]), None))
            i += 2
    return out


def collect(repo: Path, commit: Optional[str]) -> tuple[Tree, Tree, list, str, Optional[str]]:
    """-> (old tree, new tree, [(status, path, old_path)], label, commit sha)."""
    if commit:
        p = _git(repo, "rev-parse", "--verify", "-q", f"{commit}^{{commit}}", check=False)
        if p.returncode != 0:
            raise RefsError(f"unknown commit {commit!r}")
        new = p.stdout.decode().strip()
        pp = _git(repo, "rev-parse", "--verify", "-q", f"{new}^", check=False)
        old = pp.stdout.decode().strip() if pp.returncode == 0 else EMPTY_TREE
        raw = _git(repo, "diff", "--name-status", "-M", "-z", old, new).stdout
        return (Tree(repo, old), Tree(repo, new), _name_status(raw),
                f"{new[:10]}^..{new[:10]}", new)
    p = _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
    old = "HEAD" if p.returncode == 0 else EMPTY_TREE
    raw = _git(repo, "diff", "--name-status", "-M", "-z", old).stdout
    # untracked files are only ever additions: they matter for the reference search and
    # for "defined elsewhere", both handled by `git grep --untracked`.
    return Tree(repo, old), Tree(repo, None), _name_status(raw), f"{old}..working tree", None


# ---------------------------------------------------------------------------
# symbol extraction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Sym:
    name: str            # search word (method: bare method name)
    kind: str            # function | class | constant | method | table | macro | ...
    display: str         # Class.method for methods, else name


def _ext(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def _py_syms(text: str) -> Optional[set[Sym]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out: set[Sym] = set()

    def walk(body):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add(Sym(st.name, "function", st.name))
            elif isinstance(st, ast.ClassDef):
                out.add(Sym(st.name, "class", st.name))
                for m in st.body:
                    if (isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and not (m.name.startswith("__") and m.name.endswith("__"))):
                        out.add(Sym(m.name, "method", f"{st.name}.{m.name}"))
            elif isinstance(st, (ast.Assign, ast.AnnAssign)):
                targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            kind = "constant" if re.fullmatch(r"_?[A-Z][A-Z0-9_]*", n.id) \
                                else "variable"
                            out.add(Sym(n.id, kind, n.id))
            elif isinstance(st, (ast.If, ast.Try, ast.With)):
                walk(st.body)
                walk(getattr(st, "orelse", []))
                walk(getattr(st, "finalbody", []))
                for h in getattr(st, "handlers", []):
                    walk(h.body)
    walk(tree.body)
    return out


_PY_FALLBACK = [(re.compile(r"^(?:async\s+)?def\s+(\w+)", re.M), "function"),
                (re.compile(r"^class\s+(\w+)", re.M), "class"),
                (re.compile(r"^([A-Z][A-Z0-9_]+)\s*(?::[^=\n]+)?=(?!=)", re.M), "constant")]

_PHP = [(re.compile(r"\bfunction\s+&?\s*([A-Za-z_]\w*)\s*\(", re.I), "function"),
        (re.compile(r"^\s*(?:(?:abstract|final|readonly)\s+)*(?:class|interface|trait|enum)\s+"
                    r"([A-Za-z_]\w*)", re.I | re.M), "class"),
        (re.compile(r"\bdefine\s*\(\s*['\"]([A-Za-z_]\w*)['\"]", re.I), "constant"),
        (re.compile(r"\bconst\s+([A-Za-z_]\w*)\s*=", re.I), "constant")]

_JS = [(re.compile(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("), "function"),
       (re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)"), "class"),
       (re.compile(r"^\s*export\s+(?:default\s+)?(?:const|let|var|function\*?|class|"
                   r"async\s+function\*?|interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.M),
        "export"),
       (re.compile(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=", re.M),
        "variable"),
       (re.compile(r"\b(?:module\.)?exports\.([A-Za-z_$][\w$]*)\s*="), "export")]
_JS_MODEXP = re.compile(r"module\.exports\s*=\s*\{([^}]*)\}", re.S)

_DART = [(re.compile(r"^\s*(?:abstract\s+|sealed\s+|base\s+|final\s+|interface\s+)*"
                     r"(?:class|enum|mixin|typedef|extension)\s+([A-Za-z_]\w*)", re.M), "class"),
         (re.compile(r"^(?:final|const)\s+(?:[\w<>?,\s]+?\s+)?([A-Za-z_]\w*)\s*=", re.M),
          "constant")]

_SQL = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:UNIQUE\s+)?(?:TEMP(?:ORARY)?\s+)?"
                  r"(TABLE|VIEW|FUNCTION|PROCEDURE|INDEX|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                  r"[`\"\[]?(?:\w+[`\"\]]?\.[`\"\[]?)?(\w+)", re.I)

_C_DEFINE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)", re.M)
_C_FUNC = re.compile(r"^[A-Za-z_][\w\s\*&:<>,]*?[\s\*&]([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*"
                     r"(?:const\s*)?(\{|$)")
_C_KEYWORDS = {"if", "while", "for", "switch", "return", "else", "sizeof", "do", "case"}


def extract(path: str, text: Optional[str]) -> set[Sym]:
    """Symbols defined in one file (empty for unknown extensions / unreadable files)."""
    if not text:
        return set()
    ext = _ext(path)
    out: set[Sym] = set()
    if ext == ".py":
        got = _py_syms(text)
        if got is not None:
            return got
        for rx, kind in _PY_FALLBACK:
            out |= {Sym(m.group(1), kind, m.group(1)) for m in rx.finditer(text)}
        return out
    if ext in (".php", ".phtml", ".inc"):
        for rx, kind in _PHP:
            out |= {Sym(m.group(1), kind, m.group(1)) for m in rx.finditer(text)}
        return out
    if ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
        for rx, kind in _JS:
            out |= {Sym(m.group(1), kind, m.group(1)) for m in rx.finditer(text)}
        for m in _JS_MODEXP.finditer(text):
            for part in m.group(1).split(","):
                nm = re.match(r"\s*([A-Za-z_$][\w$]*)", part)
                if nm:
                    out.add(Sym(nm.group(1), "export", nm.group(1)))
        return out
    if ext == ".dart":
        for rx, kind in _DART:
            out |= {Sym(m.group(1), kind, m.group(1)) for m in rx.finditer(text)}
        return out
    if ext == ".sql":
        return {Sym(m.group(2), {"function": "function_sql"}.get(m.group(1).lower(),
                                                                  m.group(1).lower()),
                    m.group(2)) for m in _SQL.finditer(text)}
    if ext in (".ino", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"):
        out |= {Sym(m.group(1), "macro", m.group(1)) for m in _C_DEFINE.finditer(text)}
        lines = text.split("\n")
        for i, ln in enumerate(lines):
            m = _C_FUNC.match(ln)
            if not m or m.group(1) in _C_KEYWORDS or ln.lstrip().startswith(("#", "//")):
                continue
            if m.group(3) == "{" or (i + 1 < len(lines) and lines[i + 1].strip().startswith("{")):
                out.add(Sym(m.group(1), "function", m.group(1)))
        return out
    return set()


def defined_names(path: str, text: Optional[str]) -> tuple[set[str], set[str]]:
    """-> (non-method names, all names incl. methods) a file defines."""
    syms = extract(path, text)
    return {s.name for s in syms if s.kind != "method"}, {s.name for s in syms}


def family(path: str) -> Optional[str]:
    return FAMILY.get(_ext(path))


_C_EXTS = (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".ino")
_COMMENT_START = ("#", "//", "/*", "*", "<!--", "--", "'''", '"""')
_STRING_RX = re.compile(r'''"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\\n]|\\.)*`''')


def code_view(path: str, line: str) -> Optional[str]:
    """The line with string-literal contents blanked and any trailing comment cut;
    None for a comment-only line. (Per line: multi-line strings/docstrings/block
    comments that do not start with a comment marker are not tracked.)"""
    st = line.strip()
    if st.startswith("#") and _ext(path) in _C_EXTS:
        pass                                   # preprocessor line, not a comment
    elif st.startswith(_COMMENT_START):
        return None
    out = _STRING_RX.sub(lambda m: m.group(0)[0] * 2, line)
    fam = family(path)
    cuts = {"py": ("#",), "php": ("//", " #")}.get(fam, ("//",))
    for marker in cuts:
        i = out.find(marker)
        if i >= 0:
            out = out[:i]
    return out


def _blank(text: str) -> str:
    return "".join(ch if ch == "\n" else " " for ch in text)


def code_only(path: str, text: str) -> list[str]:
    """Whole file with string-literal contents and comments blanked (newlines kept), as a
    list of lines. Python: tokenize (docstrings and multi-line strings included). Others:
    a small scanner for // and /* */ comments (# too for PHP), and '...', "...", `...`
    strings; quotes never span lines, so stray apostrophes in HTML text cannot swallow
    the rest of a file."""
    fam = family(path)
    if fam == "py":
        import io
        import tokenize
        lines = text.split("\n")
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type not in (tokenize.STRING, tokenize.COMMENT) and \
                        getattr(tokenize, "FSTRING_MIDDLE", -1) != tok.type:
                    continue
                (r1, c1), (r2, c2) = tok.start, tok.end
                for r in range(r1, r2 + 1):
                    ln = lines[r - 1]
                    a = c1 if r == r1 else 0
                    b = c2 if r == r2 else len(ln)
                    lines[r - 1] = ln[:a] + " " * (b - a) + ln[b:]
            return lines
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return [code_view(path, ln) or "" for ln in text.split("\n")]
    hash_comment = fam == "php"
    out, i, n = [], 0, len(text)
    state = None                                   # None | "line" | "block" | quote char
    while i < n:
        ch = text[i]
        if ch == "\n":
            out.append(ch)
            if state not in ("block",):
                state = None
            i += 1
            continue
        if state is None:
            two = text[i:i + 2]
            if two == "//" or (hash_comment and ch == "#" and text[i:i + 2] != "#["):
                state = "line"
                out.append(" ")
            elif two == "/*":
                state = "block"
                out.append("  ")
                i += 2
                continue
            elif ch in "'\"`":
                state = ch
                out.append(ch)
            else:
                out.append(ch)
            i += 1
        elif state == "line":
            out.append(" ")
            i += 1
        elif state == "block":
            if text[i:i + 2] == "*/":
                state = None
                out.append("  ")
                i += 2
            else:
                out.append(" ")
                i += 1
        else:                                      # inside a quoted string
            if ch == "\\" and i + 1 < n and text[i + 1] != "\n":
                out.append("  ")
                i += 2
            elif ch == state:
                state = None
                out.append(ch)
                i += 1
            else:
                out.append(" ")
                i += 1
    return "".join(out).split("\n")


def is_definition_line(path: str, line: str, name: str) -> bool:
    st = line.strip()
    if family(path) == "py":
        return bool(re.match(r"(?:async\s+)?def\s+" + re.escape(name) + r"\b|class\s+"
                             + re.escape(name) + r"\b|" + re.escape(name)
                             + r"\s*(?::[^=]+)?=(?!=)", st))
    return any(s.name == name for s in extract(path, st))


# ---------------------------------------------------------------------------
# hit classification
# ---------------------------------------------------------------------------

def hit_class(path: str) -> str:
    p = path.lower()
    parts = p.split("/")
    name = parts[-1]
    if (any(x in ("test", "tests", "__tests__", "spec", "specs") for x in parts[:-1])
            or name.startswith("test_") or re.search(r"(_test|\.test|\.spec|test)\.\w+$", name)):
        return "test"
    if _ext(p) in DOC_EXTS or parts[0] in ("docs", "doc"):
        return "doc"
    if _ext(p) in CODE_EXTS:
        return "code"
    return "other"


def ref_class(path: str) -> str:
    """code / test only for files of a known language family; else doc / other."""
    c = hit_class(path)
    return "other" if c in ("code", "test") and family(path) is None else c


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------

def load_config(path: Optional[Path]) -> dict:
    """The "refs" section of a project config JSON; missing file/section -> defaults."""
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_CONFIG.items()}
    if path is None:
        return cfg
    path = Path(path)
    if not path.is_file():
        return cfg
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object")
    sec = data.get("refs", {})
    if not isinstance(sec, dict):
        raise ValueError('"refs" section must be an object')
    for k, v in sec.items():
        if k.startswith("//") or k.startswith("_"):
            continue
        if k not in DEFAULT_CONFIG:
            raise ValueError(f"unknown refs config key {k!r}")
        cfg[k] = v
    if cfg["mode"] not in ("report", "enforce"):
        raise ValueError(f"refs.mode must be 'report' or 'enforce', not {cfg['mode']!r}")
    if not isinstance(cfg["min_length"], int) or cfg["min_length"] < 1:
        raise ValueError("refs.min_length must be a positive integer")
    for k in ("ignore", "exclude"):
        if not isinstance(cfg[k], list) or not all(isinstance(x, str) for x in cfg[k]):
            raise ValueError(f"refs.{k} must be a list of strings")
    return cfg


_GLOBS: dict[str, re.Pattern] = {}


def _glob_rx(pattern: str) -> re.Pattern:
    """posix glob: `**` crosses directories, `*`/`?` do not."""
    if pattern not in _GLOBS:
        pat, i, out = _norm(pattern), 0, []
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
        _GLOBS[pattern] = re.compile("".join(out) + "(?:/.*)?$")
    return _GLOBS[pattern]


def _excluded(path: str, globs: list[str]) -> bool:
    return any(_glob_rx(g).match(path) for g in globs)


def _grep(repo: Path, rev: Optional[str], words: list[str], exclude: list[str]
          ) -> list[tuple[str, int, str]]:
    """Whole-word fixed-string search of the NEW tree -> [(path, line, text)]."""
    if not words:
        return []
    fd, pf = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(words) + "\n")
        args = ["grep", "-n", "-w", "-I", "-z", "-F", "-f", pf]
        if rev is None:
            args.append("--untracked")
        else:
            args.append(rev)
        args += ["--", "."] + [f":(exclude){e}" for e in exclude]
        p = _git(repo, *args, check=False)
    finally:
        os.unlink(pf)
    if p.returncode not in (0, 1):
        raise RefsError(f"git grep failed: {p.stderr.decode('utf-8', 'replace').strip()}")
    out = []
    prefix = f"{rev}:" if rev else ""
    for raw in p.stdout.decode("utf-8", "replace").split("\n"):
        parts = raw.split("\0")
        if len(parts) < 3:
            continue
        path = parts[0][len(prefix):] if prefix and parts[0].startswith(prefix) else parts[0]
        try:
            out.append((_norm(path), int(parts[1]), "\0".join(parts[2:])))
        except ValueError:
            continue
    return out


def _ref_regex(sym: Sym) -> re.Pattern:
    w = re.escape(sym.name)
    if sym.kind == "method":
        return re.compile(r"(?:\.|->|::|\?\.)\s*" + w + r"\b")
    # group 1: the identifier before a `.` (attribute access), if any
    return re.compile(r"(?:(\w+)\s*\.\s*)?(?<![\w$])" + w + r"(?![\w$])")


def is_reference(r: "Removed", rx: re.Pattern, path: str, text: str,
                 view: Optional[str] = None) -> bool:
    """Does this grep hit really use the removed symbol? Filters (each one a false-positive
    pattern seen on real history): other language family, comment lines, matches inside
    string literals, Python `obj.name` where obj is not the defining module, and lines
    that are themselves a definition of the name (e.g. a test fixture re-defining it)."""
    sym = r.sym
    if sym.kind in SQL_KINDS or family(path) is None:
        return bool(rx.search(text))           # docs / data files: informational only
    if family(path) != family(r.path):
        return False
    if view is None:
        view = code_view(path, text)
    if not view or not view.strip():
        return False
    if is_definition_line(path, text, sym.name):
        return False                           # a definition site, not a use
    for m in rx.finditer(view):
        if sym.kind == "method" or family(path) != "py":
            return True
        before = view[:m.start() + len(m.group(0)) - len(sym.name)].rstrip()
        if not before.endswith("."):
            return True                        # bare name
        owner = re.search(r"(\w+)\s*\.$", before)
        if owner and owner.group(1) == PurePosixPath(r.path).stem:
            return True                        # module.name; obj.name is someone else's
    return False


@dataclass
class Removed:
    sym: Sym
    path: str            # file it was removed from (old path)
    change: str          # modified | removed | renamed
    hits: list = field(default_factory=list)

    @property
    def severity(self) -> str:
        return "dangling" if any(h["class"] in ("code", "test") for h in self.hits) \
            else ("doc_only" if self.hits else "unreferenced")


def find_dangling(repo: Path, commit: Optional[str], cfg: dict,
                  exclude: tuple[str, ...] = ()) -> tuple[list[Removed], dict]:
    old_t, new_t, changes, label, sha = collect(repo, commit)
    min_len, ignore = cfg["min_length"], set(cfg["ignore"])
    globs = list(cfg.get("exclude", []))
    stats = {"compared": label, "commit": sha, "files_checked": 0, "candidates": 0}
    cands: dict[tuple[str, str], Removed] = {}
    old_t.prefetch((o or p) for st, p, o in changes if st not in "AC")
    new_t.prefetch(p for st, p, o in changes if st not in "ACD")
    for st, path, old_path in changes:
        if st in "AC":
            continue
        change = {"D": "removed", "R": "renamed"}.get(st, "modified")
        src = old_path or path
        if any(src.startswith(e.rstrip("/") + "/") for e in exclude) or _excluded(src, globs):
            continue
        old_syms = extract(src, old_t.read(src))
        if not old_syms:
            continue
        stats["files_checked"] += 1
        new_syms = set() if st == "D" else extract(path, new_t.read(path))
        new_names = {s.name for s in new_syms}
        gone_classes = {s.name for s in old_syms if s.kind == "class"} - new_names
        for s in old_syms:
            if s.kind == "method" and s.display.split(".")[0] in gone_classes:
                continue                      # the class itself is the removed symbol
            if s.kind == "variable":          # only UPPER constants are candidates
                continue
            if s.name.startswith("_") and _ext(src) == ".dart":
                continue                      # library-private: the analyzer catches it
            if s.name in new_names or len(s.name) < min_len or s.name in STOPLIST \
                    or s.name.lower() in STOPLIST or s.name in ignore or s.display in ignore:
                continue
            cands.setdefault((s.name, s.kind == "method"), Removed(s, src, change))
    stats["candidates"] = len(cands)
    if not cands:
        return [], stats
    words = sorted({k[0] for k in cands})
    hits = [h for h in _grep(new_t.repo, new_t.rev, words, list(exclude))
            if not _excluded(h[0], globs)]
    # "defined anywhere else in the NEW tree": every definition contains the word, so
    # only files with a hit need extracting.
    by_file: dict[str, list[tuple[int, str]]] = {}
    for path, ln, text in hits:
        by_file.setdefault(path, []).append((ln, text))
    # definitions in test files are fixtures/stubs and do not keep a symbol alive;
    # a definition only counts in the same language family.
    # (key: (family, from_test_file)); a symbol removed from a test file is kept alive by
    # a definition in any file, one removed from product code only by product code.
    defined: dict[tuple, set[str]] = {}
    defined_any: dict[tuple, set[str]] = {}
    views: dict[str, list[str]] = {}
    new_t.prefetch(p for p in by_file if family(p) is not None)
    for path in by_file:
        if family(path) is None:
            continue
        text = new_t.read(path) or ""
        views[path] = code_only(path, text)
        top, anyn = defined_names(path, text)
        keys = [(family(path), True)] + ([] if hit_class(path) == "test"
                                         else [(family(path), False)])
        for k in keys:
            defined.setdefault(k, set()).update(top)
            defined_any.setdefault(k, set()).update(anyn)
    rx = {k: _ref_regex(r.sym) for k, r in cands.items()}
    out = []
    for key, r in sorted(cands.items(), key=lambda kv: (kv[1].path, kv[0][0])):
        name, is_method = key
        dkey = (family(r.path), hit_class(r.path) == "test")
        if name in (defined_any if is_method else defined).get(dkey, set()):
            continue
        for path in sorted(by_file):
            vw = views.get(path)
            for ln, text in by_file[path]:
                view = vw[ln - 1] if vw is not None and ln - 1 < len(vw) else None
                if is_reference(r, rx[key], path, text, view):
                    r.hits.append({"path": path, "line": ln, "class": ref_class(path),
                                   "text": text.strip()[:200]})
        if r.hits:
            out.append(r)
    return out, stats


def build_record(found: list[Removed], stats: dict, *, mode: str, issue, attempt,
                 started: str, finished: str, seconds: float, version: str) -> dict:
    findings = []
    for r in found:
        n = {c: sum(1 for h in r.hits if h["class"] == c) for c in ("code", "test", "doc", "other")}
        order = {"code": 0, "test": 1, "other": 2, "doc": 3}
        hs = sorted(r.hits, key=lambda h: (order[h["class"]], h["path"], h["line"]))
        findings.append({"symbol": r.sym.display, "name": r.sym.name, "kind": r.sym.kind,
                         "removed_from": r.path, "file_change": r.change,
                         "severity": r.severity, "hit_counts": n,
                         "hits": hs[:MAX_HITS_PER_SYMBOL],
                         "hits_truncated": len(hs) > MAX_HITS_PER_SYMBOL})
    n_d = sum(1 for f in findings if f["severity"] == "dangling")
    return {"schema_version": SCHEMA_VERSION, "tool": "integrity refs", "tool_version": version,
            "issue": issue, "attempt": attempt, "commit": stats["commit"],
            "compared": stats["compared"], "mode": mode,
            "verdict": "dangling" if n_d else "pass",
            "counts": {"files_checked": stats["files_checked"],
                       "removed_symbols": stats["candidates"], "dangling": n_d,
                       "doc_only": sum(1 for f in findings if f["severity"] == "doc_only"),
                       "other_only": sum(1 for f in findings if f["severity"] == "unreferenced")},
            "findings": findings, "started": started, "finished": finished,
            "seconds": round(seconds, 3)}


def render_stdout(rec: dict) -> str:
    c = rec["counts"]
    head = (f"REFS CHECK: {rec['verdict'].upper()} ({rec['mode']} mode) - "
            f"{c['dangling']} dangling, {c['doc_only']} doc-only, "
            f"{c['removed_symbols']} removed symbol(s) in {c['files_checked']} file(s)  "
            f"[{rec['compared']}]")
    out = [head]
    for f in rec["findings"]:
        if f["severity"] != "dangling":
            continue
        hc = f["hit_counts"]
        out.append(f"  - {f['kind']} `{f['symbol']}` removed from {f['removed_from']} "
                   f"but still referenced ({hc['code']} code, {hc['test']} test):")
        for h in f["hits"]:
            if h["class"] in ("code", "test"):
                out.append(f"      {h['path']}:{h['line']}: {h['text'][:120]}")
        if f["hits_truncated"]:
            out.append("      ... (more in the record)")
    docs = [f["symbol"] for f in rec["findings"] if f["severity"] == "doc_only"]
    if docs:
        out.append("  doc-only references (informational): " + ", ".join(docs[:20]))
    if rec["verdict"] == "dangling":
        out.append("ACTION: these symbols were deleted but other files still use them. Either "
                   "restore each symbol, or update every listed reference in this same change.")
    return "\n".join(out)


def check_refs(repo: Path, *, cfg: dict, mode: Optional[str] = None,
               commit: Optional[str] = None, issue: Optional[int] = None,
               attempt: Optional[int] = None, record_dir: Optional[Path] = None,
               version: str = "0") -> tuple[int, dict, str]:
    """-> (exit code, record, stdout). Raises RefsError / ValueError when misconfigured."""
    t0 = time.perf_counter()
    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    mode = mode or cfg["mode"]
    if mode not in ("report", "enforce"):
        raise ValueError(f"mode must be report|enforce, not {mode!r}")
    repo = ensure_repo(Path(repo))
    exclude = []
    if record_dir is not None:
        try:
            exclude.append(Path(record_dir).resolve().relative_to(repo.resolve()).as_posix())
        except ValueError:
            pass
    found, stats = find_dangling(repo, commit, cfg, tuple(exclude))
    rec = build_record(found, stats, mode=mode, issue=issue, attempt=attempt, started=started,
                       finished=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                       seconds=time.perf_counter() - t0, version=version)
    text = render_stdout(rec)
    if record_dir is not None:
        record_dir = Path(record_dir)
        record_dir.mkdir(parents=True, exist_ok=True)
        if issue is not None:
            stem = f"refs_issue-{issue}_attempt-{attempt if attempt is not None else 0}"
        elif rec["commit"]:
            stem = f"refs_commit-{rec['commit'][:10]}"
        else:
            stem = "refs_worktree-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        (record_dir / f"{stem}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
        text += f"\n(record: {record_dir / (stem + '.json')})"
    rc = FAIL if rec["verdict"] == "dangling" and mode == "enforce" else PASS
    return rc, rec, text


# ---------------------------------------------------------------------------
# self-tests (temp git repos, offline) — called from `integrity.py self-test`
# ---------------------------------------------------------------------------

def self_test(check, integrity_py: str) -> None:
    import shutil
    import sys

    cfg = load_config(None)
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "r"
        repo.mkdir()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        env = {k: v for k, v in env.items() if not k.startswith("AGENT_")}

        def g(*a):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                  cwd=repo, check=True, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE).stdout.decode()

        def w(rel, text):
            f = repo / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(text.encode("utf-8"))

        def edit(rel, old, new):
            s = (repo / rel).read_text(encoding="utf-8")
            assert old in s, (rel, old)
            w(rel, s.replace(old, new, 1))

        def reset():
            g("checkout", "--", ".")
            g("clean", "-fdq")

        def run(**kw):
            rc, rec, _ = check_refs(repo, cfg=kw.pop("cfg", cfg), **kw)
            dang = sorted(f["symbol"] for f in rec["findings"] if f["severity"] == "dangling")
            return rc, rec, dang

        g("init", "-q")
        g("config", "core.autocrlf", "false")
        w("models/appointments.php", "<?php\nfunction find_appointment(int $id): ?array\n"
          "{\n    return null;\n}\n\nfunction appointments_for_patient($p) { return []; }\n")
        w("config.php", "<?php\ndefine('APP_LOG_FILE', '/tmp/x.log');\n"
          "const APP_VERBOSE_ERRORS = false;\nfunction cfg_get() { return 1; }\n")
        w("book.php", "<?php\nrequire 'models/appointments.php';\n"
          "$a = find_appointment(3);\n// appointments_for_patient is legacy\n"
          "echo 'appointments_for_patient';\n")
        w("lib/errors.php", "<?php\nerror_log('x', 3, APP_LOG_FILE);\n"
          "echo APP_VERBOSE_ERRORS ? 1 : 0;\n$v = cfg_get();\n")
        w("tests/test_errors.php", "<?php\ndefine('APP_LOG_FILE', 'fixture.log');\n")
        w("src/util.py", "LIMIT_MAX = 10\n\n\ndef clamp(x):\n    return x\n\n\n"
          "class Helper:\n    def frobnicate(self):\n        return 1\n\n"
          "    def zzz_unique_m(self):\n        return 2\n")
        w("src/other.py", "class Other:\n    def frobnicate(self):\n        return 0\n")
        w("src/app.py", "from src.util import LIMIT_MAX, clamp, Helper\n\n"
          "print(clamp(LIMIT_MAX), Helper().frobnicate(), Helper().zzz_unique_m())\n")
        w("web/api.js", "export function fetchWidgets() { return []; }\n"
          "export const WIDGET_URL = '/w';\n")
        w("web/main.js", "import { fetchWidgets } from './api.js';\nfetchWidgets();\n")
        w("README.md", "Call `docOnlyThing()` to start.\n")
        w("lib/doc.php", "<?php\nfunction docOnlyThing() {}\nfunction run() {}\n"
          "function abc() {}\n")
        w("lib/use.php", "<?php\nrun(); abc();\n")
        g("add", "-A")
        g("commit", "-qm", "init")

        # 1. PHP function removed but still called
        edit("models/appointments.php", "function find_appointment(int $id): ?array\n"
             "{\n    return null;\n}\n", "")
        rc, rec, dang = run(mode="report")
        check("refs: PHP function removed but still called -> dangling (report exit 0)",
              dang == ["find_appointment"] and rec["verdict"] == "dangling" and rc == PASS, dang)
        f = rec["findings"][0] if rec["findings"] else {}
        check("refs: finding has defining file, kind and file:line hits",
              f.get("removed_from") == "models/appointments.php" and f.get("kind") == "function"
              and f["hits"][0]["path"] == "book.php" and f["hits"][0]["line"] == 3, f)
        rc, _, _ = run(mode="enforce")
        check("refs: enforce mode exit 1 on dangling", rc == FAIL, rc)
        reset()
        edit("models/appointments.php", "function appointments_for_patient($p) { return []; }\n",
             "")
        _, rec, dang = run()
        check("refs: comment-only and string-only references ignored",
              rec["verdict"] == "pass", rec["findings"])
        reset()

        # 2. PHP define/const removed but used (a test fixture define does not keep it alive)
        edit("config.php", "define('APP_LOG_FILE', '/tmp/x.log');\n"
             "const APP_VERBOSE_ERRORS = false;\n", "")
        _, _, dang = run()
        check("refs: PHP define/const removed but used -> dangling",
              dang == ["APP_LOG_FILE", "APP_VERBOSE_ERRORS"], dang)
        reset()

        # 3. symbol moved to another file -> pass
        edit("config.php", "function cfg_get() { return 1; }\n", "")
        w("lib/cfg.php", "<?php\nfunction cfg_get() { return 2; }\n")
        _, rec, dang = run()
        check("refs: symbol moved to another (untracked) file -> pass",
              rec["verdict"] == "pass" and not dang, dang)
        reset()

        # 4. removed and every caller updated in the same change -> pass
        edit("config.php", "function cfg_get() { return 1; }\n", "")
        edit("lib/errors.php", "$v = cfg_get();\n", "$v = 1;\n")
        _, rec, dang = run()
        check("refs: removed + all callers updated -> pass", rec["verdict"] == "pass", dang)
        reset()

        # 5. Python function / constant / class removed but imported elsewhere
        w("src/util.py", "def unrelated():\n    return 0\n")
        _, _, dang = run()
        check("refs: Python function/constant/class removed but imported -> dangling",
              dang == ["Helper", "LIMIT_MAX", "clamp"], dang)
        reset()

        # 6. Python method removed; another class still has the same method name
        edit("src/util.py", "    def frobnicate(self):\n        return 1\n\n", "")
        _, _, dang = run()
        check("refs: Python method removed, same name defined elsewhere -> not flagged",
              dang == [], dang)
        reset()
        edit("src/util.py", "    def zzz_unique_m(self):\n        return 2\n", "    pass\n")
        _, _, dang = run()
        check("refs: Python method removed and still called as .name( -> dangling",
              dang == ["Helper.zzz_unique_m"], dang)
        reset()

        # 6b. false-positive patterns from real history
        w("src/util.py", "def unrelated():\n    return 0\n")
        w("src/app.py", "import util\n\"\"\"clamp the LIMIT_MAX\nHelper text\"\"\"\n"
          "w.clamp(1); obj().clamp(2)  # clamp\nx = 'LIMIT_MAX'\n")
        w("fw/node.ino", "const char* LIMIT_MAX = \"x\";\nint y = LIMIT_MAX;\n")
        w("archive/old.py", "from src.util import clamp\nclamp(1)\n")
        _, rec, dang = run()
        check("refs: obj.name / docstring / string / other-language / archive hits ignored",
              rec["verdict"] == "pass", [(x["symbol"], x["hits"]) for x in rec["findings"]])
        w("src/app.py", "import util\nutil.clamp(1)\n")
        _, _, dang = run()
        check("refs: Python module.name reference -> dangling", dang == ["clamp"], dang)
        reset()

        # 7. JS export removed but imported
        edit("web/api.js", "export function fetchWidgets() { return []; }\n", "")
        _, _, dang = run()
        check("refs: JS export removed but imported -> dangling", dang == ["fetchWidgets"], dang)
        reset()

        # 8. reference only in README -> doc_only, verdict pass
        edit("lib/doc.php", "function docOnlyThing() {}\n", "")
        _, rec, dang = run()
        check("refs: README-only reference -> doc_only, verdict pass",
              rec["verdict"] == "pass" and [x["severity"] for x in rec["findings"]]
              == ["doc_only"], rec["findings"])
        reset()

        # 9. short / stoplisted names ignored; config ignore list
        edit("lib/doc.php", "function run() {}\nfunction abc() {}\n", "")
        _, rec, dang = run()
        check("refs: short and stoplisted names ignored",
              rec["verdict"] == "pass" and rec["counts"]["removed_symbols"] == 0, rec["counts"])
        reset()
        edit("models/appointments.php", "function find_appointment(int $id): ?array\n"
             "{\n    return null;\n}\n", "")
        _, rec, dang = run(cfg=dict(cfg, ignore=["find_appointment"]))
        check("refs: config ignore list", rec["verdict"] == "pass", dang)

        # 10. CLI: record written (issue/attempt naming), exit codes
        rd = Path(d) / "rec"
        cp = subprocess.run([sys.executable, integrity_py, "refs", "--repo", str(repo),
                             "--record-dir", str(rd), "--issue", "66", "--attempt", "2"],
                            stdout=subprocess.PIPE, text=True, env=env)
        rp = rd / "refs_issue-66_attempt-2.json"
        ok = rp.is_file() and json.loads(rp.read_text(encoding="utf-8"))["schema_version"] == 1
        check("refs CLI: report mode exit 0, record written, stdout tells agent what to do",
              cp.returncode == 0 and ok and "find_appointment" in cp.stdout
              and "restore" in cp.stdout, cp.stdout)
        cp = subprocess.run([sys.executable, integrity_py, "refs", "--repo", str(repo),
                             "--mode", "enforce"], stdout=subprocess.PIPE, text=True, env=env)
        check("refs CLI: enforce exit 1", cp.returncode == 1, cp.stdout)
        (Path(d) / "bad.json").write_text('{"refs": {"strict": true}}', encoding="utf-8")
        cp = subprocess.run([sys.executable, integrity_py, "refs", "--repo", str(repo),
                             "--config", str(Path(d) / "bad.json")],
                            stdout=subprocess.PIPE, text=True, env=env)
        check("refs CLI: unknown config key -> exit 2", cp.returncode == 2, cp.stdout)
        cp = subprocess.run([sys.executable, integrity_py, "refs", "--repo", d],
                            stdout=subprocess.PIPE, text=True, env=env)
        check("refs CLI: not a git repo -> exit 2", cp.returncode == 2, cp.stdout)

        # 11. --commit mode compares SHA^ vs SHA and never reads the working tree
        g("commit", "-qam", "drop find_appointment")
        sha = g("rev-parse", "HEAD").strip()
        w("models/appointments.php", "<?php\nfunction find_appointment() {}\n")  # dirty tree
        rc, rec, dang = run(commit=sha, mode="enforce", record_dir=rd)
        check("refs --commit: SHA^ vs SHA, ignores working tree, record written",
              dang == ["find_appointment"] and rc == FAIL
              and (rd / f"refs_commit-{sha[:10]}.json").is_file(), dang)
        reset()
        rc, rec, dang = run(commit="HEAD~1")
        check("refs --commit: initial commit -> pass", rec["verdict"] == "pass", dang)
        shutil.rmtree(rd, ignore_errors=True)

"""
components.py — format adapters: turn one file's content into named components.

This module is the seam for future formats (xlsx/docx/pptx). An adapter takes the
raw bytes of a file and returns a `Parsed`:

    components   real, addressable parts (Python: functions, classes, methods),
                 each with a qualname, kind, span/locator, tags, description and
                 three fingerprints:
                   fingerprint      sha256 of the whole extent (children included)
                   own_fingerprint  sha256 of the extent MINUS its children's
                                    extents, so editing a method leaves its
                                    class's own fingerprint alone
                   ws_fingerprint   own content with all whitespace removed; equal
                                    ws + different own => whitespace-only change
    remainder    pseudo-components for everything not inside a component
                 (Python: `<imports>` and `<module>`; fallback: `<file>`)
    error        set when the file could not be parsed -> the gate says "unverified"

Nothing here knows about git, scopes or the runner. Standard library only.
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

TAG_RE = re.compile(r"^\s*(?:#|//|--|;|')\s*@tags\s*:\s*(.*?)\s*$", re.IGNORECASE)
COMMENT_PREFIXES = ("#", "//")
FILE, MODULE, IMPORTS = "<file>", "<module>", "<imports>"
PSEUDO = (FILE, MODULE, IMPORTS)


# ---------------------------------------------------------------------------
# generic helpers (reusable by any adapter)
# ---------------------------------------------------------------------------

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def fingerprints(lines: list[str]) -> tuple[str, str]:
    """(fingerprint, whitespace-insensitive fingerprint) of a block of lines.

    Lines are rstripped and blank lines are dropped, so trailing whitespace and
    blank-line shuffling between components never count as a change."""
    norm = [ln.rstrip() for ln in lines if ln.strip()]
    return (sha256_text("\n".join(norm)),
            sha256_text("".join("".join(ln.split()) for ln in norm)))


def parse_tags(line: str) -> Optional[list[str]]:
    """Tags from a `# @tags: a, b` / `// @tags: a, b` line, else None."""
    m = TAG_RE.match(line)
    if not m:
        return None
    return [t.strip().lower() for t in re.split(r"[,\s]+", m.group(1)) if t.strip()]


def normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def decode_text(data: bytes) -> Optional[str]:
    """UTF-8 (BOM tolerated) text with LF newlines, or None for binary/undecodable."""
    if is_binary(data):
        return None
    try:
        return normalise_newlines(data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return None


@dataclass
class Component:
    qualname: str
    kind: str
    span: tuple[int, int]               # 1-based inclusive lines (decorators included)
    tags: list[str] = field(default_factory=list)
    description: str = ""
    fingerprint: str = ""
    own_fingerprint: str = ""
    ws_fingerprint: str = ""
    parent: Optional[str] = None
    locator: str = ""                   # generic, format-specific address ("L10-42", "Sheet1!A1:C9")
    own_lines: list[tuple[int, str]] = field(default_factory=list, repr=False)  # for diffs

    def to_dict(self) -> dict:
        return {"qualname": self.qualname, "kind": self.kind, "lines": list(self.span),
                "locator": self.locator, "tags": self.tags, "description": self.description,
                "fingerprint": self.fingerprint, "own_fingerprint": self.own_fingerprint,
                "ws_fingerprint": self.ws_fingerprint, "parent": self.parent}


@dataclass
class Parsed:
    adapter: str
    components: list[Component] = field(default_factory=list)
    remainder: list[Component] = field(default_factory=list)
    error: Optional[str] = None
    whole_file: bool = False            # True: only file-level comparison is meaningful

    def by_qualname(self) -> dict[str, Component]:
        return {c.qualname: c for c in self.components + self.remainder}


class Adapter:
    """Interface. Subclasses set `name`/`extensions` and implement parse()."""
    name = "base"
    extensions: tuple[str, ...] = ()

    def parse(self, data: bytes, path: str = "") -> Parsed:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# FileAdapter: the fallback — one whole-file component
# ---------------------------------------------------------------------------

class FileAdapter(Adapter):
    name = "file"

    def parse(self, data: bytes, path: str = "") -> Parsed:
        text = decode_text(data)
        if text is None:
            fp = hashlib.sha256(data).hexdigest()
            comp = Component(FILE, "file", (0, 0), fingerprint=fp, own_fingerprint=fp,
                             ws_fingerprint=fp, locator="binary")
        else:
            lines = text.split("\n")
            fp, ws = fingerprints(lines)
            comp = Component(FILE, "file", (1, len(lines)), fingerprint=fp, own_fingerprint=fp,
                             ws_fingerprint=ws, locator=f"L1-{len(lines)}",
                             own_lines=list(enumerate(lines, 1)))
        return Parsed(self.name, [], [comp], None, whole_file=True)


# ---------------------------------------------------------------------------
# PythonAdapter: ast-based
# ---------------------------------------------------------------------------

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def base_qualname(q: str) -> str:
    """`C.x#2.inner` -> `C.x.inner` (duplicate-name suffixes removed)."""
    return re.sub(r"#\d+", "", q)


class PythonAdapter(Adapter):
    name = "python"
    extensions = (".py", ".pyw", ".pyi")

    def parse(self, data: bytes, path: str = "") -> Parsed:
        if is_binary(data):
            return Parsed(self.name, error="binary content in a .py file")
        try:
            text = normalise_newlines(data.decode("utf-8-sig"))
        except UnicodeDecodeError:
            text = normalise_newlines(data.decode("latin-1"))
        lines = text.split("\n")
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError) as exc:
            return Parsed(self.name, error=f"SyntaxError line {getattr(exc, 'lineno', '?')}: "
                                           f"{getattr(exc, 'msg', exc)}")

        recs: list[dict] = []
        seen: dict[str, int] = {}

        def header_start(start: int, def_line: int) -> int:
            """Extend upwards over comment lines at the def's indentation (tag blocks)."""
            indent = len(lines[def_line - 1]) - len(lines[def_line - 1].lstrip())
            hs = start
            while hs - 1 >= 1:
                ln = lines[hs - 2]
                if ln.lstrip().startswith(COMMENT_PREFIXES) and \
                        len(ln) - len(ln.lstrip()) == indent:
                    hs -= 1
                else:
                    break
            return hs

        def visit(node, prefix: str, parent: Optional[dict]):
            for child in ast.iter_child_nodes(node):
                if not isinstance(child, _DEFS):
                    visit(child, prefix, parent)
                    continue
                q = f"{prefix}.{child.name}" if prefix else child.name
                seen[q] = seen.get(q, 0) + 1
                if seen[q] > 1:
                    q = f"{q}#{seen[q]}"
                if isinstance(child, ast.ClassDef):
                    kind = "class"
                elif parent is not None and parent["kind"] == "class":
                    kind = "async_method" if isinstance(child, ast.AsyncFunctionDef) else "method"
                else:
                    kind = "async_function" if isinstance(child, ast.AsyncFunctionDef) else "function"
                start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                end = child.end_lineno or child.lineno
                hs = header_start(start, child.lineno)
                tags: list[str] = []
                for i in range(hs, start):
                    t = parse_tags(lines[i - 1])
                    if t:
                        tags.extend(t)
                doc = ast.get_docstring(child, clean=True) or ""
                rec = {"q": q, "kind": kind, "start": start, "end": end, "hs": hs,
                       "tags": tags, "desc": doc.strip().split("\n")[0].strip() if doc else "",
                       "parent": parent["q"] if parent else None}
                recs.append(rec)
                visit(child, q, rec)

        visit(tree, "", None)

        comps: list[Component] = []
        for r in recs:
            extent = set(range(r["hs"], r["end"] + 1))
            own = set(extent)
            for c in recs:
                if c["parent"] == r["q"]:
                    own -= set(range(c["hs"], c["end"] + 1))
            whole_fp, _ = fingerprints([lines[i - 1] for i in sorted(extent)])
            own_lines = [(i, lines[i - 1]) for i in sorted(own)]
            own_fp, ws_fp = fingerprints([t for _, t in own_lines])
            comps.append(Component(r["q"], r["kind"], (r["start"], r["end"]), r["tags"],
                                   r["desc"], whole_fp, own_fp, ws_fp, r["parent"],
                                   f"L{r['start']}-{r['end']}", own_lines))

        covered: set[int] = set()
        for r in recs:
            if r["parent"] is None:
                covered |= set(range(r["hs"], r["end"] + 1))
        import_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno not in covered:
                import_lines |= set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        n = len(lines)
        imp = [(i, lines[i - 1]) for i in range(1, n + 1) if i in import_lines]
        other = [(i, lines[i - 1]) for i in range(1, n + 1)
                 if i not in covered and i not in import_lines]
        remainder = []
        for name, kind, numbered in ((IMPORTS, "imports", imp), (MODULE, "module", other)):
            fp, ws = fingerprints([t for _, t in numbered])
            remainder.append(Component(name, kind, (1, n), fingerprint=fp, own_fingerprint=fp,
                                       ws_fingerprint=ws, locator="remainder",
                                       own_lines=numbered))
        return Parsed(self.name, comps, remainder)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

ADAPTERS: list[Adapter] = [PythonAdapter()]
FALLBACK = FileAdapter()


def register(adapter: Adapter) -> None:
    ADAPTERS.insert(0, adapter)


def adapter_for(path: str) -> Adapter:
    low = path.lower()
    for a in ADAPTERS:
        if any(low.endswith(ext) for ext in a.extensions):
            return a
    return FALLBACK

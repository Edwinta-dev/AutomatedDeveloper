# integrity — deterministic scope gate + component index

A standalone, standard-library-only (Python 3.10+) package that checks an agent's
change stayed inside the scope its issue declared, and records the result.

It is **independent of the runner** (`engine/`): it never imports it and talks to it
only through CLI arguments, `AGENT_*` environment variables, files and exit codes.
The component model is format-agnostic (an adapter per file type) so that later
adapters for Excel/Word/PowerPoint can plug into the same gate.

| File | Role |
|---|---|
| `integrity.py` | CLI: `gate`, `index`, `lookup`, `bench`, `self-test` |
| `components.py` | `Adapter` interface, `PythonAdapter` (ast), `FileAdapter` (fallback), registry |
| `scope.py` | scope / `SCOPE_NOTES` parsing, change collection via git, classification |
| `report.py` | JSON record, Markdown report, stdout summary |

## CLI

```
python integrity/integrity.py gate   [--repo R] [--config project.json] [--mode report|enforce]
                                     [--scope "a.py::f, b/**"] [--commit SHA]
                                     [--issue-file F] [--result-file F] [--issue N] [--attempt K]
                                     [--record-dir D]
python integrity/integrity.py index  --repo R [--out index.json] [--paths GLOB ...]
python integrity/integrity.py lookup --repo R [--tag T ...] [--any] [--name SUBSTR] [--path GLOB]
python integrity/integrity.py bench  --repo R [--samples 200] [--seed 1]
python integrity/integrity.py self-test
```

Runs from any cwd. As a runner gate (`validate.json`, cwd = target repo):

```json
{"label": "Scope gate",
 "argv": ["__PY__", "<path-to>/integrity/integrity.py", "gate", "--config", "__PROJECT_CONFIG__"]}
```

### gate

* Default: compares **HEAD vs the working tree** (the agent's edits are uncommitted).
  Changed files = `git diff --name-status -M HEAD` + untracked files
  (`git ls-files --others --exclude-standard`). Old content via `git show HEAD:<path>`.
* `--commit SHA`: compares `SHA^` vs `SHA` using `git show` on both sides; the working
  tree is never read or touched (a root commit is compared with the empty tree). Use it
  to measure scope drift on past commits.
* Scope comes from `--scope` (same syntax as the Scope line; overrides the issue) or the
  issue file (`--issue-file`, default `$AGENT_ISSUE_FILE`).
* `SCOPE_NOTES` come from `--result-file` (default `$AGENT_RESULT_FILE`).
* Issue / attempt: `--issue`/`--attempt`, else `$AGENT_ISSUE_NUMBER`/`$AGENT_ATTEMPT`,
  else the issue file header `# #N title`.
* Records go to `--record-dir`, default `$AGENT_RUN_DIR/integrity`; with neither, no
  records are written. Names: `scope_issue-<N>_attempt-<K>.{json,md}`; without an
  issue number `scope_commit-<sha10>.*` (commit mode) or `scope_worktree-<timestamp>.*`.
  A record dir inside the repo is excluded from the diff.

Exit codes: **0** pass / skipped / anything in `report` mode, **1** `enforce` mode and
any `out_of_scope` or `unverified` finding, **2** misconfigured (not a git repo, unknown
commit, bad args, bad config).

Stdout is a short plain summary. On a violation it lists every out-of-scope finding
with its diff hunk(s) (60 lines per finding, 300 total) and tells the agent to revert,
or — only if truly required — keep the change and explain it under `SCOPE_NOTES`
(which in enforce mode does **not** lift the block; a human must widen the scope).

## Scope syntax (contract with the runner)

One line in the issue body (first match outside fenced code blocks):

```
**Scope:** `src/hsv.py::analyse_frame`, `src/hsv.py::HSV.threshold`, `tests/test_hsv.py`, `src/vision/**`
```

Comma-separated; backticks optional. Each entry is one of:

| Entry | Meaning |
|---|---|
| `path/to/file.py` | whole file in scope (a bare directory name also covers its contents) |
| `src/vision/**`, `src/*.py` | glob, posix separators; `**` crosses directories, `*` and `?` do not |
| `path.py::Qualname` | a component. `Class` covers all its methods/nested defs; `Class.method` only that method; `func` covers functions nested in it; `CONST` a module-level constant (e.g. `Backend/koi/storage/memory.py::IMAGE_COLUMNS`) |

No Scope line → `no scope declared: skipped`, exit 0, verdict `skipped`.
`**Scope:** none` declares an empty scope (only allowed changes pass).

### SCOPE_NOTES (agent result block)

```
SCOPE_NOTES:
- src/util.py::clamp: needed to accept float input for the new threshold
- src/other.py: whole-file justification
```

Out-of-scope findings whose `path` or `path::component` is named get
`justified: true` + the text. **Justification is recorded only; it never changes the
verdict** — an LLM's explanation does not authorise a change.

## Classification

Per changed file, first matching rule wins:

1. File matches a whole-file/glob scope entry → `in_scope`.
2. File (or a renamed file's old path) matches `protect_globs` → `out_of_scope`, reason
   `protected path (agent/tool configuration or backup)`. This beats `allow_globs` and
   `allow_new_files`: only a scope entry naming the file (exact path or a glob) lifts it.
3. File matches `allow_globs` → `allowed` (`allow_glob`). Allowed findings (e.g. doc
   edits) are still listed in the record and report.
4. Added/untracked file → `allowed` (`new_file`) if `allow_new_files`, else `out_of_scope`.
5. Deleted or renamed file → `out_of_scope`.
6. Adapter-level comparison by qualname (Python; functions, classes, methods and module-level constants):
   * own fingerprint changed → `in_scope` if the qualname or an ancestor is scoped, else `out_of_scope`;
   * component only in new → `in_scope` if scoped; `allowed` (`new_component`) if
     `allow_new_components` and the scope names at least one component in this file; else `out_of_scope`;
   * component only in old (removed) → `in_scope` if scoped, else `out_of_scope`;
   * `<imports>` changed → `allowed` (`import`) if `allow_imports`, else `out_of_scope`;
   * `<module>` (other module-level lines) changed → `out_of_scope`.
7. Fallback-adapter files (non-Python) not scoped → `out_of_scope` at `<file>`.

### Default allowances and protected paths

`allow_globs` (default) — changes that are almost always part of the same piece of work:

* tests, Python: `tests/**`, `**/test_*.py`, `**/*_test.py`, `**/conftest.py`
* tests, other languages: `**/test/**`, `**/__tests__/**`, `**/*.test.*`, `**/*.spec.*`,
  `**/*_test.dart`, `**/*Test.php`, `**/*_test.go`
* docs: `**/*.md`, `docs/**`, `**/.env.example`, `**/*.env.example`

`protect_globs` (default) — agent/tool configuration and backup files, which an agent
should never change as a side effect (an agent editing `CLAUDE.md` to defer its own
issue, or committing `.mcp.json` / `.claude/settings` backups):
`**/AGENTS.md`, `**/CLAUDE.md`, `**/.claude/**`, `**/.mcp.json`, `**/.codex/**`,
`**/*.bak`, `**/*.bak-*`, `**/*.orig`. So `CLAUDE.md` is flagged even though `**/*.md`
is allowed; to permit it, put `CLAUDE.md` in the issue's Scope line.

A file that does not parse (old or new side) → `unverified` (never a silent pass).
`whitespace_only` is set when the own fingerprint differs but the whitespace-insensitive
one is equal; such changes are still out of scope if outside the scope.
Verdict: `violation` if any `out_of_scope`, else `unverified` if any `unverified`, else `pass`;
`skipped` when no scope is declared.

## Config

`--config <project config JSON>`, section `"scope"` (missing file/section → defaults;
keys starting with `//` are ignored; unknown keys → exit 2):

```json
{"scope": {
  "mode": "report",
  "allow_globs": ["tests/**", "**/test_*.py", "**/*_test.py", "**/conftest.py",
                  "**/test/**", "**/__tests__/**", "**/*.test.*", "**/*.spec.*",
                  "**/*_test.dart", "**/*Test.php", "**/*_test.go",
                  "**/*.md", "docs/**", "**/.env.example", "**/*.env.example"],
  "protect_globs": ["**/AGENTS.md", "**/CLAUDE.md", "**/.claude/**", "**/.mcp.json",
                    "**/.codex/**", "**/*.bak", "**/*.bak-*", "**/*.orig"],
  "allow_imports": true,
  "allow_new_files": true,
  "allow_new_components": true
}}
```

Accepted keys: `mode`, `allow_globs`, `protect_globs`, `allow_imports`,
`allow_new_files`, `allow_new_components`. A list given in the config replaces the
default list (it is not merged). `--mode` overrides `mode`.

## Components and tags

Adapter interface (`components.py`): `parse(bytes, path) -> Parsed(components, remainder, error)`.
A `Component` has `qualname`, `kind`, `span` (1-based inclusive lines) plus a generic
`locator` string (for non-line formats, e.g. `Sheet1!A1:C9`), `tags`, `description`,
`fingerprint` (whole extent), `own_fingerprint` (extent minus children),
`ws_fingerprint` (own content, all whitespace removed). Register new adapters with
`components.register(adapter)`; unknown extensions use `FileAdapter` (one `<file>` component).

PythonAdapter: functions, async functions, classes, nested classes, methods
(`HSV.threshold`); spans include decorators. Module-level constants are components too
(kind `constant`, qualname = the target name): any top-level `NAME = ...` or
`NAME: T = ...` with a single simple-name target, including `__all__`, tuple/list/dict
literals and multi-line values. Each has its own fingerprint, so editing one constant
flags only that constant. Chained (`a = b = 1`), tuple-unpacking, attribute/subscript
and augmented (`X += 1`) assignments stay in `<module>`; constants do not absorb the
comment lines above them and carry no tags. Lines are rstripped and blank lines ignored
before hashing. The contiguous comment block directly above a def (same indentation)
belongs to that component, so editing its tags is a change to it. Duplicate qualnames
(e.g. property getter/setter) become `name#2`; scope matching ignores the suffix.
Pseudo-components: `<imports>` (module-level `import`/`from` statements, multi-line
included, plus whole top-level `if TYPE_CHECKING:` / `try:` blocks whose statements are
all imports apart from `pass`) and `<module>` (all other lines outside components).

Tags — comment lines directly above the def/decorators (`parse_tags` also accepts `//`):

```python
# @tags: embedded, sensors, backend
@decorator
def read_sensor(): ...
```

Description = first line of the docstring.

## Record schema (`schema_version: 1`)

```json
{
  "schema_version": 1, "tool": "integrity scope gate", "tool_version": "0.1.0",
  "issue": 12, "attempt": 2, "commit": null, "compared": "HEAD..working tree",
  "mode": "enforce", "verdict": "pass|violation|skipped|unverified", "skipped_reason": "",
  "scope_declared": ["src/hsv.py::HSV.threshold", "tests/test_hsv.py"],
  "files": [{
    "path": "src/hsv.py", "old_path": null, "change": "added|modified|removed|renamed",
    "adapter": "python|file", "error": null,
    "findings": [{
      "path": "src/hsv.py", "component": "HSV.threshold | <file> | <module> | <imports>",
      "change": "added|modified|removed|renamed",
      "category": "in_scope|allowed|out_of_scope|unverified",
      "reason": "component in declared scope | import | allow_glob | new_file | new_component | ...",
      "whitespace_only": false, "justified": false, "justification": "",
      "kind": "method", "lines": [19, 23], "tags": ["vision"], "description": "Threshold x.",
      "lines_added": 1, "lines_removed": 1,
      "diff": ["@@ -23,1 +23,1 @@", "..."], "diff_truncated": false
    }]
  }],
  "counts": {"in_scope": 1, "allowed": 0, "out_of_scope": 0, "unverified": 0,
             "justified": 0, "whitespace_only": 0},
  "lines_changed": {"in_scope": 2, "out_of_scope": 0},
  "started": "2026-10-09T06:52:54+00:00", "finished": "2026-10-09T06:52:55+00:00"
}
```

`diff`/`diff_truncated` are present only on `out_of_scope` and `unverified` findings.
Tags/descriptions come from the new version (old version for removed components).
The `.md` report has a summary, a per-component table with tags and descriptions, an
out-of-scope section with diffs and agent justifications, and an unverified section.

## index / lookup

`index` walks tracked + untracked-not-ignored files (`git ls-files`), skips files over
1 MB and binaries, and emits components (`path::qualname`, kind, lines, tags, description,
fingerprint) for files with a real adapter (fallback files are not indexed). `lookup`
prints one line per component:

```
src/hsv.py::HSV.threshold  [vision]  L19-23  Threshold x.
```

Tags match ALL by default, `--any` for any. Everything is computed on demand (no
database). **Future work:** cache the index keyed by blob sha (`git ls-files -s`) so only
changed files are re-parsed.

## bench

`bench` checks out HEAD into a temporary detached `git worktree` (always removed, even on
error; the real working tree is never touched), then repeatedly: picks a random Python
component (excluding files matched by `allow_globs`), applies a syntactically valid
mutation — a no-op `_integrity_probe = 0` as the first body statement after any
docstring (60%), a whitespace-only edit verified to leave the AST unchanged (20%), or an
import addition (20%) — declares a random scope that does or does not cover it, runs the
classifier and compares with ground truth. It reports precision/recall of out-of-scope
detection, localisation (the flagged component is exactly the mutated one), in-scope
false-positive rate, whitespace-only detection, import-allowed rate and gate time.
Between samples it runs `git checkout -- . && git clean -fd` inside the temp copy only.

This measures the deterministic machinery against its own component model; it does
not measure whether declared scopes are *right*, nor adversarial evasion.

## Limitations

* Blank-line-only and trailing-whitespace-only edits are invisible by design (normalised away).
* A moved function (same qualname, same content) is not a change; a renamed function is
  a removal + an addition.
* Comments directly above a def belong to that def; other module-level comments are `<module>`.
* Module-level code other than constants and imports (`if __name__ == ...`, calls,
  augmented assignments) can only be scoped by declaring the whole file.
* A `try:`/`if` block that mixes imports with other statements (e.g. `except ImportError:
  x = None`) is not an import block: its imports are `<imports>`, the rest `<module>`.
* Text decoding assumes UTF-8 (Python falls back to latin-1); non-UTF-8 non-Python files
  are compared as bytes.
* Gate time is dominated by git process start-up (~0.5 s per run on Windows).
* Only Python has a component adapter today; everything else is whole-file.

## refs: dangling reference check

Catches the one kind of harmful change found in the study of 232 unattended-agent commits:
an agent rewrites a file and deletes functions/constants that **other files still use**,
and the test suite does not notice (e.g. IE4727 #66 deleted `find_appointment` still
called by `book.php`; #62 deleted the `APP_LOG_FILE` define still used by `lib/errors.php`).
Needs no scope declaration. Code: `integrity/refs.py` (standard library only).

```
python integrity/integrity.py refs [--repo R] [--commit SHA] [--config C]
                                   [--mode report|enforce] [--record-dir D]
                                   [--issue N --attempt K]
```

Default compares HEAD with the working tree (tracked edits + untracked files);
`--commit SHA` compares `SHA^` with `SHA` via `git cat-file`/`git grep SHA` and never reads
the working tree. `--issue`/`--attempt`/`--record-dir` default to `$AGENT_ISSUE_NUMBER`,
`$AGENT_ATTEMPT`, `$AGENT_RUN_DIR/integrity`, as for `gate`.

Validate step (runs with cwd = target repo):

```json
{"label": "Dangling references",
 "argv": ["__PY__", "<path>/integrity/integrity.py", "refs", "--config", "__PROJECT_CONFIG__"]}
```

### How it decides

1. For every modified / deleted / renamed file, extract the symbols the OLD version defines
   and the NEW version does not:
   * Python (`ast`): module-level functions, classes, UPPER_CASE constants, methods
     (`Class.method`; methods of a class that was removed entirely are not listed —
     the class is).
   * PHP: `function name(` (incl. methods), `class|interface|trait|enum Name`,
     `define('NAME'`, `const NAME =`.
   * JS/TS/MJS/CJS: `function name`, `class Name`, `export ... name`, column-0
     `const|let|var name =`, `exports.name =`, `module.exports = { a, b }`.
   * Dart: `class|enum|mixin|typedef|extension Name`, column-0 `final|const name =`
     (library-private `_names` are skipped: the analyzer already catches them).
   * SQL: `CREATE [OR REPLACE] TABLE|VIEW|FUNCTION|PROCEDURE|INDEX|TRIGGER [IF NOT EXISTS] name`.
   * C/C++/Arduino: `#define NAME`, column-0 function definitions `type name(...) {`.
   * Anything else: no symbols.
   Names shorter than `min_length` (4), in a built-in stoplist of generic names (`main`,
   `init`, `run`, `get`, `setup`, `index`, `data`, `update`, `delete`, `save` ...) or in `ignore`
   are skipped.
2. One `git grep -w -F` over the NEW tree finds every line containing any candidate word.
   A candidate is **not removed** if a file of the same language family in the new tree
   defines it (moves between files are fine). Definitions in test files keep alive only
   symbols that were themselves removed from a test file (a test fixture re-`define`-ing
   `APP_LOG_FILE` does not hide the removal from `config.php`).
3. Each remaining hit must be a real use. Filtered out (each one a false-positive pattern
   seen on real history):
   * hits in another language family (a Python constant vs a C global of the same name);
     SQL objects are the exception — they are referenced from any language, in strings;
   * comments and string literals (whole-file: `tokenize` for Python incl. docstrings,
     a small scanner for `//`, `/* */`, `#`, quotes elsewhere);
   * lines that are themselves a definition of the name;
   * Python `obj.name` where `obj` is not the defining module (`label.configure()` is not
     `client.configure`); methods only match as `.name` / `->name` / `::name`;
   * paths in `exclude` (default: `archive/`, `vendor/`, `node_modules/`, `third_party/`,
     `dist/`, `build/`, `.dart_tool/`, `*.min.js`, `*.g.dart`, lockfiles).
4. Hits are classified `code` (known source extension), `test` (`tests/**`, `test_*`,
   `*_test.*`, `*.spec.*`, `conftest.py` ...) or `doc` (`*.md`, `*.txt`, `*.rst`, `docs/**`);
   anything else is `other`. Severity: **dangling** if any code or test hit, **doc_only**
   if only doc hits (informational).

Verdict `dangling` if any finding is dangling, else `pass`. Exit codes: report mode
always 0; enforce mode 1 on `dangling`; 2 misconfigured (not a git repo, bad config,
unknown commit).

On `dangling` stdout lists each symbol, the file it was removed from and the remaining
`file:line: text` references (code and test), then tells the agent: *restore the
symbol, or update every listed reference in this same change.*

### Config

```json
"refs": {"mode": "report", "min_length": 4, "ignore": ["legacy_helper"],
         "exclude": ["archive/**", "vendor/**"]}
```

All keys optional (missing section = defaults, mode `report`); unknown keys -> exit 2.
`exclude` replaces the default list. `ignore` takes bare names or `Class.method`.

### Record

`refs_issue-<N>_attempt-<K>.json`, else `refs_commit-<sha10>.json`, else
`refs_worktree-<timestamp>.json` in the record dir:

```json
{
  "schema_version": 1, "tool": "integrity refs", "tool_version": "0.1.0",
  "issue": 66, "attempt": 1, "commit": null, "compared": "HEAD..working tree",
  "mode": "report", "verdict": "dangling",
  "counts": {"files_checked": 2, "removed_symbols": 7, "dangling": 7, "doc_only": 0,
             "other_only": 0},
  "findings": [{
    "symbol": "find_appointment", "name": "find_appointment", "kind": "function",
    "removed_from": "clinic-base/models/appointments.php", "file_change": "modified",
    "severity": "dangling",
    "hit_counts": {"code": 3, "test": 1, "doc": 0, "other": 0},
    "hits": [{"path": "clinic-base/book.php", "line": 21, "class": "code",
              "text": "$candidate = ... find_appointment($rescheduleId) : null;"}],
    "hits_truncated": false
  }],
  "started": "...", "finished": "...", "seconds": 0.51
}
```

`symbol` is `Class.method` for methods, `name` is the searched word, `kind` one of
function / class / constant / method / export / table / view / macro ...; `hits` is
sorted code, test, other, doc and capped at 10 (`hit_counts` has the totals);
`other_only` counts symbols referenced only from non-code, non-doc files.

### Validation on real history

All 232 study commits (`_study/run_refs.py`, review in `_study/refs_review.csv`):

| | first version | after false-positive fixes |
|---|---|---|
| IE4727 (167 commits) flagged | 5 (all true) | 5 (all true) |
| OutdoorKoi (65 commits) flagged | 2 (both false) | 0 |
| precision (commits) | 5/7 = 71% | 5/5 = 100% |

Flagged: #62 (auth.php functions + the `APP_LOG_FILE`/`APP_VERBOSE_ERRORS` defines — the
defines only after the test-fixture rule), #64, #66, #67, #70; every one of the 30 flagged
symbols is undefined at that commit with live callers. Runtime ~0.5 s median, < 2 s max
per commit (two `git cat-file --batch` calls + one `git grep`).

### Limitations

* Name-based, not semantic: a symbol re-defined anywhere in the same language family
  counts as present, even if callers import it from the old module.
* Removed methods are matched as `.name(`; dynamic dispatch, string callbacks
  (`array_map('fn', ...)`), `getattr`, reflection and templates that build names are not
  seen; neither are references inside strings (except SQL objects).
* Regex extractors miss unusual definition forms (C++ templates, Dart functions and
  methods, JS object-literal methods, PHP methods vs functions are not distinguished).
* Column drops, renamed parameters and changed signatures are out of scope.
* Python modules removed wholesale are found only through their symbols, not
  `import module` lines.

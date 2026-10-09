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
| `path.py::Qualname` | a component. `Class` covers all its methods/nested defs; `Class.method` only that method; `func` covers functions nested in it |

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
2. File matches `allow_globs` → `allowed` (`allow_glob`).
3. Added/untracked file → `allowed` (`new_file`) if `allow_new_files`, else `out_of_scope`.
4. Deleted or renamed file → `out_of_scope`.
5. Adapter-level comparison by qualname (Python):
   * own fingerprint changed → `in_scope` if the qualname or an ancestor is scoped, else `out_of_scope`;
   * component only in new → `in_scope` if scoped; `allowed` (`new_component`) if
     `allow_new_components` and the scope names at least one component in this file; else `out_of_scope`;
   * component only in old (removed) → `in_scope` if scoped, else `out_of_scope`;
   * `<imports>` changed → `allowed` (`import`) if `allow_imports`, else `out_of_scope`;
   * `<module>` (other module-level lines) changed → `out_of_scope`.
6. Fallback-adapter files (non-Python) not scoped → `out_of_scope` at `<file>`.

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
  "allow_globs": ["tests/**", "**/test_*.py", "**/*_test.py", "**/conftest.py"],
  "allow_imports": true,
  "allow_new_files": true,
  "allow_new_components": true
}}
```

`--mode` overrides `mode`.

## Components and tags

Adapter interface (`components.py`): `parse(bytes, path) -> Parsed(components, remainder, error)`.
A `Component` has `qualname`, `kind`, `span` (1-based inclusive lines) plus a generic
`locator` string (for non-line formats, e.g. `Sheet1!A1:C9`), `tags`, `description`,
`fingerprint` (whole extent), `own_fingerprint` (extent minus children),
`ws_fingerprint` (own content, all whitespace removed). Register new adapters with
`components.register(adapter)`; unknown extensions use `FileAdapter` (one `<file>` component).

PythonAdapter: functions, async functions, classes, nested classes, methods
(`HSV.threshold`); spans include decorators. Lines are rstripped and blank lines ignored
before hashing. The contiguous comment block directly above a def (same indentation)
belongs to that component, so editing its tags is a change to it. Duplicate qualnames
(e.g. property getter/setter) become `name#2`; scope matching ignores the suffix.
Pseudo-components: `<imports>` (module-level `import`/`from` statements, multi-line
included) and `<module>` (all other lines outside components).

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
* Code at module level between components (constants, `if __name__ == ...`) can only be
  scoped by declaring the whole file.
* Import changes inside `try:`/`if TYPE_CHECKING:` blocks: the import lines are
  `<imports>`, but the surrounding `try`/`if` lines are `<module>`.
* Text decoding assumes UTF-8 (Python falls back to latin-1); non-UTF-8 non-Python files
  are compared as bytes.
* Gate time is dominated by git process start-up (~0.5 s per run on Windows).
* Only Python has a component adapter today; everything else is whole-file.

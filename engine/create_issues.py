#!/usr/bin/env python3
"""Create GitHub issues, labels and milestones from a description file.

A generalised, data-driven replacement for hand-written issue-creation scripts.
You point it at (1) a local git repository and (2) a file describing the issues,
and it deterministically creates everything the automated runner then works on.

    python engine/create_issues.py --project MyProject --dry-run           # same lookup as run_issues.py
    python engine/create_issues.py --repo /path/to/repo --issues issues.yaml
    python engine/create_issues.py --config issue-automation.config.json   # paths from config
    python engine/create_issues.py --project MyProject --update            # repair existing issues

Design goals
------------
* Deterministic: issues are created in file order, so GitHub issue numbers
  ascend in the same order. The runner uses issue number as execution order.
* Idempotent: an issue whose exact title already exists is skipped, so a
  half-finished run can be re-run safely.
* Positional dependencies: `depends_on: ["#2"]` means "the 2nd issue in this
  file" (an exact title of another issue in the file also works). They are
  rewritten to the real GitHub numbers in the `**Depends on:**` line that
  run_issues.py reads. `--update` repairs that line on issues that already exist.
* Declarative: labels (with colours) and milestones may be declared at the top
  of the file; any others the issues use are created too. Closed milestones
  count as existing and issues are still attached to them.
* Polite: writes are paced (--delay) and GitHub rate-limit rejections are
  retried after waiting (--retries).
* Backend-neutral input: the issues file may be YAML, JSON, or the Markdown
  format produced by this project's issue packs.

Requirements
------------
* Python 3.10+
* Git and the GitHub CLI (`gh`), authenticated (`gh auth login`).
* PyYAML only if you use a .yaml/.yml issues file (JSON and Markdown need nothing).

The script never commits, pushes, branches, or opens PRs. It only creates and
edits issues, labels and milestones through `gh`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from run_issues import (DEFAULT_CONFIG_NAMES, SupervisorError, platform_root,
                        project_config_path)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class IssueSpec:
    title: str
    body: str = ""
    labels: list[str] = field(default_factory=list)
    milestone: str = ""
    assignees: list[str] = field(default_factory=list)
    depends_on: list[int] = field(default_factory=list)   # 1-based file positions


@dataclass
class LabelSpec:
    name: str
    color: str = "cccccc"          # 6-hex, no '#'
    description: str = ""
    declared: bool = True          # False: only used by an issue, never declared


@dataclass
class MilestoneSpec:
    title: str
    description: str = ""


@dataclass
class IssuePlan:
    labels: list[LabelSpec]
    milestones: list[MilestoneSpec]
    issues: list[IssueSpec]
    contract: str = ""             # appended to every issue body


@dataclass
class ExistingIssue:
    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    milestone: str


# ---------------------------------------------------------------------------
# Small subprocess helpers  (shell=False everywhere -> no quoting bugs)
# ---------------------------------------------------------------------------

class CreateError(RuntimeError):
    pass


def run(args: list[str], *, cwd: Optional[Path] = None,
        stdin_text: Optional[str] = None, check: bool = False
        ) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [str(a) for a in args],
        cwd=str(cwd) if cwd else None,
        input=stdin_text,
        text=True, encoding="utf-8", errors="replace",
        capture_output=True,
        shell=False,
    )
    if check and proc.returncode != 0:
        raise CreateError(
            f"command failed ({proc.returncode}): {' '.join(map(str, args))}\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def ensure_tool(name: str) -> None:
    try:
        ok = run([name, "--version"]).returncode == 0
    except FileNotFoundError:
        ok = False
    if not ok:
        raise CreateError(f"`{name}` was not found on PATH.")


def repo_slug(repo: Path) -> str:
    proc = run(["gh", "repo", "view", "--json", "nameWithOwner"], cwd=repo)
    if proc.returncode != 0:
        raise CreateError(
            proc.stderr.strip()
            or "Could not resolve the GitHub repository. Is this a gh-linked repo?"
        )
    return json.loads(proc.stdout)["nameWithOwner"]


# ---------------------------------------------------------------------------
# GitHub client: paced writes + rate-limit retry
# ---------------------------------------------------------------------------

_RATE_LIMIT_MARKERS = ("rate limit", "abuse detection", "http 429", "too many requests")


def is_rate_limited(text: str) -> bool:
    lower = text.lower()
    return any(m in lower for m in _RATE_LIMIT_MARKERS)


class GitHub:
    """Runs `gh` for one repository.

    Writes are spaced at least `delay` seconds apart (GitHub asks for about a
    second between content-creating requests). A call rejected for a primary
    or secondary rate limit is retried up to `retries` times: until the reset
    time when the primary quota is exhausted, else with a growing backoff.
    A rejected request was never applied, so retrying cannot duplicate it."""

    def __init__(self, repo: Path, slug: str, delay: float, retries: int):
        self.repo, self.slug = repo, slug
        self.delay, self.retries = delay, retries
        self._last_write = 0.0

    def call(self, args: list[str], *, stdin_text: Optional[str] = None,
             write: bool = False) -> subprocess.CompletedProcess:
        for attempt in range(self.retries + 1):
            if write:
                gap = self.delay - (time.monotonic() - self._last_write)
                if gap > 0:
                    time.sleep(gap)
            proc = run(["gh", *args], cwd=self.repo, stdin_text=stdin_text)
            if write:
                self._last_write = time.monotonic()
            if proc.returncode == 0 or not is_rate_limited(proc.stderr + proc.stdout):
                return proc
            if attempt == self.retries:
                break
            wait = self._rate_limit_wait(attempt)
            print(f"  rate limited by GitHub; waiting {wait:.0f}s "
                  f"(retry {attempt + 1}/{self.retries})", flush=True)
            time.sleep(wait)
        return proc

    def _rate_limit_wait(self, attempt: int) -> float:
        backoff = min(60.0 * 2 ** attempt, 900.0)
        proc = run(["gh", "api", "rate_limit", "--jq",
                    ".resources.core | [.remaining, .reset] | @tsv"], cwd=self.repo)
        try:
            remaining, reset = (int(x) for x in proc.stdout.split())
        except ValueError:
            return backoff
        if remaining == 0:                  # primary quota spent: wait for its reset
            return max(5.0, reset - time.time() + 5)
        return backoff                      # secondary limit: back off

    def api(self, path: str, *, method: str = "GET", payload: Optional[dict] = None,
            jq: str = "", paginate: bool = False) -> subprocess.CompletedProcess:
        args = ["api", path]
        if method != "GET":
            args += ["-X", method]
        if paginate:
            args.append("--paginate")
        if jq:
            args += ["--jq", jq]
        if payload is not None:
            args += ["--input", "-"]
        return self.call(args, write=method != "GET",
                         stdin_text=json.dumps(payload) if payload is not None else None)

    def api_json_lines(self, path: str, jq: str) -> list[Any]:
        """GET every page of `path`; `jq` must emit one `@json` string per item."""
        proc = self.api(path, jq=jq, paginate=True)
        if proc.returncode != 0:
            raise CreateError(f"GET {path} failed: {proc.stderr.strip()}")
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Loading the issues file  (YAML | JSON | Markdown)
# ---------------------------------------------------------------------------

def load_plan(path: Path) -> IssuePlan:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in (".yaml", ".yml"):
        data = _load_yaml(text, path)
        plan = _plan_from_mapping(data)
    elif suffix == ".json":
        plan = _plan_from_mapping(json.loads(text))
    elif suffix in (".md", ".markdown"):
        plan = _plan_from_markdown(text)
    else:
        # Unknown extension: try JSON, then Markdown.
        try:
            plan = _plan_from_mapping(json.loads(text))
        except json.JSONDecodeError:
            plan = _plan_from_markdown(text)
    return complete_plan(plan)


def complete_plan(plan: IssuePlan) -> IssuePlan:
    """Add every label and milestone the issues use but the file never declared."""
    have_labels = {lab.name.lower() for lab in plan.labels}
    have_milestones = {ms.title for ms in plan.milestones}
    for item in plan.issues:
        for name in item.labels:
            if name.lower() not in have_labels:
                have_labels.add(name.lower())
                plan.labels.append(LabelSpec(name=name, color="ededed", declared=False))
        if item.milestone and item.milestone not in have_milestones:
            have_milestones.add(item.milestone)
            plan.milestones.append(MilestoneSpec(title=item.milestone))
    return plan


def _load_yaml(text: str, path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise CreateError(
            f"{path.name} is YAML but PyYAML is not installed. "
            "Install it (`pip install pyyaml`) or use a .json / .md file."
        ) from exc
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise CreateError(f"{path.name}: top level must be a mapping.")
    return loaded


def resolve_depends(raw: list[Any], idx: int, titles: list[str],
                    aliases: Optional[dict[int, int]] = None) -> list[int]:
    """Turn depends_on entries into 1-based positions in the issues file.

    "#3" / "3" / 3 is the 3rd issue in the file (never a GitHub number); any
    other string must be the exact title of an issue in the file. `aliases`
    maps Markdown heading numbers to positions."""
    where = f"issues[{idx}] ({titles[idx - 1]!r})"
    out: list[int] = []
    for ref in raw:
        s = str(ref).strip()
        m = re.fullmatch(r"#?(\d+)", s)
        if m:
            n = int(m.group(1))
            pos = aliases.get(n, 0) if aliases else n
        elif s in titles:
            pos = titles.index(s) + 1
        else:
            raise CreateError(
                f"{where}: depends_on {s!r} is neither a file position (#N) "
                "nor the exact title of another issue in this file.")
        if not 1 <= pos <= len(titles):
            raise CreateError(
                f"{where}: depends_on {s} points outside the file "
                f"({len(titles)} issues; positions are #1..#{len(titles)}).")
        if pos == idx:
            raise CreateError(f"{where}: an issue cannot depend on itself.")
        out.append(pos)
    return _dedupe(out)


def _plan_from_mapping(data: dict) -> IssuePlan:
    if not isinstance(data, dict):
        raise CreateError("Issues file: top level must be an object/mapping.")

    labels = [
        LabelSpec(
            name=_require(item, "name", "labels[]"),
            color=str(item.get("color", "cccccc")).lstrip("#") or "cccccc",
            description=str(item.get("description", "")),
        )
        for item in data.get("labels", []) or []
    ]
    milestones = [
        MilestoneSpec(
            title=_require(item, "title", "milestones[]"),
            description=str(item.get("description", "")),
        )
        for item in data.get("milestones", []) or []
    ]

    defaults = data.get("defaults", {}) or {}
    default_labels = list(defaults.get("labels", []))
    default_milestone = str(defaults.get("milestone", ""))
    contract = str(defaults.get("contract", ""))

    raw_issues = data.get("issues")
    if not isinstance(raw_issues, list) or not raw_issues:
        raise CreateError("Issues file: `issues` must be a non-empty list.")

    titles: list[str] = []
    for i, item in enumerate(raw_issues, start=1):
        if not isinstance(item, dict):
            raise CreateError(f"issues[{i}] must be an object.")
        titles.append(_require(item, "title", f"issues[{i}]"))

    issues: list[IssueSpec] = []
    for i, item in enumerate(raw_issues, start=1):
        merged_labels = _dedupe(default_labels + list(item.get("labels", [])))
        issues.append(
            IssueSpec(
                title=titles[i - 1],
                body=str(item.get("body", "")),
                labels=[str(x) for x in merged_labels],
                milestone=str(item.get("milestone", default_milestone) or ""),
                assignees=[str(x) for x in item.get("assignees", [])],
                depends_on=resolve_depends(list(item.get("depends_on", []) or []),
                                           i, titles),
            )
        )
    return IssuePlan(labels=labels, milestones=milestones,
                     issues=issues, contract=contract)


_MD_HEADING = re.compile(r"(?m)^##\s+(?:#(\d+)\s*[·:.\-]\s*)?(.+?)\s*$")
_MD_MILELABEL = re.compile(
    r"\*\*Milestone:\*\*\s*(.+?)\s*(?:·\s*)?\*\*Labels:\*\*\s*(.+)")
_MD_LABEL_TOKEN = re.compile(r"`([^`]+)`")
_MD_DEPENDS = re.compile(r"\*\*Depends on:\*\*\s*(.+)")


def _plan_from_markdown(text: str) -> IssuePlan:
    """Parse the '## #N · Title' issue-pack Markdown this project emits.

    Metadata lines understood inside each issue block:
        **Depends on:** #a, #b        (heading numbers within this file)
        **Milestone:** Name · **Labels:** `x` `y`
    Everything else in the block is the body.
    """
    matches = list(_MD_HEADING.finditer(text))
    # Only treat the file as issue-pack Markdown if headings look like issues.
    issue_headings = [m for m in matches if m.group(1)]
    if not issue_headings:
        raise CreateError(
            "Markdown file has no '## #N · Title' issue headings. "
            "Use the JSON or YAML format instead."
        )
    titles = [m.group(2).strip() for m in issue_headings]
    aliases = {int(m.group(1)): pos for pos, m in enumerate(issue_headings, start=1)}
    issues: list[IssueSpec] = []
    labels_seen: dict[str, LabelSpec] = {}

    for i, m in enumerate(issue_headings):
        start = m.end()
        end = issue_headings[i + 1].start() if i + 1 < len(issue_headings) else len(text)
        block = text[start:end]

        milestone = ""
        labels: list[str] = []
        ml = _MD_MILELABEL.search(block)
        if ml:
            milestone = ml.group(1).strip()
            labels = _MD_LABEL_TOKEN.findall(ml.group(2))
        depends: list[str] = []
        dm = _MD_DEPENDS.search(block)
        if dm:
            depends = [d.strip() for d in re.split(r"[,\s]+", dm.group(1)) if d.strip()]

        # Body = block minus the metadata lines and a leading '> Read ...' note.
        body_lines = []
        for line in block.splitlines():
            s = line.strip()
            if s.startswith("**Depends on:**") or s.startswith("**Milestone:**"):
                continue
            if s.startswith("> Read ") and "AGENTS.md" in s:
                continue
            body_lines.append(line)
        body = "\n".join(body_lines).strip()
        # Drop a trailing horizontal rule between issues.
        body = re.sub(r"\n-{3,}\s*$", "", body).strip()

        for name in labels:
            labels_seen.setdefault(name, LabelSpec(name=name))
        issues.append(IssueSpec(title=titles[i], body=body, labels=labels,
                                milestone=milestone,
                                depends_on=resolve_depends(depends, i + 1, titles, aliases)))

    # Markdown has no declaration section; complete_plan() adds the milestones.
    return IssuePlan(labels=list(labels_seen.values()), milestones=[],
                     issues=issues, contract="")


def _require(item: dict, key: str, where: str) -> str:
    if key not in item or item[key] in (None, ""):
        raise CreateError(f"{where}: missing required field '{key}'.")
    return str(item[key])


def _dedupe(seq: list[Any]) -> list[Any]:
    seen: set = set()
    out: list = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------------------
# GitHub state (labels, milestones, existing issues)
# ---------------------------------------------------------------------------

def existing_issues(gh: GitHub) -> dict[str, ExistingIssue]:
    """Every issue (open and closed, PRs excluded) by title; lowest number wins."""
    rows = gh.api_json_lines(
        f"repos/{gh.slug}/issues?state=all&per_page=100",
        ".[] | select(.pull_request == null) | {number, title, body, state, "
        "labels: [.labels[].name], milestone: (.milestone.title // \"\")} | @json")
    out: dict[str, ExistingIssue] = {}
    for row in sorted(rows, key=lambda r: int(r["number"])):
        out.setdefault(row["title"], ExistingIssue(
            number=int(row["number"]), title=row["title"], body=row.get("body") or "",
            state=row["state"], labels=list(row["labels"]), milestone=row["milestone"]))
    return out


def existing_labels(gh: GitHub) -> set[str]:
    rows = gh.api_json_lines(f"repos/{gh.slug}/labels?per_page=100", ".[].name | @json")
    return {str(name).lower() for name in rows}


def existing_milestones(gh: GitHub) -> dict[str, tuple[int, str]]:
    """title -> (number, state), closed milestones included."""
    rows = gh.api_json_lines(f"repos/{gh.slug}/milestones?state=all&per_page=100",
                             ".[] | {title, number, state} | @json")
    return {r["title"]: (int(r["number"]), r["state"]) for r in rows}


def ensure_labels(gh: Optional[GitHub], labels: list[LabelSpec], dry_run: bool) -> None:
    if not labels:
        return
    print("== labels ==")
    have = existing_labels(gh) if gh else None
    for lab in labels:
        exists = have is not None and lab.name.lower() in have
        if exists and not lab.declared:
            print(f"  exists: {lab.name}")
            continue
        verb = "update" if exists else "create"
        note = "" if lab.declared else " (used by an issue, not declared)"
        if dry_run:
            verb = verb if have is not None else "ensure"   # offline: unknown
            print(f"  would {verb} label: {lab.name} (#{lab.color}){note}")
            continue
        # A declared label is forced to its declared colour/description.
        args = ["label", "create", lab.name, "--repo", gh.slug, "--color", lab.color]
        if lab.declared:
            args.append("--force")
        if lab.description:
            args += ["--description", lab.description]
        proc = gh.call(args, write=True)
        if proc.returncode != 0:
            raise CreateError(f"label {lab.name}: {proc.stderr.strip()}")
        print(f"  {verb}d: {lab.name}{note}")


def ensure_milestones(gh: Optional[GitHub], milestones: list[MilestoneSpec],
                      dry_run: bool) -> dict[str, int]:
    """Create missing milestones; return title -> number for every known one."""
    have = existing_milestones(gh) if gh else {}
    numbers = {title: num for title, (num, _state) in have.items()}
    if not milestones:
        return numbers
    print("== milestones ==")
    for ms in milestones:
        if ms.title in have:
            closed = " (closed; issues are still attached to it)" \
                if have[ms.title][1] == "closed" else ""
            print(f"  exists: {ms.title}{closed}")
            continue
        if dry_run:
            print(f"  would create milestone: {ms.title}")
            continue
        payload = {"title": ms.title}
        if ms.description:
            payload["description"] = ms.description
        proc = gh.api(f"repos/{gh.slug}/milestones", method="POST", payload=payload)
        if proc.returncode != 0:
            raise CreateError(f"milestone {ms.title}: {proc.stderr.strip()}")
        numbers[ms.title] = int(json.loads(proc.stdout)["number"])
        print(f"  created: {ms.title}")
    return numbers


# ---------------------------------------------------------------------------
# Issue bodies and dependency lines
# ---------------------------------------------------------------------------

DEPENDS_LINE = re.compile(r"(?m)^[ \t]*\*\*Depends on:\*\*.*$")


def depends_text(item: IssueSpec, numbers: dict[int, int]) -> tuple[str, bool]:
    """Render '#57, #58' from file positions; flag if any position has no number yet."""
    refs = [f"#{numbers[p]}" for p in item.depends_on if p in numbers]
    return ", ".join(refs), len(refs) < len(item.depends_on)


def body_dependencies(body: str) -> list[int]:
    return [int(n) for line in DEPENDS_LINE.findall(body)
            for n in re.findall(r"#(\d+)", line)]


def build_body(item: IssueSpec, contract: str, deps: str) -> str:
    parts = [item.body.rstrip()]
    if deps:
        parts.append("**Depends on:** " + deps)
    if contract.strip():
        parts.append(contract.strip())
    return "\n\n".join(p for p in parts if p).strip() + "\n"


def replace_depends(body: str, deps: str, contract: str) -> str:
    """Rewrite (or add, or drop) the **Depends on:** line of an existing body."""
    line = f"**Depends on:** {deps}" if deps else ""
    if DEPENDS_LINE.search(body):
        new = DEPENDS_LINE.sub(line, body, count=1)
        return re.sub(r"\n{3,}", "\n\n", new) if not line else new
    if not line:
        return body
    c = contract.strip()
    if c and c in body:
        return body.replace(c, f"{line}\n\n{c}", 1)
    return body.rstrip() + f"\n\n{line}\n"


def create_issue(gh: GitHub, item: IssueSpec, body: str,
                 milestone_numbers: dict[str, int]) -> int:
    payload: dict[str, Any] = {"title": item.title, "body": body}
    if item.labels:
        payload["labels"] = item.labels
    if item.assignees:
        payload["assignees"] = item.assignees
    if item.milestone:
        # By number, so closed milestones work as well as open ones.
        payload["milestone"] = milestone_numbers[item.milestone]
    proc = gh.api(f"repos/{gh.slug}/issues", method="POST", payload=payload)
    if proc.returncode != 0:
        raise CreateError(proc.stderr.strip() or f"failed to create issue: {item.title}")
    return int(json.loads(proc.stdout)["number"])


def plan_update(have: ExistingIssue, item: IssueSpec, deps: str,
                contract: str) -> tuple[dict[str, Any], list[str]]:
    """What --update would change on an existing issue: (patch, missing labels)."""
    patch: dict[str, Any] = {}
    have_labels = {x.lower() for x in have.labels}
    missing = [x for x in item.labels if x.lower() not in have_labels]
    if item.milestone and item.milestone != have.milestone:
        patch["milestone"] = item.milestone          # title; swapped for a number later
    wanted = [int(n) for n in re.findall(r"#(\d+)", deps)]
    if body_dependencies(have.body) != wanted:
        patch["body"] = replace_depends(have.body, deps, contract)
    return patch, missing


def describe_update(have: ExistingIssue, patch: dict[str, Any], missing: list[str]) -> str:
    bits = []
    if missing:
        bits.append("labels +" + ",".join(missing))
    if "milestone" in patch:
        bits.append(f"milestone {have.milestone or '-'} -> {patch['milestone']}")
    if "body" in patch:
        old = ", ".join(f"#{n}" for n in body_dependencies(have.body)) or "none"
        new = ", ".join(f"#{n}" for n in body_dependencies(patch["body"])) or "none"
        bits.append(f"depends {old} -> {new}")
    return "; ".join(bits)


def apply_update(gh: GitHub, number: int, patch: dict[str, Any], missing: list[str],
                 milestone_numbers: dict[str, int]) -> None:
    if missing:
        proc = gh.api(f"repos/{gh.slug}/issues/{number}/labels", method="POST",
                      payload={"labels": missing})
        if proc.returncode != 0:
            raise CreateError(f"labels on #{number}: {proc.stderr.strip()}")
    if patch:
        body = dict(patch)
        if "milestone" in body:
            body["milestone"] = milestone_numbers[body["milestone"]]
        proc = gh.api(f"repos/{gh.slug}/issues/{number}", method="PATCH", payload=body)
        if proc.returncode != 0:
            raise CreateError(f"editing #{number}: {proc.stderr.strip()}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _load_config(explicit: str, project: str) -> tuple[dict, Optional[Path]]:
    """--project / --config win; else auto-discover in the current directory."""
    if project:
        if explicit:
            raise CreateError("Use only one of --project / --config.")
        try:
            path = project_config_path(platform_root(), project)
        except SupervisorError as exc:
            raise CreateError(str(exc)) from exc
        return _read_config(path), path.resolve()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.exists():
            raise CreateError(f"--config file not found: {path}")
        return _read_config(path), path
    for name in DEFAULT_CONFIG_NAMES:
        cand = Path.cwd() / name
        if cand.exists():
            return _read_config(cand), cand.resolve()
    return {}, None


def _read_config(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        data = _load_yaml(text, path)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise CreateError(f"{path.name}: top level must be an object.")
    # A `create` section may hold creator-only keys; merge it over the flat keys.
    merged = dict(data)
    merged.update(data.get("create", {}) or {})
    return merged


def connect(repo: Path, delay: float, retries: int) -> GitHub:
    ensure_tool("gh")
    ensure_tool("git")
    if run(["git", "rev-parse", "--show-toplevel"], cwd=repo).returncode != 0:
        raise CreateError(f"{repo} is not inside a git repository.")
    return GitHub(repo, repo_slug(repo), delay, retries)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Create GitHub issues/labels/milestones from a description file. "
                    "--repo and --issues may come from a project config (--project/--config).")
    ap.add_argument("--project", default="",
                    help="Project folder at the repo root (beside v2.py) holding issue-automation.config.*.")
    ap.add_argument("--config", default="",
                    help="Project config file (JSON/YAML) providing repo/issues. "
                         "If omitted, issue-automation.config.* in the current dir is used.")
    ap.add_argument("--repo", default=None,
                    help="Path to the local git repository (config `repo`, else current dir).")
    ap.add_argument("--issues", default=None,
                    help="Issues description file (config `issues`; .yaml/.json/.md).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the plan without changing anything. Reads GitHub when it "
                         "can, so existing issues show as SKIP; works offline otherwise.")
    ap.add_argument("--update", action="store_true",
                    help="For issues that already exist, add missing labels/milestone and "
                         "rewrite the **Depends on:** line from the file.")
    ap.add_argument("--allow-nonempty", action="store_true",
                    help="Skip the note when the repo already has issues.")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="Minimum seconds between write requests (default 1.0).")
    ap.add_argument("--retries", type=int, default=5,
                    help="Retries after a GitHub rate-limit rejection (default 5).")
    args = ap.parse_args()
    if args.delay < 0 or args.retries < 0:
        print("ERROR: --delay and --retries must be 0 or greater.", file=sys.stderr)
        return 1

    try:
        config, config_path = _load_config(args.config, args.project)
    except CreateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if config_path:
        print(f"Using project config: {config_path}")

    # Precedence: CLI flag > config value > hard default. Relative config paths
    # resolve next to the config file, as in run_issues.py.
    base = config_path.parent if config_path else Path.cwd()
    repo_val = args.repo if args.repo is not None else config.get("repo", ".")
    issues_val = args.issues if args.issues is not None else config.get("issues")
    if not issues_val:
        print("ERROR: no issues file given. Pass --issues or set `issues` in the "
              "project config.", file=sys.stderr)
        return 1

    repo = Path(repo_val).expanduser()
    if args.repo is None and not repo.is_absolute():
        repo = base / repo
    repo = repo.resolve()
    issues_path = Path(issues_val).expanduser()
    if not issues_path.is_absolute():
        cand = base / issues_path
        issues_path = cand if cand.exists() else (Path.cwd() / issues_path)
    issues_path = issues_path.resolve()

    gh: Optional[GitHub] = None
    try:
        if not issues_path.exists():
            raise CreateError(f"Issues file not found: {issues_path}")
        plan = load_plan(issues_path)
        try:
            gh = connect(repo, args.delay, args.retries)
        except CreateError as exc:
            if not args.dry_run:
                raise
            # A dry run still works offline; it just cannot see what exists.
            print(f"NOTE: offline dry run ({exc}); existing issues cannot be detected.")
        existing = existing_issues(gh) if gh else {}
    except CreateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Repository:  {gh.slug if gh else repo_val}")
    print(f"Issues file: {issues_path.name}")
    print(f"Planned:     {len(plan.issues)} issues, "
          f"{len(plan.labels)} labels, {len(plan.milestones)} milestones")
    if existing and not args.allow_nonempty and not args.update:
        print(f"NOTE: repo already has {len(existing)} issue(s). Existing titles "
              "are skipped; new numbering continues after them.\n")

    # File position -> GitHub number, known up front for issues that exist.
    numbers = {idx: existing[item.title].number
               for idx, item in enumerate(plan.issues, start=1) if item.title in existing}

    try:
        ensure_labels(gh, plan.labels, args.dry_run)
        milestone_numbers = ensure_milestones(gh, plan.milestones, args.dry_run)

        print("== issues ==")
        created = skipped = updated = 0
        backfill: list[int] = []        # created before a (forward) dependency existed
        for idx, item in enumerate(plan.issues, start=1):
            deps, incomplete = depends_text(item, numbers)
            have = existing.get(item.title)
            if have is not None:
                patch, missing = plan_update(have, item, deps, plan.contract)
                change = describe_update(have, patch, missing)
                if not change:
                    skipped += 1
                    print(f"[{idx:03d}] SKIP existing #{have.number}: {item.title}")
                elif not args.update:
                    skipped += 1
                    print(f"[{idx:03d}] SKIP existing #{have.number}: {item.title}\n"
                          f"        differs ({change}); --update would fix it")
                elif args.dry_run:
                    print(f"[{idx:03d}] WOULD UPDATE #{have.number} ({change}): {item.title}")
                else:
                    apply_update(gh, have.number, patch, missing, milestone_numbers)
                    if incomplete:          # depends on an issue created later on
                        backfill.append(idx)
                    updated += 1
                    print(f"[{idx:03d}] UPDATED #{have.number} ({change}): {item.title}")
                continue

            if args.dry_run:
                lab = ",".join(item.labels) or "-"
                dep_view = ", ".join(f"#{numbers[p]}" if p in numbers else f"file #{p} (new)"
                                     for p in item.depends_on)
                dep_note = f" depends on {dep_view}" if dep_view else ""
                print(f"[{idx:03d}] WOULD CREATE [{item.milestone or '-'} | {lab}]"
                      f"{dep_note}: {item.title}")
                continue
            body = build_body(item, plan.contract, deps)
            number = create_issue(gh, item, body, milestone_numbers)
            numbers[idx] = number
            existing[item.title] = ExistingIssue(number, item.title, body, "open",
                                                 item.labels, item.milestone)
            if incomplete:
                backfill.append(idx)
            created += 1
            print(f"[{idx:03d}] CREATED #{number}: {item.title}")

        for idx in backfill:
            item = plan.issues[idx - 1]
            have = existing[item.title]
            deps, _ = depends_text(item, numbers)
            proc = gh.api(f"repos/{gh.slug}/issues/{have.number}", method="PATCH",
                          payload={"body": replace_depends(have.body, deps, plan.contract)})
            if proc.returncode != 0:
                raise CreateError(f"linking dependencies on #{have.number}: "
                                  f"{proc.stderr.strip()}")
            print(f"[{idx:03d}] LINKED #{have.number} depends on {deps}")
    except CreateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print()
    if args.dry_run:
        print("Dry run complete. Nothing was changed.")
    else:
        print(f"Done. Created {created}, updated {updated}, skipped {skipped}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

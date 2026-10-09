#!/usr/bin/env python3
"""Supervised, unattended coding-agent runner over GitHub issues.

A generalised, cross-platform, agent-agnostic descendant of a Codex-specific
supervisor. One agent worker per issue. The agent edits the working tree and
runs local checks, but it does NOT own git/GitHub mutations. This Python
supervisor independently re-validates the change before committing and commits
one issue at a time. It never pushes or opens a PR unless asked.

Branching defaults to a solo, one-branch workflow (templates/software/AGENTS.branching.md
is the matching policy to paste into a project's agent instructions):
    * branch_mode=auto: if a non-base branch is checked out, continue on it;
      otherwise continue work_branch (default automation/work; local, then
      origin), else create it from origin/<base>.
    * sync_base=merge: a work branch already merged into base is
      fast-forwarded; one that is behind base has base merged in (the merge
      is aborted and the run stops on any conflict).
    * close_on=merge: commits carry "Closes #N" and the issue closes when the
      PR is merged. Issues already committed on the branch are skipped.
    * Failed partial work is kept as a tag deferred/issue-<N>, not a stash or
      a branch; restore it with `git stash apply deferred/issue-<N>`.

Which issue is worked next is chosen by DETERMINISTIC rules:
    * issue-number RANGE            (--min-issue / --max-issue)
    * LABEL filters                 (--labels a,b  ;  --labels-all to require all)
    * MILESTONE filter              (--milestone "M1")
    * explicit EXCLUDES             (--exclude 35,36)
    * ascending issue-number SORT   (lowest eligible open issue first)
    * DEPENDENCIES                  (an issue whose '**Depends on:** #a, #b' issues
                                     are still open and not committed on the branch
                                     is skipped; --ignore-dependencies turns it off)

Everything project-specific is configuration, not code:
    * the agent CLI (codex, claude, aider, or any command) -> --agent / agents.json
    * what "validate" means for this repo                 -> --validate validate.json
    * the safety contract text handed to the agent        -> --contract-file

    # Zero-repeat: keep one folder per project at the repo root (beside v2.py), holding
    # issue-automation.config.json and its sidecar files, then just:
    python engine/run_issues.py --project MyProject     # <repo root>/MyProject/issue-automation.config.*
    python engine/run_issues.py --list-projects
    python engine/run_issues.py --config path/to/issue-automation.config.json

    # Any flag still overrides the config for a one-off:
    python engine/run_issues.py --max-issue 20 --push

    python engine/run_issues.py --self-test          # offline unit checks
    python engine/run_issues.py --resume-latest      # continue an interrupted run

Requirements: Python 3.10+, git, gh (authenticated), and the chosen agent CLI.
PyYAML is optional (only for .yaml config files).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blockers as blk  # noqa: E402  (environment-blocker rules, beside this script)


# ===========================================================================
# Constants / small helpers
# ===========================================================================

APP_DIR_NAME = "issue-runner"
DEFAULT_WORK_BRANCH = "automation/work"   # used by branch_mode auto/reuse when none is configured
RUNS_DIR_NAME = "runs"
STATE_FILE_NAME = "supervisor_state.json"
SUMMARY_FILE_NAME = "SUPERVISOR_SUMMARY.md"

# The agent must end its transcript with a bounded result block. Sentinels are
# overridable per agent; these are the defaults.
DEFAULT_RESULT_BEGIN = "===AGENT_ISSUE_RESULT_BEGIN==="
DEFAULT_RESULT_END = "===AGENT_ISSUE_RESULT_END==="

IS_WINDOWS = os.name == "nt"


class SupervisorError(RuntimeError):
    pass


class NoWorkError(SupervisorError):
    """Nothing to do for this config (not a failure of the harness)."""


# Exit code when --on-usage-limit exit stops the run on a provider limit, so a
# wrapper (session.py) can park and resume instead of this process sleeping.
USAGE_LIMIT_EXIT = 75
RUN_RESULT_PREFIX = "RUN_RESULT "


def emit_run_result(**fields) -> None:
    """One machine-readable line for wrappers; humans can ignore it."""
    print(RUN_RESULT_PREFIX + json.dumps(fields, sort_keys=True), flush=True)


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def print_err(message: str) -> None:
    print(f"\nERROR: {message}", file=sys.stderr, flush=True)


def find_executable(*names: str) -> Optional[str]:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def run_capture(args: Sequence[str], *, cwd: Optional[Path] = None,
                input_text: Optional[str] = None, check: bool = True,
                timeout: Optional[float] = None,
                env: Optional[dict] = None) -> subprocess.CompletedProcess:
    cp = subprocess.run(
        [str(x) for x in args],
        cwd=str(cwd) if cwd else None,
        input=input_text, env=env,
        text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        shell=False, timeout=timeout,
    )
    if check and cp.returncode != 0:
        raise SupervisorError(
            f"Command failed ({cp.returncode}): {' '.join(map(str, args))}\n{cp.stdout}")
    return cp


def git(repo: Path, git_exe: str, *args: str, check: bool = True) -> str:
    return run_capture([git_exe, *args], cwd=repo, check=check).stdout


def gh(repo: Path, gh_exe: str, *args: str, check: bool = True) -> str:
    return run_capture([gh_exe, *args], cwd=repo, check=check).stdout


def get_dirty_status(repo: Path, git_exe: str) -> str:
    return git(repo, git_exe, "status", "--porcelain").rstrip()


def get_changed_files(repo: Path, git_exe: str) -> list[str]:
    tracked = [x.strip() for x in
               git(repo, git_exe, "diff", "--name-only", "HEAD").splitlines() if x.strip()]
    untracked = [x.strip() for x in
                 git(repo, git_exe, "ls-files", "--others", "--exclude-standard").splitlines()
                 if x.strip()]
    return sorted(set(tracked + untracked))


def tail_text(path: Path, max_chars: int = 8000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except FileNotFoundError:
        return ""


def safe_write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def glob_to_regex(pattern: str) -> str:
    """Translate a path glob into an anchored regex.

    Supports gitignore-style globstar: `**/` matches zero or more directories,
    `**` matches anything, `*` matches within a segment, `?` one char. This is
    more reliable than pathlib.PurePath.match, whose `**` support varies by
    Python version and does not let `**/` match zero leading segments.
    """
    out = ["^"]
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    return "".join(out)


def glob_match(path: str, pattern: str) -> bool:
    return re.match(glob_to_regex(pattern), path.replace("\\", "/")) is not None


def load_config_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise SupervisorError(
                f"{path.name} is YAML but PyYAML is not installed; use JSON.") from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise SupervisorError(f"{path.name}: top level must be an object.")
    return data


# ===========================================================================
# Portable runs directory + process-tree kill
# ===========================================================================

def runs_root() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP_DIR_NAME / RUNS_DIR_NAME


def popen_worker(args: Sequence[str], repo: Path):
    """Start the agent so its whole process tree can later be killed."""
    kwargs = dict(
        cwd=str(repo), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        bufsize=1, shell=False,
    )
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True   # own process group for killpg
    return subprocess.Popen([str(a) for a in args], **kwargs)


def kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            taskkill = find_executable("taskkill.exe", "taskkill")
            if taskkill:
                run_capture([taskkill, "/PID", str(proc.pid), "/T", "/F"],
                            check=False, timeout=20)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


# ===========================================================================
# Tool discovery
# ===========================================================================

@dataclass
class Tools:
    git: str
    gh: str
    python: str


def discover_core_tools() -> Tools:
    git_exe = find_executable("git.exe", "git")
    gh_exe = find_executable("gh.exe", "gh")
    missing = [n for n, v in (("git", git_exe), ("gh", gh_exe)) if not v]
    if missing:
        raise SupervisorError("Required executable(s) not on PATH: " + ", ".join(missing))
    return Tools(git=git_exe or "", gh=gh_exe or "", python=sys.executable)


# ===========================================================================
# Agent adapter  (the generalisation of the codex-specific worker command)
# ===========================================================================

@dataclass
class AgentSpec:
    """How to invoke an arbitrary coding-agent CLI.

    argv is a template list. These tokens are substituted at launch:
        {EXE}         resolved agent executable
        {MODEL}       --model value (token removed if no model configured)
        {PROMPT_FILE} path to a temp file holding the prompt
        {LAST_MSG}    path the agent may write its final message to
    prompt_mode:
        "stdin" -> prompt written to the process stdin
        "file"  -> prompt written to {PROMPT_FILE}; reference it in argv
        "arg"   -> prompt substituted for a literal {PROMPT} token in argv
    """
    name: str
    exe_candidates: list[str]
    argv: list[str]
    prompt_mode: str = "stdin"
    result_begin: str = DEFAULT_RESULT_BEGIN
    result_end: str = DEFAULT_RESULT_END
    model: Optional[str] = None       # default model this agent uses
    prompt_prefix: str = ""           # custom text injected near the top of every prompt
    prompt_suffix: str = ""           # custom text injected before the result contract


BUILTIN_AGENTS: dict[str, AgentSpec] = {
    # Tested-shape preset for OpenAI Codex CLI (`codex exec --json`).
    "codex": AgentSpec(
        name="codex",
        exe_candidates=["codex.cmd", "codex.exe", "codex"],
        argv=["{EXE}", "--model", "{MODEL}", "--ask-for-approval", "never",
              "exec", "--sandbox", "workspace-write", "--json",
              "--output-last-message", "{LAST_MSG}", "-"],
        prompt_mode="stdin",
    ),
    # Anthropic Claude Code CLI, headless and unattended: never asks for approval
    # (bypassPermissions), so it can run tests, builds and training itself. Use
    # only on a machine/account dedicated to unattended work.
    "claude": AgentSpec(
        name="claude",
        exe_candidates=["claude.cmd", "claude.exe", "claude"],
        argv=["{EXE}", "--model", "{MODEL}", "--print",
              "--permission-mode", "bypassPermissions"],
        prompt_mode="stdin",
    ),
    # aider, non-interactive single message.
    "aider": AgentSpec(
        name="aider",
        exe_candidates=["aider"],
        argv=["{EXE}", "--model", "{MODEL}", "--yes", "--no-auto-commits",
              "--message-file", "{PROMPT_FILE}"],
        prompt_mode="file",
    ),
}


def _read_prompt_field(data: dict, key: str, base: Optional[Path] = None) -> str:
    """A prompt_prefix/prompt_suffix may be inline text, or {"file": "path"}.
    A relative file resolves next to the agent config (e.g. project_rules.md)."""
    val = data.get(key, "")
    if isinstance(val, dict) and "file" in val:
        f = Path(val["file"])
        if base is not None and not f.is_absolute() and (base / f).exists():
            f = base / f
        return f.read_text(encoding="utf-8")
    return str(val or "")


def resolve_agent(name_or_config: str) -> tuple[AgentSpec, str]:
    """Return (spec, resolved_exe). name_or_config is a builtin name or a config path."""
    if name_or_config in BUILTIN_AGENTS:
        spec = BUILTIN_AGENTS[name_or_config]
    else:
        path = Path(name_or_config)
        if not path.exists():
            raise SupervisorError(
                f"Unknown agent '{name_or_config}'. Use a builtin "
                f"({', '.join(BUILTIN_AGENTS)}) or a path to an agent config file.")
        data = load_config_file(path)
        spec = AgentSpec(
            name=str(data.get("name", path.stem)),
            exe_candidates=list(data["exe_candidates"]),
            argv=list(data["argv"]),
            prompt_mode=str(data.get("prompt_mode", "stdin")),
            result_begin=str(data.get("result_begin", DEFAULT_RESULT_BEGIN)),
            result_end=str(data.get("result_end", DEFAULT_RESULT_END)),
            model=(str(data["model"]) if data.get("model") else None),
            prompt_prefix=_read_prompt_field(data, "prompt_prefix", path.resolve().parent),
            prompt_suffix=_read_prompt_field(data, "prompt_suffix", path.resolve().parent),
        )
    exe = find_executable(*spec.exe_candidates)
    if not exe:
        raise SupervisorError(
            f"Agent '{spec.name}' executable not found (tried: "
            f"{', '.join(spec.exe_candidates)}).")
    return spec, exe


def build_agent_argv(spec: AgentSpec, exe: str, model: Optional[str],
                     prompt_file: Path, last_msg: Path, prompt: str) -> list[str]:
    subs = {"{EXE}": exe, "{MODEL}": model or "",
            "{PROMPT_FILE}": str(prompt_file), "{LAST_MSG}": str(last_msg),
            "{PROMPT}": prompt}
    out: list[str] = []
    skip_next = False
    for i, tok in enumerate(spec.argv):
        if skip_next:
            skip_next = False
            continue
        # Drop a "--model {MODEL}" pair entirely when no model is configured.
        if tok == "--model" and not model:
            skip_next = True
            continue
        out.append(subs.get(tok, tok))
    return out


# ===========================================================================
# Persistent state
# ===========================================================================

@dataclass
class RunState:
    repo_path: str
    repo_name: str
    branch: str
    run_dir: str
    frozen_numbers: list[int]
    frozen_titles: dict[str, str]
    started_at: str
    attempt: int = 0
    retry_counts: dict[str, int] = field(default_factory=dict)
    deferred: dict[str, str] = field(default_factory=dict)
    previous_failure: dict[str, str] = field(default_factory=dict)
    restored_deferred: list[int] = field(default_factory=list)
    current_issue: Optional[int] = None
    status: str = "running"
    base_branch: str = "main"
    # Environment blockers (see blockers.py).
    failure_memory: dict[str, dict] = field(default_factory=dict)    # issue -> last sig/tree/keys
    env_blockers: dict[str, dict] = field(default_factory=dict)      # key -> blocker + issues
    deferred_blockers: dict[str, dict] = field(default_factory=dict) # issue -> blocker that parked it
    requeued: list[int] = field(default_factory=list)                # re-queued once after a fix

    @property
    def state_path(self) -> Path:
        return Path(self.run_dir) / STATE_FILE_NAME

    def save(self) -> None:
        safe_write_json(self.state_path, asdict(self))

    @classmethod
    def load(cls, path: Path) -> "RunState":
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def resume_or_new_target(root: Path, repo: Path, git_exe: str) -> tuple[Optional[Path], str]:
    """For --resume-or-new: look at the MOST RECENT run for this repo only.
    Resume it if it is unfinished and its branch still exists; otherwise start
    a new run. Older unfinished runs (e.g. killed runs whose per-run branch was
    deleted long ago) are never dug up."""
    latest: Optional[tuple[float, Path, dict]] = None
    for sp in (root.glob(f"*/{STATE_FILE_NAME}") if root.exists() else []):
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
            if Path(data.get("repo_path", "")).resolve() != repo.resolve():
                continue
            mtime = sp.stat().st_mtime
        except Exception:
            continue
        if latest is None or mtime > latest[0]:
            latest = (mtime, sp, data)
    if latest is None:
        return None, "no earlier run for this repo; starting a new run"
    _, sp, data = latest
    status, branch = data.get("status"), str(data.get("branch", ""))
    if status not in {"running", "stopped", "interrupted"}:
        return None, f"latest run {sp.parent.name} is {status}; starting a new run"
    exists = any(run_capture([git_exe, "rev-parse", "--verify", "-q", ref], cwd=repo,
                             check=False).returncode == 0
                 for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"))
    if not exists:
        data["status"] = "abandoned"
        safe_write_json(sp, data)
        return None, (f"latest run {sp.parent.name} was on {branch}, which no longer "
                      "exists; marked it abandoned and starting a new run")
    return sp, f"continuing unfinished run {sp.parent.name} on {branch}"


def find_latest_resumable_state(root: Path, repo: Optional[Path] = None,
                                branch: str = "") -> Path:
    candidates: list[tuple[float, Path]] = []
    if root.exists():
        for sp in root.glob(f"*/{STATE_FILE_NAME}"):
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
                same_repo = (repo is None or
                             Path(data.get("repo_path", "")).resolve() == repo.resolve())
                if (same_repo and (not branch or data.get("branch") == branch)
                        and (branch or data.get("status") in
                             {"running", "stopped", "interrupted"})):
                    candidates.append((sp.stat().st_mtime, sp))
            except Exception:
                continue
    if not candidates:
        raise SupervisorError(f"No resumable run found under {root} for this repository/branch")
    candidates.sort(reverse=True)
    if branch:
        latest = json.loads(candidates[0][1].read_text(encoding="utf-8"))
        if latest.get("status") not in {"running", "stopped", "interrupted"}:
            raise SupervisorError(
                f"Latest run for {branch} is finished. Edit the config and start a new run.")
    return candidates[0][1]


# ===========================================================================
# GitHub issue queue  (RANGE + LABELS + MILESTONE filters, ascending sort)
# ===========================================================================

@dataclass(frozen=True)
class Issue:
    number: int
    title: str


@dataclass
class Filters:
    min_issue: int
    max_issue: int
    milestone: str
    labels: list[str]
    labels_all: bool
    exclude: set[int]


def get_repo_name(repo: Path, tools: Tools) -> str:
    out = gh(repo, tools.gh, "repo", "view", "--json", "nameWithOwner",
             "-q", ".nameWithOwner").strip()
    if not out:
        raise SupervisorError("Could not determine GitHub repository.")
    return out


def _issue_matches(item: dict, f: Filters) -> bool:
    number = int(item["number"])
    if number in f.exclude:
        return False
    if f.min_issue > 0 and number < f.min_issue:
        return False
    if f.max_issue > 0 and number > f.max_issue:
        return False
    if f.milestone:
        ms = (item.get("milestone") or {}).get("title") or ""
        if ms != f.milestone:
            return False
    if f.labels:
        have = {str(x.get("name", "")) for x in item.get("labels", [])}
        wanted = set(f.labels)
        if f.labels_all:
            if not wanted.issubset(have):
                return False
        elif have.isdisjoint(wanted):
            return False
    return True


def list_matching_issues(repo: Path, tools: Tools, repo_name: str,
                         f: Filters, limit: int) -> list[Issue]:
    raw = gh(repo, tools.gh, "issue", "list", "--repo", repo_name,
             "--state", "open", "--limit", str(limit),
             "--json", "number,title,milestone,labels")
    data = json.loads(raw or "[]")
    result = [Issue(int(x["number"]), str(x["title"]))
              for x in data if _issue_matches(x, f)]
    return sorted(result, key=lambda x: x.number)   # ascending = execution order


def get_open_issue_bodies(repo: Path, tools: Tools, repo_name: str) -> dict[int, str]:
    """Every open issue's body by number (the bodies carry dependency lines)."""
    raw = gh(repo, tools.gh, "issue", "list", "--repo", repo_name,
             "--state", "open", "--limit", "1000", "--json", "number,body")
    return {int(x["number"]): str(x.get("body") or "") for x in json.loads(raw or "[]")}


def get_open_frozen_issues(state: RunState, open_issues: dict[int, str],
                           done: set[int], exclude: set[int]) -> list[Issue]:
    # With close_on=merge an issue stays open until its PR merges; `done`
    # (committed on the branch) skips it here.
    return [Issue(n, state.frozen_titles[str(n)])
            for n in state.frozen_numbers
            if n in open_issues and n not in exclude and n not in done]


DEPENDS_RE = re.compile(r"(?im)^[ \t]*\*\*Depends on:\*\*(.*)$")


def parse_dependencies(body: str) -> list[int]:
    """Issue numbers from the '**Depends on:** #a, #b' line create_issues.py writes."""
    return [int(n) for line in DEPENDS_RE.findall(body) for n in re.findall(r"#(\d+)", line)]


def unmet_dependencies(issues: list[Issue], open_issues: dict[int, str],
                       done: set[int]) -> dict[int, list[int]]:
    """Issue number -> dependencies still open and not yet committed on the branch.

    A dependency committed on the work branch counts as met: with close_on=merge
    it stays open until the PR merges, but its code is already here."""
    blocked: dict[int, list[int]] = {}
    for issue in issues:
        waiting = [d for d in parse_dependencies(open_issues.get(issue.number, ""))
                   if d != issue.number and d in open_issues and d not in done]
        if waiting:
            blocked[issue.number] = waiting
    return blocked


def get_issue_text(repo: Path, tools: Tools, repo_name: str, number: int) -> str:
    item = json.loads(gh(repo, tools.gh, "issue", "view", str(number),
                         "--repo", repo_name, "--json",
                         "number,title,body,labels,milestone"))
    labels = ", ".join(str(x.get("name", "")) for x in item.get("labels", []))
    ms = (item.get("milestone") or {}).get("title") or ""
    return (f"# {item['number']} {item['title']}\n\n{item.get('body') or ''}\n\n"
            f"Labels: {labels}\nMilestone: {ms}\n")


# ===========================================================================
# Prompt + agent result protocol
# ===========================================================================

DEFAULT_CONTRACT = """\
Operating contract for the unattended coding agent:
- Implement EXACTLY the one issue described. Make only the change it specifies.
- Repository reality wins over stale specs, comments, or screenshots. Inspect
  the existing code before editing; prefer the current architecture over a
  speculative rewrite.
- Do not perform opportunistic refactors, unrelated renames, dependency bumps,
  or architecture migrations.
- Reuse existing modules, utilities, and conventions where practical.
- Tests are required. Run focused tests for what you changed plus the repo's
  existing lint/build/test for the areas you touched.
- Verify behaviour programmatically; do not claim success without evidence.
- Do not weaken, skip, or delete tests to make a check pass.
"""


def make_prompt(repo_name: str, issue: Issue, issue_text: str,
                contract: str, spec: AgentSpec,
                dirty_status: str, previous_failure: str) -> str:
    dirty = (f"There are uncommitted changes from a previous attempt:\n\n"
             f"{dirty_status}\n\nInspect them; preserve valid partial work for "
             f"THIS issue. Do not blindly reset, clean, or discard.\n"
             if dirty_status else "The working tree is clean at the start.\n")
    previous = (f"PREVIOUS ATTEMPT / VALIDATION FEEDBACK:\n\n{previous_failure}\n\n"
                f"Address this directly; do not repeat the failing approach.\n"
                if previous_failure else "")
    prefix = (f"================ PROJECT INSTRUCTIONS ================\n"
              f"{spec.prompt_prefix.strip()}\n\n" if spec.prompt_prefix.strip() else "")
    suffix = (f"================ ADDITIONAL INSTRUCTIONS ================\n"
              f"{spec.prompt_suffix.strip()}\n\n" if spec.prompt_suffix.strip() else "")
    return f"""\
You are a coding agent working NON-INTERACTIVELY in repository {repo_name}.

You are implementing EXACTLY ONE issue:
#{issue.number} {issue.title}

{prefix}================ CONTEXT READING (do this first) ================
Read, if present and in this order: AGENTS.md, CLAUDE.md, README, and any spec
or test files directly relevant to this issue. Treat repository-wide agent
instructions as binding regardless of which tool they name.

================ ISSUE ================
{issue_text}

================ CONTRACT ================
{contract.strip()}

================ OPERATING BOUNDARY ================
The Python supervisor owns ALL git and GitHub mutations. You MUST NOT:
- create/switch/delete branches; git add/commit/reset --hard/clean/stash/rebase/
  merge/push/pull/fetch
- create/close/edit GitHub issues or pull requests, or use `gh` for network ops
- modify files outside this repository
You MAY: read git state, read/edit repo files for this issue, run tests, linters,
builds, formatters, type checks.

{dirty}
{previous}
{suffix}================ FINAL RESPONSE CONTRACT ================
At the VERY END, output exactly one bounded block:

{spec.result_begin}
STATUS: COMPLETE | BLOCKED | INCOMPLETE
VALIDATION: PASS | FAIL
BLOCKER: NONE | {' | '.join(blk.DECLARABLE_KINDS)}
NEEDS: what a human must install or configure, or NONE
ISSUE: #{issue.number}
SUMMARY: one concise line
TESTS: concise semicolon-separated checks actually run
NOTES: concise remaining risk/conflict, or NONE
{spec.result_end}

Use STATUS: COMPLETE and VALIDATION: PASS only when the work is truly ready for
the supervisor's independent validation and commit.

Use STATUS: BLOCKED only when the issue cannot be finished without something
outside this repository that you may not install or configure: a system tool,
SDK, runtime version, credentials, a running service, other hardware or OS.
Name it in BLOCKER and NEEDS (e.g. "BLOCKER: MISSING_SDK", "NEEDS: Android SDK
with ANDROID_HOME set"). The supervisor then parks the issue for a human
instead of retrying it. Project dependencies inside the repo (npm install, a
repo-local venv) are not blockers; global/system installs are out of bounds.
"""


@dataclass
class AgentResult:
    status: str
    validation: str
    raw: str
    blocker: str = ""
    needs: str = ""
    summary: str = ""


def parse_agent_result(text: str, spec: AgentSpec) -> AgentResult:
    starts = [m.start() for m in re.finditer(re.escape(spec.result_begin), text)]
    if starts:
        block = text[starts[-1]:]
        end = block.find(spec.result_end)
        if end >= 0:
            block = block[: end + len(spec.result_end)]
    else:
        block = text
    sm = re.search(r"(?im)^\s*STATUS:\s*(COMPLETE|BLOCKED|INCOMPLETE)\s*$", block)
    vm = re.search(r"(?im)^\s*VALIDATION:\s*(PASS|FAIL)\s*$", block)
    bm = re.search(r"(?im)^\s*BLOCKER:\s*([A-Z_]+)\s*$", block)

    def field_(name: str) -> str:
        m = re.search(rf"(?im)^\s*{name}:\s*(.+?)\s*$", block)
        return m.group(1) if m else ""

    return AgentResult(status=sm.group(1).upper() if sm else "",
                       validation=vm.group(1).upper() if vm else "", raw=block,
                       blocker=bm.group(1).upper() if bm else "",
                       needs=field_("NEEDS"), summary=field_("SUMMARY"))


# ===========================================================================
# Worker process / streaming with watchdog
# ===========================================================================

@dataclass
class WorkerOutcome:
    returncode: int
    reason: str
    output_text: str


def _reader_thread(pipe, q: "queue.Queue[Optional[str]]", log_file) -> None:
    try:
        for line in iter(pipe.readline, ""):
            log_file.write(line)
            log_file.flush()
            q.put(line)
    finally:
        q.put(None)


def humanize_event(line: str) -> Optional[str]:
    line = line.rstrip()
    if not line:
        return None
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        return line if len(line) < 900 else None
    if not isinstance(evt, dict):
        return None
    typ = evt.get("type")
    if typ == "item.completed":
        item = evt.get("item") or {}
        if item.get("type") == "agent_message" and item.get("text"):
            return str(item["text"])
        if item.get("type") == "command_execution" and item.get("command"):
            return f"  [command] {item['command']}"
    if typ == "error":
        return f"[agent error] {evt.get('message', evt)}"
    return None


def run_agent_worker(*, repo: Path, spec: AgentSpec, exe: str, model: Optional[str],
                     prompt: str, log_path: Path, prompt_path: Path, last_path: Path,
                     stall_minutes: int, max_session_minutes: int,
                     heartbeat_seconds: int, issue: Issue, attempt: int) -> WorkerOutcome:
    prompt_path.write_text(prompt, encoding="utf-8")
    argv = build_agent_argv(spec, exe, model, prompt_path, last_path, prompt)

    proc = popen_worker(argv, repo)
    assert proc.stdin is not None and proc.stdout is not None

    if spec.prompt_mode == "stdin":
        proc.stdin.write(prompt)
    proc.stdin.close()

    q: "queue.Queue[Optional[str]]" = queue.Queue()
    start = last_output = time.monotonic()
    last_heartbeat = 0.0
    reason = "running"
    collected: list[str] = []

    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        reader = threading.Thread(target=_reader_thread,
                                  args=(proc.stdout, q, log_file), daemon=True)
        reader.start()
        print(f"[{now_local():%H:%M:%S}] attempt {attempt} | issue #{issue.number} "
              f"| {spec.name} PID {proc.pid}", flush=True)

        reader_finished = False
        while proc.poll() is None:
            try:
                item = q.get(timeout=1.0)
                if item is None:
                    reader_finished = True
                else:
                    collected.append(item)
                    last_output = time.monotonic()
                    pretty = humanize_event(item)
                    if pretty:
                        print(pretty, flush=True)
            except queue.Empty:
                pass

            elapsed = time.monotonic() - start
            silent = time.monotonic() - last_output
            if elapsed >= max_session_minutes * 60:
                reason = f"worker exceeded {max_session_minutes} min session cap"
                print(f"Watchdog: {reason}; killing worker.", flush=True)
                kill_process_tree(proc)
                break
            if elapsed >= 60 and silent >= stall_minutes * 60:
                reason = f"no output for {stall_minutes} minutes"
                print(f"Watchdog: {reason}; killing worker.", flush=True)
                kill_process_tree(proc)
                break
            if elapsed - last_heartbeat >= heartbeat_seconds:
                print(f"[{now_local():%H:%M:%S}] issue #{issue.number} | "
                      f"runtime {dt.timedelta(seconds=int(elapsed))} | "
                      f"last output {int(silent)}s ago", flush=True)
                last_heartbeat = elapsed

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            proc.wait(timeout=10)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                item = q.get_nowait()
            except queue.Empty:
                if reader_finished:
                    break
                time.sleep(0.05)
                continue
            if item is None:
                reader_finished = True
            else:
                collected.append(item)

    rc = proc.returncode if proc.returncode is not None else -999
    if reason == "running":
        reason = "worker exited normally" if rc == 0 else f"worker exited with code {rc}"
    return WorkerOutcome(returncode=rc, reason=reason, output_text="".join(collected))


# ===========================================================================
# Usage-limit detection + wait-for-reset
# ===========================================================================

def _looks_like_blocking_usage_message(text: str) -> bool:
    lower = text.lower().strip()
    if not lower:
        return False
    if "approaching" in lower and "limit" in lower and not any(
            x in lower for x in ("reached", "exceeded", "too many requests")):
        return False
    explicit = (
        "reached your usage limit", "reached your rate limit",
        "usage limit reached", "rate limit reached",
        "usage limit exceeded", "rate limit exceeded",
        "quota exceeded", "too many requests",
        "http 429", "status 429", "error 429",
        "you've hit your limit", "you have hit your limit", "hit your usage limit",
    )
    return any(p in lower for p in explicit)


def detect_usage_limit(text: str, *, worker_returncode: Optional[int] = None) -> bool:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(evt, dict) and evt.get("type") == "error":
            if _looks_like_blocking_usage_message(json.dumps(evt, ensure_ascii=False)):
                return True
    if worker_returncode is not None and worker_returncode != 0:
        return _looks_like_blocking_usage_message(text)
    return False


def usage_limit_lines(text: str) -> str:
    """Only the lines that carry the blocking usage message. Reset times must be
    parsed from these, never from the whole worker log: a log full of
    `db_reset.php` and stray digits otherwise yields a bogus clock time."""
    lines = [ln for ln in text.splitlines() if _looks_like_blocking_usage_message(ln)]
    return "\n".join(lines) if lines else text


def parse_reset_datetime(text: str, base: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    if base is None:
        base = now_local()
    text = usage_limit_lines(text)
    # Claude Code headless: "Claude AI usage limit reached|<unix seconds>"
    m = re.search(r"(?i)limit reached\|(\d{10})\b", text)
    if m:
        return dt.datetime.fromtimestamp(int(m.group(1)), tz=base.tzinfo or dt.timezone.utc)
    month = (r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
             r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
    # `reset` must be its own word: never the tail of `db_reset` or `reset.php`.
    m = re.search(rf"(?i)(?<![\w./\\-])resets?\b.{{0,40}}?{month}\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?"
                  rf".{{0,25}}?(?:at\s+)?(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm)?", text)
    if m:
        day = int(m.group(2)); year = int(m.group(3)) if m.group(3) else base.year
        hour = int(m.group(4)); minute = int(m.group(5) or 0); ampm = (m.group(6) or "").lower()
        mo = dt.datetime.strptime(m.group(1)[:3].title(), "%b").month
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        cand = dt.datetime(year, mo, day, hour, minute, tzinfo=base.tzinfo)
        if not m.group(3) and cand < base - dt.timedelta(days=2):
            cand = cand.replace(year=year + 1)
        return cand
    # Codex: "... try again at 6:22 PM."
    m = re.search(r"(?i)\btry again at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text)
    if not m:
        m = re.search(r"(?i)(?<![\w./\\-])resets?\b.{0,30}?(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
                      text)
    if m:
        hour = int(m.group(1)); minute = int(m.group(2) or 0); ampm = (m.group(3) or "").lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= base:
            cand += dt.timedelta(days=1)
        return cand
    return None


def wait_for_usage_reset(*, text: str, fallback_minutes: int, safety_seconds: int,
                         stop_event: threading.Event) -> bool:
    parsed = parse_reset_datetime(text)
    if parsed:
        wake = parsed + dt.timedelta(seconds=safety_seconds)
        print(f"Usage limit detected. Reset ~{parsed:%Y-%m-%d %H:%M:%S}; "
              f"waiting until {wake:%H:%M:%S}.", flush=True)
    else:
        wake = now_local() + dt.timedelta(minutes=fallback_minutes)
        print(f"Usage limit detected; reset time not parseable. "
              f"Waiting {fallback_minutes} min.", flush=True)
    while True:
        if stop_event.is_set():
            return False
        remaining = (wake - now_local()).total_seconds()
        if remaining <= 0:
            return True
        print(f"[{now_local():%H:%M:%S}] waiting for reset | "
              f"{dt.timedelta(seconds=int(remaining))} remaining", flush=True)
        stop_event.wait(timeout=min(60.0, max(1.0, remaining)))


# ===========================================================================
# Independent validation  (config-driven)
# ===========================================================================

@dataclass
class ValidationConfig:
    max_file_mb: int = 10
    deny_globs: list[str] = field(default_factory=lambda: [
        "**/.env", "**/.env.*", "**/*.pem", "**/*.p12", "**/*.pfx",
        "**/id_rsa", "**/id_ed25519", "**/credentials", "**/secrets*.json"])
    allow_globs: list[str] = field(default_factory=lambda: [
        "**/.env.example", "**/.env.sample", "**/.env.template"])
    always: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)


def default_validation_config() -> ValidationConfig:
    """A conservative, language-agnostic default when no config is supplied."""
    return ValidationConfig(
        always=[{"label": "whitespace/conflict-marker check",
                 "argv": ["git", "diff", "--check"]}],
        rules=[{"when_touched": ["**/*.py"],
                "commands": [{"label": "python compileall (changed dirs)",
                              "argv": ["__PY__", "-m", "compileall", "-q", "__CHANGED_DIRS__"]}]}],
    )


def load_validation_config(path: Optional[Path]) -> ValidationConfig:
    if path is None:
        return default_validation_config()
    data = load_config_file(path)
    cfg = default_validation_config()
    susp = data.get("suspicious", {}) or {}
    return ValidationConfig(
        max_file_mb=int(susp.get("max_file_mb", cfg.max_file_mb)),
        deny_globs=list(susp.get("deny_globs", cfg.deny_globs)),
        allow_globs=list(susp.get("allow_globs", cfg.allow_globs)),
        always=list(data.get("always", [])),
        rules=list(data.get("rules", [])),
    )


def suspicious_changed_files(repo: Path, files: list[str], cfg: ValidationConfig) -> list[str]:
    bad: list[str] = []
    for rel in files:
        p = Path(rel.replace("\\", "/"))
        rel_norm = rel.replace("\\", "/")
        if any(glob_match(rel_norm, g) for g in cfg.allow_globs):
            pass
        elif any(glob_match(rel_norm, g) for g in cfg.deny_globs):
            bad.append(rel)
            continue
        full = repo / rel
        if full.is_file():
            try:
                if full.stat().st_size > cfg.max_file_mb * 1024 * 1024:
                    bad.append(f"{rel} (> {cfg.max_file_mb} MB)")
            except OSError:
                pass
    return bad


@dataclass
class ValidationResult:
    passed: bool
    details: str


def _touched(files: list[str], globs: list[str]) -> bool:
    norm = [f.replace("\\", "/") for f in files]
    return any(glob_match(f, g) for f in norm for g in globs)


def _changed_dirs(files: list[str]) -> list[str]:
    # Map every changed file to its directory; repo-root files map to ".".
    # Crucially we KEEP "." so a top-level file is always validated, even when
    # other changes live in subdirectories.
    dirs = set()
    for f in files:
        parent = str(Path(f.replace("\\", "/")).parent)
        dirs.add("." if parent in ("", ".") else parent)
    return sorted(dirs) or ["."]


@dataclass
class ValidationContext:
    """What a gate command may know about the attempt it is judging.

    Path tokens (substituted anywhere inside an argv string, so
    "__HARNESS__/adversary.py" works):
        __HARNESS__     engine/, the directory of run_issues.py (adversary.py, ml_gate.py live here)
        __CONFIG_DIR__  directory of the --validate file (project sidecars live here)
        __RUN_DIR__     this run's log directory (outside the repo)
        __PROJECT_CONFIG__  the project config file (its "ml"/"adversary" sections)
    The same facts, plus the issue, are exported as AGENT_* environment
    variables (see env()). Commands still run with cwd = the repo.
    """
    harness_dir: str = ""
    config_dir: str = ""
    run_dir: str = ""
    project_config: str = ""
    issue_number: int = 0
    issue_title: str = ""
    issue_file: str = ""
    agent_result_file: str = ""
    attempt: int = 0
    attempt_started: float = 0.0

    def tokens(self) -> dict[str, str]:
        return {"__HARNESS__": self.harness_dir, "__CONFIG_DIR__": self.config_dir,
                "__RUN_DIR__": self.run_dir, "__PROJECT_CONFIG__": self.project_config}

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")   # gate output is decoded as UTF-8
        env.update({
            "AGENT_HARNESS_DIR": self.harness_dir, "AGENT_CONFIG_DIR": self.config_dir,
            "AGENT_RUN_DIR": self.run_dir, "AGENT_ISSUE_NUMBER": str(self.issue_number),
            "AGENT_ISSUE_TITLE": self.issue_title, "AGENT_ISSUE_FILE": self.issue_file,
            "AGENT_RESULT_FILE": self.agent_result_file, "AGENT_ATTEMPT": str(self.attempt),
            "AGENT_ATTEMPT_STARTED": f"{self.attempt_started:.3f}",
        })
        return env


def _expand_argv(argv: list[str], tools: Tools, changed_dirs: list[str],
                 ctx: Optional[ValidationContext] = None) -> list[list[str]]:
    """Expand placeholder tokens. __CHANGED_DIRS__ fans out to one command."""
    subs = {k: v for k, v in (ctx.tokens() if ctx else {}).items() if v}

    def sub(t: str) -> str:
        if t == "__PY__":
            return tools.python
        for k, v in subs.items():
            t = t.replace(k, v)
        return t

    argv = [sub(t) for t in argv]
    if "__CHANGED_DIRS__" in argv:
        return [[d if t == "__CHANGED_DIRS__" else t for t in argv] for d in changed_dirs]
    return [argv]


def run_validation_command(label: str, argv: Sequence[str], *, cwd: Path,
                           env: Optional[dict] = None,
                           timeout_minutes: Optional[float] = None) -> tuple[bool, str]:
    print(f"VALIDATE: {label}", flush=True)
    try:
        cp = run_capture(argv, cwd=cwd, check=False, env=env,
                         timeout=timeout_minutes * 60 if timeout_minutes else None)
    except subprocess.TimeoutExpired:
        msg = f"FAIL: {label} (timed out after {timeout_minutes} min)"
        print(msg, flush=True)
        return False, msg
    except FileNotFoundError:
        # A validation command referenced a tool that isn't on PATH (e.g. `bash`
        # on Windows). Fail the gate cleanly instead of crashing the supervisor.
        exe = str(argv[0]) if argv else "?"
        msg = (f"FAIL: {label} (executable not found: {exe!r})\n"
               f"'{exe}' is not on PATH for this user. Fix the command in your "
               f"--validate config, or install/expose that tool.")
        print(msg, flush=True)
        return False, msg
    out = cp.stdout.rstrip()
    for line in out.splitlines()[-40:]:
        print(line, flush=True)
    ok = cp.returncode == 0
    return ok, f"{'PASS' if ok else 'FAIL'}: {label} (exit {cp.returncode})\n{out}"


def independent_validation(repo: Path, tools: Tools, changed_files: list[str],
                           cfg: ValidationConfig,
                           ctx: Optional[ValidationContext] = None) -> ValidationResult:
    """Run every gate command. A command marked "skip_if_failed": true (e.g. a
    paid LLM reviewer) runs AFTER all other commands, always and rules alike,
    and is skipped if any of them failed, so cheap deterministic checks fail
    fast. "timeout_minutes" bounds one command. Each command sees
    AGENT_GATES_PASSED, a JSON array of the labels that passed before it."""
    details: list[str] = []
    passed = True
    changed_dirs = _changed_dirs(changed_files)
    base_env = ctx.env() if ctx else dict(os.environ)
    gates_passed: list[str] = []
    deferred: list[tuple[dict, Path]] = []

    def run_entry(cmd: dict, cwd: Path, final: bool = False) -> None:
        nonlocal passed
        if cmd.get("skip_if_failed") and not final:
            deferred.append((cmd, cwd))
            return
        label = cmd.get("label", " ".join(map(str, cmd["argv"])))
        if cmd.get("skip_if_failed") and not passed:
            print(f"VALIDATE: {label} -- skipped (an earlier gate failed)", flush=True)
            details.append(f"SKIPPED: {label} (an earlier gate failed)")
            return
        env = dict(base_env, AGENT_GATES_PASSED=json.dumps(gates_passed))
        all_ok = True
        for argv in _expand_argv(list(cmd["argv"]), tools, changed_dirs, ctx):
            argv = [tools.git if a == "git" else a for a in argv]
            ok, text = run_validation_command(label, argv, cwd=cwd, env=env,
                                              timeout_minutes=cmd.get("timeout_minutes"))
            all_ok &= ok
            details.append(text)
        passed &= all_ok
        if all_ok:
            gates_passed.append(label)

    for entry in cfg.always:
        run_entry(entry, repo)

    for rule in cfg.rules:
        if not _touched(changed_files, list(rule.get("when_touched", []))):
            continue
        for cmd in rule.get("commands", []):
            cwd = repo / cmd["cwd"] if cmd.get("cwd") else repo
            if not cwd.exists():
                passed = False
                details.append(f"FAIL: {cmd.get('label','?')} — cwd missing: {cwd}")
                continue
            run_entry(cmd, cwd)

    for cmd, cwd in deferred:
        run_entry(cmd, cwd, final=True)

    return ValidationResult(passed=passed, details="\n\n".join(details))


# ===========================================================================
# Commit / close / stash
# ===========================================================================

def close_issue_with_retry(repo: Path, tools: Tools, repo_name: str,
                           number: int, comment: str, attempts: int = 3) -> bool:
    for i in range(1, attempts + 1):
        cp = run_capture([tools.gh, "issue", "close", str(number), "--repo",
                          repo_name, "--comment", comment], cwd=repo, check=False)
        if cp.returncode == 0:
            return True
        print(f"issue close attempt {i}/{attempts} failed:\n{cp.stdout}", flush=True)
        if i < attempts:
            time.sleep(5 * i)
    return False


def defer_failed_work(repo: Path, tools: Tools, number: int) -> Optional[str]:
    """Park partial work under tag deferred/issue-<N> so the tree is clean again.

    A tag (not a stash or a branch) keeps one findable entry per issue and never
    clutters the branch list. Restore with `git stash apply deferred/issue-<N>`."""
    if not get_dirty_status(repo, tools.git):
        return None
    msg = f"issue-runner deferred issue #{number} at {now_local():%Y-%m-%d %H:%M:%S}"
    git(repo, tools.git, "stash", "push", "-u", "-m", msg)
    if get_dirty_status(repo, tools.git):
        raise SupervisorError(
            f"git stash left a dirty tree after deferring #{number}. Stopping.")
    tag = f"deferred/issue-{number}"
    git(repo, tools.git, "tag", "-f", tag, "stash@{0}")
    git(repo, tools.git, "stash", "drop", "stash@{0}")
    return tag


def restore_deferred_work(repo: Path, tools: Tools, number: int) -> Optional[str]:
    """Bring back an earlier run's parked work for this issue (tag
    deferred/issue-<N>) before its first attempt in this run, so a rerun
    continues instead of starting over. Only into a clean tree; if the stash
    no longer applies, the tree is returned to that clean state and the agent
    starts fresh. The tag itself is kept."""
    tag = f"deferred/issue-{number}"
    if (get_dirty_status(repo, tools.git)
            or run_capture([tools.git, "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
                           cwd=repo, check=False).returncode != 0):
        return None
    cp = run_capture([tools.git, "stash", "apply", tag], cwd=repo, check=False)
    if cp.returncode != 0:
        # The tree was clean before, so this only discards what the apply wrote.
        run_capture([tools.git, "reset", "--hard", "HEAD"], cwd=repo, check=False)
        run_capture([tools.git, "clean", "-fd"], cwd=repo, check=False)
        return None
    return (f"Restored partial work for #{number} from an earlier run (tag {tag}); it is "
            f"now uncommitted in the tree. It was not validated: check it, keep what is "
            f"right, and finish the issue.")


def worktree_fingerprint(repo: Path, git_exe: str) -> str:
    """Hash of the uncommitted changes, to tell 'tried something new' from 'no change'."""
    h = hashlib.sha1(git(repo, git_exe, "diff", "HEAD", "--binary", check=False)
                     .encode("utf-8", "replace"))
    for rel in sorted(git(repo, git_exe, "ls-files", "--others", "--exclude-standard",
                          check=False).splitlines()):
        h.update(rel.encode("utf-8", "replace"))
        p = repo / rel
        try:
            st = p.stat()
            h.update(p.read_bytes() if st.st_size <= 20 * 1024 * 1024
                     else f"{st.st_size}:{st.st_mtime_ns}".encode())
        except OSError:
            pass
    return h.hexdigest()[:16]


def record_env_blocker(state: RunState, nkey: str, b: blk.Blocker) -> list[int]:
    """Remember the blocker that parked an issue; return every issue it has parked."""
    entry = state.env_blockers.setdefault(b.key, {**b.to_dict(), "issues": []})
    if int(nkey) not in entry["issues"]:
        entry["issues"].append(int(nkey))
    state.deferred_blockers[nkey] = b.to_dict()
    return entry["issues"]


def requeue_resolved_blockers(state: RunState, repo: Path) -> list[int]:
    """Re-queue issues parked by a blocker that a probe now shows is gone (a
    human installed the tool, started Docker...). Once per issue per run, so a
    flapping service cannot loop an issue forever."""
    back: list[int] = []
    for nkey, bd in list(state.deferred_blockers.items()):
        n = int(nkey)
        b = blk.Blocker.from_dict(bd)
        if n in state.requeued or blk.probe(b, repo) is not False:
            continue
        for d in (state.deferred, state.deferred_blockers, state.retry_counts,
                  state.failure_memory):
            d.pop(nkey, None)
        if n in state.restored_deferred:
            state.restored_deferred.remove(n)      # re-apply its parked partial work
        entry = state.env_blockers.get(b.key)
        if entry and n in entry["issues"]:
            entry["issues"].remove(n)
            if not entry["issues"]:
                state.env_blockers.pop(b.key)
        state.previous_failure[nkey] = (f"This issue was parked because {b.kind} {b.subject} "
                                        f"was unavailable. It is available now; continue.")
        state.requeued.append(n)
        back.append(n)
    return back


CLOSES_RE = re.compile(r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b")


def issues_committed_on_branch(repo: Path, git_exe: str, base: str) -> set[int]:
    """Issue numbers referenced by 'Closes #N' in commits not yet on origin/<base>."""
    log = git(repo, git_exe, "log", "--format=%B", f"origin/{base}..HEAD", check=False)
    return {int(n) for n in CLOSES_RE.findall(log)}


# ===========================================================================
# Run creation / resume
# ===========================================================================

def ref_exists(repo: Path, git_exe: str, ref: str) -> bool:
    return run_capture([git_exe, "show-ref", "--verify", "--quiet", ref],
                       cwd=repo, check=False).returncode == 0


def select_work_branch(repo: Path, git_exe: str, base: str,
                       branch: str, mode: str, sync: str = "warn") -> str:
    """Pick the branch to work on and return its name.

    auto: continue the checked-out branch if it is not the base, else behave
    like reuse on `branch`. reuse: continue `branch` (local, then origin), else
    create it from origin/<base>. new: always create a fresh `branch`."""
    git(repo, git_exe, "fetch", "origin")
    base_ref = f"refs/remotes/origin/{base}"
    if not ref_exists(repo, git_exe, base_ref):
        raise SupervisorError(f"Fetched base branch origin/{base} was not found.")
    if mode == "auto":
        current = git(repo, git_exe, "branch", "--show-current").strip()
        if current and current != base:
            print(f"Continuing active branch {current}.", flush=True)
            branch = current
        elif not branch:
            raise SupervisorError("branch_mode=auto needs a work_branch when on the base branch.")
        mode = "reuse"
    if branch == base:
        raise SupervisorError("Work branch must differ from the base branch.")
    local_ref = f"refs/heads/{branch}"
    remote_ref = f"refs/remotes/origin/{branch}"
    if ref_exists(repo, git_exe, local_ref):
        if mode == "new":
            raise SupervisorError(
                f"Branch {branch} already exists. Choose another name or branch_mode=reuse.")
        git(repo, git_exe, "switch", branch)
    elif mode == "reuse" and ref_exists(repo, git_exe, remote_ref):
        git(repo, git_exe, "switch", "--track", "-c", branch, f"origin/{branch}")
    else:
        if ref_exists(repo, git_exe, remote_ref):
            raise SupervisorError(
                f"Branch origin/{branch} already exists. Use branch_mode=reuse or another name.")
        git(repo, git_exe, "switch", "--no-track", "-c", branch, f"origin/{base}")

    if mode == "reuse":
        sync_with_base(repo, git_exe, base, branch, sync)
    return branch


def sync_with_base(repo: Path, git_exe: str, base: str, branch: str, sync: str) -> None:
    """Bring a reused branch up to origin/<base> before new work lands on it."""
    behind = int(git(repo, git_exe, "rev-list", "--count", f"HEAD..origin/{base}").strip())
    if not behind:
        return
    ahead = int(git(repo, git_exe, "rev-list", "--count", f"origin/{base}..HEAD").strip())
    if not ahead:
        # Everything on the branch is already in base (e.g. its PR was merged).
        git(repo, git_exe, "merge", "--ff-only", f"origin/{base}")
        print(f"{branch} was fully merged; fast-forwarded to origin/{base}.", flush=True)
    elif sync == "merge":
        cp = run_capture([git_exe, "merge", "--no-edit", f"origin/{base}"], cwd=repo, check=False)
        if cp.returncode != 0:
            run_capture([git_exe, "merge", "--abort"], cwd=repo, check=False)
            raise SupervisorError(
                f"{branch} conflicts with origin/{base}; the merge was aborted and nothing "
                f"changed. Resolve it by hand (git merge origin/{base}) and rerun.\n{cp.stdout}")
        print(f"Merged {behind} commit(s) from origin/{base} into {branch}.", flush=True)
    elif sync == "stop":
        raise SupervisorError(
            f"{branch} is {behind} commit(s) behind origin/{base}. Merge it first, then rerun.")
    else:
        print(f"NOTICE: {branch} is {behind} base commit(s) behind origin/{base}. "
              "No merge or rebase was performed; inspect before opening a PR.", flush=True)


def prepare_new_run(args, repo: Path, tools: Tools, f: Filters) -> RunState:
    if get_dirty_status(repo, tools.git):
        raise SupervisorError("Working tree is not clean. Commit/stash before a NEW run.\n\n"
                              + get_dirty_status(repo, tools.git))
    repo_name = get_repo_name(repo, tools)
    issues = list_matching_issues(repo, tools, repo_name, f, args.issue_limit)
    if not issues:
        raise NoWorkError("No open issues match the given filters.")

    stamp = now_local().strftime("%Y%m%d-%H%M%S-%f")
    branch = args.work_branch or (f"automation/agent-{stamp}" if args.branch_mode == "new"
                                  else DEFAULT_WORK_BRANCH)
    branch = select_work_branch(repo, tools.git, args.base_branch, branch,
                                args.branch_mode, args.sync_base)
    done = issues_committed_on_branch(repo, tools.git, args.base_branch)
    issues = [x for x in issues if x.number not in done]
    if not issues:
        raise NoWorkError(f"Every matching issue is already committed on {branch} "
                              "and closes when its PR is merged. Review and merge it.")

    run_dir = runs_root() / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    state = RunState(
        repo_path=str(repo), repo_name=repo_name, branch=branch, run_dir=str(run_dir),
        frozen_numbers=[x.number for x in issues],
        frozen_titles={str(x.number): x.title for x in issues},
        started_at=iso_now(), base_branch=args.base_branch)
    state.save()
    return state


def resume_run(state_path: Path, tools: Tools) -> tuple[RunState, Path]:
    state = RunState.load(state_path)
    repo = Path(state.repo_path).resolve()
    if not repo.exists():
        raise SupervisorError(f"Resume repo no longer exists: {repo}")
    current = git(repo, tools.git, "branch", "--show-current").strip()
    if current != state.branch:
        if get_dirty_status(repo, tools.git):
            raise SupervisorError(
                f"Cannot resume {state.branch}: dirty tree on {current!r}.")
        git(repo, tools.git, "checkout", state.branch)
    state.status = "running"
    state.save()
    return state, repo


# ===========================================================================
# Supervisor loop
# ===========================================================================

def supervisor(args) -> int:
    tools = discover_core_tools()
    spec, exe = resolve_agent(args.agent)
    # Effective model: --model / config wins, else the agent config's own model.
    effective_model = (args.model or "").strip() or spec.model or None
    vcfg = load_validation_config(Path(args.validate).resolve() if args.validate else None)
    contract = (Path(args.contract_file).read_text(encoding="utf-8")
                if args.contract_file else DEFAULT_CONTRACT)
    block_rules = blk.rules_from_config(getattr(args, "blocker_rules", []))

    if run_capture([tools.gh, "auth", "status"], check=False).returncode != 0:
        raise SupervisorError("GitHub CLI not authenticated. Run `gh auth login`.")

    exclude = {int(x) for x in str(args.exclude).split(",") if x.strip().isdigit()}
    filters = Filters(min_issue=args.min_issue, max_issue=args.max_issue,
                      milestone=args.milestone,
                      labels=[x.strip() for x in args.labels.split(",") if x.strip()],
                      labels_all=args.labels_all, exclude=exclude)

    if args.resume_latest and args.resume_branch:
        raise SupervisorError("Use only one of --resume-latest / --resume-branch.")
    requested_repo = Path(args.repo).expanduser().resolve()
    resume_path: Optional[Path] = None
    if args.resume_or_new and not (args.resume_latest or args.resume_branch):
        resume_path, why = resume_or_new_target(runs_root(), requested_repo, tools.git)
        print(f"Resume-or-new: {why}", flush=True)
    if resume_path is not None:
        state, repo = resume_run(resume_path, tools)
        print(f"Resuming run: {state.run_dir}", flush=True)
    elif args.resume_latest:
        state, repo = resume_run(find_latest_resumable_state(runs_root(), requested_repo), tools)
        print(f"Resuming latest run: {state.run_dir}", flush=True)
    elif args.resume_branch:
        found = find_latest_resumable_state(runs_root(), requested_repo, args.resume_branch)
        state, repo = resume_run(found, tools)
    else:
        repo = Path(args.repo).expanduser().resolve()
        if not repo.exists():
            raise SupervisorError(f"Repo not found: {repo}")
        state = prepare_new_run(args, repo, tools, filters)

    print(f"\nSupervisor ready.\nRepo:    {state.repo_name}\nPath:    {repo}\n"
          f"Branch:  {state.branch}\nAgent:   {spec.name} ({exe})\n"
          f"Model:   {effective_model or '(agent default)'}\n"
          f"Issues:  {', '.join(map(str, state.frozen_numbers))}\n"
          f"Run dir: {state.run_dir}\n"
          f"Closing: {'on PR merge (Closes #N)' if args.close_on == 'merge' else 'on commit'}\n"
          f"Safety:  agent edits; Python validates/commits; never pushes unless asked.\n")

    invocation_start = time.monotonic()
    stop_event = threading.Event()
    end_reason = "interrupted"
    usage_reset: Optional[dt.datetime] = None
    validate_dir = (str(Path(args.validate).resolve().parent) if args.validate
                    else str(harness_dir()))

    def request_stop(signum=None, frame=None):
        if not stop_event.is_set():
            print("\nStop requested; finishing current boundary safely.", flush=True)
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):
            pass

    announced_blocked: dict[int, list[int]] = {}
    while not stop_event.is_set():
        if args.max_hours > 0 and (time.monotonic() - invocation_start) / 3600 >= args.max_hours:
            print("MaxHours reached; not launching a new worker.", flush=True)
            end_reason = "max_hours"
            break
        for n in requeue_resolved_blockers(state, repo):
            print(f"Blocker for #{n} is resolved; re-queuing it.", flush=True)
        state.save()
        open_issues = get_open_issue_bodies(repo, tools, state.repo_name)
        done = issues_committed_on_branch(repo, tools.git, state.base_branch)
        remaining = get_open_frozen_issues(state, open_issues, done, exclude)
        if not remaining:
            print("All frozen issues are closed.", flush=True)
            end_reason = "all_closed"
            break
        eligible = [x for x in remaining if str(x.number) not in state.deferred]
        if not eligible:
            print("All remaining frozen issues are deferred.", flush=True)
            end_reason = "all_deferred"
            break
        blocked = ({} if args.ignore_dependencies
                   else unmet_dependencies(eligible, open_issues, done))
        for n, deps in blocked.items():
            if announced_blocked.get(n) != deps:
                print(f"Skipping #{n}: waiting on open dependencies "
                      f"{', '.join(f'#{d}' for d in deps)}.", flush=True)
        announced_blocked = blocked
        ready = [x for x in eligible if x.number not in blocked]
        if not ready:
            print("Every remaining issue waits on an open dependency that this run "
                  "cannot finish (deferred, excluded or outside the filters).", flush=True)
            end_reason = "all_blocked"
            break

        issue = ready[0]                    # ascending -> lowest open unblocked number
        nkey = str(issue.number)
        state.attempt += 1
        state.current_issue = issue.number
        state.save()

        issue_text = get_issue_text(repo, tools, state.repo_name, issue.number)
        if issue.number not in state.restored_deferred:
            state.restored_deferred.append(issue.number)
            restored = restore_deferred_work(repo, tools, issue.number)
            if restored:
                print(restored, flush=True)
                state.previous_failure.setdefault(nkey, restored)
            state.save()
        dirty_before = get_dirty_status(repo, tools.git)
        prompt = make_prompt(state.repo_name, issue, issue_text, contract, spec,
                             dirty_before, state.previous_failure.get(nkey, ""))

        tag = f"attempt_{state.attempt:03d}_issue_{issue.number}_{now_local():%Y%m%d-%H%M%S}"
        run_dir = Path(state.run_dir)
        log_path = run_dir / f"{tag}.log"
        prompt_path = run_dir / f"{tag}_prompt.txt"
        last_path = run_dir / f"{tag}_last.txt"
        validation_path = run_dir / f"{tag}_validation.txt"

        print(f"\nLaunching {spec.name} for issue #{issue.number}: {issue.title}")
        attempt_started = time.time()
        outcome = run_agent_worker(
            repo=repo, spec=spec, exe=exe, model=effective_model, prompt=prompt,
            log_path=log_path, prompt_path=prompt_path, last_path=last_path,
            stall_minutes=args.stall_minutes, max_session_minutes=args.max_session_minutes,
            heartbeat_seconds=args.heartbeat_seconds, issue=issue, attempt=state.attempt)

        combined = outcome.output_text
        if last_path.exists():
            combined += "\n" + last_path.read_text(encoding="utf-8", errors="replace")

        if detect_usage_limit(combined, worker_returncode=outcome.returncode):
            print(f"Usage/rate limit during #{issue.number}; not consuming a retry.", flush=True)
            state.previous_failure[nkey] = (
                f"Worker ended on a usage/rate limit. Resume from current state. "
                f"Reason: {outcome.reason}")
            state.save()
            if args.on_usage_limit == "exit":
                usage_reset = parse_reset_datetime(combined)
                end_reason = "usage_limit"
                print("Usage limit: stopping so the session wrapper can park and resume.",
                      flush=True)
                break
            if not wait_for_usage_reset(text=combined,
                                        fallback_minutes=args.usage_limit_fallback_minutes,
                                        safety_seconds=args.usage_reset_safety_seconds,
                                        stop_event=stop_event):
                break
            continue

        result = parse_agent_result(combined, spec)
        changed_files = get_changed_files(repo, tools.git)
        dirty_after = get_dirty_status(repo, tools.git)

        print(f"\nWorker ended:  {outcome.reason}\nStatus:        {result.status or '(missing)'}"
              f"\nValidation:    {result.validation or '(missing)'}\nChanged files: {len(changed_files)}")

        candidate = (result.status == "COMPLETE" and result.validation == "PASS"
                     and bool(changed_files))
        gate_output = ""

        if candidate:
            suspicious = suspicious_changed_files(repo, changed_files, vcfg)
            if suspicious:
                candidate = False
                state.previous_failure[nkey] = ("Refused suspicious/large files:\n"
                                                + "\n".join(suspicious))
                print(state.previous_failure[nkey], flush=True)

        if candidate:
            print("\nRunning INDEPENDENT supervisor validation...", flush=True)
            issue_file = run_dir / f"{tag}_issue.md"
            issue_file.write_text(f"# #{issue.number} {issue.title}\n\n{issue_text}",
                                  encoding="utf-8")
            result_file = run_dir / f"{tag}_agent_result.txt"
            result_file.write_text(result.raw, encoding="utf-8")
            ctx = ValidationContext(
                harness_dir=str(harness_dir()), config_dir=validate_dir,
                project_config=getattr(args, "config_path", ""),
                run_dir=str(run_dir), issue_number=issue.number, issue_title=issue.title,
                issue_file=str(issue_file), agent_result_file=str(result_file),
                attempt=state.attempt, attempt_started=attempt_started)
            validation = independent_validation(repo, tools, changed_files, vcfg, ctx)
            validation_path.write_text(validation.details, encoding="utf-8")
            gate_output = validation.details
            if not validation.passed:
                candidate = False
                state.previous_failure[nkey] = ("Independent validation failed:\n"
                                                + validation.details[-8000:])
                print("Independent validation FAILED. Nothing committed.", flush=True)
            else:
                print("Independent validation PASSED.", flush=True)

        if candidate:
            git(repo, tools.git, "add", "-A")
            staged = git(repo, tools.git, "diff", "--cached", "--name-only").strip()
            if not staged:
                git(repo, tools.git, "reset")
                candidate = False
                state.previous_failure[nkey] = "No staged changes after git add -A."

        if candidate:
            message = f"issue #{issue.number}: {issue.title}"
            if args.close_on == "merge":
                message += f"\n\nCloses #{issue.number}."
            git(repo, tools.git, "commit", "-m", message)
            commit = git(repo, tools.git, "rev-parse", "--short", "HEAD").strip()
            print(f"Committed issue #{issue.number} as {commit}", flush=True)
            if args.close_on == "commit":
                closed = close_issue_with_retry(
                    repo, tools, state.repo_name, issue.number,
                    f"Implemented on `{state.branch}` in `{commit}`. "
                    f"Independent supervisor validation passed.")
                if not closed:
                    state.deferred[nkey] = f"commit {commit} exists but issue-close failed"
            state.retry_counts.pop(nkey, None)
            state.previous_failure.pop(nkey, None)
            state.failure_memory.pop(nkey, None)
            state.current_issue = None
            state.save()
            continue

        # Not committable.
        state.retry_counts[nkey] = state.retry_counts.get(nkey, 0) + 1
        retry = state.retry_counts[nkey]
        state.previous_failure.setdefault(nkey, (
            f"Attempt {retry} produced no validated committable result.\n"
            f"Worker: {outcome.reason}\nStatus: {result.status or '(missing)'}\n"
            f"Validation: {result.validation or '(missing)'}\n"
            f"Worktree:\n{dirty_after}\n\nLog tail:\n{tail_text(log_path, 4000)}"))

        # Can a retry help at all? Gate output is the supervisor's own evidence;
        # the agent's log is weaker, so blocker rules corroborate it (see decide).
        found = blk.classify(gate_output, "validation", block_rules)
        found += [b for b in blk.classify(combined, "agent-log", block_rules)
                  if b.key not in {x.key for x in found}]
        found = blk.probe_all(found, repo)
        memory = state.failure_memory.get(nkey, {})
        signature = blk.failure_signature(result.status, result.validation, outcome.reason,
                                          [b.key for b in found], gate_output)
        tree = worktree_fingerprint(repo, tools.git)
        decision = blk.decide(
            status=result.status, blockers=found,
            declared=blk.declared_blocker(result.blocker, result.needs, result.summary),
            previous_keys=memory.get("keys", []),
            known_keys={k: v["issues"] for k, v in state.env_blockers.items()},
            signature=signature, previous_signature=memory.get("sig", ""),
            tree=tree, previous_tree=memory.get("tree", ""), worker_reason=outcome.reason)
        state.failure_memory[nkey] = {"sig": signature, "tree": tree,
                                      "keys": [b.key for b in found]}
        for b in found:
            print(f"Blocker seen: {b.key} ({b.severity}, {b.source}, "
                  f"probe={'missing' if b.confirmed else 'n/a'}): {b.evidence}", flush=True)
        if decision.feedback:
            state.previous_failure[nkey] += "\n\n" + decision.feedback

        limit = args.max_no_progress_retries
        if decision.max_attempts:
            limit = min(limit, decision.max_attempts)
        print(f"Issue #{issue.number} not committed. Attempt {retry}/{limit} "
              f"({decision.reason}).", flush=True)

        if decision.defer or retry >= limit:
            state.deferred[nkey] = (decision.reason if decision.defer
                                    else f"no validated result after {retry} attempts")
            halt = False
            if decision.primary:
                p = decision.primary
                parked = record_env_blocker(state, nkey, p)
                state.deferred[nkey] += f"; needs a human: {p.hint}"
                print(f"Parking #{issue.number}: {decision.reason}\n  -> {p.hint}", flush=True)
                halt = (p.severity == blk.HARD and args.env_blocker_halt_after > 0
                        and len(parked) >= args.env_blocker_halt_after)
            tag = defer_failed_work(repo, tools, issue.number)
            if tag:
                state.deferred[nkey] += f"; partial work in tag {tag}"
                print(f"Preserved partial work in tag {tag} "
                      f"(restore: git stash apply {tag})", flush=True)
            state.previous_failure.pop(nkey, None)
            if halt:
                print(f"{decision.primary.key} has parked {len(parked)} issues; the environment "
                      f"needs fixing before more attempts are worth paying for. Stopping.",
                      flush=True)
                end_reason = "env_blocked"
                state.current_issue = None
                state.save()
                break
        state.current_issue = None
        state.save()

    return finalize(state, repo, tools, exclude, args, stop_event,
                    end_reason=end_reason, usage_reset=usage_reset)


# Ends after which the same run should be resumed rather than a new one frozen.
RESUMABLE_END_REASONS = {"max_hours", "usage_limit", "interrupted", "env_blocked"}


def finalize(state: RunState, repo: Path, tools: Tools, exclude: set[int],
             args, stop_event: threading.Event, *, end_reason: str = "interrupted",
             usage_reset: Optional[dt.datetime] = None) -> int:
    state.current_issue = None
    if stop_event.is_set():
        end_reason = "interrupted"
    state.status = "stopped" if end_reason in RESUMABLE_END_REASONS else "finished"
    state.save()

    open_issues = get_open_issue_bodies(repo, tools, state.repo_name)
    committed = issues_committed_on_branch(repo, tools.git, state.base_branch)
    remaining = get_open_frozen_issues(state, open_issues, committed, exclude)
    blocked = ({} if args.ignore_dependencies
               else unmet_dependencies(remaining, open_issues, committed))
    final_dirty = get_dirty_status(repo, tools.git)
    ahead = int(git(repo, tools.git, "rev-list", "--count",
                    f"origin/{state.base_branch}..HEAD").strip())
    done = sorted(committed)

    summary = (f"# Issue Supervisor Run\n\n**Repo:** {state.repo_name}\n"
               f"**Branch:** `{state.branch}`\n**Started:** {state.started_at}\n"
               f"**Finished:** {iso_now()}\n**Attempts:** {state.attempt}\n"
               f"**Commits ahead of {state.base_branch}:** {ahead}\n\n"
               "## Committed on this branch (close when the PR merges)\n"
               + ("\n".join(f"Closes #{n}" for n in done) or "- None")
               + "\n\n## Remaining open frozen issues\n"
               + ("\n".join(f"- #{x.number} {x.title}" for x in remaining) or "- None")
               + "\n\n## Blocked by open dependencies\n"
               + ("\n".join(f"- #{n} waits on {', '.join(f'#{d}' for d in deps)}"
                            for n, deps in sorted(blocked.items())) or "- None")
               + "\n\n## Deferred issues\n"
               + ("\n".join(f"- #{n}: {r}" for n, r in sorted(state.deferred.items(),
                                                              key=lambda kv: int(kv[0])))
                  or "- None")
               + "\n\n## Environment blockers (install/configure, then resume)\n"
               + ("\n".join(f"- `{k}` parked {', '.join(f'#{n}' for n in v['issues'])}: "
                            f"{v['hint']}\n  evidence: `{v['evidence']}`"
                            for k, v in sorted(state.env_blockers.items())) or "- None")
               + f"\n\n## Final worktree\n```\n{final_dirty or 'clean'}\n```\n")
    (Path(state.run_dir) / SUMMARY_FILE_NAME).write_text(summary, encoding="utf-8")
    try:                                    # one-page digest; never fails the run
        import digest
        print(f"Run digest: {digest.write_digest(state.run_dir)}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: run digest not written: {exc}")

    push_status = "not pushed (push disabled)"
    if args.push:
        if final_dirty:
            push_status = "NOT PUSHED: uncommitted changes remain."
            state.status = "interrupted"; state.save()
        elif ahead <= 0:
            push_status = f"NOT PUSHED: no commits ahead of origin/{state.base_branch}."
        else:
            cp = run_capture([tools.git, "push", "-u", "origin", state.branch],
                             cwd=repo, check=False)
            push_status = (f"PUSHED: {ahead} commit(s) on {state.branch}."
                           if cp.returncode == 0 else "PUSH FAILED:\n" + cp.stdout)
            if cp.returncode == 0 and args.open_pr:
                pr = run_capture([tools.gh, "pr", "create", "--repo", state.repo_name,
                                  "--base", state.base_branch, "--head", state.branch,
                                  "--title", f"Automated agent run {state.branch}",
                                  "--body", summary], cwd=repo, check=False)
                push_status += ("\nPR: " + pr.stdout.strip()) if pr.returncode == 0 \
                    else "\nPR creation failed:\n" + pr.stdout

    print("\n================ SUPERVISOR FINISHED ================")
    print(f"Branch:    {state.branch}\nAttempts:  {state.attempt}\n"
          f"Committed: {', '.join(f'#{n}' for n in done) or 'none'}\n"
          f"Remaining: {len(remaining)} ({len(blocked)} blocked by dependencies)\n"
          f"Run dir:   {state.run_dir}\n{push_status}")
    if ahead > 0 and not args.push:
        base = state.base_branch
        print(f"\nTo review and ship ({ahead} commit(s) ahead of {base}):\n"
              f"  git log --oneline origin/{base}..{state.branch}\n"
              f"  git diff origin/{base}...{state.branch} --stat\n"
              f"  git push -u origin {state.branch}\n"
              f"  gh pr create --base {base} --head {state.branch} --fill\n"
              "Merge with a merge commit (not squash), then rerun: the branch "
              "fast-forwards to the new base automatically.")
    print("====================================================")
    emit_run_result(reason=end_reason, status=state.status, committed=done,
                    remaining=len(remaining), blocked=len(blocked),
                    deferred=sorted(int(n) for n in state.deferred),
                    env_blockers={k: {"hint": v["hint"], "issues": v["issues"]}
                                  for k, v in state.env_blockers.items()},
                    usage_reset=usage_reset.isoformat() if usage_reset else None,
                    run_dir=state.run_dir)
    return USAGE_LIMIT_EXIT if end_reason == "usage_limit" else 0


# ===========================================================================
# Self-test (offline)
# ===========================================================================

def self_test() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool):
        print(f"{'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    spec = BUILTIN_AGENTS["codex"]
    sample = (f"noise\n{spec.result_begin}\nSTATUS: COMPLETE\nVALIDATION: PASS\n"
              f"ISSUE: #12\nSUMMARY: done\nTESTS: pytest\nNOTES: NONE\n{spec.result_end}\n")
    r = parse_agent_result(sample, spec)
    check("parse status", r.status == "COMPLETE")
    check("parse validation", r.validation == "PASS")
    rb = parse_agent_result(f"{spec.result_begin}\nSTATUS: BLOCKED\nVALIDATION: FAIL\n"
                            f"BLOCKER: MISSING_SDK\nNEEDS: Android SDK (ANDROID_HOME)\n"
                            f"SUMMARY: no sdk\n{spec.result_end}", spec)
    check("parse blocker + needs", rb.blocker == "MISSING_SDK"
          and rb.needs == "Android SDK (ANDROID_HOME)" and rb.summary == "no sdk")
    check("result without BLOCKER line still parses", r.blocker == "" and r.needs == "")
    failures.extend(blk.self_test())

    # A parked issue comes back once its blocker's probe passes, and only once.
    _rs = RunState("r", "o/r", "b", "d", [3], {"3": "x"}, "t")
    _gone = blk.Blocker("MISSING_EXECUTABLE", Path(sys.executable).name, blk.HARD, "h", "e",
                        "validation", probe="which")
    if shutil.which(_gone.subject):
        _rs.deferred["3"] = "blocked"
        _rs.restored_deferred.append(3)
        record_env_blocker(_rs, "3", _gone)
        check("resolved blocker re-queues its issue",
              requeue_resolved_blockers(_rs, Path(".")) == [3] and "3" not in _rs.deferred
              and not _rs.env_blockers and 3 not in _rs.restored_deferred)
        _rs.deferred["3"] = "blocked again"
        record_env_blocker(_rs, "3", _gone)
        check("an issue is re-queued at most once per run",
              requeue_resolved_blockers(_rs, Path(".")) == [])
    _still = blk.Blocker("MISSING_EXECUTABLE", "__no_such_tool__", blk.HARD, "h", "e",
                         "validation", probe="which")
    _rs2 = RunState("r", "o/r", "b", "d", [4], {"4": "x"}, "t", deferred={"4": "x"})
    record_env_blocker(_rs2, "4", _still)
    check("unresolved blocker stays parked", requeue_resolved_blockers(_rs2, Path(".")) == []
          and "4" in _rs2.deferred and _rs2.env_blockers[_still.key]["issues"] == [4])

    base = dt.datetime(2026, 9, 8, 17, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    check("clock-only reset", parse_reset_datetime("limit reached - resets 8pm", base)
          == dt.datetime(2026, 9, 8, 20, 0, tzinfo=base.tzinfo))
    check("past clock -> next day", parse_reset_datetime("resets at 3:30 PM", base)
          == dt.datetime(2026, 9, 9, 15, 30, tzinfo=base.tzinfo))
    check("dated reset", parse_reset_datetime("resets Sep 10 at 3pm", base)
          == dt.datetime(2026, 9, 10, 15, 0, tzinfo=base.tzinfo))
    codex_msg = ('{"type":"error","message":"You’ve hit your usage limit. Upgrade to Pro '
                 '(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage '
                 'to purchase more credits or try again at 6:22 PM."}')
    check("codex try-again clock", parse_reset_datetime(codex_msg, base)
          == dt.datetime(2026, 9, 8, 18, 22, tzinfo=base.tzinfo))
    noisy = "php tools/db_reset.php\nOK: 1 rows\n" + codex_msg + "\nreset.php 001_schema.sql\n"
    check("worker-log noise ignored (db_reset.php is not a reset time)",
          parse_reset_datetime(noisy, base) == dt.datetime(2026, 9, 8, 18, 22, tzinfo=base.tzinfo))
    check("db_reset alone parses nothing",
          parse_reset_datetime("php tools/db_reset.php --production 1", base) is None)

    check("blocking usage detected",
          detect_usage_limit('{"type":"error","message":"You have reached your rate limit."}'))
    check("claude headless limit detected",
          detect_usage_limit("Claude AI usage limit reached|1790758585", worker_returncode=1)
          and detect_usage_limit("You've hit your limit · resets 3pm", worker_returncode=1))
    check("claude epoch reset parsed",
          parse_reset_datetime("Claude AI usage limit reached|1790758585", base).timestamp()
          == 1790758585)
    check("claude builtin never asks for approval",
          "bypassPermissions" in BUILTIN_AGENTS["claude"].argv
          and "never" in BUILTIN_AGENTS["codex"].argv)
    check("approaching warning ignored", not detect_usage_limit(
        '{"type":"item.completed","item":{"type":"agent_message","text":"approaching your usage limit"}}'))

    # Filter logic
    f = Filters(min_issue=2, max_issue=5, milestone="M1", labels=["a", "b"],
                labels_all=False, exclude={4})
    mk = lambda n, ms, labs: {"number": n, "milestone": {"title": ms},
                              "labels": [{"name": x} for x in labs]}
    check("range low excluded", not _issue_matches(mk(1, "M1", ["a"]), f))
    check("explicit exclude", not _issue_matches(mk(4, "M1", ["a"]), f))
    check("milestone mismatch", not _issue_matches(mk(3, "M2", ["a"]), f))
    check("label any-match", _issue_matches(mk(3, "M1", ["b"]), f))
    check("label none-match", not _issue_matches(mk(3, "M1", ["z"]), f))
    f_all = Filters(2, 5, "", ["a", "b"], True, set())
    check("labels-all requires all", not _issue_matches(mk(3, "", ["a"]), f_all))
    check("labels-all satisfied", _issue_matches(mk(3, "", ["a", "b"]), f_all))

    # Agent argv: model token dropped when no model.
    argv = build_agent_argv(spec, "codex", None, Path("p.txt"), Path("l.txt"), "hi")
    check("model pair removed when no model", "--model" not in argv)
    argv2 = build_agent_argv(spec, "codex", "m1", Path("p.txt"), Path("l.txt"), "hi")
    check("model substituted", "m1" in argv2 and "{MODEL}" not in argv2)
    check("last-msg substituted", "l.txt" in " ".join(argv2))

    # Suspicious-file guard
    cfg = default_validation_config()
    check("deny .env", suspicious_changed_files(Path("."), ["config/.env"], cfg) == ["config/.env"])
    check("allow .env.example", suspicious_changed_files(Path("."), [".env.example"], cfg) == [])

    # Glob matcher (globstar must match top-level and nested)
    check("glob **/*.py top-level", glob_match("app.py", "**/*.py"))
    check("glob **/*.py nested", glob_match("pkg/m.py", "**/*.py"))
    check("glob frontend/** scoped", glob_match("frontend/x.tsx", "frontend/**")
          and not glob_match("backend/x.py", "frontend/**"))
    check("glob **/.env not .env.example",
          glob_match(".env", "**/.env") and not glob_match(".env.example", "**/.env"))
    check("glob **/node_modules/** catches nested packages",
          glob_match("tools/ui/node_modules/a/index.js", "**/node_modules/**")
          and not glob_match("src/lib/mail.php", "**/mail/**"))
    check("closing keywords parsed",
          CLOSES_RE.findall("x\n\nCloses #4.\nfixes #5\nsee #6") == ["4", "5"])

    # Dependencies: open deps block, closed or branch-committed deps do not.
    check("depends line parsed",
          parse_dependencies("see #9\n\n**Depends on:** #57, #59\r\nmore") == [57, 59])
    _open = {60: "**Depends on:** #57, #58", 61: "**Depends on:** #60",
             57: "", 58: "", 62: "**Depends on:** #1"}
    _blocked = unmet_dependencies([Issue(60, "a"), Issue(61, "b"), Issue(62, "c")],
                                  _open, done={57})
    check("open deps block, committed and closed deps do not",
          _blocked == {60: [58], 61: [60]})
    _st = RunState("r", "o/r", "b", "d", [57, 58, 60], {"57": "x", "58": "y", "60": "z"}, "t")
    check("frozen queue drops closed, committed and excluded",
          [x.number for x in get_open_frozen_issues(_st, _open, {57}, {58})] == [60])

    # Project-config layering: CLI > config(run section) > hard default.
    _p = build_arg_parser()
    _a = _p.parse_args(["--max-issue", "9"])
    apply_config(_a, {"repo": "/r", "min_issue": 2, "run": {"labels": "x", "push": True}})
    check("config fills repo", _a.repo == "/r")
    check("cli overrides config", _a.max_issue == 9)
    check("run-section label applied", _a.labels == "x")
    check("bool OR-merge from config", _a.push is True)
    check("hard default when unset", _a.stall_minutes == 20)
    check("branch mode defaults to auto", _a.branch_mode == "auto")
    check("reused branches sync with base by default", _a.sync_base == "merge")
    check("issues close on merge by default", _a.close_on == "merge")

    # Per-project folders at the platform root are discoverable by name.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in ("Alpha", "_template", "Empty"):
            (root / name).mkdir()
        for name in ("Alpha", "_template"):
            (root / name / "issue-automation.config.json").write_text("{}", encoding="utf-8")
        check("projects listed, template and config-less folders skipped",
              list_projects(root) == ["Alpha"])
        check("project resolves to its config",
              project_config_path(root, "Alpha").parent.name == "Alpha")
        try:
            project_config_path(root, "Missing")
        except SupervisorError as exc:
            check("unknown project names the known ones", "Alpha" in str(exc))
        else:
            check("unknown project names the known ones", False)

    # Real local Git exercise: multiple logical runs share one work branch.
    git_exe = find_executable("git.exe", "git")
    if git_exe:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = root / "origin.git", root / "repo"
            run_capture([git_exe, "init", "--bare", "--initial-branch=main", str(origin)])
            run_capture([git_exe, "clone", str(origin), str(repo)])
            git(repo, git_exe, "config", "user.email", "test@example.invalid")
            git(repo, git_exe, "config", "user.name", "Test")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            git(repo, git_exe, "add", "README.md")
            git(repo, git_exe, "commit", "-m", "base")
            git(repo, git_exe, "push", "origin", "main")
            select_work_branch(repo, git_exe, "main", "automation/repeated", "new")
            check("new work branch does not track main",
                  run_capture([git_exe, "rev-parse", "--abbrev-ref", "@{upstream}"],
                              cwd=repo, check=False).returncode != 0)
            (repo / "README.md").write_text("first run\n", encoding="utf-8")
            git(repo, git_exe, "commit", "-am", "first run")
            first_head = git(repo, git_exe, "rev-parse", "HEAD").strip()
            git(repo, git_exe, "switch", "main")
            select_work_branch(repo, git_exe, "main", "automation/repeated", "reuse")
            check("reuse retains earlier commits",
                  git(repo, git_exe, "rev-parse", "HEAD").strip() == first_head)
            check("reuse does not change main",
                  (repo / "README.md").read_text(encoding="utf-8") == "first run\n"
                  and git(repo, git_exe, "rev-list", "--count", "main..HEAD").strip() == "1")
            try:
                select_work_branch(repo, git_exe, "missing", "automation/other", "new")
            except SupervisorError:
                check("fetch failure leaves current branch intact",
                      git(repo, git_exe, "branch", "--show-current").strip()
                      == "automation/repeated")
            else:
                check("fetch failure leaves current branch intact", False)

            def land_on_main(name: str, text: str) -> None:
                git(repo, git_exe, "switch", "main")
                git(repo, git_exe, "pull", "--ff-only", "origin", "main")
                (repo / name).write_text(text, encoding="utf-8")
                git(repo, git_exe, "add", name)
                git(repo, git_exe, "commit", "-m", f"main: {name}")
                git(repo, git_exe, "push", "origin", "main")
                git(repo, git_exe, "switch", "automation/repeated")

            # auto: an active non-base branch wins over the configured name.
            picked = select_work_branch(repo, git_exe, "main", "automation/configured", "auto")
            check("auto continues the active branch",
                  picked == "automation/repeated"
                  and not ref_exists(repo, git_exe, "refs/heads/automation/configured"))

            # Closes #N marks an issue as done on the branch until it reaches base.
            (repo / "seven.txt").write_text("7\n", encoding="utf-8")
            git(repo, git_exe, "add", "seven.txt")
            git(repo, git_exe, "commit", "-m", "issue #7: seven\n\nCloses #7.")
            check("committed issues detected from Closes #N",
                  issues_committed_on_branch(repo, git_exe, "main") == {7})

            # Deferred work becomes a tag, leaving no stash and a clean tree.
            (repo / "partial.txt").write_text("wip\n", encoding="utf-8")
            tag = defer_failed_work(repo, Tools(git_exe, "", ""), 9)
            check("deferred work stored as a tag",
                  tag == "deferred/issue-9"
                  and ref_exists(repo, git_exe, "refs/tags/deferred/issue-9")
                  and not get_dirty_status(repo, git_exe)
                  and not git(repo, git_exe, "stash", "list").strip())
            git(repo, git_exe, "stash", "apply", "deferred/issue-9")
            check("deferred tag restores the work",
                  (repo / "partial.txt").read_text(encoding="utf-8") == "wip\n")
            _fp1 = worktree_fingerprint(repo, git_exe)
            check("worktree fingerprint is stable", _fp1 == worktree_fingerprint(repo, git_exe))
            (repo / "partial.txt").write_text("wip 2\n", encoding="utf-8")
            check("worktree fingerprint sees untracked edits",
                  _fp1 != worktree_fingerprint(repo, git_exe))
            (repo / "partial.txt").unlink()

            # A branch behind base gets base merged in when sync=merge.
            land_on_main("other.txt", "main\n")
            select_work_branch(repo, git_exe, "main", "", "auto", "merge")
            check("sync=merge brings base into the branch",
                  (repo / "other.txt").exists()
                  and git(repo, git_exe, "rev-list", "--count", "HEAD..origin/main").strip() == "0")

            # A conflicting base is aborted cleanly and the run stops.
            (repo / "README.md").write_text("branch\n", encoding="utf-8")
            git(repo, git_exe, "commit", "-am", "branch edit")
            before = git(repo, git_exe, "rev-parse", "HEAD").strip()
            land_on_main("README.md", "main edit\n")
            try:
                select_work_branch(repo, git_exe, "main", "", "auto", "merge")
            except SupervisorError:
                check("conflicting sync is aborted and leaves the branch unchanged",
                      git(repo, git_exe, "rev-parse", "HEAD").strip() == before
                      and not get_dirty_status(repo, git_exe)
                      and not (repo / ".git" / "MERGE_HEAD").exists())
            else:
                check("conflicting sync is aborted and leaves the branch unchanged", False)

            # Once the branch is merged into base (merge commit), a rerun fast-forwards it.
            git(repo, git_exe, "merge", "-X", "ours", "--no-edit", "origin/main")
            git(repo, git_exe, "switch", "main")
            git(repo, git_exe, "pull", "--ff-only", "origin", "main")
            git(repo, git_exe, "merge", "--no-ff", "--no-edit", "automation/repeated")
            git(repo, git_exe, "push", "origin", "main")
            git(repo, git_exe, "switch", "automation/repeated")
            select_work_branch(repo, git_exe, "main", "", "auto", "merge")
            check("merged branch fast-forwards to base",
                  git(repo, git_exe, "rev-parse", "HEAD").strip()
                  == git(repo, git_exe, "rev-parse", "origin/main").strip()
                  and issues_committed_on_branch(repo, git_exe, "main") == set())

    # Custom prompt edits from the agent spec are injected around the prompt.
    _spec = AgentSpec(name="t", exe_candidates=["x"], argv=["{EXE}"],
                      prompt_prefix="PXY", prompt_suffix="SXY")
    _pr = make_prompt("me/r", Issue(1, "t"), "body", DEFAULT_CONTRACT, _spec, "", "")
    check("prompt_prefix injected before context",
          "PXY" in _pr and _pr.index("PXY") < _pr.index("CONTEXT READING"))
    check("prompt_suffix injected before result contract",
          "SXY" in _pr and _pr.index("SXY") < _pr.index("FINAL RESPONSE CONTRACT"))

    # Gate context: path tokens expand inside strings; __PY__ stays whole-token.
    _tools = Tools(git="git", gh="gh", python=sys.executable)
    _ctx = ValidationContext(harness_dir="/h", config_dir="/c", run_dir="/r",
                             issue_number=7, issue_title="t")
    _ex = _expand_argv(["__PY__", "__HARNESS__/adversary.py", "--config",
                        "__CONFIG_DIR__/a.json"], _tools, ["."], _ctx)
    check("gate tokens expand",
          _ex == [[sys.executable, "/h/adversary.py", "--config", "/c/a.json"]])
    check("no ctx leaves tokens alone",
          _expand_argv(["__HARNESS__/x"], _tools, ["."]) == [["__HARNESS__/x"]])
    check("__CHANGED_DIRS__ still fans out",
          _expand_argv(["a", "__CHANGED_DIRS__"], _tools, ["x", "y"], _ctx)
          == [["a", "x"], ["a", "y"]])

    # skip_if_failed spares the paid reviewer; env reaches gate commands.
    with tempfile.TemporaryDirectory() as tmp:
        _py = "__PY__"
        _vc = ValidationConfig(always=[
            {"label": "env", "argv": [_py, "-c",
             "import os,sys; sys.exit(0 if os.environ['AGENT_ISSUE_NUMBER']=='7' else 1)"]},
            {"label": "reviewer", "skip_if_failed": True, "argv": [_py, "-c", "pass"]},
        ])
        _vr = independent_validation(Path(tmp), _tools, [], _vc, _ctx)
        check("gate env exported", _vr.passed and "SKIPPED" not in _vr.details)
        _vg = ValidationConfig(always=[
            {"label": "later", "skip_if_failed": True, "argv": [_py, "-c",
             "import json,os,sys; sys.exit(0 if json.loads(os.environ['AGENT_GATES_PASSED'])"
             "==['first'] else 1)"]},
            {"label": "first", "argv": [_py, "-c", "pass"]},
        ])
        check("AGENT_GATES_PASSED lists earlier passed gates",
              independent_validation(Path(tmp), _tools, [], _vg, _ctx).passed)
        _vc.always.insert(0, {"label": "fails", "argv": [_py, "-c", "raise SystemExit(1)"]})
        _vr = independent_validation(Path(tmp), _tools, [], _vc, _ctx)
        check("skip_if_failed skips after a failure",
              not _vr.passed and "SKIPPED: reviewer" in _vr.details)
        _vo = ValidationConfig(
            always=[{"label": "reviewer", "skip_if_failed": True, "argv": [_py, "-c", "pass"]}],
            rules=[{"when_touched": ["**/*.py"], "commands": [
                {"label": "rule-fails", "argv": [_py, "-c", "raise SystemExit(1)"]}]}])
        _vr = independent_validation(Path(tmp), _tools, ["a.py"], _vo, _ctx)
        check("reviewer runs after rules, skipped when a rule failed",
              _vr.details.index("rule-fails") < _vr.details.index("SKIPPED: reviewer"))
        _vt = ValidationConfig(always=[{"label": "slow", "timeout_minutes": 0.01,
                                        "argv": [_py, "-c", "import time; time.sleep(5)"]}])
        check("gate timeout fails cleanly",
              "timed out" in independent_validation(Path(tmp), _tools, [], _vt).details)

    # Deferred work from an earlier run is restored before the issue's first attempt.
    with tempfile.TemporaryDirectory() as tmp:
        _r = Path(tmp)
        _g = lambda *a: subprocess.run(["git", "-C", str(_r), "-c", "user.email=t@t",
                                        "-c", "user.name=t", *a], check=True,
                                       capture_output=True, text=True).stdout
        _g("init", "-q")
        (_r / "a.txt").write_text("one\n", encoding="utf-8")
        _g("add", "-A"); _g("commit", "-qm", "base")
        _t = Tools(git="git", gh="gh", python=sys.executable)
        (_r / "a.txt").write_text("partial\n", encoding="utf-8")
        (_r / "new.txt").write_text("new\n", encoding="utf-8")
        check("defer parks work in a tag", defer_failed_work(_r, _t, 5) == "deferred/issue-5"
              and not get_dirty_status(_r, "git"))
        _msg = restore_deferred_work(_r, _t, 5)
        check("deferred work restored into a clean tree",
              _msg and (_r / "a.txt").read_text() == "partial\n" and (_r / "new.txt").exists())
        check("no restore into a dirty tree", restore_deferred_work(_r, _t, 5) is None)
        _g("checkout", "--", "a.txt"); (_r / "new.txt").unlink()
        (_r / "a.txt").write_text("moved on\n", encoding="utf-8")
        _g("commit", "-qam", "conflicting change")
        check("conflicting deferred work is skipped and the tree left clean",
              restore_deferred_work(_r, _t, 5) is None and not get_dirty_status(_r, "git"))
        check("no tag, nothing to restore", restore_deferred_work(_r, _t, 6) is None)

    # --resume-or-new only ever looks at the most recent run for the repo.
    with tempfile.TemporaryDirectory() as tmp:
        _repo, _root = Path(tmp) / "r", Path(tmp) / "runs"
        _repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "work", str(_repo)], check=True)
        subprocess.run(["git", "-C", str(_repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "i"], check=True)

        def _state(name: str, status: str, branch: str, age: int) -> Path:
            d = _root / name
            d.mkdir(parents=True)
            sp = d / STATE_FILE_NAME
            sp.write_text(json.dumps({"repo_path": str(_repo), "status": status,
                                      "branch": branch}), encoding="utf-8")
            os.utime(sp, (time.time() - age, time.time() - age))
            return sp

        _old = _state("old", "running", "automation/agent-gone", 3000)
        _state("newer", "finished", "work", 100)
        check("stale unfinished run behind a finished one is ignored",
              resume_or_new_target(_root, _repo, "git")[0] is None)
        _new = _state("newest", "stopped", "work", 10)
        check("latest unfinished run on a live branch is resumed",
              resume_or_new_target(_root, _repo, "git")[0] == _new)
        os.utime(_old, None)
        check("latest run on a deleted branch starts new and is marked abandoned",
              resume_or_new_target(_root, _repo, "git")[0] is None
              and json.loads(_old.read_text(encoding="utf-8"))["status"] == "abandoned")

    _p = build_arg_parser().parse_args(["--resume-or-new", "--on-usage-limit", "exit"])
    check("session flags parse", _p.resume_or_new and _p.on_usage_limit == "exit")

    check("Python >= 3.10", sys.version_info >= (3, 10))

    if failures:
        print("\nSELF-TEST FAILED:", ", ".join(failures))
        return 1
    print("\nALL SELF-TESTS PASSED")
    return 0


# ===========================================================================
# CLI
# ===========================================================================

# Hard defaults for every config-backed option. A value flows:
#   command-line flag  >  project config file  >  this table.
HARD_DEFAULTS: dict[str, object] = {
    "repo": ".", "min_issue": 0, "max_issue": 0, "labels": "", "milestone": "",
    "exclude": "", "issue_limit": 200, "agent": "codex", "model": "",
    "contract_file": "", "validate": "", "base_branch": "main", "work_branch": "",
    "branch_mode": "auto", "close_on": "merge", "sync_base": "merge",
    "max_hours": 6.0, "stall_minutes": 20, "max_session_minutes": 45,
    "max_no_progress_retries": 3, "env_blocker_halt_after": 3, "heartbeat_seconds": 60,
    "usage_limit_fallback_minutes": 15, "usage_reset_safety_seconds": 90,
}
# Config keys that are booleans; command-line ON is OR-merged with the config.
BOOL_KEYS = ("labels_all", "push", "open_pr", "ignore_dependencies")

DEFAULT_CONFIG_NAMES = ("issue-automation.config.json",
                        "issue-automation.config.yaml",
                        "issue-automation.config.yml")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Supervise a coding agent across GitHub issues, filtered by "
                    "range/labels/milestone, ascending order, one validated commit each. "
                    "Flags override a project config file (see --config).")
    # Config-backed options default to None so we can tell "unset" from "set".
    p.add_argument("--config", default="",
                   help="Project config file (JSON/YAML). If omitted, "
                        "issue-automation.config.* in the current dir (then the repo) is used.")
    p.add_argument("--project", default="",
                   help="Project folder beside this script holding issue-automation.config.*.")
    p.add_argument("--list-projects", action="store_true",
                   help="List project folders beside this script and exit.")
    p.add_argument("--repo", default=None, help="Repository path.")
    # Deterministic selection
    p.add_argument("--min-issue", type=int, default=None, help="Lowest issue number (inclusive).")
    p.add_argument("--max-issue", type=int, default=None, help="Highest issue number (inclusive).")
    p.add_argument("--labels", default=None, help="Comma-separated labels to match (ANY by default).")
    p.add_argument("--labels-all", action="store_true", help="Require ALL --labels, not any.")
    p.add_argument("--milestone", default=None, help="Only issues in this milestone.")
    p.add_argument("--exclude", default=None, help="Comma-separated issue numbers to skip.")
    p.add_argument("--issue-limit", type=int, default=None, help="Max issues to scan.")
    p.add_argument("--ignore-dependencies", action="store_true",
                   help="Work issues even when their '**Depends on:**' issues are still open.")
    # Agent
    p.add_argument("--agent", default=None,
                   help="Builtin agent name (codex/claude/aider) or path to an agent config.")
    p.add_argument("--model", default=None, help="Model passed to the agent (blank = agent default).")
    p.add_argument("--contract-file", default=None, help="File with the agent safety contract.")
    # Validation
    p.add_argument("--validate", default=None, help="Validation config file (JSON/YAML).")
    # Branch / output
    p.add_argument("--base-branch", default=None, help="Branch to fork from / compare against.")
    p.add_argument("--work-branch", default=None,
                   help=f"Work branch name (default: {DEFAULT_WORK_BRANCH}; "
                        "timestamped when --branch-mode new).")
    p.add_argument("--branch-mode", choices=("new", "reuse", "auto"), default=None,
                   help="new: require a fresh branch; reuse: continue an existing named branch; "
                        "auto: continue the checked-out branch unless it is the base, else reuse.")
    p.add_argument("--close-on", choices=("merge", "commit"), default=None,
                   help="merge: commit 'Closes #N' and let the PR merge close it; "
                        "commit: close the issue right after its validated commit.")
    p.add_argument("--sync-base", choices=("merge", "warn", "stop"), default=None,
                   help="When a reused branch is behind base: merge base in (abort on "
                        "conflict), just warn, or stop.")
    p.add_argument("--push", action="store_true", help="Push the automation branch at the end.")
    p.add_argument("--open-pr", action="store_true", help="Open a PR after a successful push.")
    # Watchdog / limits
    p.add_argument("--max-hours", type=float, default=None, help="Wall-clock cap; 0 = unlimited.")
    p.add_argument("--stall-minutes", type=int, default=None)
    p.add_argument("--max-session-minutes", type=int, default=None)
    p.add_argument("--max-no-progress-retries", type=int, default=None)
    p.add_argument("--env-blocker-halt-after", type=int, default=None,
                   help="Stop the run once one environment blocker (missing SDK/tool, "
                        "Docker down...) has parked this many issues; 0 = never stop.")
    p.add_argument("--heartbeat-seconds", type=int, default=None)
    p.add_argument("--usage-limit-fallback-minutes", type=int, default=None)
    p.add_argument("--usage-reset-safety-seconds", type=int, default=None)
    # Resume / test (never config-backed)
    p.add_argument("--resume-latest", action="store_true")
    p.add_argument("--resume-branch", default="")
    p.add_argument("--resume-or-new", action="store_true",
                   help="Resume the latest unfinished run for this repo, else start a new one "
                        "(what session.py uses between slices).")
    p.add_argument("--on-usage-limit", choices=("wait", "exit"), default="wait",
                   help=f"wait: sleep in-process until the limit resets; exit: save state and "
                        f"exit {USAGE_LIMIT_EXIT} so a wrapper can park and resume.")
    p.add_argument("--self-test", action="store_true")
    return p


def harness_dir() -> Path:
    """engine/ — where run_issues.py, adversary.py and ml_gate.py live (__HARNESS__)."""
    return Path(__file__).resolve().parent


def platform_root() -> Path:
    """Repo root (parent of engine/): project folders live here, beside v2.py."""
    return harness_dir().parent


def list_projects(root: Path) -> list[str]:
    """Folders at the platform root that contain a project config ('_x'/'.x' are skipped)."""
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and d.name[0] not in "_."
                  and any((d / n).exists() for n in DEFAULT_CONFIG_NAMES))


def project_config_path(root: Path, name: str) -> Path:
    for n in DEFAULT_CONFIG_NAMES:
        cand = root / name / n
        if cand.exists():
            return cand
    known = ", ".join(list_projects(root)) or "none"
    raise SupervisorError(f"No config for project '{name}' under {root}. Known projects: {known}")


def load_project_config(explicit: str, repo_hint: str) -> tuple[dict, Optional[Path]]:
    """Return (config dict, path). Explicit --config wins; else auto-discover."""
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.exists():
            raise SupervisorError(f"--config file not found: {path}")
        return load_config_file(path), path
    search_dirs = [Path.cwd()]
    if repo_hint:
        search_dirs.append(Path(repo_hint).expanduser())
    search_dirs.append(platform_root())   # config shipped at the platform root
    for d in search_dirs:
        for name in DEFAULT_CONFIG_NAMES:
            cand = (d / name)
            if cand.exists():
                return load_config_file(cand), cand.resolve()
    return {}, None


def apply_config(args, config: dict) -> None:
    """Fill unset args from config, then from HARD_DEFAULTS. CLI always wins.

    A config may be flat, or nest run-only keys under a `run` section; both are
    read. Booleans are OR-merged (CLI --flag or config true)."""
    merged = dict(config)
    merged.update(config.get("run", {}) or {})    # run-section overrides flat for the runner
    for key, hard in HARD_DEFAULTS.items():
        if getattr(args, key) is None:
            setattr(args, key, merged.get(key, hard))
    for key in BOOL_KEYS:
        setattr(args, key, bool(getattr(args, key)) or bool(merged.get(key, False)))


def validate_args(args) -> None:
    resuming = args.resume_latest or args.resume_branch
    if args.branch_mode not in {"new", "reuse", "auto"}:
        raise SupervisorError("branch_mode must be 'new', 'reuse' or 'auto'.")
    if args.close_on not in {"merge", "commit"}:
        raise SupervisorError("close_on must be 'merge' or 'commit'.")
    if args.sync_base not in {"merge", "warn", "stop"}:
        raise SupervisorError("sync_base must be 'merge', 'warn' or 'stop'.")
    if not args.base_branch:
        raise SupervisorError("base_branch must name a branch on origin.")
    if not args.self_test and not resuming:
        if args.min_issue and args.max_issue and args.max_issue < args.min_issue:
            raise SupervisorError("--max-issue must be >= --min-issue.")
    if args.max_hours < 0:
        raise SupervisorError("--max-hours must be 0 or greater.")
    if args.stall_minutes < 1:
        raise SupervisorError("--stall-minutes must be at least 1.")
    if args.max_session_minutes < 5:
        raise SupervisorError("--max-session-minutes must be at least 5.")
    if args.max_no_progress_retries < 1:
        raise SupervisorError("--max-no-progress-retries must be at least 1.")
    if args.env_blocker_halt_after < 0:
        raise SupervisorError("--env-blocker-halt-after must be 0 or greater.")
    if args.heartbeat_seconds < 5:
        raise SupervisorError("--heartbeat-seconds must be at least 5.")


def main() -> int:
    args = build_arg_parser().parse_args()
    try:
        if args.self_test:
            return self_test()
        if args.list_projects:
            print("\n".join(list_projects(platform_root())) or "(no projects)")
            return 0
        if args.project:
            if args.config:
                raise SupervisorError("Use only one of --project / --config.")
            args.config = str(project_config_path(platform_root(), args.project))
        config, config_path = load_project_config(args.config, args.repo or ".")
        cli_repo = args.repo
        apply_config(args, config)
        args.blocker_rules = config.get("blockers") or []
        if config_path:
            print(f"Using project config: {config_path}", flush=True)
            args.config_path = str(config_path)
            # A relative sidecar path in the config resolves next to the config
            # file, so the workflow works from any launch directory.
            base = config_path.parent
            config_repo = str(config.get("repo", "") or "")
            if (cli_repo is None and config_repo
                    and not Path(config_repo).is_absolute()):
                args.repo = str((base / config_repo).resolve())
            for key in ("agent", "validate", "contract_file"):
                val = getattr(args, key)
                if val and not Path(val).is_absolute() and val not in BUILTIN_AGENTS:
                    beside = base / val
                    if beside.exists():
                        setattr(args, key, str(beside.resolve()))
        validate_args(args)
        return supervisor(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
        return 130
    except NoWorkError as exc:
        print_err(str(exc))
        emit_run_result(reason="no_work", status="finished", detail=str(exc))
        return 1
    except SupervisorError as exc:
        print_err(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

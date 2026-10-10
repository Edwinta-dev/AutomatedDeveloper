#!/usr/bin/env python3
"""
v2.py — one entry point for agenticworkflow_v2. Running any project is five steps:

    1. python v2.py new MyProj --repo C:/path/to/repo --test "npm test"          # software
       python v2.py new MyKaggle --ml --repo C:/path/to/repo --metric roc_auc    # ML
    2. write/vet MyProj/issues.yaml (and fill MyProj/project_rules.md)
    3. python v2.py check MyProj      # preflight + issue dry-run; fix what it lists
    4. python v2.py issues MyProj     # create the GitHub issues (idempotent)
    5. python v2.py run MyProj        # overnight: gates + adversary, usage-limit park/resume

    python v2.py status MyProj        # what happened: commits, ledger best, reviews
    python v2.py list                 # projects beside this script
    python v2.py usage                # Codex limit % + Claude tokens, from local logs

A project is one folder beside this script holding ONE config file
(issue-automation.config.json, including its "ml" and "adversary" sections), a
rules file (project_rules.md, injected into every prompt), issues.yaml, and the
agent/validate sidecars the templates (templates/software, templates/ml) provide.
Everything here delegates to engine/: run_issues.py (v1), create_issues.py,
session.py, ml_gate.py and adversary.py; those still work on their own for
anything unusual (python engine/run_issues.py ...).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent          # repo root: project folders live here
ENGINE = HERE / "engine"                         # run_issues.py & co. (__HARNESS__)
sys.path.insert(0, str(ENGINE))

import run_issues as v1  # noqa: E402
import session  # noqa: E402

IS_WINDOWS = os.name == "nt"
CONFIG_NAME = "issue-automation.config.json"
TEMPLATES = {"software": HERE / "templates" / "software", "ml": HERE / "templates" / "ml"}
TODO_TEST_EXE = "TODO-test-command"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def project_dir(name: str) -> Path:
    return HERE / name


def config_path(name: str) -> Path:
    try:
        return v1.project_config_path(HERE, name)
    except v1.SupervisorError as exc:
        raise SystemExit(f"ERROR: {exc}\nCreate it with: python v2.py new {name} --repo <path>")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def resolve_beside(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def todo_strings(obj, where: str = "") -> list[str]:
    """Paths of string values that still start with TODO (template placeholders)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not str(k).startswith("//"):
                out += todo_strings(v, f"{where}.{k}" if where else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += todo_strings(v, f"{where}[{i}]")
    elif isinstance(obj, str) and obj.strip().upper().startswith("TODO"):
        out.append(where)
    return out


def split_cmd(cmd: str) -> list[str]:
    return shlex.split(cmd, posix=not IS_WINDOWS)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "work"


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------

def cmd_new(args) -> int:
    if args.name[0] in "_." or not re.fullmatch(r"[A-Za-z0-9_.-]+", args.name):
        raise SystemExit("ERROR: project names use letters, digits, '-', '_', '.', "
                         "and must not start with '_' or '.'.")
    dest = project_dir(args.name)
    if dest.exists():
        raise SystemExit(f"ERROR: {dest} already exists. Edit it, or pick another name.")
    kind = "ml" if args.ml else "software"
    shutil.copytree(TEMPLATES[kind], dest,
                    ignore=shutil.ignore_patterns("AGENTS.branching.md", "__pycache__"))

    cfg = load_json(dest / CONFIG_NAME)
    if args.repo:
        repo = Path(args.repo).expanduser().resolve()
        cfg["repo"] = str(repo)
        if not repo.exists():
            print(f"NOTE: {repo} does not exist yet; clone it before `v2.py check`.")
    cfg["work_branch"] = f"automation/{slug(args.name)}"
    if args.model:
        cfg["model"] = args.model
    if kind == "ml":
        if args.metric:
            cfg["ml"]["metric"] = args.metric
        if args.minimize:
            cfg["ml"]["direction"] = "minimize"
    save_json(dest / CONFIG_NAME, cfg)

    if args.agent != "codex":
        agent = load_json(dest / "agent.json")
        spec = v1.BUILTIN_AGENTS[args.agent]
        agent.update(name=spec.name, exe_candidates=spec.exe_candidates, argv=spec.argv,
                     prompt_mode=spec.prompt_mode)
        agent.pop("//", None)
        save_json(dest / "agent.json", agent)

    if args.test:
        val = load_json(dest / "validate.json")
        for entry in val["always"]:
            if entry["argv"] and entry["argv"][0] == TODO_TEST_EXE:
                entry["label"] = "project test suite"
                entry["argv"] = split_cmd(args.test)
        save_json(dest / "validate.json", val)

    if args.issues:
        shutil.copyfile(Path(args.issues).expanduser(), dest / "issues.yaml")

    print(f"Created {kind} project {dest}\n")
    todo = [f"{CONFIG_NAME}: {t}" for t in todo_strings(load_json(dest / CONFIG_NAME))]
    if kind == "software" and not args.test:
        todo.append("validate.json: the project test command (or rerun `new` with --test)")
    print("Next:")
    step = 2
    if todo:
        print(f"  {step}. Fill in:\n" + "\n".join(f"       - {t}" for t in todo))
        step += 1
    print(f"  {step}. Write/vet  {dest / 'issues.yaml'}\n"
          f"     and the rules in {dest / 'project_rules.md'}")
    print(f"  {step + 1}. python v2.py check {args.name}\n"
          f"  {step + 2}. python v2.py issues {args.name}\n"
          f"  {step + 3}. python v2.py run {args.name}")
    return 0


# ---------------------------------------------------------------------------
# check (preflight)
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.ok: list[str] = []

    def print(self) -> None:
        for m in self.ok:
            print(f"  ok    {m}")
        for m in self.warnings:
            print(f"  WARN  {m}")
        for m in self.errors:
            print(f"  FIX   {m}")


def _git(repo: Path, *a: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *a], cwd=str(repo), text=True, encoding="utf-8",
                          errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def preflight(name: str, *, online: bool = True) -> tuple[Report, dict, Path]:
    r = Report()
    cpath = config_path(name)
    base = cpath.parent
    try:
        cfg = v1.load_config_file(cpath)
    except Exception as exc:  # noqa: BLE001
        r.errors.append(f"{cpath.name} does not load: {exc}")
        return r, {}, cpath
    for t in todo_strings(cfg):
        r.errors.append(f"{cpath.name}: fill in {t}")

    # Repo
    repo = resolve_beside(base, str(cfg.get("repo") or "."))
    base_branch = str(cfg.get("base_branch") or "main")
    if "repo" in todo_strings(cfg):
        pass                                    # already reported as a TODO above
    elif not repo.exists():
        r.errors.append(f"repo not found: {repo}")
    elif _git(repo, "rev-parse", "--show-toplevel").returncode != 0:
        r.errors.append(f"repo is not a git checkout: {repo}")
    else:
        r.ok.append(f"repo {repo}")
        if _git(repo, "remote", "get-url", "origin").returncode != 0:
            r.errors.append("repo has no 'origin' remote (issues live on its GitHub repo)")
        elif _git(repo, "rev-parse", "--verify", "-q", f"origin/{base_branch}").returncode != 0:
            r.warnings.append(f"origin/{base_branch} not fetched yet (run: git -C {repo} fetch)")
        if _git(repo, "status", "--porcelain").stdout.strip():
            r.warnings.append("repo has uncommitted changes; a NEW run needs a clean tree "
                              "(a resumed run is fine)")

    # Issues file
    issues_val = cfg.get("issues")
    if not issues_val:
        r.errors.append(f"{cpath.name}: set \"issues\" (e.g. issues.yaml)")
    else:
        ipath = resolve_beside(base, str(issues_val))
        if not ipath.exists():
            r.errors.append(f"issues file not found: {ipath}")
        else:
            try:
                import create_issues
                plan = create_issues.complete_plan(create_issues.load_plan(ipath))
                todo_titles = [i.title for i in plan.issues if i.title.upper().startswith("TODO")]
                if todo_titles:
                    r.errors.append(f"{ipath.name}: replace the template issues ({len(todo_titles)} "
                                    "titled TODO)")
                else:
                    r.ok.append(f"{ipath.name}: {len(plan.issues)} issue(s), "
                                f"{sum(1 for i in plan.issues if i.depends_on)} with dependencies")
            except Exception as exc:  # noqa: BLE001
                r.errors.append(f"{ipath.name} does not parse: {exc}")

    # Agent
    agent_val = str(cfg.get("agent") or "codex")
    agent_ref = agent_val if agent_val in v1.BUILTIN_AGENTS else str(resolve_beside(base, agent_val))
    try:
        spec, exe = v1.resolve_agent(agent_ref)
        r.ok.append(f"agent {spec.name} ({exe})")
        if "TODO" in spec.prompt_prefix:
            r.warnings.append("project_rules.md still has a TODO section (the agent sees it verbatim)")
    except Exception as exc:  # noqa: BLE001
        r.errors.append(f"agent: {exc}")

    # Validation chain
    vpath = resolve_beside(base, str(cfg.get("validate") or "")) if cfg.get("validate") else None
    if vpath is None:
        r.warnings.append("no validate file: only v1's default checks will gate commits")
    elif not vpath.exists():
        r.errors.append(f"validate file not found: {vpath}")
    else:
        vcfg = v1.load_validation_config(vpath)
        labels, last = [], []
        for entry in list(vcfg.always) + [c for rule in vcfg.rules for c in rule.get("commands", [])]:
            exe0 = str(entry["argv"][0]) if entry.get("argv") else ""
            (last if entry.get("skip_if_failed") else labels).append(entry.get("label", exe0))
            if exe0 == TODO_TEST_EXE:
                r.errors.append(f"{vpath.name}: set the project test command "
                                f"(the \"{entry.get('label')}\" entry)")
            elif exe0 not in ("__PY__", "git") and not shutil.which(exe0):
                r.errors.append(f"{vpath.name}: '{exe0}' is not on PATH ({entry.get('label')})")
            for tok in entry.get("argv", []):
                if isinstance(tok, str) and tok.startswith("__HARNESS__/"):
                    if not (ENGINE / tok[len("__HARNESS__/"):]).exists():
                        r.errors.append(f"{vpath.name}: {tok} does not exist in {ENGINE}")
        r.ok.append("gates, in order: " + " -> ".join(labels + [f"{x} (only if all passed)"
                                                                 for x in last]))

    # ML gate
    if "ml" in cfg:
        import ml_gate
        try:
            g = ml_gate.load_config(cpath)
            r.ok.append(f"ML gate: {g['direction']} {g['metric']}"
                        f"{', plausible_max ' + str(g['plausible_max']) if g['plausible_max'] is not None else ''}")
            if g["plausible_max"] is None and g["direction"] == "maximize":
                r.warnings.append("ml.plausible_max is unset: set it near the public leaderboard "
                                  "best so leakage-level scores are rejected")
            if Path(g["lock_file"]).exists():
                r.ok.append("protected test data is locked")
            else:
                r.ok.append("protected test data will be locked on first run / first passing experiment")
        except ml_gate.GateError as exc:
            r.errors.append(f"ml section: {exc}")

    # Adversary
    adv = cfg.get("adversary") or {}
    uses_adv = vpath is not None and vpath.exists() and "adversary.py" in vpath.read_text(encoding="utf-8")
    if uses_adv:
        if adv.get("enabled", True) is False:
            r.ok.append("adversary disabled in config")
        elif os.environ.get(adv.get("api_key_env", "GEMINI_API_KEY")):
            r.ok.append(f"adversary live ({adv.get('model', 'gemini-2.5-pro')}, preset "
                        f"{adv.get('preset', 'software')})")
        else:
            r.warnings.append(f"{adv.get('api_key_env', 'GEMINI_API_KEY')} not set: reviews run "
                              "UNREVIEWED (logged, never block)")

    # Project health checks ("preflight": [{"label", "argv", "timeout_seconds"}]), e.g. the
    # database the tests need is answering. A dead service otherwise burns every attempt.
    if repo.exists() and "repo" not in todo_strings(cfg):
        for entry in cfg.get("preflight") or []:
            label = entry.get("label") or " ".join(map(str, entry.get("argv", [])))
            argv = [sys.executable if a == "__PY__" else str(a) for a in entry.get("argv", [])]
            try:
                cp = subprocess.run(argv, cwd=str(repo), text=True, encoding="utf-8",
                                    errors="replace", stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    timeout=float(entry.get("timeout_seconds", 60)))
                if cp.returncode == 0:
                    r.ok.append(f"preflight: {label}")
                else:
                    r.errors.append(f"preflight failed: {label} (exit {cp.returncode}) "
                                    f"{cp.stdout.strip()[-300:]}")
            except subprocess.TimeoutExpired:
                r.errors.append(f"preflight timed out: {label} (a service may be hung)")
            except FileNotFoundError:
                r.errors.append(f"preflight: '{argv[0] if argv else '?'}' not found ({label})")

    # GitHub
    if online:
        gh = shutil.which("gh")
        if not gh:
            r.errors.append("gh (GitHub CLI) is not on PATH")
        elif subprocess.run([gh, "auth", "status"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode != 0:
            r.errors.append("gh is not authenticated (run: gh auth login)")
        else:
            r.ok.append("gh authenticated")
            if repo.exists() and "repo" not in todo_strings(cfg):
                cp = subprocess.run([gh, "repo", "view", "--json", "nameWithOwner", "-q",
                                     ".nameWithOwner"], cwd=str(repo), text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if cp.returncode == 0 and cp.stdout.strip():
                    r.ok.append(f"GitHub repo {cp.stdout.strip()}")
                else:
                    r.errors.append("gh cannot see this repo's GitHub repository (is origin a "
                                    "GitHub URL you can access?)")
    return r, cfg, cpath


def cmd_check(args) -> int:
    r, cfg, cpath = preflight(args.name, online=not args.offline)
    print(f"Preflight {args.name} ({cpath})")
    r.print()
    if not r.errors and cfg.get("issues") and not args.offline:
        print("\nIssue plan (dry run; existing issues show as SKIP):", flush=True)
        subprocess.run([sys.executable, str(ENGINE / "create_issues.py"), "--project", args.name,
                        "--dry-run", "--allow-nonempty"])
    if r.errors:
        print(f"\n{len(r.errors)} thing(s) to fix before `python v2.py run {args.name}`.")
        return 1
    print(f"\nReady. Next: python v2.py issues {args.name}   then   python v2.py run {args.name}")
    return 0


# ---------------------------------------------------------------------------
# issues / run / status / list
# ---------------------------------------------------------------------------

def cmd_issues(args, extra: list[str]) -> int:
    config_path(args.name)
    return subprocess.run([sys.executable, str(ENGINE / "create_issues.py"),
                           "--project", args.name, *extra]).returncode


def provider_for(cfg: dict, base: Path) -> str:
    agent_val = str(cfg.get("agent") or "codex")
    if agent_val in v1.BUILTIN_AGENTS:
        return agent_val
    try:
        return str(load_json(resolve_beside(base, agent_val)).get("name") or "agent")
    except Exception:  # noqa: BLE001
        return "agent"


def cmd_run(args, extra: list[str]) -> int:
    r, cfg, cpath = preflight(args.name)
    if r.errors:
        print(f"Preflight {args.name}: not ready")
        r.print()
        print(f"\nFix the above (python v2.py check {args.name}) and rerun.")
        return 1
    for w in r.warnings:
        print(f"WARN  {w}")

    if "ml" in cfg:
        import ml_gate
        g = ml_gate.load_config(cpath)
        repo = resolve_beside(cpath.parent, str(cfg["repo"]))
        if not Path(g["lock_file"]).exists() and ml_gate.expand_globs(repo, g["protected_data"]):
            lock = ml_gate.make_lock(repo, g)
            print(f"Locked {len(lock['files'])} protected data file(s) before the agent starts "
                  f"-> {g['lock_file']}")

    provider = args.provider or provider_for(cfg, cpath.parent)
    grace = args.grace_minutes if args.grace_minutes is not None else session.default_grace_minutes(cfg)
    if args.provider_probe:
        probe = split_cmd(args.provider_probe)
    elif provider == "codex":
        # Codex logs its 5h/weekly plan usage locally: check it before every slice.
        probe = [sys.executable, str(ENGINE / "usage.py"), "--probe", "codex",
                 "--max-percent", str(args.max_percent)]
    else:
        probe = None          # Claude: limits are only visible interactively; handled reactively
    if probe:
        print(f"usage probe before each slice: {' '.join(probe[1:])}")
    return session.run(
        runner=[sys.executable, str(ENGINE / "run_issues.py")], config=str(cpath), extra=extra,
        provider=provider, probe=probe,
        slice_minutes=args.slice_minutes, grace_minutes=grace,
        usage_fallback_minutes=args.usage_fallback_minutes, max_hours=args.max_hours,
        once=args.once, cwd=None, state_path=session.default_state_path(str(cpath)),
        fresh=args.fresh)


def latest_run_dir(repo: Path) -> Optional[Path]:
    root = v1.runs_root()
    best = None
    for sp in root.glob(f"*/{v1.STATE_FILE_NAME}") if root.exists() else []:
        try:
            data = load_json(sp)
            if Path(data.get("repo_path", "")).resolve() == repo.resolve():
                if best is None or sp.stat().st_mtime > best.stat().st_mtime:
                    best = sp
        except Exception:  # noqa: BLE001
            continue
    return best.parent if best else None


def cmd_status(args) -> int:
    cpath = config_path(args.name)
    cfg = v1.load_config_file(cpath)
    repo = resolve_beside(cpath.parent, str(cfg.get("repo") or "."))
    print(f"{args.name}  ({'ml' if 'ml' in cfg else 'software'})  repo {repo}")

    sp = session.default_state_path(str(cpath))
    if sp.exists():
        st = load_json(sp)
        committed = st.get("stats", {}).get("committed", [])
        cool = {k: v for k, v in (st.get("cooldowns") or {}).items() if v}
        print(f"session:  {st.get('stats', {}).get('slices', 0)} slice(s), committed "
              f"{', '.join(f'#{n}' for n in committed) or 'none'}"
              + (f"; parked {cool}" if cool else ""))
    else:
        print("session:  never run")

    run_dir = latest_run_dir(repo) if repo.exists() else None
    if run_dir:
        state = load_json(run_dir / v1.STATE_FILE_NAME)
        print(f"last run: {state.get('status')} on {state.get('branch')}, "
              f"deferred {sorted(state.get('deferred', {}), key=int) or 'none'}")
        summary = run_dir / v1.SUMMARY_FILE_NAME
        if summary.exists():
            print(f"summary:  {summary}")
        log = run_dir / "adversary_log.jsonl"
        if log.exists():
            counts: dict[str, int] = {}
            for line in log.read_text(encoding="utf-8").splitlines():
                v = json.loads(line).get("verdict", "?")
                counts[v] = counts.get(v, 0) + 1
            print(f"reviews:  {', '.join(f'{k} {n}' for k, n in sorted(counts.items()))}")

    if "ml" in cfg and repo.exists():
        import ml_gate
        try:
            g = ml_gate.load_config(cpath)
            best = ml_gate.ledger_at_head(repo, g).get("best")
            print(f"best:     {g['metric']} = {best['value']} (issue #{best.get('issue')})"
                  if best else f"best:     none yet (baseline {g['baseline']})")
        except ml_gate.GateError as exc:
            print(f"ml:       {exc}")
    return 0


def cmd_digest(args) -> int:
    cpath = config_path(args.name)
    cfg = v1.load_config_file(cpath)
    repo = resolve_beside(cpath.parent, str(cfg.get("repo") or "."))
    import digest
    if getattr(args, "run", None):                     # one run only
        out = digest.write_digest(args.run, single=True)
    else:                                              # every run since the last merge
        run_dir = latest_run_dir(repo) if repo.exists() else None
        if run_dir is None:
            print(f"no runs found for {repo}")
            return 1
        out = digest.write_digest(str(run_dir), single=False)
    if getattr(args, "print", False):
        print(out.read_text(encoding="utf-8"))
    print(f"Digest: {out}")
    return 0


def cmd_list(_args) -> int:
    names = v1.list_projects(HERE)
    if not names:
        print("(no projects; create one with: python v2.py new <Name> --repo <path>)")
    for n in names:
        try:
            cfg = v1.load_config_file(v1.project_config_path(HERE, n))
            kind = "ml" if "ml" in cfg else "software"
            print(f"{n:24} {kind:9} {cfg.get('repo', '')}")
        except Exception:  # noqa: BLE001
            print(f"{n:24} (config does not load)")
    return 0


# ---------------------------------------------------------------------------
# Self-test (offline)
# ---------------------------------------------------------------------------

def self_test() -> int:
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}")
        ok &= bool(cond)

    check("todo scan", todo_strings({"repo": "TODO: x", "//": "TODO", "ml": {"metric": "auc"},
                                     "l": ["TODO y"]}) == ["repo", "l[0]"])
    check("slug", slug("My Kaggle_Proj") == "my-kaggle-proj")
    for kind, tpl in TEMPLATES.items():
        cfg = load_json(tpl / CONFIG_NAME)
        check(f"{kind} template: issues.yaml + rules file exist",
              (tpl / cfg["issues"]).exists() and (tpl / "project_rules.md").exists())
        agent = load_json(tpl / "agent.json")
        check(f"{kind} template: rules injected via prompt_prefix file",
              agent.get("prompt_prefix") == {"file": "project_rules.md"})
        val = (tpl / "validate.json").read_text(encoding="utf-8")
        check(f"{kind} template: adversary wired to the project config",
              "__HARNESS__/adversary.py" in val and "__PROJECT_CONFIG__" in val)

    # `new` end to end into a temp harness copy is covered by the e2e script; here,
    # exercise the edits new() makes on a scratch folder.
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "P"
        shutil.copytree(TEMPLATES["software"], dest)
        val = load_json(dest / "validate.json")
        for entry in val["always"]:
            if entry["argv"] and entry["argv"][0] == TODO_TEST_EXE:
                entry["argv"] = split_cmd('python -m pytest -q "tests dir"')
        check("--test command split",
              any(e["argv"][-1] in ("tests dir", '"tests dir"') for e in val["always"]))
    print("\n" + ("ALL V2 SELF-TESTS PASSED" if ok else "SOME V2 SELF-TESTS FAILED"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="v2.py", description="agenticworkflow_v2: new -> (write issues.yaml) -> check -> issues -> run.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("new", help="create a project folder from a template")
    p.add_argument("name")
    p.add_argument("--repo", help="absolute path to the project's git checkout")
    p.add_argument("--ml", action="store_true", help="ML/Kaggle experiment project (ML gate + ML adversary)")
    p.add_argument("--agent", choices=sorted(v1.BUILTIN_AGENTS), default="codex")
    p.add_argument("--model", help="model for the agent (blank = agent default)")
    p.add_argument("--test", help='software: the test command that gates every commit, e.g. "npm test"')
    p.add_argument("--metric", help="ML: the metric results report, e.g. roc_auc")
    p.add_argument("--minimize", action="store_true", help="ML: lower is better (rmse, logloss)")
    p.add_argument("--issues", help="start from an existing issues .yaml/.json/.md file")

    p = sub.add_parser("check", help="preflight: config, repo, tools, auth, gates + issue dry-run")
    p.add_argument("name")
    p.add_argument("--offline", action="store_true", help="skip gh auth and the GitHub dry run")

    p = sub.add_parser("issues", help="create the project's GitHub issues (flags pass to create_issues.py)")
    p.add_argument("name")

    p = sub.add_parser("run", help="run overnight (flags you don't list here pass to run_issues.py)")
    p.add_argument("name")
    p.add_argument("--provider", help="usage bookkeeping name (default: from the agent)")
    p.add_argument("--provider-probe", help="command printing {\"ok\":bool,\"reset\":...} "
                   "(default for codex: usage.py --probe codex)")
    p.add_argument("--max-percent", type=float, default=97.0,
                   help="codex: pause when the 5h or weekly limit is at least this full (default 97)")
    p.add_argument("--slice-minutes", type=int, default=50)
    p.add_argument("--grace-minutes", type=int, default=None)
    p.add_argument("--usage-fallback-minutes", type=int, default=60)
    p.add_argument("--max-hours", type=float, default=0.0, help="total budget (0 = until done)")
    p.add_argument("--once", action="store_true", help="one slice only")
    p.add_argument("--fresh", action="store_true", help="forget this project's session state")

    p = sub.add_parser("status", help="commits, deferrals, reviews, ML best")
    p.add_argument("name")
    p = sub.add_parser("digest", help="write DIGEST.md: all runs on the work branch since its "
                                      "last merge into the base (or one run with --run)")
    p.add_argument("name")
    p.add_argument("--run", help="run id or run dir: digest that run only")
    p.add_argument("--print", action="store_true", help="also print the digest")
    sub.add_parser("list", help="projects beside this script")
    sub.add_parser("usage", help="Codex plan-limit %% and Claude Code token usage, from local logs")
    sub.add_parser("self-test", help="offline checks")

    args, extra = ap.parse_known_args()
    if args.cmd in ("issues", "run"):
        pass
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    if args.cmd == "new":
        return cmd_new(args)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "issues":
        return cmd_issues(args, extra)
    if args.cmd == "run":
        return cmd_run(args, extra)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "digest":
        return cmd_digest(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "usage":
        import usage
        print(usage.usage_report())
        return 0
    if args.cmd == "self-test":
        return self_test()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

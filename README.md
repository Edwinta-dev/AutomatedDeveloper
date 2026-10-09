# AutomatedDeveloper

**Hand a coding agent a backlog of GitHub issues, go to sleep, and wake up to a branch of reviewed, tested commits: one per issue.**

AutomatedDeveloper is a small set of Python scripts that supervise a coding agent (OpenAI Codex, Claude Code, Aider, or any CLI) while it works through a repository's GitHub issues unattended. The agent writes the code. The supervisor doesn't trust its claims: it checks every change with your own tests and commits only what passes. Nothing is pushed or merged without you.

```
 issues.yaml ──► GitHub issues ──► agent edits code ──► gates check it ──► 1 commit per issue ──► you review the PR
   (you)          (create)          (Codex/Claude)      (tests, ML gate,      (work branch)          (merge)
                                                         adversary veto)
```

---

## Contents

- [Why use it](#why-use-it)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [The recommended workflow](#the-recommended-workflow)
- [Writing a good backlog](#writing-a-good-backlog)
- [Safety: read this before your first run](#safety-read-this-before-your-first-run)
- [Repository layout](#repository-layout)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [Reference documentation](docs/REFERENCE.md)

---

## Why use it

Coding agents are good at well-specified tasks but unreliable judges of their own work. Left alone they will "finish" an issue with failing tests, edit the test instead of the code, or burn hours retrying something that can't work because a tool isn't installed. AutomatedDeveloper wraps the agent in rules that fix those failure modes:

- **Your tests decide, not the agent.** After the agent reports success, the supervisor re-runs your validation commands itself. A commit happens only if every one passes.
- **One issue, one commit.** Each commit is small, references its issue (`Closes #N`), and is easy to review or revert.
- **It runs all night.** Long runs are split into slices. If the agent hits its usage limit, the run pauses until the limit resets and then continues where it left off.
- **It knows when to stop.** Missing SDKs, Docker not running, no GPU: environment problems are detected by rule and the issue is parked instead of retried forever. You get a list of what to install.
- **You stay in control of `main`.** All work lands on a single work branch. You review it and open the pull request.

## How it works

Three roles keep the agent honest. Only deterministic code can approve a change.

| Role | Who | Power |
|---|---|---|
| **Improver** | The coding agent (Codex, Claude Code, ...) | Proposes a change for one issue |
| **Gate** | Your validation commands: tests, lint, and for ML projects `ml_gate.py` | **Can approve** a commit |
| **Adversary** *(optional)* | `adversary.py`, a cheap second model (Gemini) that reads the diff | **Can only veto.** A pass is not an approval. Off by default for software projects, on for ML |

A mistaken or prompt-injected reviewer can therefore cause at most a false alarm, never a bad commit.

The adversary is optional. Every check that can block a commit is deterministic, so the system is complete without it. Without it you lose one check: whether the claims in a decision record are actually *true*. The record check still confirms they *match the diff by name*. That matters most for ML work, where results can look better than they are, so ML projects have it on by default. For routine software work it's mostly extra token spend, so it starts off. Turn it on per project with `"adversary": {"enabled": true}` and a `GEMINI_API_KEY`. When an attempt is rejected, the reasons go into the agent's next prompt so it can fix them.

Issues are worked in a fixed, predictable order: lowest issue number first, filtered by your label, milestone and range settings, skipping any issue whose dependencies are still open.

## Requirements

| Need | Notes |
|---|---|
| Python 3.10+ | Standard library only, plus PyYAML if your backlog is `.yaml` (`pip install pyyaml`) |
| `git` | |
| GitHub CLI `gh` | Logged in: `gh auth login` |
| A coding agent CLI | `codex` (default), `claude`, or `aider`, installed and logged in |
| `GEMINI_API_KEY` | *Optional.* Only needed when the adversarial reviewer is enabled (the default for ML projects). Without it, reviews are skipped and logged as `UNREVIEWED` |

Works on Windows, macOS and Linux.

## Quick start

```bash
git clone https://github.com/Edwinta-dev/AutomatedDeveloper.git
cd AutomatedDeveloper
python v2.py self-test        # offline sanity check, no network or keys needed
```

Then set up your first project in five commands:

```bash
# 1. Create a project folder pointing at your repo and its test command
python v2.py new MyApp --repo C:/code/my-app --test "npm test"

# 2. Write the backlog and the rules (open these files in your editor)
#      MyApp/issues.yaml       what to build
#      MyApp/project_rules.md  rules the agent must always follow

# 3. Preflight: checks config, repo, agent, gh login, and dry-runs the issues
python v2.py check MyApp

# 4. Create the GitHub issues (safe to re-run: existing titles are skipped)
python v2.py issues MyApp

# 5. Start the run and leave it
python v2.py run MyApp
```

For a machine-learning or Kaggle-style project, add `--ml` and the metric to optimise:

```bash
python v2.py new MyKaggle --repo C:/code/my-kaggle --ml --metric roc_auc   # add --minimize for rmse, logloss, ...
```

To use Claude Code instead of Codex, add `--agent claude` (and optionally `--model <name>`) to `new`.

## The recommended workflow

**Use `v2.py` for everything.** It's the one supported entry point. The scripts in `engine/` are the building blocks it calls. They still work on their own for unusual cases, but `v2.py` runs the preflight checks and wires them together correctly.

```
new ──► write issues.yaml + project_rules.md ──► check ──► issues ──► run ──► review & merge ──► (repeat)
```

1. **`new`** copies a template into a project folder next to the scripts.
2. **Write the backlog and rules.** This is where most of your effort should go (see the next section).
3. **`check`** lists everything still to fix (`FIX`) and anything worth knowing (`WARN`). `run` refuses to start until `check` is clean.
4. **`issues`** creates the GitHub issues, labels and milestones.
5. **`run`** works the issues until all are done, or until it needs you. Watch progress any time with `python v2.py status MyApp`.
6. **Review and merge.** When the run ends it prints the exact commands to use:
   ```bash
   git log --oneline origin/main..automation/work    # what was done
   git push -u origin automation/work
   gh pr create --base main --head automation/work --fill
   ```
   Merge the PR **with a merge commit** (not squash), so the next run can fast-forward the work branch and continue.
7. **Repeat.** Add new issues to the backlog, then run `issues` and `run` again.

Other useful commands:

| Command | What it does |
|---|---|
| `python v2.py list` | List your projects |
| `python v2.py status MyApp` | Commits, parked issues, review verdicts, best ML score |
| `python v2.py usage` | Codex plan-limit %, Claude Code token usage |
| `python v2.py run MyApp --once` | Run one slice only (good for a first test) |
| `python v2.py run MyApp --max-hours 8` | Cap the total run time |
| `python v2.py run MyApp --max-issue 20` | Unknown flags are passed through to `engine/run_issues.py` |
| `python v2.py issues MyApp --dry-run` | Show what would be created, without creating it |

You can run several projects at once in separate terminals. Give them different agents so one hitting its usage limit doesn't stall the others.

## Writing a good backlog

The agent is only as good as the issues you give it. Each issue in `issues.yaml` should be small enough for one sitting and should say:

- **Context:** why the issue exists.
- **Requirements:** a checklist of what must change, naming the files where you can.
- **Acceptance:** a single command that passes when the issue is done, e.g. `python -m pytest -q tests/test_login.py`.

```yaml
issues:
  - title: "Add password reset endpoint"
    milestone: "M1"
    labels: ["size:M"]
    depends_on: ["#2"]          # the 2nd issue IN THIS FILE, not GitHub issue #2
    body: |
      ## Context
      Users who forget their password have no way back in.

      ## Requirements
      - [ ] POST /auth/reset sends a reset email (src/auth/routes.py)
      - [ ] Tokens expire after 30 minutes

      ## Acceptance
      `python -m pytest -q tests/test_reset.py` passes.
```

`project_rules.md` holds the rules that apply to *every* issue (stack, banned dependencies, files that must never change). It's injected into every prompt automatically.

ML projects treat each issue as one experiment. The first issue in the ML template freezes the data split and evaluation script. Review that commit carefully before merging, because every later result is judged against it.

## Safety: read this before your first run

> **The agent runs with no approval prompts.** Codex runs with `--ask-for-approval never` inside its workspace sandbox. Claude Code runs with `--permission-mode bypassPermissions`. This is what lets them run tests, builds and training overnight, but it also means they can run any command. **Use a machine or account dedicated to this**, and never point it at a repo containing secrets you can't afford to leak.

Other safeguards are built in:

- Nothing is pushed, merged, or turned into a PR unless you ask (`push` and `open_pr` default to `false`).
- Changed files that look like secrets or build output (`.env`, `*.pem`, `node_modules/`, ...) block the commit. See `validate.json`.
- Unfinished work isn't thrown away. It's saved as the git tag `deferred/issue-<N>` and can be restored with `git stash apply deferred/issue-<N>`.

## Repository layout

```
AutomatedDeveloper/
├── v2.py                  ★ the entry point: new / check / issues / run / status / list / usage
├── engine/                the building blocks v2.py drives
│   ├── run_issues.py      the supervisor: one agent attempt per issue, validate, commit
│   ├── create_issues.py   turns issues.yaml into GitHub issues, labels and milestones
│   ├── session.py         keeps a run alive overnight (slices, usage-limit pause/resume)
│   ├── adversary.py       veto-only AI reviewer
│   ├── ml_gate.py         deterministic gate for ML experiments
│   ├── blockers.py        detects environment problems that retrying can't fix
│   └── usage.py           usage-limit tracking for each agent
├── templates/
│   ├── software/          copied by `v2.py new`
│   └── ml/                copied by `v2.py new --ml`
├── integrity/             scope gate and component index (standalone; see integrity/README.md)
├── examples/              example adversary and validation configs
├── docs/REFERENCE.md      full configuration and internals reference
├── <YourProject>/         your projects (git-ignored; local to your machine)
└── _archive/              retired local files (git-ignored)
```

Any top-level folder containing an `issue-automation.config.json` is a project (folders starting with `_` or `.` are always skipped). Project folders are **git-ignored by default**, because they contain absolute paths and private backlogs. To version one, add a `!/<Name>/` line to `.gitignore`.

Run state and logs are kept outside the repo, in `%LOCALAPPDATA%\issue-runner\runs` on Windows and `$XDG_STATE_HOME/issue-runner/runs` elsewhere.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `run` refuses to start | Run `python v2.py check MyApp` and fix every `FIX` line |
| Run stopped with `env_blocked` | A tool or SDK is missing. The summary says what to install. Install it, then `run` again: parked issues resume automatically |
| Run stopped with `all_deferred` / `all_blocked` | Every remaining issue failed or is waiting on another one. Check `status`, fix or rewrite the issues, then `run` again |
| "Stale base" error | The supervisor fetches `origin` first and won't start from an out-of-date base. Check your network and `gh auth status` |
| Merge conflict when syncing with `main` | The merge is aborted and nothing changes. Resolve it on the work branch by hand, then `run` again |
| Reviews show `UNREVIEWED` | `GEMINI_API_KEY` isn't set, or the Gemini API was unreachable. Commits still need your tests to pass |
| Agent hit its usage limit | Nothing to do. The run pauses until the reset time and continues |

Interrupted run? `python v2.py run MyApp` resumes it. `--fresh` clears the session state (pause timers, crash counts) first.

## Roadmap

**Direction: keep a human in the loop without them reading code.** Agents can work for hours unattended. When you come back, the documentation should tell you what was built, why it was built that way, and what was traded off. You can then steer the next round, by proposing a different trade-off or naming the edge case behind a bug, without opening a source file. Each change stays isolated and reversible, so a wrong turn can be undone cleanly.

This direction comes from a study of 232 past runner commits ([results](docs/EVALUATION.md#results-so-far)):
- **Drift is rare.** Agents seldom made gratuitous changes; out-of-scope edits were mostly legitimate ripple effects.
- **The real damage needed a narrow check.** It came from deleting code that other files still used, which is now caught by a dedicated check.
- **Token savings were never the strong case**, and are no longer a goal.

| Step | Status |
|---|---|
| One validated commit per issue, unfinished work kept as tags | Done |
| **Scope gate:** records which changes fell outside the issue's declared scope (report mode) | Done |
| **Dangling reference check:** blocks deleting code that other code still uses | Done |
| **Decision records:** the agent writes `docs/decisions/NNNN-slug.md` per issue (approach, alternatives, trade-offs, assumptions, edge cases, rollback). [Format](docs/DECISION_RECORDS.md) | Agent part done |
| **Verified facts in each record:** components changed with their tags, tests run, gate verdicts, added by the tool | Done |
| **Consistency check:** flags a rationale that doesn't match the diff (deterministic check plus the adversary) | Done |
| **Run digest:** one page on return, covering what was done and which decisions need a human look (`v2.py digest`) | Done |
| **Component history:** every decision grouped by component or tag, read as a design history | Planned |
| **Pilot and evaluate** on a real project: can a person answer design questions and steer changes from the records alone? ([Study 4](docs/EVALUATION.md#study-4-documentation-for-a-human-in-the-loop)) | Planned |
| **Beyond source code:** the same record, isolate and verify loop for spreadsheets, documents and slides, through format adapters | Later |
| **Housekeeping:** drop the v1/v2 naming, make `engine/` internal, add worked examples and a backlog-writing guide | Ongoing |

## Reference documentation

Configuration keys, validation tokens, branching modes, environment-blocker rules, the ML gate's checks, and how sessions resume are all in **[docs/REFERENCE.md](docs/REFERENCE.md)**.

Run the offline test suites at any time:

```bash
python v2.py self-test
python engine/run_issues.py --self-test
python engine/adversary.py --self-test
python engine/ml_gate.py --self-test
python engine/usage.py --self-test
python engine/session.py --self-test
```

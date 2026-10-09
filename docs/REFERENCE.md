# Reference

The [README](../README.md) covers everyday use. This page is the detail behind it: every config file, how branching and validation work, and what each script does on its own.

- [Project files](#project-files)
- [The project config](#the-project-config)
- [Branching](#branching)
- [Choosing the next issue](#choosing-the-next-issue)
- [Validation (the commit gate)](#validation-the-commit-gate)
- [The adversarial reviewer](#the-adversarial-reviewer)
- [The ML gate](#the-ml-gate)
- [Scope gate](#scope-gate)
- [Dangling reference check](#dangling-reference-check)
- [Environment blockers](#environment-blockers)
- [Overnight sessions and usage limits](#overnight-sessions-and-usage-limits)
- [Creating issues](#creating-issues)
- [Using the scripts directly](#using-the-scripts-directly)

---

## Project files

A project is a top-level folder (beside `v2.py`) holding an `issue-automation.config.json`. `v2.py new` creates it from `templates/software/` or `templates/ml/`.

| File | Edit it? | Purpose |
|---|---|---|
| `issue-automation.config.json` | **Yes**, this is the main config | Repo path, branching, run limits, plus the `ml`, `adversary` and `scope` sections |
| `issues.yaml` | **Yes** | The backlog (`.yaml`, `.json` or `.md`) |
| `project_rules.md` | **Yes** | Rules injected near the top of every issue prompt (including the [decision record](DECISION_RECORDS.md) format the agent must fill in) |
| `validate.json` | Rarely | The commit gate. `new --test` fills in the test command |
| `agent.json` | Rarely | How the agent CLI is invoked |
| `AGENTS.branching.md` | Optional | Branch policy to paste into the target repo's `AGENTS.md` / `CLAUDE.md`, so interactive agents follow the same rules as the runner |

Relative paths in the config resolve next to the config file. Any command-line flag overrides the matching config value for that run. Run settings may sit at the top level or inside the `run` section; `run` wins if both are set.

## The project config

`issue-automation.config.json`:

| Key | Default | Meaning |
|---|---|---|
| `repo` | (required) | Absolute path to the project's git checkout |
| `issues` | `issues.yaml` | Backlog file |
| `agent` | `agent.json` | A builtin name (`codex`, `claude`, `aider`) or an agent sidecar file |
| `model` | `""` | Model passed to the agent. Blank means the agent's default |
| `validate` | `validate.json` | Commit-gate file |
| `contract_file` | `""` | Optional file with extra safety-contract text for the agent |
| `base_branch` | `main` | Branch to fork from and merge back into |
| `work_branch` | `automation/work` | The single long-lived work branch |
| `branch_mode`, `close_on`, `sync_base` | `auto`, `merge`, `merge` | See [Branching](#branching) |
| `run.min_issue` / `run.max_issue` | `1` / `0` | Issue-number range (`0` = no upper limit) |
| `run.labels`, `run.labels_all` | `""`, `false` | Comma-separated labels. Matches *any* label unless `labels_all` |
| `run.milestone`, `run.exclude` | `""` | Milestone filter. Comma-separated issue numbers to skip |
| `run.max_hours` | `4` | Wall-clock cap for one `run_issues.py` run (`0` = unlimited) |
| `run.stall_minutes` | `20` | Kill an agent that produces no output for this long |
| `run.max_session_minutes` | `45` | Maximum time for one agent attempt |
| `run.max_no_progress_retries` | `3` | Attempts per issue before it is deferred |
| `run.push`, `run.open_pr` | `false` | Push the branch / open a PR at the end of the run |
| `env_blocker_halt_after` | `3` | Stop the run when one blocker has parked this many issues (`0` = never) |
| `blockers` | `[]` | Project-specific blocker rules. See [Environment blockers](#environment-blockers) |
| `adversary` | | See [The adversarial reviewer](#the-adversarial-reviewer) |
| `ml` | | ML projects only. See [The ML gate](#the-ml-gate) |
| `scope` | `mode: report` | Scope-gate settings. See [Scope gate](#scope-gate) |

### Agent sidecar (`agent.json`)

Describes how to launch the agent. `{EXE}`, `{MODEL}` and `{LAST_MSG}` are filled in by the supervisor.

| Key | Meaning |
|---|---|
| `exe_candidates` | Executable names tried in order (e.g. `codex.cmd`, `codex.exe`, `codex`) |
| `argv` | The full command line |
| `prompt_mode` | `stdin` to pipe the prompt in |
| `result_begin` / `result_end` | Markers around the agent's final result block (`STATUS:`, `BLOCKER:`, `NOTES:` ...) |
| `prompt_prefix` | Text, or `{"file": "project_rules.md"}`, placed near the top of every prompt |
| `prompt_suffix` | Text placed just before the result block, e.g. "run the full test suite before declaring COMPLETE" |

## Branching

The defaults suit a solo developer: the repo has `main` plus one work branch, and one PR at a time.

| Setting | Default | Behaviour |
|---|---|---|
| `branch_mode` | `auto` | If a non-base branch is checked out, continue on it. On the base branch, continue `work_branch` (local, then origin), or create it from `origin/<base>`. `reuse` always uses `work_branch`. `new` insists on a fresh branch |
| `sync_base` | `merge` | A branch whose PR was merged fast-forwards to the base. A branch that's behind the base has the base merged in. On a conflict the merge is aborted, nothing changes and the run stops. `warn` only reports. `stop` refuses to run |
| `close_on` | `merge` | Each commit says `Closes #N.`, so the issue closes when the PR merges. A closed issue therefore always means "on main". Issues already committed on the branch are skipped. `commit` closes the issue immediately instead |
| `push`, `open_pr` | `false` | The run ends by printing the review, push and PR commands instead |

Every run fetches `origin` first and refuses to start from a stale base.

**The loop:** run → review `git log --oneline origin/main..<branch>` → push → `gh pr create` → merge **with a merge commit** (not squash) → the next run fast-forwards and continues.

**Unfinished work** for an issue is kept as the tag `deferred/issue-<N>`, not as a stash or a branch. Restore it with `git stash apply deferred/issue-<N>`.

## Choosing the next issue

The order is deterministic:

1. Open issues within `min_issue`..`max_issue`.
2. Filtered by labels (any, or all with `labels_all`), milestone, and `exclude`.
3. Sorted by ascending issue number.
4. Skipping any issue whose `**Depends on:** #a, #b` issues are still open and not yet committed on the work branch. `--ignore-dependencies` turns this off. Skipped issues are listed in the run summary.

## Validation (the commit gate)

After the agent reports success, the supervisor runs `validate.json` itself, with the target repo as the working directory. Nothing is committed unless every command exits 0.

**Order:** `always` commands first, then `rules` whose `when_touched` globs match a changed file, then commands marked `"skip_if_failed": true` (in the templates: the scope check, then the adversary), in listed order, and those only if everything before them passed. So no paid review runs on code that already failed its tests.

**`suspicious`:** changed files matching `deny_globs` (and not `allow_globs`), or larger than `max_file_mb`, block the commit.

**Per-command options:** `"skip_if_failed": true` (described above) and `"timeout_minutes": N`.

**Tokens** you can use inside `argv`:

| Token | Replaced with |
|---|---|
| `__PY__` | The supervisor's Python interpreter |
| `__CHANGED_DIRS__` | Runs the command once per changed directory |
| `__HARNESS__` | The `engine/` folder (so `__HARNESS__/adversary.py` works) |
| `__CONFIG_DIR__` | The validate file's folder |
| `__PROJECT_CONFIG__` | The project config file |
| `__RUN_DIR__` | This run's state folder |

**Environment variables** passed to each command: `AGENT_ISSUE_NUMBER`, `AGENT_ISSUE_TITLE`, `AGENT_ISSUE_FILE`, `AGENT_RESULT_FILE`, `AGENT_ATTEMPT`, `AGENT_ATTEMPT_STARTED`, `AGENT_RUN_DIR`, and related `AGENT_*` variables.

## The adversarial reviewer

`adversary.py` sends the issue text, the agent's result block, the diff and the contents of new files to a cheap, high-context model (Gemini by default) and asks it to find problems.

- **Exit 0:** no objection. This is *not* approval.
- **Exit 3:** veto. The reasons are fed into the agent's next attempt.
- **Exit 0 + `UNREVIEWED`:** no API key, or the API is down. It steps aside, and this is logged to `<run dir>/adversary_log.jsonl`. An outage never counts as approval.

It reads the model's *last* verdict block, so an echoed answer template is never mistaken for a veto.

Settings live in the config's `adversary` section: `enabled`, `preset` (`software` or `ml`), `model`, and `priors` (extra context, such as the public leaderboard range for a Kaggle task). The key is read from `GEMINI_API_KEY`. Networking uses only the standard library and honours `HTTPS_PROXY`.

To add the adversary to an older project, append the entry from `examples/validate.with-adversary.example.json` as the last command in its validate file. `examples/adversary.config.example.json` and `examples/adversary.config.ml.example.json` show a standalone configuration.

## The ML gate

For ML projects, each issue is one experiment, and `ml_gate.py` is the part that can approve it. Configure it in the config's `ml` section. It fails an attempt when:

| Check | Fails when |
|---|---|
| Protected paths | An eval script, metric or split config that exists at `HEAD` is changed or deleted. Creating one is allowed, which is how the setup issue works |
| Protected data | Test/holdout data (usually git-ignored) no longer matches its sha256 lock. `run` locks it before the agent starts if it exists; otherwise the first passing experiment does. The lock lives in the project folder, out of the agent's reach |
| Results contract | `results_file` is missing, names a different metric, has a non-finite value, or lacks `required_fields` (e.g. `seed`) |
| Freshness | The results file predates this attempt |
| Reproduction | The gate re-runs the frozen eval (`reproduce.argv`) and gets a different number. The gate's own number is the one recorded |
| Plausibility | The score is above `plausible_max` (≈ the public leaderboard best) or jumps more than `max_jump` in one step. Both are treated as leakage |
| Improvement | It doesn't beat the best committed result by `min_delta` (`require_improvement`; set `false` to also commit negative results) |

On a pass it writes `experiments/ledger.json` (best score and history) into the commit. The ledger is rebuilt from `HEAD` every time, so neither an agent editing it nor a vetoed attempt can inflate the best score. The ML adversary then looks for what code can't see: leakage paths in new code, a result this diff couldn't produce, and tuning against validation data.

Exit codes: `0` pass, `1` fail, `2` misconfigured.

```bash
python engine/ml_gate.py --config <cfg> --repo <repo> --lock     # (re)hash protected data
python engine/ml_gate.py --config <cfg> --repo <repo> --status   # show the best result on record
```

## Scope gate

Each issue may declare which code components it is allowed to change. The scope gate (`integrity/integrity.py gate`) is a deterministic check that the attempt's diff stayed inside that list. No model is involved.

**Declaring scope.** Give an issue a `scope:` list in `issues.yaml` (or JSON). Each entry is one of:

| Entry | Means |
|---|---|
| `src/hsv.py` | That file |
| `src/vision/**` | A glob of files |
| `src/hsv.py::analyse_frame`, `src/hsv.py::HSV.threshold` | One function, class or method (`path::Qualname`) |

`create_issues.py` renders it as one line after `**Depends on:**`, e.g. ``**Scope:** `src/hsv.py::analyse_frame`, `tests/test_hsv.py` ``. An issue without `scope:` gets no line. Backslashes are normalised to `/` with a warning. `--update` adds, rewrites or removes the line on existing issues to match the file.

**Modes** (config `scope.mode`):

- **`report`** (default): records out-of-scope changes and never blocks.
- **`enforce`**: an out-of-scope change fails the gate (exit 1), so nothing is committed. So does an *unverified* file, one the adapter couldn't parse, because missing verification never counts as a pass.

**Allowances:** never out of scope:

- files matching `allow_globs`. By default that's tests in Python, JS, Dart, PHP and Go, plus docs (`**/*.md`, `docs/**`) and `.env.example`;
- import changes (`allow_imports`), including `try:` / `if TYPE_CHECKING:` blocks that contain only imports;
- new files (`allow_new_files`);
- new functions, classes or constants in scoped files (`allow_new_components`).

Module-level constants are components too, so a scope can name `path.py::TABLE_COLUMNS`.

**Protected paths** (`protect_globs`): `CLAUDE.md`, `AGENTS.md`, `.claude/`, `.codex/`, `.mcp.json` and `*.bak` / `*.orig` files are always out of scope unless the scope names them explicitly. They override every allowance, because an agent editing its own instructions or committing tool settings is never a side effect of an issue.

**Use report mode.** A study of 230 past commits found that enforcing scope would wrongly block about 45% of commits, mostly for legitimate ripple effects. Scope is most useful as a record of what changed and why. See [EVALUATION.md](EVALUATION.md#results-so-far).

**SCOPE_NOTES.** When a change outside the scope is unavoidable, the agent adds a `SCOPE_NOTES:` section to its result block, one `- path::component: why` line per change. Notes are recorded beside the finding. They never authorise anything: in `enforce` mode an out-of-scope change still fails.

**Records** go to `<run dir>/integrity/` as `scope_issue-<N>_attempt-<K>.json` (for `engine/compare_runs.py`) and `.md` (for people).

**Other commands.** `python integrity/integrity.py gate --repo <repo> --commit <sha> --scope "<entries>"` checks a past commit, which is how the drift study in [EVALUATION.md](EVALUATION.md) is run. `lookup --repo <repo> --tag sensors` lists the tagged components (tags are `# @tags: a, b` comments directly above a function or class). `bench --repo <repo>` measures detection accuracy.

The gate is deliberately independent of the runner: it lives in `integrity/` at the repo root and talks to the runner only through its CLI, the `AGENT_*` environment variables and files. It runs from `validate.json` as a `skip_if_failed` command before the adversary. See `integrity/README.md` for details.

## Dangling reference check

`integrity/integrity.py refs` fails a change that **removes a function, class or constant that other code still uses**. It needs no scope. It catches the failure seen in past runs where an agent rewrites a file, drops functions other files still call, and the tests don't notice.

- **What counts as removed:** defined in the old version of a changed file, gone from the new version, and not defined anywhere else in the same language. Extractors cover Python (ast), PHP, JS/TS, Dart, SQL and C/Arduino.
- **What counts as a reference:** a whole-word match in live code or tests. Comments, strings, other languages, `archive/`, `vendor/` and generated files are ignored. References only in docs are reported as `doc_only` and don't fail.
- **Config** (`refs` section): `mode` is `enforce` (the template default) or `report`. `ignore` lists symbol names to skip. `min_length` (default 4) sets the shortest name checked, and `exclude` replaces the built-in excluded paths.
- **Output:** on failure the agent is told which symbols were removed and every remaining reference, and to restore the symbol or update all the references in the same change. Records go to `<run dir>/integrity/refs_issue-<N>_attempt-<K>.json`.
- **Evidence:** on 232 past commits it flagged 5, all real breakages the tests had missed, with no false alarms. Its filters were tuned on that same history, so expect the real false-alarm rate to be somewhat higher. `--commit <sha>` checks any past commit.
- **Limits:** it matches by name only, so a same-named definition elsewhere hides a removal. Dynamic calls (string callbacks, `getattr`), removed columns and changed signatures are not checked.

## Environment blockers

Some failures can't be fixed by retrying: a missing tool or SDK, the wrong JDK, Docker not running, no credentials, no GPU, a full disk. `blockers.py` matches failed attempts against fixed rules. Before a match counts, a probe checks the machine directly (`which`, `ANDROID_HOME`, `docker info` ...), so a tool that is actually installed is never blamed.

An issue is parked immediately, without using up its retries, when:

- the supervisor's own gate hit the blocker;
- the agent reports `STATUS: BLOCKED` with a specific `BLOCKER:` kind;
- the same blocker appears on two attempts in a row;
- the blocker already parked another issue in this run; or
- the failure is identical to the previous attempt and the worktree hasn't changed.

When one blocker has parked `env_blocker_halt_after` issues, the run stops with `env_blocked` and the summary lists what to install. After you fix it, a parked issue comes back automatically, once, when its probe passes.

Add project-specific rules in the config:

```json
"blockers": [{
  "kind": "EXTERNAL_SERVICE", "subject": "postgres",
  "pattern": "could not connect to server.*5432",
  "hint": "Start Postgres.", "severity": "hard",
  "probe_argv": ["pg_isready", "-h", "localhost"]
}]
```

`probe_argv` exiting 0 means the dependency is present. Kinds an agent may declare: `MISSING_TOOL`, `MISSING_SDK`, `PERMISSION`, `CREDENTIALS`, `EXTERNAL_SERVICE`, `PLATFORM`, `HARDWARE`, `UNKNOWN`.

## Overnight sessions and usage limits

`v2.py run` uses `engine/session.py`, which runs `engine/run_issues.py` in slices (`--slice-minutes`, default 50). Each slice is `run_issues.py --resume-or-new --on-usage-limit exit --max-hours <slice>`, so every slice continues the same run: retry counts, deferrals and half-finished issues carry over.

`run_issues.py` ends every run with one line, `RUN_RESULT {"reason": ..., "usage_reset": ..., "committed": [...]}`, and the session acts on it:

| Run ended with | Session does |
|---|---|
| `max_hours` | Starts the next slice (resumes the run) |
| `usage_limit` | Pauses until the reported reset (or `--usage-fallback-minutes`), then resumes |
| `all_closed` | The next slice starts a fresh run, picking up newly filed issues |
| `no_work` | Stops: the project is complete (exit 0) |
| `all_deferred` / `all_blocked` / `env_blocked` | Stops: a human is needed (exit 2) |
| No result (crash) | Retries with backoff. Gives up after 3 crashes in a row (exit 1) |
| Slice overran | Killed at slice + grace (`max_session_minutes` + 15). The next slice resumes |

**Usage tracking** (`python v2.py usage`):

- **Codex** logs its real 5-hour and weekly plan-limit percentages and reset times. `run` checks them before every slice and pauses at 97% (`--max-percent`).
- **Claude Code** logs only token counts locally, so its limit is handled when it's hit: the run exits on the limit message and pauses until the reset time in that message. For live percentages, run `claude` and type `/usage` (or `codex` then `/status`).

`v2.py run` options: `--once` (one slice), `--max-hours N` (total budget, `0` = until done), `--fresh` (forget this project's session state), `--slice-minutes`, `--grace-minutes`, `--provider`, `--provider-probe`, `--max-percent`, `--usage-fallback-minutes`. Any other flag is passed through to `run_issues.py`.

## Creating issues

`create_issues.py` (called by `v2.py issues`) turns the backlog into GitHub issues, labels and milestones.

- **Deterministic:** issues are created in file order, so GitHub numbers ascend in the same order the runner works them.
- **Idempotent:** an issue whose exact title already exists is skipped, so a half-finished run can be re-run safely.
- **Positional dependencies:** `depends_on: ["#2"]` means the second issue *in the file* (an exact title also works). It is rewritten to the real GitHub number in the `**Depends on:**` line the runner reads.
- **Scope:** `scope: ["src/hsv.py::analyse_frame", "tests/**"]` (path, glob or `path::Qualname`) becomes the `**Scope:**` line the [scope gate](#scope-gate) checks.
- **Labels and milestones** may be declared at the top of the file. Any others the issues use are created too. Closed milestones count as existing.
- **Polite:** writes are paced (`--delay`, default 1s) and rate-limit rejections are retried (`--retries`, default 5).
- It never commits, pushes, branches or opens PRs.

```bash
python v2.py issues MyApp --dry-run    # existing titles show as SKIP
python v2.py issues MyApp --update     # repair labels, milestones, dependency and scope lines on existing issues
```

## Using the scripts directly

`v2.py` only orchestrates. Each piece in `engine/` also runs on its own, e.g. `python engine/run_issues.py --project X`:

| Script | Use |
|---|---|
| `engine/run_issues.py` | `--project X`, `--list-projects`, `--resume-latest`, plus every config key as a flag (`--max-issue 20`, `--push`, ...) |
| `engine/create_issues.py` | `--project X` or `--repo <path> --issues <file>`, `--dry-run`, `--update` |
| `engine/session.py` | `--project X --provider claude` |
| `engine/adversary.py` | Exit 0 = no objection, 3 = veto |
| `engine/ml_gate.py` | Exit 0 pass, 1 fail, 2 misconfigured. `--lock`, `--status` |
| `engine/usage.py` | Provider cooldowns and the optional quota probe |

### Setting up a project by hand

1. Copy `templates/software/` (or `templates/ml/`) to a new top-level folder, e.g. `MyProject/`.
2. In `issue-automation.config.json`, set `repo` to the absolute path of the checkout and adjust the issue filters.
3. In `validate.json`, replace `TODO-test-command` with the project's test command.
4. Fill in `project_rules.md` and `issues.yaml`.
5. Optionally paste `AGENTS.branching.md` into the target repo's `AGENTS.md` / `CLAUDE.md`.
6. Add `node_modules/`, `vendor/`, `.env` and other local-only paths to the target repo's `.gitignore`.
7. Run `python v2.py check MyProject`.

Projects created before v2 run under `v2.py check` and `v2.py run` unchanged.

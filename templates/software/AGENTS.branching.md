## Branches and commits

<!-- Drop this section into the project's AGENTS.md / CLAUDE.md. It matches the
     run_issues.py defaults (branch_mode=auto, close_on=merge, sync_base=merge).
     Replace automation/work if the project config names another work_branch. -->

This is a solo project. Keep **one active work branch** and nothing else.

- **Use the branch that is checked out.** If it is not `main`, it is the active work branch: commit there. Do not create, switch, rename or delete branches.
- **If `main` is checked out,** work goes on `automation/work` (created from `origin/main` if it does not exist). `run_issues.py` does this for you.
- **Never** create per-issue, per-run or timestamped branches, and never park work on a backup branch. Unfinished work for an issue is saved as the tag `deferred/issue-<N>` (restore with `git stash apply deferred/issue-<N>`).
- **One issue per commit.** The commit message must carry `Closes #N.` Do **not** close issues by hand; they close when the branch's PR is merged into `main`.
- **Never push, merge, rebase or open PRs.** The owner reviews the branch, pushes it and opens one PR into `main`. Merge it with a merge commit (not squash), so the next run fast-forwards the branch automatically.
- **Resolve a conflict with `main` file by file,** never by taking one side wholesale (`-X ours`/`-X theirs`, `checkout --ours/--theirs`). Run the full test suite after the merge before committing it.

Never commit secrets (`.env`, keys, local config) or dependency/build folders (`node_modules/`, `vendor/`, `dist/`, `build/`).

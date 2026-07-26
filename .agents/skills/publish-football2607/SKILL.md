---
name: publish-football2607
description: Safely publish changes from the adam-zou/football2607 repository by reviewing scope, validating the project, committing selected files, pushing a feature branch, creating or updating a pull request, and optionally merging it when explicitly requested. Use when the user asks to Git push, sync this project, publish current Football2607 changes, submit the project, create a PR, or merge the published PR.
---

# Publish Football2607

Publish the current repository changes without silently including unrelated files, secrets, local accounts, or data snapshots. Stop after creating or updating a draft PR unless the current user request explicitly asks to merge it.

## 1. Confirm repository and authority

1. Read the repository-root `AGENTS.md` before acting.
2. Resolve the repository root with `git rev-parse --show-toplevel` and run every command there.
3. Verify `origin` resolves to `adam-zou/football2607`. Stop if it points elsewhere.
4. Require `gh` and an authenticated `gh auth status` session.
5. Run `git fetch origin --prune`, then inspect:
   - `git status --short --branch`
   - `git diff --stat`
   - `git diff --check`
   - `git log -1 --oneline`

## 2. Establish the exact scope

Inspect every modified and untracked file before staging.

- Treat `.env`, `users.json`, credentials, generated database dumps, captured HTML, screenshots, TSV/CSV snapshots, logs, and temporary files as local unless the user explicitly includes them.
- Scan candidate configuration and source files for real passwords, tokens, proxy credentials, database URLs, session secrets, and API keys. Do not print secret values.
- If changes belong to different topics, ask which topic to publish. Do not use `git add -A` for a mixed worktree.
- Preserve unrelated user changes and report every file left behind.
- Stage intended files by explicit path.

If the change affects application workflows, background scheduling, data ownership, database schemas, provider interfaces, or CLI entrypoints, read `docs/architecture/code-flow.md` and require its staged content to match the implementation. Update it before publishing when needed.

## 3. Choose the branch

- Stay on the current non-default feature branch when its changes form the intended PR.
- When starting on `main`, create `codex/<short-description>` before committing.
- Never commit feature work directly to `main`.
- Do not rebase, force-push, reset, or rewrite history automatically. Report divergence from `origin/main` when it matters to the PR.

## 4. Validate before committing

Always run `git diff --check` on the staged changes.

Choose validation from the staged paths:

- Documentation-only: no code test is required; report `git diff --check` as validation.
- Python or application changes: run the full Python suite with the repository virtual environment when available:
  - macOS/Linux: `.venv/bin/python -m pytest -q`
  - Windows: `.venv\Scripts\python.exe -m pytest -q`
- MatchWeb JavaScript changes: also run `node --test MatchWeb/tests/test_app.js` when Node is installed.
- Python entrypoint changes: run `python -m py_compile` for the changed entrypoints when useful.

If a required tool or dependency is missing, use the existing repository environment or install it once and retry. Do not commit when relevant tests fail. Diagnose failures or ask the user before excluding a failing validation.

## 5. Commit and push

1. Review `git diff --cached --stat` and `git diff --cached` before committing.
2. Commit with a terse imperative message that describes the staged diff.
3. Push with tracking:

   ```bash
   git push -u origin "$(git branch --show-current)"
   ```

4. Verify the remote branch SHA matches `HEAD`.

Never force-push.

## 6. Create or update the pull request

Query open PRs for the current head branch before creating one.

- If an open PR exists, leave it open and report that the push updated it.
- If no open PR exists, create a draft PR targeting `main`.
- Prefer the installed GitHub connector for PR creation. If it lacks permission, use `gh pr create --draft` with a temporary Markdown body file.
- If a previous PR from the same branch was merged, create a new draft PR for commits not contained in `main`.

The PR title must summarize the full branch diff. The body must contain:

- what changed;
- why it changed;
- user or operational impact;
- root cause when publishing a fix;
- exact validation commands and results.

Do not mark the PR ready or merge it without explicit authorization in the current
user request. A request to commit, push, publish, submit, or create a PR alone is not
authorization to merge.

## 7. Merge when explicitly requested

Run this section only when the current user request explicitly asks to merge the PR.

1. Resolve the exact PR for the current head branch and require it to be open, target
   `main`, and contain the remote branch's latest commit.
2. Inspect mergeability, review decision, and status checks. Do not merge when the PR
   has conflicts, requested changes, or failing/pending required checks. Report the
   blocker instead.
3. If the PR is a draft and all gates pass, mark it ready with `gh pr ready`.
4. Merge with the repository's normal merge method, defaulting to
   `gh pr merge <number> --merge`. Never use `--admin` to bypass branch protection.
5. Verify the PR state is `MERGED`, fetch `origin`, and confirm the merge commit is in
   `origin/main`. When the worktree is clean, switch to `main` and update it with
   `git pull --ff-only origin main`.

Do not delete local or remote feature branches automatically. Do not enable auto-merge
unless the user explicitly asks for auto-merge.

## 8. Verify and report

After publishing, verify:

- `git status --short --branch`;
- local `HEAD` equals the remote feature branch;
- the PR head and base branches are correct;
- the PR contains the latest commit;
- when merging was requested, the PR is merged and its merge commit is present in
  `origin/main`.

Report the branch, commit SHA and subject, PR link and draft/merge state, validations,
and any untracked or unstaged files left locally. Emit the product's Git stage,
commit, push, and PR directives only for actions that actually succeeded.

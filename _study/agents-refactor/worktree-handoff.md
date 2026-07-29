# Worktree and handoff contract

Use an isolated git worktree for repository edits. Fetch `origin/main` first; if the existing worktree is stale, create a fresh worktree from the current base rather than editing a shared checkout. Verify status before edits. Run tests inside the worktree, commit the scoped change, and record changed files, exact tests, branch/SHA, review state, and rollback path.

For coding Kanban cards, post durable evidence on the same card and hand off with `review-required:` before review. Do not create reviewer or recovery children. Completion requires an independent verdict and durable merged/resolved evidence. Never include secrets in logs, comments, or handoffs.

This repository reference is the profile-independent fallback for workers whose profile does not ship the `apply-coding-task-handoff` skill.

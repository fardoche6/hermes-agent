# Hermes Agent — active development rules

This file is the active context gate. Read `AGENTS.reference.md` for the complete development guide when a task needs detailed subsystem instructions.

## Non-negotiable engineering rules
- Preserve prompt-cache stability, message-role alternation, and the narrow core waist.
- Prefer existing infrastructure and edge integrations over new core model tools.
- Never add secrets, tokens, passwords, or connection strings to source, tests, logs, argv, or artifacts.
- Use real behavior and hermetic tests for resolution/config/I/O/security changes; tests must not write to `~/.hermes`.
- Inspect existing code, manifests, tests, and active config before editing. Run focused and relevant regression tests before claiming completion.
- Keep plugins and skills isolated from core unless a generic extension surface is required.

## Coding lifecycle contract
- Coding work is explicitly typed (`task_type=coding`), uses a real isolated worktree, and requires same-card review.
- Lifecycle: `ready -> running -> review -> running` on changes requested, or `review -> done` only after approved review plus merged/resolved evidence.
- Review is same-card: never create reviewer or recovery children, and never use `blocked` as review proof.
- Prose markers, comments, or status labels are not executable review evidence.
- Core admission must fail closed for ambiguous workspaces, coding triage, missing AGENTS context, and unsupported capability checks.
- Use `kanban_submit_review` and `kanban_review_verdict`; approval alone is not completion.

## Verification
Record exact commands and results. For context-sensitive changes, report the active context limit and rendered context size. For browser/web capability claims, prove fresh-process tool discovery and report runtime/auth blockers exactly; never fabricate browser proof.

## Scope
Do not modify SubsidySmart product AGENTS or product files while changing Hermes development policy. Do not modify protected bundled `kanban-worker` skill content; change canonical source integration instead.

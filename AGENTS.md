# Hermes Agent - Development Guide

This file is the active, compact contract for coding assistants. The detailed
reference is `_study/agents-refactor/AGENTS.md`; do not load that monolith by
default. Load only the mapped section below when the change touches that area.
Put durable domain detail in `_study/`, Obsidian, or a focused skill instead of
growing this file. Keep this file below 20,000 bytes and verify the bound with
`wc -c -w -l AGENTS.md`.

## Non-negotiable workflow

- Inspect the real code, existing tests, and project instructions before
  editing; trace symbols to definitions and usages. Make the smallest coherent
  change and run focused tests, then broader tests when risk warrants it.
- Repository edits happen in an isolated git worktree. Fetch/check the base
  first; never edit a dirty shared checkout or fall back when worktree setup
  fails. Do not commit, push, or rewrite history unless the task asks for it.
- For coding Kanban cards, use the same-card lifecycle: `ready` -> `running` ->
  independent review -> `done`. Never create reviewer/recovery children, use
  `blocked` as review, or treat prose as review proof. Completion requires
  executable review evidence and merged/resolved evidence. Non-coding work
  still needs structured completion metadata.
- Report exact commands and results. Do not claim tests, CI, deployment, or
  external writes succeeded without fresh evidence. Keep secrets and
  credentials out of logs, diffs, comments, and handoffs.
- Security guardrails: bound PyPI dependencies; SHA-pin Git URLs and GitHub
  Actions; exact-pin CI-only pip installs; do not add outbound telemetry or
  attribution without generic user opt-in; `.env` is for secrets only; never
  print, commit, or paste secret values.
- Use `scripts/run_tests.sh`, not direct `pytest`; it supplies CI-parity
  isolation and environment cleanup. For skill changes, use its focused skill
  test path as described in the reference map.

## Design invariants

- Preserve prompt-cache stability: do not mutate past context, toolsets, or
  system prompts during a conversation except context compression. Preserve
  strict message-role alternation.
- Keep the core narrow. Prefer extending existing code, then CLI + skill,
  service-gated tool, plugin, or MCP; add a new core model tool only as a last
  resort. Plugins must not hardcode plugin-specific logic into core.
- Behavioral tests assert contracts/invariants, not mutable catalogs or source
  text. For config, security, resolution, and I/O changes, exercise the real
  path against a temporary `HERMES_HOME`, not only mocks.
- Use `get_hermes_home()` for profile state and `display_hermes_home()` for
  user-facing paths. Behavioral settings belong in `config.yaml`; `.env` is
  for secrets only. New skills follow the repository skill standards and put
  non-trivial helpers in their skill `scripts/` directory.

## Reference map (load only what the change needs)

- Core/runtime, agent loop, tools, providers, prompt/cache, or model routing:
  `_study/agents-refactor/AGENTS.md`, sections `What Hermes Is`, `AIAgent`,
  `Toolsets`, `Providers`, and `Prompt/context`.
- CLI, gateway, TUI, desktop, or website: same reference, the matching
  architecture section; inspect the named source files before editing.
- Skills/plugins/MCP: same reference, `Skills`, `Toolsets`, and `Plugins`
  sections; hard skill-authoring rules are in `Skills`/`Skill authoring
  standards`.
- Tests, config, security, resolution, or I/O: same reference, `Testing`,
  `Development Environment`, and `Profile` sections. The canonical runner is
  `scripts/run_tests.sh`.
- Worktree/handoff: `_study/agents-refactor/worktree-handoff.md`. If the
  profile provides `apply-coding-task-handoff`, load it too; this tracked file
  is the fallback for orchestrator/reviewer profiles that do not.
- Lifecycle/testing/review: `_study/agents-refactor/development-lifecycle.md`.
  If the profile provides `software-development-lifecycle`, load it too; the
  tracked file is the fallback for profiles without that skill.
- Hermes runtime/config facts: profile skill `hermes-agent` when available;
  otherwise use the official docs at https://hermes-agent.nousresearch.com/docs.

The full preserved guide is reference material, not active prompt content:
`_study/agents-refactor/AGENTS.md`. Validate this contract with
`scripts/run_tests.sh tests/test_agents_contract.py -q`.

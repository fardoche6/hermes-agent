# Development lifecycle contract

Inspect the real code, tests, instructions, branch, and worktree before editing. Reproduce failures before fixing them. Make the smallest coherent change; add a narrow regression test for new behavior; run focused tests, then the broader relevant runner. Review the actual diff for correctness, security, data loss, I/O, and missing tests. Report exact commands and results; distinguish local tests from CI and live proof.

For documentation and policy changes, verify readback, `git diff --check`, required links/phrases, and forbidden stale references. For config, security, resolution, and I/O paths, exercise real imports against a temporary `HERMES_HOME`, not only mocks.

This repository reference is the profile-independent fallback for workers whose profile does not ship the `software-development-lifecycle` skill.

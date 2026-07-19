# Future Kanban card contract

This is the process contract for a fully specified coding task. It deliberately
uses the existing Kanban API; it does not introduce a second board or a second
card for recovery or review.

## Admission

Before creating a card, the orchestrator must:

1. Search the active board and tenant for the same goal, idempotency key, or
   an unarchived recovery card. A retry uses the existing card.
2. Verify the assignee profile exists and record the exact profile name.
3. Verify the requested workspace kind/path is explicit. For a worktree, the
   path must be an existing isolated worktree (or a project-linked task must
   resolve one); never accept a bare checkout or an ambiguous relative path.
4. Verify required capabilities against the assignee's real toolset names.
   Unknown names are a capability blocker, not a best-effort guess.
5. Choose the initial column:
   - fully specified, assigned, dependency-complete: `ready`;
   - unresolved scope, decomposition, assignment, or dependency design:
     `triage`;
   - a known external/human gate: `blocked` with a typed reason.

Create with an idempotency key, then immediately read the card back. The
readback must match board, tenant, assignee, workspace kind/path, parent links,
initial status, and capability assumptions. A failed readback is a blocker;
do not create a replacement card.

The supported implementation already provides the important admission rails:
`kanban_db.create_task(..., idempotency_key=...)` deduplicates non-archived
cards, `triage=True` forces `triage`, parents gate `todo -> ready`, and
`kanban_show` is the post-create readback surface.

## One-card lifecycle

The canonical identity is the original task id:

```
ready -> running -> review/proof -> done
   \-> blocked (typed recovery or human gate) -> same card -> ready/running
```

- The dispatcher claims `ready -> running` atomically and records a run.
- The implementer works only in the read-back workspace and comments changed
  files, checks, limitations, and proof. Recovery, retry, workspace repair,
  and dependency repair update that card; they never call `kanban_create`.
- A reviewer checks the same card, branch, workspace, and diff. Do not create a
  reviewer child card for one diff.
- Approval is not completion. `done` requires the reviewer verdict plus the
  required proportional tests, live proof, and PR/deployment proof where
  applicable.
- A missing capability is an exact typed blocker. Do not replace it with
  invented browser, test, CI, or deployment evidence.
- Unknown blocker kinds fail closed and route to inspection. They must not
  leave a review-loop card silently idle in `triage`.

## Same-card review API

The worker registry exposes `kanban_submit_review` and
`kanban_review_verdict` in addition to the ordinary lifecycle tools. The
implementation calls `kanban_submit_review` to close its run and enter
`review`; the dispatcher uses `claim_review_task()` to claim that same card for
the reviewer. The reviewer calls `kanban_review_verdict` on that same card:

1. `request_changes` closes the review run and returns the card to `ready`.
2. `approved` records concrete proof and enters `review_approved` while preserving
   the reviewer's exact run+claim as the finalization authorization.
3. Approval is deliberately not completion. A separate `kanban_complete` call
   is required from `review_approved`, after the required proof is present.

For coding cards, admission marks `worktree` tasks as review-required, so a
direct `kanban_complete` from an implementation run fails closed. Merge proof
is verified server-side against the configured repository's advertised remote
default branch with `git ls-remote`; local `refs/remotes/*` and caller-supplied
readback strings are not authoritative. If a reviewer dies after approval but
before finalization, the same-card recovery paths revoke the approval, close
the run, and return the card to `review`.

The state machine and both verdict paths are covered by the synthetic tests.
`blocked` remains a typed blocker and is never used as a review substitute.

## Workspace/runtime proof

For every future card, record:

- `workspace_path`, `workspace_kind`, branch, and `git status --short` from the
  worker's actual cwd;
- the resolved scheduler script path (`readlink -f`), source path, and SHA-256
  of both when a cron gate is involved;
- the exact process/tool discovery command and result after a fresh process;
- the board and tenant used for every read/write.

The direct verifier and scheduler must execute the same source file or
hash-identical copies. If that cannot be proven, stop before claiming done.

## Anomaly matrix

| Failure | Fail-closed response | Never do |
|---|---|---|
| Wrong/dirty/ambiguous workspace | typed capability/workspace blocker; repair same card | edit a legacy checkout or create a recovery card |
| Duplicate recovery | reuse original id; idempotency readback | create a replacement for the same goal |
| Triage detour | keep specified card `ready` | route complete work through `triage` |
| Reviewer child | same-card review/proof | create a reviewer child for one diff |
| Missing browser capability | exact `capability` blocker naming missing runtime/credential | claim web search is browser proof |
| Unknown toolset | preflight failure naming the unknown name | invent an `internet` toolset |
| Runtime-path drift | source/runtime path+hash mismatch blocker | validate one copy and schedule another |
| False completion | keep `review-required`/typed blocker until proof exists | mark approval or a green unit test as `done` |

## Synthetic proof

`tests/hermes_cli/test_kanban_future_card_contract.py` and
`tests/tools/test_kanban_review_regressions.py` exercise this contract
against an isolated temporary Kanban database. It proves idempotent admission,
ready-versus-triage selection, explicit workspace readback, same-card recovery,
same-card review submission/claim/verdict/completion, and changes-requested
recovery. It creates no live board card,
profile/config change, cron job, credential, or product-repository change.

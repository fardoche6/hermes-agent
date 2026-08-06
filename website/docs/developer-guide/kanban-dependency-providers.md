---
sidebar_label: "Kanban Dependency Providers"
slug: /developer-guide/kanban-dependency-providers
title: "Typed Kanban Dependency Providers"
description: "Build consumer-agnostic plugin providers for typed Kanban dependencies"
---

# Typed Kanban Dependency Providers

Hermes plugins can add a typed Kanban dependency without opening the Kanban
SQLite database. The host owns link storage, readiness, claim locking, and
workspace admission. A plugin only supplies a bounded callback for a generic
`(dependency_kind, provider_name)` key.

This surface is intentionally consumer-agnostic. Do not put product names,
consumer-specific queue behavior, or messaging rules in core or in the provider
contract.

## Register a provider

Inside a plugin's `register(ctx)` function:

```python
from hermes_cli.kanban_dependencies import KanbanDependencyResult


def check(context):
    # context.board is the resolved board slug.
    # context.task, context.link, and context.parent are frozen mappings.
    if context.link["metadata"].get("ready") is True:
        return KanbanDependencyResult(
            status="satisfied",
            generation="source-generation-17",
        )
    return KanbanDependencyResult(status="unsatisfied")


def register(ctx):
    ctx.register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        check,
        timeout_seconds=2.0,
    )
```

The callback receives no connection, manager, filesystem handle, mutable task
object, or plugin-private state from Hermes. The context includes only the
explicit board, a bounded task snapshot, the typed link, and the parent task
snapshot. Body text, comments, events, and runs are not passed to providers.

Providers are process-local. Each callback runs in a short-lived, killable
helper process, so a timeout or unload cannot leave a callback thread running
in the host. Treat callbacks as isolated functions: return the result rather
than relying on mutations to parent-local Python objects. The registration key
must be unique while the plugin set is loaded. Plugin-owned registrations are
removed when that plugin is unloaded or force-reloaded; direct registrations
and providers owned by other plugins are left alone.
`ctx.unregister_kanban_dependency_provider()` and the public
`unregister_kanban_dependency_provider()` function are idempotent owner-safe
teardown surfaces.

## Result contract

Return `KanbanDependencyResult`, or its exact JSON-compatible shape:

```json
{
  "status": "satisfied",
  "generation": "source-generation-17",
  "diagnostics": {"source": "cache"}
}
```

`status` is exactly one of:

- `satisfied` — requires a non-empty immutable `generation`.
- `unsatisfied` — the dependency is currently not met.
- `unknown` — the provider cannot establish satisfaction.

`diagnostics` is optional bounded JSON metadata. Hermes validates, bounds, and
redacts it before exposing it through readback. Do not put secrets in it.

A satisfied result may additionally include an exact immutable Git base pin:

```python
from hermes_cli.kanban_dependencies import KanbanWorkspaceBasePin

KanbanDependencyResult(
    status="satisfied",
    generation="source-generation-17",
    workspace_base=KanbanWorkspaceBasePin(
        head="<full-40-or-64-character-commit-sha>",
        tree="<full-40-or-64-character-tree-sha>",
        receipt_generation="source-receipt-17",
    ),
)
```

The host persists the generation and pin on the admitted `task_runs` row. A
later readiness sweep is not an admission record: every lifecycle mutation
pre-resolves dependency/provider state outside SQLite `write_txn`, then
re-reads the canonical typed-link/provider snapshot inside the transaction and
compares its digest immediately before the lifecycle CAS. This is the same
snapshot-CAS rule used by claim, and keeps slow provider callbacks from holding
the board write lock.

The exact successful binding is then used by worktree creation, which verifies
both the commit and tree object before returning a workspace. A public
`resolve_workspace()` call for a task with a durable dependency binding must
pass the board-bound connection returned by the official CLI/dispatcher path;
without that connection the host fails closed rather than guessing the board.

A base pin is valid only for a `worktree` task. A satisfied provider result
that carries a pin for a scratch or shared-directory task is rejected during
readiness and claim rather than failing later during workspace creation.

## Create and inspect typed links

The public Kanban APIs accept typed fields while preserving the old behavior:

```python
kb.create_task(
    conn,
    title="Consumer task",
    parents=[parent_id],
    dependency_kind="sample.gate",
    provider_name="sample",
    dependency_metadata={"ready": True},
    board="alpha",
)

kb.link_tasks(
    conn,
    parent_id,
    child_id,
    dependency_kind="sample.gate",
    provider_name="sample",
    metadata={"ready": True},
    board="alpha",
)
```

Omitting the typed fields creates an ordinary `completion` edge. Completion
edges remain satisfied only when the parent is `done` or `archived`.
Non-completion edges require a provider name; metadata is bounded opaque JSON.
Self-links and cycles are rejected. Repeating the exact same link is
idempotent; attempting to replace its kind, provider, or metadata is rejected
because those binding fields are immutable. Use `list_dependency_links()` for
stored typed link data and
`task_dependency_evidence()` for current provider/completion evidence. The
Kanban show APIs and `hermes kanban show --json` expose both surfaces, along
with the current run's immutable `dependency_binding`.

## Fail-closed behavior

The host treats all of these as `unknown` and blocks readiness/claim:

- no provider registered for the key;
- provider exception;
- provider timeout;
- malformed result, generation, pin, or diagnostic metadata;
- missing parent/task identity or malformed stored metadata;
- conflicting workspace pins;
- missing Git objects or a worktree whose `HEAD`/tree does not match the pin;
- no real process-containment boundary for a provider helper.

A provider helper is never spawned when the host cannot establish a real
containment boundary. Process-group cleanup is not treated as a substitute for
containing detached descendants; the stable diagnostic in that case is
`provider_containment_unavailable`.

Do not make a provider callback perform unbounded I/O. Hermes bounds the
callback wall-clock wait and terminates/reaps its helper process on timeout or
unload. A callback may be interrupted at either boundary, so it should return
only bounded data and keep any external side effects independently safe.

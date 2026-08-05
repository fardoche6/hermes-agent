"""Behavior tests for the generic Kanban dependency-provider contract."""

from __future__ import annotations

import json
import multiprocessing
import subprocess
import threading
import time
import sqlite3
from pathlib import Path
from types import MappingProxyType

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_dependencies import (
    KanbanDependencyResult,
    KanbanWorkspaceBasePin,
    register_kanban_dependency_provider,
    unregister_kanban_dependency_provider,
    registered_kanban_dependency_providers,
    unregister_kanban_dependency_providers,
)


@pytest.fixture
def board_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    unregister_kanban_dependency_providers()
    conn = kb.connect(db_path=tmp_path / "alpha.db", board="alpha")
    try:
        yield conn, tmp_path
    finally:
        conn.close()
        unregister_kanban_dependency_providers()


def _new_parent(conn):
    return kb.create_task(conn, title="parent", board="alpha")


def _new_provider_child(conn, parent_id, *, kind="sample.gate", provider="sample", **kwargs):
    return kb.create_task(
        conn,
        title="provider child",
        parents=[parent_id],
        dependency_kind=kind,
        provider_name=provider,
        board="alpha",
        **kwargs,
    )


def test_legacy_typed_link_migration_and_completion_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, assignee TEXT,
            status TEXT NOT NULL, priority INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL, workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,
            max_runtime_seconds INTEGER, last_heartbeat_at INTEGER,
            started_at INTEGER, current_run_id INTEGER
        );
        CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
                                 PRIMARY KEY(parent_id, child_id));
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                  task_id TEXT NOT NULL, kind TEXT NOT NULL,
                                  payload TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                task_id TEXT NOT NULL, profile TEXT, step_key TEXT,
                                status TEXT NOT NULL, claim_lock TEXT,
                                claim_expires INTEGER, worker_pid INTEGER,
                                max_runtime_seconds INTEGER, last_heartbeat_at INTEGER,
                                started_at INTEGER NOT NULL, metadata TEXT, error TEXT);
        INSERT INTO tasks(id, title, status, created_at) VALUES
          ('p', 'parent', 'ready', 1), ('c', 'child', 'todo', 1);
        INSERT INTO task_links(parent_id, child_id) VALUES ('p', 'c');
        """
    )
    raw.commit()
    raw.close()

    conn = kb.connect(db_path=path, board="alpha")
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_links)")}
        run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
        assert {"dependency_kind", "provider_name", "metadata"} <= columns
        assert "dependency_binding" in run_columns
        assert kb.list_dependency_links(conn, "c") == [{
            "parent_id": "p",
            "child_id": "c",
            "dependency_kind": "completion",
            "provider_name": None,
            "metadata": {},
        }]
    finally:
        conn.close()


def test_completion_edges_promote_only_after_done_and_cycles_are_rejected(board_db):
    conn, _ = board_db
    parent = _new_parent(conn)
    child = kb.create_task(conn, title="child", parents=[parent], board="alpha")
    assert kb.get_task(conn, child).status == "todo"
    assert kb.list_dependency_links(conn, child)[0]["dependency_kind"] == "completion"

    with pytest.raises(ValueError, match="cycle"):
        a = kb.create_task(conn, title="a", board="alpha")
        b = kb.create_task(conn, title="b", board="alpha")
        c = kb.create_task(conn, title="c", board="alpha")
        kb.link_tasks(conn, a, b, board="alpha")
        kb.link_tasks(conn, b, c, board="alpha")
        kb.link_tasks(conn, c, a, board="alpha")

    assert kb.complete_task(conn, parent, result="parent done")
    assert kb.get_task(conn, child).status == "ready"


def test_provider_context_is_frozen_board_explicit_and_opaque_metadata(board_db):
    conn, _ = board_db
    parent = _new_parent(conn)

    def provider(context):
        assert context.board == "alpha"
        assert isinstance(context.task, MappingProxyType)
        assert "body" not in context.task
        with pytest.raises(TypeError):
            context.task["status"] = "done"
        assert context.link["metadata"]["ticket"] == "redacted"
        return {
            "status": "satisfied",
            "generation": "g-1",
            "diagnostics": {
                "ok": True,
                "board": context.board,
                "child_id": context.link["child_id"],
                "frozen": True,
            },
        }

    register_kanban_dependency_provider("sample.gate", "sample", provider)
    child = _new_provider_child(
        conn,
        parent,
        dependency_metadata={"ticket": "redacted"},
    )
    assert kb.get_task(conn, child).status == "ready"
    evidence = kb.task_dependency_evidence(conn, child, board="alpha")
    assert evidence[0]["status"] == "satisfied"
    assert evidence[0]["generation"] == "g-1"
    assert evidence[0]["diagnostics"] == {
        "ok": True,
        "board": "alpha",
        "child_id": child,
        "frozen": True,
    }


@pytest.mark.parametrize(
    "mode, expected_status, expected_reason",
    [
        ("absent", "unknown", "provider_unavailable"),
        ("unsatisfied", "unsatisfied", None),
        ("unknown", "unknown", None),
        ("exception", "unknown", "provider_exception"),
        ("malformed", "unknown", "provider_malformed_result"),
        ("timeout", "unknown", "provider_timeout"),
    ],
)
def test_provider_failure_modes_fail_closed(board_db, mode, expected_status, expected_reason):
    conn, _ = board_db
    parent = _new_parent(conn)

    if mode != "absent":
        def provider(_context):
            if mode == "unsatisfied":
                return KanbanDependencyResult("unsatisfied")
            if mode == "unknown":
                return KanbanDependencyResult("unknown")
            if mode == "exception":
                raise RuntimeError("provider failure")
            if mode == "malformed":
                return {"status": "satisfied"}
            time.sleep(0.05)
            return KanbanDependencyResult("satisfied", generation="late")

        register_kanban_dependency_provider(
            "sample.gate",
            "sample",
            provider,
            timeout_seconds=0.005 if mode == "timeout" else 1.0,
        )
    child = _new_provider_child(conn, parent)
    evidence = kb.task_dependency_evidence(conn, child, board="alpha")[0]
    assert evidence["status"] == expected_status
    if expected_reason:
        assert evidence["diagnostics"]["reason"] == expected_reason
    assert kb.get_task(conn, child).status == "todo"


def test_provider_timeout_has_no_live_worker_thread_or_child(board_db):
    conn, _ = board_db
    parent = _new_parent(conn)
    stop = threading.Event()

    def provider(_context):
        stop.wait()

    register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        provider,
        timeout_seconds=0.01,
    )
    child = _new_provider_child(conn, parent)
    before_threads = {thread.ident for thread in threading.enumerate()}
    evidence = kb.task_dependency_evidence(conn, child, board="alpha")[0]
    time.sleep(0.02)
    leaked_threads = [
        thread for thread in threading.enumerate()
        if thread.ident not in before_threads
    ]
    assert evidence["status"] == "unknown"
    assert evidence["diagnostics"]["reason"] == "provider_timeout"
    assert leaked_threads == []
    assert multiprocessing.active_children() == []
    stop.set()


def test_provider_unload_cancels_inflight_callback(board_db):
    conn, tmp_path = board_db
    parent = _new_parent(conn)

    def provider(_context):
        while True:
            time.sleep(0.01)

    register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        provider,
        timeout_seconds=5.0,
        owner="plugin-a",
    )
    child = _new_provider_child(conn, parent)
    result = []

    def evaluate():
        worker_conn = kb.connect(db_path=tmp_path / "alpha.db", board="alpha")
        try:
            result.append(kb.task_dependency_evidence(worker_conn, child, board="alpha")[0])
        finally:
            worker_conn.close()

    worker = threading.Thread(target=evaluate)
    worker.start()
    deadline = time.monotonic() + 2.0
    while not multiprocessing.active_children() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert multiprocessing.active_children()
    assert unregister_kanban_dependency_providers(owner="plugin-a") == 1
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert result and result[0]["status"] == "unknown"
    assert result[0]["diagnostics"]["reason"] == "provider_unavailable"
    assert multiprocessing.active_children() == []


def test_dependency_resolution_rejects_wrong_board_before_task_lookup(board_db):
    conn, _ = board_db
    with pytest.raises(ValueError, match="does not match"):
        kb.resolve_task_dependencies(conn, "missing", board="beta")
    with pytest.raises(ValueError, match="does not match"):
        kb.task_dependency_evidence(conn, "missing", board="beta")


def test_duplicate_typed_link_is_idempotent_but_immutable_conflicts_fail(board_db):
    conn, _ = board_db
    parent = _new_parent(conn)
    child = kb.create_task(conn, title="child", board="alpha")
    kwargs = {
        "dependency_kind": "example.artifact",
        "provider_name": "registry",
        "metadata": {"artifact_id": "artifact-1", "receipt_generation": "r1"},
        "board": "alpha",
    }
    kb.link_tasks(conn, parent, child, **kwargs)
    before_events = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'linked'",
        (child,),
    ).fetchone()[0]
    kb.link_tasks(conn, parent, child, **kwargs)
    after_events = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'linked'",
        (child,),
    ).fetchone()[0]
    assert after_events == before_events
    with pytest.raises(ValueError, match="immutable"):
        kb.link_tasks(
            conn,
            parent,
            child,
            dependency_kind="example.artifact",
            provider_name="other-registry",
            metadata={"artifact_id": "artifact-2", "receipt_generation": "r2"},
            board="alpha",
        )
    assert kb.list_dependency_links(conn, child)[0]["provider_name"] == "registry"


def test_readiness_and_claim_share_claim_time_provider_resolution(board_db):
    conn, tmp_path = board_db
    parent = _new_parent(conn)
    state = {"satisfied": True, "generation": "g-ready"}

    def provider(_context):
        if not state["satisfied"]:
            return KanbanDependencyResult("unsatisfied")
        return KanbanDependencyResult("satisfied", generation=state["generation"])

    register_kanban_dependency_provider("sample.gate", "sample", provider)
    child = _new_provider_child(conn, parent)
    assert kb.get_task(conn, child).status == "ready"

    state.update(satisfied=False, generation="g-stale")
    assert kb.claim_task(conn, child, board="alpha", claimer="one") is None
    assert kb.get_task(conn, child).status == "todo"

    state.update(satisfied=True, generation="g-claim")
    assert kb.recompute_ready(conn, board="alpha") == 1
    claimed = kb.claim_task(conn, child, board="alpha", claimer="one")
    assert claimed is not None
    assert claimed.dependency_binding["dependencies"][0]["generation"] == "g-claim"
    run = kb.list_runs(conn, child)[-1]
    assert run.dependency_binding == claimed.dependency_binding
    assert json.loads(
        conn.execute("SELECT dependency_binding FROM task_runs WHERE id = ?", (run.id,)).fetchone()[0]
    ) == claimed.dependency_binding


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_claim_pin_is_consumed_by_worktree_and_mismatch_fails_closed(board_db):
    conn, tmp_path = board_db
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("base\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "base")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    pin = KanbanWorkspaceBasePin(head=head, tree=tree, receipt_generation="receipt-1")
    parent = _new_parent(conn)
    register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        lambda _context: KanbanDependencyResult("satisfied", generation="g-work", workspace_base=pin),
    )
    child = _new_provider_child(
        conn,
        parent,
        workspace_kind="worktree",
        workspace_path=str(repo),
        branch_name="wt/pinned",
    )
    claimed = kb.claim_task(conn, child, board="alpha", claimer="worker")
    workspace = kb.resolve_workspace(claimed, board="alpha")
    assert _git(workspace, "rev-parse", "HEAD") == head
    assert _git(workspace, "rev-parse", "HEAD^{tree}") == tree

    bad_pin = KanbanWorkspaceBasePin(head="f" * 40, tree="0" * 40, receipt_generation="receipt-bad")
    unregister_kanban_dependency_providers()
    register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        lambda _context: KanbanDependencyResult("satisfied", generation="g-bad", workspace_base=bad_pin),
    )
    bad_child = _new_provider_child(
        conn,
        parent,
        workspace_kind="worktree",
        workspace_path=str(repo),
        branch_name="wt/bad-pin",
    )
    bad_claim = kb.claim_task(conn, bad_child, board="alpha", claimer="worker-2")
    with pytest.raises(RuntimeError, match="unavailable|wrong tree"):
        kb.resolve_workspace(bad_claim, board="alpha")


def test_board_explicit_provider_isolation_and_concurrent_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    unregister_kanban_dependency_providers()

    def provider(context):
        return KanbanDependencyResult(
            "satisfied" if context.board == "alpha" else "unsatisfied",
            generation=context.board if context.board == "alpha" else None,
            diagnostics={"board": context.board},
        )

    register_kanban_dependency_provider("sample.gate", "sample", provider)
    alpha = kb.connect(db_path=tmp_path / "alpha.db", board="alpha")
    beta = kb.connect(db_path=tmp_path / "beta.db", board="beta")
    try:
        alpha_parent = kb.create_task(alpha, title="alpha parent", board="alpha")
        beta_parent = kb.create_task(beta, title="beta parent", board="beta")
        alpha_child = _new_provider_child(alpha, alpha_parent)
        beta_child = kb.create_task(
            beta,
            title="beta child",
            parents=[beta_parent],
            dependency_kind="sample.gate",
            provider_name="sample",
            board="beta",
        )
        assert kb.get_task(alpha, alpha_child).status == "ready"
        assert kb.get_task(beta, beta_child).status == "todo"
        with pytest.raises(ValueError, match="does not match"):
            kb.recompute_ready(alpha, board="beta")
        assert kb.task_dependency_evidence(alpha, alpha_child, board="alpha")[0]["diagnostics"] == {
            "board": "alpha",
        }
        assert kb.task_dependency_evidence(beta, beta_child, board="beta")[0]["diagnostics"] == {
            "board": "beta",
        }

        claims = []
        errors = []

        def claim():
            local = kb.connect(db_path=tmp_path / "alpha.db", board="alpha")
            try:
                claims.append(kb.claim_task(local, alpha_child, board="alpha"))
            except Exception as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)
            finally:
                local.close()

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert not errors
        assert sum(item is not None for item in claims) == 1
    finally:
        alpha.close()
        beta.close()
        unregister_kanban_dependency_providers()


def test_plugin_context_registration_uses_public_provider_surface():
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    unregister_kanban_dependency_providers()
    context = PluginContext(
        PluginManifest(name="sample-plugin", key="sample-plugin"),
        PluginManager(),
    )
    context.register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        lambda _context: KanbanDependencyResult("satisfied", generation="g"),
    )
    assert ("sample.gate", "sample") in registered_kanban_dependency_providers()
    unregister_kanban_dependency_providers()


def test_provider_identity_lifecycle_is_deterministic_and_owner_safe():
    unregister_kanban_dependency_providers()
    callback = lambda _context: KanbanDependencyResult("satisfied", generation="g")
    register_kanban_dependency_provider(
        "example.artifact",
        "registry",
        callback,
        owner="plugin-a",
    )
    assert registered_kanban_dependency_providers() == (("example.artifact", "registry"),)
    with pytest.raises(ValueError, match="already registered"):
        register_kanban_dependency_provider(
            "example.artifact",
            "registry",
            callback,
            owner="plugin-b",
        )
    assert not unregister_kanban_dependency_provider(
        "example.artifact", "registry", owner="plugin-b"
    )
    assert unregister_kanban_dependency_provider(
        "example.artifact", "registry", owner="plugin-a"
    )
    assert registered_kanban_dependency_providers() == ()


def test_generic_artifact_provider_covers_stale_identity_head_tree_generation_and_stack_depth(
    board_db,
):
    conn, _ = board_db
    parent = _new_parent(conn)
    expected = {
        "artifact_id": "artifact-42",
        "head": "a" * 40,
        "tree": "b" * 40,
        "receipt_generation": "receipt-7",
        "provider_generation": "provider-7",
        "max_stack_depth": 3,
    }

    def provider(context):
        metadata = context.link["metadata"]
        checks = (
            (metadata.get("state") != "fresh", "stale"),
            (metadata.get("artifact_id") != expected["artifact_id"], "identity"),
            (metadata.get("head") != expected["head"], "head"),
            (metadata.get("tree") != expected["tree"], "tree"),
            (
                metadata.get("provider_generation") != expected["provider_generation"],
                "generation",
            ),
            (
                metadata.get("stack_depth", 0) > expected["max_stack_depth"],
                "stack_depth",
            ),
        )
        for failed, reason in checks:
            if failed:
                return KanbanDependencyResult(
                    "unsatisfied",
                    diagnostics={"reason": reason},
                )
        return KanbanDependencyResult(
            "satisfied",
            generation=expected["provider_generation"],
        )

    register_kanban_dependency_provider("example.artifact", "registry", provider)
    base = {
        "state": "fresh",
        "artifact_id": expected["artifact_id"],
        "head": expected["head"],
        "tree": expected["tree"],
        "receipt_generation": expected["receipt_generation"],
        "provider_generation": expected["provider_generation"],
        "stack_depth": 2,
    }
    passing = _new_provider_child(
        conn,
        parent,
        kind="example.artifact",
        provider="registry",
        dependency_metadata=base,
    )
    assert kb.get_task(conn, passing).status == "ready"
    assert kb.task_dependency_evidence(conn, passing, board="alpha")[0]["status"] == "satisfied"

    for field, bad_value, reason in (
        ("state", "stale", "stale"),
        ("artifact_id", "other-artifact", "identity"),
        ("head", "c" * 40, "head"),
        ("tree", "d" * 40, "tree"),
        ("provider_generation", "provider-6", "generation"),
        ("stack_depth", 4, "stack_depth"),
    ):
        metadata = dict(base)
        metadata[field] = bad_value
        child = _new_provider_child(
            conn,
            parent,
            kind="example.artifact",
            provider="registry",
            dependency_metadata=metadata,
        )
        evidence = kb.task_dependency_evidence(conn, child, board="alpha")[0]
        assert evidence["status"] == "unsatisfied"
        assert evidence["diagnostics"]["reason"] == reason
        assert kb.get_task(conn, child).status == "todo"


def test_workspace_base_pin_is_admission_gated_to_worktree_tasks(board_db):
    conn, _ = board_db
    parent = _new_parent(conn)
    pin = KanbanWorkspaceBasePin(
        head="a" * 40,
        tree="b" * 40,
        receipt_generation="receipt-1",
    )
    register_kanban_dependency_provider(
        "example.pin",
        "registry",
        lambda _context: KanbanDependencyResult(
            "satisfied",
            generation="provider-1",
            workspace_base=pin,
        ),
    )
    child = _new_provider_child(
        conn,
        parent,
        kind="example.pin",
        provider="registry",
        workspace_kind="scratch",
    )
    evidence = kb.task_dependency_evidence(conn, child, board="alpha")[0]
    assert evidence["status"] == "unknown"
    assert evidence["diagnostics"]["reason"] == "workspace_base_requires_worktree"
    assert kb.claim_task(conn, child, board="alpha", claimer="worker") is None


def test_plugin_unload_removes_only_that_plugin_provider(tmp_path, monkeypatch):
    unregister_kanban_dependency_providers()
    home = tmp_path / "hermes"
    plugin_dir = home / "plugins" / "provider_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: provider_plugin\nversion: 0.1.0\ndescription: provider test\n"
    )
    (plugin_dir / "__init__.py").write_text(
        "from hermes_cli.kanban_dependencies import KanbanDependencyResult\n"
        "\n"
        "def register(ctx):\n"
        "    ctx.register_kanban_dependency_provider(\n"
        "        'example.plugin', 'provider',\n"
        "        lambda _context: KanbanDependencyResult('satisfied', generation='g'),\n"
        "    )\n"
    )
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - provider_plugin\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = __import__("hermes_cli.plugins", fromlist=["PluginManager"]).PluginManager()
    manager.discover_and_load(force=True)
    assert ("example.plugin", "provider") in registered_kanban_dependency_providers()
    assert manager.unload_plugin("provider_plugin")
    assert ("example.plugin", "provider") not in registered_kanban_dependency_providers()
    assert not manager.unload_plugin("provider_plugin")

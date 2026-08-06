"""Behavior tests for the generic Kanban dependency-provider contract."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
import sqlite3
import concurrent.futures
from pathlib import Path
from types import MappingProxyType

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_dependencies as kd
from hermes_cli.kanban_dependencies import (
    KanbanDependencyResult,
    KanbanWorkspaceBasePin,
    register_kanban_dependency_provider,
    unregister_kanban_dependency_provider,
    registered_kanban_dependency_providers,
    unregister_kanban_dependency_providers,
)


def _context_provider(context):
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


def _unsatisfied_provider(_context):
    return KanbanDependencyResult("unsatisfied")


def _unknown_provider(_context):
    return KanbanDependencyResult("unknown")


def _exception_provider(_context):
    raise RuntimeError("provider failure")


def _malformed_provider(_context):
    return {"status": "satisfied"}


def _timeout_provider(_context):
    time.sleep(0.05)
    return KanbanDependencyResult("satisfied", generation="late")


def _infinite_provider(_context):
    while True:
        time.sleep(0.01)


def _detached_child_provider(context):
    pid_file = Path(context.link["metadata"]["pid_file"])
    child_code = (
        "import time; "
        "time.sleep(30)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(pid_file)],
        close_fds=True,
        start_new_session=True,
    )
    pid_file.write_text(str(child.pid))
    time.sleep(1.0)
    return KanbanDependencyResult("satisfied", generation="detached")


def _board_provider(context):
    return KanbanDependencyResult(
        "satisfied" if context.board == "alpha" else "unsatisfied",
        generation=context.board if context.board == "alpha" else None,
        diagnostics={"board": context.board},
    )


def _file_state_provider(context):
    state = json.loads(Path(context.link["metadata"]["state_file"]).read_text())
    if not state["satisfied"]:
        return KanbanDependencyResult("unsatisfied")
    return KanbanDependencyResult("satisfied", generation=state["generation"])


def _workspace_pin_provider(context):
    metadata = context.link["metadata"]
    pin = KanbanWorkspaceBasePin(
        head=metadata["pin_head"],
        tree=metadata["pin_tree"],
        receipt_generation=metadata["pin_receipt"],
    )
    return KanbanDependencyResult(
        "satisfied", generation=metadata["generation"], workspace_base=pin
    )


def _artifact_provider(context):
    metadata = context.link["metadata"]
    expected = {
        "artifact_id": "artifact-42",
        "head": "a" * 40,
        "tree": "b" * 40,
        "provider_generation": "provider-7",
        "max_stack_depth": 3,
    }
    checks = (
        (metadata.get("state") != "fresh", "stale"),
        (metadata.get("artifact_id") != expected["artifact_id"], "identity"),
        (metadata.get("head") != expected["head"], "head"),
        (metadata.get("tree") != expected["tree"], "tree"),
        (metadata.get("provider_generation") != expected["provider_generation"], "generation"),
        (metadata.get("stack_depth", 0) > expected["max_stack_depth"], "stack_depth"),
    )
    for failed, reason in checks:
        if failed:
            return KanbanDependencyResult("unsatisfied", diagnostics={"reason": reason})
    return KanbanDependencyResult("satisfied", generation=expected["provider_generation"])


def _satisfied_provider(_context):
    return KanbanDependencyResult("satisfied", generation="g")


def _blocking_file_provider(context):
    metadata = context.link["metadata"]
    Path(metadata["started_file"]).write_text("started")
    release_file = Path(metadata["release_file"])
    while not release_file.exists():
        time.sleep(0.01)
    return KanbanDependencyResult("satisfied", generation="released")


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


def _new_claimed_provider_task(conn, parent_id, *, metadata):
    task_id = kb.create_task(
        conn,
        title="claimed provider task",
        assignee="programmer",
        board="alpha",
    )
    claimed = kb.claim_task(conn, task_id, board="alpha", claimer="programmer")
    assert claimed is not None
    kb.link_tasks(
        conn,
        parent_id,
        task_id,
        dependency_kind="sample.gate",
        provider_name="sample",
        metadata=metadata,
        board="alpha",
    )
    return task_id, claimed


def _wait_for_file(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"provider did not signal {path}"


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
        links = kb.list_dependency_links(conn, "c")
        assert len(links) == 1
        assert links[0]["parent_id"] == "p"
        assert links[0]["child_id"] == "c"
        assert links[0]["dependency_kind"] == "completion"
        assert links[0]["provider_name"] is None
        assert links[0]["metadata"] == {}
        assert isinstance(links[0]["metadata_digest"], str)
        assert isinstance(links[0]["edge_identity"], str)
        assert links[0]["link_version"] == 1
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

    register_kanban_dependency_provider("sample.gate", "sample", _context_provider, timeout_seconds=5.0)
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
        provider = {
            "unsatisfied": _unsatisfied_provider,
            "unknown": _unknown_provider,
            "exception": _exception_provider,
            "malformed": _malformed_provider,
            "timeout": _timeout_provider,
        }[mode]
        register_kanban_dependency_provider(
            "sample.gate",
            "sample",
            provider,
            timeout_seconds=0.005 if mode == "timeout" else 5.0,
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
    register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        _infinite_provider,
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



def test_provider_unavailable_without_containment_fails_closed(board_db, monkeypatch):
    conn, tmp_path = board_db
    parent = _new_parent(conn)
    pid_file = tmp_path / "forced-unavailable.pid"
    register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        _detached_child_provider,
        timeout_seconds=0.5,
    )
    popen_calls = []

    def forbidden_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise AssertionError("provider Popen must not run without containment")

    monkeypatch.setattr(kd, "_new_process_containment", lambda _invocation_id: None)
    monkeypatch.setattr(kd.subprocess, "Popen", forbidden_popen)
    child = _new_provider_child(
        conn,
        parent,
        dependency_metadata={"pid_file": str(pid_file)},
    )

    evidence = kb.task_dependency_evidence(conn, child, board="alpha")[0]

    assert evidence["status"] == "unknown"
    assert evidence["diagnostics"]["reason"] == "provider_containment_unavailable"
    assert popen_calls == []
    assert not pid_file.exists()
    assert multiprocessing.active_children() == []


def test_provider_timeout_kills_detached_descendants(board_db, monkeypatch):
    if not kd._containment_available():
        conn, tmp_path = board_db
        parent = _new_parent(conn)
        pid_file = tmp_path / "unavailable.pid"
        register_kanban_dependency_provider(
            "sample.gate",
            "sample",
            _detached_child_provider,
            timeout_seconds=0.5,
        )
        popen_calls = []

        def forbidden_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            raise AssertionError("provider Popen must not run without containment")

        monkeypatch.setattr(kd.subprocess, "Popen", forbidden_popen)
        child = _new_provider_child(
            conn,
            parent,
            dependency_metadata={"pid_file": str(pid_file)},
        )
        evidence = kb.task_dependency_evidence(conn, child, board="alpha")[0]
        assert evidence["diagnostics"]["reason"] == "provider_containment_unavailable"
        assert popen_calls == []
        assert not pid_file.exists()
        assert multiprocessing.active_children() == []
        return
    conn, tmp_path = board_db
    parent = _new_parent(conn)
    pid_file = tmp_path / "detached-child.pid"
    register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        _detached_child_provider,
        timeout_seconds=0.5,
    )
    child = _new_provider_child(
        conn,
        parent,
        dependency_metadata={"pid_file": str(pid_file)},
    )

    evidence = kb.task_dependency_evidence(conn, child, board="alpha")[0]

    assert evidence["status"] == "unknown"
    assert evidence["diagnostics"]["reason"] == "provider_timeout"
    deadline = time.monotonic() + 2.0
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_file.exists()
    detached_pid = int(pid_file.read_text())
    while time.monotonic() < deadline:
        try:
            os.kill(detached_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail(f"detached provider PID {detached_pid} survived cleanup")



def test_review_provider_resolution_does_not_hold_write_transaction(board_db):
    conn, tmp_path = board_db
    parent = _new_parent(conn)
    started_file = tmp_path / "review-provider-started"
    release_file = tmp_path / "review-provider-release"
    task_id, _claimed = _new_claimed_provider_task(
        conn,
        parent,
        metadata={
            "started_file": str(started_file),
            "release_file": str(release_file),
        },
    )
    register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        _blocking_file_provider,
        timeout_seconds=5.0,
    )
    submit_result = []
    submit_errors = []

    def submit():
        worker_conn = kb.connect(db_path=tmp_path / "alpha.db", board="alpha")
        try:
            submit_result.append(
                kb.submit_task_for_review(
                    worker_conn,
                    task_id,
                    "code-reviewer",
                    trusted_operator=True,
                    board="alpha",
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            submit_errors.append(exc)
        finally:
            worker_conn.close()

    submit_thread = threading.Thread(target=submit)
    writer_thread = None
    submit_thread.start()
    try:
        _wait_for_file(started_file)
        writer_done = threading.Event()
        writer_result = []
        writer_errors = []

        def competing_writer():
            writer_conn = kb.connect(db_path=tmp_path / "alpha.db", board="alpha")
            try:
                writer_result.append(
                    kb.create_task(writer_conn, title="competing writer", board="alpha")
                )
            except BaseException as exc:  # pragma: no cover - assertion below reports it
                writer_errors.append(exc)
            finally:
                writer_conn.close()
                writer_done.set()

        writer_thread = threading.Thread(target=competing_writer)
        writer_thread.start()
        assert writer_done.wait(2.0), "competing writer waited on provider resolution"
        assert writer_errors == []
        assert writer_result
        assert not release_file.exists()
    finally:
        release_file.touch()
        submit_thread.join(timeout=5.0)
        if writer_thread is not None:
            writer_thread.join(timeout=5.0)

    assert not submit_thread.is_alive()
    assert submit_errors == []
    assert submit_result and submit_result[0] is not None
    reviewed = kb.get_task(conn, task_id)
    assert reviewed is not None
    assert reviewed.status == "review"


def test_dispatch_parked_review_resolution_does_not_hold_write_transaction(
    board_db, monkeypatch
):
    conn, tmp_path = board_db
    parent = _new_parent(conn)
    started_file = tmp_path / "dispatch-provider-started"
    release_file = tmp_path / "dispatch-provider-release"
    task_id, claimed = _new_claimed_provider_task(
        conn,
        parent,
        metadata={
            "started_file": str(started_file),
            "release_file": str(release_file),
        },
    )
    assert kb.block_task(
        conn,
        task_id,
        reason="review-required: fix requested",
        kind="dependency",
        expected_run_id=claimed.current_run_id,
        expected_assignee=claimed.assignee,
        expected_claim=claimed.claim_lock,
        board="alpha",
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        _blocking_file_provider,
        timeout_seconds=5.0,
    )
    monkeypatch.setattr(kb, "recompute_ready", lambda *args, **kwargs: 0)
    dispatch_result = []
    dispatch_errors = []

    def dispatch():
        worker_conn = kb.connect(db_path=tmp_path / "alpha.db", board="alpha")
        try:
            dispatch_result.append(
                kb.dispatch_once(worker_conn, board="alpha", max_spawn=0)
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            dispatch_errors.append(exc)
        finally:
            worker_conn.close()

    dispatch_thread = threading.Thread(target=dispatch)
    writer_thread = None
    dispatch_thread.start()
    try:
        _wait_for_file(started_file)
        writer_done = threading.Event()
        writer_result = []
        writer_errors = []

        def competing_writer():
            writer_conn = kb.connect(db_path=tmp_path / "alpha.db", board="alpha")
            try:
                writer_result.append(
                    kb.create_task(
                        writer_conn,
                        title="dispatch competing writer",
                        board="alpha",
                    )
                )
            except BaseException as exc:  # pragma: no cover - assertion below reports it
                writer_errors.append(exc)
            finally:
                writer_conn.close()
                writer_done.set()

        writer_thread = threading.Thread(target=competing_writer)
        writer_thread.start()
        assert writer_done.wait(2.0), "competing writer waited on parked-review resolution"
        assert writer_errors == []
        assert writer_result
        assert not release_file.exists()
    finally:
        release_file.touch()
        dispatch_thread.join(timeout=5.0)
        if writer_thread is not None:
            writer_thread.join(timeout=5.0)

    assert not dispatch_thread.is_alive()
    assert dispatch_errors == []
    assert dispatch_result


def test_provider_unload_cancels_inflight_callback(board_db):
    conn, tmp_path = board_db
    parent = _new_parent(conn)
    register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        _infinite_provider,
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
    while not kd._ACTIVE_INVOCATIONS and time.monotonic() < deadline:
        time.sleep(0.01)
    assert kd._ACTIVE_INVOCATIONS
    assert unregister_kanban_dependency_providers(owner="plugin-a") == 1
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert result and result[0]["status"] == "unknown"
    assert result[0]["diagnostics"]["reason"] == "provider_unavailable"
    assert not kd._ACTIVE_INVOCATIONS


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
    state_file = tmp_path / "provider-state.json"
    state_file.write_text(json.dumps({"satisfied": True, "generation": "g-ready"}))
    register_kanban_dependency_provider("sample.gate", "sample", _file_state_provider, timeout_seconds=5.0)
    child = _new_provider_child(conn, parent, dependency_metadata={"state_file": str(state_file)})
    assert kb.get_task(conn, child).status == "ready"

    state_file.write_text(json.dumps({"satisfied": False, "generation": "g-stale"}))
    assert kb.claim_task(conn, child, board="alpha", claimer="one") is None
    assert kb.get_task(conn, child).status == "todo"

    state_file.write_text(json.dumps({"satisfied": True, "generation": "g-claim"}))
    assert kb.recompute_ready(conn, board="alpha") == 1
    claimed = kb.claim_task(conn, child, board="alpha", claimer="one")
    assert claimed is not None
    assert claimed.dependency_binding["dependencies"][0]["generation"] == "g-claim"
    run = kb.list_runs(conn, child)[-1]
    assert run.dependency_binding == claimed.dependency_binding
    assert json.loads(
        conn.execute("SELECT dependency_binding FROM task_runs WHERE id = ?", (run.id,)).fetchone()[0]
    ) == claimed.dependency_binding

    binding = claimed.dependency_binding
    assert binding is not None
    dependency = binding["dependencies"][0]
    assert {
        "metadata_digest",
        "edge_identity",
        "link_version",
    } <= set(dependency)
    link = kb.list_dependency_links(conn, child, board="alpha")[0]
    assert dependency["metadata_digest"] == link["metadata_digest"]
    assert dependency["edge_identity"] == link["edge_identity"]
    assert dependency["link_version"] == link["link_version"]

    tampered = json.loads(json.dumps(binding))
    tampered["dependencies"][0]["edge_identity"] = "0" * 64
    conn.execute(
        "UPDATE task_runs SET dependency_binding = ? WHERE id = ?",
        (json.dumps(tampered, sort_keys=True), run.id),
    )
    conn.commit()
    tampered_task = kb.get_task(conn, child)
    assert tampered_task is not None
    with pytest.raises(RuntimeError, match="identity|binding"):
        kb.resolve_workspace(tampered_task, board="alpha", conn=conn)


def test_concurrent_idempotent_replays_are_canonical_or_conflicts(board_db):
    conn, tmp_path = board_db
    conn.close()

    def create_one(key, title, barrier):
        worker = kb.connect(db_path=tmp_path / "alpha.db", board="alpha")
        try:
            barrier.wait(timeout=10)
            return kb.create_task(
                worker,
                title=title,
                body="body",
                assignee="worker",
                priority=3,
                idempotency_key=key,
                board="alpha",
            )
        except Exception as exc:  # asserted by the caller below
            return exc
        finally:
            worker.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for round_number in range(20):
            key = f"replay-{round_number}"
            barrier = threading.Barrier(8)
            exact = list(
                pool.map(
                    lambda _: create_one(key, "canonical", barrier),
                    range(8),
                )
            )
            assert all(not isinstance(item, Exception) for item in exact), repr(exact)
            assert len(set(exact)) == 1

            conflict_barrier = threading.Barrier(8)
            conflicts = list(
                pool.map(
                    lambda _: create_one(key, "divergent", conflict_barrier),
                    range(8),
                )
            )
            assert all(isinstance(item, ValueError) for item in conflicts)
            assert all("idempotency key conflict" in str(item) for item in conflicts)



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
    parent = _new_parent(conn)
    register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        _workspace_pin_provider,
        timeout_seconds=5.0,
    )
    child = _new_provider_child(
        conn,
        parent,
        workspace_kind="worktree",
        workspace_path=str(repo),
        branch_name="wt/pinned",
        dependency_metadata={
            "pin_head": head,
            "pin_tree": tree,
            "pin_receipt": "receipt-1",
            "generation": "g-work",
        },
    )
    claimed = kb.claim_task(conn, child, board="alpha", claimer="worker")
    assert claimed is not None
    with pytest.raises(RuntimeError, match="board-bound"):
        kb.resolve_workspace(claimed, board="alpha")
    workspace = kb.resolve_workspace(claimed, board="alpha", conn=conn)
    assert _git(workspace, "rev-parse", "HEAD") == head
    assert _git(workspace, "rev-parse", "HEAD^{tree}") == tree

    unregister_kanban_dependency_providers()
    register_kanban_dependency_provider(
        "sample.gate",
        "sample",
        _workspace_pin_provider,
        timeout_seconds=5.0,
    )
    bad_child = _new_provider_child(
        conn,
        parent,
        workspace_kind="worktree",
        workspace_path=str(repo),
        branch_name="wt/bad-pin",
        dependency_metadata={
            "pin_head": "f" * 40,
            "pin_tree": "0" * 40,
            "pin_receipt": "receipt-bad",
            "generation": "g-bad",
        },
    )
    bad_claim = kb.claim_task(conn, bad_child, board="alpha", claimer="worker-2")
    assert bad_claim is not None
    with pytest.raises(RuntimeError, match="unavailable|wrong tree"):
        kb.resolve_workspace(bad_claim, board="alpha", conn=conn)


def test_board_explicit_provider_isolation_and_concurrent_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    unregister_kanban_dependency_providers()

    register_kanban_dependency_provider("sample.gate", "sample", _board_provider, timeout_seconds=5.0)
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
        _satisfied_provider,
        timeout_seconds=5.0,
    )
    assert ("sample.gate", "sample") in registered_kanban_dependency_providers()
    unregister_kanban_dependency_providers()


def test_provider_identity_lifecycle_is_deterministic_and_owner_safe():
    unregister_kanban_dependency_providers()
    callback = _satisfied_provider
    register_kanban_dependency_provider(
        "example.artifact",
        "registry",
        callback,
        timeout_seconds=5.0,
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

    register_kanban_dependency_provider("example.artifact", "registry", _artifact_provider, timeout_seconds=5.0)
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
    register_kanban_dependency_provider(
        "example.pin",
        "registry",
        _workspace_pin_provider,
        timeout_seconds=5.0,
    )
    child = _new_provider_child(
        conn,
        parent,
        kind="example.pin",
        provider="registry",
        workspace_kind="scratch",
        dependency_metadata={
            "pin_head": "a" * 40,
            "pin_tree": "b" * 40,
            "pin_receipt": "receipt-1",
            "generation": "provider-1",
        },
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
        "def provider(_context):\n"
        "    return KanbanDependencyResult('satisfied', generation='g')\n"
        "\n"
        "def register(ctx):\n"
        "    ctx.register_kanban_dependency_provider(\n"
        "        'example.plugin', 'provider',\n"
        "        provider,\n"
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

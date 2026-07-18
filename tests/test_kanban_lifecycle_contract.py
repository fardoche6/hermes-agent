from __future__ import annotations

import concurrent.futures
import sqlite3

import pytest

from hermes_cli import kanban_db as kb


def _conn(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    return kb.connect(home / "kanban.db")


def test_review_required_task_cannot_complete_without_review_and_merge_proof(tmp_path):
    conn = _conn(tmp_path)
    try:
        task_id = kb.create_task(
            conn,
            title="coding task",
            assignee="programmer",
            task_type="coding",
            review_required=True,
            dispatcher_state={"state_type": "coding-admission-v1", "review_capacity": 1, "review_sla_seconds": 3600, "typed_predicates": ["task_type", "review_required"]},
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "repo"),
        )
        assert kb.claim_task(conn, task_id)
        assert kb.complete_task(conn, task_id, summary="finished") is False
        assert kb.get_task(conn, task_id).status == "running"

        assert kb.submit_review(conn, task_id, reviewer_assignee="code-reviewer")
        assert kb.claim_review_task(conn, task_id, claimer="code-reviewer")
        assert kb.review_verdict(conn, task_id, verdict="approved", reviewer="code-reviewer")
        assert kb.complete_task(conn, task_id, summary="merged", metadata={
            "review_evidence": {"verdict": "approved", "reviewer": "code-reviewer"},
            "merge_evidence": {"pr": 123, "merged": True},
        })
        assert kb.get_task(conn, task_id).status == "done"
    finally:
        conn.close()


def test_request_changes_returns_same_card_to_original_programmer(tmp_path):
    conn = _conn(tmp_path)
    try:
        task_id = kb.create_task(
            conn,
            title="coding task",
            assignee="programmer",
            task_type="coding",
            review_required=True,
            dispatcher_state={"state_type": "coding-admission-v1", "review_capacity": 1, "review_sla_seconds": 3600, "typed_predicates": ["task_type", "review_required"]},
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "repo"),
        )
        kb.claim_task(conn, task_id)
        assert kb.submit_review(conn, task_id, reviewer_assignee="code-reviewer")
        kb.claim_review_task(conn, task_id, claimer="code-reviewer")
        assert kb.review_verdict(conn, task_id, verdict="changes_requested", reviewer="code-reviewer")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.assignee == "programmer"
        assert kb.claim_task(conn, task_id, claimer="programmer") is not None
        assert task.review_verdict == "changes_requested"
    finally:
        conn.close()


def test_idempotency_is_atomic_across_connections(tmp_path):
    db = tmp_path / "kanban.db"
    kb.connect(db).close()

    def create():
        conn = kb.connect(db)
        try:
            return kb.create_task(
                conn,
                title="same",
                assignee="programmer",
                idempotency_key="same-key",
            )
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: create(), range(8)))
    assert len(set(ids)) == 1
    conn = kb.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key = 'same-key'").fetchone()[0] == 1
    finally:
        conn.close()


def test_false_positive_review_markers_do_not_approve(tmp_path):
    conn = _conn(tmp_path)
    try:
        task_id = kb.create_task(
            conn,
            title="coding",
            assignee="programmer",
            task_type="coding",
            review_required=True,
            dispatcher_state={"state_type": "coding-admission-v1", "review_capacity": 1, "review_sla_seconds": 3600, "typed_predicates": ["task_type", "review_required"]},
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "repo"),
        )
        kb.claim_task(conn, task_id)
        assert kb.complete_task(conn, task_id, summary="review approved in a comment") is False
        assert kb.get_task(conn, task_id).status == "running"
    finally:
        conn.close()


def test_canonical_goal_is_atomic_without_idempotency_key(tmp_path):
    db = tmp_path / "kanban.db"
    kb.connect(db).close()

    def create():
        conn = kb.connect(db)
        try:
            return kb.create_task(conn, title="same goal", body="same body")
        except ValueError as exc:
            return str(exc)
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: create(), range(8)))
    ids = [value for value in results if value.startswith("t_")]
    errors = [value for value in results if value.startswith("canonical goal")]
    assert len(ids) == 1
    assert len(errors) == 7


def test_legacy_duplicate_keys_are_reconciled_on_migration(tmp_path):
    db = tmp_path / "kanban.db"
    conn = kb.connect(db)
    conn.execute("DROP INDEX idx_tasks_idempotency_unique")
    conn.execute("DROP INDEX idx_tasks_canonical_goal_unique")
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, workspace_kind, idempotency_key, canonical_goal) "
        "VALUES ('legacy-a', 'legacy', 'done', 1, 'scratch', 'dup', 'dup-goal')"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, workspace_kind, idempotency_key, canonical_goal) "
        "VALUES ('legacy-b', 'legacy', 'done', 2, 'scratch', 'dup', 'dup-goal')"
    )
    conn.commit()
    conn.close()
    kb.init_db(db)
    conn = kb.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key = 'dup'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE canonical_goal = 'dup-goal'").fetchone()[0] == 1
    finally:
        conn.close()

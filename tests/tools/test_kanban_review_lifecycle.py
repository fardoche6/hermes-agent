"""Hermetic same-card implementer/reviewer lifecycle coverage."""
from __future__ import annotations

import json


def _db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return kb


def test_submit_review_claim_and_request_changes_return_same_card(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="repair", assignee="programmer")
        claimed = kb.claim_task(conn, task_id, claimer="host:implementer")
        assert claimed is not None
        run_id = claimed.current_run_id
        assert kb.submit_review(conn, task_id, expected_run_id=run_id, claimer="host:implementer")
        assert kb.get_task(conn, task_id).status == "review"
        reviewed = kb.claim_review_task(conn, task_id, claimer="host:reviewer")
        assert reviewed is not None
        review_run_id = reviewed.current_run_id
        assert kb.review_verdict(
            conn, task_id, approved=False, summary="Fix the failing assertion",
            metadata={"findings": ["assertion"]}, expected_run_id=review_run_id,
            claimer="host:reviewer",
        )
        task = kb.get_task(conn, task_id)
        assert task.status == "ready"
        assert task.assignee == "programmer"
        assert task.current_run_id is None
        events = [row["kind"] for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        )]
        assert "review_submitted" in events
        assert "review_verdict" in events
    finally:
        conn.close()


def test_approved_verdict_completes_only_reviewers_claimed_card(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="repair", assignee="programmer")
        claimed = kb.claim_task(conn, task_id, claimer="host:implementer")
        assert claimed is not None
        assert kb.submit_review(conn, task_id, expected_run_id=claimed.current_run_id, claimer="host:implementer")
        reviewed = kb.claim_review_task(conn, task_id, claimer="host:reviewer")
        assert reviewed is not None
        assert kb.review_verdict(
            conn, task_id, approved=True, summary="Approved after tests",
            metadata={"merge_proof": {"commit": "abc123", "checks": ["pytest"]}, "live_proof": {"checks": ["pytest"]}},
            expected_run_id=reviewed.current_run_id, claimer="host:reviewer",
        )
        assert kb.get_task(conn, task_id).status == "review_approved"
        assert kb.get_task(conn, task_id).status != "done"
        assert kb.complete_task(conn, task_id, summary="completed after approved review", metadata={"review_status": "approved"})
        assert kb.get_task(conn, task_id).status == "done"
        payloads = [json.loads(row["payload"]) for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'review_verdict'", (task_id,)
        )]
        assert payloads[-1]["verdict"] == "approved"
    finally:
        conn.close()


def test_review_handlers_fail_closed_without_exact_worker_tokens(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_missing")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK", raising=False)
    from tools import kanban_tools
    result = kanban_tools._handle_submit_review({"task_id": "t_missing"})
    assert "HERMES_KANBAN_RUN_ID" in result
    assert "HERMES_KANBAN_CLAIM_LOCK" in result


def test_review_claim_uses_independent_profile_and_recovery_preserves_review(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="repair", assignee="programmer")
        impl = kb.claim_task(conn, task_id, claimer="host:implementer")
        assert impl is not None
        assert kb.submit_review(conn, task_id, expected_run_id=impl.current_run_id, claimer="host:implementer")
        reviewer = kb.claim_review_task(conn, task_id, claimer="host:reviewer", reviewer_profile="code-reviewer")
        assert reviewer is not None
        profile = conn.execute("SELECT profile FROM task_runs WHERE id = ?", (reviewer.current_run_id,)).fetchone()["profile"]
        assert profile == "code-reviewer"
        conn.execute("UPDATE tasks SET claim_expires = 0, worker_pid = NULL WHERE id = ?", (task_id,))
        assert kb.release_stale_claims(conn) == 1
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "review"
    finally:
        conn.close()


def test_manual_reclaim_preserves_review_phase(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="repair", assignee="programmer")
        impl = kb.claim_task(conn, task_id, claimer="host:implementer")
        assert impl is not None
        assert kb.submit_review(conn, task_id, expected_run_id=impl.current_run_id, claimer="host:implementer")
        reviewer = kb.claim_review_task(conn, task_id, claimer="host:reviewer")
        assert reviewer is not None
        assert kb.reclaim_task(conn, task_id, reason="dead reviewer", signal_fn=lambda *_: None)
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "review"
    finally:
        conn.close()

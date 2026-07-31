"""Tests for the merge-proof gate in kanban completion flow (Opt 7).

Mechanically prevents premature completion of repo/coding cards from implementation
or reviewer approval alone when merged/resolved proof is required.
"""

import sqlite3
import pytest
import hermes_cli.kanban_db as kb
from tools import kanban_tools


@pytest.fixture
def db_conn(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        yield conn


def _start_task(conn: sqlite3.Connection, task_id: str):
    """Helper to transition task to running state."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))


def test_repo_card_with_pr_review_event_blocks_completion_without_proof(db_conn):
    tid = kb.create_task(
        db_conn,
        title="repo task with review",
        assignee="worker",
        workspace_kind="worktree",
        branch_name="feature/foo",
    )
    _start_task(db_conn, tid)
    # Add durable PR review event
    with kb.write_txn(db_conn):
        kb._append_event(db_conn, tid, "submitted_for_review", {"pr_url": "https://github.com/org/repo/pull/1"})

    with pytest.raises(kb.MergeProofRequiredError) as exc_info:
        kb.complete_task(db_conn, tid, summary="I finished implementation")

    assert tid in str(exc_info.value)
    task = kb.get_task(db_conn, tid)
    assert task.status == "running"

    events = [e.kind for e in kb.list_events(db_conn, tid)]
    assert "completion_blocked_missing_merge_proof" in events


def test_repo_card_with_pr_comment_blocks_completion_without_proof(db_conn):
    tid = kb.create_task(
        db_conn,
        title="repo task with pr comment",
        assignee="worker",
        workspace_kind="worktree",
        branch_name="feature/bar",
    )
    _start_task(db_conn, tid)
    kb.add_comment(db_conn, tid, "worker", "PR opened: https://github.com/org/repo/pull/42")

    with pytest.raises(kb.MergeProofRequiredError):
        kb.complete_task(db_conn, tid, summary="Done coding")

    task = kb.get_task(db_conn, tid)
    assert task.status == "running"


def test_repo_card_with_top_level_merged_sha_succeeds(db_conn):
    tid = kb.create_task(
        db_conn,
        title="repo task to complete with sha",
        assignee="worker",
        workspace_kind="worktree",
        branch_name="feature/baz",
    )
    _start_task(db_conn, tid)
    with kb.write_txn(db_conn):
        kb._append_event(db_conn, tid, "submitted_for_review", {})

    sha = "a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4"
    ok = kb.complete_task(
        db_conn,
        tid,
        summary="Merged to main",
        metadata={"merged_sha": sha},
    )
    assert ok is True
    task = kb.get_task(db_conn, tid)
    assert task.status == "done"


def test_repo_card_with_nested_merge_proof_sha_succeeds(db_conn):
    tid = kb.create_task(
        db_conn,
        title="repo task nested metadata",
        assignee="worker",
        workspace_kind="worktree",
        branch_name="feature/qux",
    )
    _start_task(db_conn, tid)
    with kb.write_txn(db_conn):
        kb._append_event(db_conn, tid, "changes_requested", {})

    ok = kb.complete_task(
        db_conn,
        tid,
        summary="PR merged",
        metadata={"merge_proof": {"merge_commit_sha": "1234567890abcdef"}},
    )
    assert ok is True
    task = kb.get_task(db_conn, tid)
    assert task.status == "done"


def test_repo_card_with_non_merge_resolutions_succeed(db_conn):
    for res in ("closed_without_merge", "superseded", "no_code_change"):
        tid = kb.create_task(
            db_conn,
            title=f"repo task resolution {res}",
            assignee="worker",
            workspace_kind="worktree",
            branch_name=f"feature/{res}",
        )
        _start_task(db_conn, tid)
        with kb.write_txn(db_conn):
            kb._append_event(db_conn, tid, "submitted_for_review", {})

        ok = kb.complete_task(
            db_conn,
            tid,
            summary=f"Resolved via {res}",
            metadata={"pr_resolution": res},
        )
        assert ok is True
        task = kb.get_task(db_conn, tid)
        assert task.status == "done"


def test_repo_card_with_durable_merge_event_succeeds(db_conn):
    tid = kb.create_task(
        db_conn,
        title="repo task with merge event",
        assignee="worker",
        workspace_kind="worktree",
        branch_name="feature/event",
    )
    _start_task(db_conn, tid)
    with kb.write_txn(db_conn):
        kb._append_event(db_conn, tid, "submitted_for_review", {})
        kb._append_event(db_conn, tid, "pr_merged", {"commit": "deadbeef"})

    ok = kb.complete_task(db_conn, tid, summary="Completed after async merge event")
    assert ok is True
    task = kb.get_task(db_conn, tid)
    assert task.status == "done"


def test_non_repo_card_succeeds_without_merge_proof(db_conn):
    tid = kb.create_task(
        db_conn,
        title="prose / non-repo card",
        assignee="worker",
        workspace_kind="scratch",
    )
    _start_task(db_conn, tid)
    with kb.write_txn(db_conn):
        kb._append_event(db_conn, tid, "submitted_for_review", {})

    ok = kb.complete_task(db_conn, tid, summary="Docs updated")
    assert ok is True
    task = kb.get_task(db_conn, tid)
    assert task.status == "done"


def test_repo_card_without_pr_provenance_succeeds_without_merge_proof(db_conn):
    tid = kb.create_task(
        db_conn,
        title="repo card without PR or review",
        assignee="worker",
        workspace_kind="worktree",
        branch_name="feature/no-pr",
    )
    _start_task(db_conn, tid)

    ok = kb.complete_task(db_conn, tid, summary="Direct repo work done")
    assert ok is True
    task = kb.get_task(db_conn, tid)
    assert task.status == "done"


def test_invalid_merged_sha_or_ambiguous_resolution_rejected(db_conn):
    tid = kb.create_task(
        db_conn,
        title="repo task invalid sha",
        assignee="worker",
        workspace_kind="worktree",
        branch_name="feature/invalid",
    )
    _start_task(db_conn, tid)
    with kb.write_txn(db_conn):
        kb._append_event(db_conn, tid, "submitted_for_review", {})

    # Non-hex SHA rejected
    with pytest.raises(kb.MergeProofRequiredError):
        kb.complete_task(db_conn, tid, summary="done", metadata={"merged_sha": "not-a-sha!"})

    # pr_resolution="merged" without a SHA rejected
    with pytest.raises(kb.MergeProofRequiredError):
        kb.complete_task(db_conn, tid, summary="done", metadata={"pr_resolution": "merged"})


def test_kanban_complete_tool_handles_merge_proof_error(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="tool test merge proof",
            assignee="test-worker",
            workspace_kind="worktree",
            branch_name="feature/tool-test",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "submitted_for_review", {})

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    # Call kanban_tools._handle_complete without merge proof
    res = kanban_tools._handle_complete({"task_id": tid, "summary": "Finished work"})
    assert "error" in res.lower()
    assert "merge proof" in res.lower()
    assert "merged_sha" in res

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "running"

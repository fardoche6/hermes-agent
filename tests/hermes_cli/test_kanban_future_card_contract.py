"""Executable proof for the documented future-card Kanban contract.

These tests use only an isolated temporary HERMES_HOME. They intentionally
exercise the existing DB/API rails rather than reading source text or touching
a live board.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from hermes_cli import kanban_db as kb


def _remote_workspace(tmp_path):
    repo = tmp_path / "repo"
    bare = tmp_path / "remote.git"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)], check=True)
    (repo / "proof.txt").write_text("proof\n")
    for command in (
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        ["git", "-C", str(repo), "add", "proof.txt"],
        ["git", "-C", str(repo), "commit", "-m", "proof"],
        ["git", "-C", str(repo), "push", "-u", "origin", "main"],
    ):
        subprocess.run(command, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return repo, commit


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def test_admission_is_idempotent_and_readback_is_explicit(kanban_home):
    workspace = kanban_home / "worktree"
    workspace.mkdir()

    with kb.connect() as conn:
        first = kb.create_task(
            conn,
            title="bounded future-card proof",
            body="Goal and acceptance criteria are specified.",
            assignee="alice",
            tenant="process-test",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            idempotency_key="future-card-proof-v1",
        )
        second = kb.create_task(
            conn,
            title="duplicate retry must reuse card",
            assignee="alice",
            tenant="process-test",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            idempotency_key="future-card-proof-v1",
        )
        task = kb.get_task(conn, first)

    assert second == first
    assert task is not None
    assert task.status == "ready"
    assert task.assignee == "alice"
    assert task.tenant == "process-test"
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(workspace)


def test_admission_routes_only_unresolved_specs_to_triage(kanban_home):
    with kb.connect() as conn:
        ready_id = kb.create_task(
            conn,
            title="specified task",
            body="Complete specification",
            assignee="alice",
        )
        triage_id = kb.create_task(
            conn,
            title="rough idea",
            body="Needs decomposition",
            assignee="alice",
            triage=True,
        )
        ready = kb.get_task(conn, ready_id)
        triage = kb.get_task(conn, triage_id)

    assert ready is not None and ready.status == "ready"
    assert triage is not None and triage.status == "triage"


def test_recovery_returns_to_same_card_without_duplicate(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="same-card recovery",
            assignee="alice",
        )
        assert kb.claim_task(conn, task_id) is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="workspace capability needs operator repair",
            kind="capability",
        )
        blocked = kb.get_task(conn, task_id)
        assert blocked is not None and blocked.status == "blocked"
        assert kb.unblock_task(conn, task_id)
        recovered = kb.get_task(conn, task_id)
        tasks = kb.list_tasks(conn, include_archived=True)

    assert recovered is not None and recovered.id == task_id
    assert recovered.status == "ready"
    assert [task.id for task in tasks] == [task_id]


def test_review_submit_verdict_and_completion_keep_one_card_identity(kanban_home):
    """Implementation handoff, reviewer claim, verdict, and completion stay
    on one task id, with approval distinct from completion."""
    repo, commit = _remote_workspace(kanban_home.parent)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="same-card review", assignee="alice",
            workspace_kind="worktree", workspace_path=str(repo),
        )
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.submit_review(
            conn, task_id, summary="implementation handoff",
            metadata={"tests_run": ["synthetic"]},
            expected_run_id=implementation.current_run_id,
            claimer=implementation.claim_lock,
        )
        assert kb.get_task(conn, task_id).status == "review"
        reviewed = kb.claim_review_task(conn, task_id, claimer="reviewer:test")
        assert reviewed is not None
        assert reviewed.id == task_id
        assert reviewed.status == "running"
        assert kb.review_verdict(
            conn, task_id, verdict="approved", summary="review passed",
            metadata={
                "merge_proof": {"commit": commit, "state": "merged", "checks": {"pytest": {"status": "passed"}}, "review_threads": {"resolved": 1, "total": 1}, "readback": {"status": "verified"}},
                "live_proof": {"checks": {"health": {"status": "healthy"}}},
            },
            expected_run_id=reviewed.current_run_id, claimer="reviewer:test",
        )
        assert kb.get_task(conn, task_id).status == "review_approved"
        assert kb.get_task(conn, task_id).status != "done"
        finalization = kb.get_task(conn, task_id)
        assert finalization is not None
        assert finalization.current_run_id is not None
        assert kb.complete_task(
            conn,
            task_id,
            summary="review proof recorded on original card",
            metadata={"review_status": "approved", "tests_run": ["synthetic"]},
            expected_run_id=finalization.current_run_id,
            claimer=finalization.claim_lock,
        )
        final = kb.get_task(conn, task_id)
        tasks = kb.list_tasks(conn, include_archived=True)

    assert final is not None and final.id == task_id
    assert final.status == "done"
    assert [task.id for task in tasks] == [task_id]


def test_review_changes_requested_returns_same_card_to_ready(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review revisions", assignee="alice")
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.submit_review(
            conn, task_id, summary="handoff", expected_run_id=implementation.current_run_id,
            claimer=implementation.claim_lock,
        )
        review_run = kb.claim_review_task(conn, task_id, claimer="reviewer:test")
        assert review_run is not None
        assert kb.review_verdict(
            conn, task_id, verdict="changes_requested", summary="fix test",
            expected_run_id=review_run.current_run_id, claimer="reviewer:test",
        )
        task = kb.get_task(conn, task_id)
        tasks = kb.list_tasks(conn, include_archived=True)

    assert task is not None and task.id == task_id and task.status == "ready"
    assert [item.id for item in tasks] == [task_id]

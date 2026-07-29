"""Hermetic same-card implementer/reviewer lifecycle coverage."""
from __future__ import annotations

import json
import subprocess

import pytest


def _db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    # The dispatcher deliberately rejects assignees that are not real Hermes
    # profiles. Create the two profiles used by this hermetic board instead of
    # weakening that production safety check.
    for profile in ("programmer", "code-reviewer"):
        (home / "profiles" / profile).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return kb


def _remote_workspace(tmp_path):
    repo = tmp_path / "repo"
    bare = tmp_path / "remote.git"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)], check=True)
    (repo / "proof.txt").write_text("proof\n")
    commands = [
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        ["git", "-C", str(repo), "add", "proof.txt"],
        ["git", "-C", str(repo), "commit", "-m", "proof"],
        ["git", "-C", str(repo), "push", "-u", "origin", "main"],
    ]
    for command in commands:
        subprocess.run(command, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return repo, commit


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
    repo, commit = _remote_workspace(tmp_path)
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="repair", assignee="programmer",
            workspace_kind="worktree", workspace_path=str(repo),
        )
        claimed = kb.claim_task(conn, task_id, claimer="host:implementer")
        assert claimed is not None
        assert kb.submit_review(conn, task_id, expected_run_id=claimed.current_run_id, claimer="host:implementer")
        reviewed = kb.claim_review_task(conn, task_id, claimer="host:reviewer")
        assert reviewed is not None
        assert kb.review_verdict(
            conn, task_id, approved=True, summary="Approved after tests",
            metadata={"merge_proof": {"commit": commit, "state": "merged", "checks": {"pytest": {"status": "passed"}}, "review_threads": {"resolved": 1, "total": 1}, "readback": {"status": "verified"}}, "live_proof": {"checks": {"health": {"status": "healthy"}}}},
            expected_run_id=reviewed.current_run_id, claimer="host:reviewer",
        )
        assert kb.get_task(conn, task_id).status == "review_approved"
        assert kb.get_task(conn, task_id).status != "done"
        assert not kb.complete_task(conn, task_id, summary="stale implementer", metadata={"review_status": "approved"}, expected_run_id=claimed.current_run_id, claimer="host:implementer")
        final = kb.get_task(conn, task_id)
        assert final.current_run_id != reviewed.current_run_id
        assert kb.complete_task(conn, task_id, summary="completed after approved review", metadata={"review_status": "approved"}, expected_run_id=final.current_run_id, claimer=final.claim_lock)
        assert kb.get_task(conn, task_id).status == "done"
        payloads = [json.loads(row["payload"]) for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'review_verdict'", (task_id,)
        )]
        assert payloads[-1]["verdict"] == "approved"
    finally:
        conn.close()


def test_dashboard_style_review_queue_assigns_reviewer_on_same_card(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="dashboard handoff", assignee="programmer")
        implementation = kb.claim_task(conn, task_id, claimer="host:implementer")
        assert implementation is not None
        assert kb.queue_review(
            conn,
            task_id,
            reviewer_profile="code-reviewer",
            summary="ready for independent review",
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "review"
        assert task.assignee == "code-reviewer"
        assert task.claim_lock is None
        assert task.current_run_id is None
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


def test_reviewer_timeout_auto_block_is_not_left_dispatchable(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="review timeout", assignee="programmer", max_retries=1,
        )
        impl = kb.claim_task(conn, task_id, claimer="host:implementer")
        assert impl is not None
        assert kb.submit_review(
            conn, task_id, expected_run_id=impl.current_run_id,
            claimer="host:implementer",
        )
        reviewer = kb.claim_review_task(
            conn, task_id, claimer=kb._claimer_id(), reviewer_profile="code-reviewer",
        )
        assert reviewer is not None
        conn.execute(
            "UPDATE tasks SET max_runtime_seconds = 1, started_at = 0, worker_pid = 999999 "
            "WHERE id = ?", (task_id,),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = 0 WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)", (task_id,),
        )
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        assert kb.enforce_max_runtime(conn, signal_fn=lambda *_args: None) == [task_id]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.consecutive_failures == 1
        assert getattr(kb.enforce_max_runtime, "_last_auto_blocked") == [task_id]
        assert kb.dispatch_once(
            conn, spawn_fn=lambda *_args: pytest.fail("redispatched"), failure_limit=1,
        ).spawned == []
    finally:
        conn.close()


def test_review_dispatch_fresh_startup_uses_no_missing_forced_skill(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    conn = kb.connect()
    captured = []
    try:
        monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
        task_id = kb.create_task(conn, title="review startup", assignee="programmer")
        impl = kb.claim_task(conn, task_id, claimer="host:implementer")
        assert impl is not None
        assert kb.submit_review(conn, task_id, expected_run_id=impl.current_run_id, claimer="host:implementer")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: captured.append(task) or 123,
        )
        assert result.spawned
        assert captured[0].assignee != "programmer"
        assert captured[0].skills == []
        assert "sdlc-review" not in captured[0].skills
    finally:
        conn.close()


def test_review_handlers_reject_wrong_run_claim_and_missing_proof(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    from tools import kanban_tools
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="handler gates", assignee="programmer")
        impl = kb.claim_task(conn, task_id, claimer="host:implementer")
        assert impl is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(int(impl.current_run_id or 0) + 1))
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", impl.claim_lock)
        assert "could not submit" in kanban_tools._handle_submit_review({"summary": "handoff"})
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(impl.current_run_id))
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", impl.claim_lock)
        assert json.loads(kanban_tools._handle_submit_review({"summary": "handoff"}))["ok"]
        reviewer = kb.claim_review_task(conn, task_id, claimer="host:reviewer", reviewer_profile="code-reviewer")
        assert reviewer is not None
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(reviewer.current_run_id))
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:reviewer")
        assert "could not record verdict" in kanban_tools._handle_review_verdict({"verdict": "approved", "metadata": {}})
    finally:
        conn.close()


@pytest.mark.parametrize("proof", [True, False, "proof", {}, {"merge_proof": {}, "live_proof": {}}])
def test_approved_review_rejects_non_structured_proof(tmp_path, monkeypatch, proof):
    kb = _db(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="proof gate", assignee="programmer")
        impl = kb.claim_task(conn, task_id, claimer="host:impl")
        assert kb.submit_review(conn, task_id, expected_run_id=impl.current_run_id, claimer="host:impl")
        reviewer = kb.claim_review_task(conn, task_id, claimer="host:review", reviewer_profile="code-reviewer")
        assert not kb.review_verdict(conn, task_id, verdict="approved", metadata={"merge_proof": proof, "live_proof": proof}, expected_run_id=reviewer.current_run_id, claimer="host:review")
        assert kb.get_task(conn, task_id).status == "running"
    finally:
        conn.close()


def test_reviewer_cannot_resubmit_its_own_run(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="self submit", assignee="programmer")
        impl = kb.claim_task(conn, task_id, claimer="host:impl")
        assert kb.submit_review(conn, task_id, expected_run_id=impl.current_run_id, claimer="host:impl")
        reviewer = kb.claim_review_task(conn, task_id, claimer="host:review", reviewer_profile="code-reviewer")
        assert not kb.submit_review(conn, task_id, expected_run_id=reviewer.current_run_id, claimer="host:review")
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_task(conn, task_id).consecutive_failures == 0
    finally:
        conn.close()


def test_reviewer_fallback_skips_unavailable_preferred_profile(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    conn = kb.connect()
    captured = []
    try:
        monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "code-reviewer-claudecode")
        task_id = kb.create_task(conn, title="fallback", assignee="programmer")
        impl = kb.claim_task(conn, task_id, claimer="host:impl")
        assert kb.submit_review(conn, task_id, expected_run_id=impl.current_run_id, claimer="host:impl")
        result = kb.dispatch_once(conn, spawn_fn=lambda task, _workspace: captured.append(task) or 123)
        assert result.spawned
        assert captured[0].assignee == "code-reviewer-claudecode"
        assert kb.get_task(conn, task_id).assignee == "programmer"
    finally:
        conn.close()

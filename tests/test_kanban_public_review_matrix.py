from __future__ import annotations

import json

from hermes_cli import kanban_db as kb


PACKET = {
    "state_type": "coding-admission-v1",
    "review_capacity": 2,
    "review_capacity_per_profile": 2,
    "review_sla_seconds": 60,
    "typed_predicates": ["task_type", "review_required"],
}


def _task(conn, tmp_path, title):
    tid = kb.create_task(
        conn,
        title=title,
        assignee="programmer",
        task_type="coding",
        review_required=True,
        dispatcher_state=PACKET,
        workspace_kind="worktree",
        workspace_path=str(tmp_path / title),
    )
    assert kb.claim_task(conn, tid, claimer="programmer")
    return tid


def test_public_review_tools_clear_claim_state_for_both_verdicts(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "matrix")
    monkeypatch.setenv("HERMES_PROFILE", "programmer")
    from tools import kanban_tools as tools

    conn = kb.connect(home / "kanban.db")
    try:
        for title, verdict in (("approved", "approved"), ("changes", "changes_requested")):
            tid = _task(conn, tmp_path, title)
            monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
            assert json.loads(tools._handle_submit_review({"reviewer_assignee": "reviewer"}))["ok"]
            monkeypatch.setenv("HERMES_PROFILE", "reviewer")
            assert kb.claim_review_task(conn, tid, claimer="reviewer") is not None
            assert json.loads(tools._handle_review_verdict({"verdict": verdict, "evidence": {"real": True}}))["ok"]
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.claim_lock is None
            assert task.claim_expires is None
            assert task.worker_pid is None
            assert task.current_run_id is None
            run = kb.latest_run(conn, tid)
            assert run is not None and run.ended_at is not None
            if verdict == "approved":
                assert task.status == "review"
                assert task.review_verdict == "approved"
                # Approved work is not eligible for review redispatch.
                result = kb.dispatch_once(conn, dry_run=True)
                assert tid not in [item[0] for item in result.spawned]
            else:
                assert task.status == "ready"
                assert task.assignee == "programmer"
                monkeypatch.setenv("HERMES_PROFILE", "programmer")
                assert kb.claim_task(conn, tid, claimer="programmer") is not None
            monkeypatch.setenv("HERMES_PROFILE", "programmer")
    finally:
        conn.close()

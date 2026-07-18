from __future__ import annotations

import json
import subprocess

from hermes_cli import kanban_db as kb


PACKET = {
    "state_type": "coding-admission-v1",
    "review_capacity": 1,
    "review_capacity_per_profile": 1,
    "review_sla_seconds": 60,
    "typed_predicates": ["task_type", "review_required"],
}


def _coding(conn, tmp_path, title, reviewer="reviewer"):
    workspace = tmp_path / title.replace(" ", "-")
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "--quiet", "--allow-empty", "-m", "init"], check=True)
    tid = kb.create_task(
        conn,
        title=title,
        assignee="programmer",
        task_type="coding",
        review_required=True,
        dispatcher_state=PACKET,
        workspace_kind="worktree",
        workspace_path=str(tmp_path / title.replace(" ", "-")),
    )
    assert kb.claim_task(conn, tid, claimer="programmer")
    assert kb.submit_review(conn, tid, reviewer_assignee=reviewer)
    return tid


def test_dispatch_review_capacity_releases_and_admits_next(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    conn = kb.connect(db)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    try:
        first = _coding(conn, tmp_path, "first")
        second = _coding(conn, tmp_path, "second")
        spawned = []

        def fake_spawn(task, workspace, board=None):
            spawned.append(task.id)
            return 4242

        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert result.spawned[0][0] == first
        assert result.spawned[0][1] == "reviewer"
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert any(item[0] == second and item[2] == "global_capacity" for item in result.skipped_review_capacity)
        assert second not in spawned

        assert kb.review_verdict(
            conn, first, verdict="changes_requested", reviewer="reviewer",
            evidence={"finding": "fix"},
        )
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert second in spawned
        assert result.spawned[-1][0] == second
    finally:
        conn.close()


def test_dispatch_review_fails_closed_for_untyped_review_row(tmp_path, monkeypatch):
    conn = kb.connect(tmp_path / "kanban.db")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    try:
        tid = kb.create_task(conn, title="untyped", assignee="reviewer")
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        result = kb.dispatch_once(conn, dry_run=True)
        assert tid not in [item[0] for item in result.spawned]
        assert any(item[0] == tid and item[2] == "typed_predicate" for item in result.skipped_review_capacity)
    finally:
        conn.close()

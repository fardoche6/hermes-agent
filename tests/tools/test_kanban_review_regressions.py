from __future__ import annotations

import subprocess

from tests.tools.test_kanban_review_lifecycle import _db, _remote_workspace


def test_worktree_card_cannot_complete_without_same_card_review(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    repo, _ = _remote_workspace(tmp_path)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review required", assignee="programmer", workspace_kind="worktree", workspace_path=str(repo))
        claimed = kb.claim_task(conn, task_id, claimer="host:impl")
        assert claimed is not None
        assert not kb.complete_task(conn, task_id, summary="bypass", expected_run_id=claimed.current_run_id, claimer="host:impl")
        assert kb.get_task(conn, task_id).status == "running"


def test_approved_review_claim_is_recovered_to_review(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    repo, commit = _remote_workspace(tmp_path)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review crash window", assignee="programmer", workspace_kind="worktree", workspace_path=str(repo))
        impl = kb.claim_task(conn, task_id, claimer="host:impl")
        assert kb.submit_review(conn, task_id, expected_run_id=impl.current_run_id, claimer="host:impl")
        reviewer = kb.claim_review_task(conn, task_id, claimer="host:review")
        proof = {"merge_proof": {"commit": commit, "state": "merged", "checks": {"pytest": {"status": "passed"}}, "review_threads": {"resolved": 1, "total": 1}, "readback": {"status": "verified"}}, "live_proof": {"checks": {"health": {"status": "healthy"}}}}
        assert kb.review_verdict(conn, task_id, verdict="approved", metadata=proof, expected_run_id=reviewer.current_run_id, claimer="host:review")
        final_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()["current_run_id"]
        conn.execute("UPDATE tasks SET claim_expires = 0, worker_pid = NULL WHERE id = ?", (task_id,))
        assert kb.release_stale_claims(conn) == 1
        assert kb.get_task(conn, task_id).status == "review_approved"
        run = conn.execute("SELECT status, outcome FROM task_runs WHERE id = ?", (reviewer.current_run_id,)).fetchone()
        assert run["status"] == "review_approved"
        assert run["outcome"] == "review_approved"
        final_run = conn.execute("SELECT status, outcome FROM task_runs WHERE id = ?", (final_id,)).fetchone()
        assert final_run["status"] == "reclaimed"
        assert final_run["outcome"] == "reclaimed"


def test_review_proof_rejects_forged_local_tracking_ref(tmp_path, monkeypatch):
    kb = _db(tmp_path, monkeypatch)
    repo, _ = _remote_workspace(tmp_path)
    (repo / "unmerged.txt").write_text("not pushed\n")
    subprocess.run(["git", "-C", str(repo), "add", "unmerged.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "unmerged"], check=True, capture_output=True)
    unmerged = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", unmerged], check=True)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="forged proof", assignee="programmer", workspace_kind="worktree", workspace_path=str(repo))
        impl = kb.claim_task(conn, task_id, claimer="host:impl")
        assert kb.submit_review(conn, task_id, expected_run_id=impl.current_run_id, claimer="host:impl")
        reviewer = kb.claim_review_task(conn, task_id, claimer="host:review")
        proof = {"merge_proof": {"commit": unmerged, "state": "merged", "checks": {"pytest": {"status": "passed"}}, "review_threads": {"resolved": 1, "total": 1}, "readback": {"status": "verified"}}, "live_proof": {"checks": {"health": {"status": "healthy"}}}}
        assert not kb.review_verdict(conn, task_id, verdict="approved", metadata=proof, expected_run_id=reviewer.current_run_id, claimer="host:review")

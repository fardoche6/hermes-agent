"""Focused regression tests for the same-card reviewer control plane.

These tests intentionally exercise the durable DB boundary, dispatcher handoff,
and worker-facing tool registration. They reproduce the production failure on
current main: a claimed reviewer has no first-class terminal decision path.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


HEAD_SHA = "855fd911d56e1c6185fda5995d8aff430d964191"
PR_URL = "https://github.com/example/repo/pull/283"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _events(conn, task_id: str) -> list[str]:
    return [
        row["kind"]
        for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
            (task_id,),
        )
    ]


def _payload(conn, task_id: str, kind: str) -> dict:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    assert row is not None, f"missing {kind} event"
    return json.loads(row["payload"] or "{}")


def _review_card(conn, *, reviewer="code-reviewer", programmer="programmer"):
    host = kb._claimer_id().split(":", 1)[0]
    task_id = kb.create_task(conn, title="review lifecycle", assignee=programmer)
    assert kb.claim_task(conn, task_id, claimer=f"{host}:implementation")
    kb.add_comment(conn, task_id, "programmer", f"PR opened: {PR_URL}")
    assert kb.submit_task_for_review(conn, task_id, reviewer)
    review = kb.claim_review_task(conn, task_id, claimer=f"{host}:review")
    assert review is not None
    assert review.status == "review"
    assert review.claim_lock == f"{host}:review"
    assert review.current_run_id is not None
    return task_id, review, host


def test_claimed_reviewer_stays_in_review_column(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.current_run_id == review.current_run_id
        assert current.assignee == "code-reviewer"


def test_approve_records_exact_head_and_routes_same_card_to_finalizer(kanban_home):
    with kb.connect() as conn:
        task_id, review, host = _review_card(conn)
        approved = kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="16/16 focused proof, CI green",
            head_sha=HEAD_SHA,
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert approved is not None
        assert approved.status == "ready"
        assert approved.assignee == "programmer"
        assert approved.claim_lock is None
        assert approved.worker_pid is None
        assert approved.current_run_id is None
        assert approved.completed_at is None
        assert len(kb.list_tasks(conn)) == 1

        evidence = _payload(conn, task_id, "review_approved")
        assert evidence["head_sha"] == HEAD_SHA
        assert evidence["reviewer"] == "code-reviewer"
        assert evidence["summary"] == "16/16 focused proof, CI green"
        assert evidence["finalizer"] == "programmer"
        run = conn.execute(
            "SELECT outcome, summary, ended_at FROM task_runs WHERE id=?",
            (review.current_run_id,),
        ).fetchone()
        assert run["outcome"] == "approved"
        assert run["summary"] == "16/16 focused proof, CI green"
        assert run["ended_at"] is not None
        assert "completed" not in _events(conn, task_id)
        assert kb.in_finalization_lane(conn, task_id) is True
        assert kb.in_correction_lane(conn, task_id) is False


def test_approve_requires_full_head_and_preserves_review_on_failure(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        with pytest.raises(ValueError, match="full 40- or 64-character"):
            kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer",
                summary="approved",
                head_sha="deadbeef",
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.current_run_id == review.current_run_id
        assert "review_approved" not in _events(conn, task_id)


def test_self_approval_and_stale_claim_fail_closed(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        with pytest.raises(RuntimeError, match="does not own"):
            kb.approve_review(
                conn,
                task_id,
                reviewer="programmer",
                summary="approved",
                head_sha=HEAD_SHA,
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
        with pytest.raises(RuntimeError, match="no longer holds"):
            kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer",
                summary="approved",
                head_sha=HEAD_SHA,
                expected_claim="stale-claim",
                expected_run_id=review.current_run_id,
            )
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "review"
        assert current.current_run_id == review.current_run_id


def test_request_changes_is_one_bundled_same_card_packet(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        with pytest.raises(RuntimeError, match="does not own"):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="programmer",
                reason="bad caller",
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
        corrected = kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason="tighten error handling",
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert corrected is not None
        assert corrected.status == "ready"
        assert corrected.assignee == "programmer"
        assert corrected.claim_lock is None
        assert corrected.worker_pid is None
        assert corrected.current_run_id is None
        assert len(kb.list_tasks(conn)) == 1
        decision = _payload(conn, task_id, "changes_requested")
        assert decision["programmer"] == "programmer"
        assert decision["reason"] == "tighten error handling"
        assert "review_approved" not in _events(conn, task_id)
        run = conn.execute(
            "SELECT outcome, ended_at FROM task_runs WHERE id=?",
            (review.current_run_id,),
        ).fetchone()
        assert run["outcome"] == "changes_requested"
        assert run["ended_at"] is not None
        assert kb.in_correction_lane(conn, task_id) is True


def test_request_changes_rejects_unclaimed_or_nonreview_status(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        conn.execute("UPDATE tasks SET claim_lock=NULL WHERE id=?", (task_id,))
        conn.commit()
        with pytest.raises(RuntimeError):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="code-reviewer",
                reason="fix",
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "review"


def test_failover_replaces_reviewer_and_rejects_stale_decision(kanban_home):
    with kb.connect() as conn:
        task_id, first, host = _review_card(conn, reviewer="reviewer-a")
        replacement_task = kb.failover_review_task(
            conn, task_id, "reviewer-b", error="reviewer-a exited",
        )
        assert replacement_task is not None
        replacement = kb.claim_review_task(
            conn, task_id, claimer=f"{host}:replacement",
        )
        assert replacement is not None
        with pytest.raises(RuntimeError):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="reviewer-a",
                reason="stale feedback",
                expected_claim=first.claim_lock,
                expected_run_id=first.current_run_id,
            )
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.assignee == "reviewer-b"
        assert current.current_run_id == replacement.current_run_id
        assert current.claim_lock == replacement.claim_lock


def test_stale_review_run_invalidates_prior_head_approval(kanban_home):
    """A replacement review run must invalidate an approval from the old run.

    Reusing the same reviewer profile and claim string is intentional: claim
    identity alone cannot distinguish the stale process, so the active run id
    is the CAS boundary.
    """
    with kb.connect() as conn:
        task_id, first, host = _review_card(conn, reviewer="code-reviewer")
        old_run_id = first.current_run_id
        old_claim = first.claim_lock
        assert old_run_id is not None
        assert old_claim is not None

        assert kb.failover_review_task(
            conn, task_id, "code-reviewer", error="reviewer process replaced",
        ) is not None
        replacement = kb.claim_review_task(conn, task_id, claimer=old_claim)
        assert replacement is not None
        assert replacement.current_run_id != old_run_id
        assert replacement.claim_lock == old_claim

        with pytest.raises(RuntimeError, match="current review run"):
            kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer",
                summary="stale approval",
                head_sha=HEAD_SHA,
                expected_claim=old_claim,
                expected_run_id=old_run_id,
            )
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.current_run_id == replacement.current_run_id
        assert "review_approved" not in _events(conn, task_id)

        approved = kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="current approval",
            head_sha=HEAD_SHA,
            expected_claim=replacement.claim_lock,
            expected_run_id=replacement.current_run_id,
        )
        assert approved is not None
        assert approved.status == "ready"


def test_repeated_approval_is_idempotent(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        approved = kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="approved once",
            head_sha=HEAD_SHA,
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert approved is not None
        before = _events(conn, task_id)

        retried = kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="approved once",
            head_sha=HEAD_SHA,
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert retried is not None
        assert retried.status == "ready"
        assert _events(conn, task_id) == before
        assert _events(conn, task_id).count("review_approved") == 1


def test_repeated_request_changes_is_idempotent(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        corrected = kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason="tighten error handling",
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert corrected is not None
        assert corrected.status == "ready"
        before = _events(conn, task_id)

        retried = kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason="tighten error handling",
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert retried is not None
        assert retried.status == "ready"
        assert _events(conn, task_id) == before
        assert _events(conn, task_id).count("changes_requested") == 1


def test_approval_leaves_no_dead_pid_for_crash_reaper(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        reviewer_pid = 555001
        kb._set_worker_pid(conn, task_id, reviewer_pid)
        approved = kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="approved",
            head_sha=HEAD_SHA,
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert approved is not None
        before = _events(conn, task_id)
        assert kb.detect_crashed_workers(conn) == []
        assert _events(conn, task_id) == before
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "ready"
        assert current.worker_pid is None
        assert current.last_failure_error is None
        assert "protocol_violation" not in before


def test_approved_card_is_claimable_by_fresh_finalizer(
    kanban_home, all_assignees_spawnable,
):
    with kb.connect() as conn:
        task_id, review, host = _review_card(conn)
        approved = kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="approved",
            head_sha=HEAD_SHA,
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert approved is not None
        finalizer = kb.claim_task(conn, task_id, claimer=f"{host}:finalizer")
        assert finalizer is not None
        assert finalizer.status == "running"
        assert finalizer.assignee == "programmer"
        assert finalizer.current_run_id != review.current_run_id


def test_approval_and_change_lanes_are_mutually_exclusive(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason="fix tests",
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert kb.in_correction_lane(conn, task_id) is True
        assert kb.in_finalization_lane(conn, task_id) is False

        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, task_id, claimer=f"{host}:implementation-2")
        assert kb.submit_task_for_review(conn, task_id, "code-reviewer")
        second_review = kb.claim_review_task(
            conn, task_id, claimer=f"{host}:review-2",
        )
        assert second_review is not None
        kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="approved",
            head_sha=HEAD_SHA,
            expected_claim=second_review.claim_lock,
            expected_run_id=second_review.current_run_id,
        )
        assert kb.in_finalization_lane(conn, task_id) is True
        assert kb.in_correction_lane(conn, task_id) is False


def test_worker_toolset_exposes_result_aware_review_transitions(
    kanban_home, monkeypatch,
):
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset
    import tools.kanban_tools  # noqa: F401 - registration side effect

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review_worker")
    invalidate_check_fn_cache()
    names = {
        definition["function"]["name"]
        for definition in registry.get_definitions(
            set(resolve_toolset("kanban")), quiet=True,
        )
        if "function" in definition
    }
    assert "kanban_approve" in names
    assert "kanban_request_changes" in names


def test_cli_help_exposes_review_decisions(kanban_home):
    import argparse

    from hermes_cli import kanban
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    kanban_parser = kanban.build_parser(subparsers)
    choices = kanban_parser._subparsers._group_actions[0].choices
    assert "approve" in choices
    assert "request-changes" in choices
    assert hasattr(kanban, "_cmd_approve") or hasattr(kanban, "_cmd_review")
    assert hasattr(kanban, "_cmd_request_changes")

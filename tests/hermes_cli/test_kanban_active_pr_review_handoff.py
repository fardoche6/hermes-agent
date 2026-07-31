"""Regression tests: an active PR must never strand a same-card correction
or a documented ``review-required:`` handoff in ``ready``.

Production sequence reproduced here (SubsidySmart coding card):

* a PR URL was posted in a task comment,
* the reviewer emitted ``changes_requested`` (same card, programmer
  correction claim),
* the correction run crashed (``pid ... not alive``) and crash recovery
  correctly returned the card to ``ready``,
* every subsequent dispatcher tick emitted
  ``respawn_guarded {reason: active_pr}`` because the guard treats *any*
  recent PR URL as an unconditional 24h veto,
* after a manual correction, the worker used the documented
  ``kanban_block(kind="dependency", reason="review-required: ...")``
  handoff, which lands in ``todo`` -> generic ``ready`` and was suppressed
  by the very same guard instead of entering the review lane.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


PR_URL = "https://github.com/totemx-AI/subsidysmart/pull/42"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _crash_current_worker(conn, task_id: str, pid: int = 987654) -> None:
    """Kill the live claim the way production did: a dead worker PID."""
    conn.execute(
        "UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?",
        (pid, int(time.time()) - 600, task_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Invariant 1 — neighboring non-triggering safety case
# ---------------------------------------------------------------------------

def test_active_pr_still_guards_generic_ready_task(kanban_home):
    """A plain ready task with only a recent PR URL and no durable
    continuation directive stays ``active_pr`` guarded (duplicate-PR
    protection is untouched)."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="initial work", assignee="programmer")
        kb.add_comment(conn, task_id, "worker", f"PR created: {PR_URL}")
        assert kb.check_respawn_guard(conn, task_id) == "active_pr"


# ---------------------------------------------------------------------------
# Invariant 2 — same-card correction recovery after a crash
# ---------------------------------------------------------------------------

def test_correction_recovery_after_crash_is_not_pr_guarded(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="coding card", assignee="programmer")
        assert kb.claim_task(conn, task_id, claimer=f"{host}:impl") is not None
        kb.add_comment(conn, task_id, "programmer", f"PR opened: {PR_URL}")
        assert kb.submit_task_for_review(conn, task_id, "code-reviewer") is not None
        assert kb.claim_review_task(conn, task_id, claimer=f"{host}:review") is not None
        corrected = kb.request_changes(
            conn, task_id, "programmer",
            reason="tighten error handling", claimer=f"{host}:impl2",
        )
        assert corrected is not None and corrected.status == "running"

        # The correction run crashes: pid not alive -> back to ready.
        _crash_current_worker(conn, task_id)
        assert task_id in kb.detect_crashed_workers(conn)
        assert kb.get_task(conn, task_id).status == "ready"

        # The PR URL must NOT suppress the correction respawn.
        assert kb.check_respawn_guard(conn, task_id) is None

        spawned = []
        result = kb.dispatch_once(
            conn, spawn_fn=lambda task, workspace: spawned.append(
                (task.id, task.assignee)
            ),
        )
        assert (task_id, "active_pr") not in result.respawn_guarded
        assert spawned == [(task_id, "programmer")]


# ---------------------------------------------------------------------------
# Invariant 3 — documented review-required dependency handoff
# ---------------------------------------------------------------------------

def test_review_required_handoff_reconciles_into_review_lane(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    monkeypatch.setattr(
        kb, "_reviewer_candidates", lambda current: (["code-reviewer"], []),
    )
    host = kb._claimer_id().split(":", 1)[0]
    spawned = []

    def spawn(task, workspace):
        spawned.append((task.id, task.assignee, tuple(task.skills or ())))
        return 4242

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="coding card", assignee="programmer")
        assert kb.claim_task(conn, task_id, claimer=f"{host}:impl") is not None
        kb.add_comment(conn, task_id, "programmer", f"PR opened: {PR_URL}")
        # Documented handoff contract.
        assert kb.block_task(
            conn, task_id, kind="dependency",
            reason="review-required: PR ready, needs independent review",
        ) is True

        result = kb.dispatch_once(conn, spawn_fn=spawn)

        task = kb.get_task(conn, task_id)
        assert task is not None
        # Must not sit in generic ready waiting for the programmer.
        assert task.status == "review", f"status={task.status!r}"
        assert task.assignee == "code-reviewer"
        assert (task_id, "active_pr") not in result.respawn_guarded
        assert spawned == [(task_id, "code-reviewer", ("sdlc-review",))]
        # Same card: no child, no duplicate.
        assert len(kb.list_tasks(conn)) == 1


def test_review_required_handoff_reconciled_from_ready_on_next_tick(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """Even when the card is already parked in generic ``ready`` (the exact
    production state), the next tick reconciles it into the review lane."""
    monkeypatch.setattr(
        kb, "_reviewer_candidates", lambda current: (["code-reviewer"], []),
    )
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="coding card", assignee="programmer")
        assert kb.claim_task(conn, task_id, claimer=f"{host}:impl") is not None
        kb.add_comment(conn, task_id, "programmer", f"PR opened: {PR_URL}")
        assert kb.block_task(
            conn, task_id, kind="dependency",
            reason="review-required: needs sign-off",
        ) is True
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        conn.commit()

        kb.dispatch_once(conn, spawn_fn=lambda task, workspace: 99)
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "review"
        assert task.assignee == "code-reviewer"
        assert len(kb.list_tasks(conn)) == 1


def test_completed_review_cycle_does_not_re_enter_review_lane(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """Once the handoff has been consumed (card already went to review and
    came back as a correction), a later ready state is a programmer lane —
    it must not be re-routed to the reviewer."""
    monkeypatch.setattr(
        kb, "_reviewer_candidates", lambda current: (["code-reviewer"], []),
    )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="coding card", assignee="programmer")
        assert kb.claim_task(conn, task_id, claimer=f"{host}:impl") is not None
        assert kb.block_task(
            conn, task_id, kind="dependency",
            reason="review-required: needs sign-off",
        ) is True
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        conn.commit()
        kb.dispatch_once(conn, spawn_fn=lambda task, workspace: 99)
        assert kb.get_task(conn, task_id).status == "review"

        # The dispatcher already claimed + spawned the reviewer above.
        # Reviewer requests changes -> programmer correction -> crash.
        assert kb.request_changes(
            conn, task_id, "programmer", reason="fix tests", claimer=f"{host}:impl2",
        ) is not None
        _crash_current_worker(conn, task_id)
        assert task_id in kb.detect_crashed_workers(conn)

        spawned = []
        kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(
                (task.id, task.assignee)
            ),
        )
        task = kb.get_task(conn, task_id)
        assert task is not None and task.assignee == "programmer"
        assert spawned == [(task_id, "programmer")]

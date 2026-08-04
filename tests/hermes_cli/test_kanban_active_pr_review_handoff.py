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
    run = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    run_id = run["current_run_id"] if run else None
    started_at = int(time.time()) - 600
    conn.execute(
        "UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?",
        (pid, started_at, task_id),
    )
    if run_id is not None:
        conn.execute(
            "UPDATE task_runs SET started_at=? WHERE id=?",
            (started_at, run_id),
        )
    conn.commit()


def _spawn_recorder(sink, pid=None):
    def _spawn(task, workspace):
        sink.append((task.id, task.assignee))
        return pid
    return _spawn


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
        assert kb.submit_task_for_review(
            conn, task_id, "code-reviewer", trusted_operator=True,
        ) is not None
        assert kb.claim_review_task(conn, task_id, claimer=f"{host}:review") is not None
        corrected = kb.request_changes(
            conn, task_id, "programmer",
            reason="tighten error handling", trusted_operator=True,
        )
        assert corrected is not None and corrected.status == "ready"
        assert corrected.claim_lock is None
        assert corrected.current_run_id is None

        spawned = []
        kb.dispatch_once(conn, spawn_fn=_spawn_recorder(spawned, pid=4242))
        assert spawned == [(task_id, "programmer")]

        # The correction run crashes: pid not alive -> back to ready.
        _crash_current_worker(conn, task_id, pid=4242)
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
            conn, task_id, "programmer", reason="fix tests",
            trusted_operator=True,
        ) is not None
        spawned = []
        kb.dispatch_once(conn, spawn_fn=_spawn_recorder(spawned, pid=4243))
        assert spawned == [(task_id, "programmer")]
        _crash_current_worker(conn, task_id, pid=4243)
        assert task_id in kb.detect_crashed_workers(conn)

        spawned.clear()
        kb.dispatch_once(conn, spawn_fn=_spawn_recorder(spawned))
        task = kb.get_task(conn, task_id)
        assert task is not None and task.assignee == "programmer"
        assert spawned == [(task_id, "programmer")]


# ---------------------------------------------------------------------------
# Invariant 4 — the ready->review handoff must respect parent dependencies
# ---------------------------------------------------------------------------

def test_review_handoff_does_not_bypass_unfinished_parents(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """A ``review-required:`` child whose parent is still open must NOT be
    reconciled into the review lane.  ``claim_task`` enforces this invariant
    for ready->running; the ready->review path must enforce it too, otherwise
    the handoff is a hole straight through the dependency graph."""
    monkeypatch.setattr(
        kb, "_reviewer_candidates", lambda current: (["code-reviewer"], []),
    )
    host = kb._claimer_id().split(":", 1)[0]
    spawned = []
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent work", assignee="programmer")
        child_id = kb.create_task(conn, title="child work", assignee="programmer")
        assert kb.claim_task(conn, child_id, claimer=f"{host}:impl") is not None
        assert kb.block_task(
            conn, child_id, kind="dependency",
            reason="review-required: needs sign-off",
        ) is True
        kb.link_tasks(conn, parent_id, child_id)
        # Racy writer / recompute promoted the child to generic ready while
        # the parent is still open (the production shape).
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child_id,))
        conn.commit()
        assert kb.get_task(conn, parent_id).status not in ("done", "archived")

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(
                (task.id, task.assignee)
            ),
        )

        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "todo", f"status={child.status!r}"
        assert child.assignee == "programmer"
        assert child_id not in result.review_reconciled
        assert child_id not in [t for t, *_ in spawned]


# ---------------------------------------------------------------------------
# Invariant 5 — dry-run must agree with the real tick and mutate nothing
# ---------------------------------------------------------------------------

def test_dry_run_agrees_with_real_dispatch_for_review_handoff(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
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

        before_status = kb.get_task(conn, task_id).status
        before_assignee = kb.get_task(conn, task_id).assignee
        before_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (task_id,),
        ).fetchone()[0]

        dry = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: 99, dry_run=True)

        # Dry run must plan the same decision as the real tick.
        assert task_id in dry.review_reconciled
        assert (task_id, "code-reviewer", "") in dry.spawned, dry.spawned
        # ...and must not mutate anything durable.
        after = kb.get_task(conn, task_id)
        assert after.status == before_status
        assert after.assignee == before_assignee
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (task_id,),
        ).fetchone()[0] == before_events

        # The real tick agrees with the dry run.
        real_spawned = []
        real = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: real_spawned.append(
                (task.id, task.assignee, "")
            ),
        )
        assert real.review_reconciled == dry.review_reconciled
        assert real_spawned == [(task_id, "code-reviewer", "")]


def test_natural_todo_dry_run_is_strictly_read_only(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """A dependency handoff in its natural todo state is only predicted."""
    monkeypatch.setattr(
        kb, "_reviewer_candidates", lambda current: (["code-reviewer"], []),
    )
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="coding card", assignee="programmer")
        assert kb.claim_task(conn, task_id, claimer=f"{host}:impl") is not None
        assert kb.block_task(
            conn, task_id, kind="dependency",
            reason="review-required: needs sign-off",
        ) is True
        before = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
            for table in ("tasks", "task_runs", "task_events", "task_comments")
        }

        result = kb.dispatch_once(conn, dry_run=True)

        assert task_id in result.review_reconciled
        assert (task_id, "code-reviewer", "") in result.spawned
        for table, rows in before.items():
            assert [tuple(row) for row in conn.execute(
                f"SELECT * FROM {table}"
            ).fetchall()] == rows


def test_no_compatible_reviewer_is_fail_closed_in_dry_and_real_dispatch(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    monkeypatch.setattr(kb, "_reviewer_candidates", lambda current: ([], []))
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

        dry = kb.dispatch_once(conn, dry_run=True)
        assert dry.review_reconciled == []
        assert dry.spawned == []
        assert kb.get_task(conn, task_id).status == "ready"

        spawned = []
        real = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        assert real.review_reconciled == []
        assert real.spawned == []
        assert spawned == []
        assert kb.get_task(conn, task_id).status == "ready"


def test_review_reconciliation_cas_does_not_touch_a_racing_claim(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    monkeypatch.setattr(
        kb, "_reviewer_candidates", lambda current: (["code-reviewer"], []),
    )
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

        original_submit = kb.submit_task_for_review
        raced = {}

        def race_then_submit(connection, task, reviewer, **kwargs):
            raced["run"] = kb.claim_task(connection, task, claimer=f"{host}:racer")
            return original_submit(connection, task, reviewer, **kwargs)

        monkeypatch.setattr(kb, "submit_task_for_review", race_then_submit)
        assert kb.reconcile_review_handoffs(conn) == []
        current = kb.get_task(conn, task_id)
        assert current.status == "running"
        assert current.assignee == "programmer"
        assert raced["run"] is not None
        run = conn.execute(
            "SELECT status, ended_at FROM task_runs WHERE id=?",
            (raced["run"].current_run_id,),
        ).fetchone()
        assert run["status"] == "running"
        assert run["ended_at"] is None

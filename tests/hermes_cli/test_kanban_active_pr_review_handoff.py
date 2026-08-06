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

import subprocess
import time
from types import SimpleNamespace
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

def test_review_required_block_routes_directly_and_is_idempotent(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """A worker handoff must enter review before generic dependency promotion.

    The reviewer list is supplied by the real profile-discovery path rather
    than a frozen assignee list. Re-delivering the same block after the
    reviewer claim exists must not close or demote that live review run.
    """
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles,
        "list_profiles",
        lambda: [
            SimpleNamespace(name="code-reviewer-a"),
            SimpleNamespace(name="code-reviewer-b"),
        ],
    )
    host = kb._claimer_id().split(":", 1)[0]
    spawned = []

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="coding card", assignee="programmer")
        assert kb.claim_task(conn, task_id, claimer=f"{host}:impl") is not None
        implementation = kb.get_task(conn, task_id)
        assert implementation is not None
        implementation_run_id = implementation.current_run_id

        assert kb.block_task(
            conn,
            task_id,
            kind="dependency",
            reason="review-required: implementation is ready",
            expected_run_id=implementation_run_id,
            expected_assignee=implementation.assignee,
            expected_claim=implementation.claim_lock,
        ) is True

        review = kb.get_task(conn, task_id)
        assert review is not None
        assert review.status == "review"
        assert review.assignee == "code-reviewer-a"
        assert review.claim_lock is None
        assert review.current_run_id is None
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        assert [row["kind"] for row in events][-2:] == [
            "dependency_wait",
            "submitted_for_review",
        ]
        implementation_run = conn.execute(
            "SELECT outcome, ended_at FROM task_runs WHERE id=?",
            (implementation_run_id,),
        ).fetchone()
        assert implementation_run["outcome"] == "review_submitted"
        assert implementation_run["ended_at"] is not None

        kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: (
                spawned.append((task.id, task.assignee)) or 4242
            ),
        )
        claimed_review = kb.get_task(conn, task_id)
        assert claimed_review is not None
        assert spawned == [(task_id, "code-reviewer-a")]
        live_review_run_id = claimed_review.current_run_id
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
        ).fetchone()[0]

        # A duplicate delivery cannot block the active reviewer or create a
        # second run.
        assert kb.block_task(
            conn,
            task_id,
            kind="dependency",
            reason="review-required: implementation is ready",
        ) is True
        unchanged = kb.get_task(conn, task_id)
        assert unchanged is not None
        assert unchanged.status == "review"
        assert unchanged.assignee == "code-reviewer-a"
        assert unchanged.claim_lock is not None
        assert unchanged.current_run_id == live_review_run_id
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (task_id,)
        ).fetchone()[0] == event_count
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
        ).fetchone()[0] == run_count

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
    host = kb._claimer_id().split(":", 1)[0]
    reviewer_process = subprocess.Popen(["sleep", "30"])
    processes = [reviewer_process]

    def record_reaped(process):
        returncode = process.wait(timeout=5)
        raw_status = -returncode if returncode < 0 else returncode << 8
        kb._record_worker_exit(process.pid, raw_status)

    try:
        with kb.connect() as conn:
            task_id = kb.create_task(conn, title="coding card", assignee="programmer")
            assert kb.claim_task(conn, task_id, claimer=f"{host}:impl") is not None
            assert kb.block_task(
                conn, task_id, kind="dependency",
                reason="review-required: needs sign-off",
            ) is True
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
            conn.commit()

            def spawn_reviewer(task, workspace):
                assert task.assignee == "code-reviewer"
                return reviewer_process.pid

            kb.dispatch_once(conn, spawn_fn=spawn_reviewer)
            review = kb.get_task(conn, task_id)
            assert review is not None
            assert review.status == "review"
            assert review.worker_pid == reviewer_process.pid
            assert review.worker_boot_id
            assert review.worker_starttime and review.worker_starttime.isdigit()

            run = conn.execute(
                "SELECT worker_pid, worker_boot_id, worker_starttime "
                "FROM task_runs WHERE id=?",
                (review.current_run_id,),
            ).fetchone()
            assert run is not None
            assert (
                run["worker_pid"], run["worker_boot_id"], run["worker_starttime"]
            ) == (
                review.worker_pid, review.worker_boot_id, review.worker_starttime,
            )
            authority = kb._latest_reviewer_authority(conn, task_id)
            assert authority is not None
            with kb.write_txn(conn):
                conn.execute(
                    "INSERT INTO task_launch_gates "
                    "(task_id, run_id, claim_lock, assignee, authority_id, gate_token, "
                    "gate_pid, gate_boot_id, gate_starttime, state, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'released', ?)",
                    (
                        task_id, review.current_run_id, review.claim_lock,
                        review.assignee, authority[0],
                        f"review-cycle-{review.worker_pid}", review.worker_pid,
                        review.worker_boot_id, review.worker_starttime, int(time.time()),
                    ),
                )
            gate = conn.execute(
                "SELECT gate_pid, gate_boot_id, gate_starttime FROM task_launch_gates "
                "WHERE task_id=? AND run_id=?",
                (task_id, review.current_run_id),
            ).fetchone()
            assert gate is not None
            assert (
                gate["gate_pid"], gate["gate_boot_id"], gate["gate_starttime"]
            ) == (
                review.worker_pid, review.worker_boot_id, review.worker_starttime,
            )

            # Reviewer requests changes, but its process still owns the run.
            assert kb.request_changes(
                conn, task_id, "programmer", reason="fix tests",
                trusted_operator=True,
            ) is not None
            reviewer_process.terminate()
            record_reaped(reviewer_process)
            # Reconcile only after the real child has been terminated, reaped,
            # and its exact wait status has been recorded.
            kb._reap_pending_review_decisions(conn)
            reconciled = kb.get_task(conn, task_id)
            assert reconciled is not None
            assert reconciled.status == "ready"
            assert reconciled.assignee == "programmer"
            assert reconciled.current_run_id is None
            assert conn.execute(
                "SELECT state FROM task_launch_gates WHERE task_id=? "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()["state"] == "retired"

            spawned = []

            def spawn_programmer(task, workspace):
                process = subprocess.Popen(["sleep", "30"])
                processes.append(process)
                spawned.append((task.id, task.assignee))
                return process.pid

            # Only after retirement/reconciliation may the programmer
            # successor be spawned.
            kb.dispatch_once(conn, spawn_fn=spawn_programmer)
            programmer = kb.get_task(conn, task_id)
            assert programmer is not None
            assert programmer.current_run_id is not None
            assert spawned == [(task_id, "programmer")]
            programmer_process = processes[-1]
            programmer_process.terminate()
            record_reaped(programmer_process)
            old = int(time.time()) - 600
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET started_at=? WHERE id=?",
                    (old, task_id),
                )
                conn.execute(
                    "UPDATE task_runs SET started_at=? WHERE id=?",
                    (old, programmer.current_run_id),
                )
            assert task_id in kb.detect_crashed_workers(conn)

            spawned.clear()
            kb.dispatch_once(conn, spawn_fn=spawn_programmer)
            task = kb.get_task(conn, task_id)
            assert task is not None and task.assignee == "programmer"
            assert spawned == [(task_id, "programmer")]
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


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
        implementation = kb.get_task(conn, child_id)
        assert implementation is not None
        kb.link_tasks(conn, parent_id, child_id)
        assert kb.block_task(
            conn, child_id, kind="dependency",
            reason="review-required: needs sign-off",
            expected_run_id=implementation.current_run_id,
            expected_assignee=implementation.assignee,
            expected_claim=implementation.claim_lock,
        ) is True
        # A parent edge can be added while the implementation worker is
        # already running; the direct handoff must re-check it before review.
        # Simulate a racy writer / recompute promoting the child to generic
        # ready while the parent is still open (the production shape).
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

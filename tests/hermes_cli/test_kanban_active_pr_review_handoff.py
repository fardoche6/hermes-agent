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
import hashlib
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
    """Kill the live claim the way production did: a dead worker PID.

    Backdates the ACTIVE RUN's ``started_at`` as well as the task's: the
    crash-detection launch grace window is measured per attempt, from the
    active run, so a run that started "now" is inside the grace window and is
    deliberately not reaped.
    """
    stamp = int(time.time()) - 600
    conn.execute(
        "UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?",
        (pid, stamp, task_id),
    )
    conn.execute(
        "UPDATE task_runs SET started_at=? "
        "WHERE id = (SELECT current_run_id FROM tasks WHERE id=?)",
        (stamp, task_id),
    )
    conn.commit()


def _spawn_recorder(sink, pid=None):
    """spawn_fn stub that records ``(task_id, assignee)`` and returns ``pid``."""
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
        assert kb.submit_task_for_review(conn, task_id, "code-reviewer") is not None
        review = kb.claim_review_task(conn, task_id, claimer=f"{host}:review")
        assert review is not None
        corrected = kb.request_changes(
            conn, task_id, "programmer", reason="tighten error handling",
            reviewer="code-reviewer", expected_claim=f"{host}:review",
            expected_run_id=review.current_run_id,
        )
        # The correction lands in the ready lane with no phantom claim.
        assert corrected is not None and corrected.status == "ready"
        assert corrected.claim_lock is None
        assert corrected.worker_pid is None

        # The PR URL must NOT suppress the correction spawn.
        assert kb.check_respawn_guard(conn, task_id) is None
        spawned = []
        kb.dispatch_once(conn, spawn_fn=_spawn_recorder(spawned, pid=4242))
        assert spawned == [(task_id, "programmer")]

        # The correction run crashes: pid not alive -> back to ready.
        _crash_current_worker(conn, task_id, pid=4242)
        assert task_id in kb.detect_crashed_workers(conn)
        assert kb.get_task(conn, task_id).status == "ready"

        # ...and the PR URL must not suppress the correction RESPAWN either.
        assert kb.check_respawn_guard(conn, task_id) is None

        spawned.clear()
        result = kb.dispatch_once(conn, spawn_fn=_spawn_recorder(spawned))
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

        review = kb.get_task(conn, task_id)
        assert review is not None
        assert kb.request_changes(
            conn, task_id, "programmer", reason="fix tests",
            reviewer="code-reviewer", expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
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


def test_fresh_board_dry_run_has_no_physical_persistence_changes(
    kanban_home, all_assignees_spawnable,
):
    """Dry-run must not create locks or alter SQLite sidecar bytes."""
    with kb.connect() as conn:
        db_path = kb.kanban_db_path()
        paths = [
            db_path,
            Path(f"{db_path}-wal"),
            Path(f"{db_path}-shm"),
            db_path.with_name(db_path.name + ".dispatch.lock"),
        ]

        def snapshot():
            out = {}
            for path in paths:
                if path.exists():
                    out[path] = (True, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
                else:
                    out[path] = (False, None, None)
            return out

        before = snapshot()
        kb.dispatch_once(conn, dry_run=True)
        after = snapshot()

        assert after == before


def test_dry_run_does_not_leak_stale_recovery_side_channels(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    monkeypatch.setattr(kb.detect_crashed_workers, "_last_auto_blocked", ["old-block"], raising=False)
    monkeypatch.setattr(kb.detect_crashed_workers, "_last_rate_limited", ["old-rate"], raising=False)
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, dry_run=True)

    assert result.auto_blocked == []
    assert result.rate_limited == []


# ---------------------------------------------------------------------------
# Invariant 6 — a reviewer that APPROVES has a valid terminal transition
#
# Production failure (run 728): a dispatcher-spawned reviewer verified the
# work and posted an exact-head APPROVE, but had nowhere to put it —
# ``kanban_unblock`` is orchestrator-only, ``kanban_complete`` would claim a
# merge that had not happened, ``kanban_block`` is not a routine approval. The
# run exited clean, the dispatcher recorded ``protocol_violation``, the card
# cycled back through ``ready`` and two redundant reviewers were spawned.
# ---------------------------------------------------------------------------

HEAD_SHA = "93ac74f13911542446b63ffc526736fed754ae6a"


def _approved_card(conn, *, host, comment_pr=True):
    """Build a card sitting in the reviewer lane with a claimed review run."""
    task_id = kb.create_task(conn, title="coding card", assignee="programmer")
    assert kb.claim_task(conn, task_id, claimer=f"{host}:impl") is not None
    if comment_pr:
        kb.add_comment(conn, task_id, "programmer", f"PR opened: {PR_URL}")
    assert kb.submit_task_for_review(conn, task_id, "code-reviewer") is not None
    assert kb.claim_review_task(conn, task_id, claimer=f"{host}:review") is not None
    return task_id


def _events(conn, task_id):
    return [
        r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task_id,),
        )
    ]


def _event_payload(conn, task_id, kind):
    import json as _json
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? "
        "ORDER BY id DESC LIMIT 1", (task_id, kind),
    ).fetchone()
    assert row is not None, f"no {kind} event on {task_id}"
    return _json.loads(row["payload"]) if row["payload"] else {}


def test_approve_records_durable_exact_head_evidence(kanban_home):
    """Approval is machine-readable: reviewer + head sha + summary land in a
    ``review_approved`` event, and the reviewer's run closes successfully."""
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        reviewer_run_id = kb.get_task(conn, task_id).current_run_id

        task = kb.approve_review(
            conn, task_id,
            reviewer="code-reviewer",
            summary="67/67 focused tests, CI green",
            head_sha=HEAD_SHA,
            expected_run_id=kb.get_task(conn, task_id).current_run_id,
            expected_claim=f"{host}:review",
        )

        assert task is not None
        payload = _event_payload(conn, task_id, "review_approved")
        assert payload["head_sha"] == HEAD_SHA
        assert payload["reviewer"] == "code-reviewer"
        assert payload["summary"] == "67/67 focused tests, CI green"

        run = conn.execute(
            "SELECT outcome, summary, ended_at FROM task_runs WHERE id=?",
            (reviewer_run_id,),
        ).fetchone()
        assert run["outcome"] == "approved"
        assert run["summary"] == "67/67 focused tests, CI green"
        assert run["ended_at"] is not None


def test_approve_requires_full_exact_head(kanban_home):
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        with pytest.raises(ValueError, match="full 40- or 64-character"):
            kb.approve_review(
                conn, task_id, reviewer="code-reviewer", summary="approved",
                head_sha="deadbeef",
                expected_run_id=kb.get_task(conn, task_id).current_run_id,
            )
        assert kb.get_task(conn, task_id).status == "review"


def test_programmer_cannot_self_approve_reviewer_lane(kanban_home):
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        with pytest.raises(RuntimeError, match="does not own the current reviewer lane"):
            kb.approve_review(
                conn, task_id, reviewer="programmer", summary="approved",
                head_sha=HEAD_SHA, expected_run_id=kb.get_task(conn, task_id).current_run_id,
                expected_claim=f"{host}:review",
            )
        assert kb.get_task(conn, task_id).status == "review"


def test_stale_reviewer_claim_cannot_close_replacement_run(kanban_home):
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        with pytest.raises(RuntimeError, match="no longer holds"):
            kb.approve_review(
                conn, task_id, reviewer="code-reviewer", summary="approved",
                head_sha=HEAD_SHA, expected_run_id=kb.get_task(conn, task_id).current_run_id,
                expected_claim=f"{host}:stale-review",
            )
        assert kb.get_task(conn, task_id).status == "review"


def test_same_profile_reclaim_rejects_stale_approval_and_accepts_replacement(
    kanban_home, all_assignees_spawnable,
):
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        first = kb.get_task(conn, task_id)
        assert first is not None
        old_run_id, reused_claim = first.current_run_id, first.claim_lock
        assert kb.failover_review_task(
            conn, task_id, "code-reviewer", error="first reviewer failed",
        ) is not None
        replacement = kb.claim_review_task(
            conn, task_id, claimer=reused_claim,
        )
        assert replacement is not None
        assert replacement.current_run_id != old_run_id
        assert replacement.claim_lock == reused_claim

        with pytest.raises(RuntimeError):
            kb.approve_review(
                conn, task_id, reviewer="code-reviewer",
                summary="stale approval", head_sha=HEAD_SHA,
                expected_claim=reused_claim, expected_run_id=old_run_id,
            )
        unchanged = kb.get_task(conn, task_id)
        assert unchanged.status == "review"
        assert unchanged.current_run_id == replacement.current_run_id
        assert conn.execute(
            "SELECT outcome FROM task_runs WHERE id=?",
            (replacement.current_run_id,),
        ).fetchone()["outcome"] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='review_approved'",
            (task_id,),
        ).fetchone()[0] == 0

        approved = kb.approve_review(
            conn, task_id, reviewer="code-reviewer", summary="current approval",
            head_sha=HEAD_SHA, expected_claim=reused_claim,
            expected_run_id=replacement.current_run_id,
        )
        assert approved.status == "ready"
        event = _event_payload(conn, task_id, "review_approved")
        assert event["reviewer"] == "code-reviewer"
        assert event["head_sha"] == HEAD_SHA
        assert approved.current_run_id is None
        assert kb.dispatch_once(
            conn, spawn_fn=_spawn_recorder([]),
        ) is not None
        assert kb.get_task(conn, task_id).status == "running"


def test_approve_hands_same_card_to_the_finalization_lane(kanban_home):
    """No child card and no phantom claim: the approved card is handed to the
    profile that submitted it for review, in the canonical ready-lane shape
    every other recovery path in the module produces."""
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)

        task = kb.approve_review(
            conn, task_id, reviewer="code-reviewer",
            summary="approved", head_sha=HEAD_SHA,
            expected_run_id=kb.get_task(conn, task_id).current_run_id,
        )

        assert task.status == "ready"
        assert task.assignee == "programmer"
        assert task.claim_lock is None
        assert task.claim_expires is None
        assert task.worker_pid is None
        assert task.current_run_id is None
        assert _event_payload(
            conn, task_id, "review_approved",
        )["finalizer"] == "programmer"
        # No fabricated run row: nothing is left open behind a claim that no
        # process holds.
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND ended_at IS NULL",
            (task_id,),
        ).fetchone()[0] == 0
        # ...and no synthetic ``claimed`` event either.
        kinds = _events(conn, task_id)
        assert "claimed" not in kinds[kinds.index("review_approved"):]
        assert kb.in_finalization_lane(conn, task_id) is True
        assert kb.in_correction_lane(conn, task_id) is False
        context = kb.build_worker_context(conn, task_id)
        assert "## Finalization directive" in context
        assert f"Approved exact head: `{HEAD_SHA}`" in context
        assert "call kanban_complete only after the merge is durably verified" in context
        # One canonical card.
        assert len(kb.list_tasks(conn)) == 1


def test_approve_is_not_completion(kanban_home):
    """Approval must never mark the card done or stamp a completion time."""
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        task = kb.approve_review(
            conn, task_id, reviewer="code-reviewer",
            summary="approved", head_sha=HEAD_SHA, expected_run_id=kb.get_task(conn, task_id).current_run_id,
                expected_claim=f"{host}:review",
        )
        assert task.status != "done"
        assert task.completed_at is None
        assert "completed" not in _events(conn, task_id)


def test_approve_does_not_fake_a_change_request(kanban_home):
    """The approve path must not borrow ``request_changes`` — the manual
    recovery workaround ('finalization-only / no code change') must not
    become the design."""
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        kb.approve_review(
            conn, task_id, reviewer="code-reviewer",
            summary="approved", head_sha=HEAD_SHA, expected_run_id=kb.get_task(conn, task_id).current_run_id,
                expected_claim=f"{host}:review",
        )
        assert "changes_requested" not in _events(conn, task_id)


def test_approve_clears_the_review_required_sticky_block(kanban_home):
    """The legacy handoff is an untyped sticky ``kanban_block``. Approving
    must resolve it without the orchestrator-only unblock tool — that dead
    end is exactly what stranded run 728."""
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="coding card", assignee="programmer")
        assert kb.claim_task(conn, task_id, claimer=f"{host}:impl") is not None
        assert kb.block_task(
            conn, task_id, reason="review-required: PR ready",
        ) is True
        assert kb._has_sticky_block(conn, task_id) is True
        assert kb.submit_task_for_review(conn, task_id, "code-reviewer") is not None
        assert kb.claim_review_task(
            conn, task_id, claimer=f"{host}:review",
        ) is not None

        kb.approve_review(
            conn, task_id, reviewer="code-reviewer",
            summary="approved", head_sha=HEAD_SHA, expected_run_id=kb.get_task(conn, task_id).current_run_id,
                expected_claim=f"{host}:review",
        )

        assert kb._has_sticky_block(conn, task_id) is False
        assert kb.get_task(conn, task_id).block_kind is None
        assert kb.get_task(conn, task_id).status == "ready"
        assert "unblocked" in _events(conn, task_id)


def test_request_changes_requires_live_owned_review_run(kanban_home):
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        task = kb.get_task(conn, task_id)
        assert task is not None
        run_id = task.current_run_id
        claim = task.claim_lock
        assert run_id is not None
        assert claim is not None
        for reviewer, expected_claim, expected_run_id, reason in [
            ("programmer", claim, run_id, "programmer cannot request"),
            ("code-reviewer", "wrong-claim", run_id, "wrong claim"),
            ("code-reviewer", claim, run_id + 1, "wrong run"),
            ("code-reviewer", claim, run_id, ""),
        ]:
            with pytest.raises((RuntimeError, ValueError)):
                kb.request_changes(
                    conn, task_id, "programmer", reason=reason,
                    reviewer=reviewer, expected_claim=expected_claim,
                    expected_run_id=expected_run_id,
                )
            current = kb.get_task(conn, task_id)
            assert current.status == "review"
            assert current.claim_lock == claim
            assert current.current_run_id == run_id
            assert conn.execute(
                "SELECT ended_at FROM task_runs WHERE id=?", (run_id,)
            ).fetchone()["ended_at"] is None


def test_request_changes_accepts_current_reviewer_after_real_failover(kanban_home):
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="failover card", assignee="programmer")
        assert kb.claim_task(conn, task_id, claimer=f"{host}:impl") is not None
        assert kb.submit_task_for_review(conn, task_id, "reviewer-a") is not None
        first = kb.claim_review_task(conn, task_id, claimer=f"{host}:review-a")
        assert first is not None
        old_claim, old_run = first.claim_lock, first.current_run_id

        assert kb.failover_review_task(
            conn, task_id, "reviewer-b", error="reviewer-a unavailable",
        ) is not None
        replacement = kb.claim_review_task(
            conn, task_id, claimer=f"{host}:review-b",
        )
        assert replacement is not None

        with pytest.raises(RuntimeError):
            kb.request_changes(
                conn, task_id, "programmer", reason="stale reviewer",
                reviewer="reviewer-a", expected_claim=old_claim,
                expected_run_id=old_run,
            )
        after = kb.get_task(conn, task_id)
        assert after is not None
        assert after.status == "review"
        assert after.assignee == "reviewer-b"
        assert after.claim_lock == replacement.claim_lock
        assert after.current_run_id == replacement.current_run_id
        assert conn.execute(
            "SELECT ended_at FROM task_runs WHERE id=?",
            (replacement.current_run_id,),
        ).fetchone()["ended_at"] is None

        corrected = kb.request_changes(
            conn, task_id, "programmer", reason="current reviewer feedback",
            reviewer="reviewer-b", expected_claim=replacement.claim_lock,
            expected_run_id=replacement.current_run_id,
        )
        assert corrected is not None
        assert corrected.status == "ready"
        assert corrected.assignee == "programmer"
        assert conn.execute(
            "SELECT outcome FROM task_runs WHERE id=?",
            (replacement.current_run_id,),
        ).fetchone()["outcome"] == "changes_requested"


def test_request_changes_rejects_unclaimed_card(kanban_home):
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        task = kb.get_task(conn, task_id)
        assert task is not None
        conn.execute("UPDATE tasks SET claim_lock=NULL WHERE id=?", (task_id,))
        conn.commit()
        with pytest.raises(RuntimeError):
            kb.request_changes(
                conn, task_id, "programmer", reason="fix", reviewer="code-reviewer",
                expected_claim=task.claim_lock, expected_run_id=task.current_run_id,
            )
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "review"


def test_approved_card_does_not_respawn_a_reviewer(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """No duplicate reviewer, no failover, no second review lane after
    approval — the only spawn is the finalizer."""
    monkeypatch.setattr(
        kb, "_reviewer_candidates", lambda current: (["code-reviewer"], []),
    )
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        kb.approve_review(
            conn, task_id, reviewer="code-reviewer",
            summary="approved", head_sha=HEAD_SHA,
            expected_run_id=kb.get_task(conn, task_id).current_run_id,
        )

        spawned = []
        result = kb.dispatch_once(conn, spawn_fn=_spawn_recorder(spawned))

        assert spawned == [(task_id, "programmer")]
        assert [(t, a) for t, a, *_ in result.spawned] == [
            (task_id, "programmer")
        ]
        assert task_id not in result.review_reconciled
        kinds = _events(conn, task_id)
        assert "review_failover" not in kinds
        assert "review_lanes_failed" not in kinds
        # Exactly one trip through the reviewer lane, ever.
        assert kinds.count("submitted_for_review") == 1
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_task(conn, task_id).assignee == "programmer"
        assert len(kb.list_tasks(conn)) == 1


def test_crashed_finalizer_resumes_the_same_pr(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """After approval, a crashed merge run must NOT be shoved back into the
    review lane, and the ``active_pr`` guard must not strand it."""
    monkeypatch.setattr(
        kb, "_reviewer_candidates", lambda current: (["code-reviewer"], []),
    )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        kb.approve_review(
            conn, task_id, reviewer="code-reviewer",
            summary="approved", head_sha=HEAD_SHA,
            expected_run_id=kb.get_task(conn, task_id).current_run_id,
        )

        # The dispatcher claims the approved card for the finalizer...
        spawned = []
        kb.dispatch_once(conn, spawn_fn=_spawn_recorder(spawned, pid=5150))
        assert spawned == [(task_id, "programmer")]
        assert kb.get_task(conn, task_id).status == "running"

        # ...and that merge run crashes.
        _crash_current_worker(conn, task_id, pid=5150)
        assert task_id in kb.detect_crashed_workers(conn)
        assert kb.get_task(conn, task_id).status == "ready"

        # A recent PR comment exists, but the card is durably in the
        # finalization lane — continuing, not duplicating.
        assert kb.check_respawn_guard(conn, task_id) is None
        assert kb.in_finalization_lane(conn, task_id) is True
        assert kb.pending_review_handoff(conn, task_id) is False

        spawned.clear()
        result = kb.dispatch_once(conn, spawn_fn=_spawn_recorder(spawned))

        assert (task_id, "active_pr") not in result.respawn_guarded
        assert spawned == [(task_id, "programmer")]
        assert task_id not in result.review_reconciled
        assert kb.get_task(conn, task_id).assignee == "programmer"


@pytest.mark.parametrize(
    "bad_status", ["ready", "todo", "done", "blocked", "running"],
)
def test_approve_from_invalid_status_is_rejected(kanban_home, bad_status):
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="coding card", assignee="programmer")
        conn.execute(
            "UPDATE tasks SET status=?, claim_lock=NULL WHERE id=?",
            (bad_status, task_id),
        )
        conn.commit()
        with pytest.raises(RuntimeError):
            kb.approve_review(
                conn, task_id, reviewer="code-reviewer",
                summary="approved",
            )
        assert kb.get_task(conn, task_id).status == bad_status


def test_approve_fails_closed_without_a_resolvable_finalizer(kanban_home):
    """No submitted_for_review provenance and no prior run: refuse rather
    than guessing who should land the merge."""
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="orphan card", assignee="code-reviewer")
        conn.execute(
            "UPDATE tasks SET status='review' WHERE id=?", (task_id,),
        )
        conn.commit()
        with pytest.raises(RuntimeError):
            kb.approve_review(
                conn, task_id, reviewer="code-reviewer",
                summary="approved", head_sha=HEAD_SHA,
            expected_run_id=kb.get_task(conn, task_id).current_run_id,
            )
        assert kb.get_task(conn, task_id).status == "review"

        # No caller can bypass the durable reviewer-lane provenance by choosing
        # an explicit finalizer; the new API has no such override.


def test_request_changes_still_returns_one_bundled_correction_packet(kanban_home):
    """Regression: the correction half of the review decision is still ONE
    bundled same-card packet — one event, one card, no child."""
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        review = kb.get_task(conn, task_id)
        assert review is not None
        task = kb.request_changes(
            conn, task_id, "programmer",
            reason="tighten error handling", reviewer="code-reviewer",
            expected_claim=review.claim_lock, expected_run_id=review.current_run_id,
        )
        assert task is not None
        # Ready lane, not a fabricated claim: identical shape to approval.
        assert task.status == "ready"
        assert task.assignee == "programmer"
        assert task.claim_lock is None
        assert task.worker_pid is None
        assert task.current_run_id is None
        assert task.completed_at is None
        payload = _event_payload(conn, task_id, "changes_requested")
        assert payload["reason"] == "tighten error handling"
        assert payload["programmer"] == "programmer"
        kinds = _events(conn, task_id)
        assert kinds.count("changes_requested") == 1
        assert "review_approved" not in kinds
        assert "claimed" not in kinds[kinds.index("changes_requested"):]
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND ended_at IS NULL",
            (task_id,),
        ).fetchone()[0] == 0
        # Same card, one packet, no child.
        assert len(kb.list_tasks(conn)) == 1
        assert kb.in_correction_lane(conn, task_id) is True
        assert kb.in_finalization_lane(conn, task_id) is False


# ---------------------------------------------------------------------------
# Invariant 7 — the review decision must not manufacture a phantom worker
#
# Both halves of the decision used to end with a NEW claim, a NEW task_runs
# row and a synthetic ``claimed`` event, while the DEAD reviewer's
# ``worker_pid`` stayed on the card. One tick later ``detect_crashed_workers``
# found that pid dead, saw it had exited cleanly (rc=0), and recorded the
# spurious ``protocol_violation`` this whole transition exists to eliminate —
# and the held claim made the card invisible to both dispatcher lanes.
# ---------------------------------------------------------------------------

def _reviewer_exits_clean(monkeypatch, pid: int) -> None:
    """Register a clean (rc=0) exit for ``pid`` in the reap registry."""
    monkeypatch.setitem(kb._recent_worker_exits, pid, (0, time.time()))


def test_approval_leaves_no_dead_pid_for_the_crash_reaper(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """Core regression: the next tick must record NO ``protocol_violation``
    and must not touch the approved card."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    host = kb._claimer_id().split(":", 1)[0]
    reviewer_pid = 555001
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        # The dispatcher stamped the reviewer's pid on the card...
        kb._set_worker_pid(conn, task_id, reviewer_pid)
        # ...and the reviewer process approves, then exits cleanly (rc=0).
        _reviewer_exits_clean(monkeypatch, reviewer_pid)

        task = kb.approve_review(
            conn, task_id, reviewer="code-reviewer",
            summary="approved", head_sha=HEAD_SHA,
            expected_run_id=kb.get_task(conn, task_id).current_run_id,
        )
        assert task.worker_pid is None
        assert task.claim_lock is None

        before = _events(conn, task_id)
        assert kb.detect_crashed_workers(conn) == []
        after = _events(conn, task_id)

        assert after == before, "crash reaper must not touch an approved card"
        assert "protocol_violation" not in after
        assert "crashed" not in after
        current = kb.get_task(conn, task_id)
        assert current.status == "ready"
        assert current.assignee == "programmer"
        assert current.last_failure_error is None
        assert current.consecutive_failures == 0


def test_request_changes_leaves_no_dead_pid_for_the_crash_reaper(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """The correction half had the identical defect."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    host = kb._claimer_id().split(":", 1)[0]
    reviewer_pid = 555002
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        kb._set_worker_pid(conn, task_id, reviewer_pid)
        _reviewer_exits_clean(monkeypatch, reviewer_pid)

        review = kb.get_task(conn, task_id)
        assert review is not None
        task = kb.request_changes(
            conn, task_id, "programmer", reason="fix it", reviewer="code-reviewer",
            expected_claim=review.claim_lock, expected_run_id=review.current_run_id,
        )
        assert task.worker_pid is None
        assert task.claim_lock is None

        before = _events(conn, task_id)
        assert kb.detect_crashed_workers(conn) == []
        assert _events(conn, task_id) == before
        assert "protocol_violation" not in _events(conn, task_id)
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.get_task(conn, task_id).last_failure_error is None


def test_approved_card_is_claimable_by_a_fresh_finalizer(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """The approved card must be visible to the ready lane so the NEXT tick
    establishes a real run behind a real pid."""
    monkeypatch.setattr(
        kb, "_reviewer_candidates", lambda current: (["code-reviewer"], []),
    )
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        kb._set_worker_pid(conn, task_id, 555003)
        reviewer_run_id = kb.get_task(conn, task_id).current_run_id
        kb.approve_review(
            conn, task_id, reviewer="code-reviewer",
            summary="approved", head_sha=HEAD_SHA,
            expected_run_id=kb.get_task(conn, task_id).current_run_id,
        )

        spawned = []
        result = kb.dispatch_once(
            conn, spawn_fn=_spawn_recorder(spawned, pid=777001),
        )

        assert spawned == [(task_id, "programmer")]
        assert [(t, a) for t, a, *_ in result.spawned] == [
            (task_id, "programmer")
        ]
        task = kb.get_task(conn, task_id)
        assert task.status == "running"
        assert task.assignee == "programmer"
        assert task.worker_pid == 777001
        assert task.claim_lock is not None
        # A genuinely NEW run, not the reviewer's.
        assert task.current_run_id is not None
        assert task.current_run_id != reviewer_run_id
        run = conn.execute(
            "SELECT profile, status, ended_at, worker_pid FROM task_runs WHERE id=?",
            (task.current_run_id,),
        ).fetchone()
        assert run["profile"] == "programmer"
        assert run["status"] == "running"
        assert run["ended_at"] is None
        assert run["worker_pid"] == 777001


def test_changes_requested_after_approval_closes_the_finalization_lane(
    kanban_home,
):
    """The two review lanes are mutually exclusive. A ``changes_requested``
    that lands after a ``review_approved`` moves the card OUT of finalization
    and back into correction — otherwise the card looks like it is being
    merged and corrected at the same time."""
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        kb.approve_review(
            conn, task_id, reviewer="code-reviewer",
            summary="approved", head_sha=HEAD_SHA,
            expected_run_id=kb.get_task(conn, task_id).current_run_id,
        )
        assert kb.in_finalization_lane(conn, task_id) is True
        assert kb.in_correction_lane(conn, task_id) is False

        # A finalizer cannot emit a reviewer-only change request.
        assert kb.claim_task(conn, task_id, claimer=f"{host}:merge") is not None
        finalizer = kb.get_task(conn, task_id)
        assert finalizer is not None
        with pytest.raises(RuntimeError):
            kb.request_changes(
                conn, task_id, "programmer", reason="merge broke main",
                reviewer="code-reviewer", expected_claim=finalizer.claim_lock,
                expected_run_id=finalizer.current_run_id,
            )
        assert kb.get_task(conn, task_id) is not None
        assert kb.get_task(conn, task_id).status == "running"


def test_approval_after_changes_requested_closes_the_correction_lane(
    kanban_home,
):
    """The mirror ordering: an approval settles a prior correction lane."""
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = _approved_card(conn, host=host)
        review = kb.get_task(conn, task_id)
        assert review is not None
        assert kb.request_changes(
            conn, task_id, "programmer", reason="fix tests",
            reviewer="code-reviewer", expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        ) is not None
        assert kb.in_correction_lane(conn, task_id) is True

        # The correction run lands, goes back through the reviewer lane, and
        # THAT reviewer approves. The programmer never approves its own
        # running card — approval is only reachable from ``status='review'``.
        assert kb.claim_task(conn, task_id, claimer=f"{host}:impl2") is not None
        assert kb.submit_task_for_review(conn, task_id, "code-reviewer") is not None
        assert kb.claim_review_task(
            conn, task_id, claimer=f"{host}:review2",
        ) is not None
        kb.approve_review(
            conn, task_id, reviewer="code-reviewer",
            summary="approved", head_sha=HEAD_SHA,
            expected_run_id=kb.get_task(conn, task_id).current_run_id,
            expected_claim=f"{host}:review2",
        )

        assert kb.in_finalization_lane(conn, task_id) is True
        assert kb.in_correction_lane(conn, task_id) is False

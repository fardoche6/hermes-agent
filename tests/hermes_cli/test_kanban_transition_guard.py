"""Regression tests for the false-done transition guard (t_bb4b3f89).

Scope: ``complete_task`` must reject a task that is currently
``blocked`` — ``unblock_task`` is the only legitimate way out of
``blocked``, and completion must not be usable as a side door around
it. This file only pins down the ``complete_task`` guard; the
``recompute_ready`` / ``promote_task`` "blocked" handling is
intentionally left untouched here (see task report — both already
have dedicated, ticket-linked, test-covered semantics that conflict
with narrowing them further).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def _blocked_task(conn):
    tid = kb.create_task(conn, title="needs review", assignee="worker")
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    assert kb.block_task(
        conn, tid, reason="review-required: please verify", expected_run_id=run_id
    )
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


def _events(conn, task_id):
    rows = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    return [r["kind"] for r in rows]


def test_complete_task_rejects_blocked_task(conn):
    tid = _blocked_task(conn)

    ok = kb.complete_task(conn, tid, result="did the thing")

    assert ok is False
    assert kb.get_task(conn, tid).status == "blocked"
    events = _events(conn, tid)
    assert "completed" not in events
    assert "completion_rejected_transition" in events


def test_complete_task_rejection_does_not_touch_done_bookkeeping(conn):
    tid = _blocked_task(conn)
    kb.complete_task(conn, tid, result="should be rejected")
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.completed_at is None
    assert task.result is None


def test_recompute_ready_never_promotes_blocked_on_its_own(conn):
    tid = _blocked_task(conn)

    for _ in range(3):
        promoted = kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"

    # The one legitimate exit: explicit unblock_task().
    assert kb.unblock_task(conn, tid)
    task = kb.get_task(conn, tid)
    assert task.status in ("ready", "todo")
    events = _events(conn, tid)
    assert "unblocked" in events


def test_promote_task_rejects_blocked_task(conn):
    tid = _blocked_task(conn)

    ok, reason = kb.promote_task(conn, tid, actor="operator")

    # promote_task already requires an explicit, audited operator call
    # (actor + reason + a 'promoted_manual' event) to move a blocked
    # task to ready, so it independently satisfies "blocked -> ready
    # must be explicit". This test pins the CURRENT (unchanged)
    # behavior rather than a narrowed one -- see task report.
    assert ok is True
    assert kb.get_task(conn, tid).status == "ready"


def test_promote_task_still_works_for_todo_task(conn):
    parent = kb.create_task(conn, title="parent", assignee="worker")
    tid = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
    assert kb.get_task(conn, tid).status == "todo"

    # Parent isn't done yet, so this exercises the "explicit operator
    # override" path (force=True) rather than the normal auto-eligible
    # path -- either way it proves promote_task still accepts a 'todo'
    # task and was not accidentally broken by the guard work above.
    ok, reason = kb.promote_task(conn, tid, actor="operator", force=True)

    assert ok is True
    assert reason is None
    assert kb.get_task(conn, tid).status == "ready"


def test_complete_task_running_to_done_still_succeeds(conn):
    tid = kb.create_task(conn, title="normal task", assignee="worker")
    kb.claim_task(conn, tid)
    assert kb.get_task(conn, tid).status == "running"

    ok = kb.complete_task(conn, tid, result="all good")

    assert ok is True
    task = kb.get_task(conn, tid)
    assert task.status == "done"
    assert task.result == "all good"
    assert "completed" in _events(conn, tid)

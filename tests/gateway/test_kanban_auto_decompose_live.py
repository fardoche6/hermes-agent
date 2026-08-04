"""Tests for live auto-decompose settings resolution (issue #49638).

The gateway dispatcher used to capture ``kanban.auto_decompose`` once at boot,
so a user who flipped it to ``false`` to STOP runaway auto-decompose (which had
created and launched tasks they didn't intend) found the flag had no effect
without a full gateway restart. ``_resolve_auto_decompose_settings`` is now
called every tick, reading the current config.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.kanban_watchers import (
    _auto_decompose_triage_tasks,
    _resolve_auto_decompose_settings,
)
from hermes_cli import kanban_db as kb


def test_disabled_by_default_when_key_absent():
    enabled, per_tick = _resolve_auto_decompose_settings(lambda: {"kanban": {}})
    assert enabled is False
    assert per_tick == 3


def test_disabled_when_flag_false():
    enabled, per_tick = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose": False}}
    )
    assert enabled is False


def test_enabled_only_when_flag_explicitly_true():
    enabled, per_tick = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose": True}}
    )
    assert enabled is True
    assert per_tick == 3


@pytest.mark.parametrize(
    "config",
    [
        {"kanban": {}},
        {"kanban": {"auto_decompose": False}},
    ],
    ids=["absent", "explicit-false"],
)
def test_block_loop_stays_on_canonical_card_when_auto_decompose_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: dict,
):
    """A recurrence-limit triage card is not authorization for graph surgery."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    enabled, _ = _resolve_auto_decompose_settings(lambda: config)
    assert enabled is False

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="canonical recovery", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        assert kb.claim_task(conn, task_id, claimer="worker") is not None
        assert kb.block_task(conn, task_id, reason="review-required", kind="capability")
        assert kb.unblock_task(conn, task_id)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        assert kb.claim_task(conn, task_id, claimer="worker") is not None
        assert kb.block_task(conn, task_id, reason="review-required", kind="capability")
        triage_task = kb.get_task(conn, task_id)
        assert triage_task is not None and triage_task.status == "triage"

        before_tasks = conn.execute(
            "SELECT id, status, assignee FROM tasks ORDER BY id"
        ).fetchall()
        before_links = conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
        ).fetchall()
        before_events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()

        assert _auto_decompose_triage_tasks(kb, 3, enabled=enabled) == 0
        spawned: list[str] = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )

        after_tasks = conn.execute(
            "SELECT id, status, assignee FROM tasks ORDER BY id"
        ).fetchall()
        after_links = conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
        ).fetchall()
        after_events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()

        assert spawned == []
        assert getattr(result, "spawned", []) == []
        assert after_tasks == before_tasks
        assert after_links == before_links
        assert after_events == before_events



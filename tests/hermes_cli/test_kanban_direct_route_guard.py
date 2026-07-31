"""Tests for the kanban direct route admission guard (kanban_direct_route_guard.py).

Validates that:
1. default-board/kanban Hermes repair assigned to programmer* is rejected with zero task/event/workspace mutation;
2. same scope assigned to code-reviewer* is rejected;
3. SubsidySmart product card is allowed;
4. non-repair default-board research is allowed;
5. direct external workers are unaffected;
6. board-aware task identity is preserved.
"""

from __future__ import annotations

from pathlib import Path
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_direct_route_guard import (
    is_direct_route_repair,
    guard_direct_route_repair,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_1_programmer_hermes_repair_rejected_zero_mutation(kanban_home):
    """1. default-board/kanban Hermes repair assigned to programmer* is rejected with zero task/event/workspace mutation."""
    conn = kb.connect(board="default")

    # Initial state assertions
    initial_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    initial_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    assert initial_tasks == 0
    assert initial_events == 0

    with pytest.raises(ValueError, match="hermes/kanban repair work must run directly"):
        kb.create_task(
            conn,
            title="fix kanban transition issue",
            assignee="programmer-gemini",
            board="default",
        )

    # Zero mutation verification
    tasks_after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    events_after = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    assert tasks_after == 0
    assert events_after == 0


def test_2_code_reviewer_hermes_repair_rejected(kanban_home):
    """2. same scope assigned to code-reviewer* is rejected."""
    conn = kb.connect(board="default")

    with pytest.raises(ValueError, match="hermes/kanban repair work must run directly"):
        kb.create_task(
            conn,
            title="review kanban repair fix",
            assignee="code-reviewer-claude",
            board="default",
            tenant="hermes",
        )

    tasks_after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert tasks_after == 0


def test_3_subsidysmart_product_card_allowed(kanban_home):
    """3. SubsidySmart product card is allowed."""
    # 3a: Dedicated board
    kb.init_db(board="subsidysmart")
    conn_ss = kb.connect(board="subsidysmart")
    tid1 = kb.create_task(
        conn_ss,
        title="Add SubsidySmart feature",
        assignee="programmer-gemini",
        board="subsidysmart",
    )
    assert tid1 is not None
    assert tid1.startswith("t_")

    # 3b: Default board with subsidysmart tenant
    conn_def = kb.connect(board="default")
    tid2 = kb.create_task(
        conn_def,
        title="Fix SubsidySmart UI bug",
        assignee="programmer-gemini",
        board="default",
        tenant="subsidysmart",
    )
    assert tid2 is not None
    assert tid2.startswith("t_")

    # 3c: Default board with subsidysmart project_id
    tid3 = kb.create_task(
        conn_def,
        title="Update SubsidySmart schema",
        assignee="programmer-gemini",
        board="default",
        project_id="subsidysmart",
    )
    assert tid3 is not None
    assert tid3.startswith("t_")

    # 3d: Default board with subsidysmart workspace_path
    tid4 = kb.create_task(
        conn_def,
        title="Refactor SubsidySmart models",
        assignee="programmer-gemini",
        board="default",
        workspace_path="/home/fardochebot/projects/subsidysmart",
    )
    assert tid4 is not None
    assert tid4.startswith("t_")


def test_4_non_repair_default_board_research_allowed(kanban_home):
    """4. non-repair default-board research is allowed."""
    conn = kb.connect(board="default")
    tid = kb.create_task(
        conn,
        title="Survey AI agent architectures",
        assignee="researcher-a",
        board="default",
    )
    assert tid is not None
    assert tid.startswith("t_")

    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "researcher-a"


def test_5_direct_external_workers_unaffected(kanban_home):
    """5. direct external workers are unaffected."""
    # Predicate directly checks that direct-route repairs return True, while external/research work returns False
    assert is_direct_route_repair(board="default", assignee="programmer-gemini") is True
    assert is_direct_route_repair(board="default", assignee="code-reviewer-claude") is True
    assert is_direct_route_repair(board="subsidysmart", assignee="programmer-gemini") is False
    assert is_direct_route_repair(board="default", assignee="programmer-gemini", tenant="subsidysmart") is False
    assert is_direct_route_repair(board="default", assignee="researcher-a") is False

    # Guard call directly passes without raising when target is valid non-repair work
    guard_direct_route_repair(board="default", assignee="researcher-a")
    guard_direct_route_repair(board="subsidysmart", assignee="programmer-gemini")
    guard_direct_route_repair(board="default", assignee="programmer-gemini", tenant="subsidysmart")


def test_6_board_aware_task_identity_preserved(kanban_home):
    """6. board-aware task identity is preserved."""
    kb.init_db(board="subsidysmart")
    conn_ss = kb.connect(board="subsidysmart")
    conn_def = kb.connect(board="default")

    tid_ss = kb.create_task(
        conn_ss,
        title="SubsidySmart Task",
        assignee="programmer-gemini",
        board="subsidysmart",
    )
    tid_def = kb.create_task(
        conn_def,
        title="Research Task",
        assignee="researcher-a",
        board="default",
    )

    task_ss = kb.get_task(conn_ss, tid_ss)
    task_def = kb.get_task(conn_def, tid_def)

    assert task_ss is not None
    assert task_ss.id == tid_ss
    assert task_def is not None
    assert task_def.id == tid_def
    assert tid_ss != tid_def

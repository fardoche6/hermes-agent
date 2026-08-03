"""Tests for kanban goal_mode — per-card Ralph-style goal loop.

Covers three layers:

1. DB: goal_mode / goal_max_turns persist through create_task + from_row,
   and a legacy DB (without the columns) migrates cleanly.
2. Spawn: _default_spawn sets the HERMES_KANBAN_GOAL_MODE env vars only
   when the card opts in.
3. Loop: goals.run_kanban_goal_loop continuation / completion / budget
   behaviour, driven entirely through injected callbacks (no live model).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import goals


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

def test_goal_mode_defaults_off(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain task", assignee="worker")
        task = kb.get_task(conn, tid)
    assert task.goal_mode is False
    assert task.goal_max_turns is None


def test_goal_mode_persists(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="open-ended task",
            assignee="worker",
            goal_mode=True,
            goal_max_turns=7,
        )
        task = kb.get_task(conn, tid)
    assert task.goal_mode is True
    assert task.goal_max_turns == 7


def test_goal_mode_without_max_turns(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker", goal_mode=True
        )
        task = kb.get_task(conn, tid)
    assert task.goal_mode is True
    assert task.goal_max_turns is None


def test_legacy_db_migrates_goal_columns(tmp_path, monkeypatch):
    """A tasks table created without goal columns must gain them on init."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal legacy schema: tasks table missing goal_mode / goal_max_turns.
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    legacy.execute(
        "INSERT INTO tasks (id, title, status, priority, created_at, workspace_kind) "
        "VALUES ('legacy1', 'old', 'ready', 0, 1, 'scratch')"
    )
    legacy.commit()
    legacy.close()

    # init_db runs the additive migration.
    kb.init_db()
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "goal_mode" in cols
        assert "goal_max_turns" in cols
        task = kb.get_task(conn, "legacy1")
    # Existing row keeps the safe default.
    assert task.goal_mode is False
    assert task.goal_max_turns is None


# ---------------------------------------------------------------------------
# Spawn env
# ---------------------------------------------------------------------------

def test_spawn_sets_goal_env_only_when_enabled(kanban_home, monkeypatch):
    captured = {}

    class _FakeProc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="goal task",
            assignee="default",
            goal_mode=True,
            goal_max_turns=5,
        )
        task = kb.get_task(conn, tid)

    kb._default_spawn(task, str(kanban_home))
    env = captured["env"]
    assert env.get("HERMES_KANBAN_GOAL_MODE") == "1"
    assert env.get("HERMES_KANBAN_GOAL_MAX_TURNS") == "5"


def test_spawn_no_goal_env_for_plain_task(kanban_home, monkeypatch):
    captured = {}

    class _FakeProc:
        pid = 4243

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="default")
        task = kb.get_task(conn, tid)

    kb._default_spawn(task, str(kanban_home))
    env = captured["env"]
    assert "HERMES_KANBAN_GOAL_MODE" not in env
    assert "HERMES_KANBAN_GOAL_MAX_TURNS" not in env


# ---------------------------------------------------------------------------
# Goal loop logic (callback-injected, no live model)
# ---------------------------------------------------------------------------

def _patch_judge(monkeypatch, verdicts):
    """Make judge_goal return a scripted sequence of verdicts."""
    seq = list(verdicts)

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        v = seq.pop(0) if seq else "done"
        # 5-tuple contract: verdict, reason, parse failure, wait, transport failure.
        return v, f"scripted:{v}", False, None, False

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)


def test_loop_stops_when_worker_already_completed(monkeypatch):
    # Worker called kanban_complete on its first turn — no judging needed.
    _patch_judge(monkeypatch, ["continue"])  # should never be consulted
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="do the thing",
        run_turn=lambda p: turns.append(p) or "x",
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block"),
        first_response="done already",
    )
    assert res["outcome"] == "completed_by_worker"
    assert turns == []  # no extra turns


def test_loop_continues_then_worker_completes(monkeypatch):
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t2",
        goal_text="ship feature",
        run_turn=lambda p: turns.append(p) or f"turn{len(turns)}",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="started",
    )
    assert res["outcome"] == "completed_by_worker"
    # Two continuation turns fed before the worker completed.
    assert len(turns) == 2
    assert all("not done yet" in p for p in turns)


def test_loop_blocks_on_budget_exhaustion(monkeypatch):
    _patch_judge(monkeypatch, ["continue"] * 10)
    blocked = {}

    def _block(reason):
        blocked["reason"] = reason

    res = goals.run_kanban_goal_loop(
        task_id="t3",
        goal_text="endless task",
        run_turn=lambda p: "still going",
        task_status_fn=lambda: "running",
        block_fn=_block,
        max_turns=3,
        first_response="turn1",
    )
    assert res["outcome"] == "blocked_budget"
    assert res["turns_used"] == 3
    assert "turn budget" in blocked["reason"].lower()


def test_loop_finalize_nudge_when_judge_done_but_open(monkeypatch):
    # Judge says done, but worker never terminated → one finalize nudge,
    # then worker completes.
    _patch_judge(monkeypatch, ["done", "done"])
    statuses = iter(["running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t4",
        goal_text="task",
        run_turn=lambda p: turns.append(p) or "ok",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="looks done",
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 1
    assert "still open" in turns[0]


def test_loop_blocks_when_judge_done_but_never_finalizes(monkeypatch):
    # Judge keeps saying done, worker never calls kanban_complete → block
    # after the single finalize nudge.
    _patch_judge(monkeypatch, ["done", "done"])
    blocked = {}

    res = goals.run_kanban_goal_loop(
        task_id="t5",
        goal_text="task",
        run_turn=lambda p: "still not finalizing",
        task_status_fn=lambda: "running",
        block_fn=lambda r: blocked.update(reason=r),
        max_turns=10,
        first_response="looks done",
    )
    assert res["outcome"] == "blocked_budget"
    assert "finalize" in blocked["reason"].lower()


def test_loop_stops_if_task_reclaimed(monkeypatch):
    _patch_judge(monkeypatch, ["continue"])
    res = goals.run_kanban_goal_loop(
        task_id="t6",
        goal_text="task",
        run_turn=lambda p: pytest.fail("should not run a turn"),
        task_status_fn=lambda: "archived",
        block_fn=lambda r: pytest.fail("should not block"),
        first_response="x",
    )
    assert res["outcome"] == "stopped"


# ---------------------------------------------------------------------------
# Review-lane handoff: goal_mode is a per-CARD flag, so a code reviewer
# spawned on a goal-mode card runs this loop too. Once it has approved /
# requested changes the card belongs to another lane — the reviewer session
# must never be fed a continuation turn (which would push it toward
# kanban_complete, falsely claiming a merge) nor sticky-blocked on budget.
# ---------------------------------------------------------------------------

def _session_with_tool(name, result=None):
    """A conversation history where the session called ``name``.

    ``result`` is the tool message content; it defaults to the shape
    ``tools.kanban_tools._ok`` actually returns, because the guard only
    treats a call that LANDED as a lane handoff.
    """
    if result is None:
        result = json.dumps({"ok": True, "task_id": "t9", "status": "ready"})
    return [
        {"role": "user", "content": "review kanban task t9"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": "{}"}}],
        },
        {"role": "tool", "name": name, "tool_call_id": "c1", "content": result},
        {"role": "assistant", "content": "Approved."},
    ]


@pytest.mark.parametrize("tool", ["kanban_approve", "kanban_request_changes"])
def test_loop_exits_after_review_transition(monkeypatch, tool):
    # After the transition the card is ready/running again (handed to the
    # finalizer / back to the programmer) — which used to pass the loop's
    # terminality check and drag the reviewer into another turn.
    _patch_judge(monkeypatch, ["continue"] * 5)  # judge would say "keep going"

    res = goals.run_kanban_goal_loop(
        task_id="t7",
        goal_text="implement the feature",
        run_turn=lambda p: pytest.fail(f"fed a continuation turn to a reviewer: {p!r}"),
        task_status_fn=lambda: "ready",
        block_fn=lambda r: pytest.fail(f"blocked an approved card: {r!r}"),
        max_turns=10,
        first_response="Reviewed the diff; approving.",
        messages_fn=lambda: _session_with_tool(tool),
    )

    assert res["outcome"] == "review_transition"
    assert tool in res["reason"]
    assert res["turns_used"] == 1  # only the reviewer's own first turn


def test_loop_exits_after_review_transition_while_running(monkeypatch):
    # Same guard when the sibling lane has already claimed the card.
    _patch_judge(monkeypatch, ["continue"] * 5)

    res = goals.run_kanban_goal_loop(
        task_id="t8",
        goal_text="implement the feature",
        run_turn=lambda p: pytest.fail("should not run a turn"),
        task_status_fn=lambda: "running",
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="Requested changes.",
        messages_fn=lambda: _session_with_tool("kanban_request_changes"),
    )
    assert res["outcome"] == "review_transition"


def test_loop_unaffected_by_review_guard_for_plain_worker(monkeypatch):
    # Regression guard: an implementation session with no review transition
    # keeps looping exactly as before, even with messages_fn wired.
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "running", "done"])
    turns = []
    history = [
        {"role": "user", "content": "work kanban task t9"},
        {"role": "assistant", "content": "Working on it."},
    ]

    res = goals.run_kanban_goal_loop(
        task_id="t9",
        goal_text="ship feature",
        run_turn=lambda p: turns.append(p) or f"turn{len(turns)}",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="started",
        messages_fn=lambda: history,
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 2


def test_loop_ignores_rejected_kanban_complete(monkeypatch):
    # kanban_complete is terminal for the stop-guard, but the goal loop
    # observes it through the card status. A completion the judge gate
    # REJECTED leaves the card running — the loop must keep pushing.
    _patch_judge(monkeypatch, ["continue"])
    statuses = iter(["running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t10",
        goal_text="ship feature",
        run_turn=lambda p: turns.append(p) or "retrying",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="tried to complete",
        messages_fn=lambda: _session_with_tool("kanban_complete"),
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 1


@pytest.mark.parametrize("tool", ["kanban_approve", "kanban_request_changes"])
def test_loop_keeps_driving_after_a_FAILED_review_transition(monkeypatch, tool):
    """THE regression: ``kanban_approve`` refused (missing summary/head_sha,
    wrong status, lost claim) changed NOTHING on the board. Reading the call
    name alone made the loop exit, the run finished clean with the card still
    in ``review`` behind a live worker_pid, and the reaper logged a
    ``protocol_violation`` + spawned a failover reviewer — the exact bug this
    branch exists to remove, reached through a failed approve."""
    _patch_judge(monkeypatch, ["continue"])
    # The card never left the reviewer's hands, because the call bounced.
    statuses = iter(["running", "running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t12",
        goal_text="review the diff",
        run_turn=lambda p: turns.append(p) or "retrying the approval",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail(f"should not block: {r!r}"),
        max_turns=10,
        first_response="Approving.",
        messages_fn=lambda: _session_with_tool(
            tool, result=json.dumps({"error": "summary is required"}),
        ),
    )

    assert res["outcome"] != "review_transition"
    assert turns, "a refused review call must not stop the loop"
    assert res["outcome"] == "completed_by_worker"


def test_loop_exits_once_the_retried_approval_lands(monkeypatch):
    """The retry the loop drives toward is honoured as soon as it succeeds."""
    _patch_judge(monkeypatch, ["continue"] * 5)
    history = _session_with_tool(
        "kanban_approve", result=json.dumps({"error": "head_sha is required"}),
    )
    history[2]["tool_call_id"] = "c1"
    landed = _session_with_tool("kanban_approve")
    landed[1]["tool_calls"][0]["id"] = "c2"
    landed[2]["tool_call_id"] = "c2"

    res = goals.run_kanban_goal_loop(
        task_id="t13",
        goal_text="review the diff",
        run_turn=lambda p: history.extend(landed) or "approved for real",
        task_status_fn=lambda: "ready",
        block_fn=lambda r: pytest.fail(f"should not block: {r!r}"),
        max_turns=10,
        first_response="Approving.",
        messages_fn=lambda: history,
    )

    assert res["outcome"] == "review_transition"
    assert "kanban_approve" in res["reason"]
    assert res["turns_used"] == 2  # first turn bounced, second one landed


def test_review_guard_fails_open_when_messages_unavailable(monkeypatch):
    # A broken message source must never wedge the loop.
    _patch_judge(monkeypatch, ["continue"])
    statuses = iter(["running", "done"])
    turns = []

    def _boom():
        raise RuntimeError("no messages")

    res = goals.run_kanban_goal_loop(
        task_id="t11",
        goal_text="ship feature",
        run_turn=lambda p: turns.append(p) or "ok",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="started",
        messages_fn=_boom,
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 1


def test_continuation_templates_are_role_aware():
    # The reviewer's endings must be reachable from the prompts the loop
    # feeds; kanban_unblock is orchestrator-only and must never appear.
    for tpl in (
        goals.KANBAN_GOAL_CONTINUATION_TEMPLATE,
        goals.KANBAN_GOAL_FINALIZE_TEMPLATE,
    ):
        text = tpl.format(reason="because")
        assert "kanban_approve" in text
        assert "kanban_request_changes" in text
        assert "kanban_unblock" not in text


# ---------------------------------------------------------------------------
# CLI judge gate tests (hermes kanban complete bypass fix)
# ---------------------------------------------------------------------------

class TestCLIJudgeGate:
    """hermes kanban complete must apply the same goal_mode judge gate as the
    kanban_complete tool (Issue #38367 sibling gap).

    Uses mocks for kb.get_task and kb.complete_task to avoid depending on the
    full kanban_db schema; the gate logic is the unit under test.
    """

    def _run(self, monkeypatch, *, goal_mode=True, judge_available=True,
             verdict="done", reason="", complete_ok=True, summary="done"):
        import argparse
        import types
        from unittest.mock import MagicMock
        from hermes_cli.kanban import _cmd_complete

        fake_task = types.SimpleNamespace(
            goal_mode=goal_mode,
            title="Finish report",
            body="acceptance: criteria",
        )
        fake_conn = MagicMock()
        complete_calls: list = []

        def fake_connect_closing():
            from contextlib import contextmanager
            @contextmanager
            def _cm():
                yield fake_conn
            return _cm()

        def fake_complete_task(conn, tid, **kw):
            complete_calls.append(tid)
            return complete_ok

        monkeypatch.setattr("hermes_cli.kanban.kb.get_task", lambda conn, tid: fake_task)
        monkeypatch.setattr("hermes_cli.kanban.kb.complete_task", fake_complete_task)
        monkeypatch.setattr("hermes_cli.kanban.kb.connect_closing", fake_connect_closing)
        monkeypatch.setattr("hermes_cli.kanban._worker_run_id_for", lambda _: None)

        _aux_client = (object(), "judge-model") if judge_available else (None, None)
        monkeypatch.setattr(
            "agent.auxiliary_client.get_text_auxiliary_client",
            lambda name: _aux_client,
        )
        # Match the real judge_goal contract:
        # (verdict, reason, parse_failed, wait_directive, transport_failed)
        monkeypatch.setattr(
            "hermes_cli.goals.judge_goal",
            lambda **kw: (verdict, reason, False, None, False),
        )

        args = argparse.Namespace(task_ids=["t1"], summary=summary, result=None, metadata=None)
        return _cmd_complete(args), complete_calls

    def test_judge_rejects_premature_completion(self, monkeypatch):
        rc, complete_calls = self._run(
            monkeypatch, verdict="continue", reason="criteria not met"
        )
        assert rc != 0, "judge rejection must produce non-zero exit code"
        assert complete_calls == [], (
            "complete_task must NOT be invoked when the judge rejects"
        )

    def test_judge_allows_accepted_completion(self, monkeypatch):
        rc, complete_calls = self._run(monkeypatch, verdict="done")
        assert rc == 0
        assert complete_calls == ["t1"]

    def test_judge_unavailable_fails_open(self, monkeypatch):
        """No auxiliary client configured → gate skipped, task completes."""
        rc, complete_calls = self._run(monkeypatch, judge_available=False)
        assert rc == 0
        assert complete_calls == ["t1"]

    def test_non_goal_mode_task_skips_gate(self, monkeypatch):
        """Plain (non-goal_mode) tasks are never sent to the judge."""
        rc, complete_calls = self._run(monkeypatch, goal_mode=False)
        assert rc == 0
        assert complete_calls == ["t1"]

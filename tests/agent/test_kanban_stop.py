"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import json

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _tool_messages(name: str, result: object, *, call_id: str = "call-1") -> list[dict]:
    return [
        {"role": "user", "content": "work the kanban task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": name,
            "tool_call_id": call_id,
            "content": json.dumps(result),
        },
    ]






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "kanban_approve" in nudge
    assert "kanban_request_changes" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


@pytest.mark.parametrize("tool_name", ["kanban_approve", "kanban_request_changes"])
def test_successful_reviewer_decision_is_terminal(clear_kanban_env, tool_name):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = _tool_messages(
        tool_name,
        {"ok": True, "task_id": "t_review", "status": "ready"},
    )

    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


@pytest.mark.parametrize("decision_name", ["kanban_approve", "kanban_request_changes"])
def test_orphan_reviewer_result_is_not_terminal(clear_kanban_env, decision_name):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = [
        {
            "role": "tool",
            "name": decision_name,
            "tool_call_id": "orphan-result",
            "content": json.dumps({"ok": True}),
        }
    ]

    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None


@pytest.mark.parametrize("decision_name", ["kanban_approve", "kanban_request_changes"])
def test_reviewer_result_with_wrong_id_is_not_terminal(clear_kanban_env, decision_name):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = _tool_messages(
        decision_name,
        {"ok": True, "task_id": "t_review", "status": "ready"},
    )
    messages[-1]["tool_call_id"] = "wrong-call-id"

    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None


@pytest.mark.parametrize(
    ("decision_name", "other_decision_name"),
    [
        ("kanban_approve", "kanban_request_changes"),
        ("kanban_request_changes", "kanban_approve"),
    ],
)
def test_reviewer_result_with_mismatched_name_is_not_terminal(
    clear_kanban_env, decision_name, other_decision_name
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = _tool_messages(
        decision_name,
        {"ok": True, "task_id": "t_review", "status": "ready"},
    )
    messages[-1]["name"] = other_decision_name

    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None


@pytest.mark.parametrize("decision_name", ["kanban_approve", "kanban_request_changes"])
def test_reviewer_call_without_result_is_not_terminal(clear_kanban_env, decision_name):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = _tool_messages(
        decision_name,
        {"ok": True, "task_id": "t_review", "status": "ready"},
    )
    messages.pop()

    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None


@pytest.mark.parametrize("decision_name", ["kanban_approve", "kanban_request_changes"])
def test_reviewer_result_uses_originating_call_name_when_result_name_missing(
    clear_kanban_env, decision_name
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = _tool_messages(
        decision_name,
        {"ok": True, "task_id": "t_review", "status": "ready"},
    )
    messages[-1].pop("name")

    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


@pytest.mark.parametrize("tool_name", ["kanban_approve", "kanban_request_changes"])
def test_rejected_reviewer_decision_is_not_terminal(clear_kanban_env, tool_name):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = _tool_messages(tool_name, {"error": "review decision rejected"})

    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None


def test_comment_only_verdict_is_not_terminal(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = _tool_messages(
        "kanban_comment",
        {"ok": True, "comment_id": 42, "body": "APPROVE"},
    )

    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None


@pytest.mark.parametrize("tool_name", ["kanban_complete", "kanban_block"])
def test_generic_complete_and_block_remain_terminal(clear_kanban_env, tool_name):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_implementation")
    messages = _tool_messages(
        tool_name,
        {"ok": True, "task_id": "t_implementation", "status": "done"},
    )

    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_ordinary_nonterminal_call_still_nudges(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_work")
    messages = _tool_messages("kanban_heartbeat", {"ok": True})

    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.





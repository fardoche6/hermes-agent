"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import json

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
    session_review_transition,
    tool_result_outcome,
)

# The two shapes every kanban tool handler actually returns
# (``tools.kanban_tools._ok`` / ``tools.registry.tool_error``).
OK_RESULT = json.dumps({"ok": True, "task_id": "t_abc", "status": "ready"})


def error_result(message: str = "summary is required") -> str:
    return json.dumps({"error": message})


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_disabled_without_kanban_task(clear_kanban_env):
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_enabled_with_kanban_task(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    assert kanban_stop_nudge_enabled() is True


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


def test_no_nudge_after_kanban_block(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {"role": "tool", "name": "kanban_block", "tool_call_id": "1", "content": "blocked"},
    ]
    assert build_kanban_stop_nudge(messages=messages) is None


def test_nudge_budget_exhausted(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    assert build_kanban_stop_nudge(messages=[], attempts=2) is None
    assert build_kanban_stop_nudge(messages=[], attempts=1, max_attempts=1) is None
    assert build_kanban_stop_nudge(messages=[], attempts=0, max_attempts=1) is not None


# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.


def test_nudge_text_warns_about_blocking(clear_kanban_env):
    """The nudge should warn that repeated violations will block the task."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    nudge = build_kanban_stop_nudge(messages=[], attempts=0)
    assert nudge is not None
    assert "block" in nudge.lower(), (
        "nudge should warn that repeated violations will block the task"
    )


def test_nudge_and_dispatcher_budgets_are_independent(clear_kanban_env):
    """Agent-side nudge budget (2) and dispatcher-side streak (3) are
    separate budgets — the nudge counter does not affect the dispatcher's
    violation streak, and vice versa.

    This is a source-level invariant check: the nudge counter
    (``_kanban_stop_nudges``) lives on the AIAgent instance and resets
    per session, while the dispatcher streak lives in the task_runs DB
    table and persists across worker respawns.
    """
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    # Agent-side: 2 nudge attempts per session
    assert build_kanban_stop_nudge(messages=[], attempts=0) is not None
    assert build_kanban_stop_nudge(messages=[], attempts=1) is not None
    assert build_kanban_stop_nudge(messages=[], attempts=2) is None
    # Dispatcher-side streak is tracked in the DB, not in the nudge module —
    # the nudge module has no knowledge of the streak counter.
    assert not hasattr(build_kanban_stop_nudge, "_streak")


@pytest.mark.parametrize(
    "tool_name",
    ["kanban_approve", "kanban_request_changes", "kanban_review"],
)
def test_review_transitions_count_as_terminal(clear_kanban_env, tool_name):
    """A reviewer that approved (or returned the work) has made a real
    terminal transition — nudging it to "finish" is what produced the
    redundant reviewer runs after run 728."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_68b81df6")
    messages = [
        {"role": "user", "content": "review the PR"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": tool_name, "tool_call_id": "1",
         "content": OK_RESULT},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is None


def test_nudge_names_reviewer_terminal_transitions(clear_kanban_env):
    """A stalled reviewer must be told to approve / request changes.

    Naming only complete/block told the reviewer to do the two things it
    must not do: completing falsely claims the merge, and blocking is not
    an approval.
    """
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_68b81df6")
    nudge = build_kanban_stop_nudge(messages=[], attempts=0)
    assert nudge is not None
    assert "kanban_approve" in nudge
    assert "kanban_request_changes" in nudge
    # Implementation-side pair is still named for non-review runs.
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge


# ── Review-lane transition lookup ────────────────────────────────────
# ``session_review_transition`` is the public API the kanban goal loop
# uses to notice that a session handed its card to another lane. Only
# the review state machine's transitions count — the implementation
# pair is observed through the card's status instead.


def _board_tool_messages(
    tool_name: str, content: str | None = None, call_id: str = "1",
) -> list[dict]:
    return [
        {"role": "user", "content": "work the card"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": tool_name, "tool_call_id": call_id,
         "content": OK_RESULT if content is None else content},
    ]


@pytest.mark.parametrize(
    "tool_name",
    ["kanban_review", "kanban_approve", "kanban_request_changes"],
)
def test_session_review_transition_names_the_tool(clear_kanban_env, tool_name):
    assert session_review_transition(_board_tool_messages(tool_name)) == tool_name


@pytest.mark.parametrize("tool_name", ["kanban_complete", "kanban_block"])
def test_session_review_transition_ignores_implementation_pair(
    clear_kanban_env, tool_name
):
    """Complete/block end the run but are NOT a lane handoff — the goal loop
    reads those off the card's status, and a rejected kanban_complete must
    not look like a review transition."""
    messages = _board_tool_messages(tool_name)
    assert session_called_kanban_terminal(messages) is True
    assert session_review_transition(messages) is None


def test_session_review_transition_none_without_board_tool(clear_kanban_env):
    messages = [
        {"role": "user", "content": "work the card"},
        {"role": "assistant", "content": "Let me look at the repo first."},
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    assert session_review_transition(messages) is None
    assert session_review_transition([]) is None
    assert session_review_transition(None) is None


def test_session_review_transition_returns_last_transition(clear_kanban_env):
    """When several fire in one session the latest one owns the handoff."""
    messages = _board_tool_messages("kanban_review") + _board_tool_messages(
        "kanban_approve"
    )
    assert session_review_transition(messages) == "kanban_approve"


def test_nudge_never_mentions_orchestrator_only_unblock(clear_kanban_env):
    """``kanban_unblock`` is orchestrator-only — workers cannot call it, so
    the nudge must never point them at it."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    nudge = build_kanban_stop_nudge(messages=[], attempts=0)
    assert nudge is not None
    assert "kanban_unblock" not in nudge


# ── Success-awareness: a REFUSED board call is not a handoff ──────────
# ``kanban_approve`` has many rejection paths (missing summary, missing
# head_sha, foreign task id, delegated child, card not in the reviewer
# lane, lost claim, unresolvable finalizer). Every one answers with
# ``tool_error`` -> ``{"error": ...}``. Matching on the tool NAME alone
# made all of them indistinguishable from a landed approval: the nudge
# stayed silent, the goal loop stopped driving, the run exited clean with
# the card still in ``review`` behind a live ``worker_pid``, and the
# reaper logged the ``protocol_violation`` + failover reviewer this whole
# transition exists to eliminate.


@pytest.mark.parametrize(
    "tool_name",
    ["kanban_review", "kanban_approve", "kanban_request_changes"],
)
def test_failed_review_call_does_not_suppress_the_nudge(
    clear_kanban_env, tool_name
):
    """THE regression test: a rejected review-lane call must still nudge."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_68b81df6")
    messages = _board_tool_messages(
        tool_name, content=error_result("summary is required"),
    )
    assert session_called_kanban_terminal(messages) is False
    assert session_review_transition(messages) is None
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "t_68b81df6" in nudge


def test_failed_approve_then_successful_approve_counts(clear_kanban_env):
    """The retry the nudge asks for is honoured once it actually lands."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = _board_tool_messages(
        "kanban_approve", content=error_result("head_sha is required"),
        call_id="1",
    ) + _board_tool_messages("kanban_approve", call_id="2")
    assert session_review_transition(messages) == "kanban_approve"
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is None


def test_successful_approve_then_failed_reapprove_is_not_a_regression(
    clear_kanban_env
):
    """A landed approval stays landed even if a redundant second call is
    refused (the card already left the reviewer lane)."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = _board_tool_messages(
        "kanban_approve", call_id="1",
    ) + _board_tool_messages(
        "kanban_approve",
        content=error_result("cannot approve t_abc from status 'ready'"),
        call_id="2",
    )
    # The last call failed, but the card DID move on the first one. The
    # guard tracks the last call that landed, not the last call made.
    assert session_review_transition(messages) == "kanban_approve"
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is None


def test_review_lane_unclassifiable_result_is_not_a_handoff(clear_kanban_env):
    """Strict default for the (new) review lane: a result we cannot classify
    is NOT promoted to success. The cost is one bounded extra nudge; the
    cost of the other default is an abandoned reviewer + failover storm."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    for content in ("ok", "", "<html>gateway timeout</html>", "[1, 2]"):
        messages = _board_tool_messages("kanban_approve", content=content)
        assert session_review_transition(messages) is None, content
        assert build_kanban_stop_nudge(messages=messages, attempts=0) is not None
    # A tool message with no content at all is equally unclassifiable.
    messages = _board_tool_messages("kanban_approve")
    messages[-1]["content"] = None
    assert session_review_transition(messages) is None
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is not None


def test_review_call_without_a_result_is_not_a_handoff(clear_kanban_env):
    """Truncated between request and answer: outcome unknown, keep driving."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {"role": "user", "content": "review the PR"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "1", "type": "function",
                 "function": {"name": "kanban_approve", "arguments": "{}"}},
            ],
        },
    ]
    assert session_review_transition(messages) is None
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is not None


@pytest.mark.parametrize("tool_name", ["kanban_complete", "kanban_block"])
def test_positively_failed_implementation_call_still_nudges(
    clear_kanban_env, tool_name
):
    """A judge-rejected ``kanban_complete`` changed nothing on the board, so
    the worker must be nudged to fix it rather than exit clean."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = _board_tool_messages(
        tool_name, content=error_result("Goal completion rejected by judge"),
    )
    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is not None


@pytest.mark.parametrize("tool_name", ["kanban_complete", "kanban_block"])
def test_implementation_pair_keeps_truncation_tolerance(
    clear_kanban_env, tool_name
):
    """Lenient default for the legacy implementation pair: an unclassifiable
    or missing result keeps counting, so the fleet-wide behaviour of these
    two tools is unchanged except for POSITIVELY identified errors."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    assert session_called_kanban_terminal(
        _board_tool_messages(tool_name, content="done")
    ) is True
    assert session_called_kanban_terminal([
        {"role": "tool", "name": tool_name, "tool_call_id": "1",
         "content": "done"},
    ]) is True
    assert session_called_kanban_terminal([
        {
            "role": "assistant", "content": "",
            "tool_calls": [
                {"id": "1", "type": "function",
                 "function": {"name": tool_name, "arguments": "{}"}},
            ],
        },
    ]) is True


def test_tool_result_outcome_classification():
    """The success/failure/unknown contract the guard is built on."""
    assert tool_result_outcome(json.dumps({"ok": True, "task_id": "t"})) is True
    assert tool_result_outcome({"ok": True}) is True
    assert tool_result_outcome(json.dumps({"error": "boom"})) is False
    assert tool_result_outcome({"error": "boom"}) is False
    assert tool_result_outcome(json.dumps({"ok": False})) is False
    # Unknown — never silently promoted either way.
    assert tool_result_outcome("ok") is None
    assert tool_result_outcome("") is None
    assert tool_result_outcome(None) is None
    assert tool_result_outcome(json.dumps({"task_id": "t"})) is None
    assert tool_result_outcome(json.dumps([1, 2])) is None
    # Multimodal content lists are flattened before parsing.
    assert tool_result_outcome(
        [{"type": "text", "text": json.dumps({"error": "boom"})}]
    ) is False
    assert tool_result_outcome(
        [{"type": "text", "text": json.dumps({"ok": True})}]
    ) is True


def test_nudge_tells_the_worker_to_retry_after_a_tool_error(clear_kanban_env):
    """The nudge must say an errored board call did not move the card —
    otherwise a model reads the refusal as terminal and stops anyway."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    nudge = build_kanban_stop_nudge(messages=[], attempts=0)
    assert nudge is not None
    assert "error" in nudge.lower()
    assert "head_sha" in nudge

"""Turn-end guard for kanban workers.

Kanban workers must end with a terminal board transition: ``kanban_complete``
/ ``kanban_block`` for implementation runs, ``kanban_approve`` /
``kanban_request_changes`` for code review runs. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.

"Called the tool" is not enough — the call has to have LANDED. Every kanban
handler answers with either ``{"ok": true, ...}`` or ``{"error": "..."}``, and
a refused call leaves the card exactly where it was, so it must not silence
the nudge or stop a goal loop. See :func:`tool_result_outcome`.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Optional


# The review state machine's transitions. Each one hands the SAME card to a
# DIFFERENT lane, so the calling session has no further business with it: it
# must not be nudged to "finish", and a goal loop driving it must stop.
#
# This membership is stated explicitly rather than derived by subtracting the
# implementation pair from the terminal set — otherwise any terminal board
# tool added later would be silently classified as a review-lane handoff,
# which is the wrong default.
REVIEW_LANE_KANBAN_TOOLS = frozenset({
    "kanban_review",
    "kanban_approve",
    "kanban_request_changes",
})

# The implementation-side terminal pair: the run either finished the work or
# gave up on it, and in both cases the card leaves the run's hands.
_IMPLEMENTATION_KANBAN_TOOLS = frozenset({
    "kanban_complete",
    "kanban_block",
})

# Every board tool that ends a run's ownership of the card.
_TERMINAL_KANBAN_TOOLS = _IMPLEMENTATION_KANBAN_TOOLS | REVIEW_LANE_KANBAN_TOOLS

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def _result_text(content: Any) -> str:
    """Flatten a tool message's ``content`` to text.

    Handles the plain-string form and the multimodal list form
    (``[{"type": "text", "text": ...}, ...]``) the dispatcher can produce.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "\n".join(parts)
    return ""


def tool_result_outcome(content: Any) -> Optional[bool]:
    """Classify a board-tool result: True success, False failure, None unknown.

    The kanban tool handlers speak exactly two shapes (``tools/kanban_tools``
    ``_ok`` and ``tools/registry`` ``tool_error``)::

        {"ok": true, ...}          # success
        {"error": "...", ...}      # refusal

    Anything else — non-JSON prose, a JSON scalar/array, a dict carrying
    neither key, a missing result entirely — is ``None``: genuinely unknown,
    NOT quietly promoted to success. Callers decide what an unknown means for
    their lane; this function never guesses.
    """
    payload: Any = content
    if not isinstance(payload, dict):
        text = _result_text(content).strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    if payload.get("error"):
        return False
    if "ok" in payload:
        return bool(payload.get("ok"))
    if "error" in payload:
        # Present but falsy/empty — a handler that explicitly says "no error".
        return True
    return None


def _last_board_tool_called(
    messages: Iterable[dict] | None,
    wanted: frozenset,
    *,
    unknown_counts: bool,
) -> Optional[str]:
    """Name of the last tool in ``wanted`` that this conversation *landed*.

    Single source of message-walking logic. A tool call is matched by the
    assistant's requested ``tool_calls`` entry and/or the ``tool`` result
    message that answers it; the RESULT decides whether the call counts,
    because a refused call changed nothing on the board. Before this,
    ``kanban_approve()`` rejected for a missing summary was indistinguishable
    from a landed approval — the run then exited clean with the card still in
    ``review``, which is the exact protocol-violation/failover storm this
    module exists to prevent.

    ``unknown_counts`` selects the default for a result we cannot classify
    (see :func:`tool_result_outcome`), including a call whose result never
    arrived because the run was truncated between request and answer.
    """
    if not messages:
        return None
    found: Optional[str] = None
    # tool_call_id -> requested name, for calls still awaiting a result.
    pending: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                name = _tool_call_name(tc)
                if name not in wanted:
                    continue
                call_id = ""
                if isinstance(tc, dict):
                    call_id = str(tc.get("id") or "")
                else:
                    call_id = str(getattr(tc, "id", "") or "")
                pending[call_id] = name
        elif role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            name = str(msg.get("name") or msg.get("tool_name") or "")
            if not name:
                name = pending.get(call_id, "")
            pending.pop(call_id, None)
            if name not in wanted:
                continue
            outcome = tool_result_outcome(msg.get("content"))
            if outcome is True or (outcome is None and unknown_counts):
                found = name
    # Calls that never got an answer: their outcome is unknown by definition.
    if unknown_counts and pending:
        for name in pending.values():
            found = name
    return found


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already LANDED a terminal kanban tool.

    A refused call does not count: the nudge exists precisely so a worker
    whose terminal call bounced gets another turn to do it properly instead
    of exiting clean into a dispatcher ``protocol_violation``.

    The two lanes differ in what an unclassifiable result means:

    * review lane — strict. ``kanban_review`` / ``kanban_approve`` /
      ``kanban_request_changes`` are new, only ever answered by ``_ok`` /
      ``tool_error``, and have many refusal paths (missing summary or
      head_sha, foreign task id, delegated child, wrong status, unresolvable
      finalizer, lost claim). An unrecognised result there is treated as
      "did not land", so the guard keeps driving the run. The cost is a
      bounded, self-correcting extra nudge; the cost of the other default is
      the abandoned-reviewer failover storm.
    * implementation pair — lenient about unknowns, to preserve the
      long-standing truncation tolerance for ``kanban_complete`` /
      ``kanban_block`` across the whole fleet. A *positively identified*
      error still does not count, which is the case that matters (a
      judge-rejected ``kanban_complete`` must not read as a handoff).
    """
    if session_review_transition(messages) is not None:
        return True
    return _last_board_tool_called(
        messages, _IMPLEMENTATION_KANBAN_TOOLS, unknown_counts=True,
    ) is not None


def session_review_transition(messages: Iterable[dict] | None) -> Optional[str]:
    """Name the review-lane board transition this conversation LANDED, if any.

    Returns the last successfully-applied member of
    ``REVIEW_LANE_KANBAN_TOOLS``, or ``None``. The implementation pair
    (``kanban_complete`` / ``kanban_block``) deliberately does NOT count:
    callers observe those through the card's status, and a *rejected*
    completion attempt must not read as a handoff.

    A review call whose result is an error — or whose result cannot be
    classified — returns ``None``: the card did not move, so the goal loop
    must keep driving this session rather than exiting and leaving the card
    stranded in ``review`` with a live ``worker_pid``.
    """
    return _last_board_tool_called(
        messages, REVIEW_LANE_KANBAN_TOOLS, unknown_counts=False,
    )


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already completed/blocked, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no terminal board "
        "transition).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call the terminal board tool for YOUR role:\n"
        "   • implementation run → `kanban_complete(summary=..., "
        "artifacts=[...])` if the work is done, or "
        "`kanban_block(reason=\"review-required: ...\")` if you are stuck.\n"
        "   • code review run → `kanban_approve(summary=..., head_sha=...)` "
        "to approve (this hands the same card to the finalizer; it does NOT "
        "mark the card done), or `kanban_request_changes(...)` to send it "
        "back. Never use `kanban_complete` for a review — that falsely "
        "claims the merge.\n"
        "3. If the board tool returns an error, READ it and call the tool "
        "again with the problem fixed — an errored call did not move the "
        "card, and stopping there is still a protocol violation.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "REVIEW_LANE_KANBAN_TOOLS",
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
    "session_review_transition",
    "tool_result_outcome",
]

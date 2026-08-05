"""Turn-end guard for kanban workers.

Kanban workers must end with a terminal board transition: implementation runs
use ``kanban_complete`` or ``kanban_block``; active review runs use
``kanban_approve`` or ``kanban_request_changes``. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.

A reviewer decision counts only when its tool result positively reports
success. A rejected decision leaves the review run open and must continue to
be nudged; a comment containing a verdict is not a lifecycle transition.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Optional


_IMPLEMENTATION_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})
_REVIEW_DECISION_KANBAN_TOOLS = frozenset(
    {"kanban_approve", "kanban_request_changes"}
)

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


def _tool_call_id(tc: Any) -> str:
    if isinstance(tc, dict):
        return str(tc.get("id") or "")
    return str(getattr(tc, "id", "") or "")


def _result_text(content: Any) -> str:
    """Flatten a tool result's supported content forms to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _tool_result_outcome(content: Any) -> Optional[bool]:
    """Classify a tool result as success, failure, or unknown.

    Kanban handlers return ``{"ok": true, ...}`` on success and
    ``{"error": "..."}`` on rejection. Unknown content is deliberately not
    promoted to success for reviewer decisions.
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
    if "error" in payload:
        return False
    if "ok" in payload:
        return payload["ok"] is True
    if "success" in payload:
        return payload["success"] is True
    return None


def _last_landed_tool(
    messages: Iterable[dict] | None,
    wanted: frozenset[str],
    *,
    unknown_counts: bool,
    require_call_match: bool,
) -> Optional[str]:
    """Return the last landed tool in ``wanted``.

    Reviewer decisions are strict: only a successful result counts. The
    implementation pair retains its historical tolerance for an unclassified
    or missing result, while an explicit error still does not count.
    """
    if not messages:
        return None

    found: Optional[str] = None
    pending: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                name = _tool_call_name(tc)
                if name in wanted:
                    pending[_tool_call_id(tc)] = name
            continue
        if msg.get("role") != "tool":
            continue

        call_id = str(msg.get("tool_call_id") or "")
        result_name = str(msg.get("name") or msg.get("tool_name") or "")
        if require_call_match:
            if not call_id:
                continue
            name = pending.pop(call_id, None)
            if name is None:
                continue
            if result_name and result_name != name:
                continue
        else:
            name = result_name or pending.get(call_id, "")
            pending.pop(call_id, None)
        if name not in wanted:
            continue

        outcome = _tool_result_outcome(msg.get("content"))
        if outcome is True or (outcome is None and unknown_counts):
            found = name

    if unknown_counts and pending:
        # Preserve the existing implementation-worker behavior when the
        # process ended between emitting a terminal call and receiving its
        # tool result. Reviewer decisions intentionally do not use this path.
        found = next(iter(pending.values()))
    return found


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already observed a terminal board outcome."""
    if messages is None:
        return False
    messages = list(messages)
    if _last_landed_tool(
        messages,
        _REVIEW_DECISION_KANBAN_TOOLS,
        unknown_counts=False,
        require_call_match=True,
    ) is not None:
        return True
    return _last_landed_tool(
        messages,
        _IMPLEMENTATION_KANBAN_TOOLS,
        unknown_counts=True,
        require_call_match=False,
    ) is not None


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already terminal, or nudge budget exhausted).
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
        "2. Call the terminal board tool for your role:\n"
        "   • implementation run → `kanban_complete(summary=..., "
        "artifacts=[...])` if done, OR `kanban_block(reason=...)` if blocked.\n"
        "   • active code-review run → `kanban_approve(head_sha=..., "
        "summary=...)` or `kanban_request_changes(...)`. A comment-only "
        "verdict is not terminal, and a rejected decision call did not move "
        "the card; fix the error and retry.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]

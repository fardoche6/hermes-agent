"""Pure rendering contracts for the Hermes human-gate notifier.

Kanban event selection, deduplication, subscription lookup, and delivery are
owned by Hermes. This module only validates an already-selected alert and
renders the Telegram Card 1 message.
"""

from dataclasses import dataclass
from html import escape
import re
from typing import Protocol

_MAX_TELEGRAM_LENGTH = 4096
_SECRET_PATTERNS = (
    re.compile(r"api[_-]?key\s*[:=]", re.IGNORECASE),
    re.compile(r"token\s*[:=]", re.IGNORECASE),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One copy-pastable Card 1 action."""

    label: str
    recommended: bool = False


@dataclass(frozen=True, slots=True)
class HumanGateAlert:
    """Input data for one already-selected human-gate notification."""

    task_id: str
    task_title: str
    board: str
    blocked_age: str
    situation: str
    decision_needed: str
    suggestions: tuple[Suggestion, ...]
    episode_kind: str = "human_block"
    block_kind: str | None = None


class Notifier(Protocol):
    """Delivery contract for a rendered alert."""

    def send(self, message: str) -> None:
        """Deliver one already-rendered message."""


def _contains_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def _validate(alert: HumanGateAlert) -> None:
    required = {
        "task_title": alert.task_title,
        "board": alert.board,
        "blocked_age": alert.blocked_age,
        "situation": alert.situation,
        "decision_needed": alert.decision_needed,
    }
    if any(not isinstance(value, str) or not value.strip() for value in required.values()):
        raise ValueError("task title, board, age, situation, and decision are required")
    if not isinstance(alert.task_id, str) or not alert.task_id.strip():
        raise ValueError("task id is required")
    if len(alert.suggestions) != 3:
        raise ValueError("exactly three suggestions are required")
    if sum(suggestion.recommended for suggestion in alert.suggestions) != 1:
        raise ValueError("exactly one suggestion must be recommended")
    if not alert.suggestions[0].recommended:
        raise ValueError("the first suggestion must be recommended")
    for suggestion in alert.suggestions:
        if not isinstance(suggestion.label, str) or not suggestion.label.strip():
            raise ValueError("suggestion labels are required")
    values = [alert.task_id, *required.values(), *(s.label for s in alert.suggestions)]
    if any(_contains_secret(value) for value in values):
        raise ValueError("secret-like values are not allowed in an alert")


def _decision_text(alert: HumanGateAlert, situation: str) -> str:
    decision = alert.decision_needed.strip()
    situation = situation.strip()
    if situation and situation not in decision:
        return f"{decision} {situation}"
    return decision


def _middle_truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    if max_length <= 1:
        return "…"[:max_length]
    left = (max_length - 1) // 2
    right = max_length - 1 - left
    return f"{value[:left]}…{value[-right:]}"


def _render(alert: HumanGateAlert, situation: str) -> str:
    title = escape(alert.task_title.strip(), quote=True)
    board = escape(alert.board.strip(), quote=True)
    age = escape(alert.blocked_age.strip(), quote=True)
    decision = escape(_decision_text(alert, situation), quote=True)
    suggestions = "\n".join(
        f"{index}- {escape(item.label.strip(), quote=True)}"
        f"{' (Recommended)' if item.recommended else ''}"
        for index, item in enumerate(alert.suggestions, start=1)
    )
    return (
        "🚨 <b>Task blocked by human decision</b>\n\n"
        f"Task: &quot;{title}&quot; ({board} · blocked {age})\n\n"
        f"Need your decision: {decision}\n\n"
        f"🔧 Suggestions:\n{suggestions}"
    )


def render_alert(alert: HumanGateAlert) -> str:
    """Render one validated alert as HTML ready for Telegram ``parse_mode=HTML``."""

    _validate(alert)
    rendered = _render(alert, alert.situation)
    if len(rendered) > _MAX_TELEGRAM_LENGTH:
        # Situation is the only free-form field permitted to shrink. Binary
        # search accounts for HTML escaping and preserves every fixed section.
        low, high = 0, len(alert.situation)
        while low < high:
            midpoint = (low + high + 1) // 2
            candidate = _middle_truncate(alert.situation, midpoint)
            if len(_render(alert, candidate)) <= _MAX_TELEGRAM_LENGTH:
                low = midpoint
            else:
                high = midpoint - 1
        rendered = _render(alert, _middle_truncate(alert.situation, low))
    if len(rendered) > _MAX_TELEGRAM_LENGTH:
        raise ValueError("alert cannot fit within Telegram's 4096-character limit")
    return rendered

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_BOARD = "default"


def _now() -> datetime:
    return datetime.utcnow()


def _find_kanban_db(board: str) -> Path:
    candidates = [
        Path.home() / ".hermes" / board / "kanban.db",
        Path.home() / ".hermes" / "kanban.db",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "Cannot find kanban.db for board "
        f"{board!r}. Checked: " + ", ".join(str(candidate) for candidate in candidates)
    )


def _coerce_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class BlockedTask:
    board: str
    id: str
    title: str
    assignee: str | None
    priority: int | None
    created_at: str | None
    blocked_at: str | None
    consecutive_failures: int | None
    reason: str | None
    db_path: Path


def collect_latest_nonhuman_blocked_tasks(limit: int = 1, board: str = DEFAULT_BOARD) -> list[BlockedTask]:
    db_path = _find_kanban_db(board)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            SELECT
                board,
                id,
                title,
                assignee,
                priority,
                created_at,
                blocked_at,
                consecutive_failures,
                reason
            FROM tasks
            WHERE status = 'blocked'
              AND blocking_kind NOT IN ('human', 'approval')
            ORDER BY blocked_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    results: list[BlockedTask] = []
    for row in rows:
        results.append(
            BlockedTask(
                board=row["board"] or board,
                id=row["id"],
                title=row["title"],
                assignee=row["assignee"],
                priority=row["priority"],
                created_at=row["created_at"],
                blocked_at=row["blocked_at"],
                consecutive_failures=row["consecutive_failures"],
                reason=row["reason"],
                db_path=db_path,
            )
        )
        if len(results) >= limit:
            break
    return results


if __name__ == "__main__":
    tasks = collect_latest_nonhuman_blocked_tasks(limit=1)
    for task in tasks:
        print(task.id)

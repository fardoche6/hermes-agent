"""Durable, compact Kanban lifecycle relay for the Hermes Home channel.

The relay is deliberately outside the task lifecycle: it reads each configured
board through the public Kanban API, stores its own cursor/outbox, and sends via
the supported ``hermes send`` command. Delivery failures therefore cannot alter
or block Kanban state.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import kanban_db as kb

EVENT_MATRIX = {
    "claimed": "wake/precheck",
    "promoted": "wake/precheck",
    "blocked": "blocker",
    "block_loop_detected": "blocker",
    "reclaimed": "recovery",
    "reclaim_deferred": "recovery",
    "unblocked": "recovery",
    "completed": "done",
}
DEFAULT_TARGET = "telegram"
DEFAULT_MAX_ATTEMPTS = 3
_MAX_FIELD = 180
_SECRET_RE = re.compile(
    r"(?i)(?:token|password|passwd|secret|api[_ -]?key|credential)\s*[:=]\s*[^\s,;]+"
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")


def relay_state_path() -> Path:
    configured = os.environ.get("HERMES_KANBAN_RELAY_STATE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return kb.kanban_home() / "kanban" / "home-relay.json"


def _safe(value: Any, limit: int = _MAX_FIELD) -> str:
    text = " ".join(str(value or "").split())
    text = _SECRET_RE.sub("[redacted]", text)
    text = _EMAIL_RE.sub("[redacted-email]", text)
    return text[:limit] + ("…" if len(text) > limit else "")


def _event_key(board: str, event: kb.Event) -> str:
    return f"{board}:{event.task_id}:{event.id}:{event.kind}"


def render_event(board: str, task: kb.Task, event: kb.Event) -> str:
    category = EVENT_MATRIX[event.kind]
    payload = event.payload or {}
    detail = payload.get("reason") or payload.get("summary") or payload.get("result")
    parts = [
        "[Kanban]",
        f"board={_safe(board, 60)}",
        f"tenant={_safe(task.tenant or 'default', 60)}",
        f"task={_safe(task.id, 80)}",
        f"title={_safe(task.title, 120)}",
        f"event={category}/{event.kind}",
        f"status={_safe(task.status, 40)}",
    ]
    if detail:
        parts.append(f"detail={_safe(detail)}")
    parts.append(f"ref=kanban://{_safe(board, 60)}/{_safe(task.id, 80)}")
    return " | ".join(parts)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "boards": {}, "outbox": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("version") == 1:
            raw.setdefault("boards", {})
            raw.setdefault("outbox", {})
            return raw
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "boards": {}, "outbox": {}}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _default_sender(target: str, message: str) -> dict[str, Any]:
    executable = shutil.which("hermes") or "hermes"
    completed = subprocess.run(
        [executable, "send", "--to", target, message, "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(_safe(completed.stderr or completed.stdout or "send failed"))
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"raw": _safe(completed.stdout)}
    if payload.get("error"):
        raise RuntimeError(_safe(payload["error"]))
    return payload


class HomeRelay:
    def __init__(
        self,
        *,
        state_path: Optional[Path] = None,
        target: Optional[str] = None,
        sender: Optional[Callable[[str, str], dict[str, Any]]] = None,
        clock: Callable[[], float] = time.time,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_base_seconds: int = 30,
    ) -> None:
        self.state_path = state_path or relay_state_path()
        self.target = (target or os.environ.get("HERMES_KANBAN_HOME_TARGET", DEFAULT_TARGET)).strip()
        self.sender = sender or _default_sender
        self.clock = clock
        self.max_attempts = max(1, max_attempts)
        self.retry_base_seconds = max(1, retry_base_seconds)

    def _discover(self) -> list[dict[str, Any]]:
        # Worker environments pin one board with HERMES_KANBAN_DB. The relay
        # is the deliberate exception: it must inspect every board.
        saved = {name: os.environ.pop(name, None) for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD")}
        try:
            return kb.list_boards(include_archived=False)
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value

    def run_once(self, *, dry_run: bool = False) -> dict[str, Any]:
        state = _load_state(self.state_path)
        now = int(self.clock())
        boards = self._discover()
        sent = failed = permanent = queued = 0

        # Seed a safe watermark for newly seen boards. This intentionally skips
        # all historical rows, including old completed events.
        for meta in boards:
            slug = str(meta["slug"])
            board_state = state["boards"].setdefault(slug, {})
            if "cursor" not in board_state:
                path = Path(meta["db_path"])
                latest = 0
                if path.exists():
                    with kb.connect(db_path=path) as conn:
                        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM task_events").fetchone()
                        latest = int(row["n"])
                board_state["cursor"] = latest

        for meta in boards:
            slug = str(meta["slug"])
            cursor = int(state["boards"][slug]["cursor"])
            path = Path(meta["db_path"])
            if not path.exists():
                continue
            with kb.connect(db_path=path) as conn:
                rows = conn.execute(
                    "SELECT * FROM task_events WHERE id > ? ORDER BY id ASC", (cursor,)
                ).fetchall()
                for row in rows:
                    event = kb.Event(
                        id=int(row["id"]), task_id=row["task_id"], kind=row["kind"],
                        payload=json.loads(row["payload"]) if row["payload"] else None,
                        created_at=int(row["created_at"]),
                        run_id=(int(row["run_id"]) if row["run_id"] is not None else None),
                    )
                    key = _event_key(slug, event)
                    if event.kind not in EVENT_MATRIX:
                        state["boards"][slug]["cursor"] = event.id
                        continue
                    task = kb.get_task(conn, event.task_id)
                    if task is None:
                        state["boards"][slug]["cursor"] = event.id
                        continue
                    state["outbox"].setdefault(key, {
                        "board": slug, "event_id": event.id, "message": render_event(slug, task, event),
                        "attempts": 0, "next_attempt_at": 0, "status": "pending",
                    })
                    queued += 1
                    state["boards"][slug]["cursor"] = event.id

        for key, item in list(state["outbox"].items()):
            if item.get("status") != "pending" or int(item.get("next_attempt_at", 0)) > now:
                continue
            if dry_run:
                continue
            try:
                result = self.sender(self.target, item["message"])
            except Exception as exc:
                item["attempts"] = int(item.get("attempts", 0)) + 1
                item["last_error"] = _safe(exc)
                failed += 1
                if item["attempts"] >= self.max_attempts:
                    item["status"] = "permanent_failure"
                    permanent += 1
                else:
                    item["next_attempt_at"] = now + self.retry_base_seconds * (2 ** (item["attempts"] - 1))
                continue
            item["status"] = "sent"
            item["sent_at"] = now
            item["sender_result"] = result if isinstance(result, dict) else {"result": _safe(result)}
            sent += 1

        _save_state(self.state_path, state)
        return {"boards": [str(b["slug"]) for b in boards], "queued": queued, "sent": sent,
                "failed": failed, "permanent_failures": permanent, "target": self.target,
                "dry_run": dry_run}


def run_once(**kwargs: Any) -> dict[str, Any]:
    return HomeRelay(**kwargs).run_once()

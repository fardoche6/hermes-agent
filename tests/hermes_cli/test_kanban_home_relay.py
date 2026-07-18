from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_home_relay import HomeRelay, render_event


def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_RELAY_STATE", str(home / "relay.json"))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")
    kb.create_board("product")
    return home


def test_current_watermark_dynamic_boards_and_duplicate_suppression(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    calls = []
    conn = kb.connect(board="default")
    task_id = kb.create_task(conn, title="Wake task", assignee="worker")
    conn.close()
    relay = HomeRelay(sender=lambda target, message: calls.append((target, message)) or {"message_id": "m1"})

    first = relay.run_once()
    assert set(first["boards"]) == {"default", "product"}
    assert calls == []  # installation watermark skips historical rows

    conn = kb.connect(board="default")
    assert kb.claim_task(conn, task_id) is not None
    conn.close()
    product_conn = kb.connect(board="product")
    product_task = kb.create_task(product_conn, title="Product task", assignee="worker")
    assert kb.claim_task(product_conn, product_task) is not None
    product_conn.close()
    assert relay.run_once()["sent"] == 2
    assert relay.run_once()["sent"] == 0
    assert calls[0][0] == "telegram"
    assert {"board=default", "board=product"} == {call[1].split(" | ")[1] for call in calls}
    assert all("event=wake/precheck/claimed" in call[1] for call in calls)
    assert all("thread" not in call[1] for call in calls)


def test_secret_and_email_redaction_and_no_body_leak():
    task = SimpleNamespace(
        id="t_secret", title="email me a@b.example password=shh", body="raw body",
        status="blocked", tenant="tenant",
    )
    event = kb.Event(1, task.id, "blocked", {"reason": "api_key=abc"}, 1)
    rendered = render_event("board", task, event)  # type: ignore[arg-type]
    assert "shh" not in rendered and "a@b.example" not in rendered and "abc" not in rendered
    assert "raw body" not in rendered
    assert "tenant=tenant" in rendered


def test_restart_cursor_recovery_and_transient_then_permanent_failure(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    conn = kb.connect(board="default")
    task_id = kb.create_task(conn, title="Retry task", assignee="worker")
    kb.claim_task(conn, task_id)
    conn.close()
    state = tmp_path / ".hermes" / "relay.json"
    HomeRelay(state_path=state, sender=lambda *_: {"message_id": "seed"}).run_once()
    conn = kb.connect(board="default")
    retry_id = kb.create_task(conn, title="Retry task 2", assignee="worker")
    kb.claim_task(conn, retry_id)
    conn.close()
    HomeRelay(state_path=state, sender=lambda *_: (_ for _ in ()).throw(RuntimeError("temporary")), max_attempts=2, retry_base_seconds=1, clock=lambda: 0).run_once()
    saved = json.loads(state.read_text())
    pending = [item for item in saved["outbox"].values() if item["status"] == "pending"]
    assert pending[0]["attempts"] == 1
    # A new process sees the same outbox and retries it; the second failure is observable.
    relay = HomeRelay(state_path=state, sender=lambda *_: (_ for _ in ()).throw(RuntimeError("permanent")), max_attempts=2, retry_base_seconds=1, clock=lambda: 2)
    result = relay.run_once()
    assert result["permanent_failures"] == 1
    saved = json.loads(state.read_text())
    assert any(item["status"] == "permanent_failure" for item in saved["outbox"].values())

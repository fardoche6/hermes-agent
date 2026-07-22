import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("gateway_safe_restart", Path(__file__).parents[1] / "scripts" / "gateway-safe-restart.py")
assert SPEC is not None and SPEC.loader is not None
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def make_board(path: Path, task_id: str = "restart"):
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, current_run_id TEXT, worker_pid INTEGER);
        CREATE TABLE task_runs (task_id TEXT, worker_pid INTEGER, status TEXT);
        CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT, body TEXT, created_at INTEGER);
        """)
        db.execute("INSERT INTO tasks VALUES (?, 'running', NULL, NULL)", (task_id,))


def test_pending_uses_realtime_start_and_dropin(monkeypatch, tmp_path):
    dropin = tmp_path / "override.conf"
    dropin.write_text("[Service]\nEnvironment=HERMES_SAFE_RESTART_PROBE=x\n")
    monkeypatch.setattr(MOD, "unit_properties", lambda _: {
        "ActiveState": "active", "MainPID": "42",
        "ExecMainStartTimestamp": "Wed 2026-07-22 10:00:00 UTC",
        "FragmentPath": "", "DropInPaths": str(dropin),
    })
    state = MOD.pending_state(["demo.service"])["demo.service"]
    assert state["newer"] == [str(dropin)]


def test_idle_excludes_current_task_but_not_other_work(tmp_path):
    board = tmp_path / "kanban.db"
    make_board(board)
    with sqlite3.connect(board) as db:
        db.execute("INSERT INTO tasks VALUES ('other', 'claimed', NULL, NULL)")
    state = MOD.idle_state([board], "restart")
    assert [row["id"] for row in state["busy_tasks"]] == ["other"]


def test_true_idle_window_returns_true_only_when_cards_and_gateway_children_are_absent(
    monkeypatch, tmp_path
):
    board = tmp_path / "kanban.db"
    make_board(board)
    monkeypatch.setattr(MOD, "gateway_worker_descendants", lambda *_args: [])

    assert MOD.true_idle_window([board], "restart", gateway_pids=[42]) is True


def test_true_idle_window_blocks_on_active_card(monkeypatch, tmp_path):
    board = tmp_path / "kanban.db"
    make_board(board)
    with sqlite3.connect(board) as db:
        db.execute("INSERT INTO tasks VALUES ('other', 'running', NULL, NULL)")
    monkeypatch.setattr(MOD, "gateway_worker_descendants", lambda *_args: [])

    assert MOD.true_idle_window([board], "restart", gateway_pids=[42]) is False


def test_true_idle_window_blocks_on_gateway_child(monkeypatch, tmp_path):
    board = tmp_path / "kanban.db"
    make_board(board)
    monkeypatch.setattr(MOD, "gateway_worker_descendants", lambda *_args: [99])

    assert MOD.true_idle_window([board], "restart", gateway_pids=[42]) is False


def test_timeout_notifies_command_and_comments(tmp_path):
    board = tmp_path / "kanban.db"
    make_board(board)
    received = tmp_path / "payload.json"
    notifier = tmp_path / "notify.py"
    notifier.write_text("import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])")
    # Use a direct Python command to exercise the same argv-safe notifier path.
    command = f"{sys.executable} {notifier} {received}"
    channel = MOD.notify_owner("busy", command=command, hermes_home=tmp_path)
    assert channel == "command"
    assert '"reason": "busy"' in received.read_text()

    MOD.append_comment([board], "restart", "SAFE_RESTART=BLOCKED")
    with sqlite3.connect(board) as db:
        assert db.execute("select body from task_comments").fetchone()[0] == "SAFE_RESTART=BLOCKED"


def test_once_live_controller_fails_closed_without_restart():
    import os
    env = os.environ.copy()
    env["HERMES_KANBAN_DB"] = "/home/fardochebot/.hermes/kanban.db"
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / "scripts" / "gateway-safe-restart.py"),
         "--task-id", "t_609a173d", "--once"],
        text=True, capture_output=True, env=env,
    )
    # Current fleet has gateway-owned workers; --once must inspect only.
    assert proc.returncode in (0, 2)
    assert "pending" in proc.stdout or "SAFE_RESTART=ERROR" in proc.stderr

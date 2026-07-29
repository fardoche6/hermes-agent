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


def test_true_idle_window_fails_closed_when_no_boards_are_discovered(monkeypatch):
    monkeypatch.setattr(MOD, "gateway_worker_descendants", lambda *_args: [])
    assert MOD.true_idle_window([], "restart", gateway_pids=[42]) is False


def test_proc_stat_parser_handles_spaces_and_parentheses_in_comm():
    raw = "123 (gateway helper (x y)) S 42 0 0 0 0 0 0 0 0 0 0"
    assert MOD._proc_stat_state_ppid(raw) == ("S", 42)


def test_gateway_worker_descendants_detects_spaced_process_name(tmp_path):
    proc = tmp_path / "proc"
    (proc / "100").mkdir(parents=True)
    (proc / "101").mkdir()
    (proc / "100" / "stat").write_text("100 (gateway) S 1 0 0")
    (proc / "101" / "stat").write_text("101 (worker with spaces) S 100 0 0")
    assert MOD.gateway_worker_descendants([100], proc_root=proc) == [101]


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


def test_once_inspection_never_restarts(monkeypatch, tmp_path, capsys):
    board = tmp_path / "kanban.db"
    make_board(board)
    monkeypatch.setattr(MOD, "pending_state", lambda _services: {
        "demo.service": {"pid": 0, "newer": []}
    })
    monkeypatch.setattr(MOD, "gateway_worker_descendants", lambda *_args: [])
    monkeypatch.setattr(MOD, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError(args)))
    result = MOD.main([
        "--task-id", "restart", "--board", str(board),
        "--services", "demo.service", "--once",
    ])
    assert result == 0
    assert '"pending"' in capsys.readouterr().out

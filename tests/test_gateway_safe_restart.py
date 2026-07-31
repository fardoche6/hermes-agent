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


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "agent").mkdir(parents=True)
    (repo / "agent" / "main.py").write_text("print('v1')\n")
    for argv in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "init"],
    ):
        subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)
    return repo


def head_of(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_code_change_gate_passes_on_clean_expected_commit(tmp_path, capsys):
    repo = make_repo(tmp_path)
    gate = MOD.code_change_gate(repo, head_of(repo), MOD.RUNTIME_MATERIAL_PATHS)
    assert gate["ok"] is True
    out = capsys.readouterr().out
    assert "RESTART-PROOF: PASS gate=head_matches_expected" in out
    assert "RESTART-PROOF: PASS gate=runtime_source_clean" in out


def test_code_change_gate_blocks_dirty_runtime_source(tmp_path, capsys):
    repo = make_repo(tmp_path)
    (repo / "agent" / "main.py").write_text("print('dirty')\n")
    gate = MOD.code_change_gate(repo, head_of(repo), MOD.RUNTIME_MATERIAL_PATHS)
    assert gate["ok"] is False
    assert gate["dirty"] == ["agent/main.py"]
    assert "RESTART-PROOF: FAIL gate=runtime_source_clean" in capsys.readouterr().out


def test_code_change_gate_ignores_non_runtime_dirt(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "README.md").write_text("docs churn\n")
    assert MOD.code_change_gate(repo, head_of(repo), MOD.RUNTIME_MATERIAL_PATHS)["ok"] is True


def test_code_change_gate_blocks_unexpected_head(tmp_path, capsys):
    repo = make_repo(tmp_path)
    reviewed = head_of(repo)
    (repo / "agent" / "main.py").write_text("print('v2')\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "v2"], check=True, capture_output=True)
    gate = MOD.code_change_gate(repo, reviewed, MOD.RUNTIME_MATERIAL_PATHS)
    assert gate["ok"] is False
    assert "RESTART-PROOF: FAIL gate=head_matches_expected" in capsys.readouterr().out


def test_code_change_gate_blocks_unknown_or_missing_commit(tmp_path, capsys):
    repo = make_repo(tmp_path)
    assert MOD.code_change_gate(repo, None, MOD.RUNTIME_MATERIAL_PATHS)["ok"] is False
    assert MOD.code_change_gate(repo, "0" * 40, MOD.RUNTIME_MATERIAL_PATHS)["ok"] is False
    assert capsys.readouterr().out.count("RESTART-PROOF: FAIL gate=expected_commit") == 2


def test_verify_replacement_requires_new_pid_health_and_commit(tmp_path, capsys):
    repo = make_repo(tmp_path)
    expected = head_of(repo)
    after = {"demo.service": {"ActiveState": "active", "MainPID": "222"}}
    result = MOD.verify_replacement({"demo.service": 111}, after, repo, expected,
                                    health_command=f"{sys.executable} -c pass")
    assert result["ok"] is True
    out = capsys.readouterr().out
    for gate in ("pid_replaced", "services_active", "runtime_healthy", "running_commit_matches"):
        assert f"RESTART-PROOF: PASS gate={gate}" in out
    assert "source=override" in out


def test_verify_replacement_fails_when_pid_unchanged(tmp_path, capsys):
    repo = make_repo(tmp_path)
    after = {"demo.service": {"ActiveState": "active", "MainPID": "111"}}
    result = MOD.verify_replacement({"demo.service": 111}, after, repo, head_of(repo),
                                    health_command=f"{sys.executable} -c pass")
    assert result["ok"] is False
    assert "RESTART-PROOF: FAIL gate=pid_replaced" in capsys.readouterr().out


def test_verify_replacement_fails_on_unhealthy_runtime(tmp_path, capsys):
    repo = make_repo(tmp_path)
    after = {"demo.service": {"ActiveState": "active", "MainPID": "222"}}
    result = MOD.verify_replacement({"demo.service": 111}, after, repo, head_of(repo),
                                    health_command=f"{sys.executable} -c 'raise SystemExit(1)'")
    assert result["ok"] is False
    assert "RESTART-PROOF: FAIL gate=runtime_healthy" in capsys.readouterr().out


def test_verify_replacement_fails_on_wrong_running_commit(tmp_path, capsys):
    repo = make_repo(tmp_path)
    after = {"demo.service": {"ActiveState": "active", "MainPID": "222"}}
    result = MOD.verify_replacement({"demo.service": 111}, after, repo, "0" * 40,
                                    health_command=f"{sys.executable} -c pass")
    assert result["ok"] is False
    assert "RESTART-PROOF: FAIL gate=running_commit_matches" in capsys.readouterr().out


def test_default_health_command_fails_without_live_gateway(tmp_path, capsys):
    repo = make_repo(tmp_path)
    after = {"demo.service": {"ActiveState": "active", "MainPID": "222"}}
    # Default health probe runs without health_command parameter.
    # Reverting default health probe to a no-op PASS would make this test fail.
    result = MOD.verify_replacement({"demo.service": 111}, after, repo, head_of(repo))
    assert result["ok"] is False
    out = capsys.readouterr().out
    assert "RESTART-PROOF: FAIL gate=runtime_healthy" in out
    assert "source=default" in out


def test_health_ok_handles_timeout_and_invalid_commands():
    assert MOD.health_ok("", timeout=1) is False
    assert MOD.health_ok("   ", timeout=1) is False
    assert MOD.health_ok("non_existent_command_123456789", timeout=1) is False
    assert MOD.health_ok(f"{sys.executable} -c 'import time; time.sleep(5)'", timeout=0.1) is False



def test_code_change_reason_refuses_restart_on_dirty_source(monkeypatch, tmp_path, capsys):
    board = tmp_path / "kanban.db"
    make_board(board)
    repo = make_repo(tmp_path)
    (repo / "agent" / "main.py").write_text("print('dirty')\n")
    monkeypatch.setenv("SAFE_RESTART_NO_DETACH", "1")
    monkeypatch.setattr(MOD, "wait_for_idle", lambda *a, **k: (_ for _ in ()).throw(AssertionError("waited")))
    result = MOD.main([
        "--task-id", "restart", "--board", str(board), "--services", "demo.service",
        "--reason", "code-change", "--expected-commit", head_of(repo), "--repo", str(repo),
    ])
    assert result == 5
    assert "RESTART-PROOF: FAIL gate=runtime_source_clean" in capsys.readouterr().out
    with sqlite3.connect(board) as db:
        assert "SAFE_RESTART=FAIL" in db.execute("select body from task_comments").fetchone()[0]


def test_code_change_reason_still_blocks_on_busy_board(monkeypatch, tmp_path):
    """The idle guard is unchanged: a reviewed commit never bypasses live work."""
    board = tmp_path / "kanban.db"
    make_board(board)
    with sqlite3.connect(board) as db:
        db.execute("INSERT INTO tasks VALUES ('other', 'running', NULL, NULL)")
    repo = make_repo(tmp_path)
    monkeypatch.setattr(MOD, "pending_state", lambda _services: {
        "demo.service": {"pid": 7, "newer": []}
    })
    monkeypatch.setenv("SAFE_RESTART_NO_DETACH", "1")
    monkeypatch.setattr(MOD, "gateway_worker_descendants", lambda *_args: [])
    monkeypatch.setattr(MOD, "notify_owner", lambda *a, **k: "marker")
    monkeypatch.setattr(MOD.time, "sleep", lambda _s: None)
    try:
        MOD.main([
            "--task-id", "restart", "--board", str(board), "--services", "demo.service",
            "--reason", "code-change", "--expected-commit", head_of(repo), "--repo", str(repo),
            "--max-wait", "0",
        ])
        raise AssertionError("expected SystemExit(3)")
    except SystemExit as exc:
        assert exc.code == 3


def test_code_change_reason_detaches_by_default(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(MOD, "run", lambda *args, **kwargs: calls.append(args) or "")
    monkeypatch.setattr(MOD, "perform", lambda _args: (_ for _ in ()).throw(AssertionError("ran inline")))
    repo = make_repo(tmp_path)
    assert MOD.main([
        "--task-id", "restart", "--reason", "code-change",
        "--expected-commit", head_of(repo), "--repo", str(repo),
    ]) == 0
    assert calls and calls[0][0] == "systemd-run"


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

#!/usr/bin/env python3
"""Fail-closed, detached restart for a Hermes gateway fleet.

The controller never restarts a service while Kanban work or gateway-owned
worker descendants are live.  It is intended to be launched by a cron/job
outside the gateway process; the actual restart runs in a detached user unit.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, cast

DEFAULT_SERVICES = (
    "hermes-gateway-orchestrator.service",
    "hermes-gateway-researcher-grok.service",
    "hermes-gateway-secretary.service",
)
MAX_WAIT_SECONDS = 24 * 60 * 60
_WORKER_MARKERS = ("hermes ", " hermes", "claude", "codex", "gemini", "spawn_agent", "tmux")


def run(*argv: str, check: bool = True) -> str:
    result = subprocess.run(argv, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(argv)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def unit_properties(unit: str) -> dict[str, str]:
    output = run(
        "systemctl", "--user", "show", unit,
        "-p", "ActiveState", "-p", "MainPID",
        "-p", "ExecMainStartTimestamp",
        "-p", "FragmentPath", "-p", "DropInPaths",
    )
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def config_files(props: dict[str, str]) -> list[Path]:
    paths: list[Path] = []
    if props.get("FragmentPath"):
        paths.append(Path(props["FragmentPath"]))
    paths.extend(Path(value) for value in props.get("DropInPaths", "").split() if value.startswith("/"))
    return [path for path in paths if path.is_file()]


def pending_state(services: Iterable[str]) -> dict[str, dict]:
    result = {}
    for service in services:
        props = unit_properties(service)
        started_us = _parse_realtime_usec(props.get("ExecMainStartTimestamp", ""))
        files = config_files(props)
        newer = [str(path) for path in files if path.stat().st_mtime_ns // 1000 > started_us]
        result[service] = {
            "active": props.get("ActiveState"),
            "pid": int(props.get("MainPID", "0") or 0),
            "start_usec": started_us,
            "files": [str(path) for path in files],
            "newer": newer,
        }
    return result


def _parse_realtime_usec(raw: str) -> int:
    # Unknown formats fail closed by returning zero (which marks all files newer).
    import datetime
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S %z"):
        try:
            return int(datetime.datetime.strptime(raw.strip(), fmt).timestamp() * 1_000_000)
        except ValueError:
            pass
    return 0


def board_paths(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pinned:
        return [Path(pinned).expanduser()]
    root = Path(os.environ.get("HERMES_KANBAN_HOME", Path.home() / ".hermes"))
    paths = [root / "kanban.db"]
    paths.extend(root.glob("kanban/boards/*/kanban.db"))
    return list(dict.fromkeys(path for path in paths if path.is_file()))


def idle_state(databases: Iterable[Path], task_id: str) -> dict:
    busy: list[dict] = []
    runs: list[dict] = []
    for database in databases:
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                busy.extend(dict(row) for row in conn.execute(
                    "SELECT id,status,current_run_id,worker_pid FROM tasks "
                    "WHERE status IN ('running','claimed') AND id != ?", (task_id,)))
                if _table_exists(conn, "task_runs"):
                    runs.extend(dict(row) for row in conn.execute(
                        "SELECT task_id,worker_pid,status FROM task_runs "
                        "WHERE status='running' AND task_id != ?", (task_id,)))
        except sqlite3.Error as exc:
            raise RuntimeError(f"cannot read Kanban board {database}: {exc}") from exc
    live_runs = [row for row in runs if _pid_alive(row.get("worker_pid"))]
    return {"busy_tasks": busy, "running_runs": runs, "live_worker_runs": live_runs}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _pid_alive(pid: object) -> bool:
    try:
        return int(pid or 0) > 0 and Path(f"/proc/{int(pid)}").exists()
    except (TypeError, ValueError):
        return False


def gateway_worker_descendants(gateway_pids: Iterable[int], exclude: set[int] | None = None) -> list[int]:
    excluded = exclude or set()
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            stat = (entry / "stat").read_text().split()
            children.setdefault(int(cast(str, stat[3])), []).append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    result: list[int] = []
    pending = list(gateway_pids)
    while pending:
        parent = pending.pop()
        for pid in children.get(parent, []):
            if pid in excluded:
                continue
            pending.append(pid)
            try:
                cmdline = (Path(f"/proc/{pid}") / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").lower()
            except OSError:
                continue
            if any(marker in cmdline for marker in _WORKER_MARKERS):
                result.append(pid)
    return sorted(set(result))


def append_comment(databases: Iterable[Path], task_id: str, body: str) -> None:
    now = int(time.time())
    for database in databases:
        with sqlite3.connect(database) as conn:
            conn.execute(
                "INSERT INTO task_comments(task_id,author,body,created_at) VALUES(?,?,?,?)",
                (task_id, "gateway-safe-restart", body, now),
            )


def notify_owner(reason: str, *, command: str | None, hermes_home: Path) -> str:
    payload = json.dumps({"event": "gateway_safe_restart", "reason": reason})
    if command:
        argv = shlex.split(command) + [payload]
        subprocess.run(argv, check=True, text=True, capture_output=True)
        return "command"
    marker = hermes_home / ".restart_pending.json"
    marker.write_text(payload + "\n", encoding="utf-8")
    return str(marker)


def wait_for_idle(args: argparse.Namespace, services: tuple[str, ...], databases: list[Path]) -> dict:
    deadline = time.monotonic() + args.max_wait
    while True:
        pending = pending_state(services)
        idle = idle_state(databases, args.task_id)
        workers = gateway_worker_descendants(
            [state["pid"] for state in pending.values() if state["pid"]],
            {os.getpid()},
        )
        snapshot = {"pending": pending, "idle": idle, "gateway_worker_children": workers}
        if any(state["newer"] for state in pending.values()) and not idle["busy_tasks"] and not idle["live_worker_runs"] and not workers:
            return snapshot
        if args.once:
            print(json.dumps(snapshot, indent=2, sort_keys=True))
            raise SystemExit(2)
        if time.monotonic() >= deadline:
            reason = "idle window not observed within 24h; restart skipped"
            channel = notify_owner(reason, command=args.notify_command, hermes_home=args.hermes_home)
            append_comment(databases, args.task_id, f"SAFE_RESTART=BLOCKED\nreason={reason}\nnotify={channel}\n{json.dumps(snapshot, sort_keys=True)}")
            raise SystemExit(3)
        time.sleep(args.poll)


def perform(args: argparse.Namespace) -> int:
    services = tuple(filter(None, args.services.split(",")))
    databases = board_paths(args.board)
    snapshot = wait_for_idle(args, services, databases)
    run("systemctl", "--user", "restart", *services)
    deadline = time.monotonic() + args.verify_timeout
    states = {}
    while time.monotonic() < deadline:
        states = {service: unit_properties(service) for service in services}
        if all(state.get("ActiveState") == "active" and int(state.get("MainPID", "0") or 0) > 0 for state in states.values()):
            break
        time.sleep(1)
    else:
        raise RuntimeError("gateway services did not become active")
    env_ok = True
    if args.verify_env:
        key, sep, expected = args.verify_env.partition("=")
        env_ok = bool(sep and all(f"{key}={expected}".encode() in (Path(f"/proc/{int(state['MainPID'])}/environ").read_bytes().split(b"\0")) for state in states.values()))
    body = (
        "SAFE_RESTART=" + ("PASS" if env_ok else "FAIL") + "\n"
        f"services={','.join(services)}\n"
        "pending_config=PASS\nidle_guard=PASS (zero running/claimed cards, zero live worker children)\n"
        "active=PASS\n"
        f"env_verification={'PASS' if env_ok else 'FAIL'}\n"
        + json.dumps({"before": snapshot, "after": states}, sort_keys=True)
    )
    append_comment(databases, args.task_id, body)
    return 0 if env_ok else 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--board")
    parser.add_argument("--services", default=','.join(DEFAULT_SERVICES))
    parser.add_argument("--max-wait", type=int, default=MAX_WAIT_SECONDS)
    parser.add_argument("--poll", type=int, default=30)
    parser.add_argument("--verify-timeout", type=int, default=90)
    parser.add_argument("--verify-env")
    parser.add_argument("--notify-command", default=os.environ.get("HERMES_SAFE_RESTART_NOTIFY_COMMAND"))
    parser.add_argument("--hermes-home", type=Path, default=Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--detached-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.once and not args.detached_child and os.environ.get("SAFE_RESTART_NO_DETACH") != "1":
        unit = "hermes-safe-restart-" + args.task_id.replace("_", "-")
        cmd = ["systemd-run", "--user", "--unit=" + unit, "--collect", sys.executable, __file__, *sys.argv[1:], "--detached-child"]
        run(*cmd)
        return 0
    return perform(args)


if __name__ == "__main__":
    raise SystemExit(main())

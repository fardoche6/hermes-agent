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
from typing import Iterable

DEFAULT_SERVICES = (
    "hermes-gateway-orchestrator.service",
    "hermes-gateway-researcher-grok.service",
    "hermes-gateway-secretary.service",
)
MAX_WAIT_SECONDS = 24 * 60 * 60
# Paths whose content is loaded into the running gateway process.  A code-change
# restart is only allowed when every one of these is clean and at the reviewed
# commit; docs/website churn must never be able to unblock an activation.
RUNTIME_MATERIAL_PATHS = ("agent", "hermes_cli", "tui_gateway", "plugins", "scripts")


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


def proof(gate: str, ok: bool, detail: str = "") -> str:
    """Emit one machine-readable gate result and return the line."""
    line = f"RESTART-PROOF: {'PASS' if ok else 'FAIL'} gate={gate}" + (f" {detail}" if detail else "")
    print(line, flush=True)
    return line


def git_head(repo: Path) -> str:
    return run("git", "-C", str(repo), "rev-parse", "HEAD")


def resolve_commit(repo: Path, ref: str) -> str | None:
    try:
        return run("git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}")
    except RuntimeError:
        return None


def runtime_dirty_paths(repo: Path, paths: Iterable[str]) -> list[str]:
    """Return porcelain entries for runtime-material paths (including untracked)."""
    output = run("git", "-C", str(repo), "status", "--porcelain", "--", *paths)
    # run() strips the output, so the porcelain XY prefix is not a fixed width
    # on the first line; take the path as the last whitespace-delimited field.
    return [line.strip().split(maxsplit=1)[-1] for line in output.splitlines() if line.strip()]


def code_change_gate(repo: Path, expected_commit: str | None, paths: Iterable[str]) -> dict:
    """Fail-closed identity gate for activating an exact reviewed commit.

    No timestamps, no file mtimes, no drop-in probing: the only question is
    whether the checkout is exactly the reviewed commit with a clean
    runtime-material tree.
    """
    paths = list(paths)
    lines: list[str] = []
    if not expected_commit:
        return {"ok": False, "expected": None, "head": None, "dirty": [],
                "proof": [proof("expected_commit", False, "reason=missing --expected-commit")]}
    expected = resolve_commit(repo, expected_commit)
    if not expected:
        return {"ok": False, "expected": None, "head": None, "dirty": [],
                "proof": [proof("expected_commit", False, f"reason=unknown-commit ref={expected_commit}")]}
    lines.append(proof("expected_commit", True, f"expected={expected}"))
    head = git_head(repo)
    head_ok = head == expected
    lines.append(proof("head_matches_expected", head_ok, f"head={head} expected={expected}"))
    dirty = runtime_dirty_paths(repo, paths)
    lines.append(proof("runtime_source_clean", not dirty,
                       f"paths={','.join(paths)} dirty={','.join(dirty) or 'none'}"))
    return {"ok": head_ok and not dirty, "expected": expected, "head": head, "dirty": dirty, "proof": lines}


def health_ok(command: str | None) -> bool:
    if not command:
        return True
    return subprocess.run(shlex.split(command), text=True, capture_output=True).returncode == 0


def verify_replacement(
    before: dict[str, int],
    after: dict[str, dict[str, str]],
    repo: Path,
    expected: str,
    health_command: str | None = None,
) -> dict:
    """Verify the fleet really is a new process tree running ``expected``."""
    lines: list[str] = []
    replaced = []
    for service, state in after.items():
        pid = int(state.get("MainPID", "0") or 0)
        old = int(before.get(service, 0) or 0)
        ok = pid > 0 and pid != old
        replaced.append(ok)
        lines.append(proof("pid_replaced", ok, f"service={service} old_pid={old} new_pid={pid}"))
    active = all(state.get("ActiveState") == "active" for state in after.values()) and bool(after)
    lines.append(proof("services_active", active, f"services={','.join(sorted(after))}"))
    healthy = health_ok(health_command)
    lines.append(proof("runtime_healthy", healthy, f"command={health_command or 'none'}"))
    head = git_head(repo)
    commit_ok = head == expected
    lines.append(proof("running_commit_matches", commit_ok, f"head={head} expected={expected}"))
    ok = all(replaced) and bool(replaced) and active and healthy and commit_ok
    return {"ok": ok, "proof": lines}


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


def true_idle_window(
    databases: Iterable[Path],
    task_id: str,
    gateway_pids: Iterable[int],
    exclude: set[int] | None = None,
) -> bool:
    """Return whether it is safe to restart the gateway fleet now.

    The restart task itself is intentionally ignored, but every other running
    or claimed card blocks.  The process check is independent of task state:
    a worker can outlive its task-row update, so any live gateway descendant
    also blocks.  This is deliberately a boolean fail-closed boundary for
    callers that must decide whether to restart, rather than a diagnostic
    snapshot that could be interpreted loosely.
    """
    databases = list(databases)
    if not databases:
        return False
    state = idle_state(databases, task_id)
    workers = gateway_worker_descendants(gateway_pids, exclude or {os.getpid()})
    return not state["busy_tasks"] and not state["live_worker_runs"] and not workers


def gateway_worker_descendants(
    gateway_pids: Iterable[int],
    exclude: set[int] | None = None,
    proc_root: Path | None = None,
) -> list[int]:
    excluded = exclude or set()
    root = proc_root or Path("/proc")
    children: dict[int, list[int]] = {}
    for entry in root.glob("[0-9]*"):
        try:
            state, ppid = _proc_stat_state_ppid((entry / "stat").read_text())
            if state != "Z":
                children.setdefault(ppid, []).append(int(entry.name))
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
                # Every non-zombie descendant belongs to the gateway service's
                # process tree.  Do not rely on command-line markers: watchdogs
                # and adapter helpers can use arbitrary executables and would
                # otherwise create a false idle window.
                state, _ppid = _proc_stat_state_ppid((root / str(pid) / "stat").read_text())
            except (OSError, ValueError, IndexError):
                continue
            if state != "Z":
                result.append(pid)
    return sorted(set(result))


def _proc_stat_state_ppid(raw: str) -> tuple[str, int]:
    """Parse state and PPID without splitting the process name."""
    close = raw.rfind(")")
    if close < 0:
        raise ValueError("invalid /proc stat: missing process-name terminator")
    fields = raw[close + 1 :].split()
    if len(fields) < 2:
        raise ValueError("invalid /proc stat: missing state or PPID")
    return fields[0], int(fields[1])


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
        if args.once:
            print(json.dumps(snapshot, indent=2, sort_keys=True))
            return snapshot
        # A code-change activation is triggered by the reviewed commit itself,
        # never by config-file timestamps; the idle guard below is unchanged.
        triggered = args.reason == "code-change" or any(state["newer"] for state in pending.values())
        if triggered and true_idle_window(
            databases,
            args.task_id,
            [state["pid"] for state in pending.values() if state["pid"]],
            {os.getpid()},
        ):
            return snapshot

        if time.monotonic() >= deadline:
            reason = "idle window not observed within 24h; restart skipped"
            channel = notify_owner(reason, command=args.notify_command, hermes_home=args.hermes_home)
            append_comment(databases, args.task_id, f"SAFE_RESTART=BLOCKED\nreason={reason}\nnotify={channel}\n{json.dumps(snapshot, sort_keys=True)}")
            raise SystemExit(3)
        time.sleep(args.poll)


def perform(args: argparse.Namespace) -> int:
    services = tuple(filter(None, args.services.split(",")))
    databases = board_paths(args.board)
    gate = None
    if args.reason == "code-change":
        # Gate before the idle wait: never park a 24h wait on a checkout that
        # can already be proven wrong.
        gate = code_change_gate(args.repo, args.expected_commit, args.runtime_paths.split(","))
        if not gate["ok"] and not args.once:
            append_comment(databases, args.task_id,
                           "SAFE_RESTART=FAIL\nreason=code-change gate\n" + "\n".join(gate["proof"]))
            return 5
    snapshot = wait_for_idle(args, services, databases)
    if args.once:
        return 0
    proof("idle_guard", True, "running_claimed_cards=0 live_worker_children=0")
    before_pids = {service: int(state.get("pid", 0) or 0) for service, state in snapshot["pending"].items()}
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
    identity = None
    if gate is not None:
        identity = verify_replacement(before_pids, states, args.repo, gate["expected"], args.health_command)
    ok = env_ok and (identity is None or identity["ok"])
    proof_lines = (gate["proof"] if gate else []) + (identity["proof"] if identity else [])
    proof_lines.append(proof("restart", ok, f"services={','.join(services)} reason={args.reason}"))
    body = (
        "SAFE_RESTART=" + ("PASS" if ok else "FAIL") + "\n"
        + "\n".join(proof_lines) + "\n"
        f"services={','.join(services)}\n"
        "pending_config=PASS\nidle_guard=PASS (zero running/claimed cards, zero live worker children)\n"
        "active=PASS\n"
        f"env_verification={'PASS' if env_ok else 'FAIL'}\n"
        + json.dumps({"before": snapshot, "after": states}, sort_keys=True)
    )
    append_comment(databases, args.task_id, body)
    if not env_ok:
        return 4
    return 0 if ok else 6


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--board")
    parser.add_argument("--services", default=','.join(DEFAULT_SERVICES))
    parser.add_argument("--max-wait", type=int, default=MAX_WAIT_SECONDS)
    parser.add_argument("--poll", type=int, default=30)
    parser.add_argument("--verify-timeout", type=int, default=90)
    parser.add_argument("--verify-env")
    parser.add_argument("--reason", choices=("config", "code-change"), default="config")
    parser.add_argument("--expected-commit", help="exact reviewed commit to activate (--reason code-change)")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--runtime-paths", default=",".join(RUNTIME_MATERIAL_PATHS))
    parser.add_argument("--health-command", help="post-restart health probe; non-zero exit fails the gate")
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

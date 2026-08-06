"""Regression coverage for identity-bound reviewer retirement and recovery."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import signal
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _review_card(conn):
    host = kb._claimer_id().split(":", 1)[0]
    task_id = kb.create_task(conn, title="identity-bound review", assignee="programmer")
    assert kb.claim_task(conn, task_id, claimer=f"{host}:implementation")
    assert kb.submit_task_for_review(
        conn, task_id, "code-reviewer", trusted_operator=True,
    )
    review = kb.claim_review_task(conn, task_id, claimer=f"{host}:review")
    assert review is not None
    return task_id, review, host


def _bind_reviewer_process(conn, task_id, review, process):
    handle = kb._capture_process_handle(process.pid)
    assert handle is not None
    identity = handle.identity
    authority = kb._latest_reviewer_authority(conn, task_id)
    assert authority is not None
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid=?, worker_boot_id=?, "
            "worker_starttime=? WHERE id=? AND current_run_id=?",
            (
                process.pid,
                identity.boot_id,
                identity.starttime,
                task_id,
                review.current_run_id,
            ),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid=?, worker_boot_id=?, "
            "worker_starttime=? WHERE id=?",
            (
                process.pid,
                identity.boot_id,
                identity.starttime,
                review.current_run_id,
            ),
        )
        conn.execute(
            "INSERT INTO task_launch_gates "
            "(task_id, run_id, claim_lock, assignee, authority_id, gate_token, "
            "gate_pid, gate_boot_id, gate_starttime, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'released', ?)",
            (
                task_id,
                review.current_run_id,
                review.claim_lock,
                review.assignee,
                authority[0],
                f"identity-test-{process.pid}",
                process.pid,
                identity.boot_id,
                identity.starttime,
                int(time.time()),
            ),
        )
    return handle, identity


def _record_approval(conn, task_id, review):
    result = kb.approve_review(
        conn,
        task_id,
        reviewer=review.assignee,
        summary="identity-bound approval",
        head_sha="a" * 40,
        expected_claim=review.claim_lock,
        expected_run_id=review.current_run_id,
    )
    assert result is not None
    assert result.status == "review"


def test_spawn_identity_contains_boot_id_starttime_and_pidfd(kanban_home):
    process = subprocess.Popen(["sleep", "30"])
    try:
        handle = kb._capture_process_handle(process.pid)
        assert handle is not None
        assert handle.pid == process.pid
        assert handle.pidfd is not None
        assert handle.identity.boot_id
        assert handle.identity.starttime.isdigit()
        assert kb._probe_bound_process(process.pid, handle.identity).status == "live"
    finally:
        process.terminate()
        process.wait(timeout=5)
        handle.close() if "handle" in locals() else None


@pytest.mark.parametrize(
    "machine",
    [
        "x86_64",
        "aarch64",
        "arm64",
        "ppc64le",
        "s390x",
        "riscv64",
        "loongarch64",
    ],
)
def test_pidfd_send_signal_libc_fallback_uses_generic_abi_map(monkeypatch, machine):
    calls = []

    class FakeLibc:
        def syscall(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(kb.sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(kb.signal, "pidfd_send_signal", None, raising=False)
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: FakeLibc())

    kb._pidfd_send_signal(17, signal.SIGTERM)

    assert calls == [(424, 17, signal.SIGTERM, 0, 0)]


def test_pidfd_send_signal_libc_fallback_rejects_unknown_architecture(monkeypatch):
    called = False

    def unexpected_cdll(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("unknown architectures must fail before libc")

    monkeypatch.setattr(kb.sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "unknown-arch")
    monkeypatch.setattr(kb.signal, "pidfd_send_signal", None, raising=False)
    monkeypatch.setattr(ctypes, "CDLL", unexpected_cdll)

    with pytest.raises(OSError, match="pidfd signalling is unavailable"):
        kb._pidfd_send_signal(17, signal.SIGTERM)
    assert called is False


def test_identity_mismatch_proves_retirement_without_signaling_reused_pid(kanban_home):
    process = subprocess.Popen(["sleep", "30"])
    try:
        handle = kb._capture_process_handle(process.pid)
        assert handle is not None
        wrong_identity = kb._ProcessIdentity(
            boot_id=handle.identity.boot_id,
            starttime=str(int(handle.identity.starttime) + 1),
        )
        signals = []
        result = kb._terminate_reclaimed_worker(
            process.pid,
            f"{kb._claimer_id().split(':', 1)[0]}:identity-test",
            process_identity=wrong_identity,
            signal_fn=lambda pid, sig: signals.append((pid, sig)),
        )
        assert result["identity_status"] == "retired"
        assert result["termination_attempted"] is False
        assert result["terminated"] is True
        assert signals == []
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_live_identity_is_required_before_signal(kanban_home):
    process = subprocess.Popen(["sleep", "30"])
    try:
        handle = kb._capture_process_handle(process.pid)
        assert handle is not None
        signals = []

        def signal_and_exit(pid, sig):
            signals.append((pid, sig))
            os.kill(pid, sig)

        result = kb._terminate_reclaimed_worker(
            process.pid,
            f"{kb._claimer_id().split(':', 1)[0]}:identity-test",
            process_identity=handle.identity,
            signal_fn=signal_and_exit,
        )
        assert result["identity_verified"] is True
        assert result["termination_attempted"] is True
        assert result["terminated"] is True
        assert signals and signals[0][0] == process.pid
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        handle.close() if "handle" in locals() else None


def test_missing_identity_fails_closed_without_signal(kanban_home):
    process = subprocess.Popen(["sleep", "30"])
    try:
        signals = []
        result = kb._terminate_reclaimed_worker(
            process.pid,
            f"{kb._claimer_id().split(':', 1)[0]}:identity-test",
            signal_fn=lambda pid, sig: signals.append((pid, sig)),
        )
        assert result["identity_status"] == "ambiguous"
        assert result["termination_attempted"] is False
        assert result["terminated"] is False
        assert signals == []
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_pending_approval_finalizes_once_after_dispatcher_restart(kanban_home):
    process = subprocess.Popen(["sleep", "30"])
    try:
        with kb.connect() as first:
            task_id, review, _host = _review_card(first)
            handle, _identity = _bind_reviewer_process(first, task_id, review, process)
            _record_approval(first, task_id, review)

        process.terminate()
        process.wait(timeout=5)
        handle.close()

        with kb.connect() as restarted:
            kb._reap_pending_review_decisions(restarted)
            current = kb.get_task(restarted, task_id)
            assert current is not None
            assert current.status == "ready"
            assert current.recovery_required is False
            assert restarted.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? "
                "AND kind='review_decision_exit_proven'",
                (task_id,),
            ).fetchone()[0] == 1
            assert restarted.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? "
                "AND kind='review_decision_finalized'",
                (task_id,),
            ).fetchone()[0] == 1
            kb._reap_pending_review_decisions(restarted)
            assert restarted.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? "
                "AND kind='review_decision_finalized'",
                (task_id,),
            ).fetchone()[0] == 1
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_pending_live_reviewer_stays_fenced_after_restart(kanban_home):
    process = subprocess.Popen(["sleep", "30"])
    try:
        with kb.connect() as first:
            task_id, review, _host = _review_card(first)
            handle, _identity = _bind_reviewer_process(first, task_id, review, process)
            _record_approval(first, task_id, review)

        with kb.connect() as restarted:
            kb._reap_pending_review_decisions(restarted)
            current = kb.get_task(restarted, task_id)
            assert current is not None
            assert current.status == "review"
            assert current.recovery_required is False
            assert restarted.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? "
                "AND kind='review_decision_exit_proven'",
                (task_id,),
            ).fetchone()[0] == 0
    finally:
        process.terminate()
        process.wait(timeout=5)
        handle.close() if "handle" in locals() else None


def test_pending_ambiguous_probe_requires_recovery(kanban_home, monkeypatch):
    process = subprocess.Popen(["sleep", "30"])
    try:
        with kb.connect() as first:
            task_id, review, _host = _review_card(first)
            handle, _identity = _bind_reviewer_process(first, task_id, review, process)
            _record_approval(first, task_id, review)

        def unavailable_pidfd(_pid, _flags=0):
            raise OSError(errno.EACCES, "pidfd unavailable")

        monkeypatch.setattr(kb, "_pidfd_open", unavailable_pidfd)
        with kb.connect() as restarted:
            kb._reap_pending_review_decisions(restarted)
            current = kb.get_task(restarted, task_id)
            assert current is not None
            assert current.status == "review"
            assert current.recovery_required is True
            assert restarted.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? "
                "AND kind='review_decision_exit_proven'",
                (task_id,),
            ).fetchone()[0] == 0
            assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)
        handle.close() if "handle" in locals() else None


def test_pending_zombie_is_durable_exit_proof(kanban_home):
    process = subprocess.Popen(["sh", "-c", "sleep 0.2; exit 7"])
    try:
        handle = kb._capture_process_handle(process.pid)
        assert handle is not None
        deadline = time.time() + 5
        while time.time() < deadline:
            snapshot, state, availability = kb._read_process_snapshot(process.pid)
            if availability == "present" and state == "Z":
                break
            time.sleep(0.01)
        assert state == "Z"
        with kb.connect() as first:
            task_id, review, _host = _review_card(first)
            _bind_reviewer_process(first, task_id, review, process)
            _record_approval(first, task_id, review)
        with kb.connect() as restarted:
            kb._reap_pending_review_decisions(restarted)
            current = kb.get_task(restarted, task_id)
            assert current is not None
            assert current.status == "ready"
            proof = restarted.execute(
                "SELECT payload FROM task_events WHERE task_id=? "
                "AND kind='review_decision_exit_proven'",
                (task_id,),
            ).fetchone()
            assert proof is not None
            assert "pidfd" in proof["payload"]
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        handle.close() if handle is not None else None

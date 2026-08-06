"""Focused regression tests for the same-card reviewer control plane.

These tests intentionally exercise the durable DB boundary, dispatcher handoff,
and worker-facing tool registration. They reproduce the production failure on
current main: a claimed reviewer has no first-class terminal decision path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


HEAD_SHA = "855fd911d56e1c6185fda5995d8aff430d964191"
PR_URL = "https://github.com/example/repo/pull/283"


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


def _events(conn, task_id: str) -> list[str]:
    return [
        row["kind"]
        for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
            (task_id,),
        )
    ]


def _payload(conn, task_id: str, kind: str) -> dict:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    assert row is not None, f"missing {kind} event"
    return json.loads(row["payload"] or "{}")


def _review_card(conn, *, reviewer="code-reviewer", programmer="programmer"):
    host = kb._claimer_id().split(":", 1)[0]
    task_id = kb.create_task(conn, title="review lifecycle", assignee=programmer)
    assert kb.claim_task(conn, task_id, claimer=f"{host}:implementation")
    kb.add_comment(conn, task_id, "programmer", f"PR opened: {PR_URL}")
    assert kb.submit_task_for_review(
        conn, task_id, reviewer, trusted_operator=True,
    )
    review = kb.claim_review_task(conn, task_id, claimer=f"{host}:review")
    assert review is not None
    assert review.status == "review"
    assert review.claim_lock == f"{host}:review"
    assert review.current_run_id is not None
    return task_id, review, host


def _start_followup_review(conn, task_id: str, *, reviewer="code-reviewer", host):
    """Start a second implementation/review run on the same card."""
    implementation = kb.claim_task(
        conn, task_id, claimer=f"{host}:implementation-followup",
    )
    assert implementation is not None
    assert kb.submit_task_for_review(
        conn, task_id, reviewer, trusted_operator=True,
    ) is not None
    review = kb.claim_review_task(
        conn, task_id, claimer=f"{host}:review-followup",
    )
    assert review is not None
    return review


def _unclaimed_review_card(conn, *, reviewer="code-reviewer"):
    """Create a review generation that is ready for dispatcher claim."""
    task_id, review, host = _review_card(conn, reviewer=reviewer)
    assert kb.reclaim_task(conn, task_id, reason="prepare dispatcher race")
    current = kb.get_task(conn, task_id)
    assert current is not None
    assert current.status == "review"
    assert current.claim_lock is None
    assert current.current_run_id is None
    return task_id, host


def _terminal_decision(conn, task_id, review, *, kind, trusted):
    if kind == "request_changes":
        return kb.request_changes(
            conn, task_id, "programmer", reviewer=None if trusted else "code-reviewer",
            reason="test decision", expected_claim=None if trusted else review.claim_lock,
            expected_run_id=None if trusted else review.current_run_id,
            trusted_operator=trusted,
        )
    return kb.approve_review(
        conn, task_id, reviewer="code-reviewer", summary="test decision",
        head_sha=HEAD_SHA, expected_claim=None if trusted else review.claim_lock,
        expected_run_id=None if trusted else review.current_run_id,
        trusted_operator=trusted,
    )


def _snapshot(conn, task_id):
    """Full row snapshot of every table a decision could mutate.

    Complete rows (``SELECT *``) are captured so status, assignee, claim
    lock, expiration, worker pid, current_run_id, every run row and every
    event row + payload are all covered without asserting on schema shape.
    """
    def _rows(sql):
        return [tuple(row) for row in conn.execute(sql, (task_id,)).fetchall()]

    return {
        "tasks": _rows("SELECT * FROM tasks WHERE id=?"),
        "task_runs": _rows("SELECT * FROM task_runs WHERE task_id=? ORDER BY id"),
        "task_events": _rows(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY id"
        ),
    }


def _assert_no_mutation(conn, task_id, before, review):
    assert _snapshot(conn, task_id) == before
    current = kb.get_task(conn, task_id)
    assert current is not None and current.current_run_id == review.current_run_id


def _start_real_review_decision_child(conn, task_id, review, tmp_path, decision):
    """Attach a real child as the reviewer PID, then let it record one verdict."""
    ready = tmp_path / f"{decision}-ready"
    go = tmp_path / f"{decision}-go"
    recorded = tmp_path / f"{decision}-recorded"
    finish = tmp_path / f"{decision}-finish"
    script = tmp_path / f"{decision}-reviewer.py"
    script.write_text(
        "from pathlib import Path\n"
        "import os, time\n"
        "from hermes_cli import kanban_db as kb\n"
        "ready = Path(os.environ['READY'])\n"
        "go = Path(os.environ['GO'])\n"
        "recorded = Path(os.environ['RECORDED'])\n"
        "finish = Path(os.environ['FINISH'])\n"
        "ready.write_text('ready')\n"
        "while not go.exists(): time.sleep(0.01)\n"
        "conn = kb.connect(db_path=Path(os.environ['KDB']))\n"
        "task_id = os.environ['TASK']\n"
        "run_id = int(os.environ['RUN_ID'])\n"
        "claim = os.environ['CLAIM']\n"
        "if os.environ['DECISION'] == 'approve':\n"
        "    kb.approve_review(conn, task_id, reviewer=os.environ['REVIEWER'],\n"
        "        summary='subprocess approval', head_sha=os.environ['HEAD'],\n"
        "        expected_claim=claim, expected_run_id=run_id)\n"
        "else:\n"
        "    kb.request_changes(conn, task_id, os.environ['PROGRAMMER'],\n"
        "        reviewer=os.environ['REVIEWER'], reason='subprocess correction',\n"
        "        expected_claim=claim, expected_run_id=run_id)\n"
        "recorded.write_text('recorded')\n"
        "while not finish.exists(): time.sleep(0.01)\n"
        "conn.close()\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path.cwd()) + os.pathsep + env.get("PYTHONPATH", "")
    env.update({
        "KDB": str(kb.kanban_db_path()),
        "TASK": task_id,
        "RUN_ID": str(review.current_run_id),
        "CLAIM": str(review.claim_lock),
        "REVIEWER": str(review.assignee),
        "PROGRAMMER": "programmer",
        "HEAD": HEAD_SHA,
        "DECISION": decision,
        "READY": str(ready),
        "GO": str(go),
        "RECORDED": str(recorded),
        "FINISH": str(finish),
    })
    child = subprocess.Popen([sys.executable, str(script)], env=env, cwd=str(Path.cwd()))
    deadline = time.time() + 5
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    authority = kb._latest_reviewer_authority(conn, task_id)
    assert authority is not None
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid=? WHERE id=? AND current_run_id=? "
            "AND claim_lock=?",
            (child.pid, task_id, review.current_run_id, review.claim_lock),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid=? WHERE id=? AND task_id=? "
            "AND claim_lock=?",
            (child.pid, review.current_run_id, task_id, review.claim_lock),
        )
        conn.execute(
            "INSERT INTO task_launch_gates "
            "(task_id, run_id, claim_lock, assignee, authority_id, gate_token, "
            "gate_pid, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'released', ?)",
            (
                task_id, review.current_run_id, review.claim_lock, review.assignee,
                authority[0], f"test-token-{child.pid}", child.pid, int(time.time()),
            ),
        )
    attached = conn.execute(
        "SELECT worker_pid FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    assert attached is not None and attached["worker_pid"] == child.pid
    go.write_text("go")
    deadline = time.time() + 5
    while not recorded.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert recorded.exists()
    return child, finish


@pytest.mark.parametrize("decision", ["approve", "request_changes"])
def test_real_terminal_decision_waits_for_physical_reviewer_retirement(
    kanban_home, tmp_path, decision,
):
    """A durable verdict cannot clear the deciding run before its PID is reaped."""
    with kb.connect() as conn:
        task_id, review, _host = _review_card(conn)
        child, finish = _start_real_review_decision_child(
            conn, task_id, review, tmp_path, decision,
        )
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.current_run_id == review.current_run_id
        assert current.claim_lock == review.claim_lock
        assert current.worker_pid == child.pid
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
            (task_id, "review_approved" if decision == "approve" else "changes_requested"),
        ).fetchone()[0] == 1

        # The old implementation immediately exposed a ready successor here.
        assert kb.claim_task(conn, task_id, claimer="local:successor") is None
        assert kb.dispatch_once(conn).spawned == []
        fenced = kb.get_task(conn, task_id)
        assert fenced is not None and fenced.worker_pid == child.pid

        finish.write_text("finish")
        deadline = time.time() + 5
        while kb._pid_alive(child.pid) and time.time() < deadline:
            time.sleep(0.01)
        assert not kb._pid_alive(child.pid)
        kb.reap_worker_zombies()
        assert kb.detect_crashed_workers(conn) == []
        finalized = kb.get_task(conn, task_id)
        assert finalized is not None
        assert finalized.status == "ready"
        assert finalized.assignee == "programmer"
        assert finalized.worker_pid is None
        successor = kb.claim_task(conn, task_id, claimer="local:successor")
        assert successor is not None and successor.status == "running"


def test_pending_review_decision_replays_after_reaper_crash(kanban_home, tmp_path, monkeypatch):
    """A durable exit proof lets a later reaper finish an interrupted finalization."""
    with kb.connect() as conn:
        task_id, review, _host = _review_card(conn)
        child, finish = _start_real_review_decision_child(
            conn, task_id, review, tmp_path, "approve",
        )
        finish.write_text("finish")
        deadline = time.time() + 5
        while kb._pid_alive(child.pid) and time.time() < deadline:
            time.sleep(0.01)
        assert not kb._pid_alive(child.pid)
        kb.reap_worker_zombies()

        original = kb._finalize_pending_review_decision
        monkeypatch.setattr(
            kb,
            "_finalize_pending_review_decision",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("reaper crashed")),
        )
        with pytest.raises(RuntimeError, match="reaper crashed"):
            kb.detect_crashed_workers(conn)
        pending = kb.get_task(conn, task_id)
        assert pending is not None and pending.worker_pid == child.pid
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? "
            "AND kind='review_decision_exit_proven'",
            (task_id,),
        ).fetchone()[0] == 1

        monkeypatch.setattr(kb, "_finalize_pending_review_decision", original)
        assert kb.detect_crashed_workers(conn) == []
        finalized = kb.get_task(conn, task_id)
        assert finalized is not None and finalized.status == "ready"


@pytest.mark.parametrize("reclaimer", ["max_runtime", "stale_running"])
def test_pending_review_decision_stays_fenced_across_reclaimers(
    kanban_home, reclaimer, monkeypatch,
):
    """Timeout/stale paths must not bypass the terminal-decision retirement fence."""
    monkeypatch.setattr(kb, "_authoritative_worker_exit", lambda _pid: False)
    with kb.connect() as conn:
        task_id, review, _host = _review_card(conn)
        reviewer_pid = 987654
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid=?, started_at=0, "
                "last_heartbeat_at=0, max_runtime_seconds=1 WHERE id=?",
                (reviewer_pid, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid=?, started_at=0 WHERE id=?",
                (reviewer_pid, review.current_run_id),
            )
        kb.approve_review(
            conn,
            task_id,
            reviewer=review.assignee,
            summary="fenced decision",
            head_sha=HEAD_SHA,
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert kb._pending_review_decision(conn, task_id) is not None

        if reclaimer == "max_runtime":
            reclaimed = kb.enforce_max_runtime(
                conn, signal_fn=lambda _pid, _sig: None,
            )
        else:
            reclaimed = kb.detect_stale_running(
                conn,
                stale_timeout_seconds=1,
                signal_fn=lambda _pid, _sig: None,
            )

        assert reclaimed == []
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.worker_pid == reviewer_pid
        assert current.current_run_id == review.current_run_id
        assert current.claim_lock == review.claim_lock
        assert current.recovery_required is True
        assert kb.claim_task(conn, task_id, claimer="local:successor") is None


@pytest.mark.parametrize("marker", ["task", "run", "gate"])
def test_recovery_required_reviewer_cannot_decide(kanban_home, marker):
    with kb.connect() as conn:
        task_id, review, _host = _review_card(conn)
        if marker == "task":
            conn.execute(
                "UPDATE tasks SET recovery_required=1, recovery_reason='test' WHERE id=?",
                (task_id,),
            )
        elif marker == "run":
            conn.execute(
                "UPDATE task_runs SET recovery_required=1, recovery_reason='test' WHERE id=?",
                (review.current_run_id,),
            )
        else:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET worker_pid=? WHERE id=?",
                    (987654, task_id),
                )
                conn.execute(
                    "UPDATE task_runs SET worker_pid=? WHERE id=?",
                    (987654, review.current_run_id),
                )
                authority = kb._latest_reviewer_authority(conn, task_id)
                assert authority is not None
                conn.execute(
                    "INSERT INTO task_launch_gates "
                    "(task_id, run_id, claim_lock, assignee, authority_id, gate_token, "
                    "gate_pid, state, recovery_required, recovery_reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'released', 1, 'test', ?)",
                    (task_id, review.current_run_id, review.claim_lock, review.assignee,
                     authority[0], "recovery-gate", 987654, int(time.time())),
                )
        conn.commit()
        before = _snapshot(conn, task_id)
        with pytest.raises(RuntimeError, match="recovery-required|recovery required"):
            kb.approve_review(
                conn, task_id, reviewer=review.assignee, summary="blocked",
                head_sha=HEAD_SHA, expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
        assert _snapshot(conn, task_id) == before


def test_recovery_required_rejects_generic_claim_reassign_and_successor(kanban_home):
    with kb.connect() as conn:
        task_id, review, _host = _review_card(conn)
        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, worker_pid=NULL, "
            "recovery_required=1, recovery_reason='test' WHERE id=?",
            (task_id,),
        )
        conn.commit()
        assert kb.claim_task(conn, task_id, claimer="local:successor") is None
        with pytest.raises(RuntimeError, match="recovery-required|recovery required"):
            kb.assign_task(conn, task_id, "programmer-2")
        assert kb.dispatch_once(conn).spawned == []
        current = kb.get_task(conn, task_id)
        assert current is not None and current.recovery_required is True


@pytest.mark.parametrize("mutation", ["task_expiry", "run_expiry", "run_profile", "run_claim"])
def test_real_review_startup_rejects_expired_or_mutated_run_identity(
    kanban_home, all_assignees_spawnable, monkeypatch, tmp_path, mutation,
):
    """A stopped released child cannot pass a stale task/run identity to exec."""
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles, "list_profiles", lambda: [SimpleNamespace(name="code-reviewer")],
    )
    sentinel = tmp_path / "stale-identity-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['REVIEW_SENTINEL']).write_text('ran')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: [sys.executable, str(worker)])
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(kb.__file__).resolve().parents[1]) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )
    monkeypatch.setenv("REVIEW_SENTINEL", str(sentinel))

    with kb.connect() as conn:
        task_id, _host = _unclaimed_review_card(conn, reviewer="code-reviewer")
        task = kb.get_task(conn, task_id)
        assert task is not None
        original_release = kb._ReviewLaunchGateHandle.release

        def release_after_identity_mutation(handle):
            os.kill(handle.pid, getattr(__import__('signal'), "SIGSTOP"))
            released = original_release(handle)
            run_id = conn.execute(
                "SELECT current_run_id FROM tasks WHERE id=?", (task_id,),
            ).fetchone()["current_run_id"]
            with kb.write_txn(conn):
                if mutation == "task_expiry":
                    conn.execute("UPDATE tasks SET claim_expires=0 WHERE id=?", (task_id,))
                elif mutation == "run_expiry":
                    conn.execute(
                        "UPDATE task_runs SET claim_expires=0 WHERE id=?",
                        (run_id,),
                    )
                elif mutation == "run_profile":
                    conn.execute(
                        "UPDATE task_runs SET profile='stale-reviewer' WHERE id=?",
                        (run_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE task_runs SET claim_lock='stale-owner' WHERE id=?",
                        (run_id,),
                    )
            os.kill(handle.pid, getattr(__import__('signal'), "SIGCONT"))
            return released

        monkeypatch.setattr(
            kb._ReviewLaunchGateHandle, "release", release_after_identity_mutation,
        )
        result = kb.dispatch_once(conn)
        assert result.spawned == [(task_id, "code-reviewer", str(kb.workspaces_root() / task_id))]
        deadline = time.time() + 5
        while time.time() < deadline:
            gate = conn.execute(
                "SELECT state, gate_pid, workspace_path FROM task_launch_gates "
                "WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if gate is not None and gate["state"] == "revalidation_failed":
                break
            time.sleep(0.01)
        assert gate is not None and gate["state"] == "revalidation_failed"
        assert not sentinel.exists()
        assert gate["workspace_path"]
        while kb._pid_alive(int(gate["gate_pid"])) and time.time() < deadline:
            time.sleep(0.01)
        kb.reap_worker_zombies()
        assert kb.detect_crashed_workers(conn) == [task_id]
        assert not Path(gate["workspace_path"]).exists()


def test_real_review_child_is_dormant_until_authorized_release(
    kanban_home, all_assignees_spawnable, monkeypatch, tmp_path,
):
    """The default reviewer launch must not run task code before release."""
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles,
        "list_profiles",
        lambda: [SimpleNamespace(name="code-reviewer")],
    )
    sentinel = tmp_path / "review-child-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['REVIEW_SENTINEL']).write_text('ran')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        kb,
        "_resolve_hermes_argv",
        lambda: [sys.executable, str(worker)],
    )
    monkeypatch.setenv("REVIEW_SENTINEL", str(sentinel))

    with kb.connect() as conn:
        task_id, _host = _unclaimed_review_card(conn, reviewer="code-reviewer")
        planned = kb.get_task(conn, task_id)
        assert planned is not None

        original_release = kb._release_review_launch

        def inspect_boundary(*args, **kwargs):
            gate = conn.execute(
                "SELECT state, workspace_path FROM task_launch_gates "
                "WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            assert gate is not None and gate["state"] == "prepared"
            assert gate["workspace_path"]
            assert Path(gate["workspace_path"]).is_dir()
            assert not sentinel.exists()
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='spawned'",
                (task_id,),
            ).fetchone()[0] == 0
            return original_release(*args, **kwargs)

        monkeypatch.setattr(kb, "_release_review_launch", inspect_boundary)
        result = kb.dispatch_once(conn)
        assert result.spawned == [(task_id, "code-reviewer", str(kb.workspaces_root() / task_id))]
        gate = conn.execute(
            "SELECT state, gate_pid, gate_token FROM task_launch_gates "
            "WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert gate is not None
        assert gate["state"] == "released"
        assert gate["gate_pid"] is not None
        assert gate["gate_token"]

        deadline = time.time() + 5
        while not sentinel.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert sentinel.read_text(encoding="utf-8") == "ran"
        assert kb.get_task(conn, task_id).status == "review"


def test_real_review_gate_failure_reaps_child_and_removes_unreleased_workspace(
    kanban_home, all_assignees_spawnable, monkeypatch, tmp_path,
):
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles, "list_profiles",
        lambda: [SimpleNamespace(name="code-reviewer")],
    )
    sentinel = tmp_path / "stale-child-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['REVIEW_SENTINEL']).write_text('ran')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: [sys.executable, str(worker)])
    monkeypatch.setenv("REVIEW_SENTINEL", str(sentinel))

    with kb.connect() as conn:
        task_id, _host = _unclaimed_review_card(conn, reviewer="code-reviewer")

        def deny_release(*_args, **_kwargs):
            gate = conn.execute(
                "SELECT state FROM task_launch_gates WHERE task_id=? "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            assert gate is not None and gate["state"] == "prepared"
            raise RuntimeError("injected release failure")

        monkeypatch.setattr(kb, "_release_review_launch", deny_release)
        result = kb.dispatch_once(conn)
        current = kb.get_task(conn, task_id)
        assert result.spawned == []
        assert current is not None
        assert current.status == "blocked"
        assert current.claim_lock is None
        assert current.worker_pid is None
        assert not sentinel.exists()
        gate = conn.execute(
            "SELECT state, workspace_path FROM task_launch_gates WHERE task_id=? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert gate is not None and gate["state"] == "retired"
        assert gate["workspace_path"]
        assert not Path(gate["workspace_path"]).exists()


def test_real_review_gate_false_termination_preserves_recovery_and_blocks_successor(
    kanban_home, all_assignees_spawnable, monkeypatch, tmp_path,
):
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles, "list_profiles",
        lambda: [SimpleNamespace(name="code-reviewer")],
    )
    sentinel = tmp_path / "surviving-child-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['REVIEW_SENTINEL']).write_text('ran')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: [sys.executable, str(worker)])
    monkeypatch.setenv("REVIEW_SENTINEL", str(sentinel))

    def deny_termination(pid, claim_lock, **_kwargs):
        return {
            "prev_pid": int(pid),
            "host_local": True,
            "termination_attempted": True,
            "terminated": False,
            "sigkill": False,
        }

    with kb.connect() as conn:
        task_id, _host = _unclaimed_review_card(conn, reviewer="code-reviewer")
        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", deny_termination)
        monkeypatch.setattr(
            kb,
            "_release_review_launch",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("inject retirement path")
            ),
        )

        result = kb.dispatch_once(conn)
        current = kb.get_task(conn, task_id)
        assert result.spawned == []
        assert current is not None
        assert current.status == "review"
        assert current.claim_lock is not None
        assert current.worker_pid is not None
        assert current.recovery_required is True
        assert not sentinel.exists()
        # The claim remains occupied, so a later tick cannot create a successor.
        later = kb.dispatch_once(conn)
        assert later.spawned == []
        assert kb.get_task(conn, task_id).recovery_required is True


def test_real_review_gate_allows_one_successor_only_after_retirement(
    kanban_home, all_assignees_spawnable, monkeypatch, tmp_path,
):
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles, "list_profiles",
        lambda: [
            SimpleNamespace(name="code-reviewer-a"),
            SimpleNamespace(name="code-reviewer-b"),
        ],
    )
    sentinel = tmp_path / "successor-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['REVIEW_SENTINEL']).write_text('ran')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: [sys.executable, str(worker)])
    monkeypatch.setenv("REVIEW_SENTINEL", str(sentinel))

    with kb.connect() as conn:
        task_id, _host = _unclaimed_review_card(conn, reviewer="code-reviewer-a")
        original_release = kb._release_review_launch
        raced = False

        def retire_before_release(*args, **kwargs):
            nonlocal raced
            if not raced:
                raced = True
                assert kb.failover_review_task(
                    conn,
                    task_id,
                    "code-reviewer-b",
                    error="new authority won before release",
                ) is not None
                return False
            return original_release(*args, **kwargs)

        monkeypatch.setattr(kb, "_release_review_launch", retire_before_release)
        first = kb.dispatch_once(conn)
        assert first.spawned == []
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.assignee == "code-reviewer-b"
        assert current.claim_lock is None
        assert not sentinel.exists()

        monkeypatch.setattr(kb, "_release_review_launch", original_release)
        second = kb.dispatch_once(conn)
        assert second.spawned == [
            (task_id, "code-reviewer-b", str(kb.workspaces_root() / task_id)),
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='spawned'",
            (task_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_launch_gates WHERE task_id=? AND state='released'",
            (task_id,),
        ).fetchone()[0] == 1
        deadline = time.time() + 5
        while not sentinel.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert sentinel.exists()


def test_real_review_attach_failure_has_no_workspace_or_task_side_effect(
    kanban_home, all_assignees_spawnable, monkeypatch, tmp_path,
):
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles, "list_profiles",
        lambda: [SimpleNamespace(name="code-reviewer")],
    )
    sentinel = tmp_path / "attach-failure-child-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['REVIEW_SENTINEL']).write_text('ran')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: [sys.executable, str(worker)])
    monkeypatch.setenv("REVIEW_SENTINEL", str(sentinel))
    original_handle = kb._review_launch_handle
    spawned_pid = None

    def hide_first_attachment(pid):
        nonlocal spawned_pid
        spawned_pid = int(pid)
        kb._review_launch_handle = original_handle
        return None

    monkeypatch.setattr(kb, "_review_launch_handle", hide_first_attachment)
    with kb.connect() as conn:
        task_id, _host = _unclaimed_review_card(conn, reviewer="code-reviewer")
        result = kb.dispatch_once(conn)
        current = kb.get_task(conn, task_id)
        assert result.spawned == []
        assert current is not None and current.status == "blocked"
        assert current.worker_pid is None
        assert not sentinel.exists()
        assert conn.execute(
            "SELECT COUNT(*) FROM task_launch_gates WHERE task_id=?",
            (task_id,),
        ).fetchone()[0] == 0
    assert spawned_pid is not None
    deadline = time.time() + 5
    while kb._pid_alive(spawned_pid) and time.time() < deadline:
        time.sleep(0.02)
    assert not kb._pid_alive(spawned_pid)


def test_real_review_stale_gate_does_not_leave_a_worktree(
    kanban_home, all_assignees_spawnable, monkeypatch, tmp_path,
):
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles, "list_profiles",
        lambda: [SimpleNamespace(name="code-reviewer")],
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)

    sentinel = tmp_path / "stale-worktree-child-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['REVIEW_SENTINEL']).write_text('ran')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: [sys.executable, str(worker)])
    monkeypatch.setenv("REVIEW_SENTINEL", str(sentinel))

    with kb.connect() as conn:
        task_id, _review, _host = _review_card(conn, reviewer="code-reviewer")
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=? WHERE id=?",
            (str(repo), task_id),
        )
        conn.commit()
        assert kb.reclaim_task(conn, task_id, reason="prepare worktree gate test")
        target = repo / ".worktrees" / task_id
        assert not target.exists()

        def deny_release(*_args, **_kwargs):
            raise RuntimeError("stale worktree generation")

        monkeypatch.setattr(kb, "_release_review_launch", deny_release)
        result = kb.dispatch_once(conn)
        current = kb.get_task(conn, task_id)
        assert result.spawned == []
        assert current is not None and current.status == "blocked"
        assert not sentinel.exists()
        assert not target.exists()


def test_real_review_parent_crash_retires_attached_gate_without_exec(
    kanban_home, all_assignees_spawnable, monkeypatch, tmp_path,
):
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    sentinel = tmp_path / "parent-crash-child-ran"
    pid_file = tmp_path / "gate.pid"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['REVIEW_SENTINEL']).write_text('ran')\n",
        encoding="utf-8",
    )

    with kb.connect() as conn:
        task_id, review, _host = _review_card(conn, reviewer="code-reviewer")
        db_path = kb.kanban_db_path()
        script = "\n".join([
            "import os, sys",
            "from pathlib import Path",
            "from hermes_cli import kanban_db as k",
            "conn = k.connect(db_path=Path(os.environ['KDB']))",
            "task = k.get_task(conn, os.environ['TASK'])",
            "authority = k._latest_unconsumed_reviewer_authority(conn, task.id)",
            "workspace = k.resolve_workspace(task, materialize=False)",
            "token = 'parent-crash-gate-token'",
            "pid = k._default_spawn(task, str(workspace), launch_gate=True, launch_token=token, launch_authority_id=authority[0])",
            "with k.write_txn(conn):",
            "    conn.execute(\"UPDATE tasks SET worker_pid=? WHERE id=? AND worker_pid IS NULL\", (pid, task.id))",
            "    conn.execute(\"UPDATE task_runs SET worker_pid=? WHERE id=? AND worker_pid IS NULL\", (pid, task.current_run_id))",
            "    conn.execute(\"INSERT INTO task_launch_gates (task_id, run_id, claim_lock, assignee, authority_id, gate_token, gate_pid, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'attached', strftime('%s','now'))\", (task.id, task.current_run_id, task.claim_lock, task.assignee, authority[0], token, pid))",
            "Path(os.environ['PIDFILE']).write_text(str(pid))",
            "os._exit(0)",
        ])
        env = dict(__import__('os').environ)
        env.update({
            "KDB": str(db_path),
            "TASK": task_id,
            "PIDFILE": str(pid_file),
            "REVIEW_SENTINEL": str(sentinel),
            "HERMES_KANBAN_DB": str(db_path),
            "HERMES_LAUNCH_TEST_WORKER": str(worker),
        })
        parent = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=str(Path.cwd()),
            env=env,
        )
        assert parent.wait(timeout=10) == 0

        deadline = time.time() + 5
        while not pid_file.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        gate_pid = int(pid_file.read_text(encoding="utf-8"))
        while kb._pid_alive(gate_pid) and time.time() < deadline:
            time.sleep(0.02)
        assert not kb._pid_alive(gate_pid)
        assert not sentinel.exists()

        crashed = kb.detect_crashed_workers(conn)
        assert task_id in crashed
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.worker_pid is None
        gate = conn.execute(
            "SELECT state, workspace_path FROM task_launch_gates WHERE task_id=? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert gate is not None and gate["state"] == "retired"
        assert gate["workspace_path"] is None


def test_real_review_startup_revalidation_failure_never_executes_task_code(
    kanban_home, all_assignees_spawnable, monkeypatch, tmp_path,
):
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles, "list_profiles",
        lambda: [SimpleNamespace(name="code-reviewer")],
    )
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    sentinel = tmp_path / "revalidation-child-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['REVIEW_SENTINEL']).write_text('ran')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: [sys.executable, str(worker)])
    monkeypatch.setenv("REVIEW_SENTINEL", str(sentinel))
    original_release = kb._ReviewLaunchGateHandle.release

    with kb.connect() as conn:
        task_id, _host = _unclaimed_review_card(conn, reviewer="code-reviewer")

        def stale_before_pipe(handle):
            conn.execute(
                "UPDATE tasks SET assignee='stale-reviewer' WHERE id=?",
                (task_id,),
            )
            conn.commit()
            return original_release(handle)

        monkeypatch.setattr(kb._ReviewLaunchGateHandle, "release", stale_before_pipe)
        result = kb.dispatch_once(conn)
        assert result.spawned == [
            (task_id, "code-reviewer", str(kb.workspaces_root() / task_id)),
        ]
        deadline = time.time() + 5
        while True:
            gate = conn.execute(
                "SELECT state, gate_pid, workspace_path FROM task_launch_gates "
                "WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if gate is not None and gate["state"] == "revalidation_failed":
                break
            if time.time() >= deadline:
                raise AssertionError("startup revalidation did not fail")
            time.sleep(0.02)
        assert not sentinel.exists()
        while kb._pid_alive(int(gate["gate_pid"])) and time.time() < deadline:
            time.sleep(0.02)
        assert not kb._pid_alive(int(gate["gate_pid"]))
        assert kb.detect_crashed_workers(conn) == [task_id]
        assert gate["workspace_path"]
        assert not Path(gate["workspace_path"]).exists()


def test_claimed_reviewer_stays_in_review_column(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.current_run_id == review.current_run_id
        assert current.assignee == "code-reviewer"


def test_running_reviewer_can_request_changes_exactly_once(kanban_home):
    """A claimed reviewer run may be ``running`` while deciding."""
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()

        corrected = kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason="tighten error handling",
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert corrected is not None
        assert corrected.status == "ready"
        assert corrected.assignee == "programmer"
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM task_events "
            "WHERE task_id=? AND kind='changes_requested'",
            (task_id,),
        ).fetchone()["count"] == 1

        retried = kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason="tighten error handling",
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert retried is not None
        assert retried.status == "ready"
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM task_events "
            "WHERE task_id=? AND kind='changes_requested'",
            (task_id,),
        ).fetchone()["count"] == 1


def test_reviewer_block_ready_reclaim_can_request_changes(kanban_home):
    """A reviewer recovered through blocked -> ready keeps its authority."""
    with kb.connect() as conn:
        task_id, review, host = _review_card(conn)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        assert kb.block_task(
            conn,
            task_id,
            reason="temporary reviewer capability failure",
            kind="capability",
            expected_run_id=review.current_run_id,
        )
        blocked = kb.get_task(conn, task_id)
        assert blocked is not None and blocked.status == "blocked"
        assert kb.unblock_task(conn, task_id)

        recovered = kb.claim_task(
            conn, task_id, claimer=f"{host}:review-retry",
        )
        assert recovered is not None
        assert recovered.status == "running"
        assert recovered.assignee == "code-reviewer"
        assert recovered.current_run_id != review.current_run_id

        corrected = kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason="retry decision",
            expected_claim=recovered.claim_lock,
            expected_run_id=recovered.current_run_id,
        )
        assert corrected is not None
        assert corrected.status == "ready"
        assert corrected.assignee == "programmer"
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM task_events "
            "WHERE task_id=? AND kind='changes_requested'",
            (task_id,),
        ).fetchone()["count"] == 1


def test_reviewer_recovery_block_does_not_false_escalate_to_triage(kanban_home):
    with kb.connect() as conn:
        task_id, review, host = _review_card(conn)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        assert kb.block_task(
            conn,
            task_id,
            reason="temporary reviewer capability failure",
            kind="capability",
            expected_run_id=review.current_run_id,
        )
        assert kb.unblock_task(conn, task_id)
        recovered = kb.claim_task(conn, task_id, claimer=f"{host}:review-retry")
        assert recovered is not None

        assert kb.block_task(
            conn,
            task_id,
            reason="temporary reviewer capability failure",
            kind="capability",
            expected_run_id=recovered.current_run_id,
        )
        blocked = kb.get_task(conn, task_id)
        assert blocked is not None
        assert blocked.status == "blocked"
        assert blocked.status != "triage"


def test_running_reviewer_can_approve_exact_head(kanban_home):
    """Approve uses the same active reviewer predicate as request-changes."""
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()

        approved = kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="focused proof is green",
            head_sha=HEAD_SHA,
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert approved is not None
        assert approved.status == "ready"
        assert approved.assignee == "programmer"


def test_running_reviewer_rejects_wrong_claim_without_mutation(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="no longer holds"):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="code-reviewer",
                reason="wrong credential",
                expected_claim="wrong-claim",
                expected_run_id=review.current_run_id,
            )

        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == review.current_run_id
        assert current.claim_lock == review.claim_lock
        assert _snapshot(conn, task_id) == before


def test_trusted_request_changes_rejects_successor_programmer_run(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        assert kb.request_changes(
            conn, task_id, "programmer", reason="first decision",
            trusted_operator=True,
        ) is not None
        correction = kb.claim_task(conn, task_id, claimer="host:programmer")
        assert correction is not None
        before = _snapshot(conn, task_id)
        before_run = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id=?",
            (correction.current_run_id,),
        ).fetchone()

        with pytest.raises(RuntimeError, match="reviewer generation"):
            kb.request_changes(
                conn, task_id, "programmer", reason="duplicate decision",
                trusted_operator=True,
            )

        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == correction.current_run_id
        assert _snapshot(conn, task_id) == before
        assert conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id=?",
            (correction.current_run_id,),
        ).fetchone() == before_run


def test_trusted_approve_rejects_successor_programmer_run(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        assert kb.approve_review(
            conn, task_id, reviewer="code-reviewer", summary="first approval",
            head_sha=HEAD_SHA, trusted_operator=True,
        ) is not None
        finalizer = kb.claim_task(conn, task_id, claimer="host:programmer")
        assert finalizer is not None
        before = _snapshot(conn, task_id)
        before_run = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id=?",
            (finalizer.current_run_id,),
        ).fetchone()

        with pytest.raises(RuntimeError, match="reviewer generation"):
            kb.approve_review(
                conn, task_id, reviewer="code-reviewer", summary="duplicate approval",
                head_sha=HEAD_SHA, trusted_operator=True,
            )

        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == finalizer.current_run_id
        assert _snapshot(conn, task_id) == before
        assert conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id=?",
            (finalizer.current_run_id,),
        ).fetchone() == before_run


def test_running_reviewer_rejects_expired_claim_without_mutation(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        conn.execute(
            "UPDATE tasks SET claim_expires=? WHERE id=?",
            (int(time.time()) - 1, task_id),
        )
        conn.commit()
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="reviewer generation"):
            kb.request_changes(
                conn, task_id, "programmer", reviewer="code-reviewer",
                reason="expired", expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )

        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.current_run_id == review.current_run_id
        assert _snapshot(conn, task_id) == before


@pytest.mark.parametrize("kind", ["request_changes", "approve"])
@pytest.mark.parametrize(
    "payload",
    ["[", "[]", "{}", '{"reviewer": null}', '{"reviewer": "   "}'],
)
def test_malformed_latest_review_authority_fails_closed(kanban_home, kind, payload):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        authority = conn.execute(
            "SELECT id FROM task_events WHERE task_id=? "
            "AND kind='submitted_for_review' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert authority is not None
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?", (payload, authority["id"]),
        )
        conn.commit()
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="reviewer generation|reviewer lane"):
            _terminal_decision(conn, task_id, review, kind=kind, trusted=kind == "approve")

        _assert_no_mutation(conn, task_id, before, review)


@pytest.mark.parametrize("kind", ["request_changes", "approve"])
@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize(
    "payload",
    ["not-json", "[]", "{}", '{"other": "missing"}',
     '{"reviewer": ""}', '{"reviewer": 42}', '{"reviewer": "   "}'],
)
def test_newer_malformed_review_authority_overrides_valid_history(
    kanban_home, kind, trusted, payload,
):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        kb._append_event(
            conn, task_id, "submitted_for_review", {"reviewer": "code-reviewer"},
            run_id=review.current_run_id,
        )
        newer = conn.execute(
            "SELECT id FROM task_events WHERE task_id=? "
            "AND kind='submitted_for_review' ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        assert newer is not None
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (payload, newer["id"]))
        conn.commit()
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="reviewer generation|reviewer lane"):
            _terminal_decision(conn, task_id, review, kind=kind, trusted=trusted)

        _assert_no_mutation(conn, task_id, before, review)


@pytest.mark.parametrize("kind", ["request_changes", "approve"])
def test_valid_newer_failover_recovers_from_malformed_history(kanban_home, kind):
    with kb.connect() as conn:
        task_id, review, host = _review_card(conn, reviewer="reviewer-a")
        kb._append_event(
            conn, task_id, "submitted_for_review", {"reviewer": "reviewer-a"},
            run_id=review.current_run_id,
        )
        newer = conn.execute(
            "SELECT id FROM task_events WHERE task_id=? "
            "AND kind='submitted_for_review' ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        assert newer is not None
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?", ("{malformed", newer["id"]),
        )
        conn.commit()
        assert kb.failover_review_task(conn, task_id, "reviewer-b", error="handoff")
        replacement = kb.claim_review_task(conn, task_id, claimer=f"{host}:replacement")
        assert replacement is not None

        if kind == "request_changes":
            result = kb.request_changes(
                conn, task_id, "programmer", reviewer="reviewer-b",
                reason="valid failover decision",
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
        else:
            result = kb.approve_review(
                conn, task_id, reviewer="reviewer-b", summary="valid failover decision",
                head_sha=HEAD_SHA, expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
        assert result is not None


@pytest.mark.parametrize("kind", ["request_changes", "approve"])
def test_missing_latest_failover_authority_fails_closed(kanban_home, kind):
    with kb.connect() as conn:
        task_id, review, host = _review_card(conn, reviewer="reviewer-a")
        assert kb.failover_review_task(conn, task_id, "reviewer-b", error="handoff")
        replacement = kb.claim_review_task(conn, task_id, claimer=f"{host}:replacement")
        assert replacement is not None
        authority = conn.execute(
            "SELECT id FROM task_events WHERE task_id=? AND kind='review_failover' "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        assert authority is not None
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps({"other": "missing"}), authority["id"]),
        )
        conn.commit()
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="reviewer generation|reviewer lane"):
            _terminal_decision(
                conn, task_id, replacement, kind=kind, trusted=kind == "approve",
            )

        _assert_no_mutation(conn, task_id, before, replacement)


@pytest.mark.parametrize("kind", ["request_changes", "approve"])
@pytest.mark.parametrize(
    "null_side",
    ["task", "run", "both", "task_boundary", "run_boundary"],
)
def test_missing_or_nonfresh_claim_expiry_fails_closed(kanban_home, kind, null_side):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        expiry = int(time.time())
        task_expiry = None if null_side in {"task", "both"} else expiry
        run_expiry = None if null_side in {"run", "both"} else expiry
        if null_side == "task_boundary":
            task_expiry = expiry
            run_expiry = expiry + 60
        elif null_side == "run_boundary":
            task_expiry = expiry + 60
            run_expiry = expiry
        conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (task_expiry, task_id))
        conn.execute(
            "UPDATE task_runs SET claim_expires=? WHERE id=?",
            (run_expiry, review.current_run_id),
        )
        conn.commit()
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="reviewer generation"):
            _terminal_decision(conn, task_id, review, kind=kind, trusted=kind == "approve")

        _assert_no_mutation(conn, task_id, before, review)


def test_trusted_request_changes_rejects_reassigned_review_owner(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="reassigned review", assignee="programmer")
        assert kb.claim_task(conn, task_id, claimer="host:implementation")
        assert kb.submit_task_for_review(
            conn, task_id, "code-reviewer", trusted_operator=True,
        ) is not None
        assert kb.assign_task(conn, task_id, "reviewer-b")
        review = kb.claim_review_task(conn, task_id, claimer="host:review")
        assert review is not None
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="reviewer generation"):
            kb.request_changes(
                conn, task_id, "programmer", reason="wrong owner",
                trusted_operator=True,
            )

        _assert_no_mutation(conn, task_id, before, review)


def test_trusted_approve_rejects_reassigned_review_owner(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="reassigned approval", assignee="programmer")
        assert kb.claim_task(conn, task_id, claimer="host:implementation")
        assert kb.submit_task_for_review(
            conn, task_id, "code-reviewer", trusted_operator=True,
        ) is not None
        assert kb.assign_task(conn, task_id, "reviewer-b")
        review = kb.claim_review_task(conn, task_id, claimer="host:review")
        assert review is not None
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="does not own|reviewer generation"):
            kb.approve_review(
                conn, task_id, reviewer="reviewer-b", summary="wrong owner",
                head_sha=HEAD_SHA, trusted_operator=True,
            )

        _assert_no_mutation(conn, task_id, before, review)


def test_old_request_retry_rejects_reclaimed_successor(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        reason = "same correction packet"
        assert kb.request_changes(
            conn, task_id, "programmer", reviewer="code-reviewer", reason=reason,
            expected_claim=review.claim_lock, expected_run_id=review.current_run_id,
        ) is not None
        successor = kb.claim_task(conn, task_id, claimer="host:successor")
        assert successor is not None
        assert kb.reclaim_task(conn, task_id, reason="successor reclaimed")
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="not active"):
            kb.request_changes(
                conn, task_id, "programmer", reviewer="code-reviewer", reason=reason,
                expected_claim=review.claim_lock, expected_run_id=review.current_run_id,
            )

        assert _snapshot(conn, task_id) == before


def test_old_approval_retry_rejects_blocked_successor(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        assert kb.approve_review(
            conn, task_id, reviewer="code-reviewer", summary="same approval",
            head_sha=HEAD_SHA, expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        ) is not None
        successor = kb.claim_task(conn, task_id, claimer="host:successor")
        assert successor is not None
        assert kb.block_task(
            conn, task_id, reason="successor blocked",
            expected_run_id=successor.current_run_id,
        )
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="not active"):
            kb.approve_review(
                conn, task_id, reviewer="code-reviewer", summary="same approval",
                head_sha=HEAD_SHA, expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )

        assert _snapshot(conn, task_id) == before


def test_approve_records_exact_head_and_routes_same_card_to_finalizer(kanban_home):
    with kb.connect() as conn:
        task_id, review, host = _review_card(conn)
        approved = kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="16/16 focused proof, CI green",
            head_sha=HEAD_SHA,
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert approved is not None
        assert approved.status == "ready"
        assert approved.assignee == "programmer"
        assert approved.claim_lock is None
        assert approved.worker_pid is None
        assert approved.current_run_id is None
        assert approved.completed_at is None
        assert len(kb.list_tasks(conn)) == 1

        evidence = _payload(conn, task_id, "review_approved")
        assert evidence["head_sha"] == HEAD_SHA
        assert evidence["reviewer"] == "code-reviewer"
        assert evidence["summary"] == "16/16 focused proof, CI green"
        assert evidence["finalizer"] == "programmer"
        run = conn.execute(
            "SELECT outcome, summary, ended_at FROM task_runs WHERE id=?",
            (review.current_run_id,),
        ).fetchone()
        assert run["outcome"] == "approved"
        assert run["summary"] == "16/16 focused proof, CI green"
        assert run["ended_at"] is not None
        assert "completed" not in _events(conn, task_id)
        assert kb.in_finalization_lane(conn, task_id) is True
        assert kb.in_correction_lane(conn, task_id) is False


def test_approve_requires_full_head_and_preserves_review_on_failure(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        with pytest.raises(ValueError, match="full 40- or 64-character"):
            kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer",
                summary="approved",
                head_sha="deadbeef",
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.current_run_id == review.current_run_id
        assert "review_approved" not in _events(conn, task_id)


def test_self_approval_and_stale_claim_fail_closed(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        with pytest.raises(RuntimeError, match="does not own"):
            kb.approve_review(
                conn,
                task_id,
                reviewer="programmer",
                summary="approved",
                head_sha=HEAD_SHA,
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
        with pytest.raises(RuntimeError, match="no longer holds"):
            kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer",
                summary="approved",
                head_sha=HEAD_SHA,
                expected_claim="stale-claim",
                expected_run_id=review.current_run_id,
            )
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "review"
        assert current.current_run_id == review.current_run_id


def test_request_changes_is_one_bundled_same_card_packet(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        with pytest.raises(RuntimeError, match="does not own"):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="programmer",
                reason="bad caller",
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
        corrected = kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason="tighten error handling",
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert corrected is not None
        assert corrected.status == "ready"
        assert corrected.assignee == "programmer"
        assert corrected.claim_lock is None
        assert corrected.worker_pid is None
        assert corrected.current_run_id is None
        assert len(kb.list_tasks(conn)) == 1
        decision = _payload(conn, task_id, "changes_requested")
        assert decision["programmer"] == "programmer"
        assert decision["reason"] == "tighten error handling"
        assert "review_approved" not in _events(conn, task_id)
        run = conn.execute(
            "SELECT outcome, ended_at FROM task_runs WHERE id=?",
            (review.current_run_id,),
        ).fetchone()
        assert run["outcome"] == "changes_requested"
        assert run["ended_at"] is not None
        assert kb.in_correction_lane(conn, task_id) is True


def test_request_changes_rejects_programmer_not_from_submission_history(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn, programmer="programmer")
        with pytest.raises(RuntimeError, match="original implementation owner"):
            kb.request_changes(
                conn,
                task_id,
                "orchestrator",
                reviewer="code-reviewer",
                reason="reroute the work",
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.assignee == "code-reviewer"
        assert current.current_run_id == review.current_run_id
        assert "changes_requested" not in _events(conn, task_id)


def test_request_changes_rejects_unclaimed_or_nonreview_status(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        conn.execute("UPDATE tasks SET claim_lock=NULL WHERE id=?", (task_id,))
        conn.commit()
        with pytest.raises(RuntimeError):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="code-reviewer",
                reason="fix",
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "review"


def test_failover_replaces_reviewer_and_rejects_stale_decision(kanban_home):
    with kb.connect() as conn:
        task_id, first, host = _review_card(conn, reviewer="reviewer-a")
        replacement_task = kb.failover_review_task(
            conn, task_id, "reviewer-b", error="reviewer-a exited",
        )
        assert replacement_task is not None
        replacement = kb.claim_review_task(
            conn, task_id, claimer=f"{host}:replacement",
        )
        assert replacement is not None
        with pytest.raises(RuntimeError):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="reviewer-a",
                reason="stale feedback",
                expected_claim=first.claim_lock,
                expected_run_id=first.current_run_id,
            )
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.assignee == "reviewer-b"
        assert current.current_run_id == replacement.current_run_id
        assert current.claim_lock == replacement.claim_lock


@pytest.mark.parametrize("decision", ["request_changes", "approve"])
def test_dispatch_failover_claims_only_the_authorized_replacement_generation(
    kanban_home, all_assignees_spawnable, monkeypatch, decision,
):
    """A failover handoff must be claimed by the reviewer named by its event.

    Profile discovery is intentionally ordered with the failed reviewer first.
    Before the regression fix, the dispatcher reassigned the card to that old
    profile instead of claiming the new failover authority.
    """
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles,
        "list_profiles",
        lambda: [
            SimpleNamespace(name="code-reviewer-a"),
            SimpleNamespace(name="code-reviewer-b"),
            SimpleNamespace(name="code-reviewer-c"),
        ],
    )
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    spawned = []

    def spawn(task, workspace):
        spawned.append((task.id, task.assignee))
        return 1200 + len(spawned)

    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="dispatcher failover", assignee="programmer")
        assert kb.claim_task(conn, task_id, claimer=f"{host}:implementation")
        assert kb.submit_task_for_review(
            conn, task_id, "code-reviewer-a", trusted_operator=True,
        ) is not None

        kb.dispatch_once(conn, spawn_fn=spawn)
        first = kb.get_task(conn, task_id)
        assert first is not None and first.assignee == "code-reviewer-a"
        assert spawned == [(task_id, "code-reviewer-a")]

        assert task_id in kb.detect_crashed_workers(conn)
        failed_over = kb.get_task(conn, task_id)
        assert failed_over is not None
        assert failed_over.assignee == "code-reviewer-b"
        authority = kb._latest_reviewer_authority(conn, task_id)
        assert authority is not None and authority[1] == "code-reviewer-b"

        spawned.clear()
        kb.dispatch_once(conn, spawn_fn=spawn)
        replacement = kb.get_task(conn, task_id)
        assert replacement is not None
        assert replacement.assignee == "code-reviewer-b"
        assert spawned == [(task_id, "code-reviewer-b")]

        # A late old-process comment is evidence only; it cannot change the
        # durable authority or consume the successor's one decision.
        kb.add_comment(conn, task_id, "code-reviewer-a", "REQUEST_CHANGES late old process")
        with pytest.raises(
            RuntimeError,
            match="reviewer generation|reviewer lane|active review run|expected run",
        ):
            if decision == "request_changes":
                kb.request_changes(
                    conn,
                    task_id,
                    "programmer",
                    reviewer="code-reviewer-a",
                    reason="stale feedback",
                    expected_claim=first.claim_lock,
                    expected_run_id=first.current_run_id,
                )
            else:
                kb.approve_review(
                    conn,
                    task_id,
                    reviewer="code-reviewer-a",
                    summary="stale approval",
                    head_sha=HEAD_SHA,
                    expected_claim=first.claim_lock,
                    expected_run_id=first.current_run_id,
                )

        if decision == "request_changes":
            result = kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="code-reviewer-b",
                reason="replacement feedback",
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
            retried = kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="code-reviewer-b",
                reason="replacement feedback",
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
        else:
            result = kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer-b",
                summary="replacement approval",
                head_sha=HEAD_SHA,
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
            retried = kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer-b",
                summary="replacement approval",
                head_sha=HEAD_SHA,
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
        assert result is not None and retried is not None
        assert _events(conn, task_id).count(
            "changes_requested" if decision == "request_changes" else "review_approved"
        ) == 1


@pytest.mark.parametrize("decision", ["request_changes", "approve"])
def test_dispatch_ignores_assignment_only_reset_after_review_failover(
    kanban_home, all_assignees_spawnable, monkeypatch, decision,
):
    """An assignment event cannot replace the latest review authority.

    This is the adversarial sequence from the stale-assignee incident:
    reviewer A fails over to B, an assignment-only write puts A back on the
    task row, then the dispatcher must reconcile B before claiming a run.
    """
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles,
        "list_profiles",
        lambda: [
            SimpleNamespace(name="code-reviewer-a"),
            SimpleNamespace(name="code-reviewer-b"),
            SimpleNamespace(name="code-reviewer-c"),
        ],
    )
    spawned = []

    def spawn(task, workspace):
        spawned.append((task.id, task.assignee))
        return 1400 + len(spawned)

    with kb.connect() as conn:
        task_id, _, _ = _review_card(conn, reviewer="code-reviewer-a")
        assert kb.failover_review_task(
            conn, task_id, "code-reviewer-b", error="reviewer-a failed",
        ) is not None
        assert kb.assign_task(conn, task_id, "code-reviewer-a")

        before_dispatch = kb.get_task(conn, task_id)
        assert before_dispatch is not None
        assert before_dispatch.assignee == "code-reviewer-a"
        authority = kb._latest_reviewer_authority(conn, task_id)
        assert authority is not None and authority[1] == "code-reviewer-b"

        kb.dispatch_once(conn, spawn_fn=spawn)
        replacement = kb.get_task(conn, task_id)
        assert replacement is not None
        assert replacement.assignee == "code-reviewer-b"
        assert spawned == [(task_id, "code-reviewer-b")]

        with pytest.raises(
            RuntimeError,
            match="reviewer generation|reviewer lane|active review run|expected run",
        ):
            if decision == "request_changes":
                kb.request_changes(
                    conn,
                    task_id,
                    "programmer",
                    reviewer="code-reviewer-a",
                    reason="stale assignment-only reviewer",
                    expected_claim=replacement.claim_lock,
                    expected_run_id=replacement.current_run_id,
                )
            else:
                kb.approve_review(
                    conn,
                    task_id,
                    reviewer="code-reviewer-a",
                    summary="stale assignment-only reviewer",
                    head_sha=HEAD_SHA,
                    expected_claim=replacement.claim_lock,
                    expected_run_id=replacement.current_run_id,
                )

        if decision == "request_changes":
            decided = kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="code-reviewer-b",
                reason="replacement reviewer decision",
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
            replay = kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="code-reviewer-b",
                reason="replacement reviewer decision",
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
        else:
            decided = kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer-b",
                summary="replacement reviewer decision",
                head_sha=HEAD_SHA,
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
            replay = kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer-b",
                summary="replacement reviewer decision",
                head_sha=HEAD_SHA,
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
        assert decided is not None and replay is not None
        assert _events(conn, task_id).count(
            "changes_requested" if decision == "request_changes" else "review_approved"
        ) == 1


@pytest.mark.parametrize("race", ["assignment", "failover"])
@pytest.mark.parametrize("decision", ["request_changes", "approve"])
def test_dispatch_defers_interleaved_reviewer_mutation_before_claim(
    kanban_home, all_assignees_spawnable, monkeypatch, race, decision,
):
    """A writer between discovery and claim cannot authorize a stale spawn."""
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles,
        "list_profiles",
        lambda: [
            SimpleNamespace(name="code-reviewer-a"),
            SimpleNamespace(name="code-reviewer-b"),
            SimpleNamespace(name="code-reviewer-c"),
        ],
    )
    spawned = []
    mutation_errors = []
    mutation_fired = False

    def spawn(task, workspace):
        spawned.append((task.id, task.assignee))
        return 1800 + len(spawned)

    with kb.connect() as conn:
        task_id, _, _ = _review_card(conn, reviewer="code-reviewer-a")
        assert kb.failover_review_task(
            conn, task_id, "code-reviewer-b", error="reviewer-a failed",
        ) is not None
        initial_claims = _events(conn, task_id).count("claimed")
        db_path = kb.kanban_db_path()

        def mutate_from_other_connection():
            race_conn = kb.connect(db_path=db_path)
            try:
                if race == "assignment":
                    result = kb.assign_task(
                        conn=race_conn,
                        task_id=task_id,
                        profile="code-reviewer-a",
                    )
                else:
                    result = kb.failover_review_task(
                        race_conn,
                        task_id,
                        "code-reviewer-c",
                        error="reviewer-b failed during dispatch",
                    )
                if result is None:
                    raise AssertionError(
                        f"{race} interleaving did not mutate the task"
                    )
            except BaseException as exc:  # propagate writer failure to the test thread
                mutation_errors.append(exc)
            finally:
                race_conn.close()

        original_claim = kb.claim_review_task

        def claim_after_interleaving(*args, **kwargs):
            nonlocal mutation_fired
            if not mutation_fired:
                mutation_fired = True
                writer = threading.Thread(target=mutate_from_other_connection)
                writer.start()
                writer.join(timeout=5)
                assert not writer.is_alive(), "interleaving writer did not finish"
                if mutation_errors:
                    raise mutation_errors[0]
            return original_claim(*args, **kwargs)

        monkeypatch.setattr(kb, "claim_review_task", claim_after_interleaving)

        first_tick = kb.dispatch_once(conn, spawn_fn=spawn)
        assert first_tick.spawned == []
        assert mutation_fired is True
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.claim_lock is None
        assert current.current_run_id is None
        assert _events(conn, task_id).count("claimed") == initial_claims

        expected_reviewer = (
            "code-reviewer-b" if race == "assignment" else "code-reviewer-c"
        )
        authority = kb._latest_reviewer_authority(conn, task_id)
        assert authority is not None and authority[1] == expected_reviewer
        assert current.assignee == (
            "code-reviewer-a" if race == "assignment" else expected_reviewer
        )

        spawned.clear()
        second_tick = kb.dispatch_once(conn, spawn_fn=spawn)
        assert len(second_tick.spawned) == 1
        assert second_tick.spawned[0][:2] == (task_id, expected_reviewer)
        replacement = kb.get_task(conn, task_id)
        assert replacement is not None
        assert replacement.assignee == expected_reviewer
        assert replacement.current_run_id is not None
        assert spawned == [(task_id, expected_reviewer)]
        claim_event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='claimed' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert claim_event is not None
        assert json.loads(claim_event["payload"])["review_authority_id"] == authority[0]

        if decision == "request_changes":
            decided = kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer=expected_reviewer,
                reason="interleaving correction",
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
            replay = kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer=expected_reviewer,
                reason="interleaving correction",
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
            terminal_kind = "changes_requested"
        else:
            decided = kb.approve_review(
                conn,
                task_id,
                reviewer=expected_reviewer,
                summary="interleaving approval",
                head_sha=HEAD_SHA,
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
            replay = kb.approve_review(
                conn,
                task_id,
                reviewer=expected_reviewer,
                summary="interleaving approval",
                head_sha=HEAD_SHA,
                expected_claim=replacement.claim_lock,
                expected_run_id=replacement.current_run_id,
            )
            terminal_kind = "review_approved"
        assert decided is not None and replay is not None
        assert _events(conn, task_id).count(terminal_kind) == 1


@pytest.mark.parametrize("decision", ["request_changes", "approve"])
def test_claimed_review_rejects_assignment_reset_before_spawn(
    kanban_home, all_assignees_spawnable, monkeypatch, decision,
):
    """Assignment-only writers cannot invalidate a live review claim."""
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles,
        "list_profiles",
        lambda: [
            SimpleNamespace(name="code-reviewer-a"),
            SimpleNamespace(name="code-reviewer-b"),
        ],
    )
    spawned = []
    assignment_errors = []

    def spawn(task, workspace):
        spawned.append((task.id, task.assignee))
        return 1900

    with kb.connect() as conn:
        task_id, _host = _unclaimed_review_card(conn, reviewer="code-reviewer-a")
        original_claim = kb.claim_review_task
        db_path = kb.kanban_db_path()

        def claim_then_assignment(*args, **kwargs):
            claimed = original_claim(*args, **kwargs)
            assert claimed is not None
            race_conn = kb.connect(db_path=db_path)
            try:
                with pytest.raises(RuntimeError, match="currently claimed"):
                    kb.assign_task(race_conn, task_id, "code-reviewer-b")
                assignment_errors.append("rejected")
            finally:
                race_conn.close()
            return claimed

        monkeypatch.setattr(kb, "claim_review_task", claim_then_assignment)
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        assert len(result.spawned) == 1
        assert result.spawned[0][:2] == (task_id, "code-reviewer-a")
        assert assignment_errors == ["rejected"]
        assert spawned == [(task_id, "code-reviewer-a")]

        review = kb.get_task(conn, task_id)
        assert review is not None and review.current_run_id is not None
        if decision == "request_changes":
            decided = kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="code-reviewer-a",
                reason="assignment boundary correction",
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
            replay = kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="code-reviewer-a",
                reason="assignment boundary correction",
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
            terminal_kind = "changes_requested"
        else:
            decided = kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer-a",
                summary="assignment boundary approval",
                head_sha=HEAD_SHA,
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
            replay = kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer-a",
                summary="assignment boundary approval",
                head_sha=HEAD_SHA,
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
            terminal_kind = "review_approved"
        assert decided is not None and replay is not None
        assert _events(conn, task_id).count(terminal_kind) == 1


@pytest.mark.parametrize("mutation", ["failover", "reclaim"])
@pytest.mark.parametrize("decision", ["request_changes", "approve"])
def test_review_spawn_pid_attachment_denies_stale_child_after_claim(
    kanban_home, all_assignees_spawnable, monkeypatch, mutation, decision,
):
    """A writer winning before PID attachment cannot authorize the old child."""
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles,
        "list_profiles",
        lambda: [
            SimpleNamespace(name="code-reviewer-a"),
            SimpleNamespace(name="code-reviewer-b"),
        ],
    )
    spawn_calls = []
    terminated = []

    def terminate(pid, claim_lock, **kwargs):
        if pid:
            terminated.append((pid, claim_lock))
        return {
            "prev_pid": int(pid) if pid else None,
            "host_local": True,
            "termination_attempted": True,
            "terminated": True,
            "sigkill": False,
        }

    with kb.connect() as conn:
        initial_reviewer = "code-reviewer-a"
        task_id, host = _unclaimed_review_card(conn, reviewer=initial_reviewer)
        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", terminate)
        race_conn = kb.connect(db_path=kb.kanban_db_path())
        try:
            def spawn(task, workspace):
                spawn_calls.append((task.id, task.assignee))
                if len(spawn_calls) == 1:
                    if mutation == "failover":
                        assert kb.failover_review_task(
                            race_conn,
                            task_id,
                            "code-reviewer-b",
                            error="winner before attachment",
                        ) is not None
                    else:
                        assert kb.reclaim_task(
                            race_conn,
                            task_id,
                            reason="winner before attachment",
                        )
                        reclaimed = kb.get_task(race_conn, task_id)
                        assert reclaimed is not None
                        assert reclaimed.claim_lock is None
                        assert reclaimed.current_run_id is None
                    return 2001
                return 2002

            first = kb.dispatch_once(conn, spawn_fn=spawn)
            assert first.spawned == []
            assert spawn_calls == [(task_id, initial_reviewer)]
            assert len(terminated) == 1
            assert terminated[0][0] == 2001
            assert terminated[0][1].startswith(f"{host}:")

            after_race = kb.get_task(conn, task_id)
            assert after_race is not None
            assert after_race.claim_lock is None
            assert after_race.current_run_id is None
            if mutation == "failover":
                assert after_race.assignee == "code-reviewer-b"
                expected_reviewer = "code-reviewer-b"
            else:
                assert after_race.assignee == initial_reviewer
                expected_reviewer = initial_reviewer
            assert after_race.worker_pid is None
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='spawned'",
                (task_id,),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND worker_pid=?",
                (task_id, 2001),
            ).fetchone()[0] == 0

            second = kb.dispatch_once(conn, spawn_fn=spawn)
            assert len(second.spawned) == 1
            assert second.spawned[0][:2] == (task_id, expected_reviewer)
            valid = kb.get_task(conn, task_id)
            assert valid is not None
            assert valid.assignee == expected_reviewer
            assert valid.worker_pid == 2002
            assert valid.current_run_id is not None
            assert spawn_calls == [(task_id, initial_reviewer), (task_id, expected_reviewer)]
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='spawned'",
                (task_id,),
            ).fetchone()[0] == 1

            if decision == "request_changes":
                decided = kb.request_changes(
                    conn,
                    task_id,
                    "programmer",
                    reviewer=expected_reviewer,
                    reason="stale child denied",
                    expected_claim=valid.claim_lock,
                    expected_run_id=valid.current_run_id,
                )
                replay = kb.request_changes(
                    conn,
                    task_id,
                    "programmer",
                    reviewer=expected_reviewer,
                    reason="stale child denied",
                    expected_claim=valid.claim_lock,
                    expected_run_id=valid.current_run_id,
                )
                terminal_kind = "changes_requested"
            else:
                decided = kb.approve_review(
                    conn,
                    task_id,
                    reviewer=expected_reviewer,
                    summary="stale child denied",
                    head_sha=HEAD_SHA,
                    expected_claim=valid.claim_lock,
                    expected_run_id=valid.current_run_id,
                )
                replay = kb.approve_review(
                    conn,
                    task_id,
                    reviewer=expected_reviewer,
                    summary="stale child denied",
                    head_sha=HEAD_SHA,
                    expected_claim=valid.claim_lock,
                    expected_run_id=valid.current_run_id,
                )
                terminal_kind = "review_approved"
            assert decided is not None and replay is not None
            assert _events(conn, task_id).count(terminal_kind) == 1
        finally:
            race_conn.close()


def test_dispatch_failover_does_not_reassign_when_authority_preflight_fails(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles,
        "list_profiles",
        lambda: [
            SimpleNamespace(name="code-reviewer-a"),
            SimpleNamespace(name="code-reviewer-b"),
            SimpleNamespace(name="code-reviewer-c"),
        ],
    )
    monkeypatch.setattr(
        profiles,
        "profile_exists",
        lambda name: name != "code-reviewer-b",
    )
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    spawned = []

    def spawn(task, workspace):
        spawned.append((task.id, task.assignee))
        return 1300 + len(spawned)

    with kb.connect() as conn:
        task_id, _, _ = _review_card(conn, reviewer="code-reviewer-a")
        assert kb.failover_review_task(
            conn,
            task_id,
            "code-reviewer-b",
            error="reviewer-a timed out",
        )
        # Preflight must also use the B authority when an assignment-only
        # write has put the stale A profile back on the task row.
        assert kb.assign_task(conn, task_id, "code-reviewer-a")

        kb.dispatch_once(conn, spawn_fn=spawn)
        failed_over = kb.get_task(conn, task_id)
        assert failed_over is not None
        assert failed_over.status == "review"
        assert failed_over.assignee == "code-reviewer-c"
        assert failed_over.claim_lock is None
        assert spawned == []
        authority = kb._latest_reviewer_authority(conn, task_id)
        assert authority is not None and authority[1] == "code-reviewer-c"
        assert _events(conn, task_id).count("review_failover") == 2

        kb.dispatch_once(conn, spawn_fn=spawn)
        replacement = kb.get_task(conn, task_id)
        assert replacement is not None
        assert replacement.assignee == "code-reviewer-c"
        assert spawned == [(task_id, "code-reviewer-c")]


def test_sequential_review_failovers_keep_only_the_newest_authority(kanban_home):
    with kb.connect() as conn:
        task_id, first, host = _review_card(conn, reviewer="reviewer-a")
        second_task = kb.failover_review_task(
            conn, task_id, "reviewer-b", error="reviewer-a timed out",
        )
        assert second_task is not None
        second = kb.claim_review_task(
            conn, task_id, claimer=f"{host}:reviewer-b",
        )
        assert second is not None

        third_task = kb.failover_review_task(
            conn, task_id, "reviewer-c", error="reviewer-b protocol violation",
        )
        assert third_task is not None
        third = kb.claim_review_task(
            conn, task_id, claimer=f"{host}:reviewer-c",
        )
        assert third is not None
        authority = kb._latest_reviewer_authority(conn, task_id)
        assert authority is not None and authority[1] == "reviewer-c"

        with pytest.raises(
            RuntimeError,
            match="reviewer generation|reviewer lane|active review run",
        ):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="reviewer-b",
                reason="stale successor feedback",
                expected_claim=second.claim_lock,
                expected_run_id=second.current_run_id,
            )

        approved = kb.approve_review(
            conn,
            task_id,
            reviewer="reviewer-c",
            summary="newest authority approval",
            head_sha=HEAD_SHA,
            expected_claim=third.claim_lock,
            expected_run_id=third.current_run_id,
        )
        assert approved is not None and approved.status == "ready"
        assert _events(conn, task_id).count("review_failover") == 2
        assert first.current_run_id != second.current_run_id != third.current_run_id


def test_cli_recovers_stranded_review_only_from_latest_reviewer_verdict(kanban_home):
    from hermes_cli import kanban

    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn, reviewer="reviewer-a")
        assert kb.reclaim_task(conn, task_id, reason="dead reviewer")
        stranded = kb.get_task(conn, task_id)
        assert stranded is not None
        assert stranded.status == "review"
        assert stranded.claim_lock is None
        assert stranded.current_run_id is None
        kb.add_comment(conn, task_id, "reviewer-a", "REQUEST_CHANGES: fix the failing test")

    output = kanban.run_slash(
        f"request-changes {task_id} programmer replay the recorded "
        "REQUEST_CHANGES verdict --recover --reviewer reviewer-a",
    )
    assert output.startswith(f"Requested changes on {task_id}")

    with kb.connect() as conn:
        recovered = kb.get_task(conn, task_id)
        assert recovered is not None
        assert recovered.status == "ready"
        assert recovered.assignee == "programmer"
        assert recovered.claim_lock is None
        assert recovered.current_run_id is None
        assert _payload(conn, task_id, "changes_requested")["recovered"] is True
        assert _events(conn, task_id).count("changes_requested") == 1

        with pytest.raises(RuntimeError, match="not active|terminal|authority|status"):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="reviewer-a",
                reason="duplicate recovery",
                trusted_operator=True,
                recovery=True,
            )


def test_stranded_review_recovery_rejects_live_claim_missing_evidence_and_wrong_owner(
    kanban_home,
):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn, reviewer="reviewer-a")
        with pytest.raises(RuntimeError, match="run or claim is still active"):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="reviewer-a",
                reason="unsafe live recovery",
                trusted_operator=True,
                recovery=True,
            )

        assert kb.reclaim_task(conn, task_id, reason="dead reviewer")
        with pytest.raises(RuntimeError, match="literal REQUEST_CHANGES"):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="reviewer-a",
                reason="missing evidence",
                trusted_operator=True,
                recovery=True,
            )

        kb.add_comment(conn, task_id, "reviewer-a", "REQUEST_CHANGES: recorded verdict")
        with pytest.raises(RuntimeError, match="original implementation owner"):
            kb.request_changes(
                conn,
                task_id,
                "other-programmer",
                reviewer="reviewer-a",
                reason="wrong owner",
                trusted_operator=True,
                recovery=True,
            )
        with pytest.raises(RuntimeError, match="latest authoritative generation"):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="reviewer-b",
                reason="wrong reviewer",
                trusted_operator=True,
                recovery=True,
            )


def test_stale_review_run_invalidates_prior_head_approval(kanban_home):
    """A replacement review run must invalidate an approval from the old run.

    Reusing the same reviewer profile and claim string is intentional: claim
    identity alone cannot distinguish the stale process, so the active run id
    is the CAS boundary.
    """
    with kb.connect() as conn:
        task_id, first, host = _review_card(conn, reviewer="code-reviewer")
        old_run_id = first.current_run_id
        old_claim = first.claim_lock
        assert old_run_id is not None
        assert old_claim is not None

        failed = kb.failover_review_task(
            conn, task_id, "code-reviewer", error="reviewer process replaced",
        )
        assert failed is not None
        assert failed.status == "blocked"
        assert failed.block_kind == "capability"

        with pytest.raises(RuntimeError, match="current review run"):
            kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer",
                summary="stale approval",
                head_sha=HEAD_SHA,
                expected_claim=old_claim,
                expected_run_id=old_run_id,
            )
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "blocked"
        assert current.current_run_id is None
        assert "review_approved" not in _events(conn, task_id)


def test_repeated_approval_is_idempotent(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        approved = kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="approved once",
            head_sha=HEAD_SHA,
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert approved is not None
        before = _events(conn, task_id)

        retried = kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="approved once",
            head_sha=HEAD_SHA,
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert retried is not None
        assert retried.status == "ready"
        assert _events(conn, task_id) == before
        assert _events(conn, task_id).count("review_approved") == 1


def test_old_approval_retry_is_rejected_after_identical_new_run_decision(
    kanban_home,
):
    with kb.connect() as conn:
        task_id, first, host = _review_card(conn)
        first_run_id = first.current_run_id
        assert first_run_id is not None
        assert kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="same decision",
            head_sha=HEAD_SHA,
            expected_claim=first.claim_lock,
            expected_run_id=first_run_id,
        ) is not None
        second = _start_followup_review(conn, task_id, host=host)
        assert second.current_run_id != first_run_id
        assert kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="same decision",
            head_sha=HEAD_SHA,
            expected_claim=second.claim_lock,
            expected_run_id=second.current_run_id,
        ) is not None
        before = _events(conn, task_id)
        with pytest.raises(RuntimeError, match="current review run|terminal"):
            kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer",
                summary="same decision",
                head_sha=HEAD_SHA,
                expected_claim=first.claim_lock,
                expected_run_id=first_run_id,
            )
        assert _events(conn, task_id) == before
        assert _events(conn, task_id).count("review_approved") == 2


def test_repeated_request_changes_is_idempotent(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        corrected = kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason="tighten error handling",
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert corrected is not None
        assert corrected.status == "ready"
        before = _events(conn, task_id)

        retried = kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason="tighten error handling",
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert retried is not None
        assert retried.status == "ready"
        assert _events(conn, task_id) == before
        assert _events(conn, task_id).count("changes_requested") == 1


def test_old_request_changes_retry_is_rejected_after_identical_new_run_decision(
    kanban_home,
):
    with kb.connect() as conn:
        task_id, first, host = _review_card(conn)
        first_run_id = first.current_run_id
        assert first_run_id is not None
        reason = "same correction packet"
        assert kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason=reason,
            expected_claim=first.claim_lock,
            expected_run_id=first_run_id,
        ) is not None
        second = _start_followup_review(conn, task_id, host=host)
        assert second.current_run_id != first_run_id
        assert kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason=reason,
            expected_claim=second.claim_lock,
            expected_run_id=second.current_run_id,
        ) is not None
        before = _events(conn, task_id)
        with pytest.raises(RuntimeError, match="current review run|terminal"):
            kb.request_changes(
                conn,
                task_id,
                "programmer",
                reviewer="code-reviewer",
                reason=reason,
                expected_claim=first.claim_lock,
                expected_run_id=first_run_id,
            )
        assert _events(conn, task_id) == before
        assert _events(conn, task_id).count("changes_requested") == 2


def test_approval_leaves_no_dead_pid_for_crash_reaper(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        reviewer_pid = 555001
        kb._set_worker_pid(conn, task_id, reviewer_pid)
        approved = kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="approved",
            head_sha=HEAD_SHA,
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert approved is not None
        before = _events(conn, task_id)
        assert kb.detect_crashed_workers(conn) == []
        after = _events(conn, task_id)
        assert after == before + ["recovery_required"]
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.worker_pid == reviewer_pid
        assert current.current_run_id == review.current_run_id
        assert current.claim_lock == review.claim_lock
        assert current.recovery_required is True
        assert current.last_failure_error is not None
        assert "protocol_violation" not in after


def test_approved_card_is_claimable_by_fresh_finalizer(
    kanban_home, all_assignees_spawnable,
):
    with kb.connect() as conn:
        task_id, review, host = _review_card(conn)
        approved = kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="approved",
            head_sha=HEAD_SHA,
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert approved is not None
        finalizer = kb.claim_task(conn, task_id, claimer=f"{host}:finalizer")
        assert finalizer is not None
        assert finalizer.status == "running"
        assert finalizer.assignee == "programmer"
        assert finalizer.current_run_id != review.current_run_id


def test_approval_and_change_lanes_are_mutually_exclusive(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        kb.request_changes(
            conn,
            task_id,
            "programmer",
            reviewer="code-reviewer",
            reason="fix tests",
            expected_claim=review.claim_lock,
            expected_run_id=review.current_run_id,
        )
        assert kb.in_correction_lane(conn, task_id) is True
        assert kb.in_finalization_lane(conn, task_id) is False

        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, task_id, claimer=f"{host}:implementation-2")
        assert kb.submit_task_for_review(
            conn, task_id, "code-reviewer", trusted_operator=True,
        )
        second_review = kb.claim_review_task(
            conn, task_id, claimer=f"{host}:review-2",
        )
        assert second_review is not None
        kb.approve_review(
            conn,
            task_id,
            reviewer="code-reviewer",
            summary="approved",
            head_sha=HEAD_SHA,
            expected_claim=second_review.claim_lock,
            expected_run_id=second_review.current_run_id,
        )
        assert kb.in_finalization_lane(conn, task_id) is True
        assert kb.in_correction_lane(conn, task_id) is False


def test_worker_toolset_exposes_result_aware_review_transitions(
    kanban_home, monkeypatch,
):
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset
    import tools.kanban_tools  # noqa: F401 - registration side effect

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review_worker")
    invalidate_check_fn_cache()
    names = {
        definition["function"]["name"]
        for definition in registry.get_definitions(
            set(resolve_toolset("kanban")), quiet=True,
        )
        if "function" in definition
    }
    assert "kanban_approve" in names
    assert "kanban_request_changes" in names


def test_cli_help_exposes_review_decisions(kanban_home):
    import argparse

    from hermes_cli import kanban
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    kanban_parser = kanban.build_parser(subparsers)
    choices = kanban_parser._subparsers._group_actions[0].choices
    assert "approve" in choices
    assert "request-changes" in choices
    assert hasattr(kanban, "_cmd_approve") or hasattr(kanban, "_cmd_review")
    assert hasattr(kanban, "_cmd_request_changes")


def test_claimed_review_counts_against_global_concurrency_cap(
    kanban_home, all_assignees_spawnable,
):
    with kb.connect() as conn:
        _review_card(conn)
        ready_id = kb.create_task(
            conn, title="blocked by review cap", assignee="programmer",
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            max_spawn=1,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        assert spawned == []
        assert result.spawned == []
        ready = kb.get_task(conn, ready_id)
        assert ready is not None
        assert ready.status == "ready"


def test_claimed_review_counts_against_profile_concurrency_cap(
    kanban_home, all_assignees_spawnable,
):
    with kb.connect() as conn:
        _review_card(conn, reviewer="code-reviewer")
        ready_id = kb.create_task(
            conn, title="same reviewer profile", assignee="code-reviewer",
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            max_spawn=5,
            max_in_progress_per_profile=1,
            spawn_fn=lambda task, workspace: spawned.append(task.id),
        )
        assert spawned == []
        assert (ready_id, "code-reviewer", 1) in result.skipped_per_profile_capped


def test_review_max_runtime_fails_over_to_alternate_reviewer(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr(
        kb, "_reviewer_candidates",
        lambda current: ([current, "reviewer-b"], []),
    )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        started_at = int(time.time()) - 120
        conn.execute(
            "UPDATE tasks SET worker_pid=?, max_runtime_seconds=?, started_at=? WHERE id=?",
            (551001, 1, started_at, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET started_at=? WHERE id=?",
            (started_at, review.current_run_id),
        )
        conn.commit()
        assert task_id in kb.enforce_max_runtime(
            conn, signal_fn=lambda _pid, _sig: None,
        )
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.assignee == "reviewer-b"
        assert current.current_run_id is None
        assert "review_failover" in _events(conn, task_id)
        closed = [r for r in kb.list_runs(conn, task_id) if r.outcome == "timed_out"]
        assert len(closed) == 1
        attempted = _payload(conn, task_id, "review_failover")["attempted"]
        assert any(
            item["profile"] == "code-reviewer" and item["error"] == closed[0].error
            for item in attempted
        )


def test_review_crash_fails_over_with_closed_run_evidence(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr(
        kb, "_reviewer_candidates",
        lambda current: ([current, "reviewer-b"], []),
    )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        started_at = int(time.time()) - 120
        conn.execute(
            "UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?",
            (551000, started_at, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET started_at=? WHERE id=?",
            (started_at, review.current_run_id),
        )
        conn.commit()
        assert task_id in kb.detect_crashed_workers(conn)
        current = kb.get_task(conn, task_id)
        assert current is not None and current.assignee == "reviewer-b"
        closed = [r for r in kb.list_runs(conn, task_id) if r.outcome == "crashed"]
        assert len(closed) == 1
        attempted = _payload(conn, task_id, "review_failover")["attempted"]
        assert any(
            item["profile"] == "code-reviewer" and item["error"] == closed[0].error
            for item in attempted
        )


def test_review_max_runtime_blocks_after_same_reviewer_exhaustion(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr(
        kb, "_reviewer_candidates", lambda current: ([current], []),
    )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        started_at = int(time.time()) - 120
        conn.execute(
            "UPDATE tasks SET worker_pid=?, max_runtime_seconds=?, started_at=? WHERE id=?",
            (551002, 1, started_at, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET started_at=? WHERE id=?",
            (started_at, review.current_run_id),
        )
        conn.commit()
        kb.enforce_max_runtime(conn, signal_fn=lambda _pid, _sig: None)
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "blocked"
        assert current.block_kind == "capability"
        assert current.assignee is None
        closed = [r for r in kb.list_runs(conn, task_id) if r.outcome == "timed_out"]
        assert len(closed) == 1
        attempted = _payload(conn, task_id, "review_lanes_failed")["attempted"]
        assert any(
            item["profile"] == "code-reviewer" and item["error"] == closed[0].error
            for item in attempted
        )


def test_review_heartbeat_stale_fails_over_to_alternate_reviewer(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr(
        kb, "_reviewer_candidates",
        lambda current: ([current, "reviewer-b"], []),
    )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        old = int(time.time()) - 7200
        conn.execute(
            "UPDATE tasks SET worker_pid=?, started_at=?, last_heartbeat_at=? WHERE id=?",
            (551003, old, old, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET started_at=? WHERE id=?",
            (old, review.current_run_id),
        )
        conn.commit()
        assert task_id in kb.detect_stale_running(
            conn, stale_timeout_seconds=1,
        )
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.assignee == "reviewer-b"
        assert "review_failover" in _events(conn, task_id)
        closed = [r for r in kb.list_runs(conn, task_id) if r.outcome == "stale"]
        assert len(closed) == 1
        attempted = _payload(conn, task_id, "review_failover")["attempted"]
        assert any(
            item["profile"] == "code-reviewer" and item["error"] == closed[0].error
            for item in attempted
        )


# ---------------------------------------------------------------------------
# Compatible-programmer transfer on the same card
# ---------------------------------------------------------------------------

def _make_profile(name: str) -> None:
    """Materialize a *configured* profile under the temp home.

    A configured profile is a directory holding a ``config.yaml``; a bare
    directory is deliberately not enough to receive a transferred card.
    """
    directory = Path.home() / ".hermes" / "profiles" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.yaml").write_text(f"profile: {name}\n", encoding="utf-8")


def _request_changes(conn, task_id, review, programmer, *, reason="transfer"):
    return kb.request_changes(
        conn, task_id, programmer, reviewer="code-reviewer", reason=reason,
        expected_claim=review.claim_lock, expected_run_id=review.current_run_id,
    )


def test_request_changes_transfers_to_compatible_programmer(kanban_home):
    _make_profile("programmer-luna")
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)

        corrected = _request_changes(conn, task_id, review, "programmer-luna")

        assert corrected is not None
        assert corrected.status == "ready"
        assert corrected.assignee == "programmer-luna"
        assert _payload(conn, task_id, "changes_requested")["programmer"] == (
            "programmer-luna"
        )


def test_transfer_retry_is_idempotent_and_binds_to_replacement(kanban_home):
    _make_profile("programmer-luna")
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        assert _request_changes(conn, task_id, review, "programmer-luna")
        # Complete post-transfer state: the replay must not change a single
        # column of tasks/task_runs/task_events.
        after = _snapshot(conn, task_id)

        replay = _request_changes(conn, task_id, review, "programmer-luna")

        assert replay is not None
        assert replay.assignee == "programmer-luna"
        assert _snapshot(conn, task_id) == after

        # The retry binds to the replacement, not the original owner, and
        # the rejected owner-mismatch retry is fully zero-mutation too.
        with pytest.raises(RuntimeError):
            _request_changes(conn, task_id, review, "programmer")
        assert _snapshot(conn, task_id) == after


def test_same_owner_request_changes_still_works(kanban_home):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)

        corrected = _request_changes(conn, task_id, review, "programmer")

        assert corrected is not None
        assert corrected.status == "ready"
        assert corrected.assignee == "programmer"
        assert _payload(conn, task_id, "changes_requested")["programmer"] == (
            "programmer"
        )


@pytest.mark.parametrize(
    "candidate",
    ["orchestrator", "code-reviewer", "code-reviewer-b", "programmer-",
     "programmer luna", "../programmer", "programmer/luna", "Programmer Luna"],
)
def test_incompatible_programmer_is_rejected_without_mutation(
    kanban_home, candidate,
):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        before = _snapshot(conn, task_id)

        with pytest.raises((RuntimeError, ValueError)):
            _request_changes(conn, task_id, review, candidate)

        _assert_no_mutation(conn, task_id, before, review)
        current = kb.get_task(conn, task_id)
        assert current is not None and current.assignee == "code-reviewer"


@pytest.mark.parametrize("candidate", ["", "   ", None])
def test_blank_programmer_is_rejected_without_mutation(kanban_home, candidate):
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        before = _snapshot(conn, task_id)

        with pytest.raises((RuntimeError, ValueError)):
            _request_changes(conn, task_id, review, candidate)

        _assert_no_mutation(conn, task_id, before, review)


def test_unknown_compatible_programmer_profile_is_rejected(kanban_home):
    """Role-shaped but nonexistent profiles fail closed before mutation."""
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError):
            _request_changes(conn, task_id, review, "programmer-ghost")

        _assert_no_mutation(conn, task_id, before, review)


def test_unconfigured_programmer_directory_is_rejected(kanban_home):
    """A bare, role-shaped profile directory is not a configured profile."""
    bare = Path.home() / ".hermes" / "profiles" / "programmer-empty"
    bare.mkdir(parents=True)
    assert not (bare / "config.yaml").exists()

    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="unavailable"):
            _request_changes(conn, task_id, review, "programmer-empty")

        _assert_no_mutation(conn, task_id, before, review)
        current = kb.get_task(conn, task_id)
        assert current is not None and current.assignee == "code-reviewer"


def test_role_shaped_but_not_programmer_lane_is_rejected(kanban_home):
    """``programmerfoo`` only looks like the lane; it must be rejected."""
    _make_profile("programmerfoo")
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="compatible programmer"):
            _request_changes(conn, task_id, review, "programmerfoo")

        _assert_no_mutation(conn, task_id, before, review)
        current = kb.get_task(conn, task_id)
        assert current is not None and current.assignee == "code-reviewer"


def test_transfer_still_requires_run_claim_and_reviewer_lane(kanban_home):
    _make_profile("programmer-luna")
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        before = _snapshot(conn, task_id)

        # Stale run id.
        with pytest.raises(RuntimeError):
            kb.request_changes(
                conn, task_id, "programmer-luna", reviewer="code-reviewer",
                reason="transfer", expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id + 991,
            )
        # Wrong claim.
        with pytest.raises(RuntimeError):
            kb.request_changes(
                conn, task_id, "programmer-luna", reviewer="code-reviewer",
                reason="transfer", expected_claim="someone-else:review",
                expected_run_id=review.current_run_id,
            )
        # Non-reviewer caller.
        with pytest.raises(RuntimeError):
            kb.request_changes(
                conn, task_id, "programmer-luna", reviewer="programmer",
                reason="transfer", expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )

        _assert_no_mutation(conn, task_id, before, review)


def test_transferred_programmer_becomes_finalizer_after_approval(kanban_home):
    _make_profile("programmer-luna")
    with kb.connect() as conn:
        task_id, review, host = _review_card(conn)
        assert _request_changes(conn, task_id, review, "programmer-luna")

        follow_up = kb.claim_task(conn, task_id, claimer=f"{host}:impl-2")
        assert follow_up is not None
        assert follow_up.assignee == "programmer-luna"
        assert kb.submit_task_for_review(
            conn, task_id, "code-reviewer", trusted_operator=True,
        ) is not None
        second = kb.claim_review_task(conn, task_id, claimer=f"{host}:review-2")
        assert second is not None

        assert kb._resolve_finalizer(
            conn, task_id, reviewer="code-reviewer",
        ) == "programmer-luna"

        approved = kb.approve_review(
            conn, task_id, reviewer="code-reviewer", summary="looks good",
            head_sha=HEAD_SHA, expected_claim=second.claim_lock,
            expected_run_id=second.current_run_id,
        )
        assert approved is not None
        assert approved.assignee == "programmer-luna"


# ---------------------------------------------------------------------------
# Strict configured-profile admission (request_changes + dispatcher)
# ---------------------------------------------------------------------------

def _write_profile_config(name: str, body: str) -> Path:
    directory = Path.home() / ".hermes" / "profiles" / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _break_profiles_import(monkeypatch) -> None:
    """Make ``from hermes_cli.profiles import ...`` fail deterministically.

    Simulates a partial install / exotic environment: the module object is
    present but exposes none of the profile helpers, so every helper import
    raises ``ImportError``. The lifecycle must fail closed, and callers must
    never see the raw ``ImportError`` (the tool handler only catches
    ``RuntimeError``/``ValueError``).
    """
    import sys
    import types

    stub = types.ModuleType("hermes_cli.profiles")
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", stub)


def test_malformed_yaml_replacement_profile_is_rejected(kanban_home):
    """``config.yaml`` that is not parsable YAML is not a configured profile."""
    _write_profile_config("programmer-bad", "[invalid yaml\n")
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="unavailable"):
            _request_changes(conn, task_id, review, "programmer-bad")

        _assert_no_mutation(conn, task_id, before, review)
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "review"
        assert current.assignee == "code-reviewer"


@pytest.mark.parametrize(
    "body", ["just a string\n", "- one\n- two\n", "", "null\n", "42\n"],
)
def test_non_mapping_yaml_replacement_profile_is_rejected(kanban_home, body):
    """A ``config.yaml`` that does not parse to a mapping is rejected."""
    _write_profile_config("programmer-scalar", body)
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        before = _snapshot(conn, task_id)

        with pytest.raises(RuntimeError, match="unavailable"):
            _request_changes(conn, task_id, review, "programmer-scalar")

        _assert_no_mutation(conn, task_id, before, review)


def test_request_changes_fails_closed_when_profile_helpers_unimportable(
    kanban_home, monkeypatch,
):
    """A broken profiles module must not leak ``ImportError`` out of admission."""
    _make_profile("programmer-luna")
    with kb.connect() as conn:
        task_id, review, _ = _review_card(conn)
        before = _snapshot(conn, task_id)

        _break_profiles_import(monkeypatch)
        with pytest.raises((RuntimeError, ValueError)) as excinfo:
            _request_changes(conn, task_id, review, "programmer-luna")
        assert not isinstance(excinfo.value, ImportError)

        _assert_no_mutation(conn, task_id, before, review)


# --- dispatcher admission ---------------------------------------------------

def _ready_transfer_card(conn):
    """Return a task id parked in ``ready`` and owned by ``programmer-luna``."""
    _make_profile("programmer-luna")
    task_id, review, _host = _review_card(conn)
    corrected = _request_changes(conn, task_id, review, "programmer-luna")
    assert corrected is not None and corrected.status == "ready"
    return task_id


class _SpawnRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return 4242


def test_dispatch_spawns_configured_transfer_target(kanban_home):
    """Baseline: with a real configured profile the transferred card spawns."""
    with kb.connect() as conn:
        task_id = _ready_transfer_card(conn)
        spawn = _SpawnRecorder()

        result = kb.dispatch_once(conn, spawn_fn=spawn, dry_run=False)

        assert len(spawn.calls) == 1
        assert [s[0] for s in result.spawned] == [task_id]
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "running"


def test_dispatch_never_claims_a_task_whose_config_disappeared(kanban_home):
    """Valid transfer, then the replacement's config.yaml is removed."""
    with kb.connect() as conn:
        task_id = _ready_transfer_card(conn)
        config = Path.home() / ".hermes" / "profiles" / "programmer-luna" / "config.yaml"
        config.unlink()
        assert config.parent.is_dir()  # bare directory survives
        before = _snapshot(conn, task_id)
        spawn = _SpawnRecorder()

        result = kb.dispatch_once(conn, spawn_fn=spawn, dry_run=False)

        assert spawn.calls == []
        assert result.spawned == []
        assert result.skipped_nonspawnable == [task_id]
        assert _snapshot(conn, task_id) == before
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "ready"


def test_dispatch_never_claims_a_task_whose_config_is_malformed(kanban_home):
    """Same fence when the config is replaced with unparsable YAML."""
    with kb.connect() as conn:
        task_id = _ready_transfer_card(conn)
        _write_profile_config("programmer-luna", "[invalid yaml\n")
        before = _snapshot(conn, task_id)
        spawn = _SpawnRecorder()

        result = kb.dispatch_once(conn, spawn_fn=spawn, dry_run=False)

        assert spawn.calls == []
        assert result.skipped_nonspawnable == [task_id]
        assert _snapshot(conn, task_id) == before


def test_dispatch_fails_closed_when_profile_helpers_unimportable(
    kanban_home, monkeypatch,
):
    """A broken profiles module must never spawn or move ready -> running."""
    with kb.connect() as conn:
        task_id = _ready_transfer_card(conn)
        before = _snapshot(conn, task_id)
        spawn = _SpawnRecorder()

        _break_profiles_import(monkeypatch)
        result = kb.dispatch_once(conn, spawn_fn=spawn, dry_run=False)

        assert spawn.calls == []
        assert result.spawned == []
        assert result.skipped_nonspawnable == [task_id]
        assert _snapshot(conn, task_id) == before
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "ready"


# --- read-only telemetry ----------------------------------------------------

def test_has_spawnable_ready_requires_a_configured_profile(kanban_home):
    with kb.connect() as conn:
        task_id = _ready_transfer_card(conn)
        assert kb.has_spawnable_ready(conn) is True

        before = _snapshot(conn, task_id)
        (Path.home() / ".hermes" / "profiles" / "programmer-luna"
         / "config.yaml").unlink()
        assert kb.has_spawnable_ready(conn) is False

        _write_profile_config("programmer-luna", "[invalid yaml\n")
        assert kb.has_spawnable_ready(conn) is False

        # Telemetry is strictly read-only.
        assert _snapshot(conn, task_id) == before


def test_has_spawnable_review_requires_a_configured_profile(kanban_home):
    _make_profile("code-reviewer")
    with kb.connect() as conn:
        task_id, _review, _host = _review_card(conn)
        conn.execute(
            "UPDATE tasks SET claim_lock = NULL WHERE id = ?", (task_id,)
        )
        conn.commit()
        assert kb.has_spawnable_review(conn) is True

        before = _snapshot(conn, task_id)
        _write_profile_config("code-reviewer", "- not: a mapping\n")
        assert kb.has_spawnable_review(conn) is False
        assert _snapshot(conn, task_id) == before


def test_telemetry_fails_closed_when_profile_helpers_unimportable(
    kanban_home, monkeypatch,
):
    with kb.connect() as conn:
        _ready_transfer_card(conn)
        assert kb.has_spawnable_ready(conn) is True

        _break_profiles_import(monkeypatch)
        assert kb.has_spawnable_ready(conn) is False
        assert kb.has_spawnable_review(conn) is False

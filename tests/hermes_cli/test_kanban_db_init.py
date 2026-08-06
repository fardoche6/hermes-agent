from __future__ import annotations

import sqlite3
import subprocess
import threading
from pathlib import Path

from hermes_cli import kanban_db as kb


def _make_legacy_db(path: Path) -> None:
    """Write a kanban DB with the pre-AUTOINCREMENT (TEXT PK) schema for the
    four tables #35096 affects, keeping every other table current so the
    additive-column migration runs cleanly on top.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(kb.SCHEMA_SQL)
    conn.executescript(
        """
        DROP TABLE task_events;
        DROP TABLE task_comments;
        DROP TABLE task_runs;
        DROP TABLE kanban_notify_subs;
        CREATE TABLE task_comments (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE task_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            profile TEXT, status TEXT NOT NULL, started_at INTEGER NOT NULL);
        CREATE TABLE kanban_notify_subs (task_id TEXT NOT NULL, platform TEXT NOT NULL,
            chat_id TEXT NOT NULL, thread_id TEXT NOT NULL DEFAULT '', user_id TEXT,
            created_at INTEGER NOT NULL, last_event_id TEXT,
            PRIMARY KEY (task_id, platform, chat_id, thread_id));
        """
    )
    conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('task-1', 'T', 'done', 1000)")
    conn.execute("INSERT INTO task_comments VALUES ('c-1', 'task-1', 'agent', 'hi', 1500)")
    conn.execute("INSERT INTO task_events VALUES ('e-1', 'task-1', 'completed', NULL, 2000)")
    conn.execute("INSERT INTO task_events VALUES ('e-2', 'task-1', 'blocked', NULL, 2100)")
    conn.execute("INSERT INTO task_runs VALUES ('r-1', 'task-1', 'default', 'done', 1000)")
    conn.execute(
        "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at, last_event_id) "
        "VALUES ('task-1', 'telegram', '123', 1000, 'e-1')"
    )
    conn.commit()
    conn.close()


def _setup_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="legacy")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    return db_path


def _table_struct(conn: sqlite3.Connection, table: str):
    column_specs = [
        (
            r["name"],
            (r["type"] or "").upper(),
            r["notnull"],
            r["dflt_value"],
            r["pk"],
        )
        for r in conn.execute(f"PRAGMA table_info({table})")
    ]
    index_specs = sorted(
        (
            r["name"],
            r["unique"],
            r["origin"],
            r["partial"],
            tuple(
                c["name"]
                for c in conn.execute(f"PRAGMA index_info({r['name']})")
            ),
        )
        for r in conn.execute(f"PRAGMA index_list({table})")
        if not r["name"].startswith("sqlite_")
    )
    return column_specs, index_specs




def test_legacy_text_pk_tables_rebuilt_to_integer_autoincrement(tmp_path, monkeypatch):
    """A pre-AUTOINCREMENT DB is migrated in place: id columns become INTEGER
    PKs, ``last_event_id`` becomes INTEGER, data is preserved, and indexes
    are recreated (DROP TABLE would otherwise take them down)."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)

    with kb.connect(db_path) as conn:
        for table in ("task_events", "task_comments", "task_runs"):
            id_col = {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})")}["id"]
            assert id_col["type"].upper() == "INTEGER" and id_col["pk"] == 1

        lei = {r["name"]: r for r in conn.execute("PRAGMA table_info(kanban_notify_subs)")}
        assert lei["last_event_id"]["type"].upper() == "INTEGER"
        assert "delivery_metadata" in lei

        # The rebuilt run table must have the exact current column/default/index
        # shape, not just an INTEGER primary key. This catches rebuild specs
        # that lag SCHEMA_SQL as new durable run fields are added.
        fresh = sqlite3.connect(":memory:")
        fresh.row_factory = sqlite3.Row
        try:
            fresh.executescript(kb.SCHEMA_SQL)
            assert _table_struct(conn, "task_runs") == _table_struct(
                fresh, "task_runs"
            )
        finally:
            fresh.close()

        # Data preserved across the rebuild.
        assert len(conn.execute("SELECT * FROM task_events").fetchall()) == 2
        assert conn.execute("SELECT body FROM task_comments").fetchone()["body"] == "hi"
        assert len(conn.execute("SELECT * FROM task_runs").fetchall()) == 1
        legacy_run = conn.execute(
            "SELECT task_id, profile, status, started_at, recovery_required, "
            "worker_boot_id, worker_starttime FROM task_runs WHERE task_id=?",
            ("task-1",),
        ).fetchone()
        assert legacy_run is not None
        assert (
            legacy_run["task_id"],
            legacy_run["profile"],
            legacy_run["status"],
            legacy_run["started_at"],
            legacy_run["recovery_required"],
            legacy_run["worker_boot_id"],
            legacy_run["worker_starttime"],
        ) == ("task-1", "default", "done", 1000, 0, None, None)
        # Non-numeric legacy cursor ("e-1") casts to 0.
        assert conn.execute("SELECT last_event_id FROM kanban_notify_subs").fetchone()["last_event_id"] == 0

        # The rebuilt identity columns must support the same reviewer PID/run
        # binding used by the production dispatch path.
        review_task_id = kb.create_task(
            conn, title="identity-bound review", assignee="programmer"
        )
        assert kb.claim_task(
            conn, review_task_id, claimer="legacy-test:implementation"
        ) is not None
        assert kb.submit_task_for_review(
            conn, review_task_id, "code-reviewer", trusted_operator=True
        ) is not None
        review = kb.claim_review_task(
            conn, review_task_id, claimer="legacy-test:review"
        )
        assert review is not None
        process = subprocess.Popen(["sleep", "30"])
        try:
            captured = kb._capture_process_handle(process.pid)
            assert captured is not None
            identity = captured.identity
            captured.close()
            authority = kb._latest_unconsumed_reviewer_authority(
                conn, review_task_id
            )
            assert authority is not None
            assert kb._set_worker_pid(
                conn,
                review_task_id,
                process.pid,
                expected_run_id=review.current_run_id,
                expected_claim=review.claim_lock,
                expected_assignee=review.assignee,
                expected_authority=authority,
            )
            task_bound = conn.execute(
                "SELECT worker_pid, worker_boot_id, worker_starttime "
                "FROM tasks WHERE id=?",
                (review_task_id,),
            ).fetchone()
            run_bound = conn.execute(
                "SELECT worker_pid, worker_boot_id, worker_starttime "
                "FROM task_runs WHERE id=?",
                (review.current_run_id,),
            ).fetchone()
            for bound in (task_bound, run_bound):
                assert bound is not None
                assert (
                    bound["worker_pid"],
                    bound["worker_boot_id"],
                    bound["worker_starttime"],
                ) == (process.pid, identity.boot_id, identity.starttime)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

        # Indexes restored, including idx_events_run (added by the additive pass).
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for name in ("idx_events_task", "idx_events_run", "idx_comments_task",
                     "idx_runs_task", "idx_runs_status", "idx_notify_task"):
            assert name in indexes

        # AUTOINCREMENT actually works after the rebuild.
        conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('task-1', 'completed', 3000)")
        new_id = conn.execute("SELECT id FROM task_events ORDER BY id DESC LIMIT 1").fetchone()["id"]
        assert isinstance(new_id, int) and new_id >= 1




def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Re-opening an already-migrated DB is a no-op and leaves data intact."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)

    with kb.connect(db_path):
        pass
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path) as conn:
        id_col = {r["name"]: r for r in conn.execute("PRAGMA table_info(task_events)")}["id"]
        assert id_col["type"].upper() == "INTEGER"
        assert len(conn.execute("SELECT * FROM task_events").fetchall()) == 2


def test_unseen_events_for_sub_survives_migrated_db(tmp_path, monkeypatch):
    """The crash that motivated #35096 — ``int(None)`` on a NULL cursor — is
    gone after migration; the notifier query returns an integer cursor."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)

    with kb.connect(db_path) as conn:
        cursor, events = kb.unseen_events_for_sub(
            conn, task_id="task-1", platform="telegram", chat_id="123"
        )
        assert isinstance(cursor, int)
        assert isinstance(events, list)

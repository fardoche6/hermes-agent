"""Tests: redact_sensitive_text is applied in kanban tool handlers.

Verifies that secrets embedded in kanban_comment body, kanban_complete
summary/result/metadata, and kanban_block reason are masked before the
values reach the DB.  Uses the same worker_env fixture pattern as
test_kanban_tools.py.
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Shared fixture — mirrors test_kanban_tools.py
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Isolated HERMES_HOME with a running task; returns the task id."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


@pytest.fixture
def review_env(worker_env):
    """Convert the worker fixture's implementation run into a review run."""
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        assert kb.submit_task_for_review(
            conn, worker_env, "code-reviewer", trusted_operator=True,
        ) is not None
        review = kb.claim_review_task(
            conn, worker_env, claimer="test-host:review",
        )
        assert review is not None
        return worker_env, review
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Positive tests — secrets are masked
# ---------------------------------------------------------------------------

def test_kanban_comment_body_scrubbed_github_pat(worker_env):
    """ghp_ PAT in comment body must be masked before DB write."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    secret = "ghp_" + "A" * 40
    kt._handle_comment({"task_id": worker_env, "body": f"token: {secret}"})
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
    finally:
        conn.close()
    assert comments, "expected at least one comment"
    stored = comments[-1].body
    assert secret not in stored
    assert stored  # something was stored


def test_kanban_block_reason_scrubbed_jwt(worker_env):
    """JWT in block reason must be masked before DB write."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    # Minimal valid-ish JWT (header.payload.sig)
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dozjgNryP4J3jVmNHl0w5N_5NjP1-iXkpHgcth826Iw"
    )
    kt._handle_block({"reason": f"Bearer {jwt}"})
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
    finally:
        conn.close()
    # block_task stores reason as run.summary
    assert run is not None
    stored = run.summary or ""
    assert jwt not in stored


# ---------------------------------------------------------------------------
# Negative test — plain text passes through unchanged
# ---------------------------------------------------------------------------

def test_kanban_comment_no_secret_passthrough(worker_env):
    """Plain text without credential patterns must pass through unchanged."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    plain = "hello from the pipeline — no secrets here"
    kt._handle_comment({"task_id": worker_env, "body": plain})
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
    finally:
        conn.close()
    stored = comments[-1].body
    assert stored == plain


# ---------------------------------------------------------------------------
# Negative test — force=True bypasses HERMES_REDACT_SECRETS=false
# ---------------------------------------------------------------------------

def test_scrub_respects_force_flag_regardless_of_config(worker_env, monkeypatch):
    """force=True must fire even when HERMES_REDACT_SECRETS=false is set."""
    monkeypatch.setenv("HERMES_REDACT_SECRETS", "false")
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    secret = "ghp_" + "C" * 40
    kt._handle_comment({"task_id": worker_env, "body": f"token: {secret}"})
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
    finally:
        conn.close()
    stored = comments[-1].body
    assert secret not in stored


# ---------------------------------------------------------------------------
# Negative test — legacy result field is also scrubbed
# ---------------------------------------------------------------------------

def test_kanban_complete_result_field_scrubbed(worker_env):
    """Legacy result field must be scrubbed just like summary."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    secret = "sk-" + "D" * 48
    kt._handle_complete({"result": f"finished with key={secret}"})
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
    finally:
        conn.close()
    assert run is not None
    stored = run.summary or run.result if hasattr(run, "result") else run.summary or ""
    assert secret not in (stored or "")


@pytest.mark.parametrize("decision", ["approve", "request_changes"])
def test_review_decision_persistence_scrubs_all_sensitive_text(review_env, decision):
    from hermes_cli import kanban_db as kb

    task_id, review = review_env
    bearer = "Bearer " + "A" * 40
    api_key = "sk-" + "B" * 48
    credential_url = "https://review-user:review-password@example.com/review"
    multiline = f"first line\nAuthorization: {bearer}\napi_key={api_key}\n{credential_url}"

    with kb.connect() as conn:
        if decision == "approve":
            updated = kb.approve_review(
                conn,
                task_id,
                reviewer="code-reviewer",
                summary=multiline,
                head_sha="855fd911d56e1c6185fda5995d8aff430d964191",
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
            event_kind = "review_approved"
        else:
            updated = kb.request_changes(
                conn,
                task_id,
                "test-worker",
                reviewer="code-reviewer",
                reason=multiline,
                expected_claim=review.claim_lock,
                expected_run_id=review.current_run_id,
            )
            event_kind = "changes_requested"
        assert updated is not None
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? "
            "ORDER BY id DESC LIMIT 1",
            (task_id, event_kind),
        ).fetchone()
        run = conn.execute(
            "SELECT summary, error, metadata FROM task_runs WHERE id=?",
            (review.current_run_id,),
        ).fetchone()
        stored = " ".join([
            event["payload"] or "",
            run["summary"] or "",
            run["error"] or "",
            run["metadata"] or "",
        ])
        for secret in (bearer, api_key, credential_url):
            assert secret not in stored
        assert "first line" in stored


def test_review_failover_persistence_scrubs_error_and_attempts(review_env):
    from hermes_cli import kanban_db as kb

    task_id, _review = review_env
    bearer = "Bearer " + "C" * 40
    api_key = "sk-" + "D" * 48
    credential_url = "https://user:password@example.com/failover"
    error = f"reviewer failed\nAuthorization: {bearer}\napi_key={api_key}\n{credential_url}"

    with kb.connect() as conn:
        failed = kb.failover_review_task(
            conn,
            task_id,
            None,
            error=error,
            attempted=[{"profile": "code-reviewer", "error": error}],
        )
        assert failed is not None
        event_rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        run_rows = conn.execute(
            "SELECT error, metadata FROM task_runs WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        task_row = conn.execute(
            "SELECT last_failure_error FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        stored = " ".join(
            [
                *(row["payload"] or "" for row in event_rows),
                *(row["error"] or "" for row in run_rows),
                *(row["metadata"] or "" for row in run_rows),
                task_row["last_failure_error"] or "",
            ]
        )
        for secret in (bearer, api_key, credential_url):
            assert secret not in stored

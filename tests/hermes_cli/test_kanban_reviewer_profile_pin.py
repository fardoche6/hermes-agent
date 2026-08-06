"""Behavioral coverage for the configured Kanban reviewer lane."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, cast

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture()
def reviewer_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    kb._INITIALIZED_PATHS.clear()

    from hermes_cli import config

    config._LOAD_CONFIG_CACHE.clear()
    config._RAW_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    return home


def _write_profile(home: Path, name: str, *, with_review_skill: bool = True) -> Path:
    profile_dir = home / "profiles" / name
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("model: test-model\n", encoding="utf-8")
    if with_review_skill:
        skill_dir = profile_dir / "skills" / "sdlc-review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# test review skill\n", encoding="utf-8")
    return profile_dir


def _write_reviewer_config(home: Path, value: object) -> None:
    import yaml

    (home / "config.yaml").write_text(
        yaml.safe_dump({"kanban": {"reviewer_profile": value}}, sort_keys=False),
        encoding="utf-8",
    )


def _run_policy_probe(hermes_home: Path, policy_home: Path) -> dict[str, object]:
    db_path = policy_home / "kanban.db"
    with kb.connect_closing(db_path=db_path) as conn:
        # This is the dispatcher-authoritative initialization.  The subprocess
        # below is the restarted worker/direct-CLI consumer.
        kb._synchronize_reviewer_policy(conn)
    script = "\n".join(
        (
            "import json",
            "from hermes_cli import kanban_db as kb",
            "with kb.connect_closing() as conn:",
            "    candidates, failures = kb._reviewer_candidates(None, conn=conn)",
            "    policy = kb._load_reviewer_policy(conn)",
            "print(json.dumps({'candidates': candidates, 'failures': failures, "
            "'state': policy.state.value, 'version': policy.policy_version}))",
        )
    )
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["HERMES_KANBAN_DB"] = str(policy_home / "kanban.db")
    env["HERMES_KANBAN_HOME"] = str(
        policy_home.parent / "worker-controlled-kanban-home"
    )
    env.pop("HERMES_KANBAN_BOARD", None)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repo_root), env.get("PYTHONPATH", "")) if part
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return cast(dict[str, object], json.loads(completed.stdout))


def _policy_state(home: Path) -> kb._ReviewerPolicyResult:
    with kb.connect_closing(db_path=home / "kanban.db") as conn:
        kb._synchronize_reviewer_policy(conn)
        return kb._load_reviewer_policy(conn)


def _reviewer_candidates(
    current: str | None,
) -> tuple[list[str], list[dict[str, str]]]:
    with kb.connect_closing() as conn:
        kb._synchronize_reviewer_policy(conn)
        return kb._reviewer_candidates(current, conn=conn)


def _load_policy(conn: Any) -> Any:
    loader = cast(Any, getattr(kb, "_load_reviewer_policy"))
    return loader(conn)


def test_reviewer_profile_is_registered_in_config_schema():
    from hermes_cli.config import DEFAULT_CONFIG, _validate_config_key

    kanban_defaults = cast(dict[str, object], DEFAULT_CONFIG["kanban"])
    assert kanban_defaults["reviewer_profile"] is None
    assert _validate_config_key("kanban.reviewer_profile") == (True, None)


def test_generic_explicit_init_does_not_initialize_reviewer_policy(
    reviewer_home, tmp_path,
):
    """Generic schema init must not become reviewer-policy authority."""
    db_path = tmp_path / "owners.db"
    _write_reviewer_config(tmp_path, "code-reviewer")

    kb.init_db(db_path)
    with kb.connect(db_path=db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        row = conn.execute(
            "SELECT state FROM kanban_reviewer_policy"
        ).fetchone()
        reviewer_candidates = cast(Any, getattr(kb, "_reviewer_candidates"))
        candidates, failures = reviewer_candidates(None, conn=conn)

    assert "kanban_reviewer_policy" in tables
    assert row is None
    assert candidates == []
    assert failures == [
        {
            "profile": "reviewer failed",
            "error": "reviewer policy is not initialized",
        }
    ]


def test_unbound_explicit_db_keeps_dependency_admission_fail_closed(
    reviewer_home, tmp_path,
):
    db_path = tmp_path / "owners.db"
    kb.init_db(db_path)

    with kb.connect(db_path=db_path) as conn:
        parent_id = kb.create_task(conn, title="unbound parent")
        with pytest.raises(ValueError, match="no bound board"):
            kb.create_task(
                conn,
                title="unbound child",
                parents=[parent_id],
            )


def test_unbound_explicit_db_does_not_inherit_ambient_default_project(
    reviewer_home, tmp_path,
):
    from hermes_cli import projects_db as pdb

    repo = tmp_path / "repo"
    repo.mkdir()
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Ambient Project",
            slug="ambient-project",
            primary_path=str(repo),
        )

    ambient_default = tmp_path / "ambient-default"
    ambient_default.mkdir()
    metadata_path = reviewer_home / "kanban" / "boards" / "default" / "board.json"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        json.dumps(
            {
                "project_id": "ambient-project",
                "default_workdir": str(ambient_default),
            }
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "explicit" / "owners.db"
    kb.init_db(db_path)
    with kb.connect(db_path=db_path) as conn:
        ordinary_id = kb.create_task(
            conn,
            title="ordinary explicit-db task",
            workspace_kind="dir",
        )
        ordinary = kb.get_task(conn, ordinary_id)
        assert ordinary is not None
        assert ordinary.project_id is None
        assert ordinary.workspace_path is None

        with pytest.raises(ValueError, match="no bound board"):
            kb.create_task(
                conn,
                title="explicit project must remain bound",
                project_id=project_id,
            )
        with pytest.raises(ValueError, match="no bound board"):
            kb.create_task(
                conn,
                title="typed dependency must remain bound",
                dependency_kind="sample.gate",
            )
        with pytest.raises(ValueError, match="no bound board"):
            kb.create_task(
                conn,
                title="provider dependency must remain bound",
                provider_name="sample",
            )
        with pytest.raises(ValueError, match="no bound board"):
            kb.create_task(
                conn,
                title="project-source fallback must remain bound",
                project_source_task_id="t_source",
            )


def test_dispatch_stops_on_reviewer_policy_sync_failure_and_recovers(
    reviewer_home, tmp_path,
):
    _write_profile(reviewer_home, "code-reviewer")
    _write_reviewer_config(reviewer_home, "code-reviewer")
    kb.init_db()

    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, dry_run=False, max_spawn=0)
        before_policy = _load_policy(conn)
        assert before_policy.state.value == "pinned"
        assert before_policy.profile == "code-reviewer"

        task_id = kb.create_task(
            conn,
            title="sync failure must not admit stale reviewer",
            assignee="programmer",
        )
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, task_id, claimer=f"{host}:implementation")
        assert kb.submit_task_for_review(
            conn,
            task_id,
            "code-reviewer",
            trusted_operator=True,
        )
        before_task = kb.get_task(conn, task_id)
        assert before_task is not None

        (reviewer_home / "config.yaml").write_text(
            "kanban:\n  reviewer_profile: [\n",
            encoding="utf-8",
        )
        conn.execute(
            "CREATE TRIGGER deny_policy_refresh "
            "BEFORE UPDATE ON kanban_reviewer_policy "
            "BEGIN SELECT RAISE(ABORT, 'policy refresh denied'); END"
        )

        spawned: list[tuple[str, str | None]] = []
        failed = kb.dispatch_once(
            conn,
            dry_run=False,
            spawn_fn=lambda task, workspace: spawned.append(
                (task.id, task.assignee)
            ),
        )
        after_failure_policy = _load_policy(conn)
        after_failure_task = kb.get_task(conn, task_id)

        assert getattr(failed, "policy_sync_failed") is True
        assert getattr(failed, "policy_sync_error_code") == "snapshot_write_failed"
        assert failed.spawned == []
        assert failed.review_reconciled == []
        assert spawned == []
        assert after_failure_policy == before_policy
        assert after_failure_task is not None
        assert after_failure_task.status == before_task.status == "review"
        assert after_failure_task.assignee == before_task.assignee == "code-reviewer"
        assert after_failure_task.current_run_id == before_task.current_run_id
        assert after_failure_task.claim_lock == before_task.claim_lock
        assert conn.in_transaction is False

        dry = kb.dispatch_once(
            conn,
            dry_run=True,
            spawn_fn=lambda task, workspace: spawned.append(
                (task.id, task.assignee)
            ),
        )
        assert getattr(dry, "policy_sync_failed") is True
        assert getattr(dry, "policy_sync_error_code") == "snapshot_stale"
        assert dry.spawned == []
        assert spawned == []
        assert _load_policy(conn) == before_policy
        dry_task = kb.get_task(conn, task_id)
        assert dry_task is not None
        assert dry_task.status == before_task.status
        assert dry_task.assignee == before_task.assignee

        conn.execute("DROP TRIGGER deny_policy_refresh")
        (reviewer_home / "config.yaml").write_text(
            "kanban:\n  reviewer_profile: code-reviewer\n# recovered\n",
            encoding="utf-8",
        )
        recovered_spawned: list[tuple[str, str | None]] = []
        recovered = kb.dispatch_once(
            conn,
            dry_run=False,
            spawn_fn=lambda task, workspace: recovered_spawned.append(
                (task.id, task.assignee)
            ),
        )
        recovered_policy = _load_policy(conn)

        assert getattr(recovered, "policy_sync_failed") is False
        assert recovered_policy.state.value == "pinned"
        assert recovered_policy.profile == "code-reviewer"
        assert recovered_policy.policy_version > before_policy.policy_version
        assert recovered_spawned == [(task_id, "code-reviewer")]
        assert recovered.spawned
        assert recovered.spawned[0][0] == task_id



def test_reviewer_policy_loader_has_explicit_absent_pinned_and_invalid_states(
    reviewer_home,
):
    assert _policy_state(reviewer_home).state.value == "absent"

    _write_reviewer_config(reviewer_home, "code-reviewer")
    pinned = _policy_state(reviewer_home)
    assert pinned.state.value == "pinned"
    assert pinned.profile == "code-reviewer"
    assert pinned.policy_version >= 2

    (reviewer_home / "config.yaml").write_text(
        "kanban:\n  reviewer_profile: [\n", encoding="utf-8",
    )
    invalid = _policy_state(reviewer_home)
    assert invalid.state.value == "invalid"
    assert invalid.error_code == "malformed_yaml"


def test_public_handoff_uses_dispatcher_policy_under_programmer_profile_home(
    reviewer_home,
    monkeypatch,
):
    dispatcher_home = reviewer_home / "dispatcher-home"
    worker_home = reviewer_home / "programmer-home"
    ambient_home = reviewer_home / "worker-controlled-kanban-home"
    dispatcher_home.mkdir()
    ambient_home.mkdir()
    _write_reviewer_config(ambient_home, None)
    _write_profile(worker_home, "programmer")
    _write_profile(worker_home, "code-reviewer-unauthorized")
    _write_profile(worker_home, "code-reviewer-authorized")
    _write_reviewer_config(dispatcher_home, "code-reviewer-authorized")
    monkeypatch.setenv("HERMES_HOME", str(worker_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(dispatcher_home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(ambient_home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(dispatcher_home / "kanban.db")
    with kb.connect_closing() as conn:
        kb._synchronize_reviewer_policy(conn)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="cross-profile reviewer handoff",
            assignee="programmer",
        )
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, task_id, claimer=f"{host}:implementation")
        implementation = kb.get_task(conn, task_id)
        assert implementation is not None

        assert kb.block_task(
            conn,
            task_id,
            kind="dependency",
            reason="review-required: implementation is ready",
            expected_run_id=implementation.current_run_id,
            expected_assignee=implementation.assignee,
            expected_claim=implementation.claim_lock,
        )

        review = kb.get_task(conn, task_id)
        assert review is not None
        assert review.status == "review"
        assert review.assignee == "code-reviewer-authorized"


def test_fresh_process_malformed_policy_never_discovers_reviewers(reviewer_home):
    dispatcher_home = reviewer_home / "dispatcher-home"
    worker_home = reviewer_home / "programmer-home"
    dispatcher_home.mkdir()
    _write_profile(worker_home, "programmer")
    _write_profile(worker_home, "code-reviewer-unauthorized")
    _write_profile(worker_home, "code-reviewer-authorized")
    secret_marker = "reviewer-policy-parser-secret"
    (dispatcher_home / "config.yaml").write_text(
        f"kanban:\n  reviewer_profile: [\n  marker: {secret_marker}\n",
        encoding="utf-8",
    )

    result = _run_policy_probe(
        worker_home,
        dispatcher_home,
    )

    assert result["candidates"] == []
    failures = cast(list[dict[str, str]], result["failures"])
    assert len(failures) == 1
    assert failures[0]["error"] == "reviewer policy config is malformed YAML"
    assert all(len(value) <= 500 for value in failures[0].values())
    assert secret_marker not in json.dumps(result)


def test_fresh_process_unreadable_policy_never_discovers_reviewers(reviewer_home):
    dispatcher_home = reviewer_home / "dispatcher-home"
    worker_home = reviewer_home / "programmer-home"
    dispatcher_home.mkdir()
    _write_profile(worker_home, "programmer")
    _write_profile(worker_home, "code-reviewer-unauthorized")
    _write_profile(worker_home, "code-reviewer-authorized")
    (dispatcher_home / "config.yaml").mkdir()

    result = _run_policy_probe(
        worker_home,
        dispatcher_home,
    )

    assert result["candidates"] == []
    failures = cast(list[dict[str, str]], result["failures"])
    assert len(failures) == 1
    assert failures[0]["error"] == "reviewer policy config is unreadable"
    assert all(len(value) <= 500 for value in failures[0].values())


def test_configured_reviewer_pin_excludes_discovered_profiles(
    reviewer_home,
    monkeypatch,
):
    _write_profile(reviewer_home, "code-reviewer")
    _write_profile(reviewer_home, "code-reviewer-claudecode")
    _write_reviewer_config(reviewer_home, "code-reviewer")

    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles,
        "list_profiles",
        lambda: pytest.fail("a configured reviewer pin must not discover reviewers"),
    )

    candidates, failures = _reviewer_candidates("code-reviewer-claudecode")

    assert candidates == ["code-reviewer"]
    assert failures == []


def test_disallowed_current_reviewer_is_not_reinserted_for_pinned_lane(reviewer_home):
    _write_profile(reviewer_home, "code-reviewer")
    _write_profile(reviewer_home, "code-reviewer-claudecode")
    _write_reviewer_config(reviewer_home, "code-reviewer")

    candidates, failures = _reviewer_candidates("code-reviewer-claudecode")

    assert failures == []
    assert "code-reviewer-claudecode" not in candidates
    with kb.connect_closing() as conn:
        selected = kb._select_review_handoff_reviewer(
            conn, "code-reviewer-claudecode",
        )
    assert selected == "code-reviewer"


@pytest.mark.parametrize("configured_null", [False, True])
def test_absent_or_null_reviewer_pin_preserves_dynamic_discovery(
    reviewer_home,
    configured_null,
):
    _write_profile(reviewer_home, "code-reviewer")
    _write_profile(reviewer_home, "code-reviewer-claudecode")
    if configured_null:
        _write_reviewer_config(reviewer_home, None)

    candidates, failures = _reviewer_candidates("code-reviewer-claudecode")

    assert failures == []
    assert candidates[0] == "code-reviewer-claudecode"
    assert set(candidates) == {"code-reviewer", "code-reviewer-claudecode"}


@pytest.mark.parametrize(
    ("value", "expected_error"),
    [
        (123, "must be a nonempty string or null"),
        (["code-reviewer"], "must be a nonempty string or null"),
        ("programmer", "must start with code-reviewer"),
        ("code-reviewer-missing", "profile unavailable"),
    ],
)
def test_malformed_or_unavailable_pin_fails_closed_with_bounded_evidence(
    reviewer_home,
    monkeypatch,
    value,
    expected_error,
):
    _write_profile(reviewer_home, "code-reviewer-claudecode")
    _write_reviewer_config(reviewer_home, value)

    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles,
        "list_profiles",
        lambda: pytest.fail("invalid pins must not fall back to discovery"),
    )

    candidates, failures = _reviewer_candidates("code-reviewer-claudecode")

    assert candidates == []
    assert len(failures) == 1
    assert expected_error in failures[0]["error"]
    assert all(len(value) <= 500 for failure in failures for value in failure.values())


def test_pinned_reviewer_requires_the_review_skill(reviewer_home):
    _write_profile(reviewer_home, "code-reviewer", with_review_skill=False)
    _write_reviewer_config(reviewer_home, "code-reviewer")

    candidates, failures = _reviewer_candidates("code-reviewer-claudecode")

    assert candidates == []
    assert failures == [
        {
            "profile": "code-reviewer",
            "error": "missing skill: sdlc-review",
        }
    ]


def test_failover_moves_preexisting_disallowed_authority_to_pinned_reviewer(
    reviewer_home,
):
    _write_profile(reviewer_home, "code-reviewer")
    _write_profile(reviewer_home, "code-reviewer-claudecode")
    _write_reviewer_config(reviewer_home, "code-reviewer")
    kb.init_db()
    with kb.connect_closing() as conn:
        kb._synchronize_reviewer_policy(conn)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="pinned reviewer failover",
            assignee="programmer",
        )
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, task_id, claimer=f"{host}:implementation")
        assert kb.submit_task_for_review(
            conn,
            task_id,
            "code-reviewer-claudecode",
            trusted_operator=True,
        )

        result = kb._failover_review_after_recovery(
            conn,
            task_id,
            error="preexisting reviewer is not allowed",
        )

        assert result is not None
        assert result.status == "review"
        assert result.assignee == "code-reviewer"
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='review_failover' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None
        payload = json.loads(event["payload"])
        assert payload["failed_reviewer"] == "code-reviewer-claudecode"
        assert payload["next_reviewer"] == "code-reviewer"


def test_custom_board_db_binds_policy_when_ambient_home_is_stale(
    reviewer_home,
    monkeypatch,
):
    worker_home = reviewer_home / "programmer-home"
    ambient_home = reviewer_home / "stale-kanban-home"
    custom_db = reviewer_home / "kanban" / "boards" / "project" / "kanban.db"
    ambient_home.mkdir()
    _write_reviewer_config(reviewer_home, "code-reviewer-authorized")
    _write_reviewer_config(ambient_home, None)
    _write_profile(worker_home, "programmer")
    _write_profile(worker_home, "code-reviewer-unauthorized")
    _write_profile(worker_home, "code-reviewer-authorized")
    monkeypatch.setenv("HERMES_HOME", str(worker_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(custom_db))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(ambient_home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(custom_db)
    with kb.connect_closing() as conn:
        kb._synchronize_reviewer_policy(conn)

    with kb.connect_closing() as conn:
        policy = kb._load_reviewer_policy(conn)
        assert policy.state is kb._ReviewerPolicyState.PINNED
        assert policy.profile == "code-reviewer-authorized"
        assert policy.source_path == str(reviewer_home / "config.yaml")
        candidates, failures = kb._reviewer_candidates(None, conn=conn)
        assert failures == []
        assert candidates == ["code-reviewer-authorized"]

        task_id = kb.create_task(
            conn,
            title="custom board reviewer handoff",
            assignee="programmer",
        )
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, task_id, claimer=f"{host}:implementation")
        implementation = kb.get_task(conn, task_id)
        assert implementation is not None
        assert kb.block_task(
            conn,
            task_id,
            kind="dependency",
            reason="review-required: custom board handoff",
            expected_run_id=implementation.current_run_id,
            expected_assignee=implementation.assignee,
            expected_claim=implementation.claim_lock,
        )
        review = kb.get_task(conn, task_id)
        assert review is not None
        assert review.status == "review"
        assert review.assignee == "code-reviewer-authorized"


def test_legacy_init_migrates_and_explicitly_initializes_compatibility_state(
    reviewer_home,
):
    legacy_db = reviewer_home / "legacy" / "kanban.db"
    legacy_db.parent.mkdir()
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(legacy_db)
    with kb.connect_closing(db_path=legacy_db) as conn:
        kb._synchronize_reviewer_policy(conn)

    with kb.connect_closing(db_path=legacy_db) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='kanban_reviewer_policy'"
        ).fetchone()
        assert table is not None
        policy = kb._load_reviewer_policy(conn)
        assert policy.state is kb._ReviewerPolicyState.ABSENT
        assert policy.error_code == "source_absent"
        row = conn.execute(
            "SELECT state, profile, policy_version FROM kanban_reviewer_policy "
            "WHERE id=1"
        ).fetchone()
        assert row["state"] == "absent"
        assert row["profile"] is None
        assert row["policy_version"] == 1


def test_authoritative_source_change_to_malformed_stays_invalid_after_restart(
    reviewer_home,
):
    _write_profile(reviewer_home, "code-reviewer")
    _write_reviewer_config(reviewer_home, "code-reviewer")
    kb.init_db()
    first = _policy_state(reviewer_home)
    assert first.state is kb._ReviewerPolicyState.PINNED

    (reviewer_home / "config.yaml").write_text(
        "kanban:\n  reviewer_profile: [\n", encoding="utf-8",
    )
    kb._INITIALIZED_PATHS.clear()
    after_change = _policy_state(reviewer_home)
    assert after_change.state is kb._ReviewerPolicyState.INVALID
    assert after_change.error_code == "malformed_yaml"
    with kb.connect_closing() as conn:
        candidates, failures = kb._reviewer_candidates(None, conn=conn)
        assert candidates == []
        assert failures[0]["error"] == "reviewer policy config is malformed YAML"


def test_direct_cli_block_then_dispatch_reconciles_from_bound_snapshot(
    reviewer_home,
    monkeypatch,
):
    dispatcher_home = reviewer_home / "dispatcher-home"
    worker_home = reviewer_home / "programmer-home"
    ambient_home = reviewer_home / "stale-worker-home"
    dispatcher_home.mkdir()
    ambient_home.mkdir()
    _write_reviewer_config(dispatcher_home, "code-reviewer-authorized")
    _write_reviewer_config(ambient_home, None)
    _write_profile(worker_home, "programmer")
    _write_profile(worker_home, "code-reviewer-unauthorized")
    _write_profile(worker_home, "code-reviewer-authorized")
    db_path = dispatcher_home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(worker_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(ambient_home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(db_path)
    with kb.connect_closing() as conn:
        kb._synchronize_reviewer_policy(conn)
        task_id = kb.create_task(
            conn,
            title="direct CLI review handoff",
            assignee="programmer",
            initial_status="running",
        )

    env = os.environ.copy()
    block = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "kanban",
            "block",
            "--kind",
            "dependency",
            task_id,
            "review-required: direct CLI handoff",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert block.returncode == 0, block.stderr
    dispatch = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "kanban",
            "dispatch",
            "--max",
            "0",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert dispatch.returncode == 0, dispatch.stderr
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "review"
        assert task.assignee == "code-reviewer-authorized"

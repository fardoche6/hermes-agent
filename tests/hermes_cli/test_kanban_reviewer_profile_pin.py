"""Behavioral coverage for the configured Kanban reviewer lane."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import cast

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
    script = "\n".join(
        (
            "import json",
            "from hermes_cli import kanban_db as kb",
            "candidates, failures = kb._reviewer_candidates(None)",
            "print(json.dumps({'candidates': candidates, 'failures': failures}))",
        )
    )
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["HERMES_KANBAN_HOME"] = str(policy_home)
    env.pop("HERMES_KANBAN_DB", None)
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


def test_reviewer_profile_is_registered_in_config_schema():
    from hermes_cli.config import DEFAULT_CONFIG, _validate_config_key

    kanban_defaults = cast(dict[str, object], DEFAULT_CONFIG["kanban"])
    assert kanban_defaults["reviewer_profile"] is None
    assert _validate_config_key("kanban.reviewer_profile") == (True, None)


def test_reviewer_policy_loader_has_explicit_absent_pinned_and_invalid_states(
    reviewer_home,
):
    load_policy = getattr(kb, "_load_reviewer_policy")
    assert load_policy().state.value == "absent"

    _write_reviewer_config(reviewer_home, "code-reviewer")
    assert load_policy().state.value == "pinned"

    (reviewer_home / "config.yaml").write_text(
        "kanban:\n  reviewer_profile: [\n", encoding="utf-8",
    )
    assert load_policy().state.value == "invalid"


def test_public_handoff_uses_dispatcher_policy_under_programmer_profile_home(
    reviewer_home,
    monkeypatch,
):
    dispatcher_home = reviewer_home / "dispatcher-home"
    worker_home = reviewer_home / "programmer-home"
    dispatcher_home.mkdir()
    _write_profile(worker_home, "programmer")
    _write_profile(worker_home, "code-reviewer-unauthorized")
    _write_profile(worker_home, "code-reviewer-authorized")
    _write_reviewer_config(dispatcher_home, "code-reviewer-authorized")
    monkeypatch.setenv("HERMES_HOME", str(worker_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(dispatcher_home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

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

    candidates, failures = kb._reviewer_candidates("code-reviewer-claudecode")

    assert candidates == ["code-reviewer"]
    assert failures == []


def test_disallowed_current_reviewer_is_not_reinserted_for_pinned_lane(reviewer_home):
    _write_profile(reviewer_home, "code-reviewer")
    _write_profile(reviewer_home, "code-reviewer-claudecode")
    _write_reviewer_config(reviewer_home, "code-reviewer")

    candidates, failures = kb._reviewer_candidates("code-reviewer-claudecode")

    assert failures == []
    assert "code-reviewer-claudecode" not in candidates
    assert (
        kb._select_review_handoff_reviewer("code-reviewer-claudecode")
        == "code-reviewer"
    )


@pytest.mark.parametrize("configured_null", [False, True])
def test_absent_or_null_reviewer_pin_preserves_dynamic_discovery(
    reviewer_home,
    configured_null,
):
    _write_profile(reviewer_home, "code-reviewer")
    _write_profile(reviewer_home, "code-reviewer-claudecode")
    if configured_null:
        _write_reviewer_config(reviewer_home, None)

    candidates, failures = kb._reviewer_candidates("code-reviewer-claudecode")

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

    candidates, failures = kb._reviewer_candidates("code-reviewer-claudecode")

    assert candidates == []
    assert len(failures) == 1
    assert expected_error in failures[0]["error"]
    assert all(len(value) <= 500 for failure in failures for value in failure.values())


def test_pinned_reviewer_requires_the_review_skill(reviewer_home):
    _write_profile(reviewer_home, "code-reviewer", with_review_skill=False)
    _write_reviewer_config(reviewer_home, "code-reviewer")

    candidates, failures = kb._reviewer_candidates("code-reviewer-claudecode")

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

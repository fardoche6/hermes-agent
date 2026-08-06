"""Behavioral coverage for the configured Kanban reviewer lane."""

from __future__ import annotations

import json
from pathlib import Path
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


def test_reviewer_profile_is_registered_in_config_schema():
    from hermes_cli.config import DEFAULT_CONFIG, _validate_config_key

    kanban_defaults = cast(dict[str, object], DEFAULT_CONFIG["kanban"])
    assert kanban_defaults["reviewer_profile"] is None
    assert _validate_config_key("kanban.reviewer_profile") == (True, None)


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

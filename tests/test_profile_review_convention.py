from pathlib import Path

from tools.profile_review_convention import (
    PHANTOM_MARKERS,
    PROGRAMMER_PROFILES,
    validate_profile_review_convention,
)


def _fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "profiles"
    soul = "kanban_comment kanban_block(reason=\"review-required: ...\") REVIEW VERDICT: APPROVE|REQUEST_CHANGES kanban_unblock"
    for name in PROGRAMMER_PROFILES:
        (root / name).mkdir(parents=True)
        (root / name / "SOUL.md").write_text(soul, encoding="utf-8")
        (root / name / "AGENTS.md").write_text(soul, encoding="utf-8")
    lifecycle = root / "orchestrator" / "skills" / "hermes-kanban" / "kanban-lifecycle"
    lifecycle.mkdir(parents=True)
    (lifecycle / "SKILL.md").write_text("durable review handoff proof", encoding="utf-8")
    launcher = root / "code-reviewer-claudecode" / "skills" / "claude-code-review-kanban-launcher"
    launcher.mkdir(parents=True)
    (launcher / "SKILL.md").write_text("## Completion\nscoped `kanban_unblock`", encoding="utf-8")
    return root


def test_profile_validator_is_root_parameterized_and_positive(tmp_path):
    root = _fixture_root(tmp_path)
    result = validate_profile_review_convention(root)
    assert result["phantom_marker_hits"] == []
    assert result["required_worker_reviewer_phrases"] == []
    assert result["programmer_soul_parity"]
    assert result["programmer_agents_parity"]
    assert result["lifecycle_valid"]
    assert result["launcher_valid"]


def test_profile_validator_reports_marker_and_copy_drift(tmp_path):
    root = _fixture_root(tmp_path)
    (root / "programmer" / "SOUL.md").write_text(PHANTOM_MARKERS[0], encoding="utf-8")
    (root / "programmer-luna" / "AGENTS.md").write_text("drift", encoding="utf-8")
    result = validate_profile_review_convention(root)
    assert result["phantom_marker_hits"]
    assert not result["programmer_soul_parity"]
    assert not result["programmer_agents_parity"]

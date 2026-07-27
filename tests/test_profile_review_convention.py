from pathlib import Path


PROGRAMMER_PROFILES = (
    "programmer",
    "programmer-gemini",
    "programmer-claudecode",
    "programmer-luna",
)
PHANTOM_MARKERS = ("kanban_submit_review", "kanban_review_verdict")


def _fixture_profiles(tmp_path):
    root = tmp_path / "profiles"
    for name in PROGRAMMER_PROFILES:
        profile = root / name
        profile.mkdir(parents=True)
        (profile / "SOUL.md").write_text(
            "kanban_comment\nkanban_block(reason=\"review-required: ...\")\n"
            "REVIEW VERDICT: APPROVE|REQUEST_CHANGES\nkanban_unblock\n"
        )
        (profile / "AGENTS.md").write_text((profile / "SOUL.md").read_text())
    lifecycle = root / "orchestrator" / "skills" / "hermes-kanban" / "kanban-lifecycle"
    lifecycle.mkdir(parents=True)
    (lifecycle / "SKILL.md").write_text("durable review handoff proof\n")
    launcher = (
        root / "code-reviewer-claudecode" / "skills"
        / "claude-code-review-kanban-launcher"
    )
    launcher.mkdir(parents=True)
    (launcher / "SKILL.md").write_text(
        "## Completion\nscoped `kanban_unblock`\n"
    )
    return root


def _markdown(root):
    return (
        path for path in root.rglob("*.md")
        if "logs" not in path.parts and "sessions" not in path.parts
    )


def test_profile_review_contract_is_hermetic(tmp_path):
    root = _fixture_profiles(tmp_path)
    hits = [
        (path, marker)
        for path in _markdown(root)
        for marker in PHANTOM_MARKERS
        if marker in path.read_text(encoding="utf-8")
    ]
    assert hits == []
    souls = [(root / name / "SOUL.md").read_bytes() for name in PROGRAMMER_PROFILES]
    agents = [(root / name / "AGENTS.md").read_bytes() for name in PROGRAMMER_PROFILES]
    assert len(set(souls)) == 1
    assert len(set(agents)) == 1
    text = (root / "programmer" / "SOUL.md").read_text()
    assert "kanban_comment" in text
    assert "review-required:" in text
    assert "REVIEW VERDICT: APPROVE|REQUEST_CHANGES" in text
    assert "kanban_unblock" in text
    lifecycle = (
        root / "orchestrator" / "skills" / "hermes-kanban"
        / "kanban-lifecycle" / "SKILL.md"
    ).read_text()
    assert "durable review handoff proof" in lifecycle
    launcher = (
        root / "code-reviewer-claudecode" / "skills"
        / "claude-code-review-kanban-launcher" / "SKILL.md"
    ).read_text()
    assert "## Completion" in launcher
    assert "scoped `kanban_unblock`" in launcher

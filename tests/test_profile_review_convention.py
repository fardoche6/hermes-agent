from pathlib import Path


PROFILE_ROOT = Path("/home/fardochebot/.hermes/profiles")
PHANTOM_MARKERS = ("kanban_submit_review", "kanban_review_verdict")
PROGRAMMER_PROFILES = (
    "programmer",
    "programmer-gemini",
    "programmer-claudecode",
    "programmer-luna",
)


def _live_markdown():
    return (
        path
        for path in PROFILE_ROOT.rglob("*.md")
        if "logs" not in path.parts and "sessions" not in path.parts
    )


def test_live_profiles_retire_phantom_review_tools():
    hits = [
        (path, marker)
        for path in _live_markdown()
        for marker in PHANTOM_MARKERS
        if marker in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert hits == []


def test_programmer_policy_copies_and_review_handshake_are_consistent():
    souls = [PROFILE_ROOT / name / "SOUL.md" for name in PROGRAMMER_PROFILES]
    agents = [PROFILE_ROOT / name / "AGENTS.md" for name in PROGRAMMER_PROFILES]
    assert len({path.read_bytes() for path in souls}) == 1
    assert len({path.read_bytes() for path in agents}) == 1

    required = (
        "kanban_comment",
        'kanban_block(reason="review-required: ...")',
        "REVIEW VERDICT: APPROVE|REQUEST_CHANGES",
        "kanban_unblock",
    )
    text = (PROFILE_ROOT / "programmer" / "SOUL.md").read_text(encoding="utf-8")
    text += (PROFILE_ROOT / "programmer" / "AGENTS.md").read_text(encoding="utf-8")
    for phrase in required:
        assert phrase in text

    lifecycle = (
        PROFILE_ROOT / "orchestrator" / "skills" / "hermes-kanban"
        / "kanban-lifecycle" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "durable review handoff proof" in lifecycle
    assert "blocked is never a review alias" not in lifecycle

    launcher = (
        PROFILE_ROOT / "code-reviewer-claudecode" / "skills"
        / "claude-code-review-kanban-launcher" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "## Completion" in launcher
    assert "scoped `kanban_unblock`" in launcher

"""Validation of the durable Kanban review convention in profile trees."""
from __future__ import annotations

from pathlib import Path

PHANTOM_MARKERS = ("kanban_submit_review", "kanban_review_verdict")
PROGRAMMER_PROFILES = (
    "programmer",
    "programmer-gemini",
    "programmer-claudecode",
    "programmer-luna",
)


def validate_profile_review_convention(profile_root: Path) -> dict:
    """Return invariant failures for a profile root; never assumes HOME."""
    root = Path(profile_root)
    markdown = [
        path for path in root.rglob("*.md")
        if "logs" not in path.parts and "sessions" not in path.parts
    ]
    hits = [
        {"path": str(path), "marker": marker}
        for path in markdown
        for marker in PHANTOM_MARKERS
        if marker in path.read_text(encoding="utf-8", errors="replace")
    ]
    souls = [root / name / "SOUL.md" for name in PROGRAMMER_PROFILES]
    agents = [root / name / "AGENTS.md" for name in PROGRAMMER_PROFILES]
    required = (
        "kanban_comment",
        'kanban_block(reason="review-required: ...")',
        "REVIEW VERDICT: APPROVE|REQUEST_CHANGES",
        "kanban_unblock",
    )
    programmer_text = ""
    if souls and all(path.is_file() for path in souls):
        programmer_text += souls[0].read_text(encoding="utf-8")
    if agents and all(path.is_file() for path in agents):
        programmer_text += agents[0].read_text(encoding="utf-8")
    lifecycle = root / "orchestrator" / "skills" / "hermes-kanban" / "kanban-lifecycle" / "SKILL.md"
    launcher = root / "code-reviewer-claudecode" / "skills" / "claude-code-review-kanban-launcher" / "SKILL.md"
    return {
        "phantom_marker_hits": hits,
        "programmer_soul_parity": len({path.read_bytes() for path in souls if path.is_file()}) == 1,
        "programmer_agents_parity": len({path.read_bytes() for path in agents if path.is_file()}) == 1,
        "required_worker_reviewer_phrases": [phrase for phrase in required if phrase not in programmer_text],
        "lifecycle_valid": lifecycle.is_file() and "durable review handoff proof" in lifecycle.read_text(encoding="utf-8") and "blocked is never a review alias" not in lifecycle.read_text(encoding="utf-8"),
        "launcher_valid": launcher.is_file() and "## Completion" in launcher.read_text(encoding="utf-8") and "scoped `kanban_unblock`" in launcher.read_text(encoding="utf-8"),
    }


def profile_review_convention_valid(profile_root: Path) -> bool:
    result = validate_profile_review_convention(profile_root)
    return (
        not result["phantom_marker_hits"]
        and result["programmer_soul_parity"]
        and result["programmer_agents_parity"]
        and not result["required_worker_reviewer_phrases"]
        and result["lifecycle_valid"]
        and result["launcher_valid"]
    )

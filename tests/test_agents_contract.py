"""Regression checks for the compact repository agent contract."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_active_agents_contract_is_bounded_and_has_fallbacks():
    active = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert (ROOT / "_study/agents-refactor/AGENTS.md").is_file()
    assert (ROOT / "_study/agents-refactor/worktree-handoff.md").is_file()
    assert (ROOT / "_study/agents-refactor/development-lifecycle.md").is_file()
    assert len(active.encode("utf-8")) < 20_000
    for phrase in (
        "bound PyPI dependencies",
        "SHA-pin Git URLs and GitHub\n  Actions",
        "exact-pin CI-only pip installs",
        "outbound telemetry",
        "`.env` is\n  for secrets only",
        "scripts/run_tests.sh",
        "apply-coding-task-handoff",
        "software-development-lifecycle",
        "Reference map (load only what the change needs)",
    ):
        assert phrase in active


def test_profile_path_links_use_relocated_reference():
    for relative in (
        "website/docs/developer-guide/contributing.md",
        "website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/developer-guide/contributing.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "blob/main/AGENTS.md#profiles-multi-instance-support" not in text
        assert "_study/agents-refactor/AGENTS.md#profiles-multi-instance-support" in text


def test_preserved_reference_matches_pre_shrink_guide():
    import hashlib

    actual = (ROOT / "_study/agents-refactor/AGENTS.md").read_bytes()
    assert hashlib.sha256(actual).hexdigest() == (
        "0b5828f02e6c8daea2721a00e08a041ff0ca7ef3a90d0ecbc21148657509dbdd"
    )

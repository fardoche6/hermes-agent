"""Regression coverage for delegated-child terminal snapshot isolation."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_delegated_child_marker_does_not_persist_in_parent_snapshot(
    monkeypatch,
    tmp_path: Path,
):
    """A child receives the lineage marker without contaminating later parent calls."""
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setenv("HERMES_PARENT_IDENTITY", "parent")

    from agent.delegation_context import (
        delegated_child_context,
        is_delegated_child_context,
    )
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        command = (
            "printf 'marker=%s\\nidentity=%s\\n' "
            '"${HERMES_DELEGATED_CHILD_CONTEXT-}" "${HERMES_PARENT_IDENTITY-}"'
        )
        before = env.execute(command)

        with delegated_child_context("child-session"):
            child = env.execute(command)
            assert is_delegated_child_context()

        assert not is_delegated_child_context()
        assert os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") is None
        after = env.execute(command)

        def identity(result: dict) -> list[str]:
            return [
                line
                for line in result["output"].splitlines()
                if line.startswith(("marker=", "identity="))
            ]

        assert identity(before) == ["marker=", "identity=parent"]
        assert identity(child) == ["marker=1", "identity=parent"]
        assert identity(after) == identity(before)

        snapshot = Path(env._snapshot_path)
        assert "HERMES_DELEGATED_CHILD_CONTEXT" not in snapshot.read_text()
    finally:
        env.cleanup()

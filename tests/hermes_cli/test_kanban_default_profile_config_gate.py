"""The ``default`` profile must fail closed on a broken root ``config.yaml``.

``_profile_is_configured("default")`` used to return True purely because the
canonical default root directory existed.  A root ``config.yaml`` that is
malformed / empty / a scalar / a list / unreadable makes the runtime config
loader silently fall back to ``DEFAULT_CONFIG``, dropping operator overrides —
yet the dispatcher would still assign, claim and spawn ``default`` cards.

These tests drive the REAL resolver through the REAL ``dispatch_once`` path
(no ``all_assignees_spawnable`` fixture, no source-text assertions) and assert
a complete tasks / task_runs / task_events snapshot is untouched.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


def _reimport():
    for mod in list(sys.modules):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    return kanban_db


@pytest.fixture()
def default_root_home(monkeypatch):
    """HERMES_HOME == the canonical default root (Docker/custom layout)."""
    root = Path(tempfile.mkdtemp(prefix="kanban_default_cfg_root_"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    yield _reimport(), root


@pytest.fixture()
def named_profile_home(monkeypatch):
    """HERMES_HOME == an ACTIVE named profile under the default root.

    ``get_default_hermes_root`` must still resolve the canonical root, so the
    gate reads ``<root>/config.yaml`` — not the named profile's own config.
    """
    root = Path(tempfile.mkdtemp(prefix="kanban_default_cfg_named_"))
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("agent: {}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    yield _reimport(), root


# --- invalid root-config matrix -------------------------------------------

def _write_malformed(root: Path) -> None:
    (root / "config.yaml").write_text("kanban: [unclosed\n  - :\n", encoding="utf-8")


def _write_empty(root: Path) -> None:
    (root / "config.yaml").write_text("", encoding="utf-8")


def _write_scalar(root: Path) -> None:
    (root / "config.yaml").write_text("just-a-string\n", encoding="utf-8")


def _write_list(root: Path) -> None:
    (root / "config.yaml").write_text("- a\n- b\n", encoding="utf-8")


def _write_unreadable(root: Path) -> None:
    path = root / "config.yaml"
    path.write_text("kanban: {}\n", encoding="utf-8")
    os.chmod(path, 0o000)


def _write_directory(root: Path) -> None:
    (root / "config.yaml").mkdir()


INVALID = [
    pytest.param(_write_malformed, id="malformed"),
    pytest.param(_write_empty, id="empty"),
    pytest.param(_write_scalar, id="scalar"),
    pytest.param(_write_list, id="list"),
    pytest.param(
        _write_unreadable,
        id="unreadable",
        marks=pytest.mark.skipif(
            os.name == "nt" or os.geteuid() == 0,
            reason="chmod 000 is not enforced for this user/platform",
        ),
    ),
    pytest.param(_write_directory, id="not-a-regular-file"),
]


# --- helpers ---------------------------------------------------------------

def _snapshot(kb):
    with kb.connect_closing() as conn:
        return (
            [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")],
            [tuple(r) for r in conn.execute("SELECT * FROM task_runs ORDER BY id")],
            [tuple(r) for r in conn.execute("SELECT * FROM task_events ORDER BY id")],
        )


class _Spawn:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return 12345


# --- tests -----------------------------------------------------------------

@pytest.mark.parametrize("write_cfg", INVALID)
def test_explicit_default_task_not_spawned_with_broken_root_config(
    default_root_home, write_cfg,
):
    kb, root = default_root_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee="default")
    write_cfg(root)
    before = _snapshot(kb)

    spawn = _Spawn()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn, dry_run=False)

    assert spawn.calls == []
    assert res.spawned == []
    assert task_id in [t[0] if isinstance(t, tuple) else t
                       for t in res.skipped_nonspawnable]
    assert _snapshot(kb) == before


@pytest.mark.parametrize("write_cfg", INVALID)
def test_default_assignee_not_applied_with_broken_root_config(
    default_root_home, write_cfg,
):
    kb, root = default_root_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee=None)
    write_cfg(root)
    before = _snapshot(kb)

    spawn = _Spawn()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=spawn, dry_run=False, default_assignee="default",
        )

    assert spawn.calls == []
    assert res.spawned == []
    assert res.auto_assigned_default == []
    assert task_id in [t[0] if isinstance(t, tuple) else t
                       for t in res.skipped_unassigned]
    assert _snapshot(kb) == before


def test_legacy_default_root_without_config_remains_spawnable(default_root_home):
    kb, root = default_root_home
    assert not (root / "config.yaml").exists()
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee="default")

    spawn = _Spawn()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn, dry_run=False)

    assert len(spawn.calls) == 1
    assert [s[0] for s in res.spawned] == [task_id]


def test_default_root_with_valid_mapping_config_remains_spawnable(default_root_home):
    kb, root = default_root_home
    (root / "config.yaml").write_text("kanban:\n  enabled: true\n", encoding="utf-8")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee="default")

    spawn = _Spawn()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn, dry_run=False)

    assert len(spawn.calls) == 1
    assert [s[0] for s in res.spawned] == [task_id]


def test_named_profile_home_resolves_canonical_default_root(named_profile_home):
    """Positive + negative cases while an ACTIVE named profile is selected."""
    kb, root = named_profile_home

    # No root config yet -> legacy default stays configured.
    assert kb._profile_is_configured("default") is True

    # Valid root mapping -> configured.
    (root / "config.yaml").write_text("kanban:\n  enabled: true\n", encoding="utf-8")
    assert kb._profile_is_configured("default") is True

    # Broken root config -> fail closed, even though the ACTIVE named
    # profile's own config.yaml is perfectly valid.
    _write_malformed(root)
    assert kb._profile_is_configured("default") is False
    assert kb._profile_is_configured("coder") is True


def test_named_profile_dispatch_blocked_by_broken_default_root(named_profile_home):
    kb, root = named_profile_home
    _write_malformed(root)
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee="default")
    before = _snapshot(kb)

    spawn = _Spawn()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn, dry_run=False)

    assert spawn.calls == []
    assert res.spawned == []
    assert task_id in [t[0] if isinstance(t, tuple) else t
                       for t in res.skipped_nonspawnable]
    assert _snapshot(kb) == before

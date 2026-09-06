"""Checkpoint-1 production-boundary proofs for protected lexical writes.

The tests deliberately drive ``write_file_tool`` through the registered terminal
environment.  They do not invoke a standalone path helper or replace the file
operation boundary with a mock.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from tools import file_state
from tools import file_tools as ft
from tools import terminal_tool as tt
from tools.environments.local import LocalEnvironment


class _BoundaryEnvironment:
    """Small terminal-shaped environment used at the real file-tool boundary."""

    def __init__(self, cwd: str, *, local_value=True, missing=None, raising=None):
        self.cwd = cwd
        self._local_value = local_value
        self._missing = missing
        self._raising = raising
        self.identity_reads = 0
        self.execute_calls = []

    @property
    def is_local(self):
        self.identity_reads += 1
        if self._raising == "is_local":
            raise RuntimeError("identity probe failed")
        if self._missing == "is_local":
            raise AttributeError("is_local")
        return self._local_value

    def __getattribute__(self, name):
        if name in {"cwd", "execute"}:
            missing = object.__getattribute__(self, "_missing")
            raising = object.__getattribute__(self, "_raising")
            if missing == name:
                raise AttributeError(name)
            if raising == name:
                raise RuntimeError(f"{name} probe failed")
        return object.__getattribute__(self, name)

    def execute(self, command, cwd=None, **kwargs):
        self.execute_calls.append((command, cwd, kwargs))
        return {"output": "", "returncode": 0}


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    """Install one fake environment in the same registry used by production."""
    env = _BoundaryEnvironment(str(tmp_path))
    monkeypatch.setattr(tt, "_active_environments", {"default": env})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(ft, "_file_ops_cache", {})
    monkeypatch.setattr(ft, "_read_tracker", {})
    monkeypatch.setattr(ft, "_patch_failure_tracker", {})
    file_state.get_registry().clear()
    yield env, tmp_path
    file_state.get_registry().clear()


def _host_probe_spy(monkeypatch):
    events: list[str] = []

    original_resolve = Path.resolve

    def resolve(self, *args, **kwargs):
        events.append("host:path.resolve")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    for name in ("getcwd", "stat", "lstat"):
        original = getattr(os, name)

        def probe(*args, _name=name, _original=original, **kwargs):
            events.append(f"host:os.{_name}")
            return _original(*args, **kwargs)

        monkeypatch.setattr(os, name, probe)

    for name in ("realpath", "exists", "isfile", "isdir", "getmtime"):
        original = getattr(os.path, name)

        def probe(*args, _name=name, _original=original, **kwargs):
            events.append(f"host:os.path.{_name}")
            return _original(*args, **kwargs)

        monkeypatch.setattr(os.path, name, probe)
    return events


def _json_write(path: str, content: str, *, task_id="default"):
    return json.loads(ft.write_file_tool(path, content, task_id=task_id))


def test_missing_backend_identity_is_rejected_before_host_preflight(
    monkeypatch, boundary
):
    env, root = boundary
    env._missing = "is_local"
    target = root / "AGENTS.md"
    target.write_text("unchanged\n", encoding="utf-8")
    events = _host_probe_spy(monkeypatch)

    result = _json_write(str(target), "changed\n")

    assert result["error"] == "UNSUPPORTED: backend identity is unavailable or ambiguous"
    observed_events = list(events)
    assert observed_events == []
    assert env.execute_calls == []
    assert target.read_text(encoding="utf-8") == "unchanged\n"


def test_raising_backend_identity_is_rejected_before_host_preflight(
    monkeypatch, boundary
):
    env, root = boundary
    env._raising = "is_local"
    target = root / "AGENTS.md"
    target.write_text("unchanged\n", encoding="utf-8")
    events = _host_probe_spy(monkeypatch)

    result = _json_write(str(target), "changed\n")

    assert result["error"] == "UNSUPPORTED: backend identity is unavailable or ambiguous"
    observed_events = list(events)
    assert observed_events == []
    assert env.execute_calls == []
    assert target.read_text(encoding="utf-8") == "unchanged\n"


@pytest.mark.parametrize("capability_mode", ["missing", "raising"])
@pytest.mark.parametrize("capability", ["cwd", "execute"])
def test_incomplete_local_capability_contract_fails_closed(
    monkeypatch, tmp_path, capability, capability_mode
):
    env = _BoundaryEnvironment(str(tmp_path))
    if capability_mode == "missing":
        env._missing = capability
    else:
        env._raising = capability
    monkeypatch.setattr(tt, "_active_environments", {"default": env})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(ft, "_file_ops_cache", {})
    monkeypatch.setattr(ft, "_read_tracker", {})
    monkeypatch.setattr(ft, "_patch_failure_tracker", {})
    file_state.get_registry().clear()
    try:
        target = tmp_path / "AGENTS.md"
        target.write_text("unchanged\n", encoding="utf-8")
        events = _host_probe_spy(monkeypatch)

        result = _json_write(str(target), "changed\n")

        assert result["error"] == "UNSUPPORTED: local POSIX capability contract is incomplete"
        assert events == []
        assert env.execute_calls == []
        assert target.read_text(encoding="utf-8") == "unchanged\n"
    finally:
        file_state.get_registry().clear()


def test_exact_local_capabilities_admit_once_without_class_name_heuristics(
    boundary,
):
    env, root = boundary
    env._local_value = True
    target = root / "ordinary.txt"

    result = _json_write(str(target), "content\n")

    assert "error" not in result
    assert env.identity_reads == 1


def test_remote_identity_cannot_preflight_an_existing_host_path(monkeypatch, boundary):
    env, root = boundary
    env._local_value = False
    target = root / "AGENTS.md"
    target.write_text("unchanged\n", encoding="utf-8")
    events = _host_probe_spy(monkeypatch)

    result = _json_write(str(target), "changed\n")

    assert result["error"] == "UNSUPPORTED: backend is not an exact local boolean"
    assert events == []
    assert env.execute_calls == []
    assert target.read_text(encoding="utf-8") == "unchanged\n"


def _install_real_local(monkeypatch, tmp_path):
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    monkeypatch.setattr(tt, "_active_environments", {"default": env})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(ft, "_file_ops_cache", {})
    monkeypatch.setattr(ft, "_read_tracker", {})
    monkeypatch.setattr(ft, "_patch_failure_tracker", {})
    file_state.get_registry().clear()
    return env


def test_final_symlink_reports_and_keys_the_lexical_entry(monkeypatch, tmp_path):
    _install_real_local(monkeypatch, tmp_path)
    physical = tmp_path / "physical.txt"
    lexical = tmp_path / "entry.txt"
    physical.write_text("old\n", encoding="utf-8")
    lexical.symlink_to(physical)
    lock_keys = []
    original_lock_path = ft.file_state.lock_path

    @contextmanager
    def tracking_lock(path):
        lock_keys.append(str(path))
        with original_lock_path(path):
            yield

    monkeypatch.setattr(ft.file_state, "lock_path", tracking_lock)

    result = _json_write(str(lexical), "new\n")

    assert result["resolved_path"] == str(lexical)
    assert result["lexical_path"] == str(lexical)
    assert result["identity_key"] == [str(tmp_path), "entry.txt"]
    assert result["files_modified"] == [str(lexical)]
    assert lock_keys == [str(lexical)]
    assert lexical.is_symlink()
    assert physical.read_text(encoding="utf-8") == "new\n"


def test_parent_symlink_stays_lexical_in_identity_and_reporting(monkeypatch, tmp_path):
    _install_real_local(monkeypatch, tmp_path)
    physical_parent = tmp_path / "physical-parent"
    physical_parent.mkdir()
    lexical_parent = tmp_path / "parent-entry"
    lexical_parent.symlink_to(physical_parent, target_is_directory=True)
    lexical_file = lexical_parent / "file.txt"

    result = _json_write(str(lexical_file), "new\n")

    assert result["resolved_path"] == str(lexical_file)
    assert result["lexical_path"] == str(lexical_file)
    assert result["identity_key"] == [str(lexical_parent), "file.txt"]
    assert lexical_file.is_file()
    assert (physical_parent / "file.txt").read_text(encoding="utf-8") == "new\n"
    assert lexical_parent.is_symlink()


@pytest.mark.parametrize(
    "cwd,path",
    [
        ("relative-cwd", "file.txt"),
        ("/tmp/cp1-invalid", ""),
        ("/tmp/cp1-invalid", "."),
        ("/tmp/cp1-invalid", ".."),
        ("/tmp/cp1-invalid", "/tmp/has\x00nul.txt"),
    ],
)
def test_invalid_lexical_target_fails_before_filesystem_inspection(
    monkeypatch, cwd, path
):
    env = _BoundaryEnvironment(cwd)
    monkeypatch.setattr(tt, "_active_environments", {"default": env})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(ft, "_file_ops_cache", {})
    monkeypatch.setattr(ft, "_read_tracker", {})
    monkeypatch.setattr(ft, "_patch_failure_tracker", {})
    file_state.get_registry().clear()
    events = _host_probe_spy(monkeypatch)

    try:
        result = _json_write(path, "changed\n")
    finally:
        file_state.get_registry().clear()

    assert result["error"].startswith("INVALID_LEXICAL_WRITE_TARGET:")
    assert events == []
    assert env.execute_calls == []


def test_backend_identity_precedes_every_host_inspection(monkeypatch, boundary):
    env, root = boundary
    events = []
    original_resolve = Path.resolve

    @property
    def identity(self):
        events.append("backend identity")
        return True

    type(env).is_local = identity

    def resolve(self, *args, **kwargs):
        events.append("host inspection")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    target = root / "AGENTS.md"

    _json_write(str(target), "changed\n")

    assert events
    assert events[0] == "backend identity"
    assert "host inspection" in events


def test_ordinary_write_patch_and_target_following_symlink_remain_compatible(
    monkeypatch, tmp_path
):
    _install_real_local(monkeypatch, tmp_path)
    regular = tmp_path / "regular.txt"
    regular.write_text("one\n", encoding="utf-8")

    write_result = _json_write(str(regular), "two\n")
    patch_result = json.loads(
        ft.patch_tool(
            mode="replace",
            path=str(regular),
            old_string="two",
            new_string="three",
        )
    )
    physical = tmp_path / "symlink-target.txt"
    lexical = tmp_path / "ordinary-link.txt"
    physical.write_text("target-old\n", encoding="utf-8")
    lexical.symlink_to(physical)
    symlink_result = _json_write(str(lexical), "target-new\n")

    assert "error" not in write_result
    assert "error" not in patch_result
    assert "error" not in symlink_result
    assert regular.read_text(encoding="utf-8") == "three\n"
    assert physical.read_text(encoding="utf-8") == "target-new\n"
    assert lexical.is_symlink()

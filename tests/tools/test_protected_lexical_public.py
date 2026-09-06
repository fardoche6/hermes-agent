"""Public protected-final-symlink contract tests.

These tests deliberately enter through write_file_tool/patch_tool.  The
OS-level failures use the reached descriptor primitive, not a test-only write
path.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.file_tools import patch_tool, write_file_tool
from tools.file_operations import ShellFileOperations
from tools.terminal_tool import set_approval_callback


@pytest.fixture(autouse=True)
def protected_test_cleanup(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    yield
    set_approval_callback(None)
    from tools.file_tools import clear_file_ops_cache

    clear_file_ops_cache()


def _approve(calls: list, answer: str = "once", callback=None):
    def approve(command, description, **kwargs):
        calls.append({"command": command, "description": description, **kwargs})
        if callback is not None:
            callback()
        return answer

    set_approval_callback(approve)


def _write(
    path: Path | str,
    content: str,
    calls: list,
    answer: str = "once",
    callback=None,
    task_id: str = "protected-public",
):
    _approve(calls, answer=answer, callback=callback)
    try:
        return json.loads(write_file_tool(str(path), content, task_id=task_id))
    finally:
        set_approval_callback(None)


def _patch(
    path: Path | str,
    old: str,
    new: str,
    calls: list,
    answer: str = "once",
    callback=None,
    task_id: str = "protected-public",
):
    _approve(calls, answer=answer, callback=callback)
    try:
        return json.loads(patch_tool(
            mode="replace",
            path=str(path),
            old_string=old,
            new_string=new,
            task_id=task_id,
        ))
    finally:
        set_approval_callback(None)


def _tree(tmp_path: Path):
    target = tmp_path / "external.txt"
    destination = tmp_path / "workspace" / "AGENTS.md"
    destination.parent.mkdir()
    target.write_bytes(b"before\n")
    destination.symlink_to(target)
    return target, destination


def test_public_write_replaces_only_protected_lexical_entry(tmp_path):
    target, destination = _tree(tmp_path)
    calls: list = []

    result = _write(destination, "after\n", calls)

    assert "error" not in result, result
    assert len(calls) == 1
    assert destination.is_symlink() is False
    assert destination.read_bytes() == b"after\n"
    assert target.read_bytes() == b"before\n"
    assert result["verified"] is True
    assert result["files_modified"] == [str(destination)]
    assert result["resolved_path"] == str(destination)
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == hashlib.sha256(b"after\n").hexdigest()


def test_public_patch_replaces_protected_lexical_entry(tmp_path):
    target, destination = _tree(tmp_path)
    calls: list = []

    result = _patch(destination, "before", "after", calls)

    assert result.get("success") is True, result
    assert len(calls) == 1
    assert destination.is_symlink() is False
    assert destination.read_bytes() == b"after\n"
    assert target.read_bytes() == b"before\n"
    assert result["files_modified"] == [str(destination)]


@pytest.mark.parametrize("answer", ["deny", "timeout"])
def test_denial_and_timeout_are_zero_change(tmp_path, answer):
    target, destination = _tree(tmp_path)
    calls: list = []

    result = _write(destination, "candidate\n", calls, answer=answer)

    assert result.get("error"), result
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"
    assert len(calls) == 1


@pytest.mark.parametrize("answer", ["deny", "timeout"])
def test_public_patch_denial_and_timeout_are_zero_change(tmp_path, answer):
    target, destination = _tree(tmp_path)
    calls: list = []

    result = _patch(destination, "before", "candidate", calls, answer=answer)

    assert result.get("error"), result
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"
    assert len(calls) == 1


def test_approval_exception_is_zero_change(tmp_path):
    target, destination = _tree(tmp_path)
    calls: list = []

    result = _write(
        destination,
        "candidate\n",
        calls,
        callback=lambda: (_ for _ in ()).throw(RuntimeError("approval callback failed")),
    )

    assert result.get("error"), result
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"


@pytest.mark.parametrize("target_kind", ["directory", "broken"])
def test_symlink_to_directory_or_broken_target_is_rejected_before_approval(
    tmp_path, target_kind,
):
    destination = tmp_path / "AGENTS.md"
    if target_kind == "directory":
        target = tmp_path / "directory"
        target.mkdir()
        destination.symlink_to(target, target_is_directory=True)
    else:
        destination.symlink_to(tmp_path / "missing-target")
    original_link = os.readlink(destination)
    calls: list = []

    result = _write(destination, "must not land\n", calls)

    assert result.get("error"), result
    assert calls == []
    assert destination.is_symlink()
    assert os.readlink(destination) == original_link
    assert list(tmp_path.glob(".hermes-protected-*.tmp")) == []


def test_intermediate_parent_symlink_fails_closed_before_approval(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    destination = alias / "AGENTS.md"
    calls: list = []

    result = _write(destination, "must not land\n", calls)

    assert result.get("error"), result
    assert calls == []
    assert not (real_parent / "AGENTS.md").exists()


def test_final_entry_race_after_approval_fails_closed(tmp_path):
    target, destination = _tree(tmp_path)
    alternate = tmp_path / "alternate.txt"
    alternate.write_bytes(b"alternate\n")
    calls: list = []

    def swap_final():
        destination.unlink()
        destination.symlink_to(alternate)

    result = _write(destination, "candidate\n", calls, callback=swap_final)

    assert result.get("error"), result
    assert destination.is_symlink()
    assert destination.resolve() == alternate
    assert target.read_bytes() == b"before\n"


def test_parent_identity_race_after_approval_fails_closed(tmp_path):
    target, destination = _tree(tmp_path)
    original_parent = destination.parent
    moved_parent = tmp_path / "moved-workspace"
    calls: list = []

    def replace_parent():
        original_parent.rename(moved_parent)
        original_parent.mkdir()

    result = _write(destination, "candidate\n", calls, callback=replace_parent)

    assert result.get("error"), result
    assert not destination.exists()
    assert (moved_parent / "AGENTS.md").is_symlink()
    assert target.read_bytes() == b"before\n"


def test_external_target_race_after_approval_fails_closed(tmp_path):
    target, destination = _tree(tmp_path)
    calls: list = []

    def drift_target():
        target.write_bytes(b"changed by another writer\n")

    result = _write(destination, "candidate\n", calls, callback=drift_target)

    assert result.get("error"), result
    assert destination.is_symlink()
    assert target.read_bytes() == b"changed by another writer\n"


def test_mixed_v4a_protected_patch_has_no_partial_application(tmp_path):
    target, protected = _tree(tmp_path)
    plain = tmp_path / "plain.txt"
    plain.write_text("plain\n", encoding="utf-8")
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {plain}\n"
        "@@\n"
        "-plain\n"
        "+changed\n"
        f"*** Update File: {protected}\n"
        "@@\n"
        "-before\n"
        "+changed\n"
        "*** End Patch"
    )
    calls: list = []
    _approve(calls)
    try:
        result = json.loads(patch_tool(mode="patch", patch=patch, task_id="protected-public"))
    finally:
        set_approval_callback(None)

    assert result.get("error"), result
    assert calls == []
    assert plain.read_text(encoding="utf-8") == "plain\n"
    assert protected.is_symlink()
    assert target.read_bytes() == b"before\n"


def test_ordinary_symlink_still_updates_physical_target(tmp_path):
    target = tmp_path / "external.txt"
    destination = tmp_path / "ordinary.txt"
    target.write_text("before\n", encoding="utf-8")
    destination.symlink_to(target)

    result = json.loads(write_file_tool(str(destination), "after\n", task_id="ordinary-symlink"))

    assert "error" not in result, result
    assert destination.is_symlink()
    assert target.read_text(encoding="utf-8") == "after\n"


def test_ordinary_symlink_patch_still_updates_physical_target(tmp_path):
    target = tmp_path / "external.txt"
    destination = tmp_path / "ordinary.txt"
    target.write_text("before\n", encoding="utf-8")
    destination.symlink_to(target)
    calls: list = []

    result = _patch(destination, "before", "after", calls)

    assert result.get("success") is True, result
    assert calls == []
    assert destination.is_symlink()
    assert target.read_text(encoding="utf-8") == "after\n"


class _RemoteEnvironment:
    is_local = False

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.calls = []

    def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return {"output": "", "returncode": 0}


def test_non_local_backend_is_rejected_before_approval_or_execution(tmp_path, monkeypatch):
    import tools.file_tools as file_tools

    target, destination = _tree(tmp_path)
    environment = _RemoteEnvironment(str(tmp_path))
    operations = ShellFileOperations(environment)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": operations)
    calls: list = []
    _approve(calls)
    try:
        result = json.loads(write_file_tool(str(destination), "candidate\n", task_id="remote"))
    finally:
        set_approval_callback(None)

    assert result.get("error"), result
    assert "non-local" in result["error"]
    assert calls == []
    assert environment.calls == []
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"


def test_non_local_backend_patch_is_rejected_before_approval_or_execution(tmp_path, monkeypatch):
    import tools.file_tools as file_tools

    target, destination = _tree(tmp_path)
    environment = _RemoteEnvironment(str(tmp_path))
    operations = ShellFileOperations(environment)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": operations)
    calls: list = []
    result = _patch(destination, "before", "candidate", calls, task_id="remote")

    assert result.get("error"), result
    assert "non-local" in result["error"]
    assert calls == []
    assert environment.calls == []
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"


class _RaisingLocalIdentity:
    def __init__(self, cwd: str):
        self.cwd = cwd
        self.calls = []

    @property
    def is_local(self):
        raise RuntimeError("backend identity unavailable")

    def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return {"output": "", "returncode": 0}


def test_missing_backend_identity_fails_closed_before_approval_or_execution(tmp_path, monkeypatch):
    import tools.file_tools as file_tools

    target, destination = _tree(tmp_path)
    environment = SimpleNamespace(cwd=str(tmp_path))
    operations = ShellFileOperations(environment)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": operations)
    calls: list = []
    _approve(calls)
    try:
        result = json.loads(write_file_tool(str(destination), "candidate\n", task_id="missing-local"))
    finally:
        set_approval_callback(None)

    assert result.get("error"), result
    assert "could not determine backend locality" in result["error"]
    assert calls == []
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"


def test_raising_backend_identity_fails_closed_before_approval_or_execution(tmp_path, monkeypatch):
    import tools.file_tools as file_tools

    target, destination = _tree(tmp_path)
    environment = _RaisingLocalIdentity(str(tmp_path))
    operations = ShellFileOperations(environment)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": operations)
    calls: list = []
    _approve(calls)
    try:
        result = json.loads(write_file_tool(str(destination), "candidate\n", task_id="raising-local"))
    finally:
        set_approval_callback(None)

    assert result.get("error"), result
    assert "could not determine backend locality" in result["error"]
    assert calls == []
    assert environment.calls == []
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"


def test_missing_local_capability_is_rejected_before_approval(tmp_path, monkeypatch):
    import tools.file_tools as file_tools
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)
    environment = SimpleNamespace(is_local=True, cwd=str(tmp_path))
    operations = ShellFileOperations(environment)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": operations)
    monkeypatch.setattr(primitive.os, "supports_dir_fd", set())
    calls: list = []
    _approve(calls)
    try:
        result = json.loads(write_file_tool(str(destination), "candidate\n", task_id="missing-cap"))
    finally:
        set_approval_callback(None)

    assert result.get("error"), result
    assert "descriptor-relative" in result["error"]
    assert calls == []
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"


@pytest.mark.parametrize("missing_capability", ["O_NOFOLLOW", "O_DIRECTORY", "fsync", "fchmod"])
def test_each_missing_no_follow_capability_fails_closed_before_approval(
    tmp_path, monkeypatch, missing_capability,
):
    import tools.file_tools as file_tools
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)
    environment = SimpleNamespace(is_local=True, cwd=str(tmp_path))
    operations = ShellFileOperations(environment)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": operations)
    monkeypatch.delattr(primitive.os, missing_capability, raising=False)
    calls: list = []
    _approve(calls)
    try:
        result = json.loads(write_file_tool(str(destination), "candidate\n", task_id="missing-cap"))
    finally:
        set_approval_callback(None)

    assert result.get("error"), result
    assert "no-follow descriptor primitives" in result["error"]
    assert calls == []
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"


@pytest.mark.parametrize("missing_function", ["open", "stat", "rename", "readlink", "symlink", "unlink"])
def test_each_missing_descriptor_relative_capability_fails_closed_before_approval(
    tmp_path, monkeypatch, missing_function,
):
    import tools.file_tools as file_tools
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)
    environment = SimpleNamespace(is_local=True, cwd=str(tmp_path))
    operations = ShellFileOperations(environment)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": operations)
    supported = set(primitive.os.supports_dir_fd)
    supported.discard(getattr(primitive.os, missing_function))
    monkeypatch.setattr(primitive.os, "supports_dir_fd", supported)
    calls: list = []
    _approve(calls)
    try:
        result = json.loads(write_file_tool(str(destination), "candidate\n", task_id="missing-cap"))
    finally:
        set_approval_callback(None)

    assert result.get("error"), result
    assert "descriptor-relative" in result["error"]
    assert calls == []
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"


def test_commit_failure_with_cleanup_failure_reports_uncertain_state(tmp_path, monkeypatch):
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)
    original_rename = primitive.os.rename
    original_unlink = primitive.os.unlink

    def fail_rename(*args, **kwargs):
        raise OSError("injected commit failure")

    def fail_temp_cleanup(name, *args, **kwargs):
        if str(name).startswith(".hermes-protected-"):
            raise OSError("injected cleanup failure")
        return original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(primitive.os, "rename", fail_rename)
    monkeypatch.setattr(primitive.os, "unlink", fail_temp_cleanup)
    monkeypatch.setattr(
        primitive.os,
        "supports_dir_fd",
        set(primitive.os.supports_dir_fd) | {fail_rename, fail_temp_cleanup},
    )
    calls: list = []

    result = _write(destination, "candidate\n", calls)

    assert result.get("error"), result
    assert "UNCERTAIN STATE" in result["error"]
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"
    # The uncertain result is honest: the failed cleanup leaves a named residue.
    assert list(destination.parent.glob(".hermes-protected-*.tmp"))


def test_post_commit_verification_uses_retained_parent_and_no_follow_final_leaf(
    tmp_path, monkeypatch,
):
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)
    original_stat = primitive.os.stat
    original_open = primitive.os.open
    final_stat_follow_flags: list[bool | None] = []
    final_stat_dir_fds: list[int] = []
    final_open_flags: list[int] = []
    final_open_dir_fds: list[int] = []

    def record_stat(path, *args, **kwargs):
        if path == "AGENTS.md" and kwargs.get("dir_fd") is not None:
            final_stat_follow_flags.append(kwargs.get("follow_symlinks"))
            final_stat_dir_fds.append(kwargs["dir_fd"])
        return original_stat(path, *args, **kwargs)

    def record_open(path, flags, *args, **kwargs):
        if path == "AGENTS.md" and kwargs.get("dir_fd") is not None:
            final_open_flags.append(flags)
            final_open_dir_fds.append(kwargs["dir_fd"])
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(primitive.os, "stat", record_stat)
    monkeypatch.setattr(primitive.os, "open", record_open)
    monkeypatch.setattr(
        primitive.os,
        "supports_dir_fd",
        set(primitive.os.supports_dir_fd) | {record_stat, record_open},
    )
    calls: list = []

    result = _write(destination, "candidate\n", calls)

    assert "error" not in result, result
    assert final_stat_follow_flags
    assert all(flag is False for flag in final_stat_follow_flags)
    assert len(set(final_stat_dir_fds)) == 1
    assert final_open_flags
    assert all(flags & os.O_NOFOLLOW for flags in final_open_flags)
    assert final_open_dir_fds == [final_stat_dir_fds[0]]
    assert destination.read_bytes() == b"candidate\n"
    assert target.read_bytes() == b"before\n"


def test_lexical_relative_and_absolute_spellings_share_entry(tmp_path, monkeypatch):
    import tools.file_tools as file_tools
    import tools.terminal_tool as terminal_tool

    target, destination = _tree(tmp_path)
    task_id = "relative-lexical"
    terminal_tool.record_session_cwd(task_id, str(tmp_path))
    file_tools.clear_file_ops_cache(task_id)
    calls: list = []

    result = _write(
        "workspace/AGENTS.md",
        "relative\n",
        calls,
        task_id=task_id,
    )
    assert "error" not in result, result
    assert not destination.is_symlink()
    assert destination.read_bytes() == b"relative\n"
    assert target.read_bytes() == b"before\n"
    assert result["resolved_path"] == str(destination)

    # The absolute spelling now addresses the same lexical regular entry.
    calls.clear()
    result = _write(destination, "absolute\n", calls)
    assert "error" not in result, result
    assert destination.read_bytes() == b"absolute\n"
    assert target.read_bytes() == b"before\n"


def test_public_patch_relative_and_absolute_spellings_share_entry(tmp_path):
    import tools.file_tools as file_tools
    import tools.terminal_tool as terminal_tool

    target, destination = _tree(tmp_path)
    task_id = "relative-patch-lexical"
    terminal_tool.record_session_cwd(task_id, str(tmp_path))
    file_tools.clear_file_ops_cache(task_id)
    calls: list = []

    result = _patch(
        "workspace/AGENTS.md",
        "before",
        "relative",
        calls,
        task_id=task_id,
    )
    assert result.get("success") is True, result
    assert not destination.is_symlink()
    assert destination.read_bytes() == b"relative\n"
    assert target.read_bytes() == b"before\n"
    assert result["resolved_path"] == str(destination)

    calls.clear()
    result = _patch(destination, "relative", "absolute", calls, task_id=task_id)
    assert result.get("success") is True, result
    assert destination.read_bytes() == b"absolute\n"
    assert target.read_bytes() == b"before\n"


@pytest.mark.parametrize("operation", ["write", "patch"])
def test_successful_result_contains_exact_regular_non_symlink_bytes(tmp_path, operation):
    target, destination = _tree(tmp_path)
    calls: list = []
    if operation == "write":
        result = _write(destination, "exact → bytes\n", calls)
    else:
        result = _patch(destination, "before", "exact → bytes", calls)

    assert "error" not in result, result
    assert destination.is_symlink() is False
    assert destination.read_bytes() == "exact → bytes\n".encode()
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == hashlib.sha256(
        "exact → bytes\n".encode()
    ).hexdigest()
    assert target.read_bytes() == b"before\n"

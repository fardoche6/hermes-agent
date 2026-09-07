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
    session_id: str | None = None,
):
    _approve(calls, answer=answer, callback=callback)
    try:
        return json.loads(write_file_tool(
            str(path), content, task_id=task_id, session_id=session_id))
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
    session_id: str | None = None,
):
    _approve(calls, answer=answer, callback=callback)
    try:
        return json.loads(patch_tool(
            mode="replace",
            path=str(path),
            old_string=old,
            new_string=new,
            task_id=task_id,
            session_id=session_id,
        ))
    finally:
        set_approval_callback(None)


def _v4a(
    path: Path | str,
    old: str,
    new: str,
    calls: list,
    answer: str = "once",
    callback=None,
    task_id: str = "protected-public",
    session_id: str | None = None,
):
    _approve(calls, answer=answer, callback=callback)
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n"
        "@@\n"
        f"-{old}\n"
        f"+{new}\n"
        "*** End Patch"
    )
    try:
        return json.loads(patch_tool(
            mode="patch", patch=patch, task_id=task_id, session_id=session_id))
    finally:
        set_approval_callback(None)


def _run_operation(
    operation: str,
    path: Path | str,
    old: str,
    new: str,
    calls: list,
    *,
    callback=None,
    task_id: str = "protected-public",
    session_id: str | None = None,
):
    if operation == "write":
        return _write(
            path, new, calls, callback=callback, task_id=task_id, session_id=session_id)
    if operation == "replace":
        return _patch(
            path, old, new, calls, callback=callback, task_id=task_id, session_id=session_id)
    return _v4a(
        path, old, new, calls, callback=callback, task_id=task_id, session_id=session_id)


def _tree(tmp_path: Path):
    target = tmp_path / "external.txt"
    destination = tmp_path / "workspace" / "AGENTS.md"
    destination.parent.mkdir()
    target.write_bytes(b"before\n")
    destination.symlink_to(target)
    return target, destination


def _assert_uncertain_cleanup(result, destination):
    assert result.get("error"), result
    assert "UNCERTAIN STATE" in result["error"]
    assert result.get("outcome") == "uncertain"
    assert result.get("files_modified") == [str(destination)]
    assert result.get("verified") is False
    assert result.get("success") is not True


def test_public_write_replaces_only_protected_lexical_entry(tmp_path):
    target, destination = _tree(tmp_path)
    calls: list = []

    result = _write(destination, "after\n", calls)

    _assert_uncertain_cleanup(result, destination)
    assert len(calls) == 1
    assert destination.is_symlink() is False
    assert destination.read_bytes() == b"after\n"
    assert target.read_bytes() == b"before\n"
    assert result["resolved_path"] == str(destination)
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == hashlib.sha256(b"after\n").hexdigest()


def test_public_patch_replaces_protected_lexical_entry(tmp_path):
    target, destination = _tree(tmp_path)
    calls: list = []

    result = _patch(destination, "before", "after", calls)

    _assert_uncertain_cleanup(result, destination)
    assert len(calls) == 1
    assert destination.is_symlink() is False
    assert destination.read_bytes() == b"after\n"
    assert target.read_bytes() == b"before\n"


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


@pytest.mark.parametrize(
    ("initial_kind", "operation"),
    [
        ("regular", "write"),
        ("regular", "replace"),
        ("regular", "v4a"),
        ("absent", "write"),
        ("absent", "replace"),
        ("absent", "v4a"),
    ],
)
def test_regular_or_absent_leaf_race_after_approval_fails_closed(
    tmp_path, initial_kind, operation,
):
    external = tmp_path / "external.txt"
    destination = tmp_path / "workspace" / "AGENTS.md"
    destination.parent.mkdir()
    external.write_bytes(b"before\n")
    if initial_kind == "regular":
        destination.write_bytes(b"before\n")

    calls: list = []

    def swap_final():
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        destination.symlink_to(external)

    result = _run_operation(
        operation,
        destination,
        "before",
        "after\n",
        calls,
        callback=swap_final,
    )

    assert result.get("error"), result
    assert len(calls) == 1
    assert destination.is_symlink()
    assert external.read_bytes() == b"before\n"
    assert result.get("files_modified", []) == []
    assert result.get("resolved_path") != str(external)
    assert result.get("verified") is not True
    assert result.get("success") is not True


@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_rename_committed_then_raised_reports_committed_path(
    tmp_path, monkeypatch, operation,
):
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)
    original_rename = primitive._rename
    rename_called = False

    def rename_then_raise(*args, **kwargs):
        nonlocal rename_called
        original_rename(*args, **kwargs)
        rename_called = True
        raise OSError("injected after-rename failure")

    monkeypatch.setattr(primitive, "_rename", rename_then_raise)
    monkeypatch.setattr(
        primitive.os,
        "supports_dir_fd",
        set(primitive.os.supports_dir_fd) | {rename_then_raise},
    )
    calls: list = []

    result = _run_operation(operation, destination, "before", "after", calls)

    assert rename_called is True
    _assert_uncertain_cleanup(result, destination)
    assert "possibly modified" in result["error"]
    assert "rollback" not in result["error"].lower()
    assert destination.read_bytes() == (b"after" if operation == "write" else b"after\n")
    assert target.read_bytes() == b"before\n"


@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_rename_rejected_before_install_reports_not_committed(
    tmp_path, monkeypatch, operation,
):
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)

    def reject_rename(*args, **kwargs):
        raise OSError("injected before-rename failure")

    monkeypatch.setattr(primitive, "_rename", reject_rename)
    calls: list = []

    result = _run_operation(operation, destination, "before", "after", calls)

    _assert_uncertain_cleanup(result, destination)
    assert "rollback" not in result["error"].lower()
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"
    assert list(destination.parent.glob(".hermes-protected-*.tmp"))


@pytest.mark.parametrize("fault", ["mutation", "observation"])
@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_post_rename_mutation_or_observation_is_uncertain(
    tmp_path, monkeypatch, operation, fault,
):
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)
    original_rename = primitive._rename
    original_stat = primitive.os.stat
    rename_done = False
    fault_fired = False

    def record_rename(*args, **kwargs):
        nonlocal rename_done
        result = original_rename(*args, **kwargs)
        rename_done = True
        return result

    def fail_or_mutate_after_rename(path, *args, **kwargs):
        nonlocal fault_fired
        if (
            rename_done
            and not fault_fired
            and path == "AGENTS.md"
            and kwargs.get("dir_fd") is not None
        ):
            fault_fired = True
            if fault == "mutation":
                destination.write_bytes(b"newer after rename\n")
            else:
                raise OSError("injected post-rename observation failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(primitive, "_rename", record_rename)
    monkeypatch.setattr(primitive.os, "stat", fail_or_mutate_after_rename)
    monkeypatch.setattr(
        primitive.os,
        "supports_dir_fd",
        set(primitive.os.supports_dir_fd) | {record_rename, fail_or_mutate_after_rename},
    )
    calls: list = []

    result = _run_operation(operation, destination, "before", "after", calls)

    assert fault_fired is True
    assert result.get("error"), result
    assert "UNCERTAIN STATE" in result["error"]
    assert "possibly modified" in result["error"]
    assert "rollback" not in result["error"].lower()
    assert result.get("outcome") == "uncertain"
    assert result.get("files_modified") == [str(destination)]
    assert result.get("verified") is False
    assert result.get("success") is not True
    assert destination.read_bytes() == (
        b"newer after rename\n"
        if fault == "mutation"
        else (b"after" if operation == "write" else b"after\n")
    )
    assert target.read_bytes() == b"before\n"


@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_in_place_drift_after_approval_fails_closed_before_commit(
    tmp_path, operation,
):
    destination = tmp_path / "workspace" / "AGENTS.md"
    destination.parent.mkdir()
    destination.write_bytes(b"before\n")
    calls: list = []

    def change_in_place_during_approval():
        with destination.open("wb") as handle:
            handle.write(
                b"concurrent\n" if operation == "write" else b"before\nconcurrent\n"
            )

    result = _run_operation(
        operation,
        destination,
        "before",
        "candidate",
        calls,
        callback=change_in_place_during_approval,
    )

    assert len(calls) == 1
    assert result.get("error"), result
    assert "rollback" not in result["error"].lower()
    assert result.get("files_modified", []) == []
    assert result.get("verified") is False
    assert result.get("success") is not True
    assert result.get("outcome") == "not_committed"
    assert destination.read_bytes() == (
        b"concurrent\n" if operation == "write" else b"before\nconcurrent\n"
    )


@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_late_in_place_drift_is_not_accepted_as_a_new_preimage(
    tmp_path, monkeypatch, operation,
):
    import tools.file_operations as file_operations

    destination = tmp_path / "workspace" / "AGENTS.md"
    destination.parent.mkdir()
    destination.write_bytes(b"before\n")
    original_read_candidate = file_operations.read_candidate
    first_read = True
    calls: list = []

    def read_then_drift(candidate):
        nonlocal first_read
        content = original_read_candidate(candidate)
        if first_read:
            first_read = False
            with destination.open("wb") as handle:
                handle.write(
                    b"concurrent\n" if operation == "write" else b"before\nconcurrent\n"
                )
        return content

    monkeypatch.setattr(file_operations, "read_candidate", read_then_drift)
    result = _run_operation(operation, destination, "before", "candidate", calls)

    assert first_read is False
    assert result.get("error"), result
    assert result.get("files_modified", []) == []
    assert result.get("verified") is False
    assert result.get("outcome") == "not_committed"
    assert result.get("success") is not True
    assert "rollback" not in result["error"].lower()
    assert destination.read_bytes() == (
        b"concurrent\n" if operation == "write" else b"before\nconcurrent\n"
    )


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


@pytest.mark.parametrize("initial_kind", ["regular", "symlink"])
def test_ordinary_v4a_still_updates_physical_target(tmp_path, initial_kind):
    target = tmp_path / "external.txt"
    destination = tmp_path / "ordinary.txt"
    target.write_text("before\n", encoding="utf-8")
    if initial_kind == "symlink":
        destination.symlink_to(target)
    else:
        destination.write_text("before\n", encoding="utf-8")
        target = destination

    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {destination}\n"
        "@@\n"
        "-before\n"
        "+after\n"
        "*** End Patch"
    )
    result = json.loads(patch_tool(mode="patch", patch=patch, task_id="ordinary-v4a"))

    assert result.get("success") is True, result
    assert target.read_text(encoding="utf-8") == "after\n"
    assert destination.is_symlink() is (initial_kind == "symlink")


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
    original_rename = primitive._rename
    original_unlink = primitive.os.unlink

    def fail_rename(*args, **kwargs):
        raise OSError("injected commit failure")

    def fail_temp_cleanup(name, *args, **kwargs):
        if str(name).startswith(".hermes-protected-"):
            raise OSError("injected cleanup failure")
        return original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(primitive, "_rename", fail_rename)
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

    _assert_uncertain_cleanup(result, destination)
    assert final_stat_follow_flags
    assert all(flag is False for flag in final_stat_follow_flags)
    assert len(set(final_stat_dir_fds)) == 1
    assert final_open_flags
    assert all(flags & os.O_NOFOLLOW for flags in final_open_flags)
    assert all(fd == final_stat_dir_fds[0] for fd in final_open_dir_fds)
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
    _assert_uncertain_cleanup(result, destination)
    assert not destination.is_symlink()
    assert destination.read_bytes() == b"relative\n"
    assert target.read_bytes() == b"before\n"
    assert result["resolved_path"] == str(destination)

    # The absolute spelling now addresses the same lexical regular entry.
    calls.clear()
    result = _write(destination, "absolute\n", calls)
    _assert_uncertain_cleanup(result, destination)
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
    _assert_uncertain_cleanup(result, destination)
    assert not destination.is_symlink()
    assert destination.read_bytes() == b"relative\n"
    assert target.read_bytes() == b"before\n"

    calls.clear()
    result = _patch(destination, "relative", "absolute", calls, task_id=task_id)
    _assert_uncertain_cleanup(result, destination)
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

    _assert_uncertain_cleanup(result, destination)
    assert destination.is_symlink() is False
    assert destination.read_bytes() == "exact → bytes\n".encode()
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == hashlib.sha256(
        "exact → bytes\n".encode()
    ).hexdigest()
    assert target.read_bytes() == b"before\n"


@pytest.mark.parametrize("drift", ["classification", "config", "path"])
@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_approval_uses_immutable_preflight_classification(
    tmp_path, monkeypatch, operation, drift,
):
    import tools.file_tools as file_tools
    import tools.file_tools_write_guards as guards

    target, destination = _tree(tmp_path)
    original_reason = guards._protected_instruction_reason
    reason_calls = 0
    config_calls = 0

    def changing_config():
        nonlocal config_calls
        config_calls += 1
        if drift == "config" and config_calls > 1:
            return False, []
        return True, []

    def changing_reason(path, task_id="default", **kwargs):
        nonlocal reason_calls
        reason_calls += 1
        if reason_calls == 1:
            return original_reason(path, task_id, **kwargs)
        if drift == "path":
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            destination.write_bytes(b"second classification interloper\n")
        return None

    monkeypatch.setattr(file_tools, "_protected_instruction_config", changing_config)
    monkeypatch.setattr(guards, "_protected_instruction_config", changing_config)
    monkeypatch.setattr(file_tools, "_protected_instruction_reason", changing_reason)
    monkeypatch.setattr(guards, "_protected_instruction_reason", changing_reason)
    calls: list = []

    result = _run_operation(operation, destination, "before", "after", calls)

    assert reason_calls == 1
    assert config_calls == 1
    assert len(calls) == 1
    _assert_uncertain_cleanup(result, destination)
    assert destination.is_symlink() is False
    assert destination.read_bytes() == (b"after" if operation == "write" else b"after\n")
    assert target.read_bytes() == b"before\n"


@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_failed_temp_creation_never_unlinks_unowned_replacement(
    tmp_path, monkeypatch, operation,
):
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)
    original_open = primitive.os.open
    created_name: list[str] = []

    def fail_temp_create(path, flags, *args, **kwargs):
        if (
            kwargs.get("dir_fd") is not None
            and isinstance(path, str)
            and path.startswith(".hermes-protected-")
        ):
            replacement = destination.parent / path
            replacement.write_bytes(b"unowned replacement\n")
            created_name.append(path)
            raise FileExistsError(path)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(primitive.os, "open", fail_temp_create)
    monkeypatch.setattr(
        primitive.os,
        "supports_dir_fd",
        set(primitive.os.supports_dir_fd) | {fail_temp_create},
    )
    calls: list = []

    result = _run_operation(operation, destination, "before", "after", calls)

    assert len(created_name) == 1
    residue = destination.parent / created_name[0]
    assert residue.read_bytes() == b"unowned replacement\n"
    assert result.get("error"), result
    assert result.get("outcome") == "not_committed"
    assert result.get("files_modified") == []
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"


@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_temp_cleanup_preserves_replacement_after_bound_creation(
    tmp_path, monkeypatch, operation,
):
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)
    original_fsync = primitive.os.fsync
    swapped = False

    def fail_staging_after_replacement(fd):
        nonlocal swapped
        original_fsync(fd)
        if not swapped:
            residues = list(destination.parent.glob(".hermes-protected-*.tmp"))
            assert len(residues) == 1
            residue = residues[0]
            residue.unlink()
            residue.write_bytes(b"replacement after creation\n")
            swapped = True
        raise OSError("injected staging failure")

    monkeypatch.setattr(primitive.os, "fsync", fail_staging_after_replacement)
    calls: list = []

    result = _run_operation(operation, destination, "before", "after", calls)

    residues = list(destination.parent.glob(".hermes-protected-*.tmp"))
    assert swapped is True
    assert len(residues) == 1
    assert residues[0].read_bytes() == b"replacement after creation\n"
    assert result.get("error"), result
    assert "UNCERTAIN STATE" in result["error"]
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"


@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_atomic_exchange_rolls_back_raced_final_without_overwrite(
    tmp_path, monkeypatch, operation,
):
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)
    original_rename = primitive._rename
    raced = False
    replacement_name = ".raced-protected-final"
    replacement_bytes = b"unapproved final replacement\n"

    def replace_before_exchange(source, final_name, parent_fd):
        nonlocal raced
        if not raced:
            fd = os.open(
                replacement_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                os.write(fd, replacement_bytes)
            finally:
                os.close(fd)
            os.rename(
                replacement_name,
                final_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            raced = True
        return original_rename(source, final_name, parent_fd)

    monkeypatch.setattr(primitive, "_rename", replace_before_exchange)
    calls: list = []

    result = _run_operation(
        operation,
        destination,
        "before",
        "candidate",
        calls,
    )

    assert raced is True, result
    assert result.get("error"), result
    assert result.get("outcome") == "uncertain"
    assert result.get("files_modified") == [str(destination)]
    assert result.get("verified") is False
    assert result.get("success") is not True
    assert "could not be proven restored" in result["error"]
    assert destination.is_symlink() is False
    assert destination.read_bytes() == replacement_bytes
    assert target.read_bytes() == b"before\n"
    residues = list(destination.parent.glob(".hermes-protected-*.tmp"))
    assert len(residues) == 1
    expected_candidate = b"candidate" if operation == "write" else b"candidate\n"
    assert residues[0].read_bytes() == expected_candidate


@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_atomic_cleanup_preserves_raced_unowned_temp_and_reports_uncertainty(
    tmp_path, monkeypatch, operation,
):
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)
    original_noreplace = primitive._rename_noreplace
    original_unlink = primitive.os.unlink
    raced = False
    replacement_bytes = b"unowned cleanup replacement\n"
    unlinks: list[str] = []

    def reject_commit(*args, **kwargs):
        raise OSError("injected atomic commit failure")

    def replace_before_cleanup_transfer(source, quarantine, parent_fd):
        nonlocal raced
        if not raced and source.startswith(".hermes-protected-"):
            original_unlink(source, dir_fd=parent_fd)
            fd = os.open(
                source,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                os.write(fd, replacement_bytes)
            finally:
                os.close(fd)
            raced = True
        return original_noreplace(source, quarantine, parent_fd)

    def record_unlink(name, *args, **kwargs):
        unlinks.append(name)
        return original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(primitive, "_rename", reject_commit)
    monkeypatch.setattr(primitive, "_rename_noreplace", replace_before_cleanup_transfer)
    monkeypatch.setattr(primitive.os, "unlink", record_unlink)
    monkeypatch.setattr(
        primitive.os,
        "supports_dir_fd",
        set(primitive.os.supports_dir_fd) | {record_unlink},
    )
    calls: list = []

    result = _run_operation(
        operation,
        destination,
        "before",
        "candidate",
        calls,
    )

    assert raced is True, result
    assert result.get("error"), result
    assert "leaving it in quarantine" in result["error"]
    assert result.get("outcome") == "uncertain"
    assert result.get("verified") is False
    assert result.get("success") is not True
    assert unlinks == []
    residues = list(destination.parent.glob(".hermes-protected-*.tmp"))
    assert any(residue.read_bytes() == replacement_bytes for residue in residues)
    assert destination.is_symlink()
    assert target.read_bytes() == b"before\n"


@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_atomic_cleanup_preserves_raced_unowned_temp_at_terminal_removal_seam(
    tmp_path, monkeypatch, operation,
):
    import tools.protected_lexical_write as primitive

    target, destination = _tree(tmp_path)
    original_remove = primitive._remove_bound_quarantine
    original_matches = primitive._entry_matches
    original_unlink = primitive.os.unlink
    replacement_bytes = b"unowned terminal cleanup replacement\n"
    terminal_calls: list[str] = []
    validated_names: list[str] = []

    def record_match(candidate, name, expected, **kwargs):
        result = original_matches(candidate, name, expected, **kwargs)
        if result:
            validated_names.append(name)
        return result

    def replace_at_terminal(candidate, quarantine):
        assert quarantine in validated_names
        terminal_calls.append(quarantine)
        original_unlink(quarantine, dir_fd=candidate.parent_fd)
        fd = os.open(
            quarantine,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=candidate.parent_fd,
        )
        try:
            os.write(fd, replacement_bytes)
        finally:
            os.close(fd)
        return original_remove(candidate, quarantine)

    monkeypatch.setattr(primitive, "_entry_matches", record_match)
    monkeypatch.setattr(primitive, "_remove_bound_quarantine", replace_at_terminal)
    calls: list = []

    result = _run_operation(
        operation,
        destination,
        "before",
        "candidate",
        calls,
    )

    assert len(terminal_calls) == 1, result
    assert result.get("error"), result
    assert "UNCERTAIN STATE" in result["error"]
    assert result.get("outcome") == "uncertain"
    assert result.get("verified") is False
    assert result.get("success") is not True
    residues = list(destination.parent.glob(".hermes-protected-*.tmp"))
    assert any(residue.read_bytes() == replacement_bytes for residue in residues)
    assert destination.is_symlink() is False
    expected_candidate = b"candidate" if operation == "write" else b"candidate\n"
    assert destination.read_bytes() == expected_candidate
    assert target.read_bytes() == b"before\n"


@pytest.mark.parametrize("outcome", ["committed", "uncertain"])
@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_protected_terminal_outcome_updates_write_and_verification_state(
    tmp_path, monkeypatch, operation, outcome,
):
    from agent.verification_evidence import record_verify_run, verification_status
    from tools import file_state
    from tools.file_tools_read_tracking import _patch_failure_tracker, _record_patch_failure

    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'protected-bookkeeping'\nversion = '0'\n",
        encoding="utf-8",
    )
    target = project / "external.txt"
    destination = project / "AGENTS.md"
    target.write_bytes(b"before\n")
    destination.symlink_to(target)
    task_id = f"protected-bookkeeping-{operation}-{outcome}"
    file_state.get_registry().clear()
    _patch_failure_tracker.clear()
    record_verify_run(root=project, session_id=task_id, ok=True)

    import tools.protected_lexical_write as primitive

    if operation != "write":
        _record_patch_failure(task_id, str(destination))
    if outcome == "committed":
        original_rename = primitive._rename

        def rename_then_raise(*args, **kwargs):
            original_rename(*args, **kwargs)
            raise OSError("injected after-rename failure")

        monkeypatch.setattr(primitive, "_rename", rename_then_raise)
        monkeypatch.setattr(
            primitive.os,
            "supports_dir_fd",
            set(primitive.os.supports_dir_fd) | {rename_then_raise},
        )
    else:
        original_rename = primitive._rename
        original_stat = primitive.os.stat
        rename_done = False
        fault_fired = False

        def record_rename(*args, **kwargs):
            nonlocal rename_done
            result = original_rename(*args, **kwargs)
            rename_done = True
            return result

        def mutate_after_rename(path, *args, **kwargs):
            nonlocal fault_fired
            if (
                rename_done
                and not fault_fired
                and path == "AGENTS.md"
                and kwargs.get("dir_fd") is not None
            ):
                fault_fired = True
                destination.write_bytes(b"newer after rename\n")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(primitive, "_rename", record_rename)
        monkeypatch.setattr(primitive.os, "stat", mutate_after_rename)
        monkeypatch.setattr(
            primitive.os,
            "supports_dir_fd",
            set(primitive.os.supports_dir_fd) | {record_rename, mutate_after_rename},
        )

    calls: list = []
    result = _run_operation(
        operation,
        destination,
        "before",
        "after",
        calls,
        task_id=task_id,
        session_id=task_id,
    )

    assert result.get("error"), result
    expected_outcome = "uncertain" if outcome == "committed" else outcome
    assert result.get("outcome") == expected_outcome
    assert result.get("files_modified") == [str(destination)]
    assert result.get("verified") is False
    writes = file_state.writes_since("observer", 0.0, [str(destination)])
    assert task_id in writes
    assert writes[task_id] == [str(destination)]
    assert verification_status(session_id=task_id, cwd=project)["status"] == "stale"
    if operation != "write":
        assert str(destination) not in _patch_failure_tracker.get(task_id, {})


@pytest.mark.parametrize("visibility", ["disappeared", "unobservable"])
@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_possible_protected_write_keeps_verification_and_last_writer_evidence(
    tmp_path, monkeypatch, operation, visibility,
):
    from agent.verification_evidence import record_verify_run, verification_status
    from tools import file_state

    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'possible-write-bookkeeping'\nversion = '0'\n",
        encoding="utf-8",
    )
    target = project / "external.txt"
    destination = project / "AGENTS.md"
    target.write_bytes(b"before\n")
    destination.symlink_to(target)
    task_id = f"possible-write-{operation}-{visibility}"
    file_state.get_registry().clear()
    record_verify_run(root=project, session_id=task_id, ok=True)

    import tools.protected_lexical_write as primitive

    original_rename = primitive._rename
    unobservable = False

    def rename_then_fail(*args, **kwargs):
        nonlocal unobservable
        original_rename(*args, **kwargs)
        if visibility == "disappeared":
            destination.unlink()
        else:
            unobservable = True
        raise OSError("injected after-rename observation failure")

    monkeypatch.setattr(primitive, "_rename", rename_then_fail)
    monkeypatch.setattr(
        primitive.os,
        "supports_dir_fd",
        set(primitive.os.supports_dir_fd) | {rename_then_fail},
    )
    if visibility == "unobservable":
        import tools.file_tools as file_tools

        original_lstat = file_tools.os.lstat

        def hide_leaf(path):
            if unobservable and path == str(destination):
                raise OSError("injected lexical observation failure")
            return original_lstat(path)

        monkeypatch.setattr(file_tools.os, "lstat", hide_leaf)

    calls: list = []
    result = _run_operation(
        operation,
        destination,
        "before",
        "after",
        calls,
        task_id=task_id,
        session_id=task_id,
    )

    assert result.get("error"), result
    assert result.get("outcome") in {"committed", "uncertain"}
    assert result.get("files_modified") == [str(destination)]
    assert result.get("verified") is False
    writes = file_state.writes_since("observer", 0.0, [str(destination)])
    assert writes.get(task_id) == [str(destination)]
    assert file_state.known_reads(task_id) == []
    assert verification_status(session_id=task_id, cwd=project)["status"] == "stale"


@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_protected_staging_uncertainty_without_modified_path_preserves_read_timestamp(
    tmp_path, monkeypatch, operation,
):
    from tools.file_tools import read_file_tool
    from tools.file_tools_read_tracking import _read_tracker

    destination = tmp_path / "AGENTS.md"
    destination.write_bytes(b"before\n")
    task_id = f"protected-staging-drift-{operation}"
    json.loads(read_file_tool(str(destination), task_id=task_id))
    before_timestamp = _read_tracker[task_id]["read_timestamps"][str(destination)]

    import tools.protected_lexical_write as primitive

    original_fsync = primitive.os.fsync
    fault_fired = False

    def fail_staging_after_guarded_drift(fd):
        nonlocal fault_fired
        original_fsync(fd)
        if not fault_fired:
            residues = list(destination.parent.glob(".hermes-protected-*.tmp"))
            assert len(residues) == 1
            residue = residues[0]
            residue.unlink()
            residue.write_bytes(b"replacement during staging\n")
            destination.write_bytes(b"concurrent\n")
            fault_fired = True
        raise OSError("injected staging uncertainty")

    monkeypatch.setattr(primitive.os, "fsync", fail_staging_after_guarded_drift)
    calls: list = []
    result = _run_operation(
        operation,
        destination,
        "before",
        "candidate",
        calls,
        task_id=task_id,
    )

    assert fault_fired is True
    assert result.get("error"), result
    assert result.get("outcome") == "uncertain"
    assert result.get("files_modified") == []
    assert destination.read_bytes() == b"concurrent\n"
    assert _read_tracker[task_id]["read_timestamps"][str(destination)] == before_timestamp


@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
def test_not_committed_drift_does_not_refresh_read_timestamp(
    tmp_path, operation,
):
    from tools.file_tools import read_file_tool
    from tools.file_tools_read_tracking import _read_tracker

    destination = tmp_path / "AGENTS.md"
    destination.write_bytes(b"before\n")
    task_id = f"protected-drift-{operation}"
    json.loads(read_file_tool(str(destination), task_id=task_id))
    before_timestamp = _read_tracker[task_id]["read_timestamps"][str(destination)]
    calls: list = []

    def drift_in_place():
        destination.write_bytes(
            b"concurrent\n" if operation == "write" else b"before\nconcurrent\n"
        )

    result = _run_operation(
        operation,
        destination,
        "before",
        "candidate",
        calls,
        callback=drift_in_place,
        task_id=task_id,
    )

    assert len(calls) == 1
    assert result.get("error"), result
    assert result.get("outcome") == "not_committed"
    assert result.get("files_modified") == []
    assert _read_tracker[task_id]["read_timestamps"][str(destination)] == before_timestamp

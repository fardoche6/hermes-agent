"""Behavior contract for protected final-file symlink writes.

These tests exercise public file-tool boundaries and intentionally remain red
until protected lexical-final-symlink handling is implemented.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import stat
from pathlib import Path

import pytest

from tools.file_tools import patch_tool, write_file_tool
from tools.terminal_tool import set_approval_callback


def _result(raw: str) -> dict:
    return json.loads(raw)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _state(root: Path) -> dict:
    entries = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == "hermes_test":
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            payload = "<directory>"
        elif stat.S_ISLNK(info.st_mode):
            payload = os.readlink(path)
        elif not stat.S_ISREG(info.st_mode):
            payload = f"<special:{stat.S_IFMT(info.st_mode)}>"
        else:
            payload = path.read_bytes()
        entries[str(rel)] = (stat.S_IFMT(info.st_mode), info.st_dev, info.st_ino, payload)
    return entries


@pytest.fixture(autouse=True)
def _clear_approval_callback():
    set_approval_callback(None)
    yield
    set_approval_callback(None)


@pytest.fixture
def symlink_capability(tmp_path: Path):
    capability_dir = tmp_path / "hermes_test"
    capability_dir.mkdir(exist_ok=True)
    target = capability_dir / ".symlink-capability-target"
    link = capability_dir / ".symlink-capability-link"
    target.write_bytes(b"capability")
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"test-owned symlink capability unavailable: {exc}")
    if not link.is_symlink():
        pytest.skip("test-owned symlink capability unavailable")
    link.unlink()


def _protected_link(tmp_path: Path, *, target_bytes: bytes = b"external\n"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "external-target.txt"
    target.write_bytes(target_bytes)
    link = tmp_path / "AGENTS.md"
    link.symlink_to("external-target.txt")
    return link, target


def _assert_unchanged(root: Path, before: dict):
    assert _state(root) == before


_PACKET_FIELDS = {
    "candidate", "candidate_sha256", "lexical_path", "lexical_parent",
    "parent_dev", "parent_ino", "final_type", "final_dev", "final_ino",
    "final_raw_target", "target_path", "target_type", "target_dev",
    "target_ino", "target_sha256", "operation", "target_effect",
    "approval_scope",
}


def _packet_fields(command: str, description: str) -> dict[str, str]:
    packet = description
    assert packet.startswith("PROTECTED_PACKET_V1\n")
    parsed = {}
    for line in packet.splitlines()[1:]:
        field, encoded = line.split("=", 1)
        assert field in _PACKET_FIELDS
        assert field not in parsed
        parsed[field] = base64.urlsafe_b64decode(encoded).decode()
    return parsed


def _assert_complete_packet(packet: dict[str, str]) -> None:
    assert set(packet) == _PACKET_FIELDS


def _assert_no_affirmative_rollback(result: dict) -> None:
    assert set(result) == {"error", "uncertain", "rollback"}
    assert result["uncertain"] is True
    assert result["rollback"] == "not_attempted"
    for field in ("success", "verified", "rolled_back", "rollback_succeeded"):
        assert result.get(field) is not True
    text = str(result.get("error", "")).lower()
    assert not re.search(
        r"(?:rollback|rolled back|rolled-back).*(?:completed|succeeded|successful|successfully)",
        text,
    )
    assert not re.search(r"(?:successfully|successful|succeeded)\s+(?:rollback|rolled back)", text)
    assert "UNCERTAIN STATE" in text.upper()


def test_approved_protected_final_symlink_replaces_link_and_preserves_target(
    tmp_path: Path, symlink_capability,
):
    link, target = _protected_link(tmp_path)
    candidate = b"approved replacement\n"
    seen = []

    def approve(command, description, **kwargs):
        seen.append((command, description, kwargs))
        assert kwargs["allow_permanent"] is False
        assert kwargs["allow_session"] is False
        return "once"

    target_before = target.read_bytes()
    target_before_stat = target.stat()
    before = _state(tmp_path)
    set_approval_callback(approve)
    result = _result(write_file_tool(str(link), candidate.decode()))

    assert not result.get("error"), result
    assert len(seen) == 1
    assert not link.is_symlink()
    assert link.read_bytes() == candidate
    assert target.stat().st_ino == target_before_stat.st_ino
    assert target.read_bytes() == target_before
    assert _sha(target_before) == _sha(target.read_bytes())
    after = _state(tmp_path)
    assert set(after) == set(before)
    assert after["AGENTS.md"][0] == stat.S_IFREG
    assert after["external-target.txt"][1:3] == before["external-target.txt"][1:3]
    assert not any("hermes-protected-" in name for name in after)


def test_approval_packet_binds_every_operation_field(tmp_path: Path, symlink_capability):
    link, target = _protected_link(tmp_path)
    candidate = b"new bytes\n"
    seen = []

    def approve(command, description, **kwargs):
        seen.append((command, description, kwargs))
        return "deny"

    set_approval_callback(approve)
    result = _result(write_file_tool(str(link), candidate.decode()))
    assert result.get("error")
    assert len(seen) == 1
    command, description, kwargs = seen[0]
    packet = command + "\n" + description
    link_stat = link.lstat()
    target_stat = target.stat()
    required = {
        "candidate": candidate.decode(),
        "candidate_sha256": _sha(candidate),
        "lexical_path": str(link.absolute()),
        "lexical_parent": str(link.parent.absolute()),
        "parent_dev": str(link.parent.stat().st_dev),
        "parent_ino": str(link.parent.stat().st_ino),
        "final_type": "symlink",
        "final_dev": str(link_stat.st_dev),
        "final_ino": str(link_stat.st_ino),
        "final_raw_target": os.readlink(link),
        "target_path": str(target.absolute()),
        "target_type": "regular",
        "target_dev": str(target_stat.st_dev),
        "target_ino": str(target_stat.st_ino),
        "target_sha256": _sha(target.read_bytes()),
        "operation": "replace",
        "target_effect": "untouched",
        "approval_scope": "one operation",
    }
    parsed = _packet_fields(command, description)
    _assert_complete_packet(parsed)
    assert parsed == required
    duplicate = description + "\n" + description.splitlines()[1]
    with pytest.raises(AssertionError):
        _packet_fields(command, duplicate)
    assert kwargs["allow_permanent"] is False
    assert kwargs["allow_session"] is False


def _capture_denied_packet(tmp_path: Path, candidate: str, link: Path, packets: list):
    set_approval_callback(lambda command, description, **_kwargs: (
        packets.append(_packet_fields(command, description)) or "deny"
    ))
    result = _result(write_file_tool(str(link), candidate))
    assert result.get("error")


def test_candidate_delta_changes_only_candidate_bindings(tmp_path: Path, symlink_capability):
    link, _target = _protected_link(tmp_path)
    packets = []
    _capture_denied_packet(tmp_path, "A\n", link, packets)
    _capture_denied_packet(tmp_path, "B\n", link, packets)
    assert len(packets) == 2
    for packet in packets:
        _assert_complete_packet(packet)
    assert packets[0]["candidate"] != packets[1]["candidate"]
    assert packets[0]["candidate_sha256"] != packets[1]["candidate_sha256"]
    for field in _PACKET_FIELDS - {"candidate", "candidate_sha256"}:
        assert packets[0][field] == packets[1][field], field


def test_path_delta_changes_only_path_and_parent_bindings(tmp_path: Path, symlink_capability):
    link, _target = _protected_link(tmp_path)
    renamed = tmp_path / "CLAUDE.md"
    packets = []
    _capture_denied_packet(tmp_path, "A\n", link, packets)
    link.rename(renamed)
    _capture_denied_packet(tmp_path, "A\n", renamed, packets)
    assert len(packets) == 2
    for packet in packets:
        _assert_complete_packet(packet)
    assert packets[0]["lexical_path"] != packets[1]["lexical_path"]
    assert packets[0]["lexical_parent"] == packets[1]["lexical_parent"]
    assert packets[0]["final_ino"] == packets[1]["final_ino"]
    for field in _PACKET_FIELDS - {"lexical_path"}:
        assert packets[0][field] == packets[1][field], field


def test_preimage_delta_changes_only_target_identity_and_hash(tmp_path: Path, symlink_capability):
    link, target = _protected_link(tmp_path)
    packets = []
    _capture_denied_packet(tmp_path, "A\n", link, packets)
    original = target.read_bytes()
    target.write_bytes(b"changed preimage\n")
    _capture_denied_packet(tmp_path, "A\n", link, packets)
    assert len(packets) == 2
    for packet in packets:
        _assert_complete_packet(packet)
    assert packets[0]["target_sha256"] != packets[1]["target_sha256"]
    assert packets[0]["target_ino"] == packets[1]["target_ino"]
    assert packets[0]["target_dev"] == packets[1]["target_dev"]
    for field in _PACKET_FIELDS - {"target_sha256"}:
        assert packets[0][field] == packets[1][field], field
    target.write_bytes(original)


def test_once_approval_does_not_authorize_reprompted_denied_operation(
    tmp_path: Path, symlink_capability,
):
    link_a, _target_a = _protected_link(tmp_path / "a")
    link_b, _target_b = _protected_link(tmp_path / "b")
    before_a = _state(tmp_path / "a")
    before_b = _state(tmp_path / "b")
    packets = []

    def approve(command, description, **_kwargs):
        packets.append(_packet_fields(command, description))
        return "once" if len(packets) == 1 else "deny"

    set_approval_callback(approve)
    first = _result(write_file_tool(str(link_a), "A\n"))
    second = _result(write_file_tool(str(link_b), "B\n"))
    assert first.get("error") is None
    assert second.get("error")
    assert len(packets) == 2
    _assert_complete_packet(packets[0])
    _assert_complete_packet(packets[1])
    assert packets[0]["candidate_sha256"] != packets[1]["candidate_sha256"]
    assert _state(tmp_path / "a") != before_a
    assert _state(tmp_path / "b") == before_b


@pytest.mark.parametrize("decision", ["deny", "timeout"])
def test_denial_and_timeout_are_complete_zero_change(tmp_path: Path, decision: str, symlink_capability):
    link, target = _protected_link(tmp_path)
    before = _state(tmp_path)
    calls = []
    set_approval_callback(lambda *args, **kwargs: calls.append((args, kwargs)) or decision)
    result = _result(write_file_tool(str(link), "must not land\n"))
    assert result.get("error")
    assert len(calls) == 1
    expected_failure = {"deny": "denied", "timeout": "timed out"}[decision]
    assert expected_failure in result["error"].lower()
    _assert_unchanged(tmp_path, before)


def test_callback_exception_is_complete_zero_change(tmp_path: Path, symlink_capability):
    _protected_link(tmp_path)
    before = _state(tmp_path)

    calls = []
    def explode(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("approval callback failed")

    set_approval_callback(explode)
    result = _result(write_file_tool(str(tmp_path / "AGENTS.md"), "must not land\n"))
    assert result.get("error")
    assert "denied" in result["error"].lower()
    assert calls == [1]
    _assert_unchanged(tmp_path, before)


def test_deterministic_pre_rename_failure_is_complete_zero_change(
    tmp_path: Path, symlink_capability, monkeypatch,
):
    link, _target = _protected_link(tmp_path)
    before = _state(tmp_path)
    approvals = []
    calls = []
    set_approval_callback(lambda *args, **kwargs: approvals.append((args, kwargs)) or "once")
    def fail_atomic(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("pre-rename fault")
    monkeypatch.setattr("tools.file_tools.os.rename", fail_atomic)
    monkeypatch.setattr("tools.file_tools.os.replace", fail_atomic)
    result = _result(write_file_tool(str(link), "must not land\n"))
    assert result.get("error")
    assert "pre-rename fault" in result["error"]
    assert len(approvals) == 1
    assert len(calls) == 1
    assert calls[0][1].get("src_dir_fd") is not None
    assert calls[0][1].get("src_dir_fd") == calls[0][1].get("dst_dir_fd")
    src, dst = calls[0][0]
    assert src != dst and Path(src).name == src and "/" not in src
    assert dst == "AGENTS.md"
    _assert_unchanged(tmp_path, before)


@pytest.mark.parametrize("raw_name", ["./AGENTS.md", "sub/../AGENTS.md"])
def test_literal_dot_and_traversal_paths_reject_before_approval(
    tmp_path: Path, raw_name: str, symlink_capability,
):
    owned = tmp_path / "owned"
    owned.mkdir()
    if raw_name.startswith("sub/"):
        (owned / "sub").mkdir()
    link = owned / "AGENTS.md"
    link.symlink_to("target.txt")
    (owned / "target.txt").write_bytes(b"safe\n")
    before = _state(tmp_path)
    calls = []
    set_approval_callback(lambda *args, **kwargs: calls.append((args, kwargs)) or "once")
    raw = f"{owned}/{raw_name}"
    result = _result(write_file_tool(raw, "nope\n"))
    assert result.get("error")
    assert calls == []
    assert _state(tmp_path) == before
    assert link.is_symlink()


@pytest.mark.parametrize("shape", ["intermediate", "directory", "broken", "nonregular", "special"])
def test_unsupported_final_or_intermediate_shapes_reject_before_approval(
    tmp_path: Path, shape: str, symlink_capability,
):
    calls = []
    set_approval_callback(lambda *args, **kwargs: calls.append((args, kwargs)) or "once")
    if shape == "intermediate":
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "target.txt").write_bytes(b"safe")
        (tmp_path / "real" / "AGENTS.md").symlink_to("target.txt")
        (tmp_path / "alias").symlink_to("real", target_is_directory=True)
        path = tmp_path / "alias" / "AGENTS.md"
    elif shape == "directory":
        path = tmp_path / "AGENTS.md"
        path.mkdir()
    elif shape == "broken":
        path = tmp_path / "AGENTS.md"
        path.symlink_to("missing-target")
    elif shape == "nonregular":
        target = tmp_path / "target-directory"
        target.mkdir()
        path = tmp_path / "AGENTS.md"
        path.symlink_to(target, target_is_directory=True)
    else:
        target = tmp_path / "special-target"
        try:
            special_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            special_socket.bind(str(target))
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"Unix-domain socket capability unavailable: {exc}")
        path = tmp_path / "AGENTS.md"
        path.symlink_to(target.name)
        special_socket.close()
    before = _state(tmp_path)
    result = _result(write_file_tool(str(path), "nope\n"))
    assert result.get("error")
    assert calls == []
    assert _state(tmp_path) == before


def test_ordinary_nonprotected_final_symlink_behavior_is_unchanged(tmp_path: Path, symlink_capability):
    target = tmp_path / "ordinary-target.txt"
    target.write_text("old\n")
    link = tmp_path / "ordinary.txt"
    link.symlink_to(target)
    result = _result(write_file_tool(str(link), "new\n"))
    assert not result.get("error"), result
    assert link.is_symlink()
    assert target.read_text() == "new\n"


@pytest.mark.parametrize("mode", ["replace", "patch"])
def test_protected_final_symlink_rejects_replace_and_v4a_before_approval(
    tmp_path: Path, mode: str, symlink_capability,
):
    link, target = _protected_link(tmp_path)
    innocent = tmp_path / "innocent.txt"
    innocent.write_text("safe\n")
    before = _state(tmp_path)
    calls = []
    set_approval_callback(lambda *args, **kwargs: calls.append((args, kwargs)) or "once")
    if mode == "replace":
        result = _result(patch_tool(
            mode="replace", path=str(link), old_string="external\n", new_string="changed\n"
        ))
    else:
        patch = ("*** Begin Patch\n" f"*** Update File: {innocent}\n@@\n-safe\n+unsafe\n"
                 f"*** Update File: {link}\n@@\n-external\n+changed\n" "*** End Patch")
        result = _result(patch_tool(mode="patch", patch=patch))
    assert result.get("error")
    assert calls == []
    _assert_unchanged(tmp_path, before)


@pytest.mark.parametrize("drift", ["parent", "final_identity", "final_text", "target_identity", "target_hash"])
def test_parent_final_and_target_drift_aborts_without_candidate_landing(
    tmp_path: Path, drift: str, symlink_capability,
):
    link, target = _protected_link(tmp_path / "parent" if drift == "parent" else tmp_path)
    candidate = b"stale packet\n"
    before = _state(tmp_path)
    expected = {}

    def approve(*_args, **_kwargs):
        drift_link = link
        if drift == "parent":
            original_parent = drift_link.parent
            replacement = tmp_path / "replacement"
            old_parent_stat = original_parent.stat()
            old_link_stat = drift_link.lstat()
            old_target_stat = target.stat()
            old_raw_target = os.readlink(drift_link)
            original_parent.rename(replacement)
            original_parent.mkdir()
            os.rename(replacement / drift_link.name, original_parent / drift_link.name)
            drift_link = original_parent / drift_link.name
            assert original_parent.stat().st_ino != old_parent_stat.st_ino
            assert (original_parent.stat().st_dev, original_parent.stat().st_ino) != (
                old_parent_stat.st_dev, old_parent_stat.st_ino
            )
            assert drift_link.lstat().st_ino == old_link_stat.st_ino
            os.rename(replacement / target.name, original_parent / target.name)
            assert (original_parent / target.name).stat().st_ino == old_target_stat.st_ino
            assert os.readlink(drift_link) == old_raw_target
        elif drift == "final_identity":
            drift_link.rename(drift_link.with_name("original-final"))
            drift_link.symlink_to("external-target.txt")
        elif drift == "final_text":
            drift_link.unlink()
            drift_link.symlink_to("./external-target.txt")
        elif drift == "target_identity":
            target.rename(target.with_name("original-target"))
            target.write_bytes(b"external\n")
        else:
            target.write_bytes(b"drifted\n")
        expected.update(_state(tmp_path))
        return "once"

    set_approval_callback(approve)
    result = _result(write_file_tool(str(link), candidate.decode()))
    assert result.get("error")
    assert candidate not in [p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()]
    assert _state(tmp_path) == expected
    if drift == "parent":
        assert (tmp_path / "parent" / "AGENTS.md").is_symlink()
        assert (tmp_path / "replacement" / "AGENTS.md").exists() is False
    elif drift in {"final_identity", "final_text"}:
        assert link.is_symlink()
    else:
        assert target.exists()
    assert before != expected


def test_descriptor_relative_failure_has_no_path_fallback(tmp_path: Path, symlink_capability, monkeypatch):
    link, target = _protected_link(tmp_path)
    before = _state(tmp_path)
    approvals = []
    set_approval_callback(lambda *_args, **_kwargs: approvals.append(1) or "once")
    calls = []
    def fail_descriptor(*args, **kwargs):
        calls.append((args, kwargs))
        if kwargs.get("src_dir_fd") is not None or kwargs.get("dst_dir_fd") is not None:
            raise OSError("descriptor-relative fault")
        raise OSError("path fallback attempted")
    monkeypatch.setattr("tools.file_tools.os.rename", fail_descriptor)
    monkeypatch.setattr("tools.file_tools.os.replace", fail_descriptor)
    result = _result(write_file_tool(str(link), "candidate\n"))
    assert result.get("error")
    assert "descriptor-relative fault" in result["error"]
    assert len(approvals) == 1
    _assert_unchanged(tmp_path, before)
    assert link.is_symlink()
    assert target.read_bytes() == b"external\n"
    assert len(calls) == 1
    src, dst = calls[0][0]
    kwargs = calls[0][1]
    assert kwargs.get("src_dir_fd") is not None
    assert kwargs.get("src_dir_fd") == kwargs.get("dst_dir_fd")
    assert Path(src).name == src and Path(dst).name == dst
    assert "/" not in src and "/" not in dst
    assert src != dst and Path(src).name == src and "/" not in src
    assert dst == "AGENTS.md"


def test_post_rename_verification_failure_reports_uncertain_state(tmp_path: Path, symlink_capability, monkeypatch):
    link, target = _protected_link(tmp_path)
    approvals = []
    set_approval_callback(lambda *_args, **_kwargs: approvals.append(1) or "once")
    target_before = target.stat()
    target_before_hash = _sha(target.read_bytes())
    commit_calls = []
    committed = False
    original_rename = os.rename
    original_replace = os.replace
    def wrapped_rename(*args, **kwargs):
        nonlocal committed
        commit_calls.append((args, kwargs, "rename"))
        result = original_rename(*args, **kwargs)
        committed = True
        return result
    def wrapped_replace(*args, **kwargs):
        nonlocal committed
        commit_calls.append((args, kwargs, "replace"))
        result = original_replace(*args, **kwargs)
        committed = True
        return result
    monkeypatch.setattr("tools.file_tools.os.rename", wrapped_rename)
    monkeypatch.setattr("tools.file_tools.os.replace", wrapped_replace)
    verify_faults = []
    original_lstat = os.lstat
    def fail_verify(*args, **kwargs):
        if committed and not verify_faults:
            verify_faults.append((args, kwargs))
            raise OSError("post-commit verification fault")
        return original_lstat(*args, **kwargs)
    monkeypatch.setattr("tools.file_tools.os.lstat", fail_verify)
    result = _result(write_file_tool(str(link), "candidate\n"))
    assert result.get("error")
    assert len(approvals) == 1
    assert len(commit_calls) == 1
    assert len(verify_faults) == 1
    assert "post-commit verification fault" in result["error"]
    _assert_no_affirmative_rollback(result)
    commit_args, commit_kwargs, _primitive = commit_calls[0]
    assert commit_kwargs.get("src_dir_fd") is not None
    assert commit_kwargs.get("src_dir_fd") == commit_kwargs.get("dst_dir_fd")
    assert len(commit_args) == 2
    assert all(Path(arg).name == arg and "/" not in arg for arg in commit_args)
    assert Path(verify_faults[0][0][0]).name == verify_faults[0][0][0]
    assert verify_faults[0][0][0] == "AGENTS.md"
    assert verify_faults[0][1].get("dir_fd") == commit_kwargs.get("src_dir_fd")
    assert not link.is_symlink()
    assert link.read_bytes() == b"candidate\n"
    assert target.read_bytes() == b"external\n"
    assert (target.stat().st_dev, target.stat().st_ino) == (target_before.st_dev, target_before.st_ino)
    assert _sha(target.read_bytes()) == target_before_hash
    assert _state(tmp_path) == {
        "AGENTS.md": (stat.S_IFREG, target_before.st_dev, link.stat().st_ino, b"candidate\n"),
        "external-target.txt": (
            stat.S_IFREG, target_before.st_dev, target_before.st_ino, b"external\n"
        ),
    }


def test_regular_protected_multifile_v4a_remains_single_atomic_approval(tmp_path: Path):
    protected = tmp_path / "AGENTS.md"
    protected.write_text("old\n")
    innocent = tmp_path / "innocent.txt"
    innocent.write_text("safe\n")
    calls = []
    set_approval_callback(lambda *args, **kwargs: calls.append((args, kwargs)) or "deny")
    patch = ("*** Begin Patch\n" f"*** Update File: {innocent}\n@@\n-safe\n+unsafe\n"
             f"*** Update File: {protected}\n@@\n-old\n+new\n" "*** End Patch")
    result = _result(patch_tool(mode="patch", patch=patch))
    assert result.get("error")
    assert len(calls) == 1
    assert protected.read_text() == "old\n"
    assert innocent.read_text() == "safe\n"
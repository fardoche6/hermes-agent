"""Descriptor-bound replacement of a protected final symlink.

This module is deliberately local-only.  The public file-tool pipeline performs
all ordinary guards, approval, locking, and result shaping; this module owns only
the no-follow filesystem primitive used after those stages have admitted a
protected final symlink.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import dataclass

from tools.file_operations_common import WriteResult


class ProtectedLexicalError(RuntimeError):
    """A protected lexical replacement could not be completed safely."""


class ProtectedLexicalNotApplicable(ProtectedLexicalError):
    """The lexical entry is absent or already a regular file."""


@dataclass(frozen=True)
class BackendAdmission:
    """Capability decision made without inspecting a destination path."""

    is_local: bool
    supported: bool
    error: str | None = None

    @classmethod
    def for_environment(cls, environment) -> "BackendAdmission":
        """Admit only an explicitly local backend with the required dir-fd APIs."""
        try:
            local = getattr(environment, "is_local")
        except Exception as exc:  # pragma: no cover - hostile provider object
            return cls(False, False, f"could not determine backend locality: {exc}")
        if local is not True:
            if local is False:
                reason = "protected lexical replacement is unavailable for a non-local backend"
            else:
                reason = "protected lexical replacement requires an explicit local backend capability"
            return cls(False, False, reason)

        if sys.platform == "win32" or os.name == "nt":
            return cls(True, False, "protected lexical replacement is unavailable on Windows")
        required_names = ("O_NOFOLLOW", "O_DIRECTORY", "fsync", "fchmod")
        if not all(hasattr(os, name) for name in required_names):
            return cls(
                True,
                False,
                "protected lexical replacement requires no-follow descriptor primitives",
            )
        supports_dir_fd = getattr(os, "supports_dir_fd", ())
        required_functions = (
            os.open,
            os.stat,
            os.rename,
            os.readlink,
            os.symlink,
            os.unlink,
        )
        if not all(function in supports_dir_fd for function in required_functions):
            return cls(
                True,
                False,
                "protected lexical replacement requires descriptor-relative no-follow filesystem APIs",
            )
        return cls(True, True)


@dataclass
class ProtectedLexicalCandidate:
    """Preimage retained from one lexical parent and its final symlink."""

    path: str
    parent_path: str
    final_name: str
    parent_fd: int
    parent_identity: dict
    final_identity: dict
    link_text: str
    target_path: str
    target_identity: dict
    target_sha256: str
    candidate_mode: int
    candidate_bytes: bytes = b""
    candidate_sha256: str = ""
    closed: bool = False

    def set_content(self, content: str | bytes) -> None:
        if isinstance(content, str):
            payload = content.encode("utf-8", "surrogateescape")
        else:
            payload = bytes(content)
        self.candidate_bytes = payload
        self.candidate_sha256 = hashlib.sha256(payload).hexdigest()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.parent_fd >= 0:
            fd, self.parent_fd = self.parent_fd, -1
            os.close(fd)


def lexical_path(path: str, cwd: str | None) -> str | None:
    """Normalize a path lexically, never following a symlink."""
    try:
        raw = os.fspath(path)
    except TypeError:
        return None
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    if raw == "~" or raw.startswith("~/"):
        raw = os.path.expanduser(raw)
    if not os.path.isabs(raw):
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return None
        raw = os.path.join(cwd, raw)
    return os.path.normpath(raw)


def _identity(st: os.stat_result) -> dict:
    return {
        "st_dev": int(st.st_dev),
        "st_ino": int(st.st_ino),
        "st_mode": int(st.st_mode),
    }


def _same_identity(expected: dict, actual: dict) -> bool:
    return all(expected.get(key) == actual.get(key) for key in ("st_dev", "st_ino", "st_mode"))


def _entry_identity(st: os.stat_result, link_text: str) -> dict:
    return {"kind": "symlink", **_identity(st), "link_target": link_text}


def _open_directory(path: str) -> int:
    """Open each parent component without following any symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(os.sep, flags)
    try:
        for component in path.split(os.sep)[1:]:
            if not component:
                continue
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_regular(path: str) -> tuple[bytes, dict]:
    parent_fd = _open_directory(os.path.dirname(path))
    fd = -1
    try:
        fd = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ProtectedLexicalError("protected external target is not a regular file")
        data = _read_fd(fd)
        return data, _identity(file_stat)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _target_path(parent_path: str, link_text: str) -> str:
    if not link_text or "\x00" in link_text:
        raise ProtectedLexicalError("protected final symlink has an invalid target")
    target = link_text if os.path.isabs(link_text) else os.path.join(parent_path, link_text)
    target = os.path.normpath(target)
    if not os.path.isabs(target) or not os.path.basename(target):
        raise ProtectedLexicalError("protected final symlink target is ambiguous")
    return target


def _random_name(parent_fd: int) -> str:
    for _ in range(32):
        name = f".hermes-protected-{os.urandom(12).hex()}.tmp"
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return name
    raise ProtectedLexicalError("could not allocate a protected temporary name")


def _unlink(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return


def _rename(source: str, destination: str, parent_fd: int) -> None:
    """Rename only through the retained parent descriptor."""
    os.rename(source, destination, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


def prepare_candidate(
    path: str,
    cwd: str | None,
    admission: BackendAdmission | None = None,
) -> ProtectedLexicalCandidate | None:
    """Capture a regular-file final symlink, or return None when not applicable."""
    admission = admission or BackendAdmission.for_environment(_LocalCapabilityMarker())
    if not admission.supported:
        raise ProtectedLexicalError(admission.error or "protected lexical capability unavailable")
    absolute = lexical_path(path, cwd)
    if absolute is None:
        raise ProtectedLexicalError("protected path is not an unambiguous absolute lexical path")
    # A protected replacement must not reinterpret dot components after approval.
    raw = os.fspath(path)
    parts = raw.replace("\\", "/").split("/")
    if os.path.isabs(raw) and parts:
        parts = parts[1:]
    if any(part in ("", ".", "..") for part in parts):
        raise ProtectedLexicalError("protected path contains an ambiguous dot or empty component")
    parent_path = os.path.dirname(absolute)
    final_name = os.path.basename(absolute)
    parent_fd = -1
    try:
        try:
            parent_fd = _open_directory(parent_path)
        except FileNotFoundError:
            raise ProtectedLexicalNotApplicable(absolute) from None
        parent_identity = _identity(os.fstat(parent_fd))
        try:
            final_stat = os.stat(final_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise ProtectedLexicalNotApplicable(absolute) from None
        if not stat.S_ISLNK(final_stat.st_mode):
            if stat.S_ISREG(final_stat.st_mode):
                raise ProtectedLexicalNotApplicable(absolute)
            raise ProtectedLexicalError("protected final entry is not a regular-file symlink")
        link_text = os.readlink(final_name, dir_fd=parent_fd)
        target = _target_path(parent_path, link_text)
        try:
            target_bytes, target_identity = _read_regular(target)
        except FileNotFoundError as exc:
            raise ProtectedLexicalError("protected final symlink target is missing") from exc
        return ProtectedLexicalCandidate(
            path=absolute,
            parent_path=parent_path,
            final_name=final_name,
            parent_fd=parent_fd,
            parent_identity=parent_identity,
            final_identity=_entry_identity(final_stat, link_text),
            link_text=link_text,
            target_path=target,
            target_identity=target_identity,
            target_sha256=hashlib.sha256(target_bytes).hexdigest(),
            candidate_mode=stat.S_IMODE(target_identity["st_mode"]),
        )
    except Exception:
        if parent_fd >= 0:
            os.close(parent_fd)
        raise


class _LocalCapabilityMarker:
    """Only used by direct helper calls that do not have a terminal object."""

    is_local = True


def _check_parent(candidate: ProtectedLexicalCandidate) -> None:
    held = _identity(os.fstat(candidate.parent_fd))
    if not _same_identity(candidate.parent_identity, held):
        raise ProtectedLexicalError("protected destination parent descriptor changed")
    fresh_fd = _open_directory(candidate.parent_path)
    try:
        fresh = _identity(os.fstat(fresh_fd))
    finally:
        os.close(fresh_fd)
    if not _same_identity(candidate.parent_identity, fresh):
        raise ProtectedLexicalError("protected destination parent path changed")


def _revalidate(candidate: ProtectedLexicalCandidate) -> None:
    _check_parent(candidate)
    final_stat = os.stat(candidate.final_name, dir_fd=candidate.parent_fd, follow_symlinks=False)
    if not stat.S_ISLNK(final_stat.st_mode):
        raise ProtectedLexicalError("protected final symlink changed after approval")
    link_text = os.readlink(candidate.final_name, dir_fd=candidate.parent_fd)
    if not _same_identity(candidate.final_identity, _entry_identity(final_stat, link_text)):
        raise ProtectedLexicalError("protected final symlink changed after approval")
    if link_text != candidate.link_text:
        raise ProtectedLexicalError("protected final symlink target changed after approval")
    target = _target_path(candidate.parent_path, link_text)
    if target != candidate.target_path:
        raise ProtectedLexicalError("protected external target path changed after approval")
    target_bytes, target_identity = _read_regular(target)
    if not _same_identity(candidate.target_identity, target_identity):
        raise ProtectedLexicalError("protected external target identity changed after approval")
    if hashlib.sha256(target_bytes).hexdigest() != candidate.target_sha256:
        raise ProtectedLexicalError("protected external target changed after approval")


def _write_temp(candidate: ProtectedLexicalCandidate) -> str:
    name = _random_name(candidate.parent_fd)
    fd = -1
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=candidate.parent_fd,
        )
        view = memoryview(candidate.candidate_bytes)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ProtectedLexicalError("protected temporary write made no progress")
            view = view[written:]
        os.fchmod(fd, candidate.candidate_mode & 0o7777)
        os.fsync(fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ProtectedLexicalError("protected temporary is not regular")
        return name
    except Exception as exc:
        if fd >= 0:
            os.close(fd)
            fd = -1
        try:
            _unlink(candidate.parent_fd, name)
        except Exception as cleanup_error:
            raise ProtectedLexicalError(
                f"UNCERTAIN STATE: protected temporary write failed and cleanup failed: {cleanup_error}"
            ) from exc
        raise
    finally:
        if fd >= 0:
            os.close(fd)


def _final_is_candidate(candidate: ProtectedLexicalCandidate) -> bool:
    try:
        final_stat = os.stat(candidate.final_name, dir_fd=candidate.parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(final_stat.st_mode):
            return False
        fd = os.open(candidate.final_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=candidate.parent_fd)
        try:
            return hashlib.sha256(_read_fd(fd)).hexdigest() == candidate.candidate_sha256
        finally:
            os.close(fd)
    except OSError:
        return False


def _rollback(candidate: ProtectedLexicalCandidate) -> None:
    """Restore the symlink only while the retained parent still names the same directory."""
    _check_parent(candidate)
    if not _final_is_candidate(candidate):
        raise ProtectedLexicalError("protected destination changed before rollback")
    restore_name = _random_name(candidate.parent_fd)
    try:
        os.symlink(candidate.link_text, restore_name, dir_fd=candidate.parent_fd)
        _rename(restore_name, candidate.final_name, candidate.parent_fd)
        restore_name = ""
        os.fsync(candidate.parent_fd)
        final_stat = os.stat(candidate.final_name, dir_fd=candidate.parent_fd, follow_symlinks=False)
        if not stat.S_ISLNK(final_stat.st_mode) or os.readlink(
            candidate.final_name, dir_fd=candidate.parent_fd
        ) != candidate.link_text:
            raise ProtectedLexicalError("protected symlink rollback verification failed")
        target_bytes, target_identity = _read_regular(candidate.target_path)
        if not _same_identity(candidate.target_identity, target_identity):
            raise ProtectedLexicalError("protected target identity changed during rollback")
        if hashlib.sha256(target_bytes).hexdigest() != candidate.target_sha256:
            raise ProtectedLexicalError("protected target changed during rollback")
    finally:
        if restore_name:
            _unlink(candidate.parent_fd, restore_name)


def _cleanup_temp(candidate: ProtectedLexicalCandidate, name: str | None) -> str | None:
    if not name:
        return None
    try:
        _unlink(candidate.parent_fd, name)
    except Exception as exc:
        return str(exc)
    return None


def commit_candidate(candidate: ProtectedLexicalCandidate) -> WriteResult:
    """Replace the final entry and prove the no-follow postcondition."""
    temp_name: str | None = None
    try:
        if not candidate.candidate_sha256:
            raise ProtectedLexicalError("protected candidate content was not prepared")
        _revalidate(candidate)
        temp_name = _write_temp(candidate)
        _revalidate(candidate)
        try:
            _rename(temp_name, candidate.final_name, candidate.parent_fd)
            temp_name = None
        except Exception as rename_error:
            committed = _final_is_candidate(candidate)
            if committed:
                try:
                    _rollback(candidate)
                except Exception as rollback_error:
                    cleanup_error = _cleanup_temp(candidate, temp_name)
                    suffix = f"; temporary cleanup failed: {cleanup_error}" if cleanup_error else ""
                    return WriteResult(
                        error=(
                            "UNCERTAIN STATE: protected rename raised after a possible commit "
                            f"and rollback could not be verified: {rollback_error}{suffix}"
                        )
                    )
            cleanup_error = _cleanup_temp(candidate, temp_name)
            if cleanup_error:
                return WriteResult(
                    error=(
                        "UNCERTAIN STATE: protected rename failed and temporary cleanup failed: "
                        f"{cleanup_error}"
                    )
                )
            return WriteResult(error=f"protected lexical replacement failed: {rename_error}")

        try:
            os.fsync(candidate.parent_fd)
        except Exception as fsync_error:
            try:
                _rollback(candidate)
            except Exception as rollback_error:
                return WriteResult(
                    error=(
                        "UNCERTAIN STATE: protected parent commit was not durable and rollback "
                        f"could not be verified: {rollback_error}"
                    )
                )
            return WriteResult(
                error=f"protected parent commit fsync failed; rollback verified: {fsync_error}"
            )

        try:
            _check_parent(candidate)
            final_stat = os.stat(candidate.final_name, dir_fd=candidate.parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(final_stat.st_mode):
                raise ProtectedLexicalError("destination is not a regular non-symlink")
            fd = os.open(candidate.final_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=candidate.parent_fd)
            try:
                final_hash = hashlib.sha256(_read_fd(fd)).hexdigest()
            finally:
                os.close(fd)
            if final_hash != candidate.candidate_sha256:
                raise ProtectedLexicalError("destination hash differs from requested bytes")
            target_bytes, target_identity = _read_regular(candidate.target_path)
            if not _same_identity(candidate.target_identity, target_identity):
                raise ProtectedLexicalError("external target identity changed")
            if hashlib.sha256(target_bytes).hexdigest() != candidate.target_sha256:
                raise ProtectedLexicalError("external target changed")
        except Exception as post_error:
            try:
                _rollback(candidate)
            except Exception as rollback_error:
                return WriteResult(
                    error=(
                        "UNCERTAIN STATE: protected postcondition failed and rollback could not "
                        f"be verified: {rollback_error}"
                    )
                )
            return WriteResult(error=f"protected postcondition failed; rollback verified: {post_error}")
        return WriteResult(bytes_written=len(candidate.candidate_bytes), verified=True)
    except Exception as exc:
        cleanup_error = _cleanup_temp(candidate, temp_name)
        if cleanup_error:
            return WriteResult(
                error=f"UNCERTAIN STATE: protected replacement failed and cleanup failed: {cleanup_error}"
            )
        return WriteResult(error=f"protected lexical replacement failed: {exc}")
    finally:
        candidate.close()

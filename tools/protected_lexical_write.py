"""Descriptor-bound replacement of a protected final symlink.

This module is deliberately local-only.  The public file-tool pipeline performs
all ordinary guards, approval, locking, and result shaping; this module owns only
the no-follow filesystem primitive used after those stages have admitted a
protected final symlink.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
import sys
from dataclasses import dataclass


_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_O_PATH = getattr(os, "O_PATH", 0)
_LIBC = None

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
        required_names = ("O_NOFOLLOW", "O_DIRECTORY", "O_PATH", "fsync", "fchmod")
        if not all(hasattr(os, name) for name in required_names):
            return cls(
                True,
                False,
                "protected lexical replacement requires no-follow descriptor primitives",
            )
        if _renameat2_function() is None:
            return cls(
                True,
                False,
                "protected lexical replacement requires renameat2 atomic rename support",
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


@dataclass(frozen=True)
class ProtectedInstructionToken:
    """Immutable protected-file classification carried through one write."""

    requested_path: str
    candidate_path: str | None
    protected: bool
    reason: str | None

    def __post_init__(self) -> None:
        if self.protected != bool(self.reason):
            raise ValueError("protected instruction token classification is inconsistent")

    def bind(self, candidate_path: str) -> "ProtectedInstructionToken":
        """Return the same classification bound to the admitted lexical path."""
        if not self.protected or not candidate_path:
            raise ValueError("cannot bind an unprotected instruction token")
        return ProtectedInstructionToken(
            requested_path=self.requested_path,
            candidate_path=candidate_path,
            protected=True,
            reason=self.reason,
        )


@dataclass
class ProtectedLexicalCandidate:
    """Preimage retained from one lexical parent and its final entry."""

    path: str
    parent_path: str
    final_name: str
    parent_fd: int
    parent_identity: dict
    final_identity: dict
    link_text: str | None
    target_path: str | None
    target_identity: dict | None
    target_sha256: str | None
    candidate_mode: int | None
    initial_kind: str = "symlink"
    original_bytes: bytes = b""
    original_sha256: str = ""
    original_mode: int | None = None
    candidate_bytes: bytes = b""
    candidate_sha256: str = ""
    protection_token: ProtectedInstructionToken | None = None
    staged_identity: dict | None = None
    committed_identity: dict | None = None
    final_fd: int = -1
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
        if self.final_fd >= 0:
            fd, self.final_fd = self.final_fd, -1
            os.close(fd)
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


def _same_inode(expected: dict, actual: dict) -> bool:
    """Compare the inode identity while allowing a bound file's mode to change."""
    return all(expected.get(key) == actual.get(key) for key in ("st_dev", "st_ino"))


def _renameat2_function():
    """Return libc's descriptor-relative atomic rename primitive, if available."""
    global _LIBC
    if _LIBC is False:
        return None
    if _LIBC is None:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            function = getattr(libc, "renameat2")
        except (AttributeError, OSError):
            _LIBC = False
            return None
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        _LIBC = libc
    return getattr(_LIBC, "renameat2", None)


def _renameat2(source: str, destination: str, parent_fd: int, flags: int) -> None:
    function = _renameat2_function()
    if function is None:
        raise ProtectedLexicalError("protected lexical replacement requires renameat2 atomic rename support")
    result = function(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _entry_matches(
    candidate: ProtectedLexicalCandidate,
    name: str,
    expected: dict,
    *,
    expected_sha256: str | None = None,
    expected_link_text: str | None = None,
) -> bool:
    """Compare one descriptor-relative entry with a bound preimage snapshot."""
    try:
        observed = os.stat(name, dir_fd=candidate.parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    observed_identity = {"kind": "regular" if stat.S_ISREG(observed.st_mode) else "symlink" if stat.S_ISLNK(observed.st_mode) else "other", **_identity(observed)}
    if observed_identity.get("kind") != expected.get("kind"):
        return False
    if expected.get("kind") == "regular":
        if not _same_identity(expected, observed_identity):
            return False
        if expected_sha256 is None:
            return True
        try:
            data, _ = _read_final_regular(candidate.parent_fd, name, expected_identity=expected)
        except (OSError, ProtectedLexicalError):
            return False
        return hashlib.sha256(data).hexdigest() == expected_sha256
    if expected.get("kind") == "symlink":
        link_text = os.readlink(name, dir_fd=candidate.parent_fd)
        if expected_link_text is not None and link_text != expected_link_text:
            return False
        return _same_identity(expected, observed_identity)
    return False


def _entry_expected_from_candidate(candidate: ProtectedLexicalCandidate) -> dict:
    if candidate.final_fd < 0:
        return candidate.final_identity
    return {
        "kind": candidate.final_identity.get("kind"),
        **_identity(os.fstat(candidate.final_fd)),
    }


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


def _read_final_regular(
    parent_fd: int,
    final_name: str,
    expected_identity: dict | None = None,
) -> tuple[bytes, dict]:
    """Read a lexical regular entry through its retained parent descriptor."""
    fd = os.open(final_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ProtectedLexicalError("protected final entry is not a regular file")
        identity = _identity(file_stat)
        if expected_identity is not None and not _same_identity(expected_identity, identity):
            raise ProtectedLexicalError("protected final regular file changed after approval")
        return _read_fd(fd), identity
    finally:
        os.close(fd)


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


def _rename(source: str, destination: str, parent_fd: int) -> None:
    """Atomically exchange two descriptor-relative lexical entries."""
    _renameat2(source, destination, parent_fd, _RENAME_EXCHANGE)


def _rename_noreplace(source: str, destination: str, parent_fd: int) -> None:
    """Move an entry only when the destination is still absent."""
    _renameat2(source, destination, parent_fd, _RENAME_NOREPLACE)


def prepare_candidate(
    path: str,
    cwd: str | None,
    admission: BackendAdmission | None = None,
) -> ProtectedLexicalCandidate | None:
    """Capture the parent and no-follow state of every protected final entry."""
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
    final_fd = -1
    try:
        try:
            parent_fd = _open_directory(parent_path)
        except FileNotFoundError:
            raise ProtectedLexicalError("protected destination parent is missing") from None
        parent_identity = _identity(os.fstat(parent_fd))
        try:
            final_stat = os.stat(final_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return ProtectedLexicalCandidate(
                path=absolute,
                parent_path=parent_path,
                final_name=final_name,
                parent_fd=parent_fd,
                parent_identity=parent_identity,
                final_identity={"kind": "absent"},
                link_text=None,
                target_path=None,
                target_identity=None,
                target_sha256=None,
                candidate_mode=None,
                initial_kind="absent",
            )
        final_fd = os.open(final_name, _O_PATH | os.O_NOFOLLOW, dir_fd=parent_fd)
        bound_identity = _identity(os.fstat(final_fd))
        if not _same_identity(bound_identity, _identity(final_stat)):
            raise ProtectedLexicalError("protected final entry changed while binding its descriptor")
        if not stat.S_ISLNK(final_stat.st_mode):
            if stat.S_ISREG(final_stat.st_mode):
                original_bytes, final_identity = _read_final_regular(
                    parent_fd,
                    final_name,
                    expected_identity=_identity(final_stat),
                )
                return ProtectedLexicalCandidate(
                    path=absolute,
                    parent_path=parent_path,
                    final_name=final_name,
                    parent_fd=parent_fd,
                    parent_identity=parent_identity,
                    final_identity={"kind": "regular", **final_identity},
                    link_text=None,
                    target_path=None,
                    target_identity=None,
                    target_sha256=None,
                    candidate_mode=stat.S_IMODE(final_stat.st_mode),
                    initial_kind="regular",
                    original_bytes=original_bytes,
                    original_sha256=hashlib.sha256(original_bytes).hexdigest(),
                    original_mode=stat.S_IMODE(final_identity["st_mode"]),
                    final_fd=final_fd,
                )
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
            initial_kind="symlink",
            final_fd=final_fd,
        )
    except Exception:
        if final_fd >= 0:
            os.close(final_fd)
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


def _revalidate(
    candidate: ProtectedLexicalCandidate,
) -> bytes | None:
    """Compare the current lexical entry with the frozen authorization snapshot."""
    _check_parent(candidate)
    try:
        final_stat = os.stat(candidate.final_name, dir_fd=candidate.parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if candidate.initial_kind == "absent":
            return None
        raise ProtectedLexicalError("protected final entry changed after approval") from None
    if candidate.initial_kind == "absent":
        raise ProtectedLexicalError("protected final entry changed after approval")
    if candidate.initial_kind == "regular":
        if not stat.S_ISREG(final_stat.st_mode) or not _same_identity(
            candidate.final_identity,
            {"kind": "regular", **_identity(final_stat)},
        ):
            raise ProtectedLexicalError("protected final regular file changed after approval")
        current_bytes, current_identity = _read_final_regular(
            candidate.parent_fd,
            candidate.final_name,
            expected_identity=candidate.final_identity,
        )
        current_sha256 = hashlib.sha256(current_bytes).hexdigest()
        retained_sha256 = candidate.original_sha256 or hashlib.sha256(candidate.original_bytes).hexdigest()
        if current_sha256 != retained_sha256:
            raise ProtectedLexicalError("protected final regular file content changed after approval")
        return current_bytes
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
    if candidate.target_identity is None or not _same_identity(candidate.target_identity, target_identity):
        raise ProtectedLexicalError("protected external target identity changed after approval")
    if candidate.target_sha256 is None or hashlib.sha256(target_bytes).hexdigest() != candidate.target_sha256:
        raise ProtectedLexicalError("protected external target changed after approval")
    return None


def read_candidate(
    candidate: ProtectedLexicalCandidate,
) -> bytes:
    """Read an admitted protected entry without following its final leaf."""
    if candidate.initial_kind == "regular":
        data = _revalidate(candidate)
        if data is None:  # pragma: no cover - regular candidates always return bytes
            raise ProtectedLexicalError("protected regular candidate has no content")
        return data
    _revalidate(candidate)
    if candidate.initial_kind == "absent":
        raise FileNotFoundError(candidate.path)
    if (
        candidate.target_path is None
        or candidate.target_identity is None
        or candidate.target_sha256 is None
    ):
        raise ProtectedLexicalError("protected symlink token is incomplete")
    target_bytes, target_identity = _read_regular(candidate.target_path)
    if not _same_identity(candidate.target_identity, target_identity):
        raise ProtectedLexicalError("protected external target identity changed after approval")
    if hashlib.sha256(target_bytes).hexdigest() != candidate.target_sha256:
        raise ProtectedLexicalError("protected external target changed after approval")
    return target_bytes


def _write_temp(candidate: ProtectedLexicalCandidate) -> str:
    name = _random_name(candidate.parent_fd)
    fd = -1
    created = False
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600 if candidate.candidate_mode is not None else 0o666,
            dir_fd=candidate.parent_fd,
        )
        # Bind ownership immediately after O_EXCL succeeds.  All later cleanup
        # must prove this exact inode still owns the generated name.
        created = True
        temp_identity = _identity(os.fstat(fd))
        if not stat.S_ISREG(temp_identity["st_mode"]):
            raise ProtectedLexicalError("protected temporary is not regular")
        candidate.staged_identity = {"kind": "regular", **temp_identity}
        view = memoryview(candidate.candidate_bytes)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ProtectedLexicalError("protected temporary write made no progress")
            view = view[written:]
        if candidate.candidate_mode is not None:
            os.fchmod(fd, candidate.candidate_mode & 0o7777)
        os.fsync(fd)
        written_identity = _identity(os.fstat(fd))
        if not _same_inode(candidate.staged_identity, written_identity):
            raise ProtectedLexicalError("protected temporary identity changed while staging")
        # fchmod above may legitimately change st_mode after the immediate
        # ownership binding; retain the same inode with its final mode.
        candidate.staged_identity = {"kind": "regular", **written_identity}
        return name
    except Exception as exc:
        if fd >= 0:
            os.close(fd)
            fd = -1
        if not created:
            # O_EXCL failed: Hermes never owned this pathname and must not
            # unlink it, even if another actor installed an object there.
            raise
        cleanup_error = _cleanup_temp(candidate, name)
        if cleanup_error:
            raise ProtectedLexicalError(
                f"UNCERTAIN STATE: protected temporary write failed and cleanup failed: {cleanup_error}"
            ) from exc
        raise
    finally:
        if fd >= 0:
            os.close(fd)


def _capture_committed_state(candidate: ProtectedLexicalCandidate) -> None:
    """Bind the observed committed object without changing the authorization snapshot."""
    if not _observe_staged_final(candidate):
        raise ProtectedLexicalError("protected committed entry is not the staged object")


def _observe_staged_final(candidate: ProtectedLexicalCandidate) -> bool:
    """Return whether the lexical leaf is the exact staged object.

    The initial missing/non-regular observation is a normal non-commit result;
    errors after an object has been observed are allowed to escape so callers
    can report an uncertain outcome rather than guessing that no rename landed.
    """
    if candidate.staged_identity is None:
        raise ProtectedLexicalError("protected staged regular identity is unavailable")
    try:
        final_stat = os.stat(candidate.final_name, dir_fd=candidate.parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(final_stat.st_mode):
        return False
    final_identity = _identity(final_stat)
    if not _same_identity(candidate.staged_identity, final_identity):
        return False
    committed_bytes, committed_identity = _read_final_regular(
            candidate.parent_fd,
            candidate.final_name,
            expected_identity=candidate.staged_identity,
        )
    if (
        candidate.candidate_mode is not None
        and stat.S_IMODE(committed_identity["st_mode"]) != candidate.candidate_mode
    ):
        return False
    if hashlib.sha256(committed_bytes).hexdigest() != candidate.candidate_sha256:
        return False
    candidate.committed_identity = {"kind": "regular", **committed_identity}
    return True


def _final_is_candidate(candidate: ProtectedLexicalCandidate) -> bool:
    """Return true only for the exact staged object, when observation succeeds."""
    try:
        return _observe_staged_final(candidate)
    except (OSError, ProtectedLexicalError):
        return False


def _cleanup_temp(
    candidate: ProtectedLexicalCandidate,
    name: str | None,
    *,
    expected_identity: dict | None = None,
    expected_sha256: str | None = None,
    expected_link_text: str | None = None,
) -> str | None:
    """Remove only an entry atomically bound to the expected object.

    The generated name is first moved with RENAME_NOREPLACE into a fresh
    quarantine name.  A raced replacement is therefore moved out of the
    original name and remains available for inspection; it is never passed to
    unlink.  Identity uncertainty leaves the quarantined object in place.
    """
    if not name:
        return None
    expected = expected_identity or candidate.staged_identity
    if expected is None:
        return "protected temporary identity is not bound; leaving it in place"
    quarantine = _random_name(candidate.parent_fd)
    try:
        _rename_noreplace(name, quarantine, candidate.parent_fd)
    except FileNotFoundError:
        return "protected temporary object could not be atomically bound; leaving it in place"
    except Exception as exc:
        return f"protected temporary object could not be atomically bound: {exc}"
    try:
        if not _entry_matches(
            candidate,
            quarantine,
            expected,
            expected_sha256=expected_sha256,
            expected_link_text=expected_link_text,
        ):
            return "protected temporary object changed; leaving it in quarantine"
    except Exception as exc:
        return f"protected temporary object could not be verified: {exc}"
    try:
        os.unlink(quarantine, dir_fd=candidate.parent_fd)
    except Exception as exc:
        return str(exc)
    return None


def _protected_failure(
    candidate: ProtectedLexicalCandidate,
    error: str,
    *,
    outcome: str,
    possibly_modified: bool,
) -> WriteResult:
    return WriteResult(
        error=error,
        verified=False,
        files_modified=[candidate.path] if possibly_modified else [],
        outcome=outcome,
    )


def _approved_preimage_matches(candidate: ProtectedLexicalCandidate, name: str) -> bool:
    expected = _entry_expected_from_candidate(candidate)
    expected_sha256 = candidate.original_sha256 if candidate.initial_kind == "regular" else None
    if not _entry_matches(
        candidate,
        name,
        expected,
        expected_sha256=expected_sha256,
        expected_link_text=candidate.link_text,
    ):
        return False
    if candidate.initial_kind != "symlink":
        return True
    if candidate.target_path is None or candidate.target_identity is None or candidate.target_sha256 is None:
        return False
    try:
        target_bytes, target_identity = _read_regular(candidate.target_path)
    except (OSError, ProtectedLexicalError):
        return False
    return (
        _same_identity(candidate.target_identity, target_identity)
        and hashlib.sha256(target_bytes).hexdigest() == candidate.target_sha256
    )


def _rollback_exchange(candidate: ProtectedLexicalCandidate, temp_name: str) -> WriteResult:
    """Exchange a raced preimage back without deleting either object."""
    try:
        _rename(temp_name, candidate.final_name, candidate.parent_fd)
    except Exception as rollback_error:
        return _protected_failure(
            candidate,
            "UNCERTAIN STATE (possibly modified): protected preimage drifted and "
            f"rollback could not be completed: {rollback_error}",
            outcome="uncertain",
            possibly_modified=True,
        )
    if not _approved_preimage_matches(candidate, candidate.final_name):
        restored = False
    else:
        restored = True
    if not _entry_matches(candidate, temp_name, candidate.staged_identity or {}):
        return _protected_failure(
            candidate,
            "UNCERTAIN STATE (possibly modified): protected preimage drifted and "
            "rollback left an unproven staged object",
            outcome="uncertain",
            possibly_modified=True,
        )
    cleanup_error = _cleanup_temp(candidate, temp_name)
    if cleanup_error:
        return _protected_failure(
            candidate,
            "UNCERTAIN STATE: protected preimage drifted and rollback completed, but "
            f"the staged object could not be cleaned up: {cleanup_error}",
            outcome="uncertain",
            possibly_modified=True,
        )
    if not restored:
        return _protected_failure(
            candidate,
            "UNCERTAIN STATE (possibly modified): protected preimage drifted and the "
            "raced replacement was preserved, but the approved lexical entry could "
            "not be proven restored",
            outcome="uncertain",
            possibly_modified=True,
        )
    return _protected_failure(
        candidate,
        "NOT COMMITTED: protected final entry changed before the atomic replacement; "
        "the replacement was preserved and the approved entry was restored",
        outcome="not_committed",
        possibly_modified=False,
    )


def _classify_rename_failure(
    candidate: ProtectedLexicalCandidate,
    temp_name: str,
    rename_error: Exception,
) -> WriteResult:
    """Classify an atomic rename exception without guessing its outcome."""
    try:
        if _observe_staged_final(candidate):
            cleanup_error = None
            if candidate.initial_kind != "absent":
                cleanup_error = _cleanup_temp(
                    candidate,
                    temp_name,
                    expected_identity=candidate.final_identity,
                    expected_sha256=candidate.original_sha256 if candidate.initial_kind == "regular" else None,
                    expected_link_text=candidate.link_text,
                )
            if cleanup_error:
                return _protected_failure(
                    candidate,
                    "UNCERTAIN STATE (possibly modified): protected atomic rename "
                    f"installed the staged object, but the displaced preimage could "
                    f"not be cleaned up: {cleanup_error}; rename error: {rename_error}",
                    outcome="uncertain",
                    possibly_modified=True,
                )
            return _protected_failure(
                candidate,
                "COMMITTED (possibly modified): protected atomic rename raised after "
                "the staged object was installed; the protected lexical entry may be "
                f"modified: {rename_error}",
                outcome="committed",
                possibly_modified=True,
            )
        if candidate.initial_kind == "absent" and isinstance(rename_error, FileExistsError):
            observation_error = None
        else:
            try:
                _revalidate(candidate)
                observation_error = None
            except Exception as error:
                observation_error = error
        if observation_error is not None:
            return _protected_failure(
                candidate,
                "UNCERTAIN STATE (possibly modified): protected atomic rename raised "
                "and the final state could not be proven; the protected lexical entry "
                f"may be modified: {observation_error}; rename error: {rename_error}",
                outcome="uncertain",
                possibly_modified=True,
            )
    except Exception as observation_error:
        return _protected_failure(
            candidate,
            "UNCERTAIN STATE (possibly modified): protected atomic rename raised and "
            f"the final state could not be proven: {observation_error}; rename error: {rename_error}",
            outcome="uncertain",
            possibly_modified=True,
        )
    cleanup_error = _cleanup_temp(candidate, temp_name)
    if cleanup_error:
        return _protected_failure(
            candidate,
            "UNCERTAIN STATE (possibly modified): protected atomic rename did not "
            "install the staged object, but its bound temporary object could not be "
            f"cleaned up: {cleanup_error}; rename error: {rename_error}",
            outcome="uncertain",
            possibly_modified=True,
        )
    return _protected_failure(
        candidate,
        "NOT COMMITTED: protected atomic rename failed, the frozen lexical entry was "
        f"unchanged, and the bound temporary object was cleaned up: {rename_error}",
        outcome="not_committed",
        possibly_modified=False,
    )


def commit_candidate(candidate: ProtectedLexicalCandidate) -> WriteResult:
    """Replace the final entry and report only a proven or explicitly uncertain outcome."""
    temp_name: str | None = None
    commit_attempted = False
    try:
        if not candidate.candidate_sha256:
            raise ProtectedLexicalError("protected candidate content was not prepared")
        _revalidate(candidate)
        temp_name = _write_temp(candidate)
        _revalidate(candidate)
        commit_attempted = True
        try:
            if candidate.initial_kind == "absent":
                _rename_noreplace(temp_name, candidate.final_name, candidate.parent_fd)
            else:
                _rename(temp_name, candidate.final_name, candidate.parent_fd)
        except Exception as rename_error:
            return _classify_rename_failure(candidate, temp_name, rename_error)

        if candidate.initial_kind != "absent":
            if not _approved_preimage_matches(candidate, temp_name):
                return _rollback_exchange(candidate, temp_name)
            cleanup_error = _cleanup_temp(
                candidate,
                temp_name,
                expected_identity=candidate.final_identity,
                expected_sha256=candidate.original_sha256 if candidate.initial_kind == "regular" else None,
                expected_link_text=candidate.link_text,
            )
            if cleanup_error:
                return _protected_failure(
                    candidate,
                    "UNCERTAIN STATE (possibly modified): protected atomic replacement "
                    "installed the candidate, but the approved displaced object could "
                    f"not be cleaned up: {cleanup_error}",
                    outcome="uncertain",
                    possibly_modified=True,
                )
        temp_name = None

        try:
            _capture_committed_state(candidate)
        except Exception as commit_state_error:
            return _protected_failure(
                candidate,
                "UNCERTAIN STATE (possibly modified): protected rename committed, "
                "but the staged object could not be observed reliably: "
                f"{commit_state_error}",
                outcome="uncertain",
                possibly_modified=True,
            )

        try:
            os.fsync(candidate.parent_fd)
        except Exception as fsync_error:
            return _protected_failure(
                candidate,
                "UNCERTAIN STATE (possibly modified): protected rename committed, "
                "but parent durability could not be proven: "
                f"{fsync_error}",
                outcome="uncertain",
                possibly_modified=True,
            )

        try:
            _check_parent(candidate)
            final_stat = os.stat(candidate.final_name, dir_fd=candidate.parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(final_stat.st_mode):
                raise ProtectedLexicalError("destination is not a regular non-symlink")
            if (
                candidate.committed_identity is None
                or not _same_identity(
                    candidate.committed_identity,
                    {"kind": "regular", **_identity(final_stat)},
                )
            ):
                raise ProtectedLexicalError("destination changed before postcondition verification")
            fd = os.open(candidate.final_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=candidate.parent_fd)
            try:
                fd_identity = _identity(os.fstat(fd))
                if not _same_identity(candidate.committed_identity, fd_identity):
                    raise ProtectedLexicalError("destination changed before postcondition verification")
                final_hash = hashlib.sha256(_read_fd(fd)).hexdigest()
            finally:
                os.close(fd)
            if final_hash != candidate.candidate_sha256:
                raise ProtectedLexicalError("destination hash differs from requested bytes")
            if candidate.initial_kind == "symlink":
                if candidate.target_path is None or candidate.target_identity is None or candidate.target_sha256 is None:
                    raise ProtectedLexicalError("protected symlink token is incomplete")
                target_bytes, target_identity = _read_regular(candidate.target_path)
                if not _same_identity(candidate.target_identity, target_identity):
                    raise ProtectedLexicalError("external target identity changed")
                if hashlib.sha256(target_bytes).hexdigest() != candidate.target_sha256:
                    raise ProtectedLexicalError("external target changed")
        except Exception as post_error:
            return _protected_failure(
                candidate,
                "UNCERTAIN STATE (possibly modified): protected postcondition could "
                f"not be proven after commit: {post_error}",
                outcome="uncertain",
                possibly_modified=True,
            )
        return WriteResult(bytes_written=len(candidate.candidate_bytes), verified=True)
    except Exception as exc:
        if commit_attempted:
            return _protected_failure(
                candidate,
                "UNCERTAIN STATE (possibly modified): protected commit was attempted "
                f"but its final outcome could not be observed: {exc}",
                outcome="uncertain",
                possibly_modified=True,
            )
        cleanup_error = _cleanup_temp(candidate, temp_name)
        if cleanup_error:
            return _protected_failure(
                candidate,
                "UNCERTAIN STATE (possibly modified): protected replacement failed "
                f"before commit and temporary cleanup failed: {cleanup_error}",
                outcome="uncertain",
                possibly_modified=True,
            )
        if "UNCERTAIN STATE" in str(exc):
            return _protected_failure(
                candidate,
                str(exc),
                outcome="uncertain",
                possibly_modified=False,
            )
        return _protected_failure(
            candidate,
            f"NOT COMMITTED: protected lexical replacement failed before commit: {exc}",
            outcome="not_committed",
            possibly_modified=False,
        )
    finally:
        candidate.close()

"""Small, import-safe primitives for lexical protected-write admission.

This module deliberately has no filesystem inspection.  A target is a lexical
identity, not a resolved target, and backend admission is based only on the
execution environment's authoritative ``is_local`` value plus its explicit
local capability contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


_LOCAL_POSIX_CAPABILITIES: tuple[str, ...] = ("cwd", "execute")

__all__ = ["BackendAdmission", "BackendKind", "LexicalWriteTarget"]

_IDENTITY_UNAVAILABLE_REASON = "backend identity is unavailable or ambiguous"
_NOT_LOCAL_REASON = "backend is not an exact local boolean"
_PLATFORM_UNSUPPORTED_REASON = "local POSIX backend is unavailable on this platform"
_CAPABILITIES_INCOMPLETE_REASON = "local POSIX capability contract is incomplete"
_ADMITTED_REASON = "local POSIX backend admitted"


@dataclass(frozen=True, slots=True)
class LexicalWriteTarget:
    """Immutable identity of one lexical directory entry.

    ``parent_dir`` and ``final_component`` are normalized text only.  No
    constructor or property resolves symlinks, reads links, or consults the
    filesystem.
    """

    parent_dir: str
    final_component: str

    def __post_init__(self) -> None:
        if type(self.parent_dir) is not str or type(self.final_component) is not str:
            raise TypeError("LexicalWriteTarget fields must be strings")
        if "\x00" in self.parent_dir or "\x00" in self.final_component:
            raise ValueError("LexicalWriteTarget cannot contain NUL")
        normalized_parent = os.path.normpath(self.parent_dir)
        if not os.path.isabs(normalized_parent) or normalized_parent != self.parent_dir:
            raise ValueError("LexicalWriteTarget parent_dir must be normalized and absolute")
        if (
            not self.final_component
            or self.final_component in {".", ".."}
            or os.path.basename(self.final_component) != self.final_component
            or os.path.dirname(self.final_component)
        ):
            raise ValueError("LexicalWriteTarget final_component must be one non-dot leaf")

    @classmethod
    def from_path(cls, lexical_path: str, *, cwd: str) -> "LexicalWriteTarget":
        """Normalize a caller-selected path without dereferencing symlinks.

        Relative paths are anchored to the explicit absolute ``cwd``.  The cwd
        itself is never inferred from the process and neither path is resolved
        against the host filesystem.
        """

        if type(lexical_path) is not str:
            raise TypeError("lexical_path must be a string")
        if type(cwd) is not str:
            raise TypeError("cwd must be a string")
        if not cwd or "\x00" in cwd or "\x00" in lexical_path:
            raise ValueError("lexical path and cwd must be non-empty and NUL-free")

        # A caller selecting the cwd itself (or its parent) has not supplied a
        # file entry.  Reject these before normpath would erase the distinction
        # between a real leaf and a dot segment.
        if not lexical_path or lexical_path in {".", ".."} or lexical_path.endswith(("/.", "/..")):
            raise ValueError("lexical path must name one non-dot leaf")

        normalized_cwd = os.path.normpath(cwd)
        if not os.path.isabs(normalized_cwd):
            raise ValueError("cwd must be an absolute path")

        joined = lexical_path if os.path.isabs(lexical_path) else os.path.join(
            normalized_cwd, lexical_path
        )
        normalized = os.path.normpath(joined)
        if not os.path.isabs(normalized):
            raise ValueError("lexical path did not produce an absolute target")

        parent_dir, final_component = os.path.split(normalized)
        if not parent_dir or not final_component:
            raise ValueError("lexical path must contain one non-empty leaf")
        return cls(
            parent_dir=os.path.normpath(parent_dir),
            final_component=final_component,
        )

    @property
    def lexical_path(self) -> str:
        """Return the normalized lexical path represented by this identity."""

        return os.path.join(self.parent_dir, self.final_component)

    @property
    def identity_key(self) -> tuple[str, str]:
        """Return the exact key used by lexical bookkeeping."""

        return (self.parent_dir, self.final_component)


class BackendKind(Enum):
    """Backend classes relevant to the protected lexical-write boundary."""

    LOCAL_POSIX = "local_posix"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class BackendAdmission:
    """Structured, fail-closed result of backend identity admission."""

    kind: BackendKind
    reason: str

    @classmethod
    def identify(cls, environment: object) -> "BackendAdmission":
        """Identify an environment without inspecting host paths.

        ``environment.is_local`` is the sole authority for local-vs-remote
        identity.  Capabilities are checked only after an exact ``True`` and
        only by attribute presence/type; no capability method is invoked.
        Invoking an environment would be an execution side effect, not an
        admission check.
        """

        try:
            is_local = getattr(environment, "is_local")
        except Exception:
            return cls(BackendKind.UNSUPPORTED, _IDENTITY_UNAVAILABLE_REASON)

        if type(is_local) is not bool:
            return cls(BackendKind.UNSUPPORTED, _IDENTITY_UNAVAILABLE_REASON)
        if not is_local:
            return cls(BackendKind.UNSUPPORTED, _NOT_LOCAL_REASON)
        if os.name != "posix":
            return cls(BackendKind.UNSUPPORTED, _PLATFORM_UNSUPPORTED_REASON)

        for capability_name in _LOCAL_POSIX_CAPABILITIES:
            try:
                capability = getattr(environment, capability_name)
            except Exception:
                return cls(BackendKind.UNSUPPORTED, _CAPABILITIES_INCOMPLETE_REASON)
            if capability_name == "cwd":
                if (
                    type(capability) is not str
                    or not capability
                    or "\x00" in capability
                ):
                    return cls(BackendKind.UNSUPPORTED, _CAPABILITIES_INCOMPLETE_REASON)
            elif not callable(capability):
                return cls(BackendKind.UNSUPPORTED, _CAPABILITIES_INCOMPLETE_REASON)

        return cls(BackendKind.LOCAL_POSIX, _ADMITTED_REASON)

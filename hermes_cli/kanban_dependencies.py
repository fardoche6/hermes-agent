"""Plugin-safe, typed Kanban dependency-provider contracts.

This module deliberately has no SQLite or plugin-manager imports.  Plugins
register a bounded callback here; :mod:`hermes_cli.kanban_db` supplies only
frozen, board-explicit task/link context when resolving a dependency.

A provider is advisory until it returns ``satisfied`` with an immutable
``generation``.  Every other result, including provider absence, exceptions,
timeouts, and malformed output, is normalized to fail-closed ``unknown``.
"""

from __future__ import annotations

import json
import io
import logging
import math
import os
import pathlib
import re
import selectors
import secrets
import subprocess
import sys
import threading
import time
import dataclasses
import itertools
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from agent.redact import redact_sensitive_text

_log = logging.getLogger(__name__)

DEPENDENCY_RESULT_STATUSES = frozenset({"satisfied", "unsatisfied", "unknown"})
MAX_METADATA_BYTES = 4096
MAX_METADATA_DEPTH = 4
MAX_METADATA_ITEMS = 32
MAX_METADATA_STRING = 1024
MAX_NAME_LENGTH = 96
MAX_GENERATION_LENGTH = 256
MAX_PROVIDER_TIMEOUT_SECONDS = 30.0
MAX_BYTES = 64 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|authorization|cookie|credential)",
    re.IGNORECASE,
)


def _validate_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise ValueError(
            f"{label} must match [A-Za-z0-9][A-Za-z0-9_.:-]{{0,95}}"
        )
    return value


def _validate_object_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _OBJECT_ID_RE.fullmatch(value):
        raise ValueError(f"{label} must be a full 40- or 64-character object id")
    return value.lower()


def _validate_board(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("dependency context board must be a string")
    board = value.strip().lower()
    if not _BOARD_RE.fullmatch(board):
        raise ValueError("dependency context board must be a canonical board slug")
    return board


def _safe_value(value: Any, *, key: Optional[str], depth: int) -> Any:
    if depth > MAX_METADATA_DEPTH:
        raise ValueError("metadata nesting is too deep")
    if key and _SENSITIVE_KEY_RE.search(key):
        return "<redacted>"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("metadata numbers must be finite")
        return value
    if isinstance(value, str):
        if len(value) > MAX_METADATA_STRING:
            raise ValueError("metadata strings are too long")
        return redact_sensitive_text(
            value,
            force=True,
            redact_url_credentials=True,
        )
    if isinstance(value, Mapping):
        if len(value) > MAX_METADATA_ITEMS:
            raise ValueError("metadata objects have too many entries")
        out: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 96:
                raise ValueError("metadata keys must be non-empty short strings")
            out[raw_key] = _safe_value(raw_value, key=raw_key, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_METADATA_ITEMS:
            raise ValueError("metadata arrays have too many entries")
        return [
            _safe_value(item, key=None, depth=depth + 1)
            for item in value
        ]
    raise ValueError(f"metadata contains unsupported value type {type(value).__name__}")


def validate_dependency_metadata(
    value: Optional[Mapping[str, Any]],
    *,
    field_name: str = "metadata",
) -> dict[str, Any]:
    """Validate, redact, and bound opaque provider metadata.

    The returned object is JSON-compatible and safe to persist.  It is not a
    provider-specific schema; the owning provider may interpret its keys.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    try:
        safe = _safe_value(value, key=None, depth=0)
        encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}: {exc}") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError(f"{field_name} exceeds {MAX_METADATA_BYTES} bytes")
    return safe


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, tuple):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class KanbanWorkspaceBasePin:
    """Exact immutable git objects a dispatched worktree must use."""

    head: str
    tree: str
    receipt_generation: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "head", _validate_object_id(self.head, "workspace head"))
        object.__setattr__(self, "tree", _validate_object_id(self.tree, "workspace tree"))
        if (
            not isinstance(self.receipt_generation, str)
            or not self.receipt_generation.strip()
            or len(self.receipt_generation) > MAX_GENERATION_LENGTH
            or any(ord(ch) < 0x20 for ch in self.receipt_generation)
        ):
            raise ValueError("workspace receipt_generation must be a bounded non-empty string")

    def as_dict(self) -> dict[str, str]:
        return {
            "head": self.head,
            "tree": self.tree,
            "receipt_generation": self.receipt_generation,
        }


@dataclass(frozen=True)
class KanbanDependencyContext:
    """Frozen, board-explicit context passed to one provider invocation."""

    board: str
    task: Mapping[str, Any]
    link: Mapping[str, Any]
    parent: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "board", _validate_board(self.board))
        for field_name in ("task", "link", "parent"):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise ValueError(f"dependency context {field_name} must be an object")
            safe = validate_dependency_metadata(value, field_name=f"context.{field_name}")
            object.__setattr__(self, field_name, _freeze(safe))


@dataclass(frozen=True)
class KanbanDependencyResult:
    """Stable provider result contract.

    ``satisfied`` requires an immutable provider ``generation``.  The
    generation is persisted with the admitted run; it is not re-used from a
    previous readiness sweep.
    """

    status: str
    generation: Optional[str] = None
    workspace_base: Optional[KanbanWorkspaceBasePin] = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in DEPENDENCY_RESULT_STATUSES:
            raise ValueError(f"invalid dependency result status: {self.status!r}")
        if self.generation is not None:
            if (
                not isinstance(self.generation, str)
                or not self.generation.strip()
                or len(self.generation) > MAX_GENERATION_LENGTH
                or any(ord(ch) < 0x20 for ch in self.generation)
            ):
                raise ValueError("dependency generation must be a bounded non-empty string")
            object.__setattr__(self, "generation", self.generation.strip())
        if self.status == "satisfied" and not self.generation:
            raise ValueError("satisfied dependency results require generation")
        if self.status != "satisfied" and (self.generation or self.workspace_base):
            raise ValueError("only satisfied results may carry generation or workspace_base")
        if self.workspace_base is not None and not isinstance(
            self.workspace_base, KanbanWorkspaceBasePin
        ):
            raise ValueError("workspace_base must be KanbanWorkspaceBasePin")
        diagnostics = validate_dependency_metadata(
            self.diagnostics,
            field_name="diagnostics",
        )
        object.__setattr__(self, "diagnostics", _freeze(diagnostics))

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.generation is not None:
            out["generation"] = self.generation
        if self.workspace_base is not None:
            out["workspace_base"] = self.workspace_base.as_dict()
        if self.diagnostics:
            out["diagnostics"] = dict(self.diagnostics)
        return out


def normalize_dependency_result(value: Any) -> KanbanDependencyResult:
    """Normalize one provider return value or raise for malformed output."""
    if isinstance(value, KanbanDependencyResult):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("provider result must be KanbanDependencyResult or an object")
    allowed = {"status", "generation", "workspace_base", "diagnostics"}
    if any(key not in allowed for key in value):
        raise ValueError("provider result contains unknown fields")
    raw_pin = value.get("workspace_base")
    pin: Optional[KanbanWorkspaceBasePin]
    if raw_pin is None:
        pin = None
    elif isinstance(raw_pin, KanbanWorkspaceBasePin):
        pin = raw_pin
    elif isinstance(raw_pin, Mapping):
        if set(raw_pin) != {"head", "tree", "receipt_generation"}:
            raise ValueError("workspace_base must contain exactly head, tree, receipt_generation")
        pin = KanbanWorkspaceBasePin(
            head=raw_pin["head"],
            tree=raw_pin["tree"],
            receipt_generation=raw_pin["receipt_generation"],
        )
    else:
        raise ValueError("workspace_base must be an object")
    status = value.get("status")
    if not isinstance(status, str):
        raise ValueError("provider result status must be a string")
    return KanbanDependencyResult(
        status=status,
        generation=value.get("generation"),
        workspace_base=pin,
        diagnostics=value.get("diagnostics") or {},
    )


@dataclass(frozen=True)
class _RegisteredProvider:
    kind: str
    provider_name: str
    callback: Callable[[KanbanDependencyContext], Any]
    timeout_seconds: float
    owner: str
    module: str
    qualname: str
    pythonpath: tuple[str, ...]
    module_file: str


_PROVIDER_LOCK = threading.RLock()
_PROVIDERS: dict[tuple[str, str], _RegisteredProvider] = {}
_ACTIVE_INVOCATIONS: dict[
    tuple[str, str],
    dict[int, tuple[subprocess.Popen[bytes], threading.Event, Optional[_ProcessContainment]]],
] = {}
_GLOBAL_ADMISSION = threading.BoundedSemaphore(8)
_PROVIDER_ADMISSION: dict[tuple[str, str], threading.BoundedSemaphore] = {}
_INVOCATION_IDS = itertools.count(1)


@dataclass
class _ProcessContainment:
    """A delegated cgroup-v2 boundary for one provider worker."""

    path: pathlib.Path
    relative_path: str
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def worker_argument(self) -> str:
        return str(self.path)

    def worker_is_contained(self, pid: int) -> bool:
        try:
            for line in pathlib.Path(f"/proc/{pid}/cgroup").read_text(
                encoding="utf-8"
            ).splitlines():
                hierarchy, separator, relative = line.partition("::")
                if separator and hierarchy == "0":
                    return relative == self.relative_path
        except (OSError, UnicodeError):
            return False
        return False

    def _populated(self) -> Optional[bool]:
        try:
            for line in (self.path / "cgroup.events").read_text(
                encoding="utf-8"
            ).splitlines():
                key, _, value = line.partition(" ")
                if key == "populated":
                    return value.strip() == "1"
        except (OSError, UnicodeError, ValueError):
            return None
        return None

    def terminate(self, timeout_seconds: float = 2.0) -> bool:
        """Kill the complete boundary and prove that it became empty."""
        with self._lock:
            if self._closed:
                return True
            if not self.path.exists():
                self._closed = True
                return True
            kill_file = self.path / "cgroup.kill"
            try:
                kill_file.write_text("1")
            except OSError:
                return False
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                populated = self._populated()
                if populated is False:
                    try:
                        self.path.rmdir()
                    except OSError:
                        if self.path.exists():
                            time.sleep(0.01)
                            continue
                    self._closed = not self.path.exists()
                    return self._closed
                time.sleep(0.01)
            return False


def _containment_parent() -> Optional[tuple[pathlib.Path, str]]:
    """Return a writable cgroup-v2 parent with a kill controller."""
    if os.name != "posix" or not sys.platform.startswith("linux"):
        return None
    try:
        relative: Optional[str] = None
        for line in pathlib.Path("/proc/self/cgroup").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.startswith("0::"):
                relative = line[3:]
                break
        if relative is None:
            return None
        mount = pathlib.Path("/sys/fs/cgroup")
        parent = mount / relative.lstrip("/")
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            return None
        if not os.access(parent / "cgroup.procs", os.W_OK):
            return None
        if not os.access(parent / "cgroup.kill", os.W_OK):
            return None
    except (OSError, StopIteration, UnicodeError):
        return None
    return parent, relative.rstrip("/") or "/"


def _containment_available() -> bool:
    return _containment_parent() is not None


def _new_process_containment(invocation_id: int) -> Optional[_ProcessContainment]:
    parent_info = _containment_parent()
    if parent_info is None:
        return None
    parent, relative_parent = parent_info
    for _ in range(3):
        name = f"hermes-provider-{os.getpid()}-{invocation_id}-{secrets.token_hex(4)}"
        path = parent / name
        try:
            path.mkdir(mode=0o700)
            relative = (
                f"{relative_parent.rstrip('/')}/{name}"
                if relative_parent != "/"
                else f"/{name}"
            )
            if not (path / "cgroup.kill").is_file():
                path.rmdir()
                return None
            return _ProcessContainment(path=path, relative_path=relative)
        except OSError:
            try:
                path.rmdir()
            except OSError:
                pass
    return None


def register_kanban_dependency_provider(
    dependency_kind: str,
    provider_name: str,
    callback: Callable[[KanbanDependencyContext], Any],
    *,
    timeout_seconds: float = 2.0,
    owner: Optional[str] = None,
) -> None:
    """Register a generic provider under ``(dependency_kind, provider_name)``.

    The callback receives one :class:`KanbanDependencyContext` and must return
    a :class:`KanbanDependencyResult` (or its exact JSON-compatible shape).
    Registration is process-local and intentionally does not expose a DB.
    """
    kind = _validate_name(dependency_kind, "dependency_kind")
    name = _validate_name(provider_name, "provider_name")
    if not callable(callback):
        raise ValueError("dependency provider callback must be callable")
    module_name = getattr(callback, "__module__", None)
    qualname = getattr(callback, "__qualname__", None)
    if (
        not isinstance(module_name, str)
        or not isinstance(qualname, str)
        or "<locals>" in qualname
        or getattr(callback, "__self__", None) is not None
        or getattr(callback, "__name__", "").startswith("<")
    ):
        raise ValueError(
            "dependency provider callback must be an importable module-level callable"
        )
    module = sys.modules.get(module_name)
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise ValueError("dependency provider module must have an importable file")
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider timeout must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_PROVIDER_TIMEOUT_SECONDS:
        raise ValueError(
            f"provider timeout must be > 0 and <= {MAX_PROVIDER_TIMEOUT_SECONDS:g} seconds"
        )
    provider_owner = str(owner or "direct")
    key = (kind, name)
    with _PROVIDER_LOCK:
        if key in _PROVIDERS:
            existing = _PROVIDERS[key]
            if (
                existing.owner == provider_owner
                and existing.module == module_name
                and existing.qualname == qualname
                and existing.module_file == str(pathlib.Path(module_file).resolve())
                and existing.timeout_seconds == timeout
            ):
                return
            raise ValueError(
                f"dependency provider {kind}/{name} is already registered by {existing.owner!r}"
            )
        _PROVIDERS[key] = _RegisteredProvider(
            kind=kind,
            provider_name=name,
            callback=callback,
            timeout_seconds=timeout,
            owner=provider_owner,
            module=module_name,
            qualname=qualname,
            pythonpath=tuple(dict.fromkeys((
                str(pathlib.Path(module_file).resolve().parent),
                str(pathlib.Path(module_file).resolve().parents[1]),
            ))),
            module_file=str(pathlib.Path(module_file).resolve()),
        )
        _PROVIDER_ADMISSION.setdefault(key, threading.BoundedSemaphore(2))


def _stop_process(
    process: Any,
    boundary: Optional[_ProcessContainment] = None,
) -> bool:
    """Terminate and reap a provider boundary, failing closed if unproven."""
    if process is None:
        return boundary is None or boundary.terminate()
    boundary_ok = boundary is not None and boundary.terminate()
    try:
        if process.poll() is None:
            # The process is already admitted to a real boundary in normal
            # operation. Directly reap the worker if needed, but never use a
            # process-group kill as a substitute for missing/unproven
            # descendant containment.
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        else:
            process.wait(timeout=0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return boundary_ok


def _cancel_invocations(
    invocations: list[tuple[Any, threading.Event, Optional[_ProcessContainment]]],
) -> None:
    for process, cancelled, boundary in invocations:
        cancelled.set()
        _stop_process(process, boundary)


def _remove_provider_keys(
    keys: list[tuple[str, str]],
) -> list[tuple[Any, threading.Event, Optional[_ProcessContainment]]]:
    invocations: list[tuple[Any, threading.Event, Optional[_ProcessContainment]]] = []
    with _PROVIDER_LOCK:
        for key in keys:
            _PROVIDERS.pop(key, None)
            invocations.extend(_ACTIVE_INVOCATIONS.pop(key, {}).values())
    return invocations


def unregister_kanban_dependency_providers(owner: Optional[str] = None) -> int:
    """Remove providers owned by ``owner`` and cancel their invocations."""
    with _PROVIDER_LOCK:
        if owner is None:
            doomed = list(_PROVIDERS)
        else:
            doomed = [
                key for key, provider in _PROVIDERS.items()
                if provider.owner == str(owner)
            ]
        invocations = _remove_provider_keys(doomed)
    _cancel_invocations(invocations)
    return len(doomed)


def unregister_kanban_dependency_provider(
    dependency_kind: str,
    provider_name: str,
    *,
    owner: Optional[str] = None,
) -> bool:
    """Remove one provider registration and report whether it was removed.

    When ``owner`` is supplied, a provider owned by a different plugin is
    never removed.  That makes plugin teardown idempotent and prevents a late
    unload from deleting a replacement registration installed by another
    owner.
    """
    key = (
        _validate_name(dependency_kind, "dependency_kind"),
        _validate_name(provider_name, "provider_name"),
    )
    expected_owner = None if owner is None else str(owner)
    with _PROVIDER_LOCK:
        provider = _PROVIDERS.get(key)
        if provider is None:
            return False
        if expected_owner is not None and provider.owner != expected_owner:
            return False
        invocations = _remove_provider_keys([key])
    _cancel_invocations(invocations)
    return True


def registered_kanban_dependency_providers() -> tuple[tuple[str, str], ...]:
    """Return only provider keys, never callbacks or plugin internals."""
    with _PROVIDER_LOCK:
        return tuple(sorted(_PROVIDERS))


def _unknown(reason: str) -> KanbanDependencyResult:
    return KanbanDependencyResult(status="unknown", diagnostics={"reason": reason})


def _thaw(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _thaw(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _provider_context_payload(context: KanbanDependencyContext) -> dict[str, Any]:
    return {
        "board": context.board,
        "task": _thaw(context.task),
        "link": _thaw(context.link),
        "parent": _thaw(context.parent),
    }


def _run_provider_process(
    key: tuple[str, str],
    provider: _RegisteredProvider,
    context: KanbanDependencyContext,
) -> KanbanDependencyResult:
    request_deadline = time.monotonic() + provider.timeout_seconds
    request = json.dumps(
        {"module": provider.module, "module_file": provider.module_file,
         "qualname": provider.qualname,
         "context": _provider_context_payload(context)},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    if len(request) > MAX_BYTES:
        return _unknown("provider_request_too_large")
    env = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    repo_root = str(pathlib.Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        dict.fromkeys((repo_root, *provider.pythonpath))
    )
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    process: Optional[subprocess.Popen[bytes]] = None
    boundary: Optional[_ProcessContainment] = None
    cancelled = threading.Event()
    invocation_id = next(_INVOCATION_IDS)
    admission = _PROVIDER_ADMISSION[key]
    if not _GLOBAL_ADMISSION.acquire(
        timeout=max(0.0, request_deadline - time.monotonic())
    ):
        return _unknown("provider_busy")
    if not admission.acquire(
        timeout=max(0.0, request_deadline - time.monotonic())
    ):
        _GLOBAL_ADMISSION.release()
        return _unknown("provider_busy")
    try:
        boundary = _new_process_containment(invocation_id)
        if boundary is None:
            return _unknown("provider_containment_unavailable")
        command = [
            sys.executable,
            "-m",
            "hermes_cli.kanban_provider_worker",
        ]
        command.extend(("--containment-cgroup", boundary.worker_argument()))
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=(os.name == "posix"),
        )
        if boundary is not None:
            containment_deadline = min(request_deadline, time.monotonic() + 1.0)
            while not boundary.worker_is_contained(process.pid):
                now = time.monotonic()
                if process.poll() is not None or now >= containment_deadline:
                    reason = (
                        "provider_timeout"
                        if now >= request_deadline
                        else "provider_containment_unavailable"
                    )
                    return _unknown(reason)
                time.sleep(0.005)
        with _PROVIDER_LOCK:
            if _PROVIDERS.get(key) is not provider:
                return _unknown("provider_unavailable")
            _ACTIVE_INVOCATIONS.setdefault(key, {})[invocation_id] = (
                process,
                cancelled,
                boundary,
            )
        assert process.stdin is not None
        process.stdin.write(request)
        process.stdin.close()
        deadline = request_deadline
        output = bytearray()
        selector: Optional[selectors.BaseSelector] = selectors.DefaultSelector()
        assert process.stdout is not None
        process_stdout = process.stdout
        if not isinstance(process_stdout, io.BufferedReader):
            return _unknown("provider_unavailable")
        os.set_blocking(process_stdout.fileno(), False)
        selector.register(process_stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if cancelled.is_set():
                return _unknown("provider_unavailable")
            remaining = deadline - time.monotonic()
            events = selector.select(min(remaining, 0.05))
            for stream, _ in events:
                stream_fileobj = stream.fileobj
                if not isinstance(stream_fileobj, io.BufferedReader):
                    continue
                chunk = stream_fileobj.read(8192)
                if chunk:
                    output.extend(chunk)
                    if len(output) > MAX_BYTES:
                        return _unknown("provider_output_too_large")
                else:
                    selector.unregister(stream_fileobj)
            if process.poll() is not None and not selector.get_map():
                break
        else:
            return _unknown("provider_timeout")
        if not output:
            return _unknown("provider_unavailable")
        try:
            envelope = json.loads(bytes(output).decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return _unknown("provider_malformed_result")
        if not isinstance(envelope, Mapping):
            return _unknown("provider_malformed_result")
        outcome = envelope.get("outcome")
        if outcome == "exception":
            _log.warning(
                "Kanban dependency provider %s/%s failed closed",
                key[0],
                key[1],
            )
            return _unknown("provider_exception")
        if outcome == "malformed":
            return _unknown("provider_malformed_result")
        if outcome != "ok":
            return _unknown("provider_malformed_result")
        try:
            return normalize_dependency_result(envelope["result"])
        except Exception:
            return _unknown("provider_malformed_result")
    finally:
        try:
            if selector is not None:
                selector.close()
        except UnboundLocalError:
            pass
        cleanup_ok = _stop_process(process, boundary)
        if not cleanup_ok:
            raise RuntimeError("provider process containment cleanup was not proven")
        with _PROVIDER_LOCK:
            active = _ACTIVE_INVOCATIONS.get(key)
            if active is not None:
                active.pop(invocation_id, None)
                if not active:
                    _ACTIVE_INVOCATIONS.pop(key, None)
        admission.release()
        _GLOBAL_ADMISSION.release()


def evaluate_kanban_dependency_provider(
    dependency_kind: str,
    provider_name: str,
    context: KanbanDependencyContext,
) -> KanbanDependencyResult:
    """Invoke a provider with a hard timeout and fail closed on every fault."""
    # All official CLI and dispatcher paths converge here. Discovery is
    # idempotent and failure-isolated by PluginManager; a broken optional
    # plugin must not remove ordinary completion edges.
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered
        _ensure_plugins_discovered()
    except Exception:
        pass
    if not isinstance(context, KanbanDependencyContext):
        return _unknown("provider_context_invalid")
    try:
        kind = _validate_name(dependency_kind, "dependency_kind")
        name = _validate_name(provider_name, "provider_name")
    except (TypeError, ValueError):
        return _unknown("provider_identity_invalid")
    with _PROVIDER_LOCK:
        provider = _PROVIDERS.get((kind, name))
    if provider is None:
        return _unknown("provider_unavailable")

    try:
        return _run_provider_process((kind, name), provider, context)
    except Exception:
        _log.debug("Kanban dependency provider process failed", exc_info=True)
        return _unknown("provider_unavailable")


__all__ = [
    "DEPENDENCY_RESULT_STATUSES",
    "KanbanDependencyContext",
    "KanbanDependencyResult",
    "KanbanWorkspaceBasePin",
    "MAX_METADATA_BYTES",
    "evaluate_kanban_dependency_provider",
    "normalize_dependency_result",
    "register_kanban_dependency_provider",
    "registered_kanban_dependency_providers",
    "unregister_kanban_dependency_provider",
    "unregister_kanban_dependency_providers",
    "validate_dependency_metadata",
]

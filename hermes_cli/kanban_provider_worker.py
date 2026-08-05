"""Portable, bounded worker protocol for one typed Kanban provider call."""
from __future__ import annotations

import asyncio
import ctypes
import importlib
import importlib.util
import inspect
import json
import os
import signal
import sys
from typing import Any

MAX_FRAME_BYTES = 64 * 1024


def _resolve(module_name: str, qualname: str, module_file: str | None = None):
    if not isinstance(module_name, str) or not isinstance(qualname, str):
        raise ValueError("provider target must identify a module-level callable")
    if not module_name or not qualname or "<locals>" in qualname:
        raise ValueError("provider target is not importable")
    if module_file:
        spec = importlib.util.spec_from_file_location(
            module_name,
            module_file,
            submodule_search_locations=[os.path.dirname(module_file)],
        )
        if spec is None or spec.loader is None:
            raise ValueError("provider target file is not importable")
        target_module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = target_module
        spec.loader.exec_module(target_module)
        target = target_module
    else:
        target = importlib.import_module(module_name)
    for part in qualname.split("."):
        if not part:
            raise ValueError("provider target contains an invalid attribute")
        target = getattr(target, part)
    if not callable(target) or getattr(target, "__self__", None) is not None:
        raise ValueError("provider target is not a module-level callable")
    return target


def _write_envelope(envelope: dict[str, Any]) -> int:
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        return 3
    sys.__stdout__.buffer.write(encoded)
    sys.__stdout__.buffer.flush()
    return 0


def main() -> int:
    if os.name == "posix" and sys.platform.startswith("linux"):
        # Descendants inherit this and are killed when this worker dies,
        # covering providers that deliberately create a second session.
        try:
            ctypes.CDLL(None).prctl(1, signal.SIGKILL)
        except Exception:
            pass
    # Redirect all provider/import output before importing any plugin code. The
    # only bytes on stdout are the bounded protocol envelope below.
    with open(os.devnull, "w", encoding="utf-8") as sink:
        sys.stdout = sink
        sys.stderr = sink
        raw = sys.__stdin__.buffer.read(MAX_FRAME_BYTES + 1)
        if len(raw) > MAX_FRAME_BYTES:
            return 2
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            callback = _resolve(
                request["module"], request["qualname"], request.get("module_file")
            )
            from hermes_cli.kanban_dependencies import (
                KanbanDependencyContext,
                normalize_dependency_result,
            )
            context = request["context"]
            if not isinstance(context, dict):
                raise ValueError("context must be an object")
            value = callback(KanbanDependencyContext(**context))
            if inspect.isawaitable(value):
                value = asyncio.run(value)
            result = normalize_dependency_result(value)
            envelope = {"outcome": "ok", "result": result.as_dict()}
        except BaseException:
            envelope = {"outcome": "exception"}
    return _write_envelope(envelope)


if __name__ == "__main__":
    raise SystemExit(main())

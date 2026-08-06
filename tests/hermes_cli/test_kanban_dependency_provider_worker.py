import asyncio
import json
import os
from pathlib import Path

import pytest

from hermes_cli.kanban_dependencies import (
    KanbanDependencyContext,
    evaluate_kanban_dependency_provider,
    register_kanban_dependency_provider,
    unregister_kanban_dependency_providers,
)


def _sync_provider(_context):
    print("SENSITIVE_PROVIDER_TEXT")
    return {"status": "satisfied", "generation": "sync"}


async def _async_provider(_context):
    await asyncio.sleep(0)
    return {"status": "satisfied", "generation": "async"}


class _BoundProvider:
    def provide(self, _context):
        return {"status": "satisfied", "generation": "bound"}


_LAMBDA_PROVIDER = lambda _context: {"status": "satisfied", "generation": "lambda"}
_BOUND_PROVIDER = _BoundProvider().provide


def _flood_provider(_context):
    os.write(1, b"x" * (128 * 1024))
    return {"status": "satisfied"}


def _environment_probe_provider(context):
    Path(context.link["metadata"]["env_file"]).write_text(
        json.dumps(dict(os.environ), sort_keys=True)
    )
    return {"status": "satisfied", "generation": "environment"}


def _context():
    return KanbanDependencyContext(
        "alpha",
        {"id": "task"},
        {"parent_id": "parent", "child_id": "task", "metadata": {}},
        {"id": "parent"},
    )


@pytest.fixture(autouse=True)
def _clean_providers():
    unregister_kanban_dependency_providers()
    yield
    unregister_kanban_dependency_providers()


def test_importable_sync_provider_suppresses_output(capsys):
    register_kanban_dependency_provider("probe.sync", "test", _sync_provider, timeout_seconds=5.0)
    result = evaluate_kanban_dependency_provider("probe.sync", "test", _context())
    assert result.status == "satisfied"
    assert "SENSITIVE_PROVIDER_TEXT" not in capsys.readouterr().out


def test_importable_async_provider_runs_in_worker():
    register_kanban_dependency_provider("probe.async", "test", _async_provider, timeout_seconds=5.0)
    result = evaluate_kanban_dependency_provider("probe.async", "test", _context())
    assert result.status == "satisfied"
    assert result.generation == "async"


def test_registration_rejects_nonportable_callables():
    def local(_context):
        return {"status": "satisfied"}

    for callback in (local, _LAMBDA_PROVIDER, _BOUND_PROVIDER):
        with pytest.raises(ValueError, match="module-level"):
            register_kanban_dependency_provider("probe.invalid", "test", callback)


def test_worker_fails_closed_on_stdout_flood():
    register_kanban_dependency_provider(
        "probe.flood", "test", _flood_provider, timeout_seconds=5.0
    )
    result = evaluate_kanban_dependency_provider("probe.flood", "test", _context())
    assert result.status == "unknown"
    assert result.diagnostics["reason"] == "provider_output_too_large"


def test_worker_uses_strict_clean_environment(tmp_path, monkeypatch):
    forbidden = (
        "HERMES_HOME",
        "HERMES_PROFILE",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    )
    ambient_home = tmp_path / "ambient-hermes_home"
    for name in forbidden:
        value = str(ambient_home) if name == "HERMES_HOME" else f"ambient-{name.lower()}"
        monkeypatch.setenv(name, value)
    env_file = tmp_path / "provider-env.json"
    register_kanban_dependency_provider(
        "probe.environment",
        "test",
        _environment_probe_provider,
        timeout_seconds=5.0,
    )
    context = KanbanDependencyContext(
        "alpha",
        {"id": "task"},
        {
            "parent_id": "parent",
            "child_id": "task",
            "metadata": {"env_file": str(env_file)},
        },
        {"id": "parent"},
    )

    result = evaluate_kanban_dependency_provider(
        "probe.environment", "test", context
    )

    assert result.status == "satisfied"
    observed = json.loads(env_file.read_text())
    assert set(observed) <= {
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "PYTHONUTF8",
        "TZ",
    }
    assert all(name not in observed for name in forbidden)
    assert not (Path(__file__).resolve().parents[2] / "ambient-hermes_home").exists()

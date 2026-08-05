import asyncio
import os

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


def _flood_provider(_context):
    os.write(1, b"x" * (128 * 1024))
    return {"status": "satisfied"}


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
    register_kanban_dependency_provider("probe.sync", "test", _sync_provider)
    result = evaluate_kanban_dependency_provider("probe.sync", "test", _context())
    assert result.status == "satisfied"
    assert "SENSITIVE_PROVIDER_TEXT" not in capsys.readouterr().out


def test_importable_async_provider_runs_in_worker():
    register_kanban_dependency_provider("probe.async", "test", _async_provider)
    result = evaluate_kanban_dependency_provider("probe.async", "test", _context())
    assert result.status == "satisfied"
    assert result.generation == "async"


def test_registration_rejects_local_closure():
    def local(_context):
        return {"status": "satisfied"}

    with pytest.raises(ValueError, match="module-level"):
        register_kanban_dependency_provider("probe.invalid", "test", local)


def test_worker_fails_closed_on_stdout_flood():
    register_kanban_dependency_provider(
        "probe.flood", "test", _flood_provider, timeout_seconds=1
    )
    result = evaluate_kanban_dependency_provider("probe.flood", "test", _context())
    assert result.status == "unknown"
    assert result.diagnostics["reason"] == "provider_output_too_large"

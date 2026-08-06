"""Imported-behavior regressions for connection-bound Kanban authority."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb

kb_any: Any = kb


def _seed_project(root: Path, slug: str) -> tuple[str, Path]:
    repo = root / f"{slug}-repo"
    repo.mkdir(parents=True)
    with pdb.connect_closing(db_path=root / "projects.db") as conn:
        project_id = pdb.create_project(
            conn,
            name=slug,
            slug=slug,
            primary_path=str(repo),
        )
    return project_id, repo


def _seed_custom_board(
    root: Path,
    *,
    project_id: str | None = None,
    repo: Path | None = None,
    directory: str = "prod",
    metadata: str = "missing",
) -> Path:
    board_root = root / "kanban" / "boards" / directory
    board_root.mkdir(parents=True)
    if metadata == "valid":
        (board_root / "board.json").write_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "default_workdir": str(repo) if repo is not None else None,
                }
            ),
            encoding="utf-8",
        )
    elif metadata == "malformed":
        (board_root / "board.json").write_text("{malformed", encoding="utf-8")
    elif metadata != "missing":
        raise AssertionError(metadata)
    db_path = board_root / "kanban.db"
    kb.init_db(db_path=db_path)
    return db_path


def _set_ambient(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)


def test_connection_authority_survives_workspace_normal_and_gated_spawn(
    tmp_path, monkeypatch
):
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    db_a = _seed_custom_board(root_a)
    _seed_custom_board(root_b)

    _set_ambient(monkeypatch, root_b)
    conn_a = kb.connect(db_path=db_a)
    try:
        task_id = kb.create_task(conn_a, title="authority scratch", assignee="programmer")
        task = kb.get_task(conn_a, task_id)
        assert task is not None

        expected_workspace = (
            root_a / "kanban" / "boards" / "prod" / "workspaces" / task_id
        )
        workspace = kb.resolve_workspace(
            task,
            board="prod",
            conn=conn_a,
            materialize=True,
        )
        assert workspace == expected_workspace
        assert workspace.is_dir()
        assert not (root_b / "kanban" / "boards" / "prod" / "workspaces" / task_id).exists()

        class _NormalProcess:
            pid = 41001

        class _GateStdin:
            def close(self):
                return None

        class _GateProcess:
            pid = 41002
            stdin = _GateStdin()

            def wait(self, timeout=None):
                return 0

        popen_calls: list[dict] = []

        def fake_popen(*args, **kwargs):
            popen_calls.append(kwargs)
            return _NormalProcess() if len(popen_calls) == 1 else _GateProcess()

        monkeypatch.setattr("subprocess.Popen", fake_popen)
        monkeypatch.setattr(kb, "_capture_process_handle", lambda _pid: None)
        monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
        monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda _root: None)
        monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

        authority = kb_any._connection_authority(conn_a)
        assert authority is not None
        kb_any._default_spawn(
            task,
            str(workspace),
            board="prod",
            authority_context=authority,
        )
        normal_env = popen_calls[0]["env"]
        assert normal_env["HERMES_KANBAN_HOME"] == str(root_a)
        assert normal_env["HERMES_KANBAN_DB"] == str(db_a)
        assert normal_env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(expected_workspace.parent)
        assert normal_env["HERMES_KANBAN_WORKSPACE"] == str(workspace)
        assert normal_env["TERMINAL_CWD"] == str(workspace)
        assert Path(popen_calls[0]["stdout"].name) == (
            root_a / "kanban" / "boards" / "prod" / "logs" / f"{task_id}.log"
        )
        popen_calls[0]["stdout"].close()
        assert str(root_b) not in {
            normal_env["HERMES_KANBAN_HOME"],
            normal_env["HERMES_KANBAN_DB"],
            normal_env["HERMES_KANBAN_WORKSPACES_ROOT"],
            normal_env["HERMES_KANBAN_WORKSPACE"],
        }

        task.current_run_id = 17
        task.claim_lock = "claim-authority"
        with pytest.raises(Exception):
            kb_any._default_spawn(
                task,
                str(workspace),
                board="prod",
                authority_context=authority,
                launch_gate=True,
                launch_token="gate-token",
                launch_authority_id=23,
            )
        gate_env = popen_calls[1]["env"]
        assert gate_env["HERMES_KANBAN_HOME"] == str(root_a)
        assert gate_env["HERMES_KANBAN_DB"] == str(db_a)
        assert gate_env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(expected_workspace.parent)
        assert gate_env["HERMES_KANBAN_WORKSPACE"] == str(workspace)
        assert gate_env["TERMINAL_CWD"] == str(workspace)
        assert gate_env["HERMES_LAUNCH_GATE_DB"] == str(db_a)
        assert Path(gate_env["HERMES_LAUNCH_GATE_LOG_PATH"]) == (
            root_a / "kanban" / "boards" / "prod" / "logs" / f"{task_id}.log"
        )
    finally:
        conn_a.close()


def test_fixed_legacy_connection_rejects_ambient_project_and_stays_scratch(
    tmp_path, monkeypatch
):
    root_a = tmp_path / "legacy-a"
    root_b = tmp_path / "ambient-b"
    root_a.mkdir()
    root_b.mkdir()
    project_b, _repo_b = _seed_project(root_b, "ambient-project")
    legacy_db = root_a / "kanban.db"
    kb.init_db(db_path=legacy_db)

    _set_ambient(monkeypatch, root_b)
    with kb.connect(db_path=legacy_db, board="default") as conn:
        with pytest.raises(ValueError, match="project|bound|not found"):
            kb.create_task(
                conn,
                title="must reject ambient project",
                project_id=project_b,
            )

        task_id = kb.create_task(conn, title="legacy ordinary task")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.project_id is None
        assert task.workspace_kind == "scratch"
        expected = root_a / "kanban" / "workspaces" / task_id
        assert kb.resolve_workspace(task, conn=conn, materialize=True) == expected
        assert expected.is_dir()
        assert not (root_b / "kanban" / "workspaces" / task_id).exists()


def test_project_source_rejects_noncanonical_connection_even_with_ambient_project(
    tmp_path, monkeypatch
):
    root_a = tmp_path / "case-a"
    root_b = tmp_path / "ambient-b"
    root_a.mkdir()
    root_b.mkdir()
    project_b, _repo_b = _seed_project(root_b, "ambient-project")
    db_a = _seed_custom_board(root_a, directory="PROD")

    _set_ambient(monkeypatch, root_b)
    with kb.connect(db_path=db_a, board="prod") as conn:
        parent_id = kb.create_task(conn, title="unbound source parent")
        with pytest.raises(ValueError, match="no bound board"):
            kb.create_task(
                conn,
                title="noncanonical source must reject",
                project_id=project_b,
                project_source_task_id=parent_id,
            )


def test_same_slug_connections_alternate_workspace_operations_without_crossover(
    tmp_path, monkeypatch
):
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    db_a = _seed_custom_board(root_a)
    db_b = _seed_custom_board(root_b)

    _set_ambient(monkeypatch, root_b)
    conn_a = kb.connect(db_path=db_a)
    conn_b = kb.connect(db_path=db_b)
    captured: list[tuple[Path, Path]] = []
    monkeypatch.setattr(kb, "_profile_is_configured", lambda _profile: True)
    monkeypatch.setattr(
        kb,
        "_synchronize_reviewer_policy",
        lambda _conn, dry_run=False, authority=None: SimpleNamespace(
            synchronized=True
        ),
    )
    try:
        for index, (conn, root, label) in enumerate(
            ((conn_a, root_a, "a"), (conn_b, root_b, "b"),
             (conn_a, root_a, "a2"), (conn_b, root_b, "b2"))
        ):
            _set_ambient(monkeypatch, root_b if label.startswith("a") else root_a)
            task_id = kb.create_task(conn, title=f"{label} task", assignee="programmer")
            task = kb.get_task(conn, task_id)
            assert task is not None
            workspace = kb.resolve_workspace(task, conn=conn, materialize=True)
            expected = root / "kanban" / "boards" / "prod" / "workspaces" / task_id
            assert workspace == expected
            captured.append((root, workspace))
            result = kb.dispatch_once(
                conn,
                spawn_fn=lambda _task, path: 42000 + index,
                max_spawn=10,
            )
            assert result.spawned
            assert result.spawned[0][2] == str(expected)
            other_root = root_b if root is root_a else root_a
            assert not (
                other_root
                / "kanban"
                / "boards"
                / "prod"
                / "workspaces"
                / task_id
            ).exists()
    finally:
        conn_a.close()
        conn_b.close()

    _set_ambient(monkeypatch, root_a)
    with kb.connect(db_path=db_b) as reopened_b:
        task_id = kb.create_task(reopened_b, title="b after reopen", assignee="programmer")
        task = kb.get_task(reopened_b, task_id)
        assert task is not None
        workspace = kb.resolve_workspace(task, conn=reopened_b, materialize=False)
        assert workspace == root_b / "kanban" / "boards" / "prod" / "workspaces" / task_id
        assert all(
            str(root) in str(path)
            for root, path in captured
        )


def test_connection_authority_pins_attachments_and_completion_cleanup(
    tmp_path, monkeypatch
):
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    db_a = _seed_custom_board(root_a)
    _seed_custom_board(root_b)

    _set_ambient(monkeypatch, root_b)
    with kb.connect(db_path=db_a) as conn:
        task_id = kb.create_task(conn, title="artifact authority")
        task = kb.get_task(conn, task_id)
        assert task is not None
        workspace = kb.resolve_workspace(
            task, board="prod", conn=conn, materialize=True
        )
        kb.set_workspace_path(conn, task_id, workspace)
        artifact = workspace / "deliverable.txt"
        artifact.write_text("root-a artifact", encoding="utf-8")

        attachment_id = kb.store_attachment_bytes(
            conn,
            task_id,
            "uploaded.txt",
            b"root-a upload",
            board="prod",
        )
        uploaded = kb.get_attachment(conn, attachment_id)
        assert uploaded is not None
        assert Path(uploaded.stored_path) == (
            root_a
            / "kanban"
            / "boards"
            / "prod"
            / "attachments"
            / task_id
            / "uploaded.txt"
        )
        assert Path(uploaded.stored_path).read_bytes() == b"root-a upload"
        assert not (
            root_b
            / "kanban"
            / "boards"
            / "prod"
            / "attachments"
            / task_id
        ).exists()

        outside = root_b / "outside-attachment.txt"
        outside.write_text("must stay put", encoding="utf-8")
        with pytest.raises(ValueError, match="authoritative task root"):
            kb.add_attachment(
                conn,
                task_id,
                filename="outside.txt",
                stored_path=str(outside),
            )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_attachments SET stored_path = ? WHERE id = ?",
                (str(outside), attachment_id),
            )
        with pytest.raises(ValueError, match="authoritative task root"):
            kb.delete_attachment(conn, attachment_id)
        assert outside.read_text(encoding="utf-8") == "must stay put"
        assert kb.get_attachment(conn, attachment_id) is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_attachments SET stored_path = ? WHERE id = ?",
                (uploaded.stored_path, attachment_id),
            )

        assert kb.complete_task(
            conn,
            task_id,
            summary="artifact preserved",
            metadata={"artifacts": [str(artifact)]},
        )
        assert not workspace.exists()
        attachments = kb.list_attachments(conn, task_id)
        assert {attachment.filename for attachment in attachments} == {
            "uploaded.txt",
            "deliverable.txt",
        }
        copied = next(
            attachment
            for attachment in attachments
            if attachment.filename == "deliverable.txt"
        )
        assert Path(copied.stored_path).read_text(encoding="utf-8") == "root-a artifact"
        assert all(str(root_a) in attachment.stored_path for attachment in attachments)
        assert not (
            root_b
            / "kanban"
            / "boards"
            / "prod"
            / "attachments"
            / task_id
        ).exists()


def test_connection_authority_pins_review_gate_cleanup(tmp_path, monkeypatch):
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    db_a = _seed_custom_board(root_a)
    _seed_custom_board(root_b)

    workspace_a = root_a / "kanban" / "boards" / "prod" / "workspaces" / "gate-a"
    workspace_b = root_b / "kanban" / "boards" / "prod" / "workspaces" / "gate-b"
    workspace_a.mkdir(parents=True)
    workspace_b.mkdir(parents=True)
    _set_ambient(monkeypatch, root_b)
    with kb.connect(db_path=db_a) as conn:
        authority = kb_any._connection_authority(conn)
        assert authority is not None
        gate_a: Any = {
            "workspace_created": 1,
            "workspace_path": str(workspace_a),
            "workspace_kind": "scratch",
        }
        kb_any._cleanup_review_launch_workspace(
            gate_a,
            authority=authority,
        )
        assert not workspace_a.exists()
        assert workspace_b.is_dir()
        gate_b: Any = {
            "workspace_created": 1,
            "workspace_path": str(workspace_b),
            "workspace_kind": "scratch",
        }
        kb_any._cleanup_review_launch_workspace(
            gate_b,
            authority=authority,
        )
        assert workspace_b.is_dir()

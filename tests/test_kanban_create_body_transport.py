from __future__ import annotations

import argparse
import os
import subprocess
import sys

import pytest

from hermes_cli import kanban as cli_kanban
from hermes_cli import kanban_db as kb


def test_cli_body_file_is_hermetic_and_inline_body_is_rejected(tmp_path):
    sentinel = "BODY_SENTINEL_MUST_NOT_BE_IN_ARGV_OR_STDERR"
    body_file = tmp_path / "body.txt"
    body_file.write_text(sentinel, encoding="utf-8")
    home = tmp_path / ".hermes"
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = os.getcwd()

    completed = subprocess.run(
        [
            sys.executable, "-m", "hermes_cli.main", "kanban", "create",
            "hermetic", "--body-file", str(body_file), "--json",
        ],
        cwd=os.getcwd(), env=env, text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert sentinel not in completed.stderr
    assert sentinel not in " ".join(completed.args)
    assert '"body": "' + sentinel in completed.stdout

    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command")
    cli_kanban.build_parser(sub)
    with pytest.raises(SystemExit):
        root.parse_args(["kanban", "create", "bad", "--body", sentinel])


def test_fake_executable_never_receives_task_body_in_argv_or_stderr(tmp_path, monkeypatch):
    sentinel = "BODY_SENTINEL_MUST_NOT_BE_IN_CHILD_TRANSPORT"
    home = tmp_path / ".hermes"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capture = tmp_path / "child-argv.txt"
    fake = tmp_path / "fake-hermes"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > '{capture}'\n"
        "printf '%s\\n' child-stderr >&2\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BIN", str(fake))
    monkeypatch.setenv("HERMES_PROFILE", "programmer")

    conn = kb.connect(home / "kanban.db")
    try:
        tid = kb.create_task(conn, title="transport", body=sentinel, assignee="programmer")
        task = kb.get_task(conn, tid)
        assert task is not None
        pid = kb._default_spawn(task, str(workspace))
        assert pid is not None
        os.waitpid(pid, 0)
    finally:
        conn.close()
    argv_text = capture.read_text(encoding="utf-8")
    log_text = (home / "kanban" / "logs" / f"{tid}.log").read_text(encoding="utf-8")
    assert sentinel not in argv_text
    assert sentinel not in log_text
    assert "child-stderr" in log_text

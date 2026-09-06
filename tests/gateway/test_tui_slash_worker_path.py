"""Regression test for slash_worker PATH construction (#83845).

When the gateway is launched by the Desktop/Dashboard app it can inherit a
minimal PATH (/usr/local/sbin:/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin)
that omits the Hermes venv bin dir and the worker user's local bin. The spawned
tui_gateway.slash_worker then cannot resolve Hermes-managed CLIs such as
browser-use/uvx via shutil.which, breaking browser_exec.

`tui_gateway.server._prepend_tool_paths` prepends managed bin, venv bin, and
the selected child's HOME/.local/bin (or HERMES_REAL_HOME/.local/bin) while
preserving the inherited PATH. It must not infer a user bin from the process
home when neither child-home variable is present.
"""

import os
import sys
from pathlib import Path

from tui_gateway import server as tui_server


class TestPrependToolPaths:
    def test_prepends_managed_venv_and_user_bin(self, monkeypatch, tmp_path):
        process_home = tmp_path / "process-home"
        child_home = tmp_path / "child-home"
        monkeypatch.setattr(Path, "home", lambda: process_home)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
        env = {"HOME": str(child_home), "PATH": "/usr/bin"}
        result = tui_server._prepend_tool_paths(env)

        parts = result["PATH"].split(os.pathsep)
        # managed bin first (managed-first policy), then venv bin, then the
        # selected child's local bin, then the original PATH preserved
        assert parts[0] == str(tmp_path / "hh" / "bin")
        assert parts[1] == str(Path(sys.executable).parent)
        assert parts[2] == str(child_home / ".local" / "bin")
        assert parts[-1] == "/usr/bin"
        assert str(process_home / ".local" / "bin") not in parts

    def test_preserves_existing_path_when_empty(self, monkeypatch, tmp_path):
        process_home = tmp_path / "process-home"
        real_home = tmp_path / "real-home"
        monkeypatch.setattr(Path, "home", lambda: process_home)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
        env = {"HERMES_REAL_HOME": str(real_home)}
        result = tui_server._prepend_tool_paths(env)

        parts = result["PATH"].split(os.pathsep)
        assert parts == [
            str(tmp_path / "hh" / "bin"),
            str(Path(sys.executable).parent),
            str(real_home / ".local" / "bin"),
        ]
        assert str(process_home / ".local" / "bin") not in parts

    def test_managed_bin_leads_path(self, monkeypatch, tmp_path):
        process_home = tmp_path / "process-home"
        monkeypatch.setattr(Path, "home", lambda: process_home)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
        # No child HOME or HERMES_REAL_HOME: process-global home must not
        # become an implicit PATH entry.
        env = {"PATH": "/bin"}
        result = tui_server._prepend_tool_paths(env)

        assert result["PATH"].split(os.pathsep) == [
            str(tmp_path / "hh" / "bin"),
            str(Path(sys.executable).parent),
            "/bin",
        ]
        assert str(process_home / ".local" / "bin") not in result["PATH"].split(os.pathsep)

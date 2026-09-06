"""Tests for TUI gateway slash_worker profile_home propagation (#40677)."""

import os
from unittest.mock import MagicMock, patch


def test_slash_worker_accepts_profile_home(tmp_path, monkeypatch):
    """The real slash-worker launch boundary binds all three home keys to the target."""
    root = tmp_path / ".hermes"
    parent = root / "profiles" / "parent"
    target = root / "profiles" / "work"
    (parent / "home").mkdir(parents=True)
    (target / "home").mkdir(parents=True)
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(parent))
    monkeypatch.setenv("HOME", str(parent / "home"))
    monkeypatch.setenv("HERMES_REAL_HOME", str(real_home))

    with patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value.stdout = MagicMock()
        mock_popen.return_value.stderr = MagicMock()

        from tui_gateway.server import _SlashWorker

        worker = _SlashWorker(
            session_key="test_key",
            model="test-model",
            profile_home=str(target),
        )

        assert mock_popen.called
        call_kwargs = mock_popen.call_args[1]
        env = call_kwargs["env"]
        assert env["HERMES_HOME"] == str(target)
        assert env["HOME"] == str(target / "home")
        assert env["HERMES_REAL_HOME"] == str(real_home)
        assert str(target / "home" / ".local" / "bin") in env["PATH"].split(os.pathsep)
        assert str(parent / "home" / ".local" / "bin") not in env["PATH"].split(os.pathsep)
        worker.close()



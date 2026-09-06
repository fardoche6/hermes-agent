"""Regression tests for _apply_profile_override HERMES_HOME guard (issue #22502).

When HERMES_HOME is set to the hermes root (e.g. systemd hardcodes
HERMES_HOME=/root/.hermes), _apply_profile_override must still read
active_profile and update HERMES_HOME to the profile directory.

When HERMES_HOME is already a profile directory (.../profiles/<name>),
_apply_profile_override must trust it and return without re-reading
active_profile (child-process inheritance contract).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest


def _run_apply_profile_override(
    tmp_path, monkeypatch, *, hermes_home: str | None, active_profile: str | None,
    argv: list[str] | None = None, extra_env: dict[str, str] | None = None,
):
    """Run _apply_profile_override in isolation.

    Returns the value of os.environ["HERMES_HOME"] after the call,
    or None if unset.
    """
    hermes_root = tmp_path / ".hermes"
    hermes_root.mkdir(parents=True, exist_ok=True)

    if active_profile is not None:
        (hermes_root / "active_profile").write_text(active_profile)

    if active_profile and active_profile != "default":
        (hermes_root / "profiles" / active_profile).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if hermes_home is not None:
        monkeypatch.setenv("HERMES_HOME", hermes_home)
    else:
        monkeypatch.delenv("HERMES_HOME", raising=False)

    monkeypatch.setattr(sys, "argv", argv or ["hermes", "gateway", "start"])

    # Scrub supervisor markers the host environment may carry (systemd-run
    # CI runners export INVOCATION_ID) so each test controls them explicitly.
    for var in (
        "HERMES_SUPERVISED_CHILD",
        "HERMES_S6_SUPERVISED_CHILD",
        "INVOCATION_ID",
        "HERMES_GATEWAY_EXTERNAL_SUPERVISOR",
    ):
        monkeypatch.delenv(var, raising=False)

    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)

    from hermes_cli.main import _apply_profile_override
    _apply_profile_override()

    return os.environ.get("HERMES_HOME")


def test_direct_cli_binds_target_before_dotenv_config_plugin_mcp_and_path_home(
    tmp_path,
):
    """Import the real CLI in a fresh process and inspect every target-bound reader."""
    root = tmp_path / ".hermes"
    parent = root / "profiles" / "parent"
    target = root / "profiles" / "target"
    (parent / "home").mkdir(parents=True)
    (target / "home").mkdir(parents=True)
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    (parent / "config.yaml").write_text("profile_marker: parent-config\n", encoding="utf-8")
    (parent / ".env").write_text("TARGET_DOTENV_MARKER=parent-dotenv\n", encoding="utf-8")
    (target / "config.yaml").write_text(
        "profile_marker: target-config\n"
        "mcp_servers:\n"
        "  target_only:\n"
        "    command: /bin/false\n"
        "plugins:\n"
        "  enabled:\n"
        "    - target_plugin\n",
        encoding="utf-8",
    )
    (target / ".env").write_text("TARGET_DOTENV_MARKER=target-dotenv\n", encoding="utf-8")
    plugin_dir = target / "plugins" / "target_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: target_plugin\nversion: '1.0'\ndescription: target-only\n",
        encoding="utf-8",
    )

    script = textwrap.dedent(
        """
        import json
        import os
        import sys
        from pathlib import Path

        sys.argv = ["hermes", "-p", "target", "--help"]
        import hermes_cli.main  # noqa: F401 — exercise the real early startup path
        from hermes_cli.config import read_raw_config
        from hermes_cli.plugins_discovery import collect_directory_manifests
        from tools.mcp_tool_config import _load_mcp_config

        raw = read_raw_config()
        target_plugins = [
            str(manifest.path)
            for manifest in collect_directory_manifests()
            if manifest.name == "target_plugin"
        ]
        print(json.dumps({
            "hermes_home": os.environ["HERMES_HOME"],
            "home": os.environ["HOME"],
            "real_home": os.environ["HERMES_REAL_HOME"],
            "path_home": str(Path.home()),
            "dotenv": os.environ.get("TARGET_DOTENV_MARKER"),
            "config": raw.get("profile_marker"),
            "plugins": target_plugins,
            "mcp": sorted(_load_mcp_config()),
        }))
        """
    )
    env = {
        "HOME": str(parent / "home"),
        "HERMES_HOME": str(parent),
        "HERMES_REAL_HOME": str(real_home),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "PYTHONUTF8": "1",
        "TERM": "dumb",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == {
        "hermes_home": str(target),
        "home": str(target / "home"),
        "real_home": str(real_home),
        "path_home": str(target / "home"),
        "dotenv": "target-dotenv",
        "config": "target-config",
        "plugins": [str(plugin_dir)],
        "mcp": ["target_only"],
    }


def test_early_interface_probe_reads_selected_profile_after_binding(tmp_path):
    """The early interface cache must not read the parent before ``-p`` binds."""
    root = tmp_path / ".hermes"
    parent = root / "profiles" / "parent"
    target = root / "profiles" / "target"
    (parent / "home").mkdir(parents=True)
    (target / "home").mkdir(parents=True)
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    (parent / "config.yaml").write_text("display:\n  interface: tui\n", encoding="utf-8")
    (target / "config.yaml").write_text("display:\n  interface: cli\n", encoding="utf-8")
    script = textwrap.dedent(
        """
        import sys

        sys.argv = ["hermes", "-p", "target"]

        class TTYProxy:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def isatty(self):
                return True

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

        sys.stdin = TTYProxy(sys.stdin)
        sys.stdout = TTYProxy(sys.stdout)
        import hermes_cli.main as main

        assert main._config_default_interface_early() == "cli"
        """
    )
    env = {
        "HOME": str(parent / "home"),
        "HERMES_HOME": str(parent),
        "HERMES_REAL_HOME": str(real_home),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "PYTHONUTF8": "1",
        "TERM": "dumb",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_profile_override_rejects_relative_home_before_mutation(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "relative/profile")
    monkeypatch.setattr(sys, "argv", ["hermes", "-p", "target", "chat"])

    from hermes_cli.main import _apply_profile_override

    with pytest.raises(SystemExit):
        _apply_profile_override()
    assert os.environ["HERMES_HOME"] == "relative/profile"


def test_profile_resolution_rejects_unrelated_profiles_segment(tmp_path, monkeypatch):
    unrelated = tmp_path / "srv" / "profiles" / "buildcache"
    unrelated.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(unrelated))

    from hermes_cli.profiles import resolve_profile_env

    with pytest.raises(ValueError, match="not anchored"):
        resolve_profile_env("target")


class TestApplyProfileOverrideHermesHomeGuard:
    """Regression guard for issue #22502.

    Verifies that HERMES_HOME pointing to the hermes root does NOT suppress
    the active_profile check, while HERMES_HOME already pointing to a
    profile directory IS trusted as-is.
    """

    def test_hermes_home_at_root_with_active_profile_is_redirected(
        self, tmp_path, monkeypatch
    ):
        """HERMES_HOME=/root/.hermes + active_profile=coder must redirect
        HERMES_HOME to .../profiles/coder.

        Bug scenario from #22502: systemd sets HERMES_HOME to the hermes root
        and the user switches to a profile via `hermes profile use`.
        Before the fix, the guard returned early and active_profile was ignored.
        """
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)

        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="coder",
        )

        assert result is not None, "HERMES_HOME must be set after profile redirect"
        assert "profiles" in result, (
            f"Expected HERMES_HOME to point into profiles/ dir, got: {result!r}"
        )
        assert result.endswith("coder"), (
            f"Expected HERMES_HOME to end with 'coder', got: {result!r}"
        )


    def test_sudo_explicit_profile_resolves_invoking_users_profile(self, tmp_path, monkeypatch):
        """sudo elias ... should resolve `-p elias` under SUDO_USER, not root."""
        root_home = tmp_path / "root"
        user_home = tmp_path / "home" / "hermes"
        profile_dir = user_home / ".hermes" / "profiles" / "elias"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (root_home / ".hermes").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: root_home)
        monkeypatch.setenv("SUDO_USER", "hermes")
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(sys, "argv", ["hermes", "-p", "elias", "gateway", "install", "--system"])

        import pwd

        monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir=str(user_home)))

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("HERMES_HOME") == str(profile_dir)
        assert sys.argv == ["hermes", "gateway", "install", "--system"]




class TestSupervisedChildIgnoresStickyProfile:
    """The reserved default gateway s6 slot must not follow active_profile.

    Inside the Docker s6 image the ``gateway-default`` service slot runs a
    bare ``hermes gateway run`` (no ``-p``) to mean "the root HERMES_HOME
    profile". The run-script exports ``HERMES_S6_SUPERVISED_CHILD=1``.
    Without a guard, ``_apply_profile_override`` would read the sticky
    ``active_profile`` file (set by e.g. the dashboard profile switcher) and
    redirect the reserved default gateway into that profile — producing a
    duplicate gateway for the active profile and no real default gateway.
    """


    def test_non_supervised_run_still_follows_active_profile(
        self, tmp_path, monkeypatch
    ):
        """Without the sentinel, a normal `hermes gateway run` still honors
        active_profile — the guard is scoped strictly to supervised children."""
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=None,
            active_profile="briefer",
            argv=["hermes", "gateway", "run"],
        )

        assert result is not None
        assert result.endswith("briefer")

    def test_supervised_named_profile_flag_still_wins(self, tmp_path, monkeypatch):
        """A supervised named-profile slot passes ``-p <name>`` explicitly;
        that must still resolve (the sentinel guard only skips the sticky
        active_profile fallback, never an explicit flag)."""
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)
        (hermes_root / "active_profile").write_text("briefer")
        (hermes_root / "profiles" / "briefer").mkdir(parents=True, exist_ok=True)
        (hermes_root / "profiles" / "coder").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setenv("HERMES_S6_SUPERVISED_CHILD", "1")
        monkeypatch.setattr(sys, "argv", ["hermes", "-p", "coder", "gateway", "run"])

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        result = os.environ.get("HERMES_HOME")
        assert result is not None
        assert result.endswith("coder")



class TestGeneralizedSupervisorMarkers:
    """Regression tests for issue #74872.

    A systemd/launchd/Scheduled-Task supervised gateway launch pins its
    profile identity via the unit's HERMES_HOME (root home for the default
    profile). It must NEVER follow the sticky ``active_profile`` file —
    otherwise the default-profile gateway silently assumes another profile's
    identity (logs + Telegram bot token) and double-polls that profile's
    token. Markers: HERMES_SUPERVISED_CHILD (generalized, exported by
    generated units), INVOCATION_ID (systemd, gateway commands only), and
    HERMES_GATEWAY_EXTERNAL_SUPERVISOR (explicit opt-in).
    """

    def _root_home(self, tmp_path):
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)
        return hermes_root

    def test_supervised_child_marker_skips_active_profile(
        self, tmp_path, monkeypatch
    ):
        """HERMES_SUPERVISED_CHILD=1 + root HERMES_HOME must keep the
        default profile's home even when active_profile names another
        profile (the #74872 identity-assumption vector)."""
        hermes_root = self._root_home(tmp_path)
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="telegram_nick",
            argv=["hermes", "gateway", "run"],
            extra_env={"HERMES_SUPERVISED_CHILD": "1"},
        )
        assert result == str(hermes_root), (
            f"supervised default gateway was redirected to {result!r}"
        )

    def test_systemd_invocation_id_skips_active_profile_for_gateway(
        self, tmp_path, monkeypatch
    ):
        """INVOCATION_ID (systemd service child) must suppress the sticky
        redirect for gateway commands — covers units installed before the
        HERMES_SUPERVISED_CHILD marker existed."""
        hermes_root = self._root_home(tmp_path)
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="telegram_nick",
            argv=["hermes", "gateway", "run"],
            extra_env={"INVOCATION_ID": "deadbeef" * 4},
        )
        assert result == str(hermes_root)

    def test_invocation_id_does_not_affect_non_gateway_commands(
        self, tmp_path, monkeypatch
    ):
        """INVOCATION_ID leaks into every descendant of a systemd-launched
        process (CI runners, user services). Non-gateway commands must keep
        honoring the sticky active_profile."""
        hermes_root = self._root_home(tmp_path)
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="coder",
            argv=["hermes", "chat"],
            extra_env={"INVOCATION_ID": "deadbeef" * 4},
        )
        assert result is not None
        assert result.endswith("coder")

    def test_external_supervisor_marker_skips_active_profile(
        self, tmp_path, monkeypatch
    ):
        hermes_root = self._root_home(tmp_path)
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="telegram_nick",
            argv=["hermes", "gateway", "run"],
            extra_env={"HERMES_GATEWAY_EXTERNAL_SUPERVISOR": "1"},
        )
        assert result == str(hermes_root)

    def test_generated_systemd_unit_exports_supervised_marker(
        self, tmp_path, monkeypatch
    ):
        """The generated systemd unit must carry the marker so fresh installs
        are protected without relying on the INVOCATION_ID heuristic."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        from hermes_cli.gateway import generate_systemd_unit

        unit = generate_systemd_unit()
        assert 'Environment="HERMES_SUPERVISED_CHILD=1"' in unit

    def test_generated_launchd_plist_exports_supervised_marker(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        from hermes_cli.gateway import generate_launchd_plist

        plist = generate_launchd_plist()
        assert "<key>HERMES_SUPERVISED_CHILD</key>" in plist

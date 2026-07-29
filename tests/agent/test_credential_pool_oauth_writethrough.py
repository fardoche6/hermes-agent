"""Regression tests for credential-pool OAuth refresh write-through to root.

Companion to ``tests/hermes_cli/test_xai_oauth_writethrough.py``. That file
covers the *non-pool* xAI refresh path (``_save_xai_oauth_tokens``). These
cover the **credential-pool** refresh path
(``CredentialPool._sync_device_code_entry_to_auth_store``): when a profile
that has no own ``providers.<id>`` block refreshes — via the pool — a rotating
OAuth grant it resolved from the global-root fallback, the rotated chain must
be written back to the global root too. Otherwise root keeps a revoked refresh
token and every other profile reading root's stale grant dies with
``refresh_token_reused`` / ``invalid_grant`` once its access token expires
(issue #48415, the Codex/xAI analog of #43589).

The tests drive the real ``_sync_device_code_entry_to_auth_store`` against
real on-disk auth stores (profile + root under ``tmp_path``) rather than
mocking the save boundary, so they exercise the actual atomic write path.
"""

import json
import threading

import pytest

from agent import credential_pool as CP
from agent.credential_pool import (
    AUTH_TYPE_OAUTH,
    CredentialPool,
    PooledCredential,
    STATUS_EXHAUSTED,
)
from hermes_cli import auth as A


def _write_store(path, store):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store), encoding="utf-8")


def _read_store(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _entry(
    provider: str,
    *,
    id: str,
    access_token: str,
    refresh_token: str,
    source: str = "device_code",
):
    return PooledCredential(
        provider=provider,
        id=id,
        label="cred",
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source=source,
        access_token=access_token,
        refresh_token=refresh_token,
    )

@pytest.fixture
def profile_and_root(tmp_path, monkeypatch):
    """Wire a profile auth store + a distinct global-root auth store on disk.

    The pytest seat belt in ``_write_through_provider_state_to_global_root``
    only refuses the *real* user's ``$HOME/.hermes/auth.json``; a tmp_path
    root is allowed, so point HOME away from the tmp root to keep the guard
    from tripping on these fixtures.
    """
    profile_path = tmp_path / "profiles" / "work" / "auth.json"
    root_path = tmp_path / "root" / "auth.json"

    monkeypatch.setattr(A, "_auth_file_path", lambda: profile_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: root_path)
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-root"))
    return profile_path, root_path


@pytest.mark.parametrize(
    "provider",
    ["openai-codex", "xai-oauth"],
)
def test_pool_refresh_writes_through_to_root_when_profile_reads_root(
    profile_and_root, provider
):
    """A profile reading root's grant must push rotated tokens back to root."""
    profile_path, root_path = profile_and_root
    # Profile has NO own provider block (reads root via fallback).
    _write_store(profile_path, {"version": 1, "providers": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                provider: {
                    "tokens": {
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    }
                }
            },
        },
    )

    pool = CredentialPool(provider, [])
    pool._sync_device_code_entry_to_auth_store(
        _entry(provider, id="e1", access_token="new-access", refresh_token="new-refresh")
    )

    # Profile got the rotated chain (existing behavior).
    profile = _read_store(profile_path)
    assert (
        profile["providers"][provider]["tokens"]["refresh_token"] == "new-refresh"
    )

    # AND the global root no longer holds the revoked refresh token (#48415).
    root = _read_store(root_path)
    assert root["providers"][provider]["tokens"]["access_token"] == "new-access"
    assert root["providers"][provider]["tokens"]["refresh_token"] == "new-refresh"


@pytest.mark.parametrize(
    "provider",
    ["openai-codex", "xai-oauth"],
)
def test_pool_refresh_does_not_touch_root_when_profile_shadows(
    profile_and_root, provider
):
    """A profile that genuinely shadows root must NOT clobber the root grant."""
    profile_path, root_path = profile_and_root
    # Profile has its OWN provider block: it shadows root legitimately.
    _write_store(
        profile_path,
        {
            "version": 1,
            "providers": {
                provider: {
                    "tokens": {
                        "access_token": "profile-old",
                        "refresh_token": "profile-old-refresh",
                    }
                }
            },
        },
    )
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                provider: {
                    "tokens": {
                        "access_token": "root-untouched",
                        "refresh_token": "root-untouched-refresh",
                    }
                }
            },
        },
    )

    pool = CredentialPool(provider, [])
    pool._sync_device_code_entry_to_auth_store(
        _entry(
            provider,
            id="e2",
            access_token="profile-new",
            refresh_token="profile-new-refresh",
        )
    )

    profile = _read_store(profile_path)
    assert (
        profile["providers"][provider]["tokens"]["refresh_token"]
        == "profile-new-refresh"
    )

    # Root keeps its own grant — write-through must not run when the profile
    # owns the block.
    root = _read_store(root_path)
    assert (
        root["providers"][provider]["tokens"]["refresh_token"]
        == "root-untouched-refresh"
    )


def test_write_through_helper_is_noop_in_classic_mode(monkeypatch, tmp_path):
    """When profile == root (classic mode), the helper must be a no-op.

    ``_global_auth_file_path`` returns None in classic mode; the profile save
    already wrote to root, so a second write would be redundant (and the
    helper has nothing to target).
    """
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: None)
    # Must not raise and must not attempt any write.
    CP._write_through_provider_state_to_global_root(
        "openai-codex", {"tokens": {"access_token": "a", "refresh_token": "r"}}
    )


def test_global_write_through_preserves_concurrent_root_update(
    profile_and_root, monkeypatch
):
    """A stale profile write-through must not erase a concurrent root login."""
    _profile_path, root_path = profile_and_root
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                "xai-oauth": {
                    "tokens": {"access_token": "old-xai", "refresh_token": "old-r"}
                }
            },
            "credential_pool": {
                "anthropic": [{"id": "anthropic-existing"}],
                "openrouter": [{"id": "openrouter-existing"}],
            },
        },
    )

    helper_loaded = threading.Event()
    helper_has_target_lock = threading.Event()
    allow_helper_save = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    real_auth_load = A._load_auth_store

    def paused_helper_load(path=None):
        store = real_auth_load(path)
        if threading.current_thread().name == "profile-write-through":
            target_holder = A._auth_lock_holder_for(root_path)
            if getattr(target_holder, "depth", 0) > 0:
                helper_has_target_lock.set()
            helper_loaded.set()
            assert allow_helper_save.wait(timeout=5)
        return store

    monkeypatch.setattr(A, "_load_auth_store", paused_helper_load)
    # The pre-fix implementation imported the loader directly; patch both
    # bindings so reverting the safe helper still exercises the stale ordering.
    monkeypatch.setattr(CP, "_load_auth_store", paused_helper_load)

    def profile_write_through():
        CP._write_through_provider_state_to_global_root(
            "xai-oauth",
            {"tokens": {"access_token": "new-xai", "refresh_token": "new-r"}},
        )

    def concurrent_codex_login():
        writer_started.set()
        with A._auth_store_lock(target_path=root_path):
            store = A._load_auth_store(root_path)
            A._store_provider_state(
                store,
                "openai-codex",
                {"tokens": {"access_token": "codex-a", "refresh_token": "codex-r"}},
                set_active=False,
            )
            pool = store.setdefault("credential_pool", {})
            pool["openai-codex"] = [{"id": "codex-login"}]
            A._save_auth_store(store, target_path=root_path)
        writer_done.set()

    helper = threading.Thread(target=profile_write_through, name="profile-write-through")
    helper.start()
    assert helper_loaded.wait(timeout=5)

    writer = threading.Thread(target=concurrent_codex_login, name="concurrent-login")
    writer.start()
    assert writer_started.wait(timeout=5)
    # A fixed helper already owns the target lock, so the writer will merge
    # after release. A reverted unlocked helper must first let the competing
    # login finish; only then do we release its stale save. This makes the
    # losing pre-fix ordering deterministic rather than scheduler-dependent.
    if not helper_has_target_lock.is_set():
        assert writer_done.wait(timeout=5)
    allow_helper_save.set()
    helper.join(timeout=5)
    writer.join(timeout=5)
    assert not helper.is_alive()
    assert not writer.is_alive()

    root = _read_store(root_path)
    assert root["providers"]["xai-oauth"]["tokens"]["refresh_token"] == "new-r"
    assert root["providers"]["openai-codex"]["tokens"]["refresh_token"] == "codex-r"
    assert root["credential_pool"]["openai-codex"] == [{"id": "codex-login"}]
    assert root["credential_pool"]["anthropic"] == [{"id": "anthropic-existing"}]
    assert root["credential_pool"]["openrouter"] == [{"id": "openrouter-existing"}]


def test_codex_pool_refresh_holds_auth_store_lock_across_post(monkeypatch, tmp_path):
    """The Codex OAuth pool refresh must POST under the cross-process auth lock.

    Codex refresh tokens are single-use. If two Hermes processes both read the
    same on-disk token and both POST it, the loser gets ``refresh_token_reused``.
    Serializing the sync -> refresh POST -> write-back sequence through the
    shared ``_auth_store_lock`` closes that window: a second process blocks on
    the flock and, once inside, adopts the rotated token instead of re-POSTing.

    This asserts the invariant directly — that ``refresh_codex_oauth_pure`` is
    only ever called while the auth-store lock is held — rather than snapshotting
    any token value.
    """
    provider = "openai-codex"
    profile_path = tmp_path / "auth.json"
    monkeypatch.setattr(A, "_auth_file_path", lambda: profile_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-root"))

    lock_held: dict = {"during_post": None}
    real_lock = A._auth_store_lock

    depth = {"n": 0}

    import contextlib

    @contextlib.contextmanager
    def tracking_lock(*args, **kwargs):
        depth["n"] += 1
        try:
            with real_lock(*args, **kwargs):
                yield
        finally:
            depth["n"] -= 1

    monkeypatch.setattr(A, "_auth_store_lock", tracking_lock)
    # credential_pool imported _auth_store_lock by name; patch that binding too.
    monkeypatch.setattr(CP, "_auth_store_lock", tracking_lock)

    def fake_refresh(access_token, refresh_token, **kwargs):
        # The POST to the token endpoint must happen with the lock held.
        lock_held["during_post"] = depth["n"] > 0
        return {
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "last_refresh": "2020-01-02T00:00:00Z",
        }

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", fake_refresh)

    entry = _entry(
        provider,
        id="codex-1",
        access_token="stale-access",
        refresh_token="stale-refresh",
    )
    pool = CredentialPool(provider, [entry])

    refreshed = pool._refresh_entry(entry, force=True)

    assert refreshed is not None
    assert refreshed.access_token == "rotated-access"
    assert refreshed.refresh_token == "rotated-refresh"
    # The invariant: the single-use token POST ran inside the auth-store lock.
    assert lock_held["during_post"] is True


def test_proven_manual_codex_alias_rotates_through_inherited_root(
    profile_and_root, monkeypatch
):
    """A matching manual alias adopts and publishes the inherited grant."""
    profile_path, root_path = profile_and_root
    old_token = "shared-grant-old"
    root_token = old_token
    rotated_token = "shared-grant-rotated"
    _write_store(
        profile_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "legacy-alias",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": old_token,
                        "refresh_token": "old-refresh",
                    }
                ]
            },
        },
    )
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": root_token,
                        "refresh_token": "old-refresh",
                    }
                }
            },
        },
    )

    refresh_args = []

    def fake_refresh(access_token, refresh_token):
        refresh_args.append((access_token, refresh_token))
        return {
            "access_token": rotated_token,
            "refresh_token": "rotated-refresh",
            "last_refresh": "synthetic-now",
        }

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", fake_refresh)
    entry = _entry(
        "openai-codex",
        id="legacy-alias",
        access_token=old_token,
        refresh_token="old-refresh",
        source="manual:device_code",
    )
    pool = CredentialPool("openai-codex", [entry])

    updated = pool._refresh_entry(entry, force=True)

    assert updated is not None
    assert refresh_args == [(root_token, "old-refresh")]
    assert updated.access_token == rotated_token
    assert updated.refresh_token == "rotated-refresh"
    root = _read_store(root_path)
    assert root["providers"]["openai-codex"]["tokens"] == {
        "access_token": rotated_token,
        "refresh_token": "rotated-refresh",
    }
    profile = _read_store(profile_path)
    metadata = profile["credential_pool"]["openai-codex"][0][
        "shared_grant_id"
    ]
    assert root["providers"]["openai-codex"]["shared_grant_id"] == metadata
    assert len(metadata) == 32
    assert "shared-grant-old" not in metadata
    assert "old-refresh" not in metadata


def test_codex_alias_adopts_out_of_band_rotation_by_grant_id(
    profile_and_root, monkeypatch
):
    profile_path, root_path = profile_and_root
    old_access = "g0-access"
    old_refresh = "g0-refresh"
    _write_store(
        profile_path,
        {"version": 1, "providers": {}, "credential_pool": {}},
    )
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": old_access,
                        "refresh_token": old_refresh,
                    }
                }
            },
        },
    )
    entry = _entry(
        "openai-codex",
        id="legacy-alias",
        access_token=old_access,
        refresh_token=old_refresh,
        source="manual:device_code",
    )
    pool = CredentialPool("openai-codex", [entry])
    entry = pool._sync_codex_entry_from_auth_store(entry)
    grant_id = entry.extra["shared_grant_id"]
    root = _read_store(root_path)
    assert root["providers"]["openai-codex"]["shared_grant_id"] == grant_id

    monkeypatch.setattr(
        A,
        "refresh_codex_oauth_pure",
        lambda access, refresh: {
            "access_token": "g1-access",
            "refresh_token": "g1-refresh",
            "last_refresh": "g1",
        },
    )
    entry = pool._refresh_entry(entry, force=True)
    assert entry.access_token == "g1-access"
    assert entry.refresh_token == "g1-refresh"

    root = _read_store(root_path)
    root["providers"]["openai-codex"]["tokens"] = {
        "access_token": "g2-access",
        "refresh_token": "g2-refresh",
    }
    _write_store(root_path, root)

    adopted = pool._sync_codex_entry_from_auth_store(entry)
    assert adopted.access_token == "g2-access"
    assert adopted.refresh_token == "g2-refresh"
    assert adopted.extra["shared_grant_id"] == grant_id
    fresh_alias = _entry(
        "openai-codex",
        id="fresh-alias",
        access_token="g2-access",
        refresh_token="g2-refresh",
        source="manual:device_code",
    )
    fresh_alias = pool._sync_codex_entry_from_auth_store(fresh_alias)
    assert fresh_alias.extra["shared_grant_id"] == grant_id


def test_codex_alias_adopts_root_after_stale_snapshot_without_writing_local_rotation(
    profile_and_root, monkeypatch
):
    profile_path, root_path = profile_and_root
    _write_store(profile_path, {"version": 1, "providers": {}, "credential_pool": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": "snapshot-access",
                        "refresh_token": "snapshot-refresh",
                    }
                }
            },
        },
    )
    entry = _entry(
        "openai-codex",
        id="stale-alias",
        access_token="snapshot-access",
        refresh_token="snapshot-refresh",
        source="manual:device_code",
    )
    pool = CredentialPool("openai-codex", [entry])
    entry = pool._sync_codex_entry_from_auth_store(entry)
    grant_id = entry.extra["shared_grant_id"]
    refresh_calls = []

    def stale_refresh(access, refresh):
        refresh_calls.append((access, refresh))
        root = _read_store(root_path)
        root["providers"]["openai-codex"]["tokens"] = {
            "access_token": "out-of-band-access",
            "refresh_token": "out-of-band-refresh",
        }
        _write_store(root_path, root)
        return {
            "access_token": "discarded-access",
            "refresh_token": "discarded-refresh",
            "last_refresh": "discarded",
        }

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", stale_refresh)
    adopted = pool._refresh_entry(entry, force=True)

    assert refresh_calls == [("snapshot-access", "snapshot-refresh")]
    assert adopted.access_token == "out-of-band-access"
    assert adopted.refresh_token == "out-of-band-refresh"
    assert adopted.extra["shared_grant_id"] == grant_id
    root = _read_store(root_path)
    assert root["providers"]["openai-codex"]["tokens"] == {
        "access_token": "out-of-band-access",
        "refresh_token": "out-of-band-refresh",
    }


def test_profile_manual_device_code_stays_independent_without_local_singleton(
    profile_and_root,
):
    """A manual profile account must not adopt root state by inheritance."""
    profile_path, root_path = profile_and_root
    profile_token = "same-account-grant-profile"
    root_token = "same-account-grant-root"
    _write_store(
        profile_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "profile-account",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": profile_token,
                        "refresh_token": "same-account-grant-profile-refresh",
                    }
                ]
            },
        },
    )
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": root_token,
                        "refresh_token": "same-account-grant-root-refresh",
                    }
                }
            },
        },
    )

    pool = CredentialPool("openai-codex", [])
    entry = _entry(
        "openai-codex",
        id="profile-account",
        access_token=profile_token,
        refresh_token="same-account-grant-profile-refresh",
        source="manual:device_code",
    )

    synced = pool._sync_codex_entry_from_auth_store(entry)

    assert synced is entry
    assert synced.access_token == profile_token
    assert synced.refresh_token == "same-account-grant-profile-refresh"

    pool._sync_device_code_entry_to_auth_store(entry)
    root = _read_store(root_path)
    assert root["providers"]["openai-codex"]["tokens"] == {
        "access_token": root_token,
        "refresh_token": "same-account-grant-root-refresh",
    }


def test_codex_different_shared_grant_ids_stay_isolated(profile_and_root):
    profile_path, root_path = profile_and_root
    _write_store(
        profile_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "independent",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "same-account-current",
                        "refresh_token": "same-account-refresh",
                        "shared_grant_id": "independent-grant",
                    }
                ]
            },
        },
    )
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "shared_grant_id": "root-grant",
                    "tokens": {
                        "access_token": "same-account-current",
                        "refresh_token": "same-account-refresh",
                    },
                }
            },
        },
    )
    entry = _entry(
        "openai-codex",
        id="independent",
        access_token="same-account-current",
        refresh_token="same-account-refresh",
        source="manual:device_code",
    )
    entry.extra["shared_grant_id"] = "independent-grant"
    pool = CredentialPool("openai-codex", [entry])

    synced = pool._sync_codex_entry_from_auth_store(entry)

    assert synced is entry
    assert synced.extra["shared_grant_id"] == "independent-grant"


@pytest.mark.parametrize(
    ("relogin_required", "error_code"),
    [(False, "codex_rate_limited"), (True, "codex_refresh_failed")],
)
def test_shared_codex_auth_errors_use_canonical_refresh_failure_handling(
    profile_and_root, monkeypatch, relogin_required, error_code
):
    profile_path, root_path = profile_and_root
    _write_store(profile_path, {"version": 1, "providers": {}, "credential_pool": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": "failure-access",
                        "refresh_token": "failure-refresh",
                    }
                }
            },
        },
    )
    entry = _entry(
        "openai-codex",
        id="failure-alias",
        access_token="failure-access",
        refresh_token="failure-refresh",
        source="manual:device_code",
    )
    pool = CredentialPool("openai-codex", [entry])
    entry = pool._sync_codex_entry_from_auth_store(entry)

    def fail_refresh(access, refresh):
        raise A.AuthError(
            "synthetic Codex refresh failure",
            provider="openai-codex",
            code=error_code,
            relogin_required=relogin_required,
        )

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", fail_refresh)
    result = pool._refresh_entry(entry, force=True)

    assert result is None
    stored = pool._entries[0]
    if relogin_required:
        assert stored.last_status != STATUS_EXHAUSTED
    else:
        assert stored.last_status == STATUS_EXHAUSTED

import json
import stat

import pytest

from halo_server.account_scope import new_profile_id, valid_profile_id
from halo_server.ring.credentials import (
    FILE_STORE_VERSION,
    CredentialStore,
    CredentialStoreError,
    CredentialSupersededError,
)


def test_store_saves_private_versioned_credentials(tmp_path):
    path = tmp_path / "private" / "credentials.json"
    store = CredentialStore(path)

    store.save_token({"refresh_token": "abc"})
    store.save_account_email("person@example.com")

    credentials = store.load()
    assert credentials.ring_token == {"refresh_token": "abc"}
    assert credentials.account_email == "person@example.com"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == FILE_STORE_VERSION
    assert payload["revision"] == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.glob("*.tmp")) == []


def test_store_atomically_migrates_v1_credentials_on_load(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "revision": 7,
                "ring_token": {"refresh_token": "legacy"},
                "account_email": "person@example.com",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    credentials = CredentialStore(path).load()

    assert credentials.ring_token == {"refresh_token": "legacy"}
    assert credentials.account_email == "person@example.com"
    assert credentials.event_credentials is None
    assert credentials.revision == 8
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": FILE_STORE_VERSION,
        "revision": 8,
        "ring_token": {"refresh_token": "legacy"},
        "account_email": "person@example.com",
        "event_credentials": None,
        "profile_id": credentials.profile_id,
    }


def test_store_rejects_symlink_and_excessive_permissions(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    symlink = tmp_path / "credentials.json"
    symlink.symlink_to(target)

    with pytest.raises(CredentialStoreError, match="regular file"):
        CredentialStore(symlink)

    target.chmod(0o640)
    with pytest.raises(CredentialStoreError, match="permissions"):
        CredentialStore(target)


def test_store_rejects_non_private_directory_and_lock_symlink(tmp_path):
    public_directory = tmp_path / "public"
    public_directory.mkdir(mode=0o755)
    public_directory.chmod(0o755)

    with pytest.raises(CredentialStoreError, match="directory permissions"):
        CredentialStore(public_directory / "credentials.json")

    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    path = private_directory / "credentials.json"
    lock_target = private_directory / "lock-target"
    lock_target.write_text("unchanged", encoding="utf-8")
    lock_target.chmod(0o644)
    lock_path = private_directory / ".credentials.json.lock"
    lock_path.symlink_to(lock_target)
    store = CredentialStore(path)

    with pytest.raises(CredentialStoreError, match="Cannot lock"):
        store.save_token({"refresh_token": "blocked"})

    assert lock_target.read_text(encoding="utf-8") == "unchanged"
    assert stat.S_IMODE(lock_target.stat().st_mode) == 0o644


def test_store_rejects_stale_writer_after_another_instance_revision(tmp_path):
    path = tmp_path / "credentials.json"
    first = CredentialStore(path)
    first.save_token({"refresh_token": "first"})
    stale = CredentialStore(path)
    assert stale.load_token() == {"refresh_token": "first"}

    first.save_token({"refresh_token": "second"})

    with pytest.raises(CredentialSupersededError, match="stale"):
        stale.save_token({"refresh_token": "stale"})

    assert CredentialStore(path).load_token() == {"refresh_token": "second"}


def test_logout_tombstone_prevents_stale_token_recreation(tmp_path):
    path = tmp_path / "credentials.json"
    active = CredentialStore(path)
    active.save_token({"refresh_token": "active"})
    stale = CredentialStore(path)
    stale.load()

    active.clear()

    with pytest.raises(CredentialSupersededError):
        stale.save_token({"refresh_token": "stale"})

    credentials = CredentialStore(path).load()
    assert credentials.ring_token is None
    assert credentials.account_email is None


def test_store_does_not_replace_malformed_credentials(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text('{"version":1,"revision":"bad"}', encoding="utf-8")
    path.chmod(0o600)
    store = CredentialStore(path)

    with pytest.raises(CredentialStoreError, match="invalid format"):
        store.load_token()

    assert json.loads(path.read_text(encoding="utf-8"))["revision"] == "bad"


def test_clear_is_one_atomic_revision(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialStore(path)
    store.save_token({"refresh_token": "active"})
    store.save_account_email("person@example.com")
    store.save_event_credentials({"gcm": {"android_id": "push-device"}})

    store.clear()

    credentials = store.load()
    assert credentials.revision == 4
    assert credentials.ring_token is None
    assert credentials.account_email is None
    assert credentials.event_credentials is None


def test_save_session_is_one_atomic_revision(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")

    store.save_session({"refresh_token": "active"}, "person@example.com")

    credentials = store.load()
    assert credentials.revision == 1
    assert credentials.ring_token == {"refresh_token": "active"}
    assert credentials.account_email == "person@example.com"


def test_assert_revision_detects_an_external_change(tmp_path):
    path = tmp_path / "credentials.json"
    first = CredentialStore(path)
    first.save_token({"refresh_token": "first"})
    revision = first.load().revision
    second = CredentialStore(path)
    second.save_token({"refresh_token": "second"})

    with pytest.raises(CredentialSupersededError, match="stale"):
        first.assert_revision(revision)


def test_mutable_tokens_are_copied_on_save_and_load(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    token = {"refresh_token": "active"}
    store.save_token(token)
    token["refresh_token"] = "changed"

    loaded = store.load_token()
    assert loaded == {"refresh_token": "active"}
    loaded["refresh_token"] = "also changed"
    assert store.load_token() == {"refresh_token": "active"}


def test_event_credentials_are_deep_copied_and_cleared_on_account_change(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    store.save_session({"refresh_token": "active"}, "first@example.com")
    event_credentials = {"gcm": {"android_id": "push-device"}}
    store.save_event_credentials(event_credentials)
    event_credentials["gcm"]["android_id"] = "mutated"

    loaded = store.load_event_credentials()
    assert loaded == {"gcm": {"android_id": "push-device"}}
    loaded["gcm"]["android_id"] = "also-mutated"
    assert store.load_event_credentials() == {"gcm": {"android_id": "push-device"}}

    store.save_session({"refresh_token": "replacement"}, "second@example.com")
    assert store.load_event_credentials() is None


@pytest.mark.parametrize(
    "invalid",
    [None, [], {"invalid": object()}, {"invalid": float("nan")}],
)
def test_store_rejects_invalid_event_credentials_without_changing_revision(tmp_path, invalid):
    store = CredentialStore(tmp_path / "credentials.json")
    store.save_session({"refresh_token": "active"}, "person@example.com")
    before = store.load()

    with pytest.raises(CredentialStoreError, match="JSON object"):
        store.save_event_credentials(invalid)

    after = store.load()
    assert after == before


@pytest.mark.parametrize("event_credentials", [[], {"gcm": {"value": float("nan")}}])
def test_store_rejects_invalid_v2_event_credentials_without_replacing_file(
    tmp_path,
    event_credentials,
):
    path = tmp_path / "credentials.json"
    payload = {
        "version": FILE_STORE_VERSION,
        "revision": 2,
        "ring_token": {"refresh_token": "active"},
        "account_email": "person@example.com",
        "event_credentials": event_credentials,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    original = path.read_bytes()

    with pytest.raises(CredentialStoreError, match="invalid format"):
        CredentialStore(path).load()

    assert path.read_bytes() == original


def test_v2_migration_preserves_fcm_and_allocates_profile_once(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "revision": 8,
                "ring_token": {"refresh_token": "old"},
                "account_email": "person@example.com",
                "event_credentials": {"gcm": {"id": "fcm"}},
            }
        )
    )
    path.chmod(0o600)
    first = CredentialStore(path).load()
    second = CredentialStore(path).load()
    assert first == second
    assert first.revision == 9
    assert valid_profile_id(first.profile_id)
    assert first.event_credentials == {"gcm": {"id": "fcm"}}


def test_token_maintenance_keeps_profile_but_explicit_reassociation_clears_fcm(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    profile = new_profile_id()
    store.save_session({"refresh_token": "initial"}, "person@example.com", profile_id=profile)
    store.save_event_credentials({"id": "old-push"})
    store.save_session({"refresh_token": "renewed"}, "person@example.com", profile_id=profile)
    assert store.load().profile_id == profile
    assert store.load().event_credentials == {"id": "old-push"}
    replacement = new_profile_id()
    store.save_session({"refresh_token": "new-login"}, "person@example.com", profile_id=replacement)
    assert store.load().profile_id == replacement
    assert store.load().event_credentials is None
    store.clear()
    assert store.load().profile_id is None


@pytest.mark.parametrize("profile", [None, "email@example.com", "../profile", "0" * 32])
def test_v3_invalid_profile_is_not_silently_replaced(tmp_path, profile):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "revision": 8,
                "ring_token": {"refresh_token": "old"},
                "account_email": "person@example.com",
                "event_credentials": None,
                "profile_id": profile,
            }
        )
    )
    path.chmod(0o600)
    original = path.read_bytes()
    with pytest.raises(CredentialStoreError, match="invalid format"):
        CredentialStore(path).load()
    assert path.read_bytes() == original


def test_failed_profile_migration_does_not_publish_identity_or_overwrite_old_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "revision": 4,
                "ring_token": {"refresh_token": "old"},
                "account_email": "person@example.com",
                "event_credentials": None,
            }
        )
    )
    path.chmod(0o600)
    before = path.read_bytes()

    def fail_replace(*_args):
        raise OSError("synthetic write failure")

    monkeypatch.setattr("halo_server.ring.credentials.os.replace", fail_replace)
    with pytest.raises(CredentialStoreError, match="Cannot write"):
        CredentialStore(path).load()
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_unknown_profile_field_cannot_promote_identity_without_token(tmp_path, version):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "version": version,
                "revision": 1,
                "ring_token": None,
                "account_email": None,
                "event_credentials": None,
                "profile_id": new_profile_id(),
            }
        )
    )
    path.chmod(0o600)
    assert CredentialStore(path).load().profile_id is None
    assert CredentialStore(path).load().profile_id is None

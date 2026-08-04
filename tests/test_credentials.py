import json
import stat

import pytest

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

    store.clear()

    credentials = store.load()
    assert credentials.revision == 3
    assert credentials.ring_token is None
    assert credentials.account_email is None


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

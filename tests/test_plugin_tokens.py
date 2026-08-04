import json
import stat

import pytest

from halo_server.api.tokens import (
    MAX_PLUGIN_IDENTITIES,
    PluginTokenStore,
    PluginTokenStoreError,
)


def test_plugin_token_is_returned_once_and_only_hash_is_stored(tmp_path):
    store = PluginTokenStore(tmp_path / "integrations")

    issued = store.create("Scrypted Home", now=100)

    assert issued.token.startswith(f"halo1.{issued.identity.id}.")
    assert store.verify(issued.token) == issued.identity
    record_path = store.directory / f"{issued.identity.id}.json"
    record = record_path.read_text(encoding="utf-8")
    assert issued.token not in record
    assert issued.token.rsplit(".", 1)[1] not in record
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700


def test_plugin_token_rejects_tampering_and_revocation_is_immediate(tmp_path):
    store = PluginTokenStore(tmp_path / "integrations")
    issued = store.create("Scrypted")
    prefix, client_id, secret = issued.token.split(".")
    replacement = "A" if secret[-1] != "A" else "B"

    assert store.verify(f"{prefix}.{client_id}.{secret[:-1]}{replacement}") is None
    assert store.revoke(client_id) is True
    assert store.verify(issued.token) is None
    assert store.revoke(client_id) is False


def test_plugin_identity_listing_never_contains_token_hash(tmp_path):
    store = PluginTokenStore(tmp_path / "integrations")
    issued = store.create("Scrypted", now=100)

    listed = store.list_identities()

    assert listed == [issued.identity]
    assert listed[0].as_dict() == {
        "id": issued.identity.id,
        "label": "Scrypted",
        "created_at": 100,
    }


@pytest.mark.parametrize("label", ["", "x" * 81, "bad\nlabel"])
def test_plugin_label_is_bounded_and_printable(tmp_path, label):
    store = PluginTokenStore(tmp_path / "integrations")

    with pytest.raises(ValueError, match="printable"):
        store.create(label)


def test_plugin_store_rejects_symlink_record(tmp_path):
    store = PluginTokenStore(tmp_path / "integrations")
    issued = store.create("Scrypted")
    path = store.directory / f"{issued.identity.id}.json"
    path.unlink()
    target = tmp_path / "target"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    path.symlink_to(target)

    with pytest.raises(PluginTokenStoreError, match="unsafe"):
        store.verify(issued.token)

    assert json.loads(target.read_text(encoding="utf-8")) == {}


def test_plugin_store_rejects_malformed_record(tmp_path):
    store = PluginTokenStore(tmp_path / "integrations")
    issued = store.create("Scrypted")
    path = store.directory / f"{issued.identity.id}.json"
    path.write_text('{"version":1}', encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(PluginTokenStoreError, match="invalid"):
        store.verify(issued.token)


def test_plugin_revoke_validates_client_id(tmp_path):
    store = PluginTokenStore(tmp_path / "integrations")

    with pytest.raises(ValueError, match="invalid"):
        store.revoke("../../credentials")


def test_plugin_store_caps_active_identities(tmp_path):
    store = PluginTokenStore(tmp_path / "integrations")
    for number in range(MAX_PLUGIN_IDENTITIES):
        store.create(f"Scrypted {number}")

    with pytest.raises(PluginTokenStoreError, match="limit"):
        store.create("One too many")

    assert len(store.list_identities()) == MAX_PLUGIN_IDENTITIES

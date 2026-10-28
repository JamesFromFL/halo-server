from dataclasses import FrozenInstanceError

import pytest

from halo_server.account_scope import AccountScope, new_profile_id, valid_profile_id


def test_profile_identity_is_opaque_unique_and_scope_immutable():
    profile = new_profile_id()
    assert valid_profile_id(profile)
    assert new_profile_id() != profile
    scope = AccountScope(profile, 1)
    with pytest.raises(FrozenInstanceError):
        scope.generation = 2


@pytest.mark.parametrize("profile", [None, 1, "", "person@example.com", "../profile", "0" * 32])
def test_scope_rejects_noncanonical_profile_identity(profile):
    assert not valid_profile_id(profile)
    with pytest.raises(ValueError, match="profile"):
        AccountScope(profile, 1)


@pytest.mark.parametrize("generation", [None, True, 0, -1, 1.0])
def test_scope_rejects_invalid_runtime_generation(generation):
    with pytest.raises(ValueError, match="generation"):
        AccountScope(new_profile_id(), generation)

"""Real crypto/private stores and race regressions for browser authentication."""

import asyncio
import os
import threading
from dataclasses import replace

import pytest

from halo_server.paths import ServerPaths
from halo_server.web.auth import (
    ABSOLUTE_SECONDS,
    IDLE_SECONDS,
    Authentication,
    AuthRecord,
    AuthStore,
    AuthStoreError,
    RateLimiter,
    issue_setup,
)
from halo_server.web.defaults import OWNER_USERNAME
from halo_server.web.errors import WebError
from halo_server.web.settings import (
    SettingsStore,
    WebSettingsError,
    access_info,
    configure,
    import_certificate,
    tls_context,
    web_directory,
)

PASSWORD = "a clear local test password"


@pytest.fixture
def paths(tmp_path):
    result = ServerPaths(tmp_path / "state", tmp_path / "run")
    result.prepare()
    return result


async def enrolled(paths, **kwargs):
    setup = issue_setup(paths)
    auth = Authentication(paths, **kwargs)
    await auth.refresh()
    anonymous, _ = auth.create()
    session, token = await auth.setup(
        anonymous, setup["setup_code"], PASSWORD, "127.0.0.1", username=OWNER_USERNAME
    )
    return auth, session, token


def test_tls_enrollment_is_private_explicit_and_persistent(paths):
    with pytest.raises(WebSettingsError):
        configure(paths, host="0.0.0.0", canonical_host="halo.local")
    initial = configure(paths)
    settings = SettingsStore(paths).load()
    bundle = web_directory(paths) / settings.certificate_file
    assert os.stat(bundle).st_mode & 0o777 == 0o600
    assert initial["certificate_fingerprint"] == access_info(paths)["certificate_fingerprint"]
    assert tls_context(paths, settings).minimum_version.name == "TLSv1_2"
    with pytest.raises(WebSettingsError, match="hostname"):
        configure(paths, host="0.0.0.0", canonical_host="halo.local", allow_lan=True)
    assert SettingsStore(paths).load() == settings
    renewed = configure(
        paths, host="0.0.0.0", canonical_host="halo.local", allow_lan=True, renew_certificate=True
    )
    assert renewed["certificate_fingerprint"] != initial["certificate_fingerprint"]
    assert bundle.exists()  # Known prior private identity is retained for recovery.
    assert tls_context(paths, SettingsStore(paths).load())


def test_tls_import_rejects_other_hostname_without_changing_selection(paths, tmp_path):
    configure(paths)
    before = SettingsStore(paths).load()
    other = ServerPaths(tmp_path / "other", tmp_path / "other-run")
    other.prepare()
    configure(other, canonical_host="another.local")
    foreign = web_directory(other) / SettingsStore(other).load().certificate_file
    with pytest.raises(WebSettingsError):
        import_certificate(paths, foreign, foreign)
    assert SettingsStore(paths).load() == before


def test_auth_store_rejects_symlink_and_dangerous_hash_parameters(paths, tmp_path):
    secret = tmp_path / "not-auth"
    secret.write_text("unchanged")
    web_directory(paths).mkdir()
    (web_directory(paths) / "authentication.json").symlink_to(secret)
    with pytest.raises(AuthStoreError):
        issue_setup(paths)
    assert secret.read_text() == "unchanged"
    with pytest.raises(AuthStoreError):
        AuthRecord(verifier="$argon2id$v=19$m=999999999,t=3,p=1$" + "A" * 22 + "$" + "A" * 43)


@pytest.mark.asyncio
async def test_setup_recovery_and_rotation_leave_ring_and_pairings_intact(paths):
    paths.credentials_file.write_text("Ring token fixture")
    paths.homekit_pairing_directory.mkdir(parents=True)
    pairing = paths.homekit_pairing_directory / "pairing-fixture"
    pairing.write_text("Apple pairing fixture")
    auth, session, token = await enrolled(paths)
    record = AuthStore(paths).load()
    assert record.verifier.startswith("$argon2id$")
    assert record.setup_digest is None and PASSWORD not in record.verifier
    assert auth.get(token, authenticated=True) is session
    with pytest.raises(AuthStoreError):
        issue_setup(paths)
    anonymous, _ = auth.create()
    second, second_token = await auth.authenticate(
        anonymous, PASSWORD, "127.0.0.2", username=OWNER_USERNAME
    )
    assert second.authenticated and second_token != token
    issue_setup(paths, reset=True)
    await auth.refresh()
    for value in (token, second_token):
        with pytest.raises(WebError, match="Sign in"):
            auth.get(value, authenticated=True)
    assert paths.credentials_file.read_text() == "Ring token fixture"
    assert pairing.read_text() == "Apple pairing fixture"
    await auth.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["setup", "password"])
async def test_local_reset_after_password_save_cannot_mint_session(paths, monkeypatch, action):
    if action == "setup":
        code = issue_setup(paths)["setup_code"]
        auth = Authentication(paths)
        await auth.refresh()
        session, _ = auth.create()
    else:
        auth, session, _ = await enrolled(paths)
    original = auth.store.save

    def reset_after_save(*args, **kwargs):
        saved = original(*args, **kwargs)
        issue_setup(paths, reset=True)
        return saved

    monkeypatch.setattr(auth.store, "save", reset_after_save)
    with pytest.raises(WebError) as error:
        if action == "setup":
            await auth.setup(session, code, PASSWORD, "peer", username=OWNER_USERNAME)
        else:
            await auth.change_password(session, PASSWORD, "a different local password", "peer")
    assert error.value.code == "authentication_changed"
    assert not any(row.authenticated for row in auth.sessions.values())
    await auth.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["setup", "password"])
async def test_prewrite_background_read_cannot_reject_completed_password_save(
    paths, monkeypatch, action
):
    if action == "setup":
        code = issue_setup(paths)["setup_code"]
        auth = Authentication(paths)
        await auth.refresh()
        session, old_token = auth.create()
    else:
        auth, session, old_token = await enrolled(paths)
    original_load, original_save, original_refresh = (
        auth.store.load,
        auth.store.save,
        auth.refresh,
    )
    read_started, read_release, saved = threading.Event(), threading.Event(), threading.Event()
    postwrite_refresh = asyncio.Event()

    def captured_read():
        record = original_load()
        read_started.set()
        assert read_release.wait(5)
        return record

    def save(*args, **kwargs):
        record = original_save(*args, **kwargs)
        saved.set()
        return record

    async def refresh():
        if saved.is_set():
            postwrite_refresh.set()
        return await original_refresh()

    monkeypatch.setattr(auth.store, "load", captured_read)
    monkeypatch.setattr(auth.store, "save", save)
    monkeypatch.setattr(auth, "refresh", refresh)
    background = asyncio.create_task(auth.refresh())
    writing = None
    try:
        assert await asyncio.to_thread(read_started.wait, 2)
        new_password = "a different synthetic owner password"
        operation = (
            auth.setup(session, code, new_password, "peer", username=OWNER_USERNAME)
            if action == "setup"
            else auth.change_password(session, PASSWORD, new_password, "peer")
        )
        writing = asyncio.create_task(operation)
        await asyncio.wait_for(postwrite_refresh.wait(), 2)
        read_release.set()
        owner, token = await writing
        await background
        assert owner.authenticated and auth.get(token, authenticated=True) is owner
        assert auth.get(old_token) is None
        assert auth.record == AuthStore(paths).load()
        anonymous, _ = auth.create()
        verified, _ = await auth.authenticate(
            anonymous, new_password, "verification", username=OWNER_USERNAME
        )
        assert verified.authenticated
    finally:
        read_release.set()
        await asyncio.gather(background, *([writing] if writing else []), return_exceptions=True)
        await auth.close()


@pytest.mark.asyncio
async def test_refresh_does_not_accept_regressed_auth_revision(paths, monkeypatch):
    auth, _, _ = await enrolled(paths)
    old = replace(auth.record, revision=auth.record.revision - 1)
    monkeypatch.setattr(auth.store, "load", lambda: old)
    with pytest.raises(AuthStoreError, match="backwards"):
        await auth.refresh()
    assert not auth.sessions
    await auth.close()


@pytest.mark.asyncio
async def test_session_idle_absolute_expiry_csrf_and_recent_reauth(paths):
    now = [100.0]
    auth, session, token = await enrolled(paths, clock=lambda: now[0])
    with pytest.raises(WebError) as error:
        auth.csrf(session, "wrong")
    assert error.value.code == "csrf_rejected"
    auth.csrf(session, session.csrf)
    with pytest.raises(WebError) as error:
        auth.csrf(session, "invalid\N{LATIN SMALL LETTER E WITH ACUTE}")
    assert error.value.code == "csrf_rejected"
    now[0] += 301
    with pytest.raises(WebError) as error:
        auth.recent(session)
    assert error.value.code == "recent_auth_required"
    now[0] = session.touched + IDLE_SECONDS
    assert auth.get(token, touch=False) is None
    fresh, value = auth.create(authenticated=True)
    fresh.touched = now[0] + ABSOLUTE_SECONDS
    now[0] += ABSOLUTE_SECONDS
    assert auth.get(value) is None
    await auth.close()


@pytest.mark.asyncio
async def test_cancelled_kdf_keeps_capacity_until_thread_finishes(paths):
    auth = Authentication(paths)
    gate = threading.Event()
    entered = threading.Event()

    def blocking():
        entered.set()
        gate.wait(5)

    first = asyncio.create_task(auth._work(blocking))
    second = asyncio.create_task(auth._work(blocking))
    await asyncio.to_thread(entered.wait, 2)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(WebError) as error:
        await auth._work(lambda: None)
    assert error.value.code == "busy"
    gate.set()
    await second
    await auth.close()
    assert not auth.tasks


def test_login_rate_limit_is_bounded_and_expires():
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    for _ in range(5):
        limiter.admit("peer")
    with pytest.raises(WebError) as error:
        limiter.admit("peer")
    assert error.value.status == 429
    now[0] = 31
    limiter.admit("peer")


@pytest.mark.asyncio
async def test_cancelled_auth_reads_coalesce_and_retirement_keeps_real_worker(paths, monkeypatch):
    auth = Authentication(paths)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def delayed_load():
        calls.append(True)
        entered.set()
        release.wait(5)
        return AuthRecord()

    monkeypatch.setattr(auth.store, "load", delayed_load)
    for _ in range(10):
        request = asyncio.create_task(auth.refresh())
        await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    stopping = asyncio.create_task(auth.close())
    await asyncio.sleep(0)
    assert not stopping.done() and calls == [True]
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    release.set()
    await auth.close()
    assert not auth.tasks


@pytest.mark.asyncio
async def test_wrong_username_verifies_password_and_never_creates_owner_session(paths, monkeypatch):
    from halo_server.web import auth as module

    auth, session, _ = await enrolled(paths)
    auth.revoke(session.key)
    anonymous, _ = auth.create()
    before = AuthStore(paths).load()
    verified = []
    original = module._verify

    def verify(value, verifier):
        verified.append(value)
        return original(value, verifier)

    monkeypatch.setattr(module, "_verify", verify)
    names = ("visitor", "Admin", " admin", "admin ", "admіn", None, {}, "a" * 65)
    for index, username in enumerate(names):
        with pytest.raises(WebError) as error:
            await auth.authenticate(anonymous, PASSWORD, str(index), username=username)
        assert error.value.code == "authentication_failed" and error.value.status == 401
        assert not any(row.authenticated for row in auth.sessions.values())
    assert verified == [PASSWORD.encode()] * len(names)
    with pytest.raises(WebError) as error:
        await auth.authenticate(
            anonymous, "a different long password", "wrong-password", username=OWNER_USERNAME
        )
    assert error.value.code == "authentication_failed" and error.value.status == 401
    assert AuthStore(paths).load() == before
    assert set(before.as_dict()) == {
        "version",
        "revision",
        "verifier",
        "setup_digest",
        "setup_expires_at",
    }
    assert before.version == 1
    authenticated, _ = await auth.authenticate(
        anonymous, PASSWORD, "success", username=OWNER_USERNAME
    )
    assert authenticated.authenticated
    await auth.close()


@pytest.mark.asyncio
async def test_bad_username_attempts_share_login_rate_limit(paths):
    auth, session, _ = await enrolled(paths)
    auth.revoke(session.key)
    anonymous, _ = auth.create()
    for _ in range(5):
        with pytest.raises(WebError) as error:
            await auth.authenticate(anonymous, PASSWORD, "peer", username="visitor")
        assert error.value.code == "authentication_failed"
    with pytest.raises(WebError) as error:
        await auth.authenticate(anonymous, PASSWORD, "peer", username=OWNER_USERNAME)
    assert error.value.code == "rate_limited" and error.value.status == 429
    assert not any(row.authenticated for row in auth.sessions.values())
    await auth.close()


@pytest.mark.asyncio
async def test_wrong_setup_username_does_not_consume_enrollment_code(paths):
    code = issue_setup(paths)["setup_code"]
    auth = Authentication(paths)
    await auth.refresh()
    anonymous, _ = auth.create()
    before = AuthStore(paths).load()
    with pytest.raises(WebError) as error:
        await auth.setup(anonymous, code, PASSWORD, "peer", username="visitor")
    assert error.value.code == "setup_rejected"
    assert AuthStore(paths).load() == before
    assert not any(row.authenticated for row in auth.sessions.values())
    owner, _ = await auth.setup(anonymous, code, PASSWORD, "peer", username=OWNER_USERNAME)
    assert owner.authenticated
    assert AuthStore(paths).load().setup_digest is None
    await auth.close()


@pytest.mark.asyncio
async def test_owner_password_rotation_revokes_sessions_and_preserves_v1_record(paths):
    auth, session, token = await enrolled(paths)
    before = AuthStore(paths).load()
    new_password = "a rotated private owner password"
    owner, new_token = await auth.change_password(session, PASSWORD, new_password, "rotate")
    assert owner.authenticated and new_token != token
    assert auth.get(token) is None
    record = AuthStore(paths).load()
    assert record.version == 1 and record.revision > before.revision
    assert set(record.as_dict()) == set(before.as_dict())
    anonymous, _ = auth.create()
    with pytest.raises(WebError) as error:
        await auth.authenticate(anonymous, PASSWORD, "old", username=OWNER_USERNAME)
    assert error.value.code == "authentication_failed"
    signed_in, _ = await auth.authenticate(anonymous, new_password, "new", username=OWNER_USERNAME)
    assert signed_in.authenticated
    await auth.close()

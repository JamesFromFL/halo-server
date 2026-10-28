"""Interop failures are observable without recording peer or transport secrets."""

from types import SimpleNamespace

import pytest

from halo_server.homekit.bridge import HomeKitBridge


def bridge():
    value = object.__new__(HomeKitBridge)
    value._broker = SimpleNamespace(boot_id="current-boot")
    value._diagnostic_last = {}
    return value


def notice(stage="video-prepare", code="invalid-video-prepare", boot="current-boot"):
    return {
        "kind": "error",
        "boot_id": boot,
        "payload": {"code": "interop-" + stage, "message": code},
    }


@pytest.mark.asyncio
async def test_only_current_allowlisted_metadata_reaches_rate_limited_log(caplog):
    value = bridge()
    await value._notice(notice())
    for _ in range(100):
        await value._notice(notice())
    await value._notice(notice("video-start", "unsupported-audio-format", "retired-boot"))
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == (
        "HomeKit operation video-prepare failed: invalid-video-prepare"
    )
    assert len(value._diagnostic_last) == 1


@pytest.mark.asyncio
async def test_untrusted_diagnostic_text_and_identifiers_are_never_logged(caplog):
    value = bridge()
    for secret in ("031-45-154", "X-HM://0023ISYWYABCD", "srtp_key=private", "raw-exception"):
        await value._notice(notice(code=secret))
        await value._notice(notice(stage=secret))
    await value._notice(
        {
            "kind": "error",
            "boot_id": "current-boot",
            "payload": {"code": "other-code", "message": "private exception"},
        }
    )
    assert not caplog.records and not value._diagnostic_last


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ("snapshot-source-failed", "snapshot-timeout"))
async def test_fixed_snapshot_reasons_preserve_failure_stage_without_peer_data(caplog, code):
    await bridge()._notice(notice("snapshot", code))
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == f"HomeKit operation snapshot failed: {code}"

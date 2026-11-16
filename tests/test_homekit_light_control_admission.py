"""Bounded HomeKit light scenes preserve siren limits and command uncertainty."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from test_homekit_controller import POLICY, configured

from halo_server.homekit import controller as native
from halo_server.homekit.models import HomeKitOperationError


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_light_scene_capacity_is_independent_from_unchanged_siren_capacity():
    controller, backend, boot, scopes = await configured(7)
    assert native.MAX_LIGHT_CONTROLS == 64 and native.MAX_CONTROLS == 2
    release = asyncio.Event()
    entered = []

    async def write(scope, control, on, *, admission):
        entered.append((control, admission))
        await release.wait()
        assert admission()
        backend.controls.append((control, on))

    backend.set_control = write
    tasks = [
        asyncio.create_task(controller.set_control(boot, scopes[i % 7], "light", True))
        for i in range(64)
    ]
    tasks.extend(
        asyncio.create_task(controller.set_control(boot, scopes[i], "siren", False))
        for i in range(2)
    )
    try:
        await until(lambda: len(entered) == 66)
        assert controller.pending_resources == 66 and backend.controls == []
        for control in ("light", "siren"):
            with pytest.raises(HomeKitOperationError) as error:
                await controller.set_control(boot, scopes[-1], control, False)
            assert error.value.code == "busy" and not error.value.possibly_sent
        assert len(entered) == 66
        release.set()
        await asyncio.gather(*tasks)
        assert backend.controls.count(("light", True)) == 64
        assert backend.controls.count(("siren", False)) == 2
        assert controller.pending_resources == 0
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["policy", "boot"])
async def test_pending_light_scene_is_revoked_without_late_setter(retirement):
    controller, backend, boot, (scope,) = await configured()
    entered = []

    async def write(scope, control, on, *, admission):
        entered.append(admission)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert not admission()
            raise

    backend.set_control = write
    tasks = [
        asyncio.create_task(controller.set_control(boot, scope, "light", True)) for _ in range(7)
    ]
    try:
        await until(lambda: len(entered) == 7)
        if retirement == "policy":
            await controller.update_policy(boot, scope, replace(POLICY, controls=False))
        else:
            await controller.attach(str(uuid4()), (scope,))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert all(not admission() for admission in entered)
        assert not backend.controls and controller.pending_resources == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control", "code", "reported_sent", "expected_sent"),
    [
        ("light", "lighting_timeout", False, False),
        ("light", "lighting_busy", False, False),
        ("light", "lighting_unavailable", False, False),
        ("light", "control_superseded", False, False),
        ("light", "lighting_timeout", True, True),
        ("light", "lighting_busy", True, True),
        ("light", "lighting_unavailable", True, True),
        ("light", "control_superseded", True, True),
        ("light", "control_timeout", False, True),
        ("light", "control_failed", False, True),
        ("light", "remote_unknown", False, True),
        ("siren", "lighting_timeout", False, True),
    ],
)
async def test_only_definitive_presend_light_errors_override_admission_marker(
    control, code, reported_sent, expected_sent
):
    controller, backend, boot, (scope,) = await configured()
    calls = []

    async def fail(scope, control, on, *, admission):
        calls.append(control)
        assert admission()  # A read-only authority check already marks uncertainty.
        raise HomeKitOperationError(code, possibly_sent=reported_sent)

    backend.set_control = fail
    try:
        with pytest.raises(HomeKitOperationError) as error:
            await controller.set_control(boot, scope, control, True)
        assert error.value.code == code and error.value.possibly_sent is expected_sent
        assert calls == [control] and not backend.controls
        assert controller.pending_resources == 0
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_generic_light_backend_failure_after_admission_remains_uncertain():
    controller, backend, boot, (scope,) = await configured()

    async def fail(scope, control, on, *, admission):
        assert admission()
        backend.controls.append((control, on))
        raise RuntimeError("sensitive backend diagnostic")

    backend.set_control = fail
    try:
        with pytest.raises(HomeKitOperationError) as error:
            await controller.set_control(boot, scope, "light", True)
        assert error.value.code == "backend_failed" and error.value.possibly_sent
        assert "sensitive" not in str(error.value)
        assert backend.controls == [("light", True)] and controller.pending_resources == 0
    finally:
        await controller.close()

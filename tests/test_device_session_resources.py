"""Hub resources follow the Ring account even before daemon callbacks run."""

from types import SimpleNamespace

import pytest

from halo_server.ring.client import RingClient, SessionSupersededError


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_account_auth_closes_after_all_registered_integrations(fail):
    order = []

    class Resource:
        async def close(self):
            order.append("provider")
            if fail:
                raise RuntimeError("fixture failure")

    async def close_auth():
        order.append("auth")

    client = RingClient()
    client._authenticated = True
    client._ring = SimpleNamespace(auth=SimpleNamespace(async_close=close_auth))
    client.register_session_resource(Resource())
    if fail:
        with pytest.raises(ExceptionGroup):
            await client._async_close()
    else:
        await client._async_close()
    assert order == ["provider", "auth"]
    assert not client._session_resources and not client.is_authenticated
    with pytest.raises(SessionSupersededError):
        client.register_session_resource(Resource())


@pytest.mark.asyncio
async def test_explicit_monitor_retirement_unregisters_before_account_close():
    class Resource:
        async def close(self):
            pytest.fail("Already unregistered")

    async def close_auth():
        pass

    client = RingClient()
    client._authenticated = True
    client._ring = SimpleNamespace(auth=SimpleNamespace(async_close=close_auth))
    resource = Resource()
    client.register_session_resource(resource)
    client.unregister_session_resource(resource)
    await client._async_close()

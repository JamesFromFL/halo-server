"""Typed same-origin management facade; there is no arbitrary RPC/media endpoint."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from aiohttp import web

from halo_server.owner_client import AdminClient, AdminClientError
from halo_server.private_json import PrivateJsonError
from halo_server.release_activation import ActivationError
from halo_server.release_execution import managed_context, release_status
from halo_server.web.auth import COOKIE, Authentication
from halo_server.web.errors import WebError, boolean, fields, integer, text
from halo_server.web.operations import Operations, RpcPool, operation_id
from halo_server.web.ring_login import RingLogin
from halo_server.web.settings import access_info
from halo_server.web.state import StateHub

PREFIX = "/api/web/v1"
CONSOLE = web.AppKey("halo_web_console", object)
SESSION = web.RequestKey("halo_web_session", object)
DRAIN_SECONDS = 20.0
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def success(result, *, status=200):
    return web.json_response({"ok": True, "result": result}, status=status)


def failure(error):
    return web.json_response(
        {
            "ok": False,
            "error": {
                "code": error.code,
                "message": str(error),
                "possibly_sent": error.possibly_sent,
            },
        },
        status=error.status,
    )


def _json_pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError
        result[name] = value
    return result


async def body(request, required=(), optional=()):
    if request.content_type != "application/json":
        raise WebError("bad_request", "This endpoint accepts JSON only", status=415)
    try:
        async with asyncio.timeout(5):
            data = await request.read()
        value = json.loads(
            data,
            object_pairs_hook=_json_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeDecodeError, RecursionError, TimeoutError):
        raise WebError("bad_request", "Request JSON is invalid") from None
    return fields(value, required, optional)


class WebConsole:
    def __init__(self, paths, settings, *, client=None, restart=None):
        self.paths, self.settings = paths, settings
        self.client = client or AdminClient(paths.admin_socket)
        self.auth = Authentication(paths)
        self.pool = RpcPool()
        self.state = StateHub(self.client, self.pool)
        self.state.web_status = self.web_status
        self.operations = Operations(paths)
        self.ring = RingLogin(self.client, self.pool, self.state)
        self.auth.on_revoke = self.ring.clear
        self.state.on_epoch = self.invalidate
        self.state.on_disconnect = self.invalidate
        self.restart = restart
        self.pairing = None
        self.pairing_at = 0.0
        self.pairing_key = None
        self.pairing_task = None
        self.draining = False
        self.active_requests = 0
        self.active_mutations = 0
        self.started = False
        self.applied_access = None

    @property
    def pending_operations(self):
        def pending(tasks):
            return sum(not task.done() for task in tasks)

        # Workers may be nested inside a tracked mutation. Count conservatively
        # without treating reserved resources as active streams. HTTP admission
        # also covers requests still parsing input or awaiting their first RPC.
        owned = max(self.operations.pending, pending(self.operations.workers))
        owned += pending(self.auth.tasks)
        owned += int(self.ring.task is not None and not self.ring.task.done())
        if self.restart is not None:
            owned += pending(getattr(self.restart, "tasks", {}).values())
        return max(self.active_mutations, owned)

    def web_status(self):
        return {"draining": self.draining, "pending_operations": self.pending_operations}

    async def start(self):
        if self.started:
            return
        self.applied_access = await asyncio.to_thread(access_info, self.paths, self.settings)
        await self.auth.refresh()
        await self.operations.start()
        if self.restart is None:
            release = managed_context()
            if release is not None:
                from halo_server.web_restart import ManagedDaemonRestart

                self.restart = ManagedDaemonRestart(
                    release.root.parent.parent, self.paths, self.client
                )
        self.started = True

    def invalidate(self, _epoch=None):
        self.pairing = self.pairing_key = None
        self.ring.clear()

    async def close(self):
        self.draining = True
        self.operations.accepting = False
        await self.ring.close()
        await self.operations.close()
        if self.restart is not None:
            await self.restart.close()
        await self.state.close()
        if self.pairing_task and not self.pairing_task.done():
            await asyncio.wait((self.pairing_task,))
        await self.auth.close()

    async def drain(self):
        self.draining = True
        self.operations.accepting = False
        try:
            async with asyncio.timeout(DRAIN_SECONDS):
                await self.ring.close()
                await self.operations.close()
                if self.restart is not None:
                    await self.restart.close()
                while self.pending_operations:
                    await asyncio.sleep(0.025)
        except TimeoutError:
            # Pausing admission is deliberate. A timeout is not permission to
            # cancel a possibly-sent write or automatically restart its owner.
            pass
        pending = self.pending_operations
        return {"drained": pending == 0, "pending_operations": pending}

    async def session(self, request):
        session = request.get(SESSION)
        response = None
        if session is None:
            session, token = self.auth.create()
            response = success(self.auth.view(session))
            _cookie(response, token)
        return response if response is not None else success(self.auth.view(session))

    async def auth_action(self, request):
        action = request.match_info["action"]
        session = request[SESSION]
        if action == "logout":
            await body(request)
            self.auth.revoke(session.key)
            response = success({"authenticated": False})
            response.del_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="Strict")
            return response
        if self.draining:
            raise WebError("service_stopping", "Web administration is draining", status=503)
        if action == "setup":
            value = await body(request, ("setup_code", "password"))
            session, token = await self.auth.setup(
                session, value["setup_code"], value["password"], request.remote
            )
        elif action in {"login", "reauth"}:
            value = await body(request, ("password",))
            session, token = await self.auth.authenticate(
                session, value["password"], request.remote
            )
        elif action == "password":
            value = await body(request, ("current_password", "password"))
            session, token = await self.auth.change_password(
                session, value["current_password"], value["password"], request.remote
            )
        else:
            raise WebError("not_found", "Endpoint was not found", status=404)
        response = success(self.auth.view(session))
        _cookie(response, token)
        return response

    async def sessions(self, request):
        return success(self.auth.list_sessions(request[SESSION]))

    async def revoke(self, request):
        self.auth.recent(request[SESSION])
        identifier = text(request.match_info["identifier"], maximum=36)
        target = next(
            (
                row
                for row in self.auth.sessions.values()
                if row.id == identifier and row.authenticated
            ),
            None,
        )
        if target is None:
            raise WebError("not_found", "Browser session was not found", status=404)
        self.auth.revoke(target.key)
        response = success({"revoked": True, "current": target.id == request[SESSION].id})
        if target.id == request[SESSION].id:
            response.del_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="Strict")
        return response

    async def access(self, request):
        saved = await asyncio.to_thread(access_info, self.paths)
        return success(
            self.applied_access | {"saved": saved, "restart_required": saved != self.applied_access}
        )

    async def snapshot(self, request):
        return success(await self.state.get())

    async def devices(self, request):
        snapshot = await self.state.get()
        return success(
            {
                "devices": snapshot["state"]["devices"] if snapshot["state"] else [],
                "daemon": snapshot["daemon"],
            }
        )

    async def events(self, request):
        cursor = request.headers.get("Last-Event-ID")
        if cursor is not None and len(cursor) > 100:
            raise WebError("bad_request", "Event cursor is invalid")
        queue = await self.state.subscribe(cursor)
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        try:
            while not self.draining:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    event = ("heartbeat", None)
                await self.auth.refresh()
                self.auth.get(request.cookies.get(COOKIE), authenticated=True, touch=False)
                if event is None:
                    break
                name, payload = event
                if payload is not None:
                    payload = payload | {"web": payload["web"] | self.web_status()}
                packet = (
                    b": heartbeat\n\n"
                    if payload is None
                    else (
                        f"id: {payload['cursor']}\nevent: {name}\ndata: "
                        + json.dumps(payload, separators=(",", ":"))
                        + "\n\n"
                    ).encode()
                )
                async with asyncio.timeout(5):
                    await response.write(packet)
        except (OSError, TimeoutError, WebError, asyncio.CancelledError):
            pass
        finally:
            self.state.unsubscribe(queue)
        return response

    async def read_owner(self, request):
        name = request.match_info["read"]
        methods = {
            "selection": self.client.homekit_cameras,
            "controls": self.client.camera_controls,
            "recording": self.client.homekit_recording_cameras,
            "configuration": self.client.configuration_get,
        }
        if name not in methods:
            raise WebError("not_found", "Endpoint was not found", status=404)
        return success(await self.pool.call(methods[name]))

    async def pairing_page(self, *, force=False):
        state = await self.state.get(force=force)
        if not state["daemon"]["available"]:
            self.invalidate()
            raise WebError("daemon_unavailable", "Halo daemon is unavailable", status=503)
        view = state["state"]
        key = (
            view["epoch"],
            view["homekit"].get("adapter_boot_id"),
            view["homekit"].get("registry_revision"),
        )
        if (
            not force
            and self.pairing is not None
            and self.pairing_key == key
            and time.monotonic() - self.pairing_at < 10
        ):
            return self.pairing
        if self.pairing_task is not None and not self.pairing_task.done():
            result = await asyncio.shield(self.pairing_task)
        else:
            self.pairing_task = asyncio.create_task(
                self.pool.call(self.client.homekit_pairing_list)
            )
            self.pairing_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
            result = await asyncio.shield(self.pairing_task)
        current = await self.state.get(force=True)
        current_view = current["state"] or {}
        current_homekit = current_view.get("homekit", {})
        current_key = (
            current_view.get("epoch"),
            current_homekit.get("adapter_boot_id"),
            current_homekit.get("registry_revision"),
        )
        if (
            not current["daemon"]["available"]
            or current_key != key
            or result.get("boot_id") != key[1]
            or result.get("registry_revision") != key[2]
            or any(
                result.get(name) != current_view.get("scope", {}).get(name)
                for name in ("profile_id", "account_generation")
            )
        ):
            raise WebError("account_changed", "Pairing authority changed; refresh", status=409)
        self.pairing, self.pairing_key, self.pairing_at = result, key, time.monotonic()
        return result

    async def pairing_list(self, request):
        return success(await self.pairing_page())

    async def _pairing_target(self, value):
        page = await self.pairing_page(force=True)
        camera = next(
            (row for row in page["cameras"] if row["camera_id"] == value["camera_id"]), None
        )
        if (
            camera is None
            or any(
                value.get(key) != page.get(key)
                for key in ("profile_id", "account_generation", "registry_revision", "boot_id")
            )
            or value["pairing_revision"] != camera.get("pairing_revision")
        ):
            raise WebError(
                "configuration_conflict", "Pairing state changed; refresh the device", status=409
            )
        return page, camera

    async def reveal(self, request):
        self.auth.recent(request[SESSION])
        value = await body(
            request,
            (
                "camera_id",
                "profile_id",
                "account_generation",
                "registry_revision",
                "pairing_revision",
                "boot_id",
            ),
        )
        page, camera = await self._pairing_target(value)
        result = await self.pool.call(self.client.homekit_pairing_reveal, page, camera)
        # A logout/recovery concurrent with IPC must never publish the secret response.
        await self.auth.refresh()
        self.auth.get(request.cookies.get(COOKIE), authenticated=True)
        return success(result)

    async def reset(self, request):
        self.auth.recent(request[SESSION])
        value = await body(
            request,
            (
                "camera_id",
                "profile_id",
                "account_generation",
                "registry_revision",
                "pairing_revision",
                "boot_id",
                "operation_id",
            ),
        )
        identifier = operation_id(value["operation_id"])
        page, camera = await self._pairing_target(value)
        if camera.get("pending_reset") and camera.get("reset_operation_id") != identifier:
            raise WebError("operation_conflict", "Resume the existing reset operation", status=409)
        identity = {
            "profile_id": page["profile_id"],
            "camera_id": camera["camera_id"],
            "accessory_id": camera["accessory_id"],
        }
        result = await self.operations.run(
            identifier,
            "reset",
            identity,
            lambda: self.pool.call(self.client.homekit_pairing_reset, page, camera, identifier),
            resume=bool(camera.get("pending_reset")),
        )
        self.pairing = None
        return success(result)

    async def select(self, request):
        value = await body(
            request,
            (
                "camera_id",
                "enabled",
                "profile_id",
                "account_generation",
                "expected_revision",
                "operation_id",
            ),
        )
        text(value["camera_id"], maximum=128)
        boolean(value["enabled"])
        integer(value["account_generation"], minimum=1)
        integer(value["expected_revision"])
        identifier = operation_id(value["operation_id"])

        async def apply():
            page = await self.pool.call(self.client.homekit_cameras)
            if (
                any(page.get(key) != value.get(key) for key in ("profile_id", "account_generation"))
                or page["revision"] != value["expected_revision"]
            ):
                raise WebError(
                    "configuration_conflict", "Device selection changed; refresh", status=409
                )
            if not any(row["camera_id"] == value["camera_id"] for row in page["cameras"]):
                raise WebError("account_changed", "Device is no longer available", status=409)
            selected = {row["camera_id"] for row in page["cameras"] if row["selected"]}
            (selected.add if value["enabled"] else selected.discard)(value["camera_id"])
            return await self.pool.call(
                self.client.homekit_select,
                value["profile_id"],
                value["account_generation"],
                sorted(selected),
                value["expected_revision"],
            )

        result = await self.operations.run(identifier, "selection", value, apply)
        self.pairing = None
        return success(result)

    async def control(self, request):
        value = await body(
            request,
            (
                "profile_id",
                "account_generation",
                "camera_id",
                "device_generation",
                "operation_id",
                "control",
                "enabled",
            ),
        )
        _scope(value)
        integer(value["device_generation"], minimum=1)
        identifier = operation_id(value["operation_id"])
        boolean(value["enabled"])
        if value["control"] not in {"light", "siren"}:
            raise WebError("bad_request", "Camera control is unsupported")
        return success(
            await self.operations.run(
                identifier,
                "control",
                value,
                lambda: self.pool.call(self.client.camera_control, **value),
            )
        )

    async def warm(self, request):
        value = await body(
            request,
            (
                "profile_id",
                "account_generation",
                "camera_id",
                "expected_revision",
                "enabled",
                "operation_id",
            ),
        )
        _scope(value)
        integer(value["expected_revision"])
        boolean(value["enabled"])
        identifier = operation_id(value["operation_id"])
        params = {key: item for key, item in value.items() if key != "operation_id"}
        return success(
            await self.operations.run(
                identifier,
                "warm",
                value,
                lambda: self.pool.call(self.client.homekit_recording_warm, **params),
            )
        )

    async def devices_monitoring(self, request):
        value = await body(
            request,
            (
                "profile_id",
                "account_generation",
                "expected_revision",
                "enabled",
                "operation_id",
            ),
        )
        text(value["profile_id"], maximum=36)
        integer(value["account_generation"], minimum=1)
        integer(value["expected_revision"])
        boolean(value["enabled"])
        identifier = operation_id(value["operation_id"])
        params = {key: item for key, item in value.items() if key != "operation_id"}
        return success(
            await self.operations.run(
                identifier,
                "configuration",
                {"target": "device_monitoring", **value},
                lambda: self.pool.call(self.client.devices_monitoring_set, **params),
            )
        )

    async def device_bridge_pairing(self, request):
        return success(await self.pool.call(self.client.devices_bridge_pairing_get))

    async def _device_bridge_target(self, value):
        page = await self.pool.call(self.client.devices_bridge_pairing_get)
        if any(
            value.get(key) != page.get(key)
            for key in (
                "profile_id",
                "account_generation",
                "registry_revision",
                "accessory_id",
                "boot_id",
                "pairing_revision",
            )
        ):
            raise WebError("configuration_conflict", "Bridge pairing changed; refresh", status=409)
        return page

    async def device_bridge_reveal(self, request):
        from halo_server.devices.publication import PAIRING_FIELDS

        self.auth.recent(request[SESSION])
        value = await body(request, tuple(PAIRING_FIELDS))
        await self._device_bridge_target(value)
        result = await self.pool.call(self.client.devices_bridge_pairing_reveal, **value)
        await self.auth.refresh()
        self.auth.get(request.cookies.get(COOKIE), authenticated=True)
        # Recheck publication/account authority after crossing the IPC boundary.
        await self._device_bridge_target(value)
        return success(result)

    async def device_bridge_reset(self, request):
        from halo_server.devices.publication import PAIRING_FIELDS

        self.auth.recent(request[SESSION])
        value = await body(request, tuple(PAIRING_FIELDS) + ("operation_id",))
        identifier = operation_id(value["operation_id"])
        page = await self._device_bridge_target(value)
        if page["pending_reset"] and page["reset_operation_id"] != identifier:
            raise WebError("configuration_conflict", "Resume the pending bridge reset", status=409)
        return success(
            await self.operations.run(
                identifier,
                "reset",
                {
                    "target": "device_bridge_pairing",
                    **{
                        key: value[key]
                        for key in ("profile_id", "accessory_id", "pairing_revision")
                    },
                },
                lambda: self.pool.call(self.client.devices_bridge_pairing_reset, **value),
                resume=page["pending_reset"],
            )
        )

    async def device_bridge_publication(self, request):
        value = await body(
            request,
            (
                "profile_id",
                "account_generation",
                "expected_registry_revision",
                "expected_revision",
                "enabled",
                "operation_id",
            ),
        )
        text(value["profile_id"], maximum=36)
        integer(value["account_generation"], minimum=1)
        integer(value["expected_registry_revision"])
        integer(value["expected_revision"])
        boolean(value["enabled"])
        identifier = operation_id(value["operation_id"])
        params = {key: item for key, item in value.items() if key != "operation_id"}
        return success(
            await self.operations.run(
                identifier,
                "configuration",
                {"target": "device_bridge_publication", **value},
                lambda: self.pool.call(self.client.devices_bridge_publication_set, **params),
            )
        )

    async def devices_bridge(self, request):
        from halo_server.devices.bridge import PREFERENCE_FIELDS, validate_preferences

        value = await body(
            request,
            tuple(PREFERENCE_FIELDS)
            + ("profile_id", "account_generation", "expected_revision", "operation_id"),
        )
        text(value["profile_id"], maximum=36)
        integer(value["account_generation"], minimum=1)
        integer(value["expected_revision"])
        try:
            validate_preferences({key: value[key] for key in PREFERENCE_FIELDS})
        except ValueError as error:
            raise WebError("bad_request", "Bridge preferences are invalid") from error
        identifier = operation_id(value["operation_id"])
        params = {key: item for key, item in value.items() if key != "operation_id"}
        return success(
            await self.operations.run(
                identifier,
                "configuration",
                {"target": "device_bridge", **value},
                lambda: self.pool.call(self.client.devices_bridge_set, **params),
            )
        )

    async def configuration(self, request):
        value = await body(request, ("configuration", "expected_revision", "operation_id"))
        fields(
            value["configuration"], ("mode", "plugin_host", "plugin_port", "rtsp_host", "rtsp_port")
        )
        integer(value["expected_revision"])
        identifier = operation_id(value["operation_id"])
        return success(
            await self.operations.run(
                identifier,
                "configuration",
                value,
                lambda: self.pool.call(
                    self.client.configuration_set,
                    value["expected_revision"],
                    value["configuration"],
                ),
            )
        )

    async def ring_login(self, request):
        value = await body(request, ("email", "password", "epoch"))
        return success(await self.ring.begin(request[SESSION], **value))

    async def ring_challenge(self, request):
        value = await body(request, ("challenge_id", "otp_code"))
        return success(
            await self.ring.continue_(request[SESSION], value["challenge_id"], value["otp_code"])
        )

    async def ring_cancel(self, request):
        return success(self.ring.cancel(request[SESSION], request.match_info["identifier"]))

    async def ring_logout(self, request):
        value = await body(request, ("epoch", "operation_id"))
        identifier = operation_id(value["operation_id"])

        async def apply():
            await self.ring.epoch(value["epoch"])
            self.ring.clear()
            await self.pool.call(self.client.logout, expected_epoch=value["epoch"])
            return {"disconnected": True}

        return success(await self.operations.run(identifier, "ring_logout", value, apply))

    async def doctor(self, request):
        probe = False
        if request.method == "POST":
            value = await body(request, ("probe",))
            probe = boolean(value["probe"])
        snapshot = await self.state.get()
        result = {
            "schema": 1,
            "kind": "web",
            "web": {"ready": True, "release": release_status()},
            "daemon_available": snapshot["daemon"]["available"],
            "daemon": None,
        }
        if result["daemon_available"]:
            result["daemon"] = await self.pool.call(self.client.doctor, probe=probe)
        return success(result)

    async def support(self, request):
        result = await self.pool.call(self.client.support_report)
        response = success(result)
        response.headers["Content-Disposition"] = 'attachment; filename="halo-support.json"'
        return response

    async def restart_info(self, request):
        return success(
            await self.restart.info()
            if self.restart
            else {"available": False, "code": "unmanaged_installation", "selection_revision": None}
        )

    async def restart_start(self, request):
        self.auth.recent(request[SESSION])
        value = await body(request, ("operation_id", "expected_revision"))
        if self.restart is None:
            raise WebError("unsupported", "Restart requires a managed installation", status=409)
        return success(
            await self.restart.start(
                operation_id(value["operation_id"]), integer(value["expected_revision"])
            ),
            status=202,
        )

    async def restart_get(self, request):
        if self.restart is None:
            raise WebError("unsupported", "Restart requires a managed installation", status=409)
        return success(await self.restart.get(operation_id(request.match_info["identifier"])))

    async def operation(self, request):
        return success(self.operations.get(request.match_info["identifier"]))

    async def operation_list(self, request):
        return success(
            {
                "operations": [
                    {
                        key: row[key]
                        for key in ("operation_id", "kind", "status", "code", "possibly_sent")
                    }
                    for row in list(self.operations.rows.values())[-50:][::-1]
                ]
            }
        )


def _scope(value):
    text(value["profile_id"], maximum=36)
    text(value["camera_id"], maximum=128)
    integer(value["account_generation"], minimum=1)


def _cookie(response, token):
    response.set_cookie(COOKIE, token, secure=True, httponly=True, samesite="Strict", path="/")


@web.middleware
async def security(request, handler):
    console = request.app[CONSOLE]
    if console.active_requests >= 32:
        return failure(WebError("busy", "The web console is busy", status=429))
    console.active_requests += 1
    mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
    if mutation:
        console.active_mutations += 1
    try:
        if not request.secure or request.headers.getall("Host", []) != [console.settings.authority]:
            raise WebError("origin_rejected", "Use the configured HTTPS address", status=403)
        origin = request.headers.getall("Origin", [])
        if origin and origin != [console.settings.origin]:
            raise WebError("origin_rejected", "Cross-origin access is not allowed", status=403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise WebError("origin_rejected", "Cross-origin access is not allowed", status=403)
        if request.path.startswith(PREFIX):
            await console.auth.refresh()
            public = request.path in {f"{PREFIX}/{name}" for name in ("session", "setup", "login")}
            passive = request.method == "GET" and request.path in {
                PREFIX + "/state",
                PREFIX + "/events",
                PREFIX + "/session",
            }
            session = console.auth.get(
                request.cookies.get(COOKIE), authenticated=not public, touch=not passive
            )
            request[SESSION] = session
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if origin != [console.settings.origin]:
                    raise WebError(
                        "origin_rejected", "Same-origin requests are required", status=403
                    )
                console.auth.csrf(session, request.headers.get("X-Halo-CSRF"))
                if console.draining and request.path != f"{PREFIX}/logout":
                    raise WebError("service_stopping", "Web administration is draining", status=503)
        return await handler(request)
    except WebError as error:
        return failure(error)
    except AdminClientError as error:
        conflict = error.code in {"configuration_conflict", "account_changed", "operation_conflict"}
        return failure(
            WebError(
                error.code,
                "Halo could not complete this request; refresh its current state",
                status=409 if conflict else 503,
                possibly_sent=error.code == "request_failed",
            )
        )
    except ActivationError as error:
        return failure(
            WebError(
                error.code,
                "Managed restart is unavailable or its state changed",
                status=409,
                possibly_sent=error.possibly_saved,
            )
        )
    except PrivateJsonError:
        return failure(
            WebError(
                "private_state_unavailable",
                "Private web state is unavailable; use local recovery",
                status=503,
            )
        )
    except web.HTTPException as error:
        return failure(WebError("request_rejected", "Request was rejected", status=error.status))
    except Exception:
        # Never log request bodies, cookies, exception strings or arbitrary daemon data.
        return failure(
            WebError("unavailable", "The web request could not be completed", status=503)
        )
    finally:
        console.active_requests -= 1
        if mutation:
            console.active_mutations -= 1


async def response_headers(request, response):
    response.headers.update(HEADERS)
    if request.path.startswith(PREFIX):
        response.headers["Cache-Control"] = "no-store"


def create_application(console, *, static_root=None):
    app = web.Application(client_max_size=64 * 1024, middlewares=[security])
    app[CONSOLE] = console
    app.on_response_prepare.append(response_headers)

    async def lifecycle(_app):
        await console.start()
        yield
        await console.close()

    app.cleanup_ctx.append(lifecycle)
    routes = [
        web.get(PREFIX + "/session", console.session),
        web.post(PREFIX + "/{action:setup|login|logout|reauth|password}", console.auth_action),
        web.get(PREFIX + "/sessions", console.sessions),
        web.delete(PREFIX + "/sessions/{identifier}", console.revoke),
        web.get(PREFIX + "/access", console.access),
        web.get(PREFIX + "/state", console.snapshot),
        web.get(PREFIX + "/devices", console.devices),
        web.post(PREFIX + "/devices/monitoring", console.devices_monitoring),
        web.post(PREFIX + "/devices/bridge", console.devices_bridge),
        web.get(PREFIX + "/devices/bridge/pairing", console.device_bridge_pairing),
        web.post(PREFIX + "/devices/bridge/publication", console.device_bridge_publication),
        web.post(PREFIX + "/devices/bridge/pairing/reveal", console.device_bridge_reveal),
        web.post(PREFIX + "/devices/bridge/pairing/reset", console.device_bridge_reset),
        web.get(PREFIX + "/events", console.events),
        web.get(PREFIX + "/{read:selection|controls|recording|configuration}", console.read_owner),
        web.get(PREFIX + "/pairing", console.pairing_list),
        web.post(PREFIX + "/pairing/reveal", console.reveal),
        web.post(PREFIX + "/pairing/reset", console.reset),
        web.post(PREFIX + "/selection", console.select),
        web.post(PREFIX + "/controls", console.control),
        web.post(PREFIX + "/recording/warm", console.warm),
        web.post(PREFIX + "/configuration", console.configuration),
        web.post(PREFIX + "/ring/login", console.ring_login),
        web.post(PREFIX + "/ring/challenge", console.ring_challenge),
        web.delete(PREFIX + "/ring/challenge/{identifier}", console.ring_cancel),
        web.post(PREFIX + "/ring/logout", console.ring_logout),
        web.get(PREFIX + "/doctor", console.doctor),
        web.post(PREFIX + "/doctor", console.doctor),
        web.get(PREFIX + "/support", console.support),
        web.get(PREFIX + "/restart", console.restart_info),
        web.post(PREFIX + "/restart", console.restart_start),
        web.get(PREFIX + "/restart/{identifier}", console.restart_get),
        web.get(PREFIX + "/operations", console.operation_list),
        web.get(PREFIX + "/operations/{identifier}", console.operation),
    ]
    app.add_routes(routes)
    _static(app, Path(static_root) if static_root else Path(__file__).parent / "static")
    return app


def _static(app, root):
    from halo_server.web_assets import validate_bundle

    allowed = validate_bundle(root)

    async def asset(request):
        name = request.match_info.get("asset", "") or "index.html"
        if name not in allowed:
            raise web.HTTPNotFound()
        path = root / name
        try:
            resolved = path.resolve(strict=True)
            if (
                not resolved.is_relative_to(root.resolve())
                or path.is_symlink()
                or not resolved.is_file()
            ):
                raise web.HTTPNotFound()
        except OSError:
            raise web.HTTPNotFound() from None
        response = web.FileResponse(resolved)
        response.headers["Cache-Control"] = (
            "no-cache" if name == "index.html" else "public, max-age=3600"
        )
        return response

    app.router.add_get("/{asset:.*}", asset)

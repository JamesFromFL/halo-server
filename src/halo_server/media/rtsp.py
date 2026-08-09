"""Digest-authenticated RTSP delivery from leased encoded media buffers."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtsp", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst, GstRtsp, GstRtspServer  # noqa: E402

from halo_server.media.buffer import EncodedMediaBuffer, MediaStreamKind  # noqa: E402
from halo_server.media.ring_media import gst_push_succeeded  # noqa: E402
from halo_server.media.timestamps import EncodedTimestampMapper  # noqa: E402

_log = logging.getLogger(__name__)

RTSP_ROLE = "halo-plugin"
VIDEO_CLOCK_RATE = 90_000
VIDEO_FRAME_DURATION_NS = Gst.SECOND // 30
AUDIO_CLOCK_RATE = 48_000
AUDIO_FRAME_DURATION_NS = Gst.SECOND * 20 // 1000
BUFFER_READ_TIMEOUT = 1.0
MEDIA_READY_TIMEOUT = 8.0
MEDIA_READY_POLL_INTERVAL = 0.1
LEASE_RENEW_INTERVAL = 15.0
WORKER_STOP_TIMEOUT = 5.0
REQUIRED_RTSP_ELEMENTS = (
    "appsrc",
    "queue",
    "h264parse",
    "rtph264pay",
    "rtpopuspay",
)


class RtspGatewayError(RuntimeError):
    """The bounded RTSP gateway could not start or serve a stream."""


@dataclass
class BufferedMediaConsumer:
    """One downstream claim on a camera media session."""

    buffer: EncodedMediaBuffer
    renew_callback: Callable[[], bool]
    close_callback: Callable[[], None]

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = False

    def renew(self) -> bool:
        with self._lock:
            return False if self._closed else bool(self.renew_callback())

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.close_callback()


class RtspMediaGateway:
    """Serve registered camera buffers through shared on-demand RTSP mounts."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        open_consumer: Callable[[str], BufferedMediaConsumer],
        username: str = "halo",
        password: str | None = None,
    ) -> None:
        if not _is_loopback_host(host):
            raise ValueError("RTSP media gateway currently requires a loopback address")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("RTSP media gateway port is invalid")
        if not username or len(username) > 64 or ":" in username:
            raise ValueError("RTSP media gateway username is invalid")
        generated_password = password or secrets.token_urlsafe(24)
        if not generated_password or len(generated_password) > 128:
            raise ValueError("RTSP media gateway password is invalid")
        self.host = host
        self.port = port
        self.bound_port: int | None = None
        self.username = username
        self._password = generated_password
        self._open_consumer = open_consumer
        self._context: GLib.MainContext | None = None
        self._loop: GLib.MainLoop | None = None
        self._server = None
        self._mounts = None
        self._source_id: int | None = None
        self._thread: threading.Thread | None = None
        self._factories: dict[str, _BufferedCameraFactory] = {}
        self._lock = threading.RLock()

    def start(self, camera_ids: list[str] | tuple[str, ...] = ()) -> None:
        with self._lock:
            if self._thread is not None:
                return
            Gst.init(None)
            missing_elements = _missing_rtsp_elements()
            if missing_elements:
                raise RtspGatewayError(
                    "RTSP media gateway is missing required GStreamer elements: "
                    + ", ".join(missing_elements)
                )
            context = GLib.MainContext.new()
            loop = GLib.MainLoop.new(context, False)
            server = GstRtspServer.RTSPServer.new()
            server.set_address(self.host)
            server.set_service(str(self.port))
            _install_digest_auth(server, self.username, self._password)
            mounts = server.get_mount_points()
            self._context = context
            self._loop = loop
            self._server = server
            self._mounts = mounts
            for camera_id in sorted(set(camera_ids)):
                self._add_camera_locked(camera_id)
            source_id = int(server.attach(context))
            if source_id == 0:
                self._clear_locked()
                raise RtspGatewayError("RTSP media gateway could not attach")
            self._source_id = source_id
            bound_port = int(server.get_bound_port())
            if not 1 <= bound_port <= 65535:
                source = context.find_source_by_id(source_id)
                if source is not None:
                    source.destroy()
                self._clear_locked()
                raise RtspGatewayError("RTSP media gateway did not bind a TCP port")
            self.bound_port = bound_port
            thread = threading.Thread(
                target=self._run_loop,
                name="halo-rtsp",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            loop = self._loop
            context = self._context
            source_id = self._source_id
            factories = tuple(self._factories.values())
            self._thread = None
            self._source_id = None
            self.bound_port = None
        for factory in factories:
            factory.stop_workers()
        if source_id is not None and context is not None:
            source = context.find_source_by_id(source_id)
            if source is not None:
                source.destroy()
        if loop is not None:
            loop.quit()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=WORKER_STOP_TIMEOUT)
            if thread.is_alive():
                raise RtspGatewayError("RTSP media gateway did not stop")
        with self._lock:
            self._clear_locked()

    def sync_cameras(self, camera_ids: list[str] | tuple[str, ...]) -> None:
        normalized = {_validate_camera_id(camera_id) for camera_id in camera_ids}

        def apply() -> None:
            with self._lock:
                for camera_id in set(self._factories) - normalized:
                    self._remove_camera_locked(camera_id)
                for camera_id in normalized - set(self._factories):
                    self._add_camera_locked(camera_id)

        self._invoke(apply)

    def rotate_credentials(self) -> None:
        password = secrets.token_urlsafe(24)

        def apply() -> None:
            with self._lock:
                self._password = password
                if self._server is not None:
                    _install_digest_auth(self._server, self.username, password)
                factories = tuple(self._factories.values())
            for factory in factories:
                factory.stop_workers()

        self._invoke(apply)

    def stream_descriptor(self, camera_id: str) -> dict | None:
        normalized = _validate_camera_id(camera_id)
        with self._lock:
            if normalized not in self._factories or self.bound_port is None:
                return None
            host = _url_host(self.host)
            path = _mount_path(normalized)
            username = quote(self.username, safe="")
            password = quote(self._password, safe="")
            url = f"rtsp://{username}:{password}@{host}:{self.bound_port}{path}"
        return {
            "id": "main",
            "name": "Halo Stream",
            "url": url,
            "container": "rtsp",
            "prebuffer": 10_000,
            "video": {"codec": "h264"},
            "audio": {"codec": "opus"},
        }

    def _run_loop(self) -> None:
        context = self._context
        loop = self._loop
        if context is None or loop is None:
            return
        context.push_thread_default()
        try:
            loop.run()
        finally:
            context.pop_thread_default()

    def _invoke(self, callback: Callable[[], None]) -> None:
        with self._lock:
            context = self._context
            thread = self._thread
        if context is None or thread is None:
            callback()
            return
        if thread is threading.current_thread():
            callback()
            return
        completed = threading.Event()
        error: list[BaseException] = []

        def invoked(_data) -> bool:
            try:
                callback()
            except BaseException as exc:
                error.append(exc)
            finally:
                completed.set()
            return GLib.SOURCE_REMOVE

        context.invoke_full(GLib.PRIORITY_DEFAULT, invoked, None)
        if not completed.wait(WORKER_STOP_TIMEOUT):
            raise RtspGatewayError("RTSP media gateway update timed out")
        if error:
            raise RtspGatewayError("RTSP media gateway update failed") from error[0]

    def _add_camera_locked(self, camera_id: str) -> None:
        camera_id = _validate_camera_id(camera_id)
        if camera_id in self._factories:
            return
        if self._mounts is None:
            return
        factory = _BufferedCameraFactory(camera_id, self._open_consumer)
        _grant_authenticated_access(factory)
        self._mounts.add_factory(_mount_path(camera_id), factory)
        self._factories[camera_id] = factory

    def _remove_camera_locked(self, camera_id: str) -> None:
        factory = self._factories.pop(camera_id, None)
        if factory is None:
            return
        factory.stop_workers()
        if self._mounts is not None:
            self._mounts.remove_factory(_mount_path(camera_id))

    def _clear_locked(self) -> None:
        self._factories.clear()
        self._mounts = None
        self._server = None
        self._context = None
        self._loop = None
        self._source_id = None
        self._thread = None
        self.bound_port = None


class _BufferedCameraFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(
        self,
        camera_id: str,
        open_consumer: Callable[[str], BufferedMediaConsumer],
    ) -> None:
        super().__init__()
        self._camera_id = camera_id
        self._open_consumer = open_consumer
        self._workers: dict[threading.Thread, threading.Event] = {}
        self._worker_lock = threading.Lock()
        self.set_launch(_passthrough_launch())
        # Scrypted's direct RTSP parser prefers UDP unless the server rejects it.
        # Large Ring IDR frames span hundreds of FU-A packets, where even one
        # missing or reordered UDP packet makes the decoder boundary unusable.
        self.set_protocols(GstRtsp.RTSPLowerTrans.TCP)
        # Each client must replay its own decoder boundary from the shared buffer.
        # CameraMediaSession still shares one Ring upstream across these consumers.
        self.set_shared(False)
        self.connect("media-configure", self._on_media_configure)

    def _on_media_configure(self, _factory, media) -> None:
        element = media.get_element()
        video_source = element.get_by_name("vsrc")
        audio_source = element.get_by_name("asrc")
        stop = threading.Event()

        def on_unprepared(_media) -> None:
            _log.info("RTSP media unprepared for camera %s", self._camera_id)
            stop.set()

        media.connect(
            "prepared",
            lambda _media: _log.info(
                "RTSP media prepared for camera %s",
                self._camera_id,
            ),
        )
        media.connect("unprepared", on_unprepared)
        worker = threading.Thread(
            target=self._prepare_and_stream,
            args=(video_source, audio_source, stop),
            name=f"halo-rtsp-{_mount_key(self._camera_id)}",
            daemon=True,
        )
        with self._worker_lock:
            self._workers[worker] = stop
        worker.start()

    def _prepare_and_stream(
        self,
        video_source,
        audio_source,
        stop: threading.Event,
    ) -> None:
        consumer: BufferedMediaConsumer | None = None
        try:
            consumer = self._open_consumer(self._camera_id)
        except Exception:
            _log.exception("Could not open RTSP media for camera %s", self._camera_id)
            stop.set()
        try:
            if consumer is None or stop.is_set():
                return
            ready_started = time.monotonic()
            deadline = ready_started + MEDIA_READY_TIMEOUT
            ready = False
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if consumer.buffer.wait_until_ready(min(MEDIA_READY_POLL_INTERVAL, remaining)):
                    ready = True
                    break
            if not ready:
                if not stop.is_set():
                    _log.warning(
                        "RTSP media for camera %s did not become decodable within %.1f seconds",
                        self._camera_id,
                        MEDIA_READY_TIMEOUT,
                    )
                return
            snapshot = consumer.buffer.snapshot()
            _log.info(
                "RTSP media for camera %s became decodable after %.3f seconds "
                "(%d packets, %d bytes)",
                self._camera_id,
                time.monotonic() - ready_started,
                len(snapshot.packets),
                snapshot.payload_bytes,
            )
            self._stream_buffer(video_source, audio_source, consumer, stop)
        except Exception:
            _log.exception("RTSP media delivery failed for camera %s", self._camera_id)
        finally:
            stop.set()
            try:
                video_source.emit("end-of-stream")
            except Exception:
                _log.exception("Could not end RTSP video for camera %s", self._camera_id)
            try:
                audio_source.emit("end-of-stream")
            except Exception:
                _log.exception("Could not end RTSP audio for camera %s", self._camera_id)
            try:
                if consumer is not None:
                    consumer.close()
            except Exception:
                _log.exception("Could not close RTSP consumer for camera %s", self._camera_id)
            finally:
                with self._worker_lock:
                    self._workers.pop(threading.current_thread(), None)

    def _stream_buffer(
        self,
        video_source,
        audio_source,
        consumer: BufferedMediaConsumer,
        stop: threading.Event,
    ) -> None:
        video_timestamps = EncodedTimestampMapper(
            clock_rate=VIDEO_CLOCK_RATE,
            default_duration_ns=VIDEO_FRAME_DURATION_NS,
        )
        audio_timestamps = EncodedTimestampMapper(
            clock_rate=AUDIO_CLOCK_RATE,
            default_duration_ns=AUDIO_FRAME_DURATION_NS,
        )
        after_sequence: int | None = None
        next_renewal = time.monotonic() + LEASE_RENEW_INTERVAL
        while not stop.is_set():
            media_read = consumer.buffer.read(
                after_sequence=after_sequence,
                timeout=BUFFER_READ_TIMEOUT,
            )
            discontinuities = {
                MediaStreamKind.VIDEO: media_read.resynchronized,
                MediaStreamKind.AUDIO: media_read.resynchronized,
            }
            for packet in media_read.packets:
                is_video = packet.stream is MediaStreamKind.VIDEO
                source = video_source if is_video else audio_source
                mapper = video_timestamps if is_video else audio_timestamps
                duration = VIDEO_FRAME_DURATION_NS if is_video else AUDIO_FRAME_DURATION_NS
                pts, timestamp_discontinuity = mapper.map(packet.rtp_timestamp)
                buffer = Gst.Buffer.new_wrapped(packet.payload)
                buffer.pts = pts
                buffer.dts = pts
                buffer.duration = duration
                if timestamp_discontinuity or discontinuities[packet.stream]:
                    buffer.set_flags(Gst.BufferFlags.DISCONT)
                    discontinuities[packet.stream] = False
                flow = source.emit("push-buffer", buffer)
                if not gst_push_succeeded(flow):
                    if not stop.is_set():
                        _log.warning(
                            "RTSP appsrc rejected %s for camera %s with flow %s",
                            packet.stream,
                            self._camera_id,
                            flow,
                        )
                    stop.set()
                    break
                after_sequence = packet.sequence
            if time.monotonic() >= next_renewal:
                if not consumer.renew():
                    stop.set()
                    break
                next_renewal = time.monotonic() + LEASE_RENEW_INTERVAL

    def stop_workers(self) -> None:
        with self._worker_lock:
            workers = tuple(self._workers.items())
        for _worker, stop in workers:
            stop.set()
        deadline = time.monotonic() + WORKER_STOP_TIMEOUT
        for worker, _stop in workers:
            if worker is threading.current_thread():
                continue
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                _log.warning(
                    "RTSP media worker %s did not stop within %.1f seconds",
                    worker.name,
                    WORKER_STOP_TIMEOUT,
                )


def _passthrough_launch() -> str:
    return (
        "( appsrc name=vsrc is-live=true format=time do-timestamp=false block=false "
        "caps=video/x-h264,stream-format=byte-stream,alignment=au "
        "! queue max-size-buffers=120 max-size-bytes=0 max-size-time=0 leaky=downstream "
        "! h264parse config-interval=-1 "
        "! rtph264pay name=pay0 pt=96 config-interval=1 "
        "  appsrc name=asrc is-live=true format=time do-timestamp=false block=false "
        "caps=audio/x-opus,channel-mapping-family=0,channels=1,rate=48000 "
        "! queue max-size-buffers=240 max-size-bytes=0 max-size-time=0 leaky=downstream "
        "! rtpopuspay name=pay1 pt=97 )"
    )


def _missing_rtsp_elements() -> tuple[str, ...]:
    return tuple(
        element for element in REQUIRED_RTSP_ELEMENTS if Gst.ElementFactory.find(element) is None
    )


def _install_digest_auth(server, username: str, password: str) -> None:
    auth = GstRtspServer.RTSPAuth.new()
    auth.set_supported_methods(GstRtsp.RTSPAuthMethod.DIGEST)
    token = GstRtspServer.RTSPToken.new()
    token.set_string(GstRtspServer.RTSP_TOKEN_MEDIA_FACTORY_ROLE, RTSP_ROLE)
    auth.add_digest(username, password, token)
    server.set_auth(auth)


def _grant_authenticated_access(factory) -> None:
    role = Gst.Structure.new_empty(RTSP_ROLE)
    role.set_value(GstRtspServer.RTSP_PERM_MEDIA_FACTORY_ACCESS, True)
    role.set_value(GstRtspServer.RTSP_PERM_MEDIA_FACTORY_CONSTRUCT, True)
    factory.add_role_from_structure(role)


def _validate_camera_id(camera_id: str) -> str:
    if not isinstance(camera_id, str) or not camera_id or len(camera_id) > 128:
        raise ValueError("camera id is invalid")
    return camera_id


def _mount_key(camera_id: str) -> str:
    return hashlib.sha256(camera_id.encode()).hexdigest()[:24]


def _mount_path(camera_id: str) -> str:
    return f"/halo/{_mount_key(camera_id)}"


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _url_host(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    return f"[{host}]" if address.version == 6 else host


def _validate_duration(value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("duration must be finite and positive")


_validate_duration(BUFFER_READ_TIMEOUT)
_validate_duration(MEDIA_READY_POLL_INTERVAL)
_validate_duration(LEASE_RENEW_INTERVAL)
_validate_duration(WORKER_STOP_TIMEOUT)

"""Pinned per-receiver routed-RTP observations, never packets or media policy."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
import threading
import time
from collections import deque

import aiortc

from halo_server.media.telemetry import StreamMetrics

PERIOD_MS = 100
CAPACITY = 64
_LIMIT = 2**53 - 1
_KINDS = ("audio", "video")
_FIELDS = ("audio_rtp", "video_rtp", "audio_frames", "video_frames")


class ReceiverMetricsError(RuntimeError):
    """The pinned observation boundary cannot be installed safely."""


def _safe(callback, *args):
    with contextlib.suppress(BaseException):
        return callback(*args)
    return None


class ReceiverPipeline:
    def __init__(self, audio_receiver, video_receiver, *, clock=time.monotonic):
        self.metrics = StreamMetrics(
            counters=("audio_rtp", "video_rtp", "audio_frames", "video_frames"),
            timings=(
                "audio_rtp_gap",
                "video_rtp_gap",
                "audio_frame_gap",
                "video_frame_gap",
                "audio_handler",
                "video_handler",
                "wakeup_lag",
            ),
        )
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._fenced = False
        self._receivers = (audio_receiver, video_receiver)
        self._bins = [None] * CAPACITY
        self._previous = {}
        self._lower = None

    def _now(self):
        with contextlib.suppress(BaseException):
            now = self._clock()
            return now if type(now) in (int, float) and math.isfinite(now) else None
        return None

    def _metric(self, method, *args):
        with contextlib.suppress(BaseException):
            getattr(self.metrics, method)(*args)

    def _tick(self, now):
        elapsed = max(0, now - self._started)
        if elapsed >= _LIMIT / 1000:
            return _LIMIT // PERIOD_MS
        return round(elapsed * 1_000_000) // 100_000

    def _elapsed_ms(self, now):
        elapsed = max(0, now - self._started)
        return _LIMIT if elapsed >= _LIMIT / 1000 else round(elapsed * 1000)

    def _record(self, kind, category, now):
        if kind not in _KINDS or now is None:
            return
        name = f"{kind}_{category}"
        tick = self._tick(now)
        with self._lock:
            if self._fenced:
                return
            item = self._bins[tick % CAPACITY]
            if item is None or item[0] != tick:
                item = self._bins[tick % CAPACITY] = (tick, dict.fromkeys(_FIELDS, 0))
            item[1][name] = min(_LIMIT, item[1][name] + 1)
            previous = self._previous.get(name)
            self._previous[name] = now
        self._metric("count", name)
        if previous is not None:
            self._metric(
                "observe",
                f"{kind}_{'rtp' if category == 'rtp' else 'frame'}_gap",
                max(0, now - previous),
            )

    def _entry(self, kind):
        now = self._now()
        self._record(kind, "rtp", now)
        return now

    def _finish(self, kind, entered):
        now = self._now()
        with self._lock:
            active = not self._fenced
        if active and now is not None and entered is not None:
            self._metric("observe", f"{kind}_handler", max(0, now - entered))

    def frame(self, kind, entered=None):
        # The optional encoded-ingress timestamp uses the same monotonic axis.
        _safe(self._record, kind, "frames", self._now() if entered is None else entered)

    def wake_lag(self, seconds):
        with self._lock:
            active = not self._fenced
        if active:
            self._metric("observe", "wakeup_lag", seconds)

    def snapshot(self):
        now = self._now()
        if now is None:
            return None
        tick = self._tick(now)
        with self._lock:
            if self._fenced:
                return None
            bins = []
            for index in range(max(0, tick - CAPACITY + 1), tick + 1):
                item = self._bins[index % CAPACITY]
                counts = (
                    dict(item[1])
                    if item is not None and item[0] == index
                    else dict.fromkeys(_FIELDS, 0)
                )
                bins.append({"elapsed_ms": index * PERIOD_MS, **counts})
            receivers = self._receivers
        shared = None
        with contextlib.suppress(BaseException):
            audio, video = (getattr(receiver, "transport", None) for receiver in receivers)
            if audio is not None and video is not None:
                shared = audio is video
        metrics = self.metrics.snapshot()
        lower = _safe(self._lower.snapshot) if self._lower is not None else None
        with self._lock:
            if self._fenced:
                return None
        result = {
            "period_ms": PERIOD_MS,
            "capacity": CAPACITY,
            "end_elapsed_ms": self._elapsed_ms(now),
            "bins": bins,
            "shared_dtls": shared,
            "metrics": metrics,
        }
        if lower is not None:
            result["lower_boundary"] = lower
        return result

    def install_lower_boundaries(self):
        """Observe already-gathered instances; unsupported diagnostics cannot stop media."""
        owner = LowerReceiverBoundary(self)
        with self._lock:
            if self._fenced or self._lower is not None:
                return
            self._lower = owner
        try:
            owner.install(self._receivers)
        except BaseException:
            owner.unavailable()

    def fence(self):
        with self._lock:
            self._fenced = True
            self._receivers = ()
            self._previous.clear()
            lower, self._lower = self._lower, None
        if lower is not None:
            lower.fence()


def install_rtp_observer(receiver, kind, pipeline):
    """Wrap one instance before receive; preserve return, failure and cancellation."""
    handler = getattr(type(receiver), "_handle_rtp_packet", None)
    code = getattr(handler, "__code__", None)
    original = getattr(receiver, "_handle_rtp_packet", None)
    required = {"_RTCRtpReceiver__jitter_buffer", "add", "_RTCRtpReceiver__decoder_queue"}
    if (
        str(getattr(aiortc, "__version__", "")) != "1.15.0"
        or kind not in _KINDS
        or not isinstance(pipeline, ReceiverPipeline)
        or not inspect.iscoroutinefunction(handler)
        or not callable(original)
        or code is None
        or code.co_argcount != 3
        or code.co_varnames[:3] != ("self", "packet", "arrival_time_ms")
        or not required.issubset(code.co_names)
        or "_handle_rtp_packet" in receiver.__dict__
    ):
        raise ReceiverMetricsError("Routed RTP observation contract changed")

    async def observed(packet, arrival_time_ms):
        entered = _safe(pipeline._entry, kind)
        try:
            return await original(packet, arrival_time_ms)
        finally:
            _safe(pipeline._finish, kind, entered)

    receiver._handle_rtp_packet = observed


LOWER_FIELDS = (
    "udp_stun",
    "udp_dtls",
    "udp_rtp",
    "udp_rtcp",
    "udp_other",
    "ice_enqueued",
    "ice_dequeued",
    "ice_rtp",
    "ice_rtcp",
    "ice_dtls",
    "ice_other",
    "dtls_rtp",
    "dtls_rtcp",
    "dtls_control",
    "rtcp_remb_attempt",
    "rtcp_rr_attempt",
    "rtcp_pli_attempt",
    "rtcp_nack_attempt",
    "rtcp_other_attempt",
)
LOWER_BIN_FIELDS = (*LOWER_FIELDS, "remb_bitrate_min", "remb_bitrate_max")
LOWER_COUNTERS = (*LOWER_FIELDS, "metadata_overflows", "queue_wait_unknown")
LOWER_TIMINGS = (
    *(f"{name}_gap" for name in LOWER_FIELDS),
    "udp_handler",
    "ice_queue_wait",
    "dtls_dispatch_wait",
    "dtls_service",
    "dtls_rtp_handler",
    "dtls_rtcp_handler",
)
MAX_LOWER_CONNECTIONS = 2
MAX_LOWER_PROTOCOLS = 16
MAX_QUEUE_TIMES = 512


def _datagram_kind(data):
    # Only public packet-type octets are examined, never retained or exported.
    if not isinstance(data, bytes) or not data:
        return "other"
    first = data[0]
    if first < 4:
        return "stun"
    if 19 < first < 64:
        return "dtls"
    if 127 < first < 192:
        return "rtcp" if len(data) > 1 and 191 < data[1] < 224 else "rtp"
    return "other"


class _QueueTimes:
    def __init__(self):
        self.times = deque()
        self.unknown = 0
        self.dequeued_at = None
        self.kind = None


class LowerReceiverBoundary:
    """Transparent, owned UDP/ICE/DTLS brackets with timestamp-only queue metadata.

    UDP is an asyncio callback boundary, not a kernel wire timestamp. DTLS RTP
    dispatch is after successful unprotect but before packet parse/demultiplex.
    Kernel socket drop counts remain explicitly unknown; global counters are
    never attributed to this source.
    """

    def __init__(self, pipeline):
        self._pipeline = pipeline
        self.metrics = StreamMetrics(counters=LOWER_COUNTERS, timings=LOWER_TIMINGS)
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._fenced = False
        self._installed = False
        self._reason = "contract_unavailable"
        self._bins = [None] * CAPACITY
        self._previous = {}
        self._hooks = []
        self._states = []
        self._udp_transports = {}
        self._connections = self._protocols = 0

    def _record(self, name, at=None):
        at = self._pipeline._now() if at is None else at
        if at is None or name not in LOWER_FIELDS:
            return
        tick = self._pipeline._tick(at)
        with self._lock:
            if self._fenced:
                return
            item = self._bins[tick % CAPACITY]
            if item is None or item[0] != tick:
                item = self._bins[tick % CAPACITY] = (tick, dict.fromkeys(LOWER_BIN_FIELDS, 0))
            item[1][name] = min(_LIMIT, item[1][name] + 1)
            previous = self._previous.get(name)
            self._previous[name] = at
        _safe(self.metrics.count, name)
        if previous is not None:
            _safe(self.metrics.observe, f"{name}_gap", max(0, at - previous))

    def _observe(self, name, began):
        now = self._pipeline._now()
        with self._lock:
            active = not self._fenced
        if active and now is not None and began is not None:
            _safe(self.metrics.observe, name, max(0, now - began))

    def _enqueue(self, state, at, queued):
        self._record("ice_enqueued", at)
        overflow = False
        with self._lock:
            if self._fenced:
                return
            if len(state.times) >= MAX_QUEUE_TIMES:
                # Forget timing for this exact already-queued prefix rather
                # than misattribute later timestamps after metadata overflow.
                state.times.clear()
                state.unknown = min(_LIMIT, max(0, queued))
                overflow = True
            else:
                state.times.append(at)
        if overflow:
            _safe(self.metrics.count, "metadata_overflows")

    def _dequeue(self, state, data):
        now = self._pipeline._now()
        kind = _datagram_kind(data)
        self._record("ice_dequeued", now)
        self._record(f"ice_{kind if kind in {'rtp', 'rtcp', 'dtls'} else 'other'}", now)
        began = None
        with self._lock:
            if self._fenced:
                return
            state.dequeued_at, state.kind = now, kind
            if state.unknown:
                state.unknown -= 1
            elif state.times:
                began = state.times.popleft()
        if began is None or now is None:
            _safe(self.metrics.count, "queue_wait_unknown")
        else:
            _safe(self.metrics.observe, "ice_queue_wait", max(0, now - began))

    def _attach(self, target, name, wrapper):
        existed = name in target.__dict__
        original = getattr(target, name)
        setattr(target, name, wrapper)
        self._hooks.append((target, name, wrapper, original, existed))

    def _udp(self, original):
        def observed(data, addr):
            entered = _safe(self._pipeline._now)
            _safe(self._record, f"udp_{_safe(_datagram_kind, data) or 'other'}", entered)
            try:
                return original(data, addr)
            finally:
                _safe(self._observe, "udp_handler", entered)
                data = addr = None

        return observed

    def _queued(self, original, connection, state):
        def observed(data, component):
            entered = _safe(self._pipeline._now)
            try:
                result = original(data, component)
                _safe(self._enqueue, state, entered, _safe(connection._queue.qsize))
                return result
            finally:
                data = component = None

        return observed

    def _recv(self, original, state):
        async def observed():
            data = None
            try:
                data = await original()
                _safe(self._dequeue, state, data)
                return data
            finally:
                data = None

        return observed

    def _dispatch(self, original, state, kind):
        async def observed(data, *args, **kwargs):
            entered = _safe(self._pipeline._now)
            _safe(self._record, f"dtls_{kind}", entered)
            _safe(self._observe, "dtls_dispatch_wait", state.dequeued_at)
            try:
                return await original(data, *args, **kwargs)
            finally:
                _safe(self._observe, f"dtls_{kind}_handler", entered)
                data = args = kwargs = None

        return observed

    def _outgoing(self, packet):
        from aiortc.rtp import (
            RTCP_PSFB_APP,
            RTCP_PSFB_PLI,
            RTCP_RTPFB_NACK,
            RtcpPsfbPacket,
            RtcpRrPacket,
            RtcpRtpfbPacket,
            unpack_remb_fci,
        )

        kind, bitrate = "other", None
        if isinstance(packet, RtcpRrPacket):
            kind = "rr"
        elif isinstance(packet, RtcpRtpfbPacket) and packet.fmt == RTCP_RTPFB_NACK:
            kind = "nack"
        elif isinstance(packet, RtcpPsfbPacket):
            if packet.fmt == RTCP_PSFB_PLI:
                kind = "pli"
            elif (
                packet.fmt == RTCP_PSFB_APP
                and isinstance(packet.fci, bytes)
                and len(packet.fci) <= 272
            ):
                with contextlib.suppress(ValueError, TypeError):
                    bitrate, _sources = unpack_remb_fci(packet.fci)
                    kind = "remb"
        now = self._pipeline._now()
        self._record(f"rtcp_{kind}_attempt", now)
        if kind == "remb" and type(bitrate) is int and 0 <= bitrate <= _LIMIT and now is not None:
            tick = self._pipeline._tick(now)
            with self._lock:
                if self._fenced:
                    return
                item = self._bins[tick % CAPACITY]
                previous = item[1]["remb_bitrate_min"]
                item[1]["remb_bitrate_min"] = (
                    min(previous, bitrate) if item[1]["rtcp_remb_attempt"] > 1 else bitrate
                )
                item[1]["remb_bitrate_max"] = max(item[1]["remb_bitrate_max"], bitrate)

    def _send_rtcp(self, original):
        async def observed(packet):
            _safe(self._outgoing, packet)
            try:
                return await original(packet)
            finally:
                packet = None

        return observed

    def _cycle(self, original, state):
        async def observed():
            state.dequeued_at = state.kind = None
            try:
                return await original()
            finally:
                _safe(self._observe, "dtls_service", state.dequeued_at)
                if state.kind == "dtls":
                    _safe(self._record, "dtls_control")

        return observed

    def install(self, receivers):
        with self._lifecycle_lock:
            if self._fenced:
                return
            self._install(receivers)

    def _install(self, receivers):
        import aioice
        from aioice.ice import Connection, StunProtocol
        from aiortc.rtcdtlstransport import RTCDtlsTransport
        from aiortc.rtcicetransport import RTCIceTransport

        def contract(cls, method, arguments, names):
            member = getattr(cls, method, None)
            code = getattr(member, "__code__", None)
            if code is None or code.co_varnames[: len(arguments)] != arguments:
                return False
            return set(names).issubset(code.co_names)

        if (
            str(getattr(aiortc, "__version__", "")) != "1.15.0"
            or str(getattr(aioice, "__version__", "")) != "0.10.2"
            or not contract(
                StunProtocol,
                "datagram_received",
                ("self", "data", "addr"),
                ("receiver", "data_received", "parse_message"),
            )
            or not contract(
                Connection, "data_received", ("self", "data", "component"), ("_queue", "put_nowait")
            )
            or not contract(
                RTCDtlsTransport,
                "_recv_next",
                ("self",),
                ("transport", "_recv", "unprotect", "_handle_rtp_data"),
            )
            or not contract(
                RTCDtlsTransport,
                "_handle_rtp_data",
                ("self", "data", "arrival_time_ms"),
                ("parse", "route_rtp", "_handle_rtp_packet"),
            )
            or not contract(
                RTCDtlsTransport,
                "_handle_rtcp_data",
                ("self", "data"),
                ("parse", "route_rtcp", "_handle_rtcp_packet"),
            )
        ):
            raise ReceiverMetricsError("Lower receive observation contract changed")
        transports, protocols = [], []
        for receiver in receivers:
            dtls = receiver.transport
            if dtls in transports:
                continue
            if type(dtls) is not RTCDtlsTransport or type(dtls.transport) is not RTCIceTransport:
                raise ReceiverMetricsError("Lower receive owner changed")
            connection = dtls.transport._connection
            if any(other.transport._connection is connection for other in transports):
                raise ReceiverMetricsError("Lower receive connection is shared unexpectedly")
            if type(connection) is not Connection or not isinstance(
                connection._queue, asyncio.Queue
            ):
                raise ReceiverMetricsError("Lower receive queue changed")
            if any(
                name in dtls.__dict__
                for name in ("_recv_next", "_handle_rtp_data", "_handle_rtcp_data")
            ):
                raise ReceiverMetricsError("Lower receive already observed")
            if (
                "data_received" in connection.__dict__
                or getattr(dtls.transport._recv, "__self__", None) is not connection
            ):
                raise ReceiverMetricsError("Lower receive already observed")
            if len(transports) >= MAX_LOWER_CONNECTIONS:
                raise ReceiverMetricsError("Lower receive owner capacity")
            transports.append(dtls)
            local = connection._protocols
            if not local or len(local) + len(protocols) > MAX_LOWER_PROTOCOLS:
                raise ReceiverMetricsError("Lower receive socket capacity")
            for protocol in local:
                if (
                    type(protocol) is not StunProtocol
                    or protocol.receiver is not connection
                    or not isinstance(protocol.transport, asyncio.DatagramTransport)
                    or "datagram_received" in protocol.__dict__
                ):
                    raise ReceiverMetricsError("Lower receive protocol changed")
                protocols.append(protocol)
        for receiver in receivers:
            if "_send_rtcp" in receiver.__dict__ or not contract(
                type(receiver), "_send_rtcp", ("self", "packet"), ("transport", "_send_rtp")
            ):
                raise ReceiverMetricsError("Receiver RTCP observation contract changed")
        # Validate the complete bounded target set before the first mutation.
        self._udp_transports = {protocol: protocol.transport for protocol in protocols}
        for dtls in transports:
            ice, state = dtls.transport, _QueueTimes()
            connection = ice._connection
            state.unknown = min(_LIMIT, connection._queue.qsize())
            self._states.append(state)
            self._attach(
                connection,
                "data_received",
                self._queued(connection.data_received, connection, state),
            )
            self._attach(ice, "_recv", self._recv(ice._recv, state))
            self._attach(dtls, "_recv_next", self._cycle(dtls._recv_next, state))
            self._attach(
                dtls, "_handle_rtp_data", self._dispatch(dtls._handle_rtp_data, state, "rtp")
            )
            self._attach(
                dtls, "_handle_rtcp_data", self._dispatch(dtls._handle_rtcp_data, state, "rtcp")
            )
            for protocol in connection._protocols:
                self._attach(protocol, "datagram_received", self._udp(protocol.datagram_received))
        for receiver in receivers:
            self._attach(receiver, "_send_rtcp", self._send_rtcp(receiver._send_rtcp))
        self._connections, self._protocols = len(transports), len(protocols)
        self._installed, self._reason = True, "ready"

    def _detach(self):
        for target, name, wrapper, original, existed in reversed(self._hooks):
            with contextlib.suppress(BaseException):
                if target.__dict__.get(name) is wrapper:
                    if existed:
                        setattr(target, name, original)
                    else:
                        delattr(target, name)
        self._hooks.clear()
        for state in self._states:
            state.times.clear()
            state.unknown = 0
            state.dequeued_at = state.kind = None
        self._states.clear()
        self._udp_transports.clear()

    def unavailable(self):
        with self._lifecycle_lock:
            self._detach()
            self._installed = False
            self._reason = "contract_unavailable"

    def fence(self):
        with self._lifecycle_lock:
            with self._lock:
                self._fenced = True
                self._previous.clear()
            self._detach()

    def _selected_coverage(self):
        """Current object membership, never transport identities or addresses."""
        unknown = {
            "state": "unavailable",
            "current_transports": None,
            "current_protocols": None,
            "nominated_pairs": None,
            "transports_covered": None,
            "protocols_covered": None,
            "nominated_covered": None,
            "wrappers_current": None,
        }
        with self._lifecycle_lock:
            if self._fenced or not self._installed:
                return unknown
            try:
                from aioice.ice import CandidatePair, Connection, StunProtocol
                from aiortc.rtcicetransport import RTCIceTransport

                with self._pipeline._lock:
                    receivers = self._pipeline._receivers
                if len(receivers) != 2:
                    return unknown
                transports, protocols, connections, nominated = [], [], [], []
                for receiver in receivers:
                    dtls = receiver.transport
                    if dtls in transports:
                        continue
                    if len(transports) >= MAX_LOWER_CONNECTIONS:
                        return unknown
                    if type(dtls.transport) is not RTCIceTransport:
                        return unknown
                    connection = dtls.transport._connection
                    if type(connection) is not Connection:
                        return unknown
                    local, selected = connection._protocols, connection._nominated
                    if (
                        not isinstance(local, list)
                        or not local
                        or len(protocols) + len(local) > MAX_LOWER_PROTOCOLS
                        or not isinstance(selected, dict)
                        or len(selected) > 2
                        or any(type(item) is not StunProtocol for item in local)
                        or any(type(item) is not CandidatePair for item in selected.values())
                    ):
                        return unknown
                    transports.append(dtls)
                    connections.append(connection)
                    protocols.extend(local)
                    nominated.extend(item.protocol for item in selected.values())

                def hooked(target, name):
                    return any(
                        owner is target and field == name for owner, field, *_rest in self._hooks
                    )

                transport_covered = all(
                    hooked(dtls, "_recv_next")
                    and hooked(dtls.transport, "_recv")
                    and hooked(connection, "data_received")
                    for dtls, connection in zip(transports, connections, strict=True)
                )
                protocols_covered = all(
                    hooked(item, "datagram_received")
                    and isinstance(item.transport, asyncio.DatagramTransport)
                    and item.transport is self._udp_transports.get(item)
                    and any(item.receiver is connection for connection in connections)
                    for item in protocols
                )
                nominated_covered = (
                    all(
                        item in protocols and hooked(item, "datagram_received")
                        for item in nominated
                    )
                    if len(nominated) == len(connections)
                    else None
                )
                targets = (
                    *transports,
                    *(item.transport for item in transports),
                    *connections,
                    *protocols,
                    *receivers,
                )
                wrappers_current = all(
                    owner.__dict__.get(name) is wrapper
                    for owner, name, wrapper, *_rest in self._hooks
                    if any(owner is target for target in targets)
                )
                complete = transport_covered and protocols_covered and wrappers_current
                state = (
                    "uncovered"
                    if not complete or nominated_covered is False
                    else "covered"
                    if nominated_covered is True
                    else "unobserved"
                )
                return {
                    "state": state,
                    "current_transports": len(transports),
                    "current_protocols": len(protocols),
                    "nominated_pairs": len(nominated),
                    "transports_covered": transport_covered,
                    "protocols_covered": protocols_covered,
                    "nominated_covered": nominated_covered,
                    "wrappers_current": wrappers_current,
                }
            except BaseException:
                return unknown

    def snapshot(self):
        now = self._pipeline._now()
        if now is None:
            return None
        tick = self._pipeline._tick(now)
        with self._lock:
            if self._fenced:
                return None
            bins = []
            for index in range(max(0, tick - CAPACITY + 1), tick + 1):
                item = self._bins[index % CAPACITY]
                counts = (
                    dict(item[1])
                    if item is not None and item[0] == index
                    else dict.fromkeys(LOWER_BIN_FIELDS, 0)
                )
                bins.append({"elapsed_ms": index * PERIOD_MS, **counts})
            result = {
                "installed": self._installed,
                "reason": self._reason,
                "connections": self._connections,
                "protocols": self._protocols,
                "udp_drops": None,
                "bins": bins,
            }
        result["metrics"] = self.metrics.snapshot()
        result["selected_transport_coverage"] = self._selected_coverage()
        with self._lock:
            return None if self._fenced else result

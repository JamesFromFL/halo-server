import time

from halo_server.media import probe
from halo_server.media.probe import probe_caps, probe_encoded_media
from halo_server.media.profiles import EncodedMediaProfile


def test_encoded_media_probe_requires_h264_and_opus_and_waits_for_cleanup(monkeypatch):
    class Receiver:
        def __init__(
            self,
            _device,
            _on_video,
            _on_audio,
            *,
            on_encoded_video,
            on_encoded_audio,
            **_kwargs,
        ):
            on_encoded_video(b"h264", 90_000)
            on_encoded_audio(b"opus", 960)

        def receive(self, stop):
            client.stop = stop

            async def run():
                return None

            return run()

    class Submitted:
        def done(self):
            return False

        def result(self, timeout):
            assert timeout == probe.PROBE_CLEANUP_TIMEOUT
            assert client.stop.is_set()
            client.cleanup_waited = True

    class Client:
        stop = None
        cleanup_waited = False

        def submit(self, coroutine):
            coroutine.close()
            return Submitted()

    client = Client()
    monkeypatch.setattr(probe, "RingStreamReceiver", Receiver)

    assert probe_encoded_media(client, object()) == EncodedMediaProfile(opus_frame_duration_ms=20)
    assert client.cleanup_waited is True


def test_encoded_media_probe_rejects_incompatible_codec(monkeypatch):
    class Receiver:
        def __init__(
            self,
            _device,
            _on_video,
            _on_audio,
            *,
            on_encoded_incompatible,
            **_kwargs,
        ):
            on_encoded_incompatible("video", "video/vp8")

        def receive(self, _stop):
            async def run():
                return None

            return run()

    class Submitted:
        def done(self):
            return False

        def result(self, _timeout):
            return None

    class Client:
        def submit(self, coroutine):
            coroutine.close()
            return Submitted()

    monkeypatch.setattr(probe, "RingStreamReceiver", Receiver)

    assert probe_encoded_media(Client(), object()) is None


def test_probe_caps_waits_for_receiver_cleanup(monkeypatch):
    class Receiver:
        def __init__(self, _device, on_video, on_audio):
            on_video(b"video", 1920, 1080)
            on_audio(b"audio", 2, 48000)

        def receive(self, stop):
            client.stop = stop

            async def run():
                return None

            return run()

    class Submitted:
        def result(self, timeout):
            assert timeout == probe.PROBE_CLEANUP_TIMEOUT
            assert client.stop.is_set()
            client.cleanup_waited = True

    class Client:
        stop = None
        cleanup_waited = False

        def submit(self, coroutine):
            coroutine.close()
            return Submitted()

    client = Client()
    monkeypatch.setattr(probe, "RingStreamReceiver", Receiver)

    assert probe_caps(client, object()) == {
        "width": 1920,
        "height": 1080,
        "achannels": 2,
        "arate": 48000,
    }
    assert client.cleanup_waited


def test_video_only_caps_probe_uses_short_audio_grace(monkeypatch):
    class Receiver:
        def __init__(self, _device, on_video, _on_audio):
            on_video(b"video", 1920, 1080)

        def receive(self, _stop):
            async def run():
                return None

            return run()

    class Submitted:
        def result(self, timeout):
            assert timeout == probe.PROBE_CLEANUP_TIMEOUT
            return None

    class Client:
        def submit(self, coroutine):
            coroutine.close()
            return Submitted()

    monkeypatch.setattr(probe, "RingStreamReceiver", Receiver)
    monkeypatch.setattr(probe, "PROBE_AUDIO_GRACE", 0.01)
    started = time.monotonic()

    caps = probe_caps(Client(), object())

    assert time.monotonic() - started < 0.5
    assert caps == {
        "width": 1920,
        "height": 1080,
        "achannels": 2,
        "arate": 48000,
    }


def test_caps_probe_closes_coroutine_when_submission_fails(monkeypatch):
    created = []

    class Receiver:
        def __init__(self, _device, _on_video, _on_audio):
            pass

        def receive(self, _stop):
            async def run():
                return None

            coroutine = run()
            created.append(coroutine)
            return coroutine

    class Client:
        def submit(self, _coroutine):
            raise RuntimeError("loop stopped")

    monkeypatch.setattr(probe, "RingStreamReceiver", Receiver)

    assert probe_caps(Client(), object()) is None
    assert created[0].cr_frame is None

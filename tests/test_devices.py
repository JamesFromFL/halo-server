from halo_server.ring.devices import summarize_cameras, summarize_device


class FakeCamera:
    id = 42
    name = "Front Door"
    family = "doorbots"
    kind = "lpd_v4"
    model = "Doorbell Pro"
    subscribed = True
    battery_life = "89"
    motion_detection = True

    def has_capability(self, capability):
        return capability in {"siren", "video"}

    async def generate_async_webrtc_stream(self):
        return None

    async def on_webrtc_candidate(self):
        return None

    async def close_webrtc_stream(self):
        return None

    def get_ice_servers(self):
        return []


def test_device_summary_is_an_allowlisted_camera_contract():
    summary = summarize_device(FakeCamera())

    assert summary.as_dict() == {
        "id": "42",
        "name": "Front Door",
        "family": "doorbots",
        "kind": "lpd_v4",
        "model": "Doorbell Pro",
        "online": True,
        "battery_percent": 89,
        "motion_detection": True,
        "has_light": False,
        "has_siren": True,
        "has_intercom": True,
    }


def test_device_summary_ignores_non_camera_and_missing_identity():
    class Light:
        family = "lights"
        id = 1

    class MissingIdentity:
        family = "stickup_cams"
        id = None

    assert summarize_device(Light()) is None
    assert summarize_device(MissingIdentity()) is None


def test_device_summary_contains_attribute_failures():
    class FaultyCamera(FakeCamera):
        @property
        def battery_life(self):
            raise RuntimeError("not available")

        @property
        def motion_detection(self):
            raise RuntimeError("not available")

        def has_capability(self, _capability):
            raise RuntimeError("not available")

    summary = summarize_device(FaultyCamera())

    assert summary.battery_percent is None
    assert summary.motion_detection is None
    assert summary.has_light is False
    assert summary.has_siren is False
    assert summary.has_intercom is False


def test_device_summary_uses_quiet_fallback_for_sdk_unknown_model():
    class NewCamera(FakeCamera):
        kind = "stickup_cam_mini_v3"

        @property
        def model(self):
            raise AssertionError("noisy SDK model accessor must not run")

    summary = summarize_device(NewCamera())

    assert summary.kind == "stickup_cam_mini_v3"
    assert summary.model == "Unknown Stickup Cam"


def test_camera_summaries_are_sorted_and_plain_json_data():
    second = FakeCamera()
    second.id = 2
    second.name = "Back Yard"
    first = FakeCamera()
    first.id = 1
    first.name = "Front Door"

    summaries = summarize_cameras([first, second])

    assert [camera["name"] for camera in summaries] == ["Back Yard", "Front Door"]


def test_device_summary_bounds_strings_to_the_plugin_contract():
    camera = FakeCamera()
    camera.name = "\U0001f4f7" * 300
    camera.kind = "k" * 200
    camera.model = "m" * 200

    summary = summarize_device(camera)

    assert len(summary.name.encode("utf-16-le")) // 2 <= 256
    assert len(summary.kind.encode("utf-16-le")) // 2 <= 128
    assert len(summary.model.encode("utf-16-le")) // 2 <= 128


def test_device_summary_omits_identity_outside_the_plugin_contract():
    camera = FakeCamera()
    camera.id = "x" * 129

    assert summarize_device(camera) is None

    camera.id = "camera-\ud800"
    assert summarize_device(camera) is None


def test_camera_summaries_ignore_duplicate_ring_ids():
    first = FakeCamera()
    duplicate = FakeCamera()
    duplicate.name = "Duplicate Front Door"

    summaries = summarize_cameras([first, duplicate])

    assert len(summaries) == 1
    assert summaries[0]["id"] == "42"

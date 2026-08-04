import pytest

from halo_server.media.timestamps import EncodedTimestampMapper, SampleTimestampMapper


def test_encoded_timestamp_mapper_stays_monotonic_across_source_restart():
    mapper = EncodedTimestampMapper(clock_rate=90_000, default_duration_ns=33_333_333)

    assert mapper.map(90_000) == (0, True)
    assert mapper.map(93_000) == (33_333_333, False)
    restart_pts, discontinuity = mapper.map(0)
    assert restart_pts > 33_333_333
    assert discontinuity is True
    assert mapper.map(3_000)[0] > restart_pts


def test_sample_timestamp_mapper_builds_contiguous_timeline_from_audio_content():
    mapper = SampleTimestampMapper()

    assert mapper.map(2880, 48000) == (0, 60_000_000, True)
    assert mapper.map(2880, 48000) == (60_000_000, 60_000_000, False)
    assert mapper.map(960, 48000) == (120_000_000, 20_000_000, False)


@pytest.mark.parametrize(("samples", "rate"), [(0, 48000), (960, 0)])
def test_sample_timestamp_mapper_rejects_invalid_audio_shape(samples, rate):
    with pytest.raises(ValueError, match="positive"):
        SampleTimestampMapper().map(samples, rate)

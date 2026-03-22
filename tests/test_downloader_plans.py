from videocp.downloader import build_download_plans
from videocp.models import MediaCandidate, MediaKind, TrackType, WatermarkMode


def test_build_download_plans_pairs_video_with_audio():
    video = MediaCandidate(
        url="https://example.com/media-video-avc1/",
        kind=MediaKind.MP4,
        track_type=TrackType.VIDEO_ONLY,
        watermark_mode=WatermarkMode.UNKNOWN,
        source="response",
        observed_via="network",
    )
    audio = MediaCandidate(
        url="https://example.com/media-audio-und-mp4a/",
        kind=MediaKind.MP4,
        track_type=TrackType.AUDIO_ONLY,
        watermark_mode=WatermarkMode.UNKNOWN,
        source="response",
        observed_via="network",
    )
    plans = build_download_plans([video, audio])
    assert len(plans) == 1
    assert plans[0].primary.url == video.url
    assert plans[0].audio is not None
    assert plans[0].audio.url == audio.url


def test_build_download_plans_skips_audio_only_primary():
    audio = MediaCandidate(
        url="https://example.com/media-audio-und-mp4a/",
        kind=MediaKind.MP4,
        track_type=TrackType.AUDIO_ONLY,
        watermark_mode=WatermarkMode.UNKNOWN,
        source="response",
        observed_via="network",
    )
    try:
        build_download_plans([audio])
    except Exception as exc:
        assert "audio-only" in str(exc)
    else:
        raise AssertionError("expected audio-only plans to fail")

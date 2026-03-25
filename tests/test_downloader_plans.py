from pathlib import Path

import requests

from videocp.downloader import build_download_plans, build_media_request_headers, download_mp4_to_path
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


def test_download_mp4_to_path_retries_chunked_encoding_error(tmp_path: Path):
    candidate = MediaCandidate(
        url="https://example.com/video.mp4",
        kind=MediaKind.MP4,
        track_type=TrackType.MUXED,
        watermark_mode=WatermarkMode.NO_WATERMARK,
        source="json",
        observed_via="json",
    )

    class FakeResponse:
        def __init__(self, chunks, *, error=None):
            self.status_code = 200
            self.headers = {"content-type": "video/mp4", "content-length": "4"}
            self._chunks = chunks
            self._error = error

        def iter_content(self, chunk_size):
            for chunk in self._chunks:
                yield chunk
            if self._error is not None:
                raise self._error

        def close(self):
            return None

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def get(self, url, headers, stream, timeout, allow_redirects):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse([b"ab"], error=requests.exceptions.ChunkedEncodingError("Connection broken"))
            return FakeResponse([b"ab", b"cd"])

    session = FakeSession()
    target_path = tmp_path / "video.mp4"

    size = download_mp4_to_path(
        session=session,
        candidate=candidate,
        target_path=target_path,
        user_agent="ua",
        referer="https://example.com/page",
        timeout_secs=10,
        emit_log=False,
    )

    assert size == 4
    assert session.calls == 2
    assert target_path.read_bytes() == b"abcd"


def test_build_media_request_headers_omits_referer_for_tv_assets():
    headers = build_media_request_headers(
        "https://upos.example.com/video.m4s?platform=android_tv_yst&deadline=1",
        "tv-ua",
        "https://www.bilibili.com/video/BV1xx",
    )

    assert headers["User-Agent"] == "tv-ua"
    assert headers["Accept-Encoding"] == "identity"
    assert "Referer" not in headers


def test_build_media_request_headers_keeps_referer_for_web_assets():
    headers = build_media_request_headers(
        "https://upos.example.com/video.m4s?deadline=1",
        "web-ua",
        "https://www.bilibili.com/video/BV1xx",
    )

    assert headers["User-Agent"] == "web-ua"
    assert headers["Accept-Encoding"] == "identity"
    assert headers["Referer"] == "https://www.bilibili.com/video/BV1xx"

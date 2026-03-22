from videocp.extractor import candidate_rank, conservative_rewrites, infer_track_type, sort_candidates
from videocp.models import MediaCandidate, MediaKind, TrackType, WatermarkMode


def test_candidate_sort_prefers_no_watermark_mp4():
    candidates = [
        MediaCandidate(
            url="https://example.com/playlist.m3u8",
            kind=MediaKind.HLS,
            track_type=TrackType.MUXED,
            watermark_mode=WatermarkMode.NO_WATERMARK,
            source="response",
            observed_via="network",
        ),
        MediaCandidate(
            url="https://example.com/wm.mp4",
            kind=MediaKind.MP4,
            track_type=TrackType.MUXED,
            watermark_mode=WatermarkMode.WATERMARK,
            source="response",
            observed_via="network",
        ),
        MediaCandidate(
            url="https://example.com/play.mp4",
            kind=MediaKind.MP4,
            track_type=TrackType.MUXED,
            watermark_mode=WatermarkMode.NO_WATERMARK,
            source="response",
            observed_via="network",
        ),
    ]
    sorted_candidates = sort_candidates(candidates)
    assert sorted_candidates[0].url == "https://example.com/play.mp4"
    assert candidate_rank(sorted_candidates[0]) < candidate_rank(sorted_candidates[1])


def test_conservative_rewrites_adds_play_variant():
    candidate = MediaCandidate(
        url="https://example.com/playwm/v1.mp4?watermark=1",
        kind=MediaKind.MP4,
        track_type=TrackType.MUXED,
        watermark_mode=WatermarkMode.WATERMARK,
        source="response",
        observed_via="network",
    )
    rewritten = conservative_rewrites([candidate])
    assert any(item.source == "rewrite" for item in rewritten)
    rewrite = next(item for item in rewritten if item.source == "rewrite")
    assert "playwm" not in rewrite.url
    assert "watermark=0" in rewrite.url


def test_infer_track_type_detects_audio_and_video_variants():
    assert infer_track_type("https://x/media-audio-und-mp4a/?mime_type=video_mp4", MediaKind.MP4) == TrackType.AUDIO_ONLY
    assert infer_track_type("https://x/media-video-avc1/?mime_type=video_mp4", MediaKind.MP4) == TrackType.VIDEO_ONLY
    assert infer_track_type("https://x/master.m3u8", MediaKind.HLS) == TrackType.MUXED

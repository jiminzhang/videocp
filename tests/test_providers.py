from videocp.extractor import ExtractionAccumulator
from videocp.input_parser import parse_input
from videocp.models import TrackType
from videocp.providers import get_provider_by_key, resolve_provider


def test_resolve_provider_supports_multiple_sites():
    assert resolve_provider("https://www.douyin.com/video/123").key == "douyin"
    assert resolve_provider("https://www.bilibili.com/video/BV1764y1y76G/").key == "bilibili"
    assert resolve_provider("https://www.xiaohongshu.com/explore/69be081c0000000021010b12").key == "xiaohongshu"


def test_parse_input_sets_provider_key(monkeypatch):
    monkeypatch.setattr("videocp.input_parser.resolve_url", lambda url, timeout_secs=15: url)
    parsed = parse_input("https://www.bilibili.com/video/BV1764y1y76G/")
    assert parsed.provider_key == "bilibili"


def test_bilibili_provider_extracts_embedded_json_payloads():
    provider = get_provider_by_key("bilibili")
    accumulator = ExtractionAccumulator(
        metadata=provider.create_metadata("https://www.bilibili.com/video/BV1764y1y76G/"),
        provider=provider,
    )
    markup = """
    <script>
    window.__INITIAL_STATE__={"bvid":"BV1764y1y76G","videoData":{"title":"B站示例","desc":"示例简介","owner":{"name":"UP主"}}};
    window.__playinfo__={"data":{"dash":{"video":[{"baseUrl":"https://upos.example.com/video-1.m4s","backupUrl":["https://upos.example.com/video-1-backup.m4s"]}],"audio":[{"baseUrl":"https://upos.example.com/audio-1.m4s"}]}}};
    </script>
    """

    accumulator.ingest_markup(markup)
    candidates = provider.sort_candidates(accumulator.candidates)

    assert accumulator.metadata.aweme_id == "BV1764y1y76G"
    assert accumulator.metadata.title == "B站示例"
    assert accumulator.metadata.desc == "示例简介"
    assert accumulator.metadata.author == "UP主"
    assert len(candidates) >= 2
    assert any(candidate.track_type == TrackType.VIDEO_ONLY for candidate in candidates)
    assert any(candidate.track_type == TrackType.AUDIO_ONLY for candidate in candidates)


def test_xiaohongshu_provider_uses_dom_snapshot_metadata():
    provider = get_provider_by_key("xiaohongshu")
    metadata = provider.create_metadata("https://www.xiaohongshu.com/explore/69be081c0000000021010b12")
    collected: list[tuple[str, str]] = []

    def add_candidate(url: str, source: str, observed_via: str, **kwargs) -> None:
        collected.append((url, kwargs.get("semantic_tag", "")))

    provider.apply_dom_snapshot(
        metadata,
        {
            "page_url": "https://www.xiaohongshu.com/explore/69be081c0000000021010b12",
            "title": "同一趟航班飞纽约，😭差距也太大了吧！ - 小红书",
            "og_title": "同一趟航班飞纽约，😭差距也太大了吧！ - 小红书",
            "description": "#商务舱机票 #国际机票",
            "og_description": "3 亿人的生活经验，都在小红书",
            "og_video": "https://sns-video-qc.xhscdn.com/example.mp4",
            "video_src": "",
            "author_text": "Rani商务舱机票",
        },
        add_candidate,
    )

    assert metadata.aweme_id == "69be081c0000000021010b12"
    assert metadata.title == "同一趟航班飞纽约，😭差距也太大了吧！"
    assert metadata.desc == "#商务舱机票 #国际机票"
    assert metadata.author == "Rani商务舱机票"
    assert collected == [("https://sns-video-qc.xhscdn.com/example.mp4", "og_video")]

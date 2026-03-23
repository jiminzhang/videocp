from videocp.extractor import ExtractionAccumulator
from videocp.models import ObservedEvent, TrackType
from videocp.input_parser import parse_input
from videocp.providers import get_provider_by_key, resolve_provider


def test_resolve_provider_supports_multiple_sites():
    assert resolve_provider("https://www.douyin.com/video/123").key == "douyin"
    assert resolve_provider("https://www.bilibili.com/video/BV1764y1y76G/").key == "bilibili"
    assert resolve_provider("https://www.xiaohongshu.com/explore/69be081c0000000021010b12").key == "xiaohongshu"


def test_parse_input_sets_provider_key(monkeypatch):
    monkeypatch.setattr("videocp.input_parser.resolve_url", lambda url, timeout_secs=15: url)
    parsed = parse_input("https://www.bilibili.com/video/BV1764y1y76G/")
    assert parsed.provider_key == "bilibili"


def test_douyin_provider_canonicalizes_modal_and_light_urls():
    provider = get_provider_by_key("douyin")
    assert provider.canonicalize_url("https://www.douyin.com/jingxuan?modal_id=7617405320117128502") == (
        "https://www.douyin.com/video/7617405320117128502"
    )
    assert provider.canonicalize_url("https://www.douyin.com/light/7617405320117128502") == (
        "https://www.douyin.com/video/7617405320117128502"
    )
    assert provider.canonicalize_url("https://www.douyin.com/video/7615069974301780963?previous_page=web_code_link") == (
        "https://www.douyin.com/video/7615069974301780963"
    )


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


def test_douyin_provider_filters_candidates_to_requested_modal_id():
    provider = get_provider_by_key("douyin")
    accumulator = ExtractionAccumulator(
        metadata=provider.create_metadata("https://www.douyin.com/jingxuan?modal_id=2222222222222222222"),
        provider=provider,
    )

    accumulator.ingest_event(
        ObservedEvent(
            url="https://www.douyin.com/aweme/v1/web/feed/",
            content_type="application/json",
            json_body={
                "aweme_list": [
                    {
                        "aweme_id": "1111111111111111111",
                        "desc": "wrong",
                        "author": {"nickname": "wrong-author"},
                        "video": {
                            "play_addr": {
                                "url_list": [
                                    "https://example.com/wrong.mp4",
                                ]
                            }
                        },
                    },
                    {
                        "aweme_id": "2222222222222222222",
                        "desc": "target",
                        "author": {"nickname": "target-author"},
                        "video": {
                            "play_addr": {
                                "url_list": [
                                    "https://example.com/target.mp4",
                                ]
                            }
                        },
                    },
                ]
            },
        )
    )

    candidates = provider.sort_candidates(accumulator.candidates)

    assert accumulator.metadata.aweme_id == "2222222222222222222"
    assert accumulator.metadata.desc == "target"
    assert accumulator.metadata.author == "target-author"
    assert [candidate.url for candidate in candidates] == ["https://example.com/target.mp4"]


def test_douyin_provider_prefers_target_json_candidates_over_network_noise():
    provider = get_provider_by_key("douyin")
    accumulator = ExtractionAccumulator(
        metadata=provider.create_metadata("https://www.douyin.com/jingxuan?modal_id=3333333333333333333"),
        provider=provider,
    )

    accumulator.ingest_event(
        ObservedEvent(
            url="https://example.com/network-noise.mp4?watermark=0",
            content_type="video/mp4",
            origin="response",
        )
    )
    accumulator.ingest_event(
        ObservedEvent(
            url="https://www.douyin.com/aweme/v1/web/feed/",
            content_type="application/json",
            json_body={
                "aweme_list": [
                    {
                        "aweme_id": "3333333333333333333",
                        "author": {"nickname": "target-author"},
                        "video": {
                            "bit_rate": [
                                {
                                    "play_addr": {
                                        "url_list": [
                                            "https://example.com/target-video-avc1.mp4?watermark=0",
                                        ]
                                    }
                                }
                            ]
                        },
                    }
                ]
            },
            origin="response",
        )
    )

    candidates = provider.sort_candidates(accumulator.candidates)

    assert candidates[0].url == "https://example.com/target-video-avc1.mp4?watermark=0"


def test_douyin_dom_author_normalizes_leading_at_sign():
    provider = get_provider_by_key("douyin")
    metadata = provider.create_metadata("https://www.douyin.com/jingxuan?modal_id=4444444444444444444")

    provider.apply_dom_snapshot(
        metadata,
        {
            "page_url": "https://www.douyin.com/jingxuan?modal_id=4444444444444444444",
            "title": "示例标题 - 抖音",
            "author_text": "@宋可为",
            "video_src": "",
            "og_video": "",
            "description": "",
            "og_description": "",
            "og_title": "",
        },
        lambda *args, **kwargs: None,
    )

    assert metadata.author == "宋可为"


def test_douyin_provider_prefers_runtime_request_stream_over_ssr_prefetch():
    provider = get_provider_by_key("douyin")
    accumulator = ExtractionAccumulator(
        metadata=provider.create_metadata("https://www.douyin.com/jingxuan?modal_id=5555555555555555555"),
        provider=provider,
    )

    accumulator.ingest_event(
        ObservedEvent(
            url="https://example.com/video/tos/cn/tos-cn-vd-0026/media-video-hvc1/?is_ssr=1&mime_type=video_mp4",
            content_type="video/mp4",
            origin="request",
        )
    )
    accumulator.ingest_event(
        ObservedEvent(
            url="https://example.com/video/tos/cn/tos-cn-ve-15/media-video-hvc1/?policy=4&signature=abc&tk=webid&fid=file&expire=123&mime_type=video_mp4",
            content_type="video/mp4",
            origin="request",
        )
    )

    candidates = provider.sort_candidates(accumulator.candidates)

    assert "policy=4" in candidates[0].url

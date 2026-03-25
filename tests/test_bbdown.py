from pathlib import Path

from videocp.bbdown import (
    BilibiliPageInfo,
    TV_API_HEADERS,
    bbdown_tv_token_path,
    download_bilibili_with_bbdown,
    fetch_bilibili_page_info,
    fetch_bilibili_tv_candidates,
    infer_bbdown_select_page,
    load_bbdown_tv_token,
    save_bbdown_tv_token,
)
from videocp.browser import BrowserConfig
from videocp.models import DownloadArtifact, MediaCandidate, MediaKind, TrackType, VideoMetadata, WatermarkMode


def test_infer_bbdown_select_page_defaults_to_first_page():
    assert infer_bbdown_select_page("https://www.bilibili.com/video/BV1764y1y76G/") == "1"
    assert infer_bbdown_select_page("https://www.bilibili.com/video/BV1764y1y76G/?p=3") == "3"


def test_save_and_load_bbdown_tv_token(tmp_path: Path):
    profile_dir = tmp_path / "profile"
    save_bbdown_tv_token(profile_dir, "token-123")

    assert bbdown_tv_token_path(profile_dir).read_text(encoding="utf-8").strip() == "access_token=token-123"
    assert load_bbdown_tv_token(profile_dir) == "token-123"


def test_fetch_bilibili_page_info_selects_requested_page(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "code": 0,
                "data": {
                    "aid": 123,
                    "bvid": "BV1764y1y76G",
                    "cid": 1001,
                    "title": "主标题",
                    "desc": "简介",
                    "owner": {"name": "UP主"},
                    "pages": [
                        {"page": 1, "cid": 1001, "part": "P1"},
                        {"page": 2, "cid": 1002, "part": "P2"},
                    ],
                },
            }

    monkeypatch.setattr("videocp.bbdown.requests.get", lambda *args, **kwargs: FakeResponse())

    info = fetch_bilibili_page_info("https://www.bilibili.com/video/BV1764y1y76G/?p=2", timeout_secs=15)

    assert info.aid == "123"
    assert info.bvid == "BV1764y1y76G"
    assert info.cid == "1002"
    assert info.page_index == 2
    assert info.author == "UP主"


def test_fetch_bilibili_tv_candidates_prefers_highest_quality_video_and_best_audio(monkeypatch):
    monkeypatch.setattr(
        "videocp.bbdown._fetch_bilibili_tv_playinfo",
        lambda page_info, token, timeout_secs: {
            "code": 0,
            "data": {
                "dash": {
                    "video": [
                        {"id": 80, "bandwidth": 1000, "base_url": "https://cdn.example.com/v80.m4s"},
                        {"id": 120, "bandwidth": 3000, "base_url": "https://cdn.example.com/v120.m4s"},
                    ],
                    "audio": [
                        {"id": 30216, "bandwidth": 64000, "base_url": "https://cdn.example.com/a-low.m4s"},
                        {"id": 30280, "bandwidth": 192000, "base_url": "https://cdn.example.com/a-high.m4s"},
                    ],
                }
            },
        },
    )

    candidates = fetch_bilibili_tv_candidates(
        BilibiliPageInfo(
            aid="123",
            bvid="BV1764y1y76G",
            cid="1002",
            page_index=2,
            title="主标题",
            desc="简介",
            author="UP主",
        ),
        token="secret",
        timeout_secs=15,
    )

    assert [candidate.track_type for candidate in candidates] == [
        TrackType.VIDEO_ONLY,
        TrackType.VIDEO_ONLY,
        TrackType.AUDIO_ONLY,
    ]
    assert candidates[0].url == "https://cdn.example.com/v120.m4s"
    assert candidates[-1].url == "https://cdn.example.com/a-high.m4s"


def test_fetch_bilibili_tv_candidates_prefers_avc_at_same_quality(monkeypatch):
    monkeypatch.setattr(
        "videocp.bbdown._fetch_bilibili_tv_playinfo",
        lambda page_info, token, timeout_secs: {
            "code": 0,
            "data": {
                "dash": {
                    "video": [
                        {"id": 80, "codecid": 12, "codecs": "hev1.1.6.L150.90", "bandwidth": 3000, "base_url": "https://cdn.example.com/v80-hevc.m4s"},
                        {"id": 80, "codecid": 7, "codecs": "avc1.640032", "bandwidth": 2500, "base_url": "https://cdn.example.com/v80-avc.m4s"},
                        {"id": 80, "codecid": 13, "codecs": "av01.0.00M.10.0.110.01.01.01.0", "bandwidth": 2800, "base_url": "https://cdn.example.com/v80-av1.m4s"},
                    ],
                    "audio": [
                        {"id": 30280, "bandwidth": 192000, "base_url": "https://cdn.example.com/a-high.m4s"},
                    ],
                }
            },
        },
    )

    candidates = fetch_bilibili_tv_candidates(
        BilibiliPageInfo(
            aid="123",
            bvid="BV1764y1y76G",
            cid="1002",
            page_index=2,
            title="主标题",
            desc="简介",
            author="UP主",
        ),
        token="secret",
        timeout_secs=15,
    )

    assert candidates[0].url == "https://cdn.example.com/v80-avc.m4s"


def test_download_bilibili_with_bbdown_uses_python_tv_pipeline(tmp_path: Path, monkeypatch):
    profile_dir = tmp_path / "profile"
    browser_config = BrowserConfig(
        profile_dir=profile_dir,
        browser_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        cdp_url="http://127.0.0.1:9222",
        headless=False,
    )

    monkeypatch.setattr("videocp.bbdown.ensure_bbdown_tv_token", lambda browser_config, timeout_secs: "secret-token")
    monkeypatch.setattr(
        "videocp.bbdown.fetch_bilibili_page_info",
        lambda source_url, timeout_secs, author_hint="": BilibiliPageInfo(
            aid="123",
            bvid="BV1764y1y76G",
            cid="1002",
            page_index=2,
            title="主标题",
            desc="简介",
            author="UP主",
        ),
    )
    monkeypatch.setattr(
        "videocp.bbdown.fetch_bilibili_tv_candidates",
        lambda page_info, token, timeout_secs: [
            MediaCandidate(
                url="https://cdn.example.com/video.m4s",
                kind=MediaKind.MP4,
                track_type=TrackType.VIDEO_ONLY,
                watermark_mode=WatermarkMode.NO_WATERMARK,
                source="tv_api",
                observed_via="api",
            ),
            MediaCandidate(
                url="https://cdn.example.com/audio.m4s",
                kind=MediaKind.MP4,
                track_type=TrackType.AUDIO_ONLY,
                watermark_mode=WatermarkMode.NO_WATERMARK,
                source="tv_api",
                observed_via="api",
            ),
        ],
    )

    def fake_download_best_candidate(extraction, output_dir, timeout_secs, watermark=None):
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "BV1764y1y76G.mp4"
        sidecar_path = output_dir / "BV1764y1y76G.json"
        output_path.write_bytes(b"video")
        sidecar_path.write_text("{}", encoding="utf-8")
        return DownloadArtifact(
            output_path=output_path,
            sidecar_path=sidecar_path,
            chosen_candidate=extraction.candidates[0],
            attempts=[{"mode": "tv_api", "status": "ok"}],
        )

    monkeypatch.setattr("videocp.bbdown.download_best_candidate", fake_download_best_candidate)

    extraction, artifact = download_bilibili_with_bbdown(
        source_url="https://www.bilibili.com/video/BV1764y1y76G/?p=2",
        browser_config=browser_config,
        output_dir=tmp_path / "downloads",
        timeout_secs=30,
        metadata_seed=VideoMetadata(
            source_url="https://www.bilibili.com/video/BV1764y1y76G/?p=2",
            site="bilibili",
            canonical_url="https://www.bilibili.com/video/BV1764y1y76G/?p=2",
            page_url="https://www.bilibili.com/video/BV1764y1y76G/?p=2",
            aweme_id="BV1764y1y76G",
            author="测试UP主",
            desc="示例简介",
            title="示例标题",
        ),
    )

    assert extraction.metadata.aweme_id == "BV1764y1y76G"
    assert extraction.metadata.author == "UP主"
    assert extraction.diagnostics["downloader"] == "bbdown_python_tv"
    assert artifact.output_path.is_file()


def test_login_tv_in_browser_uses_tv_headers(monkeypatch, tmp_path: Path):
    captured_headers: list[dict[str, str] | None] = []
    captured_html: list[str] = []

    class FakeResponse:
        def __init__(self, text: str = "", payload: dict | None = None):
            self.text = text
            self._payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_post(url, data=None, headers=None, timeout=None):
        captured_headers.append(headers)
        if "auth_code" in url:
            return FakeResponse(
                text='{"code":0,"data":{"url":"https://b23.snm0516.aisee.tv/test","auth_code":"abc"}}',
            )
        return FakeResponse(payload={"code": 0, "data": {"access_token": "token-123"}})

    class FakePage:
        def set_viewport_size(self, _size):
            return None

        def set_content(self, _html, wait_until=None):
            captured_html.append(_html)
            return None

        def evaluate(self, _script, _arg=None):
            return None

        def wait_for_timeout(self, _ms):
            return None

    class FakeBrowser:
        def new_page(self):
            return FakePage()

    class FakeBrowserSession:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return FakeBrowser()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("videocp.bbdown.requests.post", fake_post)
    monkeypatch.setattr("videocp.bbdown.BrowserSession", FakeBrowserSession)
    monkeypatch.setattr("videocp.bbdown.find_free_local_port", lambda: 9222)

    from videocp.bbdown import _login_tv_in_browser

    token = _login_tv_in_browser(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        timeout_secs=5,
    )

    assert token == "token-123"
    assert captured_headers == [TV_API_HEADERS, TV_API_HEADERS]
    assert 'data:image/svg+xml;base64,' in captured_html[0]

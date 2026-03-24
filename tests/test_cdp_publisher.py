from __future__ import annotations

import pytest

from videocp.cdp_publisher import (
    UploadNetworkTracker,
    UploadRequestInfo,
    _build_feed_detail_url,
    _classify_upload_request,
    _extract_publish_outcome,
    _looks_like_uploaded_video_detail,
    _looks_like_publish_request,
    _set_video_input_files,
    _wait_for_publish,
    _wait_for_upload,
)


class FakePage:
    def __init__(self, evaluate_results):
        self.evaluate_results = list(evaluate_results)
        self.wait_calls: list[int] = []

    def evaluate(self, script):
        del script
        if not self.evaluate_results:
            raise AssertionError("No more evaluate() results queued")
        return self.evaluate_results.pop(0)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_calls.append(timeout_ms)


class FakeLocator:
    def __init__(self):
        self.wait_args: list[tuple[str | None, int | None]] = []
        self.files: list[str] = []

    @property
    def first(self):
        return self

    def wait_for(self, state: str | None = None, timeout: int | None = None) -> None:
        self.wait_args.append((state, timeout))

    def set_input_files(self, path: str) -> None:
        self.files.append(path)


class FakePageForFileInput:
    def __init__(self):
        self.locator_calls: list[str] = []
        self.file_locator = FakeLocator()

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        return self.file_locator


def monotonic_sequence(values: list[float]):
    iterator = iter(values)
    last = values[-1]

    def _fake_monotonic():
        nonlocal last
        try:
            last = next(iterator)
        except StopIteration:
            pass
        return last

    return _fake_monotonic


def test_looks_like_publish_request_accepts_body_tokens_when_url_changes():
    assert _looks_like_publish_request(
        "https://pd.qq.com/trpc/some/new/endpoint",
        '{"jsonFeed":"{}","title":"demo"}',
    ) is True


def test_looks_like_publish_request_accepts_publish_url():
    assert _looks_like_publish_request(
        "https://pd.qq.com/trpc/feed/publish",
        "",
    ) is True


def test_looks_like_publish_request_rejects_telemetry_noise():
    assert _looks_like_publish_request(
        "https://galileotelemetry.tencent.com/collect",
        '{"feed_type":2,"clientContent":"x"}',
    ) is False


def test_extract_publish_outcome_handles_nested_success():
    outcome = _extract_publish_outcome(
        {
            "result": {"retCode": 0},
            "data": {"feedId": "B_demo"},
        },
        "",
    )

    assert outcome == {
        "ret": 0,
        "feed_id": "B_demo",
        "share_url": "",
        "error": "",
    }


def test_extract_publish_outcome_handles_retcode_and_nested_feed_id():
    outcome = _extract_publish_outcome(
        {
            "data": {"feed": {"id": "B_nested"}},
            "retcode": 0,
            "message": "",
        },
        "",
    )

    assert outcome == {
        "ret": 0,
        "feed_id": "B_nested",
        "share_url": "",
        "error": "",
    }


def test_extract_publish_outcome_handles_error_message():
    outcome = _extract_publish_outcome(
        {
            "result": {"retCode": 1001, "errMsg": "upload failed"},
        },
        "",
    )

    assert outcome == {
        "ret": 1001,
        "feed_id": "",
        "share_url": "",
        "error": "retCode=1001 upload failed",
    }


def test_looks_like_uploaded_video_detail_accepts_duration_text():
    assert _looks_like_uploaded_video_detail("09:58") is True
    assert _looks_like_uploaded_video_detail("  1:02:03 ") is True
    assert _looks_like_uploaded_video_detail("上传中 99.99%") is False


def test_build_feed_detail_url_uses_guild_path():
    assert _build_feed_detail_url(
        "https://pd.qq.com/g/657469764024457583",
        "B_demo",
    ) == "https://pd.qq.com/g/657469764024457583/post/B_demo"


def test_classify_upload_request_detects_video_apply():
    info = _classify_upload_request(
        "POST",
        "https://pd.qq.com/proxy/domain/richmedia.qq.com/ApplySliceUpload",
        '{"appid":1003,"business_type":2}',
        "fetch",
    )

    assert info == UploadRequestInfo(kind="apply", is_video=True, index=-1)


def test_upload_tracker_ignores_blob_preview_failures():
    tracker = UploadNetworkTracker(last_event_at=0.0)

    tracker.record_request(UploadRequestInfo(kind="blob_preview"), 1)
    tracker.record_failed(UploadRequestInfo(kind="blob_preview"), 1, "net::ERR_ABORTED")

    assert tracker.saw_blob_preview is True
    assert tracker.errors == []
    assert tracker.inflight_count == 0


def test_set_video_input_files_uses_attached_state(tmp_path):
    page = FakePageForFileInput()
    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(b"ok")

    _set_video_input_files(page, video_path)

    assert page.locator_calls == [".image-video-container input[type=file][accept*='video']"]
    assert page.file_locator.wait_args == [("attached", 5000)]
    assert page.file_locator.files == [str(video_path.resolve())]


def test_wait_for_upload_succeeds_once_preview_is_ready(monkeypatch):
    page = FakePage([
        {
            "text": "上传中 31%",
            "error_text": "",
            "preview_children": 0,
            "has_preview_content": False,
            "upload_busy": True,
        },
        {
            "text": "预览已生成",
            "error_text": "",
            "preview_children": 1,
            "has_preview_content": True,
            "upload_busy": False,
        },
    ])
    monkeypatch.setattr(
        "videocp.cdp_publisher.time.monotonic",
        monotonic_sequence([0.0, 0.1, 0.2, 0.3]),
    )

    _wait_for_upload(page, timeout_ms=5_000)

    assert page.wait_calls == [1000, 1000]


def test_wait_for_upload_fails_fast_on_error(monkeypatch):
    page = FakePage([
        {
            "text": "上传失败，请重试",
            "error_text": "上传失败，请重试",
            "preview_children": 0,
            "has_preview_content": False,
            "upload_busy": False,
        },
    ])
    monkeypatch.setattr(
        "videocp.cdp_publisher.time.monotonic",
        monotonic_sequence([0.0, 0.1]),
    )

    with pytest.raises(RuntimeError, match="上传失败，请重试"):
        _wait_for_upload(page, timeout_ms=5_000)


def test_wait_for_upload_succeeds_when_network_is_settled_and_duration_text_present(monkeypatch):
    page = FakePage([
        {
            "text": "09:58",
            "error_text": "",
            "preview_children": 0,
            "has_preview_content": False,
            "upload_busy": False,
        },
    ])
    tracker = UploadNetworkTracker(
        saw_apply=True,
        saw_video_apply=True,
        saw_slice=True,
        saw_blob_preview=False,
        last_event_at=0.0,
    )
    monkeypatch.setattr(
        "videocp.cdp_publisher.time.monotonic",
        monotonic_sequence([0.0, 1.1]),
    )

    _wait_for_upload(page, timeout_ms=5_000, tracker=tracker)

    assert page.wait_calls == [1000]


def test_wait_for_publish_succeeds_from_api_response(monkeypatch):
    page = FakePage([
        {
            "editor_empty": False,
            "preview_empty": False,
            "success_text": "",
            "error_text": "",
            "detail": "publishing",
        },
    ])
    monkeypatch.setattr(
        "videocp.cdp_publisher.time.monotonic",
        monotonic_sequence([0.0, 0.1]),
    )
    publish_responses = [
        {
            "url": "https://pd.qq.com/trpc/new-endpoint",
            "status": 200,
            "body": {"result": {"retCode": 0}, "data": {"feedId": "B_ok"}},
            "body_text": "",
            "request_post_data": '{"jsonFeed":"{}"}',
        }
    ]

    result = _wait_for_publish(page, timeout_ms=5_000, publish_responses=publish_responses)

    assert result.success is True
    assert result.feed_id == "B_ok"
    assert result.share_url == ""


def test_wait_for_publish_builds_detail_url_when_share_url_missing(monkeypatch):
    page = FakePage([
        {
            "editor_empty": False,
            "preview_empty": False,
            "success_text": "",
            "error_text": "",
            "detail": "publishing",
        },
    ])
    page.url = "https://pd.qq.com/g/657469764024457583"
    monkeypatch.setattr(
        "videocp.cdp_publisher.time.monotonic",
        monotonic_sequence([0.0, 0.1]),
    )
    publish_responses = [
        {
            "url": "https://pd.qq.com/qunng/guild/gotrpc/auth/trpc.qchannel.commwriter.ComWriter/PublishFeed",
            "status": 200,
            "body": {"data": {"feed": {"id": "B_nested"}}, "retcode": 0},
            "body_text": "",
            "request_post_data": '{"jsonFeed":"{}"}',
        }
    ]

    result = _wait_for_publish(
        page,
        timeout_ms=5_000,
        publish_responses=publish_responses,
        channel_url="https://pd.qq.com/g/657469764024457583",
    )

    assert result.success is True
    assert result.feed_id == "B_nested"
    assert result.share_url == "https://pd.qq.com/g/657469764024457583/post/B_nested"


def test_wait_for_publish_uses_dom_clear_as_fallback(monkeypatch):
    page = FakePage([
        {
            "editor_empty": False,
            "preview_empty": False,
            "success_text": "",
            "error_text": "",
            "detail": "submitting",
        },
        {
            "editor_empty": True,
            "preview_empty": True,
            "success_text": "",
            "error_text": "",
            "detail": "",
        },
    ])
    monkeypatch.setattr(
        "videocp.cdp_publisher.time.monotonic",
        monotonic_sequence([0.0, 0.1, 0.2, 0.3]),
    )

    result = _wait_for_publish(page, timeout_ms=5_000, publish_responses=[])

    assert result.success is True

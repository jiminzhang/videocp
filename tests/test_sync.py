from pathlib import Path

from videocp.browser import BrowserConfig
from videocp.config import AppConfig, SyncConfig, SyncTaskConfig, WatermarkConfig
from videocp.models import ParsedInput
from videocp.publisher import PublishResult
from videocp.sync import _sync_one_video
from videocp.sync_history import SyncHistory


def test_sync_skill_publish_uses_author_identity_even_with_channel_config(tmp_path: Path, monkeypatch):
    video_path = tmp_path / "downloads" / "video.mp4"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")

    monkeypatch.setattr("videocp.sync.find_processed_entry", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "videocp.sync._find_existing_download",
        lambda *args, **kwargs: {
            "output_path": video_path,
            "site": "youtube",
            "author": "author",
            "desc": "desc",
            "title": "video title",
            "content_id": "video-1",
        },
    )

    captured: dict = {}

    def fake_publish_to_channel(**kwargs):
        captured["guild_id"] = kwargs["guild_id"]
        captured["channel_id"] = kwargs["channel_id"]
        captured["feed_type"] = kwargs["feed_type"]
        return PublishResult(success=True, feed_id="feed-1", share_url="")

    monkeypatch.setattr("videocp.sync.publish_to_channel", fake_publish_to_channel)

    result = _sync_one_video(
        task=SyncTaskConfig(
            name="demo",
            source_url="https://example.com/profile",
            guild_id="123",
            channel_id="456",
            publish_method="skill",
        ),
        video_input=ParsedInput(
            raw_input="https://example.com/video/video-1",
            extracted_url="https://example.com/video/video-1",
            canonical_url="https://example.com/video/video-1",
            provider_key="youtube",
        ),
        app_cfg=AppConfig(
            output_dir=tmp_path / "downloads",
            profile_dir=tmp_path / "profile",
            browser_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=False,
            timeout_secs=30,
            max_concurrent=1,
            max_concurrent_per_site=1,
            start_interval_secs=0.0,
            watermark=WatermarkConfig(),
        ),
        sync_cfg=SyncConfig(
            history_file=tmp_path / "sync_history.json",
            skill_dir=tmp_path / "skill",
            tasks=[],
            publish_method="skill",
            skip_rate=0.0,
        ),
        browser_config=BrowserConfig(
            profile_dir=tmp_path / "profile",
            browser_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            cdp_url="http://127.0.0.1:9222",
            headless=False,
        ),
        history=SyncHistory(path=tmp_path / "sync_history.json"),
        dry_run=False,
    )

    assert result.ok is True
    assert result.action == "synced"
    assert captured == {"guild_id": "", "channel_id": "", "feed_type": 1}


def _make_sync_args(tmp_path, monkeypatch, *, task_skip_rate=-1, global_skip_rate=1.0, is_pinned=False):
    """Helper to build _sync_one_video args for skip_rate tests."""
    video_path = tmp_path / "downloads" / "video.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"video")

    monkeypatch.setattr("videocp.sync.find_processed_entry", lambda *a, **k: None)
    monkeypatch.setattr(
        "videocp.sync._find_existing_download",
        lambda *a, **k: {
            "output_path": video_path, "site": "test", "author": "a",
            "desc": "d", "title": "t", "content_id": "c1",
        },
    )
    monkeypatch.setattr(
        "videocp.sync.publish_to_channel",
        lambda **kw: PublishResult(success=True, feed_id="f1", share_url=""),
    )

    return dict(
        task=SyncTaskConfig(
            name="t", source_url="https://example.com/profile",
            guild_id="1", channel_id="2", publish_method="skill",
            skip_rate=task_skip_rate,
        ),
        video_input=ParsedInput(
            raw_input="https://example.com/v/c1",
            extracted_url="https://example.com/v/c1",
            canonical_url="https://example.com/v/c1",
            is_pinned=is_pinned,
        ),
        app_cfg=AppConfig(
            output_dir=tmp_path / "downloads", profile_dir=tmp_path / "p",
            browser_path="/usr/bin/chrome", headless=False, timeout_secs=30,
            max_concurrent=1, max_concurrent_per_site=1,
            start_interval_secs=0.0, watermark=WatermarkConfig(),
        ),
        sync_cfg=SyncConfig(
            history_file=tmp_path / "h.json", skill_dir=tmp_path / "s",
            tasks=[], publish_method="skill", skip_rate=global_skip_rate,
        ),
        browser_config=BrowserConfig(
            profile_dir=tmp_path / "p", browser_path="/usr/bin/chrome",
            cdp_url="http://127.0.0.1:9222", headless=False,
        ),
        history=SyncHistory(path=tmp_path / "h.json"),
        dry_run=False,
    )


def test_skip_rate_skips_when_random_below_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr("videocp.sync.random.random", lambda: 0.3)
    result = _sync_one_video(**_make_sync_args(tmp_path, monkeypatch, global_skip_rate=0.5))
    assert result.ok is True
    assert result.action == "skipped_random"


def test_skip_rate_syncs_when_random_above_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr("videocp.sync.random.random", lambda: 0.8)
    result = _sync_one_video(**_make_sync_args(tmp_path, monkeypatch, global_skip_rate=0.5))
    assert result.ok is True
    assert result.action == "synced"


def test_skip_rate_never_skips_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr("videocp.sync.random.random", lambda: 0.1)
    result = _sync_one_video(**_make_sync_args(tmp_path, monkeypatch, global_skip_rate=1.0, is_pinned=True))
    assert result.ok is True
    assert result.action == "synced_pinned"


def test_skip_rate_task_override(tmp_path, monkeypatch):
    monkeypatch.setattr("videocp.sync.random.random", lambda: 0.3)
    # Task skip_rate=0 should override global 1.0, so no skip
    result = _sync_one_video(**_make_sync_args(tmp_path, monkeypatch, task_skip_rate=0.0, global_skip_rate=1.0))
    assert result.ok is True
    assert result.action == "synced"

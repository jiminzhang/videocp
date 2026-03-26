import json
from pathlib import Path
from types import SimpleNamespace

from videocp.publisher import publish_to_channel


def test_publish_to_channel_uses_author_scope_when_ids_are_blank(tmp_path: Path, monkeypatch):
    skill_dir = tmp_path / "skill"
    script = skill_dir / "scripts" / "feed" / "write" / "publish_feed.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stub\n", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_subprocess_run(*args, **kwargs):
        captured["payload"] = json.loads(kwargs["input"])
        return SimpleNamespace(
            stdout=json.dumps({"success": True, "data": {"feed_id": "feed-1", "分享链接": ""}}, ensure_ascii=False),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("videocp.publisher.subprocess_run", fake_subprocess_run)

    result = publish_to_channel(
        skill_dir=skill_dir,
        video_path=tmp_path / "video.mp4",
        guild_id="",
        channel_id="",
        title="title",
        content="content",
    )

    assert result.success is True
    # Default feed_type=1 (short post): title omitted, content kept as-is
    assert captured["payload"] == {
        "guild_id": 0,
        "channel_id": 0,
        "content": "content",
        "feed_type": 1,
        "video_paths": [{"file_path": str((tmp_path / "video.mp4").resolve())}],
    }


def test_publish_short_post_moves_title_to_content_when_content_empty(tmp_path: Path, monkeypatch):
    skill_dir = tmp_path / "skill"
    script = skill_dir / "scripts" / "feed" / "write" / "publish_feed.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stub\n", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_subprocess_run(*args, **kwargs):
        captured["payload"] = json.loads(kwargs["input"])
        return SimpleNamespace(
            stdout=json.dumps({"success": True, "data": {"feed_id": "feed-2", "分享链接": ""}}, ensure_ascii=False),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("videocp.publisher.subprocess_run", fake_subprocess_run)

    result = publish_to_channel(
        skill_dir=skill_dir,
        video_path=tmp_path / "video.mp4",
        guild_id="",
        channel_id="",
        title="my video title",
        content="",
        feed_type=1,
    )

    assert result.success is True
    assert captured["payload"]["content"] == "my video title"
    assert "title" not in captured["payload"]
    assert captured["payload"]["feed_type"] == 1


def test_publish_long_post_keeps_title(tmp_path: Path, monkeypatch):
    skill_dir = tmp_path / "skill"
    script = skill_dir / "scripts" / "feed" / "write" / "publish_feed.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stub\n", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_subprocess_run(*args, **kwargs):
        captured["payload"] = json.loads(kwargs["input"])
        return SimpleNamespace(
            stdout=json.dumps({"success": True, "data": {"feed_id": "feed-3", "分享链接": ""}}, ensure_ascii=False),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("videocp.publisher.subprocess_run", fake_subprocess_run)

    result = publish_to_channel(
        skill_dir=skill_dir,
        video_path=tmp_path / "video.mp4",
        guild_id="",
        channel_id="",
        title="long post title",
        content="body text",
        feed_type=2,
    )

    assert result.success is True
    assert captured["payload"]["title"] == "long post title"
    assert captured["payload"]["content"] == "body text"
    assert captured["payload"]["feed_type"] == 2


def test_publish_parses_feed_id_from_legacy_key(tmp_path: Path, monkeypatch):
    skill_dir = tmp_path / "skill"
    script = skill_dir / "scripts" / "feed" / "write" / "publish_feed.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stub\n", encoding="utf-8")

    def fake_subprocess_run(*args, **kwargs):
        return SimpleNamespace(
            stdout=json.dumps({"success": True, "data": {"帖子ID": "legacy-id", "分享链接": "<https://example.com>"}}, ensure_ascii=False),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("videocp.publisher.subprocess_run", fake_subprocess_run)

    result = publish_to_channel(
        skill_dir=skill_dir,
        video_path=tmp_path / "video.mp4",
        guild_id="",
        channel_id="",
        title="",
        content="text",
    )

    assert result.success is True
    assert result.feed_id == "legacy-id"
    assert result.share_url == "https://example.com"

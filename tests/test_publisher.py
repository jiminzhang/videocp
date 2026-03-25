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
            stdout=json.dumps({"success": True, "data": {"帖子ID": "feed-1", "分享链接": ""}}, ensure_ascii=False),
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
    assert captured["payload"] == {
        "guild_id": 0,
        "channel_id": 0,
        "title": "title",
        "content": "content",
        "feed_type": 2,
        "video_paths": [{"file_path": str((tmp_path / "video.mp4").resolve())}],
    }

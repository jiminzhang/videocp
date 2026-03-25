from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from subprocess import run as subprocess_run

from videocp.errors import PublishError


@dataclass(slots=True)
class PublishResult:
    success: bool
    feed_id: str = ""
    share_url: str = ""
    error: str = ""


def _as_publish_scope_id(value: str) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    return int(raw)


def publish_to_channel(
    skill_dir: Path,
    video_path: Path,
    guild_id: str,
    channel_id: str,
    title: str,
    content: str,
    feed_type: int = 2,
    timeout_secs: int = 300,
) -> PublishResult:
    script = skill_dir / "scripts" / "feed" / "write" / "publish_feed.py"
    if not script.is_file():
        raise PublishError(f"publish_feed.py not found at {script}. Ensure skill is installed.")

    payload = {
        "guild_id": _as_publish_scope_id(guild_id),
        "channel_id": _as_publish_scope_id(channel_id),
        "title": title,
        "content": content,
        "feed_type": feed_type,
        "video_paths": [{"file_path": str(video_path.resolve())}],
    }

    cwd = str(skill_dir)
    env = {**os.environ}

    python = sys.executable

    try:
        proc = subprocess_run(
            [python, str(script)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout_secs,
            cwd=cwd,
            env=env,
        )
    except Exception as exc:
        raise PublishError(f"Failed to run publish_feed.py: {exc}") from exc

    stdout = proc.stdout.strip()
    if not stdout:
        stderr_hint = proc.stderr.strip()[:500] if proc.stderr else ""
        raise PublishError(f"publish_feed.py returned no output (exit {proc.returncode}). stderr: {stderr_hint}")

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        raise PublishError(f"publish_feed.py returned invalid JSON: {stdout[:200]}")

    if not result.get("success"):
        error_msg = result.get("error", "unknown error")
        needs_confirm = result.get("needs_confirm", False)
        if needs_confirm:
            raise PublishError(f"Upload requires confirmation: {error_msg}")
        return PublishResult(success=False, error=error_msg)

    data = result.get("data", {})
    feed_id = data.get("帖子ID", "")
    share_url = data.get("分享链接", "")
    # Clean up share_url (wrapped in angle brackets)
    if share_url.startswith("<") and share_url.endswith(">"):
        share_url = share_url[1:-1]

    return PublishResult(success=True, feed_id=feed_id, share_url=share_url)

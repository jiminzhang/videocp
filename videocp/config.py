from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from videocp.profile import default_profile_dir

CONFIG_FILENAME = "config.yaml"
TASKS_FILENAME = "tasks.yaml"


@dataclass(slots=True)
class WatermarkConfig:
    enabled: bool = False
    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1/chat/completions"
    model: str = "google/gemini-2.5-flash"


@dataclass(slots=True)
class AppConfig:
    output_dir: Path
    profile_dir: Path
    browser_path: str
    headless: bool
    timeout_secs: int
    max_concurrent: int
    max_concurrent_per_site: int
    start_interval_secs: float
    watermark: WatermarkConfig
    profile_videos_count: int = 3
    source_path: Path | None = None


def find_config_path(start_dir: Path | None = None) -> Path | None:
    current = (start_dir or Path.cwd()).resolve()
    while True:
        candidate = current / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _resolve_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"Invalid boolean value in {CONFIG_FILENAME}: {value!r}")


def _normalize_publish_method(value: Any, *, field_name: str, allow_empty: bool = False) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        if allow_empty:
            return ""
        return "skill"
    if normalized in {"skill", "cdp", "youtube"}:
        return normalized
    from videocp.errors import SyncError
    raise SyncError(f"Invalid {field_name}: {value!r}. Expected 'skill', 'cdp', or 'youtube'.")


def load_app_config(config_path: Path | None = None, start_dir: Path | None = None) -> AppConfig:
    resolved_path = config_path.expanduser().resolve() if config_path else find_config_path(start_dir)
    base_dir = resolved_path.parent if resolved_path is not None else (start_dir or Path.cwd()).resolve()

    payload: dict[str, Any] = {}
    if resolved_path is not None:
        if not resolved_path.is_file():
            raise ValueError(f"Config file not found: {resolved_path}")
        try:
            loaded = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {resolved_path}: {exc}") from exc
        payload = _as_mapping(loaded)

    download_config = _as_mapping(payload.get("download"))
    browser_config = _as_mapping(payload.get("browser"))
    request_config = _as_mapping(payload.get("request"))
    watermark_raw = _as_mapping(payload.get("watermark"))

    output_dir_value = download_config.get("output_dir", "./downloads")
    try:
        max_concurrent = int(download_config.get("max_concurrent", 1) or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid max_concurrent in {CONFIG_FILENAME}") from exc
    try:
        max_concurrent_per_site = int(download_config.get("max_concurrent_per_site", 1) or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid max_concurrent_per_site in {CONFIG_FILENAME}") from exc
    try:
        start_interval_secs = float(download_config.get("start_interval_secs", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid start_interval_secs in {CONFIG_FILENAME}") from exc
    try:
        profile_videos_count = int(download_config.get("profile_videos_count", 3) or 3)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid profile_videos_count in {CONFIG_FILENAME}") from exc
    profile_dir_value = browser_config.get("profile_dir", str(default_profile_dir()))
    browser_path = str(browser_config.get("browser_path", "") or "").strip()
    headless = _as_bool(browser_config.get("headless", False), False)
    try:
        timeout_secs = int(request_config.get("timeout_secs", 30) or 30)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid timeout_secs in {CONFIG_FILENAME}") from exc

    watermark_api_key = str(watermark_raw.get("api_key", "") or "").strip()
    if not watermark_api_key:
        watermark_api_key = os.environ.get("OPENROUTER_API_KEY", "")
    watermark = WatermarkConfig(
        enabled=_as_bool(watermark_raw.get("enabled", False), False),
        api_key=watermark_api_key,
        base_url=str(watermark_raw.get("base_url", WatermarkConfig.base_url) or WatermarkConfig.base_url).strip(),
        model=str(watermark_raw.get("model", WatermarkConfig.model) or WatermarkConfig.model).strip(),
    )

    return AppConfig(
        output_dir=_resolve_path(output_dir_value, base_dir),
        profile_dir=_resolve_path(profile_dir_value, base_dir),
        browser_path=browser_path,
        headless=headless,
        timeout_secs=timeout_secs,
        max_concurrent=max(1, max_concurrent),
        max_concurrent_per_site=max(1, max_concurrent_per_site),
        start_interval_secs=max(0.0, start_interval_secs),
        watermark=watermark,
        profile_videos_count=max(1, profile_videos_count),
        source_path=resolved_path,
    )


@dataclass(slots=True)
class SyncTaskConfig:
    name: str
    source_url: str
    guild_id: str
    channel_id: str
    title_template: str = "{desc}"
    content_template: str = "{desc}"
    feed_type: int = 1
    count: int = 0  # 0 means use sync.videos_per_task default
    publish_method: str = ""  # "" = inherit from sync.publish_method; "skill" | "cdp"
    skip_rate: float = -1  # -1 = inherit from sync.skip_rate; 0.0–1.0 = probability of skipping each video


@dataclass(slots=True)
class SyncConfig:
    history_file: Path
    skill_dir: Path
    tasks: list[SyncTaskConfig]
    videos_per_task: int = 1
    publish_method: str = "skill"  # "skill" | "cdp"
    skip_rate: float = 0.5  # 0.0–1.0, probability of skipping each video


def find_tasks_path(start_dir: Path | None = None) -> Path | None:
    current = (start_dir or Path.cwd()).resolve()
    while True:
        candidate = current / TASKS_FILENAME
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def load_sync_config(tasks_path: Path | None = None, start_dir: Path | None = None) -> SyncConfig:
    resolved_path = tasks_path.expanduser().resolve() if tasks_path else find_tasks_path(start_dir)
    if resolved_path is None or not resolved_path.is_file():
        from videocp.errors import SyncError
        raise SyncError(f"{TASKS_FILENAME} not found. Create it alongside {CONFIG_FILENAME}.")

    base_dir = resolved_path.parent
    try:
        loaded = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        from videocp.errors import SyncError
        raise SyncError(f"Invalid YAML in {resolved_path}: {exc}") from exc

    payload = _as_mapping(loaded)
    sync_raw = _as_mapping(payload.get("sync"))
    tasks_raw = payload.get("tasks", [])
    if not isinstance(tasks_raw, list):
        from videocp.errors import SyncError
        raise SyncError(f"'tasks' must be a list in {TASKS_FILENAME}.")

    history_file = _resolve_path(sync_raw.get("history_file", "./sync_history.json"), base_dir)
    skill_dir = _resolve_path(sync_raw.get("skill_dir", "~/.openclaw/workspace/skills/tencent-channel-community"), base_dir)
    videos_per_task = max(1, int(sync_raw.get("videos_per_task", 1) or 1))
    global_publish_method = _normalize_publish_method(
        sync_raw.get("publish_method", "skill"),
        field_name="sync.publish_method",
    )
    global_skip_rate = float(sync_raw.get("skip_rate", 0.5))

    tasks: list[SyncTaskConfig] = []
    for i, raw in enumerate(tasks_raw):
        if not isinstance(raw, dict):
            from videocp.errors import SyncError
            raise SyncError(f"Task #{i + 1} must be a mapping in {TASKS_FILENAME}.")
        name = str(raw.get("name", "")).strip()
        source_url = str(raw.get("source_url", "")).strip()
        guild_id = str(raw.get("guild_id", "")).strip()
        channel_id = str(raw.get("channel_id", "")).strip()
        task_publish_method = _normalize_publish_method(
            raw.get("publish_method", ""),
            field_name=f"tasks[{i}].publish_method",
            allow_empty=True,
        )
        effective_publish_method = task_publish_method or global_publish_method

        missing_fields: list[str] = []
        if not name:
            missing_fields.append("name")
        if not source_url:
            missing_fields.append("source_url")
        if effective_publish_method == "cdp" and not guild_id:
            missing_fields.append("guild_id")
        if missing_fields:
            from videocp.errors import SyncError
            raise SyncError(f"Task #{i + 1} missing required fields ({', '.join(missing_fields)}).")
        tasks.append(SyncTaskConfig(
            name=name,
            source_url=source_url,
            guild_id=guild_id,
            channel_id=channel_id,
            title_template=str(raw.get("title_template", "{desc}")),
            content_template=str(raw.get("content_template", "{title}")),
            feed_type=int(raw.get("feed_type", 1)),
            count=int(raw.get("count", 0) or 0),
            publish_method=task_publish_method,
            skip_rate=float(raw.get("skip_rate", -1)),
        ))

    return SyncConfig(history_file=history_file, skill_dir=skill_dir, tasks=tasks, videos_per_task=videos_per_task, publish_method=global_publish_method, skip_rate=global_skip_rate)

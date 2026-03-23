from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from videocp.profile import default_profile_dir

CONFIG_FILENAME = "config.yaml"


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

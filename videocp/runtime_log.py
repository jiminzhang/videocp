from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_LOG_LOCK = threading.Lock()
_LEVELS = {
    "quiet": 0,
    "warn": 1,
    "info": 2,
}


@dataclass(frozen=True, slots=True)
class LogText:
    text: str
    truncate: bool = True


def _current_level() -> int:
    raw = (os.environ.get("VIDEOCP_LOG_LEVEL") or "info").strip().lower()
    return _LEVELS.get(raw, _LEVELS["info"])


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _truncate(value: str, limit: int = 120) -> str:
    text = _normalize_text(value)
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def summarize_url(url: str, *, path_limit: int = 72) -> LogText:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return LogText(_normalize_text(url), truncate=False)
    path = parsed.path or "/"
    if len(path) > path_limit:
        path = f"{path[: path_limit - 3]}..."
    suffix = "?..." if parsed.query else ""
    return LogText(f"{parsed.scheme}://{parsed.netloc}{path}{suffix}", truncate=False)


def full_url(url: str) -> LogText:
    return LogText(_normalize_text(url), truncate=False)


def _format_value(value: object) -> str:
    truncate = True
    if isinstance(value, LogText):
        text = value.text
        truncate = value.truncate
    elif isinstance(value, Path):
        text = str(value)
    elif isinstance(value, float):
        text = f"{value:.2f}".rstrip("0").rstrip(".")
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    text = _truncate(text) if truncate else _normalize_text(text)
    if not text:
        return ""
    if any(char.isspace() for char in text) or any(char in text for char in '"='):
        return json.dumps(text, ensure_ascii=False)
    return text


def _log(level: str, event: str, **fields: object) -> None:
    threshold = _LEVELS.get(level, _LEVELS["info"])
    if _current_level() < threshold:
        return
    timestamp = time.strftime("%H:%M:%S")
    parts = [f"[{timestamp}] [{level.upper()}] {event}"]
    field_parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        formatted = _format_value(value)
        if not formatted:
            continue
        field_parts.append(f"{key}={formatted}")
    if field_parts:
        parts.append(" ".join(field_parts))
    with _LOG_LOCK:
        sys.stderr.write(" ".join(parts) + "\n")
        sys.stderr.flush()


def log_info(event: str, **fields: object) -> None:
    _log("info", event, **fields)


def log_warn(event: str, **fields: object) -> None:
    _log("warn", event, **fields)

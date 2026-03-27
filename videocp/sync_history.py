from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SyncHistoryEntry:
    task_name: str
    content_id: str
    site: str
    author: str
    desc: str
    output_path: str
    feed_id: str = ""
    share_url: str = ""
    synced_at: str = ""
    status: str = "ok"
    error: str = ""


@dataclass(slots=True)
class SyncHistory:
    path: Path
    entries: list[SyncHistoryEntry] = field(default_factory=list)


def load_history(path: Path) -> SyncHistory:
    if not path.is_file():
        return SyncHistory(path=path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = [
            SyncHistoryEntry(**{k: v for k, v in raw.items() if k in SyncHistoryEntry.__slots__})
            for raw in data.get("entries", [])
        ]
        return SyncHistory(path=path, entries=entries)
    except (json.JSONDecodeError, KeyError, TypeError):
        return SyncHistory(path=path)


def is_synced(history: SyncHistory, task_name: str, content_id: str) -> bool:
    return any(
        e.task_name == task_name and e.content_id == content_id and e.status == "ok"
        for e in history.entries
    )


def find_processed_entry(history: SyncHistory, task_name: str, content_id: str) -> SyncHistoryEntry | None:
    final_statuses = {"ok", "skipped_unavailable", "skipped_random"}
    for entry in reversed(history.entries):
        if entry.task_name == task_name and entry.content_id == content_id and entry.status in final_statuses:
            return entry
    return None


def add_entry(history: SyncHistory, entry: SyncHistoryEntry) -> None:
    if not entry.synced_at:
        entry.synced_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
    history.entries.append(entry)
    _save(history)


def _save(history: SyncHistory) -> None:
    payload: dict[str, Any] = {
        "version": 1,
        "entries": [asdict(e) for e in history.entries],
    }
    history.path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = history.path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, history.path)

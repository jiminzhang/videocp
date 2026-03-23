from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class MediaKind(str, Enum):
    MP4 = "mp4"
    HLS = "hls"


class TrackType(str, Enum):
    MUXED = "muxed"
    VIDEO_ONLY = "video_only"
    AUDIO_ONLY = "audio_only"
    UNKNOWN = "unknown"


class WatermarkMode(str, Enum):
    NO_WATERMARK = "no_watermark"
    WATERMARK = "watermark"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ParsedInput:
    raw_input: str
    extracted_url: str
    canonical_url: str
    provider_key: str = ""
    is_profile: bool = False


@dataclass(slots=True)
class MediaCandidate:
    url: str
    kind: MediaKind
    track_type: TrackType
    watermark_mode: WatermarkMode
    source: str
    observed_via: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["track_type"] = self.track_type.value
        data["watermark_mode"] = self.watermark_mode.value
        return data


@dataclass(slots=True)
class VideoMetadata:
    source_url: str
    site: str = ""
    canonical_url: str = ""
    page_url: str = ""
    aweme_id: str = ""
    author: str = ""
    desc: str = ""
    title: str = ""

    @property
    def content_id(self) -> str:
        return self.aweme_id

    @content_id.setter
    def content_id(self, value: str) -> None:
        self.aweme_id = value

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["content_id"] = self.content_id
        return data


@dataclass(slots=True)
class ObservedEvent:
    url: str
    status: int | None = None
    resource_type: str = ""
    content_type: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    json_body: Any = None
    origin: str = "response"


@dataclass(slots=True)
class ExtractionResult:
    metadata: VideoMetadata
    candidates: list[MediaCandidate]
    cookies: list[dict[str, Any]]
    user_agent: str
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "diagnostics": self.diagnostics,
        }


@dataclass(slots=True)
class DownloadArtifact:
    output_path: Path
    sidecar_path: Path
    chosen_candidate: MediaCandidate
    attempts: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": str(self.output_path),
            "sidecar_path": str(self.sidecar_path),
            "chosen_candidate": self.chosen_candidate.to_dict(),
            "attempts": list(self.attempts),
        }


@dataclass(slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

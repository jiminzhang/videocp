from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from videocp.errors import ExtractionError
from videocp.models import (
    ExtractionResult,
    MediaCandidate,
    MediaKind,
    ObservedEvent,
    TrackType,
    VideoMetadata,
    WatermarkMode,
)

AWEME_ID_RE = re.compile(r"/video/(\d+)")
JSON_HINTS = ("aweme", "detail", "iteminfo", "web/api", "feed")
MEDIA_HINTS = ("video", "play", "download", ".mp4", ".m3u8")
SAFE_HEADER_KEYS = {"content-type", "content-length", "content-range", "accept-ranges"}


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items() if key.lower() in SAFE_HEADER_KEYS}


def infer_media_kind(url: str, content_type: str = "") -> MediaKind | None:
    lowered_url = url.lower()
    lowered_type = content_type.lower()
    if lowered_url.endswith(".m3u8") or "mpegurl" in lowered_type or "application/x-mpegurl" in lowered_type:
        return MediaKind.HLS
    if lowered_url.endswith(".mp4") or lowered_type.startswith("video/mp4"):
        return MediaKind.MP4
    if "video/tos" in lowered_url and "bytevc1" in lowered_url:
        return MediaKind.MP4
    return None


def infer_watermark_mode(url: str, semantic_tag: str = "") -> WatermarkMode:
    lowered = url.lower()
    tag = semantic_tag.lower()
    if "playwm" in lowered or "watermark=1" in lowered or "download_addr" in tag:
        return WatermarkMode.WATERMARK
    if "play_addr" in tag or "bit_rate" in tag or "play/" in lowered or "watermark=0" in lowered:
        return WatermarkMode.NO_WATERMARK
    return WatermarkMode.UNKNOWN


def infer_track_type(url: str, kind: MediaKind, semantic_tag: str = "") -> TrackType:
    lowered = url.lower()
    tag = semantic_tag.lower()
    if kind == MediaKind.HLS:
        return TrackType.MUXED
    has_audio_markers = any(token in lowered for token in ("media-audio-", "audio-und", "mp4a"))
    has_video_markers = any(token in lowered for token in ("media-video-", "avc1", "hvc1", "bytevc1"))
    if has_audio_markers and not has_video_markers:
        return TrackType.AUDIO_ONLY
    if has_video_markers and not has_audio_markers:
        return TrackType.VIDEO_ONLY
    if any(token in tag for token in ("play_addr", "download_addr", "bit_rate")):
        return TrackType.MUXED
    return TrackType.UNKNOWN


def candidate_rank(candidate: MediaCandidate) -> tuple[int, int, int, int, str]:
    watermark_rank = 0 if candidate.watermark_mode == WatermarkMode.NO_WATERMARK else 1
    track_rank = {
        TrackType.MUXED: 0,
        TrackType.VIDEO_ONLY: 1,
        TrackType.UNKNOWN: 2,
        TrackType.AUDIO_ONLY: 3,
    }[candidate.track_type]
    kind_rank = 0 if candidate.kind == MediaKind.MP4 else 1
    source_rank = 1 if candidate.source == "rewrite" else 0
    return (watermark_rank, track_rank, kind_rank, source_rank, candidate.url)


@dataclass(slots=True)
class ExtractionAccumulator:
    metadata: VideoMetadata
    candidates: list[MediaCandidate] = field(default_factory=list)
    seen_urls: set[str] = field(default_factory=set)
    event_count: int = 0
    json_event_count: int = 0

    def add_candidate(
        self,
        url: str,
        source: str,
        observed_via: str,
        *,
        semantic_tag: str = "",
        content_type: str = "",
        note: str = "",
    ) -> None:
        kind = infer_media_kind(url, content_type)
        if kind is None:
            return
        normalized = normalize_candidate_url(url)
        if normalized in self.seen_urls:
            return
        self.seen_urls.add(normalized)
        self.candidates.append(
            MediaCandidate(
                url=url,
                kind=kind,
                track_type=infer_track_type(url, kind, semantic_tag=semantic_tag),
                watermark_mode=infer_watermark_mode(url, semantic_tag=semantic_tag),
                source=source,
                observed_via=observed_via,
                note=note,
            )
        )

    def ingest_event(self, event: ObservedEvent) -> None:
        self.event_count += 1
        self.add_candidate(
            event.url,
            source=event.origin,
            observed_via="network",
            content_type=event.content_type,
        )
        if event.json_body is not None:
            self.json_event_count += 1
            self._scan_json(event.json_body)

    def ingest_dom_snapshot(self, snapshot: dict[str, str]) -> None:
        self.metadata.page_url = snapshot.get("page_url", self.metadata.page_url)
        self.metadata.title = snapshot.get("title", self.metadata.title)
        if not self.metadata.desc:
            self.metadata.desc = snapshot.get("og_title", "") or snapshot.get("title", "")
        aweme_match = AWEME_ID_RE.search(self.metadata.page_url or self.metadata.canonical_url)
        if aweme_match and not self.metadata.aweme_id:
            self.metadata.aweme_id = aweme_match.group(1)
        for key in ("video_src", "og_video"):
            value = snapshot.get(key, "")
            if value:
                self.add_candidate(value, source="dom", observed_via="dom", note=key)

    def _scan_json(self, payload: Any, path: str = "$") -> None:
        if isinstance(payload, dict):
            if not self.metadata.aweme_id:
                aweme_id = payload.get("aweme_id") or payload.get("group_id")
                if isinstance(aweme_id, str):
                    self.metadata.aweme_id = aweme_id
            if not self.metadata.desc and isinstance(payload.get("desc"), str):
                self.metadata.desc = payload["desc"]
            author = payload.get("author")
            if isinstance(author, dict) and not self.metadata.author:
                nickname = author.get("nickname")
                if isinstance(nickname, str):
                    self.metadata.author = nickname
            for key, value in payload.items():
                next_path = f"{path}.{key}"
                self._scan_known_media_nodes(key, value, next_path)
                self._scan_json(value, next_path)
        elif isinstance(payload, list):
            for index, item in enumerate(payload):
                self._scan_json(item, f"{path}[{index}]")

    def _scan_known_media_nodes(self, key: str, value: Any, path: str) -> None:
        if key in {"play_addr", "play_addr_h264", "play_addr_265", "download_addr", "play_addr_lowbr"}:
            url_list = value.get("url_list") if isinstance(value, dict) else None
            if isinstance(url_list, list):
                for item in url_list:
                    if isinstance(item, str):
                        self.add_candidate(
                            item,
                            source="json",
                            observed_via="json",
                            semantic_tag=path,
                            note=path,
                        )
        if key == "bit_rate" and isinstance(value, list):
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    continue
                play_addr = item.get("play_addr")
                if isinstance(play_addr, dict):
                    url_list = play_addr.get("url_list")
                    if isinstance(url_list, list):
                        for candidate_url in url_list:
                            if isinstance(candidate_url, str):
                                self.add_candidate(
                                    candidate_url,
                                    source="json",
                                    observed_via="json",
                                    semantic_tag=f"{path}[{index}].play_addr",
                                    note=f"{path}[{index}].play_addr",
                                )


def normalize_candidate_url(url: str) -> str:
    return url.strip()


def conservative_rewrites(candidates: list[MediaCandidate]) -> list[MediaCandidate]:
    if any(candidate.watermark_mode == WatermarkMode.NO_WATERMARK for candidate in candidates):
        return candidates
    rewritten = list(candidates)
    seen = {normalize_candidate_url(candidate.url) for candidate in candidates}
    for candidate in candidates:
        if candidate.kind != MediaKind.MP4:
            continue
        new_url = candidate.url
        if "playwm" in new_url:
            new_url = new_url.replace("playwm", "play")
        parsed = urlparse(new_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        changed = False
        if query.get("watermark") == ["1"]:
            query["watermark"] = ["0"]
            changed = True
        if new_url != candidate.url or changed:
            if changed:
                new_url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
            normalized = normalize_candidate_url(new_url)
            if normalized in seen:
                continue
            seen.add(normalized)
            rewritten.append(
                MediaCandidate(
                    url=new_url,
                    kind=candidate.kind,
                    track_type=candidate.track_type,
                    watermark_mode=WatermarkMode.NO_WATERMARK,
                    source="rewrite",
                    observed_via=candidate.observed_via,
                    note=f"rewrite:{candidate.url}",
                )
            )
    return rewritten


def sort_candidates(candidates: list[MediaCandidate]) -> list[MediaCandidate]:
    return sorted(conservative_rewrites(candidates), key=candidate_rank)


def should_parse_json(url: str, content_type: str) -> bool:
    lowered_url = url.lower()
    lowered_type = content_type.lower()
    return "application/json" in lowered_type or any(hint in lowered_url for hint in JSON_HINTS)


def capture_dom_snapshot(page: Page) -> dict[str, str]:
    return page.evaluate(
        """() => {
            const video = document.querySelector("video");
            const ogTitle = document.querySelector('meta[property="og:title"]')?.content || "";
            const ogVideo = document.querySelector('meta[property="og:video"]')?.content || "";
            if (video) {
              video.muted = true;
              video.play().catch(() => {});
            }
            return {
              page_url: window.location.href,
              title: document.title || "",
              og_title: ogTitle,
              og_video: ogVideo,
              video_src: video?.currentSrc || video?.src || "",
            };
        }"""
    )


def extract_video(page: Page, source_url: str, timeout_secs: int) -> ExtractionResult:
    accumulator = ExtractionAccumulator(metadata=VideoMetadata(source_url=source_url, canonical_url=source_url))

    def on_request(request) -> None:
        if any(hint in request.url.lower() for hint in MEDIA_HINTS):
            accumulator.ingest_event(
                ObservedEvent(
                    url=request.url,
                    resource_type=request.resource_type,
                    origin="request",
                )
            )

    def on_response(response) -> None:
        headers = {key.lower(): value for key, value in response.headers.items()}
        content_type = headers.get("content-type", "")
        json_body = None
        if should_parse_json(response.url, content_type):
            try:
                json_body = response.json()
            except Exception:
                json_body = None
        accumulator.ingest_event(
            ObservedEvent(
                url=response.url,
                status=response.status,
                resource_type=response.request.resource_type,
                content_type=content_type,
                headers=redact_headers(headers),
                json_body=json_body,
                origin="response",
            )
        )

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        page.goto(source_url, wait_until="domcontentloaded", timeout=timeout_secs * 1000)
    except PlaywrightTimeoutError as exc:
        raise ExtractionError(f"Navigation timed out for {source_url}: {exc}") from exc

    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_secs * 1000, 5000))
    except PlaywrightTimeoutError:
        pass
    try:
        page.locator("video").first.wait_for(timeout=4000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(4000)

    snapshot = capture_dom_snapshot(page)
    accumulator.ingest_dom_snapshot(snapshot)
    page.wait_for_timeout(2500)

    candidates = sort_candidates(accumulator.candidates)
    if not candidates:
        raise ExtractionError("No media candidates observed from the page.")

    user_agent = page.evaluate("() => navigator.userAgent")
    diagnostics = {
        "page_url": snapshot.get("page_url", ""),
        "event_count": accumulator.event_count,
        "json_event_count": accumulator.json_event_count,
        "candidate_count": len(candidates),
        "title": snapshot.get("title", ""),
    }
    return ExtractionResult(
        metadata=accumulator.metadata,
        candidates=candidates,
        cookies=page.context.cookies(),
        user_agent=user_agent,
        diagnostics=diagnostics,
    )

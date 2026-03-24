from __future__ import annotations

import json
import re
from abc import ABC
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from videocp.errors import ExtractionError
from videocp.models import MediaCandidate, MediaKind, TrackType, VideoMetadata, WatermarkMode

DOUYIN_JSON_HINTS = ("aweme", "detail", "iteminfo", "web/api", "feed")
DOUYIN_MEDIA_HINTS = ("video", "play", "download", ".mp4", ".m3u8")
XHS_MEDIA_HINTS = ("sns-video", "xhscdn.com/stream", ".mp4", ".m3u8", "video")
BILIBILI_MEDIA_HINTS = ("bilivideo.com", ".m4s", ".mp4", "playurl", "dash", "upos-", "upgcxcode")
BILIBILI_JSON_HINTS = ("playurl", "x/player", "dash", "/view", "bilibili")
XHS_JSON_HINTS = ("/api/sns/web/v1/feed", "/api/sns/web/v2/feed", "/note", "xiaohongshu")
DOUYIN_VIDEO_ID_RE = re.compile(r"/video/(\d+)")
DOUYIN_LIGHT_ID_RE = re.compile(r"/light/(\d+)")
DOUYIN_MODAL_ID_RE = re.compile(r"[?&]modal_id=(\d+)")
DOUYIN_USER_PROFILE_RE = re.compile(r"/user/([A-Za-z0-9_-]+)")
BILIBILI_VIDEO_ID_RE = re.compile(r"/video/([A-Za-z0-9]+)")
BILIBILI_SPACE_RE = re.compile(r"^space\.bilibili\.com$", re.IGNORECASE)
XHS_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)")
XHS_USER_PROFILE_RE = re.compile(r"/user/profile/([A-Za-z0-9]+)")


def normalize_url_path(url: str) -> str:
    return urlparse(url).path.lower()


def infer_media_kind(url: str, content_type: str = "") -> MediaKind | None:
    lowered_url = url.lower()
    lowered_type = content_type.lower()
    path = normalize_url_path(url)
    if path.endswith(".m3u8") or "mpegurl" in lowered_type or "application/x-mpegurl" in lowered_type:
        return MediaKind.HLS
    if path.endswith(".mp4") or path.endswith(".m4s") or lowered_type.startswith("video/mp4") or lowered_type.startswith("audio/mp4"):
        return MediaKind.MP4
    if "mime_type=video_mp4" in lowered_url:
        return MediaKind.MP4
    if "video/tos" in lowered_url and any(token in lowered_url for token in ("bytevc1", "tos-cn", "/play/")):
        return MediaKind.MP4
    return None


def infer_track_type(
    url: str,
    kind: MediaKind,
    *,
    content_type: str = "",
    semantic_tag: str = "",
) -> TrackType:
    lowered = url.lower()
    tag = semantic_tag.lower()
    lowered_type = content_type.lower()
    if kind == MediaKind.HLS:
        return TrackType.MUXED
    if any(token in tag for token in ("dash.audio", ".audio[", ".audio.", "audio[")):
        return TrackType.AUDIO_ONLY
    if any(token in tag for token in ("dash.video", ".video[", ".video.", "video[")):
        return TrackType.VIDEO_ONLY
    if lowered_type.startswith("audio/"):
        return TrackType.AUDIO_ONLY
    if any(token in lowered for token in ("media-audio-", "audio-und", "mp4a")):
        return TrackType.AUDIO_ONLY
    if any(token in lowered for token in ("media-video-", "avc1", "hvc1", "bytevc1")):
        return TrackType.VIDEO_ONLY
    if any(token in tag for token in ("play_addr", "download_addr", "bit_rate", "og_video", "video_src")):
        return TrackType.MUXED
    return TrackType.UNKNOWN


def douyin_watermark_mode(url: str, semantic_tag: str = "") -> WatermarkMode:
    lowered = url.lower()
    tag = semantic_tag.lower()
    if "playwm" in lowered or "watermark=1" in lowered or "download_addr" in tag:
        return WatermarkMode.WATERMARK
    if "play_addr" in tag or "bit_rate" in tag or "play/" in lowered or "watermark=0" in lowered:
        return WatermarkMode.NO_WATERMARK
    return WatermarkMode.UNKNOWN


def generic_candidate_rank(candidate: MediaCandidate) -> tuple[int, int, int, int, str]:
    watermark_rank = 0 if candidate.watermark_mode == WatermarkMode.NO_WATERMARK else 1
    kind_rank = 0 if candidate.kind == MediaKind.MP4 else 1
    track_rank = {
        TrackType.MUXED: 0,
        TrackType.VIDEO_ONLY: 1,
        TrackType.UNKNOWN: 2,
        TrackType.AUDIO_ONLY: 3,
    }[candidate.track_type]
    source_rank = 1 if candidate.source == "rewrite" else 0
    return (watermark_rank, kind_rank, track_rank, source_rank, candidate.url)


def bilibili_candidate_rank(candidate: MediaCandidate) -> tuple[int, int, int, int, str]:
    track_rank = {
        TrackType.VIDEO_ONLY: 0,
        TrackType.MUXED: 1,
        TrackType.UNKNOWN: 2,
        TrackType.AUDIO_ONLY: 3,
    }[candidate.track_type]
    kind_rank = 0 if candidate.kind == MediaKind.MP4 else 1
    source_rank = 0 if candidate.source == "json" else 1
    return (track_rank, kind_rank, source_rank, 0, candidate.url)


def normalize_candidate_url(url: str) -> str:
    return url.strip()


def extract_assignment_json(markup: str, marker: str) -> object | None:
    index = markup.find(marker)
    if index < 0:
        return None
    object_start = min(
        [position for position in (markup.find("{", index), markup.find("[", index)) if position >= 0],
        default=-1,
    )
    if object_start < 0:
        return None
    opener = markup[object_start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    object_end = -1
    for position, char in enumerate(markup[object_start:], object_start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == opener:
            depth += 1
            continue
        if char == closer:
            depth -= 1
            if depth == 0:
                object_end = position + 1
                break
    if object_end < 0:
        return None
    try:
        return json.loads(markup[object_start:object_end])
    except json.JSONDecodeError:
        return None


def clean_title_suffix(text: str, suffixes: tuple[str, ...]) -> str:
    result = text.strip()
    for suffix in suffixes:
        if result.endswith(suffix):
            result = result[: -len(suffix)].strip()
    return result


def extract_id_from_url(url: str, patterns: tuple[re.Pattern[str], ...]) -> str:
    for pattern in patterns:
        match = pattern.search(url or "")
        if match:
            return match.group(1)
    return ""


def normalize_author_name(author: str) -> str:
    cleaned = author.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:].strip()
    return cleaned


class SiteProvider(ABC):
    key = "generic"
    display_name = "Generic"
    hosts: tuple[str, ...] = ()
    media_hints: tuple[str, ...] = ()
    json_hints: tuple[str, ...] = ()
    markup_json_markers: tuple[str, ...] = ()
    id_patterns: tuple[re.Pattern[str], ...] = ()
    title_suffixes: tuple[str, ...] = ()
    default_watermark_mode = WatermarkMode.UNKNOWN

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == item or host.endswith(f".{item}") for item in self.hosts)

    def is_profile_url(self, url: str) -> bool:
        return False

    def create_metadata(self, source_url: str) -> VideoMetadata:
        return VideoMetadata(site=self.key, source_url=source_url, canonical_url=source_url)

    def canonicalize_url(self, url: str) -> str:
        return url

    def is_media_request_candidate(self, url: str) -> bool:
        lowered = url.lower()
        return any(hint in lowered for hint in self.media_hints)

    def should_parse_json(self, url: str, content_type: str) -> bool:
        lowered_url = url.lower()
        lowered_type = content_type.lower()
        return "application/json" in lowered_type or any(hint in lowered_url for hint in self.json_hints)

    def infer_media_kind(self, url: str, content_type: str = "", semantic_tag: str = "") -> MediaKind | None:
        del semantic_tag
        return infer_media_kind(url, content_type)

    def infer_track_type(self, url: str, kind: MediaKind, content_type: str = "", semantic_tag: str = "") -> TrackType:
        return infer_track_type(url, kind, content_type=content_type, semantic_tag=semantic_tag)

    def infer_watermark_mode(self, url: str, semantic_tag: str = "") -> WatermarkMode:
        del url, semantic_tag
        return self.default_watermark_mode

    def candidate_rank(self, candidate: MediaCandidate) -> tuple[int, int, int, int, str]:
        return generic_candidate_rank(candidate)

    def conservative_rewrites(self, candidates: list[MediaCandidate]) -> list[MediaCandidate]:
        return candidates

    def sort_candidates(self, candidates: list[MediaCandidate]) -> list[MediaCandidate]:
        return sorted(self.conservative_rewrites(candidates), key=self.candidate_rank)

    def populate_metadata_from_dict(self, metadata: VideoMetadata, payload: dict, path: str) -> None:
        del metadata, payload, path

    def scan_media_node(self, accumulator, key: str, value: object, path: str) -> None:
        del accumulator, key, value, path

    def scan_json_payload(self, accumulator, payload: object, path: str = "$") -> None:
        if isinstance(payload, dict):
            self.populate_metadata_from_dict(accumulator.metadata, payload, path)
            for key, value in payload.items():
                next_path = f"{path}.{key}"
                self.scan_media_node(accumulator, key, value, next_path)
                self.scan_json_payload(accumulator, value, next_path)
        elif isinstance(payload, list):
            for index, item in enumerate(payload):
                self.scan_json_payload(accumulator, item, f"{path}[{index}]")

    def scan_markup(self, accumulator, markup: str) -> None:
        for marker in self.markup_json_markers:
            payload = extract_assignment_json(markup, marker)
            if payload is not None:
                self.scan_json_payload(accumulator, payload, path=f"markup:{marker}")

    def apply_dom_snapshot(self, metadata: VideoMetadata, snapshot: dict[str, str], add_candidate) -> None:
        metadata.page_url = snapshot.get("page_url", metadata.page_url)
        raw_title = snapshot.get("og_title") or snapshot.get("title") or metadata.title
        cleaned_title = clean_title_suffix(raw_title, self.title_suffixes)
        if cleaned_title:
            metadata.title = cleaned_title
        if not metadata.desc:
            raw_desc = (
                snapshot.get("description")
                or snapshot.get("og_description")
                or snapshot.get("og_title")
                or snapshot.get("title")
                or ""
            )
            metadata.desc = clean_title_suffix(raw_desc, self.title_suffixes)
        if not metadata.author:
            author = normalize_author_name(snapshot.get("author_text", ""))
            if author:
                metadata.author = author
        if not metadata.aweme_id:
            for candidate_url in (metadata.page_url, metadata.canonical_url, metadata.source_url):
                for pattern in self.id_patterns:
                    match = pattern.search(candidate_url or "")
                    if match:
                        metadata.aweme_id = match.group(1)
                        break
                if metadata.aweme_id:
                    break
        for key in ("video_src", "og_video"):
            value = snapshot.get(key, "")
            if value:
                add_candidate(value, source="dom", observed_via="dom", semantic_tag=key, note=key)


class DouyinProvider(SiteProvider):
    key = "douyin"
    display_name = "Douyin"
    hosts = ("douyin.com", "iesdouyin.com")
    media_hints = DOUYIN_MEDIA_HINTS
    json_hints = DOUYIN_JSON_HINTS
    id_patterns = (DOUYIN_VIDEO_ID_RE, DOUYIN_LIGHT_ID_RE, DOUYIN_MODAL_ID_RE)
    default_watermark_mode = WatermarkMode.UNKNOWN

    def is_profile_url(self, url: str) -> bool:
        path = urlparse(url).path
        if not DOUYIN_USER_PROFILE_RE.search(path):
            return False
        # A profile URL with a modal_id overlay is a video URL, not a profile
        return not any(p.search(url) for p in self.id_patterns)

    def canonicalize_url(self, url: str) -> str:
        aweme_id = extract_id_from_url(url, self.id_patterns)
        if not aweme_id:
            return url
        parsed = urlparse(url)
        return f"{parsed.scheme or 'https'}://{parsed.netloc or 'www.douyin.com'}/video/{aweme_id}"

    def infer_watermark_mode(self, url: str, semantic_tag: str = "") -> WatermarkMode:
        return douyin_watermark_mode(url, semantic_tag=semantic_tag)

    def create_metadata(self, source_url: str) -> VideoMetadata:
        metadata = super().create_metadata(source_url)
        metadata.aweme_id = extract_id_from_url(source_url, self.id_patterns)
        return metadata

    def candidate_rank(self, candidate: MediaCandidate) -> tuple[int, int, int, int, int, str]:
        watermark_rank = 0 if candidate.watermark_mode == WatermarkMode.NO_WATERMARK else 1
        source_rank = 0 if candidate.source in {"json", "rewrite"} else 1
        kind_rank = 0 if candidate.kind == MediaKind.MP4 else 1
        track_rank = {
            TrackType.VIDEO_ONLY: 0,
            TrackType.MUXED: 1,
            TrackType.UNKNOWN: 2,
            TrackType.AUDIO_ONLY: 3,
        }[candidate.track_type]
        request_runtime_rank = 0
        lowered_url = candidate.url.lower()
        if candidate.source == "request":
            has_runtime_tokens = any(token in lowered_url for token in ("policy=", "signature=", "tk=", "fid=", "expire=", "ply_type="))
            request_runtime_rank = 0 if has_runtime_tokens and "is_ssr=1" not in lowered_url else 1
        rewrite_rank = 1 if candidate.source == "rewrite" else 0
        return (watermark_rank, source_rank, request_runtime_rank, kind_rank, track_rank, rewrite_rank, candidate.url)

    def conservative_rewrites(self, candidates: list[MediaCandidate]) -> list[MediaCandidate]:
        if any(candidate.watermark_mode == WatermarkMode.NO_WATERMARK for candidate in candidates):
            return candidates
        rewritten = list(candidates)
        seen = {normalize_candidate_url(candidate.url) for candidate in candidates}
        for candidate in candidates:
            if candidate.kind != MediaKind.MP4:
                continue
            new_url = candidate.url
            changed = False
            if "playwm" in new_url:
                new_url = new_url.replace("playwm", "play")
                changed = True
            parsed = urlparse(new_url)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if query.get("watermark") == ["1"]:
                query["watermark"] = ["0"]
                new_url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
                changed = True
            normalized = normalize_candidate_url(new_url)
            if not changed or normalized in seen:
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

    def populate_metadata_from_dict(self, metadata: VideoMetadata, payload: dict, path: str) -> None:
        del path
        if not metadata.aweme_id:
            aweme_id = payload.get("aweme_id") or payload.get("group_id")
            if isinstance(aweme_id, str):
                metadata.aweme_id = aweme_id
        if not metadata.desc and isinstance(payload.get("desc"), str):
            metadata.desc = payload["desc"]
        author = payload.get("author")
        if isinstance(author, dict) and not metadata.author:
            nickname = author.get("nickname")
            if isinstance(nickname, str):
                metadata.author = nickname

    def _payload_aweme_id(self, payload: dict) -> str:
        aweme_id = payload.get("aweme_id") or payload.get("group_id")
        return aweme_id if isinstance(aweme_id, str) else ""

    def scan_json_payload(self, accumulator, payload: object, path: str = "$") -> None:
        target_aweme_id = accumulator.metadata.aweme_id

        def visit(node: object, node_path: str, active_aweme_id: str = "") -> None:
            if isinstance(node, dict):
                node_aweme_id = self._payload_aweme_id(node)
                if target_aweme_id and node_aweme_id and node_aweme_id != target_aweme_id:
                    return
                next_active_aweme_id = node_aweme_id or active_aweme_id
                should_collect_media = not target_aweme_id or next_active_aweme_id == target_aweme_id
                if should_collect_media:
                    self.populate_metadata_from_dict(accumulator.metadata, node, node_path)
                for key, value in node.items():
                    next_path = f"{node_path}.{key}"
                    if should_collect_media:
                        self.scan_media_node(accumulator, key, value, next_path)
                    visit(value, next_path, next_active_aweme_id)
                return
            if isinstance(node, list):
                for index, item in enumerate(node):
                    visit(item, f"{node_path}[{index}]", active_aweme_id)

        visit(payload, path)

    def scan_media_node(self, accumulator, key: str, value: object, path: str) -> None:
        if key in {"play_addr", "play_addr_h264", "play_addr_265", "download_addr", "play_addr_lowbr"}:
            url_list = value.get("url_list") if isinstance(value, dict) else None
            if isinstance(url_list, list):
                for item in url_list:
                    if isinstance(item, str):
                        accumulator.add_candidate(
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
                                accumulator.add_candidate(
                                    candidate_url,
                                    source="json",
                                    observed_via="json",
                                    semantic_tag=f"{path}[{index}].play_addr",
                                    note=f"{path}[{index}].play_addr",
                                )


class BilibiliProvider(SiteProvider):
    key = "bilibili"
    display_name = "Bilibili"
    hosts = ("bilibili.com", "b23.tv")
    media_hints = BILIBILI_MEDIA_HINTS
    json_hints = BILIBILI_JSON_HINTS
    markup_json_markers = ("window.__playinfo__=", "window.__INITIAL_STATE__=")
    id_patterns = (BILIBILI_VIDEO_ID_RE,)
    title_suffixes = ("_哔哩哔哩_bilibili", " - 哔哩哔哩", "_哔哩哔哩")
    default_watermark_mode = WatermarkMode.NO_WATERMARK

    def is_profile_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return bool(BILIBILI_SPACE_RE.match(host))

    def candidate_rank(self, candidate: MediaCandidate) -> tuple[int, int, int, int, str]:
        return bilibili_candidate_rank(candidate)

    def apply_dom_snapshot(self, metadata: VideoMetadata, snapshot: dict[str, str], add_candidate) -> None:
        super().apply_dom_snapshot(metadata, snapshot, add_candidate)
        title = clean_title_suffix(snapshot.get("og_title") or snapshot.get("title") or "", self.title_suffixes)
        if title:
            metadata.title = title
        if not metadata.desc or metadata.desc.startswith("http") or len(metadata.desc) > 200:
            metadata.desc = title or metadata.desc

    def infer_track_type(self, url: str, kind: MediaKind, content_type: str = "", semantic_tag: str = "") -> TrackType:
        track_type = super().infer_track_type(url, kind, content_type=content_type, semantic_tag=semantic_tag)
        if track_type != TrackType.UNKNOWN:
            return track_type
        lowered_type = content_type.lower()
        if lowered_type.startswith("audio/"):
            return TrackType.AUDIO_ONLY
        if normalize_url_path(url).endswith(".m4s"):
            return TrackType.VIDEO_ONLY
        return TrackType.UNKNOWN

    def populate_metadata_from_dict(self, metadata: VideoMetadata, payload: dict, path: str) -> None:
        del path
        if not metadata.aweme_id:
            for key in ("bvid", "aid"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    metadata.aweme_id = value
                    break
        if isinstance(payload.get("title"), str) and payload["title"]:
            metadata.title = payload["title"]
        owner = payload.get("owner")
        if isinstance(owner, dict):
            name = owner.get("name")
            if isinstance(name, str):
                metadata.author = name
        video_data = payload.get("videoData")
        if isinstance(video_data, dict):
            if not metadata.aweme_id:
                bvid = video_data.get("bvid")
                if isinstance(bvid, str):
                    metadata.aweme_id = bvid
            if isinstance(video_data.get("title"), str) and video_data["title"]:
                metadata.title = video_data["title"]
            if isinstance(video_data.get("desc"), str) and video_data["desc"]:
                metadata.desc = video_data["desc"]
            owner = video_data.get("owner")
            if isinstance(owner, dict):
                name = owner.get("name")
                if isinstance(name, str):
                    metadata.author = name

    def _add_stream_urls(self, accumulator, stream: dict, stream_type: str, path: str) -> None:
        for key in ("baseUrl", "base_url"):
            value = stream.get(key)
            if isinstance(value, str):
                accumulator.add_candidate(
                    value,
                    source="json",
                    observed_via="json",
                    semantic_tag=f"{path}.{stream_type}.{key}",
                    note=f"{path}.{stream_type}.{key}",
                )
        for key in ("backupUrl", "backup_url"):
            value = stream.get(key)
            if isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, str):
                        accumulator.add_candidate(
                            item,
                            source="json",
                            observed_via="json",
                            semantic_tag=f"{path}.{stream_type}.{key}[{index}]",
                            note=f"{path}.{stream_type}.{key}[{index}]",
                        )

    def scan_media_node(self, accumulator, key: str, value: object, path: str) -> None:
        if key == "dash" and isinstance(value, dict):
            for stream_type in ("video", "audio"):
                streams = value.get(stream_type)
                if not isinstance(streams, list):
                    continue
                for index, stream in enumerate(streams):
                    if isinstance(stream, dict):
                        self._add_stream_urls(accumulator, stream, f"{stream_type}[{index}]", path)
        if key == "durl" and isinstance(value, list):
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    continue
                candidate_url = item.get("url")
                if isinstance(candidate_url, str):
                    accumulator.add_candidate(
                        candidate_url,
                        source="json",
                        observed_via="json",
                        semantic_tag=f"{path}[{index}].url",
                        note=f"{path}[{index}].url",
                    )


class XiaohongshuProvider(SiteProvider):
    key = "xiaohongshu"
    display_name = "Xiaohongshu"
    hosts = ("xiaohongshu.com", "xhslink.com")
    media_hints = XHS_MEDIA_HINTS
    json_hints = XHS_JSON_HINTS
    id_patterns = (XHS_NOTE_ID_RE,)
    title_suffixes = (" - 小红书",)
    default_watermark_mode = WatermarkMode.NO_WATERMARK

    def is_profile_url(self, url: str) -> bool:
        path = urlparse(url).path
        return bool(XHS_USER_PROFILE_RE.search(path))

    def apply_dom_snapshot(self, metadata: VideoMetadata, snapshot: dict[str, str], add_candidate) -> None:
        super().apply_dom_snapshot(metadata, snapshot, add_candidate)
        author = snapshot.get("author_text", "").strip()
        if author:
            metadata.author = author
        if metadata.title:
            metadata.title = clean_title_suffix(metadata.title, self.title_suffixes)
        if metadata.desc:
            metadata.desc = clean_title_suffix(metadata.desc, self.title_suffixes)

    def populate_metadata_from_dict(self, metadata: VideoMetadata, payload: dict, path: str) -> None:
        del path
        if not metadata.aweme_id:
            for key in ("note_id", "id"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    metadata.aweme_id = value
                    break
        is_note_payload = any(key in payload for key in ("note_id", "note_card", "video", "interact_info"))
        user = payload.get("user")
        if isinstance(user, dict) and not metadata.author:
            nickname = user.get("nickname")
            if is_note_payload and isinstance(nickname, str):
                metadata.author = nickname
        author = payload.get("author")
        if isinstance(author, dict) and not metadata.author:
            nickname = author.get("nickname")
            if is_note_payload and isinstance(nickname, str):
                metadata.author = nickname

    def _add_possible_media_value(self, accumulator, value: object, path: str) -> None:
        if isinstance(value, str):
            accumulator.add_candidate(
                value,
                source="json",
                observed_via="json",
                semantic_tag=path,
                note=path,
            )
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, str):
                    accumulator.add_candidate(
                        item,
                        source="json",
                        observed_via="json",
                        semantic_tag=f"{path}[{index}]",
                        note=f"{path}[{index}]",
                    )

    def scan_media_node(self, accumulator, key: str, value: object, path: str) -> None:
        if key in {"url", "master_url", "masterUrl", "origin_video_key", "originVideoKey"}:
            self._add_possible_media_value(accumulator, value, path)
        if key == "url_list":
            self._add_possible_media_value(accumulator, value, path)
        if isinstance(value, dict):
            for nested_key in ("url", "master_url", "masterUrl", "origin_video_key", "originVideoKey"):
                if nested_key in value:
                    self._add_possible_media_value(accumulator, value.get(nested_key), f"{path}.{nested_key}")


PROVIDERS: tuple[SiteProvider, ...] = (
    DouyinProvider(),
    BilibiliProvider(),
    XiaohongshuProvider(),
)


def get_provider_by_key(provider_key: str) -> SiteProvider:
    for provider in PROVIDERS:
        if provider.key == provider_key:
            return provider
    raise ExtractionError(f"Unsupported provider: {provider_key}")


def resolve_provider(url: str) -> SiteProvider:
    for provider in PROVIDERS:
        if provider.matches_url(url):
            return provider
    raise ExtractionError(f"Unsupported site for URL: {url}")

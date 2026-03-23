from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from videocp.config import WatermarkConfig
from videocp.errors import DownloadError
from videocp.models import DownloadArtifact, ExtractionResult, MediaCandidate, MediaKind, TrackType
from videocp.runtime_log import full_url, log_info, log_warn

DOWNLOAD_CHUNK_SIZE = 1024 * 256
DOWNLOAD_MP4_MAX_RETRIES = 3
DOWNLOAD_RETRY_BACKOFF_SECS = 1.0
DETAILED_ATTEMPT_LOG_LIMIT = 1
DETAILED_ATTEMPT_LOG_INTERVAL = 200
RETRYABLE_DOWNLOAD_ERROR_TOKENS = (
    "truncated",
    "connection broken",
    "incompleteread",
    "timed out",
    "timeout",
)


@dataclass(slots=True)
class DownloadPlan:
    primary: MediaCandidate
    audio: MediaCandidate | None = None

    @property
    def mode(self) -> str:
        if self.audio is not None:
            return "mux_av"
        if self.primary.kind == MediaKind.HLS:
            return "hls"
        return "direct"


def sanitize_filename(value: str) -> str:
    cleaned = "".join(char if char not in '<>:"/\\|?*\n\r\t' else "_" for char in value)
    cleaned = "_".join(cleaned.split())
    return cleaned.strip("._") or "video"


def build_output_stem(extraction: ExtractionResult) -> str:
    metadata = extraction.metadata
    author = sanitize_filename(metadata.author or "unknown_author")
    content_id = sanitize_filename(metadata.content_id or "unknown_media")
    return f"{author}_{content_id}"


def allocate_output_path(output_dir: Path, stem: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate = output_dir / f"{stem}.mp4"
    suffix = 1
    while candidate.exists():
        candidate = output_dir / f"{stem}_{suffix}.mp4"
        suffix += 1
    return candidate


def build_requests_session(cookies: list[dict[str, Any]]) -> requests.Session:
    session = requests.Session()
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        session.cookies.set(
            name,
            value,
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )
    return session


def find_ffmpeg() -> str:
    return shutil.which("ffmpeg") or ""


def find_ffprobe() -> str:
    return shutil.which("ffprobe") or ""


def probe_video_dimensions(video_path: Path) -> tuple[int, int]:
    ffprobe = find_ffprobe()
    if not ffprobe:
        return (0, 0)
    proc = subprocess.run(
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", str(video_path)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return (0, 0)
    data = json.loads(proc.stdout)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            return (int(stream.get("width", 0)), int(stream.get("height", 0)))
    return (0, 0)


def _extract_frame_png(video_path: Path, seek_secs: float) -> bytes:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return b""
    command = [
        ffmpeg, "-ss", str(seek_secs), "-i", str(video_path),
        "-vframes", "1", "-f", "image2", "-c:v", "png",
        "-loglevel", "error", "pipe:1",
    ]
    proc = subprocess.run(command, capture_output=True, check=False)
    return proc.stdout if proc.returncode == 0 else b""


def _detect_watermark_with_llm(
    frame_png: bytes,
    video_width: int,
    video_height: int,
    api_key: str,
    base_url: str,
    model: str,
) -> tuple[int, int, int, int] | None:
    encoded = base64.b64encode(frame_png).decode("ascii")
    data_url = f"data:image/png;base64,{encoded}"
    prompt = (
        f"This is a video frame ({video_width}x{video_height} pixels) from a Chinese video platform. "
        "Find the channel/uploader watermark overlaid on the video. "
        "It is typically in a corner and can be: "
        "semi-transparent white text, colored/stylized text, text with glow or shadow, "
        "a small logo, or Chinese characters showing a username or channel name "
        "(e.g. 'bilibili', 'XX的世界', '@username'). "
        "Do NOT treat subtitles, captions, or on-screen text that is part of the video content as a watermark. "
        "Only detect overlaid branding/channel watermarks. "
        "If found, respond with ONLY a JSON object: "
        '{"x": <left>, "y": <top>, "w": <width>, "h": <height>} '
        "where x,y is the top-left corner in pixels of the original resolution. "
        "Add some padding around the text. "
        'If no watermark is found, respond with: {"found": false}'
    )
    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    try:
        resp = requests.post(
            base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        # Extract JSON from response (may be wrapped in markdown code block)
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        if result.get("found") is False:
            return None
        x = int(result["x"])
        y = int(result["y"])
        w = int(result["w"])
        h = int(result["h"])
        if w <= 0 or h <= 0 or x < 0 or y < 0:
            return None
        return (x, y, w, h)
    except Exception as exc:
        log_warn("postprocess.delogo.llm_error", error=str(exc))
        return None


def remove_bilibili_watermark(video_path: Path, api_key: str, base_url: str, model: str) -> bool:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log_warn("postprocess.delogo.skip", reason="ffmpeg not found")
        return False
    if not api_key:
        log_warn("postprocess.delogo.skip", reason="no api_key configured")
        return False
    width, height = probe_video_dimensions(video_path)
    if width == 0 or height == 0:
        log_warn("postprocess.delogo.skip", reason="could not probe video dimensions")
        return False
    if min(width, height) > 1080:
        log_info("postprocess.delogo.skip", reason=f"resolution too high ({width}x{height}), only <=1080p supported")
        return False
    frame_png = _extract_frame_png(video_path, seek_secs=1.0)
    if not frame_png:
        log_warn("postprocess.delogo.skip", reason="could not extract frame")
        return False
    log_info("postprocess.delogo.detect", model=model)
    rect = _detect_watermark_with_llm(frame_png, width, height, api_key, base_url, model)
    if rect is None:
        log_info("postprocess.delogo.skip", reason="no watermark detected")
        return False
    x, y, w, h = rect
    temp_path = video_path.with_name(f"{video_path.stem}.delogo{video_path.suffix}")
    log_info("postprocess.delogo.start", path=video_path, rect=f"x={x}:y={y}:w={w}:h={h}")
    command = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(video_path),
        "-vf", f"delogo=x={x}:y={y}:w={w}:h={h}",
        "-c:a", "copy",
        str(temp_path),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=False, timeout=300)
    except subprocess.TimeoutExpired:
        log_warn("postprocess.delogo.failed", error="ffmpeg timed out after 300s")
        temp_path.unlink(missing_ok=True)
        return False
    if proc.returncode != 0:
        stderr = " ".join(proc.stderr.split())
        log_warn("postprocess.delogo.failed", error=stderr or f"ffmpeg exit code {proc.returncode}")
        temp_path.unlink(missing_ok=True)
        return False
    if not temp_path.exists() or temp_path.stat().st_size == 0:
        log_warn("postprocess.delogo.failed", error="empty output")
        temp_path.unlink(missing_ok=True)
        return False
    temp_path.replace(video_path)
    log_info("postprocess.delogo.ok", path=video_path, bytes=video_path.stat().st_size)
    return True


def ffmpeg_temp_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.part{output_path.suffix}")


def cookie_header_from_cookies(cookies: list[dict[str, Any]]) -> str:
    pairs = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if isinstance(name, str) and isinstance(value, str):
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def format_download_exception(exc: Exception) -> str:
    text = str(exc).strip()
    return text or f"{type(exc).__name__}(no message)"


def is_retryable_download_error(exc: DownloadError) -> bool:
    lowered = str(exc).lower()
    return any(token in lowered for token in RETRYABLE_DOWNLOAD_ERROR_TOKENS)


def download_mp4_to_path(
    session: requests.Session,
    candidate: MediaCandidate,
    target_path: Path,
    user_agent: str,
    referer: str,
    timeout_secs: int,
    *,
    emit_log: bool = True,
) -> int:
    temp_path = target_path.with_suffix(target_path.suffix + ".part")
    last_error: DownloadError | None = None
    for attempt_index in range(1, DOWNLOAD_MP4_MAX_RETRIES + 1):
        headers = {
            "User-Agent": user_agent,
            "Referer": referer,
            "Accept-Encoding": "identity",
        }
        response = None
        try:
            response = session.get(
                candidate.url,
                headers=headers,
                stream=True,
                timeout=timeout_secs,
                allow_redirects=True,
            )
            if response.status_code not in {200, 206}:
                raise DownloadError(f"HTTP {response.status_code}")
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                raise DownloadError(f"Unexpected content type: {content_type}")
            expected = int(response.headers.get("content-length", "0") or 0)
            size = 0
            with temp_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    size += len(chunk)
            if size == 0:
                raise DownloadError("Downloaded file is empty.")
            if expected and size < expected:
                raise DownloadError(f"Downloaded file is truncated: {size} < {expected}")
            temp_path.replace(target_path)
            return size
        except requests.RequestException as exc:
            last_error = DownloadError(f"Request failed: {format_download_exception(exc)}")
        except DownloadError as exc:
            last_error = exc
        finally:
            if response is not None:
                response.close()
            if temp_path.exists() and not target_path.exists():
                temp_path.unlink(missing_ok=True)
        assert last_error is not None
        if attempt_index >= DOWNLOAD_MP4_MAX_RETRIES or not is_retryable_download_error(last_error):
            raise last_error
        if emit_log:
            log_warn(
                "download.stream.retry",
                attempt=f"{attempt_index}/{DOWNLOAD_MP4_MAX_RETRIES}",
                url=full_url(candidate.url),
                error=str(last_error),
            )
        time.sleep(DOWNLOAD_RETRY_BACKOFF_SECS * attempt_index)
    raise last_error or DownloadError("mp4 download failed")


def download_hls(
    candidate: MediaCandidate,
    output_path: Path,
    user_agent: str,
    referer: str,
    cookies: list[dict[str, Any]],
    *,
    emit_log: bool = True,
) -> None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise DownloadError("ffmpeg not found for HLS download.")
    temp_path = ffmpeg_temp_output_path(output_path)
    header_lines = [f"User-Agent: {user_agent}", f"Referer: {referer}"]
    cookie_header = cookie_header_from_cookies(cookies)
    if cookie_header:
        header_lines.append(f"Cookie: {cookie_header}")
    headers = "".join(f"{line}\r\n" for line in header_lines)
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-headers",
        headers,
        "-i",
        candidate.url,
        "-c",
        "copy",
        str(temp_path),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        stderr = " ".join(proc.stderr.split())
        raise DownloadError(stderr or "ffmpeg failed to mux HLS.")
    if not temp_path.exists() or temp_path.stat().st_size == 0:
        raise DownloadError("ffmpeg produced an empty file.")
    temp_path.replace(output_path)


def mux_av_assets(video_path: Path, audio_path: Path, output_path: Path, *, emit_log: bool = True) -> None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise DownloadError("ffmpeg not found for separate video/audio mux.")
    temp_path = ffmpeg_temp_output_path(output_path)
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        str(temp_path),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        stderr = " ".join(proc.stderr.split())
        raise DownloadError(stderr or "ffmpeg failed to mux video/audio.")
    if not temp_path.exists() or temp_path.stat().st_size == 0:
        raise DownloadError("ffmpeg produced an empty muxed file.")
    temp_path.replace(output_path)


def should_log_attempt(attempt_index: int, total_attempts: int) -> bool:
    if attempt_index <= DETAILED_ATTEMPT_LOG_LIMIT:
        return True
    if attempt_index == total_attempts:
        return True
    return attempt_index % DETAILED_ATTEMPT_LOG_INTERVAL == 0


def score_audio_match(video_candidate: MediaCandidate, audio_candidate: MediaCandidate) -> tuple[int, int, int, str]:
    video_parsed = urlparse(video_candidate.url)
    audio_parsed = urlparse(audio_candidate.url)
    same_source = 0 if video_candidate.source == audio_candidate.source else 1
    same_host = 0 if video_parsed.netloc == audio_parsed.netloc else 1
    same_watermark = 0 if video_candidate.watermark_mode == audio_candidate.watermark_mode else 1
    return (same_source, same_host, same_watermark, audio_candidate.url)


def best_audio_candidate(video_candidate: MediaCandidate, candidates: list[MediaCandidate]) -> MediaCandidate | None:
    audio_candidates = [candidate for candidate in candidates if candidate.track_type == TrackType.AUDIO_ONLY]
    if not audio_candidates:
        return None
    return min(audio_candidates, key=lambda candidate: score_audio_match(video_candidate, candidate))


def build_download_plans(candidates: list[MediaCandidate]) -> list[DownloadPlan]:
    plans: list[DownloadPlan] = []
    seen_keys: set[tuple[str, str]] = set()
    for candidate in candidates:
        if candidate.track_type == TrackType.AUDIO_ONLY:
            continue
        if candidate.track_type == TrackType.VIDEO_ONLY:
            audio_candidate = best_audio_candidate(candidate, candidates)
            if audio_candidate is None:
                continue
            key = (candidate.url, audio_candidate.url)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            plans.append(DownloadPlan(primary=candidate, audio=audio_candidate))
            continue
        key = (candidate.url, "")
        if key in seen_keys:
            continue
        seen_keys.add(key)
        plans.append(DownloadPlan(primary=candidate))
    if not plans and candidates:
        raise DownloadError("Only audio-only candidates were observed; no playable video stream found.")
    return plans


def merged_candidate(video_candidate: MediaCandidate, audio_candidate: MediaCandidate) -> MediaCandidate:
    return MediaCandidate(
        url=video_candidate.url,
        kind=video_candidate.kind,
        track_type=TrackType.MUXED,
        watermark_mode=video_candidate.watermark_mode,
        source="merged",
        observed_via=video_candidate.observed_via,
        note=f"audio={audio_candidate.url}",
    )


def write_sidecar(
    sidecar_path: Path,
    extraction: ExtractionResult,
    chosen_candidate: MediaCandidate,
    attempts: list[dict[str, str]],
) -> None:
    payload = {
        "site": extraction.metadata.site,
        "content_id": extraction.metadata.content_id,
        "aweme_id": extraction.metadata.aweme_id,
        "author": extraction.metadata.author,
        "desc": extraction.metadata.desc,
        "source_url": extraction.metadata.source_url,
        "canonical_url": extraction.metadata.canonical_url,
        "page_url": extraction.metadata.page_url,
        "chosen_candidate": chosen_candidate.to_dict(),
        "watermark_mode": chosen_candidate.watermark_mode.value,
        "candidates": [candidate.to_dict() for candidate in extraction.candidates],
        "diagnostics": extraction.diagnostics,
        "attempts": attempts,
    }
    sidecar_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def download_best_candidate(
    extraction: ExtractionResult,
    output_dir: Path,
    timeout_secs: int,
    watermark: WatermarkConfig | None = None,
) -> DownloadArtifact:
    stem = build_output_stem(extraction)
    output_path = allocate_output_path(output_dir, stem)
    sidecar_path = output_path.with_suffix(".json")
    session = build_requests_session(extraction.cookies)
    attempts: list[dict[str, str]] = []
    last_error = "no candidates"
    plans = build_download_plans(extraction.candidates)
    suppressed_failures = 0
    suppressed_last_error = ""

    def flush_suppressed_failures(*, before_attempt: str = "", outcome: str = "") -> None:
        nonlocal suppressed_failures, suppressed_last_error
        if suppressed_failures <= 0:
            return
        log_info(
            "download.attempt.suppressed",
            site=extraction.metadata.site,
            content_id=extraction.metadata.content_id or "unknown",
            count=suppressed_failures,
            last_error=suppressed_last_error,
            before_attempt=before_attempt or None,
            outcome=outcome or None,
        )
        suppressed_failures = 0
        suppressed_last_error = ""

    log_info(
        "download.plan.start",
        site=extraction.metadata.site,
        content_id=extraction.metadata.content_id or "unknown",
        plans=len(plans),
        output=output_path,
    )
    for attempt_index, plan in enumerate(plans, start=1):
        candidate = plan.primary
        detail_log = should_log_attempt(attempt_index, len(plans))
        attempt = {
            "url": candidate.url,
            "kind": candidate.kind.value,
            "track_type": candidate.track_type.value,
            "source": candidate.source,
            "mode": plan.mode,
        }
        if plan.audio is not None:
            attempt["audio_url"] = plan.audio.url
        if detail_log:
            flush_suppressed_failures(before_attempt=f"{attempt_index}/{len(plans)}")
            log_info(
                "download.attempt.start",
                site=extraction.metadata.site,
                content_id=extraction.metadata.content_id or "unknown",
                attempt=f"{attempt_index}/{len(plans)}",
                mode=plan.mode,
                kind=candidate.kind.value,
                track=candidate.track_type.value,
                source=candidate.source,
                url=full_url(candidate.url),
                audio_url=full_url(plan.audio.url) if plan.audio is not None else None,
            )
        try:
            if plan.audio is not None:
                with tempfile.TemporaryDirectory(prefix="videocp-mux-") as temp_dir_raw:
                    temp_dir = Path(temp_dir_raw)
                    video_path = temp_dir / "video.mp4"
                    audio_path = temp_dir / "audio.m4a"
                    download_mp4_to_path(
                        session=session,
                        candidate=candidate,
                        target_path=video_path,
                        user_agent=extraction.user_agent,
                        referer=extraction.metadata.page_url or extraction.metadata.canonical_url,
                        timeout_secs=timeout_secs,
                        emit_log=detail_log,
                    )
                    download_mp4_to_path(
                        session=session,
                        candidate=plan.audio,
                        target_path=audio_path,
                        user_agent=extraction.user_agent,
                        referer=extraction.metadata.page_url or extraction.metadata.canonical_url,
                        timeout_secs=timeout_secs,
                        emit_log=detail_log,
                    )
                    mux_av_assets(video_path, audio_path, output_path, emit_log=detail_log)
                chosen_candidate = merged_candidate(candidate, plan.audio)
            elif candidate.kind == MediaKind.MP4:
                download_mp4_to_path(
                    session=session,
                    candidate=candidate,
                    target_path=output_path,
                    user_agent=extraction.user_agent,
                    referer=extraction.metadata.page_url or extraction.metadata.canonical_url,
                    timeout_secs=timeout_secs,
                    emit_log=detail_log,
                )
                chosen_candidate = candidate
            else:
                download_hls(
                    candidate=candidate,
                    output_path=output_path,
                    user_agent=extraction.user_agent,
                    referer=extraction.metadata.page_url or extraction.metadata.canonical_url,
                    cookies=extraction.cookies,
                    emit_log=detail_log,
                )
                chosen_candidate = candidate
            attempt["status"] = "ok"
            attempts.append(attempt)
            if extraction.metadata.site == "bilibili" and watermark and watermark.enabled:
                remove_bilibili_watermark(output_path, watermark.api_key, watermark.base_url, watermark.model)
            write_sidecar(sidecar_path, extraction, chosen_candidate, attempts)
            flush_suppressed_failures(outcome="success")
            log_info(
                "download.complete",
                site=extraction.metadata.site,
                content_id=extraction.metadata.content_id or "unknown",
                output=output_path,
                sidecar=sidecar_path,
                bytes=output_path.stat().st_size if output_path.exists() else 0,
                chosen_source=chosen_candidate.source,
                watermark=chosen_candidate.watermark_mode.value,
            )
            return DownloadArtifact(
                output_path=output_path,
                sidecar_path=sidecar_path,
                chosen_candidate=chosen_candidate,
                attempts=attempts,
            )
        except DownloadError as exc:
            attempt["status"] = "failed"
            attempt["error"] = str(exc)
            attempts.append(attempt)
            last_error = str(exc)
            if detail_log:
                log_warn(
                    "download.attempt.failed",
                    site=extraction.metadata.site,
                    content_id=extraction.metadata.content_id or "unknown",
                    attempt=f"{attempt_index}/{len(plans)}",
                    mode=plan.mode,
                    error=str(exc),
                )
            else:
                suppressed_failures += 1
                suppressed_last_error = str(exc)
            if output_path.exists():
                output_path.unlink()
    flush_suppressed_failures(outcome="failed")
    raise DownloadError(f"All candidates failed. Last error: {last_error}", attempts=attempts)

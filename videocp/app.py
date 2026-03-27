from __future__ import annotations

import json
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from videocp.bbdown import download_bilibili_with_bbdown
from videocp.browser import BrowserConfig, open_download_browser_session
from videocp.config import WatermarkConfig
from videocp.doctor import run_doctor
from videocp.downloader import (
    allocate_output_path,
    build_output_subdir,
    build_output_stem,
    download_best_candidate,
    sanitize_filename,
)
from videocp.extractor import extract_video
from videocp.input_parser import parse_input
from videocp.models import (
    DoctorCheck,
    DownloadArtifact,
    ExtractionResult,
    MediaCandidate,
    MediaKind,
    ParsedInput,
    TrackType,
    VideoMetadata,
    WatermarkMode,
)
from videocp.profile import default_profile_dir, detect_system_browser_executable
from videocp.profile_expander import INSTAGRAM_PROFILE_RE, expand_profile
from videocp.runtime_log import full_url, log_info, log_warn
from videocp.ytdlp import download_with_ytdlp, expand_ytdlp_playlist, fetch_ytdlp_metadata, write_netscape_cookies


@dataclass(slots=True)
class DownloadOptions:
    raw_inputs: list[str]
    output_dir: Path
    profile_dir: Path
    browser_path: str
    headless: bool
    timeout_secs: int
    input_file: Path | None = None
    max_concurrent: int = 1
    max_concurrent_per_site: int = 1
    start_interval_secs: float = 0.0
    watermark: WatermarkConfig | None = None
    profile_videos_count: int = 3


@dataclass(slots=True)
class DownloadJobResult:
    raw_input: str
    parsed_input: ParsedInput | None
    extraction: ExtractionResult | None
    artifact: DownloadArtifact | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.extraction is not None and self.artifact is not None and not self.error


@dataclass(slots=True)
class DoctorOptions:
    profile_dir: Path
    browser_path: str
    headless: bool


class StartIntervalGate:
    def __init__(self, interval_secs: float):
        self.interval_secs = max(0.0, interval_secs)
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(self) -> None:
        if self.interval_secs <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._next_allowed_at:
                    self._next_allowed_at = now + self.interval_secs
                    return
                sleep_for = self._next_allowed_at - now
            time.sleep(sleep_for)


def read_input_file(input_file: Path) -> list[str]:
    lines: list[str] = []
    for line in input_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    log_info("batch.input_file.loaded", input_file=input_file, count=len(lines))
    return lines


def collect_download_inputs(raw_inputs: list[str], input_file: Path | None) -> list[str]:
    combined = list(raw_inputs)
    if input_file is not None:
        combined.extend(read_input_file(input_file))
    if not combined:
        raise RuntimeError("No inputs were provided. Pass URLs directly or use --input-file.")
    return combined


def prepare_link_list(raw_inputs: list[str], input_file: Path | None, output_file: Path, timeout_secs: int) -> list[ParsedInput]:
    log_info("prepare_list.start", output_file=output_file, timeout_secs=timeout_secs)
    prepared = [parse_input(raw_input, timeout_secs=timeout_secs) for raw_input in collect_download_inputs(raw_inputs, input_file)]
    seen: set[str] = set()
    lines: list[str] = []
    for item in prepared:
        if item.canonical_url in seen:
            continue
        seen.add(item.canonical_url)
        lines.append(item.canonical_url)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    log_info("prepare_list.complete", output_file=output_file, count=len(lines))
    return prepared


def dedupe_prepared_inputs(prepared_inputs: list[ParsedInput]) -> list[ParsedInput]:
    seen: set[str] = set()
    unique: list[ParsedInput] = []
    for item in prepared_inputs:
        if item.canonical_url in seen:
            continue
        seen.add(item.canonical_url)
        unique.append(item)
    return unique


def _expand_profile_inputs(
    prepared_inputs: list[ParsedInput],
    browser_config: BrowserConfig,
    profile_videos_count: int,
    timeout_secs: int,
) -> list[ParsedInput]:
    """Separate profile inputs from video inputs, expand profiles to video URLs."""
    profile_inputs = [item for item in prepared_inputs if item.is_profile]
    video_inputs = [item for item in prepared_inputs if not item.is_profile]
    if not profile_inputs:
        return video_inputs

    native_profiles = [item for item in profile_inputs if item.provider_key != "ytdlp"]
    ytdlp_profiles = [item for item in profile_inputs if item.provider_key == "ytdlp"]

    log_info("profile.expand.batch_start", profiles=len(profile_inputs), max_per_profile=profile_videos_count)
    expanded: list[ParsedInput] = []

    # Native provider profiles: browser-based expansion
    if native_profiles:
        with open_download_browser_session(browser_config) as browser:
            for profile_input in native_profiles:
                page = browser.new_page()
                try:
                    result = expand_profile(
                        page=page,
                        profile_url=profile_input.canonical_url,
                        max_videos=profile_videos_count,
                        timeout_secs=timeout_secs,
                    )
                finally:
                    page.close()
                for url in result.pinned_urls:
                    expanded.append(ParsedInput(
                        raw_input=url,
                        extracted_url=url,
                        canonical_url=url,
                        provider_key=profile_input.provider_key,
                        is_pinned=True,
                        author_hint=result.author,
                    ))
                for url in result.video_urls:
                    expanded.append(ParsedInput(
                        raw_input=url,
                        extracted_url=url,
                        canonical_url=url,
                        provider_key=profile_input.provider_key,
                        author_hint=result.author,
                    ))

    # yt-dlp profiles: playlist expansion via yt-dlp (or browser for Instagram)
    if ytdlp_profiles:
        # Separate Instagram profiles (need browser expansion) from others (yt-dlp --flat-playlist)
        ig_profiles = [p for p in ytdlp_profiles if INSTAGRAM_PROFILE_RE.search(p.canonical_url)]
        other_ytdlp_profiles = [p for p in ytdlp_profiles if not INSTAGRAM_PROFILE_RE.search(p.canonical_url)]

        # Instagram: browser-based reel link extraction
        if ig_profiles:
            from videocp.profile_expander import _expand_instagram_reels
            with open_download_browser_session(browser_config) as browser:
                for profile_input in ig_profiles:
                    page = browser.new_page()
                    try:
                        result = _expand_instagram_reels(
                            page=page,
                            profile_url=profile_input.canonical_url,
                            max_videos=profile_videos_count,
                            timeout_secs=timeout_secs,
                        )
                    finally:
                        page.close()
                    for url in result.video_urls:
                        expanded.append(ParsedInput(
                            raw_input=url,
                            extracted_url=url,
                            canonical_url=url,
                            provider_key="ytdlp",
                            author_hint=result.author,
                        ))

        # Other yt-dlp profiles: playlist expansion via yt-dlp
        if other_ytdlp_profiles:
            cookies: list[dict] = []
            with open_download_browser_session(browser_config) as browser:
                cookies = browser.get_cookies()
            cookies_file: Path | None = None
            temp_dir = tempfile.mkdtemp(prefix="videocp-ytdlp-expand-")
            if cookies:
                cookies_file = Path(temp_dir) / "cookies.txt"
                write_netscape_cookies(cookies, cookies_file)
            for profile_input in other_ytdlp_profiles:
                result = expand_ytdlp_playlist(
                    url=profile_input.canonical_url,
                    max_videos=profile_videos_count,
                    cookies_file=cookies_file,
                )
                for url in result.video_urls:
                    expanded.append(ParsedInput(
                        raw_input=url,
                        extracted_url=url,
                        canonical_url=url,
                        provider_key="ytdlp",
                        author_hint=result.uploader,
                    ))

    log_info("profile.expand.batch_complete", expanded=len(expanded))
    combined = video_inputs + expanded
    return dedupe_prepared_inputs(combined)


def _download_prepared_input(
    parsed: ParsedInput,
    browser_config: BrowserConfig,
    timeout_secs: int,
) -> ExtractionResult:
    with open_download_browser_session(browser_config) as browser:
        page = browser.new_page()
        try:
            extraction = extract_video(page, parsed.canonical_url, timeout_secs=timeout_secs)
        finally:
            page.close()
    return extraction


def _download_extraction_artifact(
    extraction: ExtractionResult,
    output_dir: Path,
    timeout_secs: int,
    watermark: WatermarkConfig | None = None,
) -> DownloadArtifact:
    return download_best_candidate(extraction, output_dir=output_dir, timeout_secs=timeout_secs, watermark=watermark)


def _download_bilibili_input(
    parsed: ParsedInput,
    browser_config: BrowserConfig,
    output_dir: Path,
    timeout_secs: int,
    watermark: WatermarkConfig | None = None,
) -> tuple[ExtractionResult, DownloadArtifact]:
    return download_bilibili_with_bbdown(
        source_url=parsed.canonical_url,
        browser_config=browser_config,
        output_dir=output_dir,
        timeout_secs=timeout_secs,
        watermark=watermark,
        author_hint=parsed.author_hint,
    )


def _download_ytdlp_input(
    parsed: ParsedInput,
    browser_config: BrowserConfig,
    output_dir: Path,
    timeout_secs: int,
) -> tuple[ExtractionResult, DownloadArtifact]:
    """Download a video via yt-dlp, using cookies from the CDP browser."""
    # Get cookies from the browser session
    cookies: list[dict] = []
    with open_download_browser_session(browser_config) as browser:
        cookies = browser.get_cookies()

    # Fetch metadata from yt-dlp
    with tempfile.TemporaryDirectory(prefix="videocp-ytdlp-") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        cookies_file = temp_dir / "cookies.txt"
        if cookies:
            write_netscape_cookies(cookies, cookies_file)
        else:
            cookies_file = None

        meta = fetch_ytdlp_metadata(parsed.canonical_url, cookies_file)

        # Build extraction result for consistent output
        site = meta.site or sanitize_filename(parsed.canonical_url.split("/")[2])
        metadata = VideoMetadata(
            source_url=parsed.canonical_url,
            site=site,
            canonical_url=parsed.canonical_url,
            page_url=parsed.canonical_url,
            aweme_id=meta.id,
            author=meta.uploader,
            desc=meta.title,
            title=meta.title,
        )
        candidate = MediaCandidate(
            url=parsed.canonical_url,
            kind=MediaKind.MP4,
            track_type=TrackType.MUXED,
            watermark_mode=WatermarkMode.NO_WATERMARK,
            source="ytdlp",
            observed_via="ytdlp",
            note=f"yt-dlp: {meta.title}",
        )
        extraction = ExtractionResult(
            metadata=metadata,
            candidates=[candidate],
            cookies=cookies,
            user_agent="",
            diagnostics={"downloader": "ytdlp", "ytdlp_id": meta.id, "ytdlp_site": meta.site},
        )

        # Allocate output path
        subdir = build_output_subdir(extraction)
        stem = build_output_stem(extraction)
        output_path = allocate_output_path(output_dir, subdir, stem)
        sidecar_path = output_path.with_suffix(".json")

        # Download
        download_with_ytdlp(
            url=parsed.canonical_url,
            output_path=output_path,
            cookies_file=cookies_file,
            timeout_secs=timeout_secs,
        )

        # Write sidecar
        sidecar_payload = {
            "site": metadata.site,
            "content_id": metadata.content_id,
            "author": metadata.author,
            "desc": metadata.desc,
            "title": metadata.title,
            "source_url": metadata.source_url,
            "canonical_url": metadata.canonical_url,
            "page_url": metadata.page_url,
            "output_path": str(output_path),
            "chosen_candidate": candidate.to_dict(),
            "watermark_mode": candidate.watermark_mode.value,
            "candidates": [candidate.to_dict()],
            "diagnostics": extraction.diagnostics,
            "attempts": [{"url": parsed.canonical_url, "mode": "ytdlp", "status": "ok"}],
        }
        sidecar_path.write_text(
            json.dumps(sidecar_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        artifact = DownloadArtifact(
            output_path=output_path,
            sidecar_path=sidecar_path,
            chosen_candidate=candidate,
            attempts=[{"url": parsed.canonical_url, "mode": "ytdlp", "status": "ok"}],
        )
        return extraction, artifact


def _run_download_jobs(
    prepared_inputs: list[ParsedInput],
    browser_config: BrowserConfig,
    output_dir: Path,
    timeout_secs: int,
    max_concurrent: int,
    max_concurrent_per_site: int,
    start_interval_secs: float,
    watermark: WatermarkConfig | None = None,
) -> list[DownloadJobResult]:
    results: list[DownloadJobResult | None] = [None] * len(prepared_inputs)
    total_limit = max(1, max_concurrent)
    per_site_limit = max(1, max_concurrent_per_site)
    gate = StartIntervalGate(start_interval_secs)
    site_semaphores: dict[str, threading.Semaphore] = {}
    site_lock = threading.Lock()

    def site_semaphore(provider_key: str) -> threading.Semaphore:
        with site_lock:
            semaphore = site_semaphores.get(provider_key)
            if semaphore is None:
                semaphore = threading.Semaphore(per_site_limit)
                site_semaphores[provider_key] = semaphore
            return semaphore

    total_slots = threading.Semaphore(total_limit)
    log_info(
        "batch.download.start",
        jobs=len(prepared_inputs),
        output_dir=output_dir,
        max_concurrent=total_limit,
        max_concurrent_per_site=per_site_limit,
        start_interval_secs=start_interval_secs,
    )

    def wait_for_slot_release(active_futures: list) -> list:
        if not active_futures:
            return active_futures
        done, pending = wait(active_futures, return_when=FIRST_COMPLETED)
        for future in done:
            future.result()
        return list(pending)

    def worker(index: int, parsed: ParsedInput, semaphore: threading.Semaphore) -> None:
        extraction: ExtractionResult | None = None
        try:
            gate.wait()
            log_info(
                "job.extract.start",
                job=index + 1,
                site=parsed.provider_key or "unknown",
                url=full_url(parsed.canonical_url),
            )
            if parsed.provider_key == "ytdlp":
                extraction, artifact = _download_ytdlp_input(
                    parsed=parsed,
                    browser_config=browser_config,
                    output_dir=output_dir,
                    timeout_secs=timeout_secs,
                )
                results[index] = DownloadJobResult(
                    raw_input=parsed.raw_input,
                    parsed_input=parsed,
                    extraction=extraction,
                    artifact=artifact,
                )
                log_info(
                    "job.download.complete",
                    job=index + 1,
                    site=extraction.metadata.site,
                    content_id=extraction.metadata.content_id or "unknown",
                    output=artifact.output_path,
                )
            elif parsed.provider_key == "bilibili":
                extraction, artifact = _download_bilibili_input(
                    parsed=parsed,
                    browser_config=browser_config,
                    output_dir=output_dir,
                    timeout_secs=timeout_secs,
                    watermark=watermark,
                )
                results[index] = DownloadJobResult(
                    raw_input=parsed.raw_input,
                    parsed_input=parsed,
                    extraction=extraction,
                    artifact=artifact,
                )
                log_info(
                    "job.download.complete",
                    job=index + 1,
                    site=extraction.metadata.site,
                    content_id=extraction.metadata.content_id or "unknown",
                    output=artifact.output_path,
                )
            else:
                extraction = _download_prepared_input(
                    parsed=parsed,
                    browser_config=browser_config,
                    timeout_secs=timeout_secs,
                )
                if parsed.author_hint:
                    extraction.metadata.author = parsed.author_hint
                log_info(
                    "job.extract.complete",
                    job=index + 1,
                    site=parsed.provider_key or extraction.metadata.site,
                    content_id=extraction.metadata.content_id or "unknown",
                    candidates=len(extraction.candidates),
                )
                artifact = _download_extraction_artifact(
                    extraction=extraction,
                    output_dir=output_dir,
                    timeout_secs=timeout_secs,
                    watermark=watermark,
                )
                results[index] = DownloadJobResult(
                    raw_input=parsed.raw_input,
                    parsed_input=parsed,
                    extraction=extraction,
                    artifact=artifact,
                )
                log_info(
                    "job.download.complete",
                    job=index + 1,
                    site=parsed.provider_key or extraction.metadata.site,
                    content_id=extraction.metadata.content_id or "unknown",
                    output=artifact.output_path,
                )
        except Exception as exc:
            results[index] = DownloadJobResult(
                raw_input=parsed.raw_input,
                parsed_input=parsed,
                extraction=None,
                artifact=None,
                error=str(exc),
            )
            if extraction is None:
                log_warn(
                    "job.extract.failed",
                    job=index + 1,
                    site=parsed.provider_key or "unknown",
                    url=full_url(parsed.canonical_url),
                    error=str(exc),
                )
            else:
                log_warn(
                    "job.download.failed",
                    job=index + 1,
                    site=parsed.provider_key or extraction.metadata.site,
                    error=str(exc),
                )
        finally:
            semaphore.release()
            total_slots.release()

    pending_inputs = list(enumerate(prepared_inputs))
    active_futures: list = []
    with ThreadPoolExecutor(max_workers=total_limit) as executor:
        while pending_inputs or active_futures:
            started_any = False
            index = 0
            while index < len(pending_inputs):
                if not total_slots.acquire(blocking=False):
                    break
                item_index, parsed = pending_inputs[index]
                semaphore = site_semaphore(parsed.provider_key or "unknown")
                if not semaphore.acquire(blocking=False):
                    total_slots.release()
                    index += 1
                    continue
                pending_inputs.pop(index)
                started_any = True
                active_futures.append(executor.submit(worker, item_index, parsed, semaphore))
            if pending_inputs and not started_any:
                active_futures = wait_for_slot_release(active_futures)
                continue
            if active_futures:
                active_futures = wait_for_slot_release(active_futures)
    return [item for item in results if item is not None]


def download_videos(options: DownloadOptions) -> list[tuple[ExtractionResult, DownloadArtifact]]:
    browser_path = options.browser_path or detect_system_browser_executable()
    if not browser_path:
        raise RuntimeError("No Chrome-family browser found. Use --browser-path.")
    log_info(
        "download.session.start",
        output_dir=options.output_dir,
        profile_dir=options.profile_dir or default_profile_dir(),
        headless=options.headless,
    )
    browser_config = BrowserConfig(
        profile_dir=options.profile_dir or default_profile_dir(),
        browser_path=browser_path,
        headless=options.headless,
    )
    prepared_inputs = [
        parse_input(raw_input, timeout_secs=options.timeout_secs)
        for raw_input in collect_download_inputs(options.raw_inputs, options.input_file)
    ]
    prepared_inputs = dedupe_prepared_inputs(prepared_inputs)
    prepared_inputs = _expand_profile_inputs(
        prepared_inputs, browser_config, options.profile_videos_count, options.timeout_secs,
    )
    job_results = _run_download_jobs(
        prepared_inputs=prepared_inputs,
        browser_config=browser_config,
        output_dir=options.output_dir,
        timeout_secs=options.timeout_secs,
        max_concurrent=options.max_concurrent,
        max_concurrent_per_site=options.max_concurrent_per_site,
        start_interval_secs=options.start_interval_secs,
        watermark=options.watermark,
    )
    failures = [item for item in job_results if not item.ok]
    if failures:
        failed = failures[0]
        raise RuntimeError(f"Download failed for {failed.raw_input}: {failed.error}")
    return [(item.extraction, item.artifact) for item in job_results if item.ok]


def download_jobs(options: DownloadOptions) -> list[DownloadJobResult]:
    browser_path = options.browser_path or detect_system_browser_executable()
    if not browser_path:
        raise RuntimeError("No Chrome-family browser found. Use --browser-path.")
    log_info(
        "download.jobs.start",
        output_dir=options.output_dir,
        profile_dir=options.profile_dir or default_profile_dir(),
        headless=options.headless,
        timeout_secs=options.timeout_secs,
    )
    browser_config = BrowserConfig(
        profile_dir=options.profile_dir or default_profile_dir(),
        browser_path=browser_path,
        headless=options.headless,
    )
    prepared_inputs = [
        parse_input(raw_input, timeout_secs=options.timeout_secs)
        for raw_input in collect_download_inputs(options.raw_inputs, options.input_file)
    ]
    prepared_inputs = dedupe_prepared_inputs(prepared_inputs)
    prepared_inputs = _expand_profile_inputs(
        prepared_inputs, browser_config, options.profile_videos_count, options.timeout_secs,
    )
    return _run_download_jobs(
        prepared_inputs=prepared_inputs,
        browser_config=browser_config,
        output_dir=options.output_dir,
        timeout_secs=options.timeout_secs,
        max_concurrent=options.max_concurrent,
        max_concurrent_per_site=options.max_concurrent_per_site,
        start_interval_secs=options.start_interval_secs,
        watermark=options.watermark,
    )


def download_video(options: DownloadOptions) -> tuple[ExtractionResult, DownloadArtifact]:
    result = download_videos(options)
    if not result:
        raise RuntimeError("No inputs were provided.")
    return result[0]


def doctor(options: DoctorOptions) -> list[DoctorCheck]:
    return run_doctor(
        profile_dir=options.profile_dir or default_profile_dir(),
        browser_path=options.browser_path,
        headless=options.headless,
    )

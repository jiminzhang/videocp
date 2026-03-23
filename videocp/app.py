from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from videocp.browser import BrowserConfig, open_download_browser_session
from videocp.doctor import run_doctor
from videocp.downloader import download_best_candidate
from videocp.extractor import extract_video
from videocp.input_parser import parse_input
from videocp.models import DoctorCheck, DownloadArtifact, ExtractionResult, ParsedInput
from videocp.profile import default_profile_dir, detect_system_browser_executable
from videocp.runtime_log import full_url, log_info, log_warn


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
) -> DownloadArtifact:
    return download_best_candidate(extraction, output_dir=output_dir, timeout_secs=timeout_secs)


def _run_download_jobs(
    prepared_inputs: list[ParsedInput],
    browser_config: BrowserConfig,
    output_dir: Path,
    timeout_secs: int,
    max_concurrent: int,
    max_concurrent_per_site: int,
    start_interval_secs: float,
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
            extraction = _download_prepared_input(
                parsed=parsed,
                browser_config=browser_config,
                timeout_secs=timeout_secs,
            )
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
    job_results = _run_download_jobs(
        prepared_inputs=prepared_inputs,
        browser_config=browser_config,
        output_dir=options.output_dir,
        timeout_secs=options.timeout_secs,
        max_concurrent=options.max_concurrent,
        max_concurrent_per_site=options.max_concurrent_per_site,
        start_interval_secs=options.start_interval_secs,
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
    return _run_download_jobs(
        prepared_inputs=prepared_inputs,
        browser_config=browser_config,
        output_dir=options.output_dir,
        timeout_secs=options.timeout_secs,
        max_concurrent=options.max_concurrent,
        max_concurrent_per_site=options.max_concurrent_per_site,
        start_interval_secs=options.start_interval_secs,
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

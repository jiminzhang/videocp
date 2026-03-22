from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from videocp.browser import BrowserConfig, get_global_browser
from videocp.doctor import run_doctor
from videocp.downloader import download_best_candidate
from videocp.extractor import extract_video
from videocp.input_parser import parse_input
from videocp.models import DoctorCheck, DownloadArtifact, ExtractionResult
from videocp.profile import default_profile_dir, detect_system_browser_executable


@dataclass(slots=True)
class DownloadOptions:
    raw_inputs: list[str]
    output_dir: Path
    profile_dir: Path
    browser_path: str
    headless: bool
    timeout_secs: int


@dataclass(slots=True)
class DoctorOptions:
    profile_dir: Path
    browser_path: str
    headless: bool


def download_videos(options: DownloadOptions) -> list[tuple[ExtractionResult, DownloadArtifact]]:
    browser_path = options.browser_path or detect_system_browser_executable()
    if not browser_path:
        raise RuntimeError("No Chrome-family browser found. Use --browser-path.")
    browser_config = BrowserConfig(
        profile_dir=options.profile_dir or default_profile_dir(),
        browser_path=browser_path,
        headless=options.headless,
    )
    browser = get_global_browser(browser_config)
    results: list[tuple[ExtractionResult, DownloadArtifact]] = []
    for raw_input in options.raw_inputs:
        parsed = parse_input(raw_input, timeout_secs=options.timeout_secs)
        page = browser.new_page()
        try:
            extraction = extract_video(page, parsed.canonical_url, timeout_secs=options.timeout_secs)
        finally:
            page.close()
        artifact = download_best_candidate(extraction, output_dir=options.output_dir, timeout_secs=options.timeout_secs)
        results.append((extraction, artifact))
    return results


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

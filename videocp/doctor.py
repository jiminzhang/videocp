from __future__ import annotations

from pathlib import Path

from videocp.browser import BrowserConfig, BrowserSession, probe_cdp_endpoint
from videocp.downloader import find_ffmpeg
from videocp.models import DoctorCheck
from videocp.profile import detect_system_browser_executable, prepare_profile_seed_once


def run_doctor(
    profile_dir: Path,
    browser_path: str,
    headless: bool,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    resolved_browser = browser_path or detect_system_browser_executable()
    if resolved_browser:
        checks.append(DoctorCheck("browser_detect", True, resolved_browser))
    else:
        checks.append(DoctorCheck("browser_detect", False, "No Chrome-family browser found."))
        return checks

    seed_status, seed_source = prepare_profile_seed_once(profile_dir, resolved_browser)
    checks.append(
        DoctorCheck(
            "profile_seed",
            seed_status in {"seeded", "already_seeded", "skip_non_empty", "seed_source_empty"},
            f"status={seed_status}; source={seed_source or 'none'}",
        )
    )

    ffmpeg_path = find_ffmpeg()
    checks.append(
        DoctorCheck(
            "ffmpeg",
            bool(ffmpeg_path),
            ffmpeg_path or "ffmpeg not found; HLS fallback will fail.",
        )
    )

    try:
        with BrowserSession(
            BrowserConfig(
                profile_dir=profile_dir,
                browser_path=resolved_browser,
                headless=headless,
            )
        ) as browser:
            version_probe = probe_cdp_endpoint(browser.config.cdp_url)
            checks.append(
                DoctorCheck(
                    "cdp_startup",
                    bool(version_probe.get("tcp_ok")) and bool(version_probe.get("http_ok")),
                    str(version_probe),
                )
            )
    except Exception as exc:
        checks.append(DoctorCheck("cdp_startup", False, str(exc)))

    return checks

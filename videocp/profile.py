from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

PROFILE_SEED_MARKER_FILE = ".videocp_profile_seeded"


def default_profile_dir() -> Path:
    home = Path.home()
    if platform.system().lower() == "darwin":
        return home / "Library/Caches/videocp/chrome-profile"
    return home / ".cache/videocp/chrome-profile"


def detect_system_browser_executable() -> str:
    system_name = platform.system().lower()
    static_candidates: list[Path] = []
    if system_name == "darwin":
        static_candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    elif system_name == "windows":
        roots = [
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        suffixes = [
            "Google/Chrome/Application/chrome.exe",
            "Microsoft/Edge/Application/msedge.exe",
            "BraveSoftware/Brave-Browser/Application/brave.exe",
            "Chromium/Application/chrome.exe",
        ]
        for root in roots:
            if not root:
                continue
            base = Path(root)
            for suffix in suffixes:
                static_candidates.append(base / suffix)
    for path in static_candidates:
        if path.exists():
            return str(path)
    command_candidates = [
        "google-chrome-stable",
        "google-chrome",
        "chromium-browser",
        "chromium",
        "microsoft-edge",
        "msedge",
        "brave-browser",
        "brave",
    ]
    for command in command_candidates:
        found = shutil.which(command)
        if found:
            return found
    return ""


def infer_browser_family(executable_path: str) -> str:
    normalized = executable_path.lower()
    if "edge" in normalized or "msedge" in normalized:
        return "edge"
    if "brave" in normalized:
        return "brave"
    if "chromium" in normalized:
        return "chromium"
    if "chrome" in normalized:
        return "chrome"
    return "unknown"


def default_system_user_data_dir_map() -> dict[str, Path]:
    home = Path.home()
    system_name = platform.system().lower()
    if system_name == "darwin":
        return {
            "chrome": home / "Library/Application Support/Google/Chrome",
            "edge": home / "Library/Application Support/Microsoft Edge",
            "brave": home / "Library/Application Support/BraveSoftware/Brave-Browser",
            "chromium": home / "Library/Application Support/Chromium",
        }
    if system_name == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", "").strip() or home / "AppData/Local")
        return {
            "chrome": base / "Google/Chrome/User Data",
            "edge": base / "Microsoft/Edge/User Data",
            "brave": base / "BraveSoftware/Brave-Browser/User Data",
            "chromium": base / "Chromium/User Data",
        }
    return {
        "chrome": home / ".config/google-chrome",
        "edge": home / ".config/microsoft-edge",
        "brave": home / ".config/BraveSoftware/Brave-Browser",
        "chromium": home / ".config/chromium",
    }


def ordered_user_data_dir_candidates(family: str) -> list[Path]:
    mapping = default_system_user_data_dir_map()
    result: list[Path] = []
    preferred = mapping.get(family)
    if preferred is not None:
        result.append(preferred)
    for item in mapping.values():
        if item not in result:
            result.append(item)
    return result


def detect_seed_source_profile_dir(executable_path: str) -> Path | None:
    family = infer_browser_family(executable_path)
    for candidate in ordered_user_data_dir_candidates(family):
        if candidate.exists():
            return candidate
    return None


def has_profile_data(profile_dir: Path) -> bool:
    for item in profile_dir.iterdir():
        if item.name == PROFILE_SEED_MARKER_FILE:
            continue
        return True
    return False


def seed_entry_names(source_dir: Path) -> list[str]:
    names: list[str] = []
    for fixed in ["Local State", "Last Version", "Default"]:
        if (source_dir / fixed).exists():
            names.append(fixed)
    for item in source_dir.iterdir():
        if item.is_dir() and item.name.startswith("Profile "):
            names.append(item.name)
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def ignore_copy_entries(_: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lowered = name.lower()
        if name.startswith("Singleton"):
            ignored.add(name)
            continue
        if lowered in {
            "runningchromeversion",
            "devtoolsactiveport",
            "cache",
            "code cache",
            "gpucache",
            "shadercache",
            "grshadercache",
            "graphitedawncache",
            "browsermetrics",
            "crashpad",
            "safe browsing",
            "component_crx_cache",
            "extensions_crx_cache",
            "download_cache",
        }:
            ignored.add(name)
            continue
        if lowered.endswith(".lock"):
            ignored.add(name)
    return ignored


def copy_profile_seed_from_source(source_dir: Path, profile_dir: Path) -> bool:
    names = seed_entry_names(source_dir)
    if not names:
        return False
    copied_any = False
    for name in names:
        source = source_dir / name
        target = profile_dir / name
        if not source.exists() or source.is_symlink():
            continue
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=ignore_copy_entries,
                dirs_exist_ok=False,
            )
            copied_any = True
            continue
        shutil.copy2(source, target)
        copied_any = True
    return copied_any


def prepare_profile_seed_once(profile_dir: Path, executable_path: str) -> tuple[str, str]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    marker = profile_dir / PROFILE_SEED_MARKER_FILE
    if marker.exists():
        return "already_seeded", marker.read_text(encoding="utf-8").strip()
    if has_profile_data(profile_dir):
        return "skip_non_empty", ""
    source_dir = detect_seed_source_profile_dir(executable_path)
    if source_dir is None:
        return "source_not_found", ""
    copied = copy_profile_seed_from_source(source_dir, profile_dir)
    marker.write_text(str(source_dir), encoding="utf-8")
    if copied:
        return "seeded", str(source_dir)
    return "seed_source_empty", str(source_dir)


def clear_profile_transient_artifacts(profile_dir: Path) -> list[str]:
    removed: list[str] = []
    for entry in profile_dir.iterdir():
        name = entry.name
        lowered = name.lower()
        should_remove = (
            name.startswith("Singleton")
            or lowered == "devtoolsactiveport"
            or lowered == "runningchromeversion"
            or lowered.endswith(".lock")
        )
        if not should_remove:
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed.append(name)
        except FileNotFoundError:
            continue
    return removed


def profile_lock_hint(profile_dir: Path) -> str:
    lock_markers = ["SingletonLock", "SingletonCookie", "SingletonSocket"]
    if any((profile_dir / marker).exists() for marker in lock_markers):
        return (
            "Profile seems to be in use (Singleton* lock files found). "
            "Close all windows using this profile or set a different --profile-dir."
        )
    return ""

from pathlib import Path

from videocp.profile import (
    PROFILE_SEED_MARKER_FILE,
    default_system_user_data_dir_map,
    ignore_copy_entries,
    infer_browser_family,
    prepare_profile_seed_once,
)


def test_ignore_copy_entries_filters_transient_chrome_files():
    names = [
        "SingletonLock",
        "DevToolsActivePort",
        "Cache",
        "Code Cache",
        "keep.txt",
        "Cookies.lock",
    ]
    ignored = ignore_copy_entries("", names)
    assert "SingletonLock" in ignored
    assert "DevToolsActivePort" in ignored
    assert "Cache" in ignored
    assert "Code Cache" in ignored
    assert "Cookies.lock" in ignored
    assert "keep.txt" not in ignored


def test_infer_browser_family():
    assert infer_browser_family("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome") == "chrome"
    assert infer_browser_family("/Applications/Chromium.app/Contents/MacOS/Chromium") == "chromium"
    assert infer_browser_family("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser") == "brave"


def test_default_system_user_data_dir_map_contains_chrome_path():
    mapping = default_system_user_data_dir_map()
    assert "chrome" in mapping
    assert isinstance(mapping["chrome"], Path)


def test_prepare_profile_seed_once_syncs_new_profiles_for_seeded_dir(tmp_path: Path, monkeypatch):
    source_dir = tmp_path / "source"
    profile_dir = tmp_path / "profile"
    source_dir.mkdir()
    profile_dir.mkdir()

    (source_dir / "Local State").write_text("new-local-state", encoding="utf-8")
    (source_dir / "Last Version").write_text("123", encoding="utf-8")
    (source_dir / "Default").mkdir()
    (source_dir / "Default" / "Preferences").write_text("default", encoding="utf-8")
    (source_dir / "Profile 7").mkdir()
    (source_dir / "Profile 7" / "Preferences").write_text("profile-7", encoding="utf-8")

    (profile_dir / PROFILE_SEED_MARKER_FILE).write_text(str(source_dir), encoding="utf-8")
    (profile_dir / "Local State").write_text("old-local-state", encoding="utf-8")
    (profile_dir / "Default").mkdir()
    (profile_dir / "Default" / "Preferences").write_text("keep-existing-default", encoding="utf-8")

    monkeypatch.setattr("videocp.profile.detect_seed_source_profile_dir", lambda _: source_dir)

    status, source = prepare_profile_seed_once(profile_dir, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

    assert status == "already_seeded_synced"
    assert source == str(source_dir)
    assert (profile_dir / "Local State").read_text(encoding="utf-8") == "new-local-state"
    assert (profile_dir / "Profile 7" / "Preferences").read_text(encoding="utf-8") == "profile-7"
    assert (profile_dir / "Default" / "Preferences").read_text(encoding="utf-8") == "keep-existing-default"

from pathlib import Path

from videocp.profile import default_system_user_data_dir_map, ignore_copy_entries, infer_browser_family


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

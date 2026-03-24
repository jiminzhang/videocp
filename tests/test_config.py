from pathlib import Path

import pytest

from videocp.config import find_config_path, load_app_config, load_sync_config
from videocp.errors import SyncError


def test_find_config_path_searches_parent_directories(tmp_path: Path):
    root = tmp_path / "project"
    nested = root / "downloads" / "nested"
    nested.mkdir(parents=True)
    config_path = root / "config.yaml"
    config_path.write_text("download:\n  output_dir: ./downloads\n", encoding="utf-8")

    found = find_config_path(nested)

    assert found == config_path


def test_load_app_config_resolves_paths_relative_to_config(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    config_path = root / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "download:",
                "  output_dir: ./downloads",
                "  max_concurrent: 3",
                "  max_concurrent_per_site: 2",
                "  start_interval_secs: 1.5",
                "browser:",
                "  profile_dir: ./profiles/chrome",
                "  browser_path: /Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "request:",
                "  timeout_secs: 45",
                "",
            ]
        ),
        encoding="utf-8",
    )

    config = load_app_config(config_path, start_dir=root)

    assert config.output_dir == (root / "downloads").resolve()
    assert config.profile_dir == (root / "profiles/chrome").resolve()
    assert config.browser_path.endswith("Google Chrome")
    assert config.timeout_secs == 45
    assert config.max_concurrent == 3
    assert config.max_concurrent_per_site == 2
    assert config.start_interval_secs == 1.5


def test_load_app_config_defaults_to_cwd_when_missing(tmp_path: Path):
    config = load_app_config(None, start_dir=tmp_path)

    assert config.output_dir == (tmp_path / "downloads").resolve()
    assert config.max_concurrent == 1
    assert config.max_concurrent_per_site == 1
    assert config.start_interval_secs == 0.0
    assert config.source_path is None


def test_load_app_config_rejects_missing_explicit_path(tmp_path: Path):
    with pytest.raises(ValueError, match="Config file not found"):
        load_app_config(tmp_path / "missing.yaml", start_dir=tmp_path)


def test_load_sync_config_allows_missing_channel_id_for_global_cdp(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    tasks_path = root / "tasks.yaml"
    tasks_path.write_text(
        "\n".join(
            [
                "sync:",
                "  publish_method: cdp",
                "tasks:",
                "  - name: demo",
                "    source_url: https://example.com/video",
                "    guild_id: \"123\"",
                "",
            ]
        ),
        encoding="utf-8",
    )

    config = load_sync_config(tasks_path, start_dir=root)

    assert config.publish_method == "cdp"
    assert len(config.tasks) == 1
    assert config.tasks[0].channel_id == ""
    assert config.tasks[0].publish_method == ""


def test_load_sync_config_allows_missing_channel_id_for_task_level_cdp(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    tasks_path = root / "tasks.yaml"
    tasks_path.write_text(
        "\n".join(
            [
                "sync:",
                "  publish_method: skill",
                "tasks:",
                "  - name: demo",
                "    source_url: https://example.com/video",
                "    guild_id: \"123\"",
                "    publish_method: CDP",
                "",
            ]
        ),
        encoding="utf-8",
    )

    config = load_sync_config(tasks_path, start_dir=root)

    assert config.publish_method == "skill"
    assert len(config.tasks) == 1
    assert config.tasks[0].channel_id == ""
    assert config.tasks[0].publish_method == "cdp"


def test_load_sync_config_requires_channel_id_for_skill_publish(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    tasks_path = root / "tasks.yaml"
    tasks_path.write_text(
        "\n".join(
            [
                "sync:",
                "  publish_method: skill",
                "tasks:",
                "  - name: demo",
                "    source_url: https://example.com/video",
                "    guild_id: \"123\"",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SyncError, match="channel_id"):
        load_sync_config(tasks_path, start_dir=root)

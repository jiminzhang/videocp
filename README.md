# videocp

Douyin downloader implemented in Python, using a dedicated copied Chrome profile plus CDP extraction for higher success rates.

## Install

```bash
python3 -m pip install -e '.[dev]'
```

The tool reuses an installed Chrome-family browser. `ffmpeg` is optional but recommended for HLS fallback.

## Usage

```bash
videocp doctor
videocp download '7.86 复制打开抖音，看看【示例】 https://v.douyin.com/xxxxxx/'
videocp download 'https://www.douyin.com/video/1234567890' --output-dir ./downloads --json
videocp download 'https://www.douyin.com/video/111' 'https://www.douyin.com/video/222'
```

## Notes

- First run copies local Chrome profile state into an app-owned cache directory.
- One Chrome instance is reused per process and each input uses a separate tab.
- The downloader tries no-watermark candidates first and falls back to stable playable assets.
- Single-video pages are supported in v1. Live streams, albums, and playlists are out of scope.

# videocp

Video downloader for Douyin, Bilibili, and Xiaohongshu, implemented in Python and using a dedicated copied Chrome profile plus CDP extraction for higher success rates.

## Install

```bash
python3 -m pip install -e '.[dev]'
```

The tool reuses an installed Chrome-family browser. `ffmpeg` is optional but recommended for HLS fallback.

## Usage

```bash
videocp doctor
videocp download '7.86 复制打开抖音，看看【示例】 https://v.douyin.com/xxxxxx/'
videocp download 'https://www.bilibili.com/video/BV1764y1y76G/'
videocp download 'https://www.xiaohongshu.com/explore/69be081c0000000021010b12?xsec_token=...'
videocp download 'https://www.douyin.com/video/1234567890' --output-dir ./downloads --json
videocp download 'https://www.douyin.com/video/111' 'https://www.douyin.com/video/222'
videocp prepare-list --output-file ./links.txt 'https://www.douyin.com/jingxuan?modal_id=7596491775800282387' 'https://www.bilibili.com/video/BV1764y1y76G/'
videocp download --input-file ./links.txt --max-concurrent 2 --max-concurrent-per-site 1 --start-interval-secs 5
```

The CLI also reads `config.yaml`. It searches from the current directory upward, so running inside a subdirectory still picks the repo-level config.

```yaml
download:
  output_dir: ./downloads
  max_concurrent: 1
  max_concurrent_per_site: 1
  start_interval_secs: 0

browser:
  profile_dir: ~/Library/Caches/videocp/chrome-profile
  browser_path: ""
  headless: false

request:
  timeout_secs: 30
```

## Notes

- First run copies local Chrome profile state into an app-owned cache directory.
- One Chrome instance is reused per process and each input uses a separate tab.
- All supported sites use the same Chrome + CDP probing flow. Site-specific logic only handles URL matching, metadata extraction, and media candidate discovery.
- `prepare-list` can normalize mixed share text into a plain txt URL list, and `download --input-file` can consume that list directly.
- Batch downloads support total concurrency, per-site concurrency, and task start spacing. For CDP stability, browser extraction stays serialized and file downloads overlap in parallel.
- Default filenames are simplified to `<author>_<content_id>.mp4`.
- The downloader tries no-watermark candidates first and falls back to stable playable assets.
- Single-video pages are supported in v1. Live streams, albums, and playlists are out of scope.

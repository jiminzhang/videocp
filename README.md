# videocp

Video downloader for Douyin, Bilibili, Xiaohongshu, and other sites (via yt-dlp), implemented in Python and using a dedicated copied Chrome profile plus CDP extraction for higher success rates.

## Features

- Download videos from Douyin, Bilibili, and Xiaohongshu single-video pages
- **Generic site support**: YouTube and other sites supported by yt-dlp, with browser cookies automatically exported for authenticated downloads
- **Profile/space page support**: pass a user profile URL to batch-download the most recent N videos
  - Douyin: `https://www.douyin.com/user/xxx` (skips pinned videos, only downloads recent ones)
  - Bilibili: `https://space.bilibili.com/xxx`
  - Xiaohongshu: `https://www.xiaohongshu.com/user/profile/xxx` (video notes only)
- **LLM-based watermark removal**: optionally detect and remove Bilibili watermarks via Gemini + ffmpeg delogo
- Batch download with concurrency control and per-site rate limiting
- Output organized as `{site}-{author}/{content_id}.mp4`
- No-watermark candidates tried first, with fallback to stable playable assets

## Install

```bash
python3 -m pip install -e '.[dev]'
```

The tool reuses an installed Chrome-family browser.

External tools (install separately):

| Tool | Required | Purpose |
|------|----------|---------|
| Chrome-family browser | Yes | CDP extraction for Douyin/Bilibili/Xiaohongshu |
| `ffmpeg` | Recommended | HLS fallback, video/audio muxing, watermark removal |
| `yt-dlp` | Optional | Download from YouTube and other non-builtin sites |

```bash
# macOS
brew install ffmpeg yt-dlp
```

## Usage
先在自己浏览器登录b站，抖音，小红书
```bash
videocp doctor

# 单视频下载
videocp download '7.86 复制打开抖音，看看【示例】 https://v.douyin.com/xxxxxx/'
videocp download 'https://www.bilibili.com/video/BV1764y1y76G/'
videocp download 'https://www.xiaohongshu.com/explore/69be081c0000000021010b12?xsec_token=...'
videocp download 'https://www.douyin.com/video/1234567890' --output-dir ./downloads --json

# 其他网站（通过 yt-dlp）
videocp download 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

# 用户主页批量下载（默认最新3条视频）
videocp download 'https://www.douyin.com/user/MS4wLjABAAAAxxxxxx'
videocp download 'https://space.bilibili.com/7612168'
videocp download 'https://www.xiaohongshu.com/user/profile/5756c80da9b2ed37b185c08e'
videocp download 'https://www.youtube.com/@hackbearterry/shorts'
videocp download 'https://www.youtube.com/@hackbearterry/videos'

# 指定下载数量
videocp download 'https://space.bilibili.com/7612168' --profile-videos-count 5

# 多输入 & 批量文件
videocp download 'https://www.douyin.com/video/111' 'https://www.douyin.com/video/222'
videocp prepare-list --output-file ./links.txt 'https://www.douyin.com/jingxuan?modal_id=7596491775800282387' 'https://www.bilibili.com/video/BV1764y1y76G/'
videocp download --input-file ./links.txt
```

## Configuration

The CLI reads `config.yaml`, searching from the current directory upward.

```yaml
download:
  output_dir: ./downloads
  max_concurrent: 3
  max_concurrent_per_site: 1
  start_interval_secs: 0
  profile_videos_count: 3  # number of recent videos to download from a profile page

browser:
  profile_dir: ~/Library/Caches/videocp/chrome-profile
  browser_path: ""
  headless: false

request:
  timeout_secs: 30

watermark:
  enabled: false
  # api_key: ""  # falls back to OPENROUTER_API_KEY env var
  base_url: https://openrouter.ai/api/v1/chat/completions
  model: google/gemini-3-flash-preview
```

CLI arguments override config values: `--output-dir`, `--profile-videos-count`, `--headless`, `--timeout-secs`, etc.

## Notes

- First run copies local Chrome profile state into an app-owned cache directory, and later runs sync newly added browser profiles into that copied profile.
- Download runs reuse one dedicated Chrome instance, reconnect to an already running instance when possible, and open one tab per input.
- All supported sites use the same Chrome + CDP probing flow. Site-specific logic only handles URL matching, metadata extraction, and media candidate discovery.
- `prepare-list` can normalize mixed share text into a plain txt URL list, and `download --input-file` can consume that list directly.
- Batch download concurrency, per-site limits, and task start spacing are controlled through `config.yaml` or CLI arguments.
- Browser extraction and file downloads can overlap across inputs, while still reusing the same Chrome instance.
- Output files are organized as `{site}-{author}/{content_id}.mp4` with a JSON sidecar.
- The downloader tries no-watermark candidates first and falls back to stable playable assets.
- Single-video pages and user profile pages are supported. Live streams, albums, and playlists are out of scope.
- URLs not matching built-in providers are automatically routed to yt-dlp. Browser cookies are exported in Netscape format so yt-dlp can access authenticated content.

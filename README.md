# videocp

Video downloader for Douyin, Bilibili, Xiaohongshu, Instagram, and other sites (via yt-dlp), implemented in Python. Douyin/Xiaohongshu use a dedicated copied Chrome profile plus CDP extraction, while Bilibili defaults to a Python TV-mode flow modeled after BBDown.

## Features

- Download videos from Douyin, Bilibili, and Xiaohongshu single-video pages
- **Bilibili via TV mode**: default Bilibili downloads use a built-in Python implementation modeled after BBDown's TV flow; if no cached TV token is found, videocp opens a browser QR page and waits for you to scan once
- **Instagram support**: download single reels/posts, or batch-download from a user's reels page
- **Generic site support**: YouTube and other sites supported by yt-dlp, with browser cookies automatically exported for authenticated downloads
- **Profile/space page support**: pass a user profile URL to batch-download the most recent N videos
  - Douyin: `https://www.douyin.com/user/xxx` (skips pinned videos, only downloads recent ones)
  - Bilibili: `https://space.bilibili.com/xxx`
  - Xiaohongshu: `https://www.xiaohongshu.com/user/profile/xxx` (video notes only)
  - Instagram: `https://www.instagram.com/username/reels/`
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
| Chrome-family browser | Yes | CDP extraction for Douyin/Xiaohongshu and visible Bilibili TV QR login |
| `ffmpeg` | Recommended | HLS fallback, video/audio muxing, watermark removal |
| `yt-dlp` | Optional | Download from YouTube and other non-builtin sites |

```bash
# macOS
brew install ffmpeg yt-dlp
```

## Usage
先在自己浏览器登录b站，抖音，小红书，Instagram 等需要登录的网站
```bash
videocp doctor

# 单视频下载
videocp download '7.86 复制打开抖音，看看【示例】 https://v.douyin.com/xxxxxx/'
videocp download 'https://www.bilibili.com/video/BV1764y1y76G/'
videocp download 'https://www.xiaohongshu.com/explore/69be081c0000000021010b12?xsec_token=...'
videocp download 'https://www.douyin.com/video/1234567890' --output-dir ./downloads --json

# 其他网站（通过 yt-dlp）
videocp download 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

# Instagram 单个 reel
videocp download 'https://www.instagram.com/reel/DWQQpz5lLZD/'

# 用户主页批量下载（默认最新3条视频）
videocp download 'https://www.douyin.com/user/MS4wLjABAAAAxxxxxx'
videocp download 'https://space.bilibili.com/7612168'
videocp download 'https://www.xiaohongshu.com/user/profile/5756c80da9b2ed37b185c08e'
videocp download 'https://www.instagram.com/ddk69k/reels/'
videocp download 'https://www.youtube.com/@hackbearterry/shorts'
videocp download 'https://www.youtube.com/@hackbearterry/videos'

# 指定下载数量
videocp download 'https://space.bilibili.com/7612168' --profile-videos-count 5

# 无头模式（不弹出浏览器窗口）
videocp download 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' --headless

# 多输入 & 批量文件
videocp download 'https://www.douyin.com/video/111' 'https://www.douyin.com/video/222'
videocp prepare-list --output-file ./links.txt 'https://www.douyin.com/jingxuan?modal_id=7596491775800282387' 'https://www.bilibili.com/video/BV1764y1y76G/'
videocp download --input-file ./links.txt
```

## Sync To QQ Channel

`videocp sync` / `video sync` will fetch the latest videos from configured source profiles, download them, and publish them with the configured method.

Typical flow:

```bash
# 先确认浏览器/CDP正常
video doctor

# 按 tasks.yaml 执行完整同步
video sync

# 只跑一个任务
video sync --task-name youtube-01coder30

# 每个任务只取最新 1 条
video sync --count 1

# JSON 输出
video sync --json
```

Example `tasks.yaml`:

```yaml
sync:
  history_file: ./sync_history.json
  skill_dir: ~/.openclaw/workspace/skills/tencent-channel-community
  videos_per_task: 2
  publish_method: cdp # skill(author) | cdp(channel)

tasks:
  - name: "youtube-01coder30"
    source_url: "https://www.youtube.com/@01coder30/videos"
    guild_id: "657469764024457583"
    title_template: "{desc}"
    content_template: ""
    feed_type: 2

  - name: "英語天天學"
    source_url: "https://www.youtube.com/@%E8%8B%B1%E8%AA%9E%E5%A4%A9%E5%A4%A9%E5%AD%B8/shorts"
    guild_id: "657469764024457583"
    title_template: "{desc}"
    content_template: ""
    feed_type: 2
```

Notes for sync:

- `publish_method: skill` now always publishes with the author identity. Any configured `guild_id` / `channel_id` are ignored in this mode.
- `publish_method: cdp` uses the real browser publish page. It clicks the site publish button instead of calling the site publish API directly.
- After a successful `cdp` publish, the browser page stays open for about 4 seconds before closing, to avoid an obviously bot-like instant exit.
- `guild_id` is required for `publish_method: cdp`. `channel_id` is optional there and currently ignored.
- `history_file` records published items. Entries with status `ok` or `skipped_unavailable` are treated as already processed and will be skipped on later runs.
- If a source video is unavailable for download, such as YouTube members-only content, sync marks it as `skipped_unavailable` instead of failing the whole run.
- `title_template` and `content_template` support placeholders like `{desc}`, `{author}`, `{site}`, and `{content_id}`.

## Configuration

The CLI reads `config.yaml`, and `sync` also reads `tasks.yaml`, searching from the current directory upward.

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
  headless: false  # true = no browser window; CLI: --headless / --no-headless

request:
  timeout_secs: 30

watermark:
  enabled: false
  # api_key: ""  # falls back to OPENROUTER_API_KEY env var
  base_url: https://openrouter.ai/api/v1/chat/completions
  model: google/gemini-2.5-flash
```

CLI arguments override config values:

| Argument | Description |
|----------|-------------|
| `--output-dir` | Output directory |
| `--headless` / `--no-headless` | Run Chrome without/with a visible window |
| `--timeout-secs` | Request timeout in seconds |
| `--profile-videos-count` | Number of recent videos to download from a profile page |
| `--browser-path` | Chrome executable path |
| `--profile-dir` | Dedicated Chrome profile directory |
| `--input-file` | Text file with one URL per line |
| `--json` | Print result as JSON |

## Notes

- First run copies local Chrome profile state into an app-owned cache directory, and later runs sync newly added browser profiles into that copied profile.
- Download runs reuse one dedicated Chrome instance, reconnect to an already running instance when possible, and open one tab per input.
- Douyin/Xiaohongshu still use the Chrome + CDP probing flow. Bilibili defaults to a built-in TV-mode downloader modeled after BBDown, with a cached TV token and browser QR login when needed.
- `prepare-list` can normalize mixed share text into a plain txt URL list, and `download --input-file` can consume that list directly.
- Batch download concurrency, per-site limits, and task start spacing are controlled through `config.yaml` or CLI arguments.
- Browser extraction and file downloads can overlap across inputs, while still reusing the same Chrome instance.
- Output files are organized as `{site}-{author}/{content_id}.mp4` with a JSON sidecar.
- The downloader tries no-watermark candidates first and falls back to stable playable assets.
- Single-video pages and user profile pages are supported. Live streams, albums, and playlists are out of scope.
- URLs not matching built-in providers are automatically routed to yt-dlp. Browser cookies are exported in Netscape format so yt-dlp can access authenticated content.

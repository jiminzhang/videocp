from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import random
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import qrcode
import requests
from qrcode.image.svg import SvgImage

from videocp.browser import BrowserConfig, BrowserSession, build_cdp_url, find_free_local_port
from videocp.config import WatermarkConfig
from videocp.downloader import download_best_candidate
from videocp.errors import DownloadError
from videocp.models import (
    DownloadArtifact,
    ExtractionResult,
    MediaCandidate,
    MediaKind,
    TrackType,
    VideoMetadata,
    WatermarkMode,
)
from videocp.providers import BILIBILI_VIDEO_ID_RE, extract_id_from_url
from videocp.runtime_log import log_info, log_warn

TV_LOGIN_AUTH_URL = "https://passport.snm0516.aisee.tv/x/passport-tv-login/qrcode/auth_code"
TV_LOGIN_POLL_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll"
TV_PLAYURL_API = "https://api.snm0516.aisee.tv/x/tv/playurl"
BILIBILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
TV_API_SIGN_SECRET = "59b43e04ad6965f34319062b478f83dd"
TV_LOGIN_WAIT_SECS = 300
TV_LOGIN_POLL_INTERVAL_SECS = 1
TV_TOKEN_FILE = "BBDownTV.data"
TV_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; OnePlus7TPro Build/QKQ1.190716.003) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}
BASE_URL_PORT_RE = re.compile(r"http.*:\d+", re.IGNORECASE)
_TV_LOGIN_LOCK = threading.Lock()

QUALITY_PRIORITY = {
    "127": 13,
    "126": 12,
    "125": 11,
    "120": 10,
    "116": 9,
    "112": 8,
    "100": 7,
    "80": 6,
    "74": 5,
    "64": 4,
    "48": 3,
    "32": 2,
    "16": 1,
    "6": 0,
    "5": -1,
}

CODEC_COMPAT_PRIORITY = {
    7: 3,   # AVC / H.264
    12: 2,  # HEVC / H.265
    13: 1,  # AV1
}


@dataclass(slots=True)
class BilibiliPageInfo:
    aid: str
    bvid: str
    cid: str
    page_index: int
    title: str
    desc: str
    author: str


def bbdown_state_dir(profile_dir: Path) -> Path:
    return profile_dir.parent / "bbdown"


def bbdown_tv_token_path(profile_dir: Path) -> Path:
    return bbdown_state_dir(profile_dir) / TV_TOKEN_FILE


def load_bbdown_tv_token(profile_dir: Path) -> str:
    token_path = bbdown_tv_token_path(profile_dir)
    if not token_path.is_file():
        return ""
    raw = token_path.read_text(encoding="utf-8").strip()
    if raw.startswith("access_token="):
        return raw[len("access_token="):].strip()
    return raw


def save_bbdown_tv_token(profile_dir: Path, token: str) -> Path:
    state_dir = bbdown_state_dir(profile_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    token_path = bbdown_tv_token_path(profile_dir)
    token_path.write_text(f"access_token={token.strip()}\n", encoding="utf-8")
    return token_path


def ensure_bbdown_tv_token(browser_config: BrowserConfig, timeout_secs: int) -> str:
    token = load_bbdown_tv_token(browser_config.profile_dir)
    if token:
        return token
    with _TV_LOGIN_LOCK:
        token = load_bbdown_tv_token(browser_config.profile_dir)
        if token:
            return token
        token = _login_tv_in_browser(browser_config.browser_path, timeout_secs=max(timeout_secs, TV_LOGIN_WAIT_SECS))
        save_bbdown_tv_token(browser_config.profile_dir, token)
        return token


def infer_bbdown_select_page(url: str) -> str:
    page_values = parse_qs(urlparse(url).query).get("p")
    if page_values and page_values[0].strip():
        return page_values[0].strip()
    return "1"


def download_bilibili_with_bbdown(
    *,
    source_url: str,
    browser_config: BrowserConfig,
    output_dir: Path,
    timeout_secs: int,
    watermark: WatermarkConfig | None = None,
    metadata_seed: VideoMetadata | None = None,
    author_hint: str = "",
) -> tuple[ExtractionResult, DownloadArtifact]:
    token = ensure_bbdown_tv_token(browser_config, timeout_secs=timeout_secs)
    page_info = fetch_bilibili_page_info(source_url, timeout_secs=timeout_secs, author_hint=author_hint)
    metadata = build_bbdown_metadata(
        source_url,
        page_info=page_info,
        metadata_seed=metadata_seed,
        author_hint=author_hint,
    )
    candidates = fetch_bilibili_tv_candidates(page_info, token=token, timeout_secs=timeout_secs)
    if not candidates:
        raise DownloadError("Bilibili TV API returned no playable candidates.")
    extraction = ExtractionResult(
        metadata=metadata,
        candidates=candidates,
        cookies=[],
        user_agent=TV_API_HEADERS["User-Agent"],
        diagnostics={
            "downloader": "bbdown_python_tv",
            "mode": "tv_api",
            "aid": page_info.aid,
            "bvid": page_info.bvid,
            "cid": page_info.cid,
            "page_index": page_info.page_index,
            "access_token": "present" if token else "missing",
        },
    )
    artifact = download_best_candidate(
        extraction,
        output_dir=output_dir,
        timeout_secs=timeout_secs,
        watermark=watermark,
    )
    return extraction, artifact


def fetch_bilibili_page_info(source_url: str, timeout_secs: int, author_hint: str = "") -> BilibiliPageInfo:
    video_id = _extract_video_id(source_url)
    params = {"bvid": video_id} if video_id.upper().startswith("BV") else {"aid": video_id.removeprefix("av").removeprefix("AV")}
    response = requests.get(BILIBILI_VIEW_API, params=params, headers=TV_API_HEADERS, timeout=timeout_secs)
    response.raise_for_status()
    payload = response.json()
    if int(payload.get("code", 0)) != 0:
        message = str(payload.get("message") or payload.get("msg") or "unknown error")
        raise DownloadError(f"Failed to fetch Bilibili video info: {message}")
    data = payload.get("data") or {}
    pages = data.get("pages") or []
    selected_page = _select_bilibili_page(source_url, pages, str(data.get("cid") or ""))
    return BilibiliPageInfo(
        aid=str(data.get("aid") or ""),
        bvid=str(data.get("bvid") or video_id),
        cid=str(selected_page.get("cid") or data.get("cid") or ""),
        page_index=int(selected_page.get("page") or 1),
        title=str(data.get("title") or "").strip(),
        desc=str(data.get("desc") or "").strip(),
        author=str((data.get("owner") or {}).get("name") or author_hint).strip(),
    )


def build_bbdown_metadata(
    source_url: str,
    *,
    page_info: BilibiliPageInfo,
    metadata_seed: VideoMetadata | None = None,
    author_hint: str = "",
) -> VideoMetadata:
    metadata = VideoMetadata(
        source_url=source_url,
        site="bilibili",
        canonical_url=metadata_seed.canonical_url if metadata_seed and metadata_seed.canonical_url else source_url,
        page_url=metadata_seed.page_url if metadata_seed and metadata_seed.page_url else source_url,
        aweme_id=page_info.bvid or page_info.aid,
        author=page_info.author or (metadata_seed.author if metadata_seed else "") or author_hint,
        desc=page_info.desc or (metadata_seed.desc if metadata_seed else ""),
        title=page_info.title or (metadata_seed.title if metadata_seed else ""),
    )
    if author_hint and not metadata.author:
        metadata.author = author_hint
    if metadata_seed is not None:
        if metadata_seed.title and not metadata.title:
            metadata.title = metadata_seed.title
        if metadata_seed.desc and not metadata.desc:
            metadata.desc = metadata_seed.desc
    return metadata


def fetch_bilibili_tv_candidates(page_info: BilibiliPageInfo, token: str, timeout_secs: int) -> list[MediaCandidate]:
    play_payload = _fetch_bilibili_tv_playinfo(page_info, token=token, timeout_secs=timeout_secs)
    root = _resolve_playinfo_root(play_payload)
    dash = root.get("dash")
    if isinstance(dash, dict):
        videos = dash.get("video") or []
        audios = dash.get("audio") or []
        candidates: list[MediaCandidate] = []
        for stream in sorted(videos, key=_video_stream_sort_key, reverse=True):
            primary_url = _pick_primary_url(stream)
            if not primary_url:
                continue
            quality_id = str(stream.get("id") or "")
            bandwidth = int(stream.get("bandwidth") or 0)
            codecs = str(stream.get("codecs") or "").strip()
            codecid = int(stream.get("codecid") or 0)
            candidates.append(
                MediaCandidate(
                    url=primary_url,
                    kind=MediaKind.MP4,
                    track_type=TrackType.VIDEO_ONLY,
                    watermark_mode=WatermarkMode.NO_WATERMARK,
                    source="tv_api",
                    observed_via="api",
                    note=f"qn={quality_id};codecid={codecid};codecs={codecs};bandwidth={bandwidth}",
                )
            )
        best_audio = _pick_best_audio_stream(audios)
        if best_audio is not None:
            audio_url = _pick_primary_url(best_audio)
            if audio_url:
                candidates.append(
                    MediaCandidate(
                        url=audio_url,
                        kind=MediaKind.MP4,
                        track_type=TrackType.AUDIO_ONLY,
                        watermark_mode=WatermarkMode.NO_WATERMARK,
                        source="tv_api",
                        observed_via="api",
                        note=f"id={best_audio.get('id')};bandwidth={best_audio.get('bandwidth')}",
                    )
                )
        return candidates

    durl = root.get("durl") or []
    if isinstance(durl, list) and durl:
        if len(durl) > 1:
            raise DownloadError("Bilibili TV API returned multi-clip FLV data, which is not supported yet.")
        url = str((durl[0] or {}).get("url") or "").strip()
        if url:
            return [
                MediaCandidate(
                    url=url,
                    kind=MediaKind.MP4,
                    track_type=TrackType.MUXED,
                    watermark_mode=WatermarkMode.NO_WATERMARK,
                    source="tv_api",
                    observed_via="api",
                    note="durl",
                )
            ]
    return []


def _fetch_bilibili_tv_playinfo(page_info: BilibiliPageInfo, token: str, timeout_secs: int) -> dict:
    params: dict[str, str] = {}
    if token:
        params["access_key"] = token
    params["appkey"] = "4409e2ce8ffd12b8"
    params["build"] = "106500"
    params["cid"] = page_info.cid
    params["device"] = "android"
    params["fnval"] = "4048"
    params["fnver"] = "0"
    params["fourk"] = "1"
    params["mid"] = "0"
    params["mobi_app"] = "android_tv_yst"
    params["object_id"] = page_info.aid
    params["platform"] = "android"
    params["playurl_type"] = "1"
    params["qn"] = "0"
    params["ts"] = _timestamp_secs()
    unsigned_query = urlencode(params)
    url = f"{TV_PLAYURL_API}?{unsigned_query}&sign={_sign_query(unsigned_query)}"
    log_info(
        "bbdown.tv_api.request",
        endpoint=TV_PLAYURL_API,
        aid=page_info.aid,
        cid=page_info.cid,
        page=page_info.page_index,
        access_token="present" if token else "missing",
    )
    response = requests.get(url, headers=TV_API_HEADERS, timeout=timeout_secs)
    response.raise_for_status()
    payload = response.json()
    if int(payload.get("code", 0)) != 0:
        message = str(payload.get("message") or payload.get("msg") or "unknown error")
        raise DownloadError(f"Bilibili TV playurl request failed: {message}")
    return payload


def _resolve_playinfo_root(payload: dict) -> dict:
    root = payload.get("result") or payload.get("data") or payload
    if isinstance(root, dict) and isinstance(root.get("video_info"), dict):
        return root["video_info"]
    return root if isinstance(root, dict) else {}


def _video_stream_sort_key(stream: dict) -> tuple[int, int]:
    quality_id = str(stream.get("id") or "")
    quality_rank = QUALITY_PRIORITY.get(quality_id, int(quality_id or 0))
    codec_rank = _codec_compat_rank(stream)
    bandwidth = int(stream.get("bandwidth") or 0)
    return (quality_rank, codec_rank, bandwidth)


def _codec_compat_rank(stream: dict) -> int:
    codecid = int(stream.get("codecid") or 0)
    if codecid in CODEC_COMPAT_PRIORITY:
        return CODEC_COMPAT_PRIORITY[codecid]
    codecs = str(stream.get("codecs") or "").lower()
    if codecs.startswith("avc1"):
        return CODEC_COMPAT_PRIORITY[7]
    if codecs.startswith("hev1") or codecs.startswith("hvc1"):
        return CODEC_COMPAT_PRIORITY[12]
    if codecs.startswith("av01"):
        return CODEC_COMPAT_PRIORITY[13]
    return 0


def _pick_best_audio_stream(streams: list[dict]) -> dict | None:
    if not isinstance(streams, list) or not streams:
        return None
    return max(streams, key=lambda stream: (int(stream.get("bandwidth") or 0), int(stream.get("id") or 0)))


def _pick_primary_url(stream: dict) -> str:
    urls: list[str] = []
    for key in ("base_url", "baseUrl"):
        value = str(stream.get(key) or "").strip()
        if value:
            urls.append(value)
    backup = stream.get("backup_url") or stream.get("backupUrl") or []
    if isinstance(backup, list):
        urls.extend(str(item).strip() for item in backup if str(item).strip())
    if not urls:
        return ""
    preferred = next((item for item in urls if not BASE_URL_PORT_RE.match(item)), "")
    return preferred or urls[0]


def _select_bilibili_page(source_url: str, pages: list[dict], default_cid: str) -> dict:
    page_param = infer_bbdown_select_page(source_url)
    try:
        page_index = int(page_param)
    except ValueError:
        page_index = 1
    if isinstance(pages, list):
        for page in pages:
            if int(page.get("page") or 0) == page_index:
                return page
        for page in pages:
            if str(page.get("cid") or "") == default_cid:
                return page
        if pages:
            return pages[0]
    return {"page": page_index, "cid": default_cid}


def _extract_video_id(url: str) -> str:
    bvid = extract_id_from_url(url, (BILIBILI_VIDEO_ID_RE,))
    if bvid:
        return bvid
    aid_match = re.search(r"/video/av(\d+)", url, re.IGNORECASE)
    if aid_match:
        return aid_match.group(1)
    raise DownloadError(f"Unsupported Bilibili video URL: {url}")


def _login_tv_in_browser(browser_path: str, timeout_secs: int) -> str:
    payload = _build_tv_login_payload()
    response = requests.post(TV_LOGIN_AUTH_URL, data=payload, headers=TV_API_HEADERS, timeout=15)
    response.raise_for_status()
    data = _parse_tv_login_response(response.text)
    login_url = data["url"]
    auth_code = data["auth_code"]
    payload["auth_code"] = auth_code
    payload["ts"] = _timestamp_secs()
    payload.pop("sign", None)
    payload["sign"] = _sign_query(urlencode(payload))

    with tempfile.TemporaryDirectory(prefix="videocp-bbdown-login-browser-") as temp_profile_raw:
        login_config = BrowserConfig(
            profile_dir=Path(temp_profile_raw),
            browser_path=browser_path,
            cdp_url=build_cdp_url(find_free_local_port()),
            headless=False,
        )
        with BrowserSession(login_config, prepare_seed=False, terminate_on_close=True) as browser:
            page = browser.new_page()
            page.set_viewport_size({"width": 760, "height": 920})
            _render_login_page(page, login_url)
            last_status = ""
            deadline = time.monotonic() + timeout_secs
            while time.monotonic() < deadline:
                time.sleep(TV_LOGIN_POLL_INTERVAL_SECS)
                poll_response = requests.post(TV_LOGIN_POLL_URL, data=payload, headers=TV_API_HEADERS, timeout=15)
                poll_response.raise_for_status()
                poll_payload = poll_response.json()
                code = str(poll_payload.get("code", ""))
                if code == "86039":
                    if last_status != code:
                        _set_login_status(page, "等待扫码", "请使用哔哩哔哩 App 扫描二维码。")
                        last_status = code
                    continue
                if code == "86038":
                    _set_login_status(page, "二维码已过期", "请重新发起登录。")
                    raise DownloadError("Bilibili TV login QR code expired. Retry the download to generate a new code.")
                access_token = str(poll_payload.get("data", {}).get("access_token", "")).strip()
                if access_token:
                    _set_login_status(page, "扫码成功", "已获取 TV access_token，正在继续下载。")
                    page.wait_for_timeout(1500)
                    return access_token
                message = str(poll_payload.get("message") or poll_payload.get("msg") or "unknown login status")
                log_warn("bbdown.login.poll.unexpected", code=code or "none", message=message)
            _set_login_status(page, "登录超时", "等待扫码超时，请重试。")
            raise DownloadError("Timed out waiting for Bilibili TV QR-code login.")


def _build_tv_login_payload() -> dict[str, str]:
    now = datetime.now()
    device_id = _random_string(20)
    buvid = _random_string(37)
    millis = int(now.microsecond / 1000)
    fingerprint = f"{now:%Y%m%d%H%M%S}{millis:03d}{_random_string(45)}"
    payload = {
        "appkey": "4409e2ce8ffd12b8",
        "auth_code": "",
        "bili_local_id": device_id,
        "build": "102801",
        "buvid": buvid,
        "channel": "master",
        "device": "OnePlus",
        "device_id": device_id,
        "device_name": "OnePlus7TPro",
        "device_platform": "Android10OnePlusHD1910",
        "fingerprint": fingerprint,
        "guid": buvid,
        "local_fingerprint": fingerprint,
        "local_id": buvid,
        "mobi_app": "android_tv_yst",
        "networkstate": "wifi",
        "platform": "android",
        "sys_ver": "29",
        "ts": _timestamp_secs(),
    }
    payload["sign"] = _sign_query(urlencode(payload))
    return payload


def _parse_tv_login_response(body: str) -> dict[str, str]:
    payload = json.loads(body)
    data = payload.get("data", {})
    login_url = str(data.get("url", "")).strip()
    auth_code = str(data.get("auth_code", "")).strip()
    if not login_url or not auth_code:
        message = str(payload.get("message") or payload.get("msg") or "missing tv login payload")
        raise DownloadError(f"Failed to create Bilibili TV login QR code: {message}")
    return {"url": login_url, "auth_code": auth_code}


def _sign_query(query: str) -> str:
    return hashlib.md5(f"{query}{TV_API_SIGN_SECRET}".encode("utf-8")).hexdigest()


def _timestamp_secs() -> str:
    return str(int(time.time()))


def _random_string(length: int) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_0123456789"
    return "".join(random.choice(alphabet) for _ in range(length))


def _render_login_page(page, login_url: str) -> None:
    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(login_url)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgImage)
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("utf-8")
    qr_data_url = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
    escaped_url = html.escape(login_url)
    page.set_content(
        f"""
<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>videocp Bilibili TV Login</title>
    <style>
      :root {{
        color-scheme: light;
        font-family: "SF Pro Display", "PingFang SC", "Helvetica Neue", sans-serif;
        background:
          radial-gradient(circle at top left, #ffe6b6, transparent 34%),
          radial-gradient(circle at top right, #c3efff, transparent 32%),
          linear-gradient(180deg, #f7f3e9, #fffaf2 42%, #f2f7ff 100%);
      }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        color: #1c2430;
      }}
      main {{
        width: min(92vw, 760px);
        padding: 32px;
        border-radius: 28px;
        background: rgba(255, 255, 255, 0.88);
        box-shadow: 0 24px 80px rgba(28, 36, 48, 0.12);
        backdrop-filter: blur(14px);
      }}
      h1 {{
        margin: 0 0 10px;
        font-size: 34px;
        line-height: 1.08;
      }}
      p {{
        margin: 0;
        color: #516174;
        line-height: 1.6;
      }}
      .layout {{
        margin-top: 28px;
        display: grid;
        grid-template-columns: minmax(260px, 320px) 1fr;
        gap: 28px;
        align-items: center;
      }}
      .qr {{
        padding: 18px;
        border-radius: 24px;
        background: linear-gradient(180deg, #ffffff, #f6f7fb);
        box-shadow: inset 0 0 0 1px rgba(28, 36, 48, 0.06);
      }}
      .qr img {{
        width: 100%;
        height: auto;
        display: block;
      }}
      .status {{
        margin-top: 18px;
        padding: 14px 16px;
        border-radius: 18px;
        background: #fff4d6;
        color: #8a5710;
        font-weight: 600;
      }}
      .hint {{
        margin-top: 10px;
        font-size: 14px;
      }}
      code {{
        display: block;
        margin-top: 16px;
        padding: 12px 14px;
        border-radius: 16px;
        background: #0f1720;
        color: #eef4ff;
        word-break: break-all;
        font-size: 12px;
      }}
      @media (max-width: 720px) {{
        main {{
          width: calc(100vw - 32px);
          padding: 20px;
        }}
        .layout {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
  </head>
  <body>
    <main>
      <p>videocp 正在为 B 站下载获取 TV token</p>
      <h1>请用哔哩哔哩 App 扫码</h1>
      <p>扫码并确认后，本窗口会自动继续下载。token 会缓存到本地，后续下载不需要重复扫码。</p>
      <div class="layout">
        <div class="qr"><img alt="Bilibili TV Login QR Code" src="{qr_data_url}" /></div>
        <div>
          <div id="status" class="status">等待扫码</div>
          <p id="hint" class="hint">如果二维码失效，重新发起下载即可生成新的二维码。</p>
          <code>{escaped_url}</code>
        </div>
      </div>
    </main>
  </body>
</html>
        """,
        wait_until="load",
    )


def _set_login_status(page, status: str, hint: str) -> None:
    try:
        page.evaluate(
            """([status, hint]) => {
                const statusNode = document.getElementById("status");
                const hintNode = document.getElementById("hint");
                if (statusNode) statusNode.textContent = status;
                if (hintNode) hintNode.textContent = hint;
            }""",
            [status, hint],
        )
    except Exception:
        pass

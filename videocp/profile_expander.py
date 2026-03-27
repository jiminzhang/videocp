from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.sync_api import Page, Response

from videocp.providers import DOUYIN_USER_PROFILE_RE, SiteProvider, resolve_provider
from videocp.runtime_log import full_url, log_info, log_warn

INSTAGRAM_REEL_URL_TEMPLATE = "https://www.instagram.com/reel/{shortcode}/"
INSTAGRAM_REEL_LINK_RE = re.compile(r'/reel/([A-Za-z0-9_-]+)')
INSTAGRAM_PROFILE_RE = re.compile(
    r"instagram\.com/(?!p/|reel/|stories/|explore/|accounts/|direct/)[^/?#]+(/reels)?/?$",
    re.IGNORECASE,
)
DOUYIN_VIDEO_URL_TEMPLATE = "https://www.douyin.com/video/{aweme_id}"
DOUYIN_VIDEO_LINK_RE = re.compile(r'/video/(\d+)')
BILIBILI_VIDEO_URL_TEMPLATE = "https://www.bilibili.com/video/{bvid}"
BILIBILI_BVID_RE = re.compile(r'/(BV[A-Za-z0-9]+)')
BILIBILI_SPACE_VIDEO_SUFFIX = "/video"
XHS_EXPLORE_URL_TEMPLATE = "https://www.xiaohongshu.com/explore/{note_id}"
XHS_NOTE_LINK_RE = re.compile(r'/(?:explore|discovery/item)/([A-Za-z0-9]+)')


def _extract_author_from_dom(page: Page, selectors: list[str]) -> str:
    """Try multiple CSS selectors to extract the profile author name from the page."""
    for selector in selectors:
        try:
            el = page.query_selector(selector)
            if el:
                text = (el.text_content() or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


@dataclass(slots=True)
class ProfileExpandResult:
    video_urls: list[str]
    pinned_urls: list[str]
    author: str


def expand_profile(
    page: Page,
    profile_url: str,
    max_videos: int,
    timeout_secs: int,
) -> ProfileExpandResult:
    """Expand a profile URL into individual video URLs using CDP browser.

    Dispatches to provider-specific expansion logic.
    Returns video URLs and the profile author name.
    """
    provider = resolve_provider(profile_url)
    expander = _PROFILE_EXPANDERS.get(provider.key)
    if expander is None:
        log_warn("profile.expand.unsupported", site=provider.key, url=full_url(profile_url))
        return ProfileExpandResult(video_urls=[], pinned_urls=[], author="")
    return expander(page, profile_url, max_videos, timeout_secs)


# Keep backward-compatible alias
def expand_profile_to_video_urls(
    page: Page,
    profile_url: str,
    max_videos: int,
    timeout_secs: int,
) -> list[str]:
    return expand_profile(page, profile_url, max_videos, timeout_secs).video_urls


def _expand_douyin_profile(
    page: Page,
    profile_url: str,
    max_videos: int,
    timeout_secs: int,
) -> ProfileExpandResult:
    """Extract recent video URLs from a Douyin user profile page.

    Strategy:
    1. Intercept XHR JSON responses containing aweme_list with aweme_id fields.
    2. Fallback: extract /video/ links from the DOM.
    3. Scroll to load more if needed.
    """
    collected_ids: list[str] = []
    pinned_ids: list[str] = []
    seen_ids: set[str] = set()

    def _collect_from_json(payload: object) -> None:
        if not isinstance(payload, dict):
            return
        aweme_list = payload.get("aweme_list")
        if not isinstance(aweme_list, list):
            for value in payload.values():
                if isinstance(value, (dict, list)):
                    _collect_from_json(value)
            return
        for item in aweme_list:
            if not isinstance(item, dict):
                continue
            aweme_id = item.get("aweme_id")
            if not isinstance(aweme_id, str) or not aweme_id:
                continue
            # Collect pinned/topped videos separately — they don't count against quota
            is_top = item.get("is_top") or item.get("tag", {}).get("is_top")
            if is_top and int(is_top) == 1:
                if aweme_id not in seen_ids:
                    seen_ids.add(aweme_id)
                    pinned_ids.append(aweme_id)
                    log_info("profile.expand.pinned", site="douyin", aweme_id=aweme_id)
                continue
            if aweme_id not in seen_ids:
                seen_ids.add(aweme_id)
                collected_ids.append(aweme_id)

    def on_response(response: Response) -> None:
        url = response.url.lower()
        content_type = response.headers.get("content-type", "").lower()
        if "application/json" not in content_type and "aweme" not in url:
            return
        try:
            body = response.json()
        except Exception:
            return
        _collect_from_json(body)

    page.on("response", on_response)
    log_info("profile.expand.start", site="douyin", url=full_url(profile_url), max_videos=max_videos)

    try:
        page.goto(profile_url, wait_until="domcontentloaded", timeout=timeout_secs * 1000)
    except Exception as exc:
        log_warn("profile.expand.goto_failed", site="douyin", url=full_url(profile_url), error=str(exc))
        return ProfileExpandResult(video_urls=[], pinned_urls=[], author="")

    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_secs * 1000, 8000))
    except Exception:
        pass

    page.wait_for_timeout(3000)

    # Scroll to load more videos if we don't have enough
    scroll_attempts = 0
    max_scroll_attempts = 5
    while len(collected_ids) < max_videos and scroll_attempts < max_scroll_attempts:
        prev_count = len(collected_ids)
        page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        page.wait_for_timeout(2000)
        if len(collected_ids) == prev_count:
            scroll_attempts += 1
        else:
            scroll_attempts = 0

    # Fallback: extract video links from the DOM if XHR interception yielded nothing
    if not collected_ids:
        log_info("profile.expand.fallback_dom", site="douyin")
        hrefs = page.eval_on_selector_all(
            'a[href*="/video/"]',
            "els => els.map(e => e.getAttribute('href'))",
        )
        for href in hrefs:
            if not isinstance(href, str):
                continue
            match = DOUYIN_VIDEO_LINK_RE.search(href)
            if match:
                aweme_id = match.group(1)
                if aweme_id not in seen_ids:
                    seen_ids.add(aweme_id)
                    collected_ids.append(aweme_id)

    pinned_urls = [
        DOUYIN_VIDEO_URL_TEMPLATE.format(aweme_id=aweme_id)
        for aweme_id in pinned_ids
    ]
    video_urls = [
        DOUYIN_VIDEO_URL_TEMPLATE.format(aweme_id=aweme_id)
        for aweme_id in collected_ids[:max_videos]
    ]
    author = _extract_author_from_dom(page, [
        '[data-e2e="user-info"] .name',
        '[data-e2e="user-name"]',
        '.user-info .nickname',
        'h1.name',
        'span.name',
    ])
    log_info(
        "profile.expand.complete",
        site="douyin",
        url=full_url(profile_url),
        author=author,
        found=len(collected_ids),
        pinned=len(pinned_ids),
        returned=len(video_urls),
    )
    return ProfileExpandResult(video_urls=video_urls, pinned_urls=pinned_urls, author=author)


def _expand_bilibili_profile(
    page: Page,
    profile_url: str,
    max_videos: int,
    timeout_secs: int,
) -> ProfileExpandResult:
    """Extract recent video URLs from a Bilibili space page.

    Strategy:
    1. Navigate to space.bilibili.com/{uid}/video for chronological order.
    2. Intercept XHR JSON responses from arc/search API containing vlist/archives.
    3. Fallback: extract /video/BV... links from DOM.
    4. Scroll to load more if needed.
    """
    collected_bvids: list[str] = []
    seen_bvids: set[str] = set()

    def _collect_from_json(payload: object) -> None:
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            # Recurse into nested dicts
            for value in payload.values():
                if isinstance(value, dict):
                    _collect_from_json(value)
            return
        # arc/search API: data.list.vlist
        vlist_container = data.get("list")
        if isinstance(vlist_container, dict):
            vlist = vlist_container.get("vlist")
            if isinstance(vlist, list):
                for item in vlist:
                    if not isinstance(item, dict):
                        continue
                    bvid = item.get("bvid")
                    if isinstance(bvid, str) and bvid and bvid not in seen_bvids:
                        seen_bvids.add(bvid)
                        collected_bvids.append(bvid)
        # Newer API variant: data.archives
        archives = data.get("archives")
        if isinstance(archives, list):
            for item in archives:
                if not isinstance(item, dict):
                    continue
                bvid = item.get("bvid")
                if isinstance(bvid, str) and bvid and bvid not in seen_bvids:
                    seen_bvids.add(bvid)
                    collected_bvids.append(bvid)

    def on_response(response: Response) -> None:
        url = response.url.lower()
        content_type = response.headers.get("content-type", "").lower()
        if "application/json" not in content_type:
            return
        if not any(hint in url for hint in ("arc/search", "space", "wbi")):
            return
        try:
            body = response.json()
        except Exception:
            return
        _collect_from_json(body)

    page.on("response", on_response)

    # Navigate to /video tab for chronological listing
    video_tab_url = profile_url.rstrip("/")
    if not video_tab_url.endswith("/video"):
        video_tab_url += BILIBILI_SPACE_VIDEO_SUFFIX
    log_info("profile.expand.start", site="bilibili", url=full_url(video_tab_url), max_videos=max_videos)

    try:
        page.goto(video_tab_url, wait_until="domcontentloaded", timeout=timeout_secs * 1000)
    except Exception as exc:
        log_warn("profile.expand.goto_failed", site="bilibili", url=full_url(video_tab_url), error=str(exc))
        return ProfileExpandResult(video_urls=[], pinned_urls=[], author="")

    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_secs * 1000, 8000))
    except Exception:
        pass

    page.wait_for_timeout(3000)

    # Scroll to load more videos if we don't have enough
    scroll_attempts = 0
    max_scroll_attempts = 5
    while len(collected_bvids) < max_videos and scroll_attempts < max_scroll_attempts:
        prev_count = len(collected_bvids)
        page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        page.wait_for_timeout(2000)
        if len(collected_bvids) == prev_count:
            scroll_attempts += 1
        else:
            scroll_attempts = 0

    # Fallback: extract video links from the DOM
    if not collected_bvids:
        log_info("profile.expand.fallback_dom", site="bilibili")
        hrefs = page.eval_on_selector_all(
            'a[href*="/video/BV"]',
            "els => els.map(e => e.getAttribute('href'))",
        )
        for href in hrefs:
            if not isinstance(href, str):
                continue
            match = BILIBILI_BVID_RE.search(href)
            if match:
                bvid = match.group(1)
                if bvid not in seen_bvids:
                    seen_bvids.add(bvid)
                    collected_bvids.append(bvid)

    video_urls = [
        BILIBILI_VIDEO_URL_TEMPLATE.format(bvid=bvid)
        for bvid in collected_bvids[:max_videos]
    ]
    author = _extract_author_from_dom(page, [
        '#h-name',
        '.h-name',
        '.nickname',
        'span.name',
    ])
    log_info(
        "profile.expand.complete",
        site="bilibili",
        url=full_url(profile_url),
        author=author,
        found=len(collected_bvids),
        returned=len(video_urls),
    )
    return ProfileExpandResult(video_urls=video_urls, pinned_urls=[], author=author)


def _extract_xhs_video_note_ids_from_dom(page: Page) -> list[str]:
    """Extract video note IDs from XHS profile DOM.

    Each note card is a <section class="note-item"> containing:
    - A hidden <a href="/explore/{noteId}"> link
    - A <span class="play-icon"> if the note is a video
    Returns note IDs for video notes only, in DOM order.
    """
    result = page.evaluate("""() => {
        var items = document.querySelectorAll("section.note-item");
        var ids = [];
        for (var i = 0; i < items.length; i++) {
            var el = items[i];
            if (!el.querySelector(".play-icon")) continue;
            var links = el.querySelectorAll("a[href*='/explore/']");
            for (var j = 0; j < links.length; j++) {
                var href = links[j].getAttribute("href") || "";
                var m = href.match(/\\/explore\\/([A-Za-z0-9]+)/);
                if (m && m[1]) { ids.push(m[1]); break; }
            }
        }
        return ids;
    }""")
    if not isinstance(result, list):
        return []
    return [nid for nid in result if isinstance(nid, str) and nid]


def _expand_xiaohongshu_profile(
    page: Page,
    profile_url: str,
    max_videos: int,
    timeout_secs: int,
) -> ProfileExpandResult:
    """Extract recent video note URLs from a Xiaohongshu user profile page.

    Strategy:
    1. Navigate to profile, wait for hydration.
    2. Extract video note IDs from DOM (hidden /explore/{noteId} links).
    3. Scroll to load more if needed.
    """
    log_info("profile.expand.start", site="xiaohongshu", url=full_url(profile_url), max_videos=max_videos)

    try:
        page.goto(profile_url, wait_until="domcontentloaded", timeout=timeout_secs * 1000)
    except Exception as exc:
        log_warn("profile.expand.goto_failed", site="xiaohongshu", url=full_url(profile_url), error=str(exc))
        return ProfileExpandResult(video_urls=[], pinned_urls=[], author="")

    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_secs * 1000, 8000))
    except Exception:
        pass

    page.wait_for_timeout(3000)

    # Extract video note IDs from DOM
    seen_note_ids: set[str] = set()
    collected_note_ids: list[str] = []

    def _collect_from_dom() -> None:
        for nid in _extract_xhs_video_note_ids_from_dom(page):
            if nid not in seen_note_ids:
                seen_note_ids.add(nid)
                collected_note_ids.append(nid)

    _collect_from_dom()
    log_info("profile.expand.dom", site="xiaohongshu", found=len(collected_note_ids))

    # Scroll to load more if needed
    scroll_attempts = 0
    max_scroll_attempts = 5
    while len(collected_note_ids) < max_videos and scroll_attempts < max_scroll_attempts:
        prev_count = len(collected_note_ids)
        page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        page.wait_for_timeout(2000)
        _collect_from_dom()
        if len(collected_note_ids) == prev_count:
            scroll_attempts += 1
        else:
            scroll_attempts = 0

    author = _extract_author_from_dom(page, [
        ".user-name",
        ".user-nickname",
        ".info .name",
        "span.name",
    ])

    video_urls = [
        XHS_EXPLORE_URL_TEMPLATE.format(note_id=note_id)
        for note_id in collected_note_ids[:max_videos]
    ]
    log_info(
        "profile.expand.complete",
        site="xiaohongshu",
        url=full_url(profile_url),
        author=author,
        found=len(collected_note_ids),
        returned=len(video_urls),
    )
    return ProfileExpandResult(video_urls=video_urls, pinned_urls=[], author=author)


def _expand_instagram_reels(
    page: Page,
    profile_url: str,
    max_videos: int,
    timeout_secs: int,
) -> ProfileExpandResult:
    """Extract recent reel URLs from an Instagram profile/reels page.

    Strategy:
    1. Navigate to profile reels tab, wait for hydration.
    2. Extract /reel/{shortcode} links from the DOM.
    3. Scroll to load more if needed.
    """
    # Ensure we land on the reels tab
    reels_url = profile_url.rstrip("/")
    if not reels_url.endswith("/reels"):
        reels_url += "/reels"
    reels_url += "/"

    log_info("profile.expand.start", site="instagram", url=full_url(reels_url), max_videos=max_videos)

    try:
        page.goto(reels_url, wait_until="domcontentloaded", timeout=timeout_secs * 1000)
    except Exception as exc:
        log_warn("profile.expand.goto_failed", site="instagram", url=full_url(reels_url), error=str(exc))
        return ProfileExpandResult(video_urls=[], pinned_urls=[], author="")

    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_secs * 1000, 10000))
    except Exception:
        pass

    page.wait_for_timeout(3000)

    seen_codes: set[str] = set()
    collected_codes: list[str] = []

    def _collect_from_dom() -> None:
        hrefs = page.eval_on_selector_all(
            'a[href*="/reel/"]',
            "els => els.map(e => e.getAttribute('href'))",
        )
        for href in hrefs:
            if not isinstance(href, str):
                continue
            match = INSTAGRAM_REEL_LINK_RE.search(href)
            if match:
                code = match.group(1)
                if code not in seen_codes:
                    seen_codes.add(code)
                    collected_codes.append(code)

    _collect_from_dom()
    log_info("profile.expand.dom", site="instagram", found=len(collected_codes))

    # Scroll to load more if needed
    scroll_attempts = 0
    max_scroll_attempts = 5
    while len(collected_codes) < max_videos and scroll_attempts < max_scroll_attempts:
        prev_count = len(collected_codes)
        page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        page.wait_for_timeout(2000)
        _collect_from_dom()
        if len(collected_codes) == prev_count:
            scroll_attempts += 1
        else:
            scroll_attempts = 0

    author = _extract_author_from_dom(page, [
        'header h2',
        'header span',
        'title',
    ])
    # Fallback: try to extract username from URL
    if not author:
        from urllib.parse import urlparse
        path_parts = urlparse(profile_url).path.strip("/").split("/")
        if path_parts:
            author = path_parts[0]

    video_urls = [
        INSTAGRAM_REEL_URL_TEMPLATE.format(shortcode=code)
        for code in collected_codes[:max_videos]
    ]
    log_info(
        "profile.expand.complete",
        site="instagram",
        url=full_url(profile_url),
        author=author,
        found=len(collected_codes),
        returned=len(video_urls),
    )
    return ProfileExpandResult(video_urls=video_urls, pinned_urls=[], author=author)


# Provider-keyed dispatch table.
_PROFILE_EXPANDERS: dict[str, type[None] | callable] = {
    "douyin": _expand_douyin_profile,
    "bilibili": _expand_bilibili_profile,
    "xiaohongshu": _expand_xiaohongshu_profile,
}

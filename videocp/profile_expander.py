from __future__ import annotations

import json
import re
from dataclasses import dataclass

from playwright.sync_api import Page, Response

from videocp.providers import DOUYIN_USER_PROFILE_RE, SiteProvider, resolve_provider
from videocp.runtime_log import full_url, log_info, log_warn

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
        return ProfileExpandResult(video_urls=[], author="")
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
    pinned_ids: set[str] = set()
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
            # Skip pinned/topped videos — they are not necessarily recent
            is_top = item.get("is_top") or item.get("tag", {}).get("is_top")
            if is_top and int(is_top) == 1:
                pinned_ids.add(aweme_id)
                log_info("profile.expand.skip_pinned", site="douyin", aweme_id=aweme_id)
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
        return []

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
                if aweme_id not in seen_ids and aweme_id not in pinned_ids:
                    seen_ids.add(aweme_id)
                    collected_ids.append(aweme_id)

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
        pinned_skipped=len(pinned_ids),
        returned=len(video_urls),
    )
    return ProfileExpandResult(video_urls=video_urls, author=author)


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
        return []

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
    return ProfileExpandResult(video_urls=video_urls, author=author)


def _expand_xiaohongshu_profile(
    page: Page,
    profile_url: str,
    max_videos: int,
    timeout_secs: int,
) -> ProfileExpandResult:
    """Extract recent video note URLs from a Xiaohongshu user profile page.

    Strategy:
    1. Intercept XHR JSON responses from user_posted API.
    2. Filter for video notes only (skip image notes).
    3. Fallback: extract explore/ links from DOM that have a video indicator.
    4. Scroll to load more if needed.
    """
    collected_note_ids: list[str] = []
    seen_note_ids: set[str] = set()

    def _collect_from_json(payload: object) -> None:
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            for value in payload.values():
                if isinstance(value, dict):
                    _collect_from_json(value)
            return
        notes = data.get("notes")
        if not isinstance(notes, list):
            return
        for item in notes:
            if not isinstance(item, dict):
                continue
            note_id = item.get("note_id") or item.get("id")
            if not isinstance(note_id, str) or not note_id:
                continue
            # Only collect video notes
            note_type = item.get("type")
            if note_type != "video":
                # Also check note_card for nested type
                note_card = item.get("note_card")
                if isinstance(note_card, dict):
                    note_type = note_card.get("type")
                if note_type != "video":
                    continue
            if note_id not in seen_note_ids:
                seen_note_ids.add(note_id)
                collected_note_ids.append(note_id)

    def on_response(response: Response) -> None:
        url = response.url.lower()
        content_type = response.headers.get("content-type", "").lower()
        if "application/json" not in content_type:
            return
        if "user_posted" not in url and "user/posted" not in url:
            return
        try:
            body = response.json()
        except Exception:
            return
        _collect_from_json(body)

    page.on("response", on_response)
    log_info("profile.expand.start", site="xiaohongshu", url=full_url(profile_url), max_videos=max_videos)

    try:
        page.goto(profile_url, wait_until="domcontentloaded", timeout=timeout_secs * 1000)
    except Exception as exc:
        log_warn("profile.expand.goto_failed", site="xiaohongshu", url=full_url(profile_url), error=str(exc))
        return ProfileExpandResult(video_urls=[], author="")

    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_secs * 1000, 8000))
    except Exception:
        pass

    page.wait_for_timeout(3000)

    # Scroll to load more notes if we don't have enough
    scroll_attempts = 0
    max_scroll_attempts = 5
    while len(collected_note_ids) < max_videos and scroll_attempts < max_scroll_attempts:
        prev_count = len(collected_note_ids)
        page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        page.wait_for_timeout(2000)
        if len(collected_note_ids) == prev_count:
            scroll_attempts += 1
        else:
            scroll_attempts = 0

    # Fallback: extract video note links from the DOM
    if not collected_note_ids:
        log_info("profile.expand.fallback_dom", site="xiaohongshu")
        # XHS profile pages render note cards; video notes have a play icon overlay
        note_ids_from_dom = page.evaluate("""() => {
            const cards = document.querySelectorAll('section.note-item a[href*="/explore/"]');
            const ids = [];
            for (const card of cards) {
                // Check for video indicator (play icon SVG or video tag class)
                const hasVideo = card.querySelector('.play-icon, svg.play, [class*="video"], [class*="play"]');
                if (!hasVideo) continue;
                const href = card.getAttribute('href') || '';
                const match = href.match(/\\/explore\\/([A-Za-z0-9]+)/);
                if (match) ids.push(match[1]);
            }
            return ids;
        }""")
        if not note_ids_from_dom:
            # Broader fallback: all explore links
            note_ids_from_dom = page.evaluate("""() => {
                const links = document.querySelectorAll('a[href*="/explore/"]');
                const ids = [];
                for (const link of links) {
                    const href = link.getAttribute('href') || '';
                    const match = href.match(/\\/explore\\/([A-Za-z0-9]+)/);
                    if (match && !ids.includes(match[1])) ids.push(match[1]);
                }
                return ids;
            }""")
        for note_id in (note_ids_from_dom or []):
            if isinstance(note_id, str) and note_id not in seen_note_ids:
                seen_note_ids.add(note_id)
                collected_note_ids.append(note_id)

    video_urls = [
        XHS_EXPLORE_URL_TEMPLATE.format(note_id=note_id)
        for note_id in collected_note_ids[:max_videos]
    ]
    author = _extract_author_from_dom(page, [
        '.user-name',
        '.user-nickname',
        '.name-detail .name',
        'div.info .name',
    ])
    log_info(
        "profile.expand.complete",
        site="xiaohongshu",
        url=full_url(profile_url),
        author=author,
        found=len(collected_note_ids),
        returned=len(video_urls),
    )
    return ProfileExpandResult(video_urls=video_urls, author=author)


# Provider-keyed dispatch table.
_PROFILE_EXPANDERS: dict[str, type[None] | callable] = {
    "douyin": _expand_douyin_profile,
    "bilibili": _expand_bilibili_profile,
    "xiaohongshu": _expand_xiaohongshu_profile,
}

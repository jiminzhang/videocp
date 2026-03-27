from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from videocp.browser import BrowserConfig, open_download_browser_session
from videocp.publisher import PublishResult
from videocp.runtime_log import log_info, log_warn

STUDIO_URL = "https://studio.youtube.com"
UPLOAD_URL = "https://www.youtube.com/upload"
UPLOAD_POLL_MS = 1500
POST_SUCCESS_PAUSE_MS = 3000


def youtube_publish(
    browser_config: BrowserConfig,
    video_path: Path,
    title: str,
    description: str = "",
    visibility: str = "PUBLIC",
    timeout_secs: int = 600,
) -> PublishResult:
    log_info("yt_publish.start", video=str(video_path), title=title[:50])

    with open_download_browser_session(browser_config) as session:
        page = session.new_page()
        try:
            return _do_publish(page, video_path, title, description, visibility, timeout_secs)
        except Exception as exc:
            log_warn("yt_publish.error", error=str(exc))
            return PublishResult(success=False, error=str(exc))
        finally:
            page.close()


def _do_publish(
    page,
    video_path: Path,
    title: str,
    description: str,
    visibility: str,
    timeout_secs: int,
) -> PublishResult:
    timeout_ms = timeout_secs * 1000

    # Step 1: Navigate to YouTube Studio upload page
    log_info("yt_publish.navigate")
    page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)

    # Step 2: Set video file via the file input
    log_info("yt_publish.set_file", path=str(video_path))
    file_input = _find_file_input(page)
    file_input.set_input_files(str(video_path.resolve()))

    # Step 3: Wait for upload dialog to show details form
    log_info("yt_publish.wait_details_form")
    _wait_for_details_form(page, timeout_ms)

    # Step 4: Fill in title and description
    log_info("yt_publish.fill_details", title=title[:50])
    _fill_title(page, title)
    if description:
        _fill_description(page, description)
    page.wait_for_timeout(500)

    # Step 5: Set "Not made for kids"
    _set_not_made_for_kids(page)
    page.wait_for_timeout(500)

    # Step 6: Click Next through the wizard steps (Details → Video elements → Checks → Visibility)
    for step_name in ("Video elements", "Checks", "Visibility"):
        log_info("yt_publish.next_step", step=step_name)
        _click_next_button(page)
        page.wait_for_timeout(1000)

    # Step 7: Set visibility
    log_info("yt_publish.set_visibility", visibility=visibility)
    _set_visibility(page, visibility)
    page.wait_for_timeout(500)

    # Step 8: Wait for upload to complete before publishing
    log_info("yt_publish.wait_upload_complete")
    _wait_for_upload_complete(page, timeout_ms)

    # Step 9: Click Publish/Done
    log_info("yt_publish.publish")
    _click_done_button(page)

    # Step 10: Wait for publish confirmation and extract video URL
    log_info("yt_publish.wait_confirmation")
    result = _wait_for_publish_confirmation(page, timeout_ms)
    if result.success:
        log_info("yt_publish.success", share_url=result.share_url)
        page.wait_for_timeout(POST_SUCCESS_PAUSE_MS)
    return result


# ── File input ──────────────────────────────────────────────────────


def _find_file_input(page) -> Any:
    """Find the file input element in the upload dialog."""
    for selector in (
        "input[type='file'][accept*='video']",
        "input[type='file']",
        "#content input[type='file']",
    ):
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                locator.first.wait_for(state="attached", timeout=10000)
                return locator.first
        except Exception:
            continue
    raise RuntimeError("File input not found on YouTube Studio upload page")


# ── Wait for details form ───────────────────────────────────────────


def _wait_for_details_form(page, timeout_ms: int) -> None:
    """Wait until the upload details form appears (title field visible)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        found = page.evaluate("""() => {
            // Check for title textarea in YouTube Studio upload dialog
            const titleArea = document.querySelector('#title-textarea')
                || document.querySelector('ytcp-social-suggestions-textbox#title-textarea')
                || document.querySelector('[id="title-textarea"]');
            if (titleArea) return true;
            // Also check for any textbox inside title area
            const textbox = document.querySelector('#title-textarea #textbox');
            return !!textbox;
        }""")
        if found:
            page.wait_for_timeout(500)
            return
        page.wait_for_timeout(UPLOAD_POLL_MS)

    raise TimeoutError("Upload details form did not appear within timeout")


# ── Fill title ──────────────────────────────────────────────────────


def _fill_title(page, title: str) -> None:
    """Clear the default title and fill with the given title."""
    # YouTube Studio auto-fills the title with the filename.
    # We need to clear it first, then type our title.
    page.evaluate("""(title) => {
        // Find the title textbox (contenteditable div inside #title-textarea)
        const textbox = document.querySelector('#title-textarea #textbox')
            || document.querySelector('#title-textarea [contenteditable]')
            || document.querySelector('ytcp-social-suggestions-textbox#title-textarea #textbox');
        if (!textbox) throw new Error('Title textbox not found');
        // Clear existing content
        textbox.focus();
        textbox.textContent = '';
        // Set new title
        textbox.textContent = title;
        // Trigger input event so YouTube detects the change
        textbox.dispatchEvent(new Event('input', { bubbles: true }));
        textbox.dispatchEvent(new Event('change', { bubbles: true }));
    }""", title)


# ── Fill description ────────────────────────────────────────────────


def _fill_description(page, description: str) -> None:
    """Fill the description field."""
    page.evaluate("""(description) => {
        const textbox = document.querySelector('#description-textarea #textbox')
            || document.querySelector('#description-textarea [contenteditable]')
            || document.querySelector('ytcp-social-suggestions-textbox#description-textarea #textbox');
        if (!textbox) {
            // Description field is optional - don't fail if not found
            console.warn('Description textbox not found');
            return;
        }
        textbox.focus();
        textbox.textContent = '';
        textbox.textContent = description;
        textbox.dispatchEvent(new Event('input', { bubbles: true }));
        textbox.dispatchEvent(new Event('change', { bubbles: true }));
    }""", description)


# ── Not made for kids ───────────────────────────────────────────────


def _set_not_made_for_kids(page) -> None:
    """Select 'No, it's not made for kids' radio button."""
    page.evaluate("""() => {
        // Try multiple approaches to find and click the "not made for kids" radio
        // Approach 1: By name attribute
        const radio = document.querySelector('tp-yt-paper-radio-button[name="NOT_MADE_FOR_KIDS"]')
            || document.querySelector('#radioLabel:not([for="MADE_FOR_KIDS"])');
        if (radio) {
            radio.click();
            return;
        }
        // Approach 2: Find by text content
        const radios = document.querySelectorAll('tp-yt-paper-radio-button, ytcp-ve');
        for (const r of radios) {
            const text = (r.textContent || '').toLowerCase();
            if (text.includes('not made for kids') || text.includes('不是面向儿童')) {
                r.click();
                return;
            }
        }
        // Approach 3: Second radio in audience section (first = made for kids, second = not)
        const audienceRadios = document.querySelectorAll('#audience tp-yt-paper-radio-button');
        if (audienceRadios.length >= 2) {
            audienceRadios[1].click();
            return;
        }
    }""")


# ── Wizard navigation ──────────────────────────────────────────────


def _click_next_button(page) -> None:
    """Click the Next button in the upload wizard."""
    for selector in (
        "#next-button",
        "ytcp-button#next-button",
        "#next-button button",
    ):
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                locator.first.click(timeout=5000)
                return
        except Exception:
            continue
    # Fallback: click via JS
    page.evaluate("""() => {
        const btn = document.querySelector('#next-button');
        if (btn) { btn.click(); return; }
        const ycBtn = document.querySelector('ytcp-button#next-button');
        if (ycBtn) { ycBtn.click(); }
    }""")


def _click_done_button(page) -> None:
    """Click the Done/Publish button."""
    for selector in (
        "#done-button",
        "ytcp-button#done-button",
        "#done-button button",
    ):
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                locator.first.click(timeout=5000)
                return
        except Exception:
            continue
    page.evaluate("""() => {
        const btn = document.querySelector('#done-button');
        if (btn) { btn.click(); return; }
        const ycBtn = document.querySelector('ytcp-button#done-button');
        if (ycBtn) { ycBtn.click(); }
    }""")


# ── Visibility ──────────────────────────────────────────────────────


def _set_visibility(page, visibility: str) -> None:
    """Set the video visibility (PUBLIC, UNLISTED, PRIVATE)."""
    page.evaluate("""(visibility) => {
        // Try by name attribute on radio button
        const radio = document.querySelector(`tp-yt-paper-radio-button[name="${visibility}"]`);
        if (radio) {
            radio.click();
            return;
        }
        // Fallback: find by text content
        const radios = document.querySelectorAll('tp-yt-paper-radio-button');
        const labels = { 'PUBLIC': ['public', '公开'], 'UNLISTED': ['unlisted', '不公开'], 'PRIVATE': ['private', '私享'] };
        const targets = labels[visibility] || [visibility.toLowerCase()];
        for (const r of radios) {
            const text = (r.textContent || '').toLowerCase();
            if (targets.some(t => text.includes(t))) {
                r.click();
                return;
            }
        }
    }""", visibility)


# ── Upload progress ─────────────────────────────────────────────────


def _wait_for_upload_complete(page, timeout_ms: int) -> None:
    """Wait until YouTube finishes processing the upload."""
    deadline = time.monotonic() + timeout_ms / 1000
    last_progress = ""

    while time.monotonic() < deadline:
        state = _read_upload_progress(page)

        if state["error"]:
            raise RuntimeError(f"Upload error: {state['error']}")

        if state["complete"]:
            log_info("yt_publish.upload_complete")
            return

        progress_sig = f"{state['progress_pct']}|{state['status_text']}"
        if progress_sig != last_progress:
            log_info(
                "yt_publish.uploading",
                progress=state["progress_pct"],
                status=state["status_text"][:100],
            )
            last_progress = progress_sig

        page.wait_for_timeout(UPLOAD_POLL_MS)

    raise TimeoutError("Video upload did not complete within timeout")


def _read_upload_progress(page) -> dict[str, Any]:
    """Read the current upload progress state from the dialog."""
    return page.evaluate("""() => {
        const normalize = (v) => (v || '').replace(/\\s+/g, ' ').trim();

        // Scope all checks to the upload dialog only
        const dialog = document.querySelector('ytcp-uploads-dialog');

        // Check for error messages ONLY inside the upload dialog
        let errorText = '';
        if (dialog) {
            const errorEl = dialog.querySelector('.error-short, .error-message, #error-message, .upload-error');
            const candidate = errorEl ? normalize(errorEl.textContent) : '';
            // Only treat as a real upload error if it mentions upload/video-specific terms
            if (candidate && /upload|video|file|format|size|上传|视频|文件|格式/i.test(candidate)) {
                errorText = candidate;
            }
        }

        // Read progress from the upload progress bar or text
        const progressBar = dialog
            ? dialog.querySelector('ytcp-video-upload-progress, .upload-progress, .progress-bar')
            : null;
        const progressText = progressBar ? normalize(progressBar.textContent) : '';

        // Check for percentage in text
        const pctMatch = progressText.match(/(\\d+)\\s*%/);
        const progressPct = pctMatch ? parseInt(pctMatch[1]) : -1;

        // Status text from progress label
        const statusEl = dialog
            ? dialog.querySelector('.progress-label .progress-label-text, .upload-status')
            : null;
        const statusText = statusEl ? normalize(statusEl.textContent) : progressText;

        // Primary signal: Done button is enabled (not aria-disabled)
        const doneBtn = document.querySelector('#done-button');
        const doneEnabled = doneBtn && doneBtn.getAttribute('aria-disabled') !== 'true'
            && !doneBtn.hasAttribute('disabled');

        // Secondary signals for completion
        const completionPatterns = /upload complete|processing will begin|checks complete|100\\s*%|上传完成|处理完成/i;
        const isComplete = doneEnabled || (progressPct >= 100) || completionPatterns.test(statusText);

        // Still uploading indicators
        const uploadingPatterns = /uploading(?!.*complete)|上传中|\\d+%.*remaining|剩余/i;
        const stillUploading = uploadingPatterns.test(statusText) && progressPct < 100 && progressPct >= 0;

        return {
            error: errorText,
            progress_pct: progressPct,
            status_text: statusText,
            complete: isComplete && !stillUploading,
            done_enabled: !!doneEnabled,
        };
    }""")


# ── Publish confirmation ────────────────────────────────────────────


def _wait_for_publish_confirmation(page, timeout_ms: int) -> PublishResult:
    """Wait for the publish success dialog and extract the video URL."""
    deadline = time.monotonic() + timeout_ms / 1000

    while time.monotonic() < deadline:
        state = _read_publish_state(page)

        if state["error"]:
            return PublishResult(success=False, error=state["error"])

        if state["video_url"]:
            return PublishResult(
                success=True,
                share_url=state["video_url"],
                feed_id=_extract_video_id(state["video_url"]),
            )

        if state["success"]:
            return PublishResult(success=True, share_url="", feed_id="")

        page.wait_for_timeout(UPLOAD_POLL_MS)

    return PublishResult(success=False, error="Publish confirmation timed out")


def _read_publish_state(page) -> dict[str, Any]:
    """Read publish completion state from the dialog."""
    return page.evaluate("""() => {
        const normalize = (v) => (v || '').replace(/\\s+/g, ' ').trim();

        // Scope checks to the upload dialog and any post-publish dialogs
        const dialog = document.querySelector('ytcp-uploads-still-processing-dialog')
            || document.querySelector('ytcp-uploads-dialog');
        const dialogText = dialog ? normalize(dialog.textContent) : '';

        // Look for video link in the success view
        let videoUrl = '';
        const searchRoot = dialog || document;
        const linkEl = searchRoot.querySelector('a.video-url-fadeable, a[href*="youtube.com/shorts/"], a[href*="youtube.com/watch"], a[href*="youtu.be/"], .video-url');
        if (linkEl) {
            videoUrl = linkEl.href || linkEl.textContent || '';
        }
        // Also check for any link containing shorts or watch
        if (!videoUrl) {
            const links = searchRoot.querySelectorAll('a[href*="youtube.com"]');
            for (const l of links) {
                const href = l.href || '';
                if (/shorts\\/|watch\\?v=|youtu\\.be\\//.test(href)) {
                    videoUrl = href;
                    break;
                }
            }
        }
        // Fallback: look for video URL in dialog text
        if (!videoUrl && dialogText) {
            const urlMatch = dialogText.match(/(https?:\\/\\/(?:www\\.)?(?:youtube\\.com\\/(?:shorts\\/|watch\\?v=)|youtu\\.be\\/)\\S+)/);
            if (urlMatch) videoUrl = urlMatch[1];
        }

        // Success indicators
        const successPatterns = /video published|video is being published|successfully published|上传成功|发布成功|已发布/i;
        const success = successPatterns.test(dialogText) || !!videoUrl;

        // Error indicators — only from within the dialog
        let error = '';
        if (dialog) {
            const errorEl = dialog.querySelector('.error-short, .error-message, #error-message');
            const candidate = errorEl ? normalize(errorEl.textContent) : '';
            if (candidate && /upload|publish|video|file|发布|上传|视频/i.test(candidate)) {
                error = candidate;
            }
        }

        // Check if the upload dialog has closed (publish complete)
        const uploadDialog = document.querySelector('ytcp-uploads-dialog');
        const dialogClosed = !uploadDialog || uploadDialog.hidden
            || getComputedStyle(uploadDialog).display === 'none';

        return {
            video_url: videoUrl,
            success: success || dialogClosed,
            error: error,
        };
    }""")


def _extract_video_id(url: str) -> str:
    """Extract video ID from YouTube URL."""
    if not url:
        return ""
    match = re.search(r"(?:shorts/|watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return match.group(1) if match else ""

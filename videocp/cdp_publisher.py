from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from videocp.browser import BrowserConfig, open_download_browser_session
from videocp.publisher import PublishResult
from videocp.runtime_log import log_info, log_warn

EDITOR_PLACEHOLDER = "期待你的分享..."
EDITOR_CONTAINER_SELECTOR = ".publish-editor-container"
EDITOR_SELECTOR = ".ProseMirror[contenteditable=true]"
UPLOAD_CONTAINER_SELECTOR = f"{EDITOR_CONTAINER_SELECTOR} .image-video-container"
UPLOAD_INPUT_SELECTOR = f"{UPLOAD_CONTAINER_SELECTOR} input[type=file][accept*='video']"
UPLOAD_APPLY_TOKEN = "applysliceupload"
UPLOAD_SLICE_TOKEN = "uploadslicedata"
UPLOAD_POLL_MS = 1000
UPLOAD_SETTLE_SECONDS = 1.0
POST_SUCCESS_PAUSE_MS = 4000
PUBLISH_REQUEST_BODY_TOKENS = ("jsonfeed", "patterninfo", "feed_type", "feedtype", "clientcontent")
PUBLISH_URL_TOKENS = ("publishfeed", "feed/publish", "publish", "feed/create")


@dataclass(frozen=True, slots=True)
class UploadRequestInfo:
    kind: str
    is_video: bool = False
    index: int = -1


@dataclass(slots=True)
class UploadNetworkTracker:
    saw_apply: bool = False
    saw_video_apply: bool = False
    saw_slice: bool = False
    saw_blob_preview: bool = False
    apply_statuses: list[int] = field(default_factory=list)
    slice_statuses: list[tuple[int, int]] = field(default_factory=list)
    inflight_ids: set[int] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    last_event_at: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_event_at = time.monotonic()

    @property
    def inflight_count(self) -> int:
        return len(self.inflight_ids)

    @property
    def is_network_settled(self) -> bool:
        return self.saw_video_apply and self.saw_slice and self.inflight_count == 0

    def record_request(self, info: UploadRequestInfo, request_id: int) -> None:
        if info.kind == "ignore":
            return
        self.touch()
        if info.kind == "blob_preview":
            self.saw_blob_preview = True
            return
        self.inflight_ids.add(request_id)
        if info.kind == "apply":
            self.saw_apply = True
            self.saw_video_apply = self.saw_video_apply or info.is_video
            return
        if info.kind == "slice":
            self.saw_slice = True

    def record_response(self, info: UploadRequestInfo, status: int, url: str) -> None:
        if info.kind in {"ignore", "blob_preview"}:
            return
        self.touch()
        if status >= 400:
            self.errors.append(f"{info.kind} request failed with HTTP {status}: {url}")
            return
        if info.kind == "apply":
            self.apply_statuses.append(status)
            return
        if info.kind == "slice":
            self.slice_statuses.append((info.index, status))

    def record_finished(self, info: UploadRequestInfo, request_id: int) -> None:
        if info.kind in {"ignore", "blob_preview"}:
            return
        self.inflight_ids.discard(request_id)
        self.touch()

    def record_failed(self, info: UploadRequestInfo, request_id: int, failure: str) -> None:
        if info.kind in {"ignore", "blob_preview"}:
            return
        self.inflight_ids.discard(request_id)
        self.errors.append(f"{info.kind} request failed: {failure or 'unknown failure'}")
        self.touch()


def cdp_publish_to_channel(
    browser_config: BrowserConfig,
    video_path: Path,
    guild_id: str,
    title: str,
    timeout_secs: int = 300,
) -> PublishResult:
    channel_url = f"https://pd.qq.com/g/{guild_id}"
    log_info("cdp_publish.start", guild_id=guild_id, url=channel_url, video=str(video_path))

    with open_download_browser_session(browser_config) as session:
        page = session.new_page()
        try:
            return _do_publish(page, channel_url, video_path, title, timeout_secs)
        except Exception as exc:
            log_warn("cdp_publish.error", error=str(exc))
            return PublishResult(success=False, error=str(exc))
        finally:
            page.close()


def _do_publish(page, channel_url: str, video_path: Path, title: str, timeout_secs: int) -> PublishResult:
    timeout_ms = timeout_secs * 1000

    log_info("cdp_publish.navigate", url=channel_url)
    page.goto(channel_url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)

    log_info("cdp_publish.activate_editor")
    _prepare_clean_editor(page, channel_url)

    log_info("cdp_publish.fill_content", title=title[:50])
    editor = page.locator(EDITOR_SELECTOR).first
    editor.fill(title)
    page.wait_for_timeout(300)

    log_info("cdp_publish.upload_video", path=str(video_path))
    upload_tracker = _attach_upload_network_listeners(page)
    _set_video_input_files(page, video_path)

    log_info("cdp_publish.wait_upload")
    _wait_for_upload(page, timeout_ms, upload_tracker)

    log_info("cdp_publish.submit")
    publish_responses: list[dict[str, Any]] = []

    def _on_response(response) -> None:
        request = response.request
        if request.method != "POST":
            return
        post_data = request.post_data or ""
        if not _looks_like_publish_request(response.url, post_data):
            return
        body_text = ""
        body_json: Any = None
        try:
            body_text = response.text()
        except Exception:
            body_text = ""
        if body_text:
            try:
                body_json = json.loads(body_text)
            except json.JSONDecodeError:
                body_json = None
        publish_responses.append({
            "url": response.url,
            "status": response.status,
            "body": body_json,
            "body_text": body_text[:4000],
            "request_post_data": post_data[:4000],
        })
        log_info("cdp_publish.api_response", url=response.url[:120], status=response.status)

    page.on("response", _on_response)

    _click_publish_button(page)
    result = _wait_for_publish(page, timeout_ms, publish_responses, channel_url=channel_url)
    log_info("cdp_publish.complete", success=result.success)
    _pause_after_success(page, result)
    return result


def _pause_after_success(page, result: PublishResult) -> None:
    if not result.success:
        return
    # Leave the compose page open briefly after a successful post so the flow
    # looks closer to normal user behavior before the tab is closed.
    log_info("cdp_publish.success_pause", seconds=POST_SUCCESS_PAUSE_MS / 1000)
    page.wait_for_timeout(POST_SUCCESS_PAUSE_MS)


def _prepare_clean_editor(page, channel_url: str) -> None:
    last_error = ""
    for attempt in range(2):
        _activate_editor(page)
        editor_text = _read_editor_text(page)
        upload_state = _read_upload_state(page)
        if not editor_text and not _has_stale_media(upload_state) and not upload_state["upload_busy"]:
            return
        last_error = (
            "Could not prepare clean editor before publish. "
            f"editor={editor_text[:80]} preview={upload_state['has_preview_content']} "
            f"upload_busy={upload_state['upload_busy']} detail={upload_state['text'][:120]}"
        )
        if attempt == 1:
            raise RuntimeError(last_error)
        log_warn("cdp_publish.reset_retry", attempt=attempt + 1, error=last_error)
        page.goto(channel_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

    raise RuntimeError(last_error or "Could not prepare clean editor")


def _activate_editor(page) -> None:
    header = page.locator(".editor-header.pointer").first
    try:
        header.click(timeout=10000)
        page.wait_for_timeout(500)
    except Exception:
        pass
    page.locator(EDITOR_SELECTOR).first.wait_for(timeout=5000)


def _click_publish_button(page) -> None:
    for selector in (
        ".publish-button button",
        "button:has-text('发表')",
        "button:has-text('发布')",
    ):
        locator = page.locator(selector)
        try:
            if locator.count() == 0:
                continue
        except Exception:
            continue
        locator.first.click()
        return
    raise RuntimeError("Publish button not found")


def _set_video_input_files(page, video_path: Path) -> None:
    file_input = page.locator(UPLOAD_INPUT_SELECTOR).first
    file_input.wait_for(state="attached", timeout=5000)
    file_input.set_input_files(str(video_path.resolve()))


def _attach_upload_network_listeners(page) -> UploadNetworkTracker:
    tracker = UploadNetworkTracker()

    def _request_key(request) -> int:
        return id(request)

    def _on_request(request) -> None:
        info = _classify_upload_request(
            method=request.method,
            url=request.url,
            post_data=request.post_data or "",
            resource_type=request.resource_type,
        )
        tracker.record_request(info, _request_key(request))

    def _on_response(response) -> None:
        info = _classify_upload_request(
            method=response.request.method,
            url=response.url,
            post_data=response.request.post_data or "",
            resource_type=response.request.resource_type,
        )
        tracker.record_response(info, response.status, response.url)

    def _on_request_finished(request) -> None:
        info = _classify_upload_request(
            method=request.method,
            url=request.url,
            post_data=request.post_data or "",
            resource_type=request.resource_type,
        )
        tracker.record_finished(info, _request_key(request))

    def _on_request_failed(request) -> None:
        info = _classify_upload_request(
            method=request.method,
            url=request.url,
            post_data=request.post_data or "",
            resource_type=request.resource_type,
        )
        tracker.record_failed(info, _request_key(request), request.failure or "")

    page.on("request", _on_request)
    page.on("response", _on_response)
    page.on("requestfinished", _on_request_finished)
    page.on("requestfailed", _on_request_failed)
    return tracker


def _wait_for_upload(page, timeout_ms: int, tracker: UploadNetworkTracker | None = None) -> None:
    upload_tracker = tracker or UploadNetworkTracker()
    deadline = time.monotonic() + timeout_ms / 1000
    last_signature = ""

    while time.monotonic() < deadline:
        state = _read_upload_state(page)
        if upload_tracker.errors:
            raise RuntimeError(upload_tracker.errors[0])
        if state["error_text"]:
            raise RuntimeError(state["error_text"])
        if _upload_is_ready(state, upload_tracker):
            log_info(
                "cdp_publish.upload_done",
                detail=state["text"][:120],
                preview_children=state["preview_children"],
                slices=len(upload_tracker.slice_statuses),
            )
            page.wait_for_timeout(UPLOAD_POLL_MS)
            return

        signature = (
            f"{state['has_preview_content']}|{state['upload_busy']}|{state['text']}|{state['error_text']}|"
            f"{upload_tracker.saw_video_apply}|{upload_tracker.saw_slice}|{upload_tracker.inflight_count}|"
            f"{upload_tracker.saw_blob_preview}"
        )
        if signature != last_signature:
            event = "cdp_publish.uploading" if state["upload_busy"] else "cdp_publish.upload_waiting"
            detail = state["text"][:120] or "no preview yet"
            log_info(
                event,
                detail=detail,
                inflight=upload_tracker.inflight_count,
                slices=len(upload_tracker.slice_statuses),
                blob_preview=upload_tracker.saw_blob_preview,
            )
            last_signature = signature
        page.wait_for_timeout(UPLOAD_POLL_MS)

    last_state = _read_upload_state(page)
    raise TimeoutError(
        "Video upload did not complete within timeout. "
        f"detail={last_state['text'][:200]} "
        f"saw_video_apply={upload_tracker.saw_video_apply} "
        f"slices={len(upload_tracker.slice_statuses)} inflight={upload_tracker.inflight_count} "
        f"blob_preview={upload_tracker.saw_blob_preview}"
    )


def _upload_is_ready(state: dict[str, Any], tracker: UploadNetworkTracker) -> bool:
    if state["has_preview_content"] and not state["upload_busy"]:
        return True
    if not tracker.is_network_settled or state["upload_busy"]:
        return False
    if not (
        state["has_preview_content"]
        or _looks_like_uploaded_video_detail(state["text"])
        or tracker.saw_blob_preview
    ):
        return False
    return time.monotonic() - tracker.last_event_at >= UPLOAD_SETTLE_SECONDS


def _wait_for_publish(
    page,
    timeout_ms: int,
    publish_responses: list[dict[str, Any]],
    *,
    channel_url: str = "",
) -> PublishResult:
    deadline = time.monotonic() + timeout_ms / 1000
    checked_count = 0
    page.wait_for_timeout(1500)

    while time.monotonic() < deadline:
        while checked_count < len(publish_responses):
            response = publish_responses[checked_count]
            checked_count += 1
            outcome = _extract_publish_outcome(response.get("body"), response.get("body_text", ""))
            if outcome is None:
                continue
            log_info("cdp_publish.api_check", url=response["url"][:100], retCode=outcome["ret"])
            if outcome["ret"] == 0:
                current_url = channel_url or getattr(page, "url", "")
                share_url = outcome["share_url"] or _build_feed_detail_url(current_url, outcome["feed_id"])
                log_info("cdp_publish.api_ok", feed_id=outcome["feed_id"])
                return PublishResult(
                    success=True,
                    feed_id=outcome["feed_id"],
                    share_url=share_url,
                )
            error = outcome["error"] or f"retCode={outcome['ret']}"
            log_warn("cdp_publish.api_fail", retCode=outcome["ret"], error=error)
            return PublishResult(success=False, error=error)

        state = _read_publish_state(page)
        if state["error_text"]:
            return PublishResult(success=False, error=state["error_text"])
        if state["success_text"]:
            log_info("cdp_publish.dom_ok", reason="success_message")
            return PublishResult(success=True, feed_id="", share_url="")
        if state["editor_empty"] and state["preview_empty"]:
            log_info("cdp_publish.dom_ok", reason="editor_cleared")
            return PublishResult(success=True, feed_id="", share_url="")

        page.wait_for_timeout(UPLOAD_POLL_MS)

    last_state = _read_publish_state(page)
    return PublishResult(
        success=False,
        error=(
            "Publish did not complete within timeout. "
            f"editor_empty={last_state['editor_empty']} preview_empty={last_state['preview_empty']} "
            f"detail={last_state['detail'][:200]}"
        ),
    )


def _read_editor_text(page) -> str:
    text = page.evaluate("""(selectors) => {
        const editor = document.querySelector(selectors.editorSelector);
        return editor ? (editor.textContent || '') : '';
    }""", {"editorSelector": f"{EDITOR_CONTAINER_SELECTOR} {EDITOR_SELECTOR}"})
    return _normalize_editor_text(str(text or ""))


def _read_upload_state(page) -> dict[str, Any]:
    return page.evaluate("""(selectors) => {
        const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
        const container = document.querySelector(selectors.uploadContainerSelector);
        if (!container) {
            return {
                text: '',
                error_text: '',
                preview_children: 0,
                has_preview_content: false,
                upload_busy: false,
            };
        }

        const previewRoot = container.querySelector('.image-video-preview');
        const previewChildren = container.querySelectorAll('.preview-list .image-item, .preview-item').length;
        const hasPreviewContent = !!(
            (previewRoot && (previewRoot.children.length > 0 || ['IMG', 'VIDEO', 'CANVAS'].includes(previewRoot.tagName)))
            || container.querySelector('.preview-list .image-item, .preview-item')
            || container.querySelector('img.image-video-preview, video.image-video-preview, canvas.image-video-preview')
            || container.querySelector('.image-item img, .image-item video, .image-item canvas')
            || container.querySelector('.preview-item, .video-preview, .upload-success')
        );

        const visibleText = normalize(container.textContent || '');
        const uploadBusy = !!container.querySelector(
            '.progress, .uploading, [class*=progress], [class*=loading], [class*=uploading], .is-loading'
        ) || /上传中|处理中|转码中|校验中|封面生成中|\\d+%/.test(visibleText);

        const editorContainer = document.querySelector(selectors.editorContainerSelector);
        const errorItem = Array.from(document.querySelectorAll('body *')).find((el) => {
            const cls = typeof el.className === 'string' ? el.className : '';
            const text = normalize(el.textContent || '');
            if (!text || !/上传失败|视频上传失败|发表失败|发布失败|发帖失败|请重试|格式不支持|文件过大|转码失败/.test(text)) {
                return false;
            }
            return /toast|error|tip|message|notify|upload|preview/.test(cls)
                || !!(editorContainer && editorContainer.contains(el))
                || !!el.closest('.image-video-container');
        });

        return {
            text: visibleText,
            error_text: errorItem ? normalize(errorItem.textContent || '') : '',
            preview_children: previewChildren,
            has_preview_content: hasPreviewContent,
            upload_busy: uploadBusy,
        };
    }""", {
        "uploadContainerSelector": UPLOAD_CONTAINER_SELECTOR,
        "editorContainerSelector": EDITOR_CONTAINER_SELECTOR,
    })


def _read_publish_state(page) -> dict[str, Any]:
    return page.evaluate("""(selectors) => {
        const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
        const editor = document.querySelector(selectors.editorSelector);
        const editorTextRaw = normalize(editor ? (editor.textContent || '') : '');
        const editorText = normalize(editorTextRaw.replaceAll('期待你的分享...', ''));

        const container = document.querySelector(selectors.uploadContainerSelector);
        const previewRoot = container ? container.querySelector('.image-video-preview') : null;
        const hasPreviewContent = !!(
            (previewRoot && (previewRoot.children.length > 0 || ['IMG', 'VIDEO', 'CANVAS'].includes(previewRoot.tagName)))
            || (container && container.querySelector('.preview-list .image-item, .preview-item'))
            || (container && container.querySelector('img.image-video-preview, video.image-video-preview, canvas.image-video-preview'))
            || (container && container.querySelector('.image-item img, .image-item video, .image-item canvas'))
            || (container && container.querySelector('.preview-item, .video-preview, .upload-success'))
        );

        const editorContainer = document.querySelector(selectors.editorContainerSelector);
        const messages = Array.from(document.querySelectorAll('body *')).filter((el) => {
            const cls = typeof el.className === 'string' ? el.className : '';
            return /toast|error|tip|message|notify|success|alert/.test(cls)
                || !!(editorContainer && editorContainer.contains(el));
        });

        const successItem = messages.find((el) =>
            /发表成功|发布成功|发帖成功|分享成功/.test(normalize(el.textContent || ''))
        );
        const errorItem = messages.find((el) =>
            /上传失败|视频上传失败|发表失败|发布失败|发帖失败|网络异常|请重试|格式不支持|文件过大|转码失败/.test(normalize(el.textContent || ''))
        );

        return {
            editor_empty: editorText === '',
            preview_empty: !hasPreviewContent,
            success_text: successItem ? normalize(successItem.textContent || '') : '',
            error_text: errorItem ? normalize(errorItem.textContent || '') : '',
            detail: normalize((container ? container.textContent : '') || ''),
        };
    }""", {
        "editorSelector": f"{EDITOR_CONTAINER_SELECTOR} {EDITOR_SELECTOR}",
        "uploadContainerSelector": UPLOAD_CONTAINER_SELECTOR,
        "editorContainerSelector": EDITOR_CONTAINER_SELECTOR,
    })


def _normalize_editor_text(value: str) -> str:
    return " ".join(value.replace(EDITOR_PLACEHOLDER, "").split()).strip()


def _has_stale_media(upload_state: dict[str, Any]) -> bool:
    return bool(upload_state["has_preview_content"] or _looks_like_uploaded_video_detail(upload_state["text"]))


def _classify_upload_request(method: str, url: str, post_data: str, resource_type: str) -> UploadRequestInfo:
    lowered_url = url.lower()
    lowered_body = (post_data or "").lower()
    if method == "GET" and url.startswith("blob:") and resource_type == "media":
        return UploadRequestInfo(kind="blob_preview")
    if method != "POST":
        return UploadRequestInfo(kind="ignore")
    if UPLOAD_APPLY_TOKEN in lowered_url:
        return UploadRequestInfo(
            kind="apply",
            is_video='"appid":1003' in lowered_body or '"business_type":2' in lowered_body,
        )
    if UPLOAD_SLICE_TOKEN in lowered_url:
        parsed = urlparse(url)
        index_raw = parse_qs(parsed.query).get("index", ["0"])[0]
        try:
            index = int(index_raw)
        except ValueError:
            index = -1
        return UploadRequestInfo(kind="slice", index=index)
    return UploadRequestInfo(kind="ignore")


def _looks_like_uploaded_video_detail(text: str) -> bool:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return False
    parts = [part for part in cleaned.split(" ") if part]
    if len(parts) == 1 and _is_duration_token(parts[0]):
        return True
    return any(_is_duration_token(part) for part in parts[:3])


def _is_duration_token(value: str) -> bool:
    pieces = value.split(":")
    if len(pieces) not in {2, 3}:
        return False
    if not all(piece.isdigit() for piece in pieces):
        return False
    return all(len(piece) <= 2 for piece in pieces)


def _looks_like_publish_request(url: str, post_data: str) -> bool:
    host = urlparse(url).netloc.lower()
    if host and not host.endswith("pd.qq.com"):
        return False
    lowered_url = url.lower()
    lowered_body = (post_data or "").lower()
    if any(token in lowered_body for token in PUBLISH_REQUEST_BODY_TOKENS):
        return True
    return any(token in lowered_url for token in PUBLISH_URL_TOKENS) and "feed" in lowered_url


def _extract_publish_outcome(body: Any, body_text: str) -> dict[str, Any] | None:
    payload = body
    if payload is None and body_text:
        try:
            payload = json.loads(body_text)
        except json.JSONDecodeError:
            payload = None
    if payload is None:
        return None

    ret = _coerce_int(_deep_find_first(payload, {"retCode", "retcode", "ret_code", "code", "ret", "errCode"}))
    success_flag = _deep_find_first(payload, {"success", "ok"})
    feed_id = _extract_feed_id(payload)
    share_url = str(_deep_find_first(payload, {"shareUrl", "share_url", "url"}) or "")
    error_text = str(_deep_find_first(payload, {"errMsg", "error", "errorMsg", "msg", "message"}) or "")

    if ret is None and success_flag is True:
        ret = 0
    if ret is None and feed_id:
        ret = 0
    if ret is None:
        return None

    if ret != 0 and error_text:
        error = f"retCode={ret} {error_text}".strip()
    elif ret != 0:
        error = f"retCode={ret}"
    else:
        error = ""
    return {
        "ret": ret,
        "feed_id": feed_id,
        "share_url": share_url,
        "error": error,
    }


def _extract_feed_id(payload: Any) -> str:
    direct = _deep_find_first(payload, {"feedId", "feed_id"})
    if isinstance(direct, str) and direct.startswith("B_"):
        return direct

    nested = _deep_get(payload, ["data", "feed", "id"])
    if isinstance(nested, str) and nested.startswith("B_"):
        return nested

    fallback = _deep_find_matching_string(payload, prefix="B_")
    return fallback or ""


def _deep_get(value: Any, path: list[str]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _deep_find_matching_string(value: Any, *, prefix: str) -> str:
    if isinstance(value, str):
        return value if value.startswith(prefix) else ""
    if isinstance(value, dict):
        for item in value.values():
            found = _deep_find_matching_string(item, prefix=prefix)
            if found:
                return found
        return ""
    if isinstance(value, list):
        for item in value:
            found = _deep_find_matching_string(item, prefix=prefix)
            if found:
                return found
    return ""


def _build_feed_detail_url(channel_url: str, feed_id: str) -> str:
    if not channel_url or not feed_id:
        return ""
    parsed = urlparse(channel_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2 or segments[0] != "g":
        return ""
    guild_segment = segments[1]
    return f"{parsed.scheme or 'https'}://{parsed.netloc or 'pd.qq.com'}/g/{guild_segment}/post/{feed_id}"


def _deep_find_first(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys:
                return item
        for item in value.values():
            found = _deep_find_first(item, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _deep_find_first(item, keys)
            if found is not None:
                return found
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return None

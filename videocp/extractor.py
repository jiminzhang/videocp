from __future__ import annotations

from dataclasses import dataclass, field

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from videocp.errors import ExtractionError
from videocp.models import ExtractionResult, MediaCandidate, ObservedEvent, VideoMetadata
from videocp.providers import (
    SiteProvider,
    get_provider_by_key,
    infer_track_type as generic_infer_track_type,
    normalize_candidate_url,
    resolve_provider,
)
from videocp.runtime_log import full_url, log_info, log_warn

SAFE_HEADER_KEYS = {"content-type", "content-length", "content-range", "accept-ranges"}
DEFAULT_PROVIDER = get_provider_by_key("douyin")
CANDIDATE_LOG_LIMIT = 3


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items() if key.lower() in SAFE_HEADER_KEYS}


def infer_track_type(url: str, kind, semantic_tag: str = ""):
    return generic_infer_track_type(url, kind, semantic_tag=semantic_tag)


def candidate_rank(candidate: MediaCandidate) -> tuple[int, int, int, int, str]:
    return DEFAULT_PROVIDER.candidate_rank(candidate)


def conservative_rewrites(candidates: list[MediaCandidate]) -> list[MediaCandidate]:
    return DEFAULT_PROVIDER.conservative_rewrites(candidates)


def sort_candidates(candidates: list[MediaCandidate], provider: SiteProvider | None = None) -> list[MediaCandidate]:
    return (provider or DEFAULT_PROVIDER).sort_candidates(candidates)


@dataclass(slots=True)
class ExtractionAccumulator:
    metadata: VideoMetadata
    provider: SiteProvider = field(default=DEFAULT_PROVIDER)
    candidates: list[MediaCandidate] = field(default_factory=list)
    seen_urls: set[str] = field(default_factory=set)
    event_count: int = 0
    json_event_count: int = 0
    markup_payload_count: int = 0
    candidate_log_count: int = 0

    def __post_init__(self) -> None:
        if not self.metadata.site:
            self.metadata.site = self.provider.key

    def add_candidate(
        self,
        url: str,
        source: str,
        observed_via: str,
        *,
        semantic_tag: str = "",
        content_type: str = "",
        note: str = "",
    ) -> None:
        normalized = normalize_candidate_url(url)
        if not normalized or normalized.startswith("blob:") or normalized.startswith("data:"):
            return
        kind = self.provider.infer_media_kind(url, content_type, semantic_tag=semantic_tag)
        if kind is None:
            return
        if normalized in self.seen_urls:
            return
        self.seen_urls.add(normalized)
        candidate = MediaCandidate(
            url=url,
            kind=kind,
            track_type=self.provider.infer_track_type(url, kind, content_type=content_type, semantic_tag=semantic_tag),
            watermark_mode=self.provider.infer_watermark_mode(url, semantic_tag=semantic_tag),
            source=source,
            observed_via=observed_via,
            note=note,
        )
        self.candidates.append(candidate)
        if self.candidate_log_count < CANDIDATE_LOG_LIMIT:
            log_info(
                "extract.candidate",
                site=self.metadata.site or self.provider.key,
                via=observed_via,
                source=source,
                kind=candidate.kind.value,
                track=candidate.track_type.value,
                watermark=candidate.watermark_mode.value,
                url=full_url(candidate.url),
            )
        elif self.candidate_log_count == CANDIDATE_LOG_LIMIT:
            log_info(
                "extract.candidate",
                site=self.metadata.site or self.provider.key,
                note="more candidates omitted",
            )
        self.candidate_log_count += 1

    def ingest_event(self, event: ObservedEvent) -> None:
        self.event_count += 1
        self.add_candidate(
            event.url,
            source=event.origin,
            observed_via="network",
            content_type=event.content_type,
        )
        if event.json_body is not None:
            self.json_event_count += 1
            self.provider.scan_json_payload(self, event.json_body)

    def ingest_dom_snapshot(self, snapshot: dict[str, str]) -> None:
        self.provider.apply_dom_snapshot(self.metadata, snapshot, self.add_candidate)

    def ingest_markup(self, markup: str) -> None:
        before = self.json_event_count
        self.provider.scan_markup(self, markup)
        self.markup_payload_count += max(0, self.json_event_count - before)


def capture_dom_snapshot(page: Page) -> dict[str, str]:
    return page.evaluate(
        """() => {
            const meta = (name, attr = "name") =>
              document.querySelector(`meta[${attr}="${name}"]`)?.content || "";
            const firstText = (selectors) => {
              for (const selector of selectors) {
                const value = document.querySelector(selector)?.textContent?.trim();
                if (value && !["我", "登录"].includes(value)) {
                  return value;
                }
              }
              return "";
            };
            const video = document.querySelector("video");
            if (video) {
              video.muted = true;
              video.play().catch(() => {});
            }
            return {
              page_url: window.location.href,
              title: document.title || "",
              og_title: meta("og:title"),
              og_video: meta("og:video"),
              og_description: meta("og:description"),
              description: meta("description"),
              og_type: meta("og:type"),
              video_src: video?.currentSrc || video?.src || "",
              author_text: firstText([
                '.author-wrapper .info .name',
                '.author-wrapper .name',
                '.author-container .name',
                '.note-container .name',
                '.note-content a.name[href*="/user/profile/"]',
                '.interaction-container a.name[href*="/user/profile/"]',
                'a.note-content-user[href*="/user/profile/"]',
                'a.name[href*="/user/profile/"]',
                '#v_upinfo .up-name',
                '.up-name',
                '[data-e2e="user-name"]',
              ]),
            };
        }"""
    )


def extract_video(page: Page, source_url: str, timeout_secs: int) -> ExtractionResult:
    provider = resolve_provider(source_url)
    log_info("extract.start", site=provider.key, url=full_url(source_url), timeout_secs=timeout_secs)
    accumulator = ExtractionAccumulator(
        metadata=provider.create_metadata(source_url),
        provider=provider,
    )

    def on_request(request) -> None:
        if provider.is_media_request_candidate(request.url):
            accumulator.ingest_event(
                ObservedEvent(
                    url=request.url,
                    resource_type=request.resource_type,
                    origin="request",
                )
            )

    def on_response(response) -> None:
        headers = {key.lower(): value for key, value in response.headers.items()}
        content_type = headers.get("content-type", "")
        if not (
            provider.should_parse_json(response.url, content_type)
            or provider.is_media_request_candidate(response.url)
            or provider.infer_media_kind(response.url, content_type) is not None
        ):
            return
        json_body = None
        if provider.should_parse_json(response.url, content_type):
            try:
                json_body = response.json()
            except Exception:
                json_body = None
        accumulator.ingest_event(
            ObservedEvent(
                url=response.url,
                status=response.status,
                resource_type=response.request.resource_type,
                content_type=content_type,
                headers=redact_headers(headers),
                json_body=json_body,
                origin="response",
            )
        )

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        page.goto(source_url, wait_until="domcontentloaded", timeout=timeout_secs * 1000)
    except PlaywrightTimeoutError as exc:
        raise ExtractionError(f"Navigation timed out for {source_url}: {exc}") from exc
    log_info("extract.goto.ok", site=provider.key, page_url=full_url(page.url))

    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_secs * 1000, 5000))
        log_info("extract.networkidle.ok", site=provider.key)
    except PlaywrightTimeoutError:
        log_warn("extract.networkidle.timeout", site=provider.key)
    try:
        page.locator("video").first.wait_for(timeout=4000)
        log_info("extract.video_element.ok", site=provider.key)
    except PlaywrightTimeoutError:
        log_warn("extract.video_element.timeout", site=provider.key)
    page.wait_for_timeout(4000)

    snapshot = capture_dom_snapshot(page)
    accumulator.ingest_dom_snapshot(snapshot)
    log_info(
        "extract.snapshot",
        site=provider.key,
        page_url=full_url(snapshot.get("page_url", "")),
        title=snapshot.get("title", ""),
        author=snapshot.get("author_text", ""),
        video_src=full_url(snapshot.get("video_src", "")),
    )

    markup = page.content()
    accumulator.ingest_markup(markup)
    page.wait_for_timeout(2000)

    candidates = provider.sort_candidates(accumulator.candidates)
    if not candidates:
        raise ExtractionError(f"No media candidates observed from the {provider.display_name} page.")

    user_agent = page.evaluate("() => navigator.userAgent")
    diagnostics = {
        "site": provider.key,
        "page_url": snapshot.get("page_url", ""),
        "event_count": accumulator.event_count,
        "json_event_count": accumulator.json_event_count,
        "markup_payload_count": accumulator.markup_payload_count,
        "candidate_count": len(candidates),
        "title": snapshot.get("title", ""),
    }
    top_candidates = "; ".join(
        f"{candidate.kind.value}/{candidate.track_type.value}/{candidate.watermark_mode.value}/{candidate.source}"
        for candidate in candidates[:3]
    )
    log_info(
        "extract.complete",
        site=provider.key,
        content_id=accumulator.metadata.content_id or "unknown",
        author=accumulator.metadata.author or "unknown",
        events=accumulator.event_count,
        json_payloads=accumulator.json_event_count,
        candidates=len(candidates),
        top_candidates=top_candidates,
    )
    return ExtractionResult(
        metadata=accumulator.metadata,
        candidates=candidates,
        cookies=page.context.cookies(),
        user_agent=user_agent,
        diagnostics=diagnostics,
    )

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from videocp.app import (
    DownloadOptions,
    _expand_profile_inputs,
    _run_download_jobs,
)
from videocp.browser import BrowserConfig
from videocp.config import AppConfig, SyncConfig, SyncTaskConfig
from videocp.errors import SyncError
from videocp.input_parser import parse_input
from videocp.profile import default_profile_dir, detect_system_browser_executable
from videocp.publisher import publish_to_channel
from videocp.runtime_log import full_url, log_info, log_warn
from videocp.sync_history import (
    SyncHistory,
    SyncHistoryEntry,
    add_entry,
    find_processed_entry,
    load_history,
)


@dataclass(slots=True)
class SyncOptions:
    app_config: AppConfig
    sync_config: SyncConfig
    dry_run: bool = False
    task_name_filter: str | None = None
    count_override: int | None = None


@dataclass(slots=True)
class SyncTaskResult:
    task_name: str
    ok: bool
    content_id: str = ""
    action: str = ""  # "synced" | "skipped" | "skipped_unavailable" | "no_new_video" | "failed"
    error: str = ""
    feed_id: str = ""
    share_url: str = ""
    output_path: str = ""


def run_sync(options: SyncOptions) -> list[SyncTaskResult]:
    app_cfg = options.app_config
    sync_cfg = options.sync_config

    browser_path = app_cfg.browser_path or detect_system_browser_executable()
    if not browser_path:
        raise SyncError("No Chrome-family browser found. Use --browser-path.")

    browser_config = BrowserConfig(
        profile_dir=app_cfg.profile_dir or default_profile_dir(),
        browser_path=browser_path,
        headless=app_cfg.headless,
    )

    history = load_history(sync_cfg.history_file)
    results: list[SyncTaskResult] = []

    tasks = sync_cfg.tasks
    if options.task_name_filter:
        tasks = [t for t in tasks if t.name == options.task_name_filter]
        if not tasks:
            raise SyncError(f"No task named '{options.task_name_filter}' found.")

    log_info("sync.start", tasks=len(tasks), dry_run=options.dry_run)

    for task in tasks:
        # Resolve count: CLI override > per-task count > global videos_per_task
        count = options.count_override or task.count or sync_cfg.videos_per_task
        task_results = _sync_one_task(
            task=task,
            app_cfg=app_cfg,
            sync_cfg=sync_cfg,
            browser_config=browser_config,
            history=history,
            dry_run=options.dry_run,
            count=count,
        )
        results.extend(task_results)

    log_info("sync.complete", total=len(results), synced=sum(1 for r in results if r.action in ("synced", "synced_pinned")))
    _write_daily_log(sync_cfg, results)
    return results


def _write_daily_log(sync_cfg: SyncConfig, results: list[SyncTaskResult]) -> None:
    """Append a reconciliation entry to today's daily log file."""
    _BJT = timezone(timedelta(hours=8))
    log_dir = sync_cfg.history_file.parent / "sync_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{datetime.now(_BJT).date().isoformat()}.log"

    now = datetime.now(_BJT).isoformat()
    lines: list[str] = [f"\n--- sync run at {now} ---"]
    for r in results:
        status = "OK" if r.ok else "FAIL"
        parts = [f"[{status}] {r.task_name} action={r.action}"]
        if r.content_id:
            parts.append(f"content_id={r.content_id}")
        if r.feed_id:
            parts.append(f"feed_id={r.feed_id}")
        if r.share_url:
            parts.append(f"share_url={r.share_url}")
        if r.output_path:
            parts.append(f"video={r.output_path}")
        if r.error:
            parts.append(f"error={r.error}")
        lines.append(" ".join(parts))

    summary_ok = sum(1 for r in results if r.ok)
    summary_fail = sum(1 for r in results if not r.ok)
    lines.append(f"summary: total={len(results)} ok={summary_ok} fail={summary_fail}")

    with open(log_file, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _sync_one_task(
    task: SyncTaskConfig,
    app_cfg: AppConfig,
    sync_cfg: SyncConfig,
    browser_config: BrowserConfig,
    history: SyncHistory,
    dry_run: bool,
    count: int = 1,
) -> list[SyncTaskResult]:
    log_info("sync.task.start", task=task.name, source=full_url(task.source_url), count=count)
    results: list[SyncTaskResult] = []
    try:
        # Step 1: Expand profile to get latest video URLs
        parsed = parse_input(task.source_url, timeout_secs=app_cfg.timeout_secs)
        if not parsed.is_profile:
            expanded = [parsed]
        else:
            expanded = _expand_profile_inputs(
                [parsed], browser_config, profile_videos_count=count, timeout_secs=app_cfg.timeout_secs,
            )

        if not expanded:
            log_info("sync.task.no_new", task=task.name)
            return [SyncTaskResult(task_name=task.name, ok=True, action="no_new_video")]

        # Process each expanded video
        for video_input in expanded:
            result = _sync_one_video(
                task=task,
                video_input=video_input,
                app_cfg=app_cfg,
                sync_cfg=sync_cfg,
                browser_config=browser_config,
                history=history,
                dry_run=dry_run,
            )
            results.append(result)

    except Exception as exc:
        log_warn("sync.task.failed", task=task.name, error=str(exc))
        results.append(SyncTaskResult(task_name=task.name, ok=False, action="failed", error=str(exc)))

    return results


def _sync_one_video(
    task: SyncTaskConfig,
    video_input: ParsedInput,
    app_cfg: AppConfig,
    sync_cfg: SyncConfig,
    browser_config: BrowserConfig,
    history: SyncHistory,
    dry_run: bool,
) -> SyncTaskResult:
    try:
        content_id = _extract_content_id(video_input.canonical_url)

        # Dedup check
        processed_entry = find_processed_entry(history, task.name, content_id)
        if processed_entry is not None:
            if processed_entry.status == "ok":
                log_info("sync.task.skip", task=task.name, content_id=content_id)
                return SyncTaskResult(task_name=task.name, ok=True, content_id=content_id, action="skipped")
            log_info("sync.task.skip_unavailable", task=task.name, content_id=content_id)
            return SyncTaskResult(
                task_name=task.name,
                ok=True,
                content_id=content_id,
                action="skipped_unavailable",
                error=processed_entry.error,
            )

        if dry_run:
            log_info("sync.task.dry_run", task=task.name, content_id=content_id, url=full_url(video_input.canonical_url))
            return SyncTaskResult(task_name=task.name, ok=True, content_id=content_id, action="dry_run")

        # Random skip — pinned videos are always synced
        if not video_input.is_pinned:
            effective_skip_rate = task.skip_rate if task.skip_rate >= 0 else sync_cfg.skip_rate
            if effective_skip_rate > 0 and random.random() < effective_skip_rate:
                log_info("sync.task.skip_random", task=task.name, content_id=content_id, skip_rate=effective_skip_rate)
                return SyncTaskResult(task_name=task.name, ok=True, content_id=content_id, action="skipped_random")

        # Download (reuse existing file if already downloaded)
        existing = _find_existing_download(app_cfg.output_dir, content_id)
        if existing:
            log_info("sync.task.reuse_download", task=task.name, path=str(existing["output_path"]))
            output_path = existing["output_path"]
            site = existing["site"]
            author = existing["author"]
            desc = existing["desc"]
            video_title = existing["title"]
            actual_content_id = existing["content_id"] or content_id
        else:
            log_info("sync.task.download", task=task.name, url=full_url(video_input.canonical_url))
            download_timeout = max(app_cfg.timeout_secs, 120)
            job_results = _run_download_jobs(
                prepared_inputs=[video_input],
                browser_config=browser_config,
                output_dir=app_cfg.output_dir,
                timeout_secs=download_timeout,
                max_concurrent=1,
                max_concurrent_per_site=1,
                start_interval_secs=0,
                watermark=app_cfg.watermark,
            )

            if not job_results or not job_results[0].ok:
                error = job_results[0].error if job_results else "download returned no results"
                if _is_skippable_download_error(error):
                    log_info("sync.task.download_skipped", task=task.name, content_id=content_id, reason="members_only")
                    add_entry(history, SyncHistoryEntry(
                        task_name=task.name,
                        content_id=content_id,
                        site="",
                        author="",
                        desc="",
                        output_path="",
                        status="skipped_unavailable",
                        error=error,
                    ))
                    return SyncTaskResult(
                        task_name=task.name,
                        ok=True,
                        content_id=content_id,
                        action="skipped_unavailable",
                        error=error,
                    )
                log_warn("sync.task.download_failed", task=task.name, error=error)
                add_entry(history, SyncHistoryEntry(
                    task_name=task.name, content_id=content_id,
                    site="", author="", desc="", output_path="", status="download_failed",
                    error=error,
                ))
                return SyncTaskResult(task_name=task.name, ok=False, content_id=content_id, action="failed", error=error)

            dl = job_results[0]
            meta = dl.extraction.metadata if dl.extraction else None
            output_path = dl.artifact.output_path if dl.artifact else None
            site = meta.site if meta else ""
            author = meta.author if meta else ""
            desc = meta.desc if meta else ""
            video_title = meta.title if meta else ""
            actual_content_id = meta.content_id if meta else content_id

        # Publish via configured method. Skill uploads now use author identity globally.
        publish_method = task.publish_method or sync_cfg.publish_method
        template_vars = {"site": site, "author": author, "desc": desc, "title": video_title, "content_id": actual_content_id}
        title = task.title_template.format_map(_SafeFormatMap(template_vars))
        content = task.content_template.format_map(_SafeFormatMap(template_vars))

        if publish_method == "cdp":
            log_info(
                "sync.task.publish",
                task=task.name,
                method=publish_method,
                target="channel",
                guild=task.guild_id,
                channel=task.channel_id,
            )
            from videocp.cdp_publisher import cdp_publish_to_channel
            pub_result = cdp_publish_to_channel(
                browser_config=browser_config,
                video_path=output_path,
                guild_id=task.guild_id,
                title=title,
            )
        else:
            log_info(
                "sync.task.publish",
                task=task.name,
                method=publish_method,
                target="author",
            )
            pub_result = publish_to_channel(
                skill_dir=sync_cfg.skill_dir,
                video_path=output_path,
                guild_id="",
                channel_id="",
                title=title,
                content=content,
                feed_type=task.feed_type,
            )

        if not pub_result.success:
            log_warn("sync.task.publish_failed", task=task.name, error=pub_result.error)
            add_entry(history, SyncHistoryEntry(
                task_name=task.name, content_id=actual_content_id,
                site=site, author=author, desc=desc,
                output_path=str(output_path), status="upload_failed",
                error=pub_result.error,
            ))
            return SyncTaskResult(
                task_name=task.name, ok=False, content_id=actual_content_id,
                action="failed", error=pub_result.error, output_path=str(output_path),
            )

        # Record success
        action = "synced_pinned" if video_input.is_pinned else "synced"
        log_info(
            "sync.task.complete", task=task.name,
            content_id=actual_content_id, feed_id=pub_result.feed_id, pinned=video_input.is_pinned,
        )
        add_entry(history, SyncHistoryEntry(
            task_name=task.name, content_id=actual_content_id,
            site=site, author=author, desc=desc,
            output_path=str(output_path),
            feed_id=pub_result.feed_id, share_url=pub_result.share_url,
            status="ok",
        ))
        return SyncTaskResult(
            task_name=task.name, ok=True, content_id=actual_content_id,
            action=action, feed_id=pub_result.feed_id,
            share_url=pub_result.share_url, output_path=str(output_path),
        )

    except Exception as exc:
        log_warn("sync.task.failed", task=task.name, error=str(exc))
        return SyncTaskResult(task_name=task.name, ok=False, action="failed", error=str(exc))


def _find_existing_download(output_dir: Path, content_id: str) -> dict | None:
    """Check if a video with this content_id was already downloaded (by looking at sidecar JSONs)."""
    for sidecar in output_dir.rglob(f"{content_id}.json"):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        video_path_value = data.get("output_path", "")
        video = Path(video_path_value) if isinstance(video_path_value, str) and video_path_value else sidecar.with_suffix(".mp4")
        if not video.is_absolute():
            video = (sidecar.parent / video).resolve()
        if video.is_file():
            return {
                "output_path": video,
                "site": data.get("site", ""),
                "author": data.get("author", ""),
                "desc": data.get("desc", "") or data.get("title", ""),
                "title": data.get("title", ""),
                "content_id": data.get("content_id", content_id),
            }
    return None


def _extract_content_id(url: str) -> str:
    """Extract a content identifier from a canonical URL."""
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(url)
    # YouTube: ?v=xxx
    v = parse_qs(parsed.query).get("v")
    if v:
        return v[0]
    # Other sites: last meaningful path segment
    parts = parsed.path.rstrip("/").split("/")
    for part in reversed(parts):
        if part and len(part) > 2:
            return part
    return url


def _is_skippable_download_error(error: str) -> bool:
    normalized = (error or "").lower()
    return (
        "members-only" in normalized
        or "member-only" in normalized
        or "会员专享" in normalized
        or "join this channel to get access to members-only content" in normalized
    )


class _SafeFormatMap(dict):
    """Dict that returns the key placeholder for missing keys."""

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"

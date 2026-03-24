from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from videocp.app import (
    DownloadOptions,
    DoctorOptions,
    doctor,
    download_jobs,
    prepare_link_list,
)
from videocp.config import AppConfig, load_app_config
from videocp.errors import VideoCpError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="videocp")
    parser.add_argument("--config", default=None, help="Path to config.yaml.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    download_parser = subparsers.add_parser("download", help="Download videos from supported sites.")
    download_parser.add_argument("inputs", nargs="*", help="URL, short link, or share text.")
    download_parser.add_argument("--input-file", default=None, help="Text file with one URL/share text per line.")
    download_parser.add_argument("--output-dir", default=None, help="Output directory.")
    download_parser.add_argument("--profile-dir", default=None, help="Dedicated Chrome profile directory.")
    download_parser.add_argument("--browser-path", default=None, help="Chrome executable path.")
    download_headless = download_parser.add_mutually_exclusive_group()
    download_headless.add_argument("--headless", dest="headless", action="store_true", help="Run Chrome headless.")
    download_headless.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Run Chrome with a visible window.",
    )
    download_parser.set_defaults(headless=None)
    download_parser.add_argument("--json", action="store_true", help="Print result as JSON.")
    download_parser.add_argument("--timeout-secs", type=int, default=None, help="Timeout in seconds.")
    download_parser.add_argument("--profile-videos-count", type=int, default=None, help="Number of recent videos to download from a profile page.")

    prepare_parser = subparsers.add_parser("prepare-list", help="Resolve inputs and write a canonical URL list.")
    prepare_parser.add_argument("inputs", nargs="*", help="URL, short link, or share text.")
    prepare_parser.add_argument("--input-file", default=None, help="Text file with one URL/share text per line.")
    prepare_parser.add_argument("--output-file", required=True, help="Output txt file path.")
    prepare_parser.add_argument("--timeout-secs", type=int, default=None, help="Timeout in seconds.")
    prepare_parser.add_argument("--json", action="store_true", help="Print result as JSON.")

    doctor_parser = subparsers.add_parser("doctor", help="Check browser, profile, CDP, and ffmpeg.")
    doctor_parser.add_argument("--profile-dir", default=None, help="Dedicated Chrome profile directory.")
    doctor_parser.add_argument("--browser-path", default=None, help="Chrome executable path.")
    doctor_headless = doctor_parser.add_mutually_exclusive_group()
    doctor_headless.add_argument("--headless", dest="headless", action="store_true", help="Run Chrome headless.")
    doctor_headless.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Run Chrome with a visible window.",
    )
    doctor_parser.set_defaults(headless=None)
    doctor_parser.add_argument("--json", action="store_true", help="Print result as JSON.")

    sync_parser = subparsers.add_parser("sync", help="Sync latest videos to QQ channels.")
    sync_parser.add_argument("--tasks-file", default=None, help="Path to tasks.yaml.")
    sync_parser.add_argument("--dry-run", action="store_true", help="Show what would be synced without executing.")
    sync_parser.add_argument("--task-name", default=None, help="Run only the named task.")
    sync_parser.add_argument("--json", action="store_true", help="Print result as JSON.")
    sync_parser.add_argument("--count", type=int, default=None, help="Number of latest videos to sync per task.")
    sync_headless = sync_parser.add_mutually_exclusive_group()
    sync_headless.add_argument("--headless", dest="headless", action="store_true", help="Run Chrome headless.")
    sync_headless.add_argument("--no-headless", dest="headless", action="store_false", help="Run Chrome with a visible window.")
    sync_parser.set_defaults(headless=None)

    return parser


def resolve_cli_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value).expanduser().resolve()


def apply_cli_overrides(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    cli_profile_videos_count = getattr(args, "profile_videos_count", None)
    return AppConfig(
        output_dir=resolve_cli_path(getattr(args, "output_dir", None)) or config.output_dir,
        profile_dir=resolve_cli_path(getattr(args, "profile_dir", None)) or config.profile_dir,
        browser_path=(getattr(args, "browser_path", None) if getattr(args, "browser_path", None) is not None else config.browser_path),
        headless=(getattr(args, "headless", None) if getattr(args, "headless", None) is not None else config.headless),
        timeout_secs=(getattr(args, "timeout_secs", None) if getattr(args, "timeout_secs", None) is not None else config.timeout_secs),
        max_concurrent=config.max_concurrent,
        max_concurrent_per_site=config.max_concurrent_per_site,
        start_interval_secs=config.start_interval_secs,
        watermark=config.watermark,
        profile_videos_count=(cli_profile_videos_count if cli_profile_videos_count is not None else config.profile_videos_count),
        source_path=config.source_path,
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = apply_cli_overrides(
            load_app_config(Path(args.config) if args.config else None, start_dir=Path.cwd()),
            args,
        )
        if args.command == "download":
            results = download_jobs(
                DownloadOptions(
                    raw_inputs=list(args.inputs),
                    input_file=resolve_cli_path(args.input_file),
                    output_dir=config.output_dir,
                    profile_dir=config.profile_dir,
                    browser_path=config.browser_path,
                    headless=config.headless,
                    timeout_secs=config.timeout_secs,
                    max_concurrent=config.max_concurrent,
                    max_concurrent_per_site=config.max_concurrent_per_site,
                    start_interval_secs=config.start_interval_secs,
                    watermark=config.watermark,
                    profile_videos_count=config.profile_videos_count,
                )
            )
            payload = [
                {
                    "ok": item.ok,
                    "raw_input": item.raw_input,
                    "output_path": str(item.artifact.output_path.resolve()) if item.artifact else "",
                    "sidecar_path": str(item.artifact.sidecar_path.resolve()) if item.artifact else "",
                    "chosen_candidate": item.artifact.chosen_candidate.to_dict() if item.artifact else None,
                    "site": item.extraction.metadata.site if item.extraction else (item.parsed_input.provider_key if item.parsed_input else ""),
                    "content_id": item.extraction.metadata.content_id if item.extraction else "",
                    "aweme_id": item.extraction.metadata.aweme_id if item.extraction else "",
                    "author": item.extraction.metadata.author if item.extraction else "",
                    "desc": item.extraction.metadata.desc if item.extraction else "",
                    "error": item.error,
                }
                for item in results
            ]
            if args.json:
                print(json.dumps(payload if len(payload) > 1 else payload[0], ensure_ascii=False, indent=2))
            else:
                for index, item in enumerate(payload):
                    if index:
                        print()
                    title_parts = [part for part in (item["site"], item["author"], item["content_id"]) if part]
                    if item["ok"]:
                        print("Downloaded " + " ".join(title_parts) if title_parts else "Downloaded video")
                        print(f"video: {item['output_path']}")
                        print(f"sidecar: {item['sidecar_path']}")
                    else:
                        print("Failed " + " ".join(title_parts) if title_parts else f"Failed {item['raw_input']}")
                        print(f"error: {item['error']}")
            return 0 if all(item["ok"] for item in payload) else 1

        if args.command == "prepare-list":
            output_file = resolve_cli_path(args.output_file)
            assert output_file is not None
            prepared = prepare_link_list(
                raw_inputs=list(args.inputs),
                input_file=resolve_cli_path(args.input_file),
                output_file=output_file,
                timeout_secs=config.timeout_secs,
            )
            unique_urls = list(dict.fromkeys(item.canonical_url for item in prepared))
            payload = {
                "output_file": str(output_file),
                "count": len(unique_urls),
                "items": unique_urls,
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"Saved link list: {output_file}")
                print(f"count: {payload['count']}")
            return 0

        if args.command == "sync":
            from videocp.config import load_sync_config
            from videocp.sync import SyncOptions, run_sync

            sync_config = load_sync_config(
                tasks_path=Path(args.tasks_file) if args.tasks_file else None,
                start_dir=Path.cwd(),
            )
            results = run_sync(SyncOptions(
                app_config=config,
                sync_config=sync_config,
                dry_run=args.dry_run,
                task_name_filter=args.task_name,
                count_override=args.count,
            ))
            payload = [
                {
                    "task_name": r.task_name,
                    "ok": r.ok,
                    "action": r.action,
                    "content_id": r.content_id,
                    "feed_id": r.feed_id,
                    "share_url": r.share_url,
                    "output_path": r.output_path,
                    "error": r.error,
                }
                for r in results
            ]
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                for r in results:
                    status = r.action
                    if r.ok:
                        if r.action == "synced":
                            print(f"[synced] {r.task_name}: {r.content_id}")
                            if r.share_url:
                                print(f"  share: {r.share_url}")
                            print(f"  video: {r.output_path}")
                        elif r.action == "skipped":
                            print(f"[skipped] {r.task_name}: {r.content_id} (already synced)")
                        elif r.action == "skipped_unavailable":
                            print(f"[skipped] {r.task_name}: {r.content_id} (unavailable source)")
                        elif r.action == "no_new_video":
                            print(f"[no_new] {r.task_name}: no new video found")
                        elif r.action == "dry_run":
                            print(f"[dry_run] {r.task_name}: would sync {r.content_id}")
                    else:
                        print(f"[failed] {r.task_name}: {r.error}")
            return 0 if all(r.ok for r in results) else 1

        checks = doctor(
            DoctorOptions(
                profile_dir=config.profile_dir,
                browser_path=config.browser_path,
                headless=config.headless,
            )
        )
        payload = [check.to_dict() for check in checks]
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for check in checks:
                status = "ok" if check.ok else "fail"
                print(f"[{status}] {check.name}: {check.detail}")
        return 0 if all(check.ok for check in checks if check.name != "ffmpeg") else 1
    except (VideoCpError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

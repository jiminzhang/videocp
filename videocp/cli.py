from __future__ import annotations

import argparse
import json
from pathlib import Path

from videocp.app import DownloadOptions, DoctorOptions, doctor, download_videos
from videocp.errors import VideoCpError
from videocp.profile import default_profile_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="videocp")
    subparsers = parser.add_subparsers(dest="command", required=True)

    download_parser = subparsers.add_parser("download", help="Download a Douyin video.")
    download_parser.add_argument("inputs", nargs="+", help="Douyin URL, short link, or share text.")
    download_parser.add_argument("--output-dir", default="downloads", help="Output directory.")
    download_parser.add_argument(
        "--profile-dir",
        default=str(default_profile_dir()),
        help="Dedicated Chrome profile directory.",
    )
    download_parser.add_argument("--browser-path", default="", help="Chrome executable path.")
    download_parser.add_argument("--headless", action="store_true", help="Run Chrome headless.")
    download_parser.add_argument("--json", action="store_true", help="Print result as JSON.")
    download_parser.add_argument("--timeout-secs", type=int, default=30, help="Timeout in seconds.")

    doctor_parser = subparsers.add_parser("doctor", help="Check browser, profile, CDP, and ffmpeg.")
    doctor_parser.add_argument(
        "--profile-dir",
        default=str(default_profile_dir()),
        help="Dedicated Chrome profile directory.",
    )
    doctor_parser.add_argument("--browser-path", default="", help="Chrome executable path.")
    doctor_parser.add_argument("--headless", action="store_true", help="Run Chrome headless.")
    doctor_parser.add_argument("--json", action="store_true", help="Print result as JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "download":
            results = download_videos(
                DownloadOptions(
                    raw_inputs=list(args.inputs),
                    output_dir=Path(args.output_dir),
                    profile_dir=Path(args.profile_dir),
                    browser_path=args.browser_path,
                    headless=args.headless,
                    timeout_secs=args.timeout_secs,
                )
            )
            payload = [
                {
                    "output_path": str(artifact.output_path),
                    "sidecar_path": str(artifact.sidecar_path),
                    "chosen_candidate": artifact.chosen_candidate.to_dict(),
                    "aweme_id": extraction.metadata.aweme_id,
                    "author": extraction.metadata.author,
                    "desc": extraction.metadata.desc,
                }
                for extraction, artifact in results
            ]
            if args.json:
                print(json.dumps(payload if len(payload) > 1 else payload[0], ensure_ascii=False, indent=2))
            else:
                for item in payload:
                    print(f"Saved video: {item['output_path']}")
                    print(f"Saved sidecar: {item['sidecar_path']}")
            return 0

        checks = doctor(
            DoctorOptions(
                profile_dir=Path(args.profile_dir),
                browser_path=args.browser_path,
                headless=args.headless,
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
        print(f"error: {exc}")
        return 1

import time
from pathlib import Path
from threading import Lock

from videocp.app import DownloadOptions, StartIntervalGate, dedupe_prepared_inputs, download_jobs, prepare_link_list, read_input_file
from videocp.models import DownloadArtifact, ExtractionResult, MediaCandidate, MediaKind, ParsedInput, TrackType, VideoMetadata, WatermarkMode


def test_read_input_file_ignores_comments_and_blank_lines(tmp_path: Path):
    input_file = tmp_path / "links.txt"
    input_file.write_text(
        "\n".join(
            [
                "",
                "# comment",
                "https://www.douyin.com/video/1",
                "  ",
                "https://www.bilibili.com/video/BV1764y1y76G/",
                "",
            ]
        ),
        encoding="utf-8",
    )

    items = read_input_file(input_file)

    assert items == [
        "https://www.douyin.com/video/1",
        "https://www.bilibili.com/video/BV1764y1y76G/",
    ]


def test_prepare_link_list_writes_deduplicated_canonical_urls(tmp_path: Path, monkeypatch):
    def fake_parse_input(raw_input: str, timeout_secs: int = 15) -> ParsedInput:
        return ParsedInput(
            raw_input=raw_input,
            extracted_url=raw_input,
            canonical_url=f"https://example.com/{raw_input.split('/')[-1]}",
            provider_key="douyin",
        )

    monkeypatch.setattr("videocp.app.parse_input", fake_parse_input)
    output_file = tmp_path / "batch.txt"

    prepared = prepare_link_list(
        raw_inputs=["https://a/1", "https://b/1", "https://c/2"],
        input_file=None,
        output_file=output_file,
        timeout_secs=10,
    )

    assert len(prepared) == 3
    assert output_file.read_text(encoding="utf-8") == "https://example.com/1\nhttps://example.com/2\n"


def test_dedupe_prepared_inputs_keeps_first_canonical_url():
    prepared = [
        ParsedInput(
            raw_input="https://a/1",
            extracted_url="https://a/1",
            canonical_url="https://example.com/1",
            provider_key="douyin",
        ),
        ParsedInput(
            raw_input="https://b/1",
            extracted_url="https://b/1",
            canonical_url="https://example.com/1",
            provider_key="douyin",
        ),
        ParsedInput(
            raw_input="https://c/2",
            extracted_url="https://c/2",
            canonical_url="https://example.com/2",
            provider_key="douyin",
        ),
    ]

    unique = dedupe_prepared_inputs(prepared)

    assert [item.raw_input for item in unique] == ["https://a/1", "https://c/2"]


def test_start_interval_gate_enforces_spacing():
    gate = StartIntervalGate(0.03)
    started = time.monotonic()
    gate.wait()
    gate.wait()
    gate.wait()
    elapsed = time.monotonic() - started
    assert elapsed >= 0.05


def test_download_jobs_respects_per_site_limit(tmp_path: Path, monkeypatch):
    site_map = {
        "job-1": "douyin",
        "job-2": "douyin",
        "job-3": "bilibili",
        "job-4": "bilibili",
    }

    def fake_parse_input(raw_input: str, timeout_secs: int = 15) -> ParsedInput:
        provider_key = site_map[raw_input]
        return ParsedInput(
            raw_input=raw_input,
            extracted_url=raw_input,
            canonical_url=f"https://example.com/{provider_key}/{raw_input}",
            provider_key=provider_key,
        )

    active_by_site: dict[str, int] = {"douyin": 0, "bilibili": 0}
    peak_by_site: dict[str, int] = {"douyin": 0, "bilibili": 0}
    active_total = 0
    peak_total = 0
    guard = Lock()

    def fake_download_prepared_input(parsed, browser_config, timeout_secs):
        metadata = VideoMetadata(
            source_url=parsed.raw_input,
            site=parsed.provider_key,
            canonical_url=parsed.canonical_url,
            aweme_id=parsed.raw_input,
            author=parsed.provider_key,
            desc=parsed.raw_input,
        )
        candidate = MediaCandidate(
            url="https://example.com/video.mp4",
            kind=MediaKind.MP4,
            track_type=TrackType.MUXED,
            watermark_mode=WatermarkMode.NO_WATERMARK,
            source="dom",
            observed_via="dom",
        )
        extraction = ExtractionResult(
            metadata=metadata,
            candidates=[candidate],
            cookies=[],
            user_agent="ua",
            diagnostics={},
        )
        return extraction

    def fake_download_extraction_artifact(extraction, output_dir, timeout_secs, watermark=None):
        nonlocal active_total, peak_total
        with guard:
            active_total += 1
            active_by_site[extraction.metadata.site] += 1
            peak_total = max(peak_total, active_total)
            peak_by_site[extraction.metadata.site] = max(peak_by_site[extraction.metadata.site], active_by_site[extraction.metadata.site])
        time.sleep(0.05)
        with guard:
            active_total -= 1
            active_by_site[extraction.metadata.site] -= 1

        parsed_input = extraction.metadata.aweme_id
        candidate = extraction.candidates[0]
        output_path = output_dir / f"{parsed_input}.mp4"
        sidecar_path = output_dir / f"{parsed_input}.json"
        output_path.write_bytes(b"ok")
        sidecar_path.write_text("{}", encoding="utf-8")
        artifact = DownloadArtifact(
            output_path=output_path,
            sidecar_path=sidecar_path,
            chosen_candidate=candidate,
            attempts=[],
        )
        return artifact

    def fake_download_bilibili_with_bbdown(
        *,
        source_url,
        browser_config,
        output_dir,
        timeout_secs,
        watermark,
        author_hint,
        metadata_seed=None,
    ):
        nonlocal active_total, peak_total
        with guard:
            active_total += 1
            active_by_site["bilibili"] += 1
            peak_total = max(peak_total, active_total)
            peak_by_site["bilibili"] = max(peak_by_site["bilibili"], active_by_site["bilibili"])
        time.sleep(0.05)
        with guard:
            active_total -= 1
            active_by_site["bilibili"] -= 1

        candidate = MediaCandidate(
            url=source_url,
            kind=MediaKind.MP4,
            track_type=TrackType.MUXED,
            watermark_mode=WatermarkMode.NO_WATERMARK,
            source="bbdown",
            observed_via="bbdown",
        )
        metadata = metadata_seed or VideoMetadata(
            source_url=source_url,
            site="bilibili",
            canonical_url=source_url,
            aweme_id="bilibili-job",
            author=author_hint or "bilibili",
            desc=source_url,
        )
        output_path = output_dir / f"{metadata.aweme_id}.mp4"
        sidecar_path = output_dir / f"{metadata.aweme_id}.json"
        output_path.write_bytes(b"ok")
        sidecar_path.write_text("{}", encoding="utf-8")
        artifact = DownloadArtifact(
            output_path=output_path,
            sidecar_path=sidecar_path,
            chosen_candidate=candidate,
            attempts=[],
        )
        extraction = ExtractionResult(
            metadata=metadata,
            candidates=[candidate],
            cookies=[],
            user_agent="",
            diagnostics={"downloader": "bbdown"},
        )
        return extraction, artifact

    monkeypatch.setattr("videocp.app.parse_input", fake_parse_input)
    monkeypatch.setattr("videocp.app.detect_system_browser_executable", lambda: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    monkeypatch.setattr("videocp.app._download_prepared_input", fake_download_prepared_input)
    monkeypatch.setattr("videocp.app._download_extraction_artifact", fake_download_extraction_artifact)
    monkeypatch.setattr("videocp.app.download_bilibili_with_bbdown", fake_download_bilibili_with_bbdown)

    results = download_jobs(
        DownloadOptions(
            raw_inputs=["job-1", "job-2", "job-3", "job-4"],
            output_dir=tmp_path,
            profile_dir=tmp_path / "profile",
            browser_path="",
            headless=False,
            timeout_secs=10,
            max_concurrent=4,
            max_concurrent_per_site=1,
            start_interval_secs=0,
        )
    )

    assert all(item.ok for item in results)
    assert peak_by_site["douyin"] == 1
    assert peak_by_site["bilibili"] == 1
    assert peak_total == 2


def test_download_jobs_runs_extraction_concurrently(tmp_path: Path, monkeypatch):
    def fake_parse_input(raw_input: str, timeout_secs: int = 15) -> ParsedInput:
        return ParsedInput(
            raw_input=raw_input,
            extracted_url=raw_input,
            canonical_url=f"https://example.com/douyin/{raw_input}",
            provider_key="douyin",
        )

    active_extract = 0
    peak_extract = 0
    guard = Lock()

    def fake_download_prepared_input(parsed, browser_config, timeout_secs):
        nonlocal active_extract, peak_extract
        with guard:
            active_extract += 1
            peak_extract = max(peak_extract, active_extract)
        time.sleep(0.05)
        with guard:
            active_extract -= 1
        metadata = VideoMetadata(
            source_url=parsed.raw_input,
            site=parsed.provider_key,
            canonical_url=parsed.canonical_url,
            aweme_id=parsed.raw_input,
            author="author",
            desc=parsed.raw_input,
        )
        candidate = MediaCandidate(
            url="https://example.com/video.mp4",
            kind=MediaKind.MP4,
            track_type=TrackType.MUXED,
            watermark_mode=WatermarkMode.NO_WATERMARK,
            source="dom",
            observed_via="dom",
        )
        return ExtractionResult(
            metadata=metadata,
            candidates=[candidate],
            cookies=[],
            user_agent="ua",
            diagnostics={},
        )

    def fake_download_extraction_artifact(extraction, output_dir, timeout_secs, watermark=None):
        output_path = output_dir / f"{extraction.metadata.aweme_id}.mp4"
        sidecar_path = output_dir / f"{extraction.metadata.aweme_id}.json"
        output_path.write_bytes(b"ok")
        sidecar_path.write_text("{}", encoding="utf-8")
        return DownloadArtifact(
            output_path=output_path,
            sidecar_path=sidecar_path,
            chosen_candidate=extraction.candidates[0],
            attempts=[],
        )

    monkeypatch.setattr("videocp.app.parse_input", fake_parse_input)
    monkeypatch.setattr("videocp.app.detect_system_browser_executable", lambda: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    monkeypatch.setattr("videocp.app._download_prepared_input", fake_download_prepared_input)
    monkeypatch.setattr("videocp.app._download_extraction_artifact", fake_download_extraction_artifact)

    results = download_jobs(
        DownloadOptions(
            raw_inputs=["job-1", "job-2"],
            output_dir=tmp_path,
            profile_dir=tmp_path / "profile",
            browser_path="",
            headless=False,
            timeout_secs=10,
            max_concurrent=2,
            max_concurrent_per_site=2,
            start_interval_secs=0,
        )
    )

    assert all(item.ok for item in results)
    assert peak_extract == 2


def test_download_jobs_deduplicates_same_canonical_url(tmp_path: Path, monkeypatch):
    def fake_parse_input(raw_input: str, timeout_secs: int = 15) -> ParsedInput:
        canonical = "https://example.com/douyin/1" if raw_input in {"job-1", "job-1-duplicate"} else "https://example.com/douyin/2"
        return ParsedInput(
            raw_input=raw_input,
            extracted_url=raw_input,
            canonical_url=canonical,
            provider_key="douyin",
        )

    extracted_raw_inputs: list[str] = []

    def fake_download_prepared_input(parsed, browser_config, timeout_secs):
        extracted_raw_inputs.append(parsed.raw_input)
        metadata = VideoMetadata(
            source_url=parsed.raw_input,
            site=parsed.provider_key,
            canonical_url=parsed.canonical_url,
            aweme_id=parsed.raw_input,
            author="author",
            desc=parsed.raw_input,
        )
        candidate = MediaCandidate(
            url="https://example.com/video.mp4",
            kind=MediaKind.MP4,
            track_type=TrackType.MUXED,
            watermark_mode=WatermarkMode.NO_WATERMARK,
            source="dom",
            observed_via="dom",
        )
        return ExtractionResult(
            metadata=metadata,
            candidates=[candidate],
            cookies=[],
            user_agent="ua",
            diagnostics={},
        )

    def fake_download_extraction_artifact(extraction, output_dir, timeout_secs, watermark=None):
        output_path = output_dir / f"{extraction.metadata.aweme_id}.mp4"
        sidecar_path = output_dir / f"{extraction.metadata.aweme_id}.json"
        output_path.write_bytes(b"ok")
        sidecar_path.write_text("{}", encoding="utf-8")
        return DownloadArtifact(
            output_path=output_path,
            sidecar_path=sidecar_path,
            chosen_candidate=extraction.candidates[0],
            attempts=[],
        )

    monkeypatch.setattr("videocp.app.parse_input", fake_parse_input)
    monkeypatch.setattr("videocp.app.detect_system_browser_executable", lambda: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    monkeypatch.setattr("videocp.app._download_prepared_input", fake_download_prepared_input)
    monkeypatch.setattr("videocp.app._download_extraction_artifact", fake_download_extraction_artifact)

    results = download_jobs(
        DownloadOptions(
            raw_inputs=["job-1", "job-1-duplicate", "job-2"],
            output_dir=tmp_path,
            profile_dir=tmp_path / "profile",
            browser_path="",
            headless=False,
            timeout_secs=10,
            max_concurrent=2,
            max_concurrent_per_site=2,
            start_interval_secs=0,
        )
    )

    assert all(item.ok for item in results)
    assert extracted_raw_inputs == ["job-1", "job-2"]
    assert len(results) == 2


def test_download_jobs_uses_bbdown_for_bilibili(tmp_path: Path, monkeypatch):
    def fake_parse_input(raw_input: str, timeout_secs: int = 15) -> ParsedInput:
        return ParsedInput(
            raw_input=raw_input,
            extracted_url=raw_input,
            canonical_url="https://www.bilibili.com/video/BV1764y1y76G/?p=2",
            provider_key="bilibili",
        )

    def fake_download_prepared_input(parsed, browser_config, timeout_secs):
        metadata = VideoMetadata(
            source_url=parsed.canonical_url,
            site="bilibili",
            canonical_url=parsed.canonical_url,
            page_url=parsed.canonical_url,
            aweme_id="BV1764y1y76G",
            author="元数据作者",
            desc="元数据简介",
            title="元数据标题",
        )
        candidate = MediaCandidate(
            url="https://example.com/video.mp4",
            kind=MediaKind.MP4,
            track_type=TrackType.MUXED,
            watermark_mode=WatermarkMode.NO_WATERMARK,
            source="dom",
            observed_via="dom",
        )
        return ExtractionResult(
            metadata=metadata,
            candidates=[candidate],
            cookies=[],
            user_agent="ua",
            diagnostics={},
        )

    def fake_download_bilibili_with_bbdown(
        *,
        source_url,
        browser_config,
        output_dir,
        timeout_secs,
        watermark,
        author_hint,
        metadata_seed=None,
    ):
        output_path = output_dir / "BV1764y1y76G.mp4"
        sidecar_path = output_dir / "BV1764y1y76G.json"
        output_path.write_bytes(b"ok")
        sidecar_path.write_text("{}", encoding="utf-8")
        candidate = MediaCandidate(
            url=source_url,
            kind=MediaKind.MP4,
            track_type=TrackType.MUXED,
            watermark_mode=WatermarkMode.NO_WATERMARK,
            source="bbdown",
            observed_via="bbdown",
        )
        extraction = ExtractionResult(
            metadata=VideoMetadata(
                source_url=source_url,
                site="bilibili",
                canonical_url=source_url,
                page_url=source_url,
                aweme_id="BV1764y1y76G",
                author=author_hint or "元数据作者",
                desc="元数据简介",
                title="元数据标题",
            ),
            candidates=[candidate],
            cookies=[],
            user_agent="",
            diagnostics={"downloader": "bbdown"},
        )
        artifact = DownloadArtifact(
            output_path=output_path,
            sidecar_path=sidecar_path,
            chosen_candidate=candidate,
            attempts=[{"url": source_url, "mode": "bbdown_tv", "status": "ok"}],
        )
        return extraction, artifact

    monkeypatch.setattr("videocp.app.parse_input", fake_parse_input)
    monkeypatch.setattr("videocp.app.detect_system_browser_executable", lambda: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    monkeypatch.setattr("videocp.app._download_prepared_input", fake_download_prepared_input)
    monkeypatch.setattr("videocp.app.download_bilibili_with_bbdown", fake_download_bilibili_with_bbdown)

    results = download_jobs(
        DownloadOptions(
            raw_inputs=["job-1"],
            output_dir=tmp_path,
            profile_dir=tmp_path / "profile",
            browser_path="",
            headless=False,
            timeout_secs=10,
            max_concurrent=1,
            max_concurrent_per_site=1,
            start_interval_secs=0,
        )
    )

    assert len(results) == 1
    assert results[0].ok
    assert results[0].extraction.diagnostics["downloader"] == "bbdown"

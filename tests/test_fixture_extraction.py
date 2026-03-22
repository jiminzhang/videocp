import json
from pathlib import Path

from videocp.extractor import ExtractionAccumulator, sort_candidates
from videocp.models import ObservedEvent, VideoMetadata


def test_fixture_events_extract_metadata_and_candidates():
    fixture_path = Path(__file__).parent / "fixtures" / "network_events.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    accumulator = ExtractionAccumulator(
        metadata=VideoMetadata(source_url="https://www.douyin.com/video/1234567890")
    )
    for item in payload:
        accumulator.ingest_event(
            ObservedEvent(
                url=item["url"],
                content_type=item.get("content_type", ""),
                json_body=item.get("json_body"),
                origin=item.get("origin", "response"),
            )
        )
    sorted_candidates = sort_candidates(accumulator.candidates)
    assert accumulator.metadata.aweme_id == "1234567890"
    assert accumulator.metadata.author == "fixture_author"
    assert accumulator.metadata.desc == "fixture description"
    assert sorted_candidates[0].watermark_mode.value == "no_watermark"
    assert sorted_candidates[0].kind.value == "mp4"


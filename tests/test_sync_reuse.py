import json
from pathlib import Path

from videocp.sync import _find_existing_download


def test_find_existing_download_uses_output_path_from_sidecar(tmp_path: Path):
    output_dir = tmp_path / "downloads"
    target_dir = output_dir / "bilibili-author"
    target_dir.mkdir(parents=True, exist_ok=True)
    video_path = target_dir / "BV1764y1y76G.mkv"
    sidecar_path = target_dir / "BV1764y1y76G.json"
    video_path.write_bytes(b"video")
    sidecar_path.write_text(
        json.dumps(
            {
                "site": "bilibili",
                "author": "作者",
                "desc": "简介",
                "content_id": "BV1764y1y76G",
                "output_path": str(video_path),
            }
        ),
        encoding="utf-8",
    )

    found = _find_existing_download(output_dir, "BV1764y1y76G")

    assert found is not None
    assert found["output_path"] == video_path
    assert found["author"] == "作者"
    assert found["desc"] == "简介"

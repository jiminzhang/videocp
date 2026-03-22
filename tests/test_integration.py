import os
from pathlib import Path

import pytest

from videocp.app import DownloadOptions, download_video
from videocp.profile import default_profile_dir


@pytest.mark.integration
def test_live_public_download(tmp_path: Path):
    if os.getenv("VIDEOCP_RUN_LIVE") != "1":
        pytest.skip("Set VIDEOCP_RUN_LIVE=1 to run live integration tests.")
    public_url = os.getenv("VIDEOCP_PUBLIC_URL")
    if not public_url:
        pytest.skip("Set VIDEOCP_PUBLIC_URL to a public Douyin video URL.")
    extraction, artifact = download_video(
        DownloadOptions(
            raw_inputs=[public_url],
            output_dir=tmp_path,
            profile_dir=default_profile_dir(),
            browser_path="",
            headless=False,
            timeout_secs=45,
        )
    )
    assert extraction.candidates
    assert artifact.output_path.exists()


@pytest.mark.integration
def test_live_login_visible_download(tmp_path: Path):
    if os.getenv("VIDEOCP_RUN_LIVE") != "1":
        pytest.skip("Set VIDEOCP_RUN_LIVE=1 to run live integration tests.")
    login_url = os.getenv("VIDEOCP_LOGIN_URL")
    if not login_url:
        pytest.skip("Set VIDEOCP_LOGIN_URL to a login-visible Douyin video URL.")
    extraction, artifact = download_video(
        DownloadOptions(
            raw_inputs=[login_url],
            output_dir=tmp_path,
            profile_dir=default_profile_dir(),
            browser_path="",
            headless=False,
            timeout_secs=45,
        )
    )
    assert extraction.candidates
    assert artifact.output_path.exists()

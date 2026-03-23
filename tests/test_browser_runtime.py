from videocp.browser import (
    BrowserConfig,
    BrowserSession,
    close_global_browser,
    discover_running_browser_cdp_url,
    get_global_browser,
    merge_node_options,
)


def test_merge_node_options_deduplicates_flags():
    merged = merge_node_options("--trace-warnings", ["--no-deprecation", "--trace-warnings"])
    assert merged.split() == ["--trace-warnings", "--no-deprecation"]


def test_browser_config_prefers_existing_devtools_active_port(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "DevToolsActivePort").write_text("9311\n/devtools/browser/test\n", encoding="utf-8")

    config = BrowserConfig(profile_dir=profile_dir, browser_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

    assert config.cdp_url == "http://127.0.0.1:9311"


def test_discover_running_browser_cdp_url_from_process_list(monkeypatch, tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    class FakeCompletedProcess:
        def __init__(self, stdout: str):
            self.stdout = stdout
            self.returncode = 0

    def fake_run(args, capture_output, text, check):
        return FakeCompletedProcess(
            "\n".join(
                [
                    " 101 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/other",
                    f" 205 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9333 --user-data-dir={profile_dir}",
                    f" 309 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9444 --user-data-dir={profile_dir}",
                ]
            )
        )

    monkeypatch.setattr("videocp.browser.subprocess.run", fake_run)

    assert discover_running_browser_cdp_url(profile_dir) == "http://127.0.0.1:9444"


def test_browser_config_prefers_running_browser_process(monkeypatch, tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    monkeypatch.setattr("videocp.browser.discover_running_browser_cdp_url", lambda _: "http://127.0.0.1:9555")

    config = BrowserConfig(profile_dir=profile_dir, browser_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

    assert config.cdp_url == "http://127.0.0.1:9555"


def test_browser_session_close_keeps_launched_browser_when_requested(tmp_path):
    class FakePlaywright:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    class FakeProc:
        def __init__(self):
            self.pid = 123
            self.terminated = False
            self.killed = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            return 0

        def kill(self):
            self.killed = True

    session = BrowserSession(
        BrowserConfig(
            profile_dir=tmp_path / "profile",
            browser_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            cdp_url="http://127.0.0.1:9222",
        ),
        terminate_on_close=False,
    )
    playwright = FakePlaywright()
    proc = FakeProc()
    session.playwright = playwright
    session.launched_proc = proc

    session.close()

    assert session.playwright is None
    assert session.launched_proc is None
    assert playwright.stopped is True
    assert proc.terminated is False
    assert proc.killed is False


def test_get_global_browser_closes_bootstrap_playwright_session(monkeypatch, tmp_path):
    class FakeProc:
        def __init__(self):
            self.pid = 456

        def poll(self):
            return None

    instances = []

    class FakeSession:
        def __init__(self, config, *, prepare_seed=True, terminate_on_close=True):
            self.config = config
            self.prepare_seed = prepare_seed
            self.terminate_on_close = terminate_on_close
            self.launched_proc = FakeProc()
            self.seed_status = "already_seeded"
            self.seed_source = "/tmp/source"
            self.runtime_mode = "cdp_detached"
            self.closed = False
            instances.append(self)

        def open(self):
            return self

        def close(self):
            self.closed = True

    monkeypatch.setattr("videocp.browser.BrowserSession", FakeSession)
    close_global_browser()

    runtime = get_global_browser(
        BrowserConfig(
            profile_dir=tmp_path / "profile",
            browser_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            cdp_url="http://127.0.0.1:9222",
        )
    )

    assert len(instances) == 1
    assert instances[0].closed is True
    assert runtime.runtime_mode == "cdp_detached"
    assert runtime.launched_proc is not None

    close_global_browser()

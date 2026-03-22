from __future__ import annotations

import atexit
import os
import socket
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from videocp.profile import (
    clear_profile_transient_artifacts,
    prepare_profile_seed_once,
    profile_lock_hint,
)

DETACHED_CONNECT_WAIT_SECONDS = 20.0
_GLOBAL_BROWSER_LOCK = threading.Lock()
_GLOBAL_BROWSER: BrowserSession | None = None


@dataclass(slots=True)
class BrowserConfig:
    profile_dir: Path
    browser_path: str
    cdp_url: str = ""
    headless: bool = False
    launch_args: list[str] = field(
        default_factory=lambda: [
            "--window-size=1440,960",
        ]
    )

    def __post_init__(self) -> None:
        if not self.cdp_url:
            self.cdp_url = build_cdp_url(find_free_local_port())


@contextmanager
def local_no_proxy() -> Iterator[None]:
    original = {key: os.environ.get(key) for key in ("NO_PROXY", "no_proxy")}
    try:
        values = [item for item in [original.get("NO_PROXY"), original.get("no_proxy")] if item]
        merged = ",".join(values)
        parts = [piece.strip() for piece in merged.split(",") if piece.strip()]
        for fixed in ("127.0.0.1", "localhost"):
            if fixed not in parts:
                parts.append(fixed)
        merged = ",".join(parts)
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def parse_cdp_url(cdp_url: str):
    parsed = urlparse(cdp_url)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError(f"Invalid CDP URL: {cdp_url}")
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("CDP URL host must be localhost or 127.0.0.1")
    if parsed.port is None:
        raise RuntimeError(f"CDP URL must include port: {cdp_url}")
    return parsed


def find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_cdp_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def format_exception(exc: Exception) -> str:
    text = str(exc).strip()
    return text or f"{type(exc).__name__}(no message)"


def try_connect_cdp(playwright: Playwright, cdp_url: str) -> tuple[Browser | None, str]:
    try:
        with local_no_proxy():
            return playwright.chromium.connect_over_cdp(cdp_url), ""
    except Exception as exc:
        return None, format_exception(exc)


def wait_for_cdp(playwright: Playwright, cdp_url: str, wait_seconds: float) -> tuple[Browser | None, str]:
    deadline = time.monotonic() + max(0.1, wait_seconds)
    last_error = ""
    while time.monotonic() < deadline:
        browser, error = try_connect_cdp(playwright, cdp_url)
        if browser is not None:
            return browser, ""
        last_error = error
        time.sleep(0.2)
    return None, last_error or "timeout"


def launch_detached_browser_process(
    playwright: Playwright,
    config: BrowserConfig,
) -> subprocess.Popen[str]:
    parsed = parse_cdp_url(config.cdp_url)
    executable = config.browser_path.strip() or playwright.chromium.executable_path
    clear_profile_transient_artifacts(config.profile_dir)
    chrome_args = [
        f"--remote-debugging-port={parsed.port}",
        f"--user-data-dir={config.profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if config.headless:
        chrome_args.append("--headless=new")
    chrome_args.extend(config.launch_args)
    chrome_args.append("about:blank")
    args = [executable, *chrome_args]
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def collect_launch_diagnostics(proc: subprocess.Popen[str]) -> str:
    rc = proc.poll()
    if rc is None:
        return f"pid={proc.pid},status=running"
    stderr_text = ""
    try:
        _, stderr_text = proc.communicate(timeout=1)
    except Exception:
        stderr_text = ""
    stderr_text = " ".join(stderr_text.split())
    if len(stderr_text) > 260:
        stderr_text = f"{stderr_text[:260]}...(truncated)"
    if stderr_text:
        return f"pid={proc.pid},status=exited,code={rc},stderr={stderr_text}"
    return f"pid={proc.pid},status=exited,code={rc}"


def probe_cdp_endpoint(cdp_url: str) -> dict[str, object]:
    parsed = parse_cdp_url(cdp_url)
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 0)
    tcp_ok = False
    tcp_error = ""
    try:
        with socket.create_connection((host, port), timeout=0.8):
            tcp_ok = True
    except Exception as exc:
        tcp_error = format_exception(exc)

    http_ok = False
    http_error = ""
    http_status = 0
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{cdp_url}/json/version", timeout=0.8) as response:
            http_status = int(getattr(response, "status", 0) or 0)
            http_ok = response.status == 200
    except Exception as exc:
        http_error = format_exception(exc)
    return {
        "tcp_ok": tcp_ok,
        "tcp_error": tcp_error or "none",
        "http_ok": http_ok,
        "http_status": http_status,
        "http_error": http_error or "none",
    }


class BrowserSession:
    def __init__(self, config: BrowserConfig):
        self.config = config
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.launched_proc: subprocess.Popen[str] | None = None
        self.seed_status = "not_run"
        self.seed_source = ""
        self.runtime_mode = ""

    def __enter__(self) -> "BrowserSession":
        return self.open()

    def open(self) -> "BrowserSession":
        if self.context is not None:
            return self
        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        self.seed_status, self.seed_source = prepare_profile_seed_once(
            self.config.profile_dir,
            self.config.browser_path,
        )
        with local_no_proxy():
            self.playwright = sync_playwright().start()
        self.browser, self.context = self._connect_or_launch()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _connect_or_launch(self) -> tuple[Browser, BrowserContext]:
        assert self.playwright is not None
        parse_cdp_url(self.config.cdp_url)
        browser, pre_error = try_connect_cdp(self.playwright, self.config.cdp_url)
        if browser is not None:
            contexts = list(browser.contexts)
            context = contexts[0] if contexts else browser.new_context()
            self.runtime_mode = "cdp_existing"
            return browser, context

        self.launched_proc = launch_detached_browser_process(self.playwright, self.config)
        browser, wait_error = wait_for_cdp(
            self.playwright,
            self.config.cdp_url,
            DETACHED_CONNECT_WAIT_SECONDS,
        )
        if browser is not None:
            contexts = list(browser.contexts)
            context = contexts[0] if contexts else browser.new_context()
            self.runtime_mode = "cdp_detached"
            return browser, context

        probe = probe_cdp_endpoint(self.config.cdp_url)
        if probe["tcp_ok"] and probe["http_ok"]:
            browser, wait_error_2 = wait_for_cdp(
                self.playwright,
                self.config.cdp_url,
                6.0,
            )
            if browser is not None:
                contexts = list(browser.contexts)
                context = contexts[0] if contexts else browser.new_context()
                self.runtime_mode = "cdp_detached"
                return browser, context
            if wait_error_2:
                wait_error = wait_error_2
        launch_diag = collect_launch_diagnostics(self.launched_proc)
        raise RuntimeError(
            "Failed to connect detached browser over CDP. "
            f"{profile_lock_hint(self.config.profile_dir)} "
            f"pre_connect_error={pre_error}; "
            f"post_launch_connect_error={wait_error}; "
            f"launch={launch_diag}; probe={probe}; "
        )

    def new_page(self) -> Page:
        if self.context is None:
            raise RuntimeError("Browser context not initialized.")
        return self.context.new_page()

    def get_cookies(self) -> list[dict]:
        if self.context is None:
            return []
        return self.context.cookies()

    def get_user_agent(self, page: Page) -> str:
        return page.evaluate("() => navigator.userAgent")

    def close(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        if self.launched_proc is not None and self.launched_proc.poll() is None:
            self.launched_proc.terminate()
            try:
                self.launched_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.launched_proc.kill()
            self.launched_proc = None


def _same_browser_config(left: BrowserConfig, right: BrowserConfig) -> bool:
    return (
        left.profile_dir == right.profile_dir
        and left.browser_path == right.browser_path
        and left.headless == right.headless
        and left.cdp_url == right.cdp_url
        and left.launch_args == right.launch_args
    )


def get_global_browser(config: BrowserConfig) -> BrowserSession:
    global _GLOBAL_BROWSER
    with _GLOBAL_BROWSER_LOCK:
        if _GLOBAL_BROWSER is None:
            _GLOBAL_BROWSER = BrowserSession(config).open()
            atexit.register(close_global_browser)
            return _GLOBAL_BROWSER
        if not _same_browser_config(_GLOBAL_BROWSER.config, config):
            raise RuntimeError("Global Chrome instance already exists with different browser settings.")
        return _GLOBAL_BROWSER


def close_global_browser() -> None:
    global _GLOBAL_BROWSER
    with _GLOBAL_BROWSER_LOCK:
        if _GLOBAL_BROWSER is not None:
            _GLOBAL_BROWSER.close()
            _GLOBAL_BROWSER = None

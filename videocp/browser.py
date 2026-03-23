from __future__ import annotations

import atexit
import os
import re
import shlex
import socket
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from videocp.profile import (
    clear_profile_transient_artifacts,
    prepare_profile_seed_once,
    profile_lock_hint,
)
from videocp.runtime_log import log_info, log_warn

DETACHED_CONNECT_WAIT_SECONDS = 20.0
_GLOBAL_BROWSER_LOCK = threading.Lock()
_GLOBAL_BROWSER: GlobalBrowserRuntime | None = None
DEVTOOLS_ACTIVE_PORT_FILE = "DevToolsActivePort"
PERSISTED_CDP_URL_FILE = ".videocp_cdp_url"


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
            self.cdp_url = (
                discover_running_browser_cdp_url(self.profile_dir)
                or read_persisted_cdp_url(self.profile_dir)
                or read_existing_cdp_url(self.profile_dir)
                or build_cdp_url(find_free_local_port())
            )


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


def merge_node_options(existing: str | None, required: list[str]) -> str:
    parts = shlex.split(existing or "")
    for option in required:
        if option not in parts:
            parts.append(option)
    return " ".join(parts)


@contextmanager
def temporary_node_runtime_env() -> Iterator[None]:
    original_node_options = os.environ.get("NODE_OPTIONS")
    try:
        os.environ["NODE_OPTIONS"] = merge_node_options(original_node_options, ["--no-deprecation"])
        yield
    finally:
        if original_node_options is None:
            os.environ.pop("NODE_OPTIONS", None)
        else:
            os.environ["NODE_OPTIONS"] = original_node_options


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


def persisted_cdp_url_path(profile_dir: Path) -> Path:
    return profile_dir / PERSISTED_CDP_URL_FILE


def read_persisted_cdp_url(profile_dir: Path) -> str:
    marker = persisted_cdp_url_path(profile_dir)
    try:
        return marker.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""


def write_persisted_cdp_url(profile_dir: Path, cdp_url: str) -> None:
    if not cdp_url:
        return
    try:
        persisted_cdp_url_path(profile_dir).write_text(cdp_url, encoding="utf-8")
    except OSError:
        return


def read_existing_cdp_url(profile_dir: Path) -> str:
    active_port_file = profile_dir / DEVTOOLS_ACTIVE_PORT_FILE
    try:
        first_line = active_port_file.read_text(encoding="utf-8").splitlines()[0].strip()
    except (FileNotFoundError, IndexError, OSError, UnicodeDecodeError):
        return ""
    if not first_line.isdigit():
        return ""
    return build_cdp_url(int(first_line))


def discover_running_browser_cdp_url(profile_dir: Path) -> str:
    if os.name != "posix":
        return ""
    try:
        proc = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    needle = f"--user-data-dir={profile_dir}"
    best_pid = -1
    best_port = 0
    for line in proc.stdout.splitlines():
        if needle not in line:
            continue
        match = re.match(r"\s*(\d+)\s+(.*)", line)
        if match is None:
            continue
        pid = int(match.group(1))
        command = match.group(2)
        port_match = re.search(r"--remote-debugging-port=(\d+)", command)
        if port_match is None:
            continue
        port = int(port_match.group(1))
        if pid > best_pid:
            best_pid = pid
            best_port = port
    if best_port <= 0:
        return ""
    return build_cdp_url(best_port)


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
    log_info(
        "browser.launch.start",
        executable=executable,
        profile_dir=config.profile_dir,
        cdp_url=config.cdp_url,
        headless=config.headless,
    )
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


class GlobalBrowserRuntime:
    def __init__(
        self,
        config: BrowserConfig,
        *,
        launched_proc: subprocess.Popen[str] | None = None,
        seed_status: str = "not_run",
        seed_source: str = "",
        runtime_mode: str = "",
    ):
        self.config = config
        self.launched_proc = launched_proc
        self.seed_status = seed_status
        self.seed_source = seed_source
        self.runtime_mode = runtime_mode

    def close(self) -> None:
        if self.launched_proc is not None and self.launched_proc.poll() is None:
            log_info("browser.process.detach", pid=self.launched_proc.pid)
        self.launched_proc = None


class BrowserSession:
    def __init__(
        self,
        config: BrowserConfig,
        *,
        prepare_seed: bool = True,
        terminate_on_close: bool = True,
    ):
        self.config = config
        self.prepare_seed = prepare_seed
        self.terminate_on_close = terminate_on_close
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.launched_proc: subprocess.Popen[str] | None = None
        self.seed_status = "not_run"
        self.seed_source = ""
        self.runtime_mode = ""
        self.created_context = False
        self.owned_pages: list[Page] = []

    def __enter__(self) -> "BrowserSession":
        return self.open()

    def open(self) -> "BrowserSession":
        if self.context is not None:
            return self
        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        log_info(
            "browser.session.open",
            profile_dir=self.config.profile_dir,
            cdp_url=self.config.cdp_url,
            headless=self.config.headless,
        )
        if self.prepare_seed:
            self.seed_status, self.seed_source = prepare_profile_seed_once(
                self.config.profile_dir,
                self.config.browser_path,
            )
            log_info(
                "browser.profile.seed",
                status=self.seed_status,
                source=self.seed_source or "none",
            )
        with local_no_proxy(), temporary_node_runtime_env():
            self.playwright = sync_playwright().start()
        self.browser, self.context = self._connect_or_launch()
        write_persisted_cdp_url(self.config.profile_dir, self.config.cdp_url)
        log_info(
            "browser.session.ready",
            runtime_mode=self.runtime_mode,
            contexts=len(list(self.browser.contexts)) if self.browser is not None else 0,
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _connect_or_launch(self) -> tuple[Browser, BrowserContext]:
        assert self.playwright is not None
        parse_cdp_url(self.config.cdp_url)
        log_info("browser.cdp.connect.start", cdp_url=self.config.cdp_url, mode="reuse_first")
        browser, pre_error = try_connect_cdp(self.playwright, self.config.cdp_url)
        if browser is not None:
            contexts = list(browser.contexts)
            self.created_context = not contexts
            context = contexts[0] if contexts else browser.new_context()
            self.runtime_mode = "cdp_existing"
            log_info(
                "browser.cdp.connect.ok",
                mode="reuse",
                cdp_url=self.config.cdp_url,
                contexts=len(contexts),
            )
            return browser, context
        log_info("browser.cdp.connect.miss", cdp_url=self.config.cdp_url, error=pre_error or "none")

        self.launched_proc = launch_detached_browser_process(self.playwright, self.config)
        browser, wait_error = wait_for_cdp(
            self.playwright,
            self.config.cdp_url,
            DETACHED_CONNECT_WAIT_SECONDS,
        )
        if browser is not None:
            contexts = list(browser.contexts)
            self.created_context = not contexts
            context = contexts[0] if contexts else browser.new_context()
            self.runtime_mode = "cdp_detached"
            log_info(
                "browser.cdp.connect.ok",
                mode="detached",
                cdp_url=self.config.cdp_url,
                pid=self.launched_proc.pid if self.launched_proc is not None else 0,
                contexts=len(contexts),
            )
            return browser, context

        probe = probe_cdp_endpoint(self.config.cdp_url)
        if probe["tcp_ok"] and probe["http_ok"]:
            log_info("browser.cdp.probe.ok", cdp_url=self.config.cdp_url, probe=probe)
            browser, wait_error_2 = wait_for_cdp(
                self.playwright,
                self.config.cdp_url,
                6.0,
            )
            if browser is not None:
                contexts = list(browser.contexts)
                self.created_context = not contexts
                context = contexts[0] if contexts else browser.new_context()
                self.runtime_mode = "cdp_detached"
                log_info(
                    "browser.cdp.connect.ok",
                    mode="detached_probe_retry",
                    cdp_url=self.config.cdp_url,
                    contexts=len(contexts),
                )
                return browser, context
            if wait_error_2:
                wait_error = wait_error_2
        launch_diag = collect_launch_diagnostics(self.launched_proc)
        log_warn(
            "browser.cdp.connect.failed",
            cdp_url=self.config.cdp_url,
            pre_error=pre_error or "none",
            post_error=wait_error or "none",
            launch=launch_diag,
        )
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
        page = self.context.new_page()
        self.owned_pages.append(page)
        return page

    def get_cookies(self) -> list[dict]:
        if self.context is None:
            return []
        return self.context.cookies()

    def get_user_agent(self, page: Page) -> str:
        return page.evaluate("() => navigator.userAgent")

    def close(self) -> None:
        while self.owned_pages:
            page = self.owned_pages.pop()
            try:
                page.close()
            except Exception:
                pass
        if self.created_context and self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
        self.context = None
        self.browser = None
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        if self.launched_proc is not None and self.launched_proc.poll() is None:
            if self.terminate_on_close:
                log_info("browser.process.stop", pid=self.launched_proc.pid)
                self.launched_proc.terminate()
                try:
                    self.launched_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.launched_proc.kill()
            else:
                log_info("browser.process.detach", pid=self.launched_proc.pid)
        self.launched_proc = None


def _same_browser_config(left: BrowserConfig, right: BrowserConfig) -> bool:
    return (
        left.profile_dir == right.profile_dir
        and left.browser_path == right.browser_path
        and left.headless == right.headless
        and left.cdp_url == right.cdp_url
        and left.launch_args == right.launch_args
    )


def get_global_browser(config: BrowserConfig) -> GlobalBrowserRuntime:
    global _GLOBAL_BROWSER
    with _GLOBAL_BROWSER_LOCK:
        if _GLOBAL_BROWSER is None:
            log_info(
                "browser.global.create",
                profile_dir=config.profile_dir,
                browser_path=config.browser_path,
                cdp_url=config.cdp_url,
            )
            bootstrap = BrowserSession(config, terminate_on_close=False).open()
            launched_proc = bootstrap.launched_proc
            bootstrap.launched_proc = None
            _GLOBAL_BROWSER = GlobalBrowserRuntime(
                config=bootstrap.config,
                launched_proc=launched_proc,
                seed_status=bootstrap.seed_status,
                seed_source=bootstrap.seed_source,
                runtime_mode=bootstrap.runtime_mode,
            )
            bootstrap.close()
            atexit.register(close_global_browser)
            return _GLOBAL_BROWSER
        if not _same_browser_config(_GLOBAL_BROWSER.config, config):
            raise RuntimeError("Global Chrome instance already exists with different browser settings.")
        log_info("browser.global.reuse", runtime_mode=_GLOBAL_BROWSER.runtime_mode or "unknown")
        return _GLOBAL_BROWSER


def open_download_browser_session(config: BrowserConfig) -> BrowserSession:
    get_global_browser(config)
    return BrowserSession(
        config,
        prepare_seed=False,
        terminate_on_close=False,
    ).open()


def close_global_browser() -> None:
    global _GLOBAL_BROWSER
    with _GLOBAL_BROWSER_LOCK:
        if _GLOBAL_BROWSER is not None:
            log_info("browser.global.close")
            _GLOBAL_BROWSER.close()
            _GLOBAL_BROWSER = None

"""The cloud browser engine: a real Chromium in the provider's cloud, per task.

The provider is an implementation detail behind ``ComputerPort``: nothing in
the API or the storage schema names it. Configure it with
``MIMI_CLOUD_BROWSER_API_KEY`` (and optionally ``MIMI_CLOUD_BROWSER_BASE``);
with no key the service reports that no browser container is configured.

What the engine does, in the provider's own words:

* create one session per task (``keepAlive`` so a long task is not cut off at
  the default five minutes), and release it ourselves when the task ends;
* drive the session with Playwright over the session's CDP endpoint;
* take a real screenshot after every step and hand it back as bytes;
* read back a live view URL for the session, which is a signed link a person
  who is not logged in to the provider can open.

The engine only navigates and reports what it saw. The allowlist, the budgets
and the stop are the runner's (``mimi.computer.runner``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from mimi.computer.engine import EngineError, TaskRef, _strip
from mimi.computer.guardrails import Page

VIEWPORT = {"width": 1280, "height": 800}


@dataclass
class CloudConfig:
    key: str = ""
    base: str = "https://api.browserbase.com"
    timeout: float = 90.0

    @property
    def usable(self) -> bool:
        return bool(self.key)


@dataclass
class _Session:
    session_id: str
    connect_url: str
    pw: Any = None
    browser: Any = None
    page: Any = None
    context: Any = None


@dataclass
class _State:
    sessions: dict[str, _Session] = field(default_factory=dict)
    live: dict[str, str] = field(default_factory=dict)


def _text(html: str) -> str:
    return _strip(html)[:4000]


class CloudBrowserEngine:
    """One cloud browser session per task, driven over CDP with Playwright."""

    name = "cloud_browser"

    def __init__(self, cfg: CloudConfig) -> None:
        self.cfg = cfg
        self._http = httpx.Client(base_url=cfg.base, timeout=cfg.timeout, headers={
            "X-BB-API-Key": cfg.key, "Content-Type": "application/json"})
        self._state = _State()

    # -- session lifecycle -------------------------------------------------
    def _create(self) -> tuple[str, str]:
        body = {"keepAlive": True,
                "browserSettings": {"viewport": VIEWPORT}}
        r = self._http.post("/v1/sessions", json=body)
        if r.status_code >= 400:
            raise EngineError(f"could not create a browser session: {r.status_code} "
                              f"{r.text[:200]}")
        data = r.json()
        return str(data.get("id", "")), str(data.get("connectUrl", ""))

    def _release(self, session_id: str) -> None:
        try:
            self._http.post(f"/v1/sessions/{session_id}",
                            json={"status": "REQUEST_RELEASE"})
        except httpx.HTTPError:
            pass

    def live_view(self, session_id: str) -> str:
        """The signed live view link: a person who is not logged in can open it."""
        if not session_id:
            return ""
        if session_id in self._state.live:
            return self._state.live[session_id]
        try:
            r = self._http.get(f"/v1/sessions/{session_id}/debug")
            if r.status_code >= 400:
                return ""
            data = r.json()
        except httpx.HTTPError:
            return ""
        url = str(data.get("debuggerFullscreenUrl") or "")
        if not url:
            pages = data.get("pages") or []
            url = str(pages[0].get("debuggerFullscreenUrl", "")) if pages else ""
        self._state.live[session_id] = url
        return url

    # -- the ComputerPort --------------------------------------------------
    def start(self, task: TaskRef) -> None:
        session_id, connect = self._create()
        if not connect:
            raise EngineError("the browser session had no CDP endpoint")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            self._release(session_id)
            raise EngineError("playwright is not installed in this runtime") from exc
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(connect)
            context = browser.contexts[0] if browser.contexts else browser.new_context(
                viewport=VIEWPORT)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(20000)
        except Exception as exc:  # the provider's endpoint refused us
            pw.stop()
            self._release(session_id)
            raise EngineError(f"could not connect to the browser session: {exc}") from exc
        self._state.sessions[task.task_id] = _Session(
            session_id=session_id, connect_url=connect, pw=pw, browser=browser,
            context=context, page=page)

    def _page(self, task: TaskRef) -> _Session:
        sess = self._state.sessions.get(task.task_id)
        if sess is None:
            raise EngineError("the browser session is not open")
        return sess

    def do(self, task: TaskRef, action: str, *, url: str = "", target: str = "",
           field: str = "", value: str = "", direction: str = "",
           amount: int = 0, current_url: str = "") -> Page:
        sess = self._page(task)
        page = sess.page
        try:
            download_bytes = b""
            if action == "goto":
                page.goto(url, wait_until="domcontentloaded")
            elif action == "click":
                page.get_by_text(target, exact=False).first.click()
                page.wait_for_load_state("domcontentloaded")
            elif action == "fill":
                page.get_by_label(field, exact=False).first.fill(value)
            elif action == "scroll":
                page.mouse.wheel(0, 600 if direction != "up" else -600)
            elif action == "download":
                with page.expect_download() as info:
                    page.get_by_text(target, exact=False).first.click()
                download = info.value
                import tempfile
                from pathlib import Path

                path = Path(tempfile.mkdtemp(prefix="mimi-dl-")) / (
                    download.suggested_filename or "download.bin")
                download.save_as(str(path))
                download_bytes = path.read_bytes()
            elif action not in ("screenshot", "extract"):
                raise EngineError(f"unknown action {action}")
            png = page.screenshot(type="png") if action != "download" else b""
            html = page.content()[:20000]
            text = page.inner_text("body")[:4000] if action in ("extract", "download") else ""
            return Page(url=page.url, title=page.title(), html=html, text=text,
                        screenshot=download_bytes or png)
        except EngineError:
            raise
        except Exception as exc:
            raise EngineError(f"the browser could not do {action}: "
                              f"{_clean(str(exc))[:200]}") from exc

    def sign_in_link(self, task: TaskRef) -> str:
        sess = self._state.sessions.get(task.task_id)
        return self.live_view(sess.session_id) if sess else ""

    def stop(self, task: TaskRef, reason: str) -> None:
        sess = self._state.sessions.pop(task.task_id, None)
        if sess is None:
            return
        for close in (lambda: sess.page.close(), lambda: sess.context.close(),
                      lambda: sess.browser.close(), lambda: sess.pw.stop()):
            try:
                close()
            except Exception:
                pass
        self._release(sess.session_id)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def engine_from_env() -> Any:
    """The cloud browser when a key is configured, else unconfigured."""
    import os

    key = (os.environ.get("MIMI_CLOUD_BROWSER_API_KEY", "").strip()
           or os.environ.get("MIMI_COMPUTER_API_KEY", "").strip())
    base = (os.environ.get("MIMI_CLOUD_BROWSER_BASE", "").strip()
            or "https://api.browserbase.com")
    if not key:
        from mimi.computer.service import Unconfigured

        return Unconfigured()
    return CloudBrowserEngine(CloudConfig(key=key, base=base))

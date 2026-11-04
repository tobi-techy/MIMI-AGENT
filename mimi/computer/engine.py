"""The engine seam: the only code that talks to a browser container.

``ComputerPort`` is what the runner drives. Three implementations:

* ``FakeComputer`` (tests, offline) -- a small scripted site in memory;
* ``MaritimeBoxBrowser`` (the free-plan path) -- headless Chromium through
  Playwright inside an existing Maritime **Agent box**, driven over the
  Maritime API (``exec`` and the files API). Browsers live under ``/data`` so
  they survive a sleep, a wake and a restart;
* ``MaritimeDesktopBrowser`` -- the same port against a Maritime **Computer**,
  which is where the visible screen and the viewer live. It needs a paid plan
  (``402 computer_limit`` on a free seat plan), so it is not the default yet.

Nothing here decides anything: it navigates and reports what it saw. The
guardrails and the budgets are the runner's, in ``mimi.computer.runner``.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from mimi.computer.guardrails import Page

MARKER = "/data/mimi/provisioned.json"
BRIDGE_DIR = "/data/mimi"
BRIDGE_PORT = 8899


class EngineError(RuntimeError):
    """The container or the browser could not do what was asked."""


@dataclass
class TaskRef:
    task_id: str
    domain: str


class ComputerPort(Protocol):
    name: str

    def start(self, task: TaskRef) -> None: ...

    def do(self, task: TaskRef, action: str, *, url: str = "", target: str = "",
           field: str = "", value: str = "", direction: str = "",
           amount: int = 0, current_url: str = "") -> Page: ...

    def sign_in_link(self, task: TaskRef) -> str: ...

    def stop(self, task: TaskRef, reason: str) -> None: ...


# --------------------------------------------------------------- the fake


class FakeComputer:
    """An in-memory site, for the tests. Never the shipped default."""

    name = "fake"

    def __init__(self, pages: dict[str, dict[str, Any]] | None = None,
                 redirect: dict[str, str] | None = None,
                 login: tuple[str, ...] = (), forbidden: tuple[str, ...] = (),
                 downloads: dict[str, str] | None = None,
                 fail: tuple[str, ...] = ()) -> None:
        self.pages = dict(pages or {})
        self.redirect = dict(redirect or {})
        self.login = tuple(login)
        self.forbidden = tuple(forbidden)
        self.downloads = dict(downloads or {})
        self.fail = tuple(fail)
        self.stopped: str | None = None
        self.started: str | None = None
        self.calls: list[tuple[str, str]] = []
        self.filled: dict[str, str] = {}

    def start(self, task: TaskRef) -> None:
        self.started = task.task_id

    def _resolve(self, url: str) -> str:
        seen = 0
        while url in self.redirect and seen < 5:
            url = self.redirect[url]
            seen += 1
        return url

    def _page(self, url: str) -> Page:
        if url in self.fail:
            raise EngineError(f"fake navigation failed for {url}")
        entry = self.pages.get(url)
        if entry is None:
            return Page(url=url, title="Not found", html="<h1>404</h1>",
                        text="not found")
        html = entry.get("html", "")
        if url in self.login:
            html = ('<form><input type="email" name="email">'
                    '<input type="password" name="password">'
                    '<button>Sign in</button></form>')
        elif url in self.forbidden:
            html = ('<h1>Checkout</h1><input autocomplete="cc-number" name="cardnumber">'
                    '<button>Place order</button>')
        return Page(url=url, title=entry.get("title", ""), html=html,
                    text=entry.get("text", "") or _strip(html),
                    screenshot=entry.get("png", b"\x89PNG fake"))

    def do(self, task: TaskRef, action: str, *, url: str = "", target: str = "",
           field: str = "", value: str = "", direction: str = "",
           amount: int = 0, current_url: str = "") -> Page:
        self.calls.append((action, url or current_url))
        if action == "goto":
            return self._page(self._resolve(url))
        if action in ("extract", "screenshot", "scroll"):
            return self._page(current_url)
        if action == "click":
            nxt = self.pages.get(current_url, {}).get("click", {})
            return self._page(self._resolve(nxt.get(target, current_url)))
        if action == "fill":
            if field:
                self.filled[field] = value
            return self._page(current_url)
        if action == "download":
            name = self.downloads.get(current_url, "")
            return Page(url=current_url, title="Download", html="", text="",
                        filename=name, screenshot=name.encode() if name else b"")
        return self._page(current_url)

    def sign_in_link(self, task: TaskRef) -> str:
        return ""

    def stop(self, task: TaskRef, reason: str) -> None:
        self.stopped = reason


def _strip(html: str) -> str:
    import re

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


# ------------------------------------------------- Maritime, over the API


@dataclass
class MaritimeConfig:
    token: str
    agent_id: str = ""
    base: str = "https://api.maritime.sh"
    timeout: float = 90.0

    @property
    def usable(self) -> bool:
        return bool(self.token and self.agent_id)


class MaritimeBox:
    """Thin client for the Maritime Agent box API (exec + files)."""

    def __init__(self, cfg: MaritimeConfig) -> None:
        self.cfg = cfg
        self._http = httpx.Client(base_url=cfg.base, timeout=cfg.timeout,
                                  headers={"Authorization": f"Bearer {cfg.token}"})

    def close(self) -> None:
        self._http.close()

    def exec(self, command: str, timeout: float = 60.0, detached: bool = False
             ) -> dict[str, Any]:
        body: dict[str, Any] = {"command": command, "timeout": timeout}
        if detached:
            body["detached"] = True
        r = self._http.post(f"/api/agents/{self.cfg.agent_id}/exec", json=body)
        if r.status_code >= 400:
            raise EngineError(f"exec failed: {r.status_code} {r.text[:200]}")
        return r.json()

    def write_file(self, path: str, content: str) -> None:
        r = self._http.put(f"/api/agents/{self.cfg.agent_id}/files/write",
                           json={"path": path, "content": content})
        if r.status_code >= 400:
            raise EngineError(f"write {path} failed: {r.status_code} {r.text[:200]}")

    def download(self, path: str) -> bytes:
        r = self._http.get(f"/api/agents/{self.cfg.agent_id}/files/download",
                           params={"path": path})
        if r.status_code >= 400:
            raise EngineError(f"download {path} failed: {r.status_code} {r.text[:200]}")
        return r.content


# The bridge: one long-lived Playwright browser inside the box. It is written
# to the box once and started once; every step is one short ``exec``.
BRIDGE_PY = r'''
import base64, json, os, re, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/data/mimi/pw")
sys.path.insert(0, "/data/mimi/pylib")
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = "/data/mimi/shots"
os.makedirs(OUT, exist_ok=True)
_state = {}
_lock = threading.Lock()


def strip(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        try:
            out = handle(req)
            code = 200
        except Exception as exc:  # report, never crash the bridge
            out = {"ok": False, "error": str(exc)[:400]}
            code = 500
        body = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def handle(req):
    action = req.get("action")
    with _lock:
        pw = _state.get("pw")
        if pw is None:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            _state["pw"], _state["browser"] = pw, browser
        browser = _state["browser"]
        if action == "new":
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.set_default_timeout(20000)
            _state["ctx"], _state["page"] = ctx, page
            return {"ok": True, "url": "about:blank", "title": "", "text": "", "html": ""}
        if action == "close":
            ctx = _state.pop("ctx", None)
            _state.pop("page", None)
            if ctx is not None:
                ctx.close()
            return {"ok": True}
        page = _state.get("page")
        if page is None:
            raise RuntimeError("no session: call new first")
        path = req.get("out") or os.path.join(OUT, "shot.png")
        if action == "goto":
            page.goto(req["url"], wait_until="domcontentloaded")
        elif action == "click":
            page.get_by_text(req["target"], exact=False).first.click()
            page.wait_for_load_state("domcontentloaded")
        elif action == "fill":
            page.get_by_label(req["field"], exact=False).first.fill(req["value"])
        elif action == "scroll":
            page.mouse.wheel(0, 600 if req.get("direction") != "up" else -600)
        elif action == "download":
            with page.expect_download() as info:
                page.get_by_text(req["target"], exact=False).first.click()
            dl = info.value
            dl.save_as(path)
            return {"ok": True, "url": page.url, "title": page.title(),
                    "text": "", "html": "", "file": path}
        elif action not in ("screenshot", "extract"):
            raise RuntimeError(f"unknown action {action}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        page.screenshot(path=path, full_page=False)
        html = page.content()
        return {"ok": True, "url": page.url, "title": page.title(),
                "text": strip(html)[:4000], "html": html[:20000],
                "shot": path}


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("MIMI_BRIDGE_PORT", "8899"))),
                        Handler).serve_forever()
'''

CLIENT_PY = r'''
import json, os, sys, urllib.request

port = os.environ.get("MIMI_BRIDGE_PORT", "8899")
req = urllib.request.Request("http://127.0.0.1:%s/" % port,
                             data=sys.argv[1].encode(),
                             headers={"content-type": "application/json"})
with urllib.request.urlopen(req, timeout=120) as r:
    sys.stdout.write(r.read().decode())
'''

SETUP_SH = r'''
set -e
export PLAYWRIGHT_BROWSERS_PATH=/data/mimi/pw
export PYTHONPATH=/data/mimi/pylib
mkdir -p /data/mimi/pylib /data/mimi/pw /data/mimi/shots
python3 -m pip install --quiet --break-system-packages --target /data/mimi/pylib playwright
python3 -m playwright install --with-deps chromium >/data/mimi/install.log 2>&1 || \
  python3 -m playwright install chromium >>/data/mimi/install.log 2>&1
echo "{\"ok\":true}" > /data/mimi/provisioned.json
'''


class MaritimeBoxBrowser:
    """Headless Chromium + Playwright inside an existing Maritime Agent box."""

    name = "maritime_agent_box"

    def __init__(self, cfg: MaritimeConfig) -> None:
        self.box = MaritimeBox(cfg)
        self.cfg = cfg

    # -- provisioning -----------------------------------------------------
    def provision(self) -> None:
        marker = self.box.exec(f"cat {MARKER} 2>/dev/null || echo missing")
        if "ok" not in marker.get("stdout", ""):
            self.box.write_file(f"{BRIDGE_DIR}/setup.sh", SETUP_SH)
            self.box.exec(f"sh {BRIDGE_DIR}/setup.sh", timeout=600)
        self.box.write_file(f"{BRIDGE_DIR}/bridge.py", BRIDGE_PY)
        self.box.write_file(f"{BRIDGE_DIR}/client.py", CLIENT_PY)
        self.box.exec(
            "pkill -f mimi/bridge.py 2>/dev/null || true; "
            "cd /data/mimi && PLAYWRIGHT_BROWSERS_PATH=/data/mimi/pw "
            "PYTHONPATH=/data/mimi/pylib nohup python3 /data/mimi/bridge.py "
            ">/data/mimi/bridge.log 2>&1 & sleep 3; echo started")

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        quoted = json.dumps(payload).replace("'", "'\\''")
        raw = self.box.exec(f"python3 {BRIDGE_DIR}/client.py '{quoted}'")
        out = raw.get("stdout", "").strip()
        if raw.get("exitCode") not in (0, None) or not out:
            raise EngineError(f"bridge failed: {raw.get('stderr', '')[:300]}")
        data = json.loads(out)
        if not data.get("ok"):
            raise EngineError(str(data.get("error", "bridge error"))[:300])
        return data

    def _page(self, data: dict[str, Any]) -> Page:
        shot = data.get("shot") or data.get("file") or ""
        png = b""
        if shot and data.get("action_download") is not True:
            try:
                png = self.box.download(shot)
            except EngineError:
                png = b""
        return Page(url=data.get("url", ""), title=data.get("title", ""),
                    html=data.get("html", ""), text=data.get("text", ""),
                    screenshot=png)

    # -- ComputerPort -----------------------------------------------------
    def start(self, task: TaskRef) -> None:
        self.provision()
        self._call({"action": "new"})
        self.box.exec(f"mkdir -p {BRIDGE_DIR}/shots/{task.task_id}")

    def do(self, task: TaskRef, action: str, *, url: str = "", target: str = "",
           field: str = "", value: str = "", direction: str = "",
           amount: int = 0, current_url: str = "") -> Page:
        out = posixpath.join(BRIDGE_DIR, "shots", task.task_id, f"{action}.png")
        payload = {"action": action, "url": url, "target": target, "field": field,
                   "value": value, "direction": direction, "out": out}
        data = self._call(payload)
        page = self._page(data)
        if action == "download":
            path = data.get("file", "")
            blob = self.box.download(path) if path else b""
            return Page(url=page.url, title=data.get("title", ""), text="",
                        screenshot=blob)
        return page

    def sign_in_link(self, task: TaskRef) -> str:
        return ""  # no live view on the free plan: honestly empty

    def stop(self, task: TaskRef, reason: str) -> None:
        try:
            self._call({"action": "close"})
        except EngineError:
            pass


class MaritimeDesktopBrowser:
    """The same port against a Maritime Computer (visible screen + viewer).

    Not the default: on a free seat plan ``POST /api/v1/computers`` answers
    ``402 computer_limit``. Kept here so the contract is built against the real
    shape, and so it turns on unchanged when a plan exists.
    """

    name = "maritime_desktop"

    def __init__(self, cfg: MaritimeConfig) -> None:
        self.cfg = cfg
        self._http = httpx.Client(base_url=cfg.base, timeout=cfg.timeout,
                                  headers={"Authorization": f"Bearer {cfg.token}"})

    def _computer(self, task: TaskRef) -> str:
        r = self._http.post("/api/v1/computers",
                            json={"externalUserId": f"mimi-{task.task_id}",
                                  "name": f"mimi-{task.task_id}"})
        if r.status_code >= 400:
            raise EngineError(f"computer create failed: {r.status_code} {r.text[:200]}")
        return r.json().get("id", "")

    def start(self, task: TaskRef) -> None:
        self._computer(task)

    def do(self, task: TaskRef, action: str, *, url: str = "", target: str = "",
           field: str = "", value: str = "", direction: str = "",
           amount: int = 0, current_url: str = "") -> Page:
        raise EngineError("MaritimeDesktopBrowser needs a paid plan (402 computer_limit)")

    def sign_in_link(self, task: TaskRef) -> str:
        return ""

    def stop(self, task: TaskRef, reason: str) -> None:
        pass

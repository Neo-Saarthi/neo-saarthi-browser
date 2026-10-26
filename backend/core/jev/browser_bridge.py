"""Bridge-mode Browser — same public API as browser.Browser, MF-Saarthi style.

Instead of launching its own Chromium, it drives the user's existing
browser (Chrome/Edge/Brave/Opera/Vivaldi) through the loopback sidecar
(jev_bridge.py) + JEV Bridge extension.

Public surface is intentionally identical to browser.Browser so
loop.py, decide.py and recipes/* work unchanged:
  goto, reload, url, title, page_text, snapshot, get_element,
  find_by_label, go_back, act, close, is_feed

"""

from __future__ import annotations

import base64
import os
import time
import urllib.parse
from typing import Any, Dict, Optional

import httpx
from core.jev.control import browser_input_interrupted

from browser import READY_TEXT_MIN, labels_overlap

BRIDGE_URL = os.environ.get(
    "JEV_BRIDGE_URL",
    f"http://127.0.0.1:{os.environ.get('JEV_BRIDGE_PORT', '41692')}",
)
BRIDGE_TIMEOUT = float(os.environ.get("JEV_BRIDGE_TIMEOUT", "90"))


class BridgeNotAvailable(RuntimeError):
    pass


class BridgeError(RuntimeError):
    pass


def _get(path: str, timeout: float) -> Dict[str, Any]:
    try:
        resp = httpx.get(f"{BRIDGE_URL}{path}", timeout=timeout)
    except Exception as e:
        raise BridgeNotAvailable(f"JEV bridge sidecar not reachable at {BRIDGE_URL} ({e}). "
                                 "Start it with: python jev_bridge.py")
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code >= 400:
        raise BridgeError(data.get("detail") or f"Bridge returned {resp.status_code}.")
    return data if isinstance(data, dict) else {"result": data}


def _post(path: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    try:
        resp = httpx.post(f"{BRIDGE_URL}{path}", json=payload, timeout=timeout)
    except Exception as e:
        raise BridgeNotAvailable(f"JEV bridge sidecar not reachable at {BRIDGE_URL} ({e}). "
                                 "Start it with: python jev_bridge.py")
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code == 503:
        raise BridgeNotAvailable(data.get("detail") or "No paired browser is polling.")
    if resp.status_code >= 400:
        raise BridgeError(data.get("detail") or f"Bridge returned {resp.status_code}.")
    return data if isinstance(data, dict) else {"result": data}


class _BridgeKeyboard:
    def __init__(self, owner: "BridgeBrowser"):
        self._owner = owner

    def press(self, key: str) -> None:
        self._owner._dispatch("press", {"key": key})


class _BridgeMouse:
    def __init__(self, owner: "BridgeBrowser"):
        self._owner = owner

    def click(self, x: int, y: int) -> None:
        self._owner._dispatch("click_at", {"x": x, "y": y})

    def move(self, x: int, y: int) -> None:  # no-op compat; real click carries coords
        return None

    def wheel(self, dx: int, dy: int) -> None:
        self._owner._dispatch("scroll", {"delta": dy})


class _BridgeLocator:
    """Minimal compat: allows counting elements and screenshotting canvas via toDataURL."""

    def __init__(self, owner: "BridgeBrowser", selector: str):
        self._owner = owner
        self._selector = selector

    def count(self) -> int:
        try:
            cnt = self._owner._page.operation("count_elements", {"selector": self._selector})
            return int(cnt or 0)
        except Exception:
            return 0

    def click(self) -> None:
        self._owner._page.operation("dismiss_overlay", {"selector": self._selector})

    @property
    def first(self) -> "_BridgeLocator":
        return self

    def screenshot(self, path: Optional[str] = None, *args, **kwargs) -> bytes:
        data_url = self._owner._page.operation("canvas_image", {"selector": self._selector})
        if data_url and "," in data_url:
            b64 = data_url.split(",", 1)[1]
            raw = base64.b64decode(b64)
            if path:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(raw)
            return raw
        raise BridgeError(f"Element screenshot failed for selector: {self._selector}")


class _BridgePage:

    def __init__(self, owner: "BridgeBrowser"):
        self._owner = owner
        self.keyboard = _BridgeKeyboard(owner)
        self.mouse = _BridgeMouse(owner)

    def operation(self, name: str, parameters: Dict[str, Any]) -> Any:
        out = self._owner._dispatch("page_operation", {"operation": name, "parameters": parameters})
        return out.get("value")

    def locator(self, selector: str) -> _BridgeLocator:
        return _BridgeLocator(self._owner, selector)

    def wait_for_timeout(self, ms: int) -> None:
        time.sleep(min(max(int(ms), 0), 8000) / 1000.0)

    def wait_for_load_state(self, *args, **kwargs) -> None:
        time.sleep(0.6)

    @property
    def url(self) -> str:
        return self._owner.url


def launch_browser_if_needed(preferred_family: Optional[str] = None, wait_timeout: float = 12.0) -> bool:
    """Launch the browser (Edge, Chrome, Brave) where the extension is present if not already live."""
    import subprocess
    import sys

    try:
        status = _get("/api/v1/browser-use/status", timeout=2)
        clients = status.get("clients") or []
        if any(c.get("live") for c in clients):
            return True
    except Exception:
        pass

    candidates = ["Microsoft Edge", "Google Chrome", "Brave Browser"]
    if preferred_family:
        pf_low = preferred_family.lower()
        if "chrome" in pf_low:
            candidates = ["Google Chrome", "Microsoft Edge", "Brave Browser"]
        elif "brave" in pf_low:
            candidates = ["Brave Browser", "Microsoft Edge", "Google Chrome"]

    launched = False
    if sys.platform == "darwin":
        for app in candidates:
            check = subprocess.run(["open", "-Ra", app], capture_output=True)
            if check.returncode == 0:
                subprocess.Popen(["open", "-a", app])
                launched = True
                break
    elif sys.platform == "win32":
        win_candidates = [
            [os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
             os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")],
            [os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
             os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
             os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")],
            [os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
             os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe")],
        ]
        if preferred_family and "chrome" in preferred_family.lower():
            win_candidates = [win_candidates[1], win_candidates[0], win_candidates[2]]
        elif preferred_family and "brave" in preferred_family.lower():
            win_candidates = [win_candidates[2], win_candidates[0], win_candidates[1]]

        for paths in win_candidates:
            for p in paths:
                if os.path.exists(p):
                    subprocess.Popen([p])
                    launched = True
                    break
            if launched:
                break
        if not launched:
            try:
                subprocess.Popen(["cmd", "/c", "start", "msedge"], shell=False)
                launched = True
            except Exception:
                pass
    else:  # Linux
        linux_bins = ["microsoft-edge", "google-chrome", "brave-browser", "chromium-browser"]
        if preferred_family and "chrome" in preferred_family.lower():
            linux_bins = ["google-chrome", "microsoft-edge", "brave-browser", "chromium-browser"]
        for b in linux_bins:
            try:
                subprocess.Popen([b])
                launched = True
                break
            except Exception:
                pass

    if not launched:
        return False

    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            status = _get("/api/v1/browser-use/status", timeout=2)
            clients = status.get("clients") or []
            if any(c.get("live") for c in clients):
                return True
        except Exception:
            pass

    return False


class BridgeBrowser:
    def __init__(self, client_id: Optional[str] = None,
                 timeout: Optional[float] = None, visible: bool = False):
        self._visible = visible
        self._timeout = timeout or BRIDGE_TIMEOUT
        self._client_id = client_id or os.environ.get("JEV_BRIDGE_CLIENT_ID")
        self._elements: Dict[str, Dict[str, Any]] = {}
        self._last: Dict[str, Any] = {"url": "", "title": "", "text": ""}
        self._tab_id: Optional[int] = None
        self._page = _BridgePage(self)
        # Fail fast if no paired browser (factory falls back to local).
        try:
            status = _get("/api/v1/browser-use/status", timeout=5)
        except BridgeNotAvailable:
            raise
        clients = status.get("clients") or []
        live = [c for c in clients if c.get("live")]
        if not live:
            launch_browser_if_needed(preferred_family=os.environ.get("JEV_BROWSER_FAMILY", "edge"))
            try:
                status = _get("/api/v1/browser-use/status", timeout=3)
                clients = status.get("clients") or []
                live = [c for c in clients if c.get("live")]
            except Exception:
                pass
        if self._client_id:
            match = [c for c in live if c.get("client_id") == self._client_id]
            if not match:
                raise BridgeNotAvailable(
                    f"Browser profile {self._client_id!r} is not actively polling.")
        elif not live:
            raise BridgeNotAvailable("No paired browser is connected. "
                                     "Load extension/ unpacked and run python jev_bridge.py.")
        if live and not self._client_id:
            preferred = status.get("preferred")
            ids = {c.get("client_id") for c in live}
            if preferred not in ids:
                self._client_id = live[0].get("client_id")

    # -- transport ----------------------------------------------------
    def _dispatch(self, action: str, arguments: Optional[Dict[str, Any]] = None,
                  timeout: Optional[float] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"action": action, "arguments": arguments or {}}
        if self._client_id:
            payload["client_id"] = self._client_id
        if self._tab_id:
            payload.setdefault("arguments", {})["tab_id"] = self._tab_id
        payload["timeout"] = timeout or self._timeout
        return _post("/api/v1/browser-use/desktop/action", payload,
                     timeout=(timeout or self._timeout) + 10)

    # -- Browser-compatible API ---------------------------------------
    @property
    def is_feed(self) -> bool:
        url = (self._last.get("url") or "").lower()
        return any(m in url for m in ("/shorts/", "tiktok.com",
                                      "instagram.com/reel", "/reel/"))

    def goto(self, url: str) -> None:
        try:
            out = self._dispatch("navigate", {"url": url, "visible": self._visible})
            if out.get("tab_id"):
                self._tab_id = out["tab_id"]
        except BridgeError as err:
            target_host = urllib.parse.urlparse(url).netloc.lower()
            current_host = urllib.parse.urlparse(self.url).netloc.lower()
            if not target_host or target_host not in current_host:
                raise err
        self._wait_for_content()

    def reload(self) -> None:
        self._dispatch("reload", {})
        self._wait_for_content()

    @property
    def url(self) -> str:
        return self._last.get("url") or ""

    @property
    def title(self) -> str:
        return self._last.get("title") or ""

    def page_text(self) -> str:
        return self._last.get("text") or ""

    def snapshot(self, retries: int = 3) -> Dict[str, Any]:
        last_err: Optional[Exception] = None
        for _ in range(max(1, retries)):
            try:
                out = self._dispatch("snapshot", {})
            except Exception as e:
                if browser_input_interrupted(e):
                    raise
                last_err = e
                time.sleep(0.25)
                continue
            snap = (out.get("snapshot") or out)
            if not isinstance(snap, dict):
                continue
            if snap.get("tab_id"):
                self._tab_id = snap["tab_id"]
            elements = []
            for a in snap.get("elements") or []:
                ops = []
                if not a.get("readonly") and not a.get("disabled") and (a.get("editable") or a.get("role") in ("combobox", "searchbox", "textbox")
                        or "date" in (a.get("label") or "").lower()):
                    ops.append("TYPE_TEXT")
                ops.append("CLICK")
                if not a.get("disabled") and (a.get("calendar_opener") or a.get("input_type") == "date" or (a.get("readonly") and "date" in (a.get("label") or "").lower())):
                    ops.append("SET_DATE")
                el = {"index": a.get("index"), "node": a.get("node"),
                      "role": a.get("role"), "label": a.get("label"),
                      "operations": ops}
                if a.get("select") is not None:
                    el["select"] = a["select"]
                for key in ("value", "checked", "selected", "expanded",
                            "isPassword", "href", "readonly", "disabled", "input_type", "calendar_id", "calendar_text", "calendar_bounds", "calendar_opener", "option_label", "calendar_view"):
                    if key in a:
                        el[key] = a[key]
                elements.append(el)
            self._elements = {el["index"]: el for el in elements if el.get("index")}
            self._last = {"url": snap.get("url") or self._last.get("url") or "",
                          "title": snap.get("title") or "",
                          "text": snap.get("text") or ""}
            page = {"url": self._last["url"], "title": self._last["title"],
                    "viewport": snap.get("viewport") or {},
                    "calendars": snap.get("calendars") or [],
                    "calendar_protocol": snap.get("calendar_protocol"),
                    "text": self._last["text"], "elements": elements,
                    "scroll_y": snap.get("scrollY", 0),
                    "scroll_height": snap.get("scrollHeight", 0),
                    "client_height": snap.get("clientHeight", 800),
                    "is_scrollable": snap.get("isScrollable", False),
                    "at_bottom": snap.get("atBottom", False),
                    "scroll_percent": snap.get("scrollPercent", 0)}
            if elements or len(page["text"]) > READY_TEXT_MIN:
                return page
            time.sleep(0.25)
        if self._elements or self._last.get("text"):
            return {"url": self._last.get("url") or "", "title": self._last.get("title") or "",
                    "text": self._last.get("text") or "",
                    "elements": list(self._elements.values()),
                    "scroll_y": 0, "scroll_height": 0, "client_height": 800,
                    "is_scrollable": False, "at_bottom": False, "scroll_percent": 0}
        raise BridgeError(f"snapshot failed ({last_err})")

    def get_element(self, index: str) -> Optional[Dict[str, Any]]:
        return self._elements.get(index)

    def find_by_label(self, label: str, exclude: Optional[str] = None) -> Optional[str]:
        wanted = " ".join((label or "").lower().split())
        if not wanted:
            return None
        partial = None
        for index in sorted(self._elements, key=lambda i: int(i)):
            if index == exclude:
                continue
            have = " ".join((self._elements[index].get("label") or "").lower().split())
            if have == wanted:
                return index
            if partial is None and labels_overlap(wanted, have):
                partial = index
        return partial

    def screenshot(self) -> Optional[str]:
        try:
            out = self._dispatch("screenshot", {})
            return out.get("screenshot") or out.get("dataUrl")
        except Exception as error:
            if browser_input_interrupted(error):
                raise
            return None

    def go_back(self) -> str:
        try:
            self._dispatch("go_back", {})
            time.sleep(0.6)
            return "went back to previous page"
        except Exception as e:
            if browser_input_interrupted(e):
                raise
            return f"could not go back ({e})"

    def _wait_for_content(self, timeout_ms: int = 8000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                page = self.snapshot(retries=1)
            except Exception as error:
                if browser_input_interrupted(error):
                    raise
                time.sleep(0.3)
                continue
            if page.get("elements") or len(page.get("text") or "") > READY_TEXT_MIN:
                return True
            time.sleep(0.3)
        return False

    def act(self, index: Optional[str], kind: str, text: Optional[str] = None) -> str:
        if kind == "BACK":
            return self.go_back()
        if kind == "WAIT":
            self._dispatch("wait", {"ms": 500})
            return "waited"
        if kind in ("SCROLL", "SCROLL_TOP", "SCROLL_UP"):
            if self.is_feed:
                time.sleep(0.2)
                return "no scroll available on feed"
            delta = 0 if kind == "SCROLL_TOP" else (-600 if kind == "SCROLL_UP" else 600)
            if kind == "SCROLL_TOP":
                delta = -99999
            self._dispatch("scroll", {"delta": delta})
            time.sleep(0.15)
            return "scrolled to top" if kind == "SCROLL_TOP" else (
                "scrolled up" if kind == "SCROLL_UP" else "scrolled")
        el = self._elements.get(index or "")
        if el is None:
            raise RuntimeError(f"element [{index}] is gone, re-snapshot")
        try:
            if "select" in el:
                self._dispatch("select", {"node": el["node"], "value": el["select"],
                                          "index": index})
                time.sleep(0.15)
                return f"selected option in [{index}]"
            if kind == "TYPE_TEXT":
                self._dispatch("fill", {"node": el["node"], "text": text or "",
                                        "index": index})
                time.sleep(0.2)
                return f"typed into [{index}]"
            self._dispatch("click", {"node": el["node"], "index": index})
            lbl_lower = (el.get("label") or "").lower()
            time.sleep(0.2)
            if any(k in lbl_lower for k in ("submit", "continue", "login", "confirm",
                                            "next", "sign in", "signin",
                                            "immediate", "overnight")):
                self._wait_for_content(timeout_ms=6000)
            return f"clicked [{index}]"
        except (BridgeError, BridgeNotAvailable) as e:
            msg = str(e).lower()
            if any(k in msg for k in ("gone", "re-snapshot", "stale", "no longer")):
                raise RuntimeError(f"element [{index}] is gone, covered, or disabled")
            raise

    def close(self) -> None:
        # Never kill the user's browser — just drop local state.
        self._elements = {}

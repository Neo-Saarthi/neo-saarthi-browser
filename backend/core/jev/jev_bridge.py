"""JEV browser bridge sidecar — same design as MF-Saarthi, Python port.

Extension (user's real Chrome/Edge/Brave/Opera/Vivaldi) long-polls this
loopback-only server. JEV (loop.py) dispatches jobs via /desktop/action.

  extension -> POST /bridge/auto-connect -> token
  extension -> POST /bridge/next (long-poll 20s) -> job {id, action, arguments}
  extension -> POST /bridge/result {job_id, result/error}
  jev       -> POST /desktop/action {action, arguments} -> result (waits)

Security (mirrors MF-Saarthi):
- Binds 127.0.0.1 only. No remote connections.
- Bridge routes accept chrome-extension:// origins (any unpacked ID in dev,
  optional JEV_EXTENSION_IDS allowlist for packaged builds).
- Only opaque token hash is kept for audit; raw token lives in memory.
- 12MB body cap, JSON only, no-store headers.

Stdlib only — no new dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BRIDGE_PORT = int(os.environ.get("JEV_BRIDGE_PORT", "41692"))
MAX_BODY_BYTES = 12 * 1024 * 1024
STALE_AFTER_SECONDS = 45
POLL_WAIT_SECONDS = 20
DISPATCH_TIMEOUT_SECONDS = 90

# Optional comma-separated allowlist of 32-char extension IDs for packaged
# builds. Empty = allow any unpacked extension (MF-Saarthi dev behaviour).
_EXTENSION_IDS = {
    part.strip()
    for part in os.environ.get("JEV_EXTENSION_IDS", "").split(",")
    if part.strip()
}


class BridgeError(RuntimeError):
    pass


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


class _Connection:
    def __init__(self, client_id: str, label: str, browser_family: str,
                 profile_name: str | None, token: str):
        self.client_id = client_id
        self.label = label
        self.browser_family = browser_family
        self.profile_name = profile_name
        self.token = token
        self.token_hash = _token_hash(token)
        self.connected_at = _now()
        self.last_seen_at = _now()
        self.jobs: queue.Queue = queue.Queue()
        # job_id -> {"event": Event, "result": Any, "error": str|None}
        self.pending: dict[str, dict] = {}


class Broker:
    """Thread-safe job broker. One preferred client, per-profile queues."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_token: dict[str, _Connection] = {}
        self._token_by_client: dict[str, str] = {}
        self._preferred: str | None = None

    # -- pairing ------------------------------------------------------
    def auto_connect(self, client_id: str, label: str,
                     browser_family: str, profile_name: str | None = None) -> dict:
        client_id = (client_id or "").strip() or str(uuid.uuid4())
        label = (label or "Browser").strip()[:100] or "Browser"
        browser_family = (browser_family or "chrome").strip()[:30] or "chrome"
        token = secrets.token_urlsafe(48)
        with self._lock:
            old_token = self._token_by_client.get(client_id)
            if old_token:
                old = self._by_token.pop(old_token, None)
                if old:
                    for entry in old.pending.values():
                        entry["error"] = "Browser profile reconnected."
                        entry["event"].set()
                    old.pending.clear()
            conn = _Connection(client_id, label, browser_family,
                               (profile_name or "").strip()[:100] or None, token)
            self._by_token[token] = conn
            self._token_by_client[client_id] = token
            if self._preferred is None:
                self._preferred = client_id
        return {
            "bridge_token": token,
            "client_id": client_id,
            "browser_family": browser_family,
            "label": label,
        }

    def _for_token(self, token: str) -> _Connection:
        with self._lock:
            conn = self._by_token.get(token or "")
            if conn is None:
                raise BridgeError("Unknown or revoked bridge token.")
            return conn

    def _resolve(self, client_id: str | None) -> _Connection:
        with self._lock:
            cid = (client_id or "").strip() or (self._preferred or "")
            token = self._token_by_client.get(cid, "")
            conn = self._by_token.get(token)
            if conn is None:
                raise BridgeError("No paired browser is connected.")
            if _now() - conn.last_seen_at > STALE_AFTER_SECONDS:
                raise BridgeError("Paired browser is not actively polling.")
            return conn

    # -- extension side ------------------------------------------------
    def next_job(self, token: str, wait_seconds: float = POLL_WAIT_SECONDS) -> dict | None:
        conn = self._for_token(token)
        conn.last_seen_at = _now()
        try:
            job = conn.jobs.get(timeout=max(0.0, min(float(wait_seconds or 0), 25.0)))
        except queue.Empty:
            conn.last_seen_at = _now()
            return None
        conn.last_seen_at = _now()
        return job

    def submit_result(self, token: str, job_id: str, result, error: str | None) -> None:
        conn = self._for_token(token)
        conn.last_seen_at = _now()
        with self._lock:
            entry = conn.pending.pop(job_id, None)
        if entry is None:
            return  # timed-out job already discarded (MF behaviour)
        entry["result"] = result
        entry["error"] = (error or "")[:500] if error else None
        entry["event"].set()

    # -- JEV side ------------------------------------------------------
    def dispatch(self, action: str, arguments: dict | None = None,
                 client_id: str | None = None,
                 timeout: float = DISPATCH_TIMEOUT_SECONDS) -> dict:
        conn = self._resolve(client_id)
        job_id = str(uuid.uuid4())
        entry: dict = {"event": threading.Event(), "result": None, "error": None}
        with self._lock:
            conn.pending[job_id] = entry
        conn.jobs.put({"id": job_id, "action": action,
                       "arguments": arguments or {}})
        if not entry["event"].wait(timeout=min(float(timeout or 0) or DISPATCH_TIMEOUT_SECONDS, 120.0)):
            with self._lock:
                conn.pending.pop(job_id, None)
            raise BridgeError(f"Browser action timed out ({action}).")
        if entry["error"]:
            raise BridgeError(entry["error"])
        result = entry["result"]
        return result if isinstance(result, dict) else {"result": result}

    # -- management ----------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            now = _now()
            clients = [
                {
                    "client_id": c.client_id,
                    "label": c.label,
                    "browser_family": c.browser_family,
                    "profile_name": c.profile_name,
                    "connected_at": c.connected_at,
                    "last_seen_at": c.last_seen_at,
                    "live": (now - c.last_seen_at) <= STALE_AFTER_SECONDS,
                    "queued": c.jobs.qsize(),
                    "token_hash": c.token_hash[:16] + "…",
                }
                for c in self._by_token.values()
            ]
            return {"connected": bool(clients), "preferred": self._preferred,
                    "clients": clients}

    def select(self, client_id: str) -> dict:
        with self._lock:
            if client_id not in self._token_by_client:
                raise BridgeError("Unknown browser profile.")
            self._preferred = client_id
            return {"preferred": client_id}

    def revoke(self, client_id: str) -> dict:
        with self._lock:
            token = self._token_by_client.pop(client_id, None)
            conn = self._by_token.pop(token, None) if token else None
            if conn:
                for entry in conn.pending.values():
                    entry["error"] = "Browser connection was revoked."
                    entry["event"].set()
                conn.pending.clear()
            if self._preferred == client_id:
                remaining = list(self._token_by_client)
                self._preferred = remaining[0] if remaining else None
            return {"revoked": client_id, "preferred": self._preferred}


BROKER = Broker()


def _is_loopback(handler: BaseHTTPRequestHandler) -> bool:
    host = (handler.client_address[0] if handler.client_address else "")
    return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True  # non-browser clients (JEV itself, tests) have no Origin
    if not origin.startswith("chrome-extension://"):
        return False
    if not _EXTENSION_IDS:
        return True  # dev: any unpacked extension, like MF pre-store builds
    try:
        host = urlparse(origin).hostname or ""
    except Exception:
        return False
    return host in _EXTENSION_IDS


class _Handler(BaseHTTPRequestHandler):
    server_version = "JEVBridge/1.0"

    def log_message(self, *args):  # keep test output clean
        pass

    def _send(self, code: int, payload: dict, origin: str | None = None) -> None:
        try:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if origin and origin.startswith("chrome-extension://"):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    def do_OPTIONS(self):  # CORS preflight for the extension
        origin = self.headers.get("Origin")
        path = urlparse(self.path or "/").path
        if path.startswith("/api/v1/browser-use/") and _origin_allowed(origin):
            self.send_response(204)
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Origin")
            self.end_headers()
        else:
            self._send(404, {"detail": "Browser bridge route is unavailable."})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise BridgeError("Browser bridge payload is too large.")
        raw = self.rfile.read(length) if length else b"{}"
        if len(raw) > MAX_BODY_BYTES:
            raise BridgeError("Browser bridge payload is too large.")
        ctype = self.headers.get("Content-Type") or ""
        if raw.strip() and "application/json" not in ctype.lower():
            raise BridgeError("Browser bridge accepts JSON only.")
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            raise BridgeError("Invalid JSON body.")

    def do_GET(self):
        if not _is_loopback(self):
            self._send(403, {"detail": "Browser bridge accepts only local connections."})
            return
        path = urlparse(self.path or "/").path
        if path in ("/health", "/api/v1/browser-use/health"):
            self._send(200, {"ok": True})
        elif path in ("/api/v1/browser-use/status", "/api/v1/browser-use/bridge/status"):
            self._send(200, BROKER.status())
        else:
            self._send(404, {"detail": "Browser bridge route is unavailable."})

    def do_POST(self):
        if not _is_loopback(self):
            self._send(403, {"detail": "Browser bridge accepts only local connections."})
            return
        path = urlparse(self.path or "/").path
        origin = self.headers.get("Origin")
        try:
            body = self._read_json()
        except BridgeError as e:
            self._send(413 if "too large" in str(e) else 415 if "JSON" in str(e) else 400,
                       {"detail": str(e)}, origin)
            return
        try:
            if path.endswith("/bridge/auto-connect"):
                if not _origin_allowed(origin):
                    self._send(403, {"detail": "Browser bridge requires the installed extension."}, origin)
                    return
                out = BROKER.auto_connect(
                    client_id=str(body.get("client_id") or ""),
                    label=str(body.get("label") or body.get("browser_family") or "Browser"),
                    browser_family=str(body.get("browser_family") or "chrome"),
                    profile_name=body.get("profile_name"),
                )
                self._send(200, out, origin)
            elif path.endswith("/bridge/connect"):
                # Pairing-code variant kept for MF compat; auto-accepts in single-user JEV.
                if not _origin_allowed(origin):
                    self._send(403, {"detail": "Browser bridge requires the installed extension."}, origin)
                    return
                out = BROKER.auto_connect(
                    client_id=str(body.get("client_id") or ""),
                    label=str(body.get("label") or "Browser"),
                    browser_family=str(body.get("browser_family") or "chrome"),
                    profile_name=body.get("profile_name"),
                )
                self._send(200, out, origin)
            elif path.endswith("/bridge/next"):
                if not _origin_allowed(origin):
                    self._send(403, {"detail": "Browser bridge requires the installed extension."}, origin)
                    return
                job = BROKER.next_job(str(body.get("bridge_token") or ""),
                                      body.get("wait_seconds", POLL_WAIT_SECONDS))
                self._send(200, {"job": job}, origin)
            elif path.endswith("/bridge/result"):
                if not _origin_allowed(origin):
                    self._send(403, {"detail": "Browser bridge requires the installed extension."}, origin)
                    return
                BROKER.submit_result(str(body.get("bridge_token") or ""),
                                     str(body.get("job_id") or ""),
                                     body.get("result"), body.get("error"))
                self._send(200, {"ok": True}, origin)
            elif path.endswith("/desktop/action"):
                out = BROKER.dispatch(
                    action=str(body.get("action") or ""),
                    arguments=body.get("arguments") or {},
                    client_id=body.get("client_id") or body.get("browser_id"),
                    timeout=body.get("timeout", DISPATCH_TIMEOUT_SECONDS),
                )
                self._send(200, out if isinstance(out, dict) else {"result": out}, origin)
            elif path.endswith("/connections/select"):
                self._send(200, BROKER.select(str(body.get("client_id") or "")), origin)
            elif path.endswith("/connections/revoke"):
                self._send(200, BROKER.revoke(str(body.get("client_id") or "")), origin)
            else:
                self._send(404, {"detail": "Browser bridge route is unavailable."}, origin)
        except BridgeError as e:
            msg = str(e)
            code = 401 if "revoked" in msg.lower() or "unknown" in msg.lower() else 503 \
                if "not actively polling" in msg or "no paired" in msg.lower() else 504 \
                if "timed out" in msg.lower() else 400
            self._send(code, {"detail": msg}, origin)
        except Exception as e:  # never leak internals (MF audit rule)
            self._send(500, {"detail": "Browser action failed."}, origin)


def run(port: int = BRIDGE_PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    server = run(BRIDGE_PORT)
    print(f"JEV bridge listening on http://127.0.0.1:{BRIDGE_PORT} (loopback only)")
    print("Install extension/ in Chrome/Edge/Brave/Opera/Vivaldi — it pairs automatically.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

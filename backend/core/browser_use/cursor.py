import asyncio
import atexit
import json
import os
import queue
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional


class BrowserCursorController:
    def __init__(self, enabled: Optional[bool] = None) -> None:
        self.enabled = sys.platform == "win32" and os.getenv("NEO_SAARTHI_NATIVE_BROWSER_CURSOR", os.getenv("MF_SAARTHI_NATIVE_BROWSER_CURSOR")) == "1" if enabled is None else enabled
        self.paused = threading.Event()
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen[str]] = None
        self._messages: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._active = 0
        self._engaged = False
        self._finish_timer: Optional[threading.Timer] = None
        self.cursor_size = 32
        self.cursor_width = 32
        self.cursor_height = 32
        self._sequence = 0
        self._reader: Optional[threading.Thread] = None

    def ensure_allowed(self) -> None:
        if self.paused.is_set():
            raise RuntimeError("Browser use is paused. Press Ctrl+Alt+Space to resume.")

    def set_paused(self, paused: bool) -> None:
        if paused:
            self.paused.set()
        with self._lock:
            if self._process is not None:
                self._request("pause" if paused else "resume")
            if not paused:
                self.paused.clear()

    def _read(self, process: subprocess.Popen[str]) -> None:
        try:
            if process.stdout is None:
                return
            for line in process.stdout:
                message = json.loads(line)
                if message.get("event") == "paused":
                    self.paused.set()
                elif message.get("event") == "resumed":
                    self.paused.clear()
                else:
                    self._messages.put(message)
        finally:
            if self._active:
                self.paused.set()
            self._messages.put({"error": "Windows cursor helper stopped. Restart Saarthi before continuing browser use."})

    def _request(self, action: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeError("Windows cursor helper is unavailable.")
        self._sequence += 1
        process.stdin.write(json.dumps({"id": self._sequence, "action": action, **(arguments or {})}) + "\n")
        process.stdin.flush()
        message = self._messages.get(timeout=5)
        if message.get("error"):
            raise RuntimeError(message["error"])
        if message.get("id") != self._sequence or not message.get("ok"):
            raise RuntimeError("Windows cursor helper returned an invalid response.")

        if "cursor_size" in message:
            self.cursor_size = int(message["cursor_size"])
        if "cursor_width" in message:
            self.cursor_width = int(message["cursor_width"])
            self.cursor_height = int(message["cursor_height"])
        return message

    def status(self) -> Dict[str, Any]:
        return {"active": self.enabled and self._engaged and not self.paused.is_set(), "enabled": self.enabled, "cursor_size": self.cursor_size, "cursor_width": self.cursor_width, "cursor_height": self.cursor_height}

    def move(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure_allowed()
        if not self.enabled:
            return {}
        with self._lock:
            if not self._active:
                raise RuntimeError("Browser cursor movement is not active.")
            return self._request("move", arguments)

    def acquire(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.ensure_allowed()
            if self._process is None:
                self._process = subprocess.Popen(
                    [sys.executable, "-u", str(Path(__file__).with_name("windows_cursor_worker.py")), "--parent-pid", str(os.getpid())],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self._reader = threading.Thread(target=self._read, args=(self._process,), daemon=True)
                self._reader.start()
                try:
                    ready = self._messages.get(timeout=5)
                    if ready.get("event") != "ready":
                        raise RuntimeError("Windows cursor helper could not start.")
                except BaseException:
                    self.close()
                    raise
            if self._finish_timer is not None:
                self._finish_timer.cancel()
                self._finish_timer = None
            if not self._engaged:
                try:
                    self._request("begin")
                    self._engaged = True
                except BaseException:
                    self.close()
                    raise
            self._active += 1

    def _finish(self) -> None:
        with self._lock:
            current = threading.current_thread()
            if isinstance(current, threading.Timer) and self._finish_timer is not current:
                return
            if self._active or not self._engaged:
                return
            self._finish_timer = None
            try:
                self._request("finish")
                self._engaged = False
            except BaseException:
                self.close()
                raise

    def release(self, linger: bool = False) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._active == 0:
                return
            self._active -= 1
            if self._active == 0:
                if linger and not self.paused.is_set():
                    self._finish_timer = threading.Timer(0.75, self._finish)
                    self._finish_timer.daemon = True
                    self._finish_timer.start()
                else:
                    self._finish()

    @asynccontextmanager
    async def activity(self, linger: bool = False) -> AsyncIterator[None]:
        if not self.enabled:
            self.ensure_allowed()
            yield
            return
        acquiring = asyncio.create_task(asyncio.to_thread(self.acquire))
        try:
            await asyncio.shield(acquiring)
        except asyncio.CancelledError:
            await acquiring
            await asyncio.to_thread(self.release)
            raise
        completed = False
        try:
            self.ensure_allowed()
            yield
            completed = True
        finally:
            await asyncio.shield(asyncio.to_thread(self.release, linger and completed))

    async def wait_for_takeover(self) -> None:
        while not self.paused.is_set():
            await asyncio.sleep(0.025)

    def close(self) -> None:
        with self._lock:
            if self._finish_timer is not None:
                self._finish_timer.cancel()
                self._finish_timer = None
            process = self._process
            if process is not None:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    raise RuntimeError("Windows cursor helper is still restoring the cursor.")
                self._process = None
                if self._reader is not None:
                    self._reader.join(timeout=1)
                    self._reader = None
                if process.stdout is not None:
                    process.stdout.close()
                self._messages = queue.Queue()
            self._active = 0
            self._engaged = False


browser_cursor = BrowserCursorController()
atexit.register(browser_cursor.close)

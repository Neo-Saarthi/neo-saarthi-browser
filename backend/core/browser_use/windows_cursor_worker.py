import argparse
import ctypes
import json
import queue
import sys
import threading
import time
import math
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, Optional, Callable, Tuple, List


CURSOR_IDS = (32512, 32513, 32514, 32515, 32516, 32640, 32641, 32642, 32643, 32644, 32645, 32646, 32648, 32649, 32650, 32651)
HOTKEY_ID = 0x5341


class CursorTakeover(RuntimeError):
    pass


class IconInfo(ctypes.Structure):
    _fields_ = [("icon", wintypes.BOOL), ("x", wintypes.DWORD), ("y", wintypes.DWORD), ("mask", wintypes.HBITMAP), ("color", wintypes.HBITMAP)]


class Bitmap(ctypes.Structure):
    _fields_ = [("type", wintypes.LONG), ("width", wintypes.LONG), ("height", wintypes.LONG), ("stride", wintypes.LONG), ("planes", wintypes.WORD), ("bpp", wintypes.WORD), ("bits", ctypes.c_void_p)]


class CursorInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("flags", wintypes.DWORD), ("cursor", wintypes.HANDLE), ("point", wintypes.POINT)]


class WindowsCursor:
    def __init__(self, cursor_file: Path) -> None:
        self.cursor_file = cursor_file
        self.originals: Dict[int, int] = {}
        self.registered = False
        self.cursor_size = 32
        self.cursor_width = 32
        self.cursor_height = 32
        self._baseline_bounds: Optional[Tuple[int, int]] = None
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "GetCursorInfo": ([ctypes.POINTER(CursorInfo)], wintypes.BOOL),
            "GetIconInfo": ([wintypes.HANDLE, ctypes.POINTER(IconInfo)], wintypes.BOOL),
            "GetForegroundWindow": ([], wintypes.HWND),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetClientRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "ClientToScreen": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "GetPhysicalCursorPos": ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "SetPhysicalCursorPos": ([ctypes.c_int, ctypes.c_int], wintypes.BOOL),
            "SetThreadDpiAwarenessContext": ([wintypes.HANDLE], wintypes.HANDLE),
            "LoadCursorW": ([wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HANDLE),
            "LoadImageW": ([wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.HANDLE),
            "GetDpiForWindow": ([wintypes.HWND], wintypes.UINT),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
            "CopyImage": ([wintypes.HANDLE, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.HANDLE),
            "SetSystemCursor": ([wintypes.HANDLE, wintypes.DWORD], wintypes.BOOL),
            "SystemParametersInfoW": ([wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT], wintypes.BOOL),
            "DestroyCursor": ([wintypes.HANDLE], wintypes.BOOL),
            "RegisterHotKey": ([wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
            "UnregisterHotKey": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "PeekMessageW": ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.user32, name)
            function.argtypes = arguments
            function.restype = result
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))

    def viewport(self, width: float, height: float, title: str) -> Tuple[int, int, int, int, int]:
        window = self.user32.GetForegroundWindow()
        name = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(window, name, len(name))
        if name.value != "Chrome_WidgetWin_1":
            raise RuntimeError("The selected browser must be in the foreground before the cursor can move.")
        caption = ctypes.create_unicode_buffer(2048)
        self.user32.GetWindowTextW(window, caption, len(caption))
        if title and title not in caption.value:
            raise RuntimeError("The foreground browser tab changed before cursor movement.")
        candidates: List[Tuple[float, int, int, int, int]] = []

        def inspect(child: int, data: int) -> bool:
            if data:
                return False
            name = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(child, name, len(name))
            if name.value != "Chrome_RenderWidgetHostHWND" or not self.user32.IsWindowVisible(child):
                return True
            rect = wintypes.RECT()
            origin = wintypes.POINT(0, 0)
            if self.user32.GetClientRect(child, ctypes.byref(rect)) and self.user32.ClientToScreen(child, ctypes.byref(origin)):
                w, h = rect.right, rect.bottom
                if w > 50 and h > 50:
                    error = abs(w / width - h / height) / max(w / width, h / height)
                    candidates.append((error, origin.x, origin.y, w, h))
            return True

        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
        self.user32.EnumChildWindows(window, callback_type(inspect), 0)
        if not candidates or min(candidates)[0] > 0.15:
            raise RuntimeError("The browser viewport could not be mapped to the Windows screen.")
        _, x, y, w, h = min(candidates)
        return window, x, y, w, h

    def move_to(self, arguments: Dict[str, Any], parent_alive: Callable[[], bool]) -> Dict[str, Any]:
        width, height = float(arguments["viewport_width"]), float(arguments["viewport_height"])
        x, y = float(arguments["x"]), float(arguments["y"])
        if not all(math.isfinite(v) for v in (width, height, x, y)) or width <= 0 or height <= 0 or not 0 <= x < width or not 0 <= y < height:
            raise ValueError("Cursor target must be inside the browser viewport.")
        window, left, top, physical_width, physical_height = self.viewport(width, height, str(arguments.get("title", "")))
        if arguments.get("inspect", False):
            current = CursorInfo(size=ctypes.sizeof(CursorInfo))
            if not self.user32.GetCursorInfo(ctypes.byref(current)):
                raise ctypes.WinError(ctypes.get_last_error())
            visible_width, visible_height = self.cursor_bounds(current.cursor)
            return {"visible_width": visible_width, "visible_height": visible_height}
        target_x, target_y = round(left + x * physical_width / width), round(top + y * physical_height / height)
        if arguments.get("screen_x") is not None and arguments.get("screen_y") is not None:
            target_x, target_y = round(arguments["screen_x"]), round(arguments["screen_y"])
            bounds = wintypes.RECT()
            origin = wintypes.POINT(0, 0)
            if not self.user32.GetClientRect(window, ctypes.byref(bounds)) or not self.user32.ClientToScreen(window, ctypes.byref(origin)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not origin.x <= target_x < origin.x + bounds.right or not origin.y <= target_y < origin.y + bounds.bottom:
                raise ValueError("Calibrated cursor target is outside the foreground browser window.")
        start = wintypes.POINT()
        if not self.user32.GetPhysicalCursorPos(ctypes.byref(start)):
            raise ctypes.WinError(ctypes.get_last_error())
        probe = bool(arguments.get("probe", False))
        if probe:
            target_x, target_y = start.x, start.y
        probe_distance = max(2, math.ceil((self.user32.GetDpiForWindow(window) or 96) / 96)) if probe else 0
        distance = math.hypot(target_x - start.x, target_y - start.y)
        steps = 2 if probe else 1 if distance < 2 else max(8, min(20, round(distance / 60)))
        for step in range(1, steps + 1):
            if self.hotkey():
                raise CursorTakeover("Browser use is paused. Press Ctrl+Alt+Space to resume.")
            if not parent_alive() or self.user32.GetForegroundWindow() != window:
                raise CursorTakeover("Browser cursor movement was interrupted.")
            ratio = step / steps
            eased = ratio * ratio * (3 - 2 * ratio)
            next_x = start.x + probe_distance if probe and step == 1 else round(start.x + (target_x - start.x) * eased)
            next_y = round(start.y + (target_y - start.y) * eased)
            if not self.user32.SetPhysicalCursorPos(next_x, next_y):
                raise ctypes.WinError(ctypes.get_last_error())
            if step < steps:
                time.sleep(0.04 if probe else 0.015)
        actual = wintypes.POINT()
        self.user32.GetPhysicalCursorPos(ctypes.byref(actual))
        if abs(actual.x - target_x) > 2 or abs(actual.y - target_y) > 2:
            raise CursorTakeover(f"The Windows pointer did not reach the browser target: wanted ({target_x}, {target_y}), observed ({actual.x}, {actual.y}).")

        return {"screen_x": actual.x, "screen_y": actual.y, "scale_x": physical_width / width, "scale_y": physical_height / height}

    def cursor_bounds(self, cursor: int) -> Tuple[int, int]:
        info = IconInfo()
        if not self.user32.GetIconInfo(cursor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        gdi.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi.GetBitmapBits.argtypes = [wintypes.HBITMAP, wintypes.LONG, ctypes.c_void_p]
        try:
            bitmap = Bitmap()
            if not gdi.GetObjectW(info.color or info.mask, ctypes.sizeof(bitmap), ctypes.byref(bitmap)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.color and bitmap.bpp == 32:
                pixels = ctypes.create_string_buffer(bitmap.stride * bitmap.height)
                if gdi.GetBitmapBits(info.color, len(pixels), pixels) == len(pixels):
                    raw = pixels.raw
                    rows = [row for row in range(bitmap.height) if any(raw[row * bitmap.stride + column * 4 + 3] for column in range(bitmap.width))]
                    if rows:
                        columns = [column for column in range(bitmap.width) if any(raw[row * bitmap.stride + column * 4 + 3] for row in range(bitmap.height))]
                        return max(columns) - min(columns) + 1, max(rows) - min(rows) + 1
            if info.mask:
                mask = Bitmap()
                if not gdi.GetObjectW(info.mask, ctypes.sizeof(mask), ctypes.byref(mask)) or mask.bpp != 1:
                    raise RuntimeError("The Windows cursor mask could not be measured.")
                pixels = ctypes.create_string_buffer(mask.stride * mask.height)
                if gdi.GetBitmapBits(info.mask, len(pixels), pixels) != len(pixels):
                    raise RuntimeError("The Windows cursor mask could not be read.")
                raw = pixels.raw
                height = mask.height if info.color else mask.height // 2
                columns: List[int] = []
                rows: List[int] = []
                for row in range(height):
                    for column in range(mask.width):
                        bit = 0x80 >> (column % 8)
                        opaque = not raw[row * mask.stride + column // 8] & bit
                        inverted = not info.color and bool(raw[(row + height) * mask.stride + column // 8] & bit)
                        if opaque or inverted:
                            columns.append(column)
                            rows.append(row)
                return (max(columns) - min(columns) + 1, max(rows) - min(rows) + 1) if rows else (0, 0)
            raise RuntimeError("The Windows cursor has no measurable pixels or mask.")
        finally:
            for handle in (info.mask, info.color):
                if handle:
                    gdi.DeleteObject(handle)

    def original_size(self) -> int:
        if self._baseline_bounds is None:
            width, height = self.cursor_bounds(self.originals[32512])
            if width <= 0 or height <= 0:
                width = max(1, round((self.user32.GetSystemMetrics(13) or 48) * 3 / 8))
                height = max(1, round((self.user32.GetSystemMetrics(14) or 48) * 9 / 16))
            self._baseline_bounds = width, height
        self.cursor_width, self.cursor_height = self._baseline_bounds
        return max(1, int(min(self.cursor_width * 1.12 * 42 / 32, self.cursor_height * 42 / 38)))

    def register(self) -> None:
        if not self.registered:
            if not self.user32.RegisterHotKey(None, HOTKEY_ID, 0x4003, 0x20):
                raise RuntimeError("Ctrl+Alt+Space is unavailable. Browser use did not take control of the cursor.")
            self.registered = True

    def unregister(self) -> None:
        if self.registered:
            self.user32.UnregisterHotKey(None, HOTKEY_ID)
            self.registered = False

    def activate(self) -> None:
        if self.originals:
            return
        self.register()
        try:
            for cursor_id in CURSOR_IDS:
                original = self.user32.LoadCursorW(None, ctypes.c_void_p(cursor_id))
                if not original:
                    continue
                backup = self.user32.CopyImage(original, 2, 0, 0, 0)
                if not backup:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.originals[cursor_id] = backup
            if not self.originals:
                raise RuntimeError("Windows cursor shapes are unavailable.")
            size = self.original_size()
            self.cursor_size = size
            for cursor_id in self.originals:
                cursor = self.user32.LoadImageW(None, str(self.cursor_file), 2, size, size, 0x10)
                if not cursor:
                    raise ctypes.WinError(ctypes.get_last_error())
                if not self.user32.SetSystemCursor(cursor, cursor_id):
                    self.user32.DestroyCursor(cursor)
                    raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.restore()
            self.unregister()
            raise

    def restore(self) -> None:
        arrow = self.originals.get(32512)
        if arrow and self.cursor_bounds(arrow) == (0, 0):
            self.reload_configured_cursors()
            for original in self.originals.values():
                self.user32.DestroyCursor(original)
            self.originals.clear()
            return
        failures: List[int] = []
        for cursor_id, original in list(self.originals.items()):
            cursor = self.user32.CopyImage(original, 2, 0, 0, 0)
            if not cursor or not self.user32.SetSystemCursor(cursor, cursor_id):
                if cursor:
                    self.user32.DestroyCursor(cursor)
                failures.append(cursor_id)
                continue
            self.user32.DestroyCursor(original)
            del self.originals[cursor_id]
        if failures:
            raise RuntimeError(f"Could not restore Windows cursor shapes: {failures}")

    def reload_configured_cursors(self) -> None:
        if not self.user32.SystemParametersInfoW(0x0057, 0, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def hotkey(self) -> bool:
        message = wintypes.MSG()
        pressed = False
        while self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
            if message.message == 0x0312 and message.wParam == HOTKEY_ID:
                pressed = True
        return pressed


def emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload), flush=True)


def read_commands(commands: "queue.Queue[Optional[Dict[str, Any]]]") -> None:
    try:
        for line in sys.stdin:
            commands.put(json.loads(line))
    finally:
        commands.put(None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    arguments = parser.parse_args()
    native = WindowsCursor(Path(__file__).with_name("assets") / "saarthi-pointer.cur")
    parent = native.kernel32.OpenProcess(0x00100000, False, arguments.parent_pid)
    if not parent:
        raise RuntimeError("The desktop backend is no longer running.")
    commands: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue()
    threading.Thread(target=read_commands, args=(commands,), daemon=True).start()
    active = False
    paused = False
    emit({"event": "ready", "thread_id": native.kernel32.GetCurrentThreadId()})
    try:
        while native.kernel32.WaitForSingleObject(parent, 0) == 0x102:
            if native.hotkey():
                paused = not paused
                if paused:
                    native.restore()
                elif active:
                    native.activate()
                else:
                    native.unregister()
                emit({"event": "paused" if paused else "resumed"})
            try:
                command = commands.get(timeout=0.02)
            except queue.Empty:
                continue
            if command is None:
                break
            try:
                result: Dict[str, Any] = {}
                if command["action"] == "begin":
                    if paused:
                        raise RuntimeError("Browser use is paused. Press Ctrl+Alt+Space to resume.")
                    native.activate()
                    active = True
                elif command["action"] == "finish":
                    active = False
                    native.restore()
                    if not paused:
                        native.unregister()
                elif command["action"] == "pause":
                    native.register()
                    paused = True
                    native.restore()
                    emit({"event": "paused"})
                elif command["action"] == "resume":
                    paused = False
                    if active:
                        native.activate()
                    else:
                        native.unregister()
                    emit({"event": "resumed"})
                elif command["action"] == "move":
                    if paused or not active:
                        raise RuntimeError("Browser cursor movement is not active.")
                    result = native.move_to(command, lambda: native.kernel32.WaitForSingleObject(parent, 0) == 0x102)
                else:
                    raise ValueError("Unknown cursor command.")
                emit({"id": command["id"], "ok": True, "cursor_size": native.cursor_size, "cursor_width": native.cursor_width, "cursor_height": native.cursor_height, **result})
            except CursorTakeover as error:
                paused = True
                native.restore()
                emit({"event": "paused"})
                emit({"id": command["id"], "error": str(error)})
            except Exception as error:
                emit({"id": command["id"], "error": str(error)})
    finally:
        try:
            native.restore()
        finally:
            native.unregister()
            native.kernel32.CloseHandle(parent)


if __name__ == "__main__":
    main()

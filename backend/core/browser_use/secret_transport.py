import base64
import ctypes
import hashlib
import json
import secrets
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Dict


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


_consumed: Dict[str, float] = {}
_lock = threading.Lock()


def _protect(data: bytes, entropy: bytes, decrypt: bool = False) -> bytes:
    if sys.platform != "win32":
        raise ValueError("Protected local secret transport requires Windows.")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    source_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(entropy)
    source = DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    extra = DataBlob(len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = DataBlob()
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p if decrypt else wintypes.LPCWSTR, ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None if decrypt else "Neo Saarthi local task", ctypes.byref(extra), None, None, 1, ctypes.byref(output)):
        raise ValueError("Protected local task could not be processed.")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def seal_local_task(payload: Dict[str, Any], token: str) -> str:
    content = {"version": 1, "expires": time.time() + 120, "nonce": secrets.token_urlsafe(24), "payload": payload}
    raw = json.dumps(content, ensure_ascii=False).encode("utf-8")
    if len(raw) > 32000:
        raise ValueError("Local task is too large.")
    return base64.b64encode(_protect(raw, hashlib.sha256(token.encode()).digest())).decode("ascii")


def open_local_task(sealed: str, token: str) -> Dict[str, Any]:
    try:
        if not isinstance(sealed, str) or len(sealed) > 60000:
            raise ValueError()
        content = json.loads(_protect(base64.b64decode(sealed, validate=True), hashlib.sha256(token.encode()).digest(), True))
        now = time.time()
        if content["version"] != 1 or not now < content["expires"] <= now + 125 or not isinstance(content["nonce"], str) or len(content["nonce"]) < 24 or not isinstance(content["payload"], dict) or content["payload"].get("sealed_task"):
            raise ValueError()
        with _lock:
            for nonce, expiry in list(_consumed.items()):
                if expiry < now:
                    del _consumed[nonce]
            if content["nonce"] in _consumed:
                raise ValueError()
            _consumed[content["nonce"]] = content["expires"]
        return content["payload"]
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ValueError("Protected local task is invalid, expired or already used.") from None

import json
import os
import struct
import sys
from pathlib import Path
from typing import Any, Dict

import httpx
from cryptography.fernet import InvalidToken

try:
    import msvcrt
except ImportError:
    msvcrt = None


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if len(sys.argv) > 1:
    os.environ["COMPANION_DATA_DIR"] = sys.argv[1]

from companion import runtime


ORIGIN = "chrome-extension://polchmmnbnamnpkoiipldgdngmdllgbc/"


def read_request() -> Dict[str, Any]:
    if len(sys.argv) < 3 or sys.argv[2] != ORIGIN:
        raise ValueError("Unauthorized extension origin.")
    header = sys.stdin.buffer.read(4)
    if len(header) != 4:
        raise ValueError("Invalid message header.")
    size = struct.unpack("=I", header)[0]
    if not 1 <= size <= 4096:
        raise ValueError("Message exceeds the native request limit.")
    payload = sys.stdin.buffer.read(size)
    if len(payload) != size:
        raise ValueError("Incomplete native message.")
    request = json.loads(payload)
    if not isinstance(request, dict) or type(request.get("v")) is not int or request.get("v") != 1 or request.get("op") not in {"connect", "status"}:
        raise ValueError("Invalid native operation.")
    allowed = {"v", "op", "nonce", "client_id", "browser_family"} if request["op"] == "connect" else {"v", "op", "nonce"}
    if set(request) != allowed or not isinstance(request["nonce"], str) or len(request["nonce"]) != 36:
        raise ValueError("Invalid native request fields.")
    return request


def main() -> None:
    if msvcrt:
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    response: Dict[str, Any] = {"error": "Start Neo Saarthi Browser on this computer."}
    try:
        request = read_request()
        response["nonce"] = request["nonce"]
        token = runtime.CIPHER.decrypt((runtime.DATA / "native/auth.enc").read_bytes()).decode("ascii")
        with httpx.Client(timeout=8, trust_env=False) as client:
            result = client.post("http://127.0.0.1:8000/api/companion/native", json=request, headers={"X-Neo-Native": token})
        if result.status_code == 403:
            response["error"] = "Automatic connection is disabled or this browser was disconnected in Neo Saarthi."
        elif result.is_success:
            response = result.json()
    except (OSError, ValueError, RecursionError, InvalidToken, httpx.HTTPError):
        pass
    output = json.dumps(response).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("=I", len(output)) + output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()

import json
import os
import secrets
import sys
import threading
from pathlib import Path
from typing import Any, Dict

from cryptography.fernet import Fernet
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("COMPANION_DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
BACKEND = ROOT / "backend"
OWNER = "local-browser-owner"
SESSION = secrets.token_urlsafe(32)
SETTINGS_LOCK = threading.RLock()
SETTING_NAMES = {
    "opencode_api_key", "cams_email", "karvy_username", "karvy_password", "report_password"
}


def prepare() -> None:
    os.chdir(ROOT)
    sys.path.insert(0, str(BACKEND))
    DATA.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        DATA.chmod(0o700)
    load_dotenv(ROOT / ".env", override=False)
    os.environ["MF_RUN_LOG_DIR"] = str(DATA / "logs")
    os.environ["COMPANION_DATA_DIR"] = str(DATA)
    os.environ["APP_ENV"] = "production"
    os.environ.setdefault("OPENCODE_API_KEY", "")
    os.environ.setdefault("JEV_DECIDER_ENGINE", "hybrid")
    os.environ.setdefault("NEO_SAARTHI_NATIVE_BROWSER_CURSOR", "1" if sys.platform == "win32" else "0")


prepare()

from core.browser_use.secret_transport import _protect


def encryption_key() -> bytes:
    path = DATA / "settings.key"
    if path.exists():
        raw = path.read_bytes()
        return _protect(raw, b"neo-saarthi-browser-companion", True) if sys.platform == "win32" else raw
    key = Fernet.generate_key()
    raw = _protect(key, b"neo-saarthi-browser-companion") if sys.platform == "win32" else key
    path.write_bytes(raw)
    if sys.platform != "win32":
        path.chmod(0o600)
    return key


KEY = encryption_key()
os.environ["ENCRYPTION_KEY"] = KEY.decode("ascii")
CIPHER = Fernet(KEY)


def read_settings() -> Dict[str, str]:
    with SETTINGS_LOCK:
        path = DATA / "settings.enc"
        if not path.exists():
            return {}
        return json.loads(CIPHER.decrypt(path.read_bytes()))


def save_settings(updates: Dict[str, str]) -> Dict[str, Any]:
    if set(updates) - SETTING_NAMES:
        raise ValueError("Unknown setting.")
    if any(len(value) > 4096 or "\x00" in value for value in updates.values()):
        raise ValueError("Setting is too long or contains an invalid character.")
    with SETTINGS_LOCK:
        values = read_settings()
        values.update(updates)
        path = DATA / "settings.enc"
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(CIPHER.encrypt(json.dumps(values).encode("utf-8")))
        if sys.platform != "win32":
            temporary.chmod(0o600)
        temporary.replace(path)
        if "opencode_api_key" in updates:
            os.environ["OPENCODE_API_KEY"] = values.get("opencode_api_key", "")
        return settings_status()


def settings_status() -> Dict[str, Any]:
    values = read_settings()
    return {
        "configured": {name: bool(values.get(name)) for name in sorted(SETTING_NAMES)},
        "model_key_available": bool(values.get("opencode_api_key") or os.environ.get("OPENCODE_API_KEY")),
        "secret_entry_platform": sys.platform == "win32",
    }


saved = read_settings()
if saved.get("opencode_api_key"):
    os.environ["OPENCODE_API_KEY"] = saved["opencode_api_key"]
del saved

import json
import secrets
import sys
from pathlib import Path
from typing import Dict

try:
    import winreg
except ImportError:
    winreg = None

from companion import runtime


HOST = "com.neo_saarthi.browser"
ORIGIN = "chrome-extension://polchmmnbnamnpkoiipldgdngmdllgbc/"
TOKEN = secrets.token_urlsafe(48)


def register_host() -> None:
    if winreg is None:
        raise RuntimeError("Automatic browser connection currently requires Windows.")
    directory = runtime.DATA / "native"
    directory.mkdir(exist_ok=True)
    launcher = directory / "neo-saarthi-host.bat"
    manifest = directory / (HOST + ".json")
    command = f'@echo off\r\n"{sys.executable}" -I "{runtime.ROOT / "companion/native_host.py"}" "{runtime.DATA}" %*\r\n'
    launcher.write_bytes(command.encode("utf-8"))
    configuration: Dict[str, object] = {"name": HOST, "description": "Neo Saarthi local browser connection", "path": str(launcher), "type": "stdio", "allowed_origins": [ORIGIN]}
    manifest.write_text(json.dumps(configuration), encoding="utf-8")
    (directory / "auth.enc").write_bytes(runtime.CIPHER.encrypt(TOKEN.encode("ascii")))
    for browser in (("Google", "Chrome"), ("Microsoft", "Edge"), ("Chromium",), ("Google", "ChromeForTesting")):
        key_path = chr(92).join(("Software", *browser, "NativeMessagingHosts", HOST))
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as existing:
                previous = Path(winreg.QueryValueEx(existing, "")[0])
            if previous.resolve() != manifest.resolve():
                raise RuntimeError("Another Neo Saarthi Browser installation owns automatic connection. Close it and use its configured folder.")
        except FileNotFoundError:
            pass
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest))

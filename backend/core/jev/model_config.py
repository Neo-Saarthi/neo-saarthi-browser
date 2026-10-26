import os
from pathlib import Path
from typing import Tuple
from urllib.parse import urlsplit


BROWSER_MODEL = "deepseek-v4.1-flash"


def browser_model_config() -> Tuple[str, str, str]:
    if not (os.environ.get("OPENCODE_API_KEY") or os.environ.get("TEXT_MODEL_API_KEY")):
        for path in (Path(__file__).resolve().parents[3] / ".env",):
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        name, value = line.split("=", 1)
                        os.environ.setdefault(name.strip(), value.strip())
                break
    base_url = (
        os.environ.get("BROWSER_MODEL_BASE_URL")
        or os.environ.get("OPENCODE_BASE_URL")
        or os.environ.get("OPENCODE_ZEN_GO_BASE_URL")
        or "https://opencode.ai/zen/go/v1"
    ).rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Browser AI requires an HTTPS model endpoint without embedded credentials, query or fragment.")
    key = os.environ.get("OPENCODE_API_KEY") or os.environ.get("TEXT_MODEL_API_KEY") or ""
    if not key:
        raise ValueError("Browser DeepSeek requires OPENCODE_API_KEY or TEXT_MODEL_API_KEY.")
    return base_url, BROWSER_MODEL, key

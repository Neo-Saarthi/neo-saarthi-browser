import importlib
import json
import os
import runpy
import sys
import threading
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
REPORT = Path(os.environ["COMPANION_PROVIDER_REPORT"])
LOCK = threading.Lock()
sys.path.insert(0, str(ROOT))


def observe(response: httpx.Response) -> None:
    request = response.request
    try:
        model = json.loads(request.content).get("model")
    except (ValueError, AttributeError):
        model = None
    record = {"model": model, "status": response.status_code, "host": request.url.host, "path": request.url.path}
    with LOCK:
        with REPORT.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")


def instrument(module: Any) -> None:
    module.CLIENT.event_hooks["response"].append(observe)


from companion import runtime
from companion.app import app


for name in ("decide", "core.jev.decide", "text", "core.jev.text", "core.jev.llm_decider"):
    instrument(importlib.import_module(name))

if __name__ == "__main__":
    if not app or runtime.ROOT != ROOT:
        raise RuntimeError("Wrong standalone source.")
    runpy.run_path(str(ROOT / "run.py"), run_name="__main__")

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]


def verify(original: str) -> Dict[str, Any]:
    provenance = json.loads((ROOT / "provenance.json").read_text(encoding="utf-8"))
    copied = 0
    adapted = 0
    for relative, metadata in provenance.items():
        file = ROOT / relative
        if metadata["mode"] == "copied":
            if hashlib.sha256(file.read_bytes()).hexdigest() != metadata["sha256"]:
                raise AssertionError(f"Copied file changed: {relative}")
            copied += 1
        else:
            adapted += 1
    checked = 0
    if original:
        baseline = json.loads((ROOT / "test-results/original-source-baseline.json").read_text(encoding="utf-8"))
        for relative, expected in baseline.items():
            source = Path(original) / relative
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise AssertionError(f"Original source changed: {relative}")
            checked += 1
    with tempfile.TemporaryDirectory(prefix="independent-browser-copy-") as directory:
        destination = Path(directory) / "standalone"
        shutil.copytree(ROOT, destination, ignore=shutil.ignore_patterns(".venv", "data", "test-results", "__pycache__", "test-*.txt", ".git", ".env", ".env.local"))
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["COMPANION_DATA_DIR"] = str(Path(directory) / "data")
        environment["NEO_SAARTHI_NATIVE_BROWSER_CURSOR"] = "0"
        script = "import sys,json;from pathlib import Path;root=Path(sys.argv[1]).resolve();sys.path.insert(0,str(root));from companion.app import app;from companion import runtime;from core.jev import loop;from core.browser_use.broker import browser_use_broker;bad={name:str(module.__file__) for name,module in list(sys.modules.items()) if name.startswith(('core.','api.','db.','services.','schemas.','companion.')) and getattr(module,'__file__',None) and not Path(module.__file__).resolve().is_relative_to(root)};assert not bad,bad;assert runtime.ROOT==root;assert not browser_use_broker is None;assert app;assert loop;print('ISOLATED_IMPORTS_PASS')"
        result = subprocess.run([sys.executable, "-I", "-c", script, str(destination)], cwd=directory, env=environment, capture_output=True, text=True, timeout=30)
        if result.returncode or "ISOLATED_IMPORTS_PASS" not in result.stdout:
            raise AssertionError(result.stdout + result.stderr)
    report = {"unchanged_copies": copied, "necessary_adapters": adapted, "original_files_verified": checked, "isolated_imports": True, "extension_files": sum(name.startswith("extension/") for name in provenance)}
    (ROOT / "test-results").mkdir(exist_ok=True)
    (ROOT / "test-results/independence.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", default="")
    arguments = parser.parse_args()
    print(json.dumps(verify(arguments.original), indent=2))

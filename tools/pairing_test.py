import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

import httpx
from playwright.sync_api import sync_playwright

from extension_package import ExtensionPackage
from live_test import BASE, EXTENSION_ID, ROOT, ToolbarPopup, request, wait_ready


def main(source: str) -> None:
    results = ROOT / "test-results"
    results.mkdir(exist_ok=True)
    report: Dict[str, Any] = {"tasks_run": False, "financial_sites_tested": False}
    package = Path(source)
    original_hash = hashlib.sha256(package.read_bytes()).hexdigest() if package.is_file() else None
    with ExtensionPackage(source) as extension, tempfile.TemporaryDirectory(prefix="neo-pairing-") as directory:
        environment = dict(os.environ)
        environment["COMPANION_DATA_DIR"] = str(Path(directory) / "data")
        environment["NEO_SAARTHI_NATIVE_BROWSER_CURSOR"] = "0"
        with (results / "pairing-server.log").open("wb") as log, httpx.Client(timeout=40) as client, sync_playwright() as playwright:
            process = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=environment, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            context = None
            try:
                wait_ready(client, process)
                arguments = [f"--disable-extensions-except={extension}", f"--load-extension={extension}", "--no-first-run", "--enable-unsafe-extension-debugging"]
                profile = str(Path(directory) / "chrome")
                context = playwright.chromium.launch_persistent_context(profile, channel="chromium", headless=False, args=arguments)
                page = context.new_page()
                page.goto(BASE)
                popup = ToolbarPopup(context, page)
                popup.wait("document.querySelector('#status').textContent !== 'Checking connectionâ€¦'")
                popup.evaluate("document.querySelector('#consent').checked=true;document.querySelector('#accept-consent').click();true")
                popup.wait("!document.querySelector('#pairing').hidden")
                popup.evaluate("document.querySelector('#pairing-code').value='invalid-test-code';document.querySelector('#connect').click();true")
                popup.wait("document.querySelector('#error').textContent.length > 0 && !document.querySelector('#connect').disabled")
                assert not request(client, "/api/companion/status")["browser"]["connected"]
                report["invalid_code_rejected"] = True
                code = request(client, "/api/v1/browser-use/pairing", "POST")["pairing_code"]
                popup.evaluate("document.querySelector('#pairing-code').value=" + json.dumps(code) + ";document.querySelector('#connect').click();true")
                popup.wait("document.querySelector('#status').textContent.includes('Connected')")
                state = request(client, "/api/companion/status")
                browser_id = next(item["client_id"] for item in state["browser"]["clients"] if item["connected"])
                report["fresh_popup_pairing"] = True
                response = client.post("http://127.0.0.1:41691/api/v1/browser-use/bridge/connect", json={"pairing_code": code}, headers={"Origin": "chrome-extension://" + EXTENSION_ID})
                assert response.status_code >= 400, response.status_code
                report["used_code_rejected"] = True
                popup.close()
                process.terminate()
                process.wait(timeout=15)
                process = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=environment, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                wait_ready(client, process)
                page.reload()
                popup = ToolbarPopup(context, page)
                popup.wait("document.querySelector('#status').textContent.includes('Connected')")
                assert any(item["client_id"] == browser_id and item["connected"] for item in request(client, "/api/companion/status")["browser"]["clients"])
                report["backend_restart_reconnect"] = True
                popup.close()
                context.close()
                context = playwright.chromium.launch_persistent_context(profile, channel="chromium", headless=False, args=arguments)
                page = context.new_page()
                page.goto(BASE)
                popup = ToolbarPopup(context, page)
                popup.wait("document.querySelector('#status').textContent.includes('Connected')")
                assert any(item["client_id"] == browser_id and item["connected"] for item in request(client, "/api/companion/status")["browser"]["clients"])
                report["browser_restart_reconnect"] = True
                request(client, "/api/v1/browser-use/connections/revoke", "POST", {"client_id": browser_id})
                popup.close()
                popup = ToolbarPopup(context, page)
                popup.wait("!document.querySelector('#pairing').hidden")
                report["revocation_clears_pairing"] = True
                for attempt in range(2):
                    code = request(client, "/api/v1/browser-use/pairing", "POST")["pairing_code"]
                    popup.evaluate("document.querySelector('#pairing-code').value=" + json.dumps(code) + ";document.querySelector('#connect').click();true")
                    popup.wait("document.querySelector('#status').textContent.includes('Connected')")
                    assert request(client, "/api/companion/status")["browser"]["connected"]
                    if attempt == 0:
                        state = request(client, "/api/companion/status")
                        browser_id = next(item["client_id"] for item in state["browser"]["clients"] if item["connected"])
                        request(client, "/api/v1/browser-use/connections/revoke", "POST", {"client_id": browser_id})
                        popup.close()
                        popup = ToolbarPopup(context, page)
                        popup.wait("!document.querySelector('#pairing').hidden")
                report["successful_pairings"] = 3
                report["repaired_after_revocation"] = True
                if original_hash:
                    assert hashlib.sha256(package.read_bytes()).hexdigest() == original_hash
                report["source_zip_unchanged"] = True
                report["passed"] = True
            finally:
                if context:
                    context.close()
                process.terminate()
                process.wait(timeout=15)
                (results / "pairing-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extension-package", required=True)
    main(parser.parse_args().extension_package)

import argparse
import functools
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright

from extension_package import ExtensionPackage


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RESULTS = ROOT / "test-results"
RESULTS.mkdir(exist_ok=True)
load_dotenv(ROOT / ".env", override=False)
BASE = "http://127.0.0.1:8000"
EXTENSION_ID = "polchmmnbnamnpkoiipldgdngmdllgbc"


def request(client: httpx.Client, path: str, method: str = "GET", body: Optional[Dict[str, Any]] = None) -> Any:
    response = client.request(method, BASE + path, json=body)
    response.raise_for_status()
    return response.json() if response.content else None


def wait_ready(client: httpx.Client, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Companion exited before startup; inspect server.log.")
        try:
            response = client.get(BASE + "/api/companion/session")
            if response.is_success:
                client.headers["X-Companion-Session"] = response.json()["session"]
                return
        except httpx.HTTPError:
            pass
        time.sleep(.2)
    raise RuntimeError("Companion did not start in 35 seconds.")


def wait_task(client: httpx.Client, task_id: str, page: Page) -> Dict[str, Any]:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = request(client, "/api/companion/status")
        task = next(task for task in state["tasks"] if task["id"] == task_id)
        if task["status"] != "running":
            return task
        page.wait_for_timeout(400)
    raise RuntimeError("Task did not finish within 180 seconds.")


class ToolbarPopup:
    def __init__(self, context: Any, page: Page) -> None:
        self.page = page
        self.root = context.browser.new_browser_cdp_session()
        tabs = self.root.send("Target.getTargets", {"filter": [{"type": "tab", "exclude": False}, {"exclude": True}]})["targetInfos"]
        tab = next(tab for tab in tabs if tab["url"].rstrip("/") == BASE)
        self.root.send("Extensions.triggerAction", {"id": EXTENSION_ID, "targetId": tab["targetId"]})
        self.target = ""
        for attempt in range(50):
            targets = self.root.send("Target.getTargets")["targetInfos"]
            popup = next((target for target in targets if target["url"].endswith("/popup/popup.html")), None)
            if popup:
                self.target = popup["targetId"]
                break
            page.wait_for_timeout(100)
        if not self.target:
            raise RuntimeError("Toolbar popup target did not open.")
        self.session = self.root.send("Target.attachToTarget", {"targetId": self.target, "flatten": False})["sessionId"]
        self.responses: Dict[int, Any] = {}
        self.sequence = 0
        self.root.on("Target.receivedMessageFromTarget", self.received)
        self.wait("document.readyState === 'complete' && Boolean(document.querySelector('#connect'))")

    def received(self, event: Dict[str, Any]) -> None:
        message = json.loads(event["message"])
        if "id" in message:
            self.responses[message["id"]] = message

    def evaluate(self, expression: str) -> Any:
        self.sequence += 1
        identity = self.sequence
        self.root.send("Target.sendMessageToTarget", {"sessionId": self.session, "message": json.dumps({"id": identity, "method": "Runtime.evaluate", "params": {"expression": expression, "awaitPromise": True, "returnByValue": True}})})
        deadline = time.monotonic() + 30
        while identity not in self.responses and time.monotonic() < deadline:
            self.page.wait_for_timeout(50)
        response = self.responses.pop(identity)
        if response.get("error") or response.get("result", {}).get("exceptionDetails"):
            raise RuntimeError(str(response))
        return response.get("result", {}).get("result", {}).get("value")

    def wait(self, expression: str) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            self.page.wait_for_timeout(100)
        raise RuntimeError("Toolbar popup timed out: " + str(self.evaluate("document.body.innerText")))

    def close(self) -> None:
        self.root.send("Target.closeTarget", {"targetId": self.target})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not any(item["targetId"] == self.target for item in self.root.send("Target.getTargets")["targetInfos"]):
                return
            self.page.wait_for_timeout(50)
        raise RuntimeError("Toolbar popup did not close.")


def main(extension_package: str) -> None:
    if not os.environ.get("OPENCODE_API_KEY"):
        raise RuntimeError("Live tests require your OpenCode API key in the environment.")
    report: Dict[str, Any] = {"financial_sites_tested": False, "mocked_provider_calls": False, "native_cursor": True, "tasks": []}
    with ExtensionPackage(extension_package) as extension, tempfile.TemporaryDirectory(prefix="neo-companion-live-") as directory:
        environment = dict(os.environ)
        environment["COMPANION_DATA_DIR"] = str(Path(directory) / "data")
        environment["NEO_SAARTHI_NATIVE_BROWSER_CURSOR"] = "1"
        environment["COMPANION_PROVIDER_REPORT"] = str(RESULTS / "providers.jsonl")
        (RESULTS / "providers.jsonl").write_text("")
        fixture = ThreadingHTTPServer(("127.0.0.1", 8011), functools.partial(SimpleHTTPRequestHandler, directory=str(ROOT / "tests/fixtures")))
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        with (RESULTS / "server.log").open("wb") as log:
            process = subprocess.Popen([sys.executable, "tools/e2e_server.py"], cwd=ROOT, env=environment, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                with httpx.Client(timeout=45) as client, sync_playwright() as playwright:
                    wait_ready(client, process)
                    context = playwright.chromium.launch_persistent_context(str(Path(directory) / "chrome"), channel="chromium", headless=False, viewport={"width": 1250, "height": 850}, args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}", "--no-first-run", "--enable-unsafe-extension-debugging"])
                    try:
                        ui = context.new_page()
                        ui.goto(BASE)
                        ui.wait_for_selector("#pair")
                        ui.locator("#pair").click()
                        ui.wait_for_function("() => document.querySelector('#pairing-code').textContent.length > 7")
                        code = ui.locator("#pairing-code").inner_text()
                        popup = ToolbarPopup(context, ui)
                        popup.wait("document.querySelector('#status').textContent !== 'Checking connectionÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦'")
                        popup.evaluate("document.querySelector('#consent').checked=true;document.querySelector('#accept-consent').click();true")
                        popup.wait("!document.querySelector('#pairing').hidden")
                        popup.evaluate("document.querySelector('#pairing-code').value=" + json.dumps(code) + ";document.querySelector('#connect').click();true")
                        popup.wait("document.querySelector('#status').textContent.includes('Connected')")
                        status = request(client, "/api/companion/status")
                        browser_id = status["browser"]["clients"][0]["client_id"]
                        assert status["browser"]["connected"]
                        assert status["cursor"]["enabled"]
                        report["paired_through_popup"] = True
                        request(client, "/api/companion/action", "POST", {"action": "set_permission", "arguments": {"origin": "http://127.0.0.1:8011", "mode": "allow"}})
                        popup.close()
                        ui.bring_to_front()
                        ui.wait_for_function("() => !document.querySelector('#run').disabled", timeout=15000)
                        goal = "Open http://127.0.0.1:8011/everyday.html and type Buy notebooks into the Note field, then click Save note. Finish when the page says Note saved: Buy notebooks."
                        ui.locator("#goal").fill(goal)
                        ui.locator("#budget").fill("12")
                        ui.locator("#run").click()
                        ui.wait_for_function("() => document.querySelector('#task-state').textContent === 'running'", timeout=15000)
                        task_id = request(client, "/api/companion/status")["tasks"][-1]["id"]
                        task = wait_task(client, task_id, ui)
                        assert task["status"] == "done", task
                        target = next(page for page in context.pages if "8011/everyday.html" in page.url)
                        assert target.locator("#saved").inner_text() == "Note saved: Buy notebooks"
                        report["tasks"].append({"task": "write and save note", "status": task["status"], "elapsed_seconds": task["result"]["elapsed_seconds"], "actual_confirmation": target.locator("#saved").inner_text()})
                        target.screenshot(path=str(RESULTS / "note-result.png"))
                        ui.bring_to_front()
                        ui.wait_for_function("() => !document.querySelector('#run').disabled", timeout=10000)
                        ui.locator("#goal").fill("Open http://127.0.0.1:8011/everyday.html and type Pack the charger into the Note field, then click Save note. Finish when the page says Note saved: Pack the charger.")
                        ui.locator("#run").click()
                        ui.wait_for_function("() => document.querySelector('#task-state').textContent === 'running'")
                        current = request(client, "/api/companion/status")["tasks"][-1]
                        deadline = time.monotonic() + 30
                        while time.monotonic() < deadline:
                            pages = [page for page in context.pages if "8011/everyday.html" in page.url and page != target]
                            if pages:
                                break
                            ui.wait_for_timeout(100)
                        assert pages, "Second task page did not open."
                        second = pages[-1]
                        deadline = time.monotonic() + 25
                        while time.monotonic() < deadline:
                            if any('Task Step 1/' in line for line in request(client, "/api/companion/logs")["lines"]):
                                break
                            ui.wait_for_timeout(100)
                        request(client, "/api/companion/control", "POST", {"client_id": browser_id, "paused": True})
                        before = second.evaluate("({changes:window.changes,value:document.querySelector('#note').value,saved:document.querySelector('#saved').textContent})")
                        second.wait_for_timeout(2500)
                        after = second.evaluate("({changes:window.changes,value:document.querySelector('#note').value,saved:document.querySelector('#saved').textContent})")
                        assert before == after, (before, after)
                        paused = request(client, "/api/companion/status")
                        assert paused["cursor"]["native_paused"]
                        ui.bring_to_front()
                        ui.wait_for_function("() => !document.querySelector('#resume').disabled", timeout=10000)
                        ui.locator("#resume").click()
                        second.bring_to_front()
                        completed = wait_task(client, current["id"], second)
                        assert completed["id"] == current["id"] and completed["status"] == "done", completed
                        assert second.locator("#saved").inner_text() == "Note saved: Pack the charger"
                        report["tasks"].append({"task": "pause and resume same note task", "status": completed["status"], "same_task_id": True, "unchanged_while_paused": True, "elapsed_seconds": completed["result"]["elapsed_seconds"]})
                        ui.bring_to_front()
                        ui.wait_for_timeout(1800)
                        ui.screenshot(path=str(RESULTS / "ui-desktop.png"), full_page=True)
                        ui.set_viewport_size({"width": 390, "height": 844})
                        assert ui.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        ui.screenshot(path=str(RESULTS / "ui-mobile.png"), full_page=True)
                        providers = [json.loads(line) for line in (RESULTS / "providers.jsonl").read_text().splitlines()]
                        report["provider_requests"] = providers
                        assert any(item["model"] == "jev-1.13" and item["status"] == 200 for item in providers), providers
                        assert any(item["model"] == "deepseek-v4.1-flash" and item["status"] == 200 for item in providers), providers
                        assert not any(str(item["model"]).startswith("muse") for item in providers)
                        report["passed"] = True
                    finally:
                        context.close()
            finally:
                process.terminate()
                process.wait(timeout=15)
                fixture.shutdown()
                fixture.server_close()
                thread.join(timeout=5)
                (RESULTS / "live-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extension-package", required=True, help="Existing extension ZIP or unpacked directory")
    main(parser.parse_args().extension_package)

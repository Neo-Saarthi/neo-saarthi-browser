import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from companion import runtime
from companion.app import app, task_hints
from core.browser_use.broker import BrowserUseBroker
from core.browser_use.cursor import browser_cursor
from db.local_store.workspace_db import connect, initialize_database


class CompanionSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000")
        self.addAsyncCleanup(self.client.aclose)

    async def test_no_login_but_ui_mutations_require_local_session(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("What would you like to do?", response.text)
        response = await self.client.post("/api/v1/browser-use/pairing")
        self.assertEqual(response.status_code, 401)
        session = (await self.client.get("/api/companion/session")).json()["session"]
        response = await self.client.post("/api/v1/browser-use/pairing", headers={"X-Companion-Session": session})
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.json()["pairing_code"]), 8)

    async def test_cross_origin_and_dns_rebinding_are_rejected(self) -> None:
        for headers in ({"Origin": "https://evil.example"}, {"Host": "evil.example:8000"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                response = await self.client.get("/api/companion/session", headers=headers)
                self.assertEqual(response.status_code, 403)

    async def test_extension_routes_require_exact_id_and_bridge_port(self) -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:41691") as client:
            for origin in ("https://evil.example", "chrome-extension://" + "a" * 32):
                response = await client.post("/api/v1/browser-use/bridge/connect", json={"pairing_code": "invalid-code"}, headers={"Origin": origin})
                self.assertEqual(response.status_code, 403)
            response = await client.options("/api/v1/browser-use/bridge/connect", headers={"Origin": "chrome-extension://polchmmnbnamnpkoiipldgdngmdllgbc"})
            self.assertEqual(response.status_code, 204)
            response = await client.get("/")
            self.assertEqual(response.status_code, 403)

    async def test_oversized_requests_rejected(self) -> None:
        response = await self.client.put("/api/companion/settings", content="x" * 64001, headers={"X-Companion-Session": runtime.SESSION, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)

    async def test_pairing_is_single_use_and_job_protocol_survives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            broker = BrowserUseBroker(Path(directory) / "browser.sqlite3")
            pairing = await broker.issue_pairing_code(runtime.OWNER)
            connection = await broker.connect(pairing["pairing_code"], client_id="fixture-browser")
            with self.assertRaisesRegex(RuntimeError, "invalid or expired"):
                await broker.connect(pairing["pairing_code"])
            pending = asyncio.create_task(broker.dispatch(runtime.OWNER, "browser_info", {}, client_id="fixture-browser"))
            job = await broker.next_job(connection["bridge_token"], wait_seconds=1)
            self.assertIsNotNone(job)
            await broker.submit_result(connection["bridge_token"], job["id"], {"fixture": True}, None)
            self.assertEqual(await pending, {"fixture": True})
            await broker.connection_control(connection["bridge_token"], True)
            self.assertTrue((await broker.connection_control(connection["bridge_token"]))["paused"])
            await broker.connection_control(connection["bridge_token"], False)
            await broker.revoke_connection(runtime.OWNER, "fixture-browser")
            with self.assertRaises(RuntimeError):
                await broker.next_job(connection["bridge_token"], wait_seconds=.1)

    async def test_settings_are_encrypted_and_values_are_not_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(runtime, "DATA", Path(directory)):
            response = await self.client.put("/api/companion/settings", json={"values": {"karvy_password": "test-only-secret", "cams_email": "fixture@example.org"}}, headers={"X-Companion-Session": runtime.SESSION})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("test-only-secret", response.text)
            self.assertNotIn(b"test-only-secret", (Path(directory) / "settings.enc").read_bytes())
            self.assertEqual(runtime.read_settings()["karvy_password"], "test-only-secret")
            self.assertEqual(task_hints("none"), {})
            self.assertEqual(task_hints("karvy")["password"], "test-only-secret")
            self.assertEqual(task_hints("cams")["email"], "fixture@example.org")

    async def test_companion_pause_retains_polling_and_resume_reuses_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            broker = BrowserUseBroker(Path(directory) / "browser.sqlite3")
            pairing = await broker.issue_pairing_code(runtime.OWNER)
            connection = await broker.connect(pairing["pairing_code"], client_id="pause-fixture")
            pending = asyncio.create_task(broker.dispatch(runtime.OWNER, "browser_info", {}, client_id="pause-fixture"))
            job = await broker.next_job(connection["bridge_token"], wait_seconds=1)
            self.assertIsNotNone(job)
            headers = {"X-Companion-Session": runtime.SESSION}
            try:
                with patch("companion.app.browser_use_broker", broker):
                    response = await self.client.post("/api/companion/control", json={"client_id": "pause-fixture", "paused": True}, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()["native_paused"])
                    self.assertFalse(response.json()["connection_paused"])
                    with self.assertRaisesRegex(RuntimeError, "paused"):
                        await pending
                    state = await broker.status(runtime.OWNER)
                    self.assertFalse(state["clients"][0]["paused"])
                    self.assertTrue(state["clients"][0]["connected"])
                    response = await self.client.post("/api/companion/control", json={"client_id": "pause-fixture", "paused": False}, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(response.json()["paused"])
                    await broker.connection_control(connection["bridge_token"], True)
                    response = await self.client.post("/api/companion/control", json={"client_id": "pause-fixture", "paused": False}, headers=headers)
                    self.assertEqual(response.status_code, 409)
                    self.assertIn("extension popup", response.json()["detail"])
            finally:
                browser_cursor.set_paused(False)

    async def test_one_task_at_a_time_and_settings_locked_during_run(self) -> None:
        with patch("companion.app.WORKERS", {"fixture": Mock(done=Mock(return_value=False))}):
            headers = {"X-Companion-Session": runtime.SESSION}
            response = await self.client.post("/api/companion/tasks", json={"goal": "Open example.org"}, headers=headers)
            self.assertEqual(response.status_code, 409)
            response = await self.client.put("/api/companion/settings", json={"values": {}}, headers=headers)
            self.assertEqual(response.status_code, 409)


class StorageTests(unittest.TestCase):
    def test_only_browser_tables_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.sqlite3"
            initialize_database(path)
            database = connect(path)
            tables = [row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            database.close()
            self.assertEqual(len(tables), 6)
            self.assertTrue(all(name.startswith("browser_use_") for name in tables))

    def test_source_contains_no_original_project_path(self) -> None:
        for directory in ("backend", "companion", "ui"):
            for path in (runtime.ROOT / directory).rglob("*"):
                if path.suffix in {".py", ".js", ".html", ".css"}:
                    self.assertNotIn("mf-saarthi/", path.read_text(encoding="utf-8").lower(), str(path))
                    self.assertNotIn("mf-saarthi" + chr(92), path.read_text(encoding="utf-8").lower(), str(path))

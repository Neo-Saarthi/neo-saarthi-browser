import asyncio
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]
TEST_DATA = tempfile.TemporaryDirectory(prefix="neo-connection-check-")
os.environ["COMPANION_DATA_DIR"] = TEST_DATA.name
os.environ["NEO_SAARTHI_NATIVE_BROWSER_CURSOR"] = "0"
sys.path.insert(0, str(ROOT))

from companion import runtime
from companion.app import WORKERS, app
from companion.native_bridge import TOKEN
from core.browser_use.broker import browser_use_broker
from core.browser_use.cursor import browser_cursor
from db.local_store.workspace_db import initialize_database


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        initialize_database()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000")
        self.addAsyncCleanup(self.client.aclose)
        self.ui = {"X-Companion-Session": runtime.SESSION}
        self.native = {"X-Neo-Native": TOKEN}
        runtime.save_settings({"saved_password_entry": "disabled", "browser_auto_connect": "enabled"})
        browser_cursor.set_paused(False)

    def payload(self) -> Dict[str, Any]:
        return {"v": 1, "op": "connect", "nonce": str(uuid4()), "client_id": str(uuid4()), "browser_family": "chrome"}

    async def test_native_auth_is_not_ui_auth_and_rejects_web_origins(self) -> None:
        for headers in ({}, self.ui, {**self.native, "Origin": "https://evil.example"}, {**self.native, "Host": "evil.example:8000"}):
            response = await self.client.post("/api/companion/native", json=self.payload(), headers=headers)
            self.assertEqual(response.status_code, 403)
        response = await self.client.put("/api/companion/settings", json={"values": {}}, headers=self.native)
        self.assertEqual(response.status_code, 401)

    async def test_bootstrap_connects_and_unknown_fields_are_rejected(self) -> None:
        body = self.payload()
        response = await self.client.post("/api/companion/native", json=body, headers=self.native)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["nonce"], body["nonce"])
        self.assertTrue(response.json()["connection"]["bridge_token"])
        for changes in ({"op": "execute"}, {"url": "https://evil.example"}, {"client_id": "invalid"}, {"browser_family": "unknown"}):
            response = await self.client.post("/api/companion/native", json={**body, **changes}, headers=self.native)
            self.assertEqual(response.status_code, 422)

    async def test_http_auto_connect_and_wrong_extension_are_rejected(self) -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:41691") as bridge:
            for origin in ("chrome-extension://polchmmnbnamnpkoiipldgdngmdllgbc", "chrome-extension://" + "a" * 32):
                response = await bridge.post("/api/v1/browser-use/bridge/auto-connect", json={}, headers={"Origin": origin})
                self.assertEqual(response.status_code, 403)

    async def test_consent_starts_off_and_is_immediately_revocable(self) -> None:
        body = {"v": 1, "op": "status", "nonce": str(uuid4())}
        response = await self.client.post("/api/companion/native", json=body, headers=self.native)
        self.assertFalse(response.json()["secret_entry_enabled"])
        for enabled in (True, False):
            response = await self.client.put("/api/companion/settings", json={"values": {}, "password_entry_allowed": enabled}, headers=self.ui)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["password_entry_enabled"], enabled)
            response = await self.client.post("/api/companion/native", json=body, headers=self.native)
            self.assertEqual(response.json()["secret_entry_enabled"], enabled)
        response = await self.client.put("/api/companion/settings", json={"values": {"saved_password_entry": "enabled"}}, headers=self.ui)
        self.assertEqual(response.status_code, 400)

    async def test_disconnect_blocks_rebootstrap_until_explicit_reenable(self) -> None:
        body = self.payload()
        connected = (await self.client.post("/api/companion/native", json=body, headers=self.native)).json()["connection"]
        response = await self.client.post("/api/companion/connection", json={"enabled": False}, headers=self.ui)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["auto_connect_enabled"])
        response = await self.client.post("/api/companion/native", json=body, headers=self.native)
        self.assertEqual(response.status_code, 403)
        with self.assertRaises(RuntimeError):
            await browser_use_broker.connection_control(connected["bridge_token"])
        await self.client.post("/api/companion/connection", json={"enabled": True}, headers=self.ui)
        response = await self.client.post("/api/companion/native", json=body, headers=self.native)
        self.assertEqual(response.status_code, 200)

    async def test_password_revocation_during_a_task_stops_native_input(self) -> None:
        pending = asyncio.get_running_loop().create_future()
        WORKERS["consent-fixture"] = pending
        try:
            response = await self.client.put("/api/companion/settings", json={"values": {}, "password_entry_allowed": True}, headers=self.ui)
            self.assertEqual(response.status_code, 409)
            response = await self.client.put("/api/companion/settings", json={"values": {}, "password_entry_allowed": False}, headers=self.ui)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["password_entry_enabled"])
            self.assertTrue(browser_cursor.paused.is_set())
        finally:
            WORKERS.pop("consent-fixture", None)
            pending.cancel()
            browser_cursor.set_paused(False)

    async def test_pause_retains_connection_and_resume_handles_legacy_extension_pause(self) -> None:
        connected = (await self.client.post("/api/companion/native", json=self.payload(), headers=self.native)).json()["connection"]
        for paused in (True, False):
            response = await self.client.post("/api/companion/control", json={"client_id": connected["client_id"], "paused": paused}, headers=self.ui)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["native_paused"], paused)
            self.assertFalse(response.json()["connection_paused"])
        await browser_use_broker.connection_control(connected["bridge_token"], True)
        response = await self.client.post("/api/companion/control", json={"client_id": connected["client_id"], "paused": False}, headers=self.ui)
        self.assertEqual(response.status_code, 200)
        self.assertFalse((await browser_use_broker.connection_control(connected["bridge_token"]))["paused"])

    async def test_native_framing_rejects_wrong_origin_oversize_and_unknown_fields(self) -> None:
        for origin, message in (("chrome-extension://" + "a" * 32 + "/", b"{}"), ("chrome-extension://polchmmnbnamnpkoiipldgdngmdllgbc/", b"x" * 4097), ("chrome-extension://polchmmnbnamnpkoiipldgdngmdllgbc/", b'{"v":1,"op":"shell"}')):
            framed = struct.pack("=I", len(message)) + message
            result = await asyncio.to_thread(subprocess.run, [sys.executable, "-I", str(ROOT / "companion/native_host.py"), TEST_DATA.name, origin], input=framed, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(struct.unpack("=I", result.stdout[:4])[0], len(result.stdout) - 4)
            self.assertIn(b'"error"', result.stdout[4:])


if __name__ == "__main__":
    unittest.main(verbosity=2)

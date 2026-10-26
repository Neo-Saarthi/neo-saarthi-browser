import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import Mock, patch

import httpx

from core.browser_use import windows_secret_input as native
from core.browser_use.broker import BrowserConnection, BrowserUseBroker
from core.browser_use.command_validation import validate_browser_command
from core.browser_use.secret_entry import SecretEntryError, secret_failure_reason, secret_purpose, task_secrets
from core.browser_use.secret_transport import open_local_task, seal_local_task
from core.jev import decide, logger, loop
from core.jev.saarthi_browser import SaarthiRecipeBrowser
from services import jev_service


CAMS_URL = "https://www.camsonline.com/Distributors/Service-Requests/Distributor-Mailback-Services/Request-Mailback/WBR2"


class SecretEntryTests(unittest.TestCase):
    def test_preflight_requires_consent_and_reports_old_extension_before_navigation(self) -> None:
        for info, reason in (({}, "extension_update_required"), ({"secret_entry_enabled": False}, "consent_required"), ({"secret_entry_enabled": True}, None)):
            browser = SaarthiRecipeBrowser.__new__(SaarthiRecipeBrowser)
            browser._dispatch = Mock(return_value=info)
            scope = task_secrets.set({"report_password": "Fixture@Secret!"})
            try:
                with patch.object(jev_service.loop, "find_site", return_value=True) as navigate, patch("core.jev.saarthi_browser.logger.get_logger", return_value=Mock()):
                    if reason:
                        with self.assertRaisesRegex(SecretEntryError, reason):
                            jev_service._find_site_with_resume(browser, "fixture")
                        navigate.assert_not_called()
                    else:
                        self.assertTrue(jev_service._find_site_with_resume(browser, "fixture"))
                        navigate.assert_called_once()
                browser._dispatch.assert_called_once_with("browser_info", {})
            finally:
                task_secrets.reset(scope)

    def test_failed_stages_have_safe_reasons_and_never_log_raw_errors(self) -> None:
        browser = SaarthiRecipeBrowser.__new__(SaarthiRecipeBrowser)
        for stage, message, reason in (("prepare", "Enable saved-password entry in the extension before this task.", "consent_required"), ("native_input", "Browser focus changed before saved-password entry.", "focus_changed"), ("verify", "Unexpected Fixture@Secret!", "unclassified_failure")):
            log = Mock()
            with patch("core.jev.saarthi_browser.logger.get_logger", return_value=log):
                with self.assertRaises(SecretEntryError) as caught:
                    browser._secret_stage(stage, Mock(side_effect=RuntimeError(message)))
            self.assertEqual(caught.exception.stage, stage)
            self.assertEqual(secret_failure_reason(caught.exception), reason)
            self.assertNotIn("Fixture@Secret!", str(log.method_calls) + str(caught.exception))
            self.assertIn(f"stage={stage} reason={reason}", str(log.warn.call_args))

    def test_preflight_pause_resumes_without_losing_secret_context(self) -> None:
        browser = SaarthiRecipeBrowser.__new__(SaarthiRecipeBrowser)
        browser._dispatch = Mock(side_effect=[RuntimeError("Browser Use is paused."), {"secret_entry_enabled": True}])
        browser.wait_until_resumed = Mock(return_value=True)
        scope = task_secrets.set({"report_password": "Fixture@Secret!"})
        try:
            with patch.object(jev_service.loop, "find_site", return_value=True) as navigate, patch("core.jev.saarthi_browser.logger.get_logger", return_value=Mock()):
                self.assertTrue(jev_service._find_site_with_resume(browser, "original goal"))
            browser.wait_until_resumed.assert_called_once()
            navigate.assert_called_once_with(browser, "original goal")
            self.assertEqual(task_secrets.get()["report_password"], "Fixture@Secret!")
        finally:
            task_secrets.reset(scope)

    def test_safe_failure_stage_is_persisted_in_text_and_json_logs(self) -> None:
        browser = SaarthiRecipeBrowser.__new__(SaarthiRecipeBrowser)
        with tempfile.TemporaryDirectory() as directory, patch.object(logger, "LOGS_DIR", directory):
            log = logger.AgentLogger("local-diagnostic-fixture")
            try:
                with patch("core.jev.saarthi_browser.logger.get_logger", return_value=log):
                    with self.assertRaises(SecretEntryError):
                        browser._secret_stage("native_input", Mock(side_effect=RuntimeError("Browser focus changed before saved-password entry.")))
                for path in (log.log_path, log.json_path):
                    content = Path(path).read_text(encoding="utf-8")
                    self.assertIn("stage=native_input reason=focus_changed", content)
                    self.assertNotIn("Fixture@Secret!", content)
            finally:
                log.close()

    def test_preflight_stop_returns_needs_input_with_zero_steps(self) -> None:
        browser = SimpleNamespace(_tab_id=None, url="", close=Mock(), check_saved_secret_access=Mock(side_effect=SecretEntryError("preflight", "consent_required")))
        scope = task_secrets.set({"report_password": "Fixture@Secret!"})
        try:
            with patch.object(jev_service, "SaarthiRecipeBrowser", return_value=browser), patch.object(jev_service.logger, "AgentLogger", return_value=Mock()), patch.object(jev_service.loop, "find_site") as navigate:
                result = jev_service._execute_jev_sync("CAMS fixture", 10, "fixture", None, True, "client", Mock())
            self.assertEqual(result["status"], "needs_input")
            self.assertEqual(result["steps"], 0)
            self.assertIn("consent_required", result["message"])
            navigate.assert_not_called()
            browser.close.assert_called_once()
        finally:
            task_secrets.reset(scope)

    def test_website_echoed_secret_is_removed_before_jev_request(self) -> None:
        secret = "Fixture@Secret!"
        logger.set_sensitive_values([secret])
        try:
            response = httpx.Response(200, json={"answers": {}})
            with patch.object(decide.CLIENT, "post", return_value=response) as post:
                decide._post({"page_text": f"Website echoed {secret}"})
            self.assertNotIn(secret, json.dumps(post.call_args.kwargs["json"]))
            self.assertIn("********", post.call_args.kwargs["json"]["page_text"])
        finally:
            logger.set_sensitive_values([])

    def test_task_scope_keeps_values_local_and_restores_recipe_after_completion(self) -> None:
        secret = "Fixture@Secret!"
        recipe = SimpleNamespace(field_hints={"report_password": "original"})

        def execute(goal: str, *arguments: Any) -> Dict[str, str]:
            self.assertNotIn(secret, goal)
            self.assertTrue(arguments)
            self.assertEqual(recipe.field_hints["report_password"], "[saved secret]")
            self.assertEqual(task_secrets.get()["report_password"], secret)
            return {"status": "done"}

        with patch.object(jev_service, "find_recipe", return_value=recipe), patch.object(jev_service, "_execute_jev_sync", side_effect=execute):
            result = jev_service._run_isolated(f"CAMS report using {secret}", 10, "fixture-owner", None, {"report_password": secret}, True, "fixture-client", Mock())
        self.assertEqual(result["status"], "done")
        self.assertEqual(recipe.field_hints, {"report_password": "original"})
        self.assertEqual(task_secrets.get(), {})

    def test_secret_preparation_cannot_carry_password_or_executable_payloads(self) -> None:
        args = {"bridge_protocol": "jev", "node": 42, "purpose": "report_password", "expected_url": CAMS_URL}
        validate_browser_command("prepare_secret", args)
        for key in ("text", "value", "password", "script"):
            with self.assertRaises(ValueError):
                validate_browser_command("prepare_secret", {**args, key: "Fixture@Secret!"})

    def test_dpapi_protects_payload_and_rejects_replay_tampering_and_wrong_token(self) -> None:
        payload = {"goal": "fixture", "field_hints": {"report_password": "Fixture@Secret!"}}
        sealed = seal_local_task(payload, "fixture-token")
        self.assertNotIn("Fixture@Secret!", sealed)
        with self.assertRaises(ValueError):
            open_local_task(sealed, "different-token")
        self.assertEqual(open_local_task(sealed, "fixture-token"), payload)
        with self.assertRaises(ValueError):
            open_local_task(sealed, "fixture-token")
        with self.assertRaises(ValueError):
            open_local_task(sealed[:-8] + "AAAAAAAA", "fixture-token")
        with patch("core.browser_use.secret_transport.time.time", return_value=0):
            expired = seal_local_task(payload, "fixture-token")
        with self.assertRaises(ValueError):
            open_local_task(expired, "fixture-token")

    def test_origins_and_purposes_are_exact_and_other_sensitive_fields_are_denied(self) -> None:
        field = {"label": "Enter Password", "input_type": "password"}
        self.assertEqual(secret_purpose(CAMS_URL, field), "report_password")
        self.assertEqual(secret_purpose("https://dss.kfintech.com/dssweb/", field), "password")
        self.assertEqual(secret_purpose("https://dss.kfintech.com/dssweb/report", {"label": "Confirm report password"}), "report_password")
        for url in (CAMS_URL.replace("https", "http"), CAMS_URL.replace("www.camsonline.com", "www.camsonline.com.evil.example"), CAMS_URL.replace("www.camsonline.com", "www.camsonline.com:444"), "https://example.org/"):
            self.assertIsNone(secret_purpose(url, field))
        for label in ("OTP password", "Payment password", "Card password", "Verification code"):
            self.assertIsNone(secret_purpose(CAMS_URL, {"label": label, "input_type": "password"}))

    def test_secret_value_never_enters_extension_arguments(self) -> None:
        browser = SaarthiRecipeBrowser.__new__(SaarthiRecipeBrowser)
        browser._last = {"url": CAMS_URL}
        browser._elements = {"1": {"node": 42, "label": "Enter Password", "input_type": "password"}}
        browser._dispatch = Mock(side_effect=[{"url": CAMS_URL, "purpose": "report_password", "element_id": "fixture", "ready": True}, {"filled": True}])
        scope = task_secrets.set({"report_password": "Fixture@Secret!"})
        try:
            with patch("core.jev.saarthi_browser.fill_focused_secret") as fill, patch("core.jev.saarthi_browser.browser_cursor.ensure_allowed"):
                self.assertIn("verified", browser.fill_saved_secret("1"))
            self.assertEqual(fill.call_args.args[1], "Fixture@Secret!")
            self.assertNotIn("Fixture@Secret!", str(browser._dispatch.call_args_list))
        finally:
            task_secrets.reset(scope)

    def test_native_target_checks_fail_closed_before_setting_a_value(self) -> None:
        target = {"url": CAMS_URL, "element_id": "fixture-password", "ready": True}
        pattern = SimpleNamespace(CurrentIsReadOnly=False, SetValue=Mock())
        document_pattern = SimpleNamespace(CurrentValue=CAMS_URL)
        document = SimpleNamespace(CurrentControlType=native.UIA.UIA_DocumentControlTypeId, GetCurrentPattern=Mock(return_value=SimpleNamespace(QueryInterface=lambda interface: document_pattern)))
        element = SimpleNamespace(CurrentControlType=native.UIA.UIA_EditControlTypeId, CurrentIsEnabled=True, CurrentAutomationId="fixture-password", GetCurrentPattern=Mock(return_value=SimpleNamespace(QueryInterface=lambda interface: pattern)))
        automation = SimpleNamespace(GetFocusedElement=Mock(return_value=element), ControlViewWalker=SimpleNamespace(GetParentElement=Mock(return_value=document)), CompareElements=Mock(return_value=True))
        user32 = SimpleNamespace(GetForegroundWindow=Mock(return_value=123), GetClassNameW=Mock(side_effect=lambda window, output, size: setattr(output, "value", "Chrome_WidgetWin_1")))
        with patch.object(native, "CreateObject", return_value=automation), patch.object(native.ctypes, "WinDLL", return_value=user32):
            native.fill_focused_secret(target, "Fixture@Secret!", Mock())
            pattern.SetValue.assert_called_once_with("Fixture@Secret!")
            pattern.SetValue.reset_mock()
            for change in ("focus", "origin", "readonly", "pause"):
                with self.subTest(change=change):
                    element.CurrentAutomationId = "other" if change == "focus" else "fixture-password"
                    document_pattern.CurrentValue = "https://evil.example/" if change == "origin" else CAMS_URL
                    pattern.CurrentIsReadOnly = change == "readonly"
                    allowed = Mock(side_effect=RuntimeError("Browser Use is paused.")) if change == "pause" else Mock()
                    with self.assertRaises(RuntimeError):
                        native.fill_focused_secret(target, "Fixture@Secret!", allowed)
                    pattern.SetValue.assert_not_called()

    def test_loop_uses_saved_secret_and_stops_immediately_when_authorization_is_missing(self) -> None:
        field: Dict[str, Any] = {"index": "1", "node": 1, "label": "Enter Password", "role": "textbox", "isPassword": True}
        page = {"url": CAMS_URL, "title": "Fixture", "elements": [field], "text": "Enter Password"}
        for denied in (False, True):
            browser = SimpleNamespace(url=CAMS_URL, get_element=lambda index: field, fill_saved_secret=Mock(side_effect=RuntimeError("Enable saved-password entry") if denied else None, return_value="presence verified"), task_history=[])
            with ExitStack() as stack:
                stack.enter_context(patch.object(loop.logger, "get_logger", return_value=Mock()))
                stack.enter_context(patch.object(loop, "find_recipe", return_value=None))
                stack.enter_context(patch.object(loop.trajectory, "load_trajectory", return_value=None))
                stack.enter_context(patch.object(loop.trajectory, "save_or_optimize_trajectory"))
                stack.enter_context(patch.object(loop, "snap_or_recover", return_value=(page, 0)))
                stack.enter_context(patch.object(loop, "bot_wall", return_value=False))
                stack.enter_context(patch.object(loop.decide, "choose", side_effect=[{"operation": "TYPE_TEXT", "index": "1", "value": "InventedSecret!"}, {"operation": "DONE"}]))
                text = stack.enter_context(patch.object(loop.text, "field_text"))
                result = loop.run_task(browser, "Enter saved report password", 3)
            self.assertEqual(result.startswith("needs_input:"), denied)
            browser.fill_saved_secret.assert_called_once_with("1")
            text.assert_not_called()
            self.assertNotIn("InventedSecret!", json.dumps(browser.task_history))

    def test_secret_pause_retains_task_and_does_not_fall_back_to_plain_fill(self) -> None:
        field = {"index": "1", "node": 1, "label": "Password", "role": "textbox", "isPassword": True}
        page = {"url": CAMS_URL, "title": "Fixture", "elements": [field], "text": "Password"}
        browser = SimpleNamespace(url=CAMS_URL, get_element=lambda index: field, fill_saved_secret=Mock(side_effect=[RuntimeError("Browser Use is paused."), "already populated"]), wait_until_resumed=Mock(return_value=True), task_history=[])
        with ExitStack() as stack:
            stack.enter_context(patch.object(loop.logger, "get_logger", return_value=Mock()))
            stack.enter_context(patch.object(loop, "find_recipe", return_value=None))
            stack.enter_context(patch.object(loop.trajectory, "load_trajectory", return_value=None))
            stack.enter_context(patch.object(loop.trajectory, "save_or_optimize_trajectory"))
            stack.enter_context(patch.object(loop, "snap_or_recover", return_value=(page, 0)))
            stack.enter_context(patch.object(loop, "bot_wall", return_value=False))
            stack.enter_context(patch.object(loop.decide, "choose", side_effect=[{"operation": "TYPE_TEXT", "index": "1"}, {"operation": "TYPE_TEXT", "index": "1"}, {"operation": "DONE"}]))
            plain_fill = stack.enter_context(patch.object(loop, "act_with_recovery"))
            self.assertEqual(loop.run_task(browser, "Use saved password", 3), "done")
        plain_fill.assert_not_called()
        browser.wait_until_resumed.assert_called_once()
        self.assertEqual([event["kind"] for event in browser.task_history], ["interrupted", "SECRET_INPUT"])


class SecretConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_pause_revoke_and_wrong_owner_block_native_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            broker = BrowserUseBroker(Path(directory) / "workspace.db")
            now = datetime.now(timezone.utc)
            connection = BrowserConnection("client", "owner", "token", "Fixture", "edge", None, now, now)
            broker._connections_by_token["token"] = connection
            broker._tokens_by_distributor["owner"] = {"client": "token"}
            with patch("core.browser_use.broker.browser_cursor.ensure_allowed"):
                await broker.ensure_task_input_allowed("owner", "client")
                connection.paused = True
                with self.assertRaisesRegex(RuntimeError, "paused"):
                    await broker.ensure_task_input_allowed("owner", "client")
                connection.paused = False
                with self.assertRaisesRegex(RuntimeError, "connection changed"):
                    await broker.ensure_task_input_allowed("other-owner", "client")
                broker._connections_by_token.clear()
                with self.assertRaisesRegex(RuntimeError, "connection changed"):
                    await broker.ensure_task_input_allowed("owner", "client")

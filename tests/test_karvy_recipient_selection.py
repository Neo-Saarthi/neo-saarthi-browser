import os
import asyncio
import json
import unittest
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

from playwright.sync_api import sync_playwright

from core.browser_use.command_validation import validate_browser_command
from core.browser_use.recipient_selection import RecipientSelectionError
from core.jev import loop
from core.jev.recipes.kfintech import KFintechRecipe
from core.jev.saarthi_browser import SaarthiRecipeBrowser


REPORT_URL = "https://dss.kfintech.com/dssweb/ReqTransactionReport"
TARGET_EMAIL = "wealth+ops@example.org"


class RecipientRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recipe = KFintechRecipe()
        self.context: Dict[str, Any] = {}
        self.recipe.build_milestones(f"KFintech MFSD201 set From Date 01-01-1990 select {TARGET_EMAIL} and submit report", self.context)
        self.page = {"url": REPORT_URL, "text": "Report form", "elements": [
            {"index": "fund", "node": 1, "role": "combobox", "label": "Select Fund", "value": "All Funds"},
            {"index": "date", "node": 2, "role": "textbox", "label": "From Date", "value": "01/01/1990"},
            {"index": "csv", "node": 3, "role": "radio", "label": "CSV", "checked": "true"},
            {"index": "wrong", "node": 4, "role": "checkbox", "label": "[redacted email]", "checked": "false"},
            {"index": "correct", "node": 5, "role": "checkbox", "label": "[redacted email]", "checked": "false"},
            {"index": "password", "node": 6, "role": "textbox", "label": "Self Extraction Password", "value": "[redacted sensitive field]"},
            {"index": "confirm", "node": 7, "role": "textbox", "label": "Self Extraction Confirm Password", "value": "[redacted sensitive field]"},
            {"index": "submit", "node": 8, "role": "button", "label": "Submit"},
        ]}
        self.status = {"matches": 1, "match_node": 5, "checked": False, "disabled": False, "other_checked_nodes": []}
        self.browser = SimpleNamespace(recipient_status=Mock(return_value=self.status))

    def recover(self) -> Dict[str, Any]:
        return self.recipe.handle_recovery(self.browser, self.page, [], self.context)

    def test_redacted_choices_match_locally_without_first_recipient_fallback(self) -> None:
        action = self.recover()
        self.assertEqual(action["index"], "correct")
        self.browser.recipient_status.assert_called_once_with(TARGET_EMAIL)
        self.assertNotIn(TARGET_EMAIL, json.dumps(action))
        self.assertNotIn("submitted_201", self.context)

    def test_missing_ambiguous_disabled_and_stale_matches_block_submit(self) -> None:
        for changes in ({"matches": 0}, {"matches": 2}, {"disabled": True}, {"match_node": None}, {"match_node": 999}):
            with self.subTest(changes=changes):
                self.browser.recipient_status.return_value = {**self.status, **changes}
                self.assertEqual(self.recover()["operation"], "NEEDS_INPUT")
                self.assertNotIn("submitted_201", self.context)

    def test_wrong_checked_recipient_is_removed_before_selecting_target(self) -> None:
        self.status["other_checked_nodes"] = [4]
        action = self.recover()
        self.assertEqual(action["index"], "wrong")
        self.assertEqual(action["reason"], "uncheck unintended recipient")

    def test_only_verified_recipient_allows_submit_and_success_sets_pending_state(self) -> None:
        self.status["checked"] = True
        action = self.recover()
        self.assertEqual(action["index"], "submit")
        self.assertNotIn("submitted_201", self.context)
        self.recipe.record_action_success(action, self.context)
        self.assertTrue(self.context["submitted_201"])

    def test_lost_selection_is_rechecked_and_pause_is_propagated(self) -> None:
        self.status["checked"] = True
        self.assertEqual(self.recover()["index"], "submit")
        self.status["checked"] = False
        self.assertEqual(self.recover()["index"], "correct")
        self.browser.recipient_status.side_effect = RuntimeError("Browser Use is paused.")
        with self.assertRaisesRegex(RuntimeError, "paused"):
            self.recover()
        self.assertNotIn("submitted_201", self.context)

    def test_missing_matcher_fails_closed_and_guard_error_is_not_retried(self) -> None:
        self.browser = SimpleNamespace()
        self.assertEqual(self.recover()["operation"], "NEEDS_INPUT")
        browser = SimpleNamespace(act=Mock(side_effect=RecipientSelectionError("recipient not checked")), snapshot=Mock())
        with patch.object(loop.logger, "get_logger", return_value=Mock()):
            _, error, _ = loop.act_with_recovery(browser, "CLICK", "submit", "Submit", None)
        self.assertIsInstance(error, RecipientSelectionError)
        browser.act.assert_called_once()
        browser.snapshot.assert_not_called()

    def test_recipient_command_accepts_only_bounded_metadata(self) -> None:
        args = {"bridge_protocol": "jev", "recipient_email": TARGET_EMAIL}
        validate_browser_command("recipient_status", args)
        for change in ({"value": "extra"}, {"recipient_email": "bad"}, {"recipient_email": "x" * 255 + "@example.org"}):
            with self.assertRaises(ValueError):
                validate_browser_command("recipient_status", {**args, **change})
        validate_browser_command("recipient_status", {"bridge_protocol": "jev", "recipient_mode": "any"})
        with self.assertRaises(ValueError):
            validate_browser_command("recipient_status", {"bridge_protocol": "jev", "recipient_mode": "all"})

    def test_available_recipient_goal_uses_any_mode_without_requiring_email(self) -> None:
        self.recipe.build_milestones("KFintech MFSD201 select one available registered email address and submit report From Date 01-01-1990", self.context)
        self.assertEqual(self.context["recipient_mode"], "any")
        self.context["target_email"] = ""
        action = self.recover()
        self.assertEqual(action["index"], "correct")
        self.assertEqual(action["reason"], "select available recipient")
        self.browser.recipient_status.assert_called_once_with("", mode="any")
        self.status["matches"] = 0
        self.assertEqual(self.recover()["operation"], "NEEDS_INPUT")

    def test_submit_pause_resumes_with_context_and_guard_failure_never_marks_submitted(self) -> None:
        for interrupted in (True, False):
            self.setUp()
            self.status["checked"] = True
            browser = SimpleNamespace(url=REPORT_URL, task_history=[], task_steps=0, recipient_status=self.browser.recipient_status, get_element=lambda index: next((element for element in self.page["elements"] if element["index"] == index), None), wait_until_resumed=Mock(return_value=True), act=Mock(side_effect=[RuntimeError("Browser Use is paused."), "clicked"] if interrupted else RecipientSelectionError("Report submission blocked: recipient unchecked")))
            contexts = []
            recover = self.recipe.handle_recovery

            def recovery(current: Any, page: Dict[str, Any], history: Any, context: Dict[str, Any]) -> Dict[str, Any]:
                contexts.append(context)
                if context.get("submitted_201"):
                    return {"operation": "NEEDS_INPUT", "reason": "Local fixture complete"}
                return recover(current, page, history, context)

            with ExitStack() as stack:
                stack.enter_context(patch.object(loop.logger, "get_logger", return_value=Mock()))
                stack.enter_context(patch.object(loop, "find_recipe", return_value=self.recipe))
                stack.enter_context(patch.object(self.recipe, "handle_recovery", side_effect=recovery))
                record = stack.enter_context(patch.object(self.recipe, "record_action_success", wraps=self.recipe.record_action_success))
                stack.enter_context(patch.object(loop.trajectory, "load_trajectory", return_value=None))
                stack.enter_context(patch.object(loop.trajectory, "save_or_optimize_trajectory"))
                stack.enter_context(patch.object(loop, "snap_or_recover", return_value=(self.page, 0)))
                stack.enter_context(patch.object(loop, "bot_wall", return_value=False))
                stack.enter_context(patch.object(loop.llm_decider, "verify_action_result", return_value={"confirmed": True}))
                result = loop.run_task(browser, f"KFintech MFSD201 set From Date 01-01-1990 select {TARGET_EMAIL} and submit report", 4)
            self.assertTrue(result.startswith("needs_input:"))
            if interrupted:
                browser.wait_until_resumed.assert_called_once()
                record.assert_called_once()
                self.assertTrue(contexts[-1]["submitted_201"])
                self.assertEqual(contexts[-1]["target_email"], TARGET_EMAIL)
            else:
                record.assert_not_called()
                self.assertNotIn("submitted_201", contexts[-1])
                browser.act.assert_called_once()


class RecipientDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_any_mode_survives_dispatch_without_sending_an_exact_email(self) -> None:
        browser = SaarthiRecipeBrowser("fixture-owner", "fixture-client", asyncio.get_running_loop())
        with patch("core.jev.saarthi_browser.browser_use_broker.dispatch", new=AsyncMock(return_value={"matches": 1, "recipient_count": 2, "other_checked_nodes": []})) as dispatch, patch("core.jev.saarthi_browser.logger.get_logger", return_value=Mock()):
            await asyncio.to_thread(browser.recipient_status, "", "any")
            self.assertEqual(dispatch.call_args.args[2]["recipient_mode"], "any")
            for action in ("click", "click_at", "press"):
                await asyncio.to_thread(browser._dispatch, action, {})
                self.assertEqual(dispatch.call_args.args[2]["recipient_mode"], "any")
                self.assertNotIn("recipient_email", dispatch.call_args.args[2])

    async def test_expected_recipient_is_attached_to_click_coordinate_and_key_actions(self) -> None:
        browser = SaarthiRecipeBrowser("fixture-owner", "fixture-client", asyncio.get_running_loop())
        browser._recipient_email = TARGET_EMAIL
        with patch("core.jev.saarthi_browser.browser_use_broker.dispatch", new=AsyncMock(return_value={})) as dispatch, patch("core.jev.saarthi_browser.logger.get_logger", return_value=Mock()):
            for action in ("click", "click_at", "press"):
                await asyncio.to_thread(browser._dispatch, action, {})
                self.assertEqual(dispatch.call_args.args[2]["recipient_email"], TARGET_EMAIL)

    async def test_submit_guard_errors_are_sanitized_and_stop_without_losing_binding(self) -> None:
        browser = SaarthiRecipeBrowser("fixture-owner", "fixture-client", asyncio.get_running_loop())
        browser._recipient_email = TARGET_EMAIL
        with patch("core.jev.saarthi_browser.browser_use_broker.dispatch", new=AsyncMock(side_effect=RuntimeError("Recipient check failed: private@example.org"))):
            with self.assertRaises(RecipientSelectionError) as error:
                await asyncio.to_thread(browser._dispatch, "click", {"node": 8})
        self.assertNotIn("private@example.org", str(error.exception))
        self.assertEqual(browser._recipient_email, TARGET_EMAIL)


@unittest.skipUnless(os.environ.get("NEO_EXTENSION_TEST_DIR"), "Supply --extension-package for extension DOM tests")
class LocalRecipientDomTests(unittest.TestCase):
    def test_real_dom_labels_exact_matching_and_submit_guard_on_local_fixture(self) -> None:
        source = (Path(os.environ["NEO_EXTENSION_TEST_DIR"]) / "background/policy.js").read_text(encoding="utf-8")
        html = '<label><input id="other" type="checkbox" value="other@example.org">Other email</label><label for="target">Wealth+Ops@example.org</label><input id="target" type="checkbox"><button id="submit" type="submit">Submit</button>'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
                page.goto(REPORT_URL)
                page.add_script_tag(content=source)
                page.evaluate("""() => { const inputs = [...document.querySelectorAll('input')]; globalThis.__jevFast = { ids: new WeakMap(inputs.map((element, index) => [element, index + 1])) }; }""")
                status = page.evaluate("email => __neoBrowserPolicy.recipientStatus(email)", TARGET_EMAIL)
                self.assertEqual(status["match_node"], 2)
                self.assertFalse(status["checked"])
                self.assertNotIn("@", json.dumps(status))
                guard = "email => { try { __neoBrowserPolicy.requireRecipient(document.querySelector('#submit'), email); return true; } catch { return false; } }"
                self.assertFalse(page.evaluate(guard, TARGET_EMAIL))
                page.locator("#target").check()
                self.assertTrue(page.evaluate(guard, TARGET_EMAIL))
                page.locator("#other").check()
                self.assertFalse(page.evaluate(guard, TARGET_EMAIL))
                page.locator("#other").uncheck()
                page.locator("#target").uncheck()
                self.assertFalse(page.evaluate(guard, TARGET_EMAIL))
            finally:
                browser.close()

    def test_transparent_case_variant_checkboxes_are_available_as_one_recipient(self) -> None:
        source = (Path(os.environ["NEO_EXTENSION_TEST_DIR"]) / "background/policy.js").read_text(encoding="utf-8")
        html = '<label><input style="opacity:0" id="one" type="checkbox" value="ops@example.org">ops@example.org</label><label><input style="opacity:0" id="two" type="checkbox" value="Ops@example.org">Ops@example.org</label><button id="submit" type="submit">Submit</button>'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
                page.goto(REPORT_URL)
                page.add_script_tag(content=source)
                page.evaluate("() => { __jevFast = { ids: new WeakMap([...document.querySelectorAll('input')].map((element, index) => [element, index + 1])) }; }")
                self.assertEqual(page.evaluate("() => __neoBrowserPolicy.recipientStatus(undefined, 'any').recipient_count"), 2)
                self.assertEqual(page.evaluate("() => __neoBrowserPolicy.recipientStatus(undefined, 'any').match_node"), 1)
                page.locator("#one").check()
                self.assertTrue(page.evaluate("() => __neoBrowserPolicy.recipientStatus(undefined, 'any').checked"))
                self.assertEqual(page.evaluate("() => __neoBrowserPolicy.recipientStatus('ops@example.org').matches"), 2)
                page.evaluate("() => __neoBrowserPolicy.requireRecipient(document.querySelector('#submit'), undefined, false, 'any')")
            finally:
                browser.close()

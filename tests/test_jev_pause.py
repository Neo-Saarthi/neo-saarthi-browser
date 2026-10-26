import unittest
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import Mock, patch

from core.jev import loop
from services import jev_service


class JevPauseTests(unittest.TestCase):
    def test_paused_navigation_does_not_fall_back_to_external_search(self) -> None:
        browser = SimpleNamespace(url="", goto=Mock(side_effect=RuntimeError("Browser use is paused.")))
        with patch.object(loop.logger, "get_logger", return_value=Mock()), patch.object(loop.text, "guess_site") as guess, patch.object(loop.text, "search_query") as search:
            with self.assertRaisesRegex(RuntimeError, "paused"):
                loop.find_site(browser, "Open https://example.org/fixture")
        browser.goto.assert_called_once_with("https://example.org/fixture")
        guess.assert_not_called()
        search.assert_not_called()

    def test_paused_perception_is_not_classified_as_an_empty_page(self) -> None:
        browser = SimpleNamespace(url="https://example.org/fixture", snapshot=Mock(side_effect=RuntimeError("Browser use is paused.")), reload=Mock())
        with patch.object(loop.logger, "get_logger", return_value=Mock()):
            with self.assertRaisesRegex(RuntimeError, "paused"):
                loop.snap_or_recover(browser, 2)
        browser.reload.assert_not_called()

    def test_service_returns_needs_input_when_navigation_is_interrupted(self) -> None:
        browser = SimpleNamespace(url="", _tab_id=123, close=Mock())
        with patch.object(jev_service.logger, "AgentLogger", return_value=Mock()), patch.object(jev_service, "find_recipe", return_value=None), patch.object(jev_service.browser_factory, "get_browser", return_value=browser), patch.object(loop, "find_site", side_effect=RuntimeError("Browser use is paused.")):
            result = jev_service._execute_jev_sync("Complete fixture", 8, "fixture-owner", None)
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["tab_id"], "123")
        self.assertIn("resume window", result["message"])
        browser.close.assert_called_once()

    def test_pause_and_generation_interruption_are_recognized(self) -> None:
        for message in (
            "Browser use is paused. Press Ctrl+Alt+Space to resume.",
            "Browser Use was paused by the distributor.",
            "Browser Use was interrupted. Start a new action after resuming.",
        ):
            self.assertTrue(loop.browser_input_interrupted(RuntimeError(message)))
        self.assertFalse(loop.browser_input_interrupted(RuntimeError("Element is gone")))

    def test_paused_input_does_not_retry_or_resnapshot(self) -> None:
        error = RuntimeError("Browser use is paused. Press Ctrl+Alt+Space to resume.")
        browser = SimpleNamespace(act=Mock(side_effect=error), snapshot=Mock(), find_by_label=Mock())
        with patch.object(loop.logger, "get_logger", return_value=Mock()):
            result = loop.act_with_recovery(browser, "CLICK", "2", "Submit", None)
        self.assertIs(result[1], error)
        browser.act.assert_called_once_with("2", "CLICK", None)
        browser.snapshot.assert_not_called()
        browser.find_by_label.assert_not_called()

    def test_paused_task_requests_user_input_after_one_attempt(self) -> None:
        element: Dict[str, Any] = {"index": "2", "role": "button", "label": "Submit", "operations": ["CLICK"]}
        page: Dict[str, Any] = {"url": "https://example.org/fixture", "title": "Fixture", "text": "Submit", "elements": [element]}
        browser = SimpleNamespace(url=page["url"], title=page["title"], get_element=Mock(return_value=element))
        with patch.object(loop.logger, "get_logger", return_value=Mock()), patch.object(loop, "find_recipe", return_value=None), patch.object(loop.trajectory, "load_trajectory", return_value=None), patch.object(loop, "snap_or_recover", return_value=(page, 0)), patch.object(loop, "bot_wall", return_value=False), patch.object(loop.decide, "choose", return_value={"operation": "CLICK", "index": "2"}) as choose, patch.object(loop, "act_with_recovery", return_value=(None, RuntimeError("Browser Use was paused by the distributor."), "2")) as act:
            result = loop.run_task(browser, "Submit the fixture", 8)
        self.assertTrue(result.startswith("needs_input:"))
        self.assertIn("resume window", result)
        act.assert_called_once()
        choose.assert_called_once()

    def test_stale_element_recovery_still_resolves_a_fresh_target(self) -> None:
        browser = SimpleNamespace(act=Mock(side_effect=[RuntimeError("Element is gone"), "clicked"]), snapshot=Mock(), find_by_label=Mock(return_value="3"))
        with patch.object(loop.logger, "get_logger", return_value=Mock()):
            result = loop.act_with_recovery(browser, "CLICK", "2", "Submit", None)
        self.assertEqual(result, ("clicked", None, "3"))
        self.assertEqual(browser.act.call_count, 2)
        browser.snapshot.assert_called_once()
        browser.find_by_label.assert_called_once_with("Submit", exclude="2")

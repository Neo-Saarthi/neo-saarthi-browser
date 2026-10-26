import asyncio
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import Mock, patch

from core.browser_use.broker import BrowserConnection, BrowserUseBroker
from core.jev import loop
from core.jev.browser_bridge import BridgeBrowser
from services import jev_service


class JevContinuationTests(unittest.TestCase):
    def test_short_interactive_page_is_ready_without_timeout_sleep(self) -> None:
        browser = BridgeBrowser.__new__(BridgeBrowser)
        browser.snapshot = Mock(return_value={"text": "Short form", "elements": [{"index": "1"}]})
        with patch("core.jev.browser_bridge.time.sleep") as sleep:
            self.assertTrue(browser._wait_for_content())
        browser.snapshot.assert_called_once_with(retries=1)
        sleep.assert_not_called()

    def test_paused_snapshot_never_returns_stale_cached_state(self) -> None:
        browser = BridgeBrowser.__new__(BridgeBrowser)
        browser._elements = {"1": {"index": "1"}}
        browser._last = {"text": "Old page"}
        browser._dispatch = Mock(side_effect=RuntimeError("Browser use is paused."))
        with patch("core.jev.browser_bridge.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "paused"):
                browser.snapshot()
        browser._dispatch.assert_called_once()
        sleep.assert_not_called()

    def test_resume_preserves_history_recipe_context_and_budget_without_click_replay(self) -> None:
        field = {"index": "1", "role": "textbox", "label": "Phrase", "operations": ["TYPE_TEXT", "CLICK"]}
        button = {"index": "2", "role": "button", "label": "Submit", "operations": ["CLICK"]}
        page = {"url": "https://example.org/fixture", "title": "Fixture", "text": "Phrase Submit", "elements": [field, button]}
        browser = SimpleNamespace(url=page["url"], title=page["title"], task_history=[], task_steps=0, get_element=lambda index: field if index == "1" else button, wait_until_resumed=Mock(return_value=True), snapshot=Mock(return_value=page))
        contexts: List[Dict[str, Any]] = []

        def recovery(_browser: Any, _page: Dict[str, Any], _history: List[Dict[str, Any]], context: Dict[str, Any]) -> None:
            contexts.append(context)
            context["progress"] = len(contexts)

        recipe = SimpleNamespace(name="fixture", build_milestones=Mock(return_value=[]), should_terminate=Mock(return_value=False), handle_recovery=recovery)
        decisions = [{"operation": "TYPE_TEXT", "index": "1", "value": "fixture phrase"}, {"operation": "CLICK", "index": "2"}, {"operation": "DONE"}]
        seen_histories: List[List[Dict[str, Any]]] = []

        def choose(_page: Dict[str, Any], _goal: str, history: List[Dict[str, Any]], **_options: Any) -> Dict[str, Any]:
            seen_histories.append(list(history))
            return decisions[len(seen_histories) - 1]

        with ExitStack() as stack:
            stack.enter_context(patch.object(loop.logger, "get_logger", return_value=Mock()))
            stack.enter_context(patch.object(loop, "find_recipe", return_value=recipe))
            stack.enter_context(patch.object(loop.trajectory, "load_trajectory", return_value=None))
            stack.enter_context(patch.object(loop.trajectory, "save_or_optimize_trajectory"))
            stack.enter_context(patch.object(loop, "snap_or_recover", return_value=(page, 0)))
            stack.enter_context(patch.object(loop, "bot_wall", return_value=False))
            stack.enter_context(patch.object(loop.decide, "choose", side_effect=choose))
            act = stack.enter_context(patch.object(loop, "act_with_recovery", side_effect=[("typed", None, "1"), (None, RuntimeError("Browser Use was paused."), "2")]))
            result = loop.run_task(browser, "Fill and submit fixture", 2)
        self.assertEqual(result, "done")
        self.assertEqual(act.call_count, 2)
        browser.wait_until_resumed.assert_called_once()
        self.assertEqual([item["kind"] for item in seen_histories[-1]], ["TYPE_TEXT", "interrupted"])
        self.assertTrue(all(context is contexts[0] for context in contexts))
        self.assertEqual(contexts[0]["progress"], 3)
        self.assertEqual(browser.task_steps, 2)

    def test_paused_navigation_resumes_the_same_goal_without_external_fallback(self) -> None:
        browser = SimpleNamespace(wait_until_resumed=Mock(return_value=True))
        with patch.object(loop, "find_site", side_effect=[RuntimeError("Browser use is paused."), True]) as find:
            self.assertTrue(jev_service._find_site_with_resume(browser, "Original goal"))
        self.assertEqual(find.call_count, 2)
        self.assertEqual(find.call_args.args, (browser, "Original goal"))


class ResumeGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_waits_for_native_and_connection_pause_on_the_same_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            broker = BrowserUseBroker(Path(directory) / "workspace.db")
            now = datetime.now(timezone.utc)
            connection = BrowserConnection("client", "owner", "token", "Fixture", "edge", None, now, now, paused=True)
            broker._connections_by_token["token"] = connection
            broker._tokens_by_distributor["owner"] = {"client": "token"}
            native_pause = Event()
            native_pause.set()
            with patch("core.browser_use.broker.browser_cursor", SimpleNamespace(paused=native_pause)):
                waiting = asyncio.create_task(broker.wait_for_task_resume("owner", "client", 1))
                await asyncio.sleep(0)
                connection.paused = False
                await asyncio.sleep(0.12)
                self.assertFalse(waiting.done())
                native_pause.clear()
                self.assertTrue(await waiting)
                self.assertFalse(await broker.wait_for_task_resume("other-owner", "client", 0.01))
                connection.paused = True
                self.assertFalse(await broker.wait_for_task_resume("owner", "client", 0.01))

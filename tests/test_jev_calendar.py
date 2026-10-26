import os
import base64
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from core.jev import calendar_interaction as calendar
from core.jev import llm_decider, loop
from core.jev.logger import AgentLogger
from core.jev.browser_bridge import BridgeBrowser


ROOT = Path(__file__).resolve().parents[1]


class LocalCalendarBrowser(BridgeBrowser):
    def __init__(self, page: Any) -> None:
        self.page = page
        self._last: Dict[str, Any] = {}
        self._elements: Dict[str, Dict[str, Any]] = {}
        self._tab_id = 1
        self._page = SimpleNamespace(keyboard=page.keyboard, mouse=page.mouse)
        self.interrupt_after: Optional[str] = None

    def _dispatch(self, action: str, arguments: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        if action != "snapshot" or arguments or timeout:
            raise AssertionError("Local fixtures only permit snapshot transport")
        return {"snapshot": self.page.evaluate("snapshotPage()")}

    def act(self, index: Optional[str], kind: str, text: Optional[str] = None) -> str:
        if kind != "CLICK" or text is not None:
            raise AssertionError("Calendar fixture must never type or bypass readonly")
        element = self.get_element(str(index))
        if not element:
            raise AssertionError("Missing local fixture element")
        self.page.evaluate("args => { const element=window.__jevFast.nodes.get(args.node); if(args.select!==undefined){element.value=args.select;element.dispatchEvent(new Event('change',{bubbles:true}));}else{element.click();}}", element)
        if self.interrupt_after and element.get("label") == self.interrupt_after:
            self.interrupt_after = None
            raise RuntimeError("Browser Use was paused.")
        return "clicked"

    def screenshot(self) -> str:
        return "data:image/png;base64," + base64.b64encode(self.page.screenshot()).decode("ascii")


@unittest.skipUnless(os.environ.get("NEO_EXTENSION_TEST_DIR"), "Supply --extension-package for extension DOM tests")
class CalendarLocalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.context = self.browser.new_context(viewport={"width": 1100, "height": 900})
        self.context.route("**/*", lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.set_content((ROOT / "tests/fixtures/calendars.html").read_text(encoding="utf-8"))
        source = (Path(os.environ["NEO_EXTENSION_TEST_DIR"]) / "background/jev-executor.js").read_text(encoding="utf-8")
        snapshot = source[source.index("function snapshotPage()"):source.index("function getClickTarget")]
        self.page.add_script_tag(content=(Path(os.environ["NEO_EXTENSION_TEST_DIR"]) / "background/policy.js").read_text(encoding="utf-8"))
        self.page.add_script_tag(content=snapshot)
        self.bridge = LocalCalendarBrowser(self.page)
        self.addCleanup(self.context.close)

    def select(self, layout: str, target: str) -> Dict[str, Any]:
        self.page.evaluate("configure", layout)
        snapshot = self.bridge.snapshot()
        field = next(element for element in snapshot["elements"] if element["label"] == "From Date")
        self.assertTrue(field["readonly"])
        self.assertNotIn("TYPE_TEXT", field["operations"])
        state = calendar.start_calendar(snapshot, field, target)
        with patch.object(calendar.decide, "choose", side_effect=AssertionError("Unexpected model call in deterministic fixture")):
            result = calendar.interact_calendar(self.bridge, snapshot, state, [])
        self.assertEqual(result, "verified")
        self.assertEqual(calendar.parse_date(self.page.locator("#from").input_value()).isoformat(), state["target"])
        self.assertFalse(self.page.evaluate("window.wrongCalendar || window.submitted"))
        self.assertEqual(self.page.locator("#to").input_value(), "24-Oct-2026")
        return state

    def test_year_and_month_grids_select_distant_date(self) -> None:
        state = self.select("grid", "01-Jan-1990")
        self.assertLess(state["actions"], 10)
        self.assertEqual(self.page.evaluate("window.events.filter(value=>value==='open').length"), 1)

    def test_native_year_and_month_dropdowns(self) -> None:
        self.select("dropdown", "15/02/2000")

    def test_cams_style_date_header_and_month_labels_select_a_nonfirst_day(self) -> None:
        self.select("cams", "15-Jan-1990")

    def test_nested_icon_is_associated_with_only_its_own_date_field(self) -> None:
        self.page.evaluate("""() => {
            const shared=document.createElement('div');shared.className='form-group';
            const fields=Array.from(document.querySelectorAll('.field'));fields[0].before(shared);
            for(const field of fields){field.className='';shared.append(field);const input=field.querySelector('input');const wrapper=document.createElement('span');input.before(wrapper);wrapper.append(input);}
        }""")
        self.select("cams", "15-Jan-1990")

    def test_stale_extension_snapshot_stops_before_any_click(self) -> None:
        snapshot = self.bridge.snapshot()
        snapshot.pop("calendar_protocol")
        field = next(element for element in snapshot["elements"] if element["label"] == "From Date")
        state = calendar.start_calendar(snapshot, field, "01-Jan-1990")
        with patch.object(self.bridge, "act") as act:
            self.assertIn("outdated calendar snapshot", calendar.interact_calendar(self.bridge, snapshot, state, []))
        act.assert_not_called()

    def test_calendar_logs_persist_clicked_icon_observation_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("core.jev.logger.LOGS_DIR", directory):
            log = AgentLogger("calendar-local-fixture")
            try:
                with patch.object(calendar.logger, "get_logger", return_value=log):
                    self.select("cams", "15-Jan-1990")
                text = Path(log.log_path).read_text(encoding="utf-8")
                events = json.loads(Path(log.json_path).read_text(encoding="utf-8"))
                self.assertIn("Calendar action:", text)
                self.assertIn("Open calendar", text)
                self.assertIn("Calendar observation:", text)
                self.assertIn("Calendar verified:", text)
                self.assertTrue(any(event.get("data", {}).get("calendar", {}).get("control_label") == "Open calendar" for event in events))
            finally:
                log.close()

    def test_month_arrows_and_numeric_day_buttons(self) -> None:
        self.select("arrows", "15-09-2026")

    def test_accessible_full_date_labels_and_iso_readback(self) -> None:
        self.select("iso", "2026-10-15")

    def test_canvas_calendar_uses_validated_vision_coordinates_and_verifies_readback(self) -> None:
        self.page.evaluate("configure", "vision")
        snapshot = self.bridge.snapshot()
        field = next(element for element in snapshot["elements"] if element["label"] == "From Date")
        state = calendar.start_calendar(snapshot, field, "01-Jan-1990")
        with patch.object(llm_decider, "_call_llm", return_value={"operation": "CLICK_AT", "x": 90, "y": 170}) as model:
            self.assertEqual(calendar.interact_calendar(self.bridge, snapshot, state, []), "verified")
        model.assert_called_once()
        self.assertEqual(self.page.locator("#from").input_value(), "01-Jan-1990")

    def test_pause_after_applied_year_click_resumes_without_reopening_or_replaying(self) -> None:
        self.page.evaluate("configure", "grid")
        snapshot = self.bridge.snapshot()
        field = next(element for element in snapshot["elements"] if element["label"] == "From Date")
        state = calendar.start_calendar(snapshot, field, "01-Jan-1990")
        self.bridge.interrupt_after = "1990"
        with self.assertRaisesRegex(RuntimeError, "paused"):
            calendar.interact_calendar(self.bridge, snapshot, state, [])
        self.assertEqual(calendar.interact_calendar(self.bridge, self.bridge.snapshot(), state, []), "verified")
        events = self.page.evaluate("window.events")
        self.assertEqual(events.count("open"), 1)
        self.assertEqual(events.count("year:1990"), 1)

    def test_existing_calendar_is_closed_before_opening_the_requested_field(self) -> None:
        self.page.evaluate("configure", "grid")
        self.page.evaluate("openCalendar()")
        snapshot = self.bridge.snapshot()
        field = next(element for element in snapshot["elements"] if element["label"] == "From Date")
        state = calendar.start_calendar(snapshot, field, "01-Jan-1990")
        self.assertEqual(calendar.interact_calendar(self.bridge, snapshot, state, []), "verified")
        self.assertEqual(self.page.evaluate("window.events.filter(value=>value==='open').length"), 2)

    def test_wrong_readback_does_not_report_success(self) -> None:
        self.page.evaluate("configure", "iso")
        self.page.evaluate("window.chooseDay = () => {document.getElementById('from').value='02-Oct-2026';document.getElementById('host').innerHTML='';}")
        snapshot = self.bridge.snapshot()
        field = next(element for element in snapshot["elements"] if element["label"] == "From Date")
        state = calendar.start_calendar(snapshot, field, "15-Oct-2026")
        self.assertTrue(calendar.interact_calendar(self.bridge, snapshot, state, []).startswith("needs_input:"))
        self.assertNotEqual(state["stage"], "verified")

    def test_unchanged_calendar_stops_instead_of_repeating_click(self) -> None:
        snapshot = self.bridge.snapshot()
        field = next(element for element in snapshot["elements"] if element["label"] == "From Date")
        state = calendar.start_calendar(snapshot, field, "01-Jan-1990")
        with patch.object(self.bridge, "act", return_value="unchanged") as act:
            result = calendar.interact_calendar(self.bridge, snapshot, state, [])
        self.assertIn("no progress", result)
        act.assert_called_once()

    def test_wrong_date_and_page_navigation_are_never_success(self) -> None:
        snapshot = self.bridge.snapshot()
        field = next(element for element in snapshot["elements"] if element["label"] == "From Date")
        state = calendar.start_calendar(snapshot, field, "01-Jan-1990")
        moved = dict(snapshot, url="https://different.example/")
        self.assertIn("page changed", calendar.interact_calendar(self.bridge, moved, state, []))
        self.assertEqual(state["actions"], 0)


class CalendarPolicyTests(unittest.TestCase):
    def test_ambiguous_numeric_days_and_multiple_calendars_are_not_guessed(self) -> None:
        state = {"target": "2026-10-15", "label": "From Date", "stage": "day"}
        controls = [{"index": str(index), "role": "button", "label": "15", "calendar_id": "popup", "calendar_text": "October 2026"} for index in (1, 2)]
        self.assertIsNone(calendar.dom_calendar_action({"elements": controls}, state))
        controls[1]["calendar_id"] = "other-popup"
        with patch.object(calendar.decide, "choose") as model:
            self.assertIsNone(calendar.calendar_action(SimpleNamespace(), {"elements": controls}, state, []))
        model.assert_not_called()

    def test_jev_offers_calendar_selection_for_readonly_dates(self) -> None:
        available = calendar.decide.get_available_operations({}, {"SET_DATE": {"1": {}}})
        self.assertIn("SET_DATE", available)
        self.assertEqual(llm_decider._ground([{"index": "1", "role": "textbox", "label": "From Date"}], "SET_DATE", "From Date"), "1")

    def test_invalid_dates_and_disabled_fields_are_rejected(self) -> None:
        for value in ("31-Feb-2026", "tomorrow", "01/02/03"):
            with self.assertRaises(ValueError):
                calendar.start_calendar({}, {}, value)
        with self.assertRaisesRegex(ValueError, "disabled"):
            calendar.start_calendar({}, {"disabled": True}, "2026-10-01")

    def test_scoped_jev_then_vision_fallback_receives_screenshot(self) -> None:
        controls = [{"index": "5", "role": "button", "label": "unusual date control", "calendar_id": "popup", "calendar_bounds": {"x": 10, "y": 20, "width": 300, "height": 200}}]
        page = {"elements": controls + [{"index": "99", "role": "button", "label": "Submit"}]}
        state = {"target": "1990-01-01", "label": "From Date", "stage": "year"}
        browser = SimpleNamespace(screenshot=Mock(return_value="data:image/png;base64,fixture"))
        with patch.object(calendar.decide, "choose", return_value={"operation": "WAIT"}) as jev, patch.object(llm_decider, "decide_calendar_action", return_value={"operation": "CLICK", "index": "5"}) as vision:
            self.assertEqual(calendar.calendar_action(browser, page, state, []), {"operation": "CLICK", "index": "5", "source": "vision"})
        self.assertEqual(jev.call_args.args[0]["elements"], controls)
        self.assertEqual(vision.call_args.args[2], "data:image/png;base64,fixture")

    def test_vision_coordinates_are_bounded_and_invalid_actions_rejected(self) -> None:
        page = {"viewport": {"width": 900, "height": 600}, "elements": [{"index": "5", "calendar_id": "popup", "calendar_bounds": {"x": 10, "y": 20, "width": 200, "height": 100}}]}
        with patch.object(llm_decider, "_call_llm", return_value={"operation": "CLICK_AT", "x": 50, "y": 60}) as model:
            self.assertEqual(llm_decider.decide_calendar_action(page, "Select date", "data:image/png;base64,fixture"), {"operation": "CLICK_AT", "x": 50, "y": 60})
            self.assertEqual(model.call_args.args[0][1]["content"][1]["type"], "image_url")
        for response in ({"operation": "CLICK_AT", "x": 500, "y": 60}, {"operation": "CLICK_AT", "x": float("nan"), "y": 60}, {"operation": "CLICK", "index": "99"}, {"operation": "TYPE_TEXT", "index": "5", "value": "date"}):
            with self.subTest(response=response), patch.object(llm_decider, "_call_llm", return_value=response):
                self.assertIsNone(llm_decider.decide_calendar_action(page, "Select date", "data:image/png;base64,fixture"))

    def test_loop_resumes_same_calendar_context_after_interruption(self) -> None:
        field = {"index": "1", "node": "date", "role": "textbox", "label": "From Date", "readonly": True, "value": "01-Oct-2026"}
        page = {"url": "https://example.org/calendar", "title": "Local fixture", "elements": [field], "text": "From Date"}
        browser = SimpleNamespace(url=page["url"], task_history=[], task_steps=0, get_element=lambda index: field, wait_until_resumed=Mock(return_value=True))
        contexts: List[Dict[str, Any]] = []

        def interact(_browser: Any, _page: Dict[str, Any], state: Dict[str, Any], _history: List[Dict[str, Any]]) -> str:
            contexts.append(state)
            if len(contexts) == 1:
                state["stage"] = "month"
                raise RuntimeError("Browser Use was paused.")
            self.assertEqual(state["stage"], "month")
            return "verified"

        with ExitStack() as stack:
            stack.enter_context(patch.object(loop.logger, "get_logger", return_value=Mock()))
            stack.enter_context(patch.object(loop, "find_recipe", return_value=None))
            stack.enter_context(patch.object(loop.trajectory, "load_trajectory", return_value=None))
            stack.enter_context(patch.object(loop.trajectory, "save_or_optimize_trajectory"))
            stack.enter_context(patch.object(loop, "snap_or_recover", return_value=(page, 0)))
            stack.enter_context(patch.object(loop, "bot_wall", return_value=False))
            stack.enter_context(patch.object(loop.decide, "choose", side_effect=[{"operation": "TYPE_TEXT", "index": "1", "value": "01-Jan-1990"}, {"operation": "DONE"}]))
            stack.enter_context(patch.object(loop, "interact_calendar", side_effect=interact))
            self.assertEqual(loop.run_task(browser, "Select date", 3), "done")
        self.assertIs(contexts[0], contexts[1])
        browser.wait_until_resumed.assert_called_once()


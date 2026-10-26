import datetime
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from core.jev import decide, llm_decider, logger
from core.jev.control import browser_input_interrupted


MONTHS: Tuple[str, ...] = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def parse_date(value: str) -> Optional[datetime.date]:
    cleaned = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", value.strip(), flags=re.I)
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y", "%d-%B-%Y", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%A, %B %d, %Y", "%A, %d %B %Y"):
        try:
            return datetime.datetime.strptime(cleaned, pattern).date()
        except ValueError:
            continue
    return None


def start_calendar(page: Dict[str, Any], field: Dict[str, Any], value: str) -> Dict[str, Any]:
    target = parse_date(value)
    if target is None:
        raise ValueError("Calendar target must be an unambiguous ISO date or day-month-year date.")
    if field.get("disabled"):
        raise ValueError("The requested date field is disabled.")
    return {"url": page.get("url"), "node": field.get("node"), "label": field.get("label"), "target": target.isoformat(), "actions": 0, "attempts": {}, "stage": "opening", "opened": False}


def _field(page: Dict[str, Any], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fields = [element for element in page.get("elements", []) if element.get("role") in ("textbox", "searchbox", "combobox")]
    matches = [element for element in fields if state.get("node") and element.get("node") == state["node"]]
    if len(matches) == 1:
        return matches[0]
    matches = [element for element in fields if element.get("label") == state["label"]]
    return matches[0] if len(matches) == 1 else None


def _single(elements: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return elements[0] if len(elements) == 1 else None


def _click(element: Dict[str, Any]) -> Dict[str, Any]:
    return {"operation": "CLICK", "index": str(element["index"])}


def _calendar_log(event: str, state: Dict[str, Any], page: Optional[Dict[str, Any]] = None, **details: Any) -> None:
    field = _field(page, state) if page else None
    data = {"target": state["target"], "field": state["label"], "stage": state["stage"], "actions": state.get("actions", 0), **details}
    if page is not None:
        data.update({"protocol": page.get("calendar_protocol"), "popup_count": len(calendar_ids(page)), "field_found": field is not None, "field_value": (field or {}).get("value"), "opener_node": (field or {}).get("calendar_opener"), "views": sorted({str(element.get("calendar_view")) for element in page.get("elements", []) if element.get("calendar_id")})})
    message = logger.redact_sensitive(str(data))
    logger.get_logger().info(f"Calendar {event}: {message}", calendar=data)


def calendar_ids(page: Dict[str, Any]) -> Set[str]:
    return {element["calendar_id"] for element in page.get("elements", []) if element.get("calendar_id")} | {popup["id"] for popup in page.get("calendars", [])}


def dom_calendar_action(page: Dict[str, Any], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    target = datetime.date.fromisoformat(state["target"])
    field = _field(page, state)
    elements = [element for element in page.get("elements", []) if not element.get("disabled")]
    calendars = calendar_ids(page)
    if not calendars:
        if not field:
            return None
        opener = _single([element for element in elements if field.get("calendar_opener") and element.get("node") == field["calendar_opener"]])
        return _click(opener) if opener else None
    if len(calendars) != 1:
        return None
    controls = [element for element in elements if element.get("calendar_id") in calendars]
    if not controls:
        return None
    text = str(controls[0].get("calendar_text") or "")
    view = controls[0].get("calendar_view")
    labels = [(element, str(element.get("option_label") or element.get("label") or "").strip()) for element in controls]
    if view == "month":
        month_cell = _single([element for element, label in labels if (date := parse_date(label)) is not None and (date.year, date.month) == (target.year, target.month)])
        if month_cell:
            state["stage"] = "month"
            return _click(month_cell)
    exact_date = _single([element for element, label in labels if parse_date(label) == target])
    if exact_date:
        state["stage"] = "day"
        return _click(exact_date)
    year = _single([element for element, label in labels if label == str(target.year) and element.get("role") in ("button", "gridcell", "option")])
    if year:
        state["stage"] = "year"
        return _click(year)
    month = _single([element for element, label in labels if label.lower() in (MONTHS[target.month - 1].lower(), MONTHS[target.month - 1][:3].lower()) and element.get("role") in ("button", "gridcell", "option")])
    current = re.search(r"\b(" + "|".join(MONTHS) + r")\s+(\d{4})\b", text, re.I)
    displayed = re.search(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b", text)
    displayed_date = parse_date(displayed[0]) if displayed else None
    if month and (state["stage"] == "year" or not current or int(current[2]) == target.year):
        state["stage"] = "month"
        return _click(month)
    if current or displayed_date:
        current_month = displayed_date.month if displayed_date else next(index + 1 for index, name in enumerate(MONTHS) if name.lower() == current[1].lower())
        current_year = displayed_date.year if displayed_date else int(current[2])
        if (current_year, current_month) == (target.year, target.month):
            day = _single([element for element, label in labels if label == str(target.day) and element.get("role") in ("button", "gridcell", "option")])
            if day:
                state["stage"] = "day"
                return _click(day)
        header = _single([element for element, label in labels if re.search(r"choose.*(?:month|year|date)|select.*(?:month|year)", label, re.I)])
        if current_year != target.year and header:
            state["stage"] = "year"
            return _click(header)
        direction = "previous|prev|back" if (target.year, target.month) < (current_year, current_month) else "next|forward"
        arrow = _single([element for element, label in labels if re.search(direction, label, re.I) and element.get("role") == "button"])
        if arrow:
            return _click(arrow)
    years = [int(label) for element, label in labels if re.fullmatch(r"\d{4}", label) and element.get("role") in ("button", "gridcell", "option")]
    if years and (target.year < min(years) or target.year > max(years)):
        direction = "previous|prev|back" if target.year < min(years) else "next|forward"
        arrow = _single([element for element, label in labels if re.search(direction, label, re.I) and element.get("role") == "button"])
        if arrow:
            return _click(arrow)
    return None


def calendar_action(browser: Any, page: Dict[str, Any], state: Dict[str, Any], history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    action = dom_calendar_action(page, state)
    if action:
        action["source"] = "dom"
        return action
    controls = [element for element in page.get("elements", []) if element.get("calendar_id") and not element.get("disabled")]
    if len(calendar_ids(page)) != 1:
        return None
    scoped = dict(page, elements=controls)
    hint = f"Select calendar date {state['target']} for {state['label']}. Current stage: {state['stage']}. Only use this calendar. Choose year, month, then day; use year grids or dropdowns for distant dates. Never type into the read-only date field. Never submit the form or leave this calendar."
    screenshot = browser.screenshot() if hasattr(browser, "screenshot") else None
    _calendar_log("fallback", state, page, screenshot_available=bool(screenshot), controls=len(controls))
    action = decide.choose(scoped, hint, history, extra_hint=hint, screenshot_data_url=screenshot) if controls else {}
    if action.get("operation") == "CLICK" and any(str(element["index"]) == str(action.get("index")) for element in controls):
        return dict(action, source="hybrid")
    if screenshot:
        visual = llm_decider.decide_calendar_action(scoped, hint, screenshot)
        return dict(visual, source="vision") if visual else None
    return None


def interact_calendar(browser: Any, page: Dict[str, Any], state: Dict[str, Any], history: List[Dict[str, Any]]) -> str:
    _calendar_log("start", state, page)
    try:
        result = _interact_calendar(browser, page, state, history)
    except Exception as error:
        _calendar_log("interrupted" if browser_input_interrupted(error) else "error", state, error_type=type(error).__name__, error=logger.redact_sensitive(str(error))[:160])
        raise
    _calendar_log("verified" if result == "verified" else "stop", state, result=result)
    return result


def _interact_calendar(browser: Any, page: Dict[str, Any], state: Dict[str, Any], history: List[Dict[str, Any]]) -> str:
    while state["actions"] < 48:
        if page.get("url") != state["url"]:
            return "needs_input: The page changed during calendar selection; verify the requested date manually."
        if page.get("calendar_protocol") != 1:
            return "needs_input: The running extension has an outdated calendar snapshot. Reload the updated Neo Saarthi extension and restart this task."
        field = _field(page, state)
        if calendar_ids(page) and not state["opened"]:
            browser._page.keyboard.press("Escape")
            page = browser.snapshot(retries=1)
            if calendar_ids(page):
                return "needs_input: Close the existing calendar before selecting another date."
            continue
        if field and parse_date(str(field.get("value") or "")) == datetime.date.fromisoformat(state["target"]):
            if calendar_ids(page):
                browser._page.keyboard.press("Escape")
                fresh = browser.snapshot(retries=1)
                field = _field(fresh, state)
                if not field or parse_date(str(field.get("value") or "")) != datetime.date.fromisoformat(state["target"]):
                    return "needs_input: The selected calendar date could not be verified after closing the popup."
                if calendar_ids(fresh):
                    return "needs_input: Close the calendar manually before continuing."
            state["stage"] = "verified"
            history.append({"action": f"Calendar date verified for {state['label']}", "kind": "SET_DATE", "text": state["target"]})
            return "verified"
        if field and field.get("disabled"):
            return "needs_input: The requested date field is disabled."
        try:
            action = calendar_action(browser, page, state, history)
        except Exception as error:
            if browser_input_interrupted(error):
                raise
            _calendar_log("planner error", state, error_type=type(error).__name__, error=logger.redact_sensitive(str(error))[:160])
            return "needs_input: Calendar controls could not be resolved; select the requested date manually."
        if not action:
            return "needs_input: Calendar layout is ambiguous or unsupported; select the requested date manually."
        signature = str([(element.get("label"), element.get("value"), element.get("calendar_text")) for element in page.get("elements", [])])
        key = str((signature, action))
        state["attempts"][key] = state["attempts"].get(key, 0) + 1
        if state["attempts"][key] > 1:
            return "needs_input: Calendar selection made no progress; select the requested date manually."
        state["actions"] += 1
        state["opened"] = True
        control = next((element for element in page.get("elements", []) if str(element.get("index")) == str(action.get("index"))), {})
        _calendar_log("action", state, page, operation=action["operation"], index=action.get("index"), control_label=control.get("label"), source=action.get("source"), x=action.get("x"), y=action.get("y"))
        try:
            if action.get("operation") == "CLICK_AT":
                browser._page.mouse.click(action["x"], action["y"])
            else:
                browser.act(str(action["index"]), "CLICK")
        except Exception as error:
            if browser_input_interrupted(error):
                state["attempts"].pop(key, None)
                raise
            _calendar_log("action error", state, error_type=type(error).__name__, error=logger.redact_sensitive(str(error))[:160])
            return "needs_input: A calendar control could not be activated; select the requested date manually."
        history.append({"action": f"Calendar {state['stage']} action", "kind": "calendar", "text": ""})
        page = browser.snapshot(retries=1)
        _calendar_log("observation", state, page)
    return "needs_input: Calendar selection reached its action limit; select the requested date manually."

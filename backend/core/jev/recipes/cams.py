import datetime
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from recipes.base import Milestone, WorkflowRecipe

LOGS_DIR: str = str(
    Path(os.getenv("MF_RUN_LOG_DIR", str(Path(__file__).resolve().parents[4] / "logs"))).expanduser().resolve()
    / "browser_use"
)


def _parse_date(goal: str) -> str:
    match = re.search(r"\b\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,})[-/.]\d{2,4}\b", goal)
    default_env_date = os.getenv("RTA_FROM_DATE", "01-01-1990")
    if not match:
        raw = default_env_date
    else:
        raw = match.group(0).rstrip(".,;!?")
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(raw, fmt).strftime("%d-%b-%Y")
        except ValueError:
            pass
    return raw


def _code_in(text: str, code: str) -> bool:
    return re.search(rf"\b{re.escape(code)}\b", text or "", re.IGNORECASE) is not None


def _same_date(left: str, right: str) -> bool:
    """Compare CAMS date values across display and native-input formats."""
    formats = ("%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y")
    for value in (left or "", right or ""):
        if not value:
            continue
    parsed = []
    for value in (left, right):
        item = None
        for fmt in formats:
            try:
                item = datetime.datetime.strptime(str(value).strip(), fmt).date()
                break
            except ValueError:
                pass
        parsed.append(item)
    return parsed[0] is not None and parsed[0] == parsed[1]


def _seg_in(url: str, code: str) -> bool:
    return re.search(rf"/{re.escape((code or '').lower())}(?:/|$|\?|#)", (url or "").lower() + "/") is not None


def _save_cams_confirmation(raw_text: str, report_code: str) -> str:
    os.makedirs(LOGS_DIR, exist_ok=True)
    conf_match = re.search(r"confirmation number is\s*([A-Za-z0-9\s]+?)(?:\s+and\b|\.|\n|at this point)", raw_text, re.IGNORECASE)
    conf_num = conf_match.group(1).strip() if conf_match else "CONFIRMED"
    record: Dict[str, Any] = {
        "report": report_code.upper(),
        "timestamp": datetime.datetime.now().isoformat(),
        "confirmation_number": conf_num,
        "full_message": raw_text[:2000],
    }
    path = os.path.join(LOGS_DIR, "cams_confirmations.json")
    existing: List[Dict[str, Any]] = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []
    existing.append(record)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    return conf_num


class CAMSMailbackRecipe(WorkflowRecipe):
    def __init__(self) -> None:
        super().__init__(
            name="cams_mailback",
            domains=["camsonline.com"],
            keywords=["cams", "mailback", "wbr"],
            default_start_url="https://www.camsonline.com/Distributors/Service-Requests/Distributor-Mailback-Services/Request-Mailback",
            field_hints={
                "date_format": "%d-%b-%Y",
                "default_output_format": "DBF",
                "report_password": os.environ.get("CAMS_REPORT_PASSWORD", os.environ.get("REPORT_PASSWORD", "")),
            },
        )

    def matches(self, goal: str, url: str = "") -> bool:
        url_low = (url or "").lower()
        goal_low = (goal or "").lower()
        if "camsonline.com" in url_low:
            return True
        if any(k in goal_low for k in ("kfintech", "kfinkart", "dss", "dssweb", "karvy")):
            return False
        return any(k in goal_low for k in ("camsonline", "cams", "wbr"))

    def _extract_reports(self, goal: str) -> List[str]:
        goal_low = goal.lower()
        candidates = [
            ("wbr2", ["wbr2", "mfsd201", "201"]),
            ("wbr4", ["wbr4", "mfsd203", "203"]),
            ("wbr9", ["wbr9", "mfsd211", "211"]),
            ("wbr49", ["wbr49", "mfsd243", "243"]),
        ]
        matched: List[str] = []
        for rep, aliases in candidates:
            if any(re.search(rf"\b{re.escape(a)}\b", goal_low) for a in aliases):
                matched.append(rep)
        if not matched:
            for m in re.findall(r"\bWBR\d+[A-Z]?\b", goal, re.IGNORECASE):
                c = m.lower()
                if c not in matched:
                    matched.append(c)
        return matched or ["wbr2"]

    def build_milestones(self, goal: str, context: Dict[str, Any]) -> List[Milestone]:
        reports = self._extract_reports(goal)
        context["requested_reports"] = reports
        context["completed_reports"] = []
        context["from_date"] = _parse_date(goal)
        context["goal"] = goal
        email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", goal)
        context["target_email"] = (email_match.group(0) if email_match else str(self.field_hints.get("email") or "")).strip()
        goal_low = goal.lower()
        has_do_not_submit = any(k in goal_low for k in ("do not submit", "don't submit", "dont submit", "never submit", "without submitting"))
        context["want_submit"] = "submit" in goal_low and not has_do_not_submit
        milestones: List[Milestone] = [
            Milestone(
                id="entry",
                name="Portal Entry",
                description="Enter registered email and submit.",
                instruction_hint="Enter registered email and submit.",
                is_completed=lambda page, hist, ctx: "/wbr" in (page.get("url") or "").lower(),
            ),
            Milestone(
                id="funds_and_reports",
                name="Select Funds & Reports Tab",
                description="Select All Mutual Funds and open Customer Services.",
                instruction_hint="Select All Mutual Funds option, then open Customer Services tab.",
                is_completed=lambda page, hist, ctx: any(
                    any(k in e.get("label", "").lower() for k in ("description", "from date", "date range"))
                    for e in (page.get("elements") or [])
                ),
            ),
        ]

        def _report_hint(page: Dict[str, Any], hist: List[Dict[str, Any]], ctx: Dict[str, Any], r: str) -> str:
            elems = page.get("elements") or []
            rep_low = r.lower()
            selected_rep = _seg_in(page.get("url") or "", rep_low) or any(rep_low in h.get("action", "").lower() for h in hist)
            has_pwd = any("enter password" in e.get("label", "").lower() for e in elems)
            has_imm = any(e.get("label", "").upper() == "IMMEDIATE" for e in elems)
            if selected_rep and (has_pwd or has_imm):
                action = "click 'IMMEDIATE' button to submit" if ctx.get("want_submit") else "stop before submit"
                return f"Fill 'Enter Password' and 'Re-Enter Password *', then {action}. Do not touch DESCRIPTION."
            has_date = any("from date" in e.get("label", "").lower() for e in elems)
            if selected_rep and has_date:
                return f"Fill 'From Date' with {ctx.get('from_date', '01-Jan-2024')}, then click the 'Next' button. Do not touch DESCRIPTION."
            if selected_rep:
                return "The report is selected. SCROLL down to reveal the From Date field and Next button."
            typed_search = any(rep_low in (h.get("text") or "").lower() for h in hist[-3:]) if hist else False
            if typed_search:
                return f"Click the filtered option matching '{r.upper()}' from the dropdown. Do not click 'Show report'."
            return f"Type '{r.upper()}' into the DESCRIPTION search textbox. Do not fill date or click Next until '{r.upper()}' is selected."

        def _report_step(page: Dict[str, Any], hist: List[Dict[str, Any]], ctx: Dict[str, Any], r: str) -> Optional[Dict[str, Any]]:
            if not _seg_in(page.get("url") or "", r):
                return None
            elems = page.get("elements") or []
            if not _code_in(page.get("text") or "", r):
                return None
            target_date = str(ctx.get("from_date") or "01-Jan-2024")
            from_box = next((e for e in elems if e.get("role") in ("textbox", "searchbox") and "from date" in e.get("label", "").lower()), None)
            if from_box is not None and not _same_date(from_box.get("value") or "", target_date):
                return {"operation": "SET_DATE", "index": from_box["index"], "value": target_date, "reason": "select from date using calendar controls"}
            next_btn = next((e for e in elems if e.get("role") == "button" and e.get("label", "").strip().lower() == "next"), None)
            if from_box is not None and next_btn is not None:
                return {"operation": "CLICK", "index": next_btn["index"], "reason": "proceed to passwords"}
            rep_pwd = self.field_hints.get("report_password") or os.environ.get("CAMS_REPORT_PASSWORD", os.environ.get("REPORT_PASSWORD", ""))
            if not rep_pwd:
                found = re.findall(r"\bwith\s+([^\s,;]+)", ctx.get("goal", ""))
                rep_pwd = found[-1] if found else ""
            if rep_pwd:
                pwd_box = next((e for e in elems if e.get("role") in ("textbox", "searchbox") and "enter password" in e.get("label", "").lower() and "re-enter" not in e.get("label", "").lower() and not (e.get("value") or "").strip()), None)
                if pwd_box is not None:
                    return {"operation": "TYPE_TEXT", "index": pwd_box["index"], "value": rep_pwd, "reason": "fill report password"}
                conf_box = next((e for e in elems if e.get("role") in ("textbox", "searchbox") and "re-enter password" in e.get("label", "").lower() and not (e.get("value") or "").strip()), None)
                if conf_box is not None:
                    return {"operation": "TYPE_TEXT", "index": conf_box["index"], "value": rep_pwd, "reason": "fill report confirm password"}
            return None
        for rep in reports:
            milestones.append(
                Milestone(
                    id=f"report_{rep}",
                    name=f"Report {rep.upper()}",
                    description=f"Configure and process report {rep.upper()}.",
                    instruction_hint=f"Configure report {rep.upper()}.",
                    is_completed=lambda page, hist, ctx, r=rep: r in ctx.get("completed_reports", []),
                )
            )
        return milestones

    def _remaining_reports(self, context: Dict[str, Any]) -> List[str]:
        completed = context.get("completed_reports", [])
        failed = {str(m).split("_")[-1] for m in context.get("failed_milestones", [])}
        return [r for r in context.get("requested_reports", ["wbr2"]) if r not in completed and r not in failed]

    def _summary(self, context: Dict[str, Any]) -> str:
        completed = context.get("completed_reports", [])
        failed = sorted({str(m).split("_")[-1] for m in context.get("failed_milestones", [])})
        return f"done={completed} failed={failed}"

    def handle_recovery(self, browser: Any, page: Dict[str, Any], history: List[Dict[str, Any]], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        elements = page.get("elements") or []
        page_text = page.get("text") or ""
        url = (page.get("url") or "").lower()

        cookie_btns = [
            e for e in elements
            if e.get("role") == "button" and any(k in e.get("label", "").lower() for k in ("accept all", "essential only", "close"))
        ]
        if cookie_btns:
            return {"operation": "CLICK", "index": cookie_btns[0]["index"], "reason": "dismiss banner"}

        if "request-mailback" in url and "/wbr" not in url:
            click_here = [e for e in elements if "click here" in e.get("label", "").lower()]
            if click_here and any(k in page_text.lower() for k in ("previous session", "already logged", "session active", "another session", "session expired", "timed out")):
                return {"operation": "CLICK", "index": click_here[0]["index"], "reason": "clear previous session"}
            email_filled = any(e.get("role") in ("textbox", "searchbox") and "@" in str(e.get("value") or "") for e in elements)
            submit_btn = next((e for e in elements if e.get("role") == "button" and e.get("label", "").strip().lower() == "submit"), None)
            if email_filled and submit_btn is not None:
                return {"operation": "CLICK", "index": submit_btn["index"], "reason": "submit registered email"}
            email_box = next((e for e in elements if e.get("role") in ("textbox", "searchbox") and "email" in e.get("label", "").lower()), None)
            target_email = str(context.get("target_email") or "").strip()
            if email_box and target_email and not str(email_box.get("value") or "").strip():
                return {"operation": "TYPE_TEXT", "index": email_box["index"], "value": target_email, "reason": "type registered email"}

        completed = context.setdefault("completed_reports", [])
        seen_confs: Set[str] = context.setdefault("seen_confirmations", set())
        remaining = self._remaining_reports(context)

        if not remaining:
            return {"operation": "DONE", "index": None, "reason": f"all CAMS reports completed {self._summary(context)}"}

        active_rep = remaining[0]

        conf_match = re.search(r"confirmation number is\s*([A-Za-z0-9\s]+?)(?:\s+and\b|\.|\n|at this point)", page_text, re.IGNORECASE)
        if conf_match and active_rep:
            conf_num = conf_match.group(1).strip()
            if conf_num not in seen_confs:
                seen_confs.add(conf_num)
                _save_cams_confirmation(page_text, active_rep)
                if active_rep not in completed:
                    completed.append(active_rep)
            remaining = self._remaining_reports(context)
            if not remaining:
                return {"operation": "DONE", "index": None, "reason": f"all CAMS reports confirmed {self._summary(context)}"}

        desc_boxes = [
            e for e in elements
            if "description" in (e.get("label") or "").lower()
            and (e.get("role") in ("textbox", "searchbox") or e.get("role") == "combobox")
        ]
        cs_tab = next(
            (e for e in elements if e.get("role") == "tab" and e.get("label", "").strip().lower() == "customer services"),
            next((e for e in elements if e.get("role") in ("tab", "link", "button") and "customer services" in (e.get("label") or "").lower()), None),
        )

        if "/wbr" in url and "/wbr1" not in url and not _seg_in(url, active_rep):
            if not desc_boxes:
                back_btn = next((e for e in elements if e.get("role") == "button" and e.get("label", "").strip().lower() == "back"), None)
                if back_btn is not None:
                    return {"operation": "CLICK", "index": back_btn["index"], "reason": "return from report subpage"}
                if cs_tab is not None:
                    return {"operation": "CLICK", "index": cs_tab["index"], "reason": "return to customer services catalog"}
                mailback_link = next((e for e in elements if e.get("role") == "link" and e.get("label", "").strip().lower() == "distributor mailback services"), None)
                if mailback_link is not None:
                    return {"operation": "CLICK", "index": mailback_link["index"], "reason": "return to distributor mailback catalog"}

        rep_opt = next(
            (e for e in elements if e.get("role") in ("option", "link", "button", "row", "cell") and _code_in(e.get("label") or "", active_rep.upper())),
            None,
        )
        if rep_opt is not None:
            return {"operation": "CLICK", "index": rep_opt["index"], "reason": f"select {active_rep.upper()} option"}

        on_active_wbr = _seg_in(url, active_rep)
        target_date = str(context.get("from_date") or _parse_date(""))
        rep_pwd = str(self.field_hints.get("report_password") or "")

        if on_active_wbr:
            pwd_box = next((e for e in elements if e.get("role") in ("textbox", "searchbox") and "enter password" in e.get("label", "").lower() and "re-enter" not in e.get("label", "").lower()), None)
            conf_box = next((e for e in elements if e.get("role") in ("textbox", "searchbox") and "re-enter password" in e.get("label", "").lower()), None)

            pwd_val = str((pwd_box or {}).get("value") or "").strip()
            conf_val = str((conf_box or {}).get("value") or "").strip()
            passwords_filled = bool(pwd_box and conf_box and pwd_val and conf_val)

            if passwords_filled:
                if not context.get("want_submit"):
                    if active_rep not in completed:
                        completed.append(active_rep)
                    remaining = self._remaining_reports(context)
                    if not remaining:
                        return {"operation": "DONE", "index": None, "reason": f"passwords entered and stop requested before submit {self._summary(context)}"}
                    back_btn = next((e for e in elements if e.get("role") == "button" and e.get("label", "").strip().lower() == "back"), None)
                    if back_btn is not None:
                        return {"operation": "CLICK", "index": back_btn["index"], "reason": "return to report list for next report"}
                    if cs_tab is not None:
                        return {"operation": "CLICK", "index": cs_tab["index"], "reason": "return to customer services catalog for next report"}
                    mailback_link = next((e for e in elements if e.get("role") == "link" and e.get("label", "").strip().lower() == "distributor mailback services"), None)
                    if mailback_link is not None:
                        return {"operation": "CLICK", "index": mailback_link["index"], "reason": "return to mailback catalog for next report"}
                    return {"operation": "BACK", "index": None, "reason": "navigate back to report catalog"}
                else:
                    imm_btn = next((e for e in elements if e.get("role") == "button" and e.get("label", "").strip().upper() == "IMMEDIATE"), None)
                    if imm_btn is not None:
                        return {"operation": "CLICK", "index": imm_btn["index"], "reason": "submit via immediate delivery"}

            if pwd_box is not None and not pwd_val:
                return {"operation": "TYPE_TEXT", "index": pwd_box["index"], "value": rep_pwd, "reason": "fill password"}
            if conf_box is not None and not conf_val:
                return {"operation": "TYPE_TEXT", "index": conf_box["index"], "value": rep_pwd, "reason": "fill confirm password"}

            from_box = next((e for e in elements if e.get("role") in ("textbox", "searchbox") and ("from date" in e.get("label", "").lower() or ("from" in e.get("label", "").lower() and "date" in e.get("label", "").lower()))), None)
            if from_box is not None and not _same_date(from_box.get("value") or "", target_date):
                return {"operation": "SET_DATE", "index": from_box["index"], "value": target_date, "reason": "select from date using calendar controls"}

            next_btn = next((e for e in elements if e.get("role") == "button" and e.get("label", "").strip().lower() == "next"), None)
            if next_btn is not None and not pwd_box:
                if from_box is None or _same_date(from_box.get("value") or "", target_date):
                    return {"operation": "CLICK", "index": next_btn["index"], "reason": "proceed to passwords"}

            if not pwd_box and not from_box and not page.get("at_bottom", False):
                return {"operation": "SCROLL", "index": None, "reason": "scroll down to reveal form fields"}

        fund_box = next((e for e in elements if e.get("role") == "combobox" and "fund" in e.get("label", "").lower()), None)
        fund_val = str((fund_box or {}).get("value") or "").strip()
        fund_is_all = (
            bool(fund_box)
            and bool(fund_val)
            and fund_val.lower() != "mutual fund"
        )
        if fund_is_all:
            context["fund_selected"] = True

        all_opt = next((e for e in elements if e.get("role") == "option" and "all mutual funds" in (e.get("label") or "").lower()), None)
        if all_opt is not None and not context.get("fund_selected"):
            context["fund_selected"] = True
            return {"operation": "CLICK", "index": all_opt["index"], "reason": "select All Mutual Funds"}

        if fund_box is not None and not context.get("fund_selected") and not fund_is_all:
            return {"operation": "CLICK", "index": fund_box["index"], "reason": "open Mutual Fund dropdown"}

        if cs_tab is not None and not desc_boxes and context.get("fund_selected"):
            return {"operation": "CLICK", "index": cs_tab["index"], "reason": "open Customer Services tab"}

        if desc_boxes:
            target_desc = next(
                (e for e in desc_boxes if e.get("role") in ("textbox", "searchbox")),
                desc_boxes[0],
            )
            want_code = active_rep.upper()
            val = str(target_desc.get("value") or "").strip()
            if not _code_in(val, want_code):
                return {"operation": "TYPE_TEXT", "index": target_desc["index"], "value": want_code, "reason": f"filter report list for {want_code}"}

        return None

    def should_terminate(self, goal: str, history: List[Dict[str, Any]], context: Dict[str, Any]) -> bool:
        if not self._remaining_reports(context):
            return True
        reports = context.get("requested_reports", [])
        completed = context.get("completed_reports", [])
        return bool(reports and len(completed) >= len(reports))

    def get_budget(self, goal: str, default_budget: int = 35) -> int:
        n = len(self._extract_reports(goal))
        return max(default_budget, 20 + n * 20)

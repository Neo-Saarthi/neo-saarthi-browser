import base64
import datetime
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from core.jev.control import browser_input_interrupted
from core.jev.model_config import browser_model_config
from recipes.base import Milestone, WorkflowRecipe

LOGS_DIR: str = str(
    Path(os.getenv("MF_RUN_LOG_DIR", str(Path(__file__).resolve().parents[4] / "logs"))).expanduser().resolve()
    / "browser_use"
)

CANONICAL_KFIN_REPORTS: List[Dict[str, Any]] = [
    {
        "id": "201",
        "name": "MFSD201",
        "cams_code": "wbr2",
        "aliases": ["wbr2", "mfsd201", "201", "transaction"],
        "url_key": "reqtransactionreport",
    },
    {
        "id": "203",
        "name": "MFSD203",
        "cams_code": "wbr4",
        "aliases": ["wbr4", "mfsd203", "203", "aum", "valuation"],
        "url_key": "reqclientwiseaum",
    },
    {
        "id": "211",
        "name": "MFSD211",
        "cams_code": "wbr9",
        "aliases": ["wbr9", "mfsd211", "211", "investor master", "client master"],
        "url_key": "procurementinvestormasterinformation",
    },
    {
        "id": "243",
        "name": "MFSD243",
        "cams_code": "wbr49",
        "aliases": ["wbr49", "mfsd243", "243", "sip", "stp", "account statement"],
        "url_key": "sipstpswpregistrationreport",
    },
]


def _get_captcha_data_url(browser: Any) -> str:
    try:
        p = getattr(browser, "_page", None)
        if not p or not hasattr(p, "locator"):
            return ""
        canvas = p.locator("canvas")
        if not canvas.count():
            return ""
        val = "data:image/png;base64," + base64.b64encode(canvas.first.screenshot()).decode("ascii")
        if isinstance(val, str) and val.startswith("data:image"):
            return val
    except Exception:
        pass
    return ""


def _solve_captcha_image(data_url: str) -> str:
    if not data_url or not data_url.startswith("data:image"):
        return ""
    try:
        base_url, model, key = browser_model_config()
    except ValueError:
        return ""
    headers: Dict[str, str] = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "jev-agent/0.2",
        "x-opencode-session": uuid.uuid4().hex,
    }
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Read the 5 alphanumeric characters shown in the CAPTCHA image. Output ONLY the 5 characters, with no spaces, no punctuation, and no explanation.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            }
        ],
        "max_tokens": 1024,
    }
    try:
        with httpx.Client(http2=True, timeout=25) as client:
            resp = client.post(f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    msg = choices[0].get("message", {})
                    content = str(msg.get("content") or "").strip()
                    clean = re.sub(r"[^a-zA-Z0-9]", "", content)
                    if len(clean) >= 5:
                        return clean[:5]
                    reasoning = str(msg.get("reasoning_content") or "")
                    matches = re.findall(r"\b([a-zA-Z0-9]{5})\b", reasoning)
                    if matches:
                        return matches[-1]
    except Exception:
        pass
    return ""


def _save_kfin_confirmation(report_id: str, page_text: str, ref_no: str) -> None:
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        path = os.path.join(LOGS_DIR, "kfin_confirmations.json")
        existing: Dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        existing[report_id] = {
            "report": f"MFSD{report_id}",
            "reference": ref_no,
            "timestamp": datetime.datetime.now().isoformat(),
            "full_text": page_text[:2000],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass


class KFintechRecipe(WorkflowRecipe):
    def __init__(self) -> None:
        super().__init__(
            name="kfintech",
            domains=["dss.kfintech.com", "kfintech.com"],
            keywords=["kfintech", "karvy", "kfinkart", "dss", "dssweb"],
            default_start_url="https://dss.kfintech.com/dssweb/",
            field_hints={
                "username": os.environ.get("KARVY_PORTAL_USERNAME", os.environ.get("KFINTECH_USERNAME", os.environ.get("KFIN_ARN", ""))),
                "password": os.environ.get("KARVY_PORTAL_PASSWORD", os.environ.get("KFINTECH_PASSWORD", os.environ.get("KFIN_PASSWORD", ""))),
                "report_password": os.environ.get("RTA_EXTRACTION_PASSWORD", os.environ.get("KFINTECH_REPORT_PASSWORD", os.environ.get("REPORT_PASSWORD", ""))),
            },
        )

    def matches(self, goal: str, url: str = "") -> bool:
        url_low = (url or "").lower()
        goal_low = (goal or "").lower()
        if "dss.kfintech.com" in url_low or "kfintech" in url_low or "karvy" in url_low:
            return True
        return any(k in goal_low for k in ("kfintech", "karvy", "kfinkart", "dss", "dssweb"))

    def _get_target_reports(self, goal: str) -> List[Dict[str, Any]]:
        g = goal.lower()
        matched: List[Dict[str, Any]] = []
        for rpt in CANONICAL_KFIN_REPORTS:
            if any(re.search(rf"\b{re.escape(a)}\b", g) for a in rpt["aliases"]):
                matched.append(rpt)
        if matched:
            return matched
        return CANONICAL_KFIN_REPORTS

    def build_milestones(self, goal: str, context: Dict[str, Any]) -> List[Milestone]:
        reports = self._get_target_reports(goal)
        context["reports"] = reports
        context["completed_reports"] = []
        context["goal"] = goal

        email_m = re.search(r"[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}", goal)
        context["target_email"] = email_m.group(0).lower() if email_m else str(self.field_hints.get("email") or "").strip().lower()
        context["recipient_mode"] = "any" if "available registered email" in goal.lower() or self.field_hints.get("recipient_mode") == "any" else "exact"

        user_m = re.search(r"(?:username|user\s*id|login\s*id)\s*[:=]?\s*([A-Za-z0-9_-]+)", goal, re.IGNORECASE)
        u_val = user_m.group(1).rstrip(".,;!?") if user_m else self.field_hints.get("username", "")
        if u_val and "@" in u_val:
            u_val = self.field_hints.get("username", "")
        context["username"] = u_val

        pwd_m = re.search(r"(?:login\s+password)\s*[:=]?\s*([^\s,;]+)", goal, re.IGNORECASE)
        context["password"] = pwd_m.group(1).rstrip(".,;!?") if pwd_m else self.field_hints.get("password", "")

        rep_pwd_m = re.search(r"(?:extraction\s+password|report\s+password|password)\s*[:=]?\s*([^\s,;]+)", goal, re.IGNORECASE)
        rep_pwd = rep_pwd_m.group(1).rstrip(".,;!?") if rep_pwd_m else self.field_hints.get("report_password", os.environ.get("KFINTECH_REPORT_PASSWORD", os.environ.get("REPORT_PASSWORD", "")))
        context["report_password"] = rep_pwd

        date_match = re.search(r"\b\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,})[-/.]\d{2,4}\b", goal)
        default_env_date = os.getenv("RTA_FROM_DATE", "01-01-1990")
        raw_date = date_match.group(0).rstrip(".,;!?") if date_match else default_env_date
        parsed_date = raw_date
        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%b-%Y", "%d/%b/%Y"):
            try:
                parsed_date = datetime.datetime.strptime(raw_date, fmt).strftime("%d/%m/%Y")
                break
            except ValueError:
                pass
        context["target_date"] = parsed_date

        goal_low = goal.lower()
        has_do_not_submit = any(k in goal_low for k in ("do not submit", "don't submit", "dont submit", "never submit", "without submitting"))
        context["want_submit"] = "submit" in goal_low and not has_do_not_submit

        milestones: List[Milestone] = [
            Milestone(
                id="login",
                name="Login & Authenticate",
                description="Enter username, password, solve CAPTCHA, and sign in to the portal.",
                instruction_hint="Enter Username and Password, solve CAPTCHA, and click Sign In.",
                is_completed=lambda page, hist, ctx: "dashboard" in (page.get("url") or "").lower() or any(r["url_key"] in (page.get("url") or "").lower() for r in CANONICAL_KFIN_REPORTS),
            )
        ]
        for rep in reports:
            milestones.append(
                Milestone(
                    id=f"report_{rep['id']}",
                    name=f"Report {rep['name']}",
                    description=f"Process report {rep['name']}.",
                    instruction_hint=f"Configure report {rep['name']}.",
                    is_completed=lambda page, hist, ctx, r=rep: r["id"] in ctx.get("completed_reports", []),
                )
            )
        return milestones

    def _remaining_reports(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        completed = context.get("completed_reports", [])
        return [r for r in context.get("reports", CANONICAL_KFIN_REPORTS) if r["id"] not in completed]

    def _summary(self, context: Dict[str, Any]) -> str:
        completed = context.get("completed_reports", [])
        return f"done={completed}"

    def handle_recovery(self, browser: Any, page: Dict[str, Any], history: List[Dict[str, Any]], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        elements = page.get("elements") or []
        page_text = page.get("text") or ""
        url_low = (page.get("url") or "").lower()

        if len(elements) <= 6:
            banner_close = next((e for e in elements if e.get("role") in ("button", "link") and e.get("label", "").lower() in ("close", "dismiss", "x")), None)
            if banner_close is not None:
                return {"operation": "CLICK", "index": banner_close["index"], "reason": "dismiss banner popup"}

            has_banner = any(e.get("role") == "link" and e.get("label", "").lower() == "banner" for e in elements)
            if has_banner:
                try:
                    p = getattr(browser, "_page", None)
                    if p:
                        if hasattr(p, "keyboard"):
                            p.keyboard.press("Escape")
                        if hasattr(p, "wait_for_timeout"):
                            p.wait_for_timeout(500)
                except Exception:
                    pass
                return {"operation": "WAIT", "index": None, "reason": "dismiss banner overlay via escape"}

        if any(k in page_text.lower() for k in ("already logged in", "session active", "terminate other session", "another session")):
            term_btn = next((e for e in elements if e.get("role") == "button" and any(k in e.get("label", "").lower() for k in ("terminate", "yes", "confirm", "ok"))), None)
            if term_btn:
                return {"operation": "CLICK", "index": term_btn["index"], "reason": "terminate existing session"}

        is_auth = any(k in url_low for k in ("dashboard", "/home", "/main", "reqtransactionreport", "procurementinvestormasterinformation", "sipstpswpregistrationreport", "reqclientwiseaum"))

        if not is_auth:
            user_box = next((e for e in elements if e.get("role") in ("textbox", "searchbox") and "username" in e.get("label", "").lower()), None)
            pwd_box = next((e for e in elements if e.get("role") in ("textbox", "searchbox") and "password" in e.get("label", "").lower()), None)
            captcha_box = next((e for e in elements if e.get("role") in ("textbox", "searchbox") and "captcha" in e.get("label", "").lower()), None)
            signin_btn = next((e for e in elements if e.get("role") == "button" and "sign in" in e.get("label", "").lower()), None)

            if user_box and not str(user_box.get("value") or "").strip():
                return {"operation": "TYPE_TEXT", "index": user_box["index"], "value": str(context.get("username", "")), "reason": "fill username"}
            if pwd_box and not str(pwd_box.get("value") or "").strip():
                return {"operation": "TYPE_TEXT", "index": pwd_box["index"], "value": str(context.get("password", "")), "reason": "fill password"}
            if captcha_box and not str(captcha_box.get("value") or "").strip():
                data_url = _get_captcha_data_url(browser)
                if data_url:
                    sol = _solve_captcha_image(data_url)
                    if sol and len(sol) == 5:
                        return {"operation": "TYPE_TEXT", "index": captcha_box["index"], "value": sol, "reason": "solve CAPTCHA"}
            if user_box and pwd_box and str(user_box.get("value") or "").strip() and str(pwd_box.get("value") or "").strip() and signin_btn:
                if captcha_box and str(captcha_box.get("value") or "").strip():
                    signin_clicks = context.get("signin_clicks", 0)
                    if signin_clicks > 0 and signin_clicks <= 90:
                        context["signin_clicks"] = signin_clicks + 1
                        try:
                            p = getattr(browser, "_page", None)
                            if p:
                                p.wait_for_timeout(2000)
                        except Exception:
                            pass
                        return {"operation": "WAIT", "index": None, "reason": f"waiting for login after Sign In (wait {signin_clicks}/90)"}
                    context["signin_clicks"] = signin_clicks + 1
                    try:
                        p = getattr(browser, "_page", None)
                        if p:
                            p.wait_for_timeout(1000)
                    except Exception:
                        pass
                    return {"operation": "CLICK", "index": signin_btn["index"], "reason": "click Sign In"}

        remaining = self._remaining_reports(context)
        completed = context.setdefault("completed_reports", [])
        if not remaining:
            return {"operation": "DONE", "index": None, "reason": f"all KFintech reports completed {self._summary(context)}"}

        active_rep = remaining[0]
        on_active_rep = active_rep["url_key"] in url_low

        submitted = context.get(f"submitted_{active_rep['id']}", False)
        is_success = False
        if submitted or on_active_rep:
            is_success = any(k in page_text.lower() for k in ("successfully registered", "reference number", "execution queue", "request has been", "already request placed", "request accepted", "request submitted", "mailback request"))

        if is_success:
            ref_match = re.search(r"\b(WB[A-Z0-9]{5,15}|REF\d+|TXN\d+|[A-Z]{2,6}\d{6,}|\d{7,14})\b", page_text)
            ref_no = ref_match.group(1) if ref_match else "CONFIRMED"
            if active_rep["id"] not in completed:
                _save_kfin_confirmation(active_rep["id"], page_text, ref_no)
                completed.append(active_rep["id"])
            context.pop(f"submitted_{active_rep['id']}", None)
            context.pop(f"submit_waits_{active_rep['id']}", None)
            remaining = self._remaining_reports(context)
            if not remaining:
                return {"operation": "DONE", "index": None, "reason": f"all KFintech reports confirmed {self._summary(context)}"}
            dash_link = next((e for e in elements if "dashboard" in e.get("label", "").lower()), None)
            if dash_link:
                return {"operation": "CLICK", "index": dash_link["index"], "reason": "return to dashboard"}

        if context.get(f"submitted_{active_rep['id']}"):
            submit_waits = context.get(f"submit_waits_{active_rep['id']}", 0) + 1
            context[f"submit_waits_{active_rep['id']}"] = submit_waits
            if submit_waits <= 90:
                try:
                    p = getattr(browser, "_page", None)
                    if p:
                        p.wait_for_timeout(2000)
                except Exception:
                    pass
                return {"operation": "WAIT", "index": None, "reason": f"waiting for confirmation reference number (wait {submit_waits}/90)"}
            if active_rep["id"] not in completed:
                _save_kfin_confirmation(active_rep["id"], page_text, "SUBMITTED_TIMEOUT")
                completed.append(active_rep["id"])
            dash_link = next((e for e in elements if "dashboard" in e.get("label", "").lower()), None)
            if dash_link:
                return {"operation": "CLICK", "index": dash_link["index"], "reason": "return to dashboard after submit timeout"}

        if on_active_rep:
            fund_box = next((e for e in elements if e.get("role") == "combobox" and any(k in e.get("label", "").lower() for k in ("fund", "select fund"))), None)
            all_funds_opt = next((e for e in elements if any(k in e.get("label", "").lower() for k in ("all funds", "select all", "all mutual funds")) and not any(k in e.get("label", "").lower() for k in ("split", "nse", "@"))), None)
            fund_cbs = [e for e in elements if e.get("role") == "checkbox" and "@" not in e.get("label", "") and "@" not in str(e.get("value", ""))]
            all_cbs_checked = bool(fund_cbs and all(str(e.get("checked", "")).lower() == "true" or e.get("checked") is True for e in fund_cbs))

            fund_val = str((fund_box or {}).get("value") or "").strip().lower()
            fund_is_selected = bool(
                context.get(f"fund_selected_{active_rep['id']}")
                or all_cbs_checked
                or (fund_val and fund_val != "\u200b" and ("mutual fund" in fund_val or any(k in fund_val for k in ("all", "selected"))))
            )
            if all_cbs_checked:
                context[f"fund_selected_{active_rep['id']}"] = True

            if all_funds_opt is not None and not fund_is_selected:
                context[f"fund_selected_{active_rep['id']}"] = True
                return {"operation": "CLICK", "index": all_funds_opt["index"], "reason": "select All Funds option"}

            if fund_box is not None and not fund_is_selected and not all_funds_opt:
                return {"operation": "CLICK", "index": fund_box["index"], "reason": "open fund selector"}

            if fund_is_selected and any(e.get("role") in ("option", "menuitem") for e in elements):
                try:
                    p = getattr(browser, "_page", None)
                    if p:
                        if hasattr(p, "keyboard"):
                            p.keyboard.press("Escape")
                        if hasattr(p, "locator"):
                            overlay = p.locator(".MuiBackdrop-root")
                            if overlay.count():
                                overlay.first.click()
                        if hasattr(p, "wait_for_timeout"):
                            p.wait_for_timeout(250)
                except Exception:
                    pass
                return {"operation": "WAIT", "index": None, "reason": "dismiss fund dropdown overlay"}

            target_date = str(context.get("target_date") or os.getenv("RTA_FROM_DATE", "01-01-1990"))
            from_date_box = next((e for e in elements if e.get("role") in ("textbox", "searchbox") and "from date" in e.get("label", "").lower() and not any(k in e.get("label", "").lower() for k in ("to date", "to "))), None)
            if from_date_box is not None and (from_date_box.get("value") or "").strip() != target_date:
                return {"operation": "TYPE_TEXT", "index": from_date_box["index"], "value": target_date, "reason": "fill from date"}

            csv_radio = next((e for e in elements if (e.get("role") in ("radio", "checkbox") or str(e.get("type")) == "radio") and ("csv" in e.get("label", "").lower() or str(e.get("value", "")) == "2")), None)
            if csv_radio is not None and str(csv_radio.get("checked", "")).lower() != "true":
                return {"operation": "CLICK", "index": csv_radio["index"], "reason": "select CSV format"}

            target_email = str(context.get("target_email") or "").strip().lower()
            any_recipient = context.get("recipient_mode") == "any"
            matcher = getattr(browser, "recipient_status", None)
            if (not any_recipient and not target_email) or not callable(matcher):
                return {"operation": "NEEDS_INPUT", "reason": "Report submission blocked: local recipient selection is required."}
            try:
                recipient = matcher(target_email, mode="any") if any_recipient else matcher(target_email)
            except Exception as error:
                if browser_input_interrupted(error):
                    raise
                return {"operation": "NEEDS_INPUT", "reason": "Report submission blocked: local recipient verification failed."}
            if recipient.get("matches") != 1 or recipient.get("disabled") or recipient.get("match_node") is None:
                return {"operation": "NEEDS_INPUT", "reason": "Report submission blocked: no eligible email choice is available." if any_recipient else "Report submission blocked: the requested email is missing, ambiguous, or unavailable."}
            other_nodes = recipient.get("other_checked_nodes") or []
            if other_nodes or not recipient.get("checked"):
                node = other_nodes[0] if other_nodes else recipient["match_node"]
                checkbox = next((element for element in elements if element.get("node") == node and element.get("role") == "checkbox"), None)
                if checkbox is None:
                    return {"operation": "NEEDS_INPUT", "reason": "Report submission blocked: the recipient checkbox needs a fresh observation."}
                return {"operation": "CLICK", "index": checkbox["index"], "reason": "uncheck additional recipient" if other_nodes and any_recipient else "uncheck unintended recipient" if other_nodes else "select available recipient" if any_recipient else "select exact matching recipient"}

            pwd_val = str(context.get("report_password", ""))
            pwd_box = next((e for e in elements if e.get("role") in ("textbox", "searchbox") and "extraction password" in e.get("label", "").lower() and "confirm" not in e.get("label", "").lower()), None)
            conf_box = next((e for e in elements if e.get("role") in ("textbox", "searchbox") and "confirm password" in e.get("label", "").lower()), None)

            if pwd_box is not None and not str(pwd_box.get("value") or "").strip():
                return {"operation": "TYPE_TEXT", "index": pwd_box["index"], "value": pwd_val, "reason": "fill extraction password"}
            if conf_box is not None and not str(conf_box.get("value") or "").strip():
                return {"operation": "TYPE_TEXT", "index": conf_box["index"], "value": pwd_val, "reason": "fill confirm password"}

            has_pwds = bool(pwd_box and conf_box and str(pwd_box.get("value") or "").strip() and str(conf_box.get("value") or "").strip())
            if has_pwds:
                if not context.get("want_submit"):
                    if active_rep["id"] not in completed:
                        completed.append(active_rep["id"])
                    remaining = self._remaining_reports(context)
                    if not remaining:
                        return {"operation": "DONE", "index": None, "reason": f"passwords entered and stop requested before submit {self._summary(context)}"}
                    dash_link = next((e for e in elements if "dashboard" in e.get("label", "").lower()), None)
                    if dash_link:
                        return {"operation": "CLICK", "index": dash_link["index"], "reason": "return to dashboard for next report"}
                    return {"operation": "BACK", "index": None, "reason": "navigate back for next report"}
                else:
                    sub_btn = next((e for e in elements if e.get("role") == "button" and e.get("label", "").strip().lower() == "submit"), None)
                    if sub_btn:
                        return {"operation": "CLICK", "index": sub_btn["index"], "reason": "submit report", "report_submission": active_rep["id"]}

        if not on_active_rep and is_auth:
            target_name = active_rep["name"].lower()
            target_id = active_rep["id"].lower()

            search_box = next((e for e in elements if e.get("role") in ("combobox", "searchbox", "textbox") and "available report" in e.get("label", "").lower()), None)
            if search_box is None:
                search_box = next((e for e in elements if e.get("role") in ("combobox", "searchbox", "textbox") and "search" in e.get("label", "").lower()), None)

            if search_box is not None:
                held = str(search_box.get("value") or "").strip().lower()
                if target_name in held or target_id in held:
                    opt = next((e for e in elements if e.get("role") in ("option", "link", "button", "listitem") and (target_name in e.get("label", "").lower() or target_id in e.get("label", "").lower())), None)
                    if opt is not None:
                        return {"operation": "CLICK", "index": opt["index"], "reason": f"select {active_rep['name']} option"}
                return {"operation": "TYPE_TEXT", "index": search_box["index"], "value": active_rep["name"], "reason": f"search {active_rep['name']}"}

            direct_el = next(
                (e for e in elements if e.get("role") in ("option", "link", "button") and not any(k in e.get("label", "").lower() for k in ("completed", "pending", "failed", "wbtrn", "wbcum", "wsreg")) and (target_name in e.get("label", "").lower() or f"mfsd{target_id}" in e.get("label", "").lower())),
                None,
            )
            if direct_el is not None:
                return {"operation": "CLICK", "index": direct_el["index"], "reason": f"click {active_rep['name']}"}

        return None

    def record_action_success(self, move: Dict[str, Any], context: Dict[str, Any]) -> None:
        report_id = move.get("report_submission")
        if report_id and any(report["id"] == report_id for report in self._remaining_reports(context)):
            context[f"submitted_{report_id}"] = True

    def should_terminate(self, goal: str, history: List[Dict[str, Any]], context: Dict[str, Any]) -> bool:
        if not self._remaining_reports(context):
            return True
        reports = context.get("reports", [])
        completed = context.get("completed_reports", [])
        return bool(reports and len(completed) >= len(reports))

    def get_budget(self, goal: str, default_budget: int = 35) -> int:
        n = len(self._get_target_reports(goal))
        return max(default_budget, 20 + n * 20)

"""Text model writer: site guesses, search queries, field values. Clean JSON only."""

import datetime
import json
from core.jev.logger import redact_sensitive
from core.jev.model_config import browser_model_config
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

from recipes.registry import find_recipe


CLIENT = httpx.Client(http2=True, timeout=30)
SESSION_ID = os.environ.get("OPENCODE_SESSION") or uuid.uuid4().hex


def _config() -> Tuple[str, str, str]:
    return browser_model_config()


def _parse_json(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except ValueError:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            return json.loads(text[s : e + 1])
        raise ValueError(f"No JSON in LLM reply: {text[:80]}")


def _ask(system: str, user: Any) -> Dict[str, Any]:
    return _ask_json(system, user)


def _ask_json(system: str, user: Any, strict: bool = True) -> Dict[str, Any]:
    _ = strict
    base_url, model, key = _config()
    headers: Dict[str, str] = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if "opencode.ai" in base_url:
        headers.update({"User-Agent": "jev-agent/0.2", "x-opencode-session": SESSION_ID})

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system + " Reply in JSON."},
        {"role": "user", "content": redact_sensitive(json.dumps(user))},
    ]

    resp = CLIENT.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers=headers,
        json={"model": model, "max_tokens": 4096, "messages": messages},
    )
    resp.raise_for_status()
    choices = resp.json().get("choices", [])
    if not choices:
        raise ValueError("Text model returned no choices.")
    msg: Dict[str, Any] = choices[0].get("message", {})
    content: str = msg.get("content", "")
    if not content and msg.get("reasoning_content"):
        content = msg["reasoning_content"]
    return _parse_json(content)


def guess_site(goal: str) -> str:
    """Direct site URL for the goal, or empty string when unsure."""
    # Check if a matching domain recipe specifies a default starting URL
    recipe = find_recipe(goal)
    if recipe and recipe.default_start_url:
        return recipe.default_start_url

    out = _ask(
        'Guess the single best website URL to start this task. '
        'Reply {"url": "https://..."} or {"url": ""} when unsure.',
        {"goal": goal},
    )
    url = str(out.get("url", "")).strip()
    return url if url.startswith("http") else ""


def search_query(goal: str) -> str:
    """Google query that finds where to do the goal."""
    out = _ask('Write a short search engine query for this task. Reply {"query": "..."}.', {"goal": goal})
    return str(out.get("query", goal)).strip() or goal


def field_text(
    goal: str,
    field_label: str,
    page_title: str,
    role: Optional[str] = None,
    section_context: Optional[str] = None,
    field_hints: Optional[Dict[str, Any]] = None,
) -> str:
    """Determine the exact text value to type into a web form field using LLM reasoning."""
    instructions = (
        "You are an assistant determining the exact text value to type into a web form field. "
        "Based on the user goal, current page title, field label, element role, and section context, "
        "extract the exact value.\n"
        "RULES:\n"
        "1. Output ONLY the raw literal value to type. Never include explanations, conversational filler, or surrounding quotes.\n"
        '2. NEVER include command verbs such as "type", "enter", "search for", "input", or "select".\n'
        "3. If the field is a search, filter, or combobox, extract the specific query or item requested in the goal.\n"
        "4. If the field asks for credentials, email, or contact info, extract the exact value specified in the goal.\n"
        "5. If the field is a date input, extract the requested date and match any indicated format.\n"
        'Reply strictly in JSON: {"text": "..."}.'
    )
    payload = {
        "goal": goal,
        "field": field_label,
        "page": page_title,
    }
    hints = field_hints or {}
    lbl_low = field_label.lower()
    if any(k in lbl_low for k in ("password", "pwd")):
        if any(k in lbl_low for k in ("confirm", "re-enter", "reenter")):
            rep_pwd_m = re.search(r"(?:extraction\s+password|report\s+password|confirm\s+password|confirm)\s*[:=]?\s*([^\s,;]+)", goal, re.IGNORECASE)
            if rep_pwd_m:
                return rep_pwd_m.group(1).rstrip(".,;!?")
            if hints.get("report_password"):
                return str(hints["report_password"]).rstrip(".,;!?")
        elif any(k in lbl_low for k in ("extraction", "report", "self")):
            rep_pwd_m = re.search(r"(?:extraction\s+password|report\s+password)\s*[:=]?\s*([^\s,;]+)", goal, re.IGNORECASE)
            if rep_pwd_m:
                return rep_pwd_m.group(1).rstrip(".,;!?")
            if hints.get("report_password"):
                return str(hints["report_password"]).rstrip(".,;!?")
        else:
            login_pwd_m = re.search(r"(?:login\s+password|(?:^|[\s,;])password)\s*[:=]?\s*([^\s,;]+)", goal, re.IGNORECASE)
            if login_pwd_m:
                return login_pwd_m.group(1).rstrip(".,;!?")
            if hints.get("password"):
                return str(hints["password"]).rstrip(".,;!?")
    if any(k in lbl_low for k in ("username", "user id", "userid", "login id")):
        user_m = re.search(r"(?:username|user\s*id|login\s*id)\s*[:=]?\s*([A-Za-z0-9_-]+)", goal, re.IGNORECASE)
        if not user_m:
            user_m = re.search(r"\b(ARN[-_]?\d+)\b", goal, re.IGNORECASE)
        if user_m:
            val = user_m.group(1).rstrip(".,;!?")
            if val.startswith("-"):
                val = f"ARN{val}"
            return val
        if hints.get("username"):
            return str(hints["username"]).rstrip(".,;!?")

    if "email" in lbl_low and hints.get("email"):
        return str(hints["email"])
    if "arn" in lbl_low and hints.get("arn"):
        return str(hints["arn"])
    if role:
        payload["role"] = role
    if section_context:
        payload["section_context"] = section_context

    out = {}
    try:
        out = _ask(instructions, payload)
    except Exception:
        out = {}
    text = str(out.get("text", "")).strip()

    text = text.strip('"\'` ')
    text = re.sub(r"^(?:type|enter|input|search\s+for|search|select)\s+", "", text, flags=re.IGNORECASE).strip()
    text = text.rstrip(".:;")

    hints = field_hints or {}
    date_format = hints.get("date_format")
    lbl_date = field_label.lower()
    if date_format and ("from" in lbl_date or "period" in lbl_date or ("date" in lbl_date and "to date" not in lbl_date and "to-date" not in lbl_date)):
        raw_d_match = re.search(r"\b\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,})[-/.]\d{2,4}\b", text or goal)
        if raw_d_match:
            raw_d = raw_d_match.group(0)
            for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%b-%Y", "%d/%b/%Y"):
                try:
                    dt = datetime.datetime.strptime(raw_d, fmt)
                    text = dt.strftime(date_format)
                    break
                except ValueError:
                    pass

    # Safety fallbacks if model returned empty
    if not text:
        lbl_low = field_label.lower()
        if "email" in lbl_low:
            m = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", goal)
            if m:
                text = m.group(0)
        elif "from" in lbl_low or "period" in lbl_low or ("date" in lbl_low and "to date" not in lbl_low):
            m = re.search(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b", goal)
            if m:
                text = m.group(0)
        elif any(k in lbl_low for k in ("search", "filter", "report", "description")):
            if section_context:
                m = re.search(r"\b[A-Za-z0-9_-]{2,15}\b", section_context)
                if m:
                    text = m.group(0)

    if not text:
        raise ValueError("Text model returned no value; nothing typed.")

    return text

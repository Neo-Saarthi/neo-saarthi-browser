import datetime
import json
import os
import re
import uuid
from typing import Any, Dict, Optional
import httpx
from core.jev.recipes.registry import find_recipe
from core.jev.model_config import browser_model_config
from core.jev.logger import redact_sensitive

CLIENT = httpx.Client(timeout=30)
SESSION_ID = os.environ.get('OPENCODE_SESSION') or uuid.uuid4().hex

def _ask(system: str, user: Any):
    last = None
    for strict in (True, False):
        try:
            return _ask_json(system, user, strict)
        except (ValueError, KeyError, IndexError) as e:
            last = e
    raise RuntimeError(f'text model returned unusable JSON: {last}')

def _ask_json(system: str, user: Any, strict: bool=True):
    base_url, model, key = browser_model_config()
    headers = {'Authorization': f'Bearer {key}'}
    if 'opencode.ai' in base_url:
        headers.update({'User-Agent': 'jev-agent/0.2', 'x-opencode-session': SESSION_ID})
    payload = {'model': model, 'max_tokens': 256, 'messages': [{'role': 'system', 'content': system + ' Reply in JSON.'}, {'role': 'user', 'content': redact_sensitive(json.dumps(user))}]}
    if strict:
        payload['response_format'] = {'type': 'json_object'}

    def post(body: Any) -> Any:
        return CLIENT.post(base_url + '/chat/completions', headers=headers, json=body)
    response = post(payload)
    if response.status_code == 400 and 'json_validate' in response.text and strict:
        payload.pop('response_format', None)
        response = post(payload)
    if response.is_error:
        raise RuntimeError(f'Text model returned HTTP {response.status_code}: {response.text[:150]}')
    content = response.json()['choices'][0]['message']['content'] or ''
    try:
        return json.loads(content)
    except ValueError:
        start, end = (content.find('{'), content.rfind('}'))
        if start < 0 or end <= start:
            raise ValueError(f'no JSON in text model reply: {content[:80]!r}')
        return json.loads(content[start:end + 1])

def guess_site(goal: str) -> str:
    recipe = find_recipe(goal)
    if recipe and recipe.default_start_url:
        return recipe.default_start_url
    out = _ask('Guess the single best website URL to start this task. Reply {"url": "https://..."} or {"url": ""} when unsure.', {'goal': goal})
    url = str(out.get('url', '')).strip()
    return url if url.startswith('http') else ''

def search_query(goal: str) -> str:
    out = _ask('Write a short search engine query for this task. Reply {"query": "..."}.', {'goal': goal})
    return str(out.get('query', goal)).strip() or goal

def field_text(goal: str, field_label: str, page_title: str, role: Optional[str]=None, section_context: Optional[str]=None, field_hints: Optional[Dict[str, Any]]=None) -> str:
    instructions = 'You are an assistant determining the exact text value to type into a web form field. Based on the user goal, current page title, field label, element role, and section context, extract the exact value.\nRULES:\n1. Output ONLY the raw literal value to type. Never include explanations, conversational filler, or surrounding quotes.\n2. NEVER include command verbs such as "type", "enter", "search for", "input", or "select".\n3. If the field is a search, filter, or combobox, extract the specific query or item requested in the goal.\n4. If the field asks for credentials, email, or contact info, extract the exact value specified in the goal.\n5. If the field is a date input, extract the requested date and match any indicated format.\nReply strictly in JSON: {"text": "..."}.'
    payload = {'goal': goal, 'field': field_label, 'page': page_title}
    hints = field_hints or {}
    lbl_low = field_label.lower()
    if any((k in lbl_low for k in ('password', 'pwd'))):
        val = hints.get('password') or hints.get('report_password')
        if val:
            return val
    if any((k in lbl_low for k in ('username', 'user id', 'userid', 'login id'))):
        val = hints.get('username')
        if val:
            return val
    if role:
        payload['role'] = role
    if section_context:
        payload['section_context'] = section_context
    out = {}
    try:
        out = _ask(instructions, payload)
    except Exception:
        out = {}
    text = str(out.get('text', '')).strip()
    text = text.strip('"\'` ')
    text = re.sub('^(?:type|enter|input|search\\s+for|search|select)\\s+', '', text, flags=re.IGNORECASE).strip()
    text = text.rstrip('.:;')
    hints = field_hints or {}
    date_format = hints.get('date_format')
    if date_format and any((k in field_label.lower() for k in ('date', 'period', 'from', 'to'))):
        raw_d_match = re.search('\\b\\d{1,2}[-/.]\\d{1,2}[-/.]\\d{2,4}\\b', text or goal)
        if raw_d_match:
            raw_d = raw_d_match.group(0)
            for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%d.%m.%Y', '%Y-%m-%d'):
                try:
                    dt = datetime.datetime.strptime(raw_d, fmt)
                    text = dt.strftime(date_format)
                    break
                except ValueError:
                    pass
    if not text:
        lbl_low = field_label.lower()
        if 'email' in lbl_low:
            m = re.search('[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\\.[a-zA-Z0-9-.]+', goal)
            if m:
                text = m.group(0)
        elif any((k in lbl_low for k in ('date', 'period'))):
            m = re.search('\\b\\d{1,2}[-/.]\\d{1,2}[-/.]\\d{2,4}\\b', goal)
            if m:
                text = m.group(0)
        elif any((k in lbl_low for k in ('search', 'filter', 'report', 'description'))):
            if section_context:
                m = re.search('\\b[A-Za-z0-9_-]{2,15}\\b', section_context)
                if m:
                    text = m.group(0)
    if not text:
        raise ValueError('Text model returned no value; nothing typed.')
    return text

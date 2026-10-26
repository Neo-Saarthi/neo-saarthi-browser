"""Logger and run tracing for Browser Automation agent."""

import json
import os
from datetime import datetime
from pathlib import Path
from threading import local
from typing import Any, Dict, List, Optional

LOGS_DIR: str = str(
    Path(os.getenv("MF_RUN_LOG_DIR", str(Path(__file__).resolve().parents[3] / "logs"))).expanduser().resolve()
    / "browser_use"
)


_SENSITIVE_VALUES = local()


def set_sensitive_values(values: List[str]) -> None:
    _SENSITIVE_VALUES.values = values


def redact_sensitive(value: str) -> str:
    for secret in getattr(_SENSITIVE_VALUES, 'values', []):
        value = value.replace(secret, '********').replace(json.dumps(secret)[1:-1], '********')
    return value


def _mask_sensitive_text(text, element=None):
    if text is None:
        return None
    res = redact_sensitive(str(text))
    pw = os.environ.get("KFINTECH_PASSWORD")
    if pw and pw in res:
        res = res.replace(pw, "********")
    if element:
        lbl = str(element.get("label", "")).lower()
        if element.get("isPassword") or "password" in lbl or "pwd" in lbl:
            return "********"
    return res


def _sanitize_element(element):
    if not element:
        return element
    el = dict(element)
    lbl = str(el.get("label", "")).lower()
    pw = os.environ.get("KFINTECH_PASSWORD")
    if el.get("isPassword") or "password" in lbl or "pwd" in lbl:
        if "value" in el and el["value"]:
            el["value"] = "********"
    elif pw and "value" in el and pw in str(el["value"]):
        el["value"] = str(el["value"]).replace(pw, "********")
    return el


MAX_ACTION_BYTES: int = 5 * 1024 * 1024


def _cut(v: Any, n: int) -> str:
    return str(v if v is not None else "")[:n]


class AgentLogger:
    def __init__(self, goal="run"):
        os.makedirs(LOGS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_goal = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in goal)[:30].strip("_")
        name = f"{timestamp}_{safe_goal}" if safe_goal else timestamp
        self.log_path = os.path.join(LOGS_DIR, f"{name}.log")
        self.json_path = os.path.join(LOGS_DIR, f"{name}.json")
        self.actions_jsonl_path = os.path.join(LOGS_DIR, "browser-actions.jsonl")
        self._file = open(self.log_path, "a", encoding="utf-8")
        self.events = []
        self.recorded_actions: List[Dict[str, Any]] = []
        self._log("INIT", f"Run started: goal={goal!r}")

    def _rotate_actions_if_big(self) -> None:
        try:
            if os.path.exists(self.actions_jsonl_path) and os.path.getsize(self.actions_jsonl_path) > MAX_ACTION_BYTES:
                backup = f"{self.actions_jsonl_path}.1"
                if os.path.exists(backup):
                    os.remove(backup)
                os.rename(self.actions_jsonl_path, backup)
        except Exception:
            pass

    def record_action(
        self,
        action_type: str,
        name: str = "",
        tag: str = "",
        selector: str = "",
        link: Optional[str] = None,
        value: Optional[str] = None,
    ) -> None:
        try:
            target_data: Dict[str, Any] = {
                "name": _cut(name, 120),
                "tag": _cut(tag, 20),
                "selector": _cut(selector, 200),
            }
            if link:
                target_data["link"] = _cut(link, 500)
            if value:
                target_data["value"] = _cut(value, 200)
            entry = {"type": action_type, "target": target_data}
            entry = json.loads(redact_sensitive(json.dumps(entry)))
            if entry.get("target", {}).get("value") == "********":
                entry["target"].pop("value", None)
            self.recorded_actions.append(entry)
            line = redact_sensitive(json.dumps(entry))
            self._rotate_actions_if_big()
            with open(self.actions_jsonl_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            link_part = f" {target_data['link']}" if "link" in target_data else ""
            val_part = f" value=\"{target_data['value']}\"" if "value" in target_data else ""
            print(redact_sensitive(f"[{action_type}] <{target_data['tag']}> \"{target_data['name']}\"{val_part}{link_part}"), flush=True)
        except Exception as e:
            print(f"[log-error] {e}", flush=True)

    def _log(self, level, message, file=True, **data):
        message = redact_sensitive(str(message))
        data = json.loads(redact_sensitive(json.dumps(data, default=str)))
        now_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        prefix = f"[{now_str}] [{level:<6}]"
        line = f"{prefix} {message}"
        if file:
            try:
                print(line, flush=True)
            except Exception:
                try:
                    print(line.encode("ascii", errors="replace").decode("ascii"), flush=True)
                except Exception:
                    pass
            try:
                self._file.write(line + "\n")
                self._file.flush()
            except Exception:
                pass
        self.events.append({
            "timestamp": now_str,
            "level": level,
            "message": message,
            "data": data,
        })
        self._flush_json()

    def _flush_json(self):
        try:
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(self.events, f, indent=2, default=str)
        except Exception:
            pass

    def info(self, msg, **data):
        keep = msg.startswith(("--- Task Step", "decision thought:", "Recipe recovery action:", "Task finished", "Matched workflow recipe:", "Goal reported DONE", "Calendar ", "Saved-secret entry:", "Recipient selection:"))
        self._log("INFO", msg, file=keep, **data)

    def warn(self, msg, **data):
        cap = 2000 if msg.startswith("LLM decider error") else 220
        line = msg.split("\n")[0][:cap]
        self._log("WARN", line, file=True)

    def error(self, msg, **data):
        line = msg.split("\n")[0][:220]
        self._log("ERROR", line, file=True)

    def decision(self, operation, index, element=None, probabilities=None, confidence=None):
        safe_el = _sanitize_element(element)
        el_desc = f" [{index}]" if index is not None else ""
        if safe_el:
            el_desc += f" ({safe_el.get('role', '')} {safe_el.get('label', '')!r})"
        msg = f"Decision: {operation}{el_desc}"
        if confidence is not None:
            msg += f" (confidence: {confidence:.2f})"
        self._log("DECIDE", msg, file=True, operation=operation, index=index, element=safe_el, probabilities=probabilities, confidence=confidence)

    def act_start(self, operation, index, element=None, text=None):
        safe_el = _sanitize_element(element)
        safe_text = _mask_sensitive_text(text, element)
        el_desc = f" [{index}]" if index is not None else ""
        if safe_el:
            el_desc += f" ({safe_el.get('role', '')} {safe_el.get('label', '')!r})"
        msg = f"Act: {operation}{el_desc}"
        if safe_text:
            msg += f" with text={safe_text!r}"
        self._log("ACT", msg, file=True, operation=operation, index=index, element=safe_el, text=safe_text)

    def act_success(self, result_msg, page_url=None):
        self._log("SUCCESS", f"Act ok: {result_msg} (url: {page_url})", file=False, result=result_msg, url=page_url)

    def act_fail(self, error, operation, index, element=None, fail_count=1):
        safe_el = _sanitize_element(element)
        el_desc = f" [{index}]" if index is not None else ""
        if safe_el:
            el_desc += f" ({safe_el.get('role', '')} {safe_el.get('label', '')!r})"
        msg = f"Act FAILED: {operation}{el_desc} (fail #{fail_count}) -> {error}"
        self._log("FAIL", msg, file=True, error=str(error), operation=operation, index=index, element=safe_el, fail_count=fail_count)

    def page_state(self, url, title, elements_count=0, sample_elements=None, scroll_percent=None, at_bottom=False, all_elements=None):
        safe_sample = [_sanitize_element(e) for e in (sample_elements or [])]
        safe_all = [_sanitize_element(e) for e in (all_elements or [])]
        state = f"State: {title!r} | {url} | elements={elements_count}"
        self._log(
            "PAGE",
            state,
            file=True,
            url=url,
            title=title,
            elements_count=elements_count,
            sample_elements=safe_sample,
            all_elements=safe_all,
            scroll_percent=scroll_percent,
            at_bottom=at_bottom,
        )

    def site_check(self, url, score, note):
        self._log("JUDGE", f"Site score={score:.2f} ({note}) for {url}", file=False, url=url, score=score, note=note)

    def close(self):
        try:
            self._log("END", f"Run ended. Full logs saved to: {self.log_path}")
            self._flush_json()
            self._file.close()
        except Exception:
            pass


_CURRENT_LOGGER = None


def get_logger(goal=None):
    global _CURRENT_LOGGER
    if _CURRENT_LOGGER is None:
        _CURRENT_LOGGER = AgentLogger(goal or "run")
    return _CURRENT_LOGGER


def set_logger(logger):
    global _CURRENT_LOGGER
    _CURRENT_LOGGER = logger
    return _CURRENT_LOGGER

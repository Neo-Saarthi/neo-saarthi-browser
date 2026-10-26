import json
import math
import re
from typing import Any, Dict, Set


STANDARD_ACTIONS: Set[str] = set("reload_extension browser_info get_permissions set_permission allow_origin set_allow_all_sites set_capability_policy set_history_access list_tabs list_windows list_tab_groups history bookmarks download_status claim_tab release_tab open create_window group_tabs ungroup_tabs google_ai_mode restart_extension move_tab snapshot youtube_transcript selected_text screenshot scroll navigate download cancel_download click double_click hover pointer drag fill upload press dialog wait".split())
HYBRID_ACTIONS: Set[str] = set("browser_info list_tabs claim_tab open_tab navigate reload go_back release_tab close_tab snapshot click fill prepare_secret verify_secret recipient_status select press scroll wait screenshot page_operation click_at".split())


def validate_browser_command(action: str, arguments: Dict[str, Any]) -> None:
    if not isinstance(action, str):
        raise ValueError("Invalid browser action.")
    if not isinstance(arguments, dict):
        raise ValueError("Invalid browser command arguments.")
    protocol = arguments.get("bridge_protocol")
    if protocol is not None and protocol != "jev":
        raise ValueError("Unsupported browser protocol.")
    version = arguments.get("protocol_version", 1)
    if type(version) is not int or version != 1:
        raise ValueError("Unsupported browser protocol version.")
    if action not in (HYBRID_ACTIONS if protocol == "jev" else STANDARD_ACTIONS):
        raise ValueError("Unsupported browser action.")
    if action in ("prepare_secret", "verify_secret"):
        allowed = {"bridge_protocol", "protocol_version", "trace_timing", "tab_id", "node", "purpose", "expected_url"}
        if set(arguments) - allowed or arguments.get("purpose") not in ("password", "report_password") or not isinstance(arguments.get("expected_url"), str) or len(arguments["expected_url"]) > 8192:
            raise ValueError("Saved-password preparation accepts destination metadata only.")
    if action == "recipient_status":
        allowed = {"bridge_protocol", "protocol_version", "trace_timing", "tab_id", "recipient_email", "recipient_mode"}
        if set(arguments) - allowed or (arguments.get("recipient_mode", "exact") == "exact" and not arguments.get("recipient_email")):
            raise ValueError("Recipient matching accepts target email metadata only.")
    if "recipient_mode" in arguments and (action not in ("recipient_status", "click", "click_at", "press") or arguments["recipient_mode"] not in ("exact", "any")):
        raise ValueError("Invalid recipient selection mode.")
    if "recipient_email" in arguments:
        email = arguments["recipient_email"]
        if action not in ("recipient_status", "click", "click_at", "press") or not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("Invalid recipient email metadata.")

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 8:
            raise ValueError("Browser arguments are too deeply nested.")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and (abs(value) > 9007199254740991 or not math.isfinite(value)):
            raise ValueError("Invalid numeric browser argument.")
        if isinstance(value, str) and len(value) > 100000:
            raise ValueError("Browser argument exceeds the size limit.")
        if isinstance(value, (dict, list)):
            if len(value) > 1000:
                raise ValueError("Too many browser arguments.")
            if isinstance(value, dict):
                for key, child in value.items():
                    if not isinstance(key, str) or key in {"__proto__", "constructor", "prototype", "script"}:
                        raise ValueError("Executable or unsafe browser arguments are not accepted.")
                    visit(child, depth + 1)
            else:
                for child in value:
                    visit(child, depth + 1)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("Browser arguments must be JSON data.")

    visit(arguments)
    if len(json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))) > 512000:
        raise ValueError("Browser arguments exceed the size limit.")
    for key in ("tab_id", "window_id", "download_id", "frame_id", "node"):
        value = arguments.get(key)
        if value is None:
            continue
        if not re.fullmatch(r"0|[1-9]\d*", str(value), flags=re.ASCII) or int(value) > 2147483647 or (key != "frame_id" and int(value) == 0):
            raise ValueError(f"Invalid {key}.")
    for key in ("x", "y", "start_x", "start_y", "end_x", "end_y"):
        value = arguments.get(key)
        if key in arguments and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100000):
            raise ValueError(f"Invalid {key}.")
    for key in ("ms", "wait_ms", "timeout_ms"):
        value = arguments.get(key)
        if key in arguments and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 30000):
            raise ValueError(f"Invalid {key}.")
    for key in ("url", "origin"):
        value = arguments.get(key)
        if key in arguments and (not isinstance(value, str) or len(value) > 8192):
            raise ValueError(f"Invalid {key}.")

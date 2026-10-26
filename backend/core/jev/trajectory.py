import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TRAJECTORIES_DIR: str = str(
    Path(os.getenv("MF_RUN_LOG_DIR", str(Path(__file__).resolve().parents[3] / "logs"))).expanduser().resolve()
    / "browser_use"
    / "trajectories"
)


def normalize_task_key(goal: str, start_url: Optional[str] = None) -> str:
    cleaned = (goal or "").lower()
    portal = "web"
    if any(k in cleaned for k in ("karvy", "kfin", "kfintech", "dss")):
        portal = "kfintech"
    elif any(k in cleaned for k in ("cams", "camsonline", "wbr")):
        portal = "cams"
    elif start_url:
        domain_match = re.search(r"https?://(?:www\.)?([^/]+)", start_url)
        if domain_match:
            portal = re.sub(r"[^a-z0-9]", "_", domain_match.group(1).lower())[:25]

    reports = sorted(re.findall(r"\b(?:mfsd\d+|wbr\d+)\b", cleaned))
    format_type = "csv" if "csv" in cleaned else "excel" if "excel" in cleaned or "xls" in cleaned else "default"
    
    parts = [portal]
    if reports:
        parts.extend(reports)
    parts.append(format_type)

    base = "_".join(parts)
    sanitized = re.sub(r"[^a-z0-9_]+", "_", base).strip("_")
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized}_{digest}"


def get_trajectory_path(task_key: str) -> str:
    os.makedirs(TRAJECTORIES_DIR, exist_ok=True)
    return os.path.join(TRAJECTORIES_DIR, f"{task_key}.json")


def load_trajectory(task_key: str) -> Optional[Dict[str, Any]]:
    path = get_trajectory_path(task_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and data.get("steps"):
                return data
    except Exception:
        pass
    return None


def match_playbook_action(
    trajectory: Dict[str, Any],
    step_index: int,
    page_elements: List[Dict[str, Any]],
    lookahead: int = 2,
) -> Optional[Tuple[Dict[str, Any], int]]:
    steps = trajectory.get("steps", [])
    if not steps or step_index < 0 or step_index >= len(steps):
        return None

    max_idx = min(len(steps), step_index + max(1, lookahead + 1))
    for idx in range(step_index, max_idx):
        candidate = steps[idx]
        a_type = (candidate.get("type") or "").strip().lower()
        target = candidate.get("target") or {}
        cand_label = (target.get("name") or target.get("label") or "").strip().lower()

        if a_type == "navigate" or not cand_label:
            continue

        if "captcha" in cand_label:
            continue

        match = next(
            (
                e for e in page_elements
                if cand_label in (e.get("label") or "").strip().lower()
                or (e.get("label") or "").strip().lower() in cand_label
            ),
            None,
        )
        if not match:
            continue

        matched_index = str(match.get("index"))
        matched_role = (match.get("role") or "").lower()

        op = "CLICK"
        if a_type == "type" or matched_role in ("textbox", "searchbox"):
            op = "TYPE_TEXT"
        elif a_type == "click":
            op = "CLICK"

        val = target.get("value")
        action_dict = {
            "operation": op,
            "index": matched_index,
            "value": val,
            "reason": f"Playbook replay step #{idx + 1}: {op} [{matched_index}] '{cand_label}'",
        }
        return action_dict, idx

    return None


def find_playbook_step(
    trajectory: Dict[str, Any],
    step_index: int,
    page_elements: List[Dict[str, Any]],
    lookahead: int = 2,
) -> Optional[Tuple[Dict[str, Any], int]]:
    steps = trajectory.get("steps", [])
    if not steps or step_index < 0 or step_index >= len(steps):
        return None

    max_idx = min(len(steps), step_index + max(1, lookahead + 1))
    for idx in range(step_index, max_idx):
        candidate = steps[idx]
        target = candidate.get("target") or {}
        cand_label = (target.get("name") or target.get("label") or "").strip().lower()
        if not cand_label:
            continue

        match = next(
            (
                e for e in page_elements
                if cand_label in (e.get("label") or "").strip().lower()
                or (e.get("label") or "").strip().lower() in cand_label
            ),
            None,
        )
        if match:
            result = dict(candidate)
            result["matched_index"] = str(match.get("index"))
            return result, idx

    return None


def save_or_optimize_trajectory(
    task_key: str,
    goal: str,
    raw_actions: List[Dict[str, Any]],
    status: str = "done",
) -> Optional[Dict[str, Any]]:
    if not raw_actions or status != "done":
        return None

    effective_steps: List[Dict[str, Any]] = []
    for item in raw_actions:
        a_type = item.get("type")
        target = item.get("target") or {}
        if not a_type:
            continue
        cleaned_entry = {
            "type": a_type,
            "target": {
                "name": str(target.get("name") or "")[:120],
                "tag": str(target.get("tag") or "")[:20],
                "selector": str(target.get("selector") or "")[:200],
            },
        }
        is_captcha = "captcha" in str(target.get("name") or "").lower() or "captcha" in str(target.get("selector") or "").lower()
        if "value" in target and target["value"] and not is_captcha:
            cleaned_entry["target"]["value"] = str(target["value"])[:200]
        if "link" in target and target["link"]:
            cleaned_entry["target"]["link"] = str(target["link"])[:500]
        effective_steps.append(cleaned_entry)

    if not effective_steps:
        return None

    existing = load_trajectory(task_key)
    if existing:
        existing_step_count = len(existing.get("steps", []))
        if existing_step_count > 0 and len(effective_steps) >= existing_step_count:
            return existing

    record: Dict[str, Any] = {
        "task_key": task_key,
        "goal": goal,
        "total_steps": len(effective_steps),
        "status": status,
        "steps": effective_steps,
    }

    path = get_trajectory_path(task_key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        return record
    except Exception:
        return None

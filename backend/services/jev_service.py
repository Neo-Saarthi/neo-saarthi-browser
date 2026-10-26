import asyncio
from datetime import datetime
import json
import os
import time
from threading import Lock
from typing import Any, Dict, List, Optional
from core.jev import browser_factory, logger, loop
from core.jev.saarthi_browser import SaarthiRecipeBrowser
from core.browser_use.cursor import browser_cursor
from core.browser_use.secret_entry import SecretEntryError, task_secrets
from recipes.registry import find_recipe

_EXECUTION_LOCK = Lock()


def _find_site_with_resume(browser: Any, goal: str) -> bool:
    while True:
        try:
            if task_secrets.get() and callable(getattr(browser, "check_saved_secret_access", None)):
                browser.check_saved_secret_access()
            return loop.find_site(browser, goal)
        except Exception as error:
            waiter = getattr(browser, "wait_until_resumed", None)
            if not loop.browser_input_interrupted(error) or not callable(waiter) or not waiter():
                raise


def _execute_jev_sync(goal: str, budget: int, distributor_id: str, tab_id: Optional[str], visible_browser: bool = False, client_id: Optional[str] = None, event_loop: Optional[asyncio.AbstractEventLoop] = None) -> Dict[str, Any]:
    log = logger.AgentLogger(f"{distributor_id}:{goal}")
    logger.set_logger(log)
    started_at = datetime.now().isoformat()
    env_budget = int(os.environ.get("MAX_TASK_STEPS", str(budget)))
    matched_recipe = find_recipe(goal)
    task_budget = matched_recipe.get_budget(goal, env_budget) if matched_recipe else env_budget
    try:
        if visible_browser and event_loop is None:
            raise ValueError("A running browser bridge is required.")
        b = SaarthiRecipeBrowser(distributor_id, client_id, event_loop) if visible_browser else browser_factory.get_browser()
    except Exception:
        log.close()
        logger.set_logger(None)
        raise
    if tab_id and hasattr(b, "_tab_id"):
        setattr(b, "_tab_id", int(tab_id) if str(tab_id).isdigit() else tab_id)
    started = time.perf_counter()
    try:
        raw_tab_id = getattr(b, "_tab_id", None)
        tab_id_str = str(raw_tab_id) if raw_tab_id is not None else None
        if not _find_site_with_resume(b, goal):
            return {
                "status": "stopped",
                "message": "could not reach a usable site",
                "steps": 0,
                "tab_id": tab_id_str,
                "current_url": getattr(b, "url", ""),
                "elapsed_seconds": round(time.perf_counter() - started, 1),
                "history": [],
                "confirmations": [],
            }
        result = loop.run_task(b, goal, task_budget)
        elapsed = round(time.perf_counter() - started, 1)
        res_str = str(result).lower()
        status = "done" if res_str.strip() == "done" else "needs_input" if res_str.startswith("needs_input") else "stopped"
        raw_tab_id = getattr(b, "_tab_id", None)
        tab_id_str = str(raw_tab_id) if raw_tab_id is not None else None
        confirmations: List[Dict[str, Any]] = []
        root_logs_dir = logger.LOGS_DIR
        cams_path = os.path.join(root_logs_dir, "cams_confirmations.json")
        kfin_path = os.path.join(root_logs_dir, "kfin_confirmations.json")
        if "cams" in goal.lower():
            if os.path.exists(cams_path):
                try:
                    with open(cams_path, "r", encoding="utf-8") as f:
                        all_cams = json.load(f)
                        confirmations = [item for item in all_cams if item.get("timestamp", "") >= started_at] if isinstance(all_cams, list) else []
                except Exception:
                    pass
        elif "karvy" in goal.lower() or "kfin" in goal.lower():
            if os.path.exists(kfin_path):
                try:
                    with open(kfin_path, "r", encoding="utf-8") as f:
                        all_kfin = json.load(f)
                        if isinstance(all_kfin, dict):
                            confirmations = [item for item in all_kfin.values() if item.get("timestamp", "") >= started_at]
                except Exception:
                    pass
        return {
            "status": status,
            "message": logger.redact_sensitive(str(result)),
            "steps": task_budget,
            "tab_id": tab_id_str,
            "current_url": getattr(b, "url", ""),
            "elapsed_seconds": elapsed,
            "history": getattr(b, "task_history", []),
            "confirmations": confirmations,
        }
    except Exception as error:
        if isinstance(error, SecretEntryError):
            log.warn(str(error))
            return {
                "status": "needs_input",
                "message": str(error),
                "steps": 0,
                "tab_id": str(b._tab_id) if b._tab_id is not None else None,
                "current_url": b.url,
                "elapsed_seconds": round(time.perf_counter() - started, 1),
                "history": [],
                "confirmations": [],
            }
        if not loop.browser_input_interrupted(error):
            raise
        message = "needs_input: Browser Use was paused or interrupted. The task could not continue within its resume window."
        log.warn(message)
        raw_tab_id = getattr(b, "_tab_id", None)
        return {
            "status": "needs_input",
            "message": message,
            "steps": task_budget,
            "tab_id": str(raw_tab_id) if raw_tab_id is not None else None,
            "current_url": getattr(b, "url", ""),
            "elapsed_seconds": round(time.perf_counter() - started, 1),
            "history": [],
            "confirmations": [],
        }
    finally:
        b.close()
        log.close()
        logger.set_logger(None)


async def run_jev_task(
    distributor_id: str,
    goal: str,
    budget: int = 35,
    client_id: Optional[str] = None,
    tab_id: Optional[str] = None,
    field_hints: Optional[Dict[str, Any]] = None,
    visible_browser: bool = True,
) -> Dict[str, Any]:
    cleaned_goal = (goal or "").strip()
    if not cleaned_goal:
        raise ValueError("Goal is required.")
    async with browser_cursor.activity():
        return await asyncio.to_thread(_run_isolated, cleaned_goal, budget, distributor_id, tab_id, field_hints, visible_browser, client_id, asyncio.get_running_loop())


def _run_isolated(goal: str, budget: int, distributor_id: str, tab_id: Optional[str], field_hints: Optional[Dict[str, Any]], visible_browser: bool, client_id: Optional[str], event_loop: asyncio.AbstractEventLoop) -> Dict[str, Any]:
    with _EXECUTION_LOCK:
        recipe = find_recipe(goal)
        original = dict(recipe.field_hints) if recipe else None
        previous_client = os.environ.get('JEV_BRIDGE_CLIENT_ID')
        logger.set_sensitive_values([str(value) for key, value in (field_hints or {}).items() if 'password' in key and value])
        secrets = {key: value for key, value in (field_hints or {}).items() if key in ("password", "report_password") and isinstance(value, str) and value}
        scope = task_secrets.set(secrets)
        for secret in secrets.values():
            goal = goal.replace(secret, "[saved secret]")
        try:
            if recipe and field_hints:
                recipe.field_hints.update({key: "[saved secret]" if key in secrets and visible_browser else value for key, value in field_hints.items() if isinstance(value, str)})
            if client_id:
                os.environ['JEV_BRIDGE_CLIENT_ID'] = client_id
            if visible_browser:
                return _execute_jev_sync(goal, budget, distributor_id, tab_id, True, client_id, event_loop)
            return _execute_jev_sync(goal, budget, distributor_id, tab_id)
        finally:
            if recipe and original is not None:
                recipe.field_hints.clear()
                recipe.field_hints.update(original)
            if previous_client is None:
                os.environ.pop('JEV_BRIDGE_CLIENT_ID', None)
            else:
                os.environ['JEV_BRIDGE_CLIENT_ID'] = previous_client
            logger.set_sensitive_values([])
            task_secrets.reset(scope)

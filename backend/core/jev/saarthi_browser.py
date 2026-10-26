import asyncio
import time
from typing import Any, Callable, Dict, List, Literal, Optional
from core.browser_use.broker import browser_use_broker
from core.browser_use.cursor import browser_cursor
from core.browser_use.recipient_selection import RecipientSelectionError
from core.browser_use.secret_entry import SecretEntryError, secret_failure_reason, secret_purpose, task_secrets
from core.browser_use.windows_secret_input import fill_focused_secret
from core.jev.browser_bridge import BridgeBrowser, _BridgePage
from core.jev import logger
from core.jev.control import browser_input_interrupted


class SaarthiRecipeBrowser(BridgeBrowser):
    def recipient_status(self, email: str, mode: Literal["exact", "any"] = "exact") -> Dict[str, Any]:
        self._recipient_email = email.strip().lower() if mode == "exact" else None
        self._recipient_mode = mode
        arguments: Dict[str, Any] = {"recipient_mode": mode}
        if self._recipient_email:
            arguments["recipient_email"] = self._recipient_email
        try:
            status = self._dispatch("recipient_status", arguments)
        except Exception as error:
            if browser_input_interrupted(error):
                raise
            raise RecipientSelectionError("Recipient matching is unavailable; update the extension or select the recipient manually.") from None
        logger.get_logger().info(f"Recipient selection: mode={mode} candidates={status.get('recipient_count')} matches={status.get('matches')} checked={status.get('checked')} other_checked={len(status.get('other_checked_nodes', []))}")
        return status

    def _secret_stage(self, stage: str, operation: Callable[[], Any]) -> Any:
        log = logger.get_logger()
        log.info(f"Saved-secret entry: stage={stage} outcome=started")
        try:
            result = operation()
        except Exception as error:
            if browser_input_interrupted(error):
                log.info(f"Saved-secret entry: stage={stage} outcome=interrupted")
                raise
            reason = secret_failure_reason(error)
            log.warn(f"Saved-secret entry stopped: stage={stage} reason={reason}")
            raise SecretEntryError(stage, reason) from None
        log.info(f"Saved-secret entry: stage={stage} outcome=passed")
        return result

    def check_saved_secret_access(self) -> None:
        def check() -> None:
            info = self._dispatch("browser_info", {})
            if "secret_entry_enabled" not in info:
                raise SecretEntryError("preflight", "extension_update_required")
            if info["secret_entry_enabled"] is not True:
                raise SecretEntryError("preflight", "consent_required; enable Allow saved-password entry for requested CAMS/KFintech tasks in the extension popup")
        self._secret_stage("preflight", check)

    def _ensure_secret_allowed(self) -> None:
        browser_cursor.ensure_allowed()
        if not self._client_id:
            raise RuntimeError("No paired browser is selected for saved-password entry.")
        operation = browser_use_broker.ensure_task_input_allowed(self.distributor_id, self._client_id)
        future = asyncio.run_coroutine_threadsafe(operation, self.event_loop)
        try:
            future.result(timeout=5)
        except BaseException:
            future.cancel()
            raise

    def fill_saved_secret(self, index: str) -> str:
        field = self.get_element(index) or {}
        purpose = secret_purpose(self.url, field)
        secret = task_secrets.get().get(purpose or "")
        if not purpose or not secret:
            raise SecretEntryError("resolve", "purpose_or_saved_secret_missing")
        if field.get("value"):
            return "Saved-secret field is already populated"
        target = self._secret_stage("prepare", lambda: self._dispatch("prepare_secret", {"node": field.get("node"), "purpose": purpose, "expected_url": self.url}))
        if target.get("url") != self.url or target.get("purpose") != purpose:
            raise SecretEntryError("prepare", "destination_mismatch")
        self._secret_stage("native_input", lambda: fill_focused_secret(target, secret, self._ensure_secret_allowed))
        verified = self._secret_stage("verify", lambda: self._dispatch("verify_secret", {"node": field.get("node"), "purpose": purpose, "expected_url": self.url}))
        if not verified.get("filled"):
            raise SecretEntryError("verify", "field_empty")
        return "Saved-secret field populated and presence verified"

    def __init__(self, distributor_id: str, client_id: Optional[str], event_loop: asyncio.AbstractEventLoop) -> None:
        self.distributor_id = distributor_id
        self.event_loop = event_loop
        self._timeout = 90.0
        self._visible = True
        self._client_id = client_id
        self._elements: Dict[str, Dict[str, Any]] = {}
        self._last: Dict[str, Any] = {'url': '', 'title': '', 'text': ''}
        self._tab_id: Optional[int] = None
        self._page = _BridgePage(self)
        self.task_history: List[Dict[str, Any]] = []
        self.task_steps = 0
        self.resume_count = 0
        self._recipient_email: Optional[str] = None
        self._recipient_mode: Literal["exact", "any"] = "exact"

    def wait_until_resumed(self) -> bool:
        if self._client_id is None:
            return False
        logger.get_logger().info("Browser task paused; retaining goal, history and workflow context for up to five minutes.")
        operation = browser_use_broker.wait_for_task_resume(self.distributor_id, self._client_id)
        future = asyncio.run_coroutine_threadsafe(operation, self.event_loop)
        try:
            resumed = future.result(timeout=305)
        except BaseException:
            future.cancel()
            raise
        if resumed:
            self.resume_count += 1
            logger.get_logger().info("Browser task resumed; reading the current page before the next decision.")
        return resumed

    async def _dispatch_async(self, action: str, payload: Dict[str, Any], seconds: float) -> Dict[str, Any]:
        if self._client_id is None:
            self._client_id = await browser_use_broker.select_task_client(self.distributor_id, None)
        return await browser_use_broker.dispatch(self.distributor_id, action, payload, timeout_seconds=seconds, client_id=self._client_id)

    def _dispatch(self, action: str, arguments: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        payload = dict(arguments or {})
        if action in ("click", "click_at", "press"):
            if getattr(self, "_recipient_mode", "exact") == "any":
                payload["recipient_mode"] = "any"
            elif getattr(self, "_recipient_email", None):
                payload["recipient_email"] = self._recipient_email
                payload["recipient_mode"] = "exact"
        payload['bridge_protocol'] = 'jev'
        payload['trace_timing'] = True
        if self._tab_id:
            payload.setdefault('tab_id', self._tab_id)
        seconds = timeout or self._timeout
        started = time.perf_counter()
        operation = self._dispatch_async(action, payload, seconds)
        future = asyncio.run_coroutine_threadsafe(operation, self.event_loop)
        try:
            result = future.result(timeout=seconds + 5)
            logger.get_logger().info(f"Browser bridge {action}: {time.perf_counter() - started:.3f}s", bridge_timing=result.get("bridge_timing"))
            return result
        except Exception as error:
            future.cancel()
            if str(error).startswith("Recipient check failed:"):
                raise RecipientSelectionError("Report submission blocked: one eligible recipient must be checked according to the task's selection mode. Recheck the recipient selection.") from None
            raise

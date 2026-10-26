import asyncio
import hashlib
import os
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from uuid import uuid4

from db.local_store.workspace_db import DEFAULT_DB_PATH, connect, initialize_database
from core.browser_use.workflows import BrowserWorkflowStore
from core.browser_use.cursor import browser_cursor
from core.browser_use.command_validation import validate_browser_command


class BrowserUseError(RuntimeError):
    pass


def browser_audit_error_message(error_message: Optional[str]) -> Optional[str]:
    if not error_message:
        return None
    normalized = error_message.casefold()
    categories = [
        (("timed out", "timeout"), "Browser action timed out."),
        (("canceled", "cancelled"), "Browser action canceled."),
        (("backend restarted",), "Backend restarted before the browser action completed."),
        (("revoked",), "Browser connection was revoked."),
        (("reconnected", "paired again"), "Browser connection changed during the action."),
        (("permission", "not allowed", "blocked", "denied"), "Browser policy rejected the action."),
        (("not found", "unavailable", "no longer open"), "Browser target was unavailable."),
    ]
    for markers, category in categories:
        if any(marker in normalized for marker in markers):
            return category
    return "Browser action failed."


@dataclass(frozen=True)
class BrowserJob:
    id: str
    action: str
    arguments: Dict[str, Any]


@dataclass
class BrowserConnection:
    client_id: str
    distributor_id: str
    token: str
    label: str
    browser_family: str
    profile_name: Optional[str]
    connected_at: datetime
    last_seen_at: datetime
    paused: bool = False
    control_generation: int = 0
    queue: asyncio.Queue[BrowserJob] = field(default_factory=asyncio.Queue)
    pending: Dict[str, asyncio.Future[Dict[str, Any]]] = field(default_factory=dict)


@dataclass(frozen=True)
class PairingCode:
    distributor_id: str
    expires_at: datetime


class BrowserUseBroker:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._pairing_codes: Dict[str, PairingCode] = {}
        self._connections_by_token: Dict[str, BrowserConnection] = {}
        self._tokens_by_distributor: Dict[str, Dict[str, str]] = {}
        self._lock = asyncio.Lock()
        self._audit_retention_days = self._configured_audit_retention_days()
        self._workflow_store = BrowserWorkflowStore(db_path)
        self._last_pages: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    @property
    def audit_retention_days(self) -> int:
        return self._audit_retention_days

    async def audit_direct(
        self,
        distributor_id: str,
        action: str,
        arguments: Dict[str, Any],
        execute: Callable[[], Awaitable[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        job_id = str(uuid4())
        self._start_audit(job_id, distributor_id, action, arguments)
        try:
            result = await execute()
        except asyncio.CancelledError:
            self._finish_audit(job_id, "failed", "Browser action canceled.")
            raise
        except Exception as exc:
            self._finish_audit(job_id, "failed", str(exc))
            raise
        self._finish_audit(job_id, "completed", None)
        return result

    async def issue_pairing_code(
        self,
        distributor_id: str,
        lifetime_seconds: int = 600,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        code = secrets.token_urlsafe(12)
        pairing = PairingCode(
            distributor_id=distributor_id,
            expires_at=now + timedelta(seconds=lifetime_seconds),
        )
        async with self._lock:
            self._purge_expired_pairings(now)
            self._pairing_codes[code] = pairing
        return {
            "pairing_code": code,
            "expires_at": pairing.expires_at.isoformat(),
        }

    async def connect(
        self,
        pairing_code: str,
        client_id: Optional[str] = None,
        label: Optional[str] = None,
        browser_family: Optional[str] = None,
        profile_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        async with self._lock:
            self._purge_expired_pairings(now)
            pairing = self._pairing_codes.pop(pairing_code, None)
            if pairing is None:
                raise BrowserUseError("Pairing code is invalid or expired.")
            resolved_client_id = client_id or str(uuid4())
            distributor_tokens = self._tokens_by_distributor.setdefault(
                pairing.distributor_id,
                {},
            )
            previous_token = distributor_tokens.get(resolved_client_id)
            if previous_token:
                previous_connection = self._connections_by_token.pop(
                    previous_token,
                    None,
                )
                if previous_connection:
                    for future in previous_connection.pending.values():
                        if not future.done():
                            future.set_exception(
                                BrowserUseError("Browser profile was paired again.")
                            )
                    previous_connection.pending.clear()
            token = secrets.token_urlsafe(48)
            browser_connection = BrowserConnection(
                client_id=resolved_client_id,
                distributor_id=pairing.distributor_id,
                token=token,
                label=(label or "Browser").strip()[:100] or "Browser",
                browser_family=(browser_family or "chromium").strip()[:30]
                or "chromium",
                profile_name=(profile_name or "").strip()[:100] or None,
                connected_at=now,
                last_seen_at=now,
            )
            self._connections_by_token[token] = browser_connection
            distributor_tokens[resolved_client_id] = token
            self._persist_connection(browser_connection)
            if self._preferred_client_id(pairing.distributor_id) is None:
                self._persist_preferred_client(
                    pairing.distributor_id,
                    resolved_client_id,
                )
        return {
            "bridge_token": token,
            "client_id": resolved_client_id,
            "distributor_id": pairing.distributor_id,
            "browser_family": browser_connection.browser_family,
            "connected_at": now.isoformat(),
        }

    async def connect_client(
        self,
        distributor_id: str,
        client_id: str,
        label: str,
        browser_family: str,
        profile_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        async with self._lock:
            if self._client_is_revoked(distributor_id, client_id):
                raise BrowserUseError("Browser connection was revoked.")
            distributor_tokens = self._tokens_by_distributor.setdefault(
                distributor_id,
                {},
            )
            previous_token = distributor_tokens.get(client_id)
            if previous_token:
                previous_connection = self._connections_by_token.pop(
                    previous_token,
                    None,
                )
                if previous_connection:
                    for future in previous_connection.pending.values():
                        if not future.done():
                            future.set_exception(
                                BrowserUseError("Browser profile reconnected.")
                            )
                    previous_connection.pending.clear()
            token = secrets.token_urlsafe(48)
            connection = BrowserConnection(
                client_id=client_id,
                distributor_id=distributor_id,
                token=token,
                label=label.strip()[:100] or "Browser",
                browser_family=browser_family.strip()[:30] or "chromium",
                profile_name=(profile_name or "").strip()[:100] or None,
                connected_at=now,
                last_seen_at=now,
            )
            self._connections_by_token[token] = connection
            distributor_tokens[client_id] = token
            self._persist_connection(connection)
            if self._preferred_client_id(distributor_id) is None:
                self._persist_preferred_client(distributor_id, client_id)
        return {
            "bridge_token": token,
            "client_id": client_id,
            "distributor_id": distributor_id,
            "browser_family": connection.browser_family,
            "connected_at": now.isoformat(),
        }

    def _client_is_revoked(self, distributor_id: str, client_id: str) -> bool:
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            row = database.execute(
                """
                SELECT status
                FROM browser_use_clients
                WHERE id = ? AND distributor_id = ?
                """,
                (client_id, distributor_id),
            ).fetchone()
        finally:
            database.close()
        return bool(row and row["status"] == "revoked")

    async def status(self, distributor_id: str) -> Dict[str, Any]:
        async with self._lock:
            clients = self._clients_for_distributor(distributor_id)
        last_seen_at = max(
            (client["last_seen_at"] for client in clients),
            default=None,
        )
        if last_seen_at is None:
            return {"connected": False, "last_seen_at": None, "clients": []}
        now = datetime.now(timezone.utc)
        payload_clients = [
            {
                **client,
                "last_seen_at": client["last_seen_at"].isoformat(),
                "connected": not client["paused"] and (now - client["last_seen_at"]).total_seconds() <= 45,
            }
            for client in clients
        ]
        preferred_client_id = self._preferred_client_id(distributor_id)
        for client in payload_clients:
            client["preferred"] = client["client_id"] == preferred_client_id
        return {
            "connected": any(client["connected"] for client in payload_clients),
            "last_seen_at": last_seen_at.isoformat(),
            "clients": payload_clients,
        }

    async def next_job(
        self,
        bridge_token: str,
        wait_seconds: float = 20.0,
    ) -> Optional[Dict[str, Any]]:
        browser_connection = await self._connection_for_token(bridge_token)
        if browser_connection.paused:
            return None
        self._touch(browser_connection)
        generation = browser_connection.control_generation
        deadline = asyncio.get_running_loop().time() + max(
            0.1,
            min(wait_seconds, 25.0),
        )
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                job = await asyncio.wait_for(
                    browser_connection.queue.get(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return None
            if browser_connection.paused or browser_connection.control_generation != generation:
                return None
            if job.id in browser_connection.pending:
                return asdict(job)

    async def set_connection_paused(self, bridge_token: str, paused: bool) -> Dict[str, bool]:
        return await self.connection_control(bridge_token, paused)

    async def submit_result(
        self,
        bridge_token: str,
        job_id: str,
        result: Optional[Dict[str, Any]],
        error: Optional[str],
    ) -> None:
        browser_connection = await self._connection_for_token(bridge_token)
        self._touch(browser_connection)
        future = browser_connection.pending.pop(job_id, None)
        if future is None or future.done():
            return
        if error:
            message = error[:500]
            self._finish_audit(job_id, "failed", message)
            future.set_exception(BrowserUseError(message))
        else:
            self._finish_audit(job_id, "completed", None)
            future.set_result(result or {})

    async def dispatch(
        self,
        distributor_id: str,
        action: str,
        arguments: Dict[str, Any],
        timeout_seconds: float = 90.0,
        client_id: Optional[str] = None,
        audit: bool = True,
    ) -> Dict[str, Any]:
        try:
            validate_browser_command(action, arguments)
            if not browser_cursor.enabled:
                browser_cursor.ensure_allowed()
                return await self._dispatch(distributor_id, action, arguments, timeout_seconds, client_id, audit)
            async with browser_cursor.activity(linger=action != "release_tab"):
                operation = asyncio.create_task(self._dispatch(distributor_id, action, arguments, timeout_seconds, client_id, audit))
                takeover = asyncio.create_task(browser_cursor.wait_for_takeover())
                try:
                    await asyncio.wait({operation, takeover}, return_when=asyncio.FIRST_COMPLETED)
                    browser_cursor.ensure_allowed()
                    return await operation
                finally:
                    operation.cancel()
                    takeover.cancel()
                    await asyncio.gather(operation, takeover, return_exceptions=True)
        except (RuntimeError, ValueError) as exc:
            if isinstance(exc, BrowserUseError):
                raise
            raise BrowserUseError(str(exc)) from exc

    async def distributor_for_token(self, bridge_token: str) -> str:
        connection = await self._connection_for_token(bridge_token)
        self._touch(connection)
        return connection.distributor_id

    async def connection_control(self, token: str, paused: Optional[bool] = None) -> Dict[str, bool]:
        connection = await self._connection_for_token(token)
        self._touch(connection)
        stop_cursor = False
        if paused is not None:
            async with self._lock:
                if paused:
                    stop_cursor = bool(connection.pending)
                if connection.paused != paused:
                    connection.control_generation += 1
                    connection.queue.put_nowait(BrowserJob(id=str(uuid4()), action="__control__", arguments={}))
                connection.paused = paused
                if paused:
                    for job_id, future in connection.pending.items():
                        if not future.done():
                            self._finish_audit(job_id, "failed", "Browser Use was paused by the distributor.")
                            future.set_exception(BrowserUseError("Browser Use was paused by the distributor."))
                    connection.pending.clear()
                initialize_database(self.db_path)
                database = connect(self.db_path)
                try:
                    database.execute(
                        "UPDATE browser_use_clients SET paused = ?, updated_at = ? WHERE id = ? AND token_hash = ?",
                        (int(paused), datetime.now(timezone.utc).isoformat(), connection.client_id, self._token_hash(token)),
                    )
                    database.commit()
                finally:
                    database.close()
            if browser_cursor.enabled and (stop_cursor or not paused):
                await asyncio.to_thread(browser_cursor.set_paused, paused)
        native_paused = browser_cursor.paused.is_set()
        return {"paused": connection.paused or native_paused, "connection_paused": connection.paused, "native_paused": native_paused}

    async def ensure_connection_allowed(self, token: str) -> None:
        connection = await self._connection_for_token(token)
        self._touch(connection)
        if connection.paused:
            raise BrowserUseError("Browser Use is paused. Resume it from the Neo Saarthi extension.")

    async def select_task_client(self, distributor_id: str, client_id: Optional[str]) -> str:
        async with self._lock:
            connection = self._connection_for_distributor(distributor_id, client_id)
            if connection is None:
                raise BrowserUseError("The paired browser is not actively polling. Open the Neo Saarthi extension.")
            return connection.client_id

    async def ensure_task_input_allowed(self, distributor_id: str, client_id: str) -> None:
        async with self._lock:
            token = self._tokens_by_distributor.get(distributor_id, {}).get(client_id)
            connection = self._connections_by_token.get(token) if token else None
            if connection is None or connection.distributor_id != distributor_id:
                raise BrowserUseError("The paired browser connection changed before saved-password entry.")
            if connection.paused:
                raise BrowserUseError("Browser Use is paused. Resume before saved-password entry.")
            if (datetime.now(timezone.utc) - connection.last_seen_at).total_seconds() > 45:
                raise BrowserUseError("The paired browser is no longer actively polling.")
            browser_cursor.ensure_allowed()

    async def wait_for_task_resume(self, distributor_id: str, client_id: str, timeout_seconds: float = 300.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            token = self._tokens_by_distributor.get(distributor_id, {}).get(client_id)
            connection = self._connections_by_token.get(token) if token else None
            if connection is None or connection.distributor_id != distributor_id:
                return False
            if not connection.paused and not browser_cursor.paused.is_set():
                age = (datetime.now(timezone.utc) - connection.last_seen_at).total_seconds()
                return age <= 45
            await asyncio.sleep(0.1)
        return False

    async def _dispatch(
        self,
        distributor_id: str,
        action: str,
        arguments: Dict[str, Any],
        timeout_seconds: float = 90.0,
        client_id: Optional[str] = None,
        audit: bool = True,
    ) -> Dict[str, Any]:
        async with self._lock:
            browser_connection = self._connection_for_distributor(
                distributor_id,
                client_id,
            )
            if browser_connection is None:
                selected_client_id = client_id or self._preferred_client_id(distributor_id)
                if selected_client_id:
                    token = self._tokens_by_distributor.get(distributor_id, {}).get(selected_client_id)
                    candidate = self._connections_by_token.get(token) if token else None
                    if candidate and candidate.paused:
                        raise BrowserUseError("Browser Use is paused by the distributor.")
                raise BrowserUseError(
                    "The paired browser is not actively polling. Open Chrome and the Neo Saarthi extension."
                )
            recording_id = self._workflow_store.begin_action(
                distributor_id, browser_connection.client_id, action
            )
            job_id = str(uuid4())
            future: asyncio.Future[Dict[str, Any]] = (
                asyncio.get_running_loop().create_future()
            )
            browser_connection.pending[job_id] = future
            tab_key = str(arguments.get("tab_id", ""))
            before_page = self._last_pages.get((distributor_id, browser_connection.client_id, tab_key))
            try:
                if audit:
                    self._start_audit(job_id, distributor_id, action, arguments)
                await browser_connection.queue.put(
                    BrowserJob(id=job_id, action=action, arguments=arguments)
                )
            except Exception:
                browser_connection.pending.pop(job_id, None)
                self._workflow_store.finish_action(recording_id, True)
                raise
        result: Optional[Dict[str, Any]] = None
        recording_completed = False
        try:
            result = await asyncio.wait_for(future, timeout=timeout_seconds)
            page = result.get("page")
            result_tab_id = str(result.get("tab_id", tab_key))
            if isinstance(page, dict) and result_tab_id:
                self._last_pages[(distributor_id, browser_connection.client_id, result_tab_id)] = {
                    "url": page.get("url"),
                    "interactive": page.get("interactive", []),
                }
                if len(self._last_pages) > 100:
                    self._last_pages.pop(next(iter(self._last_pages)))
            if recording_id is not None:
                try:
                    self._workflow_store.capture(
                        distributor_id, browser_connection.client_id, action,
                        arguments, before_page, result, recording_id,
                    )
                    recording_completed = True
                except Exception:
                    result["recording_warning"] = "The browser action completed, but the recording could not be updated."
            return result
        except asyncio.CancelledError:
            async with self._lock:
                browser_connection.pending.pop(job_id, None)
            if audit:
                self._finish_audit(job_id, "failed", "Browser action canceled.")
            raise
        except asyncio.TimeoutError as exc:
            async with self._lock:
                browser_connection.pending.pop(job_id, None)
            if audit:
                self._finish_audit(job_id, "timed_out", "Browser action timed out.")
            raise BrowserUseError("The browser did not complete the action in time.") from exc
        except BrowserUseError as exc:
            if audit:
                self._finish_audit(job_id, "failed", str(exc))
            raise
        finally:
            try:
                self._workflow_store.finish_action(recording_id, not recording_completed)
            except Exception:
                if result is not None:
                    result["recording_warning"] = "The browser action completed, but recording status could not be saved."

    async def revoke_connection(
        self,
        distributor_id: str,
        client_id: str,
    ) -> None:
        async with self._lock:
            tokens = self._tokens_by_distributor.get(distributor_id, {})
            token = tokens.pop(client_id, None)
            if token:
                connection = self._connections_by_token.pop(token, None)
                if connection:
                    for future in connection.pending.values():
                        if not future.done():
                            future.set_exception(
                                BrowserUseError("Browser connection was revoked.")
                            )
                    connection.pending.clear()
            if not tokens:
                self._tokens_by_distributor.pop(distributor_id, None)
        initialize_database(self.db_path)
        database = connect(self.db_path)
        now = datetime.now(timezone.utc).isoformat()
        try:
            cursor = database.execute(
                """
                UPDATE browser_use_clients
                SET status = 'revoked', revoked_at = ?, updated_at = ?
                WHERE id = ? AND distributor_id = ? AND status = 'active'
                """,
                (now, now, client_id, distributor_id),
            )
            database.commit()
        finally:
            database.close()
        if cursor.rowcount == 0:
            raise BrowserUseError("Browser connection was not found.")
        if self._preferred_client_id(distributor_id) == client_id:
            self._clear_preferred_client(distributor_id)

    async def select_connection(
        self,
        distributor_id: str,
        client_id: str,
    ) -> None:
        clients = self._clients_for_distributor(distributor_id)
        if not any(client["client_id"] == client_id for client in clients):
            raise BrowserUseError("Browser connection was not found.")
        self._persist_preferred_client(distributor_id, client_id)

    async def reset(self) -> None:
        async with self._lock:
            self._pairing_codes.clear()
            self._connections_by_token.clear()
            self._tokens_by_distributor.clear()
            self._last_pages.clear()
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            database.execute("DELETE FROM browser_use_connections")
            database.execute("DELETE FROM browser_use_clients")
            database.execute("DELETE FROM browser_use_preferences")
            database.execute("DELETE FROM browser_use_audit_events")
            database.execute("DELETE FROM browser_use_workflow_runs")
            database.execute("DELETE FROM browser_use_workflows")
            database.commit()
        finally:
            database.close()

    def audit_events(
        self,
        distributor_id: str,
        limit: Optional[int] = 100,
    ) -> List[Dict[str, Any]]:
        self._purge_old_audits(distributor_id)
        resolved_limit = max(1, min(1000, limit)) if limit is not None else None
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            query = """
                SELECT id, action, status, target_origin, error_message,
                       started_at, finished_at
                FROM browser_use_audit_events
                WHERE distributor_id = ?
                ORDER BY started_at DESC
            """
            parameters: List[Any] = [distributor_id]
            if resolved_limit is not None:
                query += " LIMIT ?"
                parameters.append(resolved_limit)
            rows = database.execute(query, parameters).fetchall()
        finally:
            database.close()
        return [dict(row) for row in rows]

    def delete_audit_events(self, distributor_id: str) -> int:
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            cursor = database.execute(
                "DELETE FROM browser_use_audit_events WHERE distributor_id = ?",
                (distributor_id,),
            )
            database.commit()
            return cursor.rowcount
        finally:
            database.close()

    async def _connection_for_token(self, token: str) -> BrowserConnection:
        async with self._lock:
            browser_connection = self._connections_by_token.get(token)
            if browser_connection is None:
                browser_connection = self._restore_connection(token)
                if browser_connection is not None:
                    self._connections_by_token[token] = browser_connection
                    self._tokens_by_distributor.setdefault(
                        browser_connection.distributor_id,
                        {},
                    )[browser_connection.client_id] = token
        if browser_connection is None:
            raise BrowserUseError("Browser bridge token is invalid or revoked.")
        if datetime.now(timezone.utc) - browser_connection.connected_at >= timedelta(days=30):
            await self.revoke_connection(browser_connection.distributor_id, browser_connection.client_id)
            raise BrowserUseError("Browser bridge token expired. Pair this browser again.")
        return browser_connection

    def _connection_for_distributor(
        self,
        distributor_id: str,
        client_id: Optional[str] = None,
    ) -> Optional[BrowserConnection]:
        tokens = self._tokens_by_distributor.get(distributor_id, {})
        if client_id:
            token = tokens.get(client_id)
            connection = self._connections_by_token.get(token) if token else None
            if connection is None:
                return None
            age = (datetime.now(timezone.utc) - connection.last_seen_at).total_seconds()
            return connection if age <= 45 and not connection.paused and datetime.now(timezone.utc) - connection.connected_at < timedelta(days=30) else None
        connections = [
            self._connections_by_token[token]
            for token in tokens.values()
            if token in self._connections_by_token
        ]
        now = datetime.now(timezone.utc)
        preferred_client_id = self._preferred_client_id(distributor_id)
        if preferred_client_id:
            preferred_connection = next((connection for connection in connections if connection.client_id == preferred_client_id), None)
            if preferred_connection and preferred_connection.paused:
                return None
            if preferred_connection is None and any(client["client_id"] == preferred_client_id and client["paused"] for client in self._clients_for_distributor(distributor_id)):
                return None
        connections = [
            connection
            for connection in connections
            if not connection.paused and (now - connection.last_seen_at).total_seconds() <= 45 and now - connection.connected_at < timedelta(days=30)
        ]
        if preferred_client_id:
            preferred = next(
                (
                    connection
                    for connection in connections
                    if connection.client_id == preferred_client_id
                ),
                None,
            )
            if preferred is not None:
                return preferred
        return max(connections, key=lambda item: item.last_seen_at, default=None)

    def _persist_connection(self, browser_connection: BrowserConnection) -> None:
        initialize_database(self.db_path)
        database = connect(self.db_path)
        now = browser_connection.connected_at.isoformat()
        try:
            database.execute(
                """
                INSERT INTO browser_use_clients (
                    id, distributor_id, token_hash, label, browser_family,
                    profile_name, status, paused, connected_at,
                    last_seen_at, revoked_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL, ?)
                ON CONFLICT(id) DO UPDATE SET
                    token_hash = excluded.token_hash,
                    label = excluded.label,
                    browser_family = excluded.browser_family,
                    profile_name = excluded.profile_name,
                    status = 'active',
                    paused = excluded.paused,
                    connected_at = excluded.connected_at,
                    last_seen_at = excluded.last_seen_at,
                    revoked_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    browser_connection.client_id,
                    browser_connection.distributor_id,
                    self._token_hash(browser_connection.token),
                    browser_connection.label,
                    browser_connection.browser_family,
                    browser_connection.profile_name,
                    int(browser_connection.paused),
                    now,
                    now,
                    now,
                ),
            )
            database.commit()
        finally:
            database.close()

    def _preferred_client_id(self, distributor_id: str) -> Optional[str]:
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            row = database.execute(
                """
                SELECT preferred_client_id
                FROM browser_use_preferences
                WHERE distributor_id = ?
                """,
                (distributor_id,),
            ).fetchone()
        finally:
            database.close()
        return str(row["preferred_client_id"]) if row else None

    def _persist_preferred_client(
        self,
        distributor_id: str,
        client_id: str,
    ) -> None:
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            database.execute(
                """
                INSERT INTO browser_use_preferences (
                    distributor_id, preferred_client_id, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(distributor_id) DO UPDATE SET
                    preferred_client_id = excluded.preferred_client_id,
                    updated_at = excluded.updated_at
                """,
                (
                    distributor_id,
                    client_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            database.commit()
        finally:
            database.close()

    def _clear_preferred_client(self, distributor_id: str) -> None:
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            database.execute(
                "DELETE FROM browser_use_preferences WHERE distributor_id = ?",
                (distributor_id,),
            )
            database.commit()
        finally:
            database.close()

    def _restore_connection(self, token: str) -> Optional[BrowserConnection]:
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            row = database.execute(
                """
                SELECT id, distributor_id, label, browser_family, profile_name,
                       connected_at, last_seen_at, paused
                FROM browser_use_clients
                WHERE token_hash = ? AND status = 'active'
                """,
                (self._token_hash(token),),
            ).fetchone()
            if row is None:
                legacy = database.execute(
                    """
                    SELECT distributor_id, connected_at, last_seen_at
                    FROM browser_use_connections
                    WHERE token_hash = ? AND status = 'active'
                    """,
                    (self._token_hash(token),),
                ).fetchone()
                if legacy is not None:
                    row = {
                        "id": f"legacy-{self._token_hash(token)[:12]}",
                        "distributor_id": legacy["distributor_id"],
                        "label": "Chrome",
                        "browser_family": "chrome",
                        "profile_name": None,
                        "connected_at": legacy["connected_at"],
                        "last_seen_at": legacy["last_seen_at"],
                        "paused": 0,
                    }
        finally:
            database.close()
        if row is None:
            return None
        browser_connection = BrowserConnection(
            client_id=str(row["id"]),
            distributor_id=str(row["distributor_id"]),
            token=token,
            label=str(row["label"]),
            browser_family=str(row["browser_family"]),
            profile_name=(
                str(row["profile_name"])
                if row["profile_name"] is not None
                else None
            ),
            connected_at=self._parse_datetime(str(row["connected_at"])),
            last_seen_at=self._parse_datetime(str(row["last_seen_at"])),
            paused=bool(row["paused"]),
        )
        self._persist_connection(browser_connection)
        if self._preferred_client_id(browser_connection.distributor_id) is None:
            self._persist_preferred_client(
                browser_connection.distributor_id,
                browser_connection.client_id,
            )
        return browser_connection

    def _clients_for_distributor(self, distributor_id: str) -> List[Dict[str, Any]]:
        active_connections = {
            connection.client_id: connection
            for connection in self._connections_by_token.values()
            if connection.distributor_id == distributor_id
        }
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            rows = database.execute(
                """
                SELECT id, label, browser_family, profile_name, last_seen_at, paused
                FROM browser_use_clients
                WHERE distributor_id = ? AND status = 'active'
                ORDER BY last_seen_at DESC
                """,
                (distributor_id,),
            ).fetchall()
        finally:
            database.close()
        clients: Dict[str, Dict[str, Any]] = {
            str(row["id"]): {
                "client_id": str(row["id"]),
                "label": str(row["label"]),
                "browser_family": str(row["browser_family"]),
                "profile_name": (
                    str(row["profile_name"])
                    if row["profile_name"] is not None
                    else None
                ),
                "last_seen_at": self._parse_datetime(str(row["last_seen_at"])),
                "paused": bool(row["paused"]),
            }
            for row in rows
        }
        for connection in active_connections.values():
            clients[connection.client_id] = {
                "client_id": connection.client_id,
                "label": connection.label,
                "browser_family": connection.browser_family,
                "profile_name": connection.profile_name,
                "last_seen_at": connection.last_seen_at,
                "paused": connection.paused,
            }
        legacy_last_seen = self._persisted_legacy_last_seen(distributor_id)
        if not clients and legacy_last_seen is not None:
            clients["legacy"] = {
                "client_id": "legacy",
                "label": "Chrome",
                "browser_family": "chrome",
                "profile_name": None,
                "last_seen_at": legacy_last_seen,
                "paused": False,
            }
        return sorted(
            clients.values(),
            key=lambda item: item["last_seen_at"],
            reverse=True,
        )

    def _persisted_legacy_last_seen(
        self,
        distributor_id: str,
    ) -> Optional[datetime]:
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            row = database.execute(
                """
                SELECT last_seen_at
                FROM browser_use_connections
                WHERE distributor_id = ? AND status = 'active'
                """,
                (distributor_id,),
            ).fetchone()
        finally:
            database.close()
        return self._parse_datetime(str(row["last_seen_at"])) if row else None

    def _touch(self, browser_connection: BrowserConnection) -> None:
        now = datetime.now(timezone.utc)
        browser_connection.last_seen_at = now
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            database.execute(
                """
                UPDATE browser_use_clients
                SET last_seen_at = ?, updated_at = ?
                WHERE id = ? AND distributor_id = ? AND token_hash = ? AND status = 'active'
                """,
                (
                    now.isoformat(),
                    now.isoformat(),
                    browser_connection.client_id,
                    browser_connection.distributor_id,
                    self._token_hash(browser_connection.token),
                ),
            )
            database.commit()
        finally:
            database.close()

    def _start_audit(
        self,
        job_id: str,
        distributor_id: str,
        action: str,
        arguments: Dict[str, Any],
    ) -> None:
        self._purge_old_audits(distributor_id)
        initialize_database(self.db_path)
        database = connect(self.db_path)
        now = datetime.now(timezone.utc).isoformat()
        target_origin = self._target_origin(action, arguments)
        try:
            database.execute(
                """
                INSERT INTO browser_use_audit_events (
                    id, distributor_id, action, status, target_origin,
                    error_message, started_at, finished_at
                ) VALUES (?, ?, ?, 'pending', ?, NULL, ?, NULL)
                """,
                (job_id, distributor_id, action, target_origin, now),
            )
            database.commit()
        finally:
            database.close()

    def _finish_audit(
        self,
        job_id: str,
        status: str,
        error_message: Optional[str],
    ) -> None:
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            database.execute(
                """
                UPDATE browser_use_audit_events
                SET status = ?, error_message = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    browser_audit_error_message(error_message),
                    datetime.now(timezone.utc).isoformat(),
                    job_id,
                ),
            )
            database.commit()
        finally:
            database.close()

    def recover_interrupted_audits(self) -> int:
        initialize_database(self.db_path)
        database = connect(self.db_path)
        now = datetime.now(timezone.utc).isoformat()
        try:
            cursor = database.execute(
                """
                UPDATE browser_use_audit_events
                SET status = 'failed',
                    error_message = 'Backend restarted before the browser action completed.',
                    finished_at = ?
                WHERE status = 'pending'
                """,
                (now,),
            )
            database.commit()
            return cursor.rowcount
        finally:
            database.close()

    def _purge_expired_pairings(self, now: datetime) -> None:
        expired = [
            code
            for code, pairing in self._pairing_codes.items()
            if pairing.expires_at <= now
        ]
        for code in expired:
            self._pairing_codes.pop(code, None)

    def _configured_audit_retention_days(self) -> int:
        raw = os.getenv("NEO_SAARTHI_BROWSER_AUDIT_RETENTION_DAYS", "30")
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("Browser audit retention must be an integer.") from exc
        if value < 1 or value > 365:
            raise ValueError("Browser audit retention must be between 1 and 365 days.")
        return value

    def _purge_old_audits(self, distributor_id: str) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._audit_retention_days)
        initialize_database(self.db_path)
        database = connect(self.db_path)
        try:
            cursor = database.execute(
                """
                DELETE FROM browser_use_audit_events
                WHERE distributor_id = ? AND started_at < ?
                """,
                (distributor_id, cutoff.isoformat()),
            )
            database.commit()
            return cursor.rowcount
        finally:
            database.close()

    def _token_hash(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _parse_datetime(self, value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _target_origin(self, action: str, arguments: Dict[str, Any]) -> Optional[str]:
        if action == "google_ai_mode":
            return "https://www.google.com"
        raw_url = arguments.get("url") or arguments.get("origin")
        if not isinstance(raw_url, str):
            return None
        parsed = urlparse(raw_url)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None


browser_use_broker = BrowserUseBroker()

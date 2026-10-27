import asyncio
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Literal, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from companion import runtime
from companion.native_bridge import TOKEN as NATIVE_TOKEN
from api.routers.browser_use import router
from core.browser_use.broker import BrowserJob, BrowserUseError, browser_use_broker
from core.browser_use.cursor import browser_cursor
from core.jev.logger import redact_sensitive
from db.local_store.workspace_db import connect, initialize_database
from services.jev_service import run_jev_task
from schemas.browser_use import BrowserDesktopActionInput


UI = runtime.ROOT / "ui"
EXTENSION_ID = "polchmmnbnamnpkoiipldgdngmdllgbc"
BRIDGE_PATHS = {f"/api/v1/browser-use/bridge/{suffix}" for suffix in ("connect", "auto-connect", "control", "cursor/check", "cursor/move", "next", "result")}
TASKS: Dict[str, Dict[str, Any]] = {}
WORKERS: Dict[str, asyncio.Task[None]] = {}
PAIRING_ATTEMPTS: List[float] = []


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    initialize_database()
    application.state.ready = True
    yield
    browser_cursor.close()


app = FastAPI(title="Neo Saarthi Browser Companion", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def local_access(request: Request, call_next: Any) -> Response:
    port = request.url.port
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    path = request.url.path
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        return JSONResponse({"detail": "Local connections only."}, status_code=403)
    if host not in {"127.0.0.1:8000", "127.0.0.1:41691"}:
        return JSONResponse({"detail": "Invalid host."}, status_code=403)
    bridge = path in BRIDGE_PATHS
    headers: Dict[str, str] = {}
    if port == 41691:
        if not bridge or origin != f"chrome-extension://{EXTENSION_ID}":
            return JSONResponse({"detail": "This port accepts the configured extension only."}, status_code=403)
        headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        if request.method == "OPTIONS":
            return Response(status_code=204, headers={**headers, "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST, OPTIONS", "Cache-Control": "no-store"})
        if request.method != "POST" or not request.headers.get("content-type", "").lower().startswith("application/json"):
            return JSONResponse({"detail": "JSON POST required."}, status_code=415, headers=headers)
        if path.endswith("/auto-connect"):
            return JSONResponse({"detail": "Pair using a one-time code."}, status_code=403, headers=headers)
        if path.endswith("/connect"):
            now = time.monotonic()
            PAIRING_ATTEMPTS[:] = [attempt for attempt in PAIRING_ATTEMPTS if now - attempt < 60]
            if len(PAIRING_ATTEMPTS) >= 15:
                return JSONResponse({"detail": "Too many pairing attempts. Wait one minute."}, status_code=429, headers=headers)
            PAIRING_ATTEMPTS.append(now)
    else:
        if bridge or (origin and origin != "http://127.0.0.1:8000") or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Same-device UI access required."}, status_code=403)
        if path == "/api/companion/native":
            if origin or not secrets.compare_digest(request.headers.get("x-neo-native", ""), NATIVE_TOKEN):
                return JSONResponse({"detail": "Native host authentication required."}, status_code=403)
        elif path.startswith("/api/") and path != "/api/companion/session":
            if not secrets.compare_digest(request.headers.get("x-companion-session", ""), runtime.SESSION):
                return JSONResponse({"detail": "Open the local companion page first."}, status_code=401)
    if request.method in {"POST", "PUT", "PATCH"}:
        maximum = 12 * 1024 * 1024 if bridge else 64000
        parts: List[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > maximum:
                return JSONResponse({"detail": "Request is too large."}, status_code=413, headers=headers)
            parts.append(chunk)
        request._body = b"".join(parts)
    response = await call_next(request)
    for name, value in headers.items():
        response.headers[name] = value
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


app.include_router(router)
app.mount("/static", StaticFiles(directory=UI), name="static")


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(UI / "index.html")


@app.get("/api/companion/session")
async def session() -> Dict[str, str]:
    return {"session": runtime.SESSION}


def scrub(value: Any) -> Any:
    text = json.dumps(value, default=str)
    values = {**runtime.read_settings(), "environment_api_key": os.environ.get("OPENCODE_API_KEY", "")}
    for key, secret in values.items():
        if secret and key in {"karvy_password", "report_password", "opencode_api_key", "environment_api_key"}:
            text = text.replace(json.dumps(secret)[1:-1], "[redacted]")
    return json.loads(redact_sensitive(text))


@app.get("/api/companion/status")
async def status() -> Dict[str, Any]:
    return {"browser": await browser_use_broker.status(runtime.OWNER), "cursor": {**browser_cursor.status(), "native_paused": browser_cursor.paused.is_set()}, "settings": runtime.settings_status(), "tasks": scrub(list(TASKS.values())), "models": {"decisions": os.environ.get("JEV_MODEL", "jev-1.13"), "text_and_vision": "deepseek-v4.1-flash"}}


class SettingsInput(BaseModel):
    values: Dict[str, str]
    password_entry_allowed: Optional[bool] = None


@app.put("/api/companion/settings")
async def settings(body: SettingsInput) -> Dict[str, Any]:
    active = any(not worker.done() for worker in WORKERS.values())
    if active and (body.values or body.password_entry_allowed is not False):
        raise HTTPException(409, "Wait until the task finishes before changing credentials.")
    try:
        if set(body.values) & {"saved_password_entry", "browser_auto_connect"}:
            raise ValueError("Use the explicit consent and connection controls.")
        updates = dict(body.values)
        if body.password_entry_allowed is not None:
            if active and not body.password_entry_allowed:
                await asyncio.to_thread(browser_cursor.set_paused, True)
            updates["saved_password_entry"] = "enabled" if body.password_entry_allowed else "disabled"
        return runtime.save_settings(updates)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


class NativeInput(BaseModel):
    v: Literal[1]
    op: Literal["connect", "status"]
    nonce: str = Field(pattern=r"^[a-f0-9-]{36}$")
    client_id: Optional[str] = Field(default=None, pattern=r"^[a-f0-9-]{36}$")
    browser_family: Optional[Literal["chrome", "edge", "chromium", "brave", "opera", "vivaldi"]] = None

    model_config = {"extra": "forbid"}


@app.post("/api/companion/native")
async def native_connection(body: NativeInput) -> Dict[str, Any]:
    settings = runtime.settings_status()
    response: Dict[str, Any] = {"nonce": body.nonce, "secret_entry_enabled": settings["password_entry_enabled"]}
    if body.op == "connect":
        if not settings["auto_connect_enabled"]:
            raise HTTPException(403, "Automatic connection disabled.")
        if not body.client_id or not body.browser_family:
            raise HTTPException(400, "Browser identity required.")
        try:
            response["connection"] = await browser_use_broker.connect_client(runtime.OWNER, body.client_id, body.browser_family.capitalize(), body.browser_family)
        except BrowserUseError:
            raise HTTPException(403, "Browser connection revoked.") from None
    return response


class ConnectionInput(BaseModel):
    enabled: bool


@app.post("/api/companion/connection")
async def automatic_connection(body: ConnectionInput) -> Dict[str, Any]:
    if WORKERS:
        raise HTTPException(409, "Wait until the active task finishes before disconnecting browsers.")
    runtime.save_settings({"browser_auto_connect": "enabled" if body.enabled else "disabled"})
    if not body.enabled:
        for client_id in list(browser_use_broker._tokens_by_distributor.get(runtime.OWNER, {})):
            await browser_use_broker.revoke_connection(runtime.OWNER, client_id)
    else:
        database = connect()
        try:
            database.execute("UPDATE browser_use_clients SET status = 'active', last_seen_at = '1970-01-01T00:00:00+00:00' WHERE distributor_id = ? AND status = 'revoked'", (runtime.OWNER,))
            database.commit()
        finally:
            database.close()
    return runtime.settings_status()


class TaskInput(BaseModel):
    goal: str = Field(min_length=1, max_length=16000)
    budget: int = Field(default=35, ge=1, le=200)
    browser_id: Optional[str] = None
    credential_profile: Literal["none", "cams", "karvy"] = "none"


def task_hints(profile: str) -> Dict[str, Any]:
    settings = runtime.read_settings()
    if profile == "cams":
        return {"email": settings.get("cams_email", ""), "report_password": settings.get("report_password", "")}
    if profile == "karvy":
        return {"username": settings.get("karvy_username", ""), "password": settings.get("karvy_password", ""), "report_password": settings.get("report_password", ""), "recipient_mode": "any"}
    return {}


async def execute(task_id: str, body: TaskInput, client_id: str) -> None:
    item = TASKS[task_id]
    try:
        result = await run_jev_task(runtime.OWNER, body.goal, body.budget, client_id, field_hints=task_hints(body.credential_profile))
        item.update({"status": result["status"], "result": scrub(result)})
    except Exception as error:
        item.update({"status": "failed", "error": scrub(str(error))})
    finally:
        item["finished_at"] = time.time()
        WORKERS.pop(task_id, None)


@app.post("/api/companion/tasks", status_code=202)
async def start_task(body: TaskInput) -> Dict[str, str]:
    if WORKERS:
        raise HTTPException(409, "A task is already running. Resume it or wait until it finishes.")
    if browser_cursor.paused.is_set():
        raise HTTPException(409, "Resume browser input before starting a task.")
    if body.credential_profile != "none" and not runtime.settings_status()["password_entry_enabled"]:
        raise HTTPException(409, "Approve saved-password entry in Settings before using this credential profile.")
    if not runtime.settings_status()["model_key_available"]:
        raise HTTPException(409, "Add your OpenCode API key in Settings first.")
    try:
        client_id = await browser_use_broker.select_task_client(runtime.OWNER, body.browser_id)
    except BrowserUseError as error:
        raise HTTPException(409, str(error)) from None
    task_id = str(uuid4())
    TASKS[task_id] = {"id": task_id, "goal": scrub(body.goal), "status": "running", "client_id": client_id, "started_at": time.time()}
    while len(TASKS) > 20:
        TASKS.pop(next(iter(TASKS)))
    WORKERS[task_id] = asyncio.create_task(execute(task_id, body, client_id))
    return {"id": task_id, "status": "running"}


class ControlInput(BaseModel):
    paused: bool
    client_id: str


@app.post("/api/companion/control")
async def control(body: ControlInput) -> Dict[str, bool]:
    token = browser_use_broker._tokens_by_distributor.get(runtime.OWNER, {}).get(body.client_id)
    if not token:
        raise HTTPException(409, "Browser is disconnected. Enable automatic connection and keep the extension enabled.")
    connection = await browser_use_broker._connection_for_token(token)
    if connection.paused and not body.paused:
        await browser_use_broker.connection_control(token, False)
    try:
        await asyncio.to_thread(browser_cursor.set_paused, body.paused)
        if body.paused:
            async with browser_use_broker._lock:
                connection.control_generation += 1
                for job_id, future in connection.pending.items():
                    if not future.done():
                        browser_use_broker._finish_audit(job_id, "failed", "Browser Use was paused by the distributor.")
                        future.set_exception(BrowserUseError("Browser Use was paused by the distributor."))
                connection.pending.clear()
                connection.queue.put_nowait(BrowserJob(id=str(uuid4()), action="__control__", arguments={}))
        return {"paused": browser_cursor.paused.is_set(), "connection_paused": False, "native_paused": browser_cursor.paused.is_set()}
    except (BrowserUseError, RuntimeError) as error:
        raise HTTPException(409, str(error)) from None


@app.get("/api/companion/logs")
async def logs() -> Dict[str, List[str]]:
    directory = runtime.DATA / "logs/browser_use"
    files = sorted(directory.glob("*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        return {"lines": []}
    with files[0].open("rb") as stream:
        stream.seek(max(0, files[0].stat().st_size - 48000))
        text = stream.read().decode("utf-8", errors="replace")
    return {"lines": scrub(text.splitlines()[-150:])}


@app.post("/api/companion/action")
async def browser_action(body: BrowserDesktopActionInput) -> Dict[str, Any]:
    try:
        return await browser_use_broker.dispatch(runtime.OWNER, body.action, body.arguments, timeout_seconds=30, client_id=body.client_id)
    except BrowserUseError as error:
        raise HTTPException(409, str(error)) from None

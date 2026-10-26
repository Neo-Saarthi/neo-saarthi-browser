import asyncio
import json
import os
import secrets
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import Field

from api.dependencies import LocalDistributor, get_current_distributor, _dev_auth_bypass_enabled, _dev_distributor
from core.browser_use.broker import BrowserUseError, browser_use_broker
from core.browser_use.cursor import browser_cursor
from core.browser_use.secret_transport import open_local_task
from core.browser_use.workflows import BrowserWorkflowError, browser_workflow_store
from core.browser_use.managed import managed_browser
from db.local_store.workspace_db import (
    DEFAULT_DB_PATH,
    connect,
    initialize_database,
    local_owner_distributor_id,
)
from services.jev_service import run_jev_task as run_browser_task
from schemas.browser_use import (
    BrowserRunInput,
    BrowserRunResponse,
    BrowserAuditDeleteResponse,
    BrowserAuditListResponse,
    BrowserBridgeConnectInput,
    BrowserBridgeConnectResponse,
    BrowserBridgeControlInput,
    BrowserBridgePollInput,
    BrowserBridgePollResponse,
    BrowserBridgeResultInput,
    BrowserBridgeStatusResponse,
    BrowserConnectionRevokeInput,
    BrowserAutoConnectInput,
    BrowserConnectionSelectInput,
    BrowserDiagnosticActionInput,
    BrowserDiagnosticActionResponse,
    BrowserDesktopActionInput,
    BrowserPairingResponse,
    BrowserWorkflowTakeoverInput,
)

router = APIRouter(prefix="/api/v1/browser-use", tags=["Browser Use"])
_browser_health_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}


def _require_desktop_pairing_authorized(request: Request, token: str) -> None:
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Desktop browser pairing is available only on this device.",
        )
    expected = os.getenv("NEO_SAARTHI_DESKTOP_AUTH_TOKEN") or os.getenv("MF_SAARTHI_DESKTOP_AUTH_TOKEN", "")
    token = token or request.headers.get("X-MF-Saarthi-Desktop-Token", "")
    if not expected or not token or not secrets.compare_digest(expected, token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Desktop setup authorization is required.",
        )


def _local_owner() -> str:
    initialize_database(DEFAULT_DB_PATH)
    database = connect(DEFAULT_DB_PATH)
    try:
        distributor_id = _browser_owner_distributor_id(database)
    finally:
        database.close()
    if distributor_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create the local business profile first.",
        )
    return distributor_id


def _audit_payload(
    distributor_id: str,
    limit: Optional[int],
) -> BrowserAuditListResponse:
    return BrowserAuditListResponse(
        retention_days=browser_use_broker.audit_retention_days,
        events=browser_use_broker.audit_events(distributor_id, limit),
    )


def _audit_export_response(distributor_id: str) -> Response:
    payload = _audit_payload(distributor_id, None)
    return Response(
        content=json.dumps(payload.model_dump(mode="json"), indent=2),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=neo-saarthi-browser-audit.json"},
    )


@router.get("/audit", response_model=BrowserAuditListResponse)
async def browser_audit_events(
    limit: int = Query(default=100, ge=1, le=1000),
    distributor: LocalDistributor = Depends(get_current_distributor),
) -> BrowserAuditListResponse:
    return _audit_payload(distributor.distributor_id, limit)


@router.get("/audit/export", response_class=Response)
async def export_browser_audit_events(
    distributor: LocalDistributor = Depends(get_current_distributor),
) -> Response:
    return _audit_export_response(distributor.distributor_id)


@router.delete("/audit", response_model=BrowserAuditDeleteResponse)
async def delete_browser_audit_events(
    distributor: LocalDistributor = Depends(get_current_distributor),
) -> BrowserAuditDeleteResponse:
    return BrowserAuditDeleteResponse(
        deleted=browser_use_broker.delete_audit_events(distributor.distributor_id)
    )




@router.post("/pairing", response_model=BrowserPairingResponse, include_in_schema=False)
async def create_browser_pairing(
    distributor: LocalDistributor = Depends(get_current_distributor),
) -> BrowserPairingResponse:
    payload = await browser_use_broker.issue_pairing_code(distributor.distributor_id)
    return BrowserPairingResponse(**payload)


@router.post("/pairing/desktop", response_model=BrowserPairingResponse, include_in_schema=False)
async def create_desktop_browser_pairing(
    request: Request,
    desktop_token: str = Header(default="", alias="X-Neo-Saarthi-Desktop-Token"),
) -> BrowserPairingResponse:
    _require_desktop_pairing_authorized(request, desktop_token)
    initialize_database(DEFAULT_DB_PATH)
    database = connect(DEFAULT_DB_PATH)
    try:
        distributor_id = _browser_owner_distributor_id(database)
    finally:
        database.close()
    if distributor_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create the local business profile before pairing a browser.",
        )
    payload = await browser_use_broker.issue_pairing_code(distributor_id)
    return BrowserPairingResponse(**payload)


@router.post("/bridge/auto-connect", response_model=BrowserBridgeConnectResponse)
async def auto_connect_browser(
    body: BrowserAutoConnectInput,
    request: Request,
    desktop_token: str = Header(default="", alias="X-Neo-Saarthi-Desktop-Token"),
) -> BrowserBridgeConnectResponse:
    if os.getenv("APP_ENV", "development") != "development":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Production browser pairing requires a one-time code.")
    _require_desktop_pairing_authorized(request, desktop_token)
    initialize_database(DEFAULT_DB_PATH)
    database = connect(DEFAULT_DB_PATH)
    try:
        distributor_id = _browser_owner_distributor_id(database)
    finally:
        database.close()
    if distributor_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Open Neo Saarthi and create the local business profile first.",
        )
    try:
        payload = await browser_use_broker.connect_client(
            distributor_id=distributor_id,
            client_id=body.client_id,
            label=body.label,
            browser_family=body.browser_family,
            profile_name=body.profile_name,
        )
    except BrowserUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    return BrowserBridgeConnectResponse(**payload)


@router.get("/status/desktop", response_model=BrowserBridgeStatusResponse)
async def desktop_browser_bridge_status(
    request: Request,
    desktop_token: str = Header(default="", alias="X-Neo-Saarthi-Desktop-Token"),
) -> BrowserBridgeStatusResponse:
    _require_desktop_pairing_authorized(request, desktop_token)
    initialize_database(DEFAULT_DB_PATH)
    database = connect(DEFAULT_DB_PATH)
    try:
        distributor_id = _browser_owner_distributor_id(database)
    finally:
        database.close()
    if distributor_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create the local business profile before viewing browsers.",
        )
    payload = await browser_use_broker.status(distributor_id)
    return BrowserBridgeStatusResponse(**payload)


@router.get("/health/desktop")
async def desktop_browser_health(
    request: Request,
    fresh: bool = Query(default=False),
    desktop_token: str = Header(default="", alias="X-Neo-Saarthi-Desktop-Token"),
) -> Dict[str, Any]:
    _require_desktop_pairing_authorized(request, desktop_token)
    distributor_id = _local_owner()
    connections = await browser_use_broker.status(distributor_id)
    runs = [run for run in browser_workflow_store.list_runs(distributor_id) if run["status"] in {"active", "pending", "interrupted"}]
    selected = next((client for client in connections["clients"] if client["connected"] and client["preferred"]), None)
    capabilities: Dict[str, Any] = {}
    if selected:
        cache_key = f"{distributor_id}:{selected['client_id']}"
        cached = _browser_health_cache.get(cache_key)
        if cached and not fresh and time.monotonic() - cached[0] < 60:
            capabilities = cached[1]
        else:
            try:
                capabilities = {
                    "info": await browser_use_broker.dispatch(distributor_id, "browser_info", {}, timeout_seconds=5.0, client_id=selected["client_id"], audit=False),
                    "permissions": await browser_use_broker.dispatch(distributor_id, "get_permissions", {}, timeout_seconds=5.0, client_id=selected["client_id"], audit=False),
                }
                _browser_health_cache[cache_key] = (time.monotonic(), capabilities)
            except BrowserUseError:
                capabilities = {"status": "unavailable"}
    return {"connections": connections, "capabilities": capabilities, "clean_browser": {"running": managed_browser.running, "public_https_only": True}, "workflows": browser_workflow_store.list_workflows(distributor_id), "runs": runs}


@router.post("/workflows/takeover/desktop")
async def desktop_browser_workflow_takeover(
    body: BrowserWorkflowTakeoverInput,
    request: Request,
    desktop_token: str = Header(default="", alias="X-Neo-Saarthi-Desktop-Token"),
) -> Dict[str, Any]:
    _require_desktop_pairing_authorized(request, desktop_token)
    distributor_id = _local_owner()
    try:
        run = browser_workflow_store.begin_takeover(distributor_id, body.run_id)
        try:
            await browser_use_broker.dispatch(distributor_id, "release_tab", {"tab_id": run["tab_id"]}, client_id=run["client_id"])
        except asyncio.CancelledError:
            browser_workflow_store.finish_takeover(distributor_id, body.run_id, False)
            raise
        except Exception:
            browser_workflow_store.finish_takeover(distributor_id, body.run_id, False)
            raise
        return browser_workflow_store.finish_takeover(distributor_id, body.run_id, True)
    except (BrowserUseError, BrowserWorkflowError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/desktop/action",
    response_model=BrowserDiagnosticActionResponse,
)
async def run_desktop_browser_action(
    body: BrowserDesktopActionInput,
    request: Request,
    desktop_token: str = Header(default="", alias="X-Neo-Saarthi-Desktop-Token"),
) -> BrowserDiagnosticActionResponse:
    _require_desktop_pairing_authorized(request, desktop_token)
    initialize_database(DEFAULT_DB_PATH)
    database = connect(DEFAULT_DB_PATH)
    try:
        distributor_id = _browser_owner_distributor_id(database)
    finally:
        database.close()
    if distributor_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create the local business profile before managing browsers.",
        )
    try:
        result = await browser_use_broker.dispatch(
            distributor_id,
            body.action,
            body.arguments,
            client_id=body.client_id,
        )
    except BrowserUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return BrowserDiagnosticActionResponse(result=result)


@router.post("/connections/revoke/desktop", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_desktop_browser_connection(
    body: BrowserConnectionRevokeInput,
    request: Request,
    desktop_token: str = Header(default="", alias="X-Neo-Saarthi-Desktop-Token"),
) -> None:
    _require_desktop_pairing_authorized(request, desktop_token)
    initialize_database(DEFAULT_DB_PATH)
    database = connect(DEFAULT_DB_PATH)
    try:
        distributor_id = _browser_owner_distributor_id(database)
    finally:
        database.close()
    if distributor_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create the local business profile before managing browsers.",
        )
    try:
        await browser_use_broker.revoke_connection(distributor_id, body.client_id)
    except BrowserUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/connections/preferred/desktop", status_code=status.HTTP_204_NO_CONTENT)
async def select_desktop_browser_connection(
    body: BrowserConnectionSelectInput,
    request: Request,
    desktop_token: str = Header(default="", alias="X-Neo-Saarthi-Desktop-Token"),
) -> None:
    _require_desktop_pairing_authorized(request, desktop_token)
    initialize_database(DEFAULT_DB_PATH)
    database = connect(DEFAULT_DB_PATH)
    try:
        distributor_id = _browser_owner_distributor_id(database)
    finally:
        database.close()
    if distributor_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create the local business profile before selecting a browser.",
        )
    try:
        await browser_use_broker.select_connection(distributor_id, body.client_id)
    except BrowserUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post(
    "/diagnostics/action",
    response_model=BrowserDiagnosticActionResponse,
    include_in_schema=False,
)
async def run_browser_diagnostic_action(
    body: BrowserDiagnosticActionInput,
    request: Request,
    desktop_token: str = Header(default="", alias="X-Neo-Saarthi-Desktop-Token"),
) -> BrowserDiagnosticActionResponse:
    if os.getenv("APP_ENV", "development") != "development":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _require_desktop_pairing_authorized(request, desktop_token)
    initialize_database(DEFAULT_DB_PATH)
    database = connect(DEFAULT_DB_PATH)
    try:
        distributor_id = _browser_owner_distributor_id(database)
    finally:
        database.close()
    if distributor_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create the local business profile before using browser diagnostics.",
        )
    try:
        result = await browser_use_broker.dispatch(
            distributor_id,
            body.action,
            body.arguments,
            timeout_seconds=150.0 if body.action == "google_ai_mode" else 90.0,
        )
    except BrowserUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return BrowserDiagnosticActionResponse(result=result)


@router.post("/diagnostics/reload", include_in_schema=False)
async def reload_development_browser_extensions(
    request: Request,
    desktop_token: str = Header(default="", alias="X-Neo-Saarthi-Desktop-Token"),
) -> Dict[str, Any]:
    if os.getenv("APP_ENV", "development") != "development":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _require_desktop_pairing_authorized(request, desktop_token)
    initialize_database(DEFAULT_DB_PATH)
    database = connect(DEFAULT_DB_PATH)
    try:
        distributor_id = _browser_owner_distributor_id(database)
    finally:
        database.close()
    if distributor_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create the local business profile before reloading browser extensions.",
        )
    connection_status = await browser_use_broker.status(distributor_id)
    client_ids: List[str] = [
        str(client["client_id"])
        for client in connection_status.get("clients", [])
        if client.get("connected") is True and client.get("client_id")
    ]
    outcomes = await asyncio.gather(
        *[
            browser_use_broker.dispatch(
                distributor_id,
                "reload_extension",
                {},
                client_id=client_id,
                timeout_seconds=10.0,
            )
            for client_id in client_ids
        ],
        return_exceptions=True,
    )
    reloaded: List[str] = []
    failed: List[Dict[str, str]] = []
    for client_id, outcome in zip(client_ids, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            failed.append({"client_id": client_id, "error": str(outcome)})
        else:
            reloaded.append(client_id)
    return {"requested": len(client_ids), "reloaded": reloaded, "failed": failed}


@router.get("/status", response_model=BrowserBridgeStatusResponse)
async def browser_bridge_status(
    distributor: LocalDistributor = Depends(get_current_distributor),
) -> BrowserBridgeStatusResponse:
    payload = await browser_use_broker.status(distributor.distributor_id)
    return BrowserBridgeStatusResponse(**payload)


@router.post("/bridge/connect", response_model=BrowserBridgeConnectResponse)
async def connect_browser_bridge(
    body: BrowserBridgeConnectInput,
) -> BrowserBridgeConnectResponse:
    try:
        payload = await browser_use_broker.connect(
            body.pairing_code,
            client_id=body.client_id,
            label=body.label,
            browser_family=body.browser_family,
            profile_name=body.profile_name,
        )
    except BrowserUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc
    return BrowserBridgeConnectResponse(**payload)


@router.post("/bridge/control")
async def set_browser_bridge_control(body: BrowserBridgeControlInput) -> Dict[str, bool]:
    try:
        return await browser_use_broker.connection_control(body.bridge_token, body.paused)
    except BrowserUseError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/connections/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_browser_connection(
    body: BrowserConnectionRevokeInput,
    distributor: LocalDistributor = Depends(get_current_distributor),
) -> None:
    try:
        await browser_use_broker.revoke_connection(
            distributor.distributor_id,
            body.client_id,
        )
    except BrowserUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/connections/preferred", status_code=status.HTTP_204_NO_CONTENT)
async def select_browser_connection(
    body: BrowserConnectionSelectInput,
    distributor: LocalDistributor = Depends(get_current_distributor),
) -> None:
    try:
        await browser_use_broker.select_connection(
            distributor.distributor_id,
            body.client_id,
        )
    except BrowserUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/bridge/next", response_model=BrowserBridgePollResponse)
async def next_browser_job(
    body: BrowserBridgePollInput,
) -> BrowserBridgePollResponse:
    try:
        job = await browser_use_broker.next_job(body.bridge_token)
    except BrowserUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc
    return BrowserBridgePollResponse(job=job)


@router.post("/bridge/result", status_code=status.HTTP_204_NO_CONTENT)
async def complete_browser_job(body: BrowserBridgeResultInput) -> None:
    try:
        await browser_use_broker.submit_result(
            bridge_token=body.bridge_token,
            job_id=body.job_id,
            result=body.result,
            error=body.error,
        )
    except BrowserUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


class BrowserCursorMoveInput(BrowserBridgePollInput):
    x: float = Field(ge=0, allow_inf_nan=False)
    y: float = Field(ge=0, allow_inf_nan=False)
    viewport_width: float = Field(gt=0, le=50000, allow_inf_nan=False)
    viewport_height: float = Field(gt=0, le=50000, allow_inf_nan=False)
    title: str = Field(default="", max_length=512)
    probe: bool = False
    inspect: bool = False
    screen_x: Optional[float] = Field(default=None, ge=-100000, le=100000, allow_inf_nan=False)
    screen_y: Optional[float] = Field(default=None, ge=-100000, le=100000, allow_inf_nan=False)


@router.post("/bridge/cursor/move", include_in_schema=False)
async def move_browser_cursor(body: BrowserCursorMoveInput) -> Dict[str, Any]:
    await check_browser_cursor(body)
    try:
        result = await asyncio.to_thread(browser_cursor.move, body.model_dump(exclude={"bridge_token"}, exclude_none=True))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"arrived": True, **(result or {})}


@router.post("/bridge/cursor/check", include_in_schema=False)
async def check_browser_cursor(body: BrowserBridgePollInput) -> Dict[str, Any]:
    try:
        await browser_use_broker.ensure_connection_allowed(body.bridge_token)
    except BrowserUseError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        browser_cursor.ensure_allowed()
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"paused": False, **browser_cursor.status()}


def _browser_owner_distributor_id(database: Any) -> Optional[str]:
    owner = local_owner_distributor_id(database)
    if owner is None and _dev_auth_bypass_enabled():
        return _dev_distributor().distributor_id
    return owner


@router.post("/desktop/run", response_model=BrowserRunResponse, include_in_schema=False)
async def run_desktop_browser_automation_task(
    body: BrowserRunInput,
    request: Request,
    desktop_token: str = Header(default="", alias="X-Neo-Saarthi-Desktop-Token"),
) -> BrowserRunResponse:
    runner_enabled = os.getenv("NEO_SAARTHI_DEV_BROWSER_RUNNER", "").lower() in {"1", "true", "yes"}
    if os.getenv("APP_ENV", "development") != "development" or not (runner_enabled or _dev_auth_bypass_enabled() or os.getenv("NEO_SAARTHI_DEV_AUTH_BYPASS", "").lower() in {"1", "true", "yes"}):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _require_desktop_pairing_authorized(request, desktop_token)
    distributor_id = _local_owner()
    try:
        if body.sealed_task:
            try:
                body = BrowserRunInput(**open_local_task(body.sealed_task, desktop_token))
            except ValueError:
                raise ValueError("Protected local task is invalid, expired or already used.") from None
        elif any("password" in key.lower() and value for key, value in body.field_hints.items()):
            raise ValueError("Password-bearing local tasks must use protected transport. Update the runner.")
        result = await run_browser_task(
            distributor_id=distributor_id,
            goal=body.goal,
            budget=body.budget,
            client_id=body.browser_id,
            tab_id=body.tab_id,
            field_hints=body.field_hints,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Browser run failed: {exc}") from exc
    return BrowserRunResponse(**result)


@router.get("/desktop/secret-capabilities", include_in_schema=False)
async def desktop_secret_capabilities(request: Request, desktop_token: str = Header(default="", alias="X-Neo-Saarthi-Desktop-Token")) -> Dict[str, int]:
    if os.getenv("APP_ENV", "development") != "development":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _require_desktop_pairing_authorized(request, desktop_token)
    return {"sealed_task_version": 1}


@router.post("/run", response_model=BrowserRunResponse)
async def run_browser_automation_task(
    body: BrowserRunInput,
    distributor: LocalDistributor = Depends(get_current_distributor),
) -> BrowserRunResponse:
    try:
        result = await run_browser_task(
            distributor_id=distributor.distributor_id,
            goal=body.goal,
            budget=body.budget,
            client_id=body.browser_id,
            tab_id=body.tab_id,
            field_hints=body.field_hints,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Browser run failed: {exc}") from exc
    return BrowserRunResponse(**result)

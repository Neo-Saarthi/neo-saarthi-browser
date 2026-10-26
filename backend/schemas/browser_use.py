from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class BrowserPairingResponse(BaseModel):
    pairing_code: str
    expires_at: datetime


class BrowserBridgeConnectInput(BaseModel):
    pairing_code: str = Field(..., min_length=8, max_length=100)
    client_id: Optional[str] = Field(default=None, min_length=8, max_length=100)
    label: Optional[str] = Field(default=None, max_length=100)
    browser_family: Optional[str] = Field(default=None, max_length=30)
    profile_name: Optional[str] = Field(default=None, max_length=100)


class BrowserBridgeConnectResponse(BaseModel):
    bridge_token: str
    client_id: str
    distributor_id: str
    browser_family: str
    connected_at: datetime


class BrowserBridgePollInput(BaseModel):
    bridge_token: str = Field(..., min_length=20, max_length=200)


class BrowserBridgeControlInput(BrowserBridgePollInput):
    paused: Optional[bool] = None


class BrowserBridgeJob(BaseModel):
    id: str
    action: str
    arguments: Dict[str, Any]


class BrowserBridgePollResponse(BaseModel):
    job: Optional[BrowserBridgeJob] = None


class BrowserBridgeResultInput(BaseModel):
    bridge_token: str = Field(..., min_length=20, max_length=200)
    job_id: str = Field(..., min_length=10, max_length=100)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = Field(default=None, max_length=500)


class BrowserBridgeStatusResponse(BaseModel):
    connected: bool
    last_seen_at: Optional[datetime] = None
    clients: List[Dict[str, Any]] = Field(default_factory=list)


class BrowserAutoConnectInput(BaseModel):
    client_id: str = Field(..., min_length=8, max_length=100)
    label: str = Field(default="Browser", min_length=1, max_length=100)
    browser_family: str = Field(default="chromium", min_length=1, max_length=30)
    profile_name: Optional[str] = Field(default=None, max_length=100)


class BrowserConnectionRevokeInput(BaseModel):
    client_id: str = Field(..., min_length=1, max_length=100)


class BrowserConnectionSelectInput(BaseModel):
    client_id: str = Field(..., min_length=1, max_length=100)


class BrowserWorkflowTakeoverInput(BaseModel):
    run_id: str = Field(..., min_length=10, max_length=100)


class BrowserDesktopActionInput(BaseModel):
    action: Literal[
        "browser_info",
        "get_permissions",
        "set_permission",
        "set_allow_all_sites",
        "set_capability_policy",
        "set_history_access",
    ]
    client_id: Optional[str] = Field(default=None, max_length=100)
    arguments: Dict[str, Any] = Field(default_factory=dict)


class BrowserDiagnosticActionInput(BaseModel):
    action: Literal[
        "reload_extension",
        "allow_origin",
        "browser_info",
        "get_permissions",
        "set_permission",
        "set_allow_all_sites",
        "set_capability_policy",
        "set_history_access",
        "list_tabs",
        "list_windows",
        "list_tab_groups",
        "claim_tab",
        "release_tab",
        "group_tabs",
        "ungroup_tabs",
        "open",
        "create_window",
        "snapshot",
        "selected_text",
        "youtube_transcript",
        "screenshot",
        "scroll",
        "navigate",
        "move_tab",
        "download",
        "cancel_download",
        "download_status",
        "click",
        "double_click",
        "hover",
        "pointer",
        "drag",
        "fill",
        "upload",
        "press",
        "dialog",
        "wait",
        "history",
        "bookmarks",
        "google_ai_mode",
        "restart_extension",
    ]
    arguments: Dict[str, Any] = Field(default_factory=dict)


class BrowserDiagnosticActionResponse(BaseModel):
    result: Dict[str, Any]


class BrowserAuditEvent(BaseModel):
    id: str
    action: str
    status: str
    target_origin: Optional[str] = None
    error_message: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None


class BrowserAuditListResponse(BaseModel):
    retention_days: int
    events: List[BrowserAuditEvent] = Field(default_factory=list)


class BrowserAuditDeleteResponse(BaseModel):
    deleted: int


class BrowserRunInput(BaseModel):
    goal: str = Field(..., min_length=3, max_length=2000)
    budget: int = Field(default=35, ge=5, le=120)
    browser_id: Optional[str] = Field(default=None, max_length=100)
    tab_id: Optional[Union[str, int]] = Field(default=None)
    field_hints: Dict[str, str] = Field(default_factory=dict)
    sealed_task: Optional[str] = Field(default=None, max_length=60000)


class BrowserRunResponse(BaseModel):
    status: str
    message: str
    steps: int
    tab_id: Optional[Union[str, int]] = None
    current_url: str = ""
    elapsed_seconds: float = 0.0
    history: List[Dict[str, Any]] = Field(default_factory=list)
    confirmations: List[Dict[str, Any]] = Field(default_factory=list)

from contextvars import ContextVar
from typing import Any, Dict, Optional
from urllib.parse import urlsplit


task_secrets: ContextVar[Dict[str, str]] = ContextVar("browser_task_secrets", default={})


class SecretEntryError(RuntimeError):
    def __init__(self, stage: str, reason: str) -> None:
        self.stage = stage
        self.reason = reason
        super().__init__(f"Saved-password entry stopped at {stage}: {reason}")


def secret_failure_reason(error: Exception) -> str:
    if isinstance(error, SecretEntryError):
        return error.reason
    message = str(error)
    known = {
        "Enable saved-password entry in the extension before this task.": "consent_required",
        "The saved-password field is unavailable.": "field_unavailable",
        "The saved-password destination changed.": "destination_changed",
        "The saved-password field cannot be securely targeted.": "field_not_targetable",
        "This sensitive field requires manual entry.": "sensitive_field_denied",
        "This is not a password field.": "not_password_field",
        "No saved secret is authorized for this website and purpose.": "purpose_denied",
        "The target browser must stay in the foreground for saved-password entry.": "browser_not_foreground",
        "Browser focus changed before saved-password entry.": "focus_changed",
        "The focused browser document does not match the authorized secret destination.": "native_destination_mismatch",
        "The password field is read-only.": "field_readonly",
        "Windows could not securely populate the focused password field. Use manual takeover.": "native_input_failed",
        "The secret target could not be verified.": "target_unverified",
        "The saved secret has an unsupported length or control character.": "unsupported_secret_format",
        "Protected saved-password entry currently requires Windows.": "platform_unsupported",
    }
    return known.get(message, "unclassified_failure")


def secret_purpose(url: str, field: Dict[str, Any]) -> Optional[str]:
    parsed = urlsplit(url)
    label = str(field.get("label") or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    if not (field.get("isPassword") or field.get("input_type") == "password" or "password" in label or "pwd" in label):
        return None
    if any(word in label for word in ("otp", "one time", "verification", "payment", "card", "pin")):
        return None
    if parsed.hostname == "www.camsonline.com" and parsed.path.startswith("/Distributors/Service-Requests/Distributor-Mailback-Services/Request-Mailback/WBR"):
        return "report_password"
    if parsed.hostname == "dss.kfintech.com" and parsed.path.startswith("/dssweb/"):
        return "report_password" if any(word in label for word in ("report", "extract", "confirm", "re-enter", "reenter")) else "password"
    return None

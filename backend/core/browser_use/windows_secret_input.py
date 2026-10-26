import ctypes
import sys
from ctypes import wintypes
from typing import Any, Callable, Dict
from urllib.parse import urlsplit

if sys.platform == "win32":
    import comtypes
    from comtypes.client import CreateObject, GetModule

    UIA = GetModule("UIAutomationCore.dll")


def fill_focused_secret(target: Dict[str, Any], secret: str, ensure_allowed: Callable[[], None]) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Protected saved-password entry currently requires Windows.")
    if not secret or len(secret) > 256 or any(ord(character) < 32 for character in secret):
        raise RuntimeError("The saved secret has an unsupported length or control character.")
    if not target.get("element_id") or not target.get("ready") or urlsplit(target.get("url", "")).scheme != "https":
        raise RuntimeError("The secret target could not be verified.")
    comtypes.CoInitialize()
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        window = user32.GetForegroundWindow()
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(window, name, len(name))
        if name.value != "Chrome_WidgetWin_1":
            raise RuntimeError("The target browser must stay in the foreground for saved-password entry.")
        automation = CreateObject(UIA.CUIAutomation)
        element = automation.GetFocusedElement()
        if not element or element.CurrentControlType != UIA.UIA_EditControlTypeId or not element.CurrentIsEnabled or element.CurrentAutomationId != target["element_id"]:
            raise RuntimeError("Browser focus changed before saved-password entry.")
        parent = element
        document_url = None
        for _ in range(32):
            if parent.CurrentControlType == UIA.UIA_DocumentControlTypeId:
                try:
                    document_url = parent.GetCurrentPattern(UIA.UIA_LegacyIAccessiblePatternId).QueryInterface(UIA.IUIAutomationLegacyIAccessiblePattern).CurrentValue
                except (OSError, comtypes.COMError):
                    document_url = None
                if document_url:
                    break
            parent = automation.ControlViewWalker.GetParentElement(parent)
            if not parent:
                break
        if document_url != target["url"]:
            raise RuntimeError("The focused browser document does not match the authorized secret destination.")
        pattern = element.GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(UIA.IUIAutomationValuePattern)
        if pattern.CurrentIsReadOnly:
            raise RuntimeError("The password field is read-only.")
        ensure_allowed()
        if user32.GetForegroundWindow() != window or not automation.CompareElements(element, automation.GetFocusedElement()):
            raise RuntimeError("Browser focus changed before saved-password entry.")
        pattern.SetValue(secret)
        ensure_allowed()
    except (OSError, comtypes.COMError):
        raise RuntimeError("Windows could not securely populate the focused password field. Use manual takeover.") from None
    finally:
        comtypes.CoUninitialize()

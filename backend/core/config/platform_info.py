import platform
import sys
from typing import NoReturn


def _native_windows_fallback(table: str, *keys: str) -> NoReturn:
    raise OSError(f"Use native Windows platform information for {table}: {', '.join(keys)}")


def initialize_platform_identity() -> None:
    if sys.platform != "win32" or getattr(platform, "_uname_cache", None) is not None:
        return
    query = getattr(platform, "_wmi_query", None)
    if query is None:
        return
    platform._wmi_query = _native_windows_fallback
    try:
        platform.uname()
    finally:
        platform._wmi_query = query

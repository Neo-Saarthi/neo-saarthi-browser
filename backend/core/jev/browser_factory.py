"""Browser factory — bridge (user's real browser) with local fallback.

JEV_BROWSER_MODE:
  bridge -> user's existing browser via sidecar+extension (MF-Saarthi style).
            Raises if no paired browser.
  local  -> old behaviour: JEV launches its own Chromium.
  auto   -> try bridge, fall back to local Chromium (default).
"""

from __future__ import annotations

import os


def get_browser():
    mode = os.environ.get("JEV_BROWSER_MODE", "auto").strip().lower()
    if mode == "local":
        import browser
        return browser.Browser()
    if mode == "bridge":
        import browser_bridge
        return browser_bridge.BridgeBrowser()
    # auto
    try:
        import browser_bridge
        return browser_bridge.BridgeBrowser()
    except Exception:
        import browser
        return browser.Browser()


def describe() -> str:
    mode = os.environ.get("JEV_BROWSER_MODE", "auto").strip().lower()
    return mode

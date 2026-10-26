import asyncio
import ipaddress
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlparse
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, ElementHandle, Error as PlaywrightError, Page, Playwright, Route, async_playwright

from core.browser_use.network_proxy import PublicHttpsProxy


class ManagedBrowserError(ValueError):
    pass


def safe_public_url(url: str) -> str:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ManagedBrowserError("Clean browser access requires a public HTTPS URL.") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or port not in {None, 443}:
        raise ManagedBrowserError("Clean browser access requires a public HTTPS URL.")
    if any(key.casefold() in {"token", "access_token", "auth", "authorization", "api_key", "apikey", "session", "code"} for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise ManagedBrowserError("URLs containing credential parameters are unavailable in the clean browser.")
    hostname = parsed.hostname.casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".local", ".internal", ".lan", ".test")):
        raise ManagedBrowserError("Local and private network sites are unavailable in the clean browser.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return url
    if not address.is_global:
        raise ManagedBrowserError("Local and private network sites are unavailable in the clean browser.")
    return url


def redact_text(value: str) -> str:
    value = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[redacted email]", value, flags=re.IGNORECASE)
    value = re.sub(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", "[redacted identifier]", value)
    value = re.sub(r"\b(?:\+?91[-\s]?)?[6-9]\d{9}\b", "[redacted phone]", value)
    return re.sub(r"\b\d{12,19}\b", "[redacted number]", value)


def sensitive_fill_target(hints: str) -> bool:
    normalized = re.sub(r"[_-]+", " ", hints.casefold())
    return bool(re.search(r"\b(?:password|hidden|otp|one time|auth|verification code|security code|recovery|credit|card|cvv|bank|account|routing|ifsc|aadhaar|aadhar|passport|pin|email|phone|mobile|ssn|social security|tax id|pan|dob|birth|api key|token|secret)\b", normalized))


class ManagedBrowser:
    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._contexts: Dict[str, BrowserContext] = {}
        self._pages: Dict[str, Dict[str, Page]] = {}
        self._elements: Dict[str, Dict[str, ElementHandle]] = {}
        self._lock = asyncio.Lock()
        self._context_lock = asyncio.Lock()
        self._proxy: Optional[PublicHttpsProxy] = None

    @property
    def running(self) -> bool:
        return bool(self._browser and self._browser.is_connected())

    async def _ensure(self) -> Browser:
        async with self._lock:
            if self._browser and self._browser.is_connected():
                return self._browser
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            if self._proxy:
                await self._proxy.close()
                self._proxy = None
            proxy = PublicHttpsProxy()
            proxy_url = await proxy.start()
            self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    proxy={"server": proxy_url},
                    args=[
                        "--proxy-bypass-list=<-loopback>",
                        "--disable-quic",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    ],
                )
            except Exception as exc:
                await self._playwright.stop()
                self._playwright = None
                await proxy.close()
                raise ManagedBrowserError("The bundled clean browser is unavailable.") from exc
            self._proxy = proxy
            return self._browser

    async def _context(self, distributor_id: str) -> BrowserContext:
        async with self._context_lock:
            existing = self._contexts.get(distributor_id)
            if existing and self.running:
                return existing
            self._contexts.pop(distributor_id, None)
            self._pages.pop(distributor_id, None)
            browser = await self._ensure()
            context = await browser.new_context(accept_downloads=False, service_workers="block")
            await context.route("**/*", self._route)
            self._contexts[distributor_id] = context
            self._pages[distributor_id] = {}
            return context

    async def _close_popup(self, popup: Page) -> None:
        try:
            await popup.close()
        except Exception:
            return

    async def _route(self, route: Route) -> None:
        try:
            safe_public_url(route.request.url)
            parsed = urlparse(route.request.url)
            loop = asyncio.get_running_loop()
            addresses = await asyncio.wait_for(loop.getaddrinfo(parsed.hostname, 443), timeout=5.0)
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                await route.abort()
                return
            await route.continue_()
        except (ManagedBrowserError, OSError, ValueError, asyncio.TimeoutError):
            await route.abort()

    def _page(self, distributor_id: str, tab_id: str) -> Page:
        page = self._pages.get(distributor_id, {}).get(tab_id)
        if page is None or page.is_closed():
            raise ManagedBrowserError("The clean browser tab is unavailable.")
        return page

    async def open(self, distributor_id: str, url: str) -> Dict[str, Any]:
        safe_public_url(url)
        context = await self._context(distributor_id)
        if len(self._pages[distributor_id]) >= 10:
            raise ManagedBrowserError("Close an existing clean browser tab before opening another.")
        page = await context.new_page()
        page.on("popup", lambda popup: asyncio.create_task(self._close_popup(popup)))
        tab_id = str(uuid4())
        self._pages[distributor_id][tab_id] = page
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return {"tab_id": tab_id, "page": await self.snapshot(distributor_id, tab_id)}
        except Exception as exc:
            await page.close()
            self._pages[distributor_id].pop(tab_id, None)
            raise ManagedBrowserError("The public page could not be opened.") from exc

    async def snapshot(self, distributor_id: str, tab_id: str) -> Dict[str, Any]:
        page = self._page(distributor_id, tab_id)
        self._elements[tab_id] = {}
        interactive: List[Dict[str, Any]] = []
        selector = "a,button,input,textarea,select,[role='button'],[role='link'],[contenteditable]"
        for frame_index, frame in enumerate(page.frames):
            try:
                locator = frame.locator(selector)
                count = min(await locator.count(), 100)
                for index in range(count):
                    if len(interactive) >= 200:
                        break
                    handle = await locator.nth(index).element_handle()
                    if handle is None:
                        continue
                    details = await handle.evaluate("element => { const rect = element.getBoundingClientRect(); const style = getComputedStyle(element); return {tag: element.tagName.toLowerCase(), type: element.type || '', label: (element.getAttribute('aria-label') || element.innerText || element.getAttribute('placeholder') || '').trim(), href: element.href || null, disabled: Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true'), visible: rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden'}; }")
                    if not details["visible"]:
                        continue
                    element_id = f"f{frame_index}:mf-{uuid4()}"
                    self._elements[tab_id][element_id] = handle
                    href = details["href"]
                    parsed_href = urlparse(href) if href else None
                    interactive.append({"id": element_id, "tag": details["tag"], "type": details["type"], "label": redact_text(str(details["label"]))[:300], "href": f"{parsed_href.scheme}://{parsed_href.netloc}{parsed_href.path}" if parsed_href and parsed_href.scheme == "https" else None, "disabled": details["disabled"]})
            except PlaywrightError:
                continue
        parsed = urlparse(page.url)
        try:
            body_text = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_text = ""
        return {"title": redact_text(await page.title()), "url": f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "text": redact_text(body_text[:30000]), "interactive": interactive}

    async def act(
        self,
        distributor_id: str,
        tab_id: str,
        action: str,
        element_id: str,
        expected_origin: str,
        element_label: str,
        value: Optional[str] = None,
    ) -> Dict[str, Any]:
        page = self._page(distributor_id, tab_id)
        parsed = urlparse(page.url)
        if f"{parsed.scheme}://{parsed.netloc}" != expected_origin:
            raise ManagedBrowserError("The clean browser page changed. Read it again.")
        handle = self._elements.get(tab_id, {}).get(element_id)
        if handle is None:
            raise ManagedBrowserError("The page reference is stale. Read the page again.")
        try:
            actual_label = await handle.evaluate("element => (element.getAttribute('aria-label') || element.innerText || element.getAttribute('placeholder') || '').trim()")
            if redact_text(str(actual_label))[:300] != element_label or not await handle.is_visible():
                raise ManagedBrowserError("The recorded control changed. Read the page again.")
            if action == "click":
                await handle.click(timeout=10000)
            elif action == "fill" and value is not None:
                hints = await handle.evaluate("element => [element.type, element.name, element.id, element.getAttribute('autocomplete'), element.getAttribute('aria-label')].join(' ').toLowerCase()")
                if sensitive_fill_target(str(hints)):
                    raise ManagedBrowserError("Sensitive fields cannot be filled by the clean browser.")
                await handle.fill(value, timeout=10000)
            else:
                raise ManagedBrowserError("Unsupported clean browser action.")
            return {"tab_id": tab_id, "page": await self.snapshot(distributor_id, tab_id)}
        except ManagedBrowserError:
            raise
        except Exception as exc:
            raise ManagedBrowserError("The page changed before the action completed. Read it again.") from exc

    async def scroll(self, distributor_id: str, tab_id: str, delta_y: int) -> Dict[str, Any]:
        page = self._page(distributor_id, tab_id)
        await page.mouse.wheel(0, max(-2000, min(delta_y, 2000)))
        return {"tab_id": tab_id, "page": await self.snapshot(distributor_id, tab_id)}

    async def close_tab(self, distributor_id: str, tab_id: str) -> Dict[str, Any]:
        page = self._page(distributor_id, tab_id)
        await page.close()
        self._pages[distributor_id].pop(tab_id, None)
        self._elements.pop(tab_id, None)
        return {"tab_id": tab_id, "closed": True}

    async def close(self) -> None:
        for context in list(self._contexts.values()):
            await context.close()
        self._contexts.clear()
        self._pages.clear()
        self._elements.clear()
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        if self._proxy:
            await self._proxy.close()
            self._proxy = None


managed_browser = ManagedBrowser()

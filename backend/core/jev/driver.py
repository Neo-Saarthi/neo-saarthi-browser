import asyncio
import base64
import threading
from typing import Any, Dict, List, Optional
from core.browser_use.broker import BrowserUseError, browser_use_broker

class JevNeedsInput(Exception):
    pass

def _role_for(tag: str, input_type: str) -> str:
    tag_low = (tag or '').lower()
    type_low = (input_type or '').lower()
    if tag_low == 'a':
        return 'link'
    if tag_low == 'button':
        return 'button'
    if tag_low == 'select':
        return 'combobox'
    if tag_low == 'textarea':
        return 'textbox'
    if tag_low == 'input':
        if type_low in ('checkbox', 'radio'):
            return type_low
        if type_low == 'search':
            return 'searchbox'
        return 'textbox'
    return 'button'

def _is_editable(tag: str, input_type: str) -> bool:
    tag_low = (tag or '').lower()
    type_low = (input_type or '').lower()
    if tag_low in ('textarea', 'select'):
        return True
    if tag_low == 'input':
        return type_low not in ('checkbox', 'radio', 'button', 'submit', 'reset', 'image', 'file', 'hidden')
    return False

class _JevKeyShim:

    def __init__(self, driver: 'JevBrowserDriver') -> None:
        self._driver = driver

    def press(self, key: str) -> None:
        self._driver.queue_key(key)

class _JevMouseShim:

    def __init__(self, driver: 'JevBrowserDriver') -> None:
        self._driver = driver

    def click(self, x: int, y: int) -> None:
        self._driver.queue_key('Escape')

class _JevLocatorShim:

    def count(self) -> int:
        return 0

    @property
    def first(self) -> '_JevLocatorShim':
        return self

    def screenshot(self, path: Optional[str]=None, *args: Any, **kwargs: Any) -> bytes:
        raise RuntimeError('Element screenshot is unavailable.')

class _JevPageShim:

    def __init__(self, driver: 'JevBrowserDriver') -> None:
        self._driver = driver
        self.keyboard = _JevKeyShim(driver)
        self.mouse = _JevMouseShim(driver)

    def evaluate(self, script: str, arg: Any=None) -> Any:
        return self._driver.evaluate_text(script)

    def wait_for_timeout(self, ms: int) -> None:
        return None

    def locator(self, selector: str) -> _JevLocatorShim:
        return _JevLocatorShim()

class JevBrowserDriver:

    def __init__(self, distributor_id: str, client_id: Optional[str]=None, tab_id: Optional[str]=None, timeout_seconds: float=90.0) -> None:
        self.distributor_id = distributor_id
        self.client_id = client_id
        self.tab_id = tab_id
        self.timeout_seconds = timeout_seconds
        self.notes: List[Dict[str, Any]] = []
        self.filled_ids: List[str] = []
        self._lock = threading.Lock()
        self._elements: Dict[str, Dict[str, Any]] = {}
        self._last_url = ''
        self._last_title = ''
        self._last_text = ''
        self._pending_keys: List[str] = []
        self._captcha_image: Optional[bytes] = None
        self._page_shim = _JevPageShim(self)

    @property
    def _page(self) -> _JevPageShim:
        return self._page_shim

    @property
    def url(self) -> str:
        with self._lock:
            return self._last_url

    @property
    def title(self) -> str:
        with self._lock:
            return self._last_title

    @property
    def is_feed(self) -> bool:
        with self._lock:
            url = self._last_url.lower()
        return any((m in url for m in ('/shorts/', 'tiktok.com', 'instagram.com/reel', '/reel/')))

    def page_text(self) -> str:
        with self._lock:
            return self._last_text

    def note(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self.notes.append(record)

    def queue_key(self, key: str) -> None:
        with self._lock:
            self._pending_keys.append(key)

    def take_queued_keys(self) -> List[str]:
        with self._lock:
            keys = list(self._pending_keys)
            self._pending_keys.clear()
            return keys

    def read_captcha_image(self) -> Optional[bytes]:
        with self._lock:
            return self._captcha_image

    def set_captcha_image(self, raw: Optional[bytes]) -> None:
        with self._lock:
            self._captcha_image = raw

    def evaluate_text(self, script: str) -> Any:
        text = (script or '').lower()
        if 'todataurl' in text:
            return None
        if 'muicircularprogress' in text or 'progressbar' in text or 'spinner' in text:
            return False
        if '.click(' in text:
            return 'not_found'
        if 'focus(' in text and 'innertext' not in text and ('textcontent' not in text):
            return True
        with self._lock:
            return self._last_text

    def get_element(self, index: Optional[str]) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._elements.get(index or '')

    def find_by_label(self, label: str, exclude: Optional[str]=None) -> Optional[str]:
        wanted = ' '.join((label or '').lower().split())
        if not wanted:
            return None
        with self._lock:
            items = dict(self._elements)
        partial: Optional[str] = None
        for index in sorted(items, key=lambda i: int(i)):
            if index == exclude:
                continue
            have = ' '.join((items[index].get('label') or '').lower().split())
            if have == wanted:
                return index
            if partial is None and have and (wanted in have or have in wanted):
                shorter, longer = sorted((len(wanted), len(have)))
                if shorter >= 4 and shorter >= 0.5 * longer:
                    partial = index
        return partial

    async def _dispatch(self, action: str, arguments: Dict[str, Any], timeout: Optional[float]=None) -> Dict[str, Any]:
        payload = dict(arguments)
        if self.tab_id:
            payload.setdefault('tab_id', self.tab_id)
        try:
            return await browser_use_broker.dispatch(self.distributor_id, action, payload, timeout_seconds=timeout or self.timeout_seconds, client_id=self.client_id)
        except BrowserUseError as exc:
            message = str(exc).lower()
            if any((k in message for k in ('blocked', 'not allowed', 'denied', 'permission'))):
                raise JevNeedsInput(str(exc)) from exc
            raise

    def _adapt_snapshot(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        interactive = raw.get('interactive') or []
        elements: List[Dict[str, Any]] = []
        with self._lock:
            filled = set(self.filled_ids)
        for position, item in enumerate(interactive[:250], start=1):
            tag = str(item.get('tag') or '')
            input_type = str(item.get('type') or '')
            role = _role_for(tag, input_type)
            element_id = str(item.get('id') or '')
            operations = ['CLICK']
            if _is_editable(tag, input_type):
                operations = ['TYPE_TEXT', 'CLICK']
            element: Dict[str, Any] = {'index': str(position), 'node': element_id, 'role': role, 'label': str(item.get('label') or role)[:120], 'operations': operations}
            if input_type.lower() == 'password':
                element['isPassword'] = True
            if item.get('href'):
                element['href'] = item.get('href')
            if item.get('checked') is not None:
                element['checked'] = 'true' if item.get('checked') else 'false'
            if item.get('selected') is not None:
                element['selected'] = 'true' if item.get('selected') else 'false'
            if element_id in filled:
                element['value'] = 'filled'
            elif role in ('textbox', 'searchbox', 'combobox'):
                element['value'] = ''
            elements.append(element)
        scroll_y = int(raw.get('scroll_y') or 0)
        doc_height = int(raw.get('document_height') or 0)
        viewport = int(raw.get('viewport_height') or 800)
        scrollable = doc_height > viewport + 80
        at_bottom = bool(scrollable and scroll_y + viewport >= doc_height - 30)
        percent = 0
        if scrollable and doc_height > viewport:
            percent = round(scroll_y / (doc_height - viewport) * 100)
        url = str(raw.get('url') or '')
        title = str(raw.get('title') or '')
        text = str(raw.get('text') or '')
        with self._lock:
            self._elements = {e['index']: e for e in elements}
            self._last_url = url
            self._last_title = title
            self._last_text = text
        return {'url': url, 'title': title, 'text': text, 'elements': elements, 'scroll_y': scroll_y, 'scroll_height': doc_height, 'client_height': viewport, 'is_scrollable': scrollable, 'at_bottom': at_bottom, 'scroll_percent': percent}

    async def goto(self, url: str) -> None:
        result = await self._dispatch('open', {'url': url}, timeout=45.0)
        if result.get('tab_id'):
            self.tab_id = str(result.get('tab_id'))
        page = result.get('page')
        if isinstance(page, dict):
            self._adapt_snapshot(page)
        else:
            await self.snapshot()

    async def reload(self) -> None:
        result = await self._dispatch('navigate', {'direction': 'reload'}, timeout=45.0)
        page = result.get('page')
        if isinstance(page, dict):
            self._adapt_snapshot(page)

    async def snapshot(self) -> Dict[str, Any]:
        result = await self._dispatch('snapshot', {}, timeout=30.0)
        page = result.get('page')
        if isinstance(page, dict):
            return self._adapt_snapshot(page)
        with self._lock:
            elements = list(self._elements.values())
            return {'url': self._last_url, 'title': self._last_title, 'text': self._last_text, 'elements': elements, 'scroll_y': 0, 'scroll_height': 0, 'client_height': 800, 'is_scrollable': False, 'at_bottom': False, 'scroll_percent': 0}

    async def flush_keys(self) -> None:
        for key in self.take_queued_keys():
            allowed = key if key in ('Escape', 'Enter', 'Tab') else 'Escape'
            await self._dispatch('press', {'key': allowed, 'frame_id': 0}, timeout=30.0)

    async def go_back(self) -> str:
        await self._dispatch('navigate', {'direction': 'back'}, timeout=30.0)
        return 'went back to previous page'

    async def capture_screenshot(self) -> Optional[bytes]:
        try:
            result = await self._dispatch('screenshot', {'full_page': False}, timeout=45.0)
        except Exception:
            return None
        image = result.get('image') or ''
        if ',' in image:
            try:
                return base64.b64decode(image.split(',', 1)[1])
            except Exception:
                return None
        return None

    async def act(self, index: Optional[str], kind: str, text: Optional[str]=None) -> str:
        await self.flush_keys()
        if kind == 'BACK':
            return await self.go_back()
        if kind == 'WAIT':
            try:
                await self._dispatch('wait', {'timeout_ms': 5000}, timeout=30.0)
            except BrowserUseError:
                await asyncio.sleep(1.0)
            return 'waited'
        if kind == 'PRESS_ESCAPE':
            await self._dispatch('press', {'key': 'Escape', 'frame_id': 0}, timeout=30.0)
            return 'pressed escape'
        if kind in ('SCROLL', 'SCROLL_TOP', 'SCROLL_UP'):
            if self.is_feed:
                await asyncio.sleep(0.2)
                return 'no scroll available on feed'
            delta = -5000 if kind == 'SCROLL_TOP' else -600 if kind == 'SCROLL_UP' else 600
            await self._dispatch('scroll', {'delta_x': 0, 'delta_y': delta, 'frame_id': 0}, timeout=30.0)
            return 'scrolled to top' if kind == 'SCROLL_TOP' else 'scrolled up' if kind == 'SCROLL_UP' else 'scrolled'
        element = self.get_element(index)
        if element is None:
            raise RuntimeError(f'element [{index}] is gone, re-snapshot')
        node = element.get('node') or ''
        if kind == 'TYPE_TEXT':
            try:
                await self._dispatch('fill', {'element_id': node, 'text': text or ''}, timeout=45.0)
            except JevNeedsInput:
                raise
            except BrowserUseError as exc:
                raise RuntimeError(f'element [{index}] is gone, covered, or disabled ({exc})') from exc
            with self._lock:
                if node and node not in self.filled_ids:
                    self.filled_ids.append(node)
            await asyncio.sleep(0.4)
            return f'typed into [{index}]'
        try:
            await self._dispatch('click', {'element_id': node}, timeout=45.0)
        except JevNeedsInput:
            raise
        except BrowserUseError as exc:
            raise RuntimeError(f'element [{index}] is gone, covered, or disabled ({exc})') from exc
        await asyncio.sleep(0.5)
        return f'clicked [{index}]'

    async def close(self) -> None:
        return None

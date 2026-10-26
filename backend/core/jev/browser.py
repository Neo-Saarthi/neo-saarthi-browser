"""Headful/headless Chromium automation engine. Clean accessibility snapshot -> numbered elements -> act."""

import base64
import os
import time
from typing import Any, Dict, List, Optional

from playwright.sync_api import sync_playwright

PAGE_TEXT_LIMIT = 6000
READY_TEXT_MIN = 80
READY_CONTROLS_MIN = 1
READY_POLL_MS = 100
READY_TIMEOUT_MS = 8000
FEED_MARKERS = ("/shorts/", "tiktok.com", "instagram.com/reel", "/reel/")

READY_JS = r"""(() => {
  if (!document.body) return {text: 0, controls: 0};
  const box = 'a,button,input,textarea,select,[role="button"],[role="link"],' +
    '[role="textbox"],[role="searchbox"],[role="combobox"]';
  let controls = 0;
  for (const e of document.querySelectorAll(box)) {
    const r = e.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= innerHeight &&
        r.left >= 0 && r.right <= innerWidth) controls++;
  }
  return {text: document.body.innerText.length, controls};
})()"""


def labels_overlap(wanted: str, have: str, min_length: int = 4, min_ratio: float = 0.5) -> bool:
    """True when one label contains the other and they are of comparable size."""
    if not wanted or not have:
        return False
    if wanted in have or have in wanted:
        shorter, longer = sorted((len(wanted), len(have)))
        return shorter >= min_length and shorter >= min_ratio * longer
    return False


def page_ready(page) -> bool:
    """True when a page shows real text and at least one on-screen control."""
    try:
        stats = page.evaluate(READY_JS) or {}
    except Exception:
        return False
    text = stats.get("text") or 0
    controls = stats.get("controls") or 0
    return text > READY_TEXT_MIN and controls >= READY_CONTROLS_MIN


SNAPSHOT_JS = r"""(() => {
  if (!document.body) return null;

  const cache = window.__jevFast = window.__jevFast || {ids: new WeakMap(), nodes: new Map(), next: 1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e, cache.next++);
    const id = cache.ids.get(e);
    cache.nodes.set(id, e);
    return id;
  };
  for (const [id, e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);

  const safe = e => !['file','hidden'].includes(e.type);
  const visible = e => {
    if (e.closest('[aria-hidden="true"],[inert]')) return false;
    if (['checkbox', 'radio'].includes(e.type)) {
      return e.checkVisibility ? e.checkVisibility() : true;
    }
    return e.checkVisibility ? e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : true;
  };

  const getAccessibleName = (e, seen) => {
    seen = seen || new Set();
    if (!e || seen.has(e)) return '';
    seen.add(e);

    const ref = (e.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => getAccessibleName(document.getElementById(id), seen)).filter(Boolean).join(' ');
    if (ref) return ref;

    const aria = e.getAttribute('aria-label');
    if (aria) return aria;

    const labels = [...(e.labels || [])].map(l => getAccessibleName(l, seen)).filter(Boolean).join(' ');
    if (labels) return labels;

    if (['button','submit','reset'].includes(e.type) && e.value) return e.value;
    if (e.getAttribute('alt')) return e.getAttribute('alt');
    if (e.getAttribute('placeholder')) return e.getAttribute('placeholder');
    if (e.getAttribute('title')) return e.getAttribute('title');

    // Check if element is a modal backdrop or dialog overlay
    if (e.classList && (e.classList.contains('cdk-overlay-backdrop') || e.classList.contains('modal-backdrop'))) {
      return 'close dropdown overlay';
    }

    if (e.tagName !== 'INPUT') {
      const text = [...e.childNodes].map(n => {
        if (n.nodeType === 3) return n.textContent;
        if (n.nodeType === 1 && n.getAttribute('aria-hidden') !== 'true') return getAccessibleName(n, seen);
        return '';
      }).join(' ').trim();
      if (text) return text;
    }

    // Associated label or heading lookup for form inputs
    if (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA') {
      const parentLabel = e.closest('label');
      if (parentLabel && parentLabel.innerText && parentLabel.innerText.trim()) {
        const pt = parentLabel.innerText.trim();
        if (pt.length <= 80) return pt;
      }
      let p = e.parentElement;
      for (let depth = 0; depth < 4 && p; depth++) {
        const lbl = p.querySelector('label, mat-label, [class*="label"], [class*="title"], h4, h5, h6, b, strong');
        if (lbl && lbl !== e && lbl.innerText && lbl.innerText.trim() && !lbl.contains(e)) {
          const t = lbl.innerText.trim();
          if (t.length <= 50) return t;
        }
        p = p.parentElement;
      }
      const raw = [e.getAttribute('placeholder'), e.getAttribute('title'), e.name, e.getAttribute('formcontrolname'), e.id].filter(Boolean).join(' ');
      if (raw) {
        return raw.replace(/([a-z])([A-Z])/g, '$1 $2').replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
      }
    }

    return '';
  };

  const roles = ['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector = 'a,button,input,textarea,select,summary,[tabindex],[role],' +
    '[class*="backdrop"],[class*="modal"],[class*="overlay"],[class*="checkbox"],' +
    '[class*="option"],[class*="tab"],[class*="item"],[class*="panel"] > *,[role="listbox"] *,[role="menu"] *';

  const determineRole = e => {
    const explicit = e.getAttribute('role');
    if (explicit && roles.includes(explicit)) return explicit;
    const tag = (e.tagName || '').toUpperCase();
    const cls = String(e.className || '');
    if (e.classList && (e.classList.contains('cdk-overlay-backdrop') || e.classList.contains('modal-backdrop') || e.classList.contains('MuiBackdrop-root') || e.classList.contains('MuiModal-backdrop'))) return 'button';
    if (tag === 'BUTTON' || tag === 'SUMMARY') return 'button';
    if (tag === 'A') return 'link';
    if (tag === 'SELECT') return 'combobox';
    if (tag === 'TEXTAREA' || e.isContentEditable) return 'textbox';
    if (tag.includes('CHECKBOX') || cls.includes('checkbox')) return 'checkbox';
    if (tag.includes('OPTION') || cls.includes('option')) return 'option';
    if (tag.includes('TAB') || cls.includes('tab')) return 'tab';
    if (tag === 'INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type === 'search') return 'searchbox';
      if (e.type === 'number') return 'spinbutton';
      return 'textbox';
    }
    // Check if styled custom widget
    if (e.getAttribute('aria-expanded') !== null || e.getAttribute('aria-haspopup') !== null) return 'combobox';
    if (e.getAttribute('aria-selected') !== null) return 'tab';
    if (e.getAttribute('aria-checked') !== null) return 'checkbox';
    return null;
  };

  const actions = [];
  for (const e of document.querySelectorAll(selector)) {
    const isBackdrop = e.classList && (e.classList.contains('cdk-overlay-backdrop') || e.classList.contains('modal-backdrop') || e.classList.contains('MuiBackdrop-root') || e.classList.contains('MuiModal-backdrop'));
    if (!isBackdrop && (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]'))) continue;
    if (!isBackdrop && e.closest && e.closest('[role="option"], mat-option') && !e.matches('[role="option"], mat-option')) continue;

    const r = e.getBoundingClientRect();
    const x = isBackdrop ? 50 : r.x + r.width / 2;
    const y = isBackdrop ? 50 : r.y + r.height / 2;
    const rname = determineRole(e);
    if (!rname || r.width <= 0 || r.height <= 0 || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;

    const rawLabel = getAccessibleName(e) || rname;
    const cleanLabel = rawLabel.replace(/\s+/g, ' ').trim();

    const base = {
      node: identity(e),
      role: rname,
      label: cleanLabel.slice(0, 120),
    };

    if (e.tagName === 'INPUT') {
      base.inputType = e.type || 'text';
    }

    if (e.type === 'password') {
      base.isPassword = true;
    }

    if (e.tagName === 'A' && e.getAttribute('href')) {
      base.href = e.getAttribute('href');
    }

    if (e.classList && (e.classList.contains('cdk-overlay-backdrop') || e.classList.contains('modal-backdrop') || e.classList.contains('MuiBackdrop-root'))) continue;

    for (const key of ['checked','selected','expanded']) {
      const value = e.getAttribute('aria-' + key);
      if (value !== null) base[key] = value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked = String(e.checked);
    const clsAll = (e.className ? String(e.className) : '') + ' ' + (e.parentElement?.className ? String(e.parentElement.className) : '');
    if (!base.checked && /checked|selected/i.test(clsAll)) base.checked = 'true';
    if (!base.selected && /selected|active/i.test(clsAll)) base.selected = 'true';
    if (rname === 'checkbox' || rname === 'radio') {
      try {
        if (!base.checked) {
          const inner = e.querySelector ? (e.querySelector('input:checked') || e.querySelector('[aria-checked="true"]')) : null;
          if (inner) base.checked = 'true';
          else if (e.querySelectorAll) {
            const kids = e.querySelectorAll('*');
            for (const d of kids) {
              const c = String(d.className || '');
              if (/checkbox--selected|checkbox-checked|mat-checkbox-checked|mdc-checkbox--selected/i.test(c) || (d.getAttribute && d.getAttribute('aria-checked') === 'true')) {
                base.checked = 'true';
                break;
              }
            }
          }
        }
        if (!base.checked) base.checked = 'false';
      } catch (err) {}
    }

    if (e.tagName === 'SELECT') {
      base.value = [...e.selectedOptions].map(o => o.label).join(', ').slice(0, 80);
      for (const o of e.options) {
        if (o.selected || o.disabled || o.closest('optgroup[disabled]')) continue;
        actions.push(Object.assign({}, base, {select: o.value, label: (base.label + ' → ' + o.label).slice(0, 120)}));
      }
      continue;
    }

    const editable = !e.readOnly && e.getAttribute('aria-readonly') !== 'true' &&
      (['textbox','searchbox','spinbutton','combobox'].includes(rname) || e.isContentEditable || e.tagName === 'INPUT');
    if (editable) base.editable = true;

    const val = (e.type === 'password') ? (e.value ? '••••••••' : '') : ('value' in e ? String(e.value) : (e.innerText || '').trim());
    if (val) base.value = val.slice(0, 80);

    actions.push(base);
  }

  // Visible text extraction
  const words = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let node, length = 0;
  while ((node = walker.nextNode()) && length < 6000) {
    const value = node.textContent.trim(), parent = node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node);
    const r = range.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth) {
      words.push(value);
      length += value.length;
    }
  }

  const elements = actions.slice(0, 250).map((a, i) => Object.assign({}, a, {index: String(i + 1)}));

  // Universal scrollable container discovery
  let scrollY = window.scrollY || window.pageYOffset || 0;
  let scrollHeight = Math.max(document.documentElement.scrollHeight || 0, document.body ? document.body.scrollHeight : 0);
  let clientHeight = window.innerHeight || document.documentElement.clientHeight || 1;

  // Find any inner scrollable element if window itself is not scrolling
  const scrollables = Array.from(document.querySelectorAll('main, article, [style*="overflow"], [class*="content"], [class*="body"]'));
  for (const el of scrollables) {
    const style = window.getComputedStyle(el);
    if (['auto', 'scroll'].includes(style.overflowY) && el.scrollHeight > el.clientHeight + 60) {
      scrollY = Math.max(scrollY, el.scrollTop);
      scrollHeight = Math.max(scrollHeight, el.scrollHeight);
      clientHeight = Math.max(clientHeight, el.clientHeight);
      break;
    }
  }

  const isScrollable = scrollHeight > clientHeight + 80;
  const atBottom = isScrollable && (scrollY + clientHeight >= scrollHeight - 30);
  const scrollPercent = isScrollable ? Math.round((scrollY / (scrollHeight - clientHeight)) * 100) : 0;

  return {
    url: location.href,
    title: document.title,
    text: words.join('\n').slice(0, 6000),
    elements,
    scrollY: Math.round(scrollY),
    scrollHeight: Math.round(scrollHeight),
    clientHeight: Math.round(clientHeight),
    isScrollable,
    atBottom,
    scrollPercent,
  };
})()"""

CLICK_TARGET_JS = r"""([node, filling]) => {
  const e = window.__jevFast ? window.__jevFast.nodes.get(node) : null;
  if (!e || !e.isConnected || e.matches(':disabled') ||
      e.closest('[aria-disabled="true"],[inert]') ||
      !e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return null;

  e.scrollIntoView?.({block: 'nearest', inline: 'nearest', behavior: 'instant'});

  // Generic UI helper: If clicking an option, menuitem, or label that wraps/contains a checkbox or radio,
  // click the checkbox/radio directly so toggle state updates cleanly across custom UI frameworks.
  // Never redirect row/table/card/email items (such as Gmail inbox rows).
  let targetEl = e;
  if (!filling) {
    const isRowOrEmail = Boolean(e.closest?.('tr, [role="row"], .zA, [data-legacy-thread-id]'));
    const isDropdownOption = Boolean(e.matches?.('[role="option"], [role="menuitem"], mat-option, .mat-option, li[role="option"]') ||
                             e.closest?.('.mat-select-panel, .mat-autocomplete-panel, .cdk-overlay-pane, [role="listbox"], [role="menu"]'));
    const isLabel = e.tagName === 'LABEL';

    if (!isRowOrEmail && (isLabel || isDropdownOption)) {
      const box = e.querySelector?.('input[type="checkbox"], input[type="radio"], [class*="Checkbox-root"], [class*="checkbox"], [role="checkbox"], .mat-select-trigger');
      if (box && (typeof box.checkVisibility !== 'function' || box.checkVisibility({checkOpacity: true}))) {
        targetEl = box;
      }
    }
  }

  const r = targetEl.getBoundingClientRect();
  const x = r.x + r.width / 2, y = r.y + r.height / 2;
  if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return null;

  const stack = (document.elementsFromPoint ? document.elementsFromPoint(x, y) :
    [document.elementFromPoint(x, y)]) || [];

  const related = el => el && (el === e || e.contains(el) || el.contains(e) ||
    el === targetEl || targetEl.contains(el) || el.contains(targetEl) ||
    (el.tagName === 'LABEL' && el.control === e));

  for (const el of stack) {
    if (related(el)) return {x, y};
    const tag = (el.tagName || '').toUpperCase();
    if (tag === 'CANVAS' || tag === 'HTML' || tag === 'BODY') continue;
    return null;
  }
  return {x, y};
}"""

SELECT_OPTION_JS = r"""([node, value]) => {
  const s = window.__jevFast ? window.__jevFast.nodes.get(node) : null;
  if (!s || s.tagName !== 'SELECT' || ![...s.options].some(o => o.value === value && !o.disabled)) return false;
  s.value = value;
  s.dispatchEvent(new Event('input', {bubbles: true}));
  s.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}"""

SETTLE_JS = r"""([node, cap]) => new Promise(resolve => {
  const started = performance.now();
  let stopped = false;
  const finish = () => { if (!stopped) { stopped = true; resolve(); } };
  setTimeout(finish, cap);
  requestAnimationFrame(() => {
    if (performance.now() - started >= cap) finish();
    else setTimeout(finish, 100);
  });
})"""


class Browser:
    def __init__(self):
        headful = os.environ.get("HEADFUL", "1") != "0"
        self._pw = sync_playwright().start()
        user_data_dir = os.environ.get("BROWSER_USER_DATA_DIR")
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if user_data_dir:
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=not headful,
                viewport={"width": 1280, "height": 800},
                args=launch_args,
            )
            self._browser = None
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        else:
            self._browser = self._pw.chromium.launch(
                headless=not headful,
                args=launch_args,
            )
            self._context = self._browser.new_context(viewport={"width": 1280, "height": 800})
            self._page = self._context.new_page()

        self._elements = {}
        self._setup_page(self._page)

        def _on_page(new_page):
            try:
                self._setup_page(new_page)
                self._page = new_page
            except Exception:
                pass

        self._context.on("page", _on_page)

    def _setup_page(self, page):
        def _handle_dialog(dialog):
            try:
                dialog.accept()
            except Exception:
                pass

        page.on("dialog", _handle_dialog)
        try:
            page.add_init_script("""
                window.addEventListener('DOMContentLoaded', () => {
                    document.querySelectorAll('a[target]').forEach(a => a.removeAttribute('target'));
                });
                document.addEventListener('click', e => {
                    const a = e.target.closest && e.target.closest('a');
                    if (a && a.target === '_blank') {
                        a.target = '_self';
                    }
                }, true);
            """)
        except Exception:
            pass

    def _ensure_page(self):
        try:
            if self._page and not self._page.is_closed():
                return self._page
        except Exception:
            pass
        if self._context and self._context.pages:
            alive = [p for p in self._context.pages if not p.is_closed()]
            if alive:
                self._page = alive[-1]
                return self._page
        return self._page

    @property
    def is_feed(self) -> bool:
        url = self._ensure_page().url.lower()
        return any(marker in url for marker in FEED_MARKERS)

    def _wait_for_content(self, timeout_ms: int = READY_TIMEOUT_MS) -> bool:
        p = self._ensure_page()
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if page_ready(p):
                return True
            p.wait_for_timeout(READY_POLL_MS)
        return False

    def goto(self, url: str):
        p = self._ensure_page()
        p.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._wait_for_content()

    def reload(self):
        p = self._ensure_page()
        p.reload(wait_until="domcontentloaded", timeout=30000)
        self._wait_for_content()

    @property
    def url(self) -> str:
        return self._ensure_page().url

    @property
    def title(self) -> str:
        return self._ensure_page().title()

    def page_text(self) -> str:
        return self._ensure_page().evaluate(
            f"document.body ? document.body.innerText.slice(0, {PAGE_TEXT_LIMIT}) : ''"
        )

    def snapshot(self, retries: int = 3) -> Dict[str, Any]:
        p = self._ensure_page()
        page = None
        for _ in range(max(1, retries)):
            try:
                page = p.evaluate(SNAPSHOT_JS)
            except Exception:
                page = None
            if page and (page.get("elements") or len(page.get("text") or "") > READY_TEXT_MIN):
                break
            p.wait_for_timeout(150)

        page = page or {}
        elements = []
        for a in page.get("elements") or []:
            ops = []
            if a.get("editable") or a.get("role") in ("combobox", "searchbox", "textbox") or "date" in (a.get("label") or "").lower():
                ops.append("TYPE_TEXT")
            ops.append("CLICK")
            el = {
                "index": a["index"],
                "node": a["node"],
                "role": a["role"],
                "label": a["label"],
                "operations": ops,
            }
            if a.get("select") is not None:
                el["select"] = a["select"]
            for key in ("value", "checked", "selected", "expanded", "isPassword", "href"):
                if key in a:
                    el[key] = a[key]
            if "inputType" in a:
                el["inputType"] = a["inputType"]
            elements.append(el)

        self._elements = {el["index"]: el for el in elements}
        return {
            "url": page.get("url") or self.url,
            "title": page.get("title") or "",
            "text": page.get("text") or "",
            "elements": elements,
            "scroll_y": page.get("scrollY", 0),
            "scroll_height": page.get("scrollHeight", 0),
            "client_height": page.get("clientHeight", 800),
            "is_scrollable": page.get("isScrollable", False),
            "at_bottom": page.get("atBottom", False),
            "scroll_percent": page.get("scrollPercent", 0),
        }

    def get_element(self, index: str) -> Optional[Dict[str, Any]]:
        return self._elements.get(index)

    def find_by_label(self, label: str, exclude: Optional[str] = None) -> Optional[str]:
        wanted = " ".join((label or "").lower().split())
        if not wanted:
            return None
        partial = None
        for index in sorted(self._elements, key=lambda i: int(i)):
            if index == exclude:
                continue
            have = " ".join((self._elements[index].get("label") or "").lower().split())
            if have == wanted:
                return index
            if partial is None and labels_overlap(wanted, have):
                partial = index
        return partial

    def screenshot(self, path: Optional[str] = None) -> Optional[str]:
        try:
            p = self._ensure_page()
            raw = p.screenshot(type="png", path=path) if path else p.screenshot(type="png")
            return f"data:image/png;base64,{base64.b64encode(raw).decode('utf-8')}"
        except Exception:
            return None

    def go_back(self) -> str:
        try:
            self._page.go_back(wait_until="domcontentloaded", timeout=4000)
            self._page.wait_for_timeout(300)
            return "went back to previous page"
        except Exception as e:
            return f"could not go back ({e})"

    def act(self, index: Optional[str], kind: str, text: Optional[str] = None) -> str:
        if kind == "BACK":
            return self.go_back()
        if kind == "WAIT":
            self._page.wait_for_timeout(500)
            return "waited"
        if kind in ("SCROLL", "SCROLL_TOP", "SCROLL_UP"):
            if self.is_feed:
                self._page.wait_for_timeout(200)
                return "no scroll available on feed"
            if kind in ("SCROLL_TOP", "SCROLL_UP"):
                delta = 0 if kind == "SCROLL_TOP" else -600
                self._page.mouse.move(640, 400)
                self._page.mouse.wheel(0, -99999 if kind == "SCROLL_TOP" else -600)
                self._page.wait_for_timeout(100)
                self._page.evaluate("""((delta) => {
                    if (delta === 0) {
                        window.scrollTo({top: 0, behavior: 'instant'});
                        const scrollables = document.querySelectorAll('main, article, [style*="overflow"], [class*="content"], mat-sidenav-content, .mat-drawer-content');
                        for (const el of scrollables) {
                            el.scrollTop = 0;
                        }
                    } else {
                        window.scrollBy({top: delta, behavior: 'instant'});
                        const scrollables = document.querySelectorAll('main, article, [style*="overflow"], [class*="content"], mat-sidenav-content, .mat-drawer-content');
                        for (const el of scrollables) {
                            if (el.scrollHeight > el.clientHeight) el.scrollTop += delta;
                        }
                    }
                })""", delta)
                self._page.wait_for_timeout(150)
                return "scrolled to top" if kind == "SCROLL_TOP" else "scrolled up"
            # Universal container scroll down
            self._page.mouse.move(640, 400)
            self._page.mouse.wheel(0, 600)
            self._page.wait_for_timeout(100)
            self._page.evaluate("""(() => {
                window.scrollBy({top: 600, behavior: 'instant'});
                const scrollables = document.querySelectorAll('main, article, [style*="overflow"], [class*="content"], mat-sidenav-content, .mat-drawer-content');
                for (const el of scrollables) {
                    if (el.scrollHeight > el.clientHeight) el.scrollTop += 600;
                }
            })()""")
            self._page.wait_for_timeout(100)
            return "scrolled"

        el = self._elements[index]
        if "select" in el:
            if not self._page.evaluate(SELECT_OPTION_JS, [el["node"], el["select"]]):
                raise RuntimeError(f"option for [{index}] is gone")
            self._page.wait_for_timeout(150)
            return f"selected option in [{index}]"

        lbl = (el.get("label") or "").lower()
        # Universal modal/dropdown overlay dismissal
        if "close dropdown overlay" in lbl or "backdrop" in lbl or "blank space" in lbl:
            try:
                self._page.keyboard.press("Escape")
                self._page.mouse.click(50, 50)
            except Exception:
                pass
            self._page.wait_for_timeout(200)
            return f"clicked [{index}] (dismissed overlay)"

        target = self._page.evaluate(CLICK_TARGET_JS, [el["node"], kind == "TYPE_TEXT"])
        if not target:
            # Check if covered by transient backdrop
            try:
                self._page.keyboard.press("Escape")
                self._page.wait_for_timeout(200)
                target = self._page.evaluate(CLICK_TARGET_JS, [el["node"], kind == "TYPE_TEXT"])
            except Exception:
                pass
            if not target:
                if kind == "CLICK":
                    try:
                        clicked = self._page.evaluate("""(node) => {
                            const domEl = window.__jevFast ? window.__jevFast.nodes.get(node) : null;
                            if (domEl) {
                                domEl.scrollIntoView({block: 'center', inline: 'center'});
                                domEl.click();
                                return true;
                            }
                            return false;
                        }""", el["node"])
                        if clicked:
                            self._page.wait_for_timeout(200)
                            lbl_lower = lbl.lower()
                            if any(k in lbl_lower for k in ("submit", "continue", "login", "confirm", "next", "sign in", "signin", "immediate", "overnight")):
                                try:
                                    self._page.wait_for_load_state("networkidle", timeout=6000)
                                except Exception:
                                    self._page.wait_for_timeout(2500)
                                self._wait_for_content()
                            return f"clicked [{index}]"
                    except Exception:
                        pass
                raise RuntimeError(f"element [{index}] is gone, covered, or disabled")

        p = self._ensure_page()
        p.mouse.click(target["x"], target["y"])
        if el.get("role") in ("option", "menuitem"):
            try:
                p.wait_for_timeout(250)
                p.keyboard.press("Escape")
                p.wait_for_timeout(250)
            except Exception:
                pass

        if kind == "TYPE_TEXT":
            try:
                self._page.evaluate("""(node) => {
                    const el = window.__jevFast ? window.__jevFast.nodes.get(node) : null;
                    if (el) {
                        const input = (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') ? el : el.querySelector('input, textarea');
                        if (input) {
                            input.removeAttribute('readonly');
                            input.focus();
                        }
                    }
                }""", el["node"])
            except Exception:
                pass
            is_date = "date" in lbl or el.get("inputType") == "date"
            if is_date:
                # CAMS may render this as either a native date input or a
                # date-mask textbox. Use the native ISO value when possible,
                # then fall back to the common CAMS display format.
                date_result = self._page.evaluate("""({node, text}) => {
                    const el = window.__jevFast ? window.__jevFast.nodes.get(node) : null;
                    const input = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')
                        ? el : el?.querySelector?.('input, textarea');
                    if (!input) return {ok: false, type: ''};
                    const match = String(text || '').match(/^(\\d{1,2})[-/.]([A-Za-z]{3,}|\\d{1,2})[-/.](\\d{2,4})$/);
                    let iso = '';
                    if (match) {
                        const day = match[1].padStart(2, '0');
                        const monthNames = {jan:'01',feb:'02',mar:'03',apr:'04',may:'05',jun:'06',jul:'07',aug:'08',sep:'09',oct:'10',nov:'11',dec:'12'};
                        const month = monthNames[String(match[2]).slice(0, 3).toLowerCase()] || String(match[2]).padStart(2, '0');
                        let year = String(match[3]);
                        if (year.length === 2) year = '20' + year;
                        iso = `${year}-${month}-${day}`;
                    }
                    input.removeAttribute('readonly');
                    input.focus();
                    if (input.type === 'date' && iso) {
                        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                        if (setter) setter.call(input, iso); else input.value = iso;
                    } else {
                        input.value = '';
                    }
                    input.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text || ''}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    return {ok: true, type: input.type || 'text', value: input.value || ''};
                }""", {"node": el["node"], "text": text or ""})
                if date_result and date_result.get("type") == "date" and date_result.get("value"):
                    self._settle_after_typing(el)
                    return f"typed date into [{index}]"
                # Date-mask fields generally accept slash-separated dates.
                self._page.keyboard.press("Control+a")
                self._page.keyboard.press("Backspace")
                date_text = text or ""
                try:
                    import datetime
                    date_text = datetime.datetime.strptime(date_text, "%d-%b-%Y").strftime("%d/%m/%Y")
                except ValueError:
                    pass
                self._page.keyboard.type(date_text, delay=25)
            else:
                self._page.keyboard.press("Control+a")
                self._page.keyboard.press("Backspace")
                self._page.keyboard.insert_text(text or "")
            try:
                self._page.evaluate("""(node) => {
                    const el = window.__jevFast ? window.__jevFast.nodes.get(node) : null;
                    if (el) {
                        const input = (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') ? el : el.querySelector('input, textarea');
                        if (input) {
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }
                }""", el["node"])
            except Exception:
                pass
            self._settle_after_typing(el)
            return f"typed into [{index}]"

        # Action handling for clicks
        old_url = self._page.url
        role = el.get("role", "")
        lbl_lower = lbl.lower()
        if any(k in lbl_lower for k in ("submit", "continue", "login", "confirm", "next", "sign in", "signin", "immediate", "overnight")):
            try:
                self._page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                self._page.wait_for_timeout(2500)
            self._wait_for_content()
        else:
            self._page.wait_for_timeout(200)

        if self._page.url != old_url or role == "link":
            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=1500)
            except Exception:
                pass

        return f"clicked [{index}]"

    def _settle_after_typing(self, el: Dict[str, Any]):
        role = (el or {}).get("role", "")
        lbl = ((el or {}).get("label") or "").lower()
        is_search = role in ("combobox", "searchbox") or any(k in lbl for k in ("search", "find", "symbol"))
        cap = 1500 if is_search else 500
        try:
            self._page.evaluate(SETTLE_JS, [(el or {}).get("node"), cap])
        except Exception:
            pass

    def close(self):
        try:
            if getattr(self, "_context", None):
                self._context.close()
            if getattr(self, "_browser", None):
                self._browser.close()
            self._pw.stop()
        except Exception:
            pass

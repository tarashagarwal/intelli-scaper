# extract_all_links.py
import asyncio
import re
from urllib.parse import urlsplit, urlunsplit, urljoin

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ------------- utils -------------
def normalize_url(base: str, u: str) -> str:
    try:
        absu = urljoin(base, u)
        parts = urlsplit(absu)
        # drop fragment, keep query
        parts = parts._replace(fragment="")
        # normalize path (remove duplicate slashes)
        path = re.sub(r"/{2,}", "/", parts.path) or "/"
        parts = parts._replace(path=path)
        return urlunsplit(parts)
    except Exception:
        return u

def same_domain(a: str, b: str) -> bool:
    try:
        ha = urlsplit(a).hostname or ""
        hb = urlsplit(b).hostname or ""
        ha = ha.lower().lstrip(".")
        hb = hb.lower().lstrip(".")
        return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)
    except Exception:
        return False

def has_hostname(u: str) -> bool:
    try:
        return bool(urlsplit(u).hostname)
    except Exception:
        return False

def in_base_path(base_url: str, u: str) -> bool:
    """Keep only URLs whose path is within the base_url's path (e.g., base='/blog' -> '/blog', '/blog/', '/blog/*')."""
    try:
        base_path = urlsplit(base_url).path or "/"
        if base_path == "/":
            return True  # no path restriction at root
        upath = urlsplit(u).path or "/"
        # match same path or any sub-path
        if upath == base_path:
            return True
        prefix = base_path if base_path.endswith("/") else base_path + "/"
        return upath.startswith(prefix)
    except Exception:
        return False

# ------------- JS hooks -------------
HOOK_HISTORY_JS = r"""
(() => {
  if (window.__navsHooked) return;
  window.__navsHooked = true;
  window.__navs = window.__navs || [];

  const pushState = history.pushState;
  const replaceState = history.replaceState;
  history.pushState = function(state, title, url) {
    try { if (url) window.__navs.push(new URL(url, location.href).href); } catch {}
    return pushState.apply(this, arguments);
  };
  history.replaceState = function(state, title, url) {
    try { if (url) window.__navs.push(new URL(url, location.href).href); } catch {}
    return replaceState.apply(this, arguments);
  };

  const _assign = window.location.assign.bind(window.location);
  window.location.assign = function(url) {
    try { if (url) window.__navs.push(new URL(url, location.href).href); } catch {}
    return _assign(url);
  };

  const hrefDesc = Object.getOwnPropertyDescriptor(Location.prototype, 'href');
  if (hrefDesc && hrefDesc.set) {
    Object.defineProperty(window.location, 'href', {
      configurable: true,
      enumerable: true,
      get: hrefDesc.get ? hrefDesc.get.bind(window.location) : () => location.toString(),
      set(v) {
        try { if (v) window.__navs.push(new URL(v, location.href).href); } catch {}
        return hrefDesc.set.call(window.location, v);
      }
    });
  }

  // Catch Turbo/Hotwire navigations if present
  document.addEventListener('turbo:visit', e => {
    try { if (e && e.detail && e.detail.url) window.__navs.push(new URL(e.detail.url, location.href).href); } catch {}
  }, true);
})();
"""

CSSPATH_JS = r"""
(el => {
  if (!(el instanceof Element)) return null;
  const esc = CSS && CSS.escape ? CSS.escape : (s => (s+'').replace(/([ #;?%&,.+*~\':"!^$[\]()=>|\/@])/g,'\\$1'));
  const parts = [];
  while (el && el.nodeType === 1 && parts.length < 20) {
    let selector = el.nodeName.toLowerCase();
    if (el.id) { selector += '#' + esc(el.id); parts.unshift(selector); break; }
    let sib = el, nth = 1;
    while (sib = sib.previousElementSibling) { if (sib.nodeName.toLowerCase() === selector) nth++; }
    selector += `:nth-of-type(${nth})`;
    parts.unshift(selector);
    el = el.parentElement;
  }
  return parts.join(' > ');
})
"""

# ------------- core extraction -------------
async def auto_scroll(page, max_steps=20, wait_ms=600):
    last_h = 0
    for _ in range(max_steps):
        h = await page.evaluate("document.body.scrollHeight")
        if h <= last_h:
            break
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(wait_ms)
        last_h = h

async def candidate_click_paths(page, limit=150):
    js = f"""
(() => {{
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const looksClickable = el => {{
    const s = getComputedStyle(el);
    return s.cursor === 'pointer' || el.tagName === 'BUTTON' || el.getAttribute('role') === 'link' || el.hasAttribute('onclick');
  }};
  const set = new Set();
  const nodes = Array.from(document.querySelectorAll("a[href], [role='link'], button, [onclick], [data-href], [data-url], [data-link]"));
  const pruned = nodes.filter(el => visible(el) && looksClickable(el));
  const paths = [];
  for (const el of pruned) {{
    try {{
      const p = ({CSSPATH_JS})(el);
      if (p && !set.has(p)) {{ set.add(p); paths.push(p); }}
      if (paths.length >= {limit}) break;
    }} catch (_) {{}}
  }}
  return paths;
}})()
"""
    return await page.evaluate(js)

async def click_probe(page, path, base_url, wait_ms, same_domain_only):
    """Click one element and capture resulting URL(s). Returns (set_of_urls, navigated_bool)."""
    out = set()
    navigated = False

    start_url = page.url
    try:
        el = await page.query_selector(path)
        if not el:
            return out, navigated

        # Try normal click first
        try:
            await el.click(timeout=1500)
        except PWTimeout:
            return out, navigated
        except Exception:
            # Fallback: dispatch a JS click (bubbling)
            try:
                await page.evaluate("(sel) => { const el = document.querySelector(sel); if (el) el.dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true})); }", path)
            except Exception:
                return out, navigated

        await page.wait_for_timeout(wait_ms)

        # Capture SPA navs recorded by our hooks
        try:
            navs = await page.evaluate("Array.isArray(window.__navs) ? window.__navs.slice() : []")
            for u in navs:
                nu = normalize_url(base_url, u)
                out.add(nu)
        except Exception:
            pass

        # Detect current URL change
        cur = page.url
        if cur and cur != start_url:
            out.add(normalize_url(base_url, cur))
            navigated = True
    finally:
        if navigated:
            try:
                await page.go_back(timeout=5000, wait_until="domcontentloaded")
                await page.wait_for_timeout(200)
            except Exception:
                pass

    # enforce same-domain (if requested) and path-scope
    if same_domain_only:
        out = {u for u in out if same_domain(u, base_url)}
    out = {u for u in out if in_base_path(base_url, u)}
    return out, navigated

async def get_all_links(
    url: str,
    max_clicks: int = 80,
    click_wait_ms: int = 1000,
    same_domain_only: bool = True,
    headless: bool = True,
    scroll_steps: int = 10
):
    results = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        # record navigations initiated by clicks (hard or SPA) and popups
        def add_url(u: str):
            if not u:
                return
            u = normalize_url(url, u)
            if (not same_domain_only or same_domain(u, url)) and in_base_path(url, u):
                results.add(u)

        page.on("request", lambda req: (req.is_navigation_request() and add_url(req.url)))
        page.on("framenavigated", lambda fr: add_url(fr.url))

        popups = []
        async def on_popup(p2):
            popups.append(p2)
            try:
                await p2.wait_for_load_state("domcontentloaded", timeout=4000)
            except Exception:
                pass
            add_url(p2.url)
            try:
                await p2.close()
            except Exception:
                pass
        page.on("popup", lambda p2: asyncio.create_task(on_popup(p2)))

        # Go & hook SPA nav
        await page.add_init_script(HOOK_HISTORY_JS)
        await page.goto(url, wait_until="networkidle")

        # Auto-scroll to reveal lazy content so clickable elements mount
        await auto_scroll(page, max_steps=scroll_steps)

        # Prepare click candidates and probe
        paths = await candidate_click_paths(page, limit=max_clicks * 2)

        clicks_done = 0
        seen_paths = set()
        for path in paths:
            if clicks_done >= max_clicks:
                break
            if path in seen_paths:
                continue
            seen_paths.add(path)
            urls_found, navigated = await click_probe(page, path, url, click_wait_ms, same_domain_only)
            # also enforce path-scope here for safety
            urls_found = {u for u in urls_found if in_base_path(url, u)}
            results |= urls_found
            clicks_done += 1
            await page.wait_for_timeout(150)

        await browser.close()

    # keep only URLs that actually have a domain/hostname and are in-base-path
    results = {u for u in results if has_hostname(u) and in_base_path(url, u)}
    return results

# ------------- "main" with in-code params (no CLI) -------------
async def _amain():
    # ---- tweak these defaults if you like ----
    url = "https://quill.co/blog"
    max_clicks = 120
    click_wait_ms = 10
    same_domain_only = True   # keep domain-limited
    headless = True           # set False to watch it work
    scroll_steps = 12
    # -----------------------------------------

    links = await get_all_links(
        url=url,
        max_clicks=max_clicks,
        click_wait_ms=click_wait_ms,
        same_domain_only=same_domain_only,
        headless=headless,
        scroll_steps=scroll_steps,
    )
    for u in sorted(links):
        print(u)

if __name__ == "__main__":
    asyncio.run(_amain())

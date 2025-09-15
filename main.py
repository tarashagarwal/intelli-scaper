# async_site_crawler.py
import asyncio
import json
import re
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from usp.tree import sitemap_tree_for_homepage
import html2text
import pdb

from hidden_links import get_all_links
import time
from playwright.async_api import Error as PWError
# ---------- config (keep params in code) ----------
DOMAIN = "interviewing.io"
START_PATH = f"{DOMAIN}"
LIMIT = 1000
CONCURRENCY = 50
HEADLESS = True
VERBOSE = True

# Only URLs whose path starts with any of these prefixes will be crawled/saved.
# Examples: ["/blog", "/blog/science"]. Leave [] to allow everything on the domain.
ALLOWED_PATH_PREFIXES = ["/mocks"]

# Cap how many candidate elements we try to click per page when probing JS-only nav
MAX_CLICK_PROBES_PER_PAGE = 30
CLICK_WAIT_MS = 1000  # wait up to N ms for a navigation after clicking

# Output
OUTPUT_JSON = "crawl_output.json"

# ---------- global data object (protected with a lock) ----------
RESULTS = []  # items: {"title": str, "type": str, "content": str, "url": str}

# ---------- in-page nav hook (installed before page scripts run) ----------
NAV_INJECT_JS = r"""
(() => {
  if (window.__NAV_LOGS__) return;
  const logs = [];
  const abs = (u) => {
    try { return new URL(u, location.href).href; } catch { return null; }
  };
  const record = (how, url) => {
    if (!url) return;
    const u = abs(url);
    if (u) logs.push({ how, url: u, ts: Date.now() });
  };

  // Expose a safe drain() to fetch and clear logs from Python
  Object.defineProperty(window, '__NAV_LOGS__', {
    value: {
      push: record,
      drain: () => { const out = logs.slice(); logs.length = 0; return out; }
    },
    configurable: false,
    enumerable: false,
    writable: false
  });

  // Hook programmatic nav APIs
  const _ps = history.pushState;
  history.pushState = function (st, ti, url) { if (url) record('history.pushState', url); return _ps.apply(this, arguments); };
  const _rs = history.replaceState;
  history.replaceState = function (st, ti, url) { if (url) record('history.replaceState', url); return _rs.apply(this, arguments); };

  const _assign = location.assign.bind(location);
  location.assign = (url) => { record('location.assign', url); _assign(url); };
  const _replace = location.replace.bind(location);
  location.replace = (url) => { record('location.replace', url); _replace(url); };

  const _open = window.open?.bind(window);
  if (_open) {
    window.open = (url, ...rest) => { if (url) record('window.open', url); return _open(url, ...rest); };
  }

  // If SPA changes the URL without full navigation
  window.addEventListener('popstate', () => record('event.popstate', location.href), true);
  window.addEventListener('hashchange', () => record('event.hashchange', location.href), true);

  // Log anchor clicks (even if prevented)
  window.addEventListener('click', (e) => {
    const a = e.target?.closest?.('a[href]');
    if (a && a.getAttribute('href')) record('anchor.click', a.href);
  }, true);
})();
"""

# ---------- helpers ----------


NAV_DESTROYED_SIGNS = (
    "Execution context was destroyed",
    "Target closed",  # window closed during nav
)

async def wait_until_stable(page, idle_ms=400, timeout_ms=15000):
    """Wait until the URL stops changing and the page is idle for a short period."""
    deadline = time.time() + timeout_ms / 1000.0
    last_url = page.url
    while time.time() < deadline:
        # wait for DOM to be ready/idle
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PWTimeout:
            # some sites never hit 'networkidle'; fall back to domcontentloaded
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except PWTimeout:
                pass
        await asyncio.sleep(idle_ms / 1000.0)
        if page.url == last_url:
            return
        last_url = page.url
    # give up: return anyway

async def safe_call(page, coro_factory, retries=3):
    """Run a page operation with retries if a navigation destroys the context."""
    last_err = None
    for _ in range(retries):
        try:
            return await coro_factory()
        except Exception as e:
            msg = str(e)
            if any(s in msg for s in NAV_DESTROYED_SIGNS):
                # navigation happened; wait for stability then retry
                await wait_until_stable(page)
                last_err = e
                continue
            raise
    # still failing; bubble last error
    raise last_err


def dbg(msg: str):
    if VERBOSE:
        print(msg, flush=True)

def to_filename(url: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]", "_", url)
    return (safe[:150] or "page") + ".md"

def same_domain(link: str, domain: str) -> bool:
    try:
        host = urlparse(link).netloc.lower()
        base = domain.lower().lstrip(".")
        return host == base or host.endswith("." + base)
    except Exception:
        return False

def path_allowed(url: str, allowed_prefixes: list[str]) -> bool:
    """Allow if list empty OR path begins with any prefix (prefixes must start with '/')."""
    if not allowed_prefixes:
        return True
    try:
        p = urlparse(url).path or "/"
        return any(p.startswith(pref) for pref in allowed_prefixes)
    except Exception:
        return False

def seed_urls_from_sitemap(domain: str, allowed_prefixes: list[str]) -> set[str]:
    """Seed with homepage and sitemap URLs filtered by ALLOWED_PATH_PREFIXES."""
    seeds = {f"https://{START_PATH}/"}  # always include startpage
    try:
        tree = sitemap_tree_for_homepage(f"https://{domain}/")
        for p in tree.all_pages():
            u = p.url
            if same_domain(u, domain) and path_allowed(u, allowed_prefixes):
                seeds.add(u)
    except Exception as e:
        dbg(f"[sitemap] could not fetch/parse sitemap for {domain}: {e}")
    return seeds

async def extract_meta_and_markdown(page):
    html = await page.content()
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = False
    markdown = h.handle(html)

    meta = await page.evaluate(
        """
        () => {
          const pick = (sel) => {
            const el = document.querySelector(sel);
            return el ? (el.getAttribute('content') ?? el.textContent ?? '').trim() : '';
          };
          // prefer canonical if present
          const canon = document.querySelector('link[rel="canonical"]')?.href || '';
          return {
            ogTitle: pick('meta[property="og:title"]') || pick('meta[name="og:title"]'),
            ogType:  pick('meta[property="og:type"]')  || pick('meta[name="og:type"]'),
            titleTag: (document.title || '').trim(),
            canonical: canon
          };
        }
        """
    )
    return markdown, meta

async def collect_static_links(page, domain: str) -> set[str]:
    links = await safe_call(page, lambda: page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)"))
    static_links = {u for u in links if isinstance(u, str) and u.startswith("http") and same_domain(u, domain)}
    out = set()
    for lnk in static_links or []:
        if isinstance(lnk, str) and lnk.startswith("http") and same_domain(lnk, domain):
            out.add(lnk)
    return out

async def collect_inline_click_urls(page) -> set[str]:
    inline_candidates = await safe_call(page, lambda: page.evaluate("""
    () => {
        const abs = (u) => { try { return new URL(u, location.href).href } catch { return '' } };
        const found = new Set();
        document.querySelectorAll('[data-href],[data-url]').forEach(el => {
        const u = el.getAttribute('data-href') || el.getAttribute('data-url');
        if (u) found.add(abs(u));
        });
        document.querySelectorAll('[onclick]').forEach(el => {
        const s = el.getAttribute('onclick') || '';
        const re = /(https?:\\/\\/[^'"\\s)]+|\\/[A-Za-z0-9_\\-\\/\\.?=&%#]+)/g;
        let m; while ((m = re.exec(s)) !== null) { const u = abs(m[1]); if (u) found.add(u); }
        });
        document.querySelectorAll('[role="link"],[role="button"]').forEach(el => {
        const u = el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('data-url');
        if (u) found.add(abs(u));
        });
        return Array.from(found);
    }
    """))
    inline_click_urls = {u for u in inline_candidates if u}
    return inline_click_urls

# async def drain_programmatic_nav(page) -> list[dict]:
#     """
#     Return and clear any logs captured by the injected nav hooks.
#     """
#     try:
#         logs = await page.evaluate("window.__NAV_LOGS__ ? window.__NAV_LOGS__.drain() : []")
#         return logs or []
#     except Exception:
#         return []

# async def probe_click_only_links(page, domain: str, max_clicks: int, wait_ms: int) -> set[str]:
#     """
#     Try to discover SPA or JS-attached click navigations by:
#       - clicking likely-interactive elements
#       - capturing actual navigations (full or pushState)
#     """
#     # Candidate elements: links with void/empty hrefs, buttons, cards, onclick/data-href/url
#     handles = await page.query_selector_all(
#         "a[href='javascript:void(0)'], a[href='#'], a:not([href]), "
#         "[onclick], [data-href], [data-url], "
#         "[role='link'], [role='button'], button"
#     )
#     handles = handles[:max_clicks]
#     origin = page.url
#     discovered: set[str] = set()

#     for el in handles:
#         try:
#             await el.scroll_into_view_if_needed(timeout=500)
#             await el.hover(timeout=500)
#             await el.click(timeout=500, force=True, no_wait_after=True)
#             # If full navigation happens, capture then go back
#             try:
#                 await page.wait_for_navigation(timeout=wait_ms)
#                 new_url = page.url
#                 if new_url and new_url != origin and same_domain(new_url, domain):
#                     discovered.add(new_url)
#                 await page.goto(origin, wait_until="domcontentloaded")
#             except PWTimeout:
#                 pass
#         except Exception:
#             continue

#     # Pull SPA pushState/open captures
#     bucket = await drain_programmatic_nav(page)
#     for item in bucket:
#         u = item.get("url")
#         if isinstance(u, str) and u.startswith("http") and same_domain(u, domain):
#             discovered.add(u)
    
#     #pdb.set_trace()

#     return discovered

# ---------- core page handler ----------
async def scrape_one_page(context, url: str, domain: str, allowed_prefixes: list[str], results_lock: asyncio.Lock):
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # let SPAs settle (they often pushState/redirect after DOMContentLoaded)
        await wait_until_stable(page)

        final_url = page.url

        # ---- gather content/meta (with retry)
        html = await safe_call(page, page.content)
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = False
        markdown = h.handle(html)

        meta = await safe_call(page, lambda: page.evaluate("""
            () => {
            const pick = (sel) => {
                const el = document.querySelector(sel);
                return el ? (el.getAttribute('content') ?? el.textContent ?? '').trim() : '';
            };
            const canon = document.querySelector('link[rel="canonical"]')?.href || '';
            return {
                ogTitle: pick('meta[property="og:title"]') || pick('meta[name="og:title"]'),
                ogType:  pick('meta[property="og:type"]')  || pick('meta[name="og:type"]'),
                titleTag: (document.title || '').trim(),
                canonical: canon
            };
            }
        """))

        parsed = urlparse(final_url)
        first_seg = (parsed.path.split("/")[1] if parsed.path and parsed.path != "/" else "") or "website"
        page_type = first_seg or meta.get("ogType") or "website"
        title = meta.get("ogTitle") or meta.get("titleTag") or ""
        canonical = meta.get("canonical") or final_url

        # ---- store ONLY if path matches allowed prefixes
        if path_allowed(final_url, allowed_prefixes):
            async with results_lock:
                result = {
                        "title": title,
                        "type": page_type,
                        "content": markdown,
                        "url": canonical,
                    }
                if result not in RESULTS:
                    RESULTS.append(result)

        if VERBOSE:
            dbg(f"[saved] {final_url}  (title='{title[:80]}', type='{page_type}')")

        # ---- discover links
        static_links = await collect_static_links(page, domain)
        inline_click_urls = await collect_inline_click_urls(page)
        hidden_links = await get_all_links(
            url=url,
            max_clicks=120,
            click_wait_ms=200,
            same_domain_only=True,
            headless=True,
            scroll_steps=12
        )

        # click_only_urls = await probe_click_only_links(
        #     page, domain=domain, max_clicks=MAX_CLICK_PROBES_PER_PAGE, wait_ms=CLICK_WAIT_MS
        # )

        all_found = static_links.union(inline_click_urls).union(hidden_links)
        
        if VERBOSE:
            dbg(f"[links] static={len(static_links)} inline={len(inline_click_urls)} hidden_links={len(hidden_links)} on {final_url}")

        # Deduplicate, same-domain, and restrict to allowed prefixes for enqueueing
        filtered = {
            u for u in all_found
            if isinstance(u, str) and u.startswith("http")
            and same_domain(u, domain)
            and path_allowed(u, allowed_prefixes)
        }
        return final_url, filtered

    except Exception as e:
        dbg(f"[error] {url} -> {e}")
        return url, set()
    finally:
        await page.close()

# ---------- worker pool using a Queue ----------
async def crawl_domain(domain: str, limit: int = 50, concurrency: int = 5, allowed_prefixes: list[str] | None = None):
    """
    Parallel crawler:
      - installs nav hooks (before any page script)
      - seeds from sitemap + homepage (homepage always included to discover deeper links)
      - visits/enqueues ONLY URLs that match allowed_prefixes (unless prefixes empty)
      - respects a page 'limit'
      - stores each matching page result in the global RESULTS
      - detects click-only navs (onclick / pushState / JS listeners) and enqueues them too
    """
    allowed_prefixes = allowed_prefixes or []

    # seeds (homepage + sitemap-filtered)
    seeds = seed_urls_from_sitemap(domain, allowed_prefixes)
    seeds.add(f"https://{domain}/")  # ensure homepage

    queue: asyncio.Queue[str] = asyncio.Queue()
    enqueued = set()
    visited = set()
    results_lock = asyncio.Lock()
    visited_lock = asyncio.Lock()

    # enqueue seeds (respect allowed prefixes *except* homepage)
    for u in seeds:
        if same_domain(u, domain) and (path_allowed(u, allowed_prefixes) or urlparse(u).path in ("", "/")):
            queue.put_nowait(u)
            enqueued.add(u)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (compatible; MyCrawler/1.0; +https://example.com/bot)"
        )
        # Install nav hooks for all pages BEFORE any page script runs
        await context.add_init_script(NAV_INJECT_JS)

        async def worker(worker_id: int):
            while True:
                try:
                    url = await asyncio.wait_for(queue.get(), timeout=3.0)
                except asyncio.TimeoutError:
                    if queue.empty():
                        return
                    continue

                # limit/visited gate
                async with visited_lock:
                    if len(visited) >= limit:
                        queue.task_done()
                        return
                    if url in visited:
                        queue.task_done()
                        continue
                    visited.add(url)

                if VERBOSE:
                    dbg(f"[worker {worker_id}] visiting: {url}")

                final_url, links = await scrape_one_page(context, url, domain, allowed_prefixes, results_lock)

                # >>> REPLACE your per-link lock loop with the batched version here <<<
                to_add = []
                async with visited_lock:
                    if len(visited) < limit:
                        for lnk in links:
                            if len(visited) >= limit:
                                break
                            if lnk not in visited and lnk not in enqueued and path_allowed(u, allowed_prefixes):
                                enqueued.add(lnk)
                                to_add.append(lnk)

                for lnk in to_add:
                    queue.put_nowait(lnk)

                if VERBOSE and to_add:
                    dbg(f"[worker {worker_id}] enqueued {len(to_add)} new links from {final_url}")

                queue.task_done()



        workers = [asyncio.create_task(worker(i)) for i in range(concurrency)]

        await queue.join()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        await context.close()
        await browser.close()

    print(f"✅ visited {len(visited)} pages for {domain}")

# ---------- entry point ----------
if __name__ == "__main__":
    asyncio.run(
        crawl_domain(
            DOMAIN,
            limit=LIMIT,
            concurrency=CONCURRENCY,
            allowed_prefixes=ALLOWED_PATH_PREFIXES,
        )
    )

    seen = set()
    unique = []
    for item in RESULTS:
        # Prefer source_url, fallback to url, else skip
        key = item.get("source_url") or item.get("url")
        if not key:   # if neither present, skip this item
            continue
        if key not in seen:
            seen.add(key)
            unique.append(item)

    RESULTS = unique

    # Save JSON
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    print(f"\nSaved {len(RESULTS)} records to {OUTPUT_JSON}")

# crawler.py
"""
Reusable async crawler module.

- Preserves your existing crawl logic (sitemap seeds, same-domain filter,
  allowed path prefixes, optional hidden-links discovery, Playwright async).
- Exposes a Crawler class that can be started/stopped programmatically.
- Streams results to disk periodically (NDJSON + rolling JSON snapshots)
  under: output/<domain>/
- Keeps memory low by flushing and clearing in-memory buffers.
"""

from __future__ import annotations
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Iterable, Optional, Set, Dict, Any
from urllib.parse import urlparse

# --- optional deps (graceful degradation) ---
try:
    from usp.tree import sitemap_tree_for_homepage  # python-usp
except Exception:
    sitemap_tree_for_homepage = None  # sitemap fallback

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    from playwright.async_api import Error as PWError
except Exception as e:
    raise RuntimeError(
        "Playwright is required. Install and run: "
        "pip install playwright && python -m playwright install"
    ) from e

try:
    import html2text
except Exception as e:
    raise RuntimeError("html2text is required: pip install html2text") from e

# Hidden-links clicker (optional; only used when quick_mode=False)
try:
    from hidden_links import get_all_links  # your module
except Exception:
    get_all_links = None

# ---------------- config & datatypes ----------------

DEFAULT_DOMAIN = "quill.co"
DEFAULT_LIMIT = 1000
DEFAULT_CONCURRENCY = 5
DEFAULT_HEADLESS = True
DEFAULT_VERBOSE = True
DEFAULT_QUICK_MODE = True
DEFAULT_ALLOWED_PREFIXES: list[str] = []  # empty => allow all
DEFAULT_CLICK_WAIT_MS = 1000
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_FLUSH_ITEMS = 50        # flush after N new pages
DEFAULT_FLUSH_SECONDS = 10.0    # and/or every T seconds

@dataclass
class CrawlerConfig:
    domain: str = DEFAULT_DOMAIN
    # Optional “start path” (e.g. "/blog"); if empty, homepage is used
    start_path: str = ""
    limit: int = DEFAULT_LIMIT
    concurrency: int = DEFAULT_CONCURRENCY
    headless: bool = DEFAULT_HEADLESS
    verbose: bool = DEFAULT_VERBOSE
    quick_mode: bool = DEFAULT_QUICK_MODE
    allowed_prefixes: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_PREFIXES))
    click_wait_ms: int = DEFAULT_CLICK_WAIT_MS
    output_dir: str = DEFAULT_OUTPUT_DIR
    flush_every_items: int = DEFAULT_FLUSH_ITEMS
    flush_every_seconds: float = DEFAULT_FLUSH_SECONDS

@dataclass
class CrawlStats:
    visited: int = 0
    enqueued: int = 0
    saved: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    last_flush_at: float = field(default_factory=time.time)

# ---------------- utilities (ported from your file) ----------------

def dbg(msg: str, verbose: bool):
    if verbose:
        print(msg, flush=True)

def same_domain(link: str, domain: str) -> bool:
    try:
        host = urlparse(link).netloc.lower()
        base = domain.lower().lstrip(".")
        return host == base or host.endswith("." + base)
    except Exception:
        return False

def path_allowed(url: str, allowed_prefixes: list[str]) -> bool:
    if not allowed_prefixes:
        return True
    try:
        p = urlparse(url).path or "/"
        return any(p.startswith(pref) for pref in allowed_prefixes)
    except Exception:
        return False

async def wait_until_stable(page, idle_ms=400, timeout_ms=15000):
    deadline = time.time() + timeout_ms / 1000.0
    last_url = page.url
    while time.time() < deadline:
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PWTimeout:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except PWTimeout:
                pass
        await asyncio.sleep(idle_ms / 1000.0)
        if page.url == last_url:
            return
        last_url = page.url

async def safe_call(page, coro_factory, retries=3):
    sigs = ("Execution context was destroyed", "Target closed")
    last_err = None
    for _ in range(retries):
        try:
            return await coro_factory()
        except Exception as e:
            if any(s in str(e) for s in sigs):
                await wait_until_stable(page)
                last_err = e
                continue
            raise
    raise last_err

async def collect_static_links(page, domain: str) -> Set[str]:
    links = await safe_call(page, lambda: page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)"))
    return {
        u for u in links
        if isinstance(u, str) and u.startswith("http") and same_domain(u, domain)
    }

async def collect_inline_click_urls(page) -> Set[str]:
    # NOTE: mirrors your inline discovery for data-href / onclick, etc.
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
    return {u for u in inline_candidates if u}

def ensure_domain_dir(out_root: str, domain: str) -> str:
    folder = os.path.join(out_root, domain)
    os.makedirs(folder, exist_ok=True)
    return folder

# ---------------- result writer (flush-to-disk) ----------------

class ResultWriter:
    def __init__(self, out_root: str, domain: str, flush_every_items: int, flush_every_seconds: float, verbose: bool):
        self.folder = ensure_domain_dir(out_root, domain)
        self.ndjson_path = os.path.join(self.folder, "pages.ndjson")
        self.snapshot_path = os.path.join(self.folder, "pages.json")
        self.buffer: list[dict] = []
        self.last_flush = time.time()
        self.flush_every_items = max(1, int(flush_every_items))
        self.flush_every_seconds = float(flush_every_seconds)
        self.verbose = verbose
        # create/append file
        if not os.path.exists(self.ndjson_path):
            with open(self.ndjson_path, "w", encoding="utf-8") as f:
                pass

    def add(self, item: dict):
        self.buffer.append(item)
        if self.should_flush():
            self.flush()

    def should_flush(self) -> bool:
        elapsed = time.time() - self.last_flush
        return len(self.buffer) >= self.flush_every_items or elapsed >= self.flush_every_seconds

    def flush(self):
        if not self.buffer:
            return
        # append to NDJSON
        with open(self.ndjson_path, "a", encoding="utf-8") as f:
            for row in self.buffer:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        # write snapshot JSON (all data we have so far on disk)
        # To keep it cheap, we just rewrite buffer's append portion into a rolling snapshot.
        # If the file exists, load quickly (best-effort), append, write back.
        try:
            existing = []
            if os.path.exists(self.snapshot_path):
                with open(self.snapshot_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
        except Exception:
            existing = []
        existing.extend(self.buffer)
        with open(self.snapshot_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        self.buffer.clear()
        self.last_flush = time.time()
        dbg(f"[flush] wrote snapshot + {self.ndjson_path}", self.verbose)

# ---------------- crawler core ----------------

class Crawler:
    def __init__(self, config: CrawlerConfig, log: Optional[Callable[[str], None]] = None):
        self.cfg = config
        self.log = log or (lambda m: dbg(m, self.cfg.verbose))
        self._stop = asyncio.Event()
        self.stats = CrawlStats()
        self._visited: Set[str] = set()
        self._enqueued: Set[str] = set()

    def request_stop(self):
        self.log("[stop] requested")
        self._stop.set()

    def _log(self, msg: str):
        self.log(msg)

    async def _seed_urls(self) -> Set[str]:
        seeds: Set[str] = set()
        home = f"https://{self.cfg.domain}/"
        if self.cfg.start_path:
            sp = self.cfg.start_path
            if sp.startswith("/"):
                seeds.add(f"https://{self.cfg.domain}{sp}")
            else:
                # allow full URL too
                if sp.startswith("http"):
                    seeds.add(sp)
                else:
                    seeds.add(home + sp.lstrip("/"))
        else:
            seeds.add(home)

        # sitemap
        if sitemap_tree_for_homepage:
            try:
                tree = sitemap_tree_for_homepage(home)
                for p in tree.all_pages():
                    u = p.url
                    if same_domain(u, self.cfg.domain) and path_allowed(u, self.cfg.allowed_prefixes):
                        seeds.add(u)
            except Exception as e:
                self._log(f"[sitemap] skip ({e})")
        else:
            self._log("[sitemap] python-usp not available; continuing without sitemap seeding")

        return seeds

    async def _scrape_one(self, context, url: str, writer: ResultWriter) -> tuple[str, Set[str]]:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await wait_until_stable(page)

            final_url = page.url

            # content -> markdown
            html = await safe_call(page, page.content)
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = False
            markdown = h.handle(html)

            # meta
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
            title = (meta.get("ogTitle") or meta.get("titleTag") or "").strip()
            canonical = meta.get("canonical") or final_url

            if path_allowed(final_url, self.cfg.allowed_prefixes):
                item = {
                    "title": title,
                    "type": page_type,
                    "content": markdown,
                    "url": canonical,
                }
                writer.add(item)
                self.stats.saved += 1
                self._log(f"[saved] {final_url} (title='{title[:80]}', type='{page_type}')")

            # discover links
            static_links = await collect_static_links(page, self.cfg.domain)
            inline_click_urls = await collect_inline_click_urls(page)
            hidden_links: Set[str] = set()
            if not self.cfg.quick_mode and get_all_links:
                try:
                    hidden_links = await get_all_links(
                        url=final_url,
                        max_clicks=120,
                        click_wait_ms=200,
                        same_domain_only=True,
                        headless=self.cfg.headless,
                        scroll_steps=12
                    )
                except Exception as e:
                    self._log(f"[hidden_links] error: {e}")

            all_found = static_links.union(inline_click_urls).union(hidden_links)
            filtered = {
                u for u in all_found
                if isinstance(u, str) and u.startswith("http")
                and same_domain(u, self.cfg.domain)
                and path_allowed(u, self.cfg.allowed_prefixes)
            }
            self._log(f"[links] static={len(static_links)} inline={len(inline_click_urls)} hidden={len(hidden_links)} on {final_url}")
            return final_url, filtered

        except Exception as e:
            self._log(f"[error] {url} -> {e}")
            return url, set()
        finally:
            await page.close()

    async def run(self) -> CrawlStats:
        seeds = await self._seed_urls()
        q: asyncio.Queue[str] = asyncio.Queue()
        writer = ResultWriter(
            out_root=self.cfg.output_dir,
            domain=self.cfg.domain,
            flush_every_items=self.cfg.flush_every_items,
            flush_every_seconds=self.cfg.flush_every_seconds,
            verbose=self.cfg.verbose
        )

        for u in seeds:
            # homepage always allowed; otherwise respect prefixes
            if same_domain(u, self.cfg.domain) and (path_allowed(u, self.cfg.allowed_prefixes) or urlparse(u).path in ("", "/")):
                q.put_nowait(u)
                self._enqueued.add(u)
        self.stats.enqueued = len(self._enqueued)
        self._log(f"[seed] {self.stats.enqueued} URLs")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.cfg.headless)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (compatible; MyCrawler/1.0; +https://example.com/bot)"
            )

            async def worker(wid: int):
                while not self._stop.is_set():
                    try:
                        url = await asyncio.wait_for(q.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        if q.empty():
                            return
                        continue

                    if self._stop.is_set():
                        q.task_done()
                        return

                    if len(self._visited) >= self.cfg.limit:
                        q.task_done()
                        return

                    if url in self._visited:
                        q.task_done()
                        continue

                    self._visited.add(url)
                    self.stats.visited = len(self._visited)
                    self._log(f"[worker {wid}] visiting: {url}")

                    _, links = await self._scrape_one(context, url, writer)

                    # Batch enqueue under lock (single-threaded here)
                    to_add = []
                    if len(self._visited) < self.cfg.limit:
                        for lnk in links:
                            if len(self._visited) >= self.cfg.limit:
                                break
                            if lnk not in self._visited and lnk not in self._enqueued and path_allowed(lnk, self.cfg.allowed_prefixes):
                                self._enqueued.add(lnk)
                                to_add.append(lnk)

                    for lnk in to_add:
                        q.put_nowait(lnk)

                    self.stats.enqueued = len(self._enqueued)
                    if to_add:
                        self._log(f"[worker {wid}] enqueued {len(to_add)} new links")
                    q.task_done()

            workers = [asyncio.create_task(worker(i)) for i in range(self.cfg.concurrency)]
            await q.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await context.close()
            await browser.close()

        # final flush
        writer.flush()
        self.stats.finished_at = time.time()
        return self.stats

# -------------- convenient runner for non-async callers --------------

def run_crawl(config: CrawlerConfig, log: Optional[Callable[[str], None]] = None, stop_event: Optional[asyncio.Event] = None) -> CrawlStats:
    """
    Synchronous entry to run the crawler (starts its own event loop).
    """
    async def _main():
        crawler = Crawler(config, log=log)
        if stop_event:
            # if someone sets stop_event, propagate to crawler
            async def wait_stop():
                await stop_event.wait()
                crawler.request_stop()
            asyncio.create_task(wait_stop())
        return await crawler.run()

    return asyncio.run(_main())

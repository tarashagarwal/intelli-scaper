# async_site_crawler.py
import asyncio
import json
import os
import re
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from usp.tree import sitemap_tree_for_homepage
import html2text

# ---------- global data object (thread-safe via lock) ----------
RESULTS = []  # each item: {"title": str, "type": str, "content": str, "url": str}

# ---------- helpers ----------
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

def seed_urls_from_sitemap(domain: str) -> set[str]:
    seeds = {f"https://{domain}/"}  # always include homepage
    try:
        tree = sitemap_tree_for_homepage(f"https://{domain}/")
        for p in tree.all_pages():
            seeds.add(p.url)
    except Exception as e:
        print(f"[sitemap] could not fetch/parse sitemap for {domain}: {e}")
    return seeds

# ---------- core page handler ----------
async def scrape_one_page(context, url: str, domain: str, results_lock: asyncio.Lock):
    """
    Loads 'url', waits for JS, extracts:
      - title (og:title -> <title>)
      - type (og:type -> 'website')
      - content (markdown of fully-rendered HTML)
      - url (final URL after redirects)
    Returns: (final_url, links_found_on_page)
    Also appends the JSON record to global RESULTS.
    """
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
        final_url = page.url  # handles redirects

        # -- get fully rendered HTML --
        html = await page.content()

        # -- convert to Markdown --
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = False
        markdown = h.handle(html)

        # -- collect OG/meta and title fallbacks in the page context --
        meta = await page.evaluate(
            """
            () => {
              const pick = (sel) => {
                const el = document.querySelector(sel);
                return el ? (el.getAttribute('content') ?? el.textContent ?? '').trim() : '';
              };
              return {
                ogTitle: pick('meta[property="og:title"]'),
                ogType:  pick('meta[property="og:type"]'),
                titleTag: (document.title || '').trim(),
              };
            }
            """
        )

        title = meta.get("ogTitle") or meta.get("titleTag") or ""
        page_type = final_url.split("/")[3] or "website"

        # -- append to global results (protected by lock) --
        async with results_lock:
            RESULTS.append(
                {
                    "title": title,
                    "type": page_type,
                    "content": markdown,  # single markdown string
                    "url": final_url,
                }
            )

        # -- collect links directly via DOM (no BeautifulSoup) --
        links = await page.eval_on_selector_all("a", "els => els.map(e => e.href)")
        # dedupe & keep within domain
        links = {lnk for lnk in links if isinstance(lnk, str) and lnk.startswith("http") and same_domain(lnk, domain)}
        return final_url, links

    except Exception as e:
        print(f"[error] {url} -> {e}")
        return url, set()
    finally:
        await page.close()

# ---------- worker pool using a Queue ----------
async def crawl_domain(domain: str, limit: int = 50, concurrency: int = 5):
    """
    Parallel crawler:
      - seeds from sitemap + homepage
      - keeps visited set
      - respects a page 'limit'
      - stores each page result in the global RESULTS
    """
    seeds = seed_urls_from_sitemap(domain)
    queue: asyncio.Queue[str] = asyncio.Queue()
    enqueued = set()
    visited = set()
    results_lock = asyncio.Lock()
    visited_lock = asyncio.Lock()

    # enqueue seeds
    for u in seeds:
        if same_domain(u, domain):
            queue.put_nowait(u)
            enqueued.add(u)

    async with async_playwright() as p:
        # one shared context for lighter resource usage
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (compatible; MyCrawler/1.0; +https://example.com/bot)"
        )

        async def worker(worker_id: int):
            while True:
                try:
                    url = await asyncio.wait_for(queue.get(), timeout=3.0)
                except asyncio.TimeoutError:
                    # no new work; worker can exit if queue really quiet
                    if queue.empty():
                        return
                    continue

                # check limit / visited atomically
                async with visited_lock:
                    if len(visited) >= limit:
                        queue.task_done()
                        return
                    if url in visited:
                        queue.task_done()
                        continue
                    visited.add(url)

                final_url, links = await scrape_one_page(context, url, domain, results_lock)

                # enqueue discovered links
                for lnk in links:
                    async with visited_lock:
                        if len(visited) >= limit:
                            break
                        if lnk not in visited and lnk not in enqueued:
                            queue.put_nowait(lnk)
                            enqueued.add(lnk)

                queue.task_done()

        # spin up workers
        workers = [asyncio.create_task(worker(i)) for i in range(concurrency)]

        # wait for all work to finish or limit reached
        await queue.join()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        await context.close()
        await browser.close()

    print(f"✅ visited {len(visited)} pages for {domain}")

# ---------- entry point ----------
if __name__ == "__main__":
    # example usage
    # change domain/limit/concurrency to taste
    domain = "nilmamano.com"
    limit = 1000
    concurrency = 10

    asyncio.run(crawl_domain(domain, limit=limit, concurrency=concurrency))

    # final JSON output (global RESULTS)
    # you can also dump this to a file if you prefer
    print(json.dumps(RESULTS, ensure_ascii=False, indent=2))

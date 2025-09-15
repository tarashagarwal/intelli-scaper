from playwright.sync_api import sync_playwright
from usp.tree import sitemap_tree_for_homepage
import html2text

def crawl_site(domain, limit=100):
    print("=" * 60)
    print(f"Crawling {domain}")

    not_visited = set()
    visited = set()
    not_visited.add(f'https://{domain}/')

    # Step 1: Load sitemap and seed URLs
    try:
        tree = sitemap_tree_for_homepage(f'https://{domain}/')
        for page in tree.all_pages():
            not_visited.add(page.url)
    except Exception as e:
        print(f"Could not fetch sitemap for {domain}: {e}")

    # Step 2: Use Playwright for crawling
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        while not_visited and len(visited) < limit:
            url = not_visited.pop()
            if url in visited:
                continue

            print(f"\nVisiting: {url}")
            try:
                page.goto(url, wait_until="networkidle")
                html = page.content()
                visited.add(url)

                # Convert HTML → Markdown
                h = html2text.HTML2Text()
                h.ignore_links = False
                h.ignore_images = False
                markdown = h.handle(html)
                print(f"--- Markdown snippet ---\n{markdown[:300]}\n")

                # Extract links directly via Playwright
                links = page.eval_on_selector_all("a", "els => els.map(e => e.href)")
                links = list(set(links))  # Deduplicate
                print(f"Found {len(links)} links on {url}")
                print(f"--- Links snippet ---\n{links[:5]}\n")
                for link in links:
                    if link.startswith("http") and domain in link:
                        if link not in visited:
                            not_visited.add(link)

            except Exception as e:
                print(f"Error visiting {url}: {e}")

        browser.close()

    print(f"\n✅ Crawled {len(visited)} pages from {domain}")
    return visited


if __name__ == "__main__":
    sites = [
        "quill.co"
    ]

    for site in sites:
        crawl_site(site, limit=100)  # limit per site for demo

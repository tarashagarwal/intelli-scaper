from robots_parser import RobotsHelper

def test_site(domain):
    print("=" * 60)
    print(f"Testing {domain}")
    try:
        rh = RobotsHelper(domain, agent="MyBot")

        print("Robots.txt URL:", rh.robots_url)
        sitemaps = rh.get_sitemaps()
        print("Sitemaps found:")
        if sitemaps:
            for sm in sitemaps:
                print("   ", sm)
        else:
            print("   None")

        # Test some URLs
        test_urls = [
            f"https://{domain}/",
            f"https://{domain}/admin/",
            f"https://{domain}/robots.txt"
        ]
        for url in test_urls:
            allowed = rh.can_fetch(url)
            print(f"  {url} -> {'Allowed' if allowed else 'Disallowed'}")

    except Exception as e:
        print(f"Error while testing {domain}: {e}")


if __name__ == "__main__":
    for site in ["interviewing.io", "reddit.com", "quora.com"]:
        test_site(site)

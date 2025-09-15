import requests
from urllib.parse import urlparse, urljoin
from robotexclusionrulesparser import RobotExclusionRulesParser


class RobotsHelper:
    def __init__(self, domain_or_url, agent="*"):
        """
        Initialize with either:
          - a domain (example.com, https://example.com)
          - OR a full robots.txt URL (https://example.com/robots.txt)

        Automatically fetches and parses robots.txt
        """
        if not domain_or_url.startswith("http"):
            domain_or_url = "https://" + domain_or_url
        parsed = urlparse(domain_or_url)

        # If input already ends with robots.txt, use directly
        if domain_or_url.endswith("/robots.txt"):
            self.robots_url = domain_or_url
            self.domain = f"{parsed.scheme}://{parsed.netloc}"
        else:
            self.domain = f"{parsed.scheme}://{parsed.netloc}"
            self.robots_url = urljoin(self.domain, "/robots.txt")

        self.agent = agent

        # Fetch and parse robots.txt
        self.rerp, self.robots_txt = self._load_robots()

        # Extract sitemaps
        self.sitemaps = self._extract_sitemaps()

    def _load_robots(self):
        headers = {"User-Agent": f"Mozilla/5.0 (compatible; {self.agent}/1.0)"}
        res = requests.get(self.robots_url, headers=headers, timeout=10)

        if res.status_code != 200:
            raise ValueError(f"Could not fetch robots.txt: {self.robots_url} ({res.status_code})")

        robots_txt = res.text.strip()
        if not robots_txt:
            raise ValueError(f"robots.txt at {self.robots_url} is empty")

        rerp = RobotExclusionRulesParser()
        rerp.parse(robots_txt)
        return rerp, robots_txt

    def _extract_sitemaps(self):
        """Extract Sitemap URLs from robots.txt content"""
        sitemaps = set()
        for line in self.robots_txt.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemaps.add(line.split(":", 1)[1].strip())
        return list(sitemaps)

    def can_fetch(self, url, agent=None):
        """Check if a URL is allowed for a given agent"""
        agent = agent or self.agent
        return self.rerp.is_allowed(agent, url)

    def is_disallowed(self, url, agent=None):
        """Check if a URL is disallowed for a given agent"""
        return not self.can_fetch(url, agent)

    def get_sitemaps(self):
        """Return sitemap URLs"""
        return self.sitemaps


# ---------------- Example usage ----------------
if __name__ == "__main__":
    # Case 1: Just give a domain
    rh1 = RobotsHelper("https://interviewing.io")
    print("Robots.txt URL:", rh1.robots_url)
    print("Sitemaps:", rh1.get_sitemaps())

    # Case 2: Explicit robots.txt
    rh2 = RobotsHelper("https://interviewing.io/robots.txt")
    print("Robots.txt URL:", rh2.robots_url)
    print("Sitemaps:", rh2.get_sitemaps())

    test_url = "https://interviewing.io/coderp/"
    print(f"Allowed? {rh1.can_fetch(test_url)}")

# intelli-scaper
We need a scapper that waits for the page to load ... sometime on a web page the js loads and then paints the page with data later.. so we were planing to use scappy which provides advanced options but it does not wait for the page to load.
So intead we will use 

What is Scrapy-Splash?

Scrapy = A very fast web scraper framework.

Splash = A lightweight headless browser (built on top of WebKit) that can render JavaScript and then give the result to Scrapy.

Scrapy-Splash = A connector that lets Scrapy talk to Splash.

👉 Together, they let you scrape JavaScript-heavy websites (the ones that don’t show full content in plain HTML).

Why use Scrapy-Splash instead of Selenium?

Faster than Selenium because Splash is made for scraping.

Integrates easily with Scrapy pipelines, middlewares, and scheduling.

Headless by default (no heavy Chrome GUI).

Can also execute Lua scripts inside Splash to control page (wait, scroll, click).




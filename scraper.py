"""
Pingo Doce price scraper — starter template.

WHAT THIS DOES
  - Checks robots.txt before touching any URL (and refuses to fetch disallowed paths).
  - Rate-limits requests so you're not hammering their servers.
  - Fetches a product/search page and parses it into a normalized Product record.
  - Stores results in a local SQLite database with a price_history table,
    so re-running this daily builds up a price-over-time dataset.

WHAT YOU NEED TO DO BEFORE RUNNING THIS FOR REAL
  1. Open https://www.pingodoce.pt in a browser, open a product category or
     search results page, and open dev tools (F12) -> Elements tab.
  2. Find the actual CSS selectors for: product name, price, unit price,
     brand, and product URL. Fill them into the PRODUCT_SELECTORS dict below.
  3. Check whether prices appear in the initial HTML (view-source:) or only
     after JavaScript runs. If only after JS, `requests` won't see them and
     you'll need Playwright instead (see the playwright_variant.py note at
     the bottom of this file).
  4. Re-read pingodoce.pt/robots.txt yourself and adjust ALLOWED/DISALLOWED
     assumptions if needed — this script checks it programmatically at
     runtime, but you should sanity-check it as a human too.

This script deliberately does NOT attempt to solve CAPTCHAs or bypass bot
detection (e.g. Cloudflare challenges). If the site blocks you, that's a
signal to slow down or stop, not to work around it.
"""

import json
import sqlite3
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.pingodoce.pt"
USER_AGENT = "PriceTrackerBot/0.1 (personal project; contact: youremail@example.com)"
MIN_DELAY_SECONDS = 2.0  # minimum pause between requests — be polite
DB_PATH = "prices.db"

# Real selectors, derived from the full product card on pingodoce.pt
# Structure observed (View Source prices are in the initial HTML, not JS):
#
#   div.product-tile-pd  [data-pid, data-gtm-info='{...clean JSON...}']
#     div.product-tile-image > a.product-tile-image-link[href]  -> product URL
#     div.product-tile-body
#       div.product-detail-info
#         div.product-name-link > a    -> product name
#         div.product-brand-name       -> brand
#         div.product-unit             -> "1 L | 0,9 €/L"
#       div.product-price
#         span.sales.reduced-price span.value[content]  -> current price
#         span.strike-through.list span.value[content]  -> original price (promo)
#         span.promo-message                             -> "Promoção até ..."
#
# The data-gtm-info attribute holds an analytics JSON blob with already-clean
# fields (item_id, item_name, item_brand, item_category, price). We use it as
# the primary source and fall back to the visible DOM where needed.

PRODUCT_SELECTORS = {
    "product_card": "div.product-tile-pd",
    "name": "div.product-name-link",
    "brand": "div.product-brand-name",
    "unit": "div.product-unit",
    "current_price_value": "span.sales.reduced-price span.value",
    "original_price_value": "span.strike-through.list span.value",
    "promo_message": "span.promo-message",
    "link": "a.product-tile-image-link",
}


@dataclass
class Product:
    store: str
    product_id: str | None       # retailer's own SKU/PID, e.g. "41043"
    name: str
    brand: str | None
    category: str | None
    price: float | None          # current price (what you'd pay now)
    original_price: float | None  # pre-discount price, if on promotion
    on_promo: bool
    promo_message: str | None
    unit_price: str | None        # e.g. "0,9 €/L"
    url: str
    scraped_at: str


class RobotsChecker:
    """Wraps urllib.robotparser so every fetch is checked against robots.txt."""

    def __init__(self, base_url: str, user_agent: str):
        self.user_agent = user_agent
        self.parser = urllib.robotparser.RobotFileParser()
        self.parser.set_url(urljoin(base_url, "/robots.txt"))
        self.parser.read()
        # crawl_delay() returns None if the site doesn't specify one
        self.crawl_delay = self.parser.crawl_delay(user_agent) or MIN_DELAY_SECONDS

    def can_fetch(self, url: str) -> bool:
        return self.parser.can_fetch(self.user_agent, url)


class RateLimitedSession:
    """A requests.Session that enforces a minimum delay between calls."""

    def __init__(self, user_agent: str, delay: float):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.delay = delay
        self._last_request_time = 0.0

    def get(self, url: str, **kwargs) -> requests.Response | None:
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        try:
            response = self.session.get(url, timeout=15, **kwargs)
            self._last_request_time = time.monotonic()
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            print(f"  [error] fetching {url}: {e}")
            return None


def parse_gtm_info(card) -> dict:
    """Extracts the analytics JSON stashed in data-gtm-info.

    This attribute holds clean, structured fields (item_id, item_name,
    item_brand, item_category, price) that are more reliable than scraping
    the visible DOM. Returns {} if absent or unparseable, so callers can
    fall back to the DOM selectors.
    """
    raw = card.get("data-gtm-info")
    if not raw:
        return {}
    try:
        data = json.loads(raw)  # BeautifulSoup already unescapes the HTML entities
    except (json.JSONDecodeError, TypeError):
        return {}
    items = data.get("items") or []
    return items[0] if items else {}


def price_from_value_span(card, selector: str) -> float | None:
    """Reads a price from <span class="value" content="0.90">.

    Pingo Doce stores the clean numeric price in the `content` attribute,
    which is far more reliable than parsing the visible "0,90 €" text.
    Falls back to parsing the text if `content` is missing.
    """
    el = card.select_one(selector)
    if el is None:
        return None
    # Preferred: the machine-readable content attribute (already dot-decimal)
    content = el.get("content")
    if content:
        try:
            return float(content)
        except ValueError:
            pass
    # Fallback: parse the visible text like "0,90 €"
    raw = el.get_text(strip=True)
    cleaned = "".join(ch for ch in raw.replace(",", ".") if ch.isdigit() or ch == ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_product_listing(html: str, page_url: str) -> list[Product]:
    """Parses a category/search page into a list of Product records.

    This is the part you MUST adapt to the real site structure —
    the selectors above are placeholders.
    """
    soup = BeautifulSoup(html, "html.parser")
    products = []
    now = datetime.now(timezone.utc).isoformat()

    cards = soup.select(PRODUCT_SELECTORS["product_card"])
    for card in cards:
        gtm = parse_gtm_info(card)  # clean structured data, may be {}

        # DOM elements (used as fallback and for promo detection)
        name_el = card.select_one(PRODUCT_SELECTORS["name"])
        brand_el = card.select_one(PRODUCT_SELECTORS["brand"])
        unit_el = card.select_one(PRODUCT_SELECTORS["unit"])
        promo_el = card.select_one(PRODUCT_SELECTORS["promo_message"])
        link_el = card.select_one(PRODUCT_SELECTORS["link"])

        current_dom = price_from_value_span(card, PRODUCT_SELECTORS["current_price_value"])
        original = price_from_value_span(card, PRODUCT_SELECTORS["original_price_value"])

        # Prefer gtm-info fields, fall back to the visible DOM
        name = gtm.get("item_name") or (name_el.get_text(strip=True) if name_el else None)
        brand = gtm.get("item_brand") or (brand_el.get_text(strip=True) if brand_el else None)
        price = gtm.get("price") if gtm.get("price") is not None else current_dom

        if not name or price is None:
            continue  # skip malformed cards rather than crashing the whole run

        # Build category from the gtm category levels (most specific first)
        cat_levels = [gtm.get(f"item_category{'' if i == 1 else i}") for i in range(1, 6)]
        category = " > ".join(c for c in cat_levels if c) or None

        product_url = urljoin(page_url, link_el["href"]) if link_el and link_el.get("href") else page_url

        products.append(Product(
            store="Pingo Doce",
            product_id=str(gtm["item_id"]) if gtm.get("item_id") else card.get("data-pid"),
            name=name,
            brand=brand,
            category=category,
            price=price,
            original_price=original,
            on_promo=original is not None,
            promo_message=promo_el.get_text(strip=True) if promo_el else None,
            unit_price=unit_el.get_text(strip=True) if unit_el else None,
            url=product_url,
            scraped_at=now,
        ))

    return products


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            product_id TEXT,
            name TEXT NOT NULL,
            brand TEXT,
            category TEXT,
            price REAL,
            original_price REAL,
            on_promo INTEGER,
            promo_message TEXT,
            unit_price TEXT,
            url TEXT NOT NULL,
            scraped_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def save_products(conn: sqlite3.Connection, products: list[Product]) -> None:
    conn.executemany(
        """INSERT INTO price_history
           (store, product_id, name, brand, category, price, original_price,
            on_promo, promo_message, unit_price, url, scraped_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(p.store, p.product_id, p.name, p.brand, p.category, p.price, p.original_price,
          int(p.on_promo), p.promo_message, p.unit_price, p.url, p.scraped_at) for p in products],
    )
    conn.commit()


def scrape_category(session: RateLimitedSession, robots: RobotsChecker, category_url: str) -> list[Product]:
    if not robots.can_fetch(category_url):
        print(f"  [skip] robots.txt disallows: {category_url}")
        return []

    response = session.get(category_url)
    if response is None:
        return []

    return parse_product_listing(response.text, category_url)


def main():
    robots = RobotsChecker(BASE_URL, USER_AGENT)
    session = RateLimitedSession(USER_AGENT, delay=max(robots.crawl_delay, MIN_DELAY_SECONDS))
    conn = init_db(DB_PATH)

    # Replace with real category/search URLs once you've inspected the site,
    # e.g. f"{BASE_URL}/pt/produtos/laticinios-e-ovos/leite/"
    category_urls = [
        f"{BASE_URL}/pt/produtos/",
    ]

    total = 0
    for url in category_urls:
        print(f"Scraping: {url}")
        products = scrape_category(session, robots, url)
        save_products(conn, products)
        total += len(products)
        print(f"  -> {len(products)} products saved")

    print(f"Done. {total} product rows saved to {DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
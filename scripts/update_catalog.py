#!/usr/bin/env python3
"""
Scrapes product listings from erco.co.il category pages and writes
erco-catalog.json in the format the inventory app expects.

Run manually:  python scripts/update_catalog.py
Run automatically by: .github/workflows/update-catalog.yml

NOTE: erco.co.il's exact HTML structure was not verified live before this
script's first run (no internet access was available while writing it).
The selectors below use standard Magento 2 markup conventions with several
fallbacks, but the FIRST real run should be checked carefully - if a
category comes back with 0 products, the selectors likely need a small
adjustment. Report the Action's log output and it can be fixed quickly.
"""
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

CATEGORIES = [
    "https://www.erco.co.il/b2c/sockets-plugs/home-office-switching.html",
    "https://www.erco.co.il/b2c/command-and-control/protection-and-contactors/miniature-circuit-breaker.html",
    "https://www.erco.co.il/b2c/cables-wires/wires.html",
    "https://www.erco.co.il/b2c/cables-wires/low-voltage-power-cables.html",
    "https://www.erco.co.il/b2c/work-tools/test-and-measurement-tools.html",
    "https://www.erco.co.il/b2c/work-tools/technical-supply/cable-ties.html",
    "https://www.erco.co.il/b2c/work-tools/technical-supply/screws-anchors.html",
    "https://www.erco.co.il/b2c/work-tools/technical-supply/hooks.html",
    "https://www.erco.co.il/b2c/cabinets-boards-boxes/distribution-board.html",
    "https://www.erco.co.il/b2c/work-tools/power-tools/drills-drivers.html",
    "https://www.erco.co.il/b2c/work-tools/power-tools/grinders.html",
    "https://www.erco.co.il/b2c/work-tools/hand-tools.html",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}

TOOL_KEYWORDS = ["כלי", "מברג", "פלייר", "מקדח", "משחזת", "רב מודד", "צבת",
                  "מד ", "בודק", "רולטקה", "פלס", "מפוח", "מסור", "פטיש"]


def guess_category(name):
    return "tool" if any(k in name for k in TOOL_KEYWORDS) else "material"


def guess_unit(name):
    if "כבל" in name or ("חוט" in name and "יח" not in name):
        return "מ'"
    return "יח'"


def parse_price_text(text):
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def extract_image(item):
    # Prefer Magento's standard product-photo class, which is the real product image
    img_el = item.select_one("img.product-image-photo") or item.select_one(".product-image-photo img")
    if not img_el:
        # fall back to any <img> that doesn't look like a promo badge/banner/ribbon
        bad_words = ["badge", "label", "ribbon", "sale", "discount", "promo", "popup", "banner"]
        for candidate in item.select("img"):
            cls = " ".join(candidate.get("class", [])).lower()
            alt = (candidate.get("alt") or "").lower()
            if any(w in cls or w in alt for w in bad_words):
                continue
            img_el = candidate
            break
    if not img_el:
        return ""
    return img_el.get("src") or img_el.get("data-src") or img_el.get("data-original") or ""


def extract_sku(item, href, block_text):
    m = re.search(r'מק"?ט[:\s]*([A-Za-z0-9\-\._+]+)', block_text)
    if m:
        return m.group(1)
    m2 = re.search(r"/([A-Za-z0-9\-\._+]+)\.html", href or "")
    if m2:
        return m2.group(1)
    return ""


def scrape_category(url, session):
    products = []
    try:
        resp = session.get(url, headers=HEADERS, timeout=25)
        resp.raise_for_status()
    except Exception as e:
        print(f"  !! failed to fetch {url}: {e}", file=sys.stderr)
        return products

    soup = BeautifulSoup(resp.text, "html.parser")

    items = soup.select("li.product-item")
    if not items:
        items = soup.select(".products.list.items .item.product")
    if not items:
        # last-resort fallback: walk up from any price-bearing element
        price_nodes = soup.select("[data-price-amount]")
        seen = set()
        items = []
        for node in price_nodes:
            parent = node.find_parent("li") or node.find_parent("div", class_=re.compile("product"))
            if parent and id(parent) not in seen:
                seen.add(id(parent))
                items.append(parent)

    for item in items:
        try:
            link_el = item.select_one("a.product-item-link") or item.select_one("a[href]")
            if not link_el:
                continue
            name = link_el.get_text(strip=True)
            href = link_el.get("href")
            if not name or not href:
                continue

            image = extract_image(item)

            price = None
            price_el = item.select_one("[data-price-amount]")
            if price_el and price_el.has_attr("data-price-amount"):
                try:
                    price = float(price_el["data-price-amount"])
                except ValueError:
                    price = None
            if price is None:
                price_text_el = item.select_one(".price")
                price = parse_price_text(price_text_el.get_text() if price_text_el else None)

            block_text = item.get_text(" ", strip=True)
            sku = extract_sku(item, href, block_text)

            products.append({
                "name": name,
                "sku": sku,
                "category": guess_category(name),
                "unit": guess_unit(name),
                "price": price,
                "imageUrl": image,
                "productUrl": href,
            })
        except Exception as e:
            print(f"  !! skipped one item: {e}", file=sys.stderr)
            continue
    return products


def main():
    session = requests.Session()
    all_products = {}

    for url in CATEGORIES:
        print(f"Scraping {url} ...")
        prods = scrape_category(url, session)
        print(f"  -> {len(prods)} products found")
        for p in prods:
            key = p["sku"] or p["name"]
            all_products[key] = p  # de-dup across categories, last one wins
        time.sleep(2)  # be polite to their server

    output = {
        "supplierName": "ERCO",
        "supplierWebsite": "https://www.erco.co.il",
        "updatedAt": time.strftime("%Y-%m-%d"),
        "products": list(all_products.values()),
    }

    with open("erco-catalog.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    print(f"\nTotal unique products written: {len(output['products'])}")
    if len(output["products"]) == 0:
        print("WARNING: zero products scraped - the site's HTML structure likely "
              "changed or differs from what this script expects. Check the log "
              "above for fetch errors, and report back so the selectors can be fixed.",
              file=sys.stderr)


if __name__ == "__main__":
    main()

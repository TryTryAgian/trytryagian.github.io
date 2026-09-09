#!/usr/bin/env python3
"""
Scrapes product listings from multiple supplier websites and writes
catalog-data.json in the multi-supplier format the inventory app expects:

  {
    "updatedAt": "YYYY-MM-DD",
    "suppliers": [
      {"supplierName": "...", "supplierWebsite": "...", "products": [...]},
      ...
    ]
  }

Each supplier in SUPPLIERS below is scraped with the "handler" named for it:
  - "magento"   : erco.co.il (standard Magento 2 category pages)
  - "brand_asp" : brandtools.co.il (custom ASP platform, ProdId-based)
  - "generic"   : best-effort fallback for any other site - tries to read
                  embedded JSON-LD Product schema (many modern e-commerce
                  platforms include this for Google/SEO purposes). Use this
                  handler as a starting point for a brand-new supplier; if a
                  site has no JSON-LD, a dedicated handler needs to be added
                  (same as brand_asp was added here).

Run manually:  python scripts/update_catalog.py
Run automatically by: .github/workflows/update-catalog.yml

NOTE: neither the ERCO Magento logic nor the new Brand handler was verified
against a live network connection while writing this (no internet access
was available in the coding environment). ERCO's handler was already
corrected once after a real run (see git history). The Brand handler is
its first real run - if a category comes back with 0 products, or names/
prices look wrong, that's expected-possible on a first try; paste the
Action's log output back for a quick fix.
"""
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}

TOOL_KEYWORDS = ["כלי", "מברג", "פלייר", "מקדח", "משחזת", "רב מודד", "צבת",
                  "מד ", "בודק", "רולטקה", "פלס", "מפוח", "מסור", "פטיש",
                  "מברגה", "פטישון", "רתכת", "מדחס", "מכונת"]

SUPPLIERS = [
    {
        "name": "ERCO",
        "website": "https://www.erco.co.il",
        "handler": "magento",
        "categories": [
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
        ],
    },
    {
        "name": "ברנד אספקה טכנית",
        "website": "https://www.brandtools.co.il",
        "handler": "brand_asp",
        "categories": [
            "https://www.brandtools.co.il/productslist.asp?catid=1298",  # אביזרי חשמל וטלפון
            "https://www.brandtools.co.il/productslist.asp?catid=4",     # כלים חשמליים
            "https://www.brandtools.co.il/productslist.asp?catid=2",     # כלים ידניים
            "https://www.brandtools.co.il/productslist.asp?catid=139",   # כלי מדידה וסימון
            "https://www.brandtools.co.il/productslist.asp?catid=1363",  # ציוד לתעשייה
        ],
    },
    # To add another supplier: copy a block above with its own category URLs.
    # If you don't know the right "handler" yet, use "generic" first - it
    # tries JSON-LD structured data, which many sites have even without a
    # dedicated handler being written for them.
]


# ---------------- shared helpers ----------------

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


def fetch(url, session):
    resp = session.get(url, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    return resp.text


# ---------------- handler: magento (ERCO) ----------------

def extract_image_magento(item):
    img_el = item.select_one("img.product-image-photo") or item.select_one(".product-image-photo img")
    if not img_el:
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


def extract_sku_magento(item, href, block_text):
    m = re.search(r'מק"?ט[:\s]*([A-Za-z0-9\-\._+]+)', block_text)
    if m:
        return m.group(1)
    m2 = re.search(r"/([A-Za-z0-9\-\._+]+)\.html", href or "")
    if m2:
        return m2.group(1)
    return ""


def scrape_magento(html, url):
    products = []
    soup = BeautifulSoup(html, "html.parser")

    items = soup.select("li.product-item")
    if not items:
        items = soup.select(".products.list.items .item.product")
    if not items:
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

            image = extract_image_magento(item)

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
            sku = extract_sku_magento(item, href, block_text)

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


# ---------------- handler: brand_asp (ברנד אספקה טכנית) ----------------

def scrape_brand_asp(html, url):
    soup = BeautifulSoup(html, "html.parser")
    groups = {}  # ProdId -> accumulated fields

    for a in soup.select('a[href*="ProductInfo.asp"]'):
        href = a.get("href", "")
        m = re.search(r"ProdId=(\d+)", href)
        if not m:
            continue
        pid = m.group(1)
        g = groups.setdefault(pid, {
            "name": None, "image": None, "price": None, "model": None,
            "productUrl": f"https://www.brandtools.co.il/ProductInfo.asp?ProdId={pid}",
        })

        img = a.find("img")
        if img and not g["image"]:
            src = img.get("src") or img.get("data-src") or ""
            if "ProductsImages/s250/" in src:
                g["image"] = src if src.startswith("http") else "https://www.brandtools.co.il" + src

        text = a.get_text(strip=True)
        if text and text != "פרטים נוספים" and not g["name"]:
            g["name"] = text

        if g["price"] is None or g["model"] is None:
            parent = a.find_parent(["div", "li", "td", "tr"]) or a.parent
            block_text = parent.get_text(" ", strip=True) if parent else ""
            if g["model"] is None:
                mm = re.search(r"דגם:\s*([^\s₪]+(?:\s[^\s₪]+)*?)(?:\s{2,}|₪|$)", block_text)
                if mm:
                    g["model"] = mm.group(1).strip()
            if g["price"] is None:
                pm = re.search(r"₪\s*([\d,]+)", block_text)
                if pm:
                    try:
                        g["price"] = float(pm.group(1).replace(",", ""))
                    except ValueError:
                        pass

    products = []
    for pid, g in groups.items():
        if not g["name"]:
            continue
        products.append({
            "name": g["name"],
            "sku": g["model"] or pid,
            "category": guess_category(g["name"]),
            "unit": "יח'",
            "price": g["price"],
            "imageUrl": g["image"] or "",
            "productUrl": g["productUrl"],
        })
    return products


# ---------------- handler: generic (JSON-LD fallback, for future suppliers) ----------------

def _ldjson_to_product(p):
    offers = p.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = offers.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    image = p.get("image")
    if isinstance(image, list):
        image = image[0] if image else ""
    name = p.get("name") or ""
    return {
        "name": name,
        "sku": p.get("sku") or p.get("mpn") or "",
        "category": guess_category(name),
        "unit": "יח'",
        "price": price,
        "imageUrl": image or "",
        "productUrl": p.get("url") or "",
    }


def scrape_generic(html, url):
    products = []
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "{}")
        except Exception:
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            items = entry.get("itemListElement")
            if items:
                for it in items:
                    p = it.get("item", it) if isinstance(it, dict) else None
                    if isinstance(p, dict) and p.get("@type") == "Product":
                        products.append(_ldjson_to_product(p))
            elif entry.get("@type") == "Product":
                products.append(_ldjson_to_product(entry))
    if not products:
        print(f"  (generic handler found no JSON-LD product data at {url} - "
              f"this site needs a dedicated handler, like brand_asp was added)", file=sys.stderr)
    return products


HANDLERS = {
    "magento": scrape_magento,
    "brand_asp": scrape_brand_asp,
    "generic": scrape_generic,
}


# ---------------- main ----------------

def main():
    session = requests.Session()
    output_suppliers = []

    for sup in SUPPLIERS:
        print(f"\n=== {sup['name']} ({sup['handler']}) ===")
        handler_fn = HANDLERS.get(sup["handler"], scrape_generic)
        all_products = {}

        for url in sup["categories"]:
            print(f"Scraping {url} ...")
            try:
                html = fetch(url, session)
            except Exception as e:
                print(f"  !! failed to fetch {url}: {e}", file=sys.stderr)
                continue
            try:
                prods = handler_fn(html, url)
            except Exception as e:
                print(f"  !! handler error on {url}: {e}", file=sys.stderr)
                prods = []
            print(f"  -> {len(prods)} products found")
            for p in prods:
                key = p["sku"] or p["name"]
                all_products[key] = p
            time.sleep(2)  # be polite to their server

        output_suppliers.append({
            "supplierName": sup["name"],
            "supplierWebsite": sup["website"],
            "products": list(all_products.values()),
        })

    output = {
        "updatedAt": time.strftime("%Y-%m-%d"),
        "suppliers": output_suppliers,
    }

    with open("catalog-data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    print("\n=== summary ===")
    total = 0
    for s in output_suppliers:
        print(f"  {s['supplierName']}: {len(s['products'])} products")
        total += len(s["products"])
    print(f"Total products written: {total}")
    if total == 0:
        print("WARNING: zero products scraped across all suppliers.", file=sys.stderr)


if __name__ == "__main__":
    main()

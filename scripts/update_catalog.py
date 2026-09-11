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

Handlers are per PLATFORM, not per supplier - "logate" works for any store
built on Logate's "virtual store" product, not just ברנד specifically, the
same way "magento" works for any Magento store, not just ERCO specifically:
  - "magento" : Magento 2 standard category pages (e.g. erco.co.il)
  - "logate"  : Logate Technologies' ASP-based "virtual store" platform,
                ProdId-driven (e.g. brandtools.co.il)
  - "konimbo" : Konimbo platform, /items/{id}-slug URLs (e.g. dror-tools.co.il)
  - "shopify" : Shopify stores - uses the public /products.json storefront
                feed (no HTML parsing needed, most reliable handler here).
                Point category URLs at "<collection-url>/products.json"
                instead of the normal page URL.
  - "generic" : best-effort fallback for any other site - tries to read
                embedded JSON-LD Product schema (many modern e-commerce
                platforms include this for Google/SEO purposes). Use this
                handler as a starting point for a brand-new supplier; if a
                site has no JSON-LD, a dedicated platform handler needs to
                be added (same as "logate" and "konimbo" were added here).

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
import os
import re
import sys
import time

# Force stdout to flush every line (like stderr already does by default). Without this,
# progress lines can get buffered and appear "late" relative to error lines in the GitHub
# Actions log, making the log look chronologically scrambled even though nothing is actually
# wrong with execution order.
sys.stdout.reconfigure(line_buffering=True)

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
        "handler": "logate",
        "warmup": True,  # this supplier started 403-blocking direct category requests
        "categories": [
            "https://www.brandtools.co.il/productslist.asp?catid=1298",  # אביזרי חשמל וטלפון
            "https://www.brandtools.co.il/productslist.asp?catid=643",   # כלים לחשמלאים
            "https://www.brandtools.co.il/productslist.asp?catid=1633",  # כלי עבודה חשמליים
            "https://www.brandtools.co.il/productslist.asp?catid=2",     # כלים ידניים
            "https://www.brandtools.co.il/productslist.asp?catid=139",   # כלי מדידה וסימון
        ],
    },
    {
        "name": "דרור כלי עבודה",
        "website": "https://www.dror-tools.co.il",
        "handler": "konimbo",
        "categories": [
            "https://www.dror-tools.co.il/183379-%D7%97%D7%A9%D7%9E%D7%9C-%D7%95%D7%90%D7%91%D7%99%D7%96%D7%A8%D7%99%D7%9D",  # חשמל ואביזרים
            "https://www.dror-tools.co.il/183352-%D7%9E%D7%A7%D7%93%D7%97%D7%95%D7%AA",  # מקדחות
            "https://www.dror-tools.co.il/183354-%D7%A4%D7%98%D7%99%D7%A9%D7%95%D7%A0%D7%99%D7%9D",  # פטישונים
            "https://www.dror-tools.co.il/183314-%D7%90%D7%91%D7%99%D7%96%D7%A8%D7%99-%D7%91%D7%98%D7%99%D7%97%D7%95%D7%AA-%D7%9E%D7%A1%D7%9B%D7%95%D7%AA-%D7%95%D7%A0%D7%A9%D7%9E%D7%99%D7%95%D7%AA",  # אביזרי בטיחות
            "https://www.dror-tools.co.il/191826-%D7%9C%D7%99%D7%99%D7%96%D7%A8%D7%99%D7%9D-%D7%95%D7%9B%D7%9C%D7%99-%D7%9E%D7%93%D7%99%D7%93%D7%94",  # לייזרים וכלי מדידה
        ],
    },
    {
        "name": "חשמל ישיר",
        "website": "https://yashir-group.biz",
        "handler": "shopify",
        "categories": [
            "https://yashir-group.biz/collections/%D7%9B%D7%9C%D7%99-%D7%A2%D7%91%D7%95%D7%93%D7%94/products.json?limit=200",       # כלי עבודה
            "https://yashir-group.biz/collections/%D7%9E%D7%95%D7%A6%D7%A8%D7%99%D7%9D-%D7%A0%D7%95%D7%A1%D7%A4%D7%99%D7%9D/products.json?limit=200",  # מוצרים נוספים
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
    # Some sites (especially older/legacy platforms) don't declare their character
    # encoding correctly in the response headers, causing requests to guess wrong
    # and mangle Hebrew text. Sniff the real encoding from the content bytes instead
    # of trusting a missing/unreliable header - this fixes it for any such site,
    # not just a specific one.
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
        resp.encoding = resp.apparent_encoding
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


# ---------------- handler: logate (Logate Technologies' "virtual store" platform) ----------------

def scrape_logate(html, url):
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


# ---------------- handler: konimbo (Konimbo platform) ----------------

def scrape_konimbo(html, url):
    soup = BeautifulSoup(html, "html.parser")
    groups = {}  # product id (from /items/{id}-slug) -> accumulated fields
    base_match = re.match(r"(https?://[^/]+)", url)
    base = base_match.group(1) if base_match else ""

    # this hidden data blob (sku + stock flag + price + another flag, no separators when
    # concatenated) sometimes ends up inside the same anchor as the product name - strip it
    # out wherever it appears rather than trusting anchor text as-is
    JUNK_RE = re.compile(r"[A-Za-z0-9._\-]{2,24}\s*out_of_stock\s*(?:true|false)?\s*[\d.]*\s*(?:true|false)?", re.I)

    for a in soup.select('a[href*="/items/"]'):
        href = a.get("href", "")
        m = re.search(r"/items/(\d+)-", href)
        if not m:
            continue
        pid = m.group(1)
        full_href = href if href.startswith("http") else (base + href)
        g = groups.setdefault(pid, {"name": None, "image": None, "price": None, "sku": None, "productUrl": full_href})

        img = a.find("img")
        if img:
            if not g["image"]:
                src = img.get("src") or img.get("data-src") or ""
                if src:
                    g["image"] = src if src.startswith("http") else (base + src)
            # the image's alt text is the most reliable source for the real product name -
            # it doesn't carry the hidden data-blob text that anchor.get_text() picks up
            alt = (img.get("alt") or "").strip()
            if alt and not g["name"]:
                g["name"] = alt

        if not g["name"]:
            raw_text = a.get_text(" ", strip=True)
            cleaned = JUNK_RE.sub(" ", raw_text)
            cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
            if cleaned and len(cleaned) > 2:
                g["name"] = cleaned

        if g["price"] is None or g["sku"] is None:
            parent = a.find_parent(["div", "li", "article"]) or a.parent
            block_text = parent.get_text(" ", strip=True) if parent else ""
            if g["price"] is None:
                pm = re.search(r"מחיר\s*([\d,]+)\s*₪", block_text)
                if pm:
                    try:
                        g["price"] = float(pm.group(1).replace(",", ""))
                    except ValueError:
                        pass
            if g["sku"] is None:
                # Konimbo tends to render a SKU-like code right before an "out_of_stock" flag
                sm = re.search(r"\b([A-Za-z0-9._\-]{3,24})\s+out_of_stock\b", block_text)
                if sm:
                    g["sku"] = sm.group(1)

    products = []
    for pid, g in groups.items():
        name = g["name"]
        if not name:
            continue
        # final safety net in case any junk still slipped through into the chosen name
        name = JUNK_RE.sub(" ", name)
        name = re.sub(r"\s+", " ", name).strip(" -")
        if not name or len(name) < 2:
            continue
        products.append({
            "name": g["name"],
            "sku": g["sku"] or pid,
            "category": guess_category(g["name"]),
            "unit": "יח'",
            "price": g["price"],
            "imageUrl": g["image"] or "",
            "productUrl": g["productUrl"],
        })
    return products


# ---------------- handler: shopify (public /products.json feed) ----------------
# Point category URLs at "<collection-url>/products.json" instead of the normal
# page URL - Shopify exposes this storefront JSON feed publicly on most stores,
# with no parsing needed at all. This is the most reliable handler here.

def scrape_shopify(raw_text, url):
    try:
        data = json.loads(raw_text)
    except Exception as e:
        print(f"  !! not valid JSON (is this really a /products.json URL?): {e}", file=sys.stderr)
        return []
    base_match = re.match(r"(https?://[^/]+)", url)
    base = base_match.group(1) if base_match else ""

    products = []
    for p in data.get("products", []):
        name = p.get("title", "")
        if not name:
            continue
        variants = p.get("variants") or [{}]
        first = variants[0] if variants else {}
        price = None
        if first.get("price") is not None:
            try:
                price = float(first["price"])
            except (TypeError, ValueError):
                price = None
        sku = first.get("sku") or ""
        images = p.get("images") or []
        image = ""
        if images and images[0].get("src"):
            image = images[0]["src"]
        elif p.get("image") and p["image"].get("src"):
            image = p["image"]["src"]
        handle = p.get("handle", "")
        product_url = f"{base}/products/{handle}" if base and handle else ""
        products.append({
            "name": name,
            "sku": sku or str(p.get("id", "")),
            "category": guess_category(name),
            "unit": "יח'",
            "price": price,
            "imageUrl": image,
            "productUrl": product_url,
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
              f"this site likely needs a dedicated platform handler, the way \"logate\" was added)", file=sys.stderr)
    return products


HANDLERS = {
    "magento": scrape_magento,
    "logate": scrape_logate,
    "konimbo": scrape_konimbo,
    "shopify": scrape_shopify,
    "generic": scrape_generic,
}


# ---------------- main ----------------

# ---------------- platform fingerprinting (for test mode) ----------------
# Telltale signatures that reveal which underlying platform a site runs on,
# even when neither the generic nor a specific handler matched. A platform
# match here doesn't guarantee a handler will work with zero changes, but
# it's a strong hint - most stores on the same platform share very similar
# markup, the way "magento" already generalizes beyond ERCO.
PLATFORM_FINGERPRINTS = {
    "konimbo": ["konimbo.co.il", "konimbo"],
    "logate": ["logate.co.il", "לוגייט טכנולוגיות"],
    "magento": ["Magento", "mage/cookies", "/static/version"],
    "shopify": ["cdn.shopify.com", "Shopify.theme", "myshopify.com"],
}


def fingerprint_platform(html):
    found = []
    for platform, needles in PLATFORM_FINGERPRINTS.items():
        if any(n in html for n in needles):
            found.append(platform)
    return found


def run_test_mode(url):
    print(f"=== TEST MODE: {url} ===")
    print("(this does not touch or commit catalog-data.json - it's just a diagnostic report)\n")
    session = requests.Session()
    try:
        html = fetch(url, session)
    except Exception as e:
        print(f"!! failed to fetch: {e}", file=sys.stderr)
        return

    print("--- trying generic handler (looks for built-in JSON-LD product data) ---")
    generic_products = scrape_generic(html, url)
    print(f"generic handler found: {len(generic_products)} products")
    if generic_products:
        for p in generic_products[:5]:
            print(f"  - {p['name']}  |  sku={p['sku']}  |  price={p['price']}  |  image={'yes' if p['imageUrl'] else 'no'}")
        print("\n[RESULT] This site has usable structured data - it can likely be added with")
        print("         handler: \"generic\" and NO custom code. Add its category URLs to")
        print("         SUPPLIERS above with that handler and it should just work.")
        return

    print("\n--- trying magento handler (in case it happens to be Magento-based, like ERCO) ---")
    magento_products = scrape_magento(html, url)
    print(f"magento handler found: {len(magento_products)} products")
    if magento_products:
        for p in magento_products[:5]:
            print(f"  - {p['name']}  |  sku={p['sku']}  |  price={p['price']}  |  image={'yes' if p['imageUrl'] else 'no'}")
        print("\n[RESULT] This looks like a Magento-based site - it can likely be added with")
        print("         handler: \"magento\" and NO custom code.")
        return

    print("\n[RESULT] Neither the generic (JSON-LD) nor the magento pattern matched this page.")
    fp = fingerprint_platform(html)
    known_handlers = set(HANDLERS.keys())
    fp_with_handler = [p for p in fp if p in known_handlers]
    fp_without_handler = [p for p in fp if p not in known_handlers]
    if fp_with_handler:
        print(f"         BUT this site looks like it's running on: {', '.join(fp_with_handler)}")
        print(f"         A handler for that platform already exists (built from a different")
        print(f"         supplier) - it will likely work here too with little or no change.")
        for p in fp_with_handler:
            if p == "shopify":
                json_url = url.rstrip("/") + "/products.json?limit=5"
                print(f"         Shopify needs the .json feed, not this page - trying: {json_url}")
                try:
                    json_text = fetch(json_url, requests.Session())
                    test_products = scrape_shopify(json_text, url)
                    print(f"         Trying \"shopify\" handler on the .json feed: found {len(test_products)} products")
                    for prod in test_products[:5]:
                        print(f"           - {prod['name']}  |  sku={prod['sku']}  |  price={prod['price']}")
                except Exception as e:
                    print(f"         Fetching the .json feed failed: {e}")
                continue
            handler_fn = HANDLERS[p]
            try:
                test_products = handler_fn(html, url)
                print(f"         Trying \"{p}\" handler directly: found {len(test_products)} products")
                for prod in test_products[:5]:
                    print(f"           - {prod['name']}  |  sku={prod['sku']}  |  price={prod['price']}")
            except Exception as e:
                print(f"         Trying \"{p}\" handler directly failed: {e}")
    elif fp_without_handler:
        print(f"         This site looks like it's running on: {', '.join(fp_without_handler)}")
        print(f"         No handler exists for that platform yet - it needs one built,")
        print(f"         same as \"logate\" was built for Logate's platform.")
    else:
        print("         This site most likely needs its own dedicated handler written for it,")
        print("         the same way \"logate\" was built for Brand's platform. Report this URL")
        print("         back (and ideally one or two real product-listing category URLs on the")
        print("         same site) to get a handler built for it.")


def main():
    test_url = os.environ.get("TEST_URL", "").strip()
    if test_url:
        run_test_mode(test_url)
        return

    session = requests.Session()
    output_suppliers = []

    for sup in SUPPLIERS:
        print(f"\n=== {sup['name']} ({sup['handler']}) ===")
        handler_fn = HANDLERS.get(sup["handler"], scrape_generic)
        all_products = {}

        # visit the homepage first to pick up any session cookie some sites require
        # before allowing access to inner pages - only for suppliers that need it
        # (opt-in per supplier, since it's unnecessary risk for ones that already work fine)
        if sup.get("warmup"):
            try:
                fetch(sup["website"], session)
                time.sleep(1)
            except Exception as e:
                print(f"  (homepage warm-up failed, continuing anyway: {e})", file=sys.stderr)

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

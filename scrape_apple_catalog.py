#!/usr/bin/env python3
"""
scrape_apple_catalog.py
=======================
Refreshes ``apple_catalog.json`` — the current US Apple Store catalog (models,
storage / colour / size variants and their US prices, plus accessories) —
straight from apple.com. ``apple_price_data.py`` picks that file up
automatically and re-exports it to ``frontend/data.json``.

Why this is a Python script and not part of the website: apple.com does not
send CORS headers, so a browser page can't read it. A local script can.

    python3 scrape_apple_catalog.py                  # full refresh (~350 requests, a few minutes)
    python3 scrape_apple_catalog.py --devices-only   # iPhone/iPad/Mac/Watch/... only (~40 requests)
    python3 scrape_apple_catalog.py --url https://www.apple.com/shop/buy-iphone/iphone-18-pro
    python3 scrape_apple_catalog.py --url <apple.com link> --add   # look one page up and merge it in
    python3 scrape_apple_catalog.py --no-cache       # ignore the 12-hour on-disk page cache

What gets read (all of it is embedded in apple.com's own HTML):
  * iPhone / iPad / displays / AirPods / TV / HomePod / AirTag: the per-SKU list
    (name incl. storage + colour, exact price).
  * Mac / Apple Watch: the configurator's price table (size / chip / case / connectivity).
  * Vision Pro and anything else without a table: the schema.org starting price.
  * Accessories: every product page linked from the accessory category pages.

Standard library only.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(ROOT, "apple_catalog.json")
CACHE_DIR = os.path.join(ROOT, ".apple_cache")
BASE = "https://www.apple.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# shop section -> product category shown in the app's product picker
SECTION_CATEGORY = {
    "buy-iphone": "iPhone", "buy-ipad": "iPad", "buy-mac": "Mac", "buy-watch": "Watch",
    "buy-vision": "Vision", "buy-airpods": "Audio", "buy-homepod": "Audio",
    "buy-tv": "Home", "buy-airtag": "Accessories",
}
CATEGORY_ORDER = ["iPhone", "iPad", "Mac", "Watch", "Vision", "Audio", "Home", "Accessories"]

# Landing pages whose links reveal the product families...
LANDING_SECTIONS = ["buy-iphone", "buy-ipad", "buy-mac", "buy-watch", "buy-vision"]
# ...plus families that have no landing page of their own.
EXTRA_FAMILIES = [
    ("buy-airpods", "airpods-pro-3"), ("buy-airpods", "airpods-4"), ("buy-airpods", "airpods-5"),
    ("buy-airpods", "airpods-max"), ("buy-tv", "apple-tv-4k"), ("buy-homepod", "homepod"),
    ("buy-homepod", "homepod-mini"), ("buy-airtag", "airtag"),
]
SKIP_SLUGS = {"carrier-offers"}

# Accessory category pages, most specific first (a product shown on several
# pages is filed under the first one that lists it).
ACCESSORY_SECTIONS = [
    ("cases-protection", "Cases & Protection"), ("chargers-adapters", "Chargers & Adapters"),
    ("headphones-speakers", "Headphones & Speakers"), ("mice-keyboards", "Mice & Keyboards"),
    ("drives-storage", "Drives & Storage"), ("gaming", "Gaming"), ("homekit", "Smart Home"),
    ("health-fitness", "Health & Fitness"), ("content-creation", "Content Creation"),
    ("accessibility", "Accessibility"), ("office", "Office"), ("travel-essentials", "Travel"),
    ("college-essentials", "College"), ("made-by-apple", "Made by Apple"),
    ("new-arrivals", "New Arrivals"),
]
DIM_ORDER = ["size", "chip", "material", "connectivity", "glass", "finish", "storage", "stand", "color"]


# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------

def fetch(url: str, use_cache: bool = True, ttl: int = 12 * 3600):
    """GET a page (text). Returns None for 404 or after repeated failures."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".html")
    if use_cache and os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
        with open(path, encoding="utf-8") as f:
            return f.read()
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=45) as r:
                body = r.read().decode("utf-8", errors="ignore")
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            return body
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except Exception as e:  # network hiccup — retry
            last = e
        time.sleep(1.5 * (attempt + 1))
    print(f"  ! could not fetch {url}: {last}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def norm(s: str) -> str:
    """Unescape entities, normalise fancy hyphens/spaces, drop invisible marks, collapse whitespace."""
    s = html.unescape(s)
    s = s.replace("\u00a0", " ").replace("\u2011", "-").replace("\u2010", "-")
    s = re.sub("[\u200e\u200f\u202a-\u202e]", "", s)      # invisible left/right-to-left marks
    return re.sub(r"\s+", " ", s).strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(s).lower()).strip("-")


def jsonld_products(page: str) -> list[dict]:
    """schema.org Product blocks -> [{'name', 'prices': [...]}]."""
    out = []
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, flags=re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for item in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []):
            if not isinstance(item, dict) or "offers" not in item:
                continue
            offers = item["offers"] if isinstance(item["offers"], list) else [item["offers"]]
            prices = []
            for o in offers:
                if not isinstance(o, dict):
                    continue
                p = o.get("price", o.get("lowPrice"))
                try:
                    prices.append(float(p))
                except (TypeError, ValueError):
                    pass
            if prices:
                out.append({"name": norm(item.get("name", "")), "prices": prices})
    return out


# ---------------------------------------------------------------------------
# Parsing one product family
# ---------------------------------------------------------------------------

SKU_RE = re.compile(
    r'\{"sku":"[A-Z0-9]+","partNumber":"[^"]*","price":\{"fullPrice":([0-9.]+)\},'
    r'"category":"[^"]*","name":"((?:[^"\\]|\\.)*)"\}')


def parse_sku_list(page: str) -> list[tuple[str, float]]:
    seen = {}
    for price, name in SKU_RE.findall(page):
        try:
            name = json.loads('"' + name + '"')
        except ValueError:
            pass
        seen[norm(name)] = float(price)
    return list(seen.items())


def split_device_sku(name: str, family_slug: str):
    """'11-inch iPad Air Wi-Fi 512GB - Space Gray' -> ('iPad Air', {size, connectivity, storage, color})."""
    n = norm(name)
    dims: dict[str, str] = {}
    if family_slug.startswith("studio-display"):
        parts = [p.strip() for p in n.split(" - ")]
        if len(parts) >= 2:
            dims["glass"] = parts[1]
        if len(parts) >= 3:
            dims["stand"] = parts[2]
        return parts[0], dims
    m = re.match(r"^(\d+(?:\.\d+)?)-inch\s+", n)
    if m:
        dims["size"] = m.group(1) + "-inch"
        n = n[m.end():]
    color = None
    if " - " in n:
        n, color = n.rsplit(" - ", 1)
    m = re.search(r"\s+with (standard|nano-texture) glass", n, re.I)
    if m:
        dims["glass"] = m.group(1).capitalize() + " glass"
        n = n[:m.start()] + n[m.end():]
    m = re.search(r"\s+with (\d+(?:GB|TB)) storage", n)
    if m:
        dims["storage"] = m.group(1)
        n = n[:m.start()] + n[m.end():]
    m = re.search(r"\s+(Wi-Fi \+ Cellular|Wi-Fi \+ Ethernet|Wi-Fi)\b", n)
    if m:
        dims["connectivity"] = m.group(1)
        n = n[:m.start()] + n[m.end():]
    m = re.search(r"\s+(\d+(?:GB|TB))\b", n)
    if m:
        dims["storage"] = m.group(1)
        tail = n[m.end():].strip()
        if color is None and tail:          # iPhone style: "iPhone 18 Pro Max 1TB Silver"
            color = tail
        n = n[:m.start()] + (" " + tail if color is None and tail else "")
    if color:
        dims["color"] = color
    return n.strip(), dims


COLOR_PREFIXES = ["sky", "space", "rose", "jet", "light", "dark", "deep", "cosmic", "soft", "mist", "cloud",
                  "natural", "desert", "radiant", "night", "pearl", "ocean"]


def pretty_value(dim: str, raw: str) -> str:
    v = raw
    if dim == "color":
        s = v.replace("_", " ").replace("-", " ")
        if " " in s:
            return s.title()
        for p in COLOR_PREFIXES:
            if s.startswith(p) and len(s) > len(p):
                return f"{p} {s[len(p):]}".title()
        return s.title()
    if dim == "size":
        return re.sub(r"^(\d+(?:\.\d+)?)inch$", r"\1-inch", v)
    if dim == "connectivity":
        return {"gps": "GPS", "gpscell": "GPS + Cellular"}.get(v, v)
    if dim == "chip":
        m = re.match(r"^(\d+)-(\d+)$", v)
        return f"{m.group(1)}-core CPU / {m.group(2)}-core GPU" if m else v
    if dim == "material":
        return v.replace("_", " ").title()
    if dim == "storage":
        return v.upper()
    if dim == "finish":
        return v.replace("_", "-").capitalize()
    return v


DIM_KEY_MAP = {"screensize": "size", "casesize": "size", "casematerial": "material",
               "connection": "connectivity", "capacity": "storage"}


def pretty_chip(chip: str | None, cores: str | None) -> str:
    """('m5pro', 'm5pro-15-16') -> 'M5 Pro · 15-core CPU / 16-core GPU'."""
    parts = []
    if chip:
        m = re.match(r"^(m\d+)(pro|max|ultra)?$", chip)
        parts.append((m.group(1).upper() + (" " + m.group(2).title() if m.group(2) else "")) if m else chip.upper())
    if cores:
        m = re.search(r"(\d+)-(\d+)$", cores)
        if m:
            parts.append(f"{m.group(1)}-core CPU / {m.group(2)}-core GPU")
    return " \u00b7 ".join(parts)


def parse_configurator(page: str) -> list[tuple[dict, float]]:
    """Mac / Watch pages: (priceKey, dimensions) records + a priceKey -> amount table."""
    recs = []
    for m in re.finditer(r'"priceKey":"([^"]+)","isComingSoon":(?:true|false),"dimensions":(\{[^{}]*\})', page):
        recs.append((m.group(1), m.group(2)))
    for m in re.finditer(r'"dimensions":(\{[^{}]*\}),"priceKey":"([^"]+)"', page):
        recs.append((m.group(2), m.group(1)))
    out, seen = [], set()
    for key, dims_json in recs:
        pm = re.search(r'"' + re.escape(key) + r'":\{[^{}]{0,900}?"amount":([0-9.]+)', page)
        if not pm:
            continue
        try:
            raw = json.loads(dims_json)
        except ValueError:
            continue
        dims, chip, cores = {}, None, None
        for k, v in raw.items():
            low = k.lower()
            name = low.split("dimension", 1)[1] if "dimension" in low else ""
            if name == "chip":
                chip = str(v)
            elif name.startswith("chip-") or "corecount" in low:
                cores = str(v)
            else:
                name = DIM_KEY_MAP.get(name, name)
                dims[name] = pretty_value(name, str(v))
        if chip or cores:
            dims["chip"] = pretty_chip(chip, cores)
        sig = (key, tuple(sorted(dims.items())))
        if sig in seen:
            continue
        seen.add(sig)
        out.append((dims, float(pm.group(1))))
    return fold_color_into_material(out)


def fold_color_into_material(skus):
    """Watch colours belong to one case material (Natural is titanium, Space Gray is aluminum...),
    so offering every colour under every material would be wrong — merge them into one option."""
    if not skus or not all("color" in d and "material" in d for d, _ in skus):
        return skus
    owner: dict[str, set] = {}
    for d, _ in skus:
        owner.setdefault(d["color"], set()).add(d["material"])
    if len({m for ms in owner.values() for m in ms}) < 2 or any(len(ms) != 1 for ms in owner.values()):
        return skus
    folded = []
    for d, price in skus:
        d = dict(d)
        d["material"] = f"{d['material']} \u00b7 {d.pop('color')}"
        folded.append((d, price))
    return folded


# ---------------------------------------------------------------------------
# Turning a list of (dims, price) into base price + per-option deltas
# ---------------------------------------------------------------------------

def _solve(A, y):
    n = len(A[0])
    M = [[sum(r[i] * r[j] for r in A) + (1e-9 if i == j else 0.0) for j in range(n)]
         + [sum(r[i] * yk for r, yk in zip(A, y))] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c and M[c][c] != 0:
                f = M[r][c] / M[c][c]
                M[r] = [a - f * b for a, b in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] for i in range(n)]


def build_variants(skus: list[tuple[dict, float]]):
    """-> (base_price, variants, max_error). Prices are modelled additively:
    price = base + sum(option deltas), fitted by least squares."""
    dims = [d for d in DIM_ORDER if any(d in s for s, _ in skus)]
    dims += sorted({d for s, _ in skus for d in s} - set(dims))
    order = {}
    for d in dims:
        first, minp = {}, {}
        for i, (s, p) in enumerate(skus):
            v = s.get(d, "Standard")
            first.setdefault(v, i)
            minp[v] = min(minp.get(v, p), p)
        order[d] = sorted(minp, key=lambda v: (minp[v], first[v]))
    cols = [("base", None)] + [(d, v) for d in dims for v in order[d][1:]]
    A = [[1.0] + [1.0 if s.get(d, "Standard") == v else 0.0 for d, v in cols[1:]] for s, _ in skus]
    y = [p for _, p in skus]
    x = _solve(A, y) if len(cols) > 1 else [sum(y) / len(y)]
    max_err = max(abs(sum(a * b for a, b in zip(row, x)) - yk) for row, yk in zip(A, y))
    delta = {(d, v): x[i] for i, (d, v) in enumerate(cols) if i > 0}
    variants = {}
    for d in dims:
        opts, used = [], set()
        for v in order[d]:
            oid = slug(v) or "standard"
            while oid in used:
                oid += "-2"
            used.add(oid)
            dv = round(delta.get((d, v), 0.0), 2)
            opts.append({"id": oid, "label": v, "delta": int(dv) if dv == int(dv) else dv})
        if len(opts) > 1:
            variants[d] = opts
    base = round(x[0], 2)
    return (int(base) if base == int(base) else base), variants, max_err


def make_product(name, category, skus, url=None, note=None):
    base, variants, err = build_variants(skus)
    if err > 0.5:
        print(f"  ~ {name}: prices are not perfectly additive (max error ${err:.0f}) — some option combos may be off",
              file=sys.stderr)
    p = {"id": slug(name), "name": name, "cat": category, "usd": base, "variants": variants}
    if url:
        p["url"] = url
    if note:
        p["note"] = note
    return p


def hermes_name(name: str, fam_slug: str) -> str:
    """Hermès pages reuse the base model's title in their metadata — keep them distinguishable."""
    if "hermes" in fam_slug and "herm" not in name.lower():
        return name.replace("Apple Watch", "Apple Watch Herm\u00e8s", 1)
    return name


def parse_family(page: str, section: str, fam_slug: str):
    """One product-family page -> list of products."""
    category = SECTION_CATEGORY.get(section, "Accessories")
    url = f"{BASE}/shop/{section}/{fam_slug}"
    ld = jsonld_products(page)
    products = []

    skus = parse_sku_list(page)
    if skus:
        groups: dict[str, list] = {}
        for name, price in skus:
            base, dims = split_device_sku(name, fam_slug)
            groups.setdefault(base, []).append((dims, price))
        for base, items in groups.items():
            products.append(make_product(base, category, items, url))
        # One family page can hold several models told apart only by display size in the URL
        # (iPhone 18 Pro = 6.3-inch, Pro Max = 6.9-inch) — remember which is which.
        for size, label in re.findall(r"dimensionScreensize_(\d+_\d+)inch:\s*\{[^}]*?displayText:\s*'Your new ([^']+?)\.'", page):
            for prod in products:
                if prod["name"] == norm(label):
                    prod.setdefault("aliases", []).append(size.replace("_", "-") + "-inch")
        return products

    cfg = parse_configurator(page)
    if cfg and ld:
        products.append(make_product(hermes_name(ld[0]["name"], fam_slug), category, cfg, url))
        return products

    for item in ld[:1]:
        products.append(make_product(
            hermes_name(item["name"], fam_slug), category, [({}, min(item["prices"]))], url,
            note="Starting price — other configurations aren't modelled; edit the price if yours differs."))
    return products


def discover_families(use_cache: bool):
    fams, seen = [], set()

    def add(section, fam):
        if fam in SKIP_SLUGS or (section, fam) in seen:
            return
        seen.add((section, fam))
        fams.append((section, fam))

    for section in LANDING_SECTIONS:
        page = fetch(f"{BASE}/shop/{section}", use_cache)
        if not page:
            continue
        for sect, fam in re.findall(r"/shop/(buy-[a-z0-9-]+)/([a-z0-9-]+)", page):
            if sect == section:
                add(sect, fam)
    for sect, fam in EXTRA_FAMILIES:
        add(sect, fam)
    return fams


# ---------------------------------------------------------------------------
# Accessories
# ---------------------------------------------------------------------------

def discover_accessory_pages(use_cache: bool):
    """-> {part: (slug, category)}"""
    found: dict[str, tuple[str, str]] = {}
    sections = [(s, label) for s, label in ACCESSORY_SECTIONS]
    for sec, label in sections:
        page = fetch(f"{BASE}/shop/accessories/all/{sec}", use_cache)
        if not page or "Page Not Found" in page[:6000]:
            continue
        for pn, sl in re.findall(r"/shop/product/([A-Za-z0-9]+)/a/([a-z0-9\-]+)", page):
            found.setdefault(pn.lower(), (sl, label))
    page = fetch(f"{BASE}/shop/accessories/all", use_cache)
    if page:
        for pn, sl in re.findall(r"/shop/product/([A-Za-z0-9]+)/a/([a-z0-9\-]+)", page):
            found.setdefault(pn.lower(), (sl, "Other"))
    return found


def fetch_accessory(part: str, sl: str, use_cache: bool):
    page = fetch(f"{BASE}/shop/product/{part}/a/{sl}", use_cache)
    if not page:
        return None
    items = jsonld_products(page)
    if not items or not items[0]["name"]:
        return None
    return items[0]["name"], min(items[0]["prices"]), f"{BASE}/shop/product/{part}/a/{sl}"


def scrape_accessories(use_cache: bool, workers: int = 5):
    pages = discover_accessory_pages(use_cache)
    print(f"Accessories: {len(pages)} product pages found — reading prices ({workers} at a time)…")
    results = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_accessory, pn, sl, use_cache): (pn, cat) for pn, (sl, cat) in pages.items()}
        done = 0
        for fut in cf.as_completed(futs):
            pn, cat = futs[fut]
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(futs)}")
            try:
                r = fut.result()
            except Exception as e:
                print(f"  ! {pn}: {e}", file=sys.stderr)
                continue
            if r:
                results[pn] = (r, cat)

    # "Crossbody Strap – Burgundy" + "Crossbody Strap – Blue" -> one product, colour variants
    def split_color(title):
        if " – " in title:
            base, tail = title.rsplit(" – ", 1)
            if len(tail) <= 32 and len(tail.split()) <= 4:
                return base.strip(), tail.strip()
        return title, None

    groups: dict[str, list] = {}
    for pn, ((title, price, url), cat) in results.items():
        base, color = split_color(title)
        groups.setdefault(base, []).append((color, price, url, cat, title, pn))
    products = []
    for base, items in groups.items():
        cat = "Accessories \u2014 " + items[0][3]
        if len(items) > 1 and all(i[0] for i in items):
            skus = [({"color": i[0]}, i[1]) for i in items]
            prod = make_product(base, cat, skus, items[0][2])
            prod["parts"] = [i[5] for i in items]
            products.append(prod)
        else:
            for color, price, url, c, title, pn in items:
                prod = make_product(title, "Accessories \u2014 " + c, [({}, price)], url)
                prod["parts"] = [pn]
                products.append(prod)
    return products


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def unique_ids(products):
    seen = {}
    for p in products:
        base = p["id"] or "product"
        n = seen.get(base, 0)
        seen[base] = n + 1
        if n:
            p["id"] = f"{base}-{n + 1}"
    return products


def scrape_devices(use_cache: bool):
    products = []
    fams = discover_families(use_cache)
    print(f"Devices: {len(fams)} product families")
    for section, fam in fams:
        page = fetch(f"{BASE}/shop/{section}/{fam}", use_cache)
        if not page:
            print(f"  - {section}/{fam}: no page")
            continue
        got = parse_family(page, section, fam)
        for p in got:
            p["family"] = fam
        print(f"  - {section}/{fam}: {', '.join(p['name'] for p in got) or 'nothing'}")
        for p in got:                      # the same product can be linked from two family pages
            if not any(q["name"] == p["name"] and q["usd"] == p["usd"] for q in products):
                products.append(p)
    return products


def category_key(cat):
    head = cat.split(" — ")[0]
    return (CATEGORY_ORDER.index(head) if head in CATEGORY_ORDER else 99, cat)


def write_catalog(products, path=OUT_PATH):
    products = unique_ids(products)
    products.sort(key=lambda p: category_key(p["cat"]))  # stable: keeps scrape order within a category
    data = {"generated": date.today().isoformat(), "source": "apple.com US store (en-US)",
            "count": len(products), "products": products}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    return data


def lookup_url(url: str, use_cache: bool):
    """Read a single apple.com link and return the product(s) it describes."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if "product" in parts and len(parts) > parts.index("product") + 1:
        idx = parts.index("product")
        pn, sl = parts[idx + 1], (parts[idx + 3] if len(parts) > idx + 3 else "")
        r = fetch_accessory(pn, sl, use_cache)
        if not r:
            return []
        return [make_product(r[0], "Accessories", [({}, r[1])], r[2])]
    section = fam = None
    if len(parts) >= 3 and parts[0] == "shop" and parts[1].startswith("buy-"):
        section, fam = parts[1], parts[2]
    else:
        last = parts[-1] if parts else ""
        for s, f in discover_families(use_cache):
            if f == last:
                section, fam = s, f
                break
    if not fam:
        return []
    page = fetch(f"{BASE}/shop/{section}/{fam}", use_cache)
    if not page:
        return []
    found = parse_family(page, section, fam)
    for p in found:
        p["family"] = fam
    return found


def main():
    ap = argparse.ArgumentParser(description="Refresh apple_catalog.json from apple.com (US store).")
    ap.add_argument("--devices-only", action="store_true", help="skip the ~300 accessory pages")
    ap.add_argument("--url", help="look up a single apple.com link and print what was found")
    ap.add_argument("--add", action="store_true", help="with --url: merge the result into apple_catalog.json")
    ap.add_argument("--no-cache", action="store_true", help="ignore the on-disk page cache")
    args = ap.parse_args()
    use_cache = not args.no_cache

    if args.url:
        found = lookup_url(args.url, use_cache)
        if not found:
            print("Couldn't find a product page for that link (is it an apple.com/shop link, or a product family page?).")
            sys.exit(1)
        for p in found:
            print(json.dumps(p, indent=1, ensure_ascii=False))
        if args.add:
            data = json.load(open(OUT_PATH, encoding="utf-8")) if os.path.exists(OUT_PATH) else {"products": []}
            by_id = {p["id"]: p for p in data["products"]}
            for p in found:
                by_id[p["id"]] = p
            write_catalog(list(by_id.values()))
            print(f"\nMerged {len(found)} product(s) into {OUT_PATH}. Now run: python3 apple_price_data.py --export-only")
        return

    products = scrape_devices(use_cache)
    if not args.devices_only:
        extra = scrape_accessories(use_cache)
    elif os.path.exists(OUT_PATH):  # keep accessories from the previous run
        old = json.load(open(OUT_PATH, encoding="utf-8"))["products"]
        extra = [p for p in old if p["cat"].startswith("Accessories —")]
    else:
        extra = []
    device_names = {p["name"] for p in products}      # e.g. AirPods are also listed as an accessory
    products += [p for p in extra if p["name"] not in device_names]
    data = write_catalog(products)
    by_cat = {}
    for p in data["products"]:
        by_cat[p["cat"].split(" — ")[0]] = by_cat.get(p["cat"].split(" — ")[0], 0) + 1
    print(f"\nWrote {OUT_PATH}: {data['count']} products — " + ", ".join(f"{k} {v}" for k, v in by_cat.items()))
    print("Next: python3 apple_price_data.py --export-only")


if __name__ == "__main__":
    main()

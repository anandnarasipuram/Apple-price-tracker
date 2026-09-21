#!/usr/bin/env python3
"""
scrape_apple_catalog.py
=======================
Refreshes ``apple_catalog.json`` — Apple's current catalog with the EXACT prices
each Apple online store charges, country by country — straight from apple.com.
``apple_price_data.py`` picks that file up and re-exports it to ``frontend/data.json``.

What it collects
  * The US store is the master catalog: every model, its storage / colour / size /
    chip options, and ~300 accessories.
  * Then it reads every other tracked country's own Apple store (apple.com/uk,
    /de, /jp, ...) and records what that store charges, in local currency, for each
    of those same products and options. Nothing is converted or estimated: a
    product/option a country's store doesn't list simply has no price there.
  * Real exchange rates (for comparing countries), with their date.
  * New products are found by crawling Apple's store pages, not from a fixed list.

Why a Python script and not part of the website: apple.com doesn't send CORS
headers, so a browser page can't read it. A local script (or a scheduled job) can.

    python3 scrape_apple_catalog.py                     # everything (US + all countries + accessories)
    python3 scrape_apple_catalog.py --devices-only      # skip accessories (much faster)
    python3 scrape_apple_catalog.py --regions DE,JP     # only refresh these countries' prices
    python3 scrape_apple_catalog.py --no-regional       # US store only
    python3 scrape_apple_catalog.py --no-regional-accessories
    python3 scrape_apple_catalog.py --url https://www.apple.com/shop/buy-iphone/iphone-18-pro --add
    python3 scrape_apple_catalog.py --no-cache          # ignore the 12-hour on-disk page cache

How prices are read (all of it is embedded in apple.com's own HTML):
  * iPhone / iPad / displays / AirPods / TV / HomePod / AirTag: the per-SKU price list.
  * Mac / Apple Watch: the configurator's price table.
  * Vision Pro and anything without a table: the schema.org starting price.
  * Accessories: the product page (matched across countries by Apple part number).

Standard library only.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import gzip
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(ROOT, "apple_catalog.json")
CACHE_DIR = os.path.join(ROOT, ".apple_cache")
BASE = "https://www.apple.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FX_URL = "https://open.er-api.com/v6/latest/USD"
FX_SOURCE = "open.er-api.com (ExchangeRate-API), daily rates"

# Every country the app tracks -> its Apple storefront path prefix, and whether the
# price Apple displays already includes sales tax. Apple's own footnotes say so:
# US "Tax ... due at sale", Canada "does not include applicable taxes"; UAE "inclusive
# of VAT (5%)", Mexico "incluyen IVA (16%)", Singapore "inclusive of GST", NZ "include GST".
REGIONS = {
    "US": {"prefix": "",      "includes_tax": False},
    "GB": {"prefix": "uk",    "includes_tax": True},
    "DE": {"prefix": "de",    "includes_tax": True},
    "CH": {"prefix": "ch-de", "includes_tax": True},
    "AE": {"prefix": "ae",    "includes_tax": True},
    "JP": {"prefix": "jp",    "includes_tax": True},
    "SG": {"prefix": "sg",    "includes_tax": True},
    "IN": {"prefix": "in",    "includes_tax": True},
    "AU": {"prefix": "au",    "includes_tax": True},
    "HK": {"prefix": "hk",    "includes_tax": True},   # no sales tax in Hong Kong: shown price is final
    "CA": {"prefix": "ca",    "includes_tax": False},
    "TW": {"prefix": "tw",    "includes_tax": True},
    "MO": {"prefix": "mo",    "includes_tax": True},
    "KR": {"prefix": "kr",    "includes_tax": True},
    "MY": {"prefix": "my",    "includes_tax": True},
    "TH": {"prefix": "th",    "includes_tax": True},
    "QA": {"prefix": "qa",    "includes_tax": True},
    "NZ": {"prefix": "nz",    "includes_tax": True},
    "MX": {"prefix": "mx",    "includes_tax": True},
    "SA": {"prefix": "sa",    "includes_tax": True},
}

# shop section -> product category shown in the app's product picker
SECTION_CATEGORY = {
    "buy-iphone": "iPhone", "buy-ipad": "iPad", "buy-mac": "Mac", "buy-watch": "Watch",
    "buy-vision": "Vision", "buy-airpods": "Audio", "buy-homepod": "Audio",
    "buy-tv": "Home", "buy-airtag": "Accessories",
}
CATEGORY_ORDER = ["iPhone", "iPad", "Mac", "Watch", "Vision", "Audio", "Home", "Accessories"]

# Where to look for product families. Every "/shop/buy-<section>/<family>" link found on
# these pages (and on each section's own landing page) is followed, so a new product — or
# a whole new section — appears without touching this file. EXTRA_FAMILIES is only a safety net.
DISCOVERY_SEEDS = ["/store", "/shop/accessories/all"]
LANDING_SECTIONS = ["buy-iphone", "buy-ipad", "buy-mac", "buy-watch", "buy-vision"]
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
ADDITIVE_TOLERANCE = 0.51      # local-currency units; anything looser is stored as an exact price table


# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------

def store_url(prefix: str, path: str) -> str:
    """apple.com URL for a path inside one country's store ('' prefix = US)."""
    return f"{BASE}/{prefix + '/' if prefix else ''}{path.lstrip('/')}"


def fetch(url: str, use_cache: bool = True, ttl: int = 12 * 3600):
    """GET a page (text). Returns None for 403/404 (no such page) or after repeated failures."""
    path = os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".html.gz")
    if use_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return f.read()
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", errors="ignore")
            if use_cache:
                with gzip.open(path, "wt", encoding="utf-8", compresslevel=3) as f:
                    f.write(body)
            return body
        except urllib.error.HTTPError as e:
            # 403/404, and Apple's own non-standard 541 ("not available in this store"), mean "no such
            # page here" — answer immediately instead of retrying. Anything else may be transient.
            if e.code in (403, 404, 541) or (400 <= e.code < 500 and e.code not in (408, 429)):
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
    s = s.replace(" ", " ").replace("‑", "-").replace("‐", "-")
    s = re.sub("[‎‏‪-‮]", "", s)      # invisible left/right-to-left marks
    return re.sub(r"\s+", " ", s).strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(s).lower()).strip("-")


def num(x: float):
    """2-decimal rounding that keeps whole numbers as ints (smaller JSON)."""
    x = round(x, 2)
    return int(x) if x == int(x) else x


def jsonld_products(page: str) -> list[dict]:
    """schema.org Product blocks -> [{'name', 'prices': [...], 'currency'}]."""
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
            prices, currency = [], None
            for o in offers:
                if not isinstance(o, dict):
                    continue
                p = o.get("price", o.get("lowPrice"))
                try:
                    prices.append(float(p))
                except (TypeError, ValueError):
                    continue
                currency = currency or o.get("priceCurrency")
            if prices:
                out.append({"name": norm(item.get("name", "")), "prices": prices, "currency": currency})
    return out


def page_currency(page: str):
    m = re.search(r'"currency(?:Code)?":"([A-Z]{3})"', page)
    return m.group(1) if m else None


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
    return " · ".join(parts)


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
        d["material"] = f"{d['material']} · {d.pop('color')}"
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


def fit_skus(skus: list[tuple[dict, float]]):
    """Model price = base + sum(option deltas), fitted by least squares.
    -> {'base', 'dims': {dim: [(label, id, delta), ...]}, 'err'}   (err = worst miss on any real SKU)"""
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
    err = max(abs(sum(a * b for a, b in zip(row, x)) - yk) for row, yk in zip(A, y))
    delta = {(d, v): x[i] for i, (d, v) in enumerate(cols) if i > 0}
    out = {}
    for d in dims:
        opts, used = [], set()
        for v in order[d]:
            oid = slug(v) or "standard"
            while oid in used:
                oid += "-2"
            used.add(oid)
            opts.append((v, oid, num(delta.get((d, v), 0.0))))
        out[d] = opts
    return {"base": num(x[0]), "dims": out, "err": err}


def build_variants(skus):
    """-> (base_price, variants, max_error) for the app's product record."""
    f = fit_skus(skus)
    variants = {d: [{"id": oid, "label": label, "delta": dv} for label, oid, dv in opts]
                for d, opts in f["dims"].items() if len(opts) > 1}
    return f["base"], variants, f["err"]


def regional_entry(skus, currency, url):
    """What one country's Apple store charges for a product, in that country's currency.
    Additive (base + option deltas) when Apple's local prices really are; otherwise the exact
    price of every SKU is kept as a table, so nothing is ever approximated."""
    f = fit_skus(skus)
    entry = {"currency": currency, "url": url}
    if f["err"] <= ADDITIVE_TOLERANCE:
        entry["base"] = f["base"]
        entry["opts"] = {d: {oid: dv for _, oid, dv in opts} for d, opts in f["dims"].items()}
    else:
        ids = {d: {label: oid for label, oid, _ in opts} for d, opts in f["dims"].items()}
        entry["table"] = [{"o": {d: ids[d][dims.get(d, "Standard")] for d in ids}, "p": num(price)}
                          for dims, price in skus]
    return entry


def make_product(name, category, skus, url=None, note=None, warn=True):
    base, variants, err = build_variants(skus)
    if warn and err > 0.5:
        print(f"  ~ {name}: US prices are not perfectly additive (max error ${err:.0f})", file=sys.stderr)
    p = {"id": slug(name), "name": name, "cat": category, "usd": base, "variants": variants, "_skus": skus}
    if url:
        p["url"] = url
    if note:
        p["note"] = note
    return p


def hermes_name(name: str, fam_slug: str) -> str:
    """Hermès pages reuse the base model's title in their metadata — keep them distinguishable."""
    if "hermes" in fam_slug and "herm" not in name.lower():
        return name.replace("Apple Watch", "Apple Watch Hermès", 1)
    return name


def parse_family(page: str, section: str, fam_slug: str, warn: bool = True):
    """One product-family page -> list of products (each carries its raw SKUs in '_skus')."""
    category = SECTION_CATEGORY.get(section) or section.replace("buy-", "").replace("-", " ").title()
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
            products.append(make_product(base, category, items, url, warn=warn))
        # One family page can hold several models told apart only by display size in the URL
        # (iPhone 18 Pro = 6.3-inch, Pro Max = 6.9-inch) — remember which is which.
        for size, label in re.findall(r"dimensionScreensize_(\d+_\d+)inch:\s*\{[^}]*?displayText:\s*'Your new ([^']+?)\.'", page):
            for prod in products:
                if prod["name"] == norm(label):
                    prod.setdefault("aliases", []).append(size.replace("_", "-") + "-inch")
        return products

    cfg = parse_configurator(page)
    if cfg and ld:
        products.append(make_product(hermes_name(ld[0]["name"], fam_slug), category, cfg, url, warn=warn))
        return products

    for item in ld[:1]:
        products.append(make_product(
            hermes_name(item["name"], fam_slug), category, [({}, min(item["prices"]))], url,
            note="Starting price — other configurations aren't modelled; edit the price if yours differs.", warn=warn))
    return products


def discover_families(use_cache: bool):
    """Follow every /shop/buy-<section>/<family> link on Apple's store pages."""
    fams, seen, sections = [], set(), list(LANDING_SECTIONS)

    def add(section, fam):
        if fam in SKIP_SLUGS or (section, fam) in seen:
            return
        seen.add((section, fam))
        fams.append((section, fam))
        if section not in sections:
            sections.append(section)

    def harvest(page):
        for sect, fam in re.findall(r"/shop/(buy-[a-z0-9-]+)/([a-z0-9-]+)", page or ""):
            add(sect, fam)

    for seed in DISCOVERY_SEEDS:
        harvest(fetch(BASE + seed, use_cache))
    i = 0
    while i < len(sections):          # sections can grow while we walk them (a brand-new one)
        page = fetch(f"{BASE}/shop/{sections[i]}", use_cache)
        if page:
            harvest(page)
        i += 1
    for sect, fam in EXTRA_FAMILIES:
        add(sect, fam)
    return fams


# ---------------------------------------------------------------------------
# Accessories (US master list)
# ---------------------------------------------------------------------------

def discover_accessory_pages(use_cache: bool):
    """-> {part: (slug, category)}"""
    found: dict[str, tuple[str, str]] = {}
    for sec, label in ACCESSORY_SECTIONS:
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


def fetch_accessory(part: str, sl: str, use_cache: bool, prefix: str = ""):
    """(title, price, currency, url) of one accessory product page, or None. The part number alone
    identifies the product, so the same call works against any country's store."""
    url = store_url(prefix, f"shop/product/{part}/a/{sl or 'x'}")
    page = fetch(url, use_cache)
    if not page:
        return None
    items = jsonld_products(page)
    if not items or not items[0]["name"]:
        return None
    return items[0]["name"], min(items[0]["prices"]), items[0]["currency"] or page_currency(page), url


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
            if done % 100 == 0:
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
    for pn, ((title, price, _cur, url), cat) in results.items():
        base, color = split_color(title)
        groups.setdefault(base, []).append((color, price, url, cat, title, pn))
    products = []
    for base, items in groups.items():
        cat = "Accessories — " + items[0][3]
        if len(items) > 1 and all(i[0] for i in items):
            skus = [({"color": i[0]}, i[1]) for i in items]
            prod = make_product(base, cat, skus, items[0][2])
            prod["parts"] = [i[5] for i in items]
            prod["_part_dims"] = {i[5]: {"color": i[0]} for i in items}
            products.append(prod)
        else:
            for color, price, url, c, title, pn in items:
                prod = make_product(title, "Accessories — " + c, [({}, price)], url)
                prod["parts"] = [pn]
                prod["_part_dims"] = {pn: {}}
                products.append(prod)
    return products


# ---------------------------------------------------------------------------
# Other countries: what each Apple store actually charges
# ---------------------------------------------------------------------------

def attach_regional_devices(products, fams, use_cache, regions, workers=6):
    """For each country: read the same product-family pages from that country's Apple store and
    record its exact local prices against the matching US product (matched by English name)."""
    by_name = {p["name"]: p for p in products if not p["cat"].startswith("Accessories —")}
    jobs = [(cc, sec, fam) for cc in regions if cc != "US" for sec, fam in fams]
    print(f"Countries: {len(regions) - ('US' in regions)} stores × {len(fams)} product families = {len(jobs)} pages…")

    def work(job):
        cc, sec, fam = job
        pref = REGIONS[cc]["prefix"]
        page = fetch(store_url(pref, f"shop/{sec}/{fam}"), use_cache)
        if not page:
            return job, None, []
        cur = page_currency(page)
        found = []
        for rp in parse_family(page, sec, fam, warn=False):
            us = by_name.get(rp["name"])
            if us:
                found.append((us["id"], regional_entry(rp["_skus"], cur, store_url(pref, f"shop/{sec}/{fam}"))))
        return job, cur, found

    by_id = {p["id"]: p for p in products}
    info = {cc: {"currency": None, "priced": set()} for cc in regions if cc != "US"}
    t0, done = time.time(), 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for job, cur, found in pool.map(work, jobs):
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(jobs)} pages ({time.time() - t0:.0f}s)")
            cc = job[0]
            if cur:
                info[cc]["currency"] = info[cc]["currency"] or cur
            for pid, entry in found:
                by_id[pid].setdefault("regional", {})[cc] = entry
                info[cc]["priced"].add(pid)
    return info


def attach_regional_accessories(products, use_cache, regions, workers=6):
    """Same part number, each country's store. A US-only accessory just won't exist abroad."""
    acc = [p for p in products if p.get("parts")]
    jobs = [(cc, p["id"], part) for cc in regions if cc != "US" for p in acc for part in p["parts"]]
    print(f"Accessories abroad: {len(acc)} products × {len(set(j[0] for j in jobs))} stores = {len(jobs)} lookups…")
    by_id = {p["id"]: p for p in acc}

    def work(job):
        cc, pid, part = job
        r = fetch_accessory(part, "", False, REGIONS[cc]["prefix"])     # not cached: thousands of small pages
        return job, r

    found: dict[tuple, dict] = {}
    t0, done = time.time(), 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for job, r in pool.map(work, jobs):
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(jobs)} ({time.time() - t0:.0f}s)")
            if r:
                cc, pid, part = job
                found.setdefault((cc, pid), {})[part] = r
    priced = {cc: set() for cc in regions if cc != "US"}
    for (cc, pid), parts in found.items():
        prod = by_id[pid]
        skus = [(prod["_part_dims"].get(part, {}), r[1]) for part, r in parts.items()]
        cur = next(r[2] for r in parts.values())
        url = next(r[3] for r in parts.values())
        prod.setdefault("regional", {})[cc] = regional_entry(skus, cur, url)
        priced[cc].add(pid)
    return priced


# ---------------------------------------------------------------------------
# Exchange rates
# ---------------------------------------------------------------------------

def fetch_fx(currencies):
    """Real market rates (units of each currency per 1 USD) with their date."""
    try:
        req = urllib.request.Request(FX_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        rates = data["rates"]
        when = parsedate_to_datetime(data["time_last_update_utc"]).date().isoformat()
        missing = [c for c in currencies if c not in rates]
        if missing:
            print(f"  ! no exchange rate for {missing}", file=sys.stderr)
        return {"date": when, "source": FX_SOURCE, "rates": {c: rates[c] for c in sorted(currencies) if c in rates}}
    except Exception as e:
        print(f"  ! exchange-rate fetch failed ({e}) — keeping the previous rates, if any", file=sys.stderr)
        return None


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


def scrape_devices(fams, use_cache):
    products = []
    print(f"US store: {len(fams)} product families")
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


def clean(product):
    return {k: v for k, v in product.items() if not k.startswith("_")}


def write_catalog(products, meta=None, path=OUT_PATH):
    products = unique_ids(products)
    products.sort(key=lambda p: category_key(p["cat"]))  # stable: keeps scrape order within a category
    data = {"generated": date.today().isoformat(), "source": "apple.com — each country's own Apple online store",
            "count": len(products)}
    data.update(meta or {})
    data["products"] = [clean(p) for p in products]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    return data


def lookup_url(url: str, use_cache: bool, regions):
    """Read a single apple.com link (US master + every country's price) and return the product(s)."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", parts[0]) and parts[0] != "us":
        parts = parts[1:]
    if "product" in parts and len(parts) > parts.index("product") + 1:
        pn = parts[parts.index("product") + 1]
        r = fetch_accessory(pn, "", use_cache)
        if not r:
            return []
        prod = make_product(r[0], "Accessories", [({}, r[1])], r[3])
        prod["parts"] = [pn]
        prod["_part_dims"] = {pn: {}}
        attach_regional_accessories([prod], use_cache, regions)
        return [prod]
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
    attach_regional_devices(found, [(section, fam)], use_cache, regions)
    return found


def previous_catalog():
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            pass
    return {"products": []}


def main():
    ap = argparse.ArgumentParser(description="Refresh apple_catalog.json from Apple's online stores.")
    ap.add_argument("--devices-only", action="store_true", help="skip accessories (US and abroad)")
    ap.add_argument("--regions", help="comma-separated country codes to (re)read, e.g. DE,JP (default: all)")
    ap.add_argument("--no-regional", action="store_true", help="US store only — no other countries")
    ap.add_argument("--no-regional-accessories", action="store_true", help="don't look up accessories abroad")
    ap.add_argument("--url", help="look up a single apple.com link and print what was found")
    ap.add_argument("--add", action="store_true", help="with --url: merge the result into apple_catalog.json")
    ap.add_argument("--no-cache", action="store_true", help="ignore the on-disk page cache")
    args = ap.parse_args()
    use_cache = not args.no_cache

    wanted = [c.strip().upper() for c in args.regions.split(",")] if args.regions else list(REGIONS)
    bad = [c for c in wanted if c not in REGIONS]
    if bad:
        sys.exit(f"Unknown country code(s): {bad}. Known: {', '.join(REGIONS)}")
    regions = [] if args.no_regional else [c for c in wanted if c != "US"]

    prev = previous_catalog()
    prev_regions = prev.get("regions", {})

    if args.url:
        found = lookup_url(args.url, use_cache, regions)
        if not found:
            print("Couldn't find a product page for that link (is it an apple.com/shop link, or a product family page?).")
            sys.exit(1)
        for p in found:
            c = clean(p)
            reg = c.pop("regional", {})
            print(json.dumps(c, indent=1, ensure_ascii=False))
            print("  prices abroad:", {cc: (e.get("base", "table"), e["currency"]) for cc, e in sorted(reg.items())})
        if args.add:
            by_id = {p["id"]: p for p in prev["products"]}
            for p in found:
                by_id[p["id"]] = clean(p)
            meta = {k: prev[k] for k in ("fx", "regions") if k in prev}
            write_catalog(list(by_id.values()), meta)
            print(f"\nMerged {len(found)} product(s) into {OUT_PATH}. Now run: python3 apple_price_data.py --export-only")
        return

    fams = discover_families(use_cache)
    products = scrape_devices(fams, use_cache)
    acc = []
    if not args.devices_only:
        acc = scrape_accessories(use_cache)
    else:                                           # keep accessories (and their prices abroad) from the last run
        acc = [p for p in prev["products"] if p["cat"].startswith("Accessories —")]
    device_names = {p["name"] for p in products}      # e.g. AirPods are also listed as an accessory
    acc = [p for p in acc if p["name"] not in device_names]

    region_info = {}
    if regions:
        info = attach_regional_devices(products, fams, use_cache, regions)
        for cc, i in info.items():
            region_info[cc] = {"currency": i["currency"], "devices": len(i["priced"])}
    if regions and acc and not args.devices_only and not args.no_regional_accessories:
        ok_regions = [c for c in regions if region_info.get(c, {}).get("devices")]
        priced = attach_regional_accessories(acc, use_cache, ok_regions)
        for cc, s in priced.items():
            region_info[cc]["accessories"] = len(s)
    products += acc

    # ---- region metadata: status, currency, tax treatment, coverage
    n_dev = len([p for p in products if not p["cat"].startswith("Accessories —")])
    n_acc = len(acc)
    meta_regions = {}
    for cc, r in REGIONS.items():
        old = prev_regions.get(cc, {})
        if cc == "US":
            meta_regions[cc] = {"prefix": "", "currency": "USD", "includes_tax": False, "status": "ok",
                                "devices": n_dev, "accessories": n_acc}
            continue
        if cc in region_info:
            i = region_info[cc]
            status = "ok" if i["devices"] else "no_store"
            meta_regions[cc] = {"prefix": r["prefix"], "currency": i["currency"] or old.get("currency"),
                                "includes_tax": r["includes_tax"], "status": status,
                                "devices": i["devices"], "accessories": i.get("accessories", old.get("accessories", 0))}
        elif old:
            meta_regions[cc] = old          # not refreshed this run — keep what we knew
        else:
            meta_regions[cc] = {"prefix": r["prefix"], "currency": None, "includes_tax": r["includes_tax"],
                                "status": "not_scraped", "devices": 0, "accessories": 0}
    currencies = {r["currency"] for r in meta_regions.values() if r.get("currency")} | {"USD"}
    fx = fetch_fx(currencies) or prev.get("fx")
    data = write_catalog(products, {"fx": fx, "regions": meta_regions})

    # ---- report
    by_cat = {}
    for p in data["products"]:
        h = p["cat"].split(" — ")[0]
        by_cat[h] = by_cat.get(h, 0) + 1
    print(f"\nWrote {OUT_PATH}: {data['count']} products — " + ", ".join(f"{k} {v}" for k, v in by_cat.items()))
    print(f"Exchange rates: {fx['date'] if fx else 'none'}\n")
    print(f"{'':4}{'store':<9}{'currency':<9}{'devices priced':<18}{'accessories priced':<20}")
    for cc, r in meta_regions.items():
        dev = f"{r['devices']}/{n_dev}" if r["status"] == "ok" else "—"
        acs = f"{r.get('accessories', 0)}/{n_acc}" if r["status"] == "ok" else "—"
        print(f"{cc:<4}{r['status']:<9}{(r.get('currency') or '-'):<9}{dev:<18}{acs:<20}")
    print("\nNext: python3 apple_price_data.py --export-only")


if __name__ == "__main__":
    main()

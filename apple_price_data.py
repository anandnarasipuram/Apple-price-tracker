#!/usr/bin/env python3
"""
apple_price_data.py
====================
Python port of the pricing data & logic from ``tariff-board.html``.

This module is the single source of truth for:
  - the Apple product catalog (prices + configurable variants)
  - international country tax / FX / tourist-refund assumptions
  - US state sales-tax rates (+ ZIP-code -> state lookup) and Canadian
    province tax rates
  - the pricing formulas themselves (ex-tax price, tourist refund cash-back,
    employee discount, landed/shipping cost, etc.)

Run it directly to:
  1. print a quick "cheapest / most expensive US state" report for a product, and
  2. export ``frontend/data.json`` — the static data file the Netlify frontend
     (in ``frontend/``) reads to render the interactive USA price map.

    python3 apple_price_data.py                       # default product (iPhone 17 Pro)
    python3 apple_price_data.py --product ip17pm       # iPhone 17 Pro Max
    python3 apple_price_data.py --list-products
    python3 apple_price_data.py --export-only          # just (re)write frontend/data.json

No third-party dependencies — standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Product catalog — September 2026 US Apple Store list prices (USD)
# ---------------------------------------------------------------------------

def _colors(*names):
    """Color variant options — cosmetic only, never affects price (delta 0)."""
    return [{"id": n.lower().replace(" ", "-"), "label": n, "delta": 0} for n in names]


PRODUCTS = [
    {"id": "ip17", "name": "iPhone 17", "cat": "iPhone", "usd": 799,
     "variants": {"storage": [{"id": "256", "label": "256GB", "delta": 0},
                               {"id": "512", "label": "512GB", "delta": 200}],
                  "color": _colors("Black", "White", "Lavender", "Sage", "Mist Blue")}},
    {"id": "ipair", "name": "iPhone Air", "cat": "iPhone", "usd": 999,
     "variants": {"storage": [{"id": "256", "label": "256GB", "delta": 0},
                               {"id": "512", "label": "512GB", "delta": 200},
                               {"id": "1tb", "label": "1TB", "delta": 400}],
                  "color": _colors("Space Black", "Sky Blue", "Light Gold", "Cloud White")}},
    {"id": "ip17pro", "name": "iPhone 17 Pro", "cat": "iPhone", "usd": 1099,
     "variants": {"storage": [{"id": "256", "label": "256GB", "delta": 0},
                               {"id": "512", "label": "512GB", "delta": 200},
                               {"id": "1tb", "label": "1TB", "delta": 400}],
                  "color": _colors("Deep Blue", "Cosmic Orange", "Silver")}},
    {"id": "ip17pm", "name": "iPhone 17 Pro Max", "cat": "iPhone", "usd": 1199,
     "variants": {"storage": [{"id": "256", "label": "256GB", "delta": 0},
                               {"id": "512", "label": "512GB", "delta": 200},
                               {"id": "1tb", "label": "1TB", "delta": 400},
                               {"id": "2tb", "label": "2TB", "delta": 600}],
                  "color": _colors("Deep Blue", "Cosmic Orange", "Silver")}},
    {"id": "ipad", "name": "iPad (A16)", "cat": "iPad", "usd": 449,
     "variants": {"storage": [{"id": "128", "label": "128GB", "delta": 0},
                               {"id": "256", "label": "256GB", "delta": 150}],
                  "color": _colors("Blue", "Pink", "Yellow", "Silver")}},
    {"id": "ipadair", "name": "iPad Air", "cat": "iPad", "usd": 749,
     "variants": {"size": [{"id": "11", "label": "11-inch", "delta": 0},
                            {"id": "13", "label": "13-inch", "delta": 200}],
                  "storage": [{"id": "128", "label": "128GB", "delta": 0},
                               {"id": "256", "label": "256GB", "delta": 100},
                               {"id": "512", "label": "512GB", "delta": 300}],
                  "color": _colors("Space Gray", "Blue", "Purple", "Starlight")}},
    {"id": "ipadpro", "name": "iPad Pro", "cat": "iPad", "usd": 1199,
     "variants": {"size": [{"id": "11", "label": "11-inch", "delta": 0},
                            {"id": "13", "label": "13-inch", "delta": 200}],
                  "storage": [{"id": "256", "label": "256GB", "delta": 0},
                               {"id": "512", "label": "512GB", "delta": 200},
                               {"id": "1tb", "label": "1TB", "delta": 400},
                               {"id": "2tb", "label": "2TB", "delta": 800}],
                  "color": _colors("Space Black", "Silver")}},
    {"id": "mba", "name": "MacBook Air (M4)", "cat": "Mac", "usd": 999,
     "variants": {"storage": [{"id": "256", "label": "256GB", "delta": 0},
                               {"id": "512", "label": "512GB", "delta": 200},
                               {"id": "1tb", "label": "1TB", "delta": 400}],
                  "color": _colors("Midnight", "Starlight", "Space Gray", "Sky Blue")}},
    {"id": "mbp", "name": 'MacBook Pro 14" (M5)', "cat": "Mac", "usd": 1599,
     "variants": {"storage": [{"id": "512", "label": "512GB", "delta": 0},
                               {"id": "1tb", "label": "1TB", "delta": 200},
                               {"id": "2tb", "label": "2TB", "delta": 600}],
                  "color": _colors("Space Black", "Silver")}},
    {"id": "watch", "name": "Apple Watch Series 11", "cat": "Watch", "usd": 399,
     "variants": {"size": [{"id": "41", "label": "41mm", "delta": 0},
                            {"id": "45", "label": "45mm", "delta": 30}],
                  "cellular": [{"id": "gps", "label": "GPS", "delta": 0},
                               {"id": "cell", "label": "GPS + Cellular", "delta": 100}],
                  "color": _colors("Midnight", "Starlight", "Rose Gold", "Jet Black")}},
    {"id": "airpods", "name": "AirPods Pro 3", "cat": "Audio", "usd": 249, "variants": {}},
]


# ---------------------------------------------------------------------------
# International country assumptions (tax, FX vs USD, regional pricing
# premium, tourist VAT/GST refund availability + recovery rate)
# ---------------------------------------------------------------------------

COUNTRIES = [
    {"code": "US", "name": "United States", "flag": "🇺🇸", "currency": "USD", "fx": 1,
     "tax_rate": 0.00, "tax_name": "Sales tax (varies, excluded)", "premium": 1.00, "refund": None,
     "note": "No national VAT; local sales tax added at register. Look up by ZIP."},
    {"code": "DE", "name": "Germany", "flag": "🇩🇪", "currency": "EUR", "fx": 0.86,
     "tax_rate": 0.19, "tax_name": "VAT", "premium": 1.05, "refund": 0.80,
     "note": "Non-EU visitors can reclaim VAT (Global Blue / Planet) when leaving the EU."},
    {"code": "GB", "name": "United Kingdom", "flag": "🇬🇧", "currency": "GBP", "fx": 0.75,
     "tax_rate": 0.20, "tax_name": "VAT", "premium": 1.03, "refund": None,
     "note": "Tourist VAT refund scheme was abolished in 2021."},
    {"code": "CH", "name": "Switzerland", "flag": "🇨🇭", "currency": "CHF", "fx": 0.80,
     "tax_rate": 0.081, "tax_name": "VAT", "premium": 1.08, "refund": 0.85,
     "note": "Refund available above a minimum receipt value, claimed at the border."},
    {"code": "AE", "name": "UAE", "flag": "🇦🇪", "currency": "AED", "fx": 3.67,
     "tax_rate": 0.05, "tax_name": "VAT", "premium": 1.00, "refund": 0.85,
     "note": "Planet Tax Free refund at airport kiosks for tourists."},
    {"code": "JP", "name": "Japan", "flag": "🇯🇵", "currency": "JPY", "fx": 147,
     "tax_rate": 0.10, "tax_name": "Consumption tax", "premium": 1.00, "refund": 0.90,
     "note": "Tax-free shopping at point of sale for tourists with passport."},
    {"code": "SG", "name": "Singapore", "flag": "🇸🇬", "currency": "SGD", "fx": 1.29,
     "tax_rate": 0.09, "tax_name": "GST", "premium": 1.05, "refund": 0.85,
     "note": "eTRS refund at Changi Airport for tourists."},
    {"code": "IN", "name": "India", "flag": "🇮🇳", "currency": "INR", "fx": 88,
     "tax_rate": 0.18, "tax_name": "GST", "premium": 1.15, "refund": None,
     "note": "No tourist GST refund scheme; import duty pushes prices higher."},
    {"code": "AU", "name": "Australia", "flag": "🇦🇺", "currency": "AUD", "fx": 1.52,
     "tax_rate": 0.10, "tax_name": "GST", "premium": 1.05, "refund": 0.95,
     "note": "Tourist Refund Scheme (TRS) refunds nearly the full GST over AUD 300."},
    {"code": "HK", "name": "Hong Kong", "flag": "🇭🇰", "currency": "HKD", "fx": 7.79,
     "tax_rate": 0.00, "tax_name": "None", "premium": 0.95, "refund": None,
     "note": "No VAT/GST at all — often the cheapest list price in the region."},
    {"code": "CA", "name": "Canada", "flag": "🇨🇦", "currency": "CAD", "fx": 1.38,
     "tax_rate": 0.13, "tax_name": "HST (ON)", "premium": 1.05, "refund": None,
     "note": "Visitor rebate discontinued in 2007. Pick a province separately."},

    # --- Added for broader "cheapest countries to buy an iPhone" coverage ---
    {"code": "TW", "name": "Taiwan", "flag": "🇹🇼", "currency": "TWD", "fx": 32.0,
     "tax_rate": 0.05, "tax_name": "VAT", "premium": 1.00, "refund": 0.80,
     "note": "One of the lowest VAT rates of any Apple Store market — consistently cheap."},
    {"code": "MO", "name": "Macau", "flag": "🇲🇴", "currency": "MOP", "fx": 8.0,
     "tax_rate": 0.00, "tax_name": "None", "premium": 0.97, "refund": None,
     "note": "No sales tax, like neighbouring Hong Kong — among the cheapest list prices."},
    {"code": "KR", "name": "South Korea", "flag": "🇰🇷", "currency": "KRW", "fx": 1380,
     "tax_rate": 0.10, "tax_name": "VAT", "premium": 1.00, "refund": 0.75,
     "note": "Tax refund kiosks (Global Blue) at Incheon for tourists on qualifying purchases."},
    {"code": "MY", "name": "Malaysia", "flag": "🇲🇾", "currency": "MYR", "fx": 4.7,
     "tax_rate": 0.00, "tax_name": "SST (exempt)", "premium": 1.05, "refund": None,
     "note": "Smartphones are currently exempt from Malaysia's Sales & Service Tax."},
    {"code": "TH", "name": "Thailand", "flag": "🇹🇭", "currency": "THB", "fx": 36.0,
     "tax_rate": 0.07, "tax_name": "VAT", "premium": 1.05, "refund": 0.85,
     "note": "VAT refund for tourists at the airport on departure, above a minimum spend."},
    {"code": "QA", "name": "Qatar", "flag": "🇶🇦", "currency": "QAR", "fx": 3.64,
     "tax_rate": 0.00, "tax_name": "None", "premium": 1.10, "refund": None,
     "note": "No general VAT. Apple has no official storefront here — priced via regional resellers, so treat as a rougher estimate."},
    {"code": "NZ", "name": "New Zealand", "flag": "🇳🇿", "currency": "NZD", "fx": 1.66,
     "tax_rate": 0.15, "tax_name": "GST", "premium": 1.05, "refund": None,
     "note": "No tourist GST refund scheme, unlike neighbouring Australia."},
    {"code": "MX", "name": "Mexico", "flag": "🇲🇽", "currency": "MXN", "fx": 18.5,
     "tax_rate": 0.16, "tax_name": "IVA", "premium": 1.08, "refund": 0.80,
     "note": "IVA refund available to tourists at the airport for purchases over a minimum amount."},
    {"code": "SA", "name": "Saudi Arabia", "flag": "🇸🇦", "currency": "SAR", "fx": 3.75,
     "tax_rate": 0.15, "tax_name": "VAT", "premium": 1.05, "refund": 0.85,
     "note": "Tourist VAT refund available at the airport for eligible visitors since 2021."},
]


# ---------------------------------------------------------------------------
# US state sales tax — approximate combined state + average local rate (%)
# ---------------------------------------------------------------------------

STATE_TAX = {
    "AL": 9.29, "AK": 1.76, "AZ": 8.4, "AR": 9.45, "CA": 8.82, "CO": 7.81, "CT": 6.35,
    "DE": 0, "FL": 7.02, "GA": 7.4, "HI": 4.5, "ID": 6.03, "IL": 8.86, "IN": 7.0,
    "IA": 6.94, "KS": 8.7, "KY": 6.0, "LA": 9.55, "ME": 5.5, "MD": 6.0, "MA": 6.25,
    "MI": 6.0, "MN": 7.49, "MS": 7.07, "MO": 8.29, "MT": 0, "NE": 6.94, "NV": 8.23,
    "NH": 0, "NJ": 6.6, "NM": 7.72, "NY": 8.53, "NC": 6.98, "ND": 6.96, "OH": 7.24,
    "OK": 8.98, "OR": 0, "PA": 6.34, "RI": 7.0, "SC": 7.46, "SD": 6.11, "TN": 9.55,
    "TX": 8.2, "UT": 7.19, "VT": 6.24, "VA": 5.75, "WA": 9.29, "WV": 6.5, "WI": 5.43,
    "WY": 5.44, "DC": 6.0, "PR": 11.5,
}

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "Washington DC",
    "PR": "Puerto Rico",
}

# ZIP3 prefix ranges -> state (approximate boundaries), for a ZIP -> state lookup.
ZIP_STATE_RANGES = [
    (0, 9, "PR"), (10, 27, "MA"), (28, 29, "RI"), (30, 38, "NH"), (39, 49, "ME"),
    (50, 59, "VT"), (60, 69, "CT"), (70, 89, "NJ"), (100, 149, "NY"), (150, 196, "PA"),
    (197, 199, "DE"), (200, 205, "DC"), (206, 219, "MD"), (220, 246, "VA"), (247, 268, "WV"),
    (270, 289, "NC"), (290, 299, "SC"), (300, 319, "GA"), (398, 399, "GA"), (320, 349, "FL"),
    (350, 369, "AL"), (370, 385, "TN"), (386, 397, "MS"), (400, 427, "KY"), (430, 459, "OH"),
    (460, 479, "IN"), (480, 499, "MI"), (500, 528, "IA"), (530, 549, "WI"), (550, 567, "MN"),
    (570, 577, "SD"), (580, 588, "ND"), (590, 599, "MT"), (600, 629, "IL"), (630, 658, "MO"),
    (660, 679, "KS"), (680, 693, "NE"), (700, 714, "LA"), (716, 729, "AR"), (730, 749, "OK"),
    (750, 799, "TX"), (885, 885, "TX"), (800, 816, "CO"), (820, 831, "WY"), (832, 838, "ID"),
    (840, 847, "UT"), (850, 865, "AZ"), (870, 884, "NM"), (889, 898, "NV"), (900, 961, "CA"),
    (967, 968, "HI"), (970, 979, "OR"), (980, 994, "WA"), (995, 999, "AK"),
]

CA_PROVINCE_TAX = {"AB": 5, "BC": 12, "MB": 12, "NB": 15, "NL": 15, "NS": 15, "ON": 13,
                    "PE": 15, "QC": 14.975, "SK": 11, "NT": 5, "NU": 5, "YT": 5}
CA_PROVINCE_NAMES = {"AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
                      "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
                      "NS": "Nova Scotia", "ON": "Ontario", "PE": "Prince Edward Island",
                      "QC": "Quebec", "SK": "Saskatchewan", "NT": "Northwest Territories",
                      "NU": "Nunavut", "YT": "Yukon"}


# ---------------------------------------------------------------------------
# Trade-in (old device) seed values
# ---------------------------------------------------------------------------

# US$ credit in "excellent" condition, before country/condition scaling.
TRADE_IN_DEVICES = [
    {"id": "ipx", "name": "iPhone X", "usd": 30},
    {"id": "ipxr", "name": "iPhone XR", "usd": 40},
    {"id": "ip11", "name": "iPhone 11", "usd": 60},
    {"id": "ip11p", "name": "iPhone 11 Pro", "usd": 80},
    {"id": "ip11pm", "name": "iPhone 11 Pro Max", "usd": 90},
    {"id": "ip12m", "name": "iPhone 12 mini", "usd": 70},
    {"id": "ip12", "name": "iPhone 12", "usd": 90},
    {"id": "ip12p", "name": "iPhone 12 Pro", "usd": 110},
    {"id": "ip12pm", "name": "iPhone 12 Pro Max", "usd": 130},
    {"id": "ip13m", "name": "iPhone 13 mini", "usd": 100},
    {"id": "ip13", "name": "iPhone 13", "usd": 130},
    {"id": "ip13p", "name": "iPhone 13 Pro", "usd": 170},
    {"id": "ip13pm", "name": "iPhone 13 Pro Max", "usd": 190},
    {"id": "ip14", "name": "iPhone 14", "usd": 170},
    {"id": "ip14pl", "name": "iPhone 14 Plus", "usd": 190},
    {"id": "ip14p", "name": "iPhone 14 Pro", "usd": 230},
    {"id": "ip14pm", "name": "iPhone 14 Pro Max", "usd": 260},
    {"id": "ip15", "name": "iPhone 15", "usd": 220},
    {"id": "ip15pl", "name": "iPhone 15 Plus", "usd": 240},
    {"id": "ip15p", "name": "iPhone 15 Pro", "usd": 310},
    {"id": "ip15pm", "name": "iPhone 15 Pro Max", "usd": 360},
    {"id": "ip16", "name": "iPhone 16", "usd": 300},
    {"id": "ip16pl", "name": "iPhone 16 Plus", "usd": 330},
    {"id": "ip16p", "name": "iPhone 16 Pro", "usd": 430},
    {"id": "ip16pm", "name": "iPhone 16 Pro Max", "usd": 480},
]

TRADE_IN_CONDITIONS = [
    {"id": "excellent", "label": "Excellent — like new", "mult": 1.0},
    {"id": "good", "label": "Good — light wear", "mult": 0.85},
    {"id": "fair", "label": "Fair — visible wear", "mult": 0.65},
    {"id": "poor", "label": "Poor — cracked/faulty", "mult": 0.35},
]

# Seed guess at where Apple's own trade-in program operates — verify locally,
# this is not authoritative. Countries added later (Taiwan, Macau, etc.)
# default to False until someone confirms otherwise.
TRADE_IN_AVAILABILITY = {
    "US": True, "DE": True, "GB": True, "CH": True, "JP": True, "AU": True, "CA": True,
    "SG": False, "AE": False, "HK": False, "IN": False,
    "TW": False, "MO": False, "KR": False, "MY": False, "TH": False,
    "QA": False, "NZ": False, "MX": False, "SA": False,
}


# ---------------------------------------------------------------------------
# Pricing logic
# ---------------------------------------------------------------------------

def get_product(product_id: str) -> dict:
    for p in PRODUCTS:
        if p["id"] == product_id:
            return p
    raise KeyError(f"Unknown product id: {product_id!r}")


def default_variant_selection(product: dict) -> dict:
    """First option of every variant dimension, e.g. {'storage': '256'}."""
    return {dim: opts[0]["id"] for dim, opts in product.get("variants", {}).items()}


def variant_delta(product: dict, selection: dict) -> int:
    total = 0
    for dim, opts in product.get("variants", {}).items():
        chosen_id = selection.get(dim, opts[0]["id"])
        opt = next((o for o in opts if o["id"] == chosen_id), opts[0])
        total += opt["delta"]
    return total


def effective_usd(product: dict, selection: Optional[dict] = None) -> int:
    """Base US list price for a product configured with `selection`."""
    if selection is None:
        selection = default_variant_selection(product)
    return product["usd"] + variant_delta(product, selection)


def tax_portion(price: float, tax_rate: float) -> float:
    """The tax component embedded in a tax-inclusive `price`."""
    return price - (price / (1 + tax_rate))


def compute_us_state_price(base_usd: float, state_code: str, employee_discount_pct: float = 0) -> dict:
    """
    Price breakdown for one US state: Apple's US list price is uniform
    nationally, so the only thing that varies by state is sales tax
    (looked up post-discount, matching how it's charged at checkout).
    """
    tax_rate = STATE_TAX.get(state_code, 0) / 100
    discounted = base_usd * (1 - employee_discount_pct / 100)
    tax_amount = discounted * tax_rate
    total = discounted + tax_amount
    return {
        "state": state_code,
        "state_name": STATE_NAMES.get(state_code, state_code),
        "base_price": round(discounted, 2),
        "tax_rate_pct": round(tax_rate * 100, 3),
        "tax_amount": round(tax_amount, 2),
        "total_price": round(total, 2),
    }


def compute_country_row(country: dict, list_price_local: float, tourist_mode: bool = False,
                         employee_mode: bool = False, employee_pct: float = 15) -> dict:
    """Mirrors computeRow() in tariff-board.html for one country."""
    ex_tax = list_price_local / (1 + country["tax_rate"])
    discount = employee_pct / 100 if employee_mode else 0
    paying_price = list_price_local * (1 - discount)
    refund_applies = tourist_mode and country["refund"] is not None
    refund_cash = tax_portion(paying_price, country["tax_rate"]) * country["refund"] if refund_applies else 0
    final_price = paying_price - refund_cash
    usd_equivalent = final_price / country["fx"]
    return {
        "list_price": list_price_local, "ex_tax": ex_tax, "paying_price": paying_price,
        "refund_cash": refund_cash, "final_price": final_price, "usd_equivalent": usd_equivalent,
        "refund_applies": refund_applies,
    }


def zip_to_state(zip_code: str) -> Optional[str]:
    zip_code = (zip_code or "").strip()
    if not zip_code.isdigit() or len(zip_code) != 5:
        return None
    zip3 = int(zip_code[:3])
    for lo, hi, state in ZIP_STATE_RANGES:
        if lo <= zip3 <= hi:
            return state
    return None


# ---------------------------------------------------------------------------
# Reporting / export
# ---------------------------------------------------------------------------

def us_state_report(product_id: str, selection: Optional[dict] = None, employee_discount_pct: float = 0) -> list:
    product = get_product(product_id)
    base = effective_usd(product, selection)
    rows = [compute_us_state_price(base, code, employee_discount_pct) for code in STATE_TAX]
    rows.sort(key=lambda r: r["total_price"])
    return rows


def print_report(product_id: str, selection: Optional[dict] = None, employee_discount_pct: float = 0) -> None:
    product = get_product(product_id)
    base = effective_usd(product, selection)
    rows = us_state_report(product_id, selection, employee_discount_pct)
    print(f"\n{product['name']} — base list price ${base:,.2f} (before state tax)\n")
    print(f"{'State':<24}{'Tax %':>8}{'Tax $':>12}{'Total':>12}")
    print("-" * 56)
    for r in rows[:5]:
        print(f"{r['state_name']:<24}{r['tax_rate_pct']:>7.2f}%{r['tax_amount']:>12,.2f}{r['total_price']:>12,.2f}")
    print("   ...")
    for r in rows[-5:]:
        print(f"{r['state_name']:<24}{r['tax_rate_pct']:>7.2f}%{r['tax_amount']:>12,.2f}{r['total_price']:>12,.2f}")
    cheapest, priciest = rows[0], rows[-1]
    spread = priciest["total_price"] - cheapest["total_price"]
    print(f"\nCheapest:  {cheapest['state_name']} (${cheapest['total_price']:,.2f})")
    print(f"Priciest:  {priciest['state_name']} (${priciest['total_price']:,.2f})")
    print(f"Spread:    ${spread:,.2f} ({spread / cheapest['total_price'] * 100:.1f}% more at the priciest state)\n")


def export_frontend_data(out_path: str) -> None:
    """Write the JSON the static frontend (frontend/app.js) loads."""
    data = {
        "products": PRODUCTS,
        "us_states": [
            {"code": code, "name": STATE_NAMES[code], "tax_rate_pct": rate}
            for code, rate in STATE_TAX.items()
        ],
        "countries": COUNTRIES,
        "trade_in_devices": TRADE_IN_DEVICES,
        "trade_in_conditions": TRADE_IN_CONDITIONS,
        "trade_in_availability": TRADE_IN_AVAILABILITY,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {out_path} ({os.path.getsize(out_path):,} bytes)")


def list_products() -> None:
    for p in PRODUCTS:
        print(f"  {p['id']:<10} {p['cat']:<7} {p['name']} — ${p['usd']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Apple price data — US state report + frontend data export.")
    parser.add_argument("--product", default="ip17pro", help="Product id (see --list-products)")
    parser.add_argument("--employee-discount", type=float, default=0, help="Employee discount %% to apply")
    parser.add_argument("--list-products", action="store_true", help="List product ids and exit")
    parser.add_argument("--export-only", action="store_true", help="Only (re)write frontend/data.json, skip the report")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "data.json"),
                         help="Path to write the frontend data JSON")
    args = parser.parse_args()

    if args.list_products:
        list_products()
        return

    if not args.export_only:
        print_report(args.product, employee_discount_pct=args.employee_discount)

    export_frontend_data(args.out)


if __name__ == "__main__":
    main()

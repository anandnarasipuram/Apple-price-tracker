# Apple Price Tracker

Three pieces:

1. **`scrape_apple_catalog.py`** — reads apple.com's US store and writes
   `apple_catalog.json`: every model with its real storage / colour / size / chip options
   and prices (iPhone, iPad, Mac, Watch, Vision, AirPods, HomePod, Apple TV, AirTag...)
   plus ~300 accessories. Re-run it whenever Apple ships something new.
2. **`apple_price_data.py`** — the pricing data & logic (Python port of the original
   `tariff-board.html` tool): loads that catalog, plus international country tax/FX
   assumptions and US state sales-tax rates and the pricing formulas. Run it locally
   for a quick report, and to (re)generate `frontend/data.json`.
3. **`frontend/`** — a static site (no build step, no server), organized as tabs
   that all share the same product/variant/currency/employee-discount filters at the top:
   - **World Prices** — a world choropleth (hover for a price/tax breakdown, click the
     US to jump to the USA Map tab) for the 20 countries this tool tracks, plus a table
     view with editable "real" local prices, a tourist VAT/GST refund toggle, and a
     side-by-side bar chart.
   - **USA Map** — choropleth of all 50 states + DC, colour-coded from cheapest to
     most expensive total price (list price + that state's sales tax). Hover (or
     tab + Enter) a state for the price/tax/total breakdown; a sortable table and a
     ZIP lookup ("find my state") are included too.
   - **Compare Countries** — three independent what-if columns (different country,
     tax override, employee discount, tourist refund per column) side by side.
   - **Trade-In** — pick an old iPhone + condition + country, get an estimated
     trade-in credit (editable), and see the net price after trading it in against
     whatever product/country you've currently got selected.
   - **Tax Data** — edit any country's tax rate, FX rate, and refund eligibility live;
     add or remove countries; reset all edits.
   ("Ship & Customs" — buy-here-ship-there with a flat customs-% estimate — also
   exists in the code but its tab button is hidden; see `TAB_IDS` in `app.js` to bring it back.)
   Paste any apple.com link into the box above the tabs — a marketing page
   (`apple.com/ipad-mini/`), a buy page with options in it
   (`.../buy-iphone/iphone-18-pro/6.9-inch-display-1tb-silver`) or an accessory page — and
   it's matched to the catalog with the storage / colour / size from the URL pre-selected.
   A link that isn't in the catalog (a model newer than the last scrape) gets a price
   prompt instead of an error, or skip links entirely and type a product name, category
   and US price into the "no link needed" row right below it.

## Keeping the catalog current

```bash
python3 scrape_apple_catalog.py                  # full refresh, ~1 minute (devices + ~300 accessories)
python3 scrape_apple_catalog.py --devices-only   # just the devices (~40 requests)
python3 scrape_apple_catalog.py --url https://www.apple.com/shop/buy-iphone/iphone-18-pro --add
                                                 # look up one page and merge it into the catalog
python3 apple_price_data.py --export-only        # then rebuild frontend/data.json
```

Then drag the `frontend/` folder onto Netlify again. (A website can't do this itself:
apple.com blocks cross-site reads, so the scraping happens here, on your machine.)

How it reads prices: Apple embeds exact per-SKU prices in each product page. iPhone /
iPad / AirPods / Apple TV / HomePod pages list every SKU (model + storage + colour);
Mac and Watch pages have a configurator price table; anything else falls back to the
page's starting price (flagged in the app). Options are modelled additively
(base price + option deltas); the scraper prints a warning if a product's real prices
don't fit that, so you'd know a combination might be off. Marketing "From $X" headlines
can differ slightly from SKU prices (e.g. iPhone 17 shows "From $899" but its cheapest
purchasable SKU is $929) — the SKU price is what's used.

## Using the Python tool

```bash
python3 apple_price_data.py                          # report for the default product (iPhone 18 Pro Max)
python3 apple_price_data.py --product iphone-18-pro  # any product id
python3 apple_price_data.py --list-products           # see all product ids
python3 apple_price_data.py --employee-discount 15    # apply a 15% discount
python3 apple_price_data.py --export-only             # just rewrite frontend/data.json
```

No third-party dependencies — standard library only (Python 3.8+).

Edit `COUNTRIES` or `STATE_TAX` at the top of `apple_price_data.py` to update tax rates,
then re-run it — it regenerates `frontend/data.json` so the site picks up the change on
next load. (If `apple_catalog.json` is missing it falls back to a small built-in sample
catalog.)

## Running the frontend locally

It's plain HTML/CSS/JS — any static server works:

```bash
cd frontend
python3 -m http.server 8000
# open http://localhost:8000
```

(A `file://` open won't work for `data.json` due to browser fetch restrictions —
use a local server, or just deploy it.)

## Deploying to Netlify

**Drag & drop (fastest):** go to [app.netlify.com/drop](https://app.netlify.com/drop)
and drag the `frontend/` folder in.

**Netlify CLI:**
```bash
npm install -g netlify-cli
cd frontend
netlify deploy --prod
```

**Git-based (auto-deploys on push):** push this repo to GitHub, then in Netlify:
New site from Git → pick the repo → set **Base directory** to `frontend` and
**Publish directory** to `frontend` (there's no build command; `netlify.toml`
already sets `publish = "."` relative to that base directory).

Both maps pull their outlines and D3 from a CDN at load time (see
`frontend/index.html`), so the deployed site needs normal internet access —
nothing else is fetched, and no data leaves the visitor's browser.

## Notes / assumptions

- The map's scope is domestic (USA): Apple's US list price is the same nationwide,
  so state-to-state differences are driven entirely by each state's approximate
  combined state + average local sales tax. City/county add-ons vary within a
  state — treat it as a planning estimate, not a receipt.
- Every tab reads the same product/variant/currency/employee-discount filters at the
  top, and the World/Compare/Ship tabs share one set of editable country assumptions
  (edited in the Tax Data tab, or inline in the World Prices table) — a change in one
  tab is reflected everywhere else immediately.

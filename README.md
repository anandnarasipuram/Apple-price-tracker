# Apple Price Tracker

Three pieces:

1. **`scrape_apple_catalog.py`** — reads Apple's own online stores and writes
   `apple_catalog.json`: every model with its real storage / colour / size / chip options
   (iPhone, iPad, Mac, Watch, Vision, AirPods, HomePod, Apple TV, AirTag...) plus ~300
   accessories, with the **exact price each country's Apple store charges, in that
   country's currency** (17 countries — see "Where the numbers come from"). Products are
   discovered by crawling Apple's store pages, so a newly launched product shows up the
   next time it runs — no code changes.
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

## Where the numbers come from (and what is still assumed)

**Read from Apple, exactly** — every price in the app for a product/country/option is what
that country's own Apple online store shows (`apple.com/de`, `/jp`, `/uk`, ...). Nothing is
computed from the US price, and there is no "premium" or guessed exchange rate in a price.
If a country's store doesn't sell a product/option (or has no store at all — Macau and
Qatar), the app shows **"no Apple price"** and leaves it off the map, rather than estimating.
Type your own price in the World table if you have a real one (e.g. from a shop).

**Still assumptions (editable in the Tax Data tab)** — Apple's stores show tax-inclusive
prices (except the US and Canada, where tax is added at checkout), so the app *splits* the
displayed price into "before tax" + "tax" using each country's standard rate; US state
sales-tax rates; tourist VAT/GST refund percentages; the employee-discount %; and the
trade-in credit values. **Exchange rates** are only used to *compare* countries (the
"in EUR / in INR" columns) and come from open.er-api.com, stamped with their date.

**Coverage limits** — accessories abroad are matched by part number, and Apple often
uses a different part number per country, so only some accessories have a price in each
country (UK/Germany ~140 of 297, most others fewer). Devices are ~all covered.

## Keeping the catalog current

```bash
python3 scrape_apple_catalog.py                  # full refresh (~10 min: US catalog, 17 stores, accessories abroad)
python3 scrape_apple_catalog.py --devices-only   # devices only, US + all countries (~1 min; keeps old accessories)
python3 scrape_apple_catalog.py --regions DE,JP  # only re-read some countries
python3 scrape_apple_catalog.py --no-regional    # US only
python3 scrape_apple_catalog.py --url https://www.apple.com/shop/buy-iphone/iphone-18-pro --add
                                                 # look up one page and merge it into the catalog
python3 scrape_apple_catalog.py --no-cache       # ignore the .apple_cache/ page cache
python3 apple_price_data.py --export-only        # then rebuild frontend/data.json
```

Then drag the `frontend/` folder onto Netlify again. (A website can't do this itself:
apple.com blocks cross-site reads, so the scraping happens on a computer, not in the browser.)

**Picking up future launches automatically.** The scraper doesn't hard-code a product list:
it crawls Apple's `/store` and each shopping section's landing page, finds every
`/shop/buy-…` family, and reads it. A new iPhone / iPad / Mac / accessory therefore appears
the next time you run it. To run it on a schedule without your computer, push this folder to
GitHub and enable `.github/workflows/refresh-prices.yml` (weekly; commits the refreshed
JSON; connect Netlify to the repo so it redeploys on each commit).

How it reads prices: Apple embeds exact per-SKU prices in each product page. iPhone /
iPad / AirPods / Apple TV / HomePod pages list every SKU (model + storage + colour);
Mac and Watch pages have a configurator price table; anything else falls back to the
page's starting price (flagged in the app). Options are modelled additively
(base price + option deltas); if a country's real prices don't fit that, the exact
price table is stored instead, so no combination is ever approximated. Marketing "From $X"
headlines can differ slightly from SKU prices (e.g. iPhone 17 shows "From $899" but its
cheapest purchasable SKU is $929) — the SKU price is what's used.

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

- The USA map's scope is domestic: Apple's US list price is the same nationwide,
  so state-to-state differences are driven entirely by each state's approximate
  combined state + average local sales tax. City/county add-ons vary within a
  state — treat it as a planning estimate, not a receipt.
- Every tab reads the same product/variant/currency/employee-discount filters at the
  top, and the World/Compare/Ship tabs share one set of editable country assumptions
  (edited in the Tax Data tab, or inline in the World Prices table) — a change in one
  tab is reflected everywhere else immediately.

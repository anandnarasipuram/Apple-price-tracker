# Apple Price Tracker

Two pieces:

1. **`apple_price_data.py`** — the pricing data & logic (Python port of the original
   `tariff-board.html` tool): the Apple product catalog, international country tax/FX
   assumptions, and US state sales-tax rates, plus the pricing formulas. Run it locally
   for a quick report, and to (re)generate `frontend/data.json`.
2. **`frontend/`** — a static site (no build step, no server) with an interactive
   USA choropleth map: pick a product/configuration, optionally apply the employee
   discount, and see every state colour-coded from cheapest to most expensive total
   price (list price + that state's sales tax). Hover (or tab + Enter) a state for
   the price/tax/total breakdown; a full sortable table and a ZIP lookup ("find my
   state") are included too.

## Using the Python tool

```bash
python3 apple_price_data.py                          # report for iPhone 17 Pro
python3 apple_price_data.py --product ip17pm          # iPhone 17 Pro Max
python3 apple_price_data.py --list-products           # see all product ids
python3 apple_price_data.py --employee-discount 15    # apply a 15% discount
python3 apple_price_data.py --export-only             # just rewrite frontend/data.json
```

No third-party dependencies — standard library only (Python 3.8+).

Edit the `PRODUCTS`, `COUNTRIES`, or `STATE_TAX` dictionaries at the top of the file
to update prices/rates, then re-run the script — it regenerates `frontend/data.json`
so the site picks up the change on next load.

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

The map pulls the US states outline and D3 from a CDN at load time (see
`frontend/index.html`), so the deployed site needs normal internet access —
nothing else is fetched, and no data leaves the visitor's browser.

## Notes / assumptions

- The map's scope is domestic (USA): Apple's US list price is the same nationwide,
  so state-to-state differences are driven entirely by each state's approximate
  combined state + average local sales tax. City/county add-ons vary within a
  state — treat it as a planning estimate, not a receipt.
- `apple_price_data.py` also carries the original tool's international country data
  (FX, VAT/GST, tourist refunds) for reuse if you want to extend the frontend with
  a country-comparison view later — the shipped site focuses on the USA map that
  was asked for.

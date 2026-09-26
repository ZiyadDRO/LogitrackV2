# Square + Multi-Location — Architecture

Written 15 Sep 2026. Companion to ROADMAP.md, which assumed one Shopify store at one location.

## Status — updated 21 Sep 2026

**Part 1 (the Square connector) is built and merged.** Steps 1 and 2 of "Order of work"
below are done; step 3 — reconciling against your cousin's real Square reports — is the
next thing, and it is yours rather than the code's.

Shipped:
- `engine_statsforecast/square_source.py` — the connector
- `engine_statsforecast/sources.py` — the registry that made Shopify and Square peers
- `engine_statsforecast/connections.py` — saved accounts, so a token is pasted once
- `/api/sources`, `/api/connections` (+ `/test`), `/api/upload/source`; the old
  `/api/upload/shopify` still works unchanged
- `UploadPanel.jsx` — "Upload file" / "Connect store", with saved accounts
- 150 assertions across `test_square_source.py`, `test_connections.py`,
  `test_source_endpoints.py`, all offline against fixtures

**Part 2 (multi-location) is not started** and nothing below it has changed. The connector
pools locations into one series per SKU today — deliberately, so it matches the
single-location catalog — and carries `location_id` through on every pre-aggregation row
so the split stays a grouping change rather than a re-fetch.

---

## The headline

**These are two independent problems and you should not solve them together.**

The Square connector is mechanical — a second module that emits the same DataFrame `shopify_source.py` already emits. A weekend of work, most of it reconciliation rather than code.

Multi-location is an architectural change to the identity of a product in your catalog. It touches keying, persistence, pooling, the backtest cache and the frontend. It is the one worth thinking hard about, and it is worth doing **even if your cousin's store were on Shopify**.

Do them in that order — connector first, single-location, against one of his two stores. Get real data flowing and reconciled before you change what a SKU *means*.

**Recommendations, up front:**

| Question | Answer |
|---|---|
| How do Square credentials fit the current UI? | A third tab next to `file` / `shopify`, backed by a source registry rather than a second hardcoded branch |
| How do you model 2 locations? | **One series per (SKU, location)**, with a SKU-level rollup as a *view*, not as the stored model |
| Do you aggregate the two stores into one number? | **No.** It breaks the exact thing your product claims to be honest about |
| Is single-location still the default? | Yes — location collapses to `None` and today's code path stays byte-identical |

---

# Part 1 — The Square connector

## 1.1 You already designed for this

`shopify_source.py` says it plainly:

> This is a DECOUPLED data source: it only fetches + reshapes. The backtest (and the upload pipeline) don't know or care that the data came from Shopify.

And `_ingest()` in `main.py` says:

> Shared ingestion for ANY raw sales DataFrame — an uploaded file OR a live source like Shopify.

That's the right seam, and it holds. A source has to produce exactly two things:

```python
fetch_sales(...)        -> DataFrame[date, sku, sku_name, units_sold, price, on_promotion]
fetch_catalog_meta(...) -> {sku: {"category": str|None, "stock": int|None, "cost": float|None}}
```

Write `square_source.py` to that contract and `/api/upload/square` is ten lines that mirror `upload_shopify`.

## 1.2 Where it is *not* decoupled — fix this first

Shopify is hardcoded in four more places, and each one is a place a second source silently does nothing:

| Location | Hardcoding |
|---|---|
| `live_actuals.shopify_actuals_provider()` | The grading loop that closes your accuracy feedback loop |
| `stock_log.sample_from_shopify()` | The hourly inventory poll that feeds censoring |
| `main.py` `/api/livelog/tick`, `/api/livelog/score` | Call both of the above **by name** |
| `main.py` `cost_src[sid] = "shopify"` → `App.jsx` `costSource === "shopify"` | Cost provenance display |

If you add Square as a second `if` branch in each, you will have a third one when the next source arrives, and `livelog/tick` will quietly poll the wrong store.

**Do this instead.** A source is an object with four methods; register them in a dict:

```python
# sources/__init__.py
class SalesSource(Protocol):
    key: str                                    # "shopify" | "square" | "csv"
    label: str                                  # "Shopify" | "Square"
    def fetch_sales(self, creds, days=None) -> pd.DataFrame: ...
    def fetch_catalog_meta(self, creds) -> dict: ...
    def sample_stock(self, creds) -> dict:      # {sku_id: on_hand} for StockLog
    def actuals_provider(self, entries, creds): # for ForecastLog.score_due
        ...

SOURCES = {"shopify": ShopifySource(), "square": SquareSource()}
```

Then `/api/livelog/tick` takes `{source, creds}` and looks it up. `cost_src[sid] = source.key`, and the frontend renders `costSource` as a label instead of comparing it to a literal. The Shopify path stays exactly what it is today — this is a move, not a rewrite.

This refactor is ~2 hours and it is the difference between "LogiTrack supports Square" and "LogiTrack supports commerce platforms."

## 1.3 What Square actually gives you, and where it differs

Square Connect v2. Base `https://connect.squareup.com/v2`, header `Square-Version: <YYYY-MM-DD>`, `Authorization: Bearer <token>`. Sandbox is `connect.squareupsandbox.com`.

Scopes needed (read-only): `ORDERS_READ`, `ITEMS_READ`, `INVENTORY_READ`, `MERCHANT_PROFILE_READ`.

The five differences that will actually cost you time:

### (a) **SKU is not on the line item.** This is the big one.

Shopify hands you `lineItems.nodes[].sku` directly. Square hands you `line_items[].catalog_object_id` — the ID of an `ITEM_VARIATION` — plus `name` and `variation_name`. The SKU lives on the catalog object.

So the Square connector must **fetch the catalog first** and build `{catalog_object_id: (sku, item_name, category_id)}`, then join. Two failure modes to handle explicitly, both common in real Square accounts:

1. **Line items with no `catalog_object_id` at all** — an amount typed into the register ("Misc $8.00"). These are real revenue but not a trackable product. Drop them, and *count* them.
2. **Variations with an empty `sku` field.** Square does not require a SKU; plenty of merchants never fill it in. Fall back to `sku or catalog_object_id`, which is stable and unique, but means the displayed identifier is an opaque Square ID.

Both belong in the `dataQuality` dict `_ingest` already returns, so the user sees them:

```python
data_quality["lineItemsWithoutCatalogObject"] = n_adhoc
data_quality["skusFallingBackToSquareId"]     = n_no_sku
```

Ask your cousin to fill in SKUs in his Square catalog before you pull. It's the single highest-leverage thing he can do, and it costs him an afternoon.

### (b) **Money is integer cents.**

`base_price_money = {"amount": 1250, "currency": "USD"}` is $12.50. Shopify gives decimal strings. Divide by 100. Getting this wrong inflates every price by 100× and your margin-based protection tiers will produce confident nonsense rather than an obvious error.

### (c) **Dates must be localized per location.** ← the subtle one

Orders come back with RFC3339 UTC timestamps. A 9pm Saturday sale in Georgia is **Sunday** in UTC. Your whole pipeline aggregates to a calendar day and the router reads day-of-week seasonality off it.

Shopify let you ignore this because a store has one timezone. Square won't: the `Location` object carries an IANA `timezone` field (`America/New_York`), and with two locations they may differ. Convert per-location before truncating:

```python
day = ts.astimezone(ZoneInfo(loc_tz[order["location_id"]])).date().isoformat()
```

For a retail store with evening trade this moves a meaningful share of sales by one day, and it smears the weekly seasonal signal your Prophet route depends on.

Use `closed_at` for completed sales, falling back to `created_at`. Filter `state_filter` to `COMPLETED`; `CANCELED` never happened, same rule as Shopify's `cancelledAt`.

### (d) **Cost probably isn't available.**

Shopify gave you `inventoryItem.unitCost` for free. On Square, per-unit cost lives in `vendor_information` on the item variation, and that's **gated behind Square for Retail Premium / Restaurants Premium**. Unless your cousin is on a premium plan, `cost` comes back `None` for everything.

That matters because unit cost drives your economic protection recommendation and your unit-economics panel. Plan for it: `fetch_catalog_meta` returns `cost: None`, the existing `cost_override` path no-ops, and the user enters costs by hand or via the CSV cost column. Worth surfacing once, clearly, at import time rather than letting every product show an empty cost field with no explanation.

### (e) **Square's pagination is much friendlier.**

`SearchOrders` takes `limit` up to 1000 (default 500) with a plain `cursor`. No query-cost budget, no 25-line-item truncation, no 100-variant cap. The three "expect these, they are not bugs" caveats in ROADMAP.md about truncation **do not apply to Square**.

`location_ids` is capped at **10 per request** — irrelevant for two stores, but chunk it anyway so the code is correct for the product you're selling.

Rate limiting: 429 + `RATE_LIMITED`. Square documents exponential backoff with jitter and does **not** document a `Retry-After` header, so don't read one — back off on your own schedule. Your `_graphql` retry loop is the right shape; just don't depend on the header.

## 1.4 The thing Square gives you that Shopify never could

`stock_log.py` opens with an honest limitation:

> It only works forward. Days before you connected the store have no readings.

**That is a Shopify limitation, not a law of nature.** Square has `POST /v2/inventory/changes/batch-retrieve`, which returns `PHYSICAL_COUNT` and `ADJUSTMENT` records with `occurred_at`, `quantity`, `location_id` and `from_state`/`to_state` — filterable by `updated_after`/`updated_before`, up to 500 `catalog_object_ids` and 1000 records per page.

Meaning: **you can reconstruct historical on-hand by replaying changes backwards from the current count.** Stockout history, on day one, without waiting four months for hourly polls to accumulate.

This is a big deal for you specifically. Your backtest needs 164 days per product, and censoring is one of your three flagged technical risks — stockout-censored demand looks like low demand, and correcting it is a core part of your honesty positioning. On Shopify that correction is unavailable for all historical data. On Square it isn't.

Concretely: add `backfill_stock_history(days)` to the Square source, have it write into `StockLog` with true `occurred_at` timestamps, and `censoring.hours_in_stock_from_samples` works unchanged on backfilled samples. Keep the hourly tick running anyway — it's cheap, and it keeps resolution high going forward.

Don't build this in week one. But know it's there, because it changes what the first backtest against his data is worth.

## 1.5 Credentials

For your cousin's store, the fast path is a **personal access token** from his own Square developer dashboard, scoped read-only, pasted into your UI exactly like the Shopify token. No OAuth flow, no app review, no approval queue — a welcome contrast to the `read_all_orders` queue you're stuck in on Shopify.

Note for later: the moment you have a *second* customer, you need Square OAuth (tokens expire and refresh, and you can't ask a stranger for a PAT). Design the credential blob as `{source, creds: {...}}` now so OAuth is a change inside `SquareSource` rather than a schema migration.

---

# Part 2 — Two locations

## 2.1 Why "just add them up" is the wrong answer for *your* product

The tempting move is to sum both stores into one series and change nothing. It is one line in the connector. Here's why it's wrong:

Store A sells 12/week and is well stocked. Store B sells 0 this week **because it was out of stock**. The aggregate reads 12, looks healthy, and your forecast is fine. You have just done the exact thing `censoring.py` exists to prevent — you've turned a stockout into observed low demand — and you've done it *before* the censoring module can see it, so it can't help.

You market this tool on abstention behavior, calibrated uncertainty and a verifiable backtest. Aggregating locations is quietly incompatible with that thesis. It's not a performance tradeoff; it's a correctness one.

It also can't answer the only question a two-location operator actually has: **not "how much do I buy" but "how much goes where."**

## 2.2 The three models

| | Model | Right when | Cost |
|---|---|---|---|
| **A** | Aggregate: sum locations, one series | Inventory is genuinely pooled — one stockroom, free transfer, one PO | Hides per-store stockouts; can't answer "which store"; contradicts the censoring story |
| **B** | **Per-location: one series per (SKU, location)** | Each store replenishes and stocks out independently — the normal SMB retail case | 2× series, each with half the history; noisier UI; needs a rollup for buying |
| **C** | Hybrid: forecast per location, plan at both levels | You want transfer recommendations and consolidated purchasing | B's cost plus a rollup layer |

**Recommendation: B as the stored data model, C's rollup as a view.** B is the smaller change and it is forward-compatible with C — you can add the rollup later without re-keying anything. Going A→B later is a migration; going B→C later is a feature.

## 2.3 The keying change

Today `_catalog` and `_sku_cache` are keyed by a flat `sku_id` string, and so is everything downstream: `_backtest_tier_cache` (`"sku|lead|coverage"`), `ForecastLog.entry_id(sku, origin, horizon)`, `StockLog._samples[sku]`, and the frontend's per-SKU stock/cost/supplier/PO records in localStorage.

**Do not mangle strings ad hoc.** `f"{sku}@{loc}"` scattered through the codebase will leak into the UI, into exports, and into the Groq categorizer prompt. One helper, one explicit field:

```python
def sku_key(sku: str, location_id: str | None) -> str:
    """Catalog key. Single-location stores key by bare SKU — unchanged from today."""
    return sku if not location_id else f"{sku}@{location_id}"
```

And carry the parts explicitly on the catalog entry, so nothing downstream has to parse the key back apart:

```python
_catalog[sid] = {
    "df": dfc, "attrs": attrs, "sku_name": sname,
    "base_sku": sku,              # NEW — the product, shared across locations
    "location_id": loc_id,        # NEW — None for single-location
    "location_name": loc_name,    # NEW — for display
    ...
}
```

**The critical property: `location_id = None` produces exactly today's key.** Single-location stores — most of your market — run the identical code path they run now, and existing persisted catalogs restore unchanged. Multi-location is a dimension that collapses to 1, not a mode.

### Give the CSV path the same dimension

Add an optional `Location` column to the upload template and `_ingest`. If you don't, multi-location becomes a Square-only feature, and it will rot the first time you touch the upload path. Two sources, one model.

## 2.4 What breaks, and what to do about it

| Component | Effect | Action |
|---|---|---|
| `catalog_store` (`VERSION = 1`) | Manifest gains `base_sku` / `location_id` | Bump to `VERSION = 2`; on load, a v1 manifest restores with `location_id = None`. Backwards-compatible, no data loss |
| `_backtest_tier_cache` fingerprint | Keys change → measured tiers invalidated | Already handled — `_drop_backtest_state` falls back to estimates and the UI labels them provisional. Re-run the backtest |
| `ForecastLog` | Keys on `sku`, which is now the composite | Works unchanged. Entries logged pre-change stay keyed by bare SKU and grade out naturally |
| `StockLog` | Keyed by sku; Square counts are already per-location | Works unchanged once you pass composite keys. **This is where per-location finally pays off** — a stockout at one store stops being averaged away |
| `router.detect_group_columns` | ⚠️ **Actively harmful if you're careless** | See below |
| Frontend localStorage | Stock, cost, supplier, PO records keyed by sku_id | Composite keys are new records. Existing single-location data untouched. Bump `storage.js` `VERSION` if you want a clean read |

### The router trap — read this one carefully

`detect_group_columns` picks a grouping attribute by cardinality, preferring `category` but falling back to "any non-reserved column that looks categorical (few distinct values relative to catalog size)."

**A `location` attribute with 2 distinct values across a few hundred SKUs is a perfect-scoring fallback candidate.** If you put location into `attrs`, the router may cheerfully decide your product families are "Store A" and "Store B" and pool a chair with a laptop because they're in the same building.

Add `location` and `location_id` to `RESERVED_COLS`. This is a one-line fix and a very annoying bug to find later.

## 2.5 The upside nobody mentions: locations are the ideal pooling cohort

The obvious objection to per-location series is data thinning — 2× the series, each with half the observations, more products falling into the intermittent/thin bucket.

But you already built the fix. Your router has a **pooled global model for thin SKUs** and a relatedness grouping layer. And *the same SKU at two stores of the same business* is the single best pooling cohort that exists: identical product, identical promotions, identical seasonality, same weather, same customer base. Far tighter than "same category."

So: make same-`base_sku`-different-`location_id` a **forced relative** — bypass the cohesion test, they're related by construction. Per-location series then borrow strength from each other by default, and the pooled model gets *more* useful under multi-location, not less. The thinning objection mostly dissolves.

Worth a note in the forecast details drawer when it happens: *"Pooled with the same product at Store B."* That's exactly the kind of legible reasoning your positioning is built on.

## 2.6 What the rollup is for

Once per-location forecasts exist, the SKU-level view is a sum — but **be careful with the prediction bands.**

If you add the 80% bands linearly (`lo_A + lo_B`, `hi_A + hi_B`) you get a band that's too wide, because both stores won't have a bad week simultaneously. If you add them in quadrature (`√(σ_A² + σ_B²)`) you're assuming independence, and two stores under the same owner running the same promotion in the same season are **positively correlated** — so that's too narrow.

Neither assumption is safe, and you already have the right instinct for this: **measure it.** Your conformal calibration machinery can calibrate the rollup band directly against held-out rollup residuals, the same way it calibrates per-SKU. Don't derive it, backtest it. Until you do, use the linear sum and label it conservative — an honestly-wide band fits the product better than a confidently-wrong narrow one.

The rollup then drives two things per-location forecasting alone can't:

- **Consolidated purchasing.** One PO to the supplier, split on arrival by each store's forecast share.
- **Transfer recommendations.** Store A has 40 units and 90 days of cover; Store B has 2 and 3 days. The answer is *move 15 units*, not *buy more*. This is the feature that justifies the whole multi-location model, and it's the one that would make a two-store operator pay — no spreadsheet does it and Stocky never did.

---

# Order of work

1. **Source registry refactor.** Move Shopify behind the interface; no behavior change. Ship it, confirm Shopify still works.
2. **`square_source.py`** — locations → catalog → orders → join → per-location-timezone day → zero-fill per (sku, location). Single location only: pass one `location_id`, key by bare SKU. **Nothing downstream changes yet.**
3. **Reconcile against Square's own reports.** See below. Do not skip this and do not build on top of it until it passes.
4. **Add the location dimension.** `sku_key`, `base_sku`/`location_id` on entries, `RESERVED_COLS`, `catalog_store` v2, `Location` column on CSV upload.
5. **Forced same-SKU pooling across locations** in the router.
6. **SKU rollup view** + consolidated purchasing.
7. **Transfer recommendations.** The payoff.
8. *Later:* historical stock backfill from inventory changes; Square OAuth for customer #2.

Steps 1–3 are a weekend. Step 4 is the real work. Steps 6–7 are product, not plumbing.

---

# Before you trust a single number

Same discipline as ROADMAP.md Step 1, adapted — and now with two stores to check separately:

1. **Export everything first.** A sync replaces the whole catalogue.
2. Pull **30 days, one location** to start.
3. **Product count** matches Square's item library.
4. **Total units for one known week, per location**, against Square's own sales report — filtered to that location. If you only check the total, a location mix-up passes silently.
5. **One product's daily numbers**, on a day with evening trade. This is where a timezone bug shows up and nowhere else.
6. **The unmapped counts**: how many line items had no `catalog_object_id`, how many SKUs fell back to a Square ID. If either is large, fix the catalog before fixing the code.
7. Check whether he has **Square Online**. Web orders carry a `location_id` too — usually a default or dedicated one — and you need to decide whether online is a third "location" or folds into one of the stores. Ask him; it's a business question, not a technical one.
8. Only then pull full history for both.

---

# Open questions — these are yours to answer, not the code's

1. **Does he move stock between the two stores?** If yes, transfers matter more than forecasts and per-location is clearly right. If the stores never exchange inventory, per-location is *still* right but the rollup matters less.
2. **Does he buy per store or once for both?** This decides whether the rollup is the primary view or a secondary one.
3. **Is he on Square Retail Premium?** Determines whether you get unit costs or hand-enter them — and unit costs gate your economic protection tiers.
4. **Are his SKUs actually filled in?** Check before writing code. Empty SKUs across a catalog change the identity strategy.
5. **How long has he been on Square?** You need 164 days per product for a measured backtest. This is the constraint that's been blocking you on Shopify — find out on day one whether it's still binding.

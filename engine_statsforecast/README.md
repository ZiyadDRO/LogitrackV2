# Router engine — Prophet + Global-pooled + Croston + fallback

A per-SKU **router** picks the right forecasting method for each product instead
of forcing one model on everything. Client-agnostic: it keys off how much signal
each SKU has and how many related SKUs exist, never off what the products are.
Same API surface and JSON schema as your other backends, so the frontend runs
against it unchanged.

## Run
```bash
pip install -r requirements.txt
uvicorn main:app --reload --app-dir engine_statsforecast --port 8000
```

## The routing decision (router.py → `route()`)

| Situation                                                        | Engine        |
|------------------------------------------------------------------|---------------|
| Established + regular demand                                     | **Prophet**   |
| Intermittent/lumpy **with own track record** (even w/ relatives) | **Croston/TSB** |
| New / thin / sparse **with** enough related SKUs to pool         | **Global** (pooled) |
| Thin intermittent orphan (no relatives)                          | **Croston/TSB** |
| Too little of anything                                           | **Abstain** (moving-average placeholder, flagged low-confidence) |

The global path is **lazy/conditional** — it only fires when a SKU is under-served
*and* there's enough related history to build assumptions from. Thresholds live at
the top of `router.py` (`ESTABLISHED_DAYS`, `MIN_RELATIVES`, …) and are tunable.

## Relatedness (universal)
`router.group_catalog` groups SKUs by whatever categorical signal the upload
carries — a `Category` column, or `brand`/`size`/`type`/… columns (auto-detected,
union across the catalog). SKUs with no shared attribute are orphans. Where only
free-text product names exist, a Groq attribute-extraction step is the intended
fallback (hook point; the router consumes whatever attribute map it's given). No
attribute names are hard-coded, so it works for any catalog.

## Engines (engines.py) — one uniform contract
- **ProphetEngine** — established regular SKUs; models the SKU's own weekly+yearly
  seasonality and price/promo regressors.
- **GlobalPooledEngine** — borrows the seasonal *shape* from related SKUs and
  anchors the *level* from the SKU's own recent sales (or peer median). This is a
  **swappable baseline**; a LightGBM global model drops in behind the same class
  later with no other changes. Handles new SKUs natively (predicts from peers when
  the SKU has near-zero history).
- **IntermittentEngine** — Croston/TSB for sparse orphans.
- **MovingAverageEngine** — the abstain fallback; flat recent average, flagged.

All four apply the **same conformal 80% band** (10th/90th percentile of backtest
residuals) so an "80% range" means the same thing regardless of engine, and all
expose `.predict_baseline()` for the scorecard's no-promo view.

## Files
- `router.py` — relatedness grouping + route decision (+ plain-language reasons).
- `engines.py` — the four engine adapters behind one contract.
- `forecast_engine.py` — shared helpers (demand classification, price analysis).
- `main.py` — catalog → relatedness → route → fit → cache; FastAPI endpoints.
- `test_router.py` — routing + contract verification (stubbed libs).

## What the UI shows
`/api/forecast` now returns `route`, `routeReason`, and an enriched
`forecastDetails` (model label, demand class, why-this-route, seasonality, price
handling, demand consistency, the conformal band, and relatives count). The
"Forecast details" side panel renders all of it, including a route badge.

## Verification status & honest limits
`python test_router.py` runs 32 checks: the route() decision table, end-to-end
routing of one SKU per archetype (regular→Prophet, sparse-orphan→Croston,
new+relatives→Global, thin-orphan→Abstain, demo→Prophet), the cache-entry
contract, forecast band/horizon, and that the global engine produces a
weekday-varying (non-flat) forecast.

> These were verified with **stubs** mirroring the API of fastapi, prophet and
> statsforecast (none installable in the build sandbox). All routing/orchestration
> logic is real; only the libraries' numerics are faked. Run `test_router.py`
> locally after `pip install -r requirements.txt` to exercise the true models.

## Known follow-ups
- The global engine is a **pooled-profile baseline**, not yet a trained ML model.
  It's deliberately swappable — replace `GlobalPooledEngine` with a LightGBM global
  model once a real catalog exists to train/validate on.
- Groq attribute extraction is a documented hook, not wired to a live call here.
- Backtest both this router and your Prophet-only build on real held-out data
  before trusting either in production.

# LogiTrack V2 — Full Audit
*July 3, 2026. Covers `engine_statsforecast/` (backend) and `src/` (frontend). Findings verified against the code; the two critical items were reproduced.*

---

## 1. Critical — fix before anything else

### 1.1 Crash: every intermittent/sporadic SKU 500s (`main.py:1166`)
`get_eligibility_status()` references undefined variables `ts` and `tw`:

```python
if demand_class in ("intermittent", "lumpy", "no_demand"):
    if ts or tw:   # NameError — ts/tw don't exist anywhere
```

**Verified live**: `NameError: name 'ts' is not defined`. Any SKU classified intermittent/lumpy/no_demand makes `/api/forecast` raise → 500. Because the frontend swallows fetch errors (`.catch(() => null)`), these SKUs **silently disappear from the dashboard**, and `fleet_summary`'s `except: continue` silently drops them from fleet counts too. Your own `TEST_PLAN.md` File 1 (SUPP-VITD → Croston) will hit this. It didn't show up with the vanity dataset because nothing routed to Croston (`model_switches.json` has only prophet/global/abstain).

Likely intended: `if reliable_days and reliable_repeats or weak_days and weak_repeats:` (or similar). Fix + add a regression test that requests a forecast for an intermittent SKU.

### 1.2 Leaked Groq API key (`App.jsx:10`)
```js
const GROQ_API_KEY ="gsk_eSr4...KJK4";
```
Hardcoded in source **and baked into `dist/assets/index-*.js`**. Anyone who gets the bundle can burn your quota. Actions: revoke the key now; move all Groq calls behind a FastAPI endpoint (e.g. `/api/ai/complete`) that reads the key from an env var; never let the browser hold it. This also fixes the awkward "apiKey" prop-drilling through five components.

---

## 2. Data loss — the biggest product gap

### 2.1 Backend forgets everything on restart
`_catalog`/`_sku_cache` are in-memory; `warmup()` clears them and reloads the demo. Every backend restart = user must re-upload their sales file. Only `model_switches.json` persists. Persist the ingested catalog (SQLite or parquet per SKU + a small JSON manifest) and reload it on startup. `_backtest_tier_cache` (feeds live protection recommendations) is also memory-only and dies on restart.

### 2.2 All business data lives only in browser localStorage
Stock levels, unit costs, fees, lead times, protection overrides, open POs, suppliers + order history, folders, categorize notes/classes — all `localStorage`. Consequences: clearing browser data (or switching browser/machine) wipes everything; the backend never knows about suppliers/POs except per request; no backup, no export/import of settings. Minimum fix: a "Backup / Restore settings" JSON export-import. Better: move params/suppliers/POs server-side next to the persisted catalog.

---

## 3. Modeling correctness risks

### 3.1 Date re-anchoring can corrupt seasonality — and the user is never warned
`_ingest()` shifts the whole sheet so the last row = yesterday. Fine for a re-upload a few days later; **destructive for a stale export**: a sheet ending in March uploaded in July moves December sales into April, so Prophet learns yearly seasonality in the wrong months and pooled monthly indices are wrong too. The backend returns `dateShiftDays`, but the frontend never reads it (grep: zero usages). At minimum surface a prominent warning when `|dateShiftDays| > ~21`; better, don't shift at all beyond a threshold (or shift by whole years to preserve month-of-year).

### 3.2 Uploaded sheets with missing zero-sale days over-forecast
`shopify_source.py` correctly reindexes each SKU to a continuous daily calendar ("CRITICAL: fill in zero-sale days") — but `_ingest()` does **not** do this for file uploads. A sheet exported as "one row per day with a sale" (very common) makes the classifier see ADI≈1 (never intermittent) and every engine average over sale-days only — the exact failure mode your own Shopify comment describes. Reindex uploads to a daily calendar (fill 0, ffill price) just like the Shopify path.

### 3.3 Backtest doesn't test the production pooling path
`backtest._relatives_as_of()` uses base `group_catalog` + `related_frames`, while live rebuilds use `adaptive_group_catalog` + `cluster_catalog` + `behavioral_relatives`. Backtest results for global-routed SKUs therefore validate a different (coarser) pool than production uses.

### 3.4 Prophet band/σ are in-sample below 120 days of history
Self-calibration (holdout refits) only kicks in at ≥120 days; below that, the conformal band and safety σ come from in-sample residuals, which understate error — exactly the optimism the calibration comment describes. Consider flagging these SKUs or applying a fixed widening factor under 120d.

### 3.5 Smaller items
- `IntermittentEngine` CV picks Croston vs TSB on as little as 1 window of MAE — noisy model selection.
- `route()` has two branches labeled "# 4" (`main`-visible only in comments — cosmetic, but the "croston anyway" branch means a 30-day intermittent orphan gets Croston while the docstring table says abstain; make the table match).
- Backtest runs `build_entry(calibrate=True)` at every cutoff → up to 6 refits per cutoff per SKU. Passing `calibrate=False` inside backtests (the buffer test already uses `lead_window_sigma`, which needs `oos_residuals`… so at least make `CALIB_WINDOWS` configurable per-call) would cut backtest time massively.

---

## 4. Backend robustness

- **`delete_sku` doesn't `_rebuild`**: remaining SKUs keep pooled forecasts built from the deleted SKU's data until the next catalog rebuild. Also relatedness counts go stale.
- **`save_events` doesn't validate the payload**: a malformed date/event makes the rebuild throw, and the SKU is popped from `_sku_cache` → it vanishes until re-upload. Validate event shape/dates before applying; on rebuild failure, keep the previous cache entry instead of dropping it.
- **No upload limits**: file read wholly into memory, no size/row cap, no rate limit.
- **Concurrency**: all state is module-level globals mutated by requests with no locking; two tabs (or an upload racing a forecast call) can interleave. Single-user today, but a cheap `threading.Lock` around rebuilds would prevent weird states. Global `_APP_TZ` mutated per request is the same pattern.
- **`/api/template` leaks temp files**: `NamedTemporaryFile(delete=False)` is never cleaned up.
- **`@app.on_event("startup")` is deprecated** in current FastAPI — migrate to lifespan handlers before an upgrade breaks it.
- **Shopify token handling**: token is typed into the browser and forwarded per request (not persisted — good), but it transits every backtest/import call. Consider storing it server-side (env or encrypted file) after first entry. In `fetch_sales`, if all 6 retries hit 429 the loop falls through and raises a generic error with the last 429 body — fine, but a clearer "rate-limited, try later" message would help.
- **`requirements.txt` is unpinned** (`prophet`, `pandas`, …) — a fresh install a year from now gets different numerics. Pin versions (or add a lock file).
- **Tests**: 49/50 pass — `get_groups lists ≥3 similar products for SPARSE` **fails** on the current code. Also everything runs against stubs (README is honest about this); there's no CI and no test with real prophet/statsforecast numerics.

## 5. Frontend

- **Errors are swallowed everywhere.** ~28 fetches, almost all `.catch(() => null)`. A failing SKU forecast, scorecard, or groups call produces no toast, no banner — things just go blank or vanish. Add a shared fetch helper with a visible error state; this is the enabler that let bug 1.1 hide.
- **`App.jsx` is 3,909 lines** with ~40 components in one file. Split it (Fleet, SkuDetail, Suppliers, Upload, modals), otherwise every change is a merge/regression hazard. No TypeScript, no frontend tests.
- **Frozen "today"**: `TODAY_MS`/`TODAY_STR` are computed at module load — a tab left open past midnight shows yesterday's dates/urgency until refresh.
- **Dead dependency**: `apexcharts` + `react-apexcharts` are in `package.json` but never imported (only recharts is used). Drop them.
- **`dist/` is committed** with the stale bundle (containing the leaked key). Add it to `.gitignore` and rebuild after the key rotation.
- **localStorage keys are unversioned** — future schema changes will need a migration or they'll crash old data. A tiny `{v: 1, data: ...}` wrapper now saves pain later.
- **No warning surfaces for `dateShiftDays` / `dataQuality`** returned by upload (badDatesDropped, duplicateRowsMerged are shown only if the upload details panel is opened — the date shift isn't shown at all).

## 6. Missing features (roadmap candidates, roughly ordered by value)

1. **Persistence + settings backup/restore** (see §2) — everything else is second to not losing user data.
2. **Purchase-order → forecast loop**: POs exist in localStorage, but there's no "receive against PO updates stock + logs actual lead time to the supplier automatically" flow end-to-end (receiving exists; PO history isn't fed back into per-SKU lead-time variance for the safety-stock σ).
3. **Refund/return netting for Shopify** (acknowledged in code comment).
4. **Groq attribute extraction server-side** (documented hook, not wired) — pairs with the key fix in 1.2.
5. **Multi-file / incremental uploads**: appending a new month to an existing catalog currently requires re-uploading the full history (a second upload adds/overwrites SKUs but re-anchors the whole timeline again).
6. **LightGBM global model** to replace the pooled-profile baseline (README follow-up).
7. **Scheduled auto-sync from Shopify** rather than manual pulls.
8. **Packaging**: `Start LogiTrack.command` is macOS-only; a Dockerfile or single-binary story would make deployment reproducible.

---

## Suggested fix order

| # | Item | Effort |
|---|------|--------|
| 1 | Fix `ts`/`tw` NameError + regression test | minutes |
| 2 | Revoke Groq key; proxy AI calls through backend; purge from dist | ~1 hr |
| 3 | Surface fetch errors in UI (shared helper + toast) | ~2 hrs |
| 4 | Persist catalog server-side; reload on startup | ~half day |
| 5 | Daily-calendar reindex for file uploads | ~1 hr |
| 6 | `dateShiftDays` warning banner (or cap the shift) | ~1 hr |
| 7 | Settings export/import (localStorage backup) | ~2 hrs |
| 8 | Validate events; don't drop cache entry on rebuild failure | ~1 hr |
| 9 | Rebuild on SKU delete; pin requirements; fix failing test | ~1 hr |
| 10 | Split App.jsx; drop apexcharts; version localStorage | ongoing |

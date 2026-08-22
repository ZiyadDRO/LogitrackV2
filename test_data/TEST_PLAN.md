# LogiTrack Test Plan

Four files, each exercising code paths the original sun-care dataset never hit. Upload them one at a time and report what you see — especially anything that **crashes, errors, or looks obviously wrong**. For each SKU, the quickest useful thing to report is the **engine/route** it picked (open **Forecast details** on that SKU) plus anything odd.

---

## File 1 — `1_mixed_catalog.xlsx`  (the main one)
7 SKUs across 3 categories. This is **winter-peaking** data (not summer) on purpose — to confirm seasonality isn't accidentally hard-coded to summer.

| SKU | What it tests | Expected behavior |
|---|---|---|
| SKIN-CLEANSER | Established + regular | **Prophet**. Yearly + weekly seasonality detected. Peak demand in **Dec–Jan**, trough in summer. |
| SKIN-MOIST | Same | **Prophet**, winter-peaking. |
| SKIN-SERUM | Stockout handling | **Prophet**. Has a 12-day **zero-stock window** → "Zero-stock days excluded from training" should read **~12**. |
| SKIN-TONER | Declining product + F-grade | **Prophet**, clear **downward** trend. Price is **$4.49** — to test the new **F grade**, open the Scorecard and enter a unit cost **above $4.49** (e.g. $5.00); grade should flip to **F (Loss)**, solid red. |
| SKIN-NIGHT-NEW | New SKU → pooling + ramp + guardrail | **Global model (pooled)** — borrows from the 4 Skincare siblings. **Launch ramp active**. **Protection level** set from margin. **New-product caution** note on the order. |
| SUPP-VITD | Intermittent, no relatives | **Croston/TSB**. Mostly zero-sale days. (Only supplement → nothing to pool with.) |
| CLEAR-MUG | Thin orphan | **Abstain — last-resort moving average** (40 days, no relatives). Should warn it's a rough placeholder. |

**Also check:** Stock auto-detects from the last row; the new SKU shows a launch-ramp note; the AI drawer (history off) answers a basic question without the token error.

---

## File 2 — `2_single_minimal.csv`
One product, **only Date + Units_Sold** — no SKU, price, or stock columns.
- **Expected:** loads fine; SKU is named after the file; forecasts with **Prophet** (400 days). No stock auto-detected (no column). No price tracking. Should NOT crash on the missing optional columns.

---

## File 3 — `3_messy.csv`
Deliberately messy single SKU: missing `Units_Sold` values, a **duplicate date**, a **5-day gap**, a **10-day stockout** (zero stock), and **rows out of date order**.
- **Expected:** loads **without crashing**; dates get sorted; blanks/dupes handled; ~10 zero-stock days excluded. The point is robustness — report any error or nonsense numbers.

---

## File 4 — `4_too_short.csv`
A 20-row SKU (below the 30-row minimum).
- **Expected:** **rejected/skipped** with a clear message like "fewer than 30 rows." Should fail *gracefully*, not crash.

---

## What to send back
For each file, just tell me:
1. Did it load, or error? (paste any error text)
2. For File 1: the **engine/route** each SKU landed on (and whether it matches the table).
3. Anything that looks wrong — absurd forecast numbers, a crash, a blank chart, a mislabeled grade, etc.

I'll interpret the results and we'll fix whatever the tests surface.

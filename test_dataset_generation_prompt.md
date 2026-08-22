# Prompt — Generate a real-world test dataset for the LogiTrack demand-forecasting tool

Copy everything below the line into a code-capable AI (one that can run Python and write an `.xlsx`). It will produce a single-sheet Excel workbook of daily sales data engineered to exercise every feature of the tool.

---

## Role and objective

You are a data engineer building a **synthetic but realistic** sales dataset to test an inventory demand-forecasting tool. The tool forecasts daily demand per SKU, recommends reorder timing and quantities, grades profitability, groups products that behave alike, and runs a leakage-safe backtest. Your dataset must contain products deliberately designed to trigger each of those capabilities and edge cases.

Produce **one Excel file**, `logitrack_realworld_test.xlsx`, with a **single sheet** named `Sales` containing the daily transaction data (schema below). Do **not** add a legend, notes, or expected-behavior sheet — data only.

Generate it programmatically with Python + pandas + openpyxl. Use a fixed random seed so it's reproducible.

## `Sales` sheet — exact schema

One row **per SKU per calendar day**, continuous from each SKU's first day to the dataset end date — **including zero-sale days** (write `Units_Sold = 0`, do not skip the row). This is critical: the tool expects a continuous daily grid; sparse/event-only rows make it misread demand.

| Column | Type | Notes |
|---|---|---|
| `SKU` | string | Stable ID, e.g. `BEV-COLA-01`. |
| `SKU_Name` | string | Human name, e.g. "Citrus Soda 330ml". |
| `Category` | string | Real product family used for grouping (see roster). |
| `Color` | string | A superficial attribute (red/black/etc.) — included to confirm the tool does NOT group on it. |
| `Date` | date | `YYYY-MM-DD`, daily. |
| `Units_Sold` | integer ≥ 0 | Whole units. 0 on no-sale days and stockout days. |
| `Price` | number | The actual selling price that day (reflects promos/price changes). |
| `Cost` | number | Per-unit cost. Drives margin, profit grade, and the backtest's tier economics. |
| `On_Promotion` | 0/1 | 1 on promo days (paired with a real price dip). |
| `Units_In_Stock` | integer ≥ 0 | On-hand at day's start; depletes with sales, jumps on replenishment; hits 0 during stockouts. |

## Global parameters

- **End date ("as of"):** the most recent Sunday on or before today. Established SKUs should span **~600 days** ending here (so ≥450 days of history unlock yearly seasonality; ≥14 unlock weekly).
- **~22 SKUs**, per the roster. Volumes range from a few units/day to several hundred/day.
- Integers for units; round prices to cents. No negative values anywhere.

## Realism and generation rules

For each SKU, build daily demand as: `base_rate × weekday_factor × seasonal_factor × price_factor × promo_factor`, then add noise (use a Poisson or negative-binomial draw around that mean so counts are integer and over-dispersion is realistic). Specifics:

- **Weekly pattern:** most retail peaks on weekends. Use weekday multipliers (e.g. Mon–Thu ≈ 0.9, Fri ≈ 1.1, Sat–Sun ≈ 1.3) for "weekly-seasonal" SKUs; keep flat for others.
- **Yearly seasonality:** for seasonal SKUs use a sinusoid over day-of-year — summer-peaking (max ~July) or winter-peaking (max ~January) — with a meaningful amplitude (peak ≈ 2× trough) so the model can detect it.
- **Price elasticity:** for price-sensitive SKUs, demand should clearly rise when price drops (e.g. a −10% price move lifts demand ~+15–25%). Encode `price_factor = (ref_price / price) ** elasticity`.
- **Promotions:** on promo days set `On_Promotion = 1`, drop `Price` ~15–30%, and let the price elasticity drive the demand spike (don't hand-spike demand separately — the price change should explain it).
- **Stock & replenishment:** simulate on-hand stock: deplete by daily sales, reorder when low, receive after a lead time. For the stockout SKU, force `Units_In_Stock = 0` (and `Units_Sold = 0`) for a couple of multi-day stretches.
- **Make patterns statistically clear**, not subtle — the point is to verify detection, so amplitudes/elasticities should be unambiguous.

## SKU roster — build exactly these

Group by the capability each tests. Pick sensible names, base rates, and prices; the **behavior** column is what matters.

### A. Established, clean (Prophet route, fully backtestable)
| SKU | Category | Pattern to encode | Tests |
|---|---|---|---|
| `BEV-COLA-01` | Beverages | ~600 days, strong weekly + **summer-peaking** yearly, low noise, **high margin** (price ≫ cost) | Prophet, weekly+yearly seasonality, grade A, backtest |
| `BEV-WATER-02` | Beverages | ~600 days, weekly + summer-peaking (co-moves with COLA-01), moderate margin | Behavioral grouping (should cluster with COLA-01), cohesion |
| `BEV-COCOA-03` | Beverages | ~600 days, **winter-peaking** (diverges from the others), moderate margin | Cohesion: should split into a different cluster / lower group cohesion |
| `STA-PEN-01` | Stationery | ~600 days, **flat/steady**, very low noise, **slim margin** | Steady demand, grade C, tight forecast bands |
| `ELEC-CABLE-01` | Electronics | ~600 days, **high volume** (200–400/day), mild weekly | High-volume handling; WAPE vs MASE behavior |

### B. Demand-pattern routing
| SKU | Category | Pattern | Tests |
|---|---|---|---|
| `STA-STAPLER-02` | Stationery | **Intermittent**: sells on ~1 day in 6, many zero days, ≥120 days history, no close peers | Croston/TSB route; high WAPE is expected (artifact) |
| `HW-DRILL-01` | Hardware | **Lumpy**: sporadic AND variable quantity (1, then 8, then 0…) | Lumpy classification, intermittent route |

### C. Price sensitivity & promotions
| SKU | Category | Pattern | Tests |
|---|---|---|---|
| `ELEC-EARBUD-01` | Electronics | 3 distinct price tiers, **each sampled 90+ days**, clear elasticity | Price-aware forecast, "all tiers reliable" |
| `ELEC-MOUSE-02` | Electronics | Price varies but some tiers **< 90 days** (thinly sampled) | "Price sensitivity — limited data" warning |
| `BEV-JUICE-04` | Beverages | A **permanent price increase** ~⅔ through the history | Price-change handling / structural shift |
| `SNK-CHIP-01` | Snacks | Recurring **promotions** (price dips + `On_Promotion=1`), demand spikes | Promo detection, normal-vs-on-sale baseline |

### D. Lifecycle stages
| SKU | Category | Pattern | Tests |
|---|---|---|---|
| `NEW-GADGET-3D` | Electronics | Only **3 days** of data at the very end | Cold start → "establishing baseline" (no forecast) |
| `NEW-BOTTLE-18D` | Beverages | Only **18 days** of data, modest upward ramp | Young/provisional tier (wider bands, amber) |
| `RAMP-SERUM-30D` | Beauty | ~35 days with a **clear upward growth trend** | Launch-ramp trend (damped growth) |
| `DISC-DVD-01` | Media | Sold normally for a year, then **no sales for the last ~210 days** | Inactive/discontinued → forecast forced to 0 |

### E. Margin, scorecard statuses & tier economics
| SKU | Category | Pattern | Tests |
|---|---|---|---|
| `SNK-CANDY-02` | Snacks | **Loss-making: Price ≤ Cost (+ fees)** every day | "Needs pricing review" section; excluded from tier cost comparison (grade F) |
| `HOME-MUG-01` | Home | Healthy demand, stock well-positioned, good margin | Scorecard status "Healthy", grade A/B |
| `HOME-VASE-02` | Home | **Overstocked**: stock far exceeds sell-through | Status "Overstocked", high cash tied up |
| `HOME-FRAME-03` | Home | **Dead stock**: barely sells AND overstocked | Status "Dead stock" |
| `KIT-PAN-01` | Kitchen | Demand rising into a **low stock** position near end | Status "Stockout risk" / "Reorder due" |

### F. Backtest edge cases & buffer behavior
| SKU | Category | Pattern | Tests |
|---|---|---|---|
| `SEAS-VOLATILE-01` | Outdoor | Established, but demand comes in **autocorrelated streaks** (good week / bad week clumping) | Lead-window σ / clumping-aware safety buffer; service-level calibration |
| `MISC-WIDGET-99` | `Core` | Normal demand but tagged with the **junk category "Core"** | Junk-category filter (should be treated as ungrouped, not pooled) |

> Coverage note: make sure across the roster you also hit a SKU **too short to backtest** (the 3-day and 18-day ones serve this — the backtest should list them as "skipped"), and at least one SKU in category Beverages with **little history that has ≥2 established peers** so the global-pooled route fires (`NEW-BOTTLE-18D` borrowing from the BEV cohort).

> The roster's "Tests" column above is **generation guidance only** — it tells you which pattern to encode. It must NOT appear anywhere in the output workbook.

## Validation checklist (run before saving)

1. Every SKU has continuous daily rows (no gaps, zeros present). Print each SKU's row count and date span.
2. No negative units/prices/stock; units and stock are integers.
3. Established SKUs span ≥ 450 days; the cold-start/young ones span 3 / 18 / ~35 days.
4. The loss-making SKU has `Price ≤ Cost` on every row; the high-margin ones have `Price ≥ ~2 × Cost`.
5. Promo SKU: `On_Promotion = 1` rows coincide with a real price dip.
6. Stockout SKU: has ≥ 2 stretches where `Units_In_Stock = 0` and `Units_Sold = 0`.
7. Discontinued SKU: last ~210 days all zero sales.
8. Print a short summary table (SKU, days, mean/day, price range, margin %) and confirm it matches the roster intent.

## Output

Save `logitrack_realworld_test.xlsx` with the single `Sales` sheet. Then print the validation summary to the console (not into the workbook) so I can eyeball it. Keep the generator script self-contained and reproducible (seeded).

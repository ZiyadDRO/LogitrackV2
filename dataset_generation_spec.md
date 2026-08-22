# Dataset Generation Spec — 25-SKU Realistic Retail Catalog for LogiTrack

Generate a **daily sales dataset** for **25 products over ~5 years**, modeled to look like a real retail catalog (real demand patterns, seasonality, promotions, stockouts, product lifecycles). Output one row **per SKU per calendar day** with no gaps.

---

## 1. Output format (must match exactly)

A single `.xlsx` file (sheet named `Sales Data`) **or** a `.csv`. Columns, in this order:

| Column | Type | Notes |
|---|---|---|
| `SKU` | text | Stable product ID, e.g. `COF-01`. |
| `SKU_Name` | text | Human name, e.g. `House Blend Ground Coffee 1kg`. |
| `Category` | text | **Drives grouping/"relatives" — products sharing a Category are pooled.** Use the categories below exactly. |
| `Date` | `YYYY-MM-DD` | One row per SKU per day, continuous, no missing days. |
| `Units_Sold` | integer ≥ 0 | Actual units sold that day (0 on no-sale or out-of-stock days). |
| `Price` | decimal | Selling price that day (drops during promotions). |
| `On_Promotion` | 0 or 1 | 1 on promo days. |
| `Units_In_Stock` | integer ≥ 0 | On-hand at end of day. Hits 0 during stockouts. |

**Date range:** roughly the last 5 years, ending within the last week of today's date. Established SKUs span the full range; the "new" and "discontinued" SKUs are exceptions (see below).

---

## 2. How to model daily demand (realism rules)

For each SKU and each day, compute an **expected** demand, then draw an integer from a Poisson distribution around it (this gives realistic day-to-day noise automatically):

```
expected = base_level
         × yearly_season(month)      # seasonal multiplier
         × weekday_factor(weekday)   # day-of-week multiplier
         × trend(day_index)          # growth/decline over time
         × promo_boost(is_promo_day) # demand spike during promos
units_sold = Poisson(expected), then clamp to available stock
```

- **Yearly seasonality:** a smooth sinusoid peaking in the SKU's season (summer ≈ June–Aug, winter ≈ Dec–Feb). Amplitude per SKU below (e.g. ±50% means demand swings from 0.5× to 1.5× across the year). Year-round SKUs use a small amplitude (±10%).
- **Day-of-week:** retail SKUs sell ~20–40% more on weekends; office SKUs sell ~30–50% **less** on weekends. Keep it consistent per category.
- **Trend:** `flat`, `growing` (level rises over the span), or `declining` (level falls). For growing/declining, move smoothly from a start level to an end level.
- **Promotions:** ~6–12 per year on promo-eligible SKUs, lasting 2–5 days. On promo days set `On_Promotion=1`, drop `Price` by 10–25%, and multiply expected demand by 1.5–2.5×. Price-sensitive SKUs get the bigger lift.
- **Noise:** Poisson handles most of it; for "erratic/lumpy" SKUs, add extra variance (occasional spikes).

**Stock simulation (so `Units_In_Stock` is realistic):**
- Start each SKU at roughly 30–45 days of cover. Each day, subtract that day's sales.
- When on-hand falls below ~10 days of cover, "place an order"; after a lead time of ~10–18 days, add a replenishment (back up to ~40 days of cover).
- If stock reaches 0 before the replenishment lands, **force `Units_Sold = 0` on those days** (you can't sell what you don't have) and keep `Units_In_Stock = 0` until restocked. For most SKUs keep stockouts **rare and short** (a few short episodes over 5 years). Two SKUs are exceptions (frequent stockouts / permanent — see below).

---

## 3. The 25 SKUs

Five categories of five products each. Keep the `Category` values **identical within a group** so pooling works.

### Category: `Coffee & Tea` — steady consumables, mild winter lift
| SKU | Name | Base/day | Season | Trend | Notes |
|---|---|---|---|---|---|
| COF-01 | House Blend Ground Coffee 1kg | ~40 | +15% winter | flat | high-volume steady |
| COF-02 | Espresso Beans 500g | ~25 | +15% winter | flat | steady |
| COF-03 | Decaf Ground 500g | ~12 | +10% winter | flat | slower steady |
| COF-04 | Iced Coffee Concentrate | ~18 | **+60% summer** | flat | clear summer peak |
| **COF-05** | **Oat Milk Latte Mix (NEW)** | ~15 | +15% winter | flat | **★ TEST: only the most recent ~30 days of history exist — no rows before that. Same Category as COF-01..04 so it borrows their seasonal shape.** |

### Category: `Camping & Outdoor` — strong summer seasonality
| SKU | Name | Base/day | Season | Trend | Notes |
|---|---|---|---|---|---|
| CMP-01 | 2-Person Tent | ~6 | **+70% summer** | flat | higher price (~$120) |
| CMP-02 | Sleeping Bag | ~8 | **+60% summer** | flat | price ~$60 |
| CMP-03 | Portable Camp Stove | ~4 | +50% summer | flat | slow + seasonal |
| CMP-04 | LED Headlamp | ~20 | +20% summer | flat | steadier |
| CMP-05 | Solar Power Bank | 5→25 | +20% summer | **growing** | ★ TEST: clear upward trend over the span |

### Category: `Winter Apparel` — strong winter seasonality
| SKU | Name | Base/day | Season | Trend | Notes |
|---|---|---|---|---|---|
| WIN-01 | Wool Gloves | ~15 | **+80% winter, ~0 summer** | flat | sharp seasonal |
| WIN-02 | Knit Beanie | ~18 | **+70% winter** | flat | |
| WIN-03 | Wool Scarf | ~6 | +70% winter | flat | ★ TEST: near-zero / intermittent in summer |
| WIN-04 | Thermal Socks 3-pack | ~22 | +30% winter | flat | sells year-round |
| WIN-05 | Fleece Earmuffs | 20→5 | +60% winter | **declining** | ★ TEST: fading product |

### Category: `Phone Accessories` — price-sensitive, promo-driven
| SKU | Name | Base/day | Season | Trend | Notes |
|---|---|---|---|---|---|
| PHN-01 | Silicone Phone Case | ~35 | flat | flat | ★ TEST: **strongly price-sensitive**, frequent promos |
| PHN-02 | USB-C Charging Cable | ~50 | flat | flat | high volume, big promo spikes |
| PHN-03 | Tempered Glass Screen Protector | ~30 | flat | flat | companion — track PHN-01's promos |
| PHN-04 | Wireless Charging Pad | 8→30 | flat | **growing** | trending up |
| **PHN-05** | **MagSafe Car Mount (DISCONTINUED)** | ~12 | flat | — | **★ TEST: sells only the first ~10 months, then `Units_Sold=0` and `Units_In_Stock=0` for the remaining ~4 years (never restocked). This tests the dormancy/discontinued guard.** |

### Category: `Office Supplies` — weekday-heavy, B2B-style
| SKU | Name | Base/day | Season | Trend | Notes |
|---|---|---|---|---|---|
| OFF-01 | Ballpoint Pens 50-pack | ~30 | flat | flat | ★ weekday-heavy, weekends low |
| OFF-02 | A4 Notebooks | ~20 | +40% Aug–Sep | flat | back-to-school peak |
| OFF-03 | Heavy-Duty Stapler | ~3 | flat | flat | ★ TEST: **intermittent** — many zero-sale days |
| OFF-04 | Desk Organizer | ~10 | flat | flat | steady |
| **OFF-05** | **Sticky Notes Bulk** | ~25 | flat | flat | **★ TEST: unreliable supply — frequent, longer out-of-stock stretches (aim for ~20–30% of days at 0 stock, scattered throughout). Tests in-stock-only scoring.** |

---

## 4. The specific things this dataset must exercise

1. **New product with relatives (REQUIRED):** `COF-05` has only ~30 days of history but lives in `Coffee & Tea` alongside four established SKUs → forces the **global pooled engine** (borrow seasonal shape + level from relatives).
2. **Discontinued / dormant:** `PHN-05` dies after ~10 months → must trigger the **inactive/discontinued** flag, not a fake overdue reorder.
3. **Frequent stockouts:** `OFF-05` → tests that scoring ignores stockout days and doesn't read them as "demand died."
4. **Intermittent demand:** `OFF-03` (and `WIN-03` in summer) → tests the Croston/intermittent route.
5. **Strong seasonality:** camping (summer) and winter apparel (winter) → tests yearly seasonality.
6. **Trend up / down:** `CMP-05` & `PHN-04` (growing), `WIN-05` (declining).
7. **Price sensitivity:** `PHN-01`, `PHN-02` (demand clearly moves with price/promo).
8. **Day-of-week patterns:** office supplies (weekday-heavy) vs retail (weekend-heavy).

---

## 5. Validation checklist (before handing the file over)

- [ ] Exactly 25 distinct SKUs, 5 per Category, Category spelled identically within each group.
- [ ] One row per SKU per day, continuous dates, no gaps (except COF-05 which starts ~30 days ago, and the date range itself).
- [ ] `Units_Sold` is 0 (not blank) on no-sale and out-of-stock days; never negative.
- [ ] `Units_In_Stock` decrements with sales and hits 0 only during intended stockouts; PHN-05 stays 0 after discontinuation.
- [ ] `On_Promotion` is 1 only on promo days, and `Price` actually drops those days.
- [ ] Seasonal SKUs show a visible yearly cycle; trending SKUs show a visible slope.
- [ ] COF-05 has ≤ ~40 rows total; PHN-05 has full-range rows but zero sales after ~month 10.
- [ ] Realistic magnitudes (no negative prices, stock never absurd).

The point of this file is to be *real-shaped but known* — so when LogiTrack's backtest runs, you can check each engine and edge case against demand you understand, while still testing patterns a clean synthetic file wouldn't have.

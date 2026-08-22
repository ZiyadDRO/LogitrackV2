# Master test dataset — `logitrack_master_test.xlsx`

24 SKUs · 13,860 rows · ends 2026-07-31 (the tool re-anchors to yesterday on upload).
Every SKU exists to prove one feature. Verified against the real router: **24/24 land on
their designed route**, and every warning/exclusion below fired in the harness.

Regenerate anytime: `python build_master_dataset.py` (same folder; seeded, so identical output).

## Upload it
Upload with **"Add to what's already loaded" OFF** (it should replace, not stack into, other test files).
One backtest starts automatically; the newcomers are too young to backtest and will say so — that's by design.

## Families & routes

| SKU | Days | Tests | Expect |
|---|---|---|---|
| VAN-SSF-01..04 | 880–900 | Prophet veterans, spring peak (Mar–May), weekend-heavy | route **prophet**, yearly seasonality ON |
| VAN-SSF-01 | 900 | Permanent price cut $799→$749 at ~day 450 | price tiers learned; "price change" event math |
| VAN-SSF-02 | 900 | 12-day stockout ~5 months ago | stockout days dropped from training, not read as zero demand |
| VAN-SSF-03 | 900 | Price never changed | "only ever sold at $999" price warning |
| VAN-SSF-05 | 860 | Sells at $1,099 against $1,120 cost | flagged **loss-making**, excluded from tier economics with the pricing-not-buffering message |
| VAN-SSF-NEW1/2/3 | 120 / 75 / 45 | Newcomers selling 8–18/day **steadily** | route **global** (pooled from the 4–5 SSF veterans); forecast must be non-flat with weekly shape + spring lift borrowed from peers |
| VAN-SSF-DEAD | 650 | Sold well for 400d, then zero for ~250d | **inactive** (forecast 0), excluded from pooling so it can't poison the newcomers |
| VAN-FLT-01..04 | 870–900 | Floating family, **summer** peak (distinct cluster from SSF) | prophet; clustering must NOT merge them with SSF |
| VAN-FLT-02 | 900 | **No cost** in the file | "no unit cost" exclusion + incomplete-totals warning; enter a cost in the Scorecard → instant re-price (no refit) |
| VAN-FLT-03 | 890 | Erratic demand with 4–8× spikes | the volatility fingerprint: over-order + lower service together |
| VAN-FLT-04 | 870 | Declining −35%/yr | damped trend down, order sizes shrink |
| VAN-FLT-NEW | 110 | Steady newcomer | global, pooled from FLT (summer shape, not spring) |
| MIR-LED-01..03 | 600–640 | Holiday peak (Nov–Dec), high margins (~65%) | prophet; MIR-LED-01 also **grows +45%/yr** (under-buffer-on-trend case) |
| MIR-LED-NEW | 85 | Steady newcomer | global from 3 mirror veterans |
| FCT-BRS-01..03 | 700–760 | Intermittent/lumpy veterans WITH relatives | route **croston** despite relatives — sporadic demand must not be pooled |
| PRT-SPARE-01 | 450 | Intermittent orphan (own category) | croston, no relatives |
| KIT-SINK-01 | 220 | Low-volume (~3/day) but regular, orphan | prophet at low volume — many selling days beat raw units |
| CLR-ODD-01 | 45 | Thin orphan, **no price data** | route **abstain** (flagged placeholder) + "no price column" warning |

## What the backtest should show
- Tested 18/24; the 6 young SKUs listed under "not tested" with exact day counts.
- Cost basis **partial (16/18)** until you add VAN-FLT-02's cost; SSF-05 flagged loss-making.
- Margins range ~15–65%, so per-product optimal service spans ~78–99% — the
  **per-product mix should genuinely WIN** here (harness: $103.6k vs $127.1k for the best
  single level, spread ~13@90 / 1@98 / 2@99). This is the dataset that proves per-product
  tuning earns its keep — the vanities file couldn't, because every margin was identical.
- Band hit rate should sit near 75–85 (a percentage — if you ever see 3000, that's the
  fixed column-collision bug regressing).
- Auto cutoffs: veterans get ~13–17 windows, KIT-SINK ~3–4 (dimmed as thin).

## Panel spot-checks
- NEW SKUs: protection says *why* it isn't measured (needs ~N more days), forecast is NOT flat.
- VAN-SSF-DEAD: inactive, zero forecast, no safety stock.
- Change one SKU's lead time → only that pair re-tests; others stay "tested".
- Harness numbers above come from stubbed engines — exact values will differ under real
  Prophet, but routes, warnings, exclusions and the mix-wins signal are structural.

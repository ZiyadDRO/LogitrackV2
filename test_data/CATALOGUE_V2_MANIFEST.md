# LogiTrack test catalogue v2 — what each product is for

`logitrack_catalogue_v2.xlsx` — 32 products, 19,352 rows, Aug 2023 → 1 Aug 2026.
Three sheets: **Sales** (upload this), **Summary** (per-SKU totals), **Bundles** (the
component map to paste into `POST /api/bundles`).

Every product exists to force one behaviour. If a SKU is here, some code path only shows
itself when that SKU is loaded. Numbers below are what the engine actually produced on
this file, so a different answer means something changed.

---

## Routing — all four paths

| SKU | History | Should route to | Why it's here |
|---|---|---|---|
| `VAN-HAR-24` | 3 years | **prophet** | The reference veteran. Also the pooling donor for newcomers. |
| `VAN-HAR-27` | 95 days | **global-pooled** | Real sales, too short alone. Should inherit the Harlow weekly + Q4 shape. |
| `FLT-ARI-27` | 48 days | **global-pooled** | Thinner still — tests the lower bound of pooling. |
| `ACC-BLK-02` | 2 years | **croston** | Rare, large bursts. True lumpy demand. |
| `EDG-BRANDNEW` | 9 days | **abstain** | Too new, no usable peers. Should refuse rather than guess. |

`ACC-HNG-01` is the **low-volume regular** case — sells most days in ones and twos. It
must stay on **Croston, not TSB**: TSB decays demand probability through zero runs, which
is right for a dying product and wrong for a steady trickle.

---

## Seasonality — including the new intermittent path

The winter and summer groups are deliberately opposed so a cohort index can't be
one-directional.

| SKU | Season | Measured index |
|---|---|---|
| `SEA-SHV-01` Snow Shovel | winter | own history, strength 0.56 — Dec **1.63×**, Jul **0.75×** |
| `SEA-ICE-03` Ice Melt | winter | own history, strength 0.59 — Dec **1.70×**, Jul **0.65×** |
| `SEA-FAN-04` Tower Fan | summer | own history, strength 0.64 — Jul **1.73×**, Dec **0.54×** |
| `SEA-SHV-NEW` | winter | **cohort**, strength 0.76 — under a year, so it borrows |

**What to check:** all five are intermittent, so before this feature they'd forecast flat
year-round. The product page should show a *Seasonal pattern* banner naming the peak and
quiet months. `SEA-SHV-NEW` should say the shape came from *similar products*.

---

## Price

| SKU | Setup | Measured |
|---|---|---|
| `FCT-BRS-01` | price swings $99–$183, true elasticity −1.6 | **−1.34** measured, 50% spread |
| `FCT-TAL-03` | permanent rise $159 → $199 at day 480 | **−0.69**, 20% spread |
| `FCT-BRS-02` | price never moves | **refused** — "price-never-varied" |

That last row is the important one: elasticity must be **0.00**, not a small number
fitted to noise. `GET /api/price-history?sku=FCT-TAL-03` should show one change point.

---

## Promotions

| SKU | Promo days | Measured lift | Route |
|---|---|---|---|
| `VAN-HAR-24` | 36 | **1.95×** | prophet — modelled as a regressor |
| `ACC-BLK-02` | 42 | **1.90×** | croston — applied as an explicit multiplier |
| `EDG-PROMOHEAVY` | 208 of 365 | 1.54× | **promo-heavy control** — 57% of days are promotional |
| `FLT-ARI-24` | 0 | **1.00×** | never promoted; must claim no effect |

`EDG-PROMOHEAVY` is the guard case: promotions are >40% of its history, so they must
**not** be held out — discounting is how that product sells, and stripping it would model
a business that doesn't exist.

---

## Censored demand

| SKU | Censored days | What it tests |
|---|---|---|
| `MIR-LUM-28` | 45 (6.2%) capped + a 10-day stockout | Busy days ending at zero — the old rule deleted these |
| `MIR-LUM-36` | 40 (11.0%) capped | Chronic under-stocking; should be flagged loudly |
| `VAN-HAR-24` | 4 (0.4%) | Occasional — should barely move the forecast |
| `FLT-ARI-24` | 0 | Clean control; banner must not appear |

**What to check:** the product page shows *"Adjusted for days you ran out"* with the units
the raw sales were hiding. A spreadsheet can't distinguish "closed at zero" from "had
nothing all day", so these read as **capped**, not unavailable — that distinction needs
Shopify inventory polling, which only works forward.

---

## Bundles

Paste the **Bundles** sheet into `POST /api/bundles`:

```json
{"bundles": {"KIT-VAN-SET": {"VAN-HAR-24": 1, "MIR-BUN-03": 1, "FCT-BUN-04": 2}}}
```

`FCT-BUN-04` barely sells on its own (intermittent, 0.12/day). Without attribution its
demand nearly vanishes; with it, the component should show a high `sharePct` and be
flagged **bundleDriven** — its forecast is really a forecast of the kit.

---

## Edge cases

| SKU | Tests |
|---|---|
| `EDG-NOPRICE` | No price column — margin rule can't apply, protection falls back |
| `EDG-NOCOST` | No cost — tier costing must skip it rather than assume |
| `EDG-NOSTOCK` | No stock column — censoring must be a complete no-op |
| `EDG-DEAD` | Discontinued at day 560 — should be caught, not forecast forward |
| `EDG-LOSS` | Sells at $210, costs $265 — loss-making, correctly excluded from tier costing |
| `VAN-BEC-36` | A clean 14-day stockout stretch |

---

## What this file can't test

Three features need **live data**, not history:

- **Arrival detection** — needs timestamped inventory polling, so connect Shopify and let
  the tick run. You can fake it: log a PO, then let stock rise.
- **Hours-in-stock** — same. Uploaded rows only carry closing stock, so partial-day
  sellouts read as capped rather than partial.
- **Live accuracy** — needs a week to pass. It seals forecasts each Sunday and grades them
  once the window closes; nothing appears until then, by design.

## Suggested first run

1. Upload the **Sales** sheet (replace mode).
2. Set a holding rate, then enter costs — watch the per-SKU backtest fill in.
3. Check `SEA-SHV-NEW` for a borrowed seasonal shape.
4. Check `MIR-LUM-36` for the censoring banner.
5. Check `FCT-BRS-02` shows no elasticity, and `FCT-BRS-01` does.
6. Post the bundle map and re-upload — `FCT-BUN-04`'s demand should jump.

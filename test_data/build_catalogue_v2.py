"""
LogiTrack test catalogue v2 — every routing path and every correction, on purpose.

Each product exists to force one behaviour. Nothing here is filler: if a SKU is in this
file, some code path only shows itself when that SKU is loaded. The manifest names what
each one should produce, so a wrong answer is visible rather than plausible.

Coverage:
  ROUTING     prophet / global-pooled / croston / abstain
  DEMAND      smooth, erratic, intermittent, lumpy, low-volume-regular
  SEASONAL    strong yearly on a fast seller, and on an INTERMITTENT one (the new path)
  PRICE       real elasticity, a permanent change, and a never-moved control
  PROMO       measurable lift on Prophet AND on Croston, plus a promo-heavy control
  CENSORING   ended-at-zero days, mid-day sellouts, and full stockout stretches
  BUNDLES     a kit plus its three components, one of which barely sells alone
  EDGE        no price, no cost, discontinued, brand new, single-day, loss-making
"""

import numpy as np
import pandas as pd

RNG = np.random.default_rng(20260802)

END = pd.Timestamp("2026-08-01")          # last day of history
DAYS_3Y = 1095
DAYS_2Y = 730
DAYS_1Y = 365

MONTHS = {
    "winter": {1: 2.6, 2: 2.2, 3: 1.4, 4: 0.8, 5: 0.5, 6: 0.4,
               7: 0.4, 8: 0.5, 9: 0.8, 10: 1.5, 11: 2.2, 12: 2.7},
    "summer": {1: 0.4, 2: 0.5, 3: 0.8, 4: 1.3, 5: 1.9, 6: 2.5,
               7: 2.7, 8: 2.4, 9: 1.5, 10: 0.8, 11: 0.5, 12: 0.4},
    "flat":   {m: 1.0 for m in range(1, 13)},
    "q4":     {1: 0.7, 2: 0.7, 3: 0.8, 4: 0.9, 5: 0.9, 6: 0.9,
               7: 0.9, 8: 1.0, 9: 1.1, 10: 1.5, 11: 2.2, 12: 2.4},
}
WEEK = {
    "retail":  [0.85, 0.90, 0.95, 1.00, 1.05, 1.35, 1.30],   # Mon..Sun, weekend-heavy
    "trade":   [1.20, 1.20, 1.15, 1.15, 1.10, 0.60, 0.60],   # weekday-heavy
    "flat":    [1.0] * 7,
}

ROWS = []


def build(sku, name, *, days, base, season="flat", week="retail", noise=0.25,
          price=None, price_path=None, cost=None, promo_windows=(), promo_lift=1.0,
          intermittent=None, trend=0.0, stockouts=(), zero_close=(), dead_after=None,
          start_offset=0, elasticity=None, category=None, subcategory=None,
          stock_level=None, no_stock_col=False):
    """One product's daily history.

    `zero_close`  day-indices where stock ends at 0 but the product was on sale all day
                  (the CAPPED case — sales are a floor, not the truth)
    `stockouts`   (start, end) ranges with nothing to sell at all (the UNAVAILABLE case)
    `intermittent` probability of a sale on any given day; None means sells daily
    """
    start = END - pd.Timedelta(days=days - 1 + start_offset)
    dates = pd.date_range(start, periods=days, freq="D")

    mo = np.array([MONTHS[season][d.month] for d in dates])
    wk = np.array([WEEK[week][d.weekday()] for d in dates])
    tr = np.linspace(1.0, 1.0 + trend, days)
    level = base * mo * wk * tr

    # Price path first: elasticity is applied to demand, so the two stay consistent.
    if price_path is not None:
        prices = np.array([price_path(i, dates[i]) for i in range(days)], dtype=float)
    elif price is not None:
        prices = np.full(days, float(price))
    else:
        prices = np.full(days, np.nan)

    promo = np.zeros(days, dtype=int)
    for a, b in promo_windows:
        promo[a:b] = 1

    if elasticity is not None and price is not None and not np.isnan(prices).all():
        level = level * (prices / float(price)) ** float(elasticity)

    level = level * np.where(promo == 1, promo_lift, 1.0)

    if intermittent is not None:
        # A seasonal slow-mover doesn't sell the same NUMBER of days year-round with
        # bigger baskets — it sells on more days in season. Varying the hit rate with the
        # season as well as the basket keeps the signal realistic AND strong enough to be
        # visible; a fixed hit rate flattens the observed monthly ratios badly.
        rate = np.clip(intermittent * mo, 0.02, 0.95)
        hits = RNG.random(days) < rate
        basket = np.maximum(level / np.maximum(rate, 1e-6), 0.5)
        y = np.where(hits, RNG.poisson(basket), 0)
    else:
        y = RNG.poisson(np.maximum(level, 0.01))
    y = y.astype(float)

    if dead_after is not None:
        y[dead_after:] = 0

    # Stock: a plausible saw-tooth, then the deliberate censoring cases carved into it.
    if stock_level is None:
        stock_level = max(20, int(base * 45))
    stock = np.full(days, float(stock_level))
    run = float(stock_level)
    for i in range(days):
        run -= y[i]
        if run <= stock_level * 0.15:
            run = float(stock_level)
        stock[i] = max(0.0, run)

    for i in zero_close:
        if 0 <= i < days:
            stock[i] = 0.0
            y[i] = max(y[i], np.ceil(level[i] * 1.6))     # busy day that ran the shelf dry

    for a, b in stockouts:
        stock[a:b] = 0.0
        y[a:b] = 0.0                                      # nothing to sell, so nothing sold

    df = pd.DataFrame({
        "SKU": sku, "SKU_Name": name, "Date": dates,
        "Units_Sold": y.astype(int),
        "Price": prices.round(2),
        "On_Promotion": promo,
        "Units_In_Stock": (np.nan if no_stock_col else stock.astype(int)),
        "Cost": (np.nan if cost is None else float(cost)),
        "Category": category or "General",
        "Subcategory": subcategory or "General",
    })
    ROWS.append(df)
    return df


VAN = dict(category="Bathroom Vanities", subcategory="Freestanding")
FLT = dict(category="Bathroom Vanities", subcategory="Floating")
MIR = dict(category="Mirrors", subcategory="LED")
FCT = dict(category="Faucets", subcategory="Widespread")
SEA = dict(category="Seasonal", subcategory="Weather")
ACC = dict(category="Accessories", subcategory="Hardware")

# ── 1. PROPHET VETERANS ─────────────────────────────────────────────────────────────
# Long, regular, seasonal. These also serve as the POOLING DONORS for newcomers and the
# COHORT DONORS for the intermittent seasonal products further down.
build("VAN-HAR-24", "Harlow 24in Vanity", days=DAYS_3Y, base=3.2, season="q4",
      week="retail", price=389.0, cost=196.0, trend=0.15,
      promo_windows=[(300, 312), (665, 677), (1030, 1042)], promo_lift=2.6,
      zero_close=[712, 713, 951, 1044], **VAN)

build("VAN-HAR-30", "Harlow 30in Vanity", days=DAYS_3Y, base=2.6, season="q4",
      week="retail", price=449.0, cost=228.0, trend=0.10,
      promo_windows=[(300, 312), (665, 677), (1030, 1042)], promo_lift=2.4, **VAN)

build("VAN-BEC-36", "Beckett 36in Vanity", days=DAYS_2Y, base=2.1, season="q4",
      week="retail", price=529.0, cost=268.0,
      promo_windows=[(300, 312), (665, 677)], promo_lift=2.2,
      stockouts=[(410, 424)], **VAN)

# ── 2. PRICE ELASTICITY ─────────────────────────────────────────────────────────────
# Prices genuinely move, and demand genuinely responds. Elasticity should come out near
# -1.6. Its twin never changes price, so elasticity must be refused there.
build("FCT-BRS-01", "Brixton Brass Faucet", days=DAYS_2Y, base=4.0, season="flat",
      week="trade", cost=41.0, price=129.0, elasticity=-1.6,
      # A wide, genuinely varying price path — enough spread for elasticity to be
      # recoverable through Poisson noise rather than attenuated to nothing.
      price_path=lambda i, d: 99.0 + (i % 120 // 15) * 14.0, **FCT)

build("FCT-BRS-02", "Brixton Chrome Faucet", days=DAYS_2Y, base=3.6, season="flat",
      week="trade", cost=38.0, price=119.0, **FCT)   # price never moves — control

# A permanent price rise partway through: the tool should see the level shift.
build("FCT-TAL-03", "Brixton Tall Faucet", days=DAYS_2Y, base=2.8, season="flat",
      week="trade", cost=52.0, price=159.0, elasticity=-1.2,
      price_path=lambda i, d: 159.0 if i < 480 else 199.0, **FCT)

# ── 3. CENSORED DEMAND ──────────────────────────────────────────────────────────────
# The whole point of censoring.py. Many days end at zero stock on the BUSIEST days, so
# raw sales understate demand and the old drop-the-row rule would delete the peak.
build("MIR-LUM-28", "Lumina 28in LED Mirror", days=DAYS_2Y, base=5.0, season="q4",
      week="retail", price=219.0, cost=104.0,
      zero_close=list(range(300, 316)) + list(range(660, 676)) + [700, 701, 702],
      stockouts=[(500, 510)], stock_level=60, **MIR)

# Chronic under-stocking: a large share of days are capped. Should be flagged loudly.
build("MIR-LUM-36", "Lumina 36in LED Mirror", days=DAYS_1Y, base=6.5, season="q4",
      week="retail", price=289.0, cost=139.0,
      zero_close=list(range(120, 200, 2)), stock_level=25, **MIR)

# ── 4. SEASONAL INTERMITTENT — the new path ─────────────────────────────────────────
# Sells a handful of days a month, heavily in winter. Croston alone would forecast this
# flat; with the cohort index it should lift going into Q4/January.
for sku, nm, base in [("SEA-SHV-01", "Alpine Snow Shovel", 0.55),
                      ("SEA-SHV-02", "Alpine Snow Shovel XL", 0.40),
                      ("SEA-ICE-03", "Ice Melt 10kg", 0.65)]:
    build(sku, nm, days=DAYS_2Y, base=base, season="winter", week="flat",
          intermittent=0.22, price=59.0, cost=24.0, stock_level=40, **SEA)

# The opposite season, so the cohort can't be one-directional.
for sku, nm, base in [("SEA-FAN-04", "Breeze Tower Fan", 0.50),
                      ("SEA-FAN-05", "Breeze Desk Fan", 0.45)]:
    build(sku, nm, days=DAYS_2Y, base=base, season="summer", week="flat",
          intermittent=0.25, price=79.0, cost=32.0, stock_level=40, **SEA)

# A NEW seasonal intermittent — under a year, so it must borrow the cohort's shape.
build("SEA-SHV-NEW", "Alpine Snow Shovel Compact", days=150, base=0.5, season="winter",
      week="flat", intermittent=0.20, price=49.0, cost=21.0, stock_level=30, **SEA)

# ── 5. INTERMITTENT VARIANTS ────────────────────────────────────────────────────────
# Low-volume REGULAR: sells most days, small numbers. Must stay on Croston, not TSB.
build("ACC-HNG-01", "Vanity Hinge Kit", days=DAYS_2Y, base=1.1, season="flat",
      week="trade", price=18.0, cost=6.4, stock_level=120, **ACC)

# True lumpy: rare, large bursts. Promo lift on a NON-Prophet route.
build("ACC-BLK-02", "Contractor Bulk Pack", days=DAYS_2Y, base=0.9, season="flat",
      week="trade", intermittent=0.07, price=340.0, cost=150.0,
      promo_windows=[(200, 214), (560, 574), (700, 714)], promo_lift=3.0, **ACC)

# ── 6. POOLED NEWCOMERS ─────────────────────────────────────────────────────────────
# Real activity, too short to model alone. Should route to global-pooled and inherit the
# veterans' weekly + yearly shape.
build("VAN-HAR-27", "Harlow 27in Vanity", days=95, base=2.4, season="q4",
      week="retail", price=419.0, cost=212.0, **VAN)
build("VAN-BEC-42", "Beckett 42in Vanity", days=70, base=1.9, season="q4",
      week="retail", price=579.0, cost=291.0, **VAN)
build("FLT-ARI-27", "Aria 27in Floating", days=48, base=1.6, season="q4",
      week="retail", price=469.0, cost=236.0, **FLT)

# ── 7. BUNDLE + COMPONENTS ──────────────────────────────────────────────────────────
# KIT-VAN-SET sells as one line item but consumes three products. FCT-BUN-04 barely
# sells alone, so without attribution its demand nearly vanishes.
build("KIT-VAN-SET", "Harlow Complete Vanity Set", days=DAYS_2Y, base=1.4, season="q4",
      week="retail", price=849.0, cost=430.0,
      promo_windows=[(300, 312), (665, 677)], promo_lift=2.5, **VAN)
build("MIR-BUN-03", "Harlow Bundle Mirror", days=DAYS_2Y, base=0.9, season="q4",
      week="retail", price=149.0, cost=68.0, **MIR)
build("FCT-BUN-04", "Harlow Bundle Faucet", days=DAYS_2Y, base=0.12, season="flat",
      week="trade", intermittent=0.10, price=99.0, cost=44.0, **FCT)

# ── 8. FLOATING RANGE (a second cohort, so pooling isn't one big blob) ──────────────
build("FLT-ARI-24", "Aria 24in Floating", days=DAYS_2Y, base=2.2, season="flat",
      week="retail", price=399.0, cost=201.0, **FLT)
build("FLT-ARI-30", "Aria 30in Floating", days=DAYS_2Y, base=1.8, season="flat",
      week="retail", price=459.0, cost=232.0, **FLT)
build("FLT-NOV-48", "Nova 48in Floating", days=DAYS_2Y, base=1.3, season="flat",
      week="retail", price=699.0, cost=354.0, **FLT)

# ── 9. EDGE CASES ───────────────────────────────────────────────────────────────────
build("EDG-NOPRICE", "Fireclay Kitchen Sink", days=DAYS_2Y, base=1.5, season="flat",
      week="retail", price=None, cost=180.0, **VAN)          # no price column at all
build("EDG-NOCOST", "Halo 32in LED Mirror", days=DAYS_2Y, base=2.0, season="flat",
      week="retail", price=199.0, cost=None, **MIR)          # no cost → no tier costing
build("EDG-NOSTOCK", "Halo 24in LED Mirror", days=DAYS_1Y, base=1.7, season="flat",
      week="retail", price=169.0, cost=79.0, no_stock_col=True, **MIR)
build("EDG-DEAD", "Retired 54in Vanity", days=DAYS_2Y, base=1.6, season="flat",
      week="retail", price=499.0, cost=252.0, dead_after=560, **VAN)
build("EDG-LOSS", "Clearance 60in Vanity", days=DAYS_1Y, base=1.2, season="flat",
      week="retail", price=210.0, cost=265.0, **VAN)         # sells below cost
build("EDG-BRANDNEW", "Harlow 21in Vanity", days=9, base=1.4, season="flat",
      week="retail", price=349.0, cost=176.0, **VAN)         # abstain: too new, few peers
build("EDG-PROMOHEAVY", "Odd-lot Shower Caddy", days=DAYS_1Y, base=1.0, season="flat",
      week="retail", price=39.0, cost=15.0,
      promo_windows=[(i, i + 4) for i in range(0, 360, 7)], promo_lift=1.8, **ACC)

# ── write ───────────────────────────────────────────────────────────────────────────
df = pd.concat(ROWS, ignore_index=True).sort_values(["Date", "SKU"]).reset_index(drop=True)
df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

out = "logitrack_catalogue_v2.xlsx"
with pd.ExcelWriter(out, engine="openpyxl") as xl:
    df.to_excel(xl, sheet_name="Sales", index=False)

    summary = (df.groupby(["SKU", "SKU_Name"], as_index=False)
                 .agg(Days=("Date", "count"), Units=("Units_Sold", "sum"),
                      First=("Date", "min"), Last=("Date", "max"),
                      PromoDays=("On_Promotion", "sum"),
                      ZeroStockDays=("Units_In_Stock", lambda s: int((s.fillna(1) <= 0).sum())),
                      Price=("Price", "last"), Cost=("Cost", "last")))
    summary["DailyAvg"] = (summary["Units"] / summary["Days"]).round(2)
    summary.to_excel(xl, sheet_name="Summary", index=False)

    pd.DataFrame([
        {"Bundle": "KIT-VAN-SET", "Component": "VAN-HAR-24", "QtyPerBundle": 1},
        {"Bundle": "KIT-VAN-SET", "Component": "MIR-BUN-03", "QtyPerBundle": 1},
        {"Bundle": "KIT-VAN-SET", "Component": "FCT-BUN-04", "QtyPerBundle": 2},
    ]).to_excel(xl, sheet_name="Bundles", index=False)

print(f"wrote {out}")
print(f"  {df['SKU'].nunique()} products, {len(df):,} rows, "
      f"{df['Date'].min()} → {df['Date'].max()}")
print(f"  {int(df['On_Promotion'].sum()):,} promo days, "
      f"{int((df['Units_In_Stock'].fillna(1) <= 0).sum()):,} zero-stock days")

"""
build_master_dataset.py — the master synthetic catalog for exercising EVERY LogiTrack path.

Design principle: every SKU exists to prove one specific feature works, and the whole
catalog is tuned against router.py's real thresholds (ESTABLISHED_DAYS=180, NEW_DAYS=60,
MIN_RELATIVES=2, RELATIVE_MIN_DAYS=180), so each product lands on its intended route
instead of wherever the noise happens to drop it.

Families share genuine seasonal shape (that's what behavioural clustering keys on), and
the newcomers sell STEADILY from day one — the fix for "choppy new SKUs get flat
forecasts". Margins vary widely on purpose so the per-product-mix comparison has real
signal for once (a uniform-margin catalog makes the mix pointless by construction).

Regenerate:  python build_master_dataset.py     (writes logitrack_master_test.xlsx here)
"""
import numpy as np
import pandas as pd

END = pd.Timestamp("2026-07-31")
rng = np.random.default_rng(42)


def _yearly(doy, peak_doy, width, amp):
    """Gaussian annual bump with wraparound (so a December peak spills into January)."""
    d = np.minimum(np.abs(doy - peak_doy), 365 - np.abs(doy - peak_doy))
    return 1.0 + amp * np.exp(-0.5 * (d / width) ** 2)


def _price_series(n, base, steps, never_change=False, perm_change=None):
    """Step-wise price history: `steps` small permanent moves at random dates, so most
    SKUs expose 2-4 learnable price tiers. `perm_change=(day, new)` forces one big cut."""
    p = np.full(n, float(base))
    if not never_change:
        for d in sorted(rng.integers(60, max(n - 30, 61), size=steps)):
            p[d:] = round(float(p[d]) * float(rng.uniform(0.93, 1.08)), 0) - 0.01
    if perm_change:
        day, new = perm_change
        if day < n:
            p[day:] = new
    return np.round(p, 2)


def _promos(n, dates, per_year=2, bf=True):
    """on_promotion flags: Black Friday week each year + `per_year` random 10-day promos."""
    promo = np.zeros(n, dtype=int)
    if bf:
        promo[(dates.month == 11) & (dates.day >= 24)] = 1
    years = max(1, n // 365)
    for _ in range(per_year * years):
        s = int(rng.integers(30, max(n - 12, 31)))
        promo[s:s + 10] = 1
    return promo


def make_sku(sku, name, days, base, *, weekly, peak, width, amp, price, cost,
             trend=0.0, kind="smooth", steps=3, never_change=False, perm_change=None,
             promo_per_year=2, stockout=None, dead_after=None, no_price=False,
             attrs=None):
    dates = pd.date_range(END - pd.Timedelta(days=days - 1), END, freq="D")
    n = len(dates)
    doy = dates.dayofyear.to_numpy()
    wk = np.array(weekly)[dates.weekday.to_numpy()]
    yr = _yearly(doy, peak, width, amp)
    tr = (1.0 + trend) ** (np.arange(n) / 365.0)
    prices = _price_series(n, price, steps, never_change, perm_change)
    promo = _promos(n, dates, promo_per_year)
    on_promo_price = prices * np.where(promo == 1, 0.82, 1.0)
    elasticity = (on_promo_price / price) ** -1.4          # cheaper → more demand
    lift = np.where(promo == 1, 1.9, 1.0)
    mu = base * wk * yr * tr * lift * elasticity

    if kind == "smooth":
        y = rng.poisson(np.clip(mu, 0.05, None)).astype(float)
    elif kind == "erratic":                                 # overdispersed + rare 4-8x spikes
        y = rng.poisson(rng.gamma(2.0, np.clip(mu, 0.05, None) / 2.0)).astype(float)
        spikes = rng.random(n) < 0.02
        y[spikes] *= rng.integers(4, 9, size=spikes.sum())
    elif kind == "intermittent":                            # many zero days, small sizes
        sell = rng.random(n) < np.clip(mu / (mu + 2.2), 0.04, 0.6)
        y = np.where(sell, 1 + rng.poisson(np.clip(mu, 0.1, None) * 0.55), 0).astype(float)
    elif kind == "lumpy":                                   # rare days, chunky sizes
        sell = rng.random(n) < 0.14
        y = np.where(sell, 1 + rng.poisson(3.2), 0).astype(float)
    else:
        raise ValueError(kind)

    stock = np.clip(210 + 120 * np.sin(np.arange(n) / 23 + rng.uniform(0, 6))
                    + rng.normal(0, 18, n), 25, None).astype(int)
    if stockout:                                            # a real out-of-stock stretch
        s, e = stockout
        stock[s:e] = 0
        y[s:e] = 0
    if dead_after is not None:                              # product discontinued mid-history
        y[dead_after:] = 0

    df = pd.DataFrame({
        "SKU": sku, "SKU_Name": name, "Date": dates, "Units_Sold": y.astype(int),
        "Price": (np.nan if no_price else on_promo_price.round(2)),
        "On_Promotion": promo, "Units_In_Stock": stock,
        "Cost": (np.nan if cost is None else float(cost)),
    })
    for k, v in (attrs or {}).items():
        df[k] = v
    return df


V = {"weekly": [0.85, 0.90, 0.95, 1.00, 1.05, 1.35, 1.30]}          # weekend showroom traffic
F = {"weekly": [1.15, 1.20, 1.20, 1.15, 1.10, 0.70, 0.65]}          # trade buyers, weekday
M_ = {"weekly": [0.95, 0.95, 1.00, 1.00, 1.05, 1.15, 1.10]}

ssf = dict(Category="Bathroom Vanities", Subcategory="Single Sink Freestanding")
flt = dict(Category="Bathroom Vanities", Subcategory="Floating Wall-Mount")
led = dict(Category="Mirrors", Subcategory="LED Mirrors")
brs = dict(Category="Faucets", Subcategory="Brass Faucets")

frames = [
    # ── Family A: freestanding vanities — spring peak, 5 veterans + 3 newcomers ──
    make_sku("VAN-SSF-01", "Harlow 24in Vanity", 900, 14, **V, peak=105, width=42, amp=0.45,
             price=799, cost=390, attrs={**ssf, "Material": "Solid Wood", "Size": "24in"},
             perm_change=(450, 749.00)),                     # permanent price cut mid-history
    make_sku("VAN-SSF-02", "Harlow 30in Vanity", 900, 11, **V, peak=105, width=42, amp=0.45,
             price=899, cost=445, attrs={**ssf, "Material": "Solid Wood", "Size": "30in"},
             stockout=(740, 752)),                           # 12-day stockout → exclusion path
    make_sku("VAN-SSF-03", "Beckett 36in Vanity", 900, 9, **V, peak=105, width=42, amp=0.45,
             price=999, cost=520, attrs={**ssf, "Material": "MDF", "Size": "36in"},
             never_change=True, steps=0),                    # price never moved → warning
    make_sku("VAN-SSF-04", "Beckett 48in Vanity", 880, 7, **V, peak=105, width=42, amp=0.45,
             price=1299, cost=610, attrs={**ssf, "Material": "MDF", "Size": "48in"}),
    make_sku("VAN-SSF-05", "Clearance 60in Vanity", 860, 6, **V, peak=105, width=42, amp=0.45,
             price=1099, cost=1120, attrs={**ssf, "Material": "Plywood", "Size": "60in"}),  # sells BELOW cost
    make_sku("VAN-SSF-NEW1", "Harlow 27in Vanity (new)", 120, 12, **V, peak=105, width=42, amp=0.45,
             price=849, cost=410, steps=0, attrs={**ssf, "Material": "Solid Wood", "Size": "27in"}),
    make_sku("VAN-SSF-NEW2", "Beckett 42in Vanity (new)", 75, 9, **V, peak=105, width=42, amp=0.45,
             price=1199, cost=560, steps=0, attrs={**ssf, "Material": "MDF", "Size": "42in"}),
    make_sku("VAN-SSF-NEW3", "Harlow 21in Vanity (brand new)", 45, 8, **V, peak=105, width=42, amp=0.45,
             price=749, cost=350, steps=0, attrs={**ssf, "Material": "Solid Wood", "Size": "21in"}),
    make_sku("VAN-SSF-DEAD", "Retired 54in Vanity", 650, 8, **V, peak=105, width=42, amp=0.45,
             price=1199, cost=580, dead_after=400,           # zero sales for last ~250d
             attrs={**ssf, "Material": "Plywood", "Size": "54in"}),

    # ── Family B: floating vanities — summer peak; erratic + declining members ──
    make_sku("VAN-FLT-01", "Aria 24in Floating", 900, 10, **V, peak=190, width=38, amp=0.5,
             price=729, cost=400, attrs={**flt, "Material": "Oak", "Size": "24in"}),
    make_sku("VAN-FLT-02", "Aria 30in Floating", 900, 8, **V, peak=190, width=38, amp=0.5,
             price=849, cost=None, attrs={**flt, "Material": "Oak", "Size": "30in"}),        # NO cost
    make_sku("VAN-FLT-03", "Nova 36in Floating", 890, 9, **V, peak=190, width=38, amp=0.5,
             price=999, cost=480, kind="erratic", attrs={**flt, "Material": "Walnut", "Size": "36in"}),
    make_sku("VAN-FLT-04", "Nova 48in Floating", 870, 12, **V, peak=190, width=38, amp=0.5,
             price=1399, cost=690, trend=-0.35, attrs={**flt, "Material": "Walnut", "Size": "48in"}),
    make_sku("VAN-FLT-NEW", "Aria 27in Floating (new)", 110, 10, **V, peak=190, width=38, amp=0.5,
             price=799, cost=415, steps=0, attrs={**flt, "Material": "Oak", "Size": "27in"}),

    # ── Family C: LED mirrors — holiday peak, high margins, one strong grower ──
    make_sku("MIR-LED-01", "Lumina 28in LED Mirror", 640, 13, **M_, peak=335, width=30, amp=0.65,
             price=449, cost=155, trend=0.45, attrs={**led, "Material": "Aluminum", "Size": "28in"}),
    make_sku("MIR-LED-02", "Lumina 36in LED Mirror", 620, 10, **M_, peak=335, width=30, amp=0.65,
             price=549, cost=190, attrs={**led, "Material": "Aluminum", "Size": "36in"}),
    make_sku("MIR-LED-03", "Halo 32in LED Mirror", 600, 8, **M_, peak=335, width=30, amp=0.65,
             price=499, cost=175, attrs={**led, "Material": "Steel", "Size": "32in"}),
    make_sku("MIR-LED-NEW", "Halo 24in LED Mirror (new)", 85, 11, **M_, peak=335, width=30, amp=0.65,
             price=399, cost=140, steps=0, attrs={**led, "Material": "Steel", "Size": "24in"}),

    # ── Family D: brass faucets — intermittent veterans (croston DESPITE relatives) ──
    make_sku("FCT-BRS-01", "Brixton Brass Faucet", 760, 2.2, **F, peak=20, width=50, amp=0.12,
             price=289, cost=238, kind="intermittent", attrs={**brs, "Material": "Brass", "Size": "8in"}),
    make_sku("FCT-BRS-02", "Brixton Tall Faucet", 740, 1.8, **F, peak=20, width=50, amp=0.12,
             price=329, cost=272, kind="intermittent", attrs={**brs, "Material": "Brass", "Size": "12in"}),
    make_sku("FCT-BRS-03", "Widespread Brass Faucet", 700, 1.4, **F, peak=20, width=50, amp=0.12,
             price=379, cost=310, kind="lumpy", attrs={**brs, "Material": "Brass", "Size": "10in"}),

    # ── Orphans: no usable relatives, each proving a fallback ──
    make_sku("PRT-SPARE-01", "Vanity Hinge Kit", 450, 1.5, **F, peak=20, width=60, amp=0.05,
             price=24.99, cost=6, kind="intermittent", steps=1,
             attrs=dict(Category="Spare Parts", Subcategory="Hardware", Material="Steel", Size="Std")),
    make_sku("KIT-SINK-01", "Fireclay Kitchen Sink", 220, 2.6, **M_, peak=280, width=45, amp=0.2,
             price=649, cost=330, steps=1,
             attrs=dict(Category="Kitchen", Subcategory="Sinks", Material="Fireclay", Size="33in")),
    make_sku("CLR-ODD-01", "Odd-lot Shower Caddy", 45, 1.6, **M_, peak=200, width=60, amp=0.1,
             price=39.99, cost=14, steps=0, no_price=True,
             attrs=dict(Category="Clearance", Subcategory="Misc", Material="Wire", Size="Std")),
]

df = pd.concat(frames, ignore_index=True).sort_values(["SKU", "Date"]).reset_index(drop=True)
df.to_excel("logitrack_master_test.xlsx", index=False)
print(f"{df['SKU'].nunique()} SKUs, {len(df):,} rows -> logitrack_master_test.xlsx")
print(df.groupby("SKU")["Units_Sold"].agg(days="size", units="sum",
      zero_share=lambda s: round((s == 0).mean(), 2)).to_string())

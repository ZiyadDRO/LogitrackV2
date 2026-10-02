"""
forecast_engine.py — shared, model-agnostic helpers used by the router build.

This is now a small helper library (no heavy forecasting deps). The actual
forecasting lives in engines.py; which engine runs is decided by router.py and
orchestrated in main.py. Kept here:

  • analyse_price / price tiers / safe-extrapolation range  (carry over unchanged)
  • classify_demand (Syntetos–Boylan)                        (drives routing)
"""
from __future__ import annotations
import numpy as np
import pandas as pd

# Three-tier price sampling thresholds.
MIN_DAYS_PRICE_INSUFFICIENT = 30
MIN_DAYS_PRICE_MODERATE     = 60
MIN_DAYS_PRICE_RELIABLE     = 90


# ════════════════════════════════════════════════════════════════════════════
#  DEMAND CLASSIFICATION  (Syntetos–Boylan)
# ════════════════════════════════════════════════════════════════════════════
def classify_demand(y: np.ndarray) -> str:
    """smooth | erratic | intermittent | lumpy | no_demand."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    nz = y[y > 0]
    if n == 0 or nz.size == 0:
        return "no_demand"
    adi = n / int(np.count_nonzero(y > 0))
    cv2 = (nz.std() / nz.mean()) ** 2 if nz.mean() > 0 else 0.0
    if adi < 1.32 and cv2 < 0.49:
        return "smooth"
    if adi < 1.32 and cv2 >= 0.49:
        return "erratic"
    if adi >= 1.32 and cv2 < 0.49:
        return "intermittent"
    return "lumpy"


def is_sparse(demand_class: str) -> bool:
    return demand_class in ("intermittent", "lumpy", "no_demand")


# A product selling on nearly half its days at a steady couple of units is a "slow
# recurring seller", not a rare-event product — even though the ADI test files both under
# "intermittent". The distinction matters for how you TALK about it: long gaps and rare
# sales deserve "may not sell for weeks"; a regular trickle deserves "about N a month".
# A silence this long, AND this far past the product's own typical gap, means stopped
# rather than quiet. Both conditions are required.
DORMANT_MIN_DAYS = 45
DORMANT_GAP_MULTIPLE = 4.0

LOW_VOLUME_REGULAR_MIN_SHARE = 0.30    # sells on ≥30% of days
LOW_VOLUME_REGULAR_MAX_RATE = 1.5      # but averages under this many units/day


def trailing_zero_run(y: np.ndarray) -> int:
    """How many days since the last sale."""
    y = np.asarray(y, dtype=float)
    n = 0
    for v in y[::-1]:
        if v > 0:
            break
        n += 1
    return n


def is_off_season(ds, y, as_of=None) -> bool:
    """Quiet now the way it was quiet this time last year, and not fading: its season is
    simply somewhere else in the year.

    A Christmas line in September, or a pool float at the end of summer, has sold little
    lately, which reads as dormant or as dead stock (and used to be labelled so, and left
    out of every family). Neither is: last year it was just as quiet in these same weeks,
    and it sold as much over the year. All of:
      · the last 30 days sold under a quarter of its usual 30 days (over the past year),
      · the same 30 days a year ago were just as quiet against that year's pace,
      · the past year sold at least half what the year before did (a fad fading out,
        or a line being run down, is not seasonal).
    """
    try:
        d = pd.to_datetime(pd.Series(ds)).reset_index(drop=True)
        v = pd.Series(np.asarray(y, dtype=float)).reset_index(drop=True)
    except Exception:                                   # noqa: BLE001
        return False
    if len(d) < 400:
        return False
    t = pd.Timestamp(as_of) if as_of is not None else d.max() + pd.Timedelta(days=1)

    def tot(a_days, b_days):
        a, b = t - pd.Timedelta(days=a_days), t - pd.Timedelta(days=b_days)
        m = ((d > a) & (d <= b)).to_numpy()
        return float(v[m].sum()), int(m.sum())

    yr, n_yr = tot(365, 0)
    prev, n_prev = tot(730, 365)
    now, _ = tot(30, 0)
    ly, n_ly = tot(395, 365)
    if n_yr < 300 or n_ly < 20 or yr < 6:
        return False
    pace = yr / n_yr * 30.0
    pace_ly = (prev / n_prev * 30.0) if n_prev >= 300 else pace
    if now > 0.25 * pace or ly > 0.25 * max(pace_ly, 1e-9):
        return False
    if n_prev >= 300 and yr < 0.5 * prev:
        return False
    return True


def is_dormant(y: np.ndarray, *, min_days: int = DORMANT_MIN_DAYS,
               gap_multiple: float = DORMANT_GAP_MULTIPLE) -> bool:
    """Has this product simply stopped selling?

    Judged against its OWN rhythm, not a fixed threshold: 40 quiet days is nothing for a
    product that sells once a month and is a clear stop for one that sells every other
    day. Both a long absolute silence and a silence far beyond its usual gap are required,
    so an ordinary lull can't trip it.

    The rhythm is `adi` over the ACTIVE period only — see the note below for why. Note
    that adi is periods-per-demand, which for a sparse series sits below the true
    inter-arrival gap, so the effective bar is several missed cycles rather than literally
    four. Measured, silence required before a product is called dormant:

        sells daily ... weekly      45 days   (the floor)
        every 14 days               52 days   ( ~3.7 cycles)
        every 30 days              101 days   ( ~3.4 cycles)
        every 60 days              162 days   ( ~2.7 cycles)
        every 90 days              182 days   ( ~2.0 cycles)

    Monotonic in the selling rhythm, which is the property that matters: a slower line
    always gets more rope than a faster one, and none of them get infinite rope.
    """
    y = np.asarray(y, dtype=float)
    if y.size == 0:
        return False
    tail = trailing_zero_run(y)
    if tail < min_days:
        return False
    nz = int((y > 0).sum())
    if nz == 0:
        return True

    # The gap is measured over the ACTIVE period — first sale to last sale — not over the
    # whole padded series. Using the full length put the trailing silence into its own
    # threshold: every quiet day lengthened the "usual gap" it had to beat, so the bar
    # ran away from the product. Measured, that made a monthly seller need 273 days of
    # silence and a bi-monthly one never qualify at all, while the docstring promised
    # four times its usual gap. Now it means what it says.
    active = y[: y.size - tail]
    if active.size == 0:
        return True                        # nothing but silence
    adi = active.size / max(int((active > 0).sum()), 1)
    return tail >= max(min_days, adi * gap_multiple)


def sparse_subtype(y: np.ndarray, demand_class: str) -> str:
    """'low_volume_regular' | 'true_intermittent' | '' (not sparse)."""
    if not is_sparse(demand_class):
        return ""
    y = np.asarray(y, dtype=float)
    if y.size == 0:
        return "true_intermittent"
    # A product that sold steadily for eighteen months and then stopped still LOOKS
    # regular when you average the whole history — share and rate are both computed
    # over all of it. Calling it "regular" then excludes the one model that would decay
    # it, so a discontinued line goes on being forecast at its old rate indefinitely.
    if is_dormant(y):
        return "true_intermittent"
    share = float((y > 0).mean())
    rate = float(y.mean())
    if share >= LOW_VOLUME_REGULAR_MIN_SHARE and rate <= LOW_VOLUME_REGULAR_MAX_RATE:
        return "low_volume_regular"
    return "true_intermittent"


# ════════════════════════════════════════════════════════════════════════════
#  DAILY-CALENDAR GAP FILL
# ════════════════════════════════════════════════════════════════════════════
def fill_daily_gaps(df: pd.DataFrame, end=None) -> tuple[pd.DataFrame, int]:
    """Reindex ONE SKU's frame (must have a 'ds' datetime column) to a continuous
    daily calendar from its first row to `end`, filling the gaps.

    `end` defaults to this SKU's own last row, which is what this did originally and is
    wrong in one specific, expensive way: a product that stopped selling gets a series
    ending on the day it stopped, so the silence afterwards does not exist. That made
    `trailing_zero_run` structurally always 0, so `is_dormant` could never fire and a
    discontinued line went on being forecast at its old rate forever.

    Callers that know the real extent of the data pass it: for a file, the LAST DATE IN
    THE FILE across all its products — a sheet covering through September genuinely tells
    you a product that stopped in June has been silent since. Days past the end of the
    data are still unknown and are never invented.

    Why: many exports only write a row for days that HAD a sale. Handing that
    straight to the engines averages demand over sale-days only — the model never
    sees the zeros — so it over-forecasts, and the demand classifier can't detect
    intermittency (ADI ≈ 1 always). The Shopify source already does this fill;
    this makes file uploads (and the backtest) behave identically.

    Fills: y → 0, on_promotion → 0, price → ffill/bfill, units_in_stock →
    ffill/bfill then 1 (assume in stock when unknown — matches upload hygiene),
    any other column (attributes like category) → ffill/bfill (per-SKU constants).
    Returns (filled_df, days_added)."""
    if df is None or len(df) < 2 or "ds" not in df.columns:
        return df, 0
    d = df.sort_values("ds").copy()
    d["ds"] = pd.to_datetime(d["ds"])
    if "price_mixed" in d.columns:
        d["price_mixed"] = pd.to_numeric(d["price_mixed"], errors="coerce").fillna(0).astype(int)
    _stop = d["ds"].max() if end is None else max(d["ds"].max(), pd.to_datetime(end))
    full = pd.date_range(d["ds"].min(), _stop, freq="D")
    if len(full) <= len(d):
        return d.reset_index(drop=True), 0
    n_added = int(len(full) - len(d))
    d = d.set_index("ds").reindex(full)
    d.index.name = "ds"
    for c in d.columns:
        if c == "y":
            d[c] = d[c].fillna(0)
        elif c == "on_promotion":
            d[c] = d[c].fillna(0)
        elif c == "price":
            if "price_mixed" in d.columns:
                # A gap day carries the last SINGLE price, not a mixed day's blend.
                listed = d[c].where(pd.to_numeric(d["price_mixed"], errors="coerce").fillna(0) < 1)
                carry = listed.ffill().bfill()
                d[c] = d[c].where(d[c].notna(), carry).ffill().bfill()
            else:
                d[c] = d[c].ffill().bfill()
        elif c == "price_mixed":
            pass                       # filled last, below: a gap day sold nothing, so it is never mixed
        elif c == "units_in_stock":
            d[c] = d[c].ffill().bfill().fillna(1)
        else:
            d[c] = d[c].ffill().bfill()
    if "price_mixed" in d.columns:
        d["price_mixed"] = pd.to_numeric(d["price_mixed"], errors="coerce").fillna(0).astype(int)
    return d.reset_index(), n_added


# ════════════════════════════════════════════════════════════════════════════
#  PRICE ANALYSIS  (model-agnostic)
# ════════════════════════════════════════════════════════════════════════════

# A price level needs this many days behind it before it counts as evidence of how demand
# responds to price. It is also the floor between the "insufficient" and "weak" labels, so
# the label a price carries and whether it is used can never disagree.
PRICE_LEVEL_MIN_DAYS = 30
# Price is only a forecast input when at least this many levels each clear that floor.
# One level is a price, not a comparison; nothing can be learned from it about response.
PRICE_LEVELS_REQUIRED = 2


def _is_mixed(lo, hi) -> bool:
    """Did one day's sales happen at genuinely different prices?

    Not "any difference". Square and Shopify report what a line PAID, net of discounts
    and rounded to the cent, so the same listed price can come back as 49.99 on one line
    and 50.00 on the next. Two cents or half a percent, whichever is larger, is noise.
    """
    if lo is None or hi is None:
        return False
    return (hi - lo) > max(0.02, 0.005 * abs(hi))


# A day counts at one price when at least this share of its units sold there. The rest
# (a coupon, a staff discount, a price match) is part of that day's demand at the shelf
# price. Requiring 100% made busy stores lose most of their days: at 30 units a day and
# 3% of sales discounted, about 60% of days had one discounted sale and were thrown out.
DOMINANT_PRICE_SHARE = 0.80


def daily_price_rollup(lines: pd.DataFrame, keys=("sku", "sku_name", "date")) -> pd.DataFrame:
    """Collapse line items to one row per product per day, at the price most units sold at.

    Each day gets:
      price         the shelf price: the price at least DOMINANT_PRICE_SHARE of the day's
                    units sold at (cent-rounding noise counts as the same price). With no
                    such price, the units-weighted average of what customers paid.
      price_mixed   1 only when no single price covers that share of units: a buy-one-
                    get-one day where half the shoppers took the deal, or a day the price
                    changed partway through. Such a day is evidence of neither price, so
                    it isn't counted toward a price level or used to measure response.
      price_listed  the shelf price on an unmixed day; blank on a mixed one. It is what
                    gets carried onto the following zero-sale days.
      on_promotion  on an unmixed day, whether most of the shelf-price units were
                    discounted; on a mixed day, whether any were.
    """
    d = lines.copy()
    key_cols = list(keys)
    d["_q"] = pd.to_numeric(d["units_sold"], errors="coerce").fillna(0)
    d["_p"] = pd.to_numeric(d["price"], errors="coerce")
    d["_promo"] = (pd.to_numeric(d["on_promotion"], errors="coerce").fillna(0) > 0).astype(int) \
        if "on_promotion" in d.columns else 0
    has_p = d["_p"].notna() & (d["_q"] > 0)

    agg = {"units_sold": ("_q", "sum"), "_plast": ("_p", "last")}
    if "on_promotion" in d.columns:
        agg["on_promotion"] = ("_promo", "max")
    for extra in ("category",):
        if extra in d.columns:
            agg[extra] = (extra, "first")
    g = d.groupby(key_cols, as_index=False, sort=False).agg(**agg)

    # Units at each exact price, per day.
    priced = d[has_p].copy()
    priced["_pr"] = priced["_p"].round(2)
    priced["_pu"] = priced["_q"] * priced["_promo"]
    by_price = priced.groupby(key_cols + ["_pr"], sort=False).agg(_u=("_q", "sum"), _pu=("_pu", "sum"))

    verdict = {}
    for k, grp in by_price.groupby(level=list(range(len(key_cols))), sort=False):
        k = k if isinstance(k, tuple) else (k,)
        prices = grp.index.get_level_values(-1).to_numpy(dtype=float)
        units = grp["_u"].to_numpy(dtype=float)
        promo_u = grp["_pu"].to_numpy(dtype=float)
        total = units.sum()
        if total <= 0:
            continue
        top = prices[units.argmax()]
        same = np.array([not _is_mixed(min(top, p), max(top, p)) for p in prices])
        share = units[same].sum() / total
        if share >= DOMINANT_PRICE_SHARE:
            price = float((prices[same] * units[same]).sum() / units[same].sum())
            promo = int(promo_u[same].sum() / units[same].sum() > 0.5)
            verdict[k] = (round(price, 2), 0, promo)
        else:
            verdict[k] = (round(float((prices * units).sum() / total), 2), 1, int(promo_u.sum() > 0))

    keys_iter = list(zip(*[g[c] for c in key_cols]))
    g["price"] = [verdict[k][0] if k in verdict else pl for k, pl in zip(keys_iter, g["_plast"])]
    g["price"] = pd.to_numeric(g["price"], errors="coerce").round(2)
    g["price_mixed"] = [verdict[k][1] if k in verdict else 0 for k in keys_iter]
    if "on_promotion" in g.columns:
        g["on_promotion"] = [verdict[k][2] if k in verdict else op for k, op in zip(keys_iter, g["on_promotion"])]
    g["price_listed"] = g["price"].where(g["price_mixed"] == 0)
    return g.drop(columns=["_plast"])


def carry_listed_price(g: pd.DataFrame) -> pd.DataFrame:
    """Fill price on zero-sale days from the last SINGLE-price day.

    A zero-sale day was sold at nothing, so its price is the shelf price in force — the
    last unambiguous price, not the weighted blend of a mixed day. Carrying the blend
    forward would invent a price level ($46.67) that was never on the shelf and then
    count every quiet day after it as evidence for that fictional level.
    """
    g = g.copy()
    if "price_mixed" not in g.columns:
        g["price_mixed"] = 0
    g["price_mixed"] = pd.to_numeric(g["price_mixed"], errors="coerce").fillna(0).astype(int)
    listed = g["price_listed"] if "price_listed" in g.columns else g["price"].where(g["price_mixed"] == 0)
    carry = listed.ffill().bfill()
    if carry.isna().all():
        carry = g["price"].ffill().bfill()
    g["price"] = g["price"].where(g["price"].notna(), carry)
    g["price_listed"] = listed.where(listed.notna(), carry.where(g["price_mixed"] == 0))
    return g
def _tier_quality(days: int) -> str:
    if days >= MIN_DAYS_PRICE_RELIABLE:
        return "reliable"
    if days >= MIN_DAYS_PRICE_MODERATE:
        return "moderate"
    if days >= MIN_DAYS_PRICE_INSUFFICIENT:
        return "weak"
    return "insufficient"


# Two qualifying levels closer together than this are the same price as far as measuring
# a response goes: a 2% difference moves demand by less than day-to-day noise.
PRICE_LEVEL_MIN_SPREAD = 0.05


def _single_price_days(df: pd.DataFrame) -> pd.Series:
    """Prices of the days that sold at ONE price (or sold nothing at the shelf price).

    A mixed day — five at $50 and one at $40 — is evidence of neither price, so it is
    left out of everything that asks "how many days has this product spent at $X?".
    """
    if "price" not in df.columns:
        return pd.Series(dtype=float)
    p = pd.to_numeric(df["price"], errors="coerce")
    if "price_mixed" in df.columns:
        mixed = pd.to_numeric(df["price_mixed"], errors="coerce").fillna(0) >= 1
        p = p[~mixed]
    return p.dropna()


def _cluster_levels(prices: pd.Series) -> list[dict]:
    """Group prices into levels, treating cent-rounding noise as the same level.

    49.99 and 50.00 are one shelf price reported two ways; counting them as two levels
    would split its days in half and could stop either half reaching the 30-day floor.
    Each level is named by its most common exact value.
    """
    if prices.empty:
        return []
    counts = prices.round(2).value_counts()
    levels: list[dict] = []
    for p in sorted(counts.index):
        if levels and not _is_mixed(levels[-1]["_anchor"], p):
            lv = levels[-1]
            lv["days"] += int(counts[p])
            lv["_members"][float(p)] = int(counts[p])
        else:
            levels.append({"_anchor": float(p), "days": int(counts[p]),
                           "_members": {float(p): int(counts[p])}})
    out = []
    for lv in levels:
        name = max(lv["_members"].items(), key=lambda kv: (kv[1], kv[0]))[0]
        out.append({"price": round(name, 2), "days": lv["days"],
                    "quality": _tier_quality(lv["days"])})
    return out


def price_evidence(df: pd.DataFrame) -> dict:
    """What this product's history can and cannot say about price.

    The single place that decides whether price is allowed to move a forecast:

      usable   True only when at least PRICE_LEVELS_REQUIRED different prices each have
               PRICE_LEVEL_MIN_DAYS single-price days behind them, and those prices are
               at least PRICE_LEVEL_MIN_SPREAD apart. One price is a price, not a
               comparison. One day at a second price is an anecdote. Below this bar a
               price event does NOTHING to the forecast and says why.
      rangeLo/rangeHi
               the span between the cheapest and dearest QUALIFYING levels. Scheduled
               prices are clamped near it (see bounds in build_entry), because outside it
               the response has never been observed and a linear model would happily
               extrapolate one day's coincidence into a doubling of demand.
      levels   every price level with its day count and 30/60/90 label, so what the page
               shows and what the gate uses can never disagree.
      mixedDays
               days that sold at genuinely different prices — reported, never counted.
    """
    out = {"levels": [], "qualifying": [], "mixedDays": 0, "usable": False,
           "reason": "no-price-data", "rangeLo": None, "rangeHi": None,
           "lastListedPrice": None, "minDays": PRICE_LEVEL_MIN_DAYS,
           "levelsRequired": PRICE_LEVELS_REQUIRED}
    if df is None or "price" not in df.columns:
        return out
    if "price_mixed" in df.columns:
        out["mixedDays"] = int((pd.to_numeric(df["price_mixed"], errors="coerce").fillna(0) >= 1).sum())
    single = _single_price_days(df)
    if single.empty:
        allp = pd.to_numeric(df["price"], errors="coerce").dropna()
        out["lastListedPrice"] = float(allp.iloc[-1]) if len(allp) else None
        out["reason"] = "no-price-data" if allp.empty else "too-few-price-levels"
        return out
    out["lastListedPrice"] = float(single.iloc[-1])
    levels = _cluster_levels(single)
    out["levels"] = levels
    qual = [lv for lv in levels if lv["days"] >= PRICE_LEVEL_MIN_DAYS]
    out["qualifying"] = qual
    if len(qual) < PRICE_LEVELS_REQUIRED:
        out["reason"] = "too-few-price-levels"
        return out
    lo, hi = min(q["price"] for q in qual), max(q["price"] for q in qual)
    out["rangeLo"], out["rangeHi"] = lo, hi
    if hi <= 0 or (hi - lo) / hi < PRICE_LEVEL_MIN_SPREAD:
        out["reason"] = "levels-too-close"
        return out
    out["usable"] = True
    out["reason"] = "usable"
    return out


# ── Extrapolation beyond the prices actually sold at ─────────────────────────
# A business running a Black Friday at half price is a normal thing to plan for, so the
# forecast extrapolates well past the observed prices. What it will not do is pretend
# that extrapolation is as certain as interpolation. Three guards instead of a clamp:
#
#   PRICE_EXTRAP_FLOOR / CEIL   hard limits on the price the model is evaluated at: 80%
#                               below the cheapest well-evidenced price, or double the
#                               dearest. A $5 typo on a $50 product is not a plan.
#   MAX_PRICE_LIFT / MIN_PRICE_RATIO
#                               price alone can at most multiply a day's demand by 5, or
#                               cut it to a fifth. A noisy slope from two price levels
#                               must not become a tenfold order.
#   extrapolation_uncertainty   the further outside the observed range, the less the
#                               size of the price effect is known. At one observed spread
#                               outside, the uncertainty is half the effect itself; at two
#                               or more, it is the whole effect. That uncertainty widens
#                               the forecast band and the safety stock, so an order for a
#                               deep discount leans toward covering the upside.
PRICE_EXTRAP_FLOOR = 0.20
PRICE_EXTRAP_CEIL = 2.00
MAX_PRICE_LIFT = 5.0
# Past the known range the response continues as an elasticity, capped at this size:
# beyond it a 10% price change would move demand by more than 40%, which is fitting noise.
PRICE_MAX_ELASTICITY = 4.0
MIN_PRICE_RATIO = 0.20


def price_bounds(evidence: dict, df: pd.DataFrame = None, current_price=None):
    """Hard limits on the price a forecast is evaluated at. None when price is not
    usable at all. Always wide enough to include today's price, which has been sold at."""
    if not evidence or not evidence.get("usable"):
        return None
    lo, hi = float(evidence["rangeLo"]), float(evidence["rangeHi"])
    b_lo, b_hi = lo * PRICE_EXTRAP_FLOOR, hi * PRICE_EXTRAP_CEIL
    if current_price is not None and current_price > 0:
        b_lo, b_hi = min(b_lo, float(current_price)), max(b_hi, float(current_price))
    return round(b_lo, 2), round(b_hi, 2)


def extrapolation_distance(price, lo, hi) -> float:
    """How far outside the observed range a price is, in units of that range's width.
    0 inside it. $20 against observed $40–$50 is 2.0: twice the spread below."""
    if price is None or lo is None or hi is None:
        return 0.0
    p, lo, hi = float(price), float(lo), float(hi)
    width = max(hi - lo, 0.05 * hi, 1e-9)
    if p < lo:
        return (lo - p) / width
    if p > hi:
        return (p - hi) / width
    return 0.0


# How unsure a price effect is, as a fraction of the effect. Inside the prices a product
# has sold at, the response is measured but still an estimate (PRICE_EFFECT_UNC). Borrowed
# from similar products it is less certain (BORROWED_EFFECT_UNC). Past the known prices
# it grows with the distance (EXTRAP_UNC_SLOPE per range-width), up to 100%.
# 0.25, not the 0.15 it was: on 256 simulated products with known responses, the measured
# response was within 0.15 of the truth only 6 times in 10, not the 8 in 10 a margin is
# meant to cover; 0.21 to 0.35 was needed. A margin sized from each product's own readings
# was tried and didn't track its real error (their scatter is mostly season and traffic,
# not sampling noise), so it is one honest figure for all.
PRICE_EFFECT_UNC = 0.25
BORROWED_EFFECT_UNC = 0.35
# ...and from the whole store, when too few in its category have a price history, less
# certain again: a store's typical response is a looser match than its category's.
STORE_BORROWED_EFFECT_UNC = 0.5
EXTRAP_UNC_SLOPE = 0.4


def extrapolation_uncertainty(x: float, floor: float = PRICE_EFFECT_UNC) -> float:
    """Standard deviation of a price effect, as a fraction of the effect itself."""
    return float(min(1.0, float(floor) + EXTRAP_UNC_SLOPE * max(0.0, x)))


def cap_price_ratio(ratio):
    return float(min(max(float(ratio), MIN_PRICE_RATIO), MAX_PRICE_LIFT))


def scheduled_prices(days, events, base_price, *, bounds=None, promotions=True):
    """The price each day is expected to sell at, from logged events. One rule set for
    every engine, so the same events can never mean different things on different routes.

      1. Shelf price: today's price, then permanent changes in date order, then temporary
         changes on top for their windows. Order of entry doesn't matter.
      2. Promotions are discounts off that shelf price. When several overlap on a day the
         DEEPEST one applies. They don't add up: a shopper takes one deal, so "buy 2 get
         1 free" plus "25% off a single" is a 33% week, not 50%.
      3. The result is held inside the hard limits (bounds).

    Returns (prices, promo_pct): two lists aligned with `days`.
    """
    keys = [str(d)[:10] for d in days]
    try:
        base = float(base_price)
    except (TypeError, ValueError):
        base = 0.0
    price = [base] * len(keys)
    evs = [e for e in (events or []) if e.get("date")]
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    for t in ("price_change_permanent", "price_change_temporary"):
        for ev in sorted((e for e in evs if e.get("type") == t), key=lambda e: str(e["date"])[:10]):
            np_ = num(ev.get("new_price"))
            if np_ is None:
                continue
            start = str(ev["date"])[:10]
            end = str(ev.get("end_date") or start)[:10]
            for i, k in enumerate(keys):
                if (k >= start) if t == "price_change_permanent" else (start <= k <= end):
                    price[i] = np_
    disc = [0.0] * len(keys)
    if promotions:
        for ev in evs:
            if ev.get("type") != "promotion":
                continue
            pct = num(ev.get("discount_pct")) or 0.0
            if pct <= 0:
                continue
            start = str(ev["date"])[:10]
            end = str(ev.get("end_date") or start)[:10]
            for i, k in enumerate(keys):
                if start <= k <= end and pct > disc[i]:
                    disc[i] = min(pct, 100.0)
    out = [round(p * (1 - d / 100.0), 2) for p, d in zip(price, disc)]
    if bounds is not None:
        lo, hi = float(bounds[0]), float(bounds[1])
        out = [min(max(p, lo), hi) for p in out]
    return out, disc


def taper_beyond_edge(y_edge, y_raw, y_ref):
    """Ease a price response toward the limits instead of stopping at them.

    y_ref   demand at today's price.
    y_edge  demand at the nearest edge of the prices this product has sold at.
    y_raw   the percentage response continued past that edge, untapered.

    Up to the edge nothing changes: it is the product's own history. Past it, the extra
    response (measured in log terms, i.e. as a multiplier) is squeezed by tanh so it
    approaches the room left before MAX_PRICE_LIFT on a cut, or MIN_PRICE_RATIO on a rise.
    Close to the edge tanh is almost a straight line, so a small step past the known
    prices keeps nearly its full effect; far out, each further step adds less, and the
    limit is approached without ever being hit. Works on arrays or scalars.
    """
    y_edge = np.asarray(y_edge, dtype=float)
    y_raw = np.asarray(y_raw, dtype=float)
    y_ref = np.asarray(y_ref, dtype=float)
    ok = (y_edge > 1e-12) & (y_raw > 1e-12) & (y_ref > 1e-12)
    safe = lambda a: np.where(ok, a, 1.0)
    le = np.log(safe(y_edge) / safe(y_ref))                   # lift already reached at the edge
    lb = np.log(safe(y_raw) / safe(y_edge))                   # extra lift past the edge
    room_up = np.maximum(np.log(MAX_PRICE_LIFT) - le, 1e-9)
    room_dn = np.maximum(le - np.log(MIN_PRICE_RATIO), 1e-9)
    lb_eff = np.where(lb >= 0, room_up * np.tanh(lb / room_up), -room_dn * np.tanh(-lb / room_dn))
    out = safe(y_edge) * np.exp(lb_eff)
    return np.where(ok, out, y_raw)


def clamp_price(p, bounds):
    if bounds is None or p is None:
        return p
    return min(max(float(p), bounds[0]), bounds[1])


def analyse_price(df: pd.DataFrame):
    """(price_varied, price_well_sampled, last_price, price_tiers, warn_level, warn_msg).

    Built on price_evidence: tiers are single-price days only, and last_price is the last
    price a day actually sold at on its own — never a blend, never a scheduled event."""
    if "price" not in df.columns:
        return False, False, None, [], "no_column", (
            "No price data was uploaded for this SKU, so price changes can't affect the forecast. "
            "Add a 'Price' column to your Excel sheet to use price.")
    ev = price_evidence(df)
    last_price = ev["lastListedPrice"]
    if last_price is None:
        return False, False, None, [], "no_column", "Price column is present but contains no valid values."

    tiers = ev["levels"]
    price_varied = len(tiers) > 1
    if not price_varied:
        return False, False, last_price, tiers, "never_changed", None

    well = all(t["quality"] == "reliable" for t in tiers)
    if not well:
        nr = [t for t in tiers if t["quality"] != "reliable"]
        desc = ", ".join(f"${t['price']:.2f} ({t['days']} days, {t['quality']})" for t in nr)
        return True, False, last_price, tiers, "thin_data", (
            f"Some prices don't have enough days yet ({desc}). A price needs "
            f"{PRICE_LEVEL_MIN_DAYS}+ days to count and 90+ to be reliable.")
    return True, True, last_price, tiers, None, None


def compute_safe_price_range(fit_df, price_tiers, p_min, p_max, residual_std, price_coef=None):
    """Statistically-grounded safe extrapolation range beyond observed prices.
    Model-agnostic: takes the backtest residual sigma (and, when available, an
    exogenous price coefficient); no model object required."""
    try:
        if not price_tiers or p_min is None or p_max is None:
            return None, None
        rng = p_max - p_min
        if rng < 0:
            return p_min, p_max
        sigma = float(residual_std)
        price_std = float(fit_df["price"].std()) if "price" in fit_df.columns else 0.0
        n = len(fit_df)
        if price_coef is not None and abs(price_coef) > 1e-9:
            stat = sigma / abs(price_coef)
        elif price_std > 0 and sigma > 0:
            stat = price_std * np.sqrt(n)
        else:
            stat = 0.0
        mean_price = float(fit_df["price"].mean()) if "price" in fit_df.columns else p_min
        max_ext = min(0.25 * rng if rng > 0 else 0.10 * mean_price, 0.10 * mean_price)
        lo_days = next((t["days"] for t in price_tiers if t["price"] == p_min), 30)
        hi_days = next((t["days"] for t in price_tiers if t["price"] == p_max), 30)
        ext_lo = max_ext * min(lo_days / MIN_DAYS_PRICE_RELIABLE, 1.0)
        ext_hi = max_ext * min(hi_days / MIN_DAYS_PRICE_RELIABLE, 1.0)
        if stat > 0:
            ext_lo = min(ext_lo, stat); ext_hi = min(ext_hi, stat)
        return round(max(0.0, p_min - ext_lo), 2), round(p_max + ext_hi, 2)
    except Exception as e:
        print(f"[safe_price_range] {e}")
        return p_min, p_max

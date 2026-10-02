"""
Price and promotion effects for the routes that can't model them.

Prophet takes price and `on_promotion` as regressors and works out their effect while it
fits. Croston, the pooled model and the moving average can't — they see dates and
quantities only. Excluding promo days (see `promos.py`) stops a Black Friday from raising
the February baseline, but it leaves a worse hole: a promotion you have PLANNED for next
week produces no lift in the forecast at all, so the tool never tells you to stock up for
your own campaign.

The fix is the standard decomposition for methods without covariates:

    forecast = clean baseline  ×  promo multiplier  ×  price multiplier

The baseline is fitted on ordinary days. The multipliers are measured separately from
history and applied only to the future days they actually apply to. Same information
Prophet uses, arrived at explicitly instead of implicitly.

Everything here is built to REFUSE rather than guess, because a fabricated multiplier is
worse than none — it looks like knowledge and it moves real orders:

  * a lift measured from two promo days is mostly noise, so estimates are shrunk toward
    "no effect" in proportion to how little evidence supports them;
  * a product with no promo history of its own borrows its cohort's average lift rather
    than inventing one, and says that's what it did;
  * elasticity is only computed when prices actually varied enough to reveal it, and a
    positive own-price elasticity (demand rising with price) is treated as noise rather
    than as a licence to forecast more when you raise prices.
"""

from __future__ import annotations

import math

from forecast_engine import MAX_PRICE_LIFT, MIN_PRICE_RATIO, scheduled_prices, taper_beyond_edge

import pandas as _pd

# Below this many promotional days, a product's own lift is mostly noise.
MIN_PROMO_DAYS = 3

# Shrinkage constant. With k=5, a product with 5 promo days gets half its measured lift
# and half "no effect"; with 20 it keeps ~80%. Stops a single lucky week from doubling
# an order.
PROMO_SHRINK_K = 5.0

# A promotion doubling or tripling demand is normal. Ten times is a data problem.
MAX_PROMO_LIFT = 5.0
MIN_PROMO_LIFT = 0.5           # a "promo" that halves sales is a mislabelled row

# Price elasticity needs prices that actually moved.
MIN_PRICE_POINTS = 30
MIN_PRICE_SPREAD = 0.05        # 5% between cheapest and dearest
ELASTICITY_SHRINK_K = 60.0     # days
MAX_ELASTICITY = 4.0           # |e|; beyond this it's fitting noise


def _mean(xs):
    xs = [float(x) for x in xs]
    return sum(xs) / len(xs) if xs else 0.0


def promo_lift(df, *, cohort_lift=None, value_col="y", promo_col="on_promotion"):
    """How much this product sells on promotion, relative to normal.

    Returns {multiplier, basis, promoDays, raw, ...}. `multiplier` is always safe to use:
    1.0 means "no evidence of an effect", never "no effect exists".
    """
    out = {"multiplier": 1.0, "basis": "none", "promoDays": 0, "normalDays": 0,
           "raw": None, "shrunk": None, "capped": False}
    if df is None or len(df) == 0 or promo_col not in df.columns:
        out["basis"] = "no-promotion-data"
        return _with_cohort(out, cohort_lift)

    m = df[promo_col].fillna(0).astype(float) >= 1
    promo_vals = df.loc[m, value_col].fillna(0).tolist()
    normal_vals = df.loc[~m, value_col].fillna(0).tolist()
    out["promoDays"], out["normalDays"] = len(promo_vals), len(normal_vals)

    if len(promo_vals) < MIN_PROMO_DAYS or not normal_vals:
        out["basis"] = "too-few-promo-days"
        return _with_cohort(out, cohort_lift)

    base = _mean(normal_vals)
    if base <= 0:
        # Everything sold happened on promotion. There's no "normal" to compare against,
        # so a ratio would be division by nothing.
        out["basis"] = "no-normal-baseline"
        return _with_cohort(out, cohort_lift)

    raw = _mean(promo_vals) / base
    out["raw"] = round(float(raw), 3)

    # Shrink toward 1.0 by how much evidence there is. Ten promo days keeps two-thirds of
    # the measured effect; three keeps under half.
    n = len(promo_vals)
    w = n / (n + PROMO_SHRINK_K)
    shrunk = 1.0 + (raw - 1.0) * w
    out["shrunk"] = round(shrunk, 3)

    capped = min(MAX_PROMO_LIFT, max(MIN_PROMO_LIFT, shrunk))
    out["capped"] = bool(abs(capped - shrunk) > 1e-9)
    out["multiplier"] = round(capped, 3)
    out["basis"] = "own-history"
    out["weight"] = round(float(w), 3)
    return out


def _with_cohort(out, cohort_lift):
    """Fall back to what similar products do, rather than to nothing.

    A brand-new product has no promo history by definition — and it's exactly the kind of
    product that launches with a discount. Borrowing the cohort's average is a real
    estimate; assuming no effect is a silent claim that promotions don't work.
    """
    if cohort_lift and cohort_lift > 1.0:
        out["multiplier"] = round(min(MAX_PROMO_LIFT, float(cohort_lift)), 3)
        out["basis"] = "cohort"
        out["cohortLift"] = round(float(cohort_lift), 3)
    return out


def cohort_promo_lift(frames, *, value_col="y", promo_col="on_promotion"):
    """Average promo lift across a group of products, for those with none of their own.

    Weighted by how much promo evidence each product has, so a product with 40 promo days
    counts for more than one with 4.
    """
    num = den = 0.0
    used = 0
    for df in frames or []:
        r = promo_lift(df, value_col=value_col, promo_col=promo_col)
        if r["basis"] != "own-history" or r["raw"] is None:
            continue
        w = r.get("weight", 0.5) * max(1, r["promoDays"])
        num += r["multiplier"] * w
        den += w
        used += 1
    if not den or used < 2:
        return None
    return round(num / den, 3)


# Days at the less common price needed, within months that saw both, before a
# same-season comparison replaces the whole-history one.
MIN_WITHIN_MONTH_DAYS = 30


def _within_month_elasticity(df, value_col="y", price_col="price"):
    """Elasticity from price levels compared inside the same calendar month (month fixed
    effects, zero days included, weighted by days). None when too few months were sold at
    more than one price."""
    if "ds" not in getattr(df, "columns", []):
        return None
    try:
        d = _pd.DataFrame({"m": _pd.to_datetime(df["ds"]).dt.month,
                           "p": _pd.to_numeric(df[price_col], errors="coerce").round(2),
                           "q": _pd.to_numeric(df[value_col], errors="coerce")}).dropna()
    except Exception:                                   # noqa: BLE001
        return None
    d = d[(d["p"] > 0) & (d["q"] >= 0)]
    if d.empty:
        return None
    cells = d.groupby(["m", "p"])["q"].agg(["count", "mean"]).reset_index()
    cells = cells[(cells["count"] >= 3) & (cells["mean"] > 0)]
    num = den = 0.0
    months = 0
    minority = 0
    for _, g in cells.groupby("m"):
        if len(g) < 2:
            continue
        w = g["count"].to_numpy(float)
        x = [math.log(v) for v in g["p"]]
        y = [math.log(v) for v in g["mean"]]
        W = w.sum()
        mx = sum(wi * xi for wi, xi in zip(w, x)) / W
        my = sum(wi * yi for wi, yi in zip(w, y)) / W
        num += sum(wi * (xi - mx) * (yi - my) for wi, xi, yi in zip(w, x, y))
        den += sum(wi * (xi - mx) ** 2 for wi, xi in zip(w, x))
        months += 1
        minority += int(w.sum() - w.max())
    if months == 0 or den <= 0 or minority < MIN_WITHIN_MONTH_DAYS:
        return None
    return {"elasticity": num / den, "months": months}


def price_elasticity(df, *, value_col="y", price_col="price"):
    """How demand responds to price, as a log-log elasticity.

    -1.5 means a 10% price rise costs about 15% of unit demand. Returns 0.0 whenever the
    data can't support an estimate — a product whose price never moved simply hasn't told
    us anything, and pretending otherwise would let a price change swing an order on no
    evidence at all.
    """
    out = {"elasticity": 0.0, "basis": "none", "points": 0, "raw": None,
           "spread": None, "capped": False}
    if df is None or price_col not in df.columns or value_col not in df.columns:
        out["basis"] = "no-price-data"
        return out

    # A day that sold at two different prices is evidence of neither: its price is a
    # blend no customer paid. Measure the response from single-price days only.
    if "price_mixed" in df.columns:
        try:
            df = df[df["price_mixed"].fillna(0).astype(float) < 1]
        except (TypeError, ValueError, AttributeError):
            pass
    # MEASURED FROM EACH PRICE'S AVERAGE DAILY SALES, ZERO DAYS INCLUDED.
    # This used to regress units on price over sale days only. For a slow seller that
    # misses most of the response: at a higher price it mostly sells on FEWER days, not
    # fewer units per sale, and the days it didn't sell were thrown away. In a simulated
    # slow seller with a true elasticity of -1.2, a 16% rise moved the forecast by 3%.
    # Averaging every day at each price, zeros included, sees the whole effect.
    rows = []
    for p, q in zip(df[price_col].tolist(), df[value_col].tolist()):
        try:
            p, q = float(p), float(q)
        except (TypeError, ValueError):
            continue
        if p > 0 and q == q and q >= 0:
            rows.append((round(p, 2), q))
    sale_days = sum(1 for _, q in rows if q > 0)
    out["points"] = sale_days
    if sale_days < MIN_PRICE_POINTS:
        out["basis"] = "too-few-points"
        return out
    levels = {}
    for p, q in rows:
        n_, s_ = levels.get(p, (0, 0.0))
        levels[p] = (n_ + 1, s_ + q)
    lv = [(p, n_, s_ / n_) for p, (n_, s_) in levels.items() if n_ >= 3 and s_ > 0]
    if len(lv) < 2:
        out["basis"] = "price-never-varied"
        return out
    prices = [p for p, _, _ in lv]
    lo, hi = min(prices), max(prices)
    spread = (hi - lo) / hi if hi else 0.0
    out["spread"] = round(spread, 4)
    if spread < MIN_PRICE_SPREAD:
        out["basis"] = "price-never-varied"
        return out
    wts = [n_ for _, n_, _ in lv]
    lx = [math.log(p) for p, _, _ in lv]
    ly = [math.log(m) for _, _, m in lv]
    W = float(sum(wts))
    mx = sum(w_ * x for w_, x in zip(wts, lx)) / W
    my = sum(w_ * y for w_, y in zip(wts, ly)) / W
    var = sum(w_ * (x - mx) ** 2 for w_, x in zip(wts, lx))
    if var <= 0:
        out["basis"] = "price-never-varied"
        return out
    raw = sum(w_ * (x - mx) * (y - my) for w_, x, y in zip(wts, lx, ly)) / var
    # SAME SEASON FIRST. Comparing each price's average across the whole history confuses
    # price with season: a discount run in the quiet months looks like it barely sold, and
    # the response comes out far too weak (a true -1.4 read as -0.7 in simulation). When
    # the same calendar months were sold at more than one price, compare within those
    # months only. Otherwise the whole-history comparison above stands.
    within = _within_month_elasticity(df, value_col, price_col)
    if within is not None:
        out["acrossMonths"] = round(float(raw), 3)
        raw = within["elasticity"]
        out["basisDetail"] = "same-month comparison"
        out["monthsCompared"] = within["months"]
    out["raw"] = round(float(raw), 3)
    out["levels"] = len(lv)

    if raw > 0:
        # Demand rising with price is almost always confounding — a premium line, or
        # promotions that coincided with restocks. Treating it as real would tell you to
        # forecast MORE when you raise prices, which is not a mistake worth making.
        out["basis"] = "positive-elasticity-ignored"
        return out

    pairs = rows
    n = sale_days
    w = n / (n + ELASTICITY_SHRINK_K)
    shrunk = raw * w
    capped = max(-MAX_ELASTICITY, shrunk)
    out["capped"] = bool(abs(capped - shrunk) > 1e-9)
    out["elasticity"] = round(float(capped), 3)
    out["basis"] = "measured"
    out["weight"] = round(float(w), 3)
    return out


# Similar products needed before their price response is borrowed.
MIN_BORROW_PEERS = 2

_peer_cache: dict = {}


def frame_key(df):
    """A cheap fingerprint of a sales frame, for caching per-frame measurements."""
    try:
        return (len(df), str(df["ds"].min())[:10], str(df["ds"].max())[:10], round(float(df["y"].sum()), 3),
                round(_col_sum(df, "price"), 3))
    except Exception:                                   # noqa: BLE001
        return None


def _col_sum(df, col):
    if col not in getattr(df, "columns", []):
        return 0.0
    try:
        return float(df[col].fillna(0).astype(float).sum())
    except (TypeError, ValueError):
        return 0.0


def _last_price(df):
    v = _pd.to_numeric(df["price"], errors="coerce").dropna()
    v = v[v > 0]
    return float(v.iloc[-1]) if len(v) else None


def peer_price_response(df):
    """One peer's own price response, only when its history clears the price gate.
    Returns {elasticity, lo, hi, current, days} or None. Cached per frame."""
    from forecast_engine import price_evidence
    key = frame_key(df)
    if key is not None and key in _peer_cache:
        return _peer_cache[key]
    out = None
    try:
        if df is not None and "price" in df.columns and len(df):
            ev = price_evidence(df)
            if ev.get("usable"):
                el = price_elasticity(df)
                cur = _last_price(df)
                if el.get("basis") == "measured" and el.get("elasticity", 0) < 0 and cur:
                    out = {"elasticity": float(el["elasticity"]), "lo": float(ev["rangeLo"]),
                           "hi": float(ev["rangeHi"]), "current": float(cur),
                           "days": int(el.get("points") or 0)}
    except Exception:                                   # noqa: BLE001
        out = None
    if key is not None:
        if len(_peer_cache) > 5000:
            _peer_cache.clear()
        _peer_cache[key] = out
    return out


def pooled_elasticity(frames):
    """Price response borrowed from similar products, for a product whose own history
    can't show one yet (a new product, or one that has only ever sold at one price).

    Only peers that clear the same 30-day gate count, and at least MIN_BORROW_PEERS of
    them must. The typical (median) response is used, and the range of prices those peers
    have actually sold at, relative to their current price, becomes the "known" range for
    this product: inside it the borrowed response applies, past it the same taper eases
    it off. Returns None when there's nothing to borrow."""
    got = [r for r in (peer_price_response(f) for f in (frames or []) if hasattr(f, "columns")) if r]
    if len(got) < MIN_BORROW_PEERS:
        return None
    els = sorted(r["elasticity"] for r in got)
    med = els[len(els) // 2] if len(els) % 2 else 0.5 * (els[len(els) // 2 - 1] + els[len(els) // 2])
    rel_lo = sorted(r["lo"] / r["current"] for r in got)
    rel_hi = sorted(r["hi"] / r["current"] for r in got)
    mid = len(got) // 2
    return {"elasticity": round(med, 3), "peers": len(got), "relLo": min(rel_lo[mid], 1.0),
            "relHi": max(rel_hi[mid], 1.0), "spread": round(els[-1] - els[0], 3), "basis": "borrowed"}


def price_multiplier(elasticity, base_price, new_price):
    """(new/base) ** elasticity — the standard constant-elasticity response."""
    try:
        b, n = float(base_price), float(new_price)
    except (TypeError, ValueError):
        return 1.0
    if b <= 0 or n <= 0 or not elasticity:
        return 1.0
    return round((n / b) ** float(elasticity), 4)


def future_price_path(future_dates, events, base_price, *, price_override=None, bounds=None):
    """The price each future day is expected to sell at: {date: price}.

    Built by forecast_engine.scheduled_prices, the same rules the Prophet route uses:
    price changes set the shelf price, the deepest overlapping promotion discounts it,
    and the hard limits hold it. A price change entered once means the same thing
    whichever engine is behind the product.
    """
    if price_override is not None:
        try:
            return {str(d)[:10]: float(price_override) for d in future_dates}
        except (TypeError, ValueError):
            pass
    try:
        base = float(base_price)
    except (TypeError, ValueError):
        return {}
    if base <= 0:
        return {}
    prices, _ = scheduled_prices(future_dates, events, base, bounds=bounds)
    return {str(d)[:10]: p for d, p in zip(future_dates, prices)}


def price_response(elasticity, base_price, new_price, known_range=None):
    """Demand multiplier for selling at `new_price` instead of `base_price`.

    Percentage response throughout (constant elasticity). Past the prices this product
    has sold at, the extra response is eased toward the limits (taper_beyond_edge), then
    held inside them as a last safety net.
    """
    m = price_multiplier(elasticity, base_price, new_price)
    try:
        p = float(new_price)
    except (TypeError, ValueError):
        return 1.0
    if known_range and elasticity:
        lo, hi = float(known_range[0]), float(known_range[1])
        edge = lo if p < lo else hi if p > hi else None
        if edge is not None:
            m_edge = price_multiplier(elasticity, base_price, edge)
            m = float(taper_beyond_edge(m_edge, m, 1.0))
    return min(max(m, MIN_PRICE_RATIO), MAX_PRICE_LIFT)


def future_multipliers(future_dates, events, lift: float, *,
                       elasticity: float = 0.0, base_price=None, planned_price=None,
                       bounds=None, known_range=None):
    """One multiplier per future day.

    Promo lift where a promotion is scheduled, price response wherever the expected price
    differs from what the product normally sells at, 1.0 everywhere else. The two combine
    multiplicatively, so a discounted promotion gets both the campaign lift and the price
    response — which is what actually happens.
    """
    promo_days = set()
    for ev in events or []:
        if ev.get("type") != "promotion":
            continue
        start, end = str(ev.get("date", ""))[:10], str(ev.get("end_date") or ev.get("date", ""))[:10]
        if not start:
            continue
        for d in future_dates:
            ds = str(d)[:10]
            if start <= ds <= (end or start):
                promo_days.add(ds)

    path = future_price_path(future_dates, events, base_price, price_override=planned_price,
                             bounds=bounds)
    out = []
    for d in future_dates:
        ds = str(d)[:10]
        pm = price_response(elasticity, base_price, path.get(ds), known_range) if path else 1.0
        out.append(round((lift if ds in promo_days else 1.0) * pm, 4))
    return out


def apply(future_df, multipliers, *, cols=("yhat", "yhat_lower", "yhat_upper")):
    """Scale a forecast frame in place-ish. Interval bounds scale with the point estimate,
    which is right for a multiplicative effect: a promo doubles the uncertainty in units
    as well as the expectation."""
    if future_df is None or len(future_df) == 0 or not multipliers:
        return future_df
    out = future_df.copy()
    n = min(len(out), len(multipliers))
    for c in cols:
        if c in out.columns:
            vals = out[c].tolist()
            for i in range(n):
                try:
                    vals[i] = float(vals[i]) * float(multipliers[i])
                except (TypeError, ValueError):
                    pass
            out[c] = vals
    return out


def explain(lift_report: dict, elast_report: dict | None = None) -> str:
    bits = []
    lr = lift_report or {}
    if lr.get("basis") == "own-history" and abs(lr.get("multiplier", 1) - 1) > 0.05:
        bits.append(f"Promotions lift this product about {lr['multiplier']:.2f}× "
                    f"(measured over {lr['promoDays']} promo days), and that lift is "
                    f"applied to any promotion you've scheduled.")
    elif lr.get("basis") == "cohort":
        bits.append(f"No promo history of its own, so it uses the {lr['cohortLift']:.2f}× "
                    f"lift similar products see. It'll switch to its own once it has "
                    f"{MIN_PROMO_DAYS} promotional days.")
    elif lr.get("basis") == "too-few-promo-days" and lr.get("promoDays"):
        bits.append(f"Only {lr['promoDays']} promotional days on record, too few to "
                    f"measure a lift, so scheduled promotions don't raise the forecast yet.")

    er = elast_report or {}
    if er.get("basis") == "measured":
        e = er["elasticity"]
        bits.append(f"Price elasticity is about {e:.2f}: a 10% price rise would cost "
                    f"roughly {abs(e) * 10:.0f}% of unit demand.")
    elif er.get("basis") == "price-never-varied":
        bits.append("The price hasn't moved enough to measure how demand responds to it.")
    return " ".join(bits)


# ═════════════════════════════════════════════════════════════════════════════
#  PRICE MEASURED TWO WAYS, AND CROSS-CHECKED
#
#  1. SAME MONTHS (price_elasticity above): each price's average daily sales compared
#     within the same calendar months, across years. Strong when the product has sold at
#     both prices in the same months; blind when it hasn't.
#  2. BEFORE / AFTER (before_after_elasticity below): at every price change, the weeks
#     just before against the weeks just after, with other products whose price didn't
#     change over those same weeks taken out as the yardstick for season and traffic.
#     Strong at a change that lands on a season turn, and for products that only ever
#     ran short promotions; noisier on a single small change.
#
#  price_reading() uses both. Tested on 144 simulated products with known responses:
#  where both read, they must agree (within CROSS_CHECK_TOL) and the average is used;
#  where they clearly disagree, price isn't used; where only one can read, it's used.
#  Against the same-months method alone: typical miss 0.48 -> 0.32, big misses 26 -> 18,
#  on 95 products instead of 110.
# ═════════════════════════════════════════════════════════════════════════════
BA_WINDOW = 91          # days compared on each side of a price change, at most
BA_MIN_SIDE = 7         # each side needs at least this many usable days
BA_MIN_STEP = 0.05      # a move under 5% isn't a price change here
BA_MIN_YARDSTICK = 3    # other products needed before they're the yardstick (category first)
BA_YARD_MIN_UNITS = 5   # a yardstick product needs this many units on each side
CROSS_CHECK_TOL = 0.75  # how far apart the two readings may be and still count as agreeing
BA_MAX_SE = 1.5         # a before/after reading less precise than this isn't a reading at all
                        # (a handful of sales around one change); tested: no loss above it

_ba_frame_cache: dict = {}


def _dated(f):
    """A peer frame as date-indexed y / price, cached per frame."""
    key = frame_key(f)
    got = _ba_frame_cache.get(key) if key is not None else None
    if got is not None:
        return got
    g = f.copy()
    g["ds"] = _pd.to_datetime(g["ds"])
    g = g.groupby("ds").agg(y=("y", "sum"), price=("price", "last")) if "price" in g.columns else \
        g.groupby("ds").agg(y=("y", "sum")).assign(price=float("nan"))
    if key is not None:
        if len(_ba_frame_cache) > 5000:
            _ba_frame_cache.clear()
        _ba_frame_cache[key] = g
    return g


def _weekday_profile(ds, y, ok):
    wd = ds.dt.weekday.to_numpy()
    yy = y.to_numpy(float)
    if ok.sum() < 28 or yy[ok].mean() <= 0:
        return [1.0] * 7
    mu = yy[ok].mean()
    idx = []
    for i in range(7):
        v = yy[ok & (wd == i)]
        if len(v) >= 4:
            n = float(v.sum())
            idx.append(1 + (float(v.mean()) / mu - 1) * n / (n + 20.0))
        else:
            idx.append(1.0)
    m = sum(idx) / 7.0
    return [x / m for x in idx]


def before_after_elasticity(df, others=None, prefer=None, holiday_mask=None, phi=2.0):
    """Price response from the product's own price changes, piece by piece (see the block
    comment above). `others`: other products' frames (ds, y, price) for the yardstick;
    `prefer`: those of them in the same category. Returns {elasticity, se, changes, lo, hi,
    detail} or None when no price change can be read."""
    import numpy as _np
    if df is None or "price" not in getattr(df, "columns", []) or len(df) < 2 * BA_MIN_SIDE:
        return None
    d = df.sort_values("ds").reset_index(drop=True)
    d["ds"] = _pd.to_datetime(d["ds"])
    y = d["y"].astype(float).to_numpy()
    price = _pd.to_numeric(d["price"], errors="coerce").to_numpy(float)
    usable = _np.isfinite(price) & (price > 0)
    if "price_mixed" in d.columns:
        usable &= _pd.to_numeric(d["price_mixed"], errors="coerce").fillna(0).to_numpy(float) < 1
    if holiday_mask is not None and len(holiday_mask) == len(d):
        usable &= ~_np.asarray(holiday_mask, bool)
    wdi = _np.array(_weekday_profile(d["ds"], d["y"].astype(float), usable))[d["ds"].dt.weekday.to_numpy()]
    segs, cur = [], None
    for i in range(len(d)):
        if not usable[i]:
            continue
        p = price[i]
        if cur and abs(math.log(p / cur[2])) < 0.01:
            cur = (cur[0], i, cur[2])
        else:
            if cur:
                segs.append(cur)
            cur = (i, i, p)
    if cur:
        segs.append(cur)
    oth = [_dated(f) for f in (others or []) if hasattr(f, "columns") and len(f)]
    pref_keys = {frame_key(f) for f in (prefer or []) if hasattr(f, "columns") and len(f)}
    pref = [_dated(f) for f in (prefer or []) if hasattr(f, "columns") and len(f)]
    rows = []
    for (a0, a1, pa), (b0, b1, pb) in zip(segs, segs[1:]):
        step = math.log(pb / pa)
        if abs(step) < BA_MIN_STEP:
            continue
        ia = [i for i in range(a0, a1 + 1) if usable[i]][-BA_WINDOW:]
        ib = [i for i in range(b0, b1 + 1) if usable[i]][:BA_WINDOW]
        if len(ia) < BA_MIN_SIDE or len(ib) < BA_MIN_SIDE:
            continue
        ya, yb = y[ia].sum(), y[ib].sum()
        if ya <= 0:
            continue
        exp_b = ya / wdi[ia].sum() * wdi[ib].sum()
        lp = math.log((yb + 0.5) / (exp_b + 0.5))
        lc, scope = 0.0, None
        lo_d, hi_d = d["ds"].iloc[ia[0]], d["ds"].iloc[ib[-1]]

        da, db = d["ds"].iloc[ia], d["ds"].iloc[ib]

        def ratio(g):
            """This other product's own after/before change in daily units, when its price
            held steady over these weeks and it was on sale the whole time; else None."""
            if g.index[0] > lo_d or g.index[-1] < hi_d:
                return None
            p_ = g["price"][(g.index >= lo_d) & (g.index <= hi_d)]
            p_ = p_[p_ > 0]
            if not len(p_) or float(p_.max()) / float(p_.min()) >= 1.01:
                return None
            ca = float(g["y"].reindex(da).fillna(0).sum())
            cb = float(g["y"].reindex(db).fillna(0).sum())
            if ca < BA_YARD_MIN_UNITS or cb < BA_YARD_MIN_UNITS:
                return None
            return math.log((cb / len(ib)) / (ca / len(ia)))

        # The yardstick is the MEDIAN of the other products' own changes, so one product
        # that launched, died or had its own event over these weeks can't swing it.
        scope = "category"
        rs = [r for r in (ratio(g) for g in pref) if r is not None]
        if len(rs) < BA_MIN_YARDSTICK:
            scope = "store"
            rs = [r for r in (ratio(g) for g in oth) if r is not None]
        if len(rs) >= BA_MIN_YARDSTICK:
            lc = float(_np.median(rs))
        else:
            lc, scope = 0.0, None
        var = phi * (1.0 / max(ya, 1.0) + 1.0 / max(exp_b, 1.0))
        rows.append({"from": round(float(pa), 2), "to": round(float(pb), 2),
                     "at": d["ds"].iloc[b0].strftime("%Y-%m-%d"), "days": [len(ia), len(ib)],
                     "elasticity": (lp - lc) / step, "w": step ** 2 / var, "yardstick": scope})
    if not rows:
        return None
    w = sum(r["w"] for r in rows)
    el = sum(r["w"] * r["elasticity"] for r in rows) / w
    prices = [r["from"] for r in rows] + [r["to"] for r in rows]
    return {"elasticity": float(el), "se": float(1.0 / math.sqrt(w)), "changes": len(rows),
            "lo": float(min(prices)), "hi": float(max(prices)), "detail": rows}


def price_reading(df, *, gate_ok, others=None, prefer=None, holiday_mask=None):
    """This product's price response from its own sales, measured two ways and
    cross-checked (see the block comment above). Returns a dict:
      elasticity  the response to use, or None
      basis       "both" | "same-months" | "before-after" | the reason it can't be used
      sameMonths / beforeAfter  each reading (None when it couldn't read)
    """
    e0 = price_elasticity(df)
    same = (float(e0["elasticity"]) if gate_ok and e0.get("basis") == "measured"
            and float(e0.get("elasticity") or 0) < 0 else None)
    ba = before_after_elasticity(df, others=others, prefer=prefer, holiday_mask=holiday_mask)
    if ba is not None and (ba["se"] > BA_MAX_SE or ba["elasticity"] < -MAX_ELASTICITY):
        # Too few sales around the changes to read, or a reading past anything plausible
        # (a price change landing on a season turn the yardstick doesn't share).
        ba = {**ba, "tooNoisy": True}
    ba_el = (max(-MAX_ELASTICITY, float(ba["elasticity"]))
             if ba and not ba.get("tooNoisy") and ba["elasticity"] < 0 else None)
    out = {"sameMonths": same, "beforeAfter": ba_el, "beforeAfterDetail": ba, "sameMonthsDetail": e0,
           "points": e0.get("points"), "changes": (ba or {}).get("changes", 0)}
    if same is not None and ba_el is not None:
        if abs(same - ba_el) <= CROSS_CHECK_TOL:
            return {**out, "elasticity": round((same + ba_el) / 2.0, 3), "basis": "both"}
        return {**out, "elasticity": None, "basis": "readings-disagree"}
    if same is not None:
        return {**out, "elasticity": round(same, 3), "basis": "same-months"}
    if ba_el is not None:
        return {**out, "elasticity": round(ba_el, 3), "basis": "before-after"}
    reason = e0.get("basis") or "none"
    if ba and not ba.get("tooNoisy") and ba["elasticity"] >= 0 and reason not in ("positive-elasticity-ignored",):
        reason = "positive-elasticity-ignored" if gate_ok else reason
    if not gate_ok and reason == "measured":
        reason = "too-few-price-levels"
    return {**out, "elasticity": None, "basis": reason}

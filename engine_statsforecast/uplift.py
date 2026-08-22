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
    out["raw"] = round(raw, 3)

    # Shrink toward 1.0 by how much evidence there is. Ten promo days keeps two-thirds of
    # the measured effect; three keeps under half.
    n = len(promo_vals)
    w = n / (n + PROMO_SHRINK_K)
    shrunk = 1.0 + (raw - 1.0) * w
    out["shrunk"] = round(shrunk, 3)

    capped = min(MAX_PROMO_LIFT, max(MIN_PROMO_LIFT, shrunk))
    out["capped"] = abs(capped - shrunk) > 1e-9
    out["multiplier"] = round(capped, 3)
    out["basis"] = "own-history"
    out["weight"] = round(w, 3)
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

    pairs = []
    for p, q in zip(df[price_col].tolist(), df[value_col].tolist()):
        try:
            p, q = float(p), float(q)
        except (TypeError, ValueError):
            continue
        if p > 0 and q > 0:      # log of zero is undefined; zero-sale days carry no price signal
            pairs.append((p, q))
    out["points"] = len(pairs)
    if len(pairs) < MIN_PRICE_POINTS:
        out["basis"] = "too-few-points"
        return out

    prices = [p for p, _ in pairs]
    lo, hi = min(prices), max(prices)
    spread = (hi - lo) / hi if hi else 0.0
    out["spread"] = round(spread, 4)
    if spread < MIN_PRICE_SPREAD:
        # The price never really moved, so nothing in this data reveals elasticity.
        out["basis"] = "price-never-varied"
        return out

    lx = [math.log(p) for p, _ in pairs]
    ly = [math.log(q) for _, q in pairs]
    mx, my = _mean(lx), _mean(ly)
    var = sum((x - mx) ** 2 for x in lx)
    if var <= 0:
        out["basis"] = "price-never-varied"
        return out
    cov = sum((x - mx) * (y - my) for x, y in zip(lx, ly))
    raw = cov / var
    out["raw"] = round(raw, 3)

    if raw > 0:
        # Demand rising with price is almost always confounding — a premium line, or
        # promotions that coincided with restocks. Treating it as real would tell you to
        # forecast MORE when you raise prices, which is not a mistake worth making.
        out["basis"] = "positive-elasticity-ignored"
        return out

    n = len(pairs)
    w = n / (n + ELASTICITY_SHRINK_K)
    shrunk = raw * w
    capped = max(-MAX_ELASTICITY, shrunk)
    out["capped"] = abs(capped - shrunk) > 1e-9
    out["elasticity"] = round(capped, 3)
    out["basis"] = "measured"
    out["weight"] = round(w, 3)
    return out


def price_multiplier(elasticity, base_price, new_price):
    """(new/base) ** elasticity — the standard constant-elasticity response."""
    try:
        b, n = float(base_price), float(new_price)
    except (TypeError, ValueError):
        return 1.0
    if b <= 0 or n <= 0 or not elasticity:
        return 1.0
    return round((n / b) ** float(elasticity), 4)


def future_price_path(future_dates, events, base_price, *, price_override=None):
    """The price each future day is expected to sell at.

    Reads the SAME events Prophet reads — `price_change_permanent` applies from its date
    onward, `price_change_temporary` only within its window, and a promotion carrying a
    `discount_pct` discounts that window. Parity matters: a price change entered once
    should move every product's forecast, whichever engine happens to be behind it.
    """
    try:
        base = float(base_price)
    except (TypeError, ValueError):
        base = None
    if price_override is not None:
        try:
            return {str(d)[:10]: float(price_override) for d in future_dates}
        except (TypeError, ValueError):
            pass
    if base is None or base <= 0:
        return {}

    path = {str(d)[:10]: base for d in future_dates}
    for ev in sorted(events or [], key=lambda e: str(e.get("date", ""))):
        t = ev.get("type")
        start = str(ev.get("date", ""))[:10]
        if not start:
            continue
        end = str(ev.get("end_date") or start)[:10]
        if t == "price_change_permanent":
            try:
                np_ = float(ev.get("new_price"))
            except (TypeError, ValueError):
                continue
            for d in path:
                if d >= start:
                    path[d] = np_
        elif t == "price_change_temporary":
            try:
                np_ = float(ev.get("new_price"))
            except (TypeError, ValueError):
                continue
            for d in path:
                if start <= d <= end:
                    path[d] = np_
        elif t == "promotion" and ev.get("discount_pct"):
            try:
                pct = float(ev["discount_pct"])
            except (TypeError, ValueError):
                continue
            for d in path:
                if start <= d <= end:
                    path[d] = round(path[d] * (1 - pct / 100.0), 2)
    return path


def future_multipliers(future_dates, events, lift: float, *,
                       elasticity: float = 0.0, base_price=None, planned_price=None):
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

    path = future_price_path(future_dates, events, base_price, price_override=planned_price)
    out = []
    for d in future_dates:
        ds = str(d)[:10]
        pm = price_multiplier(elasticity, base_price, path.get(ds)) if path else 1.0
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
        bits.append(f"Only {lr['promoDays']} promotional days on record — too few to "
                    f"measure a lift, so scheduled promotions don't raise the forecast yet.")

    er = elast_report or {}
    if er.get("basis") == "measured":
        e = er["elasticity"]
        bits.append(f"Price elasticity is about {e:.2f}: a 10% price rise would cost "
                    f"roughly {abs(e) * 10:.0f}% of unit demand.")
    elif er.get("basis") == "price-never-varied":
        bits.append("The price hasn't moved enough to measure how demand responds to it.")
    return " ".join(bits)

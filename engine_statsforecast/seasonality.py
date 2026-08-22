"""
Seasonal awareness for the intermittent route.

Croston models demand as a rate — how big the sales are, and how far apart. That is the
right shape for a product selling on 8 days out of 90, and it is why it deliberately has
no seasonal curve: you can't place a monthly shape on eight observations.

But "this product's own history is too sparse to reveal a season" is not the same as
"this product has no season". A snow shovel selling three days a month sells them in
January. Forecast it flat and you under-order going into winter and over-order coming out
— which is precisely the moment inventory decisions matter most.

The fix keeps Croston's rate as the baseline and multiplies by a monthly index, taken from
the product's own history when it has enough, and from its peers when it doesn't. The
same trick as the price/promo uplift, and the same discipline: refuse when the evidence
isn't there, because a fabricated season moves stock in the wrong direction twice a year.

── The trap this module is mostly about ──────────────────────────────────────────────

A rate measured over a FULL year is already season-neutral: the highs and lows average
out, so multiplying by a monthly index is exactly right.

A rate measured over eight months is NOT. If those eight months were the busy ones, the
rate is already inflated, and multiplying by a peak-month index on top applies the season
twice. The pooled engine hits the same problem and solves it by de-seasonalising the level
before re-applying the curve. This does the same, keyed on which months the training
window actually covered — `coverage_factor()` is the whole point of the module.
"""

from __future__ import annotations

import datetime as _dt

# A month needs this many observed days before its index means anything.
MIN_DAYS_PER_MONTH = 8

# Months that must have some data before an own-history index is usable at all. Below
# this, whole seasons are unobserved and the "index" is really a shape of the gaps.
MIN_MONTHS_COVERED = 8

# A full seasonal cycle. Below this, an own index is measuring one pass through the year
# and can't tell a season from a trend.
MIN_DAYS_FOR_OWN = 330

# Indices are clipped here. A month genuinely selling 4× the average exists; 20× is a
# data problem, and letting it through would order twenty times too much.
MIN_INDEX = 0.25
MAX_INDEX = 4.0

# Shrinkage toward flat, by how many years of evidence there are. One year of history
# keeps about half the measured swing; three years keep three-quarters.
SHRINK_YEARS = 1.0

# Below this, the peaks and troughs aren't worth acting on and the index is treated as
# flat — avoids nudging every order by 3% for no reason.
MIN_STRENGTH = 0.15


def _flat():
    return [1.0] * 12


def monthly_index(df, *, date_col="ds", value_col="y", shrink=True):
    """A 12-month multiplicative index from one product's history, mean 1.0.

    Returns {index, basis, monthsCovered, days, strength}. `index` is always safe to
    multiply by: a flat list of 1.0 means "no evidence", never "no season".
    """
    out = {"index": _flat(), "basis": "none", "monthsCovered": 0, "days": 0,
           "strength": 0.0, "years": 0.0}
    if df is None or len(df) == 0 or value_col not in df.columns:
        out["basis"] = "no-data"
        return out

    try:
        months = df[date_col].dt.month.tolist()
    except AttributeError:
        out["basis"] = "no-dates"
        return out
    vals = [float(v or 0) for v in df[value_col].fillna(0).tolist()]
    out["days"] = len(vals)
    out["years"] = round(len(vals) / 365.0, 2)

    if len(vals) < MIN_DAYS_FOR_OWN:
        # Less than a year can't distinguish "December is busy" from "sales are growing".
        out["basis"] = "too-short"
        return out

    buckets = {m: [] for m in range(1, 13)}
    for m, v in zip(months, vals):
        buckets[m].append(v)
    covered = [m for m in range(1, 13) if len(buckets[m]) >= MIN_DAYS_PER_MONTH]
    out["monthsCovered"] = len(covered)
    if len(covered) < MIN_MONTHS_COVERED:
        out["basis"] = "too-few-months"
        return out

    overall = sum(sum(buckets[m]) for m in covered) / sum(len(buckets[m]) for m in covered)
    if overall <= 0:
        out["basis"] = "no-sales"
        return out

    idx = []
    for m in range(1, 13):
        if len(buckets[m]) >= MIN_DAYS_PER_MONTH:
            idx.append(sum(buckets[m]) / len(buckets[m]) / overall)
        else:
            idx.append(1.0)          # unobserved month: assume average, don't invent

    if shrink:
        # Weight by years of evidence. One pass through the year is a single sample of
        # each season; two passes is when a pattern starts being a pattern.
        yrs = len(vals) / 365.0
        w = yrs / (yrs + SHRINK_YEARS)
        idx = [1.0 + (v - 1.0) * w for v in idx]
        out["shrinkWeight"] = round(w, 3)

    idx = _clip_and_normalise(idx)
    out["index"] = [round(v, 4) for v in idx]
    out["strength"] = strength(idx)
    out["basis"] = "own-history"
    return out


def _renormalise(idx):
    """Force mean 1.0 so applying the index can't change the annual total — it
    redistributes demand across the year, it doesn't create it."""
    m = sum(idx) / len(idx)
    return [v / m for v in idx] if m > 0 else _flat()


def _clip_and_normalise(idx, *, rounds: int = 3):
    """Bound the index AND keep its mean near 1.0.

    Order matters, and getting it wrong is not cosmetic. Clipping first and normalising
    second lets the cap be undone: an index of [4.0, 0.25 × 11] has a mean of 0.56, so
    renormalising pushes January back up to 7.1× — past the cap, on a product the cap
    exists to protect. Alternate the two until they agree, then clip LAST so the bound is
    the guarantee that survives.
    """
    out = list(idx)
    for _ in range(rounds):
        out = _renormalise(out)
        clipped = [min(MAX_INDEX, max(MIN_INDEX, v)) for v in out]
        if clipped == out:
            break
        out = clipped
    return [min(MAX_INDEX, max(MIN_INDEX, v)) for v in _renormalise(out)]


def strength(idx) -> float:
    """How seasonal this actually is: the spread of the index around 1.0.

    0.0 is flat. 0.5 means the busiest month runs roughly 50% above the quietest relative
    to average. Used to decide whether a season is worth acting on at all.
    """
    if not idx:
        return 0.0
    return round((max(idx) - min(idx)) / 2.0, 3)


def cohort_monthly_index(frames, *, date_col="ds", value_col="y"):
    """A monthly index pooled from peer products.

    Each peer is normalised to its own mean before pooling, so a high-volume neighbour
    contributes its SHAPE rather than its size. This is the same idea the pooled engine
    already uses for new products; here it serves intermittent ones, whose own history is
    too sparse to show a season even when they plainly have one.
    """
    acc = {m: [] for m in range(1, 13)}
    used = 0
    for df in frames or []:
        r = monthly_index(df, date_col=date_col, value_col=value_col, shrink=False)
        if r["basis"] != "own-history":
            continue
        used += 1
        for m in range(1, 13):
            acc[m].append(r["index"][m - 1])
    if used < 2:
        return {"index": _flat(), "basis": "too-few-peers", "peers": used, "strength": 0.0}
    idx = [(sum(acc[m]) / len(acc[m])) if acc[m] else 1.0 for m in range(1, 13)]
    idx = _clip_and_normalise(idx)
    return {"index": [round(v, 4) for v in idx], "basis": "cohort",
            "peers": used, "strength": strength(idx)}


def coverage_factor(df, idx, *, date_col="ds"):
    """The average seasonal level of the days the RATE was measured over.

    This is what stops the season being applied twice. A rate measured across a full year
    already includes the highs and lows, so its seasonal context is 1.0 and the index can
    be applied as-is. A rate measured over a busy eight months is already elevated; divide
    by this factor first, or December gets its peak applied on top of a peak.
    """
    if df is None or len(df) == 0 or not idx:
        return 1.0
    try:
        months = df[date_col].dt.month.tolist()
    except AttributeError:
        return 1.0
    if not months:
        return 1.0
    vals = [idx[m - 1] for m in months]
    f = sum(vals) / len(vals)
    return f if f > 0 else 1.0


def future_multipliers(future_dates, idx, *, coverage=1.0, min_strength=MIN_STRENGTH):
    """One multiplier per future day.

    Returns all-1.0 when the index is too flat to be worth acting on, so an aseasonal
    product is bit-for-bit unchanged rather than nudged by rounding noise.
    """
    if not idx or strength(idx) < min_strength:
        return [1.0] * len(future_dates)
    cov = coverage if coverage and coverage > 0 else 1.0
    out = []
    for d in future_dates:
        try:
            m = _dt.date.fromisoformat(str(d)[:10]).month
        except ValueError:
            out.append(1.0)
            continue
        out.append(round(idx[m - 1] / cov, 4))
    return out


def resolve(df, peer_frames=None, *, date_col="ds", value_col="y"):
    """Pick the best available seasonal index for a product, and say where it came from.

    Own history wins when there's a year of it. Otherwise peers, which is the case that
    matters: an intermittent product usually can't see its own season, and its cohort can.
    """
    own = monthly_index(df, date_col=date_col, value_col=value_col)
    if own["basis"] == "own-history" and own["strength"] >= MIN_STRENGTH:
        own["source"] = "own"
        return own
    coh = cohort_monthly_index(peer_frames, date_col=date_col, value_col=value_col)
    if coh["basis"] == "cohort" and coh["strength"] >= MIN_STRENGTH:
        coh["source"] = "cohort"
        coh["ownBasis"] = own["basis"]
        return coh
    # Nothing usable. Return the own result so the caller can explain WHY it's flat.
    own["source"] = "none"
    own["index"] = _flat()
    own["strength"] = 0.0
    if coh.get("basis") == "cohort":
        own["cohortStrength"] = coh["strength"]
    return own


MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]


def explain(res: dict) -> str:
    if not res or res.get("source") == "none":
        b = (res or {}).get("basis")
        if b == "too-short":
            return ("Not enough history to see a yearly pattern yet — a full year is "
                    "needed before seasonality can be told apart from a trend.")
        if b == "too-few-months":
            return "Sales are spread over too few months of the year to read a season from."
        if res and res.get("cohortStrength") is not None:
            return "Neither this product nor similar ones show a clear seasonal pattern."
        return ""
    idx = res.get("index") or []
    if not idx:
        return ""
    peak = idx.index(max(idx))
    trough = idx.index(min(idx))
    where = ("its own history" if res.get("source") == "own"
             else f"{res.get('peers', 0)} similar products")
    return (f"Seasonal: {MONTH_NAMES[peak]} runs about {idx[peak]:.2f}× an average month "
            f"and {MONTH_NAMES[trough]} about {idx[trough]:.2f}×, measured from {where}. "
            f"The forecast is scaled month by month, so orders rise ahead of the busy "
            f"season instead of after it.")

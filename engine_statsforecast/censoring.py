"""
Censored demand — what you sold is not what people wanted.

A day that ends at zero stock tells you demand was AT LEAST what you sold. It does not
tell you what demand was. Treating the two as the same is the single most common way an
inventory forecast talks itself into a death spiral:

    stock out  ->  sales look low  ->  forecast drops  ->  order less  ->  stock out sooner

The engine used to drop any day with zero closing stock. That's safer than believing the
number, but it is still wrong in an expensive direction: the days you sell out are your
BUSIEST days, so dropping them systematically deletes the top of the distribution and the
forecast drifts low. You end up with a model fitted only to the days nothing interesting
happened.

This module treats those days as what they statistically are: right-censored
observations. Three cases, and the distinction is availability, not sales:

    available all day            -> observed units ARE demand. Nothing to do.
    available part of the day    -> units are a floor. Scale up by the time you were
                                    open, with a hard cap so a 90-minute window can't
                                    invent a number.
    unavailable all day          -> no information at all. Excluded, not zeroed.

The uplift is deliberately conservative. Selling 5 units in the 8% of a day you had stock
does NOT mean you'd have sold 62. Demand isn't uniform across a day, early hours are
usually the busiest for a product that sells out, and one short window is a tiny sample.
`MAX_UPLIFT` bounds how far a single day can be extrapolated, and anything below
`MIN_AVAILABILITY` is treated as no information rather than a wild guess.
"""

from __future__ import annotations

import datetime as _dt

# A day counted as fully available above this. Shopify timestamps and hourly polling both
# have slack; demanding a literal 24.0 would mark ordinary days as censored.
FULL_DAY_HOURS = 24.0
FULL_AVAILABILITY = 0.95

# Below this, a day is too short to say anything. 2.4h = 10% of a day.
MIN_AVAILABILITY = 0.10

# The most a single day's observation may be scaled. 3x means a day available a third of
# the time can be projected to a full day, and no further. Chosen because demand within a
# day is not uniform — a product that sells out does so during its busy hours, so the
# remaining hours would have sold LESS per hour, not the same. Extrapolating linearly is
# already generous; extrapolating 12x would be fiction.
MAX_UPLIFT = 3.0

NORMAL = "normal"
CAPPED = "capped"            # ended at zero, but was available essentially all day
PARTIAL = "partial"          # ran out partway through
UNAVAILABLE = "unavailable"  # had nothing to sell


def availability(hours_in_stock, day_hours: float = FULL_DAY_HOURS) -> float:
    """Fraction of the day the product could actually be bought. Missing → assume full,
    because that's the pre-tracking default and it changes nothing."""
    if hours_in_stock is None:
        return 1.0
    try:
        h = float(hours_in_stock)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, h / float(day_hours or FULL_DAY_HOURS)))


def classify_day(units, closing_stock=None, hours_in_stock=None) -> str:
    """What kind of observation is this day?

    Availability decides, not sales. A day with zero sales and full stock is a real zero;
    a day with zero sales and no stock is not a data point at all.
    """
    av = availability(hours_in_stock)
    try:
        sold = float(units or 0) > 0
    except (TypeError, ValueError):
        sold = False
    ended_empty = closing_stock is not None and float(closing_stock) <= 0
    if av < MIN_AVAILABILITY:
        # A day that SOLD something had stock to sell, whatever hourly polling saw (a
        # restock and sell-through between two readings shows as 0 hours). Dropping it
        # threw real sales away. Kept as a floor: demand was at least what sold, and
        # there's too little of the day to scale it up.
        return CAPPED if sold else UNAVAILABLE
    if av < FULL_AVAILABILITY:
        return PARTIAL
    if ended_empty:
        # In stock all day and finished at zero: everyone who came got served, but we
        # cannot know whether the next person would have bought. A floor, not a fact.
        return CAPPED
    return NORMAL


def uplift_factor(av: float, cap: float = MAX_UPLIFT) -> float:
    if av >= FULL_AVAILABILITY:
        return 1.0
    if av <= 0:
        return 1.0
    return min(cap, 1.0 / av)


def estimate_demand(units, closing_stock=None, hours_in_stock=None,
                    *, cap: float = MAX_UPLIFT) -> dict:
    """Turn one day of sales into a demand estimate plus an honest description of it.

    Returns:
      observed   what actually sold — never modified, always the floor
      estimate   demand estimate for fitting (>= observed)
      floor      True when `observed` is a lower bound rather than the truth
      weight     how much this day should count when fitting. An uplifted day is an
                 inference, so it carries less weight than a day we simply watched.
    """
    u = float(units or 0.0)
    av = availability(hours_in_stock)
    kind = classify_day(u, closing_stock, hours_in_stock)

    if kind == UNAVAILABLE:
        # Nothing to learn. Excluding beats recording a zero that never happened.
        return {"kind": kind, "observed": u, "estimate": None, "floor": True,
                "availability": round(av, 3), "weight": 0.0, "uplift": None}

    if kind == NORMAL:
        return {"kind": kind, "observed": u, "estimate": u, "floor": False,
                "availability": round(av, 3), "weight": 1.0, "uplift": 1.0}

    if kind == CAPPED:
        # Available all day but finished empty. We know demand >= units; we have no basis
        # for saying how much more, so we do NOT invent one. The value is kept as a floor
        # and given less weight, so it can't drag the fit down as if it were a full
        # observation of a quiet day.
        return {"kind": kind, "observed": u, "estimate": u, "floor": True,
                "availability": round(av, 3), "weight": 0.5, "uplift": 1.0}

    f = uplift_factor(av, cap)
    est = u * f
    # Confidence falls with the length of the window, and an uplifted day is inference.
    # Weight is the availability itself: half a day observed counts about half.
    return {"kind": PARTIAL, "observed": u, "estimate": round(est, 3), "floor": True,
            "availability": round(av, 3), "weight": round(max(0.1, av), 3),
            "uplift": round(f, 3), "capped": f >= cap}


def annotate(rows: list) -> list:
    """Apply `estimate_demand` across daily rows.

    `rows`: [{date, units_sold, closing_stock?, hours_in_stock?}, ...]
    """
    out = []
    for r in rows:
        est = estimate_demand(r.get("units_sold"), r.get("closing_stock"),
                              r.get("hours_in_stock"))
        out.append({**r, **{f"demand_{k}" if k in ("estimate", "floor") else k: v
                            for k, v in est.items()}})
    return out


def summarise(rows: list) -> dict:
    """What the censoring is doing to this product, in terms someone can act on."""
    ann = annotate(rows)
    counts = {NORMAL: 0, CAPPED: 0, PARTIAL: 0, UNAVAILABLE: 0}
    obs = est = 0.0
    for r in ann:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        obs += float(r.get("units_sold") or 0)
        if r.get("demand_estimate") is not None:
            est += float(r["demand_estimate"])
    n = len(ann)
    censored = counts[CAPPED] + counts[PARTIAL] + counts[UNAVAILABLE]
    return {
        "days": n,
        "normal": counts[NORMAL], "capped": counts[CAPPED],
        "partial": counts[PARTIAL], "unavailable": counts[UNAVAILABLE],
        "censoredDays": censored,
        "censoredShare": round(censored / n * 100, 1) if n else 0.0,
        "observedUnits": round(obs, 1),
        "estimatedUnits": round(est, 1),
        # How much demand the raw sales figure was hiding.
        "hiddenUnits": round(est - obs, 1),
        "understatedPct": round((est - obs) / obs * 100, 1) if obs > 0 else None,
    }


def explain(summary: dict) -> str:
    """One sentence, in plain terms, for the product panel."""
    if not summary or not summary.get("days"):
        return ""
    c = summary["censoredDays"]
    if not c:
        return "In stock every day in this window, so sales and demand are the same thing here."
    bits = []
    if summary["partial"]:
        bits.append(f"{summary['partial']} sold out partway through the day")
    if summary["capped"]:
        bits.append(f"{summary['capped']} finished at zero")
    if summary["unavailable"]:
        bits.append(f"{summary['unavailable']} had nothing to sell")
    head = f"{c} of {summary['days']} days were limited by stock, not by demand"
    tail = f" ({'; '.join(bits)})." if bits else "."
    if summary.get("understatedPct"):
        tail += (f" Sales understate demand by roughly {summary['understatedPct']}% "
                 f"(about {summary['hiddenUnits']:.0f} units you couldn't sell).")
    return head + tail


# ── deriving hours-in-stock from inventory samples ───────────────────────────────────


# A stock reading vouches for the level this long after it was taken (a day, plus slack
# for a missed poll). Past that, the product isn't being watched.
CARRY_HOURS = 26.0
# A day needs this many watched hours before it says anything about availability.
MIN_COVERED_HOURS = 12.0


def hours_in_stock_from_samples(samples: list, day: str,
                                day_hours: float = FULL_DAY_HOURS) -> float | None:
    """How long a product was buyable on `day`, from timestamped inventory readings.

    `samples`: [{ts: ISO8601, onHand: int}, ...] in any order.

    Each sample is taken to describe the stock level until the next one. That's the
    honest reading of a poll: we know the level when we looked, and nothing between looks.
    Returns None when the day isn't covered well enough to say — a gap in polling must not
    masquerade as a stockout, which would silently delete a good day's data.
    """
    # The STORE's day (store_clock), which is how its sales are dated. Counted over UTC
    # days, a product that sold out at 11pm Eastern on Saturday and was restocked Monday
    # showed 3 hours in stock on an empty Sunday.
    import store_clock as _CLOCK
    start = _CLOCK.day_start(str(day)[:10])
    end = start + _dt.timedelta(hours=day_hours)

    pts = []
    for s in samples or []:
        try:
            ts = _dt.datetime.fromisoformat(str(s["ts"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_dt.timezone.utc)
            pts.append((ts, float(s.get("onHand") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    if not pts:
        return None
    pts.sort()

    # The level entering the day is the last reading at or before midnight.
    prior = [p for p in pts if p[0] <= start]
    inday = [p for p in pts if start < p[0] < end]
    if not prior and not inday:
        return None
    level = prior[-1][1] if prior else inday[0][1]

    # How far a reading can be trusted. Identical readings are stored once (stock_log
    # collapses them and keeps moving the latest one forward), so a level is known until
    # the NEXT reading; after the last reading there is none, and the level was carried
    # forward forever: a product last seen at 40 units in March was "in stock all day"
    # every day since, long after polling stopped. A reading now vouches for the level
    # for CARRY_HOURS past it (a day, plus slack for a missed poll), no longer.
    last_ts = pts[-1][0]
    known_until = min(end, last_ts + _dt.timedelta(hours=CARRY_HOURS))
    if any(p[0] >= end for p in pts):
        known_until = end
    known_from = start if prior else inday[0][0]

    covered = 0.0
    in_stock = 0.0
    cursor = known_from
    for ts, on_hand in inday:
        span = (min(ts, known_until) - cursor).total_seconds() / 3600.0
        if span > 0:
            covered += span
            if level > 0:
                in_stock += span
        level = on_hand
        cursor = max(cursor, ts)
    tail = (known_until - cursor).total_seconds() / 3600.0
    if tail > 0:
        covered += tail
        if level > 0:
            in_stock += tail

    # Refuse to answer on thin coverage rather than guess (the check that used to sit
    # here could never fire). With most of the day watched, the unwatched part is
    # assumed to look like the watched part.
    if covered < MIN_COVERED_HOURS:
        return None
    if covered < day_hours:
        in_stock = in_stock * day_hours / covered
    return round(min(day_hours, max(0.0, in_stock)), 2)


# ── applying it to a training frame ──────────────────────────────────────────────────


def apply_to_frame(df, availability_map=None, *, stock_col="units_in_stock",
                   date_col="ds", value_col="y"):
    """Rewrite a training frame so the models fit DEMAND rather than SALES.

    Replaces the old blanket rule ("drop every row where stock <= 0"), which was safer
    than believing the numbers but still biased: the days you sell out are your busiest,
    so dropping them deletes the top of the distribution and every forecast drifts low.

    What happens to each row:
      normal       untouched
      capped       kept at the observed value — a floor we won't inflate without evidence
      partial      scaled up by how much of the day you were actually open
      unavailable  removed, because it carries no information about demand

    `availability_map`: {"YYYY-MM-DD": hours_in_stock}. Absent dates are treated as fully
    available, so a catalogue with no tracking behaves exactly as it did before.

    Returns (adjusted_frame, summary).
    """
    import pandas as _pd  # local: this module is importable without pandas for unit tests

    if df is None or len(df) == 0:
        return df, {"days": 0, "adjusted": 0, "removed": 0, "addedUnits": 0.0}

    out = df.copy()
    amap = availability_map or {}
    dates = (out[date_col].dt.strftime("%Y-%m-%d")
             if hasattr(out[date_col], "dt") else out[date_col].astype(str))

    kinds, ests, weights = [], [], []
    prev_close = None
    for i, (_, row) in enumerate(out.iterrows()):
        day = dates.iloc[i]
        stock = row.get(stock_col) if stock_col in out.columns else None
        if stock is not None and _pd.isna(stock):
            stock = None
        hours = amap.get(day)
        # No hours tracked but a zero closing stock still tells us the day was capped —
        # that's the signal an uploaded spreadsheet carries, and it must not be lost just
        # because Shopify polling wasn't running.
        # ...and a day that OPENED empty (yesterday closed at zero), sold nothing and
        # closed at zero had nothing on the shelf all day: no information about demand,
        # exactly like a tracked day with zero hours in stock. Counted as a real zero it
        # dragged the level down and read as a holiday dip (a Black Friday spent out of
        # stock measured as -74%).
        units_ = row.get(value_col)
        if (hours is None and stock is not None and float(stock) <= 0 and prev_close is not None
                and float(prev_close) <= 0 and float(units_ or 0) <= 0):
            hours = 0.0
        prev_close = stock
        est = estimate_demand(units_, stock, hours)
        kinds.append(est["kind"])
        ests.append(est["estimate"])
        weights.append(est["weight"])

    out["_censor_kind"] = kinds
    out["_censor_weight"] = weights
    before_units = float(out[value_col].fillna(0).sum())

    keep = [k != UNAVAILABLE for k in kinds]
    removed = int(len(keep) - sum(keep))
    out = out[keep].copy()
    est_kept = [e for e, k in zip(ests, keep) if k]
    out[value_col] = [float(e) if e is not None else 0.0 for e in est_kept]

    after_units = float(out[value_col].sum())
    adjusted = int(sum(1 for k, kk in zip(kinds, keep) if kk and k == PARTIAL))
    return out, {
        "days": len(kinds),
        "normal": kinds.count(NORMAL),
        "capped": kinds.count(CAPPED),
        "partial": kinds.count(PARTIAL),
        "unavailable": kinds.count(UNAVAILABLE),
        "adjusted": adjusted,
        "removed": removed,
        "observedUnits": round(before_units, 1),
        "fittedUnits": round(after_units, 1),
        # How many units of demand the raw sales figures were hiding.
        "addedUnits": round(after_units - before_units, 1),
        "tracked": bool(amap),
    }

"""
holiday_shape.py — where the units of a holiday stretch land, day by day.

WHAT IT REPLACES AND WHY

The old holiday layer measured each holiday against "normal" days around it and applied
a +X% on top. That needed a definition of normal (the weeks before and after, which are
often part of the same seasonal climb), an exact window per holiday, and special handling
for the dip after. Each answer had edge cases, and they produced numbers that weren't
there (a "-27% tax refund season" that was just the season climbing).

WHAT THIS DOES INSTEAD

The forecast already gets the TOTAL for the weeks around a holiday right: every past year
had the holiday inside those weeks. What a smooth forecast gets wrong is WHERE in those
weeks the units land (a yearly curve can't draw a two-day spike, and a moving holiday like
Thanksgiving or Easter falls on different dates each year). So:

  1. Each holiday has a STRETCH: a few days before it, its core days, a few days after.
     One per holiday, set generous on purpose (see EVENTS). Too wide only adds ordinary
     days, which keep ordinary shares; too narrow leaves a bit of run-up with the normal
     forecast. Neither changes a total.
  2. Past years are lined up on the holiday itself (Thanksgiving to Thanksgiving, Easter
     to Easter), and each day of the stretch gets a relative level: how that day sold
     compared with the stretch as a whole, with the weekday pattern taken out. Days on a
     promotion are left out (Events carry those), and closed days are already gone.
  3. This year's forecast total for the stretch is split across its days by those
     levels. The total comes from this year's forecast, so doing better or worse than
     last year moves every day; past years only decide the split.
  4. The models train on history exactly as it happened. Past holidays are part of the
     yearly season the models learn, which is what gets each stretch's total right.
     (Flattening each past stretch first was tried and removed: a stretch running from
     a seasonal peak into a quiet spell, Christmas into January, lifted the quiet days
     at the end of the history to the peak's average, and the models took that as the
     current level.)

No "normal" is defined and nothing is added on top: the stretch's total is always the
forecast's own. Overlapping stretches split: each day belongs to the nearest holiday's
core days. A stretch already under way is split as a whole (the units already sold count
toward its total), so the days left never absorb the whole stretch. A stretch
running past the end of the forecast is split over the days there are.

Slow sellers have noisy day-by-day levels, so each day's level is pulled toward the same
day's level across similar products, and toward even, by how few units it rests on.
"""
from __future__ import annotations

import datetime as _dt
import math

import os as _os_k

import numpy as np
import pandas as pd

import holiday_calendar as _HOL

D = _dt.date
TD = _dt.timedelta

# key, name, core-days rule, days before the core, days after it, what the core is called.
# Core rules return (first core day, last core day) for a year, or a list of them for the
# Prime events (dates announced each year, see holiday_calendar.TABLE_DATES / settings).
EVENTS = [
    ("thanksgiving_week", "Thanksgiving to Cyber Monday", "thanksgiving", 7, 7, "Thanksgiving to Cyber Monday"),
    ("christmas_newyear", "Christmas to New Year's", "christmas", 14, 7, "Christmas Eve to New Year's Day"),
    ("valentines", "Valentine's Day", ("fixed", 2, 14), 10, 2, "Valentine's Day"),
    ("super_bowl", "Super Bowl", ("superbowl",), 5, 1, "game day"),
    ("presidents_day", "Presidents' Day weekend", ("weekend", ("nth", 2, 0, 3)), 3, 1, "the long weekend"),
    ("st_patricks", "St. Patrick's Day", ("fixed", 3, 17), 5, 1, "St. Patrick's Day"),
    ("easter", "Easter", ("easter",), 10, 2, "Easter weekend"),
    ("mothers_day", "Mother's Day", ("nth", 5, 6, 2), 10, 2, "Mother's Day"),
    ("memorial_day", "Memorial Day weekend", ("weekend", ("last", 5, 0)), 5, 2, "the long weekend"),
    ("fathers_day", "Father's Day", ("nth", 6, 6, 3), 10, 2, "Father's Day"),
    ("july_4", "Fourth of July", ("fixed", 7, 4), 7, 3, "the Fourth"),
    ("prime_day", "Prime Day", ("table", "prime_day"), 3, 3, "Prime Day"),
    ("labor_day", "Labor Day weekend", ("weekend", ("nth", 9, 0, 1)), 5, 2, "the long weekend"),
    ("prime_october", "Prime Big Deal Days", ("table", "prime_october"), 3, 3, "the sale days"),
    ("halloween", "Halloween", ("fixed", 10, 31), 14, 3, "Halloween"),
]
# For comparison runs only: the same 10 days either side of every holiday's core instead
# of each holiday's own lengths.
import os as _os
_FLAT = _os.environ.get("LOGITRACK_HS_FLAT", "").strip()
if _FLAT.isdigit():
    EVENTS = [(k, n, r, int(_FLAT), int(_FLAT), c) for k, n, r, _b, _a, c in EVENTS]
KEYS = [e[0] for e in EVENTS]
_HOL._EXTRA_KEYS.update(KEYS)          # so a stretch can be switched off per product
_ORDER = {k: i for i, k in enumerate(KEYS)}
_BY = {e[0]: e for e in EVENTS}

# A day's level rests on this many expected units (times how much noisier than chance the
# product's days are) before it counts at full weight; below that it's pulled toward
# similar products' level for the same day, then toward even.
SHRINK_UNITS = float(_os_k.environ.get("LOGITRACK_HS_SHRINK", "10.0"))
# Days outside a holiday's core (the run-up and the tail) are read over the days either
# side of them when they rest on few units, so a single lucky Tuesday isn't a run-up. A day
# with plenty of units behind it (SMOOTH_UNITS effective units: older years count for
# less) is read on its own, so a busy product's real one-day spike outside the core (early
# deals the Saturday before) isn't smeared over a week. At 60 this cost nothing on the
# mock stores; lower let noise in, 25 cost 0.6 points of day-by-day accuracy.
SMOOTH_DAYS = 3
SMOOTH_UNITS = float(_os_k.environ.get("LOGITRACK_HS_SMOOTH_UNITS", "60.0"))
# The first and last BLEND_DAYS days of a stretch cross-fade from the normal forecast into
# the holiday's shape, so the forecast doesn't step where a stretch starts or ends (the
# day before a stretch and its first day are both close to the normal forecast). The
# stretch keeps its total: what the edges don't take, the middle does. 0 = no blend.
BLEND_DAYS = int(_os_k.environ.get("LOGITRACK_HS_BLEND", "4"))
# A holiday is shown as moving a product only when its busiest or quietest stretch of days
# is this many standard deviations from even, on the evidence behind it (own + similar
# products). Its shape is used either way (it only moves units within the stretch).
SHOW_Z = 3.0
# How much each older year counts, next to the most recent one: a year's pattern is
# weighted by its units (more sales, less chance in it) times this for every year it is
# older than the latest. 1.0 = every year alike. On the five mock stores 1.0, 0.75 and
# 0.5 forecast equally well; 0.75 follows a shift in shopping habits (a rush moving to the
# weekend before) better, see test_holiday_shape.py.
import os as _os0
YEAR_DECAY = float(_os0.environ.get("LOGITRACK_HS_DECAY", "0.75"))
# Past years whose stretch is less than this share on file are left out (history that
# starts or ends partway through one).
MIN_COVER = 0.6
# A stretch "moves" a product (listed on its page) when its busiest or quietest day is
# this far from a typical day of the stretch.
SHOW_HIGH, SHOW_LOW = 1.25, 0.8


def name_of(key):
    return _BY[key][1] if key in _BY else key


def _cores(key, year, settings):
    """[(first core day, last core day)] for `key` in `year`."""
    rule = _BY[key][2]
    try:
        if rule == "thanksgiving":
            tg = _HOL._nth(year, 11, _HOL.THU, 4)
            return [(tg, tg + TD(days=4))]
        if rule == "christmas":
            return [(D(year, 12, 24), D(year + 1, 1, 1))]
        if rule[0] == "weekend":
            mon = _HOL._anchor(rule[1], year)
            return [(mon - TD(days=2), mon)]
        if rule[0] == "table":
            wins = _HOL.windows_for(rule[1], D(year, 1, 1), D(year, 12, 31), settings or {})
            return [(a, b) for a, b in wins]
        d = _HOL._anchor(rule, year)
        return [(d, d)] if d else []
    except Exception:                                   # noqa: BLE001
        return []


def occurrences(start, end, settings=None, off=()):
    """Every holiday occurrence whose stretch touches [start, end]:
    [{"key", "anchor", "core": (a, b), "span": (s, e)}]."""
    out = []
    for key, _n, _r, before, after, _c in EVENTS:
        if key in off:
            continue
        for y in range(start.year - 1, end.year + 2):
            for a, b in _cores(key, y, settings):
                s, e = a - TD(days=before), b + TD(days=after)
                if e >= start and s <= end:
                    out.append({"key": key, "anchor": a, "core": (a, b), "span": (s, e)})
    return out


def assign(start, end, settings=None, off=()):
    """{date: (key, anchor, offset from anchor)} for every day in [start, end] inside a
    stretch. Where stretches overlap, a day belongs to the holiday whose core days it's
    nearest (ties go to the one listed first), so no day is in two stretches."""
    occ = occurrences(start - TD(days=40), end + TD(days=40), settings, off)
    out = {}
    for o in occ:
        s, e = o["span"]
        d = max(s, start)
        while d <= min(e, end):
            a, b = o["core"]
            dist = 0 if a <= d <= b else min(abs((d - a).days), abs((d - b).days))
            cur = out.get(d)
            if cur is None or (dist, _ORDER[o["key"]]) < (cur[3], _ORDER[cur[0]]):
                out[d] = (o["key"], o["anchor"], (d - o["anchor"]).days, dist)
            d += TD(days=1)
    return {d: v[:3] for d, v in out.items()}


# ── learning each day's level ───────────────────────────────────────────────────────
def _usable(df):
    """Days whose units say something about the holiday: not on a promotion or discount
    (Events carry those, so learning them here would count a sale twice)."""
    try:
        return ~_HOL._discount_mask(df)
    except Exception:                                   # noqa: BLE001
        return np.ones(len(df), bool)


def _weekday(df, in_stretch, usable):
    try:
        return _HOL._weekday_index(df["ds"], df["y"].astype(float), usable & ~in_stretch)
    except Exception:                                   # noqa: BLE001
        return np.ones(7)


def measure(df, shift=0, settings=None, off=()):
    """One product's raw evidence, per holiday: for each day of the stretch (offset from
    the holiday), units sold and units expected at the stretch's own level, summed over
    past years; plus each past year's stretch as recorded (for comparison on the page).

    `shift`: days an uploaded sheet's history was moved; holidays are looked up on the
    dates the sales really happened."""
    out = {}
    if df is None or len(df) < 14:
        return out
    d = df.sort_values("ds").reset_index(drop=True)
    sd = TD(days=int(shift or 0))
    real = [x - sd for x in pd.to_datetime(d["ds"]).dt.date]
    A = assign(real[0], real[-1], settings, off)
    y = d["y"].astype(float).to_numpy()
    use = np.asarray(_usable(d), bool)
    in_st = np.array([r in A for r in real])
    wdi = _weekday(d, in_st, use)
    wd = pd.to_datetime(d["ds"]).dt.weekday.to_numpy()
    groups = {}
    for i, r in enumerate(real):
        g = A.get(r)
        if g is not None:
            groups.setdefault((g[0], g[1]), []).append((i, g[2]))
    # How many days each occurrence's stretch has (after overlaps are split), to judge
    # whether a year is on file well enough to use.
    span_len, span_end = {}, {}
    for r, g in assign(real[0] - TD(days=40), real[-1] + TD(days=40), settings, off).items():
        span_len[(g[0], g[1])] = span_len.get((g[0], g[1]), 0) + 1
        span_end[(g[0], g[1])] = max(span_end.get((g[0], g[1]), r), r)
    try:
        phi = float(_HOL._dispersion(y, wdi[wd], use & ~in_st))
    except Exception:                                   # noqa: BLE001
        phi = 2.0
    phi = max(1.0, min(phi, 8.0))
    usable_years = {}
    for (key, anchor), rows in groups.items():
        rec = out.setdefault(key, {"num": {}, "den": {}, "var": {}, "years": [], "core": set(), "phi": phi})
        # Only a stretch that has ENDED is evidence of how a stretch is shaped. One still
        # under way passed the coverage test at 60% of its days, and its first 60% were
        # then read as the whole of that year: the run-up counted, the peak and tail not,
        # which tilted the learned shape toward whatever part had happened so far.
        ended = span_end.get((key, anchor), real[-1]) <= real[-1]
        full = ended and len(rows) >= MIN_COVER * span_len.get((key, anchor), len(rows))
        units = float(sum(y[i] for i, _ in rows))
        core_a, core_b = next(((o["core"]) for o in occurrences(anchor, anchor, settings, off)
                               if o["key"] == key and o["anchor"] == anchor), (anchor, anchor))
        core_units = float(sum(y[i] for i, o in rows if core_a <= anchor + TD(days=o) <= core_b))
        rec["core"].update(o for _, o in rows if core_a <= anchor + TD(days=o) <= core_b)
        rec["years"].append({"anchor": anchor.isoformat(), "start": (anchor + TD(days=min(o for _, o in rows))).isoformat(),
                             "end": (anchor + TD(days=max(o for _, o in rows))).isoformat(),
                             "units": round(units, 1), "coreUnits": round(core_units, 1),
                             "days": len(rows), "full": bool(full),
                             "byOff": {int(o): float(y[i]) for i, o in rows}})
        if not full:
            continue
        ok = [(i, o) for i, o in rows if use[i]]
        wsum = float(sum(wdi[wd[i]] for i, _ in ok))
        if wsum < 5 or not ok:
            continue
        level = float(sum(y[i] for i, _ in ok)) / wsum
        if level <= 0:
            continue
        usable_years.setdefault(key, []).append((anchor, [(o, float(y[i]), level * float(wdi[wd[i]])) for i, o in ok]))
    # Each year counts by its units (they're summed, so a busier year weighs more) times
    # YEAR_DECAY for every year it is older than the most recent one on file.
    for key, yrs in usable_years.items():
        rec = out[key]
        latest = max(a for a, _ in yrs)
        for anchor, days in yrs:
            age = max(0, round((latest - anchor).days / 365.25))
            w = YEAR_DECAY ** age
            for o, yv, ev in days:
                rec["num"][o] = rec["num"].get(o, 0.0) + w * yv
                rec["den"][o] = rec["den"].get(o, 0.0) + w * ev
                rec["var"][o] = rec["var"].get(o, 0.0) + w * w * ev
    for key, rec in out.items():
        rec["years"].sort(key=lambda r: r["anchor"])
        latest = max((a for a, _ in usable_years.get(key, [])), default=None)
        for yv in rec["years"]:
            a = D.fromisoformat(yv["anchor"])
            yv["weight"] = (round(YEAR_DECAY ** max(0, round((latest - a).days / 365.25)), 3)
                            if (latest and yv["full"] and a <= latest) else 0.0)
    return out


def pool(measures, names=None):
    """Similar products' evidence per holiday and day, each product counting as one.

    Summed raw, units decided it: in a family of one 200-a-day seller and nine selling 5,
    the big one was four fifths of the pool, so "how this family shapes Christmas" was
    really "how that one product does". Now no product counts for more than the TYPICAL
    member (the median evidence among those with the holiday on file). Products with less
    than that keep their own weight rather than being scaled up, because scaling a thin
    product up would treat its noise as evidence."""
    out = {}
    tot = {}
    for m in measures or []:
        for key, rec in (m or {}).items():
            t = float(sum(rec.get("den", {}).values()))
            if t > 0:
                tot.setdefault(key, []).append(t)
    cap = {k: float(np.median(v)) for k, v in tot.items()}
    for idx, m in enumerate(measures or []):
        who = (names[idx] if names and idx < len(names) else None) or f"product {idx + 1}"
        for key, rec in (m or {}).items():
            p = out.setdefault(key, {"num": {}, "den": {}, "var": {}, "n": 0, "core": set(), "phis": [],
                                     "who": {}})
            p["n"] += 1
            p["core"].update(rec.get("core") or ())
            p["phis"].append(float(rec.get("phi") or 2.0))
            t = float(sum(rec.get("den", {}).values()))
            w = min(1.0, cap[key] / t) if (t > 0 and key in cap) else 1.0
            # How much of the similar products' evidence each one is (what the page shows).
            p["who"][who] = p["who"].get(who, 0.0) + w * t
            for o, v in rec["num"].items():
                p["num"][o] = p["num"].get(o, 0.0) + w * v
            for o, v in rec["den"].items():
                p["den"][o] = p["den"].get(o, 0.0) + w * v
            for o, v in (rec.get("var") or rec["den"]).items():
                p["var"][o] = p["var"].get(o, 0.0) + w * w * v
    return out


def _window(src, off, core):
    """(sold, expected, spread) behind day `off` from one source (own or pooled): the day
    itself on a core day or when it rests on SMOOTH_UNITS effective units or more, otherwise the days
    either side of it outside the core."""
    num, den = src.get("num", {}), src.get("den", {})
    var = src.get("var") or den
    # Effective units: what the day's evidence is worth once older years count for less
    # (the same number of units spread over more, older years is worth less).
    d0, v0 = den.get(off, 0.0), var.get(off, 0.0)
    if off in core or (v0 > 0 and d0 * d0 / v0 >= SMOOTH_UNITS):
        js = [off]
    else:
        js = [j for j in range(off - SMOOTH_DAYS, off + SMOOTH_DAYS + 1) if j not in core]
    return (float(sum(num.get(j, 0.0) for j in js)), float(sum(den.get(j, 0.0) for j in js)),
            float(sum(var.get(j, 0.0) for j in js)))


def levels(own, pooled, k=SHRINK_UNITS):
    """{key: {"r": {offset: level}, "own": expected own units, "pool": peers used, "z": ...}}.

    Each day's level is the product's own (sold / expected), pulled toward the same day
    across similar products, which is itself pulled toward even (1.0), each by how few
    units it rests on (scaled by how noisy the products' days are). Run-up and tail days
    are read over the days either side. `z` says how clearly the busiest and quietest days
    differ from even on all that evidence."""
    out = {}
    for key in set(own or {}) | set(pooled or {}):
        o, p = (own or {}).get(key) or {}, (pooled or {}).get(key) or {}
        core = set(o.get("core") or ()) | set(p.get("core") or ())
        offs = set(o.get("num", {})) | set(p.get("num", {}))
        phi_o = float(o.get("phi") or 2.0)
        phi_p = float(np.mean(p["phis"])) if p.get("phis") else 2.0
        ko, kp = k * phi_o, k * phi_p
        r, zs, own_w = {}, [], []
        for off in offs:
            pn, pd_, pv = _window(p, off, core)
            prior = (pn + kp) / (pd_ + kp)
            on, od, ov = _window(o, off, core)
            r[off] = (on + ko * prior) / (od + ko)
            if off in core or not core:
                own_w.append(od / (od + ko) if (od + ko) > 0 else 0.0)
            n_, d_ = on + pn, od + pd_
            # Older years count for less, so they're less evidence too: the spread of a
            # weighted sum is the weights squared times each day's own spread.
            v_ = ov + pv
            if d_ > 0 and v_ > 0:
                zs.append((n_ - d_) / math.sqrt(max(phi_o, phi_p) * v_))
        own_e = float(sum(o.get("den", {}).values()))
        # How much of the shape on the holiday's own days comes from this product's sales;
        # the rest comes from the similar products (and, where they're thin too, from even).
        own_share = round(100.0 * float(np.mean(own_w)), 0) if own_w else 0.0
        who = p.get("who") or {}
        tw = float(sum(who.values()))
        shares = sorted(({"name": n, "pct": round(100.0 * v / tw)} for n, v in who.items() if tw > 0),
                        key=lambda x: -x["pct"])
        out[key] = {"r": r, "own": own_e, "pool": int(p.get("n", 0)),
                    "ownShare": own_share, "peerShares": shares,
                    "years": len([y for y in o.get("years", []) if y.get("full")]),
                    "zHigh": round(max(zs), 2) if zs else 0.0, "zLow": round(min(zs), 2) if zs else 0.0}
    return out


def significant(L):
    """Whether a holiday's shape clearly moves a product: its busiest day stands this far
    above a typical day of the stretch (or its quietest this far below) AND that's well
    beyond chance on the evidence behind it. Only then is the forecast reshaped; otherwise
    the weeks around it are forecast as usual (reshaping on noise only adds error)."""
    r = list((L or {}).get("r", {}).values())
    if not r:
        return False
    med = float(np.median(r)) or 1.0
    hi, lo = max(r) / med, min(r) / med
    return bool((hi >= SHOW_HIGH and float(L.get("zHigh") or 0) >= SHOW_Z)
                or (lo <= SHOW_LOW and float(L.get("zLow") or 0) <= -SHOW_Z))


# ── using them ───────────────────────────────────────────────────────────────────────
def _wdi_of(df):
    d = df.sort_values("ds")
    use = np.asarray(_usable(d), bool)
    return _weekday(d, np.zeros(len(d), bool), use)


def redistribute(fut_dates, base, to_real, A, lv, wdi, past=None, closed=None):
    """Split each stretch's total across its days by its shape.

    fut_dates/base: the forecast days and their values before any event or closed-day
    multiplier. past: {date: units sold} for days of a stretch already sold (a
    stretch under way is split as a whole, so the days left get only their own share).
    closed: boolean per forecast day; a closed day takes no share. Days outside every
    stretch are returned untouched. Returns (values, {(key, anchor): info})."""
    base = np.asarray(base, float).copy()
    if not lv or not len(base):
        return base, {}
    closed = np.zeros(len(base), bool) if closed is None else np.asarray(closed, bool)
    fd = [pd.Timestamp(x).date() for x in fut_dates]
    groups = {}
    for i, x in enumerate(fd):
        g = A.get(to_real(x))
        if g is not None and g[0] in lv and not closed[i]:
            groups.setdefault((g[0], g[1]), []).append((i, g[2], x))
    past_by = {}
    for x, v in (past or {}).items():
        g = A.get(to_real(x))
        if g is not None and g[0] in lv:
            past_by.setdefault((g[0], g[1]), []).append((x, g[2], float(v)))
    # Each stretch's first and last offset (as assigned, after overlaps), for the blend.
    ends = {}
    for d_, g in A.items():
        if g[0] in lv:
            lo, hi = ends.get((g[0], g[1]), (g[2], g[2]))
            ends[(g[0], g[1])] = (min(lo, g[2]), max(hi, g[2]))
    orig = base.copy()
    info = {}
    for gk, rows in groups.items():
        r = lv[gk[0]]["r"]
        w = [float(wdi[x.weekday()]) * r.get(o, 1.0) for _, o, x in rows]
        pr = past_by.get(gk, [])
        wp = [float(wdi[x.weekday()]) * r.get(o, 1.0) for x, o, _ in pr]
        fut_tot = float(sum(max(orig[i], 0.0) for i, _, _ in rows))
        total = fut_tot + float(sum(v for _, _, v in pr))
        ws = float(sum(w) + sum(wp))
        if ws <= 0 or total <= 0:
            continue
        shaped = [total * wi / ws for wi in w]
        if BLEND_DAYS > 0:
            lo, hi = ends.get(gk, (min(o for _, o, _ in rows), max(o for _, o, _ in rows)))
            tau = [min(1.0, (min(o - lo, hi - o) + 1) / (BLEND_DAYS + 1)) for _, o, _ in rows]
            b0 = [max(orig[i], 0.0) for i, _, _ in rows]
            f = [bb + t * (sv - bb) for bb, t, sv in zip(b0, tau, shaped)]
            # The stretch keeps its total: the difference goes to the days in proportion to
            # how fully they follow the shape, so the edges stay near the normal forecast.
            gap = float(sum(shaped) - sum(f))
            wt = [t * sv for t, sv in zip(tau, shaped)]
            sw = float(sum(wt))
            if sw > 0:
                f = [max(0.0, fv + gap * wv / sw) for fv, wv in zip(f, wt)]
            shaped = f
        for (i, _, _), v in zip(rows, shaped):
            base[i] = v
        info[gk] = {"total": total, "sold": float(sum(v for _, _, v in pr)), "days": len(rows) + len(pr)}
    return base, info

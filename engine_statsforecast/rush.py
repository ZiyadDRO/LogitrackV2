"""One-off rushes: a stretch of weeks well above normal that has ended and wasn't there the
year before (a World Cup, a viral week, an ice storm). Left in the training data, part of it
sticks: the forecast starts from a level sales have already left. Taken out, the models learn
from ordinary weeks.

Found from the product's own sales, week by week (on days it was in stock):
  normal level   the median of the weeks just before and just after the stretch, so a
                 steady rise or fall around it isn't mistaken for a rush
  a rush         at least RUSH_MIN_WEEKS weeks in a row at RUSH_RATIO x normal or more (and
                 clearly more than chance on that many sales), at most RUSH_MAX_WEEKS long
                 (longer is a season or a new normal, not a rush), with the weeks around it
                 back near normal
  ended          at least RUSH_AFTER_WEEKS weeks after it are back near normal; a rush still
                 going on is left alone (the 4-week check follows it)
  not yearly     the same weeks don't usually rush (the middle of the last 3 years, give or
                 take 3 weeks). Only the
                 part above last year's is taken out, so a December that is always busy
                 keeps its usual December and loses only the unusual extra. With no year
                 before, similar products decide: if they rushed at the same time this year
                 but not the year before, it's a one-off (a World Cup); if they can't tell,
                 nothing is taken out (a new product's first season must never be removed).

Taking it out works like a holiday: each rush week's units are divided by how far above
normal that week was (after last year's usual share), so the days keep their ups and downs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RUSH_RATIO = 1.5          # a rush week sells at least this multiple of normal
RUSH_TAIL = 1.2           # weeks either side still this far above normal belong to it
RUSH_MIN_WEEKS = 2
RUSH_MAX_WEEKS = 10
RUSH_AFTER_WEEKS = 2      # weeks back near normal after it before it counts as ended
RUSH_SIDE_WEEKS = 8       # weeks each side the normal level is taken from
RUSH_UNUSUAL = 2.0        # at least twice as far above normal as the same weeks last year
RUSH_LAST_NORMAL = 1.3    # last year's same weeks this close to normal count as normal
RUSH_Z = 3.0              # excess units at least this many standard deviations
RUSH_PHI = 2.0            # overdispersion allowed for (retail sales are lumpier than Poisson)
MIN_DAYS_IN_WEEK = 4
YEAR_WEEKS = 52
YEAR_SLACK = 3
RUSH_YEARS = 3            # past years checked for the same peak


def _weekly(df, end):
    """Mean daily units per week (weeks ending on `end`), counting only days on file."""
    d = df[["ds", "y"]].copy()
    d["ds"] = pd.to_datetime(d["ds"])
    d = d[d["ds"] <= end]
    if not len(d):
        return None
    k = ((end - d["ds"]).dt.days // 7).to_numpy()
    n = int(k.max()) + 1
    s = np.bincount(k, d["y"].to_numpy(float), minlength=n)
    c = np.bincount(k, minlength=n)
    rate = np.where(c >= MIN_DAYS_IN_WEEK, s / np.maximum(c, 1), np.nan)
    # index 0 = oldest week
    return rate[::-1].copy(), c[::-1].copy(), end


def _level(rate, lo, hi):
    v = rate[max(lo, 0):max(hi, 0)]
    v = v[np.isfinite(v)]
    return float(np.median(v)) if len(v) >= 2 else None


def _runs(rate, count):
    """Candidate rush runs: (start, end) week indexes, inclusive. A week is flagged when it
    sells RUSH_RATIO x the median of the 12 weeks either side of it (the week next to it
    left out), so a rush shorter than a quarter can't drag its own yardstick up."""
    n = len(rate)
    flag = np.zeros(n, bool)
    for t in range(n):
        if not np.isfinite(rate[t]):
            continue
        v = np.concatenate([rate[max(0, t - 13):max(0, t - 1)], rate[t + 2:t + 14]])
        v = v[np.isfinite(v)]
        if len(v) < 4:
            continue
        b = float(np.median(v))
        flag[t] = b > 0 and rate[t] >= RUSH_RATIO * b
    out = []
    i = 0
    while i < n:
        if not flag[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and flag[j + 1]:
            j += 1
        if j - i + 1 >= RUSH_MIN_WEEKS:
            out.append((i, j))
        i = j + 1
    return out


def _ratio_at(rate, s, e, base_lo_hi=None):
    """Mean rate over weeks s..e against the normal around them."""
    if s < 0 or e >= len(rate):
        return None
    v = rate[s:e + 1]
    if np.isfinite(v).sum() < max(1, (e - s + 1) // 2):
        return None
    b = _level(np.concatenate([rate[max(0, s - RUSH_SIDE_WEEKS):max(0, s - 1)],
                               rate[e + 2:e + 2 + RUSH_SIDE_WEEKS]]), 0, 10 ** 6)
    if not b or b <= 0:
        return None
    return float(np.nanmean(v)) / b


def _last_year(rate, s, e):
    """How far above normal the same weeks usually are: for each of up to RUSH_YEARS past
    years, the best match within ±3 weeks; then the middle of those years. One unusually
    quiet or busy year can't decide it either way (the max over every year and shift
    would find a "peak" in pure noise). None when there is no year before."""
    per_year = []
    for yb in range(1, RUSH_YEARS + 1):
        best = None
        for sh in range(-YEAR_SLACK, YEAR_SLACK + 1):
            r = _ratio_at(rate, s - YEAR_WEEKS * yb + sh, e - YEAR_WEEKS * yb + sh)
            if r is not None:
                best = r if best is None else max(best, r)
        if best is not None:
            per_year.append(best)
    return float(np.median(per_year)) if per_year else None


def _peer_verdict(peers, start, end, last_day):
    """(one_off, last_ratio) from similar products, or (None, None) if they can't tell."""
    this_r, last_r = [], []
    for p in peers or []:
        if p is None or not len(p):
            continue
        w = _weekly(p, last_day)
        if w is None:
            continue
        rate = w[0]
        n = len(rate)
        # the same calendar weeks in the peer's own week numbering
        s = n - 1 - int((last_day - start).days // 7)
        e = n - 1 - int((last_day - end).days // 7)
        if s < 0 or e < s:
            continue
        r_now = _ratio_at(rate, s, e)
        r_last = _last_year(rate, s, e)
        if r_now is None or r_last is None:
            continue
        this_r.append(r_now)
        last_r.append(r_last)
    if len(this_r) < 2:
        return None, None
    now, last = float(np.median(this_r)), float(np.median(last_r))
    if last >= RUSH_TAIL + 0.1:           # they rush here every year: a season
        return False, last
    if now >= RUSH_TAIL + 0.1:            # they rushed now and not last year: a one-off
        return True, 1.0
    return None, None


def find(df, today, peers=None):
    """The one-off rushes in `df`, as a list of dicts:
       start, end (dates), weeks, ratio (how far above normal), lastYear (the same weeks a
       year earlier, 1.0 = normal), source ("own" / "similar products"), factors {date: m}
       (divide that day's units by m to take the rush out)."""
    if df is None or len(df) < 7 * (RUSH_MIN_WEEKS + RUSH_AFTER_WEEKS + 2):
        return []
    last_day = pd.to_datetime(df["ds"]).max()
    w = _weekly(df, last_day)
    if w is None:
        return []
    rate, count, _ = w
    n = len(rate)
    out = []
    for s, e in _runs(rate, count):
        # widen to the tail weeks still above normal
        base = _level(np.concatenate([rate[max(0, s - RUSH_SIDE_WEEKS):max(0, s - 1)],
                                      rate[e + 2:e + 2 + RUSH_SIDE_WEEKS]]), 0, 10 ** 6)
        if not base:
            continue
        while s > 0 and np.isfinite(rate[s - 1]) and rate[s - 1] >= RUSH_TAIL * base:
            s -= 1
        while e + 1 < n and np.isfinite(rate[e + 1]) and rate[e + 1] >= RUSH_TAIL * base:
            e += 1
        weeks = e - s + 1
        if weeks > RUSH_MAX_WEEKS:
            continue
        after = rate[e + 1:e + 1 + RUSH_AFTER_WEEKS]
        if len(after) < RUSH_AFTER_WEEKS or not np.all(np.isfinite(after)) or np.any(after >= RUSH_TAIL * base):
            continue                                    # not over yet
        before = rate[max(0, s - RUSH_SIDE_WEEKS):s]
        if np.isfinite(before).sum() < 2:
            continue                                    # nothing ordinary before it to compare with
        # more than chance?
        days = float(np.nansum(np.where(np.isfinite(rate[s:e + 1]), 1, 0)) * 7)
        exp_units = base * days
        got = float(np.nansum(rate[s:e + 1]) * 7)
        if got - exp_units < RUSH_Z * np.sqrt(RUSH_PHI * max(exp_units, 1.0)):
            continue
        ratio = got / max(exp_units, 1e-9)
        start = (last_day - pd.Timedelta(days=7 * (n - 1 - s) + 6)).normalize()
        end = (last_day - pd.Timedelta(days=7 * (n - 1 - e))).normalize()
        last = _last_year(rate, s, e)
        source = "own"
        if last is None:
            one_off, last = _peer_verdict(peers, start, end, last_day)
            source = "similar products"
            if not one_off:
                continue
        if ratio < RUSH_UNUSUAL * max(last, 1.0):
            continue                                    # it does this every year
        # Last year's same weeks within 30% of normal were normal weeks (searching ±3 weeks
        # always finds a little noise above 1).
        last = 1.0 if last < RUSH_LAST_NORMAL else last
        factors = {}
        for k in range(s, e + 1):
            if not np.isfinite(rate[k]):
                continue
            m = max(rate[k] / (base * last), 1.0)
            w_end = last_day - pd.Timedelta(days=7 * (n - 1 - k))
            for dd in range(7):
                factors[(w_end - pd.Timedelta(days=dd)).normalize()] = m
        out.append({"start": start.date().isoformat(), "end": end.date().isoformat(), "weeks": int(weeks),
                    "ratio": round(ratio, 2), "lastYear": round(last, 2), "source": source,
                    "units": round(got - exp_units * last, 1), "factors": factors})
    return out


def deflate(df, rushes):
    """`df` with each rush divided back out of its days."""
    if not rushes or df is None or not len(df):
        return df
    f = {}
    for r in rushes:
        f.update(r["factors"])
    d = df.copy()
    key = pd.to_datetime(d["ds"]).dt.normalize()
    m = key.map(f).fillna(1.0).to_numpy(float)
    if np.all(m == 1.0):
        return df
    d["y"] = d["y"].astype(float).to_numpy() / m
    return d


def explain(rushes):
    if not rushes:
        return None
    parts = []
    for r in rushes:
        a = pd.Timestamp(r["start"]).strftime("%b %-d, %Y")
        b = pd.Timestamp(r["end"]).strftime("%b %-d, %Y")
        why = ("It wasn't there the year before" if r["source"] == "own" else
               "Similar products rushed then too but not the year before")
        parts.append(f"{a} to {b}: about {r['ratio']:.1f}x its normal sales. {why}, so it's treated as a one-off")
    return ("One-off rush" + ("es" if len(rushes) > 1 else "") + " left out of the everyday level: "
            + "; ".join(parts) + ". The forecast starts from where sales are now.")

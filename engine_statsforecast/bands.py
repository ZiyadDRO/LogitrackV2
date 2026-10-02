"""
bands.py: the range for a TOTAL over several days (a week, a month).

One rule, used everywhere a range covers more than one day: the month cards, the weekly
Live accuracy log and its revisions. The daily band on the chart is a separate thing and
is not touched here.

WHY NOT ADD UP THE DAILY BANDS

Each day's band is an 80% range for that day. Adding the 30 lows and the 30 highs assumes
every day of the month misses by its full amount in the same direction. That almost never
happens, so the result was a "range" of 14 to 490 around a 215 forecast. Measured on 60
products at four past cutoffs each, 95-100% of real monthly totals landed inside it
against the 80% it claims, at about 240% of the forecast in width. It was always right
because it was too wide to say anything.

WHY NOT THE OPPOSITE (days independent)

Combining the daily half-widths in quadrature (the square root of the sum of squares)
assumes the days' misses are unrelated. They aren't: when a forecast's level is off, it's
off every day of the month. That version caught only 43-57% of monthly totals.

WHAT THIS DOES

A total's error has two parts, each measured from the product's own out-of-sample errors
(the holdout windows build_entry already runs to size the safety buffer):

  1. Day-to-day noise: the daily half-widths combined with the correlation between days
     that the holdout errors actually show (0 = unrelated, 1 = all move together).
  2. Level error: how far off the forecast's average level was in each holdout window,
     less the part day-to-day noise explains. It applies to every day of the total, so it
     grows with the number of days. Scaled to the forecast's own level, so a peak month
     isn't given a quiet month's error.

The two are combined as independent parts. Tested on the same 60 products: 7-day totals
75-79% inside, 30-day totals 75-81% inside, at about a third of the old width. Slow,
intermittent sellers still land below the range a little more often than they should.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np


def z_for(band_pct: float) -> float:
    """Two-sided band -> the upper z (80% -> 1.2816)."""
    p = min(max(float(band_pct), 1.0), 99.9) / 100.0
    return NormalDist().inv_cdf(0.5 + p / 2.0)


def _windows(oos):
    if oos is None:
        return []
    ws = oos if isinstance(oos, list) else [oos]
    return [np.asarray(w, dtype=float) for w in ws if w is not None and len(w) >= 7]


def day_correlation(oos, n: int) -> float:
    """Average correlation between the errors of days in an n-day block, from the holdout
    errors: Var(n-day sum) = n*s2*(1 + (n-1)*rho). 0 when it can't be measured."""
    ws = _windows(oos)
    n = int(n)
    if not ws or n < 2:
        return 0.0
    k = min(n, max(len(w) for w in ws) - 5)
    if k < 2:
        return 0.0
    sums = [np.convolve(w, np.ones(k), "valid") for w in ws if len(w) >= k + 5]
    if not sums:
        return 0.0
    s2 = float(np.var(np.concatenate(ws), ddof=1))
    vk = float(np.var(np.concatenate(sums), ddof=1))
    if not (s2 > 0 and np.isfinite(vk)):
        return 0.0
    return float(np.clip((vk / (k * s2) - 1.0) / (k - 1.0), 0.0, 1.0))


def level_error(oos) -> float:
    """Typical miss in the forecast's daily LEVEL (units/day), from each holdout window's
    mean error, less what day-to-day noise alone would give. 0 when it can't be measured."""
    ws = _windows(oos)
    if not ws:
        return 0.0
    s2 = float(np.var(np.concatenate(ws), ddof=1)) if sum(len(w) for w in ws) > 1 else 0.0
    means = np.array([float(w.mean()) for w in ws])
    length = float(np.mean([len(w) for w in ws]))
    v = float(np.mean(means ** 2)) - (s2 / length if length > 0 else 0.0)
    return float(math.sqrt(v)) if v > 0 and np.isfinite(v) else 0.0


def stats_for(entry: dict) -> dict:
    """What a total's range needs from a fitted entry, in a form that can be stored with a
    prediction (the weekly log keeps it so a later revision uses the same rule)."""
    oos = (entry or {}).get("oos_residuals")
    return {"levelSd": round(level_error(oos), 4),
            "refMean": (entry or {}).get("residual_ref_mean"),
            "oos": oos}


def total_band(point, lo_w, hi_w, *, level_sd=0.0, ref_mean=None, rho=0.0, band_pct=80.0,
               floor=0.0):
    """(low, high) for a total of len(lo_w) days.

    point      the total's forecast (sum of the daily forecasts)
    lo_w/hi_w  each day's distance from its forecast to its band's low / high
    level_sd   level_error() of the product, units/day
    ref_mean   the average daily sales over the holdout windows level_sd came from;
               the level error is scaled by (this total's daily forecast / ref_mean)
    rho        day_correlation() for this many days
    floor      the lowest the total can be (units already sold, for a month under way)
    """
    lo_w = np.clip(np.asarray(lo_w, dtype=float), 0.0, None)
    hi_w = np.clip(np.asarray(hi_w, dtype=float), 0.0, None)
    n = len(lo_w)
    if n == 0:
        return float(point), float(point)
    rho = float(min(max(rho or 0.0, 0.0), 1.0))

    def noise(w):
        a = float((w ** 2).sum())
        b = float(w.sum()) ** 2
        return math.sqrt(max(a + rho * (b - a), 0.0))

    scale = 1.0
    if ref_mean and ref_mean > 0 and point > 0:
        scale = min(max((float(point) / n) / float(ref_mean), 0.25), 4.0)
    lvl = z_for(band_pct) * n * float(level_sd or 0.0) * scale
    lo = float(point) - math.hypot(noise(lo_w), lvl)
    hi = float(point) + math.hypot(noise(hi_w), lvl)
    return max(float(floor), lo), max(float(point), hi)

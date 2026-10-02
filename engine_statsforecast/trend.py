"""
trend.py — taking growth out before a season is read.

A product selling more every month than the month before has, on a month-by-month
profile, "busier" late months, and any two growing products then look like they share a
season. Measured on products built to be unrelated but growing, the grouping score called
every such pair alike (0.97 against a 0.35 bar). The same mistake reaches every season this
tool borrows: a family's pooled yearly shape, and the monthly index a slow seller takes.

`detrend` divides the growth (or decline) out and leaves the average level where it was.

How much history that needs. Growth and season can only be told apart when a calendar
month has been seen in two different years: January 2025 against January 2026 is growth,
January against July is season. With that, the trend is fitted together with a level per
calendar month, so the months answer for the season and only year-on-year change counts
as trend. Without it (under about 13 months) the two can't be separated from the product's
own sales, and SHORT_MODE decides (see its note).
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

# Under ~13 months: "none" leaves the history as it is (a ramp can pass for a season),
# "linear" takes a straight-line trend out (a season rising toward the end of the history,
# like a first Christmas, is partly taken for growth). Measured: with "linear" a young
# Christmas product still joined its Christmas family 100% of the time, while a pure launch
# ramp stopped joining it at 200-250 days (100% -> 0-2%) and unrelated growing pairs at 300
# days matched 23% of the time instead of 72%. It was never worse than "none".
SHORT_MODE = os.environ.get("LOGITRACK_TREND_SHORT", "linear").strip().lower()
MIN_BUCKET_DAYS = 10         # a calendar month needs this many days to count
MIN_REPEATED_MONTHS = 2      # months seen in two different years, to separate trend from season
MAX_YEARLY = float(np.log(4.0))  # no more than 4x a year either way: past that it's a data problem


def slope(ds, y, *, short_mode=None):
    """(log growth per year, basis). basis: "seasonal-fit", "linear", or "none"."""
    short_mode = (short_mode or SHORT_MODE)
    d = pd.DatetimeIndex(pd.to_datetime(pd.Index(ds)))
    v = np.asarray(y, float)
    ok = np.isfinite(v)
    if ok.sum() < 60:
        return 0.0, "none"
    d, v = d[ok], v[ok]
    ym = (d.year.to_numpy() * 12 + d.month.to_numpy() - 1)
    ym = ym - ym.min()
    cnt = np.bincount(ym).astype(float)
    tot = np.bincount(ym, weights=v)
    keep = np.flatnonzero(cnt >= MIN_BUCKET_DAYS)
    if len(keep) < 4:
        return 0.0, "none"
    t0 = d.min()
    base_ym = t0.year * 12 + t0.month - 1
    # the middle of each calendar month, in years from the first day
    mid = np.array([((pd.Timestamp(year=(base_ym + k) // 12, month=(base_ym + k) % 12 + 1, day=15) - t0).days)
                    for k in keep], float) / 365.25
    mo = (base_ym + keep) % 12
    mean = np.maximum(tot[keep] / cnt[keep], 0.0)
    c = 0.1 * float(v.mean()) + 1e-6
    z = np.log(mean + c)
    w = np.sqrt(cnt[keep])
    reps = int((np.bincount(mo, minlength=12) >= 2).sum())
    if reps >= MIN_REPEATED_MONTHS:
        months = sorted(set(mo.tolist()))
        X = np.column_stack([mid] + [(mo == m).astype(float) for m in months])
        basis = "seasonal-fit"
    elif short_mode == "linear":
        X = np.column_stack([np.ones_like(mid), mid])
        basis = "linear"
    else:
        return 0.0, "none"
    beta, *_ = np.linalg.lstsq(X * w[:, None], z * w, rcond=None)
    b = float(beta[0] if basis == "seasonal-fit" else beta[1])
    return float(np.clip(b, -MAX_YEARLY, MAX_YEARLY)), basis


_MEMO: dict = {}


def factors(ds, y, *, short_mode=None):
    """Per-day trend factor (mean 1 over the days given), and the basis."""
    d = pd.to_datetime(pd.Index(ds))
    yv = np.asarray(y, float)
    try:
        key = (len(d), str(d[0]), str(d[-1]), float(np.nansum(yv)),
               float(np.nansum(yv * np.arange(len(yv)))), short_mode or SHORT_MODE)
    except IndexError:
        key = None
    if key is not None and key in _MEMO:
        b, basis = _MEMO[key]
    else:
        b, basis = slope(d, yv, short_mode=short_mode)
        if key is not None:
            if len(_MEMO) > 50000:
                _MEMO.clear()
            _MEMO[key] = (b, basis)
    if b == 0.0 or not len(d):
        return np.ones(len(d)), basis
    t = (d - d.min()).days.to_numpy(float) / 365.25
    f = np.exp(b * t)
    return f / float(np.mean(f)), basis


def detrend(df, *, date_col="ds", value_col="y", short_mode=None):
    """A copy of `df` with growth divided out of `value_col` (average level unchanged)."""
    if df is None or not len(df) or value_col not in df.columns:
        return df
    d = df.dropna(subset=[value_col])
    if not len(d):
        return df
    f, _ = factors(d[date_col], d[value_col].to_numpy(float), short_mode=short_mode)
    out = d.copy()
    out[value_col] = d[value_col].to_numpy(float) / f
    return out

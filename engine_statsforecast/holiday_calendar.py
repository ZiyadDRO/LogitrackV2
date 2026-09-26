"""
Holidays and promotional periods: which dates are special, and how much each one moves
a product's sales.

AUTOMATIC. Every product's history is checked against every period in the calendar
below (plus any sales periods the store adds). Nobody has to remember which holidays
matter: a holiday that moves a product shows up in its own numbers, one that doesn't
stays out. The calendar only says WHEN; how much a period moves sales is learned:

  1. Every past occurrence is measured: sales on those exact days against the ordinary
     days around them, with the weekday pattern and any discount taken out. Each year is
     kept, so the product page can show them one by one.
  2. Similar products fill in when a product has no past occurrence of its own.
  3. Checking two dozen periods on a year or two of sales turns up coincidences, so a
     period only counts when the evidence is clear: the product's own years well beyond
     chance, or similar products clearly showing it (combine). A clear lift is used at
     its measured size; an unclear one is left out of the forecast and covered by safety
     stock instead.
  4. Inside a window, days are not treated alike: the shape (Black Friday itself vs the
     Sunday after, the last days before Christmas vs the first) comes from past years
     too, pulled toward even by how little each day's own evidence is.

The models train with each applied lift divided back out of its days (see deflate), so a
spike never inflates the everyday level or the season, and each lift is multiplied back
in on its own future dates. Periods with no clear effect are left as recorded. Nothing
here ever changes recorded history.

Each day belongs to at most one period. When two overlap, a holiday's main day keeps it
first, then the shorter window keeps the rest, so no day is ever lifted twice.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import threading

import numpy as np
import pandas as pd

STORE_PATH = os.environ.get(
    "LOGITRACK_HOLIDAYS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "holiday_settings.json"))

# Ordinary days either side of a window used as its baseline.
BASELINE_DAYS = 28
# A window needs at least this many ordinary days around it to be measured at all...
MIN_BASELINE_DAYS = 14
# ...and at least this many on EACH side. A holiday is measured against the days before
# it and the days after it separately, and has to stand out from both (see measure).
MIN_SIDE_DAYS = 7
# When the ordinary level before and after a window agrees within STEP_LO, the window is
# measured against both together (the usual case: most accurate). Once they differ by
# STEP_HI or more, the level changed around the window and it is measured against the
# side it differs from least. In between, a smooth blend of the two, so a day more of
# sales can't flip the method.
STEP_LO, STEP_HI = 1.25, 1.6
# ...and enough expected sales in the window that the ratio isn't pure noise. Below this
# (a slow seller over a 3-day weekend) the product relies on its peers instead.
MIN_EXPECTED_UNITS = 3.0
# Peers needed before their typical effect is borrowed, and before their agreement can
# count as proof. Two products agreeing by chance is too easy.
MIN_POOL_PEERS = 2
MIN_CLEAR_PEERS = 3
# Lifts are held inside these. A 4x holiday exists; 20x is a data problem.
MIN_MULT, MAX_MULT = 0.25, 4.0
# Effects smaller than this are treated as none, so every order isn't nudged 2% on noise.
MIN_EFFECT = 0.05
# How much a holiday's effect moves from one year to the next (log scale: 0.2 is about
# +/-20%), on top of the counting noise of the sales themselves.
YEAR_SCATTER = 0.20
# Products in a category never react to a holiday exactly alike: at least this much
# (log scale) of difference between them is assumed, however similar the peers look.
BETWEEN_SD_FLOOR = 0.10
# When a product's own past years count as proof on their own: the average lift must be
# at least this many standard errors from zero. Every product is checked against two
# dozen periods, so the bar is high enough that chance alone passes about once in a
# hundred checks. A period the store entered itself needs less: someone knew it mattered.
OWN_CLEAR_Z = 2.5
OWN_CLEAR_Z_CUSTOM = 2.0
# ...and when similar products together count as proof (several products agreeing is
# much harder to get by chance than one).
POOL_CLEAR_Z = 2.0
# Day-by-day shape inside a window: a day's own pattern counts in full once it has about
# this many dispersion-adjusted units of expected sales behind it; below that it leans
# toward an even spread across the window.
SHAPE_K = 6.0

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

# ── The calendar ─────────────────────────────────────────────────────────────────────
# (key, name, group, rule, (days before, days after) the anchor, short description)
# Rules: ("fixed", month, day) · ("nth", month, weekday, n) · ("last", month, weekday)
#        ("thanksgiving", offset) · ("easter",) · ("superbowl",) · ("table",)
GROUPS = ["Big retail", "Seasonal", "Long weekends", "Online", "Other"]

CATALOG = [
    ("thanksgiving", "Thanksgiving Day", "Big retail", ("thanksgiving", 0), (0, 0), "The Thursday itself. Often quiet in stores."),
    ("black_friday", "Black Friday weekend", "Big retail", ("thanksgiving", 1), (0, 2), "Friday to Sunday after Thanksgiving."),
    ("cyber_monday", "Cyber Monday", "Big retail", ("thanksgiving", 4), (0, 0), "The Monday after Thanksgiving."),
    ("christmas_rush", "Christmas rush", "Big retail", ("fixed", 12, 24), (12, 0), "December 12 to 24."),
    ("christmas_day", "Christmas Day", "Big retail", ("fixed", 12, 25), (0, 0), "December 25. Often closed or quiet."),
    ("after_christmas", "After-Christmas sales", "Big retail", ("fixed", 12, 26), (0, 5), "December 26 to 31."),
    ("new_year", "New Year's Day", "Big retail", ("fixed", 1, 1), (0, 0), "January 1."),
    ("valentines", "Valentine's Day", "Seasonal", ("fixed", 2, 14), (7, 0), "The week up to February 14."),
    ("st_patricks", "St. Patrick's Day", "Seasonal", ("fixed", 3, 17), (3, 0), "March 14 to 17."),
    ("easter", "Easter", "Seasonal", ("easter",), (6, 0), "The week up to Easter Sunday."),
    ("mothers_day", "Mother's Day", "Seasonal", ("nth", 5, SUN, 2), (6, 0), "The week up to the second Sunday in May."),
    ("fathers_day", "Father's Day", "Seasonal", ("nth", 6, SUN, 3), (6, 0), "The week up to the third Sunday in June."),
    ("back_to_school", "Back to school", "Seasonal", ("fixed", 7, 20), (0, 31), "July 20 to August 20."),
    ("halloween", "Halloween", "Seasonal", ("fixed", 10, 31), (7, 0), "The week up to October 31."),
    ("presidents_day", "Presidents' Day weekend", "Long weekends", ("nth", 2, MON, 3), (2, 0), "Saturday to Monday."),
    ("memorial_day", "Memorial Day weekend", "Long weekends", ("last", 5, MON), (2, 0), "Saturday to Monday."),
    ("july_4", "Fourth of July", "Long weekends", ("fixed", 7, 4), (3, 0), "July 1 to 4."),
    ("labor_day", "Labor Day weekend", "Long weekends", ("nth", 9, MON, 1), (2, 0), "Saturday to Monday."),
    ("prime_day", "Prime Day", "Online", ("table",), (0, 0), "Amazon's summer sale. Dates change each year."),
    ("prime_october", "Prime Big Deal Days", "Online", ("table",), (0, 0), "Amazon's October sale. Dates change each year."),
    ("super_bowl", "Super Bowl weekend", "Other", ("superbowl",), (2, 0), "Friday to game day Sunday."),
    ("tax_refund", "Tax refund season", "Other", ("fixed", 2, 15), (0, 28), "Mid-February to mid-March."),
]
_BY_KEY = {c[0]: c for c in CATALOG}

# Amazon announces these a few weeks ahead. Past dates are fixed; new ones can be added
# in Settings (stored under "dates"), which is also how a year not listed here gets one.
TABLE_DATES = {
    "prime_day": [
        ("2015-07-15", "2015-07-15"), ("2016-07-12", "2016-07-12"), ("2017-07-11", "2017-07-12"),
        ("2018-07-16", "2018-07-17"), ("2019-07-15", "2019-07-16"), ("2020-10-13", "2020-10-14"),
        ("2021-06-21", "2021-06-22"), ("2022-07-12", "2022-07-13"), ("2023-07-11", "2023-07-12"),
        ("2024-07-16", "2024-07-17"), ("2025-07-08", "2025-07-11"), ("2026-06-23", "2026-06-26"),
    ],
    "prime_october": [
        ("2022-10-11", "2022-10-12"), ("2023-10-10", "2023-10-11"), ("2024-10-08", "2024-10-09"),
        ("2025-10-07", "2025-10-08"), ("2026-10-06", "2026-10-07"),
    ],
}

# ── Date rules ───────────────────────────────────────────────────────────────────────
def _nth(year, month, weekday, n):
    d = _dt.date(year, month, 1)
    d += _dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + _dt.timedelta(weeks=n - 1)


def _last(year, month, weekday):
    nxt = _dt.date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - _dt.timedelta(days=1)
    return d - _dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    a = year % 19; b, c = divmod(year, 100); d, e = divmod(b, 4)
    f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return _dt.date(year, month, day + 1)


def _anchor(rule, year):
    kind = rule[0]
    if kind == "fixed":
        return _dt.date(year, rule[1], rule[2])
    if kind == "nth":
        return _nth(year, rule[1], rule[2], rule[3])
    if kind == "last":
        return _last(year, rule[1], rule[2])
    if kind == "thanksgiving":
        return _nth(year, 11, THU, 4) + _dt.timedelta(days=rule[1])
    if kind == "easter":
        return _easter(year)
    if kind == "superbowl":
        # First Sunday of February until 2021; the second since the 17-game season (2022).
        return _nth(year, 2, SUN, 2 if year >= 2022 else 1)
    return None


def _d(x):
    return x if isinstance(x, _dt.date) and not isinstance(x, _dt.datetime) else pd.Timestamp(x).date()


def windows_for(key, start, end, settings=None):
    """Every (first day, last day) of `key` that touches [start, end], as dates."""
    start, end = _d(start), _d(end)
    settings = settings or {}
    out = []
    if key.startswith("custom:"):
        c = next((c for c in settings.get("custom") or [] if c.get("id") == key), None)
        if not c:
            return []
        for s, e in c.get("ranges") or []:
            s, e = _d(s), _d(e)
            if c.get("yearly"):
                for y in range(start.year - 1, end.year + 2):
                    try:
                        ys = s.replace(year=y)
                        ye = ys + (e - s)
                    except ValueError:            # February 29 in a non-leap year
                        continue
                    out.append((ys, ye))
            else:
                out.append((s, e))
    elif key in _BY_KEY:
        rule, (before, after) = _BY_KEY[key][3], _BY_KEY[key][4]
        if rule[0] == "table":
            rows = list(TABLE_DATES.get(key, [])) + [tuple(r) for r in (settings.get("dates") or {}).get(key, [])]
            seen = set()
            for s, e in rows:
                s, e = _d(s), _d(e)
                if (s, e) not in seen:
                    seen.add((s, e)); out.append((s, e))
        else:
            for y in range(start.year - 1, end.year + 2):
                a = _anchor(rule, y)
                out.append((a - _dt.timedelta(days=before), a + _dt.timedelta(days=after)))
    return sorted({(s, e) for s, e in out if e >= start and s <= end and e >= s})


def selected_keys(settings):
    """Every period checked: the whole calendar plus the store's own periods, minus any
    the store switched off for this product (settings["exclude"], see for_sku)."""
    settings = settings or {}
    excl = set(settings.get("exclude") or [])
    keys = [c[0] for c in CATALOG if c[0] not in excl]
    keys += [c["id"] for c in settings.get("custom") or []
             if c.get("id") and c.get("enabled", True) and c["id"] not in excl]
    return keys


def for_sku(settings, sku):
    """The store's settings as they apply to one product (its switched-off periods)."""
    settings = dict(settings or {})
    ign = ((settings.get("ignored") or {}).get(str(sku)) or []) if sku is not None else []
    settings["exclude"] = sorted(ign)
    settings.pop("ignored", None)
    return settings


def is_custom(key):
    return str(key).startswith("custom:")


# Long seasons with no single "main day".
_NO_ANCHOR = {"back_to_school", "tax_refund"}


def _main_day(key, s, e):
    """The day a holiday is named after (Valentine's is the 14th, not the week before)."""
    if key not in _BY_KEY or key in _NO_ANCHOR or _BY_KEY[key][3][0] == "table":
        return None
    return e if _BY_KEY[key][4][0] > 0 else s


class AssignMap(dict):
    """{date: key}, plus .offsets {date: (day number inside its window, window length)}
    so a day can be matched to the same day of the window in other years."""
    offsets: dict

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.offsets = {}


def assign_days(start, end, settings):
    """{date: key} for every day in [start, end] inside a picked window.

    A day is never counted under two holidays. When picked windows overlap, a holiday's
    main day keeps it first (February 14 stays Valentine's even inside Presidents' Day
    weekend), then the shorter window keeps the rest."""
    keys = selected_keys(settings)
    wins = []
    for k in keys:
        for s, e in windows_for(k, start, end, settings):
            wins.append(((e - s).days, s, e, k))
    wins.sort(key=lambda w: (w[0], w[1]))
    out = AssignMap()
    lo, hi = _d(start), _d(end)
    for _, s, e, k in wins:
        m = _main_day(k, s, e)
        if m is not None and lo <= m <= hi and m not in out:
            out[m] = k
    for _, s, e, k in wins:
        d = s
        while d <= e:
            if lo <= d <= hi and d not in out:
                out[d] = k
            d += _dt.timedelta(days=1)
    for _, s, e, k in wins:
        d = s
        while d <= e:
            if out.get(d) == k and d not in out.offsets:
                out.offsets[d] = ((d - s).days, (e - s).days + 1)
            d += _dt.timedelta(days=1)
    return out


def occurrences(assign, key):
    """Runs of consecutive days assigned to `key`: [(first, last), ...]."""
    days = sorted(d for d, k in assign.items() if k == key)
    runs, cur = [], None
    for d in days:
        if cur and (d - cur[1]).days == 1:
            cur = (cur[0], d)
        else:
            if cur:
                runs.append(cur)
            cur = (d, d)
    if cur:
        runs.append(cur)
    return runs


def name_of(key, settings=None):
    if key in _BY_KEY:
        return _BY_KEY[key][1]
    c = next((c for c in (settings or {}).get("custom") or [] if c.get("id") == key), None)
    return (c or {}).get("name") or key


# ── Settings store ───────────────────────────────────────────────────────────────────
class HolidaySettings:
    """The store's holiday settings: its own sales periods, extra Prime Day dates, and
    periods switched off for particular products."""

    def __init__(self, path=None):
        self.path = path or STORE_PATH
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self):
        with self._lock:
            return json.loads(json.dumps(self._data))

    def version(self):
        return json.dumps(self.get(), sort_keys=True)

    def save(self, data):
        clean = validate(data)
        with self._lock:
            self._data = clean
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(clean, f, indent=1)
            os.replace(tmp, self.path)
        return clean

    def purge(self):
        with self._lock:
            self._data = {}
            try:
                os.remove(self.path)
            except OSError:
                pass


class SettingsError(ValueError):
    pass


def _slug(s):
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(s)).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:40] or "period"


def validate(data):
    """Clean a settings payload, or raise SettingsError with a message for the user.
    Older settings (a store type and a list of picked holidays) are simply dropped: every
    period is now checked automatically."""
    data = data if isinstance(data, dict) else {}
    custom, ids = [], set()
    for c in data.get("custom") or []:
        name = str((c or {}).get("name") or "").strip()
        if not name:
            raise SettingsError("Each sales period needs a name.")
        ranges = []
        for r in c.get("ranges") or []:
            try:
                s, e = _d(r[0]), _d(r[1])
            except Exception:                                 # noqa: BLE001
                raise SettingsError(f"'{name}' has a date that isn't a real date.")
            if e < s:
                raise SettingsError(f"'{name}' ends before it starts.")
            if (e - s).days > 62:
                raise SettingsError(f"'{name}' is longer than two months. Use shorter periods.")
            ranges.append([s.isoformat(), e.isoformat()])
        if not ranges:
            raise SettingsError(f"'{name}' needs at least one date range.")
        cid = c.get("id") if str(c.get("id") or "").startswith("custom:") else "custom:" + _slug(name)
        base, n = cid, 2
        while cid in ids:
            cid = f"{base}-{n}"; n += 1
        ids.add(cid)
        custom.append({"id": cid, "name": name[:60], "ranges": ranges, "yearly": bool(c.get("yearly")),
                       "enabled": bool(c.get("enabled", True))})
    dates = {}
    for k, rows in (data.get("dates") or {}).items():
        if k not in TABLE_DATES:
            raise SettingsError("Dates can only be added for Prime Day and Prime Big Deal Days.")
        out = []
        for r in rows or []:
            try:
                s, e = _d(r[0]), _d(r[1])
            except Exception:                                 # noqa: BLE001
                raise SettingsError(f"{name_of(k)} has a date that isn't a real date.")
            if e < s or (e - s).days > 7:
                raise SettingsError(f"{name_of(k)} dates must be a range of a week or less.")
            out.append([s.isoformat(), e.isoformat()])
        if out:
            dates[k] = out
    known = set(_BY_KEY) | ids
    ignored = {}
    for sku, keys in (data.get("ignored") or {}).items():
        keep = []
        for k in keys or []:
            if k not in known:
                raise SettingsError(f"Unknown holiday: {k}.")
            if k not in keep:
                keep.append(k)
        if keep:
            ignored[str(sku)] = keep
    return {"custom": custom, "dates": dates, "ignored": ignored}


# ── Learning the effect ──────────────────────────────────────────────────────────────
def _weekday_index(ds, y, clean):
    """Mean-1 weekday profile from ordinary days, shrunk toward flat for slow sellers
    (a Tuesday with two sales in a year isn't a pattern)."""
    wd = ds.dt.weekday.to_numpy()
    yy = y.to_numpy(float)
    m = clean
    if m.sum() < 28 or yy[m].mean() <= 0:
        return np.ones(7)
    mu = yy[m].mean()
    idx = np.ones(7)
    for i in range(7):
        v = yy[m & (wd == i)]
        if len(v) >= 4:
            n = v.sum()
            raw = v.mean() / mu
            idx[i] = 1 + (raw - 1) * n / (n + 20.0)
    return idx / idx.mean()


def _discount_mask(df):
    m = np.zeros(len(df), bool)
    if "on_promotion" in df.columns:
        m |= pd.to_numeric(df["on_promotion"], errors="coerce").fillna(0).to_numpy(float) >= 1
    if "price" in df.columns and "price_listed" in df.columns:
        p = pd.to_numeric(df["price"], errors="coerce").to_numpy(float)
        pl = pd.to_numeric(df["price_listed"], errors="coerce").to_numpy(float)
        with np.errstate(invalid="ignore"):
            m |= np.nan_to_num(pl, nan=0) > np.nan_to_num(p, nan=0) * 1.005
    return m


def _dispersion(y, wdi, clean):
    """How much noisier this product's daily sales are than pure chance (variance over
    mean around its local level; 1 = Poisson). Real retail days are usually 1.5 to 4. A
    holiday's measured lift is only as trustworthy as this allows."""
    m = clean & np.isfinite(y)
    if m.sum() < 60:
        return 2.0
    z = pd.Series(np.where(m, y / wdi, np.nan))
    lvl = z.rolling(29, center=True, min_periods=14).mean().to_numpy() * wdi
    ok = m & np.isfinite(lvl) & (lvl > 0)
    if ok.sum() < 60 or lvl[ok].sum() <= 0:
        return 2.0
    return float(np.clip(((y[ok] - lvl[ok]) ** 2).sum() / lvl[ok].sum(), 1.0, 20.0))


def measure(df, assign, *, elasticity=0.0, ref_price=None):
    """Each picked holiday's past occurrences in `df`, measured against the days around.

    Returns {key: {"logs": [log lift per occurrence], "expected": [...], "skipped": n}}.
    Discounts are taken out: with a measured price response each day is restated at
    `ref_price`; without one, discounted days are left out, and an occurrence that was
    discounted throughout can't be separated from its discount and is skipped.
    """
    out = {}
    if df is None or not len(df) or not assign:
        return out
    d = df[["ds", "y"] + [c for c in ("price", "price_listed", "on_promotion", "price_mixed")
                          if c in df.columns]].copy()
    d = d.sort_values("ds").reset_index(drop=True)
    dates = d["ds"].dt.date.to_numpy()
    hol = np.array([assign.get(x) for x in dates], dtype=object)
    in_hol = np.array([h is not None for h in hol])
    y = d["y"].astype(float).to_numpy()
    disc = _discount_mask(d)
    usable = np.ones(len(d), bool)
    el = float(elasticity or 0.0)
    if el < 0 and ref_price and "price" in d.columns:
        p = pd.to_numeric(d["price"], errors="coerce").to_numpy(float)
        ok = np.isfinite(p) & (p > 0)
        adj = np.where(ok, (float(ref_price) / np.where(ok, p, 1.0)) ** el, 1.0)
        y = y * np.clip(adj, 0.5, 2.0)
        # A day restated from a price far outside what was seen is a guess; drop it.
        usable &= ~(ok & ((adj < 0.5) | (adj > 2.0)))
    else:
        usable &= ~disc
    wk = _weekday_index(d["ds"], pd.Series(y), usable & ~in_hol)
    wdi = wk[d["ds"].dt.weekday.to_numpy()]
    phi = _dispersion(y, wdi, usable & ~in_hol)
    # Retail counts get noisier the more that sells (variance = mean + mean^2 / k), so a
    # busy holiday day is noisier than an ordinary one. k is backed out of the ordinary
    # days' dispersion; a holiday's noise is then judged at its own, higher, level.
    _clean = usable & ~in_hol
    _lvl = float(np.mean(y[_clean])) if _clean.any() else 0.0
    k_nb = (_lvl / (phi - 1.0)) if (phi > 1.05 and _lvl > 0) else 1e9
    pos = {x: i for i, x in enumerate(dates)}
    keys = sorted({h for h in hol if h is not None})
    for k in keys:
        rec = {"logs": [], "expected": [], "base": [], "ends": [], "starts": [], "actual": [], "days": [],
               "skipped": 0, "phi": phi, "k": k_nb}
        pending = []
        for s, e in occurrences(assign, k):
            idx = [pos[x] for x in pd.date_range(s, e).date if x in pos]
            if not idx:
                continue
            span = (e - s).days + 1
            if len(idx) < max(1, math.ceil(0.6 * span)):
                continue                                      # only partly on file
            w = [i for i in idx if usable[i]]
            if not w:
                rec["skipped"] += 1
                continue
            # MEASURED AGAINST BOTH SIDES, AND IT HAS TO STAND OUT FROM BOTH.
            # Against the average of the weeks either side, a store whose everyday level
            # jumped or dropped right beside the window (a big event ending, a launch, a
            # new location) reads that step as a holiday: in a real store the weeks after
            # a World Cup made "Back to school" look like -75% on nearly every product. A
            # real holiday stands out from the days before it AND the days after it; a step
            # only from one side. So when the two sides' levels clearly differ, the change
            # is taken against the side it is smaller against (see STEP_LO/STEP_HI), and a
            # period without enough ordinary days on both sides isn't measured yet.
            sides = []
            for sgn in (-1, 1):
                b = []
                for off in range(1, BASELINE_DAYS + 1):
                    x = (s - _dt.timedelta(days=off)) if sgn < 0 else (e + _dt.timedelta(days=off))
                    i = pos.get(x)
                    if i is not None and usable[i] and not in_hol[i]:
                        b.append(i)
                sides.append(b)
            if min(len(b) for b in sides) < MIN_SIDE_DAYS or sum(len(b) for b in sides) < MIN_BASELINE_DAYS:
                continue
            actual = float(y[w].sum())
            wsum = float(wdi[w].sum())
            both = sides[0] + sides[1]
            r_b = float(np.mean(y[sides[0]] / wdi[sides[0]]))
            r_a = float(np.mean(y[sides[1]] / wdi[sides[1]]))
            pending.append({"s": s, "e": e, "w": w, "actual": actual, "wsum": wsum, "both": both,
                            "sides": sides, "r_b": r_b, "r_a": r_a,
                            "r_all": float(np.mean(y[both] / wdi[both])),
                            "step": math.log((r_b + 0.05) / (r_a + 0.05))})
        # A DIFFERENCE THAT COMES BACK EVERY YEAR IS THE SEASON. Sales before and after a
        # window can differ because the store's level changed around it once (the World
        # Cup ending just before Back to school) or because the season turns there every
        # year (quiet November into busy December around Black Friday). Only the first is
        # a reason to distrust the average of both sides; treating the second the same way
        # measured a toy store's +94% Black Friday at about +30%. So each year's step is
        # judged against a typical year's (the median, or with two years the more ordinary
        # of the two); with a single year there's nothing to compare, and the whole step
        # counts as unusual.
        steps = [p_["step"] for p_ in pending]
        if len(steps) >= 3:
            ref = float(np.median(steps))
        elif len(steps) == 2:
            ref = min(steps, key=abs)
        else:
            ref = 0.0
        offs = getattr(assign, "offsets", {})
        for p_ in pending:
            s, e, w, actual, wsum, both = p_["s"], p_["e"], p_["w"], p_["actual"], p_["wsum"], p_["both"]
            best = None
            for b_ in p_["sides"]:
                r_ = float(np.mean(y[b_] / wdi[b_]))
                lg_ = math.log((actual + 1.0) / (r_ * wsum + 1.0))
                if best is None or abs(lg_) < abs(best[1]):
                    best = (r_, lg_, r_ * float(wdi[b_].sum()))
            step = abs(p_["step"] - ref)
            wt = min(1.0, max(0.0, (step - math.log(STEP_LO)) / (math.log(STEP_HI) - math.log(STEP_LO))))
            r_all = p_["r_all"]
            rate = math.exp((1 - wt) * math.log(max(r_all, 1e-9)) + wt * math.log(max(best[0], 1e-9)))
            expected = rate * wsum
            base_units = (1 - wt) * r_all * float(wdi[both].sum()) + wt * best[2]
            if expected < MIN_EXPECTED_UNITS:
                continue
            # log of a noisy ratio runs low on average (a 30% noisy count loses ~4%), which
            # would read every quiet holiday as a small dip. Add the expected shortfall back.
            rec["logs"].append(math.log((actual + 1.0) / (expected + 1.0))
                               + min(0.2, 0.5 * phi / (expected + 1.0)))
            rec["expected"].append(expected)
            rec["base"].append(base_units)
            rec["actual"].append(actual)
            rec["starts"].append(s.isoformat())
            rec["ends"].append((e + _dt.timedelta(days=BASELINE_DAYS)).isoformat())
            rec["days"].append([(offs.get(dates[i], (None, None))[0], offs.get(dates[i], (None, None))[1],
                                 float(y[i]), rate * float(wdi[i])) for i in w])
        if rec["logs"] or rec["skipped"]:
            out[k] = rec
    return out


def applied_assign(assign, effects):
    """The day map restricted to periods that actually move this product."""
    keep = {k for k, e in (effects or {}).items() if e.get("applied")}
    out = AssignMap({d: k for d, k in (assign or {}).items() if k in keep})
    out.offsets = {d: o for d, o in getattr(assign, "offsets", {}).items() if d in out}
    return out


def deflate(df, assign, effects):
    """The frame with each applied holiday's lift divided back out of its days: what the
    models train on.

    Left in, a Black Friday spike is smeared into November and the everyday level. Cut
    out, it leaves a hole the yearly curve can't bridge (at the top of a December peak it
    undershoots badly). Divided out, the day keeps its real ups and downs, so the noise
    the safety stock is sized from stays honest, and the same lift (with its day-by-day
    shape) is multiplied back in on the future dates. Periods with no clear effect are
    left exactly as recorded: they are ordinary days."""
    if df is None or not len(df) or not assign or not effects:
        return df
    d = df.sort_values("ds").reset_index(drop=True)
    dates = d["ds"].dt.date.tolist()
    mult, _, _ = future_multipliers(dates, {k: e for k, e in effects.items() if e.get("applied")}, assign)
    m = np.asarray(mult, dtype=float)
    if np.all(np.abs(m - 1.0) < 1e-12):
        return df
    res = d.copy()
    res["y"] = d["y"].astype(float).to_numpy() / np.where(m > 0, m, 1.0)
    res["holiday_deflated"] = np.abs(m - 1.0) > 1e-12
    return res


def own_effect(rec):
    """(log effect, occurrences, precision) from one product's own measurements.

    Each past year counts by how much it can be trusted: a spike measured on hundreds of
    units says more than one measured on four. Its variance is the year-to-year scatter of
    a holiday's effect plus the noise of its sales (dispersion / expected units)."""
    logs = (rec or {}).get("logs") or []
    exp_ = (rec or {}).get("expected") or [None] * len(logs)
    n = len(logs)
    if not n:
        return 0.0, 0, 0.0
    phi = float((rec or {}).get("phi") or 1.0)
    k_nb = (rec or {}).get("k")
    acts = (rec or {}).get("actual") or [None] * n
    days = (rec or {}).get("days") or [None] * n
    bases = (rec or {}).get("base") or [None] * n
    noise = []
    for e, a, dd, bu in zip(exp_, acts, days, bases):
        e = max(float(e or MIN_EXPECTED_UNITS), 1.0)
        if k_nb and a is not None and dd:
            per_day = max(float(a), e) / max(len(dd), 1)
            noise.append((1.0 + per_day / float(k_nb)) / e)
        else:
            noise.append(phi / e)
        # The ordinary level it's compared with is itself counted from a few weeks of
        # sales. For a month-long window that's as uncertain as the window.
        if bu:
            noise[-1] += phi / max(float(bu), 1.0)
    scatter2 = YEAR_SCATTER ** 2
    if n >= 2:
        # How consistent its own years actually were, beyond their sales noise, counted
        # alongside the typical scatter as if that were two years of evidence. Years that
        # agree make the lift more certain; years that disagree make it less.
        obs = max(0.0, float(np.var(logs, ddof=1)) - float(np.mean(noise)))
        scatter2 = (2 * YEAR_SCATTER ** 2 + (n - 1) * obs) / (2 + n - 1)
    w = [1.0 / (scatter2 + v) for v in noise]
    prec = float(sum(w))
    mean = float(sum(wi * li for wi, li in zip(w, logs)) / prec)
    return mean, n, prec


def _day_ratios(rec):
    """{(length, day number): (actual, expected, occurrences)} summed over past years."""
    acc = {}
    for occ in (rec or {}).get("days") or []:
        for off, ln, a, e in occ or []:
            if off is None:
                continue
            k = (ln, off)
            aa, ee, nn = acc.get(k, (0.0, 0.0, 0))
            acc[k] = (aa + a, ee + e, nn + 1)
    return acc


def shape(own_rec, peer_recs=()):
    """How the lift is spread across the days of the window: one factor per day, averaging
    1 over the window's ordinary sales (so the window's total lift is unchanged).

    Each day's own pattern (its sales against an ordinary day, relative to the window as
    a whole) counts in proportion to how many sales stand behind it; the rest comes from
    similar products' pattern for that day, or an even spread when there is none. Returns
    None for single-day windows and when window lengths differ between years."""
    own = _day_ratios(own_rec)
    lengths = {ln for ln, _ in own}
    if not lengths:
        peer = [_day_ratios(r) for r in peer_recs if r]
        lengths = {ln for pr in peer for ln, _ in pr}
    if len(lengths) != 1:
        return None
    L = lengths.pop()
    if not L or L < 2:
        return None
    phi = float((own_rec or {}).get("phi") or 2.0)

    def rel(acc, k_nb):
        """{day: (log ratio vs the window, expected units, noise variance)}."""
        A = sum(a for (ln, _), (a, e, n) in acc.items() if ln == L)
        E = sum(e for (ln, _), (a, e, n) in acc.items() if ln == L)
        if A <= 0 or E <= 0:
            return {}
        base = math.log((A + 0.5) / (E + 0.5))
        return {off: (math.log((a + 0.5) / (e + 0.5)) - base, e,
                      (1.0 + max(a, e) / max(n, 1) / k_nb) / max(e, 0.5))
                for (ln, off), (a, e, n) in acc.items() if ln == L}

    own_rel = rel(own, float((own_rec or {}).get("k") or 1e9))
    peer_acc, peer_k = {}, []
    for r in peer_recs or []:
        if r:
            peer_k.append(float(r.get("k") or 1e9))
        for (ln, off), (a, e, nn) in _day_ratios(r).items():
            aa, ee, cc = peer_acc.get((ln, off), (0.0, 0.0, 0))
            peer_acc[(ln, off)] = (aa + a, ee + e, cc + nn)
    peer_rel = (rel(peer_acc, float(np.median(peer_k)) if peer_k else 1e9)
                if sum(1 for r in peer_recs or [] if r) >= MIN_POOL_PEERS else {})

    def shrink(rel_, _unused=None):
        """Each day's pattern kept in proportion to how much of the day-to-day spread is
        real rather than noise (days that only differ by chance collapse to even)."""
        if not rel_:
            return {}
        v = {off: nv for off, (lg, e, nv) in rel_.items()}
        lg = {off: x for off, (x, e, nv) in rel_.items()}
        wts_ = {off: 1.0 / v[off] for off in rel_}
        mean = sum(wts_[o] * lg[o] for o in rel_) / sum(wts_.values())
        # Keep a shape only when the days differ by clearly more than chance would make
        # them (a 95% chi-square test on the day-to-day differences); otherwise even.
        q = sum(wts_[o] * (lg[o] - mean) ** 2 for o in rel_)
        dfree = len(rel_) - 1
        if dfree < 1:
            return {}
        crit = dfree * (1 - 2 / (9 * dfree) + 1.645 * math.sqrt(2 / (9 * dfree))) ** 3
        if q <= crit:
            return {}
        spread = sum(wts_[o] * (lg[o] - mean) ** 2 for o in rel_) / sum(wts_.values())
        tau2 = max(0.0, spread - sum(v.values()) / len(v))
        return {off: (tau2 / (tau2 + v[off])) * (lg[off] - mean) for off in rel_}

    prior = shrink(peer_rel)
    mine = shrink(own_rel)
    g, wts = [], []
    for off in range(L):
        p0 = prior.get(off, 0.0)
        if off in own_rel:
            e = own_rel[off][1]
            # Its own pattern where it has plenty of sales that day; the peers' otherwise.
            w = e / (e + SHAPE_K * phi)
            g.append(w * mine.get(off, 0.0) + (1 - w) * p0); wts.append(e)
        else:
            g.append(p0); wts.append(None)
    fill = (sum(w for w in wts if w) / max(1, sum(1 for w in wts if w))) if any(wts) else 1.0
    wts = [w if w else fill for w in wts]
    f = [math.exp(x) for x in g]
    norm = sum(w * x for w, x in zip(wts, f)) / sum(wts)
    f = [x / norm for x in f]
    if max(abs(x - 1) for x in f) < 0.03:
        return None
    return [round(x, 4) for x in f]


def combine(own_rec, pool, custom=False):
    """One product's lift for one period, from its own past years and similar products.

      · Similar products clearly show it: the product's own years and the category's
        typical lift are combined, each counting by how precise it is. A product with
        plenty of its own clear history keeps its own number; one with little leans on
        the category; one with none takes the category's.
      · Only its own years show it, clearly (or they clearly differ from the category's):
        used as measured, not talked down.
      · Neither is clear: not applied. The forecast treats those days as ordinary, and the
        chance that there is something (as big as the past years were, up to one standard
        error) goes into the safety stock instead.

    Returns a dict with the lift, whether it's applied and where it came from, or None
    when there's nothing to go on."""
    l_own, n, prec = own_effect(own_rec)
    if n == 0 and not pool:
        return None
    se_own = 1.0 / math.sqrt(prec) if n else None
    z_bar = OWN_CLEAR_Z_CUSTOM if custom else OWN_CLEAR_Z
    own_clear = bool(n) and abs(l_own) / se_own >= z_bar
    pool_clear = bool(pool and pool.get("clear"))
    # Its own years clearly say something different from the category: it's simply not
    # like its peers on this date, so its own number stands.
    differs = (pool_clear and own_clear and
               abs(l_own - pool["log"]) / math.sqrt(se_own ** 2 + pool["sd"] ** 2) >= 2.0)
    if pool_clear and not differs:
        v_pool = pool["sd"] ** 2
        if n:
            w_o, w_p = 1.0 / se_own ** 2, 1.0 / v_pool
            log = (w_o * l_own + w_p * pool["log"]) / (w_o + w_p)
            se = 1.0 / math.sqrt(w_o + w_p)
        else:
            log, se = pool["log"], pool["sd"]
        applied = abs(log) >= MIN_EFFECT
    elif own_clear:
        log, se, applied = l_own, se_own, abs(l_own) >= MIN_EFFECT
    else:
        log = 0.0
        se = min(se_own, abs(l_own)) if n else min(pool["sd"], abs(pool["log"]))
        applied = False
    log = float(np.clip(log, math.log(MIN_MULT), math.log(MAX_MULT)))
    source = "own" if n and (not pool_clear or differs) else ("peers" if not n else "own+peers")
    return {"log": log, "multiplier": math.exp(log), "occurrences": n, "se": float(se),
            "applied": bool(applied), "z": (round(abs(l_own) / se_own, 2) if n else None),
            "source": source, "peers": (pool or {}).get("peers", 0), "poolClear": pool_clear,
            "differsFromPeers": bool(differs),
            "skipped": int((own_rec or {}).get("skipped") or 0),
            "rawOwn": math.exp(l_own) if n else None}


def pool_effects(peer_measures):
    """{key: {"log", "sd", "peers", "clear"}}: what similar products say about each period.

    A standard random-effects pool: each product's own estimate counts by how precise it
    is, and the spread between products beyond their noise is how much they genuinely
    differ (never less than BETWEEN_SD_FLOOR). `clear` when the typical lift stands
    POOL_CLEAR_Z standard errors from zero; `sd` is how far one product in the category
    may sit from that typical lift."""
    per = {}
    for m in peer_measures:
        for k, rec in (m or {}).items():
            l_own, n, prec = own_effect(rec)
            if n and prec > 0:
                per.setdefault(k, []).append((l_own, 1.0 / prec))
    out = {}
    for k, vals in per.items():
        if len(vals) < MIN_POOL_PEERS:
            continue
        l = np.array([a for a, _ in vals]); v = np.array([b for _, b in vals])
        w = 1.0 / v
        mu_fe = float((w * l).sum() / w.sum())
        q = float((w * (l - mu_fe) ** 2).sum())
        c = float(w.sum() - (w ** 2).sum() / w.sum())
        tau2 = max(BETWEEN_SD_FLOOR ** 2, (q - (len(l) - 1)) / c if c > 0 else 0.0)
        ws = 1.0 / (v + tau2)
        mu = float((ws * l).sum() / ws.sum()); se2 = float(1.0 / ws.sum())
        out[k] = {"log": mu, "peers": len(vals), "sd": float(math.sqrt(tau2 + se2)),
                  "clear": bool(len(vals) >= MIN_CLEAR_PEERS and abs(mu) / math.sqrt(se2) >= POOL_CLEAR_Z)}
    return out


def future_multipliers(dates, effects, assign):
    """(multipliers, relative uncertainty, key) per date: 1.0 / 0.0 / None off-holiday."""
    mult, se, keys = [], [], []
    for x in dates:
        k = assign.get(_d(x))
        e = effects.get(k) if k else None
        if e and not e.get("applied", abs(e["log"]) >= MIN_EFFECT) and e.get("se"):
            # Not clear enough to move the forecast, but not ruled out either: the chance
            # that it's real still belongs in the safety stock.
            mult.append(1.0); se.append(e["se"]); keys.append(k)
        elif e and e.get("applied", abs(e["log"]) >= MIN_EFFECT):
            m = e["multiplier"]
            sh = e.get("shape")
            off = getattr(assign, "offsets", {}).get(_d(x))
            if sh and off and off[1] == len(sh) and off[0] is not None:
                m = m * sh[off[0]]
            mult.append(m); se.append(e["se"]); keys.append(k)
        else:
            mult.append(1.0); se.append(0.0); keys.append(None)
    return mult, se, keys


def pct_text(m):
    p = round((m - 1) * 100)
    return f"+{p}%" if p > 0 else f"{p}%"


# ── One product, end to end ──────────────────────────────────────────────────────────
_peer_measure_cache: dict = {}
_assign_cache: dict = {}


def _frame_key(df):
    try:
        p = float(df["price"].fillna(0).astype(float).sum()) if "price" in df.columns else 0.0
        return (len(df), str(df["ds"].min())[:10], str(df["ds"].max())[:10], round(float(df["y"].sum()), 3),
                round(p, 3))
    except Exception:                                   # noqa: BLE001
        return None


def history_assign(df, settings, horizon_end=None, margin_days=70, shift=0):
    """Day→holiday map covering a frame's history (with a margin, so a window cut off by
    the start of the history is seen as partial) and, optionally, the forecast ahead.

    `shift`: days an uploaded sheet was moved so its last row lands on yesterday (see
    main._ingest). Its history then sits on moved dates, and a holiday has to be looked up
    on the date the sale really happened, or every Christmas is read one or more days off.
    The forecast ahead is on real dates and is looked up as is."""
    if df is None or not len(df) or not selected_keys(settings):
        return {}
    shift = int(shift or 0)
    first, last = df["ds"].min(), df["ds"].max()
    lo = (first - pd.Timedelta(days=margin_days)).date()
    hi = pd.Timestamp(horizon_end).date() if horizon_end is not None else last.date()
    ck = (lo, hi, shift, json.dumps(settings, sort_keys=True))
    got = _assign_cache.get(ck)
    if got is not None:
        return got
    if len(_assign_cache) > 500:
        _assign_cache.clear()
    if not shift:
        got = assign_days(lo, hi, settings)
    else:
        sd = _dt.timedelta(days=shift)
        past = assign_days(lo - sd, last.date() - sd, settings)
        got = AssignMap({d + sd: k for d, k in past.items()})
        got.offsets = {d + sd: o for d, o in past.offsets.items()}
        if hi > last.date():
            ahead = assign_days(last.date() + _dt.timedelta(days=1), hi, settings)
            got.update(ahead)
            got.offsets.update(ahead.offsets)
    _assign_cache[ck] = got
    return got


def holiday_mask(df, assign):
    if df is None or not len(df) or not assign:
        return np.zeros(0 if df is None else len(df), bool)
    return np.array([assign.get(x) is not None for x in df["ds"].dt.date], bool)


def strip(df, assign):
    """The frame without picked-holiday days, so a spike can't shape a baseline or a
    monthly season it doesn't belong to."""
    if df is None or not assign or not len(df):
        return df
    m = holiday_mask(df, assign)
    return df[~m] if m.any() else df


def _as_of(m, as_of):
    """Only the occurrences (and the days after them) that were known by `as_of`."""
    if as_of is None:
        return m
    lim = pd.Timestamp(as_of).date().isoformat()
    out = {}
    for k, rec in (m or {}).items():
        keep = [i for i, e in enumerate(rec.get("ends") or []) if e <= lim]
        out[k] = {f: [(rec.get(f) or [None] * len(rec["logs"]))[i] for i in keep]
                  for f in ("logs", "expected", "base", "ends", "starts", "actual", "days")}
        out[k]["k"] = rec.get("k", 1e9)
        out[k]["skipped"] = rec.get("skipped", 0)
        out[k]["phi"] = rec.get("phi", 1.0)
    return out


def peer_measures(frames, settings, version, price_fn=None, as_of=None, shift=0):
    """Each peer's own measurements, cached per frame and settings version. Measured once
    on the whole frame and then cut to what was known `as_of`, so a backtest or
    calibration cutoff never sees a later holiday and the work isn't redone per cutoff."""
    out = []
    for f in frames or []:
        if not hasattr(f, "columns") or not len(f):
            continue
        key = _frame_key(f)
        ck = (key, version) if key is not None else None
        if ck is not None and ck in _peer_measure_cache:
            out.append(_as_of(_peer_measure_cache[ck], as_of)); continue
        # A peer's days with nothing on the shelf say nothing about its holidays either
        # (the product's own frame gets the same treatment before it is measured).
        if "units_in_stock" in f.columns:
            try:
                import censoring as _cen
                f = _cen.apply_to_frame(f)[0]
            except Exception:                               # noqa: BLE001
                pass
        a = history_assign(f, {k: v for k, v in settings.items() if k != "exclude"}, shift=shift)
        el, ref = (price_fn(strip(f, a)) if price_fn else (0.0, None)) or (0.0, None)
        m = measure(f, a, elasticity=el, ref_price=ref)
        if ck is not None:
            if len(_peer_measure_cache) > 5000:
                _peer_measure_cache.clear()
            _peer_measure_cache[ck] = m
        out.append(_as_of(m, as_of))
    return out


def learn(df, related, settings, *, assign, elasticity=0.0, ref_price=None, price_fn=None, version="",
          as_of=None, own=None, store=None, shift=0):
    """Effects for one product: {key: combine(...)} for every picked holiday that has
    something behind it (its own past years, its peers', or both)."""
    plain = measure(df, assign, elasticity=elasticity, ref_price=ref_price)
    if own is None:
        own = plain
    else:
        # The model measured the lifts against its own baseline; which holidays could not
        # be separated from a discount still comes from the plain measurement.
        own = {**{k: {"logs": [], "expected": [], "ends": [], "skipped": v.get("skipped", 0),
                      "phi": v.get("phi", 2.0)}
                  for k, v in plain.items() if v.get("skipped") and k not in own}, **own}
    keys = selected_keys(settings)
    peers = peer_measures(related, settings, version, price_fn=price_fn, as_of=as_of, shift=shift)
    pool = pool_effects(peers)
    # A category too small to say anything about a period hands over to the whole store.
    thin = [k for k in keys if (pool.get(k) or {}).get("peers", 0) < MIN_CLEAR_PEERS]
    store_pool, store_peers = {}, []
    if thin and store:
        store_peers = peer_measures(store, settings, version, price_fn=price_fn, as_of=as_of, shift=shift)
        store_pool = pool_effects(store_peers)
    out = {}
    for k in keys:
        scope, pk, pm = "category", pool.get(k), peers
        if k in thin and (store_pool.get(k) or {}).get("peers", 0) > (pk or {}).get("peers", 0):
            scope, pk, pm = "store", store_pool.get(k), store_peers
        e = combine(own.get(k), pk, custom=is_custom(k))
        if e is not None:
            e["name"] = name_of(k, settings)
            e["peerScope"] = scope
            e["shape"] = shape(own.get(k), [m.get(k) for m in pm if m.get(k)]) if e["applied"] else None
            # Shown on the dates the sales really happened (an uploaded sheet's history may
            # have been moved; see history_assign).
            e["years"] = [{"start": (_d(st) - _dt.timedelta(days=int(shift or 0))).isoformat(),
                           "pct": round((math.exp(lg) - 1) * 100), "units": round(a),
                           "ordinary": round(ex)}
                          for st, lg, a, ex in zip((own.get(k) or {}).get("starts") or [],
                                                   (own.get(k) or {}).get("logs") or [],
                                                   (own.get(k) or {}).get("actual") or [],
                                                   (own.get(k) or {}).get("expected") or [])
                          if st is not None]
            out[k] = e
        elif (own.get(k) or {}).get("skipped"):
            out[k] = {"log": 0.0, "multiplier": 1.0, "occurrences": 0, "se": 0.0, "source": "none",
                      "peers": 0, "skipped": int(own[k]["skipped"]), "rawOwn": None, "applied": False,
                      "z": None, "poolClear": False, "shape": None, "years": [], "name": name_of(k, settings)}
    return out

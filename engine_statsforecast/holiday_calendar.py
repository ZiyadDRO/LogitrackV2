"""
Holidays and promotional periods: WHEN each one falls, and the store's settings for them.

The calendar (CATALOG, windows_for) puts every holiday on its real dates each year, moving
ones included (Thanksgiving, Easter, the Super Bowl), plus any sales periods the store adds
(custom) and dates it supplies (Prime Day). HolidaySettings stores those choices and the
periods switched off for a product (ignored).

How a holiday moves a product's sales is holiday_shape.py's job: each holiday's stretch
keeps the forecast's own total and the day-by-day split follows past years. The helpers
here it relies on are the weekday pattern, the discount mask and the day-to-day noise of a
product's sales. (An earlier layer that learned a +X% lift per holiday lived here too; it
was measured worse than the stretches and removed.)
"""
from __future__ import annotations

import datetime as _dt
import json
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


# Keys other modules may switch off per product (holiday_shape registers its stretches).
_EXTRA_KEYS: set = set()


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
    known = set(_BY_KEY) | ids | _EXTRA_KEYS
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



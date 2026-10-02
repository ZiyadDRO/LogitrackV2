"""
closed_days.py — the days the store was, or will be, closed.

WHY THIS EXISTS

A closed day isn't a bad sales day, it's a day that didn't happen. Left in the history as a
zero, Christmas Day read as "-75%" (the most the holiday layer will cut), the everyday level
was dragged down by days nobody could buy anything, and the forecast still expected sales
on the next Christmas. So the store says which days it's closed, and those days are:

  · left out of everything the forecast learns from (the product's own history, similar
    products' histories, holiday measurements, the backtest), exactly like a day with
    nothing on the shelf; and
  · forecast at zero when they come round again.

This works backwards too: marking a past day closed refits every product without it.

Two kinds of entry:
  dates    single days ("2024-12-25"), past or future
  yearly   closed every year on a holiday: Thanksgiving, Christmas Day, New Year's Day,
           Easter Sunday, the Fourth of July (Thanksgiving and Easter move every year)

Kept per workspace (see workspace.py): a store's closures and your spreadsheets' closures
are different calendars.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading

import holiday_calendar as _HOL

STORE_PATH = os.environ.get(
    "LOGITRACK_CLOSED_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "closed_days.json"))

# Holidays a store can close for every year, in the order they're offered.
YEARLY = [("thanksgiving", "Thanksgiving"), ("christmas_eve", "Christmas Eve"),
          ("christmas_day", "Christmas Day"), ("new_year", "New Year's Day"),
          ("easter", "Easter Sunday"), ("july_4", "Fourth of July")]
_YEARLY_KEYS = {k for k, _ in YEARLY}
MAX_DATES = 5000


class SettingsError(ValueError):
    pass


def _day_of(key: str, year: int) -> _dt.date | None:
    """The one day a yearly closure falls on in `year`."""
    if key == "christmas_eve":
        return _dt.date(year, 12, 24)
    if key == "july_4":
        return _dt.date(year, 7, 4)
    row = _HOL._BY_KEY.get(key)
    if row is None:
        return None
    try:
        return _HOL._anchor(row[3], year)
    except Exception:                                   # noqa: BLE001
        return None


def validate(data) -> dict:
    data = data or {}
    if not isinstance(data, dict):
        raise SettingsError("Expected an object with 'dates' and 'yearly'.")
    dates = set()
    for x in data.get("dates") or []:
        try:
            dates.add(_dt.date.fromisoformat(str(x)[:10]).isoformat())
        except ValueError:
            raise SettingsError(f"'{x}' isn't a date (use YYYY-MM-DD).")
    if len(dates) > MAX_DATES:
        raise SettingsError(f"At most {MAX_DATES} closed days.")
    yearly = [k for k, _ in YEARLY if k in set(data.get("yearly") or [])]
    bad = set(data.get("yearly") or []) - _YEARLY_KEYS
    if bad:
        raise SettingsError(f"Unknown yearly closure: {', '.join(sorted(bad))}.")
    return {"dates": sorted(dates), "yearly": yearly}


def expand(settings: dict, start: _dt.date, end: _dt.date) -> set:
    """Every closed calendar date between start and end (inclusive)."""
    out = set()
    for x in (settings or {}).get("dates") or []:
        d = _dt.date.fromisoformat(x)
        if start <= d <= end:
            out.add(d)
    for k in (settings or {}).get("yearly") or []:
        for y in range(start.year, end.year + 1):
            d = _day_of(k, y)
            if d is not None and start <= d <= end:
                out.add(d)
    return out


def yearly_label(settings: dict, d: _dt.date) -> str | None:
    """Which yearly closure covers `d`, if any."""
    for k, name in YEARLY:
        if k in ((settings or {}).get("yearly") or []) and _day_of(k, d.year) == d:
            return name
    return None


class ClosedDays:
    """{slot: settings} on disk. A missing or unreadable file is an empty calendar."""

    def __init__(self, path: str | None = None):
        self.path = path or STORE_PATH
        self._lock = threading.RLock()

    def _all(self) -> dict:
        try:
            with open(self.path) as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, slot: str) -> dict:
        with self._lock:
            raw = (self._all().get("slots") or {}).get(str(slot))
        try:
            return validate(raw)
        except SettingsError:
            return {"dates": [], "yearly": []}

    def save(self, slot: str, settings: dict) -> dict:
        clean = validate(settings)
        with self._lock:
            d = self._all()
            slots = dict(d.get("slots") or {})
            slots[str(slot)] = clean
            d["slots"] = slots
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(d, fh, indent=1)
            os.replace(tmp, self.path)
        return clean

    def drop(self, slot: str) -> None:
        with self._lock:
            d = self._all()
            hit = False
            for key in ("slots", "zones"):
                if str(slot) in (d.get(key) or {}):
                    d[key].pop(str(slot))
                    hit = True
            if hit:
                self._write(d)

    def _write(self, d: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh, indent=1)
        os.replace(tmp, self.path)

    # ── the store's time zone ──────────────────────────────────────────────────────
    # Kept with the calendar it belongs to, per workspace: a store's zone and your
    # spreadsheets' zone can differ. Two parts, so that clearing a choice falls back
    # to what the store reports rather than to nothing:
    #   set       picked on the Closed days panel (wins)
    #   detected  reported by the connected store (Square location, Shopify shop)
    def get_zone(self, slot: str) -> dict:
        with self._lock:
            z = (self._all().get("zones") or {}).get(str(slot)) or {}
        return {"set": z.get("set"), "detected": z.get("detected"),
                "detectedFrom": z.get("detectedFrom")}

    def set_zone(self, slot: str, name: str | None) -> dict:
        with self._lock:
            d = self._all()
            zones = dict(d.get("zones") or {})
            z = dict(zones.get(str(slot)) or {})
            if name:
                z["set"] = name
            else:
                z.pop("set", None)
            zones[str(slot)] = z
            d["zones"] = zones
            self._write(d)
        return self.get_zone(slot)

    def set_detected(self, slot: str, name: str | None, source: str | None) -> dict:
        with self._lock:
            d = self._all()
            zones = dict(d.get("zones") or {})
            z = dict(zones.get(str(slot)) or {})
            if z.get("detected") == name and z.get("detectedFrom") == source:
                return self.get_zone(slot)
            z["detected"], z["detectedFrom"] = name, source
            zones[str(slot)] = z
            d["zones"] = zones
            self._write(d)
        return self.get_zone(slot)

"""
Today's sales — the day that has not finished yet.

WHY THIS IS A SEPARATE FILE FROM THE CATALOGUE.

The training series ends at the last COMPLETE day, deliberately. A day still in progress
is not a small day; it is an unknown one, and feeding it to the models teaches them that
demand collapses every morning and recovers by midnight. That rule is not negotiable and
this module does not bend it: nothing here is ever ingested, refitted on, or backtested
against. It is a read-only side channel.

But "not in the model" is not the same as "not worth knowing". Two things need it:

  · Seeing the day. The sync runs at 00:15, so without this the app shows yesterday all
    day and reports today as zero — which reads as "nothing sold" rather than "not
    counted yet", and is indistinguishable from a product that really has stopped.

  · Keeping a counted stock level honest. Once someone counts a shelf, the level only
    stays true until the next sale. Closed days come from the catalogue; the hours since
    midnight come from here. Without it a count is stale by up to a day, which on a fast
    line is the whole point of counting.

WHAT IT STORES. One row per (sku, local day): units sold so far, and when we last asked.
Rows for days that have since closed are pruned — the catalogue owns those, and keeping a
second copy invites the two to disagree. There is exactly one day in here at a time.

FRESHNESS IS PART OF THE ANSWER. Every read reports how old the figure is, because a
number from 50 minutes ago and a number from 5 seconds ago justify different decisions
and the caller cannot tell them apart otherwise. `stale_after` marks it rather than
hiding it: suppressing a slightly old count would be worse than labelling it.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading


STORE_PATH = os.environ.get(
    "LOGITRACK_TODAY_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "today_sales.json"))

# Past this, the figure is labelled stale. Sampling is hourly, so a little over an hour
# means a tick was missed rather than that we are simply between ticks.
STALE_AFTER_SECONDS = 4500.0

# THE SHAPE OF A DAY. Each hourly reading is also a point on the store's running total for
# the day, so a finished day says what share of its sales had happened by each hour. Averaged
# over days, that is how a day that changed price at 2 PM is split: the share of a normal
# day's sales that comes after 2 PM gets the new price's effect. Only days read into the
# late evening count (a day whose last reading was at 3 PM would look like it stopped then),
# and until PROFILE_MIN_DAYS of them exist the day is taken as even across its 24 hours.
PROFILE_MIN_DAYS = 14
PROFILE_LAST_READING_HOUR = 21.0


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class TodaySales:
    """Units sold so far on the day in progress. Never part of the training data."""

    def __init__(self, path: str | None = None):
        self.path = path or STORE_PATH
        self._lock = threading.RLock()
        self._day: str | None = None          # the local day these counts belong to
        self._units: dict[str, float] = {}
        self._fetched_at: str | None = None
        self._curve: list = []                # [(local hour, store units so far)] today
        self._profile = {"days": 0, "cum": [0.0] * 25}
        self.load()

    # -- persistence --
    def load(self):
        with self._lock:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                self._day = raw.get("day")
                self._units = {str(k): float(v) for k, v in (raw.get("units") or {}).items()}
                self._fetched_at = raw.get("fetchedAt")
                self._curve = [(float(h), float(u)) for h, u in (raw.get("curve") or [])]
                pr = raw.get("profile") or {}
                if isinstance(pr.get("cum"), list) and len(pr["cum"]) == 25:
                    self._profile = {"days": int(pr.get("days") or 0),
                                     "cum": [float(x) for x in pr["cum"]]}
            except (FileNotFoundError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                self._day, self._units, self._fetched_at = None, {}, None
        return self

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "day": self._day, "units": self._units,
                           "fetchedAt": self._fetched_at, "curve": self._curve,
                           "profile": self._profile}, fh)
            os.replace(tmp, self.path)        # atomic: a crash can't truncate it
        return self

    # -- writes --
    def _fold_day(self):
        """Add the finished day's running total to the average shape, if it was read late
        enough to show the whole day."""
        pts = sorted(self._curve)
        self._curve = []
        if not pts or pts[-1][0] < PROFILE_LAST_READING_HOUR:
            return
        total = max(u for _, u in pts)
        if total <= 0:
            return
        hs = [0.0] + [h for h, _ in pts]
        us = [0.0] + [min(u, total) for _, u in pts]
        cum = []
        for k in range(25):
            if k >= hs[-1]:
                cum.append(1.0)
                continue
            j = max(i for i in range(len(hs)) if hs[i] <= k)
            h0, h1, u0, u1 = hs[j], hs[j + 1], us[j], us[j + 1]
            frac = 0.0 if h1 <= h0 else (k - h0) / (h1 - h0)
            cum.append((u0 + frac * (u1 - u0)) / total)
        self._profile = {"days": self._profile["days"] + 1,
                         "cum": [a + b for a, b in zip(self._profile["cum"], cum)]}

    def share_after(self, hour: float) -> tuple[float, str]:
        """(share of a normal day's sales that come after `hour`, "store" or "even")."""
        h = min(max(float(hour), 0.0), 24.0)
        with self._lock:
            n = self._profile["days"]
            if n < PROFILE_MIN_DAYS:
                return (24.0 - h) / 24.0, "even"
            cum = [c / n for c in self._profile["cum"]]
        k = min(int(h), 23)
        before = cum[k] + (h - k) * (cum[k + 1] - cum[k])
        return max(0.0, min(1.0, 1.0 - before)), "store"

    def profile_days(self) -> int:
        with self._lock:
            return int(self._profile["days"])

    def record(self, day: str, units: dict, *, now=None, hour=None) -> dict:
        """Replace the whole day's counts. Not additive, on purpose.

        Each fetch asks Square for the day's orders from the beginning, so the answer it
        returns IS the running total. Adding to what we already had would double-count
        every sale on every tick. Replacing also makes a refund or a voided order correct
        itself on the next pass rather than persisting forever.
        """
        ts = (now or utcnow()).isoformat()
        clean = {}
        for sku, u in (units or {}).items():
            try:
                clean[str(sku)] = float(u)
            except (TypeError, ValueError):
                continue
        with self._lock:
            if self._day is not None and self._day != str(day):
                self._fold_day()
            self._day = str(day)
            self._units = clean
            self._fetched_at = ts
            if hour is not None:
                self._curve.append((round(float(hour), 3), float(sum(clean.values()))))
            self.save()
        return {"day": self._day, "skus": len(clean),
                "units": float(sum(clean.values())), "at": ts}

    def clear_if_closed(self, current_day: str) -> bool:
        """Drop the stored day once it is no longer the day in progress.

        The catalogue owns closed days. Leaving yesterday's partial count here would give
        two different answers for the same date depending on which one a caller happened
        to read, and the partial one would always be the wrong one.
        """
        with self._lock:
            if self._day is not None and self._day != str(current_day):
                self._fold_day()
                self._day, self._units, self._fetched_at = None, {}, None
                self.save()
                return True
        return False

    # -- reads --
    def age_seconds(self, now=None):
        with self._lock:
            if not self._fetched_at:
                return None
        try:
            t = _dt.datetime.fromisoformat(self._fetched_at)
        except ValueError:
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        return max(0.0, ((now or utcnow()) - t).total_seconds())

    def units_for(self, sku: str, current_day: str) -> float:
        """Units sold so far today for one product, or 0.

        Returns 0 — not None — when the stored day is not the day asked for. A stale day
        contributes nothing rather than contributing yesterday's number to today's total,
        which is the arithmetic that would silently understate a shelf.
        """
        with self._lock:
            if self._day != str(current_day):
                return 0.0
            return float(self._units.get(str(sku), 0.0))

    def status(self, current_day: str, now=None) -> dict:
        with self._lock:
            fresh = self._day == str(current_day)
            age = self.age_seconds(now)
            return {
                "day": self._day,
                "forToday": bool(fresh),
                "skus": len(self._units) if fresh else 0,
                "unitsTotal": float(sum(self._units.values())) if fresh else 0.0,
                "fetchedAt": self._fetched_at,
                "ageSeconds": age,
                # Labelled, never hidden. A caller that wants to be careful can check it;
                # one that does not still gets the number rather than a blank.
                "stale": bool(age is None or age > STALE_AFTER_SECONDS),
            }

    def all_units(self, current_day: str) -> dict:
        with self._lock:
            return dict(self._units) if self._day == str(current_day) else {}

    def purge(self):
        with self._lock:
            self._day, self._units, self._fetched_at = None, {}, None
            self._curve, self._profile = [], {"days": 0, "cum": [0.0] * 25}
            self.save()
        return self

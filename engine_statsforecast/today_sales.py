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
            except (FileNotFoundError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                self._day, self._units, self._fetched_at = None, {}, None
        return self

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "day": self._day, "units": self._units,
                           "fetchedAt": self._fetched_at}, fh)
            os.replace(tmp, self.path)        # atomic: a crash can't truncate it
        return self

    # -- writes --
    def record(self, day: str, units: dict, *, now=None) -> dict:
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
            self._day = str(day)
            self._units = clean
            self._fetched_at = ts
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
            self.save()
        return self

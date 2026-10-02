"""
Availability log — how long each product was actually buyable.

Shopify reports the inventory you have NOW. It keeps no history you can query, which is
stated plainly in `fetch_catalog_meta`: "inventoryQuantity is a CURRENT snapshot —
Shopify exposes no historical daily stock". So the only way to know that a product sold
out at 2pm last Tuesday is to have been looking on Tuesday.

That's what this does: every tick records what's on hand, and those readings accumulate
into a picture of when each product was and wasn't available. `censoring.py` turns that
into hours-in-stock per day, which is what separates "sold 5, nobody else wanted any"
from "sold 5, then turned people away for sixteen hours".

Two consequences worth being honest about:

  1. It only works forward. Days before you connected the store have no readings, and
     `hours_in_stock_from_samples` returns None for them rather than guessing — an
     unpolled day must never look like a stockout, or the fix becomes worse than the bug.

  2. Resolution is bounded by how often you poll. Hourly ticks put the sell-out moment
     within an hour, which is plenty: the difference between 58% and 62% of a day changes
     no decision. Sampling is deliberately cheap for the same reason.
"""

from __future__ import annotations

import datetime as _dt

import store_clock as _CLOCK
import json
import os
import threading

import censoring as _C


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class StockLog:
    """Timestamped inventory readings, persisted.

    Readings are pruned by age rather than count: the value of a sample is that it covers
    a day you'll later want to score, so the retention window is measured in days.
    """

    def __init__(self, path: str | None = None, retain_days: int = 120):
        self.path = path or os.environ.get(
            "LOGITRACK_STOCK_LOG",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_log.json"))
        self.retain_days = int(retain_days)
        self._lock = threading.RLock()
        self._samples: dict[str, list] = {}      # sku -> [{ts, onHand}]
        self.load()

    # -- persistence --
    def load(self):
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                self._samples = {str(k): list(v) for k, v in (raw.get("samples") or {}).items()}
            except (FileNotFoundError, json.JSONDecodeError, AttributeError, TypeError):
                self._samples = {}
        return self

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "samples": self._samples}, fh)
            os.replace(tmp, self.path)     # atomic: a crash can't truncate the record
        return self

    # -- writes --
    def record(self, levels: dict, *, now=None) -> dict:
        """Record one reading per SKU. `levels`: {sku: onHand}.

        Consecutive identical readings are collapsed — a product sitting at 40 units for a
        week needs two samples, not 168, and the interpolation treats a sample as valid
        until the next one anyway. The most recent reading is always kept so the tail of
        the timeline stays accurate.
        """
        ts = (now or utcnow()).isoformat()
        added = skipped = 0
        with self._lock:
            for sku, on_hand in (levels or {}).items():
                try:
                    v = float(on_hand)
                except (TypeError, ValueError):
                    continue
                arr = self._samples.setdefault(str(sku), [])
                if len(arr) >= 2 and arr[-1].get("onHand") == v and arr[-2].get("onHand") == v:
                    arr[-1] = {"ts": ts, "onHand": v}     # slide the tail, don't grow
                    skipped += 1
                    continue
                arr.append({"ts": ts, "onHand": v})
                added += 1
            self._prune(now)
            self.save()
        return {"added": added, "collapsed": skipped, "skus": len(self._samples), "at": ts}

    def _prune(self, now=None):
        cutoff = (now or utcnow()) - _dt.timedelta(days=self.retain_days)
        for sku, arr in list(self._samples.items()):
            kept = []
            for s in arr:
                try:
                    ts = _dt.datetime.fromisoformat(str(s["ts"]).replace("Z", "+00:00"))
                except (KeyError, ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_dt.timezone.utc)
                if ts >= cutoff:
                    kept.append(s)
            # Always keep one reading from before the window, or the first day inside it
            # loses its opening level and would read as uncovered.
            if len(kept) < len(arr) and arr:
                older = [s for s in arr if s not in kept]
                if older:
                    kept = [older[-1]] + kept
            self._samples[sku] = kept

    # -- reads --
    def latest(self, sku: str):
        """The most recent on-hand reading for one SKU, or None if it was never watched.

        This is what makes a reading survive a restart. Live inventory never reaches the
        catalog frame — a sync carries it alongside the sales, not inside them — so before
        this existed the only copy lived in the browser, and a cleared cache or a second
        device silently reverted every product to the dashboard default.
        """
        arr = self._samples.get(str(sku))
        if not arr:
            return None
        try:
            return float(arr[-1].get("onHand"))
        except (TypeError, ValueError, AttributeError, IndexError):
            return None

    def latest_all(self) -> dict:
        """{sku: on_hand} for every SKU with a reading."""
        with self._lock:
            out = {}
            for sku in self._samples:
                v = self.latest(sku)
                if v is not None:
                    out[sku] = v
            return out

    def samples(self, sku: str) -> list:
        with self._lock:
            return list(self._samples.get(str(sku), []))

    def hours_in_stock(self, sku: str, day: str):
        """Hours this product was buyable on `day`, or None if it wasn't being watched."""
        return _C.hours_in_stock_from_samples(self.samples(sku), day)

    def availability_series(self, sku: str, start: str, end: str) -> list:
        """[{date, hoursInStock, availability}] across an inclusive date range."""
        d0 = _dt.date.fromisoformat(str(start)[:10])
        d1 = _dt.date.fromisoformat(str(end)[:10])
        out, d = [], d0
        while d <= d1:
            h = self.hours_in_stock(sku, d.isoformat())
            out.append({"date": d.isoformat(), "hoursInStock": h,
                        "availability": None if h is None else round(_C.availability(h), 3)})
            d += _dt.timedelta(days=1)
        return out

    def coverage(self, sku: str | None = None) -> dict:
        """How much of a picture we actually have. Shown rather than assumed, because the
        whole feature is worthless-but-invisible until enough readings exist."""
        with self._lock:
            skus = [sku] if sku else list(self._samples.keys())
            first = last = None
            total = 0
            for s in skus:
                for smp in self._samples.get(str(s), []):
                    _ld = _CLOCK.local_date(smp.get("ts"))      # the store's day
                    ts = _ld.isoformat() if _ld else ""
                    if not ts:
                        continue
                    total += 1
                    first = ts if first is None or ts < first else first
                    last = ts if last is None or ts > last else last
        days = 0
        if first and last:
            days = (_dt.date.fromisoformat(last) - _dt.date.fromisoformat(first)).days + 1
        return {"skus": len(skus), "samples": total, "firstSeen": first,
                "lastSeen": last, "daysCovered": days,
                "trackingSince": first}

    def state_today(self, sku: str, now=None) -> dict:
        """What this product's stock is doing RIGHT NOW, from the readings on file.

        Deliberately NOT hours_in_stock(). That answers "how much of this DAY was it
        buyable", and to do so it carries the last reading forward to midnight — correct
        for a finished day, misleading for one in progress. At 10am it would report a
        nearly-full day of availability for a day that is two hours old.

        This reports only what has actually been observed:

            state           in | out | unknown       (unknown = nothing sampled yet)
            level           the most recent reading
            lastReadingAt   when that reading was taken. THIS is the per-product
                            freshness signal: record() slides the tail timestamp forward
                            on an unchanged level, so it tracks the poll, not the change.
            changesToday    distinct readings on file for today. record() collapses
                            identical consecutive readings, so this counts how often the
                            level MOVED, not how often we looked — a product sitting
                            still through fourteen polls shows 1. Never read it as
                            "the sampler isn't running"; that is lastReadingAt's job,
                            and globally it is /api/sync -> sampler.
            outSince        if it is out NOW, when the run of zeroes began
            wentOutToday    it hit zero at some point today, even if it is back in stock
        """
        now = now or utcnow()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        out = {"state": "unknown", "level": None, "lastReadingAt": None,
               "changesToday": 0, "outSince": None, "wentOutToday": False}

        pts = []
        with self._lock:
            for smp in self._samples.get(str(sku), []):
                try:
                    ts = _dt.datetime.fromisoformat(str(smp["ts"]).replace("Z", "+00:00"))
                except (KeyError, ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_dt.timezone.utc)
                try:
                    pts.append((ts, float(smp.get("onHand") or 0)))
                except (TypeError, ValueError):
                    continue
        if not pts:
            return out
        pts.sort()

        last_ts, last_level = pts[-1]
        today = [(ts, v) for ts, v in pts if ts >= midnight]
        out.update({"state": "out" if last_level <= 0 else "in",
                    "level": last_level,
                    "lastReadingAt": last_ts.isoformat(),
                    "changesToday": len(today),
                    "wentOutToday": any(v <= 0 for _, v in today)})

        # How long it has been out: walk back while the readings are still zero. Reported
        # from the FIRST zero, because that is the last moment we know it was buyable —
        # anything finer would be inventing precision the hourly cadence doesn't have.
        if last_level <= 0:
            since = last_ts
            for ts, v in reversed(pts[:-1]):
                if v > 0:
                    break
                since = ts
            out["outSince"] = since.isoformat()
        return out

    def last_sample_at(self):
        """When a reading was last filed, across every product — or None.

        The sampler asks this at boot instead of keeping a timer of its own. A process
        restart wipes an in-memory timer but not the log, so this is the only answer that
        survives the thing most likely to interrupt sampling: the server going down.
        """
        newest = None
        with self._lock:
            for arr in self._samples.values():
                if not arr:
                    continue
                ts = str(arr[-1].get("ts") or "")
                if ts and (newest is None or ts > newest):
                    newest = ts
        if not newest:
            return None
        try:
            out = _dt.datetime.fromisoformat(newest.replace("Z", "+00:00"))
        except ValueError:
            return None
        return out if out.tzinfo else out.replace(tzinfo=_dt.timezone.utc)

    def purge(self):
        with self._lock:
            self._samples = {}
            self.save()
        return self


def sample_from_shopify(log: StockLog, *, fetch=None, shop=None, token=None, now=None) -> dict:
    """Take one reading of every SKU's inventory and file it.

    Failures are swallowed into the return value rather than raised: this runs on a
    background tick alongside forecast scoring, and a store hiccup must not take that
    down. A missed reading costs resolution on one day, nothing more.
    """
    if fetch is None:
        from shopify_source import fetch_catalog_meta as fetch  # noqa: WPS433
    try:
        meta = fetch(shop=shop, token=token) if (shop or token) else fetch()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "added": 0}
    levels = {}
    for sku, m in (meta or {}).items():
        if isinstance(m, dict) and m.get("stock") is not None:
            levels[sku] = m["stock"]
    if not levels:
        return {"ok": True, "added": 0, "note": "store reported no inventory figures"}
    return {"ok": True, **log.record(levels, now=now)}

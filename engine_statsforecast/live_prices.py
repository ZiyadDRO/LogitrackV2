"""
The price each product is showing RIGHT NOW, as the point-of-sale reports it.

WHY THIS EXISTS. Everything else in the tool learns price from completed sales: what a day
actually sold at. That is the right source for the past and the wrong one for the present.
A discount switched on this morning has no sales behind it yet, and a price with a sale
behind it tells you what one customer paid (perhaps with a coupon), not what the shelf
says. The POS knows the shelf: Square's item price plus any automatic discount in force,
Shopify's price against its compare-at price.

WHAT IT IS USED FOR, AND WHAT IT IS NOT.
  · Displayed as the current price, with the original struck through when discounted.
  · Fed to the forecast as a price input for the days AHEAD — a live discount behaves
    exactly like a promotion you logged yourself, dated today to its end date.
  · Never written into the sales history. Past days keep the prices they actually sold at.

Refreshed by the nightly sync and by every hourly stock reading (the same catalogue call
returns both, so it costs nothing extra). Persisted, so a restart does not forget a sale
that is on right now.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading

STORE_PATH = os.environ.get(
    "LOGITRACK_LIVE_PRICES_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_prices.json"))

# A price read longer ago than this is labelled stale rather than hidden.
STALE_AFTER_SECONDS = 4500.0

# Differences smaller than this are rounding, not a discount.
_TOL = 0.005

_FIELDS = ("listPrice", "currentPrice", "discounted", "discountName", "discountPct",
           "discountFrom", "discountUntil", "source")


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def normalise(raw: dict | None, source: str | None = None) -> dict | None:
    """One product's POS price facts in a fixed shape, or None when there is no price.

    `raw` is whatever a source's fetch_catalog_meta put under the product: listPrice
    (the undiscounted shelf price), currentPrice (what a customer pays today), and
    optionally discountName / discountFrom / discountUntil.
    """
    if not isinstance(raw, dict):
        return None
    try:
        lp = raw.get("listPrice")
        cp = raw.get("currentPrice")
        lp = None if lp is None else round(float(lp), 2)
        cp = None if cp is None else round(float(cp), 2)
    except (TypeError, ValueError):
        return None
    if lp is None and cp is None:
        return None
    if lp is None:
        lp = cp
    if cp is None:
        cp = lp
    if lp <= 0 or cp <= 0:
        return None
    disc = cp < lp - _TOL
    out = {
        "listPrice": lp,
        "currentPrice": cp if disc else lp,
        "discounted": bool(disc),
        "discountName": (raw.get("discountName") or None) if disc else None,
        "discountPct": round((1 - cp / lp) * 100, 2) if disc else None,
        "discountFrom": (raw.get("discountFrom") or None) if disc else None,
        "discountUntil": (raw.get("discountUntil") or None) if disc else None,
        "source": source or raw.get("source"),
    }
    return out


class LivePrices:
    def __init__(self, path: str | None = None):
        self.path = path or STORE_PATH
        self._lock = threading.RLock()
        self._prices: dict[str, dict] = {}
        self.load()

    def load(self):
        with self._lock:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                self._prices = {str(k): v for k, v in (raw.get("prices") or {}).items()
                                if isinstance(v, dict)}
            except (FileNotFoundError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                self._prices = {}
        return self

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"prices": self._prices}, fh, indent=1)
                os.replace(tmp, self.path)
            except OSError:
                pass

    def record(self, meta: dict | None, source: str | None = None, now=None) -> set:
        """File the prices from one catalogue read. Returns the SKUs whose price facts
        changed — the only ones whose forecast needs refitting.

        A product the read did not mention keeps its last known price: one catalogue page
        failing to come back is not evidence that a discount ended.
        """
        now = now or utcnow()
        changed = set()
        with self._lock:
            for sku, m in (meta or {}).items():
                n = normalise(m, source)
                if n is None:
                    continue
                sku = str(sku)
                old = self._prices.get(sku) or {}
                if any(old.get(k) != n.get(k) for k in _FIELDS if k != "source"):
                    changed.add(sku)
                n["fetchedAt"] = now.isoformat()
                self._prices[sku] = n
            if meta:
                self.save()
        return changed

    def get(self, sku, now=None) -> dict | None:
        with self._lock:
            p = self._prices.get(str(sku))
            if not p:
                return None
            p = dict(p)
        try:
            age = ((now or utcnow()) - _dt.datetime.fromisoformat(p["fetchedAt"])).total_seconds()
            p["ageSeconds"] = round(age)
            p["stale"] = age > STALE_AFTER_SECONDS
        except (KeyError, TypeError, ValueError):
            p["stale"] = True
        return p

    def all(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._prices.items()}

    def purge(self):
        with self._lock:
            self._prices = {}
            try:
                os.remove(self.path)
            except OSError:
                pass


def forecast_events(live: dict | None, first_day, last_day, recorded_price=None) -> list:
    """The live price as forecast inputs, in the same shape as events a person logs.

    · A shelf price different from the last recorded one becomes a permanent change from
      the first forecast day: the POS says what the price IS, and the sales history can
      only say what it was.
    · A discount in force becomes a promotion from the first forecast day to its end date,
      or through the whole horizon when the POS gives no end ("until further notice" — the
      next sync will notice when it stops). A promotion rather than a bare price change,
      because a Square or Shopify discount is exactly what `on_promotion` marked on the
      historical days that had one, so the model reads it the way it learned it.

    Past days are never touched: `first_day` is the first day without recorded sales.
    """
    if not live:
        return []
    fd = str(first_day)[:10]
    ld = str(last_day)[:10]
    evs = []
    lp = live.get("listPrice")
    try:
        if lp is not None and recorded_price is not None and abs(float(lp) - float(recorded_price)) > _TOL:
            evs.append({"type": "price_change_permanent", "date": fd, "new_price": float(lp),
                        "label": f"Shelf price in {live.get('source') or 'the POS'}",
                        "origin": "pos"})
    except (TypeError, ValueError):
        pass
    if live.get("discounted") and live.get("discountPct"):
        until = str(live.get("discountUntil") or "")[:10] or ld
        if until >= fd:
            evs.append({"type": "promotion", "date": fd, "end_date": min(until, ld),
                        "discount_pct": float(live["discountPct"]),
                        "label": live.get("discountName") or "Live discount",
                        "origin": "pos", "openEnded": not live.get("discountUntil")})
    return evs

"""
Arrival detection — noticing that an order landed, so nobody has to remember to say so.

People log a purchase order happily. Coming back three weeks later to record the day it
arrived is the step that gets skipped, and skipping it is expensive: the delivery never
enters the lead-time history, so P80 never improves, and the tool keeps planning from a
baseline someone typed once.

The inventory log already records what's on hand every tick. A restock is visible in it as
a jump: stock goes up, and stock only goes up when something arrives. So we can find the
arrival without being told.

What this deliberately does NOT do is record it silently. A jump can be a stock count
correction, a customer return, a transfer between locations, or a partial delivery, and
quietly writing the wrong received-date into the lead-time history corrupts the exact
number the feature exists to improve. So: detect, match to an open order, and ASK. The
confirmation is one click, which is a very different thing from remembering unprompted.
"""

from __future__ import annotations

import datetime as _dt

# Ignore noise. A couple of units appearing is far more likely to be a returned order or a
# recount than a delivery, and a false arrival is worse than a missed one — it writes a
# wrong date into the record that P80 is computed from.
MIN_JUMP_UNITS = 3
MIN_JUMP_FRACTION = 0.15        # or 15% of the expected order, whichever is larger

# Refunds also put stock back. Shopify's sales feed doesn't net them out, so a returned
# order looks exactly like a tiny delivery: stock goes up, nothing else changes.
#
# What separates them is SIZE relative to how fast the product sells. A delivery is
# restocking you for weeks; a refund is a fraction of a day. So a jump must be worth at
# least a couple of days of trade before it can be called an arrival — unless it matches
# an open order's quantity exactly, which no refund realistically does.
REFUND_GUARD_DAYS = 2.0

# How far from a PO's expected date a jump can land and still be considered that PO.
# Generous, because being late is exactly the case worth catching.
MATCH_WINDOW_EARLY = 21
MATCH_WINDOW_LATE = 60


def _dt_of(ts) -> _dt.datetime | None:
    try:
        d = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _d(x) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(x)[:10])
    except (TypeError, ValueError):
        return None


def find_jumps(samples: list, *, min_units: int = MIN_JUMP_UNITS) -> list:
    """Points where on-hand stock increased.

    Stock falls through sales and rises only when something is added, so every rise is a
    candidate arrival. Returns [{at, before, after, jump}] oldest first.
    """
    pts = []
    for s in samples or []:
        ts = _dt_of(s.get("ts"))
        if ts is None:
            continue
        try:
            pts.append((ts, float(s.get("onHand"))))
        except (TypeError, ValueError):
            continue
    pts.sort()
    out = []
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        jump = v1 - v0
        if jump >= max(1, min_units):
            # The STORE's day (store_clock): t1 is UTC, and a delivery counted at 9pm
            # Eastern belongs to that day, not the next.
            import store_clock as _CLOCK
            out.append({"at": t1.isoformat(), "date": _CLOCK.local_date(t1).isoformat(),
                        "before": v0, "after": v1, "jump": jump})
    return out


def match_order(jump: dict, orders: list) -> dict | None:
    """Find the open order a jump most plausibly represents.

    Scored rather than first-match: a supplier with several orders in flight needs the
    jump attached to the right one, and quantity is a much stronger signal than timing.
    """
    j = float(jump["jump"])
    jdate = _d(jump["date"])
    if jdate is None:
        return None

    best, best_score = None, -1.0
    for o in orders or []:
        if o.get("receivedDate"):
            continue                              # already recorded
        qty = float(o.get("qty") or 0)
        if qty <= 0:
            continue
        ordered = _d(o.get("orderedDate"))
        if ordered and jdate < ordered:
            continue                              # can't arrive before it was placed
        expected = _d(o.get("expectedDate")) or ordered
        if expected:
            days_off = (jdate - expected).days
            if days_off < -MATCH_WINDOW_EARLY or days_off > MATCH_WINDOW_LATE:
                continue
        else:
            days_off = 0

        # Quantity agreement dominates. A jump of 500 against a 500-unit PO is almost
        # certainly that PO even if it's three weeks late.
        ratio = min(j, qty) / max(j, qty)
        if ratio < 0.5:
            continue
        timing = 1.0 - min(abs(days_off) / float(MATCH_WINDOW_LATE), 1.0)
        score = ratio * 0.75 + timing * 0.25
        if score > best_score:
            best, best_score = o, score

    if best is None:
        return None
    qty = float(best.get("qty") or 0)
    return {
        "order": best,
        "confidence": round(best_score, 3),
        "exact": abs(j - qty) < 1e-6,
        "partial": j < qty - 1e-6,
        "over": j > qty + 1e-6,
        "jumpUnits": j,
        "orderQty": qty,
    }


def looks_like_refund(jump_units: float, order_qty: float, daily_rate=None,
                      *, exact: bool = False) -> bool:
    """Is this rise more plausibly a return than a delivery?

    An exact quantity match against an open order is never treated as a refund — a
    customer returning precisely your outstanding PO quantity isn't a real scenario.
    Otherwise the jump has to be worth more than a couple of days of sales.
    """
    if exact:
        return False
    if daily_rate is None or daily_rate <= 0:
        return False                        # no basis to judge; other guards still apply
    return jump_units < daily_rate * REFUND_GUARD_DAYS


def detect(samples: list, orders: list, *, min_units: int = MIN_JUMP_UNITS,
           since: str | None = None, daily_rate=None) -> list:
    """Arrivals worth asking the user to confirm.

    Every result is a SUGGESTION. Nothing here writes a received date — that stays a
    human decision, because a wrong one silently corrupts the lead-time record.

    `daily_rate` is this product's typical units/day. It's what lets a refund be told
    apart from a small delivery, so pass it whenever it's known.
    """
    cutoff = _d(since)
    out = []
    for jump in find_jumps(samples, min_units=min_units):
        if cutoff and _d(jump["date"]) < cutoff:
            continue
        m = match_order(jump, orders)
        if not m:
            continue
        qty = m["orderQty"]
        # A large PO whose jump is a small fraction of it is more likely a recount.
        if m["jumpUnits"] < max(min_units, qty * MIN_JUMP_FRACTION):
            continue
        # ...and a rise smaller than a couple of days' trade is more likely a return.
        if looks_like_refund(m["jumpUnits"], qty, daily_rate, exact=m["exact"]):
            continue
        o = m["order"]
        lead = None
        od, jd = _d(o.get("orderedDate")), _d(jump["date"])
        if od and jd:
            lead = (jd - od).days
        out.append({
            "orderId": o.get("id"),
            "skuId": o.get("skuId"),
            "arrivedOn": jump["date"],
            "detectedAt": jump["at"],
            "stockBefore": jump["before"],
            "stockAfter": jump["after"],
            "jumpUnits": m["jumpUnits"],
            "orderQty": qty,
            "confidence": m["confidence"],
            "exact": m["exact"],
            "partial": m["partial"],
            "over": m["over"],
            "impliedLeadDays": lead,
            "expectedDate": o.get("expectedDate"),
            "daysLate": ((jd - _d(o["expectedDate"])).days
                         if o.get("expectedDate") and jd and _d(o["expectedDate"]) else None),
            "message": describe(m, jump, lead),
        })
    out.sort(key=lambda r: (r["arrivedOn"], -r["confidence"]))
    return out


def describe(match: dict, jump: dict, lead=None) -> str:
    q, j = match["orderQty"], match["jumpUnits"]
    day = jump["date"]
    if match["exact"]:
        head = f"Stock rose by exactly {j:.0f} on {day}, matching this order."
    elif match["partial"]:
        head = (f"Stock rose by {j:.0f} on {day} against an order of {q:.0f}. "
                f"Looks like a partial delivery.")
    else:
        head = (f"Stock rose by {j:.0f} on {day}, more than the {q:.0f} ordered. "
                f"Possibly this delivery plus something else.")
    if lead is not None:
        head += f" That would make the lead time {lead} days."
    return head


def summarise(detected: list) -> dict:
    if not detected:
        return {"found": 0, "exact": 0, "partial": 0, "needsReview": 0}
    return {
        "found": len(detected),
        "exact": sum(1 for d in detected if d["exact"]),
        "partial": sum(1 for d in detected if d["partial"]),
        "over": sum(1 for d in detected if d["over"]),
        # Anything not an exact quantity match deserves a human eye before it becomes a
        # data point in the lead-time record.
        "needsReview": sum(1 for d in detected if not d["exact"] or d["confidence"] < 0.8),
        "oldest": min(d["arrivedOn"] for d in detected),
        "newest": max(d["arrivedOn"] for d in detected),
    }

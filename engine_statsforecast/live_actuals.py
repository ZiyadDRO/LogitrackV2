"""
Actual units sold, for grading a logged forecast.

Reads from Shopify because that's the only source that can answer "what sold between the
4th and the 17th" *after* the fact without someone re-exporting a spreadsheet. A live
tool has to be able to close its own loop.

The distinction this module exists to protect:

    a SKU that sold nothing   →  0.0     (a real result; the forecast was wrong or right)
    a store we couldn't read  →  None    (unknown; the entry stays pending)

Collapsing those two would silently make the tool look accurate for products that had
merely stopped reporting, and would permanently mis-grade an entry that a retry would
have scored correctly. `ForecastLog.score_due` treats None as "try again next time".
"""

from __future__ import annotations

import datetime as _dt


class ActualsUnavailable(Exception):
    pass


def _d(x) -> _dt.date:
    return _dt.date.fromisoformat(str(x)[:10])


def window_units(frame, sku: str, start, end) -> float:
    """Sum units_sold for one SKU over [start, end] inclusive, from a sales frame with
    columns [date, sku, units_sold]."""
    if frame is None or len(frame) == 0:
        return 0.0
    s, e = _d(start).isoformat(), _d(end).isoformat()
    dates = frame["date"].astype(str).str.slice(0, 10)
    m = (frame["sku"].astype(str) == str(sku)) & (dates >= s) & (dates <= e)
    if not m.any():
        return 0.0
    return float(frame.loc[m, "units_sold"].fillna(0).sum())


def lookback_days_for(entries, now=None, *, pad: int = 7) -> int:
    """How far back Shopify has to be asked so every pending window is covered.

    Fetching a fixed 90 days would silently fail to grade anything older, which is
    exactly the entry you most want graded after a gap in usage.
    """
    import store_clock as _CLOCK
    today = _CLOCK.local_date(now or _dt.datetime.now(_dt.timezone.utc))
    starts = [_d(e["windowStart"]) for e in entries if e.get("windowStart")]
    if not starts:
        return pad
    oldest = min(starts)
    return max(pad, (today - oldest).days + pad)


def shopify_actuals_provider(entries, *, now=None, fetch=None, shop=None, token=None,
                             known_skus=None):
    """Build the `actuals_fn(sku, start, end)` that ForecastLog.score_due wants.

    Shopify is queried ONCE for a window wide enough to cover every pending entry, then
    every entry is answered from that one frame — grading 200 entries must not mean 200
    API calls into a rate limit.

    If the fetch fails, the returned function yields None for everything, so nothing is
    graded and every entry survives to be retried.
    """
    if fetch is None:                                  # imported lazily: no network in tests
        from shopify_source import fetch_sales as fetch  # noqa: WPS433

    days = lookback_days_for(entries, now)
    try:
        frame = fetch(shop=shop, token=token, days=days) if (shop or token) else fetch(days=days)
        failed = False
    except Exception:
        frame, failed = None, True

    known = set()
    if not failed and frame is not None and len(frame):
        known = set(frame["sku"].astype(str).unique())
    # The store's own product list. A sales report only names products that SOLD in the
    # window it covers, so a product that sold nothing all week was "never reported",
    # returned None and stayed ungraded for ever: the tab graded only the products that
    # sold, and every week's worst misses (forecast 25, sold 0) were missing from it. A
    # product the store lists and that sold nothing did sell zero.
    if not failed and known_skus:
        known |= {str(k) for k in known_skus}

    def actuals_fn(sku, start, end):
        if failed:
            return None                                # unknown → stays pending
        # A SKU the store has never reported is not the same as one that sold zero this
        # window. Refuse to grade it rather than call the forecast infinitely wrong.
        if str(sku) not in known:
            return None
        return window_units(frame, sku, start, end)

    actuals_fn.failed = failed
    actuals_fn.days = days
    actuals_fn.knownSkus = len(known)
    return actuals_fn

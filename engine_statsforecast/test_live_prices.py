"""
The live price store: what the POS says the shelf price is right now.

Offline. No network, no model fits.
"""
from __future__ import annotations

import os
import sys
import tempfile

import live_prices as LP

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


def _store():
    d = tempfile.mkdtemp()
    return LP.LivePrices(path=os.path.join(d, "lp.json"))


def test_normalise():
    print("normalise")
    n = LP.normalise({"listPrice": 50, "currentPrice": 40, "discountName": "BF"}, "square")
    check("a lower current price is a discount", n["discounted"] and n["discountPct"] == 20.0, n)
    n = LP.normalise({"listPrice": 50, "currentPrice": 49.999}, "square")
    check("sub-cent differences are not a discount", not n["discounted"] and n["currentPrice"] == 50.0, n)
    n = LP.normalise({"currentPrice": 12.5}, "shopify")
    check("one price only: list and current are the same", n["listPrice"] == 12.5 and not n["discounted"], n)
    check("no price at all is None", LP.normalise({"stock": 4}) is None)
    check("a zero price is None", LP.normalise({"listPrice": 0}) is None)


def test_record_reports_only_changes():
    print("record")
    s = _store()
    ch = s.record({"A": {"listPrice": 50, "currentPrice": 50}, "B": {"listPrice": 10}}, "square")
    check("first reading: everything is new", ch == {"A", "B"}, ch)
    ch = s.record({"A": {"listPrice": 50, "currentPrice": 50}, "B": {"listPrice": 10}}, "square")
    check("same reading again: nothing changed, nothing to refit", ch == set(), ch)
    ch = s.record({"A": {"listPrice": 50, "currentPrice": 35, "discountName": "Sale"}}, "square")
    check("a discount switching on is a change", ch == {"A"}, ch)
    check("a product missing from a read keeps its last price", s.get("B")["listPrice"] == 10.0)
    s2 = LP.LivePrices(path=s.path)
    check("persisted across a restart", s2.get("A")["currentPrice"] == 35.0)
    s.purge()
    check("purge forgets everything", s.get("A") is None and not os.path.exists(s.path))


def test_forecast_events():
    print("forecast_events")
    live = LP.normalise({"listPrice": 50, "currentPrice": 30, "discountName": "BF",
                         "discountUntil": "2026-11-30"}, "square")
    ev = LP.forecast_events(live, "2026-11-20", "2027-12-31", recorded_price=50)
    check("a dated sale becomes one promotion to its end date",
          len(ev) == 1 and ev[0]["type"] == "promotion" and ev[0]["date"] == "2026-11-20"
          and ev[0]["end_date"] == "2026-11-30" and ev[0]["discount_pct"] == 40.0, ev)
    ev = LP.forecast_events(live, "2026-12-05", "2027-12-31", recorded_price=50)
    check("a sale that ends before the forecast starts adds nothing", ev == [], ev)
    open_ended = LP.normalise({"listPrice": 50, "currentPrice": 45}, "shopify")
    ev = LP.forecast_events(open_ended, "2026-11-20", "2027-12-31", recorded_price=50)
    check("no end date: runs through the horizon, marked open-ended",
          ev and ev[0]["end_date"] == "2027-12-31" and ev[0]["openEnded"] is True, ev)
    moved = LP.normalise({"listPrice": 55, "currentPrice": 55}, "square")
    ev = LP.forecast_events(moved, "2026-11-20", "2027-12-31", recorded_price=50)
    check("a new shelf price becomes a permanent change from the first forecast day",
          len(ev) == 1 and ev[0]["type"] == "price_change_permanent" and ev[0]["new_price"] == 55.0, ev)
    ev = LP.forecast_events(LP.normalise({"listPrice": 50}, "square"), "2026-11-20", "2027-12-31",
                            recorded_price=50)
    check("nothing on sale and nothing moved: no inputs at all", ev == [], ev)


def test_hourly_reading_carries_prices():
    """The hourly stock reading already calls the catalogue; the price rides along."""
    print("the hourly reading hands prices back")
    import sources as SRC
    import stock_log as SL
    fake = {"key": "fake", "fetch_catalog_meta": lambda creds: {
        "A": {"stock": 3, "listPrice": 50.0, "currentPrice": 40.0, "discountName": "Sale"},
        "B": {"stock": 7}}}
    SRC.SOURCES["fake"] = fake
    try:
        d = tempfile.mkdtemp()
        log = SL.StockLog(path=os.path.join(d, "s.json"))
        res = SRC._sample_stock(log, {}, "fake")
        check("stock still filed", res.get("added") == 2, res)
        check("prices handed back for products that have one",
              set((res.get("prices") or {}).keys()) == {"A"}
              and res["prices"]["A"]["currentPrice"] == 40.0, res.get("prices"))
    finally:
        SRC.SOURCES.pop("fake", None)


if __name__ == "__main__":
    print("\nLive prices\n" + "-" * 44)
    for fn in (test_normalise, test_record_reports_only_changes, test_forecast_events,
               test_hourly_reading_carries_prices):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All live-price tests passed.")

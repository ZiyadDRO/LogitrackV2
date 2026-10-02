"""
Holidays: the calendar and the store's settings, plus the price checks that used to live
beside the old per-holiday lifts.

  1. The calendar puts every holiday on the right dates; custom periods repeat; bad
     settings are refused with a message; a period can be switched off for one product.
  2. A product whose own prices read backwards doesn't use price, one with no category-
     mates doesn't borrow the store's response, and a live store's nightly re-sync keeps
     each product's planned events and hand-set category.

How holidays move a forecast is tested in test_holiday_shape.py.
"""
from __future__ import annotations
import os as _os, tempfile as _tf
_tmp = _tf.mkdtemp()
_os.environ["LOGITRACK_LIVE_PRICES_PATH"] = _os.path.join(_tmp, "live_prices.json")
_os.environ["LOGITRACK_HOLIDAYS_PATH"] = _os.path.join(_tmp, "holidays.json")

import datetime as dt
import logging
import math
import sys
import time
import warnings

warnings.filterwarnings("ignore")
logging.getLogger("cmdstanpy").disabled = True
logging.getLogger("prophet").setLevel(logging.ERROR)

import numpy as np
import pandas as pd

import holiday_calendar as H

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


D = dt.date


# ── 1. Calendar ──────────────────────────────────────────────────────────────────────
def test_calendar():
    print("holidays land on the right dates")
    w = lambda k, y: H.windows_for(k, D(y, 1, 1), D(y, 12, 31), {"selected": [k]})
    check("Thanksgiving 2025 is Nov 27", w("thanksgiving", 2025)[-1] == (D(2025, 11, 27), D(2025, 11, 27)))
    check("Black Friday weekend 2026 is Nov 27-29", (D(2026, 11, 27), D(2026, 11, 29)) in w("black_friday", 2026))
    check("Cyber Monday 2025 is Dec 1", (D(2025, 12, 1), D(2025, 12, 1)) in w("cyber_monday", 2025))
    check("Easter 2026 week ends Apr 5", (D(2026, 3, 30), D(2026, 4, 5)) in w("easter", 2026))
    check("Easter 2025 week ends Apr 20", (D(2025, 4, 14), D(2025, 4, 20)) in w("easter", 2025))
    check("Super Bowl 2026 is Feb 8", (D(2026, 2, 6), D(2026, 2, 8)) in w("super_bowl", 2026))
    check("Super Bowl 2021 was Feb 7 (first Sunday then)", (D(2021, 2, 5), D(2021, 2, 7)) in w("super_bowl", 2021))
    check("Memorial Day 2026 is May 25", (D(2026, 5, 23), D(2026, 5, 25)) in w("memorial_day", 2026))
    check("Mother's Day 2026 is May 10", (D(2026, 5, 4), D(2026, 5, 10)) in w("mothers_day", 2026))
    check("Prime Day 2025 is Jul 8-11", (D(2025, 7, 8), D(2025, 7, 11)) in w("prime_day", 2025))
    added = H.windows_for("prime_day", D(2027, 1, 1), D(2027, 12, 31),
                          {"dates": {"prime_day": [["2027-07-13", "2027-07-14"]]}})
    check("a Prime Day date added in settings is used", added == [(D(2027, 7, 13), D(2027, 7, 14))], added)

    print("custom periods")
    c = H.validate({"custom": [{"name": "Anniversary sale", "ranges": [["2024-05-10", "2024-05-12"]], "yearly": True}]})
    k = c["custom"][0]["id"]
    got = H.windows_for(k, D(2026, 1, 1), D(2026, 12, 31), c)
    check("a yearly custom period repeats", (D(2026, 5, 10), D(2026, 5, 12)) in got, got)
    for bad, msg in [({"custom": [{"name": "", "ranges": [["2025-01-01", "2025-01-02"]]}]}, "needs a name"),
                     ({"custom": [{"name": "X", "ranges": [["2025-01-05", "2025-01-02"]]}]}, "ends before"),
                     ({"ignored": {"SKU1": ["nope"]}}, "Unknown holiday"),
                     ({"dates": {"black_friday": [["2025-01-01", "2025-01-02"]]}}, "only be added")]:
        try:
            H.validate(bad); ok = False; err = ""
        except H.SettingsError as ex:
            ok, err = msg in str(ex), str(ex)
        check(f"rejected with a message: {msg}", ok, err)
    check("every calendar period is checked automatically", len(H.selected_keys({})) == len(H.CATALOG))
    one = H.for_sku({"ignored": {"SKU1": ["prime_day"]}}, "SKU1")
    check("a period switched off for one product is skipped for that product only",
          "prime_day" not in H.selected_keys(one) and "prime_day" in H.selected_keys(H.for_sku({"ignored": {"SKU1": ["prime_day"]}}, "SKU2")))
    check("a store's own period is checked too", c["custom"][0]["id"] in H.selected_keys(c))
    check("old settings (store type, picked list) are dropped, not an error",
          H.validate({"storeType": "online", "selected": ["black_friday"]}) == {"custom": [], "dates": {}, "ignored": {}})


def _fc(M, sku, events=None, lead=14, cov=30):
    M._catalog[sku]["events"] = list(events or [])
    M._rebuild(M.today(), only=sku)
    return M.get_forecast(sku_id=sku, stock=100, lead_time_days=lead, coverage_days=cov, strategy="balanced",
                          forecast_months=12, units_on_order=0, on_order_eta_days=None, unit_cost=None,
                          fees=0.0, protection=None, tz=None, stock_source="manual", stock_counted_at=None)


def _sum(v, s, e):
    day = lambda p: pd.Timestamp(p["x"], unit="ms").strftime("%Y-%m-%d")
    return sum(p["y"] for p in v["chartDataFuture"] if s <= day(p) <= e) if v else 0.0


def test_backwards_price_and_resync():
    """A product whose own prices read backwards borrows its peers' response, and a live
    store's nightly re-sync keeps each product's planned events and hand-set category."""
    import main as M
    T = M.today().normalize()
    end = T - pd.Timedelta(days=1)
    rows = []

    def add(sku, days, base_fn, price_fn, seed, cat="Shirts"):
        ds = pd.date_range(end - pd.Timedelta(days=days - 1), end)
        p = price_fn(days)
        y = np.random.default_rng(seed).poisson(base_fn(days) * (p / 40.0) ** -1.5)
        for d, v, pp in zip(ds, y, p):
            rows.append({"date": d.strftime("%Y-%m-%d"), "sku": sku, "sku_name": sku, "category": cat,
                         "units_sold": int(v), "price": float(pp)})

    def two_levels(n):
        p = np.full(n, 40.0); p[n // 3: n // 3 + 60] = 32.0
        return p
    for i in range(3):
        add(f"SHIRT{i}", 200, lambda n, i=i: np.full(n, 8.0 + i), two_levels, 40 + i)
    # Sold at $30 through a slow spell, then at $40 once a busy season began: dearer and busier.
    add("BACKWARDS", 200, lambda n: np.where(np.arange(n) < 100, 1.0, 6.0),
        lambda n: np.where(np.arange(n) < 100, 30.0, 40.0), 50)
    # The only product in its category, and it has only ever sold at one price.
    add("GADGET", 200, lambda n: np.full(n, 5.0), lambda n: np.full(n, 40.0), 60, cat="Gadgets")
    M._ingest(pd.DataFrame(rows), "store", append=False, reanchor=False, auto_backtest=False)

    print("a product whose own prices read backwards doesn't use price (and borrows nothing)")
    cut = [{"type": "price_change_temporary", "date": (T + pd.Timedelta(days=2)).strftime("%Y-%m-%d"),
            "end_date": (T + pd.Timedelta(days=8)).strftime("%Y-%m-%d"), "new_price": 30.0}]
    base = _fc(M, "BACKWARDS")
    pr = base.get("priceResponse") or {}
    check("its own prices clear the gate but aren't used",
          pr.get("applies") is False and pr.get("reason") in ("positive-elasticity-ignored", "inverted"), pr)
    check("and the page says why, in plain words", "sold more at its higher price" in (pr.get("text") or ""), pr.get("text"))
    lifted = _fc(M, "BACKWARDS", cut)
    s0 = _sum(base, (T + pd.Timedelta(days=2)).strftime("%Y-%m-%d"), (T + pd.Timedelta(days=8)).strftime("%Y-%m-%d"))
    s1 = _sum(lifted, (T + pd.Timedelta(days=2)).strftime("%Y-%m-%d"), (T + pd.Timedelta(days=8)).strftime("%Y-%m-%d"))
    check("so a price cut doesn't move its forecast", abs(s1 - s0) < 1e-6, (s0, s1))

    print("a product with one price and no category-mates doesn't borrow the store's response either")
    g = _fc(M, "GADGET")
    gp = g.get("priceResponse") or {}
    check("price isn't used", gp.get("applies") is False and gp.get("reason") != "borrowed", gp)

    print("the nightly re-sync keeps what was set on each product")
    M._catalog["SHIRT0"]["events"] = list(cut)
    M._set_attributes_locked({"BACKWARDS": {"category": "Jerseys"}}, 0)
    M._ingest(pd.DataFrame(rows), "store", append=False, reanchor=False, auto_backtest=False)
    check("a planned price change survives the re-sync", M._catalog["SHIRT0"]["events"] == cut,
          M._catalog["SHIRT0"]["events"])
    check("so does a category set by hand", (M._catalog["BACKWARDS"].get("attrs") or {}).get("category") == "Jerseys",
          M._catalog["BACKWARDS"].get("attrs"))
    M._ingest(pd.DataFrame(rows), "sheet.xlsx", append=False, reanchor=True, auto_backtest=False)
    check("a spreadsheet upload still starts clean", not M._catalog["SHIRT0"].get("events"),
          M._catalog["SHIRT0"].get("events"))


if __name__ == "__main__":
    print("\nHolidays\n" + "-" * 44)
    for fn in (test_calendar, test_backwards_price_and_resync):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All holiday tests passed.")

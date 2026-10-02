"""
The status badge, the product page and the exports must describe the same product the
same way. Each check here is a real disagreement that used to happen.

  1. Sales data a few days old made the scorecard count demand for days already past,
     so the badge said "Stockout risk" while the product page said "reorder in 7 days".
  2. Any open PO, however small or late, made a product "Healthy".
  3. A stock count saved by the browser ("2026-09-29T14:03:00.000Z") crashed every later
     forecast for that product (tz-aware vs plain dates).
  4. Exports treated daysUntilReorder == -1 (one day overdue) as "no reorder".

Offline: synthetic forecasts, no model fits.
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("LOGITRACK_PERSIST", "0")
_tmp = tempfile.mkdtemp()
for _k, _f in (("LOGITRACK_CONNECTIONS", "connections.json"), ("LOGITRACK_WORKSPACES", "ws"),
               ("LOGITRACK_LIVE_PRICES_PATH", "live_prices.json")):
    os.environ.setdefault(_k, os.path.join(_tmp, _f))

import pandas as pd

import main as M
import exports

FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(name)


TODAY = M.today()


def entry(last_sale_days_ago=0, rate=5.0, days=200):
    end = TODAY - pd.Timedelta(days=1 + last_sale_days_ago)
    ds = pd.date_range(end - pd.Timedelta(days=days - 1), end)
    df = pd.DataFrame({"ds": ds, "y": rate})
    fut = pd.DataFrame({"ds": pd.date_range(end + pd.Timedelta(days=1), periods=120), "yhat": rate})
    return {"df_train": df, "residual_dispersion": 1.0, "dormant": False, "off_season": False}, fut


def score(e, fc, stock, lt=14, po=False, on_order=0, eta=None):
    return M.sc_score_one(e, fc, stock, 5.0, 0.0, 12.0, 30, lt, 30, po, False,
                          on_order, eta, stock_source="manual", sku_id="X")


# 1 ─ stale data
print("scorecard counts demand from today, not from the day after the last sale")
e0, f0 = entry(last_sale_days_ago=0)
e10, f10 = entry(last_sale_days_ago=10)
r0, r10 = score(e0, f0, 100), score(e10, f10, 100)
check("same stock and rate: same cover whether data is current or 10 days old",
      r0["daysOfCover"] == r10["daysOfCover"], (r0["daysOfCover"], r10["daysOfCover"]))
check("so the same status", r0["status"] == r10["status"], (r0["status"], r10["status"]))
check("100 units at 5/day, 14-day lead: reorder in ~6 days, not overdue",
      r10["daysUntilReorder"] is not None and r10["daysUntilReorder"] >= 5, r10["daysUntilReorder"])

# 2 ─ open POs
print("an open PO covers a product only if it's big enough and lands in time")
e, f = entry()
small_late = score(e, f, 30, po=True, on_order=10, eta=40)
check("30 on hand at 5/day, 10 units arriving in 40 days: not Healthy",
      small_late["status"] != "Healthy", small_late["status"])
check("...and it says the order won't cover it",
      "won't cover" in small_late["recommendation"]["action"], small_late["recommendation"])
big_soon = score(e, f, 30, po=True, on_order=400, eta=3)
check("30 on hand, 400 arriving in 3 days: not at risk",
      big_soon["status"] not in ("Stockout risk", "Reorder due"), big_soon["status"])
check("...cover stays on-hand only (a PO in transit isn't stock on the shelf)",
      big_soon["daysOfCover"] == score(e, f, 30)["daysOfCover"])
legacy = score(e, f, 30, po=True, on_order=0)
check("older client sending only 'has a PO': still trusted as before",
      legacy["status"] != "Stockout risk", legacy["status"])

# 3 ─ count timestamps
print("a browser count timestamp never crashes the count-since math")
df = pd.DataFrame({"ds": pd.date_range(TODAY - pd.Timedelta(days=9), periods=9), "y": 2.0})
_prev = M._APP_TZ
try:
    M._APP_TZ = "America/New_York"
    iso = (TODAY - pd.Timedelta(days=4)).strftime("%Y-%m-%d") + "T02:30:00.000Z"
    try:
        got = M.units_sold_since("X", iso, df)
        ok = True
    except Exception as ex:                     # noqa: BLE001
        got, ok = str(ex), False
    check("ISO timestamp with Z is accepted", ok, got)
    if ok:
        want = (TODAY - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        check("02:30 UTC counts as the previous day in New York", got["since"] == want,
              (got["since"], want))
    check("a plain date still works", M.units_sold_since("X", "2026-01-01", df)["since"] == "2026-01-01")
finally:
    M._APP_TZ = _prev

# 4 ─ exports and -1
print("exports: -1 is one day overdue, only None means no reorder")


def view(dr):
    return {"skuId": f"S{dr}", "skuName": f"S{dr}", "daysUntilReorder": dr, "daysUntilStockout": 13,
            "reorderTimestamp": None, "stockoutTimestamp": None, "orderQty": 40, "__stock": 30,
            "currentMonth": {}, "protection": {}, "monthCards": []}


wb = exports.build_fleet_workbook([{"view": view(-1), "folder": None}, {"view": view(None), "folder": None}])
names = [ws.title for ws in wb.worksheets]
plan = next((ws for ws in wb.worksheets if "reorder" in ws.title.lower()), None)
cells = [str(c.value) for row in (plan.iter_rows() if plan else []) for c in row if c.value is not None]
check("reorder sheet exists", plan is not None, names)
check("one day overdue is on the reorder sheet", "S-1" in cells)
check("no reorder date is not", "SNone" not in cells)

print(f"\n{'All scorecard-consistency tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: {FAILURES}'}")
sys.exit(1 if FAILURES else 0)

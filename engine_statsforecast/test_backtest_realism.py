"""
test_backtest_realism.py — backlog batch 10.

  1. Each test window is forecast with the price changes and promotions it actually had
     (built from its recorded price / on_promotion), in the app's event shapes.
  2. Today's shelf price reaches the backtest's money: new runs value windows at it, and
     a price that moves re-prices the stored run without refitting.
  3. Cost is today's too (the dashboard's, else the latest in the data), so price and
     cost come from the same moment.

Run:  python test_backtest_realism.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import warnings
import logging

os.environ.setdefault("LOGITRACK_PERSIST", "0")
_tmp = tempfile.mkdtemp()
for _k, _f in (("LOGITRACK_CONNECTIONS", "connections.json"), ("LOGITRACK_WORKSPACES", "ws"),
               ("LOGITRACK_LIVE_PRICES_PATH", "live_prices.json")):
    os.environ.setdefault(_k, os.path.join(_tmp, _f))
warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np          # noqa: E402
import pandas as pd         # noqa: E402
import main as M            # noqa: E402
import backtest as BT       # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILURES.append(name)


# 1 ─────────────────────────────────────────────────────────────────────────────
print("1. a window's own promotions and price changes")
ds = pd.date_range("2025-01-01", periods=60)
price = np.full(60, 20.0)
promo = np.zeros(60)
price[35:40] = 15.0; promo[35:40] = 1          # a 25%-off promotion
price[45:48] = 22.0                             # a short price rise
price[52:] = 24.0                               # a price rise that sticks
g = pd.DataFrame({"ds": ds, "y": 5.0, "price": price, "on_promotion": promo})
evs = BT._window_events(g, ds[29], ds[59])
by = {e["type"]: e for e in evs}
p = by.get("promotion") or {}
check("the promotion", p.get("date") == "2025-02-05" and p.get("end_date") == "2025-02-09", p)
check("with the discount the prices show", abs((p.get("discount_pct") or 0) - 25.0) < 0.2, p)
t = by.get("price_change_temporary") or {}
check("the short rise is a temporary change", t.get("new_price") == 22.0 and t.get("end_date") == "2025-02-17", t)
pm = by.get("price_change_permanent") or {}
check("the rise that lasts is permanent", pm.get("new_price") == 24.0 and pm.get("date") == "2025-02-22", pm)
check("nothing from before the window", all(e["date"] > "2025-01-30" for e in evs), evs)
check("the events pass the app's own validation", M._validate_events(evs) == evs)
check("no price or promotion columns: no events",
      BT._window_events(g[["ds", "y"]], ds[29], ds[59]) == [])

rng = np.random.default_rng(5)
n = 260
days = pd.date_range("2024-06-01", periods=n)
pr = np.full(n, 20.0); pm_ = np.zeros(n)
pr[200:205] = 15.0; pm_[200:205] = 1
sku = pd.DataFrame({"ds": days, "y": rng.poisson(6, n).astype(float), "price": pr,
                    "on_promotion": pm_, "sku": "P"})
seen = {}
_real = BT.build_entry


def spy(*a, **k):
    seen["events"] = a[6]
    return _real(*a, **k)


BT.build_entry = spy
cut = days[190]
rows = BT._run_cutoff(sku, "P", sku, cut, 44, [(14, 30)], 95, {"price": 25.0, "cost": 9.0}, {})
BT.build_entry = _real
check("the window's forecast was given its promotion",
      any(e["type"] == "promotion" for e in (seen.get("events") or [])), seen.get("events"))

# 2/3 ───────────────────────────────────────────────────────────────────────────
print("\n2. today's price, 3. today's cost")
r = (rows or [{}])[0]
check("windows are valued at today's shelf price", r.get("price") == 25.0, r.get("price"))
check("with the dashboard cost", r.get("unitCost") == 9.0, r)
check("margin = price - cost - fees", abs(r.get("marginUnit", 0) - 16.0) < 1e-6, r.get("marginUnit"))
sku_c = sku.assign(cost=np.where(np.arange(n) < 150, 6.0, np.where(np.arange(n) < 230, 7.0, 8.0)))
rows_c = BT._run_cutoff(sku_c, "P", sku_c, cut, 44, [(14, 30)], 95, {"price": 25.0, "cost": 9.0}, {})
check("the dashboard cost wins over the data's cost history", rows_c[0]["unitCost"] == 9.0, rows_c[0])
rows_d = BT._run_cutoff(sku_c, "P", sku_c, cut, 44, [(14, 30)], 95, {"price": 25.0}, {})
check("with no dashboard cost, the LATEST cost in the data (8), not the window's (7)",
      rows_d[0]["unitCost"] == 8.0, rows_d[0]["unitCost"])

frame = pd.DataFrame([
    {"sku": "A", "price": 10.0, "unitCost": 4.0, "marginUnit": 5.0, "costKnown": True, "fees": 1.0},
    {"sku": "B", "price": 10.0, "unitCost": 3.0, "marginUnit": 7.0, "costKnown": True, "fees": 0.0},
])
out = BT._apply_costs(frame, {"A": {"price": 12.0}, "B": {"price": 12.0, "cost": 5.0, "fees": 0.5}})
a, b = out.iloc[0], out.iloc[1]
check("re-pricing keeps the cost and fees it had", a["price"] == 12.0 and a["unitCost"] == 4.0 and abs(a["marginUnit"] - 7.0) < 1e-9, dict(a))
check("a new cost replaces the old one on every window", b["unitCost"] == 5.0 and abs(b["marginUnit"] - 6.5) < 1e-9, dict(b))

print("\n   a shelf price that moves re-prices the stored run")
res_rows = []
for i in range(6):
    rr = {"sku": "A", "cutoff": i, "leadDays": 14, "coverageDays": 30, "price": 10.0, "unitCost": 4.0,
          "marginUnit": 6.0, "costKnown": True, "fees": 0.0,
          "bufferTestable": True, "covered": 1.0, "route": "prophet"}
    for pct in BT.Z:
        rr[f"cov_{pct}"] = 1.0; rr[f"lost_{pct}"] = 1.0 if pct < 98 else 0.0; rr[f"safety_{pct}"] = pct / 20
    res_rows.append(rr)
M._last_backtest_rows = pd.DataFrame(res_rows)
M._last_backtest = {"tested": 1, "tierAnalysis": BT._tier_summary(M._last_backtest_rows, 30, 0.25)}
M._last_backtest_combos = [(14, 30)]
M._backtest_inputs.clear(); M._backtest_inputs.update({"holdingPct": 25, "costs": {"A": 4.0}, "fees": {"A": 0.0}})
M._publish_cache({"A": {"regular_price": 10.0}})
check("an unchanged price does nothing", M._reprice_backtest_if_price_moved() is False)
M._publish_cache({"A": {"regular_price": 14.0}})
check("a moved price re-prices", M._reprice_backtest_if_price_moved() is True)
check("the stored windows carry the new price", float(M._last_backtest_rows["price"].iloc[0]) == 14.0,
      M._last_backtest_rows["price"].iloc[0])
check("and the margin with it", abs(float(M._last_backtest_rows["marginUnit"].iloc[0]) - 10.0) < 1e-9)
check("the report says it was re-priced", (M._last_backtest or {}).get("trigger") == "reprice")
check("and doesn't repeat for the same price", M._reprice_backtest_if_price_moved() is False)

print(f"\n{'All backtest realism tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)

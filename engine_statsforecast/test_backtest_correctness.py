"""
test_backtest_correctness.py — backlog batch 9.

  1. A product whose windows can't judge the buffer gets NO best tier (it used to get a
     "measured" 90%: the mean of nothing came back as 0 cost at every tier).
  2. The test window holds the whole decision (lead + coverage), not a fixed 44 days.
  3. Peers as of a cutoff keep their price columns.
  4. Scoring grades the days training keeps: blank stock = in stock, sell-out days are
     graded, only days with nothing to sell are left out.
  5. Calibration re-fits see relatives only up to their own cutoff, and keep the forced
     route of the fit they calibrate.

Run:  python test_backtest_correctness.py
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


def rows(sku, n, testable, cost=4.0, price=10.0):
    out = []
    for i in range(n):
        r = {"sku": sku, "cutoff": i, "costKnown": True, "unitCost": cost, "price": price,
             "marginUnit": price - cost, "leadDays": 14, "coverageDays": 30,
             "bufferTestable": testable, "covered": 1.0 if testable else np.nan}
        for pct in BT.Z:
            if testable:
                r[f"cov_{pct}"] = 1.0 if pct >= 95 else 0.0
                r[f"lost_{pct}"] = 0.0 if pct >= 95 else 3.0
                r[f"safety_{pct}"] = float(pct) / 10
            else:
                r[f"cov_{pct}"] = r[f"lost_{pct}"] = r[f"safety_{pct}"] = np.nan
        out.append(r)
    return out


# 1 ─────────────────────────────────────────────────────────────────────────────
print("1. windows that can't judge the buffer")
frame = pd.DataFrame(rows("DARK", 6, False))
ts = BT._tier_summary(frame, 30, 0.25)
row = ts["bySku"][0]
check("no best tier for a product with no testable window", row["bestTier"] is None, row["bestTier"])
check("its window count is the testable ones (0)", row["windows"] == 0 and row["windowsTotal"] == 6, row)
check("it's named as untestable", ts.get("untestableSkus") == ["DARK"], ts.get("untestableSkus"))
check("no catalog-wide best tier from it", ts["bestTier"] is None, ts["bestTier"])
check("no money figures made of nothing", all(t["totalCost"] is None for t in ts["tiers"]), ts["tiers"][0])
frame = pd.DataFrame(rows("DARK", 6, False) + rows("LIT", 6, True))
ts = BT._tier_summary(frame, 30, 0.25)
lit = next(r for r in ts["bySku"] if r["sku"] == "LIT")
dark = next(r for r in ts["bySku"] if r["sku"] == "DARK")
check("a testable product still gets its best tier", lit["bestTier"] is not None, lit)
check("... and the untestable one still doesn't", dark["bestTier"] is None, dark)
check("the catalog totals come from the testable product only", ts["costedSkus"] == 1, ts["costedSkus"])
M._backtest_tier_cache = {}
M._cache_backtest_tiers({"params": {"lead": 14, "coverage": 30}, "tierAnalysis": ts,
                         "tierAnalysisByCombo": {"14/30": ts}})
keys = sorted(M._backtest_tier_cache)
check("the app caches no measured tier for the untestable product",
      not any(k.startswith("DARK|") for k in keys), keys)
M._record_backtest_exclusions({"tierAnalysis": ts})
check("and says why", "0 usable test window" in (M._backtest_exclusions.get("DARK") or ""),
      M._backtest_exclusions.get("DARK"))

# 2 ─────────────────────────────────────────────────────────────────────────────
print("\n2. the test window holds lead + coverage")
check("default 14 + 30 stays 44", BT.effective_horizon(44, [(14, 30)]) == 44)
check("21 + 45 needs 66", BT.effective_horizon(44, [(14, 30), (21, 45)]) == 66)
check("never shorter than asked", BT.effective_horizon(90, [(14, 30)]) == 90)

# 3 ─────────────────────────────────────────────────────────────────────────────
print("\n3. peers as of a cutoff keep their prices")
days = pd.date_range("2025-01-01", periods=200)
long = pd.concat([pd.DataFrame({"sku": s, "ds": days, "y": np.random.default_rng(i).poisson(4, 200),
                                "price": 10.0 + i, "on_promotion": 0, "price_mixed": 0,
                                "category": "Mugs"})
                  for i, s in enumerate(("A", "B", "C"))], ignore_index=True)
catalog, *_ = BT._pool_state_as_of(long, days[150])
cols = set(catalog["A"]["df"].columns)
check("price, on_promotion and price_mixed are kept", {"price", "on_promotion", "price_mixed"} <= cols, cols)
check("nothing after the cutoff", catalog["A"]["df"]["ds"].max() <= days[150])

# 4 ─────────────────────────────────────────────────────────────────────────────
print("\n4. which days are graded")
g = pd.DataFrame({"ds": pd.date_range("2025-03-01", periods=6),
                  "y": [5, 7, 0, 0, 4, 3],
                  "units_in_stock": [10, 0, 0, 0, 12, 9]})
kept = BT._scoreable_days(g)
kd = list(kept["ds"].dt.day)
check("a sell-out day (sold 7, closed at 0) is graded", 2 in kd, kd)
check("days that opened empty and sold nothing are not", 3 not in kd and 4 not in kd, kd)
check("normal days are", {1, 5, 6} <= set(kd), kd)
blank = g.assign(units_in_stock=np.nan)
check("a product with no stock figures keeps every day", len(BT._scoreable_days(blank)) == 6)
mixed = g.assign(units_in_stock=[np.nan, np.nan, 4, 4, np.nan, np.nan])
check("blank cells count as in stock", len(BT._scoreable_days(mixed)) == 6)

# 5 ─────────────────────────────────────────────────────────────────────────────
print("\n5. calibration re-fits")
seen = []
_orig_fit = M.GlobalPooledEngine.fit


def spy(self, df, today, related, **k):
    mx = max((pd.to_datetime(r["ds"]).max() for r in related if hasattr(r, "columns") and len(r)), default=None)
    seen.append((pd.Timestamp(today), mx))
    return _orig_fit(self, df, today, related, **k)


M.GlobalPooledEngine.fit = spy
hist = pd.date_range("2024-06-01", periods=400)
rng = np.random.default_rng(3)
own = pd.DataFrame({"ds": hist, "y": rng.poisson(5, 400).astype(float)})
rel = [pd.DataFrame({"ds": hist, "y": rng.poisson(5, 400).astype(float)}) for _ in range(3)]
cut = hist[-1]
try:
    M.build_entry("T", "T", own, "uploaded", "t", cut, [], rel, 3, force_route="global")
except Exception as ex:                                     # noqa: BLE001
    check("build_entry runs", False, f"{type(ex).__name__}: {ex}")
M.GlobalPooledEngine.fit = _orig_fit
calib = [s for s in seen if s[0] < cut]
check("the calibration windows were fitted with the forced route", len(calib) >= 1, seen)
check("each saw its relatives only up to its own cutoff",
      all(mx is None or mx <= today for today, mx in seen), seen)

print(f"\n{'All backtest correctness tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)

"""
test_labels.py — the words the Grouping tab, the Holidays panel and the Fleet use for what
the tool is actually doing.

  · Out of season is not dormant or dead: a Christmas line in September, winter gear in
    summer. A fad that has faded, or a line that simply stopped, still is.
  · A line whose past year's sales would clear its stock is over target cover, not dead.
  · The Holidays panel says who a holiday pattern is steadied by.

Run:  python test_labels.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import warnings

_TMP = tempfile.mkdtemp()
os.environ.update({
    "LOGITRACK_CONNECTIONS": os.path.join(_TMP, "c.json"), "LOGITRACK_WORKSPACES": os.path.join(_TMP, "w"),
    "LOGITRACK_SYNC_STATE": os.path.join(_TMP, "s.json"), "LOGITRACK_LIVE_PRICES_PATH": os.path.join(_TMP, "lp.json"),
    "LOGITRACK_HOLIDAYS_PATH": os.path.join(_TMP, "h.json"), "LOGITRACK_CLOSED_PATH": os.path.join(_TMP, "cd.json"),
    "LOGITRACK_SYNC_ENABLED": "0", "LOGITRACK_SAMPLE_ENABLED": "0", "LOGITRACK_PERSIST": "0",
})
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np            # noqa: E402
import pandas as pd           # noqa: E402
import forecast_engine as FE  # noqa: E402
import main as M              # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILURES.append(name)


rng = np.random.default_rng(4)
DAYS = pd.date_range("2023-07-01", "2026-09-27")
ASOF = pd.Timestamp("2026-09-28")


def series(rate_fn):
    return pd.DataFrame({"ds": DAYS, "y": rng.poisson([rate_fn(d) for d in DAYS]).astype(float)})


xmas = series(lambda d: 6.0 if d.month in (11, 12) else 0.0)
winter = series(lambda d: 4.0 if d.month in (11, 12, 1, 2, 3) else 0.2)
fad = series(lambda d: 8.0 if d < pd.Timestamp("2025-06-01") else (0.3 if d < pd.Timestamp("2026-06-01") else 0.0))
stopped = series(lambda d: 3.0 if d < pd.Timestamp("2026-07-01") else 0.0)
steady = series(lambda d: 3.0)


def off(df):
    return FE.is_off_season(df["ds"], df["y"].to_numpy(float), ASOF)


print("out of season, or gone")
check("a Christmas line in September is out of season", off(xmas))
check("so is winter gear", off(winter))
check("a fad that faded is not", not off(fad))
check("nor a line that simply stopped selling in July", not off(stopped))
check("nor a steady seller", not off(steady))
check("too short a history to tell: not called out of season", not off(xmas.tail(300)))

print("\nthe status ladder")
args = dict(c=900, dur=500, st=0.0, lt=14, cov=30, po=False)
check("sold nothing lately, 20x its target cover: dead stock", M.sc_status(**args) == "Dead stock")
check("...unless it's out of season", M.sc_status(**args, off_season=True) == "Overstocked")
check("...or its past year's sales would clear the stock", M.sc_status(**args, sells_in_a_year=True) == "Overstocked")
check("a line whose last year sold 3x its stock sells through in a year", M._sells_in_a_year(steady, 300))
check("not a fading one", not M._sells_in_a_year(fad, 50))
check("not one holding more than a year's sales", not M._sells_in_a_year(steady, 5000))

print("\nwho a holiday pattern is steadied by")
check("the category, each counting equally",
      M._hs_who(5, {"scope": "category", "category": "Apparel"}) == "5 other Apparel products, each counting equally")
check("one other product: no 'each'", M._hs_who(1, {"scope": "category", "category": "Decor"}) == "1 other Decor product")
check("a lone product: store products that move with it, and why",
      M._hs_who(3, {"scope": "store", "category": "Auto"}) ==
      "3 products elsewhere in the store that move with it (it's the only Auto product), each counting equally")
check("a product with no category", "it has no category" in M._hs_who(2, {"scope": "store", "category": None}))
check("unknown (a backtest): similar products", M._hs_who(4, None) == "4 similar products")

print(f"\n{'All label tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)

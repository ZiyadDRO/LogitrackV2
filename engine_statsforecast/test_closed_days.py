"""
test_closed_days.py — a closed day is a day that didn't happen.

The promise: once a day is marked closed it's out of every history the forecast learns
from (past ones included), it's forecast at zero when it comes round, and nothing is
marked without being asked: days that look closed are only ever offered.

Run:  python test_closed_days.py
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import tempfile
import warnings

_TMP = tempfile.mkdtemp()
os.environ.update({
    "LOGITRACK_CONNECTIONS": os.path.join(_TMP, "connections.json"),
    "LOGITRACK_WORKSPACES": os.path.join(_TMP, "workspaces"),
    "LOGITRACK_SYNC_STATE": os.path.join(_TMP, "sync_state.json"),
    "LOGITRACK_LIVE_PRICES_PATH": os.path.join(_TMP, "lp.json"),
    "LOGITRACK_HOLIDAYS_PATH": os.path.join(_TMP, "hol.json"),
    "LOGITRACK_CLOSED_PATH": os.path.join(_TMP, "closed.json"),
    "LOGITRACK_SYNC_ENABLED": "0", "LOGITRACK_SAMPLE_ENABLED": "0", "LOGITRACK_PERSIST": "1",
})
warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np            # noqa: E402
import pandas as pd           # noqa: E402
import closed_days as CD      # noqa: E402
import catalog_store as _CS   # noqa: E402
import backtest_store as _BS  # noqa: E402
_CS._DIR = os.path.join(_TMP, "catalog_store")
_BS._DIR = os.path.join(_TMP, "backtest_store")
import main as M              # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILURES.append(name)


print("the calendar itself")
check("Thanksgiving moves with the year", CD._day_of("thanksgiving", 2025) == dt.date(2025, 11, 27)
      and CD._day_of("thanksgiving", 2026) == dt.date(2026, 11, 26))
check("so does Easter", CD._day_of("easter", 2026) == dt.date(2026, 4, 5))
st = CD.validate({"dates": ["2025-03-02", "2025-03-02", "2024-07-09"], "yearly": ["christmas_day"]})
check("dates are cleaned and de-duplicated", st["dates"] == ["2024-07-09", "2025-03-02"], st)
days = CD.expand(st, dt.date(2024, 1, 1), dt.date(2025, 12, 31))
check("single days and every-year days expand together",
      days == {dt.date(2024, 7, 9), dt.date(2025, 3, 2), dt.date(2024, 12, 25), dt.date(2025, 12, 25)}, sorted(days))
for bad in ({"dates": ["not a date"]}, {"yearly": ["arbor_day"]}):
    try:
        CD.validate(bad)
        check(f"rejects {bad}", False)
    except CD.SettingsError:
        check(f"rejects {bad}", True)

print("\na store closed every Christmas and Thanksgiving")
END = pd.Timestamp(M.today()).normalize() - pd.Timedelta(days=1)
DAYS = pd.date_range(END - pd.Timedelta(days=3 * 365), END)
rng = np.random.default_rng(3)
shut = {CD._day_of(k, y) for k in ("christmas_day", "thanksgiving") for y in range(DAYS[0].year, DAYS[-1].year + 1)}
rows = []
for sku, lam in (("A", 9.0), ("B", 6.0), ("C", 4.0)):
    y = rng.poisson(lam, len(DAYS)).astype(float)
    for d, v in zip(DAYS, y):
        rows.append({"Date": d, "SKU": sku, "SKU_Name": sku, "Units_Sold": 0.0 if d.date() in shut else v})
with M._state_lock:
    M._ingest(pd.DataFrame(rows), "shop.xlsx", append=False, auto_backtest=False)

v = M.get_closed_days()
past_shut = sorted(d.isoformat() for d in shut if d <= END.date())
check("the days nothing sold anywhere are offered", v["suggestions"] == past_shut, (v["suggestions"], past_shut))
check("...and the holidays it was shut on every year", set(v["yearlyHints"]) == {"christmas_day", "thanksgiving"},
      v["yearlyHints"])
check("but nothing is marked on its own", v["settings"] == {"dates": [], "yearly": []}, v["settings"])


def entry(sku):
    return M._cache()[sku]


def ahead(sku, day):
    f = entry(sku)["forecast"]
    r = f[pd.to_datetime(f["ds"]).dt.date == day]
    return float(r["yhat"].iloc[0]) if len(r) else None


nxt = CD._day_of("christmas_day", END.year if END.month < 12 or END.day < 25 else END.year + 1)
before_rows = len(entry("A")["df_train"])
_after = ahead("A", nxt + dt.timedelta(days=1)) or 0
check("before marking, past closures already pull next Christmas down (the holiday shape saw them)",
      (ahead("A", nxt) or 0) < 0.5 * _after, (ahead("A", nxt), _after))

res = M.set_closed_days({"dates": [], "yearly": ["christmas_day", "thanksgiving"]})
check("saving starts a refit", res["refitting"] is True)
import time
while M._holiday_refit["status"] == "running":
    time.sleep(1)
check("which finishes cleanly", M._holiday_refit["status"] == "done", M._holiday_refit)
train = entry("A")["df_train"]
gone = set(pd.to_datetime(train["ds"]).dt.date) & shut
check("past closed days are out of the history the forecast learns from", not gone, sorted(gone))
check("and only those", before_rows - len(train) == len(past_shut), (before_rows, len(train), len(past_shut)))
check("next Christmas is forecast at zero", ahead("A", nxt) == 0.0, ahead("A", nxt))
check("the day after isn't", (ahead("A", nxt + dt.timedelta(days=1)) or 0) > 1.0, ahead("A", nxt + dt.timedelta(days=1)))
_hr = entry("A").get("holidays") or {}
_xmas = next((r for r in _hr.get("effects") or [] if r["key"] == "christmas_newyear"), None)
check("the Holidays tab shows the Christmas stretch, with next Christmas closed",
      _hr.get("method") == "shape" and _xmas is not None
      and any(d["closed"] and d["units"] == 0 for d in _xmas["days"] if d["date"] == nxt.isoformat()),
      (_hr.get("method"), _xmas and [d for d in _xmas["days"] if d["date"] == nxt.isoformat()]))
check("its total is the forecast's own units for those days",
      _xmas is not None and abs(_xmas["forecast"]["units"] - sum(d["units"] or 0 for d in _xmas["days"])) < 0.6)
check("nothing left to suggest", M.get_closed_days()["suggestions"] == [], M.get_closed_days()["suggestions"])
check("the recorded sales themselves are untouched", len(M._catalog["A"]["df"]) == len(DAYS))

print("\na single future day off")
day_off = (END + pd.Timedelta(days=10)).date()
M.set_closed_days({"dates": [day_off.isoformat()], "yearly": ["christmas_day", "thanksgiving"]})
while M._holiday_refit["status"] == "running":
    time.sleep(1)
check("is forecast at zero", ahead("B", day_off) == 0.0, ahead("B", day_off))
check("the days either side aren't", (ahead("B", day_off - dt.timedelta(days=1)) or 0) > 0.5
      and (ahead("B", day_off + dt.timedelta(days=1)) or 0) > 0.5)

print("\nan uploaded sheet whose history was moved")
M._closed_memo_real["key"] = None
df = pd.DataFrame({"ds": pd.to_datetime(["2025-12-30", "2025-12-31", "2026-01-01"]), "y": [1.0, 2.0, 3.0]})
M._closed.save(M._current_slot(), {"dates": ["2025-12-25"], "yearly": []})
kept = M._drop_closed(df, 6)          # sold on Dec 24-26, shown 6 days later
check("its closed day is found where the sale really happened",
      list(kept["y"]) == [1.0, 3.0], list(kept["y"]))

print(f"\n{'All closed-day tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)

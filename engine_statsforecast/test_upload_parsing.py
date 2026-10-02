"""
The upload must load exactly what the file says. Each check is a way it used to quietly
load something else:

  1. identical rows were dropped one by one, so a one-row-per-sale file lost units
  2. (but a whole file pasted in twice must still not double every sale)
  3. day-first dates (13/02/2024) were swapped or dropped
  4. a mostly unreadable Date column loaded a fraction of the history without complaint
  5. a blank stock cell became "1 unit in stock, from your file"
  6. SKU codes were read as numbers: 00123 became 123

Runs the real endpoint with small files; fits are tiny.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
_tmp = tempfile.mkdtemp()
os.environ["LOGITRACK_PERSIST"] = "0"
for _k, _f in (("LOGITRACK_CONNECTIONS", "connections.json"), ("LOGITRACK_WORKSPACES", "ws"),
               ("LOGITRACK_LIVE_PRICES_PATH", "live_prices.json"),
               ("LOGITRACK_CLOSED_PATH", "closed_days.json")):
    os.environ[_k] = os.path.join(_tmp, _f)

import logging
for _n in ("cmdstanpy", "prophet"):
    logging.getLogger(_n).disabled = True

import pandas as pd
from fastapi.testclient import TestClient

import main as M

FAILURES: list[str] = []
client = TestClient(M.app)


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(name)


def upload(csv_text, name="t.csv"):
    r = client.post("/api/upload?mode=replace",
                    files={"file": (name, io.BytesIO(csv_text.encode()), "text/csv")},
                    params={"auto_backtest": "false"})
    return r


def total_units(sid):
    e = M._catalog.get(sid)
    return None if e is None else float(e["df"]["y"].sum())


days = pd.date_range("2025-01-01", periods=40)

print("one row per sale")
rows = ["Date,SKU,Units_Sold,Price"]
for d in days:
    rows += [f"{d:%Y-%m-%d},MUG,1,9.99"] * 3
r = upload("\n".join(rows))
check("upload accepted", r.status_code == 200, r.text[:200])
check("3 identical sales a day for 40 days = 120 units (was 40)", total_units("MUG") == 120,
      total_units("MUG"))

print("a whole file pasted in twice")
rows = ["Date,SKU,Units_Sold"] + [f"{d:%Y-%m-%d},CUP,{i % 5 + 1}" for i, d in enumerate(days)]
once = sum(i % 5 + 1 for i in range(len(days)))
r = upload("\n".join(rows + rows[1:]))
check("not doubled", total_units("CUP") == once, (total_units("CUP"), once))
check("and it says so", (r.json().get("dataQuality") or {}).get("fileRepeatedTimes") == 2,
      r.json().get("dataQuality"))

print("day-first dates")
feb = pd.date_range("2024-02-01", "2024-02-28")
rows = ["Date,SKU,Units_Sold"] + [f"{d:%d/%m/%Y},TEA,2" for d in feb]
r = upload("\n".join(rows))
dq = r.json().get("dataQuality") or {}
check("every row kept (13/02 and later used to be dropped)", total_units("TEA") == 2 * len(feb),
      (total_units("TEA"), dq))
check("and the note says day/month/year", "day/month" in (dq.get("dateFormat") or ""), dq)
e = M._catalog["TEA"]
span = (e["df"]["ds"].max() - e["df"]["ds"].min()).days
check("read as one month of consecutive days, not scattered across the year", span == len(feb) - 1, span)

print("a mostly unreadable Date column is refused, not half-loaded")
rows = ["Date,SKU,Units_Sold"] + [f"{'soon' if i % 2 else f'2024-03-{i + 1:02d}'},JAM,1" for i in range(20)]
r = upload("\n".join(rows))
check("400 with a plain message", r.status_code == 400 and "YYYY-MM-DD" in r.text, (r.status_code, r.text[:150]))

print("blank stock cells")
rows = ["Date,SKU,Units_Sold,Units_In_Stock"]
for i, d in enumerate(days):
    rows.append(f"{d:%Y-%m-%d},HAT,1,{'' if i >= 35 else 50 - i}")      # last 5 rows blank
    rows.append(f"{d:%Y-%m-%d},SCARF,1,")                               # never any stock
r = upload("\n".join(rows))
skus = {s["id"]: s for s in client.get("/api/skus").json()}
check("latest REAL figure is kept (16), not a filled-in 1", skus["HAT"]["lastKnownStock"] == 16,
      skus.get("HAT"))
check("a product with no stock figures has none", skus["SCARF"]["lastKnownStock"] is None,
      skus.get("SCARF"))
check("...and isn't marked 'from your file'", skus["SCARF"]["stockSource"] != "sheet", skus.get("SCARF"))

print("SKU codes are text")
rows = ["Date,SKU,Units_Sold"] + [f"{d:%Y-%m-%d},00123,1" for d in days] \
       + [f"{d:%Y-%m-%d}, 4567 ,1" for d in days]
r = upload("\n".join(rows))
check("00123 keeps its leading zeros", "00123" in M._catalog, list(M._catalog)[:5])
check("stray spaces are trimmed", "4567" in M._catalog, list(M._catalog)[:5])

print(f"\n{'All upload-parsing tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: {FAILURES}'}")
sys.exit(1 if FAILURES else 0)

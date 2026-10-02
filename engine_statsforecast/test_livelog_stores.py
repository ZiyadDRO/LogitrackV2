"""
test_livelog_stores.py — the Live accuracy record belongs to a store.

  1. While spreadsheets are loaded and the store is paused, the tab doesn't show the
     store's weeks as though they were about the spreadsheet; it says whose record exists.
  2. Nothing is sealed for the spreadsheets while a store is paused.
  3. Weeks sealed before this change are filed under the store.
  4. Each store's weeks are graded against that store's own sales, even while paused.
  5. Two stores with the same product code keep a record each.

Run:  python test_livelog_stores.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import warnings
import logging

_TMP = tempfile.mkdtemp()
os.environ.update({
    "LOGITRACK_CONNECTIONS": os.path.join(_TMP, "connections.json"),
    "LOGITRACK_WORKSPACES": os.path.join(_TMP, "workspaces"),
    "LOGITRACK_LIVE_PRICES_PATH": os.path.join(_TMP, "lp.json"),
    "LOGITRACK_SYNC_ENABLED": "0", "LOGITRACK_SAMPLE_ENABLED": "0", "LOGITRACK_PERSIST": "1",
})
warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import datetime as dt     # noqa: E402
import forecast_log as FL  # noqa: E402
import main as M           # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILURES.append(name)


M._flog = FL.ForecastLog(os.path.join(_TMP, "flog.json"))
a = M._CONN.upsert("square", "Corner shop", {"accessToken": "A"})["id"]
b = M._CONN.upsert("square", "Market stall", {"accessToken": "B"})["id"]
OLD = dt.date(2026, 8, 2)      # a Sunday, long past: due for grading

print("3. weeks sealed before the change are the store's")
M._flog.snapshot([{"sku": "MUG", "predicted": 10.0, "horizonDays": 7}], origin=OLD)   # untagged
M._WS.set_showing(M._WS.store_slot(a)); M._CONN.set_active(a, True)
r = M.livelog_read(None, 500)
check("they're filed under the store in use", all(e.get("store") == a for e in M._flog.all()),
      [e.get("store") for e in M._flog.all()])
check("and show on its tab", len(r["entries"]) == 1 and r["tracked"] is True, r)

print("\n5. two stores, one product code")
M._flog.snapshot([{"sku": "MUG", "predicted": 30.0, "horizonDays": 7, "store": b}], origin=OLD)
check("a record each", len(M._flog.all()) == 2, [e["id"] for e in M._flog.all()])

print("\n1-2. spreadsheets loaded, store paused")
M._CONN.set_active(a, False)
M._WS.set_showing(M._WS.SHEETS)
r = M.livelog_read(None, 500)
check("no store weeks shown", r["entries"] == [] and r["weeks"] == [], r["entries"][:1])
check("it says it isn't tracking", r["tracked"] is False and r["showing"] == "sheets")
check("and whose record is kept", (r.get("pausedStore") or {}).get("entries") == 1, r.get("pausedStore"))
snap = M.livelog_snapshot({})
check("nothing is sealed for the spreadsheets", snap.get("added") == 0 and snap.get("reason") == "store-paused", snap)

print("\n4. grading, each against its own store")
asked = []


def fake_provider(source, pending, creds, known_skus=None):
    token = creds.get("accessToken")
    asked.append((token, sorted(e["store"] for e in pending)))
    return lambda sku, start, end: {"A": 11.0, "B": 29.0}[token]


M._SRC.actuals_provider = fake_provider
res = M.livelog_score({})
check("both stores' weeks graded while paused", res["scored"] == 2, res)
got = {e["store"]: e["actual"] for e in M._flog.all()}
check("each against its own sales", got == {a: 11.0, b: 29.0}, got)
check("each store's sales read with its own login", sorted(t for t, _ in asked) == ["A", "B"], asked)

print("\n6. a product that sold nothing all week is graded as 0, not left pending")
M._flog.snapshot([{"sku": "DUD", "predicted": 25.0, "lo": 16.0, "hi": 35.0, "horizonDays": 7, "store": a}],
                 origin=OLD)
import live_actuals as LA   # noqa: E402
import pandas as pd         # noqa: E402
frame = pd.DataFrame([{"date": "2026-08-03", "sku": "MUG", "units_sold": 11}])   # DUD never appears
fn = LA.shopify_actuals_provider(M._flog.pending(), fetch=lambda days=None: frame, known_skus={"MUG", "DUD"})
check("a listed product with no sales scores 0", fn("DUD", "2026-08-02", "2026-08-08") == 0.0)
fn2 = LA.shopify_actuals_provider(M._flog.pending(), fetch=lambda days=None: frame)
check("without the product list it stays unknown (the old behaviour)", fn2("DUD", "2026-08-02", "2026-08-08") is None)
def bad(days=None):
    raise RuntimeError("store down")
fn3 = LA.shopify_actuals_provider(M._flog.pending(), fetch=bad, known_skus={"DUD"})
check("a store that can't be read still grades nothing", fn3("DUD", "2026-08-02", "2026-08-08") is None)
wk = FL.by_week([e for e in M._flog.all() if e.get("store") == a])[0]
check("the week compares like with like", wk["predictedScored"] == 10.0 and wk["predicted"] == 35.0, wk)

print("\n7. no second copy of a week already sealed under the old id")
lg = FL.ForecastLog(os.path.join(_TMP, "dup.json"))
lg.snapshot([{"sku": "X", "predicted": 5.0, "horizonDays": 7}], origin=OLD)       # old-style id
lg.claim_untagged(a)
r7 = lg.snapshot([{"sku": "X", "predicted": 1.0, "horizonDays": 7, "store": a}], origin=OLD)
check("re-sealing the week adds nothing", r7["added"] == 0 and len(lg.all()) == 1, [e["id"] for e in lg.all()])
lg._entries["c:X|dup"] = {**lg.all()[0], "id": "c:X|dup", "predicted": 1.0, "createdAt": "2099-01-01"}
check("an existing duplicate is removed", lg.dedupe() == 1 and lg.all()[0]["predicted"] == 5.0,
      [(e["id"], e["predicted"]) for e in lg.all()])

print("\n8. a product no longer in the store")
M._WS.set_showing(M._WS.store_slot(b)); M._CONN.set_active(b, True)
M._flog.snapshot([{"sku": "GONE", "predicted": 4.0, "lo": 4.0, "hi": 8.0, "horizonDays": 7, "store": b}],
                 origin=OLD)
M._store_products = lambda owner: {"MUG"}
rr = M.livelog_read(None, 500)
row = next((e for e in rr["entries"] if e["sku"] == "GONE"), {})
check("is listed as gone", row.get("gone") is True, row)
wk = next((w for w in rr["weeks"] if w["week"] == OLD.isoformat()), {})
check("and left out of the week's figures", wk.get("products") == 1, wk)

print("\nback in use")
M._WS.set_showing(M._WS.store_slot(b)); M._CONN.set_active(b, True)
r = M.livelog_read(None, 500)
check("the Market stall shows only its own weeks", {e["store"] for e in r["entries"]} == {b}
      and [e["actual"] for e in r["entries"] if not e["gone"]] == [29.0], r["entries"])
M.livelog_reset()
check("starting over clears only that store's record", {e["store"] for e in M._flog.all()} == {a},
      [e["store"] for e in M._flog.all()])

print(f"\n{'All live-log store tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)

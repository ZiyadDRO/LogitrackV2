"""
test_workspace.py — pausing a store to look at a spreadsheet, and bringing it back.

The promise being tested: pausing a store never costs its saved login, its products, or
the events and categories set on them, and a paused store never syncs over a spreadsheet.

Run:  python test_workspace.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import warnings
import logging

_TMP = tempfile.mkdtemp()
os.environ.update({
    "LOGITRACK_CONNECTIONS": os.path.join(_TMP, "connections.json"),
    "LOGITRACK_WORKSPACES": os.path.join(_TMP, "workspaces"),
    "LOGITRACK_SYNC_STATE": os.path.join(_TMP, "sync_state.json"),
    "LOGITRACK_LIVE_PRICES_PATH": os.path.join(_TMP, "lp.json"),
    "LOGITRACK_HOLIDAYS_PATH": os.path.join(_TMP, "hol.json"),
    "LOGITRACK_SYNC_ENABLED": "0", "LOGITRACK_SAMPLE_ENABLED": "0", "LOGITRACK_PERSIST": "1",
})
warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np          # noqa: E402
import pandas as pd         # noqa: E402
import catalog_store as _CS  # noqa: E402
import backtest_store as _BS  # noqa: E402
_CS._DIR = os.path.join(_TMP, "catalog_store")
_BS._DIR = os.path.join(_TMP, "backtest_store")
import main as M            # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILURES.append(name)


TOKEN = "shpat_THISMUSTSURVIVEPAUSING1234"
END = pd.Timestamp(M.today()).normalize() - pd.Timedelta(days=1)
DAYS = pd.date_range(END - pd.Timedelta(days=199), END)
rng = np.random.default_rng(1)
FETCHES = []


def store_frame():
    rows = []
    for sku, lam in (("KIOSK-1", 4.0), ("KIOSK-2", 2.0)):
        for d, y in zip(DAYS, rng.poisson(lam, len(DAYS))):
            rows.append({"date": d.strftime("%Y-%m-%d"), "sku": sku, "sku_name": sku.title(),
                         "units_sold": int(y), "price": 10.0, "on_promotion": 0})
    return pd.DataFrame(rows)


def fake_fetch_sales(source, creds, days=None):
    FETCHES.append(dict(creds))
    return store_frame()


M._SRC.fetch_sales = fake_fetch_sales
M._SRC.fetch_catalog_meta = lambda source, creds: {}
M._run_backtest_job = lambda *a, **k: False       # keep the test about workspaces


def wait_restore():
    for _ in range(600):
        if M._restore_state.get("status") != "restoring":
            return
        time.sleep(0.5)


def skus():
    with M._state_lock:
        return sorted(M._catalog)


print("a store connected and imported")
conn = M._CONN.upsert("shopify", "Cousin's kiosk", {"shop": "kiosk.myshopify.com", "token": TOKEN})
cid = conn["id"]
M.upload_from_source({"connectionId": cid})
check("its products are loaded", skus() == ["KIOSK-1", "KIOSK-2"], skus())
check("it's in use", M._CONN.list_all()[0]["active"] is True)
check("the workspace is the store's", M._current_slot() == M._WS.store_slot(cid), M._current_slot())
with M._state_lock:
    M._catalog["KIOSK-1"]["events"] = [{"type": "promotion", "date": "2099-01-01",
                                        "end_date": "2099-01-05", "discount_pct": 20}]
    M._catalog["KIOSK-1"]["attrs"] = {"category": "Snacks"}
    M._catalog["KIOSK-1"]["attrs_set"] = ["category"]
    M._persist_catalog()

print("\npaused, to look at a spreadsheet")
r = M.use_connection(cid, {"active": False})
wait_restore()
check("nothing is loaded yet (no spreadsheets before)", skus() == [], skus())
check("the store is paused", M._CONN.list_all()[0]["active"] is False)
check("its login is still saved", (M._CONN.get(cid) or {}).get("creds", {}).get("token") == TOKEN)
check("its products are set aside, not deleted", M._WS.store_slot(cid) in M._WS.parked(), M._WS.parked())
sheet = pd.DataFrame({"Date": DAYS, "SKU": "SHEET-A", "SKU_Name": "Sheet product",
                      "Units_Sold": rng.poisson(6.0, len(DAYS))})
with M._state_lock:
    M._ingest(sheet, "mock.xlsx", append=False, auto_backtest=False)
check("the spreadsheet loads on its own", skus() == ["SHEET-A"], skus())

print("\nthe nightly sync while paused")
n_before = len(FETCHES)
res = M._run_daily_sync(None, pd.Timestamp(M.today()).date())
check("skips without failing", res.get("ok") is True and res.get("reason") == "store-paused", res)
check("fetches nothing from the store", len(FETCHES) == n_before)
check("the spreadsheet is untouched", skus() == ["SHEET-A"], skus())
check("isn't reported as stale (nothing to catch up from)", M._data_is_stale() is False)
st = M.sync_status()
check("the sync panel says paused", st.get("paused") is True and st.get("connection") is None, st.get("paused"))

print("\nback in use")
r = M.use_connection(cid, {"active": True})
wait_restore()
check("the store's products come straight back", skus() == ["KIOSK-1", "KIOSK-2"], skus())
check("no re-import needed", r["needsImport"] is False and r["restored"] is True, r)
with M._state_lock:
    ev = list(M._catalog["KIOSK-1"].get("events") or [])
    cat = (M._catalog["KIOSK-1"].get("attrs") or {}).get("category")
check("the planned promotion is still there", len(ev) == 1 and ev[0]["discount_pct"] == 20, ev)
check("the category set by hand is still there", cat == "Snacks", cat)
check("the spreadsheet is set aside in turn", M._WS.SHEETS in M._WS.parked(), M._WS.parked())
check("it's in use again", M._CONN.list_all()[0]["active"] is True)

print("\npaused again: the spreadsheet comes back")
M.use_connection(cid, {"active": False})
wait_restore()
check("the spreadsheet is back", skus() == ["SHEET-A"], skus())

print("\nimporting the store by hand while the spreadsheet is loaded")
M.upload_from_source({"connectionId": cid})
check("the store's products are loaded", skus() == ["KIOSK-1", "KIOSK-2"], skus())
with M._state_lock:
    ev = list(M._catalog["KIOSK-1"].get("events") or [])
check("its promotion carried over into the fresh import", len(ev) == 1, ev)
check("the store is in use", M._CONN.list_all()[0]["active"] is True)
check("the spreadsheet was set aside, not lost", M._WS.SHEETS in M._WS.parked(), M._WS.parked())

STATE = os.path.join(_TMP, "workspaces", "state.json")

print("\nan install from before the switch existed (nothing recorded), paused")
os.remove(STATE)
M.use_connection(cid, {"active": False})
wait_restore()
check("the store's products are set aside", M._WS.store_slot(cid) in M._WS.parked(), M._WS.parked())
check("the spreadsheet is back", skus() == ["SHEET-A"], skus())
M.use_connection(cid, {"active": True})
wait_restore()
check("back in use, the store's products are back", skus() == ["KIOSK-1", "KIOSK-2"], skus())

print("\na paused store whose products were left showing, at startup")
os.remove(STATE)
M._CONN.set_active(cid, False)          # what the first version of the switch left behind
M._reconcile_workspace()
M.warmup()
wait_restore()
check("they're set aside at startup", M._WS.store_slot(cid) in M._WS.parked(), M._WS.parked())
check("and the spreadsheet is what shows", skus() == ["SHEET-A"], skus())
check("its login is still saved", (M._CONN.get(cid) or {}).get("creds", {}).get("token") == TOKEN)
M.use_connection(cid, {"active": True})
wait_restore()
check("switched back on, the store's products return", skus() == ["KIOSK-1", "KIOSK-2"], skus())
with M._state_lock:
    ev = list(M._catalog["KIOSK-1"].get("events") or [])
check("with the promotion still on them", len(ev) == 1, ev)

print("\nforgetting the account")
M.use_connection(cid, {"active": False})
wait_restore()
M.delete_connection(cid)
check("its set-aside products go with it", M._WS.store_slot(cid) not in M._WS.parked(), M._WS.parked())
check("the spreadsheet stays loaded", skus() == ["SHEET-A"], skus())

print("\na store connected with nothing loaded (a fresh copy of the project)")
_list_active, _showing = M._CONN.list_active, M._showing_store_id
_saved_cat, _saved_status = dict(M._catalog), M._restore_state.get("status")
try:
    M._CONN.list_active = lambda: [{"id": "fresh", "source": "square"}]
    M._showing_store_id = lambda: "fresh"
    with M._state_lock:
        M._catalog.clear()
    M._restore_state["status"] = "empty"
    check("is stale, so it syncs now instead of sitting empty until tonight", M._data_is_stale() is True)
    M._restore_state["status"] = "restoring"
    check("but not while a saved catalog is still being restored", M._data_is_stale() is False)
finally:
    M._CONN.list_active, M._showing_store_id = _list_active, _showing
    with M._state_lock:
        M._catalog.update(_saved_cat)
    M._restore_state["status"] = _saved_status

print(f"\n{'All workspace tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)

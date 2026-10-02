"""
test_store_switching.py — switching between stores and spreadsheets never loses or mixes data.

Backlog batch 6. Each block fails on the code before the fix:
  1. connecting a second store while one is in use keeps the first store's products
  2. deleting the store that is showing records the spreadsheets as showing
  3. a spreadsheet upload while a store is in use pauses the store and loads on its own
  4. re-importing a paused store keeps its measured protection levels
  5. a backtest that finishes after a switch is thrown away, not saved into the new one
  6. a paused store's live price / stock never reach a same-code spreadsheet product

Run:  python test_store_switching.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
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
    "LOGITRACK_STOCK_LOG": os.path.join(_TMP, "stock_log.json"),
    "LOGITRACK_TODAY_PATH": os.path.join(_TMP, "today.json"),
    "LOGITRACK_CLOSED_PATH": os.path.join(_TMP, "closed.json"),
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
from fastapi.testclient import TestClient  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILURES.append(name)


END = pd.Timestamp(M.today()).normalize() - pd.Timedelta(days=1)
DAYS = pd.date_range(END - pd.Timedelta(days=199), END)


def store_frame(prefix):
    rng = np.random.default_rng(abs(hash(prefix)) % 1000)
    rows = []
    for sku, lam in ((f"{prefix}-1", 4.0), (f"{prefix}-2", 2.0), ("SHARED", 3.0)):
        for d, y in zip(DAYS, rng.poisson(lam, len(DAYS))):
            rows.append({"date": d.strftime("%Y-%m-%d"), "sku": sku, "sku_name": sku.title(),
                         "units_sold": int(y), "price": 10.0, "on_promotion": 0})
    return pd.DataFrame(rows)


FRAMES = {}


def fake_fetch_sales(source, creds, days=None):
    shop = creds.get("shop", "x")
    if shop not in FRAMES:
        FRAMES[shop] = store_frame(shop.split(".")[0].upper())
    return FRAMES[shop].copy()


M._SRC.fetch_sales = fake_fetch_sales
M._SRC.fetch_catalog_meta = lambda source, creds: {}
_real_job = M._run_backtest_job
M._run_backtest_job = lambda *a, **k: False       # the upload's own run is not under test


def wait_restore():
    for _ in range(600):
        if M._restore_state.get("status") != "restoring":
            return
        time.sleep(0.5)


def skus():
    with M._state_lock:
        return sorted(M._catalog)


def active_ids():
    return [c["id"] for c in M._CONN.list_all() if c.get("active")]


def sheet_df(code="SHEET-A"):
    rng = np.random.default_rng(7)
    return pd.DataFrame({"Date": DAYS.strftime("%Y-%m-%d"), "SKU": code, "SKU_Name": "Sheet product",
                         "Units_Sold": rng.poisson(6.0, len(DAYS))})


client = TestClient(M.app)


def upload_sheet(df, mode="replace"):
    import io
    buf = io.StringIO(); df.to_csv(buf, index=False)
    r = client.post(f"/api/upload?mode={mode}",
                    files={"file": ("sheet.csv", buf.getvalue().encode(), "text/csv")})
    return r


# ── 1 ─────────────────────────────────────────────────────────────────────────
print("1. connecting a second store while one is in use")
M.upload_from_source({"source": "shopify", "creds": {"shop": "alpha.myshopify.com", "token": "a"},
                      "save": True, "label": "Alpha"})
a_id = active_ids()[0]
check("store A loaded", skus() == ["ALPHA-1", "ALPHA-2", "SHARED"], skus())
with M._state_lock:
    M._catalog["SHARED"]["events"] = [{"type": "promotion", "date": "2099-01-01",
                                       "end_date": "2099-01-05", "discount_pct": 20}]
    M._persist_catalog()
res = M.upload_from_source({"source": "shopify", "creds": {"shop": "beta.myshopify.com", "token": "b"},
                            "save": True, "label": "Beta"})
b_id = res["connection"]["id"]
check("store B loaded on its own", skus() == ["BETA-1", "BETA-2", "SHARED"], skus())
check("store A's products are set aside", M._WS.store_slot(a_id) in M._WS.parked(), M._WS.parked())
check("B is in use, A paused", active_ids() == [b_id], active_ids())
check("B's workspace is showing", M._current_slot() == M._WS.store_slot(b_id), M._current_slot())
with M._state_lock:
    ev = M._catalog["SHARED"].get("events") or []
check("A's planned promotion did not carry onto B's same-code product", ev == [], ev)
M.use_connection(a_id, {"active": True}); wait_restore()
check("switching back brings A's products", skus() == ["ALPHA-1", "ALPHA-2", "SHARED"], skus())
with M._state_lock:
    ev = M._catalog["SHARED"].get("events") or []
check("with A's promotion", len(ev) == 1, ev)

# ── 3 ─────────────────────────────────────────────────────────────────────────
print("\n3. a spreadsheet upload while a store is in use")
r = upload_sheet(sheet_df())
check("upload succeeds", r.status_code == 200, r.text[:300])
body = r.json() if r.status_code == 200 else {}
check("says which store was paused", body.get("storePaused") == "Alpha", body.get("storePaused"))
check("the sheet loads on its own", skus() == ["SHEET-A"], skus())
check("the store is paused", active_ids() == [], active_ids())
check("its products are set aside", M._WS.store_slot(a_id) in M._WS.parked(), M._WS.parked())
check("spreadsheets are what's showing", M._current_slot() == M._WS.SHEETS, M._current_slot())
M.use_connection(a_id, {"active": True}); wait_restore()
r = upload_sheet(sheet_df("SHEET-B"), mode="append")
check("an 'add to loaded' upload joins the spreadsheets, not the store",
      skus() == ["SHEET-A", "SHEET-B"], skus())
M.use_connection(a_id, {"active": True}); wait_restore()
check("the store comes back untouched", skus() == ["ALPHA-1", "ALPHA-2", "SHARED"], skus())

# ── 4 ─────────────────────────────────────────────────────────────────────────
print("\n4. re-importing a paused store keeps its measured protection levels")
TIER = {"bestTier": "Standard", "lead": 14, "coverage": 30, "source": "measured"}
with M._state_lock:
    M._backtest_tier_cache = {"ALPHA-1|14|30": dict(TIER)}
    M._last_backtest = {"tested": 3}
    M._last_backtest_rows = None
    M._persist_backtest()
M.use_connection(a_id, {"active": False}); wait_restore()
check("spreadsheets back", skus() == ["SHEET-A", "SHEET-B"], skus())
with M._state_lock:      # the spreadsheets have a run of their own in memory
    M._backtest_tier_cache = {"SHEET-A|14|30": dict(TIER)}
    M._persist_backtest()
M.upload_from_source({"connectionId": a_id})
with M._state_lock:
    keys = sorted(M._backtest_tier_cache)
check("A's measured tier is back after the import", "ALPHA-1|14|30" in keys, keys)
check("the spreadsheets' tier is not carried over", "SHEET-A|14|30" not in keys, keys)
saved = M._BTSTORE.load(M._catalog) or {}
check("and it's still on disk", "ALPHA-1|14|30" in (saved.get("tiers") or {}), list((saved.get("tiers") or {})))

# ── 5 ─────────────────────────────────────────────────────────────────────────
print("\n5. a backtest finishing after a switch")
BT = M._bt()
gate = threading.Event(); entered = threading.Event()
_orig_run = BT.run_for_api


def slow_run(df, *a, **k):
    entered.set(); gate.wait(30)
    return {"tested": 99, "combos": [], "tierAnalysis": {},
            "skus": [{"sku": s} for s in df["sku"].unique()]}


BT.run_for_api = slow_run
_cache_calls = []
_orig_cache = M._cache_backtest_tiers
M._cache_backtest_tiers = lambda res: _cache_calls.append(res)
_real_job({"horizon": 44, "n_cutoffs": "auto", "step": 28, "lead": 14, "coverage": 30,
           "service_pct": 95, "min_train": 120, "holding_pct": 25}, {}, "test")
entered.wait(10)
M.use_connection(a_id, {"active": False}); wait_restore()      # switch mid-run
gate.set()
for _ in range(100):
    if M._bt_job["status"] != "running":
        break
    time.sleep(0.1)
check("the run ends", M._bt_job["status"] == "done", M._bt_job)
check("its result is not saved into the spreadsheets", _cache_calls == [], len(_cache_calls))
check("the job says it was discarded", "discarded" in (M._bt_job.get("summary") or {}), M._bt_job.get("summary"))
check("the spreadsheets' measured tiers are untouched",
      "SHEET-A|14|30" in M._backtest_tier_cache, sorted(M._backtest_tier_cache))
# Same workspace, nothing moved: the run IS kept.
gate.clear(); entered.clear(); _cache_calls.clear()
_real_job({"horizon": 44, "n_cutoffs": "auto", "step": 28, "lead": 14, "coverage": 30,
           "service_pct": 95, "min_train": 120, "holding_pct": 25}, {}, "test")
entered.wait(10); gate.set()
for _ in range(100):
    if M._bt_job["status"] != "running":
        break
    time.sleep(0.1)
check("with no switch the run is kept", len(_cache_calls) == 1, len(_cache_calls))
BT.run_for_api = _orig_run
M._cache_backtest_tiers = _orig_cache

# ── 6 ─────────────────────────────────────────────────────────────────────────
print("\n6. a paused store's live readings and a same-code spreadsheet product")
check("spreadsheets showing", M._current_slot() == M._WS.SHEETS, M._current_slot())
upload_sheet(pd.concat([sheet_df(), sheet_df("ALPHA-1")]))
M._live.record({"ALPHA-1": {"listPrice": 99.0, "currentPrice": 49.0}}, "shopify")
M._slog.record({"ALPHA-1": 3})
check("the store's readings don't apply to the spreadsheet product",
      M._live_readings_apply("ALPHA-1") is False)
row = next((s for s in M.list_skus() if s["id"] == "ALPHA-1"), {})
check("its stock isn't the store's hourly reading", row.get("stockSource") != "live", row.get("stockSource"))
with M._state_lock:
    M._rebuild(M.today(), only="ALPHA-1")
ent = M._cache().get("ALPHA-1") or {}
check("its forecast doesn't use the store's shelf price",
      not ent.get("live_price") and not ent.get("pos_events"), ent.get("live_price"))
M.use_connection(a_id, {"active": True}); wait_restore()
check("with the store showing, its own product reads them", M._live_readings_apply("ALPHA-1") is True)
row = next((s for s in M.list_skus() if s["id"] == "ALPHA-1"), {})
check("and gets the hourly stock", row.get("stockSource") == "live" and row.get("lastKnownStock") == 3,
      (row.get("stockSource"), row.get("lastKnownStock")))

# ── 2 ─────────────────────────────────────────────────────────────────────────
print("\n2. deleting the store that is showing")
check("A showing, spreadsheets set aside", M._WS.SHEETS in M._WS.parked(), M._WS.parked())
M.delete_connection(a_id); wait_restore()
check("'showing' is recorded as spreadsheets", M._WS.showing() == M._WS.SHEETS, M._WS.showing())
check("the set-aside spreadsheets came back", "SHEET-A" in skus(), skus())
check("A's workspace is gone", M._WS.store_slot(a_id) not in M._WS.parked(), M._WS.parked())
check("B's set-aside copy is still there", M._WS.store_slot(b_id) in M._WS.parked(), M._WS.parked())
M.use_connection(b_id, {"active": True}); wait_restore()
check("B comes back intact", skus() == ["BETA-1", "BETA-2", "SHARED"], skus())
M._WS.drop(M._WS.SHEETS)
M.delete_connection(b_id)
check("deleting B with nothing set aside: recorded as spreadsheets", M._WS.showing() == M._WS.SHEETS,
      M._WS.showing())
check("its products stay loaded", skus() == ["BETA-1", "BETA-2", "SHARED"], skus())

print(f"\n{'All store-switching tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)

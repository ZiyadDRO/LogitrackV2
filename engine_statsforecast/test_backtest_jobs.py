"""
test_backtest_jobs.py — which test runs when settings change, and the per-product progress.

The promise: changing one product's settings refits that product, never the whole store,
whatever is already running; and the product page can tell where its product is in a run.
The real fits take minutes, so a fake backtest stands in that walks the products slowly.

Run:  python test_backtest_jobs.py
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
    "LOGITRACK_SYNC_ENABLED": "0", "LOGITRACK_SAMPLE_ENABLED": "0", "LOGITRACK_PERSIST": "0",
})
warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd          # noqa: E402
import main as M             # noqa: E402
import backtest as BT        # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILURES.append(name)


SKUS = [f"P{i}" for i in range(6)]
STEP = 0.15                      # seconds per product in the fake run
RUNS = []                        # (kind, products) per run that started
gate = threading.Event()         # holds the fake run on its first product until set


def fake_walk(products, should_stop, progress, phase=None):
    order = list(products)
    for i, s in enumerate(order):
        if should_stop and should_stop():
            raise BT.BacktestCancelled()
        if progress:
            progress(*((phase,) if phase else ()), order, i, s)
        while not gate.is_set():
            if should_stop and should_stop():
                raise BT.BacktestCancelled()
            time.sleep(0.01)
        time.sleep(STEP)
    if progress:
        progress(*((phase,) if phase else ()), order, len(order), None)


def fake_run_backtest(df, *a, only_skus=None, should_stop=None, progress=None, **k):
    want = sorted(only_skus) if only_skus else SKUS
    RUNS.append(("partial", tuple(want)))
    fake_walk(want, should_stop, progress)
    return pd.DataFrame({"sku": want, "leadDays": 14, "coverageDays": 30})


def fake_run_for_api(df, should_stop=None, progress=None, **k):
    RUNS.append(("full", tuple(SKUS)))
    fake_walk(SKUS, should_stop, progress, "fitting")
    fake_walk(SKUS, should_stop, progress, "baseline")
    return {"combos": [], "tested": len(SKUS)}


BT.run_backtest = fake_run_backtest
BT.run_for_api = fake_run_for_api
BT.merge_rows = lambda old, new: new
BT.rebuild_from_rows = lambda *a, **k: {"tested": len(SKUS)}
REAL_CACHE_TIERS = M._cache_backtest_tiers
for name in ("_cache_backtest_tiers", "_store_backtest_result", "_persist_backtest", "_record_backtest_inputs"):
    setattr(M, name, lambda *a, **k: None)
M.catalog_to_frame = lambda: pd.DataFrame({"sku": SKUS, "ds": pd.Timestamp("2025-01-01"), "y": 1.0})
M._bt_stamp = lambda: None
M._bt_stamp_still_valid = lambda stamp: True
M._last_backtest_rows = pd.DataFrame({"sku": SKUS, "leadDays": 14, "coverageDays": 30})
M._last_backtest = {"tested": len(SKUS)}

PARAMS = {"horizon": 44, "n_cutoffs": "auto", "step": 28, "lead": 14, "coverage": 30,
          "service_pct": 95, "min_train": 120}


def partial(skus):
    return M._run_partial_job(skus, dict(PARAMS), [(14, 30)], {}, 25)


def full():
    return M._run_backtest_job({**PARAMS, "holding_pct": 25, "combos": [(14, 30)]}, {}, "params-changed")


def wait_idle(timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        with M._bt_job_lock:
            if M._bt_job["status"] != "running" and not M._bt_pending:
                return
        time.sleep(0.02)
    raise RuntimeError("the job never finished")


def wait_for(cond, timeout=10):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


print("one product changed, nothing running")
gate.set()
RUNS.clear()
check("starts at once", partial(["P2"]) == "started")
wait_idle()
check("refits only that product", RUNS == [("partial", ("P2",))], RUNS)

print("\na second product changed while the first is refitting")
gate.clear()
RUNS.clear()
partial(["P2"])
wait_for(lambda: M._bt_sku_progress("P2") and M._bt_sku_progress("P2")["state"] == "refitting")
check("the product page sees its product refitting", M._bt_sku_progress("P2")["state"] == "refitting",
      M._bt_sku_progress("P2"))
other = M._bt_sku_progress("P4")
check("another product's page says it isn't the one being refitted",
      other["state"] == "other" and other["refitting"] == 1, other)
check("it's queued, not turned into a whole-store run", partial(["P4"]) == "queued")
check("the waiting product's page says it's next", M._bt_sku_progress("P4")["state"] == "queued",
      M._bt_sku_progress("P4"))
gate.set()
wait_idle()
check("the running one stops and both products refit together, nothing else",
      RUNS == [("partial", ("P2",)), ("partial", ("P2", "P4"))], RUNS)

print("\none product changed while a whole-store run is going")
gate.clear()
RUNS.clear()
full()
wait_for(lambda: M._bt_job.get("progress", {}).get("current") == "P0")
check("the whole-store run isn't thrown away", partial(["P5"]) == "queued")
check("its page says it's waiting with products ahead",
      M._bt_sku_progress("P3") == {"fraction": M._bt_fraction(M._bt_job), "state": "waiting", "ahead": 3},
      M._bt_sku_progress("P3"))
gate.set()
wait_for(lambda: (M._bt_job.get("progress") or {}).get("phase") == "baseline")
check("once its own fits are done its page says so", M._bt_sku_progress("P3")["state"] == "done",
      M._bt_sku_progress("P3"))
f = M._bt_fraction(M._bt_job)
check("the bar is past half way during the comparison pass", f is not None and 0.5 <= f <= 0.95, f)
wait_idle()
check("then only that one product is refitted", RUNS == [("full", tuple(SKUS)), ("partial", ("P5",))], RUNS)

print("\na store-wide change while a one-product refit is going")
gate.clear()
RUNS.clear()
partial(["P1"])
wait_for(lambda: M._bt_job.get("progress", {}).get("current") == "P1")
full()
partial(["P2"])                       # arrives while the whole-store run is waiting
check("a one-product change folds into the waiting whole-store run",
      M._bt_pending and M._bt_pending.get("kind") == "full", M._bt_pending)
gate.set()
wait_idle()
check("the refit stops, the whole-store run replaces it, nothing extra after",
      RUNS == [("partial", ("P1",)), ("full", tuple(SKUS))], RUNS)
check("after the run, the page has nothing to show", M._bt_sku_progress("P1") is None, M._bt_sku_progress("P1"))

print("\nthe level a product on its own settings gets")


def analysis(best, skus, mixed_cost=None):
    tiers = [{"tier": t, "totalCost": (100 if t == best else 200)} for t in (95, 99, 99.5)]
    return {"tiers": tiers, "bestTier": best, "nearestTier": best, "ranking": {"decisive": True},
            "mixedPolicy": {"totalCost": mixed_cost} if mixed_cost is not None else None,
            "assumptions": {"holdingPct": 25},
            "bySku": [{"sku": s, "costKnown": True, "lossMaking": False, "bestTier": 95,
                       "windows": 23, "tiers": {"95": {}, "99": {}, "99.5": {}}} for s in skus]}


store = analysis(99.5, SKUS)
alone = analysis(95, ["P2"])          # P2 moved to 21 days' coverage: a "store" of one
REAL_CACHE_TIERS({"params": {"lead": 14, "coverage": 30}, "tierAnalysis": store,
                  "tierAnalysisByCombo": {"14/30": store, "14/21": alone}})
got = M._backtest_tier_cache.get("P2|14|21") or {}
check("a product on different settings gets the store-wide level, not one picked from itself alone",
      got.get("pct") == 99.5 and got.get("policy") == "uniform", got)
check("and so does everyone else", (M._backtest_tier_cache.get("P0|14|30") or {}).get("pct") == 99.5)

print(f"\n{'All backtest-job tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)

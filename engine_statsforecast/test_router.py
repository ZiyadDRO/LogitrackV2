"""
test_router.py — verifies the ROUTER end to end.

The sandbox has no fastapi / prophet / statsforecast, so this injects minimal
stubs that mirror only the API surface the code touches. Every line of router.py,
engines.py and main.py's orchestration is real; only the heavy libraries are faked.
Run locally (with the real libs installed) to exercise the true numerics:
    python test_router.py
"""
import os
# Tests must never touch a real data store. Both stores honour this flag and it makes the
# run fully in-memory, so a suite run inside engine_statsforecast/ can no longer overwrite
# a live catalog — and every "fresh start" assertion below means what it says.
os.environ["LOGITRACK_PERSIST"] = "0"
import sys, types, datetime
import numpy as np
import pandas as pd

UID = "series"

# ── stub: fastapi ─────────────────────────────────────────────────────────────
fa = types.ModuleType("fastapi")
class _App:
    def add_middleware(self, *a, **k): pass
    def _deco(self, *a, **k):
        def d(fn): return fn
        return d
    get = post = delete = on_event = _deco
fa.FastAPI = lambda *a, **k: _App()
fa.Query = lambda default=None, **k: default
fa.Body = lambda default=None, **k: default
fa.File = lambda default=None, **k: default
fa.Form = lambda default=None, **k: default
class _UF: ...
fa.UploadFile = _UF
class HTTPException(Exception):
    def __init__(self, status_code=400, detail=""): super().__init__(detail); self.status_code = status_code
fa.HTTPException = HTTPException
cors = types.ModuleType("fastapi.middleware.cors"); cors.CORSMiddleware = object
resp = types.ModuleType("fastapi.responses"); resp.FileResponse = object; resp.StreamingResponse = object
mw = types.ModuleType("fastapi.middleware")
sys.modules.update({"fastapi": fa, "fastapi.middleware": mw,
                    "fastapi.middleware.cors": cors, "fastapi.responses": resp})

# ── stub: prophet ─────────────────────────────────────────────────────────────
pro = types.ModuleType("prophet")
class Prophet:
    def __init__(self, **k): self._last = None; self._mean = 1.0
    def add_regressor(self, *a, **k): pass
    def fit(self, df):
        self._last = pd.to_datetime(df["ds"]).max(); self._mean = float(df["y"].mean()); return self
    def make_future_dataframe(self, periods, freq="D"):
        d = pd.date_range(self._last + pd.Timedelta(days=1), periods=periods, freq=freq)
        return pd.DataFrame({"ds": d})
    def predict(self, df):
        out = df.copy(); wd = pd.to_datetime(out["ds"]).dt.weekday.to_numpy()
        out["yhat"] = self._mean * (1.0 + np.where(wd >= 5, 0.3, -0.1)); return out
pro.Prophet = Prophet
sys.modules["prophet"] = pro

# ── stub: statsforecast ───────────────────────────────────────────────────────
def _mk(name):
    return type(name, (), {"__init__": lambda self, **k: setattr(self, "alias", name), "alias": name})
sfm = types.ModuleType("statsforecast.models")
for _n in ["AutoARIMA", "AutoETS", "AutoTheta", "CrostonClassic", "TSB"]:
    setattr(sfm, _n, _mk(_n))
class StatsForecast:
    def __init__(self, models, freq="D", n_jobs=1): self.models = models; self._df = None
    def _nm(self, m): return getattr(m, "alias", type(m).__name__)
    def fit(self, df): self._df = df.sort_values("ds").reset_index(drop=True); return self
    def predict(self, h, X_df=None, level=None):
        last = pd.to_datetime(self._df["ds"]).max()
        d = pd.date_range(last + pd.Timedelta(days=1), periods=h, freq="D")
        base = float(self._df["y"].to_numpy()[-28:].mean()) if len(self._df) else 0.0
        out = pd.DataFrame({"unique_id": UID, "ds": d})
        for m in self.models: out[self._nm(m)] = base
        return out
    def cross_validation(self, df, h, n_windows, step_size):
        df = df.sort_values("ds").reset_index(drop=True); N = len(df); fr = []
        for w in range(n_windows):
            end = N - w * step_size; start = end - h
            if start <= 0: continue
            tr, te = df.iloc[:start], df.iloc[start:end]
            base = float(tr["y"].to_numpy()[-28:].mean()) if len(tr) else 0.0
            f = pd.DataFrame({"unique_id": UID, "ds": te["ds"].values, "cutoff": tr["ds"].iloc[-1], "y": te["y"].values})
            for m in self.models: f[self._nm(m)] = base
            fr.append(f)
        return pd.concat(fr, ignore_index=True) if fr else pd.DataFrame(columns=["unique_id", "ds", "cutoff", "y"])
sf = types.ModuleType("statsforecast"); sf.StatsForecast = StatsForecast; sf.models = sfm
sys.modules.update({"statsforecast": sf, "statsforecast.models": sfm})

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import router as R
import main as M

# Isolate model-switch persistence from the real project file: tests drive _rebuild
# over synthetic SKUs and must not write switch state into model_switches.json.
import tempfile as _tf
M.SWITCHES_FILE = _tf.mktemp(suffix="_test_switches.json")
M._switch_state = {"events": [], "lastRoute": {}}

TODAY = M.today()
PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail and not cond else ''}")

def series(days, fn, end=TODAY, attrs=None):
    start = end - pd.Timedelta(days=days - 1)
    ds = pd.date_range(start, end, freq="D")
    y = np.array([max(0, fn(i, d)) for i, d in enumerate(ds)], dtype=float)
    return pd.DataFrame({"ds": ds, "y": y})

print("\n=== ROUTER verification (stubbed libs) ===\n")

# 1) Unit-test the decision table directly --------------------------------------
print("1) route() decision table")
check("established+regular → prophet", R.route(700, 5000, "smooth", 0)[0] == "prophet")
check("bulk spike alone is NOT established", R.route(220, 120, "erratic", 3, selling_days=6)[0] == "global")
check("many selling days can graduate at lower volume", R.route(220, 75, "smooth", 0, selling_days=55)[0] == "prophet")
check("sparse + no relatives → croston", R.route(400, 120, "intermittent", 0)[0] == "croston")
check("new + relatives → global", R.route(30, 60, "smooth", 4)[0] == "global")
check("established intermittent + relatives → croston (slow-mover, not pooled)", R.route(400, 90, "lumpy", 5)[0] == "croston")
check("NEW sparse + relatives → global (too new to tell, borrow)", R.route(30, 40, "intermittent", 5)[0] == "global")
check("thin + no relatives → abstain", R.route(40, 120, "smooth", 0)[0] == "abstain")
check("regular but not 'established' → prophet", R.route(150, 90, "smooth", 0)[0] == "prophet")

# 1b) Adaptive grouping should prefer product-family fields over incidental traits.
print("\n1b) adaptive subgroup selection")
cat = {}
for k, material in enumerate(["Solid Wood", "MDF", "Plywood"]):
    cat[f"SSF{k}"] = {
        "df": series(240, lambda i, d: 12 * (1.45 if d.month in (3, 4) else 1.0) * (1.2 if d.weekday() >= 5 else 1.0)),
        "attrs": {"category": "Bathroom Vanities", "subcategory": "Single Sink Freestanding",
                  "material": material, "hardware_finish": ["Brushed Nickel", "Matte Black", "Brass"][k]},
    }
for k, finish in enumerate(["Oak", "Black", "White"]):
    cat[f"FLT{k}"] = {
        "df": series(240, lambda i, d: 9 * (1.55 if d.month in (6, 7) else 1.0) * (1.35 if i % 45 < 6 else 1.0)),
        "attrs": {"category": "Bathroom Vanities", "subcategory": "Floating Wall-Mount",
                  "finish": finish, "hardware_finish": "Integrated Pull"},
    }
ag, am = R.adaptive_group_catalog(cat, ["category"])
check("single-sink subgroup labeled by subcategory", all(am[f"SSF{k}"].get("splitCols") == ["subcategory"] for k in range(3)),
      str({k: v.get("splitCols") for k, v in am.items()}))
check("floating subgroup labeled by subcategory, not hardware", all(am[f"FLT{k}"].get("splitCols") == ["subcategory"] for k in range(3)),
      str({k: v.get("splitCols") for k, v in am.items()}))

# 2) End-to-end through the catalog/orchestration -------------------------------
print("\n2) End-to-end routing via _rebuild")
rng = np.random.default_rng(0)
M._catalog.clear(); M._sku_cache.clear()

# established regular (orphan) → prophet
M._catalog["REG"] = {"df": series(730, lambda i, d: 20 * (1.3 if d.weekday() >= 5 else 1.0) + rng.normal(0, 2)),
                     "attrs": {}, "sku_name": "Regular", "mode": "u", "filename": "f", "events": []}
# intermittent orphan → croston
M._catalog["SPARSE"] = {"df": series(400, lambda i, d: (rng.poisson(4) if rng.random() < 0.2 else 0)),
                        "attrs": {}, "sku_name": "Sparse", "mode": "u", "filename": "f", "events": []}
# new SKU + 3 established relatives in category 'vanity' → global
for k in range(3):
    M._catalog[f"DON{k}"] = {"df": series(700, lambda i, d: 15 * (1.3 if d.weekday() >= 5 else 1.0) + rng.normal(0, 2)),
                             "attrs": {"category": "vanity"}, "sku_name": f"Donor{k}", "mode": "u", "filename": "f", "events": []}
M._catalog["NEW"] = {"df": series(30, lambda i, d: 12 + rng.normal(0, 2)),
                     "attrs": {"category": "vanity"}, "sku_name": "NewVanity", "mode": "u", "filename": "f", "events": []}
# tiny orphan, regular → abstain
M._catalog["TINY"] = {"df": series(40, lambda i, d: 3 + rng.normal(0, 1)),
                      "attrs": {}, "sku_name": "Tiny", "mode": "u", "filename": "f", "events": []}

M._rebuild(TODAY)

routes = {k: M._sku_cache[k]["route"] for k in M._catalog}
print("      routes:", routes)
check("REG → prophet",   routes["REG"] == "prophet", routes["REG"])
check("SPARSE → croston", routes["SPARSE"] == "croston", routes["SPARSE"])
check("NEW → global",    routes["NEW"] == "global", routes["NEW"])
check("DONors → prophet", all(routes[f"DON{k}"] == "prophet" for k in range(3)))
check("TINY → abstain",  routes["TINY"] == "abstain", routes["TINY"])

# 3) Cache-entry contract + forecast shape --------------------------------------
print("\n3) Entry contract + forecast shape")
for k in ("REG", "SPARSE", "NEW", "TINY"):
    e = M._sku_cache[k]; fc = e["forecast"]
    ok = set(["ds", "yhat", "yhat_lower", "yhat_upper"]).issubset(fc.columns) and len(fc) >= 366
    check(f"{k}: forecast has band + horizon", ok, f"cols={list(fc.columns)} n={len(fc)}")
    check(f"{k}: future_fc only future", bool((e["future_fc"]["ds"] > TODAY).all()))
    check(f"{k}: lower ≤ upper", bool((fc["yhat_lower"] <= fc["yhat_upper"]).all()))
    check(f"{k}: details has route+reason", e["explain"].get("route") == e["route"] and len(e["route_reason"]) > 10)

# 4) Global pooled engine actually borrows seasonal shape (not flat) ------------
print("\n4) Global engine nuance + baseline")
new_fc = M._sku_cache["NEW"]["forecast"]
weekday_var = new_fc.assign(wd=new_fc["ds"].dt.weekday).groupby("wd")["yhat"].mean()
check("global forecast varies by weekday (not flat)", float(weekday_var.std()) > 1e-6, str(round(float(weekday_var.std()), 3)))
eng = M._sku_cache["NEW"]["engine"]
check("global level sourced from relatives", "related" in eng.level_source or "own" in eng.level_source, eng.level_source)
check("predict_baseline returns band df", set(["ds", "yhat", "yhat_lower", "yhat_upper"]).issubset(eng.predict_baseline(None).columns))

# 6) Categorization: attributes drive grouping + routing ------------------------
print("\n6) Categorization endpoints")
resp = M.set_attributes({"skus": {"SPARSE": {"category": "vanity"}}})
check("set_attributes returns groups + success", bool(resp.get("success")) and "skus" in resp)
check("established intermittent STAYS on Croston after gaining a category (not pooled)", M._sku_cache["SPARSE"]["route"] == "croston",
      M._sku_cache["SPARSE"]["route"])
g = M.get_groups()
srow = next(r for r in g["skus"] if r["skuId"] == "SPARSE")
check("get_groups lists ≥3 similar products for SPARSE", len(srow["similar"]) >= 3, str(len(srow["similar"])))
check("group column detected = category", g["groupColumns"] == ["category"], str(g["groupColumns"]))
check("usesGlobal flag false for established intermittent (uses Croston)", srow["usesGlobal"] is False)

# 7) Scorecard velocity-aware recommendation ------------------------------------
print("\n7) Scorecard recommendation (velocity-aware)")
a_6pct = M.sc_recommendation("long", "A", 0.06, 400, 14, False, False, True, 380)  # the reported bug
a_mod  = M.sc_recommendation("long", "A", 0.30, 400, 14, False, False, True, 380)
# dur must sit OUTSIDE the reorder window or the reorder message correctly wins first.
# sc_bands(14) = (risk 7d, due 21d) since the bands became a fraction of each product's
# own lead time; this probe used 16, which is inside it. 30 tests the intended branch.
a_fast = M.sc_recommendation("healthy", "A", 0.70, 30, 14, False, False, True, 30)
check("6% velocity + profitable → barely-selling (NOT 'profitable')", "barely selling" in a_6pct[0].lower(), a_6pct[0])
check("moderate velocity + profitable + overstocked → 'profitable' message", "profitable" in a_mod[0].lower(), a_mod[0])
check("very high velocity + profitable → under-stocking warning", "under-stock" in a_fast[0].lower(), a_fast[0])
check("barely-selling reason notes little/no realized profit", "little to no profit" in a_6pct[1].lower(), a_6pct[1])
check("TINY (abstain) details carry a last-resort caution",
      "caution" in M._sku_cache["TINY"]["explain"] and "last-resort" in M._sku_cache["TINY"]["explain"]["caution"].lower(),
      str(M._sku_cache["TINY"]["explain"].get("caution"))[:40])

# 5) Demo still works via warmup ------------------------------------------------
print("\n5) Warmup starts EMPTY (no demo dataset)")
M.warmup()
check("warmup loads no products at all", len(M._catalog) == 0 and len(M._sku_cache) == 0,
      f"catalog={len(M._catalog)} cache={len(M._sku_cache)}")
check("no demo constants remain", not hasattr(M, "DEMO_SKU_ID") and not hasattr(M, "generate_demo_df"))
try:
    M.catalog_to_frame(); _empty_ok = False
except Exception as ex:
    _empty_ok = getattr(ex, "status_code", None) == 400
check("...with a 400 rather than a crash", _empty_ok)

# 8) Price-modeled flag + event-aware current price -----------------------------
print("\n8) Price modeling flag + effective price")
ev_now = [{"type": "price_change_permanent", "date": (TODAY - pd.Timedelta(days=5)).strftime("%Y-%m-%d"), "new_price": 17.5}]
ev_fut = [{"type": "price_change_permanent", "date": (TODAY + pd.Timedelta(days=5)).strftime("%Y-%m-%d"), "new_price": 17.5}]
check("effective price reflects an in-force permanent change", M._effective_price(20.0, ev_now, TODAY) == 17.5)
check("future-dated price change does NOT change current price", M._effective_price(20.0, ev_fut, TODAY) == 20.0)

# 9) Regression: eligibility for intermittent demand must not crash ------------
print("\n9) Eligibility status (intermittent NameError regression)")
try:
    s_long, _ = M.get_eligibility_status(400, 500, "intermittent", selling_days=50)
    s_thin, _ = M.get_eligibility_status(40, 10, "lumpy", selling_days=4)
    check("intermittent + long history → CONFLICTING (no crash)", s_long == "CONFLICTING", s_long)
    check("intermittent + thin history → INSUFFICIENT (no crash)", s_thin == "INSUFFICIENT", s_thin)
except NameError as e:
    check("eligibility for intermittent demand does not raise", False, str(e))

# 10) Daily-calendar gap fill on ingest -----------------------------------------
print("\n10) fill_daily_gaps")
from forecast_engine import fill_daily_gaps
_gap = pd.DataFrame({
    "ds": pd.to_datetime(["2025-01-01", "2025-01-03", "2025-01-07"]),
    "y": [5.0, 3.0, 4.0], "price": [9.99, None, 8.99], "on_promotion": [0, 0, 1],
})
_filled, _n = fill_daily_gaps(_gap)
check("gaps filled to a continuous calendar", len(_filled) == 7 and _n == 4, f"len={len(_filled)} n={_n}")
check("filled days have zero sales", float(_filled[_filled["ds"] == "2025-01-02"]["y"].iloc[0]) == 0.0)
check("price carried forward across the gap", float(_filled[_filled["ds"] == "2025-01-04"]["price"].iloc[0]) == 9.99)
check("no-gap frame passes through unchanged", fill_daily_gaps(_filled)[1] == 0)

# 11) Event validation ------------------------------------------------------------
print("\n11) Event payload validation")
def _raises_400(events):
    try:
        M._validate_events(events); return False
    except Exception as ex:
        return getattr(ex, "status_code", None) == 400
check("valid promo passes", M._validate_events([{"type": "promotion", "date": "2026-08-01", "end_date": "2026-08-05", "discount_pct": 20}]) is not None)
check("unknown type rejected", _raises_400([{"type": "mystery", "date": "2026-08-01"}]))
check("bad date rejected", _raises_400([{"type": "promotion", "date": "not-a-date"}]))
check("end before start rejected", _raises_400([{"type": "promotion", "date": "2026-08-05", "end_date": "2026-08-01"}]))
check("price change without price rejected", _raises_400([{"type": "price_change_permanent", "date": "2026-08-01"}]))

# 12) Cost column seeds lastKnownCost on ingest -----------------------------------
print("\n12) Cost column ingestion")
_days = pd.date_range(TODAY - pd.Timedelta(days=199), periods=200, freq="D")
_ing = pd.DataFrame({
    "SKU": ["COST-A"] * 200 + ["COST-B"] * 200,
    "SKU_Name": ["Cost A"] * 200 + ["Cost B"] * 200,
    "Date": list(_days) * 2,
    "Units_Sold": [5] * 200 + [3] * 200,
    "Category": ["Widgets"] * 400,
    "Cost": [12.5] * 200 + [None] * 200,   # B has a cost column but no values
})
_res = M._ingest(_ing, "cost_test.xlsx")
_by = {r["id"]: r for r in _res["loadedSkus"]}
check("SKU with cost → lastKnownCost seeded", _by.get("COST-A", {}).get("lastKnownCost") == 12.5,
      str(_by.get("COST-A", {}).get("lastKnownCost")))
check("SKU without cost values → lastKnownCost None", _by.get("COST-B", {}).get("lastKnownCost") is None)
check("cost is NOT a grouping attribute", "cost" not in (M._catalog.get("COST-A", {}).get("attrs") or {}),
      str(M._catalog.get("COST-A", {}).get("attrs")))

# 13) Backtest hardening ----------------------------------------------------------
print("\n13) Backtest hardening")
import backtest as BT

# — cutoff schedule spreads across the span instead of clustering in the recent past —
_fa = pd.Timestamp("2023-01-01"); _lp = pd.Timestamp("2026-01-01")   # ~1096d testable
_sched = BT._cutoff_schedule(_fa, _lp, n_cutoffs=8, step=28)
_dates = [d for d, _b in _sched]
_blocks = [b for _d, b in _sched]
check("schedule returns the requested number of cutoffs", len(_sched) == 8, str(len(_sched)))
check("all cutoffs inside the allowed range", all(_fa <= d <= _lp for d in _dates))
check("cutoffs are unique", len(set(_dates)) == len(_dates))
check("schedule spans >2x the fixed-step reach (seasonal coverage)",
      (max(_dates) - min(_dates)).days > 2 * (7 * 28), f"{(max(_dates) - min(_dates)).days}d")
check("schedule mixes recent and historic blocks", set(_blocks) == {"recent", "historic"}, str(set(_blocks)))
_short = BT._cutoff_schedule(pd.Timestamp("2025-01-01"), pd.Timestamp("2025-03-01"), 8, 28)
check("short span still yields cutoffs, none out of range", 0 < len(_short) <= 8, str(len(_short)))
check("no cutoffs when the span is inverted", BT._cutoff_schedule(_lp, _fa, 8, 28) == [])

# — MASE denominator must be date-correct, i.e. taken on the CONTIGUOUS series —
_contig = np.tile([10.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0], 12) + np.arange(84) * 0.1   # weekly cycle + drift
_gapped = np.delete(_contig, np.arange(3, len(_contig), 9))       # rows removed mid-series
check("MASE denominator differs once rows are dropped (why we pass the full series)",
      abs(BT._mase_denominator(_contig) - BT._mase_denominator(_gapped)) > 1e-6,
      f"{BT._mase_denominator(_contig)} vs {BT._mase_denominator(_gapped)}")
check("MASE denominator None when series shorter than the season", BT._mase_denominator(np.arange(5.0)) is None)

# — NaN-tolerant aggregation: buffer metrics are NaN when a window isn't testable —
_f = pd.DataFrame({
    "sku": ["A", "A", "B", "B"],
    "covered": [1.0, np.nan, 0.0, 1.0],
    "cov_95": [1.0, np.nan, 0.0, 1.0], "lost_95": [0.0, np.nan, 5.0, 0.0],
    "safety_95": [3.0, np.nan, 4.0, 4.0],
    # low-volume windows carry a much worse ratio → weighted and unweighted must diverge
    "wape": [0.10, 0.10, 0.80, 0.80], "absErrSum": [40.0, 40.0, 8.0, 8.0],
    "sumY": [400.0, 400.0, 10.0, 10.0], "mase": [0.8, 0.9, 1.1, 1.0],
    "bias_pct": [1.0, 2.0, 3.0, 4.0], "coverage": [0.8, 0.8, 0.8, 0.8],
    "orderErrPct": [1.0, 1.0, 1.0, 1.0], "sigma": [1.0] * 4, "cv": [10.0] * 4,
    "trainDays": [100, 200, 400, 400], "block": ["recent"] * 2 + ["historic"] * 2,
})
_a = BT._agg(_f, ci=True)
check("service% ignores non-testable (NaN) windows", _a["service_achieved%"] == 66.7, str(_a["service_achieved%"]))
check("service_windows counts only testable windows", _a["service_windows"] == 3, str(_a["service_windows"]))
check("WAPE% is volume-weighted (pooled), not a mean of ratios",
      _a["WAPE%"] == round(96 / 820 * 100, 1), f'{_a["WAPE%"]} vs unweighted {_a["WAPE%_unweighted"]}')
check("low-volume windows no longer dominate (weighted << unweighted)",
      _a["WAPE%"] < _a["WAPE%_unweighted"] - 20,
      f'{_a["WAPE%"]} vs {_a["WAPE%_unweighted"]}')
check("_nanmean tolerates an all-NaN column", BT._nanmean(pd.Series([np.nan, np.nan])) == 0.0)
check("bootstrap returns a 2-sided interval", isinstance(_a["service_achieved%_ci"], list) and len(_a["service_achieved%_ci"]) == 2,
      str(_a["service_achieved%_ci"]))
check("bootstrap CI brackets the point estimate",
      _a["service_achieved%_ci"][0] <= _a["service_achieved%"] <= _a["service_achieved%_ci"][1],
      str(_a["service_achieved%_ci"]))
check("bootstrap returns None with a single SKU", BT._boot_ci(_f[_f["sku"] == "A"], "covered") is None)
check("train-length buckets split the frame", len(BT._by_train_bucket(_f)) == 3, str(len(BT._by_train_bucket(_f))))
check("recent/historic blocks reported separately", len(BT._by_block(_f)) == 2)

# — per-SKU rows carry a window count so thin ones can be suppressed —
_g = BT._grouped(_f, "sku")
check("per-SKU rows carry window counts", set(_g["windows"]) == {2}, str(list(_g["windows"])))
check("2-window SKUs are flagged not-reportable", not _g["reportable"].any())

# — tier summary survives NaN buffer metrics and weights per SKU, not per window —
_f2 = _f.assign(marginUnit=[5.0] * 4, unitCost=[10.0] * 4, costKnown=[True] * 4, price=[20.0] * 4)
_ts = BT._tier_summary(_f2, coverage=30, holding_annual=0.25)
_t95 = next(t for t in _ts["tiers"] if t["tier"] == 95)
check("tier achievedService averages per SKU first", _t95["achievedService"] == 75.0, str(_t95["achievedService"]))
check("tier windows counts testable windows only", _t95["windows"] == 3, str(_t95["windows"]))
check("tier carries a confidence interval", _t95["achievedServiceCI"] is not None)
check("tier money is finite despite NaN windows", np.isfinite(_t95["totalCost"]), str(_t95["totalCost"]))

# — force_route bypasses the router (the baseline-policy comparison) —
_bt_days = 400
_bt = series(_bt_days, lambda i, d: 20 + 8 * np.sin(i / 7.0))
_forced = M.build_entry("FORCE-1", "Force 1", _bt, "uploaded", "t", TODAY, [], [], 0,
                        calibrate=False, force_route="global")
check("force_route overrides the router's choice", _forced["route"] == "global", str(_forced["route"]))
check("forced route is explained as forced", "forced" in (_forced.get("route_reason") or "").lower(),
      str(_forced.get("route_reason")))
_unforced = M.build_entry("FORCE-2", "Force 2", _bt, "uploaded", "t", TODAY, [], [], 0, calibrate=False)
check("no force_route → normal routing (prophet for established regular)",
      _unforced["route"] == "prophet", str(_unforced["route"]))

# — failed cutoffs are recorded, never silently dropped —
_bad = pd.DataFrame({"sku": ["ERR"] * 400, "ds": pd.date_range(TODAY - pd.Timedelta(days=399), periods=400, freq="D"),
                     "y": np.r_[np.full(399, 5.0), np.nan]})
_bad["y"] = _bad["y"].fillna(5.0)
_skipped = []
_orig = BT._run_cutoff
BT._run_cutoff = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
try:
    _res = BT.run_backtest(_bad, horizon=44, n_cutoffs=3, step=28, min_train=120,
                           verbose=False, skipped=_skipped)
finally:
    BT._run_cutoff = _orig
_errs = [s for s in _skipped if s.get("kind") == "error"]
check("failed cutoffs are captured, not swallowed", len(_errs) >= 1, str(_skipped))
check("failure records name the SKU, cutoff and reason",
      bool(_errs) and all({"sku", "cutoff", "reason"} <= set(e) and "boom" in e["reason"] for e in _errs))
check("short-history skips are tagged distinctly",
      all(s.get("kind") in ("error", "short") for s in _skipped))

# 14) Tier decision hardening -----------------------------------------------------
print("\n14) Tier decision hardening")

# — cutoff capacity: what the data can actually support —
_capdf = pd.concat([
    pd.DataFrame({"sku": "LONG",  "ds": pd.date_range("2024-01-01", periods=900, freq="D"), "y": 5.0}),
    pd.DataFrame({"sku": "MID",   "ds": pd.date_range("2025-06-01", periods=260, freq="D"), "y": 5.0}),
    pd.DataFrame({"sku": "SHORT", "ds": pd.date_range("2026-01-01", periods=100, freq="D"), "y": 5.0}),
], ignore_index=True)
_cap = BT.cutoff_capacity(_capdf, horizon=44, min_train=120, n_cutoffs=8)
_by = {r["sku"]: r for r in _cap["bySku"]}
check("untestable SKU reported with 0 capacity", _by["SHORT"]["maxCutoffs"] == 0 and not _by["SHORT"]["testable"])
check("capacity counts untestable SKUs", _cap["untestableSkus"] == 1, str(_cap["untestableSkus"]))
# MID: span 259 → testable 95d → 95//14 + 1 = 7 once the scheduler tightens its spacing
# to honour the request. Capacity is a real ceiling: ask for ≤ it and you get it.
check("mid-history SKU capacity is the true ceiling", _by["MID"]["maxCutoffs"] == 7, str(_by["MID"]["maxCutoffs"]))
check("binding SKU is the shortest testable one", _cap["bindingSku"] == "MID", str(_cap["bindingSku"]))
check("catalog max equals the binding SKU's capacity", _cap["catalogMax"] == 7, str(_cap["catalogMax"]))
# Regression for the "advertised 40, delivered 21" bug: a request at or under capacity
# must actually be met, whatever step you configured.
_fa_m = (_capdf[_capdf["sku"] == "MID"]["ds"].min() + pd.Timedelta(days=120)).normalize()
_lp_m = (_capdf[_capdf["sku"] == "MID"]["ds"].max() - pd.Timedelta(days=44)).normalize()
for _req in (4, 6, 7):
    check(f"request of {_req} at a coarse step is honoured",
          len(BT._cutoff_schedule(_fa_m, _lp_m, _req, 28)) == _req,
          str(len(BT._cutoff_schedule(_fa_m, _lp_m, _req, 28))))
check("a request above capacity returns the ceiling, not an error",
      len(BT._cutoff_schedule(_fa_m, _lp_m, 99, 28)) == 7,
      str(len(BT._cutoff_schedule(_fa_m, _lp_m, 99, 28))))
# Capacity must match what the SCHEDULER actually returns, not a closed form over
# MIN_STEP — those disagree whenever step > MIN_STEP (regression: it advertised 40
# achievable cutoffs where step=28 could only deliver 21).
for _r in _cap["bySku"]:
    if not _r["testable"]:
        continue
    _g = _capdf[_capdf["sku"] == _r["sku"]]
    _fa = (_g["ds"].min() + pd.Timedelta(days=120)).normalize()
    _lp = (_g["ds"].max() - pd.Timedelta(days=44)).normalize()
    _actual = len(BT._cutoff_schedule(_fa, _lp, 10_000, 28))
    check(f"capacity for {_r['sku']} matches the real schedule", _r["maxCutoffs"] == _actual,
          f'advertised {_r["maxCutoffs"]}, scheduler gives {_actual}')
_capw = BT.cutoff_capacity(_capdf, horizon=44, min_train=120, n_cutoffs=8, step=56)
check("capacity is step-independent now that spacing auto-tightens",
      _capw["catalogMax"] == _cap["catalogMax"], f'{_capw["catalogMax"]} vs {_cap["catalogMax"]}')
check("SKUs that can't supply the request are listed", _cap["cappedSkus"] == ["MID"], str(_cap["cappedSkus"]))
check("long-history SKU is not capped", _by["LONG"]["maxCutoffs"] > 8, str(_by["LONG"]["maxCutoffs"]))
_cap4 = BT.cutoff_capacity(_capdf, horizon=44, min_train=120, n_cutoffs=4)
check("lowering the request clears the capped list", _cap4["cappedSkus"] == [], str(_cap4["cappedSkus"]))

# — critical ratio: the continuous optimum from the tool's own economics —
# margin = 0.75 × cost and 365/30 cycles ⇒ s* = 1 − 0.25/(12.1667 × 0.75) = 97.26%
_cr_frame = pd.DataFrame({
    "sku": ["A"] * 3 + ["B"] * 3,
    "costKnown": [True] * 6, "marginUnit": [300.0] * 3 + [300.0] * 3,
    "unitCost": [400.0] * 6,
})
_cr = BT._critical_ratio(_cr_frame, coverage=30, holding_annual=0.25)
check("critical ratio matches the closed form", abs(_cr["optimalService"] - 97.3) < 0.15, str(_cr["optimalService"]))
check("critical ratio is reported per SKU too", set(_cr["bySku"]) == {"A", "B"})
_cr_hi = BT._critical_ratio(_cr_frame, coverage=30, holding_annual=0.50)
check("higher holding rate lowers the optimum", _cr_hi["optimalService"] < _cr["optimalService"],
      f'{_cr_hi["optimalService"]} vs {_cr["optimalService"]}')
check("critical ratio None without cost-known SKUs",
      BT._critical_ratio(_cr_frame.assign(costKnown=False), 30, 0.25) is None)
check("loss-making SKUs excluded from the optimum",
      BT._critical_ratio(_cr_frame.assign(marginUnit=-5.0), 30, 0.25) is None)

# — tier ranking bootstrap: is the winner real or a coin flip? —
def _mk_tier_frame(lost_by_tier, n_sku=8, seed=1, noise=0.05):
    """Synthetic per-window tier costs. `noise` is per-SKU spread in units short —
    raise it to make two tiers genuinely indistinguishable across resamples."""
    rng = np.random.default_rng(seed); rows = []
    for i in range(n_sku):
        off = {pct: rng.normal(0, noise) for pct in lost_by_tier}   # per-SKU, not per-window
        for w in range(4):
            r = {"sku": f"S{i}", "marginUnit": 100.0, "unitCost": 50.0, "costKnown": True}
            for pct, base in lost_by_tier.items():
                r[f"lost_{pct}"] = max(0.0, base + off[pct] + rng.normal(0, noise / 4))
                r[f"safety_{pct}"] = float(pct) / 10.0
            rows.append(r)
    return pd.DataFrame(rows)
_tiers_stub = [{"tier": p} for p in sorted(BT.Z)]
# 90 is dramatically worse; 99 clearly best → decisive
_dec = BT._bootstrap_tier_ranking(_mk_tier_frame({90: 8.0, 95: 4.0, 98: 1.0, 99: 0.02}),
                                  _tiers_stub, 365 / 30, 0.25)
check("a clear winner is called decisive", _dec["decisive"] and _dec["winnerProb"] > 90,
      f'{_dec["winner"]}% @ {_dec["winnerProb"]}%')
# two tiers indistinguishable under per-SKU spread → NOT decisive
_tie = BT._bootstrap_tier_ranking(_mk_tier_frame({90: 9.0, 95: 9.0, 98: 1.30, 99: 1.30}, noise=0.35),
                                  _tiers_stub, 365 / 30, 0.25)
check("a near-tie is flagged too-close-to-call", not _tie["decisive"], str(_tie["pCheapest"]))
check("near-tie splits probability between the two candidates",
      _tie["winnerProb"] < 90 and _tie["runnerUpProb"] > 10, str(_tie["pCheapest"]))
check("P(cheapest) sums to ~100 across tiers", abs(sum(_tie["pCheapest"].values()) - 100) < 0.5,
      str(sum(_tie["pCheapest"].values())))
check("ranking None with a single SKU",
      BT._bootstrap_tier_ranking(_mk_tier_frame({90: 1.0, 95: 1.0, 98: 1.0, 99: 1.0}, n_sku=1),
                                 _tiers_stub, 365 / 30, 0.25) is None)

# — end to end through _tier_summary —
_full = _mk_tier_frame({90: 6.0, 95: 3.0, 98: 1.0, 99: 0.5})
# Only for the tiers the frame actually carries. Adding cov_ for a tier with no
# lost_/safety_ behind it is not a state the production pipeline can produce.
for pct in [p for p in BT.Z if f"lost_{p}" in _full.columns]:
    _full[f"cov_{pct}"] = 1.0 if pct >= 98 else 0.0
_full["price"] = 150.0
_ts2 = BT._tier_summary(_full, coverage=30, holding_annual=0.25)
check("tier summary exposes the critical ratio", _ts2.get("criticalRatio") is not None)
# Units must be shown alongside the money, and the money must reconcile to them.
_t95x = next(t for t in _ts2["tiers"] if t["tier"] == 95)
check("tiers carry buffer units, not just buffer cost", _t95x.get("safetyUnits") is not None,
      str(_t95x.get("safetyUnits")))
check("tiers carry missed units per year", _t95x.get("unitsShortYr") is not None)
check("buffer units rise with the protection level",
      [t["safetyUnits"] for t in _ts2["tiers"]] == sorted(t["safetyUnits"] for t in _ts2["tiers"]),
      str([t["safetyUnits"] for t in _ts2["tiers"]]))
check("missed units fall as the level rises",
      [t["unitsShortYr"] for t in _ts2["tiers"]] == sorted((t["unitsShortYr"] for t in _ts2["tiers"]), reverse=True),
      str([t["unitsShortYr"] for t in _ts2["tiers"]]))
# buffer cost/yr must equal buffer units x unit cost x holding rate
_expect = _t95x["safetyUnits"] * 50.0 * 0.25       # fixture: unitCost 50, holding 25%
check("buffer cost reconciles to buffer units x cost x holding",
      abs(_t95x["holdingCost"] - _expect) < max(2.0, 0.02 * _expect),
      f'{_t95x["holdingCost"]} vs {_expect}')
# The per-product mix is the policy the app actually recommends, so it has to appear
# alongside the uniform ones — and it must not be scored with hindsight.
_mixf = []
_rng16 = np.random.default_rng(3)
for i, (vol, marg) in enumerate([(0.2, 300), (2.5, 300), (1.0, 80)]):
    for w in range(8):
        r = {"sku": f"MX{i}", "marginUnit": float(marg), "unitCost": 200.0, "costKnown": True, "price": 500.0}
        for pct, z in BT.Z.items():
            r[f"safety_{pct}"] = z * vol * 10
            r[f"lost_{pct}"] = max(0.0, _rng16.normal(6.0 - z * vol * 2.2, 1.2))
            r[f"cov_{pct}"] = 1.0
        _mixf.append(r)
_mixts = BT._tier_summary(pd.DataFrame(_mixf), coverage=30, holding_annual=0.25)
_mp = _mixts["mixedPolicy"]
check("a per-product mix policy is reported", _mp is not None)
check("the mix beats the best single level", _mp["inSampleTotal"] < min(t["totalCost"] for t in _mixts["tiers"]),
      f'{_mp["inSampleTotal"]} vs {min(t["totalCost"] for t in _mixts["tiers"])}')
check("products genuinely pick different levels", len([k for k, v in _mp["tierCounts"].items() if v]) > 1,
      str(_mp["tierCounts"]))
check("the headline figure is out-of-sample, not hindsight",
      _mp["totalCost"] >= _mp["inSampleTotal"], f'{_mp["totalCost"]} vs {_mp["inSampleTotal"]}')
check("the hindsight bonus is reported and non-negative", _mp["hindsightBonus"] >= 0,
      str(_mp["hindsightBonus"]))
check("every tuned product got a held-out estimate", _mp["skusWithHoldout"] == _mp["skusTuned"],
      f'{_mp["skusWithHoldout"]}/{_mp["skusTuned"]}')
check("the mix reports a stayed-in-stock figure from the held-out picks",
      _mp.get("achievedService") is not None and 0 <= _mp["achievedService"] <= 100,
      str(_mp.get("achievedService")))

# The verdict must FLOW BACK into the recommendations: deploy the mix only if it won.
def _mk_analysis(mix_total, uniform_costs={90: 900.0, 95: 800.0, 98: 700.0, 99: 600.0}):
    return {"tiers": [{"tier": p, "totalCost": c} for p, c in uniform_costs.items()],
            "bestTier": 99, "nearestTier": 98,
            "ranking": {"decisive": True},
            "mixedPolicy": ({"totalCost": mix_total} if mix_total is not None else None),
            "bySku": [{"sku": "PX", "costKnown": True, "lossMaking": False, "windows": 8,
                       "bestTier": 90, "tiers": {str(p): {} for p in (90, 95, 98, 99)}}],
            "assumptions": {"holdingPct": 25}}
_pol, _uni = M._combo_policy(_mk_analysis(550.0))
check("mix wins OOS → per-product picks are deployed", _pol == "mixed", f"{_pol}/{_uni}")
_pol, _uni = M._combo_policy(_mk_analysis(650.0))
check("mix loses OOS → everyone gets the winning single level", (_pol, _uni) == ("uniform", 99),
      f"{_pol}/{_uni}")
_pol, _uni = M._combo_policy(_mk_analysis(None))
check("no mix computed → uniform fallback", _pol == "uniform")
_an = _mk_analysis(650.0); _an["ranking"] = {"decisive": False}
check("coin-flip uniform pick prefers closest-to-optimal", M._combo_policy(_an) == ("uniform", 98),
      str(M._combo_policy(_an)))
# and the cache actually writes the overridden tier
M._cache_backtest_tiers({"params": {"lead": 14, "coverage": 30},
                         "tierAnalysisByCombo": {"14/30": _mk_analysis(650.0)},
                         "tierAnalysis": {}, "skipped": []})
_e = M._backtest_tier_cache.get("PX|14|30")
check("losing mix overrides the SKU's own pick in the cache", _e and _e["pct"] == 99, str(_e))
check("the cache records WHY (policy=uniform)", _e and _e.get("policy") == "uniform", str(_e))
M._cache_backtest_tiers({"params": {"lead": 14, "coverage": 30},
                         "tierAnalysisByCombo": {"14/30": _mk_analysis(550.0)},
                         "tierAnalysis": {}, "skipped": []})
_e = M._backtest_tier_cache.get("PX|14|30")
check("winning mix keeps the SKU's own pick", _e and _e["pct"] == 90 and _e.get("policy") == "mixed", str(_e))

check("a single-window product can't be held out and says so",
      BT._mixed_policy(pd.DataFrame([_mixf[0]]), _mixts["tiers"], 365 / 30, 0.25)["skusWithHoldout"] == 0)
check("no mix without costed products", BT._mixed_policy(pd.DataFrame(), _mixts["tiers"], 365 / 30, 0.25) is None)

check("per-product rows carry buffer units too",
      all(s["tiers"]["95"].get("safetyUnits") is not None for s in _ts2["bySku"]))
check("tier summary picks a nearest-to-optimal tier", _ts2.get("nearestTier") in BT.Z, str(_ts2.get("nearestTier")))
check("tier summary exposes the ranking probabilities", _ts2.get("ranking", {}).get("pCheapest") is not None)
check("per-SKU bestTier still populated for the UI",
      all(s["bestTier"] in BT.Z for s in _ts2["bySku"]), str([s["bestTier"] for s in _ts2["bySku"]][:3]))
# ties in "closest to optimal" resolve toward the cheaper (lower) tier
_flat = _full.copy()
for pct in BT.Z:
    _flat[f"cov_{pct}"] = 1.0
_tsf = BT._tier_summary(_flat, coverage=30, holding_annual=0.25)
check("equal-achieved tiers tie-break to the lowest tier", _tsf["nearestTier"] == min(BT.Z), str(_tsf["nearestTier"]))

# 15) Append-mode upload + background job -----------------------------------------
print("\n15) Append-mode upload & backtest job")

def _sheet(sku, start, days, units=4, price=100.0, cost=40.0, cat="Vanity"):
    ds = pd.date_range(start, periods=days, freq="D")
    return pd.DataFrame({"SKU": sku, "SKU_Name": sku, "Date": ds, "Units_Sold": units,
                         "Price": price, "Cost": cost, "Category": cat,
                         "Units_In_Stock": 500})

M._catalog.clear(); M._sku_cache.clear()
_h1 = _sheet("APP-A", "2025-01-01", 200)
_r1 = M._ingest(_h1.copy(), "part1.xlsx", auto_backtest=False)
_a_first = M._catalog["APP-A"]["df"]
_first_rows, _first_true_max = len(_a_first), M._true_dates(M._catalog["APP-A"])["ds"].max()
check("first upload records its date shift", "date_shift_days" in M._catalog["APP-A"])
check("true dates survive the re-anchor", _first_true_max.normalize() == pd.Timestamp("2025-07-19"),
      str(_first_true_max.date()))

# Second file continues where the first stopped — must EXTEND, not replace.
_h2 = _sheet("APP-A", "2025-07-20", 150, units=9)
_r2 = M._ingest(_h2.copy(), "part2.xlsx", append=True, auto_backtest=False)
_a2 = M._catalog["APP-A"]["df"]
check("append extends history rather than replacing it", len(_a2) >= _first_rows + 140,
      f"{_first_rows} -> {len(_a2)}")
check("append reports rows added", (_r2["dataQuality"].get("rowsAppended") or 0) >= 140,
      str(_r2["dataQuality"].get("rowsAppended")))
check("append flags which SKUs were extended", _r2["dataQuality"].get("skusExtended") == ["APP-A"],
      str(_r2["dataQuality"].get("skusExtended")))
check("append is marked in the response", _r2.get("appended") is True)
# The old rows must still be the OLD numbers — a replace would have wiped them.
_early = _a2.sort_values("ds").head(50)["y"].mean()
_late = _a2.sort_values("ds").tail(50)["y"].mean()
check("early rows keep the first file's level", abs(_early - 4) < 0.5, str(round(_early, 2)))
check("late rows carry the second file's level", abs(_late - 9) < 0.5, str(round(_late, 2)))
check("merged frame is a continuous daily calendar",
      len(_a2) == (_a2["ds"].max() - _a2["ds"].min()).days + 1, str(len(_a2)))
check("newest row is re-anchored to yesterday",
      _a2["ds"].max().normalize() == (TODAY - pd.Timedelta(days=1)).normalize(), str(_a2["ds"].max().date()))

# Overlapping re-export: same dates, corrected numbers → new file wins, no duplication.
_before = len(_a2)
_h3 = _sheet("APP-A", "2025-09-01", 30, units=99)
_r3 = M._ingest(_h3.copy(), "correction.xlsx", append=True, auto_backtest=False)
_a3 = M._catalog["APP-A"]["df"]
check("overlapping re-export doesn't duplicate rows", len(_a3) <= _before + 2, f"{_before} -> {len(_a3)}")
check("overlap is reported", (_r3["dataQuality"].get("overlapRowsReplaced") or 0) > 0,
      str(_r3["dataQuality"].get("overlapRowsReplaced")))
check("corrected values replaced the originals", (_a3["y"] == 99).sum() >= 25, str(int((_a3["y"] == 99).sum())))

# A brand-new SKU in an append lands alongside, and untouched SKUs stay aligned.
_h4 = _sheet("APP-B", "2025-03-01", 300, units=6)
M._ingest(_h4.copy(), "newsku.xlsx", append=True, auto_backtest=False)
check("append adds unseen SKUs", "APP-B" in M._catalog and "APP-A" in M._catalog)
_maxes = {s: M._catalog[s]["df"]["ds"].max().normalize() for s in ("APP-A", "APP-B")}
check("catalog shares ONE anchor after append", len(set(_maxes.values())) == 1 or
      max(_maxes.values()) == (TODAY - pd.Timedelta(days=1)).normalize(), str(_maxes))
_rel = (M._true_dates(M._catalog["APP-A"])["ds"].max() - M._true_dates(M._catalog["APP-B"])["ds"].max()).days
check("relative spacing between SKUs is preserved", _rel != 0, f"{_rel}d apart")

# replace mode still replaces
_h5 = _sheet("APP-A", "2026-01-01", 120, units=1)
M._ingest(_h5.copy(), "fresh.xlsx", auto_backtest=False)
check("replace mode still overwrites that SKU's history", len(M._catalog["APP-A"]["df"]) == 120,
      str(len(M._catalog["APP-A"]["df"])))

# catalog → frame for the fleet-sourced backtest
_cf = M.catalog_to_frame()
check("catalog_to_frame yields one long frame", {"sku", "ds", "y"} <= set(_cf.columns), str(list(_cf.columns)[:6]))
check("catalog_to_frame covers every loaded SKU", set(_cf["sku"]) == set(M._catalog), str(set(_cf["sku"])))
check("catalog_to_frame carries grouping attributes", "category" in _cf.columns or "Category" in _cf.columns,
      str([c for c in _cf.columns]))

# Replace must REPLACE. This upsert bug let the demo product (and every SKU from a
# previous file) survive an upload and get scored as if it were real data.
M._catalog.clear(); M._sku_cache.clear()
M.warmup()
M._ingest(_sheet("FRESH-1", "2025-01-01", 300), "fresh.xlsx", auto_backtest=False)
check("a replace upload evicts SKUs from the previous file", sorted(M._catalog) == ["FRESH-1"],
      str(sorted(M._catalog)))
M._ingest(_sheet("FRESH-2", "2025-01-01", 300), "second.xlsx", auto_backtest=False)
check("a second replace upload leaves only the newest file", sorted(M._catalog) == ["FRESH-2"],
      str(sorted(M._catalog)))
M._ingest(_sheet("FRESH-3", "2025-01-01", 300), "third.xlsx", append=True, auto_backtest=False)
check("append still keeps what was already there", sorted(M._catalog) == ["FRESH-2", "FRESH-3"],
      str(sorted(M._catalog)))
# Uploading must START the measured recommendation on its own — the whole point is that
# you never have to go find the Backtest tab.
M._catalog.clear(); M._sku_cache.clear()
with M._bt_job_lock:
    M._bt_job.update({"status": "idle", "startedAt": None, "trigger": None})
_up = M._ingest(_sheet("AUTO-1", "2024-06-01", 500, units=6, price=1000.0, cost=400.0), "auto.xlsx")
check("upload reports that a backtest started", _up.get("backtestStarted") is True)
check("the job is running immediately after upload", M.backtest_status()["status"] == "running",
      M.backtest_status()["status"])
check("the run is attributed to the upload", M.backtest_status().get("trigger") == "upload",
      str(M.backtest_status().get("trigger")))
import time as _t
for _ in range(240):
    if M.backtest_status()["status"] != "running": break
    _t.sleep(1)
_st = M.backtest_status()
check("the automatic run completes", _st["status"] == "done", f'{_st["status"]}: {_st.get("error")}')
check("it populates the protection cache without anyone pressing Run",
      any(k.startswith("AUTO-1|") for k in M._backtest_tier_cache), str(sorted(M._backtest_tier_cache)))
check("the cache is keyed by lead and coverage, not the SKU alone",
      "AUTO-1|14|30" in M._backtest_tier_cache, str(sorted(M._backtest_tier_cache)))
# Demo-only startup must NOT kick off a pointless run over synthetic data.
M._catalog.clear(); M._sku_cache.clear()
with M._bt_job_lock:
    M._bt_job.update({"status": "idle", "trigger": None})
M.warmup()

# The cost curve is strictly dominated: its bar is HIGHER than the backtest's, so there
# is no product it can serve that a backtest can't. Pin that, so nobody "helpfully"
# loosens one threshold later and silently reintroduces an untested recommendation.
check("the estimate's bar is never below the backtest's",
      M.ECONOMIC_TIER_MIN_DAYS >= M.BACKTEST_MIN_TESTABLE_DAYS,
      f"estimate needs {M.ECONOMIC_TIER_MIN_DAYS}d, backtest needs {M.BACKTEST_MIN_TESTABLE_DAYS}d")
check("BACKTEST_MIN_TESTABLE_DAYS matches the backtest's own floor",
      M.BACKTEST_MIN_TESTABLE_DAYS == 120 + 44, str(M.BACKTEST_MIN_TESTABLE_DAYS))

# Backtest state is session-scoped on purpose: every launch starts clean, so a test run
# can never be contaminated by a previous one.
M._backtest_tier_cache = {"P-1|14|30": {"key": "max", "pct": 99, "windows": 8, "lead": 14, "coverage": 30}}
M._backtest_exclusions = {"P-2": "some reason"}
M._last_backtest = {"tested": 9}
M.warmup()
check("a fresh start carries no measured tiers", M._backtest_tier_cache == {}, str(M._backtest_tier_cache))
check("a fresh start carries no exclusion notes", M._backtest_exclusions == {}, str(M._backtest_exclusions))
check("a fresh start carries no stored report", M._last_backtest is None, str(M._last_backtest))
check("nothing is written to disk", not __import__("os").path.exists(
    __import__("os").path.join(__import__("os").path.dirname(M.__file__), "backtest_tiers.json")))
check("/api/backtest/last is honest on a clean start", M.backtest_last().get("available") is False)

# A request arriving mid-run must be QUEUED, not dropped: uploading a file and then
# immediately changing a lead time used to lose the second run, leaving a cache measured
# for settings the user had already moved past.
with M._bt_job_lock:
    M._bt_job.update({"status": "running", "startedAt": 1.0, "queued": False})
    M._bt_pending = None
check("a mid-run request is refused for immediate start",
      M._run_backtest_job({"horizon": 44}, {}, "second") is False)
check("...but it is queued rather than dropped", M._bt_pending is not None, str(M._bt_pending))
check("the queue is visible in the status", M.backtest_status().get("queued") is True)
M._run_backtest_job({"horizon": 99}, {}, "third")
check("only the NEWEST pending request is kept", M._bt_pending["params"]["horizon"] == 99,
      str(M._bt_pending["params"]))
with M._bt_job_lock:
    M._bt_pending = None          # don't leak a stale pending into later tests

# The measured tier must not be set from evidence the report itself refuses to print.
import backtest as _BT2
check("per-SKU tier needs as many windows as the report demands",
      M.BACKTEST_TIER_MIN_WINDOWS >= _BT2.MIN_WINDOWS_REPORTABLE,
      f"tier needs {M.BACKTEST_TIER_MIN_WINDOWS}, report shows from {_BT2.MIN_WINDOWS_REPORTABLE}")

# The full report must survive the RUN (in memory), or the Backtest tab shows nothing
# after an automatic backtest — which is exactly what happened once.
M._store_backtest_result({"tested": 14, "forecasts": 112, "overall": {"MASE": 0.8}}, "upload")
check("the full report is retained, not just the tier picks", M._last_backtest["tested"] == 14)
check("it records when and why it ran",
      M._last_backtest.get("ranAt") and M._last_backtest.get("trigger") == "upload")
check("/api/backtest/last serves it in-session", M.backtest_last().get("available") is True)
M._last_backtest = None
check("/api/backtest/last is honest when there's nothing", M.backtest_last().get("available") is False)

# 16) Per-product lead/coverage must actually get measured -------------------------
# The bug: change ONE product's lead time and the catalog-wide re-run used the fleet's
# most common values, so that product stayed permanently "stale" — a fresh test was
# promised and could never satisfy it.
print("\n15b) Stacking uploads and per-file reporting")
M._catalog.clear(); M._sku_cache.clear()
M._ingest(_sheet("F1-A", "2024-06-01", 400), "fileone.xlsx", auto_backtest=False)
M._ingest(_sheet("F2-A", "2024-06-01", 400), "filetwo.xlsx", append=True, auto_backtest=False)
check("a second upload stacks instead of replacing", sorted(M._catalog) == ["F1-A", "F2-A"],
      str(sorted(M._catalog)))
_src = M.catalog_sources()
check("each product remembers which file introduced it",
      _src == {"F1-A": "fileone.xlsx", "F2-A": "filetwo.xlsx"}, str(_src))
# Extending a product records the extra file but keeps the ORIGINAL as its section.
M._ingest(_sheet("F1-A", "2025-08-01", 200), "fileone-extra.xlsx", append=True, auto_backtest=False)
check("extending keeps the introducing file as the product's section",
      M.catalog_sources()["F1-A"] == "fileone.xlsx", str(M.catalog_sources()["F1-A"]))
check("but every contributing file is recorded",
      M._catalog["F1-A"]["sources"] == ["fileone.xlsx", "fileone-extra.xlsx"],
      str(M._catalog["F1-A"]["sources"]))
_bs_frame = pd.DataFrame({
    "sku": ["F1-A"] * 4 + ["F2-A"] * 4,
    "wape": [0.3] * 8, "absErrSum": [30.0] * 8, "sumY": [100.0] * 8, "mase": [0.8] * 8,
    "bias_pct": [1.0] * 8, "coverage": [0.8] * 8, "covered": [1.0] * 8,
    "orderErrPct": [0.0] * 8, "sigma": [1.0] * 8, "cv": [10.0] * 8, "trainDays": [400] * 8,
})
_bs = BT._by_source(_bs_frame, {"F1-A": "fileone.xlsx", "F2-A": "filetwo.xlsx"})
check("the backtest sections results per file", {b["source"] for b in _bs} == {"fileone.xlsx", "filetwo.xlsx"},
      str([b["source"] for b in _bs]))
check("each section counts its own products", all(b["products"] == 1 for b in _bs), str(_bs))
check("a product with no known file still lands somewhere",
      BT._by_source(_bs_frame, {"F1-A": "fileone.xlsx"})[0]["source"] in ("(unknown file)", "fileone.xlsx"))
check("no sources means no sections", BT._by_source(_bs_frame, {}) == [])

print("\n16) Auto-sized cutoffs")
_fa16 = pd.Timestamp("2024-01-01"); _lp16 = _fa16 + pd.Timedelta(days=736)   # 900d history
check("auto = one window per horizon, not the overlapping ceiling",
      BT._auto_cutoffs(_fa16, _lp16, 44) == 17, str(BT._auto_cutoffs(_fa16, _lp16, 44)))
check("auto is far below the MIN_STEP ceiling",
      BT._auto_cutoffs(_fa16, _lp16, 44) < len(BT._cutoff_schedule(_fa16, _lp16, 10_000, 28)),
      f"auto {BT._auto_cutoffs(_fa16, _lp16, 44)} vs ceiling {len(BT._cutoff_schedule(_fa16, _lp16, 10_000, 28))}")
check("a longer horizon needs fewer windows", BT._auto_cutoffs(_fa16, _lp16, 88) < BT._auto_cutoffs(_fa16, _lp16, 44))
check("short spans still get the floor", BT._auto_cutoffs(_fa16, _fa16 + pd.Timedelta(days=5), 44) == BT.AUTO_MIN_CUTOFFS)
check("an explicit number is still honoured", BT._resolve_cutoffs(6, _fa16, _lp16, 44) == 6)
check("None falls back to auto", BT._resolve_cutoffs(None, _fa16, _lp16, 44) == 17)

_auto_df = pd.concat([
    pd.DataFrame({"sku": "L-900", "ds": pd.date_range("2024-01-01", periods=900, freq="D"),
                  "y": 6.0, "price": 800.0, "cost": 400.0, "units_in_stock": 400, "category": "V"}),
    pd.DataFrame({"sku": "S-200", "ds": pd.date_range("2024-01-01", periods=200, freq="D"),
                  "y": 6.0, "price": 800.0, "cost": 400.0, "units_in_stock": 400, "category": "V"}),
], ignore_index=True)
_ac = BT.cutoff_capacity(_auto_df, horizon=44, min_train=120, n_cutoffs="auto", step=28)
_by16 = {r["sku"]: r for r in _ac["bySku"]}
check("capacity reports auto mode", _ac["auto"] is True)
check("each product gets its OWN window count",
      _by16["L-900"]["cutoffsUsed"] > _by16["S-200"]["cutoffsUsed"],
      f'{_by16["L-900"]["cutoffsUsed"]} vs {_by16["S-200"]["cutoffsUsed"]}')
check("auto never exceeds a product's ceiling",
      all(r["cutoffsUsed"] <= r["maxCutoffs"] for r in _ac["bySku"]))
check("nothing is reported as 'capped' in auto mode", _ac["cappedSkus"] == [], str(_ac["cappedSkus"]))
_ar = BT.run_backtest(_auto_df, horizon=44, n_cutoffs="auto", step=28, verbose=False)
check("the run really produces those per-SKU counts",
      _ar.groupby("sku").size().to_dict() == {s: _by16[s]["cutoffsUsed"] for s in _by16 if _by16[s]["cutoffsUsed"]},
      str(_ar.groupby("sku").size().to_dict()))

print("\n16b) Multiple lead/coverage pairs")
check("combos always include the primary pair", BT._normalise_combos(None, 14, 30) == [(14, 30)])
check("combos de-duplicate", BT._normalise_combos([(14, 30), (21, 40), (14, 30)], 14, 30) == [(14, 30), (21, 40)],
      str(BT._normalise_combos([(14, 30), (21, 40), (14, 30)], 14, 30)))
check("_parse_combos ignores malformed entries",
      M._parse_combos([{"lead": 21, "coverage": 40}, {"lead": "x"}, None, {}]) == [(21, 40)],
      str(M._parse_combos([{"lead": 21, "coverage": 40}, {"lead": "x"}, None, {}])))

_bt_df = pd.concat([
    pd.DataFrame({"sku": "MC-1", "ds": pd.date_range("2024-06-01", periods=520, freq="D"),
                  "y": 6.0, "price": 900.0, "cost": 400.0, "units_in_stock": 400, "category": "V"}),
    pd.DataFrame({"sku": "MC-2", "ds": pd.date_range("2024-06-01", periods=520, freq="D"),
                  "y": 9.0, "price": 700.0, "cost": 300.0, "units_in_stock": 400, "category": "V"}),
], ignore_index=True)
_mc = BT.run_for_api(_bt_df, horizon=44, n_cutoffs=4, step=28, lead=14, coverage=30,
                     baseline_route=None, combos=[(21, 40)])
check("both pairs are reported back", {(c["lead"], c["coverage"]) for c in _mc["combos"]} == {(14, 30), (21, 40)},
      str(_mc["combos"]))
check("a tier analysis exists per pair", set(_mc["tierAnalysisByCombo"]) == {"14/30", "21/40"},
      str(set(_mc["tierAnalysisByCombo"])))
check("fleet accuracy is NOT double-counted across pairs",
      _mc["overall"]["skus"] == 2 and _mc["bySku"][0]["windows"] <= 4,
      f'skus={_mc["overall"].get("skus")} windows={_mc["bySku"][0].get("windows")}')
check("the extra pair is scored on the same fits (same accuracy)",
      _mc["tierAnalysisByCombo"]["21/40"].get("totalSkus") == _mc["tierAnalysisByCombo"]["14/30"].get("totalSkus"))
# Regression: the combo's coverage WINDOW (days) once overwrote the interval-coverage
# METRIC (0-1) — every band-hit figure in the app read the window x100, i.e. "3000%".
check("band hit rate is a percentage, not the window in days x100",
      _mc["overall"]["interval_cov%"] is not None and 0 < _mc["overall"]["interval_cov%"] <= 100,
      str(_mc["overall"]["interval_cov%"]))
check("headline window count matches the primary pair, not all pairs summed",
      _mc["forecasts"] == sum(r["forecasts"] for r in _mc["bySku"]),
      f'{_mc["forecasts"]} vs {sum(r["forecasts"] for r in _mc["bySku"])}')
# and the export writes a legitimate 0% instead of a blank
import exports as _EX0
_rk0 = {"pCheapest": {90: 0.0, 95: 0.0, 98: 40.0, 99: 60.0}}
_wb0 = _EX0.build_all_workbook([], [], [], backtest={
    "tierAnalysis": {"tiers": [{"tier": p, "achievedService": 90} for p in (90, 95, 98, 99)],
                     "ranking": _rk0, "assumptions": {}}, "params": {}, "overall": {}})
_plv = _wb0["Protection Levels"]
_pcol = [c.value for c in _plv[5][:6]]   # first data row (title, note, blank, header, data)
check("a genuine 0% P(cheapest) exports as 0, not blank", 0.0 in _pcol, str(_pcol))

# and the cache must serve a product asking for the non-default pair
M._cache_backtest_tiers(_mc)
_keys = sorted(M._backtest_tier_cache)
check("cache holds an entry for the non-default pair",
      any(k.endswith("|21|40") for k in _keys), str(_keys))
check("cache still holds the default pair", any(k.endswith("|14|30") for k in _keys), str(_keys))
check("status advertises which pairs have actually been measured",
      set(M.backtest_status().get("cachedCombos") or []) == {"14/30", "21/40"},
      str(M.backtest_status().get("cachedCombos")))
check("status lists the measured products", "MC-1" in (M.backtest_status().get("cachedSkus") or []),
      str(M.backtest_status().get("cachedSkus")))
# The hole this closes: a pair nobody ever asked to measure must be visibly absent, so
# the dashboard can notice and fill it instead of showing "re-testing" forever.
check("an unmeasured pair is absent from cachedCombos",
      "60/90" not in (M.backtest_status().get("cachedCombos") or []))

# Every provisional product must say WHY. Six causes previously looked identical.
print("\n16c) Reasons a product isn't measured")
M._backtest_exclusions = {}
M._record_backtest_exclusions({
    "skipped": [{"sku": "TOO-SHORT", "kind": "short", "days": 90},
                {"sku": "BLEW-UP", "kind": "error", "reason": "ValueError: boom"}],
    "tierAnalysis": {"bySku": [
        {"sku": "NO-COST", "costKnown": False, "lossMaking": False, "windows": 8, "bestTier": 95},
        {"sku": "LOSS", "costKnown": True, "lossMaking": True, "windows": 8, "bestTier": 90},
        {"sku": "THIN", "costKnown": True, "lossMaking": False, "windows": 2, "bestTier": 95},
        {"sku": "FINE", "costKnown": True, "lossMaking": False, "windows": 8, "bestTier": 99},
    ]},
})
_ex = M._backtest_exclusions
check("short history is explained", "days of sales history" in _ex.get("TOO-SHORT", ""), str(_ex.get("TOO-SHORT")))
check("a failed run is explained", "error" in _ex.get("BLEW-UP", ""), str(_ex.get("BLEW-UP")))
check("a missing cost is explained", "unit cost" in _ex.get("NO-COST", ""), str(_ex.get("NO-COST")))
check("selling below cost is explained", "at or below cost" in _ex.get("LOSS", ""), str(_ex.get("LOSS")))
check("too-few-windows is explained", "test window" in _ex.get("THIN", ""), str(_ex.get("THIN")))
check("a healthy product gets no excuse", "FINE" not in _ex, str(_ex.get("FINE")))

check("a measured product has no reason at all",
      M._why_provisional("FINE", "backtest", 900, 14, 30, None) is None)
check("a running job says so",
      "running right now" in (M._why_provisional("X", "calculating", 900, 14, 30, None) or ""))
_why_stale = M._why_provisional("X", "stale", 900, 21, 40, {"lead": 14, "coverage": 30})
check("a settings mismatch names both the old and new numbers",
      "14-day lead" in _why_stale and "21 and 40" in _why_stale, _why_stale)
check("thin history is explained with how many more days are needed",
      "more days" in (M._why_provisional("BRAND-NEW", "economics", 100, 14, 30, None) or ""),
      str(M._why_provisional("BRAND-NEW", "economics", 100, 14, 30, None)))
check("a recorded exclusion is quoted back",
      "unit cost" in (M._why_provisional("NO-COST", "economics", 900, 14, 30, None) or ""),
      str(M._why_provisional("NO-COST", "economics", 900, 14, 30, None)))
check("exclusions persist for the whole session",
      "unit cost" in M._backtest_exclusions.get("NO-COST", ""),
      str(M._backtest_exclusions.get("NO-COST")))
check("a lookup for 21/40 no longer returns the 14/30 measurement",
      (M._backtest_tier_cache.get(M._tier_cache_key("MC-1", 21, 40)) or {}).get("coverage") == 40,
      str(M._backtest_tier_cache.get(M._tier_cache_key("MC-1", 21, 40))))
with M._bt_job_lock:
    M._bt_job.update({"status": "idle", "startedAt": None, "queued": False})
    M._bt_pending = None

# job registry
_snap = M._bt_job_snapshot()
check("job starts idle", _snap["status"] in ("idle", "done", "error"), _snap["status"])
with M._bt_job_lock:
    M._bt_job.update({"status": "running", "startedAt": 1.0})
check("a second run is refused while one is in flight",
      M._run_backtest_job({"horizon": 44}, {}, "test") is False)
check("status endpoint reports running", M.backtest_status()["status"] == "running")
with M._bt_job_lock:
    M._bt_job.update({"status": "idle", "startedAt": None})


# 17) Costs must survive without the browser's help ---------------------------------
# A dashboard-triggered run with an empty costs payload used to go costless, wiping the
# measured tiers and stamping every product "no unit cost on file" — even though the
# sheet carried costs and the Scorecard showed them.
print("\n17) Server-side cost memory")
M._catalog.clear(); M._sku_cache.clear(); M._sheet_costs.clear()
M._ingest(_sheet("CM-1", "2024-06-01", 520, price=900.0, cost=400.0), "c1.xlsx", auto_backtest=False)
check("ingest remembers the sheet's costs", M._sheet_costs.get("CM-1") == 400.0, str(M._sheet_costs))
M._ingest(_sheet("CM-2", "2024-06-01", 520, price=700.0, cost=300.0), "c2.xlsx",
          append=True, auto_backtest=False)
check("append accumulates costs", set(M._sheet_costs) == {"CM-1", "CM-2"}, str(M._sheet_costs))
_eff = M._effective_sku_costs(None)
check("no payload → sheet costs still flow", _eff["CM-1"]["cost"] == 400.0, str(_eff))
_eff = M._effective_sku_costs({"CM-1": {"cost": 425.0, "fees": 10.0}})
check("Scorecard entries win over the sheet", _eff["CM-1"]["cost"] == 425.0 and _eff["CM-2"]["cost"] == 300.0,
      str(_eff))
# End to end: an EMPTY costs payload must still produce a priced run.
import time as _t17
M.backtest_refresh({"lead": 14, "coverage": 30})          # note: no "costs" key at all
for _ in range(300):
    if M.backtest_status()["status"] != "running": break
    _t17.sleep(1)
check("a costless payload still yields a priced run",
      any(k.startswith("CM-1|") for k in M._backtest_tier_cache), str(sorted(M._backtest_tier_cache)))
check("no product is falsely stamped 'no unit cost'",
      not any("unit cost" in v for v in M._backtest_exclusions.values()), str(M._backtest_exclusions))
M._ingest(_sheet("CM-3", "2024-06-01", 520), "c3.xlsx", auto_backtest=False)   # replace mode
check("a replace upload resets cost memory to the new file", set(M._sheet_costs) == {"CM-3"},
      str(M._sheet_costs))
# The queue replays whatever costs a request captured when it was queued — which can be
# empty. The merge must happen at RUN time so even a stale request runs priced.
# (CM-3's sheet cost of 40 against its 100 price is already in _sheet_costs.)
M._backtest_tier_cache = {}; M._backtest_exclusions = {}
M._run_backtest_job({"horizon": 44, "n_cutoffs": 4, "step": 28, "lead": 14, "coverage": 30,
                     "service_pct": 95, "min_train": 120, "holding_pct": 25}, {}, "stale-pending")
for _ in range(240):
    if M.backtest_status()["status"] != "running": break
    _t17.sleep(1)
check("a job started with EMPTY captured costs still runs priced",
      any(k.startswith("CM-3|") for k in M._backtest_tier_cache), str(sorted(M._backtest_tier_cache)))

# 18) Changed economics must invalidate a measured tier ----------------------------
# Lead time and coverage were already in the cache key. Cost, fees and holding were not,
# so editing a cost left last week's figure sitting there labelled "measured".
print("\n17b) Partial costs")
# Money totals are sums over costed products, so a missing cost silently shifts them —
# and can flip which level wins. Accuracy must be unaffected either way.
_pc_rng = np.random.default_rng(4)
_pc_base = [pd.DataFrame({"sku": s, "ds": pd.date_range("2024-06-01", periods=520, freq="D"),
                          "y": _pc_rng.poisson(6, 520).astype(float), "price": pr, "cost": co,
                          "units_in_stock": 400, "category": "V"})
            for s, pr, co in [("PA", 900.0, 400.0), ("PB", 700.0, 300.0), ("PC", 1200.0, 500.0)]]
_pc_full = pd.concat(_pc_base, ignore_index=True)
_pc_part = _pc_full.copy(); _pc_part.loc[_pc_part["sku"] == "PC", "cost"] = np.nan
_r_part = BT.run_for_api(_pc_part, horizon=44, n_cutoffs=4, lead=14, coverage=30, baseline_route=None)
_r_full = BT.run_for_api(_pc_full, horizon=44, n_cutoffs=4, lead=14, coverage=30, baseline_route=None)
_ta_p, _ta_f = _r_part["tierAnalysis"], _r_full["tierAnalysis"]
check("partial costs are flagged as partial", _ta_p["costBasis"] == "partial", _ta_p["costBasis"])
check("the uncosted product is named", _ta_p["uncostedSkus"] == ["PC"], str(_ta_p["uncostedSkus"]))
check("complete costs report no gap",
      _ta_f["costBasis"] == "actual" and _ta_f["uncostedSkus"] == [], str(_ta_f["uncostedSkus"]))
check("accuracy does NOT depend on cost", _r_part["overall"]["MASE"] == _r_full["overall"]["MASE"],
      f'{_r_part["overall"]["MASE"]} vs {_r_full["overall"]["MASE"]}')
check("every product is still accuracy-tested when uncosted",
      _r_part["overall"]["skus"] == 3, str(_r_part["overall"]["skus"]))
_cost_p = next(t for t in _ta_p["tiers"] if t["tier"] == _ta_p["bestTier"])["totalCost"]
_cost_f = next(t for t in _ta_f["tiers"] if t["tier"] == _ta_f["bestTier"])["totalCost"]
check("money totals really do change once the missing cost lands", _cost_p != _cost_f,
      f"${_cost_p:,.0f} -> ${_cost_f:,.0f}")
check("an uncosted product is excluded from the money, not the run",
      all(r["sku"] != "PC" or r["costKnown"] is False for r in _ta_p["bySku"]),
      str([(r["sku"], r["costKnown"]) for r in _ta_p["bySku"]]))

print("\n17c) Re-pricing without refitting")
# Costs never touch a forecast, so entering one later must NOT trigger a refit.
_rc_rows = pd.concat([
    pd.DataFrame({"sku": s, "ds": pd.date_range("2024-06-01", periods=520, freq="D"),
                  "y": np.random.default_rng(21).poisson(6, 520).astype(float),
                  "price": pr, "units_in_stock": 400, "category": "V"})
    for s, pr in [("RC-1", 900.0), ("RC-2", 700.0)]], ignore_index=True)
_sink = []
_rc_res = BT.run_for_api(_rc_rows, horizon=44, n_cutoffs=4, lead=14, coverage=30,
                         baseline_route=None, rows_sink=_sink)
check("scored rows are handed back for re-pricing", _sink and not _sink[0].empty)
check("with no costs there is no money analysis", _rc_res["tierAnalysis"].get("priced") is False,
      str(_rc_res["tierAnalysis"].get("priced")))
_recosted = BT.recost(_rc_res, _sink[0], {"RC-1": {"cost": 400.0, "fees": 0.0},
                                          "RC-2": {"cost": 300.0, "fees": 0.0}},
                      holding_pct=25, coverage=30, combos=[(14, 30)])
check("re-pricing turns the money analysis on", _recosted["tierAnalysis"]["priced"] is True)
check("re-pricing costs every product", _recosted["tierAnalysis"]["costedSkus"] == 2,
      str(_recosted["tierAnalysis"]["costedSkus"]))
check("accuracy is byte-identical after re-pricing",
      _recosted["overall"] == _rc_res["overall"], "accuracy changed — costs leaked into the forecast")
check("the run is marked as re-priced", _recosted.get("recosted") is True)
# a partial re-cost must keep the product it wasn't given
_partial_rc = BT.recost(_recosted, _sink[0], {"RC-1": {"cost": 450.0, "fees": 0.0}},
                        holding_pct=25, coverage=30, combos=[(14, 30)])
check("re-pricing without a product's cost doesn't erase it",
      _partial_rc["tierAnalysis"]["costedSkus"] >= 1, str(_partial_rc["tierAnalysis"]["costedSkus"]))
check("a higher holding rate changes the money",
      BT.recost(_rc_res, _sink[0], {"RC-1": {"cost": 400.0}, "RC-2": {"cost": 300.0}},
                holding_pct=60, coverage=30, combos=[(14, 30)])["tierAnalysis"]["tiers"][0]["holdingCost"]
      != _recosted["tierAnalysis"]["tiers"][0]["holdingCost"])
check("nothing to re-price degrades safely", BT.recost(_rc_res, pd.DataFrame(), {}) == _rc_res)

print("\n17d) Cost must never move a measured in-stock rate")
# Stayed-in-stock is measured from demand vs forecast+buffer — no cost anywhere in it.
# If a cost edit ever shifts a per-level percentage, costs have leaked into a measurement.
_inv = []
for i in range(6):
    for w in range(8):
        _r = {"sku": f"IV{i}", "price": 500.0, "unitCost": 200.0, "costKnown": True,
              "marginUnit": 300.0, "lead": 14, "coverage": 30}
        for _pct, _z in BT.Z.items():
            _r[f"cov_{_pct}"] = float(w < {90: 6, 95: 7, 98: 8, 99: 8, 99.5: 8}[_pct])
            _r[f"lost_{_pct}"] = {90: 5.0, 95: 3.0, 98: 1.0, 99: 0.5, 99.5: 0.25}[_pct]
            _r[f"safety_{_pct}"] = _z * 12
        _inv.append(_r)
_invf = pd.DataFrame(_inv)
_cheap = BT._tier_summary(_invf.assign(unitCost=40.0, marginUnit=460.0), coverage=30, holding_annual=0.25)
_dear = BT._tier_summary(_invf.assign(unitCost=460.0, marginUnit=40.0), coverage=30, holding_annual=0.25)
_svc = lambda t: {x["tier"]: x["achievedService"] for x in t["tiers"]}
check("per-level in-stock rates are identical under different costs", _svc(_cheap) == _svc(_dear),
      f"{_svc(_cheap)} vs {_svc(_dear)}")
check("...even though the cheapest level moves", _cheap["bestTier"] != _dear["bestTier"],
      f'{_cheap["bestTier"]} vs {_dear["bestTier"]}')
check("so the headline changes only because a different ROW is shown",
      _svc(_cheap)[_cheap["bestTier"]] != _svc(_dear)[_dear["bestTier"]] or
      _cheap["bestTier"] != _dear["bestTier"])

print("\n17e) 'Download everything' really includes everything")
import exports as _EX, io as _io
_bt_stub = {
    "tested": 2, "forecasts": 16, "failedCutoffs": 0, "ranAt": 1.0,
    "params": {"lead": 14, "coverage": 30},
    "overall": {"MASE": 0.8, "MASE_ci": [0.7, 0.9], "WAPE%": 44.0, "bias%": 1.2,
                "interval_cov%": 79.0, "service_achieved%": 91.0},
    "bySku": [{"sku": "X-1", "source": "cat.xlsx", "daysHistory": 520, "engine": "prophet",
               "reportable": True, "forecasts": 8, "MASE": 0.8},
              {"sku": "X-2", "source": "cat.xlsx", "daysHistory": 520, "engine": "prophet",
               "reportable": False, "forecasts": 2, "MASE": 0.9}],
    "bySource": [{"source": "cat.xlsx", "products": 2, "forecasts": 16, "MASE": 0.8}],
    "skipped": [{"sku": "X-9", "reason": "only 40d of history", "days": 40, "kind": "short"}],
    "tierAnalysis": {
        "assumptions": {"holdingPct": 25}, "costBasis": "partial",
        "costedSkus": 1, "totalSkus": 2, "uncostedSkus": ["X-2"], "bestTier": 99,
        "ranking": {"pCheapest": {"90": 0, "95": 0, "98": 40, "99": 60}},
        "tiers": [{"tier": p, "achievedService": 80 + p / 10, "achievedServiceCI": [70, 95],
                   "safetyUnits": p, "unitsShortYr": 100 - p, "stockoutCost": 10.0,
                   "holdingCost": 5.0, "totalCost": 15.0, "bufferCash": 50.0} for p in (90, 95, 98, 99)],
        "bySku": [{"sku": "X-1", "costKnown": True, "lossMaking": False, "windows": 8, "bestTier": 99,
                   "tiers": {str(p): {"unitsYr": 1.0, "safetyUnits": 2.0, "profitYr": 3.0,
                                      "totalCostYr": 4.0} for p in (90, 95, 98, 99)}}],
        "mixedPolicy": {"totalCost": 14.0, "inSampleTotal": 13.0, "achievedService": 92.0,
                        "tierCounts": {"90": 1, "99": 1}},
    },
}
_wb = _EX.build_all_workbook([], [], [], backtest=_bt_stub)
_names = set(_wb.sheetnames)
for _want in ("Fleet Summary", "Reorder Plan", "Monthly Forecast", "Scorecard", "Open POs",
              "Suppliers", "Order History", "Backtest Summary", "Backtest by Product",
              "Backtest by File", "Protection Levels", "Levels by Product", "Backtest Not Tested"):
    check(f"'{_want}' sheet is in the full export", _want in _names, str(sorted(_names)))
_txt = "\n".join(str(c.value) for r in _wb["Backtest Summary"].iter_rows() for c in r if c.value)
check("the export carries the cost-coverage caveat", "Products missing a cost" in _txt and "X-2" in _txt)
check("the export names the policy actually in use", "Protection policy in use" in _txt, _txt[:80])
check("the export carries the hindsight figure", "Hindsight" in _txt)
_ptxt = "\n".join(str(c.value) for r in _wb["Backtest by Product"].iter_rows() for c in r if c.value)
check("thin products are flagged in the export", "NO - too few" in _ptxt)
_buf = _io.BytesIO(); _wb.save(_buf)
check("the full workbook saves without error", len(_buf.getvalue()) > 5000, f"{len(_buf.getvalue())} bytes")
check("no backtest still produces a valid workbook",
      "Fleet Summary" in _EX.build_all_workbook([], [], [], backtest=None).sheetnames)

print("\n17k) Slow sellers must not read as 'zero per day'")
from forecast_engine import sparse_subtype as _subtype
_rng_s = np.random.default_rng(5)
_ds_s = pd.date_range(TODAY - pd.Timedelta(days=739), periods=740, freq="D")
_sell = _rng_s.random(740) < 0.45
_ys = np.where(_sell, 1 + _rng_s.poisson(1.3, 740), 0)
M._catalog.clear(); M._sku_cache.clear()
M._ingest(pd.DataFrame({"SKU": "SLOW-1", "SKU_Name": "Slow", "Date": _ds_s, "Units_Sold": _ys,
                        "Price": 319.0, "Cost": 272.0, "Category": "F", "Units_In_Stock": 322}),
          "slow.xlsx", auto_backtest=False)
_fs = M.get_forecast(sku_id="SLOW-1", stock=322, lead_time_days=14, coverage_days=30, unit_cost=272.0)
_daily = [p["y"] for p in _fs["chartDataFuture"][:90]]
check("daily forecast is NOT rounded to whole units", any(0 < v < 1 or v % 1 for v in _daily),
      str(_daily[:5]))
check("...so 90 days doesn't sum to zero", sum(_daily) > 10, str(round(sum(_daily), 1)))
check("a regular trickle is called low-volume regular, not rare",
      _fs["sparseSubtype"] == "low_volume_regular", str(_fs["sparseSubtype"]))
check("it gets a plain-language demand story", bool(_fs["demandStory"]))
check("...phrased per MONTH, not per day", "a month" in (_fs["demandStory"] or ""), _fs["demandStory"] or "")
check("...and normalises the zero-sale days", "not a warning sign" in (_fs["demandStory"] or ""))

# genuinely rare demand must be described differently
_rare = np.zeros(740); _rare[::40] = 6
check("rare bursty demand is called true intermittent",
      _subtype(_rare, "lumpy") == "true_intermittent", _subtype(_rare, "lumpy"))
check("a healthy seller gets no sparse subtype", _subtype(np.full(740, 8.0), "smooth") == "")

# the guardrail: model rate far below what the product is actually doing
_e = M._sku_cache["SLOW-1"]
_orig = _e["forecast"].copy()
_e["forecast"]["yhat"] = _e["forecast"]["yhat"] * 0.35        # simulate a collapsed rate
_fs2 = M.get_forecast(sku_id="SLOW-1", stock=322, lead_time_days=14, coverage_days=30, unit_cost=272.0)
_rc = _fs2.get("rateCheck")
check("a collapsed forecast rate is flagged", _rc is not None)
check("...naming both rates", _rc and _rc["forecastRate"] < _rc["recentRate"],
      str(_rc and (_rc["forecastRate"], _rc["recentRate"])))
check("...and how far below", _rc and _rc["shortfallPct"] >= 30, str(_rc and _rc["shortfallPct"]))
check("...explaining WHY sparse models do this",
      _rc and "zero-sale days" in _rc["message"], (_rc or {}).get("message", "")[:60])
_e["forecast"] = _orig
check("a forecast that tracks recent sales is NOT flagged",
      M.get_forecast(sku_id="SLOW-1", stock=322, lead_time_days=14,
                     coverage_days=30, unit_cost=272.0).get("rateCheck") is None)

# TSB decays demand probability through zero runs — right for a dying product, wrong for
# a regular trickle. It must not even be a candidate for low-volume regular demand.
_eng = M._sku_cache["SLOW-1"]["engine"]
check("a regular trickle is NOT modelled with TSB",
      "TSB" not in (M._sku_cache["SLOW-1"].get("winning_model") or ""),
      str(M._sku_cache["SLOW-1"].get("winning_model")))
check("...and is labelled as slow recurring, not intermittent",
      "slow recurring" in (M._sku_cache["SLOW-1"].get("winning_model") or "").lower(),
      str(M._sku_cache["SLOW-1"].get("winning_model")))
check("the engine explains fractional demand in plain terms",
      "fractional per day" in ((_eng.explain_bits() or {}).get("seasonality") or {}).get("text", ""),
      str(((_eng.explain_bits() or {}).get("seasonality") or {}).get("text", ""))[:70])
# genuinely rare demand keeps TSB available
_rare_y = np.zeros(740); _rare_y[::37] = 5
M._ingest(pd.DataFrame({"SKU": "RARE-1", "SKU_Name": "Rare", "Date": _ds_s, "Units_Sold": _rare_y,
                        "Price": 400.0, "Cost": 200.0, "Category": "F", "Units_In_Stock": 50}),
          "rare.xlsx", append=True, auto_backtest=False)
check("rare bursty demand still allows TSB",
      "intermittent demand" in (M._sku_cache["RARE-1"].get("winning_model") or ""),
      str(M._sku_cache["RARE-1"].get("winning_model")))

# the export must not render a slow seller as a column of zeros
import exports as _EXs
_vs = M._export_view("SLOW-1", {"stock": 322, "unitCost": 272.0, "leadTime": 14, "coverage": 30})
_wbs = _EXs.build_sku_workbook(_vs)
_ovr = {r[0]: r[1] for r in _wbs["Overview"].iter_rows(values_only=True) if r and r[0]}
check("the export leads with how it sells", "How it sells" in _ovr and _ovr["How it sells"] != "—")
_frows = [r for r in _wbs["Daily Forecast"].iter_rows(values_only=True) if r and r[1] == "Forecast"]
check("exported daily units are fractional, not zeros",
      any(isinstance(r[2], float) and 0 < r[2] < 2 for r in _frows[:5]), str(_frows[:2]))
check("the units column is formatted to 2dp for slow sellers",
      _wbs["Daily Forecast"].cell(6, 3).number_format == "0.00",
      _wbs["Daily Forecast"].cell(6, 3).number_format)

print("\n17j) Stock on the way counts from when it LANDS")
# Units on order used to be added to today's shelf regardless of arrival date, so a
# shipment 90 days out looked like stock in the warehouse and suppressed a real reorder.
M._catalog.clear(); M._sku_cache.clear()
_po_ds = pd.date_range(TODAY - pd.Timedelta(days=399), periods=400, freq="D")
M._ingest(pd.DataFrame({"SKU": "ETA-1", "SKU_Name": "ETA", "Date": _po_ds, "Units_Sold": 10,
                        "Price": 500.0, "Cost": 200.0, "Category": "V", "Units_In_Stock": 300}),
          "eta.xlsx", auto_backtest=False)
_f = lambda eta: M.get_forecast(sku_id="ETA-1", stock=100, lead_time_days=14, coverage_days=30,
                                units_on_order=500, on_order_eta_days=eta, unit_cost=200.0)
_soon, _late, _none = _f(3), _f(90), _f(None)
check("a shipment landing soon pushes the stockout out",
      _soon["daysUntilStockout"] > 30, str(_soon["daysUntilStockout"]))
check("a shipment 90 days out does NOT",
      _late["daysUntilStockout"] < 15, str(_late["daysUntilStockout"]))
check("...and it still triggers a reorder", _late["orderQty"] > 0, str(_late["orderQty"]))
check("a late shipment isn't counted as stock at delivery",
      _late["stockAtDelivery"] == 0, str(_late["stockAtDelivery"]))
check("no ETA keeps the old behaviour (available now)",
      _none["daysUntilStockout"] == _soon["daysUntilStockout"],
      f'{_none["daysUntilStockout"]} vs {_soon["daysUntilStockout"]}')
check("a shipment arriving exactly at the new delivery date still counts",
      _f(14)["stockAtDelivery"] > 0, str(_f(14)["stockAtDelivery"]))
check("zero on-order is unaffected by an ETA",
      M.get_forecast(sku_id="ETA-1", stock=100, lead_time_days=14, coverage_days=30,
                     units_on_order=0, on_order_eta_days=5)["daysUntilStockout"]
      == M.get_forecast(sku_id="ETA-1", stock=100, lead_time_days=14, coverage_days=30,
                        units_on_order=0)["daysUntilStockout"])

print("\n17i) The cost-curve fallback must use the SET holding rate")
# It defaulted to 25% regardless, so a product falling back to the estimate was priced on
# an assumption the rest of the app had already replaced.
M._catalog.clear(); M._sku_cache.clear(); M._sheet_costs.clear()
M._ingest(_sheet("HC-1", "2025-01-01", 300, price=900.0, cost=400.0), "hc.xlsx", auto_backtest=False)
_seen = {}
for _rate in (10, 25, 45):
    M.set_holding({"holding": _rate})
    _p = M.get_forecast(sku_id="HC-1", unit_cost=400.0, lead_time_days=14, coverage_days=30)["protection"]
    _seen[_rate] = (_p["servicePct"], _p["reason"])
    check(f"the reason quotes {_rate}%/yr, not the default", f"{_rate}%/yr" in _p["reason"],
          _p["reason"][-70:])
check("a higher holding rate never RAISES the recommended level",
      _seen[45][0] <= _seen[10][0], f'10%->{_seen[10][0]}%  45%->{_seen[45][0]}%')
check("the economics block reports the rate actually used",
      (M.get_forecast(sku_id="HC-1", unit_cost=400.0, lead_time_days=14,
                      coverage_days=30)["protection"].get("economics") or {}).get("holdingPct") == 45)
M.set_holding({"holding": 25})

print("\n17h) Recompute granularity: right work for the right change")
# Session holding rate must be usable BEFORE any data is loaded, and the upload's own
# automatic run must honour it (it used to hardcode 25%).
M.set_holding({"holding": 33})
check("holding can be set with nothing loaded", M.get_holding()["holding"] == 33, str(M.get_holding()))
try:
    M.set_holding({"holding": 900}); _bad = False
except Exception as ex:
    _bad = getattr(ex, "status_code", None) == 400
check("out-of-range holding is rejected with a 400", _bad)
try:
    M.set_holding({"holding": "abc"}); _bad2 = False
except Exception as ex:
    _bad2 = getattr(ex, "status_code", None) == 400
check("non-numeric holding is rejected", _bad2)
M.set_holding({"holding": 25})

# merge_rows: fresh rows replace stale ones for the same (product, lead, coverage) only
_mk = lambda sku, L, C, tag: pd.DataFrame([{"sku": sku, "leadDays": L, "coverageDays": C, "tag": tag}])
_old = pd.concat([_mk("A", 14, 30, "old"), _mk("B", 14, 30, "old")], ignore_index=True)
_new = pd.concat([_mk("A", 14, 30, "new"), _mk("A", 21, 40, "new")], ignore_index=True)
_m = BT.merge_rows(_old, _new)
check("a refit replaces that product's rows for the same window",
      list(_m[(_m.sku == "A") & (_m.leadDays == 14)]["tag"]) == ["new"], str(_m.to_dict("records")))
check("untouched products keep their original rows",
      list(_m[_m.sku == "B"]["tag"]) == ["old"], str(_m.to_dict("records")))
check("a new window is added, not swapped in", len(_m[(_m.sku == "A")]) == 2, str(len(_m[_m.sku == "A"])))
check("merging into nothing just takes the new rows", len(BT.merge_rows(None, _new)) == 2)
check("merging nothing in keeps the old rows", len(BT.merge_rows(_old, None)) == 2)

print("\n17g) 'partial' cost basis must mean COSTS ARE MISSING")
# A catalog where every cost is present but one item sells below cost reported "partial"
# with no missing product to name — two different facts sharing one label.
_cb_rows = []
for i in range(4):
    for w in range(6):
        r = {"sku": f"CB{i}", "price": 500.0, "unitCost": 200.0, "costKnown": True,
             # CB3 sells below cost: excluded from the money, but its cost IS known
             "marginUnit": (-50.0 if i == 3 else 300.0), "leadDays": 14, "coverageDays": 30}
        for pct, z in BT.Z.items():
            r[f"cov_{pct}"] = 1.0; r[f"lost_{pct}"] = 1.0; r[f"safety_{pct}"] = z * 10
        _cb_rows.append(r)
_cb = BT._tier_summary(pd.DataFrame(_cb_rows), coverage=30, holding_annual=0.25)
check("all costs known → basis is 'actual' even with a loss-maker",
      _cb["costBasis"] == "actual", _cb["costBasis"])
check("the loss-maker is still named separately", _cb["lossMakingSkus"] == ["CB3"],
      str(_cb["lossMakingSkus"]))
check("nothing is falsely listed as missing a cost", _cb["uncostedSkus"] == [], str(_cb["uncostedSkus"]))
_cb2_rows = [dict(r, costKnown=(r["sku"] != "CB2"), marginUnit=300.0) for r in _cb_rows]
_cb2 = BT._tier_summary(pd.DataFrame(_cb2_rows), coverage=30, holding_annual=0.25)
check("a genuinely missing cost still reports 'partial'", _cb2["costBasis"] == "partial", _cb2["costBasis"])
check("...and names the product", _cb2["uncostedSkus"] == ["CB2"], str(_cb2["uncostedSkus"]))

print("\n17f) Missing-margin reason must name the RIGHT missing input")
# A product with a real cost but no price column reported "no unit cost on file",
# sending you to fix something that was never wrong.
_k, _r = M.recommend_protection(None, has_price=False, has_cost=True)
check("cost present, price missing → blames the PRICE", "no selling price" in _r, _r)
check("...and does not claim the cost is missing", "No unit cost on file" not in _r, _r)
_k, _r = M.recommend_protection(None, has_price=True, has_cost=False)
check("price present, cost missing → blames the COST", "No unit cost on file" in _r, _r)
_k, _r = M.recommend_protection(None, has_price=False, has_cost=False)
check("both missing → says both", "Neither" in _r, _r)
_k, _r = M.recommend_protection(42.0)
check("a known margin still explains itself normally", "42% margin" in _r, _r)
check("every branch still defaults to Standard",
      all(M.recommend_protection(None, has_price=p, has_cost=c)[0] == "standard"
          for p in (True, False, None) for c in (True, False, None)))

print("\n18) Economics drift")
M._record_backtest_inputs({"D-1": {"cost": 400.0, "fees": 10.0}}, 25)
check("unchanged economics are not flagged", M._economics_drifted("D-1", 400.0, 10.0, 25) is None)
check("a rounding-level cost change is tolerated", M._economics_drifted("D-1", 400.5, 10.0, 25) is None,
      str(M._economics_drifted("D-1", 400.5, 10.0, 25)))
check("a real cost change is caught", "unit cost changed" in (M._economics_drifted("D-1", 500.0, 10.0, 25) or ""),
      str(M._economics_drifted("D-1", 500.0, 10.0, 25)))
check("a fee change is caught", "fees changed" in (M._economics_drifted("D-1", 400.0, 25.0, 25) or ""))
check("a holding-rate change is caught",
      "holding rate changed" in (M._economics_drifted("D-1", 400.0, 10.0, 40) or ""),
      str(M._economics_drifted("D-1", 400.0, 10.0, 40)))
check("an unknown product isn't flagged", M._economics_drifted("NOPE", 999.0, 0.0, 25) is None)
check("no recorded inputs → nothing to drift from",
      (M._backtest_inputs.clear(), M._economics_drifted("D-1", 999.0, 0.0, 99))[1] is None)

# and the drift must actually block the measured recommendation
M._record_backtest_inputs({"D-2": {"cost": 400.0, "fees": 0.0}}, 25)
_rec = {"key": "max", "pct": 99, "windows": 8, "lead": 14, "coverage": 30, "holdingPct": 25}
_k, _r, _src, _e = M.recommend_economic_protection(
    price=900.0, unit_cost=400.0, fees=0.0, margin_pct=55.0, demand_spread=5.0,
    coverage_days=30, days_history=500, total_sales=3000, demand_class="smooth",
    backtest_rec=_rec, lead_time_days=14, sku_id="D-2")
check("unchanged economics still use the measured tier", _src == "backtest", _src)
_k, _r, _src, _e = M.recommend_economic_protection(
    price=900.0, unit_cost=600.0, fees=0.0, margin_pct=33.0, demand_spread=5.0,
    coverage_days=30, days_history=500, total_sales=3000, demand_class="smooth",
    backtest_rec=_rec, lead_time_days=14, sku_id="D-2")
check("a changed cost stops the measured tier being served", _src != "backtest", _src)
check("the panel explains the drift",
      "unit cost changed" in (M._why_provisional("D-2", _src, 500, 14, 30, _rec, 600.0, 0.0) or ""),
      str(M._why_provisional("D-2", _src, 500, 14, 30, _rec, 600.0, 0.0)))
M._backtest_inputs.clear()

print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
print("All routing checks passed. (libs stubbed — run locally for real numerics)")

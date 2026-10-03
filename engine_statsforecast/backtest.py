"""
backtest.py — leakage-safe rolling-origin backtest for the LogiTrack engines.

It re-runs the REAL router/engines at multiple historical cutoffs and scores two
things that matter:

  1) FORECAST ACCURACY  — WAPE, MASE (vs a seasonal-naive baseline), bias, and
                          80%-interval coverage.
  2) REORDER-DECISION QUALITY — the service level the buffer ACTUALLY achieved
                          vs the target, and how far the order quantity was over/
                          under what hindsight needed.

Design rules (so the numbers are honest, not flattering):
  • No lookahead. At each cutoff the model only ever sees data ≤ that date —
    enforced by reusing build_entry(today=cutoff), which strips future rows.
  • Reuse the real code. Forecasts come from the actual engines, never a copy.
  • Honest metrics. MASE/WAPE (not MAPE), signed bias, interval coverage, and a
    real naive baseline to beat.
  • Only SKUs with enough history are scored; new/sparse ones can't be backtested
    (which is itself a finding).

Usage:
  python backtest.py sales.xlsx
  python backtest.py sales.csv --horizon 44 --cutoffs 8 --step 28 --lead 14 \
                     --coverage 30 --service 95 --min-train 120 --xlsx report.xlsx
  python backtest.py --shopify         # reads SHOPIFY_* env vars (see shopify_source.py)
"""
from __future__ import annotations
import argparse
import json
import numpy as np
import pandas as pd

import router as R
from main import build_entry, lead_window_sigma, _exclude_from_pooling   # the REAL routing + engines — no reimplementation
from forecast_engine import fill_daily_gaps
import censoring as _CEN
import holiday_cover as _HCV

# The self-calibration inside build_entry refits the engine per holdout window. The
# backtest ALREADY refits at every cutoff, so run it with fewer windows — the buffer
# test still gets real out-of-sample residuals, at a fraction of the cost.
BT_CALIB_WINDOWS = 2

# Cutoff scheduling. Cutoffs are spread across each SKU's OWN testable span rather
# than all clustered in the recent past: 8 cutoffs at a fixed 28d step only ever probe
# the last ~8 months, so a model with yearly seasonality is never tested on the other
# four. Half the cutoffs stay dense-and-recent (the regime you're actually operating
# in), the rest spread back across the remaining history for seasonal coverage. The
# two blocks are reported separately — blending them hides a regime change.
MIN_STEP = 14          # cutoffs closer than this are near-duplicates, not new evidence
RECENT_FRACTION = 0.5  # share of cutoffs held at the dense recent step

# `n_cutoffs=AUTO_CUTOFFS` sizes the test per product instead of asking for a number.
# Each SKU gets as many windows as its own testable span holds WITHOUT overlapping:
# span / horizon, floored at 3 (below that nothing is readable) and capped at MIN_STEP
# spacing. Overlapping windows aren't worthless, but at a 44-day horizon and 14-day
# spacing neighbours share 68% of their days, so cramming in 3x more re-measures the same
# demand rather than adding evidence — 6x the model fits for a fraction of the precision.
AUTO_CUTOFFS = "auto"
# 4, not 3, and it has to stay in step with MIN_WINDOWS_REPORTABLE below and with
# BACKTEST_TIER_MIN_WINDOWS in main.py. At 3 the three numbers disagreed, and the gap was
# pure waste: a product with 207-299 days of history got floored at 3 windows, was fully
# backtested — every engine refit at every cutoff — and then had its result DISCARDED,
# because using a measured tier needs 4. Nothing surfaced it; the product just showed a
# provisional tier as though it had never been tested.
#
# Note this floor only sizes the REQUEST. _cutoff_schedule then places that many cutoffs
# at `step` (28d), tightening toward MIN_STEP (14d) to make them fit — so the fourth
# window was always there in 207+ days of history; nothing was ever asking for it.
# Measured, at the default 44d horizon: usable measured tier now needs ~207 days of
# history, was ~300. Both scale with the horizon, which is lead + coverage per product.
AUTO_MIN_CUTOFFS = 4

class BacktestCancelled(Exception):
    """A newer request replaced this run (the settings it was measuring changed)."""


# Per-SKU metrics computed from fewer than this many windows are too noisy to show.
MIN_WINDOWS_REPORTABLE = 4

# Training-length buckets. A cutoff at min_train=120d trains a DIFFERENT model config
# than a mature SKU (yearly seasonality off, conformal band still in-sample per the
# audit), so averaging them together conflates "the model is bad" with "the data was
# thin". Reported side by side instead.
TRAIN_BUCKETS = [(0, 180, "<180d"), (180, 365, "180-365d"), (365, 10**6, ">365d")]

# z-scores for the service-level buffer check (matches the app's protection tiers)
# Service level -> z. MUST stay in step with PROTECTION_TIERS in main.py; main._bt()
# checks them on import and refuses to run if they disagree. Keys are the literal
# tier values and their str() form is the payload key, so 99 stays an int ("99")
# while 99.5 is a float ("99.5").
Z = {90: 1.2816, 95: 1.6449, 98: 2.0537, 99: 2.3263, 99.5: 2.5758}
RESERVED = {"sku", "sku_name", "date", "ds", "units_sold", "y", "price", "on_promotion",
            "units_in_stock", "cost", "unit_cost", "price_mixed", "price_listed"}


# ─────────────────────────────────────────────────────────────────────────────
#  DATA LOADING  (file or any source that yields the same shape)
# ─────────────────────────────────────────────────────────────────────────────
def load_dataframe(path: str) -> pd.DataFrame:
    raw = pd.read_csv(path) if str(path).lower().endswith(".csv") else pd.read_excel(path)
    return normalize(raw)


def normalize(raw: pd.DataFrame) -> pd.DataFrame:
    """Lowercase columns, parse, and collapse duplicate dates — same hygiene as the
    app's upload so the backtest sees exactly what the tool would."""
    df = raw.copy()
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    if not {"date", "units_sold"}.issubset(df.columns):
        raise SystemExit("File must have 'Date' and 'Units_Sold' columns (optional: SKU, Category, Price, Units_In_Stock).")
    if "sku" not in df.columns:
        df["sku"] = "SERIES"
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["units_sold"] = pd.to_numeric(df["units_sold"], errors="coerce").fillna(0).clip(lower=0)
    # Identical rows are real sales (one row per sale), not copies: summed below. See _ingest.
    df = df.dropna(subset=["date"])
    agg = {c: ("sum" if c == "units_sold" else "last") for c in df.columns if c not in ("sku", "date")}
    df = df.groupby(["sku", "date"], as_index=False).agg(agg).sort_values(["sku", "date"]).reset_index(drop=True)
    df = df.rename(columns={"date": "ds", "units_sold": "y"})
    # Same daily-calendar fill the app's upload applies: a sheet with rows only on
    # sale days would otherwise hide every zero-sale day from the engines under test.
    # Filled to the last date in the WHOLE frame, matching what the app's ingest does.
    # Per-SKU ends would give the backtest a different series from the one being forecast
    # — in particular it would never see a dormant tail, so it would score a product the
    # app no longer believes in.
    _end = df["ds"].max() if len(df) else None
    filled = []
    for sku, g in df.groupby("sku"):
        gf, _ = fill_daily_gaps(g.drop(columns=["sku"]), end=_end)
        gf["sku"] = sku
        filled.append(gf)
    df = pd.concat(filled, ignore_index=True) if filled else df
    return df.sort_values(["sku", "ds"]).reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
#  RELATEDNESS AS OF A CUTOFF  (so pooling is leakage-safe too)
# ─────────────────────────────────────────────────────────────────────────────
def _pool_state_as_of(df: pd.DataFrame, cutoff: pd.Timestamp):
    """Catalog + grouping + clusters as known at `cutoff`, mirroring the PRODUCTION
    pipeline (_rebuild): dead/dormant exclusion → adaptive grouping → behavioural
    clustering. The old version used base group_catalog/related_frames, so backtests
    of global-routed SKUs validated a coarser pool than production actually uses."""
    attr_cols = [c for c in df.columns if c not in RESERVED]
    catalog = {}
    for s, g in df.groupby("sku"):
        gc = g[g["ds"] <= cutoff]
        if gc.empty:
            continue
        attrs = {c: (gc[c].dropna().iloc[0] if gc[c].notna().any() else None) for c in attr_cols}
        # Price facts travel with the history, as they do in the live catalog: peers lend
        # their price response and promotion lifts, which can't be read from ds/y alone.
        keep = ["ds", "y"] + [c for c in ("price", "price_mixed", "price_listed", "on_promotion")
                              if c in gc.columns]
        entry = {"df": gc[keep].reset_index(drop=True), "attrs": attrs, "sku_name": str(s)}
        if not _exclude_from_pooling(entry, as_of=cutoff):
            catalog[s] = entry
    base_cols = R.detect_group_columns(catalog)
    groups, _meta = R.adaptive_group_catalog(catalog, base_cols)
    # Same baseline discipline as production: similarity is measured with the
    # catalog-wide rhythm removed, so a replay pools exactly what live routing would.
    baseline = R.catalog_baseline(catalog)
    family = R.family_map(groups, _meta)
    clusters = R.cluster_catalog(groups, catalog, baseline=baseline, family=family)
    return catalog, groups, clusters, baseline, family


def _relatives_as_of(df: pd.DataFrame, sku: str, cutoff: pd.Timestamp, pool_cache: dict | None = None):
    key = pd.Timestamp(cutoff).normalize()
    if pool_cache is not None and key in pool_cache:
        catalog, groups, clusters, baseline, _family = pool_cache[key]
    else:
        catalog, groups, clusters, baseline, _family = _pool_state_as_of(df, cutoff)
        if pool_cache is not None:
            pool_cache[key] = (catalog, groups, clusters, baseline, _family)
    if sku not in catalog:
        return [], 0, None
    rels, n_rel, _info = R.behavioral_relatives(sku, groups, catalog, clusters, baseline=baseline)
    # Cohesion travels with the pool so the backtest routes exactly as production does.
    # Without it the replay would keep pooling across incoherent clusters that live
    # routing now declines, and the measured protection levels it produces would be
    # calibrated against a model the app no longer runs.
    coh = (R._cohesion_for_ids(_info["chosen"], catalog, baseline)
           if _info and len(_info.get("chosen") or []) >= 2 else None)
    return rels, n_rel, coh


def _peers_as_of(df: pd.DataFrame, sku: str, cutoff: pd.Timestamp, pool_cache: dict | None = None):
    """Category peers as known at the cutoff, for borrowing holiday lifts and price
    response (the same R.category_peers production uses)."""
    key = pd.Timestamp(cutoff).normalize()
    if pool_cache is None or key not in pool_cache:
        _relatives_as_of(df, sku, cutoff, pool_cache)
    if pool_cache is None or key not in pool_cache:
        return None
    catalog, groups, _clusters, _baseline, _family = pool_cache[key]
    return (R.category_peers(sku, groups, catalog, family=_family, baseline=_baseline)
            if sku in catalog else None)


def _store_peers_as_of(df, sku, cutoff, pool_cache=None):
    """Every other product as known at the cutoff: the fallback pool for holiday lifts
    when a category is too small (same R.category_peers call production makes)."""
    key = pd.Timestamp(cutoff).normalize()
    if pool_cache is None or key not in pool_cache:
        _relatives_as_of(df, sku, cutoff, pool_cache)
    if pool_cache is None or key not in pool_cache:
        return None
    catalog = pool_cache[key][0]
    return R.category_peers(sku, {}, catalog, limit=40) if sku in catalog else None


# ─────────────────────────────────────────────────────────────────────────────
#  METRICS
# ─────────────────────────────────────────────────────────────────────────────
def _scoreable_days(sku_df: pd.DataFrame) -> pd.DataFrame:
    """The days a forecast can be graded on: every day training keeps. Only a day with
    nothing to sell (UNAVAILABLE in censoring: opened empty, sold nothing, closed empty)
    is left out, the same rule the fit uses.

    This used to keep only rows with units_in_stock > 0. A blank stock cell is NaN, and
    NaN > 0 is False, so a product with no stock figures in a catalog where others had
    them lost EVERY day: it was never tested, and every sync asked to test it again. And
    a day that sold out (closing stock 0, but sold all day) was dropped from scoring while
    training kept it, so the busiest days were never graded."""
    if "units_in_stock" not in sku_df.columns:
        return sku_df
    stock = pd.to_numeric(sku_df["units_in_stock"], errors="coerce")
    if stock.isna().all():
        return sku_df
    frame = sku_df[["ds", "y"]].copy()
    frame["units_in_stock"] = stock.to_numpy()
    kept, _ = _CEN.apply_to_frame(frame.reset_index(drop=True))
    keep_days = set(pd.to_datetime(kept["ds"]))
    return sku_df[pd.to_datetime(sku_df["ds"]).isin(keep_days)]


PRICE_TOL = 0.005      # same tolerance the live-price feed uses for "a different price"


def _window_events(sku_df: pd.DataFrame, cutoff, end) -> list:
    """The price changes and promotions a test window actually had, as the events a
    person would have logged for it. Built from the recorded price and on_promotion on
    the window's own days, in the app's event shapes (promotion / price_change_*).

    Test windows used to be forecast with NO events, so any window containing a sale or
    a price change was graded as if the model should have guessed it unaided. The
    live app is always told about a planned promotion; the replay now is too. Only
    days inside the window are read, and only their price and promotion flags, never
    their sales: this is what was planned, not what happened.
    """
    if sku_df is None or not len(sku_df):
        return []
    cutoff, end = pd.Timestamp(cutoff), pd.Timestamp(end)
    has_price = "price" in sku_df.columns
    has_promo = "on_promotion" in sku_df.columns
    if not has_price and not has_promo:
        return []
    past = sku_df[sku_df["ds"] <= cutoff]
    win = sku_df[(sku_df["ds"] > cutoff) & (sku_df["ds"] <= end)].sort_values("ds")
    if win.empty:
        return []
    promo = (pd.to_numeric(win["on_promotion"], errors="coerce").fillna(0) >= 0.5).to_numpy() \
        if has_promo else np.zeros(len(win), bool)
    price = pd.to_numeric(win["price"], errors="coerce").to_numpy(float) if has_price \
        else np.full(len(win), np.nan)
    base = None
    if has_price:
        pp = past.copy()
        pp["_p"] = pd.to_numeric(pp["price"], errors="coerce")
        if "on_promotion" in pp.columns:
            reg = pp[pd.to_numeric(pp["on_promotion"], errors="coerce").fillna(0) < 0.5]["_p"].dropna()
        else:
            reg = pp["_p"].dropna()
        allp = pp["_p"].dropna()
        base = float(reg.iloc[-1]) if len(reg) else (float(allp.iloc[-1]) if len(allp) else None)
    days = [d.strftime("%Y-%m-%d") for d in pd.to_datetime(win["ds"])]
    evs = []

    # Promotions: each run of consecutive promotion days is one promotion, with the
    # discount the recorded prices show against the regular price before the window.
    i = 0
    while i < len(days):
        if not promo[i]:
            i += 1; continue
        j = i
        while j + 1 < len(days) and promo[j + 1]:
            j += 1
        ev = {"type": "promotion", "date": days[i], "end_date": days[j], "label": "Recorded promotion",
              "origin": "backtest"}
        pr = price[i:j + 1]; pr = pr[np.isfinite(pr)]
        if base and len(pr) and float(np.mean(pr)) < base * (1 - PRICE_TOL):
            ev["discount_pct"] = round(min(100.0, (1 - float(np.mean(pr)) / base) * 100), 1)
        evs.append(ev)
        i = j + 1

    # Price changes on the other days: runs at one price different from the regular one.
    # The run that lasts to the end of the window is a permanent change; any earlier one
    # was temporary.
    if base:
        i = 0
        while i < len(days):
            p_i = price[i]
            if promo[i] or not np.isfinite(p_i) or abs(p_i - base) <= base * PRICE_TOL:
                i += 1; continue
            j = i
            while (j + 1 < len(days) and not promo[j + 1] and np.isfinite(price[j + 1])
                   and abs(price[j + 1] - p_i) <= max(p_i, 0.01) * PRICE_TOL):
                j += 1
            if j == len(days) - 1:
                evs.append({"type": "price_change_permanent", "date": days[i], "new_price": round(float(p_i), 2),
                            "label": "Recorded price change", "origin": "backtest"})
            else:
                evs.append({"type": "price_change_temporary", "date": days[i], "end_date": days[j],
                            "new_price": round(float(p_i), 2), "label": "Recorded price change",
                            "origin": "backtest"})
            i = j + 1
    return sorted(evs, key=lambda e: e["date"])


def _cost_latest(sku_df: pd.DataFrame):
    """The product's most recent per-unit cost in the data ('cost'/'unit_cost'), or None.
    Used as TODAY's cost when the dashboard has none (see _run_cutoff)."""
    for c in ("cost", "unit_cost"):
        if c in sku_df.columns:
            s = pd.to_numeric(sku_df[c], errors="coerce").dropna()
            if len(s):
                return float(s.iloc[-1])
    return None


def _mase_denominator(train_y: np.ndarray, m: int = 7) -> float | None:
    """Mean absolute change of the seasonal-naive forecast on the training data —
    the standard MASE scale. MASE < 1 means we beat 'same as m days ago'."""
    if len(train_y) <= m:
        return None
    d = np.abs(train_y[m:] - train_y[:-m])
    md = float(np.mean(d))
    return md if md > 1e-9 else None


def _score_window(pred: pd.DataFrame, actual: pd.DataFrame, train_y: np.ndarray):
    """Align forecast vs actuals on date and compute per-cutoff accuracy stats.

    `train_y` must be the CONTIGUOUS daily history (gap-filled, stockout days NOT
    removed). The MASE denominator lags it positionally by 7, so a series with rows
    dropped out of the middle would silently compare against "7 rows ago" instead of
    "7 days ago" — an error that scales with how often the SKU stocked out.
    `absErrSum`/`sumY` are carried through so the aggregate can pool a volume-weighted
    WAPE rather than averaging per-window ratios."""
    m = pred.merge(actual, on="ds", how="inner")
    if m.empty:
        return None
    y = m["y"].to_numpy(float); yh = m["yhat"].clip(lower=0).to_numpy(float)
    abs_err = np.abs(y - yh)
    sumy = y.sum()
    denom = _mase_denominator(train_y)
    cov = None
    if "yhat_lower" in m and "yhat_upper" in m:
        cov = float(np.mean((y >= m["yhat_lower"].to_numpy()) & (y <= m["yhat_upper"].to_numpy())))
    return {
        "mae": float(abs_err.mean()),
        "wape": float(abs_err.sum() / sumy) if sumy > 0 else None,
        "absErrSum": float(abs_err.sum()),
        "sumY": float(sumy),
        "bias_pct": float((yh.sum() - sumy) / sumy * 100) if sumy > 0 else None,
        "mase": float(abs_err.mean() / denom) if denom else None,
        "coverage": cov,
        "n": len(m),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  ONE CUTOFF
# ─────────────────────────────────────────────────────────────────────────────
def _run_cutoff(df, sku, sku_df, cutoff, horizon, combos, service_pct,
                sku_econ=None, pool_cache=None, force_route=None):
    """Score one cutoff at EVERY (lead, coverage) pair your fleet uses.

    Lead time and coverage change only the DECISION metrics — which window the buffer is
    judged over — not the model. Fitting is ~all of the cost, so scoring extra pairs is
    nearly free. This exists because a single global pair meant a product whose lead time
    you'd changed could never be re-tested: the catalog-wide run used the most common
    values, so that product stayed permanently 'stale'. Returns one row per pair."""
    rels, n_rel, pool_coh = _relatives_as_of(df, sku, cutoff, pool_cache)
    # build_entry strips to ds <= cutoff internally → no lookahead. The window's own
    # price changes and promotions go in as planned events, as they would in the app.
    events = _window_events(sku_df, cutoff, cutoff + pd.Timedelta(days=horizon))
    entry = build_entry(sku, sku, sku_df, "uploaded", "backtest", cutoff, events, rels, n_rel,
                        pool_cohesion=pool_coh,
                        pool_baseline=(pool_cache[pd.Timestamp(cutoff).normalize()][3]
                                       if pool_cache and pd.Timestamp(cutoff).normalize() in pool_cache else None),
                        calib_windows=BT_CALIB_WINDOWS, force_route=force_route,
                        peers=_peers_as_of(df, sku, cutoff, pool_cache),
                        store_peers=_store_peers_as_of(df, sku, cutoff, pool_cache))
    fc = entry["forecast"]
    end = cutoff + pd.Timedelta(days=horizon)
    _hcols = [c for c in ("holiday", "hs_years") if c in fc.columns]
    pred = fc[(fc["ds"] > cutoff) & (fc["ds"] <= end)][["ds", "yhat", "yhat_lower", "yhat_upper"] + _hcols]
    # Score the days training keeps. Grading against days the shelf was empty all day
    # (sales 0 because there was nothing to sell) reads as over-forecasting that scales
    # with how often a product was out; grading only fully-stocked days skips the
    # sell-out days, which are the busiest. See _scoreable_days.
    scoring_df = _scoreable_days(sku_df)
    actual = scoring_df[(scoring_df["ds"] > cutoff) & (scoring_df["ds"] <= end)][["ds", "y"]]
    # MASE scale comes from the UNFILTERED (contiguous) history — see _score_window.
    train_y = sku_df[sku_df["ds"] <= cutoff]["y"].to_numpy(float)
    train_days = int(len(train_y))
    acc = _score_window(pred, actual, train_y)
    if acc is None:
        return None

    # ── Reorder-decision metrics ──
    # Compare forecast vs actual over the SAME in-stock days (mw = inner-join, which
    # drops stockout days since `actual` already excludes them). Summing the forecast
    # over ALL days while actual covers only in-stock days would make the order look
    # oversized on stockout-heavy SKUs — the same artifact, in the order metric.
    mw = pred.merge(actual, on="ds", how="inner")
    def s(col, lo, hi):
        w = mw[(mw["ds"] > lo) & (mw["ds"] <= hi)]
        return float(w[col].clip(lower=0).sum()) if len(w) else 0.0

    # Economics don't depend on the (lead, coverage) pair, so resolve them once.
    #  · Price: today's shelf price when the app sends one. The tier being chosen is for
    #    selling at today's price; a window's own price used to set its margin, so a
    #    price rise never reached the recommendation.
    #  · Cost: today's cost too: the dashboard's, else the latest in the data. Price and
    #    cost come from the same moment, so the profit per unit is one that exists: a
    #    window's old cost against today's price made a figure that was true at no time
    #    and overstated what a stockout costs. The UNITS short and carried still come
    #    from the window's real sales; only what they're worth is today's.
    econ = (sku_econ or {})
    price = float(econ.get("price") or entry.get("last_price") or 0.0)
    cost = econ.get("cost")
    if cost in (None, ""):
        cost = _cost_latest(sku_df)
    fees = float(econ.get("fees") or 0.0)
    cost_known = cost is not None
    unit_cost = float(cost) if cost_known else 0.0
    margin_unit = (price - unit_cost - fees) if cost_known else 0.0

    rows = []
    for lead, coverage in combos:
        deliver = cutoff + pd.Timedelta(days=lead)
    # Service-level test: would demand over the lead time blow past forecast + safety?
    #
    # Size the buffer to the number of days ACTUALLY SCORED, not the nominal lead. The
    # lead window only contains in-stock days (stockout days were dropped from `actual`
    # above), so a SKU that was out of stock half the window is judged on ~7 days of
    # demand. Sizing its safety stock for the full 14 (σ·√14) hands it a buffer roughly
    # 40% too large and it passes the service test for free — inflating achievedService
    # on exactly the stockout-prone SKUs where the buffer matters most.
        lt = mw[(mw["ds"] > cutoff) & (mw["ds"] <= deliver)]
        n_lt = int(len(lt))
        buffer_testable = n_lt >= max(3, lead // 2)
        if buffer_testable:
            f_lt = float(lt["yhat"].clip(lower=0).sum())
            a_lt = float(lt["y"].clip(lower=0).sum())
            _base = float(entry.get("residual_std", 0.0)) * np.sqrt(n_lt)
            _lw = lead_window_sigma(entry.get("oos_residuals"), n_lt)  # measured, clumping-aware
            _basis = _base if _lw is None else min(max(_lw, _base), 3 * _base)  # widen-only, never below sqrt(n)
            _z = Z.get(service_pct, 1.6449)
            safety = _z * _basis
            covered = bool(a_lt <= (f_lt + safety))
            short = max(0.0, a_lt - (f_lt + safety))
        else:
            # Too few in-stock days to judge the buffer honestly. Keep the row so accuracy
            # still counts; NaN the decision metrics so they're excluded, not faked.
            f_lt = a_lt = _basis = 0.0
            covered, short = np.nan, np.nan
        # Which reshaped holidays the lead window carries, and their forecast units in it
        # (in-stock days only, like f_lt): what holiday_cover calibrates its rates on and
        # replays the window with. {"key|anchor": [units, own years behind its shape]}.
        hol_expo = {}
        if buffer_testable and "holiday" in lt.columns:
            for _k, _g in lt[lt["holiday"].notna()].groupby("holiday"):
                _yrs = pd.to_numeric(_g["hs_years"], errors="coerce").dropna() if "hs_years" in _g.columns else []
                hol_expo[str(_k)] = [round(float(_g["yhat"].clip(lower=0).sum()), 3),
                                     int(_yrs.iloc[0]) if len(_yrs) else 0]

        # Order-quantity test: forecast vs actual demand over the post-delivery coverage window.
        f_cov = s("yhat", deliver, deliver + pd.Timedelta(days=coverage))
        a_cov = s("y", deliver, deliver + pd.Timedelta(days=coverage))
        order_err_pct = ((f_cov - a_cov) / a_cov * 100) if a_cov > 0 else None

        row = {"route": entry.get("route"), **acc, "covered": covered, "shortUnits": short,
               # Named to avoid colliding with the interval-coverage METRIC, which this
               # once overwrote — every band-hit figure in the app read 3000%.
               "leadDays": int(lead), "coverageDays": int(coverage),
               "orderErrPct": order_err_pct, "price": round(price, 2),
               "unitCost": round(unit_cost, 4), "marginUnit": round(margin_unit, 4),
               "costKnown": bool(cost_known),
               "fees": round(fees, 4),
               "trainDays": train_days, "leadDaysScored": n_lt,
               "bufferTestable": bool(buffer_testable),
               # The window's forecast, actual and ordinary buffer σ, kept so the holiday
               # cover can be replayed on these rows without refitting (holiday_cover.apply).
               "fcLead": round(f_lt, 4) if buffer_testable else np.nan,
               "actLead": round(a_lt, 4) if buffer_testable else np.nan,
               "basisLead": round(float(_basis), 6) if buffer_testable else np.nan,
               "holExpo": json.dumps(hol_expo) if hol_expo else "",
               # model coefficients behind the buffer (averaged across cutoffs for display)
               "sigma": round(float(entry.get("residual_std", 0.0)), 2),
               "cv": round(float(entry.get("residual_cv", 0.0)) * 100, 1)}
        for pct, z in Z.items():
            if not buffer_testable:
                row[f"cov_{pct}"] = row[f"lost_{pct}"] = row[f"safety_{pct}"] = np.nan
                continue
            saf = z * _basis
            row[f"cov_{pct}"]    = float(a_lt <= (f_lt + saf))     # float, so NaN can coexist
            row[f"lost_{pct}"]   = max(0.0, a_lt - (f_lt + saf))   # units short over the lead window
            row[f"safety_{pct}"] = saf                             # safety units carried
        rows.append(row)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
#  FULL BACKTEST
# ─────────────────────────────────────────────────────────────────────────────
def _cutoff_schedule(first_allowed, last_possible, n_cutoffs, step):
    """Cutoffs for ONE SKU, spread across its own testable span.

    Half stay dense-and-recent at `step` (the regime you're operating in now); the rest
    spread evenly back over the remaining history so a yearly-seasonality model gets
    tested on more than the last eight months. A SKU with three years of data used to
    leave 800+ testable days untouched. Returns [(cutoff, block)] newest-first, where
    block is "recent" or "historic" so the two are reported separately rather than
    blended — a business that changed pricing last year should see that split.
    """
    testable = (last_possible - first_allowed).days
    if testable < 0:
        return []

    def build(recent_step):
        n_recent = max(1, int(round(n_cutoffs * RECENT_FRACTION)))
        out, seen = [], set()

        def add(ts, block):
            key = pd.Timestamp(ts).normalize()
            if key not in seen and first_allowed <= key <= last_possible:
                seen.add(key); out.append((key, block))

        c = last_possible
        while len(out) < n_recent and c >= first_allowed:
            add(c, "recent"); c -= pd.Timedelta(days=recent_step)

        remaining = n_cutoffs - len(out)
        if remaining > 0 and out:
            oldest_recent = min(t for t, _ in out)
            back_span = (oldest_recent - first_allowed).days
            if back_span >= MIN_STEP:
                wide = max(MIN_STEP, back_span // remaining)
                c = oldest_recent - pd.Timedelta(days=wide)
                while len(out) < n_cutoffs and c >= first_allowed:
                    add(c, "historic"); c -= pd.Timedelta(days=wide)
        return out

    # Honour the request. At a fixed recent step the recent block can eat the whole
    # testable span and leave no room for the historic half, so asking for 40 at step=28
    # used to return 21 with no explanation. Tighten the recent step toward MIN_STEP
    # until the request fits; if even MIN_STEP can't supply it, the data genuinely can't
    # and cutoff_capacity reports that ceiling up front.
    eff = max(int(step), MIN_STEP)
    out = build(eff)
    if len(out) < n_cutoffs and eff > MIN_STEP:
        for cand in range(eff - 1, MIN_STEP - 1, -1):
            trial = build(cand)
            if len(trial) > len(out):
                out = trial
            if len(out) >= n_cutoffs:
                break
    return sorted(out, key=lambda t: t[0], reverse=True)


def _auto_cutoffs(first_allowed, last_possible, horizon) -> int:
    """How many windows this SKU's span genuinely supports — one per horizon length, so
    consecutive windows don't re-measure the same days."""
    testable = (last_possible - first_allowed).days
    if testable < 0:
        return 0
    return max(AUTO_MIN_CUTOFFS, int(testable // max(int(horizon), 1)) + 1)


def _resolve_cutoffs(n_cutoffs, first_allowed, last_possible, horizon) -> int:
    """`auto` sizes per SKU; a number is honoured as-is (still capped by the schedule)."""
    if n_cutoffs == AUTO_CUTOFFS or n_cutoffs is None:
        return _auto_cutoffs(first_allowed, last_possible, horizon)
    try:
        return int(n_cutoffs)
    except (TypeError, ValueError):
        return _auto_cutoffs(first_allowed, last_possible, horizon)


def _normalise_combos(combos, lead, coverage):
    """Distinct (lead, coverage) pairs to score, always including the primary one."""
    out, seen = [], set()
    for L, C in [(lead, coverage)] + list(combos or []):
        key = (int(L), int(C))
        if key not in seen:
            seen.add(key); out.append(key)
    return out


def run_backtest(df, horizon=44, n_cutoffs=AUTO_CUTOFFS, step=28, lead=14, coverage=30,
                 service_pct=95, min_train=120, verbose=True, skipped=None,
                 sku_costs=None, force_route=None, combos=None, only_skus=None, should_stop=None):
    """`only_skus` refits just those products. Relatedness still reads the WHOLE catalog
    (a product's peers don't change because you edited its lead time), so a partial run
    produces exactly the rows a full run would for those products — at a fraction of the
    cost when one item's settings change."""
    combos = _normalise_combos(combos, lead, coverage)
    horizon = effective_horizon(horizon, combos)
    want = {str(s) for s in only_skus} if only_skus else None
    rows = []
    pool_cache = {}   # cutoff → (catalog, groups, clusters); shared across SKUs
    for sku, g in df.groupby("sku"):
        # Checked between products: a run whose settings were changed under it stops
        # within one product's fits, so the new one starts now instead of queuing.
        if should_stop is not None and should_stop():
            raise BacktestCancelled()
        if want is not None and str(sku) not in want:
            continue
        g = g.sort_values("ds").reset_index(drop=True)
        span = (g["ds"].max() - g["ds"].min()).days
        if span < min_train + horizon:
            if skipped is not None:
                skipped.append({"sku": str(sku), "kind": "short", "days": int(span),
                                "reason": f"only {span}d of history (need ≥{min_train+horizon}d)"})
            if verbose:
                print(f"  · {sku}: only {span}d of history — too short to backtest (need ≥{min_train+horizon}d), skipped.")
            continue
        last_possible = (g["ds"].max() - pd.Timedelta(days=horizon)).normalize()
        first_allowed = (g["ds"].min() + pd.Timedelta(days=min_train)).normalize()
        # Per SKU, not one number for the catalog: a 900-day product supports far more
        # windows than a 200-day one, and forcing a shared count wastes the long histories.
        n_this = _resolve_cutoffs(n_cutoffs, first_allowed, last_possible, horizon)
        for cutoff, block in _cutoff_schedule(first_allowed, last_possible, n_this, step):
            try:
                r = _run_cutoff(df, sku, g, cutoff, horizon, combos, service_pct,
                                (sku_costs or {}).get(str(sku)), pool_cache, force_route)
            except Exception as ex:
                # Never swallow. A cutoff that blows up is a finding, not a non-event —
                # the old `r = None` let a third of the run vanish while the UI still
                # reported "tested N SKUs".
                if skipped is not None:
                    skipped.append({"sku": str(sku), "kind": "error", "cutoff": str(cutoff.date()),
                                    "reason": f"{type(ex).__name__}: {ex}"})
                if verbose:
                    print(f"  · {sku} @ {cutoff.date()}: {type(ex).__name__}: {ex}")
                r = None
            for one in (r or []):
                rows.append({"sku": sku, "cutoff": cutoff.date(), "block": block, **one})
    return pd.DataFrame(rows)


def _nanmean(series) -> float:
    """Mean ignoring NaN, returning 0.0 for an all-NaN column. Buffer metrics are NaN
    when a window had too few in-stock days to judge (see _run_cutoff), so every
    downstream average has to tolerate them rather than propagate NaN into the money."""
    v = pd.to_numeric(series, errors="coerce").dropna()
    return float(v.mean()) if len(v) else 0.0


def _boot_ci(frame, col, n_boot=1000, seed=0, scale=1.0):
    """95% CI by resampling SKUs (a CLUSTER bootstrap), not rows.

    Two things make the raw window count overstate how much you actually know:
    evaluation windows overlap (horizon 44 > step 28, so consecutive windows share 16
    days), and windows from one SKU are correlated with each other regardless. Both are
    WITHIN-SKU dependence, so resampling whole SKUs — keeping each one's block of
    windows intact — carries the correlation into the interval width for free, with no
    need to model it. 8 windows over a 240d span carry roughly 5.5 windows' worth of
    independent information; this is what stops a 7-point service gap from reading as a
    finding when it's inside the noise.

    Caveat worth remembering when reading the output: SKUs from one catalog share
    seasonality and pooled engines, so they aren't fully independent either and this
    still runs slightly optimistic."""
    if frame is None or frame.empty or col not in frame.columns:
        return None
    skus = frame["sku"].unique()
    if len(skus) < 2:
        return None
    groups = {s: frame.loc[frame["sku"] == s, col].astype(float).dropna().to_numpy() for s in skus}
    groups = {s: v for s, v in groups.items() if len(v)}
    if len(groups) < 2:
        return None
    keys = np.array(list(groups.keys()), dtype=object)
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(n_boot):
        pick = rng.choice(len(keys), len(keys), replace=True)
        v = np.concatenate([groups[keys[i]] for i in pick])
        if len(v):
            stats.append(float(v.mean()))
    if not stats:
        return None
    return [round(float(np.percentile(stats, 2.5)) * scale, 2),
            round(float(np.percentile(stats, 97.5)) * scale, 2)]


def _agg(frame, ci=False):
    def m(col):
        if col not in frame.columns:
            return None
        v = frame[col].astype(float).dropna()
        return round(float(v.mean()), 3) if len(v) else None
    # Volume-weighted WAPE: pool the errors and the units, don't average the ratios.
    # An unweighted mean lets a 5-unit window count as much as a 5,000-unit one. The
    # unweighted figure is kept alongside — the two diverging is itself the signal.
    sum_err = float(frame["absErrSum"].sum()) if "absErrSum" in frame.columns else 0.0
    sum_y = float(frame["sumY"].sum()) if "sumY" in frame.columns else 0.0
    cov_col = frame["covered"].astype(float).dropna() if "covered" in frame.columns else pd.Series(dtype=float)
    out = {
        "forecasts": len(frame),
        "skus": int(frame["sku"].nunique()) if "sku" in frame.columns else None,
        "WAPE%": round(sum_err / sum_y * 100, 1) if sum_y > 0 else None,       # volume-weighted
        "WAPE%_unweighted": (round(m("wape") * 100, 1) if m("wape") is not None else None),
        "MASE": m("mase"),
        "bias%": m("bias_pct"),
        "interval_cov%": (round(m("coverage") * 100, 1) if m("coverage") is not None else None),
        "service_achieved%": round(float(cov_col.mean()) * 100, 1) if len(cov_col) else None,
        "service_windows": int(len(cov_col)),   # buffer-testable windows only
        "order_err%": m("orderErrPct"),
        "sigma": m("sigma"),     # σ — daily demand swing (units/day) used in the buffer
        "cv%": m("cv"),          # demand noise (coefficient of variation)
    }
    if ci:
        out["service_achieved%_ci"] = _boot_ci(frame, "covered", scale=100.0)
        out["MASE_ci"] = _boot_ci(frame, "mase")
        out["interval_cov%_ci"] = _boot_ci(frame, "coverage", scale=100.0)
    return out


def _grouped(frame, by):
    rows = []
    for k, g in frame.groupby(by):
        a = _agg(g)
        a["windows"] = int(len(g))
        if by == "sku":
            # Per-SKU rows off 1-2 windows are noise wearing a number's clothes. Flag
            # them so the report can grey them out instead of presenting them with the
            # same authority as an 8-window SKU.
            a["reportable"] = bool(len(g) >= MIN_WINDOWS_REPORTABLE)
        else:
            # An engine row needs enough windows AND more than one product behind it —
            # "croston scores 50%" off a single SKU's 4 windows says nothing about the
            # engine, only about that one product.
            a["reportable"] = bool(len(g) >= MIN_WINDOWS_REPORTABLE and g["sku"].nunique() >= 2)
        rows.append({by if by != "route" else "engine": k, **a})
    return pd.DataFrame(rows)


def _by_train_bucket(frame):
    """Accuracy split by how much history the model had at the cutoff. A cutoff at
    min_train=120d trains a materially different configuration (yearly seasonality off,
    conformal band still in-sample) than a mature SKU, so pooling them conflates a weak
    model with thin data. Split out, this answers the question users actually have:
    how much history before I can trust this?"""
    if frame is None or frame.empty or "trainDays" not in frame.columns:
        return []
    out = []
    for lo, hi, label in TRAIN_BUCKETS:
        sub = frame[(frame["trainDays"] >= lo) & (frame["trainDays"] < hi)]
        if not sub.empty:
            out.append({"bucket": label, **_agg(sub)})
    return out


def _by_source(frame, sku_sources):
    """Accuracy grouped by the file each product came from."""
    if frame is None or frame.empty or not sku_sources:
        return []
    f = frame.copy()
    f["source"] = f["sku"].astype(str).map(lambda s: sku_sources.get(s) or "(unknown file)")
    return [{"source": src, "products": int(g["sku"].nunique()), **_agg(g)}
            for src, g in f.groupby("source")]


def _by_block(frame):
    """Recent vs historic cutoffs, kept apart on purpose: historic windows test a
    business that may no longer exist (old pricing, old channel mix)."""
    if frame is None or frame.empty or "block" not in frame.columns:
        return []
    return [{"block": b, **_agg(g)} for b, g in frame.groupby("block")]


def cutoff_capacity(df, horizon=44, min_train=120, n_cutoffs=AUTO_CUTOFFS, step=28):
    """How many cutoffs each SKU can actually supply — the answer to 'what should I set
    cutoffs to?'

    Raising `cutoffs` does NOT exclude anyone: the skip rule is span < min_train +
    horizon, independent of the count. What a too-high count does is let short-history
    SKUs quietly return fewer windows than asked for, which then reads as a per-SKU
    result when it's really a per-SKU sample-size problem. This makes that visible up
    front instead of after the run.

    The ceiling is derived by RUNNING the real scheduler with an unlimited request, not
    by a formula — the two drifted apart otherwise, advertising 40 where step=28 could
    only deliver 21. The scheduler now tightens its own spacing to honour the request, so
    this single number is genuinely what you can ask for and receive.
    """
    BIG = 10_000
    out, binding = [], None
    for sku, g in df.groupby("sku"):
        span = int((g["ds"].max() - g["ds"].min()).days)
        testable = span - horizon - min_train
        if testable < 0:
            out.append({"sku": str(sku), "days": span, "maxCutoffs": 0, "cutoffsUsed": 0,
                        "testable": False,
                        "reason": f"needs ≥{min_train + horizon}d to be testable at all"})
            continue
        first_allowed = (g["ds"].min() + pd.Timedelta(days=min_train)).normalize()
        last_possible = (g["ds"].max() - pd.Timedelta(days=horizon)).normalize()
        mx = len(_cutoff_schedule(first_allowed, last_possible, BIG, step))
        auto = _resolve_cutoffs(n_cutoffs, first_allowed, last_possible, horizon)
        used = len(_cutoff_schedule(first_allowed, last_possible, auto, step))
        out.append({"sku": str(sku), "days": span, "maxCutoffs": mx, "cutoffsUsed": used,
                    "testable": True, "reason": None})
    testable_rows = [r for r in out if r["testable"]]
    if testable_rows:
        binding = min(testable_rows, key=lambda r: r["maxCutoffs"])
    auto_mode = (n_cutoffs == AUTO_CUTOFFS or n_cutoffs is None)
    capped = [] if auto_mode else [r for r in testable_rows if r["maxCutoffs"] < int(n_cutoffs)]
    thin = [r for r in testable_rows if r["cutoffsUsed"] < MIN_WINDOWS_REPORTABLE]
    # Span needed for every tested SKU to clear the reportable bar.
    need_days = min_train + horizon + (MIN_WINDOWS_REPORTABLE - 1) * MIN_STEP
    return {
        "auto": bool(auto_mode),
        "requested": (None if auto_mode else int(n_cutoffs)),
        "windowsUsed": sum(r["cutoffsUsed"] for r in testable_rows),
        "usedRange": ([min(r["cutoffsUsed"] for r in testable_rows),
                       max(r["cutoffsUsed"] for r in testable_rows)] if testable_rows else [0, 0]),
        "minStep": MIN_STEP, "step": int(step), "horizon": int(horizon),
        "catalogMax": (binding["maxCutoffs"] if binding else 0),
        "bindingSku": (binding["sku"] if binding else None),
        "bindingDays": (binding["days"] if binding else None),
        "testableSkus": len(testable_rows),
        "untestableSkus": len(out) - len(testable_rows),
        "cappedSkus": [r["sku"] for r in capped],          # will silently return fewer
        "thinSkus": [r["sku"] for r in thin],              # will land under the reportable bar
        "reportableNeedsDays": int(need_days),
        "bySku": sorted(out, key=lambda r: r["maxCutoffs"]),
    }


def _bootstrap_tier_ranking(costed, tiers, cycles_per_year, holding_annual, n_boot=2000, seed=0):
    """P(each tier is the cheapest), by resampling SKUs.

    Builds each SKU's per-tier annual cost once, then resamples SKUs with replacement
    and re-runs the argmin. If the top two tiers come out near 50/50, the honest report
    is 'too close to call' — not a recommendation. Returns None when there aren't enough
    costed SKUs for the question to mean anything."""
    if costed is None or costed.empty or not tiers:
        return None
    pcts = [t["tier"] for t in tiers]
    per_sku = {}
    for sku, g in costed.groupby("sku"):
        margin = float(g["marginUnit"].mean())
        unit_cost = float(g["unitCost"].mean())
        row = {}
        for pct in pcts:
            if f"lost_{pct}" not in g.columns or f"safety_{pct}" not in g.columns:
                continue
            row[pct] = (_nanmean(g[f"lost_{pct}"]) * cycles_per_year * margin      # lost profit/yr
                        + _nanmean(g[f"safety_{pct}"]) * unit_cost * holding_annual   # holding/yr
                        + _HCV.cost_per_year(g, Z[pct], unit_cost, holding_annual,
                                             round(365.0 / cycles_per_year)))           # holiday cover/yr
        if row:
            per_sku[str(sku)] = row
    keys = list(per_sku)
    if len(keys) < 2:
        return None
    # Only rank tiers every SKU actually has a measurement for. The resample below used
    # `.get(pct, 0.0)`, so a tier with no data contributed a cost of ZERO — which made it
    # the cheapest in every single resample and came back as "99.5%, 100% confidence,
    # decisive". That is not a hypothetical: `Z` gained the 99.5 tier, and a backtest
    # stored before that change restores against the catalog fingerprint alone (nothing
    # checks the tier set), so the new tier arrives with no columns behind it. The
    # recommendation it produced was the MOST expensive protection level, argued for on a
    # fabricated cost of nothing. A tier we did not measure is unknown, not free.
    pcts = [p for p in pcts if all(p in row for row in per_sku.values())]
    if len(pcts) < 2:
        return None
    rng = np.random.default_rng(seed)
    wins = {p: 0 for p in pcts}
    margins_over_runner_up = []
    for _ in range(n_boot):
        pick = rng.choice(len(keys), len(keys), replace=True)
        tot = {p: sum(per_sku[keys[i]][p] for i in pick) for p in pcts}
        order = sorted(tot, key=lambda p: tot[p])
        wins[order[0]] += 1
        if len(order) > 1 and tot[order[1]] > 0:
            margins_over_runner_up.append((tot[order[1]] - tot[order[0]]) / tot[order[1]] * 100)
    probs = {p: round(wins[p] / n_boot * 100, 1) for p in pcts}
    ranked = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
    top, second = ranked[0], (ranked[1] if len(ranked) > 1 else (None, 0.0))
    return {
        "pCheapest": probs,
        "winner": top[0], "winnerProb": top[1],
        "runnerUp": second[0], "runnerUpProb": second[1],
        # A winner under ~2:1 over the runner-up isn't a decision, it's a preference.
        "decisive": bool(top[1] >= 2 * second[1] and top[1] >= 50),
        "medianMarginPct": (round(float(np.median(margins_over_runner_up)), 1)
                            if margins_over_runner_up else None),
        "resamples": n_boot,
    }


def _mixed_policy(costed, tiers, cycles_per_year, holding_annual):
    """What per-product protection levels would actually cost — measured honestly.

    The tier columns each assume ONE level for the whole catalog, but the app
    recommends a level per product, so none of those columns is the policy you'd deploy.
    This adds that fifth policy.

    The trap: picking each product's best level on the same windows you then score it on
    is the winner's curse. With a handful of windows per product the argmin is partly
    noise, so the mixed policy looks better than it would ever perform — and it's
    guaranteed to "beat" every uniform column by construction, which makes the comparison
    worthless. So the headline number is LEAVE-ONE-WINDOW-OUT: for each window, the level
    is chosen using that product's OTHER windows only, then scored on the held-out one.
    That's an out-of-sample estimate of the same policy.

    `inSampleTotal` is reported alongside precisely so the gap is visible — it's the size
    of the hindsight bonus, and a large one means the per-product picks are mostly noise.
    """
    if costed is None or costed.empty or not tiers:
        return None
    pcts = [t["tier"] for t in tiers]
    per_sku, oos_total, in_total, n_loo = {}, 0.0, 0.0, 0
    cov_samples = []      # stayed-in-stock outcomes of the held-out picks, across products
    for sku, g in costed.groupby("sku"):
        margin = float(g["marginUnit"].mean())
        unit_cost = float(g["unitCost"].mean())
        # cost of each tier in each window, for this product
        cols = {}
        for p in pcts:
            if f"lost_{p}" not in g.columns or f"safety_{p}" not in g.columns:
                continue
            lost = pd.to_numeric(g[f"lost_{p}"], errors="coerce").to_numpy(float)
            saf = pd.to_numeric(g[f"safety_{p}"], errors="coerce").to_numpy(float)
            # Holiday cover's yearly carrying is a per-product constant at a tier; spread
            # over the windows it keeps their mean equal to the yearly total, as above.
            hol = _HCV.cost_per_year(g, Z[p], unit_cost, holding_annual, round(365.0 / cycles_per_year))
            cols[p] = lost * cycles_per_year * margin + saf * unit_cost * holding_annual + hol
        if not cols:
            continue
        usable = ~np.isnan(np.vstack([cols[p] for p in cols])).any(axis=0)
        if usable.sum() == 0:
            continue
        mat = {p: cols[p][usable] for p in cols}
        n = int(usable.sum())
        means = {p: float(np.mean(mat[p])) for p in mat}
        best_p = min(means, key=lambda p: means[p])
        in_total += means[best_p]
        per_sku[str(sku)] = {"tier": best_p, "inSample": round(means[best_p], 2), "windows": n}
        cov_cols = {p: pd.to_numeric(g[f"cov_{p}"], errors="coerce").to_numpy(float)[usable]
                    for p in mat if f"cov_{p}" in g.columns}
        if n >= 2:
            # leave-one-window-out: choose on the rest, pay on the one held out
            held, held_cov = [], []
            for w in range(n):
                others = {p: float((np.sum(mat[p]) - mat[p][w]) / (n - 1)) for p in mat}
                pick = min(others, key=lambda p: others[p])
                held.append(mat[pick][w])
                if pick in cov_cols and not np.isnan(cov_cols[pick][w]):
                    held_cov.append(float(cov_cols[pick][w]))
            oos = float(np.mean(held))
            if held_cov:
                cov_samples.extend(held_cov)
            per_sku[str(sku)]["outOfSample"] = round(oos, 2)
            oos_total += oos
            n_loo += 1
        else:
            oos_total += means[best_p]      # can't hold out a single window
            per_sku[str(sku)]["outOfSample"] = None
    if not per_sku:
        return None
    return {
        "totalCost": round(oos_total, 2),            # the honest, out-of-sample figure
        # Stayed-in-stock under the mix, from the same held-out picks — so the client
        # view can show the mix on equal footing with the uniform rows.
        "achievedService": (round(float(np.mean(cov_samples)) * 100, 1) if cov_samples else None),
        "inSampleTotal": round(in_total, 2),         # what hindsight would have claimed
        # Positive = hindsight flatters the mix by this much. In-sample picks the winner
        # knowing the answer, so it always looks at least as cheap as reality.
        "hindsightBonus": round(oos_total - in_total, 2),
        "skusTuned": len(per_sku), "skusWithHoldout": n_loo,
        "bySku": per_sku,
        "tierCounts": {str(p): sum(1 for v in per_sku.values() if v["tier"] == p) for p in pcts},
    }


def _critical_ratio(frame, coverage=30, holding_annual=0.25):
    """The service level that MINIMISES cost under this tool's own economics — a
    continuous answer instead of ranking a handful of fixed guesses.

    One more unit of safety stock is worth carrying while the expected margin it saves
    exceeds what it costs to hold. At the margin:

        (1 − s) × cycles/yr × margin_per_unit  =  unit_cost × holding_rate
        ⇒  s* = 1 − (unit_cost × holding_rate) / (cycles/yr × margin_per_unit)

    This is the newsvendor critical ratio in the units this tool already tracks. It
    matters because the tiers are TARGETS and the buffer under-delivers, so ranking
    targets answers the wrong question — what you want is the target whose ACHIEVED
    service lands nearest s*. Returns per-SKU optima plus the catalog aggregate;
    loss-making and cost-unknown SKUs are excluded, as everywhere else."""
    if frame is None or frame.empty:
        return None
    cycles = 365.0 / max(int(coverage), 1)
    per_sku = {}
    for sku, g in frame.groupby("sku"):
        if not bool(g["costKnown"].any()):
            continue
        margin = float(g["marginUnit"].mean())
        unit_cost = float(g["unitCost"].mean())
        if margin <= 0 or unit_cost <= 0:
            continue
        s = 1.0 - (unit_cost * holding_annual) / (cycles * margin)
        # A SKU whose margin barely clears its holding cost has a genuinely low optimum;
        # clamp only to keep it a probability.
        per_sku[str(sku)] = round(max(0.0, min(0.9999, s)) * 100, 1)
    if not per_sku:
        return None
    vals = sorted(per_sku.values())
    return {
        "optimalService": round(float(np.mean(vals)), 1),
        "range": [vals[0], vals[-1]],
        "bySku": per_sku,
        "note": ("Service level where one more unit of safety stock stops paying for itself, "
                 "from your own margins and holding rate."),
    }


def _tier_summary(frame, coverage=30, holding_annual=0.25):
    """Compare every protection tier on calibration AND annualised money — using REAL
    per-SKU economics, no blanket margin guess. SKUs with a known unit cost (entered in
    the Scorecard or via a Cost column) are costed; SKUs without one are EXCLUDED from
    the dollar totals (they still count toward calibration).

    Per costed SKU we average, across its cutoffs, the safety stock carried and the
    shortfall per lead-window exposure, then annualise into the two costs the tier
    actually controls (the SAFETY-STOCK policy — not total inventory):
      • holding $/yr  = avg safety units × unit cost × holding rate
                        (safety stock is carried continuously → a full-year cost)
      • stockout $/yr = avg shortfall units × (reorder cycles/yr = 365/coverage)
                        × profit per unit (price − cost − fees, the margin a lost sale forfeits)
    achievedService% = share of windows the tier covered (should land ≈ the tier's target;
    well below = the buffer is under-sized there). Lowest total $/yr = the right tier.
    Returns {} when empty."""
    if frame is None or frame.empty:
        return {}
    # Holiday weeks are replayed with the holiday cover the app carries (holiday_cover.py):
    # cov_/lost_ below already include it, and its carrying cost is priced on its own.
    frame, hc_fit = _HCV.apply(frame, Z)
    cycles_per_year = 365.0 / max(int(coverage), 1)
    total_skus = int(frame["sku"].nunique())

    # Classify each cost-known SKU by margin. A product priced at or below cost
    # (margin ≤ 0) is a PRICING problem, not a buffering one — missing its sales
    # forgoes no profit, and holding more of it just loses money faster — so it's
    # pulled out of the cost comparison entirely and flagged for review.
    margins = {str(sku): float(g["marginUnit"].mean())
               for sku, g in frame.groupby("sku") if bool(g["costKnown"].any())}
    profit_skus  = {s for s, m in margins.items() if m > 0}
    loss_making  = sorted(s for s, m in margins.items() if m <= 0)
    # Windows whose buffer could actually be judged (enough in-stock days over the lead
    # time). A product with NONE was being scored as zero shortfall AND zero buffer at
    # every tier (the mean of nothing came back as 0), so its cheapest tier was always
    # the lowest one, reported as "measured". It has no measurement: it's left out of the
    # money and gets no best tier.
    testable = _testable_windows(frame)
    untestable = sorted(s for s, n in testable.items() if n == 0)
    costed_ids = {s for s in profit_skus if testable.get(s, 0) > 0}
    costed = frame[frame["sku"].astype(str).isin(costed_ids)]
    costed_skus = len(costed_ids)
    priced = costed_skus > 0
    # "partial" must mean COSTS ARE MISSING — nothing else. It used to key off
    # profit-positive products, so a catalog where every cost was present but one item
    # sold below cost reported "partial" with no missing product to point at. Two
    # different facts (a data gap vs a pricing problem) were sharing one label.
    n_cost_known = int(frame.groupby("sku")["costKnown"].any().sum())
    cost_basis = ("actual" if n_cost_known >= total_skus else "partial") if priced else "none"

    out = []
    for pct in sorted(Z.keys()):
        cov_c, lost_c, saf_c = f"cov_{pct}", f"lost_{pct}", f"safety_{pct}"
        # All three, not just cov_. The other four call sites in this file already check
        # lost_/safety_ together; this one checked cov_ and then indexed the other two,
        # which is what crashed the test suite outright once Z gained a tier.
        if cov_c not in frame.columns or lost_c not in frame.columns or saf_c not in frame.columns:
            continue
        # Calibration, averaged PER SKU FIRST then across SKUs. Pooling raw windows let a
        # long-history SKU (8 cutoffs) outvote a short one (1 cutoff) eight to one — and
        # adaptive cutoff spacing makes those counts differ more, not less.
        per_sku_cov = frame.groupby("sku")[cov_c].mean().dropna()   # (mean skips NaN windows)
        achieved = round(float(per_sku_cov.mean()) * 100, 1) if len(per_sku_cov) else None
        # Avg units short per reorder cycle, totalled across ALL tested SKUs (no cost needed) —
        # the plain-units quantity the lost-profit dollars are built from.
        units_short = float(frame.groupby("sku")[lost_c].mean().dropna().sum())
        # Buffer units carried, across ALL tested SKUs. Units don't need a cost to be
        # meaningful, and they're what you actually order — the dollars are just
        # units × a rate, so showing only the money hid half the arithmetic.
        safety_units = float(frame.groupby("sku")[saf_c].mean().dropna().sum())
        stockout = holding = buffer_cash = holiday = 0.0
        for _sku, g in costed.groupby("sku"):       # profit-positive, cost-known SKUs only
            margin_per_unit = float(g["marginUnit"].mean())
            unit_cost  = float(g["unitCost"].mean())
            avg_lost   = _nanmean(g[lost_c])         # units short per lead-window exposure
            avg_safety = _nanmean(g[saf_c])          # safety units carried
            stockout += avg_lost * cycles_per_year * margin_per_unit
            holding  += avg_safety * unit_cost * holding_annual
            buffer_cash += avg_safety * unit_cost    # standing capital parked in the buffer (one-time)
            # Holiday cover: a temporary top-up, carried one order cycle per holiday.
            holiday  += _HCV.cost_per_year(g, Z[pct], unit_cost, holding_annual, coverage)
        out.append({
            "tier": pct, "achievedService": achieved,
            # CI on the calibration figure. A "gap" smaller than this interval is noise,
            # not evidence the buffer is mis-sized — and bestTier below is chosen on
            # point estimates, so read it with the interval in hand.
            "achievedServiceCI": _boot_ci(frame, cov_c, scale=100.0),
            "gap": round(achieved - pct, 1) if achieved is not None else None,
            "unitsShort": round(units_short, 1),                       # avg units short per cycle (all SKUs)
            "unitsShortYr": round(units_short * cycles_per_year, 0),    # annualised, matches the $ cadence
            "safetyUnits": round(safety_units, 0),                      # buffer units carried (all SKUs)
            "stockoutCost": round(stockout, 2) if priced else None,   # lost PROFIT/yr (costed SKUs)
            "holdingCost": round(holding, 2) if priced else None,     # = "Buffer cost / yr" in the UI
            "holidayCost": round(holiday, 2) if priced else None,     # holiday cover carrying / yr
            "totalCost": round(stockout + holding + holiday, 2) if priced else None,
            "bufferCash": round(buffer_cash, 2) if priced else None,  # working capital tied up (a LEVEL, not /yr)
            "windows": int(frame[cov_c].notna().sum()),   # buffer-testable windows only
        })
    best = (min((t for t in out if t["totalCost"] is not None),
                key=lambda r: r["totalCost"], default=None) or {}).get("tier") if priced else None
    # Δ working capital vs the recommended tier (best = baseline 0; lower tiers free cash, higher lock it up).
    best_cash = next((t["bufferCash"] for t in out if t["tier"] == best), None)
    for t in out:
        t["bufferCashDelta"] = (round(t["bufferCash"] - best_cash, 2)
                                if (best_cash is not None and t["bufferCash"] is not None) else None)

    # ── Per-SKU × per-tier breakdown (full traceability) ──────────────────────
    # Each product's own missed units/yr and lost profit/yr at every tier, so the
    # aggregate isn't a black box. Summing profitYr across products at a tier returns
    # that tier's catalog total (= the aggregate stockoutCost). Keyed by str(tier).
    by_sku = []
    for sku, g in frame.groupby("sku"):
        ck = bool(g["costKnown"].any())
        margin = float(g["marginUnit"].mean()) if ck else None
        loss = bool(ck and margin is not None and margin <= 0)   # priced at/below cost
        n_test = testable.get(str(sku), 0)
        profitable = bool(ck and not loss and n_test > 0)
        uc = float(g["unitCost"].mean()) if ck else None
        st = {}
        for pct in sorted(Z.keys()):
            # Guard on presence: a frame from an older run (or a partial one) may not
            # carry every tier, and a missing column shouldn't take down the whole report.
            if f"lost_{pct}" not in g.columns or f"safety_{pct}" not in g.columns:
                continue
            if n_test == 0:
                st[str(pct)] = {"unitsYr": None, "safetyUnits": None, "profitYr": None,
                                "holdingCostYr": None, "holidayCostYr": None, "totalCostYr": None,
                                "bufferCash": None}
                continue
            lost_mean = _nanmean(g[f"lost_{pct}"])
            safety_mean = _nanmean(g[f"safety_{pct}"])
            uyr = lost_mean * cycles_per_year
            buffer_cash = safety_mean * uc if profitable else None
            holding_yr = buffer_cash * holding_annual if buffer_cash is not None else None
            profit_yr = uyr * margin if profitable else None
            hol_yr = (_HCV.cost_per_year(g, Z[pct], uc, holding_annual, coverage)
                      if profitable else None)
            st[str(pct)] = {"unitsYr": round(uyr, 1),
                            "safetyUnits": round(safety_mean, 0),
                            "profitYr": round(profit_yr, 0) if profit_yr is not None else None,
                            "holdingCostYr": round(holding_yr, 0) if holding_yr is not None else None,
                            "holidayCostYr": round(hol_yr, 0) if hol_yr is not None else None,
                            "totalCostYr": (round(profit_yr + holding_yr + hol_yr, 0)
                                            if (profit_yr is not None and holding_yr is not None) else None),
                            "bufferCash": round(buffer_cash, 0) if buffer_cash is not None else None}
        best_sku_tier = None
        if profitable:
            best_sku_tier = min((pct for pct in sorted(Z.keys())
                                 if str(pct) in st and st[str(pct)]["totalCostYr"] is not None),
                                key=lambda pct: st[str(pct)]["totalCostYr"], default=None)
        # `windows` counts the windows that could judge the buffer: that's what the app's
        # "measured on N windows" gate reads. All windows scored for accuracy are
        # `windowsTotal`.
        by_sku.append({"sku": str(sku), "costKnown": ck, "lossMaking": loss,
                       "windows": int(n_test), "windowsTotal": int(len(g)),
                       "untestable": n_test == 0,
                       "bestTier": best_sku_tier, "tiers": st})
    _lo = str(min(Z.keys()))
    by_sku.sort(key=lambda r: (r["tiers"].get(_lo) or {}).get("unitsYr") or 0.0, reverse=True)

    # ── Is the ranking real, or is it noise? ──────────────────────────────────
    # The tier totals come from mean shortfalls over a handful of windows per SKU, so
    # two tiers can differ by a few percent purely by chance. Resampling SKUs and
    # re-picking the winner each time turns "lowest cost" into "cheapest in X% of
    # resamples" — the difference between a recommendation and a coin flip.
    ranking = _bootstrap_tier_ranking(costed, out, cycles_per_year, holding_annual) if priced else None

    # The continuous optimum, and which tier's ACHIEVED service lands nearest it. The
    # tiers are targets; the buffer under-delivers; so the tier to pick is the one whose
    # OUTCOME is closest to optimal, not the one whose label is.
    crit = _critical_ratio(frame, coverage, holding_annual)
    nearest = None
    if crit and out:
        cand = [t for t in out if t["achievedService"] is not None]
        if cand:
            # Tie-break toward the LOWER tier: if two tiers land equally close to optimal
            # (common when several already over-deliver), the cheaper one in capital wins.
            nearest = min(cand, key=lambda t: (abs(t["achievedService"] - crit["optimalService"]),
                                               t["tier"]))["tier"]

    return {
        "assumptions": {"holdingPct": round(holding_annual * 100)},
        "costBasis": cost_basis, "priced": priced, "coverage": int(coverage),
        "cyclesPerYear": round(cycles_per_year, 1),
        "costedSkus": costed_skus, "totalSkus": total_skus,
        # Which products are missing from the money totals. Every dollar figure here is a
        # SUM over costed products, so an uncosted one doesn't just miss its own row — it
        # shifts the catalog totals and can change which tier wins. Naming them lets the
        # UI say the totals are incomplete instead of quietly under-reporting.
        "uncostedSkus": sorted(str(sku) for sku, g in frame.groupby("sku")
                               if not bool(g["costKnown"].any())),
        "lossMakingSkus": loss_making,
        # Tested for accuracy, but no window could judge the buffer: no tier is measured.
        "untestableSkus": untestable,
        "criticalRatio": crit, "nearestTier": nearest, "ranking": ranking,
        "mixedPolicy": (_mixed_policy(costed, out, cycles_per_year, holding_annual)
                        if priced else None),
        "tiers": out, "bySku": by_sku, "bestTier": best,
        # Holiday weeks with and without the holiday cover, per holiday, and the store's
        # measured rates (the app's forecast uses the primary pair's; see holiday_cover).
        "holidayCover": ({**(_HCV.summary(frame, Z, hc_fit) or {}), "rates": hc_fit["rates"]}
                         if hc_fit else None),
    }


def _testable_windows(frame) -> dict:
    """{sku: number of windows whose buffer could be judged}. A window is testable when
    its coverage flag is a number, not NaN (see bufferTestable in _run_cutoff)."""
    cols = [f"cov_{p}" for p in sorted(Z.keys()) if f"cov_{p}" in frame.columns]
    out = {}
    for sku, g in frame.groupby("sku"):
        if "bufferTestable" in g.columns:
            n = int(g["bufferTestable"].fillna(False).astype(bool).sum())
        elif cols:
            n = int(g[cols[0]].notna().sum())
        else:
            n = 0
        out[str(sku)] = n
    return out


def effective_horizon(horizon, combos=None, lead=None, coverage=None) -> int:
    """The test window has to hold the whole decision: the lead time AND the coverage
    after delivery. A fixed 44 days is 14 + 30; a product on a 21-day lead with 45 days
    of cover had its coverage window cut off at day 44 and its order error measured on a
    fraction of the days. Never shorter than asked for."""
    need = [int(horizon or 0)]
    for L, C in list(combos or []) + ([(lead, coverage)] if lead is not None and coverage is not None else []):
        try:
            need.append(int(L) + int(C))
        except (TypeError, ValueError):
            continue
    return max(need)


def _json_safe(o):
    """Recursively replace NaN/inf floats with None — JSON (and FastAPI) reject them,
    and an empty/degenerate window can legitimately produce one. None renders as '—'."""
    if isinstance(o, float):
        return o if (o == o and o not in (float("inf"), float("-inf"))) else None
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_json_safe(v) for v in o]
    return o


def merge_rows(old: pd.DataFrame | None, new: pd.DataFrame | None) -> pd.DataFrame:
    """Fresh rows replace stale ones for the same (product, lead, coverage); everything
    else is kept. This is what makes a one-product re-test safe: the other products'
    measurements are untouched, so catalog totals stay consistent."""
    if old is None or old.empty:
        return new if new is not None else pd.DataFrame()
    if new is None or new.empty:
        return old
    keys = set(zip(new["sku"].astype(str), new["leadDays"], new["coverageDays"]))
    mask = [(str(a), b, c) not in keys
            for a, b, c in zip(old["sku"].astype(str), old["leadDays"], old["coverageDays"])]
    return pd.concat([old[pd.Series(mask, index=old.index)], new], ignore_index=True)


def rebuild_from_rows(result: dict, rows: pd.DataFrame, *, combos, holding_pct=25,
                      sku_costs=None, sku_sources=None, spans=None) -> dict:
    """Recompute EVERY aggregate from a row set — accuracy, per-file, tiers, the mix.

    Used after a partial refit: the changed product's windows are new, everyone else's
    are the originals, and the catalog totals are rebuilt from the union so they always
    describe one coherent state rather than a stale sum with one product swapped in."""
    if rows is None or rows.empty:
        return result
    df = _apply_costs(rows, sku_costs)
    holding_annual = max(0.0, float(holding_pct) / 100.0)
    combos = combos or [(int(df["leadDays"].iloc[0]), int(df["coverageDays"].iloc[0]))]
    primary = df[(df["leadDays"] == combos[0][0]) & (df["coverageDays"] == combos[0][1])]
    by_sku = _grouped(primary, "sku").to_dict("records") if not primary.empty else []
    for r in by_sku:
        r["daysHistory"] = (spans or {}).get(r["sku"])
        sub = primary[primary["sku"] == r["sku"]]
        r["engine"] = (sub["route"].mode().iloc[0] if len(sub) and not sub["route"].mode().empty else None)
        r["source"] = (sku_sources or {}).get(str(r["sku"]))
    out = {
        **result,
        "tested": int(primary["sku"].nunique()) if not primary.empty else 0,
        "forecasts": int(len(primary)),
        "overall": _agg(primary, ci=True) if not primary.empty else {},
        "byEngine": _grouped(primary, "route").to_dict("records") if not primary.empty else [],
        "bySku": by_sku,
        "bySource": _by_source(primary, sku_sources) if sku_sources else result.get("bySource") or [],
        "byTrainLength": _by_train_bucket(primary),
        "byBlock": _by_block(primary),
        "tierAnalysis": _tier_summary(primary, combos[0][1], holding_annual),
        "tierAnalysisByCombo": {
            f"{L}/{C}": _tier_summary(df[(df["leadDays"] == L) & (df["coverageDays"] == C)], C, holding_annual)
            for L, C in combos},
        "combos": [{"lead": L, "coverage": C} for L, C in combos],
    }
    return _with_holiday_cover(out)


def _with_holiday_cover(res: dict) -> dict:
    """The store's holiday rates and holiday-week results at the top of a run, from the
    primary (lead, coverage) pair: the one the app's forecast reads (main._sync_holiday_rates)."""
    res["holidayCover"] = (res.get("tierAnalysis") or {}).get("holidayCover")
    return res


def _apply_costs(rows: pd.DataFrame, sku_costs: dict | None) -> pd.DataFrame:
    """Rewrite per-row economics from a costs map {sku: {cost, fees, price}}, keeping the
    original where none is given.

      price  today's shelf price, when given: every window is re-priced with it.
      cost   today's unit cost, when given: every window is re-costed with it.
      fees   the new per-unit fees.
    """
    df = rows.copy()
    costs = {str(k): v for k, v in (sku_costs or {}).items()}
    if not costs or df.empty:
        return df
    new_cost, new_margin, new_known, new_price, new_fees = [], [], [], [], []
    for _, r in df.iterrows():
        econ = costs.get(str(r["sku"])) or {}
        old_price = float(r.get("price") or 0.0)
        known0 = bool(r.get("costKnown"))
        uc0 = float(r.get("unitCost") or 0.0)
        if "fees" in r and r.get("fees") == r.get("fees") and r.get("fees") is not None:
            f0 = float(r.get("fees"))
        else:   # rows from before fees were stored: back them out of the margin
            f0 = (old_price - uc0 - float(r.get("marginUnit") or 0.0)) if known0 else 0.0
        price = float(econ["price"]) if econ.get("price") not in (None, "") else old_price
        c = econ.get("cost")
        if c not in (None, ""):
            known, uc = True, float(c)
            f = float(econ.get("fees") or 0.0)
        else:
            known, uc, f = known0, uc0, f0
        new_known.append(known); new_cost.append(uc if known else 0.0)
        new_price.append(round(price, 2)); new_fees.append(f)
        new_margin.append((price - uc - f) if known else 0.0)
    df["costKnown"], df["unitCost"], df["marginUnit"] = new_known, new_cost, new_margin
    df["price"], df["fees"] = new_price, new_fees
    return df


def recost(result: dict, rows: pd.DataFrame, sku_costs: dict | None,
           holding_pct=25, coverage=30, combos=None, rows_sink=None) -> dict:
    """Re-price an existing backtest with new costs — WITHOUT refitting anything.

    Unit cost and fees never touch a forecast. They don't change which model was chosen,
    the predictions, the residuals, or the units short and safety units at each tier —
    all of that is already measured and stored per window. They only decide what those
    units are WORTH. So adding a cost is arithmetic over rows we already have, not a
    reason to spend minutes refitting every product at every cutoff.

    Accuracy sections are returned untouched; only the money is recomputed."""
    if rows is None or rows.empty:
        return result
    # Price came from the fitted entry; cost, fees, and (when given) today's price move.
    df = _apply_costs(rows, sku_costs)
    if rows_sink is not None:
        rows_sink.append(df)
    holding_annual = max(0.0, float(holding_pct) / 100.0)
    combos = combos or [(int(df["leadDays"].iloc[0]), int(df["coverageDays"].iloc[0]))]
    primary = df[(df["leadDays"] == combos[0][0]) & (df["coverageDays"] == combos[0][1])]
    return _with_holiday_cover({
        **result,
        "tierAnalysis": _tier_summary(primary, combos[0][1], holding_annual),
        "tierAnalysisByCombo": {
            f"{L}/{C}": _tier_summary(df[(df["leadDays"] == L) & (df["coverageDays"] == C)], C, holding_annual)
            for L, C in combos},
        "recosted": True,
    })


def run_for_api(df, horizon=44, n_cutoffs=AUTO_CUTOFFS, step=28, lead=14, coverage=30,
                service_pct=95, min_train=120, holding_pct=25, sku_costs=None,
                baseline_route="prophet", combos=None, sku_sources=None, rows_sink=None,
                should_stop=None):
    """Run the backtest and return JSON-friendly results for the web UI.
    `sku_costs` = {sku_id: {"cost": float, "fees": float}} from the dashboard Scorecard,
    so the tier economics use each product's real cost (SKUs without one are skipped).
    `baseline_route` re-runs the SAME cutoffs with one engine forced on every SKU, so the
    UI can show whether routing beats a single-model policy; None skips that pass."""
    skipped = []
    holding_annual = max(0.0, float(holding_pct) / 100.0)
    combos = _normalise_combos(combos, lead, coverage)
    horizon = effective_horizon(horizon, combos)
    results = run_backtest(df, horizon, n_cutoffs, step, lead, coverage, service_pct,
                           min_train, verbose=False, skipped=skipped, sku_costs=sku_costs,
                           combos=combos, should_stop=should_stop)
    # Accuracy is identical across pairs (same fits), so report it from the primary one —
    # otherwise every SKU would appear N times in the fleet averages.
    primary = results[(results["leadDays"] == combos[0][0]) & (results["coverageDays"] == combos[0][1])] \
        if not results.empty else results
    # Hand the scored rows back so a later cost edit can be re-priced without refitting.
    if rows_sink is not None:
        rows_sink.append(results)
    by_eng = _grouped(primary, "route").to_dict("records") if not primary.empty else []
    by_sku = _grouped(primary, "sku").to_dict("records") if not primary.empty else []
    overall = _agg(primary, ci=True) if not primary.empty else {}
    # Attach each SKU's history length + engine so the AI diagnosis can't hallucinate them.
    spans = {s: int((g["ds"].max() - g["ds"].min()).days) for s, g in df.groupby("sku")}
    for r in by_sku:
        r["daysHistory"] = spans.get(r["sku"])
        r["source"] = (sku_sources or {}).get(str(r["sku"]))
        sub = primary[primary["sku"] == r["sku"]]
        r["engine"] = (sub["route"].mode().iloc[0] if len(sub) and not sub["route"].mode().empty else None)

    # ── Baseline policy comparison ────────────────────────────────────────────
    # The router's whole premise is that per-SKU model choice beats one model for
    # everything. Same cutoffs, same data, one engine forced — the only honest way to
    # check that. Forcing Prophet onto a 30-day sparse SKU may throw; those land in
    # baselineSkipped with a reason, which is itself the answer for that SKU.
    baseline = None
    if baseline_route and not results.empty:
        b_skipped = []
        try:
            b_res = run_backtest(df, horizon, n_cutoffs, step, lead, coverage, service_pct,
                                 min_train, verbose=False, skipped=b_skipped,
                                 sku_costs=sku_costs, force_route=baseline_route,
                                 should_stop=should_stop)
            if not b_res.empty:
                baseline = {"route": baseline_route, "overall": _agg(b_res, ci=True),
                            "bySku": _grouped(b_res, "sku").to_dict("records"),
                            "skipped": b_skipped,
                            "failedCutoffs": int(sum(1 for s in b_skipped if s.get("kind") == "error"))}
        except BacktestCancelled:
            raise
        except Exception as ex:
            baseline = {"route": baseline_route, "error": f"{type(ex).__name__}: {ex}"}

    return _json_safe(_with_holiday_cover({
        "params": {"horizon": horizon, "cutoffs": n_cutoffs, "step": step, "lead": lead,
                   "coverage": coverage, "service": service_pct, "minTrain": min_train,
                   "minWindowsReportable": MIN_WINDOWS_REPORTABLE},
        "skus": int(df["sku"].nunique()), "rows": int(len(df)),
        "tested": (int(primary["sku"].nunique()) if not primary.empty else 0),
        "forecasts": int(len(primary)),
        "failedCutoffs": int(sum(1 for s in skipped if s.get("kind") == "error")),
        "skipped": skipped, "overall": overall, "byEngine": by_eng, "bySku": by_sku,
        "capacity": cutoff_capacity(df, horizon, min_train, n_cutoffs, step),
        # Per-upload sections. Everything is modelled as one catalog — pooling and
        # relatedness reach across files — but each file's products are summarised on
        # their own, so a second upload doesn't disappear into a fleet average.
        "bySource": _by_source(primary, sku_sources) if sku_sources else [],
        "byTrainLength": _by_train_bucket(primary),
        "byBlock": _by_block(primary),
        "baseline": baseline,
        "combos": [{"lead": L, "coverage": C} for L, C in combos],
        "tierAnalysis": _tier_summary(primary, coverage, holding_annual),
        # One tier analysis per (lead, coverage) pair, keyed "lead/coverage". A product
        # whose lead time you changed reads its own pair instead of being stuck with the
        # catalog-wide one it no longer matches.
        "tierAnalysisByCombo": {
            f"{L}/{C}": _tier_summary(results[(results["leadDays"] == L) & (results["coverageDays"] == C)],
                                      C, holding_annual)
            for L, C in combos} if not results.empty else {},
    }))


def report(results: pd.DataFrame, service_pct=95, holding_annual=0.25, coverage=30):
    if results.empty:
        print("\nNo SKUs had enough history to backtest. Try a longer dataset or lower --min-train.")
        return
    lines = ["", "=" * 78, "BACKTEST RESULTS", "=" * 78,
             "MASE < 1.0 = beats a seasonal-naive forecast (lower is better) — the fairest single read.",
             "WAPE% is volume-weighted (WAPE%_unweighted averages the per-window ratios); the two",
             "  diverging means a few low-volume windows are driving the unweighted figure.",
             "bias% > 0 = over-forecasting (builds overstock).  interval_cov% should be ≈ 80.",
             f"service_achieved% should be ≈ your target ({service_pct}%); below = buffer too thin.",
             "  Read it against its 95% CI — windows overlap and repeat per SKU, so a gap narrower",
             "  than the interval is noise, not a mis-sized buffer.",
             "order_err% > 0 = the order would have been too big (over-stock).", "-" * 78]
    print("\n".join(lines))
    by_eng = _grouped(results, "route"); by_sku = _grouped(results, "sku")
    print("\nBy engine:"); print(by_eng.to_string(index=False))
    ov = _agg(results, ci=True)
    print("\nOverall:"); print(pd.Series(ov).to_frame().T.to_string(index=False))
    for label, key in (("service_achieved%", "service_achieved%_ci"), ("MASE", "MASE_ci"),
                       ("interval_cov%", "interval_cov%_ci")):
        if ov.get(key):
            print(f"  95% CI  {label}: {ov[key][0]} – {ov[key][1]}   (cluster bootstrap over {ov.get('skus')} SKUs)")
    print("\nPer SKU:"); print(by_sku.to_string(index=False))
    if "reportable" in by_sku.columns and (~by_sku["reportable"]).any():
        thin = ", ".join(by_sku.loc[~by_sku["reportable"], "sku"].astype(str))
        print(f"  ⚠ under {MIN_WINDOWS_REPORTABLE} windows — too few to read as a per-SKU result: {thin}")

    bt = _by_train_bucket(results)
    if len(bt) > 1:
        print("\nBy training length at the cutoff (how much history before the forecast is trustworthy):")
        print(pd.DataFrame([{"history": b["bucket"], "forecasts": b["forecasts"], "MASE": b["MASE"],
                             "WAPE%": b["WAPE%"], "bias%": b["bias%"],
                             "interval_cov%": b["interval_cov%"]} for b in bt]).to_string(index=False))
    bb = _by_block(results)
    if len(bb) > 1:
        print("\nRecent vs historic cutoffs (historic windows test an older business regime):")
        print(pd.DataFrame([{"block": b["block"], "forecasts": b["forecasts"], "MASE": b["MASE"],
                             "WAPE%": b["WAPE%"], "bias%": b["bias%"]} for b in bb]).to_string(index=False))

    ts = _tier_summary(results, coverage, holding_annual)
    if ts.get("tiers"):
        a = ts["assumptions"]
        basis = {"actual": "real per-SKU cost", "partial": "real cost where provided",
                 "none": "no cost provided"}[ts.get("costBasis", "none")]
        print(f"\nProtection tiers — calibration & annual cost  ({basis}; profit = price−cost−fees, "
              f"{a['holdingPct']}%/yr holding):")
        if not ts.get("priced"):
            print("  (no per-SKU cost → can't cost the tiers; add a Cost column. Showing calibration only.)")
        else:
            print(f"  (costed {ts['costedSkus']}/{ts['totalSkus']} tested SKUs — the rest had no cost.)")
        if ts.get("lossMakingSkus"):
            print(f"  ⚠ priced at/below cost (excluded — fix pricing, not buffer): {', '.join(ts['lossMakingSkus'])}")
        td = pd.DataFrame([{
            "tier%": t["tier"], "achievedService%": t["achievedService"],
            "95%CI": (f"{t['achievedServiceCI'][0]}–{t['achievedServiceCI'][1]}"
                      if t.get("achievedServiceCI") else "—"),
            "gap_vs_target": t["gap"], "units_short/yr": t["unitsShortYr"],
            "lost_profit$/yr": t["stockoutCost"], "buffer_cost$/yr": t["holdingCost"],
            "total$/yr": t["totalCost"], "cash_in_buffer$": t["bufferCash"],
            "best": "◀ lowest cost" if t["tier"] == ts["bestTier"] else "",
        } for t in ts["tiers"]])
        print(td.to_string(index=False))
        print(f"(units short/yr = avg units short per cycle × {ts['cyclesPerYear']} reorder cycles/yr; "
              f"lost profit/yr = units short/yr × each product's profit)")
        print("Reading it: achievedService% should land ≈ tier%. A big negative gap = the buffer is "
              "under-sized at that tier (a calibration issue), not proof the tier is wrong.")
        print("Check the gap against the 95% CI first — if the target sits inside the interval, "
              "you don't have enough windows to call it mis-sized.")
        cr, rk = ts.get("criticalRatio"), ts.get("ranking")
        if cr:
            print(f"\nOptimal service from your own economics: {cr['optimalService']}% achieved "
                  f"(per-SKU range {cr['range'][0]}–{cr['range'][1]}%).")
            print(f"  {cr['note']}")
            if ts.get("nearestTier"):
                nt = next(t for t in ts["tiers"] if t["tier"] == ts["nearestTier"])
                print(f"  → Target the {nt['tier']}% tier: it ACHIEVES {nt['achievedService']}%, closest to "
                      f"optimal. Tiers are targets and the buffer under-delivers, so pick on achieved, "
                      f"not on the label.")
        if rk:
            pc = ", ".join(f"{k}%: {v}%" for k, v in sorted(rk["pCheapest"].items()))
            print(f"\nIs the ranking real? P(cheapest) over {rk['resamples']} SKU resamples — {pc}")
            if rk["decisive"]:
                print(f"  {rk['winner']}% wins in {rk['winnerProb']}% of resamples — a real preference.")
            else:
                print(f"  ⚠ TOO CLOSE TO CALL: {rk['winner']}% ({rk['winnerProb']}%) vs {rk['runnerUp']}% "
                      f"({rk['runnerUpProb']}%). The cost difference is inside the noise — treat these as "
                      f"equivalent and decide on capital, risk appetite, or the optimal-service line above.")
    print("=" * 78)
    return by_eng, by_sku


def main():
    ap = argparse.ArgumentParser(description="Leakage-safe backtest for the LogiTrack engines.")
    ap.add_argument("file", nargs="?", help="sales .xlsx/.csv (Date, Units_Sold, [SKU, Category, Price, Units_In_Stock])")
    ap.add_argument("--shopify", action="store_true", help="pull data from Shopify (see shopify_source.py / SHOPIFY_* env vars)")
    ap.add_argument("--horizon", type=int, default=44, help="forecast/eval horizon in days (default lead+coverage)")
    ap.add_argument("--cutoffs", type=int, default=8)
    ap.add_argument("--step", type=int, default=28, help="days between cutoffs")
    ap.add_argument("--lead", type=int, default=14)
    ap.add_argument("--coverage", type=int, default=30)
    ap.add_argument("--service", type=float, default=95, choices=sorted(Z.keys()))
    ap.add_argument("--min-train", type=int, default=120, help="min days of history before a cutoff")
    ap.add_argument("--holding", type=int, default=25, help="annual inventory holding cost %% (storage+capital+obsolescence) for the tier cost comparison. Per-unit cost comes from a Cost column in the data; SKUs without one are skipped.")
    ap.add_argument("--engine", choices=["router", "prophet", "global", "croston", "abstain"], default="router",
                    help="force ONE engine on every SKU instead of routing — use to check the router earns its complexity")
    ap.add_argument("--vs", choices=["prophet", "global", "croston", "abstain"],
                    help="also run this single-engine policy over the same cutoffs and print both")
    ap.add_argument("--xlsx", help="optional path to write a styled report")
    a = ap.parse_args()

    if a.shopify:
        from shopify_source import fetch_sales      # decoupled data source
        df = normalize(fetch_sales())
    elif a.file:
        df = load_dataframe(a.file)
    else:
        ap.error("provide a file, or --shopify")

    forced = None if a.engine == "router" else a.engine
    cap = cutoff_capacity(df, a.horizon, a.min_train, a.cutoffs, a.step)
    print(f"\nCutoff capacity — how many test windows your data can actually support:")
    print(f"  requested {cap['requested']} · catalog max {cap['catalogMax']} "
          f"(limited by {cap['bindingSku']} at {cap['bindingDays']}d of history)")
    if cap["untestableSkus"]:
        print(f"  {cap['untestableSkus']} SKU(s) can't be tested at all (need ≥{a.min_train + a.horizon}d).")
    if cap["cappedSkus"]:
        print(f"  ⚠ {len(cap['cappedSkus'])} SKU(s) can't supply {cap['requested']} and will return fewer: "
              f"{', '.join(cap['cappedSkus'][:8])}{' …' if len(cap['cappedSkus']) > 8 else ''}")
    if cap["thinSkus"]:
        print(f"  ⚠ {len(cap['thinSkus'])} SKU(s) will land under {MIN_WINDOWS_REPORTABLE} windows and won't be "
              f"individually readable (needs ≥{cap['reportableNeedsDays']}d of history).")
    if not cap["cappedSkus"] and cap["catalogMax"] > cap["requested"]:
        print(f"  Every tested SKU can supply the full {cap['requested']}; you could raise it to "
              f"{cap['catalogMax']}. Tighter intervals, slower run.")
    print(f"Loaded {df['sku'].nunique()} SKU(s), {len(df):,} rows. Backtesting "
          f"(horizon={a.horizon}d, up to {a.cutoffs} cutoffs, recent step={a.step}d then spread back, "
          f"lead={a.lead}, cov={a.coverage}, target={a.service}%, engine={a.engine})…")
    hann = max(0.0, a.holding / 100.0)
    skipped = []
    results = run_backtest(df, a.horizon, a.cutoffs, a.step, a.lead, a.coverage, a.service, a.min_train,
                           skipped=skipped, force_route=forced)
    out = report(results, a.service, hann, a.coverage)
    errs = [s for s in skipped if s.get("kind") == "error"]
    if errs:
        print(f"\n⚠ {len(errs)} cutoff(s) failed and were excluded:")
        for s in errs[:10]:
            print(f"  · {s['sku']} @ {s.get('cutoff')}: {s['reason']}")
        if len(errs) > 10:
            print(f"  … and {len(errs) - 10} more")

    if a.vs:
        print(f"\n{'=' * 78}\nBASELINE POLICY: {a.vs} forced on every SKU (same cutoffs)\n{'=' * 78}")
        b_skipped = []
        b_res = run_backtest(df, a.horizon, a.cutoffs, a.step, a.lead, a.coverage, a.service,
                             a.min_train, verbose=False, skipped=b_skipped, force_route=a.vs)
        if b_res.empty:
            print(f"  {a.vs} produced no scorable windows — that is the result.")
        else:
            cmp_df = pd.DataFrame([{"policy": a.engine, **_agg(results)},
                                   {"policy": a.vs, **_agg(b_res)}])[
                ["policy", "forecasts", "MASE", "WAPE%", "bias%", "interval_cov%", "service_achieved%"]]
            print(cmp_df.to_string(index=False))
        b_errs = [s for s in b_skipped if s.get("kind") == "error"]
        if b_errs:
            print(f"  ({len(b_errs)} cutoff(s) failed under {a.vs} — forcing one engine on every SKU "
                  f"does not always fit; that is part of the comparison.)")
    if a.xlsx and out is not None:
        try:
            with pd.ExcelWriter(a.xlsx, engine="openpyxl") as w:
                results.to_excel(w, "Detail", index=False)
                out[0].to_excel(w, "By Engine", index=False)
                out[1].to_excel(w, "By SKU", index=False)
            print(f"Wrote {a.xlsx}")
        except Exception as ex:
            print(f"Could not write xlsx: {ex}")


if __name__ == "__main__":
    main()

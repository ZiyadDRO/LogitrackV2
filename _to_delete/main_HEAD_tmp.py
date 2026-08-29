"""
main.py — ROUTER build. Prophet + Global-pooled + Croston/TSB + MA fallback,
dispatched per-SKU by router.py. Client-agnostic.

Pipeline on upload/reset:
    build catalog (every SKU + its attribute columns)
      → group related SKUs (router.group_catalog)
      → per SKU: classify demand, count relatives, route(), fit chosen engine
      → cache entry (same shape the frontend already consumes) + route + reason.

Same API surface and JSON schema as before, plus: winningModel, demandClass,
route, routeReason, forecastDetails. Run on a DIFFERENT port from Prophet-only:
    uvicorn main:app --reload --app-dir engine_statsforecast --port 8000
"""
from fastapi import FastAPI, Query, UploadFile, File, HTTPException, Body, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import pandas as pd
import numpy as np
import datetime, io, os, json, math, threading, time
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta

from forecast_engine import analyse_price, classify_demand, compute_safe_price_range, fill_daily_gaps, sparse_subtype
import router as R
import datetime as _dt
import forecast_log as _FL
import stock_log as _SL
import censoring as _CEN
import promos as _PROMO
import uplift as _UP
import seasonality as _SEAS
import bundles as _BUN
import arrivals as _ARR
import catalog_store as _STORE
import backtest_store as _BTSTORE

# Bundle component map, {bundle_sku: {component_sku: qty}}. Session state like the rest of
# the catalogue — the real product reads this from Shopify's bundle definitions.
_bundle_map: dict = {}

# Instantiated at import: build_entry reads availability during a rebuild, which can be
# triggered before the endpoint definitions further down have been evaluated. Defining
# these at the bottom made that a NameError swallowed by a try/except — the feature would
# have looked wired while silently doing nothing.
_flog = _FL.ForecastLog()
_slog = _SL.StockLog()
import exports
from engines import ProphetEngine, GlobalPooledEngine, IntermittentEngine, MovingAverageEngine


@asynccontextmanager
async def _lifespan(app):
    # Replaces the deprecated @app.on_event("startup") hook.
    warmup()
    yield


app = FastAPI(lifespan=_lifespan)

# All catalog/cache mutation happens under this lock. State is module-level and
# requests can interleave (or two tabs can race an upload against a re-categorize);
# an RLock keeps rebuilds atomic without restructuring the app.
_state_lock = threading.RLock()
# Allow the local app on any port (vite dev 5173, the packaged http.server build,
# preview servers, etc.) so the browser→backend save isn't blocked by CORS just
# because the page is served on a different localhost port than expected.
#
# LOGITRACK_ORIGINS widens this for a hosted deployment WITHOUT loosening the local
# default: set it to a comma-separated list of exact origins the browser will be served
# from, e.g. LOGITRACK_ORIGINS="https://app.example.com". Localhost keeps working either
# way, so a dev machine pointed at a hosted backend still connects. Deliberately a list
# of origins and not a wildcard — allow_credentials with "*" is rejected by browsers,
# and an open CORS policy on an API holding a store's sales data is not a default worth
# shipping.
_LOCAL_ORIGIN_RE = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
_extra_origins = [o.strip() for o in os.environ.get("LOGITRACK_ORIGINS", "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_LOCAL_ORIGIN_RE,
    allow_origins=_extra_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
if _extra_origins:
    print(f"CORS: also allowing {', '.join(_extra_origins)}")


@app.get("/api/health")
def health():
    # `restoring` lets a caller tell "no data yet" apart from "data is on its way back".
    # `measured` is how many products came back with a measured protection level rather
    # than an estimate, so the UI can report what was actually recovered.
    return {"ok": True, "restore": dict(_restore_state), "skus": len(_sku_cache),
            "measured": len({k.split("|")[0] for k in _backtest_tier_cache})}

# ─── State ───────────────────────────────────────────────────────────────────
_sku_cache: dict[str, dict] = {}          # sku_id → forecast cache entry (frontend reads this)
_catalog:  dict[str, dict] = {}           # sku_id → {df, attrs, sku_name, mode, filename, events}
# ── Backtest state — session-scoped by design ────────────────────────────────
# Nothing here is written to disk or restored. Every launch starts clean: load a sheet,
# a backtest runs against it, and its results live only for that session — so a run can
# never be contaminated by a previous one, and there are no stale files to reason about.
_backtest_tier_cache: dict[str, dict] = {}  # "sku|lead|coverage" → measured protection tier
_sheet_costs: dict[str, float] = {}         # sku_id → unit cost seen at ingest (sheet/Shopify)
_backtest_inputs: dict = {}                 # the economics the last run was measured with
_session_holding_pct: int = 25              # set from the dashboard; used by EVERY run
_last_backtest_rows = None                  # scored windows, so cost edits re-price instantly
_last_backtest_combos: list = []
_backtest_exclusions: dict[str, str] = {}   # sku_id → why it has no measured tier
_last_backtest: dict | None = None          # full report from this session's last run

# ── Catalog persistence ──────────────────────────────────────────────────────
# The ingested data is written to disk after every change, and restored on boot. Only the
# DATA is saved; fitted models are re-built from it (see catalog_store for why). This is
# what makes a restart survivable: a release, a crash or a machine reboot no longer costs
# a full re-sync from Shopify.
_restore_state: dict = {"status": "idle", "skus": 0, "error": None}


def _persist_catalog():
    """Snapshot the catalog. Best-effort by design — a disk problem must never turn a
    successful upload into a failed one, so this reports and moves on."""
    try:
        extras = {"sheet_costs": {k: float(v) for k, v in _sheet_costs.items()},
                  "holding_pct": int(_session_holding_pct)}
        _STORE.save(_catalog, extras)
    except Exception as e:                                   # noqa: BLE001
        print(f"[catalog_store] snapshot skipped: {e}")


# Fingerprint of the catalog the in-memory backtest was measured against. Without it,
# a sync that does NOT auto-run a new test would leave measured tiers in memory that were
# measured on data no longer loaded — the exact contamination the on-disk fingerprint
# prevents, happening one level up.
_backtest_fingerprint: str | None = None


def _persist_backtest():
    """Snapshot the completed run, stamped with the data it was measured against."""
    try:
        globals()["_backtest_fingerprint"] = _BTSTORE.fingerprint(_catalog)
        _BTSTORE.save({"report": _last_backtest, "rows": _last_backtest_rows,
                       "combos": _last_backtest_combos, "tiers": _backtest_tier_cache,
                       "exclusions": _backtest_exclusions, "inputs": _backtest_inputs},
                      _catalog)
    except Exception as e:                                   # noqa: BLE001
        print(f"[backtest_store] snapshot skipped: {e}")


def _restore_backtest():
    """Bring back the last run IF it still describes the catalog we just restored.
    A mismatch is not an error — it means the data moved on, and an estimate is the
    honest answer until a fresh test runs."""
    saved = _BTSTORE.load(_catalog)
    if not saved:
        return False
    globals()["_last_backtest"] = saved.get("report")
    globals()["_last_backtest_rows"] = saved.get("rows")
    globals()["_last_backtest_combos"] = saved.get("combos") or []
    globals()["_backtest_tier_cache"] = saved.get("tiers") or {}
    globals()["_backtest_exclusions"] = saved.get("exclusions") or {}
    _backtest_inputs.clear(); _backtest_inputs.update(saved.get("inputs") or {})
    globals()["_backtest_fingerprint"] = _BTSTORE.fingerprint(_catalog)
    return True


def _drop_backtest_state(why: str) -> None:
    """Forget the measured run. Called when the data it described is gone."""
    globals()["_last_backtest"] = None
    globals()["_last_backtest_rows"] = None
    globals()["_last_backtest_combos"] = []
    globals()["_backtest_tier_cache"] = {}
    globals()["_backtest_exclusions"] = {}
    globals()["_backtest_fingerprint"] = None
    _backtest_inputs.clear()
    _BTSTORE.clear()
    print(f"Measured protection levels dropped — {why}. Estimates apply until a new test runs.")


# Model-switch history, persisted to disk so the overview banner survives restarts.
# Shape: {"events": [ {skuId, skuName, fromRoute, toRoute, fromLabel, toLabel,
#                       reason, date} ], "lastRoute": {sku_id: route}}
SWITCHES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_switches.json")
SWITCH_BANNER_DAYS = 7   # how long a switch stays on the overview banner


def _load_switches() -> dict:
    try:
        with open(SWITCHES_FILE) as f:
            d = json.load(f)
            d.setdefault("events", []); d.setdefault("lastRoute", {})
            return d
    except (FileNotFoundError, json.JSONDecodeError):
        return {"events": [], "lastRoute": {}}


def _save_switches(state: dict) -> None:
    try:
        with open(SWITCHES_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as ex:
        print(f"[switches] could not persist: {ex}")


_switch_state = _load_switches()


def _effective_sku_costs(payload_costs: dict | None) -> dict | None:
    """Best-known cost per SKU: the sheet's Cost column as the floor, Scorecard entries
    layered on top (Scorecard wins — the user may have corrected the sheet).

    Exists because dashboard-triggered runs used to depend ENTIRELY on the browser
    sending a complete costs map at exactly the right moment. One race — a queued run
    captured before the Scorecard state settled — and the run went costless, which
    wiped the measured tiers and stamped every product 'no unit cost on file' even
    though the costs were sitting right there in the Scorecard."""
    merged = {str(sid): {"cost": float(c), "fees": 0.0} for sid, c in _sheet_costs.items()}
    for sid, v in (payload_costs or {}).items():
        merged[str(sid)] = v
    return merged or None


def _store_backtest_result(res: dict, trigger: str) -> None:
    """Hold the last full report so the Backtest tab can show the run that already
    happened instead of an empty page. Session-scoped, like all backtest state."""
    global _last_backtest
    _last_backtest = {**res, "ranAt": time.time(), "trigger": trigger}


def _record_route(sku_id: str, sku_name: str, new_route: str, reason: str, log_event: bool = True) -> None:
    """Track a SKU's route. When `log_event` is False we silently re-baseline the route
    (update what we know) WITHOUT logging a 'switched model' event — used for re-uploads,
    re-categorization and restarts, where a route change isn't a real product maturation,
    just the catalog being recomputed. A genuine cross-update switch is logged otherwise."""
    prev = _switch_state["lastRoute"].get(sku_id)
    if log_event and prev and prev != new_route:
        _switch_state["events"].append({
            "skuId": sku_id, "skuName": sku_name,
            "fromRoute": prev, "toRoute": new_route,
            "fromLabel": R.ROUTE_LABELS.get(prev, prev),
            "toLabel": R.ROUTE_LABELS.get(new_route, new_route),
            "reason": reason, "date": today().strftime("%Y-%m-%d"),
        })
    if _switch_state["lastRoute"].get(sku_id) != new_route:
        _switch_state["lastRoute"][sku_id] = new_route
        _save_switches(_switch_state)


def _recent_switches(days: int = SWITCH_BANNER_DAYS) -> list:
    cutoff = (today() - pd.Timedelta(days=days)).date()
    out = []
    for ev in _switch_state["events"]:
        try:
            if pd.to_datetime(ev["date"]).date() >= cutoff:
                out.append(ev)
        except (ValueError, TypeError):
            continue
    return sorted(out, key=lambda e: e["date"], reverse=True)


# "Today" is evaluated FRESH on every call (never frozen at import) and, when the
# browser's timezone has been reported, in the USER's timezone — so a server that
# runs past midnight or sits in another region still shows the right current date.
_APP_TZ = None   # IANA name (e.g. "America/New_York"); set from the frontend


def set_timezone(tz):
    global _APP_TZ
    if tz and tz != _APP_TZ:
        try:
            ZoneInfo(tz); _APP_TZ = tz   # validate before storing
        except Exception:
            pass


def today():
    if _APP_TZ:
        try:
            return pd.Timestamp.now(tz=_APP_TZ).normalize().tz_localize(None)
        except Exception:
            pass
    return pd.Timestamp.now().normalize()



DEMAND_LABELS = {
    "smooth": ("Steady seller", "Sells most days in fairly consistent quantities."),
    "erratic": ("Choppy seller", "Sells most days, but quantities swing a lot."),
    "intermittent": ("Occasional seller", "Sells now and then, with many zero-sale days."),
    "lumpy": ("Sporadic seller", "Sells unpredictably and in bursts."),
    "no_demand": ("Almost no sales", "Too few sales to model meaningfully."),
}


def _conformal_text(q_lo, q_hi, calibrated):
    if calibrated:
        return (f"The shaded range is an 80% band from this engine's backtest errors — historically about "
                f"8 in 10 actual daily sales land inside it (≈ +{q_hi:.1f} / {q_lo:.1f} units around the line). "
                f"Built the same way for every product.")
    return (f"The shaded range is an 80% band from a standard statistical estimate "
            f"(±{q_hi:.1f} units around the line).")


def _make_details(method, reason, model_label, demand_class, residual_cv, q_lo, q_hi, bits, n_obs):
    d_label, d_desc = DEMAND_LABELS.get(demand_class, (demand_class, ""))
    cv = float(residual_cv)
    cons = "very consistent" if cv <= 0.30 else "moderately consistent" if cv < 0.60 else "highly variable"
    calibrated = method in ("prophet", "global", "croston")
    details = {
        "model": {"name": method, "label": model_label, "summary": f"{model_label} — {d_label.lower()}"},
        "demand": {"class": demand_class, "label": d_label, "description": d_desc},
        "route": method, "routeLabel": R.ROUTE_LABELS.get(method, method),
        "whyChosen": reason,
        "consistency": {"residualCv": round(cv, 3), "label": cons,
                        "text": (f"Day-to-day demand is {cons} (backtest variability {cv:.2f}). "
                                 f"The safety buffer is sized from how far the forecast actually misses "
                                 f"over a full lead-time window — which captures clumpy stretches, not "
                                 f"just this day-to-day swing.")},
        "interval": {"level": 80, "method": ("conformal" if calibrated else "normal-approx"),
                     "lowOffset": round(q_lo, 1), "highOffset": round(q_hi, 1),
                     "text": _conformal_text(q_lo, q_hi, calibrated)},
        "backtestText": "Accuracy/uncertainty is measured on history held out from fitting.",
        "dataPoints": int(n_obs),
    }
    if method == "abstain":
        details["caution"] = (
            "Last-resort estimate. This product has too little sales history of its own AND no similar "
            "products to borrow from, so the numbers below are a flat moving-average placeholder — not a "
            "real forecast. It will switch to a proper method once it builds up history or is grouped with "
            "similar products in the Categorize panel.")
    details.update(bits or {})          # seasonality / price / (level) from the engine
    return details


def _effective_price(last_price, events, today):
    """Price in force today(): last historical price, overridden by a permanent
    change dated on/before today, then a temporary change whose window covers
    today. Mirrors the scorecard's logic so 'Current price' tracks logged events."""
    if last_price is None:
        return None
    price = float(last_price)
    perms = sorted([e for e in (events or [])
                    if e.get("type") == "price_change_permanent" and e.get("new_price") is not None
                    and pd.to_datetime(e["date"]) <= today],
                   key=lambda e: pd.to_datetime(e["date"]))
    if perms:
        price = float(perms[-1]["new_price"])
    for e in (events or []):
        if e.get("type") == "price_change_temporary" and e.get("new_price") is not None:
            s = pd.to_datetime(e["date"]); en = pd.to_datetime(e.get("end_date", e["date"]))
            if s <= today <= en:
                price = float(e["new_price"])
    return price


# How many rolling 45-day holdout windows the self-calibration averages over per SKU.
# 1 = the old single-window behaviour (fast but noisy). More windows = a steadier σ/buffer
# estimate, but EACH window is a full refit, so cost scales linearly — and it hits the
# backtest hardest (it refits at every cutoff). 4–5 captures most of the noise reduction;
# going higher (e.g. 10) buys little extra stability for a much slower backtest/upload.
CALIB_WINDOWS = 5


def lead_window_sigma(residuals, lead):
    """Standard deviation of forecast error accumulated over LEAD-length windows,
    measured directly from out-of-sample residuals. This captures demand *clumping*
    (good/bad stretches that run several days in a row) that the textbook
    σ_daily × √lead buffer assumes away — which is why service can fall short of
    target even when the daily error is sized right. Returns None when there isn't
    enough holdout to estimate it, so the caller falls back to the √lead formula."""
    if residuals is None:
        return None
    L = int(lead)
    if L < 1:
        return None
    # `residuals` may be a single array OR a list of per-holdout-window arrays. We form
    # rolling L-day cumulative errors WITHIN each window (never across window seams) and
    # pool them, so multiple windows give a steadier lead-window estimate.
    windows = residuals if isinstance(residuals, list) else [residuals]
    sums = []
    for w in windows:
        r = np.asarray(w, dtype=float)
        if len(r) >= L + 5:
            sums.append(np.convolve(r, np.ones(L), "valid"))
    if not sums:
        return None
    pooled = np.concatenate(sums)
    s = float(np.std(pooled, ddof=1))
    return s if (np.isfinite(s) and s > 0) else None


def _cohort_promo_lift(related):
    """What similar products see from a promotion. Used when a product has none of its
    own — which is exactly the situation of a new product launching on a discount."""
    if related is None:
        return None
    frames = []
    # `related` is a list of peer FRAMES, not ids — the pooled engine consumes it
    # directly. Accept ids too, so a future caller passing either still works.
    for r in list(related)[:40]:
        d = r
        if isinstance(r, str):
            d = (_catalog.get(r) or {}).get("df")
        if d is None:
            continue
        try:
            if len(d) and "on_promotion" in getattr(d, "columns", []):
                frames.append(d)
        except (TypeError, AttributeError):
            continue
    if len(frames) < 2:
        return None
    try:
        return _UP.cohort_promo_lift(frames)
    except Exception:
        return None


def build_entry(sku_id, sku_name, df_clean, mode, filename, today, events, related, n_relatives,
                calibrate=True, calib_windows=None, force_route=None, availability=None):
    events = events or []
    df = df_clean[df_clean["ds"] <= today].copy()
    # Sales are not demand on a day you ran out. Dropping those days outright — the old
    # behaviour — is biased, because the days you sell out are the busy ones, so it
    # deleted the top of the distribution and every forecast drifted low. Instead: uplift
    # part-days by how long you were open, keep sold-out-but-open days as floors, and
    # exclude only days with nothing to sell.
    df, censor = _CEN.apply_to_frame(df, availability)
    stockout_dropped = int(censor.get("removed", 0))

    price_varied, price_well, last_price, tiers, warn_level, warn_msg = analyse_price(df)
    has_price = "price" in df.columns and bool(df["price"].notna().any())
    has_promo = "on_promotion" in df.columns and bool(df["on_promotion"].notna().any())

    days = (df["ds"].max() - df["ds"].min()).days if len(df) > 1 else 0
    sales = int(df["y"].sum())
    selling_days = int((df["y"] > 0).sum())
    demand_class = classify_demand(df["y"].to_numpy())

    # `force_route` bypasses the router so a backtest can score a single-engine policy
    # (e.g. "Prophet on everything") over the SAME cutoffs and prove the router earns
    # its complexity. Production never passes it; None → normal routing.
    if force_route:
        method, reason = force_route, f"forced to {force_route} (backtest comparison)"
    else:
        method, reason = R.route(days, sales, demand_class, n_relatives, selling_days=selling_days)
    # Price is only actually modeled on the Prophet route; global/croston/MA ignore it.
    price_modeled = (method == "prophet") and has_price
    effective_price = _effective_price(last_price, events, today)

    # Prophet takes on_promotion as a regressor and subtracts the lift itself. The other
    # three routes have no promo input at all, so for them a discount week is
    # indistinguishable from a real step up in demand — and those are exactly the routes
    # new products and intermittent sellers take, which is where launch promos live.
    fit_df, promo_report = _PROMO.hold_out(df, method)

    if method == "prophet":
        # `related` lets an established-but-under-a-year SKU (yearly seasonality off)
        # blend in a pooled annual shape from its cohesive peers; a no-op otherwise.
        eng = ProphetEngine().fit(fit_df, today, has_price, has_promo, last_price, events,
                                  related=related)
    elif method == "global":
        eng = GlobalPooledEngine().fit(fit_df, today, related=related)
    elif method == "croston":
        eng = IntermittentEngine().fit(fit_df, today)
    else:
        eng = MovingAverageEngine().fit(fit_df, today)

    # Prophet worked the price/promo effect out while fitting. The other routes can't, so
    # measure it separately and apply it as an explicit multiplier. Without this a
    # promotion you have SCHEDULED produces no lift at all on those routes — the tool
    # would never tell you to stock up for your own campaign.
    # Croston and the moving average have no seasonal curve — correctly, since you can't
    # place one on eight sales. But "can't see its own season" isn't "has no season": a
    # snow shovel selling 3 days a month sells them in January. Keep the rate as the
    # baseline and scale it month by month, from the product's own history if it has a
    # year of it, otherwise from its peers.
    season_report = None
    if method in ("croston", "abstain"):
        _peers = [r for r in (related or []) if hasattr(r, "columns")]
        _sea = _SEAS.resolve(df, _peers)
        if _sea.get("source") != "none":
            # The rate was measured over whichever months the history covers. If that
            # window sat in the busy season the rate is ALREADY elevated, and applying a
            # peak index on top would order twice over. Divide that context back out.
            _sea["coverage"] = round(_SEAS.coverage_factor(df, _sea["index"]), 4)
            season_report = {**_sea, "text": _SEAS.explain(_sea)}

    uplift_report = None
    if method != "prophet":
        _lift = _UP.promo_lift(df, cohort_lift=_cohort_promo_lift(related))
        _elast = _UP.price_elasticity(df)
        uplift_report = {"promo": _lift, "price": _elast,
                         "text": _UP.explain(_lift, _elast)}

    forecast = eng.forecast_df.copy()
    # Split history vs forecast at the SKU's LAST ACTUAL date, not the calendar
    # "today". Uploads are re-anchored so their last row is yesterday, which makes
    # today the first forecast day; keying off the last actual keeps today in the
    # forecast (not stranded as a blank between history and tomorrow).
    last_actual = df["ds"].max()
    future_fc = forecast[forecast["ds"] > last_actual].copy().reset_index(drop=True)

    if season_report and len(future_fc):
        _sm = _SEAS.future_multipliers(
            future_fc["ds"].dt.strftime("%Y-%m-%d").tolist(),
            season_report["index"], coverage=season_report.get("coverage", 1.0))
        if any(abs(m - 1.0) > 1e-9 for m in _sm):
            future_fc = _UP.apply(future_fc, _sm)
            season_report["applied"] = True
            season_report["peakMultiplier"] = round(max(_sm), 3)
            season_report["troughMultiplier"] = round(min(_sm), 3)

    if uplift_report and len(future_fc):
        _mults = _UP.future_multipliers(
            future_fc["ds"].dt.strftime("%Y-%m-%d").tolist(), events,
            uplift_report["promo"]["multiplier"],
            elasticity=uplift_report["price"]["elasticity"],
            base_price=last_price)
        if any(abs(m - 1.0) > 1e-9 for m in _mults):
            future_fc = _UP.apply(future_fc, _mults)
            uplift_report["applied"] = True
            uplift_report["daysLifted"] = sum(1 for m in _mults if m > 1.0)

    # Price range + safe extrapolation (model-agnostic; carries over).
    p_min = p_max = p_safe_min = p_safe_max = None; gap_warn = None
    if has_price and tiers:
        seen = sorted(t["price"] for t in tiers); p_min, p_max = seen[0], seen[-1]
        fit_df = df[["ds", "y"]].copy(); fit_df["price"] = df["price"].ffill().fillna(last_price or 0)
        p_safe_min, p_safe_max = compute_safe_price_range(fit_df, tiers, p_min, p_max, eng.residual_std, None)
        if len(seen) >= 2:
            gaps = [seen[i + 1] - seen[i] for i in range(len(seen) - 1)]; mg = max(gaps); rng = p_max - p_min
            if rng > 0 and mg / rng > 0.25:
                i = gaps.index(mg)
                gap_warn = (f"Large untested gap: ${seen[i]:.2f}–${seen[i+1]:.2f} (${mg:.2f}, "
                            f"{round(mg/rng*100)}% of trained range). Linear price sensitivity is assumed across it.")

    # ── Self-calibrating safety-stock σ ───────────────────────────────────────
    # In-sample residuals understate the real forecast error, so the safety
    # buffer (z·σ·√lead) comes out too thin — the backtest showed interval
    # coverage well under target and achieved service below the goal. Fix: hold
    # out the most recent ~45 days, REFIT on the rest through the exact same
    # pipeline, and measure the genuine OUT-OF-SAMPLE daily error. Widen σ when
    # that exceeds the in-sample estimate (never shrink below it). This is
    # self-calibrating per SKU — it sizes each buffer from that SKU's own holdout
    # error, so it generalizes to real data rather than being tuned to one file.
    # Wrapped so any failure silently falls back to the in-sample σ (never breaks
    # a live forecast). Skipped on the recursive holdout pass (calibrate=False).
    oos_resid = None   # list of per-window out-of-sample residual arrays (lead-window buffer)
    calib_ran = False  # True once a holdout window was actually scored (validated σ)
    n_calib_windows = int(calib_windows) if calib_windows else CALIB_WINDOWS
    if calibrate and days >= 120:
        try:
            in_sample = float(eng.residual_std)
            first_day = df["ds"].min(); WIN = 45
            wins = []
            # Roll several NON-overlapping 45-day holdout windows back in time, refitting
            # before each, so the error estimate isn't hostage to one possibly-quiet stretch.
            # As many windows as history allows, up to n_calib_windows (short SKUs use fewer;
            # the backtest passes a smaller count since it already refits at every cutoff).
            for k in range(n_calib_windows):
                cutoff_cal = today - pd.Timedelta(days=WIN * (k + 1))
                if (cutoff_cal - first_day).days < 60:      # need ≥60 days to train before a window
                    break
                cal = build_entry(sku_id, sku_name, df_clean, mode, filename, cutoff_cal,
                                  events, related, n_relatives, calibrate=False,
                                  availability=availability)
                tend = cutoff_cal + pd.Timedelta(days=WIN)
                hold = (df[(df["ds"] > cutoff_cal) & (df["ds"] <= tend)][["ds", "y"]]
                        .merge(cal["forecast"][["ds", "yhat"]], on="ds", how="inner"))
                if len(hold) >= 7:
                    wins.append((hold["y"] - hold["yhat"]).to_numpy(float))
            if wins:
                calib_ran = True
                oos_resid = wins                                 # kept per-window for the buffer
                oos = float(np.std(np.concatenate(wins), ddof=1))  # pooled daily error → σ / bands
                if np.isfinite(oos) and in_sample > 0 and oos > in_sample:
                    ratio = min(oos / in_sample, 4.0)   # cap so a noisy holdout can't explode the bands
                    eng.residual_std = round(oos, 4)
                    mean_y = float(df["y"].mean())
                    if mean_y > 0:
                        eng.residual_cv = round(oos / mean_y, 4)
                    # Widen the forward prediction bands by the same factor so coverage
                    # and service reflect real out-of-sample error, not in-sample optimism.
                    if {"yhat_lower", "yhat_upper"}.issubset(forecast.columns):
                        fut = forecast["ds"] > last_actual
                        lo = forecast.loc[fut, "yhat"] - (forecast.loc[fut, "yhat"] - forecast.loc[fut, "yhat_lower"]) * ratio
                        hi = forecast.loc[fut, "yhat"] + (forecast.loc[fut, "yhat_upper"] - forecast.loc[fut, "yhat"]) * ratio
                        forecast.loc[fut, "yhat_lower"] = lo.clip(lower=0)
                        forecast.loc[fut, "yhat_upper"] = hi
                        future_fc = forecast[forecast["ds"] > last_actual].copy().reset_index(drop=True)
        except Exception:
            pass

    # ── Uncalibrated-σ widening ───────────────────────────────────────────────
    # When holdout calibration could NOT run (under ~120 days of history, or no
    # scoreable window), Prophet's band/σ come from IN-SAMPLE residuals, which
    # systematically understate real error. Apply a fixed widening so young SKUs
    # don't ship optimistic bands and thin safety buffers. Only Prophet needs
    # this: the pooled/croston/MA engines already size σ from pooled or CV
    # residuals rather than a fit the model has seen.
    UNCALIBRATED_WIDEN = 1.30
    if calibrate and not calib_ran and method == "prophet":
        try:
            eng.residual_std = round(float(eng.residual_std) * UNCALIBRATED_WIDEN, 4)
            mean_y = float(df["y"].mean())
            if mean_y > 0:
                eng.residual_cv = round(float(eng.residual_std) / mean_y, 4)
            if {"yhat_lower", "yhat_upper"}.issubset(forecast.columns):
                fut = forecast["ds"] > last_actual
                lo = forecast.loc[fut, "yhat"] - (forecast.loc[fut, "yhat"] - forecast.loc[fut, "yhat_lower"]) * UNCALIBRATED_WIDEN
                hi = forecast.loc[fut, "yhat"] + (forecast.loc[fut, "yhat_upper"] - forecast.loc[fut, "yhat"]) * UNCALIBRATED_WIDEN
                forecast.loc[fut, "yhat_lower"] = lo.clip(lower=0)
                forecast.loc[fut, "yhat_upper"] = hi
                future_fc = forecast[forecast["ds"] > last_actual].copy().reset_index(drop=True)
        except Exception:
            pass

    details = _make_details(method, reason, eng.model_label, demand_class, eng.residual_cv,
                            eng._q_lo, eng._q_hi, eng.explain_bits(), len(df))
    details["relatives"] = int(n_relatives)

    return {
        "engine": eng, "df_train": df, "forecast": forecast, "future_fc": future_fc,
        "mode": mode, "filename": filename, "sku_name": sku_name,
        "active_yearly": days >= 450, "active_weekly": days >= 14,
        "has_price": has_price, "has_promo": has_promo,
        "price_varied": price_varied, "price_well_sampled": price_well, "last_price": last_price,
        "price_tiers": tiers, "price_trained_min": p_min, "price_trained_max": p_max,
        "price_safe_min": p_safe_min, "price_safe_max": p_safe_max, "price_gap_warning": gap_warn,
        "price_warn_level": warn_level, "price_warn_msg_upload": warn_msg,
        "stockout_rows_dropped": stockout_dropped, "censoring": censor,
        "promo_handling": promo_report, "uplift": uplift_report,
        "seasonality_applied": season_report,
        "residual_cv": eng.residual_cv, "residual_std": eng.residual_std, "oos_residuals": oos_resid,
        "sigma_calibrated": calib_ran, "events": events,
        "winning_model": eng.model_label, "demand_class": demand_class,
        "route": method, "route_reason": reason, "explain": details,
        "price_modeled": price_modeled, "effective_price": effective_price,
    }


_last_rebuild_errors: dict[str, str] = {}   # sku_id → why it couldn't be modeled (surfaced on upload)
NO_CATEGORY_MARKERS = ("no category", "uncategorized", "uncategorised", "unknown category", "no product family")


def _exclude_from_pooling(c: dict, as_of=None) -> bool:
    """Catalog-level dead/dormant guard for relatedness.
    Scorecard dead stock also depends on manager-entered current stock, so the
    frontend does the exact UI exclusion. Here we conservatively keep no-recent-
    sales products from donating behavior to pooled forecasts.
    """
    df = c.get("df")
    if df is None or len(df) == 0 or "y" not in df.columns:
        return False
    as_of = as_of or today()
    sold = df[df["y"] > 0]
    if sold.empty:
        return True
    last_sale = pd.Timestamp(sold["ds"].max())
    if (as_of - last_sale).days > 180:
        return True
    recent = df[df["ds"] > (as_of - pd.Timedelta(days=90))]
    return len(recent) > 0 and float(recent["y"].sum()) <= 0


def _rebuild(today, only: str | None = None, record_switches: bool = False):
    """Recompute relatedness over the whole catalog, then (re)fit one or all SKUs.
    Each SKU is built independently and guarded: one SKU that fails to model (e.g. an
    odd lifecycle/edge case) is SKIPPED and recorded — it must never take down the whole
    upload, which previously left the dashboard silently empty.
    `record_switches` defaults to False: routing recomputes (re-upload, re-categorize,
    restart) silently re-baseline rather than spamming 'model switched' banners."""
    global _last_rebuild_errors
    if only is None:
        _last_rebuild_errors = {}
    pool_catalog = {sid: c for sid, c in _catalog.items() if not _exclude_from_pooling(c, today)}
    base_cols = R.detect_group_columns(pool_catalog)
    groups, group_meta = R.adaptive_group_catalog(pool_catalog, base_cols)
    clusters = R.cluster_catalog(groups, pool_catalog)   # behavioural sub-clusters per category
    targets = [only] if only else list(_catalog.keys())
    for sku_id in targets:
        c = _catalog[sku_id]
        try:
            # Pool only from the behavioural cluster this SKU best fits (not every
            # category-mate). n_rel is the chosen cluster's size, so routing to the
            # global model still requires ≥ MIN_RELATIVES genuinely-similar peers.
            rels, n_rel, cluster_info = R.behavioral_relatives(sku_id, groups, pool_catalog, clusters)
            # Hours-in-stock, where we have it. Absent → every day counts as fully
            # available, so an untracked catalogue behaves exactly as before.
            _avail = None
            try:
                _cov = _slog.coverage(sku_id)
                if _cov.get("samples"):
                    _avail = {r["date"]: r["hoursInStock"] for r in _slog.availability_series(
                        sku_id, _cov["firstSeen"], _FL.today().isoformat())
                        if r["hoursInStock"] is not None}
            except Exception:
                _avail = None
            entry = build_entry(sku_id, c["sku_name"], c["df"], c["mode"], c["filename"],
                                today, c.get("events", []), rels, n_rel, availability=_avail)
            entry["cluster_info"] = cluster_info
            entry["group_info"] = group_meta.get(sku_id)
            _sku_cache[sku_id] = entry
            _last_rebuild_errors.pop(sku_id, None)
            # Log a forecast-model switch (e.g. global→Prophet) for the overview banner.
            _record_route(sku_id, c["sku_name"], entry["route"], entry["route_reason"], log_event=record_switches)
        except Exception as ex:
            import traceback; traceback.print_exc()
            _last_rebuild_errors[sku_id] = f"{type(ex).__name__}: {ex}"
            # If this SKU already had a working entry (e.g. a re-fit after an event
            # save or re-categorize failed), KEEP the previous entry rather than
            # making the SKU vanish from the dashboard. Only brand-new SKUs (first
            # fit, nothing to fall back to) stay absent — reported via errors.
            if sku_id not in _sku_cache:
                pass  # nothing to preserve; the upload response reports the error


def warmup():
    """Start empty. There is no demo dataset — the app shows an empty state until you
    load real data, so nothing synthetic can ever be mistaken for your own numbers or
    leak into a forecast, a backtest or an export."""
    global _backtest_tier_cache, _backtest_exclusions, _last_backtest
    _backtest_tier_cache, _backtest_exclusions, _last_backtest = {}, {}, None
    _sheet_costs.clear()
    _backtest_inputs.clear()
    globals()["_last_backtest_rows"] = None
    globals()["_last_backtest_combos"] = []
    with _state_lock:
        _catalog.clear(); _sku_cache.clear()

    saved, extras = _STORE.load()
    restored_tiers = 0
    if not saved:
        _restore_state.update({"status": "empty", "skus": 0, "error": None})
        print("Ready — no data loaded yet. Upload a sales file or connect Shopify.")
        return

    # Re-fit on a background thread rather than inside startup. A hosted deployment's
    # health check should not wait minutes for a model fit, and a slow boot is the
    # difference between a rolling deploy and a failed one. /api/health reports progress;
    # until it finishes the app behaves exactly as it does before any data is loaded.
    with _state_lock:
        _catalog.update(saved)
        _sheet_costs.update({k: float(v) for k, v in (extras.get("sheet_costs") or {}).items()})
        if extras.get("holding_pct") is not None:
            globals()["_session_holding_pct"] = int(extras["holding_pct"])
    # Before the refit, not after: the tier cache is keyed to the DATA, not to the fitted
    # models, so it is valid the moment the catalog is back.
    if _restore_backtest():
        restored_tiers = len({k.split("|")[0] for k in _backtest_tier_cache})
        print(f"Measured protection levels restored for {restored_tiers} products.")
    _restore_state.update({"status": "restoring", "skus": len(saved), "error": None})
    print(f"Restoring {len(saved)} products from the last session. Re-fitting in the background...")

    def _refit():
        try:
            with _state_lock:
                _rebuild(today())
            _restore_state.update({"status": "ready", "skus": len(_sku_cache)})
            # Say which it actually is. The old wording claimed estimates unconditionally,
            # including right after a run had just been restored — which read as though
            # the restore hadn't worked.
            note = (f"{restored_tiers} with measured protection levels."
                    if restored_tiers else
                    "Protection levels are estimates until a backtest runs.")
            print(f"Ready. {len(_sku_cache)} products restored. {note}")
        except Exception as e:                               # noqa: BLE001
            _restore_state.update({"status": "failed", "error": str(e)})
            print(f"Restore failed ({e}). Upload a sales file or connect Shopify.")

    threading.Thread(target=_refit, name="restore-refit", daemon=True).start()


# ─── SKU list / delete / events ──────────────────────────────────────────────
@app.get("/api/skus")
def list_skus():
    out = []
    for sku_id, e in _sku_cache.items():
        df = e["df_train"]
        out.append({"id": sku_id, "name": e["sku_name"] or sku_id, "mode": e["mode"], "filename": e["filename"],
                    "daysOfHistory": int((df["ds"].max() - df["ds"].min()).days) if df is not None else 0,
                    "totalUnitsSold": int(df["y"].sum()) if df is not None else 0})
    return out


# Friendly labels for the demand-pattern fallback breakdown (used when categories
# aren't distinguishing — e.g. a single-niche seller where every SKU is one category).
_PATTERN_LABEL = {"smooth": "Steady sellers", "erratic": "Variable", "intermittent": "Occasional",
                  "lumpy": "Sporadic", "no_demand": "No recent sales"}


@app.post("/api/fleet-summary")
def fleet_summary(payload: dict = Body(default={})):
    """Aggregate the catalog into the Fleet-overview metrics, with smart fallbacks:
      • $ values need unit costs — if too few SKUs have a cost, we report UNITS instead.
      • the breakdown shows categories ONLY if they're distinguishing; otherwise it falls
        back to demand pattern (which the tool always knows and is meaningful for any store).
    Reuses get_forecast per SKU (reads cached models — no refit, so this is cheap)."""
    set_timezone(payload.get("tz"))
    params = payload.get("params") or {}
    h = {"healthy": 0, "reorder_soon": 0, "overdue": 0, "overstocked": 0, "dead": 0}
    r_units = r_value = r_known = r_n = 0
    inv_value = inv_known = inv_n = dead_value = dead_n = 0
    cats: dict[str, int] = {}; patt: dict[str, int] = {}
    for sid, e in list(_sku_cache.items()):
        p = params.get(sid) or {}
        stock = int(p.get("stock") or 0); lt = int(p.get("leadTime") or 14); cov = int(p.get("coverage") or 30)
        oo = int(p.get("onOrder") or 0); fees = float(p.get("fees") or 0.0)
        cost = p.get("cost"); has_cost = cost not in (None, "") and float(cost or 0) > 0
        try:
            fc = get_forecast(sku_id=sid, stock=stock, lead_time_days=lt, coverage_days=cov,
                              strategy=p.get("strategy") or "balanced", forecast_months=1, units_on_order=oo,
                              unit_cost=(float(cost) if has_cost else None), fees=fees,
                              protection=p.get("protection"), tz=payload.get("tz"))
        except Exception:
            continue
        d_ro = fc.get("daysUntilReorder"); d_so = fc.get("daysUntilStockout")
        oq = int(fc.get("orderQty") or 0)
        if fc.get("inactive"):                         bucket = "dead"
        elif oo > 0:                                   bucket = "healthy"   # on order = covered
        elif d_ro is not None and d_ro <= 0:           bucket = "overdue"
        elif d_ro is not None and d_ro <= 7:           bucket = "reorder_soon"
        elif oq == 0 and (d_so is None or d_so > (lt + cov) * 3): bucket = "overstocked"
        else:                                          bucket = "healthy"
        h[bucket] += 1
        if bucket in ("overdue", "reorder_soon"):
            r_n += 1; r_units += oq
            if has_cost: r_known += 1; r_value += oq * (float(cost) + fees)
        inv_n += 1
        if has_cost: inv_known += 1; inv_value += stock * float(cost)
        if bucket == "dead":
            dead_n += 1
            if has_cost: dead_value += stock * float(cost)
        cat = (e_attrs := (_catalog.get(sid, {}).get("attrs") or {})).get("category") or e_attrs.get("Category")
        if cat not in (None, ""): cats[str(cat)] = cats.get(str(cat), 0) + 1
        patt[_PATTERN_LABEL.get(e.get("demand_class"), "Other")] = patt.get(_PATTERN_LABEL.get(e.get("demand_class"), "Other"), 0) + 1
    total = sum(h.values())
    # categories are only useful if there are ≥2 AND no single one swallows the catalog
    use_cat = len(cats) >= 2 and (max(cats.values()) / max(total, 1)) < 0.8
    src = cats if use_cat else patt
    items = sorted(({"label": k, "count": v} for k, v in src.items()), key=lambda x: -x["count"])[:6]
    r_costs_ok = r_n > 0 and r_known / r_n >= 0.5
    inv_costs_ok = inv_n > 0 and inv_known / inv_n >= 0.5
    return {
        "total": total, "needAction": h["overdue"] + h["reorder_soon"], "health": h,
        "reorder": {"units": r_units, "value": (round(r_value) if r_costs_ok else None), "skus": r_n},
        "inventory": {"value": (round(inv_value) if inv_costs_ok else None)},
        "deadStock": {"count": dead_n, "value": (round(dead_value) if (inv_costs_ok and dead_n) else None)},
        "breakdown": {"by": ("category" if use_cat else "pattern"), "items": items},
    }


@app.delete("/api/skus/{sku_id}")
def delete_sku(sku_id: str):
    with _state_lock:
        if sku_id not in _sku_cache and sku_id not in _catalog:
            raise HTTPException(404, "SKU not found.")
        _sku_cache.pop(sku_id, None); _catalog.pop(sku_id, None)
        # Relatedness/pooling was computed WITH this SKU in the catalog — rebuild so
        # remaining SKUs stop borrowing from (or counting) the deleted product.
        try:
            _rebuild(today())
        except Exception as ex:
            import traceback; traceback.print_exc()
            print(f"[delete] rebuild after deleting {sku_id} failed: {ex}")
        _persist_catalog()
    return {"success": True, "deleted": sku_id}


_EVENT_TYPES = {"promotion", "price_change_permanent", "price_change_temporary"}


def _validate_events(events) -> list:
    """Sanity-check the event payload BEFORE it's applied to the catalog. A malformed
    event used to blow up the re-fit, which dropped the SKU from the cache entirely.
    Raises HTTPException(400) with a plain-language reason instead."""
    if not isinstance(events, list):
        raise HTTPException(400, "Events must be a list.")
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            raise HTTPException(400, f"Event #{i + 1} is not an object.")
        etype = e.get("type")
        if etype not in _EVENT_TYPES:
            raise HTTPException(400, f"Event #{i + 1} has unknown type '{etype}'.")
        try:
            start = pd.to_datetime(e.get("date"))
            if pd.isna(start):
                raise ValueError
        except Exception:
            raise HTTPException(400, f"Event #{i + 1} has an invalid date '{e.get('date')}'.")
        if e.get("end_date") not in (None, ""):
            try:
                end = pd.to_datetime(e["end_date"])
                if pd.isna(end):
                    raise ValueError
            except Exception:
                raise HTTPException(400, f"Event #{i + 1} has an invalid end date '{e.get('end_date')}'.")
            if end < start:
                raise HTTPException(400, f"Event #{i + 1} ends before it starts.")
        if etype in ("price_change_permanent", "price_change_temporary"):
            try:
                np_ = float(e.get("new_price"))
                if not np.isfinite(np_) or np_ < 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise HTTPException(400, f"Event #{i + 1} needs a valid non-negative new price.")
        if e.get("discount_pct") not in (None, ""):
            try:
                d = float(e["discount_pct"])
                if not (0 <= d <= 100):
                    raise ValueError
            except (TypeError, ValueError):
                raise HTTPException(400, f"Event #{i + 1} discount must be between 0 and 100.")
    return events


@app.post("/api/skus/{sku_id}/events")
def save_events(sku_id: str, events: list = Body(...)):
    with _state_lock:
        if sku_id not in _catalog:
            raise HTTPException(404, "SKU not found.")
        events = _validate_events(events)
        prev = _catalog[sku_id].get("events", [])
        _catalog[sku_id]["events"] = events
        _rebuild(today(), only=sku_id)
        if sku_id in _last_rebuild_errors:
            # The re-fit failed even after validation — roll the events back so the
            # SKU keeps its previous (working) forecast instead of a broken state.
            _catalog[sku_id]["events"] = prev
            err = _last_rebuild_errors.pop(sku_id, "unknown error")
            try:
                _rebuild(today(), only=sku_id)
            except Exception:
                pass
            raise HTTPException(400, f"Events could not be applied ({err}); previous events kept.")
        # Only reached when the events applied cleanly — the rollback path raises above,
        # and what it rolls back to is already what's on disk.
        _persist_catalog()
    return {"success": True, "eventsApplied": len(events)}


# ─── Template ────────────────────────────────────────────────────────────────
@app.get("/api/template")
def download_template():
    dates = pd.date_range("2024-01-01", periods=20, freq="D"); rows = []
    pa = [23.99] * 10 + [19.99] * 10
    sa = [42, 38, 51, 47, 33, 60, 58, 44, 39, 52, 55, 50, 63, 59, 44, 74, 71, 57, 51, 65]
    pr = [0] * 18 + [1, 1]
    for d, u, p, q in zip(dates, sa, pa, pr):
        rows.append({"SKU": "WIDGET-A", "SKU_Name": "Blue Widget", "Category": "Widgets", "Date": d.strftime("%Y-%m-%d"),
                     "Units_Sold": u, "Price": p, "On_Promotion": q,
                     "Units_In_Stock": max(0, 300 - sum(sa[:list(sa).index(u) + 1]))})
    sb = [22, 18, 31, 27, 13, 40, 38, 24, 19, 32, 25, 21, 34, 30, 16, 43, 41, 27, 22, 35]
    for d, u in zip(dates, sb):
        rows.append({"SKU": "GADGET-B", "SKU_Name": "Red Gadget", "Category": "Gadgets", "Date": d.strftime("%Y-%m-%d"),
                     "Units_Sold": u, "Price": 14.99, "On_Promotion": 0,
                     "Units_In_Stock": max(0, 200 - sum(sb[:list(sb).index(u) + 1]))})
    # Build in memory and stream — the old NamedTemporaryFile(delete=False) left a
    # file in /tmp on every download and never cleaned it up.
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        pd.DataFrame(rows).to_excel(w, index=False, sheet_name="Sales Data")
        ws = w.sheets["Sales Data"]
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = 16
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="logitrack_multi_sku_template.xlsx"'})


# ─── Upload ──────────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB — plenty for years of daily rows
MAX_UPLOAD_ROWS  = 1_000_000          # guard against pathological files


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), mode: str = Query(default="replace")):
    """`mode=append` extends the loaded fleet instead of replacing it: new products are
    added, and products already present get this file's rows merged into their existing
    history on true calendar dates."""
    if not file.filename.endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(400, "Only .xlsx, .xls, or .csv files are accepted.")
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File is too large ({len(contents) / 1e6:.0f} MB; limit "
                                 f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB). Export a smaller date range.")
    try:
        df_raw = pd.read_csv(io.BytesIO(contents)) if file.filename.endswith(".csv") else pd.read_excel(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(400, f"Could not read the file: {e}")
    if len(df_raw) > MAX_UPLOAD_ROWS:
        raise HTTPException(413, f"File has {len(df_raw):,} rows (limit {MAX_UPLOAD_ROWS:,}).")
    with _state_lock:
        return _ingest(df_raw, file.filename, append=(str(mode).lower() == "append"))


def _true_dates(entry) -> pd.DataFrame:
    """A catalog frame back on its ORIGINAL calendar dates.

    Every upload is re-anchored so its newest row becomes yesterday. That offset is
    stored per entry, so undoing it recovers the dates the file actually carried —
    which is the only sound basis for merging a second upload into the first."""
    d = entry["df"].copy()
    sh = int(entry.get("date_shift_days") or 0)
    if sh and len(d):
        d["ds"] = d["ds"] - pd.Timedelta(days=sh)
    return d


def _reanchor_catalog() -> int:
    """Re-apply ONE shared offset across the whole catalog so the newest row anywhere
    lands on yesterday. Shared, not per-SKU: relative spacing between SKUs is what the
    pooling and seasonal comparison rely on, so they must all move together."""
    # Once a live source is in the catalog, its dates ARE the calendar — there is nothing
    # to anchor to and shifting would only introduce error. A mixed catalog (an old sheet
    # sitting alongside a Shopify feed) is left on true dates for the same reason: the
    # live feed is the one that knows what today is.
    if any(e.get("live_source") for e in _catalog.values()):
        for e in _catalog.values():
            e["date_shift_days"] = 0
        print("Re-anchor skipped — catalog contains live data, which keeps its real dates.")
        return 0

    trues = []
    for e in _catalog.values():
        d = _true_dates(e)
        if len(d):
            trues.append(d["ds"].max())
    if not trues:
        return 0
    offset = (today() - pd.Timedelta(days=1)).normalize() - max(trues).normalize()
    shift = int(offset.days)
    for e in _catalog.values():
        d = _true_dates(e)
        if len(d):
            d["ds"] = d["ds"] + offset
        e["df"] = d
        e["date_shift_days"] = shift
    return shift


def _merge_sku_history(sid: str, new_df: pd.DataFrame, new_shift: int) -> tuple[int, int]:
    """Fold a newly uploaded frame into whatever history that SKU already had.

    Both sides are put back on true dates first, so a July file lands after a June one
    instead of being stacked on top of it. On a date present in both, the NEW file wins —
    a re-export is assumed to be a correction. Returns (rows_added, overlap_rows)."""
    prev = _catalog.get(sid)
    if prev is None or not len(prev.get("df", [])):
        return len(new_df), 0
    old = _true_dates(prev)
    new = new_df.copy()
    if new_shift and len(new):
        new["ds"] = new["ds"] - pd.Timedelta(days=new_shift)
    overlap = int(old["ds"].isin(new["ds"]).sum())
    merged = pd.concat([old, new], ignore_index=True)
    # kind="stable" is load-bearing, not a preference: the default quicksort reorders
    # equal keys arbitrarily, so keep="last" would pick the old row or the new one at
    # random on any overlapping date. Stable sort keeps concat order (old, then new), so
    # the re-export deterministically wins.
    merged = (merged.sort_values("ds", kind="stable").drop_duplicates(subset=["ds"], keep="last")
                    .reset_index(drop=True))
    # A gap between the two ranges (e.g. Jan–Jun then Aug–Dec) must become explicit
    # zero-sale days, not a silent hole the engines would read as continuous demand.
    merged, _ = fill_daily_gaps(merged)
    prev["df"] = merged
    prev["date_shift_days"] = 0        # merged frame is on true dates; _reanchor fixes it
    return int(len(merged) - len(old)), overlap


def _ingest(df_raw, filename: str, stock_override: dict | None = None,
            cost_override: dict | None = None, append: bool = False,
            auto_backtest: bool = True, reanchor: bool = True):
    """Shared ingestion for ANY raw sales DataFrame — an uploaded file OR a live source
    like Shopify. Normalizes/dedups, optionally re-anchors, builds the per-SKU catalog,
    refits, and returns the same payload the upload endpoint always has. `stock_override` /
    `cost_override` (e.g. live Shopify inventory + unit costs) supply current values per
    SKU when the data carries no stock/cost columns — used only to prefill the dashboard.

    `reanchor` — TRUE for uploaded files, FALSE for a live source. See the block below;
    the short version is that a spreadsheet has an unknown vintage and a live feed does
    not, so shifting the one is a fix and shifting the other is corruption."""
    df_raw.columns = [c.strip().lower().replace(" ", "_") for c in df_raw.columns]
    if not {"date", "units_sold"}.issubset(df_raw.columns):
        raise HTTPException(400, "Missing required columns 'Date' and 'Units_Sold'. "
                                 "Optional: SKU, SKU_Name, Category (or other attributes), Price, On_Promotion, Units_In_Stock.")
    has_sku = "sku" in df_raw.columns
    try:
        df_raw["date"] = pd.to_datetime(df_raw["date"], errors="coerce")
        df_raw["units_sold"] = pd.to_numeric(df_raw["units_sold"], errors="coerce")
    except Exception as e:
        raise HTTPException(400, f"Could not parse data: {e}")
    # ── Data-quality accounting (reported back to the user) ──
    rows_in        = len(df_raw)
    bad_dates      = int(df_raw["date"].isna().sum())
    missing_sales  = int(df_raw["units_sold"].isna().sum())     # blanks → treated as 0
    df_raw["units_sold"] = df_raw["units_sold"].fillna(0).clip(lower=0)
    for c in ("price", "on_promotion", "units_in_stock"):
        if c in df_raw.columns:
            df_raw[c] = pd.to_numeric(df_raw[c], errors="coerce")
    # A Cost / Unit_Cost column seeds the dashboard's unit-cost field (like stock),
    # so margins/protection tiers work without hand-entering costs per SKU.
    cost_col = next((c for c in ("cost", "unit_cost") if c in df_raw.columns), None)
    if cost_col:
        df_raw[cost_col] = pd.to_numeric(df_raw[cost_col], errors="coerce")
    if "on_promotion" in df_raw.columns:
        df_raw["on_promotion"] = df_raw["on_promotion"].fillna(0).clip(0, 1)
    if "units_in_stock" in df_raw.columns:
        df_raw["units_in_stock"] = df_raw["units_in_stock"].fillna(1).clip(lower=0)
    df_raw = df_raw.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    # A bundle sale is one line item that consumed several products. Without pushing that
    # demand down, every component under-forecasts while its stock quietly drains — and
    # the bundle itself looks fine, which is what makes it hard to spot.
    if _bundle_map and has_sku:
        _rows = df_raw.to_dict("records")
        for _r in _rows:
            _r["date"] = str(_r["date"])[:10]
        _rows, _brep = _BUN.attribute(_rows, _bundle_map)
        if _brep.get("addedUnits"):
            df_raw = pd.DataFrame(_rows)
            df_raw["date"] = pd.to_datetime(df_raw["date"], errors="coerce")
            df_raw = df_raw.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    # ── Collapse duplicate dates so nothing is ever double-counted ───────────
    # 1) Drop fully-identical rows — this fixes accidental copies AND the worst
    #    case where a whole file was duplicated (every row repeats → collapses
    #    back to the single correct copy). No information is lost.
    # 2) Any date that STILL repeats (same day, different numbers = same-day
    #    transactions) is summed into one daily total; stock/price take the last
    #    value of the day, on-promotion the max. Guarantees exactly one row/day/SKU.
    key = ["sku", "date"] if has_sku else ["date"]
    df_raw = df_raw.drop_duplicates()
    dups_remaining = int(df_raw.duplicated(subset=key).sum())
    if dups_remaining:
        agg = {}
        for c in df_raw.columns:
            if c in key:
                continue
            agg[c] = "sum" if c == "units_sold" else ("max" if c == "on_promotion" else "last")
        df_raw = df_raw.groupby(key, as_index=False).agg(agg).sort_values("date").reset_index(drop=True)
    duplicate_rows_merged = rows_in - bad_dates - len(df_raw)
    data_quality = {
        "rowsIn": rows_in, "rowsUsed": len(df_raw),
        "badDatesDropped": bad_dates, "missingSalesFilledZero": missing_sales,
        "duplicateRowsMerged": int(max(duplicate_rows_merged, 0)),
    }

    # ── Re-anchor the sheet's timeline to "yesterday" — UPLOADS ONLY ─────────
    # Treat the most recent row in the upload as if it were yesterday, regardless
    # of its actual calendar date. This shifts every row by one shared offset so
    # the forecast starts cleanly from today with no dead gap between the last
    # data point and the real calendar date. Without this, re-uploading the same
    # sheet days later leaves a growing stretch the tool reads as "nothing sold."
    # A single global offset (from the whole sheet's max date) preserves the
    # relative spacing between rows and between SKUs, which the pooling relies on.
    #
    # A LIVE SOURCE IS THE OPPOSITE CASE and must never be shifted. A spreadsheet has
    # an unknown vintage — exported last Tuesday, uploaded today — so anchoring it is a
    # repair. Shopify's dates are the real ones, and a gap since the last order is a
    # fact about the store worth seeing, not an artefact to paper over. Worse, the
    # offset is recomputed on every ingest, so a live feed would have its whole calendar
    # nudged on each sync, by a different amount each time, while the forecast log went
    # on labelling its weeks with the true clock. The two records would slowly disagree
    # about what day it is.
    date_shift_days = 0
    if reanchor and len(df_raw):
        latest = df_raw["date"].max().normalize()
        offset = (today() - pd.Timedelta(days=1)) - latest
        date_shift_days = int(offset.days)
        if date_shift_days != 0:
            df_raw["date"] = df_raw["date"] + offset

    # Attribute columns = anything not reserved (Category, Brand, Size, …) → relatedness signal.
    attr_cols = [c for c in df_raw.columns if c not in R.RESERVED_COLS]

    if not has_sku:
        sid = filename.replace(".xlsx", "").replace(".xls", "").replace(".csv", "").upper().replace(" ", "-")
        groups_iter = {sid: (df_raw, sid)}
    else:
        if "sku_name" not in df_raw.columns:
            df_raw["sku_name"] = df_raw["sku"]
        groups_iter = {str(s): (g, str(g["sku_name"].iloc[0])) for s, g in df_raw.groupby("sku")}

    # "Replace" has to actually replace. This used to only upsert per SKU, so every SKU
    # from a previous upload survived every subsequent one — and got scored alongside the
    # new file's products in the backtest.
    if not append:
        _catalog.clear()
        _sku_cache.clear()
        _sheet_costs.clear()

    loaded, errors = [], []
    last_stock = {}   # sid -> units_in_stock on the most recent dated row (<= today)
    last_cost = {}    # sid -> most recent per-unit cost from a Cost/Unit_Cost column
    missing_days_filled = 0
    rows_appended = overlap_rows = 0      # append-mode accounting, reported back
    added_skus, extended = [], []
    for sid, (grp, sname) in groups_iter.items():
        keep = ["date", "units_sold"] + [c for c in ("price", "on_promotion", "units_in_stock") if c in grp.columns]
        dfc = grp[keep].rename(columns={"date": "ds", "units_sold": "y"}).copy()
        dfc["ds"] = pd.to_datetime(dfc["ds"])
        # Sheets that only carry a row per SALE day would otherwise hide every
        # zero-sale day from the model (over-forecasting + broken intermittency
        # detection). Reindex to a continuous daily calendar, same as Shopify.
        dfc, n_filled = fill_daily_gaps(dfc)
        missing_days_filled += n_filled
        # Allow brand-new products in (as few as 2 rows). Thin SKUs are handled
        # honestly downstream: < 7 own-days → "establishing baseline" (forecast held),
        # and the eligibility/abstain tiers label anything still too thin to forecast.
        # A 30-row floor here would silently drop every new product on upload.
        if len(dfc) < 2:
            errors.append(f"{sid}: fewer than 2 rows (has {len(dfc)}), skipped."); continue
        # Carry the latest known on-hand stock forward so the UI can pre-fill it.
        # grp is already sorted by date; take the most recent row at or before today.
        if "units_in_stock" in grp.columns:
            past = grp[grp["date"] <= today()]
            ss = pd.to_numeric((past if not past.empty else grp)["units_in_stock"], errors="coerce").dropna()
            if len(ss) > 0:
                last_stock[sid] = int(ss.iloc[-1])
        # Latest known per-unit cost (same pattern as stock) → prefills the
        # Scorecard's unit-cost field so margin/protection economics light up.
        if cost_col:
            past = grp[grp["date"] <= today()]
            cs = pd.to_numeric((past if not past.empty else grp)[cost_col], errors="coerce").dropna()
            cs = cs[cs > 0]
            if len(cs) > 0:
                last_cost[sid] = round(float(cs.iloc[-1]), 2)
        attrs = {c: (grp[c].dropna().iloc[0] if grp[c].notna().any() else None) for c in attr_cols}
        if append and sid in _catalog:
            # Extend, don't replace. Keep the existing events and any attributes the new
            # sheet doesn't carry; a partial re-export shouldn't wipe a product's setup.
            prev = _catalog[sid]
            added, overlap = _merge_sku_history(sid, dfc, date_shift_days)
            rows_appended += added
            overlap_rows += overlap
            if added or overlap:
                extended.append(sid)
            prev["attrs"] = {**(prev.get("attrs") or {}),
                             **{k: v for k, v in attrs.items() if v is not None}}
            prev["sku_name"] = sname or prev.get("sku_name")
            prev["mode"] = "uploaded"
            prev["filename"] = filename
            # Keep every file that contributed rows, in order. `sources[0]` is the file
            # that introduced the product, which is how the backtest sections its report.
            srcs = list(prev.get("sources") or ([prev["filename"]] if prev.get("filename") else []))
            if filename not in srcs:
                srcs.append(filename)
            prev["sources"] = srcs
        else:
            if append:
                added_skus.append(sid)
            _catalog[sid] = {"df": dfc, "attrs": attrs, "sku_name": sname,
                             "mode": "uploaded", "filename": filename, "events": [],
                             "sources": [filename], "date_shift_days": date_shift_days,
                             # Kept separate from `mode`, which the dashboard reads to decide
                             # whether "Clear all data" is offered. This flag is only about
                             # whether the dates may be moved.
                             "live_source": (not reanchor)}

    if stock_override:                       # live inventory (Shopify) → prefill current stock
        for sid in groups_iter:
            if last_stock.get(sid) is None and stock_override.get(sid) is not None:
                last_stock[sid] = int(stock_override[sid])
    if cost_override:                        # live unit costs (Shopify) → prefill unit cost
        for sid in groups_iter:
            if last_cost.get(sid) is None and cost_override.get(sid) is not None:
                last_cost[sid] = round(float(cost_override[sid]), 2)

    if not any(s in _catalog for s in groups_iter):
        raise HTTPException(400, "No valid SKUs could be loaded. " + " ".join(errors))

    data_quality["missingDaysFilled"] = int(missing_days_filled)

    # Appending changes everyone's timeline, so re-anchor the whole catalog with one
    # shared offset — otherwise SKUs the new file didn't touch drift relative to the
    # ones it did, and the pooled seasonal comparison silently compares misaligned dates.
    if append:
        date_shift_days = _reanchor_catalog()   # no-op once anything in the catalog is live
        data_quality["rowsAppended"] = int(rows_appended)
        data_quality["overlapRowsReplaced"] = int(overlap_rows)
        data_quality["skusAdded"] = added_skus
        data_quality["skusExtended"] = extended

    if not append:
        _switch_state["events"] = []   # a fresh upload re-baselines the model-switch banner (no stale switches)
        _save_switches(_switch_state)
    _rebuild(today())   # relatedness needs the whole catalog, so fit after all are loaded
    for sid in groups_iter:                                  # report any SKU that failed to model
        if sid in _last_rebuild_errors:
            errors.append(f"{sid}: could not be modeled and was skipped ({_last_rebuild_errors[sid]}).")

    for sid in groups_iter:
        if sid not in _sku_cache:
            continue
        e = _sku_cache[sid]; dt = e["df_train"]
        loaded.append({"id": sid, "name": e["sku_name"], "rows": len(dt),
                       "stockoutRowsDropped": e["stockout_rows_dropped"],
        # What the censoring correction did, so an adjusted forecast is never silent.
        "seasonalityApplied": (lambda x: None if not x or not x.get("text") else x)(
            e.get("seasonality_applied")),
        "uplift": (lambda u: None if not u or not u.get("text") else u)(e.get("uplift")),
        "promoHandling": (lambda pr: None if not pr or not _PROMO.explain(pr) else
                          {**pr, "text": _PROMO.explain(pr)})(e.get("promo_handling")),
        "censoring": (lambda c: None if not c or not (c.get("partial") or c.get("capped") or c.get("unavailable"))
                      else {**c, "text": _CEN.explain({
                          "days": c.get("days", 0), "capped": c.get("capped", 0),
                          "partial": c.get("partial", 0), "unavailable": c.get("unavailable", 0),
                          "censoredDays": c.get("capped", 0) + c.get("partial", 0) + c.get("unavailable", 0),
                          "hiddenUnits": c.get("addedUnits", 0),
                          "understatedPct": (round(c["addedUnits"] / c["observedUnits"] * 100, 1)
                                             if c.get("observedUnits") else None),
                      })})(e.get("censoring")), "hasPrice": e["has_price"],
                       "priceVaried": e["price_varied"], "route": e["route"], "winningModel": e["winning_model"],
                       "lastKnownStock": last_stock.get(sid), "lastKnownCost": last_cost.get(sid),
                       "dateRange": {"start": dt["ds"].min().strftime("%B %d, %Y"), "end": dt["ds"].max().strftime("%B %d, %Y")}})
    # Start the measured recommendation straight away. Nobody should have to know the
    # Backtest tab exists to get a protection level grounded in their own history — the
    # cost curve is what you see meanwhile, and the UI labels it as provisional.
    # Costs come from the sheet's Cost column if it has one; the dashboard re-runs later
    # with Scorecard costs if the user enters better ones.
    _sheet_costs.update({str(sid): float(c) for sid, c in last_cost.items()})

    # A live feed syncs constantly, and a full replay per sync is both untenable and
    # almost always pointless: cutoffs sit `step` days apart, so on 27 days out of 28 no
    # new window has matured and the run is guaranteed to reach the same answer it
    # reached yesterday. Uploads keep the original behaviour — a sheet arrives once, and
    # nobody should have to know the Backtest tab exists to get a measured protection
    # level. A live source gets one cold-start run when nothing has been measured yet,
    # and after that waits for a review to be triggered deliberately.
    live_source = not reanchor
    backtest_started = False
    if live_source and _backtest_tier_cache:
        # Skipping the run is only safe while the measurements still describe THIS data.
        if _backtest_fingerprint and _BTSTORE.fingerprint(_catalog) == _backtest_fingerprint:
            print("Live sync — data unchanged for testing purposes; automatic backtest skipped.")
        else:
            _drop_backtest_state("the sync changed the sales history they were measured on")
    elif auto_backtest and _catalog:
        backtest_started = _run_backtest_job(
            {"horizon": 44, "n_cutoffs": "auto", "step": 28, "lead": 14, "coverage": 30,
             # Honour the rate the user set BEFORE uploading — this used to hardcode 25%,
             # so a client who'd entered their real figure got a run against the default
             # and had to trigger another one.
             "service_pct": 95, "min_train": 120, "holding_pct": _session_holding_pct},
            _effective_sku_costs(None) or {},
            "upload")

    _persist_catalog()      # the data is now the app's; a restart re-fits it rather than asking for it again
    return {"success": True, "filename": filename, "loadedSkus": loaded, "appended": bool(append),
            "backtestStarted": backtest_started,
            "errors": errors, "dateShiftDays": date_shift_days, "dataQuality": data_quality}


@app.post("/api/upload/shopify")
def upload_shopify(payload: dict = Body(default={})):
    """Pull live sales from a Shopify store and load them into the dashboard exactly as if a
    sheet had been uploaded. Body: { shop, token, apiVersion?, days? }. Read-only token with
    scope read_orders (read_all_orders for >60 days of history)."""
    import shopify_source as SH
    try:
        raw = SH.fetch_sales(shop=payload.get("shop"), token=payload.get("token"),
                             api_version=payload.get("apiVersion"), days=payload.get("days"))
    except SH.ShopifyError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Shopify fetch failed: {e}")
    # Category (product_type) + current inventory via GraphQL — best-effort: if the token
    # lacks read_products or the call fails, the sales still import (just without them).
    stock_map = cost_map = None
    try:
        meta = SH.fetch_catalog_meta(shop=payload.get("shop"), token=payload.get("token"), api_version=payload.get("apiVersion"))
        if meta:
            raw["category"] = raw["sku"].map(lambda s: (meta.get(s) or {}).get("category"))
            stock_map = {s: m["stock"] for s, m in meta.items() if m.get("stock") is not None}
            cost_map = {s: m["cost"] for s, m in meta.items() if m.get("cost") is not None}
    except Exception:
        pass
    shop = (payload.get("shop") or "store").replace(".myshopify.com", "").strip()
    with _state_lock:
        return _ingest(raw, f"Shopify · {shop}", stock_override=stock_map, cost_override=cost_map,
                       reanchor=False)   # live dates are the real dates


@app.post("/api/reset")
def reset_all(payload: dict = Body(default=None)):
    """Clear everything back to an empty app.

    `{"scope": "everything"}` additionally purges the two records that outlive a
    catalog on purpose: the forecast log (weekly sealed predictions and their grades)
    and the stock log (hourly inventory readings). Those normally survive a re-upload
    BECAUSE they are a record of what was predicted and what was on the shelf at the
    time — history the catalog can't reconstruct. Wiping them is the right thing before
    a clean run against a real store, and the wrong thing by accident, so it is opt-in.

    NOTE: the server is only half the app. Stock levels, unit costs, suppliers, purchase
    orders and folders live in the browser, and the caller has to clear those itself —
    see handleResetEverything in App.jsx."""
    scope = (payload or {}).get("scope")
    everything = scope == "everything"
    global _switch_state
    with _state_lock:
        _switch_state = {"lastRoute": {}, "events": []}   # clean slate for the switch banner
        _save_switches(_switch_state)
        _STORE.clear()      # before warmup(), which would otherwise restore what we just cleared
        _BTSTORE.clear()
        globals()["_backtest_fingerprint"] = None
        purged = {}
        if everything:
            try:
                _flog.purge(); purged["forecastLog"] = True
            except Exception as e:                           # noqa: BLE001
                purged["forecastLog"] = f"failed: {e}"
            try:
                _slog.purge(); purged["stockLog"] = True
            except Exception as e:                           # noqa: BLE001
                purged["stockLog"] = f"failed: {e}"
        warmup()
    return {"success": True, "cleared": True, "scope": scope or "catalog", "purged": purged}


# ═════════════════════════════════════════════════════════════════════════════
#  CATEGORIZATION / RELATEDNESS  (powers the "dump product info" panel)
#  Attributes themselves are extracted client-side by Groq from pasted product
#  info; here we store them and compute which products are similar to which.
# ═════════════════════════════════════════════════════════════════════════════
def _to_jsonable(obj):
    """Recursively convert numpy/pandas scalar types to native Python so FastAPI's
    JSON encoder can serialize the response. Seasonal month numbers and cohesion
    stats arrive as numpy.int64/float64/ndarray, which jsonable_encoder can't handle
    (it raised "'numpy.int64' object is not iterable") — that 500'd every save."""
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_to_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, np.generic):           # numpy scalar (int64, float64, bool_, …)
        v = obj.item()
        return None if (isinstance(v, float) and (np.isnan(v) or np.isinf(v))) else v
    if isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
        return None
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if obj is pd.NaT:
        return None
    return obj


def _compute_groups():
    pool_catalog = {sid: c for sid, c in _catalog.items() if not _exclude_from_pooling(c)}
    cols = R.detect_group_columns(pool_catalog)
    base_groups = R.group_catalog(pool_catalog, cols)
    groups, group_meta = R.adaptive_group_catalog(pool_catalog, cols)
    clusters = R.cluster_catalog(groups, pool_catalog)
    # Map each established member → the id-set of its behavioural cluster, and score how
    # tightly each cluster's seasonal patterns line up (avg pairwise correlation).
    # Catalog-wide baseline shape = the average seasonal fingerprint across all products.
    # Subtracting it lets cohesion measure DISTINCTIVE co-movement (beyond generic seasonality).
    _all_sigs = [s for s in (R._seasonal_signature(pool_catalog[sid].get("df")) for sid in pool_catalog) if s is not None]
    _baseline = np.mean(_all_sigs, axis=0) if _all_sigs else None
    member_key = {}; cohesion_by_key = {}
    for g, cls in clusters.items():
        for cl in cls:
            key = tuple(sorted(cl))
            cohesion_by_key[key] = R.cluster_cohesion(cl, pool_catalog, _baseline)
            for m in cl:
                member_key[m] = key

    def _nearest_pool_link(sid, group_key, own_cluster_key=None):
        """Best raw seasonal link from `sid` to any eligible multi-SKU pool in
        the same category. Used to explain near misses like A-B = 75%, A-C = 38%:
        C stays out of the pool, but the UI can show the strongest link that
        failed the 40% threshold.
        """
        if group_key is None or sid not in pool_catalog:
            return None
        sig = R._seasonal_signature(pool_catalog[sid].get("df"))
        if sig is None:
            return None
        best = None
        for cl in clusters.get(group_key, []):
            key = tuple(sorted(cl))
            if own_cluster_key is not None and key == own_cluster_key:
                continue
            if len(cl) < R.MIN_RELATIVES:
                continue
            for other in cl:
                if other == sid or other not in pool_catalog:
                    continue
                osig = R._seasonal_signature(pool_catalog[other].get("df"))
                score = R._shape_corr(sig, osig)
                if best is None or score > best["score"]:
                    best = {
                        "score": round(float(score), 3),
                        "threshold": R.SHAPE_THRESHOLD,
                        "skuId": other,
                        "skuName": pool_catalog[other].get("sku_name") or other,
                        "cluster": list(key),
                    }
        return best

    def _nearest_category_link(sid, group_key):
        """Best raw seasonal link to any same-category peer, even when the
        category has no eligible pooled group. This explains two-product
        categories that failed to pool: A-B may be 32%, below the 40% threshold,
        but users still need to see that number.
        """
        if group_key is None or sid not in pool_catalog:
            return None
        sig = R._seasonal_signature(pool_catalog[sid].get("df"))
        if sig is None:
            return None
        best = None
        for other, og in base_groups.items():
            if other == sid or og != group_key or other not in pool_catalog:
                continue
            osig = R._seasonal_signature(pool_catalog[other].get("df"))
            if osig is None:
                continue
            score = R._shape_corr(sig, osig)
            if best is None or score > best["score"]:
                best = {
                    "score": round(float(score), 3),
                    "threshold": R.SHAPE_THRESHOLD,
                    "skuId": other,
                    "skuName": pool_catalog[other].get("sku_name") or other,
                }
        return best

    def _days_hist(sid):
        e = _sku_cache.get(sid) or {}
        dft = e.get("df_train")
        try:
            return int((dft["ds"].max() - dft["ds"].min()).days) if dft is not None and len(dft) > 1 else 0
        except Exception:
            return None
    rows = []
    for sid, c in pool_catalog.items():
        entry = _sku_cache.get(sid, {})
        seas = ((entry.get("explain") or {}).get("seasonality") or {})
        g = groups.get(sid)
        bg = base_groups.get(sid)
        info = entry.get("cluster_info")
        if sid in member_key:                       # established → its own cluster
            ckey, basis = member_key[sid], None
        elif info and info.get("chosen"):           # new SKU → the cluster it was matched to
            ckey, basis = tuple(sorted(info["chosen"])), info.get("basis")
        else:
            ckey, basis = None, None
        if ckey:                                    # similar = the cluster peers actually used
            similar = [{"skuId": o, "skuName": pool_catalog[o].get("sku_name") or o}
                       for o in ckey if o != sid and o in pool_catalog]
        else:
            similar = []
        if not similar:                             # no cluster, or a SINGLETON cluster (a SKU
            # that behaves unlike its category-mates) → still show the category-mates so the
            # Categorize panel can display what it was compared against.
            similar = [{"skuId": o, "skuName": pool_catalog[o].get("sku_name") or o}
                       for o, og in groups.items() if o != sid and g is not None and og == g]
        rows.append({
            "skuId": sid, "skuName": c.get("sku_name") or sid,
            "attributes": dict(c.get("attrs") or {}),
            "group": g, "baseGroup": bg, "groupInfo": group_meta.get(sid),
            "groupSize": len(similar) + (1 if g is not None else 0),
            "cluster": list(ckey) if ckey else None, "clusterBasis": basis,
            "clusterCohesion": (cohesion_by_key.get(ckey) if ckey else None),
            "nearestPoolLink": _nearest_pool_link(sid, g, ckey),
            "nearestCategoryLink": _nearest_category_link(sid, bg),
            "daysHistory": _days_hist(sid),
            "route": entry.get("route"),
            "winningModel": entry.get("winning_model"),
            "usesGlobal": entry.get("route") == "global",
            "usesYearlyPool": bool(seas.get("blended")),
            "yearlyPoolWeight": seas.get("blendWeight"),
            "yearlyPoolSize": seas.get("poolSize"),
            "similar": similar,
        })
    return _to_jsonable({"groupColumns": cols, "skus": rows})


@app.get("/api/groups")
def get_groups():
    """Current relatedness view: every SKU, its detected attributes, its group,
    and the products considered similar to it."""
    try:
        return _compute_groups()
    except Exception as ex:
        import traceback; traceback.print_exc()
        return {"groupColumns": [], "skus": [], "regroupError": f"{type(ex).__name__}: {ex}"}


@app.post("/api/attributes")
def set_attributes(payload: dict = Body(...)):
    """Merge in attributes (e.g. Groq-extracted {category, brand, size, …}) for one
    or more SKUs, re-group + re-route the whole catalog, and return the new groups.
    Body: { "skus": { "<sku_id>": {"category": "...", "brand": "...", ...}, ... } }"""
    skus = payload.get("skus", {}) or {}
    updated = 0
    with _state_lock:
        return _set_attributes_locked(skus, updated)


def _set_attributes_locked(skus, updated):
    for sid, attrs in skus.items():
        if sid in _catalog:
            merged = dict(_catalog[sid].get("attrs") or {})
            sku_label = f"{sid} {_catalog[sid].get('sku_name') or ''}".lower()
            force_no_category = any(marker in sku_label for marker in NO_CATEGORY_MARKERS)
            changed = False
            for k, v in (attrs or {}).items():
                if force_no_category and str(k).lower() == "category":
                    v = None
                if v in (None, ""):
                    if k in merged:
                        merged.pop(k, None)
                        changed = True
                elif merged.get(k) != v:
                    merged[k] = v
                    changed = True
            _catalog[sid]["attrs"] = merged
            if changed:
                updated += 1
    regroup_error = None
    if updated:
        _persist_catalog()      # attributes live in the catalog, so they belong in the snapshot
        try:
            _rebuild(today())   # relatedness/routing may change for the whole catalog
        except Exception as ex:
            import traceback; traceback.print_exc()
            regroup_error = f"{type(ex).__name__}: {ex}"
    # The attributes are already merged into _catalog above, so a failure while
    # re-grouping/re-routing must NOT fail the save — the user's categories are kept
    # and the error is surfaced (and logged) instead of taking down the whole "Apply".
    try:
        groups = _compute_groups()
    except Exception as ex:
        import traceback; traceback.print_exc()
        regroup_error = regroup_error or f"{type(ex).__name__}: {ex}"
        groups = {"groupColumns": [], "skus": []}
    return {"success": True, "updated": updated, "regroupError": regroup_error, **groups}


# ═════════════════════════════════════════════════════════════════════════════
#  Shared eligibility / reliability / strategy helpers  (unchanged behaviour)
# ═════════════════════════════════════════════════════════════════════════════
def get_reliability_flag(fd, hd):
    r = fd / hd if hd > 0 else 1.0
    if r < 0.25: return "GREEN", None
    if r < 0.50: return "YELLOW", (f"The forecast window ({fd} days) is more than 25% of your sales history "
                                   f"({hd} days). Treat later months as directional.")
    if r < 0.75: return "ORANGE", (f"The forecast window ({fd} days) is more than 50% of your sales history "
                                   f"({hd} days). Confidence in later months is limited.")
    return "RED", (f"The forecast window ({fd} days) approaches/exceeds your history ({hd} days). "
                   f"These estimates are highly speculative.")


def get_demand_volatility(cv):
    if cv < 0.30: return "GREEN", "lean", f"stable demand (CV {cv:.2f})", 1.282
    if cv < 0.60: return "YELLOW", "balanced", f"variable demand (CV {cv:.2f})", 1.645
    return "ORANGE", "conservative", f"volatile demand (CV {cv:.2f})", 2.326


# ── Protection level (service level) ─────────────────────────────────────────
# The buffer = z × σ × √lead_time. σ already captures how erratic demand is; z is
# the SEPARATE business choice of how hard to protect against a stockout. We set z
# from the item's UNIT ECONOMICS, not its volatility: the more margin a lost sale
# forfeits, the more it's worth holding extra to avoid one. Sales velocity is left
# to flow through σ (a faster seller already gets a bigger buffer in units).
PROTECTION_TIERS = [
    {"key": "light",    "label": "Light",     "pct": 90,   "z": 1.282},
    {"key": "standard", "label": "Standard",  "pct": 95,   "z": 1.645},
    {"key": "high",     "label": "High",      "pct": 98,   "z": 2.054},
    # `key` is PERSISTED — src/App.jsx writes skuParams[sku].protection to localStorage
    # and replays it on every forecast fetch. So "max" must keep meaning 99, forever.
    # Only its LABEL moved (it is no longer the maximum). Repointing this key at 99.5
    # would silently re-protect every user who had already chosen Maximum.
    {"key": "max",      "label": "Very high", "pct": 99,   "z": 2.326},
    {"key": "ultra",    "label": "Maximum",   "pct": 99.5, "z": 2.576},
]
_TIER_BY_KEY = {t["key"]: t for t in PROTECTION_TIERS}
_TIER_KEY_BY_PCT = {t["pct"]: t["key"] for t in PROTECTION_TIERS}
_TIER_PCTS = [t["pct"] for t in PROTECTION_TIERS]


def _svc(v, default=95):
    """A service level as a float, snapped to a tier we actually have a z for.

    Tiers are no longer all integers. The request handlers used to coerce `service`
    with int(), which turned 99.5 into 99 — running the whole backtest at the wrong
    buffer and filing the result under the wrong tier. Snapping also closes an older
    hole: an unrecognised value reached backtest.Z.get(pct, 1.6449) and quietly got
    the 95% z while still being labelled whatever was asked for.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return min(_TIER_PCTS, key=lambda p: abs(p - f))


_BT_TIERS_CHECKED = False


def _bt():
    """Import the backtest module, verifying its tier table still matches ours.

    backtest.Z and PROTECTION_TIERS are two hand-kept copies of the same ladder, in
    two files, and nothing links them. Adding a level to one and not the other does
    not crash: you get a tier the UI can't offer, or a tier with no z that silently
    falls back to 1.6449. Check once per process and fail loudly instead of serving a
    run computed at the wrong buffer.
    """
    global _BT_TIERS_CHECKED
    import backtest as BT       # the one real import; every caller goes through here
    if not _BT_TIERS_CHECKED:
        ours   = {t["pct"]: round(float(t["z"]), 3) for t in PROTECTION_TIERS}
        theirs = {p: round(float(z), 3) for p, z in BT.Z.items()}
        if ours != theirs:
            raise RuntimeError(
                "Protection tier tables disagree. main.PROTECTION_TIERS says "
                f"{sorted(ours.items())}, backtest.Z says {sorted(theirs.items())}. "
                "Both must list the same levels with the same z.")
        _BT_TIERS_CHECKED = True
    return BT


DEFAULT_HOLDING_ANNUAL = 0.25
ECONOMIC_TIER_MIN_DAYS = 180
ECONOMIC_TIER_MIN_SALES = 100
ECONOMIC_TIER_BLOCKED_DEMAND = {"intermittent", "lumpy", "no_demand"}
# Must match backtest.MIN_WINDOWS_REPORTABLE. It was 3 while the report refused to show
# per-SKU figures under 4 windows — so a product's protection level could be set from
# evidence the app itself considered too thin to print. (Pinned by a test.)
BACKTEST_TIER_MIN_WINDOWS = 4
# Shortest history the backtest can score at all (min_train 120 + horizon 44). Below it
# no measured recommendation is possible for that product, however long you wait — which
# is the one case where the estimate is genuinely the best available answer.
BACKTEST_MIN_TESTABLE_DAYS = 164


def _normal_pdf(z):
    return math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)


def _normal_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


def _expected_shortage_units(spread, z):
    """Expected positive tail beyond z*spread for a zero-mean normal error."""
    spread = float(spread or 0.0)
    if spread <= 0:
        return 0.0
    return spread * max(0.0, _normal_pdf(z) - z * (1.0 - _normal_cdf(z)))


def recommend_protection(margin_pct, *, has_price=None, has_cost=None):
    """Recommended protection tier (key, reason) from gross margin %. Falls back to
    Standard when margin isn't known.

    Margin needs BOTH a price and a cost, so when it's missing the reason must say which
    one is actually absent. This used to blame the cost unconditionally — a product with
    a perfectly good cost but no price column reported "no unit cost on file", sending
    you to the Scorecard to fix something that was never wrong."""
    if margin_pct is None:
        if has_cost and not has_price:
            what = ("There's a unit cost on file, but no selling price in the data, so margin "
                    "can't be worked out")
            fix = "Add a Price column to the sales file (or pull prices from Shopify)"
        elif has_price and not has_cost:
            what = "No unit cost on file, so protection can't be tuned to this item's economics yet"
            fix = "Add a unit cost on the Scorecard"
        elif has_price is False and has_cost is False:
            what = "Neither a selling price nor a unit cost is available, so margin can't be worked out"
            fix = "Add a Price column to the file and a unit cost on the Scorecard"
        else:
            what = "Margin isn't known for this item yet"
            fix = "Add a unit cost on the Scorecard (and a Price column in the data)"
        return "standard", f"{what} — defaulting to Standard (95%). {fix} to tailor it."
    if   margin_pct < 15: key = "light"
    elif margin_pct < 30: key = "standard"
    elif margin_pct < 50: key = "high"
    else:                 key = "max"
    t = _TIER_BY_KEY[key]
    feel = "very costly" if margin_pct >= 50 else "costly" if margin_pct >= 30 else \
           "moderately costly" if margin_pct >= 15 else "cheap"
    return key, (f"This item keeps about {margin_pct:.0f}% margin, so a lost sale is {feel} to forgo. "
                 f"{t['label']} protection ({t['pct']}%) is recommended — it weighs that stockout cost "
                 f"against the cost of holding extra stock.")


def recommend_economic_protection(*, price, unit_cost, fees, margin_pct, demand_spread,
                                  coverage_days, days_history, total_sales, demand_class,
                                  holding_annual=DEFAULT_HOLDING_ANNUAL, backtest_rec=None,
                                  lead_time_days=None, sku_id=None):
    """Hybrid protection recommendation.

    Use a per-SKU cost curve when the SKU has enough regular history and real unit
    economics. Otherwise return the existing margin/default recommendation with an
    explicit source so the UI can explain why.
    """
    fallback_key, fallback_reason = recommend_protection(
        margin_pct,
        has_price=(price not in (None, "") and float(price or 0) > 0),
        has_cost=(unit_cost not in (None, "")))
    fallback_source = "default" if margin_pct is None else "margin"

    if backtest_rec:
        bt_lead = backtest_rec.get("lead")
        bt_cov = backtest_rec.get("coverage")
        lead_match = bt_lead is None or lead_time_days is None or int(bt_lead) == int(lead_time_days)
        cov_match = bt_cov is None or int(bt_cov) == int(coverage_days)
        drift = _economics_drifted(sku_id, unit_cost, fees, backtest_rec.get("holdingPct"))
        if drift:
            # Measured, but on economics that no longer hold. Fall through to the
            # estimate rather than present a figure for a product that has changed.
            lead_match = False
        if lead_match and cov_match and int(backtest_rec.get("windows") or 0) >= BACKTEST_TIER_MIN_WINDOWS:
            key = backtest_rec.get("key")
            tier = _TIER_BY_KEY.get(key)
            if tier:
                cost = backtest_rec.get("totalCostYr")
                cost_txt = f"about ${cost:,.0f}/yr" if isinstance(cost, (int, float)) else "the lowest tested cost"
                if backtest_rec.get("policy") == "uniform":
                    # Per-product tuning failed its out-of-sample test, so the whole
                    # catalog runs the winning single level — and the user deserves to
                    # know that's why this product isn't on its "own" tier.
                    reason = (f"Recommended from the last backtest: one shared level performed best. "
                              f"Per-product tuning was tried and did NOT hold up when scored on unseen "
                              f"weeks — the differences between products were mostly noise — so every "
                              f"product runs {tier['label']} protection ({tier['pct']}%), the level that "
                              f"won for the catalog as a whole across "
                              f"{int(backtest_rec.get('windows') or 0)} historical tests.")
                else:
                    reason = (f"Recommended from the last backtest: {tier['label']} protection "
                              f"({tier['pct']}%) was the cheapest tier for this SKU, {cost_txt}, "
                              f"across {int(backtest_rec.get('windows') or 0)} historical tests — and "
                              f"per-product tuning beat one-level-for-everything when scored on unseen weeks.")
                return key, reason, "backtest", backtest_rec

    if margin_pct is None or price in (None, "") or unit_cost in (None, ""):
        return fallback_key, fallback_reason, fallback_source, None
    price = float(price)
    unit_cost = float(unit_cost)
    fees = float(fees or 0.0)
    margin_unit = price - unit_cost - fees
    if price <= 0 or unit_cost <= 0:
        return fallback_key, fallback_reason, fallback_source, None
    if margin_unit <= 0:
        t = _TIER_BY_KEY["light"]
        reason = ("This item is priced at or below its unit cost after fees, so a stockout "
                  "doesn't forgo positive profit. Light protection is recommended until the "
                  "price, cost, or fees are fixed.")
        return "light", reason, "economics", {
            "basis": "known cost, non-positive margin",
            "holdingPct": round(holding_annual * 100),
            "marginUnit": round(margin_unit, 2),
            "tiers": [{"key": x["key"], "pct": x["pct"], "totalCost": None,
                       "stockoutCost": None, "holdingCost": None} for x in PROTECTION_TIERS],
            "best": "light",
        }
    if days_history < ECONOMIC_TIER_MIN_DAYS or total_sales < ECONOMIC_TIER_MIN_SALES:
        return fallback_key, (fallback_reason + " Cost-based tuning will switch on once this "
                              f"SKU has at least {ECONOMIC_TIER_MIN_DAYS} days and "
                              f"{ECONOMIC_TIER_MIN_SALES} sales of history."), fallback_source, None
    if demand_class in ECONOMIC_TIER_BLOCKED_DEMAND:
        return fallback_key, (fallback_reason + " Cost-based tuning is held back because this "
                              "SKU's demand is intermittent; the margin rule is more stable than "
                              "a thin per-item cost curve."), fallback_source, None
    spread = float(demand_spread or 0.0)
    if spread <= 0 or not np.isfinite(spread):
        return fallback_key, (fallback_reason + " Cost-based tuning is held back because the "
                              "lead-time demand uncertainty is not measurable yet."), fallback_source, None

    cycles_per_year = 365.0 / max(int(coverage_days or 1), 1)
    rows = []
    for tier in PROTECTION_TIERS:
        z = float(tier["z"])
        safety_units = z * spread
        shortage_units = _expected_shortage_units(spread, z)
        stockout_cost = shortage_units * cycles_per_year * margin_unit
        holding_cost = safety_units * unit_cost * holding_annual
        rows.append({
            "key": tier["key"], "label": tier["label"], "pct": tier["pct"],
            "safetyUnits": round(safety_units, 1),
            "shortUnitsYr": round(shortage_units * cycles_per_year, 1),
            "stockoutCost": round(stockout_cost, 2),
            "holdingCost": round(holding_cost, 2),
            "totalCost": round(stockout_cost + holding_cost, 2),
        })
    best = min(rows, key=lambda r: r["totalCost"])
    chosen = _TIER_BY_KEY[best["key"]]
    reason = (f"Recommended from this SKU's cost curve: {chosen['label']} protection "
              f"({chosen['pct']}%) has the lowest expected annual cost, about "
              f"${best['totalCost']:,.0f}/yr, balancing lost profit from stockouts "
              f"against buffer holding cost. Assumes {round(holding_annual * 100)}%/yr "
              "holding cost.")
    return best["key"], reason, "economics", {
        "basis": "per-SKU expected cost",
        "holdingPct": round(holding_annual * 100),
        "marginUnit": round(margin_unit, 2),
        "cyclesPerYear": round(cycles_per_year, 1),
        "leadTimeSpread": round(spread, 2),
        "tiers": rows,
        "best": best["key"],
    }


def get_eligibility_status(days, sales, demand_class=None, selling_days=None):
    selling_days = int(selling_days or 0)
    reliable_days = days >= 180
    reliable_repeats = selling_days >= 45
    reliable_units = sales >= 60
    weak_days = days >= 90
    weak_repeats = selling_days >= 15
    weak_units = sales >= 30
    # Intermittent/lumpy demand stays uncertain per period no matter how long the
    # history is, so it should NOT be labeled a "reliable" forecast.
    if demand_class in ("intermittent", "lumpy", "no_demand"):
        if (reliable_days and reliable_repeats) or (weak_days and weak_repeats):
            return "CONFLICTING", ("Demand is intermittent — many zero-sale days — so per-period forecasts stay "
                                   "uncertain even with this much history. Handled by a specialist intermittent-demand "
                                   "model; treat the figures as directional, not precise.")
        return "INSUFFICIENT", ("Demand is intermittent and history is thin — treat this as a rough placeholder "
                                "until more sales accumulate.")
    if reliable_days and reliable_repeats and reliable_units:
        return "FORECAST_ELIGIBLE", (
            f"Enough history and repeated sales for a reliable forecast "
            f"({days} days, {selling_days} selling days, {sales:,} units).")
    if weak_days and weak_repeats and weak_units:
        return "LOW_CONFIDENCE", (
            f"Clears the minimum bar ({days} days, {selling_days} selling days), but use as a "
            f"general guide rather than a precise plan.")
    if reliable_days and (not reliable_repeats or not weak_units):
        return "CONFLICTING", (
            f"Long history ({days} days) but only {selling_days} selling days and {sales:,} units. "
            f"This may remain a pooled or intermittent-style forecast for a long time unless sales become more regular.")
    if reliable_units and not weak_days:
        return "CONFLICTING", (f"Strong sales ({sales:,} units) but history is too short ({days} days) "
                                             f"to detect seasonality. Check back in {90 - days} days.")
    return "INSUFFICIENT", (f"Not enough data ({days} days, {sales} units). At least 90 days and 30 sales required.")


def get_price_change_warning(entry, events):
    pe = [e for e in events if e.get("type") in ("price_change_permanent", "price_change_temporary")]
    if not pe: return None, None
    wl = entry.get("price_warn_level")
    if wl == "no_column": return "warning", "No price data was uploaded; the model can't adjust for a price change."
    if wl == "never_changed":
        return "warning", f"This SKU only ever sold at ${entry.get('last_price'):.2f}; no price-change history to learn from."
    if wl == "thin_data":
        return "warning", entry.get("price_warn_msg_upload", "Price data is limited.")
    tiers, tmin, tmax = entry.get("price_tiers", []), entry.get("price_trained_min"), entry.get("price_trained_max")
    obs = {round(t["price"], 2) for t in tiers}
    nps = [float(e["new_price"]) for e in pe if e.get("new_price") is not None]
    if not nps: return None, None
    npx = nps[-1]
    if round(npx, 2) in obs: return None, None
    if tmin is not None and tmax is not None:
        if npx < tmin or npx > tmax:
            return "warning", f"${npx:.2f} is outside observed prices (${tmin:.2f}–${tmax:.2f})."
        return "info", f"${npx:.2f} wasn't directly observed; demand is estimated from the learned price relationship."
    return None, None


# Every forecast band is the 80% the models are fitted with. This used to map a
# per-product "strategy" onto 70/80/90; see the note in get_forecast for why that
# stopped. Kept as a single constant so the number lives in one place.
FORECAST_BAND_PCT = 80

# Order guardrail: below this much history, flag the order and suggest a cap.
GUARDRAIL_NEW_DAYS   = 90   # "thin history" threshold (≈ 3 months)
GUARDRAIL_COVER_DAYS = 42   # suggested first-order cap: ~6 weeks of cover


@app.get("/api/forecast")
def get_forecast(sku_id: str = Query(...), stock: int = Query(default=500, ge=0),
                 lead_time_days: int = Query(default=14, ge=1), coverage_days: int = Query(default=30, ge=1),
                 strategy: str = Query(default="balanced"), forecast_months: int = Query(default=1, ge=1, le=12),
                 units_on_order: int = Query(default=0, ge=0),
                 on_order_eta_days: int | None = Query(default=None, ge=0),
                 unit_cost: float | None = Query(default=None), fees: float = Query(default=0.0, ge=0),
                 protection: str | None = Query(default=None), tz: str | None = Query(default=None)):
    set_timezone(tz)   # keep "today" aligned to the user's local date
    if sku_id not in _sku_cache:
        raise HTTPException(404, f"SKU '{sku_id}' not found.")
    e = _sku_cache[sku_id]
    df_train, forecast, future_fc = e["df_train"], e["forecast"], e["future_fc"]
    events = e.get("events", [])
    if df_train is None or forecast is None or future_fc is None:
        raise HTTPException(503, "Model is not ready yet.")
    # Did the upload actually carry a stock column? If not, the stock figure (and
    # therefore the stockout/reorder math) is an assumed default, not real data.
    stock_data_available = "units_in_stock" in df_train.columns

    # ── One confidence level for every product ───────────────────────────────
    # The band used to move with demand volatility: steady products were shown a 70%
    # interval, volatile ones a 90%. That applied the same adjustment twice, because the
    # width ALREADY varies with volatility through sigma, and it made two products'
    # bands incomparable — they were drawn to different promises. Worse, it inverted the
    # reading: a steady product's tight-looking 70% band is a WEAKER claim (reality lands
    # outside it 3 weeks in 10) than a volatile product's wide 90% one.
    #
    # So the level is fixed at the 80% the models are actually fitted with, and the width
    # is left to sigma alone. A stable product gets a narrow band because its demand is
    # narrow, which is the whole of the story and the only part worth drawing.
    #
    # `strategy` is still accepted so older clients don't 422, but it no longer changes
    # the interval. How hard to protect against a stockout is a separate decision, made
    # from unit economics in PROTECTION_TIERS, and that one still varies per product.
    interval_width = FORECAST_BAND_PCT
    forecast = forecast.copy()
    last_actual = df_train["ds"].max()   # forecast region begins the day AFTER this
    future_fc = forecast[forecast["ds"] > last_actual].copy().reset_index(drop=True)

    # Dormancy guard: if the most recent usable (in-stock) sales day is far in the past,
    # the product has had no stock AND no sales for a long stretch — it's effectively
    # discontinued or chronically out of stock. Without this, the forecast anchors to that
    # ancient last-sale date and emits nonsense ("reorder overdue 1777 days ago", a buy
    # recommendation for a 5-years-dead item). We flag it and suppress the stale dates.
    INACTIVE_DAYS = 180
    inactive_days = max((today() - pd.Timestamp(last_actual)).days, 0)
    inactive = inactive_days > INACTIVE_DAYS
    inactive_msg = (
        f"No sales in {inactive_days:,} days (last sold {pd.Timestamp(last_actual):%b %Y}). This product "
        f"appears discontinued or has been out of stock long-term, so the forward forecast has been set "
        f"to 0 — its old sales pattern is years stale and not a reliable signal for today. If it's actually "
        f"still active, upload recent sales/stock and the forecast will pick back up."
    ) if inactive else None

    # Discontinued/dormant items: replaying the ancient seasonal pattern forward yields a
    # confident-but-wrong demand number (e.g. "368 units expected" for an item dead 4 years).
    # Collapse the entire forward projection to 0 — expected demand for a discontinued product
    # is 0 until it demonstrably starts selling again. Historical actuals are left intact, so
    # the chart still shows what it used to sell; only the go-forward forecast flattens to 0.
    if inactive:
        _fut = forecast["ds"] > last_actual
        forecast.loc[_fut, ["yhat", "yhat_lower", "yhat_upper"]] = 0.0
        for _c in ("yhat", "yhat_lower", "yhat_upper"):
            if _c in future_fc.columns:
                future_fc[_c] = 0.0

    days_hist = (df_train["ds"].max() - df_train["ds"].min()).days
    total_sales = int(df_train["y"].sum())
    selling_days = int((df_train["y"] > 0).sum())
    status, status_msg = get_eligibility_status(days_hist, total_sales, e.get("demand_class"), selling_days)
    tot_fc_days = forecast_months * 31
    rel_color, rel_msg = get_reliability_flag(tot_fc_days, days_hist)
    pc_level, pc_msg = get_price_change_warning(e, events)

    # New-SKU baseline gate: for the first week of a product's life there isn't enough of
    # its OWN history to forecast — and by design we do NOT borrow a brand-new SKU's volume
    # from pooled peers (the peer group may sell at a very different volume). So below the
    # floor we forecast nothing and just track inventory, regardless of category/relatives.
    # The forecast switches on automatically once a week of the SKU's own data is in.
    BASELINE_DAYS = 7
    own_days = int(df_train["ds"].dt.normalize().nunique())
    too_new = (not inactive) and own_days < BASELINE_DAYS
    obs_rate = (total_sales / own_days) if own_days > 0 else 0.0   # raw observed units/day so far
    obs_runway = int(stock / obs_rate) if (too_new and obs_rate > 0 and stock > 0) else None
    # Honest stock-out call from raw velocity, available from day one of sales. Even with no
    # forecast yet, if the observed pace will empty the shelf soon, we say so (and when).
    obs_stockout_ts = None
    if obs_runway is not None:
        _osd = today() + pd.Timedelta(days=obs_runway)
        obs_stockout_ts = int(pd.Timestamp(_osd).replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    too_new_msg = (
        f"Establishing baseline — {own_days} of {BASELINE_DAYS} days of sales. A product needs about a "
        f"week of its own history before a forecast means anything, so we're holding off rather than "
        f"guessing from a few days (or from other products that may sell at a different volume). "
        f"Inventory is still tracked below, and the forecast turns on automatically once a week of data is in."
    ) if too_new else None

    # Young-product tier: between the baseline week and ~a month of history the forecast IS
    # live, but it's built on only a few weeks of sales — so it should speak softly. We widen
    # the prediction band (more honest uncertainty) and flag it; the front end downgrades any
    # reorder alert from a hard red "OVERDUE" to an amber "provisional — heads-up" and the
    # order guardrail (< 90 days history) already caps the first-order size. This turns the
    # day-7 hand-off from a cliff (silent → confident red alarm) into a ramp.
    YOUNG_DAYS = 28
    young = (not inactive) and (not too_new) and own_days < YOUNG_DAYS
    young_msg = (
        f"Young product — {own_days} of {YOUNG_DAYS} days of history. The forecast is live but still "
        f"provisional: it's built on only a few weeks of sales, so reorder timing is a heads-up rather "
        f"than a hard deadline, and the recommended order is kept conservative until more history builds."
    ) if young else None
    if young:
        YOUNG_BAND = 1.4   # widen the forward interval to reflect thin-history uncertainty
        _fut = forecast["ds"] > last_actual
        _mid = forecast.loc[_fut, "yhat"]
        forecast.loc[_fut, "yhat_lower"] = (_mid - (_mid - forecast.loc[_fut, "yhat_lower"]) * YOUNG_BAND).clip(lower=0)
        forecast.loc[_fut, "yhat_upper"] = _mid + (forecast.loc[_fut, "yhat_upper"] - _mid) * YOUNG_BAND
        future_fc = forecast[forecast["ds"] > last_actual].copy().reset_index(drop=True)

    # Stock on the way counts FROM THE DAY IT LANDS, not from today. Adding it to today's
    # shelf treated a shipment 90 days out as if it were already in the warehouse, which
    # pushed the projected stockout far into the future and suppressed a reorder the user
    # genuinely needed. `on_order_eta_days` is how many days until it arrives; with no
    # date the old behaviour is kept (available immediately) so nothing silently changes
    # for callers that don't supply one.
    future_fc["cum_mid"] = future_fc["yhat"].clip(lower=0).cumsum()
    _eta = None if on_order_eta_days is None else max(0, int(on_order_eta_days))
    if units_on_order and _eta:
        # Available stock on each future day = today's stock + the PO once it has landed.
        _days_out = (future_fc["ds"] - today()).dt.days
        _arrived = (_days_out >= _eta).astype(float) * float(units_on_order)
        so = future_fc[future_fc["cum_mid"] >= (float(stock) + _arrived)]
        eff_stock = stock + units_on_order      # for display/back-compat only
    else:
        eff_stock = stock + units_on_order
        so = future_fc[future_fc["cum_mid"] >= eff_stock]
    # Internally -1 means "no stockout in the forecast horizon" (kept as an int so the
    # downstream order math stays simple). On the way OUT (see the return dict) it's
    # converted to None *only* when there's genuinely no reorder — so a real reorder
    # landing exactly one day overdue (d_ro == -1) is no longer mistaken for "none".
    so_ts = ro_ts = None; d_so = d_ro = -1
    if not so.empty:
        sod = so.iloc[0]["ds"]; d_so = (sod - today()).days
        so_ts = int(pd.Timestamp(sod).replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        rod = sod - pd.Timedelta(days=lead_time_days); d_ro = (rod - today()).days
        ro_ts = int(pd.Timestamp(rod).replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    if inactive:
        # Dormant product: its "stockout"/"reorder" dates are anchored years in the past,
        # so they'd read as absurdly overdue. Suppress them (→ None in the response).
        so_ts = ro_ts = None; d_so = d_ro = -1
    if too_new:
        # First week of life — no trustworthy forecast yet, so suppress the reorder/stockout
        # dates. The UI shows the "establishing baseline" state instead of a guessed number.
        so_ts = ro_ts = None; d_so = d_ro = -1

    cv = e.get("residual_cv", 0.4); rstd = e.get("residual_std", 0.0)
    # Demand volatility (σ proxy) is still surfaced for context, but it no longer
    # picks the service level — that now comes from the protection tier below.
    vcolor, vstrat, vdesc, _z_from_cv = get_demand_volatility(cv)

    # Protection level: recommend from the item's economics when supported, otherwise
    # fall back to the margin/default heuristic. Manual override still wins.
    price = e.get("effective_price") or e.get("last_price")
    margin_pct = None
    if price and unit_cost not in (None, "") and float(price) > 0:
        margin_pct = (float(price) - float(unit_cost) - float(fees)) / float(price) * 100.0
    # Buffer from the MEASURED lead-window error when we have it (captures clumping),
    # else the textbook σ_daily × √lead. Self-calibrating, capped at 3× the formula so
    # a noisy holdout can't explode the buffer, and falls back safely.
    _base = rstd * np.sqrt(lead_time_days)
    _lw = lead_window_sigma(e.get("oos_residuals"), lead_time_days)
    # WIDEN-ONLY: clumping needs more buffer (use the measurement), but a calm 45-day
    # holdout under-measuring error must never THIN the buffer below the √lead formula.
    _basis = _base if _lw is None else min(max(_lw, _base), 3 * _base)
    rec_key, rec_reason, rec_source, rec_economics = recommend_economic_protection(
        price=price, unit_cost=unit_cost, fees=fees, margin_pct=margin_pct,
        demand_spread=_basis, coverage_days=coverage_days,
        days_history=days_hist, total_sales=total_sales, demand_class=e.get("demand_class"),
        lead_time_days=lead_time_days, sku_id=str(sku_id),
        # Use the rate the user actually set. This defaulted to 25% no matter what they'd
        # entered, so a product falling back to the cost curve was priced on an assumption
        # the rest of the app had already replaced.
        holding_annual=max(0.0, _session_holding_pct / 100.0),
        backtest_rec=_backtest_tier_cache.get(_tier_cache_key(str(sku_id), lead_time_days, coverage_days)),
    )
    chosen_key = protection if protection in _TIER_BY_KEY else rec_key
    tier = _TIER_BY_KEY[chosen_key]
    z = tier["z"]
    safety = round(z * _basis)
    if inactive:
        safety = 0   # no safety stock for a discontinued item — don't buffer demand of 0
    # Is a better recommendation being computed right now? The cost curve is a real
    # answer, not a placeholder — but if a backtest is mid-flight the number is about to
    # change, and showing it as settled invites a decision the user would then revisit.
    _job = _bt_job_snapshot()
    _bt_cached = _backtest_tier_cache.get(_tier_cache_key(str(sku_id), lead_time_days, coverage_days)) \
        or next((v for k, v in _backtest_tier_cache.items() if k.startswith(f"{sku_id}|")), None)
    if _job.get("status") == "running" and rec_source != "backtest":
        _status = "calculating"
    elif rec_source == "backtest":
        _status = "backtest"
    elif _bt_cached:
        # We have a measured pick for this SKU but it was run for different settings.
        _status = "stale"
    else:
        _status = rec_source
    protection_info = {
        "chosen": chosen_key, "recommended": rec_key, "options": PROTECTION_TIERS,
        "reason": rec_reason, "marginPct": round(margin_pct, 1) if margin_pct is not None else None,
        "costKnown": margin_pct is not None, "overridden": chosen_key != rec_key,
        # Whether a COST exists, separate from whether margin could be computed — the two
        # diverge when the price is what's missing.
        "costKnownRaw": unit_cost not in (None, ""),
        "priceKnown": bool(price) and float(price or 0) > 0,
        "servicePct": tier["pct"], "label": tier["label"],
        "source": rec_source, "economics": rec_economics,
        # When the measurement behind this recommendation was actually taken. "Measured"
        # and "measured four months ago" are different claims, and the UI had no way to
        # tell them apart.
        "measuredAt": (_bt_cached or {}).get("measuredAt"),
        "status": _status,
        # The buffer math needs a level at ALL times — safety stock feeds the order
        # quantity — so a figure always exists. `provisional` says whether it has
        # actually been tested against this product's history, or is still an estimate
        # the UI should decline to present as an answer.
        "provisional": _status in ("calculating", "stale", "economics", "margin", "default"),
        "whyProvisional": _why_provisional(str(sku_id), _status, days_hist,
                                           lead_time_days, coverage_days, _bt_cached,
                                           unit_cost, fees),
        "jobStartedAt": _job.get("startedAt") if _status == "calculating" else None,
    }
    doff = max(d_ro, 0) + lead_time_days
    cw = future_fc.iloc[doff:doff + coverage_days]
    if cw.empty: cw = future_fc.tail(coverage_days)
    add = cw["yhat"].clip(lower=0).mean(); cov_qty = round(add * coverage_days); target = cov_qty + safety
    dud = max(d_ro, 0) + lead_time_days
    dem_del = round(future_fc.head(dud)["yhat"].clip(lower=0).sum())
    # …and only count it against the NEW order if it actually arrives before that order
    # does. A PO landing after your next delivery can't cover demand in between.
    _po_in_time = units_on_order if (_eta is None or _eta <= dud) else 0
    stock_del = max(0, stock - dem_del + _po_in_time)
    if d_ro > 0:
        sbr = round(future_fc.head(d_ro)["yhat"].clip(lower=0).sum()); psr = max(0, stock - sbr)
    else:
        sbr, psr = 0, stock
    order_qty = max(0, target - stock_del)
    if inactive:
        order_qty = 0   # never recommend reordering a discontinued product
    if too_new:
        order_qty = 0   # don't recommend an order off less than a week of data

    # ── Order guardrail for thin-history SKUs ────────────────────────────────
    # When a product has little history, the forecast (and therefore this order
    # recommendation) is still speculative. Rather than silently trusting it, we
    # flag the order and suggest a conservative first-order cap (~6 weeks of cover)
    # so a new SKU can't trigger a big over-buy. The manager still decides.
    order_guardrail = None
    if days_hist < GUARDRAIL_NEW_DAYS and order_qty > 0:
        cap = max(0, round(add * GUARDRAIL_COVER_DAYS) - stock_del)
        exceeded = cap < order_qty
        wks = GUARDRAIL_COVER_DAYS // 7
        if exceeded:
            reason = (f"Only {days_hist} days of sales history — this forecast is still "
                      f"speculative. The recommended {order_qty:,} exceeds ~{wks} weeks of cover; "
                      f"consider capping the first order near {cap:,} units and reordering once "
                      f"more sales come in.")
        else:
            reason = (f"Only {days_hist} days of sales history — treat this as provisional. The "
                      f"recommended {order_qty:,} is within ~{wks} weeks of cover, so it's a "
                      f"reasonable first order; just plan to reorder as more sales land.")
        order_guardrail = {
            "active": True, "capExceeded": exceeded, "suggestedCap": cap,
            "coverDays": GUARDRAIL_COVER_DAYS, "rawOrderQty": order_qty,
            "daysHistory": days_hist, "reason": reason,
        }

    cmp = today().to_period("M")
    usf = int(df_train[df_train["ds"].dt.to_period("M") == cmp]["y"].sum())
    fcr = float(future_fc[future_fc["ds"].dt.to_period("M") == cmp]["yhat"].clip(lower=0).sum())
    lmt = int(df_train[df_train["ds"].dt.to_period("M") == (cmp - 1)]["y"].sum())

    cards = []; cms = pd.Timestamp(today().to_period("M").to_timestamp())
    for i in range(forecast_months):
        ms = cms + relativedelta(months=i); me = ms + relativedelta(months=1) - pd.Timedelta(days=1)
        fm = forecast[(forecast["ds"] >= ms) & (forecast["ds"] <= me)]; fut = fm[fm["ds"] > last_actual]
        past = df_train[(df_train["ds"] >= ms) & (df_train["ds"] <= last_actual)]
        asum = int(past["y"].sum()); fsum = float(fut["yhat"].clip(lower=0).sum())
        flo = float(fut["yhat_lower"].clip(lower=0).sum()); fhi = float(fut["yhat_upper"].clip(lower=0).sum())
        mflag, mmsg = get_reliability_flag(max((me - today()).days, 1), days_hist)
        cards.append({"monthLabel": ms.strftime("%B %Y"), "isCurrent": i == 0, "actualsSoFar": asum,
                      "forecastRemaining": round(fsum), "projectedTotal": round(asum + fsum),
                      "rangeLow": round(asum + flo), "rangeHigh": round(asum + fhi),
                      "reliabilityColor": mflag, "reliabilityMessage": mmsg, "intervalWidth": interval_width})

    monthly = [{"month": pd.Timestamp(str(p)).strftime("%B %Y"), "total": int(v)}
               for p, v in df_train.groupby(df_train["ds"].dt.to_period("M"))["y"].sum().items()]
    # ── Slow sellers, and a check that the model hasn't collapsed the rate ──────
    # A sub-1/day product is not "no demand" — it's a slow recurring seller, and the
    # whole UI has to say so in rates rather than whole units.
    _fc_rate = float(future_fc["yhat"].clip(lower=0).head(90).mean()) if len(future_fc) else 0.0
    _y = df_train["y"].to_numpy(float)
    _r90 = float(_y[-90:].mean()) if len(_y) >= 30 else (float(_y.mean()) if len(_y) else 0.0)
    _r180 = float(_y[-180:].mean()) if len(_y) >= 60 else _r90
    _recent = max(_r90, _r180)
    _sell_share = float((_y > 0).mean()) if len(_y) else 0.0
    slow_seller = bool(_fc_rate < 1.0 and _fc_rate > 0)
    # Guardrail: if the model's forward rate is far below what this product has actually
    # been doing, say so. Intermittent models decay their demand PROBABILITY during a run
    # of zero days, which can leave the point forecast well under a rate the product is
    # still genuinely selling at.
    rate_check = None
    if _recent > 0.05 and _fc_rate < _recent * 0.7:
        rate_check = {
            "forecastRate": round(_fc_rate, 2), "recentRate": round(_recent, 2),
            "shortfallPct": round((1 - _fc_rate / _recent) * 100),
            "sellingDayShare": round(_sell_share * 100),
            "message": (f"The forecast averages {_fc_rate:.2f}/day, but this product has actually been "
                        f"selling {_recent:.2f}/day over the last "
                        f"{'90' if _r90 >= _r180 else '180'} days — about "
                        f"{round((1 - _fc_rate / _recent) * 100)}% lower. Sparse-demand models lower their "
                        f"estimate during stretches of zero-sale days, which can undershoot a product that "
                        f"is still selling regularly. Treat the order suggestion as a floor and check the "
                        f"backtest before trusting a cut this size."),
        }
    _sparse_sub = sparse_subtype(_y, e.get("demand_class"))
    # One plain sentence describing how this product actually sells, so the UI never has
    # to render "0 per day" as though that were the story.
    if _sparse_sub == "low_volume_regular":
        _demand_story = (f"Slow but regular: sells on about {round(_sell_share * 100)}% of days, "
                         f"roughly {_fc_rate:.1f} a day — call it {round(_fc_rate * 30)} a month. "
                         f"Plenty of zero-sale days is normal for this product, not a warning sign.")
    elif _sparse_sub == "true_intermittent":
        _demand_story = (f"Sporadic: sells on about {round(_sell_share * 100)}% of days, often with long "
                         f"gaps. Expect roughly {round(_fc_rate * 30)} a month arriving in bursts rather "
                         f"than steadily.")
    elif slow_seller:
        _demand_story = (f"Low volume: about {_fc_rate:.1f} a day, roughly {round(_fc_rate * 30)} a month.")
    else:
        _demand_story = None
    hist = [{"x": int(r["ds"].timestamp() * 1000), "y": int(r["y"])} for _, r in df_train.iterrows()]
    fut_line, fut_rng = [], []
    for _, r in future_fc.iterrows():
        if r["ds"] > today() + pd.Timedelta(days=tot_fc_days + 60): continue
        ms_ = int(r["ds"].timestamp() * 1000)
        # Do NOT round to whole units. A slow seller forecast at 0.4/day rounds to 0
        # every single day, so the chart and the export both read "0 per day" for a
        # product that genuinely sells ~12 a month. Two decimals keeps the daily rate
        # honest; anything that needs whole units (order quantities) rounds at the END,
        # after summing over the lead time and coverage window.
        fut_line.append({"x": ms_, "y": round(max(0.0, float(r["yhat"])), 2)})
        fut_rng.append({"x": ms_, "y": [max(0, round(float(r["yhat_lower"]))), max(0, round(float(r["yhat_upper"])))]})
    promos = [{"date": ev["date"], "end_date": ev.get("end_date", ev["date"]), "label": ev.get("label") or "Promo",
               "discount_pct": ev.get("discount_pct")} for ev in events
              if ev.get("type") == "promotion" and ev.get("date", "") >= today().strftime("%Y-%m-%d")]

    return {
        "skuId": sku_id, "skuName": e["sku_name"], "mode": e["mode"], "filename": e["filename"],
        "status": status, "statusMessage": status_msg,
        "inactive": inactive, "inactiveMessage": inactive_msg, "inactiveDays": inactive_days,
        "tooNew": too_new, "tooNewMessage": too_new_msg, "ownDays": own_days, "baselineDays": BASELINE_DAYS,
        "observedDailyRate": round(obs_rate, 2), "observedRunwayDays": obs_runway,
        "observedStockoutTs": obs_stockout_ts,
        "young": young, "youngMessage": young_msg, "youngThreshold": YOUNG_DAYS,
        "activeYearly": e["active_yearly"], "activeWeekly": e["active_weekly"],
        "daysOfHistory": days_hist, "sellingDays": selling_days, "totalUnitsSold": total_sales,
        "reliabilityColor": rel_color, "reliabilityMessage": rel_msg, "intervalWidth": interval_width,
        "demandVolatilityColor": vcolor, "demandVolatilityStrategy": vstrat, "demandVolatilityDesc": vdesc,
        "residualCv": round(cv, 3), "zScore": round(z, 3), "residualStd": round(float(rstd), 2),
        "hasPrice": e["has_price"], "hasPromotion": e["has_promo"], "priceVaried": e["price_varied"],
        "priceWellSampled": e["price_well_sampled"], "lastPrice": e["last_price"],
        "priceModeled": e.get("price_modeled", False), "currentPrice": e.get("effective_price"),
        "priceTiers": e.get("price_tiers", []), "priceTrainedMin": e.get("price_trained_min"),
        "priceTrainedMax": e.get("price_trained_max"), "priceSafeMin": e.get("price_safe_min"),
        "priceSafeMax": e.get("price_safe_max"), "priceGapWarning": e.get("price_gap_warning"),
        "stockoutRowsDropped": e["stockout_rows_dropped"],
        "priceChangeWarning": pc_level is not None, "priceChangeLevel": pc_level, "priceChangeMessage": pc_msg,
        "events": events,
        "winningModel": e.get("winning_model"), "demandClass": e.get("demand_class"),
        "route": e.get("route"), "routeReason": e.get("route_reason"), "forecastDetails": e.get("explain"),
        "clusterInfo": e.get("cluster_info"), "groupInfo": e.get("group_info"),
        "protection": protection_info, "stockDataAvailable": stock_data_available,
        "avgDailyDemand": float(round(add, 1)), "safetyStock": safety, "coverageQty": cov_qty,
        "targetInventory": target, "orderQty": order_qty, "orderGuardrail": order_guardrail,
        "projectedStockReorder": psr,
        "stockAtDelivery": stock_del, "salesBeforeReorder": sbr,
        "stockoutTimestamp": so_ts, "reorderTimestamp": ro_ts,
        # A stockout date in the PAST (d_so < 0) means the item is already out — surface
        # that as 0 days ("out now"), never a negative countdown. `alreadyOut` lets the UI /
        # exports say "Out of stock" instead of showing a bare 0.
        "daysUntilStockout": (max(int(d_so), 0) if so_ts is not None else None),
        "alreadyOut": bool(so_ts is not None and int(d_so) < 0),
        "daysUntilReorder": (d_ro if ro_ts is not None else None),
        "currentMonth": {"unitsSoFar": usf, "forecastRemaining": round(fcr), "lastMonthTotal": lmt},
        "monthCards": cards, "monthlySummary": monthly,
        "chartDataHistory": hist, "chartDataFuture": fut_line, "chartDataRange": fut_rng, "upcomingPromos": promos,
        "slowSeller": slow_seller, "rateCheck": rate_check,
        "sparseSubtype": _sparse_sub,
        "demandStory": _demand_story,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  SCORECARD  (unchanged logic; baseline now via engine.predict_baseline)
# ═════════════════════════════════════════════════════════════════════════════
SELLTHROUGH_NEUTRAL = 0.30; HEALTH_HEALTHY = 70; HEALTH_ATTENTION = 40
# Healthy sell-through band (trailing window). Outside it, velocity overrides a
# good margin in the recommendation — a great margin earns nothing if nothing
# sells, and selling out fast means missed sales even at a healthy margin.
SELLTHROUGH_LOW = 0.15    # below → too slow (overstock / slow mover)
SELLTHROUGH_HIGH = 0.55   # above → too fast (likely under-stocked)
SC_PROVISIONAL_DAYS = 90  # under this much history a SKU is "still establishing"
MARGIN_TIER_A = 40.0; MARGIN_TIER_B = 20.0; TRAILING_DAYS_DEFAULT = 30
STATUS_RANK = {"Stockout risk": 0, "Dead stock": 1, "Overstocked": 2, "Reorder due": 3, "Healthy": 4}


def sc_effective_price_today(entry, events):
    price = entry.get("last_price")
    if price is None: return None
    perm = [e for e in events if e.get("type") == "price_change_permanent" and e.get("new_price") is not None
            and pd.to_datetime(e["date"]) <= today()]
    if perm:
        perm.sort(key=lambda e: pd.to_datetime(e["date"])); price = float(perm[-1]["new_price"])
    for e in events:
        if e.get("type") == "price_change_temporary" and e.get("new_price") is not None:
            s = pd.to_datetime(e["date"]); en = pd.to_datetime(e.get("end_date", e["date"]))
            if s <= today() <= en: price = float(e["new_price"])
    return float(price)


def sc_on_promo_today(entry, events):
    for e in events:
        if e.get("type") == "promotion":
            s = pd.to_datetime(e["date"]); en = pd.to_datetime(e.get("end_date", e["date"]))
            if s <= today() <= en: return True, e.get("discount_pct")
    df = entry.get("df_train")
    if df is not None and "on_promotion" in df.columns and len(df):
        try:
            if float(df.sort_values("ds").iloc[-1].get("on_promotion", 0)) >= 1: return True, None
        except Exception: pass
    return False, None


def sc_baseline_future_fc(entry, regular_price):
    eng = entry.get("engine")
    if eng is None: return entry.get("future_fc")
    try:
        fc = eng.predict_baseline(regular_price)
        return fc[fc["ds"] > today()].copy().reset_index(drop=True)
    except Exception as ex:
        print(f"[scorecard] baseline failed: {ex}"); return entry.get("future_fc")


def sc_days_of_cover(fc, stock):
    if fc is None or fc.empty: return None
    f = fc.copy(); f["cum"] = f["yhat"].clip(lower=0).cumsum(); hit = f[f["cum"] >= stock]
    return None if hit.empty else max(int((hit.iloc[0]["ds"] - today()).days), 0)


def sc_cover_score(c, lt, cov):
    c = 365 if c is None else c; lo = lt * 1.2; hi = lt + cov; dead = max(hi * 4.0, hi + 120.0)
    if lo <= c <= hi: return 1.0
    if c < lo: return max(0.0, c / lo)
    if c >= dead: return 0.0
    return max(0.0, 1.0 - (c - hi) / (dead - hi))


def sc_reliability_score(cv):
    if cv is None: return 0.6
    cv = float(cv)
    if cv <= 0.30: return 1.0
    if cv >= 0.90: return 0.10
    return 1.0 - (cv - 0.30) / 0.60 * 0.90


# sc_trend_score is gone with the "Sales trend" signal it fed. The bar needed more
# nuance than a bar can carry: it compared the FORECAST for the next 30 days against
# the last 30 days of actuals, so a product whose sales collapsed last month but is
# forecast to hold at that new lower rate read "steady" — while the label called
# anything under 0.95 "declining" and the bar itself only started penalising below
# 0.90, so a SKU at 0.92 showed "declining 8%" beside a full green bar.
#
# The same comparison survives where it has room to explain itself: `rising` still
# softens the new-product recommendation, and a genuine divergence (forecast under
# 60% or over 160% of the recent run rate) still raises a flag — a flag only appears
# when something is actually worth saying, which is the right shape for this signal.


def sc_return_tier(m):
    if m is None: return "—"
    if m >= MARGIN_TIER_A: return "A"
    if m >= MARGIN_TIER_B: return "B"
    if m > 0: return "C"
    return "F"   # 0% or negative margin — you lose (or make nothing) on each sale


def sc_cover_direction(c, lt, cov):
    if c is None: return "long"
    if c < lt * 1.2: return "short"
    if c > (lt + cov): return "long"
    return "healthy"


def sc_bands(lt):
    """Reorder bands for ONE product, scaled to its own lead time.

    A flat "0-5 days is due, anything earlier is a risk" said the same thing about a
    3-day air line and a 60-day sea crossing. Ten days from the reorder point is
    comfortable on air and close to unrecoverable on sea, and the flat rule called
    both routine.

    So the bands are fractions of that product's lead time:

        risk  = lt / 2     under this, one ordinary supplier delay eats the slack
        due   = lt * 1.5   beyond this, the reorder is far enough off to be routine

    At the default 14-day lead time these come out at 7 and 21 days — the numbers a
    flat rule would have used anyway, which is a decent sign the fractions are sane.
    Air (lt 3) gets 2 and 5; sea (lt 60) gets 30 and 90.
    """
    lt = int(lt) if lt and int(lt) > 0 else 14
    return max(1, round(lt / 2)), max(2, round(lt * 1.5))


def sc_status(c, dur, st, lt, cov, po, is_new=False):
    """The ONE status definition. The fleet card renders these verbatim rather than
    classifying again, so the two tabs cannot disagree about a product.

    Every threshold here is per-product: the reorder bands come from this product's
    lead time, and the coverage ceiling is its own lead time + coverage window. A
    line with a 90-day coverage target is healthy at 70 days of stock; one with a
    14-day target is overstocked at the same 70. Nothing is compared against a
    fleet-wide constant.
    """
    risk, due = sc_bands(lt)
    # Not just "already late" — anything inside half a lead time is a position you
    # may not be able to recover by ordering today.
    if not po and dur is not None and dur < risk: return "Stockout risk"
    # A new product is never "Dead stock" — it hasn't had a chance to prove out.
    # It can still read as "Overstocked" (that's an accurate, recoverable signal).
    if (st is not None and st < 0.05) and (c is None or c > (lt + cov) * 3):
        return "Overstocked" if is_new else "Dead stock"
    if c is None or c > (lt + cov): return "Overstocked"
    if not po and dur is not None and risk <= dur <= due: return "Reorder due"
    return "Healthy"


def sc_recommendation(direction, tier, st, c, lt, on_promo, po, cost_known, dur,
                      is_new=False, rising=False, days_hist=None, cov=30):
    # "soon" has to mean the same thing the status does, or the recommendation and the
    # badge above it describe different products. Both come from sc_bands now.
    _risk, _due = sc_bands(lt)
    prof = tier in ("A", "B"); soon = dur is not None and dur <= _due
    if po and soon: return ("Covered — reorder in transit", "A purchase order is already on its way. No action needed.")
    if soon:
        if dur < 0:
            n = abs(dur); t = (" Profitable line, prioritize it." if prof else " Margins are slim, keep it disciplined.")
            return ("Reorder overdue — order immediately", f"Reorder point passed {n} day{'s' if n != 1 else ''} ago. Order today." + t)
        if dur == 0:
            t = (" Profitable — give it priority." if prof else " Margins thin, order conservatively.")
            return ("Reorder due today", "Latest you can reorder and still arrive in time." + t)
        if not cost_known: return ("Reorder approaching", "Enter a unit cost to size the order against profitability.")
        if prof: return ("Reorder approaching — profitable line", f"Reorder point in ~{dur} day{'s' if dur != 1 else ''}. Prioritize it.")
        return ("Reorder approaching", f"Reorder point in ~{dur} day{'s' if dur != 1 else ''}. Reorder conservatively.")
    pct = round((st or 0) * 100)
    if direction == "long":
        # New product carrying more than it needs: it's genuinely overstocked and
        # slow RIGHT NOW, but it hasn't had time to establish — so hold and let it
        # prove out rather than marking it down or discontinuing it.
        if is_new:
            hist_txt = f"{days_hist} days" if days_hist is not None else "little history"
            trend_txt = (" and demand is trending up" if rising else "")
            cover_txt = "beyond a year" if c is None else f"~{c} days"
            return ("Overstocked for now — hold, it's still establishing",
                    f"It's carrying {cover_txt} of cover (more than your {cov}-day target) and only ~{pct}% has sold{trend_txt} — "
                    f"but it only has {hist_txt} of sales history, so this read is provisional. Hold: pause or trim new orders and "
                    f"let demand settle before judging it. Don't mark it down or discontinue yet.")
        very_dead = (c is None) or (c > (lt + 30) * 4)
        slow = st is not None and st < SELLTHROUGH_LOW   # velocity below the healthy band
        if slow and very_dead and tier in ("C", "F", "—"):
            return ("Excess stock — consider clearing",
                    ("Sales are minimal, stock far exceeds demand, AND it loses money on each sale — strong candidate to discontinue or liquidate."
                     if tier == "F" else
                     "Sales are minimal and stock far exceeds demand. Consider a markdown, promotion, or discontinuing it."))
        if slow:
            # Velocity overrides a good margin: a healthy per-unit margin earns
            # little to nothing when almost none of the stock is actually selling.
            note = (" The per-unit margin looks healthy, but only a small fraction is actually selling, so little to no profit is being realized."
                    if prof else " Sales are minimal and the margin is thin.")
            return ("Overstocked & barely selling — reduce orders",
                    f"Only ~{pct}% of stock has sold recently while it's projected to outlast demand by a wide margin." + note +
                    " Cut future orders, and consider a markdown or promotion to move it.")
        if prof:
            return ("Overstocked but profitable — reduce future orders",
                    "You're carrying more than demand requires, but it's selling at a healthy margin. Keep it, but lower future order quantities.")
        return ("Overstocked — scale back ordering", "Stock will outlast demand and the margin is slim. Reduce future orders.")

    # Cover is healthy/short — but very high velocity still signals under-stocking,
    # even when the margin looks fine (you're leaving sales on the table).
    fast = st is not None and st > SELLTHROUGH_HIGH
    if fast:
        note = (" It's a healthy-margin line, so missed sales are costly — prioritize keeping it in stock."
                if prof else " Keep an eye on the margin, but avoid stocking out.")
        return ("Selling fast — risk of under-stocking",
                f"About {pct}% of available stock has sold recently — it's moving quickly and you may be under-stocked." + note +
                " Consider raising the stock level or reorder point so you don't miss sales.")
    base = ("Healthy — maintain position", "Stock is well matched to demand. Hold and monitor.")
    if on_promo: return (base[0], base[1] + " A promotion is active, so sales are temporarily elevated.")
    return base


def sc_score_one(entry, fc, stock, unit_cost, fees, reg_price, td, lt, cov, po, on_promo):
    df = entry["df_train"]; cutoff = today() - pd.Timedelta(days=td)
    tr = df[df["ds"] > cutoff]; tu = int(tr["y"].sum()) if len(tr) else 0; ta = tu / max(td, 1)
    n30 = fc.head(30) if fc is not None else None
    fa = float(n30["yhat"].clip(lower=0).mean()) if n30 is not None and len(n30) else None
    trend = (fa / ta) if (fa is not None and ta > 0) else None
    cover = sc_days_of_cover(fc, stock); direction = sc_cover_direction(cover, lt, cov)
    dur = None if cover is None else (cover - lt)
    denom = tu + max(stock, 0); st = (tu / denom) if denom > 0 else None
    scov = sc_cover_score(cover, lt, cov); srel = sc_reliability_score(entry.get("residual_cv"))
    days_hist = int((df["ds"].max() - df["ds"].min()).days) if len(df) > 1 else 0
    is_new = days_hist < SC_PROVISIONAL_DAYS
    rising = trend is not None and trend > 1.05
    status = sc_status(cover, dur, st, lt, cov, po, is_new)
    cover_txt = "beyond a year" if cover is None else f"{cover} days"
    _cv = entry.get("residual_cv")
    relw = "n/a" if _cv is None else ("very consistent" if _cv <= 0.30 else "moderately consistent" if _cv < 0.60 else "highly variable")
    # Two signals, and they are genuinely independent: one is stock POSITION, the
    # other is demand NOISE. Neither can be derived from the other.
    #
    # "Sales trend" went first — a forecast-vs-run-rate ratio needing more nuance
    # than a bar can carry (see the note by the deleted sc_trend_score).
    #
    # "Sales velocity" went next, for a sharper reason. Sell-through is
    #     st = units_sold / (units_sold + stock) = W / (W + days_of_cover)
    # so it is days-of-cover in different units. Its fixed band put "under-stocked"
    # at cover under 13 days and "slow mover" at cover over 56 — fleet-wide
    # constants, which meant a healthy air line (3-day lead) always read
    # under-stocked and a healthy sea line (60-day lead) always read slow mover.
    # Scaling those thresholds per-product fixes that and lands exactly on
    # sc_cover_direction: checked over 19,152 lead-time x coverage x days-of-cover
    # combinations, a per-SKU velocity bar and the Stock coverage bar above it
    # disagree zero times. It was a second rendering of the first row.
    #
    # `st` itself stays — it does independent work where actuals and forecast
    # diverge (a product that genuinely stopped selling collapses st while its
    # forecast-derived cover does not), which is what the dead-stock test and the
    # slow/fast recommendation wording use it for. It is still reported as
    # `sellThrough` for the table column.
    breakdown = [
        {"key": "cover", "label": "Stock coverage", "raw": cover_txt, "score": round(scov, 2),
         "explanation": f"Stock projected to last {cover_txt}. Healthy is ~{round(lt*1.2)}–{lt+cov} days."},
        {"key": "reliability", "label": "Sales consistency", "raw": relw, "score": round(srel, 2),
         "explanation": "How consistent day-to-day demand is."},
    ]
    flags = []
    if st is not None and st < 0.05 and (cover is None or cover > (lt + cov)):
        flags.append({"type": "dead", "text": "Very low sales against high on-hand stock."})
    if trend is not None and ta > 0 and fa is not None:
        if fa < ta * 0.6: flags.append({"type": "diverge", "text": "Forecast well below recent run rate."})
        elif fa > ta * 1.6: flags.append({"type": "diverge", "text": "Forecast well above recent run rate."})
    fees_v = fees if (fees is not None and fees >= 0) else 0.0
    margin = None; ck = unit_cost is not None and unit_cost > 0
    if ck and reg_price:
        margin = round((reg_price - unit_cost - fees_v) / reg_price * 100, 1) if reg_price else None
    if is_new:
        flags.append({"type": "new", "text": f"New product ({days_hist}d of history) — health read is provisional."})
    tier = sc_return_tier(margin)
    action, reason = sc_recommendation(direction, tier, st, cover, lt, on_promo, po, ck, dur,
                                       is_new=is_new, rising=rising, days_hist=days_hist, cov=cov)
    return {"daysOfCover": cover, "coverDirection": direction, "daysUntilReorder": dur,
            "sellThrough": round(st, 3) if st is not None else None, "trailingUnits": tu,
            "status": status, "statusRank": STATUS_RANK.get(status, 9), "signals": breakdown,
            "flags": flags, "marginPct": margin, "returnTier": tier,
            "recommendation": {"action": action, "reason": reason}}


@app.post("/api/scorecard")
def get_scorecard(payload: dict = Body(default={})):
    td = int(payload.get("trailingDays", TRAILING_DAYS_DEFAULT)); inp = payload.get("skus", {}) or {}
    rows = []
    for sku_id, e in _sku_cache.items():
        if e.get("df_train") is None or e.get("future_fc") is None: continue
        cfg = inp.get(sku_id, {})
        stock = int(cfg.get("stock", 500)); uc = cfg.get("unitCost", None); uc = float(uc) if uc not in (None, "") else None
        fees = cfg.get("fees", None); fees = float(fees) if fees not in (None, "") else 0.0
        lt = int(cfg.get("leadTime", 14)); cov = int(cfg.get("coverage", 30)); po = bool(cfg.get("hasOpenPo", False))
        events = e.get("events", []) or []
        reg = sc_effective_price_today(e, events); on_promo, disc = sc_on_promo_today(e, events)
        pp = round(reg * (1 - float(disc) / 100), 2) if (on_promo and disc and reg is not None) else None
        live = sc_score_one(e, e["future_fc"], stock, uc, fees, reg, td, lt, cov, po, on_promo)
        row = {"skuId": sku_id, "skuName": e.get("sku_name") or sku_id, "mode": e.get("mode"),
               "stock": stock, "unitCost": uc, "fees": fees, "costKnown": uc is not None and uc > 0,
               "regularPrice": reg, "onPromoToday": on_promo, "promoPrice": pp,
               "carryingValue": round(stock * uc, 2) if uc is not None else None,
               "leadTime": lt, "coverage": cov, "trailingDays": td,
               "winningModel": e.get("winning_model"), "demandClass": e.get("demand_class"), "route": e.get("route"),
               **live}
        if on_promo:
            row["baselineView"] = sc_score_one(e, sc_baseline_future_fc(e, reg), stock, uc, fees, reg, td, lt, cov, po, False)
        rows.append(row)
    dist = {k: 0 for k in STATUS_RANK}
    for r in rows: dist[r["status"]] = dist.get(r["status"], 0) + 1
    scatter = [{"skuId": r["skuId"], "skuName": r["skuName"], "marginPct": r["marginPct"],
                "daysOfCover": r["daysOfCover"], "carryingValue": r["carryingValue"],
                "status": r["status"], "returnTier": r["returnTier"]} for r in rows]
    by_rank = sorted(rows, key=lambda r: r["statusRank"])
    by_carry = sorted([r for r in rows if r["carryingValue"] is not None], key=lambda r: r["carryingValue"], reverse=True)
    return {"generatedAt": today().strftime("%Y-%m-%d"), "trailingDays": td, "rows": rows, "distribution": dist,
            "scatter": scatter, "needsAttention": by_rank[:5], "highestCarry": by_carry[:5],
            "modelSwitches": _recent_switches()}


@app.get("/api/model-switches")
def model_switches(days: int = Query(default=SWITCH_BANNER_DAYS, ge=1, le=365)):
    """Recent forecast-model switches (for the overview banner). `days` controls
    the lookback window; the full history is always kept on disk."""
    return {"switches": _recent_switches(days), "windowDays": days,
            "totalLogged": len(_switch_state["events"])}


@app.get("/api/history")
def get_history(sku_id: str | None = Query(default=None), recent_days: int = Query(default=90, ge=0, le=400)):
    """Historical sales for the AI assistant. Returns COMPLETE monthly aggregates
    (full coverage of each SKU's history) plus the most recent `recent_days` of
    daily rows — enough to answer historical/trend questions without shipping
    thousands of raw rows. Dates reflect the loaded (re-anchored) data."""
    out = []
    targets = [sku_id] if sku_id else list(_sku_cache.keys())
    for sid in targets:
        e = _sku_cache.get(sid)
        if not e or e.get("df_train") is None or e["df_train"].empty:
            continue
        df = e["df_train"]
        has_price = "price" in df.columns
        has_promo = "on_promotion" in df.columns
        has_stock = "units_in_stock" in df.columns

        monthly = []
        for per, grp in df.groupby(df["ds"].dt.to_period("M")):
            row = {"month": str(per), "units": int(grp["y"].sum())}
            if has_price and grp["price"].notna().any(): row["avgPrice"] = round(float(grp["price"].mean()), 2)
            if has_promo: row["promoDays"] = int((grp["on_promotion"] > 0).sum())
            monthly.append(row)

        daily = []
        recent = df.tail(recent_days) if recent_days else df.iloc[0:0]
        for _, r in recent.iterrows():
            d = {"date": r["ds"].strftime("%Y-%m-%d"), "units": int(r["y"])}
            if has_price and pd.notna(r.get("price")): d["price"] = round(float(r["price"]), 2)
            if has_promo: d["promo"] = int(float(r.get("on_promotion", 0)) > 0)
            if has_stock and pd.notna(r.get("units_in_stock")): d["stock"] = int(r["units_in_stock"])
            daily.append(d)

        out.append({
            "skuId": sid, "skuName": e.get("sku_name") or sid,
            "dateRange": {"start": df["ds"].min().strftime("%Y-%m-%d"), "end": df["ds"].max().strftime("%Y-%m-%d")},
            "totalUnits": int(df["y"].sum()), "monthly": monthly, "recentDaily": daily,
        })
    return {"skus": out, "recentDays": recent_days}


# ─────────────────────────────────────────────
# EXCEL EXPORTS (styled .xlsx downloads)
# ─────────────────────────────────────────────
def _stream(wb, basename):
    """basename without extension; the download date is appended to the filename."""
    filename = f"{basename}_{datetime.date.today().isoformat()}.xlsx"
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _num(x, d):
    return x if x not in (None, "") else d


def _export_view(sid, p):
    """Run the full forecast for one SKU using the manager's saved params, so the
    export matches what's on screen. Stashes the on-hand stock under __stock."""
    p = p or {}
    view = get_forecast(
        sku_id=sid,
        stock=int(_num(p.get("stock"), 500)),
        lead_time_days=int(_num(p.get("leadTime"), 14)),
        coverage_days=int(_num(p.get("coverage"), 30)),
        strategy="balanced",
        forecast_months=3,   # exports always project the full 3-month horizon
        units_on_order=int(_num(p.get("unitsOnOrder"), 0)),
        on_order_eta_days=(int(p["onOrderEtaDays"]) if p.get("onOrderEtaDays") is not None else None),
        unit_cost=(float(p["unitCost"]) if p.get("unitCost") not in (None, "") else None),
        fees=float(_num(p.get("fees"), 0)),
        protection=(p.get("protection") or None),
    )
    view["__stock"] = int(_num(p.get("stock"), 500))
    return view


def _fleet_items(skus, folders):
    fmap = {}
    for f in (folders or {}).values():
        for sid in (f.get("skuIds") or []):
            fmap[sid] = f.get("name")
    items = []
    for sid in _sku_cache:
        try:
            items.append({"view": _export_view(sid, (skus or {}).get(sid)), "folder": fmap.get(sid)})
        except Exception as ex:
            print(f"[export] {sid}: {ex}")
    return items


@app.post("/api/export/sku/{sku_id}")
def export_sku(sku_id: str, payload: dict = Body(default={})):
    if sku_id not in _sku_cache:
        raise HTTPException(404, "SKU not found.")
    p = (payload.get("skus") or {}).get(sku_id) or payload.get("params") or payload
    wb = exports.build_sku_workbook(_export_view(sku_id, p))
    return _stream(wb, f"{sku_id}_forecast")


@app.post("/api/export/fleet")
def export_fleet(payload: dict = Body(default={})):
    items = _fleet_items(payload.get("skus"), payload.get("folders"))
    return _stream(exports.build_fleet_workbook(items), "logitrack_fleet")


@app.post("/api/export/suppliers")
def export_suppliers(payload: dict = Body(default={})):
    suppliers = payload.get("suppliers") or []
    if isinstance(suppliers, dict):
        suppliers = list(suppliers.values())
    return _stream(exports.build_suppliers_workbook(suppliers), "logitrack_suppliers")


@app.post("/api/export/all")
def export_all(payload: dict = Body(default={})):
    skus = payload.get("skus") or {}
    items = _fleet_items(skus, payload.get("folders"))
    suppliers = payload.get("suppliers") or []
    if isinstance(suppliers, dict):
        suppliers = list(suppliers.values())
    sc_body = {"trailingDays": payload.get("trailingDays", 30),
               "skus": {sid: {"stock": _num((skus.get(sid) or {}).get("stock"), 500),
                              "unitCost": (skus.get(sid) or {}).get("unitCost"),
                              "fees": (skus.get(sid) or {}).get("fees"),
                              "leadTime": _num((skus.get(sid) or {}).get("leadTime"), 14),
                              "coverage": _num((skus.get(sid) or {}).get("coverage"), 30),
                              "hasOpenPo": (skus.get(sid) or {}).get("hasOpenPo", False)}
                        for sid in _sku_cache}}
    try:
        sc_rows = get_scorecard(sc_body).get("rows", [])
    except Exception as ex:
        print(f"[export] scorecard: {ex}"); sc_rows = []
    # The backtest lives server-side, so "everything" can include it without the browser
    # having to send it back. Previously it was simply absent from the full report.
    return _stream(exports.build_all_workbook(items, suppliers, sc_rows, backtest=_last_backtest), "logitrack_full_report")


# ─────────────────────────────────────────────
# BACKTEST  (forecast accuracy + reorder-decision quality)
# ─────────────────────────────────────────────
def _parse_sku_costs(costs):
    """Normalize the per-SKU economics map sent from the dashboard Scorecard into
    {sku_id: {"cost": float|None, "fees": float}}. Accepts a dict or a JSON string;
    drops entries with no usable cost (those SKUs are then skipped from the $ analysis)."""
    if not costs:
        return None
    if isinstance(costs, str):
        try:
            costs = json.loads(costs)
        except Exception:
            return None
    if not isinstance(costs, dict):
        return None
    out = {}
    for sid, v in costs.items():
        if not isinstance(v, dict):
            continue
        c = v.get("cost", v.get("unitCost"))
        try:
            c = float(c) if c not in (None, "") else None
        except (TypeError, ValueError):
            c = None
        if c is None:
            continue                      # no cost → skip this SKU (no assumption)
        try:
            f = float(v.get("fees")) if v.get("fees") not in (None, "") else 0.0
        except (TypeError, ValueError):
            f = 0.0
        out[str(sid)] = {"cost": c, "fees": f}
    return out or None


def _bt_params(d):
    g = lambda k, dv: int(d.get(k, dv)) if str(d.get(k, dv)).strip() != "" else dv
    return dict(horizon=g("horizon", 44), n_cutoffs=g("cutoffs", 8), step=g("step", 28),
                lead=g("lead", 14), coverage=g("coverage", 30),
                # NOT g(): service is the one param that can be fractional (99.5).
                service_pct=_svc(d.get("service", 95)), min_train=g("minTrain", 120),
                holding_pct=g("holding", 25))


def _parse_combos(raw):
    """[{lead, coverage}, ...] → [(lead, coverage), ...], ignoring anything malformed."""
    out = []
    for c in (raw or []):
        try:
            out.append((int(c["lead"]), int(c["coverage"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def catalog_to_frame() -> pd.DataFrame:
    """The loaded fleet as one long frame the backtest can consume.

    Lets the backtest run against what's already on the dashboard instead of asking you
    to re-upload the same file — which was both redundant and a live footgun, since
    nothing stopped you backtesting a different file than your fleet. Cost isn't kept in
    the catalog frame, so per-SKU costs still come from the Scorecard (`sku_costs`)."""
    frames = []
    for sid, entry in _catalog.items():
        d = entry.get("df")
        if d is None or len(d) < 2:
            continue
        g = d.copy()
        g["sku"] = str(sid)
        for k, v in (entry.get("attrs") or {}).items():
            if v is not None and k not in g.columns:
                g[k] = v
        frames.append(g)
    if not frames:
        raise HTTPException(400, "No sales data is loaded yet. Upload a file first.")
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["sku", "ds"]).reset_index(drop=True)


def catalog_sources() -> dict:
    """sku_id → the file that introduced it. Everything is modelled as ONE catalog (a
    product's peers can come from any file), but the report is sectioned by source so you
    can see how each upload performed on its own."""
    out = {}
    for sid, entry in _catalog.items():
        srcs = entry.get("sources") or ([entry.get("filename")] if entry.get("filename") else [])
        out[str(sid)] = str(srcs[0]) if srcs else "(unknown file)"
    return out


# ── Background backtest job ──────────────────────────────────────────────────
# The backtest refits every engine at every cutoff, so it takes minutes. Running it
# inline blocked the request and left the dashboard showing a cost-curve number with no
# hint that a better one was on its way. This runs it on a worker thread and exposes the
# state, so the UI can say "calculating" instead of quietly showing the weaker answer.
_bt_job: dict = {"status": "idle", "startedAt": None, "finishedAt": None,
                 "params": None, "error": None, "trigger": None, "progress": None,
                 "queued": False}
_bt_job_lock = threading.Lock()
_bt_pending: dict | None = None      # newest request that arrived mid-run, if any


def _bt_job_snapshot() -> dict:
    with _bt_job_lock:
        snap = dict(_bt_job)
    # When the STORED REPORT last changed. A recost re-prices the last run without
    # starting a job, so a client watching only job transitions never learns that the
    # numbers moved — which is exactly what happened when the holding rate changed: the
    # server recosted correctly and the Backtest tab kept rendering the old figures.
    snap["lastRanAt"] = (_last_backtest or {}).get("ranAt") or 0
    return snap


def _run_backtest_job(params: dict, sku_costs: dict, trigger: str) -> bool:
    """Start a run, or QUEUE it if one is already going.

    Dropping the second request was wrong: upload a file and then immediately change a
    lead time, and the tweak's re-run vanished, leaving a cache measured for settings the
    user had already moved on from. Only the newest pending request is kept — older ones
    are superseded, not stacked, since each would be recomputing the same thing."""
    global _bt_pending
    with _bt_job_lock:
        if _bt_job["status"] == "running":
            _bt_pending = {"params": dict(params), "sku_costs": dict(sku_costs), "trigger": trigger}
            _bt_job["queued"] = True
            return False
        _bt_job.update({"status": "running", "startedAt": time.time(), "finishedAt": None,
                        "params": dict(params), "error": None, "trigger": trigger, "queued": False})

    def work():
        BT = _bt()          # checked import: verifies Z matches PROTECTION_TIERS
        try:
            df = None
            with _state_lock:            # snapshot the catalog under the lock…
                df = catalog_to_frame()
            # …then compute OUTSIDE it. The fit is the slow part and touches only the
            # snapshot, so holding the lock for minutes would freeze every other request.
            with _state_lock:
                srcs = catalog_sources()
            # Merge at RUN time, not just at request time: a queued request replays the
            # costs it captured when it was queued, which can be stale or empty. The
            # sheet costs are the floor no matter when — or from where — the job starts.
            _eff = _effective_sku_costs(sku_costs) or {}
            _sink = []
            res = BT.run_for_api(df, **params, sku_costs=_eff, sku_sources=srcs, rows_sink=_sink)
            with _state_lock:
                global _last_backtest_rows, _last_backtest_combos
                _last_backtest_rows = _sink[0] if _sink else None
                _last_backtest_combos = [(c["lead"], c["coverage"]) for c in (res.get("combos") or [])]
                _cache_backtest_tiers(res)
                _record_backtest_inputs(_eff, params.get("holding_pct"))
                _store_backtest_result(res, trigger)
                _persist_backtest()
            with _bt_job_lock:
                _bt_job.update({"status": "done", "finishedAt": time.time(), "error": None,
                                "summary": {"tested": res.get("tested"), "forecasts": res.get("forecasts"),
                                            "failedCutoffs": res.get("failedCutoffs")}})
        except Exception as ex:
            with _bt_job_lock:
                _bt_job.update({"status": "error", "finishedAt": time.time(),
                                "error": f"{type(ex).__name__}: {ex}"})
        finally:
            # Hand off to whatever arrived while we were busy. Started OUTSIDE the lock —
            # _bt_job_lock is a plain Lock, so re-entering it here would deadlock.
            global _bt_pending
            with _bt_job_lock:
                nxt, _bt_pending = _bt_pending, None
                _bt_job["queued"] = False
            if nxt:
                _run_backtest_job(nxt["params"], nxt["sku_costs"], nxt["trigger"])

    threading.Thread(target=work, daemon=True, name="backtest-job").start()
    return True


@app.post("/api/settings/holding")
def set_holding(payload: dict = Body(default={})):
    """Set the annual holding rate for this session. Can be called with NO data loaded,
    so a client who knows their real figure enters it once and the very first automatic
    backtest uses it — instead of running on 25% and needing a re-run."""
    global _session_holding_pct
    try:
        v = int(float((payload or {}).get("holding")))
    except (TypeError, ValueError):
        raise HTTPException(400, "holding must be a number, e.g. 32")
    if not 0 <= v <= 200:
        raise HTTPException(400, "holding must be between 0 and 200 (percent per year)")
    _session_holding_pct = v
    return {"holding": _session_holding_pct}


@app.get("/api/settings/holding")
def get_holding():
    return {"holding": _session_holding_pct}


@app.post("/api/backtest/partial")
def backtest_partial(payload: dict = Body(default={})):
    """Re-test ONLY the named products, then rebuild the catalog totals from the merge.

    One product's lead time changing doesn't invalidate the other seventeen — their
    measured windows are still exactly right. This refits just the affected products and
    merges their fresh rows over the stale ones, so a single-product edit costs a
    single product's fits instead of the whole catalog's."""
    BT = _bt()          # checked import: verifies Z matches PROTECTION_TIERS
    p = payload or {}
    skus = [str(x) for x in (p.get("skus") or []) if str(x)]
    if not skus:
        return {"started": False, "reason": "no products named"}
    with _state_lock:
        if _last_backtest_rows is None or not _last_backtest:
            return {"started": False, "reason": "no previous run to merge into"}
    def g(k, d):
        try: return int(p.get(k, d))
        except Exception: return d
    params = {"horizon": g("horizon", 44), "n_cutoffs": p.get("cutoffs") or "auto",
              "step": g("step", 28), "lead": g("lead", 14), "coverage": g("coverage", 30),
              "service_pct": _svc(p.get("service", 95)), "min_train": g("minTrain", 120)}
    combos = _parse_combos(p.get("combos"))
    costs = _effective_sku_costs(_parse_sku_costs(p.get("costs"))) or {}
    started = _run_partial_job(skus, params, combos, costs, int(p.get("holding", _session_holding_pct)))
    return {"started": started, "skus": skus, **_bt_job_snapshot()}


@app.get("/api/backtest/last")
def backtest_last():
    """The most recent full report, whoever started it. The Backtest tab loads this on
    open so it shows the run that already happened instead of an empty page."""
    if not _last_backtest:
        return {"available": False, "job": _bt_job_snapshot()}
    return {"available": True, "result": _last_backtest, "job": _bt_job_snapshot()}


def _run_partial_job(skus: list, params: dict, combos: list, sku_costs: dict, holding_pct: int) -> bool:
    """Background refit of `skus` only, merged into the stored rows."""
    with _bt_job_lock:
        if _bt_job["status"] == "running":
            return False
        _bt_job.update({"status": "running", "startedAt": time.time(), "finishedAt": None,
                        "params": dict(params), "error": None, "trigger": f"partial:{len(skus)}",
                        "queued": False})

    def work():
        BT = _bt()          # checked import: verifies Z matches PROTECTION_TIERS
        global _last_backtest_rows, _last_backtest_combos, _session_holding_pct
        try:
            with _state_lock:
                df = catalog_to_frame()
                prev_rows, prev_res = _last_backtest_rows, _last_backtest
                prev_combos = list(_last_backtest_combos or [])
            # Relatedness still reads the whole catalog; only these products are refitted.
            fresh = BT.run_backtest(df, params["horizon"], params["n_cutoffs"], params["step"],
                                    params["lead"], params["coverage"], params["service_pct"],
                                    params["min_train"], verbose=False, sku_costs=sku_costs,
                                    combos=combos or prev_combos or None, only_skus=skus)
            merged = BT.merge_rows(prev_rows, fresh)
            all_combos = sorted({(int(a), int(b)) for a, b in
                                 zip(merged["leadDays"], merged["coverageDays"])})
            with _state_lock:
                spans = {s: int((g["ds"].max() - g["ds"].min()).days) for s, g in df.groupby("sku")}
                res = BT.rebuild_from_rows(prev_res, merged, combos=all_combos,
                                           holding_pct=holding_pct, sku_costs=sku_costs,
                                           sku_sources=catalog_sources(), spans=spans)
                _last_backtest_rows, _last_backtest_combos = merged, all_combos
                _session_holding_pct = int(holding_pct)
                _cache_backtest_tiers(res)
                _record_backtest_inputs(sku_costs, holding_pct)
                _store_backtest_result(res, f"partial:{','.join(skus[:3])}")
            with _bt_job_lock:
                _bt_job.update({"status": "done", "finishedAt": time.time(), "error": None,
                                "summary": {"refitted": len(skus), "tested": res.get("tested")}})
        except Exception as ex:
            with _bt_job_lock:
                _bt_job.update({"status": "error", "finishedAt": time.time(),
                                "error": f"{type(ex).__name__}: {ex}"})
        finally:
            global _bt_pending
            with _bt_job_lock:
                nxt, _bt_pending = _bt_pending, None
                _bt_job["queued"] = False
            if nxt:
                _run_backtest_job(nxt["params"], nxt["sku_costs"], nxt["trigger"])

    threading.Thread(target=work, daemon=True, name="backtest-partial").start()
    return True


@app.get("/api/backtest/status")
def backtest_status():
    """Where the background run is up to, plus WHICH (lead, coverage) pairs have actually
    been measured.

    The dashboard needs that list because change-detection alone isn't enough: a product
    whose lead time was set in a previous session never *changes*, so nothing fires, and
    the upload's own run only covers the defaults. It stayed 'stale' forever while the UI
    claimed a fresh test was queued. The client compares what it needs against this."""
    combos = sorted({f"{v.get('lead')}/{v.get('coverage')}"
                     for v in _backtest_tier_cache.values()
                     if v.get("lead") is not None and v.get("coverage") is not None})
    return {**_bt_job_snapshot(), "cachedCombos": combos,
            "cachedSkus": sorted({k.split("|")[0] for k in _backtest_tier_cache})}


@app.post("/api/backtest/catalog")
def backtest_catalog(payload: dict = Body(default={})):
    """Backtest the LOADED FLEET. Synchronous, because the Backtest tab wants the full
    report back; the dashboard uses /api/backtest/refresh for the background version."""
    BT = _bt()          # checked import: verifies Z matches PROTECTION_TIERS
    p = payload or {}
    def g(k, d):
        try: return int(p.get(k, d))
        except Exception: return d
    with _state_lock:
        df = catalog_to_frame()
    _sink = []
    res = BT.run_for_api(df, g("horizon", 44), p.get("cutoffs") or "auto", g("step", 28), g("lead", 14),
                         g("coverage", 30), _svc(p.get("service", 95)), g("minTrain", 120), g("holding", 25),
                         _eff_costs := _effective_sku_costs(_parse_sku_costs(p.get("costs"))),
                         combos=_parse_combos(p.get("combos")),
                         sku_sources=catalog_sources(), rows_sink=_sink)
    with _state_lock:
        global _last_backtest_rows, _last_backtest_combos
        _last_backtest_rows = _sink[0] if _sink else None
        _last_backtest_combos = [(c["lead"], c["coverage"]) for c in (res.get("combos") or [])]
        _cache_backtest_tiers(res)
        _record_backtest_inputs(_eff_costs, g("holding", 25))
        _store_backtest_result(res, "fleet-tab")
    return res


@app.post("/api/backtest/recost")
def backtest_recost(payload: dict = Body(default={})):
    """Re-price the last run with new costs — no refitting, milliseconds not minutes.

    Adding a unit cost doesn't change a single forecast, so there is nothing to refit;
    only what the already-measured units are worth. Falls back to `started: False` if
    there's no run to re-price, so the caller can trigger a full one instead."""
    BT = _bt()          # checked import: verifies Z matches PROTECTION_TIERS
    p = payload or {}
    with _state_lock:
        if _last_backtest_rows is None or not _last_backtest:
            return {"recosted": False, "reason": "no previous run to re-price"}
        global _session_holding_pct
        holding = int(p.get("holding", _session_holding_pct))
        _session_holding_pct = holding
        eff = _effective_sku_costs(_parse_sku_costs(p.get("costs"))) or {}
        res = BT.recost(_last_backtest, _last_backtest_rows, eff,
                        holding_pct=holding,
                        combos=_last_backtest_combos or None)
        _cache_backtest_tiers(res)
        _record_backtest_inputs(eff, holding)
        _store_backtest_result(res, "recost")
    return {"recosted": True, "costedSkus": (res.get("tierAnalysis") or {}).get("costedSkus"),
            "totalSkus": (res.get("tierAnalysis") or {}).get("totalSkus")}


@app.post("/api/backtest/refresh")
def backtest_refresh(payload: dict = Body(default={})):
    """Kick off a background backtest over the LOADED FLEET. Used both by the Backtest
    tab and by the dashboard when lead time or coverage changes."""
    p = payload or {}
    def g(k, d):
        try: return int(p.get(k, d))
        except Exception: return d
    params = {"horizon": g("horizon", 44), "n_cutoffs": p.get("cutoffs") or "auto", "step": g("step", 28),
              "lead": g("lead", 14), "coverage": g("coverage", 30),
              "service_pct": _svc(p.get("service", 95)), "min_train": g("minTrain", 120),
              "holding_pct": g("holding", 25), "combos": _parse_combos(p.get("combos"))}
    global _session_holding_pct
    _session_holding_pct = int(params["holding_pct"])
    started = _run_backtest_job(params, _effective_sku_costs(_parse_sku_costs(p.get("costs"))) or {},
                                p.get("trigger") or "manual")
    snap = _bt_job_snapshot()
    return {"started": started, **snap,
            "message": None if started else "A backtest is already running; this request was ignored."}


def _tier_cache_key(sku: str, lead, coverage) -> str:
    return f"{sku}|{int(lead)}|{int(coverage)}"


def _record_backtest_exclusions(res: dict) -> None:
    """Why each product's measured tier is missing.

    The cache only holds successes, so a product absent from it was indistinguishable
    from one that had simply never been tested — every failure surfaced as the same bare
    word, "provisional". These are the reasons a product can drop out, recorded at the
    moment it happens so the dashboard can say which one applies."""
    global _backtest_exclusions
    out = {}
    for s in (res.get("skipped") or []):
        sku = str(s.get("sku") or "")
        if not sku:
            continue
        if s.get("kind") == "error":
            out.setdefault(sku, f"its test windows hit an error ({s.get('reason')}).")
        else:
            out.setdefault(sku, f"it only has {s.get('days')} days of sales history — testing needs at "
                                f"least {BACKTEST_MIN_TESTABLE_DAYS}, so there's nothing to measure "
                                f"against yet.")
    for row in (res.get("tierAnalysis") or {}).get("bySku") or []:
        sku = str(row.get("sku") or "")
        if not sku or sku in out:
            continue
        windows = int(row.get("windows") or 0)
        if not row.get("costKnown"):
            out[sku] = ("no unit cost on file, so there's no way to price a stockout against the cost "
                        "of holding stock. Add one in the Scorecard.")
        elif row.get("lossMaking"):
            out[sku] = ("it sells at or below cost after fees, so a stockout forgoes no profit and more "
                        "safety stock would only lose money faster. That's a pricing issue, not a buffer one.")
        elif windows < BACKTEST_TIER_MIN_WINDOWS:
            out[sku] = (f"it produced only {windows} usable test window"
                        f"{'' if windows == 1 else 's'} — under {BACKTEST_TIER_MIN_WINDOWS} "
                        f"the result is too noisy to act on.")
        elif not row.get("bestTier"):
            out[sku] = "the test couldn't separate the protection levels for it."
    _backtest_exclusions = out


def _why_provisional(sku_id: str, status: str, days_hist: int,
                     lead_time_days: int, coverage_days: int, cached: dict | None,
                     _drift_cost=None, _drift_fees=None) -> str | None:
    """One plain sentence explaining why this product isn't using measured data.

    Ordered by what the user can act on. Timing first (it's about to resolve on its own),
    then a settings mismatch (resolves shortly), then reasons that need something FROM
    them — a cost, a price fix, more history."""
    if status == "backtest":
        return None
    job = _bt_job_snapshot()
    if status == "calculating":
        return ("A test is running right now — this will switch to a measured figure when it finishes, "
                "usually a few minutes.")
    drift = _economics_drifted(sku_id, _drift_cost, _drift_fees, (cached or {}).get("holdingPct"))
    if drift:
        return (f"The last test measured it before {drift}. That changes which protection level is "
                f"cheapest, so a fresh test has been queued.")
    if status == "stale":
        if cached:
            return (f"It was measured for a {cached.get('lead')}-day lead time and "
                    f"{cached.get('coverage')}-day coverage window. You're now asking for "
                    f"{lead_time_days} and {coverage_days}, which changes the answer, so a test for "
                    f"those settings has been queued.")
        return "Your current lead time and coverage haven't been tested yet; a test has been queued."
    # Recorded when the product dropped out of the last run.
    reason = _backtest_exclusions.get(str(sku_id))
    if reason:
        return f"It was left out of the last test because {reason}"
    if days_hist < BACKTEST_MIN_TESTABLE_DAYS:
        return (f"It has {days_hist} days of sales history. Testing needs at least "
                f"{BACKTEST_MIN_TESTABLE_DAYS} — about {BACKTEST_MIN_TESTABLE_DAYS - days_hist} more days "
                f"— before there's anything to measure against.")
    if job.get("status") == "error":
        return f"The last test failed to finish ({job.get('error')}), so no measured figure is available."
    if not _backtest_tier_cache:
        return ("No test has been run yet. One starts automatically when you upload a sales file, "
                "or you can run it from the Backtest tab.")
    return ("This product wasn't included in the last test run — uploading again, or running the "
            "Backtest tab, will pick it up.")


def _combo_policy(analysis: dict) -> tuple[str, float | None]:
    """Did per-product tuning actually EARN its keep in this combo's test?

    The per-product picks are only deployed if the mixed policy beat the best single
    level out-of-sample. If it didn't, the picks are treated as noise and every product
    gets the winning uniform level instead — otherwise the dashboard would be running
    the very policy the backtest just showed to be worse. Returns
    ("mixed", None) or ("uniform", tier_pct)."""
    tiers = (analysis or {}).get("tiers") or []
    costs = [t.get("totalCost") for t in tiers if t.get("totalCost") is not None]
    mp = (analysis or {}).get("mixedPolicy") or {}
    if costs and mp.get("totalCost") is not None and mp["totalCost"] < min(costs):
        return "mixed", None
    # Uniform fallback: prefer closest-to-optimal when the cost ranking was a coin flip,
    # mirroring how the UI presents the same decision.
    rk = (analysis or {}).get("ranking") or {}
    nearest, best = (analysis or {}).get("nearestTier"), (analysis or {}).get("bestTier")
    pick = nearest if (rk and not rk.get("decisive") and nearest) else (best or nearest)
    # float(), NOT int(): tier percentages are no longer all whole numbers, and int()
    # here silently turned a 99.5 pick into 99 — a real tier, so nothing downstream
    # could tell it had been rewritten.
    return "uniform", (float(pick) if pick else None)


def _record_backtest_inputs(sku_costs: dict | None, holding_pct) -> None:
    """The per-SKU economics and holding rate a run was measured with.

    A measured tier is only valid for the numbers it was measured with — change a unit
    cost and last week's answer describes a product that no longer exists. Lead time and
    coverage were already part of the cache key; cost and holding were not, so a cost
    edit left a stale figure sitting there labelled 'measured'."""
    global _backtest_inputs
    _backtest_inputs = {
        "holdingPct": (int(holding_pct) if holding_pct is not None else None),
        "costs": {str(k): float((v or {}).get("cost") or 0.0) for k, v in (sku_costs or {}).items()},
        "fees": {str(k): float((v or {}).get("fees") or 0.0) for k, v in (sku_costs or {}).items()},
    }


# A cost has to move by more than this before a measured tier is called stale — rounding
# a cost from 400 to 400.01 shouldn't invalidate a run that takes minutes.
COST_DRIFT_TOLERANCE = 0.02


def _economics_drifted(sku_id: str, unit_cost, fees, holding_pct=None) -> str | None:
    """Plain description of what changed since the run, or None if it still applies."""
    if not _backtest_inputs:
        return None
    sid = str(sku_id)
    was = (_backtest_inputs.get("costs") or {}).get(sid)
    if was is not None and unit_cost not in (None, "") and float(was) > 0:
        now = float(unit_cost)
        if abs(now - was) / was > COST_DRIFT_TOLERANCE:
            return f"its unit cost changed from ${was:,.2f} to ${now:,.2f}"
    was_fees = (_backtest_inputs.get("fees") or {}).get(sid)
    if was_fees is not None and fees not in (None, ""):
        if abs(float(fees) - was_fees) > max(0.01, abs(was_fees) * COST_DRIFT_TOLERANCE):
            return f"its per-unit fees changed from ${was_fees:,.2f} to ${float(fees):,.2f}"
    was_hold = _backtest_inputs.get("holdingPct")
    if was_hold is not None and holding_pct is not None and int(was_hold) != int(holding_pct):
        return f"the holding rate changed from {was_hold}%/yr to {int(holding_pct)}%/yr"
    return None


def _cache_backtest_tiers(res: dict) -> None:
    """Keep credible per-SKU backtest tier picks available for live recommendations.

    Keyed by (sku, lead, coverage), because a tier measured over a 14-day lead says
    nothing about a 21-day one. A single catalog-wide key meant that changing ONE
    product's lead time left it permanently 'stale': the re-run used the fleet's most
    common values, so its own pair was never measured."""
    global _backtest_tier_cache
    params = res.get("params") or {}
    next_cache = {}
    measured_at = res.get("ranAt")

    by_combo = res.get("tierAnalysisByCombo") or {}
    for combo_key, analysis in (by_combo or {}).items():
        try:
            c_lead, c_cov = (int(x) for x in str(combo_key).split("/"))
        except ValueError:
            continue
        policy, uniform_pct = _combo_policy(analysis)
        for row in (analysis or {}).get("bySku") or []:
            if not row.get("costKnown") or row.get("lossMaking") or not row.get("bestTier"):
                continue
            windows = int(row.get("windows") or 0)
            if windows < BACKTEST_TIER_MIN_WINDOWS:
                continue
            # _svc(), not int(): it snaps to the exact value in PROTECTION_TIERS and
            # keeps its type, so str(pct) below still produces the payload's own key
            # ("99" for the int, "99.5" for the float). int() merged 99.5 into 99;
            # float() would render "99.0" and miss the lookup entirely.
            pct = _svc(row["bestTier"], None) if policy == "mixed" else _svc(uniform_pct or row["bestTier"], None)
            key = _TIER_KEY_BY_PCT.get(pct)
            if not key:
                continue
            tier = (row.get("tiers") or {}).get(str(pct)) or {}
            next_cache[_tier_cache_key(str(row["sku"]), c_lead, c_cov)] = {
                "key": key, "pct": pct, "windows": windows, "measuredAt": measured_at,
                "lead": c_lead, "coverage": c_cov, "policy": policy,
                "holdingPct": (analysis.get("assumptions") or {}).get("holdingPct"),
                "unitsYr": tier.get("unitsYr"), "profitYr": tier.get("profitYr"),
                "holdingCostYr": tier.get("holdingCostYr"), "totalCostYr": tier.get("totalCostYr"),
                "bufferCash": tier.get("bufferCash"),
            }
    _legacy_policy, _legacy_uniform = _combo_policy(res.get("tierAnalysis") or {})
    for row in (res.get("tierAnalysis") or {}).get("bySku") or []:
        if not row.get("costKnown") or row.get("lossMaking") or not row.get("bestTier"):
            continue
        windows = int(row.get("windows") or 0)
        if windows < BACKTEST_TIER_MIN_WINDOWS:
            continue
        pct = (_svc(row["bestTier"], None) if _legacy_policy == "mixed"
               else _svc(_legacy_uniform or row["bestTier"], None))
        key = _TIER_KEY_BY_PCT.get(pct)
        if not key:
            continue
        tier = (row.get("tiers") or {}).get(str(pct)) or {}
        next_cache.setdefault(_tier_cache_key(str(row["sku"]), params.get("lead") or 14,
                                              params.get("coverage") or 30), {
            "key": key, "pct": pct, "windows": windows, "policy": _legacy_policy,
            "measuredAt": measured_at,
            "lead": params.get("lead"), "coverage": params.get("coverage"),
            "holdingPct": (res.get("tierAnalysis") or {}).get("assumptions", {}).get("holdingPct"),
            "unitsYr": tier.get("unitsYr"), "profitYr": tier.get("profitYr"),
            "holdingCostYr": tier.get("holdingCostYr"), "totalCostYr": tier.get("totalCostYr"),
            "bufferCash": tier.get("bufferCash"),
        })
    _backtest_tier_cache = next_cache
    _record_backtest_exclusions(res)


# /api/backtest/file and /api/backtest/shopify were REMOVED deliberately.
#
# They backtested a file that was NOT the loaded catalog, then wrote the result into the
# same global state the dashboard reads. Uploading an unrelated spreadsheet there wiped
# every real product's measured protection tier, and left `_last_backtest` (the foreign
# file's results) inconsistent with `_last_backtest_rows` (the real catalog's windows) —
# so a later cost edit would re-price one dataset using another's measurements.
#
# There is now exactly one way data enters the app: the Products page. Whatever is loaded
# gets backtested automatically and the Backtest tab reports on that. To test a different
# catalog, load it there (with "Add to what's already loaded" off) and it IS the catalog.


# ══════════════════════════════════════════════════════════════════════════════════════
# LIVE FORECAST LOG
#
# The backtest re-simulates history; it can only ever tell you how the tool WOULD have
# done on data it already has. This records what the tool says today and grades it once
# the days it predicted have actually happened. Different question, better evidence — the
# forecast being graded here had no access to what came next, because what came next
# hadn't happened.
#
# Deliberately NOT session state: this is the one thing that cannot be rebuilt. Lose the
# record of what you predicted on the 3rd and the only way to get it back is to wait
# another two weeks.
# ══════════════════════════════════════════════════════════════════════════════════════

import live_actuals as _LA          # noqa: E402
# One week per entry, so a card on the Live accuracy page IS a week. A 14-day window
# straddled two tiles and made the grid impossible to read.
DEFAULT_LOG_HORIZONS = [7]


def _horizon_prediction(future_fc, horizon_days: int):
    """Total units expected over the next `horizon_days`, with a band for that TOTAL.

    The point estimate is just the cumulative sum. The band is not: adding up each day's
    interval assumes every day misses in the same direction, which would give a band so
    wide it is always "right" and tells you nothing. Daily half-widths are combined in
    quadrature instead — the same independent-error assumption the safety-stock formula
    (z·σ·√lead) already makes. That's deliberate: it means the calibration number this
    log produces is a direct test of whether your BUFFERS are sized correctly, not just
    whether some abstract interval was pretty.
    """
    import numpy as _np
    h = int(horizon_days)
    seg = future_fc.head(h)
    if seg is None or len(seg) == 0:
        return None
    yhat = seg["yhat"].clip(lower=0)
    point = float(yhat.sum())
    if not {"yhat_lower", "yhat_upper"}.issubset(seg.columns):
        return {"predicted": point, "lo": None, "hi": None}
    lo_w = (yhat - seg["yhat_lower"].clip(lower=0)).clip(lower=0)
    hi_w = (seg["yhat_upper"] - yhat).clip(lower=0)
    lo_tot = float(_np.sqrt(float((lo_w ** 2).sum())))
    hi_tot = float(_np.sqrt(float((hi_w ** 2).sum())))
    return {"predicted": point,
            "lo": max(0.0, point - lo_tot),
            "hi": point + hi_tot}


@app.post("/api/livelog/snapshot")
def livelog_snapshot(payload: dict = Body(default={})):
    """Freeze what we predict right now for this period.

    Idempotent within a period by construction: calling it five times on the same Monday
    records one entry per product, and the FIRST prediction is the one kept. Re-deriving
    it later with newer data and grading that would be marking your own homework.
    """
    horizons = payload.get("horizons") or DEFAULT_LOG_HORIZONS
    cadence = payload.get("cadence") or _FL.WEEKLY
    rows, skipped = [], []
    with _state_lock:
        skus = list(_sku_cache.keys())
    for sid in skus:
        e = _sku_cache.get(sid) or {}
        fc = e.get("future_fc")
        if fc is None or len(fc) == 0:
            skipped.append(sid)
            continue
        name = (_catalog.get(sid) or {}).get("sku_name") or sid
        for h in horizons:
            pred = _horizon_prediction(fc, h)
            if pred is None:
                skipped.append(sid)
                continue
            rows.append({"sku": sid, "skuName": name, "horizonDays": int(h),
                         "predicted": pred["predicted"], "lo": pred["lo"], "hi": pred["hi"],
                         # The same constant the chart is drawn to, so what the log grades
                         # and what the user sees can never diverge.
                         "band": FORECAST_BAND_PCT, "model": (e.get("details") or {}).get("route")})
    res = _flog.snapshot(rows, cadence=cadence)
    return {**res, "skipped": len(skipped), "candidates": len(rows),
            "nextOrigin": _FL.next_origin(cadence=cadence).isoformat(),
            "summary": _flog.summary()}


@app.post("/api/livelog/score")
def livelog_score(payload: dict = Body(default={})):
    """Grade everything whose window has closed, using real Shopify sales.

    Anything not yet due is untouched — the clock decides, not the button.
    """
    pending = _flog.pending()
    due = _flog.due()
    if not due:
        return {"scored": 0, "unavailable": 0, "reason": "nothing-due",
                "summary": _flog.summary()}
    try:
        fn = _LA.shopify_actuals_provider(
            pending, shop=payload.get("shop"), token=payload.get("token"))
    except Exception as exc:
        raise HTTPException(400, f"Could not read the store: {exc}")
    res = _flog.score_due(fn)
    return {**res, "sourceFailed": bool(getattr(fn, "failed", False)),
            "lookbackDays": getattr(fn, "days", None),
            "summary": _flog.summary()}


@app.post("/api/livelog/tick")
def livelog_tick(payload: dict = Body(default={})):
    """The whole cycle, driven by the clock rather than by a button.

    Called whenever the app is open. It does two things, both idempotent:

      1. Opens this period's entries if they aren't open yet. Runs once per period no
         matter how often it's called, and never rewrites a prediction already made.
      2. Grades anything whose window has fully closed — strictly after the last counted
         day, so a window ending Saturday is graded from Sunday.

    Nothing here can be hurried. Calling it a hundred times on a Wednesday records the
    same one set of predictions and grades nothing new.
    """
    out = {"snapshot": None, "score": None, "stock": None}
    # Read inventory FIRST. Shopify keeps no history, so a reading missed now is a day we
    # can never reconstruct — whereas scoring can always be retried tomorrow.
    try:
        out["stock"] = _SL.sample_from_shopify(
            _slog, shop=payload.get("shop"), token=payload.get("token"))
    except Exception as exc:
        out["stock"] = {"ok": False, "error": str(exc), "added": 0}
    try:
        out["snapshot"] = livelog_snapshot({"horizons": payload.get("horizons"),
                                            "cadence": payload.get("cadence")})
    except Exception as exc:
        out["snapshot"] = {"error": str(exc), "added": 0}
    if _flog.due():
        try:
            out["score"] = livelog_score({})
        except HTTPException as exc:
            out["score"] = {"error": exc.detail, "scored": 0}
        except Exception as exc:
            out["score"] = {"error": str(exc), "scored": 0}
    else:
        out["score"] = {"scored": 0, "reason": "nothing-due"}
    out["summary"] = _flog.summary()
    out["stockCoverage"] = _slog.coverage()
    return out


@app.post("/api/bundles")
def bundles_set(payload: dict = Body(default={})):
    """Define which products a bundle consumes. Re-ingest to apply it to history."""
    global _bundle_map
    incoming = _BUN.normalise_map(payload.get("bundles") or {})
    with _state_lock:
        known = list(_catalog.keys())
    report = _BUN.validate(incoming, known_skus=known or None)
    _bundle_map = incoming
    return {"saved": len(incoming), "validation": report,
            "note": "Applies to sales loaded from here on — re-upload or re-sync to "
                    "attribute past bundle sales."}


@app.get("/api/bundles")
def bundles_get():
    with _state_lock:
        known = list(_catalog.keys())
    rows = []
    for sid, c in list(_catalog.items()):
        df = c.get("df")
        if df is None or not len(df):
            continue
        for _, r in df.iterrows():
            rows.append({"date": str(r.get("ds"))[:10], "sku": sid,
                         "units_sold": float(r.get("y") or 0)})
    dep = _BUN.dependency(rows, _bundle_map) if _bundle_map else {}
    return {"bundles": _bundle_map, "validation": _BUN.validate(_bundle_map, known or None),
            "dependency": dep,
            "explain": _BUN.explain(_BUN.attribute(rows, _bundle_map)[1], dep) if _bundle_map else ""}


@app.get("/api/arrivals/suggested")
def arrivals_suggested(payload_orders: str | None = Query(default=None)):
    """Deliveries the inventory record says landed, which nobody has confirmed.

    Suggestions only. Nothing is written: a stock jump can be a recount, a return or a
    transfer, and a wrong received-date silently corrupts the lead-time history that P80
    is computed from. One click to confirm is a very different thing from remembering.
    """
    import json as _json
    try:
        orders = _json.loads(payload_orders) if payload_orders else []
    except (ValueError, TypeError):
        orders = []
    by_sku = {}
    for o in orders:
        by_sku.setdefault(str(o.get("skuId")), []).append(o)
    found = []
    for sku, os_ in by_sku.items():
        # The product's own sales rate is what tells a return apart from a small
        # delivery: a refund is a fraction of a day's trade, a delivery is weeks of it.
        rate = None
        c = _catalog.get(sku) or {}
        df = c.get("df")
        if df is not None and len(df):
            recent = df.tail(60)
            if len(recent):
                rate = float(recent["y"].mean())
        found.extend(_ARR.detect(_slog.samples(sku), os_, daily_rate=rate))
    found.sort(key=lambda r: r["arrivedOn"])
    return {"suggested": found, "summary": _ARR.summarise(found),
            "tracking": _slog.coverage().get("samples", 0) > 0}


@app.get("/api/price-history")
def price_history(sku: str = Query(...), days: int = Query(default=365, ge=7, le=1500)):
    """What this product has sold at over time, and what that reveals about demand.

    Price history already arrives with every sale (Shopify reports it per line item), but
    nothing surfaced it. Without seeing it you can't tell whether an elasticity estimate
    is trustworthy or whether the price simply never moved.
    """
    c = _catalog.get(sku) or {}
    df = c.get("df")
    if df is None or not len(df) or "price" not in df.columns:
        return {"sku": sku, "series": [], "elasticity": None,
                "explain": "No price data has been loaded for this product."}
    d2 = df.tail(days).copy()
    series, last = [], None
    for _, r in d2.iterrows():
        try:
            p = float(r["price"])
        except (TypeError, ValueError):
            continue
        if pd.isna(p) or p <= 0:
            continue
        day = str(r["ds"])[:10]
        # One point per CHANGE, not per day — a year of an unchanged price is one line,
        # and the changes are what you actually want to look at.
        if last is None or abs(p - last) > 1e-9:
            series.append({"date": day, "price": round(p, 2),
                           "changePct": None if last is None else round((p - last) / last * 100, 1)})
            last = p
    elast = _UP.price_elasticity(d2)
    prices = [s["price"] for s in series]
    return {
        "sku": sku, "series": series, "changes": max(0, len(series) - 1),
        "current": prices[-1] if prices else None,
        "min": min(prices) if prices else None, "max": max(prices) if prices else None,
        "elasticity": elast,
        "explain": _UP.explain({}, elast),
    }


@app.get("/api/availability")
def availability_read(sku: str = Query(...), days: int = Query(default=60, ge=1, le=400)):
    """How long this product was actually buyable, day by day, and what that means for
    the demand figures. Sales on a sold-out day are a floor, not a fact."""
    end = _FL.today()
    start = end - _dt.timedelta(days=days - 1)
    series = _slog.availability_series(sku, start.isoformat(), end.isoformat())

    rows = []
    cat = _catalog.get(sku) or {}
    df = cat.get("df")
    if df is not None and len(df):
        avail = {r["date"]: r["hoursInStock"] for r in series}
        d2 = df.copy()
        d2["date"] = d2["ds"].dt.strftime("%Y-%m-%d") if "ds" in d2.columns else d2.get("date")
        for _, r in d2.iterrows():
            day = str(r.get("date"))
            if day < start.isoformat():
                continue
            rows.append({"date": day, "units_sold": float(r.get("y", r.get("units_sold", 0)) or 0),
                         "closing_stock": (float(r["units_in_stock"])
                                           if "units_in_stock" in d2.columns and pd.notna(r.get("units_in_stock"))
                                           else None),
                         "hours_in_stock": avail.get(day)})
    summary = _CEN.summarise(rows) if rows else None
    return {"sku": sku, "series": series, "summary": summary,
            "explain": _CEN.explain(summary) if summary else "",
            "coverage": _slog.coverage(sku),
            "tracking": _slog.coverage(sku).get("samples", 0) > 0}


@app.get("/api/livelog")
def livelog_read(sku: str | None = Query(default=None), limit: int = Query(default=500, ge=1, le=5000)):
    entries = _flog.for_sku(sku) if sku else _flog.all()
    entries = list(reversed(entries))[:limit]
    out = []
    for e in entries:
        d = dict(e)
        d["dueInDays"] = _FL.days_until_due(e)
        d["isDue"] = _FL.is_due(e)
        out.append(d)
    org = _FL.period_origin()
    return {"entries": out, "weeks": _FL.by_week(_flog.all()),
            "summary": _flog.summary(), "perSku": _FL.per_sku(_flog.all()),
            "cadence": _FL.WEEKLY,
            "currentOrigin": org.isoformat(),
            "currentPeriodEnds": (org + _dt.timedelta(days=6)).isoformat(),
            "nextOrigin": _FL.next_origin().isoformat(),
            "horizonDays": DEFAULT_LOG_HORIZONS[0]}


@app.post("/api/livelog/amend")
def livelog_amend(payload: dict = Body(default={})):
    """Revise a sealed week — used when a promotion is declared partway through it.

    The original figure is kept and the entry is marked amended, so the revision is
    visible rather than silent. Already-graded weeks are refused outright.
    """
    sku, week = payload.get("sku"), payload.get("week")
    reason = payload.get("reason") or "promotion declared mid-week"
    if not sku or not week:
        raise HTTPException(400, "sku and week are required")
    entry = next((e for e in _flog.for_sku(sku) if e.get("origin") == str(week)[:10]), None)
    if entry is None:
        raise HTTPException(404, "no entry for that product and week")
    if _FL.is_scored(entry):
        raise HTTPException(409, "that week is already graded")

    # Re-derive the week's forecast WITH the promotion now scheduled, over the days that
    # remain. Days already elapsed keep what was originally expected — the campaign
    # cannot lift sales that have already happened.
    e = _sku_cache.get(sku) or {}
    fc = e.get("future_fc")
    if fc is None or not len(fc):
        raise HTTPException(409, "no current forecast to revise from")
    h = int(entry.get("horizonDays") or 7)
    pred = _horizon_prediction(fc, h)
    if pred is None:
        raise HTTPException(409, "could not build a revised figure")

    with _flog._lock:                                   # noqa: SLF001 - same module family
        _flog._entries[entry["id"]] = _FL.amend(
            entry, pred["predicted"], reason, lo=pred["lo"], hi=pred["hi"])
        _flog.save()
    return {"amended": True, "entry": _flog._entries[entry["id"]]}


@app.post("/api/livelog/reset")
def livelog_reset():
    _flog.purge()
    return {"ok": True, "summary": _flog.summary()}

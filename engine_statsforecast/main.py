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
import datetime, io, os, json, math, re, threading, time
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta

from forecast_engine import (analyse_price, daily_price_rollup, price_evidence, price_bounds, extrapolation_distance, extrapolation_uncertainty, cap_price_ratio, MAX_PRICE_LIFT, PRICE_EXTRAP_FLOOR, PRICE_EXTRAP_CEIL, PRICE_EFFECT_UNC, BORROWED_EFFECT_UNC, STORE_BORROWED_EFFECT_UNC, classify_demand, compute_safe_price_range, fill_daily_gaps,
                             sparse_subtype, is_dormant as _FE_is_dormant,
                             is_off_season as _FE_off_season)
import router as R
import datetime as _dt
import forecast_log as _FL
import stock_log as _SL
import today_sales as _TS
import censoring as _CEN
import promos as _PROMO
import uplift as _UP
import seasonality as _SEAS
import bundles as _BUN
import arrivals as _ARR
import catalog_store as _STORE
import backtest_store as _BTSTORE
import sources as _SRC            # the live-store registry (Shopify, Square, …)
import scheduler as _SCHED        # when the nightly sync runs
import connections as _CONN       # saved credentials, so a token is entered once

# Bundle component map, {bundle_sku: {component_sku: qty}}. Session state like the rest of
# the catalogue — the real product reads this from Shopify's bundle definitions.
_bundle_map: dict = {}

# Instantiated at import: build_entry reads availability during a rebuild, which can be
# triggered before the endpoint definitions further down have been evaluated. Defining
# these at the bottom made that a NameError swallowed by a try/except — the feature would
# have looked wired while silently doing nothing.
_flog = _FL.ForecastLog()
_slog = _SL.StockLog()
# The day in progress, kept OUT of the catalogue on purpose — see today_sales.py.
_today_sales = _TS.TodaySales()
# What each product's shelf says right now, straight from the POS — see live_prices.py.
import live_prices as _LP
_live = _LP.LivePrices()
import holiday_calendar as _HOL
import rush as _RUSH
import workspace as _WS
# One-off rushes (rush.py) are left out of the training data. LOGITRACK_RUSH=0 switches it off.
RUSH_ON = os.environ.get("LOGITRACK_RUSH", "1").strip() not in ("0", "false", "no")
# Slow sellers' rates are worked out with the season taken out (see build_entry).
# LOGITRACK_SLOW_DESEASON=0 switches it off.
SLOW_DESEASON = os.environ.get("LOGITRACK_SLOW_DESEASON", "1").strip() not in ("0", "false", "no")
_holidays = _HOL.HolidaySettings()
import closed_days as _CD
import holiday_shape as _HS
import holiday_cover as _HC
import store_clock as _CLOCK
import bands as _BANDS
# Holidays are handled by holiday_shape: each holiday's stretch keeps the forecast's own
# total and only the day-by-day split follows past years. (The earlier layer, a +X% lift
# per holiday measured against the weeks around it, was measured worse and removed; so was
# flattening past stretches before training, which lifted the quiet days after a peak and
# forecast a December gift item +118% for January.) LOGITRACK_HOLIDAY_SHAPES=0 switches the
# stretches off, for comparison runs.
HOLIDAY_SHAPES = os.environ.get("LOGITRACK_HOLIDAY_SHAPES", "1").strip() not in ("0", "false", "no")
_hs_peer_cache: dict = {}
_closed = _CD.ClosedDays()
import exports
from engines import ProphetEngine, GlobalPooledEngine, IntermittentEngine, MovingAverageEngine
import engines as _ENGINES
# Days before Prophet fits a yearly pattern (engines.YEARLY_MIN_DAYS); read softly so a
# stand-in engines module in the tests still imports.
YEARLY_MIN_DAYS = int(getattr(_ENGINES, "YEARLY_MIN_DAYS", 450))


@asynccontextmanager
async def _lifespan(app):
    # Replaces the deprecated @app.on_event("startup") hook.
    _reconcile_workspace()
    _apply_store_zone()      # before warmup: every fit starts from "today"
    warmup()
    # The nightly sync is what makes the tool current without anyone asking. It is a daemon
    # thread, so it lives and dies with this process — which is the honest limit: keep the
    # backend running (a real box, or launchd on a Mac) or drive /api/sync/now from cron.
    _sync.start()
    st = _sync.status()
    if st["enabled"]:
        print(f"Nightly sync armed for {st['at']} {st['timezone']} (next: {st['nextRunAt']}).")
    # Hourly inventory readings. Started after the sync so a cold boot does its restore
    # first, and reported separately because a missed reading is unrecoverable while a
    # missed sync is merely late.
    _sampler.start()
    sst = _sampler.status()
    if sst["enabled"]:
        _mins = int(sst["intervalSeconds"] // 60)
        print(f"Stock sampling every {_mins} min (last filed: {sst['lastFiledAt'] or 'never'}).")
    yield
    _sampler.stop()
    _sync.stop()


app = FastAPI(lifespan=_lifespan)

# A numpy number (np.bool_, np.float64, ...) anywhere in a reply made FastAPI fail the whole
# request with a 500, which the page can only report as "Failed to fetch". Replies are
# built from pandas/numpy results in many places, so they're converted here once rather
# than trusting every one of them to cast.
try:
    import fastapi.encoders as _FE
    for _t in (np.bool_, np.int8, np.int16, np.int32, np.int64, np.uint8, np.uint16, np.uint32,
               np.uint64, np.float16, np.float32, np.float64):
        _FE.ENCODERS_BY_TYPE[_t] = lambda v: v.item()
except (ImportError, AttributeError):       # a test's stand-in for fastapi
    pass

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
# THE stock figure used when nobody has counted one. There used to be two different
# answers to this and they contradicted each other: the forecast path assumed 0 units and
# the scorecard path assumed 500, so the SAME product came back "about to stock out" on
# one screen and "overstocked" on the other. Both were confident, both were invented.
#
# 50 is deliberately unremarkable — small enough not to read as a warehouse, large enough
# not to read as an emergency. It exists so the arithmetic has something to chew on, and
# every surface that shows it must say it is synthetic. What actually protects the user is
# not the number: it is that an UNCOUNTED product publishes no stockout date, no reorder
# countdown and no order quantity, because all three would be fiction.
ASSUMED_STOCK = 50


# ── The catalogue is PUBLISHED, never edited in place ───────────────────────────────
#
# _rebuild used to write `_sku_cache[sku_id] = entry` one product at a time. The readers
# that aggregate across the catalogue — /api/skus, the Fleet summary, the Scorecard,
# grouping, the exports — deliberately do NOT take _state_lock, because a full refit holds
# it for minutes and blocking every page load for that long is worse than the bug it fixes.
#
# The consequence, once the nightly sync started running on a background thread: a refit
# landing under a reader served a HALF-SWAPPED catalogue. Some products refit, some not,
# and totals summed across both. Silent, rare, and unreproducible after the fact — the
# worst shape a bug can have.
#
# So the catalogue is now immutable once published. A writer copies it, edits the copy,
# and rebinds the name in a single statement; a reader binds it ONCE through _cache() and
# reads from that snapshot for the rest of its work. A reader therefore sees the whole old
# catalogue or the whole new one, never a mixture, and never waits for a refit.
#
# The rule, for anything added later: never mutate the dict _cache() hands you, and never
# read _sku_cache twice in one request — the second read may be a different catalogue.
# `x in _cache()` followed by `_cache()[x]` is two catalogues and can raise KeyError.
# At a few hundred products the copy is a pointer copy: microseconds, once per refit.
def _cache() -> dict[str, dict]:
    """The current catalogue, as a consistent snapshot. Bind once, then read from it."""
    return _sku_cache


def _publish_cache(new: dict[str, dict]) -> None:
    """Swap a newly-built catalogue in, atomically. _state_lock is reentrant, so this is
    correct whether or not the caller already holds it."""
    global _sku_cache
    with _state_lock:
        _sku_cache = new


# How often a COLD boot publishes partial progress. Only ever used when the catalogue
# started empty, where there is no old-vs-new mixture to create and showing products as
# they arrive is what makes a restore feel alive instead of broken.
_PUBLISH_EVERY = 25
_catalog:  dict[str, dict] = {}           # sku_id → {df, attrs, sku_name, mode, filename, events}
# ── Backtest state — session-scoped by design ────────────────────────────────
# Nothing here is written to disk or restored. Every launch starts clean: load a sheet,
# a backtest runs against it, and its results live only for that session — so a run can
# never be contaminated by a previous one, and there are no stale files to reason about.
_backtest_tier_cache: dict[str, dict] = {}  # "sku|lead|coverage" → measured protection tier
_sheet_costs: dict[str, float] = {}         # sku_id → unit cost seen at ingest (sheet/Shopify)
# Where each of those costs came from ("sheet", "shopify", "square", …). Computed at
# ingest and then THROWN AWAY, which is why every restart re-labelled a Square cost as
# coming from a spreadsheet — on the one screen whose job is saying where a number came
# from. Persisted alongside the costs so the label survives a restart with them.
_cost_sources: dict[str, str] = {}          # sku_id → provenance of _sheet_costs[sku_id]
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
                  "cost_sources": {k: str(v) for k, v in _cost_sources.items()},
                  "holding_pct": int(_session_holding_pct)}
        _STORE.save(_catalog, extras)
    except Exception as e:                                   # noqa: BLE001
        print(f"[catalog_store] snapshot skipped: {e}")


# Fingerprint of the catalog the in-memory backtest was measured against. Without it,
# a sync that does NOT auto-run a new test would leave measured tiers in memory that were
# measured on data no longer loaded — the exact contamination the on-disk fingerprint
# prevents, happening one level up.
_backtest_fingerprint: str | None = None
# The same snapshot, addressable per product. `grew_only` needs the parts, not the hash:
# "different" and "invalidated" are not the same question.
_backtest_parts: dict = {}


def _persist_backtest():
    """Snapshot the completed run, stamped with the data it was measured against."""
    try:
        globals()["_backtest_fingerprint"] = _BTSTORE.fingerprint(_catalog)
        globals()["_backtest_parts"] = _BTSTORE.fingerprint_parts(_catalog)
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
    _sync_holiday_rates(saved.get("report"))
    _backtest_inputs.clear(); _backtest_inputs.update(saved.get("inputs") or {})
    globals()["_backtest_fingerprint"] = _BTSTORE.fingerprint(_catalog)
    globals()["_backtest_parts"] = _BTSTORE.fingerprint_parts(_catalog)
    return True


def _drop_backtest_state(why: str) -> None:
    """Forget the measured run. Called when the data it described is gone."""
    globals()["_last_backtest"] = None
    globals()["_last_backtest_rows"] = None
    globals()["_last_backtest_combos"] = []
    globals()["_backtest_tier_cache"] = {}
    globals()["_backtest_exclusions"] = {}
    globals()["_backtest_fingerprint"] = None
    globals()["_backtest_parts"] = {}
    _HC.set_rates(None)
    _backtest_inputs.clear()
    _BTSTORE.clear()
    print(f"Measured protection levels dropped — {why}. Estimates apply until a new test runs.")


def _forget_backtest_memory() -> None:
    """Forget the IN-MEMORY run only, leaving what's on disk alone. Used when the loaded
    workspace changes: the run in memory described the products that were just set
    aside, and its tiers must not be read (or re-saved) against another workspace's."""
    globals()["_last_backtest"] = None
    globals()["_last_backtest_rows"] = None
    globals()["_last_backtest_combos"] = []
    globals()["_backtest_tier_cache"] = {}
    globals()["_backtest_exclusions"] = {}
    globals()["_backtest_fingerprint"] = None
    globals()["_backtest_parts"] = {}
    _HC.set_rates(None)
    _backtest_inputs.clear()


# Bumped every time the loaded products are set aside or swapped for another workspace's.
# A backtest records it when it snapshots the catalog and throws its result away if it
# moved: a run measured on store A's products must never be saved into store B's.
_ws_epoch = 0


def _bump_ws_epoch() -> None:
    globals()["_ws_epoch"] = _ws_epoch + 1


def _bt_stamp() -> tuple:
    """What a backtest was measured against: (workspace epoch, per-product fingerprint).
    Call under _state_lock, at the same moment the catalog is snapshotted."""
    return (_ws_epoch, _BTSTORE.fingerprint_parts(_catalog))


def _bt_stamp_still_valid(stamp: tuple) -> bool:
    """The run can be kept: same workspace, and its data only grew (a sync that added
    days) rather than being rewritten or replaced. Call under _state_lock."""
    ep, parts = stamp
    if ep != _ws_epoch:
        return False
    now = _BTSTORE.fingerprint_parts(_catalog)
    return parts == now or _BTSTORE.grew_only(parts, now)


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


def _current_prices() -> dict:
    """{sku: today's regular shelf price}, the price the protection level is chosen for
    (the live list price when the store reports one, else the last recorded price, moved
    by any price change already in force)."""
    out = {}
    for sid, e in (_cache() or {}).items():
        p = e.get("regular_price") or e.get("effective_price") or e.get("last_price")
        try:
            if p is not None and float(p) > 0:
                out[str(sid)] = round(float(p), 2)
        except (TypeError, ValueError):
            continue
    return out


# A shelf price has to move by more than this before the measured tiers are re-priced.
PRICE_DRIFT_TOLERANCE = 0.01


def _reprice_backtest_if_price_moved() -> bool:
    """Re-price the stored run when a product's shelf price has moved since.

    The backtest's money (lost profit per unit short) is margin, and margin is price
    minus cost. Cost and fee edits already re-priced the run; a price change never did,
    so a product whose price went up kept a protection level chosen for the old margin.
    Re-pricing is arithmetic over the stored windows (backtest.recost), not a refit."""
    global _last_backtest_rows
    try:
        with _state_lock:
            rows = _last_backtest_rows
            if rows is None or not len(rows) or not _last_backtest or "price" not in rows.columns:
                return False
            now = _current_prices()
            last = (rows.sort_values("cutoff").groupby(rows["sku"].astype(str))["price"].last()
                    if "cutoff" in rows.columns else rows.groupby(rows["sku"].astype(str))["price"].last())
            moved = [sid for sid, p in now.items()
                     if sid in last.index and float(last[sid] or 0) > 0
                     and abs(p - float(last[sid])) / float(last[sid]) > PRICE_DRIFT_TOLERANCE]
            if not moved:
                return False
            BT = _bt()
            inputs = _backtest_inputs or {}
            econ = {sid: {"price": p} for sid, p in now.items()}
            for sid, c in (inputs.get("costs") or {}).items():
                if sid in econ and c:
                    econ[sid].update({"cost": c, "fees": (inputs.get("fees") or {}).get(sid, 0.0)})
            holding = inputs.get("holdingPct")
            holding = int(holding) if holding is not None else int(_session_holding_pct)
            sink = []
            res = BT.recost(_last_backtest, rows, econ, holding_pct=holding,
                            combos=_last_backtest_combos or None, rows_sink=sink)
            if sink:
                _last_backtest_rows = sink[0]
            _cache_backtest_tiers(res)
            _record_backtest_inputs({**{k: v for k, v in econ.items() if v.get("cost")}}, holding)
            _store_backtest_result(res, "reprice")
            _persist_backtest()
            print(f"Shelf price moved for {len(moved)} product(s); measured protection levels re-priced.")
            return True
    except Exception as exc:                                  # noqa: BLE001 — never fail a refit over it
        print(f"Re-pricing the backtest skipped ({exc}).")
        return False


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
        # Merge rather than replace: the sheet knows a cost and never knows a fee, so a
        # payload carrying only a fee used to drop the cost, and one carrying only a
        # cost used to drop the fee back to zero. Both halves survive now.
        base = merged.get(str(sid), {})
        merged[str(sid)] = {**base, **{k: x for k, x in (v or {}).items() if x is not None}}
    # Today's shelf price travels with the costs, so every window is valued at the margin
    # the product earns NOW (see backtest._run_cutoff).
    for sid, p in _current_prices().items():
        merged.setdefault(sid, {})["price"] = p
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


# "Today" is evaluated FRESH on every call (never frozen at import), on the STORE's
# clock: see store_clock.py and _apply_store_zone below. _APP_TZ mirrors that zone for
# the code that reads it directly.
_APP_TZ = _CLOCK.DEFAULT_ZONE
_CLOCK.set_zone(_APP_TZ)


def set_timezone(tz):
    """Kept so older pages that still send their browser's zone don't break. It no longer
    sets anything: the viewer's zone isn't the store's (someone checking from another
    city, or two people in different zones on a hosted server, used to move "today" for
    everyone). The store's zone comes from _apply_store_zone."""
    return None


def _resolve_store_zone() -> tuple:
    """(zone, source) for what's loaded now. In order: the zone picked on the Closed days
    panel ("set"), then the one the connected store reports ("square" / "shopify"), then
    Eastern Time ("default")."""
    try:
        z = _closed.get_zone(_current_slot())
    except Exception:                                   # noqa: BLE001 — never block on this
        z = {}
    if _CLOCK.valid(z.get("set")):
        return z["set"], "set"
    if _CLOCK.valid(z.get("detected")):
        return z["detected"], (z.get("detectedFrom") or "store")
    return _CLOCK.DEFAULT_ZONE, "default"


def _apply_store_zone() -> str:
    """Point the app's clock at the store's zone. Called at startup, when the loaded
    workspace changes, when a store reports its zone, and when the zone is picked."""
    global _APP_TZ
    zone, _src = _resolve_store_zone()
    _CLOCK.set_zone(zone)
    _APP_TZ = zone
    return zone


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
        return (f"The shaded range is an 80% band built from this engine's backtest errors: about "
                f"8 in 10 actual daily sales land inside it (≈ +{q_hi:.1f} / {q_lo:.1f} units around the line). "
                f"Every product's band is built the same way.")
    return (f"The shaded range is an 80% band from a standard statistical estimate "
            f"(±{q_hi:.1f} units around the line).")


# ── How steady demand is, judged against what is achievable at this sales volume ──
#
# This signal used to be the residual CV: forecast error / average daily units, graded
# at fixed cuts of 0.30 and 0.60. That reads sensibly and is almost entirely a measure
# of sales VOLUME. Unit sales are counts, and counts carry irreducible randomness — a
# product averaging n a day swings by roughly √n even when underlying demand is
# perfectly steady — so the ratio has a hard floor of 1/√n. Inverting the cuts:
# "highly variable" began at 0.60, i.e. anything selling under ~2.8 a day, and "very
# consistent" needed 0.30, i.e. ~11+ a day. Over the catalogue this was built against,
# 26 of 30 scored products read "highly variable" and NOT ONE could ever have reached
# "very consistent" however regular its sales were. A bar that says the same thing
# about nearly every product is not telling anyone anything.
#
# Dividing by √mean instead of mean removes exactly that floor. What is left is the
# index of dispersion (σ/√μ, the square root of the variance-to-mean ratio):
#
#     1.0   as steady as a product selling this much can possibly be
#     1.5   swinging half again wider than the floor
#     2.0   variance four times the mean, the conventional marker for real overdispersion
#
# so a half-a-day line and a fifty-a-day line are finally judged on the same footing.
# The cuts below are points on that scale, not a fit to any one catalogue.
#
# residual_cv is untouched and still reported: it is the right shape for the backtest's
# diagnostics, and σ (which sizes every safety buffer) never went through this at all.
DISPERSION_STEADY   = 1.25   # at or under: as steady as this volume allows
DISPERSION_VARIABLE = 2.00   # over: genuinely erratic, beyond counting noise
DISPERSION_FLOOR    = 3.50   # where the bar bottoms out
DISPERSION_MIN_UNITS = 20    # under this many units in the window, decline to judge


def demand_dispersion(residual_std, ref_mean, ref_units):
    """σ/√μ, or None when there is too little selling to say anything honest.

    A product that sold nine units all year has no measurable steadiness; forcing a
    verdict out of it would be inventing one. The scorecard shows those as "n/a"."""
    try:
        sd = float(residual_std); mu = float(ref_mean)
    except (TypeError, ValueError):
        return None
    if not (sd >= 0) or mu <= 0 or float(ref_units or 0) < DISPERSION_MIN_UNITS:
        return None
    return round(sd / (mu ** 0.5), 4)


def dispersion_label(d):
    if d is None: return "n/a"
    if d <= DISPERSION_STEADY:   return "very consistent"
    if d < DISPERSION_VARIABLE:  return "moderately consistent"
    return "highly variable"


def volatility_label(d):
    """Same cuts, said in the direction the bar is named for. A row called volatility
    whose values read "very consistent" makes the reader work out which end is bad."""
    if d is None: return "not enough sales to say"
    if d <= DISPERSION_STEADY:   return "steady"
    if d < DISPERSION_VARIABLE:  return "somewhat volatile"
    return "highly volatile"


def _volatility_tip(d, rate):
    base = ("Swing in daily sales, measured against the randomness any product this size "
            "carries anyway. 1.0x is as steady as that sales volume permits; past 2.0x it "
            "moves more than its size explains.")
    if d is None:
        return "Too few sales in the measured window to judge volatility. " + base
    return f"This one swings {d:.1f}x that floor{'' if not rate else f', on about {rate:.1f} a day'}. " + base


def _consistency_tip(dispersion, ref_mean):
    """Hover text on the Sales consistency bar. Short, and it names the yardstick —
    a number with no yardstick is what made the old bar unreadable."""
    base = ("How much day-to-day swing is left once you allow for the randomness any product "
            "this size carries. 1.0x is as steady as that sales volume permits; past 2.0x it "
            "moves more than its size explains.")
    if dispersion is None:
        return "Too few sales in the measured window to judge steadiness. " + base
    rate = "" if not ref_mean else f", on about {ref_mean:.1f} a day"
    return f"This one swings {dispersion:.1f}x the floor{rate}. " + base


def _consistency_text(label, dispersion, ref_mean):
    """Say what the verdict rests on. Selling three a day and swinging by two is not
    erratic, it is what selling three a day looks like; the old wording quoted a raw
    ratio that made every small product sound out of control."""
    tail = ("The safety buffer is sized from how far the forecast misses over a full "
            "lead-time window, so it covers clumpy stretches too.")
    if dispersion is None:
        return "Too few sales in the measured window to judge how steady demand is. " + tail
    rate = "" if not ref_mean else f" (about {ref_mean:.1f} a day)"
    if dispersion <= DISPERSION_STEADY:
        return (f"Day-to-day demand is {label}. Its swing is {dispersion:.1f}x the smallest possible at "
                f"this sales volume{rate}, so most of the movement is ordinary counting noise. " + tail)
    return (f"Day-to-day demand is {label}. Its swing is {dispersion:.1f}x the smallest possible at this "
            f"sales volume{rate}, so it moves more than its size alone explains. " + tail)


def _make_details(method, reason, model_label, demand_class, residual_cv, q_lo, q_hi, bits, n_obs,
                  dispersion=None, ref_mean=None):
    d_label, d_desc = DEMAND_LABELS.get(demand_class, (demand_class, ""))
    cv = float(residual_cv)
    cons = dispersion_label(dispersion)
    calibrated = method in ("prophet", "global", "croston")
    details = {
        "model": {"name": method, "label": model_label, "summary": f"{model_label}: {d_label.lower()}"},
        "demand": {"class": demand_class, "label": d_label, "description": d_desc},
        "route": method, "routeLabel": R.ROUTE_LABELS.get(method, method),
        "whyChosen": reason,
        "consistency": {"residualCv": round(cv, 3), "label": cons,
                        "dispersion": dispersion,
                        "text": _consistency_text(cons, dispersion, ref_mean)},
        "interval": {"level": 80, "method": ("conformal" if calibrated else "normal-approx"),
                     "lowOffset": round(q_lo, 1), "highOffset": round(q_hi, 1),
                     "text": _conformal_text(q_lo, q_hi, calibrated)},
        "backtestText": "Accuracy/uncertainty is measured on history held out from fitting.",
        "dataPoints": int(n_obs),
    }
    if method == "abstain":
        details["caution"] = (
            "Last-resort estimate. This product has too little sales history, and too few closely "
            "matching products to borrow a seasonal pattern from, so its everyday level is a flat "
            "recent average. Holidays and price changes still apply where similar products show them. "
            "It switches to a proper method once it builds history or is grouped with similar "
            "products in the Grouping tab.")
    details.update(bits or {})          # seasonality / price / (level) from the engine
    return details


def _effective_price(last_price, events=None, today=None):
    """The current price: the last price a day actually SOLD at, on its own.

    This used to be overridden by any logged event dated on or before today, so typing a
    price change into the forecast tool rewrote what the page reported as the price you
    are charging — a planning input editing the record of what happened. Events are for
    the forecast only. What was charged comes from recorded sales and nothing else; if an
    event and the register disagree, the register is right about the past.

    `events` and `today` are accepted and ignored so no caller can reintroduce the old
    behaviour by accident."""
    return None if last_price is None else float(last_price)


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


def _levels_text(ev) -> str:
    lv = (ev or {}).get("levels") or []
    if not lv:
        return "no single-price days yet"
    return ", ".join(f"${t['price']:.2f} ({t['days']} day{'s' if t['days'] != 1 else ''}, {t['quality']})"
                     for t in sorted(lv, key=lambda t: -t["days"]))


def _price_response(method, eng, has_price, price_varied, elast, evidence=None, bounds=None,
                    borrowed=None, borrowed_range=None, borrowed_bounds=None, known_range=None) -> dict:
    """Will a price change move THIS product's forecast — and if not, why not?

    There are two unrelated mechanisms behind "price affects the forecast", one per kind
    of engine, and each can decline for its own reasons. Before this, declining was
    SILENT: you added a price cut, saved, and nothing happened, with no way to tell a
    deliberate refusal from a bug. Every refusal below is a real, defensible decision;
    the failure was only that none of them was ever said out loud.

    Returns {applies, reason, text, ...}. `applies` is the one bit the UI needs to decide
    whether to warn; `text` is the sentence it shows.
    """
    if not has_price:
        return {"applies": False, "reason": "no-price-data",
                "text": "No price data for this product, so price changes can't affect its forecast."}
    ev = evidence or {}
    mixed = int(ev.get("mixedDays") or 0)
    mixed_note = (f" {mixed} day{'s' if mixed != 1 else ''} sold at more than one price and "
                  f"{'aren' if mixed != 1 else 'isn'}'t counted.") if mixed else ""
    own_backwards = bool(ev.get("usable")) and (elast or {}).get("basis") == "borrowed"
    _who = ("other products in the store" if (borrowed or {}).get("scope") == "store"
            else "similar products")
    _ob = (elast or {}).get("ownBasis")
    _why_own = ("Its own prices have too few days with sales to show how demand responds."
                if _ob == "too-few-points" else
                "This one sold more at its higher price, so something other than price drove its "
                "sales and its own prices can't be used.")
    if borrowed and ((evidence is not None and not ev.get("usable")) or own_backwards):
        el = float(borrowed["elasticity"])
        lift = round(((0.9 ** el) - 1) * 100)
        need_d = ev.get("minDays", 30); need_n = ev.get("levelsRequired", 2)
        rng = ((f" It has sold at ${borrowed_range[0]:.2f} to ${borrowed_range[1]:.2f}; past that the "
                f"response eases off the further you go.") if (borrowed_range and ev.get("usable")) else
               (f" Those products have sold at ${borrowed_range[0]:.2f} to ${borrowed_range[1]:.2f} in "
                f"today's terms; past that the response eases off the further you go.")
               if borrowed_range else "")
        return {"applies": True, "reason": "borrowed", "elasticity": el, "peers": borrowed["peers"],
                "bounds": list(borrowed_bounds) if borrowed_bounds else None, "levels": ev.get("levels"),
                "knownRange": list(borrowed_range) if borrowed_range else None,
                "mixedDays": mixed,
                "scope": borrowed.get("scope") or "category",
                "text": ((f"Borrowed from {borrowed['peers']} {_who}. {_why_own} "
                          f"A 10% price cut lifts units about {lift}%. "
                          f"The band is wider because it's borrowed.{rng}") if own_backwards else
                         (f"Borrowed from {borrowed['peers']} {_who}, because this one doesn't have "
                          f"{need_n} prices with {need_d}+ days yet: a 10% price cut lifts units about {lift}%. "
                          f"The band is wider because it's borrowed.{rng}"))}
    e = elast or {}
    if e.get("basis") == "measured" and e.get("elasticity"):
        # The product's own cross-checked reading (uplift.price_reading), on any route.
        el = float(e["elasticity"])
        lift = round(((0.9 ** el) - 1) * 100)
        how = e.get("method")
        _kr = known_range or ((ev.get("rangeLo"), ev.get("rangeHi")) if ev.get("rangeLo") is not None else None)
        _n = int(e.get("changes") or 0)
        _chg = f"{_n} price change{'s' if _n != 1 else ''}"
        if how == "both":
            src = (f"Measured two ways from its own sales, the same months across years and the weeks "
                   f"before vs after its {_chg}, and they agree")
        elif how == "before-after":
            src = (f"Measured from its own {_chg}, comparing the weeks before and after each one "
                   f"against products whose price didn't change then")
        else:
            src = (f"Measured from its own sales, comparing the same months across years "
                   f"({e.get('points')} single-price days with sales)")
        clamp = ""
        if _kr and bounds:
            clamp = (f" Known range: ${_kr[0]:.2f} to ${_kr[1]:.2f}. Outside it the response is extrapolated "
                     f"in percentages and eases off the further you go, approaching {MAX_PRICE_LIFT:g}× on cuts "
                     f"and -80% on rises, with more safety stock. Prices below ${bounds[0]:.2f} or above "
                     f"${bounds[1]:.2f} count as those limits.")
        return {"applies": True, "reason": "measured", "method": how, "elasticity": el,
                "sameMonths": e.get("sameMonths"), "beforeAfter": e.get("beforeAfter"),
                "bounds": list(bounds) if bounds else None,
                "knownRange": list(_kr) if _kr else None, "levels": ev.get("levels"),
                "mixedDays": mixed,
                "text": f"{src}: a 10% price cut lifts units about {lift}%, and a price change shifts the forecast to match.{clamp}"}
    if e.get("basis") == "readings-disagree":
        def _pct(x):
            return f"{round(((0.9 ** float(x)) - 1) * 100)}%"
        return {"applies": False, "reason": "readings-disagree",
                "sameMonths": e.get("sameMonths"), "beforeAfter": e.get("beforeAfter"),
                "levels": ev.get("levels"), "mixedDays": mixed,
                "text": (f"Price isn't used. Its two readings disagree: the same months across years say a 10% "
                         f"cut lifts units about {_pct(e['sameMonths'])}, the weeks around its price changes say "
                         f"about {_pct(e['beforeAfter'])}. Something besides price is moving its sales, so "
                         f"neither can be trusted yet.")}
    if evidence is not None and not ev.get("usable"):
        need_d = ev.get("minDays", 30); need_n = ev.get("levelsRequired", 2)
        if ev.get("reason") == "levels-too-close":
            return {"applies": False, "reason": "levels-too-close", "levels": ev.get("levels"),
                    "mixedDays": mixed,
                    "text": (f"Price isn't used yet. The prices with {need_d}+ days "
                             f"({_levels_text({'levels': ev.get('qualifying')})}) are too close "
                             f"together to show how demand responds.{mixed_note}")}
        return {"applies": False, "reason": "too-few-price-levels", "levels": ev.get("levels"),
                "mixedDays": mixed, "need": need_d, "levelsNeeded": need_n,
                "text": (f"Price isn't used yet. It needs {need_n} prices with {need_d}+ days each, or a "
                         f"price change with a week or more of sales on each side. "
                         f"So far: {_levels_text(ev)}.{mixed_note}")}
    clamp_note = ""
    if bounds is not None and ev.get("rangeLo") is not None:
        clamp_note = (f" Known range: ${ev['rangeLo']:.2f} to ${ev['rangeHi']:.2f}. Outside it the response is "
                      f"extrapolated in percentages and eases off the further you go, approaching "
                      f"{MAX_PRICE_LIFT:g}× on cuts and -80% on rises, with more safety stock. Prices below "
                      f"${bounds[0]:.2f} or above ${bounds[1]:.2f} count as those limits.")
    # Only when Prophet was actually given price. It isn't when the product's own prices
    # read backwards (_own_price_ok in build_entry); that case is answered below, the same
    # way on every route.
    if method == "prophet" and (getattr(eng, "has_price", False) or getattr(eng, "_price_inverted", None)):
        inv = getattr(eng, "_price_inverted", None)
        if inv:
            return {"applies": False, "reason": "inverted", "detail": inv,
                    "text": ("Price isn't used. This product sold more at its higher prices, so "
                             "something other than price drove its sales and its prices can't "
                             "show how demand responds.")}
        if not price_varied:
            return {"applies": False, "reason": "price-never-varied",
                    "text": ("Price isn't used: it has never changed in this product's sales "
                             "history, so there's nothing to learn from.")}
        return {"applies": True, "reason": "modelled", "bounds": list(bounds) if bounds else None,
                "levels": ev.get("levels"), "mixedDays": mixed,
                "text": ("Price is an input to this product's model, so this change shifts the forecast."
                         + clamp_note)}

    basis = e.get("basis") or "none"
    need = getattr(_UP, "MIN_PRICE_POINTS", 30)
    if basis == "too-few-points":
        return {"applies": False, "reason": basis, "points": e.get("points"), "need": need,
                "text": (f"Price isn't used yet. Only {e.get('points', 0)} days with sales carry a "
                         f"price, and {need} are needed to measure how demand responds.")}
    if basis == "price-never-varied":
        return {"applies": False, "reason": basis,
                "text": ("Price isn't used: it has barely changed in this product's sales "
                         "history, so there's nothing to learn from.")}
    if basis == "positive-elasticity-ignored":
        return {"applies": False, "reason": basis,
                "text": ("Price isn't used. This product sold more at its higher prices, so "
                         "something other than price drove its sales and its prices can't "
                         "show how demand responds.")}
    return {"applies": False, "reason": basis,
            "text": "This product's forecast doesn't respond to price changes."}


_closed_memo: dict = {"snap": None, "days": frozenset()}


def _store_closed_days() -> frozenset:
    """Days nothing sold anywhere in the store (every product on file sold 0) while the
    store normally sells: the store was closed. Worked out once per set of products."""
    # The raw catalogue, not the published one: this is asked while products are being
    # (re)built, before anything is published.
    ents = list(_catalog.values())
    snap = tuple(sorted(id(e.get("df")) for e in ents))
    if _closed_memo["snap"] == snap:
        return _closed_memo["days"]
    frames = [e["df"][["ds", "y"]] for e in ents
              if hasattr(e.get("df"), "columns") and len(e["df"])]
    days = frozenset()
    if len(frames) >= 3:
        g = pd.concat(frames).groupby("ds")["y"].agg(["sum", "count"])
        if float(g["sum"].median()) >= 5.0:
            days = frozenset(pd.to_datetime(g.index[(g["sum"] <= 0) & (g["count"] >= 3)]).date)
    _closed_memo.update({"snap": snap, "days": days})
    return days


def _hs_who(n, info):
    """Who a holiday pattern is steadied by, in words: the category, the store products
    that move with it, or (when that's unknown, e.g. in a backtest) similar products."""
    info = info or {}
    cat = info.get("category")
    s = "" if n == 1 else "s"
    eq = ", none counting for more than a typical one" if n > 1 else ""
    if info.get("scope") == "category" and cat:
        return f"{n} other {cat} product{s}{eq}"
    if info.get("scope") == "store":
        alone = f"it's the only {cat} product" if cat else "it has no category"
        mv = "moves" if n == 1 else "move"
        return f"{n} product{s} elsewhere in the store that {mv} with it ({alone}){eq}"
    return f"{n} similar product{s}"


def _hs_report(df, future_fc, lv, own, off, to_real, settings, shift=0, peer_info=None):
    """What the product page shows under Holidays (holiday_shape): for every holiday, its
    next stretch day by day (units already sold, then this forecast's units, the same
    numbers the chart shows), how its busiest and quietest days compare with a typical day
    of the stretch, and the same stretch in past years for comparison."""
    fut = {}
    for x, v in zip(future_fc["ds"], future_fc["yhat"]):
        fut[to_real(pd.Timestamp(x).date())] = max(float(v), 0.0)
    hist = {to_real(pd.Timestamp(x).date()): float(v) for x, v in zip(df["ds"], df["y"])}
    if not fut:
        return None
    f0, f1 = min(fut), max(fut)
    closed = _closed_real()
    A = _HS.assign(f0 - datetime.timedelta(days=40), f1 + datetime.timedelta(days=40), settings)
    rows = []
    for key, name, _rule, before, after, core_label in _HS.EVENTS:
        occ = sorted((o for o in _HS.occurrences(f0 - datetime.timedelta(days=30), f0 + datetime.timedelta(days=400),
                                                 settings) if o["key"] == key and o["span"][1] >= f0),
                     key=lambda o: o["anchor"])
        if not occ:
            continue
        o = occ[0]
        a_, b_ = o["core"]
        span = [o["span"][0] + datetime.timedelta(days=i) for i in range((o["span"][1] - o["span"][0]).days + 1)]
        mine = [d for d in span if (A.get(d) or (None,))[0] == key and A[d][1] == o["anchor"]]
        L = (lv or {}).get(key) or {}
        r = L.get("r") or {}
        levels_ = [r.get((d - o["anchor"]).days, 1.0) for d in mine]
        med = float(np.median(levels_)) if levels_ else 1.0
        days, total, core_units, sold, partial = [], 0.0, 0.0, 0.0, False
        for d, lvl in zip(mine, levels_):
            is_past = d < f0
            u = hist.get(d) if is_past else fut.get(d)
            if d in closed:
                u = 0.0
            if u is None and not is_past:
                partial = True
            days.append({"date": d.isoformat(), "units": (round(u, 1) if u is not None else None),
                         "sold": bool(is_past), "closed": d in closed, "core": a_ <= d <= b_,
                         "x": round(lvl / med, 2) if med > 0 else 1.0})
            if u is not None:
                total += u
                if a_ <= d <= b_:
                    core_units += u
                if is_past:
                    sold += u
        # The same days in past years, lined up on the holiday: only the days this year's
        # stretch has, and only years that had nearly all of them (another holiday may have
        # taken some that year), so a comparison is never 7 days against 4.
        _offs = {(d - o["anchor"]).days: (a_ <= d <= b_) for d in mine}
        past = []
        for yv in ((own or {}).get(key) or {}).get("years") or []:
            bo = yv.get("byOff") or {}
            common = [q for q in _offs if q in bo]
            if not _offs or len(common) < 0.8 * len(_offs):
                continue
            # Why a day is missing matters to whoever reads the comparison: the sales
            # history may simply stop before it (the most recent year, usually), or
            # another holiday may have claimed it that year.
            try:
                _anc = datetime.date.fromisoformat(str(yv["anchor"])[:10])
                _nofile = sum(1 for q in _offs if q not in bo
                              and (_anc + datetime.timedelta(days=q)) not in hist)
            except Exception:                               # noqa: BLE001
                _nofile = 0
            past.append({"year": int(yv["anchor"][:4]), "start": yv["start"], "end": yv["end"],
                         "units": round(sum(bo[q] for q in common), 1),
                         "coreUnits": round(sum(bo[q] for q in common if _offs[q]), 1),
                         "days": len(common), "allDays": len(common) == len(_offs),
                         "ofDays": len(_offs), "missingNoData": int(_nofile),
                         "missingOther": int(len(_offs) - len(common) - _nofile),
                         "weight": yv.get("weight")})
        # "× a typical day" is read off the units the bars draw, so the tallest bar is the
        # busiest day and a taller bar never reads as a smaller multiple. It used to come
        # from the holiday's shape alone, before the weekday pattern, the blend into the
        # stretch's edges and any promotion, so a Saturday could stand taller than a day
        # marked 1.58x. Days past the end of the forecast (no units) keep the shape's
        # figure and are left out of busiest/quietest. "Typical" is the stretch's average
        # day, the same yardstick the past years below use.
        _u = [dd["units"] for dd in days if not dd["closed"] and dd["units"] is not None]
        _med_u = float(np.mean(_u)) if _u else 0.0
        if _med_u > 0:
            for dd in days:
                if dd["units"] is not None and not dd["closed"]:
                    dd["x"] = round(dd["units"] / _med_u, 2)
        xs = [(dd["x"], dd["date"]) for dd in days
              if not dd["closed"] and (_med_u <= 0 or dd["units"] is not None)]
        hi = max(xs) if xs else (1.0, None)
        lo = min(xs) if xs else (1.0, None)
        n_own, n_pool = int(L.get("years") or 0), int(L.get("pool") or 0)
        if key in (off or ()):
            status = "off"
        elif not mine:
            status = "merged"
        elif not L or (n_own == 0 and n_pool < 2):
            status = "new"
        elif _HS.significant(L):
            status = "moves"
        else:
            status = "flat"
        last = past[-1] if past else None
        # No percentage when the history was moved to end yesterday: this year's total
        # comes from the moved seasons and the past years from the real dates, so the two
        # sit on different parts of the calendar and the gap between them is the move,
        # not the product. (A +127% on a flat product is what it produced.)
        vs = (round((total - last["units"]) / last["units"] * 100)
              if (last and last["units"] > 0 and last["allDays"] and not partial and not sold
                  and not shift) else None)
        merged_into = merged_key = None
        if not mine:
            _o = next((A.get(o["core"][0]) for _ in (0,) if A.get(o["core"][0])), None)
            merged_into = _HS.name_of(_o[0]) if _o else None
            merged_key = _o[0] if _o else None
        _none = (peer_info or {}).get("scope") == "none"
        # Where the day-by-day pattern comes from, in parts the page can lay out: this
        # product's own past years and how much they weigh on the holiday's own days, and
        # which similar products steady it and how much of their evidence each one is.
        own_pct = float(L.get("ownShare") or 0.0) if L else 0.0
        shares = list(L.get("peerShares") or []) if L else []
        basis = {"ownYears": n_own, "ownPct": round(own_pct) if (n_own or n_pool) else None,
                 "peerPct": (round(100 - own_pct) if n_pool else 0),
                 "peers": shares[:12], "peerCount": n_pool,
                 "scope": (peer_info or {}).get("scope"), "category": (peer_info or {}).get("category"),
                 "alone": _none}
        if n_own and n_pool:
            text = (f"From {n_own} past year{'s' if n_own != 1 else ''} of this product's sales "
                    f"({basis['ownPct']}% of the pattern on the holiday's days), steadied by "
                    f"{_hs_who(n_pool, peer_info)} ({basis['peerPct']}%)")
        elif n_own:
            text = (f"From {n_own} past year{'s' if n_own != 1 else ''} of this product's sales only"
                    + ("; no similar product has this holiday in its history" if not _none else
                       "; no other product in the store moves with it closely enough"))
        elif n_pool:
            text = f"None of this product's own yet: from {_hs_who(n_pool, peer_info)}"
        else:
            text = ""

        # Every past year of this holiday on file, newest first: its own stretch as it sold,
        # its busiest day, and the change from the year before (only between two complete
        # years, so a year the history starts or stops partway through isn't a "drop").
        all_years = []
        for yv in ((own or {}).get(key) or {}).get("years") or []:
            try:
                anc = datetime.date.fromisoformat(str(yv["anchor"])[:10])
            except Exception:                               # noqa: BLE001
                continue
            bo = {int(k): float(v) for k, v in (yv.get("byOff") or {}).items()}
            if not bo:
                continue
            _core = next((oc["core"] for oc in _HS.occurrences(anc, anc, settings)
                          if oc["key"] == key and oc["anchor"] == anc), (anc, anc))
            ydays = [{"date": (anc + datetime.timedelta(days=q)).isoformat(), "units": round(u, 1),
                      "sold": True, "closed": False,
                      "core": _core[0] <= anc + datetime.timedelta(days=q) <= _core[1]}
                     for q, u in sorted(bo.items())]
            mean_u = float(np.mean(list(bo.values()))) if bo else 0.0
            for dd in ydays:
                dd["x"] = round(dd["units"] / mean_u, 2) if mean_u > 0 else 1.0
            pk = max(ydays, key=lambda dd: dd["units"])
            all_years.append({"year": anc.year, "anchor": anc.isoformat(), "start": yv["start"], "end": yv["end"],
                              "coreStart": _core[0].isoformat(), "coreEnd": _core[1].isoformat(),
                              "units": round(float(yv.get("units") or 0), 1),
                              "coreUnits": round(float(yv.get("coreUnits") or 0), 1),
                              "complete": bool(yv.get("full")), "days": ydays,
                              "peak": ({"date": pk["date"], "x": pk["x"]} if pk["units"] > 0 else None)})
        all_years.sort(key=lambda y: y["anchor"])
        for i, y in enumerate(all_years):
            prev = all_years[i - 1] if i else None
            y["pctVsPrior"] = (round((y["units"] - prev["units"]) / prev["units"] * 100)
                               if prev and prev["units"] > 0 and y["complete"] and prev["complete"]
                               and len(y["days"]) == len(prev["days"]) else None)
        all_years.reverse()
        # The most recent one that has ENDED: shown when the next one is outside the
        # forecast the page asked for (see _holidays_view).
        last_done = next((y for y in all_years if y["end"] < f0.isoformat()), None)
        rows.append({
            "key": key, "name": name, "status": status, "active": status == "moves", "coreLabel": core_label,
            "next": {"start": mine[0].isoformat() if mine else o["span"][0].isoformat(),
                     "end": mine[-1].isoformat() if mine else o["span"][1].isoformat(),
                     "coreStart": a_.isoformat(), "coreEnd": b_.isoformat()},
            "days": days,
            "peak": {"x": hi[0], "date": hi[1]}, "quiet": {"x": lo[0], "date": lo[1]},
            "forecast": {"units": round(total, 1), "coreUnits": round(core_units, 1), "sold": round(sold, 1),
                         "underway": sold > 0, "partial": partial},
            "lastYear": ({**last, "pct": vs} if last else None),
            "last": last_done, "allYears": all_years,
            "years": past, "yearsUsed": n_own, "peers": n_pool, "text": text, "basis": basis,
            "mergedInto": merged_into, "mergedKey": merged_key,
            "peerNames": list((peer_info or {}).get("names") or [])[:25],
        })
    # A holiday falling inside another's stretch this time is named with it ("Valentine's
    # Day & Super Bowl") instead of getting a line of its own.
    by_key = {r["key"]: r for r in rows}
    for r in rows:
        if r["status"] == "merged" and r.get("mergedKey") in by_key:
            host = by_key[r["mergedKey"]]
            host.setdefault("alsoIncludes", []).append(r["name"])
    for r in rows:
        if r.get("alsoIncludes"):
            r["name"] = " & ".join([r["name"]] + r["alsoIncludes"])
    rows = [r for r in rows if not (r["status"] == "merged" and r.get("mergedKey") in by_key)]
    order = {"moves": 0, "flat": 1, "new": 2, "merged": 3, "off": 4}
    rows.sort(key=lambda r: (order[r["status"]], r["next"]["start"]))
    return {"checked": len(_HS.EVENTS), "method": "shape", "effects": rows,
            "forecastStart": f0.isoformat(),
            "moves": sum(1 for r in rows if r["status"] == "moves"), "shiftDays": int(shift or 0),
            # Shaded on the chart, named once under it.
            "windows": [{"key": r["key"], "name": r["name"], "start": r["next"]["start"], "end": r["next"]["end"],
                         "peakDate": r["peak"]["date"] if r["peak"]["x"] >= 1.1 else None}
                        for r in rows if r["status"] == "moves"]}


def _holidays_view(rep, horizon_days):
    """Which occurrence of each holiday the page shows, for the forecast it asked for.

    The next one when it starts inside the forecast (its units are this forecast's, and
    they feed the stockout date and the order); otherwise the most recent one that has
    ended, as it actually sold. A long-range forecast of a holiday months out is today's
    pace stretched forward, not something to plan with. Worked out per request from the
    horizon in days, so 1, 2, 3 months, or any other horizon, all follow the same rule."""
    if not rep or rep.get("method") != "shape":
        return rep
    try:
        f0 = datetime.date.fromisoformat(rep.get("forecastStart"))
    except Exception:                                       # noqa: BLE001
        return rep
    cut = f0 + datetime.timedelta(days=max(int(horizon_days or 0), 1) - 1)
    out = []
    for r in rep.get("effects") or []:
        r = dict(r)
        try:
            nxt = datetime.date.fromisoformat(r["next"]["start"])
        except Exception:                                   # noqa: BLE001
            nxt = None
        if nxt is not None and nxt <= cut:
            r["view"] = "next"
        elif r.get("last"):
            r["view"] = "last"
        else:
            r["view"] = "none"          # not in this forecast, and never on file yet
        out.append(r)
    return {**rep, "effects": out, "horizonDays": int(horizon_days or 0), "horizonEnd": cut.isoformat(),
            "windows": [w for w in rep.get("windows") or []
                        if w.get("start") and datetime.date.fromisoformat(w["start"]) <= cut]}


def _with_future(forecast, future_fc, last_actual):
    """`forecast` with its future rows replaced by `future_fc`, so every later step —
    calibration's re-slice, the product page, the scorecard — sees the same numbers."""
    past = forecast[forecast["ds"] <= last_actual]
    return pd.concat([past, future_fc], ignore_index=True, sort=False)


def _hs_years_of(fc, occ):
    """Own years behind the shape of holiday occurrence `occ` ("key|anchor") in a forecast
    frame carrying holiday_cover's columns."""
    if fc is None or "holiday" not in fc.columns or "hs_years" not in fc.columns:
        return 0
    v = pd.to_numeric(fc.loc[fc["holiday"] == occ, "hs_years"], errors="coerce").dropna()
    return int(v.iloc[0]) if len(v) else 0


def _effect_sd(fc):
    """Per-day uncertainty of the price and holiday effects, combined."""
    tot = pd.Series(0.0, index=fc.index)
    for c in ("x_sd", "h_sd"):
        if c in fc.columns:
            tot = tot + pd.to_numeric(fc[c], errors="coerce").fillna(0.0) ** 2
    return np.sqrt(tot)


def _widen_for_extrapolation(fc, last_actual):
    """Add the uncertainty of price and holiday effects (x_sd, h_sd) to the band in
    quadrature."""
    if fc is None or not ({"x_sd", "h_sd"} & set(fc.columns)) or not {"yhat_lower", "yhat_upper"}.issubset(fc.columns):
        return fc
    fc = fc.copy()
    _sd = _effect_sd(fc)
    fut = (fc["ds"] > last_actual) & (_sd > 0)
    if not fut.any():
        return fc
    z80 = 1.2816
    xs = _sd[fut].astype(float) * z80
    y = fc.loc[fut, "yhat"].astype(float)
    hi_w = (fc.loc[fut, "yhat_upper"].astype(float) - y).clip(lower=0)
    lo_w = (y - fc.loc[fut, "yhat_lower"].astype(float)).clip(lower=0)
    fc.loc[fut, "yhat_upper"] = y + np.sqrt(hi_w ** 2 + xs ** 2)
    fc.loc[fut, "yhat_lower"] = (y - np.sqrt(lo_w ** 2 + xs ** 2)).clip(lower=0)
    return fc


# Borrowing a price response from similar products is OFF: a product's price changes move
# its forecast only through its own price history. The code is kept; set
# LOGITRACK_PRICE_BORROWING=1 to switch it back on.
PRICE_BORROWING = os.environ.get("LOGITRACK_PRICE_BORROWING", "0").strip() in ("1", "true", "yes")


# ── Closed days ───────────────────────────────────────────────────────────────────────
# See closed_days.py. A closed day is left out of every history the forecast learns from
# and forecast at zero. Dates are real calendar dates: an uploaded sheet whose history was
# moved (date_shift_days) has its closed days looked up where they really happened.
_closed_memo_real = {"key": None, "real": frozenset()}


def _closed_settings() -> dict:
    try:
        return _closed.get(_current_slot())
    except Exception:                                   # noqa: BLE001
        return {"dates": [], "yearly": []}


def _closed_real() -> frozenset:
    st = _closed_settings()
    key = json.dumps(st, sort_keys=True)
    if _closed_memo_real["key"] != key:
        t = today().date()
        real = _CD.expand(st, datetime.date(t.year - 15, 1, 1), datetime.date(t.year + 3, 12, 31))
        _closed_memo_real.update({"key": key, "real": frozenset(real)})
    return _closed_memo_real["real"]


def _drop_closed(df, shift=0):
    """`df` without its closed days (history is on dates moved by `shift`)."""
    real = _closed_real()
    if not real or df is None or not len(df) or "ds" not in df.columns:
        return df
    sd = datetime.timedelta(days=int(shift or 0))
    on = {d + sd for d in real}
    m = pd.to_datetime(df["ds"]).dt.date.isin(on).to_numpy()
    return df[~m] if m.any() else df


def _closed_chart_days(df_train, sku_id, horizon_days) -> list:
    """Closed days from the first day of history to the end of the forecast, as chart x
    values (ms). Dates inside the history are the chart's (moved) dates, like the line."""
    try:
        if not _closed_real() or df_train is None or not len(df_train):
            return []
        d0 = pd.to_datetime(df_train["ds"]).min().normalize()
        d1 = pd.to_datetime(df_train["ds"]).max().normalize() + pd.Timedelta(days=int(horizon_days or 0) + 1)
        days = pd.date_range(d0, d1, freq="D")
        m = _closed_mask(days, sku_id)
        return [int(t.timestamp() * 1000) for t, c in zip(days, m) if c]
    except Exception:                                        # noqa: BLE001
        return []


def _closed_mask(ds, sku_id) -> np.ndarray:
    """Which of these forecast dates the store is closed on. Dates inside the product's
    history (a backtest or calibration cutoff) are on its moved dates; dates after it are
    real ones."""
    real = _closed_real()
    d = pd.to_datetime(pd.Series(ds)).dt.date
    if not real or not len(d):
        return np.zeros(len(d), bool)
    ent = _catalog.get(sku_id) or {}
    sd = datetime.timedelta(days=int(ent.get("date_shift_days") or 0))
    hdf = ent.get("df")
    h_end = pd.to_datetime(hdf["ds"]).max().date() if hasattr(hdf, "columns") and len(hdf) else None
    return np.array([((x - sd) in real) if (h_end is not None and x <= h_end) else (x in real)
                     for x in d], bool)


def build_entry(sku_id, sku_name, df_clean, mode, filename, today, events, related, n_relatives,
                pool_cohesion=None,
                calibrate=True, calib_windows=None, force_route=None, availability=None,
                live_price=None, holidays=None, peers=None, store_peers=None, peer_scope="category",
                py_weight=None, pool_baseline=None, peer_info=None):
    events = events or []
    df = df_clean[df_clean["ds"] <= today].copy()
    # Closed days never happened: out of the history before anything is learned from it.
    df = _drop_closed(df, (_catalog.get(sku_id) or {}).get("date_shift_days"))
    # Sales are not demand on a day you ran out. Dropping those days outright — the old
    # behaviour — is biased, because the days you sell out are the busy ones, so it
    # deleted the top of the distribution and every forecast drifted low. Instead: uplift
    # part-days by how long you were open, keep sold-out-but-open days as floors, and
    # exclude only days with nothing to sell.
    df, censor = _CEN.apply_to_frame(df, availability)
    stockout_dropped = int(censor.get("removed", 0))

    price_varied, price_well, last_price, tiers, warn_level, warn_msg = analyse_price(df)
    has_price = "price" in df.columns and bool(df["price"].notna().any())
    # THE GATE. Price may move this forecast only when two different prices each have
    # 30+ single-price days behind them. Below that, a price event does nothing and says
    # why; above it, scheduled prices are clamped near the range actually observed.
    p_evidence = price_evidence(df) if has_price else None
    price_usable = bool(p_evidence and p_evidence.get("usable"))
    p_bounds = price_bounds(p_evidence, df, last_price) if price_usable else None
    p_range = ((p_evidence["rangeLo"], p_evidence["rangeHi"]) if price_usable else None)
    has_promo = "on_promotion" in df.columns and bool(df["on_promotion"].notna().any())

    # HOLIDAYS. The store's picked periods (Settings). Their days are kept out of every
    # baseline, season and price estimate below, so a Black Friday spike can't pass for
    # ordinary demand, a strong December or a price response. Each holiday's own lift is
    # learned from this product's past years and its peers', then applied to the upcoming
    # windows. With nothing picked, all of this is a no-op.
    # Every calendar period plus the store's own is checked for every product, minus any
    # switched off for this one. Peers are always measured against the full calendar.
    _hglobal = _holidays.get() if holidays is None else (holidays or {})
    # An uploaded sheet may have been moved so its last row lands on yesterday; holidays
    # are looked up on the dates its sales really happened (one shift for the whole
    # catalogue, 0 for a live store).
    _hshift = int((_catalog.get(sku_id) or {}).get("date_shift_days") or 0)
    # Who a holiday lift or a price response is borrowed from: the category (see
    # R.category_peers), falling back to the behavioural relatives.
    # Only what was known on `today`: a calibration or backtest cutoff must not learn a
    # peer's Black Friday that hadn't happened yet.
    _peers_full = [r for r in (peers if peers is not None else (related or []))
                   if hasattr(r, "columns") and len(r)]
    _store_full = [r for r in (store_peers or []) if hasattr(r, "columns") and len(r)]

    def _borrow(cat_frames):
        """The price response similar products show; when too few in the category have a
        price history, the whole store's (the same fallback holidays use), with more
        safety stock because a store-wide typical response is a looser match.
        Switched off (PRICE_BORROWING): a product's price response comes from its own
        prices or not at all."""
        if not PRICE_BORROWING:
            return None
        b = _UP.pooled_elasticity(cat_frames)
        if b is not None:
            b = {**b, "scope": peer_scope or "category"}
        if b is None and _store_full:
            b = _UP.pooled_elasticity([r[r["ds"] <= today] for r in _store_full])
            if b is not None:
                b = {**b, "scope": "store"}
        return b

    def _price_side():
        """Own history, similar products' histories as of `today`, and the borrowed price
        response (None unless PRICE_BORROWING is on)."""
        _pc = [r[r["ds"] <= today] for r in _peers_full]
        _b = _borrow(_pc) if (has_price and last_price and not _own_price_ok(df)) else None
        return df, _pc, _b

    # THE PRICE READING. One response per product, from its own sales only, used the same
    # way by every engine (uplift.price_reading): measured across the same months of
    # different years and before vs after each price change, cross-checked when both can
    # read. Other products whose price held steady over the same weeks are the yardstick
    # for season and traffic in the before/after reading; same category first.
    _ba_others = [r[r["ds"] <= today] for r in _store_full]
    _ba_prefer = ([r[r["ds"] <= today] for r in _peers_full] if peer_scope == "category" else None)
    _readings = {}

    def _reading(own_frame):
        k = _UP.frame_key(own_frame)
        got = _readings.get(k) if k is not None else None
        if got is None:
            got = _UP.price_reading(own_frame, gate_ok=price_usable, others=_ba_others, prefer=_ba_prefer)
            if k is not None:
                _readings[k] = got
        return got

    def _own_price_ok(own_frame):
        """Whether this product's OWN prices give a response the forecast can use: one of
        the two readings points the normal way and, when both can read, they agree. A
        product whose dearer days sold more (from a season or a launch, not the price), or
        whose two readings disagree, has no usable response of its own."""
        if not (has_price and last_price):
            return False
        return _reading(own_frame).get("elasticity") is not None

    def _el(own_frame, borrowed_):
        if _own_price_ok(own_frame):
            return float(_reading(own_frame)["elasticity"])
        return float(borrowed_["elasticity"]) if borrowed_ else 0.0

    df_nohol, peers_clean, borrowed = _price_side()

    # BORROWED PRICE RESPONSE. A product that hasn't sold at two prices for 30+ days can't
    # show how its demand responds, but similar products that have can. Their typical
    # response stands in, so a price change or promotion on a new product still moves its
    # forecast, with a wider band because it's borrowed. The same goes for a product whose
    # own prices read backwards (see _own_price_ok).
    own_price_ok = _own_price_ok(df_nohol)
    reading = _reading(df_nohol) if (has_price and last_price) else None
    # Only the RESPONSE is borrowed. A product that has sold at enough prices (whose own
    # reading just couldn't be used) keeps its own known range and limits: those are
    # prices it has really sold at.
    if price_usable and (own_price_ok or borrowed):
        eff_range, eff_bounds = p_range, p_bounds
    elif own_price_ok:
        # Read from its price changes alone (short promotions, say): the known range is
        # the prices on either side of those changes.
        _ba = reading.get("beforeAfterDetail") or {}
        _lp = float(last_price)
        eff_range = (round(min(_ba.get("lo", _lp), _lp), 2), round(max(_ba.get("hi", _lp), _lp), 2))
        eff_bounds = (round(eff_range[0] * PRICE_EXTRAP_FLOOR, 2), round(eff_range[1] * PRICE_EXTRAP_CEIL, 2))
    elif borrowed:
        _lp = float(last_price)
        eff_range = (round(_lp * borrowed["relLo"], 2), round(_lp * borrowed["relHi"], 2))
        eff_bounds = (round(min(eff_range[0] * PRICE_EXTRAP_FLOOR, _lp), 2),
                      round(max(eff_range[1] * PRICE_EXTRAP_CEIL, _lp), 2))
    else:
        eff_range = eff_bounds = None

    df_imp = df

    # HOLIDAY STRETCHES (holiday_shape.py). Each holiday's stretch keeps the forecast's own
    # total; only where in the stretch the units land follows past years, lined up on the
    # holiday itself. Learned from this product's own past years (promotion days left out,
    # Events carry those), pulled toward similar products' shape by how little it sold.
    # The models train on history as it happened; nothing is flattened first.
    hs_levels, hs_own, hs_A = {}, {}, {}
    _hs_off = set(((_hglobal or {}).get("ignored") or {}).get(str(sku_id)) or [])
    _hs_sd = datetime.timedelta(days=_hshift)
    _hs_full = (_catalog.get(sku_id) or {}).get("df")
    _hs_end = (pd.to_datetime(_hs_full["ds"]).max().date() if hasattr(_hs_full, "columns") and len(_hs_full)
               else (df["ds"].max().date() if len(df) else None))

    def _to_real(x):
        return (x - _hs_sd) if (_hs_end is not None and x <= _hs_end) else x

    if HOLIDAY_SHAPES and len(df) >= 28:
        try:
            hs_own = _HS.measure(df, _hshift, _hglobal, off=_hs_off)
            _pm = []
            # Names of the similar products, in the same order as their frames, so the page
            # can say which ones steady the pattern and by how much.
            _src_peers = peers if peers is not None else []
            _all_names = list((peer_info or {}).get("names") or [])
            _pm_names = ([n for r, n in zip(_src_peers, _all_names) if hasattr(r, "columns") and len(r)]
                         if peers is not None and len(_all_names) == len(_src_peers) else None)
            for r in _peers_full:
                rr = r[r["ds"] <= today]
                ck = (_HOL._frame_key(rr), _hshift, json.dumps((_hglobal or {}).get("dates") or {}, sort_keys=True))
                m = _hs_peer_cache.get(ck) if ck[0] is not None else None
                if m is None:
                    m = _HS.measure(rr, _hshift, _hglobal)
                    if ck[0] is not None:
                        if len(_hs_peer_cache) > 5000:
                            _hs_peer_cache.clear()
                        _hs_peer_cache[ck] = m
                _pm.append({k: v for k, v in m.items() if k not in _hs_off})
            hs_levels = {k: v for k, v in _HS.levels(hs_own, _HS.pool(_pm, _pm_names)).items() if k not in _hs_off}
            _r0 = _to_real(df["ds"].min().date())
            hs_A = _HS.assign(_r0, _r0 + datetime.timedelta(days=(df["ds"].max() - df["ds"].min()).days + 800),
                              _hglobal, off=_hs_off)
        except Exception:                                   # noqa: BLE001 — never fail a build over it
            import traceback; traceback.print_exc()
            hs_levels, hs_A = {}, {}
    # ONE-OFF RUSHES (rush.py): weeks well above normal that have ended and weren't there
    # the year before. Divided back out like a holiday, so a World Cup or a viral month
    # can't leave the everyday level stuck above where sales are now. Looked for after the
    # holidays are out, so a Black Friday is never mistaken for one.
    rushes = []
    if RUSH_ON and len(df_imp):
        try:
            rushes = _RUSH.find(df_imp, today, peers=[r[r["ds"] <= today] for r in _peers_full])
        except Exception:                                   # noqa: BLE001 — never fail a build over it
            rushes = []
        if rushes:
            df_imp = _RUSH.deflate(df_imp, rushes)
    _rush_days = set()
    for _r in rushes:
        _rush_days.update(_r["factors"].keys())
    # Similar products' histories feed the pooled season, as they happened, and only up
    # to `today`: a calibration window re-fits at an earlier cutoff with the same
    # relatives, and they used to arrive whole, so the pooled season of a holdout window
    # had already seen the days it was being graded on.
    related_clean = [(r[pd.to_datetime(r["ds"]) <= pd.Timestamp(today)] if hasattr(r, "columns") and "ds" in r.columns
                      else r) for r in (related or [])]
    related_clean = [r for r in related_clean if not hasattr(r, "columns") or len(r)]

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
        method, reason = R.route(days, sales, demand_class, n_relatives, selling_days=selling_days,
                                 pool_cohesion=pool_cohesion)
    # Price is only actually modeled on the Prophet route; global/croston/MA ignore it.
    price_modeled = (method == "prophet") and has_price
    effective_price = _effective_price(last_price, events, today)
    regular_price = effective_price
    # The POS's live price, when there is one, is the truth about TODAY: what the shelf
    # says and whether a discount is on. It becomes the displayed current price and a
    # forecast input from the first day without sales. It never touches the history.
    # Only ever passed by _rebuild for the real today — never by a backtest cutoff or a
    # calibration window, which must not see a discount that started after them.
    pos_events = []
    if live_price and len(df):
        _first = (df["ds"].max() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        _last = (df["ds"].max() + pd.Timedelta(days=800)).strftime("%Y-%m-%d")
        pos_events = _LP.forecast_events(live_price, _first, _last, recorded_price=last_price)
        effective_price = float(live_price.get("currentPrice") or effective_price or 0) or effective_price
        regular_price = float(live_price.get("listPrice") or regular_price or 0) or regular_price
    # POS-derived inputs go FIRST: they start today, so a change you scheduled for later
    # still overrides them from its own date onward.
    fc_events = pos_events + list(events)

    # Prophet takes on_promotion as a regressor and subtracts the lift itself. The other
    # three routes have no promo input at all, so for them a discount week is
    # indistinguishable from a real step up in demand — and those are exactly the routes
    # new products and intermittent sellers take, which is where launch promos live.
    # Discounted days stay in the training data only when price can explain them (Prophet
    # with a usable price). Otherwise they're held out so the baseline is the regular-price
    # rate, and any scheduled discount is applied as a price change on top.
    fit_df, promo_report = _PROMO.hold_out(
        df, method if method != "prophet" else "prophet-without-price")
    # One-off rush days train at the level around them (rush.py), on every route.
    if df_imp is not df and len(fit_df) and _rush_days:
        _imp = df_imp.set_index("ds")
        _rm = pd.to_datetime(fit_df["ds"]).dt.normalize().isin(_rush_days).to_numpy()
        if _rm.any():
            fit_df = fit_df.copy()
            _vals = _imp["y"].reindex(fit_df["ds"]).to_numpy(float)
            fit_df["y"] = np.where(_rm & np.isfinite(_vals), _vals, fit_df["y"].to_numpy(float))

    # Routes without a price input learn a level from history. When part of that history
    # sold at a different price, the level is off: a stretch at a lower price makes today's
    # rate look higher than it is. With a measured response, restate each past day at
    # today's price before fitting, so the baseline is "demand at the current price" and
    # price events move it from there. Prophet included: it no longer fits a price number
    # of its own, it uses this same reading.
    if own_price_ok and last_price:
        _e = float(reading["elasticity"])
        if _e and "price" in fit_df.columns:
            _p = pd.to_numeric(fit_df["price"], errors="coerce").fillna(float(last_price)).clip(lower=0.01)
            _adj = (float(last_price) / _p) ** _e
            fit_df = fit_df.copy()
            fit_df["y"] = fit_df["y"] * _adj.clip(0.5, 2.0)

    # SLOW SELLERS' SEASONS. Croston and the moving average work out one rate from recent
    # sales and hold it flat. When recent months were the quiet season that rate starts too
    # low for the busy one ahead (and too high coming out of it), and scaling it by the
    # coming months can't make up for it. So the season is taken out FIRST: each past day
    # is divided by its season's level (a smooth daily curve from the monthly index, own
    # history if it has a year of it, otherwise similar products), the rate is worked out
    # from that, and the coming days' season is put back on after.
    _slow_sea = None
    # What the models learn from, before the season is taken out: holidays and one-off rushes
    # divided out, past discount days held out or restated at today's price (see the same
    # weeks in past years, below).
    _fit_plain = fit_df
    if method in ("croston", "abstain") and SLOW_DESEASON and len(fit_df):
        _sp = [r for r in related_clean if hasattr(r, "columns")]
        _s0 = _SEAS.resolve(df_imp, _sp)
        if _s0.get("source") != "none" and _s0.get("strength", 0) >= _SEAS.MIN_STRENGTH:
            _di = np.asarray(_SEAS.daily_index(fit_df["ds"].dt.strftime("%Y-%m-%d").tolist(), _s0["index"]), float)
            if np.all(np.isfinite(_di)) and np.all(_di > 0):
                fit_df = fit_df.copy()
                fit_df["y"] = fit_df["y"].astype(float).to_numpy() / _di
                _slow_sea = _s0

    if method == "prophet":
        # `related` lets an established-but-under-a-year SKU (yearly seasonality off)
        # blend in a pooled annual shape from its cohesive peers; a no-op otherwise.
        # has_promo=False: a promotion is a price cut, carried by the price input. Learning a
        # separate "promotion boost" from the same discounted days counted one effect twice.
        # has_price=False: Prophet's own price fit had no check on it (a price change landing
        # on a season turn read as a price effect). The product's cross-checked reading is
        # applied on top instead, as on every other route.
        # The store's average yearly shape (months of R.catalog_baseline), so the pooled
        # blend judges agreement on the same specific scale grouping and routing use.
        _bmo = (np.asarray(pool_baseline, float)[7:19] if pool_baseline is not None
                and len(pool_baseline) >= 19 else None)
        eng = ProphetEngine().fit(fit_df, today, False, False, last_price, fc_events, past_years_weight=py_weight,
                                  related=related_clean, price_bounds=p_bounds, price_range=p_range,
                                  baseline_mo=_bmo)
    elif method == "global":
        eng = GlobalPooledEngine().fit(fit_df, today, related=related_clean)
    elif method == "croston":
        eng = IntermittentEngine().fit(fit_df, today)
    else:
        eng = MovingAverageEngine().fit(fit_df, today)

    # The engine may have REFUSED price after fitting — Prophet now switches it off when
    # the fitted effect points backwards (dearer = more sales). Whatever it decided is
    # the truth about whether price moves this forecast, so the flag follows the engine
    # rather than the route; otherwise the page would promise price sensitivity the model
    # no longer has, and a price event would quietly do nothing under a label saying it
    # would.
    if method == "prophet":
        price_modeled = bool(getattr(eng, "has_price", False))

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
    if _slow_sea is not None:
        season_report = {**_slow_sea, "coverage": 1.0, "smooth": True, "deseasonalized": True,
                         "text": _SEAS.explain(_slow_sea)}
    elif method in ("croston", "abstain"):
        _peers = [r for r in related_clean if hasattr(r, "columns")]
        _sea = _SEAS.resolve(df_imp, _peers)
        if _sea.get("source") != "none":
            # The rate was measured over whichever months the history covers. If that
            # window sat in the busy season the rate is ALREADY elevated, and applying a
            # peak index on top would order twice over. Divide that context back out.
            _sea["coverage"] = round(_SEAS.coverage_factor(df_imp, _sea["index"]), 4)
            season_report = {**_sea, "text": _SEAS.explain(_sea)}

    uplift_report = None
    # Prices reach the forecast one of two ways: inside Prophet's model when it takes price
    # as an input, or as explicit multipliers on top of any other forecast (every other
    # route, and a Prophet product priced from its peers' response).
    prophet_prices = method == "prophet" and bool(getattr(eng, "has_price", False))
    prophet_refused = method == "prophet" and bool(getattr(eng, "_price_inverted", None))
    if prophet_refused and not borrowed and has_price and last_price:
        # Prophet's own fit read price backwards, so it switched price off. Similar
        # products stand in, as they do for any product with no usable response of its own.
        borrowed = _borrow(peers_clean)
        if borrowed:
            eff_range, eff_bounds = p_range, p_bounds      # Prophet only gets price when usable
    if not prophet_prices and (not prophet_refused or borrowed):
        # Promotions are price cuts now, so there is no separate promotion boost to learn or
        # borrow from similar products. A promotion's discount moves the forecast through the
        # price response below, and a promotion with no discount doesn't move it at all.
        _lift = {"multiplier": 1.0, "basis": "promotions-are-price-changes", "promoDays": 0}
        _r = reading or {}
        _elast = dict(_r.get("sameMonthsDetail") or {"elasticity": 0.0, "basis": "no-price-data", "points": 0})
        _rd = {k: _r.get(k) for k in ("sameMonths", "beforeAfter", "changes")}
        if own_price_ok:
            _elast = {**_elast, "elasticity": float(_r["elasticity"]), "basis": "measured",
                      "method": _r.get("basis"), **_rd}
        else:
            _elast = {**_elast, "elasticity": 0.0, "basis": _r.get("basis") or _elast.get("basis") or "none",
                      **_rd}
        if borrowed and (not own_price_ok or prophet_refused):
            _elast = {**_elast, "elasticity": borrowed["elasticity"], "basis": "borrowed",
                      "peers": borrowed["peers"],
                      "ownBasis": "inverted" if prophet_refused else _elast.get("basis")}
        uplift_report = {"promo": _lift, "price": _elast,
                         "text": _UP.explain(_lift, _elast)}

    price_response = _price_response(method, eng, has_price, price_varied,
                                     (uplift_report or {}).get("price"),
                                     evidence=p_evidence, bounds=eff_bounds if own_price_ok else p_bounds,
                                     known_range=eff_range if own_price_ok else None,
                                     borrowed=borrowed if (uplift_report or {}).get("price", {}).get("basis") == "borrowed" else None,
                                     borrowed_range=eff_range, borrowed_bounds=eff_bounds)
    # Whether a price event moves this forecast, whichever engine is behind it. This used
    # to be "is the route Prophet", so a product with a measured elasticity on another
    # route was shown "price changes won't affect this forecast" while they did.
    price_modeled = bool(price_response.get("applies"))

    forecast = eng.forecast_df.copy()
    # Split history vs forecast at the SKU's LAST ACTUAL date, not the calendar
    # "today". Uploads are re-anchored so their last row is yesterday, which makes
    # today the first forecast day; keying off the last actual keeps today in the
    # forecast (not stranded as a blank between history and tomorrow).
    last_actual = df["ds"].max()
    future_fc = forecast[forecast["ds"] > last_actual].copy().reset_index(drop=True)

    # Slow sellers with a year of their own history: the same weeks last year, scaled by
    # this year's pace (seasonality.same_weeks_last_year).
    _yoy = None
    if SLOW_DESEASON and method in ("croston", "abstain") and len(future_fc):
        try:
            _fd = future_fc["ds"].tolist()
            _yoy = _SEAS.same_weeks_last_year(df_imp, _fd)
            # Past sales include past sales events. A store sale that comes round every
            # year (Black Friday, the March clearance) is then forecast even if nobody enters
            # it again, which is right. But on days where you HAVE scheduled a price change
            # or a sale, its effect is added separately, so those days are read from the
            # history with past sale days held out, or last year's sale is counted twice.
            if _yoy and fc_events and last_price:
                _path = _UP.future_price_path([str(d)[:10] for d in _fd], fc_events, last_price)
                _ev_days = [i for i, d in enumerate(_fd)
                            if _path.get(str(d)[:10]) is not None
                            and abs(float(_path[str(d)[:10]]) - float(last_price)) > 1e-9]
                if _ev_days:
                    _clean = _SEAS.same_weeks_last_year(_fit_plain, _fd)
                    if _clean:
                        _dl = list(_yoy["daily"])
                        for i in _ev_days:
                            if i < len(_dl) and i < len(_clean["daily"]):
                                _dl[i] = _clean["daily"][i]
                        _yoy = {**_yoy, "daily": _dl}
        except Exception:                                   # noqa: BLE001
            _yoy = None
    if (season_report or _yoy) and len(future_fc):
        _dates = future_fc["ds"].dt.strftime("%Y-%m-%d").tolist()
        if season_report:
            _sm = (_SEAS.daily_index(_dates, season_report["index"])
                   if season_report.get("smooth") else
                   _SEAS.future_multipliers(_dates, season_report["index"],
                                            coverage=season_report.get("coverage", 1.0)))
        else:
            _sm = [1.0] * len(_dates)
        if _yoy:
            _base = future_fc["yhat"].clip(lower=0).to_numpy(float)
            _b = _base * np.asarray(_sm, float)
            _y = np.asarray(_yoy["daily"], float)[:len(_b)]
            _strong = float((season_report or {}).get("strength") or 0.0) >= _SEAS.YOY_STRONG
            _fin = _y if _strong else 0.5 * _b + 0.5 * _y
            if np.all(_base > 1e-9):
                _sm = (_fin / _base).tolist()
                season_report = {**(season_report or {"index": _SEAS._flat(), "source": "own"}),
                                 "sameWeeksLastYear": {k: _yoy[k] for k in ("growth", "lastYear", "recent")},
                                 "yoyWeight": 1.0 if _strong else 0.5}
                _g = _yoy["growth"]
                _ny = int(_yoy.get("years") or 1)
                season_report["text"] = (
                    f"Forecast from the same weeks in past years ({round(_yoy['lastYear'])} units in the next "
                    f"{_yoy.get('days', 30)} days a year ago"
                    + (f"; {_ny} years averaged" if _ny > 1 else "") + "), "
                    + (f"{'up' if _g >= 1 else 'down'} {abs(round((_g - 1) * 100))}% for how its last 6 months "
                       f"compare with a year before" if abs(_g - 1) >= 0.005 else "at the same pace as a year ago")
                    + ("." if _strong else
                       ", averaged half and half with its recent selling rate (its season is mild)."))
        if any(abs(m - 1.0) > 1e-9 for m in _sm):
            future_fc = _UP.apply(future_fc, _sm)
            forecast = _with_future(forecast, future_fc, last_actual)
            season_report["applied"] = True
            season_report["peakMultiplier"] = round(max(_sm), 3)
            season_report["troughMultiplier"] = round(min(_sm), 3)

    # HOLIDAY STRETCHES AHEAD: each stretch's total split across its days by its shape
    # (see HOLIDAY STRETCHES above). Before any promotion or closed-day multiplier, so a
    # scheduled sale lifts the reshaped day and a closed day takes no share. A stretch
    # already under way is split as a whole, the units already sold counting toward its
    # total, so the days left never take the whole stretch.
    hs_info = {}
    hs_apply = {}
    if hs_levels and hs_A and len(future_fc):
        try:
            _wdi = _HS._wdi_of(df_imp)
            _past = {pd.Timestamp(x).date(): float(v) for x, v in zip(df_imp["ds"], df_imp["y"])
                     if (last_actual - pd.Timestamp(x)).days < 60}
            _old = future_fc["yhat"].clip(lower=0).to_numpy(float)
            _apply = {k: v for k, v in hs_levels.items() if _HS.significant(v)}
            hs_apply = _apply
            _new, hs_info = _HS.redistribute(future_fc["ds"].tolist(), _old, _to_real, hs_A, _apply, _wdi,
                                             past=_past, closed=_closed_mask(future_fc["ds"], sku_id))
            if hs_info:
                _ratio = np.where(_old > 1e-9, _new / np.maximum(_old, 1e-9), 1.0)
                future_fc = future_fc.copy()
                future_fc["yhat"] = np.where(_old > 1e-9, future_fc["yhat"].to_numpy(float) * _ratio, _new)
                for _c in ("yhat_lower", "yhat_upper"):
                    if _c in future_fc.columns:
                        future_fc[_c] = pd.to_numeric(future_fc[_c], errors="coerce").to_numpy(float) * _ratio
                forecast = _with_future(forecast, future_fc, last_actual)
        except Exception:                                   # noqa: BLE001
            import traceback; traceback.print_exc()
            hs_info = {}

    if uplift_report and len(future_fc):
        _mults = _UP.future_multipliers(
            future_fc["ds"].dt.strftime("%Y-%m-%d").tolist(), fc_events,
            uplift_report["promo"]["multiplier"],
            elasticity=uplift_report["price"]["elasticity"],
            base_price=last_price, bounds=eff_bounds, known_range=eff_range)
        if any(abs(m - 1.0) > 1e-9 for m in _mults):
            future_fc = _UP.apply(future_fc, _mults)
            uplift_report["applied"] = True
            uplift_report["daysLifted"] = sum(1 for m in _mults if m > 1.0)
            # How much of each day is a price effect, and how far outside the observed
            # prices it was extrapolated — the same uncertainty Prophet's route carries.
            _el = uplift_report["price"]["elasticity"]
            if eff_range and _el:
                _floor = ((STORE_BORROWED_EFFECT_UNC if (borrowed or {}).get("scope") == "store"
                           else BORROWED_EFFECT_UNC) if uplift_report["price"].get("basis") == "borrowed"
                          else PRICE_EFFECT_UNC)
                _dates = future_fc["ds"].dt.strftime("%Y-%m-%d").tolist()
                _path = _UP.future_price_path(_dates, fc_events, last_price, bounds=eff_bounds)
                _xs = []
                for _d, _y in zip(_dates, future_fc["yhat"].clip(lower=0).tolist()):
                    _p = _path.get(_d)
                    if _p is None or abs(_p - float(last_price or 0)) < 1e-9:
                        _xs.append(0.0); continue
                    _pm = _UP.price_response(_el, last_price, _p, eff_range)
                    _u = extrapolation_uncertainty(extrapolation_distance(_p, *eff_range), floor=_floor)
                    _xs.append(abs(_y - _y / _pm) * _u if _pm > 0 else 0.0)
                future_fc["x_sd"] = _xs
        # The scorecard reads future_fc, the product page re-derives it from `forecast`.
        # These multipliers used to reach only the first, so on every non-Prophet route a
        # price change or promotion moved the scorecard's order but not the product page.
        forecast = _with_future(forecast, future_fc, last_actual)

    holiday_report = None

    # CLOSED DAYS AHEAD sell nothing. Applied after every other multiplier, so nothing
    # can lift a day the store isn't open.
    closed_ahead = 0
    if len(future_fc):
        _cz = _closed_mask(future_fc["ds"], sku_id)
        if _cz.any():
            _cm = np.where(_cz, 0.0, 1.0)
            future_fc = _UP.apply(future_fc, _cm.tolist())
            for _c in ("x_sd", "h_sd"):
                if _c in future_fc.columns:
                    future_fc[_c] = pd.to_numeric(future_fc[_c], errors="coerce").fillna(0.0).to_numpy(float) * _cm
            forecast = _with_future(forecast, future_fc, last_actual)
            closed_ahead = int(_cz.sum())

    # HOLIDAY COVER (holiday_cover.py): each day of a stretch this forecast reshapes carries
    # an uncertainty in proportion to its units, at the store's measured holiday rate. The
    # order math adds it up per holiday over the order window, so holiday weeks get the
    # extra buffer they've been measured to need. After every multiplier, so a closed day
    # carries none and a promoted day its lifted units' share.
    if hs_apply and len(future_fc):
        try:
            future_fc = _HC.attach(future_fc, hs_A, hs_apply, hs_levels, _to_real)
            forecast = _with_future(forecast, future_fc, last_actual)
        except Exception:                                   # noqa: BLE001 — never fail a build over it
            import traceback; traceback.print_exc()

    # What the Holidays tab shows, read off the final forecast (promotions and closed days
    # included), so it always says what the chart says and moves whenever it does.
    if HOLIDAY_SHAPES and len(future_fc):
        try:
            holiday_report = _hs_report(df, future_fc, hs_levels, hs_own, _hs_off, _to_real, _hglobal, _hshift,
                                        peer_info=peer_info)
        except Exception:                                   # noqa: BLE001
            import traceback; traceback.print_exc()
            holiday_report = None

    # Price range + safe extrapolation (model-agnostic; carries over).
    p_min = p_max = p_safe_min = p_safe_max = None; gap_warn = None
    if has_price and tiers:
        # Trained range = the QUALIFYING levels when there are any; the safe range is the
        # exact clamp the forecast applies, so the warning and the behaviour agree.
        _q = (p_evidence or {}).get("qualifying") or tiers
        seen = sorted(t["price"] for t in _q); p_min, p_max = seen[0], seen[-1]
        if p_bounds is not None:
            p_safe_min, p_safe_max = p_bounds
        if len(seen) >= 2:
            gaps = [seen[i + 1] - seen[i] for i in range(len(seen) - 1)]; mg = max(gaps); rng = p_max - p_min
            if rng > 0 and mg / rng > 0.25:
                i = gaps.index(mg)
                gap_warn = (f"Large untested gap: ${seen[i]:.2f} to ${seen[i+1]:.2f} (${mg:.2f}, "
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
    # Demand level over the SAME windows the residuals were measured on. The steadiness
    # signal divides error by demand, and dividing a recent error by a lifetime average
    # compares two different periods — on a seasonal line that alone moved the verdict
    # depending on which season the holdout windows happened to land in.
    ref_mean = None; ref_units = 0.0
    n_calib_windows = int(calib_windows) if calib_windows else CALIB_WINDOWS
    if calibrate and days >= 120:
        try:
            in_sample = float(eng.residual_std)
            first_day = df["ds"].min(); WIN = 45
            wins = []; win_actuals = []
            # Roll several NON-overlapping 45-day holdout windows back in time, refitting
            # before each, so the error estimate isn't hostage to one possibly-quiet stretch.
            # As many windows as history allows, up to n_calib_windows (short SKUs use fewer;
            # the backtest passes a smaller count since it already refits at every cutoff).
            for k in range(n_calib_windows):
                cutoff_cal = today - pd.Timedelta(days=WIN * (k + 1))
                if (cutoff_cal - first_day).days < 60:      # need ≥60 days to train before a window
                    break
                # Same model and pool as the fit being calibrated: the route it was forced
                # to (a backtest's single-engine pass) and the pool's cohesion, or the
                # holdout measured a different forecast from the one it sizes the buffer for.
                cal = build_entry(sku_id, sku_name, df_clean, mode, filename, cutoff_cal,
                                  events, related, n_relatives, calibrate=False,
                                  pool_cohesion=pool_cohesion, force_route=force_route,
                                  availability=availability, holidays=_hglobal, peers=peers,
                                  store_peers=store_peers, pool_baseline=pool_baseline,
                                  py_weight=(float(getattr(eng, "_py_w", 0.0) or 0.0)
                                             if method == "prophet" else None))
                tend = cutoff_cal + pd.Timedelta(days=WIN)
                hold = (df[(df["ds"] > cutoff_cal) & (df["ds"] <= tend)][["ds", "y"]]
                        .merge(cal["forecast"][["ds", "yhat"]], on="ds", how="inner"))
                if len(hold) >= 7:
                    wins.append((hold["y"] - hold["yhat"]).to_numpy(float))
                    win_actuals.append(hold["y"].to_numpy(float))
            if wins:
                calib_ran = True
                _ref = np.concatenate(win_actuals)
                ref_mean = float(_ref.mean()); ref_units = float(_ref.sum())
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
    # σ as MEASURED, before the deliberate widening below. The widening exists to keep a
    # young product's safety buffer from being thin; describing that product's demand as
    # 30% more erratic than it is would be a different claim, and a wrong one.
    sigma_observed = float(eng.residual_std)
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

    # No holdout ran (short history, or no scoreable window): fall back to the recent
    # demand level rather than the lifetime one, for the same reason as above.
    if ref_mean is None and len(df):
        _tail = df[df["ds"] > df["ds"].max() - pd.Timedelta(days=180)]["y"]
        if len(_tail):
            ref_mean = float(_tail.mean()); ref_units = float(_tail.sum())
    eng.residual_dispersion = demand_dispersion(sigma_observed, ref_mean, ref_units)

    # Days whose price was extrapolated beyond what this product has sold at get a wider
    # band. Done last, so neither calibration step above can overwrite it.
    forecast = _widen_for_extrapolation(forecast, last_actual)
    future_fc = forecast[forecast["ds"] > last_actual].copy().reset_index(drop=True)

    details = _make_details(method, reason, eng.model_label, demand_class, eng.residual_cv,
                            eng._q_lo, eng._q_hi, eng.explain_bits(), len(df),
                            dispersion=eng.residual_dispersion, ref_mean=ref_mean)
    details["relatives"] = int(n_relatives)
    if season_report and season_report.get("applied") and method in ("croston", "abstain") \
            and season_report.get("text"):
        details["seasonality"] = {**(details.get("seasonality") or {}), "yearly": True,
                                  "text": season_report["text"]}
    if rushes:
        details["oneOff"] = {"count": len(rushes), "text": _RUSH.explain(rushes),
                             "periods": [{k: r[k] for k in ("start", "end", "weeks", "ratio", "lastYear", "source")}
                                         for r in rushes]}
    # The engine describes its own inputs; a price response applied on top of it (measured
    # or borrowed) is not the engine's, so say what actually happens.
    # One verdict on price, said the same way everywhere: whenever Prophet isn't carrying
    # price itself, the price response decides what the details say (the engine alone
    # would say "no price data" for a product whose prices simply can't be used).
    if not prophet_prices and price_response.get("text"):
        details["price"] = {**(details.get("price") or {}), "used": bool(price_response.get("applies")),
                            "text": price_response.get("text")}
    if holiday_report and holiday_report.get("method") == "shape":
        _act = [r for r in holiday_report["effects"] if r["status"] == "moves"]
        details["holidays"] = {
            "count": len(_act),
            "text": ((f"Around {', '.join(r['name'] for r in _act[:5])}"
                      + (f" and {len(_act) - 5} more" if len(_act) > 5 else "")
                      + ", each holiday's forecast units are spread over its days the way past years sold, "
                        "lined up on the holiday itself. The total for those weeks is this forecast's own.")
                     if _act else
                     "No holiday changes how this product's sales fall across the days around it.")}
    elif holiday_report:
        _act = [r for r in holiday_report["effects"] if r["active"]]
        details["holidays"] = {
            "count": len(_act),
            "text": ((f"{holiday_report['checked']} holidays and sales periods checked automatically. Sales history "
                      f"shows: " + ", ".join(f"{r['name']} {_HOL.pct_text(r['multiplier'])}" for r in _act[:6])
                      + (f", and {len(_act) - 6} more" if len(_act) > 6 else "") + ". Each is added on its own dates.")
                     if _act else
                     f"{holiday_report['checked']} holidays and sales periods checked automatically. None shows a "
                     f"clear effect on this product yet.")}

    try:
        _off = bool(len(df)) and bool(_FE_off_season(df["ds"], df["y"].to_numpy(dtype=float),
                                                    pd.Timestamp(today) + pd.Timedelta(days=1)))
    except Exception:                                       # noqa: BLE001
        _off = False
    return {
        "engine": eng, "df_train": df, "forecast": forecast, "future_fc": future_fc,
        "mode": mode, "filename": filename, "sku_name": sku_name,
        "active_yearly": days >= YEARLY_MIN_DAYS, "active_weekly": days >= 14,
        "has_price": has_price, "has_promo": has_promo,
        "price_varied": price_varied, "price_well_sampled": price_well, "last_price": last_price,
        "price_tiers": tiers, "price_trained_min": p_min, "price_trained_max": p_max,
        "price_usable": price_usable, "price_mixed_days": int((p_evidence or {}).get("mixedDays") or 0),
        "last_recorded_date": (df["ds"].max().strftime("%Y-%m-%d") if len(df) else None),
        "price_safe_min": p_safe_min, "price_safe_max": p_safe_max, "price_gap_warning": gap_warn,
        "price_warn_level": warn_level, "price_warn_msg_upload": warn_msg,
        "stockout_rows_dropped": stockout_dropped, "censoring": censor,
        "promo_handling": promo_report, "uplift": uplift_report,
        "price_response": price_response, "holidays": holiday_report,
        "borrowed_price": borrowed if (uplift_report or {}).get("price", {}).get("basis") == "borrowed" else None,
        "seasonality_applied": season_report,
        "residual_cv": eng.residual_cv, "residual_std": eng.residual_std, "oos_residuals": oos_resid,
        "residual_dispersion": eng.residual_dispersion, "residual_ref_mean": ref_mean,
        "residual_sigma_observed": sigma_observed,
        "sigma_calibrated": calib_ran, "events": events, "pos_events": pos_events,
        "live_price": live_price, "regular_price": regular_price,
        "winning_model": eng.model_label, "demand_class": demand_class,
        # Has it simply stopped selling? Computed here, on the SAME series the models were
        # fitted to, so the badge, the routing and the forecast can never disagree about
        # it. Only meaningful now that zero-fill runs to the window end — before that a
        # live-sourced series could not end in a zero and this was always False.
        # Quiet the way it was quiet this time last year (a Christmas line in September)
        # is out of season, not dormant: it keeps its normal status and its family.
        "off_season": _off,
        "dormant": bool(_FE_is_dormant(df["y"].to_numpy(dtype=float))) and not _off if len(df) else False,
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
    # Out of season is not dormant: a Christmas line in September still defines what a
    # Christmas product does, and a new one needs it as family.
    if _FE_off_season(df["ds"], df["y"].to_numpy(dtype=float), as_of):
        return False
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
    # Closed days are out of every history used below, the product's own and the similar
    # products it borrows from (holiday lifts, seasons, relatives).
    _open = ({sid: {**c, "df": _drop_closed(c.get("df"), c.get("date_shift_days"))}
              for sid, c in _catalog.items()} if _closed_real() else _catalog)
    pool_catalog = {sid: c for sid, c in _open.items() if not _exclude_from_pooling(c, today)}
    base_cols = R.detect_group_columns(pool_catalog)
    groups, group_meta = R.adaptive_group_catalog(pool_catalog, base_cols)
    # The catalog-wide average fingerprint, computed once. Everything below measures
    # similarity with this subtracted, so "alike" means alike beyond the rhythm the whole
    # business shares rather than because of it.
    _baseline = R.catalog_baseline(pool_catalog)
    _family = R.family_map(groups, group_meta)
    clusters = R.cluster_catalog(groups, pool_catalog, baseline=_baseline,   # behavioural sub-clusters per category
                                 family=_family)
    targets = [only] if only else list(_catalog.keys())
    # Build into a copy, publish at the end. A refit of an already-populated catalogue
    # publishes exactly once: any intermediate publish would recreate the old/new mixture
    # this whole arrangement exists to prevent. A cold boot starts from nothing, so there
    # is no mixture to make and it may publish as it goes.
    staged = dict(_sku_cache)
    incremental = not staged
    built = 0
    for sku_id in targets:
        c = _open[sku_id]
        try:
            # Pool only from the behavioural cluster this SKU best fits (not every
            # category-mate). n_rel is the chosen cluster's size, so routing to the
            # global model still requires ≥ MIN_RELATIVES genuinely-similar peers.
            rels, n_rel, cluster_info = R.behavioral_relatives(sku_id, groups, pool_catalog, clusters,
                                                               baseline=_baseline)
            # A product left out of pooling (no sales for a while: a Christmas item in
            # September) still has a category: it borrows holiday patterns from it, and is
            # judged against the store on its own history when it has none.
            if sku_id in pool_catalog:
                _peer_cat, _peer_fam = pool_catalog, _family
            else:
                _peer_cat = {**pool_catalog, sku_id: c}
                _peer_fam = {**_family, sku_id: R.group_catalog({sku_id: c}, base_cols).get(sku_id)}
            _peer_frames, _peer_ids = R.category_peers(sku_id, groups, _peer_cat, family=_peer_fam,
                                                       baseline=_baseline, with_ids=True)
            # Who its holiday pattern is steadied by, in words the Holidays panel can use.
            _pcat = _peer_fam.get(sku_id)
            _in_cat = _pcat is not None and any(m != sku_id and f == _pcat for m, f in _peer_fam.items())
            _peer_info = {"scope": "category" if _in_cat else ("store" if _peer_ids else "none"),
                          "category": _pcat,
                          "names": [(_peer_cat.get(m) or {}).get("sku_name") or m for m in _peer_ids]}
            # How much the chosen pool actually agrees on a shape. This used to be
            # computed only for the Grouping tab's display while routing ignored it,
            # so a pool the UI drew in red still supplied a newcomer's whole forecast.
            pool_coh = (R._cohesion_for_ids(cluster_info["chosen"], pool_catalog, _baseline)
                        if cluster_info and len(cluster_info.get("chosen") or []) >= 2 else None)
            # Hours-in-stock, where we have it. Absent → every day counts as fully
            # available, so an untracked catalogue behaves exactly as before.
            _avail = None
            _live_ok = _live_readings_apply(sku_id)
            try:
                _cov = _slog.coverage(sku_id) if _live_ok else {}
                if _cov.get("samples"):
                    _avail = {r["date"]: r["hoursInStock"] for r in _slog.availability_series(
                        sku_id, _cov["firstSeen"], _FL.today().isoformat())
                        if r["hoursInStock"] is not None}
            except Exception:
                _avail = None
            entry = build_entry(sku_id, c["sku_name"], c["df"], c["mode"], c["filename"],
                                today, c.get("events", []), rels, n_rel,
                                pool_cohesion=pool_coh, availability=_avail, pool_baseline=_baseline,
                                live_price=_live.get(sku_id) if _live_ok else None,
                                peers=_peer_frames, peer_info=_peer_info,
                                store_peers=R.category_peers(sku_id, {}, pool_catalog, limit=40),
                                # With no category, or no other product in it, "similar
                                # products" is already the whole store: say so.
                                peer_scope=("category" if _peer_fam.get(sku_id) is not None and any(
                                    m != sku_id and ff == _peer_fam.get(sku_id) and m in pool_catalog
                                    for m, ff in _peer_fam.items()) else "store"))
            entry["cluster_info"] = cluster_info
            entry["group_info"] = group_meta.get(sku_id)
            staged[sku_id] = entry
            _last_rebuild_errors.pop(sku_id, None)
            built += 1
            if incremental and built % _PUBLISH_EVERY == 0:
                _publish_cache(dict(staged))
            # Log a forecast-model switch (e.g. global→Prophet) for the overview banner.
            _record_route(sku_id, c["sku_name"], entry["route"], entry["route_reason"], log_event=record_switches)
        except Exception as ex:
            import traceback; traceback.print_exc()
            _last_rebuild_errors[sku_id] = f"{type(ex).__name__}: {ex}"
            # If this SKU already had a working entry (e.g. a re-fit after an event
            # save or re-categorize failed), KEEP the previous entry rather than
            # making the SKU vanish from the dashboard. Only brand-new SKUs (first
            # fit, nothing to fall back to) stay absent — reported via errors.
            if sku_id not in staged:
                pass  # nothing to preserve; the upload response reports the error
    _publish_cache(staged)
    # A shelf price that moved re-prices the measured protection levels (no refit).
    _reprice_backtest_if_price_moved()


def warmup():
    """Start empty. There is no demo dataset — the app shows an empty state until you
    load real data, so nothing synthetic can ever be mistaken for your own numbers or
    leak into a forecast, a backtest or an export."""
    global _backtest_tier_cache, _backtest_exclusions, _last_backtest
    _backtest_tier_cache, _backtest_exclusions, _last_backtest = {}, {}, None
    _HC.set_rates(None)
    _sheet_costs.clear()
    _cost_sources.clear()
    _backtest_inputs.clear()
    globals()["_last_backtest_rows"] = None
    globals()["_last_backtest_combos"] = []
    with _state_lock:
        _catalog.clear(); _publish_cache({})

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
        _cost_sources.update({k: str(v) for k, v in (extras.get("cost_sources") or {}).items()})
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
    """The product list, WITH the stock and cost the uploaded sheet carried.

    These two fields used to be sent only in the /api/upload response, so the browser
    learned a product's real stock exactly once — at upload — and kept it in local
    storage. Every restart restores the catalog from disk and calls this endpoint
    instead, which said nothing about stock, so the client fell back to its 500-unit
    default for the whole catalogue. On a 1.2/day product that reads as 417 days of
    cover, and the Fleet reported every product overstocked or dead with nothing to
    order. The engine knew the real figure the whole time — it just had no way to say it.

    Clearing site data, or opening the tool in another browser, produced the same
    silent wipe. The client seeds only where it has no value of its own, so a number
    the user typed is never overwritten by the sheet.
    """
    out = []
    for sku_id, e in _cache().items():
        df = e["df_train"]
        # Latest non-null units_in_stock on or before today, mirroring _ingest's own rule.
        last_stock, stock_src = None, None
        _cat = _catalog.get(sku_id) or {}
        try:
            if "last_known_stock" in _cat:
                # What the file actually said (see _ingest); None when it gave no figure.
                if _cat["last_known_stock"] is not None:
                    last_stock, stock_src = int(_cat["last_known_stock"]), "sheet"
            elif df is not None and "units_in_stock" in df.columns:
                ss = pd.to_numeric(df["units_in_stock"], errors="coerce").dropna()
                if len(ss):
                    last_stock, stock_src = int(ss.iloc[-1]), "sheet"
        except Exception:
            last_stock = None
        if last_stock is None:
            # A LIVE source carries inventory ALONGSIDE the sales, never inside them, so a
            # synced catalog has no units_in_stock column and this used to return null —
            # which the browser then "seeded" over an empty slot with the 500-unit default.
            # That is the exact regression handleUploadSuccess already documents having
            # fixed once for costs. StockLog is the timestamped record of on-hand and it
            # survives a restart, so it is the right thing to read back.
            logged = _slog.latest(sku_id) if _live_readings_apply(sku_id) else None
            if logged is not None:
                last_stock, stock_src = int(round(logged)), "live"
        out.append({"id": sku_id, "name": e["sku_name"] or sku_id, "mode": e["mode"], "filename": e["filename"],
                    "daysOfHistory": int((df["ds"].max() - df["ds"].min()).days) if df is not None else 0,
                    "totalUnitsSold": int(df["y"].sum()) if df is not None else 0,
                    "lastKnownStock": last_stock, "stockSource": stock_src,
                    "lastKnownCost": _sheet_costs.get(str(sku_id)),
                    "costSource": _cost_sources.get(str(sku_id))})
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
    for sid, e in _cache().items():
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
        if sku_id not in _cache() and sku_id not in _catalog:
            raise HTTPException(404, "SKU not found.")
        _publish_cache({k: v for k, v in _sku_cache.items() if k != sku_id})
        _catalog.pop(sku_id, None)
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
    try:
        _livelog_reconcile([sku_id], trigger="planned")
    except Exception as exc:                                # noqa: BLE001 — never fail a save over it
        print(f"[livelog] could not revise for new events: {exc}")
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
        df_raw = _read_sheet(contents, file.filename)
    except Exception as e:
        raise HTTPException(400, f"Could not read the file: {e}")
    if len(df_raw) > MAX_UPLOAD_ROWS:
        raise HTTPException(413, f"File has {len(df_raw):,} rows (limit {MAX_UPLOAD_ROWS:,}).")
    # A store in use is paused and set aside first (login and products kept), so the file
    # goes into your spreadsheets' workspace instead of merging into the store's products.
    paused = _set_aside_store_for_upload()
    try:
        with _state_lock:
            res = _ingest(df_raw, file.filename, append=(str(mode).lower() == "append"))
    except Exception:
        if paused:
            warmup()        # the spreadsheets were loaded unfitted for this upload
        raise
    if paused:
        res["storePaused"] = paused
    return res


def _read_sheet(contents: bytes, filename: str) -> pd.DataFrame:
    """Read an uploaded CSV / Excel file with the SKU column as TEXT.

    Read as numbers, a code like 00123 became 123 (and 123.0 once any cell was blank),
    so a later upload of the same product (from Excel, where it was text) arrived as a
    different product and split its history. The header is read first to find whichever
    column is the SKU, in any capitalisation."""
    is_csv = filename.lower().endswith(".csv")
    reader = (lambda **kw: pd.read_csv(io.BytesIO(contents), **kw)) if is_csv else \
             (lambda **kw: pd.read_excel(io.BytesIO(contents), **kw))
    head = reader(nrows=0)
    sku_cols = [c for c in head.columns if str(c).strip().lower().replace(" ", "_") == "sku"]
    return reader(dtype={c: str for c in sku_cols}) if sku_cols else reader()


_ISO_DATE = re.compile(r"^\s*\d{4}-\d{1,2}-\d{1,2}")
_SLASH_DATE = re.compile(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})")


def _parse_dates(col: pd.Series) -> tuple[pd.Series, str | None]:
    """Parse a Date column without guessing its format from the first row.

    pd.to_datetime on "01/02/2024, 05/02/2024, 13/02/2024" (day first: February) read the
    first two as Jan 2 and May 2 and dropped the 13th as unreadable, with no warning about
    the swap. Numbers-with-slashes are now checked across the whole column: any first
    number over 12 means day-first; any second number over 12 means month-first (US);
    if nothing tells them apart, US order is assumed and said so. Returns (dates, note)."""
    if pd.api.types.is_datetime64_any_dtype(col):
        return col, None
    s = col.astype("string").str.strip()
    nonblank = s.dropna()
    nonblank = nonblank[nonblank != ""]
    if not len(nonblank):
        return pd.to_datetime(s, errors="coerce"), None
    if nonblank.str.match(_ISO_DATE).mean() >= 0.8:
        return pd.to_datetime(s, errors="coerce", format="ISO8601"), None
    parts = nonblank.str.extract(_SLASH_DATE)
    if parts.notna().all(axis=1).mean() >= 0.8:
        a = pd.to_numeric(parts[0], errors="coerce")
        b = pd.to_numeric(parts[1], errors="coerce")
        day_first = bool((a > 12).any()) and not bool((b > 12).any())
        note = None
        if day_first:
            note = "Dates read as day/month/year (a first number above 12 was found)."
        elif not (b > 12).any():
            note = ("Dates could be day/month or month/day; read as month/day/year (US). "
                    "If that's wrong, write them as YYYY-MM-DD.")
        return pd.to_datetime(s, errors="coerce", dayfirst=day_first, format="mixed"), note
    return pd.to_datetime(s, errors="coerce", format="mixed"), None


def _repeated_block(df: pd.DataFrame) -> int:
    """How many times the whole file repeats as one block (1 = it doesn't): rows
    0..n-1 again as n..2n-1, and so on. A pasted-twice sheet repeats that way; a file
    with one row per sale repeats rows next to each other, which is real demand."""
    n = len(df)
    if n < 2:
        return 1
    rows = df.astype(str).agg("\u241f".join, axis=1).tolist()
    for k in (2, 3, 4):
        if n % k:
            continue
        m = n // k
        if all(rows[i * m:(i + 1) * m] == rows[:m] for i in range(1, k)):
            return k
    return 1


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
            auto_backtest: bool = True, reanchor: bool = True,
            cost_source: str = "shopify"):
    """Shared ingestion for ANY raw sales DataFrame — an uploaded file OR a live source
    like Shopify. Normalizes/dedups, optionally re-anchors, builds the per-SKU catalog,
    refits, and returns the same payload the upload endpoint always has. `stock_override` /
    `cost_override` (e.g. live Shopify inventory + unit costs) supply current values per
    SKU when the data carries no stock/cost columns — used only to prefill the dashboard.

    `reanchor` — TRUE for uploaded files, FALSE for a live source. See the block below;
    the short version is that a spreadsheet has an unknown vintage and a live feed does
    not, so shifting the one is a fix and shifting the other is corruption."""
    df_raw.columns = [c.strip().lower().replace(" ", "_") for c in df_raw.columns]
    # Identical rows are NOT dropped one by one any more. A file with one row per sale
    # has identical rows whenever the same item sells twice on a day at the same price:
    # 3 mugs became 1 while the message said "no double-counting". What IS collapsed is a
    # sheet pasted into itself: the whole block of rows repeated, in order, 2-4 times.
    # Checked here, on the rows as they came, before anything re-sorts them.
    copies_collapsed = _repeated_block(df_raw)
    if copies_collapsed > 1:
        df_raw = df_raw.iloc[: len(df_raw) // copies_collapsed].reset_index(drop=True)
    if not {"date", "units_sold"}.issubset(df_raw.columns):
        raise HTTPException(400, "Missing required columns 'Date' and 'Units_Sold'. "
                                 "Optional: SKU, SKU_Name, Category (or other attributes), Price, On_Promotion, Units_In_Stock.")
    has_sku = "sku" in df_raw.columns
    if has_sku:
        # Codes are text (see _read_sheet); trim stray spaces so "A1 " and "A1" are one product.
        _s = df_raw["sku"]
        df_raw["sku"] = _s.where(_s.isna(), _s.astype(str).str.strip())
    date_note = None
    try:
        df_raw["date"], date_note = _parse_dates(df_raw["date"])
        df_raw["units_sold"] = pd.to_numeric(df_raw["units_sold"], errors="coerce")
    except Exception as e:
        raise HTTPException(400, f"Could not parse data: {e}")
    # Most dates unreadable means the format was misread, not that the data is bad.
    # Better to stop and say so than to load a fraction of the history silently.
    _n = len(df_raw)
    _bad = int(df_raw["date"].isna().sum())
    if _n >= 10 and _bad / _n > 0.2:
        raise HTTPException(400, f"{_bad:,} of {_n:,} dates couldn't be read. "
                                 "Write dates as YYYY-MM-DD (for example 2024-02-13) and upload again.")
    # ── Data-quality accounting (reported back to the user) ──
    rows_in        = len(df_raw)
    bad_dates      = int(df_raw["date"].isna().sum())
    missing_sales  = int(df_raw["units_sold"].isna().sum())     # blanks → treated as 0
    df_raw["units_sold"] = df_raw["units_sold"].fillna(0).clip(lower=0)
    for c in ("price", "price_mixed", "on_promotion", "units_in_stock"):
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
        # The stock figures AS GIVEN, before blanks are filled. Blanks become 1 below so
        # the stockout handling reads an unknown day as "in stock" rather than sold out,
        # but the last known count must come from real figures: taken after the fill, a
        # blank latest cell became "1 unit in stock, from your file" and triggered urgent
        # reorders.
        df_raw["_stock_raw"] = df_raw["units_in_stock"].clip(lower=0)
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
    dups_remaining = int(df_raw.duplicated(subset=key).sum())
    if dups_remaining:
        agg = {}
        for c in df_raw.columns:
            if c in key:
                continue
            agg[c] = "sum" if c == "units_sold" else ("max" if c in ("on_promotion", "price_mixed") else "last")
        # (pandas "last" skips blanks, so _stock_raw keeps the day's last REAL figure.)
        _pr = None
        if "price" in df_raw.columns:
            # Same-day transactions at different prices: a units-weighted day price and a
            # mixed-day flag, not whichever row came last. Same rule as the live sources.
            _pr = daily_price_rollup(df_raw[key + ["units_sold", "price"]], keys=tuple(key))
        df_raw = df_raw.groupby(key, as_index=False).agg(agg)
        if _pr is not None:
            _pr = _pr[key + ["price", "price_mixed"]].rename(
                columns={"price": "_rp", "price_mixed": "_rm"})
            df_raw = df_raw.merge(_pr, on=key, how="left")
            df_raw["price"] = df_raw["_rp"].where(df_raw["_rp"].notna(), df_raw["price"])
            prev = df_raw["price_mixed"] if "price_mixed" in df_raw.columns else 0
            df_raw["price_mixed"] = pd.concat(
                [pd.Series(prev, index=df_raw.index).fillna(0), df_raw["_rm"].fillna(0)],
                axis=1).max(axis=1).astype(int)
            df_raw = df_raw.drop(columns=["_rp", "_rm"])
        df_raw = df_raw.sort_values("date").reset_index(drop=True)
    duplicate_rows_merged = rows_in - bad_dates - len(df_raw)
    data_quality = {
        "rowsIn": rows_in, "rowsUsed": len(df_raw),
        "badDatesDropped": bad_dates, "missingSalesFilledZero": missing_sales,
        "duplicateRowsMerged": int(max(duplicate_rows_merged, 0)),
    }
    if copies_collapsed > 1:
        data_quality["fileRepeatedTimes"] = copies_collapsed
    if date_note:
        data_quality["dateFormat"] = date_note

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
    attr_cols = [c for c in df_raw.columns if c not in R.RESERVED_COLS and c != "_stock_raw"]

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
    # A LIVE STORE'S RE-SYNC KEEPS WHAT THE PERSON SET. The nightly sync re-imports the
    # whole store as a replace, which used to wipe every product's planned price changes
    # and promotions (and any category set by hand) at 00:15 each night. Those describe
    # the future or the product, not the sales, so a product that comes back from its own
    # store keeps them. A spreadsheet upload still replaces everything: its product ids
    # and dates may mean something else entirely.
    carried: dict = {}
    if not append and not reanchor:
        carried = {sid: {"events": list(c.get("events") or []), "attrs": dict(c.get("attrs") or {}),
                         "attrs_set": list(c.get("attrs_set") or []),
                         "attrs_ai": list(c.get("attrs_ai") or [])}
                   for sid, c in _catalog.items() if c.get("live_source")}
    if not append:
        _catalog.clear()
        _publish_cache({})
        _sheet_costs.clear()
        _cost_sources.clear()

    loaded, errors = [], []
    last_stock = {}   # sid -> units_in_stock on the most recent dated row (<= today)
    last_cost = {}    # sid -> most recent per-unit cost from a Cost/Unit_Cost column
    missing_days_filled = 0
    rows_appended = overlap_rows = 0      # append-mode accounting, reported back
    added_skus, extended = [], []
    # The last date anywhere in this import. Every product's calendar is filled out to
    # here, so a product that stopped selling in June carries real zeros through to the
    # end of the data instead of a series that simply stops — which is what let a dead
    # product keep being forecast at its old rate. See fill_daily_gaps.
    try:
        _data_end = max(pd.to_datetime(g["date"]).max() for g, _ in groups_iter.values())
    except (ValueError, KeyError, TypeError):
        _data_end = None
    for sid, (grp, sname) in groups_iter.items():
        keep = ["date", "units_sold"] + [c for c in ("price", "price_mixed", "on_promotion", "units_in_stock") if c in grp.columns]
        # A product with no stock figure anywhere in the file has no stock data, even if
        # other products in the file do. Kept as a column of filled-in 1s, it read as
        # "counted, from your file".
        if "units_in_stock" in keep and "_stock_raw" in grp.columns and grp["_stock_raw"].isna().all():
            keep.remove("units_in_stock")
        dfc = grp[keep].rename(columns={"date": "ds", "units_sold": "y"}).copy()
        dfc["ds"] = pd.to_datetime(dfc["ds"])
        # Sheets that only carry a row per SALE day would otherwise hide every
        # zero-sale day from the model (over-forecasting + broken intermittency
        # detection). Reindex to a continuous daily calendar, same as Shopify.
        dfc, n_filled = fill_daily_gaps(dfc, end=_data_end)
        missing_days_filled += n_filled
        # Allow brand-new products in (as few as 2 rows). Thin SKUs are handled
        # honestly downstream: < 7 own-days → "establishing baseline" (forecast held),
        # and the eligibility/abstain tiers label anything still too thin to forecast.
        # A 30-row floor here would silently drop every new product on upload.
        if len(dfc) < 2:
            errors.append(f"{sid}: fewer than 2 rows (has {len(dfc)}), skipped."); continue
        # Carry the latest known on-hand stock forward so the UI can pre-fill it.
        # grp is already sorted by date; take the most recent row at or before today.
        _stk_col = "_stock_raw" if "_stock_raw" in grp.columns else "units_in_stock"
        if _stk_col in grp.columns:
            past = grp[grp["date"] <= today()]
            ss = pd.to_numeric((past if not past.empty else grp)[_stk_col], errors="coerce").dropna()
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
            _hand = set(prev.get("attrs_set") or [])
            _from_file = {k: v for k, v in attrs.items() if v is not None and k not in _hand}
            prev["attrs"] = {**(prev.get("attrs") or {}), **_from_file}
            # What the file says replaces an AI guess, and is the file's from now on.
            prev["attrs_ai"] = sorted(set(prev.get("attrs_ai") or []) - set(_from_file))
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
            _kept = carried.get(sid) or {}
            _file_keys = {k for k, v in attrs.items() if v is not None}   # what this file/store says
            if _kept:
                # The store's values fill in and update; what a person set by hand wins,
                # including a category they cleared on purpose.
                _hand = set(_kept.get("attrs_set") or [])
                attrs = {**_kept.get("attrs", {}), **{k: v for k, v in attrs.items() if v is not None}}
                for k in _hand:
                    if _kept.get("attrs", {}).get(k) is None:
                        attrs.pop(k, None)
                    else:
                        attrs[k] = _kept["attrs"][k]
            _catalog[sid] = {"df": dfc, "attrs": attrs, "sku_name": sname,
                             "mode": "uploaded", "filename": filename,
                             "events": list(_kept.get("events") or []),
                             "attrs_set": list(_kept.get("attrs_set") or []),
                             # An AI guess the store's own data now covers is the store's.
                             "attrs_ai": sorted(set(_kept.get("attrs_ai") or []) - _file_keys),
                             "sources": [filename], "date_shift_days": date_shift_days,
                             # Kept separate from `mode`, which the dashboard reads to decide
                             # whether "Clear all data" is offered. This flag is only about
                             # whether the dates may be moved.
                             "live_source": (not reanchor)}

    # Where a stock figure came from travels with it, exactly as cost provenance does.
    # "unknown" is a real, distinct answer: a product the store does not inventory-track
    # returns no count, and showing that as a number — any number — is the failure mode.
    stock_src: dict[str, str] = {sid: "sheet" for sid, v in last_stock.items() if v is not None}
    # The last REAL stock figure a file gave, kept on the product (and saved with it), so
    # /api/skus reads that rather than the history column, whose blanks are filled with 1.
    for sid in groups_iter:
        if sid in _catalog and (last_stock.get(sid) is not None or not append):
            _catalog[sid]["last_known_stock"] = last_stock.get(sid)
    if stock_override:                       # live inventory → prefill current stock
        for sid in groups_iter:
            if last_stock.get(sid) is None and stock_override.get(sid) is not None:
                last_stock[sid] = int(stock_override[sid])
                stock_src[sid] = "live"
    # Which source a cost came from travels with it. A number pulled from a live store and
    # one read out of a spreadsheet column deserve different trust, and the product
    # page now shows which it was — it cannot work that out from the value alone.
    # `cost_source` names the platform ("shopify", "square", …) rather than assuming one:
    # a Square cost labelled "shopify" is a lie told quietly, on the one screen whose whole
    # job is saying where a number came from. Defaults to "shopify" so any caller that
    # hasn't been updated keeps its old behaviour exactly.
    cost_src: dict[str, str] = {sid: "sheet" for sid in last_cost}
    if cost_override:                        # live unit costs → prefill unit cost
        for sid in groups_iter:
            if last_cost.get(sid) is None and cost_override.get(sid) is not None:
                last_cost[sid] = round(float(cost_override[sid]), 2)
                cost_src[sid] = cost_source

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

    # The catalogue _rebuild just published, bound once — the response is built from a
    # single snapshot rather than re-reading a name another thread may have swapped.
    _cache_snap = _cache()
    for sid in groups_iter:
        if sid not in _cache_snap:
            continue
        e = _cache_snap[sid]; dt = e["df_train"]
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
                       "stockSource": stock_src.get(sid, "unknown"),
                       "costSource": cost_src.get(sid),
                       "dateRange": {"start": dt["ds"].min().strftime("%B %d, %Y"), "end": dt["ds"].max().strftime("%B %d, %Y")}})
    # Start the measured recommendation straight away. Nobody should have to know the
    # Backtest tab exists to get a protection level grounded in their own history — the
    # cost curve is what you see meanwhile, and the UI labels it as provisional.
    # Costs come from the sheet's Cost column if it has one; the dashboard re-runs later
    # with Scorecard costs if the user enters better ones.
    _sheet_costs.update({str(sid): float(c) for sid, c in last_cost.items()})
    _cost_sources.update({str(sid): cost_src.get(sid, "sheet") for sid in last_cost})

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
        # Does the measurement still describe this data? Three answers, not two.
        #
        # This used to compare hashes, which differ the instant a single day of sales
        # lands — so every nightly sync threw the measured tiers away and the tool fell
        # back to cost-curve estimates until the NEXT sync happened to find an empty cache
        # and re-ran. Protection levels alternated night to night for no reason connected
        # to demand. A tier chosen from several test windows across months does not stop
        # describing a product because yesterday's sales arrived.
        new_parts = _BTSTORE.fingerprint_parts(_catalog)
        if _backtest_fingerprint and _BTSTORE.fingerprint(_catalog) == _backtest_fingerprint:
            print("Live sync — data unchanged for testing purposes; automatic backtest skipped.")
        elif _BTSTORE.grew_only(_backtest_parts, new_parts):
            # History extended and nothing was rewritten. Keep the measurements and let
            # them age; the weekly refresh (or one of the auto-run conditions) replaces
            # them on purpose rather than by accident.
            globals()["_backtest_fingerprint"] = _BTSTORE.fingerprint(_catalog)
            globals()["_backtest_parts"] = new_parts
            print("Live sync — history grew; measured protection levels kept (they will be "
                  "re-measured on the weekly refresh).")
        else:
            _drop_backtest_state("the sync rewrote the sales history they were measured on")
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


# ═════════════════════════════════════════════════════════════════════════════
#  LIVE STORE CONNECTIONS  (Shopify, Square, …)
#
#  One code path per operation, not one per platform. `sources.py` holds the registry;
#  everything below looks a source up by key and never learns its name. See that module
#  for why "add a second `if source == 'square'` branch" was the wrong answer.
# ═════════════════════════════════════════════════════════════════════════════

def _resolve_connection(payload: dict) -> tuple:
    """Turn a request body into (source_key, creds, connection_id, saved_label).

    Three shapes are accepted, in priority order:
      1. {"connectionId": "abc"}      — a saved connection; the token never left the server
      2. {"source": "square", "creds": {...}}
      3. {"shop": ..., "token": ...}  — the original flat Shopify body, still honoured so an
                                        older frontend bundle keeps working

    Anything else in the body (a different `days`, a narrower location list) layers on top
    of a saved connection's credentials, so "use my saved store but pull only 30 days"
    works without re-entering the token.
    """
    payload = payload or {}
    cid = payload.get("connectionId") or payload.get("connection_id")
    if cid:
        saved = _CONN.get(cid)
        if not saved:
            raise HTTPException(404, "That saved connection no longer exists. Reconnect the store.")
        creds = dict(saved.get("creds") or {})
        creds.update({k: v for k, v in (payload.get("creds") or {}).items() if v not in (None, "")})
        # The saved label travels with it: a catalog imported from "Cousin's store" should
        # say so, not fall back to the generic platform name.
        return saved.get("source"), creds, cid, saved.get("label")

    source = (payload.get("source") or "").strip().lower()
    creds = dict(payload.get("creds") or {})
    if not source:
        if payload.get("shop") or payload.get("token"):          # legacy flat Shopify body
            source = "shopify"
            creds = {"shop": payload.get("shop"), "token": payload.get("token"),
                     "apiVersion": payload.get("apiVersion")}
        else:
            raise HTTPException(400, "No store connection given. Pick a saved account or enter credentials.")
    # Legacy flat shape WITH an explicit source ({"source": "shopify", "shop": …}). Only
    # applies when the body has no `creds` key at all — an explicitly empty creds dict means
    # "use this source's environment variables", and must not be back-filled from the body,
    # or {"source": x, "creds": {}} would resolve to creds={"creds": {}}.
    if not creds and "creds" not in payload:
        creds = {k: v for k, v in payload.items()
                 if k not in ("source", "days", "save", "label", "connectionId", "connection_id")}
    return source, creds, None, None


def _source_error(source_key: str):
    """The source's own exception type, so a clean 'your token is wrong' message survives
    instead of being flattened into a 500."""
    try:
        return _SRC.error_class(source_key)
    except Exception:                                    # noqa: BLE001
        return Exception


@app.get("/api/sources")
def list_sources():
    """What the dashboard needs to render a connect form for each platform, including which
    fields are secret. Adding a platform gives it a working form with no frontend change."""
    return {"sources": _SRC.catalogue()}


@app.get("/api/connections")
def list_connections():
    """Saved store connections — the 'accounts' you pick instead of re-pasting a token.

    Browser-safe by construction: connections.public() strips every secret field and leaves
    a masked hint. The token itself never leaves this process.
    """
    return {"connections": _CONN.list_all()}


@app.post("/api/connections")
def save_connection(payload: dict = Body(default={})):
    """Create or update a saved connection. Body: { source, label, creds, id? }.

    A secret that isn't resent is kept, so renaming a connection — or changing which
    locations it pulls — doesn't require the token the browser never had.
    """
    source = (payload.get("source") or "").strip().lower()
    try:
        _SRC.get(source)
    except KeyError as e:
        raise HTTPException(400, str(e))
    creds = payload.get("creds") or {}
    label = (payload.get("label") or "").strip() or _SRC.describe(source, creds)
    return _CONN.upsert(source, label, creds, connection_id=payload.get("id"))


@app.delete("/api/connections/{connection_id}")
def delete_connection(connection_id: str):
    slot = _WS.store_slot(connection_id)
    # Read BEFORE deleting: with nothing recorded, whose products are loaded is worked
    # out from the saved stores, and this one is about to stop being one.
    was = _current_slot() if _CONN.get(connection_id) else None
    if not _CONN.delete(connection_id):
        raise HTTPException(404, "No such connection.")
    # Its set-aside products go with it. If ITS products are the ones loaded, what shows
    # next is recorded as the spreadsheets: your set-aside sheets come back if there are
    # any, otherwise its products stay loaded and are yours as a spreadsheet would be.
    # Leaving "showing" unrecorded let a restart guess another store's workspace, and the
    # next switch then wrote these products over that store's set-aside copy.
    if was == slot and _STORE.enabled():
        if _WS.has(_WS.SHEETS):
            _swap_workspace(_WS.SHEETS)
        else:
            _WS.set_showing(_WS.SHEETS)
    elif _WS.showing() == slot:
        _WS.set_showing(_WS.SHEETS)
    _WS.drop(slot)
    _closed.drop(slot)
    _apply_store_zone()
    return {"success": True, "deleted": connection_id}


# ── Workspaces: a store's products, or your spreadsheets ─────────────────────────
# See workspace.py. Pausing a store sets its products aside (with their events, categories
# and measured protection levels) and brings back whatever spreadsheets were loaded before;
# putting it back in use does the reverse and catches up with a sync. The saved login is
# never touched either way.
def _ws_dirs() -> list:
    return [("catalog", _STORE.path()), ("backtest", _BTSTORE._DIR)]


def _infer_slot(live: bool) -> str:
    """Whose products these are, when nothing was recorded (an install from before
    workspaces existed). Store products belong to the store in use or, with none in use,
    the store used most recently. Paused or not doesn't decide whose they ARE: working it
    out from "in use" alone called a paused store's products spreadsheets and never set
    them aside."""
    if not live:
        return _WS.SHEETS
    conns = _CONN.list_active() or _CONN.list_all()
    return _WS.store_slot(conns[0]["id"]) if conns else _WS.SHEETS


def _current_slot() -> str:
    """The workspace loaded now. Worked out once when there's no record, then recorded, so
    pausing or resuming a store can't change the answer halfway through a switch."""
    s = _WS.showing()
    if s:
        return s
    with _state_lock:
        live = any(e.get("live_source") for e in _catalog.values())
        empty = not _catalog
    s = _infer_slot(live)
    if not empty and _STORE.enabled():
        _WS.set_showing(s)
    return s


def _reconcile_workspace() -> None:
    """At startup: a paused store's products must not be what's showing. (They could be,
    from before workspaces existed, or from the switch that got this wrong.) Set them
    aside and bring back the spreadsheets, before anything is fitted."""
    if not _STORE.enabled():
        return
    try:
        s = _WS.showing()
        if not s:
            saved, _ = _STORE.load()
            if not saved:
                return
            s = _infer_slot(any(e.get("live_source") for e in saved.values()))
            _WS.set_showing(s)
        cid = _WS.connection_of(s)
        conn = _CONN.get(cid) if cid else None
        if conn and not _CONN.is_active(conn):
            _WS.park(s, _ws_dirs())
            _WS.unpark(_WS.SHEETS, _ws_dirs())
            _WS.set_showing(_WS.SHEETS)
            print(f"Store '{conn.get('label')}' is paused: its products were set aside.")
    except Exception as exc:          # never block startup over this
        print(f"Workspace check skipped: {exc}")


def _showing_store_id() -> str | None:
    return _WS.connection_of(_current_slot())


def _live_readings_apply(sku_id) -> bool:
    """Whether the store's live readings (shelf price, hourly stock) apply to the loaded
    products. They are filed by product code only, and the sampler keeps reading a paused
    store, so a spreadsheet product with the same code as one of its products picked up
    that store's prices and stock. They apply only while a store's products are the ones
    loaded. (With saving switched off there are no workspaces to tell them apart, and
    the readings apply as they always did.)"""
    try:
        if not _STORE.enabled():
            return True
        return _WS.is_store(_current_slot())
    except Exception:                                       # noqa: BLE001
        return False


def _swap_workspace(target: str, refit: bool = True):
    """Set the loaded products aside under their workspace and load `target`'s. Returns
    None when `target` is already loaded, else whether it had anything to bring back.
    With refit=False the products are loaded into memory without fitting (the caller is
    about to re-ingest them)."""
    with _state_lock:
        cur = _current_slot()
        if cur == target:
            return None
        if _catalog:
            _persist_catalog()
            _WS.park(cur, _ws_dirs())
        else:
            _WS.drop(cur)
        had = _WS.unpark(target, _ws_dirs())
        _WS.set_showing(target)
        _bump_ws_epoch()           # any backtest still running measured the other products
        _apply_store_zone()        # the workspace now loaded may keep a different clock
        if not refit:
            saved, extras = _STORE.load()
            _catalog.clear(); _publish_cache({})
            _catalog.update(saved)
            _sheet_costs.clear(); _cost_sources.clear()
            _sheet_costs.update({k: float(v) for k, v in (extras.get("sheet_costs") or {}).items()})
            _cost_sources.update({k: str(v) for k, v in (extras.get("cost_sources") or {}).items()})
            # The measured protection levels in memory belong to the workspace just set
            # aside. Left in place, the re-import that follows compared them with these
            # products, found the history "rewritten" and wiped the tiers this workspace
            # had saved on disk. Forget them and read this workspace's own back.
            _forget_backtest_memory()
            _restore_backtest()
    if refit:
        warmup()
    return had


def _undo_import_swap(prev_slot: str | None) -> None:
    """An import that switched workspaces and then failed: go back to what was showing.
    The failed ingest may have half-replaced the products in memory, but nothing reached
    disk (the catalog is saved at the end of a successful ingest), so the disk copy is
    what gets set aside again."""
    if not prev_slot:
        return
    try:
        with _state_lock:
            saved, _ = _STORE.load()
            _catalog.clear(); _publish_cache({})
            _catalog.update(saved)
        _swap_workspace(prev_slot)
    except Exception as exc:                                 # noqa: BLE001
        print(f"Could not switch back after a failed import ({exc}).")


def _set_aside_store_for_upload() -> str | None:
    """A spreadsheet upload while a store is in use: pause the store and switch to the
    spreadsheets' workspace first, so the file never merges into (or replaces) the
    store's products. Returns the paused store's name, or None when nothing changed."""
    if not _STORE.enabled():
        return None
    slot = _current_slot()
    if not _WS.is_store(slot):
        return None
    conn = _CONN.get(_WS.connection_of(slot)) or {}
    if conn:
        _CONN.set_active(conn["id"], False)
    # refit=False: the upload that follows fits everything anyway.
    _swap_workspace(_WS.SHEETS, refit=False)
    _apply_store_zone()
    return conn.get("label") or "The store"


@app.get("/api/workspace")
def workspace_status():
    slot = _current_slot()
    cid = _WS.connection_of(slot)
    conn = _CONN.get(cid) if cid else None
    return {"showing": "store" if cid else "sheets", "connectionId": cid,
            "label": (conn or {}).get("label"), "parked": _WS.parked(),
            "restoring": _restore_state.get("status") == "restoring"}


@app.post("/api/connections/{connection_id}/use")
def use_connection(connection_id: str, payload: dict = Body(default={})):
    """Body: { active: true | false }.

    true  → this store is in use: synced nightly and its products shown. Whatever is
            loaded now (spreadsheets, or another store) is set aside first. If the store's
            products were set aside earlier they come straight back as they were (and the
            nightly sync catches them up if they've fallen behind); if not, `needsImport`
            tells the page to import them.
    false → paused: its products are set aside (not deleted), your spreadsheets come
            back, and nothing syncs from it until it's back in use. The login stays saved,
            and stock is still read hourly (those hours can't be recovered later).
    """
    if not _CONN.get(connection_id):
        raise HTTPException(404, "No such connection.")
    if not _STORE.enabled():
        raise HTTPException(400, "Switching needs saved data, and saving is switched off "
                                 "(LOGITRACK_PERSIST=0).")
    active = bool(payload.get("active", True))
    slot = _WS.store_slot(connection_id)
    needs_import = False
    had = None
    if active:
        _CONN.set_active(connection_id, True)
        had = _swap_workspace(slot)
        with _state_lock:
            empty = not _catalog
        # Set-aside products come back exactly as they were. If they've fallen more than a
        # sync behind, the nightly sync's own staleness check (it asks the data, see
        # _data_is_stale) catches them up on its next look; with nothing set aside, the
        # page is told to import.
        if not had and (had is False or empty):
            needs_import = True
    else:
        # Whose products are loaded is read BEFORE pausing: with nothing recorded it's
        # worked out from which store is in use, and after pausing there isn't one.
        was = _current_slot()
        _CONN.set_active(connection_id, False)
        if was == slot:
            had = _swap_workspace(_WS.SHEETS)
    return {"connections": _CONN.list_all(), "showing": "store" if active else "sheets",
            "restored": bool(had), "needsImport": needs_import,
            "restoring": _restore_state.get("status") == "restoring"}


@app.post("/api/connections/test")
def test_connection(payload: dict = Body(default={})):
    """Check credentials WITHOUT importing anything, and report what they can see.

    This exists because the alternative — discovering the token is wrong halfway through a
    two-year pull that has already replaced the catalog — is a bad way to find out. For
    Square it also returns the locations, which is what the location picker reads.
    """
    source, creds, cid, saved_label = _resolve_connection(payload)
    try:
        info = {"source": source, "ok": True,
                "label": saved_label or _SRC.describe(source, creds)}
        if source == "square":
            import square_source as SQ
            info["locations"] = SQ.fetch_locations(
                access_token=creds.get("accessToken") or creds.get("access_token"),
                environment=creds.get("environment") or "production",
                api_version=creds.get("apiVersion"))
        else:
            info["products"] = len(_SRC.fetch_catalog_meta(source, creds) or {})
    except _source_error(source) as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:                               # noqa: BLE001
        raise HTTPException(400, f"Could not reach {source}: {e}")
    if cid:
        _CONN.touch(cid)
    return info


@app.post("/api/upload/source")
def upload_from_source(payload: dict = Body(default={})):
    """Pull live sales from any connected store and load them exactly as if a sheet had been
    uploaded.

    Body: { connectionId } or { source, creds }, plus optional { days, save, label }.
    `save: true` stores the credentials as a reusable connection and returns it, so the next
    import is one click and the hourly tick can run with nobody present.
    """
    source, creds, cid, saved_label = _resolve_connection(payload)
    days = payload.get("days")
    if days in ("", "all"):
        days = None

    try:
        raw = _SRC.fetch_sales(source, creds, days=days)
    except _source_error(source) as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:                               # noqa: BLE001
        raise HTTPException(400, f"{source.title()} fetch failed: {e}")
    # The store's own time zone (Square location, Shopify shop), filed for its workspace
    # below and used for the app's clock unless one was picked on the Closed days panel.
    _store_tz = None
    try:
        _store_tz = ((raw.attrs.get(source) or {}).get("timezone")) if hasattr(raw, "attrs") else None
    except Exception:                                    # noqa: BLE001
        _store_tz = None

    # Category + current inventory + unit cost — best-effort by design: if the token lacks
    # the product scope, or the platform simply doesn't expose costs (Square gates them
    # behind Retail Premium), the sales still import. They just arrive without the prefill.
    stock_map = cost_map = None
    meta_error = None
    try:
        meta = _SRC.fetch_catalog_meta(source, creds)
        if meta:
            if "category" not in raw.columns or raw["category"].isna().all():
                raw["category"] = raw["sku"].map(lambda s: (meta.get(s) or {}).get("category"))
            stock_map = {s: m["stock"] for s, m in meta.items() if m.get("stock") is not None}
            cost_map = {s: m["cost"] for s, m in meta.items() if m.get("cost") is not None}
            # The shelf price and any discount on now — filed BEFORE ingesting, so the
            # rebuild inside _ingest already forecasts with it.
            try:
                _live.record(meta, source)
            except Exception as exc:                     # noqa: BLE001 — never fail an import over it
                print(f"Could not file live prices ({exc}).")
    except Exception as e:                               # noqa: BLE001
        meta_error = str(e)

    # An explicit label wins, then the saved connection's own name, then the platform's
    # generic description. Without the middle one a catalog pulled from a saved account
    # gets filed as "Square" rather than as the name its owner gave it.
    # ── Don't let a hiccup replace a good catalog ────────────────────────────
    # An import REPLACES everything. That is fine when a person clicked it and can see the
    # result; it is not fine at 00:15 with nobody watching, because a partial response —
    # a truncated page, a token that lost a scope, a location that went quiet — would
    # silently swap 28 products for 3 and every forecast would follow the smaller set.
    # Scheduled syncs therefore refuse a drastic shrink and keep yesterday's data, which
    # is stale but correct. A manual import does not set this: you asked, you can see.
    if payload.get("guardCatalogShrink"):
        with _state_lock:
            existing = len(_catalog)
        incoming = int(raw["sku"].nunique()) if "sku" in getattr(raw, "columns", []) else 0
        floor = max(1, int(existing * SYNC_MIN_SKU_RATIO))
        if existing and incoming < floor:
            return {"success": False, "guardBlocked": True, "source": source,
                    "existingSkus": existing, "incomingSkus": incoming,
                    "errors": [f"Sync refused: the store reported {incoming} products but the "
                               f"catalog holds {existing}, so the existing data was kept. "
                               f"Re-import by hand if the drop is real."]}

    label = (payload.get("label") or "").strip() or saved_label or _SRC.describe(source, creds)

    # Import statistics the source chose to report — how many register-typed line items were
    # dropped, how many products have no SKU set. Surfaced rather than swallowed, because
    # "3,000 rows imported" quietly hides "and 400 line items weren't products".
    source_stats = dict(getattr(raw, "attrs", {}).get(source) or {})

    # File the reading BEFORE ingesting. StockLog is the only thing that remembers on-hand
    # across a restart, and an import is a perfectly good reading — waiting for the hourly
    # tick would leave a fresh catalog with no inventory at all until the browser next
    # happened to be open.
    if stock_map:
        try:
            _slog.record({str(k): v for k, v in stock_map.items()})
        except Exception as exc:                         # noqa: BLE001 — never fail an import over it
            print(f"Could not log inventory reading ({exc}).")

    # A saved store imports into ITS workspace: whatever else is loaded (your spreadsheets,
    # another store) is set aside first, and the store's own set-aside products come back
    # so the planned events and categories on them carry over into the fresh import.
    #
    # A NEW store being saved is filed first, so it gets a workspace of its own and goes
    # through the same path. It used to set aside only spreadsheets: connecting a second
    # store while one was in use imported straight over the first store's products, and
    # nothing kept a copy. A one-off import (not saved) while a store is in use pauses
    # that store and loads into your spreadsheets' workspace, for the same reason.
    prev_slot = _current_slot() if _STORE.enabled() else None
    prev_active = next((c["id"] for c in _CONN.list_all() if c.get("active")), None)
    created_cid = None
    store_paused = None
    swapped = False
    if _STORE.enabled() and not cid and payload.get("save"):
        created_cid = cid = _CONN.upsert(source, label, creds)["id"]
    if cid and _STORE.enabled():
        _CONN.set_active(cid, True)           # pauses whichever store was in use
        swapped = _swap_workspace(_WS.store_slot(cid), refit=False) is not None
    elif _STORE.enabled() and _WS.is_store(prev_slot):
        was = _CONN.get(_WS.connection_of(prev_slot)) or {}
        if was:
            _CONN.set_active(was["id"], False)
            store_paused = was.get("label") or "The store"
        swapped = _swap_workspace(_WS.SHEETS, refit=False) is not None

    if _CLOCK.valid(_store_tz):
        try:
            _closed.set_detected(_current_slot(), _store_tz, source)
        except Exception as exc:                         # noqa: BLE001 — never fail an import over it
            print(f"Could not file the store's time zone ({exc}).")
    _apply_store_zone()         # before ingesting: the fit starts from the store's "today"

    try:
        with _state_lock:
            res = _ingest(raw, label, stock_override=stock_map, cost_override=cost_map,
                          reanchor=False,   # live dates are the real dates
                          cost_source=source)
    except Exception:
        # Put back what was showing before this import started, as it was.
        if swapped:
            _undo_import_swap(prev_slot)
        if created_cid:
            _CONN.delete(created_cid)
            _WS.drop(_WS.store_slot(created_cid)); _closed.drop(_WS.store_slot(created_cid))
        if prev_active:
            _CONN.set_active(prev_active, True)
        elif cid:
            _CONN.set_active(cid, False)
        _apply_store_zone()
        raise
    if store_paused:
        res["storePaused"] = store_paused

    res["source"] = source
    if source_stats:
        res.setdefault("dataQuality", {}).update(
            {f"source_{k}": v for k, v in source_stats.items()})
    if meta_error:
        res.setdefault("errors", []).append(
            f"Stock and category prefill unavailable ({meta_error}). Sales imported fine.")
    res["costsAvailable"] = bool(cost_map)

    if payload.get("save") or cid:
        saved = _CONN.upsert(source, label, creds, connection_id=cid)
        _CONN.touch(saved["id"])
        if _STORE.enabled():
            _CONN.set_active(saved["id"], True)
            _WS.set_showing(_WS.store_slot(saved["id"]))
            if _CLOCK.valid(_store_tz):
                try:
                    _closed.set_detected(_WS.store_slot(saved["id"]), _store_tz, source)
                except Exception:                        # noqa: BLE001
                    pass
            _apply_store_zone()
        res["connection"] = _CONN.public(_CONN.get(saved["id"]) or {}) or saved
    return res


# ═════════════════════════════════════════════════════════════════════════════
#  THE NIGHTLY SYNC
#
#  Everything above this point waits to be asked. Nothing re-imported on a schedule, so a
#  catalog was only ever as fresh as the last time someone clicked Import — which the
#  forecast math already defends against (fc_fwd is anchored to today, and dataAgeDays is
#  reported) but cannot fix: correct arithmetic on week-old demand is still week-old demand.
#
#  `scheduler.py` owns WHEN. This owns WHAT.
# ═════════════════════════════════════════════════════════════════════════════

# A scheduled sync that would shrink the catalog below this fraction of its current size is
# refused. See the guard in upload_from_source for why.
SYNC_MIN_SKU_RATIO = 0.5


# Which weekday the protection tiers are re-measured on (0 = Monday). Weekly, because the
# tier is chosen from several test windows spanning months — one more day of sales barely
# moves it, while a full re-measure costs minutes of refitting. Weekly also lands on the
# same spine the forecast log already uses, so both validators refresh on one rhythm.
SYNC_BACKTEST_WEEKDAY = int(os.environ.get("LOGITRACK_BACKTEST_WEEKDAY", "0"))


def _combos_in_use(limit: int = 12) -> list:
    """Distinct (lead, coverage) pairs worth measuring, commonest first.

    One run can score several pairs, and measuring the ones people actually plan with is
    far cheaper than re-running every time a P80 lead time moves. Capped, because each
    extra pair costs scoring time.
    """
    from collections import Counter
    counts = Counter((v["lead"], v["coverage"]) for v in _settings_seen.values())
    return [[L, C] for (L, C), _n in counts.most_common(limit)]


def _backtest_needed() -> list:
    """The reasons a backtest should run NOW, regardless of the weekly slot.

    These are exactly the three ways a product falls back to a cost-curve estimate. Each
    used to be a resting state: the tool quietly showed the weaker answer and waited for
    somebody to notice and press a button. A fallback is better treated as a TRIGGER — if
    the estimate is showing because something changed, re-measure the thing that changed.
    """
    reasons = []
    with _state_lock:
        catalog = dict(_catalog)
        measured = {k.split("|")[0] for k in _backtest_tier_cache}
        # Tested last time and explained (no cost, sells below cost, too few windows that
        # could judge the buffer): re-running tonight can't change that answer, so it
        # isn't a reason to. Only a product that was too SHORT can have grown into one.
        explained = {sid for sid, why in (_backtest_exclusions or {}).items()
                     if not str(why).startswith("it only has")}

    # 1. Products that have crossed the testable threshold since the last run.
    newly = []
    for sid, e in catalog.items():
        if sid in measured or sid in explained:
            continue
        df = e.get("df")
        if df is None or len(df) < 2:
            continue
        try:
            span = int((pd.to_datetime(df["ds"]).max() - pd.to_datetime(df["ds"]).min()).days)
        except Exception:                                # noqa: BLE001
            continue
        if span >= BACKTEST_MIN_TESTABLE_DAYS:
            newly.append(sid)
    if newly:
        reasons.append({"reason": "newly-testable", "skus": sorted(newly)[:20],
                        "count": len(newly),
                        "detail": f"{len(newly)} product(s) now have enough history to test."})

    # 2. Planning settings in use that no measurement covers, even within tolerance.
    #    The lead time is a P80 that drifts as shipments land, so this is the common one.
    uncovered = []
    for sid, lead, cov in _settings_in_use():
        if sid not in measured:
            continue
        if _tier_lookup(sid, lead, cov)[0] is None:
            uncovered.append({"sku": sid, "lead": lead, "coverage": cov})
    if uncovered:
        reasons.append({"reason": "settings-uncovered", "settings": uncovered[:20],
                        "count": len(uncovered),
                        "detail": (f"{len(uncovered)} product(s) are planned at a lead time or "
                                   f"coverage the last run never measured.")})

    # 3. Economics that moved beyond what a re-price can absorb.
    drifted = []
    for sid in measured:
        rec = next((v for k, v in _backtest_tier_cache.items() if k.startswith(f"{sid}|")), None)
        why = _economics_drifted(sid, (_sheet_costs.get(sid) if _sheet_costs else None),
                                 None, (rec or {}).get("holdingPct"))
        if why:
            drifted.append({"sku": sid, "why": why})
    if drifted:
        reasons.append({"reason": "economics-drifted", "skus": drifted[:20],
                        "count": len(drifted),
                        "detail": f"{len(drifted)} product(s) were measured on costs that have changed."})
    return reasons


def _settings_in_use() -> list:
    """(sku, lead, coverage) actually being planned with right now.

    Stock, lead times and coverage live in the browser, so the server only learns them when
    a forecast is requested. Every request records its combination here, which is what lets
    a sync know that a lead time drifted without anyone telling it.
    """
    with _state_lock:
        return [(sid, v["lead"], v["coverage"]) for sid, v in _settings_seen.items()]


# {sku_id: {lead, coverage, at}} — the last settings each product was planned with.
_settings_seen: dict[str, dict] = {}


def _note_settings(sku_id, lead, coverage):
    with _state_lock:
        _settings_seen[str(sku_id)] = {"lead": int(lead), "coverage": int(coverage),
                                       "at": time.time()}


# How long to wait for the backtest before calling the day incomplete and trying again
# later. It is the slow stage by a wide margin, and the one most likely to be interrupted.
SYNC_BACKTEST_TIMEOUT = 20 * 60


def _wait_for_backtest(timeout=SYNC_BACKTEST_TIMEOUT, poll=2.0) -> dict:
    """Block until the background backtest job settles.

    The sync used to return the moment the job was KICKED OFF, so "today is done" meant
    "the fetch and refit are done and something expensive is still running". Quit the app
    then and the backtest died with no trace: its state is in memory, its result is
    session-scoped, and the persisted copy is fingerprinted against data that now has no
    matching result — so it was silently discarded and nothing re-ran it. Waiting is what
    makes the completion marker mean what it says.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = _bt_job_snapshot()
        status = snap.get("status")
        if status == "done":
            return {"ok": True, "status": status}
        if status == "error":
            return {"ok": False, "status": status, "error": snap.get("error")}
        if status == "idle":
            # Nothing running. Whether that is fine depends entirely on whether a run was
            # WANTED, which only the caller knows — so report the fact and let it decide.
            # Treating idle as success is how a night that dropped every measured tier
            # reported itself complete.
            return {"ok": True, "status": "idle", "note": "no backtest job was running"}
        time.sleep(poll)
    return {"ok": False, "status": "timeout",
            "error": f"backtest still running after {int(timeout / 60)} minutes"}


def _run_daily_sync(ledger=None, day=None) -> dict:
    """Bring everything current, one stage at a time, resuming whatever is outstanding.

    THREE STAGES, MARKED ONLY ON SUCCESS

      fetch     pull sales + inventory from the saved connection and ingest them
      refit     re-fit every SKU (this is what makes every tab current)
      backtest  re-measure protection tiers against the new data

    A stage with no marker is indistinguishable from one that never ran, which is what an
    interrupted process should look like. On the next start the missing stages run and the
    finished ones don't — and `fetch` in particular is never repeated, because re-running
    it costs an API call against someone else's rate limit and returns the same data.
    """
    stages = ("fetch", "refit", "backtest")
    day = day or today().date()
    out = {"day": day.isoformat() if hasattr(day, "isoformat") else str(day), "stages": {}}

    def done(stage):
        """Has this stage finished — either earlier today, or a moment ago in this run?

        Both halves matter. The ledger covers a resume across a restart; `out["stages"]`
        covers the same run, because ingest refits as it loads and the refit stage below
        must not immediately repeat it. Consulting only the ledger made the job correct
        with one and wrong without it.
        """
        if out["stages"].get(stage, {}).get("ok"):
            return True
        return bool(ledger and ledger.stage_done(day, stage))

    def mark(stage, ok, note=None):
        out["stages"][stage] = {"ok": ok, "note": note}
        if ledger:
            ledger.mark_stage(day, stage, ok=ok, note=note)

    saved = _CONN.list_all()
    if not saved:
        return {**out, "ok": False, "reason": "no-saved-connection",
                "detail": "Nothing to sync from. Connect a store and tick Remember this account."}
    active = [c for c in saved if c.get("active")]
    if not active:
        # Paused on purpose: a skip, not a failure (and nothing to retry).
        return {**out, "ok": True, "skip": True, "reason": "store-paused",
                "detail": "The store is paused, so nothing syncs until it's back in use."}
    cid = active[0]["id"]
    out["label"] = active[0].get("label")

    # ── 1. fetch + ingest ────────────────────────────────────────────────────
    if done("fetch"):
        out["stages"]["fetch"] = {"ok": True, "note": "already fetched today"}
    else:
        try:
            res = upload_from_source({"connectionId": cid, "guardCatalogShrink": True})
        except HTTPException as exc:
            mark("fetch", False, str(exc.detail))
            return {**out, "ok": False, "reason": "fetch-failed", "detail": str(exc.detail)}
        except Exception as exc:              # noqa: BLE001
            mark("fetch", False, str(exc))
            return {**out, "ok": False, "reason": "fetch-failed", "detail": str(exc)}
        if res.get("guardBlocked"):
            mark("fetch", False, "refused a catalog shrink")
            return {**out, "ok": False, "reason": "guard-catalog-shrink",
                    "existingSkus": res.get("existingSkus"), "incomingSkus": res.get("incomingSkus"),
                    "detail": (res.get("errors") or ["Refused a drastic catalog shrink."])[0]}
        loaded = res.get("loadedSkus")
        out["source"] = res.get("source")
        out["skus"] = len(loaded) if isinstance(loaded, list) else loaded
        out["dataQuality"] = res.get("dataQuality")
        mark("fetch", True)
        # _ingest already refits as part of loading, so the refit rides along with a
        # successful fetch rather than being repeated immediately for no reason.
        mark("refit", True, "refit as part of ingest")

    # ── 2. refit (only reached when fetch was already done on an earlier attempt) ──
    if not done("refit"):
        try:
            with _state_lock:
                _rebuild(today())
            mark("refit", True, "re-fit from the persisted catalog")
        except Exception as exc:              # noqa: BLE001
            mark("refit", False, str(exc))
            return {**out, "ok": False, "reason": "refit-failed", "detail": str(exc)}

    # ── 3. backtest ──────────────────────────────────────────────────────────
    # Runs on the weekly weekday, OR whenever one of the three fallback conditions says a
    # product is currently resting on an estimate it shouldn't be. Everything else is a
    # deliberate skip, recorded as such — a skipped stage is NOT the same as a finished
    # one, and this used to report "ok" whenever the job happened to be idle, which on a
    # night that had just dropped the tiers meant declaring the day complete while every
    # protection level was an estimate.
    if done("backtest"):
        out["stages"]["backtest"] = {"ok": True, "note": "already measured today"}
    else:
        weekday_due = day.weekday() == SYNC_BACKTEST_WEEKDAY
        triggers = _backtest_needed()
        out["backtestTriggers"] = triggers
        if not weekday_due and not triggers:
            # Nothing to re-measure. The existing tiers still stand and are simply ageing.
            mark("backtest", True, "not due: measured tiers still current")
            out["backtest"] = {"ok": True, "status": "skipped",
                               "note": "weekly refresh not due and no product fell back to an estimate"}
        else:
            why = "weekly refresh" if weekday_due else "; ".join(t["detail"] for t in triggers)
            started = False
            try:
                with _state_lock:
                    started = _run_backtest_job(
                        {"horizon": 44, "n_cutoffs": "auto", "step": 28, "lead": 14,
                         "coverage": 30, "service_pct": 95, "min_train": 120,
                         "holding_pct": _session_holding_pct,
                         # Measure every lead/coverage actually in use, so a drifting P80
                         # lead time lands on a measurement instead of an estimate.
                         "combos": _combos_in_use()},
                        _effective_sku_costs(None) or {}, "sync")
            except Exception as exc:                     # noqa: BLE001
                mark("backtest", False, str(exc))
                return {**out, "ok": False, "reason": "backtest-failed", "detail": str(exc)}
            bt = _wait_for_backtest()
            bt["why"] = why
            bt["startedHere"] = started
            out["backtest"] = bt
            if bt.get("ok"):
                mark("backtest", True, why)
            else:
                # Left unmarked on purpose: an interrupted or timed-out measurement should
                # be picked up again rather than papered over as finished.
                mark("backtest", False, bt.get("error"))
                return {**out, "ok": False, "reason": "backtest-incomplete",
                        "detail": bt.get("error") or "backtest did not finish"}

    # ── 4. the forecast log and the inventory reading ────────────────────────
    try:
        tick = livelog_tick({"connectionId": cid})
        out["graded"] = (tick or {}).get("score", {}).get("scored")
        out["stock"] = (tick or {}).get("stock")
    except Exception as exc:                  # noqa: BLE001
        out["tickError"] = str(exc)

    complete = bool(ledger.mark_complete(day, stages)) if ledger else True
    return {**out, "ok": True, "complete": complete}


def _data_is_stale() -> bool:
    """Is the newest sale on file older than yesterday?

    This is the question the scheduler should be asking, and it is deliberately derived
    from the DATA rather than from a remembered run time. A timestamp can be wrong in both
    directions — it claims "synced today" after a run that fetched nothing, and "never
    synced" after every restart — whereas the age of the newest sale is exactly what
    anyone means by "is the tool current", and it fixes itself.

    No store connected, or the store paused (spreadsheets loaded) → not stale: there is
    nothing to sync from, and syncing on boot would surprise someone who just wanted to
    upload a spreadsheet.

    A store connected but NOTHING loaded → stale. That is a fresh copy of the project, or
    one whose saved catalog was lost: the connection is on, and the app sat empty until
    the nightly slot, or until someone switched the connection off and on to make it load.
    Not while a saved catalog is still being restored from disk, which fills it shortly.
    """
    if not _CONN.list_active() or _showing_store_id() is None:
        return False
    with _state_lock:
        if not _catalog:
            return _restore_state.get("status") != "restoring"
        newest = None
        for e in _catalog.values():
            df = e.get("df")
            if df is None or not len(df):
                continue
            try:
                d = pd.Timestamp(df["ds"].max()).normalize()
            except Exception:                            # noqa: BLE001
                continue
            if newest is None or d > newest:
                newest = d
    if newest is None:
        return False
    return data_is_stale_by(newest, today())


def data_is_stale_by(newest, now) -> bool:
    """Has a whole nightly sync been missed?

    The training data ends at the last COMPLETE day. So a healthy catalogue is one day old
    after the 00:15 run and TWO days old between midnight and 00:15, when yesterday has
    finished but its sync has not happened yet. This used to say "stale" at anything over
    one day — written when the data still ran to the day of the sync — which meant every
    night, the moment midnight passed, the stale path pulled the sync forward to 00:00:30
    and skipped the fifteen-minute buffer that exists so late sales land first.

    Two days is expected; more than two means a sync genuinely did not happen, and then
    running immediately rather than waiting for tonight is exactly right.
    """
    return (pd.Timestamp(now).normalize() - pd.Timestamp(newest).normalize()).days > 2


def _store_wall_clock():
    """The store's local time, naive, for the nightly sync: "00:15" means 00:15 where the
    store is, not wherever the server happens to run (a UTC server fired it at 8:15pm
    Eastern, before the day being synced was over)."""
    return _CLOCK.now().replace(tzinfo=None)


_sync = _SCHED.DailySync(_run_daily_sync, is_stale=_data_is_stale,
                         stages=("fetch", "refit", "backtest"), clock=_store_wall_clock)
_sync.zone_name = lambda: _CLOCK.zone() or "UTC"   # shown as the sync's time zone


# A price change seen by the hourly reading refits that product straight away, up to this
# many per tick. Anything beyond waits for the next tick or the nightly sync.
LIVE_PRICE_REFITS_PER_TICK = 10


def _sample_stock_now() -> dict:
    """One inventory reading from the saved connection, filed in the stock log.

    This is the ONLY thing in the app that observes a moment rather than computing a
    state, and it is the input the censoring correction runs on: `hours_in_stock` per
    product per day is reconstructed from these readings, and that is what separates
    "sold 3 because demand was 3" from "sold 3 because it ran out before lunch".

    Taken hourly. At the one-a-day cadence this replaced, the reconstruction had only two
    possible answers — in stock all day, or out all day — so the partial-day branch of
    censoring.py could never be reached and the correction, though fully built and
    tested, had never once fired on real data.

    Returns rather than raises on every failure path: an unattended sampler that throws
    takes its thread down, and a dead sampler loses hours that cannot be recovered.
    """
    saved = _CONN.list_all()
    if not saved:
        # skip, not failure: nothing is broken, there is simply nothing to read from.
        return {"ok": False, "skip": True, "reason": "no-saved-connection"}
    # _CONN.get is the server-side view WITH secrets; list_all() is the masked one the
    # browser gets, so the token is read here and nowhere near a response body.
    full = _CONN.get(saved[0]["id"]) or {}
    creds = full.get("creds") or {}
    source = full.get("source")
    if not creds or not source:
        return {"ok": False, "skip": True, "reason": "credentials-unavailable"}
    try:
        res = _SRC.sample_stock(source, _slog, creds) or {}
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "reason": "sample-failed", "detail": f"{type(exc).__name__}: {exc}"}

    # SECOND moment-reading, same tick: how much has sold so far today.
    #
    # This is the fetch I argued against when it was proposed as "hourly orders", and the
    # objection was right for the shape it had then — anything fetched hourly was being
    # ingested, and a partial day in the training series teaches the models that demand
    # collapses every morning. The objection was to the ingestion, not to the fetch. Now
    # that the training window provably ends at the last COMPLETE day, today's figure has
    # nowhere to leak into, and two things genuinely need it: showing the day, and keeping
    # a counted stock level current between counts.
    #
    # Failure here must not fail the stock reading, which is the unrecoverable half.
    try:
        t = _SRC.fetch_today(source, creds) or {}
        if t.get("day") and not t.get("unsupported"):
            _today_sales.clear_if_closed(t["day"])
            _ln = _local_now()
            rec = _today_sales.record(t["day"], t.get("units") or {}, hour=_ln.hour + _ln.minute / 60.0)
            res["todaySales"] = {"day": rec["day"], "units": rec["units"], "skus": rec["skus"]}
        elif t.get("unsupported"):
            res["todaySales"] = {"unsupported": True}
    except Exception as exc:                              # noqa: BLE001
        res["todaySalesError"] = f"{type(exc).__name__}: {exc}"
    # THIRD, from the same catalogue call: the price on the shelf right now. A discount
    # switched on at 10am should not wait for tonight's sync to show up, so a product
    # whose price facts changed is refitted now — a handful at most per tick, since each
    # refit is a full model fit and this runs unattended.
    try:
        changed = _live.record(res.pop("prices", None), source)
        if changed:
            res["pricesChanged"] = len(changed)
            # Only while this store's products are the ones loaded: with it paused the
            # catalog is your spreadsheets, and a matching product code there is not this
            # store's product.
            showing_this = _showing_store_id() == full.get("id")
            todo = ([sid for sid in changed if sid in _catalog][:LIVE_PRICE_REFITS_PER_TICK]
                    if showing_this else [])
            for sid in todo:
                with _state_lock:
                    _rebuild(today(), only=sid)
            if todo:
                with _state_lock:
                    _persist_catalog()
            res["pricesRefitted"] = len(todo)
            if showing_this:
                try:
                    res["weeksRevised"] = _livelog_reconcile(
                        [sid for sid in changed if sid in _catalog], trigger="shelf")["revised"]
                except Exception as exc:                  # noqa: BLE001
                    res["weeksReviseError"] = str(exc)
    except Exception as exc:                              # noqa: BLE001
        res["livePriceError"] = f"{type(exc).__name__}: {exc}"
    # sample_from_shopify swallows store errors into its return value rather than raising,
    # so an empty reading is a failure even though nothing was thrown.
    if not res.get("added") and not res.get("collapsed"):
        return {"ok": False, "reason": "no-levels-returned", **res}
    return {"ok": True, "source": source, "label": full.get("label"), **res}


def _last_stock_sample_at():
    """When a reading was last filed — looked up LAZILY, through the module global.

    Binding `_slog.last_sample_at` directly would pin the sampler to whichever log object
    existed at import time, so anything that swaps `_slog` (the reset path, and the fake
    logs the endpoint tests install) would leave the sampler still interrogating the old
    one and deciding "not due" against a record nobody is writing to any more. Tolerant of
    a log that doesn't implement it: the sampler treats None as "nothing on file".
    """
    fn = getattr(_slog, "last_sample_at", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:                                     # noqa: BLE001
        return None


_sampler = _SCHED.IntervalSampler(_sample_stock_now, last_sample_at=_last_stock_sample_at)


@app.get("/api/sync")
def sync_status():
    """When the nightly sync last ran, when it runs next, and what it would sync from."""
    st = _sync.status()
    saved = _CONN.list_all()
    active = [c for c in saved if c.get("active")]
    st["connection"] = active[0] if active else None
    # Saved but paused: the page says so instead of "nothing to sync from".
    st["paused"] = bool(saved) and not active
    st["showing"] = "store" if _showing_store_id() else "sheets"
    st["catalogSkus"] = len(_catalog)
    st["syncStatePath"] = _sync.state_path
    # The hourly reading is a separate clock with separate rules — it captures a moment,
    # so it never catches up — and it is reported separately rather than folded in.
    st["sampler"] = {**_sampler.status(), "coverage": _slog.coverage()}
    # The backtest's own schedule, and why it would (or wouldn't) run next.
    st["backtest"] = {
        "weekday": SYNC_BACKTEST_WEEKDAY,
        "weekdayName": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                        "Saturday", "Sunday"][SYNC_BACKTEST_WEEKDAY % 7],
        "dueToday": today().weekday() == SYNC_BACKTEST_WEEKDAY,
        "triggers": _backtest_needed(),
        "measuredSkus": len({k.split("|")[0] for k in _backtest_tier_cache}),
        "lastRanAt": (_last_backtest or {}).get("ranAt"),
        "combosInUse": _combos_in_use(),
    }
    return st


@app.post("/api/sync/now")
def sync_now():
    """Run the sync immediately.

    This is also the entry point for a real cron in production — `curl -X POST
    .../api/sync/now` from crontab gives you a sync that survives the process being
    restarted, which an in-process thread cannot.
    """
    return _sync.run_now(trigger="manual")


@app.post("/api/sync/settings")
def sync_settings(payload: dict = Body(default={})):
    """Body: { enabled?, at? } — `at` is 'HH:MM' in the STORE's time zone (store_clock)."""
    return _sync.configure(at=payload.get("at"), enabled=payload.get("enabled"))


@app.get("/api/availability")
def availability_now():
    """Live stock state per product, the health of both clocks, and what the censored-
    demand correction has actually done.

    One endpoint rather than three, because all three answer the same question — "is this
    thing working while I'm not watching" — and the dashboard shows them together.

    THE UPLIFT BLOCK IS THE ACCEPTANCE TEST. censoring.py has been in the codebase, fully
    tested, unable to fire: it needs to know a product ran out PARTWAY THROUGH a day, and
    at one stock reading a day that judgement has only two possible answers. Hourly
    sampling is what makes the partial case reachable. `productDays` is therefore the
    number that says whether any of it mattered. Zero after a week means either the store
    genuinely never sells out mid-day — worth knowing — or sampling is not running, which
    the sampler block above it will show.
    """
    snap = _cache()
    now = _SL.utcnow()
    skus, out_now, went_out, unknown = {}, 0, 0, 0
    for sid, e in snap.items():
        st = _slog.state_today(sid, now=now)
        st["name"] = e.get("sku_name") or sid
        skus[sid] = st
        if st["state"] == "unknown":
            unknown += 1
        elif st["state"] == "out":
            out_now += 1
        if st["wentOutToday"]:
            went_out += 1

    # What the correction did, summed over the catalogue. Each entry carries its own
    # summary from apply_to_frame; nothing is recomputed here.
    up = {"productDays": 0, "skus": 0, "unitsAdded": 0.0, "cappedDays": 0, "excludedDays": 0}
    for e in snap.values():
        c = e.get("censoring") or {}
        partial = int(c.get("partial") or 0)
        up["productDays"] += partial
        up["cappedDays"] += int(c.get("capped") or 0)
        up["excludedDays"] += int(c.get("unavailable") or 0)
        up["unitsAdded"] += float(c.get("addedUnits") or 0.0)
        if partial:
            up["skus"] += 1
    up["unitsAdded"] = round(up["unitsAdded"], 1)

    return {
        "at": now.isoformat(),
        "skus": skus,
        "counts": {"total": len(snap), "outNow": out_now,
                   "wentOutToday": went_out, "unsampled": unknown},
        "sampler": {**_sampler.status(), "coverage": _slog.coverage()},
        "sync": {"lastRunDate": _sync.last_run_date.isoformat() if _sync.last_run_date else None,
                 "todayComplete": _sync.ledger.is_complete(today().date()),
                 "pendingStages": _sync.ledger.pending_stages(today().date(), _sync.stages),
                 "nextRunAt": _sync.next_run_at().isoformat() if _sync.enabled else None,
                 "lastRunOk": (_sync.last_run or {}).get("ok")},
        "uplift": up,
    }


@app.post("/api/sync/sample-now")
def sample_now():
    """Take one inventory reading immediately, outside the hourly cadence.

    Also the cron entry point: `curl -X POST .../api/sync/sample-now` hourly from crontab
    gives sampling that survives a process restart, which an in-process thread cannot.
    """
    return _sampler.sample_now(trigger="manual")


@app.post("/api/sync/sample-settings")
def sample_settings(payload: dict = Body(default={})):
    """Body: { enabled?, intervalSeconds? }. Below an hour buys nothing — StockLog
    collapses identical consecutive readings — and costs someone else's rate limit."""
    return _sampler.configure(enabled=payload.get("enabled"),
                              interval_seconds=payload.get("intervalSeconds"))


@app.post("/api/upload/shopify")
def upload_shopify(payload: dict = Body(default={})):
    """Back-compat alias for the original Shopify-only endpoint.

    Kept so a stale frontend bundle — or the curl script in the ROADMAP — keeps working
    unchanged. New callers should use /api/upload/source.
    """
    body = dict(payload or {})
    body.setdefault("source", "shopify")
    if "creds" not in body:
        body["creds"] = {"shop": body.get("shop"), "token": body.get("token"),
                         "apiVersion": body.get("apiVersion")}
    return upload_from_source(body)


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
            try:
                _live.purge(); purged["livePrices"] = True
                _holidays.purge(); purged["holidays"] = True
            except Exception as e:                           # noqa: BLE001
                purged["livePrices"] = f"failed: {e}"
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
    # Catalog-wide baseline shape = the average seasonal fingerprint across all products.
    # Subtracting it is what lets similarity mean DISTINCTIVE co-movement rather than
    # "both follow the store's rhythm". It is built BEFORE clustering now, because the
    # clustering itself is measured on it — it used to be computed afterwards, purely to
    # decorate the display.
    _baseline = R.catalog_baseline(pool_catalog)
    clusters = R.cluster_catalog(groups, pool_catalog, baseline=_baseline,
                                 family=R.family_map(groups, group_meta))
    member_key = {}; cohesion_by_key = {}
    for g, cls in clusters.items():
        for cl in cls:
            key = tuple(sorted(cl))
            cohesion_by_key[key] = R.cluster_cohesion(cl, pool_catalog, _baseline)
            for m in cl:
                # A category's clusters now include its subgrouped products as donors;
                # each product is still shown in the cluster of its OWN group.
                if groups.get(m) == g:
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
                score = R.distinct_corr(sig, osig, _baseline)
                if best is None or score > best["score"]:
                    best = {
                        "score": round(float(score), 3),
                        "threshold": R.DISTINCT_THRESHOLD,
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
            score = R.distinct_corr(sig, osig, _baseline)
            if best is None or score > best["score"]:
                best = {
                    "score": round(float(score), 3),
                    "threshold": R.DISTINCT_THRESHOLD,
                    "skuId": other,
                    "skuName": pool_catalog[other].get("sku_name") or other,
                }
        return best

    def _days_hist(sid):
        e = _cache().get(sid) or {}
        dft = e.get("df_train")
        try:
            return int((dft["ds"].max() - dft["ds"].min()).days) if dft is not None and len(dft) > 1 else 0
        except Exception:
            return None
    rows = []
    for sid, c in pool_catalog.items():
        entry = _cache().get(sid, {})
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
            # What it actually borrows, when that isn't visible from where it's shown:
            # a product alone on the card that still takes a season or a pooled forecast
            # from a family it was matched to (see below).
            "borrowsFrom": _borrows_from(sid, entry, info, ckey, seas),
            "familyNote": ((info or {}).get("basis") if not (info or {}).get("chosen") else None),
        })
    # A product at category level (no subgroup of its own) is compared across its whole
    # category, so its cluster can be made of another subgroup's products: Crew Socks
    # moving with the two tees. Shown on that subgroup's card, marked as category level,
    # rather than alone under "different behaviour" as if it matched nothing.
    own_key = {r["skuId"]: tuple(sorted(r["cluster"])) if r.get("cluster") else None for r in rows}
    for r in rows:
        ck = own_key.get(r["skuId"])
        meta = r.get("groupInfo") or {}
        if not ck or len(ck) < 2 or meta.get("level") != "base" or r.get("clusterBasis"):
            continue
        mates = [m for m in ck if m != r["skuId"]]
        if not mates or any(groups.get(m) == groups.get(r["skuId"]) for m in mates):
            continue
        keys = {own_key.get(m) for m in mates}
        if len(keys) == 1 and None not in keys:
            r["cluster"] = list(next(iter(keys)))
            r["categoryLevel"] = True
            r["categoryLevelWith"] = [pool_catalog[m].get("sku_name") or m for m in mates if m in pool_catalog]
    return _to_jsonable({"groupColumns": cols, "skus": rows})


def _borrows_from(sid, entry, info, ckey, seas):
    """[{skuId, skuName}] + what, when this product takes something from a family it
    isn't shown with: a pooled forecast (global route), a yearly shape blended into its
    forecast, or the season a slow seller is scaled by. None otherwise."""
    chosen = list((info or {}).get("chosen") or [])
    if not chosen or (ckey and set(chosen) <= set(ckey)):
        return None
    route = entry.get("route")
    sea = entry.get("seasonality_applied") or {}
    what = ("its forecast" if route == "global"
            else "its yearly shape" if (seas or {}).get("blended")
            else "its season" if route in ("croston", "abstain") and sea.get("source") == "cohort"
            else None)
    if not what:
        return None
    return {"what": what, "skus": [{"skuId": m, "skuName": (_catalog.get(m) or {}).get("sku_name") or m}
                                   for m in chosen]}


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
    Body: { "skus": { "<sku_id>": {"category": "...", "brand": "...", ...}, ... },
            "source": "user" | "ai" }

    `source: "ai"` (the Grouping tab's classifier) only FILLS IN: it sets attributes that
    are empty or that the AI set before, never one the file, the store or a person gave,
    and what it sets is not recorded as set by hand. It used to save AI guesses exactly
    like a person's edits, so opening the Grouping tab replaced the file's own Category
    and every later import then kept the guess over the file."""
    skus = payload.get("skus", {}) or {}
    source = "ai" if str(payload.get("source") or "").lower() == "ai" else "user"
    updated = 0
    with _state_lock:
        return _set_attributes_locked(skus, updated, source=source)


def _set_attributes_locked(skus, updated, source: str = "user"):
    for sid, attrs in skus.items():
        if sid in _catalog:
            merged = dict(_catalog[sid].get("attrs") or {})
            hand = set(_catalog[sid].get("attrs_set") or [])
            ai = set(_catalog[sid].get("attrs_ai") or [])
            sku_label = f"{sid} {_catalog[sid].get('sku_name') or ''}".lower()
            force_no_category = any(marker in sku_label for marker in NO_CATEGORY_MARKERS)
            changed = False
            for k, v in (attrs or {}).items():
                k = str(k)
                if force_no_category and k.lower() == "category":
                    v = None
                if source == "ai":
                    # Fill in only: an empty attribute, or one the AI itself set earlier.
                    owned_by_ai = k in ai or merged.get(k) in (None, "")
                    if k in hand or not owned_by_ai:
                        continue
                    if v in (None, ""):
                        if k in merged and k in ai:
                            merged.pop(k, None); ai.discard(k); changed = True
                        continue
                    if merged.get(k) != v:
                        merged[k] = v; changed = True
                    ai.add(k)
                    continue
                if v in (None, ""):
                    if k in merged:
                        merged.pop(k, None)
                        changed = True
                elif merged.get(k) != v:
                    merged[k] = v
                    changed = True
                hand.add(k); ai.discard(k)
            _catalog[sid]["attrs"] = merged
            _catalog[sid]["attrs_set"] = sorted(hand)
            _catalog[sid]["attrs_ai"] = sorted(ai)
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
    return "RED", (f"The forecast window ({fd} days) approaches or exceeds your history ({hd} days). "
                   f"These estimates are highly speculative.")


def get_demand_volatility(d):
    """Same scale as the scorecard's steadiness bar, so the noise badge here and the
    Sales consistency row there can never disagree about a product."""
    if d is None: return "YELLOW", "balanced", "not enough sales to judge steadiness", 1.645
    if d <= DISPERSION_STEADY:
        return "GREEN", "lean", f"steady for its sales volume ({d:.1f}x the floor)", 1.282
    if d < DISPERSION_VARIABLE:
        return "YELLOW", "balanced", f"moderately variable ({d:.1f}x the floor)", 1.645
    return "ORANGE", "conservative", f"genuinely erratic ({d:.1f}x the floor)", 2.326


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
            what = "There's a unit cost on file but no selling price in the data, so margin can't be worked out"
            fix = "Add a Price column to the sales file (or pull prices from Shopify)"
        elif has_price and not has_cost:
            what = "No unit cost on file, so protection can't be tuned to this item's economics yet"
            fix = "Add a unit cost on the product page (or in the Fleet tab's Costs & fees sheet)"
        elif has_price is False and has_cost is False:
            what = "Neither a selling price nor a unit cost is available, so margin can't be worked out"
            fix = "Add a Price column to the file and a unit cost on the product page"
        else:
            what = "Margin isn't known for this item yet"
            fix = "Add a unit cost on the product page (and a Price column in the data)"
        return "standard", f"{what}. Using Standard (95%) for now. {fix} to tailor it."
    if   margin_pct < 15: key = "light"
    elif margin_pct < 30: key = "standard"
    elif margin_pct < 50: key = "high"
    else:                 key = "max"
    t = _TIER_BY_KEY[key]
    feel = "very costly" if margin_pct >= 50 else "costly" if margin_pct >= 30 else \
           "moderately costly" if margin_pct >= 15 else "cheap"
    return key, (f"This item keeps about {margin_pct:.0f}% margin, so a lost sale is {feel} to forgo. "
                 f"{t['label']} protection ({t['pct']}%) is recommended to balance that stockout cost "
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
        # Tolerant, not exact — see TIER_LEAD_TOLERANCE. A P80 lead time that moved by a
        # day when a shipment landed must not demote a measured tier to an estimate.
        lead_match = _within(bt_lead, lead_time_days, TIER_LEAD_TOLERANCE)
        cov_match = _within(bt_cov, coverage_days, TIER_COVERAGE_TOLERANCE)
        near = bool(lead_match and cov_match
                    and not (bt_lead is None or lead_time_days is None
                             or int(bt_lead) == int(lead_time_days)))
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
                              f"Per-product tuning didn't hold up on unseen weeks (the differences were "
                              f"mostly noise), so every product runs {tier['label']} protection "
                              f"({tier['pct']}%), the catalog-wide winner across "
                              f"{int(backtest_rec.get('windows') or 0)} historical tests.")
                else:
                    reason = (f"Recommended from the last backtest: {tier['label']} protection "
                              f"({tier['pct']}%) was the cheapest tier for this SKU, {cost_txt}, "
                              f"across {int(backtest_rec.get('windows') or 0)} historical tests. "
                              f"Per-product tuning beat one level for everything on unseen weeks.")
                if near:
                    reason += (f" Measured at a {int(bt_lead)}-day lead time rather than "
                               f"today's {int(lead_time_days)}. That's close enough that the cheapest "
                               f"tier is unchanged, and the buffer is still sized from today's lead.")
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
        return fallback_key, (fallback_reason + " Cost-based tuning switches on once this "
                              f"SKU has at least {ECONOMIC_TIER_MIN_DAYS} days and "
                              f"{ECONOMIC_TIER_MIN_SALES} sales of history."), fallback_source, None
    if demand_class in ECONOMIC_TIER_BLOCKED_DEMAND:
        return fallback_key, (fallback_reason + " Cost-based tuning is held back because this "
                              "SKU's demand is intermittent, where the margin rule is more stable."), fallback_source, None
    spread = float(demand_spread or 0.0)
    if spread <= 0 or not np.isfinite(spread):
        return fallback_key, (fallback_reason + " Cost-based tuning is held back because "
                              "lead-time demand uncertainty isn't measurable yet."), fallback_source, None

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
              f"${best['totalCost']:,.0f}/yr (lost profit from stockouts plus buffer holding "
              f"cost). Assumes {round(holding_annual * 100)}%/yr holding cost.")
    return best["key"], reason, "economics", {
        "basis": "per-SKU expected cost",
        "holdingPct": round(holding_annual * 100),
        "marginUnit": round(margin_unit, 2),
        "cyclesPerYear": round(cycles_per_year, 1),
        "leadTimeSpread": round(spread, 2),
        "tiers": rows,
        "best": best["key"],
    }


# ── Grading ──────────────────────────────────────────────────────────────────
#
# TWO AXES, NOT ONE LADDER.
#
# The old rule mixed them and produced CONFLICTING, which covered three unrelated
# situations — intermittent demand, long history with thin sales, and strong sales with
# short history — and so told a reader nothing without the prose beside it. It was also
# 37% of the input space, measured across the characterization grid: not a corner case.
#
#   EVIDENCE       how much we know          thin  → partial → established
#   PREDICTABILITY how knowable it is at all steady/variable/occasional/dormant
#
# Predictability is not a history problem: more data never makes lumpy demand smooth. It
# therefore CAPS the grade rather than sitting on the same scale, which is what the old
# rule was really doing when it forced intermittent products into CONFLICTING.
#
# EVIDENCE IS MEASURED IN DECISION HORIZONS, NOT DAYS.
#
# The question this tool answers is "how much do I order to cover the next lead time plus
# coverage window" — 44 days at the defaults, which is what the backtest scores. Grading
# that against a fixed 180 days asks a product with a 7-day lead time to clear the same
# bar as one with a 90-day lead time, which is not the same question. Expressed as
# multiples of the horizon the thresholds land within a rounding of the old constants at
# the default horizon (4 x 44 = 176 vs 180; 2 x 44 = 88 vs 90) and scale correctly away
# from it. Floors stop a very short horizon from grading anything on a fortnight of data.
READY_HORIZONS = 4            # horizons of history before a forecast is "measured"
USABLE_HORIZONS = 2           # ...before it is worth planning from at all
READY_DAYS_FLOOR = 60
USABLE_DAYS_FLOOR = 30
DEFAULT_DECISION_HORIZON = 44  # lead 14 + coverage 30; the backtest's own horizon

STATUS_READY = "FORECAST_ELIGIBLE"
STATUS_USABLE = "LOW_CONFIDENCE"
STATUS_NOT_READY = "INSUFFICIENT"

_PREDICTABILITY = {"smooth": "steady", "erratic": "variable",
                   "intermittent": "occasional", "lumpy": "occasional",
                   "no_demand": "dormant"}


def evidence_thresholds(horizon_days=None):
    """(ready_days, usable_days) for a given decision horizon."""
    h = int(horizon_days or DEFAULT_DECISION_HORIZON)
    if h <= 0:
        h = DEFAULT_DECISION_HORIZON
    return (max(READY_HORIZONS * h, READY_DAYS_FLOOR),
            max(USABLE_HORIZONS * h, USABLE_DAYS_FLOOR))


def grade_forecast(days, sales, demand_class=None, selling_days=None, horizon_days=None) -> dict:
    """The full grade: an ordinal status, a machine-readable reason, and the two axes.

    `status` is ordinal and comparable — three levels, sortable, meaningful on its own.
    `reason` is why, as a stable code the UI can render however it likes. Keeping those
    separate is the whole point: a grade that needs a paragraph to interpret is not a grade.
    """
    days = int(days or 0)
    sales = int(sales or 0)
    selling_days = int(selling_days or 0)
    horizon = int(horizon_days or DEFAULT_DECISION_HORIZON)
    ready_days, usable_days = evidence_thresholds(horizon)

    predictability = _PREDICTABILITY.get(demand_class, "unknown")

    # ── Axis 1: evidence ──
    has_ready = (days >= ready_days and selling_days >= R.ESTABLISHED_SELLING_DAYS
                 and sales >= R.ESTABLISHED_SALES)
    has_usable = (days >= usable_days and selling_days >= R.MIN_HISTORY_SELLING_DAYS
                  and sales >= R.MIN_HISTORY_SALES)

    if has_ready:
        status, reason = STATUS_READY, "established"
    elif has_usable:
        status, reason = STATUS_USABLE, "partial_history"
    else:
        status = STATUS_NOT_READY
        if days < usable_days and sales < R.MIN_HISTORY_SALES and selling_days < R.MIN_HISTORY_SELLING_DAYS:
            reason = "new_product"
        elif days < usable_days:
            reason = "short_history"
        elif selling_days < R.MIN_HISTORY_SELLING_DAYS:
            reason = "few_selling_days"
        else:
            reason = "low_volume"

    # ── Axis 2: predictability caps the grade ──
    # A product selling on 8 days in 90 stays uncertain per period however long you watch
    # it, so it never reaches the top grade — but it is still worth planning from, which
    # is exactly what the intermittent route exists to do. The old rule sent these to
    # CONFLICTING, a label that reads as a fault rather than as a property of the demand.
    if predictability in ("occasional", "dormant") and status in (STATUS_READY, STATUS_USABLE):
        status = STATUS_USABLE
        reason = "dormant" if predictability == "dormant" else "intermittent_demand"

    return {"status": status, "reason": reason, "predictability": predictability,
            "days": days, "sellingDays": selling_days, "units": sales,
            "decisionHorizonDays": horizon,
            "readyAtDays": ready_days, "usableAtDays": usable_days,
            "message": _grade_message(status, reason, days, selling_days, sales,
                                      horizon, ready_days, usable_days)}


def _grade_message(status, reason, days, selling_days, sales, horizon, ready_days, usable_days):
    """Prose for one (status, reason). Every branch names the horizon it is grading for,
    because "not enough history" is meaningless without saying enough for what."""
    if reason == "established":
        return (f"Measured forecast for the next {horizon} days "
                f"({days} days of history, {selling_days} selling days, {sales:,} units).")
    if reason == "intermittent_demand":
        return ("Sells in occasional bursts, so the intermittent-demand model handles it and a "
                "per-period figure stays uncertain however long the history. Good for deciding "
                f"cover over {horizon} days, not for a precise weekly number.")
    if reason == "dormant":
        if not sales:
            return "Hasn't sold yet. Its forecast starts from its own sales once it does."
        return "No recent sales. Nothing to plan from until it starts selling again."
    if reason == "partial_history":
        return (f"Enough to plan the next {horizon} days, but treat it as directional: "
                f"{days} days of history against {ready_days} for a measured forecast.")
    if reason == "short_history":
        return (f"{days} days of history; {usable_days} needed to plan {horizon} days ahead. "
                f"About {max(usable_days - days, 0)} more days of selling.")
    if reason == "few_selling_days":
        return (f"Long enough history ({days} days) but only {selling_days} days with a sale. "
                f"It needs {R.MIN_HISTORY_SELLING_DAYS} to tell a pattern from a few one-off sales "
                f"before planning {horizon} days ahead.")
    if reason == "low_volume":
        return (f"Only {sales:,} units across {days} days, below the {R.MIN_HISTORY_SALES} needed "
                f"before the numbers mean much over a {horizon}-day window.")
    return (f"Not enough data yet ({days} days, {sales:,} units). Needs about {usable_days} days "
            f"and {R.MIN_HISTORY_SALES} units to plan {horizon} days ahead.")


def get_eligibility_status(days, sales, demand_class=None, selling_days=None, horizon_days=None):
    """(status, message) — the two-value form the forecast payload has always returned.

    Kept as the narrow interface so callers that only want a label and a sentence do not
    have to care about the rest; `grade_forecast` is the full picture.
    """
    g = grade_forecast(days, sales, demand_class, selling_days, horizon_days)
    return g["status"], g["message"]


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
            return "warning", f"${npx:.2f} is outside observed prices (${tmin:.2f} to ${tmax:.2f})."
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
# stock defaults to 0, NOT 500. An unknown stock level is not "probably 500" — that
# default read as hundreds of days of cover on a slow mover and reported the whole
# catalogue as overstocked. Unknown is 0-with-a-flag everywhere else; this was the
# last place still disagreeing.
def get_forecast(sku_id: str = Query(...), stock: int = Query(default=ASSUMED_STOCK, ge=0),
                 lead_time_days: int = Query(default=14, ge=1), coverage_days: int = Query(default=30, ge=1),
                 strategy: str = Query(default="balanced"), forecast_months: int = Query(default=1, ge=1, le=12),
                 units_on_order: int = Query(default=0, ge=0),
                 on_order_eta_days: int | None = Query(default=None, ge=0),
                 unit_cost: float | None = Query(default=None), fees: float = Query(default=0.0, ge=0),
                 protection: str | None = Query(default=None), tz: str | None = Query(default=None),
                 stock_source: str | None = Query(default=None),
                 stock_counted_at: str | None = Query(default=None)):
    set_timezone(tz)   # keep "today" aligned to the user's local date
    # One snapshot for the whole request. Reading _sku_cache twice here meant the
    # membership test and the lookup could hit two different catalogues, so a delete
    # landing between them raised KeyError instead of returning the 404 just above.
    _snap = _cache()
    if sku_id not in _snap:
        raise HTTPException(404, f"SKU '{sku_id}' not found.")
    e = _snap[sku_id]
    df_train, forecast, future_fc = e["df_train"], e["forecast"], e["future_fc"]
    events = e.get("events", [])
    if df_train is None or forecast is None or future_fc is None:
        raise HTTPException(503, "Model is not ready yet.")
    # WHERE THE STOCK NUMBER CAME FROM.
    #
    # This used to be `"units_in_stock" in df_train.columns` — a test that can only ever
    # be true for a spreadsheet upload. A live source carries inventory ALONGSIDE the
    # sales, never inside them, so every Square-connected product reported "assumed" even
    # when Square had handed us a real count. Provenance is not a property of the training
    # frame; it belongs to the figure being used, so it is resolved here in that order:
    #
    #   1. what the caller says it is  (the browser knows: typed, live, or from a sheet)
    #   2. a units_in_stock column     (a spreadsheet that carried one)
    #   3. a StockLog reading          (the hourly sampler saw this product on a shelf)
    #   4. unknown                     (nobody has ever counted it)
    # Same rule as the scorecard: an explicit "unknown" from the caller is the absence of
    # an answer, not a negative one, so the evidence below still gets its turn.
    _src = (stock_source or "").strip().lower() or None
    if _src in (None, "unknown", "none", "null"):
        if "units_in_stock" in df_train.columns:
            _src = "sheet"
        elif _live_readings_apply(sku_id) and _slog.latest(str(sku_id)) is not None:
            _src = "live"
        else:
            _src = "unknown"
    stock_counted = _src in ("manual", "live", "sheet")
    # Kept under its old name for any client that still reads it; it now means what its
    # name always claimed rather than "the upload had a column".
    stock_data_available = stock_counted

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
        f"looks discontinued or out of stock long-term, so its forecast is set to 0; the old sales pattern "
        f"is too stale to trust. If it's still active, upload recent sales and stock and the forecast "
        f"will pick back up."
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
    # The decision this forecast serves is "cover the next lead time plus coverage window",
    # so that is what the grade is graded against — not an abstract notion of forecast
    # quality. A 7-day lead time and a 90-day one are not the same question and should not
    # clear the same bar.
    _note_settings(sku_id, lead_time_days, coverage_days)
    decision_horizon = int(lead_time_days or 14) + int(coverage_days or 30)
    grade = grade_forecast(days_hist, total_sales, e.get("demand_class"),
                           selling_days, decision_horizon)
    status, status_msg = grade["status"], grade["message"]

    # ── Cap the projection at what the history supports ──────────────────────
    # forecast_months accepted 1-12 with no relation to how much data existed, and exports
    # always asked for 3. Projecting 93 days from 133 days of history is extrapolating most
    # of the sample; from 40 days it is mostly invention. The rule: you may look forward as
    # far as you have looked back, but never less than one decision horizon — otherwise a
    # young product could not be planned at all, which is the opposite of the point.
    requested_fc_days = forecast_months * 31
    tot_fc_days = max(decision_horizon, min(requested_fc_days, max(days_hist, 1)))
    horizon_capped = tot_fc_days < requested_fc_days
    if horizon_capped:
        forecast_months = max(1, math.ceil(tot_fc_days / 31))
        tot_fc_days = min(tot_fc_days, forecast_months * 31)
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
        f"Establishing baseline: {own_days} of {BASELINE_DAYS} days of sales. A forecast needs about a "
        f"week of this product's own history, so it's on hold rather than guessing. Inventory is still "
        f"tracked below, and the forecast turns on automatically once a week of data is in."
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
        f"Young product: {own_days} of {YOUNG_DAYS} days of history. The forecast is live but provisional, "
        f"built on only a few weeks of sales. Reorder timing is a heads-up, not a hard deadline, and the "
        f"recommended order stays conservative until more history builds."
    ) if young else None
    # A young product's band used to be stretched 1.4x here. Removed deliberately: every
    # band on every product is now the model's own FORECAST_BAND_PCT interval, unadjusted,
    # so "80%" means the same thing everywhere. The 1.4 was a round number nobody had
    # measured — the reasoning behind it was sound (a model fitted on three weeks is
    # over-confident) but the size of the correction was a guess, and a guessed correction
    # dressed as an 80% band is worse than an honest un-corrected one. If the Backtest
    # tab's interval coverage shows young products landing outside their band far more
    # than 20% of the time, put a widening back and set it FROM that measurement.
    # This never touched order quantities: safety stock comes from measured lead-window
    # error and the protection tier, not from these bounds. `young` still drives the
    # provisional labelling and the order guardrail.

    # ── Anchor the order math to TODAY, not to the last row of data ───────────────
    # `future_fc` begins the day after the newest sale on file, which is only today if
    # the data is fresh. Nothing re-imports on a schedule, so a catalog can easily be a
    # week old — and every calculation below was indexing this frame as though position
    # 0 were today. Two things went wrong at once. The stockout cumulative charged days
    # that have ALREADY HAPPENED against the stock count the user entered TODAY, so it
    # burned the shelf down twice and pulled the reorder date forward. And `d_ro` is
    # measured from today (`(rod - today()).days`) yet was used as an index into this
    # frame, so the coverage window was sampled by however many days the data was stale
    # — the wrong slice of the season, drifting further off the longer between imports.
    #
    # `fc_fwd` starts at today, so an index into it IS a day offset from today, which is
    # what every caller below already assumed. The full `future_fc` stays as it is for
    # the chart series: the forecast line should still meet the end of the history
    # rather than opening a gap the user has to interpret.
    fc_fwd = future_fc[future_fc["ds"] >= today()].copy().reset_index(drop=True)
    if fc_fwd.empty:
        # Data older than the whole forecast horizon. Nothing forward-looking survives,
        # so fall back to the raw frame rather than dividing by an empty window; the
        # dormancy guard above has usually already flagged a product this stale.
        fc_fwd = future_fc

    # Stock on the way counts FROM THE DAY IT LANDS, not from today. Adding it to today's
    # shelf treated a shipment 90 days out as if it were already in the warehouse, which
    # pushed the projected stockout far into the future and suppressed a reorder the user
    # genuinely needed. `on_order_eta_days` is how many days until it arrives; with no
    # date the old behaviour is kept (available immediately) so nothing silently changes
    # for callers that don't supply one.
    fc_fwd["cum_mid"] = fc_fwd["yhat"].clip(lower=0).cumsum()
    _eta = None if on_order_eta_days is None else max(0, int(on_order_eta_days))
    if units_on_order and _eta:
        # Available stock on each future day = today's stock + the PO once it has landed.
        _days_out = (fc_fwd["ds"] - today()).dt.days
        _arrived = (_days_out >= _eta).astype(float) * float(units_on_order)
        so = fc_fwd[fc_fwd["cum_mid"] >= (float(stock) + _arrived)]
        eff_stock = stock + units_on_order      # for display/back-compat only
    else:
        eff_stock = stock + units_on_order
        so = fc_fwd[fc_fwd["cum_mid"] >= eff_stock]
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
    # Demand volatility is still surfaced for context, but it no longer picks the
    # service level — that now comes from the protection tier below.
    vcolor, vstrat, vdesc, _z_from_cv = get_demand_volatility(e.get("residual_dispersion"))

    # Protection level: recommend from the item's economics when supported, otherwise
    # fall back to the margin/default heuristic. Manual override still wins.
    # Margin, and the protection tier chosen from it, on the REGULAR price: a sale this
    # week is not a reason to protect a product less for the next three months.
    price = e.get("regular_price") or e.get("effective_price") or e.get("last_price")
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
    # A scheduled price outside what this product has sold at is an extrapolation, and
    # its effect is uncertain in proportion to how far out it is. Add that uncertainty,
    # over the window this order has to cover, in quadrature — so an order for a deep
    # discount leans toward the upside instead of betting on one straight line.
    # An effect that's off is off on EVERY day it applies: a price response 20% too weak
    # is 20% too weak all week. So each effect's uncertainty adds up day by day rather than
    # averaging out. Price effects share one response, so they add up together; separate
    # holidays are learned separately, so those combine as independent errors.
    _x_extra = 0.0
    _win = fc_fwd.head(decision_horizon)
    _sx = float(pd.to_numeric(_win["x_sd"], errors="coerce").fillna(0.0).sum()) if "x_sd" in _win.columns else 0.0
    _sh2 = 0.0
    _hol_units = {}     # holiday "key|anchor" → its forecast units inside the order window
    if "h_sd" in _win.columns:
        _hs = pd.to_numeric(_win["h_sd"], errors="coerce").fillna(0.0)
        _hk = _win["holiday"] if "holiday" in _win.columns else pd.Series([None] * len(_win), index=_win.index)
        for _k in {k for k in _hk.tolist() if k}:
            _sh2 += float(_hs[_hk == _k].sum()) ** 2
            _hol_units[_k] = float(pd.to_numeric(_win.loc[_hk == _k, "yhat"], errors="coerce")
                                   .fillna(0.0).clip(lower=0).sum())
    _x_extra = float(np.sqrt(_sx ** 2 + _sh2))
    if _sx > 0:
        _basis = float(np.sqrt(_basis ** 2 + _sx ** 2))
    # The standing buffer stops here. Holiday cover goes on top of it, but only while a
    # holiday is inside the order window: it's a temporary top-up sold down afterwards,
    # so the cost curve (which prices the buffer as carried all year) is given the buffer
    # without it, and the cover is reported, and priced, on its own line.
    _basis_standing = _basis
    if _sh2 > 0:
        _basis = float(np.sqrt(_basis ** 2 + _sh2))
    rec_key, rec_reason, rec_source, rec_economics = recommend_economic_protection(
        price=price, unit_cost=unit_cost, fees=fees, margin_pct=margin_pct,
        demand_spread=_basis_standing, coverage_days=coverage_days,
        days_history=days_hist, total_sales=total_sales, demand_class=e.get("demand_class"),
        lead_time_days=lead_time_days, sku_id=str(sku_id),
        # Use the rate the user actually set. This defaulted to 25% no matter what they'd
        # entered, so a product falling back to the cost curve was priced on an assumption
        # the rest of the app had already replaced.
        holding_annual=max(0.0, _session_holding_pct / 100.0),
        backtest_rec=_tier_lookup(str(sku_id), lead_time_days, coverage_days)[0],
    )
    chosen_key = protection if protection in _TIER_BY_KEY else rec_key
    tier = _TIER_BY_KEY[chosen_key]
    z = tier["z"]
    safety = round(z * _basis)
    # Of which, holiday cover: what the holidays in the order window add to the standing
    # buffer at this protection level. Its carrying cost is one order cycle's (it is
    # bought before the holiday and sold down over the cycle after), not a year's.
    _hc_units = max(0, safety - round(z * _basis_standing))
    if inactive:
        safety = 0   # no safety stock for a discontinued item — don't buffer demand of 0
        _hc_units = 0
    _hc_now = _HC.current()
    holiday_cover = None
    if _hol_units:
        _hc_cost = (_hc_units * float(unit_cost) * max(0.0, _session_holding_pct / 100.0)
                    * coverage_days / 365.0) if unit_cost not in (None, "") else None
        holiday_cover = {
            "units": int(_hc_units),
            "standingUnits": int(safety - _hc_units),
            "carryCost": round(_hc_cost, 2) if _hc_cost is not None else None,
            "carryDays": int(coverage_days),
            "rateSource": _hc_now["source"],
            "holidays": [{"key": k.split("|")[0], "name": _HS.name_of(k.split("|")[0]),
                          "anchor": k.split("|")[1] if "|" in k else None,
                          "unitsInWindow": round(u, 1),
                          "rate": _HC.rate_for(k.split("|")[0], _hs_years_of(fc_fwd, k))}
                         for k, u in sorted(_hol_units.items(), key=lambda kv: -kv[1])],
        }
    # Is a better recommendation being computed right now? The cost curve is a real
    # answer, not a placeholder — but if a backtest is mid-flight the number is about to
    # change, and showing it as settled invites a decision the user would then revisit.
    _job = _bt_job_snapshot()
    _bt_cached, _bt_match = _tier_lookup(str(sku_id), lead_time_days, coverage_days)
    if _bt_cached is None:
        _bt_cached = next((v for k, v in _backtest_tier_cache.items()
                           if k.startswith(f"{sku_id}|")), None)
    if _job.get("status") == "running" and rec_source != "backtest":
        _status = "calculating"
    elif rec_source == "backtest":
        _status = "backtest"
    elif _bt_cached:
        # We have a measured pick for this SKU but it was run for different settings.
        _status = "stale"
    else:
        _status = rec_source
    # What this tier ACTUALLY delivered when replayed against real history, as opposed
    # to what its name promises. The two differ — and systematically, not randomly: the
    # buffer is sized as z x sigma off a NORMAL table, while real demand errors have
    # fatter tails, so every tier under-delivers its nominal number. On this catalogue
    # nominal 95% measured 84.8%, and nominal 99.5% measured 95.3%.
    # The tier machinery already absorbs this (it picks by measured cost, which is how it
    # settled on 99.5%), so the arithmetic is not wrong — but "Maximum protection (99.5%)"
    # reads as a promise of 99.5%, and it is not one. Ship the measured figure alongside
    # so the UI can stop overstating it.
    _achieved_pct = None
    try:
        for _t in (((_last_backtest or {}).get("tierAnalysis") or {}).get("tiers") or []):
            if _t.get("tier") is not None and float(_t["tier"]) == float(tier["pct"]):
                _achieved_pct = _t.get("achievedService")
                break
    except Exception:
        _achieved_pct = None

    protection_info = {
        "chosen": chosen_key, "recommended": rec_key, "options": PROTECTION_TIERS,
        # Measured stayed-in-stock rate for the chosen tier; None until a backtest exists.
        "achievedPct": _achieved_pct,
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
    cw = fc_fwd.iloc[doff:doff + coverage_days]
    if cw.empty: cw = fc_fwd.tail(coverage_days)
    add = cw["yhat"].clip(lower=0).mean(); cov_qty = round(add * coverage_days); target = cov_qty + safety
    dud = max(d_ro, 0) + lead_time_days
    dem_del = round(fc_fwd.head(dud)["yhat"].clip(lower=0).sum())
    # …and only count it against the NEW order if it actually arrives before that order
    # does. A PO landing after your next delivery can't cover demand in between.
    _po_in_time = units_on_order if (_eta is None or _eta <= dud) else 0
    stock_del = max(0, stock - dem_del + _po_in_time)
    if d_ro > 0:
        sbr = round(fc_fwd.head(d_ro)["yhat"].clip(lower=0).sum()); psr = max(0, stock - sbr)
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
            reason = (f"Only {days_hist} days of sales history, so this forecast is still "
                      f"speculative. The recommended {order_qty:,} exceeds ~{wks} weeks of cover; "
                      f"consider capping the first order near {cap:,} units and reordering once "
                      f"more sales come in.")
        else:
            reason = (f"Only {days_hist} days of sales history, so treat this as provisional. The "
                      f"recommended {order_qty:,} is within ~{wks} weeks of cover, a reasonable "
                      f"first order; plan to reorder as more sales land.")
        order_guardrail = {
            "active": True, "capExceeded": exceeded, "suggestedCap": cap,
            "coverDays": GUARDRAIL_COVER_DAYS, "rawOrderQty": order_qty,
            "daysHistory": days_hist, "reason": reason,
        }

    cmp = today().to_period("M")
    usf = int(df_train[df_train["ds"].dt.to_period("M") == cmp]["y"].sum())
    fcr = float(fc_fwd[fc_fwd["ds"].dt.to_period("M") == cmp]["yhat"].clip(lower=0).sum())
    lmt = int(df_train[df_train["ds"].dt.to_period("M") == (cmp - 1)]["y"].sum())
    # The same month a year ago. Against last month, a seasonal product's change is mostly
    # its season (a sun hat is always up in May); against the same month last year the
    # season cancels out and what's left is whether the product is actually growing.
    # Only when the whole of that month is inside the history.
    _ly = cmp - 12
    lyt = (int(df_train[df_train["ds"].dt.to_period("M") == _ly]["y"].sum())
           if len(df_train) and df_train["ds"].min() <= _ly.to_timestamp() else None)

    cards = []; cms = pd.Timestamp(today().to_period("M").to_timestamp())
    for i in range(forecast_months):
        ms = cms + relativedelta(months=i); me = ms + relativedelta(months=1) - pd.Timedelta(days=1)
        fm = forecast[(forecast["ds"] >= ms) & (forecast["ds"] <= me)]; fut = fm[fm["ds"] > last_actual]
        past = df_train[(df_train["ds"] >= ms) & (df_train["ds"] <= last_actual)]
        asum = int(past["y"].sum()); fsum = float(fut["yhat"].clip(lower=0).sum())
        # The range for the month's TOTAL, by the one rule every multi-day range uses
        # (bands.py). It used to add up the days' lows and highs, which assumes every day
        # misses fully in the same direction: 14 to 490 around a 215 forecast.
        flo, fhi = _month_band(fut, fsum, e)
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
    _fc_rate = float(fc_fwd["yhat"].clip(lower=0).head(90).mean()) if len(fc_fwd) else 0.0
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
            "message": (f"The forecast averages {_fc_rate:.2f}/day, about "
                        f"{round((1 - _fc_rate / _recent) * 100)}% below the {_recent:.2f}/day this "
                        f"product sold over the last {'90' if _r90 >= _r180 else '180'} days. Sparse-demand "
                        f"models lower their estimate during runs of zero-sale days, so treat the order "
                        f"suggestion as a floor and check the backtest before trusting a cut this size."),
        }
    _sparse_sub = sparse_subtype(_y, e.get("demand_class"))
    # One plain sentence describing how this product actually sells, so the UI never has
    # to render "0 per day" as though that were the story.
    if _sparse_sub == "low_volume_regular":
        _demand_story = (f"Slow but regular: sells on about {round(_sell_share * 100)}% of days, "
                         f"roughly {_fc_rate:.1f} a day (about {round(_fc_rate * 30)} a month). "
                         f"Plenty of zero-sale days is normal for this product, not a warning sign.")
    elif _sparse_sub == "true_intermittent":
        _demand_story = (f"Sporadic: sells on about {round(_sell_share * 100)}% of days, often with long "
                         f"gaps. Expect roughly {round(_fc_rate * 30)} a month arriving in bursts rather "
                         f"than steadily.")
    elif slow_seller:
        _demand_story = (f"Low volume: about {_fc_rate:.1f} a day, roughly {round(_fc_rate * 30)} a month.")
    else:
        _demand_story = None
    # What has sold since the shelf was counted, and therefore what is on it now. Only
    # meaningful for a figure someone actually counted — decrementing an assumed 50 would
    # be compounding one invention with another.
    _since_count = units_sold_since(str(sku_id), stock_counted_at, df_train) if stock_counted else None
    _derived_stock = (max(0, int(round(stock - _since_count["total"])))
                      if (_since_count and _since_count.get("since")) else None)

    hist = [{"x": int(r["ds"].timestamp() * 1000), "y": int(r["y"])} for _, r in df_train.iterrows()]
    fut_line, fut_rng = [], []
    _fc_from = today().normalize()
    for _, r in future_fc.iterrows():
        # BOTH ends. The upper bound has always been here; the lower one had not, so a
        # product whose data stopped weeks ago emitted weeks of "forecast" dated in the
        # PAST — the curve began the day after the last training row while every headline
        # number (stockout date, reorder date) was measured from today, and the two
        # silently disagreed. Zero-filling to the window end fixes the usual cause; this
        # makes the chart correct even when a series legitimately ends early.
        if r["ds"] < _fc_from: continue
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
        # The grade's two axes and its cause, separately addressable. `status` alone is
        # ordinal and comparable; `statusReason` says why without the UI parsing prose.
        "statusReason": grade["reason"], "predictability": grade["predictability"],
        "decisionHorizonDays": grade["decisionHorizonDays"],
        "readyAtDays": grade["readyAtDays"], "usableAtDays": grade["usableAtDays"],
        # What the projection was actually allowed to cover, and whether that is shorter
        # than what was asked for — so the chart can say "3 months requested, 133 supported"
        # rather than silently drawing an extrapolation.
        "forecastDays": int(tot_fc_days), "forecastMonths": int(forecast_months),
        "horizonCapped": bool(horizon_capped), "requestedForecastDays": int(requested_fc_days),
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
        "residualDispersion": e.get("residual_dispersion"), "demandRefMean": e.get("residual_ref_mean"),
        "demandSigmaObserved": e.get("residual_sigma_observed"),
        "demandVolatilityLabel": volatility_label(e.get("residual_dispersion")),
        "hasPrice": e["has_price"], "hasPromotion": e["has_promo"], "priceVaried": e["price_varied"],
        "priceWellSampled": e["price_well_sampled"], "lastPrice": e["last_price"],
        "priceModeled": e.get("price_modeled", False), "currentPrice": e.get("effective_price"),
        "listPrice": e.get("regular_price"),
        "priceDiscounted": bool((e.get("live_price") or {}).get("discounted")),
        "livePrice": e.get("live_price"), "posEvents": e.get("pos_events") or [],
        "priceUsable": e.get("price_usable", False), "priceMixedDays": e.get("price_mixed_days", 0),
        "priceLevelMinDays": 30, "lastRecordedDate": e.get("last_recorded_date"),
        "priceResponse": e.get("price_response"),
        # Extra safety stock (units, before the protection z) for extrapolated price days.
        "priceExtrapolationSpread": round(_x_extra, 1),
        # The same, split: how unsure the price effects and the holiday lifts are over the
        # lead time plus coverage (units, before the protection z). Grows with how deep a
        # discount goes past known prices and how few years a holiday was learned from.
        "effectBuffer": {"price": round(_sx, 1), "holiday": round(float(np.sqrt(_sh2)), 1),
                         "total": round(_x_extra, 1)},
        # Temporary extra buffer for the holidays inside the order window (holiday_cover.py):
        # already inside safetyStock and the order quantity; split out so the page can show
        # it on its own line and price it as one cycle's carrying, not a year's.
        "holidayCover": holiday_cover,
        "holidays": _holidays_view(e.get("holidays"), tot_fc_days),
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
        "stockSource": _src, "stockCounted": stock_counted, "stockUsed": int(stock),
        "assumedStock": (None if stock_counted else ASSUMED_STOCK),
        # A COUNT PLUS WHAT HAS SOLD SINCE. A shelf someone counted on Monday is not
        # Monday's number on Thursday, and the tool knows exactly what left in between —
        # closed days from the catalogue, today from the hourly side channel. Reported
        # rather than silently applied: the caller sees the count, the drawdown and the
        # derived level, so a number that moved can always be explained.
        "sinceCount": _since_count,
        "derivedStock": _derived_stock,
        "todaySales": {**_today_sales.status(today().strftime("%Y-%m-%d")),
                       "units": float(_today_sales.units_for(str(sku_id),
                                                             today().strftime("%Y-%m-%d")))},
        # How old the newest sale on file is. Nothing re-imports on a schedule, so the
        # page can sit open for days looking live while the demand under it does not
        # move. Every date on screen advances; the data does not. The UI says so.
        "lastSalesDate": pd.Timestamp(last_actual).strftime("%Y-%m-%d"),
        "dataAgeDays": max(int((today().normalize() - pd.Timestamp(last_actual).normalize()).days), 0),
        "avgDailyDemand": float(round(add, 1)), "safetyStock": safety, "coverageQty": cov_qty,
        "targetInventory": target,
        # Order quantity is target minus stock-at-delivery — the same fiction if the stock
        # half was invented. Suppressed rather than guessed.
        "orderQty": (order_qty if stock_counted else 0), "orderGuardrail": order_guardrail,
        "projectedStockReorder": psr,
        "stockAtDelivery": stock_del, "salesBeforeReorder": sbr,
        "stockoutTimestamp": (so_ts if stock_counted else None),
        "reorderTimestamp": (ro_ts if stock_counted else None),
        # A stockout date in the PAST (d_so < 0) means the item is already out — surface
        # that as 0 days ("out now"), never a negative countdown. `alreadyOut` lets the UI /
        # exports say "Out of stock" instead of showing a bare 0.
        #
        # ALL THREE ARE NULL WHEN NOBODY HAS COUNTED THE STOCK. Every one of them is a
        # subtraction from the stock figure, so with an assumed figure they are arithmetic
        # on a number we invented. That is exactly how a product nobody had counted, and
        # which had not sold in two months, came to display "Overdue 13d — order 0": the
        # assumed level was read as a real position and the countdown ran from it. A
        # missing countdown is honest; a confident wrong one costs someone an order.
        "daysUntilStockout": (max(int(d_so), 0) if (stock_counted and so_ts is not None) else None),
        # A counted zero is out now too. The stockout search starts today, so with nothing
        # on the shelf it lands on day 0, never below it, and this used to stay False:
        # screens and exports said "0 days" instead of "Out of stock".
        "alreadyOut": bool(stock_counted and (float(stock) <= 0
                                              or (so_ts is not None and int(d_so) < 0))),
        "daysUntilReorder": (d_ro if (stock_counted and ro_ts is not None) else None),
        "currentMonth": {"unitsSoFar": usf, "forecastRemaining": round(fcr), "lastMonthTotal": lmt,
                         "lastYearTotal": lyt},
        "monthCards": cards, "monthlySummary": monthly,
        "chartDataHistory": hist, "chartDataFuture": fut_line, "chartDataRange": fut_rng, "upcomingPromos": promos,
        # Days the store is closed (Closed days on the Fleet overview) across the chart,
        # past and future, as chart x values. They're left out of training and forecast
        # at 0; the chart marks each with a dot on the axis.
        "closedDays": _closed_chart_days(df_train, sku_id, tot_fc_days),
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
# Ordering for "needs attention". "Stock not counted" sits just below a real stockout:
# it is genuinely actionable (go and count it) and it blocks every other verdict, so it
# should not be buried under products whose position we actually know.
STATUS_RANK = {"Stockout risk": 0, "Stock not counted": 1, "Dead stock": 2,
               "Overstocked": 3, "Reorder due": 4, "Healthy": 5}


def sc_effective_price_today(entry, events=None):
    """The REGULAR price: the POS's listed price when it reports one, else the last price
    recorded in sales. Logged events never rewrite it — see _effective_price."""
    lp = (entry.get("live_price") or {}).get("listPrice")
    return float(lp) if lp else _effective_price(entry.get("last_price"))


def sc_on_promo_today(entry, events):
    # A discount the POS says is on right now outranks everything else: it is observed.
    lv = entry.get("live_price") or {}
    if lv.get("discounted"):
        return True, lv.get("discountPct")
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
        fc = fc[fc["ds"] > today()].copy().reset_index(drop=True)
        # Holidays aren't promotions: the regular-price view still has Black Friday in it.
        ff = entry.get("future_fc")
        if ff is not None and "h_mult" in ff.columns and len(fc):
            hm = ff[["ds", "h_mult"]].drop_duplicates("ds")
            fc = fc.merge(hm, on="ds", how="left")
            m = pd.to_numeric(fc["h_mult"], errors="coerce").fillna(1.0)
            for c in ("yhat", "yhat_lower", "yhat_upper"):
                if c in fc.columns:
                    fc[c] = fc[c] * m
            fc = fc.drop(columns=["h_mult"])
        return fc
    except Exception as ex:
        print(f"[scorecard] baseline failed: {ex}"); return entry.get("future_fc")


def _first_arr(f, stock, arrived):
    hit = f[f["cum"] >= (stock + arrived)]
    return None if hit.empty else max(int((hit.iloc[0]["ds"] - today()).days), 0)


def sc_days_of_cover(fc, stock, on_order=0, eta_days=None):
    """Days until projected demand exhausts the stock available.

    With `on_order` the units are counted only from the day they ARRIVE, which is the
    same rule /api/forecast uses for its stockout date. Called twice per product: once
    bare, for the "Days of Stock Remaining" column, which must keep meaning what is on
    the shelf today, and once with the inbound PO, for the health bar — a product with
    a container landing on Tuesday is not in the same position as one with nothing
    coming, and the bar was reading them identically."""
    if fc is None or fc.empty: return None
    f = fc.copy(); f["cum"] = f["yhat"].clip(lower=0).cumsum()

    def _first(avail):
        hit = f[f["cum"] >= avail]
        return None if hit.empty else max(int((hit.iloc[0]["ds"] - today()).days), 0)

    bare = _first(float(stock))
    qty = float(on_order or 0)
    if qty <= 0:
        return bare
    if eta_days is None:                      # no date given: available now, as /api/forecast assumes
        return _first(float(stock) + qty)
    eta = max(0, int(eta_days))
    # A delivery cannot cover demand that happened before it landed. Without this the
    # cumulative walk stepped straight over an in-progress stockout: nothing on the
    # shelf today plus a container due tomorrow came back as a month of cover.
    if bare is not None and bare < eta:
        return bare
    arrived = ((f["ds"] - today()).dt.days >= eta).astype(float) * qty
    return _first_arr(f, float(stock), arrived)


# sc_cover_score is gone with the "Stock coverage" bar it fed — see the note above the
# `breakdown` list in sc_score_one for why a point-in-time reading of a sawtooth made a
# poor health signal. sc_cover_direction below SURVIVES: it feeds the status and the
# recommendation wording, which read it alongside days-until-reorder rather than alone.


# The bar's colour cuts, as ScorecardTab implements them: >= .66 green, >= .33 amber,
# below that red. Named here because the score below is built to land on them.
BAR_GREEN, BAR_AMBER = 0.66, 0.33


def sc_volatility_score(d):
    """Bar length. Green means good, so the score runs opposite to the name: more
    volatile gives a shorter bar.

    The ramp is piecewise so that each colour band lines up exactly with the label
    beside it. A first cut used one smooth slope and put 25 of 26 products on a green
    bar, including ones the label called volatile — a row reading "somewhat volatile"
    next to a full green bar makes the reader decide which half to believe, and it was
    the same everything-in-one-bucket failure the old CV had, moved from the words to
    the colour."""
    if d is None: return 0.6
    d = float(d)
    if d <= 1.0: return 1.0
    if d <= DISPERSION_STEADY:                                   # steady -> green
        return 1.0 - (d - 1.0) / (DISPERSION_STEADY - 1.0) * (1.0 - BAR_GREEN - 0.01)
    if d < DISPERSION_VARIABLE:                                  # somewhat -> amber
        span = DISPERSION_VARIABLE - DISPERSION_STEADY
        return (BAR_GREEN - 0.01) - (d - DISPERSION_STEADY) / span * (BAR_GREEN - 0.01 - BAR_AMBER)
    if d >= DISPERSION_FLOOR: return 0.10                        # highly -> red
    span = DISPERSION_FLOOR - DISPERSION_VARIABLE
    return max(0.10, (BAR_AMBER - 0.01) - (d - DISPERSION_VARIABLE) / span * (BAR_AMBER - 0.11))


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


def units_sold_since(sku_id: str, counted_at, df_train=None) -> dict:
    """Units sold since a stock count was taken, so a count can stay true after it.

    A counted shelf is accurate for exactly as long as nobody buys anything. Without this
    a manual count is right the moment it is entered and drifts every hour afterwards,
    which on a fast line makes it wrong by the afternoon — and the whole reason someone
    counted was to stop guessing.

    Two sources, because one day is not like the others:

      closed days  come from the catalogue, which is authoritative and complete for every
                   day that has finished.
      today        comes from today_sales, the hourly side channel, because the catalogue
                   deliberately does not carry the day in progress.

    They cannot overlap: the catalogue stops at the last complete day and today_sales only
    ever holds the current one. That is enforced at both ends rather than assumed here.

    `counted_at` is a date or timestamp. A count taken mid-day cannot be attributed to an
    hour — the catalogue's resolution is a day — so the day of the count is deliberately
    EXCLUDED from the closed-day sum. Counting the whole of that day would subtract sales
    that happened before the person counted, understating the shelf. Today's own figure is
    included only when the count was taken before today.
    """
    out = {"closedDays": 0.0, "today": 0.0, "total": 0.0, "since": None,
           "todayIncluded": False}
    if counted_at is None:
        return out
    try:
        cd = pd.to_datetime(counted_at)
        # The browser saves the moment of a count as UTC ("2026-09-29T14:03:00.000Z").
        # Compared as-is with the plain dates of the sales history, that raised a
        # TypeError and every later forecast for the product failed with a 500. The day
        # that matters is the store's day, so convert to its time zone, then drop the zone.
        if cd.tzinfo is not None:
            cd = cd.tz_convert(_APP_TZ) if _APP_TZ else cd.tz_convert(None)
            cd = cd.tz_localize(None)
        cd = cd.normalize()
    except (ValueError, TypeError):
        return out
    out["since"] = cd.strftime("%Y-%m-%d")

    if df_train is not None and len(df_train):
        after = df_train[df_train["ds"] > cd]
        out["closedDays"] = float(after["y"].clip(lower=0).sum()) if len(after) else 0.0

    _today_key = today().strftime("%Y-%m-%d")
    if cd < today().normalize():
        out["today"] = float(_today_sales.units_for(str(sku_id), _today_key))
        out["todayIncluded"] = True
    out["total"] = round(out["closedDays"] + out["today"], 2)
    return out


def _sells_in_a_year(df, stock) -> bool:
    """Would the past year's sales clear this stock, on a line that isn't fading (the past
    year sold at least half what the year before did)?"""
    try:
        if df is None or not len(df) or stock is None:
            return False
        t = pd.Timestamp(df["ds"].max())
        yr = float(df.loc[df["ds"] > t - pd.Timedelta(days=365), "y"].sum())
        prev_m = (df["ds"] > t - pd.Timedelta(days=730)) & (df["ds"] <= t - pd.Timedelta(days=365))
        prev = float(df.loc[prev_m, "y"].sum()) if int(prev_m.sum()) >= 300 else None
        if prev is not None and yr < 0.5 * prev:
            return False
        return yr > 0 and yr >= float(stock)
    except Exception:                                       # noqa: BLE001
        return False


def sc_status(c, dur, st, lt, cov, po, is_new=False, dormant=False, stock_counted=True,
              ever_sold=True, off_season=False, sells_in_a_year=False):
    """The ONE status definition. The fleet card renders these verbatim rather than
    classifying again, so the two tabs cannot disagree about a product.

    Every threshold here is per-product: the reorder bands come from this product's
    lead time, and the coverage ceiling is its own lead time + coverage window. A
    line with a 90-day coverage target is healthy at 70 days of stock; one with a
    14-day target is overstocked at the same 70. Nothing is compared against a
    fleet-wide constant.

    PRECEDENCE, AND WHY IT IS THIS ORDER.

    1. Stock not counted. Everything below this line is arithmetic on the stock figure,
       so if that figure was invented, every verdict built on it is invented too. This
       has to come first or the assumed number quietly becomes a diagnosis — which is
       what produced "overdue, order 0" on products nobody had counted.

    2. Dormant. A product that has stopped selling is dead whatever its shelf says, and
       that judgement belongs to the demand series, not the stock position. This replaces
       a rule that decided dead-vs-overstocked on HISTORY LENGTH: under 90 days it said
       "Overstocked", over it said "Dead stock". Two products behaving identically — no
       sales, plenty of cover — got different badges because one had been on sale longer,
       which is exactly the inconsistency this ladder exists to remove. Dormancy is judged
       against each product's own rhythm, so it needs no such crutch.

    3. Stockout risk, 4. Dead stock (the INVENTORY sense: still selling a little, but
       buried in cover), 5. Overstocked, 6. Reorder due, 7. Healthy — as before.

    `is_new` still protects a genuinely new product from the inventory-sense dead call,
    because a line that launched three weeks ago has not had its chance. It no longer
    decides anything about dormancy — see `ever_sold` below.
    """
    if not stock_counted: return "Stock not counted"
    # HAS IT SOLD, not HOW OLD IS IT.
    #
    # `is_dormant` is true for two different situations: a product that sold and then
    # stopped, and one that has never sold at all. Only the second deserves the benefit
    # of the doubt, and total history length cannot tell them apart — a line that traded
    # for a month and then went quiet for two has 89 days of history and would be waved
    # through by a 90-day "still new" guard as though it had just launched. It has not
    # just launched; it has died. `ever_sold` asks the question that actually separates
    # them. A never-sold product falls through to the rules below, where `is_new` still
    # protects it while it is genuinely young and stops protecting it once it is not.
    if dormant and ever_sold: return "Dead stock"
    risk, due = sc_bands(lt)
    # Not just "already late" — anything inside half a lead time is a position you
    # may not be able to recover by ordering today.
    if not po and dur is not None and dur < risk: return "Stockout risk"
    # A new product is never "Dead stock" — it hasn't had a chance to prove out.
    # It can still read as "Overstocked" (that's an accurate, recoverable signal).
    # Dead stock is stock that won't sell. Out of season (quiet now as it was this time
    # last year) has sold nothing lately by definition, and a line whose past year's sales
    # would clear this stock, and isn't fading, is carrying stock into its next season
    # (a pool float in late September). Both are over target cover, not dead.
    if (st is not None and st < 0.05) and (c is None or c > (lt + cov) * 3):
        return "Overstocked" if (is_new or off_season or sells_in_a_year) else "Dead stock"
    if c is None or c > (lt + cov): return "Overstocked"
    if not po and dur is not None and risk <= dur <= due: return "Reorder due"
    return "Healthy"


def sc_recommendation(direction, tier, st, c, lt, on_promo, po, cost_known, dur,
                      is_new=False, rising=False, days_hist=None, cov=30, po_sized=False, dur_bare=None):
    # "soon" has to mean the same thing the status does, or the recommendation and the
    # badge above it describe different products. Both come from sc_bands now.
    #
    # With a sized PO, `dur` already counts it from the day it lands and `dur_bare` is the
    # shelf alone. "Covered" is said only when the PO actually covers the gap; a PO too
    # small or too late gets said as such instead of "No action needed".
    _risk, _due = sc_bands(lt)
    prof = tier in ("A", "B"); soon = dur is not None and dur <= _due
    if po and not po_sized and soon:
        return ("Covered: reorder in transit", "A purchase order is already on its way. No action needed.")
    if po_sized:
        bare_soon = dur_bare is not None and dur_bare <= _due
        if not soon and bare_soon:
            return ("Covered: reorder in transit", "The purchase order on its way covers this. No action needed.")
        if soon:
            return ("Order in transit won't cover it",
                    "Even counting the order on its way (from the day it lands), stock runs short"
                    + (f" and the reorder point passed {abs(dur)} day{'s' if abs(dur) != 1 else ''} ago" if dur < 0 else "")
                    + ". Order more now, or bring the delivery forward.")
    if soon:
        if dur < 0:
            n = abs(dur); t = (" Profitable line, prioritize it." if prof else " Margins are slim, keep it disciplined.")
            return ("Reorder overdue: order immediately", f"Reorder point passed {n} day{'s' if n != 1 else ''} ago. Order today." + t)
        if dur == 0:
            t = (" Profitable, so give it priority." if prof else " Margins thin, order conservatively.")
            return ("Reorder due today", "Latest you can reorder and still arrive in time." + t)
        if not cost_known: return ("Reorder approaching", "Enter a unit cost to size the order against profitability.")
        if prof: return ("Reorder approaching: profitable line", f"Reorder point in ~{dur} day{'s' if dur != 1 else ''}. Prioritize it.")
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
            return ("Overstocked for now: hold, it's still establishing",
                    f"It's carrying {cover_txt} of cover (more than your {cov}-day target) and only ~{pct}% has sold{trend_txt}, "
                    f"but with only {hist_txt} of sales history this read is provisional. Pause or trim new orders and "
                    f"let demand settle. Don't mark it down or discontinue yet.")
        very_dead = (c is None) or (c > (lt + 30) * 4)
        slow = st is not None and st < SELLTHROUGH_LOW   # velocity below the healthy band
        if slow and very_dead and tier in ("C", "F", "—"):
            return ("Excess stock: consider clearing",
                    ("Sales are minimal, stock far exceeds demand, and it loses money on each sale. A strong candidate to discontinue or liquidate."
                     if tier == "F" else
                     "Sales are minimal and stock far exceeds demand. Consider a markdown, promotion, or discontinuing it."))
        if slow:
            # Velocity overrides a good margin: a healthy per-unit margin earns
            # little to nothing when almost none of the stock is actually selling.
            note = (" The per-unit margin looks healthy, but so little is selling that little to no profit is being realized."
                    if prof else " Sales are minimal and the margin is thin.")
            return ("Overstocked and barely selling: reduce orders",
                    f"Only ~{pct}% of stock has sold recently, and it's projected to far outlast demand." + note +
                    " Cut future orders, and consider a markdown or promotion to move it.")
        if prof:
            return ("Overstocked but profitable: reduce future orders",
                    "You're carrying more than demand needs, but it sells at a healthy margin. Keep it and lower future order quantities.")
        return ("Overstocked: scale back ordering", "Stock will outlast demand and the margin is slim. Reduce future orders.")

    # Cover is healthy/short — but very high velocity still signals under-stocking,
    # even when the margin looks fine (you're leaving sales on the table).
    fast = st is not None and st > SELLTHROUGH_HIGH
    if fast:
        note = (" It's a healthy-margin line, so missed sales are costly. Prioritize keeping it in stock."
                if prof else " Keep an eye on the margin, but avoid stocking out.")
        return ("Selling fast: risk of under-stocking",
                f"About {pct}% of available stock has sold recently, so you may be under-stocked." + note +
                " Consider raising the stock level or reorder point so you don't miss sales.")
    base = ("Healthy: maintain position", "Stock is well matched to demand. Hold and monitor.")
    if on_promo: return (base[0], base[1] + " A promotion is active, so sales are temporarily elevated.")
    return base


def _from_today(fc):
    """The forecast from today on. A product's forecast starts the day after its last sale
    on file, so with sales data 10 days old the first 10 rows are days that have already
    passed. Counting their demand against today's stock made the scorecard call a product
    "Stockout risk" while its own page said "reorder in 7 days". Same rule as fc_fwd in
    get_forecast, including its fallback for data older than the whole horizon."""
    if fc is None or fc.empty:
        return fc
    fwd = fc[fc["ds"] >= today()]
    return fwd.reset_index(drop=True) if len(fwd) else fc


def sc_score_one(entry, fc, stock, unit_cost, fees, reg_price, td, lt, cov, po, on_promo,
                 on_order=0, on_order_eta=None, stock_source=None, sku_id=None):
    fc = _from_today(fc)
    df = entry["df_train"]; cutoff = today() - pd.Timedelta(days=td)
    tr = df[df["ds"] > cutoff]; tu = int(tr["y"].sum()) if len(tr) else 0; ta = tu / max(td, 1)
    n30 = fc.head(30) if fc is not None else None
    fa = float(n30["yhat"].clip(lower=0).mean()) if n30 is not None and len(n30) else None
    trend = (fa / ta) if (fa is not None and ta > 0) else None
    cover = sc_days_of_cover(fc, stock); direction = sc_cover_direction(cover, lt, cov)
    # On-hand, and the same figure once stock already on its way is counted. `cover`
    # stays on-hand-only because that is what "Days of Stock Remaining" claims to be;
    # the detail panel's stockout date counts the PO, so both are reported rather than
    # one screen quietly disagreeing with the other.
    inbound = int(on_order or 0)
    cover_pos = (sc_days_of_cover(fc, stock, inbound, on_order_eta) if inbound > 0 else cover)
    dur = None if cover is None else (cover - lt)
    # The reorder countdown with the open PO counted from the day it lands, which is what
    # the product page's reorder date already does. The status used to look only at
    # whether a PO existed: 10 units arriving in 40 days turned a product that runs out
    # in a week "Healthy". When the browser sends no quantity (older clients), the bare
    # flag is still trusted, as before.
    po_sized = bool(po) and inbound > 0
    dur_eff = (None if cover_pos is None else cover_pos - lt) if po_sized else dur
    denom = tu + max(stock, 0); st = (tu / denom) if denom > 0 else None
    svol = sc_volatility_score(entry.get("residual_dispersion"))
    days_hist = int((df["ds"].max() - df["ds"].min()).days) if len(df) > 1 else 0
    is_new = days_hist < SC_PROVISIONAL_DAYS
    rising = trend is not None and trend > 1.05
    # Provenance comes from the caller's params: the browser knows whether a figure was
    # typed, synced or invented. Absent that, a StockLog reading still proves someone has
    # seen this product on a shelf. Passed in as an argument — reaching into the caller's
    # locals for `cfg` is what broke the whole scorecard endpoint.
    # "unknown" IS NOT AN ANSWER — it is the absence of one.
    #
    # This first read the caller's value and only fell back to the log when the caller
    # sent nothing at all. But the browser always sends something: DEFAULT_PARAMS carries
    # stockSource "unknown", which is a truthy string, so the fallback never ran and every
    # product in a Square-connected catalogue was reported as uncounted — including ones
    # the sampler had a real reading for. A client saying "I don't know" must never
    # outrank evidence the server holds.
    _ssrc = str(stock_source or "").strip().lower()
    if _ssrc in ("", "unknown", "none", "null") and sku_id is not None:
        _ssrc = ("live" if _live_readings_apply(sku_id) and _slog.latest(str(sku_id)) is not None
                 else "unknown")
    if not _ssrc:
        _ssrc = "unknown"
    _counted = _ssrc in ("manual", "live", "sheet")
    # Cover itself stays on-hand only: a big PO shouldn't flip a product to "Overstocked"
    # before it has even landed. Only the reorder countdown counts it.
    status = sc_status(cover, dur_eff, st, lt, cov,
                       bool(po) and not po_sized, is_new,
                       dormant=bool(entry.get("dormant")), stock_counted=_counted,
                       ever_sold=bool(len(df) and float(df["y"].sum()) > 0),
                       off_season=bool(entry.get("off_season")),
                       sells_in_a_year=_sells_in_a_year(df, stock))
    _disp = entry.get("residual_dispersion")
    # ONE signal. Each of the other three was a restatement of something already on
    # screen, and each was deleted once that was demonstrated.
    #
    # "Sales trend" went first: a forecast-vs-run-rate ratio needing more nuance than a
    # bar can carry (see the note by the deleted sc_trend_score).
    #
    # "Sales velocity" went next. Sell-through is
    #     st = units_sold / (units_sold + stock) = W / (W + days_of_cover)
    # so it is days-of-cover in different units. Checked over 19,152 lead-time x
    # coverage x days-of-cover combinations, a per-SKU velocity bar and the Stock
    # coverage bar disagreed zero times. It was a second rendering of that row.
    #
    # "Stock coverage" has now gone too, and for a reason worth writing down because it
    # is not obvious. Days of cover is a SAWTOOTH: it peaks the day a delivery lands and
    # falls to the safety buffer just before the next one, so reading it against a fixed
    # band scores where a product sits in its reorder cycle as much as whether anything
    # is wrong. Simulated against this tool's OWN reorder policy, with a perfect forecast
    # and no stockout ever occurring, a correctly-run product read "short" on 24% of days
    # at a 14-day lead time and 39% at 60 days. Worse, the bar barely separated the case
    # it existed to catch: a product bought four months at a time scored 0.71 average
    # against 0.82 for the same product ordered properly, while the order size itself
    # separated them 133 days to 34. A snapshot of a cycling number dilutes a structural
    # fact into noise.
    #
    # Nothing actionable was lost. The bottom of the cycle is already said, and said
    # better, by the STATUS, which keys off days-until-reorder and therefore does not
    # cycle: "Reorder due", "Covered — reorder in transit", "Stockout risk". The top is
    # already caught by the overstock ceiling and the dead-stock test. `cover`,
    # `cover_pos`, `direction` and `st` all survive because those rules, the table
    # column, the scatter and the recommendation wording still read them.
    _sig = entry.get("residual_sigma_observed")
    _rate = entry.get("residual_ref_mean")
    breakdown = [
        {"key": "volatility", "label": "Sales volatility", "raw": volatility_label(_disp),
         "score": round(svol, 2),
         # The plain version of the same fact, in units rather than a ratio. A percentage
         # invites the reader to compare products of different sizes on it, which is the
         # trap that made the old CV figure unreadable; units per day do not.
         # Only when there IS a verdict. sigma and the rate exist even when the window
         # held too few units to judge dispersion, so this line was rendering
         # "about ±1.3 a day on 1.8 a day" directly under "not enough sales to say" —
         # the row contradicting itself in two lines.
         "note": (f"about ±{_sig:.1f} a day on {_rate:.1f} a day"
                  if (_disp is not None and _sig is not None and _rate) else None),
         "explanation": _volatility_tip(_disp, _rate)},
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
        flags.append({"type": "new", "text": f"New product ({days_hist} days of history), so the health read is provisional."})
    tier = sc_return_tier(margin)
    action, reason = sc_recommendation(direction, tier, st, cover, lt, on_promo, po, ck, dur_eff,
                                       is_new=is_new, rising=rising, days_hist=days_hist, cov=cov,
                                       po_sized=po_sized, dur_bare=dur)
    # Cover and the reorder countdown are both stock ÷ demand. With an assumed stock they
    # are arithmetic on an invented number, and the fleet reads daysUntilReorder straight
    # into an "Overdue Nd" badge — which is how an uncounted, unsold product came to be
    # reported as an overdue reorder. Null means "not measured", which the UI already
    # knows how to render; a number here would mean "measured", which would be false.
    return {"daysOfCover": (cover if _counted else None),
            "daysOfCoverWithInbound": (cover_pos if _counted else None),
            "unitsOnOrder": inbound,
            "coverDirection": (direction if _counted else None),
            # Counting an open PO from its arrival, like the product page's reorder date.
            "daysUntilReorder": (dur_eff if _counted else None),
            "sellThrough": round(st, 3) if st is not None else None, "trailingUnits": tu,
            "status": status, "statusRank": STATUS_RANK.get(status, 9), "signals": breakdown,
            # The two facts the status now turns on, published so the UI can explain a
            # badge rather than re-derive it and risk disagreeing.
            "dormant": bool(entry.get("dormant")), "offSeason": bool(entry.get("off_season")),
            "stockCounted": _counted, "stockSource": _ssrc,
            "flags": flags, "marginPct": margin, "returnTier": tier,
            "recommendation": {"action": action, "reason": reason}}


@app.post("/api/scorecard")
def get_scorecard(payload: dict = Body(default={})):
    td = int(payload.get("trailingDays", TRAILING_DAYS_DEFAULT)); inp = payload.get("skus", {}) or {}
    rows = []
    for sku_id, e in _cache().items():
        if e.get("df_train") is None or e.get("future_fc") is None: continue
        cfg = inp.get(sku_id, {})
        stock = int(cfg.get("stock", ASSUMED_STOCK)); uc = cfg.get("unitCost", None); uc = float(uc) if uc not in (None, "") else None
        fees = cfg.get("fees", None); fees = float(fees) if fees not in (None, "") else 0.0
        lt = int(cfg.get("leadTime", 14)); cov = int(cfg.get("coverage", 30)); po = bool(cfg.get("hasOpenPo", False))
        # Quantity and arrival date of an open PO. Only the boolean used to come across,
        # so the coverage bar judged a product with a container inbound exactly as it
        # judged one with nothing coming. Absent or malformed values fall back to 0/None,
        # which reproduces the previous behaviour rather than guessing.
        try:
            ooq = max(0, int(cfg.get("unitsOnOrder") or 0))
        except (TypeError, ValueError):
            ooq = 0
        _eta = cfg.get("onOrderEtaDays")
        try:
            eta = None if _eta in (None, "") else max(0, int(_eta))
        except (TypeError, ValueError):
            eta = None
        events = e.get("events", []) or []
        reg = sc_effective_price_today(e, events); on_promo, disc = sc_on_promo_today(e, events)
        pp = round(reg * (1 - float(disc) / 100), 2) if (on_promo and disc and reg is not None) else None
        live = sc_score_one(e, e["future_fc"], stock, uc, fees, reg, td, lt, cov, po, on_promo,
                            ooq, eta, stock_source=cfg.get("stockSource"), sku_id=sku_id)
        row = {"skuId": sku_id, "skuName": e.get("sku_name") or sku_id, "mode": e.get("mode"),
               "stock": stock, "unitCost": uc, "fees": fees, "costKnown": uc is not None and uc > 0,
               "regularPrice": reg, "onPromoToday": on_promo, "promoPrice": pp,
               "carryingValue": round(stock * uc, 2) if uc is not None else None,
               "leadTime": lt, "coverage": cov, "trailingDays": td,
               "winningModel": e.get("winning_model"), "demandClass": e.get("demand_class"), "route": e.get("route"),
               **live}
        if on_promo:
            # Same provenance, or the promo-baseline view grades the same product on a
            # different set of facts from the row it sits inside.
            row["baselineView"] = sc_score_one(e, sc_baseline_future_fc(e, reg), stock, uc, fees, reg,
                                               td, lt, cov, po, False, ooq, eta,
                                               stock_source=cfg.get("stockSource"), sku_id=sku_id)
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
    _snap = _cache()
    targets = [sku_id] if sku_id else list(_snap.keys())
    for sid in targets:
        e = _snap.get(sid)
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
    filename = f"{basename}_{_CLOCK.today().isoformat()}.xlsx"
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
        stock=int(_num(p.get("stock"), ASSUMED_STOCK)),
        lead_time_days=int(_num(p.get("leadTime"), 14)),
        coverage_days=int(_num(p.get("coverage"), 30)),
        strategy="balanced",
        forecast_months=3,   # exports always project the full 3-month horizon
        units_on_order=int(_num(p.get("unitsOnOrder"), 0)),
        on_order_eta_days=(int(p["onOrderEtaDays"]) if p.get("onOrderEtaDays") is not None else None),
        unit_cost=(float(p["unitCost"]) if p.get("unitCost") not in (None, "") else None),
        fees=float(_num(p.get("fees"), 0)),
        protection=(p.get("protection") or None),
        # Provenance travels into the export too, or the sheet re-derives it and gets it
        # wrong — which is precisely how live Square counts came to be labelled "assumed".
        stock_source=(p.get("stockSource") or None),
        stock_counted_at=(p.get("stockCountedAt") or None),
    )
    view["__stock"] = int(_num(p.get("stock"), ASSUMED_STOCK))
    return view


def _fleet_items(skus, folders):
    fmap = {}
    for f in (folders or {}).values():
        for sid in (f.get("skuIds") or []):
            fmap[sid] = f.get("name")
    items = []
    for sid in _cache():
        try:
            items.append({"view": _export_view(sid, (skus or {}).get(sid)), "folder": fmap.get(sid)})
        except Exception as ex:
            print(f"[export] {sid}: {ex}")
    return items


@app.post("/api/export/sku/{sku_id}")
def export_sku(sku_id: str, payload: dict = Body(default={})):
    if sku_id not in _cache():
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
               "skus": {sid: {"stock": _num((skus.get(sid) or {}).get("stock"), ASSUMED_STOCK),
                              "unitCost": (skus.get(sid) or {}).get("unitCost"),
                              "fees": (skus.get(sid) or {}).get("fees"),
                              "leadTime": _num((skus.get(sid) or {}).get("leadTime"), 14),
                              "coverage": _num((skus.get(sid) or {}).get("coverage"), 30),
                              # The same provenance and PO facts the screen's scorecard
                              # gets, or the report's statuses differ from the screen's.
                              "stockSource": (skus.get(sid) or {}).get("stockSource") or "unknown",
                              "unitsOnOrder": _num((skus.get(sid) or {}).get("unitsOnOrder"), 0),
                              "onOrderEtaDays": (skus.get(sid) or {}).get("onOrderEtaDays"),
                              "hasOpenPo": (skus.get(sid) or {}).get("hasOpenPo", False)}
                        for sid in _cache()}}
    try:
        sc_rows = get_scorecard(sc_body).get("rows", [])
    except Exception as ex:
        print(f"[export] scorecard: {ex}"); sc_rows = []
    # The backtest lives server-side, so "everything" can include it without the browser
    # having to send it back. Previously it was simply absent from the full report.
    open_pos = payload.get("openPOs") if isinstance(payload.get("openPOs"), dict) else {}
    return _stream(exports.build_all_workbook(items, suppliers, sc_rows, backtest=_last_backtest,
                                              open_pos=open_pos), "logitrack_full_report")


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
        d = _drop_closed(entry.get("df"), entry.get("date_shift_days"))
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


class _StaleBacktest(Exception):
    """A run whose products were set aside or replaced before it finished."""


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
                srcs = catalog_sources()
                stamp = _bt_stamp()
            # …then compute OUTSIDE it. The fit is the slow part and touches only the
            # snapshot, so holding the lock for minutes would freeze every other request.
            # Merge at RUN time, not just at request time: a queued request replays the
            # costs it captured when it was queued, which can be stale or empty. The
            # sheet costs are the floor no matter when — or from where — the job starts.
            _eff = _effective_sku_costs(sku_costs) or {}
            _sink = []
            res = BT.run_for_api(df, **params, sku_costs=_eff, sku_sources=srcs, rows_sink=_sink)
            with _state_lock:
                global _last_backtest_rows, _last_backtest_combos
                if not _bt_stamp_still_valid(stamp):
                    # The products it measured were set aside or replaced while it ran.
                    # Saving it now would file store A's tiers under store B.
                    raise _StaleBacktest()
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
        except _StaleBacktest:
            with _bt_job_lock:
                _bt_job.update({"status": "done", "finishedAt": time.time(), "error": None,
                                "summary": {"discarded": "the loaded products changed while it ran"}})
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


# ─── Holidays ────────────────────────────────────────────────────────────────
# Which promotional periods the store cares about. Saved to its own file, so it survives
# restarts. Saving refits every product in the background (a minute or two on a big
# catalogue); GET reports progress so the page can say so.
_holiday_refit = {"status": "idle", "startedAt": None, "finishedAt": None, "error": None}
_holiday_refit_lock = threading.Lock()


def _holiday_catalog_view(settings):
    t = today().date()
    out = []
    for key, name, group, rule, (before, after), desc in _HOL.CATALOG:
        wins = _HOL.windows_for(key, t, t + datetime.timedelta(days=400), settings)
        nxt = next(((s_, e_) for s_, e_ in wins if e_ >= t), None)
        row = {"key": key, "name": name, "group": group, "description": desc,
               "next": ({"start": nxt[0].isoformat(), "end": nxt[1].isoformat()} if nxt else None),
               "table": rule[0] == "table"}
        if rule[0] == "table":
            past = _HOL.windows_for(key, datetime.date(2015, 1, 1), t + datetime.timedelta(days=800), settings)
            row["dates"] = [{"start": a.isoformat(), "end": b.isoformat()} for a, b in past]
            row["needsDate"] = nxt is None
        out.append(row)
    return out


@app.get("/api/holidays")
def get_holidays():
    """The calendar every product is checked against, the store's own sales periods and
    added Prime Day dates, and whether a refit after a change is still running."""
    st = _holidays.get()
    return {"settings": {"custom": st.get("custom") or [], "dates": st.get("dates") or {},
                         "ignored": st.get("ignored") or {}},
            "groups": _HOL.GROUPS, "catalog": _holiday_catalog_view(st), "refit": dict(_holiday_refit)}


@app.post("/api/holidays")
def set_holidays(payload: dict = Body(default={})):
    """Store-wide changes: the store's own sales periods and Prime Day dates. Every
    product is refitted in the background. Per-product switches are kept as they are
    unless the payload carries its own `ignored`."""
    before = _holidays.get()
    merged = {"custom": (payload or {}).get("custom", before.get("custom") or []),
              "dates": (payload or {}).get("dates", before.get("dates") or {}),
              "ignored": (payload or {}).get("ignored", before.get("ignored") or {})}
    try:
        clean = _HOL.validate(merged)
    except _HOL.SettingsError as ex:
        raise HTTPException(400, str(ex))
    changed = (json.dumps(_HOL.validate(before), sort_keys=True) != json.dumps(clean, sort_keys=True))
    _holidays.save(clean)
    started = _start_holiday_refit() if (changed and _catalog) else False
    return {"success": True, "settings": clean, "refitting": started, "refit": dict(_holiday_refit)}


# ─── Closed days ─────────────────────────────────────────────────────────────────────
def _catalog_shift() -> int:
    """How far the loaded history was moved (one shift for a whole sheet; 0 for a store)."""
    shifts = [int(e.get("date_shift_days") or 0) for e in list(_catalog.values())]
    return max(set(shifts), key=shifts.count) if shifts else 0


def _closed_view() -> dict:
    st = _closed_settings()
    t = today().date()
    sh = datetime.timedelta(days=_catalog_shift())
    # Days nothing sold anywhere in the store while it normally sells, on real dates. Only
    # ever offered: a quiet day is not assumed to be a closed one.
    try:
        seen = _store_closed_days()
    except Exception:                                   # noqa: BLE001
        seen = frozenset()
    marked = _CD.expand(st, datetime.date(1990, 1, 1), datetime.date(t.year + 3, 12, 31))
    sugg = sorted(d - sh for d in seen if (d - sh) < t and (d - sh) not in marked)
    first = last = None
    for e in list(_catalog.values()):
        f = e.get("df")
        if hasattr(f, "columns") and len(f):
            a_ = pd.to_datetime(f["ds"]).min().date() - datetime.timedelta(days=int(e.get("date_shift_days") or 0))
            b_ = pd.to_datetime(f["ds"]).max().date() - datetime.timedelta(days=int(e.get("date_shift_days") or 0))
            first = a_ if first is None or a_ < first else first
            last = b_ if last is None or b_ > last else last
    # A holiday the store was shut on in every year of its history is offered as "every year".
    hints = []
    if first and last:
        for k, name in _CD.YEARLY:
            if k in st["yearly"]:
                continue
            days = [_CD._day_of(k, y) for y in range(first.year, last.year + 1)]
            days = [d for d in days if d and first <= d <= last]
            if len(days) >= 2 and all((d in set(sugg)) or (d in marked) for d in days):
                hints.append(k)
    return {"settings": st,
            "yearlyOptions": [{"key": k, "name": n, "next": next((d.isoformat() for d in (
                _CD._day_of(k, y) for y in (t.year, t.year + 1)) if d and d >= t), None)}
                for k, n in _CD.YEARLY],
            # Each yearly closure's date in every year the calendar can show, so the page
            # can draw a switched-on closure without asking again.
            "yearlyDates": {k: [d.isoformat() for d in (
                _CD._day_of(k, y) for y in range(min(first.year if first else t.year, t.year - 5), t.year + 4))
                if d] for k, _ in _CD.YEARLY},
            "suggestions": [d.isoformat() for d in sugg], "yearlyHints": hints,
            "history": {"first": first.isoformat() if first else None,
                        "last": last.isoformat() if last else None},
            "timezone": _zone_view(),
            "refit": dict(_holiday_refit)}


def _zone_view() -> dict:
    """The store's clock, for the Closed days panel and the page's own "today"."""
    zone, source = _resolve_store_zone()
    z = {}
    try:
        z = _closed.get_zone(_current_slot())
    except Exception:                                   # noqa: BLE001
        pass
    return {"zone": zone, "source": source, "detected": z.get("detected"),
            "detectedFrom": z.get("detectedFrom"), "default": _CLOCK.DEFAULT_ZONE,
            "today": _CLOCK.today().isoformat()}


@app.get("/api/store-clock")
def store_clock_view():
    """The store's time zone and today's date on its clock. The page reads its dates from
    this rather than from the browser's own clock, so the two can't be a day apart."""
    return _zone_view()


@app.get("/api/closed-days")
def get_closed_days():
    """The days the store is closed (single dates and yearly holidays), the days that look
    closed in the sales history but aren't marked, and whether a refit is running."""
    return _closed_view()


@app.post("/api/closed-days")
def set_closed_days(payload: dict = Body(default={})):
    """Body: { dates: [YYYY-MM-DD], yearly: [holiday keys] }. Replaces the calendar for
    what's loaded now, then refits every product in the background: past closed days
    leave every history the forecast learns from, and closed days ahead are forecast at 0."""
    slot = _current_slot()
    payload = dict(payload or {})
    # The store's time zone rides along: { timezone: "America/Chicago" } picks one, null
    # goes back to what the store reports (or Eastern). Absent leaves it alone.
    zone_changed = False
    if "timezone" in payload:
        tz_new = payload.pop("timezone")
        if tz_new not in (None, "") and not _CLOCK.valid(tz_new):
            raise HTTPException(400, f"'{tz_new}' isn't a time zone.")
        was = _APP_TZ
        _closed.set_zone(slot, tz_new or None)
        zone_changed = _apply_store_zone() != was
        if not any(k in payload for k in ("dates", "yearly")):
            payload = dict(_closed.get(slot))
    before = _closed.get(slot)
    try:
        clean = _closed.save(slot, payload or {})
    except _CD.SettingsError as ex:
        raise HTTPException(400, str(ex))
    changed = json.dumps(before, sort_keys=True) != json.dumps(clean, sort_keys=True)
    # A new zone can move "today" by a day, which every forecast starts from: refit too.
    started = _start_holiday_refit() if ((changed or zone_changed) and _catalog) else False
    return {**_closed_view(), "refitting": started}


@app.post("/api/skus/{sku_id}/holidays")
def set_sku_holidays(sku_id: str, payload: dict = Body(default={})):
    """Switch periods off (or back on) for one product, e.g. a spike you know was a
    one-off. Only this product is refitted."""
    with _state_lock:
        if sku_id not in _catalog:
            raise HTTPException(404, "SKU not found.")
        st = _holidays.get()
        ign = dict(st.get("ignored") or {})
        ign[str(sku_id)] = list((payload or {}).get("ignored") or [])
        try:
            clean = _HOL.validate({**st, "ignored": ign})
        except _HOL.SettingsError as ex:
            raise HTTPException(400, str(ex))
        _holidays.save(clean)
        _rebuild(today(), only=sku_id)
        if sku_id in _last_rebuild_errors:
            raise HTTPException(400, f"Couldn't refit: {_last_rebuild_errors.pop(sku_id)}")
    return {"success": True, "ignored": clean["ignored"].get(str(sku_id), [])}


@app.get("/api/holidays/report")
def holiday_report_xlsx():
    """Every product against every holiday: this year's stretch and its forecast units, the
    busiest and quietest days, and the same days in past years. One workbook."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    ws = wb.active; ws.title = "By product"
    head = ["SKU", "Product", "Holiday", "Shapes the forecast", "Stretch", "Forecast units", "On the holiday itself",
            "Busiest day", "Quietest day", "Last year (same days)", "Vs last year", "Past years", "Based on"]
    ws.append(head)
    label = {"moves": "Yes", "flat": "No real change", "new": "Not in history yet", "off": "Switched off",
             "merged": "Part of another holiday this time"}
    for sid, e in sorted(_cache().items()):
        rep = e.get("holidays") or {}
        if rep.get("method") != "shape":
            continue
        for r in rep.get("effects") or []:
            f, ly, nx = r.get("forecast") or {}, r.get("lastYear") or {}, r.get("next") or {}
            pk, qt = r.get("peak") or {}, r.get("quiet") or {}
            ws.append([sid, e.get("sku_name") or sid, r["name"], label.get(r["status"], r["status"]),
                       f"{nx.get('start')} to {nx.get('end')}", f.get("units"), f.get("coreUnits"),
                       (f"{pk['date']} ({pk['x']}x)" if pk.get("date") else ""),
                       (f"{qt['date']} ({qt['x']}x)" if qt.get("date") else ""),
                       ly.get("units"), (f"{ly['pct']:+d}%" if ly.get("pct") is not None else ""),
                       "; ".join(f"{y['year']}: {y['units']}" for y in r.get("years") or []), r.get("text") or ""])
    widths = [12, 26, 28, 22, 26, 14, 18, 20, 20, 18, 12, 40, 60]
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="1F2328")
        c.alignment = Alignment(vertical="center")
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    ws.freeze_panes = "A2"
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    fn = f"logitrack_holidays_{today().strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})


def _start_holiday_refit():
    with _holiday_refit_lock:
        if _holiday_refit["status"] == "running":
            _holiday_refit["again"] = True        # picked up when the current one finishes
            return True
        _holiday_refit.update({"status": "running", "startedAt": time.time(), "finishedAt": None,
                               "error": None, "again": False})

    def work():
        while True:
            try:
                with _state_lock:
                    _rebuild(today())
                err = None
            except Exception as ex:                          # noqa: BLE001
                import traceback; traceback.print_exc()
                err = f"{type(ex).__name__}: {ex}"
            with _holiday_refit_lock:
                if _holiday_refit.get("again") and err is None:
                    _holiday_refit["again"] = False
                    continue
                _holiday_refit.update({"status": "error" if err else "done",
                                       "finishedAt": time.time(), "error": err})
                return

    threading.Thread(target=work, daemon=True, name="holiday-refit").start()
    return True


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
                stamp = _bt_stamp()
            # Relatedness still reads the whole catalog; only these products are refitted.
            fresh = BT.run_backtest(df, params["horizon"], params["n_cutoffs"], params["step"],
                                    params["lead"], params["coverage"], params["service_pct"],
                                    params["min_train"], verbose=False, sku_costs=sku_costs,
                                    combos=combos or prev_combos or None, only_skus=skus)
            merged = BT.merge_rows(prev_rows, fresh)
            all_combos = sorted({(int(a), int(b)) for a, b in
                                 zip(merged["leadDays"], merged["coverageDays"])})
            with _state_lock:
                if not _bt_stamp_still_valid(stamp):
                    raise _StaleBacktest()
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
        except _StaleBacktest:
            with _bt_job_lock:
                _bt_job.update({"status": "done", "finishedAt": time.time(), "error": None,
                                "summary": {"discarded": "the loaded products changed while it ran"}})
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
        srcs = catalog_sources()
        stamp = _bt_stamp()
    _sink = []
    # A request that names no pairs or holding rate gets the ones in use, not 14/30 at
    # 25%: the result replaces every product's measured tier, so testing the wrong
    # settings threw away the right measurements.
    _combos = _parse_combos(p.get("combos")) or [tuple(c) for c in _combos_in_use()]
    _lead, _cov = (_combos[0] if _combos else (g("lead", 14), g("coverage", 30)))
    _hold = g("holding", _session_holding_pct)
    res = BT.run_for_api(df, g("horizon", 44), p.get("cutoffs") or "auto", g("step", 28), g("lead", _lead),
                         g("coverage", _cov), _svc(p.get("service", 95)), g("minTrain", 120), _hold,
                         _eff_costs := _effective_sku_costs(_parse_sku_costs(p.get("costs"))),
                         combos=_combos,
                         sku_sources=srcs, rows_sink=_sink)
    with _state_lock:
        global _last_backtest_rows, _last_backtest_combos
        if not _bt_stamp_still_valid(stamp):
            raise HTTPException(409, "The loaded products changed while the test ran, so its "
                                     "result was not kept. Run it again.")
        _last_backtest_rows = _sink[0] if _sink else None
        _last_backtest_combos = [(c["lead"], c["coverage"]) for c in (res.get("combos") or [])]
        _cache_backtest_tiers(res)
        _record_backtest_inputs(_eff_costs, _hold)
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
        _sink = []
        res = BT.recost(_last_backtest, _last_backtest_rows, eff,
                        holding_pct=holding,
                        combos=_last_backtest_combos or None, rows_sink=_sink)
        if _sink:       # the windows now carry the prices and costs they were valued at
            globals()["_last_backtest_rows"] = _sink[0]
        _cache_backtest_tiers(res)
        _record_backtest_inputs(eff, holding)
        _store_backtest_result(res, "recost")
    return {"recosted": True, "costedSkus": (res.get("tierAnalysis") or {}).get("costedSkus"),
            "totalSkus": (res.get("tierAnalysis") or {}).get("totalSkus")}


@app.post("/api/backtest/refresh")
def backtest_refresh(payload: dict = Body(default={})):
    """Kick off a background backtest over the LOADED FLEET. Used both by the Backtest
    tab and by the dashboard when lead time or coverage changes."""
    global _session_holding_pct
    p = payload or {}
    def g(k, d):
        try: return int(p.get(k, d))
        except Exception: return d
    _combos = _parse_combos(p.get("combos")) or [tuple(c) for c in _combos_in_use()]
    _lead, _cov = (_combos[0] if _combos else (14, 30))
    params = {"horizon": g("horizon", 44), "n_cutoffs": p.get("cutoffs") or "auto", "step": g("step", 28),
              "lead": g("lead", _lead), "coverage": g("coverage", _cov),
              "service_pct": _svc(p.get("service", 95)), "min_train": g("minTrain", 120),
              "holding_pct": g("holding", _session_holding_pct), "combos": _combos}
    _session_holding_pct = int(params["holding_pct"])
    started = _run_backtest_job(params, _effective_sku_costs(_parse_sku_costs(p.get("costs"))) or {},
                                p.get("trigger") or "manual")
    snap = _bt_job_snapshot()
    return {"started": started, **snap,
            "message": None if started else "A backtest is already running; this request was ignored."}


def _tier_cache_key(sku: str, lead, coverage) -> str:
    return f"{sku}|{int(lead)}|{int(coverage)}"


# How far a measured tier may be reused when the planning settings have moved.
#
# WHY THERE IS A TOLERANCE AT ALL. The planning lead time is the P80 of that lane's
# completed deliveries, so it MOVES every time a shipment lands — 14, then 17, then 16 —
# and with few deliveries it moves a lot. An exact-match cache key therefore misses almost
# every time a supplier delivers, and the product silently drops from a measured tier to a
# cost-curve estimate because a parcel arrived a day late. That is not caution; it is
# throwing away the better answer over a rounding difference.
#
# WHY 25% IS DEFENSIBLE. The buffer is z x sigma over the lead window, and sigma grows as
# the square root of the lead, so a 25% change in lead moves the buffer about 12%. More to
# the point, the tier CHOICE — which service level is cheapest — is far stabler than the
# buffer magnitude: it turns on the ratio of stockout cost to holding cost, which a
# modest lead change barely touches. A near match is labelled as one, never passed off as
# exact.
TIER_LEAD_TOLERANCE = 0.25
TIER_COVERAGE_TOLERANCE = 0.25


def _within(measured, requested, tolerance) -> bool:
    """Is `requested` close enough to `measured` to reuse its measurement?"""
    if measured is None or requested is None:
        return True
    try:
        m, r = float(measured), float(requested)
    except (TypeError, ValueError):
        return False
    if m <= 0:
        return False
    return abs(r - m) / m <= tolerance


def _tier_lookup(sku_id: str, lead, coverage):
    """The best measured tier for these settings: (record, match) where match is
    'exact', 'near' or None.

    Exact first, then the nearest measurement inside tolerance — nearest by relative lead
    distance, because lead drives the buffer far more than coverage does.
    """
    sid = str(sku_id)
    exact = _backtest_tier_cache.get(_tier_cache_key(sid, lead, coverage))
    if exact:
        return exact, "exact"
    best, best_dist = None, None
    for key, rec in _backtest_tier_cache.items():
        if not key.startswith(f"{sid}|"):
            continue
        if not _within(rec.get("lead"), lead, TIER_LEAD_TOLERANCE):
            continue
        if not _within(rec.get("coverage"), coverage, TIER_COVERAGE_TOLERANCE):
            continue
        try:
            dist = abs(float(lead) - float(rec.get("lead"))) / max(float(rec.get("lead")), 1.0)
        except (TypeError, ValueError):
            dist = 1.0
        if best_dist is None or dist < best_dist:
            best, best_dist = rec, dist
    return (best, "near") if best else (None, None)


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
            out.setdefault(sku, f"it only has {s.get('days')} days of sales history, and testing needs at "
                                f"least {BACKTEST_MIN_TESTABLE_DAYS}.")
    for row in (res.get("tierAnalysis") or {}).get("bySku") or []:
        sku = str(row.get("sku") or "")
        if not sku or sku in out:
            continue
        windows = int(row.get("windows") or 0)
        if not row.get("costKnown"):
            out[sku] = ("no unit cost on file, so a stockout can't be priced against the cost of "
                        "holding stock. Add one on the product page or in the Fleet tab's Costs & fees sheet.")
        elif row.get("lossMaking"):
            out[sku] = ("it sells at or below cost after fees, so more safety stock would only lose money "
                        "faster. That's a pricing issue, not a buffer one.")
        elif windows < BACKTEST_TIER_MIN_WINDOWS:
            out[sku] = (f"it produced only {windows} usable test window"
                        f"{'' if windows == 1 else 's'}, and under {BACKTEST_TIER_MIN_WINDOWS} "
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
        return ("A test is running right now. This switches to a measured figure when it finishes, "
                "usually within a few minutes.")
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
                f"{BACKTEST_MIN_TESTABLE_DAYS} (about {BACKTEST_MIN_TESTABLE_DAYS - days_hist} more days).")
    if job.get("status") == "error":
        return f"The last test failed to finish ({job.get('error')}), so no measured figure is available."
    if not _backtest_tier_cache:
        return ("No test has been run yet. One starts automatically when you upload a sales file, "
                "or you can run it from the Backtest tab.")
    return ("This product wasn't included in the last test run. Upload again or run the "
            "Backtest tab to pick it up.")


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
    with_cost = {str(k): v for k, v in (sku_costs or {}).items() if (v or {}).get("cost") not in (None, "")}
    _backtest_inputs = {
        "holdingPct": (int(holding_pct) if holding_pct is not None else None),
        "costs": {k: float(v.get("cost") or 0.0) for k, v in with_cost.items()},
        "fees": {k: float(v.get("fees") or 0.0) for k, v in with_cost.items()},
        "prices": {str(k): float(v["price"]) for k, v in (sku_costs or {}).items()
                   if (v or {}).get("price") not in (None, "")},
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
    _sync_holiday_rates(res)


def _sync_holiday_rates(res: dict | None) -> None:
    """Hand the store's measured holiday rates (backtest.py → holiday_cover.py) to the
    forecast. No run, or a run without holiday windows: the defaults stand."""
    hc = (res or {}).get("holidayCover") or {}
    _HC.set_rates(hc.get("rates"), (res or {}).get("ranAt"))


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


def _month_band(fut, point, entry):
    """(low, high) for the forecast part of a month card: bands.total_band over the month's
    days still to come. `fut` holds those days' yhat / yhat_lower / yhat_upper."""
    if fut is None or len(fut) == 0 or point <= 0:
        return 0.0, 0.0
    if not {"yhat_lower", "yhat_upper"}.issubset(fut.columns):
        return point, point
    y = fut["yhat"].clip(lower=0).to_numpy(float)
    lo_w = (y - fut["yhat_lower"].clip(lower=0).to_numpy(float)).clip(min=0)
    hi_w = (fut["yhat_upper"].to_numpy(float) - y).clip(min=0)
    st = _BANDS.stats_for(entry)
    return _BANDS.total_band(point, lo_w, hi_w, level_sd=st["levelSd"], ref_mean=st["refMean"],
                             rho=_BANDS.day_correlation(st["oos"], len(fut)),
                             band_pct=FORECAST_BAND_PCT)


def _horizon_prediction(future_fc, horizon_days: int, start=None, end=None, entry=None):
    """Total units expected over the next `horizon_days`, with a band for that TOTAL.

    The point estimate is just the cumulative sum. The band is bands.total_band, the one
    rule for every multi-day range (the month cards use it too): adding up each day's
    interval assumes every day misses fully in the same direction (always "right", says
    nothing), and combining them in quadrature assumes the days' misses are unrelated,
    which caught only ~60% of real weeks against the 80% claimed. total_band measures how
    the days' errors move together, and how far off the level runs, from the product's
    own holdout errors. `entry` is the fitted entry those come from; without it the band
    falls back to quadrature. What it was built from is returned as `bandStats` so the log
    can rebuild it the same way when a week is revised.
    """
    import numpy as _np
    h = int(horizon_days)
    if start is not None:
        # By DATE, not by position. The forecast begins the day after the last recorded
        # sale, which is only the week's first day when the week is sealed on time. Sealed
        # on a Monday, head(7) was Monday..Sunday, graded as Sunday..Saturday.
        s0 = pd.Timestamp(start).normalize()
        s1 = pd.Timestamp(end).normalize() if end is not None else s0 + pd.Timedelta(days=h - 1)
        ds = pd.to_datetime(future_fc["ds"]).dt.normalize()
        seg = future_fc[(ds >= s0) & (ds <= s1)]
    else:
        seg = future_fc.head(h)
    if seg is None or len(seg) == 0:
        return None
    yhat = seg["yhat"].clip(lower=0)
    point = float(yhat.sum())
    days = [d.date().isoformat() for d in pd.to_datetime(seg["ds"])]
    base = {"predicted": point, "countFrom": days[0], "days": len(days)}
    if not {"yhat_lower", "yhat_upper"}.issubset(seg.columns):
        return {**base, "lo": None, "hi": None,
                "daily": [{"d": d, "y": round(float(v), 3), "lo": 0.0, "hi": 0.0}
                          for d, v in zip(days, yhat)]}
    lo_w = (yhat - seg["yhat_lower"].clip(lower=0)).clip(lower=0)
    hi_w = (seg["yhat_upper"] - yhat).clip(lower=0)
    st = _BANDS.stats_for(entry) if entry else {"levelSd": 0.0, "refMean": None, "oos": None}
    bs = {"levelSd": st["levelSd"], "refMean": st["refMean"],
          "rho": round(_BANDS.day_correlation(st["oos"], len(seg)), 4)}
    lo_t, hi_t = _BANDS.total_band(point, lo_w.to_numpy(float), hi_w.to_numpy(float),
                                   level_sd=bs["levelSd"], ref_mean=bs["refMean"], rho=bs["rho"],
                                   band_pct=FORECAST_BAND_PCT)
    return {**base,
            "lo": lo_t,
            "hi": hi_t,
            "bandStats": bs,
            "daily": [{"d": d, "y": round(float(v), 3), "lo": round(float(a), 3), "hi": round(float(b), 3)}
                      for d, v, a, b in zip(days, yhat, lo_w, hi_w)]}


def _local_now():
    """Now, on the store's clock when the app knows it (set from the browser)."""
    if _APP_TZ:
        try:
            return pd.Timestamp.now(tz=_APP_TZ).tz_localize(None)
        except Exception:                                   # noqa: BLE001
            pass
    return pd.Timestamp.now()


def _price_path_now(sid: str, entry: dict, days: list) -> dict:
    """{date: price} for `days` as things stand NOW: the product's saved events plus the
    shelf price the POS reports, by the same rules the forecast uses (scheduled_prices).

    Read from the sources rather than from the cached forecast, so a shelf change the
    hourly reading has seen counts even when that product's refit is still queued."""
    base = entry.get("last_price")
    if not base or not days:
        return {}
    try:
        evs = list((_catalog.get(sid) or {}).get("events") or entry.get("events") or [])
        last = entry.get("df_train")
        first = ((pd.to_datetime(last["ds"]).max() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                 if last is not None and len(last) else str(days[0])[:10])
        pos = _LP.forecast_events(_live.get(sid) if _live_readings_apply(sid) else None,
                                  first, str(days[-1])[:10], recorded_price=base)
        bounds = (entry.get("price_response") or {}).get("bounds")
        return _UP.future_price_path([str(d)[:10] for d in days], pos + evs, base, bounds=bounds)
    except Exception:                                       # noqa: BLE001
        return {}


def _price_path_used(entry: dict, days: list) -> dict:
    """{date: price} as the CACHED forecast assumed it: the events and shelf-price inputs it
    was last built with. Sealing uses this, not _price_path_now: a product whose refit is
    still queued (the hourly reading refits a few at a time) must be sealed at the price
    its forecast really assumed, so the next reconcile sees the change and revises it."""
    base = entry.get("last_price")
    if not base or not days:
        return {}
    try:
        evs = list(entry.get("pos_events") or []) + list(entry.get("events") or [])
        bounds = (entry.get("price_response") or {}).get("bounds")
        return _UP.future_price_path([str(d)[:10] for d in days], evs, base, bounds=bounds)
    except Exception:                                       # noqa: BLE001
        return {}


def _price_effect(entry: dict):
    """f(day, price) -> this product's demand at `price` relative to its recorded price,
    as its own forecast applies price. None when price doesn't move its forecast.

    Two mechanisms, as in _price_response: a measured or borrowed elasticity, or Prophet's
    own price input. For Prophet the model is asked directly, the same day at both prices."""
    pr = entry.get("price_response") or {}
    base = entry.get("last_price")
    if not pr.get("applies") or not base:
        return None
    if pr.get("elasticity") is not None and pr.get("reason") in ("measured", "borrowed"):
        el, kr = float(pr["elasticity"]), pr.get("knownRange")
        return lambda day, p: float(_UP.price_response(el, base, p, kr))
    # Prophet's own price input. Not reached today (build_entry fits Prophet without price
    # and applies the cross-checked reading on top, the branch above), kept for when it is.
    eng = entry.get("engine")
    if pr.get("reason") != "modelled" or eng is None or not hasattr(eng, "_predict"):
        return None
    from forecast_engine import MAX_PRICE_LIFT as _MX, MIN_PRICE_RATIO as _MN
    memo = {}

    def at(p):
        k = round(float(p), 4)
        if k not in memo:
            b = eng._predict(eng.events, price_override=float(p))
            memo[k] = dict(zip(pd.to_datetime(b["ds"]).dt.strftime("%Y-%m-%d"),
                               b["yhat"].clip(lower=0).astype(float)))
        return memo[k]

    def fn(day, p):
        y_p, y_b = at(p).get(day), at(base).get(day)
        if not y_p or not y_b:
            return 1.0
        return float(min(max(y_p / y_b, _MN), _MX))
    return fn


def _fmt_days(days: list) -> str:
    ds = sorted(days)
    a, b = pd.Timestamp(ds[0]), pd.Timestamp(ds[-1])
    return a.strftime("%a") if a == b else f"{a.strftime('%a')}–{b.strftime('%a')}"


def _livelog_reconcile(skus=None, *, trigger: str = "price", now=None) -> dict:
    """Bring open weeks in line with the price each remaining day will now sell at.

    Every sealed, ungraded week remembers the price each of its days was forecast at. When
    that stops being the price the day will sell at (a promotion saved partway through the
    week, a shelf price changed at the till), those days, and only those, are revised: the
    ORIGINAL forecast for the day times this product's own price effect, new price against
    old. Days already sold are never touched. Today, when the change lands partway through
    it, takes the effect only on the share of a normal day's sales still to come.

    Idempotent: once a day carries the new price there is nothing left to find, so running
    it on every trigger (and every tick, to catch anything missed) is safe."""
    t_now = pd.Timestamp(now) if now is not None else _local_now()
    # The log's own clock (UTC) for "is this week already due"; a test's `now` is taken as it.
    _utc_now = (t_now.to_pydatetime().replace(tzinfo=datetime.timezone.utc) if now is not None else None)
    today_s = t_now.strftime("%Y-%m-%d")
    hour = t_now.hour + t_now.minute / 60.0
    snap = _cache()
    wanted = set(str(x) for x in skus) if skus is not None else None
    revised, dirty = [], False
    # Only the loaded store's weeks: another store's (or a paused one's) weeks were made
    # for its own products, and a same-code product loaded now says nothing about them.
    _owner = _log_owner()
    for e in list(_flog.all()):
        if _owner is None or (e.get("store") or _owner) != _owner:
            continue
        sid = e.get("sku")
        if wanted is not None and sid not in wanted:
            continue
        # Never a week already due for grading on the log's own clock: in the evening in
        # the US it is already the next day in UTC, and grading may be under way.
        if _FL.is_scored(e) or _FL.is_due(e, _utc_now) or e.get("dueAt", "") < today_s:
            continue
        daily = e.get("daily") or []
        open_days = [d for d in daily if d.get("d", "") >= today_s and d.get("price") is not None]
        if not open_days:
            continue
        entry = snap.get(sid) or {}
        path = _price_path_now(sid, entry, [d["d"] for d in open_days])
        diff = [d for d in open_days
                if path.get(d["d"]) and abs(path[d["d"]] - float(d["price"])) > 0.005 * max(float(d["price"]), 0.01)]
        if not diff:
            continue
        eff = _price_effect(entry)
        factors, split = {}, None
        for d in diff:
            p_old, p_new = float(d["price"]), float(path[d["d"]])
            r = 1.0
            if eff is not None:
                try:
                    m_old = eff(d["d"], p_old)
                    r = eff(d["d"], p_new) / m_old if m_old > 0 else 1.0
                except Exception:                           # noqa: BLE001
                    r = 1.0
            if d["d"] == today_s:
                start_h = hour
                lv = (_live.get(sid) if _live_readings_apply(sid) else None) or {}
                # When the POS says when its discount began, and today was sealed at the
                # undiscounted shelf price, that start is when the change happened: an
                # earlier day means all of today. (A discount that later got deeper keeps
                # the hour it was noticed: its start time belongs to the first step.)
                _lp = lv.get("listPrice")
                if (trigger == "shelf" and lv.get("discounted") and lv.get("discountFrom")
                        and _lp is not None and abs(p_old - float(_lp)) <= 0.005 * max(float(_lp), 0.01)):
                    try:
                        df_ = pd.Timestamp(lv["discountFrom"])
                        if df_.tzinfo is not None:           # onto the store's clock, naive
                            df_ = df_.tz_convert(_APP_TZ).tz_localize(None) if _APP_TZ else df_.tz_convert(None)
                        if df_.strftime("%Y-%m-%d") < today_s:
                            start_h = 0.0
                        elif (df_.strftime("%Y-%m-%d") == today_s and df_ <= t_now
                              and "T" in str(lv["discountFrom"])):
                            start_h = df_.hour + df_.minute / 60.0
                    except Exception:                       # noqa: BLE001
                        pass
                share, basis = _today_sales.share_after(start_h)
                split = (start_h, share, basis)
                r = (1.0 - share) + share * r
            factors[d["d"]] = (r, p_new, p_old)
        first = diff[0]
        p0, p1 = float(first["price"]), float(path[first["d"]])
        what = ("promotion or price change saved" if trigger == "planned"
                else "shelf price changed" if trigger == "shelf" else "price changed")
        when = ""
        if split is not None:
            hh = int(split[0]); ap = "AM" if hh < 12 else "PM"; h12 = hh % 12 or 12
            when = f", from ~{h12} {ap} {pd.Timestamp(today_s).strftime('%a')}"
        reason = f"{what}: {_fmt_days([d['d'] for d in diff])} ${p0:.2f} → ${p1:.2f}{when}"
        if eff is None:
            reason += " (this product's forecast doesn't respond to price)"
        with _flog._lock:                                   # noqa: SLF001 - same module family
            cur = _flog._entries.get(e["id"])
            if cur is None or _FL.is_scored(cur):
                continue
            # Re-checked under the lock: only days still carrying the price the factor was
            # worked out from. Two triggers at once (the hourly reading and a tick) would
            # otherwise both apply the same change, 1.5x becoming 2.25x.
            _now_p = {d["d"]: d.get("price") for d in (cur.get("daily") or [])}
            ok = {k: (f[0], f[1]) for k, f in factors.items()
                  if _now_p.get(k) is not None and abs(float(_now_p[k]) - f[2]) <= 1e-6}
            if not ok:
                continue
            new = _FL.revise_days(cur, ok, reason)
            if new is not cur:
                _flog._entries[e["id"]] = new
                dirty = True
                if new.get("amended") and new.get("predicted") != cur.get("predicted"):
                    revised.append({"id": e["id"], "reason": reason,
                                    "before": cur.get("predicted"), "after": new.get("predicted"),
                                    "splitBasis": split[2] if split else None})
    if dirty:
        with _flog._lock:                                   # noqa: SLF001
            _flog.save()                                    # once per pass, not once per product
    return {"revised": len(revised), "entries": revised}


# ── Whose record: the live log is kept per store ────────────────────────────────────
# One log file, but every week is filed under the store it was predicted for (its
# connection id), or "sheets" when it was sealed from spreadsheets with no store at all.
# The Live accuracy tab shows the record of what's loaded; a paused store's weeks are kept
# and keep being graded against that store's own sales, but they don't show while your
# spreadsheets are loaded.
LOG_SHEETS = "sheets"


def _log_owner() -> str | None:
    """Whose record goes with what's loaded: the store in use, or None while spreadsheets
    are loaded and a store is saved (nothing of theirs is tracked)."""
    sid = _showing_store_id()
    if sid:
        return sid
    return None if _CONN.list_all() else LOG_SHEETS


def _store_products(owner) -> set:
    """The products a store lists: its loaded catalog while it's in use, else the copy
    set aside when it was paused. Empty for spreadsheets or an unknown store."""
    if not owner or owner == LOG_SHEETS:
        return set()
    try:
        if _showing_store_id() == owner:
            with _state_lock:
                return {str(k) for k in _catalog}
        path = os.path.join(_WS._slot_dir(_WS.store_slot(owner)), "catalog", "manifest.json")
        with open(path) as fh:
            return {str(k) for k in (json.load(fh).get("skus") or {})}
    except Exception:                                        # noqa: BLE001
        return set()


def _claim_legacy_log() -> None:
    """Weeks sealed before the log recorded whose they were. They were only ever sealed
    for the store in use, so they're the store's: the one showing, else the most recently
    used saved store."""
    try:
        if None not in _flog.stores():
            _flog.dedupe()
            return
        sid = _showing_store_id()
        if not sid:
            saved = _CONN.list_all()
            sid = saved[0]["id"] if saved else LOG_SHEETS
        _flog.claim_untagged(sid)
        _flog.dedupe()
    except Exception as exc:                                # noqa: BLE001
        print(f"Live log: could not file older weeks ({exc}).")


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
    origin = _FL.period_origin(cadence=cadence)
    _owner = _log_owner()
    if _owner is None:          # spreadsheets loaded while a store is paused: not tracked
        return {"added": 0, "skipped": 0, "candidates": 0, "reason": "store-paused",
                "summary": _flog.summary()}
    # A snapshot, not the lock: sealing the week must not queue behind a refit that
    # holds _state_lock for minutes — a missed seal is a permanent hole in the record.
    _snap = _cache()
    skus = list(_snap.keys())
    for sid in skus:
        e = _snap.get(sid) or {}
        fc = e.get("future_fc")
        if fc is None or len(fc) == 0:
            skipped.append(sid)
            continue
        name = (_catalog.get(sid) or {}).get("sku_name") or sid
        for h in horizons:
            pred = _horizon_prediction(fc, h, start=origin, entry=e)
            if pred is None:
                skipped.append(sid)
                continue
            # The price each day is forecast at, so a later change of price can be found
            # and the week revised for it (see _livelog_reconcile).
            _pp = _price_path_used(e, [d["d"] for d in pred["daily"]])
            for _d in pred["daily"]:
                _d["price"] = _pp.get(_d["d"])
            rows.append({"sku": sid, "skuName": name, "horizonDays": int(h),
                         "predicted": pred["predicted"], "lo": pred["lo"], "hi": pred["hi"],
                         "countFrom": pred["countFrom"], "daily": pred["daily"],
                         "bandStats": pred.get("bandStats"),
                         # The same constant the chart is drawn to, so what the log grades
                         # and what the user sees can never diverge.
                         "band": FORECAST_BAND_PCT, "model": (e.get("details") or {}).get("route"),
                         "store": _owner})
    res = _flog.snapshot(rows, cadence=cadence, origin=origin)
    return {**res, "skipped": len(skipped), "candidates": len(rows),
            "nextOrigin": _FL.next_origin(cadence=cadence).isoformat(),
            "summary": _flog.summary()}


def _livelog_connection(payload: dict) -> tuple:
    """Which store the clock-driven endpoints should read.

    A tick fired by cron carries no credentials — that is the whole point of saving a
    connection. Resolution order:

      1. whatever the caller named explicitly (a connectionId, or source + creds)
      2. the most recently used SAVED connection, whichever platform it is
      3. environment variables, which is what the ROADMAP's cron setup assumes and what
         every existing deployment is doing today

    Step 3 returns empty creds on purpose: each source's own _cfg() falls back to its env
    vars, so nothing about the current Shopify-by-environment setup changes.
    """
    payload = payload or {}
    if any(payload.get(k) for k in ("connectionId", "connection_id", "source", "shop", "token")):
        try:
            source, creds, cid, _label = _resolve_connection(payload)
            if cid:
                _CONN.touch(cid)
            return source, creds
        except HTTPException:
            pass                                  # fall through to a saved/env connection

    saved = _CONN.list_all()
    if saved:
        full = _CONN.get(saved[0]["id"]) or {}
        _CONN.touch(saved[0]["id"])
        return full.get("source"), (full.get("creds") or {})

    if os.environ.get("SQUARE_ACCESS_TOKEN") and not os.environ.get("SHOPIFY_TOKEN"):
        return "square", {}
    return "shopify", {}


@app.post("/api/livelog/score")
def livelog_score(payload: dict = Body(default={})):
    """Grade everything whose window has closed, using real sales from the connected store.

    Anything not yet due is untouched — the clock decides, not the button.
    """
    _claim_legacy_log()
    due = _flog.due()
    if not due:
        return {"scored": 0, "unavailable": 0, "reason": "nothing-due",
                "summary": _flog.summary()}
    # Each store's weeks are graded against THAT store's sales. It used to read whichever
    # store was used most recently, for every week in the log.
    total = {"scored": 0, "unavailable": 0, "scoredIds": [], "stillWaiting": 0}
    failed, last_source, lookback = False, None, None
    for owner in sorted({e.get("store") for e in due}, key=str):
        if owner == LOG_SHEETS:
            if _CONN.list_all():
                continue        # spreadsheet predictions can't be graded against a store
            source, creds = _livelog_connection(payload)
        elif owner and _CONN.get(owner):
            full = _CONN.get(owner) or {}
            source, creds = full.get("source"), (full.get("creds") or {})
        else:
            continue            # that store was deleted: nothing to grade against
        pending = [e for e in _flog.pending() if e.get("store") == owner]
        try:
            fn = _SRC.actuals_provider(source, pending, creds,
                                       known_skus=_store_products(owner))
        except Exception as exc:
            raise HTTPException(400, f"Could not read the store: {exc}")
        res = _flog.score_due(fn, select=lambda e, o=owner: e.get("store") == o)
        total["scored"] += res["scored"]; total["unavailable"] += res["unavailable"]
        total["scoredIds"] += res["scoredIds"]; total["stillWaiting"] = res["stillWaiting"]
        failed = failed or bool(getattr(fn, "failed", False))
        last_source, lookback = source, getattr(fn, "days", None)
    return {**total, "source": last_source, "sourceFailed": failed,
            "lookbackDays": lookback, "summary": _flog.summary()}


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
    _claim_legacy_log()
    source, creds = _livelog_connection(payload)
    out["source"] = source
    # Read inventory FIRST. Shopify keeps no queryable stock history, so a reading missed
    # now is a day that can never be reconstructed — whereas scoring can always be retried
    # tomorrow. (Square DOES expose historical inventory changes, which means its gap is
    # backfillable later; that doesn't make skipping the reading free, so the order stands.)
    try:
        out["stock"] = _SRC.sample_stock(source, _slog, creds)
    except Exception as exc:
        out["stock"] = {"ok": False, "error": str(exc), "added": 0}
    # New predictions are sealed only while a store in use is what's loaded. Spreadsheets
    # loaded while the store is paused would otherwise be sealed and later graded against
    # the store's real sales, for products it doesn't have. Grading what was already sealed
    # carries on either way.
    _sid = _showing_store_id()
    if _sid and any(c["id"] == _sid for c in _CONN.list_active()):
        try:
            out["snapshot"] = livelog_snapshot({"horizons": payload.get("horizons"),
                                                "cadence": payload.get("cadence")})
        except Exception as exc:
            out["snapshot"] = {"error": str(exc), "added": 0}
    elif _CONN.list_all():
        out["snapshot"] = {"skipped": "store-paused", "added": 0}
    else:
        try:
            out["snapshot"] = livelog_snapshot({"horizons": payload.get("horizons"),
                                                "cadence": payload.get("cadence")})
        except Exception as exc:
            out["snapshot"] = {"error": str(exc), "added": 0}
    # Anything a trigger missed (a refit that failed, a change made while this was down).
    try:
        out["revised"] = _livelog_reconcile()
    except Exception as exc:                                # noqa: BLE001
        out["revised"] = {"error": str(exc), "revised": 0}
    if _flog.due():
        try:
            # Pass the resolved connection through rather than re-resolving from an empty
            # body — otherwise a tick told to read store A could grade against store B.
            out["score"] = livelog_score({"source": source, "creds": creds})
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
            "note": "Applies to sales loaded from here on. Re-upload or re-sync to "
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
        if float(r.get("price_mixed", 0) or 0) >= 1:
            continue                    # a blend of two prices is not a price change
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
    _claim_legacy_log()
    owner = _log_owner()
    mine = _flog.for_store(owner) if owner is not None else []
    # A product that has left the store (deleted, or its SKU changed in the POS) can't be
    # graded: there are no sales under that code to read. Such weeks used to sit as
    # "ready" for ever and count as products in the week. They're listed as gone and
    # left out of the week's figures; weeks graded before it left keep their result.
    listed = _store_products(owner) if owner not in (None, LOG_SHEETS) else None
    gone = ({e["id"] for e in mine if not _FL.is_scored(e) and e["sku"] not in listed}
            if listed else set())
    counted = [e for e in mine if e["id"] not in gone]
    entries = [e for e in mine if e["sku"] == str(sku)] if sku else mine
    entries = list(reversed(entries))[:limit]
    out = []
    for e in entries:
        d = dict(e)
        d["dueInDays"] = _FL.days_until_due(e)
        d["isDue"] = _FL.is_due(e)
        d["gone"] = e["id"] in gone
        out.append(d)
    org = _FL.period_origin()
    # While spreadsheets are loaded and a store is paused, say whose record exists instead
    # of showing the store's weeks as though they were about the spreadsheet.
    paused = None
    if owner is None:
        conns = {c["id"]: c for c in _CONN.list_all()}
        kept = {st: len(_flog.for_store(st)) for st in _flog.stores() if st in conns}
        if kept:
            st = max(kept, key=kept.get)
            paused = {"label": conns[st].get("label") or "Your store", "entries": kept[st],
                      "weeks": len({e["week"] for e in _flog.for_store(st)})}
    return {"entries": out, "weeks": _FL.by_week(counted),
            "showing": "store" if owner not in (None, LOG_SHEETS) else "sheets",
            "tracked": owner is not None, "pausedStore": paused,
            "summary": _FL.summarise(counted), "perSku": _FL.per_sku(counted),
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
    if not sku or not week:
        raise HTTPException(400, "sku and week are required")
    _owner = _log_owner()
    entry = next((e for e in _flog.for_sku(sku) if e.get("origin") == str(week)[:10]
                  and (e.get("store") or _owner) == _owner), None)
    if entry is None:
        raise HTTPException(404, "no entry for that product and week")
    if _FL.is_scored(entry):
        raise HTTPException(409, "that week is already graded")

    # The same path every trigger takes: only days whose price has changed move, each by
    # this product's own price effect on its ORIGINAL forecast. Re-forecasting the rest of
    # the week here would grade a forecast made with the week's first days already known.
    _livelog_reconcile([sku], trigger="planned")
    return {"amended": bool((_flog._entries.get(entry["id"]) or {}).get("amended")),
            "entry": _flog._entries.get(entry["id"])}


@app.post("/api/livelog/reset")
def livelog_reset():
    """Start the record over, for what's loaded only: another store's weeks are kept."""
    _claim_legacy_log()
    owner = _log_owner()
    if owner is not None:
        _flog.purge_store(owner)
    return {"ok": True, "summary": _FL.summarise(_flog.for_store(owner) if owner else [])}

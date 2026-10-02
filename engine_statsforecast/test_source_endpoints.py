"""
test_source_endpoints.py — the API surface for live store connections.

Scope: the PLUMBING, not the forecasting. The heavy siblings (router, engines,
forecast_log, …) are stubbed, and `_ingest` is replaced with a recorder, because what
needs proving here is different:

  · credentials resolve from a saved connection, from an inline body, and from the old
    flat Shopify body that existing frontend bundles still send
  · a token saved once is never handed back to the browser
  · the clock-driven tick picks the right store when nobody is present to name one
  · Shopify's behaviour is untouched by all of it

That last one is the real risk of this change, so it is asserted directly rather than
assumed.

Run:  python test_source_endpoints.py
"""
from __future__ import annotations
import os as _os_lp, tempfile as _tf_lp
# Never write the real live-price store from a test.
_os_lp.environ["LOGITRACK_LIVE_PRICES_PATH"] = _os_lp.path.join(_tf_lp.mkdtemp(), "live_prices.json")

import json
import os
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp()
os.environ["LOGITRACK_CONNECTIONS"] = os.path.join(_TMP, "connections.json")
os.environ["LOGITRACK_PERSIST"] = "0"
# The scheduler's day ledger and the workspace record, in this run's own folder: the test
# drives real sync runs, which otherwise wrote into the LIVE sync_state.json (and failed
# whenever the real nightly sync had already completed today).
os.environ["LOGITRACK_SYNC_STATE"] = os.path.join(_TMP, "sync_state.json")
os.environ["LOGITRACK_WORKSPACES"] = os.path.join(_TMP, "workspaces")
for _k in ("SHOPIFY_SHOP", "SHOPIFY_TOKEN", "SQUARE_ACCESS_TOKEN", "SQUARE_ENVIRONMENT"):
    os.environ.pop(_k, None)

import pandas as pd                                          # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


# ─── Stub the forecasting stack so main.py can be imported at all ────────────

def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _FakeLog:
    def __init__(self, *a, **k):
        pass

    def pending(self):
        return [{"sku": "A", "windowStart": "2026-03-01"}]

    def due(self):
        return []

    def summary(self):
        return {}

    def coverage(self):
        return {}

    _store = {}

    def record(self, levels, **k):
        self.levels = dict(levels or {})
        _FakeLog._store.update({str(k2): float(v) for k2, v in self.levels.items()})
        return {"added": len(self.levels), "collapsed": 0, "skus": len(_FakeLog._store)}

    def latest(self, sku):
        return _FakeLog._store.get(str(sku))

    def latest_all(self):
        return dict(_FakeLog._store)

    def score_due(self, fn):
        return {"scored": 0}

    def purge(self):
        pass


# The connectors' day rollup is REAL here: these tests drive the real Square/Shopify
# connectors, and a stubbed rollup would hand them None. It is pure pandas, no model.
import importlib.util as _ilu
_fe_spec = _ilu.spec_from_file_location("_fe_real", __file__.rsplit("/", 1)[0] + "/forecast_engine.py")
_fe_real = _ilu.module_from_spec(_fe_spec); _fe_spec.loader.exec_module(_fe_real)
# Everything main imports from forecast_engine is available for real (it is pure numpy /
# pandas); only the calls these suites deliberately neutralise are overridden below. A
# hand-listed stub broke every time main imported one more helper.
_fe_public = {k: getattr(_fe_real, k) for k in dir(_fe_real) if not k.startswith("__")}
_stub("forecast_engine", **{**_fe_public, **dict(analyse_price=lambda *a, **k: None,
      daily_price_rollup=_fe_real.daily_price_rollup, price_evidence=lambda *a, **k: {},
      price_bounds=lambda *a, **k: None, carry_listed_price=_fe_real.carry_listed_price,
      classify_demand=lambda *a, **k: None, compute_safe_price_range=lambda *a, **k: None,
      fill_daily_gaps=lambda *a, **k: None, sparse_subtype=lambda *a, **k: None,
      # main now imports this to put dormancy on each entry, which is what decides the
      # "Dead stock" badge. Stubbed False here: these suites test grading and endpoints,
      # not dormancy — test_status_badges drives sc_status with the flag directly.
      is_dormant=lambda *a, **k: False)})
# The router stub carries the REAL threshold values: the grading rule reads them, so a
# stub with invented numbers would test a rule nobody ships.
_stub("router", group_catalog=lambda *a, **k: {}, detect_group_columns=lambda *a, **k: [],
      ESTABLISHED_DAYS=180, ESTABLISHED_SELLING_DAYS=45, ESTABLISHED_SALES=60,
      MIN_HISTORY_DAYS=90, MIN_HISTORY_SALES=30, MIN_HISTORY_SELLING_DAYS=15,
      NEW_DAYS=60, MIN_RELATIVES=2, RELATIVE_MIN_DAYS=180)
_stub("forecast_log", ForecastLog=_FakeLog, WEEKLY="weekly",
      next_origin=lambda **k: __import__("datetime").date(2026, 3, 1),
      DEFAULT_LOG_HORIZONS=[7])
_stub("censoring"), _stub("promos"), _stub("uplift"), _stub("seasonality")
_stub("bundles", normalise_map=lambda x: x or {}, validate=lambda *a, **k: {},
      attribute=lambda rows, m: (rows, {}))
_stub("arrivals")
_stub("catalog_store", enabled=lambda: False, save=lambda *a, **k: False,
      load=lambda *a, **k: (None, {}), clear=lambda: None, path=lambda: _TMP)
# The REAL growth logic — it is the thing under test in test_tiers_survive_growth.
def _bt_parts(catalog):
    out = {}
    for sid, e in (catalog or {}).items():
        df = e.get("df")
        if df is None or not len(df):
            continue
        ds = pd.to_datetime(df["ds"])
        out[str(sid)] = {"rows": int(len(df)), "first": f"{ds.min():%Y-%m-%d}",
                         "last": f"{ds.max():%Y-%m-%d}",
                         "units": round(float(pd.to_numeric(df["y"]).sum()), 3)}
    return out


def _bt_grew(old, new):
    if not old:
        return False
    for sid, was in old.items():
        now = new.get(sid)
        if now is None or now["first"] != was["first"] or now["last"] < was["last"]:
            return False
        if now["rows"] < was["rows"] or now["units"] < was["units"] - 1e-6:
            return False
    return True


_stub("backtest_store", clear=lambda: None, save=lambda *a, **k: None,
      load=lambda *a, **k: None, fingerprint=lambda c: str(sorted(_bt_parts(c).items())),
      fingerprint_parts=_bt_parts, grew_only=_bt_grew)
_stub("exports")
_stub("engines", ProphetEngine=object, GlobalPooledEngine=object,
      IntermittentEngine=object, MovingAverageEngine=object)


# A faithful stand-in for stock_log.sample_from_shopify — including the part that makes it
# reusable: it takes a `fetch` callable and only imports Shopify when given none. The
# registry relies on that, so the stub has to honour it or the test proves nothing.
_shopify_stock_calls = []


def _fake_sample_from_shopify(slog, fetch=None, shop=None, token=None, now=None):
    if fetch is None:                                # the un-injected path: Shopify by import
        _shopify_stock_calls.append({"shop": shop, "token": token, "via": "import"})
        fetch = _fake_shopify_meta
    try:
        meta = fetch(shop=shop, token=token) if (shop or token) else fetch()
    except Exception as exc:                          # swallowed, never raised — see stock_log
        return {"ok": False, "error": str(exc), "added": 0}
    levels = {sku: m["stock"] for sku, m in (meta or {}).items()
              if isinstance(m, dict) and m.get("stock") is not None}
    if not levels:
        return {"ok": True, "added": 0, "note": "store reported no inventory figures"}
    return {"ok": True, **slog.record(levels, now=now)}


_stub("stock_log", StockLog=_FakeLog, sample_from_shopify=_fake_sample_from_shopify)


# live_actuals is genuinely generic already — this stub preserves the contract that
# matters: it takes a `fetch` callable and only reaches for Shopify when given none.
def _fake_actuals_provider(entries, now=None, fetch=None, shop=None, token=None, known_skus=None):
    if fetch is None:
        raise AssertionError("actuals_provider fell back to importing Shopify")
    frame = fetch(days=30)

    def actuals_fn(sku, start, end):
        return 0.0

    actuals_fn.failed = False
    actuals_fn.days = 30
    actuals_fn.knownSkus = int(frame["sku"].nunique()) if frame is not None else 0
    actuals_fn.frame = frame
    return actuals_fn


_stub("live_actuals", shopify_actuals_provider=_fake_actuals_provider,
      window_units=lambda *a, **k: 0.0, lookback_days_for=lambda *a, **k: 30)


# A fake Shopify source, so we can prove the Shopify path is unchanged.
class ShopifyError(Exception):
    pass


_shopify_sales_calls = []


def _fake_shopify_fetch_sales(shop=None, token=None, api_version=None, days=None):
    _shopify_sales_calls.append({"shop": shop, "token": token,
                                 "api_version": api_version, "days": days})
    if not shop or not token:
        raise ShopifyError("Shopify shop name and access token are required.")
    return pd.DataFrame([{"date": "2026-03-01", "sku": "SH-1", "sku_name": "Shopify thing",
                          "units_sold": 2, "price": 9.99, "on_promotion": 0}])


_shopify_meta_calls = []


def _fake_shopify_meta(shop=None, token=None, api_version=None):
    _shopify_meta_calls.append({"shop": shop, "token": token})
    return {"SH-1": {"category": "Widgets", "stock": 7, "cost": 4.0}}


_stub("shopify_source", ShopifyError=ShopifyError, fetch_sales=_fake_shopify_fetch_sales,
      fetch_catalog_meta=_fake_shopify_meta)

# square_source is the REAL module, driven by the fake HTTP session from its own tests.
import square_source as SQ                                    # noqa: E402
from test_square_source import _fake as fake_square           # noqa: E402

_real_request = SQ._request


def _install_fake_square():
    """Point the real Square module at fixture responses instead of the network."""
    session = fake_square()

    def patched(method, path, token, api_version=None, environment="production",
                json_body=None, params=None, retries=6, session=None):
        # Ignore whatever session the caller passed; always use the fixture.
        return _real_request(method, path, token, api_version, environment,
                             json_body, params, retries, session=session_fixture)

    session_fixture = session

    SQ._request = patched
    return session


import main                                                   # noqa: E402

# Replace the ingestion engine with a recorder: this file is about the plumbing that
# reaches it, not about what it does with a frame.
_ingested = []


def _fake_ingest(df_raw, filename, stock_override=None, cost_override=None,
                 append=False, auto_backtest=True, reanchor=True, cost_source="shopify"):
    _ingested.append({"filename": filename, "rows": len(df_raw),
                      "skus": sorted(set(df_raw["sku"])), "stock": stock_override,
                      "cost": cost_override, "reanchor": reanchor,
                      "costSource": cost_source,
                      "columns": list(df_raw.columns)})
    return {"success": True, "filename": filename, "loadedSkus": df_raw["sku"].nunique(),
            "errors": [], "dataQuality": {"rowsIn": len(df_raw)}}


main._ingest = _fake_ingest
SECRET = "EAAAlgTHISMUSTNEVERAPPEAR7Xk2"


def reset_connections():
    p = main._CONN.path()
    if os.path.exists(p):
        os.remove(p)
    _ingested.clear()
    _shopify_sales_calls.clear()
    _shopify_stock_calls.clear()
    _shopify_meta_calls.clear()


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_source_catalogue():
    print("GET /api/sources")
    out = main.list_sources()
    keys = {s["key"] for s in out["sources"]}
    check("both platforms are offered", keys == {"shopify", "square"}, keys)
    square = next(s for s in out["sources"] if s["key"] == "square")
    check("square declares its fields",
          [f["name"] for f in square["fields"]] == ["accessToken", "environment", "locationIds"])
    check("the token field is marked secret",
          square["fields"][0].get("secret") is True)
    check("no credential values are in the catalogue", SECRET not in json.dumps(out))


def test_shopify_path_unchanged():
    print("the Shopify path still behaves exactly as before")
    reset_connections()
    res = main.upload_shopify({"shop": "acme", "token": "shpat_x", "days": 90})
    check("the legacy flat body still works", res["success"] is True)
    check("credentials reached the Shopify source",
          _shopify_sales_calls[-1]["shop"] == "acme" and _shopify_sales_calls[-1]["token"] == "shpat_x")
    check("days is passed through", _shopify_sales_calls[-1]["days"] == 90)
    check("the filename label is unchanged",
          _ingested[-1]["filename"] == "Shopify · acme", _ingested[-1]["filename"])
    check("live data is NOT re-anchored", _ingested[-1]["reanchor"] is False)
    check("stock prefill still arrives", _ingested[-1]["stock"] == {"SH-1": 7})
    check("cost prefill still arrives", _ingested[-1]["cost"] == {"SH-1": 4.0})
    check("costs are reported as available", res["costsAvailable"] is True)
    check("cost provenance is still stamped as shopify",
          _ingested[-1]["costSource"] == "shopify")
    check("nothing was saved without being asked", main._CONN.list_all() == [])

    check("a .myshopify.com suffix is still stripped",
          main._SRC.describe("shopify", {"shop": "acme.myshopify.com"}) == "Shopify · acme")


def test_square_import():
    print("POST /api/upload/source — Square")
    reset_connections()
    _install_fake_square()
    res = main.upload_from_source({
        "source": "square",
        "creds": {"accessToken": SECRET, "environment": "production"},
        "days": 365})
    check("the import succeeds", res["success"] is True)
    check("it is labelled as Square", _ingested[-1]["filename"] == "Square",
          _ingested[-1]["filename"])
    check("square dates are not re-anchored either", _ingested[-1]["reanchor"] is False)
    check("real SKUs arrive", "MUG-12" in _ingested[-1]["skus"])
    check("stock prefill is summed across locations",
          _ingested[-1]["stock"]["MUG-12"] == 17)
    check("costs are correctly reported as unavailable", res["costsAvailable"] is False)
    check("the source is named in the response", res["source"] == "square")
    check("cost provenance would be stamped as square, not shopify",
          _ingested[-1]["costSource"] == "square", _ingested[-1]["costSource"])
    check("ad-hoc line items are reported to the user",
          res["dataQuality"]["source_adHocLineItems"] == 1)
    check("sku-less products are reported to the user",
          res["dataQuality"]["source_skusWithoutSkuField"] == 1)
    check("the category column survives ingestion",
          "category" in _ingested[-1]["columns"])


def test_save_and_reuse():
    print("saving a connection and reusing it")
    reset_connections()
    _install_fake_square()
    res = main.upload_from_source({
        "source": "square", "label": "Cousin's store",
        "creds": {"accessToken": SECRET, "environment": "production"},
        "save": True})
    conn = res["connection"]
    check("a connection comes back", bool(conn and conn["id"]))
    check("the response never carries the token", SECRET not in json.dumps(res),
          "TOKEN LEAKED IN IMPORT RESPONSE")
    check("only a masked hint is returned", conn["secretHint"] == "EAAA••••7Xk2")
    check("the label is kept", conn["label"] == "Cousin's store")

    listed = main.list_connections()
    check("it shows up in the account list", len(listed["connections"]) == 1)
    check("the list never carries the token", SECRET not in json.dumps(listed))

    # The whole point: import again with no credentials at all.
    _install_fake_square()
    again = main.upload_from_source({"connectionId": conn["id"], "days": 30})
    check("a saved connection imports with no token in the request",
          again["success"] is True)
    check("and is labelled from the saved connection",
          _ingested[-1]["filename"] == "Cousin's store", _ingested[-1]["filename"])
    check("last-used is recorded",
          main.list_connections()["connections"][0]["lastUsedAt"] is not None)

    gone = main.delete_connection(conn["id"])
    check("it can be deleted", gone["success"] is True)
    check("and is then really gone", main.list_connections()["connections"] == [])


def test_unknown_connection():
    print("a deleted connection fails loudly, not silently")
    reset_connections()
    try:
        main.upload_from_source({"connectionId": "does-not-exist"})
        check("unknown connection raises", False)
    except Exception as e:
        check("unknown connection says so", "no longer exists" in str(e), str(e))

    try:
        main.upload_from_source({})
        check("an empty body raises", False)
    except Exception as e:
        check("an empty body explains what to do", "pick a saved account" in str(e).lower(), str(e))


def test_connection_test_endpoint():
    print("POST /api/connections/test")
    reset_connections()
    _install_fake_square()
    out = main.test_connection({"source": "square", "creds": {"accessToken": SECRET}})
    check("it reports success", out["ok"] is True)
    check("it returns the locations for the picker",
          {l["name"] for l in out["locations"]} == {"Alpharetta", "Westside", "Closed store"})
    check("locations carry their timezone",
          out["locations"][0]["timezone"] == "America/New_York")
    check("nothing was imported", _ingested == [])
    check("the token is not echoed back", SECRET not in json.dumps(out))

    bad = _install_fake_square()
    bad.routes["GET /v2/locations"] = [({}, 401)]
    try:
        main.test_connection({"source": "square", "creds": {"accessToken": "nope"}})
        check("a bad token raises", False)
    except Exception as e:
        check("a bad token gives a readable reason", "rejected the token" in str(e), str(e))


def test_livelog_picks_the_right_store():
    print("the clock-driven tick, with nobody present")
    reset_connections()

    # 1. Nothing saved, no env vars → Shopify reading its own environment, as today.
    out = main.livelog_tick({})
    check("falls back to Shopify when nothing is configured", out["source"] == "shopify")
    check("and reads inventory with empty creds, so env vars apply",
          len(_shopify_meta_calls) == 1 and _shopify_meta_calls[-1]["shop"] is None)

    # 2. A saved Square connection → the tick uses it without being told.
    _install_fake_square()
    main.save_connection({"source": "square", "label": "Cousin's",
                          "creds": {"accessToken": SECRET}})
    before = len(_shopify_meta_calls)
    out = main.livelog_tick({})
    check("a saved connection is picked up by the tick", out["source"] == "square", out["source"])
    check("and Shopify is not polled instead", len(_shopify_meta_calls) == before)
    check("inventory was actually read", out["stock"]["ok"] is True, out["stock"])
    check("it read real counts", out["stock"]["added"] == 2, out["stock"])

    # 3. An explicit connection wins over the saved default.
    shop_conn = main.save_connection({"source": "shopify", "label": "My shopify",
                                      "creds": {"shop": "acme", "token": "shpat_y"}})
    before = len(_shopify_meta_calls)
    out = main.livelog_tick({"connectionId": shop_conn["id"]})
    check("an explicitly named connection wins", out["source"] == "shopify")
    check("and it is the named one that gets polled",
          _shopify_meta_calls[-1]["shop"] == "acme" and len(_shopify_meta_calls) == before + 1)

    # 4. A store hiccup must not raise out of the tick — forecast scoring runs after it.
    broken = _install_fake_square()
    broken.routes["GET /v2/locations"] = [({}, 500)]
    main.save_connection({"source": "square", "label": "Cousin's again",
                          "creds": {"accessToken": SECRET}})
    out = main.livelog_tick({})
    check("a failing store is swallowed, not raised", out["stock"]["ok"] is False, out["stock"])
    check("and the rest of the tick still ran", out["score"] is not None)

    check("no token leaks through the tick response", SECRET not in json.dumps(out))


def test_env_var_fallback():
    print("environment-variable deployments keep working")
    reset_connections()
    os.environ["SQUARE_ACCESS_TOKEN"] = SECRET
    try:
        source, creds = main._livelog_connection({})
        check("a SQUARE_ACCESS_TOKEN-only deployment resolves to square", source == "square")
        check("and passes empty creds so the source reads its own env vars", creds == {})
        os.environ["SHOPIFY_TOKEN"] = "shpat_env"
        source, _ = main._livelog_connection({})
        check("shopify still wins when both are set (unchanged behaviour)",
              source == "shopify")
    finally:
        os.environ.pop("SQUARE_ACCESS_TOKEN", None)
        os.environ.pop("SHOPIFY_TOKEN", None)


def test_actuals_provider_is_generic():
    print("grading reads the right store")
    reset_connections()
    _install_fake_square()
    fn = main._SRC.actuals_provider("square", [{"sku": "MUG-12", "windowStart": "2026-03-01"}],
                                    {"accessToken": SECRET})
    check("the square adapter produced a real frame", fn.knownSkus >= 2, fn.knownSkus)
    check("it did not fall back to Shopify", len(_shopify_sales_calls) == 0)

    fn2 = main._SRC.actuals_provider("shopify", [{"sku": "SH-1", "windowStart": "2026-03-01"}],
                                     {"shop": "acme", "token": "shpat_z"})
    check("the shopify adapter still works", fn2.knownSkus == 1)
    check("with the right credentials", _shopify_sales_calls[-1]["shop"] == "acme")


def test_stock_is_logged_at_import():
    """Live inventory has to survive a restart, and the only thing that remembers it is
    StockLog — the catalog frame never carries a units_in_stock column for a live sync."""
    print("inventory is filed at import, not just handed to the browser")
    reset_connections()
    _FakeLog._store.clear()
    _install_fake_square()
    res = main.upload_from_source({"source": "square",
                                   "creds": {"accessToken": SECRET, "environment": "production"}})
    check("the import succeeded", res["success"] is True)
    check("Square counts reached the stock log",
          main._slog.latest("MUG-12") == 17, main._slog.latest("MUG-12"))
    check("summed across both locations", main._slog.latest("TOTE-1") == 40)
    check("a product with no count is not invented",
          main._slog.latest("VAR_2") is None)
    check("the same numbers still prefill the dashboard",
          _ingested[-1]["stock"]["MUG-12"] == 17)


def test_stock_provenance():
    print("every stock figure says where it came from")
    reset_connections()
    _FakeLog._store.clear()
    _install_fake_square()
    main.upload_from_source({"source": "square", "creds": {"accessToken": SECRET}})
    # _ingest is stubbed here, so assert on what it was handed plus what the log holds.
    check("counted products are marked live",
          _ingested[-1]["stock"] and "MUG-12" in _ingested[-1]["stock"])
    check("uncounted products are absent rather than defaulted",
          "VAR_2" not in (_ingested[-1]["stock"] or {}))


def test_grade_is_horizon_aware():
    print("the grade is graded against the decision horizon")
    short = main.grade_forecast(120, 500, "smooth", 100, horizon_days=10)
    long_ = main.grade_forecast(120, 500, "smooth", 100, horizon_days=90)
    check("same product, shorter question, higher grade",
          short["status"] == "FORECAST_ELIGIBLE" and long_["status"] != "FORECAST_ELIGIBLE",
          (short["status"], long_["status"]))
    check("CONFLICTING is never emitted",
          "CONFLICTING" not in {short["status"], long_["status"]})
    check("the reason is a code, not prose", short["reason"] == "established")


def test_nightly_sync_end_to_end():
    """The sync has to work with nobody present: no credentials in the request, no browser."""
    print("the nightly sync, unattended")
    reset_connections()
    _FakeLog._store.clear()
    _install_fake_square()

    # Nothing saved yet — it must say so rather than throwing.
    res = main._run_daily_sync()
    check("with no saved connection it reports why",
          res["ok"] is False and res["reason"] == "no-saved-connection", res)

    main.save_connection({"source": "square", "label": "Kiosk",
                          "creds": {"accessToken": SECRET}})
    _install_fake_square()
    # _ingest is stubbed here, so nothing is loaded for a backtest to run on. On the
    # weekly backtest day the sync would (rightly) report that, and this test failed
    # every Monday. The backtest stage has its own tests below.
    _wd = main.SYNC_BACKTEST_WEEKDAY
    main.SYNC_BACKTEST_WEEKDAY = (main.today().weekday() + 3) % 7
    try:
        res = main._run_daily_sync()
    finally:
        main.SYNC_BACKTEST_WEEKDAY = _wd
    check("with a saved connection it syncs", res["ok"] is True, res)
    check("it names the account", res["label"] == "Kiosk")
    check("it re-ingested", _ingested and _ingested[-1]["filename"] == "Kiosk")
    check("live dates are still not re-anchored", _ingested[-1]["reanchor"] is False)
    check("it also refreshed inventory", main._slog.latest("MUG-12") == 17)
    check("no token leaks into the sync record", SECRET not in json.dumps(res))

    st = main.sync_status()
    check("status exposes the schedule", st["at"] and "enabled" in st)
    check("status names the connection it would use", st["connection"]["label"] == "Kiosk")
    check("and never the token", SECRET not in json.dumps(st))


def test_sync_refuses_to_wipe_the_catalog():
    """A partial response at 00:15 with nobody watching is the dangerous case."""
    print("the sync guards against a catalog wipe")
    reset_connections()
    main.save_connection({"source": "square", "label": "Kiosk",
                          "creds": {"accessToken": SECRET}})

    # Pretend the catalog already holds 28 products.
    main._catalog.clear()
    for i in range(28):
        main._catalog[f"SKU{i}"] = {"df": None}
    try:
        _install_fake_square()          # fixtures report only 3 products
        res = main._run_daily_sync()
        check("a drastic shrink is refused",
              res["ok"] is False and res["reason"] == "guard-catalog-shrink", res)
        check("it reports both counts",
              res["existingSkus"] == 28 and res["incomingSkus"] == 3, res)
        check("the existing catalog is untouched", len(main._catalog) == 28)
        check("and it says what to do", "re-import by hand" in res["detail"].lower(), res["detail"])

        # A manual import does NOT set the guard: the user asked and can see the result.
        _install_fake_square()
        manual = main.upload_from_source({"connectionId": main._CONN.list_all()[0]["id"]})
        check("a manual import is not blocked", manual.get("guardBlocked") is not True)
    finally:
        main._catalog.clear()


def test_sync_records_a_store_outage():
    print("an unreachable store is reported, not raised")
    reset_connections()
    main._catalog.clear()
    main.save_connection({"source": "square", "label": "Kiosk",
                          "creds": {"accessToken": SECRET}})
    broken = _install_fake_square()
    broken.routes["POST /v2/orders/search"] = [({}, 500)]
    res = main._run_daily_sync()
    check("the failure is returned, not thrown",
          res["ok"] is False and res["reason"] == "fetch-failed", res)
    check("with the reason attached", "500" in res["detail"] or "error" in res["detail"].lower(),
          res["detail"])

    # And the scheduler records it without dying.
    rec = main._sync.run_now(trigger="schedule")
    check("the scheduler marks the run failed", rec["ok"] is False, rec)
    # NOT last_run_date. That is the most recent COMPLETED day, and a failed run
    # deliberately completes nothing — asserting it here passed only when an unrelated
    # completed day happened to be left in sync_state.json by an earlier run, which made
    # this test's result depend on the state file rather than on the code. The mechanism
    # that actually stops a failure hot-looping is the ledger's per-day cooling-off.
    _day = main._SCHED._now_local().date()
    check("and starts cooling off so it won't hot-loop",
          main._sync.ledger.cooling_off(_day) is True)
    check("the failed attempt is on record", main._sync.ledger.attempts(_day) >= 1)
    check("but the day is NOT marked complete", main._sync.ledger.is_complete(_day) is False)




def _frame(first, days):
    return {"df": pd.DataFrame({"ds": pd.date_range(first, periods=days), "y": [2] * days})}


def test_tolerant_tier_lookup():
    """A P80 lead time moves whenever a shipment lands. An exact-match cache key demoted a
    measured tier to an estimate because a parcel arrived a day late."""
    print("a measured tier survives a drifting lead time")
    main._backtest_tier_cache.clear()
    main._backtest_tier_cache[main._tier_cache_key("A", 14, 30)] = {
        "key": "standard", "lead": 14, "coverage": 30, "windows": 6, "totalCostYr": 100}
    try:
        rec, match = main._tier_lookup("A", 14, 30)
        check("an exact match is exact", match == "exact" and rec["key"] == "standard")
        rec, match = main._tier_lookup("A", 16, 30)
        check("16 days reuses the 14-day measurement", match == "near" and rec is not None)
        rec, match = main._tier_lookup("A", 17, 30)
        check("17 is still inside tolerance", match == "near", match)
        rec, match = main._tier_lookup("A", 30, 30)
        check("30 is too far — no measurement", rec is None, match)
        rec, match = main._tier_lookup("A", 14, 60)
        check("a doubled coverage is too far too", rec is None, match)
        rec, match = main._tier_lookup("B", 14, 30)
        check("another product gets nothing", rec is None)

        check("tolerance is symmetric", main._within(14, 12, 0.25) and main._within(14, 16, 0.25))
        check("and bounded", not main._within(14, 20, 0.25))
        check("a missing measurement is not a mismatch", main._within(None, 14, 0.25))
    finally:
        main._backtest_tier_cache.clear()


def test_tiers_survive_history_growth():
    """The bug that made protection levels alternate night to night."""
    print("measured tiers survive a night of new sales")
    main._catalog.clear()
    main._backtest_tier_cache.clear()
    try:
        main._catalog["A"] = _frame("2026-05-11", 100)
        main._backtest_tier_cache["A|14|30"] = {"key": "standard", "lead": 14,
                                                "coverage": 30, "windows": 6}
        main._backtest_parts = main._BTSTORE.fingerprint_parts(main._catalog)
        main._backtest_fingerprint = main._BTSTORE.fingerprint(main._catalog)

        # One more day of sales — the ordinary nightly case.
        main._catalog["A"] = _frame("2026-05-11", 101)
        grew = main._BTSTORE.grew_only(main._backtest_parts,
                                       main._BTSTORE.fingerprint_parts(main._catalog))
        check("one extra day counts as growth", grew is True)

        # History replaced — a genuine invalidation.
        main._catalog["A"] = _frame("2026-06-01", 101)
        rewritten = main._BTSTORE.grew_only(main._backtest_parts,
                                            main._BTSTORE.fingerprint_parts(main._catalog))
        check("a re-anchored start date is NOT growth", rewritten is False)

        # A product vanishing is not growth either.
        main._catalog.clear()
        check("a measured product disappearing is not growth",
              main._BTSTORE.grew_only(main._backtest_parts,
                                      main._BTSTORE.fingerprint_parts(main._catalog)) is False)
    finally:
        main._catalog.clear()
        main._backtest_tier_cache.clear()
        main._backtest_parts = {}


def test_backtest_auto_run_conditions():
    """The three ways a product falls back to an estimate must each TRIGGER a run."""
    print("the three fallback conditions trigger a run")
    main._catalog.clear()
    main._backtest_tier_cache.clear()
    main._settings_seen.clear()
    try:
        check("a quiet catalog asks for nothing", main._backtest_needed() == [])

        # 1. newly testable
        main._catalog["A"] = _frame("2026-01-01", main.BACKTEST_MIN_TESTABLE_DAYS + 10)
        reasons = {r["reason"] for r in main._backtest_needed()}
        check("a product crossing the testable threshold triggers", "newly-testable" in reasons,
              reasons)

        main._catalog.clear()
        main._catalog["B"] = _frame("2026-01-01", 30)
        check("a product still too short does not",
              "newly-testable" not in {r["reason"] for r in main._backtest_needed()})

        # 2. settings uncovered
        main._catalog.clear()
        main._backtest_tier_cache["C|14|30"] = {"key": "standard", "lead": 14,
                                                "coverage": 30, "windows": 6}
        main._note_settings("C", 14, 30)
        check("planning at the measured settings triggers nothing",
              "settings-uncovered" not in {r["reason"] for r in main._backtest_needed()})
        main._note_settings("C", 45, 30)          # well outside tolerance
        reasons = {r["reason"] for r in main._backtest_needed()}
        check("a lead time no measurement covers triggers", "settings-uncovered" in reasons,
              reasons)
        main._note_settings("C", 16, 30)          # inside tolerance
        check("a small P80 drift does NOT trigger a re-run",
              "settings-uncovered" not in {r["reason"] for r in main._backtest_needed()})

        check("the combos in use are offered to the next run",
              [16, 30] in main._combos_in_use(), main._combos_in_use())
    finally:
        main._catalog.clear()
        main._backtest_tier_cache.clear()
        main._settings_seen.clear()


def test_sync_backtest_stage_is_honest():
    print("the sync stage says what it actually did")
    reset_connections()
    main._catalog.clear()
    main._backtest_tier_cache.clear()
    main._settings_seen.clear()
    main.save_connection({"source": "square", "label": "Kiosk",
                          "creds": {"accessToken": SECRET}})
    try:
        import datetime as _d
        _install_fake_square()
        # A day that is NOT the weekly weekday, with nothing fallen back.
        not_due = _d.date(2026, 9, 22)            # Tuesday; weekday slot is Monday
        while not_due.weekday() == main.SYNC_BACKTEST_WEEKDAY:
            not_due += _d.timedelta(days=1)
        res = main._run_daily_sync(day=not_due)
        check("it runs", res["ok"] is True, res)
        bt = res.get("backtest") or {}
        check("and records the skip as a skip, not a measurement",
              bt.get("status") == "skipped", bt)
        check("with the reason stated", "not due" in (res["stages"]["backtest"]["note"] or ""),
              res["stages"]["backtest"])

        # The weekly weekday.
        _install_fake_square()
        due = _d.date(2026, 9, 21)
        while due.weekday() != main.SYNC_BACKTEST_WEEKDAY:
            due += _d.timedelta(days=1)
        res2 = main._run_daily_sync(day=due)
        check("on the weekly weekday it attempts a measurement",
              (res2.get("backtest") or {}).get("why") == "weekly refresh", res2.get("backtest"))
    finally:
        main._catalog.clear()
        main._backtest_tier_cache.clear()
        main._settings_seen.clear()


def test_save_connection_validation():
    print("saving rubbish is rejected")
    reset_connections()
    try:
        main.save_connection({"source": "etsy", "creds": {}})
        check("an unknown source is rejected", False)
    except Exception as e:
        check("an unknown source names the known ones",
              "shopify" in str(e) and "square" in str(e), str(e))


if __name__ == "__main__":
    for fn in (test_source_catalogue, test_shopify_path_unchanged, test_square_import,
               test_save_and_reuse, test_unknown_connection, test_connection_test_endpoint,
               test_livelog_picks_the_right_store, test_env_var_fallback,
               test_actuals_provider_is_generic, test_stock_is_logged_at_import,
               test_stock_provenance, test_grade_is_horizon_aware,
               test_nightly_sync_end_to_end, test_sync_refuses_to_wipe_the_catalog,
               test_sync_records_a_store_outage, test_tolerant_tier_lookup,
               test_tiers_survive_history_growth, test_backtest_auto_run_conditions,
               test_sync_backtest_stage_is_honest, test_save_connection_validation):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All source-endpoint tests passed.")

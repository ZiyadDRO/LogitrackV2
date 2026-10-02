"""
Holiday cover (holiday_cover.py): the temporary extra buffer for weeks a holiday reshapes.

  1. Rates: a holiday gets its own rate only with enough windows; fewer own years never
     gets a smaller rate; a thin sample is pulled toward the default.
  2. Replay: the backtest re-grades windows with the cover leave-one-year-out, keeps the
     without-cover figures, can run twice on the same rows, and prices the cover as one
     order cycle per holiday.
  3. The order: a product with a Black Friday spike carries cover only while Thanksgiving
     is inside its order window, in proportion to how much of the stretch is in it; the
     cover is inside the safety stock and reported on its own line, and the protection
     level's cost curve never sees it.

Run:  python test_holiday_cover.py
"""
from __future__ import annotations
import os as _os, tempfile as _tf
_tmp = _tf.mkdtemp()
_os.environ.setdefault("LOGITRACK_PERSIST", "0")
_os.environ["LOGITRACK_LIVE_PRICES_PATH"] = _os.path.join(_tmp, "live_prices.json")
_os.environ["LOGITRACK_HOLIDAYS_PATH"] = _os.path.join(_tmp, "holidays.json")

import datetime as dt
import json
import logging
import sys
import warnings

warnings.filterwarnings("ignore")
logging.getLogger("cmdstanpy").disabled = True
logging.getLogger("prophet").setLevel(logging.ERROR)

import numpy as np
import pandas as pd

import holiday_cover as HC

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


Z = {90: 1.2816, 95: 1.6449}


def _frame(seed=0, n_ord=400, n_hol=150, hol_noise=0.45, years=(2023, 2024, 2025), key="christmas_newyear",
           skus=30, own_years=2):
    """Replayed windows: ordinary ones whose buffer is right, holiday ones whose misses are
    bigger than the buffer by hol_noise × holiday units."""
    r = np.random.default_rng(seed)
    rows = []
    for i in range(n_ord):
        b = 10.0
        rows.append({"sku": f"S{i % skus}", "cutoff": "2024-03-01", "fcLead": 100.0,
                     "actLead": 100.0 + r.normal(0, b), "basisLead": b, "holExpo": "",
                     "cov_90": np.nan, "lost_90": np.nan, "cov_95": np.nan, "lost_95": np.nan})
    for i in range(n_hol):
        y = years[i % len(years)]
        H = 80.0
        rows.append({"sku": f"S{i % skus}", "cutoff": f"{y}-12-10", "fcLead": 100.0,
                     "actLead": 100.0 + r.normal(0, np.sqrt(10.0 ** 2 + (hol_noise * H) ** 2)),
                     "basisLead": 10.0, "holExpo": json.dumps({f"{key}|{y}-12-24": [H, own_years]}),
                     "cov_90": np.nan, "lost_90": np.nan, "cov_95": np.nan, "lost_95": np.nan})
    return pd.DataFrame(rows)


# ── 1. Rates ─────────────────────────────────────────────────────────────────────────
def test_rates():
    print("rates")
    w = HC._windows(_frame())
    rates = HC.fit(w)
    check("a holiday with enough windows across enough products gets its own rate",
          "christmas_newyear" in rates["own"], rates["own"].keys())
    own = rates["own"]["christmas_newyear"]["2"]
    check("its rate lands near how wrong it really runs (0.45 of its units)", 0.3 <= own <= 0.6, own)
    few = HC.fit(HC._windows(_frame(n_hol=40)))
    check("one with few windows shares the pooled rate", few["own"] == {}, few["own"])
    check("fewer own years never gets a smaller rate",
          all(rates["shared"][a] >= rates["shared"][b] for a, b in (("1", "2"), ("2", "3"))), rates["shared"])
    calm = HC.fit(HC._windows(_frame(n_hol=12, hol_noise=0.0)))
    check("a handful of calm holiday windows doesn't read as 'no cover needed'",
          calm["shared"]["all"] >= 0.2, calm["shared"])
    check("the rate looked up falls back to the shared one",
          HC.rate_for("easter", 1, rates) == rates["shared"]["1"])
    check("an unknown years count is treated as the least certain", HC.bucket(0) == 1 and HC.bucket(7) == 3)
    s = HC.cover_sigma({"a|2025-01-01": [100, 1], "b|2025-02-01": [100, 1]}, {"shared": {"1": 0.3, "all": 0.3}})
    check("two holidays in a window combine as independent errors", abs(s - np.sqrt(2) * 30) < 1e-6, s)


# ── 2. Replay ────────────────────────────────────────────────────────────────────────
def test_replay():
    print("backtest replay")
    f = _frame()
    out, fitted = HC.apply(f, Z)
    hol = out["holWin"].astype(bool)
    without = out.loc[hol, "cov0_95"].mean()
    with_ = out.loc[hol, "cov_95"].mean()
    ordinary = out.loc[~hol, "cov_95"].mean()
    check("holiday windows miss more often than ordinary ones without cover", without < ordinary - 0.05,
          (without, ordinary))
    check("with the cover they come close to ordinary ones (out of sample)", with_ >= ordinary - 0.04,
          (with_, ordinary))
    check("ordinary windows are untouched", (out.loc[~hol, "cov_95"] == out.loc[~hol, "cov0_95"]).all())
    again, _ = HC.apply(out, Z)
    check("replaying the replayed rows gives the same answer",
          np.allclose(again["cov_95"].to_numpy(float), out["cov_95"].to_numpy(float)))
    check("rates are fitted leave-one-year-out", set(fitted["byYear"]) == {"2023", "2024", "2025"}, fitted["byYear"].keys())
    g = out[out["sku"] == "S0"]
    c = HC.cost_per_year(g, 1.6449, unit_cost=10.0, holding_annual=0.25, coverage=30)
    occ_max = g[g["hcov1"] > 0].groupby("holOcc")["hcov1"].max().mean()
    expect = occ_max * 1.6449 * 10.0 * 0.25 * 30 / 365
    check("cover is priced as one order cycle per holiday, not a year", abs(c - expect) < 1e-6, (c, expect))
    s = HC.summary(out, Z, fitted)
    check("the summary lists each holiday with and without cover",
          s["byHoliday"][0]["key"] == "christmas_newyear" and s["byHoliday"][0]["without"] < s["byHoliday"][0]["with"],
          s["byHoliday"])
    plain, none = HC.apply(f.drop(columns=["holExpo"]), Z)
    check("rows from before the cover existed are left alone", none is None and plain.equals(f.drop(columns=["holExpo"])))


# ── 3. The order ─────────────────────────────────────────────────────────────────────
def _thanksgiving(y):
    d = dt.date(y, 11, 1)
    d += dt.timedelta(days=(3 - d.weekday()) % 7)
    return d + dt.timedelta(days=21)


def test_order():
    print("the order")
    import main as M
    HC.set_rates(None)
    T = M.today().normalize()
    end = T - pd.Timedelta(days=1)
    ds = pd.date_range(end - pd.Timedelta(days=3 * 365 + 40), end)
    tg = {_thanksgiving(y) for y in range(ds[0].year, ds[-1].year + 1)}
    rows = []
    for i in range(4):
        r = np.random.default_rng(10 + i)
        for d in ds:
            lam = 10.0
            for t in tg:
                off = (d.date() - t).days
                if off == 1:
                    lam *= 4.0          # Black Friday
                elif off in (2, 3):
                    lam *= 2.5
                elif off == 0:
                    lam *= 0.5
                elif -6 <= off <= -1:
                    lam *= 1.3
            rows.append({"date": d.strftime("%Y-%m-%d"), "sku": f"TOY{i}", "sku_name": f"TOY{i}",
                         "category": "Toys", "units_sold": int(r.poisson(lam)), "price": 20.0})
    M._ingest(pd.DataFrame(rows), "store", append=False, reanchor=False, auto_backtest=False)
    M._rebuild(M.today(), only="TOY0")

    def fc(cov):
        return M.get_forecast(sku_id="TOY0", stock=100, lead_time_days=14, coverage_days=cov, strategy="balanced",
                              forecast_months=12, units_on_order=0, on_order_eta_days=None, unit_cost=8.0,
                              fees=0.0, protection="standard", tz=None, stock_source="manual", stock_counted_at=None)

    t_next = min(t for t in (_thanksgiving(T.year), _thanksgiving(T.year + 1)) if t > (T + pd.Timedelta(days=14)).date())
    to = lambda d: (pd.Timestamp(d) - T).days - 14       # coverage days that end the window on d
    before = fc(max(1, to(t_next - dt.timedelta(days=10))))
    part = fc(to(t_next + dt.timedelta(days=1)))
    full = fc(to(t_next + dt.timedelta(days=9)))
    moves = [r["key"] for r in ((full.get("holidays") or {}).get("effects") or []) if r.get("status") == "moves"]
    check("the Black Friday spike is a stretch that moves this product", "thanksgiving_week" in moves, moves)
    hb, hp, hf = before.get("holidayCover"), part.get("holidayCover"), full.get("holidayCover")
    check("no cover while Thanksgiving is outside the order window", hb is None, hb)
    check("cover once the whole stretch is inside it", bool(hf) and hf["units"] > 0, hf)
    check("part of the stretch inside the window gets part of the cover",
          bool(hp) and 0 < hp["units"] < hf["units"], (hp, hf))
    check("the cover is inside the safety stock, reported on its own line",
          hf and full["safetyStock"] == hf["standingUnits"] + hf["units"], (full.get("safetyStock"), hf))
    check("it's priced as one order cycle of carrying",
          hf and hf["carryCost"] is not None and abs(hf["carryCost"] - hf["units"] * 8.0 * 0.25 * hf["carryDays"] / 365) < 0.05,
          hf)
    ab, af = (before.get("protection") or {}).get("economics"), (full.get("protection") or {}).get("economics")
    sb = [t.get("safetyUnits") for t in (ab or {}).get("tiers") or []]
    sf = [t.get("safetyUnits") for t in (af or {}).get("tiers") or []]
    check("the protection level's cost curve doesn't see the cover (its buffer prices all year)",
          sb and sf and all(abs(a - b) <= max(2.0, 0.15 * a) for a, b in zip(sb, sf)), (sb, sf))
    check("the cover comes from the default rate until a backtest measures the store's",
          hf and hf["rateSource"] == "default", hf)


if __name__ == "__main__":
    test_rates()
    test_replay()
    test_order()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("All holiday-cover tests passed.")

"""
holiday_cover.py — extra safety stock for the weeks a holiday reshapes.

WHY

The safety buffer is sized from how wrong the forecast is over an ordinary lead window
(lead_window_sigma). Holiday weeks are harder: the stretch's total is the year's biggest
and rests on one to three past occurrences, and its shape is partly borrowed from similar
products. Measured on the five mock stores, a 95% buffer kept holiday lead windows in
stock 84% of the time against 92% for ordinary ones, and the gap was the same outside Q4
(it is the holiday, not the season) and on every engine.

WHAT

Each holiday day of a stretch the forecast reshapes (holiday_shape "moves") carries an
uncertainty h_sd = rate × that day's forecast units. The buffer code (main.py) adds h_sd up
within each holiday over the order window, because a holiday that runs hot runs hot on all
its days, and combines the holidays and the ordinary buffer as independent errors. So the
extra is proportional to the holiday units actually inside the window: half a stretch in
the window gets about half the cover, a stretch outside it gets none, and it moves every
night with the forecast.

The rate is how wrong holiday weeks run, as a share of their forecast units. It is a store
figure, not a product one (a product has one to three past occurrences of each holiday,
too few to measure on its own):

  • A holiday with enough tested windows in the store gets its own rate; every other
    holiday shares one rate measured from all of them pooled.
  • Either is stepped by how many of the product's own years the shape rests on (1, 2,
    3+): fewer years, more uncertainty.

HOW THE RATE IS MEASURED (backtest)

For every replayed lead window that a moving holiday overlaps, the backtest knows the
forecast units on the holiday's days (H), the miss (actual − forecast) and the ordinary
buffer's σ (b). A window is covered at protection z with rate r when
    miss ≤ z × √(b² + (r·H)²).
The rate is the smallest r (in steps of 0.01) that keeps that holiday's windows in stock as
often as ordinary windows are at the 95% level. Calibrating on the outcome directly is
robust to the odd extreme window. The backtest reports it LEAVE-ONE-YEAR-OUT: each year's
holiday windows are graded with a rate measured without that year, so the "with cover"
figure is out of sample.

The cover is a temporary top-up, not part of the standing buffer: it rides in the orders
placed while a holiday is inside the order window and is sold down after. Its carrying
cost is therefore one order cycle's worth per holiday, not a year's.
"""
from __future__ import annotations

import json
import threading

import numpy as np
import pandas as pd

# A holiday gets its own rate once this many replayed windows across this many products
# carry it; below that it shares the pooled rate.
MIN_OWN_WINDOWS = 100
MIN_OWN_SKUS = 20
# A (holiday, years) cell is fitted on its own only with this many windows; otherwise the
# holiday's (or the shared) all-years rate stands in.
MIN_CELL_WINDOWS = 60
# Windows used for fitting: the holiday carries at least this share of the window's
# forecast units. A window that only grazes a stretch can't tell how wrong the holiday
# runs; it's still given the cover when replayed.
FIT_MIN_SHARE = 0.25
# A shared rate measured on few windows is pulled toward the default by this many
# windows' weight: (n × measured + PRIOR_WINDOWS × default) / (n + PRIOR_WINDOWS). A store's
# backtest replays far fewer holiday windows than it has holidays, and a handful of them,
# all covered, would otherwise read as "holidays need no cover".
PRIOR_WINDOWS = 60
# A holiday with fewer replayed weeks than this is listed for analysts but not shown to
# clients on its own: two or three weeks give an in-stock rate of 0% or 100% by chance.
MIN_WEEKS_SHOWN = 5
# The protection level the rate is calibrated at.
FIT_Z = 1.6449
GRID = np.round(np.arange(0.0, 2.0001, 0.01), 2)
MAX_RATE = float(GRID[-1])
YEAR_BUCKETS = (1, 2, 3)

# Used before any backtest has measured the store's own: what the five mock stores
# measured (pooled 0.30; 0.50 on one year of own history, 0.25 on two or three), rounded
# toward more cover.
DEFAULT_RATES = {"shared": {"all": 0.30, "1": 0.50, "2": 0.30, "3": 0.30}, "own": {}}

_lock = threading.Lock()
_state = {"rates": None, "source": "default", "measuredAt": None}


def bucket(years) -> int:
    """Own years behind a shape → 1, 2 or 3 (3+). A shape built only from similar
    products (0 own years) is treated as the least certain."""
    try:
        y = int(years or 0)
    except (TypeError, ValueError):
        y = 0
    return max(1, min(3, y))


def set_rates(rates: dict | None, measured_at=None) -> None:
    with _lock:
        if rates:
            _state.update({"rates": rates, "source": "backtest", "measuredAt": measured_at})
        else:
            _state.update({"rates": None, "source": "default", "measuredAt": None})


def current() -> dict:
    with _lock:
        return {"rates": _state["rates"] or DEFAULT_RATES, "source": _state["source"],
                "measuredAt": _state["measuredAt"]}


def _lookup(rates: dict, key: str, years) -> float:
    b = str(bucket(years))
    own = (rates.get("own") or {}).get(key)
    table = own if own else (rates.get("shared") or {})
    v = table.get(b, table.get("all"))
    if v is None:
        v = (rates.get("shared") or {}).get("all", DEFAULT_RATES["shared"]["all"])
    return float(v)


def rate_for(key: str, years, rates: dict | None = None) -> float:
    return _lookup(rates or current()["rates"], key, years)


def attach(future_fc: pd.DataFrame, A: dict, moving: dict, levels: dict, to_real,
           rates: dict | None = None) -> pd.DataFrame:
    """Mark each forecast day of a moving holiday stretch and give it its uncertainty.

    Adds three columns: `holiday` ("key|anchor", so the buffer adds a stretch's days up
    together), `hs_years` (own years behind its shape) and `h_sd` (rate × the day's
    forecast units; 0 off a holiday). Run after every multiplier, so a closed day (0
    units) carries none and a promoted day carries its lifted units' share."""
    if future_fc is None or not len(future_fc):
        return future_fc
    out = future_fc.copy()
    keys, years, sds = [], [], []
    yhat = pd.to_numeric(out["yhat"], errors="coerce").fillna(0.0).clip(lower=0).to_numpy(float)
    for x, y in zip(out["ds"], yhat):
        g = (A or {}).get(to_real(pd.Timestamp(x).date())) if moving else None
        if g is None or g[0] not in moving:
            keys.append(None); years.append(None); sds.append(0.0)
            continue
        yrs = int(((levels or {}).get(g[0]) or {}).get("years") or 0)
        keys.append(f"{g[0]}|{g[1].isoformat()}")
        years.append(yrs)
        sds.append(rate_for(g[0], yrs, rates) * float(y))
    out["holiday"] = keys
    out["hs_years"] = years
    out["h_sd"] = sds
    return out


def cover_sigma(expo: dict, rates: dict) -> float:
    """σ of the holiday part over a window: each holiday's rate × its units in the
    window, combined across holidays as independent errors. `expo` is
    {"key|anchor": [units, years]}."""
    tot = 0.0
    for k, (units, yrs) in (expo or {}).items():
        tot += (_lookup(rates, str(k).split("|")[0], yrs) * float(units)) ** 2
    return float(np.sqrt(tot))


# ── backtest side ─────────────────────────────────────────────────────────────────────
def _expo(cell) -> dict:
    if isinstance(cell, dict):
        return cell
    if not isinstance(cell, str) or not cell:
        return {}
    try:
        v = json.loads(cell)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _windows(frame: pd.DataFrame) -> pd.DataFrame | None:
    """One row per buffer-testable window with what the fit needs."""
    need = {"fcLead", "actLead", "basisLead", "holExpo"}
    if frame is None or frame.empty or not need.issubset(frame.columns):
        return None
    w = frame[["sku", "cutoff", "fcLead", "actLead", "basisLead", "holExpo"]].copy()
    w = w[pd.to_numeric(w["basisLead"], errors="coerce") > 0]
    if w.empty:
        return None
    ex = w["holExpo"].map(_expo)
    w["miss"] = pd.to_numeric(w["actLead"], errors="coerce") - pd.to_numeric(w["fcLead"], errors="coerce")
    w["b"] = pd.to_numeric(w["basisLead"], errors="coerce")
    main = ex.map(lambda e: max(e.items(), key=lambda kv: float(kv[1][0]))[0] if e else None)
    w["key"] = main.map(lambda k: str(k).split("|")[0] if k else None)
    w["occ"] = main
    w["year"] = main.map(lambda k: int(str(k).split("|")[1][:4]) if k and "|" in str(k) else None)
    w["H"] = [float(e[k][0]) if k else 0.0 for e, k in zip(ex, main)]
    w["yb"] = [bucket(e[k][1]) if k else None for e, k in zip(ex, main)]
    fc = pd.to_numeric(w["fcLead"], errors="coerce").fillna(0.0)
    w["share"] = np.where(fc > 0, w["H"] / fc.where(fc > 0, 1.0), 0.0)
    w["expo"] = ex
    return w.dropna(subset=["miss", "b"])


def _fit_one(g: pd.DataFrame, target: float) -> float:
    m, b, H = g["miss"].to_numpy(float), g["b"].to_numpy(float), g["H"].to_numpy(float)
    for r in GRID:
        if float(np.mean(m <= FIT_Z * np.sqrt(b ** 2 + (r * H) ** 2))) >= target:
            return float(r)
    return MAX_RATE


def _monotone(table: dict) -> dict:
    """Fewer own years never gets a smaller rate than more: a noisy 3-year cell above
    the 2-year one is lifted, so the step follows the evidence's direction."""
    out = dict(table)
    nxt = None
    for b in ("3", "2", "1"):
        if b in out and out[b] is not None:
            if nxt is not None and out[b] < nxt:
                out[b] = nxt
            nxt = out[b]
    return out


def _fit_table(g: pd.DataFrame, target: float, fallback: dict | None, prior: dict | None = None) -> dict:
    """Rates for all years together and per own-years bucket. With `prior`, each is
    pulled toward the prior's by how few windows it rests on (see PRIOR_WINDOWS)."""
    def shrink(r, n, cell):
        if not prior:
            return r
        p = float(prior.get(cell, prior.get("all")))
        return round((n * r + PRIOR_WINDOWS * p) / (n + PRIOR_WINDOWS), 2)

    t = {"all": shrink(_fit_one(g, target), len(g), "all")}
    for b in YEAR_BUCKETS:
        gb = g[g["yb"] == b]
        if len(gb) >= MIN_CELL_WINDOWS or (prior and len(gb)):
            t[str(b)] = shrink(_fit_one(gb, target), len(gb), str(b))
        elif fallback and str(b) in fallback:
            t[str(b)] = fallback[str(b)]
        elif prior:
            t[str(b)] = float(prior.get(str(b), t["all"]))
        else:
            t[str(b)] = t["all"]
    return _monotone(t)


def fit(w: pd.DataFrame) -> dict | None:
    """Store rates from replayed windows (see module docstring). None when there are
    no holiday windows to learn from."""
    if w is None or w.empty:
        return None
    ordinary = w[w["key"].isna()]
    hol = w[w["key"].notna() & (w["share"] >= FIT_MIN_SHARE)]
    if ordinary.empty or hol.empty:
        return None
    target = float(np.mean(ordinary["miss"].to_numpy(float) <= FIT_Z * ordinary["b"].to_numpy(float)))
    shared = _fit_table(hol, target, None, prior=DEFAULT_RATES["shared"])
    own, meta = {}, {}
    for k, g in hol.groupby("key"):
        enough = len(g) >= MIN_OWN_WINDOWS and g["sku"].nunique() >= MIN_OWN_SKUS
        meta[k] = {"windows": int(len(g)), "skus": int(g["sku"].nunique()), "own": bool(enough)}
        if enough:
            own[k] = _fit_table(g, target, shared)
    return {"shared": shared, "own": own, "target": round(target, 4), "holidays": meta,
            "windows": int(len(hol))}


def apply(frame: pd.DataFrame, z_by_pct: dict) -> tuple[pd.DataFrame, dict | None]:
    """Replay every window with the holiday cover the app would have carried.

    Rates are fitted LEAVE-ONE-YEAR-OUT: a window of a holiday in year Y is covered with
    rates measured on the other years' windows only. Rewrites cov_/lost_ for every tier
    (from the stored forecast, actual and buffer, so it can run again on the same rows)
    and adds:
      cov0_/lost0_  the same without holiday cover, for the before/after comparison
      hcov1         cover at z = 1 (units); at a tier it is z × hcov1
      holOcc        the holiday occurrence the window mostly carries ("key|anchor")
    Returns (frame, {"rates": rates fitted on every year, "folds": {...}}) or the frame
    untouched and None when it carries no holiday information."""
    w = _windows(frame)
    if w is None:
        return frame, None
    full = fit(w)
    if full is None:
        return frame, None
    years = sorted({int(y) for y in w["year"].dropna()})
    by_year = {}
    for y in years:
        r = fit(w[w["year"] != y])
        by_year[y] = r or full
    out = frame.copy()
    hcov1 = pd.Series(0.0, index=out.index)
    occ = pd.Series([None] * len(out), index=out.index, dtype=object)
    for idx, row in w.iterrows():
        if not row["expo"]:
            continue
        y = row["year"]
        rates = by_year.get(int(y)) if y is not None and pd.notna(y) else full
        h = cover_sigma(row["expo"], rates or full)
        hcov1.at[idx] = float(np.sqrt(row["b"] ** 2 + h ** 2) - row["b"])
        occ.at[idx] = row["occ"]
    out["hcov1"] = hcov1
    out["holOcc"] = occ
    out["holWin"] = occ.notna()
    f = pd.to_numeric(out.get("fcLead"), errors="coerce")
    a = pd.to_numeric(out.get("actLead"), errors="coerce")
    b = pd.to_numeric(out.get("basisLead"), errors="coerce")
    testable = f.notna() & a.notna() & b.notna()
    for pct, z in z_by_pct.items():
        cc, lc = f"cov_{pct}", f"lost_{pct}"
        if cc not in out.columns:
            continue
        base_line = f + z * b
        with_line = f + z * (b + hcov1)
        out[f"cov0_{pct}"] = np.where(testable, (a <= base_line).astype(float), out[cc])
        out[f"lost0_{pct}"] = np.where(testable, (a - base_line).clip(lower=0), out[lc])
        out[cc] = np.where(testable, (a <= with_line).astype(float), out[cc])
        out[lc] = np.where(testable, (a - with_line).clip(lower=0), out[lc])
    return out, {"rates": full, "byYear": {str(k): v for k, v in by_year.items()}}


def cost_per_year(g: pd.DataFrame, z: float, unit_cost: float, holding_annual: float,
                  coverage: int) -> float:
    """Yearly carrying cost of one product's holiday cover at protection z.

    The cover rides in the orders placed while a holiday is in the order window and is
    sold down over the cycle after, so each holiday costs one order cycle of carrying
    (coverage days) on its full cover. The full cover is the largest any replayed window
    of that occurrence held; occurrences of a holiday are averaged, and the holidays
    added up."""
    if "hcov1" not in g.columns or "holOcc" not in g.columns:
        return 0.0
    h = g[pd.to_numeric(g["hcov1"], errors="coerce") > 0]
    if h.empty or unit_cost <= 0:
        return 0.0
    occ_max = h.groupby("holOcc")["hcov1"].max()
    per_key = occ_max.groupby(occ_max.index.map(lambda k: str(k).split("|")[0])).mean()
    units = float(per_key.sum()) * float(z)
    return units * unit_cost * holding_annual * max(int(coverage), 1) / 365.0


def summary(frame: pd.DataFrame, z_by_pct: dict, fitted: dict | None) -> dict | None:
    """What the Backtest tab shows about holiday weeks: in stock with and without the
    cover (at each tier, and per holiday at the 95% level), and the rates in use."""
    if fitted is None or frame is None or frame.empty or "hcov1" not in frame.columns:
        return None
    is_h = frame["holWin"].fillna(False).astype(bool) if "holWin" in frame.columns else \
        pd.to_numeric(frame["hcov1"], errors="coerce").fillna(0) > 0
    key = frame["holOcc"].map(lambda k: str(k).split("|")[0] if isinstance(k, str) else None)

    def rate(col, mask):
        v = pd.to_numeric(frame.loc[mask, col], errors="coerce").dropna() if col in frame.columns else pd.Series(dtype=float)
        return round(float(v.mean()) * 100, 1) if len(v) else None

    tiers = []
    for pct in sorted(z_by_pct):
        if f"cov_{pct}" not in frame.columns:
            continue
        tiers.append({"tier": pct,
                      "holidayWith": rate(f"cov_{pct}", is_h), "holidayWithout": rate(f"cov0_{pct}", is_h),
                      "ordinary": rate(f"cov_{pct}", ~is_h)})
    from holiday_shape import name_of
    rates = fitted["rates"]
    meta = rates.get("holidays") or {}
    rows = []
    for k in sorted({x for x in key.dropna()}, key=lambda x: -int((key == x).sum())):
        m = is_h & (key == k)
        own = k in (rates.get("own") or {})
        table = (rates.get("own") or {}).get(k) or rates.get("shared") or {}
        z95 = z_by_pct.get(95, FIT_Z)
        cover = frame.loc[m].groupby("holOcc")["hcov1"].max() * z95 if m.any() else pd.Series(dtype=float)
        rows.append({"key": k, "name": name_of(k), "windows": int(m.sum()),
                     "skus": int(frame.loc[m, "sku"].nunique()),
                     "rateSource": "own" if own else "shared",
                     "rates": {b: table.get(b) for b in ("1", "2", "3")},
                     "fitWindows": (meta.get(k) or {}).get("windows", 0),
                     "with": rate("cov_95", m), "without": rate("cov0_95", m),
                     "byTier": {str(p): {"with": rate(f"cov_{p}", m), "without": rate(f"cov0_{p}", m)}
                                for p in sorted(z_by_pct) if f"cov_{p}" in frame.columns},
                     "avgCoverUnits": round(float(cover.mean()), 1) if len(cover) else None})
    return {"tiers": tiers, "byHoliday": rows, "minWeeksShown": MIN_WEEKS_SHOWN,
            "windows": int(is_h.sum()), "ordinaryWindows": int((~is_h).sum()),
            "target": rates.get("target"),
            "rules": {"minOwnWindows": MIN_OWN_WINDOWS, "minOwnSkus": MIN_OWN_SKUS,
                      "minShare": FIT_MIN_SHARE},
            "shared": rates.get("shared"), "own": rates.get("own"),
            "outOfSample": "leave-one-year-out"}

"""
engines.py — the three forecasting engines the router dispatches to, all behind
one uniform contract so main.py treats them identically.

Contract (after .fit(...)):
    .forecast_df     DataFrame[ds, yhat, yhat_lower, yhat_upper]  (≥366 days past today)
    .residual_std    float   (out-of-sample-ish backtest sigma → safety stock)
    .residual_cv     float
    .model_label     str     (human label for the panel)
    .predict_baseline(regular_price) -> DataFrame   (no-promo view for the scorecard)
    .explain_bits()  -> dict  (engine-specific detail merged into forecastDetails)

All three apply the SAME conformal 80% band (10th/90th percentile of backtest
residuals) so an "80% range" means the same thing regardless of engine.

Heavy libs (prophet, statsforecast) are imported lazily inside fit(), so this
module imports cleanly even where they aren't installed, and tests can stub them.
"""
from __future__ import annotations
import os
import numpy as np
import pandas as pd
import trend as _TR

Z80 = 1.2816
UID = "series"

# TREND_LEVER (new / short-history SKUs): launch-ramp (damped growth) settings for
# the pooled global engine — how a ramping new product's trend is projected.
# Code-only lever — only change it if REAL data shows a consistent bias.
MIN_TREND_DAYS = 21    # need ≥3 weeks of own sales before trusting a growth slope
# Prophet's trend is a straight line: whatever slope the last stretch of history had is
# carried forward for the whole horizon. After a spike (a World Cup, a launch, a viral
# week) that slope is the fall back to normal, and the forecast kept falling for months
# after sales had levelled off. Past the last day the slope is DAMPED: each day keeps
# this share of the previous day's trend change, so a recent rise or fall still counts
# for the next few weeks and then levels off (at 0.97 the whole projected change is
# about a month's worth of the current slope). Env LOGITRACK_PROPHET_DAMP overrides it.
PROPHET_TREND_DAMP = float(os.environ.get("LOGITRACK_PROPHET_DAMP", "0.97"))
# CHECKED AGAINST ITS OWN LAST FOUR WEEKS. Prophet's straight-line trend and fixed-size
# weekly swings are fine for a product selling at a steady rhythm, and badly wrong for
# one whose level just changed (a jersey after a World Cup: forecast 1 a day while it
# sold 3). Before each forecast, recent 4-week stretches are hidden one at a time and
# forecast twice from the history before them: by Prophet and by a recent-sales model
# (_recent_level_forecast: a smoothed recent level times the weekday pattern). The final
# forecast leans on each in proportion to how well it did: where Prophet called those
# weeks well it stays mostly Prophet, where it missed badly it is mostly recent sales.
# Env LOGITRACK_RECENT_CHECK=0 switches it off. (An automatic ETS model was tried first:
# as accurate, but on some days it moved a 30-day forecast by more than 100%.)
# Only for products WITHOUT their own yearly seasonality (under 450 days, the same switch
# as `_yearly_native`). There Prophet is just a trend line plus a weekly pattern, which is
# exactly what goes wrong after a jump or a drop. With a year and a quarter of history
# Prophet also knows the annual pattern and ETS does not: a quiet October that both
# called equally well would otherwise flatten the December peak by half.
RECENT_CHECK_DAYS = int(os.environ.get("LOGITRACK_RECENT_CHECK", "28"))
# Days of its own history before Prophet fits a yearly pattern. With exactly a year every
# month has been seen once, so a product that simply grew all year reads as "December is
# always busy". About 15 months lets the first months repeat and pins the pattern down.
YEARLY_MIN_DAYS = int(os.environ.get("LOGITRACK_YEARLY_MIN_DAYS", "450"))
RECENT_CHECK_MIN_TRAIN = 56
# How many back-to-back 4-week stretches are tested (as many as the history allows, up to
# this). One stretch let a single odd week swing the mix; three average it out, the same
# way the slow-seller engine picks its model.
RECENT_CHECK_WINDOWS = int(os.environ.get("LOGITRACK_RECENT_CHECK_WINDOWS", "3"))
# The recent-sales side of the check reads sales lately AGAINST the season similar products
# share (when they agree on one), so quiet weeks before a busy season aren't taken as "sales
# have dropped". LOGITRACK_SEASON_CHECK=0 switches it off.
# CHECKED AGAINST PAST YEARS. Prophet stores a season as a fixed number of units for each
# time of year, learned across all its years. When a strongly seasonal product's sales jump
# for good (a competitor closes, the store gets busier), the season can't grow with it, so
# Prophet pushes its everyday level up to explain the bigger peak: after the season it stays
# too high, and at the next peak it's too low. It also can't follow a season as sharp as six
# weeks of Mother's Day potting mix. "The same weeks in past years, at this year's pace"
# (seasonality.same_weeks_last_year) has neither problem. For products with a strong season
# (strength >= seasonality.YOY_STRONG) and a year and a half of history, the last 3 four-week
# stretches are hidden one at a time and forecast both ways from the history before them;
# the forecast leans on each in proportion to how well it did, like the 4-week check.
# LOGITRACK_PAST_YEARS_CHECK=0 switches it off.
PAST_YEARS_CHECK = os.environ.get("LOGITRACK_PAST_YEARS_CHECK", "1").strip() not in ("0", "false", "no")
PAST_YEARS_WINDOWS = 3
PAST_YEARS_DAYS = 28
PAST_YEARS_REACH = 364     # past years can say something about the next year; past that, Prophet alone
PAST_YEARS_FADE = 30
# How the yearly and weekly patterns combine with the level. "additive" adds a fixed number
# of units per season; "multiplicative" scales with the level (see the note in fit()).
PROPHET_SEASONALITY_MODE = os.environ.get("LOGITRACK_PROPHET_MODE", "additive")   # multiplicative tested: worse (24% vs 20% on 416 dev forecasts)
SEASON_AWARE_CHECK = os.environ.get("LOGITRACK_SEASON_CHECK", "1").strip() not in ("0", "false", "no")


def _damp_trend(fc, last_date, phi=None):
    """yhat from a Prophet prediction frame with the trend damped after `last_date`."""
    phi = PROPHET_TREND_DAMP if phi is None else phi
    y = fc["yhat"].to_numpy(dtype=float).copy()
    if not (0.0 < phi < 1.0) or "trend" not in fc.columns:
        return y
    fut = (fc["ds"] > last_date).to_numpy()
    past = np.where(~fut)[0]
    if not fut.any() or not len(past):
        return y
    tr = fc["trend"].to_numpy(dtype=float)
    t0 = tr[past[-1]]
    k = (fc["ds"][fut] - fc["ds"].iloc[past[-1]]).dt.days.to_numpy(dtype=float)
    slope = (tr[fut][0] - t0) / max(k[0], 1.0)
    damped = t0 + slope * phi * (1.0 - phi ** k) / (1.0 - phi)
    mult = (fc["multiplicative_terms"].to_numpy(dtype=float)[fut]
            if "multiplicative_terms" in fc.columns else 0.0)
    y[fut] = y[fut] + (damped - tr[fut]) * (1.0 + mult)
    return y


# Days for a day's weight in the recent level to halve. Tested on 36 simulated products
# under 450 days (steady, growing, fading, spike-then-normal, seasonal, promoted): 7 was
# the most accurate but moved more day to day, 14 the steadiest; 10 sits between (weekly
# error 35.0% against Prophet alone at 39.2%, worst day-to-day move 16% against 32%).
RECENT_HALF_LIFE = 10
RECENT_PROFILE_DAYS = 56   # the weekday pattern comes from the last 8 weeks


def _season_factors(dates, mo, strength=1.0):
    """A smooth daily seasonal level from a 12-month index (mid-month anchors, straight lines
    between), pulled toward 1 by `strength`. None when there is no index."""
    if mo is None or strength <= 0:
        return None
    import seasonality as _S
    v = np.asarray(_S.daily_index([str(pd.Timestamp(d).date()) for d in dates], list(map(float, mo)),
                                  min_strength=0.0), dtype=float)
    return 1.0 + float(strength) * (v - 1.0)


def _recent_level_forecast(frame, h, half_life=None, season=None):
    """What "sales lately" says: a recent level times this product's usual weekday pattern,
    held flat. The level is an average of recent days in which each day's weight halves
    every RECENT_HALF_LIFE days, so a single busy or quiet day moves it a few percent, not
    the whole forecast. The weekday pattern comes from the last 8 weeks and scales with
    the level (it can't push a quiet day below zero). Clipped at 0, never negative.

    `season` = (monthly index, strength): "sales lately" is then read against the season,
    so a quiet September before a busy winter isn't carried into the winter. Each past day
    is divided by its seasonal level, the level is worked out from that, and the coming
    days' seasonal level is put back on."""
    hl = float(half_life or RECENT_HALF_LIFE)
    d = frame[["ds", "y"]].sort_values("ds")
    y = np.clip(d["y"].to_numpy(dtype=float), 0.0, None)
    _last = pd.to_datetime(d["ds"]).max() if len(d) else None
    s_hist = s_fut = None
    if season is not None and len(y):
        s_hist = _season_factors(d["ds"], *season)
        s_fut = _season_factors(pd.date_range(_last + pd.Timedelta(days=1), periods=int(h), freq="D"), *season)
        if s_hist is not None and s_fut is not None and np.all(s_hist > 0) and np.all(s_fut > 0):
            y = y / s_hist
        else:
            s_hist = s_fut = None
    wd = pd.to_datetime(d["ds"]).dt.weekday.to_numpy()
    if not len(y):
        return np.zeros(int(h))
    tail = slice(max(0, len(y) - RECENT_PROFILE_DAYS), len(y))
    yt, wt = y[tail], wd[tail]
    mu = float(yt.mean())
    prof = np.ones(7)
    if mu > 0:
        for i in range(7):
            v = yt[wt == i]
            if len(v):
                n = float(v.sum())
                prof[i] = 1.0 + (float(v.mean()) / mu - 1.0) * n / (n + 20.0)
        prof = prof / prof.mean()
    z = y / prof[wd]
    a = 1.0 - 0.5 ** (1.0 / hl)
    lvl = float(z[: min(len(z), 28)].mean())
    for v in z:
        lvl += a * (v - lvl)
    last = pd.to_datetime(d["ds"]).max()
    fwd = (pd.date_range(last + pd.Timedelta(days=1), periods=int(h), freq="D").weekday).to_numpy()
    out = lvl * prof[fwd]
    if s_fut is not None:
        out = out * s_fut
    return np.clip(out, 0.0, None)


# The recent-sales side of the 4-week check (named for what it does in the details).
_ets_forecast = _recent_level_forecast


TREND_DAMP     = 0.90  # daily damping; projected momentum fades over ~2 weeks
TREND_MIN_FRAC = 0.15  # ignore slopes whose 30-day projection is <15% of level (noise)

# ── shared band helpers ──────────────────────────────────────────────────────
def conformal_offsets(resid, residual_std: float) -> tuple[float, float]:
    """(q_lo ≤ 0, q_hi ≥ 0) for an 80% band from signed residual percentiles;
    falls back to ±z·σ when the backtest is too short."""
    if resid is not None and len(resid) >= 8:
        return float(np.percentile(resid, 10)), float(np.percentile(resid, 90))
    return -Z80 * residual_std, Z80 * residual_std


def _band(dates, yhat, q_lo, q_hi) -> pd.DataFrame:
    yhat = np.clip(np.asarray(yhat, dtype=float), 0, None)
    return pd.DataFrame({
        "ds": pd.to_datetime(pd.Index(dates)),
        "yhat": yhat,
        "yhat_lower": np.clip(yhat + q_lo, 0, None),
        "yhat_upper": np.clip(yhat + q_hi, 0, None),
    }).reset_index(drop=True)


def _horizon(today, last_date) -> int:
    return int((pd.Timestamp(today) - pd.Timestamp(last_date)).days) + 366


def _future_dates(last_date, horizon):
    return pd.date_range(pd.Timestamp(last_date) + pd.Timedelta(days=1), periods=horizon, freq="D")


# ── pooled seasonality (shared by GlobalPooledEngine and Prophet's yearly blend) ──
# The pooled monthly index is the annual seasonal SHAPE learned from a SKU's
# established siblings. GlobalPooledEngine uses it as the whole forecast for
# new/thin SKUs; ProphetEngine BLENDS it in for "established but under a year"
# SKUs, which Prophet itself can't give annual seasonality to (see fit()).
POOLED_FADE_START   = 180   # ESTABLISHED_DAYS: full pooled yearly weight at/under this
POOLED_YEARLY_DAYS  = YEARLY_MIN_DAYS   # matches Prophet's yearly_seasonality cutoff; weight → 0 here
BLEND_MIN_RELATIVES = 2     # need a real pool, not a singleton, to borrow an annual shape
BLEND_MIN_COHESION  = 0.35  # siblings must actually agree on a shape (mirror router's gate)


def _frame_days(df):
    return int((df["ds"].max() - df["ds"].min()).days) if df is not None and len(df) > 1 else 0


def pooled_seasonal_indices(related):
    """Weekly (7) and monthly (12) seasonal indices pooled from `related` frames,
    each sibling normalized to its OWN mean so magnitude can't dominate, then the
    pooled curve renormalized to mean 1.0. Returns (wk, mo, resid_norm) where
    resid_norm is the list of normalized in-sample residuals of the pooled fit
    (its spread sizes the pooled band). With no relatives, returns flat indices."""
    wk = np.ones(7); mo = np.ones(12)
    resid_norm = []
    if related:
        wk_acc = {i: [] for i in range(7)}; mo_acc = {i: [] for i in range(12)}
        for r in related:
            # Each sibling's growth out first (trend.py), or a family of growing products
            # hands a newcomer "later months are busier" as if it were a season.
            # "none" under 13 months: the season a newcomer borrows keeps its full size
            # unless growth can be measured cleanly (the same months in two years).
            rr = _TR.detrend(r.dropna(subset=["y"]), short_mode="none")
            mu = float(rr["y"].mean()) or 1.0
            wd = rr["ds"].dt.weekday.to_numpy(); mn = rr["ds"].dt.month.to_numpy() - 1
            norm = rr["y"].to_numpy() / mu
            for i in range(7):
                v = norm[wd == i]
                if len(v): wk_acc[i].append(float(v.mean()))
            for i in range(12):
                v = norm[mn == i]
                if len(v): mo_acc[i].append(float(v.mean()))
        wk = np.array([np.mean(wk_acc[i]) if wk_acc[i] else 1.0 for i in range(7)])
        mo = np.array([np.mean(mo_acc[i]) if mo_acc[i] else 1.0 for i in range(12)])
        wk = wk / (wk.mean() or 1.0); mo = mo / (mo.mean() or 1.0)
        for r in related:
            rr = r.dropna(subset=["y"])
            if not len(rr):
                continue
            mu = float(rr["y"].mean()) or 1.0
            pred = mu * wk[rr["ds"].dt.weekday.to_numpy()] * mo[rr["ds"].dt.month.to_numpy() - 1]
            resid_norm.extend(((rr["y"].to_numpy() - pred) / mu).tolist())
    return wk, mo, resid_norm


def pooled_monthly_cohesion(related, mo=None, min_months=6, baseline_mo=None):
    """How much the siblings AGREE on an annual shape, in [0,1]: the mean Pearson
    correlation between each sibling's own monthly profile and the pooled centroid.
    A pool of flat (non-seasonal) or disagreeing siblings scores near 0, so the
    caller can decline to borrow a shape that isn't really there. Needs ≥2 siblings
    each covering ≥`min_months` of the year to return anything above 0."""
    profs = []
    for r in related:
        rr = _TR.detrend(r.dropna(subset=["y"]), short_mode="none")
        if not len(rr):
            continue
        mu = float(rr["y"].mean()) or 1.0
        mn = rr["ds"].dt.month.to_numpy() - 1
        norm = rr["y"].to_numpy() / mu
        prof = np.full(12, np.nan)
        for i in range(12):
            v = norm[mn == i]
            if len(v): prof[i] = float(v.mean())
        if np.count_nonzero(~np.isnan(prof)) >= min_months:
            profs.append(prof)
    if len(profs) < 2:
        return 0.0
    if baseline_mo is not None and np.all(np.isfinite(np.asarray(baseline_mo, float))):
        # The SPECIFIC scale, as grouping and routing use (router.distinct_corr): the
        # store's average yearly shape out first, so siblings that only share what every
        # product does (a Christmas bump across the whole store) don't count as agreeing.
        bm = np.asarray(baseline_mo, float)
        profs = [p - bm for p in profs]
    # Every PAIR of siblings, as router._cohesion_for_ids does. Each sibling against the
    # pooled average scored unrelated siblings around 1/sqrt(n) (0.5 for four), because
    # the average contains the sibling itself: past the 0.35 bar on nothing at all.
    cors = []
    for i in range(len(profs)):
        for k in range(i + 1, len(profs)):
            a, b = profs[i], profs[k]
            m = ~np.isnan(a) & ~np.isnan(b)
            if m.sum() < min_months:
                continue
            a, b = a[m], b[m]
            if np.std(a) < 1e-9 or np.std(b) < 1e-9:
                cors.append(0.0)      # a flat sibling contributes no seasonal agreement
                continue
            cors.append(float(np.corrcoef(a, b)[0, 1]))
    return float(max(0.0, np.mean(cors))) if cors else 0.0


def pooled_blend_weight(days, n_relatives, cohesion,
                        fade_start=POOLED_FADE_START, yearly_days=POOLED_YEARLY_DAYS,
                        min_relatives=BLEND_MIN_RELATIVES, min_cohesion=BLEND_MIN_COHESION):
    """Weight in [0,1] for blending pooled yearly seasonality into a Prophet forecast.
    Zero unless there's a real, cohesive pool AND Prophet is still short of its own
    yearly cycle. Otherwise fades linearly from 1.0 at `fade_start` days down to 0.0
    at `yearly_days` (so there's no discontinuity when the SKU graduates to Prophet's
    own annual seasonality), scaled by how far cohesion clears the trust floor."""
    if n_relatives < min_relatives or cohesion < min_cohesion or days >= yearly_days:
        return 0.0
    span = max(yearly_days - fade_start, 1)
    hist = float(np.clip((yearly_days - days) / span, 0.0, 1.0))      # 1 → 0 over the band
    coh = float(np.clip((cohesion - min_cohesion) / (1.0 - min_cohesion), 0.0, 1.0))
    coh = 0.5 + 0.5 * coh                                            # ≥0.5 once the gate is passed
    return float(hist * coh)


def apply_pooled_yearly(fc, mo, weight, resid_std_norm=0.0, level=None, after=None):
    """Multiply a forecast's monthly seasonality toward the pooled index `mo`
    (mean-1) by `weight`, on rows after `after` (the last actual date). Since the
    blend is only used when Prophet's own yearly is OFF, this ADDS annual shape
    rather than double-counting it. Bands scale with the center, and are widened in
    quadrature by the pooled residual spread (scaled to `level`, ×weight) to reflect
    that a borrowed shape is an assumption."""
    if weight <= 0 or fc is None or not len(fc):
        return fc
    fc = fc.copy()
    mo = np.asarray(mo, dtype=float)
    ds = pd.to_datetime(fc["ds"])
    mask = np.ones(len(fc), bool) if after is None else (ds > pd.Timestamp(after)).to_numpy()
    factor = np.where(mask, 1.0 + weight * (mo[ds.dt.month.to_numpy() - 1] - 1.0), 1.0)
    center = fc["yhat"].to_numpy(float)
    half_lo = center - fc["yhat_lower"].to_numpy(float)
    half_hi = fc["yhat_upper"].to_numpy(float) - center
    new_center = center * factor
    new_half_lo = half_lo * factor
    new_half_hi = half_hi * factor
    if resid_std_norm and level:
        extra = np.where(mask, weight * float(resid_std_norm) * float(level), 0.0)
        new_half_lo = np.sqrt(new_half_lo ** 2 + extra ** 2)
        new_half_hi = np.sqrt(new_half_hi ** 2 + extra ** 2)
    fc["yhat"] = np.clip(new_center, 0, None)
    fc["yhat_lower"] = np.clip(new_center - new_half_lo, 0, None)
    fc["yhat_upper"] = np.clip(new_center + new_half_hi, 0, None)
    return fc


# ═════════════════════════════════════════════════════════════════════════════
#  PROPHET ENGINE — established, regular-demand SKUs (the workhorse)
# ═════════════════════════════════════════════════════════════════════════════
class ProphetEngine:
    method = "prophet"
    model_label = "Prophet"

    def fit(self, df, today, has_price, has_promo, last_price, events, related=None,
            price_bounds=None, price_range=None, past_years_weight=None, baseline_mo=None, **_):
        self._baseline_mo = baseline_mo
        from prophet import Prophet  # lazy
        # A calibration refit passes the live forecast's past-years weight instead of
        # re-running that check at every holdout (3 more Prophet fits each time).
        self._py_given = past_years_weight
        self.today = pd.Timestamp(today)
        # (lo, hi): scheduled prices are forecast as if clamped into this range. Outside
        # it the price response was never observed, and the regressor is linear, so it
        # would extrapolate without limit. See forecast_engine.price_bounds.
        self.price_bounds = price_bounds
        # (lo, hi) of the prices actually sold at for 30+ days. Inside it the price effect
        # is interpolated; outside it, extrapolated — and marked as less certain.
        self.price_range = price_range
        self.has_price, self.has_promo = has_price, has_promo
        self.last_price, self.events = last_price, (events or [])
        df = df.sort_values("ds").reset_index(drop=True)
        self.last_date = df["ds"].max()
        self.horizon = _horizon(today, self.last_date)

        days = (df["ds"].max() - df["ds"].min()).days if len(df) > 1 else 0
        # Yearly seasonality needs ~one full cycle to estimate, not two. Requiring
        # 720 days left every SKU with 1–2 years of data with NO annual seasonality —
        # which badly over-forecast seasonal products (the backtest caught this).
        # 450 days (~1.25 cycles) is enough to lock the phase with some margin.
        # TREND_LEVER (mature SKUs): changepoint_prior_scale = how hard the forecast
        # follows a recent rise/fall. 0.05 is Prophet's default. LOWER = flatter, less
        # trend-chasing (steadier, less over/under-shoot); HIGHER = more reactive.
        # Code-only lever — only change it if REAL data shows a consistent bias.
        self._yearly_native = days >= YEARLY_MIN_DAYS

        def _build(with_price):
            mm = Prophet(yearly_seasonality=self._yearly_native, weekly_seasonality=days >= 14,
                         daily_seasonality=False, interval_width=0.80,
                         changepoint_prior_scale=0.05, seasonality_mode=PROPHET_SEASONALITY_MODE,
                         # Prophet's own intervals are never used (the band comes from
                         # conformal_offsets), and drawing them was most of every predict.
                         uncertainty_samples=0)
            # Price enters as log(price / today's price), MULTIPLYING the forecast. A 10%
            # price change then moves demand by the same percentage on a quiet Tuesday
            # and a busy Saturday, which is how shoppers respond. As a plain additive
            # regressor it moved every day by the same number of units, so the effect
            # was too small on busy days and too large on quiet ones, and the measured
            # response came out about a quarter too weak.
            if with_price: mm.add_regressor("price", mode="multiplicative", standardize=True)
            if has_promo: mm.add_regressor("on_promotion", standardize=False)
            return mm

        fit_df = df[["ds", "y"]].copy()
        _raw = df["price"].ffill().fillna(last_price or 0) if has_price else None
        self._pref = float(last_price or 0) or (float(_raw.median()) if _raw is not None else 0.0) or 1.0
        if has_price: fit_df["price"] = self._log_price(_raw)
        if has_promo: fit_df["on_promotion"] = df["on_promotion"].fillna(0).clip(0, 1)
        m = _build(has_price)
        m.fit(fit_df)

        # ── Does the fitted price effect point the right way? ─────────────────
        # Prophet fits the price coefficient with NO sign constraint. On sparse data a
        # coincidence — a discount that happened to land in a quiet stretch — becomes
        # "cheaper means fewer sales", and every price event then moves the forecast
        # BACKWARDS: cut the price, demand drops; raise it, demand climbs. Measured on a
        # reproduction of exactly that, a 20% cut took a 30-day forecast from 155 units
        # to 45 and a 20% rise took it to 265.
        #
        # uplift.price_elasticity has refused a positive relationship for a long time
        # ("positive-elasticity-ignored") — but only on the non-Prophet path, which is not
        # the one that applies price. This holds Prophet to the same standard, tested on
        # the thing that matters rather than on an internal coefficient: predict the same
        # window at today's price and 10% above it. If the dearer price sells MORE, the
        # relationship this data taught the model is backwards, so the model is refitted
        # without price. That does not make price effects right — it makes them neutral,
        # which is the honest answer when the data cannot tell us which way they go.
        self._price_inverted = None
        if has_price:
            lp = float(last_price or 0) or float(fit_df["price"].median() or 0)
            if lp > 0:
                probe = m.make_future_dataframe(periods=30, freq="D")
                lo, hi = probe.copy(), probe.copy()
                lo["price"], hi["price"] = lp, lp * 1.10
                if has_promo:
                    lo["on_promotion"] = 0; hi["on_promotion"] = 0
                y_lo = float(m.predict(self._tx(lo)).tail(30)["yhat"].clip(lower=0).sum())
                y_hi = float(m.predict(self._tx(hi)).tail(30)["yhat"].clip(lower=0).sum())
                if y_hi > y_lo + 1e-6:
                    self._price_inverted = {
                        "at": round(lp, 2), "unitsAtPrice": round(y_lo, 1),
                        "unitsAt10pctHigher": round(y_hi, 1),
                        "changePct": round((y_hi - y_lo) / y_lo * 100, 1) if y_lo > 0 else None,
                    }
                    has_price = False
                    self.has_price = False
                    fit_df = fit_df.drop(columns=["price"])
                    m = _build(False)
                    m.fit(fit_df)
        self._model = m
        self._fit_df = fit_df

        # In-sample residuals → conformal band + safety-stock sigma.
        resid = fit_df["y"].to_numpy() - m.predict(fit_df)["yhat"].to_numpy()
        self._blend_setup(related, days, fit_df)
        try:
            self._recent_check(fit_df, _build, self.has_price)
        except Exception:                                   # noqa: BLE001 — never fail a fit over it
            self._ets_w, self._ets_future, self._recent = 0.0, None, None
        try:
            self._past_years_check(fit_df, _build)
        except Exception:                                   # noqa: BLE001
            self._py_w, self._py_future, self._pastyears = 0.0, None, None
        self.residual_std = float(np.std(resid))
        self.residual_cv = self.residual_std / max(float(fit_df["y"].mean()), 1.0)
        self._q_lo, self._q_hi = conformal_offsets(resid, self.residual_std)
        self.forecast_df = self._predict(self.events)
        return self

    def _blend_setup(self, related, days, fit_df):

        # ── Blended pooled yearly seasonality ─────────────────────────────────
        # An "established but under a year" SKU (≈180–450 days) runs on Prophet, but
        # with yearly seasonality OFF it carries NO annual shape of its own. If it has
        # a real, cohesive pool of relatives that already have enough annual history
        # (≥450 days), borrow their pooled monthly index and fade it in by history +
        # cohesion (pooled_blend_weight); _predict() then calls
        # apply_pooled_yearly to nudge the forecast toward that shape and widen the band
        # for the borrowed signal. Weight is 0 (a no-op) once yearly is native, or when
        # there's no mature cohesive pool — so under-450 SKUs never bootstrap annual
        # seasonality from each other.
        self._blend_w = 0.0; self._blend_mo = None; self._blend_cohesion = None
        self._blend_resid_std = 0.0; self._blend_level = None
        # The season the 4-week check reads recent sales against (see _recent_check): the
        # same shape similar products share, at full strength once they agree on it.
        self._check_season = None
        blend_related = [r for r in (related or []) if _frame_days(r) >= POOLED_YEARLY_DAYS]
        self._n_pool_relatives = len(blend_related)
        if not self._yearly_native and blend_related:
            _wk_p, mo_p, resid_norm = pooled_seasonal_indices(blend_related)
            cohesion = pooled_monthly_cohesion(blend_related, mo_p,
                                               baseline_mo=getattr(self, "_baseline_mo", None))
            w = pooled_blend_weight(days, self._n_pool_relatives, cohesion)
            if (SEASON_AWARE_CHECK and self._n_pool_relatives >= BLEND_MIN_RELATIVES
                    and cohesion >= BLEND_MIN_COHESION and float(np.std(mo_p)) > 1e-6):
                self._check_season = (mo_p, 0.5 + 0.5 * float(np.clip(
                    (cohesion - BLEND_MIN_COHESION) / (1.0 - BLEND_MIN_COHESION), 0.0, 1.0)))
            if w > 0 and float(np.std(mo_p)) > 1e-6:
                self._blend_w = w
                self._blend_mo = mo_p
                self._blend_cohesion = cohesion
                self._blend_resid_std = float(np.std(resid_norm)) if resid_norm else 0.0
                self._blend_level = max(float(fit_df["y"].mean()), 1.0)

    def _blend_factor(self, dates):
        """What apply_pooled_yearly multiplies Prophet's forecast by on `dates` (1 = none)."""
        if getattr(self, "_blend_w", 0.0) <= 0 or getattr(self, "_blend_mo", None) is None:
            return np.ones(len(dates))
        mo = np.asarray(self._blend_mo, dtype=float)
        mn = pd.to_datetime(pd.Series(dates)).dt.month.to_numpy() - 1
        return 1.0 + self._blend_w * (mo[mn] - 1.0)

    def _log_price(self, prices):
        return np.log(np.clip(np.asarray(prices, dtype=float), 0.01, None) / float(getattr(self, "_pref", 1.0) or 1.0))

    def _tx(self, frame):
        """A frame of real prices, as the model sees them (log of price / today's price)."""
        if not self.has_price or "price" not in frame.columns:
            return frame
        out = frame.copy()
        out["price"] = self._log_price(out["price"])
        return out

    def _future_exog(self, events, price_override=None, force_no_promo=False):
        f = self._model.make_future_dataframe(periods=self.horizon, freq="D")
        if self.has_price:
            if price_override is not None:
                f["price"] = float(price_override)
            else:
                # Same rules as every other engine: shelf price from price changes, then
                # the deepest overlapping promotion as a discount, then the hard limits.
                from forecast_engine import scheduled_prices
                prices, _ = scheduled_prices(f["ds"].tolist(), events, self.last_price or 0.0,
                                             bounds=getattr(self, "price_bounds", None),
                                             promotions=not force_no_promo)
                f["price"] = prices
            if getattr(self, "price_bounds", None) is not None:
                lo, hi = self.price_bounds
                f["price"] = f["price"].astype(float).clip(lower=float(lo), upper=float(hi))
        if self.has_promo:
            f["on_promotion"] = 0
        return f

    def _yhat(self, frame):
        """Prophet's prediction for `frame` (history + future), with the trend damped past
        the last day of history (see PROPHET_TREND_DAMP). Returns (yhat array, raw frame)."""
        fc = self._model.predict(self._tx(frame))
        return _damp_trend(fc, self.last_date), fc

    def _recent_check(self, fit_df, build, with_price):
        """Hide recent 4-week stretches one at a time, forecast each with Prophet and with
        ETS from only the history before it, and weight ETS by how much better it did over
        all of them (see RECENT_CHECK_DAYS / RECENT_CHECK_WINDOWS)."""
        self._ets_w, self._ets_future, self._recent = 0.0, None, None
        H = RECENT_CHECK_DAYS
        if getattr(self, "_yearly_native", False):
            return
        if H <= 0 or len(fit_df) < H + RECENT_CHECK_MIN_TRAIN or not self.horizon:
            return
        n = len(fit_df)
        # Judged week by week, not day by day: orders cover weeks, and single days of a
        # lumpy seller are so noisy that a model a third too low and one spot on miss by
        # about the same per day.
        miss_p, miss_e, windows = [], [], []
        for k in range(1, RECENT_CHECK_WINDOWS + 1):
            cut = n - k * H
            if cut < RECENT_CHECK_MIN_TRAIN:
                break
            train, test = fit_df.iloc[:cut], fit_df.iloc[cut:cut + H]
            mh = build(with_price)
            mh.fit(train)
            frame = pd.concat([train.tail(1), test]).drop(columns=["y"])
            yp = np.clip(_damp_trend(mh.predict(frame), train["ds"].max())[1:], 0.0, None)
            yp = yp * self._blend_factor(test["ds"].to_numpy())[:len(yp)]
            ye = _ets_forecast(train, H, season=getattr(self, "_check_season", None))[:len(test)]
            act = test["y"].to_numpy(dtype=float)
            wk = np.arange(len(act)) // 7
            wa = np.bincount(wk, act)
            miss_p += list(np.abs(wa - np.bincount(wk, yp)))
            miss_e += list(np.abs(wa - np.bincount(wk, ye)))
            windows.append({"actual": round(float(act.sum()), 1), "prophet": round(float(yp.sum()), 1),
                            "ets": round(float(ye.sum()), 1)})
        if not windows:
            return
        err_p, err_e = float(np.mean(miss_p)) / 7.0, float(np.mean(miss_e)) / 7.0
        if err_p + err_e <= 0:
            return
        w = err_p ** 2 / (err_p ** 2 + err_e ** 2)
        self._recent = {"days": H, "windows": len(windows), "prophetMissPerDay": round(err_p, 2),
                        "etsMissPerDay": round(err_e, 2), "etsWeight": round(w, 2),
                        "tests": windows, **windows[0]}
        if w < 0.02:
            return
        self._ets_future = _ets_forecast(fit_df, self.horizon, season=getattr(self, "_check_season", None))
        self._ets_w = float(w)
        if w >= 0.5:
            self.model_label = "Prophet, adjusted to recent sales"

    def _past_years_check(self, fit_df, build):
        """Hide the last 3 four-week stretches one at a time, forecast each with Prophet and
        with the same weeks in past years (from only the history before it), and weight the
        past years by how much better they did (see PAST_YEARS_CHECK)."""
        self._py_w, self._py_future, self._pastyears = 0.0, None, None
        if not PAST_YEARS_CHECK or not getattr(self, "_yearly_native", False) or not self.horizon:
            return
        import seasonality as _S
        if _S.monthly_index(fit_df)["strength"] < _S.YOY_STRONG:
            return
        fut_dates = _future_dates(self.last_date, self.horizon)
        reach = min(len(fut_dates), PAST_YEARS_REACH)
        future = _S.same_weeks_last_year(fit_df, fut_dates[:reach])
        if future is None:
            return
        given = getattr(self, "_py_given", None)
        if given is not None:
            if given >= 0.02:
                self._py_future, self._py_w = np.asarray(future["daily"], float), float(given)
            return
        H, n = PAST_YEARS_DAYS, len(fit_df)
        miss_p, miss_y, windows = [], [], []
        for k in range(1, PAST_YEARS_WINDOWS + 1):
            cut = n - k * H
            if cut < H:
                break
            train, test = fit_df.iloc[:cut], fit_df.iloc[cut:cut + H]
            yy = _S.same_weeks_last_year(train, test["ds"].tolist())
            if yy is None:
                break
            mh = build(False)
            mh.fit(train)
            frame = pd.concat([train.tail(1), test]).drop(columns=["y"])
            yp = np.clip(_damp_trend(mh.predict(frame), train["ds"].max())[1:], 0.0, None)
            ye = np.asarray(yy["daily"], float)[:len(test)]
            act = test["y"].to_numpy(dtype=float)
            wk = np.arange(len(act)) // 7
            wa = np.bincount(wk, act)
            miss_p += list(np.abs(wa - np.bincount(wk, yp)))
            miss_y += list(np.abs(wa - np.bincount(wk, ye)))
            windows.append({"actual": round(float(act.sum()), 1), "prophet": round(float(yp.sum()), 1),
                            "pastYears": round(float(ye.sum()), 1)})
        if len(windows) < PAST_YEARS_WINDOWS:
            return
        err_p, err_y = float(np.mean(miss_p)) / 7.0, float(np.mean(miss_y)) / 7.0
        if err_p + err_y <= 0:
            return
        w = err_p ** 2 / (err_p ** 2 + err_y ** 2)
        self._pastyears = {"days": H, "windows": len(windows), "prophetMissPerDay": round(err_p, 2),
                           "pastYearsMissPerDay": round(err_y, 2), "pastYearsWeight": round(w, 2),
                           "years": future.get("years"), "growth": future.get("growth"),
                           "tests": windows, **windows[0]}
        if w < 0.02:
            return
        self._py_future = np.asarray(future["daily"], float)
        self._py_w = float(w)
        if w >= 0.5:
            self.model_label = "Prophet, adjusted to past years"

    def _predict(self, events, price_override=None, force_no_promo=False):
        f = self._future_exog(events, price_override, force_no_promo)
        _y, fc = self._yhat(f)
        keep = (fc["ds"] > self.last_date).to_numpy()
        yhat = _y[keep]
        x_sd = np.zeros(len(yhat))
        ref = None
        # Price events move this forecast through a LINEAR regressor, which extrapolates
        # without limit. Compare every day against the same day at today's price: cap the
        # ratio, and measure how much of the day is a price effect so its uncertainty can
        # be carried into the band and the safety stock.
        if self.has_price and price_override is None and (self.last_price or 0) > 0:
            ref_f = f.copy()
            ref_f["price"] = float(self.last_price)
            ref = np.clip(self._yhat(ref_f)[0][keep], 0, None)
            prices = f["price"].to_numpy(dtype=float)[keep]
            moved = np.abs(prices - float(self.last_price)) > 1e-9
            if moved.any():
                from forecast_engine import (MAX_PRICE_LIFT, MIN_PRICE_RATIO, PRICE_MAX_ELASTICITY,
                                             extrapolation_distance, extrapolation_uncertainty,
                                             taper_beyond_edge)
                rng = getattr(self, "price_range", None)
                # OUTSIDE THE PRICES SOLD AT, RESPOND IN PERCENTAGES, NOT A STRAIGHT LINE.
                # The regressor is linear: every dollar moves the same number of units. That
                # is fine between prices the product has sold at, and wrong past them. A line
                # fitted between $40 and $50 runs out of sales around $70, so a 20% rise
                # forecast almost half the demand gone. Past the known range the response is
                # continued at the elasticity the model shows across that range: each 1% of
                # price moves demand by the same %, which bends instead of hitting zero.
                if rng and float(rng[1]) > float(rng[0]):
                    lo_p, hi_p = float(rng[0]), float(rng[1])
                    out_lo = moved & (prices < lo_p - 1e-9)
                    out_hi = moved & (prices > hi_p + 1e-9)
                    if out_lo.any() or out_hi.any():
                        f_lo, f_hi = f.copy(), f.copy()
                        f_lo["price"], f_hi["price"] = lo_p, hi_p
                        y_lo = np.clip(self._yhat(f_lo)[0][keep], 0, None)
                        y_hi = np.clip(self._yhat(f_hi)[0][keep], 0, None)
                        ok = (y_lo > 1e-9) & (y_hi > 1e-9)
                        with np.errstate(divide="ignore", invalid="ignore"):
                            el = np.where(ok, np.log(np.where(ok, y_lo / np.where(ok, y_hi, 1), 1))
                                          / np.log(lo_p / hi_p), 0.0)
                        el = np.clip(el, -PRICE_MAX_ELASTICITY, 0.0)
                        with np.errstate(divide="ignore", invalid="ignore"):
                            raw_lo = y_lo * np.power(np.clip(prices, 1e-9, None) / lo_p, el)
                            raw_hi = y_hi * np.power(np.clip(prices, 1e-9, None) / hi_p, el)
                        # ...and ease it toward the limits instead of stopping at them.
                        yhat = np.where(out_lo, taper_beyond_edge(y_lo, raw_lo, ref), yhat)
                        yhat = np.where(out_hi, taper_beyond_edge(y_hi, raw_hi, ref), yhat)
                y = np.clip(yhat, 0, None)
                capped = np.where(ref > 0, np.clip(y, MIN_PRICE_RATIO * ref, MAX_PRICE_LIFT * ref), y)
                yhat = np.where(moved, capped, yhat)
                self._price_lift_capped = bool(np.any(moved & (np.abs(capped - y) > 1e-9)))
                if rng:
                    u = np.array([extrapolation_uncertainty(extrapolation_distance(p, rng[0], rng[1]))
                                  for p in prices])
                    x_sd = np.where(moved, np.abs(np.clip(yhat, 0, None) - ref) * u, 0.0)
        w_e = getattr(self, "_ets_w", 0.0)
        ets = getattr(self, "_ets_future", None)
        if w_e > 0 and ets is not None and len(ets) >= len(yhat):
            # ETS knows nothing of price, so it takes the same price effect as Prophet's
            # forecast shows day by day (1 where the price is today's).
            ratio = np.ones(len(yhat))
            if self.has_price and (self.last_price or 0) > 0:
                if ref is None:
                    _rf = f.copy(); _rf["price"] = float(self.last_price)
                    _ref = np.clip(self._yhat(_rf)[0][keep], 0, None)
                else:
                    _ref = ref
                ratio = np.where(_ref > 1e-9, np.clip(yhat, 0, None) / np.where(_ref > 1e-9, _ref, 1.0), 1.0)
            _e = ets[:len(yhat)] * ratio
            if getattr(self, "_check_season", None) is not None:
                _e = _e / self._blend_factor(fc["ds"].values[keep])
            mixed = (1.0 - w_e) * np.clip(yhat, 0, None) + w_e * _e
            with np.errstate(divide="ignore", invalid="ignore"):
                x_sd = np.where(np.clip(yhat, 0, None) > 1e-9, x_sd * mixed / np.clip(yhat, 1e-9, None), x_sd)
            yhat = mixed
        w_y = getattr(self, "_py_w", 0.0)
        py = getattr(self, "_py_future", None)
        if w_y > 0 and py is not None and len(yhat):
            m = min(len(py), len(yhat))
            fade = np.clip((m - np.arange(m)) / float(PAST_YEARS_FADE), 0.0, 1.0) if m < len(yhat) else np.ones(m)
            ww = w_y * fade
            yhat = np.asarray(yhat, dtype=float).copy()
            yhat[:m] = (1.0 - ww) * np.clip(yhat[:m], 0, None) + ww * py[:m]
        band = _band(fc["ds"].values[keep], yhat, self._q_lo, self._q_hi)
        if getattr(self, "_blend_w", 0.0) > 0 and getattr(self, "_blend_mo", None) is not None:
            band = apply_pooled_yearly(band, self._blend_mo, self._blend_w,
                                       resid_std_norm=self._blend_resid_std,
                                       level=self._blend_level)
        band["x_sd"] = x_sd
        return band

    def predict_baseline(self, regular_price):
        return self._predict(self.events, price_override=regular_price, force_no_promo=True)

    def explain_bits(self):
        if getattr(self, "_blend_w", 0.0) > 0 and getattr(self, "_blend_mo", None) is not None:
            seasonality = {
                "weekly": True, "yearly": True, "blended": True,
                "blendWeight": round(float(self._blend_w), 2),
                "poolSize": self._n_pool_relatives,
                "text": (f"Weekly seasonality is learned from this product's own history. With under a "
                         f"full year of sales, its yearly seasonality is blended "
                         f"{round(self._blend_w * 100)}% from {self._n_pool_relatives} closely-matched related "
                         f"products, fading out as its own history builds."),
            }
        elif getattr(self, "_yearly_native", True):
            seasonality = {"weekly": True, "yearly": True, "blended": False,
                           "text": "Prophet models this product's own weekly and yearly seasonality directly."}
        else:
            seasonality = {"weekly": True, "yearly": False, "blended": False,
                           "text": ("Prophet models this product's own weekly seasonality. A yearly cycle needs "
                                    "about 15 months of history (or a close enough group of similar products).")}
        rc = getattr(self, "_recent", None)
        recent = None
        if rc:
            share = round(rc["etsWeight"] * 100)
            _p, _e, _a = round(rc["prophet"]), round(rc["ets"]), round(rc["actual"])
            _nw = int(rc.get("windows") or 1)
            _also = (f" Over {_nw} back-to-back 4-week tests like this one" if _nw > 1 else " So")
            recent = {**rc, "text": (
                (f"Tested on its last 4 weeks: Prophet would have forecast {_p}, a model that follows "
                 f"recent sales {_e}, and it sold {_a}.{_also}, the forecast comes out {share}% the "
                 f"recent-sales model and {100 - share}% Prophet.")
                if share >= 2 else
                (f"Tested on its last 4 weeks: Prophet would have forecast {_p}, a model that follows "
                 f"recent sales {_e}, and it sold {_a}. Prophet called {'these tests' if _nw > 1 else 'it'} "
                 f"better, so the forecast is Prophet's."))}
        py = getattr(self, "_pastyears", None)
        if py and recent is None:
            share = round(py["pastYearsWeight"] * 100)
            _p, _e, _a = round(py["prophet"]), round(py["pastYears"]), round(py["actual"])
            recent = {**py, "etsWeight": py["pastYearsWeight"], "text": (
                (f"Tested on its last 4 weeks: Prophet would have forecast {_p}, the same weeks in past years "
                 f"(at this year's pace) {_e}, and it sold {_a}. Over 3 back-to-back 4-week tests like this one, "
                 f"the forecast comes out {share}% past years and {100 - share}% Prophet.")
                if share >= 2 else
                (f"Tested on its last 4 weeks: Prophet would have forecast {_p}, the same weeks in past years "
                 f"{_e}, and it sold {_a}. Prophet called these tests better, so the forecast is Prophet's."))}
        return {"seasonality": seasonality, "recentCheck": recent,
                "price": {"used": bool(self.has_price), "sensitivity": None,
                          "inverted": getattr(self, "_price_inverted", None),
                          "text": (
                              "Price is an input, so price changes and promotions (counted as price cuts) shift the forecast."
                              if self.has_price else
                              ("Price isn't an input for this product. Its lower-priced days happened "
                               "to sell less, which is almost certainly coincidence, so price is switched "
                               "off rather than applied backwards. Price changes won't move this forecast "
                               "until the data shows a normal relationship."
                               if getattr(self, "_price_inverted", None) else
                               "No price data was provided."))}}


# ═════════════════════════════════════════════════════════════════════════════
#  GLOBAL POOLED ENGINE — new/thin/sparse SKUs WITH enough relatives
#  (lazy: only invoked by the router when relatives exist. Borrows seasonal
#   SHAPE from the group and anchors LEVEL from the SKU's own sales or peers.
#   This is the swappable baseline; a LightGBM global model drops in here later.)
# ═════════════════════════════════════════════════════════════════════════════
class GlobalPooledEngine:
    method = "global"
    model_label = "Global model (pooled)"

    def fit(self, df, today, related, **_):
        self.today = pd.Timestamp(today)
        df = df.sort_values("ds").reset_index(drop=True)
        self.last_date = df["ds"].max() if len(df) else self.today
        self.horizon = _horizon(today, self.last_date)
        self.n_relatives = len(related)

        # 1) Pooled seasonal profile from relatives (each normalized to its own mean).
        #    Shared with Prophet's yearly-blend path; resid_norm's spread sizes the band.
        wk, mo, resid_norm = pooled_seasonal_indices(related)
        self._wk, self._mo = wk, mo

        # 2) Level: own recent mean. Never another product's volume (see below).
        #    The own-sales level is SEASONALLY ADJUSTED before use: a new SKU's
        #    recent history is usually only a few weeks long and may sit entirely
        #    inside a high (or low) season. Averaging it raw and then multiplying by
        #    the monthly/weekly index in _predict() would apply that season twice.
        #    So we divide each observed day by its own wk*mo factor to recover a
        #    neutral, average-period baseline; _predict() then re-applies the curve once.
        own_df = df.dropna(subset=["y"])
        own = own_df["y"]
        # Launch-ramp defaults (no growth trend unless we find a real one below).
        self._trend = 0.0; self._trend_anchor = None; self.trend_source = None
        if len(own) >= 7 and own.sum() > 0:
            recent = own_df.tail(90)
            sea = (self._wk[recent["ds"].dt.weekday.to_numpy()]
                   * self._mo[recent["ds"].dt.month.to_numpy() - 1])
            sea = np.where(sea > 1e-6, sea, 1.0)
            deseason = recent["y"].to_numpy() / sea          # neutral daily series
            self.level = float(deseason.mean())
            self.level_source = "own recent sales (seasonally adjusted)"

            # LAUNCH RAMP: fit a linear trend on the deseasonalized series. A new
            # product is often still ramping up; holding the level flat under-forecasts
            # it. We only act with enough days (signal vs noise) and only on a slope
            # big enough to matter; _predict() then DAMPS it so growth fades instead
            # of extrapolating a steep climb forever. Retired automatically once the
            # SKU graduates to Prophet (which models its own trend).
            n = len(deseason)
            if n >= MIN_TREND_DAYS:
                idx = np.arange(n, dtype=float)
                slope, intercept = np.polyfit(idx, deseason, 1)
                # Only ramp UP. A genuine launch climbs; a flat seller in a rising
                # season can show a spurious downward slope, and projecting a decline
                # from a few weeks of data is unreliable (and risks under-ordering).
                # Downside risk is handled conservatively by the order guardrail instead.
                if slope > 0 and slope * 30.0 >= TREND_MIN_FRAC * max(self.level, 1.0):
                    slope = float(min(slope, self.level))   # no absurd ramps
                    self._trend = slope
                    self._trend_anchor = max(float(intercept + slope * (n - 1)), 0.0)
                    self.trend_source = "own recent sales"
        else:
            # HOW MUCH IT SELLS ALWAYS COMES FROM ITS OWN SALES. Similar products lend their
            # seasonal shape, holidays and price response, never their volume: a product
            # that has sat on the shelf 20 days without a sale, next to peers selling 6 a
            # day, was forecast 185 units a month and would have been reordered. With no
            # sales yet the forecast is 0 until it sells.
            self.level = float(own.mean()) if len(own) and own.sum() > 0 else 0.0
            self.level_source = "own sales" if self.level > 0 else "no sales yet"

        # 3) Band / sigma from pooled normalized residuals, scaled to this level.
        if resid_norm:
            rn = np.asarray(resid_norm, dtype=float)
            self.residual_std = float(np.std(rn) * self.level)
            self._q_lo = float(np.percentile(rn, 10) * self.level)
            self._q_hi = float(np.percentile(rn, 90) * self.level)
        else:
            self.residual_std = 0.35 * self.level
            self._q_lo, self._q_hi = -Z80 * self.residual_std, Z80 * self.residual_std
        self.residual_cv = self.residual_std / max(self.level, 1.0)

        self.forecast_df = self._predict()
        return self

    def _predict(self):
        dates = _future_dates(self.last_date, self.horizon)
        wd = dates.weekday.to_numpy(); mn = dates.month.to_numpy() - 1
        h = len(dates)
        if self._trend and self._trend_anchor is not None:
            # Holt-style DAMPED trend: cumulative trend at day t = slope * Σ φ^k (k=1..t).
            # The geometric sum converges, so the ramp flattens to a ceiling instead of
            # rising without bound. Anchored at the last actual day's fitted level.
            phi = TREND_DAMP
            k = np.arange(1, h + 1)
            damp_cum = phi * (1 - phi ** k) / (1 - phi)
            base = np.clip(self._trend_anchor + self._trend * damp_cum, 0.0, None)
        else:
            base = np.full(h, self.level)
        yhat = base * self._wk[wd] * self._mo[mn]
        return _band(dates, yhat, self._q_lo, self._q_hi)

    def predict_baseline(self, regular_price):
        # Pooled model has no price/promo input → baseline equals live (honest).
        return self.forecast_df

    def explain_bits(self):
        return {"seasonality": {"weekly": True, "yearly": True,
                                "text": (f"Seasonal shape is pooled from {self.n_relatives} related products "
                                         f"(this product doesn't have enough of its own history to learn it).")},
                "price": {"used": False, "sensitivity": None,
                          "text": "Forecast by similarity to related products; price isn't an input in this mode."},
                "level": {"value": round(self.level, 1), "source": self.level_source,
                          "text": ("It hasn't sold yet, so nothing is forecast until it does. Its volume "
                                   "will come from its own sales, never from other products'."
                                   if self.level_source == "no sales yet" else
                                   f"Expected volume is set from {self.level_source}.")},
                "trend": {"active": bool(self._trend), "perDay": round(self._trend, 3),
                          "text": (f"Recent sales are {'rising' if self._trend > 0 else 'falling'} "
                                   f"(~{abs(self._trend):.2f} units/day, seasonally adjusted); a damped growth "
                                   f"trend is applied and fades over ~2 weeks."
                                   if self._trend else
                                   "No meaningful growth trend, so volume is held flat.")}}


# ═════════════════════════════════════════════════════════════════════════════
#  INTERMITTENT ENGINE — sparse demand with NO relatives (Croston / TSB)
# ═════════════════════════════════════════════════════════════════════════════
class IntermittentEngine:
    method = "croston"
    model_label = "Croston/TSB"

    def fit(self, df, today, **_):
        from statsforecast import StatsForecast            # lazy
        from statsforecast.models import CrostonClassic, TSB
        # Imported separately and softly: a statsforecast build without SBA must cost us
        # the correction, not the entire intermittent route.
        try:
            from statsforecast.models import CrostonSBA
        except ImportError:
            CrostonSBA = None
        self.today = pd.Timestamp(today)
        df = df.sort_values("ds").reset_index(drop=True)
        self.last_date = df["ds"].max()
        self.horizon = _horizon(today, self.last_date)
        base = pd.DataFrame({"unique_id": UID, "ds": df["ds"].values, "y": df["y"].values})

        # TSB decays the demand PROBABILITY through runs of zero days — right when a
        # product may be dying, wrong for one that simply sells every second or third day
        # and is still going. Offering it as a candidate there lets a stretch of quiet
        # days collapse the point forecast well below the rate the product is genuinely
        # selling at. So TSB only competes for genuinely rare, bursty demand; a regular
        # low-volume trickle stays on Croston, which estimates the rate without the decay.
        from forecast_engine import sparse_subtype, classify_demand
        _sub = sparse_subtype(df["y"].to_numpy(float), classify_demand(df["y"].to_numpy(float)))
        self.sparse_subtype = _sub
        # Croston's rate estimator is known to be biased UPWARD: it divides demand size
        # by inter-demand interval, and the expectation of that ratio exceeds the ratio of
        # expectations. On this catalogue it showed as intermittent products running
        # +30% high with band coverage of 68% against a nominal 80% — over-forecasting
        # pushes the interval off-centre, so both symptoms have one cause.
        #
        # The Syntetos-Boylan approximation is the standard correction (a (1 - alpha/2)
        # factor). It competes rather than replaces: cross-validation picks whichever
        # actually does better on this product's own history.
        cands = {"CrostonClassic": CrostonClassic()}
        if CrostonSBA is not None:
            cands["CrostonSBA"] = CrostonSBA()
        if _sub != "low_volume_regular":
            # How often it sells (alpha_p) moves half as fast as how many it sells per sale.
            # At 0.2 a single sale after a quiet fortnight took a one-a-week product from
            # 0.9 to 9 a month overnight (and back down over the next two weeks). At 0.1
            # the same sale moves it 2.4 to 6.2. On a real store's slow sellers that cost
            # 8% in accuracy and cut the worst overnight swing from 13x to under 5x.
            cands["TSB"] = TSB(alpha_d=0.2, alpha_p=0.1)
        # Use as many CV windows as the history affords (up to 3) — a single window
        # made the Croston-vs-TSB pick hostage to one stretch of noise. And require
        # the challenger to beat the default by a real margin (2% MAE) to displace
        # it, so ties don't flip the model between refits.
        n = len(df); test_h = min(28, max(7, n // 6))
        nw = 3 if n >= test_h * 6 else 2 if n >= test_h * 4 else 1
        SWITCH_MARGIN = 0.98   # challenger must be ≥2% better than CrostonClassic
        best, best_resid = "CrostonClassic", None
        try:
            sf = StatsForecast(models=list(cands.values()), freq="D", n_jobs=1)
            cv = sf.cross_validation(df=base, h=test_h, n_windows=nw, step_size=test_h)
            resids = {nm: cv["y"].to_numpy() - cv[nm].to_numpy() for nm in cands}
            maes = {nm: float(np.mean(np.abs(r))) for nm, r in resids.items()}
            best_resid = resids["CrostonClassic"]
            base_mae = maes.get("CrostonClassic", float("inf"))
            for nm, mae in maes.items():
                if nm != "CrostonClassic" and mae < base_mae * SWITCH_MARGIN:
                    best, best_resid, base_mae = nm, resids[nm], mae
        except Exception as e:
            print(f"[intermittent] CV failed: {e}")
        self.model_label = (f"{best} (slow recurring demand)" if _sub == "low_volume_regular"
                            else f"{best} (intermittent demand)")

        self.residual_std = float(np.std(best_resid)) if best_resid is not None and len(best_resid) > 1 \
            else float(np.std(df["y"].to_numpy()))
        self.residual_cv = self.residual_std / max(float(df["y"].mean()), 1.0)
        self._q_lo, self._q_hi = conformal_offsets(best_resid, self.residual_std)

        # ── Band widening for intermittent demand ────────────────────────────────
        # Measured, not guessed. A backtest over 147 Croston forecasts on the real
        # catalogue put interval coverage at 65.4% against an 80% band — the spiky
        # upside of intermittent demand is exactly what conformal offsets taken from
        # in-sample residuals under-estimate. Under a normal, 65.4% is +/-0.945 sigma
        # and 80% is +/-1.282, so 1.282/0.945 = 1.36 closes it; heavy tails mean that
        # is a floor rather than an over-correction.
        #
        # This scales the BAND ONLY. `residual_std` above is untouched, and that is
        # deliberate: residual_std is what feeds lead_window_sigma and therefore the
        # safety buffer, and the same backtest shows Croston products already ordering
        # 43.8% too big while achieving 93.2% service. Widening their buffer would add
        # cash to products that are over-stocked already. The band was lying about how
        # uncertain the forecast is; the order quantity was not the thing at fault.
        #
        # Re-derive this from interval_cov% per engine after a backtest rather than
        # carrying it forward on faith.
        INTERMITTENT_BAND_WIDEN = 1.36
        self._q_lo *= INTERMITTENT_BAND_WIDEN
        self._q_hi *= INTERMITTENT_BAND_WIDEN

        sf2 = StatsForecast(models=[cands[best]], freq="D", n_jobs=1); sf2.fit(df=base)
        raw = sf2.predict(h=self.horizon)
        raw = raw.reset_index() if "ds" not in raw.columns else raw
        point = next(c for c in raw.columns if c not in ("unique_id", "ds") and "-lo-" not in c and "-hi-" not in c)
        self.forecast_df = _band(raw["ds"].values, raw[point].to_numpy(), self._q_lo, self._q_hi)
        return self

    def predict_baseline(self, regular_price):
        return self.forecast_df

    def explain_bits(self):
        _regular = getattr(self, "sparse_subtype", "") == "low_volume_regular"
        return {"seasonality": {"weekly": False, "yearly": False,
                                "text": ("This product sells on some days and not others, so it's modelled as a "
                                         "steady rate (units per day), not a seasonal curve. That rate is often "
                                         "fractional per day, so orders are sized by summing it across the lead "
                                         "time and coverage window."
                                         if _regular else
                                         "Intermittent demand is modeled by the size and timing of sporadic sales, not seasonal curves.")},
                "price": {"used": False, "sensitivity": None,
                          "text": "Price isn't used: intermittent demand is too sparse to estimate it."}}


# ═════════════════════════════════════════════════════════════════════════════
#  MOVING-AVERAGE FALLBACK — the 'abstain' route. Not really a forecast; a
#  low-confidence placeholder so the UI has numbers while the status flags it.
# ═════════════════════════════════════════════════════════════════════════════
class MovingAverageEngine:
    method = "abstain"
    model_label = "Moving average: last resort (no history, no relatives)"

    def fit(self, df, today, **_):
        self.today = pd.Timestamp(today)
        df = df.sort_values("ds").reset_index(drop=True)
        self.last_date = df["ds"].max() if len(df) else self.today
        self.horizon = _horizon(today, self.last_date)
        y = df["y"].dropna().to_numpy()
        self.level = float(np.mean(y[-28:])) if len(y) else 0.0
        self.residual_std = float(np.std(y)) if len(y) > 1 else 0.0
        self.residual_cv = self.residual_std / max(self.level, 1.0)
        self._q_lo, self._q_hi = -Z80 * self.residual_std, Z80 * self.residual_std
        dates = _future_dates(self.last_date, self.horizon)
        self.forecast_df = _band(dates, np.full(len(dates), self.level), self._q_lo, self._q_hi)
        return self

    def predict_baseline(self, regular_price):
        return self.forecast_df

    def explain_bits(self):
        return {"seasonality": {"weekly": False, "yearly": False,
                                "text": "Not enough data to model seasonality, so this is a flat recent-average placeholder."},
                "price": {"used": False, "sensitivity": None, "text": "Price isn't used in the fallback estimate."}}

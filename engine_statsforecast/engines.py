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
import numpy as np
import pandas as pd

Z80 = 1.2816
UID = "series"

# TREND_LEVER (new / short-history SKUs): launch-ramp (damped growth) settings for
# the pooled global engine — how a ramping new product's trend is projected.
# Code-only lever — only change it if REAL data shows a consistent bias.
MIN_TREND_DAYS = 21    # need ≥3 weeks of own sales before trusting a growth slope
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
POOLED_YEARLY_DAYS  = 450   # matches Prophet's yearly_seasonality cutoff; weight → 0 here
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
            rr = r.dropna(subset=["y"])
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


def pooled_monthly_cohesion(related, mo=None, min_months=6):
    """How much the siblings AGREE on an annual shape, in [0,1]: the mean Pearson
    correlation between each sibling's own monthly profile and the pooled centroid.
    A pool of flat (non-seasonal) or disagreeing siblings scores near 0, so the
    caller can decline to borrow a shape that isn't really there. Needs ≥2 siblings
    each covering ≥`min_months` of the year to return anything above 0."""
    profs = []
    for r in related:
        rr = r.dropna(subset=["y"])
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
    centroid = np.asarray(mo, dtype=float) if mo is not None else np.nanmean(np.vstack(profs), axis=0)
    cors = []
    for prof in profs:
        m = ~np.isnan(prof) & ~np.isnan(centroid)
        if m.sum() < min_months:
            continue
        a, b = prof[m], centroid[m]
        if np.std(a) < 1e-9 or np.std(b) < 1e-9:
            cors.append(0.0)          # a flat sibling contributes no seasonal agreement
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

    def fit(self, df, today, has_price, has_promo, last_price, events, related=None, **_):
        from prophet import Prophet  # lazy
        self.today = pd.Timestamp(today)
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
        self._yearly_native = days >= 450
        m = Prophet(yearly_seasonality=self._yearly_native, weekly_seasonality=days >= 14,
                    daily_seasonality=False, interval_width=0.80,
                    changepoint_prior_scale=0.05)
        if has_price: m.add_regressor("price", standardize=True)
        if has_promo: m.add_regressor("on_promotion", standardize=False)

        fit_df = df[["ds", "y"]].copy()
        if has_price: fit_df["price"] = df["price"].ffill().fillna(last_price or 0)
        if has_promo: fit_df["on_promotion"] = df["on_promotion"].fillna(0).clip(0, 1)
        m.fit(fit_df)
        self._model = m
        self._fit_df = fit_df

        # In-sample residuals → conformal band + safety-stock sigma.
        resid = fit_df["y"].to_numpy() - m.predict(fit_df)["yhat"].to_numpy()
        self.residual_std = float(np.std(resid))
        self.residual_cv = self.residual_std / max(float(fit_df["y"].mean()), 1.0)
        self._q_lo, self._q_hi = conformal_offsets(resid, self.residual_std)

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
        blend_related = [r for r in (related or []) if _frame_days(r) >= POOLED_YEARLY_DAYS]
        self._n_pool_relatives = len(blend_related)
        if not self._yearly_native and blend_related:
            _wk_p, mo_p, resid_norm = pooled_seasonal_indices(blend_related)
            cohesion = pooled_monthly_cohesion(blend_related, mo_p)
            w = pooled_blend_weight(days, self._n_pool_relatives, cohesion)
            if w > 0 and float(np.std(mo_p)) > 1e-6:
                self._blend_w = w
                self._blend_mo = mo_p
                self._blend_cohesion = cohesion
                self._blend_resid_std = float(np.std(resid_norm)) if resid_norm else 0.0
                self._blend_level = max(float(fit_df["y"].mean()), 1.0)

        self.forecast_df = self._predict(self.events)
        return self

    def _future_exog(self, events, price_override=None, force_no_promo=False):
        f = self._model.make_future_dataframe(periods=self.horizon, freq="D")
        if self.has_price:
            f["price"] = price_override if price_override is not None else (self.last_price or 0.0)
            if price_override is None:
                for ev in events:
                    if ev.get("type") in ("price_change_permanent", "price_change_temporary"):
                        s = pd.to_datetime(ev["date"]); np_ = float(ev.get("new_price", self.last_price or 0))
                        if ev["type"] == "price_change_permanent":
                            f.loc[f["ds"] >= s, "price"] = np_
                        else:
                            e = pd.to_datetime(ev.get("end_date", ev["date"]))
                            f.loc[(f["ds"] >= s) & (f["ds"] <= e), "price"] = np_
        if self.has_promo:
            f["on_promotion"] = 0
            if not force_no_promo:
                for ev in events:
                    if ev.get("type") == "promotion":
                        s = pd.to_datetime(ev["date"]); e = pd.to_datetime(ev.get("end_date", ev["date"]))
                        msk = (f["ds"] >= s) & (f["ds"] <= e)
                        f.loc[msk, "on_promotion"] = 1
                        if self.has_price and ev.get("discount_pct"):
                            f.loc[msk, "price"] = (f.loc[msk, "price"] * (1 - float(ev["discount_pct"]) / 100)).round(2)
        return f

    def _predict(self, events, price_override=None, force_no_promo=False):
        f = self._future_exog(events, price_override, force_no_promo)
        fc = self._model.predict(f)
        fc = fc[fc["ds"] > self.last_date]
        band = _band(fc["ds"].values, fc["yhat"].to_numpy(), self._q_lo, self._q_hi)
        if getattr(self, "_blend_w", 0.0) > 0 and getattr(self, "_blend_mo", None) is not None:
            band = apply_pooled_yearly(band, self._blend_mo, self._blend_w,
                                       resid_std_norm=self._blend_resid_std,
                                       level=self._blend_level)
        return band

    def predict_baseline(self, regular_price):
        return self._predict(self.events, price_override=regular_price, force_no_promo=True)

    def explain_bits(self):
        if getattr(self, "_blend_w", 0.0) > 0 and getattr(self, "_blend_mo", None) is not None:
            seasonality = {
                "weekly": True, "yearly": True, "blended": True,
                "blendWeight": round(float(self._blend_w), 2),
                "poolSize": self._n_pool_relatives,
                "text": (f"Weekly seasonality is learned from this product's own history. It doesn't yet "
                         f"have a full year of sales, so its yearly seasonality is blended "
                         f"{round(self._blend_w * 100)}% from {self._n_pool_relatives} closely-matched related "
                         f"products, fading out as it builds up its own annual history."),
            }
        elif getattr(self, "_yearly_native", True):
            seasonality = {"weekly": True, "yearly": True, "blended": False,
                           "text": "Prophet models this product's own weekly and yearly seasonality directly."}
        else:
            seasonality = {"weekly": True, "yearly": False, "blended": False,
                           "text": ("Prophet models this product's own weekly seasonality; it doesn't yet have "
                                    "a full year of history (or a close enough pool of related products) to "
                                    "establish a yearly cycle.")}
        return {"seasonality": seasonality,
                "price": {"used": bool(self.has_price), "sensitivity": None,
                          "text": ("Price and promotions are inputs, so changing them shifts the forecast."
                                   if self.has_price else "No price data was provided.")}}


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

        # 2) Level: own recent mean if the SKU has real sales; else peer median level.
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
        elif related:
            self.level = float(np.median([float(r["y"].mean()) for r in related]))
            self.level_source = "typical volume of related products"
        else:
            self.level = float(own.mean()) if len(own) else 0.0
            self.level_source = "own sales"

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
                          "text": f"Expected volume is set from {self.level_source}."},
                "trend": {"active": bool(self._trend), "perDay": round(self._trend, 3),
                          "text": (f"Recent sales are {'rising' if self._trend > 0 else 'falling'} "
                                   f"(~{abs(self._trend):.2f} units/day, seasonally adjusted); a damped growth "
                                   f"trend is applied and fades over ~2 weeks."
                                   if self._trend else
                                   "No meaningful growth trend detected — volume is held flat.")}}


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
            cands["TSB"] = TSB(alpha_d=0.2, alpha_p=0.2)
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
                                         "steady underlying RATE (units per day) rather than a seasonal curve. "
                                         "Expected demand is fractional per day — orders are sized by summing "
                                         "that rate across the lead time and coverage window."
                                         if _regular else
                                         "Intermittent demand is modeled by the size and timing of sporadic sales, not seasonal curves.")},
                "price": {"used": False, "sensitivity": None,
                          "text": "Price isn't used — intermittent demand has too sparse a signal to estimate it."}}


# ═════════════════════════════════════════════════════════════════════════════
#  MOVING-AVERAGE FALLBACK — the 'abstain' route. Not really a forecast; a
#  low-confidence placeholder so the UI has numbers while the status flags it.
# ═════════════════════════════════════════════════════════════════════════════
class MovingAverageEngine:
    method = "abstain"
    model_label = "Moving average — LAST RESORT (no history, no relatives)"

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
                                "text": "Not enough data to model seasonality — this is a flat recent-average placeholder."},
                "price": {"used": False, "sensitivity": None, "text": "Price isn't used in the fallback estimate."}}

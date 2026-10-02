"""
test_global_seasonal.py — locks in the GlobalPooledEngine seasonal-adjustment behavior.

Background
----------
A new/thin SKU is forecast by the pooled "global" model: it borrows the seasonal
SHAPE (weekly + monthly index, each normalized so a sibling's own average is 1.0)
from related SKUs, and sets its own VOLUME (level) from its own recent sales.

The bug this test guards against: a new SKU's recent history is often only a few
weeks long and can sit entirely inside a high season. If the level is averaged raw
and then multiplied by the monthly index in _predict(), the season is applied
TWICE and the forecast balloons (e.g. a SKU selling ~10/day in early summer gets
projected at ~20/day for peak July, a rate it has never approached).

The fix deseasonalizes the level before use, so the seasonal curve is applied once.

These tests use NO heavy libraries — GlobalPooledEngine.fit() only needs numpy/pandas.
Run:  python test_global_seasonal.py   (exits non-zero if anything regresses)
"""
import datetime
import numpy as np
import pandas as pd
from engines import GlobalPooledEngine


def _seasonal_siblings(n=4, years=2, peak_month=7, amp=1.0, base=30.0):
    """Build `n` established sibling SKUs that all share a clean summer-peaking
    seasonal curve (peaks in `peak_month`, troughs in winter). Each sibling has a
    different base volume so the model can't cheat by copying a magnitude."""
    end = pd.Timestamp("2026-06-14")
    dates = pd.date_range(end - pd.Timedelta(days=365 * years), end, freq="D")
    frames = []
    for k in range(n):
        # cosine season: 1+amp at peak_month, 1-amp at the opposite month
        phase = (dates.month.to_numpy() - peak_month) / 12.0 * 2 * np.pi
        season = 1.0 + amp * np.cos(phase)
        level = base * (1.0 + 0.4 * k)  # distinct magnitudes per sibling
        y = np.round(level * season).clip(min=0)
        frames.append(pd.DataFrame({"ds": dates, "y": y}))
    return frames


def _new_sku_high_season(daily=10.0):
    """A brand-new SKU: ~6 weeks of flat sales landing entirely in May–June
    (high season). Its raw daily average is `daily`, but its DESEASONALIZED
    baseline should be well below that."""
    dates = pd.date_range("2026-05-01", "2026-06-14", freq="D")
    return pd.DataFrame({"ds": dates, "y": np.full(len(dates), daily)})


def _month_total(model, year, month):
    fc = model.forecast_df
    m = (fc["ds"].dt.year == year) & (fc["ds"].dt.month == month)
    return float(fc.loc[m, "yhat"].sum())


def run():
    today = datetime.date(2026, 6, 16)
    siblings = _seasonal_siblings()
    new = _new_sku_high_season(daily=10.0)

    model = GlobalPooledEngine().fit(new, today=today, related=siblings)

    # peak (July) and trough (January) seasonal indices the model learned
    jul_idx = float(model._mo[6])
    jan_idx = float(model._mo[0])
    assert jul_idx > 1.4, f"expected a strong summer peak, got July index {jul_idx:.2f}"
    assert jan_idx < jul_idx, "July should outrank January in the seasonal curve"

    # 1) The level must be DESEASONALIZED: the SKU's raw mean is 10/day, but it was
    #    measured in high season, so the neutral baseline must be clearly lower.
    assert model.level < 9.0, (
        f"level not seasonally adjusted: got {model.level:.2f}/day, expected < 9 "
        f"(raw in-season mean is 10/day). The season is being double-counted.")
    assert "seasonally adjusted" in model.level_source

    # 2) The July forecast must NOT exceed what raw double-counting would produce.
    #    Raw bug behavior ≈ 10/day * July_index. The fixed forecast must be well under it.
    jul = _month_total(model, 2026, 7)
    buggy_jul_daily = 10.0 * jul_idx
    fixed_jul_daily = jul / 31.0
    assert fixed_jul_daily < buggy_jul_daily * 0.85, (
        f"July looks double-counted: {fixed_jul_daily:.1f}/day vs buggy "
        f"{buggy_jul_daily:.1f}/day")

    # 3) Sanity: July (peak) should still be the highest of the summer months,
    #    and the forecast should stay positive and finite everywhere.
    jun = _month_total(model, 2026, 6)
    aug = _month_total(model, 2026, 8)
    assert jul >= aug and jul >= jun, "July should remain the seasonal peak"
    assert np.isfinite(model.forecast_df["yhat"]).all()
    assert (model.forecast_df["yhat"] >= 0).all()

    # 4) Control: if a NEW SKU's window were a full neutral year, deseasonalizing
    #    must be a no-op (level ≈ raw mean) — the fix only corrects partial windows.
    full_year = pd.DataFrame({
        "ds": pd.date_range("2025-06-15", "2026-06-14", freq="D"),
        "y": np.full(365, 8.0)})
    m2 = GlobalPooledEngine().fit(full_year, today=today, related=siblings)
    assert abs(m2.level - 8.0) < 0.6, (
        f"deseasonalizing a full-year window should ~no-op, got level {m2.level:.2f}")

    print("PASS  level={:.2f}/day (raw was 10)  July={:.0f} (~{:.1f}/day, buggy≈{:.0f})  "
          "full-year-level={:.2f}".format(
              model.level, jul, fixed_jul_daily, buggy_jul_daily * 31, m2.level))


def run_launch_ramp():
    """The damped launch-ramp trend: a clearly-growing new SKU should forecast
    UPWARD from its current level, but the growth must DAMPEN to a plateau (never
    extrapolate a steep climb forever). A flat seller must get no trend at all."""
    today = datetime.date(2026, 6, 16)
    # Flat sibling donors (no seasonality) so the test isolates the trend logic.
    flat_sibs = [pd.DataFrame({
        "ds": pd.date_range("2025-05-01", "2026-06-15", freq="D"),
        "y": np.full(411, 15.0)}) for _ in range(3)]

    # A) Strong launch ramp: 34 days climbing 4 → 20/day, ending yesterday.
    days = pd.date_range("2026-05-13", "2026-06-15", freq="D")
    ramp = pd.DataFrame({"ds": days, "y": np.linspace(4, 20, len(days)).round()})
    r = GlobalPooledEngine().fit(ramp, today=today, related=flat_sibs)
    assert r._trend > 0, "a clear upward ramp should activate a positive trend"
    fc = r.forecast_df["yhat"].to_numpy()
    assert fc[0] >= 18, f"forecast should continue near the last actual (~20), got {fc[0]:.1f}"
    early, late = fc[:14].mean(), fc[30:90].mean()
    assert late > early, "ramp should keep rising past the first two weeks"
    plateau = fc[60:120].max()
    assert plateau < 20 * 2.0, f"damped ramp must plateau, not explode (max {plateau:.1f})"
    assert np.isfinite(fc).all() and (fc >= 0).all()

    # B) Flat seller → NO trend (held flat).
    flat = pd.DataFrame({"ds": days, "y": np.full(len(days), 12.0)})
    f = GlobalPooledEngine().fit(flat, today=today, related=flat_sibs)
    assert f._trend == 0.0, "a flat seller must not get a growth trend"

    print("PASS  ramp: trend={:.3f}/day  start={:.0f} plateau≈{:.0f}  |  flat: trend={:.1f}".format(
        r._trend, fc[0], fc[60:120].max(), f._trend))


def run_no_sales_borrows_no_volume():
    """A product that hasn't sold yet is forecast 0, never at its peers' volume."""
    today = pd.Timestamp("2026-09-26")
    rng = np.random.default_rng(1)
    rel = [pd.DataFrame({"ds": pd.date_range(end=today - pd.Timedelta(days=1), periods=400),
                         "y": rng.poisson(6, 400).astype(float)}) for _ in range(4)]
    new = pd.DataFrame({"ds": pd.date_range(end=today - pd.Timedelta(days=1), periods=20), "y": 0.0})
    e = GlobalPooledEngine().fit(new, today=today, related=rel)
    total = float(e.forecast_df["yhat"].to_numpy()[:30].sum())
    assert total == 0.0, f"forecast {total:.1f} for a product with no sales (peers sell ~6/day)"
    assert e.level_source == "no sales yet", e.level_source
    print(f"PASS  no sales yet: 30-day forecast {total:.1f} (its peers sell ~180 a month)")


if __name__ == "__main__":
    run()
    run_launch_ramp()
    run_no_sales_borrows_no_volume()

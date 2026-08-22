"""
test_prophet_blend.py — locks in the blended pooled-yearly-seasonality path used by
ProphetEngine for "established but under a year" SKUs (≈180–450 days of history).

Prophet turns yearly seasonality OFF below ~450 days, so such a SKU would carry NO
annual shape of its own. When it has a real, cohesive pool of relatives, it borrows
their pooled monthly index, faded in by history + cohesion. These tests exercise the
math directly (pooled_blend_weight / pooled_monthly_cohesion / apply_pooled_yearly)
so they need NO heavy libs — Prophet itself is not imported.

Run:  python test_prophet_blend.py   (exits non-zero if anything regresses)
"""
import sys
import numpy as np
import pandas as pd

from engines import (pooled_seasonal_indices, pooled_monthly_cohesion,
                     pooled_blend_weight, apply_pooled_yearly,
                     POOLED_FADE_START, POOLED_YEARLY_DAYS,
                     BLEND_MIN_RELATIVES, BLEND_MIN_COHESION)

_fails = []
def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        _fails.append(name)


def _seasonal_siblings(n=4, years=2, peak_month=7, amp=0.6, base=30.0):
    """`n` established siblings sharing a clean summer-peaking annual curve, each at a
    different magnitude so only the SHAPE (not volume) is shared."""
    end = pd.Timestamp("2026-06-14")
    dates = pd.date_range(end - pd.Timedelta(days=365 * years), end, freq="D")
    frames = []
    for k in range(n):
        phase = (dates.month.to_numpy() - peak_month) / 12.0 * 2 * np.pi
        season = 1.0 + amp * np.cos(phase)
        y = np.round(base * (1.0 + 0.4 * k) * season).clip(min=0)
        frames.append(pd.DataFrame({"ds": dates, "y": y}))
    return frames


def _seasonal_siblings_days(n=4, days=300, peak_month=7, amp=0.6, base=30.0):
    end = pd.Timestamp("2026-06-14")
    dates = pd.date_range(end - pd.Timedelta(days=days), end, freq="D")
    frames = []
    for k in range(n):
        phase = (dates.month.to_numpy() - peak_month) / 12.0 * 2 * np.pi
        season = 1.0 + amp * np.cos(phase)
        y = np.round(base * (1.0 + 0.4 * k) * season).clip(min=0)
        frames.append(pd.DataFrame({"ds": dates, "y": y}))
    return frames


def _disagreeing_siblings(years=2, base=30.0):
    """A pool whose members DISAGREE on annual shape: half peak in summer, half in
    winter. There's no shared seasonality to borrow, so cohesion should stay low."""
    end = pd.Timestamp("2026-06-14")
    dates = pd.date_range(end - pd.Timedelta(days=365 * years), end, freq="D")
    frames = []
    for k, peak in enumerate((7, 7, 1, 1)):
        phase = (dates.month.to_numpy() - peak) / 12.0 * 2 * np.pi
        season = 1.0 + 0.6 * np.cos(phase)
        frames.append(pd.DataFrame({"ds": dates, "y": np.round(base * (1 + 0.4 * k) * season).clip(min=0)}))
    return frames


def _truly_flat_siblings(n=4, years=2, base=30.0):
    """`n` perfectly flat (non-seasonal) siblings — the pooled monthly index they
    produce should be ~all-ones, so the blend is a no-op even if the gate is passed."""
    end = pd.Timestamp("2026-06-14")
    dates = pd.date_range(end - pd.Timedelta(days=365 * years), end, freq="D")
    return [pd.DataFrame({"ds": dates, "y": np.full(len(dates), base * (1 + 0.4 * k))}) for k in range(n)]


# ── 1) cohesion separates a real shared shape from a disagreeing pool ───────────
sib = _seasonal_siblings()
_, mo_sib, _ = pooled_seasonal_indices(sib)
coh_seasonal = pooled_monthly_cohesion(sib, mo_sib)
coh_disagree = pooled_monthly_cohesion(_disagreeing_siblings(), None)
check("cohesive seasonal pool scores high", coh_seasonal > 0.9, round(coh_seasonal, 3))
check("disagreeing pool scores below the borrow gate", coh_disagree < BLEND_MIN_COHESION, round(coh_disagree, 3))

# A truly flat pool produces a ~flat monthly index → the engine's std(mo) guard makes
# the blend a no-op regardless of the gate (borrowing "no shape" changes nothing).
_, mo_flat, _ = pooled_seasonal_indices(_truly_flat_siblings())
check("flat pool yields a ~flat monthly index (blend is a no-op)", float(np.std(mo_flat)) < 1e-6,
      round(float(np.std(mo_flat)), 6))

# The Prophet yearly blend must borrow annual shape only from mature annual donors.
# Under-450 products may be grouped together for behavior, but they should not
# bootstrap yearly seasonality from each other's partial-year histories.
young_relatives = [r for r in _seasonal_siblings_days(days=300) if (r["ds"].max() - r["ds"].min()).days >= POOLED_YEARLY_DAYS]
check("under-450 siblings are not eligible yearly-blend donors", len(young_relatives) == 0)

# ── 2) blend weight: gated, and fades to 0 as the SKU nears a full year ─────────
w_180 = pooled_blend_weight(POOLED_FADE_START, 4, coh_seasonal)
w_300 = pooled_blend_weight(300, 4, coh_seasonal)
w_449 = pooled_blend_weight(POOLED_YEARLY_DAYS - 1, 4, coh_seasonal)
w_native = pooled_blend_weight(POOLED_YEARLY_DAYS, 4, coh_seasonal)
check("weight > 0 at the young end of the band", w_180 > 0, round(w_180, 3))
check("weight fades monotonically toward a full year", w_180 > w_300 > w_449, f"{w_180:.2f}>{w_300:.2f}>{w_449:.2f}")
check("weight is 0 once Prophet's own yearly is native", w_native == 0.0, round(w_native, 3))
check("weight is 0 without enough relatives",
      pooled_blend_weight(300, BLEND_MIN_RELATIVES - 1, coh_seasonal) == 0.0)
check("weight is 0 when the pool isn't cohesive",
      pooled_blend_weight(300, 4, coh_disagree) == 0.0)

# ── 3) apply_pooled_yearly injects the pooled shape, weight-scaled, no double-count ─
future = pd.date_range("2026-06-15", periods=365, freq="D")
flat = pd.DataFrame({"ds": future, "yhat": 100.0, "yhat_lower": 80.0, "yhat_upper": 120.0})

unchanged = apply_pooled_yearly(flat, mo_sib, 0.0)
check("weight 0 is a no-op", np.allclose(unchanged["yhat"], flat["yhat"]))

full = apply_pooled_yearly(flat, mo_sib, 1.0)
jul = full.loc[full["ds"].dt.month == 7, "yhat"].mean()
jan = full.loc[full["ds"].dt.month == 1, "yhat"].mean()
check("full weight lifts the peak month above the trough", jul > jan * 1.3, f"Jul={jul:.1f} Jan={jan:.1f}")
# mean-1 index → applying it to a flat 100 should recover 100*mo per month exactly
exp_jul = 100.0 * mo_sib[6]
check("full weight equals level × pooled month index", abs(jul - exp_jul) < 1e-6, f"{jul:.3f} vs {exp_jul:.3f}")

half = apply_pooled_yearly(flat, mo_sib, 0.5)
jul_half = half.loc[half["ds"].dt.month == 7, "yhat"].mean()
check("half weight lands between flat and full", jan < jul_half < jul, f"{jul_half:.1f}")

# ── 4) borrowed shape widens the band (it's an assumption, not observed) ─────────
widened = apply_pooled_yearly(flat, mo_sib, 1.0, resid_std_norm=0.3, level=100.0)
base_halfwidth = (flat["yhat_upper"] - flat["yhat_lower"]).mean()
new_halfwidth = (widened["yhat_upper"] - widened["yhat_lower"]).mean()
check("pooled residual spread widens the interval", new_halfwidth > base_halfwidth,
      f"{new_halfwidth:.1f} > {base_halfwidth:.1f}")
check("bands stay ordered lower ≤ upper", bool((widened["yhat_lower"] <= widened["yhat_upper"]).all()))

print(f"\n=== {'ALL PASSED' if not _fails else str(len(_fails)) + ' FAILED: ' + str(_fails)} ===")
sys.exit(1 if _fails else 0)

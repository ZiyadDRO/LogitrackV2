"""One-off rushes, slow sellers' seasons and the season-aware 4-week check."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rush as RU            # noqa: E402
import seasonality as SE     # noqa: E402
import engines as EN         # noqa: E402

_pass = _fail = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  [PASS] {name}")
    else:
        _fail += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def frame(rate_fn, start="2023-01-01", end="2026-06-30", seed=1):
    r = np.random.default_rng(seed)
    ds = pd.date_range(start, end)
    lam = np.array([rate_fn(d) for d in ds], float)
    return pd.DataFrame({"ds": ds, "y": r.poisson(lam).astype(float)})


TODAY = pd.Timestamp("2026-07-01")
RUSH = (pd.Timestamp("2026-03-02"), pd.Timestamp("2026-03-29"))

print("— one-off rushes —")
one_off = frame(lambda d: 12.0 if RUSH[0] <= d <= RUSH[1] else 4.0)
r = RU.find(one_off, TODAY)
check("a 4-week rush at 3x that ended and wasn't there last year is found",
      len(r) == 1 and r[0]["start"] <= "2026-03-08" and r[0]["end"] >= "2026-03-22", str(r and [(x["start"], x["end"], x["ratio"]) for x in r]))
d = RU.deflate(one_off, r)
m = d[(d.ds >= RUSH[0]) & (d.ds <= RUSH[1])]["y"].mean()
check("taken out, those weeks look like ordinary weeks", 3.0 < m < 5.5, f"{m:.2f} a day")
check("days outside it are untouched",
      np.allclose(d[d.ds < RUSH[0] - pd.Timedelta(days=14)]["y"], one_off[one_off.ds < RUSH[0] - pd.Timedelta(days=14)]["y"]))
check("it's explained", "one-off" in (RU.explain(r) or ""), RU.explain(r))

yearly = frame(lambda d: 12.0 if (d.month == 3 and d.day <= 28) else 4.0)
check("the same weeks busy every year are a season, not a rush", RU.find(yearly, TODAY) == [],
      str(RU.find(yearly, TODAY)))

weak = frame(lambda d: 4.0 * ((1.3 if d.year == 2025 else 3.0) if (d.month == 3 and d.day <= 28) else 1.0),
             start="2022-01-01")
check("a peak that was weak last year but normal the years before is still a season", RU.find(weak, TODAY) == [],
      str(RU.find(weak, TODAY)))

step = frame(lambda d: 10.0 if d >= pd.Timestamp("2026-03-02") else 4.0)
check("a permanent step up (a competitor closing) is not a rush", RU.find(step, TODAY) == [])

ongoing = frame(lambda d: 12.0 if d >= pd.Timestamp("2026-06-08") else 4.0)
check("a rush still going on is left alone", RU.find(ongoing, TODAY) == [])

young = frame(lambda d: 12.0 if RUSH[0] <= d <= RUSH[1] else 4.0, start="2025-12-01")
check("with no year before and no similar products, nothing is taken out", RU.find(young, TODAY) == [])
peers = [frame(lambda d: 9.0 if RUSH[0] <= d <= RUSH[1] else 3.0, seed=s) for s in (5, 6, 7)]
got = RU.find(young, TODAY, peers=peers)
check("...but when similar products rushed then and not the year before, it's a one-off",
      len(got) == 1 and got[0]["source"] == "similar products", str(got))
seasonal_peers = [frame(lambda d: 9.0 if (d.month == 3 and d.day <= 28) else 3.0, seed=s) for s in (5, 6, 7)]
check("...and when they rush then every year, it's their season and stays in",
      RU.find(young, TODAY, peers=seasonal_peers) == [])

print("\n— slow sellers: the same weeks last year —")
season = lambda d: 1.0 + 6.0 * np.exp(-0.5 * ((d.dayofyear - 290) / 18.0) ** 2)
slow = frame(season, end="2026-09-30")
fut = pd.date_range("2026-10-01", periods=30)
yy = SE.same_weeks_last_year(slow, fut)
true = sum(season(d) for d in fut)
check("forecast from past years' same weeks lands near the true season",
      yy is not None and abs(sum(yy["daily"]) - true) / true < 0.25,
      f"{sum(yy['daily']) if yy else None:.1f} vs {true:.1f}" if yy else "None")
grown = frame(lambda d: season(d) * (1.5 if d >= pd.Timestamp("2025-10-01") else 1.0), end="2026-09-30")
yg = SE.same_weeks_last_year(grown, fut)
check("a product selling faster than a year ago is scaled up", yg is not None and yg["growth"] > 1.2, str(yg and yg["growth"]))
check("under a year of history: not used", SE.same_weeks_last_year(slow[slow.ds >= "2025-12-01"], fut) is None)
quiet = slow.copy(); quiet.loc[quiet.ds >= "2026-06-15", "y"] = 0.0
check("nothing sold for 13 weeks: not used (left to the fading rate)", SE.same_weeks_last_year(quiet, fut) is None)
gone = slow[slow.ds < "2026-09-10"]          # out of stock (days not on file) for the last 3 weeks
check("out of stock most of the last 4 weeks: not used (its pace now can't be seen)",
      SE.same_weeks_last_year(gone, fut) is None)
fading = slow.copy(); fading.loc[fading.ds >= "2026-09-01", "y"] = (fading.loc[fading.ds >= "2026-09-01", "y"] * 0).astype(float)
fading.loc[fading.ds == "2026-09-20", "y"] = 1.0
check("selling far below last year's pace lately: not used (left to the rate, which follows it)",
      SE.same_weeks_last_year(fading, fut) is None)
di = SE.daily_index(["2026-01-15", "2026-01-31", "2026-02-15"], [1.0, 2.0] + [1.0] * 10)
check("the monthly index runs smoothly between months", di[0] < di[1] < di[2], str(di))

print("\n— the 4-week check reads recent sales against the season —")
# Flat demand at the season's low point, a busy season ahead: sales lately must not be
# carried flat into the peak.
mo = [2.0, 1.5, 1.0, 0.7, 0.6, 0.6, 0.6, 0.7, 0.8, 1.0, 1.4, 2.1]
d0 = pd.date_range("2026-06-01", "2026-09-30")
low = pd.DataFrame({"ds": d0, "y": [4.0 * (1 + 0.0 * i) for i in range(len(d0))]})
flat = EN._recent_level_forecast(low, 90)
seas = EN._recent_level_forecast(low, 90, season=(mo, 1.0))
check("without a season it stays flat", abs(flat[-1] - flat[0]) < 1e-6)
check("with the season it rises into the busy months", seas[-7:].mean() > 1.5 * seas[:7].mean(),
      f"{seas[:7].mean():.2f} -> {seas[-7:].mean():.2f}")

print("\n— a strongly seasonal product after a permanent jump (the past-years check) —")
import logging as _lg
_lg.getLogger("cmdstanpy").disabled = True
_lg.getLogger("prophet").disabled = True
spring = lambda d: 1.0 + 12.0 * np.exp(-0.5 * ((d.dayofyear - 135) / 16.0) ** 2)
jump = lambda d: spring(d) * (1.3 if d >= pd.Timestamp("2025-02-01") else 1.0)
potting = frame(jump, start="2022-03-01", end="2025-10-10", seed=11)
cut = pd.Timestamp("2025-10-11")
nxt = pd.date_range(cut, periods=30)
truth30 = sum(jump(d) for d in nxt)
EN.PAST_YEARS_CHECK = False
off = EN.ProphetEngine().fit(potting, cut, False, False, None, [], related=[])
EN.PAST_YEARS_CHECK = True
on = EN.ProphetEngine().fit(potting, cut, False, False, None, [], related=[])
f_off = float(off.forecast_df[off.forecast_df.ds >= cut].head(30).yhat.clip(lower=0).sum())
f_on = float(on.forecast_df[on.forecast_df.ds >= cut].head(30).yhat.clip(lower=0).sum())
check("Prophet alone stays too high after the season (the jump pushed its level up)", f_off > 1.2 * truth30,
      f"{f_off:.0f} vs true {truth30:.0f}")
check("checked against past years, it lands much closer", abs(f_on - truth30) < 0.6 * abs(f_off - truth30),
      f"{f_on:.0f} vs true {truth30:.0f} (Prophet alone {f_off:.0f})")
check("and it says so", "past years" in ((on.explain_bits().get("recentCheck") or {}).get("text") or ""))
flatp = frame(lambda d: 5.0 * (1.3 if d >= pd.Timestamp("2025-02-01") else 1.0), start="2022-03-01", end="2025-10-10", seed=12)
fl = EN.ProphetEngine().fit(flatp, cut, False, False, None, [], related=[])
check("a product with no real season isn't checked against past years", getattr(fl, "_pastyears", None) is None)

print(f"\n=== {_pass} passed, {_fail} failed ===")
sys.exit(1 if _fail else 0)

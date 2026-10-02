"""
test_holiday_shape.py — a holiday stretch keeps the forecast's total; only where in the
stretch the units land follows past years, lined up on the holiday.

The promises tested:
  · the total for a stretch is always the forecast's own (doing better or worse this year
    moves every day; past years only decide the split)
  · a stretch cut off by the end of the forecast, or already under way, is split as a
    whole: the days there are never absorb the whole stretch
  · no day is in two stretches, whatever overlaps
  · a closed day takes no share; promotion days don't teach the shape (Events carry them,
    so a sale is never counted twice)
  · a slow seller's noise isn't shown as a holiday effect

Run:  python test_holiday_shape.py
"""
from __future__ import annotations

import datetime as dt
import sys
import os

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import holiday_shape as HS   # noqa: E402
import holiday_calendar as H  # noqa: E402

FAILURES = []
D = dt.date


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILURES.append(name)


def tg(y):
    return H._nth(y, 11, H.THU, 4)


def history(rate=10.0, bf=3.0, tg_day=0.2, years=(2022, 2023, 2024, 2025), end=D(2026, 9, 27), seed=1,
            promo_on_bf=False, noise=True):
    """Daily sales: flat `rate`, Black Friday `bf`x, Thanksgiving itself `tg_day`x."""
    rng = np.random.default_rng(seed)
    ds = pd.date_range(D(years[0], 1, 1), end)
    lam = np.full(len(ds), rate)
    promo = np.zeros(len(ds))
    for y in years:
        t = tg(y)
        for i, x in enumerate(ds.date):
            if x == t:
                lam[i] = rate * tg_day
            if x == t + dt.timedelta(days=1):
                lam[i] = rate * bf
                promo[i] = 1.0 if promo_on_bf else 0.0
    y_ = rng.poisson(lam).astype(float) if noise else lam
    return pd.DataFrame({"ds": ds, "y": y_, "on_promotion": promo})


print("stretches and overlaps")
A = HS.assign(D(2026, 1, 1), D(2027, 12, 31))
owners = {}
for d, (k, a, o) in A.items():
    owners.setdefault(d, []).append(k)
check("no day is in two stretches", all(len(v) == 1 for v in owners.values()))
t26 = tg(2026)
check("Thanksgiving's stretch runs a week before it to a week after Cyber Monday",
      A.get(t26 - dt.timedelta(days=7), ("",))[0] == "thanksgiving_week"
      and A.get(t26 + dt.timedelta(days=11), ("",))[0] == "thanksgiving_week"
      and A.get(t26 + dt.timedelta(days=12), ("",))[0] != "thanksgiving_week")
check("Black Friday is offset 1 from Thanksgiving every year",
      A[t26 + dt.timedelta(days=1)][2] == 1 and HS.assign(tg(2027), tg(2027) + dt.timedelta(days=1))[tg(2027) + dt.timedelta(days=1)][2] == 1)
v27 = HS.assign(D(2027, 2, 1), D(2027, 2, 20))
check("the 2027 Super Bowl lands on Valentine's Day: one stretch, not two",
      v27[D(2027, 2, 14)][0] == "valentines", v27.get(D(2027, 2, 14)))
# A chain: Easter 2027 is Mar 28, St. Patrick's Mar 17: each day goes to the nearer core.
e27 = HS.assign(D(2027, 3, 10), D(2027, 4, 5))
check("in an overlap each day goes to the nearer holiday",
      e27[D(2027, 3, 18)][0] == "st_patricks" and e27[D(2027, 3, 20)][0] == "easter",
      (e27.get(D(2027, 3, 18)), e27.get(D(2027, 3, 20))))

print("\nlearning the shape")
h = history()
m = HS.measure(h)
lv = HS.levels(m, {})
r = lv["thanksgiving_week"]["r"]
check("Black Friday reads well above the stretch (about 3x, pulled a little toward ordinary on 4 years)",
      2.2 < r[1] < 3.4, r.get(1))
check("Thanksgiving itself reads quiet", r[0] < 0.45, r.get(0))
check("an ordinary day in the stretch reads about even", 0.85 < r[-5] < 1.15, r.get(-5))
check("and it shows as clear", lv["thanksgiving_week"]["zHigh"] >= HS.SHOW_Z, lv["thanksgiving_week"]["zHigh"])

slow = HS.levels(HS.measure(history(rate=0.7, bf=1.0, tg_day=1.0, seed=4)), {})
check("a slow seller with no holiday effect isn't shown as one",
      slow["thanksgiving_week"]["zHigh"] < HS.SHOW_Z and slow["thanksgiving_week"]["zLow"] > -HS.SHOW_Z,
      (slow["thanksgiving_week"]["zHigh"], slow["thanksgiving_week"]["zLow"]))
check("and its levels stay within noise", max(abs(v - 1) for v in slow["thanksgiving_week"]["r"].values()) < 0.5,
      max(abs(v - 1) for v in slow["thanksgiving_week"]["r"].values()))

check("a clear holiday reshapes the forecast", HS.significant(lv["thanksgiving_week"]))
check("noise doesn't (those weeks stay as the normal forecast)", not HS.significant(slow["thanksgiving_week"]))

pr = HS.levels(HS.measure(history(promo_on_bf=True)), {})
check("a spike that was always on a promotion teaches no shape (Events carry it)",
      abs(pr["thanksgiving_week"]["r"].get(1, 1.0) - 1.0) < 0.3, pr["thanksgiving_week"]["r"].get(1))

print("\nwhen habits shift, recent years count more")


def shifted(seed, rate=10.0):
    """The rush was on Black Friday in 2022-23; from 2024 the deals start the Saturday
    before, and that's where it stays."""
    rng = np.random.default_rng(seed)
    ds = pd.date_range(D(2022, 1, 1), D(2026, 9, 27))
    lam = np.full(len(ds), rate)
    for y in range(2022, 2026):
        peak = tg(y) + dt.timedelta(days=1 if y < 2024 else -5)
        lam[[i for i, x in enumerate(ds.date) if x == peak][0]] = 3.5 * rate
    return pd.DataFrame({"ds": ds, "y": rng.poisson(lam).astype(float), "on_promotion": 0.0})


def err_vs_now(decay, seed):
    HS.YEAR_DECAY = decay
    r = HS.levels(HS.measure(shifted(seed)), {})["thanksgiving_week"]["r"]
    offs = sorted(o for o in r if -7 <= o <= 11)
    truth = {o: (3.5 if o == -5 else 1.0) for o in offs}
    t_sum, r_sum = sum(truth.values()), sum(r[o] for o in offs)
    return sum(abs(r[o] / r_sum - truth[o] / t_sum) for o in offs)


_keep = HS.YEAR_DECAY
e_flat = np.mean([err_vs_now(1.0, s) for s in range(5)])
e_rec = np.mean([err_vs_now(0.75, s) for s in range(5)])
HS.YEAR_DECAY = _keep
check("the spread follows the new habit more closely with recent years weighted up",
      e_rec < e_flat, (round(e_flat, 3), round(e_rec, 3)))
busy = HS.levels(HS.measure(shifted(3, rate=40.0)), {})["thanksgiving_week"]["r"]
small = HS.levels(HS.measure(shifted(3, rate=3.0)), {})["thanksgiving_week"]["r"]
check("a busy product's new spike the Saturday before is read as a spike, not smeared over the week",
      busy[-5] > 2.0 * np.median([busy[o] for o in range(-7, 12) if o in busy]), busy.get(-5))
check("a small product's run-up days are still read with their neighbours (too few units to trust one day)",
      small[-5] < 1.6, small.get(-5))

print("\nsplitting this year's total")
wdi = np.ones(7)
ident = lambda x: x                                       # noqa: E731
span = [t26 + dt.timedelta(days=i) for i in range(-7, 12)]
base = np.full(len(span), 10.0)
new, info = HS.redistribute(span, base, ident, A, lv, wdi)
check("the stretch keeps the forecast's total", abs(new.sum() - base.sum()) < 1e-6, (new.sum(), base.sum()))
i_bf = span.index(t26 + dt.timedelta(days=1))
check("Black Friday gets the biggest share", int(np.argmax(new)) == i_bf and new[i_bf] > 2.2 * np.median(new),
      (int(np.argmax(new)), new[i_bf], np.median(new)))
if HS.BLEND_DAYS > 0:
    check("the stretch's first and last days stay close to the normal forecast (no step at the edges)",
          abs(new[0] - base[0]) < 0.25 * base[0] and abs(new[-1] - base[-1]) < 0.25 * base[-1], (new[0], new[-1]))
new2, _ = HS.redistribute(span, base * 1.5, ident, A, lv, wdi)
check("doing 50% better this year: every day moves 50%, the split stays", np.allclose(new2, new * 1.5))
around = [D(2027, 1, 15) + dt.timedelta(days=i) for i in range(10)]   # between New Year's and Valentine's
check("(those days really are outside every stretch)", not any(d in HS.assign(D(2027, 1, 1), D(2027, 2, 28)) for d in around))
same, _ = HS.redistribute(around, np.full(10, 7.0), ident, A, lv, wdi)
check("days outside every stretch are untouched", np.allclose(same, 7.0))

print("\na stretch cut off by the end of the forecast")
cut = span[:6]                                            # ends before Thanksgiving
part, _ = HS.redistribute(cut, base[:6], ident, A, lv, wdi)
check("the days there keep only their own share (no spike crammed in)",
      abs(part.sum() - base[:6].sum()) < 1e-6 and part.max() < 1.3 * base[:6].mean(), part.round(2))

print("\na stretch already under way")
sold = {d: 10.0 for d in span[:9]}                        # through Black Friday, flattened
rest = span[9:]
left, inf2 = HS.redistribute(rest, np.full(len(rest), 10.0), ident, A, lv, wdi, past=sold)
full_share = new[9:]
check("the days left get what they'd have got in the whole stretch",
      np.allclose(left, full_share, rtol=0.02), (left.round(2), full_share.round(2)))
check("they never take the whole stretch's spike", left.max() < 1.4 * 10.0, left.max())

print("\nclosed days")
closed = np.array([d == t26 for d in span])
cz, _ = HS.redistribute(span, base, ident, A, lv, wdi, closed=closed)
check("a closed day takes no share (left as it was, to be zeroed by the closed-day step)",
      cz[span.index(t26)] == base[span.index(t26)])
open_tot = base[~closed].sum()
check("the open days share the open days' total", abs(cz[~closed].sum() - open_tot) < 1e-6)

print("\nsimilar products fill in for a product with no past holiday")
new_prod = history(years=(2026,), end=D(2026, 9, 27))       # launched this year
peers = [HS.measure(history(seed=s)) for s in (7, 8, 9)]
lvn = HS.levels(HS.measure(new_prod), HS.pool(peers))
check("it takes their Black Friday", lvn["thanksgiving_week"]["r"].get(1, 1.0) > 1.8,
      lvn["thanksgiving_week"]["r"].get(1))
check("and says it has none of its own", lvn["thanksgiving_week"]["years"] == 0)

print(f"\n{'All holiday-shape tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)

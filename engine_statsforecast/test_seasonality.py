"""
Seasonality for the intermittent route.

The headline behaviour is easy; the trap is double-application. A rate measured over a
partial year is ALREADY seasonally biased, and multiplying it by a peak index on top
orders twice as much as it should. Most of these tests are about that.
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seasonality as S   # noqa: E402

_pass = _fail = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  [PASS] {name}")
    else:
        _fail += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


WINTER = {1: 4.0, 2: 3.0, 3: 1.5, 4: 0.5, 5: 0.3, 6: 0.2,
          7: 0.2, 8: 0.3, 9: 0.5, 10: 1.5, 11: 3.0, 12: 4.0}


def seasonal(days=730, start="2024-01-01", shape=None, base=2.0):
    shape = shape or WINTER
    rows = []
    d = pd.Timestamp(start)
    for i in range(days):
        day = d + pd.Timedelta(days=i)
        rows.append({"ds": day, "y": base * shape[day.month]})
    return pd.DataFrame(rows)


def flat(days=730, start="2024-01-01", base=2.0):
    return seasonal(days, start, {m: 1.0 for m in range(1, 13)}, base)


print("\n— a snow shovel's season is found in its own history —")
r = S.monthly_index(seasonal())
check("it reads as own-history", r["basis"] == "own-history", str(r["basis"]))
check("January is the peak", r["index"].index(max(r["index"])) == 0, str(r["index"]))
check("June/July are the trough", r["index"].index(min(r["index"])) in (5, 6), str(r["index"]))
check("January runs well above average", r["index"][0] > 1.8, str(r["index"][0]))
check("July runs well below", r["index"][6] < 0.6, str(r["index"][6]))
check("it is recognised as seasonal", r["strength"] > 0.5, str(r["strength"]))
check("the index averages to 1.0 — it redistributes, never inflates",
      abs(sum(r["index"]) / 12 - 1.0) < 0.01, str(sum(r["index"]) / 12))

print("\n— a flat product gets no season, and no nudge —")
f = S.monthly_index(flat())
check("strength is ~zero", f["strength"] < 0.05, str(f["strength"]))
check("every multiplier is exactly 1.0",
      all(m == 1.0 for m in S.future_multipliers(["2026-01-05", "2026-07-05"], f["index"])),
      str(S.future_multipliers(["2026-01-05", "2026-07-05"], f["index"])))

print("\n— under a year, it refuses —")
short = S.monthly_index(seasonal(days=200))
check("it says the history is too short", short["basis"] == "too-short", str(short))
check("...and returns a flat index", short["index"] == [1.0] * 12)
check("a season and a trend can't be told apart yet", short["strength"] == 0.0)

print("\n— evidence is shrunk: one year counts for less than three —")
one = S.monthly_index(seasonal(days=365))
three = S.monthly_index(seasonal(days=1095))
check("one year keeps roughly half the swing", 1.4 < one["index"][0] < 2.6, str(one["index"][0]))
check("three years keep more", three["index"][0] > one["index"][0],
      f"{three['index'][0]} vs {one['index'][0]}")
check("both agree on WHICH month peaks",
      one["index"].index(max(one["index"])) == three["index"].index(max(three["index"])))

print("\n— an intermittent product borrows its cohort's season —")
peers = [seasonal(), seasonal(base=5.0), seasonal(base=0.5)]
coh = S.cohort_monthly_index(peers)
check("a cohort index is built", coh["basis"] == "cohort", str(coh["basis"]))
check("...from all three peers", coh["peers"] == 3)
check("...with the same peak", coh["index"].index(max(coh["index"])) == 0, str(coh["index"]))
check("volume doesn't dominate shape — a 10x peer doesn't swamp it",
      abs(coh["index"][0] - S.cohort_monthly_index([seasonal(), seasonal(base=50.0)])["index"][0]) < 0.3,
      "shape should be scale-free")
check("one peer is not a cohort", S.cohort_monthly_index([seasonal()])["basis"] == "too-few-peers")

sparse = seasonal(days=200)          # too short for its own index
res = S.resolve(sparse, peers)
check("a sparse product resolves to the cohort", res["source"] == "cohort", str(res.get("source")))
check("...and says its own history was too short", res["ownBasis"] == "too-short")
check("...and gets a real seasonal shape", res["strength"] > 0.4, str(res["strength"]))

print("\n— THE TRAP: a season must never be applied twice —")
# A rate measured across a FULL year is already season-neutral.
full = seasonal(days=730)
idx = S.monthly_index(full)["index"]
cov_full = S.coverage_factor(full, idx)
check("a full year of training has neutral seasonal context",
      abs(cov_full - 1.0) < 0.05, str(cov_full))

# A rate measured across WINTER ONLY is already inflated.
winter_only = seasonal(days=730)
winter_only = winter_only[winter_only["ds"].dt.month.isin([11, 12, 1, 2])]
cov_winter = S.coverage_factor(winter_only, idx)
check("a winter-only training window is seasonally elevated", cov_winter > 1.5, str(cov_winter))
jan_raw = S.future_multipliers(["2027-01-15"], idx, coverage=1.0)[0]
jan_corrected = S.future_multipliers(["2027-01-15"], idx, coverage=cov_winter)[0]
check("without correction January would be scaled up again", jan_raw > 1.8, str(jan_raw))
check("...and the correction brings it back to about neutral",
      0.8 < jan_corrected < 1.3, str(jan_corrected))
check("...because the rate already contained the peak", jan_corrected < jan_raw)

summer_only = seasonal(days=730)
summer_only = summer_only[summer_only["ds"].dt.month.isin([6, 7, 8])]
cov_summer = S.coverage_factor(summer_only, idx)
check("a summer-only window is seasonally depressed", cov_summer < 0.7, str(cov_summer))
check("...so January is scaled up MORE, not less",
      S.future_multipliers(["2027-01-15"], idx, coverage=cov_summer)[0] > jan_raw,
      "a rate learned in the quiet season must be lifted further for the peak")

print("\n— the multipliers point the right way through the year —")
dates = [f"2027-{m:02d}-15" for m in range(1, 13)]
mult = S.future_multipliers(dates, idx)
check("January orders more", mult[0] > 1.5, str(mult[0]))
check("July orders less", mult[6] < 0.6, str(mult[6]))
check("the busy month is the biggest multiplier", mult.index(max(mult)) == 0)
check("ordering rises BEFORE the season, not after",
      mult[10] > 1.0 and mult[11] > 1.0, f"Nov {mult[10]}, Dec {mult[11]}")

print("\n— a weak season is left alone —")
mild = S.monthly_index(seasonal(shape={m: (1.05 if m in (1, 12) else 1.0) for m in range(1, 13)}))
check("a 5% wobble is below the threshold", mild["strength"] < S.MIN_STRENGTH, str(mild["strength"]))
check("...so nothing is scaled", all(m == 1.0 for m in S.future_multipliers(dates, mild["index"])))
check("resolve() reports no usable season",
      S.resolve(seasonal(shape={m: 1.0 for m in range(1, 13)}), [])["source"] == "none")

print("\n— extremes are clipped —")
crazy = S.monthly_index(seasonal(days=1460, shape={m: (50.0 if m == 1 else 0.01) for m in range(1, 13)}))
check("no month exceeds the cap", max(crazy["index"]) <= S.MAX_INDEX, str(max(crazy["index"])))
check("no month falls below the floor", min(crazy["index"]) >= S.MIN_INDEX * 0.9, str(min(crazy["index"])))

print("\n— it explains itself —")
e = S.explain(S.resolve(seasonal(), []))
check("it names the peak month", "January" in e, e)
check("...and the quiet one", "July" in e or "June" in e, e)
check("...and what it does for ordering", "ahead of the busy season" in e, e)
e2 = S.explain(S.resolve(seasonal(days=200), peers))
check("a borrowed season says it is borrowed", "similar products" in e2, e2)
check("too little history is explained", "full year is needed" in S.explain(S.resolve(seasonal(days=200), [])),
      S.explain(S.resolve(seasonal(days=200), [])))
check("nothing measured means nothing claimed", S.explain({}) == "")

print("\n— malformed input can't take it down —")
check("an empty frame is safe", S.monthly_index(pd.DataFrame())["basis"] == "no-data")
check("None is safe", S.monthly_index(None)["basis"] == "no-data")
check("no peers is safe", S.cohort_monthly_index(None)["basis"] == "too-few-peers")
check("bad dates are safe", S.future_multipliers(["not-a-date"], idx) == [1.0])
check("coverage on an empty frame is neutral", S.coverage_factor(pd.DataFrame(), idx) == 1.0)

print(f"\n=== {_pass} passed, {_fail} failed ===")
sys.exit(1 if _fail else 0)

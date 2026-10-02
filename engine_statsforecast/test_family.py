"""
test_family.py — who a product borrows from.

  · A product left at category level borrows from the WHOLE category, including the
    products that earned a subgroup of their own.
  · Holiday patterns are borrowed from the whole category, whatever subgroup a product is in.
  · In the holiday pool no product counts for more than a typical member.
  · A holiday stretch still under way isn't evidence of how stretches are shaped.

Run:  python test_family.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np            # noqa: E402
import pandas as pd           # noqa: E402
import router as R            # noqa: E402
import holiday_shape as HS    # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILURES.append(name)


rng = np.random.default_rng(1)
DAYS = pd.date_range("2023-01-01", "2025-06-30")


def frame(peak_month, lam=20.0):
    m = DAYS.month.to_numpy()
    season = np.where(m == peak_month, 2.5, 1.0)
    return pd.DataFrame({"ds": DAYS, "y": rng.poisson(lam * season).astype(float)})


# Kitchen: three pans that earned a subgroup, and one kettle left at category level. All
# four peak in July. Garden: three products peaking in April (the rest of the store).
cat = {f"PAN{i}": {"df": frame(7)} for i in range(3)}
cat["KETTLE"] = {"df": frame(7)}
cat.update({f"G{i}": {"df": frame(4)} for i in range(3)})
groups = {**{f"PAN{i}": "Kitchen | subcategory:Pans" for i in range(3)}, "KETTLE": "Kitchen",
          **{f"G{i}": "Garden" for i in range(3)}}
meta = {**{f"PAN{i}": {"level": "subgroup", "baseGroup": "Kitchen"} for i in range(3)},
        "KETTLE": {"level": "base", "baseGroup": "Kitchen"},
        **{f"G{i}": {"level": "base", "baseGroup": "Garden"} for i in range(3)}}
fam = R.family_map(groups, meta)
bl = R.catalog_baseline(cat)

print("a product left at category level")
old = R.cluster_catalog(groups, cat, baseline=bl)
_, n_old, _ = R.behavioral_relatives("KETTLE", groups, cat, old, baseline=bl)
check("used to have no one to borrow from", n_old == 0, n_old)
new = R.cluster_catalog(groups, cat, baseline=bl, family=fam)
frames, n_new, info = R.behavioral_relatives("KETTLE", groups, cat, new, baseline=bl)
check("now borrows from the pans in its category", n_new == 3 and set(info["chosen"]) == {"PAN0", "PAN1", "PAN2"},
      info)
check("never from another category", not any(m.startswith("G") for cl in new["Kitchen"] for m in cl))
_, n_pan, info_pan = R.behavioral_relatives("PAN0", groups, cat, new, baseline=bl)
check("a subgrouped product still borrows from its own subgroup", set(info_pan["chosen"]) == {"PAN1", "PAN2"},
      info_pan)

print("\nholiday patterns come from the whole category")
ids = lambda frames_: {next(k for k, v in cat.items() if v["df"] is f) for f in frames_}   # noqa: E731
check("a pan borrows from the kettle too", ids(R.category_peers("PAN0", groups, cat, family=fam))
      == {"PAN1", "PAN2", "KETTLE"})
check("...where it used to see only the other pans", ids(R.category_peers("PAN0", groups, cat)) == {"PAN1", "PAN2"})
check("the kettle borrows from the pans", ids(R.category_peers("KETTLE", groups, cat, family=fam))
      == {"PAN0", "PAN1", "PAN2"})

print("\nno product outweighs a typical member")
flat = {"num": {o: 1.0 for o in range(-3, 2)}, "den": {o: 1.0 for o in range(-3, 2)},
        "var": {o: 1.0 for o in range(-3, 2)}, "core": {0}, "phi": 1.0}


def rec(scale, peak):
    r = {k: (dict(v) if isinstance(v, dict) else v) for k, v in flat.items()}
    r["num"] = {o: scale * (peak if o == 0 else 1.0) for o in flat["num"]}
    r["den"] = {o: scale * 1.0 for o in flat["den"]}
    r["var"] = {o: scale * 1.0 for o in flat["den"]}
    return {"xmas": r}


# One big seller whose peak day is 4x; nine small ones whose peak is 1.5x.
p = HS.pool([rec(200.0, 4.0)] + [rec(5.0, 1.5)] * 9)["xmas"]
share_big = (5.0 * 4.0) / (p["num"][0])
check("the big seller is weighed as a typical member", abs(p["den"][0] - 10 * 5.0) < 1e-9, p["den"][0])
check("so the family's peak reads mostly as the family's", p["num"][0] / p["den"][0] < 2.0,
      p["num"][0] / p["den"][0])
p2 = HS.pool([rec(5.0, 1.5), rec(1.0, 9.0)])["xmas"]
# totals 25 and 5, typical (median) 15: the bigger is trimmed to 15/25, the thin one stays 1x
check("a thin product is not scaled up (only the bigger one is trimmed)",
      abs(p2["den"][0] - (5.0 * 15 / 25 + 1.0)) < 1e-9, p2["den"][0])

print("\na holiday stretch still under way")
d = pd.DataFrame({"ds": pd.date_range("2022-11-01", "2024-12-24")})
d["y"] = 10.0
m = HS.measure(d)["christmas_newyear"]
yrs = {y["anchor"][:4]: y for y in m["years"]}
check("last year's finished stretch is evidence", yrs["2023"]["full"] and yrs["2023"]["weight"] > 0, yrs.get("2023"))
check("this year's, half done, is not", not yrs["2024"]["full"] and yrs["2024"]["weight"] == 0, yrs.get("2024"))

print("\na product alone in its category")
cat2 = dict(cat); groups2 = dict(groups); meta2 = dict(meta)
cat2["BIKE"] = {"df": frame(7)}                       # peaks in July, like the kitchen products
cat2["NEW"] = {"df": frame(7).tail(60).reset_index(drop=True)}   # too new to have a shape
groups2.update({"BIKE": "Bikes", "NEW": "Scooters"})
meta2.update({"BIKE": {"level": "base", "baseGroup": "Bikes"}, "NEW": {"level": "base", "baseGroup": "Scooters"}})
fam2 = R.family_map(groups2, meta2)
bl3 = R.catalog_baseline(cat2)


def ids2(frames_):
    return {next(k for k, v in cat2.items() if v["df"] is f) for f in frames_}


got = ids2(R.category_peers("BIKE", groups2, cat2, family=fam2, baseline=bl3))
check("borrows only from store products that move with it", got and got <= {"PAN0", "PAN1", "PAN2", "KETTLE"}, got)
check("...and not from the ones that don't", not (got & {"G0", "G1", "G2"}), got)
check("one too new to show a shape borrows nothing", R.category_peers("NEW", groups2, cat2, family=fam2, baseline=bl3) == [])
nocat = {k: None for k in cat2}
got_nc = ids2(R.category_peers("BIKE", nocat, cat2, baseline=bl3))
check("in a store with no categories at all, the same rule", got_nc and not (got_nc & {"G0", "G1", "G2"}), got_nc)
check("the store-wide price comparison group is left alone",
      len(R.category_peers("BIKE", {}, cat2, limit=40)) == len(cat2) - 1)

print("\na young product picks a family only when its sales match one")
cat3 = {**{f"W{i}": {"df": frame(1)} for i in range(3)}, **{f"S{i}": {"df": frame(7)} for i in range(3)}}
g3 = {k: "Apparel" for k in cat3}
YD = pd.date_range("2025-02-20", "2025-09-25")


def young(peak_month=None):
    m = YD.month.to_numpy()
    mu = 8.0 * (np.where(m == peak_month, 2.5, 1.0) if peak_month else np.ones(len(m)))
    return pd.DataFrame({"ds": YD, "y": rng.poisson(mu).astype(float)})


def pick(df_):
    c = dict(cat3); c["Y"] = {"df": df_}
    gg = dict(g3); gg["Y"] = "Apparel"
    b_ = R.catalog_baseline(c)
    cl_ = R.cluster_catalog(gg, c, baseline=b_)
    return R.behavioral_relatives("Y", gg, c, cl_, baseline=b_)[2]


i_match = pick(young(7))
check("one whose own months match the summer family borrows from it",
      set(i_match["chosen"]) == {"S0", "S1", "S2"}, i_match)
i_none = pick(young(4))                                   # an April peak: neither family's shape
check("one whose months match neither family borrows none (was: the best weak match)",
      i_none["chosen"] == [] and "match no family" in i_none["basis"], i_none)
i_new = pick(young(7).tail(50).reset_index(drop=True))
check("one too new to judge still goes by sales volume, as before",
      i_new["chosen"] != [] and "volume" in i_new["basis"], i_new)

print("\ngrowth is not a season")
import trend as TRD                                        # noqa: E402
import seasonality as SEA                                  # noqa: E402
LONG = pd.date_range("2023-10-01", "2026-09-20")


def grow(prof, g, days=LONG, lam=20.0):
    t = np.arange(len(days)) / 365.25
    mu = lam * prof[days.month - 1] * np.exp(g * (t - t.mean()))
    return pd.DataFrame({"ds": days, "y": rng.poisson(mu).astype(float)})


def rand_season(k):
    p = rng.normal(0, 1, 12); p -= p.mean()
    return np.exp(k * p / p.std())


g = grow(np.ones(12), 0.7)
b_, basis = TRD.slope(g["ds"], g["y"])
check("a product growing 2x a year is measured as growing about that fast", abs(b_ - 0.7) < 0.15 and basis == "seasonal-fit",
      (b_, basis))
pairs = [(grow(rand_season(.3), rng.uniform(.5, 1.0)), grow(rand_season(.3), rng.uniform(.5, 1.0))) for _ in range(30)]
bl2 = R.catalog_baseline({str(i): {"df": grow(rand_season(rng.uniform(0, .5)), rng.choice([0, .4]))} for i in range(30)})
hits = sum(R.distinct_corr(R._seasonal_signature(a), R._seasonal_signature(b), bl2) >= R.DISTINCT_THRESHOLD
           for a, b in pairs)
check("unrelated growing products are rarely called a family (was ~70%)", hits / len(pairs) < 0.45, hits / len(pairs))
fam_prof = rand_season(.5)
fam_hits = sum(R.distinct_corr(R._seasonal_signature(grow(fam_prof, 0.0)),
                               R._seasonal_signature(grow(fam_prof, rng.uniform(.4, 1.0))), bl2) >= R.DISTINCT_THRESHOLD
               for _ in range(10))
check("a real family still matches when one member grows and one doesn't", fam_hits == 10, fam_hits)
idx = SEA.monthly_index(grow(np.ones(12), 0.9))
check("a growing product with no season gets no season in its yearly index", idx["strength"] < 0.15, idx["strength"])

print("\nthe yearly blend's agreement check is on the specific scale")
import engines as ENG                                      # noqa: E402
store_bump = np.ones(12); store_bump[11] = 2.0            # every product in the store: December x2
sibs = [grow(store_bump * rand_season(.15), 0.0, days=pd.date_range("2024-06-01", "2026-09-20")) for _ in range(4)]
raw = ENG.pooled_monthly_cohesion(sibs)
spec = ENG.pooled_monthly_cohesion(sibs, baseline_mo=store_bump / store_bump.mean())
check("siblings sharing only the store's December bump look alike raw", raw > 0.5, raw)
check("...but not once the store's own shape is taken out", spec < ENG.BLEND_MIN_COHESION, spec)
own = rand_season(.5)
fam_sibs = [grow(store_bump * own * rand_season(.1), 0.0, days=pd.date_range("2024-06-01", "2026-09-20")) for _ in range(4)]
spec_f = ENG.pooled_monthly_cohesion(fam_sibs, baseline_mo=store_bump / store_bump.mean())
check("a family with a season of its own still clears the bar", spec_f >= ENG.BLEND_MIN_COHESION, spec_f)

print(f"\n{'All family tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)

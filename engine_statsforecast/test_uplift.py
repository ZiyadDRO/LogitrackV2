"""
Price and promo effects for the non-Prophet routes.

The whole point is a multiplier applied to real orders, so most of these tests are about
when it must be 1.0 — a fabricated lift looks like knowledge and moves stock.
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uplift as U   # noqa: E402

_pass = _fail = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  [PASS] {name}")
    else:
        _fail += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def frame(days=200, promo_idx=(), promo_mult=3.0, base=10.0, price=None):
    rows = []
    for i in range(days):
        on = 1 if i in promo_idx else 0
        rows.append({"ds": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
                     "y": base * (promo_mult if on else 1.0),
                     "on_promotion": on,
                     **({"price": price(i)} if price else {})})
    return pd.DataFrame(rows)


print("\n— a promo lift is measured from the product's own history —")
r = U.promo_lift(frame(promo_idx=range(100, 130)))
check("it finds the lift", r["basis"] == "own-history", str(r))
check("the raw ratio is right", r["raw"] == 3.0, str(r["raw"]))
check("...but it is shrunk toward no-effect", r["multiplier"] < 3.0, str(r["multiplier"]))
check("...and with 30 promo days it keeps most of it", r["multiplier"] > 2.6, str(r["multiplier"]))
check("the promo day count is reported", r["promoDays"] == 30)

print("\n— thin evidence is shrunk hard —")
few = U.promo_lift(frame(promo_idx=range(100, 104)))
many = U.promo_lift(frame(promo_idx=range(100, 160)))
check("4 promo days keep less than 60% of the lift", few["multiplier"] < 2.2, str(few["multiplier"]))
check("60 promo days keep more than 90%", many["multiplier"] > 2.8, str(many["multiplier"]))
check("more evidence always means more of the effect",
      many["multiplier"] > few["multiplier"], f"{many['multiplier']} vs {few['multiplier']}")

print("\n— too few promo days means no claim at all —")
two = U.promo_lift(frame(promo_idx=(100, 101)))
check("2 days is not enough", two["basis"] == "too-few-promo-days", str(two))
check("...so the multiplier is exactly 1", two["multiplier"] == 1.0)
check("a product that never promoted says so",
      U.promo_lift(frame())["basis"] == "too-few-promo-days")
check("no promo column at all is handled",
      U.promo_lift(pd.DataFrame([{"ds": pd.Timestamp("2026-01-01"), "y": 5}]))["basis"] == "no-promotion-data")

print("\n— a wild ratio is capped —")
wild = U.promo_lift(frame(promo_idx=range(100, 160), promo_mult=50.0))
check("50x is not passed through", wild["multiplier"] <= U.MAX_PROMO_LIFT, str(wild["multiplier"]))
check("...and the cap is recorded", wild["capped"] is True)
check("a 'promo' that halves sales is floored",
      U.promo_lift(frame(promo_idx=range(100, 160), promo_mult=0.05))["multiplier"] >= U.MIN_PROMO_LIFT)

print("\n— a new product borrows what similar products see —")
cohort = U.cohort_promo_lift([frame(promo_idx=range(100, 140)), frame(promo_idx=range(50, 90))])
check("a cohort lift is computed", cohort and cohort > 2.5, str(cohort))
newp = U.promo_lift(frame(days=20), cohort_lift=cohort)
check("the new product uses it", newp["basis"] == "cohort", str(newp))
check("...and says where it came from", newp["cohortLift"] == cohort)
check("without a cohort it stays at 1.0", U.promo_lift(frame(days=20))["multiplier"] == 1.0)
check("a cohort of one is not a cohort", U.cohort_promo_lift([frame(promo_idx=range(100, 140))]) is None)

print("\n— price elasticity needs prices that actually moved —")
flat = U.price_elasticity(frame(days=200, price=lambda i: 30.0))
check("a fixed price reveals nothing", flat["basis"] == "price-never-varied", str(flat))
check("...so elasticity is zero", flat["elasticity"] == 0.0)
check("no price column at all is handled",
      U.price_elasticity(frame(days=100))["basis"] == "no-price-data")
check("too few points is refused",
      U.price_elasticity(frame(days=10, price=lambda i: 30.0 - i))["basis"] == "too-few-points")

print("\n— a real price response is measured —")
import math
def elastic_frame(e=-1.5, days=200):
    rows = []
    for i in range(days):
        p = 20.0 + (i % 10) * 1.5           # prices genuinely move
        q = 100.0 * (p / 20.0) ** e
        rows.append({"ds": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
                     "y": q, "price": p, "on_promotion": 0})
    return pd.DataFrame(rows)
er = U.price_elasticity(elastic_frame(-1.5))
check("it recovers the sign", er["elasticity"] < 0, str(er))
check("...and roughly the magnitude", -1.5 < er["elasticity"] < -1.0, str(er["elasticity"]))
check("...shrunk toward zero on 200 days", abs(er["elasticity"]) < 1.5, str(er["elasticity"]))
check("it reports the spread it saw", er["spread"] > 0.3, str(er["spread"]))

print("\n— demand rising with price is treated as noise, not licence —")
def perverse(days=200):
    rows = []
    for i in range(days):
        p = 20.0 + (i % 10) * 1.5
        rows.append({"ds": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
                     "y": p * 5, "price": p, "on_promotion": 0})
    return pd.DataFrame(rows)
pv = U.price_elasticity(perverse())
check("a positive elasticity is discarded", pv["elasticity"] == 0.0, str(pv))
check("...and the reason is named", pv["basis"] == "positive-elasticity-ignored")
check("...so raising prices never raises the forecast",
      U.price_multiplier(pv["elasticity"], 20, 30) == 1.0)

print("\n— the multipliers turn into a forecast adjustment —")
check("a price cut raises demand", U.price_multiplier(-1.5, 20, 18) > 1.0,
      str(U.price_multiplier(-1.5, 20, 18)))
check("a price rise lowers it", U.price_multiplier(-1.5, 20, 25) < 1.0)
check("no change is neutral", U.price_multiplier(-1.5, 20, 20) == 1.0)
check("zero elasticity is neutral", U.price_multiplier(0, 20, 40) == 1.0)
check("junk prices are neutral", U.price_multiplier(-1.5, 0, 40) == 1.0)

dates = [f"2026-09-{d:02d}" for d in range(1, 11)]
evs = [{"type": "promotion", "date": "2026-09-03", "end_date": "2026-09-05"}]
mult = U.future_multipliers(dates, evs, 2.5)
check("only the promo days are lifted",
      [m for m, d in zip(mult, dates) if d in ("2026-09-03", "2026-09-04", "2026-09-05")] == [2.5, 2.5, 2.5],
      str(mult))
check("...and every other day is untouched",
      all(m == 1.0 for m, d in zip(mult, dates) if d not in ("2026-09-03", "2026-09-04", "2026-09-05")),
      str(mult))
check("no scheduled promo means no lift anywhere",
      all(m == 1.0 for m in U.future_multipliers(dates, [], 2.5)))
check("a non-promotion event is ignored",
      all(m == 1.0 for m in U.future_multipliers(dates, [{"type": "price_change_permanent", "date": "2026-09-03"}], 2.5)))

print("\n— applying it scales the forecast and its band together —")
fut = pd.DataFrame([{"ds": d, "yhat": 10.0, "yhat_lower": 6.0, "yhat_upper": 14.0} for d in dates])
scaled = U.apply(fut, mult)
by = {str(r["ds"]): r for _, r in scaled.iterrows()}
check("a promo day is lifted", by["2026-09-03"]["yhat"] == 25.0, str(by["2026-09-03"]["yhat"]))
check("...and so is its band", by["2026-09-03"]["yhat_upper"] == 35.0)
check("...proportionally, so the band stays as wide relative to the mean",
      by["2026-09-03"]["yhat_upper"] / by["2026-09-03"]["yhat"] == fut.iloc[0]["yhat_upper"] / fut.iloc[0]["yhat"])
check("an ordinary day is untouched", by["2026-09-01"]["yhat"] == 10.0)
check("an empty frame is safe", U.apply(pd.DataFrame(), mult) is not None)
check("no multipliers is a no-op", len(U.apply(fut, [])) == len(fut))

print("\n— it explains itself —")
e1 = U.explain(U.promo_lift(frame(promo_idx=range(100, 140))))
check("a measured lift is described", "Promotions lift this product" in e1, e1)
check("...and tied to scheduled promos", "promotion you've scheduled" in e1, e1)
e2 = U.explain(U.promo_lift(frame(days=20), cohort_lift=2.4))
check("a borrowed lift says it is borrowed", "similar products see" in e2, e2)
check("...and when it will stop borrowing", "its own once it has" in e2, e2)
e3 = U.explain({}, U.price_elasticity(elastic_frame(-1.5)))
check("elasticity is put in plain terms", "of unit demand" in e3, e3)
check("nothing measured means nothing claimed", U.explain({}, {}) == "")

print("\n— a planned price change moves the forecast, same events Prophet reads —")
FD = [f"2026-09-{d:02d}" for d in range(1, 11)]
perm = [{"type": "price_change_permanent", "date": "2026-09-05", "new_price": 25.0}]
path = U.future_price_path(FD, perm, base_price=20.0)
check("before the change it's the old price", path["2026-09-04"] == 20.0, str(path["2026-09-04"]))
check("from the change on, it's the new one", path["2026-09-05"] == 25.0 and path["2026-09-10"] == 25.0)
temp = [{"type": "price_change_temporary", "date": "2026-09-03", "end_date": "2026-09-05", "new_price": 15.0}]
tp = U.future_price_path(FD, temp, base_price=20.0)
check("a temporary change only covers its window",
      tp["2026-09-03"] == 15.0 and tp["2026-09-05"] == 15.0 and tp["2026-09-06"] == 20.0, str(tp))
disc = [{"type": "promotion", "date": "2026-09-02", "end_date": "2026-09-03", "discount_pct": 30}]
dp = U.future_price_path(FD, disc, base_price=20.0)
check("a discounted promo lowers the price in its window", dp["2026-09-02"] == 14.0, str(dp["2026-09-02"]))
check("...and not outside it", dp["2026-09-04"] == 20.0)

print("\n— price and promo effects compound, as they do in reality —")
m = U.future_multipliers(FD, disc, 2.0, elasticity=-1.5, base_price=20.0)
i = FD.index("2026-09-02")
check("a discounted promo gets BOTH the campaign lift and the price response",
      m[i] > 2.0, str(m[i]))
check("...and an ordinary day gets neither", m[FD.index("2026-09-08")] == 1.0, str(m))
mperm = U.future_multipliers(FD, perm, 1.0, elasticity=-1.5, base_price=20.0)
check("a price RISE lowers the forecast", mperm[FD.index("2026-09-06")] < 1.0, str(mperm[FD.index("2026-09-06")]))
check("...and only from the change date", mperm[FD.index("2026-09-01")] == 1.0)
check("with no elasticity measured, price changes do nothing",
      all(x == 1.0 for x in U.future_multipliers(FD, perm, 1.0, elasticity=0.0, base_price=20.0)))

print("\n— malformed events can't corrupt the path —")
bad = [{"type": "price_change_permanent", "date": "2026-09-05", "new_price": "free"},
       {"type": "price_change_permanent"},
       {"type": "promotion", "date": "2026-09-02", "discount_pct": "lots"}]
bp = U.future_price_path(FD, bad, base_price=20.0)
check("every day keeps the base price", all(v == 20.0 for v in bp.values()), str(bp))
check("no base price means no path", U.future_price_path(FD, perm, base_price=None) == {})
check("an explicit override wins over events",
      U.future_price_path(FD, perm, base_price=20.0, price_override=12.0)["2026-09-08"] == 12.0)

print(f"\n=== {_pass} passed, {_fail} failed ===")
sys.exit(1 if _fail else 0)

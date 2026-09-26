"""
Price changes must move the forecast the right way — or say plainly that they can't.

TWO BUGS THIS GUARDS, both reported from real use:

1. BACKWARDS. Prophet fits the price coefficient with no sign constraint. On sparse data a
   coincidence — a discount that landed in a quiet stretch — taught the model that cheaper
   means FEWER sales. Every price event then moved the forecast backwards: a cut lowered
   demand, a rise raised it. Reproduced: a 20% cut took a 30-day forecast from 155 units
   to 45, a 20% rise took it to 265. The guard that should have caught it already existed
   (uplift.price_elasticity refuses a positive relationship) but only ran on the engines
   that DON'T apply price.

2. SILENT. On sparse products a price event did nothing at all — correctly, since fifteen
   selling days cannot measure elasticity — but it said nothing, so a deliberate refusal
   was indistinguishable from a bug.

The central property: a product whose data shows a NORMAL price response must keep it.
Neutralising every price effect would pass the first check trivially and be useless.

Needs prophet installed; runs real fits.
"""
from __future__ import annotations
import os as _os_lp, tempfile as _tf_lp
# Never write the real live-price store from a test.
_os_lp.environ["LOGITRACK_LIVE_PRICES_PATH"] = _os_lp.path.join(_tf_lp.mkdtemp(), "live_prices.json")

import logging
import sys
import warnings

warnings.filterwarnings("ignore")
logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
logging.getLogger("prophet").setLevel(logging.ERROR)
logging.getLogger("cmdstanpy").disabled = True

import numpy as np
import pandas as pd

import main as M

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


N = 200


def _rows(sku, y, price, days):
    return [{"date": d.strftime("%Y-%m-%d"), "sku": sku, "sku_name": sku,
             "units_sold": int(v), "price": float(p)} for d, v, p in zip(days, y, price)]


def _catalogue():
    end = M.today().normalize() - pd.Timedelta(days=1)
    days = pd.date_range(end - pd.Timedelta(days=N - 1), end, freq="D")
    rng = np.random.default_rng(5)

    y = rng.poisson(6, N).astype(float); p = np.full(N, 50.0)
    y[80:120] = rng.poisson(2, 40); p[80:120] = 40.0          # discount in a quiet spell
    conf = _rows("CONFOUNDED", y, p, days)

    y = rng.poisson(6, N).astype(float); p = np.full(N, 50.0)
    y[80:120] = rng.poisson(9, 40); p[80:120] = 40.0          # discount that genuinely sold more
    norm = _rows("NORMAL", y, p, days)

    y = np.zeros(N); y[rng.choice(N, 15, replace=False)] = 2
    p = np.full(N, 50.0); p[100:] = 40.0                       # a clear cut, too few sale days
    sparse = _rows("SPARSE", y, p, days)

    # Plenty of sales, but only ONE price with 30+ days: 199 days at $50, one at $45. That
    # single day at $45 used to produce the biggest price effect of any product tested.
    y = rng.poisson(6, N).astype(float); p = np.full(N, 50.0); p[150] = 45.0
    oneday = _rows("ONEDAY", y, p, days)

    # Two real levels, but every $40 day is MIXED — some units at $50, some at $40 — so
    # none of them is evidence of either price. Only one level qualifies.
    y = rng.poisson(6, N).astype(float); p = np.full(N, 50.0)
    mixed = _rows("MIXED", y, p, days)
    for r in mixed[60:120]:
        r["price"] = 46.0; r["price_mixed"] = 1

    M._ingest(pd.DataFrame(conf + norm + sparse + oneday + mixed), "t.csv", append=False,
              reanchor=False, auto_backtest=False)


def _thirty_days(sku, new_price=None, events=None):
    ev = list(events or [])
    if new_price is not None:
        ev = [{"type": "price_change_permanent",
               "date": (M.today() + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
               "new_price": new_price}]
    M._catalog[sku]["events"] = ev
    M._rebuild(M.today(), only=sku)
    v = M.get_forecast(sku_id=sku, stock=100, lead_time_days=14, coverage_days=30,
                       strategy="balanced", forecast_months=3, units_on_order=0,
                       on_order_eta_days=None, unit_cost=None, fees=0.0, protection=None,
                       tz=None, stock_source="manual", stock_counted_at=None)
    return sum(pt["y"] for pt in v["chartDataFuture"][:30]), v


def test_backwards_relationship_is_neutralised():
    print("a discount that landed in a quiet spell does not teach 'cheaper sells less'")
    base, v = _thirty_days("CONFOUNDED")
    cut, _ = _thirty_days("CONFOUNDED", 40.0)
    rise, _ = _thirty_days("CONFOUNDED", 60.0)
    check("a price cut does not LOWER the forecast", cut >= base - 1e-6, f"{base:.1f} -> {cut:.1f}")
    check("a price rise does not RAISE the forecast", rise <= base + 1e-6, f"{base:.1f} -> {rise:.1f}")
    check("price is reported as not modelled", v["priceModeled"] is False)
    pr = v.get("priceResponse") or {}
    # Caught either by the product's own price reading (before Prophet is given price) or,
    # failing that, by Prophet's own check after fitting. Same verdict, same words.
    check("and the reason is named", pr.get("applies") is False
          and pr.get("reason") in ("inverted", "positive-elasticity-ignored"), pr)


def test_a_real_price_response_survives():
    """The guard must not throw away genuine elasticity to make the first test pass."""
    print("a product that genuinely sells more when cheaper keeps that behaviour")
    base, v = _thirty_days("NORMAL")
    cut, _ = _thirty_days("NORMAL", 40.0)
    rise, _ = _thirty_days("NORMAL", 60.0)
    check("price is still modelled", v["priceModeled"] is True)
    check("a cut RAISES the forecast", cut > base * 1.05, f"{base:.1f} -> {cut:.1f}")
    check("a rise LOWERS the forecast", rise < base * 0.95, f"{base:.1f} -> {rise:.1f}")
    check("and it says price applies", (v.get("priceResponse") or {}).get("applies") is True)


def test_sparse_product_says_why_nothing_happened():
    print("a product with too few sale days says so instead of silently ignoring the change")
    base, v = _thirty_days("SPARSE")
    cut, _ = _thirty_days("SPARSE", 40.0)
    pr = v.get("priceResponse") or {}
    check("the forecast is unchanged (nothing to measure from)", abs(cut - base) < 1e-6)
    check("the refusal is explicit", pr.get("applies") is False and pr.get("reason") == "too-few-points", pr)
    check("and it names the counts", "15" in (pr.get("text") or "") and "30" in (pr.get("text") or ""),
          pr.get("text"))


def test_one_day_at_a_price_is_not_a_price_level():
    print("one day at $45 among 199 at $50 cannot move the forecast")
    base, v = _thirty_days("ONEDAY")
    cut, _ = _thirty_days("ONEDAY", 25.0)
    pr = v.get("priceResponse") or {}
    check("a cut to $25 changes nothing", abs(cut - base) < 1e-6, f"{base:.1f} -> {cut:.1f}")
    check("reason is too few price levels", pr.get("reason") == "too-few-price-levels", pr)
    check("the text names the levels and the 30-day rule",
          "$50.00" in pr.get("text", "") and "$45.00 (1 day," in pr.get("text", "")
          and "30+" in pr.get("text", ""), pr.get("text"))
    check("price reported as not modelled", v["priceModeled"] is False)
    lv = {t["price"]: t for t in v["priceTiers"]}
    check("the one-day price is labelled insufficient", lv.get(45.0, {}).get("quality") == "insufficient", lv)


def test_mixed_days_count_for_nothing():
    print("days that sold at two prices are reported, and never counted as a price level")
    base, v = _thirty_days("MIXED")
    cut, _ = _thirty_days("MIXED", 40.0)
    pr = v.get("priceResponse") or {}
    check("the mixed days are counted and reported", v.get("priceMixedDays") == 60, v.get("priceMixedDays"))
    check("the blended $46 never appears as a level",
          all(abs(t["price"] - 46.0) > 0.01 for t in v["priceTiers"]), v["priceTiers"])
    check("so only one level qualifies and price does nothing",
          pr.get("reason") == "too-few-price-levels" and abs(cut - base) < 1e-6, pr)
    check("and the explanation says mixed days were not counted", "more than one price" in pr.get("text", "") and "t counted" in pr.get("text", ""), pr.get("text"))
    check("current price is the last SINGLE price", v.get("currentPrice") == 50.0, v.get("currentPrice"))


def test_deep_discounts_extrapolate_with_honest_uncertainty():
    print("a Black Friday price well below anything sold is forecast, with a wider range")
    base, v = _thirty_days("NORMAL")
    lo, hi = v["priceSafeMin"], v["priceSafeMax"]
    check("hard limits are wide: 80% below the cheapest level, double the dearest",
          lo == 8.0 and hi == 100.0, (lo, hi))
    at_45, v45 = _thirty_days("NORMAL", 45.0)
    at_25, v25 = _thirty_days("NORMAL", 25.0)
    at_20, v20 = _thirty_days("NORMAL", 20.0)
    check("inside the observed range a cut still raises demand", at_45 > base, f"{base:.1f} -> {at_45:.1f}")
    check("$25 is NOT flattened to the edge of what was sold — it sells more than $40 would",
          at_25 > _thirty_days("NORMAL", 40.0)[0] * 1.05, f"{at_25:.1f}")
    check("and $20 more again", at_20 > at_25, f"{at_25:.1f} -> {at_20:.1f}")
    check("inside the observed range the effect is still an estimate: a small spread",
          0 < v45.get("priceExtrapolationSpread", 0) < v25.get("priceExtrapolationSpread", 0),
          (v45.get("priceExtrapolationSpread"), v25.get("priceExtrapolationSpread")))
    check("an extrapolated price carries extra safety stock",
          v25["priceExtrapolationSpread"] > 0 and v25["safetyStock"] > v["safetyStock"],
          (v25.get("priceExtrapolationSpread"), v["safetyStock"], v25["safetyStock"]))
    check("and the further out, the more", v20["priceExtrapolationSpread"] > v25["priceExtrapolationSpread"],
          (v25["priceExtrapolationSpread"], v20["priceExtrapolationSpread"]))

    # The band itself widens on extrapolated days. The model's own band is a fixed width
    # in units, so compare against $40 — the cheapest price actually sold at, inside the
    # range — where the band carries only the small in-range term.
    def width(price):
        _thirty_days("NORMAL", price)
        fut = M._cache()["NORMAL"]["future_fc"].iloc[2:30]
        return float((fut["yhat_upper"] - fut["yhat_lower"]).mean())
    w40, w25 = width(40.0), width(25.0)
    check("the forecast band is wider at $25 than at $40", w25 > w40 * 1.2, (w40, w25))

    at_5, _ = _thirty_days("NORMAL", 5.0)
    at_8, _ = _thirty_days("NORMAL", 8.0)
    check("a $5 typo is treated as the $8 floor, not extrapolated further", abs(at_5 - at_8) < 1e-6)
    check("and price alone never multiplies demand past 5x", at_5 <= base * 5.0 + 1e-6, f"{base:.1f} -> {at_5:.1f}")
    pr = v.get("priceResponse") or {}
    check("the extrapolation is explained", "extrapolated" in pr.get("text", ""), pr.get("text"))


def test_events_never_touch_recorded_reality():
    print("events are forecast inputs only — past days and the current price come from sales")
    t = M.today()
    base, v0 = _thirty_days("NORMAL")
    # A temporary sale that is entirely over: recorded sales already show what happened.
    past = [{"type": "price_change_temporary", "new_price": 40.0,
             "date": (t - pd.Timedelta(days=20)).strftime("%Y-%m-%d"),
             "end_date": (t - pd.Timedelta(days=10)).strftime("%Y-%m-%d")}]
    after_past, v1 = _thirty_days("NORMAL", events=past)
    check("a wholly-past temporary change has no effect on the forecast", abs(after_past - base) < 1e-6,
          f"{base:.2f} vs {after_past:.2f}")
    # A permanent change dated in the past: the current price still comes from the register.
    perm = [{"type": "price_change_permanent", "new_price": 40.0,
             "date": (t - pd.Timedelta(days=5)).strftime("%Y-%m-%d")}]
    hist_before = M._catalog["NORMAL"]["df"]["price"].round(2).tolist()
    after_perm, v2 = _thirty_days("NORMAL", events=perm)
    hist_after = M._catalog["NORMAL"]["df"]["price"].round(2).tolist()
    check("current price is NOT rewritten by a past-dated event",
          v2.get("currentPrice") == v0.get("currentPrice") == 50.0, (v0.get("currentPrice"), v2.get("currentPrice")))
    check("recorded price history is untouched by it", hist_before == hist_after
          and hist_after[-3:] == [50.0, 50.0, 50.0], hist_after[-3:])
    check("it applies to the forecast from the first unrecorded day", after_perm > base * 1.05,
          f"{base:.1f} -> {after_perm:.1f}")
    check("and the payload says where recorded history ends", v2.get("lastRecordedDate") is not None)
    M._catalog["NORMAL"]["events"] = []


def test_rises_bend_and_promotions_are_price_cuts():
    """Past the prices sold at, the response continues in percentages, not a straight
    line; a promotion is its discount and nothing more; buy X get Y is its per-unit cut."""
    print("rises bend, promotions count as price cuts, multi-buy works")
    base, _ = _thirty_days("NORMAL")
    cut, _ = _thirty_days("NORMAL", 40.0)          # inside the known range
    rise, _ = _thirty_days("NORMAL", 60.0)         # 20% above the highest price sold at
    check("a 20% rise past the known range loses less than a 20% cut gains",
          (base - rise) / base < (cut - base) / base, f"rise {rise / base - 1:+.0%}, cut {cut / base - 1:+.0%}")
    check("and it doesn't fall off a cliff (a straight line would lose about half)",
          rise / base > 0.6, f"{rise / base - 1:+.0%}")
    t = M.today()
    win = lambda v: sum(p["y"] for p in v["chartDataFuture"][10:17])
    _, v0 = _thirty_days("NORMAL")
    promo = [{"type": "promotion", "date": (t + pd.Timedelta(days=10)).strftime("%Y-%m-%d"),
              "end_date": (t + pd.Timedelta(days=16)).strftime("%Y-%m-%d")}]
    _, vp = _thirty_days("NORMAL", events=promo)
    check("a promotion with no discount changes nothing", abs(win(vp) - win(v0)) < 1e-6, (win(v0), win(vp)))
    bogo = [dict(promo[0], discount_pct=50.0, multibuy={"buy": 1, "get": 1}, label="Buy 1 get 1 free")]
    _, vb = _thirty_days("NORMAL", events=bogo)
    check("buy 1 get 1 free lifts its window", win(vb) > win(v0) * 1.2, (win(v0), win(vb)))
    M._catalog["NORMAL"]["events"] = []


def test_non_prophet_routes_show_what_they_order():
    """On Croston and the moving average, price and promo effects are applied as
    multipliers. They reached the scorecard's copy of the forecast but not the one the
    product page reads, so a price change moved the order and not the chart."""
    print("non-Prophet routes: the page and the scorecard see the same forecast")
    N2 = 200
    end = M.today().normalize() - pd.Timedelta(days=1)
    days = pd.date_range(end - pd.Timedelta(days=N2 - 1), end, freq="D")
    rng = np.random.default_rng(5)
    y = rng.poisson(6, N2).astype(float); p = np.full(N2, 50.0)
    y[80:120] = rng.poisson(9, 40); p[80:120] = 40.0
    df = pd.DataFrame({"ds": days, "y": y, "price": p})
    ev = [{"type": "price_change_permanent", "new_price": 25.0,
           "date": (M.today() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")}]
    for route in ("croston", "ma"):
        e = M.build_entry("X", "X", df, "upload", "f", M.today(), ev, [], 0,
                          calibrate=False, force_route=route)
        la = e["df_train"]["ds"].max()
        page = e["forecast"][e["forecast"]["ds"] > la]["yhat"].head(30).sum()
        card = e["future_fc"]["yhat"].head(30).sum()
        check(f"{route}: page and scorecard agree", abs(page - card) < 1e-6, f"{page:.1f} vs {card:.1f}")
        check(f"{route}: the $25 price is extrapolated with uncertainty",
              "x_sd" in e["future_fc"].columns and float(e["future_fc"]["x_sd"].max()) > 0)


def test_live_discount_from_the_pos():
    """The POS says the product is on sale today. That is the displayed price, and a
    forecast input for the days ahead — never a rewrite of the sales history."""
    print("a discount the POS reports is shown, forecast, and kept out of history")
    t = M.today()
    base, v0 = _thirty_days("NORMAL")
    hist_before = M._catalog["NORMAL"]["df"]["price"].round(2).tolist()
    until = (t + pd.Timedelta(days=9)).strftime("%Y-%m-%d")
    M._live.record({"NORMAL": {"listPrice": 50.0, "currentPrice": 40.0,
                               "discountName": "Fall sale", "discountUntil": until}}, "square")
    try:
        _, v = _thirty_days("NORMAL")
        check("current price is the POS's discounted price", v.get("currentPrice") == 40.0, v.get("currentPrice"))
        check("the original price is reported alongside", v.get("listPrice") == 50.0 and v.get("priceDiscounted") is True,
              (v.get("listPrice"), v.get("priceDiscounted")))
        pe = v.get("posEvents") or []
        check("it becomes a promotion from the first unrecorded day to the sale's end",
              len(pe) == 1 and pe[0]["type"] == "promotion" and pe[0]["end_date"] == until
              and abs(pe[0]["discount_pct"] - 20.0) < 1e-6, pe)
        f0 = [p["y"] for p in v0["chartDataFuture"]]
        f1 = [p["y"] for p in v["chartDataFuture"]]
        check("demand is higher during the sale", sum(f1[1:8]) > sum(f0[1:8]) * 1.05,
              f"{sum(f0[1:8]):.1f} -> {sum(f1[1:8]):.1f}")
        check("and back to normal after it ends", abs(sum(f1[15:30]) - sum(f0[15:30])) < 1e-6 * max(1, sum(f0[15:30])) + 1e-6,
              f"{sum(f0[15:30]):.1f} vs {sum(f1[15:30]):.1f}")
        check("sales history is untouched", M._catalog["NORMAL"]["df"]["price"].round(2).tolist() == hist_before)
        check("a logged event list is not polluted by the POS input", M._catalog["NORMAL"].get("events") == [])
        e = M._cache()["NORMAL"]
        check("scorecard: regular price is the listed price, and it is on promotion today",
              M.sc_effective_price_today(e) == 50.0 and M.sc_on_promo_today(e, [])[0] is True)
        # A backtest cutoff or calibration window never sees today's discount.
        be = M.build_entry("NORMAL", "NORMAL", M._catalog["NORMAL"]["df"], "upload", "f", t, [], [], 0,
                           calibrate=False)
        check("a build without the live price has no POS inputs", be["pos_events"] == [])
        # The sale ends: the next reading says current == list.
        M._live.record({"NORMAL": {"listPrice": 50.0, "currentPrice": 50.0}}, "square")
        _, v2 = _thirty_days("NORMAL")
        check("when the POS says the sale is over, the forecast returns to normal",
              abs(sum(p["y"] for p in v2["chartDataFuture"][:30]) - base) < 1e-6 and not v2.get("priceDiscounted"))
    finally:
        M._live.purge()
        M._catalog["NORMAL"]["events"] = []


if __name__ == "__main__":
    print("\nPrice response\n" + "-" * 48)
    _catalogue()
    for fn in (test_backwards_relationship_is_neutralised,
               test_a_real_price_response_survives,
               test_sparse_product_says_why_nothing_happened,
               test_one_day_at_a_price_is_not_a_price_level,
               test_mixed_days_count_for_nothing,
               test_deep_discounts_extrapolate_with_honest_uncertainty,
               test_events_never_touch_recorded_reality,
               test_non_prophet_routes_show_what_they_order,
               test_live_discount_from_the_pos,
               test_rises_bend_and_promotions_are_price_cuts):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All price-response tests passed.")

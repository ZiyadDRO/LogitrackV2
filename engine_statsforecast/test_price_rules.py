"""
How scheduled prices are built, and how far past known prices a response may go.

Offline and fast: no model fits.
"""
from __future__ import annotations

import sys

import numpy as np

import forecast_engine as FE
import uplift as UP

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


D = ["2026-11-26", "2026-11-27", "2026-11-28", "2026-11-29", "2026-12-01"]


def test_overlapping_promotions_do_not_stack():
    print("overlapping promotions: the deepest applies, they don't add up")
    evs = [{"type": "promotion", "date": "2026-11-27", "end_date": "2026-11-29", "discount_pct": 33.33,
            "multibuy": {"buy": 2, "get": 1}},
           {"type": "promotion", "date": "2026-11-27", "end_date": "2026-11-29", "discount_pct": 25.0}]
    prices, disc = FE.scheduled_prices(D, evs, 60.0)
    check("Black Friday: buy 2 get 1 + 25% off is 33% off, not 50%", prices[1] == round(60 * (1 - 0.3333), 2), prices)
    check("days outside the week are untouched", prices[0] == 60.0 and prices[4] == 60.0, prices)
    check("order of entry doesn't matter", FE.scheduled_prices(D, evs[::-1], 60.0)[0] == prices)


def test_promotions_discount_the_price_in_force():
    print("a promotion is a discount off whatever the shelf price is that day")
    evs = [{"type": "price_change_permanent", "date": "2026-11-26", "new_price": 50.0},
           {"type": "price_change_temporary", "date": "2026-11-28", "end_date": "2026-11-28", "new_price": 45.0},
           {"type": "promotion", "date": "2026-11-27", "end_date": "2026-11-29", "discount_pct": 20.0}]
    prices, _ = FE.scheduled_prices(D, evs, 60.0)
    check("permanent change sets the shelf price", prices[0] == 50.0, prices)
    check("promotion discounts it", prices[1] == 40.0, prices)
    check("temporary change + promotion = 20% off the temporary price", prices[2] == 36.0, prices)
    check("temporary change entered before the permanent one still wins its window",
          FE.scheduled_prices(D, [evs[1], evs[0], evs[2]], 60.0)[0] == prices)
    check("a promotion with no discount changes nothing",
          FE.scheduled_prices(D, [{"type": "promotion", "date": "2026-11-27", "end_date": "2026-11-29"}], 60.0)[0]
          == [60.0] * 5)


def test_taper():
    print("past the known prices the response eases toward the limits")
    ref, edge = 6.0, 9.0                                   # $50 sells 6, $40 sells 9 (elasticity ~ -1.82)
    raw = lambda p: edge * (p / 40.0) ** -1.82
    lifts = [float(FE.taper_beyond_edge(edge, raw(p), ref)) / ref for p in (40, 30, 25, 20, 10, 8)]
    check("unchanged at the edge of the known prices", abs(lifts[0] - 1.5) < 1e-9, lifts)
    check("close to the edge it keeps almost all of the response", abs(lifts[1] - raw(30) / ref) < 0.1, lifts)
    check("every deeper cut still adds something", all(b > a for a, b in zip(lifts, lifts[1:])), lifts)
    check("and never reaches the 5x ceiling", lifts[-1] < FE.MAX_PRICE_LIFT, lifts)
    rise = [float(FE.taper_beyond_edge(ref, ref * (p / 50.0) ** -1.82, ref)) / ref for p in (60, 75, 100)]
    check("rises ease toward -80% without reaching it",
          all(b < a for a, b in zip(rise, rise[1:])) and rise[-1] > FE.MIN_PRICE_RATIO, rise)
    m = UP.price_response(-1.82, 50.0, 10.0, (40.0, 50.0))
    check("the non-Prophet route uses the same taper", abs(m - lifts[4]) < 0.02, (m, lifts[4]))


if __name__ == "__main__":
    print("\nPrice rules\n" + "-" * 44)
    for fn in (test_overlapping_promotions_do_not_stack, test_promotions_discount_the_price_in_force, test_taper):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All price-rule tests passed.")

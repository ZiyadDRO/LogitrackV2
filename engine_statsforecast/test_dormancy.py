"""
Dormancy and Croston bias — both found by running the real catalogue through the tool.

EDG-DEAD sold ~1.3/day for eighteen months, then nothing for ninety days, and the tool
still recommended ordering 69 more units at 1.5/day. The cause: `sparse_subtype` measures
share and rate over the WHOLE history, so a product that was regular and has since died
still reads as regular — which excludes TSB, the one model that decays through zero runs.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import forecast_engine as FE   # noqa: E402

_pass = _fail = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  [PASS] {name}")
    else:
        _fail += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


rng = np.random.default_rng(7)


def series(alive_days, dead_days, rate=1.3, share=0.7):
    live = np.where(rng.random(alive_days) < share, rng.poisson(rate / share, alive_days), 0)
    return np.concatenate([live.astype(float), np.zeros(dead_days)])


print("\n— the case from the real report: EDG-DEAD —")
dead = series(560, 169)
check("it has genuinely stopped", FE.trailing_zero_run(dead) == 169, str(FE.trailing_zero_run(dead)))
check("...and is recognised as dormant", FE.is_dormant(dead) is True)
check("...so it is NOT called a regular low-volume seller",
      FE.sparse_subtype(dead, "intermittent") == "true_intermittent",
      FE.sparse_subtype(dead, "intermittent"))
check("...which lets a decaying model compete for it",
      FE.sparse_subtype(dead, "intermittent") != "low_volume_regular")

print("\n— a steady trickle is NOT dormant, whatever the gaps —")
steady = series(729, 0, rate=1.2, share=0.6)
check("no trailing silence", FE.trailing_zero_run(steady) < 45)
check("...not dormant", FE.is_dormant(steady) is False)
check("...and still protected as low-volume regular",
      FE.sparse_subtype(steady, "intermittent") == "low_volume_regular",
      FE.sparse_subtype(steady, "intermittent"))

print("\n— dormancy is judged against the product's OWN rhythm —")
# Sells roughly monthly. 50 quiet days is barely more than one gap — not a stop.
monthly = np.zeros(720); monthly[::30] = 4.0
monthly = np.concatenate([monthly, np.zeros(50)])
check("a monthly seller quiet for 50 days is not dormant", FE.is_dormant(monthly) is False,
      f"tail={FE.trailing_zero_run(monthly)} adi={720/24:.0f}")
# The same silence on a daily seller IS a stop.
daily = np.concatenate([np.full(720, 2.0), np.zeros(50)])
check("a daily seller quiet for 50 days IS dormant", FE.is_dormant(daily) is True)
check("...and a monthly seller quiet for 200 days is too",
      FE.is_dormant(np.concatenate([monthly[:720], np.zeros(200)])) is True)

print("\n— an ordinary lull can't trip it —")
check("44 days is under the absolute floor",
      FE.is_dormant(np.concatenate([np.full(400, 2.0), np.zeros(44)])) is False)
check("a product that never sold at all is dormant", FE.is_dormant(np.zeros(200)) is True)
check("an empty series is not", FE.is_dormant(np.array([])) is False)
check("a sale on the last day is never dormant",
      FE.is_dormant(np.concatenate([np.zeros(300), [1.0]])) is False)

print("\n— non-sparse products are untouched —")
fast = np.full(729, 12.0)
check("a fast seller has no subtype", FE.sparse_subtype(fast, "smooth") == "")
check("...even with a trailing gap",
      FE.sparse_subtype(np.concatenate([fast, np.zeros(100)]), "smooth") == "")

print(f"\n=== {_pass} passed, {_fail} failed ===")
sys.exit(1 if _fail else 0)

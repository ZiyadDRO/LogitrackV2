"""
bands.total_band: the range for a multi-day TOTAL (month cards, the weekly log).

Guards the bug it replaced (a month's range made by adding up the days' lows and highs:
14 to 490 around 215) and pins the two limits the rule sits between.
"""
import math
import sys

import numpy as np

import bands as B

FAILURES = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILURES.append(name)


w = np.full(30, 5.0)          # 30 days, each ±5 around 7/day
point = 210.0

lo, hi = B.total_band(point, w, w, rho=0.0)
check("unrelated days, no level error: quadrature (5*sqrt(30))",
      abs((hi - point) - 5 * math.sqrt(30)) < 1e-6, (lo, hi))
lo, hi = B.total_band(point, w, w, rho=1.0)
check("days that always move together: the days' widths add up", abs((hi - point) - 150) < 1e-6, (lo, hi))
lo, hi = B.total_band(point, w, w, rho=0.1)
check("in between for anything in between", 5 * math.sqrt(30) < hi - point < 150, (lo, hi))

lo0, hi0 = B.total_band(point, w, w, rho=0.0)
lo1, hi1 = B.total_band(point, w, w, rho=0.0, level_sd=1.0)
check("a level error widens both sides", lo1 < lo0 and hi1 > hi0)
exp = math.hypot(5 * math.sqrt(30), B.z_for(80) * 30 * 1.0)
check("level error grows with the days (n * z * sd), added as an independent part",
      abs((hi1 - point) - exp) < 1e-6, (hi1 - point, exp))
lo2, hi2 = B.total_band(point, w, w, level_sd=1.0, ref_mean=3.5)
check("level error scaled to this forecast's level (7/day vs 3.5/day in the holdout)",
      hi2 - point > hi1 - point)

lo, hi = B.total_band(20.0, np.full(30, 5.0), np.full(30, 5.0), rho=1.0)
check("never below zero", lo == 0.0, (lo, hi))
lo, hi = B.total_band(20.0, np.full(30, 5.0), np.full(30, 5.0), rho=1.0, floor=12)
check("never below what's already sold (floor)", lo == 12, (lo, hi))

# Measured correlation: independent noise reads ~0, a shared level shift reads high.
rng = np.random.default_rng(3)
indep = [rng.normal(0, 2, 45) for _ in range(5)]
check("independent holdout errors read as ~0 correlation", B.day_correlation(indep, 30) < 0.1,
      B.day_correlation(indep, 30))
check("independent holdout errors read as ~0 level error", B.level_error(indep) < 0.4,
      B.level_error(indep))
shifted = [rng.normal(m, 2, 45) for m in (-3, 3, -3, 3, 2)]
check("windows that each ran high or low read as a level error", B.level_error(shifted) > 2,
      B.level_error(shifted))
check("no holdout errors: no correlation, no level error",
      B.day_correlation(None, 30) == 0.0 and B.level_error(None) == 0.0)

print(f"\n{'All band tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: {FAILURES}'}")
sys.exit(1 if FAILURES else 0)

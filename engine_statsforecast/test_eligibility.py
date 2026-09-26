"""
test_eligibility.py — a CHARACTERIZATION test for get_eligibility_status.

This is not a test of what the grading rule *should* say. It is a record of what it
*does* say, across the whole input grid, so that any future change to it has to declare
itself instead of drifting.

That distinction matters here more than usual. The rule is six thresholds feeding four
labels, three of the thresholds are duplicated as bare literals in another module, and the
labels are about to be reorganised. Without a grid like this, "I only tidied the
constants" and "I moved 400 products into a different band" look identical from the
outside.

WHAT IT PINS
  · every (days, selling_days, units, demand_class) combination that sits on or near a
    threshold, including the exact boundary values (89/90, 179/180, 44/45, 29/30, 14/15)
  · that the labels come from a fixed, known set
  · that the intermittent override outranks the history ladder, at any history length

Run:  python test_eligibility.py            — verify against the recorded grid
      python test_eligibility.py --record   — re-record after an INTENDED change
      python test_eligibility.py --analyse  — what each threshold actually decides
"""
from __future__ import annotations
import os as _os_lp, tempfile as _tf_lp
# Never write the real live-price store from a test.
_os_lp.environ["LOGITRACK_LIVE_PRICES_PATH"] = _os_lp.path.join(_tf_lp.mkdtemp(), "live_prices.json")

import hashlib
import itertools
import re
import json
import os
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp()
os.environ["LOGITRACK_CONNECTIONS"] = os.path.join(_TMP, "connections.json")
os.environ["LOGITRACK_PERSIST"] = "0"

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eligibility_grid.json")


# ─── Stub the forecasting stack so main.py imports without Prophet et al ─────

def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _FakeLog:
    def __init__(self, *a, **k):
        pass

    def pending(self):
        return []

    def due(self):
        return []

    def summary(self):
        return {}

    def coverage(self):
        return {}

    def record(self, levels, **k):
        return {"added": 0}

    def purge(self):
        pass


import importlib.util as _ilu
_fe_spec = _ilu.spec_from_file_location("_fe_real", __file__.rsplit("/", 1)[0] + "/forecast_engine.py")
_fe_real = _ilu.module_from_spec(_fe_spec); _fe_spec.loader.exec_module(_fe_real)
# Everything main imports from forecast_engine is available for real (it is pure numpy /
# pandas); only the calls these suites deliberately neutralise are overridden below. A
# hand-listed stub broke every time main imported one more helper.
_fe_public = {k: getattr(_fe_real, k) for k in dir(_fe_real) if not k.startswith("__")}
_stub("forecast_engine", **{**_fe_public, **dict(analyse_price=lambda *a, **k: None,
      daily_price_rollup=lambda *a, **k: None, price_evidence=lambda *a, **k: {},
      price_bounds=lambda *a, **k: None, carry_listed_price=lambda g: g,
      classify_demand=lambda *a, **k: None, compute_safe_price_range=lambda *a, **k: None,
      fill_daily_gaps=lambda *a, **k: None, sparse_subtype=lambda *a, **k: None,
      # main now imports this to put dormancy on each entry, which is what decides the
      # "Dead stock" badge. Stubbed False here: these suites test grading and endpoints,
      # not dormancy — test_status_badges drives sc_status with the flag directly.
      is_dormant=lambda *a, **k: False)})
_stub("forecast_log", ForecastLog=_FakeLog, WEEKLY="weekly",
      next_origin=lambda **k: __import__("datetime").date(2026, 3, 1),
      DEFAULT_LOG_HORIZONS=[7])
_stub("censoring"), _stub("promos"), _stub("uplift"), _stub("seasonality")
_stub("bundles", normalise_map=lambda x: x or {}, validate=lambda *a, **k: {},
      attribute=lambda rows, m: (rows, {}))
_stub("arrivals")
_stub("catalog_store", enabled=lambda: False, save=lambda *a, **k: False,
      load=lambda *a, **k: (None, {}), clear=lambda: None, path=lambda: _TMP)
_stub("backtest_store", clear=lambda: None, save=lambda *a, **k: None, load=lambda *a, **k: None)
_stub("exports")
_stub("engines", ProphetEngine=object, GlobalPooledEngine=object,
      IntermittentEngine=object, MovingAverageEngine=object)
_stub("stock_log", StockLog=_FakeLog, sample_from_shopify=lambda *a, **k: {"ok": True})
_stub("live_actuals", shopify_actuals_provider=lambda *a, **k: None,
      window_units=lambda *a, **k: 0.0, lookback_days_for=lambda *a, **k: 30)
_stub("shopify_source", ShopifyError=Exception, fetch_sales=lambda **k: None,
      fetch_catalog_meta=lambda **k: {})
_stub("square_source", SquareError=Exception, fetch_sales=lambda **k: None,
      fetch_catalog_meta=lambda **k: {}, fetch_locations=lambda **k: [])
_stub("sources", catalogue=lambda: [], get=lambda k: {}, describe=lambda *a: "",
      fetch_sales=lambda *a, **k: None, fetch_catalog_meta=lambda *a, **k: {},
      sample_stock=lambda *a, **k: {}, error_class=lambda k: Exception,
      actuals_provider=lambda *a, **k: None)
_stub("connections", list_all=lambda: [], get=lambda i: None, path=lambda: _TMP,
      upsert=lambda *a, **k: {}, delete=lambda i: False, touch=lambda i: None,
      default_for=lambda s: None)

import main                                                   # noqa: E402

ELIGIBILITY = main.get_eligibility_status

# Boundary values either side of every threshold in play, so an off-by-one moves the grid.
DAYS = [0, 1, 29, 30, 59, 60, 89, 90, 91, 119, 120, 179, 180, 181, 200, 365, 400]
SELLING = [0, 1, 14, 15, 16, 30, 44, 45, 46, 60, 100]
UNITS = [0, 1, 29, 30, 31, 59, 60, 61, 100, 500]
CLASSES = [None, "smooth", "erratic", "intermittent", "lumpy", "no_demand"]

# THREE grades, ordinal. CONFLICTING is deliberately gone: it covered three unrelated
# causes and was 37% of this grid, so it graded nothing and explained nothing. Its causes
# now live in `reason`, on the grade each actually deserves.
KNOWN_LABELS = {"FORECAST_ELIGIBLE", "LOW_CONFIDENCE", "INSUFFICIENT"}
KNOWN_REASONS = {"established", "partial_history", "short_history", "few_selling_days",
                 "low_volume", "new_product", "intermittent_demand", "dormant"}
KNOWN_PREDICTABILITY = {"steady", "variable", "occasional", "dormant", "unknown"}

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def build_grid(fn=None) -> dict:
    """{'days|selling|units|class': LABEL} for every combination."""
    fn = fn or ELIGIBILITY
    grid = {}
    for d, s, u, c in itertools.product(DAYS, SELLING, UNITS, CLASSES):
        label, _msg = fn(d, u, c, s)
        grid[f"{d}|{s}|{u}|{c}"] = label
    return grid


def fingerprint(grid: dict) -> str:
    return hashlib.sha256(json.dumps(grid, sort_keys=True).encode()).hexdigest()[:16]


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_labels_are_known():
    print("the label set is closed")
    grid = build_grid()
    seen = set(grid.values())
    check("no label outside the known three", seen <= KNOWN_LABELS, seen - KNOWN_LABELS)
    check("all three labels are reachable", seen == KNOWN_LABELS, KNOWN_LABELS - seen)
    check("CONFLICTING is gone", "CONFLICTING" not in seen)
    check("the grid is fully populated", len(grid) == len(DAYS) * len(SELLING) * len(UNITS) * len(CLASSES))

    reasons, preds = set(), set()
    for d, sd, u, c in itertools.product(DAYS, SELLING, UNITS, CLASSES):
        g = main.grade_forecast(d, u, c, sd)
        reasons.add(g["reason"]); preds.add(g["predictability"])
    check("every reason is from the known set", reasons <= KNOWN_REASONS, reasons - KNOWN_REASONS)
    check("every predictability is from the known set",
          preds <= KNOWN_PREDICTABILITY, preds - KNOWN_PREDICTABILITY)


def test_two_axes_are_independent():
    """Evidence and predictability are separate things, and only one of them is fixable
    by waiting. The old rule collapsed them and produced CONFLICTING."""
    print("evidence and predictability are separate axes")
    steady = main.grade_forecast(400, 5000, "smooth", 300)
    lumpy = main.grade_forecast(400, 5000, "lumpy", 300)
    check("identical evidence, different predictability, different grade",
          steady["status"] != lumpy["status"], (steady["status"], lumpy["status"]))
    check("the evidence axis agrees for both",
          steady["days"] == lumpy["days"] and steady["readyAtDays"] == lumpy["readyAtDays"])
    check("lumpy is capped at usable, not failed",
          lumpy["status"] == "LOW_CONFIDENCE", lumpy["status"])
    check("and says why in a code, not a paragraph",
          lumpy["reason"] == "intermittent_demand", lumpy["reason"])
    check("steady with the same evidence is the top grade",
          steady["status"] == "FORECAST_ELIGIBLE")

    # Predictability caps; it never promotes.
    thin_lumpy = main.grade_forecast(10, 2, "lumpy", 2)
    check("a thin lumpy product is still NOT_READY on evidence",
          thin_lumpy["status"] == "INSUFFICIENT", thin_lumpy["status"])


def test_thresholds_scale_with_the_decision_horizon():
    """The bar is "can you answer the reorder question", and that question is a different
    size for a 7-day lead time than a 90-day one."""
    print("the bar scales with the decision horizon")
    ready44, usable44 = main.evidence_thresholds(44)
    check("default horizon lands near the old fixed constants",
          170 <= ready44 <= 182 and 84 <= usable44 <= 92, (ready44, usable44))

    ready10, _ = main.evidence_thresholds(10)
    ready90, _ = main.evidence_thresholds(90)
    check("a short horizon needs less history", ready10 < ready44 < ready90,
          (ready10, ready44, ready90))
    check("but never absurdly little", ready10 >= main.READY_DAYS_FLOOR, ready10)

    # Same product, two different planning questions.
    short = main.grade_forecast(120, 500, "smooth", 100, horizon_days=10)
    long_ = main.grade_forecast(120, 500, "smooth", 100, horizon_days=90)
    check("120 days is enough to plan 10 days ahead",
          short["status"] == "FORECAST_ELIGIBLE", short["status"])
    check("but not enough to plan 90 days ahead",
          long_["status"] != "FORECAST_ELIGIBLE", long_["status"])
    check("and the message names the horizon it is grading for",
          "90 days" in long_["message"] or "90" in long_["message"], long_["message"])


def test_intermittent_override():
    print("occasional demand caps the grade rather than failing it")
    for cls in ("intermittent", "lumpy", "no_demand"):
        label, _ = ELIGIBILITY(400, 5000, cls, 300)
        check(f"{cls} never reaches the top grade, even at 400 days",
              label != "FORECAST_ELIGIBLE", label)
        check(f"{cls} with plenty of history is still usable",
              label == "LOW_CONFIDENCE", label)
        check(f"{cls} with thin history is INSUFFICIENT",
              ELIGIBILITY(30, 5, cls, 3)[0] == "INSUFFICIENT")
    check("dormant is called dormant, not 'conflicting'",
          main.grade_forecast(400, 5000, "no_demand", 300)["reason"] == "dormant")


def test_monotonic_in_history():
    print("more history never downgrades a product")
    order = {"INSUFFICIENT": 0, "CONFLICTING": 1, "LOW_CONFIDENCE": 2, "FORECAST_ELIGIBLE": 3}
    # Holding sales/selling-days at a level that clears every units gate, the label must be
    # non-decreasing in days. A ladder that dips as history grows is a bug, not a policy.
    bad = []
    for cls in (None, "smooth", "erratic"):
        prev = -1
        for d in DAYS:
            label, _ = ELIGIBILITY(d, 500, cls, 100)
            rank = order[label]
            if rank < prev:
                bad.append((cls, d, label))
            prev = max(prev, rank)
    check("label never goes down as days increase", not bad, bad[:4])


def test_boundaries():
    print("the labels change exactly where the thresholds say")
    ready, usable = main.evidence_thresholds(main.DEFAULT_DECISION_HORIZON)
    check(f"one day under ready ({ready - 1}) is not the top grade",
          ELIGIBILITY(ready - 1, 500, None, 100)[0] != "FORECAST_ELIGIBLE")
    check(f"exactly ready ({ready}) is the top grade",
          ELIGIBILITY(ready, 500, None, 100)[0] == "FORECAST_ELIGIBLE")
    check("44 selling days falls short of the top grade",
          ELIGIBILITY(ready, 500, None, 44)[0] != "FORECAST_ELIGIBLE")
    check("45 selling days clears it", ELIGIBILITY(ready, 500, None, 45)[0] == "FORECAST_ELIGIBLE")
    check(f"one day under usable ({usable - 1}) is not usable",
          ELIGIBILITY(usable - 1, 50, None, 20)[0] != "LOW_CONFIDENCE")
    check(f"exactly usable ({usable}) is usable",
          ELIGIBILITY(usable, 50, None, 20)[0] == "LOW_CONFIDENCE")
    check("14 selling days falls short", ELIGIBILITY(120, 50, None, 14)[0] != "LOW_CONFIDENCE")
    check("15 selling days clears it", ELIGIBILITY(120, 50, None, 15)[0] == "LOW_CONFIDENCE")
    check("29 units falls short", ELIGIBILITY(120, 29, None, 20)[0] != "LOW_CONFIDENCE")
    check("30 units clears it", ELIGIBILITY(120, 30, None, 20)[0] == "LOW_CONFIDENCE")


def test_message_sanity():
    print("the messages don't lie about the numbers")
    # Every message has to name the horizon it is grading for: "not enough history" is
    # meaningless without saying enough for WHAT.
    bad_horizon, negatives = [], []
    for d, sd, u, c in itertools.product(DAYS, SELLING, UNITS, CLASSES):
        g = main.grade_forecast(d, u, c, sd)
        m = g["message"]
        if g["reason"] not in ("dormant",) and str(g["decisionHorizonDays"]) not in m:
            bad_horizon.append((d, sd, u, c, g["reason"]))
        if re.search(r"-\s*\d", m):        # a negative NUMBER, not a hyphenated word
            negatives.append((d, sd, u, c, m))
    check("every message names its decision horizon", not bad_horizon, bad_horizon[:3])
    check("no message counts down from the past", not negatives, negatives[:2])
    _, msg2 = ELIGIBILITY(0, 0, None, 0)
    check("the floor message states what is needed", "88" in msg2 and "30" in msg2, msg2)


def test_thresholds_are_single_sourced():
    """The point of reading router's constants is that retuning them RETUNES THE LABEL.

    Before, these numbers were duplicated as literals here, so moving the router's gate to
    200 days left the grade still announcing 180 — the label would describe a model that
    no longer existed, and nothing would fail. This is the test that would have caught it.
    """
    print("the grade tracks the router's thresholds")
    import router as R
    original = R.ESTABLISHED_DAYS
    del original
    # The DAYS gate is now horizon-derived, so what must stay single-sourced are the
    # selling-days and units gates the router also routes on.
    orig_sd = R.ESTABLISHED_SELLING_DAYS
    try:
        check("at the stock threshold, 45 selling days is eligible",
              ELIGIBILITY(200, 500, None, 45)[0] == "FORECAST_ELIGIBLE")
        R.ESTABLISHED_SELLING_DAYS = 60
        check("retuning the router to 60 makes 45 selling days ineligible",
              ELIGIBILITY(200, 500, None, 45)[0] != "FORECAST_ELIGIBLE",
              "the grade is still using its own copy of the number")
    finally:
        R.ESTABLISHED_SELLING_DAYS = orig_sd

    orig_units = R.MIN_HISTORY_SALES
    try:
        R.MIN_HISTORY_SALES = 200
        _, msg = ELIGIBILITY(0, 0, None, 0)
        check("the floor message quotes the live units threshold", "200" in msg, msg)
    finally:
        R.MIN_HISTORY_SALES = orig_units


def test_matches_recorded_grid():
    print("behaviour matches the recorded grid")
    grid = build_grid()
    if not os.path.exists(GOLDEN):
        check("a recorded grid exists", False,
              f"none at {GOLDEN} — run with --record to create it")
        return
    old = json.load(open(GOLDEN))
    recorded, current = old["grid"], grid
    moved = {k: (v, current.get(k)) for k, v in recorded.items() if current.get(k) != v}
    added = set(current) - set(recorded)
    removed = set(recorded) - set(current)
    check("no cell changed label", not moved,
          f"{len(moved)} moved, e.g. " + "; ".join(
              f"{k} {a}->{b}" for k, (a, b) in list(moved.items())[:3]))
    check("no grid cells appeared or vanished", not added and not removed,
          f"+{len(added)} -{len(removed)}")
    check("fingerprint matches", fingerprint(grid) == old["fingerprint"],
          f"{fingerprint(grid)} vs {old['fingerprint']}")


# ─── Analysis: what is each threshold actually deciding? ─────────────────────

def analyse():
    """How much work each input does, measured rather than argued about."""
    base = build_grid()
    total = len(base)
    print(f"grid: {total:,} combinations\n")

    from collections import Counter
    print("label distribution:")
    for label, n in Counter(base.values()).most_common():
        print(f"  {label:<20}{n:>7,}  {n/total:>6.1%}")

    # What changes if the units test is dropped from each band?
    def without_units(days, sales, demand_class=None, selling_days=None):
        selling_days = int(selling_days or 0)
        reliable = days >= 180 and selling_days >= 45
        weak = days >= 90 and selling_days >= 15
        if demand_class in ("intermittent", "lumpy", "no_demand"):
            return ("CONFLICTING" if (reliable or weak) else "INSUFFICIENT"), ""
        if reliable:
            return "FORECAST_ELIGIBLE", ""
        if weak:
            return "LOW_CONFIDENCE", ""
        if days >= 180:
            return "CONFLICTING", ""
        if sales >= 60 and days < 90:
            return "CONFLICTING", ""
        return "INSUFFICIENT", ""

    alt = build_grid(without_units)
    moved = [k for k in base if base[k] != alt[k]]
    print(f"\ndropping the units test moves {len(moved):,} of {total:,} cells ({len(moved)/total:.1%})")
    if moved:
        ex = Counter(f"{base[k]} -> {alt[k]}" for k in moved)
        for t, n in ex.most_common(6):
            print(f"  {t:<42}{n:>7,}")
        print("\n  cells that move, by units held:")
        by_u = Counter(int(k.split('|')[2]) for k in moved)
        for u in sorted(by_u):
            print(f"    units={u:<6}{by_u[u]:>7,}")


if __name__ == "__main__":
    if "--record" in sys.argv:
        grid = build_grid()
        json.dump({"fingerprint": fingerprint(grid), "cells": len(grid), "grid": grid},
                  open(GOLDEN, "w"), indent=0, sort_keys=True)
        print(f"recorded {len(grid):,} cells -> {GOLDEN}  ({fingerprint(grid)})")
        sys.exit(0)
    if "--analyse" in sys.argv:
        analyse()
        sys.exit(0)

    for fn in (test_labels_are_known, test_two_axes_are_independent,
               test_thresholds_scale_with_the_decision_horizon, test_intermittent_override,
               test_monotonic_in_history, test_boundaries, test_message_sanity,
               test_thresholds_are_single_sourced, test_matches_recorded_grid):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("Eligibility behaviour unchanged.")

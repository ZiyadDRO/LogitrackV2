"""
Bundle attribution and arrival detection.

Both features are about information that exists but isn't being recorded: demand that
arrived through a bundle, and a delivery nobody logged. Both are also places where being
confidently wrong is worse than doing nothing — a mis-attributed bundle inflates a
product's forecast, and a mis-detected arrival writes a false date into the lead-time
record that P80 is built from. Most of these tests are about the refusals.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bundles as B      # noqa: E402
import arrivals as A     # noqa: E402

_pass = _fail = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  [PASS] {name}")
    else:
        _fail += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════ BUNDLES

print("\n— a bundle map can be written several ways —")
check("dict of quantities", B.normalise_map({"KIT": {"VAN": 1, "FCT": 2}}) == {"KIT": {"VAN": 1.0, "FCT": 2.0}})
check("list of objects",
      B.normalise_map({"KIT": [{"sku": "VAN", "qty": 1}, {"sku": "FCT", "qty": 2}]})
      == {"KIT": {"VAN": 1.0, "FCT": 2.0}})
check("bare list defaults to one each",
      B.normalise_map({"KIT": ["VAN", "FCT"]}) == {"KIT": {"VAN": 1.0, "FCT": 1.0}})
check("a bundle containing itself is dropped, not looped",
      B.normalise_map({"KIT": {"KIT": 1, "VAN": 1}}) == {"KIT": {"VAN": 1.0}})
check("zero and negative quantities are ignored",
      B.normalise_map({"KIT": {"VAN": 0, "FCT": -2, "MIR": 1}}) == {"KIT": {"MIR": 1.0}})
check("junk quantities fall back to 1",
      B.normalise_map({"KIT": {"VAN": "two"}}) == {"KIT": {"VAN": 1.0}})
check("an empty map is fine", B.normalise_map(None) == {})

print("\n— one bundle sale draws down every part —")
MAP = {"KIT": {"VAN": 1, "FCT": 1, "HDL": 2}}
rows = [
    {"date": "2026-08-01", "sku": "KIT", "units_sold": 3},
    {"date": "2026-08-01", "sku": "VAN", "units_sold": 5},
    {"date": "2026-08-02", "sku": "KIT", "units_sold": 1},
]
out, rep = B.attribute(rows, MAP)
by = {(r["date"], r["sku"]): r for r in out}
check("the bundle's own history is untouched", by[("2026-08-01", "KIT")]["units_sold"] == 3)
check("a component that also sold alone is topped up",
      by[("2026-08-01", "VAN")]["units_sold"] == 8, str(by[("2026-08-01", "VAN")]["units_sold"]))
check("...and says how much came from bundles",
      by[("2026-08-01", "VAN")]["bundle_units"] == 3)
check("quantities per bundle are respected",
      by[("2026-08-01", "HDL")]["units_sold"] == 6, str(by[("2026-08-01", "HDL")]["units_sold"]))
check("a component with NO standalone sales still gets a row",
      ("2026-08-01", "FCT") in by, "demand would have vanished entirely")
check("...and it is marked as bundle-only", by[("2026-08-01", "FCT")].get("from_bundle_only") is True)
check("the second day is attributed too", by[("2026-08-02", "HDL")]["units_sold"] == 2)
check("the report counts the bundle sales", rep["bundleUnits"] == 4.0, str(rep))
check("...and the units pushed down", rep["addedUnits"] == 16.0, str(rep["addedUnits"]))

print("\n— without a map, nothing changes at all —")
out2, rep2 = B.attribute(rows, {})
check("rows come back identical", out2 == rows)
check("...and it says it is inactive", rep2["active"] is False)

print("\n— bundles inside bundles are flattened —")
NEST = {"MEGA": {"KIT": 2, "MIR": 1}, "KIT": {"VAN": 1, "HDL": 2}}
flat = B.expand(NEST)
check("MEGA resolves to leaf parts",
      flat["MEGA"] == {"VAN": 2.0, "HDL": 4.0, "MIR": 1.0}, str(flat["MEGA"]))
check("the inner bundle still resolves on its own", flat["KIT"] == {"VAN": 1.0, "HDL": 2.0})
CYCLE = {"A": {"B": 1}, "B": {"A": 1}}
res = B.expand(CYCLE)
check("a cycle terminates instead of hanging", isinstance(res, dict) and len(res) == 2, str(res))

print("\n— a bad map costs you that line, not the feature —")
v = B.validate({"KIT": {"VAN": 1, "TYPO": 1}}, known_skus=["KIT", "VAN"])
check("it names the unknown component", v["unknownComponents"] == ["TYPO"], str(v))
check("...and reports not-ok", v["ok"] is False)
check("a clean map validates", B.validate({"KIT": {"VAN": 1}}, ["KIT", "VAN"])["ok"] is True)
check("nesting is surfaced as suspicious", B.validate(NEST)["nested"] == [("MEGA", "KIT")], str(B.validate(NEST)))

print("\n— knowing which products live off bundles —")
heavy = [{"date": "2026-08-01", "sku": "KIT", "units_sold": 10},
         {"date": "2026-08-01", "sku": "VAN", "units_sold": 1}]
dep = B.dependency(heavy, MAP)
check("VAN is mostly bundle-driven", dep["VAN"]["bundleDriven"] is True, str(dep["VAN"]))
check("...with the share named", dep["VAN"]["sharePct"] == 90.9, str(dep["VAN"]["sharePct"]))
light = [{"date": "2026-08-01", "sku": "KIT", "units_sold": 1},
         {"date": "2026-08-01", "sku": "VAN", "units_sold": 99}]
check("a product selling on its own is not flagged",
      B.dependency(light, MAP)["VAN"]["bundleDriven"] is False)
check("the explanation names the risk",
      "with the bundle in mind" in B.explain(B.attribute(heavy, MAP)[1], dep),
      B.explain(B.attribute(heavy, MAP)[1], dep))
check("no bundles means no claim", B.explain({"active": False}) == "")


# ══════════════════════════════════════════════════════════════ ARRIVALS

def S(day, hour, n):
    return {"ts": f"2026-08-{day:02d}T{hour:02d}:00:00Z", "onHand": n}


print("\n— stock only goes up when something arrives —")
samples = [S(1, 0, 50), S(2, 0, 30), S(3, 0, 12), S(4, 0, 512), S(5, 0, 480)]
jumps = A.find_jumps(samples)
check("exactly one jump is found", len(jumps) == 1, str(jumps))
check("...on the right day", jumps[0]["date"] == "2026-08-04", str(jumps[0]))
check("...with the right size", jumps[0]["jump"] == 500.0)
check("ordinary selling produces no jumps", A.find_jumps([S(1, 0, 50), S(2, 0, 30)]) == [])

print("\n— a jump is matched to the order it plausibly is —")
POS = [{"id": "o1", "skuId": "A", "qty": 500, "orderedDate": "2026-07-15",
        "expectedDate": "2026-08-03", "receivedDate": None}]
det = A.detect(samples, POS)
check("the arrival is detected", len(det) == 1, str(det))
d = det[0]
check("...tied to the order", d["orderId"] == "o1")
check("...on the day stock rose", d["arrivedOn"] == "2026-08-04")
check("...as an exact quantity match", d["exact"] is True)
check("...with the lead time it implies", d["impliedLeadDays"] == 20, str(d["impliedLeadDays"]))
check("...and how late it was", d["daysLate"] == 1, str(d["daysLate"]))
check("the message reads plainly", "matching this order" in d["message"], d["message"])
check("...and states the lead time", "lead time 20 days" in d["message"], d["message"])

print("\n— nothing is recorded without asking —")
check("an already-received order is never re-detected",
      A.detect(samples, [{**POS[0], "receivedDate": "2026-08-04"}]) == [])
check("a jump with no matching order is not reported",
      A.detect(samples, [{"id": "x", "skuId": "A", "qty": 9, "orderedDate": "2026-07-15"}]) == [])
check("a jump before the order was placed is impossible",
      A.detect(samples, [{"id": "y", "skuId": "A", "qty": 500,
                          "orderedDate": "2026-08-20", "expectedDate": "2026-09-01"}]) == [])

print("\n— small movements are not deliveries —")
noise = [S(1, 0, 100), S(2, 0, 102)]
check("a 2-unit rise is ignored", A.find_jumps(noise) == [], str(A.find_jumps(noise)))
check("...even with an open order it isn't reported",
      A.detect(noise, [{"id": "o", "skuId": "A", "qty": 500, "orderedDate": "2026-07-01",
                        "expectedDate": "2026-08-02"}]) == [])
small_vs_big = [S(1, 0, 100), S(2, 0, 120)]
check("a 20-unit rise against a 500-unit PO is not called a delivery",
      A.detect(small_vs_big, [{"id": "o", "skuId": "A", "qty": 500, "orderedDate": "2026-07-01",
                               "expectedDate": "2026-08-02"}]) == [],
      "a recount would have been logged as an arrival")

print("\n— partial and over-deliveries are flagged for a human —")
part = [S(1, 0, 10), S(2, 0, 310)]
dp = A.detect(part, [{"id": "p1", "skuId": "A", "qty": 500, "orderedDate": "2026-07-10",
                      "expectedDate": "2026-08-02"}])
check("a 300-of-500 arrival is detected", len(dp) == 1, str(dp))
check("...and marked partial", dp[0]["partial"] is True and dp[0]["exact"] is False)
check("...and says so plainly", "partial delivery" in dp[0]["message"], dp[0]["message"])
over = [S(1, 0, 10), S(2, 0, 610)]
do = A.detect(over, [{"id": "p2", "skuId": "A", "qty": 500, "orderedDate": "2026-07-10",
                      "expectedDate": "2026-08-02"}])
check("an over-delivery is flagged too", do[0]["over"] is True, str(do[0]))
check("...with a cautious message", "possibly this delivery plus something else" in do[0]["message"])
check("both need review", A.summarise(dp + do)["needsReview"] == 2, str(A.summarise(dp + do)))

print("\n— the right order among several in flight —")
multi = [{"id": "small", "skuId": "A", "qty": 60, "orderedDate": "2026-07-20",
          "expectedDate": "2026-08-03"},
         {"id": "big", "skuId": "A", "qty": 500, "orderedDate": "2026-07-15",
          "expectedDate": "2026-08-04"}]
dm = A.detect(samples, multi)
check("quantity decides, not order of listing", dm[0]["orderId"] == "big", str(dm[0]))
smalljump = [S(1, 0, 10), S(4, 0, 70)]
ds = A.detect(smalljump, multi)
check("a 60-unit jump matches the 60-unit order", ds[0]["orderId"] == "small", str(ds[0]))

print("\n— a very late delivery is still caught —")
late = [S(1, 0, 10), S(28, 0, 510)]
dl = A.detect(late, [{"id": "L", "skuId": "A", "qty": 500, "orderedDate": "2026-07-01",
                      "expectedDate": "2026-08-02"}])
check("it is detected 26 days late", len(dl) == 1, str(dl))
check("...and the lateness is quantified", dl[0]["daysLate"] == 26, str(dl[0]["daysLate"]))
check("...which is exactly the case worth catching", dl[0]["impliedLeadDays"] == 58,
      str(dl[0]["impliedLeadDays"]))

print("\n— malformed data doesn't take it down —")
check("bad timestamps are skipped",
      len(A.find_jumps([{"ts": "nope", "onHand": 1}, S(1, 0, 10), S(2, 0, 510)])) == 1)
check("missing quantities are skipped",
      A.detect(samples, [{"id": "z", "skuId": "A", "orderedDate": "2026-07-01"}]) == [])
check("no samples means nothing detected", A.detect([], POS) == [])
check("an empty summary is safe", A.summarise([])["found"] == 0)

# ══════════════════════════════════════════════════════════════ PROMOTIONS
import pandas as pd
import promos as P

def frame(days=120, promo_days=()):
    rows = []
    for i in range(days):
        d = pd.Timestamp("2026-01-01") + pd.Timedelta(days=i)
        on = 1 if i in promo_days else 0
        rows.append({"ds": d, "y": 40.0 if on else 5.0, "on_promotion": on})
    return pd.DataFrame(rows)

print("\n— Prophet keeps its promo days; it models them —")
f = frame(promo_days=range(100, 110))
kept, rep = P.hold_out(f, "prophet")
check("nothing is removed", len(kept) == len(f))
check("...and it says why", rep["reason"] == "route-models-promotions", str(rep))
check("...with no message to show", P.explain(rep) == "")

print("\n— the routes that CAN'T model promos have them held out —")
for route in ("global", "croston", "moving_average"):
    k, r = P.hold_out(frame(promo_days=range(100, 110)), route)
    check(f"{route}: the 10 promo days are excluded", r["removed"] == 10 and len(k) == 110, str(r))
    check(f"{route}: ...and it is reported", r["applied"] is True)

print("\n— which is the whole point: the spike stops moving the baseline —")
f2 = frame(promo_days=range(100, 110))
raw_mean = f2["y"].mean()
clean_mean = P.hold_out(f2, "croston")[0]["y"].mean()
check("the raw mean is inflated by the promo", raw_mean > 7.0, str(round(raw_mean, 2)))
check("the held-out mean is the real baseline", clean_mean == 5.0, str(clean_mean))
check("...a difference of about 40%", (raw_mean - clean_mean) / clean_mean > 0.35,
      str(round((raw_mean - clean_mean) / clean_mean, 3)))

print("\n— but not when discounting IS the business —")
always = frame(promo_days=range(0, 70))     # 58% of days on promo
k3, r3 = P.hold_out(always, "global")
check("nothing is removed", r3["applied"] is False and len(k3) == len(always))
check("...because promos are the norm", r3["reason"] == "promotions-are-the-norm", str(r3))
check("...and it says so plainly", "part of how this product normally sells" in P.explain(r3),
      P.explain(r3))

print("\n— nor when it would leave too little to fit —")
short = frame(days=50, promo_days=range(0, 10))
k4, r4 = P.hold_out(short, "croston")
check("nothing is removed", r4["applied"] is False)
check("...because too little would remain", r4["reason"] == "too-little-left", str(r4))
check("...and it explains the trade", "too little history" in P.explain(r4), P.explain(r4))

print("\n— a catalogue with no promo data is untouched —")
plain = pd.DataFrame([{"ds": pd.Timestamp("2026-01-01"), "y": 5.0}])
k5, r5 = P.hold_out(plain, "croston")
check("the frame is returned as-is", len(k5) == 1 and r5["applied"] is False)
check("...and it says there was no data", r5["reason"] == "no-promotion-data", str(r5))
k6, r6 = P.hold_out(frame(promo_days=()), "croston")
check("a promo column with no promos is a no-op", r6["reason"] == "no-promotions-in-history")
check("an empty frame is safe", P.hold_out(pd.DataFrame(), "croston")[1]["applied"] is False)

print("\n— a refund is not a delivery —")
# Product sells ~20/day. A customer returns 6 units against a small open order.
refund = [S(1, 0, 100), S(2, 0, 106)]
SMALL_PO = [{"id": "sp", "skuId": "A", "qty": 10, "orderedDate": "2026-07-20",
             "expectedDate": "2026-08-02"}]
check("without a sales rate it would be reported",
      len(A.detect(refund, SMALL_PO)) == 1, "guard needs the rate to work")
check("knowing the product sells 20/day, 6 units is a return",
      A.detect(refund, SMALL_PO, daily_rate=20) == [], str(A.detect(refund, SMALL_PO, daily_rate=20)))
check("a slow seller's 6 units IS plausibly a delivery",
      len(A.detect(refund, SMALL_PO, daily_rate=1)) == 1)
check("an EXACT quantity match is never dismissed as a refund",
      A.looks_like_refund(10, 10, daily_rate=999, exact=True) is False)
exact_small = [S(1, 0, 100), S(2, 0, 110)]
check("...so a 10-unit PO arriving exactly is still caught on a fast seller",
      len(A.detect(exact_small, SMALL_PO, daily_rate=50)) == 1,
      str(A.detect(exact_small, SMALL_PO, daily_rate=50)))
check("a real delivery dwarfs the daily rate and passes",
      len(A.detect(samples, POS, daily_rate=20)) == 1)
check("no rate means the other guards still do the work",
      A.looks_like_refund(5, 500, daily_rate=None) is False)

print(f"\n=== {_pass} passed, {_fail} failed ===")
sys.exit(1 if _fail else 0)

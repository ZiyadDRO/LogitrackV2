"""
Forecast-log tests. Every one of these drives a fake clock, because the behaviour that
matters — "you cannot grade this yet" — only shows up as time passes, and waiting two
real weeks per assertion isn't a test strategy.
"""
import datetime as _dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import forecast_log as FL   # noqa: E402

_pass = _fail = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  [PASS] {name}")
    else:
        _fail += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def at(y, m, d, hh=12):
    """A controllable clock reading."""
    return _dt.datetime(y, m, d, hh, tzinfo=_dt.timezone.utc)


def store():
    fh = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    fh.close()
    os.unlink(fh.name)
    return FL.ForecastLog(fh.name)


def rows(*skus, predicted=100.0, lo=70.0, hi=130.0, h=14):
    return [{"sku": s, "skuName": f"Product {s}", "predicted": predicted,
             "lo": lo, "hi": hi, "band": 80, "horizonDays": h,
             "model": "prophet", "leadDays": 21, "coverageDays": 30} for s in skus]


print("\n— weeks run Sunday to Saturday —")
# 2026-08-02 is a Sunday; the 5th is that Wednesday, the 8th that Saturday.
mon, wed, sun = at(2026, 8, 2), at(2026, 8, 5), at(2026, 8, 8)
check("Sunday anchors to itself", FL.period_origin(mon) == _dt.date(2026, 8, 2))
check("mid-week snaps back to that Sunday", FL.period_origin(wed) == _dt.date(2026, 8, 2))
check("Saturday still belongs to that week", FL.period_origin(sun) == _dt.date(2026, 8, 2))
check("the next Sunday starts a new period", FL.period_origin(at(2026, 8, 9)) == _dt.date(2026, 8, 9))
check("the anchor really is a Sunday", FL.period_origin(wed).strftime("%A") == "Sunday")
check("monthly anchors to the 1st", FL.period_origin(wed, FL.MONTHLY) == _dt.date(2026, 8, 1))
check("next weekly origin is 7 days on", FL.next_origin(wed) == _dt.date(2026, 8, 9))
check("next monthly origin rolls the month", FL.next_origin(wed, FL.MONTHLY) == _dt.date(2026, 9, 1))
check("month-end rolls correctly", FL.next_origin(at(2026, 1, 31), FL.MONTHLY) == _dt.date(2026, 2, 1))
check("December rolls into January", FL.next_origin(at(2026, 12, 15), FL.MONTHLY) == _dt.date(2027, 1, 1))

print("\n— a snapshot records the future, and waits —")
log = store()
snap = log.snapshot(rows("A", "B"), now=mon)
check("both products recorded", snap["added"] == 2, str(snap))
check("origin is the Sunday that started the week", snap["origin"] == "2026-08-02")
e = log.for_sku("A")[0]
check("the window starts ON the origin", e["windowStart"] == "2026-08-02")
check("...and its last counted day is 14 days later", e["dueAt"] == "2026-08-15")
check("...which is a Saturday", _dt.date.fromisoformat(e["dueAt"]).strftime("%A") == "Saturday")
check("...so it becomes gradable the Sunday after", e["scorableFrom"] == "2026-08-16")
check("...and that really is a Sunday",
      _dt.date.fromisoformat(e["scorableFrom"]).strftime("%A") == "Sunday")
check("nothing is scored yet", e["actual"] is None and e["scoredAt"] is None)
check("it is not due on the day it was made", not FL.is_due(e, mon))
check("...nor the day before the window ends", not FL.is_due(e, at(2026, 8, 14)))
check("...NOR on its final Saturday, whose sales are still coming in",
      not FL.is_due(e, at(2026, 8, 15)))
check("...and is due the Sunday morning after", FL.is_due(e, at(2026, 8, 16)))
check("countdown reads correctly", FL.days_until_due(e, mon) == 14, str(FL.days_until_due(e, mon)))

print("\n— you cannot grade a forecast early —")
try:
    FL.score_entry(e, 95.0, now=at(2026, 8, 10))
    check("scoring early raises", False, "it allowed it")
except FL.NotDueError:
    check("scoring early raises", True)
check("score_due grades nothing before the date",
      log.score_due(lambda s, a, b: 95.0, now=at(2026, 8, 10))["scored"] == 0)
check("...and the entry is still pending", not FL.is_scored(log.for_sku("A")[0]))
check("the store reports it as waiting, not due",
      len(log.waiting(at(2026, 8, 10))) == 2 and len(log.due(at(2026, 8, 10))) == 0)

print("\n— once the date arrives it grades, exactly once —")
res = log.score_due(lambda s, a, b: 95.0, now=at(2026, 8, 16))
check("both entries graded", res["scored"] == 2, str(res))
a = log.for_sku("A")[0]
check("the actual is recorded", a["actual"] == 95.0)
check("absolute error is right", a["absErr"] == 5.0, str(a["absErr"]))
check("signed error keeps direction (we over-forecast)", a["signedErr"] == 5.0)
check("percent error is right", a["pctErr"] == 5.3, str(a["pctErr"]))
check("95 lands inside the 70–130 band", a["inBand"] is True)
again = log.score_due(lambda s, a_, b: 999.0, now=at(2026, 8, 17))
check("re-running does not re-grade", again["scored"] == 0, str(again))
check("...and cannot overwrite the actual", log.for_sku("A")[0]["actual"] == 95.0)

print("\n— a prediction is never quietly rewritten —")
log2 = store()
log2.snapshot(rows("A", predicted=100.0), now=mon)
log2.snapshot(rows("A", predicted=500.0), now=wed)      # same week, different number
kept = log2.for_sku("A")
check("the same period yields one entry", len(kept) == 1, str(len(kept)))
check("...and it keeps the ORIGINAL prediction", kept[0]["predicted"] == 100.0, str(kept[0]["predicted"]))
log2.snapshot(rows("A", predicted=500.0), now=at(2026, 8, 9))
check("a new period does add a new entry", len(log2.for_sku("A")) == 2)
check("...with its own origin", log2.for_sku("A")[1]["origin"] == "2026-08-09")

print("\n— a missing read leaves the entry pending rather than scoring a zero —")
log3 = store()
log3.snapshot(rows("A", "B"), now=mon)
r = log3.score_due(lambda s, a_, b: None if s == "B" else 90.0, now=at(2026, 8, 16))
check("the readable one is graded", r["scored"] == 1, str(r))
check("the unreadable one is reported", r["unavailable"] == 1)
check("...and stays pending for next time", not FL.is_scored(log3.for_sku("B")[0]))
check("...it is NOT recorded as zero sales", log3.for_sku("B")[0]["actual"] is None)
r2 = log3.score_due(lambda s, a_, b: 88.0, now=at(2026, 8, 17))
check("it grades once the read works", r2["scored"] == 1 and log3.for_sku("B")[0]["actual"] == 88.0)

print("\n— a source that throws doesn't corrupt the log —")
log3b = store()
log3b.snapshot(rows("A"), now=mon)


def boom(*_a):
    raise RuntimeError("shopify 503")


rb = log3b.score_due(boom, now=at(2026, 8, 16))
check("the failure is contained", rb["scored"] == 0 and rb["unavailable"] == 1, str(rb))
check("the entry survives intact", log3b.for_sku("A")[0]["predicted"] == 100.0)

print("\n— calibration is the metric protection levels rest on —")
log4 = store()


def build(n, hit_pattern, band=80):
    lg = store()
    for i in range(n):
        org = _dt.date(2026, 1, 5) + _dt.timedelta(days=7 * i)
        lg.snapshot([{"sku": "A", "predicted": 100.0, "lo": 70.0, "hi": 130.0,
                      "band": band, "horizonDays": 14}], origin=org, now=at(2026, 1, 5))
    after = at(2027, 1, 1)
    hits = iter(hit_pattern)
    lg.score_due(lambda s, a_, b: 100.0 if next(hits) else 400.0, now=after)
    return lg


lg = build(20, [True] * 16 + [False] * 4)
cal = lg.summary()["calibration"]
check("an 80% band hitting 16/20 reads as 80%", cal["hitRate"] == 80.0, str(cal))
check("...and is judged calibrated", cal["verdict"] == "calibrated", str(cal))

lg = build(20, [True] * 11 + [False] * 9)
cal = lg.summary()["calibration"]
check("a band hitting 55% is flagged overconfident", cal["verdict"] == "overconfident", str(cal))
check("...with the gap named", cal["gap"] == -25.0, str(cal["gap"]))

lg = build(20, [True] * 20)
check("a band that never misses is underconfident",
      lg.summary()["calibration"]["verdict"] == "underconfident",
      str(lg.summary()["calibration"]))

lg = build(6, [True] * 3 + [False] * 3)
cal = lg.summary()["calibration"]
check("too few entries refuses to render a verdict", cal["verdict"] == "too-few", str(cal))
check("...but still reports the count", cal["n"] == 6)
check("no entries at all says so", FL.calibration([])["verdict"] == "no-data")

print("\n— accuracy is volume-weighted so tiny sellers can't dominate —")
lg = store()
lg.snapshot([{"sku": "BIG", "predicted": 1000.0, "lo": 900.0, "hi": 1100.0, "band": 80, "horizonDays": 14},
             {"sku": "TINY", "predicted": 2.0, "lo": 0.0, "hi": 5.0, "band": 80, "horizonDays": 14}],
            now=mon)
lg.score_due(lambda s, a_, b: 1000.0 if s == "BIG" else 1.0, now=at(2026, 8, 16))
acc = lg.summary()["accuracy"]
# TINY is 100% wrong; BIG is perfect. A plain mean would read 50% error.
check("one tiny product being wrong doesn't wreck the headline",
      acc["wape"] is not None and acc["wape"] < 1.0, str(acc))
check("both are still counted", acc["n"] == 2)
check("per-SKU still surfaces the bad one",
      FL.per_sku(lg.all())[0]["sku"] == "TINY", str(FL.per_sku(lg.all())[0]))

print("\n— bias separates 'noisy' from 'systematically high' —")
lg = store()
for i in range(6):
    lg.snapshot([{"sku": "A", "predicted": 120.0, "lo": 80.0, "hi": 160.0, "band": 80, "horizonDays": 14}],
                origin=_dt.date(2026, 1, 5) + _dt.timedelta(days=7 * i), now=at(2026, 1, 5))
lg.score_due(lambda s, a_, b: 100.0, now=at(2027, 1, 1))
acc = lg.summary()["accuracy"]
check("consistently over-forecasting shows positive bias", acc["bias"] == 20.0, str(acc))
check("...and it equals the error when the miss is one-directional", acc["wape"] == 20.0, str(acc))

print("\n— the summary tells you what to expect and when —")
log5 = store()
log5.snapshot(rows("A", "B", "C"), now=mon)
s = log5.summary(now=at(2026, 8, 10))
check("nothing scored yet", s["scored"] == 0)
check("all three waiting", s["waiting"] == 3)
check("none due yet", s["dueNow"] == 0)
check("it names the next due date", s["nextDueAt"] == "2026-08-16", str(s["nextDueAt"]))
check("...and the countdown", s["nextDueInDays"] == 6, str(s["nextDueInDays"]))
s2 = log5.summary(now=at(2026, 8, 16))
check("on the day, they read as due", s2["dueNow"] == 3 and s2["waiting"] == 0, str(s2))
check("nothing left to wait for", s2["nextDueAt"] is None)

print("\n— horizons of different lengths coexist on one origin —")
log6 = store()
log6.snapshot(rows("A", h=7) + rows("A", h=28), now=mon)
es = log6.for_sku("A")
check("both horizons are kept", len(es) == 2, str(len(es)))
check("they have different due dates", {e["dueAt"] for e in es} == {"2026-08-08", "2026-08-29"},
      str({e["dueAt"] for e in es}))
check("both windows end on a Saturday",
      all(_dt.date.fromisoformat(e["dueAt"]).strftime("%A") == "Saturday" for e in es))
check("the 7-day one grades first",
      len(log6.due(at(2026, 8, 9))) == 1, str(len(log6.due(at(2026, 8, 9)))))
check("the 28-day one is still waiting", len(log6.waiting(at(2026, 8, 9))) == 1)

print("\n— the log survives a restart —")
path = store().path
lg = FL.ForecastLog(path)
lg.snapshot(rows("A", "B"), now=mon)
lg.score_due(lambda s, a_, b: 95.0, now=at(2026, 8, 16))
reopened = FL.ForecastLog(path)
check("entries come back", len(reopened.all()) == 2, str(len(reopened.all())))
check("scores come back", reopened.for_sku("A")[0]["actual"] == 95.0)
check("predictions come back unchanged", reopened.for_sku("A")[0]["predicted"] == 100.0)
reopened.snapshot(rows("A"), now=mon)
check("re-snapshotting after a restart is still a no-op", len(reopened.for_sku("A")) == 1)
check("a corrupt file degrades to empty rather than crashing",
      len(FL.ForecastLog(os.devnull).all()) == 0)

print("\n— a zero-sales window is a real result, not a missing one —")
log7 = store()
log7.snapshot([{"sku": "Z", "predicted": 5.0, "lo": 0.0, "hi": 12.0, "band": 80, "horizonDays": 14}], now=mon)
log7.score_due(lambda s, a_, b: 0.0, now=at(2026, 8, 16))
z = log7.for_sku("Z")[0]
check("zero is recorded as zero", z["actual"] == 0.0)
check("...and graded", FL.is_scored(z))
check("...with percent error left undefined rather than infinite", z["pctErr"] is None)
check("...and band membership is still decided", z["inBand"] is True)

# ─────────────────────────────────────────────────────────────────────────────────────
import live_actuals as LA
import pandas as pd

print("\n— reading actuals back out of a sales frame —")
# Laid out against a REAL window: Sunday 2026-08-02 through Saturday 2026-08-15.
frame = pd.DataFrame([
    {"date": "2026-07-31", "sku": "A", "units_sold": 77},   # before the window
    {"date": "2026-08-02", "sku": "A", "units_sold": 10},   # the origin day itself — counts
    {"date": "2026-08-10", "sku": "A", "units_sold": 20},
    {"date": "2026-08-15", "sku": "A", "units_sold": 5},    # the final Saturday — counts
    {"date": "2026-08-16", "sku": "A", "units_sold": 99},   # after the window
    {"date": "2026-08-10", "sku": "B", "units_sold": 3},
])
W = ("2026-08-02", "2026-08-15")
check("sums exactly the window", LA.window_units(frame, "A", *W) == 35.0,
      str(LA.window_units(frame, "A", *W)))
check("the origin day is counted", LA.window_units(frame, "A", "2026-08-02", "2026-08-02") == 10.0)
check("the final Saturday is counted", LA.window_units(frame, "A", "2026-08-15", "2026-08-15") == 5.0)
check("the day before is excluded", LA.window_units(frame, "A", "2026-08-03", "2026-08-15") == 25.0)
check("the day after is excluded", 99.0 not in [LA.window_units(frame, "A", *W)])
check("doesn't leak other products", LA.window_units(frame, "B", *W) == 3.0)
check("a product with no sales in the window reads zero",
      LA.window_units(frame, "B", "2026-07-01", "2026-07-02") == 0.0)
check("an empty frame reads zero", LA.window_units(pd.DataFrame(), "A", *W) == 0.0)

print("\n— the lookback stretches to cover the oldest pending entry —")
old = [{"windowStart": "2026-05-01"}, {"windowStart": "2026-08-04"}]
d = LA.lookback_days_for(old, now=at(2026, 8, 16))
check("it reaches back past the oldest window", d > (_dt.date(2026, 8, 17) - _dt.date(2026, 5, 1)).days,
      str(d))
check("no entries still asks for something", LA.lookback_days_for([], now=at(2026, 8, 16)) >= 7)

print("\n— one fetch grades many entries —")
calls = []
def fake_fetch(days=None, shop=None, token=None):
    calls.append(days)
    return frame
lg = store()
lg.snapshot(rows("A", "B"), now=mon)
fn = LA.shopify_actuals_provider(lg.pending(), now=at(2026, 8, 16), fetch=fake_fetch)
res = lg.score_due(fn, now=at(2026, 8, 16))
check("Shopify was called exactly once", len(calls) == 1, str(calls))
check("both entries graded from that one call", res["scored"] == 2, str(res))
check("A got its real total", lg.for_sku("A")[0]["actual"] == 35.0, str(lg.for_sku("A")[0]["actual"]))
check("B got its real total", lg.for_sku("B")[0]["actual"] == 3.0)

print("\n— a store that can't be read grades nothing —")
def dead_fetch(**_k):
    raise RuntimeError("401 unauthorised")
lg2 = store()
lg2.snapshot(rows("A"), now=mon)
fn2 = LA.shopify_actuals_provider(lg2.pending(), now=at(2026, 8, 16), fetch=dead_fetch)
check("the provider reports the failure", fn2.failed is True)
r2 = lg2.score_due(fn2, now=at(2026, 8, 16))
check("nothing is graded", r2["scored"] == 0, str(r2))
check("the entry survives to be retried", not FL.is_scored(lg2.for_sku("A")[0]))
r3 = lg2.score_due(LA.shopify_actuals_provider(lg2.pending(), now=at(2026, 8, 17), fetch=fake_fetch),
                   now=at(2026, 8, 17))
check("...and grades once the store comes back", r3["scored"] == 1)

print("\n— a product the store has never reported is not 'sold zero' —")
lg3 = store()
lg3.snapshot(rows("GHOST"), now=mon)
fn3 = LA.shopify_actuals_provider(lg3.pending(), now=at(2026, 8, 16), fetch=fake_fetch)
r4 = lg3.score_due(fn3, now=at(2026, 8, 16))
check("it is left ungraded rather than scored 0", r4["scored"] == 0 and r4["unavailable"] == 1, str(r4))
check("...so it can't be recorded as a 100% miss", lg3.for_sku("GHOST")[0]["actual"] is None)

print("\n— weeks are filed under the month they mostly happened in —")
check("a week wholly inside a month is easy", FL.month_of_week("2026-08-09") == "2026-08")
# 2026-08-30 is a Sunday: Aug 30, 31 then Sep 1-5. Five of seven days are September.
check("a week straddling a boundary follows the majority",
      FL.month_of_week("2026-08-30") == "2026-09", FL.month_of_week("2026-08-30"))
# 2026-09-27 Sunday: Sep 27-30 (4 days) then Oct 1-3 (3). September wins.
check("...and the other way too", FL.month_of_week("2026-09-27") == "2026-09",
      FL.month_of_week("2026-09-27"))
check("every week lands in exactly one month",
      len({FL.month_of_week(f"2026-08-{d:02d}") for d in (2, 9, 16, 23, 30)}) == 2)

print("\n— a week is one tile —")
lg = store()
for i in range(3):
    org = _dt.date(2026, 8, 2) + _dt.timedelta(days=7 * i)
    lg.snapshot(rows("A", "B", "C", h=7), origin=org, now=at(2026, 8, 2))
wk = FL.by_week(lg.all(), now=at(2026, 8, 2))
check("three weeks, three tiles", len(wk) == 3, str(len(wk)))
check("newest first", wk[0]["week"] > wk[1]["week"])
check("each tile holds every product", all(w["products"] == 3 for w in wk), str([w["products"] for w in wk]))
check("...and knows when it closes", wk[2]["endsOn"] == "2026-08-08", wk[2]["endsOn"])
check("...and when it can be graded", wk[2]["gradableFrom"] == "2026-08-09")
check("an open week says so", wk[0]["status"] == "open", wk[0]["status"])
lg.score_due(lambda s, a_, b: 90.0, now=at(2026, 8, 30))
wk2 = FL.by_week(lg.all(), now=at(2026, 8, 30))
check("a graded week reports its accuracy", wk2[0]["wape"] is not None, str(wk2[0]))
check("...and how many landed in band", wk2[0]["inBand"] == 3, str(wk2[0]["inBand"]))
check("...and totals both sides", wk2[0]["predicted"] > 0 and wk2[0]["actual"] == 270.0, str(wk2[0]))
check("...and reads as scored", wk2[0]["status"] == "scored")

print("\n— a promotion declared mid-week is a revision, not a rewrite —")
lg3 = store()
lg3.snapshot(rows("A", predicted=100.0, h=7), now=at(2026, 8, 2))
e0 = lg3.for_sku("A")[0]
amended = FL.amend(e0, 260.0, "flash sale from Wednesday", now=at(2026, 8, 5))
check("the new figure is used for grading", amended["predicted"] == 260.0)
check("...but the original is kept", amended["originalPredicted"] == 100.0)
check("...and it is marked", amended["amended"] is True)
check("...with the reason", "flash sale" in amended["amendReason"])
check("amending twice keeps the FIRST original",
      FL.amend(amended, 300.0, "again")["originalPredicted"] == 100.0)

graded = FL.score_entry(e0, 95.0, now=at(2026, 8, 20))
try:
    FL.amend(graded, 500.0, "nice try")
    check("a graded week cannot be revised", False, "it allowed it")
except FL.AmendedError:
    check("a graded week cannot be revised", True)
check("...because that would be choosing the answer after seeing it", True)

print(f"\n=== {_pass} passed, {_fail} failed ===")
sys.exit(1 if _fail else 0)

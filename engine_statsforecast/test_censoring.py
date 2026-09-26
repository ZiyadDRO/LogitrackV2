"""
Censored-demand tests.

The behaviour under test is a claim about what you DON'T know: sales on a sold-out day are
a floor, not a fact. Most of these assert that the code refuses to invent a number, which
is the failure mode that matters — an over-confident uplift is worse than none, because it
looks like data.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import censoring as C   # noqa: E402

_pass = _fail = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  [PASS] {name}")
    else:
        _fail += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


print("\n— the case that started this: 5 in stock, 20 people wanted it —")
# Sold all 5, ended at zero, but was on the shelf all day.
d = C.estimate_demand(5, closing_stock=0, hours_in_stock=24)
check("it is not treated as a normal day", d["kind"] == C.CAPPED, d["kind"])
check("the 5 is kept as a floor", d["floor"] is True and d["observed"] == 5)
check("no demand is invented from thin air", d["estimate"] == 5, str(d["estimate"]))
check("...but it counts for less than a day we actually watched",
      d["weight"] < 1.0, str(d["weight"]))
normal = C.estimate_demand(5, closing_stock=12, hours_in_stock=24)
check("the same sales with stock left over IS a normal day", normal["kind"] == C.NORMAL)
check("...and is taken at face value", normal["floor"] is False and normal["weight"] == 1.0)
check("the two are told apart despite identical sales",
      d["weight"] != normal["weight"] and d["floor"] != normal["floor"])

print("\n— sold out at 2pm is about 60% of a day —")
p = C.estimate_demand(5, closing_stock=0, hours_in_stock=14)
check("it reads as partial", p["kind"] == C.PARTIAL, p["kind"])
check("availability is measured, not guessed", p["availability"] == 0.583, str(p["availability"]))
check("the estimate is scaled up", p["estimate"] > 5, str(p["estimate"]))
check("...to roughly a full day's worth", 8.0 < p["estimate"] < 9.0, str(p["estimate"]))
check("the observed number is still kept", p["observed"] == 5)
check("it is flagged as a floor", p["floor"] is True)
check("it counts about as much as the time it was open",
      abs(p["weight"] - 0.583) < 0.01, str(p["weight"]))

print("\n— a 90-minute window cannot invent a number —")
tiny = C.estimate_demand(5, closing_stock=0, hours_in_stock=1.5)
check("6% of a day is treated as no information", tiny["kind"] == C.UNAVAILABLE, tiny["kind"])
check("...so nothing is estimated from it", tiny["estimate"] is None)
check("...and it carries no weight in the fit", tiny["weight"] == 0.0)
near = C.estimate_demand(5, closing_stock=0, hours_in_stock=2.5)
check("just above the floor, it IS used", near["kind"] == C.PARTIAL, near["kind"])
check("...but the uplift is capped", near["capped"] is True, str(near))
check("...at 3x, not 10x", near["estimate"] == 15.0, str(near["estimate"]))
check("uncapped arithmetic would have said 48", round(5 / (2.5 / 24)) == 48)

print("\n— a full stockout day is excluded, not recorded as zero demand —")
z = C.estimate_demand(0, closing_stock=0, hours_in_stock=0)
check("it is unavailable", z["kind"] == C.UNAVAILABLE)
check("it contributes nothing", z["weight"] == 0.0 and z["estimate"] is None)
real_zero = C.estimate_demand(0, closing_stock=40, hours_in_stock=24)
check("a genuine no-sale day IS a real zero", real_zero["kind"] == C.NORMAL)
check("...and is kept at full weight", real_zero["estimate"] == 0.0 and real_zero["weight"] == 1.0)
check("the two zeros are not confused",
      z["weight"] != real_zero["weight"] and z["estimate"] != real_zero["estimate"])

print("\n— untracked days behave exactly as before —")
u = C.estimate_demand(7)
check("no availability data means a normal day", u["kind"] == C.NORMAL)
check("...taken at face value", u["estimate"] == 7 and u["weight"] == 1.0)
check("...and not flagged", u["floor"] is False)
check("bad availability data is ignored rather than trusted",
      C.estimate_demand(7, hours_in_stock="nonsense")["kind"] == C.NORMAL)

print("\n— slack in the clock doesn't mark ordinary days as censored —")
check("23.5h of 24 still counts as a full day",
      C.estimate_demand(5, closing_stock=3, hours_in_stock=23.5)["kind"] == C.NORMAL)
check("...even ending at zero it is 'capped', not 'partial'",
      C.estimate_demand(5, closing_stock=0, hours_in_stock=23.5)["kind"] == C.CAPPED)

print("\n— the direction of the bias, over a window —")
# Two weeks where the busiest days sold out. Dropping them (the old behaviour) removes
# the top of the distribution; keeping them raw understates it.
rows = ([{"date": f"2026-08-{i:02d}", "units_sold": 4, "closing_stock": 20, "hours_in_stock": 24}
         for i in range(1, 11)] +
        [{"date": f"2026-08-{i:02d}", "units_sold": 9, "closing_stock": 0, "hours_in_stock": 10}
         for i in range(11, 15)])
s = C.summarise(rows)
check("it counts the censored days", s["censoredDays"] == 4, str(s))
check("...as a share", s["censoredShare"] == 28.6, str(s["censoredShare"]))
raw_total = sum(r["units_sold"] for r in rows)
check("raw sales total 76", raw_total == 76, str(raw_total))
check("the estimate is higher than raw sales", s["estimatedUnits"] > raw_total, str(s))
check("...and it names the hidden units", s["hiddenUnits"] > 0)
check("...and the percentage understated", s["understatedPct"] > 0, str(s["understatedPct"]))

print("\n— the explanation is in plain words —")
e = C.explain(s)
check("it says how many days were stock-limited", "limited by stock, not by demand" in e, e)
check("it names the partial days", "sold out partway" in e, e)
check("it quantifies what was lost", "units you couldn't sell" in e, e)
clean = C.summarise([{"date": "2026-08-01", "units_sold": 3, "closing_stock": 10, "hours_in_stock": 24}])
check("a clean window says so plainly",
      "sales and demand are the same thing" in C.explain(clean), C.explain(clean))
check("no data produces no claim", C.explain({}) == "")

print("\n— hours in stock, from timestamped inventory readings —")
S = lambda h, n: {"ts": f"2026-08-10T{h:02d}:00:00Z", "onHand": n}
full = [S(0, 50), S(6, 40), S(12, 30), S(18, 20), S(23, 10)]
check("in stock all day reads as ~24h",
      C.hours_in_stock_from_samples(full, "2026-08-10") == 24.0,
      str(C.hours_in_stock_from_samples(full, "2026-08-10")))
sellout = [S(0, 5), S(8, 2), S(14, 0), S(20, 0)]
h = C.hours_in_stock_from_samples(sellout, "2026-08-10")
check("hitting zero at 2pm reads as 14h", h == 14.0, str(h))
check("...which is 58% availability", round(C.availability(h), 3) == 0.583, str(C.availability(h)))
empty_all_day = [S(0, 0), S(12, 0), S(23, 0)]
check("empty all day reads as 0h",
      C.hours_in_stock_from_samples(empty_all_day, "2026-08-10") == 0.0)
restock = [S(0, 0), S(9, 0), S(10, 60), S(20, 55)]
hr = C.hours_in_stock_from_samples(restock, "2026-08-10")
check("a restock at 10am gives back the rest of the day", hr == 14.0, str(hr))

print("\n— a polling gap must not look like a stockout —")
check("no samples at all yields None, not zero",
      C.hours_in_stock_from_samples([], "2026-08-10") is None)
check("samples only from other days yield None",
      C.hours_in_stock_from_samples([{"ts": "2026-09-01T00:00:00Z", "onHand": 5}], "2026-08-10") is None)
check("a None result is treated as a full, normal day",
      C.estimate_demand(5, closing_stock=None,
                        hours_in_stock=C.hours_in_stock_from_samples([], "2026-08-10"))["kind"] == C.NORMAL)
check("malformed samples are skipped, not fatal",
      C.hours_in_stock_from_samples([{"ts": "not-a-date", "onHand": 1}, S(0, 10)], "2026-08-10") == 24.0)
check("a reading from before the day carries into it",
      C.hours_in_stock_from_samples([{"ts": "2026-08-09T22:00:00Z", "onHand": 8}], "2026-08-10") == 24.0)

# ─────────────────────────────────────────────────────────────────────────────────────
import datetime as _dt
import tempfile
import stock_log as SL


def at(y, m, d, hh=12):
    return _dt.datetime(y, m, d, hh, tzinfo=_dt.timezone.utc)


def slog():
    fh = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    fh.close(); os.unlink(fh.name)
    return SL.StockLog(fh.name)


print("\n— building the availability record by polling —")
lg = slog()
for hour, level in [(0, 5), (8, 2), (14, 0), (20, 0)]:
    lg.record({"A": level}, now=at(2026, 8, 10, hour))
check("it derives the sell-out hour", lg.hours_in_stock("A", "2026-08-10") == 14.0,
      str(lg.hours_in_stock("A", "2026-08-10")))
check("...and that becomes a partial day",
      C.estimate_demand(5, 0, lg.hours_in_stock("A", "2026-08-10"))["kind"] == C.PARTIAL)
check("an unpolled day says 'unknown', not 'stocked out'",
      lg.hours_in_stock("A", "2026-01-01") is None)
check("...so that day is scored as normal, not deleted",
      C.estimate_demand(4, None, lg.hours_in_stock("A", "2026-01-01"))["kind"] == C.NORMAL)

print("\n— a flat product doesn't bloat the log —")
lg2 = slog()
for i in range(50):
    lg2.record({"B": 40}, now=at(2026, 8, 10, 0) + _dt.timedelta(hours=i))
check("50 identical readings collapse to 2", len(lg2.samples("B")) == 2, str(len(lg2.samples("B"))))
check("...and the tail stays current",
      lg2.samples("B")[-1]["ts"].startswith("2026-08-12"), lg2.samples("B")[-1]["ts"])
check("...and it still reads as in stock all day",
      lg2.hours_in_stock("B", "2026-08-11") == 24.0, str(lg2.hours_in_stock("B", "2026-08-11")))
lg2.record({"B": 0}, now=at(2026, 8, 12, 6))
check("a CHANGE is always recorded", len(lg2.samples("B")) == 3, str(len(lg2.samples("B"))))

print("\n— the record survives a restart —")
p = slog().path
a1 = SL.StockLog(p)
a1.record({"A": 9}, now=at(2026, 8, 10, 0))
a1.record({"A": 0}, now=at(2026, 8, 10, 15))
a2 = SL.StockLog(p)
check("samples come back", len(a2.samples("A")) == 2, str(len(a2.samples("A"))))
check("...and still derive the same hours", a2.hours_in_stock("A", "2026-08-10") == 15.0,
      str(a2.hours_in_stock("A", "2026-08-10")))
check("a corrupt file degrades to empty rather than crashing",
      len(SL.StockLog(os.devnull).samples("A")) == 0)

print("\n— coverage is reported, not assumed —")
cov = a2.coverage("A")
check("it says when tracking started", cov["trackingSince"] == "2026-08-10", str(cov))
check("...and how many samples exist", cov["samples"] == 2, str(cov))
check("an empty log reports nothing rather than pretending",
      slog().coverage()["samples"] == 0)

print("\n— a series across a window —")
lg3 = slog()
lg3.record({"A": 10}, now=at(2026, 8, 10, 0))
lg3.record({"A": 0}, now=at(2026, 8, 11, 12))
lg3.record({"A": 30}, now=at(2026, 8, 13, 0))
ser = lg3.availability_series("A", "2026-08-10", "2026-08-13")
by = {r["date"]: r for r in ser}
check("a full day reads 1.0", by["2026-08-10"]["availability"] == 1.0, str(by["2026-08-10"]))
check("the sell-out day reads half", by["2026-08-11"]["availability"] == 0.5, str(by["2026-08-11"]))
check("the empty day reads 0", by["2026-08-12"]["availability"] == 0.0, str(by["2026-08-12"]))
check("the restock day is available again", by["2026-08-13"]["availability"] == 1.0, str(by["2026-08-13"]))

print("\n— a store that won't answer costs one reading, not the tick —")
def dead(**_k):
    raise RuntimeError("429 rate limited")
r = SL.sample_from_shopify(slog(), fetch=dead)
check("the failure is contained", r["ok"] is False and r["added"] == 0, str(r))
def store(**_k):
    return {"A": {"stock": 12}, "B": {"stock": 0}, "C": {"stock": None}}
lg4 = slog()
r2 = SL.sample_from_shopify(lg4, fetch=store, now=at(2026, 8, 10, 9))
check("a good read files every SKU with a figure", r2["added"] == 2, str(r2))
check("...including a legitimate zero", lg4.samples("B")[0]["onHand"] == 0.0)
check("...and skips SKUs with no figure", lg4.samples("C") == [])

# ─────────────────────────────────────────────────────────────────────────────────────
import pandas as pd

print("\n— fitting on demand instead of sales —")
# A product selling ~4/day normally, but 4 busy days where it sold out.
def frame():
    rows = []
    for i in range(1, 11):
        rows.append({"ds": pd.Timestamp(f"2026-08-{i:02d}"), "y": 4.0, "units_in_stock": 20})
    for i in range(11, 15):
        rows.append({"ds": pd.Timestamp(f"2026-08-{i:02d}"), "y": 9.0, "units_in_stock": 0})
    return pd.DataFrame(rows)

avail = {f"2026-08-{i:02d}": 12.0 for i in range(11, 15)}   # sold out at midday
adj, rep = C.apply_to_frame(frame(), avail)

old_kept = frame()[frame()["units_in_stock"] > 0]
check("the OLD rule deleted the 4 busiest days", len(old_kept) == 10, str(len(old_kept)))
check("...and its daily mean was only 4.0", round(old_kept["y"].mean(), 2) == 4.0,
      str(round(old_kept["y"].mean(), 2)))
check("the new rule keeps all 14 days", len(adj) == 14, str(len(adj)))
check("...and the busy days are uplifted", adj["y"].max() == 18.0, str(adj["y"].max()))
check("...raising the daily mean above 4", adj["y"].mean() > 4.0, str(round(adj["y"].mean(), 2)))
check("it reports what it changed", rep["partial"] == 4 and rep["adjusted"] == 4, str(rep))
check("...and how many units the raw sales hid", rep["addedUnits"] > 0, str(rep["addedUnits"]))
check("nothing was removed — every day was buyable at some point", rep["removed"] == 0, str(rep))

print("\n— a day with nothing to sell is still excluded —")
f2 = frame()
f2.loc[len(f2)] = {"ds": pd.Timestamp("2026-08-15"), "y": 0.0, "units_in_stock": 0}
adj2, rep2 = C.apply_to_frame(f2, {**avail, "2026-08-15": 0.0})
check("the empty day is dropped", len(adj2) == 14, str(len(adj2)))
check("...and counted as removed", rep2["removed"] == 1, str(rep2))
check("...not recorded as a zero-demand day", 0.0 not in list(adj2["y"])[10:])

print("\n— an untracked catalogue is completely unaffected —")
plain = pd.DataFrame([{"ds": pd.Timestamp(f"2026-08-{i:02d}"), "y": float(i)} for i in range(1, 11)])
adj3, rep3 = C.apply_to_frame(plain, None)
check("every row survives", len(adj3) == 10)
check("every value is untouched", list(adj3["y"]) == list(plain["y"]))
check("...and it says nothing was adjusted", rep3["adjusted"] == 0 and rep3["removed"] == 0, str(rep3))
check("...and reports that tracking is off", rep3["tracked"] is False)

print("\n— a spreadsheet with closing stock still gets the capped treatment —")
# No hours tracked, but units_in_stock == 0 tells us the day ended empty.
adj4, rep4 = C.apply_to_frame(frame(), None)
check("those days are capped, not dropped", rep4["capped"] == 4, str(rep4))
check("...and kept in the fit", len(adj4) == 14, str(len(adj4)))
check("...at their observed value, not inflated", adj4["y"].max() == 9.0, str(adj4["y"].max()))
check("...which still beats deleting them entirely", adj4["y"].mean() > old_kept["y"].mean(),
      f"{adj4['y'].mean()} vs {old_kept['y'].mean()}")

print("\n— a spreadsheet day that opened empty, sold nothing and closed empty is not a zero —")
# Yesterday closed at 0 and nothing came in: there was nothing to sell, so the day says
# nothing about demand (a Black Friday spent out of stock used to read as a -74% holiday).
oos = pd.DataFrame([{"ds": pd.Timestamp(f"2026-09-{i:02d}"), "y": y, "units_in_stock": st}
                    for i, (y, st) in enumerate([(5, 20), (6, 14), (14, 0), (0, 0), (0, 0), (0, 30), (7, 23)], 1)])
adj5, rep5 = C.apply_to_frame(oos, None)
check("the two empty-shelf days are left out", rep5["removed"] == 2 and len(adj5) == 5, str(rep5))
check("the day it sold out is kept as a floor", rep5["capped"] == 1 and 14.0 in list(adj5["y"]), str(rep5))
check("the restock day (sold 0 but ended with 30) counts as a real zero", 0.0 in list(adj5["y"]), list(adj5["y"]))

print("\n— an empty frame doesn't explode —")
e0, r0 = C.apply_to_frame(pd.DataFrame(), {})
check("it returns cleanly", r0["days"] == 0, str(r0))
check("None is handled too", C.apply_to_frame(None, {})[1]["days"] == 0)

print(f"\n=== {_pass} passed, {_fail} failed ===")
sys.exit(1 if _fail else 0)

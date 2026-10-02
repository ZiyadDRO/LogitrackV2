"""
test_livelog_late.py — a week locked late is graded on the days it actually forecast.

The week runs Sunday to Saturday. If the app wasn't running on Sunday, the first lock
happens later, when the forecast already starts after the days that have sold. Those
days must drop out of both sides of the comparison, not shift everything by a day.

Run:  python test_livelog_late.py
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import tempfile
import warnings

_TMP = tempfile.mkdtemp()
os.environ.update({
    "LOGITRACK_CONNECTIONS": os.path.join(_TMP, "connections.json"),
    "LOGITRACK_WORKSPACES": os.path.join(_TMP, "workspaces"),
    "LOGITRACK_SYNC_STATE": os.path.join(_TMP, "sync_state.json"),
    "LOGITRACK_LIVE_PRICES_PATH": os.path.join(_TMP, "lp.json"),
    "LOGITRACK_HOLIDAYS_PATH": os.path.join(_TMP, "hol.json"),
    "LOGITRACK_CLOSED_PATH": os.path.join(_TMP, "closed.json"),
    "LOGITRACK_SYNC_ENABLED": "0", "LOGITRACK_SAMPLE_ENABLED": "0", "LOGITRACK_PERSIST": "0",
})
warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd            # noqa: E402
import forecast_log as FL      # noqa: E402
import main as M               # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILURES.append(name)


SUN = dt.date(2026, 8, 2)                      # a Sunday; that week ends Sat Aug 8


def fc_from(first_day, n=20):
    """A forecast whose value on each day is its day-of-month, so sums name the days."""
    ds = pd.date_range(pd.Timestamp(first_day), periods=n)
    y = [float(d.day) for d in ds]
    return pd.DataFrame({"ds": ds, "yhat": y, "yhat_lower": [v - 1 for v in y],
                         "yhat_upper": [v + 1 for v in y]})


print("the week's total is taken by date")
on_time = M._horizon_prediction(fc_from(SUN), 7, start=SUN)
check("locked on Sunday: all seven days, Sun..Sat", on_time["days"] == 7
      and on_time["predicted"] == sum(range(2, 9)), on_time)
late = M._horizon_prediction(fc_from(SUN + dt.timedelta(days=1)), 7, start=SUN)
check("locked Monday: Mon..Sat only, not Mon..Sun", late["days"] == 6
      and late["predicted"] == sum(range(3, 9)) and late["countFrom"] == "2026-08-03", late)
early = M._horizon_prediction(fc_from(SUN - dt.timedelta(days=1)), 7, start=SUN)
check("Saturday's sales not in yet: still Sun..Sat, not Sat..Fri", early["days"] == 7
      and early["predicted"] == sum(range(2, 9)), early)
check("the old behaviour without a start date is unchanged",
      M._horizon_prediction(fc_from(SUN), 7)["predicted"] == sum(range(2, 9)))

print("\nthe log grades only the counted days")
log = FL.ForecastLog(os.path.join(_TMP, "log.json"))
log.snapshot([{"sku": "A", "predicted": late["predicted"], "lo": late["lo"], "hi": late["hi"],
               "band": 80, "horizonDays": 7, "countFrom": late["countFrom"], "daily": late["daily"]}],
             origin=SUN, now=dt.datetime(2026, 8, 3, 9, tzinfo=dt.timezone.utc))
e = log.all()[0]
check("the entry still belongs to the Sunday week", e["origin"] == "2026-08-02" and e["dueAt"] == "2026-08-08")
check("...counted from Monday, six days", e["countFrom"] == "2026-08-03" and e["daysCounted"] == 6, e)
asked = []
log.score_due(lambda sku, a, b: asked.append((a, b)) or 40.0,
              now=dt.datetime(2026, 8, 9, 9, tzinfo=dt.timezone.utc))
check("sales are read for Mon..Sat", asked == [("2026-08-03", "2026-08-08")], asked)
wk = FL.by_week(log.all())[0]
check("the week tile says where counting started", wk["countedFrom"] == "2026-08-03", wk)

log2 = FL.ForecastLog(os.path.join(_TMP, "log2.json"))
log2.snapshot([{"sku": "A", "predicted": 10.0, "horizonDays": 7, "countFrom": "2026-08-02"}], origin=SUN)
check("an on-time week carries no note", FL.by_week(log2.all())[0]["countedFrom"] is None)
old = FL.make_entry("A", SUN, 7, 10.0)
check("entries without a start count from the Sunday", old["countFrom"] == "2026-08-02" and old["daysCounted"] == 7)

print("\na promotion saved on Thursday for Friday and Saturday")
import today_sales as TS                                    # noqa: E402
import uplift as UP                                         # noqa: E402
M._today_sales = TS.TodaySales(os.path.join(_TMP, "today.json"))


class _Live:
    def __init__(self):
        self.p = {}

    def get(self, sid):
        return self.p.get(sid)


M._live = _Live()
EL = -2.0


def product(sid, *, applies=True, reason="measured"):
    hist = pd.DataFrame({"ds": pd.date_range("2026-05-01", "2026-08-01"), "y": 5.0})
    M._catalog[sid] = {"events": [], "df": hist, "sku_name": sid}
    M._sku_cache[sid] = {"last_price": 10.0, "df_train": hist, "events": [],
                         "price_response": {"applies": applies, "reason": reason, "elasticity": EL,
                                            "knownRange": None, "bounds": None}}


def seal(sid, log_name):
    M._flog = FL.ForecastLog(os.path.join(_TMP, log_name))
    pred = M._horizon_prediction(fc_from(SUN), 7, start=SUN)
    for d in pred["daily"]:
        d["price"] = 10.0
    M._flog.snapshot([{"sku": sid, "predicted": pred["predicted"], "lo": pred["lo"], "hi": pred["hi"],
                       "band": 80, "horizonDays": 7, "countFrom": pred["countFrom"], "daily": pred["daily"]}],
                     origin=SUN)
    return pred


def day_y(sid, iso):
    return next(d["y"] for d in M._flog.all()[0]["daily"] if d["d"] == iso)


product("P")
orig = seal("P", "log3.json")
M._catalog["P"]["events"] = [{"type": "promotion", "date": "2026-08-07", "end_date": "2026-08-08",
                              "discount_pct": 20}]
THU10 = pd.Timestamp("2026-08-06 10:00")
res = M._livelog_reconcile(["P"], trigger="planned", now=THU10)
r = UP.price_response(EL, 10.0, 8.0, None) / UP.price_response(EL, 10.0, 10.0, None)
check("one week revised", res["revised"] == 1, res)
check("Sunday to Thursday keep their Sunday forecast",
      all(day_y("P", f"2026-08-0{k}") == k for k in range(2, 7)))
check("Friday and Saturday take the discount's effect on top of it",
      abs(day_y("P", "2026-08-07") - 7 * r) < 0.01 and abs(day_y("P", "2026-08-08") - 8 * r) < 0.01,
      (day_y("P", "2026-08-07"), 7 * r))
e = M._flog.all()[0]
check("the week total follows", abs(e["predicted"] - (sum(range(2, 7)) + 15 * r)) < 0.05, e["predicted"])
check("both figures kept, with the reason", e["originalPredicted"] == orig["predicted"]
      and "Fri–Sat" in e["amendReason"] and "$10.00 → $8.00" in e["amendReason"], e.get("amendReason"))
check("running it again changes nothing", M._livelog_reconcile(["P"], now=THU10)["revised"] == 0)
check("the week tile lists the reason", FL.by_week(M._flog.all())[0]["revisions"] == [e["amendReason"]])

print("\nthe shelf price changes at 2 PM on Thursday, unplanned")
product("Q")
seal("Q", "log4.json")
M._live.p["Q"] = {"listPrice": 8.0, "currentPrice": 8.0, "discounted": False, "source": "square"}
THU14 = pd.Timestamp("2026-08-06 14:00")
M._livelog_reconcile(["Q"], trigger="shelf", now=THU14)
share = (24 - 14) / 24
check("Sunday to Wednesday untouched", all(day_y("Q", f"2026-08-0{k}") == k for k in range(2, 6)))
check("Thursday takes the effect only on the part of the day after 2 PM (even split so far)",
      abs(day_y("Q", "2026-08-06") - 6 * ((1 - share) + share * r)) < 0.01, day_y("Q", "2026-08-06"))
check("Friday and Saturday take it in full",
      abs(day_y("Q", "2026-08-07") - 7 * r) < 0.01 and abs(day_y("Q", "2026-08-08") - 8 * r) < 0.01)
check("the reason says when", "from ~2 PM Thu" in M._flog.all()[0]["amendReason"], M._flog.all()[0]["amendReason"])
M._live.p["Q"] = {"listPrice": 10.0, "currentPrice": 10.0, "discounted": False, "source": "square"}
M._livelog_reconcile(["Q"], trigger="shelf", now=pd.Timestamp("2026-08-07 00:30"))
_s2 = 23.5 / 24
check("when the price goes back at 12:30 AM Friday, Saturday returns to its Sunday forecast",
      abs(day_y("Q", "2026-08-08") - 8) < 0.02, day_y("Q", "2026-08-08"))
check("...and Friday does too, apart from its first half hour at the old price",
      abs(day_y("Q", "2026-08-07") - 7 * r * ((1 - _s2) + _s2 / r)) < 0.02, day_y("Q", "2026-08-07"))
check("...Thursday keeps what it had", abs(day_y("Q", "2026-08-06") - 6 * ((1 - share) + share * r)) < 0.01)
check("...and both changes are listed", len(M._flog.all()[0]["revisions"]) == 2)

print("\na product whose forecast ignores price")
product("N", applies=False, reason="too-few-price-levels")
orig_n = seal("N", "log5.json")
M._catalog["N"]["events"] = [{"type": "promotion", "date": "2026-08-07", "end_date": "2026-08-08",
                              "discount_pct": 20}]
M._livelog_reconcile(["N"], trigger="planned", now=THU10)
en = M._flog.all()[0]
check("its figure doesn't move and it isn't marked revised",
      en["predicted"] == orig_n["predicted"] and not en.get("amended"))
check("but the new price is remembered, so it isn't found again",
      next(d["price"] for d in en["daily"] if d["d"] == "2026-08-07") == 8.0)

print("\ntwo checks at once apply a change once")
import threading                                            # noqa: E402
product("R")
seal("R", "log6.json")
M._live.p["R"] = {"listPrice": 8.0, "currentPrice": 8.0, "discounted": False, "source": "square"}
_orig_eff = M._price_effect


def _slow_eff(entry):
    fn = _orig_eff(entry)
    if fn is None:
        return None

    def g(day, p):
        import time as _t
        _t.sleep(0.05)
        return fn(day, p)
    return g


M._price_effect = _slow_eff
ths = [threading.Thread(target=M._livelog_reconcile, args=(["R"],), kwargs={"trigger": "shelf", "now": THU10})
       for _ in range(3)]
[t.start() for t in ths]; [t.join() for t in ths]
M._price_effect = _orig_eff
check("Friday is lifted once, not two or three times", abs(day_y("R", "2026-08-07") - 7 * r) < 0.01,
      (day_y("R", "2026-08-07"), 7 * r))
check("...and recorded once", len(M._flog.all()[0].get("revisions") or []) == 1,
      len(M._flog.all()[0].get("revisions") or []))

print("\na week already due for grading is never revised")
product("U")
seal("U", "log7.json")
M._catalog["U"]["events"] = [{"type": "promotion", "date": "2026-08-08", "end_date": "2026-08-08", "discount_pct": 20}]
res_u = M._livelog_reconcile(["U"], trigger="planned", now=pd.Timestamp("2026-08-09 00:30"))
check("Saturday evening in the US is Sunday on the log's clock: left alone", res_u["revised"] == 0, res_u)

print("\nsealing uses the prices the forecast itself was built with")
product("V")
M._sku_cache["V"]["future_fc"] = fc_from(FL.period_origin())
M._sku_cache["V"]["events"], M._sku_cache["V"]["pos_events"] = [], []
M._live.p["V"] = {"listPrice": 10.0, "currentPrice": 8.0, "discounted": True, "discountPct": 20.0,
                  "discountFrom": None, "source": "square"}     # on at the till, refit still queued
M._flog = FL.ForecastLog(os.path.join(_TMP, "log8.json"))
_keep = M._sku_cache
M._sku_cache = {"V": _keep["V"]}
M.livelog_snapshot({})
ev = M._flog.all()[0]
check("its days are sealed at the price its forecast assumed ($10), not the till's ($8)",
      all(d["price"] == 10.0 for d in ev["daily"]), [d["price"] for d in ev["daily"]][:3])
nxt = M._livelog_reconcile(["V"], trigger="shelf")
check("so the next check finds the discount and revises the week", nxt["revised"] == 1, nxt)
M._sku_cache = _keep

print("\nthe store's own shape of a day")
ts = TS.TodaySales(os.path.join(_TMP, "today2.json"))
check("before two weeks of readings, the day is split evenly", ts.share_after(18) == (6 / 24, "even"))
for k in range(15):
    day = (dt.date(2026, 7, 1) + dt.timedelta(days=k)).isoformat()
    for hh, u in ((9, 0), (12, 20), (15, 50), (18, 90), (22, 100)):
        ts.record(day, {"A": u}, hour=hh)
ts.clear_if_closed("2026-07-31")
sh, basis = ts.share_after(18)
check("after two weeks it uses the store's pattern", basis == "store" and abs(sh - 0.10) < 0.01, (sh, basis))
ts2 = TS.TodaySales(os.path.join(_TMP, "today3.json"))
ts2.record("2026-07-01", {"A": 40}, hour=15)
ts2.clear_if_closed("2026-07-02")
check("a day read only until 3 PM isn't learned from", ts2.profile_days() == 0)
check("the shape survives a restart", TS.TodaySales(os.path.join(_TMP, "today2.json")).profile_days() == 15)

print("\nthe Prophet route asks its own model")


class _Eng:
    events = []

    def _predict(self, events, price_override=None):
        ds = pd.date_range("2026-08-02", periods=10)
        return pd.DataFrame({"ds": ds, "yhat": [100.0 - 5.0 * float(price_override)] * 10})


fn = M._price_effect({"last_price": 10.0, "engine": _Eng(),
                      "price_response": {"applies": True, "reason": "modelled"}})
check("a price cut's effect is the model's own ratio", abs(fn("2026-08-07", 8.0) - 60.0 / 50.0) < 1e-9,
      fn("2026-08-07", 8.0))

print("\nend to end: a real catalog, the week locked, then a promotion saved for tomorrow")
import numpy as np                                          # noqa: E402
import catalog_store as _CS                                  # noqa: E402
import backtest_store as _BS                                 # noqa: E402
_CS._DIR = os.path.join(_TMP, "catalog_store")
_BS._DIR = os.path.join(_TMP, "backtest_store")
M._live = M._LP.LivePrices(os.path.join(_TMP, "lp2.json"))
M._flog = FL.ForecastLog(os.path.join(_TMP, "log_e2e.json"))
M._catalog.clear()
END = pd.Timestamp(M.today()).normalize() - pd.Timedelta(days=1)
DAYS = pd.date_range(END - pd.Timedelta(days=500), END)
rng = np.random.default_rng(5)
rows = []
for d in DAYS:
    price = 20.0 if (d.dayofyear // 30) % 2 else 16.0          # alternating months: a readable price effect
    lam = 12.0 * (price / 20.0) ** -2.0
    rows.append({"Date": d, "SKU": "E1", "SKU_Name": "E1", "Units_Sold": float(rng.poisson(lam)), "Price": price})
    rows.append({"Date": d, "SKU": "E2", "SKU_Name": "E2", "Units_Sold": float(rng.poisson(6.0)), "Price": 9.0})
with M._state_lock:
    M._ingest(pd.DataFrame(rows), "e2e.xlsx", append=False, auto_backtest=False)
M.livelog_snapshot({})
ents = {x["sku"]: x for x in M._flog.all()}
e1 = ents.get("E1")
check("each locked day remembers its price", e1 is not None and all(d.get("price") for d in e1["daily"]),
      e1 and e1["daily"][:2])
tmr = (pd.Timestamp(M._local_now()).normalize() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
if e1 is not None and tmr <= e1["dueAt"]:
    before = {d["d"]: d["y"] for d in e1["daily"]}
    M.save_events("E1", [{"type": "promotion", "date": tmr, "end_date": tmr, "discount_pct": 20}])
    after = {d["d"]: d["y"] for d in M._flog._entries[e1["id"]]["daily"]}
    applies = (M._cache()["E1"].get("price_response") or {}).get("applies")
    check("only tomorrow moved", all(after[k] == before[k] for k in before if k != tmr), (before, after))
    check("tomorrow went up if this product responds to price", (after[tmr] > before[tmr]) == bool(applies),
          (applies, before[tmr], after[tmr]))
    print("    ", applies, round(before[tmr], 2), "->", round(after[tmr], 2), M._flog._entries[e1["id"]].get("amendReason"))
    check("the other product is untouched", M._flog._entries[ents["E2"]["id"]]["predicted"] == ents["E2"]["predicted"])
else:
    print("  (skipped: tomorrow is past this week's end)")

print(f"\n{'All late-lock tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)

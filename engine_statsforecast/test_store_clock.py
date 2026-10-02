"""
The store's time zone decides which day everything belongs to (store_clock.py).

Each check is a day-boundary bug that happened because something counted days in UTC,
or in whichever browser zone last reached the server:
  - Live accuracy graded a week on Saturday evening (US), before Saturday's sales were in
  - Shopify filed every order after 7-8pm US time under the next day
  - sold-out hours were counted over UTC days
  - the nightly sync ran at 00:15 SERVER time (8:15pm Eastern on a UTC server)
  - a viewer's browser zone moved "today" for everyone

Offline. No network, no fits.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.setdefault("LOGITRACK_PERSIST", "0")
for _k, _f in (("LOGITRACK_CONNECTIONS", "connections.json"), ("LOGITRACK_WORKSPACES", "ws"),
               ("LOGITRACK_LIVE_PRICES_PATH", "live_prices.json"),
               ("LOGITRACK_CLOSED_PATH", "closed_days.json")):
    os.environ[_k] = os.path.join(_tmp, _f)

import store_clock as C

FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(name)


UTC = dt.timezone.utc

print("store_clock on its own")
C.set_zone(None)
check("unset: UTC, as the modules behaved before",
      C.local_date("2026-10-04T02:00:00Z") == dt.date(2026, 10, 4))
C.set_zone("America/New_York")
check("Eastern: 02:00 UTC Sunday is Saturday evening",
      C.local_date("2026-10-04T02:00:00Z") == dt.date(2026, 10, 3))
check("naive datetimes are taken as UTC",
      C.local_date(dt.datetime(2026, 10, 4, 2, 0)) == dt.date(2026, 10, 3))
check("a plain date is its own day", C.local_date("2026-10-04") == dt.date(2026, 10, 4))
check("summer: Eastern is UTC-4 (EDT), not EST",
      C.day_start("2026-07-01").utcoffset() == dt.timedelta(hours=-4))
check("winter: UTC-5 (EST)", C.day_start("2026-01-15").utcoffset() == dt.timedelta(hours=-5))
check("an invalid zone is refused", not C.valid("Mars/Base"))

print("Live accuracy waits for the store's Saturday to end")
import forecast_log as FL
entry = FL.make_entry("S", dt.date(2026, 9, 27), 7, 10.0)     # Sun Sep 27 .. Sat Oct 3
sat_evening = dt.datetime(2026, 10, 4, 1, 0, tzinfo=UTC)       # 9pm Sat Eastern
sun_morning = dt.datetime(2026, 10, 4, 14, 0, tzinfo=UTC)      # 10am Sun Eastern
check("not due at 9pm Saturday Eastern (already Sunday in UTC)", not FL.is_due(entry, sat_evening))
check("due on Sunday", FL.is_due(entry, sun_morning))

print("sold-out hours are counted over the store's days")
import censoring as CE
samples = [{"ts": "2026-10-04T03:00:00Z", "onHand": 0},            # 11pm Sat Eastern: sold out
           {"ts": "2026-10-05T14:00:00Z", "onHand": 20}]           # 10am Mon Eastern: restocked
check("an empty Sunday reads 0 hours in stock (was 3 over the UTC day)",
      CE.hours_in_stock_from_samples(samples, "2026-10-04") == 0.0,
      CE.hours_in_stock_from_samples(samples, "2026-10-04"))

print("Shopify orders are dated in the shop's zone")
import shopify_source as SH
check("Christmas Eve 8:30pm in New York stays on Christmas Eve",
      SH._local_day("2026-12-25T01:30:00Z", "America/New_York") == "2026-12-24")
check("without a known zone, the old UTC day is kept",
      SH._local_day("2026-12-25T01:30:00Z", None) == "2026-12-25")

print("the app's zone: picked > store's own > Eastern, never the browser's")
import main as M
check("default is Eastern", M._resolve_store_zone() == ("America/New_York", "default"),
      M._resolve_store_zone())
M.set_timezone("Asia/Tokyo")
M._apply_store_zone()
check("a browser reporting Tokyo doesn't move the store's clock", M._APP_TZ == "America/New_York", M._APP_TZ)
slot = M._current_slot()
M._closed.set_detected(slot, "America/Chicago", "square")
M._apply_store_zone()
check("a Square store's zone is used", M._resolve_store_zone() == ("America/Chicago", "square"))
check("today() follows it", C.zone() == "America/Chicago" and M._APP_TZ == "America/Chicago")
res = M.set_closed_days({"timezone": "America/Los_Angeles"})
check("picking one on the Closed days panel wins",
      res["timezone"]["zone"] == "America/Los_Angeles" and res["timezone"]["source"] == "set", res.get("timezone"))
check("closed days are left as they were", res["settings"] == {"dates": [], "yearly": []})
res = M.set_closed_days({"timezone": None})
check("clearing it goes back to the store's own", res["timezone"]["zone"] == "America/Chicago")
try:
    M.set_closed_days({"timezone": "Mars/Base"})
    check("an invalid zone is rejected", False)
except Exception as ex:                                   # noqa: BLE001
    check("an invalid zone is rejected", "time zone" in str(getattr(ex, "detail", ex)))
check("/api/store-clock reports the zone and the store's date",
      M.store_clock_view()["zone"] == "America/Chicago" and len(M.store_clock_view()["today"]) == 10)
wall = M._store_wall_clock()
check("the nightly sync reads the store's wall clock (naive, store-local)",
      wall.tzinfo is None and abs((wall - dt.datetime.now(dt.timezone.utc).astimezone(C.tzinfo())
                                   .replace(tzinfo=None)).total_seconds()) < 5)

print(f"\n{'All store-clock tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: {FAILURES}'}")
sys.exit(1 if FAILURES else 0)

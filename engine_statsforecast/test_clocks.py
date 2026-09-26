"""
Both clocks, driven through a simulated day and night, with manual runs mixed in.

The requirement being tested is simple to say and was not true: pressing "sync now" or
"read now" must never cost a scheduled run. Before this file:

  daily   a FAILED manual sync started the one-hour cooldown, so clicking the button at
          00:10 on a bad connection pushed the 00:15 run to 01:15. And the staleness rule,
          written for an older data shape, pulled the nightly run forward to 00:00:30
          every night — skipping the buffer that exists so late sales land first.

  hourly  a scheduled reading that fired while a manual one was in flight was dropped,
          and the thread went back to sleep for the full hour. After an outage the
          thread could sleep through a six-hour backoff even after "read now" had
          succeeded. A restart waited an hour from BOOT rather than from the last reading.
          Changing a setting took an unscheduled reading.

Every check here runs the real classes against a fake clock — the only way to prove what
happens at 00:15 without waiting for 00:15. No network, no server, no model fits.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

import scheduler as S

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


class Clock:
    def __init__(self, t):
        self.t = dt.datetime.fromisoformat(t)

    def __call__(self):
        return self.t

    def set(self, t):
        self.t = dt.datetime.fromisoformat(t)

    def advance(self, seconds):
        self.t = self.t + dt.timedelta(seconds=seconds)


def _tmp():
    fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd); os.unlink(p)
    return p


# ── the daily sync ─────────────────────────────────────────────────────────────────
def _daily(clock, newest_box, outcome_box):
    """A DailySync whose job marks all three stages ok/failed, and whose staleness is
    computed exactly as main.py computes it, from the newest sale on file."""
    ledger = S.DayLedger(path=_tmp(), clock=clock)

    def job(led, day):
        ok = outcome_box["ok"]
        for st in ("fetch", "refit", "backtest"):
            led.mark_stage(day, st, ok=ok)
        if ok:
            led.mark_complete(day, ("fetch", "refit", "backtest"))
            # a successful sync files through the last COMPLETE day
            newest_box["d"] = day - dt.timedelta(days=1)
        return {"ok": ok}

    def stale():
        return (clock().date() - newest_box["d"]).days > 2      # main.data_is_stale_by

    return S.DailySync(job, at="00:15", enabled=True, clock=clock, ledger=ledger,
                       is_stale=stale, state_path=ledger.path)


def _run_ticks(sync, clock, until, step=30):
    fired = []
    end = dt.datetime.fromisoformat(until)
    while clock() < end:
        rec = sync.tick()
        if rec:
            fired.append((clock().strftime("%H:%M:%S"), rec.get("trigger"), rec.get("ok")))
        clock.advance(step)
    return fired


def test_nightly_fires_at_0015_not_before():
    print("the nightly sync waits for 00:15 — it does not jump to midnight")
    c = Clock("2026-09-25T23:50:00")
    newest = {"d": dt.date(2026, 9, 24)}          # after last night's run: through the 24th
    sync = _daily(c, newest, {"ok": True})
    sync.ledger.mark_complete(dt.date(2026, 9, 25), ())     # the 25th's run already happened
    fired = _run_ticks(sync, c, "2026-09-26T00:30:00")
    check("exactly one run overnight", len(fired) == 1, fired)
    check("and it fired at 00:15, not at midnight",
          bool(fired) and fired[0][0].startswith("00:15"), fired)
    check("triggered by the schedule", bool(fired) and fired[0][1] == "schedule", fired)


def test_the_old_rule_would_have_fired_at_midnight():
    """Proves the test above can fail: with the old >1-day rule it fires at 00:00:30."""
    print("control: the previous staleness rule fires just after midnight")
    c = Clock("2026-09-25T23:50:00")
    newest = {"d": dt.date(2026, 9, 24)}
    sync = _daily(c, newest, {"ok": True})
    sync.is_stale = lambda: (c().date() - newest["d"]).days > 1
    sync.ledger.mark_complete(dt.date(2026, 9, 25), ())
    fired = _run_ticks(sync, c, "2026-09-26T00:30:00")
    check("the old rule fires before the slot", bool(fired) and fired[0][0] < "00:15", fired)


def test_manual_success_midday_leaves_tonight_alone():
    print("a manual sync during the day does not stop tonight's run")
    c = Clock("2026-09-26T14:00:00")
    newest = {"d": dt.date(2026, 9, 25)}
    sync = _daily(c, newest, {"ok": True})
    sync.ledger.mark_complete(dt.date(2026, 9, 26), ())
    rec = sync.run_now(trigger="manual")
    check("the manual sync ran", rec.get("ok") is True, rec)
    c.set("2026-09-26T23:55:00")
    fired = _run_ticks(sync, c, "2026-09-27T00:30:00")
    check("tonight's scheduled run still happens, at 00:15",
          len(fired) == 1 and fired[0][0].startswith("00:15") and fired[0][1] == "schedule", fired)


def test_failed_manual_does_not_delay_the_slot():
    print("a manual sync that FAILS at 00:10 does not push the 00:15 run back an hour")
    c = Clock("2026-09-27T00:10:00")
    newest = {"d": dt.date(2026, 9, 25)}
    outcome = {"ok": False}
    sync = _daily(c, newest, outcome)
    rec = sync.run_now(trigger="manual")
    check("the manual attempt failed", rec.get("ok") is False)
    outcome["ok"] = True                              # the connection comes back
    fired = _run_ticks(sync, c, "2026-09-27T00:30:00")
    check("the scheduled run still fires at 00:15",
          bool(fired) and fired[0][0].startswith("00:15") and fired[0][2] is True, fired)


def test_scheduled_failure_still_backs_off():
    """The cooldown must still protect the store from the poll — only manual is exempt."""
    print("a failing SCHEDULED run still waits an hour before retrying")
    c = Clock("2026-09-28T00:14:00")
    newest = {"d": dt.date(2026, 9, 26)}
    sync = _daily(c, newest, {"ok": False})
    fired = _run_ticks(sync, c, "2026-09-28T01:20:00")
    times = [f[0] for f in fired]
    check("one attempt at 00:15, the next about an hour later, nothing in between",
          len(times) == 2 and times[0].startswith("00:15") and times[1] >= "01:15", times)


def test_missed_night_catches_up_before_the_slot():
    print("a whole missed night is caught up immediately, not held to 00:15")
    c = Clock("2026-09-30T00:02:00")
    newest = {"d": dt.date(2026, 9, 26)}              # machine was off: 4 days behind
    sync = _daily(c, newest, {"ok": True})
    sync.ledger.mark_complete(dt.date(2026, 9, 27), ())
    fired = _run_ticks(sync, c, "2026-09-30T00:05:00")
    check("it runs straight away", len(fired) == 1 and fired[0][0] < "00:15", fired)


def test_manual_in_flight_at_the_slot():
    print("a manual sync running AT 00:15 does not cause a second run or a lost one")
    c = Clock("2026-10-01T00:15:00")
    newest = {"d": dt.date(2026, 9, 29)}
    sync = _daily(c, newest, {"ok": True})
    sync._running = True                              # a manual run is in progress
    first = sync.tick()
    check("the scheduled tick yields to it", first and first.get("skipped") == "already-running", first)
    sync._running = False
    sync.run_now(trigger="manual")                    # the manual run finishes the day
    fired = _run_ticks(sync, c, "2026-10-01T00:30:00")
    check("and no duplicate run follows", fired == [], fired)


# ── the hourly sampler ─────────────────────────────────────────────────────────────
class Log:
    def __init__(self):
        self.last = None


def _sampler(clock, log, outcome):
    def job():
        if outcome["ok"]:
            log.last = clock().replace(tzinfo=None)
            return {"ok": True, "added": 1}
        return {"ok": False, "reason": "sample-failed"}
    s = S.IntervalSampler(job, interval_seconds=3600, enabled=True, clock=clock,
                          last_sample_at=lambda: log.last)
    return s


def _hour_ticks(s, clock, hours, manual_at=(), step=30):
    scheduled, manual = [], []
    manual_at = set(manual_at)
    end = clock() + dt.timedelta(hours=hours)
    while clock() < end:
        hm = clock().strftime("%H:%M:%S")
        if hm in manual_at:
            s.sample_now(trigger="manual"); manual.append(hm)
        rec = s.tick()
        if rec:
            scheduled.append(clock().strftime("%H:%M"))
        clock.advance(step)
    return scheduled, manual


def test_hourly_cadence_ignores_manual_readings():
    print("manual readings do not move, swallow or delay the scheduled ones")
    c = Clock("2026-10-02T08:00:00")
    log = Log(); log.last = dt.datetime(2026, 10, 2, 7, 30)
    s = _sampler(c, log, {"ok": True})
    sched, man = _hour_ticks(s, c, 6, manual_at={"09:10:00", "10:29:30", "12:00:00"})
    check("three manual readings were taken", len(man) == 3, man)
    check("scheduled readings land every hour on the original cadence",
          sched == ["08:30", "09:30", "10:30", "11:30", "12:30", "13:30"], sched)


def test_collision_is_retried_not_dropped():
    print("a scheduled reading that collides with a manual one is retried, not lost")
    c = Clock("2026-10-03T10:29:50")
    log = Log(); log.last = dt.datetime(2026, 10, 3, 9, 30)
    s = _sampler(c, log, {"ok": True})
    s.tick()                                          # establishes next = 10:30
    c.set("2026-10-03T10:30:00")
    s._running = True                                 # manual reading in flight
    check("the colliding tick yields", s.tick() is None)
    s._running = False
    c.advance(30)
    rec = s.tick()
    check("and the scheduled reading happens on the next poll",
          rec is not None and rec.get("trigger") in ("schedule", "boot"), rec)


def test_manual_success_ends_a_backoff():
    print("after an outage, a successful manual reading pulls the schedule back in")
    c = Clock("2026-10-04T08:00:00")
    log = Log(); log.last = None
    outcome = {"ok": False}
    s = _sampler(c, log, outcome)
    for _ in range(5):                                # five failed scheduled readings
        s._next_at = c()
        s.tick()
    backed_off = (s._next_at - c()).total_seconds()
    check("the schedule had backed off beyond an hour", backed_off > 3600, backed_off)
    outcome["ok"] = True
    s.sample_now(trigger="manual")
    pulled = (s._next_at - c()).total_seconds()
    check("a successful manual reading brings it back to one hour", pulled <= 3600 + 1, pulled)


def test_restart_resumes_the_cadence():
    print("a restart resumes the cadence from the last reading, not from boot")
    c = Clock("2026-10-05T10:20:00")
    log = Log(); log.last = dt.datetime(2026, 10, 5, 9, 30)        # 50 min ago
    s = _sampler(c, log, {"ok": True})
    sched, _ = _hour_ticks(s, c, 1.2)
    check("the next reading is at 10:30, one hour after the last — not 11:20",
          bool(sched) and sched[0] == "10:30", sched)


def test_configure_takes_no_reading():
    print("changing a setting does not take a reading")
    c = Clock("2026-10-06T10:00:00")
    log = Log(); log.last = dt.datetime(2026, 10, 6, 9, 50)
    calls = {"n": 0}

    def job():
        calls["n"] += 1
        return {"ok": True}
    s = S.IntervalSampler(job, interval_seconds=3600, enabled=True, clock=c,
                          last_sample_at=lambda: log.last)
    s.tick()
    s.configure(interval_seconds=1800)
    s.tick()
    check("no reading was taken", calls["n"] == 0, calls)
    check("but the shorter interval applies from now", s._next_at <= c() + dt.timedelta(seconds=1800))


def test_asleep_for_hours_is_one_reading_not_a_burst():
    print("waking from six hours asleep takes one reading, not six")
    c = Clock("2026-10-07T08:00:00")
    log = Log(); log.last = dt.datetime(2026, 10, 7, 7, 30)
    s = _sampler(c, log, {"ok": True})
    s.tick()
    c.set("2026-10-07T14:10:00")                       # laptop lid was shut
    sched, _ = _hour_ticks(s, c, 0.5)
    check("exactly one catch-up reading", len(sched) == 1, sched)
    check("and the next is an hour after it", s._next_at == dt.datetime(2026, 10, 7, 15, 10), s._next_at)


if __name__ == "__main__":
    print("\nBoth clocks, with manual runs mixed in\n" + "-" * 48)
    for fn in (test_nightly_fires_at_0015_not_before,
               test_the_old_rule_would_have_fired_at_midnight,
               test_manual_success_midday_leaves_tonight_alone,
               test_failed_manual_does_not_delay_the_slot,
               test_scheduled_failure_still_backs_off,
               test_missed_night_catches_up_before_the_slot,
               test_manual_in_flight_at_the_slot,
               test_hourly_cadence_ignores_manual_readings,
               test_collision_is_retried_not_dropped,
               test_manual_success_ends_a_backoff,
               test_restart_resumes_the_cadence,
               test_configure_takes_no_reading,
               test_asleep_for_hours_is_one_reading_not_a_burst):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All clock tests passed.")

"""
test_scheduler.py — the nightly sync's timing, against a frozen clock.

Every case here is a way a real machine breaks a naive "every 24 hours" timer: a laptop
asleep through the slot, a restart at an awkward hour, a DST jump, a clock nudged by NTP,
two processes racing. None of them need a forecasting engine to reproduce, which is the
whole reason timing lives in its own module.

Run:  python test_scheduler.py
"""
from __future__ import annotations

import datetime as dt
import sys
import threading
import time

import os
import tempfile

import json

import scheduler as S

_STATE_DIR = tempfile.mkdtemp()
_n = [0]


def _state():
    """A fresh state path per scheduler, so persistence is exercised without tests
    leaking into each other."""
    _n[0] += 1
    return os.path.join(_STATE_DIR, f"s{_n[0]}.json")


def mark_done(sync, date, stages=None):
    """Pretend a day finished. last_run_date is derived from the ledger now, so a test
    that wants 'we already synced yesterday' has to say so the way the real thing does."""
    for st in (stages or sync.stages):
        sync.ledger.mark_stage(date, st, ok=True)
    sync.ledger.mark_complete(date, stages or sync.stages)
    return sync


FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def at(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += dt.timedelta(**kw)
        return self.now


# ─── Parsing ─────────────────────────────────────────────────────────────────

def test_parse_at():
    print("time-of-day parsing")
    check("a normal time parses", S.parse_at("00:15") == (0, 15))
    check("a late time parses", S.parse_at("23:59") == (23, 59))
    check("whitespace is tolerated", S.parse_at("  06:30 ") == (6, 30))
    for junk in ("nonsense", "25:00", "12:99", "", None, "12", 7, "-1:00"):
        check(f"{junk!r} falls back to the default rather than disabling the sync",
              S.parse_at(junk) == tuple(int(x) for x in S.DEFAULT_AT.split(":")),
              S.parse_at(junk))


def test_next_run_after():
    print("next occurrence")
    n = at(2026, 9, 22, 9, 0)
    check("later today when the slot is ahead",
          S.next_run_after(n, 23, 30) == at(2026, 9, 22, 23, 30))
    check("tomorrow when the slot has passed",
          S.next_run_after(n, 0, 15) == at(2026, 9, 23, 0, 15))
    check("exactly now counts as passed, so it rolls forward",
          S.next_run_after(at(2026, 9, 22, 0, 15), 0, 15) == at(2026, 9, 23, 0, 15))


# ─── Due-ness: the part that actually breaks in the field ────────────────────

def test_due_basics():
    print("is_due — the ordinary day")
    yesterday = dt.date(2026, 9, 21)
    check("before the slot, having run yesterday: not due",
          not S.is_due(at(2026, 9, 22, 0, 10), 0, 15, yesterday))
    check("at the slot: due",
          S.is_due(at(2026, 9, 22, 0, 15), 0, 15, yesterday))
    check("after the slot: due",
          S.is_due(at(2026, 9, 22, 9, 0), 0, 15, yesterday))
    check("already ran today: not due again",
          not S.is_due(at(2026, 9, 22, 9, 0), 0, 15, dt.date(2026, 9, 22)))
    check("still not due late the same evening",
          not S.is_due(at(2026, 9, 22, 23, 59), 0, 15, dt.date(2026, 9, 22)))


def test_at_most_once_per_day():
    """The failure this prevents: an elapsed-time timer firing twice when a clock moves."""
    print("at most once per calendar day")
    c = Clock(at(2026, 9, 22, 0, 15))
    fired = []
    def job(ledger, day):
        fired.append(c.now)
        mark_done(sync, day, ("fetch",))
        return {"ok": True}

    sync = S.DailySync(job, at="00:15", enabled=True, clock=c, poll_seconds=0.01,
                       state_path=_state(), stages=("fetch",))
    mark_done(sync, dt.date(2026, 9, 21))

    for _ in range(5):                                   # five loop passes, same day
        h, m = sync.hour_minute()
        if not sync.ledger.is_complete(c().date()) and S.is_due(c(), h, m, sync.last_run_date):
            sync.run_now(trigger="schedule")
        c.advance(minutes=1)
    check("fires exactly once despite repeated checks", len(fired) == 1, len(fired))

    c.advance(days=1)
    h, m = sync.hour_minute()
    if not sync.ledger.is_complete(c().date()) and S.is_due(c(), h, m, sync.last_run_date):
        sync.run_now(trigger="schedule")
    check("fires again the next day", len(fired) == 2, len(fired))


def test_clock_jumped_backwards():
    """NTP correction or a DST fall-back. An elapsed-time timer double-fires here."""
    print("a clock nudged backwards")
    check("having already run today, a backward nudge does not re-fire",
          not S.is_due(at(2026, 9, 22, 1, 0), 0, 15, dt.date(2026, 9, 22)))
    # DST spring-forward: 02:00 -> 03:00. The slot at 00:15 already passed; still once.
    check("a forward jump past the slot does not re-fire either",
          not S.is_due(at(2026, 3, 8, 3, 30), 0, 15, dt.date(2026, 3, 8)))


def test_missed_slot_catches_up():
    print("a machine asleep through the slot")
    two_days_ago = dt.date(2026, 9, 20)
    check("woken at 09:00 with two-day-old data: syncs now",
          S.is_due(at(2026, 9, 22, 9, 0), 0, 15, two_days_ago))
    check("and before the slot, still catches up on stale data",
          S.is_due(at(2026, 9, 22, 0, 5), 0, 15, two_days_ago))
    check("but data from yesterday's slot is not stale enough to jump the queue",
          not S.is_due(at(2026, 9, 22, 0, 5), 0, 15, dt.date(2026, 9, 21)))
    check("catch_up=False refuses to run early",
          not S.is_due(at(2026, 9, 22, 0, 5), 0, 15, two_days_ago, catch_up=False))


def test_first_boot_does_not_fire_instantly():
    print("a first launch is not hijacked by a sync")
    check("never run, before the slot: waits",
          not S.is_due(at(2026, 9, 22, 9, 0), 23, 30, None))
    check("never run, after the slot: runs, because the day's data is already due",
          S.is_due(at(2026, 9, 22, 9, 0), 0, 15, None))


# ─── Running ─────────────────────────────────────────────────────────────────

def test_records_success_and_failure():
    print("outcomes are recorded, not raised")
    c = Clock(at(2026, 9, 22, 0, 15))
    ok = S.DailySync(lambda: {"ok": True, "skus": 28}, clock=c, poll_seconds=0.01, state_path=_state())
    rec = ok.run_now()
    check("a success is marked ok", rec["ok"] is True)
    check("the job's own result is kept verbatim", rec["result"]["skus"] == 28)
    check("start and finish are both stamped", rec["startedAt"] and rec["finishedAt"])
    check("the trigger is recorded", rec["trigger"] == "manual")

    def boom():
        raise RuntimeError("store unreachable")

    bad = S.DailySync(boom, clock=c, poll_seconds=0.01, state_path=_state())
    rec2 = bad.run_now(trigger="schedule")
    check("a failure does not propagate", rec2["ok"] is False)
    check("and explains itself", "store unreachable" in rec2["error"], rec2)
    check("a failed run does NOT mark the day done — it must be retried",
          bad.last_run_date is None, bad.last_run_date)
    check("but it does record the attempt, so it backs off instead of hot-looping",
          bad.ledger.cooling_off(c.now.date()))
    check("a job returning ok=False is recorded as a failure",
          S.DailySync(lambda: {"ok": False, "reason": "guard"}, clock=c, state_path=_state()).run_now()["ok"] is False)


def test_no_concurrent_runs():
    print("two triggers don't overlap")
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(2)
        return {"ok": True}

    sync = S.DailySync(slow, poll_seconds=0.01, state_path=_state())
    t = threading.Thread(target=sync.run_now, daemon=True)
    t.start()
    check("the first run started", started.wait(1))
    second = sync.run_now(trigger="manual")
    check("a concurrent run is refused, not queued", second.get("skipped") == "already-running",
          second)
    release.set()
    t.join(2)
    check("the first run completed", sync.last_run["ok"] is True)


def test_configure_and_status():
    print("configuration and status")
    c = Clock(at(2026, 9, 22, 9, 0))
    sync = S.DailySync(lambda: {"ok": True}, at="00:15", enabled=True, clock=c, poll_seconds=0.01, state_path=_state())
    st = sync.status()
    check("status reports the schedule", st["at"] == "00:15" and st["enabled"] is True)
    check("next run is tomorrow's slot",
          st["nextRunAt"].startswith("2026-09-23T00:15"), st["nextRunAt"])
    check("nothing has run yet", st["lastRun"] is None)

    sync.configure(at="3:5")
    check("a loose time is normalised", sync.at == "03:05", sync.at)
    sync.configure(enabled=False)
    check("disabling clears the next run", sync.status()["nextRunAt"] is None)
    check("and reports disabled", sync.status()["enabled"] is False)
    sync.configure(at="junk")
    check("junk falls back to the default", sync.at == S.DEFAULT_AT, sync.at)


def test_thread_runs_it():
    """End to end through the real thread, on a fast poll."""
    print("the thread actually fires")
    c = Clock(at(2026, 9, 22, 0, 20))
    fired = []

    def once(ledger, day):
        fired.append(1)
        ledger.mark_stage(day, "fetch", ok=True)
        ledger.mark_complete(day, ("fetch",))
        return {"ok": True}

    sync = S.DailySync(once, at="00:15", enabled=True, clock=c, poll_seconds=0.01,
                       state_path=_state(), stages=("fetch",))
    mark_done(sync, dt.date(2026, 9, 21))
    sync.start()
    deadline = time.time() + 2
    # Wait for the run to be recorded too, not just started: `fired` is set inside the
    # run and `last_run` just after it, so on a busy machine the check could land between.
    while (not fired or sync.last_run is None) and time.time() < deadline:
        time.sleep(0.01)
    check("the scheduled run happened without anyone asking", fired == [1], fired)
    check("and was attributed to the schedule", sync.last_run["trigger"] == "schedule")
    sync.stop()
    n = len(fired)
    time.sleep(0.1)
    check("stopping ends it", len(fired) == n)

    # A disabled scheduler is inert even when the slot is due.
    fired2 = []
    off = S.DailySync(lambda: fired2.append(1) or {"ok": True},
                      at="00:15", enabled=False, clock=c, poll_seconds=0.01,
                      state_path=_state(), stages=("fetch",))
    mark_done(off, dt.date(2026, 9, 20))
    off.start()
    time.sleep(0.1)
    check("a disabled scheduler never fires", fired2 == [], fired2)
    off.stop()


def test_loop_survives_a_broken_job():
    print("a broken job doesn't kill the thread")
    c = Clock(at(2026, 9, 22, 9, 0))
    calls = []

    def flaky():
        calls.append(1)
        raise ValueError("nope")

    sync = S.DailySync(flaky, at="00:15", enabled=True, clock=c, poll_seconds=0.01, state_path=_state())
    mark_done(sync, dt.date(2026, 9, 20))
    sync.start()
    deadline = time.time() + 2
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    check("it ran and failed", len(calls) == 1, len(calls))
    time.sleep(0.15)
    check("and did not retry in a hot loop", len(calls) == 1, len(calls))
    c.advance(days=1)   # a new day clears both the cooling-off window and the day key
    deadline = time.time() + 2
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.01)
    check("the thread is still alive and tries again tomorrow", len(calls) == 2, len(calls))
    sync.stop()




def test_stale_data_brings_a_sync_forward():
    """The scheduler should ask the DATA, not a remembered timestamp."""
    print("stale data earns an early sync")
    before_slot = at(2026, 9, 22, 6, 0)          # slot is 23:30, so normally not due
    yesterday = dt.date(2026, 9, 21)
    check("fresh data waits for the slot",
          not S.is_due(before_slot, 23, 30, yesterday, stale=False))
    check("stale data does not wait",
          S.is_due(before_slot, 23, 30, yesterday, stale=True))
    check("but staleness never beats the once-a-day cap",
          not S.is_due(before_slot, 23, 30, dt.date(2026, 9, 22), stale=True),
          "a shop that sold nothing yesterday would re-pull forever")
    check("catch_up=False ignores staleness too",
          not S.is_due(before_slot, 23, 30, yesterday, stale=True, catch_up=False))

    # And through the real object: a stale checker that throws must not break the loop.
    def boom():
        raise RuntimeError("catalog locked")

    sync = S.DailySync(lambda: {"ok": True}, at="23:30", clock=Clock(before_slot),
                       is_stale=boom, poll_seconds=0.01, state_path=_state())
    check("a broken staleness check reads as 'not stale'", sync._stale() is False)
    check("and status still renders", sync.status()["dataStale"] is False)

    live = S.DailySync(lambda: {"ok": True}, at="23:30", clock=Clock(before_slot),
                       is_stale=lambda: True, poll_seconds=0.01, state_path=_state())
    check("status reports staleness", live.status()["dataStale"] is True)


def test_last_run_survives_a_restart():
    """The bug this fixes: last_run_date lived only in memory, so every restart looked
    like 'never synced' and pulled the whole store again."""
    print("the run date survives a restart")
    path = _state()
    c = Clock(at(2026, 9, 22, 9, 0))
    def job(ledger, day):
        ledger.mark_stage(day, "fetch", ok=True)
        ledger.mark_complete(day, ("fetch",))
        return {"ok": True, "skus": 28}

    first = S.DailySync(job, at="00:15", clock=c, poll_seconds=0.01,
                        state_path=path, stages=("fetch",))
    check("a fresh scheduler has no history", first.last_run_date is None)
    first.run_now(trigger="schedule")
    check("it recorded today", first.last_run_date == dt.date(2026, 9, 22))

    # Simulate a process restart against the same state file.
    second = S.DailySync(lambda: {"ok": True}, at="00:15", clock=c,
                         poll_seconds=0.01, state_path=path, stages=("fetch",))
    check("the restarted scheduler remembers the date",
          second.last_run_date == dt.date(2026, 9, 22), second.last_run_date)
    check("and the run record", (second.last_run or {}).get("result", {}).get("skus") == 28)
    h, m = second.hour_minute()
    check("so a restart does NOT trigger a redundant sync",
          not S.is_due(c(), h, m, second.last_run_date))

    c.advance(days=1)
    check("and tomorrow it is due again", S.is_due(c(), h, m, second.last_run_date))


def test_corrupt_state_file_is_survivable():
    print("a corrupt state file does not break the boot")
    path = _state()
    with open(path, "w") as fh:
        fh.write("{not json")
    def job(ledger, day):
        ledger.mark_stage(day, "fetch", ok=True)
        ledger.mark_complete(day, ("fetch",))
        return {"ok": True}

    sync = S.DailySync(job, poll_seconds=0.01, state_path=path, stages=("fetch",))
    check("it starts with no history rather than raising", sync.last_run_date is None)
    rec = sync.run_now()
    check("and overwrites it on the next run", rec["ok"] is True)
    sync2 = S.DailySync(job, poll_seconds=0.01, state_path=path, stages=("fetch",))
    check("which then reads back", sync2.last_run_date is not None, sync2.last_run_date)
    check("and the completed day survives", sync2.ledger.is_complete(dt.date.today()))




# ─── The stage ledger: the resume story ──────────────────────────────────────

def test_ledger_stages():
    print("the ledger records stages, only on success")
    led = S.DayLedger(path=_state(), clock=lambda: at(2026, 9, 22, 0, 15))
    d = dt.date(2026, 9, 22)
    check("nothing is done on a fresh day", led.pending_stages(d, ("fetch", "refit")) == ["fetch", "refit"])
    check("and the day is not complete", not led.is_complete(d))
    led.mark_stage(d, "fetch", ok=True)
    check("a finished stage is recorded", led.stage_done(d, "fetch"))
    check("and drops out of pending", led.pending_stages(d, ("fetch", "refit")) == ["refit"])
    led.mark_stage(d, "refit", ok=False, note="killed")
    check("a FAILED stage is not 'done'", not led.stage_done(d, "refit"))
    check("so it stays pending — an interrupted stage re-runs",
          led.pending_stages(d, ("fetch", "refit")) == ["refit"])
    check("and the day cannot be marked complete",
          led.mark_complete(d, ("fetch", "refit")) is False)
    led.mark_stage(d, "refit", ok=True)
    check("once every stage lands, the day completes",
          led.mark_complete(d, ("fetch", "refit")) is True)
    check("and reports complete", led.is_complete(d))


def test_ledger_survives_a_kill():
    """The whole point: quit mid-calculation and the next start resumes."""
    print("an interrupted day resumes; a finished day does not re-run")
    path = _state()
    d = dt.date(2026, 9, 22)
    clock = lambda: at(2026, 9, 22, 0, 20)

    first = S.DayLedger(path=path, clock=clock)
    first.mark_stage(d, "fetch", ok=True)          # fetched, then the process died
    del first

    resumed = S.DayLedger(path=path, clock=clock)
    check("the fetch is remembered", resumed.stage_done(d, "fetch"))
    check("the unfinished stages are still pending",
          resumed.pending_stages(d, ("fetch", "refit", "backtest")) == ["refit", "backtest"])
    check("the day is not complete", not resumed.is_complete(d))

    resumed.mark_stage(d, "refit", ok=True)
    resumed.mark_stage(d, "backtest", ok=True)
    resumed.mark_complete(d, ("fetch", "refit", "backtest"))
    after = S.DayLedger(path=path, clock=clock)
    check("a completed day survives a restart too", after.is_complete(d))
    check("with nothing left to do", after.pending_stages(d, ("fetch", "refit", "backtest")) == [])


def test_ledger_cooling_off():
    """A store that is down must not turn a 30-second poll into 2,880 API calls."""
    print("a failed attempt backs off")
    now = [at(2026, 9, 22, 0, 20)]
    led = S.DayLedger(path=_state(), clock=lambda: now[0])
    d = dt.date(2026, 9, 22)
    check("no attempts yet, so no cooling off", not led.cooling_off(d))
    led.note_attempt(d)
    check("straight after a failed attempt it backs off", led.cooling_off(d))
    now[0] = at(2026, 9, 22, 0, 40)
    check("twenty minutes later, still backing off", led.cooling_off(d))
    now[0] = at(2026, 9, 22, 1, 30)
    check("an hour later it will try again", not led.cooling_off(d))
    led.mark_stage(d, "fetch", ok=True)
    led.mark_complete(d, ("fetch",))
    now[0] = at(2026, 9, 22, 1, 31)
    led.note_attempt(d)
    check("a completed day never cools off — it simply isn't due", not led.cooling_off(d))


def test_ledger_prunes_and_migrates():
    print("the ledger stays small and upgrades in place")
    path = _state()
    led = S.DayLedger(path=path, clock=lambda: at(2026, 9, 22, 0, 15))
    for i in range(1, 25):
        led.mark_stage(dt.date(2026, 9, 1) + dt.timedelta(days=i), "fetch", ok=True)
    check("old days are pruned", len(led._days) <= led.RETAIN_DAYS, len(led._days))
    check("the newest is kept", led.stage_done(dt.date(2026, 9, 25), "fetch"))

    # The previous flat format must not cause a re-sync on the first boot after upgrading.
    legacy = _state()
    with open(legacy, "w") as fh:
        json.dump({"lastRunDate": "2026-09-22", "lastRun": {"ok": True}}, fh)
    up = S.DayLedger(path=legacy, clock=lambda: at(2026, 9, 22, 9, 0))
    check("a legacy run date reads as a completed day", up.is_complete(dt.date(2026, 9, 22)))
    check("and the old run record is kept", (up.last_run or {}).get("ok") is True)


def test_sync_resumes_missing_stages_only():
    print("the scheduler re-runs only what is outstanding")
    path = _state()
    c = Clock(at(2026, 9, 22, 0, 20))
    seen = []

    def job(ledger, day):
        seen.append(ledger.pending_stages(day, ("fetch", "refit", "backtest")))
        ledger.mark_stage(day, "fetch", ok=True)
        ledger.mark_stage(day, "refit", ok=True)
        # backtest deliberately left unfinished, as if the app were quit here
        return {"ok": False}

    sync = S.DailySync(job, at="00:15", clock=c, poll_seconds=0.01, state_path=path,
                       stages=("fetch", "refit", "backtest"))
    rec = sync.run_now(trigger="schedule")
    check("the first run saw everything pending", seen[0] == ["fetch", "refit", "backtest"])
    check("and the day is NOT marked complete", rec["complete"] is False, rec)
    check("nor does last_run_date advance", sync.last_run_date is None, sync.last_run_date)

    def finish(ledger, day):
        seen.append(ledger.pending_stages(day, ("fetch", "refit", "backtest")))
        ledger.mark_stage(day, "backtest", ok=True)
        ledger.mark_complete(day, ("fetch", "refit", "backtest"))
        return {"ok": True}

    sync2 = S.DailySync(finish, at="00:15", clock=c, poll_seconds=0.01, state_path=path,
                        stages=("fetch", "refit", "backtest"))
    rec2 = sync2.run_now(trigger="schedule")
    check("the restart saw ONLY the unfinished stage", seen[1] == ["backtest"], seen[1])
    check("it was marked a resume", rec2["resumed"] is True)
    check("and now the day is complete", rec2["complete"] is True)
    check("so last_run_date is today", sync2.last_run_date == dt.date(2026, 9, 22))

    st = sync2.status()
    check("status says today is done", st["todayComplete"] is True)
    check("with nothing pending", st["pendingStages"] == [])


def test_completed_day_is_not_redone():
    print("a finished day is left alone, however often the loop ticks")
    path = _state()
    c = Clock(at(2026, 9, 22, 9, 0))
    runs = []

    def job(ledger, day):
        runs.append(1)
        for st in ("fetch", "refit", "backtest"):
            ledger.mark_stage(day, st, ok=True)
        ledger.mark_complete(day, ("fetch", "refit", "backtest"))
        return {"ok": True}

    sync = S.DailySync(job, at="00:15", enabled=True, clock=c, poll_seconds=0.01,
                       state_path=path, stages=("fetch", "refit", "backtest"))
    sync.start()
    deadline = time.time() + 2
    while not runs and time.time() < deadline:
        time.sleep(0.01)
    check("it ran once", len(runs) == 1, len(runs))
    time.sleep(0.2)
    check("and the loop leaves the finished day alone", len(runs) == 1, len(runs))
    c.advance(days=1)
    deadline = time.time() + 2
    while len(runs) < 2 and time.time() < deadline:
        time.sleep(0.01)
    check("tomorrow it runs again", len(runs) == 2, len(runs))
    sync.stop()


def test_interrupted_day_resumes_without_waiting_for_tomorrow():
    """The bug in the first version: a raised job stamped the day done, so an interrupted
    refit never retried. This is the regression test for it."""
    print("an interrupted calculation does not wait until tomorrow")
    path = _state()
    now = [at(2026, 9, 22, 0, 20)]
    c = lambda: now[0]
    attempts = []

    def flaky(ledger, day):
        attempts.append(1)
        ledger.mark_stage(day, "fetch", ok=True)
        if len(attempts) == 1:
            raise RuntimeError("quit mid-refit")
        ledger.mark_stage(day, "refit", ok=True)
        ledger.mark_complete(day, ("fetch", "refit"))
        return {"ok": True}

    sync = S.DailySync(flaky, at="00:15", enabled=True, clock=c, poll_seconds=0.01,
                       state_path=path, stages=("fetch", "refit"))
    rec = sync.run_now(trigger="schedule")
    check("the interruption is recorded as a failure", rec["ok"] is False)
    check("the day is NOT complete", rec["complete"] is False)
    check("the fetch it did finish is remembered",
          sync.ledger.stage_done(dt.date(2026, 9, 22), "fetch"))
    check("it backs off briefly rather than hot-looping",
          sync.ledger.cooling_off(dt.date(2026, 9, 22)))

    now[0] = at(2026, 9, 22, 2, 0)                # an hour on
    check("then it is willing to try again",
          not sync.ledger.cooling_off(dt.date(2026, 9, 22)))
    rec2 = sync.run_now(trigger="schedule")
    check("the retry completes the day", rec2["complete"] is True, rec2)
    check("and did not re-fetch", len(attempts) == 2)


if __name__ == "__main__":
    for fn in (test_parse_at, test_next_run_after, test_due_basics,
               test_at_most_once_per_day, test_clock_jumped_backwards,
               test_missed_slot_catches_up, test_first_boot_does_not_fire_instantly,
               test_records_success_and_failure, test_no_concurrent_runs,
               test_configure_and_status, test_thread_runs_it,
               test_loop_survives_a_broken_job, test_stale_data_brings_a_sync_forward,
               test_last_run_survives_a_restart, test_corrupt_state_file_is_survivable,
               test_ledger_stages, test_ledger_survives_a_kill, test_ledger_cooling_off,
               test_ledger_prunes_and_migrates, test_sync_resumes_missing_stages_only,
               test_completed_day_is_not_redone,
               test_interrupted_day_resumes_without_waiting_for_tomorrow):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All scheduler tests passed.")

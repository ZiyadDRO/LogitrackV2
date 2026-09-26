"""
Hourly inventory sampling, and the thing it exists to make possible.

TWO KINDS OF SCHEDULED WORK. DailySync computes a STATE: a missed nightly run is run
late and produces the same answer, so it catches up. IntervalSampler observes a MOMENT:
nobody records how much stock you had at 2pm last Tuesday, so a missed hour is gone and
catching up is not merely unnecessary, it is meaningless. These tests hold that line,
because the two classes look similar enough that someone will eventually "fix" the
sampler by giving it catch-up.

AND THE POINT OF THE WHOLE EXERCISE. censoring.py corrects for demand you never saw
because you ran out — but it can only do that on a day it can tell you ran out PARTWAY
THROUGH. That judgement is reconstructed from timestamped readings. At one reading a
day the reconstruction has two possible answers, ~24h or ~0h, so the partial case is
unreachable and the correction never fires. The last test here is the one that matters:
it shows the partial case is dead at daily cadence and alive at hourly.

Offline. No network, no server, no model fits.
"""
from __future__ import annotations

import datetime as dt
import sys
import threading
import time

import censoring as C
import scheduler as S
import stock_log as SL


FAILURES: list[str] = []
UTC = dt.timezone.utc


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


def tmp_log():
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".json"); os.close(fd); os.unlink(path)
    return SL.StockLog(path=path)


# ── 1. it takes readings, and it never lets a failure escape ────────────────────────
def test_takes_a_reading():
    calls = []
    s = S.IntervalSampler(lambda: (calls.append(1), {"ok": True, "added": 3})[1])
    rec = s.sample_now(trigger="test")
    check("reading taken", len(calls) == 1)
    check("reported ok", rec["ok"] is True)
    check("result carried through", rec["result"]["added"] == 3)
    check("counted", s.status()["samplesTaken"] == 1)
    check("no failures recorded", s.status()["consecutiveFailures"] == 0)


def test_a_throwing_job_never_escapes():
    def boom():
        raise RuntimeError("store is down")
    s = S.IntervalSampler(boom)
    rec = s.sample_now()                       # must not raise
    check("failure captured, not raised", rec["ok"] is False)
    check("error recorded", "store is down" in rec.get("error", ""))
    check("failure counted", s.status()["consecutiveFailures"] == 1)


def test_a_falsy_result_counts_as_failure():
    # A real failure: the store was reachable-ish but gave us nothing usable. Contrast
    # with the skip case below — "no-saved-connection" is NOT this.
    s = S.IntervalSampler(lambda: {"ok": False, "reason": "no-levels-returned"})
    s.sample_now()
    check("ok:False counted as a failure", s.status()["consecutiveFailures"] == 1)


def test_a_skip_is_neither_success_nor_failure():
    """"No store connected yet" is not a malfunction. Counting it as one lit a red
    "readings are failing" warning on a fresh install where nothing was wrong."""
    s = S.IntervalSampler(lambda: {"ok": False, "skip": True, "reason": "no-saved-connection"},
                          interval_seconds=3600)
    base = s.status()["effectiveIntervalSeconds"]
    for _ in range(5):
        rec = s.sample_now()
    st = s.status()
    check("a skip is not counted as a failure", st["consecutiveFailures"] == 0, st)
    check("a skip is not counted as a reading either", st["samplesTaken"] == 0, st)
    check("and it does not back the interval off",
          st["effectiveIntervalSeconds"] == base, st["effectiveIntervalSeconds"])
    check("the attempt is still recorded, marked skipped", rec["skipped"] is True)

    # A genuine failure still counts, so the warning still means something.
    s.job = lambda: {"ok": False, "reason": "sample-failed"}
    s.sample_now()
    check("a real failure still counts", s.status()["consecutiveFailures"] == 1)


def test_failures_back_off_but_never_give_up():
    s = S.IntervalSampler(lambda: {"ok": False}, interval_seconds=3600)
    base = s.status()["effectiveIntervalSeconds"]
    for _ in range(20):
        s.sample_now()
    grown = s.status()["effectiveIntervalSeconds"]
    check("interval backed off while failing", grown > base, f"{base} -> {grown}")
    check("backoff is capped, so it keeps trying",
          grown <= 3600 * S._MAX_BACKOFF_MULTIPLIER, grown)
    s.job = lambda: {"ok": True}
    s.sample_now()
    check("one success resets the backoff", s.status()["effectiveIntervalSeconds"] == base)


def test_no_concurrent_readings():
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set(); release.wait(5); return {"ok": True}
    s = S.IntervalSampler(slow)
    t = threading.Thread(target=s.sample_now, daemon=True); t.start()
    started.wait(5)
    second = s.sample_now()
    release.set(); t.join(timeout=5)
    check("a second reading is skipped, not queued", second.get("skipped") == "already-running")


# ── 2. moment semantics: no catch-up, and boot asks the LOG ─────────────────────────
def test_never_catches_up():
    import inspect as _i
    src = _i.getsource(S.IntervalSampler)
    check("the sampler has no catch_up switch to turn on", "catch_up" not in src)
    check("DailySync still does catch up — the two are deliberately different",
          "catch_up" in _i.getsource(S.DailySync))


def test_boot_asks_the_log_not_a_timer():
    log = tmp_log()
    s = S.IntervalSampler(lambda: {"ok": True}, interval_seconds=3600,
                          last_sample_at=log.last_sample_at)
    check("nothing on file -> a reading is owed", s.due() is True)

    log.record({"A": 5})
    check("just filed -> not owed", s.due() is False)

    # A process that restarts every two minutes must not re-sample every time. Simulate
    # an old reading by rewriting its timestamp.
    stale = (dt.datetime.now(UTC) - dt.timedelta(hours=2)).isoformat()
    log._samples["A"][-1]["ts"] = stale
    check("an hour-old reading -> owed again", s.due() is True)


def test_missing_hours_are_simply_absent():
    """The honest consequence: a gap in the log stays a gap. Nothing invents a reading."""
    log = tmp_log()
    t0 = dt.datetime(2026, 5, 1, 8, 0, tzinfo=UTC)
    log.record({"A": 10}, now=t0)
    log.record({"A": 2}, now=t0 + dt.timedelta(hours=6))    # 6 unsampled hours between
    ts = [s["ts"] for s in log.samples("A")]
    check("only the hours actually sampled are on file", len(ts) == 2, ts)


# ── 3. the payoff: hourly makes the partial-day case reachable ──────────────────────
def _hours(log, day):
    return C.hours_in_stock_from_samples(log.samples("A"), day)


def test_daily_cadence_cannot_see_a_partial_day():
    """One reading a day: the product sold out at 11am and the log cannot tell."""
    log = tmp_log()
    day = "2026-05-04"
    # Yesterday's closing reading, then tonight's — which is all a nightly sync gets.
    log.record({"A": 12}, now=dt.datetime(2026, 5, 3, 23, 50, tzinfo=UTC))
    log.record({"A": 0}, now=dt.datetime(2026, 5, 4, 23, 50, tzinfo=UTC))
    h = _hours(log, day)
    cls = C.classify_day(units=4, closing_stock=0, hours_in_stock=h)
    check("nightly reading reports a nearly-full day of availability", h is not None and h > 23, h)
    check("so the day is classified CAPPED, never PARTIAL", cls == C.CAPPED, cls)
    # The uplift the next test earns is unavailable here: a full day of availability
    # scales by 1.0, so the four units stand as if they were the whole of demand.
    check("and no uplift is available to apply",
          C.uplift_factor(C.availability(h)) == 1.0, C.uplift_factor(C.availability(h)))


def test_hourly_cadence_sees_the_partial_day():
    """The same real day, sampled hourly: in stock until 11am, empty after."""
    log = tmp_log()
    day = "2026-05-04"
    log.record({"A": 12}, now=dt.datetime(2026, 5, 3, 23, 50, tzinfo=UTC))
    for hour in range(0, 24):
        level = 12 - hour if hour < 11 else 0
        log.record({"A": max(level, 0)}, now=dt.datetime(2026, 5, 4, hour, 0, tzinfo=UTC))
    h = _hours(log, day)
    cls = C.classify_day(units=4, closing_stock=0, hours_in_stock=h)
    check("hourly readings recover the real availability (~11h)",
          h is not None and 10 <= h <= 12, h)
    check("the day is now classified PARTIAL", cls == C.PARTIAL, cls)

    av = C.availability(h)
    factor = C.uplift_factor(av)
    check("an uplift is applied", factor > 1.0, factor)
    check("and it stays inside the cap", factor <= C.MAX_UPLIFT, factor)


def test_the_uplift_is_conservative_not_linear_fantasy():
    """A 90-minute window must not be projected into a full day's demand."""
    av = C.availability(1.5)                    # 1.5h of 24
    check("a very short window is below the floor", av < C.MIN_AVAILABILITY, av)
    check("uplift for a capped-out window never exceeds MAX_UPLIFT",
          C.uplift_factor(C.availability(4.0)) <= C.MAX_UPLIFT)


# ── 4. it can be switched off and driven from cron instead ──────────────────────────
def test_configure():
    s = S.IntervalSampler(lambda: {"ok": True})
    st = s.configure(enabled=False)
    check("can be disabled", st["enabled"] is False)
    check("a disabled sampler is never due", s.due() is False)
    st = s.configure(enabled=True, interval_seconds=120)
    check("interval configurable", st["intervalSeconds"] == 120)
    st = s.configure(interval_seconds=5)
    check("interval floored at 60s so nobody DoSes their own store", st["intervalSeconds"] == 60)


def test_thread_runs_it_and_stops():
    calls = []
    s = S.IntervalSampler(lambda: (calls.append(1), {"ok": True})[1], interval_seconds=60)
    s.start()
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    check("the thread took a reading at boot", len(calls) >= 1)
    s.stop()
    check("stop disarms it", s.status()["enabled"] is False)


if __name__ == "__main__":
    print("\nHourly stock sampling\n" + "-" * 44)
    for fn in (test_takes_a_reading,
               test_a_throwing_job_never_escapes,
               test_a_falsy_result_counts_as_failure,
               test_a_skip_is_neither_success_nor_failure,
               test_failures_back_off_but_never_give_up,
               test_no_concurrent_readings,
               test_never_catches_up,
               test_boot_asks_the_log_not_a_timer,
               test_missing_hours_are_simply_absent,
               test_daily_cadence_cannot_see_a_partial_day,
               test_hourly_cadence_sees_the_partial_day,
               test_the_uplift_is_conservative_not_linear_fantasy,
               test_configure,
               test_thread_runs_it_and_stops):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All sampler tests passed.")

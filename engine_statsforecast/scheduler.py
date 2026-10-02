"""
scheduler.py — when the nightly sync runs. Not what it does.

WHY THE SPLIT

The job needs the catalog, the source registry and the ingest pipeline; the timing needs a
clock and nothing else. Keeping them apart means the part that is easy to get subtly wrong
— "is it tomorrow yet", DST, a laptop that slept through 00:15 — is testable without
standing up a forecasting engine, which is why every case below can be exercised against a
frozen clock in milliseconds.

WHAT IT GUARANTEES

  · AT MOST ONCE PER CALENDAR DAY. The run is keyed on the local date it was for, not on
    "1440 minutes since the last one". A process that starts at 09:00 does not immediately
    fire yesterday's missed sync and then fire again at 00:15; a clock that jumps forward
    does not fire twice. That date is PERSISTED, because it used to live only in memory —
    so every restart looked like "never synced" and pulled the whole store again. Five
    restarts while working meant five full pulls and five refits.
  · IT ASKS THE DATA, NOT A TIMESTAMP. `is_stale()` reports whether the newest sale on
    file is actually old. A remembered run-time can be wrong in both directions — it says
    "synced today" after a run that fetched nothing, and "never synced" after a restart —
    whereas the age of the data is the thing anyone actually cares about and it is
    self-correcting.
  · IT CATCHES UP, ONCE. A machine asleep at 00:15 and woken at 09:00 syncs at 09:00 —
    the point is fresh data, and refusing to run because the moment passed would leave the
    tool stale all day. That is a deliberate choice and `catch_up=False` turns it off.
  · IT NEVER RAISES INTO THE THREAD. A sync that throws records the failure and waits for
    tomorrow. An unhandled exception in a daemon thread dies silently and takes every
    future sync with it, which is the worst of both worlds.

WHY JUST AFTER MIDNIGHT AND NOT AT MIDNIGHT

A sale rung up at 23:59 is in the till at 23:59, but a card can settle a little later and
a register can be a little slow to push. Syncing a few minutes into the new day costs
nothing and removes a whole class of "yesterday looks short" confusion.

THE ONE CAVEAT WORTH KNOWING

The time is SERVER-LOCAL. For a single store, or several in one timezone, that is what you
want. For locations spread across timezones, pick a time that is after midnight in the
WESTERNMOST one — at 00:15 in New York it is still 21:15 yesterday in Los Angeles, and a
sync then would capture a partial day for the California store. The connector assigns each
sale to its own location's calendar day correctly; this is only about when to ask.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading

DEFAULT_AT = "00:15"          # local, a few minutes into the new day
_POLL_SECONDS = 30.0          # how often the thread re-checks the clock
STATE_PATH = os.environ.get(
    "LOGITRACK_SYNC_STATE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_state.json"))


def _now_local():
    return _dt.datetime.now()


def parse_at(value) -> tuple:
    """'HH:MM' → (hour, minute). Falls back to the default rather than refusing to run:
    a typo in a config string should not silently disable the only thing keeping the data
    fresh."""
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError, TypeError):
        pass
    h, m = DEFAULT_AT.split(":")
    return int(h), int(m)


def next_run_after(now, hour, minute) -> _dt.datetime:
    """The next occurrence of hour:minute strictly after `now`."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += _dt.timedelta(days=1)
    return target


def is_due(now, hour, minute, last_run_date, *, catch_up=True, stale=False):
    """Should a sync run right now?

    `last_run_date` is the LOCAL DATE a sync last ran for, or None. Keying on the date is
    what makes this at-most-once-per-day across a restart, a clock change and a DST shift —
    all three of which break an elapsed-time check.

    `stale` is the data's own verdict: the newest sale on file is older than it should be.
    It can bring a sync FORWARD (catch up now rather than waiting for tonight's slot) but
    it can never override the once-a-day cap — a shop that sold nothing yesterday has
    legitimately old data, and re-pulling every thirty seconds would not change that.
    """
    today = now.date()
    if last_run_date == today:
        return False
    scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= scheduled_today:
        return True
    # Before today's slot. Two ways to earn an early run: the data says it is stale, or a
    # machine that was off at yesterday's slot has fallen a whole day behind.
    if catch_up and stale:
        return True
    if catch_up and last_run_date is not None and last_run_date < today - _dt.timedelta(days=1):
        return True
    # First ever start, data not stale: don't fire instantly on boot. Someone launching the
    # app wants it responsive, not mid-sync, and there is a button.
    return False


class DayLedger:
    """What has finished, for which calendar day, stage by stage.

    THE PROBLEM THIS SOLVES

    A single "ran today" flag cannot tell three different situations apart:

        the day's work finished                  → do nothing
        the store was unreachable                → back off, try later
        the calculation was killed halfway       → resume NOW

    The first version of this stamped the date in a `finally`, so a job that raised — or a
    refit interrupted by someone quitting the app — marked the day done and nothing
    retried until tomorrow. That is the exact failure worth designing against, because it
    is invisible: the tool looks current and simply is not.

    So each STAGE is recorded separately and ONLY ON SUCCESS. A stage that never wrote its
    marker is indistinguishable from one that never started, which is precisely the
    behaviour you want from an interrupted run.

    WHY FETCH IS SPECIAL

    Re-running a refit costs CPU. Re-running a fetch costs an API call against someone
    else's rate limit, and returns the same data. So a resume skips `fetch` if it already
    succeeded today and re-runs only the calculation stages.
    """

    RETAIN_DAYS = 14
    RETRY_AFTER_SECONDS = 3600.0     # a failed FETCH waits an hour, not 30 seconds

    def __init__(self, path=None, clock=None):
        self.path = path if path is not None else STATE_PATH
        self.clock = clock or _now_local
        self._lock = threading.RLock()
        self._days = {}
        self.last_run = None          # the most recent run record, for the status endpoint
        self.load()

    # -- persistence --
    def load(self):
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                days = raw.get("days")
                self._days = dict(days) if isinstance(days, dict) else {}
                self.last_run = raw.get("lastRun")
                # Migrate the old flat shape, so an upgrade doesn't re-sync on first boot.
                legacy = raw.get("lastRunDate")
                if legacy and legacy not in self._days:
                    self._days[legacy] = {"stages": {}, "complete": True, "migrated": True}
            except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError, OSError):
                self._days, self.last_run = {}, None
        return self

    def save(self):
        with self._lock:
            self._prune()
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"days": self._days, "lastRun": self.last_run}, fh, indent=0)
                os.replace(tmp, self.path)
            except OSError:
                pass                  # losing this costs one redundant run, never a boot
        return self

    def _prune(self):
        if len(self._days) <= self.RETAIN_DAYS:
            return
        for key in sorted(self._days)[:-self.RETAIN_DAYS]:
            self._days.pop(key, None)

    # -- reads --
    def day(self, day) -> dict:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        with self._lock:
            return dict(self._days.get(key) or {"stages": {}, "complete": False})

    def is_complete(self, day) -> bool:
        return bool(self.day(day).get("complete"))

    def stage_done(self, day, stage) -> bool:
        return bool((self.day(day).get("stages") or {}).get(stage, {}).get("ok"))

    def pending_stages(self, day, stages) -> list:
        return [s for s in stages if not self.stage_done(day, s)]

    def attempts(self, day) -> int:
        return int(self.day(day).get("attempts") or 0)

    def seconds_since_attempt(self, day, scheduled_only=False):
        entry = self.day(day)
        ts = entry.get("lastScheduledAttemptAt") if scheduled_only else entry.get("lastAttemptAt")
        if not ts:
            return None
        try:
            return (self.clock() - _dt.datetime.fromisoformat(ts)).total_seconds()
        except (ValueError, TypeError):
            return None

    def cooling_off(self, day) -> bool:
        """True when a recent attempt failed and it is too soon to try again.

        Without this a store that is down turns the 30-second poll into 2,880 API calls a
        day. With it, an outage costs one attempt an hour.
        """
        # SCHEDULED attempts only. The cooldown exists so the 30-second poll can't turn an
        # outage into thousands of calls; a person pressing "sync now" is not the poll,
        # and a manual attempt that failed used to push the NIGHTLY run back by an hour —
        # clicking the button at 00:10 on a bad connection cost you the 00:15 slot.
        if self.is_complete(day):
            return False
        since = self.seconds_since_attempt(day, scheduled_only=True)
        return since is not None and since < self.RETRY_AFTER_SECONDS

    # -- writes --
    def _touch(self, key) -> dict:
        return self._days.setdefault(key, {"stages": {}, "complete": False})

    def note_attempt(self, day, scheduled=True):
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        with self._lock:
            entry = self._touch(key)
            now = self.clock().isoformat()
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            entry["lastAttemptAt"] = now
            if scheduled:
                entry["lastScheduledAttemptAt"] = now
        return self.save()

    def mark_stage(self, day, stage, ok=True, note=None):
        """Record a stage. `ok=False` is written too — it is the difference between
        'this failed and we know why' and 'nobody ever got here'."""
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        with self._lock:
            entry = self._touch(key)
            rec = {"ok": bool(ok), "at": self.clock().isoformat()}
            if note:
                rec["note"] = note
            entry.setdefault("stages", {})[stage] = rec
        return self.save()

    def mark_complete(self, day, stages=()):
        """Only ever called after every stage has actually finished."""
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        with self._lock:
            entry = self._touch(key)
            entry["complete"] = all(
                (entry.get("stages") or {}).get(s, {}).get("ok") for s in stages) if stages else True
            entry["completedAt"] = self.clock().isoformat()
        self.save()
        return self.is_complete(day)

    def record_run(self, record):
        with self._lock:
            self.last_run = record
        return self.save()


class DailySync:
    """A daemon thread that calls `job` once per local day.

    `job` takes no arguments and returns a dict describing what happened; whatever it
    returns is recorded verbatim as `lastRun.result` for the status endpoint.
    """

    def __init__(self, job, *, at=None, enabled=None, catch_up=True, clock=None,
                 poll_seconds=_POLL_SECONDS, is_stale=None, state_path=None,
                 ledger=None, stages=None):
        self.job = job
        # Returns True when the newest data on file is older than it ought to be. Optional:
        # without it the scheduler falls back to the clock alone.
        self.is_stale = is_stale
        self.state_path = state_path if state_path is not None else STATE_PATH
        self._at = at or os.environ.get("LOGITRACK_SYNC_AT") or DEFAULT_AT
        env_enabled = os.environ.get("LOGITRACK_SYNC_ENABLED")
        self.enabled = (enabled if enabled is not None
                        else (env_enabled or "1").strip().lower() not in ("0", "false", "no"))
        self.catch_up = catch_up
        self.clock = clock or _now_local
        self.poll_seconds = poll_seconds
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._running = False
        self.ledger = ledger or DayLedger(path=self.state_path, clock=self.clock)
        self.stages = tuple(stages or ("fetch", "refit", "backtest"))

    # -- what the ledger says --
    @property
    def last_run(self):
        return self.ledger.last_run

    @property
    def last_run_date(self):
        """The most recent COMPLETED day, or None. Read from the ledger rather than kept
        alongside it, so the two can never disagree."""
        with self.ledger._lock:
            done = [d for d, v in self.ledger._days.items() if v.get("complete")]
        if not done:
            return None
        try:
            return _dt.date.fromisoformat(max(done))
        except ValueError:
            return None

    # -- config --
    @property
    def at(self):
        return self._at

    def configure(self, *, at=None, enabled=None):
        with self._lock:
            if at is not None:
                h, m = parse_at(at)
                self._at = f"{h:02d}:{m:02d}"
            if enabled is not None:
                self.enabled = bool(enabled)
        self._wake.set()              # re-evaluate immediately rather than after a poll
        return self.status()

    def hour_minute(self):
        return parse_at(self._at)

    # -- status --
    def next_run_at(self):
        h, m = self.hour_minute()
        return next_run_after(self.clock(), h, m)

    def status(self) -> dict:
        _today_date = self.clock().date()
        _today = _today_date.isoformat()
        with self._lock:
            return {
                "enabled": bool(self.enabled),
                "at": self._at,
                "timezone": (self.zone_name() if callable(getattr(self, "zone_name", None))
                             else str(_dt.datetime.now().astimezone().tzinfo)),
                "nextRunAt": self.next_run_at().isoformat() if self.enabled else None,
                "lastRunDate": self.last_run_date.isoformat() if self.last_run_date else None,
                "lastRun": self.last_run,
                "running": self._running,
                "dataStale": self._stale(),
                "stages": list(self.stages),
                "today": _today,
                "todayComplete": self.ledger.is_complete(_today_date),
                "pendingStages": self.ledger.pending_stages(_today_date, self.stages),
                "coolingOff": self.ledger.cooling_off(_today_date),
            }

    def _stale(self) -> bool:
        """The data's own verdict, never allowed to raise into the scheduler."""
        if not self.is_stale:
            return False
        try:
            return bool(self.is_stale())
        except Exception:                                 # noqa: BLE001
            return False

    # -- the run --
    def run_now(self, trigger="manual") -> dict:
        """Run whatever is still outstanding for today. Never raises.

        Shared by the thread, the manual button and an external cron hitting the endpoint,
        so all three resume the same way and share the same bookkeeping.

        The job is handed `(ledger, day)` when it accepts them, so it can skip the stages
        already marked done and re-run only what is missing. Nothing is marked complete
        here — only the job knows when a stage actually finished, and a marker written on
        the way in is the bug this whole class exists to avoid.
        """
        with self._lock:
            if self._running:
                return {"ok": False, "skipped": "already-running", "lastRun": self.last_run}
            self._running = True
        started = self.clock()
        day = started.date()
        pending = self.ledger.pending_stages(day, self.stages)
        record = {"startedAt": started.isoformat(), "trigger": trigger,
                  "day": day.isoformat(), "resumed": bool(self.ledger.attempts(day)),
                  "pendingStages": pending}
        self.ledger.note_attempt(day, scheduled=(trigger == "schedule"))
        try:
            try:
                result = self.job(self.ledger, day) or {}
            except TypeError:
                result = self.job() or {}                 # a job that wants no context
            record.update({"ok": bool(result.get("ok", True)), "result": result})
        except Exception as exc:                          # noqa: BLE001 — see module docstring
            record.update({"ok": False, "error": str(exc)})
        finally:
            record["finishedAt"] = self.clock().isoformat()
            record["complete"] = self.ledger.is_complete(day)
            with self._lock:
                self._running = False
            self.ledger.record_run(record)
        return record

    # -- lifecycle --
    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, name="daily-sync", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._wake.set()
        self.enabled = False
        return self

    def tick(self, now=None):
        """One evaluation of the schedule: run if due, otherwise do nothing. Returns the
        run record, or None. The thread calls this every poll; tests call it directly with
        a fake clock, which is the only way to prove what happens at 00:15 without waiting
        for 00:15."""
        if not self.enabled:
            return None
        h, m = self.hour_minute()
        now = now or self.clock()
        day = now.date()
        # Three questions, in cheapest-first order: has today already finished, are we
        # backing off from a failure, and does the clock (or the data) say it is time. An
        # incomplete day is the resume path — it is exactly what makes a killed
        # calculation pick itself back up.
        if (not self.ledger.is_complete(day)
                and not self.ledger.cooling_off(day)
                and is_due(now, h, m, self.last_run_date,
                           catch_up=self.catch_up, stale=self._stale())):
            return self.run_now(trigger="schedule")
        return None

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception as exc:                      # noqa: BLE001
                print(f"Daily sync loop error ({exc}).")
            if self._wake.wait(self.poll_seconds):
                self._wake.clear()                        # config changed; re-evaluate now


# ─────────────────────────────────────────────────────────────────────────────────────
#  MOMENT READINGS
# ─────────────────────────────────────────────────────────────────────────────────────
SAMPLE_SECONDS = 3600.0        # hourly
_MAX_BACKOFF_MULTIPLIER = 6    # a dead store is retried at most every 6 intervals
_SAMPLE_POLL_SECONDS = 30.0    # how often the thread checks whether a reading is owed


def _same_clock(t, like):
    """Express `t` in the same kind of datetime as `like`, so they can be compared.

    The log records aware UTC timestamps; the scheduler's clock is naive local time.
    Comparing the two raises, and "fixing" that by stripping the tzinfo would silently
    shift every reading by the UTC offset — four or five hours in the eastern US.
    """
    if t is None or like is None:
        return t
    if t.tzinfo is not None and like.tzinfo is None:
        return t.astimezone().replace(tzinfo=None)
    if t.tzinfo is None and like.tzinfo is not None:
        return t.replace(tzinfo=like.tzinfo)
    return t


class IntervalSampler:
    """A daemon thread that takes a MOMENT READING on a fixed interval.

    WHY THIS IS NOT DailySync. DailySync catches up: a missed nightly run is simply run
    late, because what it does — fetch, refit, backtest — computes a STATE, and one run
    against current data produces the same answer as the seven that were missed.

    A stock reading is the opposite kind of thing. It observes a moment. Nobody records
    how much stock you had at 2pm last Tuesday, so an hour nobody sampled is gone for
    good — and no amount of running late recovers it. Catching up is therefore not just
    unnecessary here, it is meaningless, and pretending otherwise would be worse than
    doing nothing: it would file a reading taken now under an hour it does not describe.

    WHAT THE READINGS ARE FOR. Sales are daily and stay daily. These readings exist only
    to answer one question per product per day: how many hours was it actually buyable?
    That number is what lets the engine tell "sold 3 because demand was 3" apart from
    "sold 3 because it ran out at 11am" — and without it the second case silently teaches
    the forecast that demand is falling, which orders less, which sells out sooner.

    At one reading a day that question has only two possible answers, ~24h or ~0h, so the
    partial-day case can never be detected and the correction never fires. Hourly is the
    coarsest cadence that makes it real. StockLog collapses runs of identical readings,
    so a product sitting still all week costs two samples, not 168.

    ON FAILURE. A store hiccup must never escape this thread. Failures are counted and
    back the interval off linearly, to a ceiling, so an expired token doesn't mean 24
    doomed API calls a day — but the sampler keeps trying, because the cost of staying
    quiet is unrecoverable and the cost of one wasted call an hour is nothing.
    """

    def __init__(self, job, *, interval_seconds=SAMPLE_SECONDS, enabled=None, clock=None,
                 last_sample_at=None, name="stock-sampler"):
        self.job = job
        # Returns when a reading was last filed (a datetime, or None). Optional, and
        # consulted only at boot: a process that restarts every few minutes must not take
        # a reading every time, and the log itself already knows when it was last written.
        self.last_sample_at = last_sample_at
        self.interval_seconds = float(interval_seconds)
        env_enabled = os.environ.get("LOGITRACK_SAMPLE_ENABLED")
        self.enabled = (enabled if enabled is not None
                        else (env_enabled or "1").strip().lower() not in ("0", "false", "no"))
        self.clock = clock or _now_local
        self.name = name
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._running = False
        self._last = None             # the last attempt's record, whatever happened
        self._failures = 0
        self._taken = 0
        # WHEN THE NEXT SCHEDULED READING IS OWED. The schedule is this one timestamp and
        # nothing else. It used to be implicit in the thread's sleep — "wake up an hour
        # after the last thing I did" — which let a manual reading, a settings change or a
        # restart silently shift, swallow or delay the scheduled ones. Now only a scheduled
        # reading advances it, so pressing "read now" can never cost you the next hour.
        self._next_at = None
        self.poll_seconds = _SAMPLE_POLL_SECONDS

    # -- config --
    def configure(self, *, enabled=None, interval_seconds=None):
        with self._lock:
            if enabled is not None:
                self.enabled = bool(enabled)
            if interval_seconds is not None:
                self.interval_seconds = max(60.0, float(interval_seconds))
                # A shorter interval should take effect now, not after the old one runs
                # out. It does NOT take a reading: changing a setting is not a reason to
                # sample, and it used to be one.
                if self._next_at is not None:
                    soonest = self.clock() + _dt.timedelta(seconds=self._current_interval())
                    self._next_at = min(self._next_at, soonest)
        self._wake.set()
        return self.status()

    # -- status --
    def _last_filed(self):
        if not self.last_sample_at:
            return None
        try:
            return self.last_sample_at()
        except Exception:                                 # noqa: BLE001
            return None

    def _current_interval(self) -> float:
        """Back off while the store is refusing us, but never give up on it."""
        mult = min(self._failures, _MAX_BACKOFF_MULTIPLIER) or 1
        return self.interval_seconds * mult

    def status(self) -> dict:
        with self._lock:
            filed = self._last_filed()
            return {
                "enabled": bool(self.enabled),
                "intervalSeconds": self.interval_seconds,
                "effectiveIntervalSeconds": self._current_interval(),
                "running": self._running,
                "samplesTaken": self._taken,
                "consecutiveFailures": self._failures,
                "lastAttempt": self._last,
                "lastFiledAt": filed.isoformat() if filed else None,
                "nextScheduledAt": self._next_at.isoformat() if self._next_at else None,
            }

    # -- the run --
    def sample_now(self, trigger="manual") -> dict:
        """Take one reading. Never raises, and never blocks a second caller."""
        with self._lock:
            if self._running:
                return {"ok": False, "skipped": "already-running", "lastAttempt": self._last}
            self._running = True
        started = self.clock()
        record = {"startedAt": started.isoformat(), "trigger": trigger}
        failures_before = self._failures
        try:
            result = self.job() or {}
            ok = bool(result.get("ok", True))
            # A SKIP is neither. "There is no store connected yet" is not a malfunction,
            # and counting it as one lit a red "readings are failing" warning on a fresh
            # install where nothing was wrong — which trains the one light that matters to
            # be ignored. It does not back the interval off either: there is nothing to
            # back off from, and the moment a store IS connected we want the next reading.
            skipped = bool(result.get("skip"))
            record.update({"ok": ok, "skipped": skipped, "result": result})
            with self._lock:
                if skipped:
                    pass
                elif ok:
                    self._failures = 0
                    self._taken += 1
                else:
                    self._failures += 1
        except Exception as exc:                          # noqa: BLE001
            record.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            with self._lock:
                self._failures += 1
        finally:
            record["finishedAt"] = self.clock().isoformat()
            with self._lock:
                self._running = False
                self._last = record
                # A manual reading never moves the schedule — with one exception that
                # only helps. If the schedule had backed off because the store kept
                # failing, and a manual reading now SUCCEEDS, the outage is evidently over;
                # waiting out the rest of a multi-hour backoff would just lose readings.
                # So the next scheduled reading is pulled in to one normal interval away.
                if (trigger != "schedule" and record.get("ok") and not record.get("skipped")
                        and failures_before > 0 and self._next_at is not None):
                    soonest = self.clock() + _dt.timedelta(seconds=self.interval_seconds)
                    self._next_at = min(self._next_at, soonest)
        return record

    def due(self, now=None) -> bool:
        """Is a reading owed right now? Asked at boot, against the LOG rather than against
        a timer this process has no memory of."""
        if not self.enabled:
            return False
        filed = self._last_filed()
        if filed is None:
            return True                                   # nothing on file: start the record
        now = now or self.clock()
        try:
            if filed.tzinfo is None and now.tzinfo is not None:
                filed = filed.replace(tzinfo=now.tzinfo)
            elif filed.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=filed.tzinfo)
            return (now - filed).total_seconds() >= self._current_interval()
        except (TypeError, ValueError):
            return True

    # -- lifecycle --
    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._wake.set()
        self.enabled = False
        return self

    def tick(self, now=None):
        """One evaluation of the schedule. Returns the record of a reading it took, or None.

        The thread calls this every poll; tests call it with a fake clock.

        FIRST CALL. The next reading is placed one interval after the last one FILED — the
        log's own record — not one interval after boot. A restart fifty minutes after a
        reading used to wait another full hour, leaving a 110-minute hole; now it resumes
        the cadence it interrupted, and a reading already overdue is taken at once.

        A COLLISION with a manual reading in flight does not cost the hour. The scheduled
        reading is simply retried on the next poll, thirty seconds later, rather than
        being dropped until the following hour as it used to be.

        FALLING BEHIND — a laptop asleep for six hours — does not trigger a burst of
        catch-up readings. A stock reading describes the moment it is taken; six readings
        taken now would all describe now. The schedule just restarts from the present.
        """
        if not self.enabled:
            return None
        now = now or self.clock()
        with self._lock:
            if self._next_at is None:
                filed = _same_clock(self._last_filed(), now)
                self._next_at = (now if filed is None
                                 else filed + _dt.timedelta(seconds=self._current_interval()))
            if now < self._next_at:
                return None
            first = self._taken == 0 and self._last is None
        rec = self.sample_now(trigger="boot" if first else "schedule")
        if rec.get("skipped") == "already-running":
            return None                      # a manual reading is in flight: retry next poll
        with self._lock:
            nxt = self._next_at + _dt.timedelta(seconds=self._current_interval())
            if nxt <= now:                   # fell behind: restart from now, no burst
                nxt = now + _dt.timedelta(seconds=self._current_interval())
            self._next_at = nxt
        return rec

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception as exc:                      # noqa: BLE001
                print(f"Stock sampler loop error ({exc}).")
            if self._wake.wait(self.poll_seconds):
                self._wake.clear()                        # config changed; re-evaluate now

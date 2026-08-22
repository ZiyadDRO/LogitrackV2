"""
Forecast log — what we predicted, when, and what actually happened.

The backtest answers "how would this have done on history we already have". It can only
ever re-simulate. This module answers the different question: how is the tool doing NOW,
against sales that hadn't happened yet when the forecast was made.

Three rules make the answer trustworthy, and each is enforced here rather than by
convention:

  1. A prediction is written once and never recomputed. If you re-derive what you
     "would have said" using data that has since arrived, you are grading a forecast
     that had information the real one didn't. `snapshot()` refuses to overwrite.

  2. Nothing is scored before its due date. An entry made on the 3rd for a 14-day
     horizon cannot be graded until the 17th, no matter how often you press the button.
     `score_due()` filters on the clock, and `score_entry()` raises if called early.

  3. Origins are period-aligned. Snapshots land on a fixed weekday rather than "whenever
     someone opened the app", so the log has an even spine and two users of the same
     store produce the same origins.

The clock is injected everywhere (`now` parameter) because time-dependent logic that
can't be fast-forwarded can't be tested — you'd be waiting two weeks to find a bug.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import threading
from typing import Iterable

# ── clock ────────────────────────────────────────────────────────────────────────────
# Everything reads the clock through here. Tests swap it for a controllable one.


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def today(now=None) -> _dt.date:
    return (now or utcnow()).date()


def _d(x) -> _dt.date:
    if isinstance(x, _dt.date) and not isinstance(x, _dt.datetime):
        return x
    if isinstance(x, _dt.datetime):
        return x.date()
    return _dt.date.fromisoformat(str(x)[:10])


def _iso(x) -> str:
    return _d(x).isoformat()


# ── period alignment ─────────────────────────────────────────────────────────────────

WEEKLY = "weekly"
MONTHLY = "monthly"
# Weeks run Sunday → Saturday, so a window always closes at the end of a Saturday night.
# That matches how a trading week is actually counted, and it means a 14-day window is
# exactly two whole weeks rather than a fortnight that ends mid-week.
ANCHOR_WEEKDAY = 6          # Sunday (Python: Mon=0 … Sun=6). Fixed so origins never drift.


def period_origin(now=None, cadence: str = WEEKLY) -> _dt.date:
    """The origin date for the period containing `now`.

    Weekly snaps back to the most recent Sunday; monthly to the 1st. Two people opening
    the app on different days of the same week get the same origin, so the log gets one
    entry per period per product instead of one per visit.
    """
    d = today(now)
    if cadence == MONTHLY:
        return d.replace(day=1)
    return d - _dt.timedelta(days=(d.weekday() - ANCHOR_WEEKDAY) % 7)


def next_origin(now=None, cadence: str = WEEKLY) -> _dt.date:
    cur = period_origin(now, cadence)
    if cadence == MONTHLY:
        return (cur.replace(day=28) + _dt.timedelta(days=7)).replace(day=1)
    return cur + _dt.timedelta(days=7)


# ── entries ──────────────────────────────────────────────────────────────────────────


def entry_id(sku: str, origin, horizon_days: int) -> str:
    """Deterministic, so re-snapshotting the same period is a no-op rather than a
    duplicate. This is what makes rule 1 enforceable."""
    return f"{sku}|{_iso(origin)}|{int(horizon_days)}"


def make_entry(sku: str, origin, horizon_days: int, predicted: float,
               lo=None, hi=None, band: int = 80, *, model=None,
               lead_days=None, coverage_days=None, sku_name=None, now=None) -> dict:
    origin_d = _d(origin)
    h = int(horizon_days)
    return {
        "week": origin_d.isoformat(),
        "month": month_of_week(origin_d),
        "id": entry_id(sku, origin_d, h),
        "sku": str(sku),
        "skuName": sku_name or str(sku),
        "origin": origin_d.isoformat(),
        # The window is h whole days STARTING at the origin: [origin, origin+h-1].
        # `dueAt` is the LAST DAY COUNTED, not the day it can be graded — a window that
        # includes Saturday isn't finished until Saturday night is over, so grading
        # happens from `scorableFrom` (the next day) onward. Getting this wrong would
        # score a 14-day forecast against 13 days of sales and call the model pessimistic.
        "windowStart": origin_d.isoformat(),
        "dueAt": (origin_d + _dt.timedelta(days=h - 1)).isoformat(),
        "scorableFrom": (origin_d + _dt.timedelta(days=h)).isoformat(),
        "horizonDays": h,
        "predicted": round(float(predicted), 2),
        "lo": None if lo is None else round(float(lo), 2),
        "hi": None if hi is None else round(float(hi), 2),
        "band": int(band),
        "model": model,
        "leadDays": lead_days,
        "coverageDays": coverage_days,
        "createdAt": (now or utcnow()).isoformat(),
        # Filled in only once the window has actually elapsed.
        "actual": None, "scoredAt": None, "absErr": None, "pctErr": None,
        "inBand": None, "signedErr": None,
    }


def is_due(entry: dict, now=None) -> bool:
    """True only once the whole window is behind us.

    Strictly after the last counted day: an entry whose window ends Saturday becomes
    gradable on Sunday, because Saturday's sales are still arriving until 23:59:59.
    """
    return today(now) > _d(entry["dueAt"])


def scorable_from(entry: dict) -> _dt.date:
    v = entry.get("scorableFrom")
    return _d(v) if v else (_d(entry["dueAt"]) + _dt.timedelta(days=1))


def days_until_due(entry: dict, now=None) -> int:
    """Days until it can be GRADED, which is the day after the window's last day."""
    return (scorable_from(entry) - today(now)).days


def is_scored(entry: dict) -> bool:
    return entry.get("scoredAt") is not None


def week_of(entry) -> str:
    """The Sunday that starts this entry's week — the tile it belongs to."""
    return _iso(entry.get("origin"))


def month_of_week(origin) -> str:
    """Which month a week belongs to, as YYYY-MM.

    Weeks don't divide into months, so a rule is needed rather than a guess. A week is
    filed under the month holding MOST of its days — equivalently the month of its
    midpoint (Wednesday, for a Sunday start). That keeps every week in exactly one place,
    always the month it mostly happened in, and never leaves a month empty because its
    first week began in the previous one.
    """
    mid = _d(origin) + _dt.timedelta(days=3)
    return f"{mid.year:04d}-{mid.month:02d}"


class AmendedError(Exception):
    """Raised when an amendment would rewrite history rather than record a revision."""


class NotDueError(Exception):
    """Raised when something tries to grade a forecast whose window hasn't closed."""


def amend(entry: dict, new_predicted: float, reason: str, *, now=None,
          lo=None, hi=None) -> dict:
    """Revise a sealed forecast, keeping what it originally said.

    Needed for one honest case: you declare a promotion partway through a week that has
    already been sealed. The original number was a forecast of a week without a
    campaign in it; grading it against a week WITH one measures the campaign, not the
    model.

    So it is recorded as a revision, never a rewrite — `originalPredicted` survives, the
    reason is stored, and the entry is permanently marked `amended`. You can always see
    both numbers and why they differ. An already-graded entry cannot be touched at all:
    that would be choosing the answer after seeing the result.
    """
    if is_scored(entry):
        raise AmendedError(f"{entry['id']} is already graded; a revision now would be "
                           f"picking the answer after seeing the result")
    out = dict(entry)
    if "originalPredicted" not in out or out.get("originalPredicted") is None:
        out["originalPredicted"] = entry.get("predicted")
        out["originalLo"], out["originalHi"] = entry.get("lo"), entry.get("hi")
    out["predicted"] = round(float(new_predicted), 2)
    if lo is not None:
        out["lo"] = round(float(lo), 2)
    if hi is not None:
        out["hi"] = round(float(hi), 2)
    out["amended"] = True
    out["amendedAt"] = (now or utcnow()).isoformat()
    out["amendReason"] = str(reason or "").strip() or "revised"
    return out


def score_entry(entry: dict, actual_units: float, *, now=None) -> dict:
    """Grade one entry. Refuses if the window is still open — scoring a partial window
    would compare a 14-day forecast against 3 days of sales and call the model terrible.
    """
    if not is_due(entry, now):
        raise NotDueError(
            f"{entry['id']} counts sales through {entry['dueAt']} and cannot be graded "
            f"until {scorable_from(entry).isoformat()} "
            f"({days_until_due(entry, now)} days away)")
    a = float(actual_units)
    p = float(entry["predicted"])
    out = dict(entry)
    out["actual"] = round(a, 2)
    out["signedErr"] = round(p - a, 2)
    out["absErr"] = round(abs(p - a), 2)
    out["pctErr"] = None if a == 0 else round(abs(p - a) / a * 100.0, 1)
    lo, hi = entry.get("lo"), entry.get("hi")
    out["inBand"] = None if lo is None or hi is None else bool(lo <= a <= hi)
    out["scoredAt"] = (now or utcnow()).isoformat()
    return out


# ── metrics ──────────────────────────────────────────────────────────────────────────


def calibration(entries: Iterable[dict]) -> dict:
    """Did the prediction intervals mean what they claimed?

    This is the metric protection levels rest on. An "80% band" that only contains the
    truth 55% of the time means the buffer sized from it is too small, and every service
    level in the tool is optimistic. Backtesting can suggest this; only live data proves
    it, because live intervals were built without seeing what came next.
    """
    scored = [e for e in entries if is_scored(e) and e.get("inBand") is not None]
    if not scored:
        return {"n": 0, "hitRate": None, "nominal": None, "gap": None, "verdict": "no-data"}
    hits = sum(1 for e in scored if e["inBand"])
    n = len(scored)
    rate = hits / n * 100.0
    nominal = sum(e["band"] for e in scored) / n
    gap = rate - nominal
    # Binomial standard error — with 12 entries even a perfect model swings ±11 points,
    # so don't let anyone read a verdict off noise.
    se = math.sqrt(max(nominal, 1e-9) * (100 - nominal) / n)
    if n < 10:
        verdict = "too-few"
    elif abs(gap) <= 2 * se:
        verdict = "calibrated"
    elif gap < 0:
        verdict = "overconfident"      # bands too narrow → buffers too small
    else:
        verdict = "underconfident"     # bands too wide  → holding more than needed
    return {"n": n, "hits": hits, "hitRate": round(rate, 1),
            "nominal": round(nominal, 1), "gap": round(gap, 1),
            "se": round(se, 1), "verdict": verdict}


def accuracy(entries: Iterable[dict]) -> dict:
    """Volume-weighted error over scored entries. Weighted, not averaged, so a product
    selling 4 units a month can't dominate the headline number."""
    scored = [e for e in entries if is_scored(e)]
    if not scored:
        return {"n": 0, "wape": None, "bias": None, "meanAbsErr": None}
    tot_act = sum(float(e["actual"]) for e in scored)
    tot_abs = sum(float(e["absErr"]) for e in scored)
    tot_sig = sum(float(e["signedErr"]) for e in scored)
    return {
        "n": len(scored),
        "wape": None if tot_act <= 0 else round(tot_abs / tot_act * 100.0, 1),
        # Positive bias = we forecast more than sold = you carry stock you didn't need.
        "bias": None if tot_act <= 0 else round(tot_sig / tot_act * 100.0, 1),
        "meanAbsErr": round(tot_abs / len(scored), 2),
        "units": round(tot_act, 1),
    }


def per_sku(entries: Iterable[dict]) -> list:
    by = {}
    for e in entries:
        by.setdefault(e["sku"], []).append(e)
    rows = []
    for sku, es in by.items():
        acc = accuracy(es)
        cal = calibration(es)
        scored = [x for x in es if is_scored(x)]
        rows.append({
            "sku": sku,
            "skuName": es[0].get("skuName") or sku,
            "entries": len(es),
            "scored": len(scored),
            "pending": len(es) - len(scored),
            "wape": acc["wape"], "bias": acc["bias"],
            "hitRate": cal["hitRate"], "calN": cal["n"],
            "lastScored": max((x["scoredAt"] for x in scored), default=None),
        })
    rows.sort(key=lambda r: (r["wape"] is None, -(r["wape"] or 0)))
    return rows


def by_week(entries: list, now=None) -> list:
    """One row per week — the tile. Newest first, grouped under its month."""
    weeks = {}
    for e in entries or []:
        k = week_of(e)
        w = weeks.setdefault(k, {"week": k, "month": e.get("month") or month_of_week(k),
                                 "entries": []})
        w["entries"].append(e)
    out = []
    for k in sorted(weeks, reverse=True):
        w = weeks[k]
        es = w["entries"]
        scored = [x for x in es if is_scored(x)]
        acc = accuracy(es)
        cal = calibration(es)
        start = _d(k)
        h = max((int(x.get("horizonDays") or 7) for x in es), default=7)
        out.append({
            "week": k,
            "month": w["month"],
            "endsOn": (start + _dt.timedelta(days=h - 1)).isoformat(),
            "gradableFrom": (start + _dt.timedelta(days=h)).isoformat(),
            "products": len(es),
            "scored": len(scored),
            "pending": len(es) - len(scored),
            "due": sum(1 for x in es if not is_scored(x) and is_due(x, now)),
            "amended": sum(1 for x in es if x.get("amended")),
            "wape": acc["wape"], "bias": acc["bias"],
            "hitRate": cal["hitRate"], "band": (es[0].get("band") if es else 80),
            "inBand": sum(1 for x in scored if x.get("inBand")),
            "predicted": round(sum(float(x.get("predicted") or 0) for x in es), 1),
            "actual": round(sum(float(x.get("actual") or 0) for x in scored), 1) if scored else None,
            "status": ("scored" if scored and len(scored) == len(es)
                       else "partial" if scored
                       else "due" if any(is_due(x, now) for x in es)
                       else "open"),
        })
    return out


def summarise(entries: list, now=None) -> dict:
    pending = [e for e in entries if not is_scored(e)]
    due_now = [e for e in pending if is_due(e, now)]
    waiting = [e for e in pending if not is_due(e, now)]
    nxt = min((scorable_from(e) for e in waiting), default=None)
    return {
        "total": len(entries),
        "scored": sum(1 for e in entries if is_scored(e)),
        "dueNow": len(due_now),
        "waiting": len(waiting),
        "nextDueAt": nxt.isoformat() if nxt else None,
        "nextDueInDays": (nxt - today(now)).days if nxt else None,
        "calibration": calibration(entries),
        "accuracy": accuracy(entries),
        "firstOrigin": min((e["origin"] for e in entries), default=None),
        "lastOrigin": max((e["origin"] for e in entries), default=None),
    }


# ── store ────────────────────────────────────────────────────────────────────────────


class ForecastLog:
    """Append-only store, persisted to JSON.

    This is the one thing in the tool that MUST survive a restart. Everything else can be
    rebuilt from the source data; a forecast can't — the moment you lose what you said on
    the 3rd, there is no way to recover it, and the only way to get it back is to wait
    another two weeks.
    """

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get(
            "LOGITRACK_FORECAST_LOG",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "forecast_log.json"))
        self._lock = threading.RLock()
        self._entries: dict[str, dict] = {}
        self.load()

    # -- persistence --
    def load(self):
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                items = raw.get("entries", raw) if isinstance(raw, dict) else raw
                self._entries = {e["id"]: e for e in items if isinstance(e, dict) and e.get("id")}
            except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
                self._entries = {}
        return self

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            payload = {"version": 1, "entries": list(self._entries.values())}
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
            os.replace(tmp, self.path)      # atomic; a crash mid-write can't truncate the log
        return self

    # -- reads --
    def all(self) -> list:
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: (e["origin"], e["sku"]))

    def for_sku(self, sku: str) -> list:
        return [e for e in self.all() if e["sku"] == str(sku)]

    def pending(self, now=None) -> list:
        return [e for e in self.all() if not is_scored(e)]

    def due(self, now=None) -> list:
        """Entries whose window has closed and which haven't been graded yet."""
        return [e for e in self.all() if not is_scored(e) and is_due(e, now)]

    def waiting(self, now=None) -> list:
        return [e for e in self.all() if not is_scored(e) and not is_due(e, now)]

    # -- writes --
    def snapshot(self, rows: list, *, cadence: str = WEEKLY, now=None,
                 origin=None) -> dict:
        """Record what we predict right now, for this period.

        `rows`: [{sku, predicted, lo, hi, band, horizonDays, model, ...}].

        Re-running in the same period is deliberately a no-op. That's not an
        optimisation — it's what stops a prediction being quietly refreshed with newer
        information and then graded as though it were made on Monday.
        """
        org = _d(origin) if origin else period_origin(now, cadence)
        added, skipped = [], []
        with self._lock:
            for r in rows:
                sku = r.get("sku")
                h = int(r.get("horizonDays") or 0)
                if not sku or h <= 0 or r.get("predicted") is None:
                    continue
                eid = entry_id(sku, org, h)
                if eid in self._entries:
                    skipped.append(eid)         # already recorded for this period
                    continue
                self._entries[eid] = make_entry(
                    sku, org, h, r["predicted"],
                    lo=r.get("lo"), hi=r.get("hi"), band=int(r.get("band") or 80),
                    model=r.get("model"), lead_days=r.get("leadDays"),
                    coverage_days=r.get("coverageDays"), sku_name=r.get("skuName"),
                    now=now)
                added.append(eid)
            if added:
                self.save()
        return {"origin": org.isoformat(), "added": len(added), "skipped": len(skipped),
                "addedIds": added}

    def score_due(self, actuals_fn, *, now=None) -> dict:
        """Grade every entry whose window has closed.

        `actuals_fn(sku, window_start, window_end) -> float | None` supplies real units
        sold. Returning None means "couldn't determine" and leaves the entry pending
        rather than recording a zero, because a missing read and a genuine zero are very
        different things and conflating them would make the tool look accurate for
        products that simply stopped reporting.
        """
        graded, unavailable = [], []
        with self._lock:
            for e in self.due(now):
                try:
                    a = actuals_fn(e["sku"], e["windowStart"], e["dueAt"])
                except Exception:
                    a = None
                if a is None:
                    unavailable.append(e["id"])
                    continue
                self._entries[e["id"]] = score_entry(e, a, now=now)
                graded.append(e["id"])
            if graded:
                self.save()
        return {"scored": len(graded), "unavailable": len(unavailable),
                "scoredIds": graded, "stillWaiting": len(self.waiting(now))}

    def summary(self, now=None) -> dict:
        return summarise(self.all(), now)

    def purge(self):
        """Only for tests and an explicit user reset."""
        with self._lock:
            self._entries = {}
            self.save()
        return self

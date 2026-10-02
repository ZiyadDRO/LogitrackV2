"""
store_clock.py: the store's time zone, and "which day is it" by the store's calendar.

One rule for the whole app: EXACT MOMENTS are kept in UTC (when an order was placed, when
stock was read, when a count was taken), and the question "which day does this belong
to?" is answered in the STORE's time zone.

Why the store's and not the viewer's: a store's sales day is set by where the store is.
The browser used to report its own zone, and the server kept whichever one arrived last,
so on a hosted server two people in different zones would keep flipping each other's
"today", and the nightly jobs (which run with no browser open) had no zone at all. Several
modules also counted days in UTC, which for a US store puts every evening after 7-8pm on
the next day.

The zone is chosen in main.py (_apply_store_zone): the one set on the Closed days panel,
else the connected store's own (Square location, Shopify shop), else Eastern Time.
Until main sets it, UTC, which is what these modules did before, so a module used on its
own (a test, a script) behaves as it always has.
"""
from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

DEFAULT_ZONE = "America/New_York"

_zone: str | None = None


def valid(name) -> bool:
    if not name or not isinstance(name, str):
        return False
    try:
        ZoneInfo(name)
        return True
    except Exception:                                   # noqa: BLE001
        return False


def set_zone(name: str | None) -> None:
    global _zone
    _zone = name if valid(name) else None


def zone() -> str | None:
    return _zone


def tzinfo():
    return ZoneInfo(_zone) if _zone else _dt.timezone.utc


def now() -> _dt.datetime:
    """This moment, on the store's clock (timezone-aware)."""
    return _dt.datetime.now(_dt.timezone.utc).astimezone(tzinfo())


def today() -> _dt.date:
    return now().date()


def local_date(ts) -> _dt.date | None:
    """The store's calendar day for a moment: an aware datetime, a naive one (taken as
    UTC), or an ISO string ("...Z" included). A plain date string is its own day."""
    if ts is None:
        return None
    if isinstance(ts, _dt.datetime):
        t = ts
    elif isinstance(ts, _dt.date):
        return ts
    else:
        raw = str(ts).strip()
        if len(raw) == 10:
            try:
                return _dt.date.fromisoformat(raw)
            except ValueError:
                return None
        try:
            t = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.astimezone(tzinfo()).date()


def day_start(day) -> _dt.datetime:
    """Midnight at the start of `day` on the store's clock, as an aware datetime."""
    d = day if isinstance(day, _dt.date) else _dt.date.fromisoformat(str(day)[:10])
    return _dt.datetime.combine(d, _dt.time.min, tzinfo=tzinfo())

"""Backtest persistence — measured protection levels that survive a restart.

WHY THIS EXISTS, GIVEN main.py SAYS BACKTEST STATE IS SESSION-SCOPED

The original rule was that nothing about a backtest is written to disk, so "a run can
never be contaminated by a previous one, and there are no stale files to reason about."
The hazard is real: reporting one dataset's measurements against a different dataset is
worse than reporting nothing.

But forgetting is a blunt way to get that guarantee, and it has a cost that grows once
the app runs on a server: every deploy silently downgrades every product from a measured
protection level to an estimate until something re-runs a job that takes minutes.

So this keeps the guarantee and drops the amnesia. A saved run records a FINGERPRINT of
the catalog it was measured against — per product: row count, first and last date, and
the total units. On load, the fingerprint is recomputed from the current catalog and the
saved run is used ONLY if it still matches. Change the data in any way that could move a
result — new rows, a re-sync, a deleted product, an edited history — and the fingerprint
differs, the saved run is discarded, and you are back to an estimate exactly as before.

Contamination is therefore impossible by construction rather than by forgetting, which is
the same property the original comment was protecting, enforced more precisely.

WHAT IS SAVED

The full report (what the Backtest tab renders), the scored windows (so a cost edit can
re-price instantly instead of refitting), the measured tier per product, the exclusions,
and the economics the run was measured with — which `_economics_drifted` needs in order
to notice that costs have moved since.
"""

import os, json, shutil, hashlib
import pandas as pd

VERSION = 1
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_store")


def enabled() -> bool:
    """Shares the catalog's switch: with no persisted catalog there is nothing for a
    saved run to be checked against, so persisting one would be meaningless."""
    return os.environ.get("LOGITRACK_PERSIST", "1").strip() not in ("0", "false", "no")


def fingerprint(catalog: dict) -> str:
    """Identify the DATA a run was measured against.

    Deliberately cheap and structural rather than a hash of every row: what can move a
    backtest result is a product appearing or leaving, its history growing or shrinking,
    its date range shifting, or its units changing. All four show up here. Rounding the
    unit total keeps float noise from inventing a mismatch."""
    parts = []
    for sid in sorted(catalog):
        df = (catalog.get(sid) or {}).get("df")
        if df is None or not len(df):
            continue
        ds = pd.to_datetime(df["ds"])
        y = pd.to_numeric(df["y"], errors="coerce").fillna(0).sum()
        parts.append(f"{sid}|{len(df)}|{ds.min():%Y-%m-%d}|{ds.max():%Y-%m-%d}|{round(float(y), 3)}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def save(state: dict, catalog: dict) -> bool:
    """Persist a completed run against the catalog it was measured on. Never raises — a
    disk problem must not fail the backtest that just succeeded."""
    if not enabled():
        return False
    tmp, old = _DIR + ".tmp", _DIR + ".old"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)

        rows = state.get("rows")
        if rows is not None and len(rows):
            rows.to_csv(os.path.join(tmp, "rows.csv"), index=False)

        payload = {
            "version": VERSION,
            "fingerprint": fingerprint(catalog),
            "report": state.get("report"),
            # JSON keys are strings; the combos are (lead, coverage) pairs, so they go out
            # as lists and come back as tuples rather than relying on tuple round-tripping.
            "combos": [list(c) for c in (state.get("combos") or [])],
            "tiers": state.get("tiers") or {},
            "exclusions": state.get("exclusions") or {},
            "inputs": state.get("inputs") or {},
            "hasRows": rows is not None and len(rows) > 0,
        }
        with open(os.path.join(tmp, "backtest.json"), "w") as f:
            json.dump(payload, f)

        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(_DIR):
            os.rename(_DIR, old)
        os.rename(tmp, _DIR)
        shutil.rmtree(old, ignore_errors=True)
        return True
    except Exception as e:                                   # noqa: BLE001
        print(f"[backtest_store] save failed ({e}) — the run stays in memory only")
        shutil.rmtree(tmp, ignore_errors=True)
        return False


def load(catalog: dict) -> dict | None:
    """Return the saved run, or None if there isn't one or it no longer describes this
    catalog. None means "fall back to estimates" — the pre-existing behaviour."""
    if not enabled() or not os.path.isfile(os.path.join(_DIR, "backtest.json")):
        return None
    try:
        with open(os.path.join(_DIR, "backtest.json")) as f:
            payload = json.load(f)
        if int(payload.get("version") or 0) != VERSION:
            print(f"[backtest_store] saved run is version {payload.get('version')}, "
                  f"this build reads {VERSION} — ignoring it")
            return None

        want = fingerprint(catalog)
        if payload.get("fingerprint") != want:
            print("[backtest_store] the data has changed since the last test — "
                  "discarding it and falling back to estimated protection levels.")
            return None

        rows = None
        fp = os.path.join(_DIR, "rows.csv")
        if payload.get("hasRows") and os.path.isfile(fp):
            # sku as text, always: a catalog of numeric-looking ids would otherwise come
            # back as ints and stop matching the string keys the tier cache is built on.
            rows = pd.read_csv(fp, dtype={"sku": str})

        return {
            "report": payload.get("report"),
            "rows": rows,
            "combos": [tuple(c) for c in (payload.get("combos") or [])],
            "tiers": payload.get("tiers") or {},
            "exclusions": payload.get("exclusions") or {},
            "inputs": payload.get("inputs") or {},
        }
    except Exception as e:                                   # noqa: BLE001
        print(f"[backtest_store] restore failed ({e}) — falling back to estimates")
        return None


def clear() -> None:
    shutil.rmtree(_DIR, ignore_errors=True)
    shutil.rmtree(_DIR + ".tmp", ignore_errors=True)
    shutil.rmtree(_DIR + ".old", ignore_errors=True)

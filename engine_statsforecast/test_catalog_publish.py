"""
The catalogue is published, never edited in place.

WHY THIS FILE EXISTS. `_rebuild` used to write `_sku_cache[sku_id] = entry` one product at
a time, while every reader that aggregates across the catalogue (/api/skus, the Fleet
summary, the Scorecard, grouping, the exports) read it without taking _state_lock — on
purpose, because a full refit holds that lock for minutes and blocking every page load for
that long is worse than the bug.

That was survivable only while nothing ran in the background. Once the nightly sync moved
onto its own thread, a refit landing under a reader served a HALF-SWAPPED catalogue: some
products refit, some not, and totals summed across both. It is silent, it is rare, and it
is unreproducible afterwards, which is the worst shape a bug can have.

The fix is a publish discipline: a writer copies the catalogue, edits the copy, and rebinds
the name in one statement; a reader binds it once and reads that snapshot. These tests
check the property that buys — a reader sees one whole catalogue, never a mixture — rather
than checking that particular lines of code are present.

The generation trick: in generation `g` every product's number is `g`. A reader summing N
products must therefore see exactly N*g. Any other total is arithmetic done across two
catalogues, which is precisely the bug.

Offline. No network, no model fits, no server.
"""
from __future__ import annotations

import inspect
import sys
import threading
import time

import pandas as pd

import main


FAILURES: list[str] = []
N_SKUS = 40


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


def entry(sku: str, generation: int) -> dict:
    """A catalogue entry shaped enough for the real readers, tagged with its generation."""
    days = pd.date_range("2026-01-01", periods=10, freq="D")
    df = pd.DataFrame({"ds": days, "y": [float(generation)] * 10})
    return {"sku_name": f"{sku} name", "mode": "daily", "filename": "test.csv",
            "df_train": df, "future_fc": None, "generation": generation,
            "stockout_rows_dropped": 0}


def catalogue(generation: int) -> dict:
    return {f"SKU{i:03d}": entry(f"SKU{i:03d}", generation) for i in range(N_SKUS)}


# ── 1. a reader never sees two generations at once ──────────────────────────────────
def test_publish_is_atomic_for_readers():
    main._publish_cache(catalogue(1))
    seen_totals: set[int] = set()
    stop = threading.Event()
    reader_error: list[str] = []

    def reader():
        try:
            while not stop.is_set():
                # Exactly what a real aggregating endpoint does: bind once, then sum.
                snap = main._cache()
                total = sum(e["generation"] for e in snap.values())
                if snap:
                    seen_totals.add(total)
        except Exception as exc:                                  # noqa: BLE001
            reader_error.append(f"{type(exc).__name__}: {exc}")

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    for g in range(2, 40):
        main._publish_cache(catalogue(g))
        time.sleep(0.002)
    stop.set(); t.join(timeout=5)

    check("reader did not blow up", not reader_error, reader_error)
    check("reader saw more than one catalogue (test is actually exercising it)",
          len(seen_totals) > 1, f"totals seen: {sorted(seen_totals)[:5]}")
    torn = [tot for tot in seen_totals if tot % N_SKUS != 0]
    check("every total was one whole generation, never a mixture", not torn,
          f"torn totals: {sorted(torn)[:5]}")


# ── 2. a held snapshot is immune to later writes ────────────────────────────────────
def test_snapshot_survives_a_publish():
    main._publish_cache(catalogue(7))
    held = main._cache()
    main._publish_cache(catalogue(8))
    check("held snapshot keeps its own generation",
          all(e["generation"] == 7 for e in held.values()))
    check("the live catalogue moved on",
          all(e["generation"] == 8 for e in main._cache().values()))
    check("they are different objects", held is not main._cache())


def test_delete_does_not_mutate_a_held_snapshot():
    main._publish_cache(catalogue(3))
    held = main._cache()
    victim = "SKU005"
    # What delete_sku does, with the lock its endpoint holds.
    with main._state_lock:
        main._publish_cache({k: v for k, v in main._cache().items() if k != victim})
    check("delete removed it from the live catalogue", victim not in main._cache())
    check("delete left the held snapshot whole", victim in held)
    check("held snapshot kept its full size", len(held) == N_SKUS)


# ── 3. the real endpoint, hammered during publishes ─────────────────────────────────
def test_list_skus_reads_one_generation():
    main._publish_cache(catalogue(1))
    stop = threading.Event()
    bad: list[str] = []

    def reader():
        while not stop.is_set():
            try:
                rows = main.list_skus()
            except Exception as exc:                              # noqa: BLE001
                bad.append(f"raised {type(exc).__name__}: {exc}")
                return
            # totalUnitsSold is 10 days x the generation number, so a clean read gives
            # one distinct value across the whole response.
            gens = {r["totalUnitsSold"] for r in rows}
            if rows and len(gens) != 1:
                bad.append(f"mixed generations in one response: {sorted(gens)}")
                return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    for g in range(2, 30):
        main._publish_cache(catalogue(g))
        time.sleep(0.002)
    stop.set(); t.join(timeout=5)
    check("/api/skus never mixed two catalogues", not bad, bad[:2])


# ── 4. check-then-get is one snapshot, so a delete can't turn a 404 into a crash ────
def test_forecast_lookup_cannot_keyerror():
    src = inspect.getsource(main.get_forecast)
    head = src.split("df_train")[0]
    check("get_forecast does not read _sku_cache twice",
          head.count("_cache()") <= 1 and "_sku_cache[" not in head,
          head[:200])
    check("get_forecast binds a snapshot before the membership test",
          "_snap = _cache()" in head and "in _snap" in head)


# ── 5. the invariant itself, guarded at the source ──────────────────────────────────
def test_rebuild_never_writes_the_live_cache():
    src = inspect.getsource(main._rebuild)
    check("_rebuild does not assign into the live catalogue",
          "_sku_cache[" not in src,
          [ln.strip() for ln in src.splitlines() if "_sku_cache[" in ln])
    check("_rebuild stages its work", "staged[sku_id] = entry" in src)
    check("_rebuild publishes when it is done", "_publish_cache(staged)" in src)


def test_no_endpoint_mutates_the_published_catalogue():
    """Nothing outside the publish machinery may write to _sku_cache."""
    src = inspect.getsource(main)
    offenders = []
    allowed = ("_sku_cache = new", "staged = dict(_sku_cache)", "_sku_cache.items() if k != sku_id")
    for i, line in enumerate(src.splitlines(), 1):
        st = line.strip()
        if st.startswith("#") or "_sku_cache" not in st:
            continue
        if any(a in st for a in allowed):
            continue
        # Only writes AIMED AT the catalogue count. `len(_sku_cache)` inside some other
        # object's .update() is a read and must not be flagged.
        if ("_sku_cache[" in st
                or "_sku_cache.clear(" in st or "_sku_cache.pop(" in st
                or "_sku_cache.update(" in st or "_sku_cache.setdefault(" in st):
            offenders.append(f"{i}: {st}")
    check("no in-place mutation of the published catalogue survives", not offenders, offenders)


if __name__ == "__main__":
    print("\nCatalogue publish discipline\n" + "-" * 44)
    for fn in (test_publish_is_atomic_for_readers,
               test_snapshot_survives_a_publish,
               test_delete_does_not_mutate_a_held_snapshot,
               test_list_skus_reads_one_generation,
               test_forecast_lookup_cannot_keyerror,
               test_rebuild_never_writes_the_live_cache,
               test_no_endpoint_mutates_the_published_catalogue):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All catalogue-publish tests passed.")

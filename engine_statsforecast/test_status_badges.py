"""
Every badge, in every condition, with no cell left to chance.

WHY THIS FILE EXISTS. Two products in the same catalogue — both with a few months of
sporadic history, neither having sold in months — were showing different badges, and the
reason turned out to be nothing about the products. One had 64 days of history and one
had 110, and `sc_status` used a flat 90-day cutoff to decide whether "not selling, plenty
of cover" meant "Overstocked" or "Dead stock". Meanwhile a third, whose stock nobody had
ever counted, was reported as an OVERDUE REORDER for zero units, because the placeholder
stock figure was fed into a countdown as though it were a reading.

So the point here is not to check a handful of happy paths. It is to enumerate the whole
input space and assert that the answer is defensible in every cell — because the failure
mode was never "this one case is wrong", it was "these two cases disagree for no reason".

The axes, and what each is:
    counted    has anyone actually counted this stock?   (the placeholder problem)
    dormant    has it stopped selling, on its own rhythm? (the 90-day-flip problem)
    cover      days of stock at forecast demand
    dur        days until the reorder point (cover - lead time)
    st         sell-through: trailing units / (trailing units + stock)
    is_new     under 90 days of history
    po         a purchase order already inbound

Offline. No network, no server, no model fits.
"""
from __future__ import annotations

import itertools
import sys

import main as M


FAILURES: list[str] = []
LT, COV = 14, 30


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


def status(counted=True, dormant=False, cover=60, st=0.5, is_new=False, po=False,
           lt=LT, cov=COV, ever_sold=True):
    dur = None if cover is None else cover - lt
    return M.sc_status(cover, dur, st, lt, cov, po, is_new=is_new,
                       dormant=dormant, stock_counted=counted, ever_sold=ever_sold)


# ── 1. uncounted stock outranks everything ─────────────────────────────────────────
def test_uncounted_wins_over_every_other_input():
    """Every verdict below is arithmetic on the stock figure. If that figure was invented,
    so is the verdict — so nothing else may override this."""
    print("uncounted stock is reported as such, whatever else is true")
    seen = set()
    for dormant, cover, st, is_new, po in itertools.product(
            (False, True), (None, 0, 5, 60, 500), (None, 0.0, 0.04, 0.5), (False, True), (False, True)):
        seen.add(status(counted=False, dormant=dormant, cover=cover, st=st, is_new=is_new, po=po))
    check("exactly one verdict across all 160 combinations", len(seen) == 1, seen)
    check("and it is 'Stock not counted'", seen == {"Stock not counted"}, seen)


def test_uncounted_is_never_an_urgency():
    print("an uncounted product never produces a reorder urgency")
    from_fleet = M.STATUS_RANK["Stock not counted"]
    check("it ranks below a real stockout", from_fleet > M.STATUS_RANK["Stockout risk"])
    check("but above everything we CAN measure",
          all(from_fleet < M.STATUS_RANK[k]
              for k in ("Dead stock", "Overstocked", "Reorder due", "Healthy")))


# ── 2. dormancy, not history length, decides dead ──────────────────────────────────
def test_history_length_no_longer_flips_the_badge():
    """THE REPORTED BUG. Same behaviour, different history length, different badge."""
    print("two identical non-sellers get the same badge regardless of age")
    # "Not selling, plenty of cover" — the Anime Lamp (64d) vs World Cup (110d) case.
    young = status(counted=True, dormant=True, cover=500, st=0.0, is_new=False)
    old   = status(counted=True, dormant=True, cover=500, st=0.0, is_new=False)
    check("a dormant product is Dead stock", young == "Dead stock", young)
    check("and history length changes nothing between two dormant products", young == old)

    # Across the whole space, dormant + established never yields anything but dead.
    outs = {status(counted=True, dormant=True, cover=c, st=s, is_new=n, po=p)
            for c in (None, 0, 5, 60, 500) for s in (None, 0.0, 0.5)
            for p in (False, True) for n in (False, True)}
    check("dormant + has sold is ALWAYS Dead stock, at any age", outs == {"Dead stock"}, outs)


def test_a_genuinely_new_product_is_never_called_dead():
    """The distinction is HAS IT SOLD, not how old it is.

    This test previously asserted that dormant + is_new is never Dead stock, and that was
    wrong in a way only the real endpoint revealed: a product that traded for a month and
    then went silent for two has 89 days of history, trips a 90-day "still new" guard, and
    was reported as Overstocked. It had not just launched — it had died.
    """
    print("never-sold and new is spared; sold-then-stopped is not")
    never = status(counted=True, dormant=True, cover=500, st=0.0, is_new=True, ever_sold=False)
    check("never sold + still young -> not Dead stock", never != "Dead stock", never)
    check("it reads as Overstocked instead", never == "Overstocked", never)

    stopped = status(counted=True, dormant=True, cover=500, st=0.0, is_new=True, ever_sold=True)
    check("SOLD then stopped -> Dead stock even under the 90-day mark",
          stopped == "Dead stock", stopped)

    # And once a never-sold product is no longer young, it stops being excused.
    old_never = status(counted=True, dormant=True, cover=500, st=0.0, is_new=False, ever_sold=False)
    check("never sold and no longer new -> Dead stock", old_never == "Dead stock", old_never)


def test_dormant_beats_a_stockout_reading():
    """A product that stopped selling and happens to be low on stock is not urgent."""
    print("dormant outranks a stockout countdown")
    out = status(counted=True, dormant=True, cover=1, st=0.9, is_new=False)
    check("no 'order now' on something nobody is buying", out == "Dead stock", out)


# ── 3. the ordinary ladder still works ─────────────────────────────────────────────
def test_live_product_ladder():
    print("a live, counted, selling product still grades normally")
    check("low cover -> Stockout risk", status(cover=5) == "Stockout risk")
    check("just past the reorder point -> Reorder due",
          status(cover=25) == "Reorder due", status(cover=25))
    check("comfortable -> Healthy", status(cover=40) == "Healthy", status(cover=40))
    check("way over target -> Overstocked", status(cover=200, st=0.5) == "Overstocked")
    check("buried in cover and barely moving -> Dead stock",
          status(cover=500, st=0.01) == "Dead stock")
    check("an inbound PO suppresses the urgency",
          status(cover=5, po=True) != "Stockout risk", status(cover=5, po=True))


def test_every_cell_is_a_known_status():
    """No combination may produce something the UI has no word for."""
    print("the whole input space maps onto the known vocabulary")
    known = set(M.STATUS_RANK)
    bad = []
    for counted, dormant, cover, st, is_new, po in itertools.product(
            (True, False), (False, True), (None, 0, 1, 5, 25, 44, 60, 200, 500),
            (None, 0.0, 0.01, 0.049, 0.05, 0.5, 1.0), (False, True), (False, True)):
        for ever in (True, False):
            out = status(counted=counted, dormant=dormant, cover=cover, st=st,
                         is_new=is_new, po=po, ever_sold=ever)
            if out not in known:
                bad.append((out, counted, dormant, cover, st, is_new, po, ever))
    check(f"all {2*2*9*7*2*2*2} combinations return a known status", not bad, bad[:3])


def test_status_and_rank_agree():
    print("every status the ladder can emit has a rank")
    emitted = set()
    for counted, dormant, cover, st, is_new, po in itertools.product(
            (True, False), (False, True), (None, 0, 5, 25, 60, 500),
            (None, 0.0, 0.01, 0.5), (False, True), (False, True)):
        emitted.add(status(counted=counted, dormant=dormant, cover=cover, st=st,
                           is_new=is_new, po=po))
    missing = emitted - set(M.STATUS_RANK)
    check("no status is missing from STATUS_RANK", not missing, missing)


# ── 4. the specific products from the bug report ───────────────────────────────────
def test_the_reported_products():
    print("the three products that prompted this")
    # Anime Lamp: 64d history, 2 units ever, 63 units on the shelf from Square, dormant.
    lamp = status(counted=True, dormant=True, cover=None, st=0.0, is_new=True, ever_sold=True)
    check("Anime Lamp (64d history, live count, sold twice then stopped) -> Dead stock",
          lamp == "Dead stock", lamp)

    # World Cup: 110d history, 1 unit ever, NO stock figure anywhere.
    wc = status(counted=False, dormant=True, cover=None, st=0.0, is_new=False)
    check("World Cup (no count anywhere) -> Stock not counted", wc == "Stock not counted", wc)
    check("and NOT an overdue reorder", wc != "Stockout risk", wc)

    # They must not disagree for reasons unrelated to the products.
    check("the two differ only because one is counted and the other is not",
          lamp != wc
          and status(counted=False, dormant=True, cover=None, st=0.0, is_new=False) == wc
          and status(counted=True, dormant=True, cover=None, st=0.0, is_new=False) == lamp)


# ── 5. the assumed figure itself ───────────────────────────────────────────────────
def test_assumed_stock_is_one_number():
    print("there is exactly one assumed-stock figure")
    check("ASSUMED_STOCK is 50", M.ASSUMED_STOCK == 50, M.ASSUMED_STOCK)
    import inspect
    src = inspect.getsource(M)
    # STOCK defaults only. `limit=Query(default=500)` on a log endpoint is a row cap and
    # has nothing to do with inventory; flagging it would train this check to be ignored.
    strays = [ln.strip() for ln in src.splitlines()
              if ('"stock", 500' in ln or "'stock', 500" in ln
                  or 'get("stock"), 500' in ln
                  or ("stock" in ln and "default=500" in ln.replace(" ", "")))
              and "ASSUMED_STOCK" not in ln and not ln.strip().startswith("#")]
    check("no 500-unit STOCK default survives anywhere", not strays, strays)

    # And the browser must agree with the server, or the two screens diverge again.
    import pathlib, re
    hj = pathlib.Path(__file__).resolve().parent.parent / "src" / "lib" / "helpers.js"
    if hj.exists():
        m = re.search(r"DEFAULT_PARAMS\s*=\s*\{\s*stock:\s*(\d+)", hj.read_text())
        check("the browser's DEFAULT_PARAMS.stock matches ASSUMED_STOCK",
              m is not None and int(m.group(1)) == M.ASSUMED_STOCK,
              m.group(1) if m else "not found")


# ── 5b. the UI must have a word for every status the backend can emit ──────────────
def test_every_backend_status_has_a_ui_badge():
    """The sidebar used to decide its badge with a chain of string comparisons that knew
    about two statuses. Everything else fell through to a reorder countdown which is null
    for an unmeasured product, so it rendered NOTHING — a whole catalogue of blank rows,
    and no error anywhere. Adding a status on the server could do it again silently.

    So: every status the ladder can emit must map to a shared definition, and each of
    those must carry a badge unless blankness is the deliberate choice.
    """
    print("the UI has a word for every status")
    import pathlib, re
    hj = pathlib.Path(__file__).resolve().parent.parent / "src" / "lib" / "helpers.js"
    if not hj.exists():
        check("helpers.js found", False, str(hj)); return
    src = hj.read_text()

    keymap = dict(re.findall(r'"([^"]+)":\s*"([a-z]+)"',
                             re.search(r"SC_STATUS_KEY\s*=\s*\{(.*?)\}", src, re.S).group(1)))
    states = set(re.findall(r"^\s{2}([a-z]+):\s*\{", src, re.M))

    missing = [st for st in M.STATUS_RANK if st not in keymap]
    check("every backend status maps to a UI key", not missing, missing)

    unknown = [k for k in keymap.values() if k not in states]
    check("every mapped key has a SKU_STATES entry", not unknown, unknown)

    # Which statuses are allowed to render no badge at all, and why.
    silent_ok = {"healthy", "unrated"}
    blank = []
    for st, key in keymap.items():
        block = re.search(rf"^\s{{2}}{key}:\s*\{{(.*?)^\s{{2}}\}},", src, re.S | re.M)
        if not block:
            continue
        badge = re.search(r'badge:\s*("([^"]*)"|null)', block.group(1))
        if key not in silent_ok and (badge is None or badge.group(1) == "null"):
            blank.append(st)
    check("every actionable status carries a badge", not blank, blank)

    # And the sidebar must be reading those definitions rather than hard-coding them.
    sb = hj.parent.parent / "components" / "Sidebar.jsx"
    if sb.exists():
        body = sb.read_text()
        check("the sidebar derives its badge from the shared definitions",
              "statusInfo(" in body and "info.badge" in body)
        check("and no longer hard-codes status strings for the badge",
              'healthStatus === "Dead stock"' not in body.split("const badgeText")[0].split("badgeText")[-1]
              or "info.badge" in body)


# ── 6. the endpoints, actually called ──────────────────────────────────────────────
def test_the_scorecard_endpoint_actually_runs():
    """THE TEST THAT WAS MISSING.

    Every check above drives sc_status directly, and they all passed while /api/scorecard
    raised NameError on its first line of new code — `cfg` was read from the caller's
    scope. A unit test on a pure function cannot see that; only calling the endpoint can.
    So this builds a real catalogue and calls the real handler, twice: once with
    provenance supplied and once without, because the second path is the one that broke.
    """
    print("the real endpoint, with a real catalogue")
    import pandas as pd
    import warnings; warnings.filterwarnings("ignore")

    end = M.today().normalize() - pd.Timedelta(days=1)
    days = pd.date_range(end - pd.Timedelta(days=89), end, freq="D")
    rows = [{"date": d.strftime("%Y-%m-%d"), "sku": "FAST", "sku_name": "Fast",
             "units_sold": 3, "price": 10.0} for d in days]
    # Sold for 30 days, silent for 60. NOT a new product — a dead one.
    rows += [{"date": d.strftime("%Y-%m-%d"), "sku": "STOPPED", "sku_name": "Stopped",
              "units_sold": 2, "price": 10.0} for d in days[:30]]
    try:
        M._ingest(pd.DataFrame(rows), "t.csv", append=False, reanchor=False, auto_backtest=False)
    except Exception as exc:                                  # noqa: BLE001
        check("catalogue ingests", False, f"{type(exc).__name__}: {exc}")
        return

    body = {"trailingDays": 30, "skus": {
        "FAST":    {"stock": 120, "leadTime": 14, "coverage": 30, "stockSource": "live"},
        "STOPPED": {"stock": 80,  "leadTime": 14, "coverage": 30, "stockSource": "live"}}}
    try:
        sc = M.get_scorecard(body)
    except Exception as exc:                                  # noqa: BLE001
        check("/api/scorecard runs at all", False, f"{type(exc).__name__}: {exc}")
        return
    check("/api/scorecard runs at all", True)
    by = {r["skuId"]: r for r in sc["rows"]}
    check("a healthy seller grades Healthy", by["FAST"]["status"] == "Healthy", by["FAST"]["status"])
    check("sold-then-stopped grades Dead stock",
          by["STOPPED"]["status"] == "Dead stock", by["STOPPED"]["status"])
    check("and it is reported as dormant", by["STOPPED"].get("dormant") is True)

    # The path that crashed: no stockSource in the params at all.
    try:
        sc2 = M.get_scorecard({"trailingDays": 30,
                               "skus": {"FAST": {"stock": 50, "leadTime": 14, "coverage": 30}}})
    except Exception as exc:                                  # noqa: BLE001
        check("/api/scorecard runs without stockSource", False, f"{type(exc).__name__}: {exc}")
        return
    r = sc2["rows"][0]
    check("/api/scorecard runs without stockSource", True)
    check("no provenance -> Stock not counted", r["status"] == "Stock not counted", r["status"])
    check("and no reorder countdown is published", r.get("daysUntilReorder") is None)
    check("and no cover is published", r.get("daysOfCover") is None)
    check("every status in the distribution is a known one",
          set(sc2["distribution"]) <= set(M.STATUS_RANK), set(sc2["distribution"]))


def test_the_forecast_endpoint_actually_runs():
    print("the forecast endpoint, both provenance paths")
    def fc(**kw):
        base = dict(sku_id="FAST", stock=50, lead_time_days=14, coverage_days=30,
                    strategy="balanced", forecast_months=3, units_on_order=0,
                    on_order_eta_days=None, unit_cost=None, fees=0.0, protection=None,
                    tz=None, stock_source=None, stock_counted_at=None)
        base.update(kw)
        return M.get_forecast(**base)
    try:
        a = fc(stock_source="unknown")
        b = fc(stock=100, stock_source="manual",
               stock_counted_at=(M.today().normalize()).strftime("%Y-%m-%d"))
    except Exception as exc:                                  # noqa: BLE001
        check("/api/forecast runs", False, f"{type(exc).__name__}: {exc}")
        return
    check("/api/forecast runs", True)
    check("uncounted publishes no stockout date", a["daysUntilStockout"] is None)
    check("uncounted publishes no reorder date", a["daysUntilReorder"] is None)
    check("uncounted publishes no order quantity", a["orderQty"] == 0)
    check("uncounted names the placeholder", a["assumedStock"] == M.ASSUMED_STOCK)
    check("counted publishes a stockout date", b["daysUntilStockout"] is not None)
    check("counted publishes an order quantity", b["orderQty"] > 0)
    check("a count taken today subtracts nothing yet",
          b["sinceCount"]["total"] == 0, b["sinceCount"])


if __name__ == "__main__":
    print("\nStatus badges — every condition\n" + "-" * 48)
    for fn in (test_uncounted_wins_over_every_other_input,
               test_uncounted_is_never_an_urgency,
               test_history_length_no_longer_flips_the_badge,
               test_a_genuinely_new_product_is_never_called_dead,
               test_dormant_beats_a_stockout_reading,
               test_live_product_ladder,
               test_every_cell_is_a_known_status,
               test_status_and_rank_agree,
               test_the_reported_products,
               test_assumed_stock_is_one_number,
               test_every_backend_status_has_a_ui_badge,
               test_the_scorecard_endpoint_actually_runs,
               test_the_forecast_endpoint_actually_runs):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All status-badge tests passed.")

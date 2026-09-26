# LogiTrack — Roadmap to Live Data

Rewritten 23 Sep 2026. Replaces the 26 Aug version, which was written for a Shopify-only
tool and described the clock, the backtest cadence and the read-path race as work still to
do. All three now exist, so following the old document would have meant rebuilding the
scheduler.

Square is the live integration. Shopify still works and is still supported; it is simply
no longer the one being tested against a real store.

---

## Where things stand

**Done, and verified by tests.**

- **Two commerce platforms behind one interface.** `sources.py` is a registry; Shopify and
  Square are entries in it. The dashboard menu, the saved-accounts list and the import path
  are all driven off `/api/sources`, so a third platform is a new entry, not a new code path.
- **Saved store credentials.** Tokens live at mode `0600`, gitignored, and are **never sent
  to the browser** — the browser gets a masked hint (`EAAA••••7Xk2`). Secret detection is
  fail-closed by substring, so a new camelCase field can't leak by being forgotten.
- **Data survives a restart.** Catalogue, costs, cost provenance, stock log and backtest all
  persist and reload.
- **A nightly sync runs on its own, with a resumable ledger.** Three stages — `fetch`,
  `refit`, `backtest` — each marked **only on success**, so a process killed mid-refit
  resumes the missing stages on the next start rather than believing the day is done.
- **Hourly inventory sampling**, which is what makes the censored-demand correction real
  (see below).
- **The catalogue is published, never edited in place.** A refit can no longer be read
  half-swapped.
- **Unknown stock is 0 with a flag**, everywhere, including the API defaults.
- **Grading settled** at three ordinal levels, with reason and predictability reported
  separately.

**Needs you, not code.**

- Set `LOGITRACK_ORIGINS="https://your-domain"` wherever the backend runs. It accepts only
  localhost otherwise and will refuse everything the moment it's hosted.
- Decide whether Square owns stock or you do (see Step 2).

---

## The two clocks, and why they are different

This is the part most likely to be "simplified" by someone later, so it is written down.

| | Nightly sync | Hourly sampler |
|---|---|---|
| Class | `DailySync` | `IntervalSampler` |
| Default | `00:15` local | every 3600s |
| What it does | Computes a **state** | Observes a **moment** |
| Missed run | Runs late, same answer | **Gone forever** |
| Catches up? | **Yes** | **No — and must not** |

A missed nightly refit is merely late: one run against current data gives the same answer
as the seven that were missed. A missed stock reading is unrecoverable, because nobody
records how much stock you had at 2pm last Tuesday. Giving the sampler catch-up would not
recover anything — it would file a reading taken now under an hour it does not describe.

### Why hourly, specifically

`censoring.py` corrects for demand you never saw because you ran out. Without it, the days
you sell out — your **busiest** days — read as low-demand days, the forecast drifts down,
you order less, and you sell out sooner. It is the classic inventory death spiral and the
module has existed, fully tested, for months.

It could never fire. The correction needs to know a product ran out **partway through** a
day, and that judgement is reconstructed from timestamped readings. At one reading a day
the reconstruction has exactly two possible answers — in stock ~24h, or out ~0h — so the
partial-day branch was unreachable and the uplift had never once run on real data.

`test_sampler.py` demonstrates both halves: the same real day (sold out at 11am) classified
`CAPPED` with no uplift at daily cadence, and `PARTIAL` with an uplift at hourly.

Sampling hourly costs almost nothing in storage — `StockLog` collapses runs of identical
readings, so a product sitting still all week is two samples, not 168 — and the uplift is
deliberately conservative: capped at 3×, and anything under 10% availability is treated as
no information rather than extrapolated.

### Cron, if you'd rather not trust a thread

Both clocks are daemon threads, so they live and die with the process. On a real box that
is fine. If you'd rather have something that survives a restart, both have endpoints:

```
0 * * * *   curl -X POST http://127.0.0.1:8000/api/sync/sample-now
15 0 * * *  curl -X POST http://127.0.0.1:8000/api/sync/now
```

`/api/sync` reports both clocks, including sampling coverage and consecutive failures.

---

## The backtest's cadence

Weekly on `SYNC_BACKTEST_WEEKDAY` (default Monday), **plus** three triggers that fire the
moment an estimate is standing in for a measurement:

1. **newly-testable** — a product just crossed the history bar and has never been measured
2. **settings-uncovered** — a lead time or coverage now in use was never scored
3. **economics-drifted** — cost or price moved past tolerance

Lead/coverage matching is tolerant (±25%), because a P80 lead time that moves by a day
should not discard a perfectly good measurement and trigger 2,500 model fits.

Don't run it more often than this. Test windows sit 28 days apart, so on most days no new
evidence has matured and a re-run returns yesterday's answer. Re-measuring constantly also
makes buying policy jitter on noise. **The live accuracy log is the monitor; the backtest is
the periodic policy review.** Don't use the review as the monitor.

### History thresholds, as they actually compute

| | Days | What it is |
|---|---|---|
| `BACKTEST_MIN_TESTABLE_DAYS` | **164** | 120 train + 44 horizon. Below this, no window exists at all. |
| Usable measured tier | **207** | 164 + three more cutoffs at the 14-day minimum spacing. |

Both scale with the horizon, which is `lead + coverage` computed **per product**, not a
constant. A 40-day lead time pushes the usable bar to ~233 days.

Known wrinkle, deliberately left: the usable-tier gate is driven by the **full** horizon,
while the safety buffer it unlocks is measured over the **lead window only**. So a
quarterly buyer waits two months longer than a monthly buyer for a measured buffer, on
evidence the buffer doesn't use. Splitting the gate is the correct fix; it also
re-decouples `BACKTEST_TIER_MIN_WINDOWS` from `MIN_WINDOWS_REPORTABLE`, which is the exact
drift that caused the cutoff-floor bug. Revisit once real products cross 207 days and the
cost is measurable.

---

## Step 1 — Reconcile Square against Square

The connector has run against a real kiosk (28 products, 133-day window), but before any
number is trusted in anger, and separately per location:

1. **Export everything first.** A sync replaces the whole catalogue.
2. Pull **30 days, one location**.
3. **Product count** matches Square's item library.
4. **Total units for one known week, per location**, against Square's own report filtered
   to that location. Checking only the total lets a location mix-up pass silently.
5. **One product's daily numbers**, on a day with evening trade. A timezone bug shows up
   here and nowhere else.
6. **The unmapped counts** — line items with no `catalog_object_id`, SKUs that fell back to
   a Square ID. If either is large, fix the catalog before fixing code.
7. Only then, full history.

`check_stock.py` reconciles inventory against the raw API with its own summing, deliberately
not reusing the connector's, so it can catch a summing bug rather than agree with one.

---

## Step 2 — The server

**Hetzner Cloud CX22, ~€4.35/month.** 2 vCPU, 4 GB RAM, 40 GB NVMe. A plain Linux box is
what this app wants: real cron, a real filesystem, one instance, no cold starts, enough
memory for Prophet fits.

Non-negotiables whatever you pick:

- **≥2 GB RAM**, ideally 4. Prophet and StatsForecast fits are the heavy part; 512 MB won't
  do it, which rules out the cheapest managed tiers on memory alone.
- **A persistent disk that survives deploys.** Container filesystems usually don't.
- **No sleeping.** Free and near-free tiers spin down when idle, and this app's whole
  problem is that its clock stops when nothing is running.
- **One instance.** The catalogue is an in-memory working copy; two instances would drift.

Managed platforms, for reference: Render's cron jobs can't access persistent disks, so the
tick would have to `curl` the web service. Fly.io works out ~$12/mo for 2 GB. Railway's
metered billing is hard to predict. Prices move — check before committing.

**Two decisions to settle while you build it.**

*Does Square own stock, or do you?* If Square owns it, the stock field becomes read-only and
arrival confirmation gets much simpler. If you keep manual overrides, you need a
reconciliation rule. Decide before writing schema; it shapes the tables.

*Suppliers, POs and settings need a home.* Square refills stock and unit costs on a sync but
not these, and supplier delivery history is the only input to every lead-time estimate in
the app. Nothing worth migrating exists yet, so this is build-once, not a data move.

---

## Step 3 — Live Accuracy: start it on a Sunday

The only honest accuracy evidence you have: forecasts sealed *before* the week happened,
then graded against what actually sold. A backtest replays history the models were fitted
near; this doesn't.

**The trap.** A weekly snapshot is labelled Sunday-to-Saturday, but the prediction it seals
is "the next 7 days from right now." Start on a Wednesday and the first sealed week holds a
Wednesday-to-Tuesday forecast filed as Sunday-to-Saturday, graded against Sunday-to-Saturday
sales. A period can only be recorded once, so it can't be corrected afterwards.

So: **start on a Sunday**, or start whenever and throw away week one. After week one it
self-corrects. A week nobody's backend was running is never backfilled — it is simply absent
from the record forever, which is the strongest argument for the clock living on a server.

---

## Step 4 — Still open

**Net out refunds.** A return currently counts as demand. That biases forecasts high, which
is the safe direction, but a large return can also look like a delivery arriving and write a
false date into lead-time history — and lead-time history is what every P80 is built from.
Needs a check of how Square reports refunds before it's written.

**Make the sync incremental.** It replaces the entire catalogue every time, clearing
model-switch history and refitting everything. Store a watermark, add an append path.
Deferred on purpose: at 28 products the full replace costs seconds. Revisit when the
catalogue is large or the API bill matters.

**Webhooks for orders and inventory**, with the poll as a reconciliation safety net.

**Stop the full-catalogue refit on small edits.** Adding one product's attributes or
deleting one product currently refits everything.

---

## Known gaps, deliberately left

**Fitted models aren't saved.** On restart the app refits from saved data — half a minute,
in the background, app usable throughout. On purpose: a pickled Prophet object is welded to
the library versions that wrote it, and a subtly-wrong load after a dependency bump is worse
than a slow boot. It's a wait, not a loss.

**Backtests are saved conservatively.** A saved run is reused only if the data fingerprint
still matches. `grew_only()` softens this — a catalogue that merely gained rows keeps its
tiers — but a shrink, a deletion or a changed start date discards them and you fall back to
estimates, which the UI labels provisional.

**Backtests still run as a full replay.** Old windows never change when new sales arrive, so
only new windows need scoring — a run could go from ~2,500 model fits to a handful.

**No significance test on per-product vs one-level-for-everyone.** The comparison
cross-validates, so it's honest about bias, but there's no confidence interval on the
difference, so a marginal win deploys per-product levels.

**Per-product levels are all-or-nothing.** Either every product gets its own protection level
or none do. A middle path is better but needs calibrating against real data.

**Multiple server instances aren't supported.** Stay on one.

**No seasonality under a year of history.** Discussed, not built. The candidates are a
category-level seasonal library and a declared-seasonality override ("I see a spike in
Nov–Dec"). This is the single biggest limitation for a store with 4 months of data.

**Multi-location.** Spec'd in `SQUARE_AND_LOCATIONS.md`, steps 4–7 not started: the
`location` dimension, `RESERVED_COLS`, the rollup view, transfer recommendations.

---

## Order of work

1. Reconcile Square per location — before trusting any number
2. Pick a server, set `LOGITRACK_ORIGINS`
3. Start Live Accuracy **on a Sunday**
4. Net out refunds
5. Seasonality for short histories — the real blocker on a young store
6. Multi-location
7. Incremental sync + webhooks, when scale makes it matter

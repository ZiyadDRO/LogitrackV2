# LogiTrack — Roadmap to Live Data

Written 26 Aug 2026. Covers what's done, what to do next, and in what order.

---

## Where things stand

**Done and ready to test.**

- Data survives a restart. The catalogue is written to disk and re-fitted on boot instead of asking for a re-upload.
- A completed backtest survives a restart too, fingerprinted against the data it was measured on. Same data → restored. Changed data → discarded, and you fall back to estimates.
- Live data keeps its real dates. Date re-anchoring now applies to CSV uploads only.
- A live sync no longer triggers a full backtest every time.
- "Reset everything" actually wipes everything, on the server and in the browser.

**Needs you, not code.**

- Apply for the `read_all_orders` Shopify scope. Without it you get 60 days of history, and a backtest needs 164 days per product — so every product gets skipped. **This has an approval queue outside your control. Start it first.**
- Set `SHOPIFY_SHOP` and `SHOPIFY_TOKEN` where the backend runs, and cron the hourly tick. Inventory history cannot be backfilled: every hour you don't capture is gone permanently.

---

## Step 1 — Test the connector (this weekend)

The Shopify connector has never run against a real store. The file says so. Everything downstream assumes it works, so find out first.

1. **Export everything first.** A sync replaces the whole catalogue.
2. Create a **read-only** custom app in Shopify — `read_orders` and `read_products` only.
3. Pull a **small window first** (30 days) to see whether it connects at all.
4. **Reconcile before trusting it:** product count, total units for one known week, one product's daily numbers, against Shopify's own reports.
5. Only then pull full history.

**Expect these, they are not bugs:**

- Every product "not tested — needs 164 days of history" if you only got 60 days.
- Returns counted as sales. Refunds aren't netted out yet.
- Orders with more than 25 line items truncated; products with more than 100 variants truncated.
- Inventory as one aggregate number, no per-location split.
- The app freezing for a few minutes on a deep pull.

---

## Step 2 — The server

### Recommendation

**Hetzner Cloud CX22 — about €4.35/month.** 2 vCPU, 4 GB RAM, 40 GB NVMe.

It's a plain Linux box, which is exactly what this app wants: real cron, a real filesystem, one instance, no cold starts, and enough memory for Prophet fits. Nothing else comes close on price for that much RAM.

The trade-off is that you're the sysadmin — OS updates, TLS certificates, and your own deploy script. For one app serving one store, that's an afternoon of setup and then very little.

### Why not the managed platforms

| | Cheapest useful tier | Persistent disk | Scheduled jobs | Catch for this app |
|---|---|---|---|---|
| **Hetzner CX22** | ~€4.35/mo, 4 GB RAM | Included (40 GB) | Real cron | You manage the box |
| **Render** | Starter $7/mo is 512 MB — too small for Prophet, so a larger paid tier | $0.25/GB/mo | Dedicated cron service, min $1/mo each | **Cron jobs can't access persistent disks** — the tick would have to `curl` the web service instead |
| **Fly.io** | ~$2/mo at 256 MB, plus ~$5/GB RAM — call it ~$12/mo for 2 GB | Volumes | Scheduled machines | More moving parts than you need |
| **Railway** | ~$5–15/mo, metered | Volumes | Cron | Metered billing is hard to predict |

Two things rule out the cheapest managed tiers regardless of price:

- **Memory.** Fitting Prophet and StatsForecast models is the heavy part of this app. 512 MB won't do it.
- **Sleeping.** Free and near-free tiers spin down when idle. This app's whole problem is that its clock stops when nothing is running.

### Non-negotiables when you pick

- At least 2 GB RAM, ideally 4.
- A persistent disk that survives deploys — container filesystems usually don't.
- **One instance.** Two would each hold their own copy of the catalogue in memory and drift apart. Scaling out is a much bigger job than the storage swap.
- Something that runs cron reliably.

### Two things to settle while you build it

**Does Shopify own stock, or do you?** If Shopify owns it, the stock field becomes read-only and the arrival-confirmation flow gets much simpler. If you keep manual overrides, you need a reconciliation rule. Decide before writing any schema, because it shapes the tables.

**Suppliers, purchase orders and settings need a home on the server.** Shopify can refill stock and unit costs on a sync, but not these, and the supplier delivery history is the only input to every lead-time estimate in the app. Nothing worth migrating exists yet, so this is a build-it-once job rather than a data move.

### Do this on day one

Set `LOGITRACK_ORIGINS="https://your-domain"`. The backend only accepts localhost connections otherwise, and it will refuse everything the moment it's hosted.

---

## Step 3 — The clock

Nothing in the backend currently runs on a schedule. The only periodic thing in the app is a timer inside the Live Accuracy screen, which runs only while that tab is open.

| Job | When | What it does | If it never runs |
|---|---|---|---|
| **Inventory sampling** | Hourly | Reads stock levels into the stock log | **Permanent data loss.** Shopify keeps no inventory history, so an unsampled hour can never be recovered. Degrades stockout detection and every forecast. |
| **Sales sync** | Hourly, or webhooks | Pulls new orders | Forecasts train on a window that ages out |
| **Refit + re-anchor** | Nightly | Re-cuts every forecast against the new last day of data | Reorder dates drift; at 180 days every product falsely flips to dormant |
| **Seal the week's forecast** | Weekly, Sunday | Locks in what was predicted before the week starts | A missed week is never backfilled — a permanent hole in your accuracy record |
| **Grade the closed week** | Weekly, Sunday | Scores last week's sealed forecast against actuals | Entries mature and sit ungraded forever |
| **Backtest review** | Monthly, or on trigger | Re-measures protection levels | Levels slowly stop matching reality |

**On the monthly backtest.** Don't run it more often. Test windows sit 28 days apart, so on most days no new evidence has matured and a re-run is guaranteed to produce the answer it produced yesterday. Re-measuring constantly also makes buying policy jitter on noise.

Triggers worth adding later, beyond the monthly floor:

- Live accuracy reports `overconfident` — your bands are too narrow, which means buffers are too small. This is the signal that a review is actually needed.
- Economics moved past tolerance (already detected in code).
- A lead time changed materially.
- Pool membership changed — review the affected products, since pooling means their forecasts moved.

**The live accuracy log is the monitor; the backtest is the periodic policy review.** Don't use the review as the monitor.

### Every job checks the clock when it runs

Cron does not backfill. If the box was down, or a deploy overran, or a job crashed, that firing is simply gone and the next one behaves as though nothing was missed.

So every scheduled job should store a **last-run timestamp** and, whenever it starts (on schedule *or* at boot), ask: how long has it been, and is that too long? A job that is overdue runs immediately instead of waiting for its next slot. Concretely, on startup the server should check each job and catch up anything stale before serving traffic as normal.

The data for this partly exists already: the saved backtest carries `ranAt`, and each measured protection level now carries `measuredAt`. Nothing checks them yet.

**What catching up means differs by job, and getting this wrong is silent.**

Jobs that capture a moment cannot be caught up. Missing one leaves a permanent hole:

| Job | Missed run |
|---|---|
| Inventory sampling | Gone. Shopify holds no history, so those hours can never be recovered. |
| Sealing the week's forecast | Gone. You cannot retroactively seal what you predicted last Tuesday. That week is absent from your accuracy record forever. |

Jobs that compute a state catch up on their own, and only ever need to run **once** no matter how many firings were missed:

| Job | Missed run |
|---|---|
| Sales sync | The next pull covers the gap. |
| Nightly refit | One refit against current data. You do not need the seven you missed. |
| Grading closed weeks | Already handled well: `lookback_days_for` widens the Shopify query to reach the oldest ungraded window rather than assuming a fixed 90 days, so everything pending grades in one pass. |
| Backtest review | One run, not four. |

**And say so on screen when something is behind.** If the last refit was three days ago, the numbers are three days stale and the user cannot tell. The backend already computes freshness markers that nothing displays.

### What should run unattended, and what should not

Not everything belongs on a timer. Three groups:

**Always, regardless of whether anyone is watching.** Inventory sampling, weekly sealing, weekly grading. These capture or close a moment, so there is no such thing as doing them later. This is the whole reason the clock has to leave the browser.

**On a schedule, for freshness.** Sales sync and the nightly refit. Not strictly moment-bound, but running them overnight is what makes opening the app instant and current instead of a minute of waiting followed by numbers measured against yesterday's data. Cheap enough to just do.

**Not unattended.** The full backtest. One run is roughly 2,500 model fits, which on a small box means pinned CPU for a long stretch and a real risk of running out of memory. It also produces the same answer on about 27 nights in 28, because test windows sit 28 days apart and no new evidence has matured. Monthly, or when a trigger fires. Never nightly.

### One hazard that background work introduces

Today almost nothing runs while you are using the app, so this does not bite. On a server it will.

Catalogue mutations are guarded by a lock, but the read paths (`/api/forecast`, `/api/scorecard`, `/api/skus`, grouping, history) do not take it. A nightly refit running while someone loads the fleet page can serve a half-swapped catalogue: some products refit, some not, totals summed across both. Rare, silent, and very hard to reproduce afterwards.

Worth fixing before the clock goes live, either by having readers take the lock or by building the new state off to one side and swapping it in atomically.

### Live Accuracy — start it on a Sunday

This is the only self-updating feature in the app, and the only honest accuracy evidence you have: forecasts sealed *before* the week happened, then graded against what actually sold. A backtest replays history the models were fitted near; this doesn't. Over time it should become the thing that tells you when protection levels need reviewing.

**One trap when you switch it on.** A weekly snapshot is labelled Sunday-to-Saturday, but the prediction it seals is "the next 7 days from right now." Start the hourly tick on a Wednesday and the first sealed week contains a Wednesday-to-Tuesday forecast filed as Sunday-to-Saturday — and it's graded against Sunday-to-Saturday sales. A period can only be recorded once, so it can't be corrected afterwards.

After week one it self-corrects: with the tick running continuously, each new week gets sealed within an hour of Sunday midnight, so the mismatch is hours rather than days.

So: **start the tick on a Sunday**, or start whenever and throw away week one's number. Don't read the first week as a real result.

Also worth knowing: a week nobody's app was running is never backfilled. It's simply absent from the record forever. That's the strongest argument for the clock living on a server rather than in a browser tab.

---

## Step 4 — Adapt for a live stream

- **Make the sync incremental.** It currently replaces the entire catalogue every time — clearing model-switch history, refitting everything. Store a watermark and add an append path.
- **Webhooks for orders and inventory**, with the hourly poll as a reconciliation safety net.
- **Net out refunds.** Right now a return counts as demand, which biases forecasts high. That direction is safe (you over-order rather than stock out), but it also means a large return can look like a delivery arriving and write a false date into your lead-time history.
- **Stop the full-catalogue refit on small edits.** Adding one product's attributes or deleting one product currently refits everything.
- **Don't re-anchor live data.** Already done — but keep it in mind if that code is ever touched.

---

## Known gaps, deliberately left

**Fitted models aren't saved.** On restart the app refits every product from the saved data — half a minute or so, in the background, app usable throughout. This is on purpose: a pickled Prophet object is welded to the library versions that wrote it, and a subtly-wrong load on a server after a dependency bump is worse than a slow boot. It's a wait, not a loss.

**Backtests are saved, but conservatively.** A saved run is only reused if the data fingerprint still matches exactly. Any change — new sales, a re-sync, a deleted product — discards it and you fall back to estimated protection levels until a new test runs. Estimates are labelled provisional in the UI, so nothing misrepresents itself. On a server this matters more than locally: every deploy is a restart.

**Backtests still run as a full replay.** They could be incremental — old test windows never change when new sales arrive, so only new windows need scoring. That would cut a run from ~2,500 model fits to a handful. Worth doing once the monthly cadence is real.

**No significance test on per-product vs one-level-for-everyone.** The comparison is honest about bias (it cross-validates), but there's no confidence interval on the difference, so a marginal win deploys per-product levels. The clean fix is to include the mixed policy in the bootstrap ranking that already exists for uniform levels, and apply the same "decisive" standard to both.

**Per-product levels are all-or-nothing.** Either every product gets its own protection level or none do. A middle path — a product earns its own level when its individual evidence clears the bar — is better, but the threshold needs calibrating against real data. Revisit after a month of live sales.

**Multiple server instances aren't supported.** The catalogue lives in memory as the working copy. Two instances would drift. Stay on one.

---

## Order of work

1. Apply for `read_all_orders` — **today**, it has a queue
2. Start hourly inventory capture — **today**, the data is unrecoverable
3. Test the connector — this weekend
4. Pick a server, set `LOGITRACK_ORIGINS`
5. Build the clock
6. Incremental sync + webhooks
7. Backtest cadence and triggers, with real data to calibrate against

---

## Sources

- [Render vs Railway vs Fly.io: Pricing Compared (2026)](https://dev.to/pavel-hostim/render-vs-railway-vs-flyio-pricing-compared-2026-2e5p)
- [How Render handles scheduled tasks](https://render.com/articles/how-render-handles-scheduled-tasks)
- [Render pricing, disks and free-tier sleep](https://www.srvrlss.io/provider/render/)
- [Hetzner Cloud CX22 pricing 2026](https://vpsfor.dev/posts/hetzner-cx22-pricing-2026/)
- [Hetzner Cloud review and benchmarks](https://betterstack.com/community/guides/web-servers/hetzner-cloud-review/)

Prices move — check the provider's live page before committing.

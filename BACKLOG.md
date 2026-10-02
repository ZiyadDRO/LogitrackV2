# LogiTrack V2 backlog

From the full audit on 2026-09-29. Line numbers are approximate; each item names the function or area to search for. Tick items as they're done, with a one-line note.

Conventions: `daysUntilReorder` null = no reorder date or stock not counted; -1 = one day overdue (NOT "none"). In JS `null <= 0` is true.

## Batch 1. One shared forecast request (done 2026-09-29)
- [x] Product page forecast request omitted stock_source / stock_counted_at (SkuDetailPanel.jsx buildUrl). **Fixed:** one builder, `forecastUrl` in src/lib/api.js, used by the product page and both Fleet requests.
- [x] Fleet page-load request omitted unit_cost, fees, protection, stock_counted_at (App.jsx loadSkuList). **Fixed:** same builder.
- [x] Exports sent no stockSource and the typed lead time (App.jsx buildExportSkus; main.py _export_view / export_all). **Fixed:** exports send merged params, the planned lead time, stockSource, stockCountedAt; export_all's scorecard body now carries stockSource, unitsOnOrder, onOrderEtaDays.
- [x] exports.py treated daysUntilReorder == -1 as "no reorder". **Fixed:** only None means none; one-day-overdue products are on the reorder sheet.
- [x] Displays used the typed lead time instead of the planned one (SkuDetailPanel coverage dates / gap warning / reorder banner / swing row / runway note; FleetViews reorder bands and cash-plan repeats). **Fixed:** all use planningLeadTime(...).days (App passes `leadTimeOf`).
- [x] "Use avg" / "Use P80" buttons did nothing once 3+ deliveries existed. **Fixed:** removed; the card marks what planning actually uses ("in use"), and when the lead time is pinned it offers "Plan on measured P80". The order-math card now reads the plan's source (measured / pinned / estimate / one-off).
- [x] Sidebar urgency badge ignored each product's lead time. **Fixed:** SkuListItem takes the planned lead time.

## Batch 2. Stock counts (done 2026-09-29)
- [x] A saved count timestamp ("...Z") crashed every later forecast with a 500 (units_sold_since). **Fixed:** converted to the store's time zone, then compared as a plain date.
- [x] Stock box confirmed a first count on the first keystroke. **Fixed:** confirm on Enter or leaving the box.
- [x] A counted stock of 0 never set alreadyOut. **Fixed:** counted zero = out of stock.
- [x] Inventory value included placeholder stock. **Fixed:** counted stock only, labelled when some products aren't counted.

## Batch 3. Status badges (done 2026-09-29)
- [x] With older sales data the scorecard counted demand for days already past (false "Stockout risk"). **Fixed:** `_from_today` in sc_score_one, same rule as get_forecast's fc_fwd.
- [x] Any open PO made a product Healthy. **Fixed:** the reorder countdown counts the PO from the day it lands; a PO too small or too late says "Order in transit won't cover it". Cover stays on-hand only. Clients that send no PO quantity keep the old behaviour.
- [x] buildScorecardBody ignored the one-off slow shipment while forecasts included it. **Fixed:** removed ignoreOneOff there (the backtest signature keeps it on purpose). The Fleet "reorder" pill also handles negative days.

Tests: engine_statsforecast/test_scorecard_consistency.py (fails on the old code, passes now).

## Batch 4. Dates and time zones (done 2026-09-29)
One rule now: exact moments stay in UTC; which DAY something belongs to is decided in the store's time zone (engine_statsforecast/store_clock.py). The zone: picked on the Closed days panel ("Store time zone"), else the connected store's own (Square location / Shopify shop), else Eastern Time (America/New_York, so EST/EDT switch automatically). The browser's zone no longer sets anything.
- [x] Browser worked in UTC (todayStr, formatDate, poEtaDays, modals, product page). **Fixed:** helpers.js todayStr/todayMs use the store's zone from /api/store-clock (App loads it; falls back to the browser's calendar date, never UTC). Header date, file names and the AI's "today" use it too.
- [x] Live accuracy graded a week before Saturday was over. **Fixed:** forecast_log.today uses the store's day.
- [x] Shopify orders dated by UTC. **Fixed:** the orders query also reads shop.ianaTimezone; orders are dated in it and "yesterday" is the shop's yesterday.
- [x] Sold-out hours over UTC days; arrivals dated by UTC day. **Fixed:** censoring, arrivals, stock_log coverage and live_actuals use store days.
- [x] (added) Nightly sync ran at 00:15 SERVER time. **Fixed:** runs on the store's wall clock.
- [x] (added) Square/Shopify report their zone at import; it's filed per workspace (closed_days.json "zones") and applied before ingesting.

Tests: engine_statsforecast/test_store_clock.py.

## Batch 5. Upload (done 2026-09-29)
- [x] drop_duplicates() dropped identical same-day sale lines (3 mugs → 1). **Fixed:** identical rows are kept and summed. Only a file whose whole block of rows repeats in order (pasted in 2-4 times) is collapsed, and the upload says so ("fileRepeatedTimes"). Same rule in backtest.load_dataframe.
- [x] Dates were guessed from the first row; day-first dates got swapped/dropped. **Fixed:** `_parse_dates` checks the whole column (ISO → first number > 12 = day-first → otherwise US, with a note). If more than 20% of dates can't be read, the upload is refused with a plain message instead of half-loading.
- [x] Blank stock became "1 unit from your file". **Fixed:** last known stock comes from the real figures (`_stock_raw`), is saved on the product (`last_known_stock`, persisted by catalog_store) and read by /api/skus. A product with no stock figures in a file that has a stock column no longer gets a column of filled-in 1s.
- [x] SKU codes read as numbers (leading zeros lost, spaces kept). **Fixed:** `_read_sheet` reads the SKU column as text; codes are trimmed.

Tests: engine_statsforecast/test_upload_parsing.py.

## Batch 6. Store switching: data safety (done 2026-09-29)
- [x] Connecting a second store while one is in use deleted the first store's products (upload_from_source). **Fixed:** a new store being saved is filed first and goes through the same switch as a saved one: whatever is loaded is set aside under its own workspace, the previous store is paused. A one-off (unsaved) import while a store shows pauses it and loads into the spreadsheets' workspace. A failed import switches back to what was showing.
- [x] Deleting the showing store left "showing" unrecorded. **Fixed:** set-aside spreadsheets come back if there are any, otherwise its products stay loaded as spreadsheets; either way SHEETS is recorded.
- [x] Spreadsheet upload while a store is in use merged into the store. **Fixed:** /api/upload pauses the store and switches to spreadsheets first, returns `storePaused` (the store's name), shown under the upload; UploadPanel text says so.
- [x] Import on a paused store wiped its measured protection levels. **Fixed:** `_swap_workspace(refit=False)` forgets the in-memory backtest (`_forget_backtest_memory`) and reads the workspace's own back (`_restore_backtest`).
- [x] A backtest finishing after a switch saved into the new workspace. **Fixed:** `_ws_epoch` bumped on every swap; full, partial and fleet-tab runs record (epoch, per-product fingerprint) when they snapshot and discard the result if the workspace changed or the data was rewritten (growth is fine).
- [x] A paused store's live prices / stock readings leaked into same-code spreadsheet products. **Fixed:** `_live_readings_apply()` — live price, hourly availability and StockLog stock apply only while a store's workspace is loaded (rebuild, /api/skus, forecast, scorecard, price path, live log).

Tests: engine_statsforecast/test_store_switching.py (fails on the old code, passes now).

## Batch 7. Product page editing (done 2026-09-29)
- [x] Draft saved whole and only reset on product switch (Mark received then any edit put stock back). **Fixed:** the page saves only the changed fields (`patchParams(id, patch)` in App); the draft follows params changes, keeping unsaved edits on top.
- [x] Debounced save could write A's settings onto B; closing dropped it; stale forecasts showed. **Fixed:** the pending save carries the product id, is flushed on product switch and on close; forecast responses are numbered and only the newest shows; the previous product's figures are cleared on switch. Params are keyed on content so the page stops re-requesting on every render.
- [x] refreshSkuSummary / loadScorecardRows / loadSkuList missed `suppliers`. **Fixed:** added; a supplier change refreshes the products whose planned lead time moved (and the scorecard).
- [x] Events editor Save dropped a filled-in, not-added event or an unconfirmed AI-parsed one. **Fixed:** Save includes both; a half-filled one stops the save and says what's missing.

Checked in a browser: order in transit, Mark Received (stock 50 → 70, its order marked received), then a lead-time edit: stock stays 70.

## Batch 8. Purchase orders and suppliers (done 2026-09-29)
- [x] Marking one order received cancelled another. **Fixed:** the PO carries `orderId`; Mark Received on the page marks that order, the Suppliers tab clears a PO only when it IS that order (`findPoOrder` / `poIsOrder`; old POs matched by date + qty only when unambiguous). Editing an open PO updates its order instead of filing a second one.
- [x] Empty arrival date added stock but left the order in transit. **Fixed:** Confirm needs a date (button disabled, markReceived refuses).
- [x] Logging an already-arrived past delivery added it to today's stock. **Fixed:** `arrivalAddsToStock`: arriving today adds; counted after it arrived doesn't; an old delivery with no count is off by default. The log form shows it as a tick box you can change.
- [x] Full report "Open POs" sheet always empty. **Fixed:** exportAll sends openPOs; the sheet lists each PO plus supplier orders still in transit (no double rows).
- [x] Supplier export listed "Unassigned orders" as a supplier. **Fixed:** no KPI row; its orders show as "(no supplier)" in the history.

Tests: tests/orders.test.mjs (node), engine_statsforecast/test_exports_orders.py.

## Batch 9. Backtest correctness (done 2026-09-29)
- [x] Untestable windows gave a "measured" Light 90%. **Fixed:** `_tier_summary` counts testable windows per product (`windows`; all windows are `windowsTotal`); a product with none gets no best tier, no money, is listed in `untestableSkus`, and stays out of the catalog totals, ranking and mix. The app's ≥N-windows gate now reads testable windows. Horizon = max(asked, lead + coverage) for every run (`effective_horizon`), so recost/rebuild inherit it. Products explained by the last run no longer re-trigger the nightly test.
- [x] As-of-cutoff catalog dropped price columns. **Fixed:** price, price_mixed, price_listed, on_promotion kept.
- [x] Scoring filtered units_in_stock > 0. **Fixed:** `_scoreable_days` uses censoring's rule: blank = in stock, sell-out days graded, only UNAVAILABLE days left out.
- [x] Scoring dropped sold-out days training keeps. **Fixed:** same.
- [x] Calibration refits leaked `related` and dropped force_route / pool_cohesion. **Fixed:** build_entry cuts relatives to its own `today`; the calibration call passes force_route and pool_cohesion.
- [x] "Re-test now" sent no combos/holding. **Fixed:** the tab calls App's triggerBacktest (with its horizon / min-history settings). The server endpoints also default to the pairs in use and the session holding rate rather than 14/30/25%.

Tests: engine_statsforecast/test_backtest_correctness.py.

## Batch 10. Backtest realism (done 2026-09-29)
- [x] Test windows forecast with events=[]. **Fixed:** `_window_events` builds each window's promotions (runs of on_promotion, with the discount the prices show) and price changes (temporary / permanent) from its recorded price, in the app's event shapes.
- [x] Shelf price changes didn't reach the economics. **Fixed:** runs value windows at today's price (sent with the costs); a price that moves >1% re-prices the stored run after a refit (`_reprice_backtest_if_price_moved`, recost, no refit). Recost now stores the re-priced windows.
- [x] Cost: dashboard cost used for every window. **Decided with Z (2026-09-29):** the money side uses TODAY's price and TODAY's cost (the dashboard's, else the latest cost in the data), so profit per unit is one that exists now. Using each window's old cost against today's price mixed two eras and overstated what a stockout costs. The units short / carried still come from each window's real sales.

Tests: engine_statsforecast/test_backtest_realism.py.

## Batch 11. Shopify (done 2026-09-29)
- [x] fetch_catalog_meta was ~30,000 points. **Fixed:** products(first: 8) × variants(first: 20) ≈ 515 points; a product with more variants is finished with its own query (≈ 304). `estimate_query_cost` + tests in test_shopify_source.py.
- [ ] NOTE (not in the audit, not changed): by the same estimate the ORDERS query (30 orders × 25 line items with their money objects) is ≈ 6,000 points. Check against a dev store before relying on Shopify imports.

## Batch 12. Smaller logic fixes (done 2026-09-29)
- [x] Grouping tab auto-classified on mount and saved AI categories as user-set. **Fixed:** it waits for the server's groups and skips products that already have a category; AI tags are saved with `source: "ai"`, which only fills empty (or earlier-AI) attributes, never counts as set by hand (`attrs_ai`), and yields to the file's values on the next import. Tests: test_attributes_source.py.
- [x] bundles.py added a kit's units to every row of the component that day. **Fixed:** once per (day, component).
- [x] censoring.py: last reading carried forever; dead guard; sales day with 0 tracked hours dropped. **Fixed:** a reading vouches for 26h past it (or until the next reading); a day needs 12 watched hours, and a partly watched day is scaled from what was seen; a day that sold something below 10% availability is kept as a floor instead of dropped.
- [x] Holding-cost wizard matched "art" inside "parts". **Fixed:** whole-word matching (plural allowed; stems marked with *). Tests: tests/holding.test.mjs.

## Added 2026-09-29 (after batches 6-12)
- [x] Live accuracy showed a paused store's weeks while spreadsheets were loaded, and graded every week against the most recently used store. **Fixed:** every week is filed under its store (`store` on the entry, part of its id; older weeks claimed by the store). The tab shows the loaded store's record only; with spreadsheets loaded it says the store is paused and its record is kept. Each store's weeks are graded against its own sales, paused or not. Nothing is sealed for spreadsheets while a store is saved. "Start over" clears only the loaded store's record. Tests: test_livelog_stores.py.
- [x] Live accuracy never graded a product that sold NOTHING in the week (the sales report only names products that sold, so a zero-seller was "unknown" and stayed pending for ever). Week of 9/20 showed 10 of 36 graded, 50% in band; with the zero-sellers it's 35 graded, 34% in band, said 137 / sold 55. **Fixed:** the store's own product list counts as known, so a listed product with no sales scores 0; a store that can't be read still grades nothing. "said X · sold Y" now compares the same (graded) products. Tests: test_livelog_stores.py.
- [x] My store-tagging change sealed a second copy of the 9/27 week (new ids carry the store, and the duplicate check only compared ids). **Fixed:** the check compares store + product + week + horizon; existing duplicates are removed on the next load, keeping the week sealed first (Sunday).
- [x] A product that left the store (8921959, "2 Clearance Jersey — Regular": last seen by Square 9/28) sat as "ready" for ever, counted in the week, and clicking it opened a forecast that doesn't exist. **Fixed:** shown faded as "not in store", left out of the week's figures, not clickable. "ready" now reads "grading soon" / "grades in Nd"; the table note says Expected is the forecast, not sales.
- [x] Holidays panel reworked (2026-09-30). Each holiday shows its NEXT occurrence only when it starts inside the forecast horizon the page asked for (1/2/3 months, or any other: `_holidays_view(report, horizon_days)`, per request); otherwise the most recent one that has ENDED, as it sold. Cards restructured (title + badge, bars, 3 figures, busiest day, where the pattern comes from, "View all years" list newest first with units, % vs prior year (complete, same-length years only) and each year's busiest day). A holiday inside another's stretch is named with it ("Valentine's Day & Super Bowl"). "No real change" → "No significant change detected, or not yet seen", at the bottom. "Steadied by" now shows the split: own years' weight on the core days vs similar products', and each similar product's share (holiday_shape.pool/levels `ownShare`, `peerShares`); wording no longer claims "each counting equally" (they're capped at a typical product's weight).
- [x] Closed days (Fleet overview → Closed days): checked end to end. Already left out of every product's training history, the backtest and grouping, and forecast at 0 ahead (verified: past closed days absent from training, closed days ahead 0, next day normal). **Added:** the product chart marks each closed day, past and future, with an orange dot on the axis (hover for the date) and a key line under the chart (`closedDays` on /api/forecast).
- [x] Unit cost / fees / holding edits re-price the finished test silently. **Fixed:** a notice at the bottom ("Re-pricing…" → "Protection levels re-priced", or "Saved" when there's no finished test yet).

## Before a first client (plan and check with Z first)
- [ ] Move Groq calls behind the backend (key out of the browser bundle): ai.js, AiDrawer.jsx, CategorizePanel.jsx, modals.jsx AI parser.
- [ ] Square multi-location (square_source.py reads one; the cousin's store has 2).

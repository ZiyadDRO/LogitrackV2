import React, { useState } from 'react';
import { ClosedDaysButton } from './ClosedDays';
import { urgencyLevel, URGENCY_STYLES, URGENCY_STYLES_LM, formatDate,
         skuState, SKU_STATES, stockIsCounted, todayMs } from '../lib/helpers';
import { Tip } from './common';
import { loadStorage, saveStorage } from '../lib/storage';
import { FleetAlertBanner } from './Sidebar';
import { terminal, projFill, projFillSoft, MONO, SANS } from '../lib/theme';

// ─── FLEET SUMMARY VIEW ───────────────────────
// ─── FLEET · REORDER SCHEDULE ──────────────────────────────────────────────────
// A backend scorecard status -> the fleet's key for it. One-to-one now: the fleet
// shows the scorecard's five statuses under the scorecard's own names, rather than
// re-deciding on a different axis and calling the result by the same words.
export const SCORE_STATUS = {
  "Stockout risk": { key: "stockout",  label: SKU_STATES.stockout.label },
  "Dead stock":    { key: "dead",      label: SKU_STATES.dead.label },
  "Overstocked":   { key: "overstock", label: SKU_STATES.overstock.label },
  "Reorder due":   { key: "reorder",   label: SKU_STATES.reorder.label },
  "Healthy":       { key: "healthy",   label: SKU_STATES.healthy.label },
};

/* The backtest headline that used to sit up here ("90% stayed in stock · protection
 * tuned per product") is gone. The number was real — the share of replayed lead-time
 * windows where the buffer covered actual demand — but nothing on the card said it
 * came from a replay of past data rather than a measurement of the business, so it
 * read as a claim about how the shelves are doing right now. It also arrived with no
 * confidence interval, while the same figure on the Backtest tab carries one, plus
 * the target it was aiming at and the per-product breakdown that makes it mean
 * anything. A number that needs that much context doesn't belong in a corner.
 */

/* LAYOUT NOTE — why this is one grid and not two panels.
 *
 * The old page drew the same fact twice. "Needs attention" was a list of products
 * ordered by when they are due; "Upcoming reorders" was a bar chart of money by
 * week. Both are the same reorder dates — one read down the products, the other
 * summed across them — and neither could tell you which products were inside a
 * given bar, or when a given product's money actually lands.
 *
 * So they are one grid: products down, the same eight weeks across, and the bar
 * chart is the column total sitting under its own column. Read a row to see when a
 * product comes due and when it comes round again; read a column to see what is in
 * that week. Nothing was dropped in the merge — every row still carries its status
 * pill and its action button, and the totals below are the same numbers the chart
 * drew, in the same order.
 */
/* ── Projected reorder cycles ──────────────────────────────────────────────────
 *
 * Every repeat used to be drawn as the same quantity at a fixed interval: one
 * order's size divided by one flat daily average, forever. That is a fair argument
 * that a second cycle EXISTS, and it was wrong about everything else. A product
 * whose forecast doubles by month three showed the same order in week 8 as in
 * week 2, and its reorder date never moved — so the one part of this page that
 * calls itself a purchasing plan quietly stopped reading the forecast after the
 * first order.
 *
 * This walks the real curve, and applies the ENGINE'S order formula at each cycle
 * rather than a lookalike of it:
 *
 *     target      = demand across THAT cycle's coverage window + safety stock
 *     posAtDel    = inventory position when the delivery lands
 *     order       = max(0, target − posAtDel)
 *
 * Cycle 1 is not simulated. It is taken verbatim from the engine, so this table and
 * the product page can never disagree about the order you are actually about to
 * place; the simulation starts from the shelf position that order leaves behind.
 *
 * Returns [{ day, qty, est }] in days from the same origin as `firstDay`. `est`
 * marks a cycle whose coverage window ran off the end of the forecast and had to
 * lean on a flat tail rate — the UI says so rather than presenting it as measured.
 */
export function projectCycles({ curve, stock, firstDay, firstQty, lead, coverage,
                                safety, horizonDays, dataAge = null, maxCycles = 12 }) {
  if (!Array.isArray(curve) || curve.length < 14 || !(lead > 0) || !(coverage > 0)) return [];
  /* The curve begins the day after the newest sale on file, which is only today if the
     data is fresh — and nothing re-imports on a schedule. `firstDay` is measured from
     TODAY, so indexing the curve with it directly would read the wrong slice of the
     season by however many days the import is stale. Rather than take an alignment on
     faith, derive it from the points' own timestamps: `lag` is how far back the curve
     starts, and every day offset is shifted by it. Fresh data gives lag 0 and this
     changes nothing. */
  const DAY = 86400000;
  /* Preferred source is the engine's own `dataAgeDays` — the curve starts the day after
     the newest sale, so the offset is exactly that minus one, with no clock arithmetic
     to get wrong across time zones. The timestamp comparison is only a fallback for a
     response that predates that field. */
  let lag;
  /* `Number(null)` is 0, not NaN — and 0 is finite, so a bare isFinite check accepted
     the default and made the fallback below unreachable. Same trap posInt() documents
     in helpers.js for empty strings. Test explicitly for a value first. */
  if (dataAge != null && dataAge !== "" && Number.isFinite(Number(dataAge))) {
    lag = Math.max(0, Math.round(Number(dataAge)) - 1);
  } else {
    const t0 = Number(curve[0]?.x);
    const todayMid = todayMs();   // store's today, on the same UTC-midnight grid as the curve
    lag = Number.isFinite(t0) ? Math.max(0, Math.round((todayMid - t0) / DAY)) : 0;
  }
  const n = curve.length;
  /* Past the forecast we hold the last four weeks' rate flat — the mildest assumption
     available, and one the caller is told it is leaning on. */
  const tail = curve.slice(-28).reduce((t, p) => t + (Number(p.y) || 0), 0) / Math.min(28, n);
  const last = Math.max(0, horizonDays) + lead + coverage + Math.max(0, lag) + 2;
  /* Prefix sums: the reorder test and the order sizing each need a window total on
     every simulated day, and a catalogue of a few hundred products makes that the hot
     loop. Built once per product, every window is then two subtractions. */
  const pre = new Float64Array(last + 1);
  for (let i = 0; i < last; i++) pre[i + 1] = pre[i] + (i < n ? (Number(curve[i].y) || 0) : tail);
  const cum = i => pre[Math.min(Math.max(i + lag, 0), last)];   // i is days from today
  const sum = (a, b) => cum(b) - cum(a);

  const d0 = Math.max(0, firstDay);
  /* Inventory POSITION, not what is on the shelf: on-hand plus everything already on
     order. That distinction is the whole point for a long lead time — a 60-day sea
     line reorders two or three times before its first container ever lands, and a
     simulation that waits for delivery would show it ordering once a quarter. */
  let pos = Math.max(0, stock - sum(0, d0)) + Math.max(0, firstQty);
  const out = [];
  for (let d = d0 + 1; d <= horizonDays && out.length < maxCycles; d++) {
    pos = Math.max(0, pos - sum(d, d + 1));
    const arrive = d + lead;
    const posAtDel = pos - sum(d, arrive);
    // Order on the day the position can no longer cover the wait plus the buffer. A
    // busier stretch pulls this earlier on its own — nothing here is on a fixed cadence.
    if (posAtDel <= safety) {
      const held = Math.max(0, posAtDel);
      const qty  = Math.max(0, Math.round(sum(arrive, arrive + coverage) + safety - held));
      if (qty <= 0) break;            // a cycle that orders nothing never comes round again
      out.push({ day: d, qty, est: arrive + coverage + lag > n });
      pos += qty;
    }
  }
  return out;
}

// The kinds of product the fleet list can show, in the order the boxes appear.
const FLEET_TYPES = ["stockout", "reorder", "overstock", "uncounted", "dead", "healthy", "unrated"];
const DEFAULT_FLEET_TYPES = ["stockout", "reorder", "overstock", "uncounted"];
const ATTENTION_TYPES = new Set(["stockout", "reorder", "overstock", "uncounted", "dead"]);
const FLEET_FILTER_KEY = "logitrack_fleet_filter";

export function FleetBento({ skuForecasts, getParams, leadTimeOf = null, openPOs, onSelectSku, lm, onExportFleet, onExportAll, exporting, scorecardRows = [], onEditCosts = null, onClosedDays = null, closedCount = 0 }) {
  const T = terminal(lm);
  const PROJ_FILL = projFill(T);        // bars — texture reads at size
  const PROJ_CHIP = projFillSoft(T);    // chips — texture behind digits, so lighter
  const fmt = n => "$" + Math.round(n).toLocaleString();
  const scoreBySku = Object.fromEntries((scorecardRows || []).map(r => [r.skuId, r]));
  // Lead time travels in, because the reorder bands are a fraction of it — a
  // 60-day sea line and a 3-day air line do not share a "due soon".
  // The PLANNED lead time when App provides it (the typed field is only one input).
  const leadOf = id => (leadTimeOf ? leadTimeOf(id) : (Number(getParams(id)?.leadTime) || null));
  const stateOf = s => skuState(s, scoreBySku[s.skuId], !!openPOs[s.skuId], leadOf(s.skuId));
  const bucket = s => stateOf(s).key;
  const counts = { stockout:0, reorder:0, overstock:0, dead:0, healthy:0, uncounted:0, unrated:0 };
  /* Which kinds of product the list on the right shows. Chosen by the viewer and
     remembered in this browser; dead stock is off by default because it is a standing
     condition to review occasionally, not work for today. */
  const [show, setShowRaw] = useState(() => {
    const v = loadStorage(FLEET_FILTER_KEY, null);
    return (v && Array.isArray(v.types)) ? { types: v.types, onOrder: !!v.onOrder }
                                         : { types: DEFAULT_FLEET_TYPES, onOrder: false };
  });
  const setShow = next => { setShowRaw(next); saveStorage(FLEET_FILTER_KEY, next); };
  const toggleType = k => setShow({ ...show, types: show.types.includes(k)
    ? show.types.filter(t => t !== k) : [...show.types, k] });
  /* Overlays, counted OVER the statuses rather than instead of them. As buckets they
     stole from the five — a Healthy product with a PO left the healthy count, a young
     one became "new" — so the fleet's totals could never match the scorecard's. */
  const onOrderCount = skuForecasts.filter(s => !!openPOs[s.skuId]).length;
  const newCount     = skuForecasts.filter(s => s.tooNew || s.young).length;
  /* One source of truth for the bar AND the legend, so colours/order always line up.
   * This bar IS the scorecard's distribution: the same five statuses under the same
   * names, plus `unrated` for products the scorecard never classified. Nothing else
   * is a segment, because anything else would take a product out of the status it
   * belongs to and put the two tabs' totals back out of step.
   *
   * No day counts in the labels. They used to read "order ≤5d" / "healthy 21d+",
   * honest only while every product shared one clock. The bands are a fraction of
   * each product's own lead time now — 7 and 21 days on a 14-day line, 30 and 90 on
   * a 60-day sea crossing — so no single number here could be true for the fleet. */
  const segDefs = [
    ["stockout",  T.red,     SKU_STATES.stockout.label],
    ["dead",      T.dead,    SKU_STATES.dead.label],
    ["overstock", T.over,    SKU_STATES.overstock.label],
    ["reorder",   T.blue,    SKU_STATES.reorder.label],
    ["healthy",   T.green,   SKU_STATES.healthy.label],
    // Nobody has counted it and the store reports no level. This was missing from both
    // the bar and the counts, so a catalogue of uncounted products drew an empty bar.
    ["uncounted", T.soft, SKU_STATES.uncounted.label],
    // Products /api/scorecard never classified (no fitted forecast). Shown rather
    // than folded into healthy, so the bar still sums to the catalogue.
    ["unrated",   T.faint,   SKU_STATES.unrated.label],
  ];
  // The colour a status owns, used by the pills AND the grid cells so a red cell and
  // a red legend block always mean the same thing.
  const STATUS_COLOR = { stockout:T.red, dead:T.dead, overstock:T.over, reorder:T.blue,
                         healthy:T.green, uncounted:T.soft, unrated:T.faint };
  skuForecasts.forEach(s => { const k = bucket(s); counts[k] = (counts[k] || 0) + 1; });
  const total = skuForecasts.length || 1;
  /* The stock-position axis (dead / overstocked) only exists once the scorecard has
   * answered. Printing "0 dead stock" off a check that never ran reads as a clean
   * bill of health rather than a pending one, so that line stays quiet until every
   * product has been rated — which is now exactly "no unrated ones left". */
  const stockScored = counts.unrated === 0 && skuForecasts.length > 0;
  /* The headline counts what you have to BUY, not everything that isn't perfect.
   * Dead stock and overstocked are standing conditions, not a purchase you make
   * today; folding them in meant one number moving for two unrelated reasons. */
  const toOrder = counts.stockout + counts.reorder;
  /* "Stockout risk" is not a synonym for overdue — it also holds products whose
     reorder point is still ahead but inside half a lead time, where ordering today
     may already be too late. Labelling the whole bucket "(overdue)" would be wrong
     for those, so the genuinely-late ones are counted out separately instead. */
  const overdueCount = skuForecasts.filter(s => bucket(s) === "stockout"
    && (s.daysUntilReorder ?? 0) < 0).length;

  const provDays = s => (s.daysUntilReorder != null ? s.daysUntilReorder : s.observedRunwayDays);
  const provLow = skuForecasts.filter(s => (s.tooNew || s.young) && !openPOs[s.skuId]
    && provDays(s) != null && provDays(s) <= 10);
  const newSoon = [...provLow].sort((a,b) => (provDays(a) ?? 99) - (provDays(b) ?? 99));
  const newSoonIds = new Set(newSoon.map(s => s.skuId));

  /* `newSoon` is computed first and subtracted here. A young product with a reorder
     date satisfies BOTH filters — it is young AND its timing puts it in "stockout" —
     so it used to be listed twice, once under its hard status and again under
     "New · keep an eye". Two panels hid that; one grid does not. The gentle
     treatment wins, because that is the whole point of the new-product carve-out:
     a product with three weeks of history has not earned a hard "Overdue". */
  const urgent = skuForecasts.filter(s => !openPOs[s.skuId] && ["stockout","dead","overstock","reorder"].includes(bucket(s)))
    .filter(s => !newSoonIds.has(s.skuId))
    .sort((a,b) => (a.daysUntilReorder ?? 999) - (b.daysUntilReorder ?? 999));
  // New SKUs (baseline/young) that look like they'll run low soon — surfaced gently as
  // "consider ordering," never as hard overdue. "Soon" = forecast reorder OR observed
  // runway within ~10 days. orderQty is 0 for baseline, so we only suggest a qty if known.

  let costKnown = false, invValue = 0, invKnown = false, invUncounted = 0;

  /* How old the sales behind all of this are. Nothing re-imports on a schedule, so this
     page can sit open for days looking perfectly live: the countdowns tick, the week
     columns slide forward, and every number is still built on whatever was last
     imported. The dates advance; the demand does not. Saying so is the difference
     between a plan and a plan that quietly expired.
     Freshest product wins — one dormant SKU whose last sale was in March should not
     make an otherwise current catalog look abandoned. */
  const ages = skuForecasts.map(s => Number(s.dataAgeDays)).filter(Number.isFinite);
  const dataAge = ages.length ? Math.min(...ages) : null;
  const vintage = dataAge == null ? null
    : dataAge <= 1 ? { text: "Sales data is current.", stale: false }
    : { text: `Sales data is ${dataAge} days old. Nothing has been imported since.`,
        stale: dataAge >= 7 };

  /* TODAY, stated separately from everything else on this page.
     The sales sync runs at 00:15 and the training data stops at the last COMPLETE day,
     so every figure above is current through yesterday and says nothing about the hours
     since. That gap is invisible: a shop that traded all day looks identical to one that
     did not. The hourly tick knows the answer, so the page says it out loud rather than
     leaving the reader to assume the silence means zero. */
  const todayRows = skuForecasts.map(s => s.todaySales).filter(t => t && t.forToday && !t.unsupported);
  const soldToday = todayRows.reduce((n, t) => n + (Number(t.units) || 0), 0);
  const todaySkus = todayRows.filter(t => (Number(t.units) || 0) > 0).length;
  const todayStale = todayRows.length > 0 && todayRows.every(t => t.stale);
  const todayText = todayRows.length === 0 ? null
    : soldToday > 0
      ? `${soldToday.toLocaleString()} sold today across ${todaySkus} product${todaySkus === 1 ? "" : "s"} (not in the figures below yet).`
      : "Nothing sold yet today.";

  // Upcoming reorders by week — the forecast turned into a purchasing schedule (cash, or
  // units when cost is unknown). This is the tool's unique value: a forward buying plan.
  const WEEKS = 8;
  const weekly = Array.from({ length:WEEKS }, () => ({ next:0, repeat:0, nextUnits:0, repeatUnits:0, est:false }));
  /* The per-product week matrix that used to live here (skuId -> eight {firm, proj}
     cells) is gone with the merged grid. The two panels below need the SUMS by week
     and the products by due date; neither needs every product crossed with every
     week, which is what made that structure — and the 240-cell grid it fed — the
     heaviest thing on the page. */
  /* Calendar weeks, Sunday–Saturday, not rolling 7-day blocks counted off today. A buyer
     plans against the week on the wall: on a Friday, "due in 3 days" is next week's
     problem, and a rolling window would have filed it under this one.
     Whole-day integer arithmetic, deliberately — `dow` is how far into the current week
     today already is, so a reorder `d` days out sits `dow + d` days after Sunday and
     `floor(/7)` is its week. Dividing millisecond timestamps would put an hour of DST
     between a date and the week it belongs to. */
  const today = new Date(); today.setHours(0,0,0,0);
  const dow = today.getDay();                                   // 0 = Sunday
  const weekOf = d => Math.floor((dow + Math.round(d)) / 7);     // negative = already past
  const addDays = n => { const x = new Date(today); x.setDate(x.getDate() + n); return x; };
  const weekStart = i => addDays(i * 7 - dow);
  const weekEnd   = i => addDays(i * 7 - dow + 6);
  const fmtDay = dt => dt.toLocaleDateString(undefined, { month:"short", day:"numeric" });
  const weekRange = i => `${fmtDay(weekStart(i))} - ${fmtDay(weekEnd(i))}`;
  /* The header form. "Sep 6 – Sep 12" wraps in a narrow column and the second line
     reads as a stray number, so the month is only repeated when the week actually
     crosses one. */
  const weekRangeShort = i => {
    const a = weekStart(i), b = weekEnd(i);
    return a.getMonth() === b.getMonth()
      ? `${fmtDay(a)}-${b.getDate()}`
      : `${fmtDay(a)} - ${fmtDay(b)}`;
  };

  /* ONE purchasing number on this page, and the schedule is it. There used to be a
     second one — a "To order now" tile — and the two could never agree, because they
     were not measuring the same thing: the tile counted STATUS (every product the
     scorecard calls "Stockout risk" or "Reorder due") while the schedule is a CALENDAR
     of reorder dates. The scorecard's bands are fractions of each product's own lead
     time (risk = lt/2, due = lt × 1.5, see helpers.reorderBands), so a 60-day sea line
     is "Reorder due" with a reorder date up to 90 days out. Sat next to a bar labelled
     "now", that reads as a contradiction to anyone looking at the page, and no bucketing
     rule can fix it — the two numbers answer different questions. So the dollars are
     stated once, on the calendar, and the status view stays a COUNT of products on the
     fleet-health strip.

     Each product contributes two kinds of cell, and the difference matters enough to draw:

       `firm`   — the product's NEXT reorder, on the date the forecast actually gives it.
       `proj`   — the same product coming round again, projected at its own cycle length
                  (one order's quantity ÷ its daily demand = the days of cover it buys).

     Without the second part the schedule quietly under-reports the back half of its own
     horizon. It draws each product once, but the median product here reorders about
     every 38 days, so inside eight weeks most of them come round again — and the last
     bars showed a few hundred dollars where the real figure was tens of thousands. It
     was not a stale number; the second cycle was never drawn at all, and no amount of
     live sales data would have filled it in.

     Projection stops where it stops being a forecast: overstocked and dead stock are
     being cleared, not rebought, so they contribute their next order and nothing after
     it. Later cycles are simulated against each product's own forecast curve rather
     than repeated at a flat rate (see projectCycles), so they move and resize with the
     forecast — but they are still projections, not dated orders, which is why they stay
     hatched and outlined rather than solid. Committed and projected should never read
     as the same money.

     Products with no reorder date can't be placed on a calendar and are left out rather
     than swept into the first bar to make a total come out even. */
  const REPEATABLE = s => s !== "overstock" && s !== "dead";
  skuForecasts.forEach(s => {
    const prm = getParams(s.skuId); const c = Number(prm.unitCost); const f = Number(prm.fees) || 0;
    const stk = Number(prm.stock) || 0;
    // Only stock someone actually counted. A placeholder (50 units, source "unknown") times
    // cost is money nobody has seen: 200 uncounted products at $20 read as $200,000.
    if (c > 0 && stockIsCounted(prm)) { invValue += stk * c; invKnown = true; }
    else if (c > 0) invUncounted += 1;
    if (openPOs[s.skuId] || !s.orderQty) return;
    /* stateOf, not s.daysUntilReorder — skuState falls back to the scorecard's copy of
       the date, so reading the raw field made products vanish from the plan. */
    const state = stateOf(s);
    const days = state.days;
    if (days == null) return;
    const cost = c > 0 ? s.orderQty * (c + f) : 0;
    if (c > 0) costKnown = true;

    const w = Math.max(weekOf(days), 0);   // anything already late joins the first bar
    if (w < WEEKS) {
      weekly[w].nextUnits += s.orderQty; weekly[w].next += cost;
    }

    // Repeats, simulated against this product's own forecast curve. Overstocked and
    // dead stock are being cleared rather than rebought, so they contribute their
    // next order and nothing after it.
    if (!REPEATABLE(state.key)) return;
    projectCycles({
      curve: s.chartDataFuture, stock: stk, firstDay: days, firstQty: s.orderQty,
      lead: leadOf(s.skuId) || 14, coverage: Number(prm.coverage) || 30,
      safety: Number(s.safetyStock) || 0, horizonDays: WEEKS * 7,
      dataAge: s.dataAgeDays,
    }).forEach(({ day, qty, est }) => {
      const wr = weekOf(day);
      // A repeat that would have fallen in a past week is history, not a plan.
      if (wr < 0 || wr >= WEEKS) return;
      weekly[wr].repeatUnits += qty; weekly[wr].repeat += (c > 0 ? qty * (c + f) : 0);
      // `est` — a cycle whose coverage window ran past the forecast — is now carried
      // for the week rather than per product, because the chart is per week.
      if (est) weekly[wr].est = true;
    });
  });
  const chartVal = costKnown;
  // One accessor for "the number this chart is currently drawing", so the bars, the
  // totals and the tile can't disagree about whether we're in dollars or units.
  const valOf   = x => chartVal ? x.next   : x.nextUnits;
  const repOf   = x => chartVal ? x.repeat : x.repeatUnits;
  const totalOf = x => valOf(x) + repOf(x);
  const maxBar  = Math.max(...weekly.map(totalOf), 1);
  const anyRepeat = weekly.some(x => repOf(x) > 0);
  const horizonTotal  = weekly.reduce((t, x) => t + totalOf(x), 0);
  const horizonRepeat = weekly.reduce((t, x) => t + repOf(x), 0);
  const anyEst = weekly.some(x => x.est);

  // ── presentation helpers ────────────────────────────────────────────────────
  /* SURFACE RULES — square, and bordered hard enough to see.
   *
   * The old page drew every panel with a hairline at ~11% white and a 16px radius,
   * which on a near-black ground is a card you infer rather than one you see. Three
   * changes, applied everywhere so nothing is half-converted:
   *
   *   EDGE   1.5px at the STRONGER line token, not 1px at the faint one.
   *   RULE   the same weight inside tables — under a column header, above a totals
   *          row — so the bands of a table separate without needing a fill.
   *   radius 0 on every surface: cards, buttons, pills, chips, bars, segments.
   *
   * Colour is untouched. The palette stays the dark Terminal set it already was —
   * near-black ground, amber as the single accent, and the five status hues shared
   * with the scorecard. */
  const EDGE  = `2px solid ${T.line2}`;
  const RULE  = `2px solid ${T.line2}`;
  const mono  = { fontFamily:MONO, fontVariantNumeric:"tabular-nums" };
  const card  = { background:T.panel, border:EDGE, borderRadius:0, padding:"16px 20px", boxShadow:T.shadow };
  const cap   = { fontSize:13, letterSpacing:".07em", textTransform:"uppercase", color:T.dim, fontWeight:600 };
  const pill  = (bg, fg, txt, title) => (
    <span title={title} style={{ ...mono, background:bg, color:fg, fontSize:14, fontWeight:600,
      padding:"3px 9px", borderRadius:0, whiteSpace:"nowrap" }}>{txt}</span>);
  const btnGhost = { ...mono, fontSize:15, fontWeight:500, padding:"8px 13px", borderRadius:0,
    cursor: exporting ? "default" : "pointer", background:"transparent", color:T.soft,
    border:`2px solid ${T.line2}`, whiteSpace:"nowrap", opacity: exporting ? 0.6 : 1 };
  const btnSolid = { ...btnGhost, background:T.btnBg, color:T.btnFg, border:"2px solid transparent", fontWeight:600 };
  const seg = (n, color) => n > 0 ? <div key={color+n} style={{ width:(n/total*100)+"%", background:color }} /> : null;

  const money = n => chartVal ? fmt(n) : n.toLocaleString() + " units";

  /* "add unit costs …" was already written in the two places where the absence of a
     cost is actually felt — the chart can't show money, the inventory tile can't
     show a value. It just wasn't clickable. */
  const addCostsLink = label => onEditCosts
    ? <button onClick={onEditCosts} style={{ background:"transparent", border:"none", padding:0,
        font:"inherit", color:T.amber, cursor:"pointer", textDecoration:"underline",
        textUnderlineOffset:3, textDecorationColor:`${T.amber}66` }}>{label}</button>
    : <>{label}</>;

  const actionBtn = (txt, solid = true) => (
    <div title={txt} style={{ ...mono, fontSize:14.5, fontWeight:600, padding:"7px 14px", borderRadius:0,
      textAlign:"center", whiteSpace:"nowrap", flexShrink:0,
      background: solid ? T.btnBg : "transparent", color: solid ? T.btnFg : T.soft,
      border: solid ? "2px solid transparent" : `2px solid ${T.line2}` }}>{txt}</div>);

  /* The list shows EVERY product of the kinds ticked above it, and scrolls. It used to
     stop at six and send you to "the Scorecard" for the rest — a tab that no longer
     exists. Most urgent first: status, then how soon the reorder falls due. */
  const RANK = { stockout:0, reorder:1, uncounted:2, overstock:3, dead:4, unrated:5, healthy:6 };
  const listed = skuForecasts
    .filter(s => show.types.includes(bucket(s)))
    .filter(s => show.onOrder || !openPOs[s.skuId])
    .filter(s => !newSoonIds.has(s.skuId))
    .sort((a,b) => (RANK[bucket(a)] ?? 9) - (RANK[bucket(b)] ?? 9)
                || (a.daysUntilReorder ?? 999) - (b.daysUntilReorder ?? 999));
  const onlyWork = show.types.every(k => ATTENTION_TYPES.has(k));

  /* CHART — the row is full width and the plot is more than twice as tall.
     Squeezed into a half-width panel these eight bars were 40px apart with a 76px
     ceiling, so a $95k week and a $61k week differed by about a centimetre and the
     date labels had to be abbreviated past reading. Given the whole row, the bars
     get room to differ and the labels get to be dates. */
  const BAR_MAX = 190;

  return (
    <div style={{ background:T.bg, color:T.ink, minHeight:"100%", padding:"20px 24px 36px", fontFamily:SANS }}>
      <style>{`.lt-row:hover{background:${T.sunken};}`}</style>

      {/* ── header ───────────────────────────────────────────────────────── */}
      <div style={{ display:"flex", alignItems:"flex-start", justifyContent:"space-between", marginBottom:14, gap:16 }}>
        <div>
          <div style={{ fontSize:28, fontWeight:700, letterSpacing:"-.03em" }}>Fleet overview</div>
          <div style={{ fontSize:16, color:T.soft, marginTop:5 }}>
            <b style={{ ...mono, color:T.ink, fontWeight:600 }}>{skuForecasts.length}</b> SKUs ·{" "}
            <b style={{ ...mono, color:T.ink, fontWeight:600 }}>{toOrder}</b> to order</div>
        </div>
        <div style={{ display:"flex", alignItems:"center", gap:9 }}>
          {/* A store setting, not an export or a data sheet: its own look, set apart. */}
          {onClosedDays && (<>
            <ClosedDaysButton onClick={onClosedDays} count={closedCount} lm={lm} />
            <span aria-hidden="true" style={{ width:2, alignSelf:"stretch", background:T.line, margin:"2px 3px" }} />
          </>)}
          {onEditCosts && (
            <button onClick={onEditCosts} style={btnGhost}
              title="What you pay per unit, plus any per-unit fees. Drives margin, profit grade and protection levels.">
              Costs &amp; fees
            </button>
          )}
          <button onClick={onExportFleet} disabled={!!exporting} style={btnGhost}
            title="Download all SKUs: forecasts, daily projections and monthly breakdowns (Excel)">
            {exporting === "fleet" ? "Preparing…" : "Export all SKUs"}
          </button>
          <button onClick={onExportAll} disabled={!!exporting} style={btnSolid}
            title="One workbook: fleet, scorecard, suppliers and order history">
            {exporting === "all" ? "Preparing…" : "Download everything"}
          </button>
        </div>
      </div>

      {/* ── row 1 · fleet health + KPIs | needs attention ────────────────── */}
      {/* The four KPI cards moved UP here, into the left column under fleet health,
          as a 2×2. They used to sit in a row of their own at the very bottom, which
          left the left column ending about 450px above the right one — a black void
          beside the attention list, and a fifth band of page below the chart. Now the
          column carries health on top and the four figures beneath it, and it runs to
          the same depth as the list it sits next to. Nothing was added or removed;
          the same four cards are in the space that was empty. */}
      <div style={{ display:"grid", gridTemplateColumns:"minmax(0,1fr) minmax(0,1.2fr)", gap:12, alignItems:"stretch" }}>

        <div style={{ display:"flex", flexDirection:"column", gap:12, minWidth:0 }}>
        <div style={{ ...card, display:"flex", flexDirection:"column" }}>
          <div style={cap}>Fleet health · {skuForecasts.length} SKUs</div>
          <div style={{ ...mono, display:"flex", alignItems:"baseline", gap:10, marginTop:8 }}>
            <span style={{ fontSize:54, fontWeight:600, letterSpacing:"-.045em", lineHeight:1 }}>{toOrder}</span>
            <span style={{ fontFamily:SANS, fontSize:20.5, color:T.soft }}>to order</span>
          </div>
          <div style={{ fontSize:15.5, color:T.soft, marginTop:12 }}>
            {toOrder === 0 ? "Nothing to order"
              : `${counts.stockout} at risk${overdueCount > 0 ? ` (${overdueCount} overdue)` : ""}`
                + (counts.reorder > 0 ? ` · ${counts.reorder} due soon` : "")}
            {stockScored && counts.overstock > 0 ? ` · ${counts.overstock} overstocked` : ""}
            {stockScored && counts.dead > 0 ? ` · ${counts.dead} dead stock` : ""}
            {onOrderCount > 0 ? ` · ${onOrderCount} on order` : ""}
            {newCount > 0 ? ` · ${newCount} new${provLow.length ? ` (${provLow.length} running low)` : ""}` : ""}
          </div>
          {/* Square ends, and a 2px surface gap between segments rather than a stroke
              around each — a gap separates without adding a second colour. */}
          <div style={{ display:"flex", height:12, overflow:"hidden", marginTop:12, gap:2 }}>
            {segDefs.map(([k,c]) => seg(counts[k], c))}
          </div>
          <div style={{ display:"flex", flexWrap:"wrap", gap:"7px 18px", marginTop:12, fontSize:15, color:T.soft }}>
            {segDefs.filter(([k]) => counts[k] > 0).map(([k,c,label]) => (
              <span key={k} title={SKU_STATES[k].help}
                style={{ display:"flex", alignItems:"center", gap:7, cursor:"help" }}>
                <span style={{ width:10, height:10, background:c, flexShrink:0 }} />
                <b style={{ ...mono, color:T.ink, fontWeight:600 }}>{counts[k]}</b> {label}
                {k === "stockout" && overdueCount > 0 &&
                  <span style={{ color:T.faint }}>({overdueCount} overdue)</span>}
              </span>
            ))}
          </div>
          {vintage && (
            <div style={{ fontSize:14, marginTop:16, paddingTop:14, borderTop:`2px solid ${T.line}`, display:"flex", alignItems:"center", gap:7,
                          color: vintage.stale ? T.over : T.faint }}>
              <span style={{ width:7, height:7, flexShrink:0, background: vintage.stale ? T.over : T.green }} />
              {vintage.text}
              {todayText && (
                <span style={{ marginLeft: 8, color: todayStale ? T.over : T.faint }}>
                  {todayText}{todayStale ? " (a check was missed)" : ""}
                </span>
              )}
            </div>
          )}
        </div>

        {/* The four figures, two by two, filling the column rather than opening a
            fifth band at the foot of the page. `1fr` rows so the pair in each row is
            the same height whatever their subtext does. */}
        <div style={{ display:"grid", gridTemplateColumns:"repeat(2,1fr)", gridAutoRows:"1fr", gap:12, flex:1 }}>

        <div style={{ ...card, padding:"14px 16px", display:"flex", flexDirection:"column", justifyContent:"center" }}>
          <div style={cap}>Due this week</div>
          <div style={{ ...mono, fontSize:28, fontWeight:500, letterSpacing:"-.03em", marginTop:7, lineHeight:1 }}>
            {chartVal ? fmt(totalOf(weekly[0])) : totalOf(weekly[0]).toLocaleString()}
          </div>
          <div style={{ fontSize:14.5, marginTop:6, color:T.soft, fontWeight:500 }}>overdue + due by {fmtDay(weekEnd(0))}</div>
          {chartVal && horizonRepeat > 0 &&
            <div style={{ fontSize:14, marginTop:3, color:T.faint }}>
              {fmt(horizonRepeat)} of the {WEEKS}-week total is projected repeats
            </div>}
        </div>

        <div style={{ ...card, padding:"14px 16px", display:"flex", flexDirection:"column", justifyContent:"center" }}>
          <div style={cap}>Inventory value</div>
          <div style={{ ...mono, fontSize:28, fontWeight:500, letterSpacing:"-.03em", marginTop:7, lineHeight:1 }}>{invKnown ? fmt(invValue) : "-"}</div>
          <div style={{ fontSize:14.5, marginTop:6, color:T.soft, fontWeight:500 }}>
            {invKnown
              ? (invUncounted ? `counted stock only (${invUncounted} product${invUncounted === 1 ? "" : "s"} not counted)` : "stock on hand")
              : invUncounted ? "count stock to value it" : addCostsLink("add unit costs to value stock")}
          </div>
          {invKnown && onEditCosts && (
            <div style={{ fontSize:14, marginTop:4 }}>{addCostsLink("Edit costs & fees")}</div>
          )}
        </div>

        <div style={{ ...card, padding:"14px 16px", display:"flex", flexDirection:"column", justifyContent:"center" }}>
          <div style={cap}>Healthy</div>
          <div style={{ ...mono, fontSize:28, fontWeight:500, letterSpacing:"-.03em", marginTop:7, lineHeight:1, color:T.green }}>{counts.healthy}</div>
          <div style={{ fontSize:14.5, marginTop:6, color:T.soft, fontWeight:500 }}>{Math.round(counts.healthy/total*100)}% of catalog</div>
        </div>

        <div style={{ ...card, padding:"14px 16px", display:"flex", flexDirection:"column", justifyContent:"center" }}>
          <div style={cap}>Total SKUs</div>
          <div style={{ ...mono, fontSize:28, fontWeight:500, letterSpacing:"-.03em", marginTop:7, lineHeight:1, color:T.amber }}>{skuForecasts.length}</div>
          <div style={{ fontSize:14.5, marginTop:6, color:T.soft, fontWeight:500 }}>forecast &amp; protected</div>
        </div>
        </div>
        </div>

        {/* The one card carrying work gets the accent edge — a 3px amber rule down its
            left side, the same mark the expanded supplier row uses. It appears only
            when there is actually something to do, so it means "here", not "this is
            the second panel". */}
        <div style={{ ...card, padding:0, display:"flex", flexDirection:"column",
          borderLeft: urgent.length > 0 ? `3px solid ${T.amber}` : EDGE }}>
          <div style={{ display:"flex", alignItems:"baseline", justifyContent:"space-between", gap:12,
            padding:"16px 20px 12px", borderBottom:RULE }}>
            <div style={{ fontSize:20.5, fontWeight:600, letterSpacing:"-.02em" }}>
              {onlyWork ? "Needs attention" : "Products"}
            </div>
            <div style={{ ...mono, fontSize:16.5, fontWeight:600, color: listed.length && onlyWork ? T.amber : T.faint }}>
              {listed.length}
            </div>
          </div>

          {/* What to list. Counts are the whole catalogue's, so an unticked box still
              tells you how many of that kind there are. */}
          <div style={{ display:"flex", flexWrap:"wrap", gap:"6px 14px", padding:"10px 20px", borderBottom:RULE }}>
            {FLEET_TYPES.map(k => (
              <label key={k} title={SKU_STATES[k].help}
                style={{ display:"flex", alignItems:"center", gap:6, fontSize:14, cursor:"pointer",
                  color: show.types.includes(k) ? T.ink : T.faint, userSelect:"none" }}>
                <input type="checkbox" checked={show.types.includes(k)} onChange={() => toggleType(k)}
                  style={{ accentColor:T.amber, margin:0, cursor:"pointer" }} />
                <span style={{ width:9, height:9, background:STATUS_COLOR[k], flexShrink:0 }} />
                {SKU_STATES[k].title}
                <span style={{ ...mono, color:T.faint }}>{counts[k] || 0}</span>
              </label>
            ))}
            <label title="Products with an order already on its way. When off, they are hidden whatever their status."
              style={{ display:"flex", alignItems:"center", gap:6, fontSize:14, cursor:"pointer",
                color: show.onOrder ? T.ink : T.faint, userSelect:"none" }}>
              <input type="checkbox" checked={show.onOrder} onChange={() => setShow({ ...show, onOrder: !show.onOrder })}
                style={{ accentColor:T.amber, margin:0, cursor:"pointer" }} />
              On order
              <span style={{ ...mono, color:T.faint }}>{onOrderCount}</span>
            </label>
          </div>

          <div style={{ maxHeight:560, overflowY:"auto", flex:1, minHeight:0 }}>
          {listed.length === 0 && (
            <div style={{ fontSize:15.5, color:T.soft, padding:"26px 20px" }}>
              {show.types.length === 0 ? "Tick a type above to list those products."
                : onlyWork && urgent.length === 0 ? "Nothing to order. Every product is covered. ✓"
                : "No products of the ticked types."}
            </div>
          )}

          {listed.map((s, i) => {
            const d = s.daysUntilReorder; const b = bucket(s); const sc = scoreBySku[s.skuId];
            const tag = b === "dead" ? pill(T.deadBg, T.deadFg, "Dead stock", SKU_STATES.dead.help)
              : b === "overstock" ? pill(T.overBg, T.overFg, sc?.sellThrough != null ? `Overstocked · ${Math.round(sc.sellThrough * 100)}% sold` : "Overstocked", SKU_STATES.overstock.help)
              : b === "stockout" ? pill(T.redBg, T.redFg, d == null ? "Stockout risk" : d < 0 ? `Overdue ${Math.abs(d)}d` : d === 0 ? "Due today" : `Due in ${d}d`, SKU_STATES.stockout.help)
              : b === "reorder" ? pill(T.blueBg, T.blueFg, d == null ? "Due" : d < 0 ? `Overdue ${Math.abs(d)}d` : d === 0 ? "Due today" : `Due in ${d}d`, SKU_STATES.reorder.help)
              : b === "healthy" ? pill(T.greenBg, T.greenFg, "Healthy", SKU_STATES.healthy.help)
              : b === "uncounted" ? pill(T.sunken, T.soft, "Not counted", SKU_STATES.uncounted.help)
              : pill("transparent", T.faint, "Not rated", SKU_STATES.unrated.help);
            /* Out of season is an overlay, not a status: a Christmas line in September is
               scored by the normal rules and says why it's quiet. */
            const offTag = sc?.offSeason
              ? pill(T.sunken, T.soft, "out of season", "Quiet now the same way it was this time last year, and it sold in the months after. Its forecast follows past years, so orders rise ahead of its season.")
              : null;
            const action = b === "dead" ? "Review / clear"
              : b === "overstock" ? "Reduce orders"
              : b === "uncounted" ? "Count stock"
              : (b === "healthy" || b === "unrated") ? "View"
              : `Order ${s.orderQty?.toLocaleString() ?? "-"}`;
            const plain = !(b === "stockout" || b === "reorder") || !!openPOs[s.skuId];
            return (
              <div key={s.skuId} className="lt-row" onClick={() => onSelectSku(s.skuId)}
                style={{ display:"flex", alignItems:"center", justifyContent:"space-between", gap:14,
                  padding:"11px 20px", cursor:"pointer",
                  borderTop: i === 0 ? "none" : `2px solid ${T.line}` }}>
                <div style={{ minWidth:0 }}>
                  <div style={{ fontSize:16, fontWeight:600, letterSpacing:"-.01em", color:T.ink,
                    whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>{s.skuName}</div>
                  <div style={{ ...mono, fontSize:14, color:T.faint, marginTop:2,
                    whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>
                    {s.skuId} · {SKU_STATES[b].label}
                  </div>
                </div>
                <div style={{ display:"flex", alignItems:"center", gap:10, flexShrink:0 }}>
                  {openPOs[s.skuId] && pill(T.blueBg, T.blueFg, "On order")}
                  {tag}{offTag}
                  {actionBtn(openPOs[s.skuId] ? "View" : action, !plain)}
                </div>
              </div>);
          })}

          {newSoon.length > 0 && (
            <div style={{ ...cap, color:T.amber, padding:"12px 20px 8px", borderTop:RULE }}>New · keep an eye</div>
          )}
          {newSoon.slice(0, 2).map((s, i) => { const d = provDays(s);
            return (
              <div key={s.skuId} className="lt-row" onClick={() => onSelectSku(s.skuId)}
                style={{ display:"flex", alignItems:"center", justifyContent:"space-between", gap:14,
                  padding:"11px 20px", cursor:"pointer", borderTop: i === 0 ? "none" : `2px solid ${T.line}` }}>
                <div style={{ minWidth:0 }}>
                  <div style={{ fontSize:16, fontWeight:600, color:T.ink,
                    whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>{s.skuName}</div>
                  <div style={{ ...mono, fontSize:14, color:T.faint, marginTop:2 }}>
                    {s.skuId} · ~{Math.max(d,0)}d left
                  </div>
                </div>
                <div style={{ display:"flex", alignItems:"center", gap:10, flexShrink:0 }}>
                  {pill(T.amberSoft, T.amber, "New · order soon?")}
                  {actionBtn(s.orderQty > 0 ? `Consider ~${s.orderQty.toLocaleString()}` : "Consider", false)}
                </div>
              </div>);
          })}

          </div>
        </div>
      </div>

      {/* ── row 2 · upcoming reorders, full width ────────────────────────── */}
      <div style={{ ...card, padding:"16px 20px 18px", marginTop:12 }}>
        <div style={{ display:"flex", alignItems:"flex-start", justifyContent:"space-between", gap:20 }}>
          <div>
            <div style={cap}>Upcoming reorders · next {WEEKS} weeks</div>
            <div style={{ fontSize:17.5, color:T.ink, marginTop:5, fontWeight:500 }}>
              {chartVal ? "Purchasing cash coming due. Plan your POs ahead."
                        : <>Units coming due. {addCostsLink("Add unit costs")} to see this as money.</>}
            </div>
          </div>
          <div style={{ textAlign:"right", flexShrink:0 }}>
            <div style={{ ...mono, fontSize:24, fontWeight:600, letterSpacing:"-.03em" }}>
              {chartVal ? fmt(horizonTotal) : horizonTotal.toLocaleString()}
            </div>
            <div style={{ fontSize:14, color:T.faint, marginTop:3 }}>{WEEKS}-week total</div>
          </div>
        </div>

        {/* Plot. Column labels sit under the baseline, values over each bar; the two
            never share a row, so nothing collides however tall a bar gets. */}
        <div style={{ display:"grid", gridTemplateColumns:`repeat(${WEEKS}, minmax(0,1fr))`, gap:14,
          alignItems:"end", marginTop:22 }}>
          {weekly.map((wk, i) => {
            const firm = valOf(wk), proj = repOf(wk), v = firm + proj;
            const h = v > 0 ? Math.max(Math.round(v / maxBar * BAR_MAX), 4) : 0;
            const hProj = v > 0 ? Math.round(proj / v * h) : 0;
            const tip = (i===0 ? `Already overdue, plus everything due by ${fmtDay(weekEnd(0))}` : weekRange(i))
              + `\nNext order: ${money(firm)}`
              + (proj > 0 ? `\nProjected repeat orders: ${money(proj)}` : "");
            return (
              <div key={i} title={tip} style={{ display:"flex", flexDirection:"column", alignItems:"center", gap:7 }}>
                <div style={{ ...mono, fontSize:14.5, fontWeight:600, height:16, whiteSpace:"nowrap",
                  color: i===0 ? T.amber : T.soft }}>
                  {v > 0 ? (chartVal ? fmt(v) : v.toLocaleString()) : ""}
                </div>
                {/* Capped width and centred: a bar that fills its whole column reads as
                    a slab of colour rather than a measured length. */}
                <div style={{ width:"100%", maxWidth:112, height:h, display:"flex", flexDirection:"column" }}>
                  {hProj > 0 && (
                    <div style={{ height:hProj, background:PROJ_FILL,
                      border:`2px dashed ${T.amber}99`, borderBottom:"none", boxSizing:"border-box",
                      marginBottom:2 }} />
                  )}
                  <div style={{ flex:1, background: i===0 ? T.amber : `${T.amber}a8` }} />
                </div>
              </div>);
          })}
        </div>

        <div style={{ borderTop:RULE }} />

        <div style={{ display:"grid", gridTemplateColumns:`repeat(${WEEKS}, minmax(0,1fr))`, gap:14, marginTop:9 }}>
          {weekly.map((_, i) => (
            <div key={i} style={{ ...mono, fontSize:14, textAlign:"center", lineHeight:1.35,
              color: i === 0 ? T.amber : T.dim, fontWeight: i === 0 ? 600 : 400 }}>
              {i === 0 ? <>Overdue + due this week</> : weekRange(i)}
              {i === 0 && <div style={{ fontSize:13, color:T.faint, fontWeight:400 }}>through {fmtDay(weekEnd(0))}</div>}
            </div>
          ))}
        </div>

        {anyRepeat && (
          <div style={{ display:"flex", flexWrap:"wrap", gap:"6px 22px", fontSize:14.5, color:T.soft, marginTop:16,
            paddingTop:13, borderTop:`2px solid ${T.line}` }}>
            <span style={{ display:"flex", alignItems:"center", gap:8 }}>
              <span style={{ width:13, height:13, background:T.amber, flexShrink:0 }} />
              Next order · dated by the forecast
            </span>
            <span style={{ display:"flex", alignItems:"center", gap:8, cursor:"help" }}
              title="Each later cycle is simulated on the product's own forecast: it reorders when stock can't cover the lead time plus buffer, sized to that cycle's coverage window. A busier stretch orders more, and sooner. Hatched because it is a projection, not a dated order; overstocked and dead stock are left out.">
              <span style={{ width:13, height:13, background:PROJ_FILL, border:`2px dashed ${T.amber}99`,
                boxSizing:"border-box", flexShrink:0 }} />
              Projected repeat · the same product coming round again
              {anyEst && <span style={{ color:T.faint }}> (later weeks assume the last four weeks' rate holds)</span>}
            </span>
          </div>
        )}
      </div>

    </div>
  );
}

export function FleetSummaryView({ skuList, skuForecasts, onSelectSku, openPOs, folders, lm, onExportFleet, onExportAll, exporting }) {
  const [groupByFolder, setGroupByFolder] = useState(false);
  const sorted = [...skuForecasts].sort((a, b) => {
    const order = { critical: 0, high: 1, medium: 2, low: 3, on_order: 4, none: 5 };
    return (order[urgencyLevel(a.daysUntilReorder, !!openPOs[a.skuId])] ?? 5) - (order[urgencyLevel(b.daysUntilReorder, !!openPOs[b.skuId])] ?? 5);
  });

  const textMain  = "text-[var(--t-ink)]";
  const textMuted = "text-[var(--t-dim)]";

  const SkuCard = ({ s }) => {
    const hasPO   = !!openPOs[s.skuId];
    const urgency = urgencyLevel(s.daysUntilReorder, hasPO);
    const us      = lm ? URGENCY_STYLES_LM[urgency] : URGENCY_STYLES[urgency];
    const folderEntry = Object.values(folders).find(f => f.skuIds.includes(s.skuId));
    const isOverdue = !hasPO && urgency === "critical" && s.daysUntilReorder != null;
    return (
      <div key={s.skuId} onClick={() => onSelectSku(s.skuId)}
           className={`border rounded-2xl p-4 cursor-pointer hover:brightness-95 transition-all ${us.row}`}>
        {isOverdue && (
          <div className={`flex items-center gap-2 mb-3 pb-3 border-b ${"border-[var(--t-bad-line)]"}`}>
            <span className="text-[16.5px]">🚨</span>
            <span className={`text-[15px] font-bold ${"text-[var(--t-bad)]"}`}>
              {s.daysUntilReorder === 0 ? "REORDER DUE TODAY" : `REORDER OVERDUE: ${Math.abs(s.daysUntilReorder)} day${Math.abs(s.daysUntilReorder) !== 1 ? "s" : ""} ago`}
            </span>
          </div>
        )}
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <div className={`h-2.5 w-2.5 rounded-full shrink-0 ${us.dot}`} />
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <div className={`text-[16.5px] font-bold ${textMain} truncate`}>{s.skuName}</div>
                {folderEntry && !groupByFolder && (
                  <span className={`shrink-0 flex items-center gap-1 text-[14px] px-1.5 py-0.5 rounded-md font-mono border ${"text-[var(--t-dim)] bg-[var(--t-panel)] border-[var(--t-line2)]"}`}>
                    <svg className="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
                    </svg>
                    {folderEntry.name}
                  </span>
                )}
              </div>
              <div className={`text-[14px] font-mono ${textMuted}`}>{s.skuId}</div>
            </div>
          </div>
          <div className="flex items-center gap-6 shrink-0 text-right">
            <div className="text-[15px]">
              <div className={`${textMuted} uppercase tracking-widest text-[13px] flex items-center justify-end gap-0.5`}><Tip text="The latest date you should place a reorder to avoid running out of stock, based on lead time and coverage window.">Order By</Tip></div>
              <div className={`font-bold tabular-nums mt-0.5 ${hasPO ? "text-[var(--t-accent)]" : isOverdue ? "text-[var(--t-bad)]" : urgency === "high" ? "text-[var(--t-warn)]" : textMain}`}>
                {hasPO ? "On Order" : isOverdue ? (s.daysUntilReorder === 0 ? "Today" : `${Math.abs(s.daysUntilReorder)}d overdue`) : s.daysUntilReorder != null ? formatDate(s.daysUntilReorder).split(",")[0] : "-"}
              </div>
            </div>
            <div className="text-[15px]">
              <div className={`${textMuted} uppercase tracking-widest text-[13px] flex items-center justify-end gap-0.5`}><Tip text="How many days remain before you need to place a reorder. Negative means you're already overdue.">Reorder In</Tip></div>
              <div className={`font-bold tabular-nums mt-0.5 ${us.text}`}>
                {hasPO ? "-" : s.daysUntilReorder == null ? "-" : s.daysUntilReorder === 0 ? "Today" : s.daysUntilReorder < 0 ? `${Math.abs(s.daysUntilReorder)}d overdue` : `${s.daysUntilReorder}d`}
              </div>
            </div>
            <div className="text-[15px]">
              <div className={`${textMuted} uppercase tracking-widest text-[13px] flex items-center justify-end gap-0.5`}><Tip text="Suggested order quantity to restore your stock to the target coverage window, based on forecast demand.">Units to Order</Tip></div>
              <div className={`font-bold tabular-nums mt-0.5 ${"text-[var(--t-accent)]"}`}>{s.orderQty?.toLocaleString() ?? "-"}</div>
            </div>
          </div>
        </div>
      </div>
    );
  };

  const renderGrouped = () => {
    const folderEntries = Object.entries(folders);
    const ungroupedSkus = sorted.filter(s => !Object.values(folders).some(f => f.skuIds.includes(s.skuId)));
    return (
      <div className="space-y-6">
        {folderEntries.map(([fid, f]) => {
          const folderSkus = sorted.filter(s => f.skuIds.includes(s.skuId));
          if (folderSkus.length === 0) return null;
          return (
            <div key={fid}>
              <div className="flex items-center gap-2 mb-2">
                <svg className={`h-3.5 w-3.5 ${textMuted}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
                </svg>
                <span className={`text-[16.5px] font-bold ${textMain}`}>{f.name}</span>
                <span className={`text-[14px] font-mono ${textMuted}`}>{folderSkus.length} SKU{folderSkus.length !== 1 ? "s" : ""}</span>
              </div>
              <div className="grid grid-cols-1 gap-3">
                {folderSkus.map(s => <SkuCard key={s.skuId} s={s} />)}
              </div>
            </div>
          );
        })}
        {ungroupedSkus.length > 0 && (
          <div>
            {folderEntries.length > 0 && (
              <div className="flex items-center gap-2 mb-2">
                <span className={`text-[16.5px] font-bold ${"text-[var(--t-dim)]"} uppercase tracking-widest text-[15px]`}>Ungrouped</span>
                <span className={`text-[14px] font-mono ${"text-[var(--t-dim)]"}`}>{ungroupedSkus.length} SKU{ungroupedSkus.length !== 1 ? "s" : ""}</span>
              </div>
            )}
            <div className="grid grid-cols-1 gap-3">
              {ungroupedSkus.map(s => <SkuCard key={s.skuId} s={s} />)}
            </div>
          </div>
        )}
      </div>
    );
  };

  const hasFolders = Object.keys(folders).length > 0;
  const grpBtnActive   = "bg-[var(--t-accent-soft)] border-[var(--t-accent-line)] text-[var(--t-accent)]";
  const grpBtnInactive = "bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-soft)] hover:border-[var(--t-line2)] hover:text-[var(--t-soft)]";

  return (
    <div className="flex-1 overflow-y-auto p-7 space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h2 className={`text-xl font-bold ${textMain}`}>Fleet Overview</h2>
          <p className={`text-[15px] ${textMuted} mt-1`}>{skuList.length} SKU{skuList.length !== 1 ? "s" : ""} loaded · sorted by urgency</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap justify-end">
          {hasFolders && (
            <button onClick={() => setGroupByFolder(v => !v)}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[15px] font-semibold border transition-all ${groupByFolder ? grpBtnActive : grpBtnInactive}`}>
              <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
              </svg>
              {groupByFolder ? "Grouped by folder" : "Group by folder"}
            </button>
          )}
          {skuList.length > 0 && (
            <>
              <button onClick={onExportFleet} disabled={!!exporting}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[15px] font-semibold bg-[var(--t-good-soft)] hover:bg-[var(--t-good-soft)] disabled:opacity-60 text-[var(--t-ink)] transition-all">
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" /></svg>
                {exporting === "fleet" ? "Preparing…" : "Download all SKUs"}
              </button>
              <button onClick={onExportAll} disabled={!!exporting} title="One workbook: fleet, scorecard, suppliers, and order history"
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[15px] font-semibold border transition-all ${"border-[var(--t-good-line)] text-[var(--t-good)] hover:bg-[var(--t-good-soft)]"} disabled:opacity-60`}>
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" /></svg>
                {exporting === "all" ? "Preparing…" : "Download everything"}
              </button>
            </>
          )}
        </div>
      </div>
      {/* No leadTimeOf here: this view (unused — App renders FleetBento) has no
          getParams in scope, and the prop is optional by design. */}
      <FleetAlertBanner skuForecasts={skuForecasts} openPOs={openPOs} lm={lm} />
      {groupByFolder ? renderGrouped() : (
        <div className="grid grid-cols-1 gap-3">
          {sorted.map(s => <SkuCard key={s.skuId} s={s} />)}
        </div>
      )}
    </div>
  );
}

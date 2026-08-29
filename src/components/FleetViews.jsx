import React, { useState } from 'react';
import { urgencyLevel, URGENCY_STYLES, URGENCY_STYLES_LM, formatDate,
         skuState, SKU_STATES } from '../lib/helpers';
import { Tip } from './common';
import { FleetAlertBanner } from './Sidebar';

// ─── FLEET SUMMARY VIEW ───────────────────────
// ─── FLEET BENTO (redesign · warm-dark-violet) ─────────────────────────────
// Self-contained, inline-themed so it doesn't depend on the global light/dark system
// yet. Wired to real data with the fallbacks: $ metrics only when cost is known,
// accuracy only when a backtest has run. Increment 1 of the redesign.
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
export function FleetBento({ skuForecasts, getParams, openPOs, onSelectSku, lm, onExportFleet, onExportAll, exporting, scorecardRows = [] }) {
  const T = lm
    ? { bg:"#f1f5f9", surface:"#ffffff", ink:"#0f172a", soft:"#64748b", faint:"#94a3b8",
        line:"rgba(15,23,42,.08)", line2:"rgba(15,23,42,.14)", accent:"#7c3aed", accentText:"#6d28d9",
        accentSoft:"rgba(124,58,237,.10)", green:"#16a34a", amber:"#b45309", red:"#dc2626", over:"#6d28d9",
        btnBg:"#7c3aed", btnFg:"#ffffff",
        pillRedBg:"rgba(220,38,38,.10)", pillRedFg:"#b91c1c", pillAmberBg:"rgba(217,119,6,.12)", pillAmberFg:"#b45309",
        slow:"#d97706", sky:"#0284c7" }
    : { bg:"#0c0c0e", surface:"#161619", ink:"#f5f5f7", soft:"#a1a1a8", faint:"#6c6c74",
        line:"rgba(255,255,255,.08)", line2:"rgba(255,255,255,.14)", accent:"#8b7dff", accentText:"#b9afff",
        accentSoft:"rgba(139,125,255,.14)", green:"#30d158", amber:"#ffb340", red:"#ff5b52", over:"#b9afff",
        btnBg:"#ffffff", btnFg:"#0c0c0e",
        pillRedBg:"rgba(255,91,82,.16)", pillRedFg:"#ff8079", pillAmberBg:"rgba(255,179,64,.15)", pillAmberFg:"#ffc777",
        slow:"#ffb340", sky:"#38bdf8" };
  const fmt = n => "$" + Math.round(n).toLocaleString();
  /* Projected repeat orders are hatched, not just paler: at a glance the texture says
     "modelled" where a lighter shade would only read as "less". Works in both themes
     and doesn't lean on colour alone. */
  const PROJ_FILL = lm
    ? "repeating-linear-gradient(135deg,rgba(124,58,237,.30) 0 5px,rgba(124,58,237,.11) 5px 10px)"
    : "repeating-linear-gradient(135deg,rgba(139,125,255,.42) 0 5px,rgba(139,125,255,.15) 5px 10px)";
  const scoreBySku = Object.fromEntries((scorecardRows || []).map(r => [r.skuId, r]));
  // Lead time travels in, because the reorder bands are a fraction of it — a
  // 60-day sea line and a 3-day air line do not share a "due soon".
  const stateOf = s => skuState(s, scoreBySku[s.skuId], !!openPOs[s.skuId],
                               Number(getParams(s.skuId)?.leadTime) || null);
  const bucket = s => stateOf(s).key;
  const counts = { stockout:0, reorder:0, overstock:0, dead:0, healthy:0, unrated:0 };
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
    ["dead",      T.faint,   SKU_STATES.dead.label],
    ["overstock", T.slow,    SKU_STATES.overstock.label],
    ["reorder",   T.sky,     SKU_STATES.reorder.label],
    ["healthy",   T.green,   SKU_STATES.healthy.label],
    // Products /api/scorecard never classified (no fitted forecast). Shown rather
    // than folded into healthy, so the bar still sums to the catalogue.
    ["unrated",   T.faint,   SKU_STATES.unrated.label],
  ];
  skuForecasts.forEach(s => counts[bucket(s)]++);
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

  const urgent = skuForecasts.filter(s => !openPOs[s.skuId] && ["stockout","dead","overstock","reorder"].includes(bucket(s)))
    .sort((a,b) => (a.daysUntilReorder ?? 999) - (b.daysUntilReorder ?? 999));
  // New SKUs (baseline/young) that look like they'll run low soon — surfaced gently as
  // "consider ordering," never as hard overdue. "Soon" = forecast reorder OR observed
  // runway within ~10 days. orderQty is 0 for baseline, so we only suggest a qty if known.
  const provDays = s => (s.daysUntilReorder != null ? s.daysUntilReorder : s.observedRunwayDays);
  const provLow = skuForecasts.filter(s => (s.tooNew || s.young) && !openPOs[s.skuId]
    && provDays(s) != null && provDays(s) <= 10);
  const newSoon = [...provLow].sort((a,b) => (provDays(a) ?? 99) - (provDays(b) ?? 99));

  let costKnown = false, invValue = 0, invKnown = false;

  // Upcoming reorders by week — the forecast turned into a purchasing schedule (cash, or
  // units when cost is unknown). This is the tool's unique value: a forward buying plan.
  const WEEKS = 8;
  const weekly = Array.from({ length:WEEKS }, () => ({ next:0, repeat:0, nextUnits:0, repeatUnits:0 }));
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
  const weekRange = i => `${fmtDay(weekStart(i))} – ${fmtDay(weekEnd(i))}`;

  /* ONE purchasing number on this page, and the chart is it. There used to be a second
     one — a "To order now" tile — and the two could never agree, because they were not
     measuring the same thing: the tile counted STATUS (every product the scorecard calls
     "Stockout risk" or "Reorder due") while the chart is a CALENDAR of reorder dates.
     The scorecard's bands are fractions of each product's own lead time (risk = lt/2,
     due = lt × 1.5, see helpers.reorderBands), so a 60-day sea line is "Reorder due"
     with a reorder date up to 90 days out. Sat next to a bar labelled "now", that reads
     as a contradiction to anyone looking at the page, and no bucketing rule can fix it —
     the two numbers answer different questions. So the dollars are stated once, on the
     calendar, and the status view stays a COUNT of products on the fleet-health card.

     Each bar has two parts, and the difference matters enough to draw:

       `next`   — the product's NEXT reorder, on the date the forecast actually gives it.
       `repeat` — the same product coming round again, projected at its own cycle length
                  (one order's quantity ÷ its daily demand = the days of cover it buys).

     Without the second part the chart quietly under-reports the back half of its own
     horizon. It draws each product once, but the median product here reorders about
     every 38 days, so inside eight weeks most of them come round again — and the last
     bars showed a few hundred dollars where the real figure was tens of thousands. It
     was not a stale number; the second cycle was never drawn at all, and no amount of
     live sales data would have filled it in.

     Projection stops where it stops being a forecast: overstocked and dead stock are
     being cleared, not rebought, so they contribute their next order and nothing after
     it. Everything past the first repeat assumes demand holds at today's average and
     that the same quantity gets ordered each time, which is why it is drawn as hatching
     rather than solid — committed and projected should never read as the same money.

     Products with no reorder date can't be placed on a calendar and are left out rather
     than swept into the first bar to make a total come out even. */
  const REPEATABLE = s => s !== "overstock" && s !== "dead";
  skuForecasts.forEach(s => {
    const prm = getParams(s.skuId); const c = Number(prm.unitCost); const f = Number(prm.fees) || 0;
    const stk = Number(prm.stock) || 0;
    if (c > 0) { invValue += stk * c; invKnown = true; }
    if (openPOs[s.skuId] || !s.orderQty) return;
    /* stateOf, not s.daysUntilReorder — skuState falls back to the scorecard's copy of
       the date, so reading the raw field made products vanish from the plan. */
    const state = stateOf(s);
    const days = state.days;
    if (days == null) return;
    const cost = c > 0 ? s.orderQty * (c + f) : 0;
    if (c > 0) costKnown = true;

    const w = Math.max(weekOf(days), 0);   // anything already late joins the first bar
    if (w < WEEKS) { weekly[w].nextUnits += s.orderQty; weekly[w].next += cost; }

    // Days of cover one order buys — the product's own reorder rhythm.
    const rate = Number(s.avgDailyDemand) || 0;
    const cycle = rate > 0 ? s.orderQty / rate : 0;
    if (cycle < 1 || !REPEATABLE(state.key)) return;
    for (let t = days + cycle, guard = 0; guard < 60; t += cycle, guard++) {
      const wr = weekOf(t);
      if (wr >= WEEKS) break;
      // A repeat that would have fallen in a past week is history, not a plan.
      if (wr >= 0) { weekly[wr].repeatUnits += s.orderQty; weekly[wr].repeat += cost; }
    }
  });
  const chartVal = costKnown;
  // One accessor for "the number this chart is currently drawing", so the bars, the
  // totals and the tile can't disagree about whether we're in dollars or units.
  const valOf   = x => chartVal ? x.next   : x.nextUnits;
  const repOf   = x => chartVal ? x.repeat : x.repeatUnits;
  const totalOf = x => valOf(x) + repOf(x);
  const maxBar  = Math.max(...weekly.map(totalOf), 1);
  const anyRepeat = weekly.some(x => repOf(x) > 0);
  // Chart row height, and what's left for a bar once the value (14) + label (34) + two
  // 9px gaps are taken out of it.
  const horizonTotal  = weekly.reduce((t, x) => t + totalOf(x), 0);
  const horizonRepeat = weekly.reduce((t, x) => t + repOf(x), 0);
  const CHART_H = 170, BAR_MAX = CHART_H - 14 - 34 - 18;


  const seg = (n, color) => n > 0 ? <div style={{ width:(n/total*100)+"%", background:color }} /> : null;
  const card = { background:T.surface, border:`1px solid ${T.line}`, borderRadius:18, padding:"20px 22px", position:"relative", overflow:"hidden", boxShadow: lm ? "0 1px 3px rgba(15,23,42,.06),0 1px 2px rgba(15,23,42,.04)" : "none" };
  const pill = (bg, fg, txt) => <span style={{ background:bg, color:fg, fontSize:12, fontWeight:600, padding:"4px 11px", borderRadius:980, whiteSpace:"nowrap" }}>{txt}</span>;
  const btnGhost = { fontSize:13, fontWeight:600, padding:"10px 14px", borderRadius:12, cursor: exporting ? "default" : "pointer", background:"transparent", color:T.soft, border:`1px solid ${T.line2}`, whiteSpace:"nowrap", opacity: exporting ? 0.6 : 1 };
  const btnSolid = { ...btnGhost, background:T.btnBg, color:T.btnFg, border:"none" };

  return (
    <div style={{ background:T.bg, color:T.ink, minHeight:"100%", padding:"24px 28px 40px", fontFamily:'-apple-system,BlinkMacSystemFont,"SF Pro Display","Inter",sans-serif' }}>
      <div style={{ position:"absolute", inset:0, pointerEvents:"none", background:"radial-gradient(620px 420px at 16% 2%,rgba(139,125,255,.13),transparent 60%)" }} />
      <div style={{ position:"relative", display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:20 }}>
        <div><div style={{ fontSize:28, fontWeight:700, letterSpacing:"-.03em" }}>Fleet overview</div>
          <div style={{ fontSize:13.5, color:T.soft, marginTop:3 }}>{skuForecasts.length} SKUs · {toOrder} to order</div></div>
        <div style={{ display:"flex", alignItems:"center", gap:10 }}>
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

      <div style={{ position:"relative", display:"grid", gridTemplateColumns:"repeat(4,1fr)", gap:16 }}>
        <div style={{ ...card, gridColumn:"span 2", minHeight:200 }}>
          <div style={{ fontSize:13, color:T.faint, fontWeight:500 }}>Fleet health · {skuForecasts.length} SKUs</div>
          <div style={{ fontSize:52, fontWeight:700, letterSpacing:"-.045em", lineHeight:1, marginTop:6 }}>{toOrder} <span style={{ fontSize:24, color:T.soft, fontWeight:600, letterSpacing:"normal", marginLeft:2 }}>to order</span></div>
          <div style={{ fontSize:13.5, color:T.soft, marginTop:7 }}>
            {/* Only the parts that are actually non-zero. "0 stockout risk · 0 dead
                stock · 0 slow" was four labels to say nothing, and two of those
                zeros were a check that hadn't run rather than a clean result. */}
            {toOrder === 0 ? "Nothing to order"
              : `${counts.stockout} at risk${overdueCount > 0 ? ` (${overdueCount} overdue)` : ""}`
                + (counts.reorder > 0 ? ` · ${counts.reorder} due soon` : "")}
            {stockScored && counts.overstock > 0 ? ` · ${counts.overstock} overstocked` : ""}
            {stockScored && counts.dead > 0 ? ` · ${counts.dead} dead stock` : ""}
            {onOrderCount > 0 ? ` · ${onOrderCount} on order` : ""}
            {newCount > 0 ? ` · ${newCount} new${provLow.length ? ` (${provLow.length} running low)` : ""}` : ""}
          </div>
          <div style={{ display:"flex", height:14, borderRadius:8, overflow:"hidden", marginTop:22, gap:2 }}>
            {segDefs.map(([k,c]) => seg(counts[k], c))}
          </div>
          <div style={{ display:"flex", flexWrap:"wrap", gap:"8px 16px", marginTop:14, fontSize:12.5, color:T.soft }}>
            {/* title= carries the shared definition, so hovering any legend entry
                explains the band without a paragraph living on the page. */}
            {segDefs.filter(([k]) => counts[k] > 0).map(([k,c,label]) => (
              <span key={k} title={SKU_STATES[k].help}
                style={{ display:"flex", alignItems:"center", gap:6, cursor:"help" }}>
                <span style={{ width:9, height:9, borderRadius:3, background:c, flexShrink:0 }} />
                <b style={{ color:T.ink }}>{counts[k]}</b> {label}
                {k === "stockout" && overdueCount > 0 &&
                  <span style={{ color:T.faint }}>({overdueCount} overdue)</span>}
              </span>
            ))}
          </div>
        </div>

        <div style={{ ...card, gridColumn:"span 2", minHeight:200 }}>
          <div style={{ display:"flex", alignItems:"baseline", justifyContent:"space-between", marginBottom:8 }}>
            <div style={{ fontSize:17, fontWeight:600, letterSpacing:"-.02em" }}>Needs attention</div>
            {(urgent.length + newSoon.length) > 0 && <span style={{ fontSize:12.5, color:T.soft, fontWeight:600 }}>{urgent.length + newSoon.length}</span>}
          </div>
          <div className="sku-scroll" style={{ maxHeight:300, overflowY:"auto", marginRight:-12, paddingRight:12 }}>
          {urgent.length === 0 && newSoon.length === 0 && <div style={{ fontSize:13.5, color:T.soft, padding:"20px 0" }}>Nothing urgent — every SKU is covered. ✓</div>}
          {urgent.map(s => { const d = s.daysUntilReorder; const b = bucket(s); const sc = scoreBySku[s.skuId];
            /* Every tag says the same thing the fleet bar says, in the same words —
               a row tagged "Overstocked" sits under the "overstocked" segment, and
               a timing tag names the days rather than a mood ("Watch 14d"). */
            const tag = b === "dead" ? pill("rgba(100,116,139,.16)", T.faint, "Dead stock")
              : b === "overstock" ? pill(T.pillAmberBg, T.pillAmberFg, sc?.sellThrough != null ? `Overstocked · ${Math.round(sc.sellThrough * 100)}% sold` : "Overstocked")
              : b === "stockout" ? pill(T.pillRedBg,T.pillRedFg, d == null ? "Stockout risk" : d < 0 ? `Overdue ${Math.abs(d)}d` : d === 0 ? "Due today" : `Due in ${d}d`)
              : pill(T.pillAmberBg,T.pillAmberFg, d != null ? `Due in ${d}d` : "Due");
            const action = b === "dead" ? "Review / clear"
              : b === "overstock" ? "Reduce orders"
              : `Order ${s.orderQty?.toLocaleString() ?? "—"}`;
            return (
              <div key={s.skuId} onClick={() => onSelectSku(s.skuId)} style={{ display:"flex", alignItems:"center", gap:12, padding:"11px 0", borderTop:`1px solid ${T.line}`, cursor:"pointer" }}>
                <div style={{ flex:1, minWidth:0 }}><div style={{ fontSize:14, fontWeight:600, letterSpacing:"-.01em", whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>{s.skuName}</div>
                  <div style={{ fontSize:11.5, color:T.faint, marginTop:1 }}>{s.skuId}{sc?.status ? ` · ${sc.status}` : ""}</div></div>
                {tag}
                <div style={{ background:T.btnBg, color:T.btnFg, fontSize:12.5, fontWeight:600, padding:"7px 13px", borderRadius:980, whiteSpace:"nowrap" }}>{action}</div>
              </div>);
          })}
          {newSoon.length > 0 && (
            <div style={{ fontSize:10.5, fontWeight:700, textTransform:"uppercase", letterSpacing:".07em", color:T.accentText, marginTop: urgent.length ? 16 : 4, marginBottom:-2 }}>New · keep an eye</div>
          )}
          {newSoon.map(s => { const d = provDays(s);
            return (
              <div key={s.skuId} onClick={() => onSelectSku(s.skuId)} style={{ display:"flex", alignItems:"center", gap:12, padding:"11px 0", borderTop:`1px solid ${T.line}`, cursor:"pointer" }}>
                <div style={{ flex:1, minWidth:0 }}>
                  <div style={{ fontSize:14, fontWeight:600, letterSpacing:"-.01em", whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>{s.skuName}</div>
                  <div style={{ fontSize:11.5, color:T.faint, marginTop:1 }}>{s.skuId} · ~{Math.max(d,0)}d of stock left</div>
                </div>
                {pill(T.accentSoft, T.accentText, "New · order soon?")}
                <div style={{ border:`1px solid ${T.line2}`, color:T.soft, fontSize:12.5, fontWeight:600, padding:"6px 12px", borderRadius:980, whiteSpace:"nowrap" }}>
                  {s.orderQty > 0 ? `Consider ~${s.orderQty.toLocaleString()}` : "Consider ordering"}
                </div>
              </div>);
          })}
          </div>
        </div>

        <div style={{ ...card, gridColumn:"span 4" }}>
          <div style={{ fontSize:13, color:T.faint, fontWeight:500 }}>Upcoming reorders · next {WEEKS} weeks</div>
          <div style={{ fontSize:13.5, color:T.soft, marginTop:2 }}>{chartVal ? "Purchasing cash coming due — plan your POs ahead" : "Units coming due — add unit costs to see it as $"}</div>
          {/* Bars are sized in px against BAR_MAX rather than as a % of the row, so the
              tallest one can't grow past the space left by the value above it and the
              dated label below — a % height ignored both and overflowed the card. */}
          <div style={{ display:"flex", alignItems:"flex-end", gap:14, height:CHART_H, marginTop:18 }}>
            {weekly.map((wk, i) => {
              const firm = valOf(wk), proj = repOf(wk), v = firm + proj;
              const h = v > 0 ? Math.max(Math.round(v / maxBar * BAR_MAX), 4) : 0;
              const hProj = v > 0 ? Math.round(proj / v * h) : 0;
              const money = n => chartVal ? fmt(n) : n.toLocaleString() + " units";
              const tip = (i===0 ? `Already overdue, plus everything due by ${fmtDay(weekEnd(0))}` : weekRange(i))
                + `\n${money(firm)} — next order`
                + (proj > 0 ? `\n${money(proj)} — projected repeat orders` : "");
              return (
                <div key={i} style={{ flex:1, display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"flex-end", gap:9, height:"100%" }}>
                  <div style={{ fontSize:11.5, color:i===0?T.accentText:T.faint, fontWeight:600, height:14 }}>{v > 0 ? (chartVal ? fmt(v) : v.toLocaleString()) : ""}</div>
                  {/* Stacked: solid below is the order the forecast actually dates, hatched
                      above is the same product projected round again at its own cycle. */}
                  <div title={tip} style={{ width:"100%", height:h, borderRadius:"7px 7px 2px 2px", overflow:"hidden", display:"flex", flexDirection:"column" }}>
                    <div style={{ height:hProj, background:PROJ_FILL, borderBottom: (hProj > 0 && h - hProj > 0) ? `1px solid ${T.surface}` : "none" }} />
                    <div style={{ flex:1, background:i===0?"linear-gradient(180deg,#b9afff,#8b7dff)":"linear-gradient(180deg,#7c6cff,#574acb)" }} />
                  </div>
                  <div style={{ height:34, textAlign:"center", lineHeight:1.18 }}>
                    <div style={{ fontSize:10.5, color:i===0?T.accentText:T.faint, fontWeight:i===0?600:400 }}>
                      {i===0 ? "Overdue + due this week" : weekRange(i)}
                    </div>
                    {i===0 && <div style={{ fontSize:9.5, color:T.faint, marginTop:1 }}>through {fmtDay(weekEnd(0))}</div>}
                  </div>
                </div>);
            })}
          </div>
          {anyRepeat && (
            <div style={{ display:"flex", flexWrap:"wrap", alignItems:"center", gap:"6px 18px", marginTop:14, fontSize:11.5, color:T.soft }}>
              <span style={{ display:"flex", alignItems:"center", gap:7 }}>
                <span style={{ width:11, height:11, borderRadius:3, background:"linear-gradient(180deg,#7c6cff,#574acb)", flexShrink:0 }} />
                Next order · dated by the forecast
              </span>
              <span style={{ display:"flex", alignItems:"center", gap:7, cursor:"help" }}
                title="Projected from each product's own reorder cycle — one order's quantity divided by its daily demand. Assumes demand holds and the same quantity is ordered again. Overstocked and dead stock are left out.">
                <span style={{ width:11, height:11, borderRadius:3, background:PROJ_FILL, flexShrink:0 }} />
                Projected repeat · same product coming round again
              </span>
            </div>
          )}
        </div>

        {/* Deliberately the first bar of the chart, restated — same variable, so the two
            can never disagree. The tile above it is the headline; the chart is the detail. */}
        <div style={card}><div style={{ fontSize:13, color:T.faint, fontWeight:500 }}>Due this week</div>
          <div style={{ fontSize:32, fontWeight:700, letterSpacing:"-.04em", marginTop:9, lineHeight:1 }}>{chartVal ? fmt(totalOf(weekly[0])) : totalOf(weekly[0]).toLocaleString()}</div>
          <div style={{ fontSize:12.5, marginTop:9, color:T.soft, fontWeight:500 }}>overdue + due by {fmtDay(weekEnd(0))}</div>
          <div style={{ fontSize:12, marginTop:3, color:T.faint }}>{chartVal ? `${fmt(horizonTotal)} over the next ${WEEKS} weeks` : "units · add unit costs to see $"}</div>
          {/* The horizon total counts each product's repeat orders too, which is most of
              the back half of the chart. Unlabelled, it just looks like the number grew. */}
          {chartVal && horizonRepeat > 0 &&
            <div style={{ fontSize:11.5, marginTop:2, color:T.faint }}>incl. {fmt(horizonRepeat)} projected repeats</div>}</div>

        <div style={card}><div style={{ fontSize:13, color:T.faint, fontWeight:500 }}>Inventory value</div>
          <div style={{ fontSize:32, fontWeight:700, letterSpacing:"-.04em", marginTop:9, lineHeight:1 }}>{invKnown ? fmt(invValue) : "—"}</div>
          <div style={{ fontSize:12.5, marginTop:9, color:T.faint, fontWeight:500 }}>{invKnown ? "stock on hand" : "add unit costs to value stock"}</div></div>

        <div style={card}><div style={{ fontSize:13, color:T.faint, fontWeight:500 }}>Healthy</div>
          <div style={{ fontSize:32, fontWeight:700, letterSpacing:"-.04em", marginTop:9, lineHeight:1, color:T.green }}>{counts.healthy}</div>
          <div style={{ fontSize:12.5, marginTop:9, color:T.faint, fontWeight:500 }}>{Math.round(counts.healthy/total*100)}% of catalog</div></div>

        <div style={{ ...card }}><div style={{ position:"absolute", width:160, height:160, borderRadius:"50%", background:"radial-gradient(circle,rgba(139,125,255,.16),transparent 65%)", bottom:-70, left:-40 }} />
          <div style={{ fontSize:13, color:T.faint, fontWeight:500 }}>Total SKUs</div>
          <div style={{ fontSize:32, fontWeight:700, letterSpacing:"-.04em", marginTop:9, lineHeight:1, color:T.accentText }}>{skuForecasts.length}</div>
          <div style={{ fontSize:12.5, marginTop:9, color:T.faint, fontWeight:500 }}>forecast &amp; protected</div></div>
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

  const textMain  = lm ? "text-slate-900" : "text-white";
  const textMuted = lm ? "text-slate-500" : "text-slate-500";

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
          <div className={`flex items-center gap-2 mb-3 pb-3 border-b ${lm ? "border-red-200" : "border-red-900/40"}`}>
            <span className="text-sm">🚨</span>
            <span className={`text-xs font-bold ${lm ? "text-red-700" : "text-red-400"}`}>
              {s.daysUntilReorder === 0 ? "REORDER DUE TODAY" : `REORDER OVERDUE — ${Math.abs(s.daysUntilReorder)} day${Math.abs(s.daysUntilReorder) !== 1 ? "s" : ""} ago`}
            </span>
          </div>
        )}
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <div className={`h-2.5 w-2.5 rounded-full shrink-0 ${us.dot}`} />
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <div className={`text-sm font-bold ${textMain} truncate`}>{s.skuName}</div>
                {folderEntry && !groupByFolder && (
                  <span className={`shrink-0 flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-md font-mono border ${lm ? "text-slate-500 bg-white border-slate-300" : "text-slate-500 bg-slate-800/60 border-slate-700/40"}`}>
                    <svg className="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
                    </svg>
                    {folderEntry.name}
                  </span>
                )}
              </div>
              <div className={`text-[11px] font-mono ${textMuted}`}>{s.skuId}</div>
            </div>
          </div>
          <div className="flex items-center gap-6 shrink-0 text-right">
            <div className="text-xs">
              <div className={`${textMuted} uppercase tracking-widest text-[10px] flex items-center justify-end gap-0.5`}><Tip text="The latest date you should place a reorder to avoid running out of stock, based on lead time and coverage window.">Order By</Tip></div>
              <div className={`font-bold tabular-nums mt-0.5 ${hasPO ? "text-violet-500" : isOverdue ? "text-red-500" : urgency === "high" ? "text-amber-500" : textMain}`}>
                {hasPO ? "On Order" : isOverdue ? (s.daysUntilReorder === 0 ? "Today" : `${Math.abs(s.daysUntilReorder)}d overdue`) : s.daysUntilReorder != null ? formatDate(s.daysUntilReorder).split(",")[0] : "—"}
              </div>
            </div>
            <div className="text-xs">
              <div className={`${textMuted} uppercase tracking-widest text-[10px] flex items-center justify-end gap-0.5`}><Tip text="How many days remain before you need to place a reorder. Negative means you're already overdue.">Reorder In</Tip></div>
              <div className={`font-bold tabular-nums mt-0.5 ${us.text}`}>
                {hasPO ? "—" : s.daysUntilReorder == null ? "—" : s.daysUntilReorder === 0 ? "Today" : s.daysUntilReorder < 0 ? `${Math.abs(s.daysUntilReorder)}d overdue` : `${s.daysUntilReorder}d`}
              </div>
            </div>
            <div className="text-xs">
              <div className={`${textMuted} uppercase tracking-widest text-[10px] flex items-center justify-end gap-0.5`}><Tip text="Suggested order quantity to restore your stock to the target coverage window, based on forecast demand.">Units to Order</Tip></div>
              <div className={`font-bold tabular-nums mt-0.5 ${lm ? "text-violet-700" : "text-violet-300"}`}>{s.orderQty?.toLocaleString() ?? "—"}</div>
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
                <span className={`text-sm font-bold ${textMain}`}>{f.name}</span>
                <span className={`text-[11px] font-mono ${textMuted}`}>{folderSkus.length} SKU{folderSkus.length !== 1 ? "s" : ""}</span>
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
                <span className={`text-sm font-bold ${lm ? "text-slate-500" : "text-slate-600"} uppercase tracking-widest text-xs`}>Ungrouped</span>
                <span className={`text-[11px] font-mono ${lm ? "text-slate-400" : "text-slate-700"}`}>{ungroupedSkus.length} SKU{ungroupedSkus.length !== 1 ? "s" : ""}</span>
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
  const grpBtnActive   = lm ? "bg-violet-50 border-violet-400 text-violet-700" : "bg-violet-950/40 border-violet-700/60 text-violet-300";
  const grpBtnInactive = lm ? "bg-white border-slate-300 text-slate-600 hover:border-slate-400 hover:text-slate-800" : "bg-slate-900/60 border-slate-700/50 text-slate-400 hover:border-slate-600 hover:text-slate-200";

  return (
    <div className="flex-1 overflow-y-auto p-7 space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h2 className={`text-xl font-bold ${textMain}`}>Fleet Overview</h2>
          <p className={`text-xs ${textMuted} mt-1`}>{skuList.length} SKU{skuList.length !== 1 ? "s" : ""} loaded · sorted by urgency</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap justify-end">
          {hasFolders && (
            <button onClick={() => setGroupByFolder(v => !v)}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all ${groupByFolder ? grpBtnActive : grpBtnInactive}`}>
              <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
              </svg>
              {groupByFolder ? "Grouped by folder" : "Group by folder"}
            </button>
          )}
          {skuList.length > 0 && (
            <>
              <button onClick={onExportFleet} disabled={!!exporting}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 disabled:opacity-60 text-white transition-all">
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" /></svg>
                {exporting === "fleet" ? "Preparing…" : "Download all SKUs"}
              </button>
              <button onClick={onExportAll} disabled={!!exporting} title="One workbook: fleet, scorecard, suppliers, and order history"
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all ${lm ? "border-emerald-300 text-emerald-700 hover:bg-emerald-50" : "border-emerald-800 text-emerald-300 hover:bg-emerald-950/40"} disabled:opacity-60`}>
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
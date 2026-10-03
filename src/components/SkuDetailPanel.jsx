import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useDebouncedCallback } from 'use-debounce';
import ForecastDetailsDrawer from '../ForecastDetailsDrawer';
import ForecastChart from './ForecastChart';
import { OrderInTransitModal, EventsModal } from './modals';
import { ArrivalPromptBanner } from './ArrivalPrompts';
import { Tip, ReliabilityBadge, MetricCard, MonthForecastCard, MonthlySummaryTable, PriceTierTable, SignalPill } from './common';
import { formatDate, isoToDisplay, urgencyLevel, autoStrategy, computeSkuLeadTimeStats,
         makeSupId, makeOrdId, todayMs, todayStr, STATUS_CONFIG, poEtaDays, planningLeadTime, LEAD_TIME_MIN_DELIVERIES, FREIGHT_MODES, leadTimeTracks, supplierOf, resolveLane, readSlowShipment, slowShipmentNote,
         readLeadTimeChanges, latestChangeAt, applyLeadTimeChange, undoLastLeadTimeChange,
         describeLeadTimeChange, parkOrder, adoptParkedOrders, REASON_LABELS, stockIsCounted,
         findPoOrder, updateOrder, UNASSIGNED_SUP_ID } from '../lib/helpers';
import { UnitEconomicsCard } from './UnitEconomics';
import { SkuActionBar, PriceHistoryDrawer, HolidaysDrawer, forecastHealth } from './SkuActions';
import { terminal, MONO, SANS } from '../lib/theme';
import { API, fetchJson, forecastUrl } from '../lib/api';
import { saveStorage } from '../lib/storage';
import { GROQ_URL, GROQ_MODEL, GROQ_LOW_REASONING } from '../lib/ai';

// ─── ORDER MATH BREAKDOWN ─────────────────────
/* What a count is worth now. A shelf counted on Monday is not Monday's number on
   Thursday, and the tool knows exactly what left in between — so it shows the count, the
   drawdown and the level it derives, rather than silently substituting one for another.
   A number that moved on its own is only trustworthy if it can be explained. */
export function StockSinceCount({ data, params, lm }) {
  const since = data?.sinceCount;
  const derived = data?.derivedStock;
  const today = data?.todaySales;
  if (!since?.since) {
    return <p className={`text-[13px] mt-1 ${"text-[var(--t-dim)]"}`}>Counted by you.</p>;
  }
  const sold = Number(since.total) || 0;
  const when = new Date(since.since + "T00:00:00").toLocaleDateString(undefined,
    { month: "short", day: "numeric" });
  if (sold <= 0) {
    return (
      <p className={`text-[13px] mt-1 ${"text-[var(--t-dim)]"}`}>
        Counted {when}. Nothing sold since, so this is still current.
      </p>
    );
  }
  return (
    <div className={`text-[13px] mt-1 space-y-0.5 ${"text-[var(--t-dim)]"}`}>
      <div>
        Counted <strong>{params.stock}</strong> on {when} · <strong>{sold}</strong> sold since
        {since.todayIncluded && Number(since.today) > 0
          ? ` (${since.today} today)` : ""}
      </div>
      <div className={"text-[var(--t-soft)]"}>
        Likely on the shelf now: <strong>{derived}</strong>
      </div>
      {today?.stale && since.todayIncluded && (
        <div className={"text-[var(--t-warn)]"}>
          Today&apos;s figure is over an hour old. A reading was missed.
        </div>
      )}
    </div>
  );
}

/* Today's running total, from the hourly tick. Deliberately understated next to the
   month figures: it is a partial day and says so, and it carries its own freshness
   because a number from ten minutes ago and one from two hours ago support different
   decisions. Silent when the source cannot supply it at all, rather than printing a zero
   that would be indistinguishable from a real one. */
export function TodaySoFar({ data, muted }) {
  const t = data?.todaySales;
  if (!t || t.unsupported || t.forToday === false) return null;
  const units = Number(t.units) || 0;
  const mins = t.ageSeconds == null ? null : Math.round(t.ageSeconds / 60);
  const when = mins == null ? "" : mins < 2 ? "just now" : `${mins} min ago`;
  return (
    <div className={`text-[13px] mt-1 ${t.stale ? "text-[var(--t-warn)]" : muted}`}>
      {units > 0 ? <><strong>{units.toLocaleString()}</strong> today</> : "none today"}
      {when && <> · checked {when}</>}
      {t.stale && <> · a check was missed</>}
    </div>
  );
}

export function OrderMathCard({ data, leadTime, planSource, planNeeded, coverageDays, unitsOnOrder, lm, skuSt, demandVolatilityColor }) {
  const bg    = "bg-[var(--t-sunken)] border-[var(--t-line)]";
  const label = "text-[var(--t-dim)]";
  const head  = "text-[var(--t-dim)] border-[var(--t-line)]";
  const val   = "text-[var(--t-ink)]";
  const sub   = "text-[var(--t-soft)] border-[var(--t-line)]";
  const divider = "border-[var(--t-line)]";

  // What planning actually used, from planningLeadTime's `source`. This used to be guessed
  // by comparing the TYPED lead time with the measured avg/P80, so a product planning on
  // a measured P80 could still say "Manual lead time... log 3 orders to unlock P80".
  const usingP80 = planSource === "measured";
  const daysEarlier = usingP80 && skuSt?.avg != null && leadTime > skuSt.avg ? leadTime - skuSt.avg : null;

  // Protection level is the MULTIPLIER on the demand spread: buffer = z x spread, where
  // the spread is how far the forecast misses over a whole lead time, in units. It is
  // picked from economics when supported, otherwise from the margin/default fallback.
  const prot = data.protection;

  return (
    // Two columns on a wide panel. As one full-width strip the rows are label-left /
    // number-right, so every pair was separated by the entire screen and the card ran
    // twice as tall as its content needed. The split is weighted rather than even: the
    // left side is short labels and numbers, the right side is sentences.
    <div className={`${bg} border rounded-xl grid md:grid-cols-[minmax(0,1fr)_minmax(0,1.45fr)]`} style={{overflow: "clip"}}>
      {/* ── Quantity math ── */}
      <div className="p-3.5 font-mono text-[15px] space-y-1.5">
        <div className={`text-[14px] uppercase tracking-widest font-bold pb-1.5 mb-0.5 border-b ${head}`}>Order qty breakdown</div>
        <div className={`flex justify-between ${label}`}><span>Demand after delivery ({coverageDays}d)</span><span className={`${val} tabular-nums`}>{data.coverageQty.toLocaleString()}</span></div>
        {data.holidayCover?.units > 0 ? (<>
          <div className={`flex justify-between ${label}`}><span>Demand buffer</span><span className="text-[var(--t-accent)] tabular-nums">+ {data.holidayCover.standingUnits.toLocaleString()}</span></div>
          {/* Temporary: only while a holiday is inside the order window, sold down after. */}
          <div className={`flex justify-between ${label}`}
            title={`Extra cover for ${data.holidayCover.holidays.map(h => h.name).join(" and ")}: holiday weeks sell less predictably, so this order carries more spare stock. It's temporary: once the holiday passes, the next order is smaller by whatever's left.${data.holidayCover.carryCost != null ? ` About $${Math.round(data.holidayCover.carryCost).toLocaleString()} to carry for one order cycle.` : ""}`}>
            <span className="truncate pr-2">Holiday cover ({data.holidayCover.holidays[0]?.name}{data.holidayCover.holidays.length > 1 ? ` +${data.holidayCover.holidays.length - 1}` : ""})</span>
            <span className="text-[var(--t-accent)] tabular-nums">+ {data.holidayCover.units.toLocaleString()}</span>
          </div>
        </>) : (
          <div className={`flex justify-between ${label}`}><span>Demand buffer</span><span className="text-[var(--t-accent)] tabular-nums">+ {data.safetyStock.toLocaleString()}</span></div>
        )}
        <div className={`flex justify-between ${sub} border-t pt-1.5 mt-0.5`}><span>Target stock level</span><span className="font-bold tabular-nums">{data.targetInventory.toLocaleString()}</span></div>
        <div className={`flex justify-between text-[14px] ${label}`}><span>Est. stock at delivery</span><span className="text-[var(--t-bad)] tabular-nums">− {(data.stockAtDelivery ?? data.projectedStockReorder).toLocaleString()}</span></div>
        {unitsOnOrder > 0 && (
          <div className={`flex justify-between text-[14px] ${label}`}><span>Open PO in transit</span><span className="text-[var(--t-accent)] tabular-nums">− {unitsOnOrder.toLocaleString()}</span></div>
        )}
        <div className={`flex justify-between font-bold border-t ${divider} pt-1.5 mt-0.5 text-[16.5px] ${"text-[var(--t-accent)]"}`}><span>Units to order</span><span className="tabular-nums">= {data.orderQty.toLocaleString()}</span></div>
      </div>

      {/* ── Protection summary ── */}
      <div className={`border-t md:border-t-0 md:border-l ${divider} divide-y ${divider}`}>
        <div className={`text-[13px] uppercase tracking-widest font-bold px-3.5 pt-3.5 pb-1.5 ${"text-[var(--t-dim)]"}`}>Protection breakdown</div>

        {/* Demand buffer row */}
        <div className={`flex items-start gap-2.5 px-3.5 py-2 ${"bg-[var(--t-panel)]"}`}>
          <div className={`mt-0.5 h-4 w-4 rounded-full flex items-center justify-center shrink-0 ${"bg-[var(--t-good-soft)]"}`}>
            <svg className={`h-2.5 w-2.5 ${"text-[var(--t-good)]"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7"/></svg>
          </div>
          <div className="flex-1 min-w-0">
            {/* While the measured recommendation is being computed, show NOTHING rather
                than a provisional level. A number that appears and then changes invites
                a decision the user would have to revisit. */}
            {prot?.status === "calculating" ? (
              <div className={`text-[14px] font-semibold ${"text-[var(--t-soft)]"}`}>Protection level</div>
            ) : (
              <div className={`text-[14px] font-semibold ${"text-[var(--t-soft)]"}`}>
                {/* Nominal AND measured. The tier name is a target, not an outcome: the
                    buffer is z x sigma from a normal table while real demand has fatter
                    tails, so every tier lands below its own label. The backtest already
                    knows the real figure — showing only the nominal one let the page
                    promise protection it does not deliver. */}
                {prot?.label || "Standard"} protection ({prot?.servicePct ?? 95}%)
                {prot?.achievedPct != null && (
                  <span className={`ml-1.5 font-mono font-normal text-[14px] ${"text-[var(--t-warn)]"}`}
                        title={`Store-wide, not this product alone: replayed against your sales history, the ${prot.servicePct}% level kept ${prot.achievedPct}% of lead-time windows in stock across all tested products. The level's name is the target; this is what it delivered across your store.`}>
                    · {prot.achievedPct}% store-wide
                  </span>
                )}
                <span className={`ml-1.5 font-mono font-normal text-[14px] ${"text-[var(--t-dim)]"}`}>+{data.safetyStock} units</span>
              </div>
            )}
            <div className={`text-[14px] mt-0.5 ${prot?.status === "calculating" ? ("text-[var(--t-accent)]") : ("text-[var(--t-dim)]")}`}>
              {prot?.status === "calculating" ? (
                // A backtest is in flight, so this number is about to change. Say so
                // rather than presenting the weaker answer as settled.
                <span className="inline-flex items-center gap-1.5">
                  <svg className="h-3 w-3 animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z" />
                  </svg>
                  Calculating…
                </span>
              ) : prot?.testRunning ? (
                <span className="text-[var(--t-accent)] font-semibold">Backtest running now · this may update when it finishes</span>
              ) : prot?.status === "stale" ? "tested for a different lead time or coverage · estimate until re-tested"
                : prot?.source === "backtest" ? "cheapest across your own sales history"
                : prot?.source === "economics" ? "provisional estimate, not yet tested against your history"
                : prot?.costKnown ? `set from ${Math.round(prot.marginPct)}% margin (too little history to test)`
                : "default: add a unit cost to tune"}{prot?.overridden ? " · manual override" : ""}
            </div>
          </div>
        </div>

        {/* Delivery timing row */}
        <div className={`flex items-start gap-2.5 px-3.5 py-2 ${"bg-[var(--t-panel)]"}`}>
          {usingP80 ? (
            <div className={`mt-0.5 h-4 w-4 rounded-full flex items-center justify-center shrink-0 ${"bg-[var(--t-accent-soft)]"}`}>
              <svg className={`h-2.5 w-2.5 ${"text-[var(--t-accent)]"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7"/></svg>
            </div>
          ) : (
            <div className={`mt-0.5 h-4 w-4 rounded-full flex items-center justify-center shrink-0 ${"bg-[var(--t-warn-soft)]"}`}>
              <svg className={`h-2.5 w-2.5 ${"text-[var(--t-warn)]"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>
            </div>
          )}
          <div className="flex-1 min-w-0">
            {usingP80 ? (
              <>
                <div className={`text-[14px] font-semibold ${"text-[var(--t-soft)]"}`}>
                  Late deliveries: P80 active
                  {daysEarlier ? <span className={`ml-1.5 font-mono font-normal text-[14px] ${"text-[var(--t-accent)]"}`}>orders {daysEarlier}d earlier</span> : null}
                </div>
                <div className={`text-[14px] mt-0.5 ${"text-[var(--t-dim)]"}`}>
                  80% of past deliveries arrive within {leadTime}d{daysEarlier ? `, covering up to ${daysEarlier}d delay` : ""}
                </div>
              </>
            ) : (
              <>
                <div className={`text-[14px] font-semibold ${"text-[var(--t-warn)]"}`}>
                  {planSource === "next-order" ? "Next shipment: one-off lead time" : "Late deliveries: no protection"}
                </div>
                <div className={`text-[14px] mt-0.5 ${"text-[var(--t-dim)]"}`}>
                  {planSource === "manual"
                    ? `Pinned lead time (${leadTime}d), ignoring delivery history`
                    : planSource === "next-order"
                    ? `Planning this order on ${leadTime}d; the normal lead time returns after it`
                    : `Estimated lead time (${leadTime}d). Log ${planNeeded || LEAD_TIME_MIN_DELIVERIES} more deliver${(planNeeded || LEAD_TIME_MIN_DELIVERIES) === 1 ? "y" : "ies"} to measure it`}
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// (Forecast-reliability explainer + training-data-quality callout were folded
//  into SkuSignalStrip — a single compact health bar — below.)

// ─── SKU SIGNAL STRIP ─────────────────────────
// One dense, scannable health bar that replaces the old Eligibility +
// Reliability + Training-Data-Quality cards. Pills by default; full detail +
// AI "Why?" on demand. (Stripe / Linear / Bloomberg style: compact, expandable.)
export function SkuSignalStrip({ mlData, statusCfg, apiKey, lm, alwaysOpen = false }) {
  // State resets on SKU change via key={mlData.skuId} at the call site (remount).
  const [openRaw, setOpen]            = useState(false);
  const open = alwaysOpen || openRaw;
  const [explanation, setExplanation] = useState(null);
  const [loading, setLoading]         = useState(false);

  const lowRel = ["YELLOW", "ORANGE", "RED"].includes(mlData.reliabilityColor);
  const explain = async () => {
    setLoading(true); setExplanation(null);
    const prompt = `You are an inventory data analyst. A demand forecast for SKU "${mlData.skuName}" has been flagged as unreliable.\nReliability: ${mlData.reliabilityColor}\nMessage: ${mlData.reliabilityMessage}\nStatus: ${mlData.status} — ${mlData.statusMessage}\nDays of history: ${mlData.daysOfHistory}\nIn 3-4 short bullet points (use • character), explain why the forecast has low reliability, what data issues are causing it, and what the user can do to improve accuracy. Be specific and practical. No markdown, no headers, no em dashes.`;
    try {
      const res = await fetch(GROQ_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${apiKey}` },
        body: JSON.stringify({ model: GROQ_MODEL, messages: [{ role: "user", content: prompt }], max_tokens: 300, temperature: 0.3, ...GROQ_LOW_REASONING }),
      });
      const data = await res.json();
      setExplanation(data?.choices?.[0]?.message?.content || "Could not generate explanation.");
    } catch { setExplanation("Failed to fetch explanation."); }
    finally  { setLoading(false); }
  };

  const relTone    = { GREEN: "emerald", YELLOW: "amber", ORANGE: "orange", RED: "rose" };
  const statusTone = { FORECAST_ELIGIBLE: "emerald", LOW_CONFIDENCE: "amber", INSUFFICIENT: "rose",
                       CONFLICTING: "amber" };   // deprecated grade, kept for stale payloads
  const dataLabel  = { GREEN: "Strong data", YELLOW: "Moderate data", ORANGE: "Limited data", RED: "Thin data" }[mlData.reliabilityColor] || "Strong data";
  const noiseTone  = { GREEN: "emerald", YELLOW: "amber", ORANGE: "orange" }[mlData.demandVolatilityColor];
  const noiseLabel = { GREEN: "Low noise", YELLOW: "Moderate noise", ORANGE: "High noise" }[mlData.demandVolatilityColor];

  let pricePill = null;
  if (mlData.hasPrice && mlData.priceModeled && mlData.priceVaried) {
    const n = mlData.priceTiers?.length || 0;
    pricePill = mlData.priceWellSampled
      ? { tone: "emerald", text: `Price modelled${n ? ` · ${n} tier${n !== 1 ? "s" : ""}` : ""}` }
      : { tone: "amber", text: "Price: limited data" };
  }
  const zeroStock = mlData.stockoutRowsDropped > 0;
  const hasDetail = mlData.statusMessage || mlData.reliabilityMessage || (pricePill && mlData.priceTiers?.length) || zeroStock;

  const card = "bg-[var(--t-panel)] border-[var(--t-line)]";
  const head = "text-[var(--t-dim)]";

  return (
    <div className={`${card} border rounded-2xl px-4 py-3`}>
      <div className="flex items-center gap-2 flex-wrap">
        {!alwaysOpen && <span className={`text-[14px] uppercase tracking-widest font-bold mr-1 ${head}`}>Forecast health</span>}
        {/* Inside Forecast details the grade and data strength are already the headline
            above this, so only the signals not said there are repeated as pills. */}
        {!alwaysOpen && (
          <SignalPill tone={statusTone[mlData.status] || "slate"} lm={lm}>
            <span className={`h-1.5 w-1.5 rounded-full ${statusCfg.dot}`} />{statusCfg.label}
          </SignalPill>
        )}
        {!alwaysOpen && <SignalPill tone={relTone[mlData.reliabilityColor] || "emerald"} lm={lm}>{dataLabel}</SignalPill>}
        {noiseLabel && <SignalPill tone={noiseTone} lm={lm}>{noiseLabel}{mlData.intervalWidth ? ` · ${mlData.intervalWidth}% band` : ""}</SignalPill>}
        {pricePill && !alwaysOpen && <SignalPill tone={pricePill.tone} lm={lm}>{pricePill.text}</SignalPill>}
        {zeroStock && <SignalPill tone="slate" lm={lm}>{mlData.stockoutRowsDropped} zero-stock day{mlData.stockoutRowsDropped > 1 ? "s" : ""} excluded</SignalPill>}
        <div className="flex-1 min-w-[12px]" />
        {lowRel && !explanation && !loading && (
          <button onClick={() => { setOpen(true); explain(); }}
            className={`text-[14px] font-semibold ${"text-[var(--t-warn)] hover:text-[var(--t-warn)]"}`}>Why?</button>
        )}
        {hasDetail && !alwaysOpen && (
          <button onClick={() => setOpen(o => !o)}
            className={`text-[14px] font-semibold ${"text-[var(--t-dim)] hover:text-[var(--t-soft)]"}`}>{open ? "Hide details" : "Details"}</button>
        )}
      </div>

      {open && (
        <div className={`mt-3 pt-3 border-t space-y-3 ${"border-[var(--t-line)]"}`}>
          {mlData.statusMessage && <p className={`text-[15px] leading-relaxed ${"text-[var(--t-dim)]"}`}>{mlData.statusMessage}</p>}
          <ReliabilityBadge color={mlData.reliabilityColor} message={mlData.reliabilityMessage} intervalWidth={mlData.intervalWidth} demandVolatilityColor={mlData.demandVolatilityColor} lm={lm} />
          {zeroStock && (
            <p className={`text-[14px] leading-relaxed ${"text-[var(--t-dim)]"}`}>
              {mlData.stockoutRowsDropped} zero-stock day{mlData.stockoutRowsDropped > 1 ? "s were" : " was"} removed from training so the model doesn't learn artificially low demand during stockouts.
            </p>
          )}
          {/* Price levels live in Price history now, next to the rest of the price story. */}
          {(loading || explanation) && (
            <div>
              {loading && (
                <div className="flex items-center gap-2">
                  <div className="h-2.5 w-2.5 border-2 border-[var(--t-warn-line)] border-t-transparent rounded-full animate-spin" />
                  <span className={`text-[14px] ${"text-[var(--t-warn)]"}`}>Analysing data quality…</span>
                </div>
              )}
              {explanation && <p className={`text-[15px] leading-relaxed whitespace-pre-wrap ${"text-[var(--t-soft)]"}`}>{explanation}</p>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── SKU DETAIL PANEL ─────────────────────────
/* Where this product is in the running backtest, polled about once a second while the
   "Backtest running now" banner is up. The engine reports which product it's fitting, so
   this can say "refitting this product" vs "waiting, 4 ahead" rather than one spinner. */
function RefitProgress({ skuId }) {
  const [st, setSt] = useState(null);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      const d = await fetchJson(`${API}/api/backtest/status?sku=${encodeURIComponent(skuId)}`).catch(() => null);
      if (alive && d) setSt(d.sku ? { ...d.sku, partial: Array.isArray(d.partialSkus) } : null);
    };
    tick();
    const t = setInterval(tick, 1200);
    return () => { alive = false; clearInterval(t); };
  }, [skuId]);
  if (!st) return null;
  const pct = st.fraction == null ? null : Math.round(st.fraction * 100);
  const label = {
    refitting: "Refitting this product",
    done: st.partial ? "This product is refitted · updating the store totals" : "This product is refitted · finishing the rest of the store",
    waiting: st.ahead ? `Waiting · ${st.ahead} product${st.ahead === 1 ? "" : "s"} ahead of this one` : "Starting",
    queued: "Next up · starts when the current test finishes",
    other: `Re-testing ${st.refitting || "other"} other product${st.refitting === 1 ? "" : "s"} · this one isn't refitted, its level may still shift with the store's`,
  }[st.state] || "Working";
  // A queued product has no progress of its own yet; show the current test's.
  return (
    <div className="mt-2">
      <div className="flex items-center justify-between gap-2 text-[13px] text-[var(--t-dim)]">
        <span className="truncate">{label}</span>
        {pct != null && <span style={{ fontFamily: MONO }}>{pct}%</span>}
      </div>
      <div className="mt-1 h-1.5 rounded-full bg-[var(--t-line)] overflow-hidden">
        <div className="h-full rounded-full bg-[var(--t-accent)] transition-[width] duration-700 ease-out"
             style={{ width: `${pct ?? 0}%`, opacity: st.state === "queued" || st.state === "other" ? 0.45 : 1 }} />
      </div>
    </div>
  );
}

export default function SkuDetailPanel({ skuId, skuList, params: paramsIn, onParamChange, openPOs, setOpenPOs, apiKey, lm, suppliers, setSuppliers, receiveStock, refreshNonce, onExport, exporting,
                                        arrival = null, onConfirmArrival = null, onIgnoreArrival = null }) {
  // The page builds a fresh params object on every render; keyed on content so the
  // forecast isn't re-requested (and the draft re-synced) when nothing changed.
  const paramsKey = JSON.stringify(paramsIn ?? {});
  const params = useMemo(() => paramsIn, [paramsKey]);   // eslint-disable-line react-hooks/exhaustive-deps
  const [mlData,      setMlData]      = useState(null);
  const [loading,     setLoading]     = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showPoModal, setShowPoModal] = useState(false);
  const [showEvents,  setShowEvents]  = useState(false);
  const [showForecastDetails, setShowForecastDetails] = useState(false);
  const [showPriceHistory, setShowPriceHistory] = useState(false);
  const [showHolidays, setShowHolidays] = useState(false);
  const [showBufferMath, setShowBufferMath] = useState(false);
  const [showSlowWhy,   setShowSlowWhy]   = useState(false);
  const [slowDraft,     setSlowDraft]     = useState({ open: false, dir: "later", days: "", reason: "" });
  // null when closed; otherwise { ground, sea, air } being edited in the change dialog.
  const [ltChange,      setLtChange]      = useState(null);
  const [draftParams, setDraft]       = useState(params);
  // Draggable width, same as the product sidebar. Kept in a ref-backed handler so the
  // listener doesn't need re-binding on every render.
  const [panelWidth, setPanelWidth]   = useState(() => {
    try {
      const saved = Number(window.localStorage?.getItem("logitrack_detail_w"));
      return Number.isFinite(saved) && saved >= 260 ? saved : 320;
    } catch { return 320; }   // private mode / storage disabled must not break the panel
  });
  const panelRef = useRef(null);
  const startResizePanel = (e) => {
    e.preventDefault();
    const left = panelRef.current?.getBoundingClientRect().left ?? 0;
    const move = (ev) => setPanelWidth(Math.min(640, Math.max(260, ev.clientX - left)));
    const up = () => {
      window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up);
      document.body.style.cursor = ""; document.body.style.userSelect = "";
      setPanelWidth(w => { try { window.localStorage?.setItem("logitrack_detail_w", String(w)); } catch {} return w; });
    };
    document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none";
    window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
  };
  /* Edits are saved as the FIELDS that changed, bound to the product they were made on.
     The whole draft used to be saved: the draft was copied from params only when the
     product changed, so after "Mark received" added stock elsewhere, the next edit here
     saved the old stock back. And the save was addressed by the page to whichever product
     was open when the 600 ms timer fired, so switching products inside that window wrote
     A's settings onto B; closing the panel dropped the pending save altogether. */
  const pendingRef = useRef({ id: null, patch: {} });
  const onParamChangeRef = useRef(onParamChange);
  useEffect(() => { onParamChangeRef.current = onParamChange; }, [onParamChange]);
  const debouncedCommit = useDebouncedCallback(() => {
    const { id, patch } = pendingRef.current;
    pendingRef.current = { id: null, patch: {} };
    if (id && Object.keys(patch).length) onParamChangeRef.current(id, patch);
  }, 600);
  // Switching product or closing the panel saves what was typed, to the product it was
  // typed on, straight away.
  useEffect(() => () => { debouncedCommit.flush(); }, [skuId]);   // eslint-disable-line react-hooks/exhaustive-deps
  // Follow changes made elsewhere (stock received, a supplier change, the Fleet), keeping
  // any edit here that hasn't been saved yet on top.
  useEffect(() => {
    const own = pendingRef.current.id === skuId ? pendingRef.current.patch : {};
    setDraft({ ...params, ...own });
  }, [params, skuId]);
  const updateDraft = (key, value) => updateDraftMany({ [key]: value });
  /* Several fields move together (the default lane's baseline IS params.leadTime).
     Setting them one at a time would clobber, since each call reads the same draft. */
  const updateDraftMany = (patch) => {
    if (pendingRef.current.id && pendingRef.current.id !== skuId) debouncedCommit.flush();
    pendingRef.current = { id: skuId, patch: { ...pendingRef.current.patch, ...patch } };
    setDraft(d => ({ ...d, ...patch }));
    debouncedCommit();
  };
  /* The count-confirmation. Holds the typed value until the person says it is real. */
  const [pendingCount, setPendingCount] = useState(null);
  /* What's being typed over an UNCOUNTED placeholder, held here until the person finishes
     (Enter or leaving the box). The confirm used to open on the first keystroke with the
     box still showing the placeholder, so typing 120 asked "record 1 units?" and a
     habitual Yes recorded a count of 1. */
  const [stockTyping, setStockTyping] = useState(null);
  useEffect(() => { setStockTyping(null); setPendingCount(null); }, [skuId]);
  const askToRecordCount = () => {
    if (stockTyping == null) return;
    const txt = String(stockTyping).trim();
    const v = Math.round(Number(txt));
    if (txt === "" || !Number.isFinite(v) || v < 0 || v === draftParams.stock) { setStockTyping(null); return; }
    setPendingCount(v);
  };
  const cancelCount = () => { setPendingCount(null); setStockTyping(null); };
  const confirmCount = () => {
    updateDraftMany({
      stock: pendingCount,
      stockSource: "manual",
      // WHEN, not just how many. A count is only true until the next sale, so the moment
      // it was taken is what lets the tool subtract what has sold since and keep it true.
      // Without this the number is right once and drifts from then on.
      stockCountedAt: new Date().toISOString(),
    });
    setPendingCount(null);
    setStockTyping(null);
  };

  const openPO     = openPOs[skuId];
  const onOrderQty = openPO ? openPO.qty : 0;
  // When it lands, not just how much — see poEtaDays.
  const onOrderEta = poEtaDays(openPO);
  // The lead time actually PLANNED with: the P80 of completed deliveries once there are
  // enough of them, otherwise your number. Manual mode always wins.
  const plan            = planningLeadTime(skuId, params, suppliers);
  // Read tracks off the DRAFT so a baseline you're typing updates the row you're typing in.
  const tracks          = leadTimeTracks(skuId, draftParams, suppliers);
  const supplierName    = supplierOf(skuId, suppliers)?.name || null;
  // With no default lane declared, planning pools every delivery — so show that as its
  // own row rather than leaving the table with nothing to say about what's in force.
  const activeTrack     = draftParams.freightMode
    ? (tracks.find(t => t.mode === draftParams.freightMode) || null)
    : (() => {
        const all = computeSkuLeadTimeStats(skuId, suppliers, latestChangeAt(draftParams));
        const measured = all.n >= LEAD_TIME_MIN_DELIVERIES ? all.p80 : null;
        return { mode: null, label: "every logged delivery", baseline: Number(draftParams.leadTime) || null,
                 n: all.n, avg: all.avg, p80: measured, plan: measured ?? Number(draftParams.leadTime),
                 needed: Math.max(0, LEAD_TIME_MIN_DELIVERIES - all.n) };
      })();
  /* Measured stats START OVER at the latest change. Before, this one call had no cutoff,
     so the moment you declared a new arrangement the panel went on showing an average and
     a P80 built entirely from the old one — the exact numbers the change said were no
     longer true. Now it reads the same window planning does: nothing before the change,
     rebuilt delivery by delivery as new ones land. */
  const changes         = readLeadTimeChanges(draftParams);
  const cutoff          = latestChangeAt(draftParams);
  const skuSt           = computeSkuLeadTimeStats(skuId, suppliers, cutoff);
  const skuStAll        = computeSkuLeadTimeStats(skuId, suppliers);   // for "set aside" counts only
  /* The one-off adjustment, and what the lane would do WITHOUT it. Both directions are
     expressed against this number, so it has to be the same figure the form previews,
     the banner compares to, and the backtest keeps running against. */
  const oneOff          = readSlowShipment(draftParams);
  const normalPlan      = planningLeadTime(skuId, draftParams, suppliers, { ignoreOneOff: true }).days;
  const demandStratRef    = useRef("balanced"); // updated after each fetch from reliability signal
  const mlDataRef         = useRef(null);       // tracks latest mlData without causing fetch deps
  const hasBootstrapped   = useRef(false);      // true once first corrected fetch for this SKU is done
  useEffect(() => { mlDataRef.current = mlData; }, [mlData]);
  useEffect(() => {
    // Reset all per-SKU refs when SKU changes
    demandStratRef.current  = "balanced";
    mlDataRef.current       = null;
    hasBootstrapped.current = false;
  }, [skuId]);
  const fetchSeq = useRef(0);
  // The previous product's figures never show under this one's name while it loads.
  useEffect(() => { setMlData(null); }, [skuId]);
  const fetchData  = useCallback(() => {
    if (!skuId) return;
    // After bootstrap, read correct strategy from latest data
    if (mlDataRef.current?.demandVolatilityColor) {
      demandStratRef.current = autoStrategy(mlDataRef.current.demandVolatilityColor);
    }
    setLoading(true);
    // Only the newest request may show. A slow answer for the product you just left (or
    // for settings you've since changed) used to land on top of the right one.
    const reqId = ++fetchSeq.current;
    const current = () => reqId === fetchSeq.current;
    // The shared builder (lib/api.js). This page used to build its own URL and leave out
    // where the stock figure came from, so a count typed here read as "not counted": no
    // reorder date and "order 0" on this page while the Fleet showed it overdue.
    const buildUrl = strat => forecastUrl(skuId, params, {
      leadTime: plan.days, months: params.months, onOrderQty, onOrderEta, strategy: strat });
    const stratUsed = demandStratRef.current;
    fetchJson(buildUrl(stratUsed), undefined, `Forecast for ${skuId}`).then(d => {
      if (!current()) return;
      if (!d) { setLoading(false); return; }
      const correctStrat = autoStrategy(d.demandVolatilityColor);
      demandStratRef.current = correctStrat;
      if (!hasBootstrapped.current && stratUsed !== correctStrat) {
        // First fetch used wrong strategy — silently re-fetch with correct one before showing
        hasBootstrapped.current = true;
        fetchJson(buildUrl(correctStrat), undefined, `Forecast for ${skuId}`)
          .then(d2 => { if (!current()) return; setMlData(d2 || d); setLoading(false); });
      } else {
        hasBootstrapped.current = true;
        setMlData(d);
        setLoading(false);
      }
    });
  }, [skuId, params, onOrderQty, onOrderEta, plan.days, refreshNonce]);
  useEffect(() => { fetchData(); }, [fetchData]);
  const handleSaveEvents = async (events) => {
    const res = await fetchJson(`${API}/api/skus/${encodeURIComponent(skuId)}/events`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(events),
    }, "Saving events");
    // On failure the toast carries the backend's validation message; keep the
    // modal open so the user can fix the event instead of silently losing it.
    if (res) { setShowEvents(false); fetchData(); }
  };
  const handleSavePO = (poIn) => {
    /* Editing the PO that's already open updates ITS supplier order. Saving again used to
       file a second order for the same shipment. A new PO gets a new order, and the PO
       carries that order's id, so marking one received never touches another. */
    const prevPo = openPOs[skuId];
    const linked = prevPo ? findPoOrder(suppliers, skuId, prevPo) : null;
    const orderId = linked?.order?.id || makeOrdId();
    const po = { ...poIn, orderId };
    const next = { ...openPOs, [skuId]: po }; setOpenPOs(next); saveStorage("logitrack_pos", next); setShowPoModal(false);
    const sameSupplier = linked && !poIn.newSupplierName &&
      ((poIn.supplierId || null) === (linked.supId === UNASSIGNED_SUP_ID ? null : linked.supId));
    if (linked && setSuppliers && !sameSupplier) {
      // Moved to another supplier: take the order out of the old one's history; it's
      // filed under the new one below, keeping its id.
      setSuppliers(prev => ({ ...prev, [linked.supId]: { ...prev[linked.supId],
        orders: (prev[linked.supId]?.orders || []).filter(o => o.id !== orderId) } }));
    }
    if (linked && setSuppliers && sameSupplier) {
      setSuppliers(prev => updateOrder(prev, linked.supId, orderId, {
        orderedDate: po.ordered, expectedDate: po.delivery, qty: po.qty,
        freightMode: po.freightMode ?? linked.order.freightMode ?? null,
        ...(po.notes ? { notes: po.notes } : {}),
      }));
      return;
    }
    // The one-off "next order will be slower" expectation was ABOUT this order. Placing
    // it consumes the expectation — otherwise it silently inflates every future
    // recommendation forever, and nothing in the UI would tell you why.
    // ...and the reason travels WITH it: the floating note becomes the order's note, so
    // the explanation ends up filed against the shipment it was about instead of
    // evaporating when the expectation is consumed.
    const slow = readSlowShipment(params);
    if (slow) updateDraftMany({ slowShipment: undefined, nextLeadTime: undefined });
    /* The order record is built ONCE and filed either against a supplier or, when
       there isn't one, in the holding ledger. It used to be built only inside the
       "if (supId)" branch, so an order with no supplier was never written down at
       all: the PO sat in openPOs, and on arrival handleClearPO added the stock and
       deleted it, taking both dates with it. That product could never learn its own
       lead time however many orders went through it. Every field is the same either
       way — a parked order is a complete record, just not filed under anyone yet. */
    let supId = po.supplierId;
    if (po.newSupplierName && setSuppliers) {
      supId = makeSupId();
      setSuppliers(prev => ({ ...prev, [supId]: { id: supId, name: po.newSupplierName, skuIds: [skuId], orders: [] } }));
    }
    if (!setSuppliers) return;
    const order = {
      id: orderId, orderedDate: po.ordered, receivedDate: null,
      expectedDate: po.delivery, qty: po.qty, skuId,
      freightMode: po.freightMode ?? params.freightMode ?? null,
      notes: [po.notes, slowShipmentNote(slow)].filter(Boolean).join(" · "),
      expectedSlow: slow ? true : undefined,
      slowReason: slow?.reason || undefined,
      slowDays: slow?.days || undefined,
    };
    setSuppliers(prev => {
      // A brand-new supplier isn't in `prev` yet on this pass — the setState above
      // hasn't flushed — so trust supId over a lookup that would still miss it.
      const target = po.newSupplierName ? supId : (supId && prev?.[supId] ? supId : null);
      if (!target) return parkOrder(prev, order);
      const sup = prev[target] || { id: target, name: po.newSupplierName, skuIds: [skuId], orders: [] };
      // Assigning by logging an order also claims anything parked for this product.
      const withOrder = { ...prev, [target]: { ...sup, orders: [...(sup.orders || []), order] } };
      return adoptParkedOrders(withOrder, target, skuId);
    });
  };
  const handleClearPO = () => {
    const po = openPOs[skuId];
    // The PO has arrived: add its quantity to on-hand stock.
    if (po?.qty && receiveStock) receiveStock(skuId, po.qty);
    // Mark ITS supplier order received, so it can't be counted again. This used to mark
    // the first open order for the product, which with two on the water was often the
    // other one.
    const linked = po ? findPoOrder(suppliers, skuId, po) : null;
    if (linked && setSuppliers) {
      setSuppliers(prev => updateOrder(prev, linked.supId, linked.order.id, { receivedDate: todayStr() }));
    }
    const next = { ...openPOs }; delete next[skuId]; setOpenPOs(next); saveStorage("logitrack_pos", next); setShowPoModal(false);
  };

  if (!mlData) return (
    <div className="flex-1 flex items-center justify-center">
      <div className="flex flex-col items-center gap-3">
        <div className="h-6 w-6 border-2 border-[var(--t-accent-line)] border-t-transparent rounded-full animate-spin" />
        <span className={`text-[15px] font-mono ${"text-[var(--t-dim)]"}`}>Loading forecast…</span>
      </div>
    </div>
  );

  const statusCfg = STATUS_CONFIG[mlData.status] || STATUS_CONFIG.INSUFFICIENT;
  /* The grade says how much to trust the forecast; the reason says why, without the reader
     parsing a paragraph to find out. Separating them is the point of the rework. */
  const statusReason = REASON_LABELS[mlData.statusReason] || null;
  /* An uncounted product shows "—", not a number. A 0 nobody has verified is not the same
     claim as a counted 0, and rendering them identically is what turned an unknown into an
     urgent reorder (or, with the old 500 default, into "plenty in stock, nothing to do"). */
  const stockCounted = stockIsCounted(params);
  const projTotal = mlData.currentMonth.unitsSoFar + mlData.currentMonth.forecastRemaining;
  const pctChange = mlData.currentMonth.lastMonthTotal > 0
    ? ((projTotal - mlData.currentMonth.lastMonthTotal) / mlData.currentMonth.lastMonthTotal) * 100 : 0;
  // The same month a year ago: the season cancels out, so this is the one that says
  // whether the product is growing. Absent until there's a full year of history.
  const lyTotal = mlData.currentMonth.lastYearTotal;
  const pctYoY = lyTotal > 0 ? ((projTotal - lyTotal) / lyTotal) * 100 : null;
  const hasOpenPO   = !!openPO;
  /* plan.days, not the default. The reorder bands are a fraction of a product's OWN
     lead time (at-risk = lead/2, due = lead x1.5), and omitting it fell back to 14 days
     for everything — so a 60-day sea-freight line read "due within 90 days" on the Fleet
     (which does pass it) and "due within 21 days" here. Same product, same moment, two
     answers. It is the same lead time the forecast itself was requested with on line 344. */
  const urgency     = urgencyLevel(mlData.daysUntilReorder, hasOpenPO, plan.days);
  const showReorder = mlData.daysUntilReorder != null && !hasOpenPO;

  // Theming shortcuts
  const sidebarBg  = "bg-[var(--t-sunken)] border-[var(--t-line)]";
  const lbl        = "block text-[15px] text-[var(--t-soft)] font-semibold uppercase tracking-widest mb-1.5";
  const inp        = "w-full bg-[var(--t-panel)] border border-[var(--t-line2)] rounded-lg px-3 py-2 text-[16.5px] text-[var(--t-ink)] focus:outline-none focus:border-[var(--t-accent)]";
  const divider    = "border-[var(--t-line)]";
  const infoHead   = "text-[14px] uppercase tracking-widest text-[var(--t-dim)] font-bold";
  const infoLabel  = "text-[var(--t-dim)] text-[15px]";
  const infoVal    = "text-[var(--t-ink)]";
  const mainBg     = "bg-[var(--t-sunken)]";
  const cardBg     = "bg-[var(--t-panel)] border-[var(--t-line)]";
  const textMain   = "text-[var(--t-ink)]";
  const textMuted  = "text-[var(--t-dim)]";

  // CI button active/inactive
  const ciBtnActive   = "bg-[var(--t-accent-soft)] border-[var(--t-accent-line)] ring-1 ring-[var(--t-accent-line)]";
  const ciBtnInactive = "bg-[var(--t-panel)] border-[var(--t-line2)] hover:border-[var(--t-line2)]";
  const ciLblActive   = "text-[var(--t-accent)]";
  const ciLblInactive = "text-[var(--t-soft)]";
  const ciCiActive    = "text-[var(--t-accent)]";
  const ciCiInactive  = "text-[var(--t-dim)]";
  const ciSub         = "text-[var(--t-dim)]";

  // Month buttons
  const moBtnActive   = "bg-[var(--t-btn-bg)] border-[var(--t-accent-line)] text-[var(--t-btn-fg)]";
  const moBtnInactive = "bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-soft)] hover:border-[var(--t-line2)] hover:text-[var(--t-ink)]";

  // On order badge
  const poCard = "bg-[var(--t-accent-soft)] border-[var(--t-accent-line)]";
  const poHead = "text-[var(--t-accent)]";
  const poText = "text-[var(--t-soft)]";

  /* One block of variables at the root instead of the 228 `lm ? light : dark`
     ternaries this file used to carry. The theme flips here; everything below reads
     tokens and no longer knows or cares which mode it is in. */
  const T = terminal(lm);
  const VARS = {
    "--t-bg": T.bg, "--t-panel": T.panel, "--t-sunken": T.sunken,
    "--t-ink": T.ink, "--t-soft": T.soft, "--t-dim": T.dim, "--t-faint": T.faint,
    "--t-line": T.line, "--t-line2": T.line2,
    "--t-accent": T.amber, "--t-accent-soft": `${T.amber}14`, "--t-accent-line": `${T.amber}55`,
    "--t-good": T.green, "--t-good-soft": `${T.green}16`, "--t-good-line": `${T.green}55`,
    "--t-warn": T.over,  "--t-warn-soft": `${T.over}16`,  "--t-warn-line": `${T.over}55`,
    "--t-bad": T.red,    "--t-bad-soft": `${T.red}14`,    "--t-bad-line": `${T.red}55`,
    "--t-info": T.blue,  "--t-info-soft": `${T.blue}14`,  "--t-info-line": `${T.blue}55`,
    "--t-btn-bg": T.btnBg, "--t-btn-fg": T.btnFg,
  };
  return (
    <div className="flex h-full" style={{ ...VARS, background: T.bg, color: T.ink, fontFamily: SANS }}>
      {/* Figures are monospaced; prose is not. Applied by class so the file's existing
          `tabular-nums` and `font-mono` cells pick the face up without touching each one. */}
      <style>{`.tabular-nums,.font-mono{font-family:${MONO};font-variant-numeric:tabular-nums}`}</style>
      {/* Forecast "why" panel — opened from the Forecast details button in the header */}
      <ForecastDetailsDrawer data={mlData} lm={lm} open={showForecastDetails} onClose={() => setShowForecastDetails(false)}
        health={mlData ? forecastHealth(mlData, STATUS_CONFIG[mlData.status] || STATUS_CONFIG.INSUFFICIENT) : null}
        healthBody={mlData ? <SkuSignalStrip key={mlData.skuId} mlData={mlData}
          statusCfg={STATUS_CONFIG[mlData.status] || STATUS_CONFIG.INSUFFICIENT} apiKey={apiKey} lm={lm} alwaysOpen /> : null} />
      {mlData && <PriceHistoryDrawer open={showPriceHistory} onClose={() => setShowPriceHistory(false)} mlData={mlData} />}
      {mlData && <HolidaysDrawer open={showHolidays} onClose={() => setShowHolidays(false)} mlData={mlData} onChanged={fetchData} />}
      {/* Promoting a placeholder to a real count. Confirmed once, because it changes what
          the tool is willing to claim about this product from here on. */}
      {pendingCount !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
             onClick={cancelCount}>
          <div onClick={e => e.stopPropagation()}
               className={`w-[min(440px,92vw)] rounded-xl border p-5 space-y-3 ${"bg-[var(--t-panel)] border-[var(--t-line2)]"}`}>
            <div className={`text-[16px] font-semibold ${"text-[var(--t-ink)]"}`}>
              Record {pendingCount} units as a real count?
            </div>
            <p className={`text-[14px] leading-relaxed ${"text-[var(--t-soft)]"}`}>
              The {params.stock} shown now is a placeholder, since your POS reports no level
              for this product. Saving {pendingCount} records a real count taken right now.
              From then on, sales are subtracted to keep it current: closed days from your
              sales history, today&apos;s sales from the hourly check.
            </p>
            <p className={`text-[14px] leading-relaxed ${"text-[var(--t-soft)]"}`}>
              Days of cover, stockout date, reorder date and order quantity are hidden while
              stock is a placeholder. Saving makes them appear.
            </p>
            <div className="flex gap-2 justify-end pt-1">
              <button onClick={cancelCount}
                className={`px-3 py-1.5 rounded-lg text-[14px] border ${"border-[var(--t-line2)] text-[var(--t-soft)] hover:bg-[var(--t-sunken)]"}`}>
                Cancel
              </button>
              <button onClick={confirmCount}
                className={`px-3 py-1.5 rounded-lg text-[14px] font-semibold ${"bg-[var(--t-accent)] text-[var(--t-btn-fg)]"}`}>
                Yes, this is a real count
              </button>
            </div>
          </div>
        </div>
      )}
      {/* Params panel */}
      <div ref={panelRef} style={{ width: panelWidth }}
        className={`relative shrink-0 ${sidebarBg} border-r flex flex-col`}>
        <div onMouseDown={startResizePanel} title="Drag to resize"
          className={`absolute top-0 right-0 h-full w-1.5 cursor-col-resize z-30 ${"hover:bg-[var(--t-line2)]"}`} />
        <div className="flex-1 flex flex-col p-5 gap-5 overflow-y-auto sku-scroll">
        <div className="space-y-4">
          <div>
            <label className={lbl}>Units in Stock</label>
            <input type="number" value={stockTyping ?? draftParams.stock}
              onBlur={() => { if (!stockCounted) askToRecordCount(); }}
              onKeyDown={e => { if (e.key === "Enter" && !stockCounted) { e.preventDefault(); askToRecordCount(); } }}
              onChange={e => {
                if (!stockCounted) { setStockTyping(e.target.value); return; }
                const v = Number(e.target.value);
                /* Editing an UNCOUNTED figure is a different act from adjusting a counted
                   one. The number on screen was a placeholder; typing over it is the
                   moment this product stops being a guess. That is worth confirming once
                   — partly so nobody promotes a placeholder by leaning on the arrow keys,
                   and partly because everything downstream changes at that instant: the
                   stockout date, the reorder countdown and the order quantity all appear,
                   having been deliberately withheld while the level was invented. */
                updateDraft("stock", v);
              }} className={inp} />
            {stockCounted ? (
              <StockSinceCount data={mlData} params={draftParams} lm={lm} />
            ) : (
              <p className={`text-[13px] mt-1 ${"text-[var(--t-warn)]"}`}>
                Placeholder: your POS reports no level for this product. Cover, stockout
                and reorder dates are hidden until you enter a real count.
              </p>
            )}
          </div>

          {/* Unit economics. Cost and fees used to be editable ONLY in the Scorecard
              tab, while the protection card six inches below this said "add a unit
              cost to tune" — pointing at a control on a different screen. They live
              here now, next to the thing they change. */}
          <UnitEconomicsCard lm={lm} params={draftParams}
            price={mlData?.currentPrice ?? mlData?.lastPrice ?? null}
            listPrice={mlData?.listPrice ?? null} livePrice={mlData?.livePrice ?? null}
            onChange={updateDraftMany} />
          {/* ── Lead Time ──
              The "Lead Time (days)" summary card that used to head this section is gone.
              It restated one number — the default lane's — that the table below already
              shows in its own highlighted row, and its caption ("a placeholder until you
              log deliveries. Set it per shipping method in the table below") did nothing
              but point at the table it was sitting on top of. The number it reported and
              the number in the table could also disagree, because the card read the
              resolved plan while the table's input read only this product's own params.
              One table, one number, no card to drift from it. */}
          <div className="space-y-2">

            {/* Per-lane tracks. Ground/Sea/Air are separate shipping lanes and never
                pool: a sea crossing says nothing about how long air takes. Lanes are
                created lazily, so a product that only ships one way shows one row with a
                number and the rest blank — blank being a clearer signal than a guess. */}
            <div className={`rounded-xl border overflow-hidden ${"bg-[var(--t-sunken)] border-[var(--t-line)]"}`}>
              <div className={`px-3 py-2 flex items-center justify-between border-b ${"border-[var(--t-line)]"}`}>
                <span className={`text-[13px] uppercase tracking-widest font-bold ${"text-[var(--t-dim)]"}`}>
                  Lead time by shipping method
                </span>
                <label className="flex items-center gap-1.5">
                  <span className={`text-[13px] ${"text-[var(--t-dim)]"}`}>Usually ships by</span>
                  <select value={draftParams.freightMode || resolveLane(skuId, draftParams, suppliers) || Object.keys(FREIGHT_MODES)[0]}
                    onChange={e => updateDraft("freightMode", e.target.value)}
                    className={`rounded border px-1.5 py-0.5 text-[14px] ${"bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-ink)]"}`}>

                    {Object.entries(FREIGHT_MODES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                  </select>
                </label>
              </div>
              <div className={`grid grid-cols-[1fr_auto_auto_auto] gap-x-3 px-3 py-1.5 text-[13px] uppercase tracking-wider font-semibold ${"text-[var(--t-dim)]"}`}>
                <span>Method</span><span className="text-right">Baseline</span>
                {/* "Deliveries", not "Ships" or "Orders" — this counts shipments that
                    ARRIVED (both an ordered and a received date). An outstanding PO isn't
                    in it, so "Orders" overstated what the number covers, and the supplier
                    tables called the identical figure something else again. */}
                <span className="text-right">P80</span><span className="text-right">Deliveries</span>
              </div>
              {tracks.map(t => (
                <div key={t.mode}
                  className={`grid grid-cols-[1fr_auto_auto_auto] gap-x-3 items-center px-3 py-1.5 border-t text-[14px] ${"border-[var(--t-line)]"} ${t.isDefault ? ("bg-[var(--t-accent-soft)]") : ""}`}>
                  <span className={`font-medium ${t.used ? ("text-[var(--t-soft)]") : ("text-[var(--t-dim)]")}`}>
                    {t.label}
                    {t.isDefault && <span className={`ml-1.5 text-[12.5px] uppercase tracking-wide ${"text-[var(--t-accent)]"}`}>default</span>}
                  </span>
                  {/* `t.baseline` is the RESOLVED number for this lane — this product's
                      own, else the one inherited from its supplier, else (for the default
                      lane) params.leadTime. This used to read only the product's own
                      params, so a supplier-wide baseline planned the product silently
                      while its row sat blank, and the default row showed a stale
                      params.leadTime that wasn't what planning used. Clearing the box
                      still means "inherit": the supplier's number reappears, italic. */}
                  <input type="number" placeholder="-"
                    value={t.baseline ?? ""}
                    onChange={e => {
                      const v = e.target.value === "" ? undefined : Number(e.target.value);
                      const patch = {
                        leadTimes: { ...(draftParams.leadTimes || {}), [t.mode]: { ...(draftParams.leadTimes?.[t.mode] || {}), baseline: v } },
                      };
                      // Filling in a lane when none is chosen yet declares it — you told us
                      // how this ships by typing in that row, so don't also demand a dropdown.
                      if (!draftParams.freightMode) patch.freightMode = t.mode;
                      // The chosen lane IS params.leadTime; a dozen other screens read that
                      // field, so keep the two in lockstep rather than letting them drift.
                      // undefined, never "" — an empty string survives `??` and then
                      // Number("") is 0, which is not a lead time any endpoint accepts.
                      if (t.isDefault || !draftParams.freightMode) patch.leadTime = v;
                      updateDraftMany(patch);
                    }}
                    title={t.inherited ? `Inherited from ${supplierName || "this supplier"}. Type here to give this product its own.` : undefined}
                    className={`w-16 text-right rounded border px-1.5 py-0.5 font-mono text-[14px] ${
                      t.inherited
                        ? ("bg-[var(--t-panel)] border-[var(--t-line)] text-[var(--t-dim)] italic")
                        : ("bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-ink)]")}`} />
                  <span className={`text-right font-mono tabular-nums w-12 ${t.p80 != null ? ("text-[var(--t-good)] font-bold") : ("text-[var(--t-soft)]")}`}>
                    {t.p80 != null ? `${t.p80}d` : "-"}
                  </span>
                  <span className={`text-right font-mono tabular-nums w-8 ${"text-[var(--t-dim)]"}`}>{t.n || "-"}</span>
                </div>
              ))}
              {/* Carries the resolved planning number now that the card above is gone —
                  the one figure the whole section exists to produce, stated once. The
                  manual branch also carries the only way out of manual mode, which used
                  to live on the card. */}
              <div className={`px-3 py-2 border-t text-[13px] ${"border-[var(--t-line)] text-[var(--t-dim)]"}`}>
                {params.leadTimeMode === "manual"
                  ? <>Planning on <b>{plan.days}d</b>: pinned by you, ignoring delivery history.{" "}
                      <button onClick={() => updateDraft("leadTimeMode", undefined)}
                        className={`font-semibold underline ${"text-[var(--t-soft)]"}`}>measure it instead</button></>
                  : activeTrack.p80 != null
                  ? <>Planning on <b>{plan.days}d</b>: measured {activeTrack.label}, P80 of {activeTrack.n} shipments. Your baseline is used only if that record goes away. Editing it never drops a delivery.</>
                  : draftParams.freightMode
                    ? <>Planning on <b>{plan.days}d</b>: {activeTrack.inherited ? `${supplierName || "supplier"}'s` : "your"} {activeTrack.label} baseline. {activeTrack.needed} more {activeTrack.label} delivery{activeTrack.needed === 1 ? "" : " deliveries"} and it switches to this product&apos;s own measured P80. Other products&apos; shipments are never mixed in.</>
                    : <>Fill in the row for how this product ships to make it the planning method. Until then it pools every delivery.</>}
              </div>
              {/* The opt-out checkbox that used to sit here is gone. It existed back when
                  a product could inherit its supplier's MEASURED average — pooling that
                  once dragged a whole catalogue's planning from 44 days to 90 because one
                  made-to-order sibling shared the pool. That pooling was removed; all a
                  supplier passes down now is a baseline you typed, and typing a number in
                  the row above already overrides it. A checkbox to refuse an inheritance
                  you can simply overwrite is a switch with nothing behind it. */}
              {tracks.some(t => t.inherited) && (
                <div className={`px-3 py-1.5 border-t text-[13px] ${"border-[var(--t-line)] text-[var(--t-dim)]"}`}>
                  Greyed numbers come from {supplierName || "this supplier"}. Type over one to make it this product&apos;s own.
                </div>
              )}
            </div>

            {/* Declaring a permanent change: a CHANGEPOINT, not a reset. Old deliveries
                are set aside, never deleted, so undoing it restores the previous
                measurement exactly — which is what makes it safe to try. */}
            {/* Every change, newest first. Only the newest can be undone: rolling back an
                older one while a newer stands would leave the baselines describing an
                arrangement that never existed. Undo the newest and the one below it
                becomes undoable in turn, all the way back to the original numbers. */}
            {changes.length > 0 && (
              <div className={`rounded-xl border overflow-hidden ${"bg-[var(--t-sunken)] border-[var(--t-line)]"}`}>
                <div className={`px-3 py-1.5 text-[13px] uppercase tracking-widest font-bold border-b ${"text-[var(--t-dim)] border-[var(--t-line)]"}`}>
                  Lead time changes ({changes.length})
                </div>
                {changes.slice().reverse().map((c, i) => (
                  <div key={c.at + i} className={`px-3 py-2 flex items-start gap-2 border-b last:border-b-0 ${"border-[var(--t-line)]"}`}>
                    <div className="flex-1 min-w-0">
                      <div className={`text-[14px] font-semibold ${i === 0 ? ("text-[var(--t-ink)]") : ("text-[var(--t-dim)]")}`}>
                        {describeLeadTimeChange(c)}
                      </div>
                      <div className={`text-[13px] ${"text-[var(--t-dim)]"}`}>
                        {isoToDisplay(c.at)}
                        {i === 0
                          ? <> · measuring from here{plan.supersededN > 0 ? ` · ${plan.supersededN} earlier ${plan.supersededN === 1 ? "delivery" : "deliveries"} set aside` : ""}</>
                          : " · superseded"}
                      </div>
                    </div>
                    {i === 0 && (
                      <button onClick={() => { const patch = undoLastLeadTimeChange(draftParams); if (patch) updateDraftMany(patch); }}
                        className={`shrink-0 text-[13px] font-semibold px-2 py-1 rounded-lg border transition-all ${"bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-soft)] hover:border-[var(--t-line2)]"}`}>
                        Undo
                      </button>
                    )}
                  </div>
                ))}
                <div className={`px-3 py-1.5 text-[14px] border-t ${"border-[var(--t-line)] text-[var(--t-dim)]"}`}>
                  Measured figures restart at the newest change, so P80 rebuilds from deliveries logged since then.
                  {plan.priorP80 ? ` The previous arrangement measured ${plan.priorP80}d.` : ""}
                  {" "}Nothing is deleted. Undo brings the older deliveries straight back.
                </div>
              </div>
            )}

            {/* Declaring a permanent change used to be a faint underlined link that set a
                changepoint and then left you to edit the baselines yourself — so the tool
                knew the OLD numbers were wrong but had to guess the new ones from whatever
                you typed next, if anything. Asking for all three up front means the change
                and the numbers arrive together.

                Still a CHANGEPOINT, not a reset: old deliveries are set aside, never
                deleted, so undo restores the previous measurement exactly. That's what
                makes it safe to try. */}
            {ltChange ? (
              <div className={`rounded-xl border px-3 py-2.5 space-y-2 ${"bg-[var(--t-panel)] border-[var(--t-accent-line)]"}`}>
                <div className={`text-[14px] font-bold ${"text-[var(--t-ink)]"}`}>New lead times from today</div>
                <div className={`text-[13px] ${"text-[var(--t-dim)]"}`}>
                  Change what moved. Anything you don&apos;t touch keeps its current number.
                </div>
                {tracks.map(t => (
                  <div key={t.mode} className="flex items-center gap-2">
                    <span className={`text-[14px] w-14 shrink-0 ${"text-[var(--t-soft)]"}`}>{t.label}</span>
                    <input type="number" min="1" placeholder="-"
                      value={ltChange[t.mode] ?? ""}
                      onChange={e => setLtChange(c => ({ ...c, [t.mode]: e.target.value }))}
                      className={`w-16 text-right rounded border px-1.5 py-0.5 font-mono text-[14px] ${"bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-ink)]"}`} />
                    <span className={`text-[13px] ${"text-[var(--t-dim)]"}`}>
                      d{t.baseline != null && Number(ltChange[t.mode]) !== t.baseline ? ` · was ${t.baseline}d` : ""}
                    </span>
                  </div>
                ))}
                <div className="flex items-center gap-2 pt-0.5">
                  <button
                    onClick={() => {
                      // Stacks onto the history rather than replacing it, and records what
                      // each lane was so the entry can be undone exactly.
                      const patch = applyLeadTimeChange(draftParams, ltChange);
                      if (patch) {
                        // The chosen lane IS params.leadTime — a dozen screens read that
                        // field, so keep the two in lockstep rather than letting them drift.
                        const dflt = draftParams.freightMode || resolveLane(skuId, draftParams, suppliers);
                        if (dflt && patch.leadTimes?.[dflt]?.baseline > 0) patch.leadTime = patch.leadTimes[dflt].baseline;
                        updateDraftMany(patch);
                      }
                      setLtChange(null);
                    }}
                    className="text-[14px] font-bold px-3 py-1.5 rounded-lg bg-[var(--t-btn-bg)] hover:bg-[var(--t-btn-bg)] text-[var(--t-btn-fg)] transition-colors">
                    Apply from today
                  </button>
                  <button onClick={() => setLtChange(null)}
                    className={`text-[14px] px-2 py-1.5 ${"text-[var(--t-dim)] hover:text-[var(--t-soft)]"}`}>cancel</button>
                </div>
                <div className={`text-[14px] ${"text-[var(--t-dim)]"}`}>
                  Deliveries before today are set aside, not deleted. Undo brings them back.
                </div>
              </div>
            ) : (
              <button
                onClick={() => setLtChange(Object.fromEntries(tracks.map(t => [t.mode, t.baseline ?? ""])))}
                className={`w-full text-[14px] font-semibold py-1.5 rounded-lg border transition-all ${"bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-soft)] hover:border-[var(--t-accent-line)] hover:text-[var(--t-accent)]"}`}>
                Lead times have changed
              </button>
            )}

            {/* A delay you're expecting on the next order. It floats here — carrying its
                reason — until a PO is actually raised against it, at which point it
                becomes that order's note and stops floating. Discard it and nothing was
                ever written. */}
            {plan.source === "next-order" ? (() => {
              const early = oneOff && oneOff.delta != null && oneOff.delta < 0;
              const tone = early
                ? ("bg-[var(--t-info-soft)] border-[var(--t-info-line)]")
                : ("bg-[var(--t-warn-soft)] border-[var(--t-warn-line)]");
              const ink = early
                ? ("text-[var(--t-info)]")
                : ("text-[var(--t-warn)]");
              return (
              <div className={`rounded-lg border px-2.5 py-2 ${tone}`}>
                <div className="flex items-baseline gap-2">
                  <span className={`text-lg font-bold tabular-nums ${ink}`}>{plan.days}d</span>
                  <span className={`text-[14px] ${ink}`}>
                    on the next order
                    {oneOff?.delta ? ` · ${oneOff.delta > 0 ? "+" : ""}${oneOff.delta}d vs the usual ${normalPlan}d` : ""}
                  </span>
                  <button onClick={() => updateDraftMany({ slowShipment: undefined, nextLeadTime: undefined })}
                    className={`ml-auto text-[14px] font-semibold underline ${"text-[var(--t-dim)]"}`}>discard</button>
                </div>
                {plan.reason && (
                  <div className={`text-[14px] mt-1 italic ${ink}`}>&ldquo;{plan.reason}&rdquo;</div>
                )}
                <div className={`text-[13px] mt-1 ${"text-[var(--t-dim)]"}`}>
                  {early
                    ? <>Reordering later and holding a smaller buffer until it&apos;s placed.</>
                    : <>Reordering earlier and holding a bigger buffer until it&apos;s placed.</>}
                  {" "}Raise the order and this becomes its note; discard it and nothing is kept.
                </div>
                <button onClick={() => setShowSlowWhy(v => !v)}
                  className={`text-[13px] underline mt-1 ${ink}`}>
                  {showSlowWhy ? "hide" : "what happens to my history?"}
                </button>
                {showSlowWhy && (
                  <div className={`text-[13px] mt-1 leading-relaxed ${"text-[var(--t-dim)]"}`}>
                    Nothing is excluded. When it lands it counts in this product&apos;s lead times and the
                    supplier&apos;s averages like any other delivery, because it really did take that long.
                    Protection levels stay tested against your normal{" "}
                    {planningLeadTime(skuId, { ...params, slowShipment: undefined, nextLeadTime: undefined }, suppliers).days}-day
                    wait, not this one-off.
                  </div>
                )}
              </div>
              );
            })() : slowDraft.open ? (() => {
              /* Entered as a CHANGE against this lane's normal wait, not as an absolute.
                 "Three weeks late" is how anyone actually describes it, and it's the only
                 phrasing that can also express arriving early. */
              const d = Math.round(Number(slowDraft.days));
              const valid = Number.isFinite(d) && d > 0;
              const delta = slowDraft.dir === "sooner" ? -d : d;
              const result = normalPlan + delta;
              // A speed-up can't outrun the calendar: an order still takes at least a day.
              const tooFast = slowDraft.dir === "sooner" && valid && result < 1;
              return (
              <div className={`rounded-xl border px-3 py-2.5 space-y-2 ${"bg-[var(--t-panel)] border-[var(--t-line2)]"}`}>
                <div className={`text-[14px] font-bold ${"text-[var(--t-ink)]"}`}>The next order only</div>
                <div className="flex items-center gap-1.5">
                  {[["later", "Arriving later"], ["sooner", "Arriving sooner"]].map(([k, label]) => (
                    <button key={k} type="button" onClick={() => setSlowDraft(x => ({ ...x, dir: k }))}
                      className={`flex-1 text-[14px] font-semibold py-1 rounded-lg border transition-all ${
                        slowDraft.dir === k
                          ? (k === "sooner"
                              ? ("bg-[var(--t-info-soft)] border-[var(--t-info-line)] text-[var(--t-info)]")
                              : ("bg-[var(--t-warn-soft)] border-[var(--t-warn-line)] text-[var(--t-warn)]"))
                          : ("bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-dim)] hover:border-[var(--t-line2)]")}`}>
                      {label}
                    </button>
                  ))}
                </div>
                <div className="flex items-center gap-2">
                  <input type="number" min="1" autoFocus placeholder="days"
                    value={slowDraft.days}
                    onChange={e => setSlowDraft(x => ({ ...x, days: e.target.value }))}
                    className={`w-20 rounded border px-1.5 py-1 text-[15px] ${"bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-ink)]"}`} />
                  <span className={`text-[14px] ${"text-[var(--t-dim)]"}`}>
                    days {slowDraft.dir === "sooner" ? "sooner than" : "later than"} the usual {normalPlan}d
                  </span>
                </div>
                <input type="text" placeholder="what happened? (port strike, expedited freight, factory shutdown…)"
                  value={slowDraft.reason}
                  onChange={e => setSlowDraft(x => ({ ...x, reason: e.target.value }))}
                  className={`w-full rounded border px-1.5 py-1 text-[15px] ${"bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-ink)]"}`} />
                {valid && (
                  <div className={`text-[13px] ${tooFast ? ("text-[var(--t-bad)]") : ("text-[var(--t-soft)]")}`}>
                    {tooFast
                      ? `That's more than the whole ${normalPlan}-day wait. The next order can't arrive before it's placed.`
                      : `Planning the next order at ${result}d instead of ${normalPlan}d.`}
                  </div>
                )}
                <div className="flex items-center gap-2">
                  <button disabled={!valid || tooFast}
                    onClick={() => {
                      updateDraft("slowShipment", { days: result, delta, basis: normalPlan,
                                                    reason: slowDraft.reason.trim(), at: todayStr() });
                      setSlowDraft({ open: false, dir: "later", days: "", reason: "" });
                    }}
                    className={`text-[14px] font-bold px-3 py-1.5 rounded-lg transition-colors ${valid && !tooFast ? "bg-[var(--t-btn-bg)] hover:bg-[var(--t-btn-bg)] text-[var(--t-btn-fg)]" : `opacity-40 cursor-not-allowed border ${"border-[var(--t-line2)] text-[var(--t-dim)]"}`}`}>
                    Save
                  </button>
                  <button onClick={() => setSlowDraft({ open: false, dir: "later", days: "", reason: "" })}
                    className={`text-[14px] px-2 py-1.5 ${"text-[var(--t-dim)] hover:text-[var(--t-soft)]"}`}>cancel</button>
                  <span className={`text-[13px] ml-auto text-right ${"text-[var(--t-dim)]"}`}>
                    The reason carries onto the order when you raise it.
                  </span>
                </div>
              </div>
              );
            })() : (
              <button onClick={() => setSlowDraft({ open: true, dir: "later", days: "", reason: "" })}
                className={`w-full text-[14px] font-semibold py-1.5 rounded-lg border transition-all ${"bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-soft)] hover:border-[var(--t-warn-line)] hover:text-[var(--t-warn)]"}`}>
                Something&apos;s affecting the next order
              </button>
            )}

            {/* SKU lead time comparison card */}
            {skuSt.n > 0 && (() => {
              const reliable    = skuSt.n >= 3;
              // Planning never uses the average (half of orders would land late), and it
              // uses the measured P80 by itself once there are 3+ deliveries. The old
              // "Use avg" / "Use P80" buttons only rewrote the typed lead time, which
              // planning ignores at that point, so they did nothing while showing
              // "Selected". "In use" now reflects what planning actually does.
              const activeAvg   = false;
              const activeP80   = plan.source === "measured";
              const pinned      = params.leadTimeMode === "manual";
              const daysGap     = skuSt.p80 !== null && skuSt.avg !== null ? skuSt.p80 - skuSt.avg : null;
              return (
                <div className={`rounded-xl border overflow-hidden ${"border-[var(--t-line)]"}`}>
                  {/* Header */}
                  <div className={`px-3 py-2 flex items-center justify-between ${"bg-[var(--t-sunken)]"}`}>
                    <span className={`text-[14px] font-bold uppercase tracking-widest ${"text-[var(--t-dim)]"}`}>
                      SKU lead times
                    </span>
                    <span className={`text-[14px] ${"text-[var(--t-dim)]"}`}>
                      {skuSt.n} order{skuSt.n !== 1 ? "s" : ""}
                      {!reliable && <span className={`ml-1.5 ${"text-[var(--t-warn)]"}`}>· need ≥ 3 to apply</span>}
                    </span>
                  </div>

                  {/* Two-column comparison */}
                  <div className={`grid ${skuSt.p80 !== null ? "grid-cols-2" : "grid-cols-1"} divide-x ${"divide-[var(--t-line)]"}`}>
                    {/* AVG column */}
                    <div className={`p-3 transition-colors ${activeAvg ? ("bg-[var(--t-sunken)]") : ("bg-[var(--t-panel)]")}`}>
                      <div className="flex items-center justify-between mb-1.5">
                        <span className={`text-[13px] uppercase tracking-widest font-bold ${"text-[var(--t-dim)]"}`}>Avg</span>
                        {activeAvg && <span className={`text-[13px] font-semibold rounded-full px-1.5 py-0.5 ${"bg-[var(--t-line)] text-[var(--t-soft)]"}`}>in use</span>}
                      </div>
                      <div className={`text-xl font-bold tabular-nums leading-none ${"text-[var(--t-ink)]"}`}>{skuSt.avg}d</div>
                      <div className={`text-[14px] mt-1.5 leading-snug ${"text-[var(--t-dim)]"}`}>
                        No delay protection
                      </div>
                      <div className={`text-[14px] ${"text-[var(--t-dim)]"}`}>
                        ~50% on-time
                      </div>
                      <div className={`text-[13px] mt-1 ${"text-[var(--t-dim)]"}`}>For reference, not planned on</div>
                    </div>

                    {/* P80 column */}
                    {skuSt.p80 !== null && (
                      <div className={`p-3 transition-colors ${activeP80 ? ("bg-[var(--t-accent-soft)]") : ("bg-[var(--t-panel)]")}`}>
                        <div className="flex items-center justify-between mb-1.5">
                          <span className={`text-[13px] uppercase tracking-widest font-bold ${"text-[var(--t-accent)]"}`}>P80</span>
                          {activeP80 && <span className={`text-[13px] font-semibold rounded-full px-1.5 py-0.5 ${"bg-[var(--t-accent-soft)] text-[var(--t-accent)]"}`}>in use</span>}
                        </div>
                        <div className={`text-xl font-bold tabular-nums leading-none ${"text-[var(--t-accent)]"}`}>{skuSt.p80}d</div>
                        <div className={`text-[14px] mt-1.5 leading-snug font-medium ${"text-[var(--t-accent)]"}`}>
                          {daysGap ? `Orders ${daysGap}d earlier` : "Earlier order"}
                        </div>
                        <div className={`text-[14px] ${"text-[var(--t-dim)]"}`}>
                          80% on-time
                        </div>
                        {reliable && pinned && (
                          <button onClick={() => updateDraft("leadTimeMode", undefined)}
                            className={`mt-2 w-full py-1 rounded-md text-[14px] font-semibold border transition-all ${"bg-[var(--t-accent-soft)] border-[var(--t-accent-line)] text-[var(--t-accent)] hover:bg-[var(--t-accent-soft)]"}`}>
                            Plan on measured P80 ↑
                          </button>
                        )}
                      </div>
                    )}
                  </div>

                </div>
              );
            })()}
          </div>

          {/* ── Stockout Protection ── */}
          {mlData?.protection && (() => {
            const p       = mlData.protection;
            // Volume-adjusted, not the raw CV. CV is error divided by daily units, so
            // across this catalogue it correlates 0.85 with 1/sqrt(units per day): it
            // mostly reported how few a day something sold. A perfectly steady product
            // selling 1.1 a day showed 97%, which read as alarming and was not. This is
            // the same figure the scorecard's Sales volatility bar uses, so the two
            // screens now report one number instead of two that disagree.
            const disp    = mlData.residualDispersion;
            const sigObs  = mlData.demandSigmaObserved;
            const refRate = mlData.demandRefMean;
            const zScore  = mlData.zScore;
            const setProt = (key) => updateDraft("protection", key === p.recommended ? undefined : key);
            return (
              <div>
                <label className={lbl}>
                  <Tip text="How hard to guard against running out. With enough history and a unit cost, it's picked from the item's expected stockout cost vs. buffer holding cost; otherwise from the margin rule. You can override it.">Stockout Protection</Tip>
                </label>
                {/* A test running (or queued) is about to replace what's below. Said loudly,
                    not as a quiet tag: a level picked now may be a different one in minutes. */}
                {p.testRunning && (
                  <div className="mb-2 rounded-xl border px-3 py-2.5 flex items-start gap-2.5 bg-[var(--t-accent-soft)] border-[var(--t-accent-line)]">
                    <svg className="h-4 w-4 mt-0.5 shrink-0 animate-spin text-[var(--t-accent)]" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z" />
                    </svg>
                    <div className="flex-1 min-w-0">
                      <div className="text-[15px] font-semibold text-[var(--t-accent)]">Backtest running now</div>
                      <div className="text-[14px] text-[var(--t-soft)]">
                        Testing protection levels against your sales history. This product&apos;s level and buffer
                        {p.status === "calculating" ? " will appear" : " may change"} when it finishes.
                        No need to stay on this page.
                      </div>
                      <RefitProgress skuId={skuId} />
                    </div>
                  </div>
                )}
                {!p.testRunning && p.status === "stale" && (
                  <div className="mb-2 text-[14px] text-[var(--t-dim)]">
                    Tested for a different lead time or coverage than this product&apos;s current settings, so the level
                    below is an estimate until it&apos;s re-tested. Changing a setting starts a re-test on its own; Backtest
                    tab → Re-test now runs one by hand.
                  </div>
                )}
                <div className={`rounded-xl border overflow-hidden ${"bg-[var(--t-sunken)] border-[var(--t-line)]"}`}>
                  {/* While a test is running, show NOTHING — no tiers, no percentages, no
                      buffer. A provisional figure that appears and then changes invites a
                      decision the user would have to revisit, and it only stands for a few
                      minutes. This applies to every product, including ones that will end
                      up on the estimate anyway: until the run finishes we don't know which. */}
                  {p.status === "calculating" ? (
                    <div className="px-3 py-4 flex items-center gap-2.5">
                      <svg className={`h-4 w-4 animate-spin ${"text-[var(--t-accent)]"}`} fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z" />
                      </svg>
                      <div>
                        <div className={`text-[15px] font-semibold ${"text-[var(--t-soft)]"}`}>Calculating…</div>
                        <div className={`text-[14px] ${"text-[var(--t-dim)]"}`}>
                          The protection levels appear here when the backtest finishes.
                        </div>
                      </div>
                    </div>
                  ) : (
                  <div className="grid grid-cols-[repeat(auto-fit,minmax(96px,1fr))] gap-1.5 p-2">
                    {/* auto-fit, not a fixed column count: the tier list comes from the
                        backend and grew from 4 to 5. A hardcoded grid-cols-4 left the
                        fifth option stranded on its own row at quarter width. */}
                    {p.options.map((t) => {
                      const active = t.key === p.chosen;
                      const isRec  = t.key === p.recommended;
                      return (
                        <button key={t.key} onClick={() => setProt(t.key)}
                          style={active ? { boxShadow: "0 0 0 1px var(--t-accent)" } : undefined}
                          className={`relative rounded-lg px-1 py-1.5 text-center transition-all border ${active
                            ? ("bg-[var(--t-accent-soft)] border-[var(--t-accent)] text-[var(--t-ink)]")
                            : isRec
                            ? ("bg-[var(--t-panel)] border-[var(--t-accent-line)] text-[var(--t-soft)] hover:border-[var(--t-accent)]")
                            : ("bg-[var(--t-panel)] border-[var(--t-line)] text-[var(--t-soft)] hover:border-[var(--t-line2)]")}`}>
                          <div className="text-[14px] font-bold leading-none">{t.label}</div>
                          <div className={`text-[13px] font-mono mt-0.5 ${active ? "text-[var(--t-accent)]" : ("text-[var(--t-dim)]")}`}>{t.pct}%</div>
                          {/* No word inside the tile at all.
                              It started as an absolutely-positioned pill, centred on the
                              tile and `whitespace-nowrap`: about 110px of text laid over
                              a 64px tile, so it covered the very label it pointed at —
                              "90% Light" disappeared behind it. Moving it INTO the tile
                              stopped it covering a neighbour, but then it just clipped.
                              Measured across container widths from 300 to 700px the
                              tiles land between 103 and 116px, depending on how many
                              columns auto-fit picks, and the word needs 110: it fits at
                              some widths and truncates at others, which is a coin flip
                              rather than a fix.
                              So the tile is marked by its BORDER, and the word is said
                              in full on the line below, where there is room for it. */}
                        </button>
                      );
                    })}
                  </div>
                  )}
                  {/* Why this was chosen — suppressed entirely while a test runs. */}
                  {p.status !== "calculating" && (
                  <div className={`px-3 py-2 text-[14px] leading-relaxed border-t ${"bg-[var(--t-panel)] border-[var(--t-line)] text-[var(--t-dim)]"}`}>
                    {!p.overridden && (() => {
                      const rec = (p.options || []).find(o => o.key === p.recommended);
                      return rec ? <span className="font-semibold text-[var(--t-accent)]">Recommended: {rec.label} ({rec.pct}%). </span> : null;
                    })()}
                    {p.overridden
                      ? <><span className="font-semibold">Manual override.</span> {p.reason} <button onClick={() => setProt(p.recommended)} className={`underline ${"text-[var(--t-accent)]"}`}>Reset to recommended</button></>
                      : p.reason}
                    {/* Why this product isn't on measured data. "Provisional" alone read
                        identically whether the cause was thin history, a missing cost, a
                        pricing problem, or a test still running. */}
                    {p.provisional && p.whyProvisional && (
                      <div className={`mt-1.5 text-[13px] rounded-md px-2 py-1.5 leading-relaxed ${"bg-[var(--t-warn-soft)] text-[var(--t-warn)]"}`}>
                        <span className="font-semibold">Why this isn&apos;t measured yet: </span>{p.whyProvisional}
                      </div>
                    )}
                    {p.economics?.tiers?.length > 0 && p.source === "economics" && (
                      <div className={`mt-1.5 text-[13px] ${"text-[var(--t-dim)]"}`}>
                        Meanwhile: estimated from margin ${p.economics.marginUnit}/unit and {p.economics.holdingPct}%/yr holding,
                        assuming demand error is well behaved.
                      </div>
                    )}
                    {p.source === "backtest" && p.economics && (
                      <div className={`mt-1.5 text-[13px] ${"text-[var(--t-dim)]"}`}>
                        Backtest winner · {p.economics.windows} tests · holding {p.economics.holdingPct}%/yr
                      </div>
                    )}
                  </div>
                  )}
                  {/* Stats footer */}
                  {p.status !== "calculating" && disp != null && (
                    <div className={`divide-y ${"divide-[var(--t-line)] border-t border-[var(--t-line)]"}`}>
                      <div className={`flex items-center justify-between px-3 py-1.5 text-[14px] ${"bg-[var(--t-panel)]"}`}>
                        <Tip text={`Daily sales swing after trend and seasonality, measured against the randomness normal for a product this size: 1.0x is as steady as that volume allows, past 2.0x it moves more than its size explains.${sigObs != null && refRate ? ` Here that is about ±${sigObs.toFixed(1)} a day on ${refRate.toFixed(1)} a day.` : ""} The buffer comes from the raw swing in units over a lead time, not this size-adjusted ratio, so a bigger swing raises it but a higher ratio alone does not.`}>
                          <span className={"text-[var(--t-dim)]"}>Sales volatility</span>
                        </Tip>
                        <span className={`font-mono font-bold ${"text-[var(--t-soft)]"}`}>{disp.toFixed(1)}x{mlData.demandVolatilityLabel ? ` · ${mlData.demandVolatilityLabel}` : ""}</span>
                      </div>
                      <div className={`flex items-center justify-between px-3 py-1.5 text-[14px] ${"bg-[var(--t-panel)]"}`}>
                        <Tip text={`Protection level: the probability of not stocking out during a replenishment cycle. z=${zScore?.toFixed(3)} is the standard normal score for ${p.servicePct}%.`}>
                          <span className={"text-[var(--t-dim)]"}>Protection (z={zScore?.toFixed(2)})</span>
                        </Tip>
                        <span className={`font-mono font-bold ${"text-[var(--t-accent)]"}`}>{p.status === "calculating" ? "calculating…" : `${p.servicePct}%${p.achievedPct != null ? ` → ${p.achievedPct}% store-wide` : ""}${p.status === "stale" ? " · tested for other settings" : p.source === "backtest" ? " · tested" : p.source === "economics" ? " · provisional" : p.marginPct != null ? ` · ${Math.round(p.marginPct)}% margin` : ""}`}</span>
                      </div>
                      {/* Show details: plain-English buffer math */}
                      <button onClick={() => setShowBufferMath(v => !v)}
                        className={`w-full flex items-center justify-between px-3 py-1.5 text-[14px] font-semibold ${"text-[var(--t-accent)] hover:bg-[var(--t-sunken)]"}`}>
                        <span>{showBufferMath ? "Hide" : "Show"} how this buffer was calculated</span>
                        <svg className={`h-3 w-3 transition-transform ${showBufferMath ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                      </button>
                      {showBufferMath && (() => {
                        const sigma = mlData.residualStd;
                        const lt    = plan.days;   // what the buffer was sized over, not the typed field
                        const rootL = lt != null ? Math.sqrt(lt) : null;
                        const rowC  = "text-[var(--t-dim)]";
                        const valC  = "text-[var(--t-soft)]";
                        // The buffer is protection × the demand SWING over the whole lead-time wait.
                        // We use the larger of: the day-swing stretched by √lead (the floor), or the
                        // swing MEASURED over real lead-time windows (bigger when demand clumps). Back
                        // the swing actually used out of the held buffer so the rows stay consistent.
                        const formulaSpread = (sigma != null && rootL != null) ? sigma * rootL : null;
                        // The swing is the standing buffer's; holiday cover is on its own line.
                        const hcv = mlData.holidayCover?.units > 0 ? mlData.holidayCover : null;
                        const standing = hcv ? hcv.standingUnits : mlData.safetyStock;
                        const usedSpread = (zScore && standing != null) ? standing / zScore : formulaSpread;
                        const widened = (usedSpread != null && formulaSpread != null) && usedSpread > formulaSpread * 1.08;
                        return (
                          <div className={`px-3 py-2.5 space-y-1.5 text-[14px] ${"bg-[var(--t-sunken)]"}`}>
                            <div className={`flex justify-between ${rowC}`}><span>Typical demand</span><span className={`font-mono ${valC}`}>~{mlData.avgDailyDemand}/day{mlData.avgDailyDemand < 1 ? ` (~${Math.round(mlData.avgDailyDemand * 30)}/mo)` : ""}</span></div>
                            <div className={`flex justify-between ${rowC}`}><span>Day-to-day swing (σ)</span><span className={`font-mono ${valC}`}>±{sigma}/day</span></div>
                            <div className={`flex justify-between ${rowC}`}><span>Swing over the {lt}-day wait</span><span className={`font-mono ${valC}`}>±{usedSpread?.toFixed(1)} {widened ? "(measured)" : `≈ σ×√${lt}`}</span></div>
                            <div className={`flex justify-between ${rowC}`}><span>Protection factor</span><span className={`font-mono ${valC}`}>×{zScore?.toFixed(2)} ({p.servicePct}%)</span></div>
                            {hcv && (
                              <div className={`flex justify-between ${rowC}`}><span>Holiday cover ({hcv.holidays.map(h => h.name).join(", ")})</span><span className={`font-mono ${valC}`}>+{hcv.units}</span></div>
                            )}
                            <div className={`flex justify-between font-semibold border-t pt-1.5 ${"border-[var(--t-line)] text-[var(--t-soft)]"}`}><span>Buffer held</span><span className="font-mono">{mlData.safetyStock} units</span></div>
                            {hcv && (
                              <p className={`leading-relaxed ${"text-[var(--t-dim)]"}`}>
                                Of which <span className="font-semibold">{hcv.units}</span> is holiday cover: holiday weeks sell less predictably, so orders placed while a holiday is inside the order window carry extra. It&apos;s temporary; once the holiday passes the next order is smaller by whatever&apos;s left{hcv.carryCost != null ? `, so it costs about $${Math.round(hcv.carryCost).toLocaleString()} to carry, not a year's holding` : ""}.
                              </p>
                            )}
                            <p className={`pt-1 leading-relaxed ${"text-[var(--t-dim)]"}`}>
                              We hold <span className="font-semibold">{mlData.safetyStock}</span> spare units to cover demand swings during the {lt}-day wait about {p.servicePct}% of the time{p.achievedPct != null ? ` (replayed against your history, this level kept ${p.achievedPct}% of windows in stock across your store, since real demand has a longer tail than the bell curve the buffer assumes)` : ""}. The swing comes from how far past forecasts missed over real {lt}-day stretches{widened ? "; this item sells in clumps, so it's wider than a steady seller's and earns extra buffer" : ", and here it matches the day-to-day swing stretched over the wait"}. A noisier or clumpier item, or a longer lead time, raises this; a steadier one lowers it.
                            </p>
                          </div>
                        );
                      })()}
                    </div>
                  )}
                </div>
              </div>
            );
          })()}

          {/* ── Coverage Window ── */}
          <div>
            <label className={lbl}>
              <Tip text="How many days of stock the order should cover, starting when it arrives. Bigger means larger orders and a longer runway between reorders.">Coverage Window (days)</Tip>
            </label>
            <input type="number" min="1" value={draftParams.coverage} onChange={e => updateDraft("coverage", Math.max(1, Number(e.target.value) || 1))} className={inp} />
            {mlData?.daysUntilStockout > 0 && (() => {
              // Coverage starts when an order placed NOW would actually arrive:
              //   today + max(daysUntilReorder, 0) + leadTime.
              // On schedule this equals the stockout date (you order on the reorder
              // date and it lands as you run out). Overdue, daysUntilReorder is ≤ 0,
              // so delivery is today + leadTime — which can fall AFTER the stockout,
              // exposing an unavoidable out-of-stock gap. This matches the backend's
              // order-quantity math (which also assumes "order today" when overdue).
              // plan.days, the lead time the order math used. The typed field can differ
              // (a measured P80, a supplier's baseline, a one-off slow shipment), and
              // using it here drew coverage dates and hid the out-of-stock gap.
              const deliveryOffset = Math.max(mlData.daysUntilReorder ?? 0, 0) + plan.days;
              const deliveryDate = new Date(todayMs());
              deliveryDate.setUTCDate(deliveryDate.getUTCDate() + deliveryOffset);
              const coverageEnd = new Date(deliveryDate.getTime());
              coverageEnd.setUTCDate(coverageEnd.getUTCDate() + draftParams.coverage);
              const fmt = d => d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
              // Gap = days you'll be out of stock before this order can land (overdue only).
              const gapDays = Math.max(0, deliveryOffset - mlData.daysUntilStockout);
              return (
                <div className={`rounded-xl border overflow-hidden ${"border-[var(--t-line)]"}`}>
                  <div className={`px-3 py-2 flex items-center justify-between ${"bg-[var(--t-panel)]"}`}>
                    <span className={`text-[14px] ${"text-[var(--t-dim)]"}`}>Covered from</span>
                    <span className={`text-[14px] font-semibold tabular-nums ${"text-[var(--t-soft)]"}`}>{fmt(deliveryDate)}</span>
                    <span className={`text-[14px] ${"text-[var(--t-soft)]"}`}>→</span>
                    <span className={`text-[14px] font-semibold tabular-nums ${"text-[var(--t-soft)]"}`}>{fmt(coverageEnd)}</span>
                  </div>
                  {gapDays > 0 && (
                    <div className={`px-3 py-1.5 border-t text-[14px] leading-relaxed ${"border-[var(--t-bad-line)] bg-[var(--t-bad-soft)] text-[var(--t-bad)]"}`}>
                      ⚠ Even ordering today, stock won't arrive until {fmt(deliveryDate)}. Expect ~{gapDays} day{gapDays !== 1 ? "s" : ""} out of stock before then.
                    </div>
                  )}
                  <div className={`px-3 py-1.5 border-t text-[14px] leading-relaxed ${"border-[var(--t-line)] bg-[var(--t-sunken)] text-[var(--t-dim)]"}`}>
                    Estimated from the current forecast. Demand above forecast shortens the window, below extends it. Delivery delays shift the start date.
                  </div>
                </div>
              );
            })()}
          </div>

          {/* ── Forecast Horizon ── */}
          <div>
            <label className={lbl}>
              <Tip text="How far ahead to forecast. 1 month is fastest. 3 months shows longer-term trends.">Forecast Horizon</Tip>
            </label>
            <div className="flex gap-1.5">
              {[1, 2, 3].map(m => (
                <button key={m} onClick={() => updateDraft("months", m)}
                  className={`flex-1 py-2 rounded-lg text-[16.5px] font-bold border transition-all ${draftParams.months === m ? moBtnActive : moBtnInactive}`}>
                  {m}mo
                </button>
              ))}
            </div>
          </div>
        </div>

        {loading && (
          <div className={`text-[14px] font-mono p-2 rounded-lg flex items-center gap-2 ${"text-[var(--t-warn)] bg-[var(--t-warn-soft)] border border-[var(--t-warn-line)]"}`}>
            <div className="h-2 w-2 border-2 border-[var(--t-warn-line)] border-t-transparent rounded-full animate-spin" />
            Recalculating…
          </div>
        )}

        {hasOpenPO && (
          <div className={`${poCard} border rounded-xl p-3 space-y-1.5`}>
            <div className="flex items-center gap-1.5">
              <div className="h-1.5 w-1.5 rounded-full bg-[var(--t-accent)]" />
              <span className={`text-[14px] font-bold uppercase tracking-widest ${poHead}`}>Order In Transit</span>
            </div>
            <div className={`text-[15px] ${poText} space-y-0.5 font-mono`}>
              <div>{openPO.qty.toLocaleString()} units</div>
              <div>Est. delivery <span className={textMain}>{isoToDisplay(openPO.delivery)}</span></div>
              {openPO.supplier && <div className={textMuted}>{openPO.supplier}</div>}
            </div>
            <button onClick={() => setShowPoModal(true)} className={`text-[14px] ${"text-[var(--t-accent)] hover:text-[var(--t-accent)]"} transition-colors`}>Edit / Mark received →</button>
          </div>
        )}

        <div className={`border-t ${divider} pt-4 space-y-2`}>
          <div className={`${infoHead} mb-1`}>Model Info</div>
          {[
            ["Yearly pattern",  mlData.activeYearly ? "Detected" : "-",       mlData.activeYearly ? ("text-[var(--t-good)]") : ""],
            ["Weekly pattern",  mlData.activeWeekly ? "Detected" : "-",       mlData.activeWeekly ? ("text-[var(--t-good)]") : ""],
            ...(mlData.priceModeled ? [["Price tracking",  mlData.priceWellSampled ? "Active" : "Limited",
              mlData.priceWellSampled ? ("text-[var(--t-good)]") : ("text-[var(--t-warn)]")]] : []),
            ["History",         `${mlData.daysOfHistory}d`,                   infoVal],
            ["Total sold",      mlData.totalUnitsSold?.toLocaleString(),       infoVal],
          ].map(([k, v, c]) => (
            <div key={k} className={`flex justify-between ${infoLabel}`}>
              <span>{k}</span><span className={c || infoVal}>{v}</span>
            </div>
          ))}
          {mlData.stockoutRowsDropped > 0 && (
            <div className={`flex justify-between text-[14px] ${"text-[var(--t-warn)]"}`}>
              <span>Zero-stock days excluded</span><span>{mlData.stockoutRowsDropped}</span>
            </div>
          )}
          {(mlData.currentPrice ?? mlData.lastPrice) != null && (
            <div className={`flex justify-between ${infoLabel}`}><span>Current price</span>
              <span className={infoVal}>
                {mlData.priceDiscounted && mlData.listPrice != null && (
                  <s className="text-[var(--t-dim)] mr-1.5">${mlData.listPrice.toFixed(2)}</s>
                )}
                <span className={mlData.priceDiscounted ? "text-[var(--t-warn)] font-bold" : ""}>${(mlData.currentPrice ?? mlData.lastPrice).toFixed(2)}</span>
              </span>
            </div>
          )}
          {mlData.events?.length > 0 && (
            <div className={`flex justify-between ${infoLabel}`}><span>Events logged</span><span className={"text-[var(--t-accent)]"}>{mlData.events.length}</span></div>
          )}
        </div>
      
        </div>
      </div>

      {/* Main content */}
      <div className={`flex-1 overflow-y-auto p-7 space-y-6 ${mainBg}`}>
        <div className="space-y-4">
          <div>
            <h2 className={`text-xl font-bold ${textMain} tracking-tight`}>{mlData.skuName}</h2>
            <p className={`text-[15px] ${textMuted} font-mono mt-1`}>
              {mlData.skuId} · {mlData.filename || "-"}
              {mlData.horizonCapped && (
                <span className="opacity-70"
                  title={`Only ${mlData.forecastDays} days of projection are supported by ${mlData.daysOfHistory ?? "this product's"} days of history. The forecast looks ahead only as far as the history looks back.`}>
                  {" "}· {mlData.forecastDays}d projected
                </span>
              )}
            </p>
          </div>
          {/* One row of actions, each saying the one thing worth knowing before you
              click it. Forecast health is the colour of the first tile. */}
          <SkuActionBar mlData={mlData} statusCfg={statusCfg} statusReason={statusReason}
            hasOpenPO={hasOpenPO} openPO={openPO} exporting={exporting}
            onForecastDetails={() => setShowForecastDetails(true)}
            onPriceHistory={() => setShowPriceHistory(true)}
            onHolidays={() => setShowHolidays(true)}
            onEvents={() => setShowEvents(true)}
            onOrder={() => setShowPoModal(true)}
            onExport={onExport} />
        </div>

        {/* "Price changes won't move this forecast" used to be a full-width banner here.
            The Price history tile says it at the top of the page, and its panel says why. */}
        {mlData.priceModeled && mlData.priceChangeWarning && (() => {
          const isInfo = mlData.priceChangeLevel === "info";
          return (
            <div className={`border rounded-2xl p-4 flex items-start gap-3 ${
              isInfo
                ? "bg-[var(--t-accent-soft)] border-[var(--t-accent-line)]"
                : "bg-[var(--t-warn-soft)] border-[var(--t-warn-line)]"
            }`}>
              <span className="text-base mt-0.5 shrink-0">{isInfo ? "ℹ️" : "⚠️"}</span>
              <div>
                <div className={`text-[16.5px] font-bold mb-1 ${
                  isInfo
                    ? "text-[var(--t-accent)]"
                    : "text-[var(--t-warn)]"
                }`}>{isInfo ? "PRICE NOT DIRECTLY OBSERVED" : "PRICE OUTSIDE HISTORICAL RANGE"}</div>
                <p className={`text-[15px] leading-relaxed ${
                  isInfo
                    ? "text-[var(--t-accent)]"
                    : "text-[var(--t-warn)]"
                }`}>{mlData.priceChangeMessage}</p>
              </div>
            </div>
          );
        })()}

        {hasOpenPO && (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${"bg-[var(--t-accent-soft)] border-[var(--t-accent-line)]"}`}>
            <span className="text-base mt-0.5 shrink-0">📦</span>
            <div className="flex-1">
              <div className={`text-[16.5px] font-bold mb-1 ${"text-[var(--t-accent)]"}`}>ORDER IN TRANSIT: REORDER ALERT SUPPRESSED</div>
              <p className={`text-[15px] leading-relaxed ${"text-[var(--t-accent)]"}`}>
                <span className={`font-semibold ${textMain}`}>{openPO.qty.toLocaleString()} units</span>
                {openPO.supplier ? ` ordered from ${openPO.supplier}` : " on order"}.
                Expected delivery: <span className={`font-semibold ${textMain}`}>{isoToDisplay(openPO.delivery)}</span>.
              </p>
            </div>
            <button onClick={() => setShowPoModal(true)} className={`text-[14px] border rounded-lg px-2 py-1 transition-colors shrink-0 ${"text-[var(--t-accent)] border-[var(--t-accent-line)] hover:bg-[var(--t-accent-soft)]"}`}>Edit</button>
          </div>
        )}

        {mlData.inactive && (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${"bg-[var(--t-sunken)] border-[var(--t-line2)]"}`}>
            <span className="text-base mt-0.5 shrink-0">💤</span>
            <div className="flex-1">
              <div className={`text-[16.5px] font-bold mb-1 ${"text-[var(--t-soft)]"}`}>INACTIVE: likely discontinued</div>
              <p className={`text-[15px] leading-relaxed ${"text-[var(--t-soft)]"}`}>{mlData.inactiveMessage}</p>
            </div>
          </div>
        )}

        {mlData.tooNew && (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${"bg-[var(--t-accent-soft)] border-[var(--t-accent-line)]"}`}>
            <span className="text-base mt-0.5 shrink-0">🌱</span>
            <div className="flex-1">
              <div className={`text-[16.5px] font-bold mb-1 ${"text-[var(--t-accent)]"}`}>ESTABLISHING BASELINE: {mlData.ownDays} of {mlData.baselineDays} days</div>
              <p className={`text-[15px] leading-relaxed ${"text-[var(--t-accent)]"}`}>{mlData.tooNewMessage}</p>
            </div>
          </div>
        )}

        {mlData.young && (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${"bg-[var(--t-warn-soft)] border-[var(--t-warn-line)]"}`}>
            <span className="text-base mt-0.5 shrink-0">🌿</span>
            <div className="flex-1">
              <div className={`text-[16.5px] font-bold mb-1 ${"text-[var(--t-warn)]"}`}>YOUNG PRODUCT: {mlData.ownDays} of {mlData.youngThreshold} days · forecast is provisional</div>
              <p className={`text-[15px] leading-relaxed ${"text-[var(--t-warn)]"}`}>
                {mlData.youngMessage}
                {showReorder ? ` At this early pace, stock may run low around ${formatDate(mlData.daysUntilStockout)}, so consider ordering ~${mlData.orderQty?.toLocaleString()} when you're ready. Not a hard deadline yet: the estimate firms up as more sales come in.` : ""}
              </p>
            </div>
          </div>
        )}

        {showReorder && !mlData.young && (urgency === "critical" ? (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${"bg-[var(--t-bad-soft)] border-[var(--t-bad-line)]"}`}>
            <span className="text-base mt-0.5 shrink-0">🚨</span>
            <div className="flex-1">
              <div className={`text-[16.5px] font-bold mb-1 ${"text-[var(--t-bad)]"}`}>{mlData.daysUntilReorder === 0 ? "REORDER DUE TODAY" : `REORDER OVERDUE: ${Math.abs(mlData.daysUntilReorder)} day${Math.abs(mlData.daysUntilReorder) !== 1 ? "s" : ""} ago`}</div>
              <p className={`text-[15px] leading-relaxed ${"text-[var(--t-bad)]"}`}>
                {mlData.daysUntilReorder === 0 ? "Today is your reorder deadline." : "Your reorder deadline has passed."} Stock projected to run out <span className={`font-semibold ${textMain}`}>{formatDate(mlData.daysUntilStockout)}</span>. Place your order immediately.
              </p>
            </div>
            <button onClick={() => setShowPoModal(true)} className={`text-[14px] border rounded-lg px-2 py-1 transition-colors shrink-0 ${"text-[var(--t-bad)] border-[var(--t-bad-line)] hover:bg-[var(--t-bad-soft)]"}`}>Mark Ordered</button>
          </div>
        ) : (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${"bg-[var(--t-warn-soft)] border-[var(--t-warn-line)]"}`}>
            <span className="text-base mt-0.5 shrink-0">⚠️</span>
            <div className="flex-1">
              <div className={`text-[16.5px] font-bold mb-1 ${"text-[var(--t-warn)]"}`}>UPCOMING REORDER: {mlData.daysUntilReorder} days</div>
              <p className={`text-[15px] leading-relaxed ${"text-[var(--t-warn)]"}`}>
                Place order by <span className={`font-semibold ${textMain}`}>{formatDate(mlData.daysUntilReorder)}</span> to cover your {plan.days}-day lead time.
                Stockout projected <span className={`font-semibold ${textMain}`}>{formatDate(mlData.daysUntilStockout)}</span>.
              </p>
            </div>
            <button onClick={() => setShowPoModal(true)} className={`text-[14px] border rounded-lg px-2 py-1 transition-colors shrink-0 ${"text-[var(--t-warn)] border-[var(--t-warn-line)] hover:bg-[var(--t-warn-soft)]"}`}>Mark Ordered</button>
          </div>
        ))}

        {/* Forecast health moved into Forecast details; its verdict colours that tile. */}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <MetricCard lm={lm} label="In Stock"
            value={stockCounted ? params.stock.toLocaleString() : "-"}
            sub={!stockCounted ? "not counted: set a stock count"
                 : mlData.stockDataAvailable === false ? "assumed (no stock data)"
                 : hasOpenPO ? `+ ${openPO.qty.toLocaleString()} on order` : "units on hand"}
            valueColor={mlData.stockDataAvailable === false ? ("text-[var(--t-warn)]") : undefined} />
          <MetricCard lm={lm}
            label={<Tip text="When your current stock is predicted to run out if no new order arrives.">Stockout Date</Tip>}
            value={mlData.daysUntilStockout != null ? formatDate(mlData.daysUntilStockout).split(",")[0] : "-"}
            sub={mlData.daysUntilStockout != null ? `${mlData.daysUntilStockout} days away` : "Sufficient stock"}
            valueColor="text-[var(--t-bad)]" />
          <MetricCard lm={lm}
            label={<Tip text="The last safe date to place an order so it arrives before you run out of stock.">Order By</Tip>}
            value={hasOpenPO ? "On Order" : mlData.daysUntilReorder > 0 ? formatDate(mlData.daysUntilReorder).split(",")[0] : mlData.daysUntilReorder === 0 ? "Today" : mlData.daysUntilReorder != null ? `${Math.abs(mlData.daysUntilReorder)}d` : "-"}
            sub={hasOpenPO ? `Est. ${isoToDisplay(openPO.delivery)}` : mlData.daysUntilReorder > 0 ? `${mlData.daysUntilReorder} days from now` : mlData.daysUntilReorder === 0 ? "Order today" : mlData.daysUntilReorder != null ? "overdue" : "-"}
            valueColor={hasOpenPO ? ("text-[var(--t-accent)]") : mlData.daysUntilReorder <= 0 && mlData.daysUntilReorder != null ? "text-[var(--t-bad)]" : ("text-[var(--t-warn)]")} />
          <MetricCard lm={lm}
            label={<Tip text="Recommended order quantity: covers your coverage window demand plus a safety buffer for forecast uncertainty.">Units to Order</Tip>}
            value={mlData.orderQty.toLocaleString()} sub="recommended qty"
            valueColor={"text-[var(--t-accent)]"} accent />
        </div>

        {showReorder && (
          <div className={`flex items-start gap-2 text-[14px] leading-relaxed ${"text-[var(--t-dim)]"}`}>
            <span className="shrink-0">ℹ️</span>
            <span>The stockout date, order-by date and order quantity assume you order <span className="font-semibold">today</span>. The demand forecast isn't reduced for stockouts; it stays true demand (you just lose those sales). Ordering later pushes delivery out and lengthens any out-of-stock gap.</span>
          </div>
        )}

        {mlData.orderGuardrail?.active && (() => {
          const g = mlData.orderGuardrail;
          const tone = g.capExceeded
            ? ("bg-[var(--t-warn-soft)] border-[var(--t-warn-line)] text-[var(--t-warn)]")
            : ("bg-[var(--t-sunken)] border-[var(--t-line)] text-[var(--t-soft)]");
          return (
            <div className={`flex items-start gap-2.5 rounded-xl border px-4 py-3 text-[15px] ${tone}`}>
              <span className="shrink-0 mt-0.5">{g.capExceeded ? "⚠️" : "ℹ️"}</span>
              <div>
                <span className="font-semibold">New-product caution.</span> {g.reason}
                {g.capExceeded && (
                  <> <span className="opacity-80">Suggested first order: <span className="font-bold tabular-nums">{g.suggestedCap.toLocaleString()}</span> vs. {g.rawOrderQty.toLocaleString()} recommended.</span></>
                )}
              </div>
            </div>
          );
        })()}

        {mlData.tooNew ? (
          <>
          <div className={`${cardBg} border rounded-2xl p-5 h-[320px]`}>
            <ForecastChart historyPoints={mlData.chartDataHistory} futurePoints={[]} rangePoints={[]}
              stockoutTime={null} reorderTime={null} intervalWidth={mlData.intervalWidth}
              upcomingPromos={mlData.upcomingPromos} lm={lm} todaySales={mlData.todaySales}
              closedDays={mlData.closedDays || []} />
          </div>
          <div className={`${cardBg} border rounded-2xl p-6`}>
            <div className="flex items-center gap-2 mb-2">
              <span className="text-lg">🌱</span>
              <span className={`text-[16.5px] font-bold ${textMain}`}>Forecast paused: establishing baseline</span>
            </div>
            <p className={`text-[15px] leading-relaxed mb-5 ${textMuted}`}>
              We start forecasting once this product has about a week of its own sales ({mlData.ownDays}/{mlData.baselineDays} days so far). Until then, here's what we can track straight from inventory.
            </p>
            {/* For a slow seller, per-day whole units read as "0". Lead with how it
                actually sells — a monthly rate and the fact that zero-sale days are
                normal — before any daily number appears. */}
            {mlData.demandStory && (
              <div className={`mb-4 rounded-xl border px-4 py-3 text-[15px] leading-relaxed ${"bg-[var(--t-info-soft)] border-[var(--t-info-line)] text-[var(--t-soft)]"}`}>
                <span className="font-semibold">How this product sells: </span>{mlData.demandStory}
                <div className={`mt-1 ${textMuted}`}>
                  Orders aren&apos;t sized from one day. They add this rate up across your lead time and coverage window.
                </div>
              </div>
            )}
            {mlData.rateCheck && (
              <div className={`mb-4 rounded-xl border px-4 py-3 text-[15px] leading-relaxed ${"bg-[var(--t-warn-soft)] border-[var(--t-warn-line)] text-[var(--t-warn)]"}`}>
                <span className="font-semibold">Forecast is running below recent sales. </span>{mlData.rateCheck.message}
              </div>
            )}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div><div className={`text-[14px] uppercase tracking-widest font-bold mb-1 ${textMuted}`}>In stock</div><span className={`text-2xl font-bold tabular-nums ${textMain}`}>{params.stock.toLocaleString()}</span></div>
              <div><div className={`text-[14px] uppercase tracking-widest font-bold mb-1 ${textMuted}`}>Sold so far</div><span className={`text-2xl font-bold tabular-nums ${textMain}`}>{mlData.currentMonth?.unitsSoFar?.toLocaleString() ?? "-"}</span></div>
              <div><div className={`text-[14px] uppercase tracking-widest font-bold mb-1 ${textMuted}`}>Observed pace</div><span className={`text-2xl font-bold tabular-nums ${textMain}`}>{mlData.observedDailyRate ?? "-"}</span><span className={`text-[15px] ${textMuted}`}>/day</span>
                {mlData.observedDailyRate != null && mlData.observedDailyRate < 1 && (
                  <div className={`text-[14px] ${textMuted}`}>≈ {Math.round(mlData.observedDailyRate * 30)}/month</div>
                )}</div>
              <div><div className={`text-[14px] uppercase tracking-widest font-bold mb-1 ${textMuted}`}>Stock covers</div><span className={`text-2xl font-bold tabular-nums ${textMain}`}>{mlData.observedRunwayDays != null ? `~${mlData.observedRunwayDays}` : "-"}</span><span className={`text-[15px] ${textMuted}`}>{mlData.observedRunwayDays != null ? " days" : ""}</span></div>
            </div>
            {mlData.observedRunwayDays != null && (() => {
              const soon = mlData.observedRunwayDays <= plan.days;
              return (
                <div className={`mt-4 rounded-xl border px-4 py-3 text-[15px] leading-relaxed ${soon
                  ? ("bg-[var(--t-warn-soft)] border-[var(--t-warn-line)] text-[var(--t-warn)]")
                  : ("bg-[var(--t-sunken)] border-[var(--t-line)] text-[var(--t-soft)]")}`}>
                  {soon ? "⚠️ " : "ℹ️ "}At its current pace (~{mlData.observedDailyRate}/day), this runs out around{" "}
                  <span className="font-semibold">{mlData.observedStockoutTs ? new Date(mlData.observedStockoutTs).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : `~${mlData.observedRunwayDays} days`}</span>
                  , about {mlData.observedRunwayDays} days of stock left.
                  {soon ? ` That's inside your ${plan.days}-day lead time, so it's worth ordering now even though the forecast is still warming up.` : ""}
                </div>
              );
            })()}
            <p className={`text-[14px] mt-4 ${textMuted}`}>Pace and runway are raw observations from the days so far, not a forecast.</p>
          </div>
          </>
        ) : (
        <>
        <ArrivalPromptBanner arrival={arrival} lm={lm}
          onConfirm={onConfirmArrival} onIgnore={onIgnoreArrival} />
        {/* A seasonal scale on a route that has no seasonal curve of its own. */}
        {mlData.seasonalityApplied?.text && (
          <div className={`mb-4 rounded-xl border px-4 py-3 text-[15px] leading-relaxed ${"bg-[var(--t-info-soft)] border-[var(--t-info-line)] text-[var(--t-info)]"}`}>
            <span className="font-semibold">Seasonal pattern. </span>
            {mlData.seasonalityApplied.text}
            {mlData.seasonalityApplied.applied && (
              <span className={`block mt-1 ${"text-[var(--t-info)]"}`}>
                In this forecast window that ranges from {mlData.seasonalityApplied.troughMultiplier}× to
                {" "}{mlData.seasonalityApplied.peakMultiplier}× the flat rate.
              </span>
            )}
          </div>
        )}
        {/* Price and promo effects on the routes that can't model them internally.
            Silent multipliers moving an order would be worse than none. */}
        {mlData.uplift?.text && (
          <div className={`mb-4 rounded-xl border px-4 py-3 text-[15px] leading-relaxed ${"bg-[var(--t-accent-soft)] border-[var(--t-accent-line)] text-[var(--t-accent)]"}`}>
            <span className="font-semibold">Price &amp; promotions. </span>
            {mlData.uplift.text}
            {mlData.uplift.applied && (
              <span className={`block mt-1 ${"text-[var(--t-accent)]"}`}>
                Applied to {mlData.uplift.daysLifted} upcoming day{mlData.uplift.daysLifted === 1 ? "" : "s"} in this forecast.
              </span>
            )}
          </div>
        )}
        {/* An adjusted forecast must never be silent — if the model was fitted on
            something other than raw sales, say so and say why. */}
        {mlData.censoring && (
          <div className={`mb-4 rounded-xl border px-4 py-3 text-[15px] leading-relaxed ${"bg-[var(--t-info-soft)] border-[var(--t-info-line)] text-[var(--t-info)]"}`}>
            <span className="font-semibold">Adjusted for days you ran out. </span>
            {mlData.censoring.text}
            {mlData.censoring.addedUnits > 0 && (
              <span className={`block mt-1 ${"text-[var(--t-info)]"}`}>
            The forecast is fitted to an estimated {mlData.censoring.fittedUnits} units rather than the
            {" "}{mlData.censoring.observedUnits} you sold, because selling out caps sales without
            capping demand. Left uncorrected this drifts lower every cycle.
              </span>
            )}
          </div>
            )}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          {(() => {
            /* Holidays inside the chart's horizon, named once under it. The shading is
               named too, but only where its band is wide enough (see ForecastChart). */
            const fut = mlData.inactive ? [] : (mlData.chartDataFuture || []);
            const endX = fut.length ? fut[fut.length - 1].x : 0;
            const wins = ((mlData.holidays || {}).windows || [])
              .filter(w => new Date(`${w.start}T00:00:00Z`).getTime() <= endX);
            const fmt = (iso) => new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
            return (
              <div className={`lg:col-span-2 ${cardBg} border rounded-2xl p-5 h-[420px] flex flex-col`}>
                <div className="flex-1 min-h-0">
                  <ForecastChart historyPoints={mlData.chartDataHistory}
                    futurePoints={fut}
                    rangePoints={mlData.inactive ? [] : mlData.chartDataRange}
                    stockoutTime={mlData.inactive ? null : mlData.stockoutTimestamp}
                    reorderTime={mlData.inactive ? null : mlData.reorderTimestamp} intervalWidth={mlData.intervalWidth}
                    poLandsTime={openPO?.delivery ? new Date(`${openPO.delivery}T12:00:00Z`).getTime() : null}
                    upcomingPromos={mlData.upcomingPromos} holidayWindows={wins} lm={lm} todaySales={mlData.todaySales}
                    closedDays={mlData.closedDays || []} />
                </div>
                {wins.length > 0 && (
                  <div className={`pt-2 text-[13.5px] leading-snug ${textMuted} truncate`}
                    title={wins.map(w => `${w.name} (${fmt(w.start)} to ${fmt(w.end)})${w.peakDate ? `, busiest ${fmt(w.peakDate)}` : ""}`).join(" · ")}>
                    <span className="inline-block h-2.5 w-2.5 mr-1.5 align-middle rounded-sm bg-[var(--t-sunken)] border border-[var(--t-line2)]" />
                    Holiday stretches: {wins.map(w => `${w.name} (${fmt(w.start)}–${fmt(w.end)})`).join(" · ")}
                  </div>
                )}
                {(mlData.closedDays || []).length > 0 && (
                  <div className={`pt-1.5 text-[13.5px] leading-snug ${textMuted}`}>
                    <span className="inline-block h-2.5 w-2.5 mr-1.5 align-middle rounded-full" style={{ background: "#F07A1A" }} />
                    Store closed ({mlData.closedDays.length} day{mlData.closedDays.length === 1 ? "" : "s"} on this chart):
                    left out of training, and forecast at 0. Set them under Closed days on the Fleet overview.
                  </div>
                )}
              </div>
            );
          })()}
          {/* Current Month — standalone card aligned with chart */}
          <div className={`${cardBg} border rounded-2xl p-6 flex flex-col justify-between h-[360px]`}>
            {/* Top: header + big number */}
            <div>
              <div className={`text-[14px] uppercase tracking-widest font-bold mb-4 ${textMuted}`}>Current Month</div>
              <div className="flex items-baseline gap-3 mb-1">
                <span className={`text-6xl font-bold ${textMain} tabular-nums leading-none`}>{projTotal.toLocaleString()}</span>
              </div>
              <div className="flex items-center gap-2 mt-2">
                <span className={`text-[16.5px] font-mono font-bold ${pctChange >= 0 ? "text-[var(--t-good)]" : "text-[var(--t-bad)]"}`}>
                  {pctChange >= 0 ? "▲" : "▼"} {Math.abs(pctChange).toFixed(1)}%
                </span>
                <span className={`text-[16.5px] ${textMuted}`}>vs last month</span>
              </div>
              {pctYoY != null && (
                <div className="flex items-center gap-2 mt-0.5"
                  title={`${lyTotal.toLocaleString()} sold in the same month last year. Unlike last month, the season cancels out here, so this shows whether the product is growing.`}>
                  <span className={`text-[16.5px] font-mono font-bold ${pctYoY >= 0 ? "text-[var(--t-good)]" : "text-[var(--t-bad)]"}`}>
                    {pctYoY >= 0 ? "▲" : "▼"} {Math.abs(pctYoY).toFixed(1)}%
                  </span>
                  <span className={`text-[16.5px] ${textMuted}`}>vs last year</span>
                </div>
              )}
            </div>
            {/* Middle: sold / remaining */}
            <div className={`grid grid-cols-2 gap-4 py-5 border-y ${divider}`}>
              <div>
                <div className={`text-[14px] uppercase tracking-widest font-bold mb-1 ${textMuted}`}>Sold so far</div>
                <span className={`text-3xl font-bold tabular-nums ${textMain}`}>{mlData.currentMonth.unitsSoFar.toLocaleString()}</span>
                {/* TODAY, which is in none of the figures above it.
                    The sales sync runs at 00:15 and the training data deliberately stops
                    at the last COMPLETE day, so every month-to-date number on this card
                    is current through yesterday and silent about the hours since. Without
                    this line a shop that traded all day reads as though it had not, and
                    there is no way to tell that from a product that genuinely stopped. */}
                <TodaySoFar data={mlData} muted={textMuted} />
              </div>
              <div>
                <div className={`text-[14px] uppercase tracking-widest font-bold mb-1 ${textMuted}`}>Remaining</div>
                <span className="text-3xl font-bold tabular-nums text-[var(--t-accent)]">{mlData.currentMonth.forecastRemaining.toLocaleString()}</span>
              </div>
            </div>
            {/* Bottom: last month */}
            <div className="flex items-baseline gap-2">
              <span className={`text-2xl font-bold tabular-nums ${textMain}`}>{mlData.currentMonth.lastMonthTotal.toLocaleString()}</span>
              <span className={`text-[16.5px] ${textMuted}`}>last month</span>
            </div>
          </div>
        </div>

        {/* Order Math — two columns on a wide panel, stacked on a narrow one */}
        <OrderMathCard data={mlData} leadTime={plan.days} planSource={plan.source} planNeeded={plan.needed} coverageDays={params.coverage} unitsOnOrder={onOrderQty} lm={lm} skuSt={skuSt} demandVolatilityColor={mlData?.demandVolatilityColor} />

        <div>
          <div className={`text-[14px] uppercase tracking-widest font-bold mb-3 ${textMuted}`}>Sales Forecast by Month</div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {mlData.monthCards?.map((card, i) => <MonthForecastCard key={i} card={card} lm={lm} />)}
          </div>
        </div>
        </>
        )}

        <div>
          <button onClick={() => setShowHistory(!showHistory)}
            className={`flex items-center gap-2 text-[15px] transition-colors ${"text-[var(--t-accent)] hover:text-[var(--t-accent)]"}`}>
            <svg className={`h-3 w-3 transition-transform ${showHistory ? "rotate-90" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
            </svg>
            {showHistory ? "Hide sales history" : "View monthly sales history"}
          </button>
          {showHistory && mlData.monthlySummary && <div className="mt-3"><MonthlySummaryTable rows={mlData.monthlySummary} lm={lm} /></div>}
        </div>
      </div>
      {showPoModal && <OrderInTransitModal skuId={skuId} existing={openPO} onSave={handleSavePO} onClear={handleClearPO} onClose={() => setShowPoModal(false)} lm={lm} suppliers={suppliers} defaultMode={params.freightMode}
        params={params} />}
      {showEvents && <EventsModal skuId={skuId} events={mlData.events || []} hasPrice={mlData.hasPrice} hasPromotion={mlData.hasPromotion}
        priceVaried={mlData.priceVaried} priceWellSampled={mlData.priceWellSampled} lastPrice={mlData.lastPrice}
        priceTiers={mlData.priceTiers} priceTrainedMin={mlData.priceTrainedMin} priceTrainedMax={mlData.priceTrainedMax}
        priceSafeMin={mlData.priceSafeMin} priceSafeMax={mlData.priceSafeMax} priceModeled={mlData.priceModeled}
        priceResponse={mlData.priceResponse} priceMixedDays={mlData.priceMixedDays || 0}
        holidays={((mlData.holidays || {}).effects || []).filter(h => h.status === "moves")}
        posEvents={mlData.posEvents || []} livePrice={mlData.livePrice}
        lastRecordedDate={mlData.lastRecordedDate}
        onSave={handleSaveEvents} onClose={() => setShowEvents(false)} lm={lm} />}
    </div>
  );
}
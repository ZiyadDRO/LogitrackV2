import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useDebouncedCallback } from 'use-debounce';
import ForecastDetailsDrawer from '../ForecastDetailsDrawer';
import ForecastChart from './ForecastChart';
import { OrderInTransitModal, EventsModal } from './modals';
import { ArrivalPromptBanner } from './ArrivalPrompts';
import { Tip, ReliabilityBadge, MetricCard, MonthForecastCard, MonthlySummaryTable, PriceTierTable, SignalPill } from './common';
import { formatDate, isoToDisplay, urgencyLevel, autoStrategy, computeSkuLeadTimeStats,
         makeSupId, makeOrdId, todayMs, todayStr, STATUS_CONFIG, poEtaDays, planningLeadTime, LEAD_TIME_MIN_DELIVERIES, FREIGHT_MODES, leadTimeTracks, supplierOf, resolveLane, readSlowShipment, slowShipmentNote,
         readLeadTimeChanges, latestChangeAt, applyLeadTimeChange, undoLastLeadTimeChange,
         describeLeadTimeChange, parkOrder, adoptParkedOrders } from '../lib/helpers';
import { API, TZ, fetchJson } from '../lib/api';
import { saveStorage } from '../lib/storage';
import { GROQ_URL, GROQ_MODEL } from '../lib/ai';

// ─── ORDER MATH BREAKDOWN ─────────────────────
export function OrderMathCard({ data, leadTime, coverageDays, unitsOnOrder, lm, skuSt, demandVolatilityColor }) {
  const bg    = lm ? "bg-slate-50 border-slate-200"    : "bg-[#1c1c20] border-white/10";
  const label = lm ? "text-slate-500"                  : "text-slate-500";
  const head  = lm ? "text-slate-500 border-slate-200" : "text-slate-600 border-white/10";
  const val   = lm ? "text-slate-900"                  : "text-white";
  const sub   = lm ? "text-slate-600 border-slate-200" : "text-slate-300 border-white/10";
  const divider = lm ? "border-slate-200" : "border-white/10";

  // Detect lead time mode
  const usingP80 = skuSt?.p80 !== null && leadTime === skuSt?.p80 && skuSt?.p80 !== skuSt?.avg;
  const usingAvg = skuSt?.avg !== null && leadTime === skuSt?.avg;
  const daysEarlier = skuSt?.p80 && skuSt?.avg ? skuSt.p80 - skuSt.avg : null;

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
      <div className="p-3.5 font-mono text-xs space-y-1.5">
        <div className={`text-[11px] uppercase tracking-widest font-bold pb-1.5 mb-0.5 border-b ${head}`}>Order qty breakdown</div>
        <div className={`flex justify-between ${label}`}><span>Demand after delivery ({coverageDays}d)</span><span className={`${val} tabular-nums`}>{data.coverageQty.toLocaleString()}</span></div>
        <div className={`flex justify-between ${label}`}><span>Demand buffer</span><span className="text-violet-500 tabular-nums">+ {data.safetyStock.toLocaleString()}</span></div>
        <div className={`flex justify-between ${sub} border-t pt-1.5 mt-0.5`}><span>Target stock level</span><span className="font-bold tabular-nums">{data.targetInventory.toLocaleString()}</span></div>
        <div className={`flex justify-between text-[11px] ${label}`}><span>Est. stock at delivery</span><span className="text-rose-500 tabular-nums">− {(data.stockAtDelivery ?? data.projectedStockReorder).toLocaleString()}</span></div>
        {unitsOnOrder > 0 && (
          <div className={`flex justify-between text-[11px] ${label}`}><span>Open PO in transit</span><span className="text-violet-500 tabular-nums">− {unitsOnOrder.toLocaleString()}</span></div>
        )}
        <div className={`flex justify-between font-bold border-t ${divider} pt-1.5 mt-0.5 text-sm ${lm ? "text-violet-700" : "text-violet-400"}`}><span>Units to order</span><span className="tabular-nums">= {data.orderQty.toLocaleString()}</span></div>
      </div>

      {/* ── Protection summary ── */}
      <div className={`border-t md:border-t-0 md:border-l ${divider} divide-y ${divider}`}>
        <div className={`text-[10px] uppercase tracking-widest font-bold px-3.5 pt-3.5 pb-1.5 ${lm ? "text-slate-400" : "text-slate-600"}`}>Protection breakdown</div>

        {/* Demand buffer row */}
        <div className={`flex items-start gap-2.5 px-3.5 py-2 ${lm ? "bg-white" : "bg-transparent"}`}>
          <div className={`mt-0.5 h-4 w-4 rounded-full flex items-center justify-center shrink-0 ${lm ? "bg-emerald-100" : "bg-emerald-950/40"}`}>
            <svg className={`h-2.5 w-2.5 ${lm ? "text-emerald-600" : "text-emerald-400"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7"/></svg>
          </div>
          <div className="flex-1 min-w-0">
            {/* While the measured recommendation is being computed, show NOTHING rather
                than a provisional level. A number that appears and then changes invites
                a decision the user would have to revisit. */}
            {prot?.status === "calculating" ? (
              <div className={`text-[11px] font-semibold ${lm ? "text-slate-700" : "text-slate-300"}`}>Protection level</div>
            ) : (
              <div className={`text-[11px] font-semibold ${lm ? "text-slate-700" : "text-slate-300"}`}>
                {prot?.label || "Standard"} protection ({prot?.servicePct ?? 95}%)
                <span className={`ml-1.5 font-mono font-normal text-[11px] ${lm ? "text-slate-400" : "text-slate-500"}`}>+{data.safetyStock} units</span>
              </div>
            )}
            <div className={`text-[11px] mt-0.5 ${prot?.status === "calculating" ? (lm ? "text-violet-600" : "text-violet-300") : (lm ? "text-slate-400" : "text-slate-500")}`}>
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
              ) : prot?.status === "stale" ? "re-testing for your new settings…"
                : prot?.source === "backtest" ? "cheapest across your own sales history"
                : prot?.source === "economics" ? "provisional estimate — not yet tested against your history"
                : prot?.costKnown ? `set from ${Math.round(prot.marginPct)}% margin — too little history to test`
                : "default — add a unit cost to tune"}{prot?.overridden ? " · manual override" : ""}
            </div>
          </div>
        </div>

        {/* Delivery timing row */}
        <div className={`flex items-start gap-2.5 px-3.5 py-2 ${lm ? "bg-white" : "bg-transparent"}`}>
          {usingP80 ? (
            <div className={`mt-0.5 h-4 w-4 rounded-full flex items-center justify-center shrink-0 ${lm ? "bg-violet-100" : "bg-violet-950/40"}`}>
              <svg className={`h-2.5 w-2.5 ${lm ? "text-violet-600" : "text-violet-400"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7"/></svg>
            </div>
          ) : (
            <div className={`mt-0.5 h-4 w-4 rounded-full flex items-center justify-center shrink-0 ${lm ? "bg-amber-100" : "bg-amber-950/40"}`}>
              <svg className={`h-2.5 w-2.5 ${lm ? "text-amber-600" : "text-amber-400"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>
            </div>
          )}
          <div className="flex-1 min-w-0">
            {usingP80 ? (
              <>
                <div className={`text-[11px] font-semibold ${lm ? "text-slate-700" : "text-slate-300"}`}>
                  Late deliveries — P80 active
                  <span className={`ml-1.5 font-mono font-normal text-[11px] ${lm ? "text-violet-500" : "text-violet-500"}`}>orders {daysEarlier}d earlier</span>
                </div>
                <div className={`text-[11px] mt-0.5 ${lm ? "text-slate-400" : "text-slate-500"}`}>
                  80% of past deliveries arrive within {leadTime}d — covers up to {daysEarlier}d delay
                </div>
              </>
            ) : (
              <>
                <div className={`text-[11px] font-semibold ${lm ? "text-amber-700" : "text-amber-400"}`}>
                  Late deliveries — no protection
                  {usingAvg && skuSt?.p80 && <span className={`ml-1.5 font-normal text-[11px] ${lm ? "text-slate-400" : "text-slate-500"}`}>using avg · switch to P80 to protect</span>}
                </div>
                <div className={`text-[11px] mt-0.5 ${lm ? "text-slate-400" : "text-slate-500"}`}>
                  {usingAvg
                    ? `Using avg lead time (${leadTime}d) — ~50% of deliveries arrive on time`
                    : `Manual lead time (${leadTime}d) — log ≥ 3 orders to unlock P80`}
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
export function SkuSignalStrip({ mlData, statusCfg, apiKey, lm }) {
  // State resets on SKU change via key={mlData.skuId} at the call site (remount).
  const [open, setOpen]               = useState(false);
  const [explanation, setExplanation] = useState(null);
  const [loading, setLoading]         = useState(false);

  const lowRel = ["YELLOW", "ORANGE", "RED"].includes(mlData.reliabilityColor);
  const explain = async () => {
    setLoading(true); setExplanation(null);
    const prompt = `You are an inventory data analyst. A demand forecast for SKU "${mlData.skuName}" has been flagged as unreliable.\nReliability: ${mlData.reliabilityColor}\nMessage: ${mlData.reliabilityMessage}\nStatus: ${mlData.status} — ${mlData.statusMessage}\nDays of history: ${mlData.daysOfHistory}\nIn 3-4 short bullet points (use • character), explain why the forecast has low reliability, what data issues are causing it, and what the user can do to improve accuracy. Be specific and practical. No markdown, no headers.`;
    try {
      const res = await fetch(GROQ_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${apiKey}` },
        body: JSON.stringify({ model: GROQ_MODEL, messages: [{ role: "user", content: prompt }], max_tokens: 300, temperature: 0.3 }),
      });
      const data = await res.json();
      setExplanation(data?.choices?.[0]?.message?.content || "Could not generate explanation.");
    } catch { setExplanation("Failed to fetch explanation."); }
    finally  { setLoading(false); }
  };

  const relTone    = { GREEN: "emerald", YELLOW: "amber", ORANGE: "orange", RED: "rose" };
  const statusTone = { FORECAST_ELIGIBLE: "emerald", LOW_CONFIDENCE: "amber", CONFLICTING: "orange", INSUFFICIENT: "rose" };
  const dataLabel  = { GREEN: "Strong data", YELLOW: "Moderate data", ORANGE: "Limited data", RED: "Thin data" }[mlData.reliabilityColor] || "Strong data";
  const noiseTone  = { GREEN: "emerald", YELLOW: "amber", ORANGE: "orange" }[mlData.demandVolatilityColor];
  const noiseLabel = { GREEN: "Low noise", YELLOW: "Moderate noise", ORANGE: "High noise" }[mlData.demandVolatilityColor];

  let pricePill = null;
  if (mlData.hasPrice && mlData.priceModeled && mlData.priceVaried) {
    const n = mlData.priceTiers?.length || 0;
    pricePill = mlData.priceWellSampled
      ? { tone: "emerald", text: `Price modelled${n ? ` · ${n} tier${n !== 1 ? "s" : ""}` : ""}` }
      : { tone: "amber", text: "Price — limited data" };
  }
  const zeroStock = mlData.stockoutRowsDropped > 0;
  const hasDetail = mlData.statusMessage || mlData.reliabilityMessage || (pricePill && mlData.priceTiers?.length) || zeroStock;

  const card = lm ? "bg-white border-slate-200" : "bg-[#161619] border-white/10";
  const head = lm ? "text-slate-500" : "text-slate-600";

  return (
    <div className={`${card} border rounded-2xl px-4 py-3`}>
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`text-[11px] uppercase tracking-widest font-bold mr-1 ${head}`}>Forecast health</span>
        <SignalPill tone={statusTone[mlData.status] || "slate"} lm={lm}>
          <span className={`h-1.5 w-1.5 rounded-full ${statusCfg.dot}`} />{statusCfg.label}
        </SignalPill>
        <SignalPill tone={relTone[mlData.reliabilityColor] || "emerald"} lm={lm}>{dataLabel}</SignalPill>
        {noiseLabel && <SignalPill tone={noiseTone} lm={lm}>{noiseLabel}{mlData.intervalWidth ? ` · ${mlData.intervalWidth}% band` : ""}</SignalPill>}
        {pricePill && <SignalPill tone={pricePill.tone} lm={lm}>{pricePill.text}</SignalPill>}
        {zeroStock && <SignalPill tone="slate" lm={lm}>{mlData.stockoutRowsDropped} zero-stock day{mlData.stockoutRowsDropped > 1 ? "s" : ""} excluded</SignalPill>}
        <div className="flex-1 min-w-[12px]" />
        {lowRel && !explanation && !loading && (
          <button onClick={() => { setOpen(true); explain(); }}
            className={`text-[11px] font-semibold ${lm ? "text-amber-700 hover:text-amber-800" : "text-amber-400 hover:text-amber-300"}`}>Why?</button>
        )}
        {hasDetail && (
          <button onClick={() => setOpen(o => !o)}
            className={`text-[11px] font-semibold ${lm ? "text-slate-500 hover:text-slate-700" : "text-slate-500 hover:text-slate-300"}`}>{open ? "Hide details" : "Details"}</button>
        )}
      </div>

      {open && (
        <div className={`mt-3 pt-3 border-t space-y-3 ${lm ? "border-slate-200" : "border-white/10"}`}>
          {mlData.statusMessage && <p className={`text-xs leading-relaxed ${lm ? "text-slate-500" : "text-slate-500"}`}>{mlData.statusMessage}</p>}
          <ReliabilityBadge color={mlData.reliabilityColor} message={mlData.reliabilityMessage} intervalWidth={mlData.intervalWidth} demandVolatilityColor={mlData.demandVolatilityColor} lm={lm} />
          {zeroStock && (
            <p className={`text-[11px] leading-relaxed ${lm ? "text-slate-500" : "text-slate-500"}`}>
              {mlData.stockoutRowsDropped} zero-stock day{mlData.stockoutRowsDropped > 1 ? "s were" : " was"} removed from training so the model doesn't learn artificially low demand during stockouts.
            </p>
          )}
          {pricePill && mlData.priceTiers?.length > 0 && (
            <div>
              <div className={`text-[11px] uppercase tracking-widest font-bold mb-1 ${head}`}>Price tiers</div>
              <PriceTierTable tiers={mlData.priceTiers} lm={lm} />
            </div>
          )}
          {(loading || explanation) && (
            <div>
              {loading && (
                <div className="flex items-center gap-2">
                  <div className="h-2.5 w-2.5 border-2 border-amber-400 border-t-transparent rounded-full animate-spin" />
                  <span className={`text-[11px] ${lm ? "text-amber-600" : "text-amber-400"}`}>Analysing data quality…</span>
                </div>
              )}
              {explanation && <p className={`text-xs leading-relaxed whitespace-pre-wrap ${lm ? "text-slate-700" : "text-slate-300"}`}>{explanation}</p>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── SKU DETAIL PANEL ─────────────────────────
export default function SkuDetailPanel({ skuId, skuList, params, onParamChange, openPOs, setOpenPOs, apiKey, lm, suppliers, setSuppliers, receiveStock, refreshNonce, onExport, exporting,
                                        arrival = null, onConfirmArrival = null, onIgnoreArrival = null }) {
  const [mlData,      setMlData]      = useState(null);
  const [loading,     setLoading]     = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showPoModal, setShowPoModal] = useState(false);
  const [showEvents,  setShowEvents]  = useState(false);
  const [showForecastDetails, setShowForecastDetails] = useState(false);
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
  useEffect(() => { setDraft(params); }, [skuId]);
  const debouncedCommit = useDebouncedCallback(p => onParamChange(p), 600);
  const updateDraft = (key, value) => updateDraftMany({ [key]: value });
  /* Several fields move together (the default lane's baseline IS params.leadTime).
     Setting them one at a time would clobber, since each call reads the same draft. */
  const updateDraftMany = (patch) => {
    const next = { ...draftParams, ...patch };
    setDraft(next);
    debouncedCommit(next);
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
  const fetchData  = useCallback(() => {
    if (!skuId) return;
    // After bootstrap, read correct strategy from latest data
    if (mlDataRef.current?.demandVolatilityColor) {
      demandStratRef.current = autoStrategy(mlDataRef.current.demandVolatilityColor);
    }
    setLoading(true);
    const { stock, leadTime, coverage, months } = params;
    const costQ = (params.unitCost != null && params.unitCost !== "") ? `&unit_cost=${params.unitCost}` : "";
    const feesQ = (params.fees != null && params.fees !== "") ? `&fees=${params.fees}` : "";
    const protQ = params.protection ? `&protection=${params.protection}` : "";
    const buildUrl = strat =>
      `${API}/api/forecast?sku_id=${encodeURIComponent(skuId)}&stock=${stock}&lead_time_days=${plan.days}&coverage_days=${coverage}&strategy=${strat}&forecast_months=${months}&units_on_order=${onOrderQty}${onOrderEta != null ? `&on_order_eta_days=${onOrderEta}` : ""}${costQ}${feesQ}${protQ}&tz=${TZ}`;
    const stratUsed = demandStratRef.current;
    fetchJson(buildUrl(stratUsed), undefined, `Forecast for ${skuId}`).then(d => {
      if (!d) { setLoading(false); return; }
      const correctStrat = autoStrategy(d.demandVolatilityColor);
      demandStratRef.current = correctStrat;
      if (!hasBootstrapped.current && stratUsed !== correctStrat) {
        // First fetch used wrong strategy — silently re-fetch with correct one before showing
        hasBootstrapped.current = true;
        fetchJson(buildUrl(correctStrat), undefined, `Forecast for ${skuId}`)
          .then(d2 => { setMlData(d2 || d); setLoading(false); });
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
  const handleSavePO = (po) => {
    const next = { ...openPOs, [skuId]: po }; setOpenPOs(next); saveStorage("logitrack_pos", next); setShowPoModal(false);
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
      id: makeOrdId(), orderedDate: po.ordered, receivedDate: null,
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
    // Mark the linked supplier in-transit order received too, so it can't be counted again.
    if (po && setSuppliers) {
      setSuppliers(prev => {
        const next = { ...prev };
        for (const sid of Object.keys(next)) {
          const orders = next[sid].orders || [];
          const idx = orders.findIndex(o => o.skuId === skuId && !o.receivedDate);
          if (idx >= 0) {
            const copy = orders.slice();
            copy[idx] = { ...copy[idx], receivedDate: todayStr() };
            next[sid] = { ...next[sid], orders: copy };
            break;
          }
        }
        return next;
      });
    }
    const next = { ...openPOs }; delete next[skuId]; setOpenPOs(next); saveStorage("logitrack_pos", next); setShowPoModal(false);
  };

  if (!mlData) return (
    <div className="flex-1 flex items-center justify-center">
      <div className="flex flex-col items-center gap-3">
        <div className="h-6 w-6 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
        <span className={`text-xs font-mono ${lm ? "text-slate-500" : "text-slate-500"}`}>Loading forecast…</span>
      </div>
    </div>
  );

  const statusCfg = STATUS_CONFIG[mlData.status] || STATUS_CONFIG.INSUFFICIENT;
  const projTotal = mlData.currentMonth.unitsSoFar + mlData.currentMonth.forecastRemaining;
  const pctChange = mlData.currentMonth.lastMonthTotal > 0
    ? ((projTotal - mlData.currentMonth.lastMonthTotal) / mlData.currentMonth.lastMonthTotal) * 100 : 0;
  const hasOpenPO   = !!openPO;
  const urgency     = urgencyLevel(mlData.daysUntilReorder, hasOpenPO);
  const showReorder = mlData.daysUntilReorder != null && !hasOpenPO;

  // Theming shortcuts
  const sidebarBg  = lm ? "bg-slate-100 border-slate-200" : "bg-[#161619] border-white/10";
  const lbl        = lm ? "block text-xs text-slate-600 font-semibold uppercase tracking-widest mb-1.5"
                        : "block text-[11px] text-slate-400 font-semibold uppercase tracking-widest mb-1.5";
  const inp        = lm ? "w-full bg-white border border-slate-300 rounded-lg px-3 py-2 text-sm text-slate-900 focus:outline-none focus:border-violet-500"
                        : "w-full bg-slate-900 border border-slate-700/60 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-violet-600/60 tabular-nums";
  const divider    = lm ? "border-slate-200" : "border-white/10";
  const infoHead   = lm ? "text-[11px] uppercase tracking-widest text-slate-500 font-bold" : "text-[9px] uppercase tracking-widest text-slate-700 font-bold";
  const infoLabel  = lm ? "text-slate-500 text-xs" : "text-slate-600 text-[10px]";
  const infoVal    = lm ? "text-slate-800" : "text-slate-400";
  const mainBg     = lm ? "bg-slate-50" : "";
  const cardBg     = lm ? "bg-white border-slate-200" : "bg-[#161619] border-white/10";
  const textMain   = lm ? "text-slate-900" : "text-white";
  const textMuted  = lm ? "text-slate-500" : "text-slate-500";

  // CI button active/inactive
  const ciBtnActive   = lm ? "bg-violet-50 border-violet-400 ring-1 ring-violet-200" : "bg-violet-950/40 border-violet-700/60 ring-1 ring-violet-700/30";
  const ciBtnInactive = lm ? "bg-white border-slate-300 hover:border-slate-400" : "bg-slate-900/60 border-slate-700/40 hover:border-slate-600";
  const ciLblActive   = lm ? "text-violet-700" : "text-violet-300";
  const ciLblInactive = lm ? "text-slate-700" : "text-slate-300";
  const ciCiActive    = lm ? "text-violet-600" : "text-violet-400";
  const ciCiInactive  = lm ? "text-slate-400" : "text-slate-600";
  const ciSub         = lm ? "text-slate-500" : "text-slate-500";

  // Month buttons
  const moBtnActive   = lm ? "bg-violet-600 border-violet-500 text-white" : "bg-violet-600 border-violet-500 text-white shadow-lg shadow-violet-900/30";
  const moBtnInactive = lm ? "bg-white border-slate-300 text-slate-600 hover:border-slate-400 hover:text-slate-800" : "bg-slate-900 border-slate-700/60 text-slate-400 hover:border-slate-600";

  // On order badge
  const poCard = lm ? "bg-violet-50 border-violet-300" : "bg-violet-950/30 border-violet-800/40";
  const poHead = lm ? "text-violet-700" : "text-violet-300";
  const poText = lm ? "text-slate-600" : "text-slate-400";

  return (
    <div className="flex h-full">
      {/* Forecast "why" panel — opened from the Forecast details button in the header */}
      <ForecastDetailsDrawer data={mlData} lm={lm} open={showForecastDetails} onClose={() => setShowForecastDetails(false)} />
      {/* Params panel */}
      <div ref={panelRef} style={{ width: panelWidth }}
        className={`relative shrink-0 ${sidebarBg} border-r flex flex-col`}>
        <div onMouseDown={startResizePanel} title="Drag to resize"
          className={`absolute top-0 right-0 h-full w-1.5 cursor-col-resize z-30 ${lm ? "hover:bg-violet-300" : "hover:bg-violet-600/50"}`} />
        <div className="flex-1 flex flex-col p-5 gap-5 overflow-y-auto sku-scroll">
        <div className="space-y-4">
          <div>
            <label className={lbl}>Units in Stock</label>
            <input type="number" value={draftParams.stock} onChange={e => updateDraft("stock", Number(e.target.value))} className={inp} />
          </div>
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
            <div className={`rounded-xl border overflow-hidden ${lm ? "bg-slate-50 border-slate-200" : "bg-slate-900/40 border-slate-700/40"}`}>
              <div className={`px-3 py-2 flex items-center justify-between border-b ${lm ? "border-slate-200" : "border-slate-700/40"}`}>
                <span className={`text-[10px] uppercase tracking-widest font-bold ${lm ? "text-slate-500" : "text-slate-500"}`}>
                  Lead time by shipping method
                </span>
                <label className="flex items-center gap-1.5">
                  <span className={`text-[10px] ${lm ? "text-slate-500" : "text-slate-500"}`}>Usually ships by</span>
                  <select value={draftParams.freightMode || resolveLane(skuId, draftParams, suppliers) || Object.keys(FREIGHT_MODES)[0]}
                    onChange={e => updateDraft("freightMode", e.target.value)}
                    className={`rounded border px-1.5 py-0.5 text-[11px] ${lm ? "bg-white border-slate-300 text-slate-900" : "bg-slate-900 border-slate-700 text-slate-200"}`}>

                    {Object.entries(FREIGHT_MODES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                  </select>
                </label>
              </div>
              <div className={`grid grid-cols-[1fr_auto_auto_auto] gap-x-3 px-3 py-1.5 text-[10px] uppercase tracking-wider font-semibold ${lm ? "text-slate-400" : "text-slate-600"}`}>
                <span>Method</span><span className="text-right">Baseline</span>
                {/* "Deliveries", not "Ships" or "Orders" — this counts shipments that
                    ARRIVED (both an ordered and a received date). An outstanding PO isn't
                    in it, so "Orders" overstated what the number covers, and the supplier
                    tables called the identical figure something else again. */}
                <span className="text-right">P80</span><span className="text-right">Deliveries</span>
              </div>
              {tracks.map(t => (
                <div key={t.mode}
                  className={`grid grid-cols-[1fr_auto_auto_auto] gap-x-3 items-center px-3 py-1.5 border-t text-[11px] ${lm ? "border-slate-200" : "border-slate-700/30"} ${t.isDefault ? (lm ? "bg-violet-50" : "bg-violet-950/20") : ""}`}>
                  <span className={`font-medium ${t.used ? (lm ? "text-slate-700" : "text-slate-300") : (lm ? "text-slate-400" : "text-slate-600")}`}>
                    {t.label}
                    {t.isDefault && <span className={`ml-1.5 text-[9px] uppercase tracking-wide ${lm ? "text-violet-600" : "text-violet-400"}`}>default</span>}
                  </span>
                  {/* `t.baseline` is the RESOLVED number for this lane — this product's
                      own, else the one inherited from its supplier, else (for the default
                      lane) params.leadTime. This used to read only the product's own
                      params, so a supplier-wide baseline planned the product silently
                      while its row sat blank, and the default row showed a stale
                      params.leadTime that wasn't what planning used. Clearing the box
                      still means "inherit": the supplier's number reappears, italic. */}
                  <input type="number" placeholder="—"
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
                    title={t.inherited ? `Inherited from ${supplierName || "this supplier"} — type here to give this product its own` : undefined}
                    className={`w-16 text-right rounded border px-1.5 py-0.5 font-mono text-[11px] ${
                      t.inherited
                        ? (lm ? "bg-white border-slate-200 text-slate-400 italic" : "bg-slate-900 border-slate-700/50 text-slate-500 italic")
                        : (lm ? "bg-white border-slate-300 text-slate-900" : "bg-slate-900 border-slate-700 text-slate-200")}`} />
                  <span className={`text-right font-mono tabular-nums w-12 ${t.p80 != null ? (lm ? "text-emerald-700 font-bold" : "text-emerald-400 font-bold") : (lm ? "text-slate-300" : "text-slate-700")}`}>
                    {t.p80 != null ? `${t.p80}d` : "—"}
                  </span>
                  <span className={`text-right font-mono tabular-nums w-8 ${lm ? "text-slate-500" : "text-slate-500"}`}>{t.n || "—"}</span>
                </div>
              ))}
              {/* Carries the resolved planning number now that the card above is gone —
                  the one figure the whole section exists to produce, stated once. The
                  manual branch also carries the only way out of manual mode, which used
                  to live on the card. */}
              <div className={`px-3 py-2 border-t text-[10px] ${lm ? "border-slate-200 text-slate-500" : "border-slate-700/40 text-slate-500"}`}>
                {params.leadTimeMode === "manual"
                  ? <>Planning on <b>{plan.days}d</b> — pinned by you, ignoring delivery history.{" "}
                      <button onClick={() => updateDraft("leadTimeMode", undefined)}
                        className={`font-semibold underline ${lm ? "text-slate-600" : "text-slate-300"}`}>measure it instead</button></>
                  : activeTrack.p80 != null
                  ? <>Planning on <b>{plan.days}d</b> — measured {activeTrack.label}, P80 of {activeTrack.n} shipments. Your baseline is only used if that record goes away; editing it never drops a delivery.</>
                  : draftParams.freightMode
                    ? <>Planning on <b>{plan.days}d</b> — {activeTrack.inherited ? `${supplierName || "supplier"}'s` : "your"} {activeTrack.label} baseline. {activeTrack.needed} more {activeTrack.label} delivery{activeTrack.needed === 1 ? "" : " deliveries"} and it switches to this product&apos;s own measured P80. No other product&apos;s shipments are ever mixed in.</>
                    : <>Fill in the row for however this product ships — that sets it as the method to plan from. Until then it pools every delivery.</>}
              </div>
              {/* The opt-out checkbox that used to sit here is gone. It existed back when
                  a product could inherit its supplier's MEASURED average — pooling that
                  once dragged a whole catalogue's planning from 44 days to 90 because one
                  made-to-order sibling shared the pool. That pooling was removed; all a
                  supplier passes down now is a baseline you typed, and typing a number in
                  the row above already overrides it. A checkbox to refuse an inheritance
                  you can simply overwrite is a switch with nothing behind it. */}
              {tracks.some(t => t.inherited) && (
                <div className={`px-3 py-1.5 border-t text-[10px] ${lm ? "border-slate-200 text-slate-500" : "border-slate-700/40 text-slate-500"}`}>
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
              <div className={`rounded-xl border overflow-hidden ${lm ? "bg-slate-50 border-slate-200" : "bg-slate-900/40 border-slate-700/40"}`}>
                <div className={`px-3 py-1.5 text-[10px] uppercase tracking-widest font-bold border-b ${lm ? "text-slate-500 border-slate-200" : "text-slate-500 border-slate-700/40"}`}>
                  Lead time changes ({changes.length})
                </div>
                {changes.slice().reverse().map((c, i) => (
                  <div key={c.at + i} className={`px-3 py-2 flex items-start gap-2 border-b last:border-b-0 ${lm ? "border-slate-200/70" : "border-slate-700/25"}`}>
                    <div className="flex-1 min-w-0">
                      <div className={`text-[11px] font-semibold ${i === 0 ? (lm ? "text-slate-800" : "text-slate-200") : (lm ? "text-slate-500" : "text-slate-500")}`}>
                        {describeLeadTimeChange(c)}
                      </div>
                      <div className={`text-[10px] ${lm ? "text-slate-500" : "text-slate-600"}`}>
                        {isoToDisplay(c.at)}
                        {i === 0
                          ? <> · measuring from here{plan.supersededN > 0 ? ` · ${plan.supersededN} earlier ${plan.supersededN === 1 ? "delivery" : "deliveries"} set aside` : ""}</>
                          : " · superseded"}
                      </div>
                    </div>
                    {i === 0 && (
                      <button onClick={() => { const patch = undoLastLeadTimeChange(draftParams); if (patch) updateDraftMany(patch); }}
                        className={`shrink-0 text-[10px] font-semibold px-2 py-1 rounded-lg border transition-all ${lm ? "bg-white border-slate-300 text-slate-600 hover:border-slate-400" : "bg-slate-800 border-slate-700 text-slate-300 hover:border-slate-500"}`}>
                        Undo
                      </button>
                    )}
                  </div>
                ))}
                <div className={`px-3 py-1.5 text-[9.5px] border-t ${lm ? "border-slate-200 text-slate-500" : "border-slate-700/40 text-slate-500"}`}>
                  Measured figures restart at the newest change, so P80 rebuilds from deliveries logged since then.
                  {plan.priorP80 ? ` The previous arrangement measured ${plan.priorP80}d.` : ""}
                  {" "}Nothing is deleted — undo brings the older deliveries straight back.
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
              <div className={`rounded-xl border px-3 py-2.5 space-y-2 ${lm ? "bg-white border-violet-300" : "bg-slate-900/60 border-violet-800/60"}`}>
                <div className={`text-[11px] font-bold ${lm ? "text-slate-800" : "text-slate-200"}`}>New lead times from today</div>
                <div className={`text-[10px] ${lm ? "text-slate-500" : "text-slate-500"}`}>
                  Change what moved and leave the rest — anything you don&apos;t touch keeps its current number.
                </div>
                {tracks.map(t => (
                  <div key={t.mode} className="flex items-center gap-2">
                    <span className={`text-[11px] w-14 shrink-0 ${lm ? "text-slate-600" : "text-slate-400"}`}>{t.label}</span>
                    <input type="number" min="1" placeholder="—"
                      value={ltChange[t.mode] ?? ""}
                      onChange={e => setLtChange(c => ({ ...c, [t.mode]: e.target.value }))}
                      className={`w-16 text-right rounded border px-1.5 py-0.5 font-mono text-[11px] ${lm ? "bg-white border-slate-300 text-slate-900" : "bg-slate-900 border-slate-700 text-slate-200"}`} />
                    <span className={`text-[10px] ${lm ? "text-slate-400" : "text-slate-600"}`}>
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
                    className="text-[11px] font-bold px-3 py-1.5 rounded-lg bg-violet-700 hover:bg-violet-600 text-white transition-colors">
                    Apply from today
                  </button>
                  <button onClick={() => setLtChange(null)}
                    className={`text-[11px] px-2 py-1.5 ${lm ? "text-slate-500 hover:text-slate-700" : "text-slate-400 hover:text-slate-200"}`}>cancel</button>
                </div>
                <div className={`text-[9.5px] ${lm ? "text-slate-400" : "text-slate-600"}`}>
                  Deliveries before today are set aside, not deleted. Undo brings them back.
                </div>
              </div>
            ) : (
              <button
                onClick={() => setLtChange(Object.fromEntries(tracks.map(t => [t.mode, t.baseline ?? ""])))}
                className={`w-full text-[11px] font-semibold py-1.5 rounded-lg border transition-all ${lm ? "bg-white border-slate-300 text-slate-600 hover:border-violet-400 hover:text-violet-700" : "bg-slate-800/60 border-slate-700 text-slate-300 hover:border-violet-700 hover:text-violet-300"}`}>
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
                ? (lm ? "bg-sky-50 border-sky-200" : "bg-sky-950/20 border-sky-900/40")
                : (lm ? "bg-amber-50 border-amber-200" : "bg-amber-950/20 border-amber-900/40");
              const ink = early
                ? (lm ? "text-sky-700" : "text-sky-300")
                : (lm ? "text-amber-700" : "text-amber-300");
              return (
              <div className={`rounded-lg border px-2.5 py-2 ${tone}`}>
                <div className="flex items-baseline gap-2">
                  <span className={`text-lg font-bold tabular-nums ${ink}`}>{plan.days}d</span>
                  <span className={`text-[11px] ${ink}`}>
                    on the next order
                    {oneOff?.delta ? ` · ${oneOff.delta > 0 ? "+" : ""}${oneOff.delta}d vs the usual ${normalPlan}d` : ""}
                  </span>
                  <button onClick={() => updateDraftMany({ slowShipment: undefined, nextLeadTime: undefined })}
                    className={`ml-auto text-[11px] font-semibold underline ${lm ? "text-slate-500" : "text-slate-400"}`}>discard</button>
                </div>
                {plan.reason && (
                  <div className={`text-[11px] mt-1 italic ${ink}`}>&ldquo;{plan.reason}&rdquo;</div>
                )}
                <div className={`text-[10px] mt-1 ${lm ? "text-slate-500" : "text-slate-500"}`}>
                  {early
                    ? <>Reordering later and holding a smaller buffer until it&apos;s placed.</>
                    : <>Reordering earlier and holding a bigger buffer until it&apos;s placed.</>}
                  {" "}Raise the order and this becomes its note; discard it and nothing is kept.
                </div>
                <button onClick={() => setShowSlowWhy(v => !v)}
                  className={`text-[10px] underline mt-1 ${ink}`}>
                  {showSlowWhy ? "hide" : "what happens to my history?"}
                </button>
                {showSlowWhy && (
                  <div className={`text-[10px] mt-1 leading-relaxed ${lm ? "text-slate-500" : "text-slate-500"}`}>
                    Nothing is excluded. When it lands it counts in this product&apos;s lead times and the
                    supplier&apos;s averages like any other delivery — because it really did take that long.
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
              <div className={`rounded-xl border px-3 py-2.5 space-y-2 ${lm ? "bg-white border-slate-300" : "bg-slate-900/60 border-slate-700"}`}>
                <div className={`text-[11px] font-bold ${lm ? "text-slate-800" : "text-slate-200"}`}>The next order only</div>
                <div className="flex items-center gap-1.5">
                  {[["later", "Arriving later"], ["sooner", "Arriving sooner"]].map(([k, label]) => (
                    <button key={k} type="button" onClick={() => setSlowDraft(x => ({ ...x, dir: k }))}
                      className={`flex-1 text-[11px] font-semibold py-1 rounded-lg border transition-all ${
                        slowDraft.dir === k
                          ? (k === "sooner"
                              ? (lm ? "bg-sky-100 border-sky-400 text-sky-800" : "bg-sky-950/40 border-sky-600 text-sky-200")
                              : (lm ? "bg-amber-100 border-amber-400 text-amber-800" : "bg-amber-950/40 border-amber-600 text-amber-200"))
                          : (lm ? "bg-white border-slate-300 text-slate-500 hover:border-slate-400" : "bg-slate-900 border-slate-700/60 text-slate-500 hover:border-slate-600")}`}>
                      {label}
                    </button>
                  ))}
                </div>
                <div className="flex items-center gap-2">
                  <input type="number" min="1" autoFocus placeholder="days"
                    value={slowDraft.days}
                    onChange={e => setSlowDraft(x => ({ ...x, days: e.target.value }))}
                    className={`w-20 rounded border px-1.5 py-1 text-xs ${lm ? "bg-white border-slate-300 text-slate-900" : "bg-slate-900 border-slate-700 text-slate-200"}`} />
                  <span className={`text-[11px] ${lm ? "text-slate-500" : "text-slate-500"}`}>
                    days {slowDraft.dir === "sooner" ? "sooner than" : "later than"} the usual {normalPlan}d
                  </span>
                </div>
                <input type="text" placeholder="what happened? (port strike, expedited freight, factory shutdown…)"
                  value={slowDraft.reason}
                  onChange={e => setSlowDraft(x => ({ ...x, reason: e.target.value }))}
                  className={`w-full rounded border px-1.5 py-1 text-xs ${lm ? "bg-white border-slate-300 text-slate-900" : "bg-slate-900 border-slate-700 text-slate-200"}`} />
                {valid && (
                  <div className={`text-[10px] ${tooFast ? (lm ? "text-rose-600" : "text-rose-400") : (lm ? "text-slate-600" : "text-slate-400")}`}>
                    {tooFast
                      ? `That's more than the whole ${normalPlan}-day wait — the next order can't arrive before it's placed.`
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
                    className={`text-[11px] font-bold px-3 py-1.5 rounded-lg transition-colors ${valid && !tooFast ? "bg-violet-700 hover:bg-violet-600 text-white" : `opacity-40 cursor-not-allowed border ${lm ? "border-slate-300 text-slate-400" : "border-slate-700 text-slate-600"}`}`}>
                    Save
                  </button>
                  <button onClick={() => setSlowDraft({ open: false, dir: "later", days: "", reason: "" })}
                    className={`text-[11px] px-2 py-1.5 ${lm ? "text-slate-500 hover:text-slate-700" : "text-slate-400 hover:text-slate-200"}`}>cancel</button>
                  <span className={`text-[10px] ml-auto text-right ${lm ? "text-slate-400" : "text-slate-600"}`}>
                    The reason carries onto the order when you raise it.
                  </span>
                </div>
              </div>
              );
            })() : (
              <button onClick={() => setSlowDraft({ open: true, dir: "later", days: "", reason: "" })}
                className={`w-full text-[11px] font-semibold py-1.5 rounded-lg border transition-all ${lm ? "bg-white border-slate-300 text-slate-600 hover:border-amber-400 hover:text-amber-700" : "bg-slate-800/60 border-slate-700 text-slate-300 hover:border-amber-700 hover:text-amber-300"}`}>
                Something&apos;s affecting the next order
              </button>
            )}

            {/* SKU lead time comparison card */}
            {skuSt.n > 0 && (() => {
              const reliable    = skuSt.n >= 3;
              const activeAvg   = draftParams.leadTime === skuSt.avg;
              const activeP80   = skuSt.p80 !== null && draftParams.leadTime === skuSt.p80;
              const daysGap     = skuSt.p80 !== null && skuSt.avg !== null ? skuSt.p80 - skuSt.avg : null;
              return (
                <div className={`rounded-xl border overflow-hidden ${lm ? "border-slate-200" : "border-slate-700/50"}`}>
                  {/* Header */}
                  <div className={`px-3 py-2 flex items-center justify-between ${lm ? "bg-slate-100" : "bg-slate-800/50"}`}>
                    <span className={`text-[11px] font-bold uppercase tracking-widest ${lm ? "text-slate-500" : "text-slate-500"}`}>
                      SKU lead times
                    </span>
                    <span className={`text-[11px] ${lm ? "text-slate-400" : "text-slate-600"}`}>
                      {skuSt.n} order{skuSt.n !== 1 ? "s" : ""}
                      {!reliable && <span className={`ml-1.5 ${lm ? "text-amber-500" : "text-amber-400"}`}>· need ≥ 3 to apply</span>}
                    </span>
                  </div>

                  {/* Two-column comparison */}
                  <div className={`grid ${skuSt.p80 !== null ? "grid-cols-2" : "grid-cols-1"} divide-x ${lm ? "divide-slate-200" : "divide-slate-700/50"}`}>
                    {/* AVG column */}
                    <div className={`p-3 transition-colors ${activeAvg ? (lm ? "bg-slate-100" : "bg-slate-800/60") : (lm ? "bg-white" : "bg-transparent")}`}>
                      <div className="flex items-center justify-between mb-1.5">
                        <span className={`text-[10px] uppercase tracking-widest font-bold ${lm ? "text-slate-400" : "text-slate-500"}`}>Avg</span>
                        {activeAvg && <span className={`text-[10px] font-semibold rounded-full px-1.5 py-0.5 ${lm ? "bg-slate-200 text-slate-600" : "bg-slate-700 text-slate-300"}`}>active</span>}
                      </div>
                      <div className={`text-xl font-bold tabular-nums leading-none ${lm ? "text-slate-800" : "text-white"}`}>{skuSt.avg}d</div>
                      <div className={`text-[11px] mt-1.5 leading-snug ${lm ? "text-slate-400" : "text-slate-500"}`}>
                        No delay protection
                      </div>
                      <div className={`text-[11px] ${lm ? "text-slate-400" : "text-slate-500"}`}>
                        ~50% on-time
                      </div>
                      {reliable && (
                        <button onClick={() => updateDraft("leadTime", skuSt.avg)}
                          className={`mt-2 w-full py-1 rounded-md text-[11px] font-semibold border transition-all ${
                            activeAvg
                              ? (lm ? "bg-slate-200 border-slate-300 text-slate-500 cursor-default" : "bg-slate-700/50 border-slate-600 text-slate-400 cursor-default")
                              : (lm ? "bg-white border-slate-300 text-slate-700 hover:border-slate-400" : "bg-slate-800 border-slate-700 text-slate-300 hover:border-slate-500")
                          }`}
                          disabled={activeAvg}>
                          {activeAvg ? "Selected" : "Use avg"}
                        </button>
                      )}
                    </div>

                    {/* P80 column */}
                    {skuSt.p80 !== null && (
                      <div className={`p-3 transition-colors ${activeP80 ? (lm ? "bg-violet-50" : "bg-violet-950/30") : (lm ? "bg-white" : "bg-transparent")}`}>
                        <div className="flex items-center justify-between mb-1.5">
                          <span className={`text-[10px] uppercase tracking-widest font-bold ${lm ? "text-violet-500" : "text-violet-500"}`}>P80</span>
                          {activeP80 && <span className={`text-[10px] font-semibold rounded-full px-1.5 py-0.5 ${lm ? "bg-violet-100 text-violet-700" : "bg-violet-900/60 text-violet-300"}`}>active</span>}
                        </div>
                        <div className={`text-xl font-bold tabular-nums leading-none ${lm ? "text-violet-700" : "text-violet-400"}`}>{skuSt.p80}d</div>
                        <div className={`text-[11px] mt-1.5 leading-snug font-medium ${lm ? "text-violet-600" : "text-violet-500"}`}>
                          {daysGap ? `Orders ${daysGap}d earlier` : "Earlier order"}
                        </div>
                        <div className={`text-[11px] ${lm ? "text-slate-400" : "text-slate-500"}`}>
                          80% on-time
                        </div>
                        {reliable && skuSt.p80 !== skuSt.avg && (
                          <button onClick={() => updateDraft("leadTime", skuSt.p80)}
                            className={`mt-2 w-full py-1 rounded-md text-[11px] font-semibold border transition-all ${
                              activeP80
                                ? (lm ? "bg-violet-100 border-violet-300 text-violet-600 cursor-default" : "bg-violet-900/50 border-violet-700 text-violet-300 cursor-default")
                                : (lm ? "bg-violet-50 border-violet-300 text-violet-700 hover:bg-violet-100" : "bg-violet-950/30 border-violet-700/50 text-violet-300 hover:border-violet-500")
                            }`}
                            disabled={activeP80}>
                            {activeP80 ? "Selected" : "Use P80 ↑"}
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
                  <Tip text="How hard to guard against running out. When there is enough history and a unit cost, this is picked from the item's expected stockout cost versus buffer holding cost. Otherwise it falls back to the margin rule. You can override it.">Stockout Protection</Tip>
                </label>
                <div className={`rounded-xl border overflow-hidden ${lm ? "bg-slate-50 border-slate-200" : "bg-slate-900/40 border-slate-700/40"}`}>
                  {/* While a test is running, show NOTHING — no tiers, no percentages, no
                      buffer. A provisional figure that appears and then changes invites a
                      decision the user would have to revisit, and it only stands for a few
                      minutes. This applies to every product, including ones that will end
                      up on the estimate anyway: until the run finishes we don't know which. */}
                  {p.status === "calculating" ? (
                    <div className="px-3 py-4 flex items-center gap-2.5">
                      <svg className={`h-4 w-4 animate-spin ${lm ? "text-violet-600" : "text-violet-400"}`} fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z" />
                      </svg>
                      <div>
                        <div className={`text-[12px] font-semibold ${lm ? "text-slate-700" : "text-slate-200"}`}>Calculating…</div>
                        <div className={`text-[11px] ${lm ? "text-slate-500" : "text-slate-500"}`}>
                          Testing protection levels against this product&apos;s sales history. Usually a few minutes.
                        </div>
                      </div>
                    </div>
                  ) : (
                  <div className="grid grid-cols-[repeat(auto-fit,minmax(64px,1fr))] gap-1 p-1.5">
                    {/* auto-fit, not a fixed column count: the tier list comes from the
                        backend and grew from 4 to 5. A hardcoded grid-cols-4 left the
                        fifth option stranded on its own row at quarter width. */}
                    {p.options.map((t) => {
                      const active = t.key === p.chosen;
                      const isRec  = t.key === p.recommended;
                      return (
                        <button key={t.key} onClick={() => setProt(t.key)}
                          className={`relative rounded-lg px-1 py-1.5 text-center transition-all border ${active
                            ? (lm ? "bg-violet-600 border-violet-600 text-white" : "bg-violet-500 border-violet-500 text-white")
                            : (lm ? "bg-white border-slate-200 text-slate-600 hover:border-slate-300" : "bg-slate-900/40 border-slate-700/50 text-slate-300 hover:border-slate-600")}`}>
                          <div className="text-[11px] font-bold leading-none">{t.label}</div>
                          <div className={`text-[10px] font-mono mt-0.5 ${active ? "text-white/80" : (lm ? "text-slate-400" : "text-slate-500")}`}>{t.pct}%</div>
                          {isRec && (
                            <span className={`absolute -top-1.5 left-1/2 -translate-x-1/2 px-1 rounded-full text-[7px] uppercase tracking-wide font-bold whitespace-nowrap ${active ? "bg-white text-violet-700" : (lm ? "bg-violet-100 text-violet-700" : "bg-violet-500/20 text-violet-300")}`}>Recommended</span>
                          )}
                        </button>
                      );
                    })}
                  </div>
                  )}
                  {/* Why this was chosen — suppressed entirely while a test runs. */}
                  {p.status !== "calculating" && (
                  <div className={`px-3 py-2 text-[11px] leading-relaxed border-t ${lm ? "bg-white border-slate-200 text-slate-500" : "bg-slate-900/20 border-slate-700/40 text-slate-400"}`}>
                    {p.overridden
                      ? <><span className="font-semibold">Manual override.</span> {p.reason} <button onClick={() => setProt(p.recommended)} className={`underline ${lm ? "text-violet-600" : "text-violet-400"}`}>Reset to recommended</button></>
                      : p.reason}
                    {/* Why this product isn't on measured data. "Provisional" alone read
                        identically whether the cause was thin history, a missing cost, a
                        pricing problem, or a test still running. */}
                    {p.provisional && p.whyProvisional && (
                      <div className={`mt-1.5 text-[10px] rounded-md px-2 py-1.5 leading-relaxed ${lm ? "bg-amber-50 text-amber-700" : "bg-amber-950/20 text-amber-400"}`}>
                        <span className="font-semibold">Why this isn&apos;t measured yet: </span>{p.whyProvisional}
                      </div>
                    )}
                    {p.economics?.tiers?.length > 0 && p.source === "economics" && (
                      <div className={`mt-1.5 text-[10px] ${lm ? "text-slate-400" : "text-slate-500"}`}>
                        Meanwhile: estimated from margin ${p.economics.marginUnit}/unit and {p.economics.holdingPct}%/yr holding,
                        assuming demand error is well behaved.
                      </div>
                    )}
                    {p.source === "backtest" && p.economics && (
                      <div className={`mt-1.5 text-[10px] ${lm ? "text-slate-400" : "text-slate-500"}`}>
                        Backtest winner · {p.economics.windows} tests · holding {p.economics.holdingPct}%/yr
                      </div>
                    )}
                  </div>
                  )}
                  {/* Stats footer */}
                  {p.status !== "calculating" && disp != null && (
                    <div className={`divide-y ${lm ? "divide-slate-100 border-t border-slate-200" : "divide-slate-700/30 border-t border-slate-700/40"}`}>
                      <div className={`flex items-center justify-between px-3 py-1.5 text-[11px] ${lm ? "bg-white" : "bg-slate-900/20"}`}>
                        <Tip text={`Swing in daily sales after trend and seasonality are removed, measured against the randomness any product this size carries anyway. 1.0x is as steady as that sales volume permits; past 2.0x it moves more than its size explains.${sigObs != null && refRate ? ` Here that is about ±${sigObs.toFixed(1)} a day on ${refRate.toFixed(1)} a day.` : ""} The buffer is sized from that raw swing in units, accumulated over a lead time — not from this ratio, which divides the swing by product size so that two products of different sizes can be compared. Bigger swing means a bigger buffer; a higher ratio does not, on its own.`}>
                          <span className={lm ? "text-slate-400" : "text-slate-600"}>Sales volatility</span>
                        </Tip>
                        <span className={`font-mono font-bold ${lm ? "text-slate-600" : "text-slate-300"}`}>{disp.toFixed(1)}x{mlData.demandVolatilityLabel ? ` · ${mlData.demandVolatilityLabel}` : ""}</span>
                      </div>
                      <div className={`flex items-center justify-between px-3 py-1.5 text-[11px] ${lm ? "bg-white" : "bg-slate-900/20"}`}>
                        <Tip text={`Protection level: the probability of not stocking out during a replenishment cycle. z=${zScore?.toFixed(3)} is the standard normal score for ${p.servicePct}%.`}>
                          <span className={lm ? "text-slate-400" : "text-slate-600"}>Protection (z={zScore?.toFixed(2)})</span>
                        </Tip>
                        <span className={`font-mono font-bold ${lm ? "text-violet-600" : "text-violet-300"}`}>{p.status === "calculating" ? "calculating…" : `${p.servicePct}%${p.status === "stale" ? " · re-testing" : p.source === "backtest" ? " · tested" : p.source === "economics" ? " · provisional" : p.marginPct != null ? ` · ${Math.round(p.marginPct)}% margin` : ""}`}</span>
                      </div>
                      {/* Show details: plain-English buffer math */}
                      <button onClick={() => setShowBufferMath(v => !v)}
                        className={`w-full flex items-center justify-between px-3 py-1.5 text-[11px] font-semibold ${lm ? "text-violet-600 hover:bg-slate-50" : "text-violet-400 hover:bg-slate-800/40"}`}>
                        <span>{showBufferMath ? "Hide" : "Show"} how this buffer was calculated</span>
                        <svg className={`h-3 w-3 transition-transform ${showBufferMath ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                      </button>
                      {showBufferMath && (() => {
                        const sigma = mlData.residualStd;
                        const lt    = draftParams.leadTime;
                        const rootL = lt != null ? Math.sqrt(lt) : null;
                        const rowC  = lm ? "text-slate-500" : "text-slate-400";
                        const valC  = lm ? "text-slate-700" : "text-slate-200";
                        // The buffer is protection × the demand SWING over the whole lead-time wait.
                        // We use the larger of: the day-swing stretched by √lead (the floor), or the
                        // swing MEASURED over real lead-time windows (bigger when demand clumps). Back
                        // the swing actually used out of the held buffer so the rows stay consistent.
                        const formulaSpread = (sigma != null && rootL != null) ? sigma * rootL : null;
                        const usedSpread = (zScore && mlData.safetyStock != null) ? mlData.safetyStock / zScore : formulaSpread;
                        const widened = (usedSpread != null && formulaSpread != null) && usedSpread > formulaSpread * 1.08;
                        return (
                          <div className={`px-3 py-2.5 space-y-1.5 text-[11px] ${lm ? "bg-slate-50" : "bg-slate-900/40"}`}>
                            <div className={`flex justify-between ${rowC}`}><span>Typical demand</span><span className={`font-mono ${valC}`}>~{mlData.avgDailyDemand}/day{mlData.avgDailyDemand < 1 ? ` (~${Math.round(mlData.avgDailyDemand * 30)}/mo)` : ""}</span></div>
                            <div className={`flex justify-between ${rowC}`}><span>Day-to-day swing (σ)</span><span className={`font-mono ${valC}`}>±{sigma}/day</span></div>
                            <div className={`flex justify-between ${rowC}`}><span>Swing over the {lt}-day wait</span><span className={`font-mono ${valC}`}>±{usedSpread?.toFixed(1)} {widened ? "(measured)" : `≈ σ×√${lt}`}</span></div>
                            <div className={`flex justify-between ${rowC}`}><span>Protection factor</span><span className={`font-mono ${valC}`}>×{zScore?.toFixed(2)} ({p.servicePct}%)</span></div>
                            <div className={`flex justify-between font-semibold border-t pt-1.5 ${lm ? "border-slate-200 text-slate-700" : "border-slate-700/50 text-slate-200"}`}><span>Buffer held</span><span className="font-mono">{mlData.safetyStock} units</span></div>
                            <p className={`pt-1 leading-relaxed ${lm ? "text-slate-400" : "text-slate-500"}`}>
                              We hold <span className="font-semibold">{mlData.safetyStock}</span> spare units so demand swings during the {lt}-day wait for a reorder won't run you out about {p.servicePct}% of the time. That swing is measured from how far past forecasts actually missed over real {lt}-day stretches{widened ? " — and because this item's sales come in clumps, it's wider than a steady seller's, so it earns extra buffer" : ", and here it lines up with the usual day-to-day swing stretched over the wait"}. A noisier or clumpier item, or a longer lead time, raises this; a steady one lowers it.
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
              <Tip text="How many days of stock the order should cover, starting from when it arrives. Bigger = larger orders, longer runway between reorders.">Coverage Window (days)</Tip>
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
              const deliveryOffset = Math.max(mlData.daysUntilReorder ?? 0, 0) + draftParams.leadTime;
              const deliveryDate = new Date(todayMs());
              deliveryDate.setUTCDate(deliveryDate.getUTCDate() + deliveryOffset);
              const coverageEnd = new Date(deliveryDate.getTime());
              coverageEnd.setUTCDate(coverageEnd.getUTCDate() + draftParams.coverage);
              const fmt = d => d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
              // Gap = days you'll be out of stock before this order can land (overdue only).
              const gapDays = Math.max(0, deliveryOffset - mlData.daysUntilStockout);
              return (
                <div className={`rounded-xl border overflow-hidden ${lm ? "border-slate-200" : "border-slate-700/50"}`}>
                  <div className={`px-3 py-2 flex items-center justify-between ${lm ? "bg-white" : "bg-transparent"}`}>
                    <span className={`text-[11px] ${lm ? "text-slate-400" : "text-slate-500"}`}>Covered from</span>
                    <span className={`text-[11px] font-semibold tabular-nums ${lm ? "text-slate-700" : "text-slate-300"}`}>{fmt(deliveryDate)}</span>
                    <span className={`text-[11px] ${lm ? "text-slate-300" : "text-slate-600"}`}>→</span>
                    <span className={`text-[11px] font-semibold tabular-nums ${lm ? "text-slate-700" : "text-slate-300"}`}>{fmt(coverageEnd)}</span>
                  </div>
                  {gapDays > 0 && (
                    <div className={`px-3 py-1.5 border-t text-[11px] leading-relaxed ${lm ? "border-rose-100 bg-rose-50 text-rose-600" : "border-rose-900/30 bg-rose-950/20 text-rose-300"}`}>
                      ⚠ Even ordering today, stock won't arrive until {fmt(deliveryDate)} — expect ~{gapDays} day{gapDays !== 1 ? "s" : ""} out of stock before then.
                    </div>
                  )}
                  <div className={`px-3 py-1.5 border-t text-[11px] leading-relaxed ${lm ? "border-slate-100 bg-slate-50 text-slate-400" : "border-slate-700/30 bg-slate-800/20 text-slate-600"}`}>
                    Estimated from current forecast. Actual window may vary — demand above forecast shortens it, below extends it. Delivery delays shift the start date.
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
                  className={`flex-1 py-2 rounded-lg text-sm font-bold border transition-all ${draftParams.months === m ? moBtnActive : moBtnInactive}`}>
                  {m}mo
                </button>
              ))}
            </div>
          </div>
        </div>

        {loading && (
          <div className={`text-[11px] font-mono p-2 rounded-lg flex items-center gap-2 ${lm ? "text-amber-700 bg-amber-50 border border-amber-200" : "text-amber-400 bg-amber-950/20 border border-amber-900/30"}`}>
            <div className="h-2 w-2 border-2 border-amber-400 border-t-transparent rounded-full animate-spin" />
            Recalculating…
          </div>
        )}

        {hasOpenPO && (
          <div className={`${poCard} border rounded-xl p-3 space-y-1.5`}>
            <div className="flex items-center gap-1.5">
              <div className="h-1.5 w-1.5 rounded-full bg-violet-500" />
              <span className={`text-[11px] font-bold uppercase tracking-widest ${poHead}`}>Order In Transit</span>
            </div>
            <div className={`text-xs ${poText} space-y-0.5 font-mono`}>
              <div>{openPO.qty.toLocaleString()} units</div>
              <div>Est. delivery <span className={textMain}>{isoToDisplay(openPO.delivery)}</span></div>
              {openPO.supplier && <div className={textMuted}>{openPO.supplier}</div>}
            </div>
            <button onClick={() => setShowPoModal(true)} className={`text-[11px] ${lm ? "text-violet-600 hover:text-violet-800" : "text-violet-400 hover:text-violet-300"} transition-colors`}>Edit / Mark received →</button>
          </div>
        )}

        <div className={`border-t ${divider} pt-4 space-y-2`}>
          <div className={`${infoHead} mb-1`}>Model Info</div>
          {[
            ["Yearly pattern",  mlData.activeYearly ? "Detected" : "—",       mlData.activeYearly ? (lm ? "text-emerald-700" : "text-emerald-400") : ""],
            ["Weekly pattern",  mlData.activeWeekly ? "Detected" : "—",       mlData.activeWeekly ? (lm ? "text-emerald-700" : "text-emerald-400") : ""],
            ...(mlData.priceModeled ? [["Price tracking",  mlData.priceWellSampled ? "Active" : "Limited",
              mlData.priceWellSampled ? (lm ? "text-emerald-700" : "text-emerald-400") : (lm ? "text-amber-700" : "text-amber-400")]] : []),
            ["History",         `${mlData.daysOfHistory}d`,                   infoVal],
            ["Total sold",      mlData.totalUnitsSold?.toLocaleString(),       infoVal],
          ].map(([k, v, c]) => (
            <div key={k} className={`flex justify-between ${infoLabel}`}>
              <span>{k}</span><span className={c || infoVal}>{v}</span>
            </div>
          ))}
          {mlData.stockoutRowsDropped > 0 && (
            <div className={`flex justify-between text-[11px] ${lm ? "text-amber-600" : "text-amber-600"}`}>
              <span>Zero-stock days excluded</span><span>{mlData.stockoutRowsDropped}</span>
            </div>
          )}
          {mlData.priceModeled && (mlData.currentPrice ?? mlData.lastPrice) != null && (
            <div className={`flex justify-between ${infoLabel}`}><span>Current price</span><span className={infoVal}>${(mlData.currentPrice ?? mlData.lastPrice).toFixed(2)}</span></div>
          )}
          {mlData.events?.length > 0 && (
            <div className={`flex justify-between ${infoLabel}`}><span>Events logged</span><span className={lm ? "text-violet-700" : "text-violet-400"}>{mlData.events.length}</span></div>
          )}
        </div>
      
        </div>
      </div>

      {/* Main content */}
      <div className={`flex-1 overflow-y-auto p-7 space-y-6 ${mainBg}`}>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className={`text-xl font-bold ${textMain} tracking-tight`}>{mlData.skuName}</h2>
            <p className={`text-xs ${textMuted} font-mono mt-1`}>{mlData.skuId} · {mlData.filename || "—"}</p>
          </div>
          <div className="flex items-center gap-2 flex-wrap justify-end">
            <button onClick={onExport} disabled={!!exporting} title="Download this SKU's forecast, daily projection, and monthly breakdown as Excel"
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 disabled:opacity-60 text-white transition-all">
              <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" /></svg>
              {exporting ? "Preparing…" : "Export"}
            </button>
            {mlData.forecastDetails && (
              <button onClick={() => setShowForecastDetails(true)} title="See how this forecast was produced"
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all ${lm ? "bg-white border-slate-300 text-violet-600 hover:border-violet-400 hover:text-violet-700" : "bg-slate-900 border-slate-700 text-violet-400 hover:text-violet-300 hover:border-violet-700"}`}>
                <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                Forecast details
              </button>
            )}
            <button onClick={() => setShowEvents(true)}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all ${
                mlData.events?.length > 0
                  ? (lm ? "bg-violet-50 border-violet-300 text-violet-700 hover:border-violet-400" : "bg-violet-950/30 border-violet-800/50 text-violet-300 hover:border-violet-600")
                  : (lm ? "bg-white border-slate-300 text-slate-600 hover:border-slate-400 hover:text-slate-800" : "bg-slate-900 border-slate-700 text-slate-400 hover:text-slate-200 hover:border-slate-600")
              }`}>
              <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
              </svg>
              Events{mlData.events?.length > 0 ? ` (${mlData.events.length})` : ""}
            </button>
            {!hasOpenPO && (
              <button onClick={() => setShowPoModal(true)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all ${lm ? "bg-white border-slate-300 text-slate-600 hover:border-slate-400 hover:text-slate-800" : "bg-slate-900 border-slate-700 text-slate-400 hover:text-slate-200 hover:border-slate-600"}`}>
                <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" />
                </svg>
                Mark Order In Transit
              </button>
            )}
            <div className={`h-2 w-2 rounded-full ${statusCfg.dot}`} />
            <span className={`text-xs ${textMuted}`}>{statusCfg.label}</span>
          </div>
        </div>

        {mlData.hasPrice && !mlData.priceModeled && (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${lm ? "bg-amber-50 border-amber-300" : "bg-amber-950/20 border-amber-900/40"}`}>
            <span className="text-base mt-0.5 shrink-0">⚠️</span>
            <div>
              <div className={`text-sm font-bold mb-1 ${lm ? "text-amber-700" : "text-amber-300"}`}>PRICE SENSITIVITY UNAVAILABLE</div>
              <p className={`text-xs leading-relaxed ${lm ? "text-amber-700" : "text-amber-300/70"}`}>
                This product is forecast by <span className="font-semibold">{mlData.winningModel}</span> because it's new/sparse with no established history to learn a price response from.
                Price effects can't be estimated reliably here, so price history, ranges, the safe-extrapolation zone, and the effect of price changes are hidden to avoid implying a precision that isn't there.
                Price still feeds margin in the scorecard — it just doesn't move this forecast. Once the product builds up history (or is grouped with priced siblings), it can route to a price-aware model.
              </p>
            </div>
          </div>
        )}

        {mlData.priceModeled && mlData.priceChangeWarning && (() => {
          const isInfo = mlData.priceChangeLevel === "info";
          return (
            <div className={`border rounded-2xl p-4 flex items-start gap-3 ${
              isInfo
                ? lm ? "bg-violet-50 border-violet-300"    : "bg-violet-950/20 border-violet-900/30"
                : lm ? "bg-orange-50 border-orange-300" : "bg-orange-950/20 border-orange-900/30"
            }`}>
              <span className="text-base mt-0.5 shrink-0">{isInfo ? "ℹ️" : "⚠️"}</span>
              <div>
                <div className={`text-sm font-bold mb-1 ${
                  isInfo
                    ? lm ? "text-violet-700"    : "text-violet-300"
                    : lm ? "text-orange-700" : "text-orange-300"
                }`}>{isInfo ? "PRICE NOT DIRECTLY OBSERVED" : "PRICE OUTSIDE HISTORICAL RANGE"}</div>
                <p className={`text-xs leading-relaxed ${
                  isInfo
                    ? lm ? "text-violet-600"    : "text-violet-300/70"
                    : lm ? "text-orange-600" : "text-orange-300/70"
                }`}>{mlData.priceChangeMessage}</p>
              </div>
            </div>
          );
        })()}

        {hasOpenPO && (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${lm ? "bg-violet-50 border-violet-300" : "bg-violet-950/20 border-violet-900/30"}`}>
            <span className="text-base mt-0.5 shrink-0">📦</span>
            <div className="flex-1">
              <div className={`text-sm font-bold mb-1 ${lm ? "text-violet-700" : "text-violet-300"}`}>ORDER IN TRANSIT — REORDER ALERT SUPPRESSED</div>
              <p className={`text-xs leading-relaxed ${lm ? "text-violet-600" : "text-violet-300/70"}`}>
                <span className={`font-semibold ${textMain}`}>{openPO.qty.toLocaleString()} units</span>
                {openPO.supplier ? ` ordered from ${openPO.supplier}` : " on order"}.
                Expected delivery: <span className={`font-semibold ${textMain}`}>{isoToDisplay(openPO.delivery)}</span>.
              </p>
            </div>
            <button onClick={() => setShowPoModal(true)} className={`text-[11px] border rounded-lg px-2 py-1 transition-colors shrink-0 ${lm ? "text-violet-600 border-violet-300 hover:bg-violet-100" : "text-violet-400 border-violet-800/40 hover:text-violet-300"}`}>Edit</button>
          </div>
        )}

        {mlData.inactive && (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${lm ? "bg-slate-100 border-slate-300" : "bg-slate-800/40 border-slate-600/50"}`}>
            <span className="text-base mt-0.5 shrink-0">💤</span>
            <div className="flex-1">
              <div className={`text-sm font-bold mb-1 ${lm ? "text-slate-700" : "text-slate-200"}`}>INACTIVE — likely discontinued</div>
              <p className={`text-xs leading-relaxed ${lm ? "text-slate-600" : "text-slate-400"}`}>{mlData.inactiveMessage}</p>
            </div>
          </div>
        )}

        {mlData.tooNew && (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${lm ? "bg-violet-50 border-violet-200" : "bg-violet-950/20 border-violet-900/40"}`}>
            <span className="text-base mt-0.5 shrink-0">🌱</span>
            <div className="flex-1">
              <div className={`text-sm font-bold mb-1 ${lm ? "text-violet-700" : "text-violet-300"}`}>ESTABLISHING BASELINE — {mlData.ownDays} of {mlData.baselineDays} days</div>
              <p className={`text-xs leading-relaxed ${lm ? "text-violet-700/80" : "text-violet-300/70"}`}>{mlData.tooNewMessage}</p>
            </div>
          </div>
        )}

        {mlData.young && (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${lm ? "bg-amber-50 border-amber-200" : "bg-amber-950/15 border-amber-900/30"}`}>
            <span className="text-base mt-0.5 shrink-0">🌿</span>
            <div className="flex-1">
              <div className={`text-sm font-bold mb-1 ${lm ? "text-amber-700" : "text-amber-300"}`}>YOUNG PRODUCT — {mlData.ownDays} of {mlData.youngThreshold} days · forecast is provisional</div>
              <p className={`text-xs leading-relaxed ${lm ? "text-amber-700/80" : "text-amber-300/70"}`}>
                {mlData.youngMessage}
                {showReorder ? ` Heads-up: at this early pace, stock looks like it'll run low around ${formatDate(mlData.daysUntilStockout)} — consider ordering ~${mlData.orderQty?.toLocaleString()} when you're ready. It's not a hard deadline yet; the estimate firms up as more sales come in.` : ""}
              </p>
            </div>
          </div>
        )}

        {showReorder && !mlData.young && (urgency === "critical" ? (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${lm ? "bg-red-50 border-red-300" : "bg-red-950/25 border-red-900/40"}`}>
            <span className="text-base mt-0.5 shrink-0">🚨</span>
            <div className="flex-1">
              <div className={`text-sm font-bold mb-1 ${lm ? "text-red-700" : "text-red-300"}`}>{mlData.daysUntilReorder === 0 ? "REORDER DUE TODAY" : `REORDER OVERDUE — ${Math.abs(mlData.daysUntilReorder)} day${Math.abs(mlData.daysUntilReorder) !== 1 ? "s" : ""} ago`}</div>
              <p className={`text-xs leading-relaxed ${lm ? "text-red-600" : "text-red-300/70"}`}>
                {mlData.daysUntilReorder === 0 ? "Today is your reorder deadline." : "Your reorder deadline has passed."} Stock projected to run out <span className={`font-semibold ${textMain}`}>{formatDate(mlData.daysUntilStockout)}</span>. Place your order immediately.
              </p>
            </div>
            <button onClick={() => setShowPoModal(true)} className={`text-[11px] border rounded-lg px-2 py-1 transition-colors shrink-0 ${lm ? "text-red-600 border-red-300 hover:bg-red-100" : "text-red-300 border-red-800/50 hover:text-white"}`}>Mark Ordered</button>
          </div>
        ) : (
          <div className={`border rounded-2xl p-4 flex items-start gap-3 ${lm ? "bg-amber-50 border-amber-300" : "bg-amber-950/15 border-amber-900/30"}`}>
            <span className="text-base mt-0.5 shrink-0">⚠️</span>
            <div className="flex-1">
              <div className={`text-sm font-bold mb-1 ${lm ? "text-amber-700" : "text-amber-300"}`}>UPCOMING REORDER — {mlData.daysUntilReorder} days</div>
              <p className={`text-xs leading-relaxed ${lm ? "text-amber-600" : "text-amber-300/70"}`}>
                Place order by <span className={`font-semibold ${textMain}`}>{formatDate(mlData.daysUntilReorder)}</span> to cover your {params.leadTime}-day lead time.
                Stockout projected <span className={`font-semibold ${textMain}`}>{formatDate(mlData.daysUntilStockout)}</span>.
              </p>
            </div>
            <button onClick={() => setShowPoModal(true)} className={`text-[11px] border rounded-lg px-2 py-1 transition-colors shrink-0 ${lm ? "text-amber-700 border-amber-300 hover:bg-amber-100" : "text-amber-300 border-amber-800/50 hover:text-white"}`}>Mark Ordered</button>
          </div>
        ))}

        <SkuSignalStrip key={mlData.skuId} mlData={mlData} statusCfg={statusCfg} apiKey={apiKey} lm={lm} />

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <MetricCard lm={lm} label="In Stock" value={params.stock.toLocaleString()}
            sub={mlData.stockDataAvailable === false ? "assumed — no stock data" : hasOpenPO ? `+ ${openPO.qty.toLocaleString()} on order` : "units on hand"}
            valueColor={mlData.stockDataAvailable === false ? (lm ? "text-amber-600" : "text-amber-400") : undefined} />
          <MetricCard lm={lm}
            label={<Tip text="When your current stock is predicted to run out if no new order arrives.">Stockout Date</Tip>}
            value={mlData.daysUntilStockout != null ? formatDate(mlData.daysUntilStockout).split(",")[0] : "—"}
            sub={mlData.daysUntilStockout != null ? `${mlData.daysUntilStockout} days away` : "Sufficient stock"}
            valueColor="text-rose-500" />
          <MetricCard lm={lm}
            label={<Tip text="The last safe date to place an order so it arrives before you run out of stock.">Order By</Tip>}
            value={hasOpenPO ? "On Order" : mlData.daysUntilReorder > 0 ? formatDate(mlData.daysUntilReorder).split(",")[0] : mlData.daysUntilReorder === 0 ? "Today" : mlData.daysUntilReorder != null ? `${Math.abs(mlData.daysUntilReorder)}d` : "—"}
            sub={hasOpenPO ? `Est. ${isoToDisplay(openPO.delivery)}` : mlData.daysUntilReorder > 0 ? `${mlData.daysUntilReorder} days from now` : mlData.daysUntilReorder === 0 ? "Order today" : mlData.daysUntilReorder != null ? "overdue" : "—"}
            valueColor={hasOpenPO ? (lm ? "text-violet-600" : "text-violet-300") : mlData.daysUntilReorder <= 0 && mlData.daysUntilReorder != null ? "text-red-500" : (lm ? "text-amber-600" : "text-amber-400")} />
          <MetricCard lm={lm}
            label={<Tip text="Recommended order quantity: covers your coverage window demand plus a safety buffer for forecast uncertainty.">Units to Order</Tip>}
            value={mlData.orderQty.toLocaleString()} sub="recommended qty"
            valueColor={lm ? "text-violet-700" : "text-violet-300"} accent />
        </div>

        {showReorder && (
          <div className={`flex items-start gap-2 text-[11px] leading-relaxed ${lm ? "text-slate-500" : "text-slate-500"}`}>
            <span className="shrink-0">ℹ️</span>
            <span>The stockout date, order-by date, and order quantity assume you place the order <span className="font-semibold">today</span>. They don't reduce the demand forecast for any stockout — that stays true demand (you simply lose those sales). Ordering later pushes delivery out and lengthens any out-of-stock gap.</span>
          </div>
        )}

        {mlData.orderGuardrail?.active && (() => {
          const g = mlData.orderGuardrail;
          const tone = g.capExceeded
            ? (lm ? "bg-amber-50 border-amber-200 text-amber-800" : "bg-amber-950/20 border-amber-900/40 text-amber-300")
            : (lm ? "bg-slate-50 border-slate-200 text-slate-600" : "bg-slate-900/40 border-slate-700 text-slate-300");
          return (
            <div className={`flex items-start gap-2.5 rounded-xl border px-4 py-3 text-[12px] ${tone}`}>
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
              upcomingPromos={mlData.upcomingPromos} lm={lm} />
          </div>
          <div className={`${cardBg} border rounded-2xl p-6`}>
            <div className="flex items-center gap-2 mb-2">
              <span className="text-lg">🌱</span>
              <span className={`text-sm font-bold ${textMain}`}>Forecast paused — establishing baseline</span>
            </div>
            <p className={`text-xs leading-relaxed mb-5 ${textMuted}`}>
              We start forecasting once this product has about a week of its own sales ({mlData.ownDays}/{mlData.baselineDays} days so far). Until then, here's what we can track straight from inventory.
            </p>
            {/* For a slow seller, per-day whole units read as "0". Lead with how it
                actually sells — a monthly rate and the fact that zero-sale days are
                normal — before any daily number appears. */}
            {mlData.demandStory && (
              <div className={`mb-4 rounded-xl border px-4 py-3 text-xs leading-relaxed ${lm ? "bg-sky-50 border-sky-200 text-slate-700" : "bg-sky-950/15 border-sky-900/40 text-slate-300"}`}>
                <span className="font-semibold">How this product sells: </span>{mlData.demandStory}
                <div className={`mt-1 ${textMuted}`}>
                  Orders aren&apos;t sized from one day — they add this rate up across your lead time and coverage window.
                </div>
              </div>
            )}
            {mlData.rateCheck && (
              <div className={`mb-4 rounded-xl border px-4 py-3 text-xs leading-relaxed ${lm ? "bg-amber-50 border-amber-200 text-amber-800" : "bg-amber-950/15 border-amber-900/30 text-amber-300"}`}>
                <span className="font-semibold">Forecast is running below recent sales. </span>{mlData.rateCheck.message}
              </div>
            )}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div><div className={`text-[11px] uppercase tracking-widest font-bold mb-1 ${textMuted}`}>In stock</div><span className={`text-2xl font-bold tabular-nums ${textMain}`}>{params.stock.toLocaleString()}</span></div>
              <div><div className={`text-[11px] uppercase tracking-widest font-bold mb-1 ${textMuted}`}>Sold so far</div><span className={`text-2xl font-bold tabular-nums ${textMain}`}>{mlData.currentMonth?.unitsSoFar?.toLocaleString() ?? "—"}</span></div>
              <div><div className={`text-[11px] uppercase tracking-widest font-bold mb-1 ${textMuted}`}>Observed pace</div><span className={`text-2xl font-bold tabular-nums ${textMain}`}>{mlData.observedDailyRate ?? "—"}</span><span className={`text-xs ${textMuted}`}>/day</span>
                {mlData.observedDailyRate != null && mlData.observedDailyRate < 1 && (
                  <div className={`text-[11px] ${textMuted}`}>≈ {Math.round(mlData.observedDailyRate * 30)}/month</div>
                )}</div>
              <div><div className={`text-[11px] uppercase tracking-widest font-bold mb-1 ${textMuted}`}>Stock covers</div><span className={`text-2xl font-bold tabular-nums ${textMain}`}>{mlData.observedRunwayDays != null ? `~${mlData.observedRunwayDays}` : "—"}</span><span className={`text-xs ${textMuted}`}>{mlData.observedRunwayDays != null ? " days" : ""}</span></div>
            </div>
            {mlData.observedRunwayDays != null && (() => {
              const soon = mlData.observedRunwayDays <= (params.leadTime || 14);
              return (
                <div className={`mt-4 rounded-xl border px-4 py-3 text-xs leading-relaxed ${soon
                  ? (lm ? "bg-amber-50 border-amber-200 text-amber-800" : "bg-amber-950/15 border-amber-900/30 text-amber-300")
                  : (lm ? "bg-slate-50 border-slate-200 text-slate-600" : "bg-slate-900/40 border-slate-700 text-slate-300")}`}>
                  {soon ? "⚠️ " : "ℹ️ "}At its current pace (~{mlData.observedDailyRate}/day), this runs out around{" "}
                  <span className="font-semibold">{mlData.observedStockoutTs ? new Date(mlData.observedStockoutTs).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : `~${mlData.observedRunwayDays} days`}</span>
                  {" "}— about {mlData.observedRunwayDays} days of stock left.
                  {soon ? ` That's inside your ${params.leadTime}-day lead time, so it's worth ordering now even though the forecast is still warming up.` : ""}
                </div>
              );
            })()}
            <p className={`text-[11px] mt-4 ${textMuted}`}>Pace and runway are raw observations from the days seen so far — not a forecast.</p>
          </div>
          </>
        ) : (
        <>
        <ArrivalPromptBanner arrival={arrival} lm={lm}
          onConfirm={onConfirmArrival} onIgnore={onIgnoreArrival} />
        {/* A seasonal scale on a route that has no seasonal curve of its own. */}
        {mlData.seasonalityApplied?.text && (
          <div className={`mb-4 rounded-xl border px-4 py-3 text-xs leading-relaxed ${lm ? "bg-teal-50 border-teal-200 text-teal-900" : "bg-teal-950/15 border-teal-900/30 text-teal-300"}`}>
            <span className="font-semibold">Seasonal pattern. </span>
            {mlData.seasonalityApplied.text}
            {mlData.seasonalityApplied.applied && (
              <span className={`block mt-1 ${lm ? "text-teal-700" : "text-teal-400/80"}`}>
                In this forecast window that ranges from {mlData.seasonalityApplied.troughMultiplier}× to
                {" "}{mlData.seasonalityApplied.peakMultiplier}× the flat rate.
              </span>
            )}
          </div>
        )}
        {/* Price and promo effects on the routes that can't model them internally.
            Silent multipliers moving an order would be worse than none. */}
        {mlData.uplift?.text && (
          <div className={`mb-4 rounded-xl border px-4 py-3 text-xs leading-relaxed ${lm ? "bg-violet-50 border-violet-200 text-violet-900" : "bg-violet-950/15 border-violet-900/30 text-violet-300"}`}>
            <span className="font-semibold">Price &amp; promotions. </span>
            {mlData.uplift.text}
            {mlData.uplift.applied && (
              <span className={`block mt-1 ${lm ? "text-violet-700" : "text-violet-400/80"}`}>
                Applied to {mlData.uplift.daysLifted} upcoming day{mlData.uplift.daysLifted === 1 ? "" : "s"} in this forecast.
              </span>
            )}
          </div>
        )}
        {/* An adjusted forecast must never be silent — if the model was fitted on
            something other than raw sales, say so and say why. */}
        {mlData.censoring && (
          <div className={`mb-4 rounded-xl border px-4 py-3 text-xs leading-relaxed ${lm ? "bg-sky-50 border-sky-200 text-sky-900" : "bg-sky-950/15 border-sky-900/30 text-sky-300"}`}>
            <span className="font-semibold">Adjusted for days you ran out. </span>
            {mlData.censoring.text}
            {mlData.censoring.addedUnits > 0 && (
              <span className={`block mt-1 ${lm ? "text-sky-700" : "text-sky-400/80"}`}>
            The forecast is fitted to an estimated {mlData.censoring.fittedUnits} units rather than the
            {" "}{mlData.censoring.observedUnits} you sold, because selling out caps sales without
            capping demand. Left uncorrected this drifts lower every cycle.
              </span>
            )}
          </div>
            )}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          <div className={`lg:col-span-2 ${cardBg} border rounded-2xl p-5 h-[360px]`}>
            <ForecastChart historyPoints={mlData.chartDataHistory}
              futurePoints={mlData.inactive ? [] : mlData.chartDataFuture}
              rangePoints={mlData.inactive ? [] : mlData.chartDataRange}
              stockoutTime={mlData.inactive ? null : mlData.stockoutTimestamp}
              reorderTime={mlData.inactive ? null : mlData.reorderTimestamp} intervalWidth={mlData.intervalWidth}
              upcomingPromos={mlData.upcomingPromos} lm={lm} />
          </div>
          {/* Current Month — standalone card aligned with chart */}
          <div className={`${cardBg} border rounded-2xl p-6 flex flex-col justify-between h-[360px]`}>
            {/* Top: header + big number */}
            <div>
              <div className={`text-[11px] uppercase tracking-widest font-bold mb-4 ${textMuted}`}>Current Month</div>
              <div className="flex items-baseline gap-3 mb-1">
                <span className={`text-6xl font-bold ${textMain} tabular-nums leading-none`}>{projTotal.toLocaleString()}</span>
              </div>
              <div className="flex items-center gap-2 mt-2">
                <span className={`text-sm font-mono font-bold ${pctChange >= 0 ? "text-emerald-500" : "text-rose-500"}`}>
                  {pctChange >= 0 ? "▲" : "▼"} {Math.abs(pctChange).toFixed(1)}%
                </span>
                <span className={`text-sm ${textMuted}`}>vs last month</span>
              </div>
            </div>
            {/* Middle: sold / remaining */}
            <div className={`grid grid-cols-2 gap-4 py-5 border-y ${divider}`}>
              <div>
                <div className={`text-[11px] uppercase tracking-widest font-bold mb-1 ${textMuted}`}>Sold so far</div>
                <span className={`text-3xl font-bold tabular-nums ${textMain}`}>{mlData.currentMonth.unitsSoFar.toLocaleString()}</span>
              </div>
              <div>
                <div className={`text-[11px] uppercase tracking-widest font-bold mb-1 ${textMuted}`}>Remaining</div>
                <span className="text-3xl font-bold tabular-nums text-violet-500">{mlData.currentMonth.forecastRemaining.toLocaleString()}</span>
              </div>
            </div>
            {/* Bottom: last month */}
            <div className="flex items-baseline gap-2">
              <span className={`text-2xl font-bold tabular-nums ${textMain}`}>{mlData.currentMonth.lastMonthTotal.toLocaleString()}</span>
              <span className={`text-sm ${textMuted}`}>last month</span>
            </div>
          </div>
        </div>

        {/* Order Math — two columns on a wide panel, stacked on a narrow one */}
        <OrderMathCard data={mlData} leadTime={params.leadTime} coverageDays={params.coverage} unitsOnOrder={onOrderQty} lm={lm} skuSt={skuSt} demandVolatilityColor={mlData?.demandVolatilityColor} />

        <div>
          <div className={`text-[11px] uppercase tracking-widest font-bold mb-3 ${textMuted}`}>Sales Forecast by Month</div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {mlData.monthCards?.map((card, i) => <MonthForecastCard key={i} card={card} lm={lm} />)}
          </div>
        </div>
        </>
        )}

        <div>
          <button onClick={() => setShowHistory(!showHistory)}
            className={`flex items-center gap-2 text-xs transition-colors ${lm ? "text-violet-600 hover:text-violet-800" : "text-violet-400 hover:text-violet-300"}`}>
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
        onSave={handleSaveEvents} onClose={() => setShowEvents(false)} lm={lm} />}
    </div>
  );
}
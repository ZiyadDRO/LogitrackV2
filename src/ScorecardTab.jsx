import { posInt, buildScorecardBody, statusInfo } from './lib/helpers';
import React, { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { ScatterChart, Scatter, XAxis, YAxis, ZAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell, ReferenceArea } from "recharts";

// ════════════════════════════════════════════════════════════════════════════
//  SKU SCORECARD TAB  (named-status model)
//  Pairs with POST /api/scorecard. Add as a tab next to Fleet / Suppliers.
//
//  The headline is a STATUS, not a number — each maps to an action, and routine
//  reorder urgency is kept separate from genuine health problems:
//    Healthy · Reorder due · Overstocked · Stockout risk · Dead stock
//  A profit grade (A/B/C, margin after cost + fees) sits alongside it.
//
//  Wiring in App.jsx:
//    <ScorecardTab api={API} skuList={skuList} skuParams={skuParams}
//       setSkuParams={setSkuParams} openPOs={openPOs} lm={lightMode} />
// ════════════════════════════════════════════════════════════════════════════

// ─── TOOLTIP (self-contained copy of App's Tip) ──────────────────────────────
function Tip({ text, children }) {
  const [show, setShow] = useState(false);
  const [coords, setCoords] = useState({ x: 0, y: 0 });
  const ref = useRef(null);
  const handleEnter = () => {
    if (ref.current) {
      const r = ref.current.getBoundingClientRect();
      setCoords({ x: r.left + r.width / 2, y: r.top });
    }
    setShow(true);
  };
  return (
    <span ref={ref} className="inline-flex items-center gap-0.5 cursor-help"
      onMouseEnter={handleEnter} onMouseLeave={() => setShow(false)}>
      {children}
      <span className="text-[10px] opacity-50 select-none">ⓘ</span>
      {show && (
        <span style={{ position: "fixed", left: coords.x, top: coords.y - 8, transform: "translate(-50%, -100%)", zIndex: 9999 }}
          className="px-2.5 py-1.5 text-[11px] leading-relaxed bg-slate-900 text-slate-100 border border-slate-700 rounded-lg pointer-events-none shadow-xl w-max max-w-[280px] text-center font-normal whitespace-normal tracking-normal">
          {text}
          <span className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-slate-700" />
        </span>
      )}
    </span>
  );
}

// ─── GLOSSARY (plain-language tooltips) ──────────────────────────────────────
const HELP = {
  // Built from the shared table so this glossary can't drift from the chips, the row
  // badges or the fleet's legend — which is how "Reorder due" ended up meaning one
  // thing here and another there.
  status:   "The item's current inventory situation, and what it implies you should do. Hover any status chip or badge for its exact definition — the thresholds are a fraction of each product's own lead time, not fixed days.",
  cover:    "How many days your current stock will last at the forecasted rate of sale — not a flat historical average.",
  returnTier: "A grade for how profitable each sale is, based on margin after unit cost and fees. A is a strong margin (40%+), B is moderate (20%+), C is slim (under 20%), F is a loss (0% or negative — you make nothing or lose money per sale). A profitable item keeps a strong grade even if it sells slowly.",
  margin:   "The percentage of the selling price kept as profit after unit cost and any fees.",
  sellThrough: "Of everything available in the trailing window — what sold plus what is still on the shelf — the share that sold. Useful for ranking, but not a health verdict on its own: it is arithmetically days-of-cover in other units, so read Stock coverage for whether the position is right.",
  carrying: "The value of cash currently tied up in this item's on-hand stock (units on hand × unit cost).",
  recommendation: "The suggested next step, weighing both how the stock is positioned and how profitable the item is.",
  promo:    "This item is currently discounted, so it's selling faster than usual. 'On sale' reflects its status during the promotion; 'Normal' reflects typical demand, and is the better basis for planning once the promotion ends.",
  cost:     "What you pay per unit. Required to calculate margin, the profit grade, and cash tied up.",
  fees:     "Any per-unit costs not included in unit cost — for example payment processing, fulfillment, or marketplace fees. Leave at 0 if there are none.",
};

// Status styling + display order (most urgent first).
const STATUS_STYLE = {
  "Stockout risk": { dot: "bg-rose-500",   lm: "bg-rose-100 text-rose-800 border-rose-300",          dk: "bg-rose-950 text-rose-300 border-rose-800",          hex: "#f43f5e" },
  "Dead stock":    { dot: "bg-orange-500", lm: "bg-orange-100 text-orange-800 border-orange-300",    dk: "bg-orange-950 text-orange-300 border-orange-800",    hex: "#f97316" },
  "Overstocked":   { dot: "bg-amber-500",  lm: "bg-amber-100 text-amber-800 border-amber-300",       dk: "bg-amber-950 text-amber-300 border-amber-800",       hex: "#f59e0b" },
  "Reorder due":   { dot: "bg-sky-500",    lm: "bg-sky-100 text-sky-800 border-sky-300",             dk: "bg-sky-950 text-sky-300 border-sky-800",             hex: "#0ea5e9" },
  "Healthy":       { dot: "bg-emerald-500",lm: "bg-emerald-100 text-emerald-800 border-emerald-300", dk: "bg-emerald-950 text-emerald-300 border-emerald-800", hex: "#10b981" },
};
const STATUS_ORDER = ["Stockout risk", "Dead stock", "Overstocked", "Reorder due", "Healthy"];

const TIER_STYLE = {
  A:   { lm: "bg-emerald-100 text-emerald-800 border-emerald-300", dk: "bg-emerald-950 text-emerald-300 border-emerald-800" },
  B:   { lm: "bg-amber-100 text-amber-800 border-amber-300",       dk: "bg-amber-950 text-amber-300 border-amber-800" },
  C:   { lm: "bg-rose-100 text-rose-800 border-rose-300",          dk: "bg-rose-950 text-rose-300 border-rose-800" },
  F:   { lm: "bg-red-600 text-white border-red-700",               dk: "bg-red-700 text-white border-red-600" },
  "—": { lm: "bg-slate-100 text-slate-500 border-slate-300",       dk: "bg-slate-800 text-slate-500 border-slate-700" },
};

const fmtCover = (d) => (d == null ? "365+ days" : `${d} days`);
const fmtMoney = (v) => (v == null ? "—" : "$" + Math.round(v).toLocaleString());
const fmtPct   = (v) => (v == null ? "—" : `${v}%`);

// stZone and its ST_OPT_LO / ST_OPT_HI band are gone with the velocity signal and the
// colour-coded sell-through column, which were its only two callers. The band claimed
// 35–70% was "healthy pace" for every product; because sell-through is W/(W + cover),
// that was really "cover between 13 and 56 days" — a pair of fleet-wide constants
// applied to products whose own healthy range is lt*1.2 to lt+cov. A 3-day-lead air
// line sat permanently above it, a 60-day sea line permanently below it, and both
// were fine. Stock coverage judges the same thing per-product.

// ─── SIGNALS (expanded row) ──────────────────────────────────────────────────
function Signals({ view, lm }) {
  const barBg = lm ? "bg-slate-200" : "bg-slate-800";
  const label = lm ? "text-slate-600" : "text-slate-300";
  const muted = lm ? "text-slate-500" : "text-slate-500";
  const head  = lm ? "text-slate-400" : "text-slate-600";
  const color = (s) => (s >= 0.66 ? "bg-emerald-500/80" : s >= 0.33 ? "bg-amber-500/80" : "bg-rose-500/80");
  return (
    <div className="space-y-3">
      <div>
        <div className={`text-[11px] uppercase tracking-widest font-bold ${head}`}>Signals behind this status</div>
        <div className={`text-[11px] ${muted} mt-0.5`}>Green is strong, amber is mixed, red is weak.</div>
      </div>
      {/* The special "Sales velocity" renderer that used to sit here is gone with the
          signal it drew. Sell-through is days-of-cover in other units, so its band
          restated the Stock coverage bar directly above it — and restated it with
          fleet-wide thresholds, which is why healthy air lines read "under-stocked"
          and healthy sea lines read "slow mover". */}
      {(view.signals || []).map((c) => {
        return (
          <div key={c.key} className="flex items-center gap-3">
            <div className="w-40 shrink-0">
              <Tip text={c.explanation}>
                <span className={`text-[11px] font-semibold ${label}`}>{c.label}</span>
              </Tip>
              <div className={`text-[10px] ${muted}`}>{c.raw}</div>
            </div>
            <div className="flex-1">
              <div className={`h-2 ${barBg} rounded-full overflow-hidden`}>
                <div className={`h-full ${color(c.score)} rounded-full`} style={{ width: `${Math.max(5, c.score * 100)}%` }} />
              </div>
            </div>
          </div>
        );
      })}
      {view.flags?.length > 0 && (
        <div className="pt-1 space-y-1.5">
          {view.flags.map((f, i) => (
            <div key={i} className={`text-[11px] flex items-start gap-1.5 ${lm ? "text-amber-700" : "text-amber-400"}`}>
              <span className="shrink-0">⚑</span><span>{f.text}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── EXPANDED ROW DETAIL ─────────────────────────────────────────────────────
function RowDetail({ row, view, showPromoToggle, promoBaseline, setPromoBaseline, lm }) {
  const cardBg = lm ? "bg-slate-50 border-slate-200" : "bg-[#0a0f16] border-slate-800/60";
  const label  = lm ? "text-slate-500" : "text-slate-500";
  const val    = lm ? "text-slate-900" : "text-white";
  return (
    <div className={`${cardBg} border rounded-xl p-4 grid grid-cols-1 lg:grid-cols-2 gap-5`}>
      <Signals view={view} lm={lm} />
      <div className="space-y-4">
        {showPromoToggle && (
          <div className={`rounded-lg border p-2.5 ${lm ? "bg-violet-50 border-violet-200" : "bg-violet-950/20 border-violet-900/40"}`}>
            <div className="flex items-center justify-between gap-2">
              <Tip text={HELP.promo}>
                <span className={`text-[11px] font-bold ${lm ? "text-violet-700" : "text-violet-300"}`}>Promotion active</span>
              </Tip>
              <div className="flex rounded-lg overflow-hidden border border-violet-400/40 text-[11px] font-semibold">
                <button onClick={() => setPromoBaseline(false)}
                  className={`px-2.5 py-1 ${!promoBaseline ? "bg-violet-600 text-white" : (lm ? "bg-white text-violet-600" : "bg-transparent text-violet-300")}`}>
                  On sale
                </button>
                <button onClick={() => setPromoBaseline(true)}
                  className={`px-2.5 py-1 ${promoBaseline ? "bg-violet-600 text-white" : (lm ? "bg-white text-violet-600" : "bg-transparent text-violet-300")}`}>
                  Normal
                </button>
              </div>
            </div>
            <p className={`text-[11px] mt-1.5 leading-relaxed ${lm ? "text-violet-700/80" : "text-violet-300/70"}`}>
              {promoBaseline
                ? "Showing the status as if the sale weren't running — use this to plan what to reorder once the sale ends."
                : "Showing the status during the sale, when it's selling faster than normal — this is what your stock has to get through right now."}
            </p>
          </div>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Stat label="Profit margin" help={HELP.margin} value={fmtPct(view.marginPct)} lm={lm} />
          <Stat label="Return grade"
            help={HELP.returnTier + (view.marginPct != null ? ` This item's margin is ${view.marginPct}%.` : " Add a unit cost to grade it.")}
            value={view.returnTier === "—" ? "— add cost" : `${view.returnTier} · ${({ A: "Strong", B: "Moderate", C: "Slim", F: "Loss" })[view.returnTier] || ""}`} lm={lm} />
        </div>
        <div className={`rounded-lg border p-3 ${lm ? "bg-white border-slate-200" : "bg-[#0d1117] border-slate-800"}`}>
          <Tip text={HELP.recommendation}>
            <span className={`text-[11px] uppercase tracking-widest font-bold ${label}`}>Recommended action</span>
          </Tip>
          <div className={`text-sm font-bold mt-1 ${val}`}>{view.recommendation.action}</div>
          <p className={`text-[11px] mt-1 leading-relaxed ${label}`}>{view.recommendation.reason}</p>
        </div>
      </div>
    </div>
  );
}

function Stat({ label, help, value, lm }) {
  return (
    <div className={`rounded-lg border p-2.5 ${lm ? "bg-white border-slate-200" : "bg-[#0d1117] border-slate-800"}`}>
      <Tip text={help}><span className={`text-[10px] uppercase tracking-widest font-semibold ${lm ? "text-slate-500" : "text-slate-500"}`}>{label}</span></Tip>
      <div className={`text-base font-bold tabular-nums ${lm ? "text-slate-900" : "text-white"}`}>{value}</div>
    </div>
  );
}

// ─── INLINE NUMBER INPUT (cost / fees) ───────────────────────────────────────
function NumInput({ skuId, current, onSave, lm, placeholder }) {
  const [v, setV] = useState(current ?? "");
  useEffect(() => { setV(current ?? ""); }, [current]);
  const inp = lm
    ? "w-16 bg-white border border-slate-300 rounded px-1.5 py-0.5 text-xs text-slate-900 focus:outline-none focus:border-sky-500"
    : "w-16 bg-slate-900 border border-slate-700 rounded px-1.5 py-0.5 text-xs text-slate-200 focus:outline-none focus:border-sky-600";
  return (
    <div className="flex items-center gap-1">
      <span className={lm ? "text-slate-400 text-xs" : "text-slate-600 text-xs"}>$</span>
      <input type="number" step="0.01" min="0" value={v} placeholder={placeholder}
        onChange={(e) => setV(e.target.value)}
        onBlur={() => { if (`${v}` !== `${current ?? ""}`) onSave(skuId, v === "" ? null : parseFloat(v)); }}
        onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
        className={inp} />
    </div>
  );
}

// ─── SCATTER TOOLTIP ─────────────────────────────────────────────────────────
function ScatterTip({ active, payload, lm }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div style={{ background: lm ? "#fff" : "#0d1117", border: `1px solid ${lm ? "#cbd5e1" : "#1e293b"}`, borderRadius: 8, padding: "6px 10px", fontSize: 11 }}>
      <div style={{ fontWeight: "bold", color: lm ? "#0f172a" : "#fff" }}>{d.skuName}</div>
      <div style={{ color: STATUS_STYLE[d.status]?.hex || "#94a3b8" }}>{statusInfo(d.status).title}</div>
      {/* Raw days AND the multiple, so the normalised axis stays readable. */}
      <div style={{ color: lm ? "#64748b" : "#94a3b8" }}>Stock: {fmtCover(d.daysOfCover)}
        {d.target > 0 && <> · target {d.target}d</>}</div>
      <div style={{ color: lm ? "#64748b" : "#94a3b8" }}>Margin {d.marginPct != null ? d.marginPct + "%" : "—"} (grade {d.returnTier})</div>
      <div style={{ color: lm ? "#64748b" : "#94a3b8" }}>Cash tied up {fmtMoney(d.carryingValue)}</div>
    </div>
  );
}

// ─── MAIN ────────────────────────────────────────────────────────────────────
export default function ScorecardTab({ api = "http://localhost:8000", skuList = [], skuParams = {}, setSkuParams, openPOs = {}, lm = false, folders = {}, suppliers = {} }) {
  const [data, setData]       = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState(null);
  const [sortKey, setSortKey] = useState("status");
  const [sortDir, setSortDir] = useState("asc");        // worst status first
  const [search, setSearch]   = useState("");
  const [statusFilter, setStatusFilter] = useState(null);
  const [expanded, setExpanded]   = useState(null);
  const [baselineFor, setBaselineFor] = useState({});
  const [collapsedFolders, setCollapsedFolders] = useState({});   // folderId → collapsed?

  /* Shared with App.jsx (which scores the fleet card) so both tabs ask this
     endpoint the SAME question. This copy used to send the raw params.leadTime
     while App sent the resolved planning lead time, and the two tabs then reported
     different statuses for the same catalogue. */
  const buildBody = useCallback(
    () => buildScorecardBody(skuList, skuParams, openPOs, suppliers),
    [skuList, skuParams, openPOs, suppliers]);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const res = await fetch(`${api}/api/scorecard`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildBody()),
      });
      if (!res.ok) throw new Error(`API error ${res.status}`);
      setData(await res.json());
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [api, buildBody]);

  useEffect(() => { load(); }, [load]);

  const saveCost = (skuId, cost) => {
    if (setSkuParams) setSkuParams((prev) => ({ ...prev, [skuId]: { ...(prev?.[skuId] || {}), unitCost: cost } }));
  };
  const saveFees = (skuId, fees) => {
    if (setSkuParams) setSkuParams((prev) => ({ ...prev, [skuId]: { ...(prev?.[skuId] || {}), fees: fees } }));
  };

  // Pick the right view (live vs baseline) for a row.
  const viewFor = (row) => (row.onPromoToday && baselineFor[row.skuId] && row.baselineView) ? row.baselineView : row;

  function pick(view, row, key) {
    switch (key) {
      case "status":       return view.statusRank ?? 9;
      case "daysOfCover":  return view.daysOfCover ?? 99999;
      case "sellThrough":  return view.sellThrough ?? -1;
      case "marginPct":    return view.marginPct ?? -1;
      case "carryingValue":return row.carryingValue ?? -1;
      case "skuName":      return row.skuName.toLowerCase();
      default:             return view.statusRank ?? 9;
    }
  }

  const rows = useMemo(() => {
    if (!data?.rows) return [];
    let r = [...data.rows];
    if (search.trim()) {
      const q = search.toLowerCase();
      r = r.filter((x) => x.skuName.toLowerCase().includes(q) || x.skuId.toLowerCase().includes(q));
    }
    if (statusFilter) r = r.filter((x) => viewFor(x).status === statusFilter);
    r.sort((a, b) => {
      const va = pick(viewFor(a), a, sortKey), vb = pick(viewFor(b), b, sortKey);
      const na = va == null ? -Infinity : va, nb = vb == null ? -Infinity : vb;
      return sortDir === "asc" ? (na > nb ? 1 : -1) : (na < nb ? 1 : -1);
    });
    return r;
  }, [data, search, statusFilter, sortKey, sortDir, baselineFor]);

  const toggleSort = (key) => {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir(key === "skuName" || key === "status" ? "asc" : "desc"); }
  };

  const exportCsv = () => {
    const cols = ["SKU", "Name", "Status", "Days of Stock Remaining", "Profit Grade", "Margin %", "Sell-through %", "Cash Tied Up", "Unit Cost", "Fees", "Recommendation"];
    const lines = [cols.join(",")];
    rows.forEach((row) => {
      const v = viewFor(row);
      lines.push([
        row.skuId, `"${row.skuName}"`, `"${v.status}"`,
        v.daysOfCover ?? "365+", v.returnTier, v.marginPct ?? "",
        v.sellThrough == null ? "" : Math.round(v.sellThrough * 100),
        row.carryingValue ?? "", row.unitCost ?? "", row.fees ?? 0, `"${v.recommendation.action}"`,
      ].join(","));
    });
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "sku_scorecard.csv"; a.click();
    URL.revokeObjectURL(url);
  };

  // styling shorthands
  const page   = lm ? "text-slate-900" : "text-slate-100";
  const card   = lm ? "bg-white border-slate-200" : "bg-[#0d1117] border-slate-800";
  const muted  = lm ? "text-slate-500" : "text-slate-500";
  const thCls  = `text-left text-[11px] uppercase tracking-widest font-bold ${lm ? "text-slate-500" : "text-slate-500"} px-3 py-2 select-none cursor-pointer`;
  const rowBorder = lm ? "border-slate-100" : "border-slate-800/60";

  if (error) return <div className={`p-6 text-sm ${lm ? "text-rose-600" : "text-rose-400"}`}>Couldn't load scorecard: {error}. Is the backend running?</div>;
  if (!data && loading) return <div className={`p-6 text-sm ${muted}`}>Assessing inventory…</div>;
  if (!data) return null;

  const dist = data.distribution || {};
  const overdueCount = (data.rows || []).filter(
    (r) => r.status === "Stockout risk" && (r.daysUntilReorder ?? 0) < 0).length;
  const total = Object.values(dist).reduce((a, b) => a + b, 0);

  /* Scatter x-axis: stock as a MULTIPLE of this product's own target cover, not raw
   * days.
   *
   * It used to plot raw days with a green "healthy" band hardcoded at 17–44 — the
   * numbers the default 14-day lead time and 30-day coverage happen to produce. Every
   * product got judged against that one band, so a line with a 90-day coverage target
   * sitting healthily on 70 days of stock appeared far to the right of "healthy", and
   * its own green dot contradicted the background it sat on. There is no single band:
   * the ceiling is lead time + coverage, and that is per-product.
   *
   * Dividing by that ceiling makes one line true for everyone — 1.0 is exactly at
   * target for every product, whatever its lead time — and lets a 90-day-cover line
   * and a 30-day one be compared on the same axis, which raw days never allowed.
   * 3.0 is where sc_status starts calling it dead stock. */
  const X_CAP = 3;
  const targetCover = Object.fromEntries((data.rows || []).map((r) =>
    [r.skuId, (Number(r.leadTime) || 0) + (Number(r.coverage) || 0)]));
  const scatterData = (data.scatter || [])
    .filter((d) => d.marginPct != null && targetCover[d.skuId] > 0)
    .map((d) => ({ ...d, target: targetCover[d.skuId],
      x: d.daysOfCover == null ? X_CAP : Math.min(d.daysOfCover / targetCover[d.skuId], X_CAP) }));

  // Needs pricing review: products with a known cost whose margin is ≤ 0 (sold at or
  // below cost). These make nothing or lose money per sale — a pricing problem, not an
  // inventory one — so they get their own section at the top. None → no section.
  const needsPricing = (data.rows || [])
    .filter((r) => { const v = viewFor(r); return v.marginPct != null && v.marginPct <= 0; })
    .sort((a, b) => (viewFor(a).marginPct ?? 0) - (viewFor(b).marginPct ?? 0));   // most negative first

  return (
    <div className={`p-4 sm:p-6 space-y-5 ${page}`}>
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-lg font-bold tracking-tight flex items-center gap-2">
            SKU Scorecard
            <Tip text="A point-in-time view of your inventory's health: each item's current situation, what to do about it, and how profitable it is. The forecast tab projects future demand; this assesses your current position.">
              <span className={`text-[11px] font-normal ${muted}`}>what is this?</span>
            </Tip>
          </h2>
          <p className={`text-[11px] ${muted}`}>Assessed {data.generatedAt} · trailing {data.trailingDays} days · {total} SKUs</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={load} disabled={loading}
            className={`text-[11px] px-3 py-1.5 rounded-lg border ${lm ? "border-slate-300 hover:bg-slate-100 text-slate-600" : "border-slate-700 hover:bg-slate-800 text-slate-300"}`}>
            {loading ? "Refreshing…" : "Refresh"}
          </button>
          <button onClick={exportCsv}
            className="text-[11px] px-3 py-1.5 rounded-lg bg-sky-600 hover:bg-sky-500 text-white font-semibold">
            Export CSV
          </button>
        </div>
      </div>

      {/* Needs pricing review — products sold at or below cost (only shown if any exist) */}
      {needsPricing.length > 0 && (
        <div className={`border rounded-2xl overflow-hidden ${lm ? "bg-rose-50 border-rose-300" : "bg-rose-950/15 border-rose-900/40"}`}>
          <div className={`px-4 py-2.5 flex items-center gap-2 flex-wrap border-b ${lm ? "border-rose-200" : "border-rose-900/40"}`}>
            <span className="text-base">⚠️</span>
            <span className={`text-xs font-bold uppercase tracking-widest ${lm ? "text-rose-800" : "text-rose-300"}`}>Needs pricing review</span>
            <span className={`text-[10px] font-mono rounded px-1.5 py-0.5 ${lm ? "bg-rose-200 text-rose-800" : "bg-rose-900/50 text-rose-300"}`}>{needsPricing.length}</span>
            <span className={`text-[11px] ${lm ? "text-rose-700" : "text-rose-400/80"}`}>priced at or below cost — you make nothing or lose money on each sale</span>
          </div>
          <div className={`divide-y ${lm ? "divide-rose-100" : "divide-rose-900/30"}`}>
            {needsPricing.map((r) => {
              const v = viewFor(r);
              return (
                <button key={r.skuId} onClick={() => setExpanded(r.skuId)}
                  className={`w-full text-left px-4 py-2.5 flex items-center justify-between gap-3 transition-colors ${lm ? "hover:bg-rose-100/60" : "hover:bg-rose-950/30"}`}>
                  <div className="min-w-0">
                    <div className="text-sm font-semibold truncate">{r.skuName}</div>
                    <div className={`text-[10px] font-mono ${muted}`}>{r.skuId}</div>
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    <span className={`text-[11px] tabular-nums ${muted}`}>cost ${Number(r.unitCost).toLocaleString()}{r.fees ? ` + $${Number(r.fees).toLocaleString()} fees` : ""}</span>
                    <span className={`px-2 py-0.5 rounded text-xs font-bold tabular-nums border ${lm ? "bg-rose-100 text-rose-700 border-rose-300" : "bg-rose-950/40 text-rose-300 border-rose-800"}`}>{v.marginPct}% margin</span>
                    <span className={`px-2 py-0.5 rounded text-[11px] font-bold border ${lm ? TIER_STYLE.F.lm : TIER_STYLE.F.dk}`}>F</span>
                  </div>
                </button>
              );
            })}
          </div>
          <div className={`px-4 py-2 text-[11px] ${lm ? "text-rose-700/90" : "text-rose-400/70"}`}>
            Fix the price, unit cost, or fees in the table below. Until then, these are excluded from the backtest's tier cost comparison — no amount of safety stock makes a money-losing product profitable.
          </div>
        </div>
      )}

      {/* Status chips (click to filter) */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        {STATUS_ORDER.map((s) => {
          const st = STATUS_STYLE[s];
          const active = statusFilter === s;
          return (
            <button key={s} onClick={() => setStatusFilter(active ? null : s)}
              title={statusInfo(s).help}
              className={`${card} border rounded-xl p-3 text-left transition-all ${active ? "ring-2 ring-sky-500" : ""}`}>
              <div className="flex items-center gap-2">
                <span className={`h-2 w-2 rounded-full ${st.dot}`} />
                {/* Wording comes from the shared table, so this chip and the fleet's
                    legend can never call the same status two different things. */}
                <span className={`text-[11px] uppercase tracking-widest font-bold ${muted} leading-tight`}>{statusInfo(s).title}</span>
              </div>
              <div className="text-2xl font-bold tabular-nums mt-1">{dist[s] ?? 0}</div>
              {/* Not every at-risk product is overdue — some are simply too close to
                  the reorder point to order in time — so the late ones are split out
                  rather than the whole chip being labelled "(overdue)". */}
              {s === "Stockout risk" && overdueCount > 0 && (
                <div className={`text-[10px] ${muted} mt-0.5`}>{overdueCount} already overdue</div>
              )}
            </button>
          );
        })}
      </div>

      {/* Stock position × profit scatter */}
      <div className={`${card} border rounded-2xl p-4`}>
        <div className="flex items-center gap-2 mb-1">
          <span className="text-[11px] font-bold uppercase tracking-widest">Stock position vs. profit</span>
          <Tip text="Each point is a product, plotted against ITS OWN target cover (lead time + coverage window) rather than a fixed number of days — so a line that stocks 90 days on purpose and one that stocks 30 are comparable here. 1.0 is exactly at target; past it is carrying more than needed. Higher up = stronger margin. Color shows status; point size is cash tied up. Watch for large points on the right — that's cash sitting in overstock.">
            <span className={`text-[11px] ${muted}`}>read me</span>
          </Tip>
        </div>
        <div style={{ width: "100%", height: 260 }}>
          <ResponsiveContainer>
            <ScatterChart margin={{ top: 10, right: 16, bottom: 16, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={lm ? "#e2e8f0" : "#1e293b"} />
              {/* At or under target cover is fine; past 1.0 is carrying more than the
                  product's own lead time + coverage window asks for. True for every
                  product on the chart, which the old fixed 17–44 band was not. */}
              <ReferenceArea x1={0.4} x2={1} fill="#10b981" fillOpacity={lm ? 0.07 : 0.06} />
              <XAxis type="number" dataKey="x" name="Stock vs target cover" domain={[0, X_CAP]}
                ticks={[0, 1, 2, 3]} tickFormatter={(v) => (v === 1 ? "target" : `${v}×`)}
                tick={{ fill: lm ? "#64748b" : "#475569", fontSize: 9 }}
                label={{ value: "Stock vs. this product's target cover →", position: "insideBottom", offset: -6, fill: lm ? "#64748b" : "#475569", fontSize: 10 }} />
              <YAxis type="number" dataKey="marginPct" name="Profit margin" domain={[0, "auto"]} unit="%"
                tick={{ fill: lm ? "#64748b" : "#475569", fontSize: 9 }}
                label={{ value: "Higher margin ↑", angle: -90, position: "insideLeft", fill: lm ? "#64748b" : "#475569", fontSize: 10 }} />
              <ZAxis type="number" dataKey="carryingValue" range={[40, 400]} />
              <Tooltip content={<ScatterTip lm={lm} />} cursor={{ strokeDasharray: "3 3" }} />
              <Scatter data={scatterData}>
                {scatterData.map((d, i) => (
                  <Cell key={i} fill={STATUS_STYLE[d.status]?.hex || "#64748b"} fillOpacity={0.75} />
                ))}
              </Scatter>
            </ScatterChart>
          </ResponsiveContainer>
        </div>
        {(data.scatter || []).some((d) => d.marginPct == null) && (
          <p className={`text-[11px] ${muted} mt-1`}>SKUs without a unit cost are hidden from this chart — add a cost in the table to plot them.</p>
        )}
      </div>

      {/* Controls */}
      <div className="flex items-center gap-2 flex-wrap">
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search SKU…"
          className={`text-xs px-3 py-1.5 rounded-lg border w-48 ${lm ? "bg-white border-slate-300 text-slate-900" : "bg-slate-900 border-slate-700 text-slate-200"} focus:outline-none focus:border-sky-500`} />
        {statusFilter && (
          <button onClick={() => setStatusFilter(null)} className={`text-[11px] px-2 py-1 rounded ${lm ? "bg-slate-100 text-slate-600" : "bg-slate-800 text-slate-400"}`}>
            {statusFilter} ✕
          </button>
        )}
        <span className={`text-[11px] ${muted}`}>{rows.length} shown</span>
      </div>

      {/* Table */}
      <div className={`${card} border rounded-2xl overflow-hidden`}>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className={`border-b ${rowBorder}`}>
                <th className={thCls} onClick={() => toggleSort("skuName")}>
                  <Tip text="Click any row to see the signals behind its status.">SKU</Tip>
                </th>
                <th className={thCls} onClick={() => toggleSort("status")}><Tip text={HELP.status}>Status{arrow(sortKey, sortDir, "status")}</Tip></th>
                <th className={thCls} onClick={() => toggleSort("daysOfCover")}><Tip text={HELP.cover}>Days of Stock Remaining{arrow(sortKey, sortDir, "daysOfCover")}</Tip></th>
                <th className={thCls} onClick={() => toggleSort("marginPct")}><Tip text={HELP.returnTier}>Profit grade{arrow(sortKey, sortDir, "marginPct")}</Tip></th>
                <th className={thCls} onClick={() => toggleSort("sellThrough")}><Tip text={HELP.sellThrough}>Selling speed{arrow(sortKey, sortDir, "sellThrough")}</Tip></th>
                <th className={thCls} onClick={() => toggleSort("carryingValue")}><Tip text={HELP.carrying}>Cash tied up{arrow(sortKey, sortDir, "carryingValue")}</Tip></th>
                <th className={thCls}><Tip text={HELP.cost}>Unit cost</Tip></th>
                <th className={thCls}><Tip text={HELP.fees}>Fees</Tip></th>
                <th className={thCls}><Tip text={HELP.recommendation}>Action</Tip></th>
              </tr>
            </thead>
            <tbody>
              {(() => {
                const renderRow = (row) => {
                const v = viewFor(row);
                const st = STATUS_STYLE[v.status] || STATUS_STYLE["Healthy"];
                const ts = TIER_STYLE[v.returnTier] || TIER_STYLE["—"];
                const isOpen = expanded === row.skuId;
                return (
                  <React.Fragment key={row.skuId}>
                    <tr onClick={() => setExpanded(isOpen ? null : row.skuId)}
                      className={`border-b ${rowBorder} cursor-pointer transition-colors ${lm ? "hover:bg-slate-50" : "hover:bg-slate-800/40"}`}>
                      <td className="px-3 py-2.5">
                        <div className="flex items-center gap-2">
                          <span className={`text-[11px] ${muted}`}>{isOpen ? "▾" : "▸"}</span>
                          <div>
                            <div className="text-xs font-semibold">{row.skuName}</div>
                            <div className={`text-[10px] font-mono ${muted}`}>{row.skuId}{row.onPromoToday ? " · 🏷 promo" : ""}</div>
                          </div>
                        </div>
                      </td>
                      <td className="px-3 py-2.5">
                        {/* Row badge: same wording and same hover definition as the chip
                            above it and the fleet's legend. Here we know the product's
                            actual days, so a genuinely late one can say "overdue" —
                            which the bucket label itself must not claim for all of them. */}
                        <span title={statusInfo(v.status).help}
                          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-bold border cursor-help ${lm ? st.lm : st.dk}`}>
                          <span className={`h-1.5 w-1.5 rounded-full ${st.dot}`} />
                          {statusInfo(v.status).title}
                          {v.status === "Stockout risk" && v.daysUntilReorder < 0 &&
                            <span className="font-normal opacity-80">· overdue {Math.abs(v.daysUntilReorder)}d</span>}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 text-xs font-mono tabular-nums">{fmtCover(v.daysOfCover)}</td>
                      <td className="px-3 py-2.5">
                        <span className={`px-2 py-0.5 rounded text-[11px] font-bold border ${lm ? ts.lm : ts.dk}`}>{v.returnTier}</span>
                      </td>
                      {/* Plain number, no colour verdict: the thresholds behind that
                          colouring are the ones we just established are wrong per-SKU.
                          Still sortable, so you can rank by it. */}
                      <td className="px-3 py-2.5 text-xs font-mono tabular-nums">{v.sellThrough == null ? "—" : `${Math.round(v.sellThrough*100)}%`}</td>
                      <td className="px-3 py-2.5 text-xs font-mono tabular-nums">{fmtMoney(row.carryingValue)}</td>
                      <td className="px-3 py-2.5" onClick={(e) => e.stopPropagation()}>
                        <NumInput skuId={row.skuId} current={row.unitCost} onSave={saveCost} lm={lm} placeholder="cost" />
                      </td>
                      <td className="px-3 py-2.5" onClick={(e) => e.stopPropagation()}>
                        <NumInput skuId={row.skuId} current={row.fees} onSave={saveFees} lm={lm} placeholder="fees" />
                      </td>
                      <td className="px-3 py-2.5 text-[11px] max-w-[180px]">{v.recommendation.action}</td>
                    </tr>
                    {isOpen && (
                      <tr className={lm ? "bg-slate-50/60" : "bg-black/20"}>
                        <td colSpan={9} className="px-3 py-3">
                          <RowDetail
                            row={row} view={v}
                            showPromoToggle={row.onPromoToday && !!row.baselineView}
                            promoBaseline={!!baselineFor[row.skuId]}
                            setPromoBaseline={(b) => setBaselineFor((prev) => ({ ...prev, [row.skuId]: b }))}
                            lm={lm}
                          />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
                };
                const folderList = Object.entries(folders || {});
                if (!folderList.length) return rows.map(renderRow);
                // Group the (already sorted/filtered) rows by folder; ungrouped go last.
                const folderOf = (skuId) => folderList.find(([, f]) => (f.skuIds || []).includes(skuId))?.[0] || null;
                const byFolder = {}; const ungrouped = [];
                rows.forEach((row) => { const fid = folderOf(row.skuId); if (fid) (byFolder[fid] ||= []).push(row); else ungrouped.push(row); });
                const sections = [];
                folderList.forEach(([fid, f]) => { if (byFolder[fid]?.length) sections.push([fid, f.name || "Folder", byFolder[fid]]); });
                if (ungrouped.length) sections.push([null, "Ungrouped", ungrouped]);
                return sections.map(([fid, name, frows]) => {
                  const collapsed = fid && collapsedFolders[fid];
                  return (
                    <React.Fragment key={fid || "__ungrouped"}>
                      <tr onClick={() => fid && setCollapsedFolders((c) => ({ ...c, [fid]: !c[fid] }))}
                        className={`border-b ${rowBorder} ${fid ? "cursor-pointer" : ""} ${lm ? "bg-slate-100/70 hover:bg-slate-100" : "bg-white/[0.03] hover:bg-white/[0.06]"}`}>
                        <td colSpan={9} className="px-3 py-2">
                          <div className="flex items-center gap-2">
                            {fid ? <span className={`text-[11px] ${muted}`}>{collapsed ? "▸" : "▾"}</span> : null}
                            <svg className={`h-3.5 w-3.5 ${fid ? (lm ? "text-violet-600" : "text-violet-400") : muted}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" /></svg>
                            <span className="text-xs font-bold">{name}</span>
                            <span className={`text-[10px] font-mono rounded px-1.5 py-0.5 ${lm ? "bg-slate-200 text-slate-600" : "bg-white/10 text-slate-400"}`}>{frows.length}</span>
                          </div>
                        </td>
                      </tr>
                      {!collapsed && frows.map(renderRow)}
                    </React.Fragment>
                  );
                });
              })()}
              {rows.length === 0 && (
                <tr><td colSpan={9} className={`px-3 py-8 text-center text-xs ${muted}`}>No SKUs match.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <p className={`text-[11px] ${muted} leading-relaxed`}>
        The status describes each item's current inventory situation and what to do about it; the profit grade (A/B/C) reflects how profitable each sale is. A reorder coming due is treated as a routine action ("Reorder due"), not a health problem — only a missed window or a position you can't recover from reads as "Stockout risk." Select any row for the signals behind its status.
      </p>
    </div>
  );
}

function arrow(sortKey, sortDir, key) {
  if (sortKey !== key) return "";
  return sortDir === "asc" ? " ↑" : " ↓";
}
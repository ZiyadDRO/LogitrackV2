import React from "react";
import { useState, useEffect, useRef } from "react";
// Use the shared Groq config rather than a local copy — a duplicated model string
// here was left behind when the model was last changed, and silently 404'd.
import { GROQ_URL, GROQ_MODEL } from "./lib/ai";

const Z = { 90: 1.2816, 95: 1.6449, 98: 2.0537, 99: 2.3263 };  // service level → z

// ── plain-English hover explanations (what it is · how it affects orders · what it means) ──
const TIP = {
  name:      "The product or forecasting engine this row is scoring.",
  forecasts: "How many past-date tests ran for this row. More tests = more trustworthy averages.",
  wape:      "WAPE. Average miss size, as a % of sales. Lower is better. Runs high for sparse/sporadic sellers, so don't over-trust it there.",
  mase:      "MASE. Accuracy vs a naive 'same as last season' guess. Under 1 = beating that guess; over 1 = worse than it. The best single accuracy score.",
  bias:      "Does the forecast lean high or low? Plus = over-forecasting (you'd over-order). Minus = under-forecasting (stockout risk). Near 0 is ideal.",
  cov:       "80% interval coverage. How often real sales landed inside the forecast's uncertainty band. Should be ~80%. Lower means the bands are too narrow / overconfident.",
  order:     "How far off the suggested order size was. Plus = ordered too much, minus = too little. Near 0 means right-sized orders.",
  sku:       "The product this buffer math is for.",
  sigma:     "How much this product's daily sales bounce around. A bigger swing needs a bigger safety buffer.",
  cv:        "That daily swing relative to average sales. High = unpredictable demand.",
  z:         "A multiplier set by your service target. A higher target gives a bigger number, so a bigger buffer.",
  rootlead:  "Stretches the buffer for how long a restock takes. Longer lead time = bigger buffer.",
  buffer:    "The resulting safety stock in units (z × σ × √lead): spare units kept on hand so you don't run out.",
  ci:        "95% confidence interval. Test windows overlap and repeat per product, so the headline number is less certain than the raw count suggests. If your target sits inside this range, you don't have enough evidence to call it a real gap.",
  windows:   "How many test windows this product got. Products with few windows produce noisy numbers — read them as a hint, not a result.",
  train:     "How much sales history the model had at the moment it made each forecast. Accuracy naturally improves with history, so this splits 'the model is weak' from 'there wasn't enough data yet'.",
  block:     "Recent windows test how things work now. Historic windows reach further back and may reflect an older pricing or channel mix — useful for seasonal coverage, but a worse guide to today.",
  baseline:  "The same tests re-run with ONE engine forced onto every product, instead of picking per product. If routing isn't clearly better, the added complexity isn't paying for itself.",
  optimal:   "The service level where one more unit of safety stock stops paying for itself, worked out from your own margins and holding rate. It's a continuous answer — the four tiers are just the dial settings you can pick from.",
  achievedVsTarget: "Tiers are TARGETS. The buffer under-delivers, so a 99% target may only achieve 97%. Pick the tier whose ACHIEVED number lands nearest the optimum, not the one whose label looks right.",
  pcheapest: "How often this tier came out cheapest when we resampled your products. Tier costs come from averages over a handful of windows, so a small dollar gap can be pure chance. Under about 2-to-1 over the runner-up, treat them as equivalent.",
  capacity:  "How many test windows your sales history can actually support. Asking for more doesn't exclude any product — short-history products just quietly return fewer windows, which then looks like a result when it's really a sample-size problem.",
};
const PTIP = {
  horizon:  "How far ahead each test forecasts before it's graded against what really happened.",
  minTrain: "The least history a product needs to be testable. Below this, it's skipped.",
};

// ── column dictionary ────────────────────────────────────────────────────────
// [label, what the number IS, what a good one looks like]. Rendered as a visible,
// expandable guide under each table — hover tooltips alone aren't discoverable, and
// don't exist at all on a touchscreen.
const COLDEF = {
  tests:    ["Tests", "How many past dates we forecast from, then graded against what actually sold.", "More is steadier. Under 4 is too few to read as a result."],
  avgmiss:  ["Avg miss", "Typical gap between forecast and actual, as a % of units sold. Weighted by volume, so busy weeks count more. (WAPE)", "Lower is better, but it runs high on slow sellers no matter how good the model is — judge those on 'vs naive'."],
  vsnaive:  ["vs naive", "Accuracy compared with just repeating what sold this time last week. 0.8 means 20% less error than that guess. (MASE)", "Under 1 beats the naive guess. Over 1 means you'd do better with the simple rule."],
  runs:     ["Runs high/low", "Whether the forecast leans over or under on average. +10% means it predicted 10% more than sold.", "Near 0. Plus builds overstock, minus risks stockouts."],
  bandhit:  ["Band hit rate", "How often real sales landed inside the forecast's uncertainty range. (80% interval coverage)", "About 80%. Lower means the range is too narrow and the tool is overconfident."],
  instock:  ["Stayed in stock", "Share of past reorder cycles where the buffer was big enough to avoid running out.", "Should match your protection target. Below it means the buffer is too thin."],
  ordersize:["Order size", "How far off the suggested order quantity was. +10% means it would have ordered 10% too much.", "Near 0. Plus ties up cash in excess stock, minus risks running short."],
  bufunits: ["Buffer units", "Spare units carried across the catalog at this level. Multiply by unit cost and your holding rate to get the buffer cost.", "Rises with the level \u2014 this is the stock you're actually paying to hold."],
  missunits:["Missed units / yr", "Units of demand per year you'd fail to cover at this level. Multiply by profit per unit to get the lost profit.", "Falls as the level rises \u2014 this is what a stockout costs you in product, before money."],
  tier:     ["Level", "The protection target — how hard the buffer tries to prevent a stockout.", "Higher keeps more spare stock, costs more to hold."],
  achieved: ["Achieved", "What that target actually delivered in testing. Targets and outcomes differ because the buffer under-delivers.", "Should land near the level itself. Well below means the buffer is under-sized there."],
  ci:       ["95% CI", "The range the true number is likely to sit in, given how few tests there are.", "Narrower is more certain. If your target sits inside it, you can't call the gap real."],
  vstarget: ["vs target", "Achieved minus the level. Negative means under-protecting.", "Near 0. Marked 'n/s' when the gap is inside the confidence range, i.e. not meaningful."],
  pcheap:   ["P(cheapest)", "How often this level came out cheapest when we re-ran the maths on resampled products.", "Over about 60% is a real preference. Two levels near 50/50 are a coin flip."],
  lostprofit:["Lost profit / yr", "Margin you'd forgo each year to stockouts at this level, across the catalog.", "Falls as the level rises. Trade it against buffer cost."],
  buffercost:["Buffer cost / yr", "Yearly cost of holding the safety stock — storage, tied-up cash, obsolescence.", "Rises as the level rises."],
  totalcost:["Total $/yr", "Lost profit plus buffer cost. The number to minimise.", "Lowest wins — but check P(cheapest) before trusting a small gap."],
  cash:     ["Cash in buffer", "One-time working capital parked in safety stock. Not a yearly cost; its yearly cost is the buffer column.", "Lower frees cash. Worth weighing if capital is tight."],
  history:  ["History", "How much sales history the model had when it made these forecasts.", "Accuracy usually improves with history — that's data, not model quality."],
  window:   ["Window", "Recent windows test how things work now; historic ones reach further back for seasonal coverage.", "Similar numbers mean nothing major changed in the business."],
  policy:   ["Policy", "Which model choice was used — per-product routing, or one model forced on everything.", "If routing isn't clearly better, the complexity isn't paying for itself."],
};

// ── metric color logic (green = good, amber = watch, red = bad) ──
const tone = (lm, level) => {
  const m = {
    good:  lm ? "text-emerald-600" : "text-emerald-400",
    warn:  lm ? "text-amber-600"   : "text-amber-400",
    bad:   lm ? "text-rose-600"    : "text-rose-400",
    none:  lm ? "text-slate-600"   : "text-slate-300",
  };
  return m[level] || m.none;
};
const lvlMASE = (v) => (v == null ? "none" : v < 1 ? "good" : v < 1.5 ? "warn" : "bad");
const lvlBias = (v) => (v == null ? "none" : Math.abs(v) < 10 ? "good" : Math.abs(v) < 25 ? "warn" : "bad");
const lvlCov  = (v) => (v == null ? "none" : v >= 75 && v <= 88 ? "good" : v >= 65 && v <= 92 ? "warn" : "bad");
const lvlSvc  = (v, t) => (v == null ? "none" : v >= t ? "good" : v >= t - 7 ? "warn" : "bad");
const lvlOrd  = (v) => (v == null ? "none" : Math.abs(v) < 15 ? "good" : Math.abs(v) < 30 ? "warn" : "bad");
const fmt = (v, suffix = "") => (v == null ? "—" : `${v}${suffix}`);
const ciText = (ci, suffix = "") => (Array.isArray(ci) && ci.length === 2 ? `${ci[0]}${suffix} – ${ci[1]}${suffix}` : null);
// ── CSV export ───────────────────────────────────────────────────────────────
// One file, sectioned, so a spreadsheet opens it readably: a titled block per table
// with a blank line between. Everything on screen is included, including the caveats —
// an exported number that loses its "measured on 4 windows" context is worse than none.
const csvCell = (v) => {
  if (v == null) return "";
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};
const csvBlock = (title, cols, rows) => {
  if (!rows?.length) return "";
  const head = cols.map((c) => csvCell(c.label)).join(",");
  const body = rows.map((r) => cols.map((c) => csvCell(c.get(r))).join(",")).join("\n");
  return `${csvCell(title)}\n${head}\n${body}\n\n`;
};
const buildCsv = (res) => {
  const ta = res.tierAnalysis || {}, ov = res.overall || {};
  const P = res.params || {};
  let out = "";
  out += `LogiTrack backtest export\n`;
  out += `Generated,${csvCell(new Date().toISOString())}\n`;
  if (res.ranAt) out += `Test run,${csvCell(new Date(res.ranAt * 1000).toISOString())}\n`;
  out += `Products tested,${res.tested}\nSimulated reorders,${res.forecasts}\n`;
  out += `Lead time (days),${P.lead}\nCoverage window (days),${P.coverage}\n`;
  out += `Holding rate %/yr,${ta.assumptions?.holdingPct ?? ""}\n`;
  out += `Cost basis,${csvCell(ta.costBasis)}\n`;
  if (ta.uncostedSkus?.length) out += `Products missing a cost,${csvCell(ta.uncostedSkus.join(" "))}\n`;
  // Loss-makers are excluded from the dollar totals too — for a different reason, so
  // they have to be named separately or the totals look unexplained.
  if (ta.lossMakingSkus?.length) out += `Excluded - sells at or below cost,${csvCell(ta.lossMakingSkus.join(" "))}\n`;
  out += `Products included in the money totals,${ta.costedSkus} of ${ta.totalSkus}\n`;
  if (res.failedCutoffs) out += `Failed test windows,${res.failedCutoffs}\n`;
  out += "\n";

  const accCols = [
    { label: "Tests", get: (r) => r.forecasts },
    { label: "Avg miss % (WAPE, volume-weighted)", get: (r) => r["WAPE%"] },
    { label: "vs naive (MASE)", get: (r) => r.MASE },
    { label: "Runs high/low %", get: (r) => r["bias%"] },
    { label: "Band hit rate %", get: (r) => r["interval_cov%"] },
    { label: "Stayed in stock %", get: (r) => r["service_achieved%"] },
    { label: "Order size err %", get: (r) => r["order_err%"] },
  ];
  out += csvBlock("Overall", [{ label: "Scope", get: () => "ALL" }, ...accCols], [ov]);
  out += csvBlock("By product",
    [{ label: "Product", get: (r) => r.sku }, { label: "From file", get: (r) => r.source },
     { label: "Days of history", get: (r) => r.daysHistory }, { label: "Engine", get: (r) => r.engine },
     { label: "Enough windows to read?", get: (r) => (r.reportable === false ? "NO - too few" : "yes") },
     ...accCols], res.bySku);
  out += csvBlock("By engine", [{ label: "Engine", get: (r) => r.engine }, ...accCols], res.byEngine);
  out += csvBlock("By uploaded file",
    [{ label: "File", get: (r) => r.source }, { label: "Products", get: (r) => r.products }, ...accCols],
    res.bySource);
  out += csvBlock("Accuracy by history available",
    [{ label: "History", get: (r) => r.bucket }, ...accCols], res.byTrainLength);
  out += csvBlock("Recent vs historic windows",
    [{ label: "Window", get: (r) => r.block }, ...accCols], res.byBlock);

  out += csvBlock("Protection levels", [
    { label: "Level %", get: (t) => t.tier },
    { label: "Stayed in stock %", get: (t) => t.achievedService },
    { label: "95% CI low", get: (t) => t.achievedServiceCI?.[0] },
    { label: "95% CI high", get: (t) => t.achievedServiceCI?.[1] },
    { label: "P(cheapest) %", get: (t) => ta.ranking?.pCheapest?.[t.tier] ?? ta.ranking?.pCheapest?.[String(t.tier)] },
    { label: "Buffer units", get: (t) => t.safetyUnits },
    { label: "Missed units/yr", get: (t) => t.unitsShortYr },
    { label: "Lost profit $/yr", get: (t) => t.stockoutCost },
    { label: "Buffer cost $/yr", get: (t) => t.holdingCost },
    { label: "Total $/yr", get: (t) => t.totalCost },
    { label: "Cash in buffer $", get: (t) => t.bufferCash },
  ], ta.tiers);

  if (ta.mixedPolicy) {
    const mp = ta.mixedPolicy;
    const bu = Math.min(...(ta.tiers || []).map((t) => t.totalCost).filter((v) => v != null));
    out += `A level per product (out-of-sample)\n`;
    out += `Total $/yr,${mp.totalCost}\nStayed in stock %,${mp.achievedService ?? ""}\n`;
    const diff = bu - mp.totalCost;
    out += `${diff >= 0 ? "Beats" : "Loses to"} best single level by $/yr,${Math.abs(diff).toFixed(2)}\n`;
    out += `In use,${mp.totalCost < bu ? "yes - per-product levels applied" : "no - one level used for everything"}\n`;
    out += `Hindsight would have claimed $/yr,${mp.inSampleTotal}\n`;
    out += `Level spread,${csvCell(Object.entries(mp.tierCounts || {}).filter(([, n]) => n).map(([p, n]) => `${n} at ${p}%`).join(" "))}\n\n`;
  }

  const tiers = (ta.tiers || []).map((t) => t.tier);
  out += csvBlock("Per product, per level", [
    { label: "Product", get: (r) => r.sku },
    { label: "Cost known", get: (r) => (r.costKnown ? "yes" : "no") },
    { label: "Sells at/below cost", get: (r) => (r.lossMaking ? "yes" : "no") },
    { label: "Windows", get: (r) => r.windows },
    { label: "Its own best level %", get: (r) => r.bestTier },
    ...tiers.flatMap((t) => [
      { label: `${t}% missed units/yr`, get: (r) => r.tiers?.[t]?.unitsYr },
      { label: `${t}% buffer units`, get: (r) => r.tiers?.[t]?.safetyUnits },
      { label: `${t}% lost profit $/yr`, get: (r) => r.tiers?.[t]?.profitYr },
      { label: `${t}% total $/yr`, get: (r) => r.tiers?.[t]?.totalCostYr },
    ]),
  ], ta.bySku);

  out += csvBlock("Not tested", [
    { label: "Product", get: (r) => r.sku },
    { label: "Reason", get: (r) => r.reason },
    { label: "Days of history", get: (r) => r.days },
  ], res.skipped);
  return out;
};
const downloadCsv = (res) => {
  const stamp = new Date(res.ranAt ? res.ranAt * 1000 : Date.now()).toISOString().slice(0, 16).replace(/[:T]/g, "-");
  const blob = new Blob([buildCsv(res)], { type: "text/csv;charset=utf-8;" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `logitrack-backtest-${stamp}.csv`;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(a.href);
};

const fetchStatus = async (api) => {
  try { return await (await fetch(`${api}/api/backtest/status`)).json(); } catch { return null; }
};
const whenRan = (ts) => {
  if (!ts) return null;
  const mins = Math.round((Date.now() - ts * 1000) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"} ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hour${hrs === 1 ? "" : "s"} ago`;
  return new Date(ts * 1000).toLocaleDateString();
};
// A gap only counts as evidence if the target falls OUTSIDE the interval. Inside it,
// the honest read is "not enough windows to tell", not "the buffer is mis-sized".
const ciCovers = (ci, target) => Array.isArray(ci) && ci.length === 2 && target >= ci[0] && target <= ci[1];

export default function BacktestTab({ api = "http://localhost:8000", lm = false, apiKey, skuParams = {},
                                     holdingPct = 25, setHoldingPct = null,
                                     res = null, setRes = () => {}, diag = null, setDiag = () => {},
                                     waiting = [], waitingIds = [], onPickSku = null }) {
  // Per-SKU economics from the dashboard Scorecard: only SKUs with a real unit cost are
  // sent; the backtest costs those and skips the rest (no blanket margin guess).
  const buildCosts = () => {
    const out = {};
    for (const [sku, p] of Object.entries(skuParams || {})) {
      const c = Number(p?.unitCost);
      if (p?.unitCost != null && p.unitCost !== "" && Number.isFinite(c)) {
        out[sku] = { cost: c, fees: Number(p?.fees) || 0 };
      }
    }
    return out;
  };
  // `res` and `diag` are lifted to the parent so results persist across tab switches.
  const [viewMode, setView]   = useState("client");        // "client" | "analyst"
  const [showFormulas, setShowFormulas] = useState(false);
  const [showTierDetail, setShowTierDetail] = useState(false);
  const [openSections, setOpenSections] = useState({});
  const [diagLoading, setDiagLoading] = useState(false);
  // Only the two methodology knobs remain local. Cutoffs are sized per product, holding
  // comes from the Products page, lead/coverage come from each product.
  const [p, setP]             = useState({ horizon: 44, minTrain: 120 });
  const [advanced, setAdv]    = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState(null);

  const card  = lm ? "bg-white border-slate-200" : "bg-[#0d1117] border-slate-800";
  const muted = lm ? "text-slate-500" : "text-slate-500";
  const text  = lm ? "text-slate-900" : "text-white";
  const inp   = lm ? "bg-white border-slate-300 text-slate-900" : "bg-slate-900 border-slate-700 text-slate-200";
  const th    = `text-left text-[11px] uppercase tracking-widest font-bold ${muted} px-3 py-2`;
  const setNum = (k) => (e) => setP((s) => ({ ...s, [k]: e.target.value }));
  // header cell with a hover explanation (dotted underline signals it's hoverable)
  const Th = ({ tip, children }) => (
    <th className={th} title={tip}>
      <span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">{children}</span>
    </th>
  );

  // Collapsible section. The analyst view was a single wall of tables; this folds it
  // into three questions asked in the order you'd actually ask them, each showing a
  // one-line answer while collapsed so you can skip what you don't need.
  const Section = ({ id, title, summary, flag = false, defaultOpen = false, children }) => {
    const open = openSections[id] ?? defaultOpen;
    return (
      <div className={`${card} border rounded-2xl overflow-hidden ${flag ? (lm ? "border-amber-300" : "border-amber-900/50") : ""}`}>
        <button onClick={() => setOpenSections((s) => ({ ...s, [id]: !open }))}
          className={`w-full px-4 py-3 flex items-center gap-3 text-left transition-colors ${lm ? "hover:bg-slate-50" : "hover:bg-slate-800/40"}`}>
          <span className={`text-sm font-bold shrink-0 ${text}`}>{title}</span>
          {flag && <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded-full shrink-0 ${lm ? "bg-amber-100 text-amber-700" : "bg-amber-900/50 text-amber-300"}`}>check this</span>}
          <span className={`text-[11px] truncate ${muted}`}>{summary}</span>
          <span className={`ml-auto text-[11px] font-semibold shrink-0 ${lm ? "text-sky-600" : "text-sky-400"}`}>{open ? "Hide" : "Show"}</span>
        </button>
        {open && <div className={`border-t ${lm ? "border-slate-200" : "border-slate-800"} p-4 space-y-4`}>{children}</div>}
      </div>
    );
  };

  // Visible column guide. Sits under its table, closed by default so it doesn't add to
  // the clutter, but always one click away — unlike a hover tooltip.
  const Guide = ({ id, cols, extra = [] }) => {
    const open = openSections[`guide:${id}`] ?? false;
    const rows = [...cols.map((c) => COLDEF[c]).filter(Boolean), ...extra];
    if (!rows.length) return null;
    return (
      <div className={`border-t ${lm ? "border-slate-200" : "border-slate-800"}`}>
        <button onClick={() => setOpenSections((s) => ({ ...s, [`guide:${id}`]: !open }))}
          className={`w-full px-4 py-2 text-left text-[11px] font-semibold ${lm ? "text-sky-600 hover:bg-slate-50" : "text-sky-400 hover:bg-slate-800/40"} transition-colors`}>
          {open ? "Hide" : "What do these columns mean?"}
        </button>
        {open && (
          <dl className={`px-4 pb-3 space-y-2 border-t ${lm ? "border-slate-100" : "border-slate-800/60"} pt-3`}>
            {rows.map(([label, what, good]) => (
              <div key={label} className="grid grid-cols-1 sm:grid-cols-[130px_1fr] gap-x-3 gap-y-0.5">
                <dt className={`text-[11px] font-bold ${text}`}>{label}</dt>
                <dd className={`text-[11px] leading-relaxed ${muted}`}>
                  {what} <span className={lm ? "text-slate-700" : "text-slate-300"}><span className="font-semibold">Good looks like:</span> {good}</span>
                </dd>
              </div>
            ))}
          </dl>
        )}
      </div>
    );
  };

  // Filled by the IIFE at the top of the analyst view, read by the Section headers.
  let runQualityIssues = [], trustSummary = "", decisionSummary = "", accuracySummary = "";

  // A backtest runs automatically when you upload, so by the time you open this tab the
  // results usually already exist. Load them instead of showing an empty page — and if a
  // run is still going, wait for it rather than making the user press Run again.
  const [autoLoad, setAutoLoad] = useState(null);   // null | "loading" | "none" | "running"
  // Whether a NEWER run is in flight while results are already on screen. Without this
  // the client view could show a superseded run — e.g. one from before you changed a
  // lead time — with nothing to say so.
  const [jobRunning, setJobRunning] = useState(false);
  const wasRunning = useRef(false);
  // The poller runs on a 5s interval created once, so it can't close over `res` — a ref
  // keeps it looking at what is actually on screen rather than the value from mount.
  const resRef = useRef(res);
  useEffect(() => { resRef.current = res; }, [res]);
  useEffect(() => {
    let stop = false;
    const tick = async () => {
      const st = await fetchStatus(api);
      if (stop) return;
      const running = st?.status === "running";
      setJobRunning(running);
      // Pull whenever the SERVER's report is newer than what's on screen — not only when
      // a job finishes. A recost (holding rate, unit costs) re-prices the stored run
      // without starting a job at all, so watching job transitions alone left this tab
      // showing stale numbers indefinitely. And the holding control lives on this tab,
      // so that was every single time.
      const serverRanAt = Number(st?.lastRanAt || 0);
      const onScreenRanAt = Number(resRef.current?.ranAt || 0);
      if ((wasRunning.current && !running) || serverRanAt > onScreenRanAt) {
        try {
          const d = await (await fetch(`${api}/api/backtest/last`)).json();
          if (!stop && d?.available && d.result) setRes(d.result);
        } catch { /* leave what's on screen */ }
      }
      wasRunning.current = running;
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => { stop = true; clearInterval(id); };
  }, [api]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    let cancelled = false, timer = null;
    const pull = async () => {
      try {
        const r = await fetch(`${api}/api/backtest/last`);
        const d = await r.json();
        if (cancelled) return;
        // Take the server's report whenever it's NEWER than what's on screen. This used
        // to bail out if any result was already loaded — but `res` lives in the parent
        // and survives tab switches, so a run that finished while you were on another
        // tab left the previous upload's results showing indefinitely.
        if (d?.available && d.result) {
          if ((d.result.ranAt || 0) > (res?.ranAt || 0)) setRes(d.result);
          setAutoLoad(null);
          return;
        }
        if (res) { setAutoLoad(null); return; }
        if (d?.job?.status === "running") { setAutoLoad("running"); timer = setTimeout(pull, 4000); return; }
        setAutoLoad("none");
      } catch { if (!cancelled) setAutoLoad("none"); }
    };
    setAutoLoad("loading"); pull();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [api, jobRunning]);   // re-check when a run starts or finishes, not just on mount

  // One data path in: the Products page. This just re-tests whatever's loaded.
  const run = async () => {
    setError(null); setDiag(null); setLoading(true);
    try {
      const r = await fetch(`${api}/api/backtest/catalog`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ horizon: p.horizon, minTrain: p.minTrain, costs: buildCosts() }),
      });
      const out = await r.json();
      if (!r.ok) throw new Error(out?.detail || `Backtest failed (${r.status})`);
      setRes(out);
    } catch (e) {
      setError(e?.message || "Something went wrong.");
    } finally {
      setLoading(false);
    }
  };

  const runDiagnosis = async () => {
    if (!res) return;
    setDiagLoading(true); setDiag(null);
    const prompt =
`You are an expert demand-forecasting analyst interpreting a rolling-origin backtest (no leakage) for an inventory tool. Turn these numbers into a clear, honest read: separate REAL problems from measurement ARTIFACTS and give the single right lever for each. Use ONLY the numbers provided — never invent figures.

HOW THE TOOL ACTUALLY WORKS (map symptoms → causes):
• Safety buffer = z × σ, where σ is the MEASURED out-of-sample lead-window error from a multi-window "dress rehearsal" (refit on holdouts), floored at the textbook σ_daily×√lead and capped at 3×. So low service is usually NOT "bands too narrow from in-sample optimism" — it's more often a ONE-SIDED risk the symmetric buffer can't price (a SKU trending UP keeps under-shooting forward windows) or genuinely volatile/spiky demand.
• Forecast = level × weekly/seasonal shape (± a damped trend). Yearly seasonality turns on automatically once a SKU trains on ≥450 days — read daysHistory, never guess.
• Lifecycle: a SKU with <7 days of own history is in "baseline" (no forecast yet); 7–27 days is "young/provisional" (wider bands); >180 idle days is "inactive" (forecast forced to 0).

METRIC MEANINGS: MASE<1 beats seasonal-naive (>1 = worse than naive). bias% signed over(+)/under(−). interval_cov% target ≈80 (lower = bands too narrow). service_achieved% should ≈ the target service (lower = the buffer didn't cover actual lead-time demand). order_err% = order too big(+)/small(−). WAPE is INFLATED on sparse/low-volume SKUs and means little there.

HARD RULES — do not violate:
1. Read each SKU's daysHistory and engine before commenting; never claim a SKU is short on history without checking. daysHistory ≥ 450 ⇒ yearly seasonality already ON.
2. High WAPE / MASE>1 on a Croston (intermittent) SKU is EXPECTED — a measurement artifact of mostly-zero days, NOT a reason to switch engines. Croston is correct for intermittent demand; Prophet is worse. Say "ignore the WAPE here," not "fix it."
3. A SKU with EXCELLENT accuracy (low WAPE, MASE<1, bias≈0, coverage≈80) but LOW service does NOT have a forecast problem. Its service gap is a buffer/trend issue — recommend a higher protection tier (more safety stock), or note it's a growth-trend SKU under-buffered on the upside. Do NOT tell the user to "improve the forecast."
4. Over-ordering (+order_err) AND low service TOGETHER is the fingerprint of VOLATILE demand (spikes beat the buffer near-term while the forecast runs high on average). Explain it as volatility, not a contradiction — you can't fix both by changing the order size.
5. Few "forecasts" (cutoffs) ⇒ noisy numbers. Any SKU with reportable=false (under ${res.params?.minWindowsReportable ?? 4} windows) must be called out as not yet trustworthy, never acted on.
6a. tierAnalysis.criticalRatio.optimalService is the service level that MINIMISES cost under the user's own margins and holding rate — a continuous optimum. tierAnalysis.nearestTier is the tier whose ACHIEVED service lands closest to it. Because tiers are TARGETS and the buffer under-delivers, recommend on ACHIEVED, not on the tier label. If nearestTier and bestTier disagree, say so and explain that bestTier ranks four discrete points while criticalRatio describes the underlying curve.
6b. tierAnalysis.ranking.pCheapest gives P(each tier is cheapest) over product resamples. If ranking.decisive is false, you MUST say the tier choice is too close to call and refuse to pick one on cost alone — point to the optimal-service line, working capital, or risk appetite instead.
6c. capacity tells you how many test windows the data supports. If capacity.cappedSkus or capacity.thinSkus is non-empty, note that those products returned fewer windows than requested and their individual numbers are under-powered.
6. CONFIDENCE INTERVALS OVERRIDE POINT ESTIMATES. Fields ending _ci are 95% intervals from resampling PRODUCTS (windows overlap in time and repeat per product, so the raw forecast count overstates the evidence). If the service target falls INSIDE service_achieved%_ci, you must say the run cannot distinguish the buffer from correctly sized — do NOT recommend a tier change off that gap. Same for MASE_ci straddling 1.0: that is "indistinguishable from naive", not "beats naive".
7. Use byTrainLength before blaming the model. If accuracy improves markedly with history, the honest read is "not enough history yet", not "the model is weak" — recommend waiting, not tuning.
8. Use byBlock to spot regime change: recent much worse than historic ⇒ something changed in the business recently. Historic much worse ⇒ old data reflects a business that no longer exists and is dragging the average down.
9. If a "baseline" block is present it re-ran the SAME cutoffs with one engine forced on every SKU. Compare MASE. If the router isn't clearly better, say so plainly — do not defend the routing. A baseline with failed cutoffs means that engine doesn't fit every SKU, which is evidence FOR routing.
10. If failedCutoffs > 0, open by stating that some windows failed and the results are therefore partial.

RESULTS (target service ${res.params?.service}%):
${JSON.stringify({ overall: res.overall, byEngine: res.byEngine, bySku: res.bySku,
                  byTrainLength: res.byTrainLength, byBlock: res.byBlock,
                  baseline: res.baseline ? { route: res.baseline.route, overall: res.baseline.overall, failedCutoffs: res.baseline.failedCutoffs } : null,
                  failedCutoffs: res.failedCutoffs,
                  capacity: res.capacity ? { requested: res.capacity.requested, catalogMax: res.capacity.catalogMax,
                                             bindingSku: res.capacity.bindingSku, cappedSkus: res.capacity.cappedSkus,
                                             thinSkus: res.capacity.thinSkus } : null,
                  criticalRatio: res.tierAnalysis?.criticalRatio,
                  nearestTier: res.tierAnalysis?.nearestTier,
                  bestTier: res.tierAnalysis?.bestTier,
                  ranking: res.tierAnalysis?.ranking,
                  perSkuBestTier: (res.tierAnalysis?.bySku || []).map((s) => ({ sku: s.sku, bestTier: s.bestTier })),
                  tierCalibration: (res.tierAnalysis?.tiers || []).map((t) => ({ tier: t.tier, achieved: t.achievedService, ci: t.achievedServiceCI })) }, null, 1)}

Write a short, plain-English read in concise "• " bullet lines (no markdown headers). Cover the fleet overall, then only the SKUs that genuinely stand out. For each, say whether it's a REAL issue or an ARTIFACT, the likely cause in the tool's terms, and the ONE concrete lever (raise protection tier / accept the sparse-demand metric / needs more history / growth-trend under-buffer). Lead with what actually needs action, and explicitly tell the user which scary-looking numbers to IGNORE.`;
    try {
      const r = await fetch(GROQ_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
        body: JSON.stringify({ model: GROQ_MODEL, messages: [{ role: "user", content: prompt }], max_tokens: 700, temperature: 0.2 }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d?.error?.message || `AI error ${r.status}`);
      setDiag(d?.choices?.[0]?.message?.content || "No diagnosis returned.");
    } catch (e) {
      setDiag("Couldn't run the diagnosis: " + (e?.message || "") + (apiKey ? "" : " (no AI key configured)."));
    } finally {
      setDiagLoading(false);
    }
  };

  // Rows built from too few windows are dimmed and badged rather than hidden — the
  // number still exists, it just shouldn't be read with the same confidence.
  const Row = ({ r, name }) => (
    <tr className={`${lm ? "border-t border-slate-100" : "border-t border-slate-800/60"} ${r.reportable === false ? "opacity-50" : ""}`}>
      <td className={`px-3 py-2 text-xs font-semibold ${text}`}>
        {name}
        {r.reportable === false && (
          <span title={`Only ${r.windows} test window${r.windows === 1 ? "" : "s"} — too few to read as a result.`}
            className={`ml-1.5 text-[10px] font-semibold px-1.5 py-0.5 rounded-full cursor-help ${lm ? "bg-slate-100 text-slate-500" : "bg-slate-800 text-slate-400"}`}>
            {r.windows}w · thin
          </span>
        )}
      </td>
      <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{r.forecasts}</td>
      <td className={`px-3 py-2 text-xs tabular-nums ${muted}`} title={r["WAPE%_unweighted"] != null ? `Volume-weighted. Unweighted (mean of per-window ratios): ${r["WAPE%_unweighted"]}%` : undefined}>{fmt(r["WAPE%"], "%")}</td>
      <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlMASE(r.MASE))}`}>{fmt(r.MASE)}</td>
      <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlBias(r["bias%"]))}`}>{r["bias%"] == null ? "—" : `${r["bias%"] > 0 ? "+" : ""}${r["bias%"]}%`}</td>
      <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlCov(r["interval_cov%"]))}`}>{fmt(r["interval_cov%"], "%")}</td>
      <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlSvc(r["service_achieved%"], res?.params?.service ?? 95))}`}>{fmt(r["service_achieved%"], "%")}</td>
      <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlOrd(r["order_err%"]))}`}>{r["order_err%"] == null ? "—" : `${r["order_err%"] > 0 ? "+" : ""}${r["order_err%"]}%`}</td>
    </tr>
  );

  // Plain-English column names; the technical term lives in the hover text so analysts
  // still get MASE/WAPE without everyone else having to decode them.
  const Head = () => (
    <thead><tr>
      <Th tip={TIP.name}>Name</Th><Th tip={TIP.forecasts}>Tests</Th><Th tip={TIP.wape}>Avg miss</Th>
      <Th tip={TIP.mase}>vs naive</Th><Th tip={TIP.bias}>Runs high/low</Th><Th tip={TIP.cov}>Band hit rate</Th>
      <Th tip={TIP.service}>Stayed in stock</Th><Th tip={TIP.order}>Order size</Th>
    </tr></thead>
  );

  return (
    <div className={`flex-1 overflow-y-auto p-4 sm:p-6 space-y-5 ${lm ? "text-slate-900" : "text-slate-100"}`}>
      {/* Products without a settled lead time are skipped, not blocked — the same way the
          engine already skips products with no unit cost. Say which, and why. */}
      {waiting.length > 0 && (
        <div className={`rounded-xl border px-3.5 py-2.5 ${lm ? "bg-amber-50 border-amber-200" : "bg-amber-950/20 border-amber-900/40"}`}>
          <div className={`text-xs font-semibold ${lm ? "text-amber-800" : "text-amber-300"}`}>
            {waiting.length} product{waiting.length === 1 ? "" : "s"} not tested yet — no lead time set
          </div>
          <div className={`text-[11px] mt-0.5 ${lm ? "text-amber-700" : "text-amber-300/80"}`}>
            Testing against the default would look like evidence when it is a guess. Set a lead time — on the
            product, or once on its supplier — and {waiting.length === 1 ? "it joins" : "they join"} the next run
            automatically. Everything else has already been tested.
          </div>
          <div className="flex flex-wrap gap-1.5 mt-2">
            {waiting.map((name, i) => (
              <button key={`${name}-${i}`} onClick={() => onPickSku && onPickSku(waitingIds[i])}
                className={`text-[10px] font-mono px-2 py-0.5 rounded-md border transition-all ${lm ? "bg-white border-amber-300 text-amber-800 hover:border-amber-500" : "bg-slate-900 border-amber-900/50 text-amber-300 hover:border-amber-700"}`}>
                {name}
              </button>
            ))}
          </div>
        </div>
      )}
      <div>
        <h2 className="text-lg font-bold tracking-tight">Forecast Backtest</h2>
        <p className={`text-[11px] ${muted} mt-1 max-w-3xl`}>
          How your forecasts and reorder suggestions would have performed on your own sales history — re-run at
          dozens of past dates and graded against what actually sold next. Measured, not projected.
          It tests whatever is loaded on the Products page and updates itself; products without enough history are listed as untested.
        </p>
      </div>

      {/* No source picker, no per-run parameters: data comes in on the Products page and
          everything that shapes a run — cutoffs, holding rate, each product's lead time
          and coverage — is set where it belongs and applied automatically. What's left
          here are two methodology knobs most people never touch. */}
      <div className={`${card} border rounded-2xl p-4 space-y-3`}>
        <div className="flex items-center gap-3 flex-wrap">
          <button onClick={run} disabled={loading}
            className="px-4 py-2 rounded-lg text-xs font-bold bg-sky-600 hover:bg-sky-500 disabled:opacity-60 text-white transition-all">
            {loading ? "Testing… (refits every product — give it a few minutes)" : "Re-test now"}
          </button>
          <span className={`text-[11px] ${muted}`}>
            Runs on its own whenever you load data or change a cost, lead time or coverage window.
          </span>
          <button onClick={() => setAdv((v) => !v)} className={`ml-auto text-[11px] ${muted} hover:underline`}>
            {advanced ? "Hide method settings" : "Method settings"}
          </button>
        </div>
        {advanced && (
          <div className={`grid grid-cols-2 sm:grid-cols-4 gap-3 pt-1 border-t ${lm ? "border-slate-200" : "border-slate-800"}`}>
            {[["horizon", "Forecast horizon (d)"], ["minTrain", "Min history (d)"]].map(([k, lab]) => (
              <div key={k}>
                <label title={PTIP[k]} className={`block text-[10px] uppercase tracking-widest ${muted} mb-1 mt-2 cursor-help`}>{lab}</label>
                <input type="number" value={p[k]} onChange={setNum(k)} className={`w-full rounded-lg border px-2 py-1.5 text-xs ${inp}`} />
              </div>
            ))}
            <div className={`col-span-2 text-[11px] ${muted} mt-2 leading-relaxed`}>
              Changing either of these changes what a &ldquo;test&rdquo; means, so press <span className="font-semibold">Re-test now</span> afterwards.
              Everything else is automatic: test count is sized per product, holding rate comes from the Products page, and lead time and coverage come from each product.
            </div>
          </div>
        )}
        {error && <div className={`text-[11px] rounded-lg px-3 py-2 border ${lm ? "text-rose-700 bg-rose-50 border-rose-200" : "text-rose-400 bg-rose-950/20 border-rose-900/30"}`}>{error}</div>}
      </div>


      {/* No results yet: say which of the three reasons it is, rather than nothing. */}
      {!res && !loading && autoLoad === "running" && (
        <div className={`${card} border rounded-2xl p-4 flex items-center gap-3`}>
          <svg className={`h-4 w-4 animate-spin ${lm ? "text-sky-600" : "text-sky-400"}`} fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z" />
          </svg>
          <div className={`text-[12px] ${text}`}>
            <div className="font-semibold">A test is running on your products.</div>
            <div className={muted}>Results will appear here automatically — usually a few minutes. No need to press anything.</div>
          </div>
        </div>
      )}
      {!res && !loading && autoLoad === "none" && (
        <div className={`${card} border rounded-2xl p-4 text-[12px] ${muted}`}>
          <span className={`font-semibold ${text}`}>No results yet.</span> A test runs automatically when you load
          products — if nothing is loaded, import a file on the <span className="font-semibold">Products</span> page first.
        </div>
      )}

      {/* Results */}
      {res && (
        <div className="space-y-4">
          {/* View toggle + clear */}
          {/* Controls on one line, description on the next — the long "ran at" text used
              to push Download CSV onto a wrapped row at the far right, where it hid. */}
          <div className="space-y-1.5">
            <div className="flex items-center gap-2 flex-wrap">
              <div className={`inline-flex rounded-lg border overflow-hidden ${lm ? "border-slate-300" : "border-slate-700"}`}>
                {[["client", "Client view"], ["analyst", "Analyst view"]].map(([v, lab]) => (
                  <button key={v} onClick={() => setView(v)}
                    className={`px-3 py-1.5 text-xs font-semibold transition-all ${viewMode === v
                      ? (lm ? "bg-sky-600 text-white" : "bg-sky-500 text-white")
                      : (lm ? "bg-white text-slate-600 hover:bg-slate-50" : "bg-slate-900 text-slate-300 hover:bg-slate-800")}`}>
                    {lab}
                  </button>
                ))}
              </div>
              <button onClick={() => downloadCsv(res)} title="Every table on this page, as one sectioned CSV."
                className={`flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-lg border transition-all ${lm ? "bg-white text-sky-700 border-sky-300 hover:bg-sky-50" : "bg-slate-900 text-sky-300 border-sky-800 hover:bg-sky-950/40"}`}>
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" /></svg>
                Download CSV
              </button>
              <button onClick={() => { setRes(null); setDiag(null); }}
                className={`ml-auto flex items-center gap-1 text-[11px] font-semibold px-2.5 py-1 rounded-lg border transition-all ${lm ? "text-rose-600 border-rose-200 hover:bg-rose-50" : "text-rose-400 border-rose-900/40 hover:bg-rose-950/30"}`}>
                <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
                Clear results
              </button>
            </div>
            <div className={`text-[11px] ${muted}`}>
              {viewMode === "client" ? "Plain-language summary — safe to share." : "Full metrics, formulas & per-SKU detail."}
              {res.ranAt && <> · ran {new Date(res.ranAt * 1000).toLocaleString()}{res.trigger === "upload" ? " (automatically, on upload)" : ""}</>}
            </div>
          </div>

          {/* ───────────────── CLIENT VIEW ───────────────── */}
          {viewMode === "client" && (() => {
            const ov = res.overall || {};
            const ta = res.tierAnalysis || {};
            const money = (v) => v == null ? "—" : `$${Math.round(v).toLocaleString()}`;
            const bestRow = (ta.tiers || []).find((t) => t.tier === ta.bestTier);
            const svc = bestRow ? bestRow.achievedService : ov["service_achieved%"];
            const oe = ov["order_err%"];
            const orderText = oe == null ? "no order data" : Math.abs(oe) < 8 ? "right-sized on average" : `ran ${Math.abs(Math.round(oe))}% too ${oe > 0 ? "high" : "low"}`;
            const accText = ov.MASE == null ? "—" : ov.MASE < 1 ? "Solid" : ov.MASE < 1.3 ? "Fair" : "Rough";
            const accSub = ov.MASE == null ? "" : ov.MASE < 1 ? "beats a naive last-season guess" : ov.MASE < 1.3 ? "about as good as a naive guess" : "below a naive guess — see per-product detail";
            const costNote = !ta.priced ? ""
              : ta.costBasis === "actual" ? "Based on your real per-unit cost and fees."
              : `Based on the ${ta.costedSkus} of ${ta.totalSkus} tested products that have a cost entered — add cost for the rest to include them.`;
            return (
              <div className="space-y-4">
                <div className={`${card} border rounded-2xl p-4`}>
                  <h3 className={`text-sm font-bold ${text}`}>How these reorder suggestions would have performed on your own sales history</h3>
                  <p className={`text-[11px] ${muted} mt-1`}>
                    Tested across {res.forecasts} simulated past reorder points on {res.tested} product{res.tested !== 1 ? "s" : ""}, each graded against what actually sold next — measured, not projected.
                  </p>
                  {/* Freshness. This is the shareable view, so it must never quietly
                      present a superseded run as current. */}
                  <p className={`text-[11px] mt-1 ${jobRunning ? (lm ? "text-amber-600" : "text-amber-400") : muted}`}>
                    {res.ranAt ? <>Test run {whenRan(res.ranAt)}{res.trigger === "upload" ? ", automatically when you uploaded" : ""}.</> : null}
                    {jobRunning && <> <span className="font-semibold">A newer test is running now</span> — these figures may change when it finishes.</>}
                  </p>
                  <div className="flex flex-wrap gap-3 mt-3">
                    <div className={`flex-1 min-w-[150px] rounded-xl border p-4 ${lm ? "bg-white border-slate-200" : "bg-[#0d1117] border-slate-800"}`}>
                      <div className={`text-[10px] uppercase tracking-widest font-bold ${muted} mb-1`}>Stayed in stock</div>
                      <div className={`text-2xl font-bold tabular-nums ${text}`}>{svc == null ? "—" : `${svc}%`}</div>
                      <div className={`text-[11px] ${muted} mt-0.5`}>{bestRow ? `at the recommended ${ta.bestTier}% level` : "of past reorder cycles"}</div>
                      {/* Show the range, not just the headline — a single number here reads as
                          far more precise than a few dozen overlapping test windows support. */}
                      {ciText(bestRow?.achievedServiceCI ?? ov["service_achieved%_ci"], "%") && (
                        <div className={`text-[10px] ${muted} mt-1`} title={TIP.ci}>
                          likely between {ciText(bestRow?.achievedServiceCI ?? ov["service_achieved%_ci"], "%")}
                        </div>
                      )}
                    </div>
                    <div className={`flex-1 min-w-[150px] rounded-xl border p-4 ${lm ? "bg-white border-slate-200" : "bg-[#0d1117] border-slate-800"}`}>
                      <div className={`text-[10px] uppercase tracking-widest font-bold ${muted} mb-1`}>Order sizing</div>
                      <div className={`text-2xl font-bold tabular-nums ${text}`}>{oe == null ? "—" : Math.abs(oe) < 8 ? "On point" : `${oe > 0 ? "+" : ""}${Math.round(oe)}%`}</div>
                      <div className={`text-[11px] ${muted} mt-0.5`}>{orderText}</div>
                    </div>
                    <div className={`flex-1 min-w-[150px] rounded-xl border p-4 ${lm ? "bg-white border-slate-200" : "bg-[#0d1117] border-slate-800"}`}>
                      <div className={`text-[10px] uppercase tracking-widest font-bold ${muted} mb-1`}>Forecast accuracy</div>
                      <div className={`text-2xl font-bold ${text}`}>{accText}</div>
                      <div className={`text-[11px] ${muted} mt-0.5`}>{accSub}</div>
                    </div>
                  </div>
                </div>

                {ta.priced && ta.bestTier && (
                  <div className={`rounded-2xl border p-4 ${lm ? "bg-emerald-50 border-emerald-200" : "bg-emerald-950/20 border-emerald-900/40"}`}>
                    {/* The client view is the one you'd share, so it must not sound more
                        certain than the analyst view. When the cost ranking is a coin flip
                        we say so and fall back to the reason that IS decisive. */}
                    {(() => {
                      const rk = ta.ranking, cr = ta.criticalRatio;
                      const tied = rk && !rk.decisive;
                      const shown = tied && ta.nearestTier != null ? ta.nearestTier : ta.bestTier;
                      const shownRow = (ta.tiers || []).find((t) => t.tier === shown) || bestRow;
                      const other = tied ? (rk.winner === shown ? rk.runnerUp : rk.winner) : null;
                      // When the per-product mix beats every single level, the mix IS the
                      // recommendation and this banner must lead with it — otherwise it
                      // contradicts the table right below.
                      const mp = ta.mixedPolicy;
                      const bestUniformCost = Math.min(...(ta.tiers || []).map((t) => t.totalCost).filter((v) => v != null));
                      // One verdict, stated as fact — the app deploys it automatically,
                      // so this is "what's in use", not a suggestion to weigh up.
                      if (mp?.totalCost != null && mp.totalCost < bestUniformCost) {
                        return (
                          <>
                            <div className={`text-sm font-bold ${lm ? "text-emerald-800" : "text-emerald-300"}`}>
                              In use: a level per product ({Object.entries(mp.tierCounts || {}).filter(([, n]) => n).map(([p, n]) => `${n} at ${p}%`).join(", ")})
                            </div>
                            <p className={`text-[12px] mt-1 leading-relaxed ${lm ? "text-emerald-700" : "text-emerald-300/80"}`}>
                              Each product carries the level its own numbers call for. Tested against one level
                              for everything: saves {money(bestUniformCost - mp.totalCost)}/yr. Applied automatically.
                            </p>
                          </>
                        );
                      }
                      return (
                        <>
                          <div className={`text-sm font-bold ${lm ? "text-emerald-800" : "text-emerald-300"}`}>In use: {shown}% on every product</div>
                          <p className={`text-[12px] mt-1 leading-relaxed ${lm ? "text-emerald-700" : "text-emerald-300/80"}`}>
                            {tied
                              ? <>Cheapest overall — {other}% costs about the same, but {shown}% lands closer to your optimal in-stock rate ({cr ? `${cr.optimalService}%` : "—"}).</>
                              : <>Cheapest overall at about {money(shownRow?.totalCost)}/yr.</>}
                            {mp?.totalCost != null && <> A custom level per product was also tested — it didn&apos;t beat this, so one level is used everywhere.</>}
                            {" "}Applied automatically.
                          </p>
                        </>
                      );
                    })()}
                  </div>
                )}

                {ta.priced && ta.tiers?.length > 0 && (
                  <div className={`${card} border rounded-2xl overflow-hidden`}>
                    <div className={`px-4 py-2 text-[11px] uppercase tracking-widest font-bold ${muted} border-b ${lm ? "border-slate-200" : "border-slate-800"}`}>Protection levels compared</div>
                    <table className="w-full">
                      <thead><tr>
                        <Th tip="Service-level target — a higher level keeps more spare stock on hand.">Level</Th>
                        <Th tip="How often the buffer would have prevented a stockout in past reorder cycles.">Stayed in stock</Th>
                        <Th tip="Projected yearly cost = profit lost on sales you'd miss + cost of holding the safety buffer. Lowest wins.">Est. cost / yr</Th>
                      </tr></thead>
                      <tbody>
                        {(() => {
                          // When a per-product mix exists and wins, IT is the recommendation —
                          // the single-level rows become the comparison, not the pick.
                          const mp = ta.mixedPolicy;
                          const bestUniformCost = Math.min(...ta.tiers.map((t) => t.totalCost).filter((v) => v != null));
                          const mixWins = mp?.totalCost != null && mp.totalCost < bestUniformCost;
                          return (
                            <>
                              {ta.tiers.map((t) => {
                                const isBest = !mixWins && t.tier === ta.bestTier;
                                return (
                                  <tr key={t.tier} className={`${lm ? "border-t border-slate-100" : "border-t border-slate-800/60"} ${isBest ? (lm ? "bg-emerald-50" : "bg-emerald-950/20") : ""}`}>
                                    <td className={`px-3 py-2 text-xs font-bold ${text}`}>Everything at {t.tier}%{isBest && <span className={`ml-2 text-[10px] font-semibold px-1.5 py-0.5 rounded-full ${lm ? "bg-emerald-100 text-emerald-700" : "bg-emerald-900/50 text-emerald-300"}`}>in use</span>}</td>
                                    <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlSvc(t.achievedService, t.tier))}`}>{t.achievedService}%</td>
                                    <td className={`px-3 py-2 text-xs font-bold tabular-nums ${isBest ? tone(lm, "good") : text}`}>{money(t.totalCost)}</td>
                                  </tr>
                                );
                              })}
                              {mp?.totalCost != null && (
                                <tr className={`border-t-2 ${lm ? "border-slate-300" : "border-slate-700"} ${mixWins ? (lm ? "bg-emerald-50" : "bg-emerald-950/20") : ""}`}>
                                  <td className={`px-3 py-2 text-xs font-bold ${text}`}
                                      title="Each product set to its own best level rather than one level for everything. Costed fairly: every simulated reorder is scored with a level chosen before seeing that reorder's outcome.">
                                    A level per product{mixWins && <span className={`ml-2 text-[10px] font-semibold px-1.5 py-0.5 rounded-full ${lm ? "bg-emerald-100 text-emerald-700" : "bg-emerald-900/50 text-emerald-300"}`}>in use</span>}
                                    <div className={`text-[10px] font-normal ${muted}`}>
                                      {Object.entries(mp.tierCounts || {}).filter(([, n]) => n).map(([p, n]) => `${n} at ${p}%`).join(" · ")}
                                    </div>
                                  </td>
                                  <td className={`px-3 py-2 text-xs font-bold tabular-nums ${text}`}>{mp.achievedService != null ? `${mp.achievedService}%` : "—"}</td>
                                  <td className={`px-3 py-2 text-xs font-bold tabular-nums ${mixWins ? tone(lm, "good") : text}`}>
                                    {money(mp.totalCost)}
                                    {mixWins && <span className={`ml-1.5 text-[10px] font-semibold ${lm ? "text-emerald-700" : "text-emerald-300"}`}>saves {money(bestUniformCost - mp.totalCost)}/yr</span>}
                                  </td>
                                </tr>
                              )}
                            </>
                          );
                        })()}
                      </tbody>
                    </table>
                    {ta.mixedPolicy?.totalCost != null && (
                      <div className={`px-4 py-2 text-[11px] leading-relaxed ${muted} border-t ${lm ? "border-slate-200" : "border-slate-800"}`}>
                        <span className="font-semibold">A level per product</span> = each product on its own best level instead of one shared level. The row marked <span className="font-semibold">in use</span> is applied automatically.
                      </div>
                    )}
                  </div>
                )}
                {/* Money totals are sums over costed products only. Missing one doesn't
                    just omit its row — it moves the totals and can flip which level wins. */}
                {ta.priced && ta.uncostedSkus?.length > 0 && (
                  <div className={`text-[12px] rounded-xl border px-4 py-3 ${lm ? "bg-amber-50 border-amber-200 text-amber-800" : "bg-amber-950/15 border-amber-900/30 text-amber-300"}`}>
                    <span className="font-semibold">These costs are incomplete — the totals above will change.</span>{" "}
                    {ta.costedSkus} of {ta.totalSkus} tested products have a unit cost. The yearly figures add up only
                    those {ta.costedSkus}, so adding the rest will move every total and may change which level wins.
                    <div className="mt-1.5">
                      <span className="font-semibold">Missing a cost:</span>{" "}
                      <span className="font-mono">{ta.uncostedSkus.slice(0, 12).join(", ")}{ta.uncostedSkus.length > 12 ? ` +${ta.uncostedSkus.length - 12} more` : ""}</span>
                    </div>
                    <div className={`mt-1.5 ${lm ? "text-amber-700" : "text-amber-400/90"}`}>
                      Add them in the Scorecard and the test re-runs by itself — accuracy results below are unaffected, they don&apos;t depend on cost.
                    </div>
                  </div>
                )}
                {!ta.priced && (
                  <div className={`text-[12px] rounded-xl border px-4 py-3 ${lm ? "bg-amber-50 border-amber-200 text-amber-800" : "bg-amber-950/15 border-amber-900/30 text-amber-300"}`}>
                    <span className="font-semibold">No unit costs yet, so there are no dollar figures.</span> The test still ran and its
                    accuracy results are valid — those don&apos;t depend on cost. What&apos;s missing is the money comparison:
                    without a cost there&apos;s no way to weigh a lost sale against the cost of holding stock. Add costs in the
                    Scorecard (or include a <span className="font-semibold">Cost</span> column in your file) and the test re-runs
                    by itself. Until then the table shows how often each level kept you in stock.
                  </div>
                )}
              </div>
            );
          })()}

          {/* ───────────────── ANALYST VIEW ───────────────── */}
          {viewMode === "analyst" && (<>
          {/* Three questions, in order: can I trust it → what do I do → how accurate is it.
              Everything below folds away; only the decision is open by default. */}
          {(() => {
            const ov = res.overall || {}, cap = res.capacity || {}, ta = res.tierAnalysis || {};
            const issues = [];
            if (res.failedCutoffs > 0) issues.push(`${res.failedCutoffs} window${res.failedCutoffs === 1 ? "" : "s"} failed`);
            if (cap.cappedSkus?.length) issues.push(`${cap.cappedSkus.length} product${cap.cappedSkus.length === 1 ? "" : "s"} under-tested`);
            if (ov["interval_cov%"] != null && (ov["interval_cov%"] < 75 || ov["interval_cov%"] > 88)) issues.push("bands miscalibrated");
            if ((res.bySku || []).some((r) => r.reportable === false)) issues.push("some products too thin to read");
            runQualityIssues = issues;
            trustSummary = issues.length
              ? `${res.forecasts} tests on ${res.tested} products · ${issues.join(" · ")}`
              : `${res.forecasts} tests on ${res.tested} products · nothing flagged`;
            const nt = (ta.tiers || []).find((t) => t.tier === (ta.nearestTier ?? ta.bestTier));
            const _mp = ta.mixedPolicy;
            const _bu = Math.min(...(ta.tiers || []).map((t) => t.totalCost).filter((v) => v != null));
            const mixInUse = _mp?.totalCost != null && _mp.totalCost < _bu;
            decisionSummary = mixInUse
              ? `In use: a level per product — saves ${"$" + Math.round(_bu - _mp.totalCost).toLocaleString()}/yr vs one level`
              : nt
              ? `In use: ${nt.tier}% on every product — stays in stock ${nt.achievedService}% of the time`
              : "Add unit costs in the Scorecard to get a recommendation";
            accuracySummary = ov.MASE == null ? "—"
              : `${ov.MASE < 1 ? "Beats" : "Loses to"} a naive forecast (${ov.MASE}) · misses by ${ov["WAPE%"]}% on average`;
            return null;
          })()}

          <Section id="decision" title="What should I do?" summary={decisionSummary} defaultOpen>
            {res.tierAnalysis?.tiers?.length > 0 && (() => {
              const ta = res.tierAnalysis;
              const money = (v) => v == null ? "—" : `$${Math.round(v).toLocaleString()}`;
              const best = ta.bestTier;
              const cyc = ta.cyclesPerYear;
              const basisNote = !ta.priced ? "no per-SKU cost — calibration only"
                : ta.costBasis === "actual" ? `real cost · ${ta.totalSkus} tested SKUs`
                : `real cost · ${ta.costedSkus}/${ta.totalSkus} tested SKUs — totals incomplete`;
              const lostProfitTip = `Total profit you'd forgo to stockouts per year at this tier, across the catalog. The shortfall is a counterfactual per tier — at each cutoff we re-pretend you'd ordered to forecast + this tier's buffer and count the demand it wouldn't cover over the lead time — NOT the empty-shelf events in your history (those are excluded). Computed PER PRODUCT (each product's units short/yr × its own profit, price − cost − fees) then summed — see the per-product breakdown below. Costed SKUs only.`;
              return (
                <div className={`${card} border rounded-2xl overflow-hidden`}>
                  <div className={`px-4 py-2 flex items-center justify-between border-b ${lm ? "border-slate-200" : "border-slate-800"}`}>
                    <span className={`text-[11px] uppercase tracking-widest font-bold ${muted}`}>Protection tiers — calibration & annual cost</span>
                    <span className={`text-[10px] ${muted}`}>{basisNote} · {ta.assumptions.holdingPct}%/yr holding · {cyc} cycles/yr</span>
                  </div>

                  {/* The continuous optimum — the tiers are just dial settings around it */}
                  {ta.criticalRatio && (() => {
                    const cr = ta.criticalRatio, nt = (ta.tiers || []).find((t) => t.tier === ta.nearestTier);
                    const disagree = ta.nearestTier != null && ta.bestTier != null && ta.nearestTier !== ta.bestTier;
                    return (
                      <div className={`px-4 py-3 border-b text-[12px] leading-relaxed ${lm ? "border-slate-200 bg-sky-50/60 text-slate-700" : "border-slate-800 bg-sky-950/15 text-slate-300"}`}>
                        <span className="font-semibold" title={TIP.optimal}>Optimal in-stock rate from your margins: ~{cr.optimalService}%.</span>
                        {nt && <> The {nt.tier}% tier lands closest (achieves {nt.achievedService}%).</>}
                        {" "}<span title={TIP.achievedVsTarget}>Judge tiers on <span className="font-semibold">Achieved</span>, not their label.</span>
                        {disagree && <> <span className="font-semibold">Note:</span> cheapest says {ta.bestTier}%, closest-to-optimal says {ta.nearestTier}% — prefer {ta.nearestTier}% unless the cost gap below is decisive.</>}
                      </div>
                    );
                  })()}

                  {/* Is the ranking real, or a coin flip? */}
                  {ta.ranking && !ta.ranking.decisive && (
                    <div className={`px-4 py-3 border-b text-[12px] leading-relaxed ${lm ? "border-slate-200 bg-amber-50 text-amber-800" : "border-slate-800 bg-amber-950/15 text-amber-300"}`}>
                      <span className="font-semibold">Cost tie:</span> {ta.ranking.winner}% and {ta.ranking.runnerUp}% are within noise of each other ({ta.ranking.winnerProb}% vs {ta.ranking.runnerUpProb}% of resamples) — the pick falls back to closest-to-optimal.
                    </div>
                  )}
                  <table className="w-full">
                    <thead><tr>
                      <Th tip="The service-level target. A higher tier carries more safety stock.">Tier</Th>
                      <Th tip="Share of lead-time windows the tier actually covered, averaged per product first so a long-history product doesn't outvote a short one. Should land ≈ the tier %. Well below = the buffer is under-sized at that tier (a calibration issue), not proof the tier is wrong.">Achieved</Th>
                      <Th tip={TIP.ci}>95% CI</Th>
                      <Th tip="Achieved service minus the tier's target. Negative = under-protecting — but only if the target falls outside the confidence interval.">vs target</Th>
                      {ta.ranking && <Th tip={TIP.pcheapest}>P(cheapest)</Th>}
                      <Th tip="Spare units you'd carry across the catalog at this level. This is the quantity the buffer cost is built from: buffer units x unit cost x holding rate.">Buffer units</Th>
                      <Th tip="Units of demand per year you'd fail to cover at this level, across the catalog. This is the quantity the lost profit is built from: missed units x profit per unit.">Missed units / yr</Th>
                      <Th tip={lostProfitTip}>Lost profit / yr</Th>
                      <Th tip="The yearly cost of carrying the safety buffer = Cash in buffer × your holding rate. Rises with the tier because more safety stock costs more to hold (tied-up capital + storage + obsolescence).">Buffer cost / yr</Th>
                      <Th tip="Lost profit/yr + Buffer cost/yr. The lowest total is the economically right tier — this is the profit decision.">Total $/yr</Th>
                      <th className={`${th} border-l ${lm ? "border-slate-200" : "border-slate-800"}`} title="Working capital parked in the safety buffer at this tier (safety units × cost). A ONE-TIME amount you tie up — like a till float — NOT a yearly cost (its yearly cost is the Buffer cost column). The sub-line is the extra/less cash vs the recommended tier. It is NOT added to Total.">
                        <span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">Cash in buffer</span>
                      </th>
                    </tr></thead>
                    <tbody>
                      {ta.tiers.map((t) => {
                        const isBest = t.tier === best;
                        const rowBg = isBest ? (lm ? "bg-emerald-50" : "bg-emerald-950/20") : "";
                        return (
                          <tr key={t.tier} className={`${lm ? "border-t border-slate-100" : "border-t border-slate-800/60"} ${rowBg}`}>
                            <td className={`px-3 py-2 text-xs font-bold tabular-nums ${text}`}>
                              {t.tier}%{isBest && <span className={`ml-2 text-[10px] font-semibold px-1.5 py-0.5 rounded-full ${lm ? "bg-emerald-100 text-emerald-700" : "bg-emerald-900/50 text-emerald-300"}`}>lowest cost</span>}
                            </td>
                            <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlSvc(t.achievedService, t.tier))}`}>{t.achievedService}%</td>
                            <td className={`px-3 py-2 text-[11px] tabular-nums ${muted}`}>{ciText(t.achievedServiceCI, "%") || "—"}</td>
                            {/* An apparent gap is only evidence when the tier's own target falls
                                outside the interval; otherwise it reads as "too close to call". */}
                            <td className={`px-3 py-2 text-xs tabular-nums ${ciCovers(t.achievedServiceCI, t.tier) ? muted : t.gap < -7 ? tone(lm, "bad") : t.gap < 0 ? tone(lm, "warn") : tone(lm, "good")}`}
                                title={ciCovers(t.achievedServiceCI, t.tier) ? "The tier's target falls inside the confidence interval — not enough windows to call this a real gap." : undefined}>
                              {t.gap > 0 ? "+" : ""}{t.gap}
                              {ciCovers(t.achievedServiceCI, t.tier) && <span className={`ml-1 text-[10px] ${muted}`}>n/s</span>}
                            </td>
                            {ta.ranking && (() => {
                              const pc = ta.ranking.pCheapest?.[t.tier] ?? ta.ranking.pCheapest?.[String(t.tier)];
                              return (
                                <td className={`px-3 py-2 text-xs font-bold tabular-nums ${pc == null ? muted : pc >= 60 ? tone(lm, "good") : pc >= 25 ? tone(lm, "warn") : muted}`}>
                                  {pc == null ? "—" : `${pc}%`}
                                </td>
                              );
                            })()}
                            <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{t.safetyUnits == null ? "—" : Math.round(t.safetyUnits).toLocaleString()}</td>
                            <td className={`px-3 py-2 text-xs font-semibold tabular-nums ${text}`}>{t.unitsShortYr == null ? "—" : Math.round(t.unitsShortYr).toLocaleString()}</td>
                            <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{money(t.stockoutCost)}</td>
                            <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{money(t.holdingCost)}</td>
                            <td className={`px-3 py-2 text-xs font-bold tabular-nums ${isBest ? tone(lm, "good") : text}`}>{money(t.totalCost)}</td>
                            <td className={`px-3 py-2 tabular-nums align-top border-l ${lm ? "border-slate-200" : "border-slate-800"}`}>
                              <div className={`text-xs font-semibold ${text}`}>{money(t.bufferCash)}</div>
                              <div className={`text-[10px] ${muted}`}>{isBest ? "baseline" : t.bufferCashDelta == null ? "" : `${t.bufferCashDelta > 0 ? "+" : "−"}$${Math.round(Math.abs(t.bufferCashDelta)).toLocaleString()} vs ${best}%`}</div>
                            </td>
                          </tr>
                        );
                      })}
                      {/* The policy the app actually recommends: a level per product.
                          None of the four rows above is that policy, so without this the
                          table compares only options you wouldn't choose. */}
                      {ta.mixedPolicy && (() => {
                        const mp = ta.mixedPolicy;
                        const bestUniform = Math.min(...(ta.tiers || []).map((t) => t.totalCost).filter((v) => v != null));
                        const saves = bestUniform - mp.totalCost;
                        const spread = Object.entries(mp.tierCounts || {}).filter(([, n]) => n)
                          .map(([p, n]) => `${n}×${p}%`).join(", ");
                        if (saves <= 0) {
                          // The mix lost its test, so it isn't deployed — one quiet line,
                          // not a full row competing for attention with the real answer.
                          return (
                            <tr className={`border-t ${lm ? "border-slate-200" : "border-slate-800"}`}>
                              <td colSpan={ta.ranking ? 11 : 10} className={`px-3 py-2 text-[11px] ${muted}`}
                                  title="Each product's own best level was tried and scored on unseen weeks. It cost more than the best single level, so one level is used for everything.">
                                A custom level per product was tested ({spread}) — {money(mp.totalCost)}/yr, no better than the best single level. One level is in use for everything.
                              </td>
                            </tr>
                          );
                        }
                        return (
                          <tr className={`border-t-2 ${lm ? "border-slate-300 bg-emerald-50" : "border-slate-700 bg-emerald-950/20"}`}>
                            <td className={`px-3 py-2.5 text-xs font-bold ${text}`}>
                              Per-product mix
                              <span className={`ml-2 text-[10px] font-semibold px-1.5 py-0.5 rounded-full ${lm ? "bg-emerald-100 text-emerald-700" : "bg-emerald-900/50 text-emerald-300"}`}>in use</span>
                              <div className={`text-[10px] font-normal mt-0.5 ${muted}`}>{spread}</div>
                            </td>
                            <td className={`px-3 py-2.5 text-xs ${muted}`} colSpan={ta.ranking ? 6 : 5}>
                              Each product at its own best level.
                            </td>
                            <td className={`px-3 py-2.5 text-xs font-bold tabular-nums ${tone(lm, "good")}`}>{money(mp.totalCost)}</td>
                            <td className={`px-3 py-2.5 text-[11px] tabular-nums ${muted}`} colSpan={2}>
                              saves {money(saves)}/yr
                            </td>
                          </tr>
                        );
                      })()}
                    </tbody>
                  </table>

                  <div className={`px-4 py-2 text-[11px] ${muted} border-t ${lm ? "border-slate-200" : "border-slate-800"}`}>
                    Dollars here are just units × a rate; judge tiers by lowest <span className="font-semibold">Total $/yr</span>. Details under the guide below.
                  </div>
                  <Guide id="tiers" cols={["tier","achieved","ci","vstarget","pcheap","bufunits","missunits","lostprofit","buffercost","totalcost","cash"]}
                    extra={[
                      ...(ta.mixedPolicy ? [["Per-product mix", "Every product on its own cheapest level instead of one shared level. Scored fairly: each simulated reorder is graded with a level chosen from that product's OTHER test windows, never the one being graded — otherwise the mix would win automatically by keeping every product's lucky result. Hindsight scoring would have claimed " + money(ta.mixedPolicy.inSampleTotal) + "/yr.", "If it beats the best single level, the app deploys the per-product levels; if not, everyone gets the winning single level. Applied automatically either way."]] : []),
                      ["How the dollars are built", "Lost profit/yr = missed units/yr × profit per unit (price − cost − fees), summed per product. Buffer cost/yr = buffer units × unit cost × your holding rate. The shortfall is a simulation per tier — would forecast + that tier's buffer have covered the next lead-time's real demand — not the stockouts in your history.", "Costs cover only the safety-stock policy, the part the tier changes."],
                      ["Cash in buffer vs Buffer cost", "Cash in buffer is one-time working capital parked in safety stock — not a yearly cost and not added to Total. Its yearly cost is already the Buffer cost column (cash × holding rate).", "Use it to judge affordability, not to rank tiers."],
                    ]} />
                </div>
              );
            })()}

            {res.tierAnalysis?.lossMakingSkus?.length > 0 && (
              <div className={`text-[12px] rounded-xl border px-4 py-3 ${lm ? "bg-amber-50 border-amber-200 text-amber-800" : "bg-amber-950/15 border-amber-900/30 text-amber-300"}`}>
                <span className="font-semibold">Priced at or below cost:</span> {res.tierAnalysis.lossMakingSkus.join(", ")}. These lose money on every sale, so a stockout forgoes no profit and more safety stock would only lose money faster — they're <span className="font-semibold">excluded from the cost comparison</span>. This is a pricing/cost issue, not an inventory one: review the price or cost in the Scorecard.
              </div>
            )}

            {/* Per-product breakdown — full traceability */}
            {res.tierAnalysis?.bySku?.length > 0 && (() => {
              const ta = res.tierAnalysis;
              const tiers = ta.tiers.map((t) => t.tier);
              const best = ta.bestTier;
              const money = (v) => v == null ? "—" : `$${Math.round(v).toLocaleString()}`;
              const thc = `text-right text-[11px] uppercase tracking-widest font-bold ${muted} px-3 py-2`;
              const colHL = (t) => t === best ? (lm ? "bg-emerald-50" : "bg-emerald-950/20") : "";
              return (
                <div className={`${card} border rounded-2xl overflow-hidden`}>
                  <button onClick={() => setShowTierDetail((v) => !v)}
                    className={`w-full px-4 py-2 flex items-center justify-between ${lm ? "hover:bg-slate-50" : "hover:bg-slate-800/40"} transition-colors`}>
                    <span className={`text-[11px] uppercase tracking-widest font-bold ${muted}`}>Per-product breakdown</span>
                    <span className={`text-[11px] font-semibold ${lm ? "text-sky-600" : "text-sky-400"}`}>{showTierDetail ? "Hide" : "Show"} · {ta.bySku.length} products</span>
                  </button>
                  {showTierDetail && (
                    <div className={`border-t ${lm ? "border-slate-200" : "border-slate-800"}`}>
                      {/* Per-product recommended tier — one catalog-wide tier leaves money on
                          the table whenever margins differ between products. */}
                      {(() => {
                        const withTier = ta.bySku.filter((s) => s.bestTier != null);
                        if (!withTier.length) return null;
                        const differing = withTier.filter((s) => s.bestTier !== best);
                        const crBySku = ta.criticalRatio?.bySku || {};
                        return (
                          <>
                            <div className={`px-4 pt-3 pb-1.5 text-xs uppercase tracking-widest font-bold ${text}`}>
                              Recommended tier per product
                              <span className={`ml-2 font-normal normal-case tracking-normal ${muted}`}>
                                — the catalog-wide pick is {best}%; {differing.length === 0 ? "every product agrees" : `${differing.length} product${differing.length === 1 ? "" : "s"} would prefer a different one`}
                              </span>
                            </div>
                            <table className="w-full">
                              <thead><tr>
                                <th className={th} title="The product this row is about."><span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">Product</span></th>
                                <th className={thc} title="The protection level that costs this product the least, from its own margin and demand swing."><span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">Its own best tier</span></th>
                                <th className={thc} title={TIP.optimal}><span className="underline decoration-dotted underline-offset-4 cursor-help">Optimal service</span></th>
                                <th className={thc} title="Yearly lost profit + buffer cost for this product at its own best level."><span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">Cost at its tier</span></th>
                                <th className={thc} title={`The same yearly cost if this product is held at the catalog-wide ${best}% instead.`}><span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">Cost at {best}%</span></th>
                                <th className={thc} title="What it costs this product per year to sit at the catalog-wide level instead of its own. Sum the column to price a single setting for everything."><span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">Penalty for one-size-fits-all</span></th>
                              </tr></thead>
                              <tbody>
                                {withTier.map((s) => {
                                  const own = s.tiers[s.bestTier]?.totalCostYr;
                                  const cat = s.tiers[best]?.totalCostYr;
                                  const pen = own != null && cat != null ? cat - own : null;
                                  const off = s.bestTier !== best;
                                  return (
                                    <tr key={s.sku} className={lm ? "border-t border-slate-100" : "border-t border-slate-800/60"}>
                                      <td className={`px-3 py-2 text-sm font-semibold ${text}`}>{s.sku}</td>
                                      <td className={`px-3 py-2 text-sm font-bold tabular-nums text-right ${off ? tone(lm, "warn") : text}`}>{s.bestTier}%</td>
                                      <td className={`px-3 py-2 text-sm tabular-nums text-right ${muted}`}>{crBySku[s.sku] != null ? `${crBySku[s.sku]}%` : "—"}</td>
                                      <td className={`px-3 py-2 text-sm tabular-nums text-right ${muted}`}>{money(own)}</td>
                                      <td className={`px-3 py-2 text-sm tabular-nums text-right ${muted}`}>{money(cat)}</td>
                                      <td className={`px-3 py-2 text-sm font-bold tabular-nums text-right ${!pen ? muted : tone(lm, "warn")}`}>{pen ? money(pen) : "—"}</td>
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                            <Guide id="persku" cols={[]} extra={[
                              ["Its own best tier", "The protection level that costs this product the least, worked out from its own margin and demand swing.", "Products with similar margins should mostly agree."],
                              ["Optimal service", "The stay-in-stock rate where one more spare unit stops paying for itself, for this product.", "The tier whose achieved rate is nearest this is the right one for it."],
                              ["Cost at its tier", "Yearly lost profit plus buffer cost at that product's own best level.", "Lower is better — it's the floor for this product."],
                              ["Penalty", "Extra yearly cost of holding this product at the catalog-wide level instead of its own.", "Near $0 means one setting for everything is fine. A large total is the case for per-product levels."],
                            ]} />
                            <div className={`px-4 py-2 text-[11px] leading-relaxed ${muted}`}>
                              Each product&apos;s own cheapest tier, from its own margin and demand volatility. The last column is what it costs that product to be held at the catalog-wide {best}% instead — sum it to see the price of a single setting for everything. Products whose margins are similar will mostly agree; a wide spread here is the argument for per-product protection levels.
                            </div>
                          </>
                        );
                      })()}
                      {/* Missed units / yr by product */}
                      <div className={`px-4 pt-4 pb-1.5 text-xs uppercase tracking-widest font-bold ${text}`}>Missed units / yr</div>
                      <table className="w-full">
                        <thead><tr>
                          <th className={th} title="The product this row is about."><span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">Product</span></th>
                          {tiers.map((t) => <th key={t} className={`${thc} ${colHL(t)}`} title={`This product if you ran it at the ${t}% protection level.`}><span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">{t}%</span></th>)}
                        </tr></thead>
                        <tbody>
                          {ta.bySku.map((s) => (
                            <tr key={s.sku} className={lm ? "border-t border-slate-100" : "border-t border-slate-800/60"}>
                              <td className={`px-3 py-2 text-sm font-semibold ${text}`}>{s.sku}{!s.costKnown && <span className={`ml-1.5 text-[10px] ${muted}`}>(no cost)</span>}{s.lossMaking && <span className={`ml-1.5 text-[10px] font-semibold ${lm ? "text-amber-600" : "text-amber-400"}`}>≤ cost</span>}</td>
                              {tiers.map((t) => { const v = s.tiers[t]?.unitsYr; return (
                                <td key={t} className={`px-3 py-2 text-sm tabular-nums text-right ${!v ? muted : `font-bold ${text}`} ${colHL(t)}`}>{s.tiers[t] ? v.toLocaleString() : "—"}</td>
                              ); })}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      {/* Buffer units by product — the quantity behind the buffer cost */}
                      <div className={`px-4 pt-4 pb-1.5 text-xs uppercase tracking-widest font-bold ${text}`}>
                        Buffer units carried
                        <span className={`font-normal normal-case tracking-normal ${muted}`}> — spare stock held per product; × unit cost × holding rate = buffer cost</span>
                      </div>
                      <table className="w-full">
                        <thead><tr>
                          <Th tip="The product this row is about.">Product</Th>
                          {tiers.map((t) => <th key={t} className={`${thc} ${colHL(t)}`} title={`Buffer units this product would carry at the ${t}% level.`}><span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">{t}%</span></th>)}
                        </tr></thead>
                        <tbody>
                          {ta.bySku.map((s) => (
                            <tr key={s.sku} className={lm ? "border-t border-slate-100" : "border-t border-slate-800/60"}>
                              <td className={`px-3 py-2 text-sm font-semibold ${text}`}>{s.sku}</td>
                              {tiers.map((t) => { const v = s.tiers[t]?.safetyUnits; return (
                                <td key={t} className={`px-3 py-2 text-sm tabular-nums text-right ${!v ? muted : `font-bold ${text}`} ${colHL(t)}`}>{v == null ? "—" : Math.round(v).toLocaleString()}</td>
                              ); })}
                            </tr>
                          ))}
                          <tr className={`${lm ? "border-t-2 border-slate-300 bg-slate-50" : "border-t-2 border-slate-700 bg-slate-800/30"}`}>
                            <td className={`px-3 py-2.5 text-sm font-bold ${text}`}>Total units</td>
                            {ta.tiers.map((t) => <td key={t.tier} className={`px-3 py-2.5 text-base font-bold tabular-nums text-right ${text} ${colHL(t.tier)}`}>{t.safetyUnits == null ? "—" : Math.round(t.safetyUnits).toLocaleString()}</td>)}
                          </tr>
                        </tbody>
                      </table>
                      {/* Lost profit / yr by product + TOTAL */}
                      <div className={`px-4 pt-4 pb-1.5 text-xs uppercase tracking-widest font-bold ${text}`}>Lost profit / yr</div>
                      <table className="w-full">
                        <thead><tr>
                          <th className={th} title="The product this row is about."><span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">Product</span></th>
                          {tiers.map((t) => <th key={t} className={`${thc} ${colHL(t)}`} title={`This product if you ran it at the ${t}% protection level.`}><span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">{t}%</span></th>)}
                        </tr></thead>
                        <tbody>
                          {ta.bySku.map((s) => (
                            <tr key={s.sku} className={lm ? "border-t border-slate-100" : "border-t border-slate-800/60"}>
                              <td className={`px-3 py-2 text-sm font-semibold ${text}`}>{s.sku}{s.lossMaking && <span className={`ml-1.5 text-[10px] font-semibold ${lm ? "text-amber-600" : "text-amber-400"}`}>≤ cost</span>}</td>
                              {tiers.map((t) => { const v = s.tiers[t]?.profitYr; return (
                                <td key={t} className={`px-3 py-2 text-sm tabular-nums text-right ${!v ? muted : `font-bold ${text}`} ${colHL(t)}`}>{money(v)}</td>
                              ); })}
                            </tr>
                          ))}
                          <tr className={`${lm ? "border-t-2 border-slate-300 bg-slate-50" : "border-t-2 border-slate-700 bg-slate-800/30"}`}>
                            <td className={`px-3 py-2.5 text-sm font-bold ${text}`}>Total / yr</td>
                            {ta.tiers.map((t) => <td key={t.tier} className={`px-3 py-2.5 text-base font-bold tabular-nums text-right ${t.tier === best ? tone(lm, "good") : text} ${colHL(t.tier)}`}>{money(t.stockoutCost)}</td>)}
                          </tr>
                        </tbody>
                      </table>
                      {/* Cash in buffer by product + TOTAL */}
                      <div className={`px-4 pt-4 pb-1.5 text-xs uppercase tracking-widest font-bold ${text}`}>Cash in buffer <span className={`font-normal normal-case tracking-normal ${muted}`}>— one-time working capital, not a yearly cost</span></div>
                      <table className="w-full">
                        <thead><tr>
                          <th className={th} title="The product this row is about."><span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">Product</span></th>
                          {tiers.map((t) => <th key={t} className={`${thc} ${colHL(t)}`} title={`This product if you ran it at the ${t}% protection level.`}><span className="underline decoration-dotted decoration-slate-500/60 underline-offset-4 cursor-help">{t}%</span></th>)}
                        </tr></thead>
                        <tbody>
                          {ta.bySku.map((s) => (
                            <tr key={s.sku} className={lm ? "border-t border-slate-100" : "border-t border-slate-800/60"}>
                              <td className={`px-3 py-2 text-sm font-semibold ${text}`}>{s.sku}</td>
                              {tiers.map((t) => { const v = s.tiers[t]?.bufferCash; return (
                                <td key={t} className={`px-3 py-2 text-sm tabular-nums text-right ${!v ? muted : `font-bold ${text}`} ${colHL(t)}`}>{money(v)}</td>
                              ); })}
                            </tr>
                          ))}
                          <tr className={`${lm ? "border-t-2 border-slate-300 bg-slate-50" : "border-t-2 border-slate-700 bg-slate-800/30"}`}>
                            <td className={`px-3 py-2.5 text-sm font-bold ${text}`}>Total</td>
                            {ta.tiers.map((t) => <td key={t.tier} className={`px-3 py-2.5 text-base font-bold tabular-nums text-right ${text} ${colHL(t.tier)}`}>{money(t.bufferCash)}</td>)}
                          </tr>
                        </tbody>
                      </table>
                      <div className={`px-4 py-2 text-[11px] ${muted}`}>Sorted by exposure. <span className="font-semibold">Lost profit</span> Total/yr matches the tier table above. <span className="font-semibold">Cash in buffer</span> is the working capital each product ties up at each tier — a one-time amount, not added to the cost; its yearly cost already sits in Buffer cost. The {best ? `${best}%` : "recommended"} column is highlighted.</div>
                    </div>
                  )}
                </div>
              );
            })()}

          </Section>

          <Section id="trust" title="Can I trust this run?" summary={trustSummary}
                   flag={runQualityIssues.length > 0} defaultOpen={runQualityIssues.length > 0}>
            {/* Failed cutoffs — surfaced, never silently dropped */}
            {res.failedCutoffs > 0 && (() => {
              const errs = (res.skipped || []).filter((s) => s.kind === "error");
              return (
                <div className={`text-[12px] rounded-xl border px-4 py-3 ${lm ? "bg-rose-50 border-rose-200 text-rose-800" : "bg-rose-950/15 border-rose-900/30 text-rose-300"}`}>
                  <span className="font-semibold">{res.failedCutoffs} test window{res.failedCutoffs === 1 ? "" : "s"} failed to run</span> and {res.failedCutoffs === 1 ? "was" : "were"} excluded from every number below — so these results cover less than the full run.
                  <ul className="mt-1.5 space-y-0.5 font-mono text-[11px] opacity-90">
                    {errs.slice(0, 5).map((s, i) => <li key={i}>{s.sku} @ {s.cutoff}: {s.reason}</li>)}
                  </ul>
                  {errs.length > 5 && <div className="mt-1 text-[11px] opacity-80">…and {errs.length - 5} more.</div>}
                </div>
              );
            })()}

            {/* Cutoff capacity — what the data can actually support */}
            {res.capacity && (() => {
              const c = res.capacity;
              const over = c.cappedSkus?.length > 0, thin = c.thinSkus?.length > 0;
              const tn = over || thin ? (lm ? "bg-amber-50 border-amber-200 text-amber-800" : "bg-amber-950/15 border-amber-900/30 text-amber-300")
                                      : (lm ? "bg-slate-50 border-slate-200 text-slate-600" : "bg-slate-900/40 border-slate-800 text-slate-400");
              return (
                <div className={`text-[12px] rounded-xl border px-4 py-3 ${tn}`} title={TIP.capacity}>
                  {c.auto ? (
                    <>
                      <span className="font-semibold">Test size chosen automatically per product:</span>{" "}
                      <span className="font-semibold">{c.windowsUsed}</span> test windows in total
                      {c.usedRange && c.usedRange[0] !== c.usedRange[1] && <> ({c.usedRange[0]}–{c.usedRange[1]} each, according to how much history it has)</>}.
                      Every product gets as many windows as its own history genuinely supports — one per {c.horizon}-day
                      forecast horizon, so no two windows grade the same days twice.
                      {c.untestableSkus > 0 && <> {c.untestableSkus} product{c.untestableSkus === 1 ? "" : "s"} can&apos;t be tested at all yet.</>}
                      {thin && <> {c.thinSkus.length} will land under {res.params?.minWindowsReportable ?? 4} windows and won&apos;t be individually readable — that needs ≥{c.reportableNeedsDays}d of history.</>}
                    </>
                  ) : (
                    <>
                      <span className="font-semibold">Test-window capacity:</span> you asked for {c.requested} per product;
                      your data supports up to <span className="font-semibold">{c.catalogMax}</span> before the shortest-history
                      product runs out{c.bindingSku ? <> (<span className="font-mono">{c.bindingSku}</span>, {c.bindingDays}d)</> : null}.
                      {c.untestableSkus > 0 && <> {c.untestableSkus} product{c.untestableSkus === 1 ? "" : "s"} can&apos;t be tested at all.</>}
                      {over && <> <span className="font-semibold">{c.cappedSkus.length} can&apos;t supply {c.requested}</span> and will return fewer: <span className="font-mono">{c.cappedSkus.slice(0, 6).join(", ")}{c.cappedSkus.length > 6 ? " …" : ""}</span>.</>}
                      {thin && <> {c.thinSkus.length} will land under {res.params?.minWindowsReportable ?? 4} windows and won&apos;t be individually readable.</>}
                    </>
                  )}
                </div>
              );
            })()}

            {/* Confidence — how much the headline numbers can actually carry */}
            {(res.overall?.["service_achieved%_ci"] || res.overall?.MASE_ci) && (() => {
              const ov = res.overall, target = res.params?.service ?? 95;
              const svcCI = ov["service_achieved%_ci"], inconclusive = ciCovers(svcCI, target);
              return (
                <div className={`${card} border rounded-2xl p-4`}>
                  <div className={`text-[11px] uppercase tracking-widest font-bold ${muted} mb-2`}>How much these numbers can carry</div>
                  <div className="flex flex-wrap gap-x-6 gap-y-2">
                    {[["Stayed in stock", ov["service_achieved%"], svcCI, "%"], ["vs naive forecast", ov.MASE, ov.MASE_ci, ""],
                      ["Band hit rate", ov["interval_cov%"], ov["interval_cov%_ci"], "%"]].map(([lab, v, ci, sfx]) => (
                      <div key={lab}>
                        <div className={`text-[10px] uppercase tracking-widest font-bold ${muted}`}>{lab}</div>
                        <div className={`text-sm font-bold tabular-nums ${text}`}>{fmt(v, sfx)}</div>
                        <div className={`text-[11px] ${muted}`} title={TIP.ci}>
                          <span className="underline decoration-dotted underline-offset-2 cursor-help">95% CI</span> {ciText(ci, sfx) || "—"}
                        </div>
                      </div>
                    ))}
                  </div>
                  <p className={`text-[11px] ${muted} mt-3 leading-relaxed`}>
                    Intervals come from resampling <span className="font-semibold">products</span>, not windows — test windows overlap in time and repeat per product, so the raw
                    count of {res.forecasts} forecasts overstates how much independent evidence you have.
                    {inconclusive
                      ? <> Your {target}% target sits <span className="font-semibold">inside</span> the service interval, so this run can&apos;t tell you the buffer is mis-sized — you need more history or more products, not a tier change.</>
                      : <> Your {target}% target sits <span className="font-semibold">outside</span> the service interval, so the gap is real and worth acting on.</>}
                    {" "}Products from one catalog share seasonality, so even this runs slightly optimistic.
                  </p>
                </div>
              );
            })()}

            {/* Skipped — short history and hard failures are different things */}
            {res.skipped?.length > 0 && (() => {
              const shortOnes = res.skipped.filter((s) => s.kind !== "error");
              return shortOnes.length > 0 ? (
                <div className={`text-[11px] ${muted}`}>
                  Skipped (too little history to backtest): {shortOnes.map((s) => `${s.sku} (${s.days}d)`).join(" · ")}
                </div>
              ) : null;
            })()}
          </Section>

          <Section id="accuracy" title="How accurate is the forecast?" summary={accuracySummary}>
            <div className={`text-[11px] ${muted} leading-relaxed`}>
              Tested {res.tested}/{res.skus} SKUs · {res.forecasts} forecasts.{" "}
              Hover any column heading for what it means. In short: <span className="font-semibold">vs naive</span> under 1 is good,{" "}
              <span className="font-semibold">runs high/low</span> near 0 is unbiased, <span className="font-semibold">band hit rate</span> should sit near 80%.{" "}
              <span className="font-semibold">Avg miss</span> is naturally high on slow sellers — lean on <span className="font-semibold">vs naive</span> there.
            </div>

            <div className={`${card} border rounded-2xl overflow-hidden`}>
              <div className={`px-4 py-2 text-[11px] uppercase tracking-widest font-bold ${muted} border-b ${lm ? "border-slate-200" : "border-slate-800"}`}>By engine</div>
              <table className="w-full"><Head /><tbody>
                {res.byEngine.map((r) => <Row key={r.engine} r={r} name={r.engine} />)}
                {res.overall?.forecasts ? <Row r={res.overall} name="ALL (overall)" /> : null}
              </tbody></table>
              <Guide id="engine" cols={["tests","avgmiss","vsnaive","runs","bandhit","instock","ordersize"]} />
            </div>

            {/* Per-upload sections. One catalog for modelling, separate reporting. */}
          {res.bySource?.length > 1 && (
            <div className={`${card} border rounded-2xl overflow-hidden`}>
              <div className={`px-4 py-2 text-[11px] uppercase tracking-widest font-bold ${muted} border-b ${lm ? "border-slate-200" : "border-slate-800"}`}>
                By uploaded file
              </div>
              <table className="w-full">
                <thead><tr>
                  <Th tip="The file these products came from. Products are forecast and tested as one catalog — this splits the results back out by upload.">File</Th>
                  <Th tip="How many products this file contributed.">Products</Th>
                  <Th tip={TIP.forecasts}>Tests</Th><Th tip={TIP.mase}>vs naive</Th>
                  <Th tip={TIP.wape}>Avg miss</Th><Th tip={TIP.bias}>Runs high/low</Th>
                  <Th tip={TIP.cov}>Band hit rate</Th><Th tip={TIP.service}>Stayed in stock</Th>
                </tr></thead>
                <tbody>
                  {res.bySource.map((b) => (
                    <tr key={b.source} className={lm ? "border-t border-slate-100" : "border-t border-slate-800/60"}>
                      <td className={`px-3 py-2 text-xs font-semibold ${text}`}><span className="font-mono">{b.source}</span></td>
                      <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{b.products}</td>
                      <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{b.forecasts}</td>
                      <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlMASE(b.MASE))}`}>{fmt(b.MASE)}</td>
                      <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{fmt(b["WAPE%"], "%")}</td>
                      <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlBias(b["bias%"]))}`}>{b["bias%"] == null ? "—" : `${b["bias%"] > 0 ? "+" : ""}${b["bias%"]}%`}</td>
                      <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlCov(b["interval_cov%"]))}`}>{fmt(b["interval_cov%"], "%")}</td>
                      <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlSvc(b["service_achieved%"], res?.params?.service ?? 95))}`}>{fmt(b["service_achieved%"], "%")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className={`px-4 py-2 text-[11px] leading-relaxed ${muted} border-t ${lm ? "border-slate-200" : "border-slate-800"}`}>
                Products from every file are modelled together — a product can borrow seasonal shape from a peer in a different upload, which is usually what you want. This table splits the <span className="font-semibold">results</span> back out so one upload&apos;s performance doesn&apos;t hide inside another&apos;s.
              </div>
            </div>
          )}

          {res.bySku?.length > 0 && (
              <div className={`${card} border rounded-2xl overflow-hidden`}>
                <div className={`px-4 py-2 text-[11px] uppercase tracking-widest font-bold ${muted} border-b ${lm ? "border-slate-200" : "border-slate-800"}`}>By SKU</div>
                <table className="w-full"><Head />
                  <tbody>
                    {(() => {
                      // Group rows under a per-file subheader when more than one upload is
                      // loaded; a flat list otherwise.
                      const srcs = [...new Set(res.bySku.map((r) => r.source).filter(Boolean))];
                      if (srcs.length < 2) return res.bySku.map((r) => <Row key={r.sku} r={r} name={r.sku} />);
                      return srcs.map((src) => (
                        <React.Fragment key={src}>
                          <tr className={lm ? "bg-slate-50" : "bg-slate-800/40"}>
                            <td colSpan={8} className={`px-3 py-1.5 text-[10px] uppercase tracking-widest font-bold ${muted}`}>
                              From <span className="font-mono normal-case tracking-normal">{src}</span>
                            </td>
                          </tr>
                          {res.bySku.filter((r) => r.source === src).map((r) => <Row key={r.sku} r={r} name={r.sku} />)}
                        </React.Fragment>
                      ));
                    })()}
                  </tbody>
                </table>
                <Guide id="sku" cols={["tests","avgmiss","vsnaive","runs","bandhit","instock","ordersize"]} />
              </div>
            )}

            {/* Accuracy by how much history the model actually had */}
            {res.byTrainLength?.length > 1 && (
              <div className={`${card} border rounded-2xl overflow-hidden`}>
                <div className={`px-4 py-2 text-[11px] uppercase tracking-widest font-bold ${muted} border-b ${lm ? "border-slate-200" : "border-slate-800"}`}>
                  Accuracy by history available at forecast time
                </div>
                <table className="w-full">
                  <thead><tr>
                    <Th tip={TIP.train}>History</Th><Th tip={TIP.forecasts}>Tests</Th><Th tip={TIP.mase}>vs naive</Th>
                    <Th tip={TIP.wape}>Avg miss</Th><Th tip={TIP.bias}>Runs high/low</Th><Th tip={TIP.cov}>Band hit rate</Th>
                  </tr></thead>
                  <tbody>
                    {res.byTrainLength.map((b) => (
                      <tr key={b.bucket} className={lm ? "border-t border-slate-100" : "border-t border-slate-800/60"}>
                        <td className={`px-3 py-2 text-xs font-semibold ${text}`}>{b.bucket}</td>
                        <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{b.forecasts}</td>
                        <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlMASE(b.MASE))}`}>{fmt(b.MASE)}</td>
                        <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{fmt(b["WAPE%"], "%")}</td>
                        <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlBias(b["bias%"]))}`}>{b["bias%"] == null ? "—" : `${b["bias%"] > 0 ? "+" : ""}${b["bias%"]}%`}</td>
                        <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlCov(b["interval_cov%"]))}`}>{fmt(b["interval_cov%"], "%")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <Guide id="train" cols={["history","tests","vsnaive","avgmiss","runs","bandhit"]} />
                <div className={`px-4 py-2 text-[11px] leading-relaxed ${muted} border-t ${lm ? "border-slate-200" : "border-slate-800"}`}>
                  Forecasts made early in a product&apos;s life train a genuinely different model — yearly seasonality is off and the uncertainty bands aren&apos;t yet self-calibrated. If accuracy improves sharply with history, the fix is <span className="font-semibold">time</span>, not tuning: this row tells you how much history a product needs before you should trust its numbers.
                </div>
              </div>
            )}

            {/* Recent vs historic windows */}
            {res.byBlock?.length > 1 && (
              <div className={`${card} border rounded-2xl overflow-hidden`}>
                <div className={`px-4 py-2 text-[11px] uppercase tracking-widest font-bold ${muted} border-b ${lm ? "border-slate-200" : "border-slate-800"}`}>Recent vs historic windows</div>
                <table className="w-full">
                  <thead><tr>
                    <Th tip={TIP.block}>Window</Th><Th tip={TIP.forecasts}>Tests</Th><Th tip={TIP.mase}>vs naive</Th>
                    <Th tip={TIP.wape}>Avg miss</Th><Th tip={TIP.bias}>Runs high/low</Th>
                  </tr></thead>
                  <tbody>
                    {res.byBlock.map((b) => (
                      <tr key={b.block} className={lm ? "border-t border-slate-100" : "border-t border-slate-800/60"}>
                        <td className={`px-3 py-2 text-xs font-semibold capitalize ${text}`}>{b.block}</td>
                        <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{b.forecasts}</td>
                        <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlMASE(b.MASE))}`}>{fmt(b.MASE)}</td>
                        <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{fmt(b["WAPE%"], "%")}</td>
                        <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlBias(b["bias%"]))}`}>{b["bias%"] == null ? "—" : `${b["bias%"] > 0 ? "+" : ""}${b["bias%"]}%`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <Guide id="block" cols={["window","tests","vsnaive","avgmiss","runs"]} />
                <div className={`px-4 py-2 text-[11px] leading-relaxed ${muted} border-t ${lm ? "border-slate-200" : "border-slate-800"}`}>
                  Tests are spread across your history rather than bunched in recent months, so seasonal patterns get exercised. Historic windows may reflect an older pricing or channel mix — if <span className="font-semibold">recent</span> is much worse than <span className="font-semibold">historic</span>, something about the business changed recently and the model hasn&apos;t caught up.
                </div>
              </div>
            )}

            {/* Does routing beat one engine for everything? */}
            {res.baseline && (res.baseline.overall || res.baseline.error) && (() => {
              const b = res.baseline, ov = res.overall || {};
              if (b.error) return (
                <div className={`text-[12px] rounded-xl border px-4 py-3 ${lm ? "bg-amber-50 border-amber-200 text-amber-800" : "bg-amber-950/15 border-amber-900/30 text-amber-300"}`}>
                  Baseline comparison couldn&apos;t run: <span className="font-mono">{b.error}</span>
                </div>
              );
              const bo = b.overall, rM = ov.MASE, bM = bo.MASE;
              const verdict = (rM == null || bM == null) ? null
                : rM < bM - 0.03 ? { t: "good", s: `Routing wins — ${(((bM - rM) / bM) * 100).toFixed(0)}% more accurate than ${b.route} on everything.` }
                : bM < rM - 0.03 ? { t: "bad",  s: `${b.route} on everything beat the router here. Worth asking what routing is buying you.` }
                : { t: "warn", s: "Too close to call — routing isn't clearly earning its complexity on this data." };
              return (
                <div className={`${card} border rounded-2xl overflow-hidden`}>
                  <div className={`px-4 py-2 text-[11px] uppercase tracking-widest font-bold ${muted} border-b ${lm ? "border-slate-200" : "border-slate-800"}`}>
                    Does per-product routing beat one model for everything?
                  </div>
                  <table className="w-full">
                    <thead><tr>
                      <Th tip={TIP.baseline}>Policy</Th><Th tip={TIP.forecasts}>Tests</Th><Th tip={TIP.mase}>vs naive</Th>
                      <Th tip={TIP.wape}>Avg miss</Th><Th tip={TIP.bias}>Runs high/low</Th><Th tip={TIP.cov}>Band hit rate</Th><Th tip={TIP.service}>Stayed in stock</Th>
                    </tr></thead>
                    <tbody>
                      {[["Router (per product)", ov], [`${b.route} on everything`, bo]].map(([lab, r]) => (
                        <tr key={lab} className={lm ? "border-t border-slate-100" : "border-t border-slate-800/60"}>
                          <td className={`px-3 py-2 text-xs font-semibold ${text}`}>{lab}</td>
                          <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{r.forecasts}</td>
                          <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlMASE(r.MASE))}`}>{fmt(r.MASE)}</td>
                          <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>{fmt(r["WAPE%"], "%")}</td>
                          <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlBias(r["bias%"]))}`}>{r["bias%"] == null ? "—" : `${r["bias%"] > 0 ? "+" : ""}${r["bias%"]}%`}</td>
                          <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlCov(r["interval_cov%"]))}`}>{fmt(r["interval_cov%"], "%")}</td>
                          <td className={`px-3 py-2 text-xs font-bold tabular-nums ${tone(lm, lvlSvc(r["service_achieved%"], res?.params?.service ?? 95))}`}>{fmt(r["service_achieved%"], "%")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <Guide id="baseline" cols={["policy","tests","vsnaive","avgmiss","runs","bandhit","instock"]} />
                  <div className={`px-4 py-2 text-[11px] leading-relaxed border-t ${lm ? "border-slate-200" : "border-slate-800"}`}>
                    {verdict && <span className={`font-semibold ${tone(lm, verdict.t)}`}>{verdict.s} </span>}
                    <span className={muted}>
                      Same products, same cutoffs, same data — the only difference is that the second row forces <span className="font-semibold">{b.route}</span> onto every product instead of choosing per product.
                      {b.failedCutoffs > 0 && <> {b.failedCutoffs} window{b.failedCutoffs === 1 ? "" : "s"} failed under {b.route} because one engine doesn&apos;t fit every product — that&apos;s part of the answer, not a glitch.</>}
                      {" "}Check the confidence intervals above before treating a small difference as decisive.
                    </span>
                  </div>
                </div>
              );
            })()}

            {showFormulas && (
              <div className={`${card} border rounded-2xl p-4 space-y-3 text-[11px] ${lm ? "text-slate-600" : "text-slate-400"}`}>
                <div className={`font-semibold ${text}`}>The formulas behind these numbers</div>
                <div><span className="font-mono">Safety buffer = z × σ × √(lead time)</span> — z is set by your service target
                  ({res.params?.service}% → z={Z[res.params?.service]?.toFixed(2) ?? "—"}), σ is each SKU's daily demand swing, lead = {res.params?.lead}d.</div>
                <div><span className="font-mono">Order qty ≈ (forecast demand over coverage window) + safety buffer − stock at delivery.</span></div>
                <div><span className="font-mono">Forecast = level × seasonality (± trend).</span> The <span className={`font-semibold ${text}`}>trend</span> is how hard the forecast follows a recent rise or fall: too aggressive and it overshoots (you over- or under-order), too flat and it misses real growth. It's a code-level setting (in <span className="font-mono">engines.py</span> — search <span className="font-mono">"TREND_LEVER"</span>), not a dashboard control, and should only be touched if real data shows a consistent bias. Yearly seasonality needs ≥~450 days of history to switch on.</div>
                <div className="pt-1">
                  <div className={`font-semibold ${text} mb-1`}>Per-SKU coefficients (averaged over cutoffs)</div>
                  <table className="w-full">
                    <thead><tr><Th tip={TIP.sku}>SKU</Th><Th tip={TIP.sigma}>σ /day</Th><Th tip={TIP.cv}>CV</Th><Th tip={TIP.z}>z</Th><Th tip={TIP.rootlead}>√lead</Th><Th tip={TIP.buffer}>≈ buffer</Th></tr></thead>
                    <tbody>
                      {res.bySku.map((r) => {
                        const z = Z[res.params?.service] ?? 1.6449, rootL = Math.sqrt(res.params?.lead ?? 14);
                        const buf = r.sigma != null ? Math.round(z * r.sigma * rootL) : null;
                        return (
                          <tr key={r.sku} className={lm ? "border-t border-slate-100" : "border-t border-slate-800/60"}>
                            <td className={`px-3 py-1.5 text-xs font-semibold ${text}`}>{r.sku}</td>
                            <td className="px-3 py-1.5 text-xs tabular-nums">{r.sigma ?? "—"}</td>
                            <td className="px-3 py-1.5 text-xs tabular-nums">{r["cv%"] != null ? `${r["cv%"]}%` : "—"}</td>
                            <td className="px-3 py-1.5 text-xs tabular-nums">{z.toFixed(2)}</td>
                            <td className="px-3 py-1.5 text-xs tabular-nums">{rootL.toFixed(1)}</td>
                            <td className={`px-3 py-1.5 text-xs tabular-nums font-bold ${text}`}>{buf ?? "—"} units</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

          </Section>

          {/* AI diagnosis + formulas */}
          <div className="flex items-center gap-2 flex-wrap">
            <button onClick={runDiagnosis} disabled={diagLoading}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-violet-600 hover:bg-violet-500 disabled:opacity-60 text-white transition-all">
              <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" /></svg>
              {diagLoading ? "Reading the results…" : "Interpret these results"}
            </button>
            <button onClick={() => setShowFormulas((v) => !v)} className={`text-[11px] ${muted} hover:underline`}>
              {showFormulas ? "Hide formulas & coefficients" : "Show formulas & coefficients"}
            </button>
          </div>

          {diag && (
            <div className={`${card} border rounded-2xl p-4 text-[12px] leading-relaxed whitespace-pre-wrap ${lm ? "text-slate-700" : "text-slate-300"}`}>
              {diag}
            </div>
          )}

          </>)}
        </div>
      )}
    </div>
  );
}

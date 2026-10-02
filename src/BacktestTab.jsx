import React from "react";
import { useState, useEffect, useRef } from "react";
import { terminal, MONO, SANS } from "./lib/theme";

// Service level → z. A THIRD copy of this table (backtest.py has Z, main.py has
// PROTECTION_TIERS) — the backend pair check each other on import; this one can only
// be kept in step by hand. A level missing here doesn't throw: the buffer worked
// example below silently substitutes 1.6449 and shows arithmetic the engine never did.
const Z = { 90: 1.2816, 95: 1.6449, 98: 2.0537, 99: 2.3263, 99.5: 2.5758 };

// ── plain-English hover explanations (what it is · how it affects orders · what it means) ──
const TIP = {
  name:      "The product or forecasting engine this row is scoring.",
  forecasts: "How many past-date tests ran for this row. More tests = more trustworthy averages.",
  wape:      "WAPE. Average miss size, as a % of sales. Lower is better. Runs high for sparse/sporadic sellers, so don't over-trust it there.",
  mase:      "MASE: accuracy compared with a naive 'same as last season' guess. Under 1 beats that guess, over 1 is worse. The best single accuracy score.",
  bias:      "Whether the forecast leans high or low. Plus means over-forecasting (you'd over-order), minus means under-forecasting (stockout risk). Near 0 is ideal.",
  cov:       "80% interval coverage: how often real sales landed inside the forecast's uncertainty band. Should be about 80%. Lower means the band is too narrow (overconfident).",
  order:     "How far off the suggested order size was. Plus = ordered too much, minus = too little. Near 0 means right-sized orders.",
  sku:       "The product this buffer math is for.",
  sigma:     "How much this product's daily sales bounce around. A bigger swing needs a bigger safety buffer.",
  cv:        "That daily swing relative to average sales. High = unpredictable demand.",
  z:         "A multiplier set by your service target. A higher target gives a bigger number, so a bigger buffer.",
  rootlead:  "Stretches the buffer for how long a restock takes. Longer lead time = bigger buffer.",
  buffer:    "The resulting safety stock in units (z × σ × √lead): spare units kept on hand so you don't run out.",
  ci:        "95% confidence interval: the range the true number likely sits in. Test windows overlap and repeat per product, so it's less certain than the raw count suggests. If your target is inside this range, there isn't enough evidence to call it a real gap.",
  windows:   "How many test windows this product got. Few windows give noisy numbers, so read them as a hint, not a result.",
  train:     "How much sales history the model had when it made each forecast. Accuracy improves with history, so this separates 'weak model' from 'not enough data yet'.",
  block:     "Recent windows test how things work now. Historic windows reach further back and may reflect older pricing or channels. They help with seasonal coverage but are a worse guide to today.",
  baseline:  "The same tests re-run with ONE engine on every product instead of picking one per product. If picking per product isn't clearly better, the extra complexity isn't paying off.",
  optimal:   "The service level where one more unit of safety stock stops paying for itself, based on your margins and holding rate. It can be any value; the tiers are just the settings you can pick from.",
  achievedVsTarget: "Tiers are targets, and the buffer under-delivers: a 99% target may only achieve 97%. Pick the tier whose ACHIEVED number is nearest the optimum, not the one whose label looks right.",
  pcheapest: "How often this tier came out cheapest when we resampled your products. Costs average over a handful of windows, so a small dollar gap can be chance. Under about 2-to-1 over the runner-up, treat them as equal.",
  capacity:  "How many test windows your sales history can support. Asking for more doesn't exclude any product, but short-history products quietly return fewer windows. That looks like a result but is really a sample-size problem.",
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
  avgmiss:  ["Avg miss", "Typical gap between forecast and actual, as a % of units sold. Weighted by volume, so busy weeks count more. (WAPE)", "Lower is better. It runs high on slow sellers whatever the model, so judge those on 'vs naive'."],
  vsnaive:  ["vs naive", "Accuracy compared with just repeating what sold this time last week. 0.8 means 20% less error than that guess. (MASE)", "Under 1 beats the naive guess. Over 1 means you'd do better with the simple rule."],
  runs:     ["Runs high/low", "Whether the forecast leans over or under on average. +10% means it predicted 10% more than sold.", "Near 0. Plus builds overstock, minus risks stockouts."],
  bandhit:  ["Band hit rate", "How often real sales landed inside the forecast's uncertainty range. (80% interval coverage)", "About 80%. Lower means the range is too narrow and the tool is overconfident."],
  instock:  ["Stayed in stock", "Share of past reorder cycles where the buffer was big enough to avoid running out.", "Should match your protection target. Below it means the buffer is too thin."],
  ordersize:["Order size", "How far off the suggested order quantity was. +10% means it would have ordered 10% too much.", "Near 0. Plus ties up cash in excess stock, minus risks running short."],
  bufunits: ["Buffer units", "Spare units carried across the catalog at this level. Multiply by unit cost and your holding rate to get the buffer cost.", "Rises with the level. This is the stock you're actually paying to hold."],
  missunits:["Missed units / yr", "Units of demand per year you'd fail to cover at this level. Multiply by profit per unit to get the lost profit.", "Falls as the level rises. This is what stockouts cost you in product, before money."],
  tier:     ["Level", "The protection target: how hard the buffer tries to prevent a stockout.", "Higher keeps more spare stock, costs more to hold."],
  achieved: ["Achieved", "What that target actually delivered in testing. Targets and outcomes differ because the buffer under-delivers.", "Should land near the level itself. Well below means the buffer is under-sized there."],
  ci:       ["95% CI", "The range the true number is likely to sit in, given how few tests there are.", "Narrower is more certain. If your target sits inside it, you can't call the gap real."],
  vstarget: ["vs target", "Achieved minus the level. Negative means under-protecting.", "Near 0. Marked 'n/s' when the gap is inside the confidence range, i.e. not meaningful."],
  pcheap:   ["P(cheapest)", "How often this level came out cheapest when we re-ran the maths on resampled products.", "Over about 60% is a real preference. Two levels near 50/50 are a coin flip."],
  lostprofit:["Lost profit / yr", "Margin you'd forgo each year to stockouts at this level, across the catalog.", "Falls as the level rises. Trade it against buffer cost."],
  buffercost:["Buffer holding / yr", "The yearly cost of holding your safety stock: tied-up cash, storage, insurance, obsolescence. It is Cash in buffer × your holding rate.", "Rises as the level rises. Trade it against lost profit."],
  totalcost:["Total $/yr", "Lost profit plus buffer cost. The number to minimise.", "Lowest wins, but check P(cheapest) before trusting a small gap."],
  cash:     ["Cash in buffer", "One-time working capital parked in safety stock. Not a yearly cost; its yearly cost is the buffer column.", "Lower frees cash. Worth weighing if capital is tight."],
  history:  ["History", "How much sales history the model had when it made these forecasts.", "Accuracy usually improves with more history. That reflects the data, not the model."],
  window:   ["Window", "Recent windows test how things work now; historic ones reach further back for seasonal coverage.", "Similar numbers mean nothing major changed in the business."],
  policy:   ["Policy", "Which model choice was used: the best model per product, or one model forced on everything.", "If per-product choice isn't clearly better, the extra complexity isn't paying off."],
};

// ── metric color logic (green = good, amber = watch, red = bad) ──
/* The palette now lives in CSS variables that flip with the theme, so a metric's
   colour no longer depends on `lm` at all. The parameter is kept because ~40 call
   sites pass it, and a signature change for no behavioural gain is churn. */
const tone = (_lm, level) => ({
  good: "text-[var(--t-good)]",
  warn: "text-[var(--t-warn)]",
  bad:  "text-[var(--t-bad)]",
  none: "text-[var(--t-soft)]",
}[level] || "text-[var(--t-soft)]");
const lvlMASE = (v) => (v == null ? "none" : v < 1 ? "good" : v < 1.5 ? "warn" : "bad");
const lvlBias = (v) => (v == null ? "none" : Math.abs(v) < 10 ? "good" : Math.abs(v) < 25 ? "warn" : "bad");
const lvlCov  = (v) => (v == null ? "none" : v >= 75 && v <= 88 ? "good" : v >= 65 && v <= 92 ? "warn" : "bad");
const lvlSvc  = (v, t) => (v == null ? "none" : v >= t ? "good" : v >= t - 7 ? "warn" : "bad");
const lvlOrd  = (v) => (v == null ? "none" : Math.abs(v) < 15 ? "good" : Math.abs(v) < 30 ? "warn" : "bad");
const fmt = (v, suffix = "") => (v == null ? "-" : `${v}${suffix}`);
const ciText = (ci, suffix = "") => (Array.isArray(ci) && ci.length === 2 ? `${ci[0]}${suffix} to ${ci[1]}${suffix}` : null);
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
    { label: "Buffer holding $/yr", get: (t) => t.holdingCost },
    { label: "Holiday cover $/yr", get: (t) => t.holidayCost },
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
    // Same order the on-screen tables run in, and complete enough to reconcile:
    // lost profit + buffer holding = total. Holding and cash used to be missing, so
    // the exported "total" couldn't be rebuilt from its own neighbouring columns.
    ...tiers.flatMap((t) => [
      { label: `${t}% missed units/yr`, get: (r) => r.tiers?.[t]?.unitsYr },
      { label: `${t}% lost profit $/yr`, get: (r) => r.tiers?.[t]?.profitYr },
      { label: `${t}% buffer units`, get: (r) => r.tiers?.[t]?.safetyUnits },
      { label: `${t}% cash in buffer $`, get: (r) => r.tiers?.[t]?.bufferCash },
      { label: `${t}% buffer holding $/yr`, get: (r) => r.tiers?.[t]?.holdingCostYr },
      { label: `${t}% holiday cover $/yr`, get: (r) => r.tiers?.[t]?.holidayCostYr },
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

/* ─── HOLIDAY WEEKS ─────────────────────────────────────────────────────────────
 * Holiday weeks sell less predictably than ordinary ones, so orders placed while a
 * holiday is inside the order window carry temporary extra cover (holiday_cover.py).
 * These show what that cover bought in the replay: how often holiday weeks stayed in
 * stock with it and without it, at the store's measured rate per holiday. The "with"
 * figures are out of sample (each year graded with a rate measured without it). */
const HMUTED = "text-[var(--t-dim)]";
const HTEXT = "text-[var(--t-ink)]";
const hpct = (v) => (v == null ? "-" : `${v}%`);
const hrateText = (r) => {
  const vs = ["1", "2", "3"].map((b) => r?.[b]);
  if (vs.every((v) => v == null)) return "-";
  return vs.map((v) => (v == null ? "-" : `${Math.round(v * 100)}%`)).join(" / ");
};
const tierRow = (hc, tier) => (hc?.tiers || []).find((t) => t.tier === tier)
  || (hc?.tiers || []).find((t) => t.tier === 95);

function HolidayWeeksTable({ hc, lm }) {
  const rows = hc?.byHoliday || [];
  const th = `text-left text-[14px] uppercase tracking-widest font-bold ${HMUTED} px-3 py-2`;
  return (
    <div>
      <div className={`px-4 py-3 border-b text-[15px] leading-relaxed border-[var(--t-line)] bg-[var(--t-accent-soft)] text-[var(--t-soft)]`}>
        Holiday weeks get temporary extra cover, sized by how far off holiday weeks have run in this store.
        A holiday gets <span className="font-semibold">its own rate</span> once it has {hc?.rules?.minOwnWindows ?? 100}+
        tested weeks across {hc?.rules?.minOwnSkus ?? 20}+ products; the rest <span className="font-semibold">share one</span>.
        Rates are stepped by how many of the product&apos;s own years the holiday&apos;s pattern rests on (1 / 2 / 3+), and
        graded leave-one-year-out.
      </div>
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead><tr>
            <th className={th} title="The holiday stretch the forecast reshapes.">Holiday</th>
            <th className={th} title="Own: measured on this holiday's weeks alone. Shared: not enough of its weeks yet, so it uses the rate measured on every holiday together.">Rate source</th>
            <th className={th} title="Extra cover as a share of the holiday's forecast units, for a pattern resting on 1 / 2 / 3+ of the product's own years. Fewer years, less certain, more cover.">Rate by history (1 / 2 / 3+ yrs)</th>
            <th className={th} title="Replayed weeks that carried this holiday.">Weeks</th>
            <th className={th} title="Products those weeks came from.">Products</th>
            <th className={th} title="How often those weeks stayed in stock at the 95% level, without the cover and with it.">In stock: without → with</th>
            <th className={th} title="Typical extra units carried before this holiday at the 95% level.">Avg cover</th>
          </tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className="border-t border-[var(--t-line)]">
                <td className={`px-3 py-2 text-[15px] font-semibold ${HTEXT}`}>{r.name}</td>
                <td className={`px-3 py-2 text-[14px] ${r.rateSource === "own" ? HTEXT : HMUTED}`}>{r.rateSource === "own" ? "own" : "shared"}</td>
                <td className={`px-3 py-2 text-[15px] tabular-nums ${HTEXT}`}>{hrateText(r.rates)}</td>
                <td className={`px-3 py-2 text-[15px] tabular-nums ${HMUTED}`}>{r.windows}</td>
                <td className={`px-3 py-2 text-[15px] tabular-nums ${HMUTED}`}>{r.skus}</td>
                <td className="px-3 py-2 text-[15px] tabular-nums">
                  <span className={tone(lm, lvlSvc(r.without, 95))}>{hpct(r.without)}</span>
                  <span className={HMUTED}> → </span>
                  <span className={`font-bold ${tone(lm, lvlSvc(r.with, 95))}`}>{hpct(r.with)}</span>
                </td>
                <td className={`px-3 py-2 text-[15px] tabular-nums ${HMUTED}`}>{r.avgCoverUnits == null ? "-" : `+${Math.round(r.avgCoverUnits)} units`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className={`px-4 py-2 text-[14px] ${HMUTED} border-t border-[var(--t-line)]`}>
        {hc.windows} holiday weeks and {hc.ordinaryWindows} ordinary weeks replayed. Shared rate (1 / 2 / 3+ yrs): {hrateText(hc.shared)}.
      </div>
    </div>
  );
}

function HolidayWeeksCard({ hc, tier, lm, money, holidayCost }) {
  const rows = hc?.byHoliday || [];
  const t = tierRow(hc, tier);
  if (!rows.length || !t) return null;
  const shared = rows.filter((r) => r.rateSource !== "own");
  const own = rows.filter((r) => r.rateSource === "own");
  const th = `text-left text-[14px] uppercase tracking-widest font-bold ${HMUTED} px-3 py-2`;
  return (
    <div className="bg-[var(--t-panel)] border-[var(--t-line)] border rounded-2xl overflow-hidden">
      <div className="p-4">
        <h3 className={`text-[16.5px] font-bold ${HTEXT}`}>Holiday weeks</h3>
        <p className={`text-[15px] ${HMUTED} mt-1 max-w-[720px] leading-relaxed`}>
          Holiday weeks sell less predictably than ordinary ones, so orders placed before a holiday carry
          temporary extra cover, sold down after it. Without it you&apos;d have stayed in stock
          in <span className="font-semibold">{hpct(t.holidayWithout)}</span> of holiday weeks; with it,{" "}
          <span className={`font-semibold ${tone(lm, lvlSvc(t.holidayWith, t.ordinary ?? 95))}`}>{hpct(t.holidayWith)}</span>,
          against {hpct(t.ordinary)} of ordinary weeks.
          {holidayCost != null && <> It costs about {money(holidayCost)}/yr to carry, included in the totals below.</>}
        </p>
      </div>
      <table className="w-full">
        <thead><tr>
          <th className={th}>Holiday</th>
          <th className={th} title="Replayed weeks that carried this holiday.">Weeks tested</th>
          <th className={th} title="How often those weeks stayed in stock at the 95% level, without the holiday cover and with it.">In stock: without → with</th>
          <th className={th} title="Typical extra units carried before this holiday, per product, at the 95% level.">Typical cover</th>
        </tr></thead>
        <tbody>
          {[...own, ...shared].map((r) => (
            <tr key={r.key} className="border-t border-[var(--t-line)]">
              <td className={`px-3 py-2 text-[15px] font-semibold ${HTEXT}`}>
                {r.name}{r.rateSource !== "own" && <span className={HMUTED}>*</span>}
              </td>
              <td className={`px-3 py-2 text-[15px] tabular-nums ${HMUTED}`}>{r.windows}</td>
              <td className="px-3 py-2 text-[15px] tabular-nums">
                <span className={HMUTED}>{hpct(r.without)} → </span>
                <span className={`font-bold ${tone(lm, lvlSvc(r.with, 95))}`}>{hpct(r.with)}</span>
              </td>
              <td className={`px-3 py-2 text-[15px] tabular-nums ${HMUTED}`}>{r.avgCoverUnits == null ? "-" : `+${Math.round(r.avgCoverUnits)} units`}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {shared.length > 0 && (
        <div className={`px-4 py-2 text-[14px] ${HMUTED} border-t border-[var(--t-line)]`}>
          * Shares one rate measured on every holiday together: not enough of its own weeks yet to measure it alone.
        </div>
      )}
    </div>
  );
}

export default function BacktestTab({ api = "http://localhost:8000", lm = false,
                                     holdingPct = 25, setHoldingPct = null,
                                     res = null, setRes = () => {}, setDiag = () => {},
                                     waiting = [], waitingIds = [], onPickSku = null, onRetest = null }) {
  // `res` and `diag` are lifted to the parent so results persist across tab switches.
  const [viewMode, setView]   = useState("client");        // "client" | "analyst"
  // Which tab of the analyst grid is showing. "summary" is the calibration & annual
  // cost table; every other key is a metric in the product x tier matrix.
  const [matrixMetric, setMatrixMetric] = useState("summary");
  const [openSections, setOpenSections] = useState({});
  // Products tested against the default lead time are named on demand, not by default:
  // the complaint is the message, the list is the drill-down.
  const [showWaiting, setShowWaiting] = useState(false);
  // Only the two methodology knobs remain local. Cutoffs are sized per product, holding
  // comes from the Products page, lead/coverage come from each product.
  const [p, setP]             = useState({ horizon: 44, minTrain: 120 });
  const [advanced, setAdv]    = useState(false);
  const [error, setError]     = useState(null);

  const card  = "bg-[var(--t-panel)] border-[var(--t-line)]";
  const muted = "text-[var(--t-dim)]";
  const text  = "text-[var(--t-ink)]";
  const inp   = "bg-[var(--t-bg)] border-[var(--t-line2)] text-[var(--t-ink)]";
  const th    = `text-left text-[14px] uppercase tracking-widest font-bold ${muted} px-3 py-2`;
  const setNum = (k) => (e) => setP((s) => ({ ...s, [k]: e.target.value }));
  // header cell with a hover explanation (dotted underline signals it's hoverable)
  const Th = ({ tip, children }) => (
    <th className={th} title={tip}>
      <span className="underline decoration-dotted decoration-[var(--t-faint)] underline-offset-4 cursor-help">{children}</span>
    </th>
  );

  // Visible column guide. Sits under its table, closed by default so it doesn't add to
  // the clutter, but always one click away — unlike a hover tooltip.
  const Guide = ({ id, cols, extra = [] }) => {
    const open = openSections[`guide:${id}`] ?? false;
    const rows = [...cols.map((c) => COLDEF[c]).filter(Boolean), ...extra];
    if (!rows.length) return null;
    return (
      <div className={`border-t border-[var(--t-line)]`}>
        <button onClick={() => setOpenSections((s) => ({ ...s, [`guide:${id}`]: !open }))}
          className={`w-full px-4 py-2 text-left text-[14px] font-semibold text-[var(--t-accent)] hover:bg-[var(--t-sunken)] transition-colors`}>
          {open ? "Hide" : "What do these columns mean?"}
        </button>
        {open && (
          <dl className={`px-4 pb-3 space-y-2 border-t border-[var(--t-line)] pt-3`}>
            {rows.map(([label, what, good]) => (
              <div key={label} className="grid grid-cols-1 sm:grid-cols-[148px_1fr] gap-x-3 gap-y-0.5">
                <dt className={`text-[14px] font-bold ${text}`}>{label}</dt>
                <dd className={`text-[14px] leading-relaxed ${muted}`}>
                  {what} <span className={"text-[var(--t-soft)]"}><span className="font-semibold">Good looks like:</span> {good}</span>
                </dd>
              </div>
            ))}
          </dl>
        )}
      </div>
    );
  };

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
        if (d?.job?.status === "running") { setAutoLoad("running"); timer = setTimeout(pull, 4000); return; }
        /* The server has no run. Say so, rather than leaving whatever is on screen.
           This used to bail out when `res` was set, which meant a report could survive
           the run it described — after a reset, or after the data changed enough that
           the saved run no longer applies. Showing a stale report is worse than showing
           none: it reads as current. */
        if (res) { setRes(null); setDiag?.(null); }
        setAutoLoad("none");
      } catch { if (!cancelled) setAutoLoad("none"); }
    };
    setAutoLoad("loading"); pull();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [api, jobRunning]);   // re-check when a run starts or finishes, not just on mount

  // One data path in: the Products page. This just re-tests whatever's loaded, through
  // the same request the dashboard makes: every lead/coverage pair your products use,
  // their costs and your holding rate. It used to call the fleet endpoint with none of
  // those, so the server tested 14/30 at 25% and the result replaced every product's
  // measured tier with ones measured for the wrong settings.
  const [requested, setRequested] = useState(false);
  const sawJob = useRef(false);
  useEffect(() => {
    if (!requested) return;
    if (jobRunning) { sawJob.current = true; return; }
    if (sawJob.current) { setRequested(false); sawJob.current = false; }
  }, [jobRunning, requested]);
  useEffect(() => {
    if (!requested) return;
    // A run that finished before the status poll ever saw it still ends the wait.
    const t = setTimeout(() => { if (!sawJob.current) setRequested(false); }, 20000);
    return () => clearTimeout(t);
  }, [requested]);
  // Busy while this tab's request is starting or any test is running (the dashboard's
  // own runs included), so a second request isn't queued on top of one in progress.
  const loading = requested || jobRunning;
  const run = async () => {
    setError(null); setDiag(null);
    if (!onRetest) { setError("Re-testing isn't available here."); return; }
    setRequested(true); sawJob.current = false;
    try {
      const started = await onRetest({ horizon: Number(p.horizon) || 44, minTrain: Number(p.minTrain) || 120 });
      if (started === false) {
        setRequested(false);
        setError("Nothing to test yet: no product has a confirmed lead time.");
      }
    } catch (e) {
      setRequested(false);
      setError(e?.message || "Something went wrong.");
    }
  };

  // Rows built from too few windows are dimmed and badged rather than hidden — the
  // number still exists, it just shouldn't be read with the same confidence.
  const Row = ({ r, name }) => (
    <tr className={`border-t border-[var(--t-line)] ${r.reportable === false ? "opacity-50" : ""}`}>
      <td className={`px-3 py-2 text-[15px] font-semibold ${text}`}>
        {name}
        {r.reportable === false && (
          <span title={`Only ${r.windows} test window${r.windows === 1 ? "" : "s"}, too few to read as a result.`}
            className={`ml-1.5 text-[13px] font-semibold px-1.5 py-0.5 rounded-full cursor-help bg-[var(--t-sunken)] text-[var(--t-dim)]`}>
            {r.windows}w · thin
          </span>
        )}
      </td>
      <td className={`px-3 py-2 text-[15px] tabular-nums ${muted}`}>{r.forecasts}</td>
      <td className={`px-3 py-2 text-[15px] tabular-nums ${muted}`} title={r["WAPE%_unweighted"] != null ? `Volume-weighted. Unweighted (mean of per-window ratios): ${r["WAPE%_unweighted"]}%` : undefined}>{fmt(r["WAPE%"], "%")}</td>
      <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${tone(lm, lvlMASE(r.MASE))}`}>{fmt(r.MASE)}</td>
      <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${tone(lm, lvlBias(r["bias%"]))}`}>{r["bias%"] == null ? "-" : `${r["bias%"] > 0 ? "+" : ""}${r["bias%"]}%`}</td>
      <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${tone(lm, lvlCov(r["interval_cov%"]))}`}>{fmt(r["interval_cov%"], "%")}</td>
      <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${tone(lm, lvlSvc(r["service_achieved%"], res?.params?.service ?? 95))}`}>{fmt(r["service_achieved%"], "%")}</td>
      <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${tone(lm, lvlOrd(r["order_err%"]))}`}>{r["order_err%"] == null ? "-" : `${r["order_err%"] > 0 ? "+" : ""}${r["order_err%"]}%`}</td>
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

  /* One block of variables at the root instead of ~100 `lm ? light : dark` ternaries
     scattered through the markup. The theme flips here and everything below follows. */
  const T = terminal(lm);
  const VARS = {
    "--t-bg": T.bg, "--t-panel": T.panel, "--t-sunken": T.sunken,
    "--t-ink": T.ink, "--t-soft": T.soft, "--t-dim": T.dim, "--t-faint": T.faint,
    "--t-line": T.line, "--t-line2": T.line2,
    "--t-accent": T.amber, "--t-accent-soft": `${T.amber}14`, "--t-accent-line": `${T.amber}55`,
    "--t-good": T.green, "--t-good-soft": `${T.green}16`, "--t-good-line": `${T.green}55`,
    "--t-warn": T.over,  "--t-warn-soft": `${T.over}16`,  "--t-warn-line": `${T.over}55`,
    "--t-bad": T.red,    "--t-bad-soft": `${T.red}14`,    "--t-bad-line": `${T.red}55`,
    "--t-info": T.blue,  "--t-info-soft": `${T.blue}16`,
    "--t-btn-bg": T.btnBg, "--t-btn-fg": T.btnFg,
  };
  return (
    <div style={{ ...VARS, background:T.bg, fontFamily:SANS }}
      className={`flex-1 overflow-y-auto p-4 sm:p-6 space-y-5 text-[var(--t-ink)]`}>
      {/* Figures are monospaced; prose is not. Applied by class so the ~62 existing
          `tabular-nums` cells pick the face up without touching each one. */}
      <style>{`.tabular-nums{font-family:${MONO};font-variant-numeric:tabular-nums}`}</style>
      {/* Products without a settled lead time are skipped, not blocked — the same way the
          engine already skips products with no unit cost. Say which, and why. */}
      {waiting.length > 0 && (
        <div className={`rounded-xl border px-3.5 py-2.5 bg-[var(--t-warn-soft)] border-[var(--t-warn-line)]`}>
          {/* This said "not tested yet", which was flatly wrong: these products WERE
              tested — the whole report below is built from them — they were just tested
              against the default lead time because none is set. A reader who believed the
              old wording would conclude the page was empty while looking straight at the
              results. The honest complaint is about what the numbers were measured
              AGAINST, not whether they exist. */}
          <div className={`text-[15px] font-semibold text-[var(--t-warn)]`}>
            {waiting.length} product{waiting.length === 1 ? "" : "s"} tested against the default lead time
          </div>
          <div className={`text-[14px] mt-0.5 text-[var(--t-warn)]`}>
            {waiting.length === 1 ? "It has" : "They have"} no lead time of their own, so the results below
            assume the default and are a guess about timing. Set a lead time on the product (or once on its
            supplier) and the next run measures {waiting.length === 1 ? "it" : "them"} properly.
          </div>
          {/* The list is a drill-down, not the message. Naming fourteen products inline
              pushed the page down and buried the one sentence that says what to do. */}
          <button onClick={() => setShowWaiting((v) => !v)}
            className={`mt-2 text-[14px] font-semibold text-[var(--t-warn)] hover:underline`}>
            {showWaiting ? "Hide details" : `View details (${waiting.length})`}
          </button>
          {showWaiting && (
            <div className="flex flex-wrap gap-1.5 mt-2">
              {waiting.map((name, i) => (
                <button key={`${name}-${i}`} onClick={() => onPickSku && onPickSku(waitingIds[i])}
                  className={`text-[13px] font-mono px-2 py-0.5 rounded-md border transition-all bg-[var(--t-panel)] border-[var(--t-warn-line)] text-[var(--t-warn)] hover:border-[var(--t-warn)]`}>
                  {name}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
      {/* What ISN'T in the numbers below, stated before them rather than inside a
          collapsed section underneath. A product with too little history is absent from
          every figure on this tab; a failed window silently shrinks the run. One line
          each, names and days — nothing to read around. */}
      {res && (() => {
        const shortOnes = (res.skipped || []).filter((s) => s.kind !== "error");
        const failed = res.failedCutoffs || 0;
        if (!shortOnes.length && !failed) return null;
        return (
          <div className={`rounded-xl border px-3.5 py-2.5 text-[14px] leading-relaxed bg-[var(--t-sunken)] border-[var(--t-line)] ${muted}`}>
            {shortOnes.length > 0 && (
              <div>
                <span className={`font-semibold ${text}`}>Skipped (too little history to backtest):</span>{" "}
                <span className="tabular-nums">{shortOnes.map((x) => `${x.sku} (${x.days}d)`).join(" · ")}</span>
              </div>
            )}
            {failed > 0 && (
              <div className={shortOnes.length ? "mt-1" : ""}>
                <span className={`font-semibold text-[var(--t-warn)]`}>{failed} test window{failed === 1 ? "" : "s"} failed</span>
                {" "}and {failed === 1 ? "was" : "were"} excluded from every number below.
              </div>
            )}
          </div>
        );
      })()}

      <div>
        <h2 className="text-lg font-bold tracking-tight">Forecast Backtest</h2>
        <p className={`text-[14px] ${muted} mt-1 max-w-3xl`}>
          How your forecasts and reorder suggestions would have done on your own sales history. They are re-run at
          dozens of past dates and graded against what actually sold next (measured, not projected).
          It tests whatever is loaded on the Products page, updates itself, and lists products without enough history as untested.
        </p>
      </div>

      {/* No source picker, no per-run parameters: data comes in on the Products page and
          everything that shapes a run — cutoffs, holding rate, each product's lead time
          and coverage — is set where it belongs and applied automatically. What's left
          here are two methodology knobs most people never touch. */}
      <div className={`${card} border rounded-2xl p-4 space-y-3`}>
        <div className="flex items-center gap-3 flex-wrap">
          <button onClick={run} disabled={loading}
            className="px-4 py-2 rounded-lg text-[15px] font-bold bg-[var(--t-btn-bg)] hover:bg-[var(--t-btn-bg)] disabled:opacity-60 text-[var(--t-btn-fg)] transition-all">
            {loading ? "Testing… (refits every product, takes a few minutes)" : "Re-test now"}
          </button>
          <span className={`text-[14px] ${muted}`}>
            Runs on its own whenever you load data or change a cost, lead time or coverage window.
          </span>
          <button onClick={() => setAdv((v) => !v)} className={`ml-auto text-[14px] ${muted} hover:underline`}>
            {advanced ? "Hide method settings" : "Method settings"}
          </button>
        </div>
        {advanced && (
          <div className={`grid grid-cols-2 sm:grid-cols-4 gap-3 pt-1 border-t border-[var(--t-line)]`}>
            {[["horizon", "Forecast horizon (d)"], ["minTrain", "Min history (d)"]].map(([k, lab]) => (
              <div key={k}>
                <label title={PTIP[k]} className={`block text-[13px] uppercase tracking-widest ${muted} mb-1 mt-2 cursor-help`}>{lab}</label>
                <input type="number" value={p[k]} onChange={setNum(k)} className={`w-full rounded-lg border px-2 py-1.5 text-[15px] ${inp}`} />
              </div>
            ))}
            <div className={`col-span-2 text-[14px] ${muted} mt-2 leading-relaxed`}>
              These change what a &ldquo;test&rdquo; means, so press <span className="font-semibold">Re-test now</span> afterwards.
              Everything else is automatic: test count is sized per product, holding rate comes from the Products page, and lead time and coverage from each product.
            </div>
          </div>
        )}
        {error && <div className={`text-[14px] rounded-lg px-3 py-2 border text-[var(--t-bad)] bg-[var(--t-bad-soft)] border-[var(--t-bad-line)]`}>{error}</div>}
      </div>


      {/* No results yet: say which of the three reasons it is, rather than nothing. */}
      {!res && !loading && autoLoad === "running" && (
        <div className={`${card} border rounded-2xl p-4 flex items-center gap-3`}>
          <svg className={`h-4 w-4 animate-spin text-[var(--t-accent)]`} fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z" />
          </svg>
          <div className={`text-[15px] ${text}`}>
            <div className="font-semibold">A test is running on your products.</div>
            <div className={muted}>Results appear here automatically, usually within a few minutes. No need to press anything.</div>
          </div>
        </div>
      )}
      {!res && !loading && autoLoad === "none" && (
        <div className={`${card} border rounded-2xl p-4 text-[15px] ${muted}`}>
          <span className={`font-semibold ${text}`}>No results yet.</span> A test runs automatically when you load
          products. If nothing is loaded, import a file on the <span className="font-semibold">Products</span> page first.
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
              <div className={`inline-flex rounded-lg border overflow-hidden border-[var(--t-line2)]`}>
                {[["client", "Client view"], ["analyst", "Analyst view"]].map(([v, lab]) => (
                  <button key={v} onClick={() => setView(v)}
                    className={`px-3 py-1.5 text-[15px] font-semibold transition-all ${viewMode === v
                      ? ("bg-[var(--t-btn-bg)] text-[var(--t-btn-fg)]")
                      : ("bg-[var(--t-panel)] text-[var(--t-soft)] hover:bg-[var(--t-sunken)]")}`}>
                    {lab}
                  </button>
                ))}
              </div>
              <button onClick={() => downloadCsv(res)} title="Every table on this page, as one sectioned CSV."
                className={`flex items-center gap-1.5 text-[15px] font-semibold px-3 py-1.5 rounded-lg border transition-all bg-[var(--t-panel)] text-[var(--t-accent)] border-[var(--t-accent-line)] hover:bg-[var(--t-accent-soft)]`}>
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" /></svg>
                Download CSV
              </button>
              {/* "Clear results" lived here. It only ever emptied this tab's local copy,
                  which made sense while the browser held the report. The server persists
                  it now, so the very next poll (5s) pulled the same report straight back
                  — a button that visibly undid itself. What's on this tab is whatever the
                  server has: a stored run, a new one, or nothing. */}
            </div>
            <div className={`text-[14px] ${muted}`}>
              {viewMode === "client" ? "Plain-language summary, safe to share." : "Full metrics, formulas & per-SKU detail."}
              {res.ranAt && <> · ran {new Date(res.ranAt * 1000).toLocaleString()}{res.trigger === "upload" ? " (automatically, on upload)" : ""}</>}
            </div>
          </div>

          {/* ───────────────── CLIENT VIEW ───────────────── */}
          {viewMode === "client" && (() => {
            const ov = res.overall || {};
            const ta = res.tierAnalysis || {};
            const money = (v) => v == null ? "-" : `$${Math.round(v).toLocaleString()}`;
            const bestRow = (ta.tiers || []).find((t) => t.tier === ta.bestTier);
            const svc = bestRow ? bestRow.achievedService : ov["service_achieved%"];
            const oe = ov["order_err%"];
            const orderText = oe == null ? "no order data" : Math.abs(oe) < 8 ? "right-sized on average" : `ran ${Math.abs(Math.round(oe))}% too ${oe > 0 ? "high" : "low"}`;
            /* MASE is forecast error as a ratio of a naive "same as last season" guess,
               so 1 − MASE is the share of that error the forecast removes. This card used
               to read "Solid / beats a naive last-season guess" and never showed the
               figure — the figure is the whole point of the comparison.
               The interval governs the headline: when MASE's 95% CI straddles 1.0 the run
               genuinely cannot separate the forecast from the naive guess, and printing
               "17% better" off a point estimate would claim precision a few dozen
               overlapping windows don't support. */
            const mci = Array.isArray(ov.MASE_ci) && ov.MASE_ci.length === 2 ? ov.MASE_ci : null;
            const maseUnsure = mci ? mci[0] < 1 && mci[1] > 1 : false;
            const betterPct = ov.MASE == null ? null : Math.round((1 - ov.MASE) * 100);
            /* The figure is a COMPARISON, not a score, and a bare "17%" under the words
               "Forecast accuracy" reads at a glance as "the forecast is 17% accurate" —
               the opposite of what it says. The direction word travels with the number so
               the tile can only be read one way. */
            const accNum = ov.MASE == null ? "-" : maseUnsure ? "Too close to call" : `${Math.abs(betterPct)}%`;
            const accDir = (ov.MASE == null || maseUnsure) ? null : betterPct >= 0 ? "better" : "worse";
            const accSub = ov.MASE == null ? ""
              : maseUnsure ? "this run can't separate it from a naive last-season guess"
              : betterPct >= 0 ? "fewer forecast errors than a naive last-season guess"
              : "more forecast errors than a naive last-season guess";
            /* Same terms as the headline — a lower MASE is a better forecast, so the
               interval flips when it becomes a percentage improvement. Named for what it
               is: these are 95% bootstrap intervals over products, and "likely" undersold
               a number the run is actually entitled to state. Both bounds share the
               headline's sign, because an interval spanning zero is the maseUnsure case. */
            const accRange = (!mci || maseUnsure) ? null
              : `95% confident: ${Math.round((1 - mci[1]) * 100)} to ${Math.round((1 - mci[0]) * 100)}% ${accDir}`;
            const hcRow = tierRow(res.holidayCover, ta.bestTier ?? 95);
            const costNote = !ta.priced ? ""
              : ta.costBasis === "actual" ? "Based on your real per-unit cost and fees."
              : `Based on the ${ta.costedSkus} of ${ta.totalSkus} tested products with a cost entered. Add costs for the rest to include them.`;
            return (
              <div className="space-y-4">
                <div className={`${card} border rounded-2xl p-4`}>
                  <h3 className={`text-[16.5px] font-bold ${text}`}>How these reorder suggestions would have performed on your own sales history</h3>
                  <p className={`text-[14px] ${muted} mt-1`}>
                    Tested across {res.forecasts} simulated past reorder points on {res.tested} product{res.tested !== 1 ? "s" : ""}, each graded against what actually sold next.
                  </p>
                  {/* Freshness. This is the shareable view, so it must never quietly
                      present a superseded run as current. */}
                  <p className={`text-[14px] mt-1 ${jobRunning ? ("text-[var(--t-warn)]") : muted}`}>
                    {res.ranAt ? <>Test run {whenRan(res.ranAt)}{res.trigger === "upload" ? ", automatically when you uploaded" : ""}.</> : null}
                    {jobRunning && <> <span className="font-semibold">A newer test is running now.</span> These figures may change when it finishes.</>}
                  </p>
                  <div className="flex flex-wrap gap-3 mt-3">
                    <div className={`flex-1 min-w-[150px] rounded-xl border p-4 bg-[var(--t-panel)] border-[var(--t-line)]`}>
                      <div className={`text-[13px] uppercase tracking-widest font-bold ${muted} mb-1`}>Stayed in stock</div>
                      <div className={`text-2xl font-bold tabular-nums ${text}`}>{svc == null ? "-" : `${svc}%`}</div>
                      <div className={`text-[14px] ${muted} mt-0.5`}>{bestRow ? `at the recommended ${ta.bestTier}% level` : "of past reorder cycles"}</div>
                      {/* Show the range, not just the headline — a single number here reads as
                          far more precise than a few dozen overlapping test windows support. */}
                      {ciText(bestRow?.achievedServiceCI ?? ov["service_achieved%_ci"], "%") && (
                        <div className={`text-[13px] ${muted} mt-1`} title={TIP.ci}>
                          95% confident: {ciText(bestRow?.achievedServiceCI ?? ov["service_achieved%_ci"], "%")}
                        </div>
                      )}
                      {hcRow?.holidayWith != null && (
                        <div className={`text-[13px] ${muted} mt-1`} title="Weeks a holiday reshapes, graded with the temporary holiday cover, against every other week.">
                          Holiday weeks {hcRow.holidayWith}% · other weeks {hcRow.ordinary}%
                        </div>
                      )}
                    </div>
                    <div className={`flex-1 min-w-[150px] rounded-xl border p-4 bg-[var(--t-panel)] border-[var(--t-line)]`}>
                      <div className={`text-[13px] uppercase tracking-widest font-bold ${muted} mb-1`}>Order sizing</div>
                      <div className={`text-2xl font-bold tabular-nums ${text}`}>{oe == null ? "-" : Math.abs(oe) < 8 ? "On point" : `${oe > 0 ? "+" : ""}${Math.round(oe)}%`}</div>
                      <div className={`text-[14px] ${muted} mt-0.5`}>{orderText}</div>
                    </div>
                    <div className={`flex-1 min-w-[150px] rounded-xl border p-4 bg-[var(--t-panel)] border-[var(--t-line)]`}>
                      <div className={`text-[13px] uppercase tracking-widest font-bold ${muted} mb-1`}>Forecast accuracy</div>
                      <div className={`font-bold ${text} ${maseUnsure ? "text-xl" : "text-2xl"}`}>
                        <span className={maseUnsure ? "" : "tabular-nums"}>{accNum}</span>
                        {accDir && <span className="text-base font-semibold ml-1">{accDir}</span>}
                      </div>
                      <div className={`text-[14px] ${muted} mt-0.5`}>{accSub}</div>
                      {accRange && (
                        <div className={`text-[13px] ${muted} mt-1`} title={TIP.ci}>{accRange}</div>
                      )}
                    </div>
                  </div>
                </div>

                <HolidayWeeksCard hc={res.holidayCover} tier={ta.bestTier ?? 95} lm={lm} money={money}
                  holidayCost={bestRow?.holidayCost} />

                {ta.priced && ta.bestTier && (
                  <div className={`rounded-2xl border p-4 bg-[var(--t-good-soft)] border-[var(--t-good-line)]`}>
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
                            <div className={`text-[16.5px] font-bold text-[var(--t-good)]`}>
                              In use: a level per product ({Object.entries(mp.tierCounts || {}).filter(([, n]) => n).map(([p, n]) => `${n} at ${p}%`).join(", ")})
                            </div>
                            <p className={`text-[15px] mt-1 leading-relaxed text-[var(--t-good)]`}>
                              Each product gets the level its own numbers call for. Compared with one level
                              for everything, this saves {money(bestUniformCost - mp.totalCost)}/yr. Applied automatically.
                            </p>
                          </>
                        );
                      }
                      return (
                        <>
                          <div className={`text-[16.5px] font-bold text-[var(--t-good)]`}>In use: {shown}% on every product</div>
                          <p className={`text-[15px] mt-1 leading-relaxed text-[var(--t-good)]`}>
                            {tied
                              ? <>Cheapest overall. {other}% costs about the same, but {shown}% is closer to your optimal in-stock rate ({cr ? `${cr.optimalService}%` : "-"}).</>
                              : <>Cheapest overall at about {money(shownRow?.totalCost)}/yr.</>}
                            {mp?.totalCost != null && <> A custom level per product was also tested. It didn&apos;t beat this, so one level is used everywhere.</>}
                            {" "}Applied automatically.
                          </p>
                        </>
                      );
                    })()}
                  </div>
                )}

                {/* THE CURVE.
                    This whole tab exists to find the low point of a cost curve, and it
                    was previously only findable by reading three columns of a table and
                    doing the comparison in your head. Every level's cost is two opposing
                    quantities — profit lost to stockouts, which falls as you protect more,
                    and cash tied up in buffer, which rises — so the bars are stacked and
                    the shape IS the trade-off. The cheapest column is the answer, and it
                    is the shortest one. */}
                {ta.priced && ta.tiers?.length > 0 && (() => {
                  const rows = ta.tiers.filter((t) => t.totalCost != null);
                  if (!rows.length) return null;
                  const max = Math.max(...rows.map((t) => t.totalCost));
                  const cheapest = rows.reduce((a, b) => (b.totalCost < a.totalCost ? b : a));
                  const H = 168;
                  return (
                    <div className={`${card} border rounded-2xl p-4`}>
                      <div className="flex items-start justify-between gap-4 flex-wrap">
                        <div>
                          <h3 className={`text-[16.5px] font-bold ${text}`}>What each protection level would have cost you</h3>
                          <p className={`text-[15px] ${muted} mt-1 max-w-[640px] leading-relaxed`}>
                            Replayed against your own sales history. More protection means fewer missed sales but more
                            cash tied up in buffer stock. The best level is where the two together cost least.
                          </p>
                        </div>
                        <div className={`flex gap-4 text-[14.5px] ${muted}`}>
                          <span className="flex items-center gap-2">
                            <span className="inline-block w-3 h-3" style={{ background: "var(--t-bad)" }} />
                            Profit lost to stockouts
                          </span>
                          <span className="flex items-center gap-2">
                            <span className="inline-block w-3 h-3" style={{ background: "var(--t-info)" }} />
                            Cost of holding buffer
                          </span>
                          {rows.some((t) => (t.holidayCost || 0) > 0) && (
                            <span className="flex items-center gap-2">
                              <span className="inline-block w-3 h-3" style={{ background: "var(--t-warn)" }} />
                              Holiday cover
                            </span>
                          )}
                        </div>
                      </div>

                      <div className="flex items-end gap-4 mt-5" style={{ height: H + 108 }}>
                        {rows.map((t) => {
                          const isBest = t.tier === cheapest.tier;
                          const h = Math.max(Math.round((t.totalCost / max) * H), 6);
                          const sc = t.stockoutCost, hc = t.holdingCost, hh = t.holidayCost || 0;
                          const known = sc != null && hc != null && (sc + hc + hh) > 0;
                          const scH = known ? Math.round((sc / (sc + hc + hh)) * h) : 0;
                          const hhH = known ? Math.round((hh / (sc + hc + hh)) * h) : 0;
                          return (
                            <div key={t.tier} className="flex-1 flex flex-col items-center justify-end gap-2"
                              title={`${t.tier}% · ${money(t.totalCost)}/yr total`
                                + (known ? `\n${money(sc)} profit lost to stockouts\n${money(hc)} cost of holding buffer`
                                  + (hh > 0 ? `\n${money(hh)} holiday cover` : "") : "")}>
                              <div className={`text-[15.5px] font-bold tabular-nums ${isBest ? "text-[var(--t-accent)]" : text}`}>
                                {money(t.totalCost)}
                              </div>
                              <div className="w-full flex flex-col" style={{ height: h }}>
                                {known ? (<>
                                  <div style={{ height: scH, background: "var(--t-bad)", opacity: isBest ? 1 : .55 }} />
                                  <div style={{ flex: 1, background: "var(--t-info)", opacity: isBest ? 1 : .55 }} />
                                  {hhH > 0 && <div style={{ height: hhH, background: "var(--t-warn)", opacity: isBest ? 1 : .55 }} />}
                                </>) : (
                                  <div style={{ flex: 1, background: "var(--t-info)", opacity: isBest ? 1 : .55 }} />
                                )}
                              </div>
                              {/* The cheapest column gets a rule under it and the only
                                  colour on the row, so the answer reads before any number. */}
                              <div className="w-full text-center" style={{
                                borderTop: isBest ? "2px solid var(--t-accent)" : "2px solid var(--t-line)",
                                paddingTop: 7, height: 64 }}>
                                <div className={`text-[15.5px] font-bold ${isBest ? "text-[var(--t-accent)]" : text}`}>{t.tier}%</div>
                                <div className={`text-[14px] tabular-nums ${muted}`}>
                                  {t.achievedService != null ? `${t.achievedService}% stayed in stock` : "-"}
                                </div>
                                {isBest && <div className={`text-[14px] font-bold text-[var(--t-accent)] mt-0.5`}>CHEAPEST</div>}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  );
                })()}

                {ta.priced && ta.tiers?.length > 0 && (
                  <div className={`${card} border rounded-2xl overflow-hidden`}>
                    <div className={`px-4 py-2 text-[14px] uppercase tracking-widest font-bold ${muted} border-b border-[var(--t-line)]`}>Protection levels compared</div>
                    <table className="w-full">
                      <thead><tr>
                        <Th tip="Service-level target. A higher level keeps more spare stock on hand.">Level</Th>
                        <Th tip="How often the buffer would have prevented a stockout in past reorder cycles.">Stayed in stock</Th>
                        <Th tip="Projected yearly cost = profit lost on sales you'd miss + cost of holding the safety buffer + cost of the extra stock carried before holidays. Lowest wins.">Est. cost / yr</Th>
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
                                  <tr key={t.tier} className={`border-t border-[var(--t-line)] ${isBest ? ("bg-[var(--t-good-soft)]") : ""}`}>
                                    <td className={`px-3 py-2 text-[15px] font-bold ${text}`}>Everything at {t.tier}%{isBest && <span className={`ml-2 text-[13px] font-semibold px-1.5 py-0.5 rounded-full bg-[var(--t-good-soft)] text-[var(--t-good)]`}>in use</span>}</td>
                                    <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${tone(lm, lvlSvc(t.achievedService, t.tier))}`}>{t.achievedService}%</td>
                                    <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${isBest ? tone(lm, "good") : text}`}>{money(t.totalCost)}</td>
                                  </tr>
                                );
                              })}
                              {mp?.totalCost != null && (
                                <tr className={`border-t-2 border-[var(--t-line2)] ${mixWins ? ("bg-[var(--t-good-soft)]") : ""}`}>
                                  <td className={`px-3 py-2 text-[15px] font-bold ${text}`}
                                      title="Each product on its own best level instead of one level for everything. Costed fairly: each simulated reorder is scored with a level chosen before seeing its outcome.">
                                    A level per product{mixWins && <span className={`ml-2 text-[13px] font-semibold px-1.5 py-0.5 rounded-full bg-[var(--t-good-soft)] text-[var(--t-good)]`}>in use</span>}
                                    <div className={`text-[13px] font-normal ${muted}`}>
                                      {Object.entries(mp.tierCounts || {}).filter(([, n]) => n).map(([p, n]) => `${n} at ${p}%`).join(" · ")}
                                    </div>
                                  </td>
                                  <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${text}`}>{mp.achievedService != null ? `${mp.achievedService}%` : "-"}</td>
                                  <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${mixWins ? tone(lm, "good") : text}`}>
                                    {money(mp.totalCost)}
                                    {mixWins && <span className={`ml-1.5 text-[13px] font-semibold text-[var(--t-good)]`}>saves {money(bestUniformCost - mp.totalCost)}/yr</span>}
                                  </td>
                                </tr>
                              )}
                            </>
                          );
                        })()}
                      </tbody>
                    </table>
                    {ta.mixedPolicy?.totalCost != null && (
                      <div className={`px-4 py-2 text-[14px] leading-relaxed ${muted} border-t border-[var(--t-line)]`}>
                        <span className="font-semibold">A level per product</span> = each product on its own best level instead of one shared level. The row marked <span className="font-semibold">in use</span> is applied automatically.
                      </div>
                    )}
                  </div>
                )}
                {/* Money totals are sums over costed products only. Missing one doesn't
                    just omit its row — it moves the totals and can flip which level wins. */}
                {ta.priced && ta.uncostedSkus?.length > 0 && (
                  <div className={`text-[15px] rounded-xl border px-4 py-3 bg-[var(--t-warn-soft)] border-[var(--t-warn-line)] text-[var(--t-warn)]`}>
                    <span className="font-semibold">These costs are incomplete, so the totals above will change.</span>{" "}
                    {ta.costedSkus} of {ta.totalSkus} tested products have a unit cost. The yearly figures count only
                    those {ta.costedSkus}, so adding the rest may change which level wins.
                    <div className="mt-1.5">
                      <span className="font-semibold">Missing a cost:</span>{" "}
                      <span className="font-mono">{ta.uncostedSkus.slice(0, 12).join(", ")}{ta.uncostedSkus.length > 12 ? ` +${ta.uncostedSkus.length - 12} more` : ""}</span>
                    </div>
                    <div className={`mt-1.5 text-[var(--t-warn)]`}>
                      Add them on each product&apos;s page or in the Fleet tab&apos;s Costs &amp; fees sheet, and the test re-runs by itself. Accuracy results don&apos;t depend on cost.
                    </div>
                  </div>
                )}
                {!ta.priced && (
                  <div className={`text-[15px] rounded-xl border px-4 py-3 bg-[var(--t-warn-soft)] border-[var(--t-warn-line)] text-[var(--t-warn)]`}>
                    <span className="font-semibold">No unit costs yet, so there are no dollar figures.</span> Accuracy results are still
                    valid, but without costs a lost sale can&apos;t be weighed against the cost of holding stock. Add costs on the
                    product&apos;s page or in the Fleet tab&apos;s Costs &amp; fees sheet (or include a <span className="font-semibold">Cost</span> column
                    in your file) and the test re-runs by itself.
                  </div>
                )}
              </div>
            );
          })()}

          {/* ───────────────── ANALYST VIEW ───────────────── */}
          {viewMode === "analyst" && (<>

          {/* The analyst view is one grid: a summary tab carrying the tier decision, then
              one tab per metric of the product × tier matrix. What used to surround it is
              gone — a per-product recommendation table that restated the matrix, a caveats
              section (now a strip at the top of the tab, read before the numbers rather
              than after), four diagnostic accuracy tables, and an AI write-up. */}
          {res.tierAnalysis?.tiers?.length > 0 && (() => {
            const ta = res.tierAnalysis;
            const money = (v) => v == null ? "-" : `$${Math.round(v).toLocaleString()}`;
            const best = ta.bestTier;
            const cyc = ta.cyclesPerYear;
            const basisNote = !ta.priced ? "no per-SKU cost, calibration only"
              : ta.costBasis === "actual" ? `real cost · ${ta.totalSkus} tested SKUs`
              : `real cost · ${ta.costedSkus}/${ta.totalSkus} tested SKUs (totals incomplete)`;
            const lostProfitTip = `Profit you'd lose to stockouts each year at this tier, across the catalog (costed SKUs only). It's simulated: at each test date we assume you ordered forecast + this tier's buffer and count the demand it wouldn't cover over the lead time, not the stockouts in your history. Worked out per product (units short/yr × its profit: price − cost − fees), then summed; see the per-product tabs.`;

            const bySku = ta.bySku || [];
            const tiers = ta.tiers.map((t) => t.tier);
            const thc = `text-right text-[14px] uppercase tracking-widest font-bold ${muted} px-3 py-2`;
            const colHL = (t) => t === best ? ("bg-[var(--t-good-soft)]") : "";

            const METRICS = [
              { key: "unitsYr",       label: "Missed units / yr",   fmt: (v) => Math.round(v).toLocaleString(), total: (t) => t.unitsShortYr, round: true },
              { key: "profitYr",      label: "Lost profit / yr",    fmt: money, total: (t) => t.stockoutCost },
              { key: "safetyUnits",   label: "Buffer units",        fmt: (v) => Math.round(v).toLocaleString(), total: (t) => t.safetyUnits, round: true },
              { key: "bufferCash",    label: "Cash in buffer",      fmt: money, total: (t) => t.bufferCash },
              { key: "holdingCostYr", label: "Buffer holding / yr", fmt: money, total: (t) => t.holdingCost },
              { key: "holidayCostYr", label: "Holiday cover / yr",  fmt: money, total: (t) => t.holidayCost },
            ];

            /* A product that never ran short in ANY window forfeits no profit at any tier,
               so the cheapest buffer wins by arithmetic rather than by fit. Marking that as
               "its own best level" would assert a pick this run never made. */
            const unranked = (r) => !tiers.some((t) => (r.tiers[t]?.profitYr || 0) > 0);

            // Magnitude as a neutral wash, so the shape of a row reads without reading its
            // digits. Deliberately grey: these are quantities, half good when large.
            const shade = (v, lo, hi) => {
              if (v == null || !(hi > lo)) return "";
              const f = (v - lo) / (hi - lo);
              if (f < 0.2) return "";
              return f > 0.75 ? "bg-[var(--t-line)]" : "bg-[var(--t-sunken)]";
            };
            const rowRange = (r, key) => {
              const vs = tiers.map((t) => r.tiers[t]?.[key]).filter((v) => v != null);
              return vs.length ? [Math.min(...vs), Math.max(...vs)] : [0, 0];
            };

            const hcv = res.holidayCover;
            const TABS = [{ key: "summary", label: "Calibration & annual cost" },
                          ...(hcv?.byHoliday?.length ? [{ key: "holidays", label: "Holiday weeks" }] : []),
                          ...METRICS.map((m) => ({ key: m.key, label: m.label }))];
            const activeKey = TABS.some((t) => t.key === matrixMetric) ? matrixMetric : "summary";
            const metric = METRICS.find((m) => m.key === activeKey);

            return (
              <div className={`${card} border rounded-2xl overflow-hidden`}>
                <div className={`px-4 py-2 flex items-center justify-between gap-3 flex-wrap border-b border-[var(--t-line)]`}>
                  <span className={`text-[14px] uppercase tracking-widest font-bold ${muted}`}>Protection tiers</span>
                  <span className={`text-[14px] ${muted}`}>{basisNote}{ta.assumptions?.holdingPct != null ? ` · ${ta.assumptions.holdingPct}%/yr holding` : ""} · {cyc} cycles/yr</span>
                </div>

                <div className={`flex flex-wrap gap-1 px-4 py-2.5 border-b border-[var(--t-line)]`}>
                  {TABS.map((t) => (
                    <button key={t.key} onClick={() => setMatrixMetric(t.key)}
                      className={`px-2.5 py-1 rounded-full text-[14px] font-semibold transition-colors ${
                        t.key === activeKey
                          ? ("bg-[var(--t-btn-bg)] text-[var(--t-btn-fg)]")
                          : ("bg-[var(--t-sunken)] text-[var(--t-soft)] hover:bg-[var(--t-line)]")}`}>
                      {t.label}
                    </button>
                  ))}
                </div>

                {activeKey === "summary" ? (<>
                  {/* The continuous optimum — the tiers are just dial settings around it */}
                  {ta.criticalRatio && (() => {
                    const cr = ta.criticalRatio, nt = (ta.tiers || []).find((t) => t.tier === ta.nearestTier);
                    const disagree = ta.nearestTier != null && ta.bestTier != null && ta.nearestTier !== ta.bestTier;
                    return (
                      <div className={`px-4 py-3 border-b text-[15px] leading-relaxed border-[var(--t-line)] bg-[var(--t-accent-soft)] text-[var(--t-soft)]`}>
                        <span className="font-semibold" title={TIP.optimal}>Optimal in-stock rate from your margins: ~{cr.optimalService}%.</span>
                        {nt && <> The {nt.tier}% tier lands closest (achieves {nt.achievedService}%).</>}
                        {" "}<span title={TIP.achievedVsTarget}>Judge tiers on <span className="font-semibold">Achieved</span>, not their label.</span>
                        {disagree && <> <span className="font-semibold">Note:</span> cheapest says {ta.bestTier}%, closest-to-optimal says {ta.nearestTier}%. Prefer {ta.nearestTier}% unless the cost gap below is decisive.</>}
                      </div>
                    );
                  })()}

                  {/* Is the ranking real, or a coin flip? */}
                  {ta.ranking && !ta.ranking.decisive && (
                    <div className={`px-4 py-3 border-b text-[15px] leading-relaxed border-[var(--t-warn-line)] bg-[var(--t-warn-soft)] text-[var(--t-warn)]`}>
                      <span className="font-semibold">Cost tie:</span> {ta.ranking.winner}% and {ta.ranking.runnerUp}% are within noise of each other ({ta.ranking.winnerProb}% vs {ta.ranking.runnerUpProb}% of resamples), so the pick falls back to closest-to-optimal.
                    </div>
                  )}
                  <table className="w-full">
                    <thead><tr>
                      <Th tip="The service-level target. A higher tier carries more safety stock.">Tier</Th>
                      <Th tip="Share of lead-time windows the tier actually covered, averaged per product so long histories don't outweigh short ones. Should be close to the tier %. Well below means the buffer is under-sized at that tier, not that the tier is wrong.">Achieved</Th>
                      <Th tip={TIP.ci}>95% CI</Th>
                      <Th tip="Achieved minus the tier's target. Negative means under-protecting, but only if the target falls outside the 95% CI.">vs target</Th>
                      {ta.ranking && <Th tip={TIP.pcheapest}>P(cheapest)</Th>}
                      {/* Ordered as two halves of one trade-off, each running units -> money:
                            what stockouts cost you, then what protecting against them costs.
                            "Buffer units" and "Cash in buffer" are the same quantity in two
                            units and now sit together — they used to be five columns apart. */}
                      <Th tip="Units of demand per year you'd fail to cover at this level, across the catalog. This is the quantity the lost profit is built from: missed units x profit per unit.">Missed units / yr</Th>
                      <Th tip={lostProfitTip}>Lost profit / yr</Th>
                      <Th tip="Spare units you'd carry across the catalog at this level. This is the quantity the holding cost is built from: buffer units x unit cost x holding rate.">Buffer units</Th>
                      <th className={`${th} border-l border-[var(--t-line)]`} title="Cash tied up in the safety buffer at this tier (buffer units × unit cost). A one-time amount, not a yearly cost, and not added to Total; its yearly cost is the next column. The sub-line is the cash difference vs the recommended tier.">
                        <span className="underline decoration-dotted decoration-[var(--t-faint)] underline-offset-4 cursor-help">Cash in buffer</span>
                      </th>
                      <Th tip="Yearly cost of holding the safety buffer: Cash in buffer × your holding rate. Covers tied-up capital, storage, insurance and obsolescence. Rises with the tier, since more stock costs more to hold.">Buffer holding / yr</Th>
                      <Th tip="Yearly cost of the extra stock carried before holidays (Holiday weeks tab). It's a temporary top-up bought before each holiday and sold down after, so each holiday costs one order cycle of holding, not a year's.">Holiday cover / yr</Th>
                      <Th tip="Lost profit/yr + Buffer holding/yr + Holiday cover/yr. The lowest total is the most profitable tier. Cash in buffer is not included: it's capital tied up, not a yearly expense.">Total $/yr</Th>
                    </tr></thead>
                    <tbody>
                      {ta.tiers.map((t) => {
                        const isBest = t.tier === best;
                        const rowBg = isBest ? ("bg-[var(--t-good-soft)]") : "";
                        return (
                          <tr key={t.tier} className={`border-t border-[var(--t-line)] ${rowBg}`}>
                            <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${text}`}>
                              {t.tier}%{isBest && <span className={`ml-2 text-[13px] font-semibold px-1.5 py-0.5 rounded-full bg-[var(--t-good-soft)] text-[var(--t-good)]`}>lowest cost</span>}
                            </td>
                            <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${tone(lm, lvlSvc(t.achievedService, t.tier))}`}>{t.achievedService}%</td>
                            <td className={`px-3 py-2 text-[14px] tabular-nums ${muted}`}>{ciText(t.achievedServiceCI, "%") || "-"}</td>
                            {/* An apparent gap is only evidence when the tier's own target falls
                                outside the interval; otherwise it reads as "too close to call". */}
                            <td className={`px-3 py-2 text-[15px] tabular-nums ${ciCovers(t.achievedServiceCI, t.tier) ? muted : t.gap < -7 ? tone(lm, "bad") : t.gap < 0 ? tone(lm, "warn") : tone(lm, "good")}`}
                                title={ciCovers(t.achievedServiceCI, t.tier) ? "The tier's target falls inside the confidence interval, so there aren't enough windows to call this a real gap." : undefined}>
                              {t.gap > 0 ? "+" : ""}{t.gap}
                              {ciCovers(t.achievedServiceCI, t.tier) && <span className={`ml-1 text-[13px] ${muted}`}>n/s</span>}
                            </td>
                            {ta.ranking && (() => {
                              const pc = ta.ranking.pCheapest?.[t.tier] ?? ta.ranking.pCheapest?.[String(t.tier)];
                              return (
                                <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${pc == null ? muted : pc >= 60 ? tone(lm, "good") : pc >= 25 ? tone(lm, "warn") : muted}`}>
                                  {pc == null ? "-" : `${pc}%`}
                                </td>
                              );
                            })()}
                            <td className={`px-3 py-2 text-[15px] font-semibold tabular-nums ${text}`}>{t.unitsShortYr == null ? "-" : Math.round(t.unitsShortYr).toLocaleString()}</td>
                            <td className={`px-3 py-2 text-[15px] tabular-nums ${muted}`}>{money(t.stockoutCost)}</td>
                            <td className={`px-3 py-2 text-[15px] tabular-nums ${muted}`}>{t.safetyUnits == null ? "-" : Math.round(t.safetyUnits).toLocaleString()}</td>
                            <td className={`px-3 py-2 tabular-nums align-top border-l border-[var(--t-line)]`}>
                              <div className={`text-[15px] font-semibold ${text}`}>{money(t.bufferCash)}</div>
                              <div className={`text-[13px] ${muted}`}>{isBest ? "baseline" : t.bufferCashDelta == null ? "" : `${t.bufferCashDelta > 0 ? "+" : "−"}$${Math.round(Math.abs(t.bufferCashDelta)).toLocaleString()} vs ${best}%`}</div>
                            </td>
                            <td className={`px-3 py-2 text-[15px] tabular-nums ${muted}`}>{money(t.holdingCost)}</td>
                            <td className={`px-3 py-2 text-[15px] tabular-nums ${muted}`}>{money(t.holidayCost)}</td>
                            <td className={`px-3 py-2 text-[15px] font-bold tabular-nums ${isBest ? tone(lm, "good") : text}`}>{money(t.totalCost)}</td>
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
                            <tr className={`border-t border-[var(--t-line)]`}>
                              <td colSpan={ta.ranking ? 12 : 11} className={`px-3 py-2 text-[14px] ${muted}`}
                                  title="Each product's own best level was tried and scored on unseen weeks. It cost more than the best single level, so one level is used for everything.">
                                A custom level per product was tested ({spread}): {money(mp.totalCost)}/yr, no better than the best single level. One level is in use for everything.
                              </td>
                            </tr>
                          );
                        }
                        return (
                          <tr className={`border-t-2 border-[var(--t-line2)] bg-[var(--t-good-soft)]`}>
                            <td className={`px-3 py-2.5 text-[15px] font-bold ${text}`}>
                              Per-product mix
                              <span className={`ml-2 text-[13px] font-semibold px-1.5 py-0.5 rounded-full bg-[var(--t-good-soft)] text-[var(--t-good)]`}>in use</span>
                              <div className={`text-[13px] font-normal mt-0.5 ${muted}`}>{spread}</div>
                            </td>
                            <td className={`px-3 py-2.5 text-[15px] ${muted}`} colSpan={ta.ranking ? 6 : 5}>
                              Each product at its own best level.
                            </td>
                            <td className={`px-3 py-2.5 text-[15px] font-bold tabular-nums ${tone(lm, "good")}`}>{money(mp.totalCost)}</td>
                            <td className={`px-3 py-2.5 text-[14px] tabular-nums ${muted}`} colSpan={3}>
                              saves {money(saves)}/yr
                            </td>
                          </tr>
                        );
                      })()}
                    </tbody>
                  </table>

                  <div className={`px-4 py-2 text-[14px] ${muted} border-t border-[var(--t-line)]`}>
                    Dollars here are just units × a rate; judge tiers by lowest <span className="font-semibold">Total $/yr</span>. Details under the guide below.
                  </div>
                  <Guide id="tiers" cols={["tier","achieved","ci","vstarget","pcheap","bufunits","missunits","lostprofit","buffercost","totalcost","cash"]}
                    extra={[
                      ...(ta.mixedPolicy ? [["Per-product mix", "Every product on its own cheapest level instead of one shared level. Scored fairly: each reorder is graded with a level picked from that product's other test windows, so lucky results can't win it. Hindsight scoring would have claimed " + money(ta.mixedPolicy.inSampleTotal) + "/yr.", "If it beats the best single level, the app uses per-product levels; if not, every product gets the winning single level. Applied automatically either way."]] : []),
                      ["How the dollars are built", "Lost profit/yr = missed units/yr × profit per unit (price − cost − fees), summed per product. Buffer holding/yr = buffer units × unit cost × your holding rate. Holiday cover/yr = the extra stock carried before each holiday × unit cost × holding rate × one order cycle. The shortfall is simulated per tier (would forecast + that tier's buffer have covered the next lead time's real demand?), not the stockouts in your history.", "Costs cover only the safety-stock policy, the part the tier changes."],
                      ["Cash in buffer vs Buffer holding", "Cash in buffer is one-time working capital parked in safety stock. It's not a yearly cost and not added to Total; its yearly cost is the Buffer holding column (cash × holding rate).", "Use it to judge affordability, not to rank tiers."],
                    ]} />
                </>) : activeKey === "holidays" ? (
                  <HolidayWeeksTable hc={hcv} lm={lm} />
                ) : bySku.length === 0 ? (
                  <div className={`px-4 py-4 text-[15px] ${muted}`}>No per-product breakdown in this run.</div>
                ) : (
                  <div>
                    <div className={`px-4 pt-3 pb-1 text-[15px] uppercase tracking-widest font-bold ${text}`}>
                      Every product at every tier
                      <span className={`ml-2 font-normal normal-case tracking-normal ${muted}`}>· {metric.label}</span>
                    </div>
                    <div className="overflow-x-auto">
                      <table className="w-full">
                        <thead><tr>
                          <th className={th} title="The product this row is about."><span className="underline decoration-dotted decoration-[var(--t-faint)] underline-offset-4 cursor-help">Product</span></th>
                          {tiers.map((t) => <th key={t} className={`${thc} ${colHL(t)}`} title={`This product if you ran it at the ${t}% protection level.`}><span className="underline decoration-dotted decoration-[var(--t-faint)] underline-offset-4 cursor-help">{t}%</span></th>)}
                        </tr></thead>
                        <tbody>
                          {bySku.map((r) => {
                            const [lo, hi] = rowRange(r, metric.key);
                            /* The per-product recommendation table is gone, so the cell
                               outline is the only thing left carrying each product's own
                               cheapest level — the figure the client view's "a level per
                               product saves $X/yr" claim rests on. */
                            const own = unranked(r) ? null : r.bestTier;
                            return (
                              <tr key={r.sku} className={"border-t border-[var(--t-line)]"}>
                                <td className={`px-3 py-2 text-[16.5px] font-semibold ${text}`}>
                                  {r.sku}
                                  {!r.costKnown && <span className={`ml-1.5 text-[13px] ${muted}`}>(no cost)</span>}
                                  {r.lossMaking && <span className={`ml-1.5 text-[13px] font-semibold text-[var(--t-warn)]`}>≤ cost</span>}
                                  {own == null && <span className={`ml-1.5 text-[13px] ${muted}`} title="This product never ran short in any test window, so every tier costs it the same in lost profit and this run can't rank tiers for it.">never short</span>}
                                </td>
                                {tiers.map((t) => { const v = r.tiers[t]?.[metric.key]; const isOwn = t === own; return (
                                  <td key={t}
                                      style={isOwn ? { boxShadow: "inset 0 0 0 1.5px var(--t-good-line)" } : undefined}
                                      title={isOwn ? `${r.sku}'s own cheapest level` : undefined}
                                      className={`px-3 py-2 text-[16.5px] tabular-nums text-right ${v == null ? muted : text} ${shade(v, lo, hi)} ${colHL(t)}`}>
                                    {v == null ? "-" : metric.fmt(v)}
                                  </td>
                                ); })}
                              </tr>
                            );
                          })}
                          <tr className={`border-t-2 border-[var(--t-line2)] bg-[var(--t-sunken)]`}>
                            <td className={`px-3 py-2.5 text-[16.5px] font-bold ${text}`}>Total</td>
                            {ta.tiers.map((t) => { const v = metric.total(t); return (
                              <td key={t.tier} className={`px-3 py-2.5 text-base font-bold tabular-nums text-right ${text} ${colHL(t.tier)}`}>{v == null ? "-" : metric.fmt(v)}</td>
                            ); })}
                          </tr>
                        </tbody>
                      </table>
                    </div>
                    <div className={`px-4 py-2 text-[14px] leading-relaxed ${muted}`}>
                      Sorted by exposure; darker shading means a bigger value within the row.
                      The {best ? `${best}%` : "recommended"} column is the catalog-wide pick, and an outlined cell is that product's own cheapest level.
                      <span className="font-semibold"> Lost profit</span> and <span className="font-semibold">Buffer holding</span> totals match the Calibration tab and add up to Total $/yr;
                      <span className="font-semibold"> Cash in buffer</span> is one-time working capital, not a yearly cost.
                    </div>
                  </div>
                )}
              </div>
            );
          })()}

          {res.tierAnalysis?.lossMakingSkus?.length > 0 && (
            <div className={`text-[15px] rounded-xl border px-4 py-3 bg-[var(--t-warn-soft)] border-[var(--t-warn-line)] text-[var(--t-warn)]`}>
              <span className="font-semibold">Priced at or below cost:</span> {res.tierAnalysis.lossMakingSkus.join(", ")}. These lose money on every sale, so a stockout costs no profit and they&apos;re <span className="font-semibold">excluded from the cost comparison</span>. It&apos;s a pricing issue, not an inventory one: review the price or cost on the product&apos;s page.
            </div>
          )}

          {/* Accuracy, pared to the one table that answers the question. The by-SKU,
              by-training-window, by-block and baseline tables were diagnostics for a
              question this table already answers. */}
          <div className={`${card} border rounded-2xl overflow-hidden`}>
            <div className={`px-4 py-2 flex items-center justify-between gap-3 flex-wrap border-b border-[var(--t-line)]`}>
              <span className={`text-[16.5px] font-bold ${text}`}>How accurate is the forecast?</span>
              <span className={`text-[14px] ${muted}`}>by engine · {res.tested}/{res.skus} products · {res.forecasts} tests</span>
            </div>
            <div className={`px-4 py-2 text-[14px] ${muted} leading-relaxed border-b border-[var(--t-line)]`}>
              <span className="font-semibold">vs naive</span> under 1 is good, <span className="font-semibold">runs high/low</span> near 0 is unbiased,{" "}
              <span className="font-semibold">band hit rate</span> should sit near 80%. <span className="font-semibold">Avg miss</span> runs high on slow sellers
              whatever the model, so lean on <span className="font-semibold">vs naive</span> there.
            </div>
            <table className="w-full"><Head /><tbody>
              {(res.byEngine || []).map((r) => <Row key={r.engine} r={r} name={r.engine} />)}
              {res.overall?.forecasts ? <Row r={res.overall} name="ALL (overall)" /> : null}
            </tbody></table>
            <Guide id="engine" cols={["tests","avgmiss","vsnaive","runs","bandhit","instock","ordersize"]} />
          </div>

          </>)}
        </div>
      )}
    </div>
  );
}

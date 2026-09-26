import { posInt, namedSuppliers, buildScorecardBody } from './lib/helpers';
import { useState, useRef, useEffect } from "react";
import { loadStorage, saveStorage } from "./lib/storage";
import { GROQ_URL, GROQ_MODEL_CHAT, GROQ_NO_TOOLS, groqToolsRan,
         checkGroqModels, groqHealthMessage, recordGroqUsage, usageSummary } from "./lib/ai";

// ─────────────────────────────────────────────
// FULL CONTEXT BUILDER  (folder + supplier aware)
// ─────────────────────────────────────────────
/** One line per product, for EVERY product, always sent.
 *
 *  The fleet used to be described only by the full 14-section block below, for all 32
 *  products, on every message: roughly 10,000 tokens a question against a free-tier
 *  budget of 8,000 a minute. Most of it is drill-down — which engine the router picked
 *  and why, how the conformal band was built, the protection cost curve — which matters
 *  when someone asks about ONE product and is dead weight when they ask what to order.
 *
 *  So: a roster line for everything, full detail only where it is needed. The roster is
 *  what stops that from becoming a blind spot; a product the question did not name is
 *  still visible, with the facts that decide whether it belongs in an answer at all. */
function rosterLine(s, sc, po, params) {
  const cover = !sc ? "?" : sc.daysOfCover == null ? "365+" : `${sc.daysOfCover}d`;
  const ro = s.daysUntilReorder === -1 ? "n/a" : s.daysUntilReorder <= 0 ? "OVERDUE" : `${s.daysUntilReorder}d`;
  return [
    `${s.skuName} (${s.skuId})`,
    sc ? sc.status : "unrated",
    `stock ${params.stock?.toLocaleString?.() ?? params.stock}`,
    `cover ${cover}`,
    `reorder ${ro}`,
    `order ${s.orderQty?.toLocaleString() ?? "?"}`,
    `${s.avgDailyDemand ?? "?"}/day`,
    s.demandVolatilityLabel || "volatility n/a",
    po ? `PO ${po.qty?.toLocaleString()} due ${po.delivery ?? "?"}` : "no PO",
    sc?.returnTier && sc.returnTier !== "—" ? `grade ${sc.returnTier}` : "",
  ].filter(Boolean).join(" | ");
}


/** Which products deserve the full 14-section block for THIS question.
 *
 *  Three sources, deliberately generous, because the cost of including a product that
 *  turns out to be irrelevant is a few hundred tokens, while the cost of omitting one
 *  the user asked about is a confidently thin answer:
 *    1. anything the question names, by product name or SKU id
 *    2. anything not Healthy, so "what needs attention" works without naming anything
 *    3. the most urgent by reorder date, to fill up to the cap when the first two are thin
 *
 *  Matching is loose on purpose: lowercase, and a product counts as named if the
 *  question contains its id or any word of its name four characters or longer. "Harlow"
 *  pulls in every Harlow product, which is the behaviour someone typing that expects. */
function pickDetailSkus(question, skuForecasts, scorecard, cap = 3) {
  // 3, and the reason is a hard external ceiling rather than taste.
  //
  // groq/compound-mini advertises 70,000 tokens per minute, but it is a ROUTER: it
  // dispatches to sub-models (openai/gpt-oss-120b, llama-3.3-70b-versatile) that carry
  // their own free-tier limits, and gpt-oss-120b's is 8,000 TPM. Probing it directly,
  // requests up to ~6,400 real tokens returned 200 and ~9,500 came back 429 naming the
  // SUB-MODEL, not compound. So the usable per-request budget is ~8,000 tokens, not
  // 70,000, and the headline figure is unreachable for a single large call.
  //
  // A detail block renders at ~2,900 characters (~725 tokens). Budget for one question:
  //   roster, every product      ~1,050 tokens
  //   3 detail blocks            ~2,175
  //   preamble and instructions    ~500
  //   compound-mini's own scaffold ~449   (measured: a 30-token prompt bills as 449)
  //   recent chat turns            ~200
  //   room for the reply           ~700
  //                              -------
  //                              ~5,100 tokens, with ~2,900 of headroom.
  // At 6 blocks the same sum reaches ~7,250, which is inside the ceiling on paper and
  // over it in practice once a question or a reply runs long. That margin is what made
  // the drawer fail intermittently rather than consistently.
  //
  // On a paid tier this can go straight back to 6, or higher.
  const q = (question || "").toLowerCase();
  const scMap = {};
  (scorecard?.rows || []).forEach(r => { scMap[r.skuId] = r; });

  // Score rather than a yes/no match. A flat "any word of 4+ letters" test pulled 14 of
  // 32 products for "why is the Harlow 24in Vanity forecast so low", because in this
  // catalogue "vanity" is a category word that half the products share. Counting HOW
  // MANY of the question's words a product matches ranks the one actually asked about
  // above its siblings, and a word shared by most of the catalogue stops being evidence.
  const common = new Map();
  skuForecasts.forEach(s => {
    new Set(String(s.skuName || "").toLowerCase().split(/[^a-z0-9]+/).filter(w => w.length >= 4))
      .forEach(w => common.set(w, (common.get(w) || 0) + 1));
  });
  const tooCommon = Math.max(2, Math.ceil(skuForecasts.length * 0.25));

  const scored = [];
  const needy = [];
  const rest  = [];
  skuForecasts.forEach(s => {
    const id = String(s.skuId || "");
    let score = 0;
    if (id && q.includes(id.toLowerCase())) score += 100;          // an id is unambiguous
    new Set(String(s.skuName || "").toLowerCase().split(/[^a-z0-9]+/).filter(w => w.length >= 4))
      .forEach(w => { if (q.includes(w)) score += (common.get(w) >= tooCommon ? 1 : 5); });
    if (score > 0) { scored.push([score, s]); return; }
    const st = scMap[id]?.status;
    if (st && st !== "Healthy") needy.push(s); else rest.push(s);
  });
  const named = new Set(
    scored.sort((a, b) => b[0] - a[0]).slice(0, cap).map(([, s]) => String(s.skuId)));

  // Soonest reorder first; -1 means "no reorder needed", so it sorts last.
  const byUrgency = (a, b) => {
    const x = a.daysUntilReorder === -1 ? 1e9 : a.daysUntilReorder;
    const y = b.daysUntilReorder === -1 ? 1e9 : b.daysUntilReorder;
    return x - y;
  };
  const out = new Set(named);
  [...needy.sort(byUrgency), ...rest.sort(byUrgency)].forEach(s => {
    if (out.size < cap) out.add(String(s.skuId));
  });
  return out;
}


function buildFleetContext(skuForecasts, openPOs, skuParams, skuList, folders, suppliers, scorecard, groups, history, detailFor = null) {
  if (!skuForecasts || skuForecasts.length === 0) return "No SKU data loaded yet.";
  // null = every product in full, which is what the reorder brief still wants.
  const wantsDetail = (id) => detailFor === null || detailFor.has(id);

  const DEFAULT_PARAMS = { stock: 500, leadTime: 14, coverage: 30, strategy: "balanced", months: 1 };

  // Map of skuId -> scorecard row (inventory-health metrics), if loaded.
  const scMap = {};
  (scorecard?.rows || []).forEach(r => { scMap[r.skuId] = r; });

  // Map of skuId -> classification/relatedness row (attributes, group, similar), if loaded.
  const grpMap = {};
  (groups?.skus || []).forEach(r => { grpMap[r.skuId] = r; });

  // ── 1. Folder structure section ──
  let folderSection = "";
  if (folders && Object.keys(folders).length > 0) {
    const lines = ["=== FOLDER STRUCTURE ==="];
    Object.entries(folders).forEach(([, f]) => {
      const skuNames = f.skuIds.map(id => {
        const meta = skuList?.find(x => x.id === id);
        return meta ? `${meta.name} (${id})` : id;
      });
      lines.push(`Folder: "${f.name}" → ${skuNames.length > 0 ? skuNames.join(", ") : "(empty)"}`);
    });

    // Ungrouped
    const allFoldered = new Set(Object.values(folders).flatMap(f => f.skuIds));
    const ungrouped   = (skuList || []).filter(s => !allFoldered.has(s.id));
    if (ungrouped.length > 0) {
      lines.push(`Ungrouped → ${ungrouped.map(s => `${s.name} (${s.id})`).join(", ")}`);
    }
    lines.push("=== END FOLDER STRUCTURE ===\n");
    folderSection = lines.join("\n");
  }

  // ── 2. Per-SKU data ──
  // Every product, one line each. Built before the detail blocks so that the roster is
  // never the thing that gets dropped when something has to give.
  const roster = [
    "=== FLEET ROSTER (every product) ===",
    "name (id) | status | stock | days of cover | reorder in | order qty | rate | volatility | open PO | profit grade",
    ...skuForecasts.map(s => rosterLine(
      s, scMap[s.skuId], openPOs?.[s.skuId], skuParams?.[s.skuId] ?? DEFAULT_PARAMS)),
    "=== END FLEET ROSTER ===\n",
  ].join("\n");

  const skus = skuForecasts.map(s => {
    if (!wantsDetail(s.skuId)) return "";
    const po     = openPOs?.[s.skuId];
    const params = skuParams?.[s.skuId] ?? DEFAULT_PARAMS;
    const meta   = skuList?.find(x => x.id === s.skuId);
    // Find which folder this SKU belongs to
    const folderEntry = folders
      ? Object.entries(folders).find(([, f]) => f.skuIds.includes(s.skuId))
      : null;
    const folderLabel = folderEntry ? `"${folderEntry[1].name}"` : "Ungrouped";

    const lines = [
      `━━━ SKU: ${s.skuName} (ID: ${s.skuId}) ━━━`,

      // Source
      meta?.mode === "uploaded"
        ? `Source: uploaded file (${s.filename || "unknown"})`
        : `Source: not from a file`,

      // Folder membership
      `Folder: ${folderLabel}`,

      // Parameters the user has set
      `\n[User Parameters]`,
      `  Units in stock: ${params.stock.toLocaleString()}`,
      `  Lead time: ${params.leadTime} days`,
      `  Coverage window: ${params.coverage} days`,
      `  Confidence strategy: ${params.strategy} (${params.strategy === "lean" ? "70%" : params.strategy === "balanced" ? "80%" : "90%"} CI)`,

      // Model info
      `\n[Model & Data Quality]`,
      `  Days of history: ${s.daysOfHistory ?? "N/A"}, total sold: ${s.totalUnitsSold?.toLocaleString() ?? "N/A"}`,
      `  Data eligibility: ${s.status ?? "N/A"} — ${s.statusMessage ?? "N/A"}`,
      `  Seasonality: yearly ${s.activeYearly ? "yes" : "no"}, weekly ${s.activeWeekly ? "yes" : "no"}`,
      `  Price tracking: ${s.hasPrice ? (s.priceWellSampled ? "active (well sampled)" : "active (limited)") : "not available"}${s.priceModeled === false ? " — NOTE: price not used by this SKU's model" : ""}; current ${(s.currentPrice ?? s.lastPrice) ? `$${(s.currentPrice ?? s.lastPrice).toFixed(2)}` : "N/A"}; varied: ${s.priceVaried ? "yes" : "no"}`,
      `  Zero-stock days excluded from training: ${s.stockoutRowsDropped ?? 0}; forecast-reliability: ${s.reliabilityColor ?? "N/A"}`,

      // Product classification & relatedness (from the categorization step)
      (() => {
        const gr = grpMap[s.skuId];
        if (!gr) return "";
        const attrs = gr.attributes && Object.keys(gr.attributes).length
          ? Object.entries(gr.attributes).map(([k, v]) => `${k}=${v}`).join(", ") : "none yet (not categorized)";
        const sim = gr.similar?.length ? gr.similar.map(x => x.skuName).join(", ") : "none (orphan — forecasts from its own history)";
        return [
          `\n[Product Classification & Similar Products]`,
          `  Attributes: ${attrs}; Group: ${gr.group ?? "ungrouped"}`,
          `  Similar (can pool with): ${sim}; pooling into global model now: ${gr.usesGlobal ? "Yes" : "No"}`,
        ].join("\n");
      })(),

      // Demand volatility (under-the-hood residual analysis)
      `\n[Demand Volatility — Residual Analysis]`,
      `  Daily demand swing (σ, residual std): ${s.residualStd != null ? "±" + s.residualStd + " units/day" : "N/A"}`,
      // Dispersion, not the raw CV. CV is error divided by daily units, so it mostly
      // reports how few units a day something sells: below ~2.8/day it is always
      // "volatile" and below ~11/day it can never be "stable". Dividing by sqrt(units)
      // takes out that floor, so 1.0 means "as steady as this sales volume permits".
      `  Sales volatility (swing vs the minimum unavoidable at this volume): ${s.residualDispersion != null ? s.residualDispersion.toFixed(2) + "x" : "N/A"}${s.demandVolatilityLabel ? ` — ${s.demandVolatilityLabel}` : ""}${s.demandRefMean != null ? ` on ~${s.demandRefMean.toFixed(1)} units/day` : ""}`,
      `  Volatility classification: ${s.demandVolatilityColor ?? "N/A"} — ${s.demandVolatilityDesc ?? "N/A"}`,
      `  (Residual CV, engine diagnostic only — do NOT judge volatility from it, it mostly tracks how few units a day a product sells: ${s.residualCv != null ? (s.residualCv * 100).toFixed(1) + "%" : "N/A"})`,

      // Protection level (service level) — cost-driven when supported, margin/default fallback otherwise
      `\n[Protection Level — service level for the safety buffer]`,
      s.protection ? [
        `  Chosen: ${s.protection.label} (${s.protection.servicePct}% service level, z=${s.zScore?.toFixed(3) ?? "N/A"})`,
        `  Recommended (by the tool): ${(s.protection.options || []).find(o => o.key === s.protection.recommended)?.label ?? s.protection.recommended} (${(s.protection.options || []).find(o => o.key === s.protection.recommended)?.pct ?? "?"}%)`,
        `  Recommendation basis: ${s.protection.source === "backtest" ? "per-SKU backtest cheapest tier" : s.protection.source === "economics" ? "per-SKU expected cost curve" : s.protection.source === "margin" ? "margin heuristic" : "default (cost missing)"}`,
        `  Manager override active: ${s.protection.overridden ? "Yes — they picked a level different from the recommendation" : "No — using the recommendation"}`,
        `  Gross margin used: ${s.protection.marginPct != null ? s.protection.marginPct + "%" : "unknown (no unit cost → defaults to Standard 95%)"}`,
        s.protection.source === "backtest" && s.protection.economics ? `  Backtest tier: ${s.protection.economics.windows} tests, total cost $${s.protection.economics.totalCostYr}/yr, holding ${s.protection.economics.holdingPct}%/yr` : "",
        s.protection.source === "economics" && s.protection.economics ? `  Cost curve: margin $${s.protection.economics.marginUnit}/unit, holding ${s.protection.economics.holdingPct}%/yr, best ${(s.protection.options || []).find(o => o.key === s.protection.economics.best)?.label ?? s.protection.economics.best}` : "",
        `  Why: ${s.protection.reason}`,
      ].join("\n") : "  N/A (older backend)",

      // Forecast method — which engine the router picked for THIS sku
      `\n[Forecast Method]`,
      `  Engine used: ${s.winningModel ?? "N/A"}${s.route ? ` (route: ${s.route})` : ""}`,
      `  Demand pattern: ${s.demandClass ?? "N/A"}`,
      s.routeReason ? `  Why this method was chosen: ${s.routeReason}` : "",
      s.forecastDetails?.seasonality?.text ? `  Seasonality handled: ${s.forecastDetails.seasonality.text}` : "",
      s.forecastDetails?.price?.text ? `  Price handling: ${s.forecastDetails.price.text}` : "",
      s.forecastDetails?.interval?.text ? `  Forecast range (80% band): ${s.forecastDetails.interval.text}` : "",
      s.forecastDetails?.relatives != null ? `  Related products pooled from: ${s.forecastDetails.relatives}` : "",
      s.forecastDetails?.trend?.active
        ? `  Launch-ramp trend: ACTIVE — ${s.forecastDetails.trend.text}`
        : "",

      // Key forecast metrics
      `\n[Forecast & Order Metrics]`,
      `  Avg daily demand: ${s.avgDailyDemand} units/day`,
      `  Days until stockout: ${s.daysUntilStockout === -1 ? "N/A (sufficient)" : `${s.daysUntilStockout}`}; days until reorder: ${s.daysUntilReorder === -1 ? "N/A" : s.daysUntilReorder <= 0 ? "OVERDUE" : `${s.daysUntilReorder}`}`,
      `  Order math: target ${s.targetInventory ?? "?"} (coverage ${s.coverageQty ?? "?"} + safety ${s.safetyStock ?? "?"}) − stock at delivery ${s.stockAtDelivery ?? "?"} = order ${s.orderQty?.toLocaleString() ?? "?"} units`,
      `  Safety buffer = z ${s.zScore?.toFixed(2) ?? "N/A"} × σ ${s.residualStd ?? "?"} × √lead_time`,
      s.orderGuardrail?.active
        ? `  New-product order guardrail: ${s.orderGuardrail.capExceeded ? `ESCALATED — suggest ~${s.orderGuardrail.suggestedCap?.toLocaleString()} not ${s.orderGuardrail.rawOrderQty?.toLocaleString()} (thin history)` : "advisory — thin history, order is provisional"}`
        : "",

      // Current month actuals
      `\n[Current Month]`,
      `  Units sold so far this month: ${s.currentMonth?.unitsSoFar?.toLocaleString() ?? "N/A"}`,
      `  Forecast remaining this month: ${s.currentMonth?.forecastRemaining?.toLocaleString() ?? "N/A"}`,
      `  Last month total: ${s.currentMonth?.lastMonthTotal?.toLocaleString() ?? "N/A"}`,

      // Month-by-month forecast cards (all 3 months)
      `\n[Monthly Forecast (3 months)]`,
      ...(s.monthCards?.map(c => [
        `  ${c.monthLabel}:`,
        `    Projected total: ${c.projectedTotal?.toLocaleString()} units`,
        `    Range: ${c.rangeLow?.toLocaleString()} – ${c.rangeHigh?.toLocaleString()} units (reliability: ${c.reliabilityColor})`,
        c.isCurrent ? `    Actuals so far: ${c.actualsSoFar?.toLocaleString()}, remaining forecast: ${c.forecastRemaining?.toLocaleString()}` : "",
      ].filter(Boolean).join("\n")) ?? ["  No month forecast data available"]),

      // Open PO
      `\n[Open Purchase Order]`,
      po
        ? [
            `  Status: Order in transit — reorder alert suppressed`,
            `  Units ordered: ${po.qty?.toLocaleString()}`,
            `  Order date: ${po.ordered ?? "N/A"}`,
            `  Expected delivery: ${po.delivery ?? "N/A"}`,
            `  Supplier: ${po.supplier || "Not specified"}`,
          ].join("\n")
        : `  Status: No open PO`,

      // Events
      `\n[Logged Events]`,
      s.events?.length > 0
        ? s.events.map(e => `  • ${e.type} on ${e.date}${e.end_date ? ` – ${e.end_date}` : ""}${e.label ? ` (${e.label})` : ""}${e.new_price ? ` → new price $${e.new_price}` : ""}`).join("\n")
        : `  None`,

      // Upcoming promos
      s.upcomingPromos?.length > 0
        ? `\n[Upcoming Promotions in Forecast Window]\n` + s.upcomingPromos.map(p => `  • ${p.label || "Promo"}: ${p.date}${p.end_date ? ` – ${p.end_date}` : ""}`).join("\n")
        : "",

      // Price change warning
      s.priceChangeWarning
        ? `\n[Price Change Warning]\n  ${s.priceChangeMessage}`
        : "",

      // Inventory scorecard (current situation + profitability), if loaded
      (() => {
        const sc = scMap[s.skuId];
        if (!sc) return "";
        const view = sc; // live view (promo-adjusted if on promo)
        const cov  = view.daysOfCover == null ? "365+ (no depletion within a year)" : `${view.daysOfCover} days`;
        // On-hand and full position are different facts. Handing over only the first
        // let the assistant call a product short while a PO was days from landing.
        const inb  = view.unitsOnOrder > 0
          ? ` (on hand; ${view.unitsOnOrder.toLocaleString()} more on order, ${view.daysOfCoverWithInbound == null ? "365+" : view.daysOfCoverWithInbound + " days"} in total)`
          : "";
        const st   = view.sellThrough == null ? "N/A" : `${Math.round(view.sellThrough * 100)}%`;
        const out = [
          `\n[Inventory Scorecard]`,
          `  Status: ${view.status}; days of stock: ${cov}${inb}; sell-through: ${st}`,
          `  Profit grade: ${view.returnTier}${view.marginPct != null ? ` (margin ${view.marginPct}%)` : " (no cost)"}; cash tied up: ${sc.carryingValue != null ? "$" + Math.round(sc.carryingValue).toLocaleString() : "N/A"}`,
          `  Recommended action: ${view.recommendation.action} — ${view.recommendation.reason}`,
        ];
        if (view.flags?.length) out.push(`  Flags: ${view.flags.map(f => f.text).join(" | ")}`);
        if (sc.onPromoToday) {
          out.push(`  NOTE: promotion active (on-sale view). No-promo status: ${sc.baselineView ? `${sc.baselineView.status}, ${sc.baselineView.daysOfCover == null ? "365+" : sc.baselineView.daysOfCover + "d"}` : "n/a"}.`);
        }
        return out.join("\n");
      })(),
    ];

    return lines.filter(l => l !== "").join("\n");
  });

  // ── 3. Supplier section ──
  let supplierSection = "";
  // Real suppliers only — the un-assigned holding ledger isn't one, and describing it
  // to the model as a supplier would invite advice about "managing" it.
  const supList = namedSuppliers(suppliers);
  if (supList.length > 0) {
    const lines = ["\n\n=== SUPPLIER RELIABILITY ==="];
    supList.forEach(sup => {
      const orders    = sup.orders || [];
      const completed = orders.filter(o => o.receivedDate && o.orderedDate);
      const inTransit = orders.filter(o => !o.receivedDate);
      const leadTimes = completed.map(o => Math.round((new Date(o.receivedDate) - new Date(o.orderedDate)) / 86400000)).filter(d => d >= 0);
      const avg  = leadTimes.length ? Math.round(leadTimes.reduce((a, b) => a + b, 0) / leadTimes.length) : null;
      const withExp = completed.filter(o => o.expectedDate);
      let onTimeRate = null, avgVariance = null;
      if (withExp.length > 0) {
        const variances = withExp.map(o => Math.round((new Date(o.receivedDate) - new Date(o.expectedDate)) / 86400000));
        avgVariance = Math.round(variances.reduce((a, b) => a + b, 0) / variances.length * 10) / 10;
        onTimeRate  = Math.round(variances.filter(v => v <= 0).length / variances.length * 100);
      }
      lines.push(`━━ Supplier: ${sup.name} ━━`);
      lines.push(`  Assigned SKUs: ${sup.skuIds?.length ? sup.skuIds.join(", ") : "none"}`);
      lines.push(`  Completed orders: ${completed.length}`);
      lines.push(`  Avg lead time: ${avg !== null ? avg + "d" : "N/A"}`);
      lines.push(`  On-time rate: ${onTimeRate !== null ? onTimeRate + "%" : "N/A"}`);
      lines.push(`  Avg delivery variance: ${avgVariance !== null ? (avgVariance > 0 ? "+" + avgVariance + "d (late)" : avgVariance + "d (early/on time)") : "N/A"}`);
      if (inTransit.length > 0) {
        lines.push(`  In-transit orders (${inTransit.length}):`);
        inTransit.forEach(o => {
          lines.push(`    - ${o.qty || "?"} units ordered ${o.orderedDate}${o.expectedDate ? ", expected " + o.expectedDate : ""}${o.skuId ? " (SKU: " + o.skuId + ")" : ""}`);
        });
      }
      if (completed.length > 0) {
        lines.push(`  Recent completed orders:`);
        completed.slice(-3).forEach(o => {
          const lt = Math.round((new Date(o.receivedDate) - new Date(o.orderedDate)) / 86400000);
          const variance = o.expectedDate ? Math.round((new Date(o.receivedDate) - new Date(o.expectedDate)) / 86400000) : null;
          lines.push(`    - ${o.qty || "?"} units: ordered ${o.orderedDate}, received ${o.receivedDate} (${lt}d lead time${variance !== null ? ", " + (variance > 0 ? "+" + variance + "d vs expected" : variance === 0 ? "on time" : Math.abs(variance) + "d early") : ""})`);
        });
      }
    });
    lines.push("=== END SUPPLIER DATA ===");
    supplierSection = lines.join("\n");
  }

  // ── 4. Fleet inventory summary ──
  let scorecardSection = "";
  if (scorecard?.distribution) {
    const d = scorecard.distribution;
    const attn = (scorecard.needsAttention || []).slice(0, 5)
      .map(r => `${r.skuName} (${r.status})`).join(", ");
    scorecardSection = [
      "=== INVENTORY STATUS SUMMARY ===",
      `Status distribution: Stockout risk ${d["Stockout risk"] || 0}, Dead stock ${d["Dead stock"] || 0}, Overstocked ${d["Overstocked"] || 0}, Reorder due ${d["Reorder due"] || 0}, Healthy ${d["Healthy"] || 0}`,
      attn ? `Most urgent (worst first): ${attn}` : "",
      "Status = each item's current inventory situation. Profit grade (A/B/C/F) = margin per sale after cost + fees (A 40%+, B 20%+, C slim under 20%, F a loss at 0% or negative). A slow seller with an A/B grade is profitable — recommend right-sizing, not liquidation. An F item loses money on every sale — flag it for a price increase, cost renegotiation, or discontinuation. 'Reorder due' is a routine action, not a health problem.",
      "=== END INVENTORY STATUS SUMMARY ===\n",
    ].filter(Boolean).join("\n");
  }

  // ── 5. Product classification / relatedness summary ──
  let classificationSection = "";
  if (groups?.skus?.length) {
    const byGroup = {};
    groups.skus.forEach(s => { const g = s.group || "(ungrouped / orphan)"; (byGroup[g] ||= []).push(s.skuName); });
    const lines = ["\n\n=== PRODUCT CLASSIFICATION & RELATEDNESS ==="];
    lines.push(`Grouping signal in use: ${groups.groupColumns?.length ? groups.groupColumns.join(", ") : "none yet — no category/attribute data has been provided for these products"}`);
    Object.entries(byGroup).forEach(([g, names]) => lines.push(`  Group "${g}": ${names.join(", ")}`));
    lines.push("Products in the same group can share a pooled (global) forecast — useful for new or sparse items that borrow from established siblings. Ungrouped/orphan products forecast from their own history only (or fall back if they have too little).");
    lines.push("=== END PRODUCT CLASSIFICATION ===");
    classificationSection = lines.join("\n");
  }

  // ── 6. Historical sales data (complete monthly + recent daily per SKU) ──
  let historySection = "";
  if (history?.skus?.length) {
    const lines = [
      "\n\n=== HISTORICAL SALES DATA ===",
      "Complete MONTHLY totals over each SKU's full history, plus the most recent " +
        `${history.recentDays ?? 90} days of DAILY rows. Use this for any question about prior/historical sales, ` +
        "trends, seasonality, best/worst periods, or price/promo history. Dates reflect the loaded data " +
        "(re-anchored so the latest row is treated as 'yesterday'). For a specific single day older than the " +
        "daily window, reason from that month's total rather than inventing a daily figure.",
    ];
    history.skus.forEach(h => {
      lines.push(`\n━━ ${h.skuName} (${h.skuId}) ━━ | ${h.dateRange?.start} → ${h.dateRange?.end} | ${h.totalUnits?.toLocaleString()} units total`);
      if (h.monthly?.length) {
        lines.push("  Monthly (units" + (h.monthly[0].avgPrice != null ? ", avg price" : "") + (h.monthly[0].promoDays != null ? ", promo days" : "") + "):");
        lines.push("    " + h.monthly.map(m =>
          `${m.month}: ${m.units.toLocaleString()}` +
          (m.avgPrice != null ? ` @ $${m.avgPrice}` : "") +
          (m.promoDays ? ` (${m.promoDays}p)` : "")
        ).join(" | "));
      }
      if (h.recentDaily?.length) {
        lines.push(`  Recent daily (last ${h.recentDaily.length}):`);
        lines.push("    " + h.recentDaily.map(r =>
          `${r.date}: ${r.units}` +
          (r.price != null ? ` $${r.price}` : "") +
          (r.promo ? " promo" : "") +
          (r.stock != null ? ` stk${r.stock}` : "")
        ).join(" | "));
      }
    });
    lines.push("=== END HISTORICAL SALES DATA ===");
    historySection = lines.join("\n");
  }

  const detail = skus.filter(Boolean);
  const detailHeader = detail.length
    ? `\n=== FULL DETAIL for ${detail.length} of ${skuForecasts.length} products ===\n`
      + "Every other product appears in the roster above with its headline numbers only. If a\n"
      + "question needs engine internals, protection reasoning, monthly projections or event\n"
      + "history for a product NOT detailed here, say which product you need and stop — do not\n"
      + "infer those from the roster line.\n"
    : "";

  return folderSection + roster + scorecardSection + classificationSection
       + detailHeader + detail.join("\n\n") + supplierSection + historySection;
}

// Was a third private copy of this, and the most wrong: it sent the raw
// params.leadTime AND dropped `fees`, so the model was briefed with different
// statuses and different margins than either tab showed. Uses the shared builder
// in helpers now.

// ─────────────────────────────────────────────
// REORDER BRIEF
// ─────────────────────────────────────────────
function ReorderBrief({ skuForecasts, openPOs, skuParams, skuList, apiKey }) {
  const [brief,   setBrief]   = useState(null);
  const [loading, setLoading] = useState(false);
  const [copied,  setCopied]  = useState(false);

  const generate = async () => {
    setLoading(true);
    setBrief(null);

    const DEFAULT_PARAMS = { stock: 500, leadTime: 14, coverage: 30, strategy: "balanced" };
    const today = new Date();
    const fmt = d => d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });

    const skuLines = (skuForecasts || []).map(s => {
      const po     = openPOs?.[s.skuId];
      const params = skuParams?.[s.skuId] ?? DEFAULT_PARAMS;
      return [
        `SKU: ${s.skuName} (${s.skuId})`,
        `  Reorder status: ${po ? "ON ORDER — alert suppressed" : s.daysUntilReorder === -1 ? "No reorder needed" : s.daysUntilReorder <= 0 ? "OVERDUE" : `${s.daysUntilReorder} days until deadline`}`,
        `  Units to order: ${s.orderQty?.toLocaleString()}`,
        `  Stockout in: ${s.daysUntilStockout === -1 ? "N/A" : `${s.daysUntilStockout} days`}`,
        `  Lead time: ${params.leadTime} days`,
        po ? `  Open PO: ${po.qty} units from ${po.supplier || "supplier"}, arriving ${po.delivery}` : "",
      ].filter(Boolean).join("\n");
    }).join("\n\n");

    const prompt = `You are a purchasing manager writing a weekly procurement brief.
Today is ${fmt(today)}.

Write a concise, professional reorder brief that:
1. Opens with one sentence summarizing the overall fleet status
2. Lists each SKU that needs action, in order of urgency (overdue first, then by days remaining)
3. For each actionable SKU: state what to order, how many units, and by when — in one clear sentence
4. Lists SKUs with open POs as "covered" in a separate short section
5. Lists SKUs needing no action as "healthy" in one line
6. Closes with one sentence on the biggest risk if any deadline is missed

Use plain text. No markdown, no asterisks. Use • for bullet points. Never use em dashes or en dashes.
Write it so it could be forwarded directly to a procurement team.

Fleet data:
${skuLines}`;

    try {
      const res = await fetch(GROQ_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${apiKey}` },
        body: JSON.stringify({
          model: GROQ_MODEL_CHAT,
          ...GROQ_NO_TOOLS,
          messages: [{ role: "user", content: prompt }],
          max_tokens: 600,
          temperature: 0.3,
        }),
      });
      const data = await res.json();
      // The brief renders in its own component, so there is no setUsage in scope here.
      // recordGroqUsage stores at module level regardless, which is where the reading
      // lives; the drawer's header picks it up on its next render.
      recordGroqUsage(data, 600);
      const ran = groqToolsRan(data);
      if (ran) console.warn(`[ai] built-in tools ran despite being disabled: ${ran}`);
      setBrief((ran ? `[warning: the model ran ${ran}, so outside content may have influenced this brief]\n\n` : "")
        + (data?.choices?.[0]?.message?.content || "Could not generate brief."));
    } catch {
      setBrief("Failed to generate brief. Check your API key.");
    } finally {
      setLoading(false);
    }
  };

  const copy = () => {
    navigator.clipboard.writeText(brief);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="mx-4 mb-3 border border-[var(--t-line)] rounded-2xl overflow-hidden">
      <button onClick={generate} disabled={loading}
        className="w-full flex items-center gap-2.5 px-4 py-3 bg-[var(--t-bg)] hover:bg-[var(--t-line)] disabled:opacity-60 transition-colors text-left">
        <div className="h-6 w-6 rounded-lg bg-[var(--t-info-soft)] border border-[var(--t-info-line)] flex items-center justify-center shrink-0">
          {loading
            ? <div className="h-2.5 w-2.5 border-2 border-[var(--t-info-line)] border-t-transparent rounded-full animate-spin" />
            : <svg className="h-3 w-3 text-[var(--t-info)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
          }
        </div>
        <div>
          <div className="text-[14px] font-semibold text-[var(--t-ink)]">
            {loading ? "Generating brief…" : "Generate Reorder Brief"}
          </div>
          <div className="text-[13px] text-[var(--t-dim)]">AI-written purchasing summary, ready to forward</div>
        </div>
      </button>
      {brief && (
        <div className="border-t border-[var(--t-line)] bg-[#070b10]">
          <div className="px-4 py-3 max-h-64 overflow-y-auto">
            <p className="text-[14px] text-[var(--t-soft)] leading-relaxed whitespace-pre-wrap">{brief}</p>
          </div>
          <div className="px-4 py-2 border-t border-[var(--t-line)] flex justify-between items-center">
            <button onClick={() => setBrief(null)} className="text-[13px] text-[var(--t-soft)] hover:text-[var(--t-dim)] transition-colors">dismiss</button>
            <button onClick={copy}
              className="flex items-center gap-1.5 text-[13px] font-semibold text-[var(--t-info)] hover:text-[var(--t-info)] border border-[var(--t-info-line)] hover:border-[var(--t-info-line)] bg-[var(--t-info-soft)] rounded-lg px-2.5 py-1 transition-all">
              {copied
                ? <><svg className="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>Copied!</>
                : <><svg className="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>Copy to clipboard</>
              }
            </button>
          </div>
        </div>
      )}
    </div>
  );
}


const SUGGESTED_PROMPTS = [
  "Which SKUs are unhealthiest right now and why?",
  "Any slow movers that are still profitable I shouldn't liquidate?",
  "Which SKUs are overstocked or look like dead stock?",
  "Where is my inventory cash tied up with the lowest return?",
];

// ─────────────────────────────────────────────
// MESSAGE BUBBLE
// ─────────────────────────────────────────────
function MessageBubble({ msg }) {
  const isUser = msg.role === "user";
  return (
    <div className={`flex gap-2.5 ${isUser ? "flex-row-reverse" : "flex-row"}`}>
      {!isUser && (
        <div className="h-6 w-6 shrink-0 rounded-lg bg-[var(--t-accent-soft)] border border-[var(--t-accent-line)] flex items-center justify-center mt-0.5">
          <svg className="h-3 w-3 text-[var(--t-accent)]" viewBox="0 0 24 24" fill="none" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
          </svg>
        </div>
      )}
      <div className={`max-w-[85%] rounded-2xl px-3.5 py-2.5 text-[14px] leading-relaxed whitespace-pre-wrap ${
        isUser
          ? "bg-[var(--t-info-soft)] border border-[var(--t-info-line)] text-[var(--t-info)] rounded-tr-sm"
          : "bg-[var(--t-line)] border border-[var(--t-line2)] text-[var(--t-ink)] rounded-tl-sm"
      }`}>
        {msg.content}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────
// TYPING INDICATOR
// ─────────────────────────────────────────────
function TypingIndicator() {
  return (
    <div className="flex gap-2.5 flex-row">
      <div className="h-6 w-6 shrink-0 rounded-lg bg-[var(--t-accent-soft)] border border-[var(--t-accent-line)] flex items-center justify-center">
        <svg className="h-3 w-3 text-[var(--t-accent)]" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
        </svg>
      </div>
      <div className="bg-[var(--t-line)] border border-[var(--t-line2)] rounded-2xl rounded-tl-sm px-4 py-3 flex items-center gap-1">
        {[0, 1, 2].map(i => (
          <div key={i} className="h-1.5 w-1.5 rounded-full bg-[var(--t-sunken)] animate-bounce"
               style={{ animationDelay: `${i * 150}ms`, animationDuration: "0.8s" }} />
        ))}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────
// MAIN DRAWER
// ─────────────────────────────────────────────
export default function AiDrawer({ skuForecasts, openPOs, skuParams, skuList, folders, apiKey, suppliers, api = "http://localhost:8000" }) {
  const [open,     setOpen]     = useState(false);
  const [messages, setMessages] = useState([]);
  const [input,    setInput]    = useState("");
  const [loading,  setLoading]  = useState(false);
  const [error,    setError]    = useState(null);
  // Off by default: including full sales history makes the request too large for
  // Groq's free tier (12k tokens/min). Turn on once you're on a higher tier.
  const [includeHistory, setIncludeHistory] = useState(false);
  // Deprecation banner and a live token readout. Both exist because this project's
  // failures have all been silent: a retired model surfaced as "couldn't parse that",
  // and an oversized request surfaced as nothing at all.
  const [health, setHealth] = useState(null);
  const [usage,  setUsage]  = useState(null);

  useEffect(() => {
    let alive = true;
    checkGroqModels().then(h => { if (alive) setHealth(h); });
    return () => { alive = false; };
  }, []);
  const bottomRef = useRef(null);
  const inputRef  = useRef(null);

  // ── Draggable button state ──
  //
  // null means "no saved position", which renders from the CSS bottom instead. Anything
  // that is not a finite number MUST collapse back to null, because `top: NaNpx` is an
  // invalid declaration: the browser drops it, `bottom` is already "auto", and the
  // button lands at viewport y=0 with no way to move it. `NaN ?? fallback` returns NaN
  // rather than the fallback, so every later drag computed NaN too and the button was
  // stuck at the top of the screen permanently.
  //
  // It got there from a plain CLICK. mouseup fired without any movement and saved
  // String(btnYRef.current) while btnY was still null, writing the literal "null", which
  // parseInt turned into NaN on the next load. So opening the drawer once and reloading
  // was enough to break it.
  const readBtnY = () => {
    const n = parseInt(loadStorage("logitrack_ai_btn_y", null), 10);
    return Number.isFinite(n) ? n : null;
  };
  const clampBtnY = (n) =>
    (Number.isFinite(n) ? Math.max(8, Math.min(window.innerHeight - 60, n)) : null);
  const [btnY, setBtnY] = useState(() => clampBtnY(readBtnY()));
  const btnYRef      = useRef(btnY);
  const hasMoved     = useRef(false);
  const dragStartY   = useRef(0);
  const dragStartTop = useRef(0);

  useEffect(() => { btnYRef.current = btnY; }, [btnY]);

  // A position saved on a tall window is off-screen on a short one. The clamp only ever
  // ran mid-drag, so a smaller window left the button below the fold with no way to
  // reach it — the same class of bug as the NaN, just at the other end.
  useEffect(() => {
    const onResize = () => setBtnY(prev => (prev === null ? null : clampBtnY(prev)));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const handleMouseDown = (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    hasMoved.current     = false;
    dragStartY.current   = e.clientY;
    // Number.isFinite, not ??, so a bad stored value can't poison the drag.
    dragStartTop.current = Number.isFinite(btnYRef.current)
      ? btnYRef.current
      : (window.innerHeight - 24 - 44);

    const onMove = (ev) => {
      const delta = ev.clientY - dragStartY.current;
      if (Math.abs(delta) > 5) hasMoved.current = true;
      const newTop = Math.max(8, Math.min(window.innerHeight - 60, dragStartTop.current + delta));
      setBtnY(newTop);
    };

    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      // Only persist an actual drag, and only a real number. Saving on every click is
      // what wrote "null" into storage in the first place.
      if (hasMoved.current && Number.isFinite(btnYRef.current)) {
        saveStorage("logitrack_ai_btn_y", String(btnYRef.current));
      }
    };

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const handleBtnClick = () => {
    if (hasMoved.current) return; // was a drag, not a click
    setOpen(true);
  };

  useEffect(() => {
    if (bottomRef.current) bottomRef.current.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus();
  }, [open]);

  const sendMessage = async (text) => {
    const userText = (text ?? input).trim();
    if (!userText || loading) return;
    setInput("");
    setError(null);

    // Echo the user's message and show the thinking indicator IMMEDIATELY, before
    // any network calls. Previously setMessages ran only AFTER the scorecard and
    // groups fetches resolved, so the user's own message didn't appear for 1-2s
    // after hitting enter — it looked like nothing was happening.
    const newMessages = [...messages, { role: "user", content: userText }];
    setMessages(newMessages);
    setLoading(true);

    // Pull the AI's supporting context. These endpoints are independent, so we
    // fetch them in PARALLEL rather than one after another — roughly halving the
    // wait before the request reaches Groq. All three are best-effort: a failure
    // (or an older Prophet-only backend without an endpoint) just yields null.
    //   • scorecard — inventory-health status, coverage, return tiers, actions
    //   • groups    — product classification / relatedness / similar products
    //   • history   — full sales history; opt-in (large, needs a higher Groq tier)
    const fetchJsonSafe = async (url, opts) => {
      try { const r = await fetch(url, opts); return r.ok ? await r.json() : null; }
      catch { return null; }
    };
    const [scorecard, groups, history] = await Promise.all([
      fetchJsonSafe(`${api}/api/scorecard`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        /* `suppliers` is not optional. buildScorecardBody feeds it to planningLeadTime,
           which without it can't resolve a measured or supplier-inherited lead time and
           falls back to the raw params value. Dropping it here meant the assistant was
           briefed with different statuses than the Fleet and Scorecard tabs display. */
        body: JSON.stringify(buildScorecardBody(skuList, skuParams, openPOs, suppliers)),
      }),
      fetchJsonSafe(`${api}/api/groups`),
      includeHistory ? fetchJsonSafe(`${api}/api/history?recent_days=90`) : Promise.resolve(null),
    ]);

    // Which products get the full block. Everything else still appears in the roster.
    const detailFor = pickDetailSkus(text, skuForecasts, scorecard);
    let fleetCtx = buildFleetContext(skuForecasts, openPOs, skuParams, skuList, folders,
                                     suppliers, scorecard, groups, history, detailFor);

    // Last-resort cap. This used to be fleetCtx.slice(0, 28000), a blind cut through
    // whatever text happened to be at character 28,000: it sliced mid-product and
    // silently dropped every product after the cut, so on a 32-product catalogue the
    // assistant was briefed on roughly the first two thirds and had no idea the rest
    // existed. Which products vanished depended only on sort order. Now the roster is
    // built first and always survives, and if the total is still too long the detail
    // blocks are dropped from the end — losing depth on some products, never their
    // existence. (The old comment also cited a 12k/min budget, which was the retired
    // model's; see GROQ_MODEL_CHAT in lib/ai.js for the current one.)
    // Both figures are now set by the ~8,000-token per-request ceiling described above,
    // not by the per-minute allowance. 20,000 characters is ~5,000 tokens, which leaves
    // room for the preamble, the turns and the reply.
    //
    // "Full sales history" cannot fit inside that ceiling on the free tier at all: 90
    // days of daily rows across a real catalogue runs to tens of thousands of tokens on
    // its own. Rather than let the toggle build a request that is certain to be refused,
    // it now gets the same budget and is trimmed to fit — the model sees a slice of the
    // history and is told so, instead of the request failing outright.
    const FLEET_CHAR_BUDGET = 20000;
    if (fleetCtx.length > FLEET_CHAR_BUDGET) {
      const cut = fleetCtx.lastIndexOf("\n\n━━━ SKU:", FLEET_CHAR_BUDGET);
      fleetCtx = (cut > 0 ? fleetCtx.slice(0, cut) : fleetCtx.slice(0, FLEET_CHAR_BUDGET)) +
        "\n\n[Detail for the remaining products was dropped to fit the token limit. They are all\n" +
        "still listed in the fleet roster above. Ask about one by name for its full detail.]";
    }

    // Build folder summary for system prompt preamble
    const folderSummary = (folders && Object.keys(folders).length > 0)
      ? `\nThe manager has organised SKUs into the following folders: ${
          Object.entries(folders).map(([, f]) => {
            const names = f.skuIds.map(id => skuList?.find(s => s.id === id)?.name || id);
            return `"${f.name}" (${names.join(", ") || "empty"})`;
          }).join("; ")
        }. When answering questions about a folder by name, use this mapping. Folder names are the manager's own business groupings — treat them as meaningful context (e.g. a folder named "Seasonal" implies the SKUs inside are seasonal products).`
      : "";

    const systemPrompt = `You are an inventory intelligence assistant embedded in LogiTrack, a demand forecasting dashboard.
You have complete, real-time data for every SKU including 3-month forecasts, model quality indicators, user parameters, open purchase orders, logged events, and pricing info. ${includeHistory
  ? "You also have HISTORICAL SALES DATA for every SKU — complete month-by-month totals over its full history plus the most recent ~90 days of daily rows (with price/promo/stock) — found in the \"HISTORICAL SALES DATA\" section; use it to answer any question about past sales, trends, seasonality, or best/worst periods."
  : "You do NOT currently have full historical sales data loaded — the manager has 'Full sales history' turned off (it's large and needs a higher Groq tier). You still have recent-month actuals and the 3-month forecast per SKU. If asked about detailed past sales beyond those, say the full history isn't loaded right now and can be enabled with the 'Full sales history' toggle."} You also have full supplier reliability data including each supplier's order history, average lead time, on-time rate, delivery variance, and any orders currently in transit.
You also have full visibility into how the manager has organised SKUs into folders.${folderSummary}
You also know HOW EACH SKU IS FORECAST: this app routes every product to the best method — Prophet (established, regular demand), a pooled GLOBAL model (new or sparse products that borrow seasonal shape and volume from related/similar products), Croston/TSB (intermittent demand), or a last-resort moving-average placeholder (too little data and no relatives). Each SKU lists its chosen engine, why, and — under "Product Classification & Similar Products" — its extracted attributes (category, brand, size, etc.), its group, and which products it is similar to. Do NOT assume Prophet for every SKU; use the named engine. When asked about categorization, similar products, or why a product is or isn't pooling, use the classification data provided.
You also have each SKU's INVENTORY SCORECARD: a named STATUS describing its current situation (Healthy, Reorder due, Overstocked, Stockout risk, or Dead stock), the signal behind it (sales volatility), days of stock remaining (on hand, plus the position once any inbound PO lands), sell-through, a profit grade A/B/C/F based on margin after cost and fees (A 40%+, B 20%+, C slim under 20%, F a loss at 0% or negative), cash tied up, flags, and a recommended action. Status and profitability are SEPARATE axes. Reorder urgency is kept out of "health": a reorder coming due is a routine action ("Reorder due"), NOT a problem — only a missed window or an unrecoverable position is "Stockout risk." A slow-moving SKU with an A/B profit grade is profitable and should be right-sized, NOT liquidated — only low-selling, overstocked, low-margin (C) items are dead-stock/markdown candidates. An F-grade item loses money on every sale regardless of how it sells — flag it for a price increase, cost renegotiation, or discontinuation. A NEW product (flagged "health read is provisional", under ~90 days of history) is NEVER dead stock and should not be marked down or discontinued even when overstocked — it hasn't had time to establish; recommend holding and pausing/trimming reorders until it builds history. When a promotion is active, the status shown is the on-sale view; a normal (no-promo) status is also provided for post-promotion planning.

KEY MECHANICS you must reason with correctly:
• PROTECTION LEVEL (safety buffer service level): after a backtest runs, mature costed SKUs use their per-SKU cheapest backtest tier when the live lead time and coverage match the backtest settings. Otherwise, when a SKU has enough regular history and a real unit cost, the recommendation comes from its expected cost curve: lost profit from stockouts versus yearly holding cost for the safety buffer. Remaining SKUs fall back to the margin heuristic (Light 90% / Standard 95% / High 98% / Very high 99% / Maximum 99.5%), and if no unit cost is entered they default to Standard (95%). The manager can override it. Demand volatility (CV/σ) sizes the buffer; it does NOT directly choose the service level.
• LAUNCH RAMP: only the pooled GLOBAL model applies a damped upward growth trend for still-ramping new products; it fades over time and retires once the SKU has enough history to move to Prophet.
• ORDER GUARDRAIL: for thin-history SKUs the recommended order is flagged (and an escalated version suggests a conservative ~6-week first-order cap) because early forecasts are speculative.
• ORDER TIMING: stockout date, order-by, coverage window, and order quantity all assume the order is placed TODAY (delivery = today + lead time). When OVERDUE, delivery can land after the stockout, creating an unavoidable out-of-stock gap. Stockout demand is treated as LOST sales (not backordered): the order covers demand from delivery forward and does not pad to recover sales missed during the gap. The demand forecast stays true customer demand and is NOT reduced for stockouts.
• Uploaded data is re-anchored so the latest row is treated as "yesterday," and stock levels are auto-seeded from the most recent Units_In_Stock; the forecast begins from today.
Be concise, specific, and actionable. Use plain language — no markdown headers, no asterisks.
Use bullet lines with the • character when listing multiple items. Keep answers focused and scannable.
Never use em dashes (—) or en dashes (–); use commas, colons, periods or parentheses instead.
When asked about a folder, aggregate data across all SKUs in that folder and answer at the folder level.
Today's date is ${new Date().toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" })}.

=== FULL FLEET DATA ===
${fleetCtx}
=== END FLEET DATA ===`;

    // Only send the last few turns to the model (the big fleet context already
    // lives in the system prompt) so the request doesn't grow unbounded.
    // (newMessages / loading were already set at the top so the echo is instant.)
    const chatMessages = [
      { role: "system", content: systemPrompt },
      ...newMessages.slice(-6).map(m => ({
        role: m.role === "assistant" ? "assistant" : "user",
        content: m.content,
      })),
    ];

    try {
      const res = await fetch(GROQ_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${apiKey}`,
        },
        body: JSON.stringify({
          model: GROQ_MODEL_CHAT,
          ...GROQ_NO_TOOLS,
          messages: chatMessages,
          max_tokens: 700,
          temperature: 0.3,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const msg = err?.error?.message || `API error ${res.status}`;
        // Two different failures used to share one message, and it named the wrong fix
        // for one of them. Running out of the MINUTE'S allowance (429) means the request
        // was fine and you need to wait; a request too big for the model means it will
        // never succeed no matter how long you wait. Telling someone to "ask about fewer
        // SKUs" when they simply need to pause sixty seconds sends them rewriting a
        // question that was never the problem.
        const retry = Number(res.headers.get("retry-after")) || null;
        if (res.status === 429 || /rate limit|tokens per minute|TPM|requests per/i.test(msg)) {
          throw new Error(
            `Used up this minute's allowance on ${GROQ_MODEL_CHAT}. `
            + (retry ? `Try again in about ${Math.ceil(retry)}s. ` : "Try again in a minute. ")
            + (includeHistory
                ? "Turning off \"Full sales history\" below makes each question far cheaper."
                : "Each question sends your fleet summary, so a few in quick succession add up.")
          );
        }
        if (/too large|context length|reduce your message|maximum context/i.test(msg)) {
          throw new Error(
            "That request is too big for one call"
            + (includeHistory ? ", almost certainly the full sales history. Turn off \"Full sales history\" below." : ". Ask about fewer products at once.")
          );
        }
        throw new Error(msg);
      }

      const data = await res.json();
      recordGroqUsage(data, 700); setUsage(usageSummary());
      // Tools are meant to be off. If one ran anyway, external content reached the same
      // context as the catalogue, and the reader has to know that before trusting it.
      const ran = groqToolsRan(data);
      if (ran) console.warn(`[ai] built-in tools ran despite being disabled: ${ran}`);
      const reply = (ran ? `[warning: the model ran ${ran}, so this answer may include content from outside your data]\n\n` : "")
        + (data?.choices?.[0]?.message?.content || "No response received.");
      setMessages(prev => [...prev, { role: "assistant", content: reply }]);
    } catch (err) {
      setError(err.message);
      setMessages(prev => [...prev, { role: "assistant", content: "Sorry, I hit an error. Check your API key or network." }]);
    } finally {
      setLoading(false);
    }
  };

  const handleKey = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  };

  const urgentCount = skuForecasts?.filter(s =>
    !openPOs?.[s.skuId] && s.daysUntilReorder !== -1 && s.daysUntilReorder <= 7
  ).length ?? 0;

  // Build a concise folder count label for the header
  const folderCount = folders ? Object.keys(folders).length : 0;

  return (
    <>
      {/* ── TRIGGER BUTTON (Y-axis draggable) ── */}
      <button
        onMouseDown={handleMouseDown}
        onClick={handleBtnClick}
        style={btnY !== null
          ? { position: "fixed", top: `${btnY}px`, right: "24px", bottom: "auto", zIndex: 40 }
          : { position: "fixed", bottom: "24px",  right: "24px",            zIndex: 40 }
        }
        className="flex items-center gap-2 px-4 py-2.5 rounded-2xl bg-[var(--t-accent-soft)] hover:bg-[var(--t-accent-soft)] border border-[var(--t-accent-line)] shadow-lg shadow-[var(--t-accent-line)] transition-colors select-none cursor-grab active:cursor-grabbing"
      >
        {/* Grip indicator */}
        <svg className="h-3 w-3 text-[var(--t-accent)] shrink-0" viewBox="0 0 10 16" fill="currentColor">
          <circle cx="3" cy="3"  r="1.2"/><circle cx="7" cy="3"  r="1.2"/>
          <circle cx="3" cy="8"  r="1.2"/><circle cx="7" cy="8"  r="1.2"/>
          <circle cx="3" cy="13" r="1.2"/><circle cx="7" cy="13" r="1.2"/>
        </svg>
        <svg className="h-4 w-4 text-[var(--t-ink)]" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
        </svg>
        <span className="text-[15px] font-semibold text-[var(--t-ink)]">Ask AI</span>
        {urgentCount > 0 && (
          <span className="h-4 w-4 rounded-full bg-[var(--t-bad-soft)] text-[var(--t-ink)] text-[13px] font-bold flex items-center justify-center">
            {urgentCount}
          </span>
        )}
      </button>

      {/* ── BACKDROP ── */}
      {open && (
        <div className="fixed inset-0 z-40 bg-black/30 backdrop-blur-[2px]" onClick={() => setOpen(false)} />
      )}

      {/* ── DRAWER ── */}
      <div className={`fixed top-0 right-0 h-full z-50 w-[420px] flex flex-col bg-[#0a0e14] border-l border-[var(--t-line)] shadow-2xl shadow-black/60 transition-transform duration-300 ease-out ${open ? "translate-x-0" : "translate-x-full"}`}>

        {/* Header */}
        <div className="shrink-0 px-5 py-4 border-b border-[var(--t-line)] flex items-center gap-3">
          <div className="h-7 w-7 rounded-xl bg-[var(--t-accent-soft)] border border-[var(--t-accent-line)] flex items-center justify-center">
            <svg className="h-3.5 w-3.5 text-[var(--t-accent)]" viewBox="0 0 24 24" fill="none" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
            </svg>
          </div>
          <div className="flex-1">
            <div className="text-[15px] font-bold text-[var(--t-ink)]">Fleet Intelligence</div>
            <div className="text-[13px] text-[var(--t-dim)] font-mono">
              {skuForecasts?.length ?? 0} SKU{skuForecasts?.length !== 1 ? "s" : ""}
              {folderCount > 0 ? ` · ${folderCount} folder${folderCount !== 1 ? "s" : ""}` : ""}
              {/* Derived from GROQ_MODEL_CHAT rather than typed, so the label can't outlive
                  the model it names the next time Groq retires one. */}
              {` · full context · ${GROQ_MODEL_CHAT.split("/").pop()}`}
              {/* The measured size of the last request. Every limit hit in this project
                  was diagnosed by estimating tokens from character counts, and the
                  estimates were wrong twice; this is the number Groq actually billed. */}
              {usage && (
                <span className={usage.tight ? "text-[var(--t-warn)]" : "text-[var(--t-soft)]"}>
                  {` · ${usage.text}`}{usage.tight ? " (tight)" : ""}
                </span>
              )}
            </div>
          </div>
          <button onClick={() => setOpen(false)}
            className="h-7 w-7 rounded-lg bg-[var(--t-line)] hover:bg-[var(--t-line)] flex items-center justify-center transition-colors">
            <svg className="h-3.5 w-3.5 text-[var(--t-dim)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Reorder Brief */}
        <ReorderBrief
          skuForecasts={skuForecasts}
          openPOs={openPOs}
          skuParams={skuParams}
          skuList={skuList}
          apiKey={apiKey}
        />

        {/* Retired-model banner. Sits above everything because when it fires, nothing
            else in this drawer will work, and the alternative is six features failing
            separately with errors that each describe a symptom rather than the cause. */}
        {groqHealthMessage(health) && (
          <div className="shrink-0 mx-4 mt-3 rounded-lg border border-[var(--t-warn-line)] bg-[var(--t-warn-soft)] px-3 py-2.5">
            <div className="flex items-start gap-2">
              <svg className="h-3.5 w-3.5 text-[var(--t-warn)] mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>
              </svg>
              <div>
                <div className="text-[14px] font-semibold text-[var(--t-warn)]">Model no longer available</div>
                <div className="text-[14px] text-[var(--t-warn)] mt-0.5 leading-snug">{groqHealthMessage(health)}</div>
              </div>
            </div>
          </div>
        )}

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
          {messages.length === 0 && (
            <div className="space-y-4 pt-2">
              <div className="text-center space-y-1.5">
                <div className="text-[14px] text-[var(--t-dim)] font-medium">Ask anything about your inventory</div>
                <div className="text-[14px] text-[var(--t-soft)]">
                  {folderCount > 0
                    ? `Knows your folders, forecasts, reorders and events`
                    : "Knows your 3-month forecasts, settings, events and POs"}
                </div>
              </div>
              <div className="grid grid-cols-1 gap-2">
                {/* Dynamic suggested prompts — show folder-aware ones if folders exist */}
                {[
                  ...(folderCount > 0 && folders
                    ? Object.values(folders).slice(0, 1).map(f =>
                        `Which SKUs in "${f.name}" need the most attention?`
                      )
                    : []),
                  ...SUGGESTED_PROMPTS,
                ].slice(0, 4).map((p, i) => (
                  <button key={i} onClick={() => sendMessage(p)}
                    className="text-left px-3 py-2.5 rounded-xl bg-[var(--t-line)] border border-[var(--t-line2)] hover:border-[var(--t-accent-line)] hover:bg-[var(--t-accent-soft)] transition-all group">
                    <span className="text-[14px] text-[var(--t-dim)] group-hover:text-[var(--t-accent)] transition-colors leading-snug">{p}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((msg, i) => <MessageBubble key={i} msg={msg} />)}
          {loading && <TypingIndicator />}
          <div ref={bottomRef} />
        </div>

        {/* Clear */}
        {messages.length > 0 && (
          <div className="px-4 pb-1 flex justify-end">
            <button onClick={() => { setMessages([]); setError(null); }}
              className="text-[13px] text-[var(--t-soft)] hover:text-[var(--t-dim)] transition-colors">
              Clear conversation
            </button>
          </div>
        )}

        {/* Input */}
        <div className="shrink-0 px-4 pb-5 pt-2">
          {error && (
            <div className="mb-2 text-[13px] text-[var(--t-bad)] bg-[var(--t-bad-soft)] border border-[var(--t-bad-line)] rounded-lg px-3 py-2">{error}</div>
          )}
          <div className="flex items-center justify-between mb-2">
            <button onClick={() => setIncludeHistory(v => !v)} role="switch" aria-checked={includeHistory}
              title="Gives the assistant each SKU's full monthly and recent daily sales so it can answer questions about past sales. It's large, so it needs a Groq tier above the free 12,000 tokens/minute."
              className="flex items-center gap-2 text-[13px] text-[var(--t-dim)] hover:text-[var(--t-soft)] transition-colors select-none">
              <span className={`relative h-4 w-7 rounded-full transition-colors shrink-0 ${includeHistory ? "bg-[var(--t-accent-soft)]" : "bg-[var(--t-line)]"}`}>
                <span className={`absolute top-0.5 h-3 w-3 rounded-full bg-[var(--t-panel)] transition-all ${includeHistory ? "left-[14px]" : "left-0.5"}`} />
              </span>
              <span>Full sales history: <span className={includeHistory ? "text-[var(--t-accent)] font-semibold" : "font-semibold"}>{includeHistory ? "on" : "off"}</span></span>
            </button>
            {includeHistory && <span className="text-[12.5px] text-[var(--t-warn)]">needs higher Groq tier</span>}
          </div>
          <div className="flex gap-2 items-end">
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKey}
              rows={1}
              placeholder="Ask about folders, reorders, forecasts, risks…"
              className="flex-1 bg-[var(--t-bg)] border border-[var(--t-line2)] focus:border-[var(--t-accent-line)] rounded-xl px-3 py-2.5 text-[14px] text-[var(--t-ink)] placeholder-[var(--t-line2)] focus:outline-none resize-none leading-relaxed transition-colors"
              style={{ minHeight: "38px", maxHeight: "120px", overflowY: "auto" }}
              onInput={e => {
                e.target.style.height = "auto";
                e.target.style.height = Math.min(e.target.scrollHeight, 120) + "px";
              }}
            />
            <button
              onClick={() => sendMessage()}
              disabled={!input.trim() || loading}
              className="h-9 w-9 rounded-xl bg-[var(--t-accent-soft)] hover:bg-[var(--t-accent-soft)] disabled:bg-[var(--t-line)] disabled:cursor-not-allowed flex items-center justify-center transition-all shrink-0"
            >
              {loading ? (
                <div className="h-3 w-3 border-2 border-[var(--t-line2)] border-t-transparent rounded-full animate-spin" />
              ) : (
                <svg className="h-3.5 w-3.5 text-[var(--t-ink)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
                </svg>
              )}
            </button>
          </div>
          <div className="mt-2 text-[13px] text-[var(--t-soft)] text-center">Enter to send · Shift+Enter for new line · Drag grip to reposition</div>
        </div>
      </div>
    </>
  );
}

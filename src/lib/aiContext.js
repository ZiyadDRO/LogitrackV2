// What the AI assistant is told, built from exactly what the Fleet is showing.
//
// Two rules shape everything here:
//
//  1. SCOPE. Only the products loaded right now go in: the store in use, or the uploaded
//     spreadsheets when no store is. A paused store contributes its NAME and nothing else,
//     so the assistant can say "that store is paused" without carrying its data (or its
//     tokens) on every question. Anything kept in this browser for other workspaces
//     (folders, POs, supplier assignments, a forecast still arriving from before a switch)
//     is filtered to the products showing before it reaches the prompt.
//
//  2. SIZE. Groq's free tier allows ~8,000 tokens per request per model. The prompt is a
//     one-line roster for every product plus full detail for the few the question is
//     about, so a 35-product fleet costs ~4,500 tokens and a question about one product
//     still gets everything the tool knows about it.

import { namedSuppliers, todayStr } from "./helpers.js";

const DEFAULT_PARAMS = { stock: 500, leadTime: 14, coverage: 30, strategy: "balanced" };

const n0 = (v) => (v == null || !Number.isFinite(Number(v)) ? "?" : Math.round(Number(v)).toLocaleString("en-US"));
const n1 = (v) => (v == null || !Number.isFinite(Number(v)) ? "?" : String(Math.round(Number(v) * 10) / 10));
const money = (v) => (v == null || !Number.isFinite(Number(v)) ? "?" : `$${Number(v).toFixed(2)}`);
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** "2026-12-24" -> "Dec 24" (plus the year when it isn't this year). Parsed by hand: new
 *  Date("2026-12-24") is UTC midnight, which is Dec 23 in every US time zone. */
function day(iso, withYear = false) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
  if (!m) return String(iso || "?");
  const y = Number(m[1]);
  const showYear = withYear || y !== new Date().getFullYear();
  return `${MON[Number(m[2]) - 1]} ${Number(m[3])}${showYear ? ` ${y}` : ""}`;
}
function clock(ts) {
  if (!ts) return null;
  const d = typeof ts === "number" ? new Date(ts < 1e12 ? ts * 1000 : ts) : new Date(ts);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
const isoToday = () => todayStr();   // the store's date, same as every other screen
const addDays = (iso, k) => {
  const [y, m, d] = iso.split("-").map(Number);
  const t = new Date(Date.UTC(y, m - 1, d + k));
  return t.toISOString().slice(0, 10);
};

// ─────────────────────────────────────────────
// SCOPE
// ─────────────────────────────────────────────
/** The forecasts for the products showing, once each. skuList is what the sidebar and the
 *  Fleet render; skuForecasts can briefly hold a product from the previous workspace (a
 *  refresh that was in flight during a store switch lands after the new list). */
export function shownForecasts(skuForecasts, skuList) {
  const ids = new Set((skuList || []).map((s) => String(s.id)));
  const seen = new Set();
  return (skuForecasts || []).filter((s) => {
    const id = String(s?.skuId ?? "");
    if (!id || seen.has(id) || (ids.size && !ids.has(id))) return false;
    seen.add(id);
    return true;
  });
}

// ─────────────────────────────────────────────
// SMALL FORMATTERS (shared by the roster, the detail block and the reorder brief)
// ─────────────────────────────────────────────
/** Reorder timing as a phrase. daysUntilReorder is null when there is no reorder date
 *  (nothing runs out within the forecast, or the stock was never counted, where the
 *  server refuses to do arithmetic on an assumed figure). -1 is NOT a sentinel: it is a
 *  reorder date that passed yesterday. `null <= 0` is true in JavaScript, which is how
 *  uncounted products used to be briefed to the assistant as OVERDUE. */
export function reorderPhrase(s) {
  if (s?.stockCounted === false) return "unknown (stock not counted)";
  const d = s?.daysUntilReorder;
  if (d == null) return "none needed";
  if (d <= 0) return d === 0 ? "due today" : `OVERDUE by ${-d}d`;
  return `in ${d}d`;
}
function stockoutPhrase(s) {
  if (s?.stockCounted === false) return "unknown (stock not counted)";
  if (s?.alreadyOut) return "OUT OF STOCK now";
  const d = s?.daysUntilStockout;
  return d == null ? "not within the forecast" : `in ${d}d`;
}

/** Live shelf price / discount from the store's POS, or null. */
function livePricePhrase(s) {
  const lp = s?.livePrice;
  if (!lp) return null;
  const src = lp.source ? ` (${lp.source})` : "";
  if (lp.discounted) {
    const pct = lp.discountPct != null ? ` ${Math.round(lp.discountPct)}% off` : "";
    const name = lp.discountName ? ` "${lp.discountName}"` : "";
    const until = lp.discountUntil ? ` until ${day(lp.discountUntil)}` : "";
    return `on sale now${name}${pct}: ${money(lp.currentPrice)} vs shelf ${money(lp.listPrice)}${until}${src}`;
  }
  return `shelf price ${money(lp.listPrice ?? lp.currentPrice)}${src}`;
}

/** Units sold today so far, from the store's live feed (only when the feed is for today). */
function todayUnits(s) {
  const t = s?.todaySales;
  if (!t || t.unsupported || !t.forToday) return null;
  return Number(t.units) || 0;
}

/** Holiday stretches whose forecast actually moves, soonest first. */
function holidayStretches(s, limit = 2) {
  const rep = s?.holidays;
  if (!rep?.effects?.length) return [];
  const today = isoToday();
  const live = rep.method === "shape"
    ? rep.effects.filter((e) => e.status === "moves")
    : rep.effects.filter((e) => e.active);
  return live
    .filter((e) => e.next?.end && e.next.end >= today)
    .sort((a, b) => String(a.next.start).localeCompare(String(b.next.start)))
    .slice(0, limit)
    .map((e) => {
      const nx = e.next;
      const bits = [`${e.name} ${day(nx.start)} to ${day(nx.end)}`];
      if (e.forecast?.units != null) {
        bits.push(`forecast ${n0(e.forecast.units)} units` +
          (e.forecast.coreUnits != null && e.coreLabel ? ` (${n0(e.forecast.coreUnits)} over ${e.coreLabel})` : "") +
          (e.forecast.underway ? `, underway, ${n0(e.forecast.sold)} sold so far` : ""));
      } else if (e.multiplier != null) {
        bits.push(`sales x${n1(e.multiplier)} on those days`);
      }
      if (e.peak?.date) bits.push(`busiest ${day(e.peak.date)}`);
      // Not when the history was moved to end yesterday: past years are on real dates
      // and this year's total on the moved ones, so the comparison would mislead.
      if (e.lastYear?.units != null && !rep.shiftDays) bits.push(`last year ${n0(e.lastYear.units)}${e.lastYear.allDays === false ? ` (only ${e.lastYear.days} of the days on file)` : ""}`);
      return bits.join(", ");
    });
}

/** Short tags for the roster: the facts that decide whether a product belongs in an answer. */
function rosterFlags(s, sc, grp) {
  const f = [];
  if (s.alreadyOut) f.push("OUT OF STOCK");
  if (sc?.offSeason) f.push("out of season");
  else if (sc?.dormant || (s.inactive && !sc)) f.push("stopped selling");
  if (s.tooNew) f.push("too new to forecast");
  else if (s.young) f.push(`new (${s.daysOfHistory ?? "?"}d history)`);
  const lp = s.livePrice;
  if (lp?.discounted) f.push(`on sale now${lp.discountPct != null ? ` ${Math.round(lp.discountPct)}% off` : ""}`);
  else if (sc?.onPromoToday) f.push("on promotion");
  const t = todayUnits(s);
  if (t != null) f.push(`today ${n0(t)}`);
  if (s.forecastDetails?.oneOff) f.push("one-off rush left out");
  if (s.rateCheck?.shortfallPct) f.push(`forecast ${s.rateCheck.shortfallPct}% under recent sales`);
  if (grp?.borrowsFrom) f.push("borrows from its family");
  if ((s.dataAgeDays ?? 0) > 3) f.push(`sales data ${s.dataAgeDays}d old`);
  return f;
}

export function rosterLine(s, sc, po, params, grp) {
  const counted = s.stockCounted !== false;
  const stock = s.stockUsed ?? params?.stock;
  const cover = !counted ? "?" : !sc ? "?" : sc.daysOfCover == null ? "365+" : `${sc.daysOfCover}d`;
  const parts = [
    `${s.skuName} (${s.skuId})`,
    sc ? sc.status : "unrated",
    counted ? `stock ${n0(stock)}` : "stock NOT COUNTED",
    `cover ${cover}`,
    `reorder ${reorderPhrase(s).replace(/^in /, "")}`,
    counted ? `order ${n0(s.orderQty)}` : "",
    `${n1(s.avgDailyDemand)}/day`,
    s.demandVolatilityLabel || "",
    po ? `PO ${n0(po.qty)} due ${po.delivery ? day(po.delivery) : "?"}` : "",
    sc?.returnTier && sc.returnTier !== "—" ? `grade ${sc.returnTier}` : "",
  ];
  const flags = rosterFlags(s, sc, grp);
  if (flags.length) parts.push(flags.join(", "));
  return parts.filter(Boolean).join(" | ");
}

// ─────────────────────────────────────────────
// WHICH PRODUCTS GET FULL DETAIL
// ─────────────────────────────────────────────
/** Which products deserve the full block for THIS question.
 *
 *  1. anything the question names (by id, or by the words of its name; a word most of the
 *     catalogue shares counts for little, so "Harlow 24in Vanity" ranks that product
 *     above its siblings rather than pulling in every vanity)
 *  2. if the question names nothing, the product the previous question was about, so a
 *     follow-up like "and when should I order it?" keeps its subject
 *  3. then anything not Healthy, then the soonest reorder, up to the cap.
 *
 *  The cap is 3 because of the free tier's ~8,000-token request ceiling: a detail block
 *  is ~500 tokens, the roster ~1,500 on a 35-product fleet, and the reply needs room. */
export function pickDetailSkus(question, skuForecasts, scorecard, cap = 3, previousQuestion = "") {
  const scMap = {};
  (scorecard?.rows || []).forEach((r) => { scMap[r.skuId] = r; });

  const urgency = (s) => {
    const d = s.stockCounted === false ? null : s.daysUntilReorder;
    return d == null ? 1e9 : d;
  };
  const STOP = new Set(["the", "and", "for", "how", "why", "what", "are", "its", "was", "you", "can", "did",
    "has", "any", "per", "not", "all", "much", "many", "with", "this", "that", "from", "about", "should", "does"]);
  const words = (s) => new Set(String(s.skuName || "").toLowerCase().split(/[^a-z0-9]+/)
    .filter((w) => w.length >= 3 && !STOP.has(w)));
  const common = new Map();
  skuForecasts.forEach((s) => words(s).forEach((w) => common.set(w, (common.get(w) || 0) + 1)));
  const tooCommon = Math.max(2, Math.ceil(skuForecasts.length * 0.25));
  // Whole words only ("tee" must not match "between"), singular or plural either way.
  const scoreFor = (text) => {
    const raw = String(text || "").toLowerCase();
    const qw = new Set();
    raw.split(/[^a-z0-9]+/).filter(Boolean).forEach((w) => {
      qw.add(w); qw.add(w + "s"); qw.add(w.replace(/s$/, "")); qw.add(w.replace(/es$/, ""));
    });
    const out = [];
    skuForecasts.forEach((s) => {
      const id = String(s.skuId || "").toLowerCase();
      let score = 0;
      if (id && raw.includes(id)) score += 100;
      words(s).forEach((w) => { if (qw.has(w)) score += (common.get(w) >= tooCommon ? 1 : 5); });
      if (score > 0) out.push([score, String(s.skuId)]);
    });
    if (!out.length) return [];
    // Keep the products that match about as well as the best one: "zip hoodie" should be
    // the Zip Hoodie, not every hoodie.
    // Ties (a question about "candles" names every candle equally) go to the most urgent.
    const urg = Object.fromEntries(skuForecasts.map((s) => [String(s.skuId), urgency(s)]));
    out.sort((a, b) => b[0] - a[0] || urg[a[1]] - urg[b[1]]);
    const best = out[0][0];
    return out.filter(([sc]) => sc >= Math.min(best, 5) && sc * 2 > best).map(([, id]) => id);
  };

  let named = scoreFor(question);
  if (!named.length && previousQuestion) named = scoreFor(previousQuestion).slice(0, 1);
  const out = new Set(named.slice(0, cap));
  // A question about specific products gets detail on those only; the roster still
  // lists everything else. Filling the spare slots with unrelated products cost ~600
  // tokens each for nothing.
  if (out.size) return out;

  const needy = [], rest = [];
  skuForecasts.forEach((s) => {
    if (out.has(String(s.skuId))) return;
    const st = scMap[s.skuId]?.status;
    (st && st !== "Healthy" ? needy : rest).push(s);
  });
  [...needy.sort((a, b) => urgency(a) - urgency(b)), ...rest.sort((a, b) => urgency(a) - urgency(b))]
    .forEach((s) => { if (out.size < cap) out.add(String(s.skuId)); });
  return out;
}

// ─────────────────────────────────────────────
// FLEET-LEVEL SECTIONS
// ─────────────────────────────────────────────
/** Which store is showing and which are paused. Paused stores: names only. */
export function storeSection(live, shown) {
  const ws = live?.workspace;
  const conns = live?.connections?.connections || [];
  const sync = live?.sync;
  const lines = ["=== WHAT THE FLEET IS SHOWING ==="];
  const showingId = ws?.connectionId || null;
  const showingConn = conns.find((c) => c.id === showingId);
  const src = (c) => (c?.source ? c.source[0].toUpperCase() + c.source.slice(1) : "store");

  if (showingId) {
    lines.push(`Showing: the ${src(showingConn)} store "${ws.label || showingConn?.label || "store"}", live-connected. ${shown.length} products loaded.`);
    const lr = sync?.lastRun;
    const syncBits = [];
    if (lr?.finishedAt) syncBits.push(`last nightly sync ${clock(lr.finishedAt)}${lr.ok ? "" : ` (did not finish: ${lr.result?.reason || "error"})`}`);
    if (sync?.nextRunAt) syncBits.push(`next ${clock(sync.nextRunAt)}`);
    const lf = sync?.sampler?.lastFiledAt;
    if (lf) syncBits.push(`stock last read from the store ${clock(lf)} (read hourly)`);
    if (syncBits.length) lines.push(`Store sync: ${syncBits.join("; ")}.`);
    // Today's sales feed: one status for the whole store, carried on every forecast.
    const t = shown.find((s) => s.todaySales && !s.todaySales.unsupported)?.todaySales;
    if (t?.forToday) {
      lines.push(`Today's sales so far (live from the store${t.fetchedAt ? `, as of ${clock(t.fetchedAt)}` : ""}${t.stale ? ", feed is behind" : ""}): ${n0(t.unitsTotal)} units across ${t.skus} products. These are NOT in the daily history yet; they land after tonight's sync.`);
    } else if (shown.some((s) => s.todaySales?.unsupported)) {
      lines.push("Today's sales so far: this store's connection does not report them.");
    } else {
      lines.push("Today's sales so far: no reading for today yet.");
    }
  } else {
    lines.push(`Showing: the manager's uploaded spreadsheets (no store is in use). ${shown.length} products loaded. Nothing here is live: no today's sales, no live prices.`);
  }
  const paused = conns.filter((c) => c.id !== showingId && c.active === false);
  const otherActive = conns.filter((c) => c.id !== showingId && c.active !== false);
  if (paused.length) {
    lines.push(`Paused stores (their products are set aside and NOT in this data): ${paused.map((c) => `"${c.label}" (${src(c)})`).join(", ")}.`);
  }
  if (otherActive.length && !showingId) {
    lines.push(`In use but not loaded yet: ${otherActive.map((c) => `"${c.label}"`).join(", ")}.`);
  }
  // Freshness: every date on screen advances every day; the sales under it only move when
  // new data arrives. Say when that last happened if it's been a while.
  const newest = shown.map((s) => s.lastSalesDate).filter(Boolean).sort().pop();
  const age = Math.min(...shown.map((s) => s.dataAgeDays ?? 0));
  if (newest) lines.push(`Newest sale on file: ${day(newest, true)}${age > 2 ? ` (${age} days ago, so recent sales are missing)` : ""}.`);
  lines.push("=== END ===\n");
  return lines.join("\n");
}

/** Store-closed days the forecast treats as zero-sales days, upcoming only. */
export function closedDaysLine(cd) {
  const set = cd?.settings;
  if (!set) return "";
  const today = isoToday();
  const until = addDays(today, 180);
  const dates = new Set((set.dates || []).filter((d) => d >= today && d <= until));
  (set.yearly || []).forEach((k) => (cd.yearlyDates?.[k] || []).forEach((d) => { if (d >= today && d <= until) dates.add(d); }));
  const names = (set.yearly || []).map((k) => cd.yearlyOptions?.find((o) => o.key === k)?.name || k);
  if (!dates.size && !names.length) return "Store closed days: none set (every day is treated as a selling day).\n";
  const list = [...dates].sort().slice(0, 10).map((d) => day(d));
  return `Store closed days (forecast is zero on them, and their demand is not moved to other days): ${list.length ? `next: ${list.join(", ")}` : "none in the next 6 months"}${names.length ? `; every year: ${names.join(", ")}` : ""}.\n`;
}

/** Live accuracy: last week's forecasts graded against what actually sold. */
export function liveAccuracyLine(ll) {
  const sm = ll?.summary;
  if (!sm) return "";
  const acc = sm.accuracy || {}, cal = sm.calibration || {};
  if (!acc.n) {
    return `Live accuracy (forecasts graded against real sales): nothing graded yet${sm.nextDueInDays != null ? `, first grading in ${sm.nextDueInDays} days` : ""}.\n`;
  }
  const bias = acc.bias == null ? "" : `, bias ${acc.bias > 0 ? "+" : ""}${acc.bias}% (${acc.bias > 2 ? "forecasting high" : acc.bias < -2 ? "forecasting low" : "balanced"})`;
  const hit = cal.hitRate == null ? "" : `, actual sales inside the forecast range ${cal.hitRate}% of the time vs ${cal.nominal}% targeted (${cal.verdict})`;
  return `Live accuracy (${acc.n} forecasts graded against real sales): typical miss ${acc.wape}%${bias}${hit}.\n`;
}

/** The backtest (past sales held out and re-forecast), one line. `bt` is
 *  { available, result: { ranAt, overall, tested } } or a running job. */
export function backtestLine(bt) {
  const r = bt?.available ? bt.result : null;
  if (!r) return bt?.job?.status === "running" ? "Backtest: running now; protection levels are estimates until it finishes.\n" : "";
  const ov = r.overall || {};
  const bits = [];
  if (ov["WAPE%"] != null) bits.push(`typical miss ${ov["WAPE%"]}% of units`);
  if (ov["bias%"] != null) bits.push(`bias ${ov["bias%"] > 0 ? "+" : ""}${n1(ov["bias%"])}%`);
  if (ov["service_achieved%"] != null) bits.push(`safety stock covered demand in ${ov["service_achieved%"]}% of tests`);
  if (r.tested != null) bits.push(`${r.tested} products tested`);
  if (r.ranAt) bits.push(`ran ${clock(r.ranAt)}`);
  return bits.length ? `Backtest (past sales held out and re-forecast): ${bits.join(", ")}.\n` : "";
}

// ─────────────────────────────────────────────
// PER-PRODUCT DETAIL
// ─────────────────────────────────────────────
function detailBlock(s, ctx) {
  const { scMap, grpMap, openPOs, skuParams, folderOf, liveMap, btMap } = ctx;
  const po = openPOs?.[s.skuId];
  const params = skuParams?.[s.skuId] ?? DEFAULT_PARAMS;
  const sc = scMap[s.skuId];
  const gr = grpMap[s.skuId];
  const fd = s.forecastDetails || {};
  const prot = s.protection;
  const recOpt = (prot?.options || []).find((o) => o.key === prot?.recommended);
  const L = [];

  L.push(`━━━ ${s.skuName} (${s.skuId}) ━━━ folder: ${folderOf[s.skuId] || "none"}`);

  // Stock and settings
  if (s.stockCounted === false) {
    L.push(`Stock: NOT COUNTED (the ${n0(s.assumedStock ?? s.stockUsed)} used is a placeholder), so stockout date, reorder date and order quantity are withheld. Ask the manager for a count.`);
  } else {
    const since = s.sinceCount?.since && s.derivedStock != null
      ? `; counted ${day(s.sinceCount.since)}, ${n0(s.sinceCount.total)} sold since, so ~${n0(s.derivedStock)} on the shelf now` : "";
    L.push(`Stock: ${n0(s.stockUsed ?? params.stock)} (source: ${s.stockSource === "live" ? "read from the store" : s.stockSource === "sheet" ? "uploaded sheet" : s.stockSource || "entered"}${since}).`);
  }
  L.push(`Settings: lead time ${params.leadTime}d, coverage ${params.coverage}d. Protection: ${prot ? `${prot.label} ${prot.servicePct}% (tool recommends ${recOpt?.label ?? prot.recommended}; basis ${prot.source === "backtest" ? "backtest's cheapest tier" : prot.source === "economics" ? "cost curve" : prot.source === "margin" ? "margin rule" : "default, no unit cost"}${prot.overridden ? "; MANAGER OVERRIDE" : ""}${prot.marginPct != null ? `; margin ${prot.marginPct}%` : ""})` : "n/a"}.`);

  // What kind of seller it is
  L.push(`History: ${n0(s.daysOfHistory)} days, ${n0(s.totalUnitsSold)} sold.${s.status && s.status !== "FORECAST_ELIGIBLE" && s.statusMessage ? ` Data: ${s.statusMessage}` : ""}`);
  if (s.demandStory) L.push(`Pattern: ${s.demandStory}`);
  if (s.youngMessage) L.push(`New product: ${s.youngMessage}`);
  if (s.inactiveMessage) L.push(`Inactive: ${s.inactiveMessage}`);
  if (s.tooNewMessage) L.push(`Too new: ${s.tooNewMessage}`);
  if (sc?.offSeason) L.push("Out of season: quiet now the same way it was this time last year, so it is NOT dead stock; it keeps its status and its season is expected back.");
  else if (sc?.dormant) L.push("Stopped selling: no sales lately and no seasonal reason for it.");
  L.push(`Volatility: ${s.demandVolatilityLabel || "n/a"}${s.residualDispersion != null ? `, ${n1(s.residualDispersion)}x the smallest swing possible at ~${n1(s.demandRefMean)}/day (1.0x = as steady as that volume allows)` : ""}; daily swing ±${n1(s.residualStd)}.`);

  // Grouping
  if (gr) {
    const attrs = gr.attributes && Object.keys(gr.attributes).length
      ? Object.entries(gr.attributes).map(([k, v]) => `${k}=${v}`).join(", ") : "none";
    L.push(`Group: ${gr.group ?? "none"} (attributes: ${attrs}); similar: ${gr.similar?.length ? gr.similar.map((x) => x.skuName).join(", ") : "none"}; pooled global model: ${gr.usesGlobal ? "yes" : "no"}${gr.borrowsFrom ? `; borrows from ${typeof gr.borrowsFrom === "string" ? gr.borrowsFrom : gr.borrowsFrom.name || gr.borrowsFrom.skuName || JSON.stringify(gr.borrowsFrom)}` : ""}${gr.familyNote ? `; family: ${gr.familyNote}` : ""}.`);
  }

  // How the forecast is made
  L.push(`Engine: ${s.winningModel ?? "?"}${fd.routeLabel ? ` [${fd.routeLabel}]` : ""}. Why: ${fd.whyChosen || s.routeReason || "n/a"}`);
  if (fd.seasonality?.text) L.push(`Seasonality: ${fd.seasonality.text}`);
  if (fd.price?.text) L.push(`Price: ${fd.price.text}`);
  const hols = holidayStretches(s, 2);
  if (fd.holidays?.text && !hols.length) L.push(`Holidays: ${fd.holidays.text}`);
  if (fd.oneOff?.text) L.push(`One-off rush: ${fd.oneOff.text}`);
  if (fd.trend?.active) L.push(`Launch ramp: ${fd.trend.text}`);
  if (s.rateCheck?.message) L.push(`Rate check: ${s.rateCheck.message}`);
  if (fd.interval?.highOffset != null) L.push(`Daily range: ${fd.interval.level ?? 80}% of days land within +${n1(fd.interval.highOffset)} / ${n1(fd.interval.lowOffset)} units of the forecast line.`);

  // Numbers
  const math = s.stockCounted === false ? "withheld (stock not counted)"
    : `target ${n0(s.targetInventory)} (coverage ${n0(s.coverageQty)} + safety ${n0(s.safetyStock)}) minus stock at delivery ${n0(s.stockAtDelivery)} = order ${n0(s.orderQty)}`;
  L.push(`Order: ${math}. Runs out ${stockoutPhrase(s)}; reorder ${reorderPhrase(s)}. Forecast ${n1(s.avgDailyDemand)}/day.${s.orderGuardrail?.active ? ` Thin-history guardrail: ${s.orderGuardrail.capExceeded ? `suggest ~${n0(s.orderGuardrail.suggestedCap)} instead of ${n0(s.orderGuardrail.rawOrderQty)}` : "order is provisional"}.` : ""}`);
  const cm = s.currentMonth || {};
  const cur = (s.monthCards || []).find((c) => c.isCurrent);
  L.push(`This month${cur ? ` (${cur.monthLabel})` : ""}: ${n0(cm.unitsSoFar)} sold so far, ~${n0(cm.forecastRemaining)} more forecast${cur ? `, ~${n0(cur.projectedTotal)} total (range ${n0(cur.rangeLow)}-${n0(cur.rangeHigh)})` : ""}. Last month ${n0(cm.lastMonthTotal)}. Same month last year ${cm.lastYearTotal == null ? "n/a" : n0(cm.lastYearTotal)}.`);
  const next = (s.monthCards || []).filter((c) => !c.isCurrent);
  if (next.length) L.push(`Next months: ${next.map((c) => `${c.monthLabel} ~${n0(c.projectedTotal)} (${n0(c.rangeLow)}-${n0(c.rangeHigh)})`).join("; ")}.`);
  if (hols.length) L.push(`Holiday stretches ahead: ${hols.join(" | ")}.`);

  // Live store facts
  const lp = livePricePhrase(s);
  const t = todayUnits(s);
  if (lp || t != null) L.push(`Live from the store: ${[lp, t != null ? `${n0(t)} sold today so far` : null].filter(Boolean).join("; ")}.`);

  // Orders, events, promos
  L.push(po ? `Open PO: ${n0(po.qty)} units ordered ${po.ordered ? day(po.ordered) : "?"}, due ${po.delivery ? day(po.delivery) : "?"}${po.supplier ? ` from ${po.supplier}` : ""}.` : "Open PO: none.");
  const evs = [...(s.events || []), ...(s.posEvents || []).map((e) => ({ ...e, label: e.label || "from the store" }))];
  if (evs.length) L.push(`Events: ${evs.slice(-6).map((e) => `${e.type} ${day(e.date)}${e.end_date && e.end_date !== e.date ? `-${day(e.end_date)}` : ""}${e.label ? ` (${e.label})` : ""}${e.new_price ? ` new price $${e.new_price}` : ""}${e.discount_pct ? ` ${e.discount_pct}% off` : ""}`).join("; ")}${evs.length > 6 ? ` (+${evs.length - 6} earlier)` : ""}.`);
  if (s.upcomingPromos?.length) L.push(`Upcoming promotions: ${s.upcomingPromos.map((p) => `${p.label || "Promo"} ${day(p.date)}${p.end_date && p.end_date !== p.date ? `-${day(p.end_date)}` : ""}${p.discount_pct ? ` ${p.discount_pct}% off` : ""}`).join("; ")}.`);
  if (s.priceChangeWarning && s.priceChangeMessage) L.push(`Price warning: ${s.priceChangeMessage}`);

  // Scorecard
  if (sc) {
    const cov = sc.daysOfCover == null ? "365+" : `${sc.daysOfCover}d`;
    const inb = sc.unitsOnOrder > 0 ? ` (${sc.daysOfCoverWithInbound == null ? "365+" : sc.daysOfCoverWithInbound + "d"} counting ${n0(sc.unitsOnOrder)} on order)` : "";
    L.push(`Scorecard: ${sc.status}; cover ${cov}${inb}; sell-through ${sc.sellThrough == null ? "n/a" : Math.round(sc.sellThrough * 100) + "%"}; grade ${sc.returnTier}${sc.marginPct != null ? ` (margin ${sc.marginPct}%)` : " (no cost)"}; cash tied up ${sc.carryingValue != null ? "$" + n0(sc.carryingValue) : "n/a"}. Action: ${sc.recommendation?.action} (${sc.recommendation?.reason})`);
    if (sc.flags?.length) L.push(`Flags: ${sc.flags.map((f) => f.text).join(" | ")}`);
    if (sc.onPromoToday && sc.baselineView) L.push(`On promotion today (status is the on-sale view); without the promo: ${sc.baselineView.status}, ${sc.baselineView.daysOfCover == null ? "365+" : sc.baselineView.daysOfCover + "d"} cover.`);
  }

  // Accuracy for this product: the backtest's, then live grading
  const bt = btMap[s.skuId];
  if (bt && bt["WAPE%"] != null) L.push(`Backtest: typical miss ${bt["WAPE%"]}% over ${bt.windows ?? "?"} tests${bt.reportable === false ? " (too few tests to lean on)" : ""}.`);
  const la = liveMap[s.skuId];
  if (la?.scored) L.push(`Live accuracy: ${la.scored} weeks graded, typical miss ${la.wape ?? "?"}%, bias ${la.bias == null ? "?" : (la.bias > 0 ? "+" : "") + la.bias + "%"}${la.hitRate != null ? `, in range ${la.hitRate}%` : ""}.`);

  return L.join("\n");
}

// ─────────────────────────────────────────────
// SUPPLIERS (only the ones supplying products that are showing)
// ─────────────────────────────────────────────
function supplierSection(suppliers, shownIds) {
  const list = namedSuppliers(suppliers)
    .map((sup) => ({ ...sup, skuIds: (sup.skuIds || []).filter((id) => shownIds.has(String(id))) }))
    .filter((sup) => sup.skuIds.length || (sup.orders || []).some((o) => o.skuId && shownIds.has(String(o.skuId))));
  if (!list.length) return "";
  const lines = ["=== SUPPLIERS (for the products showing) ==="];
  const days = (a, b) => Math.round((new Date(b) - new Date(a)) / 86400000);
  list.forEach((sup) => {
    const orders = (sup.orders || []).filter((o) => !o.skuId || shownIds.has(String(o.skuId)));
    const done = orders.filter((o) => o.receivedDate && o.orderedDate);
    const transit = orders.filter((o) => !o.receivedDate);
    const lts = done.map((o) => days(o.orderedDate, o.receivedDate)).filter((d) => d >= 0);
    const avg = lts.length ? Math.round(lts.reduce((a, b) => a + b, 0) / lts.length) : null;
    const withExp = done.filter((o) => o.expectedDate);
    const vars = withExp.map((o) => days(o.expectedDate, o.receivedDate));
    const onTime = vars.length ? Math.round(vars.filter((v) => v <= 0).length / vars.length * 100) : null;
    const avgVar = vars.length ? Math.round(vars.reduce((a, b) => a + b, 0) / vars.length * 10) / 10 : null;
    lines.push(`${sup.name}: supplies ${sup.skuIds.join(", ") || "none showing"}; ${done.length} orders received; avg lead ${avg ?? "?"}d; on time ${onTime ?? "?"}%${avgVar != null ? `; avg ${avgVar > 0 ? avgVar + "d late" : Math.abs(avgVar) + "d early/on time"}` : ""}.`
      + (transit.length ? ` In transit: ${transit.map((o) => `${o.qty || "?"} units${o.skuId ? ` of ${o.skuId}` : ""} ordered ${day(o.orderedDate)}${o.expectedDate ? `, expected ${day(o.expectedDate)}` : ""}`).join("; ")}.` : ""));
  });
  lines.push("=== END SUPPLIERS ===\n");
  return lines.join("\n");
}

// ─────────────────────────────────────────────
// HISTORY (opt-in toggle)
// ─────────────────────────────────────────────
function historySection(history, shownIds) {
  const rows = (history?.skus || []).filter((h) => shownIds.has(String(h.skuId)));
  if (!rows.length) return "";
  const lines = [`=== SALES HISTORY (monthly totals, plus the last ${history.recentDays ?? 90} days daily) ===`];
  rows.forEach((h) => {
    lines.push(`${h.skuName} (${h.skuId}) ${h.dateRange?.start} to ${h.dateRange?.end}, ${n0(h.totalUnits)} units`);
    if (h.monthly?.length) lines.push("  monthly: " + h.monthly.map((m) => `${m.month} ${m.units}${m.avgPrice != null ? ` @$${m.avgPrice}` : ""}${m.promoDays ? ` ${m.promoDays}p` : ""}`).join(" | "));
    if (h.recentDaily?.length) lines.push("  daily: " + h.recentDaily.map((r) => `${r.date.slice(5)} ${r.units}${r.promo ? "p" : ""}`).join(" | "));
  });
  lines.push("=== END SALES HISTORY ===\n");
  return lines.join("\n");
}

// ─────────────────────────────────────────────
// THE WHOLE CONTEXT
// ─────────────────────────────────────────────
/**
 * @param shown       forecasts for the products showing (see shownForecasts)
 * @param detailFor   Set of skuIds to describe in full
 * @param data        { openPOs, skuParams, skuList, folders, suppliers, scorecard, groups,
 *                      history, live: { workspace, connections, sync, closedDays, livelog, backtest } }
 */
export function buildFleetContext(shown, detailFor, data) {
  const { openPOs, skuParams, folders, suppliers, scorecard, groups, history, live } = data || {};
  if (!shown?.length) {
    return storeSection(live, []) + "No products are loaded right now.";
  }
  const shownIds = new Set(shown.map((s) => String(s.skuId)));
  const scMap = {}; (scorecard?.rows || []).forEach((r) => { scMap[r.skuId] = r; });
  const grpMap = {}; (groups?.skus || []).forEach((r) => { grpMap[r.skuId] = r; });
  const liveMap = {}; (live?.livelog?.perSku || []).forEach((r) => { liveMap[r.sku] = r; });

  // Folders, limited to the products showing. A folder with none of them showing is left
  // out entirely (its products belong to another workspace).
  const folderOf = {};
  const folderList = Object.values(folders || {})
    .map((f) => ({ name: f.name, ids: (f.skuIds || []).map(String).filter((id) => shownIds.has(id)) }))
    .filter((f) => f.ids.length);
  folderList.forEach((f) => f.ids.forEach((id) => { if (!folderOf[id]) folderOf[id] = f.name; }));

  // Roster, grouped by the manager's folders so the folder list isn't sent twice.
  const byId = Object.fromEntries(shown.map((s) => [String(s.skuId), s]));
  const line = (s) => rosterLine(s, scMap[s.skuId], openPOs?.[s.skuId], skuParams?.[s.skuId] ?? DEFAULT_PARAMS, grpMap[s.skuId]);
  const roster = [
    `=== PRODUCTS SHOWING (${shown.length}) ===`,
    "name (id) | status | stock | days of cover | reorder | order qty | forecast/day | volatility | open PO | profit grade | notes",
  ];
  if (folderList.length) {
    folderList.forEach((f) => { roster.push(`[Folder "${f.name}"]`); f.ids.forEach((id) => roster.push(line(byId[id]))); });
    const loose = shown.filter((s) => !folderOf[s.skuId]);
    if (loose.length) { roster.push("[Not in a folder]"); loose.forEach((s) => roster.push(line(s))); }
  } else {
    shown.forEach((s) => roster.push(line(s)));
  }
  roster.push("=== END PRODUCTS ===\n");

  // Fleet summary
  const sum = [];
  if (scorecard?.distribution) {
    const d = scorecard.distribution;
    sum.push(`Status counts: Stockout risk ${d["Stockout risk"] || 0}, Reorder due ${d["Reorder due"] || 0}, Overstocked ${d["Overstocked"] || 0}, Dead stock ${d["Dead stock"] || 0}, Healthy ${d["Healthy"] || 0}.`);
    const offs = (scorecard.rows || []).filter((r) => r.offSeason && shownIds.has(String(r.skuId)));
    if (offs.length) sum.push(`Out of season now (quiet as usual for this time of year, not dead): ${offs.map((r) => r.skuName).join(", ")}.`);
    const attn = (scorecard.needsAttention || []).filter((r) => shownIds.has(String(r.skuId))).slice(0, 5);
    if (attn.length) sum.push(`Most urgent: ${attn.map((r) => `${r.skuName} (${r.status})`).join(", ")}.`);
  }
  if (groups?.skus?.length) {
    const byGroup = {};
    groups.skus.filter((g) => shownIds.has(String(g.skuId))).forEach((g) => { (byGroup[g.group || "ungrouped"] ||= []).push(g.skuName); });
    sum.push(`Product groups (by ${groups.groupColumns?.length ? groups.groupColumns.join(", ") : "nothing yet, no categories given"}): ${Object.entries(byGroup).map(([g, names]) => `${g} (${names.length})`).join(", ")}.`);
  }
  const hol = {};
  const soon = addDays(isoToday(), 90);
  shown.forEach((s) => {
    const rep = s.holidays;
    (rep?.effects || []).filter((e) => (rep.method === "shape" ? e.status === "moves" : e.active)
      && e.next?.start && e.next.start <= soon && e.next.end >= isoToday())
      .forEach((e) => { (hol[e.name] ||= { next: e.next, n: 0 }).n += 1; });
  });
  const holList = Object.entries(hol).sort((a, b) => String(a[1].next.start).localeCompare(String(b[1].next.start)));
  if (holList.length) sum.push(`Holiday stretches in the next 90 days that change the forecast: ${holList.map(([name, h]) => `${name} ${day(h.next.start)} to ${day(h.next.end)} (${h.n} products)`).join(", ")}.`);
  const uncounted = shown.filter((s) => s.stockCounted === false);
  if (uncounted.length) sum.push(`Stock never counted for ${uncounted.length}: ${uncounted.slice(0, 8).map((s) => s.skuName).join(", ")}${uncounted.length > 8 ? "..." : ""}. Their reorder dates and order quantities are withheld.`);
  const summary = sum.length ? `=== FLEET SUMMARY ===\n${sum.join("\n")}\n` + closedDaysLine(live?.closedDays)
    + liveAccuracyLine(live?.livelog) + backtestLine(live?.backtest) + "=== END SUMMARY ===\n"
    : closedDaysLine(live?.closedDays) + liveAccuracyLine(live?.livelog) + backtestLine(live?.backtest);

  const btMap = {}; (live?.backtest?.result?.bySku || []).forEach((r) => { btMap[r.sku] = r; });
  const ctx = { scMap, grpMap, openPOs, skuParams, folderOf, liveMap, btMap };
  const detail = shown.filter((s) => detailFor?.has(String(s.skuId))).map((s) => detailBlock(s, ctx));
  const detailHead = detail.length
    ? `=== FULL DETAIL for ${detail.length} of ${shown.length} products ===\n`
      + "Other products have only their roster line. If a question needs more about one of\n"
      + "them, name it and say to ask about it by name; don't infer the missing details.\n"
    : "";

  return storeSection(live, shown) + "\n" + roster.join("\n") + "\n" + summary + "\n"
    + detailHead + detail.join("\n\n") + "\n\n" + supplierSection(suppliers, shownIds)
    + historySection(history, shownIds);
}

/** Trim to budget without ever dropping the roster: history goes first, then detail
 *  blocks from the end. */
export function fitToBudget(ctx, budget) {
  if (ctx.length <= budget) return ctx;
  let out = ctx;
  const END = "=== END SALES HISTORY ===\n";
  const h = out.indexOf("=== SALES HISTORY");
  if (h >= 0) {
    const e = out.indexOf(END, h) + END.length;
    const hist = out.slice(h, e);
    const room = budget - (out.length - hist.length) - 120;
    const keep = room > 400 ? hist.slice(0, hist.lastIndexOf("\n", room)) : "";
    out = out.slice(0, h)
      + (keep ? `${keep}\n[History cut short here to fit the free AI limit.]\n${END}`
              : "[Sales history left out: too large for the free AI limit.]\n")
      + out.slice(e);
    if (out.length <= budget) return out;
  }
  for (;;) {
    if (out.length <= budget) break;
    const cut = out.lastIndexOf("\n━━━ ");
    if (cut < 0) break;
    const end = out.indexOf("\n\n", cut + 1);
    const next = out.slice(0, cut) + "\n[Detail for one more product was dropped to fit the free AI limit. Ask about it by name.]"
      + (end > 0 ? out.slice(end) : "");
    if (next.length >= out.length) break;
    out = next;
  }
  return out.length > budget ? out.slice(0, budget) : out;
}

// Shared non-visual helpers, constants and styles used across the app.

// "Today" is computed at CALL time, not module-load time — a tab left open past
// midnight used to keep showing yesterday's dates and urgency until a refresh.
//
// And on the STORE's clock. These used the UTC date, so from 8pm Eastern the browser was
// already on tomorrow: order-by dates showed a day late, "Mark received" recorded
// tomorrow (adding a day to measured lead times), and PO arrivals counted a day short.
// The zone comes from the server (/api/store-clock, set in App via setStoreZone), the same
// one the forecasts use, so the page and the server can't disagree about the date. Until
// it arrives, the browser's own calendar date is used, never UTC.
let _storeZone = null;
export function setStoreZone(zone) { _storeZone = zone || null; }
export function storeZone() { return _storeZone; }
const _pad2 = (n) => String(n).padStart(2, "0");
/** Today's date on the store's clock, "YYYY-MM-DD". */
export function todayStr() {
  const now = new Date();
  if (_storeZone) {
    try {
      const parts = new Intl.DateTimeFormat("en-US", { timeZone: _storeZone, year: "numeric",
        month: "2-digit", day: "2-digit" }).formatToParts(now);
      const get = (t) => parts.find((p) => p.type === t)?.value;
      if (get("year") && get("month") && get("day")) return `${get("year")}-${get("month")}-${get("day")}`;
    } catch { /* unknown zone: fall through to the local calendar */ }
  }
  return `${now.getFullYear()}-${_pad2(now.getMonth() + 1)}-${_pad2(now.getDate())}`;
}
/** Midnight UTC of the store's today: the day grid every date in the app sits on (the
 *  server sends dates as UTC midnights, "2026-08-30" parses as one). Not "now": date
 *  arithmetic from the current moment is what drifted a day in the evening. */
export function todayMs() {
  const [y, m, d] = todayStr().split("-").map(Number);
  return Date.UTC(y, m - 1, d);
}

/* stock defaults to 0, NOT 500.
 *
 * 500 was indefensible: on a 1.2/day seller it reads as 417 days of cover, so the Fleet
 * reported "overstocked, nothing to order" for a product nobody had counted — an error
 * that whispers. 0 is also wrong when the truth is unknown, but it is wrong LOUDLY, and
 * for inventory that is the safer direction to be wrong in.
 *
 * What makes it safe rather than merely loud is `stockSource`. A 0 stamped "unknown"
 * means "nobody has counted this", and the UI says so instead of demanding an urgent
 * reorder — which is the failure sanitizeParams below already warns about. A 0 stamped
 * "live" or "manual" is a real, believable zero and does drive a reorder. */
/* 50, and it must match ASSUMED_STOCK in main.py. There used to be two answers — the
   browser assumed 0 and the server assumed 500 — so the same uncounted product read
   "about to stock out" here and "overstocked" there. Both confident, both invented.
   The number is a placeholder so the arithmetic has something to chew on; what keeps it
   honest is stockSource:"unknown", which suppresses every countdown built on it. */
export const DEFAULT_PARAMS = { stock: 50, stockSource: "unknown", leadTime: 14, coverage: 30,
                                strategy: "balanced", months: 1 };

/** Where a stock figure came from, and whether it can be trusted to act on. */
export const STOCK_SOURCES = {
  manual:  { label: "counted by you",  tone: "amber", trusted: true  },
  live:    { label: "from your store", tone: "green", trusted: true  },
  sheet:   { label: "from your file",  tone: "blue",  trusted: true  },
  unknown: { label: "not counted",     tone: "grey",  trusted: false },
};

export const stockIsCounted = (p) => STOCK_SOURCES[p?.stockSource || "unknown"]?.trusted === true;

/* ── Live availability, from the hourly stock readings ────────────────────────────────
 *
 * The backend samples stock every hour. That is what lets the forecast tell "sold 3
 * because demand was 3" from "sold 3 because it ran out at 11am" — and the second case,
 * uncorrected, teaches the model that demand is falling and quietly orders less.
 *
 * This turns one product's `/api/availability` row into a dot and, where it earns one, a
 * few words. Deliberately: the green case gets NO text. Twenty-eight rows each saying "in
 * stock" is noise, and noise is what stops anyone noticing the one row that says
 * something. The dot alone carries "this is being watched"; text is reserved for the
 * exceptions worth reading.
 */
export const AVAILABILITY_TONES = {
  in:      "bg-[var(--t-good)]",
  wasOut:  "bg-[var(--t-warn)]",
  out:     "bg-[var(--t-bad)]",
  unknown: "bg-[var(--t-line2)]",
};

const _clockTime = (iso) => {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? null
    : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
};

/** → { key, dot, label, title } | null when there is nothing worth drawing. */
export function availabilityChip(state, { samplerEnabled = true } = {}) {
  if (!state) return null;
  if (state.state === "unknown") {
    // Only worth flagging while sampling is meant to be happening. With it switched off,
    // "not sampled" is the expected state for every product and says nothing.
    if (!samplerEnabled) return null;
    return { key: "unknown", dot: AVAILABILITY_TONES.unknown, label: null,
             title: "No stock reading yet. The hourly sampler hasn't reached this product." };
  }
  const seen = _clockTime(state.lastReadingAt);
  if (state.state === "out") {
    const since = _clockTime(state.outSince);
    return {
      key: "out",
      dot: AVAILABILITY_TONES.out,
      label: since ? `out since ${since}` : "out of stock",
      // "about" is not hedging for its own sake: readings are hourly, so the true moment
      // is somewhere in the hour before the first zero. Stating it to the minute would be
      // precision the data does not have.
      title: since
        ? `Out of stock. Last seen with stock before about ${since}; readings are hourly.`
        : "Out of stock.",
    };
  }
  if (state.wentOutToday) {
    return { key: "wasOut", dot: AVAILABILITY_TONES.wasOut, label: "was out today",
             title: `Back in stock (${state.level} units at ${seen}) after hitting zero earlier today. `
                  + "Today's demand is corrected upward." };
  }
  return { key: "in", dot: AVAILABILITY_TONES.in, label: null,
           title: `In stock: ${state.level} units as of ${seen}.` };
}

/** A positive integer, or null. `??` is not enough on its own: an empty string is neither
 *  null nor undefined, so `p.leadTime ?? 14` happily yields "" — and `Number("")` is 0,
 *  while `Number(undefined)` is NaN. Both reach the API as invalid and 422 there. */
export function posInt(v, fallback = null) {
  const n = Math.round(Number(v));
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

/**
 * Repair a params object read from storage or about to be sent.
 *
 * Clearing a numeric field in the UI writes "" (and older builds wrote NaN). Those values
 * PERSIST, so fixing the code that produced them doesn't heal a browser that already
 * saved one — every reload replays the bad value. Sanitising on the way IN is what makes
 * it self-heal, and on the way OUT is what stops any single missed call site mattering.
 */
export function sanitizeParams(p) {
  const out = { ...(p || {}) };
  out.leadTime = posInt(out.leadTime, DEFAULT_PARAMS.leadTime);
  out.coverage = posInt(out.coverage, DEFAULT_PARAMS.coverage);
  out.months   = posInt(out.months,   DEFAULT_PARAMS.months);
  /* Stock is the one field where 0 is a real, meaningful value — you can genuinely have
     none. But Number("") is also 0, so a BLANK box would silently read as "sold out" and
     have the tool screaming stockout for a product nobody had counted yet. Blank and zero
     have to be told apart before the coercion, not after. */
  const blank = out.stock === "" || out.stock == null ||
                (typeof out.stock === "string" && out.stock.trim() === "");
  const st = blank ? NaN : Math.round(Number(out.stock));
  out.stock = Number.isFinite(st) && st >= 0 ? st : DEFAULT_PARAMS.stock;
  /* A blank box is not a count of zero, and the difference has to survive the coercion
     above — otherwise the 0 it lands on is indistinguishable from a counted zero and the
     tool starts demanding a reorder for a product nobody has looked at. */
  if (blank) out.stockSource = "unknown";
  else if (!out.stockSource) out.stockSource = "manual";
  // Per-lane baselines: drop blanks rather than storing "" under a lane key.
  if (out.leadTimes && typeof out.leadTimes === "object") {
    const lanes = {};
    for (const [k, v] of Object.entries(out.leadTimes)) {
      const b = posInt(v?.baseline, null);
      if (b != null) lanes[k] = { ...v, baseline: b };
    }
    out.leadTimes = Object.keys(lanes).length ? lanes : undefined;
  }
  const nx = posInt(out.nextLeadTime, null);
  if (nx == null) delete out.nextLeadTime; else out.nextLeadTime = nx;
  return out;
}

/** Every params map from storage, repaired in one pass. */
export function sanitizeAllParams(map) {
  const out = {};
  for (const [id, p] of Object.entries(map || {})) out[id] = sanitizeParams(p);
  return out;
}

export function formatDate(daysFromToday) {
  if (daysFromToday < 0) return "Overdue";
  const d = new Date(todayMs());
  d.setUTCDate(d.getUTCDate() + daysFromToday);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
}

export function isoToDisplay(iso) {
  if (!iso) return "-";
  const d = new Date(iso + "T00:00:00Z");
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
}

/* ─── ONE DEFINITION OF A SKU'S STATE ────────────────────────────────────────
 *
 * The scorecard's `sc_status` (main.py) is the definition. This mirrors it exactly
 * — same bands, same precedence, same five names — so the fleet card renders the
 * scorecard's verdict rather than reaching its own, and the two tabs cannot
 * disagree about a product.
 *
 * They used to. The fleet bucketed on TIMING (days until reorder, against a
 * fleet-wide 21) while the scorecard bucketed on STOCK POSITION (coverage against
 * that product's own lead time + coverage window). Both called the result
 * "healthy", so one product could be healthy on one tab and overstocked on the
 * other and neither was wrong.
 *
 * Nothing here is compared against a fleet-wide constant. The bands come from the
 * product's own lead time (see `reorderBands`), the coverage ceiling from its own
 * coverage target. That is why the labels below carry no day counts: there is no
 * single number that would be true for every product, and printing one would be
 * the old lie in a new place.
 */
export const NEW_SKU_DAYS = 28;                      // YOUNG_DAYS in main.py

/* The body POSTed to /api/scorecard, built in ONE place.
 *
 * There were three copies — App.jsx (feeding the fleet card), ScorecardTab and
 * AiDrawer — and they asked the endpoint different questions. App sent the RESOLVED
 * planning lead time (measured P80, else the lane baseline, else the supplier's);
 * the other two sent the raw `params.leadTime` field, which ignores every one of
 * those. AiDrawer additionally dropped `fees`, so its margins were wrong too.
 *
 * Same endpoint, different inputs, different statuses back — which is exactly how
 * the fleet and the scorecard ended up reporting different counts for the same
 * catalogue. It got worse once the reorder bands became fractions of the lead time,
 * because then a lead-time difference moved the band boundaries AND the `lt + cov`
 * overstock ceiling at once.
 *
 * `planningLeadTime` is the number the tool actually plans with; the raw field is
 * only its last fallback. Anything scoring a product has to use the former.
 */
export function buildScorecardBody(skuList, skuParams, openPOs, suppliers, trailingDays = 30) {
  const skus = {};
  (skuList || []).forEach((s) => {
    const p = sanitizeParams({ ...DEFAULT_PARAMS, ...(skuParams?.[s.id] || {}) });
    const po = openPOs?.[s.id];
    skus[s.id] = {
      stock: p.stock,
      /* WHERE that number came from. Without it the server had to guess, and its guess
         was "assume a default" — which is how an uncounted product got a confident
         overdue-reorder badge. With it, the server suppresses every figure that is
         stock-divided-by-demand rather than publishing arithmetic on a placeholder. */
      stockSource: p.stockSource || "unknown",
      /* The lead time the FORECASTS plan on, one-off slow shipment included. This used to
         ignore the one-off, so with a 45-day next shipment on a 14-day line the product
         page said "reorder overdue" while this badge said "Reorder due". (The backtest's
         signature in App.jsx ignores the one-off on purpose: it measures the normal lane.) */
      leadTime: planningLeadTime(s.id, p, suppliers).days,
      coverage: p.coverage,
      unitCost: p.unitCost ?? null,
      fees: p.fees ?? 0,
      hasOpenPo: !!po,
      // The PO's SIZE and ARRIVAL, not just that one exists. Without these the
      // scorecard scored a product with a container inbound exactly as it scored one
      // with nothing coming, so its coverage bar contradicted the stockout date on the
      // detail panel, which has always counted the PO.
      unitsOnOrder: po ? (Number(po.qty) || 0) : 0,
      onOrderEtaDays: po ? poEtaDays(po) : null,
    };
  });
  return { trailingDays, skus };
}

/** Reorder bands for ONE product, scaled to its lead time. Mirrors sc_bands().
 *  A flat rule called ten-days-out routine for both a 3-day air line and a 60-day
 *  sea crossing; on sea that is nearly unrecoverable. At the default 14-day lead
 *  time these land on 7 and 21 — what a flat rule would have picked anyway. */
export function reorderBands(leadTimeDays) {
  const lt = Number(leadTimeDays) > 0 ? Number(leadTimeDays) : DEFAULT_PARAMS.leadTime;
  return { risk: Math.max(1, Math.round(lt / 2)), due: Math.max(2, Math.round(lt * 1.5)) };
}

/* The five scorecard statuses, plus `unrated` for products the scorecard hasn't
 * classified. Nothing else is a bucket.
 *
 * "on order" and "new" used to be buckets here, and both quietly stole from the
 * five: a Healthy product with a PO left the healthy count and a young one became
 * "new", so the fleet's healthy total could sit either side of the scorecard's and
 * be wrong in both directions at once. They are overlays now — a product keeps its
 * status and is separately flagged (`onOrder`, `isNew` on the returned object).
 */
/* `label` is the fleet bar's lower-case form, `title` the scorecard's sentence case,
 * `help` the hover definition. All three live here so the two tabs cannot describe
 * the same status differently — the failure mode this area kept falling into.
 *
 * Every definition states its band in terms of the product's OWN lead time, because
 * that is what the thresholds are made of. Quoting a flat "7 days" would be true for
 * a 14-day line and wrong for every other. */
export const SKU_STATES = {
  stockout: {
    label: "stockout risk", title: "Stockout risk", badge: "AT RISK", urgency: "critical",
    help: "Overdue, or too close to the reorder point for an order today to beat the lead time. The window is half this product's lead time: 7 days on a 14-day line, 30 on a 60-day sea crossing. Order now.",
  },
  reorder: {
    label: "reorder due soon", title: "Reorder due soon", badge: "REORDER", urgency: "high",
    help: "The reorder point is between half and 1.5 lead times away. There is still slack, so order as routine, not urgently.",
  },
  overstock: {
    label: "overstocked", title: "Overstocked", badge: "OVER", urgency: "low",
    help: "More stock than this product's target cover (its lead time plus its coverage window). At 70 days of stock, a 90-day target is fine but a 30-day target is overstocked. Reduce future orders.",
  },
  dead: {
    label: "dead stock", title: "Dead stock", badge: "DEAD", urgency: "low",
    help: "No sales for several times its usual gap between sales, or under 5% of on-hand sold in the trailing window while holding over 3x its target cover. Consider a markdown, promotion, or discontinuing it. New products are never marked dead stock.",
  },
  healthy: {
    label: "healthy", title: "Healthy", badge: null, urgency: "low",
    help: "Stock is inside this product's target cover and the reorder point is far enough out to be routine. Nothing to do.",
  },
  uncounted: {
    label: "stock not counted", title: "Stock not counted", badge: "COUNT", urgency: "none",
    help: "No stock count yet, and your store doesn't report one. Cover, stockout date and order quantity stay hidden until you enter a count on the product page.",
  },
  unrated: {
    label: "not rated", title: "Not rated", badge: null, urgency: "none",
    help: "No fitted forecast yet, so this product is unmeasured, not unhealthy. This usually resolves once it has enough sales history.",
  },
};

/** Overlay labels — shown alongside a status, never instead of one. */
export const SKU_OVERLAYS = { onOrder: "on order", new: "new" };

export const SC_STATUS_KEY = {
  "Stockout risk": "stockout", "Dead stock": "dead", "Overstocked": "overstock",
  "Reorder due": "reorder", "Healthy": "healthy", "Stock not counted": "uncounted",
};

/** Backend status string -> the shared label/title/help entry, so the scorecard's
 *  chips and the fleet's legend are never worded differently. */
export function statusInfo(scStatus) {
  return SKU_STATES[SC_STATUS_KEY[scStatus]] || SKU_STATES.unrated;
}

/* Precedence mirrors sc_status line for line.
 *
 * `stockScored` reports whether the stock-position axis was actually evaluated.
 * Without a scorecard row we can place a product on the reorder timeline but know
 * nothing about dead/overstocked, and callers must not render "0 dead stock" off a
 * check that never ran.
 */
export function skuState(fc = {}, scoreRow = null, hasOpenPO = false, leadTimeDays = null) {
  const d = fc.daysUntilReorder ?? scoreRow?.daysUntilReorder ?? null;
  const stockScored = !!scoreRow?.status;
  const lt = leadTimeDays ?? scoreRow?.leadTime ?? null;
  const { risk, due } = reorderBands(lt);
  const key = (() => {
    // The scorecard's word, verbatim — it saw this product's real coverage and
    // sell-through, which the forecast object doesn't carry.
    if (stockScored) return SC_STATUS_KEY[scoreRow.status] || "unrated";
    /* No scorecard row. /api/skus returns every product; /api/scorecard skips any
       without a fitted forecast, so this is a permanent state for some of them, not
       just a loading blip. It used to fall through to "healthy", which invented a
       verdict nothing had reached and put the fleet's healthy count above the
       scorecard's. Timing alone can still establish URGENCY — worth surfacing, and
       it errs toward showing work rather than hiding it — but never "healthy",
       which is a claim about stock position we have not measured. */
    if (!hasOpenPO && d != null && d < risk) return "stockout";
    if (!hasOpenPO && d != null && d <= due) return "reorder";
    return "unrated";
  })();
  return { key, days: d, stockScored, bands: { risk, due },
           // Overlays: true alongside the status, never replacing it.
           isNew: !!(fc.tooNew || fc.young), onOrder: !!hasOpenPO,
           ...SKU_STATES[key] };
}

export function urgencyLevel(daysUntilReorder, hasOpenPO, leadTimeDays = null) {
  if (hasOpenPO)                return "on_order";
  if (daysUntilReorder == null) return "none";
  return skuState({ daysUntilReorder }, null, false, leadTimeDays).urgency;
}

/* Three grades, ordinal, in plain language. The server no longer emits CONFLICTING — it
   covered three unrelated causes, told a reader nothing on its own, and was over a third
   of the possible inputs. Its causes moved to `statusReason` below, on whichever grade
   each actually deserves. The entry is kept only so a stale cached payload still renders. */
export const STATUS_CONFIG = {
  FORECAST_ELIGIBLE: { dot: "bg-emerald-400", label: "Ready to plan"  },
  LOW_CONFIDENCE:    { dot: "bg-amber-400",   label: "Directional"    },
  INSUFFICIENT:      { dot: "bg-rose-400",    label: "Not enough data" },
  CONFLICTING:       { dot: "bg-orange-400",  label: "Directional"    },  // deprecated
};

/** Why a product got its grade. Short enough to sit next to the grade as a chip. */
export const REASON_LABELS = {
  established:         "measured",
  partial_history:     "short history",
  short_history:       "needs more days",
  few_selling_days:    "too few sale days",
  low_volume:          "too few units",
  new_product:         "new product",
  intermittent_demand: "sells in bursts",
  dormant:             "no recent sales",
};

/** How predictable the demand is, independent of how much history exists. */
export const PREDICTABILITY_LABELS = {
  steady: "steady", variable: "variable", occasional: "occasional",
  dormant: "dormant", unknown: "",
};

// Urgency styles — dark mode only (used in sidebar/fleet where lm is passed)
export const URGENCY_STYLES = {
  critical: { row: "bg-red-950/40 border-red-900/50",       dot: "bg-red-400 animate-pulse",    text: "text-red-400"    },
  high:     { row: "bg-amber-950/30 border-amber-900/40",   dot: "bg-amber-400 animate-pulse",  text: "text-amber-400"  },
  medium:   { row: "bg-yellow-950/20 border-yellow-900/30", dot: "bg-yellow-400",               text: "text-yellow-400" },
  low:      { row: "bg-slate-900/50 border-white/10",      dot: "bg-slate-500",                text: "text-slate-400"  },
  on_order: { row: "bg-violet-950/20 border-violet-900/30",       dot: "bg-violet-400",                  text: "text-violet-400"    },
  none:     { row: "bg-slate-900/30 border-white/10",   dot: "bg-slate-600",                text: "text-slate-500"  },
};
export const URGENCY_STYLES_LM = {
  critical: { row: "bg-red-50 border-red-300",       dot: "bg-red-500 animate-pulse",    text: "text-red-600"    },
  high:     { row: "bg-amber-50 border-amber-300",   dot: "bg-amber-500 animate-pulse",  text: "text-amber-600"  },
  medium:   { row: "bg-yellow-50 border-yellow-300", dot: "bg-yellow-500",               text: "text-yellow-700" },
  low:      { row: "bg-slate-50 border-slate-300",   dot: "bg-slate-400",                text: "text-slate-600"  },
  on_order: { row: "bg-violet-50 border-violet-300",       dot: "bg-violet-500",                  text: "text-violet-700"    },
  none:     { row: "bg-white border-slate-200",      dot: "bg-slate-300",                text: "text-slate-500"  },
};

export function makeFolderId() { return "folder_" + Math.random().toString(36).slice(2, 9); }
export function makeSupId()  { return "sup_" + Math.random().toString(36).slice(2, 9); }
export function makeOrdId()  { return "ord_" + Math.random().toString(36).slice(2, 9); }

// ─── STAT HELPERS ────────────────────────────
export function pctile(arr, p) {
  if (!arr.length) return 0;
  const sorted = [...arr].sort((a, b) => a - b);
  const idx = (p / 100) * (sorted.length - 1);
  const lo = Math.floor(idx), hi = Math.ceil(idx);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}

// ─── SUPPLIER HELPERS ─────────────────────────
export function computeSupplierStats(supplier) {
  const completed = (supplier.orders || []).filter(o => o.receivedDate && o.orderedDate);
  if (!completed.length) return { avg: null, p80: null, n: 0, onTimeRate: null, avgVariance: null };
  const leadTimes = completed
    .map(o => Math.round((new Date(o.receivedDate) - new Date(o.orderedDate)) / 86400000))
    .filter(d => d >= 0);
  if (!leadTimes.length) return { avg: null, p80: null, n: 0, onTimeRate: null, avgVariance: null };
  const avg = Math.round(leadTimes.reduce((a, b) => a + b, 0) / leadTimes.length);
  const p80 = Math.round(pctile(leadTimes, 80));
  // Variance vs expected date
  const withExpected = completed.filter(o => o.expectedDate);
  let onTimeRate = null, avgVariance = null;
  if (withExpected.length > 0) {
    const variances = withExpected.map(o =>
      Math.round((new Date(o.receivedDate) - new Date(o.expectedDate)) / 86400000)
    );
    avgVariance = Math.round(variances.reduce((a, b) => a + b, 0) / variances.length * 10) / 10;
    onTimeRate  = Math.round(variances.filter(v => v <= 0).length / variances.length * 100);
  }
  return { avg, p80, n: leadTimes.length, onTimeRate, avgVariance };
}

export function getEffectiveLeadTime(skuId, manualLeadTime, suppliers) {
  if (!suppliers) return { days: manualLeadTime, source: "manual", supplierName: null };
  const entry = Object.values(suppliers).find(s => (s.skuIds || []).includes(skuId));
  if (!entry) return { days: manualLeadTime, source: "manual", supplierName: null };
  const stats = computeSupplierStats(entry);
  if (stats.p80 !== null && stats.n >= 2) {
    return { days: stats.p80, source: "supplier", supplierName: entry.name, avg: stats.avg, p80: stats.p80, n: stats.n };
  }
  return { days: manualLeadTime, source: "manual", supplierName: entry.name };
}

// Maps forecast reliability (a proxy for demand volatility) to CI strategy
/* Every product now uses the same 80% forecast band.
 *
 * This used to widen or narrow the CONFIDENCE LEVEL by demand volatility: steady
 * products were shown a 70% interval, volatile ones 90%. That double-counted, because
 * the band's WIDTH already varies with volatility through sigma, and it left two
 * products' bands incomparable since they were drawn to different promises. It also read
 * backwards: a 70% band looks tighter but promises less (reality lands outside it about
 * 3 weeks in 10) than a 90% one.
 *
 * Volatility still matters and is still shown; it just describes the product now instead
 * of quietly changing what the shaded region means. Kept as a function rather than
 * deleted so every call site keeps working and none of them re-fetch on a mismatch. */
export function autoStrategy() { return "balanced"; }

// Shipping methods have genuinely different lead times — averaging a 42-day sea
// container with a 7-day air freight gives a number that is wrong for both. Tagging is
// optional: untagged deliveries behave exactly as before.
export const FREIGHT_MODES = { ground: "Ground", sea: "Sea", air: "Air" };

export function computeSkuLeadTimeStats(skuId, suppliers, since = null, mode = null) {
  // `since` restricts to deliveries received AFTER a declared lead-time change. Old
  // deliveries are never deleted — they're just excluded from the forward-looking
  // estimate, so removing the changepoint brings the full history straight back.
  const cutoff = since ? new Date(since) : null;
  const orders = Object.values(suppliers || {}).flatMap(sup =>
    (sup.orders || []).filter(o => o.skuId === skuId && o.receivedDate && o.orderedDate &&
      (!cutoff || new Date(o.receivedDate) >= cutoff) &&
      (!mode || (o.freightMode || null) === mode))
  );
  if (!orders.length) return { avg: null, p80: null, n: 0 };
  const leadTimes = orders
    .map(o => Math.round((new Date(o.receivedDate) - new Date(o.orderedDate)) / 86400000))
    .filter(d => d >= 0);
  if (!leadTimes.length) return { avg: null, p80: null, n: 0 };
  const avg = Math.round(leadTimes.reduce((a, b) => a + b, 0) / leadTimes.length);
  const p80 = Math.round(pctile(leadTimes, 80));
  return { avg, p80, n: leadTimes.length };
}


/**
 * Days from today until an open PO is expected to land, or null if no date is recorded.
 *
 * The forecast needs this because stock on the way only helps FROM the day it arrives —
 * counting it as available today makes a shipment three months out look like stock in
 * the warehouse and quietly suppresses a reorder you actually need.
 */
export function poEtaDays(po) {
  if (!po || !po.delivery) return null;
  const eta = new Date(po.delivery);
  if (Number.isNaN(eta.getTime())) return null;
  /* Both sides on the SAME day grid, and that grid is UTC — "2026-08-30" parses as UTC
     midnight, and todayStr() is a UTC date too. The old code parsed the ETA as UTC and
     then floored it with local setHours(), which for anyone west of Greenwich lands on
     the previous local day: every ETA came back a day short, so in-transit stock started
     counting toward cover a day before it could possibly arrive. */
  const etaUtc = Date.UTC(eta.getUTCFullYear(), eta.getUTCMonth(), eta.getUTCDate());
  // Today on the store's clock, on the same UTC-midnight grid.
  return Math.max(0, Math.round((etaUtc - todayMs()) / 86400000));
}


/**
 * The lead time used for PLANNING — i.e. "if I order today, when does it land?"
 *
 * This is a prediction about the NEXT order, so it should come from how long this
 * supplier has actually taken, not from a number typed once and forgotten. It uses the
 * P80 of completed deliveries: 4 in 5 have arrived within it. The average would be wrong
 * here — plan to the average and roughly half your orders arrive after you needed them,
 * and the safety buffer can't rescue you because that buffer sizes DEMAND variability,
 * not supplier lateness.
 *
 * Not to be confused with an in-transit PO's expected date, which is a fact about one
 * shipment already placed (see poEtaDays). A promise about one delivery is not a
 * prediction for the next.
 *
 * Manual always wins: set `leadTimeMode: "manual"` and your number is used verbatim.
 */
export const LEAD_TIME_MIN_DELIVERIES = 3;   // below this, delivery history is too thin

/** Lead-time stats across EVERY product from this SKU's supplier, since an optional
 *  changepoint. A product with one delivery of its own is still buying from a supplier
 *  you may have twenty deliveries from — that's real evidence about how long they take. */
export function computeSupplierLeadTimeStats(skuId, suppliers, since = null, mode = null) {
  const entry = Object.values(suppliers || {}).find(s => (s.skuIds || []).includes(skuId));
  if (!entry) return { avg: null, p80: null, n: 0, supplierName: null };
  const cutoff = since ? new Date(since) : null;
  const leadTimes = (entry.orders || [])
    .filter(o => o.receivedDate && o.orderedDate && (!cutoff || new Date(o.receivedDate) >= cutoff) &&
      (!mode || (o.freightMode || null) === mode))
    .map(o => Math.round((new Date(o.receivedDate) - new Date(o.orderedDate)) / 86400000))
    .filter(d => d >= 0);
  if (!leadTimes.length) return { avg: null, p80: null, n: 0, supplierName: entry.name };
  return {
    avg: Math.round(leadTimes.reduce((a, b) => a + b, 0) / leadTimes.length),
    p80: Math.round(pctile(leadTimes, 80)),
    n: leadTimes.length, supplierName: entry.name,
  };
}

/** The baseline for one lane. Lanes are created lazily: a product that only ever ships
 *  sea has one baseline, not three, and the unused lanes read blank rather than carrying
 *  a number nobody chose. Falls back to the product-level baseline for the default lane. */
/* ── Expecting the next order to be slower ──────────────────────────────────────────
 *
 * A one-off delay is a FORECAST about a shipment that hasn't been placed yet, so it has
 * nowhere permanent to live until it has. The lifecycle:
 *
 *   1. You record it       → floats on the product. Plans reorder earlier, raises the
 *                            buffer, and carries the reason you gave.
 *   2. You raise the PO    → it attaches to that order: the reason becomes the order's
 *                            note, and the floating expectation is consumed.
 *   3. The order lands     → it is an ordinary delivery. It counts in the lead-time
 *                            history and in the supplier's averages like any other,
 *                            because it really did take that long.
 *   4. You discard it      → the floating note goes with it. Nothing was written.
 *
 * Step 3 is the one worth being explicit about: the flag never excludes anything from
 * history. Excluding "explained" delays is how a P80 quietly drifts optimistic.
 */
export function readSlowShipment(params) {
  const s = params?.slowShipment;
  if (s && Number(s.days) > 0) {
    // `delta`/`basis` are carried when the adjustment was entered as a change against the
    // lane's normal wait — which is the only way to express an order arriving EARLY.
    // Expedited freight and a cancelled holiday shutdown are as real as a port strike,
    // and a tool that can only record bad news makes you plan around stock you'll
    // actually have three weeks sooner.
    return { days: Math.round(Number(s.days)), reason: s.reason || "", at: s.at || null,
             delta: Number.isFinite(Number(s.delta)) ? Math.round(Number(s.delta)) : null,
             basis: Number(s.basis) > 0 ? Math.round(Number(s.basis)) : null };
  }
  // Older saves stored a bare number. Read it rather than losing the expectation.
  const legacy = Number(params?.nextLeadTime);
  return Number.isFinite(legacy) && legacy > 0
    ? { days: Math.round(legacy), reason: "", at: null, delta: null, basis: null } : null;
}

export function slowShipmentNote(slow) {
  if (!slow) return "";
  const why = (slow.reason || "").trim();
  const dir = slow.delta == null ? "One-off" : slow.delta < 0 ? "Arriving early" : "Delayed";
  const head = `${dir} (${slow.days}d${slow.delta ? `, ${slow.delta > 0 ? "+" : ""}${slow.delta}d vs usual` : ""})`;
  return why ? `${head}: ${why}` : head;
}

/* ── Lead-time changes stack ─────────────────────────────────────────────────────────
 *
 * A single `leadTimeChangedAt` could only ever describe the most recent change, so the
 * second one silently erased the first — and with it the answer to "when did this become
 * a 44-day lane?". They're kept as a list now, oldest first, each carrying what it set
 * AND what it replaced.
 *
 * Only the LAST one can be undone, and that's deliberate: undoing an older change while a
 * newer one stands would leave the baselines describing an arrangement that never existed.
 * Undo the newest, and the one before it becomes undoable in turn.
 *
 * The measurement cutoff is always the latest change. Everything before it is set aside,
 * never deleted — remove every change and the full history reappears untouched.
 */
export function readLeadTimeChanges(params) {
  const raw = Array.isArray(params?.leadTimeChanges) ? params.leadTimeChanges : null;
  if (raw && raw.length) {
    return raw.filter(c => c && c.at).slice().sort((a, b) => String(a.at).localeCompare(String(b.at)));
  }
  // Older saves recorded a single date and no numbers. Read it as one entry rather than
  // losing the changepoint — the lanes are simply unknown for it.
  const legacy = params?.leadTimeChangedAt;
  return legacy ? [{ at: legacy, lanes: {}, prev: {}, legacy: true }] : [];
}

/** The measurement cutoff: deliveries before this don't count toward the current P80. */
export function latestChangeAt(params) {
  const list = readLeadTimeChanges(params);
  return list.length ? list[list.length - 1].at : null;
}

/** Record a change. Returns a params PATCH — the caller decides how to commit it.
 *  `lanes` is { mode: days }; a mode left out is untouched and keeps its number. */
export function applyLeadTimeChange(params, lanes, at = null) {
  const when = at || todayStr();
  const nextLanes = { ...(params?.leadTimes || {}) };
  const set = {}, prev = {};
  for (const [mode, v] of Object.entries(lanes || {})) {
    const days = posInt(v, null);
    if (days == null) continue;
    const before = posInt(params?.leadTimes?.[mode]?.baseline, null);
    if (before === days) continue;                 // nothing actually moved
    prev[mode] = before;                           // null means "had none"
    set[mode] = days;
    nextLanes[mode] = { ...(nextLanes[mode] || {}), baseline: days };
  }
  if (!Object.keys(set).length) return null;       // no-op: don't stack an empty entry
  const prior = readLeadTimeChanges(params);
  const last = prior[prior.length - 1];
  let changes;
  if (last && last.at === when) {
    /* Same day: fixing sea, then realising air moved too, is one change with two lanes —
       not two entries dated identically. `prev` keeps the OLDEST value so undo still
       rolls all the way back to where the day started. */
    changes = [...prior.slice(0, -1), {
      at: when,
      lanes: { ...(last.lanes || {}), ...set },
      prev: { ...prev, ...(last.prev || {}) },
    }];
  } else {
    changes = [...prior, { at: when, lanes: set, prev }];
  }
  return { leadTimes: nextLanes, leadTimeChanges: changes, leadTimeChangedAt: when };
}

/** Undo the most recent change, restoring the baselines it wrote — but only where they
 *  haven't been edited since. A number you typed afterwards is a newer statement than the
 *  one the change made, and undo has no business overruling it. */
export function undoLastLeadTimeChange(params) {
  const list = readLeadTimeChanges(params);
  if (!list.length) return null;
  const last = list[list.length - 1];
  const rest = list.slice(0, -1);
  const lanes = { ...(params?.leadTimes || {}) };
  for (const [mode, wrote] of Object.entries(last.lanes || {})) {
    const now = posInt(params?.leadTimes?.[mode]?.baseline, null);
    if (now !== posInt(wrote, null)) continue;     // edited since — leave it alone
    const before = posInt(last.prev?.[mode], null);
    if (before == null) delete lanes[mode];
    else lanes[mode] = { ...(lanes[mode] || {}), baseline: before };
  }
  return {
    leadTimes: Object.keys(lanes).length ? lanes : undefined,
    leadTimeChanges: rest.length ? rest : undefined,
    leadTimeChangedAt: rest.length ? rest[rest.length - 1].at : undefined,
  };
}

/** A human summary of one entry: "Sea 21d → 44d, Air added at 5d". */
export function describeLeadTimeChange(change) {
  const parts = Object.entries(change?.lanes || {}).map(([mode, to]) => {
    const from = change.prev?.[mode];
    const label = FREIGHT_MODES[mode] || mode;
    return from == null ? `${label} set to ${to}d` : `${label} ${from}d → ${to}d`;
  });
  return parts.length ? parts.join(", ") : "Started measuring fresh";
}

export function supplierOf(skuId, suppliers) {
  return Object.values(suppliers || {}).find(s => (s.skuIds || []).includes(skuId)) || null;
}

/* ─── ORDERS FOR PRODUCTS WITH NO SUPPLIER YET ────────────────────────────────
 *
 * They used to be thrown away. An order logged without a supplier lived only in
 * openPOs; when it arrived, the stock went up, the PO was deleted, and both dates
 * went with it — so that product could never learn its own lead time no matter how
 * many orders you logged through it.
 *
 * They now park in a holding record instead. It carries `skuIds: []` deliberately:
 * `supplierOf` looks products up by that list, so an empty one means parking an
 * order never makes a product look assigned to anything. Lead-time maths is
 * unaffected either way — `computeSkuLeadTimeStats` scans every record's orders and
 * filters by skuId, so a parked delivery feeds that product's P80 exactly like any
 * other. Only the supplier LISTS need to skip it, via `namedSuppliers`.
 */
export const UNASSIGNED_SUP_ID   = "__unassigned";
export const UNASSIGNED_SUP_NAME = "Unassigned orders";

/** Suppliers you actually deal with. Use for every list, picker and count — never
 *  for lead-time maths, which has to see the parked orders too. */
export function namedSuppliers(suppliers) {
  return Object.values(suppliers || {}).filter(s => s?.id !== UNASSIGNED_SUP_ID);
}

/** File an order that has no supplier. Returns the next suppliers map. */
export function parkOrder(suppliers, order) {
  const led = suppliers?.[UNASSIGNED_SUP_ID]
    || { id: UNASSIGNED_SUP_ID, name: UNASSIGNED_SUP_NAME, skuIds: [], orders: [] };
  return { ...(suppliers || {}),
           [UNASSIGNED_SUP_ID]: { ...led, orders: [...(led.orders || []), order] } };
}

/** Hand a product's parked orders to the supplier it was just assigned to, whole —
 *  every field travels, including an order still in transit with no arrival date
 *  yet. The holding record is dropped once it's empty so it never lingers. */
export function adoptParkedOrders(suppliers, supId, skuId) {
  const led = suppliers?.[UNASSIGNED_SUP_ID];
  const target = suppliers?.[supId];
  if (!led || !target || supId === UNASSIGNED_SUP_ID) return suppliers;
  const mine = (led.orders || []).filter(o => o.skuId === skuId);
  if (!mine.length) return suppliers;
  const rest = (led.orders || []).filter(o => o.skuId !== skuId);
  const next = { ...suppliers,
    [supId]: { ...target, orders: [...(target.orders || []), ...mine] } };
  if (rest.length) next[UNASSIGNED_SUP_ID] = { ...led, orders: rest };
  else delete next[UNASSIGNED_SUP_ID];
  return next;
}

/* ─── THE OPEN PO AND ITS SUPPLIER ORDER ──────────────────────────────────────
 *
 * A product has one open PO (openPOs[skuId]) and each PO is filed as an order in the
 * supplier history. They are linked by `orderId` on the PO. Before the link, marking ANY
 * open order for a product received cleared its PO, and clearing the PO marked the
 * FIRST open order received, so with two orders on the water one arrival cancelled the
 * other. POs saved before the link are matched by order date and quantity, and only when
 * that picks out exactly one order.
 */
export function findPoOrder(suppliers, skuId, po) {
  if (!po) return null;
  const hits = [];
  for (const sup of Object.values(suppliers || {})) {
    (sup?.orders || []).forEach(o => {
      if (o.skuId !== skuId || o.receivedDate) return;
      if (po.orderId ? o.id === po.orderId
                     : (o.orderedDate === po.ordered && Number(o.qty) === Number(po.qty))) {
        hits.push({ supId: sup.id, order: o });
      }
    });
  }
  if (po.orderId) return hits[0] || null;
  if (hits.length === 1) return hits[0];
  if (hits.length > 1) return null;
  // Legacy PO, nothing matched on date and quantity: fall back only when the product has
  // a single open order, so it can't be the wrong one.
  const open = [];
  for (const sup of Object.values(suppliers || {})) {
    (sup?.orders || []).forEach(o => { if (o.skuId === skuId && !o.receivedDate) open.push({ supId: sup.id, order: o }); });
  }
  return open.length === 1 ? open[0] : null;
}

/** Does the open PO for `skuId` belong to this supplier order? */
export function poIsOrder(openPOs, suppliers, skuId, orderId) {
  const po = openPOs?.[skuId];
  if (!po) return false;
  if (po.orderId) return po.orderId === orderId;
  return findPoOrder(suppliers, skuId, po)?.order?.id === orderId;
}

/** Set one order's fields. Returns the next suppliers map. */
export function updateOrder(suppliers, supId, orderId, patch) {
  const sup = suppliers?.[supId];
  if (!sup) return suppliers;
  return { ...suppliers, [supId]: { ...sup,
    orders: (sup.orders || []).map(o => o.id === orderId ? { ...o, ...patch } : o) } };
}

/* Does a delivery that arrived on `arrivedOn` still need adding to the stock figure?
 * Not when the stock was COUNTED after it arrived: the count already includes it, and
 * adding it again double-counts. Logging an old delivery for the lead-time record used
 * to add its units to today's stock regardless. A delivery arriving today is new stock;
 * with no count on file, one that arrived before today follows `pastDefault`. */
export function arrivalAddsToStock(params, arrivedOn, { pastDefault = false } = {}) {
  const day = String(arrivedOn || "").slice(0, 10);
  if (!day) return false;
  // Arriving today: the arrival is now, after any count, so it adds.
  if (day >= todayStr()) return true;
  const counted = params?.stockCountedAt ? isoToStoreDay(params.stockCountedAt) : null;
  // Counted on a later day: the count already includes it. (Counted the same day is
  // ambiguous; it's treated as before the delivery, as a delivery marked today would be.)
  if (counted && counted > day) return false;
  return counted ? true : pastDefault;
}

/** The store's calendar day an ISO timestamp falls on. */
function isoToStoreDay(iso) {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
    const tz = _storeZone;
    return tz ? d.toLocaleDateString("en-CA", { timeZone: tz }) : d.toLocaleDateString("en-CA");
  } catch { return String(iso).slice(0, 10); }
}

/** A supplier's typed baseline for one lane. Set once, inherited by every product under
 *  that supplier — so a catalogue of 23 SKUs needs one number, not 23. This is a LADDER
 *  LEVEL, never written into the products: change it and every inheriting product moves,
 *  and any product that set its own number keeps it. */
export function supplierLaneBaseline(suppliers, skuId, mode) {
  const sup = supplierOf(skuId, suppliers);
  const v = Number(sup?.leadTimes?.[mode]?.baseline);
  return Number.isFinite(v) && v > 0 ? v : null;
}

export function laneBaseline(params, mode, suppliers = null, skuId = null) {
  const lanes = params?.leadTimes || {};
  const v = Number(lanes?.[mode]?.baseline);
  if (Number.isFinite(v) && v > 0) return v;
  /* Inherit the supplier's typed baseline. A `noSupplierFallback` opt-out used to gate
     this, but nothing in the app ever set it — four places read the flag, none wrote it,
     so the branch was unreachable and these comments described an escape hatch that
     didn't exist. It had nothing left to do either: a product that doesn't want its
     supplier's number types its own, and the check above returns that first. */
  if (suppliers && skuId) {
    const sup = supplierLaneBaseline(suppliers, skuId, mode);
    if (sup != null) return sup;
  }
  // The default lane inherits the product baseline — that's the number that already exists
  // for every product, so single-lane users never type anything new.
  const dflt = params?.freightMode || null;
  if (!mode || mode === dflt) {
    const b = Number(params?.leadTime);
    if (Number.isFinite(b) && b > 0) return b;
  }
  return null;
}

/** Every lane this product has evidence or an opinion about, for the per-product table.
 *  A lane appears once it has a baseline OR a single tagged shipment — never before. */
export function leadTimeTracks(skuId, params, suppliers) {
  const changedAt = latestChangeAt(params);
  const dflt = params?.freightMode || null;
  return Object.keys(FREIGHT_MODES).map(mode => {
    const st = computeSkuLeadTimeStats(skuId, suppliers, changedAt, mode);
    const own = laneBaseline(params, mode);           // this product's own, if any
    const inherited = supplierLaneBaseline(suppliers, skuId, mode);
    const base = laneBaseline(params, mode, suppliers, skuId);
    const measured = st.n >= LEAD_TIME_MIN_DELIVERIES ? st.p80 : null;
    return {
      mode, label: FREIGHT_MODES[mode], baseline: base,
      // true when the number shown came from the supplier rather than this product
      inherited: base != null && Number(params?.leadTimes?.[mode]?.baseline) !== base && inherited === base,
      supplierBaseline: inherited,
      n: st.n, avg: st.avg, p80: measured,
      // what planning would actually use for this lane, in isolation
      plan: measured != null ? measured : base,
      isDefault: mode === dflt,
      used: st.n > 0 || base != null,
      needed: measured != null ? 0 : Math.max(0, LEAD_TIME_MIN_DELIVERIES - st.n),
    };
  });
}

/**
 * Resolve the planning lead time as Supplier -> Product -> Shipping method.
 *
 * Lanes never mix. A sea shipment tells you nothing about how long air takes, so when a
 * lane is too thin to measure we fall back to THAT LANE's baseline rather than blending
 * in deliveries from a different lane — blending would hand an air order a sea number.
 *
 * @param {object} opts
 * @param {boolean} opts.ignoreOneOff  Skip the next-order expectation. Used when picking
 *   the lead time to BACKTEST: a temporary blip shouldn't trigger a full re-test at a
 *   window you'll abandon next week, and protection levels should reflect normal running.
 * @param {string} opts.mode  Plan for a specific lane instead of the product's default —
 *   e.g. costing an air order for a product that normally ships sea.
 */
export function planningLeadTime(skuId, params, suppliers, opts = {}) {
  // Levels, most-specific first. Nothing here ever deletes data: each level is a filter or
  // an override, so any of them can be undone and the level below reappears.
  //
  //   1. next-order  - "this ONE shipment will be slow". Temporary by construction.
  //   2. manual      - "ignore my delivery history, I know better". A standing override.
  //   3. measured    - P80 of THIS LANE's deliveries since the last declared change.
  //   4. baseline    - this lane's baseline: the product's own, else the supplier's.
  const changedAt = latestChangeAt(params);
  // No declared method: fall to the lane this product actually ships most, so a backtest
  // (and the plan) reads that lane's history instead of pooling incompatible ones.
  const mode = opts.mode || params?.freightMode || resolveLane(skuId, params, suppliers);
  const laneBase = laneBaseline(params, mode, suppliers, skuId);
  /* Whatever the params contain, a planning lead time must be a usable positive number.
     Blank fields, NaN and 0 all reach here as soon as someone clears an input, and every
     consumer downstream (the forecast endpoint, the buffer math, the backtest combos)
     treats a non-positive lead time as invalid rather than as "none". Clamp once, here,
     so no caller has to remember. */
  const _sane = (v) => (Number.isFinite(Number(v)) && Number(v) > 0 ? Number(v) : null);
  // A number typed for THIS product is a deliberate statement about it. Sibling products'
  // deliveries are evidence about them — useful when we have nothing, but they must not
  // silently overrule someone who already said what this lane takes.
  const ownBaseline = Number(params?.leadTimes?.[mode]?.baseline) > 0
    || (params?.leadTimeMode === "manual");
  const baseline = _sane(laneBase)
    ?? _sane(params?.leadTime)
    ?? Number(DEFAULT_PARAMS.leadTime);

  const slow = readSlowShipment(params);
  if (!opts.ignoreOneOff && slow) {
    return { days: slow.days, source: "next-order", n: 0, baseline, changedAt, mode,
             reason: slow.reason, slowAt: slow.at };
  }
  if (params?.leadTimeMode === "manual") {
    return { days: baseline, source: "manual", n: 0, baseline, changedAt, mode };
  }
  // Measured - but only from deliveries in this lane, received since the current
  // arrangement began. Declaring a change doesn't wipe anything; it moves this window
  // forward. Undo it and the older deliveries qualify again and the previous P80 returns.
  const st = computeSkuLeadTimeStats(skuId, suppliers, changedAt, mode);
  const enough = x => x && x.p80 != null && x.n >= LEAD_TIME_MIN_DELIVERIES;
  if (enough(st)) {
    return { days: st.p80, source: "measured", n: st.n, avg: st.avg, p80: st.p80,
             baseline, changedAt, mode, modeMatched: Boolean(mode) };
  }
  /* No borrowing of other products' MEASURED deliveries. A sibling's shipments are
     evidence about that sibling — pooling them was worth it only when a supplier had
     nothing else to offer, and supplier-level lane BASELINES now fill that gap without
     the risk. The risk was real: a made-to-order SKU in the same pool dragged every
     sibling's planned lead time from 44 days to 90.

     So the ladder is: this product's own deliveries, then the baseline for its lane
     (its own, or the one inherited from its supplier). Nothing else can move it. */
  const all = computeSkuLeadTimeStats(skuId, suppliers, null, mode);
  return { days: baseline, source: "baseline", n: st?.n || 0, mode,
           laneBaselineSet: laneBase != null,
           needed: Math.max(0, LEAD_TIME_MIN_DELIVERIES - (st?.n || 0)),
           supersededN: changedAt ? Math.max(0, (all?.n || 0) - (st?.n || 0)) : 0,
           priorP80: changedAt && all?.n >= LEAD_TIME_MIN_DELIVERIES ? all.p80 : null,
           baseline, changedAt };
}

/**
 * ONE place that decides how a lead time is described, because four screens were each
 * inventing their own sentence and getting it wrong in different ways.
 *
 * The word that caused the trouble was "usually". A P80 is not a usual wait — it is the
 * figure 4 in 5 deliveries beat, deliberately pessimistic, and calling it usual invites
 * someone to plan to it as a typical case. A baseline isn't a usual wait either: nothing
 * has been measured, so it's an estimate. And when two deliveries have landed and both
 * disagree with the estimate, saying "usually 21d" while sitting on 27d of evidence is
 * simply false.
 *
 *   label   short, fits in a hint line
 *   detail  the sentence under it
 *   estimate  true when nothing has been measured — callers can style it as provisional
 *   disagrees true when logged deliveries contradict the number being shown
 */
export function leadTimeBasis(skuId, params, suppliers, opts = {}) {
  const plan = planningLeadTime(skuId, params, suppliers, opts);
  const mode = plan.mode || null;
  const lane = mode ? FREIGHT_MODES[mode] : "All methods";
  const st = computeSkuLeadTimeStats(skuId, suppliers, latestChangeAt(params), mode);
  const out = { days: plan.days, source: plan.source, mode, lane, n: st.n, avg: st.avg,
                label: "", detail: "", estimate: false, disagrees: false };

  if (plan.source === "next-order") {
    const normal = planningLeadTime(skuId, params, suppliers, { ...opts, ignoreOneOff: true }).days;
    const delta = plan.days - normal;
    out.normal = normal;
    out.delta = delta;
    out.label = `${plan.days}d (one-off)`;
    out.detail = delta === 0
      ? `Flagged for the next order${plan.reason ? `: ${plan.reason}` : ""}.`
      : `${Math.abs(delta)}d ${delta > 0 ? "longer" : "shorter"} than this lane's ${normal}d${plan.reason ? `: ${plan.reason}` : ""}.`;
    return out;
  }
  if (plan.source === "manual") {
    out.label = `${plan.days}d (set by you)`;
    out.detail = `Used instead of ${lane.toLowerCase()} delivery history.`;
    return out;
  }
  if (plan.source === "measured") {
    out.label = `${plan.days}d (${lane} P80)`;
    out.detail = `80% of ${st.n} ${lane.toLowerCase()} ${st.n === 1 ? "delivery" : "deliveries"} arrived within ${plan.days}d; the average was ${st.avg}d.`;
    return out;
  }

  // Baseline: an estimate, and it must not pretend otherwise.
  const ownSet = mode ? Number(params?.leadTimes?.[mode]?.baseline) > 0 : false;
  const supBase = mode ? supplierLaneBaseline(suppliers, skuId, mode) : null;
  const fromSupplier = !ownSet && supBase != null && supBase === plan.days;
  const supName = supplierOf(skuId, suppliers)?.name || "the supplier";
  out.estimate = true;
  out.label = `${plan.days}d (${lane} estimate)`;
  out.detail = fromSupplier
    ? `${supName}'s figure for ${lane.toLowerCase()}. Nothing measured for this product yet.`
    : `Your figure for ${lane.toLowerCase()}. Nothing measured yet.`;
  if (st.n > 0 && st.avg != null) {
    // Below LEAD_TIME_MIN_DELIVERIES a P80 isn't trustworthy enough to plan from, but the
    // deliveries are still evidence and hiding them is how an estimate stays wrong.
    out.disagrees = Math.abs(st.avg - plan.days) >= Math.max(2, plan.days * 0.15);
    const need = Math.max(0, LEAD_TIME_MIN_DELIVERIES - st.n);
    out.detail = `${st.n} ${st.n === 1 ? "delivery has" : "deliveries have"} averaged ${st.avg}d`
      + (out.disagrees ? `, but the estimate says ${plan.days}d. ` : `. `)
      + (need ? `${need} more and this switches to measured.` : "");
  }
  return out;
}

/**
 * Decide what a change to the per-SKU (leadTime/coverage) fingerprints requires.
 * Lives here rather than inline in an effect so the rules are testable.
 *
 *   prev/now : { skuId: "lead/coverage" }.  A SKU absent from `now` isn't ready yet.
 *
 * A SKU appearing for the first time (its lead time was just settled) needs testing just
 * as much as one whose value moved — both are "this product's window is untested".
 */
export function planRecompute(prev, now) {
  if (!prev) return { kind: "full", skus: [], reason: "first-run" };
  const keys = Object.keys(now);
  const changed = keys.filter(k => prev[k] !== undefined && prev[k] !== now[k]);
  const added   = keys.filter(k => prev[k] === undefined);
  const touched = [...changed, ...added];
  if (!touched.length) return { kind: "none", skus: [], reason: "no-change" };
  // Everything moved at once — a global input (holding rate, a supplier baseline every
  // product inherits). Refitting one at a time would be slower than one clean sweep.
  if (touched.length === keys.length && keys.length > 1) {
    return { kind: "full", skus: touched, reason: "all-changed" };
  }
  return { kind: "partial", skus: touched, reason: added.length ? "newly-ready" : "params-changed" };
}

/** Has this product's lead time actually been established, or is it still the untouched
 *  default? Backtesting against a placeholder produces confident-looking numbers built on
 *  a guess — the same reason the engine skips products with no unit cost. Confirmed means:
 *  measured from deliveries, borrowed from a supplier's measured record, or a baseline
 *  someone deliberately set (on the product or on its supplier). */
export function leadTimeConfirmed(skuId, params, suppliers) {
  const plan = planningLeadTime(skuId, params, suppliers, { ignoreOneOff: true });
  if (plan.source === "measured") return true;
  if (plan.source === "manual") return true;             // explicitly typed
  const mode = resolveLane(skuId, params, suppliers);
  if (mode && Number(params?.leadTimes?.[mode]?.baseline) > 0) return true;
  if (mode && supplierLaneBaseline(suppliers, skuId, mode) != null) return true;
  return false;
}

/** Which lane to plan/backtest from when the product hasn't declared one. Deterministic:
 *  the declared method, else the lane with the most deliveries, else null (pool everything).
 *  Ties break on FREIGHT_MODES order so the answer never depends on object iteration. */
export function resolveLane(skuId, params, suppliers) {
  if (params?.freightMode) return params.freightMode;
  let best = null, bestN = 0;
  for (const mode of Object.keys(FREIGHT_MODES)) {
    const n = computeSkuLeadTimeStats(skuId, suppliers, latestChangeAt(params), mode).n;
    if (n > bestN) { best = mode; bestN = n; }
  }
  return best;
}

/** A supplier's delivery record for ONE shipping method.
 *
 *  Blending lanes gives a number that describes no real shipment: a 44-day sea crossing
 *  averaged with a 6-day air run is wrong for both, and the on-time rate mixes two
 *  completely different promises. Every figure here is per-lane for that reason.
 */
export function supplierLaneStats(supplier, mode = null) {
  const orders = (supplier?.orders || []).filter(o =>
    o.receivedDate && o.orderedDate && (!mode || (o.freightMode || null) === mode));
  if (!orders.length) {
    return { mode, n: 0, avg: null, p80: null, onTimeRate: null, avgVariance: null, late: 0 };
  }
  const lts = orders
    .map(o => Math.round((new Date(o.receivedDate) - new Date(o.orderedDate)) / 86400000))
    .filter(d => d >= 0);
  const promised = orders.filter(o => o.expectedDate);
  const variances = promised.map(o =>
    Math.round((new Date(o.receivedDate) - new Date(o.expectedDate)) / 86400000));
  const late = variances.filter(v => v > 0).length;
  return {
    mode, n: lts.length,
    avg: lts.length ? Math.round(lts.reduce((a, b) => a + b, 0) / lts.length) : null,
    p80: lts.length ? Math.round(pctile(lts, 80)) : null,
    onTimeRate: promised.length ? Math.round((promised.length - late) / promised.length * 100) : null,
    // Deliveries with no expected date can't be scored. Counted, not hidden, so the UI
    // can explain an absent rate instead of showing a bare dash.
    unpromised: orders.filter(o => !o.expectedDate).length,
    avgVariance: variances.length
      ? Math.round(variances.reduce((a, b) => a + b, 0) / variances.length) : null,
    late, promised: promised.length,
  };
}

/** Every lane this supplier has actually shipped, plus an all-lanes total. */
export function supplierStatsByLane(supplier) {
  const out = { all: supplierLaneStats(supplier, null), lanes: {} };
  for (const mode of Object.keys(FREIGHT_MODES)) {
    const st = supplierLaneStats(supplier, mode);
    if (st.n > 0) out.lanes[mode] = st;
  }
  const tagged = Object.values(out.lanes).reduce((a, s) => a + s.n, 0);
  out.untagged = Math.max(0, out.all.n - tagged);
  out.laneCount = Object.keys(out.lanes).length;
  return out;
}

/* ── Grading a live week ─────────────────────────────────────────────────────────────
 *
 * A forecast is never exactly right, so "was it correct" is the wrong question. The right
 * one is "was it off by more than its own uncertainty said it would be", and that means
 * measuring the miss in units of the product's OWN predicted band:
 *
 *     z = (actual − predicted) / halfWidth,   halfWidth = (hi − lo) / 2
 *
 * z = 0 lands on the midpoint, |z| = 1 sits exactly on the band edge, |z| = 2 misses by a
 * whole extra band. This is the only fair scale across a catalogue: a volatile product
 * legitimately carries a wide band, so being 40 units out is expected of it, while the
 * same 40 units on a stable narrow-band product is a real failure. Percentage error would
 * call the volatile one worse — it punishes products for being volatile, which is exactly
 * the trap that inflates WAPE on the intermittent SKUs.
 *
 * Why the hit rate ALONE can't be the grade: an interval can be made to contain the truth
 * 100% of the time by making it absurdly wide. Coverage rewards vagueness. Pairing it with
 * the typical |z| closes that door — wide lazy bands show a suspiciously LOW median |z|,
 * and the grade below treats that as its own failure rather than as success.
 *
 * For a well-calibrated 80% band the median |z| is ≈ 0.53 (the median of |N(0,1)| is
 * 0.674, and an 80% interval is ±1.282σ, so 0.674 / 1.282). That constant is what
 * "textbook" means here — not zero, which would mean the bands are far too wide.
 */
export const IDEAL_MEDIAN_Z = 0.53;
// Below 10 scored products a hit rate swings on binomial noise alone — ±13 points at
// n=10 even from a perfect model. Under this, report the number but refuse the verdict.
export const MIN_SCORED_FOR_VERDICT = 10;

/** How far off one prediction was, in band-widths. Null when there's no band to measure
 *  against, or when the band is degenerate (a zero-width band makes every miss infinite). */
export function bandZ(entry) {
  const a = Number(entry?.actual), p = Number(entry?.predicted);
  const lo = Number(entry?.lo), hi = Number(entry?.hi);
  if (![a, p, lo, hi].every(Number.isFinite)) return null;
  const half = (hi - lo) / 2;
  if (!(half > 0)) return null;
  return (a - p) / half;
}

export function median(xs) {
  const v = xs.filter(Number.isFinite).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

/**
 * Grade a set of scored predictions on both axes at once: did actuals land inside the
 * bands as often as the bands claim, and were the midpoints close.
 *
 * @param entries scored live-log entries (predicted/lo/hi/actual)
 * @param context optional wider set (e.g. the last 4 weeks) used for the VERDICT while
 *        the headline number still comes from `entries`. One week is worth showing and
 *        a poor thing to judge on.
 */
export function gradeWeek(entries, context = null) {
  const scored = (entries || []).filter(e => Number.isFinite(Number(e?.actual)));
  const zs = scored.map(bandZ).filter(v => v != null);
  const inBand = scored.filter(e => e.inBand === true).length;
  const withBand = scored.filter(e => e.inBand === true || e.inBand === false).length;
  const nominal = scored.length ? (Number(scored[0].band) || 80) : 80;
  const out = {
    n: scored.length, withBand, inBand, nominal,
    inRate: withBand ? Math.round(inBand / withBand * 100) : null,
    medianZ: median(zs.map(Math.abs)),
    // Signed: positive means actuals came in ABOVE forecast (under-ordering risk),
    // negative means the forecast ran hot (excess stock). Direction changes the action,
    // so a grade that only reports magnitude hides the half that matters.
    biasZ: median(zs),
    tone: "idle", verdict: "too-few", judged: false,
  };
  if (!out.n) return out;

  // The verdict may be drawn from a wider window than the number on display.
  const judge = context && context.length ? gradeWeek(context) : null;
  const jr = judge ? judge.inRate : out.inRate;
  const jz = judge ? judge.medianZ : out.medianZ;
  const jn = judge ? judge.n : out.n;
  out.judgedOn = jn;
  if (jn < MIN_SCORED_FOR_VERDICT || jr == null || jz == null) {
    out.tone = "idle"; out.verdict = "too-few";
    return out;
  }
  out.judged = true;
  const gap = jr - nominal;                 // coverage vs what the band promised
  if (jz > 1.0 || gap < -20) {
    // Typical miss at or beyond the band edge: the intervals are too narrow and every
    // buffer sized from them is optimistic.
    out.tone = "bad"; out.verdict = "bands-too-narrow";
  } else if (jz < 0.25 && gap > 12) {
    // Almost everything inside, and hugging the midpoint far too closely — the bands are
    // padded, which looks like accuracy and quietly buys stock you don't need.
    out.tone = "warn"; out.verdict = "bands-too-wide";
  } else if (jz <= 0.75 && Math.abs(gap) <= 12) {
    out.tone = "good"; out.verdict = "calibrated";
  } else {
    out.tone = "warn"; out.verdict = "drifting";
  }
  return out;
}

/** Every supplier's deliveries, pooled per lane, for the page-level strip.
 *
 *  Pooled across suppliers this is a portfolio figure, not a planning one — nothing reads
 *  it to decide a date. It exists to answer "how are my suppliers doing overall", which is
 *  a question about the whole book and is meaningless per SKU. */
export function allSuppliersByLane(suppliers) {
  const merged = { orders: Object.values(suppliers || {}).flatMap(s => s.orders || []) };
  return supplierStatsByLane(merged);
}

/** Supplier-level rollup. REPORTING ONLY - planning never reads a supplier-wide number
 *  as a silent default; it only ever borrows a specific lane, and says when it does. */
/* A delivery that arrived late for a known reason is NOT excluded from lane history.
 * Every late shipment has a cause; excluding them all deletes the tail that P80 exists to
 * capture, and quietly turns an intended 80% service level into roughly 60%. The one-off
 * flag is forward-looking only: it adjusts the ETA of the PO in transit, and the delivery
 * counts normally once it lands. P80 already forgives rare events in proportion to how
 * rarely they happen. The reason is kept as a label for supplier reporting. */
export function supplierLeadTimeSummary(supplierId, suppliers, paramsBySku = {}) {
  const entry = (suppliers || {})[supplierId];
  if (!entry) return null;
  const skuIds = entry.skuIds || [];
  const perSku = skuIds.map(id => {
    const st = computeSkuLeadTimeStats(id, suppliers, null, null);
    return { skuId: id, avg: st.avg, p80: st.p80, n: st.n };
  }).filter(r => r.n > 0);

  const byMode = {};
  for (const mode of Object.keys(FREIGHT_MODES)) {
    const lts = (entry.orders || [])
      .filter(o => o.receivedDate && o.orderedDate && (o.freightMode || null) === mode)
      .map(o => Math.round((new Date(o.receivedDate) - new Date(o.orderedDate)) / 86400000))
      .filter(d => d >= 0);
    if (lts.length) {
      byMode[mode] = {
        label: FREIGHT_MODES[mode], n: lts.length,
        avg: Math.round(lts.reduce((a, b) => a + b, 0) / lts.length),
        p80: Math.round(pctile(lts, 80)),
      };
    }
  }
  const untagged = (entry.orders || [])
    .filter(o => o.receivedDate && o.orderedDate && !o.freightMode).length;

  if (!perSku.length) return { supplierName: entry.name, products: 0, byMode, untagged };
  const sorted = [...perSku].sort((a, b) => a.avg - b.avg);
  return {
    supplierName: entry.name,
    products: perSku.length,
    shipments: perSku.reduce((a, r) => a + r.n, 0),
    avgProductLeadTime: Math.round(perSku.reduce((a, r) => a + r.avg, 0) / perSku.length),
    fastest: sorted[0],
    slowest: sorted[sorted.length - 1],
    byMode, untagged,
  };
}

// holding.js — work out an annual inventory holding rate from a short questionnaire.
//
// DETERMINISM IS THE WHOLE POINT. The same answers must always produce the same rate,
// so no AI ever produces a number here. The rate is plain arithmetic over lookup tables
// below — identical inputs, identical output, forever, and auditable line by line.
//
// An AI is used for exactly one thing: mapping free-text like "we sell bathroom vanities
// online" onto one of the INDUSTRY keys below. That's a classification with a fixed,
// finite answer set, not a number. It's also tried LOCALLY first (keyword match), cached
// by normalised text, and pinned to temperature 0 — so in practice it rarely runs, and
// when it does the same sentence maps to the same key every time.

// ── Component tables (percentage points of unit cost, per year) ───────────────
// Sources are ordinary inventory-carrying-cost components; the split is what lets the
// result be explained rather than asserted.
export const CAPITAL = {
  own_cash:   { pct: 7,  label: "Own cash, no borrowing", note: "opportunity cost (what that money could earn elsewhere)" },
  bank_loan:  { pct: 11, label: "Bank loan or credit line", note: "typical small-business borrowing cost" },
  expensive:  { pct: 20, label: "Card, factoring or merchant advance", note: "high-cost financing" },
  investor:   { pct: 13, label: "Investor funded", note: "expected return on capital" },
};

export const STORAGE = {
  own_small:  { pct: 2,  label: "Own/leased space, compact products" },
  own_bulky:  { pct: 5,  label: "Own/leased space, bulky products" },
  third_small:{ pct: 5,  label: "3PL or fulfilment centre, compact" },
  third_bulky:{ pct: 10, label: "3PL or fulfilment centre, bulky" },
  dropship:   { pct: 0,  label: "Don't hold stock (dropship)" },
};

export const OBSOLESCENCE = {
  stable:     { pct: 1,  label: "Barely dates: same product for years" },
  seasonal:   { pct: 4,  label: "Seasonal: leftovers get discounted" },
  trend:      { pct: 9,  label: "Style/trend driven: goes out of fashion" },
  tech:       { pct: 8,  label: "Tech: superseded by new models" },
  perishable: { pct: 15, label: "Dated or perishable: expires" },
};

export const RISK = {
  durable:    { pct: 1,  label: "Durable, low theft risk" },
  fragile:    { pct: 3,  label: "Fragile: breakages happen" },
  valuable:   { pct: 4,  label: "Small and valuable: shrinkage risk" },
};

const INSURANCE_TAX = 1;   // insurance + property/inventory tax, roughly flat

// ── Industry presets: sensible defaults for the two questions people can't answer ──
export const INDUSTRY = {
  furniture:      { label: "Furniture & large home goods", storageBulk: "bulky",  obsolescence: "seasonal",   risk: "fragile" },
  home_decor:     { label: "Home décor & small furnishings", storageBulk: "compact", obsolescence: "trend",   risk: "fragile" },
  building:       { label: "Building materials & fixtures", storageBulk: "bulky",  obsolescence: "stable",    risk: "durable" },
  apparel:        { label: "Apparel & footwear",           storageBulk: "compact", obsolescence: "trend",     risk: "durable" },
  electronics:    { label: "Electronics & tech",           storageBulk: "compact", obsolescence: "tech",      risk: "valuable" },
  food:           { label: "Food, drink & perishables",    storageBulk: "compact", obsolescence: "perishable",risk: "durable" },
  beauty:         { label: "Beauty & personal care",       storageBulk: "compact", obsolescence: "perishable",risk: "valuable" },
  jewellery:      { label: "Jewellery & watches",          storageBulk: "compact", obsolescence: "stable",    risk: "valuable" },
  auto_parts:     { label: "Auto parts & industrial",      storageBulk: "bulky",   obsolescence: "stable",    risk: "durable" },
  toys:           { label: "Toys, games & hobby",          storageBulk: "compact", obsolescence: "seasonal",  risk: "durable" },
  sports:         { label: "Sports & outdoor",             storageBulk: "bulky",   obsolescence: "seasonal",  risk: "durable" },
  pet:            { label: "Pet supplies",                 storageBulk: "bulky",   obsolescence: "stable",    risk: "durable" },
  books_media:    { label: "Books & media",                storageBulk: "compact", obsolescence: "trend",     risk: "durable" },
  general:        { label: "Something else / mixed",       storageBulk: "compact", obsolescence: "seasonal",  risk: "durable" },
};

// Keyword matcher — tried BEFORE any AI call, so most inputs never need one.
const KEYWORDS = [
  ["furniture", ["vanit", "furnitur", "sofa", "couch", "cabinet", "mattress", "table", "chair", "desk", "dresser", "wardrobe"]],
  ["home_decor", ["decor", "mirror", "rug", "cushion", "candle", "lamp", "lighting", "art", "frame", "vase"]],
  ["building", ["tile", "faucet", "plumb", "lumber", "hardware", "fixture", "sink", "tap", "paint", "floor", "door", "window"]],
  ["apparel", ["cloth", "apparel", "shoe", "footwear", "shirt", "dress", "fashion", "garment", "jacket", "sock"]],
  ["electronics", ["electronic", "laptop", "phone", "computer", "gadget", "camera", "headphone", "console", "tv "]],
  ["food", ["food", "drink", "beverage", "coffee", "tea", "snack", "grocer", "bakery", "produce", "wine", "beer"]],
  ["beauty", ["beauty", "cosmetic", "skincare", "makeup", "fragrance", "perfume", "shampoo", "supplement"]],
  ["jewellery", ["jewel", "jewellery", "jewelry", "watch", "ring", "necklace", "diamond", "gold"]],
  ["auto_parts", ["auto", "car part", "vehicle", "tyre", "tire", "engine", "industrial", "machinery", "tool"]],
  ["toys", ["toy", "game", "puzzle", "hobby", "lego", "board game", "model kit"]],
  ["sports", ["sport", "outdoor", "camping", "fitness", "bike", "bicycle", "gym", "ski", "golf"]],
  ["pet", ["pet ", "dog", "cat ", "animal feed", "aquarium"]],
  ["books_media", ["book", "magazine", "vinyl", "media", "stationery", "print"]],
];

export function classifyIndustryLocally(text) {
  const t = String(text || "").toLowerCase();
  if (!t.trim()) return null;
  for (const [key, words] of KEYWORDS) {
    if (words.some((w) => t.includes(w))) return key;
  }
  return null;
}

// ── The calculator: pure arithmetic, no randomness, no network ────────────────
export function computeHolding({ capital, storageBulk, obsolescence, risk, dropship = false,
                                 knownRate = null }) {
  // If they know their borrowing rate, use it verbatim rather than a bracket.
  const capitalPct = knownRate != null && knownRate !== "" && Number.isFinite(Number(knownRate))
    ? Math.max(0, Math.min(60, Number(knownRate)))
    : (CAPITAL[capital] || CAPITAL.own_cash).pct;
  const storageKey = dropship ? "dropship"
    : storageBulk === "third_bulky" || storageBulk === "third_small" ||
      storageBulk === "own_bulky" || storageBulk === "own_small" ? storageBulk : "own_small";
  const storagePct = (STORAGE[storageKey] || STORAGE.own_small).pct;
  const obsPct = (OBSOLESCENCE[obsolescence] || OBSOLESCENCE.seasonal).pct;
  const riskPct = (RISK[risk] || RISK.durable).pct;
  const total = capitalPct + storagePct + obsPct + riskPct + INSURANCE_TAX;
  return {
    holding: Math.round(total),                 // integer → stable, no drifting decimals
    breakdown: [
      { part: "Cost of capital", pct: capitalPct,
        why: knownRate ? "your stated borrowing rate" : (CAPITAL[capital] || CAPITAL.own_cash).note },
      { part: "Storage & handling", pct: storagePct, why: (STORAGE[storageKey] || {}).label },
      { part: "Obsolescence / markdowns", pct: obsPct, why: (OBSOLESCENCE[obsolescence] || {}).label },
      { part: "Damage & shrinkage", pct: riskPct, why: (RISK[risk] || {}).label },
      { part: "Insurance & tax", pct: INSURANCE_TAX, why: "typical flat allowance" },
    ],
  };
}

// Industry preset → the two answers people usually can't give themselves.
export function defaultsForIndustry(key, ownsSpace = true) {
  const ind = INDUSTRY[key] || INDUSTRY.general;
  const bulky = ind.storageBulk === "bulky";
  return {
    storageBulk: ownsSpace ? (bulky ? "own_bulky" : "own_small") : (bulky ? "third_bulky" : "third_small"),
    obsolescence: ind.obsolescence,
    risk: ind.risk,
  };
}

// ── AI classification: cached, constrained, temperature 0 ────────────────────
const CACHE_KEY = "logitrack_industry_cache";
const norm = (t) => String(t || "").toLowerCase().replace(/[^a-z0-9 ]/g, " ").replace(/\s+/g, " ").trim();

function readCache() {
  try { return JSON.parse(localStorage.getItem(CACHE_KEY) || "{}"); } catch { return {}; }
}
function writeCache(c) {
  try { localStorage.setItem(CACHE_KEY, JSON.stringify(c)); } catch { /* ignore */ }
}

/**
 * Map free text to an INDUSTRY key. Deterministic in practice because:
 *   1. an exact-match cache is checked first — the same text ALWAYS returns the same key;
 *   2. a local keyword matcher handles most inputs with no model involved;
 *   3. the model only ever picks from a fixed list, at temperature 0, and anything not
 *      on the list is discarded rather than trusted.
 * The model never sees or produces a rate.
 */
export async function classifyIndustry(text, { apiKey, url, model } = {}) {
  const key = norm(text);
  if (!key) return { key: "general", how: "default" };
  const cache = readCache();
  if (cache[key]) return { key: cache[key], how: "cached" };
  const local = classifyIndustryLocally(key);
  if (local) { cache[key] = local; writeCache(cache); return { key: local, how: "keyword" }; }
  if (!apiKey || !url || !model) return { key: "general", how: "default" };
  const allowed = Object.keys(INDUSTRY).join(", ");
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify({
        // reasoning_effort: gpt-oss models spend max_tokens on internal reasoning
        // first, and 12 reserved tokens leaves nothing for an answer. See
        // GROQ_LOW_REASONING in lib/ai.js for the measurement. Falls back to
        // classifyIndustryLocally either way, so this only restores the AI path.
        model, temperature: 0, max_tokens: 12, seed: 7, reasoning_effort: "low",
        messages: [{
          role: "user",
          content: `Classify this business into EXACTLY ONE category. Reply with the category key only, no punctuation or explanation.\n\nKeys: ${allowed}\n\nBusiness: ${text}`,
        }],
      }),
    });
    const d = await r.json();
    const raw = norm(d?.choices?.[0]?.message?.content).replace(/ /g, "_");
    const picked = Object.keys(INDUSTRY).includes(raw) ? raw : "general";
    cache[key] = picked; writeCache(cache);
    return { key: picked, how: picked === "general" ? "default" : "ai" };
  } catch {
    return { key: "general", how: "default" };
  }
}

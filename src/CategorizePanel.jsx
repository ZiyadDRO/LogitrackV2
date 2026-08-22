// ─────────────────────────────────────────────────────────────────────────
// CategorizePanel.jsx
// "Dump product info" → AI classification → similar products.
//
// A left-edge tab opens a panel where you paste free-text product info for each
// SKU. "Classify and Apply Changes" sends it to Groq, extracts structured attributes
// (category, brand, size, color, material, style), saves those attributes to the
// backend, then re-groups the catalog and returns, for
// every product, which other products are considered similar (and whether that
// makes it eligible for the pooled global model).
//
// Works for ALL products, not just ones that end up using the global model.
//
// Props: skuList [{id,name}], api (base url), apiKey (Groq), lm (light mode).
// ─────────────────────────────────────────────────────────────────────────
import React, { useState, useEffect } from 'react';
import { loadStorage, saveStorage } from './lib/storage';
import { fetchJson } from './lib/api';
import { GROQ_URL, GROQ_MODEL } from './lib/ai';

const ATTR_KEYS = ["category", "subcategory", "brand", "size", "color", "material", "style"];
const INFO_STORAGE_KEY = "logitrack_product_descriptions";
const CLASSES_STORAGE_KEY = "logitrack_product_classes";   // skuId → attributes (survives reloads)
const NOTES_STORAGE_KEY = "logitrack_group_notes";         // clusterKey → AI one-liner
const AUTOCLASSIFIED_KEY = "logitrack_autoclassified_ids"; // skuIds we've already auto-classified once
const NO_CATEGORY_RE = /\b(no category|uncategorized|uncategorised|unknown category|no product family)\b/i;
const FAMILY_LABEL_KEYS = ["subcategory", "vanity_type", "product_type", "type"];
const SPLIT_DETAIL_KEYS = ["mounting", "sink_count", "finish", "color", "style", "material", "top_material", "hardware_finish", "brand", "size"];

function parseJsonLoose(text) {
  if (!text) return null;
  let t = text.trim().replace(/^```(?:json)?/i, "").replace(/```$/i, "").trim();
  const a = t.indexOf("{"), b = t.lastIndexOf("}");
  if (a >= 0 && b > a) t = t.slice(a, b + 1);
  try { return JSON.parse(t); } catch { return null; }
}

function humanizeValue(value) {
  return String(value || "")
    .replace(/_/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function titleCaseLoose(value) {
  const text = humanizeValue(value);
  if (!text) return "";
  if (/[a-z]/.test(text) && /[A-Z]/.test(text)) return text;
  return text.replace(/\b[a-z]/g, ch => ch.toUpperCase());
}

function familyNounFromCategory(category) {
  const clean = titleCaseLoose(category);
  if (!clean || clean === "Uncategorized") return "Products";
  const words = clean.split(/\s+/).filter(Boolean);
  return words[words.length - 1] || "Products";
}

function singularFamilyNoun(noun) {
  const lower = humanizeValue(noun).toLowerCase();
  if (lower.endsWith("ies")) return lower.slice(0, -3) + "y";
  if (lower.endsWith("s")) return lower.slice(0, -1);
  return lower;
}

function productFamilyLabel(value, category) {
  const base = titleCaseLoose(value);
  if (!base) return "";
  const noun = familyNounFromCategory(category);
  const lower = base.toLowerCase();
  const singular = singularFamilyNoun(noun);
  if (lower.includes(noun.toLowerCase()) || (singular && lower.includes(singular))) return base;
  return `${base} ${noun}`;
}

function commonAttrValue(members, key) {
  const values = (members || [])
    .map(s => humanizeValue(s.attributes?.[key]))
    .filter(Boolean);
  if (!values.length) return "";
  const first = values[0].toLowerCase();
  return values.every(v => v.toLowerCase() === first) ? values[0] : "";
}

function behaviorGroupTitle(members, category, index = 0) {
  const first = members?.[0] || {};
  const info = first.groupInfo || {};
  const splitCols = Array.isArray(info.splitCols) ? info.splitCols : [];
  const groupCols = Array.isArray(info.groupCols) ? info.groupCols : [];
  const titleKeys = [...FAMILY_LABEL_KEYS, ...groupCols, ...splitCols];
  let familyKey = "";
  let familyValue = "";
  for (const key of titleKeys) {
    if (key === "category" || !FAMILY_LABEL_KEYS.includes(key)) continue;
    const value = commonAttrValue(members, key);
    if (value) { familyKey = key; familyValue = value; break; }
  }
  const familyTitle = familyValue ? productFamilyLabel(familyValue, category) : "";
  const detailKeys = (familyTitle ? splitCols : [...splitCols, ...SPLIT_DETAIL_KEYS])
    .filter((key, pos, arr) => key && key !== "category" && key !== familyKey && arr.indexOf(key) === pos);
  const detailValues = detailKeys.map(key => titleCaseLoose(commonAttrValue(members, key))).filter(Boolean);
  if (familyTitle) {
    return detailValues.length ? `${familyTitle} - ${detailValues.slice(0, 2).join(" - ")}` : familyTitle;
  }
  if (detailValues.length) return detailValues.slice(0, 2).join(" - ");
  return `Behavior group ${index + 1}`;
}

export default function CategorizePanel({ skuList = [], api = "http://localhost:8000", apiKey, lm = false, onApplied, open = false, onClose = () => {}, embedded = false, scorecardRows = [] }) {
  const [info, setInfo] = useState(() => loadStorage(INFO_STORAGE_KEY, {}));         // skuId → pasted text
  const [classes, setClasses] = useState(() => loadStorage(CLASSES_STORAGE_KEY, {})); // skuId → attributes object (persisted)
  const [groups, setGroups] = useState(null);      // backend /api/groups response
  const [phase, setPhase] = useState("idle");      // idle | classifying | applying
  const [err, setErr] = useState(null);

  const [openGroups, setOpenGroups] = useState({});   // category → collapsed?
  const [q, setQ] = useState("");                      // filter the describe list
  const [groupNotes, setGroupNotes] = useState(() => loadStorage(NOTES_STORAGE_KEY, {})); // clusterKey → AI-written one-liner (persisted)
  const panelBg = lm ? 'bg-slate-100' : 'bg-[#0c0c0e]';
  const border = lm ? 'border-slate-200' : 'border-white/10';
  const textMain = lm ? 'text-slate-900' : 'text-white';
  const textBody = lm ? 'text-slate-600' : 'text-slate-300';
  const textMute = lm ? 'text-slate-500' : 'text-slate-500';
  const cardBg = lm ? 'bg-white border-slate-200' : 'bg-[#161619] border-white/10';
  const inputCls = lm
    ? 'w-full bg-white border border-slate-300 rounded-lg px-2.5 py-2 text-xs text-slate-900 focus:outline-none focus:border-violet-500'
    : 'w-full bg-[#161619] border border-white/10 rounded-lg px-2.5 py-2 text-xs text-slate-200 focus:outline-none focus:border-violet-500/60';

  useEffect(() => { saveStorage(INFO_STORAGE_KEY, info); }, [info]);
  // Persist classifications + group notes so the grouping view shows instantly on
  // load and doesn't vanish when you switch tabs or restart the backend.
  useEffect(() => { saveStorage(CLASSES_STORAGE_KEY, classes); }, [classes]);
  useEffect(() => { saveStorage(NOTES_STORAGE_KEY, groupNotes); }, [groupNotes]);

  async function loadGroups() {
    try {
      const g = await fetchJson(`${api}/api/groups`, undefined, "Loading product groups");
      if (g) {
        setGroups(g);
        const seed = {};
        (g.skus || []).forEach(s => { if (s.attributes && Object.keys(s.attributes).length) seed[s.skuId] = s.attributes; });
        if (Object.keys(seed).length) setClasses(c => ({ ...seed, ...c }));
        // If we have saved classifications the backend doesn't know about (e.g. it was
        // restarted and lost its in-memory attrs), push them back so groups recompute
        // correctly — without re-calling the AI.
        const missing = {};
        Object.entries(classes).forEach(([sid, attrs]) => {
          if (!attrs || !Object.keys(attrs).length) return;
          const known = (g.skus || []).find(s => s.skuId === sid)?.attributes || {};
          if (!Object.keys(known).length && skuList.some(s => s.id === sid)) missing[sid] = attrs;
        });
        if (Object.keys(missing).length) { try { await saveAttributes(missing); } catch {} }
        else explainGroups(g);
        return g;
      }
    } catch { /* backend may be the Prophet-only build; panel still usable */ }
    return null;
  }

  // Load groups when opened (slide-over) or on mount (embedded tab).
  useEffect(() => { if (open || embedded) { setErr(null); loadGroups(); } }, [open, embedded]);

  // Auto-classify ONCE for products we've never classified before (e.g. a fresh
  // upload or a newly-added batch), so grouping "just works" from the start. Each
  // SKU is remembered in a persisted set the moment it's auto-classified, so it is
  // never auto-classified again — subsequent re-runs are manual. Newly-added
  // products (ids not in the set) still get their single automatic pass.
  useEffect(() => {
    if (!(open || embedded)) return;
    if (!apiKey || phase !== "idle" || !skuList.length) return;
    const seen = loadStorage(AUTOCLASSIFIED_KEY, {});
    const neverClassified = skuList.filter(s => !(classes[s.id] && classes[s.id].category) && !seen[s.id]);
    if (neverClassified.length === 0) return;
    // Mark every current SKU as seen up front (persisted synchronously) so a re-render
    // mid-classification can't trigger a second pass.
    const nextSeen = { ...seen };
    skuList.forEach(s => { nextSeen[s.id] = true; });
    saveStorage(AUTOCLASSIFIED_KEY, nextSeen);
    classifyAndApply();
  }, [open, embedded, apiKey, skuList, phase, classes]);

  function buildClassificationPrompt() {
    const lines = skuList.map(s => {
      const extra = (info[s.id] || "").trim().replace(/\s+/g, " ");
      const existing = classes[s.id] && Object.keys(classes[s.id]).length
        ? ` Current saved tags: ${JSON.stringify(classes[s.id])}.`
        : "";
      return `${s.id}: ${s.name || s.id}${extra ? " — user description: " + extra : ""}${existing}`;
    }).join("\n");
    return (
`You are a product-catalog taxonomist. Below is a store's FULL product list (id: name — optional user description — current saved tags). Assign a coherent, CONSISTENT set of categories that groups genuinely similar products together, and extract other attributes.

Rules:
- Consider the whole catalog at once. Reuse the SAME category for products that belong together; never invent a near-duplicate category per item.
- The user description is the most authoritative clue. If the generic product name conflicts with the user's description, trust the description.
- Categories must be real product families (e.g. "Beverages", "Drinkware", "Phone Accessories", "Apparel", "Power Tools") — NOT superficial traits alone (color/material/size) and NOT vague catch-alls ("Core", "Misc", "Other", "General", "New").
- subcategory is a finer sub-family WITHIN the category (e.g. category "Drinkware" → subcategory "Travel Mugs"; category "Vanities" → subcategory "Freestanding Vanities"). Reuse the SAME subcategory across products that belong to the same sub-family; omit it when the category has no meaningful split. Never make the subcategory equal to the category.
- Only place a product in a category if it genuinely fits; if nothing fits well, omit its category (better ungrouped than wrongly grouped).
- If a product name or description explicitly says "No Category", "Uncategorized", or "Unknown category", leave category omitted even if the product name hints at a family.
- Existing saved tags are context, not a constraint. Update them when the description makes a better category or attribute clear.
- This may be re-run as new products arrive, so pick the category names that make the OVERALL grouping cleanest, even if that slightly redefines an existing category for a better match.

Return ONLY a JSON object mapping each product id to an object using any of these keys you can determine: ${ATTR_KEYS.join(", ")}. Use concise canonical values. Omit keys you cannot determine. No prose, no code fences.

Products:
${lines}`
    );
  }

  async function saveAttributes(skus) {
    let res;
    try {
      res = await fetch(`${api}/api/attributes`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ skus }),
      });
    } catch (e) {
      // fetch rejected = no response at all → backend not listening / blocked by CORS.
      const err = new Error("network"); err.kind = "network"; throw err;
    }
    if (!res.ok) {
      // got a response, but an error status → backend is up but the save failed server-side.
      const err = new Error("http"); err.kind = "http"; err.status = res.status; throw err;
    }
    const result = await res.json();
    const backendAttrs = {};
    (result.skus || []).forEach(s => { backendAttrs[s.skuId] = s.attributes || {}; });
    if (Object.keys(backendAttrs).length) setClasses(c => ({ ...c, ...backendAttrs }));
    setGroups(result); explainGroups(result);
    onApplied?.();
    return result;
  }

  function attrsForSku(skuId, fallbackAttrs = {}) {
    if (Object.prototype.hasOwnProperty.call(classes, skuId)) return classes[skuId] || {};
    const grouped = groups?.skus?.find(s => s.skuId === skuId)?.attributes;
    return grouped || fallbackAttrs || {};
  }

  async function classifyAndApply() {
    setErr(null);
    if (!skuList.length) { setErr("No products loaded yet."); return; }
    if (!apiKey) { setErr("Add an AI API key before classifying product descriptions."); return; }
    setPhase("classifying");
    // Step 1 — AI: read the descriptions and extract tags (this is the same Groq
    // call that powers "Ask AI"). Failures here are about the AI key / connection.
    let next;
    try {
      const res = await fetch(GROQ_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${apiKey}` },
        body: JSON.stringify({ model: GROQ_MODEL, messages: [{ role: "user", content: buildClassificationPrompt() }], max_tokens: 2400, temperature: 0.1 }),
      });
      const data = await res.json();
      const parsed = parseJsonLoose(data?.choices?.[0]?.message?.content);
      if (!parsed) { setErr("Couldn't parse the AI response. Try again or simplify the info."); setPhase("idle"); return; }
      next = {};
      Object.entries(parsed).forEach(([skuId, attrs]) => {
        if (attrs && typeof attrs === "object") {
          const sku = skuList.find(s => s.id === skuId);
          const evidence = `${sku?.name || ""} ${info[skuId] || ""}`;
          const merged = { ...(classes[skuId] || {}), ...attrs };
          if (NO_CATEGORY_RE.test(evidence)) merged.category = null;
          next[skuId] = merged;
        }
      });
      if (!Object.keys(next).length) { setErr("The AI did not return any usable tags."); setPhase("idle"); return; }
      setClasses(c => ({ ...c, ...next }));
    } catch {
      setErr("Couldn't reach the AI to read your product descriptions — check the AI key / internet connection.");
      setPhase("idle");
      return;
    }
    // Step 2 — backend: save the tags and re-group the catalog. A failure here is the
    // local backend, NOT the AI — so say so (the tags were generated fine).
    try {
      setPhase("applying");
      await saveAttributes(next);
    } catch (e) {
      if (e?.kind === "http") {
        setErr(`Tags were generated and the backend responded, but rejected the save (HTTP ${e.status}). Check the LogiTrack terminal window for the error trace.`);
      } else {
        setErr(`Tags were generated, but the backend at ${api} didn't respond at all — the server isn't running there. Open ${api}/api/health in a tab: if it doesn't load, relaunch with "Start LogiTrack.command" and keep that window open.`);
      }
    } finally {
      setPhase("idle");
    }
  }

  async function removeTag(skuId, key) {
    const prevAttrs = attrsForSku(skuId);
    if (!(key in prevAttrs)) return;
    setClasses(c => {
      const nextAttrs = { ...attrsForSku(skuId) };
      delete nextAttrs[key];
      return { ...c, [skuId]: nextAttrs };
    });
    setPhase("applying"); setErr(null);
    try {
      await saveAttributes({ [skuId]: { [key]: null } });
    } catch {
      setClasses(c => ({ ...c, [skuId]: prevAttrs }));
      setErr("Couldn't remove that tag — is the backend running?");
    } finally {
      setPhase("idle");
    }
  }

  async function clearTags(skuId) {
    const prevAttrs = attrsForSku(skuId);
    const keys = Object.keys(prevAttrs);
    if (!keys.length) return;
    setClasses(c => ({ ...c, [skuId]: {} }));
    setPhase("applying"); setErr(null);
    try {
      await saveAttributes({ [skuId]: Object.fromEntries(keys.map(k => [k, null])) });
    } catch {
      setClasses(c => ({ ...c, [skuId]: prevAttrs }));
      setErr("Couldn't remove those tags — is the backend running?");
    } finally {
      setPhase("idle");
    }
  }

  // Export the current grouping as a CSV (client-side — no backend needed): one row per
  // product with its category/subcategory, attributes, model, and the products it pools with.
  function downloadGroupingCsv() {
    const rows = groups?.skus || [];
    if (!rows.length) return;
    const clean = (v) => {
      if (v == null) return "";
      let s = String(v).trim();
      const m = s.match(/^[a-z][a-z _-]*:\s*(.+)$/i); if (m) s = m[1].trim();
      s = s.replace(/_/g, " ").replace(/\s+/g, " ").trim();
      if (s && !/[A-Z]/.test(s)) s = s.replace(/\b\w/g, c => c.toUpperCase());
      return s;
    };
    const catOf = (s) => clean(s.baseGroup) || clean(s.attributes?.category) || "Uncategorized";
    const FAMILY_KEYS = ["subcategory", "sub_category", "vanity_type", "product_type", "type", "family", "product_family", "collection", "class"];
    const isAssorted = (s, cat) => /\b(assorted|mixed|miscellaneous|misc|various|bundle|sampler|grab\s?bag|job\s?lot|pallet|warehouse|lot)\b/i.test(`${s.skuName || ""} ${cat || ""} ${s.attributes?.category || ""}`);
    // Same subcategory derivation the folders use: engine's adaptive split first, then a
    // family attribute — so the CSV matches the nested folders (e.g. coffee → Ground / 1 Lb).
    const subOf = (s, cat) => {
      if (isAssorted(s, cat)) return "";
      const g = s.group != null ? String(s.group) : "";
      if (g.includes("|")) {
        const label = g.split("|").slice(1).map(p => { const i = p.indexOf(":"); return clean(i >= 0 ? p.slice(i + 1) : p); }).filter(Boolean).join(" · ");
        if (label && label.toLowerCase() !== cat.toLowerCase()) return label;
      }
      const a = s.attributes || {};
      for (const k of FAMILY_KEYS) { if (a[k]) { const v = clean(a[k]); if (v && v.toLowerCase() !== cat.toLowerCase()) return v; } }
      return "";
    };
    const esc = (v) => { const s = v == null ? "" : String(v); return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    const header = ["SKU ID", "Product Name", "Category", "Subcategory", "Brand", "Size", "Color", "Material", "Style", "Model / route", "Pooling mode", "Similar products"];
    const lines = [header.join(",")];
    rows.forEach((s) => {
      const a = s.attributes || {};
      const cat = catOf(s);
      const pooled = (s.similar || []).map((x) => x.skuName || x.skuId).join("; ");
      lines.push([
        s.skuId, s.skuName, cat, subOf(s, cat), clean(a.brand), clean(a.size),
        clean(a.color), clean(a.material), clean(a.style), s.winningModel || s.route || "",
        s.usesGlobal ? "Global pooled forecast" : s.usesYearlyPool ? "Prophet with pooled yearly seasonality" : "",
        pooled,
      ].map(esc).join(","));
    });
    const csv = "﻿" + lines.join("\r\n");   // BOM → Excel reads it as UTF-8
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const el = document.createElement("a");
    el.href = url; el.download = `logitrack_grouping_${new Date().toISOString().slice(0, 10)}.csv`; el.click();
    URL.revokeObjectURL(url);
  }

  // Ask the AI for a short, tailored sentence per behavioural group — grounded in that
  // group's products + seasonal signature, so each card reads differently (not a template).
  async function explainGroups(g) {
    if (!apiKey || !g?.skus) return;
    const byKey = {};
    g.skus.forEach(s => {
      if (s.clusterBasis || !Array.isArray(s.cluster) || !s.cluster.length) return;
      const k = [...s.cluster].sort().join("|");
      if (!byKey[k]) byKey[k] = { names: [], members: [], coh: s.clusterCohesion, cat: s.baseGroup || s.group || "products" };
      byKey[k].names.push(s.skuName);
      byKey[k].members.push(s);
    });
    // Only ask the AI about clusters we DON'T already have a saved note for. Notes are
    // persisted, so re-opening the Grouping tab (or a backend re-sync) won't re-run the
    // AI or "re-evaluate" — it just reuses what's cached. New/changed clusters still get one.
    const keys = Object.keys(byKey).filter(k => !groupNotes[k]);
    if (!keys.length) return;
    const lines = keys.map((k, i) => {
      const grp = byKey[k], coh = grp.coh || {}, sh = coh.shared || {};
      const title = behaviorGroupTitle(grp.members, grp.cat, i);
      const seas = [
        sh.highMonths?.length ? `peaks in ${sh.highMonths.join("/")}` : "",
        sh.lowMonths?.length ? `dips in ${sh.lowMonths.join("/")}` : "",
        sh.weekend ? `${sh.weekend} weekend bias` : "",
      ].filter(Boolean).join(", ") || "no standout seasonal peaks — mostly the generic retail rhythm";
      return `${i + 1} | group title "${title}" | category "${grp.cat}" | products: ${grp.names.join(", ")} | overall co-move ${Math.round((coh.avg || 0) * 100)}%, specific seasonal match ${Math.max(0, Math.round((coh.distinctive || 0) * 100))}% after removing the normal store-wide rhythm | seasonal signature: ${seas}`;
    }).join("\n");
    const prompt =
`For each product group below, write ONE short, specific sentence (max ~28 words, plain business English) describing the demand behavior a NEW product would inherit if it borrows this group's forecast. Ground it strictly in the given numbers and seasonal signature — do NOT invent figures. Make every sentence DIFFERENT and specific (name the seasonal behaviour when present; if specific seasonal match is low, say the borrowed behavior is mostly the normal store-wide rhythm). Avoid jargon like "correlation". Use the group title naturally when helpful. Do not mention group numbers, product counts, or backend mechanics.
Return ONLY a JSON object mapping each group number to its sentence. No prose, no code fences.

Groups:
${lines}`;
    try {
      const res = await fetch(GROQ_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${apiKey}` },
        body: JSON.stringify({ model: GROQ_MODEL, messages: [{ role: "user", content: prompt }], max_tokens: 900, temperature: 0.45 }),
      });
      const data = await res.json();
      const parsed = parseJsonLoose(data?.choices?.[0]?.message?.content);
      if (!parsed) return;
      const notes = {};
      keys.forEach((k, i) => { const t = parsed[i + 1] ?? parsed[String(i + 1)]; if (t) notes[k] = String(t); });
      setGroupNotes(prev => ({ ...prev, ...notes }));
    } catch { /* keep the templated fallback */ }
  }

  const chip = (k, v, onRemove) => (
    <span key={k} className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] mr-1 mb-1
      ${lm ? 'bg-sky-50 text-sky-700 border-sky-200' : 'bg-sky-950 text-sky-300 border-sky-800'}`}>
      <span className="opacity-60 mr-1">{k}:</span>{String(v)}
      {onRemove && (
        <button type="button" disabled={phase !== "idle"} onClick={(e) => { e.stopPropagation(); onRemove(); }}
          className={`ml-1.5 rounded px-1.5 py-0.5 leading-none select-none ${lm ? 'hover:bg-sky-100 text-sky-500 hover:text-sky-800' : 'hover:bg-sky-900 text-sky-500 hover:text-sky-200'} disabled:opacity-40`}
          aria-label={`Remove ${k} tag`}
          title={`Remove ${k} tag`}>
          <span aria-hidden="true">×</span>
        </button>
      )}
    </span>
  );

  const content = (
    <>
            <div className={`sticky top-0 z-10 flex items-center justify-between ${embedded ? 'px-6 py-6' : 'px-5 py-4'} border-b ${border} ${panelBg}`}>
              <div>
                <div className={`${embedded ? 'text-2xl' : 'text-sm'} font-bold ${textMain} tracking-tight`}>Product grouping</div>
                <div className={`${embedded ? 'text-[13.5px] mt-1' : 'text-[11px]'} ${textMute}`}>Categorize products, then see which ones behave alike — so new or sparse items can borrow a forecast from their siblings.</div>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {groups?.skus?.length > 0 && (
                  <button onClick={downloadGroupingCsv} title="Download grouping data as a CSV"
                    className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors ${lm ? 'border-slate-200 text-slate-600 hover:bg-slate-100' : 'border-white/10 text-slate-300 hover:bg-slate-800/60'}`}>
                    <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2" />
                    </svg>
                    Download CSV
                  </button>
                )}
                {!embedded && <button onClick={onClose} className={`text-lg px-2 ${lm ? 'text-slate-400 hover:text-slate-700' : 'text-slate-500 hover:text-white'}`}>×</button>}
              </div>
            </div>

            <div className={`${embedded ? 'px-6 py-5' : 'px-5 py-4'} space-y-4`}>
              {embedded && groups?.skus?.length > 0 && (() => {
                const deadIds = new Set((scorecardRows || []).filter(r => r.status === "Dead stock").map(r => r.skuId));
                const sks = groups.skus.filter(s => !deadIds.has(s.skuId));
                const cats = new Set(sks.map(s => s.group).filter(Boolean)).size;
                const bgroups = new Set(sks.filter(s => Array.isArray(s.cluster)).map(s => [...s.cluster].sort().join("|"))).size;
                const pooled = sks.filter(s => s.usesGlobal).length;
                const yearlyPooled = sks.filter(s => s.usesYearlyPool).length;
                const tile = (label, val, sub) => (
                  <div className={`rounded-2xl border ${cardBg} p-4`}>
                    <div className={`text-[11px] uppercase tracking-widest font-bold ${textMute}`}>{label}</div>
                    <div className={`text-3xl font-bold tabular-nums mt-2 ${textMain}`}>{val}</div>
                    {sub && <div className={`text-[11px] mt-1.5 ${textMute}`}>{sub}</div>}
                  </div>
                );
                return (
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    {tile("Products", sks.length)}
                    {tile("Categories", cats)}
                    {tile("Behaviour groups", bgroups, "shape-similar clusters")}
                    {tile("Pooled forecasts", pooled, "borrow from siblings")}
                    {tile("Yearly blends", yearlyPooled, "veterans borrowing annual shape")}
                  </div>
                );
              })()}
              <p className={`text-xs ${textBody} leading-relaxed`}>
                Paste anything that describes each product — title, description, spec sheet. The AI pulls out
                category, brand, size, color, etc. Products that share attributes are grouped, which is what
                lets new or sparse items borrow a forecast from their siblings.
              </p>

              <div className="flex flex-col lg:flex-row gap-5 items-start">
              <div className="w-full lg:w-[340px] lg:shrink-0 space-y-4">
              <div className="flex items-center gap-2">
                <span className={`text-lg font-bold tracking-tight ${textMain}`}>Describe your products</span>
                {skuList.length > 0 && <span className={`text-[10.5px] ml-auto ${textMute}`}>{skuList.filter(s => (info[s.id] || Object.keys(attrsForSku(s.id)).length)).length}/{skuList.length} done</span>}
              </div>
              {skuList.length > 6 && (
                <input value={q} onChange={e => setQ(e.target.value)} placeholder="Filter products…"
                  className={`${inputCls} !py-1.5`} />
              )}
              {/* Per-SKU info inputs — compact, scrollable so a big catalog doesn't run forever */}
              <div className={`rounded-xl border ${cardBg} divide-y ${lm ? 'divide-slate-200' : 'divide-white/10'} max-h-[58vh] overflow-y-auto sku-scroll`}>
                {skuList.length === 0 && <div className={`px-3 py-3 text-xs ${textMute}`}>No products loaded yet.</div>}
                {skuList.filter(s => !q.trim() || (s.name || s.id).toLowerCase().includes(q.trim().toLowerCase())).map(s => {
                  const displayAttrs = attrsForSku(s.id);
                  const hasTags = Object.keys(displayAttrs).length > 0;
                  const done = !!((info[s.id] || "").trim() || hasTags);
                  return (
                    <div key={s.id} className="px-3 py-2">
                      <div className="flex items-center gap-2 mb-1">
                        <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${done ? "bg-emerald-500" : (lm ? "bg-slate-300" : "bg-slate-600")}`} />
                        <span className={`text-[11px] font-semibold ${textMain} truncate`}>{s.name || s.id}</span>
                        {hasTags && (
                          <button type="button" onClick={() => clearTags(s.id)} disabled={phase !== "idle"}
                            className={`ml-auto text-[10px] ${lm ? 'text-slate-400 hover:text-rose-600' : 'text-slate-500 hover:text-rose-400'} disabled:opacity-50`}>
                            clear tags
                          </button>
                        )}
                      </div>
                      <textarea rows={1} className={`${inputCls} !py-1.5 resize-none`} placeholder="Paste title / specs…"
                        value={info[s.id] || ""} onChange={e => setInfo(v => ({ ...v, [s.id]: e.target.value }))} />
                      {hasTags && (
                        <div className="mt-1 flex flex-wrap">{Object.entries(displayAttrs).map(([k, v]) => chip(k, v, () => removeTag(s.id, k)))}</div>
                      )}
                    </div>
                  );
                })}
              </div>

              {err && <div className={`text-xs ${lm ? 'text-rose-600' : 'text-rose-400'}`}>{err}</div>}

              <button onClick={classifyAndApply} disabled={phase !== "idle"}
                className={`w-full rounded-lg py-2.5 text-xs font-semibold transition-colors disabled:opacity-60
                  ${lm ? 'bg-violet-600 text-white hover:bg-violet-700' : 'bg-violet-600 text-white hover:bg-violet-500'}`}>
                {phase === "classifying" ? "Classifying…" : phase === "applying" ? "Saving…" : "Classify and Apply Changes"}
              </button>
              </div>{/* end left column */}

              <div className="flex-1 min-w-0 space-y-4">
              <div className={`text-lg font-bold tracking-tight ${textMain}`}>Product groups</div>
              {/* Groups / similar products — bucketed by category, one section per group */}
              {groups?.skus && (() => {
                const deadIds = new Set((scorecardRows || []).filter(r => r.status === "Dead stock").map(r => r.skuId));
                const groupRows = groups.skus.filter(s => !deadIds.has(s.skuId));
                const cols = groups.groupColumns || [];
                const catOf = s => s.baseGroup || (cols.length ? cols.map(c => s.attributes?.[c]).filter(Boolean).join(" · ") : "") || "Uncategorized";
                const ckOf = s => Array.isArray(s.cluster) && s.cluster.length ? [...s.cluster].sort().join("|") : null;
                // A SKU with a clusterBasis was MATCHED into a group it isn't an established
                // member of → it's a new product. Everyone else is a veteran.
                const veterans  = groupRows.filter(s => !s.clusterBasis);
                const newcomers = groupRows.filter(s => s.clusterBasis);

                const vByCluster = {}; const looseVets = [];
                veterans.forEach(s => { const ck = ckOf(s); if (ck) (vByCluster[ck] ||= []).push(s); else looseVets.push(s); });
                // A newcomer only ACTUALLY borrows when it routed to the global pooled model,
                // which requires ≥ MIN_RELATIVES genuinely-similar veterans. A match to a
                // single-veteran cluster doesn't qualify (the engine falls back to the SKU's
                // own early sales) — so only usesGlobal newcomers render as "borrowing".
                const newByCluster = {}; const newOrphans = [];
                newcomers.forEach(s => {
                  const ck = ckOf(s);
                  if (ck && s.usesGlobal) (newByCluster[ck] ||= []).push(s);
                  else newOrphans.push(s);
                });
                const allClusterKeys = Object.keys(vByCluster).sort((a, b) => vByCluster[b].length - vByCluster[a].length);
                const clusterKeys = allClusterKeys.filter(ck => (vByCluster[ck]?.length || 0) >= 2);
                const NEW_DAYS = 60;
                const singletonVets = allClusterKeys
                  .filter(ck => (vByCluster[ck]?.length || 0) < 2)
                  .flatMap(ck => vByCluster[ck] || []);
                const standaloneVets = [
                  ...looseVets.filter(s => s.daysHistory == null || s.daysHistory >= NEW_DAYS),
                  ...singletonVets,
                ];
                looseVets.filter(s => s.daysHistory != null && s.daysHistory < NEW_DAYS).forEach(s => newOrphans.push(s));
                const byCategory = {};
                clusterKeys.forEach(ck => {
                  const members = vByCluster[ck];
                  const cat = catOf(members[0]);
                  (byCategory[cat] ||= { clusters: [], standalone: [], newOrphans: [] }).clusters.push(ck);
                });
                standaloneVets.forEach(s => {
                  const cat = catOf(s);
                  (byCategory[cat] ||= { clusters: [], standalone: [], newOrphans: [] }).standalone.push(s);
                });
                newOrphans.forEach(s => {
                  const cat = catOf(s);
                  (byCategory[cat] ||= { clusters: [], standalone: [], newOrphans: [] }).newOrphans.push(s);
                });
                const categoryKeys = Object.keys(byCategory).sort((a, b) => {
                  const na = byCategory[a].clusters.reduce((sum, ck) => sum + (vByCluster[ck]?.length || 0), 0) + byCategory[a].standalone.length + byCategory[a].newOrphans.length;
                  const nb = byCategory[b].clusters.reduce((sum, ck) => sum + (vByCluster[ck]?.length || 0), 0) + byCategory[b].standalone.length + byCategory[b].newOrphans.length;
                  return nb - na || a.localeCompare(b);
                });

                if (!veterans.length && !newcomers.length) {
                  return <div className={`text-xs ${textMute} px-1`}>No grouping signal yet — add categories on the left.</div>;
                }
                const pct = v => v == null ? null : Math.round(v * 100);
                const distPct = coh => coh?.distinctive == null ? null : Math.max(0, Math.round(coh.distinctive * 100));
                const cohColor = d => d == null ? textMute : d >= 0.6 ? (lm ? "text-emerald-600" : "text-emerald-400") : d >= 0.3 ? (lm ? "text-amber-600" : "text-amber-400") : (lm ? "text-rose-600" : "text-rose-400");
                const linkPct = link => link?.score == null ? null : Math.round(link.score * 100);
                const thresholdPct = link => Math.round((link?.threshold ?? 0.4) * 100);
                const cleanGroupNote = t => String(t || "").replace(/^Group\s+\d+\s+products?\s+/i, "Products ").replace(/^Group\s+\d+\s+/i, "These products ");
                const inheritedGroupNote = t => {
                  const clean = cleanGroupNote(t);
                  if (!clean) return "";
                  if (/\b(new|borrow|borrowing|inherits?|launch)\b/i.test(clean)) return clean;
                  return `A new product borrowing this forecast inherits this pattern: ${clean}`;
                };
                const monthsList = a => !a?.length ? "" : a.length === 1 ? a[0] : a.slice(0, -1).join(", ") + " & " + a[a.length - 1];
                const whyText = (coh) => {
                  const d = coh.distinctive ?? 0, sh = coh.shared || {};
                  const feats = [];
                  if (sh.highMonths?.length) feats.push(`both sell noticeably more in ${monthsList(sh.highMonths)}`);
                  if (sh.lowMonths?.length) feats.push(`${sh.highMonths?.length ? "and ease off" : "both ease off"} in ${monthsList(sh.lowMonths)}`);
                  if (sh.weekend === "high") feats.push("with a sharper weekend lift than most");
                  if (sh.weekend === "low") feats.push("with a flatter weekend than most");
                  const why = feats.join(" ");
                  if (d >= 0.6) return `Strong specific match. These products share a seasonal shape that stands out from the normal store-wide rhythm${why ? ` — ${why}` : ""}. A new product borrowing this forecast inherits that specific shape.`;
                  if (d >= 0.3) return `Moderate specific match. A new product borrowing this forecast inherits part of this group's own seasonal shape${why ? ` (${why})` : ""}, layered on top of the normal store-wide rhythm.`;
                  return `Most of the ${pct(coh.avg)}% overlap is the normal store-wide rhythm — weekends, holidays, or broad seasonal lifts. A new product borrowing this forecast mostly inherits that broad rhythm${why ? `, with only a faint shared tilt (${why})` : ""}.`;
                };
                const vetRow = s => {
                  const displayAttrs = attrsForSku(s.skuId, s.attributes);
                  const gi = s.groupInfo || {};
                  return (
                    <div key={s.skuId} className={`rounded-lg border ${cardBg} px-2.5 py-1.5`}>
                      <div className="flex items-center gap-1.5 min-w-0">
                        <span className={`text-xs font-semibold ${textMain} min-w-0 truncate`}>{s.skuName}</span>
                        {s.usesYearlyPool && (
                          <span className={`shrink-0 text-[9px] font-bold rounded-full px-1.5 py-0.5 ${lm ? 'bg-sky-100 text-sky-700 border border-sky-200' : 'bg-sky-950/40 text-sky-300 border border-sky-800/50'}`}
                            title={`This product has enough history for Prophet, but its yearly seasonality is still blended ${Math.round((s.yearlyPoolWeight || 0) * 100)}% from related products.`}>
                            yearly pool
                          </span>
                        )}
                      </div>
                      {s.usesYearlyPool && (
                        <div className={`text-[10px] ${lm ? 'text-sky-700' : 'text-sky-300'} mt-0.5`}>
                          Yearly seasonality blended {Math.round((s.yearlyPoolWeight || 0) * 100)}% from {s.yearlyPoolSize || "related"} product{s.yearlyPoolSize === 1 ? "" : "s"}
                        </div>
                      )}
                      {gi.level === "subgroup" && (
                        <div className={`text-[10px] ${lm ? 'text-emerald-700' : 'text-emerald-400'} mt-0.5`}>
                          Behavior subgroup: {(gi.splitCols || []).join(" + ")} · {gi.usableMembers} eligible matches
                        </div>
                      )}
                      {gi.level === "base" && gi.reason && (
                        <div className={`text-[10px] ${textMute} mt-0.5`}>Broad group used · {gi.reason}</div>
                      )}
                      {Object.keys(displayAttrs).length > 0 && (
                        <div className="mt-0.5 flex flex-wrap">
                          {Object.entries(displayAttrs).map(([k, v]) => chip(k, v, () => removeTag(s.skuId, k)))}
                        </div>
                      )}
                    </div>
                  );
                };
                return (
                  <div className="space-y-6">
                    {deadIds.size > 0 && (
                      <div className={`text-[11px] rounded-xl border px-3 py-2 ${lm ? 'bg-slate-50 border-slate-200 text-slate-500' : 'bg-slate-900/40 border-slate-700/40 text-slate-500'}`}>
                        {deadIds.size} dead-stock product{deadIds.size !== 1 ? "s are" : " is"} excluded from grouping and pooled forecasts.
                      </div>
                    )}

                    {categoryKeys.map(cat => {
                      const entry = byCategory[cat];
                      const productCount = entry.clusters.reduce((sum, ck) => sum + (vByCluster[ck]?.length || 0) + (newByCluster[ck]?.length || 0), 0) + entry.standalone.length + entry.newOrphans.length;
                      return (
                        <div key={cat} className={`rounded-2xl border ${cardBg} overflow-hidden`}>
                          <div className={`px-4 py-3 border-b ${border} flex items-center gap-2`}>
                            <span className={`text-sm font-bold ${textMain}`}>{cat}</span>
                            <span className={`text-[10px] font-mono rounded px-1.5 py-0.5 ${lm ? 'bg-slate-100 text-slate-500' : 'bg-white/5 text-slate-400'}`}>{productCount}</span>
                            {entry.clusters.length > 1 && <span className={`ml-auto text-[11px] ${textMute}`}>{entry.clusters.length} behavior groups</span>}
                          </div>
                          <div className="p-4 space-y-4">
                            {entry.clusters.map((ck, idx) => {
                              const members = vByCluster[ck];
                              const borrowers = newByCluster[ck] || [];
                              const coh = members[0]?.clusterCohesion ?? null;
                              const poolInfo = members[0]?.groupInfo || {};
                              const groupTitle = behaviorGroupTitle(members, cat, idx);
                              const collapsed = !!openGroups[ck];
                              return (
                                <div key={ck} className={`rounded-xl border ${lm ? 'border-slate-200 bg-slate-50/40' : 'border-white/10 bg-black/10'} overflow-hidden`}>
                                  <button onClick={() => setOpenGroups(o => ({ ...o, [ck]: !o[ck] }))}
                                    className={`w-full flex items-center gap-2 px-3 py-2.5 text-left transition-colors ${lm ? 'hover:bg-slate-50' : 'hover:bg-white/5'}`}>
                                    <svg className={`h-3 w-3 shrink-0 transition-transform ${collapsed ? '' : 'rotate-90'} ${textMute}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" /></svg>
                                    <span className={`text-xs font-bold ${textMain}`}>{groupTitle}</span>
                                    <span className={`text-[10px] ${textMute}`}>{members.length} established match{members.length !== 1 ? "es" : ""}</span>
                                    {poolInfo.level === "subgroup" && <span className={`text-[10px] ${lm ? 'text-emerald-700' : 'text-emerald-400'}`}>{(poolInfo.splitCols || []).join(" + ")}</span>}
                                    {coh?.distinctive != null && <span className={`text-[11px] font-bold ml-auto ${cohColor(coh.distinctive)}`} title="How much this group's shared seasonality stands out from the normal store-wide rhythm">{distPct(coh)}% specific</span>}
                                  </button>
                                  {!collapsed && (
                                    <div className={`px-3 pb-3 pt-2 border-t ${border}`}>
                                      <div className={borrowers.length ? "grid sm:grid-cols-2 gap-4" : ""}>
                                        <div className="space-y-1.5 min-w-0">
                                          <div className={`text-[10px] font-bold uppercase tracking-wide ${textMute}`}>Established matching products</div>
                                          {members.filter(s => !s.usesYearlyPool).map(vetRow)}
                                          {members.some(s => s.usesYearlyPool) && (
                                            <div className="space-y-1.5 pt-2">
                                              <div className={`text-[10px] font-bold uppercase tracking-wide ${lm ? 'text-sky-700' : 'text-sky-300'}`}>Established — borrowing yearly shape</div>
                                              {members.filter(s => s.usesYearlyPool).map(vetRow)}
                                            </div>
                                          )}
                                        </div>
                                        {borrowers.length > 0 && (
                                          <div className="space-y-1.5 min-w-0">
                                            <div className={`text-[10px] font-bold uppercase tracking-wide ${lm ? 'text-violet-700' : 'text-violet-300'}`}>New — borrowing this</div>
                                            {borrowers.map(s => (
                                              <div key={s.skuId} className={`rounded-lg px-2.5 py-1.5 ${lm ? 'bg-violet-50 border border-violet-100' : 'bg-violet-950/20 border border-violet-900/30'}`}>
                                                <span className={`text-xs font-semibold ${textMain}`}>{s.skuName}</span>
                                                {s.clusterBasis && <div className={`text-[10px] ${textMute} mt-0.5`}>↳ {s.clusterBasis}</div>}
                                                {s.groupInfo?.reason && <div className={`text-[10px] ${textMute} mt-0.5`}>{s.groupInfo.reason}</div>}
                                              </div>
                                            ))}
                                          </div>
                                        )}
                                      </div>
                                      {coh && (
                                        <div className={`mt-3 pt-3 border-t ${border} space-y-2`}>
                                          <div className="flex items-center justify-between text-[11px]">
                                            <span className={textMute}>Overall move-together</span>
                                            <span className={`font-bold ${textMain}`}>{pct(coh.avg)}%</span>
                                          </div>
                                          <div className="flex items-center justify-between text-[11px] gap-3">
                                            <span className={textMute}>Specific seasonal match <span className="opacity-60">· beyond store-wide rhythm</span></span>
                                            <span className={`font-bold shrink-0 ${cohColor(coh.distinctive)}`}>{distPct(coh) ?? "—"}%</span>
                                          </div>
                                          <p className={`text-[10px] ${textMute} leading-relaxed`}>
                                            This asks whether the shared seasonality is special to this group, not just the normal store rhythm like weekends, holidays, or broad summer/winter lifts.
                                          </p>
                                          {coh.min != null && coh.min < coh.avg - 0.05 && (
                                            <div className="flex items-center justify-between text-[11px]">
                                              <span className={textMute}>Weakest pair</span>
                                              <span className={`font-bold ${cohColor(coh.min)}`}>{pct(coh.min)}%</span>
                                            </div>
                                          )}
                                          <p className={`text-[10.5px] ${textMute} leading-relaxed pt-1.5`}>{inheritedGroupNote(groupNotes[ck]) || whyText(coh)}</p>
                                        </div>
                                      )}
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                            {entry.standalone.length > 0 && (
                              <div className={`rounded-xl border px-3 py-2.5 ${lm ? 'bg-amber-50 border-amber-200' : 'bg-amber-950/10 border-amber-900/30'}`}>
                                <div className={`text-[10px] font-bold uppercase tracking-wide ${lm ? 'text-amber-700' : 'text-amber-300'}`}>Same category, different behavior</div>
                                <div className="grid sm:grid-cols-2 gap-2 mt-2">
                                  {entry.standalone.map(s => {
                                    const poolLink = s.nearestPoolLink && !deadIds.has(s.nearestPoolLink.skuId) ? s.nearestPoolLink : null;
                                    const catLink = s.nearestCategoryLink && !deadIds.has(s.nearestCategoryLink.skuId) ? s.nearestCategoryLink : null;
                                    return (
                                      <div key={s.skuId} className={`rounded-lg border ${cardBg} px-2.5 py-1.5`}>
                                        <span className={`text-xs font-semibold ${textMain}`}>{s.skuName}</span>
                                        <div className={`text-[10px] ${textMute} mt-0.5`}>
                                          {s.route === "global"
                                            ? "This product needed a borrowed forecast, but no reliable multi-product veteran pool was available."
                                            : s.usesYearlyPool
                                            ? "This product forecasts directly with Prophet, but still borrows yearly seasonality from related products until it has enough annual history."
                                            : "This product has enough of its own history to forecast directly, so it is shown here but does not need to borrow a pooled forecast."}
                                        </div>
                                        {poolLink ? (
                                          <div className={`mt-1.5 text-[10px] rounded-md px-2 py-1 ${lm ? 'bg-white border border-amber-200 text-amber-800' : 'bg-amber-950/20 border border-amber-900/30 text-amber-300'}`}>
                                            Highest link to the pooled group: <span className="font-bold">{linkPct(poolLink)}%</span>
                                            {" "}with {poolLink.skuName}
                                            {poolLink.score >= (poolLink.threshold ?? 0.4) ? " · strong match, but not enough products share the reliable subgroup." : " · needs "}
                                            {poolLink.score < (poolLink.threshold ?? 0.4) && <span className="font-bold">{thresholdPct(poolLink)}%</span>}
                                            {poolLink.score < (poolLink.threshold ?? 0.4) && " to join."}
                                          </div>
                                        ) : catLink ? (
                                          <div className={`mt-1.5 text-[10px] rounded-md px-2 py-1 ${lm ? 'bg-white border border-amber-200 text-amber-800' : 'bg-amber-950/20 border border-amber-900/30 text-amber-300'}`}>
                                            Closest same-category match: <span className="font-bold">{linkPct(catLink)}%</span>
                                            {" "}with {catLink.skuName}
                                            {catLink.score >= (catLink.threshold ?? 0.4) ? " · strong match, but it still needs a reliable multi-product subgroup to form a pool." : " · needs "}
                                            {catLink.score < (catLink.threshold ?? 0.4) && <span className="font-bold">{thresholdPct(catLink)}%</span>}
                                            {catLink.score < (catLink.threshold ?? 0.4) && " to form a pool."}
                                          </div>
                                        ) : (
                                          <div className={`mt-1.5 text-[10px] ${textMute}`}>No eligible non-dead same-category product has a reliable seasonal fingerprint to score against.</div>
                                        )}
                                      </div>
                                    );
                                  })}
                                </div>
                              </div>
                            )}
                            {entry.newOrphans.length > 0 && (
                              <div className={`rounded-xl border px-3 py-2.5 ${lm ? 'bg-violet-50 border-violet-100' : 'bg-violet-950/10 border-violet-900/30'}`}>
                                <div className={`text-[10px] font-bold uppercase tracking-wide ${lm ? 'text-violet-700' : 'text-violet-300'}`}>Not included in pooled forecast</div>
                                <div className="grid sm:grid-cols-2 gap-2 mt-2">
                                  {entry.newOrphans.map(s => (
                                    <div key={s.skuId} className={`rounded-lg border ${cardBg} px-2.5 py-1.5`}>
                                      <span className={`text-xs font-semibold ${textMain}`}>{s.skuName}</span>
                                      <div className={`text-[10px] ${textMute} mt-0.5`}>{s.clusterBasis
                                        ? "It was matched to a category peer set, but that set did not produce enough pooled veterans under the current rules, so it uses its own early sales."
                                        : "Too little own history to infer seasonality, and no eligible multi-product veteran pool was found yet, so it uses its own early sales."}</div>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                );
              })()}
              {!groups?.skus?.length && <div className={`text-xs ${textMute} px-1`}>Paste product info on the left and hit "Classify and Apply Changes" to see groups here.</div>}
              </div>{/* end right column */}
              </div>{/* end grid */}
            </div>
    </>
  );

  if (embedded) {
    return (
      <div className={`flex-1 overflow-y-auto w-full ${panelBg}`}>
        <div className="w-full max-w-[1700px] mx-auto pb-12">{content}</div>
      </div>
    );
  }
  return (
    <>
      {!open ? null : (
        <div className="fixed inset-0 z-50">
          <div className="absolute inset-0 bg-black/40 backdrop-blur-[1px]" onClick={onClose} />
          <aside className={`absolute left-0 top-0 h-full w-full max-w-lg overflow-y-auto border-r ${border} ${panelBg} shadow-2xl`}>
            {content}
          </aside>
        </div>
      )}
    </>
  );
}

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
import { todayStr } from './lib/helpers';
import { GROQ_URL, GROQ_MODEL, GROQ_LOW_REASONING } from './lib/ai';
import { terminal, fs, MONO, SANS, scrim } from './lib/theme';

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
  /* A detail already said by the family title is not a detail. "Freestanding
     Vanities" split on `mounting` was rendering as "Freestanding Vanities -
     Freestanding", which reads like a bug because it is one. */
  const familyLower = familyTitle.toLowerCase();
  const detailValues = detailKeys
    .map(key => titleCaseLoose(commonAttrValue(members, key)))
    .filter(Boolean)
    .filter(v => !familyLower.includes(v.toLowerCase()));
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
  /* The describe form is a MODE, not a column. It is where you go to teach the tool
     about your catalogue; the groups are what you come back to look at. Sharing a row
     meant the thing you read every day got 60% of a screen and the thing you touch
     once got 340 fixed pixels of it permanently. */
  const [showDescribe, setShowDescribe] = useState(false);
  const [groupNotes, setGroupNotes] = useState(() => loadStorage(NOTES_STORAGE_KEY, {})); // clusterKey → AI-written one-liner (persisted)
  const T = terminal(lm);
  const mono  = { fontFamily: MONO, fontVariantNumeric: "tabular-nums" };
  const cap   = { fontSize: fs.cap, letterSpacing: ".07em", textTransform: "uppercase", color: T.dim, fontWeight: 600 };
  const panel = { background: T.panel, border: `2px solid ${T.line}`, boxShadow: T.shadow };
  const inp   = { width: "100%", background: T.bg, border: `2px solid ${T.line2}`, borderRadius: 2,
                  padding: "8px 10px", fontSize: fs.body, color: T.ink, outline: "none",
                  fontFamily: SANS, boxSizing: "border-box" };
  const btn   = { fontFamily: MONO, fontSize: fs.body, fontWeight: 500, padding: "8px 13px", borderRadius: 2,
                  background: "transparent", color: T.soft, border: `2px solid ${T.line2}`,
                  whiteSpace: "nowrap", cursor: "pointer" };
  const btnSolid = { ...btn, background: T.btnBg, color: T.btnFg, border: "2px solid transparent", fontWeight: 600 };

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
        // Put back as AI tags (fill-in only): they came from an earlier classification,
        // and must not override a category the file or store gives.
        if (Object.keys(missing).length) { try { await saveAttributes(missing, "ai"); } catch {} }
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
  // Waits for the server's groups: it used to run on mount, before they arrived, when
  // every product looked uncategorised, so it classified the whole catalogue, including
  // products whose file or store already gave a Category.
  useEffect(() => {
    if (!(open || embedded)) return;
    if (!apiKey || phase !== "idle" || !skuList.length || !groups) return;
    const seen = loadStorage(AUTOCLASSIFIED_KEY, {});
    const serverCat = (id) => (groups.skus || []).find(s => s.skuId === id)?.attributes?.category;
    const neverClassified = skuList.filter(s => !(classes[s.id] && classes[s.id].category)
                                                && !serverCat(s.id) && !seen[s.id]);
    if (neverClassified.length === 0) return;
    // Mark every current SKU as seen up front (persisted synchronously) so a re-render
    // mid-classification can't trigger a second pass.
    const nextSeen = { ...seen };
    skuList.forEach(s => { nextSeen[s.id] = true; });
    saveStorage(AUTOCLASSIFIED_KEY, nextSeen);
    classifyAndApply();
  }, [open, embedded, apiKey, skuList, phase, classes, groups]);

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

  // source "ai": the server only fills empty attributes (or ones the AI set before) and
  // doesn't record them as set by hand, so the file's own Category is never replaced.
  async function saveAttributes(skus, source = "user") {
    let res;
    try {
      res = await fetch(`${api}/api/attributes`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ skus, source }),
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
        body: JSON.stringify({ model: GROQ_MODEL, messages: [{ role: "user", content: buildClassificationPrompt() }], max_tokens: 2400, temperature: 0.1, ...GROQ_LOW_REASONING }),
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
      setErr("Couldn't reach the AI to read your product descriptions. Check your AI key and internet connection.");
      setPhase("idle");
      return;
    }
    // Step 2 — backend: save the tags and re-group the catalog. A failure here is the
    // local backend, NOT the AI — so say so (the tags were generated fine).
    try {
      setPhase("applying");
      await saveAttributes(next, "ai");
    } catch (e) {
      if (e?.kind === "http") {
        setErr(`Tags were generated, but the backend rejected the save (HTTP ${e.status}). Check the LogiTrack terminal window for the error.`);
      } else {
        setErr(`Tags were generated, but the backend at ${api} didn't respond, so the server isn't running there. Open ${api}/api/health in a tab. If it doesn't load, relaunch with "Start LogiTrack.command" and keep that window open.`);
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
      setErr("Couldn't remove that tag. Is the backend running?");
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
      setErr("Couldn't remove those tags. Is the backend running?");
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
    el.href = url; el.download = `logitrack_grouping_${todayStr()}.csv`; el.click();
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
Return ONLY a JSON object mapping each group number to its sentence. No prose, no code fences. Never use em dashes in the sentences.

Groups:
${lines}`;
    try {
      const res = await fetch(GROQ_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${apiKey}` },
        body: JSON.stringify({ model: GROQ_MODEL, messages: [{ role: "user", content: prompt }], max_tokens: 900, temperature: 0.45, ...GROQ_LOW_REASONING }),
      });
      const data = await res.json();
      const parsed = parseJsonLoose(data?.choices?.[0]?.message?.content);
      if (!parsed) return;
      const notes = {};
      keys.forEach((k, i) => { const t = parsed[i + 1] ?? parsed[String(i + 1)]; if (t) notes[k] = String(t); });
      setGroupNotes(prev => ({ ...prev, ...notes }));
    } catch { /* keep the templated fallback */ }
  }

  /* An extracted attribute. `key:` is set small and dim because you scan the VALUE —
     "Vanities", "Brushed Nickel" — and the key is only there to disambiguate. */
  const chip = (k, v, onRemove) => (
    <span key={k} style={{ display:"inline-flex", alignItems:"center", gap:5, marginRight:5, marginBottom:5,
      padding:"3px 8px", borderRadius:2, fontSize:fs.small, background:T.blueBg, color:T.blueFg,
      border:`2px solid ${T.blue}33` }}>
      <span style={{ ...mono, fontSize:fs.tick, opacity:.65 }}>{k}</span>
      <span style={{ fontWeight:500 }}>{String(v)}</span>
      {onRemove && (
        <button type="button" disabled={phase !== "idle"} onClick={(e) => { e.stopPropagation(); onRemove(); }}
          aria-label={`Remove ${k} tag`} title={`Remove ${k} tag`}
          style={{ background:"transparent", border:"none", color:T.blueFg, cursor:"pointer",
            padding:"0 0 0 2px", fontSize:fs.small, opacity:.7, lineHeight:1 }}>
          <span aria-hidden="true">×</span>
        </button>
      )}
    </span>
  );

  const content = (
    <>
      <style>{`.cat-grp:hover{background:${T.sunken};} .cat-sku:hover{background:${T.sunken};}`}</style>

      {/* ── header ─────────────────────────────────────────────────────────── */}
      <div style={{ position:"sticky", top:0, zIndex:10, display:"flex", alignItems:"flex-start",
        justifyContent:"space-between", gap:16, padding: embedded ? "20px 24px 16px" : "16px 18px",
        borderBottom:`2px solid ${T.line}`, background:T.bg }}>
        <div>
          <div style={{ fontSize: embedded ? 26 : 18, fontWeight:700, letterSpacing:"-.03em", color:T.ink }}>Product grouping</div>
          <div style={{ fontSize: embedded ? 14 : fs.body, color:T.soft, marginTop:5, maxWidth:720, lineHeight:1.55 }}>
            Which products sell alike, so a new or short-history product can borrow a forecast from similar ones.
          </div>
        </div>
        <div style={{ display:"flex", alignItems:"center", gap:9, flexShrink:0 }}>
          <button onClick={() => setShowDescribe(true)} style={btnSolid}
            title="Paste a title or spec sheet for each product, then classify them into groups">
            Describe products
          </button>
          {groups?.skus?.length > 0 && (
            <button onClick={downloadGroupingCsv} title="Download grouping data as a CSV" style={btn}>Download CSV</button>
          )}
          {!embedded && (
            <button onClick={onClose} title="Close"
              style={{ ...btn, border:"none", fontSize:19.5, padding:"2px 8px", color:T.faint }}>×</button>
          )}
        </div>
      </div>

      <div style={{ padding: embedded ? "16px 24px 24px" : "14px 18px", display:"flex", flexDirection:"column", gap:14 }}>

        {/* ── figures ──────────────────────────────────────────────────────── */}
        {embedded && groups?.skus?.length > 0 && (() => {
          const deadIds = new Set((scorecardRows || []).filter(r => r.status === "Dead stock").map(r => r.skuId));
          const sks = groups.skus.filter(s => !deadIds.has(s.skuId));
          const cats = new Set(sks.map(s => s.group).filter(Boolean)).size;
          const bgroups = new Set(sks.filter(s => Array.isArray(s.cluster)).map(s => [...s.cluster].sort().join("|"))).size;
          const pooled = sks.filter(s => s.usesGlobal).length;
          const yearlyPooled = sks.filter(s => s.usesYearlyPool).length;
          /* Five equal columns, centred, instead of five blocks shoved against the
             left edge of a full-width strip. Dividers sit BETWEEN cells, so the row
             does not end on a rule with nothing after it. */
          const fig = (label, val, note, color, i = 0) => (
            <div key={label} style={{ textAlign:"center", padding:"0 16px",
              borderLeft: i === 0 ? "none" : `2px solid ${T.line}` }}>
              <div style={{ ...cap, display:"flex", justifyContent:"center" }}>{label}</div>
              <div style={{ ...mono, fontSize:30, fontWeight:500, letterSpacing:"-.03em", lineHeight:1, marginTop:7, color: color || T.ink }}>{val}</div>
              {note && <div style={{ fontSize:fs.small, color:T.soft, marginTop:6, lineHeight:1.45 }}>{note}</div>}
            </div>
          );
          return (
            <div style={{ ...panel, padding:"16px 18px", display:"grid",
              gridTemplateColumns:"repeat(5, minmax(0,1fr))", alignItems:"start" }}>
              {fig("Products", sks.length, "being grouped", undefined, 0)}
              {fig("Categories", cats, "product families", undefined, 1)}
              {fig("Behaviour groups", bgroups, "shape-similar clusters", undefined, 2)}
              {fig("Pooled forecasts", pooled, "borrow from siblings", pooled ? T.amber : undefined, 3)}
              {fig("Yearly blends", yearlyPooled, "established products borrowing yearly shape", yearlyPooled ? T.blue : undefined, 4)}
            </div>
          );
        })()}

        {/* ── describe your products · a modal, not a column ──────────────── */}
        {/* Same backdrop as Costs & fees: blurred, so the page behind is visibly
            still there but has nothing left to read. Typing a spec sheet for forty
            products is a job you sit down to do, and it deserves the screen while you
            are doing it — not 340 fixed pixels beside the thing it produces. */}
        {showDescribe && (
          <div style={{ position:"fixed", inset:0, zIndex:70, fontFamily:SANS }}>
            <div onClick={() => setShowDescribe(false)} style={scrim(lm)} />
            <div style={{ position:"absolute", top:"5vh", left:"50%", transform:"translateX(-50%)",
              width:"min(820px, 94vw)", maxHeight:"90vh", display:"flex", flexDirection:"column",
              background:T.bg, color:T.ink, border:`2px solid ${T.line2}`,
              boxShadow:"0 24px 60px rgba(0,0,0,.5)" }}>

              <div style={{ padding:"18px 20px 14px", borderBottom:`2px solid ${T.line}`,
                display:"flex", alignItems:"flex-start", justifyContent:"space-between", gap:16 }}>
                <div>
                  <div style={{ fontSize:22, fontWeight:700, letterSpacing:"-.02em" }}>Describe your products</div>
                  {skuList.length > 0 && (
                    <div style={{ ...mono, fontSize:fs.small, color:T.soft, marginTop:5 }}>
                      {skuList.filter(s => (info[s.id] || Object.keys(attrsForSku(s.id)).length)).length}/{skuList.length} described
                    </div>
                  )}
                </div>
                <button onClick={() => setShowDescribe(false)} style={{ ...mono, background:"transparent",
                  border:"none", color:T.soft, fontSize:20, cursor:"pointer", padding:"0 4px", lineHeight:1 }}>×</button>
              </div>

              <div style={{ padding:"14px 20px 18px", overflowY:"auto",
                display:"flex", flexDirection:"column", gap:10 }}>
            <div style={{ fontSize:fs.body, color:T.soft, lineHeight:1.6 }}>
              Paste anything that describes each product: a title, a spec sheet, a line from your catalogue.
              Products that share attributes get grouped.
            </div>

            {skuList.length > 6 && (
              <input value={q} onChange={e => setQ(e.target.value)} placeholder="Filter products…" style={inp} />
            )}

            {/* Per-SKU info inputs — scrollable so a big catalog doesn't run forever */}
            {/* No maxHeight here any more. Inside a modal that already scrolls, a
                58vh inner scroller meant two nested scrollbars and a last row cut in
                half — which reads as a rendering fault, not as "there is more below".
                The modal body is the only thing that scrolls now. */}
            <div style={{ ...panel }} className="sku-scroll">
              {skuList.length === 0 && <div style={{ padding:"14px 12px", fontSize:fs.body, color:T.soft }}>No products loaded yet.</div>}
              {skuList.filter(s => !q.trim() || (s.name || s.id).toLowerCase().includes(q.trim().toLowerCase())).map((s, i) => {
                const displayAttrs = attrsForSku(s.id);
                const hasTags = Object.keys(displayAttrs).length > 0;
                const done = !!((info[s.id] || "").trim() || hasTags);
                return (
                  <div key={s.id} style={{ padding:"10px 12px", borderTop: i > 0 ? `2px solid ${T.line}` : "none" }}>
                    <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:6 }}>
                      <span title={done ? "Described or tagged" : "Nothing yet"}
                        style={{ width:8, height:8, flexShrink:0, borderRadius:"50%", background: done ? T.green : T.faint }} />
                      <span style={{ fontSize:fs.row, fontWeight:600, color:T.ink, minWidth:0,
                        whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>{s.name || s.id}</span>
                      {hasTags && (
                        <button type="button" onClick={() => clearTags(s.id)} disabled={phase !== "idle"}
                          style={{ marginLeft:"auto", flexShrink:0, background:"transparent", border:"none",
                            fontSize:fs.small, color:T.faint, cursor:"pointer" }}>clear tags</button>
                      )}
                    </div>
                    <textarea rows={1} placeholder="Paste title / specs…" style={{ ...inp, resize:"none", padding:"6px 9px" }}
                      value={info[s.id] || ""} onChange={e => setInfo(v => ({ ...v, [s.id]: e.target.value }))} />
                    {hasTags && (
                      <div style={{ marginTop:6, display:"flex", flexWrap:"wrap" }}>
                        {Object.entries(displayAttrs).map(([k, v]) => chip(k, v, () => removeTag(s.id, k)))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

              </div>

              {/* Pinned footer. The action should not be something you scroll forty
                  products to reach. */}
              <div style={{ padding:"14px 20px 16px", borderTop:`2px solid ${T.line}`,
                display:"flex", flexDirection:"column", gap:10, flexShrink:0 }}>
                {err && <div style={{ fontSize:fs.body, color:T.red }}>{err}</div>}
                <button onClick={classifyAndApply} disabled={phase !== "idle"}
                  style={{ ...btnSolid, width:"100%", textAlign:"center", padding:"13px 15px", fontSize:fs.num,
                    opacity: phase !== "idle" ? .6 : 1 }}>
                  {phase === "classifying" ? "Classifying…" : phase === "applying" ? "Saving…" : "Classify and apply changes"}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ── the groups · the whole page ─────────────────────────────────── */}
        <div style={{ minWidth:0, display:"flex", flexDirection:"column", gap:12 }}>

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
                return <div style={{ fontSize:fs.body, color:T.soft }}>No grouping signal yet. Click Describe products to add categories.</div>;
              }
              const pct = v => v == null ? null : Math.round(v * 100);
              const distPct = coh => coh?.distinctive == null ? null : Math.max(0, Math.round(coh.distinctive * 100));
              const cohColor = d => d == null ? T.faint : d >= 0.6 ? T.green : d >= 0.3 ? T.over : T.red;
              const cohWord  = d => d == null ? "-" : d >= 0.6 ? "strong" : d >= 0.3 ? "moderate" : "weak";
              const linkPct = link => link?.score == null ? null : Math.round(link.score * 100);
              /* Fallbacks mirror router.DISTINCT_THRESHOLD. Scores and thresholds are both
                 the "specific to this group" number now — the one measured after the
                 store-wide rhythm is removed — so a score and the bar beside it are finally
                 on the same scale. */
              const thresholdPct = link => Math.round((link?.threshold ?? 0.35) * 100);

              /* WHY THIS PRODUCT ISN'T IN A POOL — five different facts, not one label.
               *
               * This whole bucket used to be headed "Same category, different behaviour"
               * and every card in it read "nothing to score against". For a category
               * holding ONE product that is simply false twice over: it has no behaviour
               * different from anything, because there is nothing else in the category,
               * and it wasn't scored against something and found wanting — it was never
               * scored at all. The five cases below are genuinely different situations
               * with different answers ("stock it and wait" vs "nothing to do here"), so
               * they get different words.
               *
               *   lone      the only product in its category — nothing to compare with
               *   dead      its only comparable peer is dead stock, so it was dropped
               *   unscored  peers exist, but none shares enough history to correlate
               *   near      it DOES match one, but no reliable group formed around it
               *   differs   it was scored against every peer and matches none
               *
               * `peers` counts the other products shown on this category card, which is
               * what the reader can actually see and count.
               */
              const standaloneCase = (s, peers) => {
                const poolLink = s.nearestPoolLink && !deadIds.has(s.nearestPoolLink.skuId) ? s.nearestPoolLink : null;
                const catLink  = s.nearestCategoryLink && !deadIds.has(s.nearestCategoryLink.skuId) ? s.nearestCategoryLink : null;
                const link     = poolLink || catLink;
                const rawLink  = s.nearestPoolLink || s.nearestCategoryLink;
                if (link) {
                  const strong = link.score >= (link.threshold ?? 0.35);
                  return { kind: strong ? "near" : "differs", link, poolLink };
                }
                /* `dead` is tested BEFORE `lone`, and the order is the whole point. Dead
                   stock is filtered out of this view, so a two-product category whose other
                   half is dead arrives here looking like a category of one. Calling that
                   "the only product in this category" contradicts what the user sees in
                   their own data, and hides the actual reason — which is fixable, since
                   the peer comes back the moment it sells again. */
                if (rawLink)     return { kind: "dead", link: null, poolLink: null };
                if (peers <= 0)  return { kind: "lone", link: null, poolLink: null };
                return { kind: "unscored", link: null, poolLink: null, peers };
              };
              /* The heading describes what the cards under it actually have in common.
                 "Different behaviour" is only true of products that were compared. */
              const standaloneHeading = cases => {
                if (!cases.length) return null;
                const kinds = new Set(cases.map(c => c.kind));
                if (kinds.size === 1) {
                  const only = [...kinds][0];
                  if (only === "lone")     return { text: cases.length === 1 ? "Only product in this category" : "Alone in their categories", tone: "quiet" };
                  if (only === "dead")     return { text: "Only comparable product is dead stock", tone: "warn" };
                  if (only === "unscored") return { text: "Not enough shared history to compare", tone: "warn" };
                  if (only === "near")     return { text: "Matches a product, but no group formed", tone: "warn" };
                }
                if (!kinds.has("differs")) return { text: "Not compared against anything here", tone: "warn" };
                return { text: "Same category, different behaviour", tone: "warn" };
              };
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
                if (d >= 0.6) return `Strong specific match. These products share a seasonal shape that stands out from the normal store-wide rhythm${why ? `: ${why}` : ""}. A new product borrowing this forecast inherits that specific shape.`;
                if (d >= 0.3) return `Moderate specific match. A new product borrowing this forecast inherits part of this group's own seasonal shape${why ? ` (${why})` : ""}, layered on top of the normal store-wide rhythm.`;
                return `Most of the ${pct(coh.avg)}% overlap is the normal store-wide rhythm (weekends, holidays, broad seasonal lifts). A new product borrowing this forecast mostly inherits that broad rhythm${why ? `, with only a faint shared tilt (${why})` : ""}.`;
              };

              /* A product inside a group. The badges each carry one fact and the long
                 version of that fact lives on hover — three grey sentences stacked under
                 a name is how this page became unreadable. */
              const vetRow = (s, headerSplit = "") => {
                const displayAttrs = attrsForSku(s.skuId, s.attributes);
                const gi = s.groupInfo || {};
                return (
                  <div key={s.skuId} style={{ padding:"13px 15px", border:`2px solid ${T.line}`, background:T.panel }}>
                    <div style={{ display:"flex", alignItems:"center", gap:9, minWidth:0 }}>
                      <span style={{ fontSize:16.5, fontWeight:600, color:T.ink, minWidth:0,
                        whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>{s.skuName}</span>
                      {s.usesYearlyPool && (
                        <span title={`Enough history for Prophet, but its yearly seasonality is still blended ${Math.round((s.yearlyPoolWeight || 0) * 100)}% from ${s.yearlyPoolSize || "related"} related product${s.yearlyPoolSize === 1 ? "" : "s"}.`}
                          style={{ ...mono, flexShrink:0, fontSize:fs.tick, fontWeight:600, padding:"2px 6px",
                            background:T.blueBg, color:T.blueFg, border:`2px solid ${T.blue}33` }}>
                          yearly {Math.round((s.yearlyPoolWeight || 0) * 100)}%
                        </span>
                      )}
                      {gi.level === "subgroup" && (gi.splitCols || []).join("+") !== headerSplit && (
                        <span title={`Behaviour subgroup on ${(gi.splitCols || []).join(" + ")} · ${gi.usableMembers} eligible matches`}
                          style={{ ...mono, flexShrink:0, fontSize:fs.tick, fontWeight:600, padding:"2px 6px",
                            background:T.greenBg, color:T.greenFg, border:`2px solid ${T.green}33` }}>
                          {(gi.splitCols || []).join(" + ")}
                        </span>
                      )}
                      {s.categoryLevel && (
                        <span title={`It has no subgroup of its own, so it is compared across all of ${catOf(s)}. It moves with ${(s.categoryLevelWith || []).join(", ")}, so it is shown with them.`}
                          style={{ ...mono, flexShrink:0, fontSize:fs.tick, padding:"2px 6px",
                            background:T.blueBg, color:T.blueFg, border:`2px solid ${T.blue}33` }}>category level</span>
                      )}
                      {gi.level === "base" && gi.reason && !s.categoryLevel && (
                        <span title={gi.reason}
                          style={{ ...mono, flexShrink:0, fontSize:fs.tick, padding:"2px 6px",
                            background:T.sunken, color:T.soft, border:`2px solid ${T.line}` }}>broad group</span>
                      )}
                    </div>
                    {Object.keys(displayAttrs).length > 0 && (
                      <div style={{ marginTop:6, display:"flex", flexWrap:"wrap" }}>
                        {Object.entries(displayAttrs).map(([k, v]) => chip(k, v, () => removeTag(s.skuId, k)))}
                      </div>
                    )}
                  </div>
                );
              };

              return (
                <div style={{ display:"flex", flexDirection:"column", gap:14 }}>
                  {deadIds.size > 0 && (
                    <div style={{ fontSize:fs.body, color:T.soft, border:`2px solid ${T.line}`, background:T.sunken, padding:"9px 12px" }}>
                      {deadIds.size} dead-stock product{deadIds.size !== 1 ? "s are" : " is"} excluded from grouping and pooled forecasts.
                    </div>
                  )}

                  {categoryKeys.map(cat => {
                    const entry = byCategory[cat];
                    const productCount = entry.clusters.reduce((sum, ck) => sum + (vByCluster[ck]?.length || 0) + (newByCluster[ck]?.length || 0), 0) + entry.standalone.length + entry.newOrphans.length;
                    return (
                      <div key={cat} style={panel}>
                        {/* Category, behaviour group and product now step DOWN in size
                            in that order. They were 17.5 / 15.5 / 15.5 — barely a scale
                            at all — so a page of nested boxes read as one uniform mass
                            and you had to use the indent to work out what was inside
                            what. Padding grew with them: the density complaint was as
                            much about boxes touching as about the type. */}
                        <div style={{ display:"flex", alignItems:"center", gap:12, padding:"18px 22px",
                          borderBottom:`2px solid ${T.line}`, background:T.sunken }}>
                          <span style={{ fontSize:24, fontWeight:700, letterSpacing:"-.02em", color:T.ink }}>{cat}</span>
                          <span style={{ ...mono, fontSize:fs.body, padding:"3px 9px", background:T.panel,
                            border:`2px solid ${T.line}`, color:T.soft }}>{productCount}</span>
                          {entry.clusters.length > 1 && (
                            <span style={{ marginLeft:"auto", fontSize:fs.body, color:T.soft }}>{entry.clusters.length} behaviour groups</span>
                          )}
                        </div>
                        <div style={{ padding:20, display:"flex", flexDirection:"column", gap:16 }}>

                          {entry.clusters.map((ck, idx) => {
                            const members = vByCluster[ck];
                            const borrowers = newByCluster[ck] || [];
                            /* The card speaks for its own group. A category-level product shown
                               here because it moves with this group (categoryLevel) is listed,
                               but the title, split and scores stay the group's own. */
                            const core = members.filter(m => !m.categoryLevel);
                            const lead = core[0] || members[0];
                            const coh = lead?.clusterCohesion ?? null;
                            const poolInfo = lead?.groupInfo || {};
                            const groupTitle = behaviorGroupTitle(core.length ? core : members, cat, idx);
                            const collapsed = !!openGroups[ck];
                            const d = coh?.distinctive;
                            const hSplit = poolInfo.level === "subgroup" ? (poolInfo.splitCols || []).join("+") : "";
                            return (
                              <div key={ck} style={{ border:`2px solid ${T.line}`, background:T.bg }}>
                                <button className="cat-grp" onClick={() => setOpenGroups(o => ({ ...o, [ck]: !o[ck] }))}
                                  style={{ width:"100%", display:"flex", alignItems:"center", gap:12, padding:"15px 16px",
                                    textAlign:"left", background:"transparent", border:"none", cursor:"pointer", color:T.ink }}>
                                  <span style={{ ...mono, fontSize:fs.body, color:T.dim, flexShrink:0,
                                    transform: collapsed ? "none" : "rotate(90deg)" }}>▸</span>
                                  <span style={{ fontSize:19, fontWeight:600, color:T.ink, minWidth:0,
                                    whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>{groupTitle}</span>
                                  <span style={{ fontSize:fs.body, color:T.soft, flexShrink:0 }}>
                                    {members.length} match{members.length !== 1 ? "es" : ""}
                                    {borrowers.length > 0 && <span style={{ color:T.amber }}> · {borrowers.length} borrowing</span>}
                                  </span>
                                  {/* What the group was split on, said ONCE here rather than
                                      repeated as an identical badge on every product in it. */}
                                  {poolInfo.level === "subgroup" && (poolInfo.splitCols || []).length > 0 && (
                                    <span title={`Grouped on ${(poolInfo.splitCols || []).join(" + ")}`}
                                      style={{ ...mono, flexShrink:0, fontSize:fs.small, padding:"2px 7px",
                                        background:T.greenBg, color:T.greenFg, border:`2px solid ${T.green}33` }}>
                                      {(poolInfo.splitCols || []).join(" + ")}
                                    </span>
                                  )}
                                  {/* The quality of the match, as a bar rather than a bare
                                      percentage — you are comparing groups to each other, and
                                      a length is comparable at a glance where a number is not. */}
                                  {d != null && (
                                    <span title="How much this group's shared seasonality stands out from the normal store-wide rhythm"
                                      style={{ marginLeft:"auto", display:"flex", alignItems:"center", gap:9, flexShrink:0 }}>
                                      <span style={{ width:64, height:7, background:T.line, position:"relative" }}>
                                        <span style={{ position:"absolute", left:0, top:0, bottom:0,
                                          width:`${Math.min(100, distPct(coh))}%`, background:cohColor(d) }} />
                                      </span>
                                      <span style={{ ...mono, fontSize:fs.body, fontWeight:600, color:cohColor(d), width:34, textAlign:"right" }}>{distPct(coh)}%</span>
                                      <span style={{ fontSize:fs.body, color:T.soft, width:62 }}>{cohWord(d)}</span>
                                    </span>
                                  )}
                                </button>

                                {!collapsed && (
                                  <div style={{ padding:"12px 13px", borderTop:`2px solid ${T.line}` }}>
                                    <div style={{ display:"grid", gridTemplateColumns: borrowers.length ? "1fr 1fr" : "1fr", gap:16 }}>
                                      <div style={{ minWidth:0, display:"flex", flexDirection:"column", gap:6 }}>
                                        <div style={cap}>Established products</div>
                                        {members.filter(s => !s.usesYearlyPool).map(s => vetRow(s, hSplit))}
                                        {members.some(s => s.usesYearlyPool) && (
                                          <>
                                            <div style={{ ...cap, color:T.blueFg, marginTop:6 }}>Established, borrowing yearly shape</div>
                                            {members.filter(s => s.usesYearlyPool).map(s => vetRow(s, hSplit))}
                                          </>
                                        )}
                                      </div>
                                      {borrowers.length > 0 && (
                                        <div style={{ minWidth:0, display:"flex", flexDirection:"column", gap:6 }}>
                                          <div style={{ ...cap, color:T.amber }}>New, borrowing this</div>
                                          {borrowers.map(s => (
                                            <div key={s.skuId} style={{ padding:"9px 11px", border:`2px solid ${T.amber}44`, background:`${T.amber}0f` }}>
                                              <div style={{ fontSize:fs.row, fontWeight:600, color:T.ink }}>{s.skuName}</div>
                                              {(s.clusterBasis || s.groupInfo?.reason) && (
                                                <div title={[s.clusterBasis, s.groupInfo?.reason].filter(Boolean).join(" · ")}
                                                  style={{ fontSize:fs.small, color:T.soft, marginTop:3,
                                                    whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>
                                                  ↳ {s.clusterBasis || s.groupInfo?.reason}
                                                </div>
                                              )}
                                            </div>
                                          ))}
                                        </div>
                                      )}
                                    </div>

                                    {coh && (
                                      <div style={{ marginTop:14, paddingTop:12, borderTop:`2px solid ${T.line}` }}>
                                        <div style={{ display:"flex", gap:26, flexWrap:"wrap", alignItems:"flex-start" }}>
                                          {/* The specific score leads: it is the one every
                                              decision uses (who groups, who pools, whether a
                                              season is borrowed). The overall figure includes
                                              the rhythm every product shares, so it sits second
                                              and says so. */}
                                          <div>
                                            <div style={cap}>
                                              <span title="Whether the shared seasonality is special to this group, rather than the normal store rhythm (weekends, holidays, broad summer/winter lifts). This is the score grouping and borrowing decisions use.">Specific to this group</span>
                                            </div>
                                            <div style={{ ...mono, fontSize:30, fontWeight:600, color:cohColor(coh.distinctive), marginTop:4 }}>{distPct(coh) ?? "-"}%</div>
                                          </div>
                                          <div>
                                            <div style={cap}>
                                              <span title="How alike their sales patterns are overall, including the weekend and holiday rhythm every product in the store shares. Shown for context; decisions use the specific score.">Overall, incl. store rhythm</span>
                                            </div>
                                            <div style={{ ...mono, fontSize:22, fontWeight:600, color:T.soft, marginTop:4 }}>{pct(coh.avg)}%</div>
                                          </div>
                                          {/* The weakest pair on the same scale as the headline
                                              figure and the threshold. It used to show the raw
                                              score beside a distinctive one, which invited a
                                              comparison that did not mean anything. */}
                                          {(coh.distinctiveMin ?? coh.min) != null
                                            && (coh.distinctiveMin ?? coh.min) < (coh.distinctive ?? coh.avg) - 0.05 && (
                                            <div>
                                              <div style={cap}>Weakest pair</div>
                                              <div style={{ ...mono, fontSize:22, fontWeight:600, color:cohColor(coh.distinctiveMin ?? coh.min), marginTop:4 }}>{Math.max(0, Math.round((coh.distinctiveMin ?? coh.min) * 100))}%</div>
                                            </div>
                                          )}
                                        </div>
                                        {/* One sentence, at a size you can read. The three
                                            stacked grey paragraphs that used to live here said
                                            the same thing the two figures above already say. */}
                                        <p style={{ fontSize:fs.body, color:T.soft, lineHeight:1.65, marginTop:12, marginBottom:0 }}>
                                          {inheritedGroupNote(groupNotes[ck]) || whyText(coh)}
                                        </p>
                                      </div>
                                    )}
                                  </div>
                                )}
                              </div>
                            );
                          })}

                          {/* ── not in a behaviour group ─────────────────────────── */}
                          {entry.standalone.length > 0 && (() => {
                            /* Peers = the other products on this category card. Computed
                               once here so the heading and every card agree on the count
                               the reader can see with their own eyes. */
                            const peers = Math.max(0, productCount - 1);
                            const cases = entry.standalone.map(s => standaloneCase(s, peers));
                            const head  = standaloneHeading(cases);
                            const quiet = head?.tone === "quiet";
                            /* A lone product isn't a warning — it's just a small category.
                               Amber on it reads as "something is wrong here" and nothing is. */
                            const edge  = quiet ? T.line   : `${T.over}44`;
                            const fill  = quiet ? T.sunken : `${T.over}0d`;
                            return (
                            <div style={{ border:`2px solid ${edge}`, background:fill, padding:"11px 13px" }}>
                              <div style={{ ...cap, color: quiet ? T.dim : T.overFg }}>{head?.text}</div>
                              <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fill,minmax(340px,1fr))", gap:12, marginTop:12 }}>
                                {entry.standalone.map((s, si) => {
                                  const kase = cases[si];
                                  const poolLink = kase.poolLink;
                                  /* Short label on the row, full explanation on hover. Each of
                                     these used to be a two-line grey paragraph under every
                                     product, which is a wall of text for a fact you can state
                                     in three words. */
                                  /* `route === "global"` MEANS it is pooling — that branch is only
                                     reached with enough cohesive peers. This row used to read "no pool
                                     available" for exactly those products, i.e. it fired precisely when
                                     it was wrong. A product lands here when it has no behavioural match
                                     of its own inside this category, which does not stop it borrowing
                                     from the nearest cluster that does cohere. */
                                  const bf = s.borrowsFrom;
                                  const bfNames = bf ? bf.skus.map(x => x.skuName) : [];
                                  const why = bf
                                    ? [`borrows ${bf.what} from ${bfNames.length > 2 ? `${bfNames.slice(0, 2).join(", ")} +${bfNames.length - 2}` : bfNames.join(" & ")}`,
                                       `It doesn't sit in a group here, but its sales matched these products closely enough (35%+) that it takes ${bf.what} from them: ${bfNames.join(", ")}.`]
                                    : s.route === "global"
                                    ? ["borrowing from the closest group", "No close match of its own here, but it borrows a pooled forecast from the nearest group in its category with a similar sales shape."]
                                    : s.usesYearlyPool
                                    ? ["borrows yearly shape only", "Forecasts directly with Prophet, but borrows its yearly pattern from related products until it has enough yearly history."]
                                    : s.familyNote && /match no family/.test(s.familyNote)
                                    ? ["forecasts on its own", `Its own sales ${s.familyNote.replace(/^its own sales /, "")}, so it borrows nothing from a family and forecasts from its own sales.`]
                                    : ["forecasts on its own", "Has enough history of its own to forecast directly, so it doesn't need to borrow a pooled forecast."];
                                  const link = kase.link;
                                  const strong = kase.kind === "near";
                                  /* The no-link cases each say what actually happened. "Nothing
                                     to score against" was one sentence covering three of them,
                                     and it was only true for the first. */
                                  const noLinkText =
                                      kase.kind === "lone"     ? "the only product in this category"
                                    : kase.kind === "dead"     ? "its closest match is dead stock, so it was left out"
                                    : `${peers} other product${peers === 1 ? "" : "s"} here, but not enough shared history to compare`;
                                  const noLinkTip =
                                      kase.kind === "lone"     ? "Nothing else sits in this category, so there is nobody to compare it with. Add a related product and it will be scored against it."
                                    : kase.kind === "dead"     ? "The nearest product by shape is dead stock, which is excluded from grouping and pooled forecasts."
                                    : "Two products need at least 5 months of overlapping sales before their seasonal shapes can be compared at all.";
                                  return (
                                    <div key={s.skuId} style={{ padding:"13px 15px", border:`2px solid ${T.line}`, background:T.panel }}>
                                      <div style={{ fontSize:16.5, fontWeight:600, color:T.ink,
                                        whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>{s.skuName}</div>
                                      <div title={why[1]} style={{ fontSize:fs.small, color:T.soft, marginTop:3 }}>{why[0]}</div>
                                      {link ? (
                                        <div style={{ marginTop:7, fontSize:fs.small, color:T.soft, lineHeight:1.5 }}
                                          title={poolLink
                                            ? (strong ? "Strong match, but not enough products share the reliable subgroup."
                                                      : `Needs ${thresholdPct(link)}% to join the pooled group.`)
                                            : (strong ? "Strong match, but it still needs a reliable multi-product subgroup to form a pool."
                                                      : `Needs ${thresholdPct(link)}% to form a pool.`)}>
                                          {link.score > 0 ? (<>
                                            <b style={{ ...mono, color: strong ? T.green : T.over }}>{linkPct(link)}%</b>
                                            {" "}closest to {link.skuName}
                                          </>) : link.score <= -0.2 ? (
                                            /* A negative score means the two move OPPOSITE ways (a Halloween
                                               line against a Christmas one), not a percentage of anything. */
                                            <span title={`Its busy months are ${link.skuName}'s quiet ones, and the other way round.`}>
                                              moves opposite to {link.skuName}
                                            </span>
                                          ) : (
                                            <span title={`Its sales pattern has nothing in common with ${link.skuName}'s, the closest product in its category.`}>
                                              no match, even with {link.skuName}
                                            </span>
                                          )}
                                          {!strong && <span style={{ color:T.faint }}> · needs {thresholdPct(link)}%</span>}
                                          {strong && <span style={{ color:T.faint }}> · no reliable group formed around it</span>}
                                        </div>
                                      ) : (
                                        <div title={noLinkTip} style={{ fontSize:fs.small, color:T.faint, marginTop:7, lineHeight:1.5 }}>
                                          {noLinkText}
                                        </div>
                                      )}
                                    </div>
                                  );
                                })}
                              </div>
                            </div>
                            );
                          })()}

                          {/* ── not pooled ──────────────────────────────────────── */}
                          {entry.newOrphans.length > 0 && (
                            <div style={{ border:`2px solid ${T.line2}`, background:T.sunken, padding:"11px 13px" }}>
                              <div style={cap}>Not included in a pooled forecast</div>
                              <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fill,minmax(340px,1fr))", gap:12, marginTop:12 }}>
                                {entry.newOrphans.map(s => (
                                  <div key={s.skuId} style={{ padding:"9px 11px", border:`2px solid ${T.line}`, background:T.panel }}>
                                    <div style={{ fontSize:fs.row, fontWeight:600, color:T.ink,
                                      whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>{s.skuName}</div>
                                    <div style={{ fontSize:fs.small, color:T.soft, marginTop:3 }}
                                      title={s.clusterBasis
                                        ? "It matched similar products in its category, but not enough established ones to pool with, so it uses its own early sales."
                                        : "Too little history to see seasonality, and no group of similar established products was found yet, so it uses its own early sales."}>
                                      uses its own early sales
                                    </div>
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
            {!groups?.skus?.length && (
              <div style={{ fontSize:fs.body, color:T.soft, lineHeight:1.6 }}>
                Click “Describe products”, paste product info, then “Classify and apply changes” to see groups here.
              </div>
            )}
          </div>
      </div>
    </>
  );

  if (embedded) {
    return (
      <div style={{ flex:1, overflowY:"auto", width:"100%", background:T.bg, color:T.ink, fontFamily:SANS }}>
        <div style={{ width:"100%", maxWidth:1700, margin:"0 auto", paddingBottom:48 }}>{content}</div>
      </div>
    );
  }
  return (
    <>
      {!open ? null : (
        <div style={{ position:"fixed", inset:0, zIndex:50 }}>
          <div style={{ position:"absolute", inset:0, background:"rgba(0,0,0,.45)", backdropFilter:"blur(1px)" }} onClick={onClose} />
          <aside style={{ position:"absolute", left:0, top:0, height:"100%", width:"100%", maxWidth:560,
            overflowY:"auto", borderRight:`2px solid ${T.line}`, background:T.bg, color:T.ink,
            fontFamily:SANS, boxShadow:"0 0 40px rgba(0,0,0,.5)" }}>
            {content}
          </aside>
        </div>
      )}
    </>
  );
}

import { loadStorage } from './storage';

// skuId → attributes the Grouping tab has saved in this browser (survives reloads).
export const CLASSES_STORAGE_KEY = "logitrack_product_classes";

/* Categories this browser has saved that the server has lost. The server keeps them in
   its project folder; this browser keeps a copy too. A fresh copy of the project (a new
   download, a restored machine) starts with none, and the Grouping tab used to put the
   browser's copy back only at the moment it opened, which on a fresh copy came before
   the store's products had loaded, so nothing matched, nothing went back, and every
   product stayed Uncategorized from then on (and with no category peers, holiday
   patterns went undetected too). The app calls this whenever the product list changes.
   Sent as AI tags (fill-in only), so a category the file or store gives always wins.
   Returns the number of products put back. */
export async function restoreSavedClasses(api, skuIds) {
  const saved = loadStorage(CLASSES_STORAGE_KEY, {});
  const ids = new Set(skuIds || []);
  if (!ids.size || !Object.keys(saved).length) return 0;
  let groups;
  try {
    const r = await fetch(`${api}/api/groups`);
    if (!r.ok) return 0;
    groups = await r.json();
  } catch { return 0; }
  const known = {};
  (groups?.skus || []).forEach((s) => { known[s.skuId] = s.attributes || {}; });
  const missing = {};
  Object.entries(saved).forEach(([sid, attrs]) => {
    if (!ids.has(sid) || !attrs || !attrs.category) return;
    if (!(sid in known) || (known[sid] && known[sid].category)) return;
    missing[sid] = attrs;
  });
  if (!Object.keys(missing).length) return 0;
  try {
    const r = await fetch(`${api}/api/attributes`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skus: missing, source: "ai" }),
    });
    return r.ok ? Object.keys(missing).length : 0;
  } catch { return 0; }
}

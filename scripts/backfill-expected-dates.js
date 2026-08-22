/* ONE-OFF DEV REPAIR — paste into the browser console, then reload.
 *
 * Not part of the app. Orders logged before the supplier form persisted its computed
 * expected date were saved with expectedDate: null, so on-time rate has nothing to score
 * against and reads "—". The form is fixed; this only repairs rows already on disk.
 *
 * It dates each delivery from its lane BASELINE, never from a measured P80 — scoring
 * deliveries against a P80 computed from those same deliveries returns ~80% by
 * construction and would mean nothing.
 *
 * Delete this file once the dev data is clean. Shopify-fed installs never need it.
 */
(() => {
  const read = (k) => { const r = JSON.parse(localStorage.getItem(k) || "null");
                        return r && r.__v !== undefined ? r.data : r; };
  const write = (k, v) => localStorage.setItem(k, JSON.stringify({ __v: 1, data: v }));

  const sups = read("logitrack_suppliers") || {};
  const params = read("logitrack_params") || {};
  const pos = (v) => { const n = Math.round(Number(v)); return Number.isFinite(n) && n > 0 ? n : null; };
  let filled = 0, skipped = 0;

  for (const sup of Object.values(sups)) {
    sup.orders = (sup.orders || []).map((o) => {
      if (!o.receivedDate || o.expectedDate || !o.orderedDate) return o;
      const p = o.skuId ? params[o.skuId] : null;
      const m = o.freightMode || null;
      const days = (m && pos(p?.leadTimes?.[m]?.baseline))
                || (m && pos(sup.leadTimes?.[m]?.baseline))
                || (!m && pos(p?.leadTime));
      if (!days) { skipped++; return o; }
      filled++;
      return { ...o, expectedDate: new Date(new Date(o.orderedDate + "T12:00:00Z").getTime()
                                            + days * 864e5).toISOString().slice(0, 10) };
    });
  }
  write("logitrack_suppliers", sups);
  console.log(`Dated ${filled} deliveries.` +
    (skipped ? ` ${skipped} skipped — no baseline for that shipping method.` : "") +
    " Reload the page.");
})();

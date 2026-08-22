import React from 'react';

/**
 * "New units detected — log the arrival?"
 *
 * The inventory record shows stock jumped; an order is open; the two match. That's enough
 * to ASK, and deliberately not enough to record. A stock rise can be a recount, a
 * transfer, or a customer return, and a wrong received-date silently corrupts the
 * lead-time history that every P80 in the tool is built from.
 *
 * So the interaction is one click to confirm or one to dismiss — which is a completely
 * different ask from remembering, unprompted, three weeks later.
 */

function ordersKey(suppliers) {
  const parts = [];
  for (const sup of Object.values(suppliers || {})) {
    for (const o of sup.orders || []) {
      if (!o.receivedDate && o.orderedDate && o.skuId && Number(o.qty) > 0) {
        parts.push(`${o.id}:${o.skuId}:${o.qty}:${o.orderedDate}:${o.expectedDate || ""}`);
      }
    }
  }
  return parts.sort().join("|");
}

function dismissedKey(d) {
  return Object.keys(d || {}).sort().join("|");
}

export function useArrivalPrompts(api, suppliers, openPOs, dismissed) {
  const [found, setFound] = React.useState([]);

  // Only orders that were actually placed and not yet received can produce a prompt.
  // Without this a stock rise on a product with no outstanding order — the classic
  // refund — would still be offered as an arrival.
  /* Keyed on CONTENT, not object identity. A caller passing an inline object literal
     would otherwise produce a new reference every render → new memo → new callback → new
     effect → setState → render, forever. That's a hang, not a slow page, and it would
     only show up in whichever screen happened to construct the prop inline. */
  const openOrders = React.useMemo(() => {
    const out = [];
    for (const sup of Object.values(suppliers || {})) {
      for (const o of sup.orders || []) {
        if (!o.receivedDate && o.orderedDate && o.skuId && Number(o.qty) > 0) {
          out.push({ id: o.id, skuId: o.skuId, qty: o.qty, orderedDate: o.orderedDate,
                     expectedDate: o.expectedDate || null, receivedDate: null,
                     supplierId: sup.id, supplierName: sup.name });
        }
      }
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ordersKey(suppliers)]);

  const refresh = React.useCallback(async () => {
    if (!openOrders.length) { setFound([]); return; }
    try {
      const r = await fetch(`${api}/api/arrivals/suggested?payload_orders=${
        encodeURIComponent(JSON.stringify(openOrders))}`);
      const d = await r.json();
      const byId = Object.fromEntries(openOrders.map(o => [o.id, o]));
      setFound((d?.suggested || [])
        .filter(x => !dismissed?.[`${x.orderId}|${x.arrivedOn}`])
        .map(x => ({ ...x, supplierId: byId[x.orderId]?.supplierId,
                     supplierName: byId[x.orderId]?.supplierName })));
    } catch { setFound([]); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, openOrders, dismissedKey(dismissed)]);

  React.useEffect(() => { refresh(); }, [refresh]);
  return { found, refresh, openOrders };
}

/** Fleet-page list. Hidden entirely when there's nothing to confirm. */
export function ArrivalPromptList({ found = [], skuList = [], lm = false,
                                    onConfirm, onIgnore }) {
  if (!found.length) return null;
  const name = (id) => skuList.find(s => s.id === id)?.name || id;
  const card = lm ? "bg-emerald-50 border-emerald-200" : "bg-emerald-950/15 border-emerald-900/40";
  const head = lm ? "text-emerald-800" : "text-emerald-300";
  const body = lm ? "text-emerald-700" : "text-emerald-300/80";
  const faint = lm ? "text-emerald-600/70" : "text-emerald-400/60";

  return (
    <div className={`${card} border rounded-2xl overflow-hidden mb-5`}>
      <div className={`px-4 py-3 border-b ${lm ? "border-emerald-200" : "border-emerald-900/40"}`}>
        <div className={`text-sm font-bold ${head}`}>
          New units detected — log {found.length === 1 ? "this arrival" : "these arrivals"}?
        </div>
        <div className={`text-[11px] mt-0.5 ${body}`}>
          Stock went up on {found.length === 1 ? "a product" : "products"} with an order outstanding.
          Confirming records the delivery date, which is what sharpens your lead times.
        </div>
      </div>
      {found.map((f) => (
        <div key={`${f.orderId}-${f.arrivedOn}`}
          className={`px-4 py-2.5 flex items-center gap-3 flex-wrap border-b last:border-b-0 ${lm ? "border-emerald-200/60" : "border-emerald-900/25"}`}>
          <div className="flex-1 min-w-[220px]">
            <div className={`text-[12px] font-semibold ${head}`}>{name(f.skuId)}</div>
            <div className={`text-[11px] ${body}`}>{f.message}</div>
            {(f.partial || f.over || f.confidence < 0.8) && (
              <div className={`text-[10px] mt-0.5 ${faint}`}>
                Worth a look before confirming — the quantity doesn&apos;t match the order exactly.
              </div>
            )}
          </div>
          <button onClick={() => onConfirm && onConfirm(f)}
            className={`text-[11px] font-semibold px-3 py-1.5 rounded-lg border transition-all ${lm ? "bg-white border-emerald-400 text-emerald-800 hover:bg-emerald-100" : "bg-emerald-950/40 border-emerald-700 text-emerald-200 hover:bg-emerald-900/40"}`}>
            Yes, it arrived
          </button>
          <button onClick={() => onIgnore && onIgnore(f)}
            className={`text-[11px] px-2.5 py-1.5 rounded-lg ${lm ? "text-emerald-700 hover:bg-emerald-100" : "text-emerald-400 hover:bg-emerald-900/30"}`}>
            Ignore
          </button>
        </div>
      ))}
    </div>
  );
}

/** Product-page banner for one SKU. */
export function ArrivalPromptBanner({ arrival, lm = false, onConfirm, onIgnore }) {
  if (!arrival) return null;
  const card = lm ? "bg-emerald-50 border-emerald-200" : "bg-emerald-950/15 border-emerald-900/40";
  const head = lm ? "text-emerald-800" : "text-emerald-300";
  const body = lm ? "text-emerald-700" : "text-emerald-300/80";
  return (
    <div className={`${card} border rounded-xl px-4 py-3 mb-4 flex items-center gap-3 flex-wrap`}>
      <div className="flex-1 min-w-[240px]">
        <div className={`text-[12px] font-bold ${head}`}>An order arrival was detected</div>
        <div className={`text-[11px] mt-0.5 ${body}`}>{arrival.message}</div>
      </div>
      <button onClick={() => onConfirm && onConfirm(arrival)}
        className={`text-[11px] font-semibold px-3 py-1.5 rounded-lg border transition-all ${lm ? "bg-white border-emerald-400 text-emerald-800 hover:bg-emerald-100" : "bg-emerald-950/40 border-emerald-700 text-emerald-200 hover:bg-emerald-900/40"}`}>
        Yes, mark it received
      </button>
      <button onClick={() => onIgnore && onIgnore(arrival)}
        className={`text-[11px] px-2.5 py-1.5 rounded-lg ${lm ? "text-emerald-700 hover:bg-emerald-100" : "text-emerald-400 hover:bg-emerald-900/30"}`}>
        Ignore
      </button>
    </div>
  );
}

import React, { useState, useEffect, useRef, useMemo } from 'react';
import { computeSupplierStats, computeSkuLeadTimeStats, makeSupId, makeOrdId, isoToDisplay, todayStr, FREIGHT_MODES, LEAD_TIME_MIN_DELIVERIES,
         supplierStatsByLane, allSuppliersByLane, leadTimeTracks, leadTimeBasis, latestChangeAt, readLeadTimeChanges, describeLeadTimeChange,
         namedSuppliers, adoptParkedOrders } from '../lib/helpers';
import { saveStorage } from '../lib/storage';
import { Tip } from './common';

// ─── SUPPLIER PANEL ───────────────────────────
export default function SupplierPanel({ suppliers, setSuppliers, skuList, lm, openPOs, setOpenPOs, receiveStock, onExport, exporting,
                                       pendingArrival = null, onArrivalHandled = null,
                                       skuParams = {} }) {
  const [selected,     setSelected]     = useState(null);
  const [editName,     setEditName]     = useState("");
  const [addingOrder,  setAddingOrder]  = useState(null);
  const [orderForm,    setOrderForm]    = useState({});
  const [addingSupName, setAddingSupName] = useState("");
  const [showAddSup,   setShowAddSup]   = useState(false);
  const [markingOrd,   setMarkingOrd]   = useState(null); // { supId, ordId }
  const [markDate,     setMarkDate]     = useState(todayStr());
  const [showHistory,  setShowHistory]  = useState({}); // supplierId → bool
  const [assignOpen,   setAssignOpen]   = useState(null); // supplierId with the picker open
  const [assignQuery,  setAssignQuery]  = useState("");
  const [openSku,      setOpenSku]      = useState(null); // "supId:skuId" expanded in the SKU list
  const [histQuery,    setHistQuery]    = useState({});   // supplierId → search text
  const [renaming,     setRenaming]     = useState(null); // supplierId being renamed inline
  const assignRef = useRef(null);
  /* Arriving from a "yes, it arrived" prompt: open the right supplier, scroll its history
     into view, and pre-fill the date the stock ACTUALLY moved — not today's. Using today
     would put a wrong number into the lead-time record, which is the thing this whole
     feature exists to get right. */
  useEffect(() => {
    if (!pendingArrival) return;
    const supId = pendingArrival.supplierId
      || Object.values(suppliers || {}).find(s => (s.orders || []).some(o => o.id === pendingArrival.orderId))?.id;
    if (!supId) return;
    setSelected(supId);
    setShowHistory(p => ({ ...p, [supId]: true }));
    setMarkingOrd({ supId, ordId: pendingArrival.orderId });
    setMarkDate(pendingArrival.arrivedOn);
  }, [pendingArrival]);   // eslint-disable-line react-hooks/exhaustive-deps
  // Click-away and Escape close the picker, so it can't be left hanging over the page.
  useEffect(() => {
    if (!assignOpen) return;
    const away = (e) => { if (assignRef.current && !assignRef.current.contains(e.target)) setAssignOpen(null); };
    const esc  = (e) => { if (e.key === "Escape") setAssignOpen(null); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [assignOpen]);

  const textMain  = lm ? "text-slate-900" : "text-white";
  const textMuted = lm ? "text-slate-500" : "text-slate-500";
  const cardBg    = lm ? "bg-white border-slate-200" : "bg-[#161619] border-white/10";
  const rowBg     = lm ? "bg-slate-50 border-slate-200" : "bg-white/[0.03] border-white/10";
  const inp       = lm
    ? "w-full bg-white border border-slate-300 rounded-lg px-3 py-2 text-sm text-slate-900 focus:outline-none focus:border-violet-500"
    : "w-full bg-[#161619] border border-white/10 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-violet-500/60";
  const lbl       = lm
    ? "block text-[11px] text-slate-500 uppercase tracking-widest mb-1"
    : "block text-[10px] text-slate-500 uppercase tracking-widest mb-1";
  const SupTile = ({ label, val, sub, color }) => (
    <div className={`rounded-xl border ${rowBg} px-3.5 py-3`}>
      <div className={`text-[10px] uppercase tracking-widest font-bold ${textMuted}`}>{label}</div>
      <div className={`text-2xl font-bold tabular-nums mt-1 ${color || textMain}`}>{val}</div>
      {sub && <div className={`text-[10.5px] mt-1 ${textMuted}`}>{sub}</div>}
    </div>
  );
  const otColor = (r) => r == null ? undefined : r >= 80 ? (lm ? "text-emerald-600" : "text-emerald-400") : r >= 60 ? (lm ? "text-amber-600" : "text-amber-400") : (lm ? "text-rose-600" : "text-rose-400");

  const createSupplier = () => {
    if (!addingSupName.trim()) return;
    const id = makeSupId();
    setSuppliers(prev => ({ ...prev, [id]: { id, name: addingSupName.trim(), skuIds: [], orders: [] } }));
    setAddingSupName(""); setShowAddSup(false);
  };
  const deleteSupplier = (id) => {
    setSuppliers(prev => { const n = { ...prev }; delete n[id]; return n; });
    if (selected === id) setSelected(null);
  };
  const renameSupplier = (id) => {
    if (!editName.trim()) return;
    setSuppliers(prev => ({ ...prev, [id]: { ...prev[id], name: editName.trim() } }));
    setEditName("");
  };
  const toggleSkuAssign = (supId, skuId) => {
    setSuppliers(prev => {
      const sup = prev[supId];
      const adding = !sup.skuIds.includes(skuId);
      const skuIds = adding ? [...sup.skuIds, skuId] : sup.skuIds.filter(s => s !== skuId);
      const next = { ...prev, [supId]: { ...sup, skuIds } };
      /* Assigning claims the orders this product logged before it had a supplier —
         whole, every field, including one still in transit with no arrival date yet.
         Un-assigning deliberately does NOT hand them back: they're this supplier's
         deliveries now, and moving them out again would silently change its measured
         P80 and on-time rate. */
      return adding ? adoptParkedOrders(next, supId, skuId) : next;
    });
  };
  /* What the form SHOWS in the expected-date box, worked out rather than guessed.
   *
   *   ignoreOneOff — this form logs shipments, and plenty of them already happened. A
   *   "the next one will be late" flag is about the NEXT order, so applying it here dated
   *   a delivery from March by a delay declared in August, and then called 50 days the
   *   usual wait. That flag belongs to the PO modal on the product page and nowhere else.
   */
  const autoExpected = (form, sup = null) => {
    const sku = form.skuId;
    const pp = sku ? skuParams?.[sku] : null;
    const mode = form.freightMode || Object.keys(FREIGHT_MODES)[0];
    const from = form.orderedDate || todayStr();
    const at = (days) => new Date(new Date(`${from}T12:00:00Z`).getTime() + days * 864e5).toISOString().slice(0, 10);
    if (sku && pp) {
      const b = leadTimeBasis(sku, pp, suppliers, { mode, ignoreOneOff: true });
      if (Number(b?.days) > 0) return at(b.days);
    }
    /* No product attached — "none (don't change stock)" is a normal way to log a
       delivery. It used to save no expected date at all, which quietly put another
       unscoreable row into the on-time rate. The supplier's own baseline covers it. */
    const supBase = Number(sup?.leadTimes?.[mode]?.baseline);
    return Number.isFinite(supBase) && supBase > 0 ? at(supBase) : "";
  };

  const addOrder = (supId) => {
    if (!orderForm.orderedDate) return;
    const id = makeOrdId();
    const arrived = !orderForm.notArrived;                  // unchecked "not arrived yet" => already arrived
    const qty = parseInt(orderForm.qty) || 0;
    const skuId = orderForm.skuId || null;
    const receivedDate = arrived ? (orderForm.receivedDate || todayStr()) : null;
    const order = {
      id, orderedDate: orderForm.orderedDate, receivedDate,
      /* SAVE the date the box was showing. It used to save only what you typed, so an
         untouched form filed the order with no expected date at all — and on-time rate,
         which needs a promised date to score against, sat on "—" forever with a full
         history behind it. What you see is what gets stored. */
      expectedDate: orderForm.expectedDate || autoExpected(orderForm, suppliers[supId]) || null, qty,
      notes: orderForm.notes || "", skuId,
      freightMode: orderForm.freightMode || Object.keys(FREIGHT_MODES)[0],
    };
    setSuppliers(prev => ({ ...prev, [supId]: { ...prev[supId], orders: [...(prev[supId].orders || []), order] } }));
    // If logged as already arrived and tied to a product, add the units to stock now.
    if (receivedDate && skuId && qty > 0 && receiveStock) receiveStock(skuId, qty);
    setAddingOrder(null); setOrderForm({});
  };
  /* Baselines live on the SUPPLIER, inherited by every product under it. Editing here
     moves every product that hasn't set its own number — that's the point: one entry for
     the whole catalogue instead of one per SKU. */
  const setSupLane = (supId, mode, value) => {
    setSuppliers(prev => {
      const sup = prev[supId];
      const lanes = { ...(sup.leadTimes || {}) };
      if (value === "" || value == null) delete lanes[mode];
      else lanes[mode] = { ...(lanes[mode] || {}), baseline: Number(value) };
      const next = { ...prev, [supId]: { ...sup, leadTimes: lanes } };
      saveStorage("logitrack_suppliers", next);
      return next;
    });
  };
  const markReceived = (supId, ordId, date) => {
    const sup = suppliers[supId];
    const ord = (sup?.orders || []).find(o => o.id === ordId);
    // Add the received units to stock (only on the transition into "received").
    if (ord && !ord.receivedDate && ord.skuId && ord.qty > 0 && receiveStock) receiveStock(ord.skuId, ord.qty);
    // Clear the linked openPO if present
    if (ord?.skuId && openPOs?.[ord.skuId] && setOpenPOs) {
      const nextPOs = { ...openPOs }; delete nextPOs[ord.skuId];
      setOpenPOs(nextPOs); saveStorage("logitrack_pos", nextPOs);
    }
    setSuppliers(prev => ({
      ...prev, [supId]: {
        ...prev[supId],
        orders: prev[supId].orders.map(o => o.id === ordId ? { ...o, receivedDate: date } : o)
      }
    }));
    setMarkingOrd(null);
    // Clears the prompt everywhere and re-checks, so a confirmed arrival stops being
    // offered the moment it's recorded.
    if (onArrivalHandled) onArrivalHandled();
  };
  const removeOrder = (supId, ordId) => {
    setSuppliers(prev => ({ ...prev, [supId]: { ...prev[supId], orders: prev[supId].orders.filter(o => o.id !== ordId) } }));
  };

  // The holding ledger for un-assigned orders is storage, not a supplier — it gets no
  // card here. Its orders still count toward each product's own lead time regardless.
  const supList = namedSuppliers(suppliers);

  return (
    <div className="flex-1 overflow-y-auto p-7">
      <div className="max-w-5xl mx-auto space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h2 className={`text-2xl font-bold tracking-tight ${textMain}`}>Supplier reliability</h2>
            <p className={`text-[13.5px] ${textMuted} mt-1`}>Track deliveries and measure how reliably each supplier meets its promised dates.</p>
          </div>
          <div className="flex items-center gap-2">
            {supList.length > 0 && (
              <button onClick={onExport} disabled={!!exporting} title="Download all suppliers and their KPIs + order history as Excel"
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 disabled:opacity-60 text-white transition-all">
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" /></svg>
                {exporting ? "Preparing…" : "Export Excel"}
              </button>
            )}
            <button onClick={() => setShowAddSup(v => !v)}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all ${lm ? "bg-white border-slate-300 text-slate-700 hover:border-slate-400" : "bg-slate-900 border-slate-700 text-slate-300 hover:border-slate-500"}`}>
              <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" /></svg>
              Add Supplier
            </button>
          </div>
        </div>

        {supList.length > 0 && (() => {
          const stats = supList.map(s => ({ s, st: computeSupplierStats(s) }));
          const inTransit = supList.reduce((a, s) => a + (s.orders?.filter(o => !o.receivedDate).length || 0), 0);
          /* Two different populations, and conflating them is why this strip read "—"
             with a full delivery history behind it. Avg lead time needs an ORDER DATE and
             an ARRIVAL; on-time additionally needs a PROMISED date. Weighting both by the
             on-time-rated subset meant one supplier with no expected dates blanked the
             lead time too — a figure it had every ingredient for. */
          const timed = stats.filter(x => x.st.n > 0);
          const rated = stats.filter(x => x.st.onTimeRate != null && x.st.n);
          const wAvg = (rows, key) => {
            const tot = rows.reduce((a, x) => a + x.st.n, 0);
            return tot ? Math.round(rows.reduce((a, x) => a + x.st[key] * x.st.n, 0) / tot) : null;
          };
          const onTime = wAvg(rated, "onTimeRate");
          const avgLead = wAvg(timed, "avg");
          const laneAll = allSuppliersByLane({ ...suppliers });
          const laneBits = Object.entries(laneAll.lanes).map(([k, v]) => `${FREIGHT_MODES[k]} ${v.p80}d`);
          const unpromisedAll = supList.reduce((a, s) =>
            a + (s.orders || []).filter(o => o.receivedDate && !o.expectedDate).length, 0);
          const tile = (label, val, sub, color) => (
            <div className={`${cardBg} border rounded-2xl p-4`}>
              <div className={`text-[11px] uppercase tracking-widest font-bold ${textMuted}`}>{label}</div>
              <div className={`text-3xl font-bold tabular-nums mt-2 ${color || textMain}`}>{val}</div>
              {sub && <div className={`text-[11px] mt-1.5 ${textMuted}`}>{sub}</div>}
            </div>
          );
          return (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {tile("Suppliers", supList.length)}
              {tile("On-time rate", onTime != null ? `${onTime}%` : "—",
                rated.length ? `across ${rated.reduce((a, x) => a + x.st.n, 0)} deliveries`
                  : timed.length ? `${unpromisedAll} deliveries logged without an expected date`
                  : "no completed orders yet",
                onTime == null ? undefined : onTime >= 90 ? "text-emerald-500" : onTime >= 75 ? "text-amber-500" : "text-rose-500")}
              {tile("Avg lead time", avgLead != null ? `${avgLead}d` : "—",
                laneBits.length ? `P80 · ${laneBits.join(" · ")}`
                  : avgLead != null ? "order → arrival" : "no completed orders yet")}
              {tile("In transit", inTransit, inTransit ? "open orders" : "none open", inTransit ? (lm ? "text-violet-700" : "text-violet-300") : undefined)}
            </div>
          );
        })()}

        {showAddSup && (
          <div className={`${cardBg} border rounded-2xl p-4 flex gap-2`}>
            <input type="text" value={addingSupName} onChange={e => setAddingSupName(e.target.value)}
              onKeyDown={e => e.key === "Enter" && createSupplier()}
              placeholder="Supplier name" className={`${inp} flex-1`} autoFocus />
            <button onClick={createSupplier} className="bg-violet-700 hover:bg-violet-600 text-white text-xs font-bold px-4 rounded-lg transition-colors">Add</button>
            <button onClick={() => { setShowAddSup(false); setAddingSupName(""); }}
              className={`text-xs px-3 rounded-lg transition-colors ${lm ? "bg-slate-100 hover:bg-slate-200 text-slate-600" : "bg-slate-800 hover:bg-slate-700 text-slate-300"}`}>Cancel</button>
          </div>
        )}

        {supList.length === 0 && (
          <div className={`${cardBg} border rounded-2xl p-8 text-center space-y-2`}>
            <p className={`text-sm font-semibold ${textMain}`}>No suppliers yet</p>
            <p className={`text-xs ${textMuted}`}>Add a supplier, then link orders using "Mark Order In Transit" on any SKU. Orders will appear here for tracking.</p>
          </div>
        )}

        {supList.map(sup => {
          const st      = computeSupplierStats(sup);
          const unassigned = skuList.filter(sku => !sup.skuIds?.includes(sku.id));
          // Match on name OR id so a SKU code works as well as a product name.
          const q = assignQuery.trim().toLowerCase();
          const assignMatches = q
            ? unassigned.filter(sku => `${sku.name} ${sku.id}`.toLowerCase().includes(q))
            : unassigned;
          const isOpen  = selected === sup.id;
          const orders  = sup.orders || [];
          const inTransit  = orders.filter(o => !o.receivedDate);
          const completed  = orders.filter(o => o.receivedDate);
          // Delivery history, filtered by product. A supplier with 23 SKUs and two years
          // of orders is unreadable otherwise, and "when did THIS one last arrive" is the
          // question people actually come here with.
          const hq = (histQuery[sup.id] || "").trim().toLowerCase();
          const histMatches = hq
            ? completed.filter(o => {
                const nm = o.skuId ? (skuList.find(s => s.id === o.skuId)?.name || o.skuId) : "";
                return `${nm} ${o.skuId || ""} ${o.notes || ""}`.toLowerCase().includes(hq);
              })
            : completed;
          return (
            <div key={sup.id} className={`${cardBg} border rounded-2xl`}>
              {/* Header */}
              <div className="flex items-center gap-3 px-5 py-4 cursor-pointer" onClick={() => setSelected(isOpen ? null : sup.id)}>
                <div className={`h-8 w-8 rounded-xl flex items-center justify-center shrink-0 ${lm ? "bg-violet-100 text-violet-700" : "bg-violet-950/40 text-violet-400"}`}>
                  <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
                  </svg>
                </div>
                <div className="flex-1 min-w-0">
                  {/* Renaming belongs beside the name, not in a row halfway down the card
                      where you'd never look for it. */}
                  {renaming === sup.id ? (
                    <div className="flex items-center gap-1.5" onClick={e => e.stopPropagation()}>
                      <input autoFocus type="text" defaultValue={sup.name}
                        onChange={e => setEditName(e.target.value)}
                        onKeyDown={e => {
                          if (e.key === "Enter") { renameSupplier(sup.id); setRenaming(null); }
                          if (e.key === "Escape") { setEditName(""); setRenaming(null); }
                        }}
                        className={`text-sm font-bold rounded border px-1.5 py-0.5 w-56 ${lm ? "bg-white border-violet-400 text-slate-900" : "bg-slate-900 border-violet-600 text-white"}`} />
                      <button onClick={() => { renameSupplier(sup.id); setRenaming(null); }}
                        className="text-[11px] font-bold px-2 py-1 rounded-lg bg-violet-700 hover:bg-violet-600 text-white transition-colors">Save</button>
                      <button onClick={() => { setEditName(""); setRenaming(null); }}
                        className={`text-[11px] px-1.5 py-1 ${textMuted}`}>Cancel</button>
                    </div>
                  ) : (
                    <div className="flex items-center gap-1.5 group/nm">
                      <span className={`text-sm font-bold ${textMain}`}>{sup.name}</span>
                      <button title="Rename supplier"
                        onClick={e => { e.stopPropagation(); setEditName(sup.name); setRenaming(sup.id); }}
                        className={`h-5 w-5 rounded flex items-center justify-center opacity-0 group-hover/nm:opacity-60 hover:!opacity-100 transition-opacity ${lm ? "hover:bg-slate-100 text-slate-600" : "hover:bg-slate-800 text-slate-400"}`}>
                        <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                        </svg>
                      </button>
                    </div>
                  )}
                  <div className={`text-[11px] ${textMuted} flex items-center gap-2`}>
                    <span>{sup.skuIds?.length || 0} SKU{(sup.skuIds?.length || 0) !== 1 ? "s" : ""}</span>
                    {inTransit.length > 0 && <span className={lm ? "text-violet-600 font-semibold" : "text-violet-400 font-semibold"}>{inTransit.length} in transit</span>}
                    {st.n > 0 && <span>{st.n} completed</span>}
                  </div>
                </div>
                <div className="flex items-center gap-4 shrink-0">
                  {st.onTimeRate !== null && (
                    <div className="text-right">
                      <div className={`text-[10px] ${textMuted} uppercase tracking-widest`}>On-time</div>
                      <div className={`text-base font-bold tabular-nums ${st.onTimeRate >= 80 ? (lm ? "text-emerald-700" : "text-emerald-400") : st.onTimeRate >= 60 ? (lm ? "text-amber-700" : "text-amber-400") : (lm ? "text-rose-600" : "text-rose-400")}`}>{st.onTimeRate}%</div>
                    </div>
                  )}
                  {st.p80 !== null && (
                    <div className="text-right">
                      <div className={`text-[10px] ${textMuted} uppercase tracking-widest`}><Tip text="80% of deliveries arrived within this many days — a conservative but reliable planning estimate.">P80</Tip></div>
                      <div className={`text-base font-bold tabular-nums ${lm ? "text-violet-700" : "text-violet-400"}`}>{st.p80}d</div>
                    </div>
                  )}
                </div>
                <button onClick={e => { e.stopPropagation(); deleteSupplier(sup.id); }}
                  className={`h-6 w-6 rounded flex items-center justify-center shrink-0 opacity-50 hover:opacity-100 transition-all ${lm ? "hover:bg-red-50 hover:text-red-600" : "hover:bg-red-950/30 hover:text-red-400"}`}>
                  <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" /></svg>
                </button>
                <svg className={`h-3.5 w-3.5 shrink-0 transition-transform ${isOpen ? "rotate-180" : ""} ${textMuted}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </div>

              {isOpen && (
                <div className={`border-t ${lm ? "border-slate-200" : "border-white/10"} p-5 space-y-5`}>
                  {/* Everything about one shipping method on a single line: the baseline
                      you plan from, and what the deliveries actually did. Lanes never
                      blend — a 44-day sea crossing averaged with a 6-day air run is wrong
                      for both, and the on-time rate would be mixing two entirely different
                      promises.

                      The header and the rows share ONE column definition. They were
                      separate grids with `auto` columns before, so each sized to its own
                      content and "On-time" ended up sitting over a column of dashes. */}
                  {(() => {
                    const byLane   = supplierStatsByLane(sup);
                    // Last column widened from 3.25rem: "DELIVERIES" is a longer header
                    // than the "ORDERS" it replaced and would otherwise clip.
                    const COLS     = "grid grid-cols-[minmax(0,1fr)_4.5rem_3.25rem_3.25rem_4rem_5rem] gap-x-3 items-center";
                    const head     = `text-[10px] uppercase tracking-wider font-bold ${lm ? "text-slate-400" : "text-slate-600"}`;
                    const num      = `text-right font-mono tabular-nums text-[12.5px] font-bold ${lm ? "text-slate-800" : "text-slate-200"}`;
                    const dash     = `text-right font-mono text-[12.5px] ${lm ? "text-slate-300" : "text-slate-700"}`;
                    const unpromised = (sup.orders || []).filter(o => o.receivedDate && !o.expectedDate).length;
                    return (
                      <div className={`rounded-xl border overflow-hidden ${lm ? "bg-slate-50 border-slate-200" : "bg-slate-900/40 border-slate-700/40"}`}>
                        <div className={`px-3.5 py-2.5 border-b flex items-baseline justify-between gap-2 ${lm ? "border-slate-200" : "border-slate-700/40"}`}>
                          <span className={`text-[12px] font-bold ${textMain}`}>Every product from {sup.name}</span>
                          <span className={`text-[10px] ${textMuted}`}>
                            {st.n} completed {st.n === 1 ? "delivery" : "deliveries"}
                            {inTransit.length > 0 && ` · ${inTransit.length} in transit`}
                          </span>
                        </div>
                        <div className={`${COLS} px-3.5 py-1.5 ${head} border-b ${lm ? "border-slate-200" : "border-slate-700/40"}`}>
                          <span>Method</span><span className="text-right">Baseline</span>
                          <span className="text-right">Avg</span><span className="text-right">P80</span>
                          {/* Counts shipments that ARRIVED, not orders placed — an open PO
                              isn't in it. Same figure the product page shows, now under the
                              same name. */}
                          <span className="text-right">On-time</span><span className="text-right">Deliveries</span>
                        </div>
                        {Object.entries(FREIGHT_MODES).map(([k, label]) => {
                          const v      = byLane.lanes[k] || { n: 0, avg: null, p80: null, onTimeRate: null };
                          const own    = sup.leadTimes?.[k]?.baseline;
                          const ownSet = Number.isFinite(Number(own)) && Number(own) > 0;
                          /* The box SHOWS the number in force rather than sitting empty
                             asking for one the tool already knows. Precedence: typed here,
                             then this lane's measured P80, then what the products say. */
                          const measured = v.n >= LEAD_TIME_MIN_DELIVERIES ? v.p80 : null;
                          const fromProducts = [...new Set((sup.skuIds || []).map(id => {
                            const n = Number(skuParams?.[id]?.leadTimes?.[k]?.baseline);
                            return Number.isFinite(n) && n > 0 ? n : null;
                          }).filter(n => n != null))].sort((a, b) => a - b);
                          const derived = measured != null ? measured
                            : fromProducts.length === 1 ? fromProducts[0] : null;
                          const mixed  = !ownSet && measured == null && fromProducts.length > 1;
                          const source = ownSet ? "set here"
                            : measured != null ? `measured P80 · ${v.n} deliveries`
                            : fromProducts.length === 1 ? "from the product page"
                            : mixed ? `products differ: ${fromProducts.join(", ")}d` : null;
                          return (
                            <div key={k} className={`${COLS} px-3.5 py-2 border-b last:border-b-0 ${lm ? "border-slate-200/70" : "border-slate-700/25"}`}>
                              <div className="min-w-0">
                                <div className={`text-[12px] font-semibold ${lm ? "text-slate-700" : "text-slate-300"}`}>{label}</div>
                                {source && (
                                  <div className={`text-[9.5px] truncate ${ownSet ? textMuted : (lm ? "text-violet-600" : "text-violet-400")}`} title={source}>{source}</div>
                                )}
                              </div>
                              <div className="flex items-center gap-1">
                                <input type="number" min="1" placeholder={mixed ? "mixed" : "—"}
                                  value={ownSet ? own : (derived ?? "")}
                                  onChange={e => setSupLane(sup.id, k, e.target.value)}
                                  title={ownSet ? "Set on this supplier. Clear it to fall back to the product's own number."
                                        : source ? `Showing the ${source}. Typing here sets a supplier-wide default instead.`
                                        : `No ${label.toLowerCase()} lead time known yet.`}
                                  className={`w-full min-w-0 text-right rounded border px-1.5 py-1 font-mono text-[11.5px] ${
                                    ownSet ? (lm ? "bg-white border-slate-300 text-slate-900" : "bg-slate-900 border-slate-700 text-slate-200")
                                           : (lm ? "bg-white border-slate-200 text-slate-500 italic" : "bg-slate-900 border-slate-700/50 text-slate-400 italic")}`} />
                                <span className={`text-[9px] ${lm ? "text-slate-400" : "text-slate-600"}`}>d</span>
                              </div>
                              <span className={v.avg != null ? num : dash}>{v.avg != null ? `${v.avg}d` : "—"}</span>
                              <span className={`${v.p80 != null ? num : dash} ${v.n >= LEAD_TIME_MIN_DELIVERIES ? (lm ? "text-violet-600" : "text-violet-400") : ""}`}>
                                {v.p80 != null ? `${v.p80}d` : "—"}
                              </span>
                              <span className={`${v.onTimeRate != null ? num : dash} ${otColor(v.onTimeRate) || ""}`}
                                title={v.onTimeRate == null && v.n > 0
                                  ? "These deliveries were logged without an expected date, so there's nothing to score against."
                                  : undefined}>
                                {v.onTimeRate != null ? `${v.onTimeRate}%` : "—"}
                              </span>
                              <span className={`${dash} ${v.n ? (lm ? "text-slate-500" : "text-slate-500") : ""}`}>{v.n || "—"}</span>
                            </div>
                          );
                        })}

                        <div className={`border-t px-3.5 py-1.5 text-[9.5px] leading-relaxed ${lm ? "border-slate-200 text-slate-500" : "border-slate-700/40 text-slate-500"}`}>
                          Baseline is what every product here plans from until that method has {LEAD_TIME_MIN_DELIVERIES} deliveries of its own; a product that sets its own number keeps it.
                          {byLane.untagged > 0 && ` ${byLane.untagged} untagged.`}
                          {unpromised > 0 && ` On-time skips ${unpromised} ${unpromised === 1 ? "delivery" : "deliveries"} logged without an expected date.`}
                        </div>
                      </div>
                    );
                  })()}

                  {/* Assigned SKUs — one line each, expanding to that product's own three
                      methods. The supplier figures above are the whole book; this is where
                      a single product's numbers live, and they can differ. */}
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <div className={`${lbl} mb-0`}>Assigned SKUs ({sup.skuIds?.length || 0})</div>
                      {/* Search-and-pick instead of a wall of chips: with a real catalogue
                          the chip list was hundreds of buttons deep and buried everything
                          below it. Collapsed by default, costs one click to open. */}
                      {unassigned.length > 0 && (
                        <div className="relative" ref={assignOpen === sup.id ? assignRef : null}>
                          <button onClick={() => { setAssignOpen(assignOpen === sup.id ? null : sup.id); setAssignQuery(""); }}
                            className={`text-[11px] font-semibold px-2 py-1 rounded-lg border transition-all ${lm ? "bg-white border-slate-300 text-slate-600 hover:border-violet-400 hover:text-violet-700" : "bg-slate-800 border-slate-700/60 text-slate-400 hover:border-violet-700 hover:text-violet-300"}`}>
                            + Assign product <span className={lm ? "text-slate-400" : "text-slate-600"}>({unassigned.length})</span>
                          </button>
                          {assignOpen === sup.id && (
                            <div className={`absolute right-0 top-full mt-1 w-72 rounded-xl border shadow-xl z-40 overflow-hidden ${lm ? "bg-white border-slate-200" : "bg-[#1c1c20] border-slate-700"}`}>
                              <div className={`p-2 border-b ${lm ? "border-slate-200" : "border-slate-700/60"}`}>
                                <input autoFocus type="text" value={assignQuery}
                                  onChange={e => setAssignQuery(e.target.value)}
                                  onKeyDown={e => {
                                    if (e.key === "Escape") setAssignOpen(null);
                                    // Enter assigns when the search has narrowed to one product
                                    if (e.key === "Enter" && assignMatches.length === 1) {
                                      toggleSkuAssign(sup.id, assignMatches[0].id); setAssignQuery("");
                                    }
                                  }}
                                  placeholder="Search products…"
                                  className={`w-full text-xs px-2 py-1.5 rounded-lg border outline-none ${lm ? "bg-slate-50 border-slate-200 text-slate-800 focus:border-violet-400" : "bg-slate-900 border-slate-700 text-slate-200 focus:border-violet-600"}`} />
                              </div>
                              <div className="max-h-64 overflow-y-auto sku-scroll">
                                {assignMatches.map(sku => (
                                  <button key={sku.id} onClick={() => { toggleSkuAssign(sup.id, sku.id); setAssignQuery(""); }}
                                    className={`w-full text-left px-3 py-2 text-[11px] font-mono truncate transition-colors ${lm ? "text-slate-700 hover:bg-violet-50" : "text-slate-300 hover:bg-violet-950/30"}`}>
                                    {sku.name}
                                  </button>
                                ))}
                                {assignMatches.length === 0 && (
                                  <div className={`px-3 py-4 text-[11px] text-center ${textMuted}`}>
                                    {assignQuery ? `No product matches “${assignQuery}”` : "Everything is assigned"}
                                  </div>
                                )}
                              </div>
                              {assignMatches.length > 0 && (
                                <div className={`px-3 py-1.5 border-t text-[10px] ${lm ? "border-slate-200 text-slate-400" : "border-slate-700/60 text-slate-600"}`}>
                                  {assignMatches.length} of {unassigned.length} shown
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                    {skuList.length === 0 && <div className={`text-[11px] mb-3 ${textMuted}`}>No SKUs loaded yet</div>}

                    {(sup.skuIds?.length > 0) && (() => {
                      // Last column widened to fit the "DELIVERIES" header (was "SHIPS").
                      const SCOLS = "grid grid-cols-[minmax(0,1fr)_3.25rem_3.25rem_5rem] gap-x-3 items-center";
                      const shead = `text-[10px] uppercase tracking-widest font-bold ${lm ? "text-slate-400" : "text-slate-500"}`;
                      return (
                        <div className={`rounded-xl border overflow-hidden ${lm ? "border-slate-200" : "border-slate-700/50"}`}>
                          <div className={`${SCOLS} px-3 py-1.5 ${shead} ${lm ? "bg-slate-100 border-b border-slate-200" : "bg-slate-800/50 border-b border-slate-700/40"}`}>
                            <span>Product</span><span className="text-right">Avg</span><span className="text-right">P80</span><span className="text-right">Deliveries</span>
                          </div>
                          {sup.skuIds.map((skuId, i) => {
                            const skuName  = skuList.find(s => s.id === skuId)?.name || skuId;
                            /* Each product's own changepoint applies here too. Showing a
                               pre-change average on this row while the product page shows
                               "—" would have the same product reporting two lead times. */
                            const st2      = computeSkuLeadTimeStats(skuId, { [sup.id]: sup }, latestChangeAt(skuParams?.[skuId]));
                            const st2All   = computeSkuLeadTimeStats(skuId, { [sup.id]: sup });
                            const setAside = Math.max(0, st2All.n - st2.n);
                            const changedOn = latestChangeAt(skuParams?.[skuId]);
                            const lastChange = readLeadTimeChanges(skuParams?.[skuId]).slice(-1)[0] || null;
                            const hasData  = st2.n > 0;
                            const reliable = st2.n >= LEAD_TIME_MIN_DELIVERIES;
                            const key      = `${sup.id}:${skuId}`;
                            const open2    = openSku === key;
                            const pp       = skuParams?.[skuId] || null;
                            return (
                              <div key={skuId} className={i > 0 ? `border-t ${lm ? "border-slate-100" : "border-slate-700/30"}` : ""}>
                                <div onClick={() => setOpenSku(open2 ? null : key)}
                                  className={`${SCOLS} px-3 py-2.5 cursor-pointer transition-colors ${open2 ? (lm ? "bg-violet-50" : "bg-violet-950/20") : (lm ? "hover:bg-slate-50" : "hover:bg-slate-800/20")}`}>
                                  <div className="flex items-center gap-1.5 min-w-0">
                                    <svg className={`h-2.5 w-2.5 shrink-0 transition-transform ${open2 ? "rotate-90" : ""} ${textMuted}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M9 5l7 7-7 7" />
                                    </svg>
                                    <div className="min-w-0">
                                      <div className={`text-[11px] font-mono truncate ${lm ? "text-slate-700" : "text-slate-300"}`}>{skuName}</div>
                                      {/* A product whose lead time was permanently changed is measuring
                                          a different arrangement to the one next to it, so the row has to
                                          say so — otherwise two rows of numbers look comparable when one
                                          covers three years and the other covers three weeks. */}
                                      {changedOn && (
                                        <div className={`text-[9.5px] truncate ${lm ? "text-violet-600" : "text-violet-400"}`}
                                          title={lastChange ? `${describeLeadTimeChange(lastChange)} on ${isoToDisplay(changedOn)}` : undefined}>
                                          since {isoToDisplay(changedOn)}
                                        </div>
                                      )}
                                    </div>
                                  </div>
                                  <span className={`text-right text-[11px] font-bold tabular-nums ${hasData ? (lm ? "text-slate-600" : "text-slate-300") : (lm ? "text-slate-300" : "text-slate-600")}`}>
                                    {hasData ? `${st2.avg}d` : "—"}
                                  </span>
                                  <span className={`text-right text-[11px] font-bold tabular-nums ${hasData && st2.p80 != null ? (reliable ? (lm ? "text-violet-600" : "text-violet-400") : (lm ? "text-slate-400" : "text-slate-500")) : (lm ? "text-slate-300" : "text-slate-600")}`}>
                                    {hasData && st2.p80 != null ? `${st2.p80}d` : "—"}
                                  </span>
                                  <span className={`text-right text-[11px] tabular-nums ${hasData ? (lm ? "text-slate-500" : "text-slate-500") : (lm ? "text-slate-300" : "text-slate-600")}`}
                                    title={setAside > 0 ? `${setAside} earlier ${setAside === 1 ? "delivery" : "deliveries"} set aside by a lead-time change` : undefined}>
                                    {st2.n > 0 ? st2.n : "none"}
                                    {setAside > 0 && <span className={`ml-0.5 text-[9px] ${lm ? "text-slate-400" : "text-slate-600"}`}>+{setAside}</span>}
                                    {hasData && !reliable && <span className="ml-0.5 text-[10px] text-amber-500" title={`${LEAD_TIME_MIN_DELIVERIES - st2.n} more before a P80 is trusted`}>⚠</span>}
                                  </span>
                                </div>
                                {open2 && (
                                  <div className={`px-3 pt-1 pb-3 border-t ${lm ? "bg-slate-50/70 border-slate-100" : "bg-slate-900/30 border-slate-700/30"}`}>
                                    {changedOn && (
                                      <div className={`mt-2 mb-1 rounded-lg border px-2.5 py-1.5 text-[10px] leading-relaxed ${lm ? "bg-violet-50/70 border-violet-200 text-slate-600" : "bg-violet-950/15 border-violet-900/40 text-slate-400"}`}>
                                        <span className={`font-semibold ${lm ? "text-violet-800" : "text-violet-300"}`}>
                                          Lead time changed {isoToDisplay(changedOn)}
                                          {lastChange && !lastChange.legacy ? ` — ${describeLeadTimeChange(lastChange)}` : ""}.
                                        </span>{" "}
                                        Figures here cover {st2.n === 0 ? "no deliveries" : `the ${st2.n} ${st2.n === 1 ? "delivery" : "deliveries"}`} since.
                                        {setAside > 0 && ` ${setAside} earlier ${setAside === 1 ? "one is" : "ones are"} set aside — nothing was deleted.`}
                                      </div>
                                    )}
                                    {pp ? (() => {
                                      const tracks2 = leadTimeTracks(skuId, pp, suppliers);
                                      const basis   = leadTimeBasis(skuId, pp, suppliers, { ignoreOneOff: true });
                                      return (
                                        <>
                                          <div className={`${SCOLS} pt-1.5 pb-1 ${shead}`}>
                                            <span>Its own methods</span><span className="text-right">Baseline</span><span className="text-right">P80</span><span className="text-right">Deliveries</span>
                                          </div>
                                          {tracks2.map(t => (
                                            <div key={t.mode} className={`${SCOLS} py-0.5 text-[11px]`}>
                                              <span className={`truncate ${t.used ? (lm ? "text-slate-700" : "text-slate-300") : (lm ? "text-slate-400" : "text-slate-600")}`}>
                                                {t.label}
                                                {t.isDefault && <span className={`ml-1.5 text-[9px] uppercase tracking-wide ${lm ? "text-violet-600" : "text-violet-400"}`}>default</span>}
                                              </span>
                                              <span className={`text-right font-mono tabular-nums ${t.baseline == null ? (lm ? "text-slate-300" : "text-slate-700") : t.inherited ? (lm ? "text-slate-400 italic" : "text-slate-500 italic") : (lm ? "text-slate-700" : "text-slate-300")}`}
                                                title={t.inherited ? `Inherited from ${sup.name}` : undefined}>
                                                {t.baseline != null ? `${t.baseline}d` : "—"}
                                              </span>
                                              <span className={`text-right font-mono tabular-nums ${t.p80 != null ? (lm ? "text-emerald-700 font-bold" : "text-emerald-400 font-bold") : (lm ? "text-slate-300" : "text-slate-700")}`}>
                                                {t.p80 != null ? `${t.p80}d` : "—"}
                                              </span>
                                              <span className={`text-right font-mono tabular-nums ${lm ? "text-slate-500" : "text-slate-500"}`}>{t.n || "—"}</span>
                                            </div>
                                          ))}
                                          <div className={`text-[10px] mt-2 leading-relaxed ${lm ? "text-slate-500" : "text-slate-500"}`}>
                                            <span className={`font-semibold ${lm ? "text-slate-700" : "text-slate-300"}`}>Plans on {basis.label}.</span>{" "}
                                            {basis.detail}
                                            {tracks2.some(t => t.inherited) && <> Italic baselines come from {sup.name}.</>}
                                          </div>
                                        </>
                                      );
                                    })() : (
                                      <div className={`text-[10px] pt-2 ${textMuted}`}>Open this product to set its lead times per method.</div>
                                    )}
                                    <button onClick={() => toggleSkuAssign(sup.id, skuId)}
                                      className={`mt-2 text-[10px] font-semibold px-2 py-1 rounded-lg border transition-all ${lm ? "bg-white border-slate-300 text-slate-500 hover:border-rose-400 hover:text-rose-600" : "bg-slate-800 border-slate-700/60 text-slate-400 hover:border-rose-800 hover:text-rose-400"}`}>
                                      Unassign from {sup.name}
                                    </button>
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      );
                    })()}
                  </div>


                  {/* In Transit */}
                  {inTransit.length > 0 && (
                    <div>
                      <div className={`${lbl} mb-2`}>In Transit ({inTransit.length})</div>
                      <div className="space-y-2">
                        {inTransit.map(ord => {
                          const daysLeft = ord.expectedDate
                            ? Math.round((new Date(ord.expectedDate) - new Date(todayStr())) / 86400000)
                            : null;
                          const skuName = ord.skuId ? skuList.find(s => s.id === ord.skuId)?.name || ord.skuId : null;
                          const isMarking = markingOrd?.supId === sup.id && markingOrd?.ordId === ord.id;
                          return (
                            <div key={ord.id} className={`border rounded-xl p-3 ${lm ? "bg-violet-50 border-violet-200" : "bg-violet-950/15 border-violet-900/30"}`}>
                              <div className="flex items-start justify-between gap-3">
                                <div className="flex-1 min-w-0 space-y-0.5">
                                  {skuName && <div className={`text-[11px] font-semibold ${lm ? "text-violet-700" : "text-violet-300"}`}>{skuName}</div>}
                                  <div className={`text-xs font-mono ${textMain}`}>
                                    Ordered {isoToDisplay(ord.orderedDate)}
                                    {ord.qty > 0 && <span className={`ml-2 ${textMuted}`}>· {ord.qty.toLocaleString()} units</span>}
                                  </div>
                                  {ord.expectedDate && (
                                    <div className={`text-xs flex items-center gap-2`}>
                                      <span className={textMuted}>Expected {isoToDisplay(ord.expectedDate)}</span>
                                      {daysLeft !== null && (
                                        <span className={`font-semibold text-[11px] font-mono ${daysLeft < 0 ? (lm ? "text-rose-600" : "text-rose-400") : daysLeft <= 3 ? (lm ? "text-amber-700" : "text-amber-400") : (lm ? "text-emerald-700" : "text-emerald-400")}`}>
                                          {daysLeft < 0 ? `${Math.abs(daysLeft)}d overdue` : daysLeft === 0 ? "due today" : `${daysLeft}d remaining`}
                                        </span>
                                      )}
                                    </div>
                                  )}
                                </div>
                                <div className="flex items-center gap-2 shrink-0">
                                  {!isMarking ? (
                                    <button onClick={() => { setMarkingOrd({ supId: sup.id, ordId: ord.id }); setMarkDate(todayStr()); }}
                                      className={`text-[11px] px-2.5 py-1.5 rounded-lg border font-semibold transition-colors ${lm ? "bg-emerald-50 border-emerald-300 text-emerald-700 hover:bg-emerald-100" : "bg-emerald-950/20 border-emerald-800/40 text-emerald-400 hover:border-emerald-600"}`}>
                                      Mark arrived
                                    </button>
                                  ) : (
                                    <div className="flex items-center gap-1.5">
                                      <input type="date" value={markDate} onChange={e => setMarkDate(e.target.value)}
                                        className={`text-xs px-2 py-1 rounded-lg border ${lm ? "bg-white border-slate-300 text-slate-900" : "bg-slate-800 border-slate-700 text-slate-100"}`} />
                                      <button onClick={() => markReceived(sup.id, ord.id, markDate)}
                                        className="text-[11px] px-2.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-bold transition-colors">
                                        ✓ Confirm
                                      </button>
                                      <button onClick={() => setMarkingOrd(null)}
                                        className={`text-[11px] px-2 py-1.5 rounded-lg ${lm ? "text-slate-500 hover:text-slate-700" : "text-slate-500 hover:text-slate-300"} transition-colors`}>
                                        Cancel
                                      </button>
                                    </div>
                                  )}
                                  <button onClick={() => removeOrder(sup.id, ord.id)}
                                    className={`h-5 w-5 rounded flex items-center justify-center opacity-40 hover:opacity-100 transition-all ${lm ? "hover:text-red-600" : "hover:text-red-400"}`}>
                                    <svg className="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" /></svg>
                                  </button>
                                </div>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}


                  {/* Completed orders */}
                  <div>
                    <div className="flex items-center justify-between gap-3 mb-2">
                      <div className="flex items-center gap-3 min-w-0">
                        <div className={`${lbl} mb-0 shrink-0`}>Delivery History</div>
                        {completed.length > 0 && (
                          <button onClick={() => setShowHistory(p => ({ ...p, [sup.id]: !p[sup.id] }))}
                            className={`text-[11px] shrink-0 transition-colors ${lm ? "text-violet-600 hover:text-violet-800" : "text-violet-400 hover:text-violet-300"}`}>
                            {showHistory[sup.id] ? "Hide" : `Show ${completed.length}`}
                          </button>
                        )}
                      </div>
                      {showHistory[sup.id] && completed.length > 0 && (
                        <input type="text" value={histQuery[sup.id] || ""}
                          onChange={e => setHistQuery(p => ({ ...p, [sup.id]: e.target.value }))}
                          placeholder="Search by product…"
                          className={`text-[11px] px-2 py-1 rounded-lg border w-48 outline-none ${lm ? "bg-white border-slate-300 text-slate-800 focus:border-violet-400" : "bg-slate-900 border-slate-700 text-slate-200 focus:border-violet-600"}`} />
                      )}
                    </div>

                    {addingOrder === sup.id && (
                      <div className={`${rowBg} border rounded-xl p-4 mb-3 space-y-3`}>
                        <div className="grid grid-cols-2 gap-3">
                          <div className="col-span-2">
                            <label className={lbl}>Product</label>
                            <select value={orderForm.skuId || ""} onChange={e => setOrderForm(p => ({ ...p, skuId: e.target.value }))} className={inp}>
                              <option value="">— none (don't change stock) —</option>
                              {skuList.map(s => <option key={s.id} value={s.id}>{s.name} ({s.id})</option>)}
                            </select>
                          </div>
                          <div><label className={lbl}>Order date</label><input type="date" value={orderForm.orderedDate || ""} onChange={e => setOrderForm(p => ({ ...p, orderedDate: e.target.value }))} className={inp} /></div>
                          <div><label className={lbl}>Quantity</label><input type="number" min="0" value={orderForm.qty || ""} onChange={e => setOrderForm(p => ({ ...p, qty: e.target.value }))} className={inp} placeholder="e.g. 500" /></div>
                          <div>
                            <label className={lbl}>Shipping method (optional)</label>
                            <select value={orderForm.freightMode || Object.keys(FREIGHT_MODES)[0]}
                              onChange={e => setOrderForm(p => ({ ...p, freightMode: e.target.value }))} className={inp}>

                              {Object.entries(FREIGHT_MODES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                            </select>
                          </div>
                          <div>
                            <label className={lbl}>Expected date</label>
                            {(() => {
                              const sku  = orderForm.skuId;
                              const pp   = sku ? skuParams?.[sku] : null;
                              const mode = orderForm.freightMode || Object.keys(FREIGHT_MODES)[0];
                              const basis = (sku && pp)
                                ? leadTimeBasis(sku, pp, suppliers, { mode, ignoreOneOff: true }) : null;
                              const auto = autoExpected(orderForm, sup);
                              return (
                                <>
                                  <input type="date" value={orderForm.expectedDate || auto}
                                    onChange={e => setOrderForm(p => ({ ...p, expectedDate: e.target.value }))}
                                    className={inp} />
                                  <div className={`text-[10px] mt-0.5 ${lm ? "text-slate-500" : "text-slate-500"}`}>
                                    {orderForm.expectedDate ? "Your date."
                                      : basis ? <span title={basis.detail}>{basis.label}{basis.disagrees ? " — deliveries disagree" : ""}</span>
                                      : auto ? `${FREIGHT_MODES[mode]} baseline for ${sup.name}.`
                                      : sku ? "No lead time known for this product yet."
                                      : "No baseline for this method — on-time can't be scored without a date."}
                                  </div>
                                </>
                              );
                            })()}
                          </div>
                          {!orderForm.notArrived && (
                            <div><label className={lbl}>Arrival date</label><input type="date" value={orderForm.receivedDate || todayStr()} onChange={e => setOrderForm(p => ({ ...p, receivedDate: e.target.value }))} className={inp} /></div>
                          )}
                          <div className="col-span-2">
                            <label className="flex items-center gap-2 cursor-pointer select-none">
                              <input type="checkbox" checked={!!orderForm.notArrived} onChange={e => setOrderForm(p => ({ ...p, notArrived: e.target.checked }))} />
                              <span className={`text-xs ${textMain}`}>Not arrived yet — track as in transit (stock updates when it arrives)</span>
                            </label>
                          </div>
                          <div className="col-span-2"><label className={lbl}>Notes (optional)</label><input type="text" value={orderForm.notes || ""} onChange={e => setOrderForm(p => ({ ...p, notes: e.target.value }))} className={inp} /></div>
                        </div>
                        {orderForm.skuId && !orderForm.notArrived && (parseInt(orderForm.qty) || 0) > 0 && (
                          <p className={`text-[11px] ${lm ? "text-emerald-700" : "text-emerald-400"}`}>
                            ✓ Adds {(parseInt(orderForm.qty) || 0).toLocaleString()} unit{(parseInt(orderForm.qty) || 0) !== 1 ? "s" : ""} to {skuList.find(s => s.id === orderForm.skuId)?.name || orderForm.skuId} stock.
                          </p>
                        )}
                        <div className="flex gap-2">
                          <button onClick={() => addOrder(sup.id)} className="flex-1 bg-violet-700 hover:bg-violet-600 text-white text-xs font-bold py-2 rounded-lg transition-colors">Save</button>
                          <button onClick={() => { setAddingOrder(null); setOrderForm({}); }} className={`flex-1 text-xs py-2 rounded-lg transition-colors ${lm ? "bg-white border border-slate-300 text-slate-700 hover:bg-slate-100" : "bg-slate-800 hover:bg-slate-700 text-slate-300"}`}>Cancel</button>
                        </div>
                      </div>
                    )}

                    {completed.length === 0 && !addingOrder && inTransit.length === 0 && (
                      <p className={`text-xs text-center py-3 ${textMuted}`}>No deliveries logged yet.</p>
                    )}

                    {showHistory[sup.id] && completed.length > 0 && histMatches.length === 0 && (
                      <p className={`text-xs text-center py-3 ${textMuted}`}>
                        No delivery matches “{histQuery[sup.id]}”.
                      </p>
                    )}

                    {showHistory[sup.id] && histMatches.length > 0 && (
                      <div className="space-y-2">
                        {hq && (
                          <div className={`text-[10px] ${textMuted}`}>{histMatches.length} of {completed.length} deliveries</div>
                        )}
                        {histMatches.slice().reverse().map(ord => {
                          const lt = Math.round((new Date(ord.receivedDate) - new Date(ord.orderedDate)) / 86400000);
                          const variance = ord.expectedDate
                            ? Math.round((new Date(ord.receivedDate) - new Date(ord.expectedDate)) / 86400000)
                            : null;
                          return (
                            <div key={ord.id} className={`${rowBg} border rounded-xl p-3 flex items-center gap-3`}>
                              <div className="flex-1 min-w-0">
                                {/* Which product this was. The row showed dates and a
                                    duration but never said what arrived, which made the
                                    history unsearchable by the one thing you'd search by. */}
                                {ord.skuId && (
                                  <div className={`text-[11px] font-semibold truncate ${lm ? "text-violet-700" : "text-violet-300"}`}>
                                    {skuList.find(s => s.id === ord.skuId)?.name || ord.skuId}
                                  </div>
                                )}
                                <div className="flex items-center gap-2 flex-wrap text-xs font-mono">
                                  <span className={textMain}>{isoToDisplay(ord.orderedDate)}</span>
                                  <svg className={`h-3 w-3 ${textMuted}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 5l7 7-7 7M5 5l7 7-7 7" /></svg>
                                  <span className={lm ? "text-emerald-700" : "text-emerald-400"}>{isoToDisplay(ord.receivedDate)} ({lt}d)</span>
                                  {ord.freightMode && FREIGHT_MODES[ord.freightMode] && (
                                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${lm ? "bg-slate-200 text-slate-600" : "bg-slate-800 text-slate-400"}`}>{FREIGHT_MODES[ord.freightMode]}</span>
                                  )}
                                  {variance !== null && (
                                    <span className={`text-[11px] font-semibold ${variance <= 0 ? (lm ? "text-emerald-600" : "text-emerald-400") : (lm ? "text-rose-600" : "text-rose-400")}`}
                                      title={`Against the ${isoToDisplay(ord.expectedDate)} expected date.`}>
                                      {variance > 0 ? `+${variance}d late` : variance === 0 ? "on time" : `${Math.abs(variance)}d early`}
                                    </span>
                                  )}
                                  {ord.qty > 0 && <span className={`text-[11px] ${textMuted}`}>{ord.qty.toLocaleString()} units</span>}
                                </div>
                                {ord.notes && <div className={`text-[11px] mt-0.5 italic ${textMuted}`}>{ord.notes}</div>}
                              </div>
                              <button onClick={() => removeOrder(sup.id, ord.id)}
                                className={`h-5 w-5 rounded flex items-center justify-center shrink-0 opacity-40 hover:opacity-100 transition-all ${lm ? "hover:text-red-600" : "hover:text-red-400"}`}>
                                <svg className="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" /></svg>
                              </button>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>

                  {/* The one action on this card, at the end of it. */}
                  {addingOrder !== sup.id && (
                    <button onClick={() => { setAddingOrder(sup.id); setOrderForm({ skuId: sup.skuIds?.[0] || "", freightMode: Object.keys(FREIGHT_MODES)[0] }); }}
                      className={`w-full text-[12px] font-semibold py-2 rounded-xl border transition-all ${lm ? "bg-white border-slate-300 text-slate-600 hover:border-violet-400 hover:text-violet-700" : "bg-slate-800/60 border-slate-700/60 text-slate-300 hover:border-violet-700 hover:text-violet-300"}`}>
                      + Log / stock an order
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
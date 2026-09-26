import React, { useState, useEffect, useRef } from 'react';
import { computeSupplierStats, computeSkuLeadTimeStats, makeSupId, makeOrdId, isoToDisplay, todayStr, FREIGHT_MODES, LEAD_TIME_MIN_DELIVERIES,
         supplierStatsByLane, allSuppliersByLane, leadTimeTracks, leadTimeBasis, latestChangeAt, readLeadTimeChanges, describeLeadTimeChange,
         namedSuppliers, adoptParkedOrders } from '../lib/helpers';
import { saveStorage } from '../lib/storage';
import { Tip } from './common';
import { terminal, MONO, SANS } from '../lib/theme';

/* ─── SUPPLIER PANEL ──────────────────────────────────────────────────────────
 *
 * LAYOUT NOTE — why every supplier now carries a ruler.
 *
 * The card used to answer "how reliable is this supplier?" with two numbers: an
 * on-time percentage and a P80. Both are summaries of a distribution nobody could
 * see, and the two failure modes they hide are opposite ones. A supplier that is
 * always 40 days and a supplier that is 12 days half the time and 70 days the other
 * half can post the same average, and you would plan the second one exactly wrong.
 *
 * So each row draws its own deliveries on a shared 0–N day axis: one tick per
 * completed shipment, green if it beat its expected date and red if it did not,
 * with the average-to-P80 span shaded behind them and open orders marked as
 * diamonds at the day they are expected. The axis is shared across suppliers, so
 * the rows are directly comparable — which two summary numbers in separate cards
 * never were.
 *
 * Ticks are coloured against each order's OWN expected date, which was derived from
 * that order's own freight lane. That matters: a 44-day sea crossing and a 6-day air
 * run must never be scored against one another, and colouring by "late or not"
 * rather than by absolute position keeps each shipment judged on its own promise.
 * The `quoted` marker is drawn only when one lane accounts for nearly all of the
 * history, and it is labelled with that lane's name; a genuinely mixed supplier
 * gets no single line, because no single number would be true for it.
 *
 * Rows still expand in place, and everything that was in the expanded card is still
 * there: the per-method baselines, the assigned products with their own history, in
 * transit, the delivery history with its search, and the log-an-order form.
 */
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
  /* "supplierId|skuId" → bool. Delivery history per PRODUCT, because the question people
     open a supplier with is "when did THIS one last arrive", and a supplier with 29
     products and two years of orders answers that with an undifferentiated pile. */
  const [showSkuHist,  setShowSkuHist]  = useState({});
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

  const T = terminal(lm);
  const mono  = { fontFamily: MONO, fontVariantNumeric: "tabular-nums" };
  const cap   = { fontSize:13, letterSpacing:".07em", textTransform:"uppercase", color:T.dim, fontWeight:600 };
  const panel = { background:T.panel, border:`2px solid ${T.line}`, boxShadow:T.shadow };
  const sub   = { fontSize:14, color:T.soft, lineHeight:1.6 };
  const btnGhost = { ...mono, fontSize:15, fontWeight:500, padding:"7px 12px", borderRadius:2,
    background:"transparent", color:T.soft, border:`2px solid ${T.line2}`, whiteSpace:"nowrap", cursor:"pointer" };
  const btnSolid = { ...btnGhost, background:T.btnBg, color:T.btnFg, border:"2px solid transparent", fontWeight:600 };
  const btnMini  = { ...mono, fontSize:14, fontWeight:600, padding:"4px 8px", borderRadius:2,
    background:"transparent", color:T.soft, border:`2px solid ${T.line2}`, cursor:"pointer" };
  const inp = { ...mono, width:"100%", background:T.bg, border:`2px solid ${T.line2}`, borderRadius:2,
    padding:"7px 9px", fontSize:15, color:T.ink, outline:"none", boxSizing:"border-box" };
  const lbl = { ...cap, display:"block", marginBottom:5 };
  const otColor = r => r == null ? T.faint : r >= 80 ? T.green : r >= 60 ? T.over : T.red;

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

  // ── the shared ruler ────────────────────────────────────────────────────────
  const days = (a, b) => Math.round((new Date(b) - new Date(a)) / 86400000);
  /* One delivery, reduced to the two things the ruler draws: how long it took, and
     whether it beat the date it had promised. `late === null` means the order was
     logged without an expected date, so it cannot be scored either way — drawn grey
     rather than quietly counted as a success. */
  const deliveriesOf = sup => (sup.orders || []).filter(o => o.receivedDate).map(o => ({
    id: o.id, d: days(o.orderedDate, o.receivedDate),
    late: o.expectedDate ? days(o.expectedDate, o.receivedDate) > 0 : null,
    mode: o.freightMode || null,
  })).filter(x => Number.isFinite(x.d) && x.d >= 0);
  const openOf = sup => (sup.orders || []).filter(o => !o.receivedDate && o.expectedDate)
    .map(o => ({ id: o.id, d: days(o.orderedDate, o.expectedDate) }))
    .filter(x => Number.isFinite(x.d) && x.d >= 0);
  /* One axis for every supplier, so the rows can be read against each other. A
     per-row axis would rescale each supplier to fill its own track and make a
     6-day air line look exactly like a 70-day sea one. */
  const axisMax = (() => {
    let m = 0;
    supList.forEach(s => {
      deliveriesOf(s).forEach(x => { if (x.d > m) m = x.d; });
      openOf(s).forEach(x => { if (x.d > m) m = x.d; });
      Object.values(s.leadTimes || {}).forEach(l => { const b = Number(l?.baseline); if (b > m) m = b; });
    });
    const step = m > 120 ? 40 : m > 60 ? 20 : 10;
    return Math.max(30, Math.ceil((m * 1.06) / step) * step);
  })();
  const axisTicks = (() => {
    const step = axisMax / 4;
    return [0, 1, 2, 3, 4].map(i => Math.round(step * i));
  })();
  /* The quoted line is only honest when one freight lane accounts for nearly all of
     the history. A supplier running sea and air at once has two quotes and no single
     one to draw, so it gets none and says so instead. */
  const quotedOf = (sup, dels) => {
    const byMode = {};
    dels.forEach(x => { if (x.mode) byMode[x.mode] = (byMode[x.mode] || 0) + 1; });
    const modes = Object.entries(byMode).sort((a, b) => b[1] - a[1]);
    const tagged = modes.reduce((a, [, n]) => a + n, 0);
    if (!modes.length) return null;
    const [mode, n] = modes[0];
    if (tagged > 0 && n / tagged < 0.8) return { mixed: true };
    const base = Number(sup.leadTimes?.[mode]?.baseline);
    return base > 0 ? { mode, label: FREIGHT_MODES[mode], days: base } : null;
  };

  /* One dot per delivery, stacked where several took the same time.

     This replaced a strip of 3px hairlines with an average-to-P80 band shaded behind
     them. That plot was honest but unreadable: a row of thin ticks is a texture rather
     than a figure, you cannot count it, and the shaded band read as decoration nobody
     could name. A binned histogram was tried first and was worse — eighteen deliveries
     collapse into three fat slabs that lose the shape entirely, and a supplier with one
     delivery renders as a full-height mountain.

     Dots keep every delivery at its true position, stay countable, scale from one
     delivery to thirty, and need no statistical vocabulary: the pile IS the typical
     wait and the stragglers are visibly stragglers. */
  const DOT = 10, DOT_GAP = 3, LABEL_H = 15, PLOT_H = 52;
  const figCap  = { fontSize:13, letterSpacing:".1em", textTransform:"uppercase", color:T.dim,
                    fontWeight:600, whiteSpace:"nowrap" };
  const figVal  = { fontSize:28, fontWeight:600, lineHeight:1.1, marginTop:5, whiteSpace:"nowrap" };
  const figUnit = { fontFamily:SANS, fontSize:16.5, fontWeight:500, marginLeft:4, color:T.dim };

  /* One delivery row, shared by the supplier-wide history and the per-product one, so
     the two can never drift into describing the same delivery differently. */
  const deliveryRow = (sup, ord, { showSku = true } = {}) => {
    const lt = Math.round((new Date(ord.receivedDate) - new Date(ord.orderedDate)) / 86400000);
    const variance = ord.expectedDate
      ? Math.round((new Date(ord.receivedDate) - new Date(ord.expectedDate)) / 86400000)
      : null;
    return (
      <div key={ord.id} style={{ display:"flex", alignItems:"center", gap:12, padding:"7px 0", borderTop:`2px solid ${T.line}` }}>
        <div style={{ flex:1, minWidth:0 }}>
          {showSku && ord.skuId && (
            <div style={{ fontSize:14.5, fontWeight:600, color:T.blueFg, whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>
              {skuList.find(x => x.id === ord.skuId)?.name || ord.skuId}
            </div>
          )}
          <div style={{ ...mono, display:"flex", alignItems:"center", gap:9, flexWrap:"wrap", fontSize:14, marginTop:1 }}>
            <span style={{ color:T.ink }}>{isoToDisplay(ord.orderedDate)}</span>
            <span style={{ color:T.faint }}>→</span>
            <span style={{ color:T.green }}>{isoToDisplay(ord.receivedDate)} ({lt}d)</span>
            {ord.freightMode && FREIGHT_MODES[ord.freightMode] && (
              <span style={{ fontSize:13, padding:"1px 6px", background:T.sunken, border:`2px solid ${T.line}`, color:T.soft }}>{FREIGHT_MODES[ord.freightMode]}</span>
            )}
            {variance !== null && (
              <span title={`Against the ${isoToDisplay(ord.expectedDate)} expected date.`}
                style={{ fontWeight:600, color: variance <= 0 ? T.green : T.red }}>
                {variance > 0 ? `+${variance}d late` : variance === 0 ? "on time" : `${Math.abs(variance)}d early`}
              </span>
            )}
            {ord.qty > 0 && <span style={{ color:T.faint }}>{ord.qty.toLocaleString()} units</span>}
          </div>
          {ord.notes && <div style={{ ...sub, fontStyle:"italic", marginTop:2 }}>{ord.notes}</div>}
        </div>
        <button onClick={() => removeOrder(sup.id, ord.id)} title="Remove this delivery"
          style={{ ...mono, fontSize:14, background:"transparent", border:"none", color:T.faint, cursor:"pointer", padding:2 }}>✕</button>
      </div>
    );
  };

  const Ruler = ({ sup, st }) => {
    const dels = deliveriesOf(sup), open = openOf(sup);
    const q = quotedOf(sup, dels);
    const pct = d => Math.min(100, Math.max(0, d / axisMax * 100));
    const lateN = dels.filter(x => x.late === true).length;
    const unscoredN = dels.filter(x => x.late === null).length;

    /* Stack anything landing within ~2% of the axis into one column, so dots touch
       rather than overlap. Sorted first so the stack order is stable between renders. */
    const cols = [];
    const placed = [...dels].sort((a, b) => a.d - b.d).map(x => {
      const p = pct(x.d);
      let c = cols.find(k => Math.abs(k.p - p) < 2.2);
      if (!c) { c = { p, n: 0 }; cols.push(c); }
      const row = c.n; c.n += 1;
      return { ...x, p: c.p, row };
    });
    const maxRow = placed.length ? Math.max(...placed.map(x => x.row)) : 0;

    return (
      <div style={{ minWidth:0 }}>
        <div style={{ position:"relative", height:LABEL_H + PLOT_H }}>
          {/* The figure the tool orders against. Its label owns a band at the top so it
              can never collide with a dot or an open-order marker. */}
          {st.p80 != null && (
            <>
              <div style={{ position:"absolute", left:`${pct(st.p80)}%`, top:LABEL_H, bottom:0, width:2,
                marginLeft:-1, background:T.amber, opacity:.75, pointerEvents:"none" }} />
              <div title={`Planning figure: 80% of deliveries arrived within ${st.p80} days.`}
                style={{ ...mono, position:"absolute", left:`${pct(st.p80)}%`, top:0, fontSize:12,
                  color:T.amber, whiteSpace:"nowrap",
                  transform: pct(st.p80) > 78 ? "translateX(-100%)" : "translateX(-50%)" }}>
                plans on {st.p80}d
              </div>
            </>
          )}
          {q && !q.mixed && (
            <div title={`${q.label} baseline: ${q.days} days`}
              style={{ position:"absolute", left:`${pct(q.days)}%`, top:LABEL_H, bottom:0, width:1,
                background:T.line2, pointerEvents:"none" }} />
          )}
          {/* 2px ring in the surface colour separates touching dots — a gap, not a stroke. */}
          {placed.map(x => (
            <div key={x.id}
              title={`${x.d} days${x.late === null ? " · no expected date, not scored" : x.late ? " · after the expected date" : " · within the expected date"}`}
              style={{ position:"absolute", left:`${x.p}%`, bottom:x.row * (DOT + DOT_GAP),
                width:DOT, height:DOT, marginLeft:-DOT / 2, borderRadius:"50%",
                background: x.late === null ? T.faint : x.late ? T.red : T.green,
                boxShadow:`0 0 0 2px ${T.panel}` }} />
          ))}
          {open.map(x => (
            <div key={x.id} title={`Open order · expected day ${x.d}`}
              style={{ position:"absolute", left:`${pct(x.d)}%`,
                bottom:Math.min((maxRow + 1) * (DOT + DOT_GAP), PLOT_H - 10),
                width:8, height:8, marginLeft:-4, border:`2px solid ${T.soft}`,
                transform:"rotate(45deg)", boxShadow:`0 0 0 2px ${T.panel}` }} />
          ))}
        </div>
        <div style={{ borderTop:`2px solid ${T.line2}`, position:"relative", height:16, marginTop:3 }}>
          {axisTicks.map((d, i) => (
            <span key={i} style={{ ...mono, position:"absolute", left:`${pct(d)}%`, fontSize:13.5, color:T.faint,
              transform: i === 0 ? "none" : i === 4 ? "translateX(-100%)" : "translateX(-50%)" }}>{d}d</span>
          ))}
        </div>
        {/* An axis of bare numbers makes the reader work out what they measure. Say it. */}
        <div style={{ fontSize:13.5, color:T.faint, marginTop:1 }}>days from order to arrival</div>
        {/* A sentence, not a tally — "3 of 18 arrived late" needs no decoding.
            The two caveats used to be a bare ⚑ with the explanation hidden in a
            tooltip, which is the squint-and-guess this card is meant to remove.
            They are short enough to just say. */}
        <div style={{ fontSize:15, color:T.soft, marginTop:8 }}>
          {dels.length === 0 ? "No deliveries logged yet."
            : lateN > 0
              ? <><b style={{ ...mono, color:T.red }}>{lateN}</b> of <b style={{ ...mono, color:T.ink }}>{dels.length}</b> arrived after the promised date</>
              : <>All <b style={{ ...mono, color:T.ink }}>{dels.length}</b> arrived on time</>}
          {unscoredN > 0 && (
            <span style={{ color:T.faint }}>
              {" · "}{unscoredN} logged with no promised date, so not counted here
            </span>
          )}
          {q?.mixed && <span style={{ color:T.faint }}>{" · "}mixed shipping methods, so no single quoted time fits</span>}
        </div>
      </div>
    );
  };

  return (
    <div style={{ background:T.bg, color:T.ink, minHeight:"100%", padding:"20px 24px 36px", fontFamily:SANS,
      flex:1, overflowY:"auto" }}>
      <style>{`.sup-row:hover{background:${T.sunken};} .sup-sku:hover{background:${T.sunken};}
        .ren{opacity:0;transition:opacity .12s} .sup-row:hover .ren{opacity:.55} .ren:hover{opacity:1!important}`}</style>

      {/* ── header ───────────────────────────────────────────────────────── */}
      <div style={{ display:"flex", alignItems:"flex-start", justifyContent:"space-between", gap:16, marginBottom:14 }}>
        <div>
          <div style={{ fontSize:28, fontWeight:700, letterSpacing:"-.03em" }}>Supplier reliability</div>
          <div style={{ fontSize:15.5, color:T.soft, marginTop:4 }}>
            How reliably each supplier meets the dates it promised.
          </div>
        </div>
        <div style={{ display:"flex", alignItems:"center", gap:9 }}>
          {supList.length > 0 && (
            <button onClick={onExport} disabled={!!exporting} style={{ ...btnGhost, opacity: exporting ? .6 : 1 }}
              title="Download all suppliers and their KPIs + order history as Excel">
              {exporting ? "Preparing…" : "Export Excel"}
            </button>
          )}
          <button onClick={() => setShowAddSup(v => !v)} style={btnSolid}>Add supplier</button>
        </div>
      </div>

      {/* ── fleet-wide figures ───────────────────────────────────────────── */}
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
        /* Four equal columns, centred, rather than four blocks packed against the
           left edge with the rest of a 1400px strip left empty. The dividers are
           drawn BETWEEN cells (`:not(:first-child)` in effect, via the index) so the
           strip does not end in a rule pointing at nothing. */
        const fig = (label, val, note, color, i = 0) => (
          <div key={label} style={{ textAlign:"center", padding:"0 18px",
            borderLeft: i === 0 ? "none" : `2px solid ${T.line}` }}>
            <div style={{ ...cap, justifyContent:"center", display:"flex" }}>{label}</div>
            <div style={{ ...mono, fontSize:30, fontWeight:500, letterSpacing:"-.03em", lineHeight:1, marginTop:7, color: color || T.ink }}>{val}</div>
            {note && <div style={{ ...mono, fontSize:14, color:T.faint, marginTop:6, lineHeight:1.45 }}>{note}</div>}
          </div>
        );
        return (
          <div style={{ ...panel, padding:"16px 18px", display:"grid",
            gridTemplateColumns:"repeat(4, minmax(0,1fr))", alignItems:"start" }}>
            {fig("Suppliers", supList.length, supList.length === 1 ? "one on file" : "on file", undefined, 0)}
            {fig("On-time rate", onTime != null ? `${onTime}%` : "-",
              rated.length ? `across ${rated.reduce((a, x) => a + x.st.n, 0)} deliveries`
                : timed.length ? `${unpromisedAll} logged without an expected date`
                : "no completed orders yet",
              onTime == null ? undefined : onTime >= 90 ? T.green : onTime >= 75 ? T.over : T.red, 1)}
            {fig("Avg lead time", avgLead != null ? `${avgLead}d` : "-",
              laneBits.length ? `P80 · ${laneBits.join(" · ")}`
                : avgLead != null ? "order → arrival" : "no completed orders yet", undefined, 2)}
            {fig("In transit", inTransit, inTransit ? "open orders" : "none open",
              inTransit ? T.blue : undefined, 3)}
          </div>
        );
      })()}

      {showAddSup && (
        <div style={{ ...panel, padding:12, marginTop:12, display:"flex", gap:8 }}>
          <input type="text" value={addingSupName} onChange={e => setAddingSupName(e.target.value)}
            onKeyDown={e => e.key === "Enter" && createSupplier()}
            placeholder="Supplier name" style={{ ...inp, flex:1 }} autoFocus />
          <button onClick={createSupplier} style={btnSolid}>Add</button>
          <button onClick={() => { setShowAddSup(false); setAddingSupName(""); }} style={btnGhost}>Cancel</button>
        </div>
      )}

      {supList.length === 0 && (
        <div style={{ ...panel, padding:"34px 24px", textAlign:"center", marginTop:12 }}>
          <div style={{ fontSize:16.5, fontWeight:600 }}>No suppliers yet</div>
          <div style={{ fontSize:15, color:T.soft, marginTop:6, lineHeight:1.6 }}>
            Add a supplier, then link orders with “Mark Order In Transit” on any SKU. Orders appear here for tracking.
          </div>
        </div>
      )}

      {/* ── the rulers ───────────────────────────────────────────────────── */}
      {/* The legend is its own card now, and so is every supplier.
          They used to be one tall panel with the suppliers divided by internal
          rules, which meant an expanded supplier and the two collapsed ones under it
          all read as a single object — you could not see where one ended. Separate
          cards with real space between them make each supplier a thing you can look
          at on its own, and the legend stops looking like a heading for the first
          row only. */}
      {supList.length > 0 && (
        <>
        <div style={{ ...panel, marginTop:12 }}>
          <div style={{ padding:"16px 20px 13px" }}>
            <div>
              <div style={{ fontSize:20.5, fontWeight:600, letterSpacing:"-.02em" }}>Lead time, as actually delivered</div>
              <div style={{ fontSize:14.5, color:T.soft, marginTop:3 }}>One dot per delivery, placed on the day it landed. Stacks mean several took the same time.</div>
            </div>
            <div style={{ display:"flex", flexWrap:"wrap", gap:"8px 18px", fontSize:15, color:T.soft, marginTop:10 }}>
              <span style={{ display:"flex", alignItems:"center", gap:6 }}><span style={{ width:10, height:10, borderRadius:"50%", background:T.green }} /> arrived on time</span>
              <span style={{ display:"flex", alignItems:"center", gap:6 }}><span style={{ width:10, height:10, borderRadius:"50%", background:T.red }} /> arrived late</span>
              <span style={{ display:"flex", alignItems:"center", gap:6 }}><span style={{ width:8, height:8, border:`2px solid ${T.ink}`, transform:"rotate(45deg)" }} /> still on its way</span>
              {/* The shaded average→P80 band is gone with the strip plot. What replaced
                  it is a single labelled line at the figure the tool actually orders
                  against, which is the only one of the two a buyer acts on. */}
              <span style={{ display:"flex", alignItems:"center", gap:6, cursor:"help" }}
                title="The planning figure: 80% of this supplier's deliveries arrived within this many days.">
                <span style={{ width:2, height:13, background:T.amber }} /> what we plan on (P80)
              </span>
            </div>
          </div>

        </div>

        <div style={{ display:"flex", flexDirection:"column", gap:12, marginTop:12 }}>
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
              <div key={sup.id} style={{ ...panel }}>

                {/* ── the row ───────────────────────────────────────────── */}
                {/* Two stacked bands, not one row of seven columns.
                    Name, subtitle, three figures, a chart, an axis, a count and a delete
                    button all sat on a single line — eight things to parse at once, each
                    squeezed into 74–116px. For someone reading without perfect eyesight
                    that is a squint and a guess. Identity and figures share the top band;
                    the chart gets the full width underneath, which also spreads the dots
                    out enough to actually see them. */}
                <div className="sup-row" onClick={() => setSelected(isOpen ? null : sup.id)}
                  style={{ padding:"16px 20px 18px", cursor:"pointer",
                    background: isOpen ? T.sunken : "transparent" }}>
                 <div style={{ display:"flex", alignItems:"flex-start", justifyContent:"space-between", gap:24 }}>
                  <div style={{ display:"flex", alignItems:"flex-start", gap:10, minWidth:0 }}>
                  <span style={{ ...mono, fontSize:14, color:T.dim, marginTop:3, transform: isOpen ? "rotate(90deg)" : "none", transition:"transform .12s" }}>▸</span>

                  <div style={{ minWidth:0 }}>
                    {/* Renaming belongs beside the name, not in a row halfway down the card
                        where you'd never look for it. */}
                    {renaming === sup.id ? (
                      <div style={{ display:"flex", alignItems:"center", gap:6 }} onClick={e => e.stopPropagation()}>
                        <input autoFocus type="text" defaultValue={sup.name}
                          onChange={e => setEditName(e.target.value)}
                          onKeyDown={e => {
                            if (e.key === "Enter") { renameSupplier(sup.id); setRenaming(null); }
                            if (e.key === "Escape") { setEditName(""); setRenaming(null); }
                          }}
                          style={{ ...inp, width:200, fontSize:15.5, fontWeight:600, borderColor:T.amber }} />
                        <button onClick={() => { renameSupplier(sup.id); setRenaming(null); }} style={btnMini}>Save</button>
                        <button onClick={() => { setEditName(""); setRenaming(null); }} style={{ ...btnMini, border:"none", color:T.faint }}>Cancel</button>
                      </div>
                    ) : (
                      <div style={{ display:"flex", alignItems:"center", gap:7 }}>
                        <span style={{ fontSize:18.5, fontWeight:600, letterSpacing:"-.01em",
                          whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>{sup.name}</span>
                        <button title="Rename supplier" className="ren"
                          onClick={e => { e.stopPropagation(); setEditName(sup.name); setRenaming(sup.id); }}
                          style={{ ...mono, fontSize:13, background:"transparent", border:"none", color:T.faint, cursor:"pointer", padding:2 }}>✎</button>
                      </div>
                    )}
                    <div style={{ fontSize:15, color:T.soft, marginTop:3 }}>
                      {sup.skuIds?.length || 0} product{(sup.skuIds?.length || 0) !== 1 ? "s" : ""}
                      {inTransit.length > 0 && <span style={{ color:T.blue }}> · {inTransit.length} in transit</span>}
                      {st.n > 0 && ` · ${st.n} ${st.n === 1 ? "delivery" : "deliveries"} logged`}
                    </div>
                  </div>
                  </div>

                  {/* Three figures again. The average was dropped from this row on the
                      grounds that the chart already drew it — the left edge of the shaded
                      average-to-P80 band. That band went when the strip plot became a dot
                      plot, so the justification went with it and the average was left
                      nowhere on the page. It is also the number most people reach for
                      first, so it earns its place next to the one we actually plan on. */}
                  {/* Caption above the number, matching the Fleet cards. Reading a label
                      before a figure is easier than inferring one after it. */}
                  <div style={{ display:"flex", gap:34, flexShrink:0 }}>
                    <div>
                      <div style={figCap}>On time</div>
                      <div style={{ ...mono, ...figVal, color: st.onTimeRate == null ? T.faint : otColor(st.onTimeRate) }}>
                        {st.onTimeRate != null ? `${st.onTimeRate}%` : "-"}
                      </div>
                    </div>
                    <div>
                      <div style={figCap}>
                        <Tip text="The plain average of every completed delivery. Don't plan against it: half of all deliveries arrive later than average. Order dates use the P80 instead.">Average wait</Tip>
                      </div>
                      <div style={{ ...mono, ...figVal, color: st.avg == null ? T.faint : T.soft }}>
                        {st.avg != null ? <>{st.avg}<span style={figUnit}>days</span></> : "-"}
                      </div>
                    </div>
                    <div>
                      <div style={figCap}>
                        <Tip text="The planning figure. 80% of this supplier's deliveries arrived within this many days, so ordering against it leaves you short only about one time in five. The Backtest and product pages call it P80 too.">We plan on</Tip>
                      </div>
                      <div style={{ ...mono, ...figVal, color: st.p80 == null ? T.faint : T.amber }}>
                        {st.p80 != null ? <>{st.p80}<span style={figUnit}>days</span></> : "-"}
                      </div>
                      <div style={{ fontSize:13.5, color:T.faint, marginTop:3, whiteSpace:"nowrap" }}>P80 · 80th percentile</div>
                    </div>
                    <button onClick={e => { e.stopPropagation(); deleteSupplier(sup.id); }}
                      title={`Delete ${sup.name}`}
                      style={{ ...mono, fontSize:15, background:"transparent", border:"none", color:T.faint,
                        cursor:"pointer", padding:2, alignSelf:"flex-start" }}>✕</button>
                  </div>
                 </div>

                 {/* the chart, full width beneath */}
                 <div style={{ marginTop:16 }}><Ruler sup={sup} st={st} /></div>
                </div>

                {/* ── expanded ──────────────────────────────────────────── */}
                {isOpen && (
                  <div style={{ borderTop:`2px solid ${T.line}`, borderLeft:`2px solid ${T.amber}`,
                    background:T.sunken, padding:"16px 18px" }}>

                    {/* Two bands, not three columns. Three side-by-side tables in a 1180px
                        card left every one of them about 340px: the supplier's method table
                        ran its own header into itself ("MethodBaseline"), "Ground" clipped to
                        "Groun", and the product table — the one that now opens into a whole
                        per-product history — was the narrowest of the three. So the two
                        summary blocks share the top row and the product table gets the full
                        width underneath, where it does its work. Placement is by grid
                        coordinates so the reading order in the markup stays methods →
                        products → in transit. */}
                    <div style={{ display:"grid", gridTemplateColumns:"minmax(0,1.55fr) minmax(280px,1fr)",
                      gap:"20px 22px", alignItems:"start" }}>

                      {/* ── methods and baselines ──────────────────────── */}
                      {/* Everything about one shipping method on a single line: the baseline
                          you plan from, and what the deliveries actually did. Lanes never
                          blend — a 44-day sea crossing averaged with a 6-day air run is wrong
                          for both, and the on-time rate would be mixing two entirely different
                          promises.

                          The header and the rows share ONE column definition. They were
                          separate grids with `auto` columns before, so each sized to its own
                          content and "On-time" ended up sitting over a column of dashes. */}
                      {(() => {
                        const byLane = supplierStatsByLane(sup);
                        const COLS = { display:"grid", gridTemplateColumns:"minmax(0,1fr) 100px 66px 66px 72px 62px", gap:"0 10px", alignItems:"center" };
                        const num  = { ...mono, textAlign:"right", fontSize:15, fontWeight:600, color:T.ink };
                        const dash = { ...mono, textAlign:"right", fontSize:15, color:T.faint };
                        const unpromised = (sup.orders || []).filter(o => o.receivedDate && !o.expectedDate).length;
                        return (
                          <div style={{ gridColumn:"1", gridRow:"1", minWidth:0 }}>
                            <div style={{ marginBottom:9 }}>
                              <div style={cap}>Every product from {sup.name}</div>
                              <div style={{ ...mono, fontSize:13, color:T.faint, marginTop:3 }}>
                                {st.n} completed {st.n === 1 ? "delivery" : "deliveries"}
                                {inTransit.length > 0 && ` · ${inTransit.length} in transit`}
                              </div>
                            </div>
                            <div style={{ ...COLS, ...cap, fontSize:12.5, letterSpacing:".07em", paddingBottom:6, borderBottom:`2px solid ${T.line}` }}>
                              <span>Method</span><span style={{ textAlign:"right" }}>Baseline</span>
                              <span style={{ textAlign:"right" }}>Avg</span><span style={{ textAlign:"right" }}>P80</span>
                              {/* Counts shipments that ARRIVED, not orders placed — an open PO
                                  isn't in it. Same figure the product page shows, now under the
                                  same name. */}
                              <span style={{ textAlign:"right" }}>On-time</span><span style={{ textAlign:"right" }}>Deliv.</span>
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
                                <div key={k} style={{ ...COLS, padding:"6px 0", borderBottom:`2px solid ${T.line}` }}>
                                  <div style={{ minWidth:0 }}>
                                    <div style={{ fontSize:15, fontWeight:500, color:T.ink }}>{label}</div>
                                    {source && (
                                      <div title={source} style={{ ...mono, fontSize:12.5, whiteSpace:"nowrap", overflow:"hidden",
                                        textOverflow:"ellipsis", color: ownSet ? T.faint : T.amber }}>{source}</div>
                                    )}
                                  </div>
                                  <div style={{ display:"flex", alignItems:"center", gap:3 }}>
                                    <input type="number" min="1" placeholder={mixed ? "mixed" : "-"}
                                      value={ownSet ? own : (derived ?? "")}
                                      onChange={e => setSupLane(sup.id, k, e.target.value)}
                                      title={ownSet ? "Set on this supplier. Clear it to fall back to the product's own number."
                                            : source ? `Showing the ${source}. Typing here sets a supplier-wide default instead.`
                                            : `No ${label.toLowerCase()} lead time known yet.`}
                                      style={{ ...inp, textAlign:"right", padding:"3px 5px", fontSize:14.5,
                                        fontStyle: ownSet ? "normal" : "italic", color: ownSet ? T.ink : T.soft }} />
                                    <span style={{ ...mono, fontSize:12.5, color:T.faint }}>d</span>
                                  </div>
                                  <span style={v.avg != null ? num : dash}>{v.avg != null ? `${v.avg}d` : "-"}</span>
                                  <span style={{ ...(v.p80 != null ? num : dash), color: v.n >= LEAD_TIME_MIN_DELIVERIES ? T.amber : (v.p80 != null ? T.ink : T.faint) }}>
                                    {v.p80 != null ? `${v.p80}d` : "-"}
                                  </span>
                                  <span style={{ ...(v.onTimeRate != null ? num : dash), color: v.onTimeRate != null ? otColor(v.onTimeRate) : T.faint }}
                                    title={v.onTimeRate == null && v.n > 0
                                      ? "These deliveries were logged without an expected date, so there's nothing to score against."
                                      : undefined}>
                                    {v.onTimeRate != null ? `${v.onTimeRate}%` : "-"}
                                  </span>
                                  <span style={{ ...dash, color: v.n ? T.soft : T.faint }}>{v.n || "-"}</span>
                                </div>
                              );
                            })}
                            <div style={{ ...sub, marginTop:8 }}>
                              Products here plan from the baseline until that method has {LEAD_TIME_MIN_DELIVERIES} deliveries of its own. A product that sets its own number keeps it.
                              {byLane.untagged > 0 && ` ${byLane.untagged} untagged.`}
                              {unpromised > 0 && ` On-time skips ${unpromised} ${unpromised === 1 ? "delivery" : "deliveries"} logged without an expected date.`}
                            </div>
                          </div>
                        );
                      })()}

                      {/* ── assigned SKUs ──────────────────────────────── */}
                      {/* Assigned SKUs — one line each, expanding to that product's own three
                          methods. The supplier figures on the left are the whole book; this is
                          where a single product's numbers live, and they can differ. */}
                      <div style={{ gridColumn:"1 / -1", gridRow:"2", minWidth:0 }}>
                        <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:9 }}>
                          <span style={cap}>Assigned SKUs ({sup.skuIds?.length || 0})</span>
                          {/* Search-and-pick instead of a wall of chips: with a real catalogue
                              the chip list was hundreds of buttons deep and buried everything
                              below it. Collapsed by default, costs one click to open. */}
                          {unassigned.length > 0 && (
                            <div style={{ position:"relative" }} ref={assignOpen === sup.id ? assignRef : null}>
                              <button onClick={() => { setAssignOpen(assignOpen === sup.id ? null : sup.id); setAssignQuery(""); }}
                                style={{ ...btnMini, color:T.amber, borderColor:`${T.amber}66` }}>
                                + Assign product <span style={{ color:T.faint }}>({unassigned.length})</span>
                              </button>
                              {assignOpen === sup.id && (
                                <div style={{ position:"absolute", right:0, top:"100%", marginTop:4, width:280, zIndex:40,
                                  background:T.panel, border:`2px solid ${T.line2}`, boxShadow:"0 8px 28px rgba(0,0,0,.35)" }}>
                                  <div style={{ padding:8, borderBottom:`2px solid ${T.line}` }}>
                                    <input autoFocus type="text" value={assignQuery}
                                      onChange={e => setAssignQuery(e.target.value)}
                                      onKeyDown={e => {
                                        if (e.key === "Escape") setAssignOpen(null);
                                        // Enter assigns when the search has narrowed to one product
                                        if (e.key === "Enter" && assignMatches.length === 1) {
                                          toggleSkuAssign(sup.id, assignMatches[0].id); setAssignQuery("");
                                        }
                                      }}
                                      placeholder="Search products…" style={{ ...inp, fontSize:14.5, padding:"6px 8px" }} />
                                  </div>
                                  <div className="sku-scroll" style={{ maxHeight:256, overflowY:"auto" }}>
                                    {assignMatches.map(sku => (
                                      <button key={sku.id} onClick={() => { toggleSkuAssign(sup.id, sku.id); setAssignQuery(""); }}
                                        className="sup-sku"
                                        style={{ ...mono, display:"block", width:"100%", textAlign:"left", padding:"7px 11px",
                                          fontSize:14, color:T.soft, background:"transparent", border:"none", cursor:"pointer",
                                          whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>
                                        {sku.name}
                                      </button>
                                    ))}
                                    {assignMatches.length === 0 && (
                                      <div style={{ padding:"16px 11px", fontSize:14, textAlign:"center", color:T.faint }}>
                                        {assignQuery ? `No product matches “${assignQuery}”` : "Everything is assigned"}
                                      </div>
                                    )}
                                  </div>
                                  {assignMatches.length > 0 && (
                                    <div style={{ ...mono, padding:"5px 11px", borderTop:`2px solid ${T.line}`, fontSize:13, color:T.faint }}>
                                      {assignMatches.length} of {unassigned.length} shown
                                    </div>
                                  )}
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                        {skuList.length === 0 && <div style={{ ...sub, marginBottom:10 }}>No SKUs loaded yet</div>}

                        {(sup.skuIds?.length > 0) && (() => {
                          const SCOLS = { display:"grid", gridTemplateColumns:"minmax(0,1fr) 102px 80px 80px 108px", gap:"0 14px", alignItems:"center" };
                          return (
                            <div style={{ border:`2px solid ${T.line}` }}>
                              <div style={{ ...SCOLS, ...cap, fontSize:12.5, letterSpacing:".07em", padding:"6px 10px", background:T.panel, borderBottom:`2px solid ${T.line}` }}>
                                <span>Product</span><span style={{ textAlign:"right" }}>Method</span>
                                <span style={{ textAlign:"right" }}>Avg</span><span style={{ textAlign:"right" }}>P80</span>
                                <span style={{ textAlign:"right" }}>Deliveries</span>
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
                                /* Which lane this product actually plans on, named on the row.
                                   The number on its own said nothing about which promise it
                                   belongs to, and the three lanes are not comparable. */
                                const basis0   = pp ? leadTimeBasis(skuId, pp, suppliers, { ignoreOneOff: true }) : null;
                                const laneName = basis0?.mode ? FREIGHT_MODES[basis0.mode] : null;
                                return (
                                  <div key={skuId} style={{ borderTop: i > 0 ? `2px solid ${T.line}` : "none" }}>
                                    <div className={open2 ? "" : "sup-sku"} onClick={() => setOpenSku(open2 ? null : key)}
                                      style={{ ...SCOLS, padding:"7px 10px", cursor:"pointer", background: open2 ? T.bg : "transparent" }}>
                                      <div style={{ display:"flex", alignItems:"center", gap:6, minWidth:0 }}>
                                        <span style={{ ...mono, fontSize:12.5, color:T.faint, transform: open2 ? "rotate(90deg)" : "none" }}>▸</span>
                                        <div style={{ minWidth:0 }}>
                                          <div style={{ fontSize:14.5, color:T.ink, whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>{skuName}</div>
                                          {/* A product whose lead time was permanently changed is measuring
                                              a different arrangement to the one next to it, so the row has to
                                              say so — otherwise two rows of numbers look comparable when one
                                              covers three years and the other covers three weeks. */}
                                          {changedOn && (
                                            <div title={lastChange ? `${describeLeadTimeChange(lastChange)} on ${isoToDisplay(changedOn)}` : undefined}
                                              style={{ ...mono, fontSize:12.5, color:T.amber, whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>
                                              since {isoToDisplay(changedOn)}
                                            </div>
                                          )}
                                        </div>
                                      </div>
                                      <span style={{ ...mono, textAlign:"right", fontSize:14, color:T.soft }}>{laneName || "-"}</span>
                                      <span style={{ ...mono, textAlign:"right", fontSize:14.5, fontWeight:600, color: hasData ? T.ink : T.faint }}>
                                        {hasData ? `${st2.avg}d` : "-"}
                                      </span>
                                      <span style={{ ...mono, textAlign:"right", fontSize:14.5, fontWeight:600,
                                        color: hasData && st2.p80 != null ? (reliable ? T.amber : T.soft) : T.faint }}>
                                        {hasData && st2.p80 != null ? `${st2.p80}d` : "-"}
                                      </span>
                                      <span style={{ ...mono, textAlign:"right", fontSize:14, color: hasData ? T.soft : T.faint }}
                                        title={setAside > 0 ? `${setAside} earlier ${setAside === 1 ? "delivery" : "deliveries"} set aside by a lead-time change` : undefined}>
                                        {st2.n > 0 ? st2.n : "none"}
                                        {setAside > 0 && <span style={{ fontSize:12.5, color:T.faint }}> +{setAside}</span>}
                                        {hasData && !reliable && <span style={{ color:T.over }} title={`${LEAD_TIME_MIN_DELIVERIES - st2.n} more before a P80 is trusted`}> ⚠</span>}
                                      </span>
                                    </div>
                                    {open2 && (
                                      <div style={{ padding:"8px 10px 12px", background:T.bg, borderTop:`2px solid ${T.line}` }}>
                                        {changedOn && (
                                          <div style={{ ...sub, border:`2px solid ${T.amber}44`, background:`${T.amber}10`, padding:"7px 9px", marginBottom:8 }}>
                                            <b style={{ color:T.amber }}>
                                              Lead time changed {isoToDisplay(changedOn)}
                                              {lastChange && !lastChange.legacy ? `: ${describeLeadTimeChange(lastChange)}` : ""}.
                                            </b>{" "}
                                            Figures here cover {st2.n === 0 ? "no deliveries" : `the ${st2.n} ${st2.n === 1 ? "delivery" : "deliveries"}`} since.
                                            {setAside > 0 && ` ${setAside} earlier ${setAside === 1 ? "one is" : "ones are"} set aside. Nothing was deleted.`}
                                          </div>
                                        )}
                                        {pp ? (() => {
                                          const tracks2 = leadTimeTracks(skuId, pp, suppliers);
                                          const basis   = leadTimeBasis(skuId, pp, suppliers, { ignoreOneOff: true });
                                          const OWN = { display:"grid", gridTemplateColumns:"minmax(0,1fr) 102px 80px 80px 108px", gap:"0 14px", alignItems:"center" };
                                          return (
                                            <>
                                              <div style={{ ...OWN, ...cap, fontSize:12.5, letterSpacing:".07em", paddingBottom:4 }}>
                                                <span>Its own methods</span><span /><span style={{ textAlign:"right" }}>Baseline</span>
                                                <span style={{ textAlign:"right" }}>P80</span><span style={{ textAlign:"right" }}>Deliveries</span>
                                              </div>
                                              {tracks2.map(t => (
                                                <div key={t.mode} style={{ ...OWN, padding:"2px 0", fontSize:14 }}>
                                                  <span style={{ color: t.used ? T.ink : T.faint, whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis" }}>
                                                    {t.label}
                                                    {t.isDefault && <span style={{ ...mono, fontSize:12.5, textTransform:"uppercase", letterSpacing:".06em", color:T.amber }}> default</span>}
                                                  </span>
                                                  <span />
                                                  <span title={t.inherited ? `Inherited from ${sup.name}` : undefined}
                                                    style={{ ...mono, textAlign:"right", fontStyle: t.inherited ? "italic" : "normal",
                                                      color: t.baseline == null ? T.faint : t.inherited ? T.soft : T.ink }}>
                                                    {t.baseline != null ? `${t.baseline}d` : "-"}
                                                  </span>
                                                  <span style={{ ...mono, textAlign:"right", fontWeight: t.p80 != null ? 600 : 400,
                                                    color: t.p80 != null ? T.green : T.faint }}>
                                                    {t.p80 != null ? `${t.p80}d` : "-"}
                                                  </span>
                                                  <span style={{ ...mono, textAlign:"right", color:T.soft }}>{t.n || "-"}</span>
                                                </div>
                                              ))}
                                              <div style={{ ...sub, marginTop:8 }}>
                                                <b style={{ color:T.ink }}>Plans on {basis.label}.</b>{" "}
                                                {basis.detail}
                                                {tracks2.some(t => t.inherited) && <> Italic baselines come from {sup.name}.</>}
                                              </div>
                                            </>
                                          );
                                        })() : (
                                          <div style={sub}>Open this product to set its lead times per method.</div>
                                        )}
                                        {/* This product's OWN deliveries. The only history on
                                            this page used to be the supplier's whole pile, so
                                            "when did this one last arrive, and was it late" meant
                                            reading 29 products' orders and filtering by eye. The
                                            per-method figures above are for this product; its
                                            shipments should be too. */}
                                        {(() => {
                                          const hk = `${sup.id}|${skuId}`;
                                          const mine = completed.filter(o => o.skuId === skuId);
                                          const openMine = inTransit.filter(o => o.skuId === skuId);
                                          const lateMine = mine.filter(o => o.expectedDate
                                            && (new Date(o.receivedDate) - new Date(o.expectedDate)) > 0).length;
                                          return (
                                            <div style={{ marginTop:12, borderTop:`2px solid ${T.line}`, paddingTop:10 }}>
                                              <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", gap:12, flexWrap:"wrap" }}>
                                                <div style={{ fontSize:14.5, color:T.soft }}>
                                                  {mine.length === 0 ? "No deliveries logged for this product yet."
                                                    : <>{mine.length} {mine.length === 1 ? "delivery" : "deliveries"} for this product
                                                        {lateMine > 0 && <>, <b style={{ ...mono, color:T.red }}>{lateMine}</b> late</>}
                                                        {openMine.length > 0 && <span style={{ color:T.blue }}> · {openMine.length} on the way</span>}</>}
                                                </div>
                                                {mine.length > 0 && (
                                                  <button onClick={() => setShowSkuHist(p => ({ ...p, [hk]: !p[hk] }))}
                                                    style={{ ...btnMini, color:T.amber, borderColor:`${T.amber}66` }}>
                                                    {showSkuHist[hk] ? "Hide deliveries" : `View ${mine.length} ${mine.length === 1 ? "delivery" : "deliveries"}`}
                                                  </button>
                                                )}
                                              </div>
                                              {showSkuHist[hk] && mine.length > 0 && (
                                                <div style={{ marginTop:6 }}>
                                                  {mine.slice().reverse().map(ord => deliveryRow(sup, ord, { showSku:false }))}
                                                </div>
                                              )}
                                            </div>
                                          );
                                        })()}
                                        <button onClick={() => toggleSkuAssign(sup.id, skuId)}
                                          style={{ ...btnMini, marginTop:11, color:T.soft }}>
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

                      {/* ── in transit ─────────────────────────────────── */}
                      <div style={{ gridColumn:"2", gridRow:"1", minWidth:0 }}>
                        <div style={{ ...cap, marginBottom:9 }}>In transit ({inTransit.length})</div>
                        {inTransit.length === 0 && <div style={sub}>Nothing open with this supplier.</div>}
                        <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
                          {inTransit.map(ord => {
                            const daysLeft = ord.expectedDate
                              ? Math.round((new Date(ord.expectedDate) - new Date(todayStr())) / 86400000)
                              : null;
                            const skuName = ord.skuId ? skuList.find(s => s.id === ord.skuId)?.name || ord.skuId : null;
                            const isMarking = markingOrd?.supId === sup.id && markingOrd?.ordId === ord.id;
                            return (
                              <div key={ord.id} style={{ border:`2px solid ${T.blue}55`, background:`${T.blue}0f`, padding:"9px 11px" }}>
                                <div style={{ display:"flex", alignItems:"flex-start", justifyContent:"space-between", gap:10 }}>
                                  <div style={{ flex:1, minWidth:0 }}>
                                    {skuName && <div style={{ fontSize:14.5, fontWeight:600, color:T.blueFg }}>{skuName}</div>}
                                    <div style={{ ...mono, fontSize:14, color:T.ink, marginTop:2 }}>
                                      Ordered {isoToDisplay(ord.orderedDate)}
                                      {ord.qty > 0 && <span style={{ color:T.soft }}> · {ord.qty.toLocaleString()} units</span>}
                                    </div>
                                    {ord.expectedDate && (
                                      <div style={{ ...mono, fontSize:14, marginTop:2, display:"flex", gap:8, flexWrap:"wrap" }}>
                                        <span style={{ color:T.soft }}>Expected {isoToDisplay(ord.expectedDate)}</span>
                                        {daysLeft !== null && (
                                          <span style={{ fontWeight:600, color: daysLeft < 0 ? T.red : daysLeft <= 3 ? T.over : T.green }}>
                                            {daysLeft < 0 ? `${Math.abs(daysLeft)}d overdue` : daysLeft === 0 ? "due today" : `${daysLeft}d remaining`}
                                          </span>
                                        )}
                                      </div>
                                    )}
                                  </div>
                                  <button onClick={() => removeOrder(sup.id, ord.id)} title="Remove this order"
                                    style={{ ...mono, fontSize:14, background:"transparent", border:"none", color:T.faint, cursor:"pointer", padding:2 }}>✕</button>
                                </div>
                                <div style={{ marginTop:8 }}>
                                  {!isMarking ? (
                                    <button onClick={() => { setMarkingOrd({ supId: sup.id, ordId: ord.id }); setMarkDate(todayStr()); }}
                                      style={{ ...btnMini, color:T.green, borderColor:`${T.green}66` }}>Mark arrived</button>
                                  ) : (
                                    <div style={{ display:"flex", alignItems:"center", gap:6, flexWrap:"wrap" }}>
                                      <input type="date" value={markDate} onChange={e => setMarkDate(e.target.value)}
                                        style={{ ...inp, width:"auto", padding:"3px 6px", fontSize:14 }} />
                                      <button onClick={() => markReceived(sup.id, ord.id, markDate)}
                                        style={{ ...btnMini, background:T.green, color:T.onFill, border:"2px solid transparent" }}>✓ Confirm</button>
                                      <button onClick={() => setMarkingOrd(null)} style={{ ...btnMini, border:"none", color:T.faint }}>Cancel</button>
                                    </div>
                                  )}
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    </div>

                    {/* ── delivery history ───────────────────────────────── */}
                    <div style={{ marginTop:18, borderTop:`2px solid ${T.line}`, paddingTop:14 }}>
                      <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", gap:12, marginBottom:9 }}>
                        <div style={{ display:"flex", alignItems:"center", gap:12, minWidth:0 }}>
                          {/* Named against the per-product history above it. Two lists
                              headed "Delivery history" on one card would be a puzzle. */}
                          <span style={cap}>Every delivery from this supplier</span>
                          {completed.length > 0 && (
                            <button onClick={() => setShowHistory(p => ({ ...p, [sup.id]: !p[sup.id] }))}
                              style={{ ...mono, fontSize:14, background:"transparent", border:"none", color:T.amber, cursor:"pointer", padding:0 }}>
                              {showHistory[sup.id] ? "Hide" : `Show ${completed.length}`}
                            </button>
                          )}
                        </div>
                        {showHistory[sup.id] && completed.length > 0 && (
                          <input type="text" value={histQuery[sup.id] || ""}
                            onChange={e => setHistQuery(p => ({ ...p, [sup.id]: e.target.value }))}
                            placeholder="Search by product…"
                            style={{ ...inp, width:220, padding:"5px 8px", fontSize:14 }} />
                        )}
                      </div>

                      {addingOrder === sup.id && (
                        <div style={{ ...panel, padding:14, marginBottom:12 }}>
                          <div style={{ display:"grid", gridTemplateColumns:"repeat(2,1fr)", gap:12 }}>
                            <div style={{ gridColumn:"span 2" }}>
                              <label style={lbl}>Product</label>
                              <select value={orderForm.skuId || ""} onChange={e => setOrderForm(p => ({ ...p, skuId: e.target.value }))} style={inp}>
                                <option value="">None (don&apos;t change stock)</option>
                                {skuList.map(s => <option key={s.id} value={s.id}>{s.name} ({s.id})</option>)}
                              </select>
                            </div>
                            <div><label style={lbl}>Order date</label><input type="date" value={orderForm.orderedDate || ""} onChange={e => setOrderForm(p => ({ ...p, orderedDate: e.target.value }))} style={inp} /></div>
                            <div><label style={lbl}>Quantity</label><input type="number" min="0" value={orderForm.qty || ""} onChange={e => setOrderForm(p => ({ ...p, qty: e.target.value }))} style={inp} placeholder="e.g. 500" /></div>
                            <div>
                              <label style={lbl}>Shipping method (optional)</label>
                              <select value={orderForm.freightMode || Object.keys(FREIGHT_MODES)[0]}
                                onChange={e => setOrderForm(p => ({ ...p, freightMode: e.target.value }))} style={inp}>
                                {Object.entries(FREIGHT_MODES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                              </select>
                            </div>
                            <div>
                              <label style={lbl}>Expected date</label>
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
                                      onChange={e => setOrderForm(p => ({ ...p, expectedDate: e.target.value }))} style={inp} />
                                    <div style={{ ...sub, marginTop:3 }}>
                                      {orderForm.expectedDate ? "Your date."
                                        : basis ? <span title={basis.detail}>{basis.label}{basis.disagrees ? " (deliveries disagree)" : ""}</span>
                                        : auto ? `${FREIGHT_MODES[mode]} baseline for ${sup.name}.`
                                        : sku ? "No lead time known for this product yet."
                                        : "No baseline for this method. On-time can't be scored without a date."}
                                    </div>
                                  </>
                                );
                              })()}
                            </div>
                            {!orderForm.notArrived && (
                              <div><label style={lbl}>Arrival date</label><input type="date" value={orderForm.receivedDate || todayStr()} onChange={e => setOrderForm(p => ({ ...p, receivedDate: e.target.value }))} style={inp} /></div>
                            )}
                            <div style={{ gridColumn:"span 2" }}>
                              <label style={{ display:"flex", alignItems:"center", gap:8, cursor:"pointer", userSelect:"none" }}>
                                <input type="checkbox" checked={!!orderForm.notArrived} onChange={e => setOrderForm(p => ({ ...p, notArrived: e.target.checked }))} />
                                <span style={{ fontSize:15, color:T.ink }}>Not arrived yet: track as in transit (stock updates when it arrives)</span>
                              </label>
                            </div>
                            <div style={{ gridColumn:"span 2" }}><label style={lbl}>Notes (optional)</label><input type="text" value={orderForm.notes || ""} onChange={e => setOrderForm(p => ({ ...p, notes: e.target.value }))} style={inp} /></div>
                          </div>
                          {orderForm.skuId && !orderForm.notArrived && (parseInt(orderForm.qty) || 0) > 0 && (
                            <p style={{ ...mono, fontSize:14, color:T.green, marginTop:10 }}>
                              ✓ Adds {(parseInt(orderForm.qty) || 0).toLocaleString()} unit{(parseInt(orderForm.qty) || 0) !== 1 ? "s" : ""} to {skuList.find(s => s.id === orderForm.skuId)?.name || orderForm.skuId} stock.
                            </p>
                          )}
                          <div style={{ display:"flex", gap:8, marginTop:12 }}>
                            <button onClick={() => addOrder(sup.id)} style={{ ...btnSolid, flex:1, textAlign:"center" }}>Save</button>
                            <button onClick={() => { setAddingOrder(null); setOrderForm({}); }} style={{ ...btnGhost, flex:1, textAlign:"center" }}>Cancel</button>
                          </div>
                        </div>
                      )}

                      {completed.length === 0 && !addingOrder && inTransit.length === 0 && (
                        <p style={{ ...sub, textAlign:"center", padding:"10px 0" }}>No deliveries logged yet.</p>
                      )}

                      {showHistory[sup.id] && completed.length > 0 && histMatches.length === 0 && (
                        <p style={{ ...sub, textAlign:"center", padding:"10px 0" }}>
                          No delivery matches “{histQuery[sup.id]}”.
                        </p>
                      )}

                      {showHistory[sup.id] && histMatches.length > 0 && (
                        <div>
                          {hq && <div style={{ ...sub, marginBottom:6 }}>{histMatches.length} of {completed.length} deliveries</div>}
                          {histMatches.slice().reverse().map(ord => deliveryRow(sup, ord))}
                        </div>
                      )}

                      {/* The one action on this card, at the end of it. */}
                      {addingOrder !== sup.id && (
                        <button onClick={() => { setAddingOrder(sup.id); setOrderForm({ skuId: sup.skuIds?.[0] || "", freightMode: Object.keys(FREIGHT_MODES)[0] }); }}
                          style={{ ...btnGhost, width:"100%", marginTop:12, textAlign:"center" }}>
                          + Log / stock an order
                        </button>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
        </>
      )}
    </div>
  );
}

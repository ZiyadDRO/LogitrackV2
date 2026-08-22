import React, { useState } from 'react';
import { isoToDisplay, todayMs, todayStr, FREIGHT_MODES, planningLeadTime, leadTimeBasis,
         supplierOf, namedSuppliers } from '../lib/helpers';
import { GROQ_API_KEY, GROQ_URL, GROQ_MODEL } from '../lib/ai';
import { PriceTierTable } from './common';

// ─── ORDER IN TRANSIT MODAL ───────────────────
export function OrderInTransitModal({ skuId, existing, onSave, onClear, onClose, lm, suppliers, defaultMode,
                                     params = null }) {
  /* Expected arrival from what this product's lane ACTUALLY takes — its measured P80 if
     it has one, else the baseline for that lane (its own, or the supplier's). A flat
     today+14 ignored everything the tool knows: a sea order would land three weeks
     early on paper and the PO would look overdue long before it really was. */
  const leadFor = (mode) => {
    if (!params) return 14;
    // Deliberately NOT ignoreOneOff. That flag stops a temporary blip re-running the
    // backtest or distorting protection levels — but THIS is the order the blip is about,
    // so excluding it would date the PO by a normal wait and then show it overdue for the
    // exact reason you already flagged.
    const p = planningLeadTime(skuId, params, suppliers, { mode: mode || undefined });
    return Number.isFinite(p?.days) && p.days > 0 ? p.days : 14;
  };
  // Where the date came from, in words — shared with every other screen so the same
  // number is never described two different ways.
  const basisFor = (mode) => (params ? leadTimeBasis(skuId, params, suppliers, { mode: mode || undefined }) : null);
  const dateIn = (days) => {
    const d = new Date(todayMs()); d.setUTCDate(d.getUTCDate() + Math.round(days));
    return d.toISOString().split("T")[0];
  };
  const defaultDelivery = dateIn(leadFor(defaultMode));
  const [qty,        setQty]        = useState(existing?.qty        ?? "");
  const [ordered,    setOrdered]    = useState(existing?.ordered    ?? todayStr());
  const [delivery,   setDelivery]   = useState(existing?.delivery   ?? defaultDelivery);
  /* Default to the supplier this product is already assigned to. It used to open on
     "No supplier" regardless, so an order for an assigned product filed itself against
     nobody unless you re-picked the supplier by hand every time — and the field called
     itself "optional" while doing it, when the answer was already known. */
  const [supplierId, setSupplierId] = useState(
    existing?.supplierId ?? supplierOf(skuId, suppliers)?.id ?? "");
  // Tagging here is what makes the per-lane P80 possible later — an untagged delivery
  // can't be told apart from any other once it lands.
  // A method is always chosen: an untagged delivery can't feed a lane's P80, which is
  // the entire point of tagging it.
  const [freightMode, setFreightMode] = useState(
    existing?.freightMode || defaultMode || Object.keys(FREIGHT_MODES)[0]);
  // Switching lane re-dates the arrival — air and sea are not the same wait, and leaving
  // a sea date on an air order would defeat the point of tagging it. Only while the user
  // hasn't overridden the date themselves.
  const [dateTouched, setDateTouched] = useState(false);
  const basis = basisFor(freightMode);
  const leadUsed = leadFor(freightMode);
  const [showNewSup, setShowNewSup] = useState(false);
  const [newSupName, setNewSupName] = useState("");

  // namedSuppliers, not every record: the holding ledger for un-assigned orders is
  // storage, not somebody you buy from, and must never appear in a picker.
  const supList = namedSuppliers(suppliers);

  const onModeChange = (k) => {
    const next = k;                       // no toggling off — one method is always set
    setFreightMode(next);
    if (!dateTouched && !existing) setDelivery(dateIn(leadFor(next)));
  };

  const handleSupChange = (val) => {
    if (val === "_new") { setShowNewSup(true); setSupplierId(""); }
    else { setSupplierId(val); setShowNewSup(false); setNewSupName(""); }
  };

  const overlay = "fixed inset-0 bg-black/60 flex items-center justify-center z-50";
  const panel   = lm ? "bg-white border border-slate-300 rounded-2xl p-6 w-[400px] space-y-5 shadow-2xl"
                     : "bg-[#161619] border border-slate-700 rounded-2xl p-6 w-[400px] space-y-5 shadow-2xl";
  const title   = lm ? "text-sm font-bold text-slate-900" : "text-sm font-bold text-white";
  const desc    = lm ? "text-xs text-slate-500 mt-1" : "text-[11px] text-slate-500 mt-1";
  const lbl     = lm ? "block text-xs text-slate-600 font-semibold uppercase tracking-widest mb-1.5"
                     : "block text-[11px] text-slate-400 font-semibold uppercase tracking-widest mb-1.5";
  const inp     = lm ? "w-full bg-slate-50 border border-slate-300 rounded-lg px-3 py-2 text-sm text-slate-900 focus:outline-none focus:border-violet-500"
                     : "w-full bg-slate-900 border border-slate-700/60 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-violet-600/60";

  const handleSave = () => {
    const q = parseInt(qty);
    if (!isNaN(q) && q > 0) {
      const po = { qty: q, ordered, delivery, freightMode: freightMode || null };
      if (showNewSup && newSupName.trim()) {
        po.newSupplierName = newSupName.trim();
      } else if (supplierId && suppliers?.[supplierId]) {
        po.supplierId = supplierId;
        po.supplier   = suppliers[supplierId].name;
      }
      onSave(po);
    }
  };

  return (
    <div className={overlay} onClick={onClose}>
      <div className={panel} onClick={e => e.stopPropagation()}>
        <div>
          <h3 className={title}>Mark Order In Transit</h3>
          <p className={desc}>Record an open PO so the system knows stock is incoming and suppresses the reorder alert.</p>
        </div>
        <div className="space-y-3">
          <div>
            <label className={lbl}>Units Ordered</label>
            <input type="number" value={qty} onChange={e => setQty(e.target.value)} className={inp} placeholder="e.g. 500" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={lbl}>Order Date</label>
              <input type="date" value={ordered} onChange={e => setOrdered(e.target.value)} className={inp} />
            </div>
            <div>
              <label className={lbl}>Expected Delivery</label>
              <input type="date" value={delivery}
                onChange={e => { setDelivery(e.target.value); setDateTouched(true); }} className={inp} />
              <div className={desc}>
                {dateTouched
                  ? "Your date — nothing will override it."
                  : basis
                    ? <><span className="font-semibold">{basis.label}.</span> {basis.detail}</>
                    : <>Change it if you know better.</>}
              </div>
            </div>
          </div>
          <div>
            <label className={lbl}>Shipping Method</label>
            <div className="grid grid-cols-3 gap-1.5">
              {Object.entries(FREIGHT_MODES).map(([k, v]) => (
                <button key={k} type="button" onClick={() => onModeChange(k)}
                  className={`text-xs py-1.5 rounded-lg border font-medium transition-all ${
                    freightMode === k
                      ? (lm ? "bg-violet-100 border-violet-400 text-violet-800" : "bg-violet-950/40 border-violet-600 text-violet-200")
                      : (lm ? "bg-white border-slate-300 text-slate-500 hover:border-slate-400" : "bg-slate-900 border-slate-700/60 text-slate-500 hover:border-slate-600")}`}>
                  {v}
                </button>
              ))}
            </div>
            <div className={desc}>
              Counts toward your {FREIGHT_MODES[freightMode]} lead time.
            </div>
          </div>
          <div>
            <label className={lbl}>Supplier</label>
            {showNewSup ? (
              <div className="flex gap-2">
                <input type="text" value={newSupName} onChange={e => setNewSupName(e.target.value)}
                  placeholder="New supplier name" className={`${inp} flex-1`} autoFocus />
                <button onClick={() => { setShowNewSup(false); setNewSupName(""); }}
                  className={`text-xs px-3 rounded-lg shrink-0 ${lm ? "bg-slate-100 text-slate-600 hover:bg-slate-200" : "bg-slate-800 text-slate-400 hover:bg-slate-700"}`}>
                  Cancel
                </button>
              </div>
            ) : (
              <select value={supplierId} onChange={e => handleSupChange(e.target.value)} className={inp}>
                {/* Says what leaving it blank actually does, so the fallback needs no
                    paragraph underneath explaining itself. */}
                <option value="">No supplier — order still tracked</option>
                {supList.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
                <option value="_new">+ Add new supplier…</option>
              </select>
            )}
            {supplierId && suppliers?.[supplierId] && (
              <p className={`text-[11px] mt-1 ${lm ? "text-violet-600" : "text-violet-500"}`}>
                This order will appear in {suppliers[supplierId].name}'s delivery history.
              </p>
            )}
            {showNewSup && newSupName.trim() && (
              <p className={`text-[11px] mt-1 ${lm ? "text-violet-600" : "text-violet-500"}`}>
                A new supplier "{newSupName.trim()}" will be created.
              </p>
            )}
          </div>
        </div>
        <div className="flex gap-2 pt-1">
          {existing && (
            <button onClick={onClear}
              className="flex-1 py-2 rounded-xl text-sm font-semibold border bg-rose-50 border-rose-300 text-rose-700 hover:bg-rose-100 transition-all">
              Mark Received
            </button>
          )}
          <button onClick={handleSave}
            className="flex-1 py-2 rounded-xl text-sm font-semibold bg-violet-600 hover:bg-violet-500 text-white transition-all">
            Save PO
          </button>
          <button onClick={onClose}
            className={`flex-1 py-2 rounded-xl text-sm font-semibold border transition-all ${lm ? "border-slate-300 text-slate-600 hover:bg-slate-100" : "border-slate-700 text-slate-400 hover:bg-slate-800"}`}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── EVENTS MODAL ─────────────────────────────

export function EventsModal({ skuId, events, hasPrice, hasPromotion, priceVaried, priceWellSampled, lastPrice,
  priceTiers, priceTrainedMin, priceTrainedMax, priceSafeMin, priceSafeMax, priceModeled = true, onSave, onClose, lm }) {
  const [localEvents, setLocalEvents] = useState(events || []);
  const [adding, setAdding]           = useState(null);
  const [form, setForm]               = useState({});
  // Natural language event entry
  const [nlMode,    setNlMode]    = useState(false);
  const [nlText,    setNlText]    = useState("");
  const [nlLoading, setNlLoading] = useState(false);
  const [nlParsed,  setNlParsed]  = useState(null);
  const [nlError,   setNlError]   = useState(null);

  const parseNlEvent = async () => {
    if (!nlText.trim()) return;
    setNlLoading(true); setNlParsed(null); setNlError(null);
    const prompt = `You are a structured data extractor for an inventory system. The user describes a pricing or promotional event in plain English. Extract the event and return ONLY a JSON object — no other text.

Event types:
- "price_change_permanent" — a lasting price change
- "price_change_temporary" — a price change with start and end date
- "promotion" — a promotional discount window

Return JSON with these fields (omit fields that are absent or unclear):
{
  "type": "price_change_permanent"|"price_change_temporary"|"promotion",
  "date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "new_price": <number>,
  "discount_pct": <number>,
  "label": "<brief label>"
}

Today is ${todayStr()}. Convert any relative dates (e.g. "next week", "July 1") to YYYY-MM-DD.
Return ONLY the JSON object.

User input: "${nlText.trim()}"`;
    try {
      const res = await fetch(GROQ_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${GROQ_API_KEY}` },
        body: JSON.stringify({ model: GROQ_MODEL, messages: [{ role: "user", content: prompt }], max_tokens: 200, temperature: 0.1 }),
      });
      const data = await res.json();
      const raw = data?.choices?.[0]?.message?.content?.trim() || "";
      const match = raw.match(/\{[\s\S]*\}/);
      if (!match) throw new Error("Could not parse response");
      const parsed = JSON.parse(match[0]);
      if (!parsed.type || !parsed.date) throw new Error("Missing required fields");
      setNlParsed(parsed);
    } catch (e) {
      setNlError("Couldn't parse that. Try being more specific, e.g. 'Price drops to $12.99 on Jan 15'.");
    } finally {
      setNlLoading(false);
    }
  };

  const confirmNlEvent = () => {
    if (!nlParsed) return;
    const ev = { type: nlParsed.type, date: nlParsed.date, label: nlParsed.label || "" };
    if (nlParsed.new_price)     ev.new_price    = parseFloat(nlParsed.new_price);
    if (nlParsed.end_date)      ev.end_date     = nlParsed.end_date;
    if (nlParsed.discount_pct)  ev.discount_pct = parseFloat(nlParsed.discount_pct);
    setLocalEvents(prev => [...prev, ev].sort((a, b) => a.date.localeCompare(b.date)));
    setNlMode(false); setNlText(""); setNlParsed(null); setNlError(null);
  };

  const addEvent = () => {
    if (!form.date) return;
    const ev = { type: adding, date: form.date, label: form.label || "" };
    if (adding === "price_change_permanent" || adding === "price_change_temporary") {
      if (!form.new_price) return;
      ev.new_price = parseFloat(form.new_price);
      if (adding === "price_change_temporary") ev.end_date = form.end_date || form.date;
    }
    if (adding === "promotion") {
      ev.end_date = form.end_date || form.date;
      if (form.discount_pct) ev.discount_pct = parseFloat(form.discount_pct);
    }
    setLocalEvents(prev => [...prev, ev].sort((a, b) => a.date.localeCompare(b.date)));
    setAdding(null);
    setForm({});
  };

  const removeEvent = (i) => setLocalEvents(prev => prev.filter((_, idx) => idx !== i));
  const typeLabel = {
    price_change_permanent: { label: "Permanent Price Change", color: lm ? "text-orange-700" : "text-orange-300", bg: lm ? "bg-orange-50 border-orange-300" : "bg-orange-950/30 border-orange-900/40" },
    price_change_temporary: { label: "Temporary Price Change", color: lm ? "text-amber-700" : "text-amber-300", bg: lm ? "bg-amber-50 border-amber-300" : "bg-amber-950/30 border-amber-900/40" },
    promotion:              { label: "Promotion Window",       color: lm ? "text-violet-700" : "text-violet-300", bg: lm ? "bg-violet-50 border-violet-300" : "bg-violet-950/30 border-violet-900/40" },
  };

  const panel = lm ? "bg-white border border-slate-300 text-slate-900" : "bg-[#161619] border border-slate-700 text-white";
  const muted = lm ? "text-slate-500" : "text-slate-500";
  const input = lm ? "w-full bg-white border border-slate-300 rounded-lg px-2 py-1.5 text-sm text-slate-900 focus:outline-none focus:border-violet-500" : "w-full bg-slate-900 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-violet-600/60";
  const addPanel = lm ? "border border-slate-300 rounded-xl p-4 space-y-3 bg-slate-50" : "border border-slate-700 rounded-xl p-4 space-y-3 bg-slate-900/40";
  const labelClass = lm ? "block text-[11px] text-slate-500 uppercase tracking-widest mb-1" : "block text-[10px] text-slate-500 uppercase tracking-widest mb-1";

  const PriceContextBanner = () => {
    if (hasPrice && !priceModeled) return (
      <div className={`${lm ? "bg-amber-50 border-amber-300" : "bg-amber-950/20 border-amber-900/40"} border rounded-xl p-3 flex gap-2.5 items-start`}>
        <span className="text-sm shrink-0">⚠️</span>
        <div>
          <div className={`text-[11px] font-bold mb-0.5 ${lm ? "text-amber-700" : "text-amber-300"}`}>Price changes won't affect this forecast</div>
          <p className={`text-[11px] leading-relaxed ${lm ? "text-amber-700" : "text-amber-300/70"}`}>This product is forecast by a non-price model (new/sparse — its price response can't be estimated reliably). You can still log a price change for your records and the scorecard margin, but it will not move the forecast.</p>
        </div>
      </div>
    );
    if (!hasPrice) return (
      <div className={`${lm ? "bg-slate-50 border-slate-300" : "bg-slate-900/60 border-slate-700/50"} border rounded-xl p-3 flex gap-2.5 items-start`}>
        <span className="text-sm shrink-0">!</span>
        <div>
          <div className={`text-[11px] font-bold mb-0.5 ${lm ? "text-slate-700" : "text-slate-300"}`}>No price data in upload</div>
          <p className={`text-[11px] ${muted} leading-relaxed`}>Your Excel sheet has no <span className="font-mono">Price</span> column. Promotions can still be logged, but price changes will not affect the forecast until price history is included.</p>
        </div>
      </div>
    );
    if (!priceVaried) return (
      <div className={`${lm ? "bg-amber-50 border-amber-300" : "bg-amber-950/20 border-amber-900/30"} border rounded-xl p-3 flex gap-2.5 items-start`}>
        <span className="text-sm shrink-0">$</span>
        <div>
          <div className={`text-[11px] font-bold mb-0.5 ${lm ? "text-amber-700" : "text-amber-300"}`}>Single price point in history</div>
          <p className={`text-[11px] leading-relaxed ${lm ? "text-amber-700" : "text-amber-300/70"}`}>This SKU has only been sold at <span className="font-mono">${lastPrice?.toFixed(2)}</span>. The model has no price-response data to learn from yet.</p>
        </div>
      </div>
    );
    if (!priceWellSampled) {
      const hasInsufficient = priceTiers?.some(t => t.quality === "insufficient");
      return (
        <div className={`${lm ? "bg-amber-50 border-amber-300" : "bg-amber-950/15 border-amber-900/25"} border rounded-xl p-3 flex gap-2.5 items-start`}>
          <span className="text-sm shrink-0">$</span>
          <div className="flex-1 min-w-0">
            <div className={`text-[11px] font-bold mb-0.5 ${lm ? "text-amber-700" : "text-amber-300"}`}>
              {hasInsufficient ? "Insufficient data on some price tiers" : "Limited price variation data"}
            </div>
            <p className={`text-[11px] leading-relaxed ${lm ? "text-amber-700" : "text-amber-300/70"}`}>
              {hasInsufficient
                ? "Some price points have fewer than 30 days of data — the price coefficient for those tiers is unreliable."
                : "Price variation detected, but some tiers have fewer than 90 days of data."}
            </p>
            {priceTiers?.length > 0 && <PriceTierTable tiers={priceTiers} lm={lm} />}
          </div>
        </div>
      );
    }
    return (
      <div className={`${lm ? "bg-emerald-50 border-emerald-300" : "bg-emerald-950/20 border-emerald-900/30"} border rounded-xl p-3 flex gap-2.5 items-start`}>
        <span className="text-sm shrink-0">✓</span>
        <div>
          <div className={`text-[11px] font-bold mb-0.5 ${lm ? "text-emerald-700" : "text-emerald-300"}`}>Price sensitivity active</div>
          <p className={`text-[11px] leading-relaxed ${lm ? "text-emerald-700" : "text-emerald-300/70"}`}>The model has sufficient price history to estimate demand response.</p>
        </div>
      </div>
    );
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
      <div className={`${panel} rounded-2xl p-6 w-[480px] max-h-[85vh] flex flex-col shadow-2xl`} onClick={e => e.stopPropagation()}>
        <div className="mb-4">
          <h3 className="text-sm font-bold">Pricing & Promotion Events</h3>
          <p className={`text-[11px] ${muted} mt-1`}>Declare planned price changes and promotions. The model will apply these when forecasting future demand.</p>
        </div>

        <PriceContextBanner />

        <div className="flex-1 overflow-y-auto space-y-2 my-4 pr-1">
          {localEvents.length === 0 && <div className={`text-xs text-center py-6 ${muted}`}>No events declared yet.</div>}
          {localEvents.map((ev, i) => {
            const cfg = typeLabel[ev.type] || { label: ev.type, color: lm ? "text-slate-700" : "text-slate-400", bg: lm ? "bg-slate-50 border-slate-200" : "bg-slate-900 border-white/10" };
            return (
              <div key={i} className={`border rounded-xl p-3 flex items-start justify-between gap-3 ${cfg.bg}`}>
                <div className="min-w-0">
                  <div className={`text-xs font-bold ${cfg.color}`}>{cfg.label}</div>
                  <div className={`text-xs mt-0.5 flex gap-3 flex-wrap ${lm ? "text-slate-600" : "text-slate-400"}`}>
                    <span>{isoToDisplay(ev.date)}{ev.end_date && ev.end_date !== ev.date ? ` -> ${isoToDisplay(ev.end_date)}` : ""}</span>
                    {ev.new_price && <span>{`-> $${ev.new_price.toFixed(2)}`}</span>}
                    {ev.discount_pct && <span className={lm ? "text-violet-700" : "text-violet-400"}>{ev.discount_pct}% off</span>}
                    {ev.label && <span className={`${muted} italic`}>{ev.label}</span>}
                  </div>
                </div>
                <button onClick={() => removeEvent(i)} className={`h-5 w-5 rounded flex items-center justify-center shrink-0 transition-colors ${lm ? "bg-white hover:bg-rose-50 text-slate-500 hover:text-rose-600" : "bg-slate-800 hover:bg-rose-900 text-slate-400"}`}>
                  <svg className="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" /></svg>
                </button>
              </div>
            );
          })}
        </div>

        {adding ? (
          <div className={addPanel}>
            <div className={`text-xs font-bold ${lm ? "text-slate-700" : "text-slate-300"}`}>{typeLabel[adding]?.label}</div>
            <div className="grid grid-cols-2 gap-3">
              <div><label className={labelClass}>{adding === "promotion" ? "Promo Start" : "Effective Date"}</label><input type="date" value={form.date || ""} onChange={e => setForm(p => ({...p, date: e.target.value}))} className={input} /></div>
              {(adding === "price_change_temporary" || adding === "promotion") && <div><label className={labelClass}>End Date</label><input type="date" value={form.end_date || ""} onChange={e => setForm(p => ({...p, end_date: e.target.value}))} className={input} /></div>}
              {(adding === "price_change_permanent" || adding === "price_change_temporary") && <div><label className={labelClass}>New Price ($)</label><input type="number" step="0.01" value={form.new_price || ""} onChange={e => setForm(p => ({...p, new_price: e.target.value}))} className={input} placeholder="e.g. 19.99" /></div>}
              {adding === "promotion" && <div><label className={labelClass}>Discount % (optional)</label><input type="number" step="1" min="1" max="99" value={form.discount_pct || ""} onChange={e => setForm(p => ({...p, discount_pct: e.target.value}))} className={input} placeholder="e.g. 20" /></div>}
            </div>
            {/* Out-of-range warning for price change events */}
            {(adding === "price_change_permanent" || adding === "price_change_temporary") && (() => {
              const newPriceVal = parseFloat(form.new_price);
              if (isNaN(newPriceVal) || !hasPrice || !priceModeled || priceTrainedMin == null || priceTrainedMax == null) return null;
              const outsideTrained = newPriceVal < priceTrainedMin || newPriceVal > priceTrainedMax;
              const outsideSafe    = priceSafeMin != null && priceSafeMax != null &&
                                     (newPriceVal < priceSafeMin || newPriceVal > priceSafeMax);
              if (!outsideTrained && !outsideSafe) return null;
              const isCritical = outsideSafe;
              return (
                <div className={`flex items-start gap-2 rounded-lg p-2.5 ${isCritical
                  ? (lm ? "bg-rose-50 border border-rose-300" : "bg-rose-950/20 border border-rose-900/30")
                  : (lm ? "bg-orange-50 border border-orange-300" : "bg-orange-950/20 border border-orange-900/30")}`}>
                  <span className="text-xs shrink-0">⚠</span>
                  <div>
                    <p className={`text-[11px] leading-relaxed font-semibold mb-0.5 ${isCritical ? (lm ? "text-rose-700" : "text-rose-300") : (lm ? "text-orange-700" : "text-orange-300")}`}>
                      {isCritical ? "Outside safe extrapolation range" : "Outside trained price range"}
                    </p>
                    <p className={`text-[11px] leading-relaxed ${isCritical ? (lm ? "text-rose-600" : "text-rose-300/80") : (lm ? "text-orange-600" : "text-orange-300/80")}`}>
                      ${newPriceVal.toFixed(2)} is {outsideSafe
                        ? `beyond the safe extrapolation zone ($${priceSafeMin?.toFixed(2)}–$${priceSafeMax?.toFixed(2)})`
                        : `outside the trained range ($${priceTrainedMin.toFixed(2)}–$${priceTrainedMax.toFixed(2)})`
                      }. The model is extrapolating linearly — actual demand response at this price is unknown.
                    </p>
                  </div>
                </div>
              );
            })()}
            <div><label className={labelClass}>Label (optional)</label><input type="text" value={form.label || ""} onChange={e => setForm(p => ({...p, label: e.target.value}))} className={input} placeholder={adding === "promotion" ? "e.g. Black Friday, Summer Sale" : "e.g. Competitor match, Annual review"} /></div>
            <div className="flex gap-2 pt-1">
              <button onClick={addEvent} className="flex-1 bg-violet-700 hover:bg-violet-600 text-white text-xs font-bold py-2 rounded-lg transition-colors">Add Event</button>
              <button onClick={() => { setAdding(null); setForm({}); }} className={`flex-1 text-xs py-2 rounded-lg transition-colors ${lm ? "bg-white border border-slate-300 hover:bg-slate-100 text-slate-700" : "bg-slate-800 hover:bg-slate-700 text-slate-300"}`}>Cancel</button>
            </div>
          </div>
        ) : nlMode ? (
          <div className={addPanel}>
            <div className={`text-xs font-bold ${lm ? "text-slate-700" : "text-slate-300"}`}>Describe the event in plain English</div>
            <textarea
              value={nlText}
              onChange={e => setNlText(e.target.value)}
              placeholder='e.g. "Price drops to $12.99 starting next Monday" or "20% off promo from July 1 to July 7"'
              rows={3}
              className={`${input} resize-none`}
            />
            {nlError && <p className={`text-[11px] ${lm ? "text-rose-600" : "text-rose-400"}`}>{nlError}</p>}
            {nlParsed && (
              <div className={`border rounded-xl p-3 space-y-1 ${typeLabel[nlParsed.type]?.bg || (lm ? "bg-slate-50 border-slate-200" : "bg-slate-900 border-white/10")}`}>
                <div className={`text-[11px] font-bold ${typeLabel[nlParsed.type]?.color || (lm ? "text-slate-700" : "text-slate-300")}`}>Parsed: {typeLabel[nlParsed.type]?.label || nlParsed.type}</div>
                <div className={`text-xs flex flex-wrap gap-3 ${lm ? "text-slate-600" : "text-slate-400"}`}>
                  <span>{isoToDisplay(nlParsed.date)}{nlParsed.end_date ? ` → ${isoToDisplay(nlParsed.end_date)}` : ""}</span>
                  {nlParsed.new_price    && <span>→ ${parseFloat(nlParsed.new_price).toFixed(2)}</span>}
                  {nlParsed.discount_pct && <span>{nlParsed.discount_pct}% off</span>}
                  {nlParsed.label        && <span className={`italic ${lm ? "text-slate-500" : "text-slate-500"}`}>{nlParsed.label}</span>}
                </div>
                <div className="flex gap-2 pt-1">
                  <button onClick={confirmNlEvent} className="flex-1 bg-violet-700 hover:bg-violet-600 text-white text-[11px] font-bold py-1.5 rounded-lg transition-colors">Confirm & Add</button>
                  <button onClick={() => { setNlParsed(null); setNlError(null); }} className={`flex-1 text-[11px] py-1.5 rounded-lg transition-colors ${lm ? "bg-white border border-slate-300 hover:bg-slate-100 text-slate-700" : "bg-slate-800 hover:bg-slate-700 text-slate-300"}`}>Try again</button>
                </div>
              </div>
            )}
            {!nlParsed && (
              <div className="flex gap-2 pt-1">
                <button onClick={parseNlEvent} disabled={nlLoading || !nlText.trim()}
                  className="flex-1 bg-violet-700 hover:bg-violet-600 disabled:opacity-50 text-white text-xs font-bold py-2 rounded-lg transition-colors flex items-center justify-center gap-1.5">
                  {nlLoading ? <><div className="h-2.5 w-2.5 border-2 border-white border-t-transparent rounded-full animate-spin" />Parsing…</> : "Parse"}
                </button>
                <button onClick={() => { setNlMode(false); setNlText(""); setNlError(null); setNlParsed(null); }}
                  className={`flex-1 text-xs py-2 rounded-lg transition-colors ${lm ? "bg-white border border-slate-300 hover:bg-slate-100 text-slate-700" : "bg-slate-800 hover:bg-slate-700 text-slate-300"}`}>Cancel</button>
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-2">
            <div className={`text-[11px] uppercase tracking-widest font-bold ${muted}`}>Add Event</div>
            <div className="grid grid-cols-2 gap-2">
              <button onClick={() => setAdding("price_change_permanent")} className={`${typeLabel.price_change_permanent.bg} ${typeLabel.price_change_permanent.color} border hover:brightness-105 text-[11px] font-bold py-2.5 px-2 rounded-lg transition-colors text-center leading-tight`}>Permanent<br/>Price Change</button>
              <button onClick={() => setAdding("price_change_temporary")} className={`${typeLabel.price_change_temporary.bg} ${typeLabel.price_change_temporary.color} border hover:brightness-105 text-[11px] font-bold py-2.5 px-2 rounded-lg transition-colors text-center leading-tight`}>Temporary<br/>Price Change</button>
              <button onClick={() => setAdding("promotion")} className={`${typeLabel.promotion.bg} ${typeLabel.promotion.color} border hover:brightness-105 text-[11px] font-bold py-2.5 px-2 rounded-lg transition-colors text-center leading-tight`}>Promotion<br/>Window</button>
              <button onClick={() => setNlMode(true)}
                className={`border hover:brightness-105 text-[11px] font-bold py-2.5 px-2 rounded-lg transition-colors text-center leading-tight ${lm ? "bg-violet-50 border-violet-300 text-violet-700" : "bg-violet-950/20 border-violet-800/40 text-violet-300"}`}>
                ✨ Describe it<br/>(AI parse)
              </button>
            </div>
          </div>
        )}

        <div className={`flex gap-2 pt-4 border-t mt-4 ${lm ? "border-slate-200" : "border-white/10"}`}>
          <button onClick={() => onSave(localEvents)} className="flex-1 bg-violet-600 hover:bg-violet-500 text-white text-xs font-bold py-2.5 rounded-xl transition-colors">Save & Update Forecast</button>
          <button onClick={onClose} className={`px-4 text-xs font-bold py-2.5 rounded-xl transition-colors ${lm ? "bg-white border border-slate-300 hover:bg-slate-100 text-slate-700" : "bg-slate-800 hover:bg-slate-700 text-slate-300"}`}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

// ─── ASSIGN FOLDER MODAL ──────────────────────
export function AssignFolderModal({ skuId, skuName, folders, onAssign, onClose, lm }) {
  const panel = lm ? "bg-white border border-slate-300 rounded-2xl p-5 w-[300px] space-y-3 shadow-2xl"
                   : "bg-[#161619] border border-slate-700 rounded-2xl p-5 w-[300px] space-y-3 shadow-2xl";
  const title = lm ? "text-xs font-bold text-slate-900" : "text-xs font-bold text-white";
  const sub   = lm ? "text-xs text-slate-500 mt-0.5 font-mono truncate" : "text-[11px] text-slate-500 mt-0.5 font-mono truncate";
  const btn   = lm
    ? "w-full flex items-center gap-2.5 px-3 py-2 rounded-lg bg-slate-50 hover:bg-violet-50 border border-slate-200 hover:border-violet-300 transition-all text-left"
    : "w-full flex items-center gap-2.5 px-3 py-2 rounded-lg bg-slate-800/60 hover:bg-slate-700/60 border border-slate-700/40 hover:border-slate-600 transition-all text-left";
  const cancel = lm
    ? "w-full text-xs text-slate-500 hover:text-slate-700 border border-slate-200 rounded-lg py-1.5 transition-colors"
    : "w-full text-[11px] text-slate-500 hover:text-slate-300 border border-white/10 rounded-lg py-1.5 transition-colors";
  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
      <div className={panel} onClick={e => e.stopPropagation()}>
        <div>
          <h3 className={title}>Move to folder</h3>
          <p className={sub}>{skuName}</p>
        </div>
        <div className="space-y-1.5">
          {Object.entries(folders)
            .sort(([, a], [, b]) => {
              const pa = a.parentId ? (folders[a.parentId]?.name || "") + "/" + a.name : a.name;
              const pb = b.parentId ? (folders[b.parentId]?.name || "") + "/" + b.name : b.name;
              return pa.localeCompare(pb);
            })
            .map(([id, f]) => (
            <button key={id} onClick={() => onAssign(id)} className={`${btn} ${f.parentId ? "pl-6" : ""}`}>
              <svg className={`h-4 w-4 shrink-0 ${lm ? "text-slate-400" : "text-slate-500"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
              </svg>
              <span className={`text-xs truncate ${lm ? "text-slate-800" : "text-slate-200"}`}>
                {f.parentId && <span className={lm ? "text-slate-400" : "text-slate-500"}>{folders[f.parentId]?.name || "?"} / </span>}
                {f.name}
              </span>
              <span className={`ml-auto text-[11px] font-mono ${lm ? "text-slate-400" : "text-slate-500"}`}>{f.skuIds.length}</span>
            </button>
          ))}
          {Object.keys(folders).length === 0 && (
            <p className={`text-xs text-center py-3 ${lm ? "text-slate-400" : "text-slate-600"}`}>No folders yet — create one first.</p>
          )}
        </div>
        <button onClick={onClose} className={cancel}>Cancel</button>
      </div>
    </div>
  );
}
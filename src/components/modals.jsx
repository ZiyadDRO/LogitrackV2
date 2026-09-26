import React, { useState } from 'react';
import { isoToDisplay, todayMs, todayStr, FREIGHT_MODES, planningLeadTime, leadTimeBasis,
         supplierOf, namedSuppliers } from '../lib/helpers';
import { GROQ_API_KEY, GROQ_URL, GROQ_MODEL, GROQ_LOW_REASONING } from '../lib/ai';
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
  const panel   = "bg-[var(--t-panel)] border border-[var(--t-line2)] rounded-2xl p-6 w-[400px] space-y-5 shadow-2xl";
  const title   = "text-[16.5px] font-bold text-[var(--t-ink)]";
  const desc    = "text-[15px] text-[var(--t-dim)] mt-1";
  const lbl     = "block text-[15px] text-[var(--t-soft)] font-semibold uppercase tracking-widest mb-1.5";
  const inp     = "w-full bg-[var(--t-sunken)] border border-[var(--t-line2)] rounded-lg px-3 py-2 text-[16.5px] text-[var(--t-ink)] focus:outline-none focus:border-[var(--t-accent-line)]";

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
          <p className={desc}>Record an open PO so incoming stock is counted and the reorder alert is suppressed.</p>
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
                  ? "Your date. Nothing will override it."
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
                  className={`text-[15px] py-1.5 rounded-lg border font-medium transition-all ${
                    freightMode === k
                      ? ("bg-[var(--t-accent-soft)] border-[var(--t-accent-line)] text-[var(--t-accent)]")
                      : ("bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-dim)] hover:border-[var(--t-line2)]")}`}>
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
                  className={`text-[15px] px-3 rounded-lg shrink-0 ${"bg-[var(--t-sunken)] text-[var(--t-soft)] hover:bg-[var(--t-sunken)]"}`}>
                  Cancel
                </button>
              </div>
            ) : (
              <select value={supplierId} onChange={e => handleSupChange(e.target.value)} className={inp}>
                {/* Says what leaving it blank actually does, so the fallback needs no
                    paragraph underneath explaining itself. */}
                <option value="">No supplier (order still tracked)</option>
                {supList.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
                <option value="_new">+ Add new supplier…</option>
              </select>
            )}
            {supplierId && suppliers?.[supplierId] && (
              <p className={`text-[14px] mt-1 ${"text-[var(--t-accent)]"}`}>
                This order will appear in {suppliers[supplierId].name}'s delivery history.
              </p>
            )}
            {showNewSup && newSupName.trim() && (
              <p className={`text-[14px] mt-1 ${"text-[var(--t-accent)]"}`}>
                A new supplier "{newSupName.trim()}" will be created.
              </p>
            )}
          </div>
        </div>
        <div className="flex gap-2 pt-1">
          {existing && (
            <button onClick={onClear}
              className="flex-1 py-2 rounded-xl text-[16.5px] font-semibold border bg-[var(--t-bad-soft)] border-[var(--t-bad-line)] text-[var(--t-bad)] hover:bg-[var(--t-bad-soft)] transition-all">
              Mark Received
            </button>
          )}
          <button onClick={handleSave}
            className="flex-1 py-2 rounded-xl text-[16.5px] font-semibold bg-[var(--t-accent-soft)] hover:bg-[var(--t-accent-soft)] text-[var(--t-ink)] transition-all">
            Save PO
          </button>
          <button onClick={onClose}
            className={`flex-1 py-2 rounded-xl text-[16.5px] font-semibold border transition-all ${"border-[var(--t-line2)] text-[var(--t-soft)] hover:bg-[var(--t-sunken)]"}`}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── EVENTS MODAL ─────────────────────────────

/** "Buy X get Y free" as the per-unit discount it amounts to: Y free in every X+Y. */
function multibuyFields(buy, get) {
  const b = Math.max(1, Math.round(buy)), g = Math.max(1, Math.round(get));
  return { discount_pct: Math.round(g / (b + g) * 10000) / 100, multibuy: { buy: b, get: g },
           label: `Buy ${b} get ${g} free` };
}

export function EventsModal({ skuId, events, hasPrice, hasPromotion, priceVaried, priceWellSampled, lastPrice,
  priceTiers, priceTrainedMin, priceTrainedMax, priceSafeMin, priceSafeMax, priceModeled = true, onSave, onClose, lm,
  priceResponse = null, priceMixedDays = 0, lastRecordedDate = null, posEvents = [], livePrice = null,
  holidays = [] }) {
  /* Whether a price change can move THIS product's forecast, decided by the server. When
     it can't, adding one used to be a silent no-op: save, nothing changes, no reason given.
     Every refusal has a real cause — too few selling days to measure, a relationship in
     the data that points backwards, a price that never moved — and each is now said at
     the moment it matters, which is while the change is being entered. */
  const priceInert = priceResponse && priceResponse.applies === false;
  const priceLive = !!(priceResponse && priceResponse.applies);
  // `bounds` are the hard limits the model is evaluated within; `observed` the range of
  // prices with 30+ days behind them. Between the two a price is extrapolated — allowed,
  // but with a wider forecast range and more safety stock.
  const borrowed = priceLive && priceResponse.reason === "borrowed";
  const bounds = !priceLive ? null
    : (!borrowed && priceSafeMin != null && priceSafeMax != null) ? [priceSafeMin, priceSafeMax]
    : (priceResponse.bounds || null);
  const observed = !priceLive ? null
    : (!borrowed && priceTrainedMin != null && priceTrainedMax != null) ? [priceTrainedMin, priceTrainedMax]
    : (priceResponse.knownRange || null);
  const basePrice = livePrice?.listPrice ?? lastPrice;

  /* Events are FORECAST inputs. Days up to lastRecordedDate already have real sales,
     and those — not an event — say what was charged and sold. So an event is judged
     against where recorded history ends, and against the prices actually observed. */
  const overlapDays = (a, b) => a.date <= (b.end_date || b.date) && b.date <= (a.end_date || a.date);
  const eventNotes = (ev) => {
    const notes = [];
    /* Promotions don't add up: a shopper takes one deal. On days two overlap, the deeper
       discount is the one forecast (same rule as the server). */
    if (ev.type === "promotion" && ev.discount_pct && ev.date) {
      const others = [...localEvents, ...posEvents].filter(o => o !== ev && o.type === "promotion"
        && o.discount_pct && o.date && overlapDays(ev, o));
      if (others.length) {
        const deepest = Math.max(ev.discount_pct, ...others.map(o => o.discount_pct));
        const names = others.map(o => o.label || `${Math.round(o.discount_pct)}% off`).join(", ");
        notes.push({ tone: "dim", text: `Overlaps with ${names}. Promotions don't add up: on shared days the deepest discount (${Math.round(deepest)}%) is used.` });
      }
    }
    const end = ev.end_date || ev.date;
    const isPrice = ev.type === "price_change_permanent" || ev.type === "price_change_temporary";
    if (lastRecordedDate && ev.date && ev.date <= lastRecordedDate) {
      if (ev.type !== "price_change_permanent" && end <= lastRecordedDate) {
        notes.push({ tone: "dim", text: "Already happened. Recorded sales for these dates are used as is, so this event has no effect." });
      } else if (ev.type === "price_change_permanent") {
        notes.push({ tone: "dim", text: `Dated before your latest recorded day (${isoToDisplay(lastRecordedDate)}). Past days keep the prices actually charged. This only affects the forecast.` });
        if (lastPrice != null && ev.new_price != null && Math.abs(ev.new_price - lastPrice) < 0.005) {
          notes.push({ tone: "dim", text: "Your recorded sales are already at this price." });
        }
      } else {
        notes.push({ tone: "dim", text: `Partly past. Only days after ${isoToDisplay(lastRecordedDate)} are affected.` });
      }
    }
    if (ev.type === "promotion" && !ev.discount_pct) {
      notes.push({ tone: "dim", text: "No discount, so this doesn't change the forecast." });
    }
    if (bounds) {
      let p = isPrice ? ev.new_price : null;
      if (ev.type === "promotion" && ev.discount_pct && basePrice != null) p = basePrice * (1 - ev.discount_pct / 100);
      if (p != null && !isNaN(p)) {
        if (p < bounds[0] || p > bounds[1]) {
          const edge = p < bounds[0] ? bounds[0] : bounds[1];
          notes.push({ tone: "warn", text: `Forecast as if $${edge.toFixed(2)}. $${p.toFixed(2)} is past the furthest the model goes from prices you've sold at.` });
        } else if (observed && (p < observed[0] - 0.005 || p > observed[1] + 0.005)) {
          const below = p < observed[0];
          const ref = below ? observed[0] : observed[1];
          const pct = Math.round(Math.abs(p - ref) / ref * 100);
          notes.push({ tone: "warn", text: `Extrapolated: $${p.toFixed(2)} is ${pct}% ${below ? "below the lowest" : "above the highest"} price with 30+ days of history ($${ref.toFixed(2)}). The forecast range and safety stock are widened for it.` });
        }
      }
    }
    return notes;
  };
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
- "promotion" — a promotional discount window. For "buy X get Y free" deals, also return buy_qty and get_qty.

Return JSON with these fields (omit fields that are absent or unclear):
{
  "type": "price_change_permanent"|"price_change_temporary"|"promotion",
  "date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "new_price": <number>,
  "discount_pct": <number>,
  "buy_qty": <number>,
  "get_qty": <number>,
  "label": "<brief label>"
}

Today is ${todayStr()}. Convert any relative dates (e.g. "next week", "July 1") to YYYY-MM-DD.
Return ONLY the JSON object.

User input: "${nlText.trim()}"`;
    // An API failure is not a user mistake. A retired model, a missing key or no network
    // all used to surface as "couldn't parse that", which sends the user off rewording a
    // sentence that was never the problem. Errors carrying `shown` say what really broke.
    const apiFail = (m) => Object.assign(new Error(m), { shown: m });
    try {
      if (!GROQ_API_KEY) {
        throw apiFail("No API key is set, so plain-English entry is off. Use the form below.");
      }
      let res;
      try {
        res = await fetch(GROQ_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json", "Authorization": `Bearer ${GROQ_API_KEY}` },
          body: JSON.stringify({ model: GROQ_MODEL, messages: [{ role: "user", content: prompt }], max_tokens: 200, temperature: 0.1, ...GROQ_LOW_REASONING }),
        });
      } catch {
        throw apiFail("Couldn't reach the language model. Check your connection, or use the form below.");
      }
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        const why = data?.error?.message || `${res.status} ${res.statusText}`;
        throw apiFail(`The language model rejected the request: ${why}. This is not a problem with your wording. The form below still works.`);
      }
      const raw = data?.choices?.[0]?.message?.content?.trim() || "";
      const match = raw.match(/\{[\s\S]*\}/);
      if (!match) throw new Error("Could not parse response");
      const parsed = JSON.parse(match[0]);
      if (!parsed.type || !parsed.date) throw new Error("Missing required fields");
      setNlParsed(parsed);
    } catch (e) {
      setNlError(e?.shown || "Couldn't parse that. Try being more specific, e.g. 'Price drops to $12.99 on Jan 15'.");
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
    if (nlParsed.type === "promotion" && nlParsed.buy_qty > 0 && nlParsed.get_qty > 0) {
      Object.assign(ev, multibuyFields(nlParsed.buy_qty, nlParsed.get_qty));
      if (!ev.end_date) ev.end_date = ev.date;
    }
    setLocalEvents(prev => [...prev, ev].sort((a, b) => a.date.localeCompare(b.date)));
    setNlMode(false); setNlText(""); setNlParsed(null); setNlError(null);
  };

  const addEvent = () => {
    if (!form.date) return;
    if (adding === "multibuy") {
      const buy = parseInt(form.buy_qty || "1", 10), get = parseInt(form.get_qty || "1", 10);
      if (!(buy > 0) || !(get > 0)) return;
      const ev = { type: "promotion", date: form.date, end_date: form.end_date || form.date,
                   ...multibuyFields(buy, get), ...(form.label ? { label: form.label } : {}) };
      setLocalEvents(prev => [...prev, ev].sort((a, b) => a.date.localeCompare(b.date)));
      setAdding(null); setForm({});
      return;
    }
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
    price_change_permanent: { label: "Permanent Price Change", color: "text-[var(--t-warn)]", bg: "bg-[var(--t-warn-soft)] border-[var(--t-warn-line)]" },
    price_change_temporary: { label: "Temporary Price Change", color: "text-[var(--t-warn)]", bg: "bg-[var(--t-warn-soft)] border-[var(--t-warn-line)]" },
    promotion:              { label: "Promotion Window",       color: "text-[var(--t-accent)]", bg: "bg-[var(--t-accent-soft)] border-[var(--t-accent-line)]" },
    multibuy:               { label: "Multi-buy Deal",         color: "text-[var(--t-accent)]", bg: "bg-[var(--t-accent-soft)] border-[var(--t-accent-line)]" },
  };

  const panel = "bg-[var(--t-panel)] border border-[var(--t-line2)] text-[var(--t-ink)]";
  const muted = "text-[var(--t-dim)]";
  const input = "w-full bg-[var(--t-panel)] border border-[var(--t-line2)] rounded-lg px-2 py-1.5 text-[16.5px] text-[var(--t-ink)] focus:outline-none focus:border-[var(--t-accent-line)]";
  const addPanel = "border border-[var(--t-line2)] rounded-xl p-4 space-y-3 bg-[var(--t-sunken)]";
  const labelClass = "block text-[14px] text-[var(--t-dim)] uppercase tracking-widest mb-1";

  const MixedNote = () => priceMixedDays > 0 ? (
    <p className={`text-[13px] mt-1 ${muted}`}>
      {priceMixedDays} day{priceMixedDays === 1 ? "" : "s"} sold at more than one price (e.g. some units discounted) and {priceMixedDays === 1 ? "is" : "are"} not counted toward any price.
    </p>
  ) : null;

  const PriceContextBanner = () => {
    if (!hasPrice) return (
      <div className={`${"bg-[var(--t-sunken)] border-[var(--t-line2)]"} border rounded-xl p-3 flex gap-2.5 items-start`}>
        <span className="text-[16.5px] shrink-0">!</span>
        <div>
          <div className={`text-[14px] font-bold mb-0.5 ${"text-[var(--t-soft)]"}`}>No price data</div>
          <p className={`text-[14px] ${muted} leading-relaxed`}>There is no price history for this product. Promotions can still be logged, but price changes will not affect the forecast.</p>
        </div>
      </div>
    );
    /* The server's verdict decides this banner. It used to be worked out here from
       different flags, and could say "price sensitivity active" on a product where a
       price change did nothing. */
    if (priceInert) return (
      <div className={`${"bg-[var(--t-warn-soft)] border-[var(--t-warn-line)]"} border rounded-xl p-3 flex gap-2.5 items-start`}>
        <span className="text-[16.5px] shrink-0">$</span>
        <div className="flex-1 min-w-0">
          <div className={`text-[14px] font-bold mb-0.5 ${"text-[var(--t-warn)]"}`}>Price changes won't affect this forecast</div>
          <p className={`text-[14px] leading-relaxed ${"text-[var(--t-warn)]"}`}>{priceResponse.text}</p>
          {priceTiers?.length > 0 && <PriceTierTable tiers={priceTiers} lm={lm} />}
        </div>
      </div>
    );
    return (
      <div className={`${"bg-[var(--t-good-soft)] border-[var(--t-good-line)]"} border rounded-xl p-3 flex gap-2.5 items-start`}>
        <span className="text-[16.5px] shrink-0">✓</span>
        <div className="flex-1 min-w-0">
          <div className={`text-[14px] font-bold mb-0.5 ${"text-[var(--t-good)]"}`}>Price changes move this forecast</div>
          <p className={`text-[14px] leading-relaxed ${"text-[var(--t-good)]"}`}>
            {borrowed ? priceResponse.text : bounds && observed
              ? `At least two prices each have 30+ days of history ($${observed[0].toFixed(2)} to $${observed[1].toFixed(2)}). Prices outside that range are extrapolated, down to $${bounds[0].toFixed(2)} and up to $${bounds[1].toFixed(2)}, with a wider forecast range and more safety stock.`
              : "At least two prices each have 30+ days of history."}
          </p>
          {priceTiers?.length > 0 && <PriceTierTable tiers={priceTiers} lm={lm} />}
          <MixedNote />
        </div>
      </div>
    );
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
      <div className={`${panel} rounded-2xl p-6 w-[480px] max-w-[calc(100vw-32px)] max-h-[85vh] flex flex-col shadow-2xl`} onClick={e => e.stopPropagation()}>
        {/* Everything above the Save row scrolls as one: the price table alone can be taller
            than the window, and a separately scrolling list inside it got squeezed to nothing. */}
        <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain -mx-6 px-6">
        <div className="mb-4">
          <h3 className="text-[16.5px] font-bold">Pricing & Promotion Events</h3>
          <p className={`text-[14px] ${muted} mt-1`}>Declare planned price changes and promotions. These only shape the forecast{lastRecordedDate ? ` for days after ${isoToDisplay(lastRecordedDate)}` : ""}. Recorded sales and prices are never changed.</p>
          {holidays.length > 0 && (
            <p className={`text-[14px] ${muted} mt-1.5`}>
              Holidays are already counted on their own dates ({holidays.slice(0, 3).map(h => `${h.name} ${h.pct > 0 ? "+" : ""}${h.pct}%`).join(", ")}{holidays.length > 3 ? ", …" : ""}). Enter only the deal you run, not the holiday itself.
            </p>
          )}
        </div>

        <PriceContextBanner />

        <div className="space-y-2 my-4">
          {/* Read from the POS, not typed here: refreshed every hour, and it ends when
              the POS says it ends. Shown so the forecast never has an unexplained input. */}
          {posEvents.filter(ev => ev.type === "promotion").map((ev, i) => {
            const src = livePrice?.source ? livePrice.source[0].toUpperCase() + livePrice.source.slice(1) : "your POS";
            return (
              <div key={`pos${i}`} className="border rounded-xl p-3 bg-[var(--t-accent-soft)] border-[var(--t-accent-line)]">
                <div className="text-[15px] font-bold text-[var(--t-accent)]">Live discount · from {src}</div>
                <div className="text-[15px] mt-0.5 flex gap-3 flex-wrap text-[var(--t-soft)]">
                  <span>{ev.label}</span>
                  <span>{Math.round(ev.discount_pct)}% off{livePrice?.listPrice ? ` → $${(livePrice.currentPrice ?? livePrice.listPrice).toFixed(2)}` : ""}</span>
                  <span>{ev.openEnded ? "no end date set" : `until ${isoToDisplay(ev.end_date)}`}</span>
                </div>
                <div className={`text-[13px] mt-1 leading-snug ${muted}`}>
                  Checked every hour. It shapes the forecast from today and stops when {src} says the discount has ended.
                </div>
                {eventNotes({ type: "promotion", date: ev.date, end_date: ev.end_date, discount_pct: ev.discount_pct })
                  .filter(n => n.tone === "warn").map((n, j) => (
                    <div key={j} className="text-[13px] mt-1 leading-snug text-[var(--t-warn)]">{n.text}</div>
                  ))}
                {priceInert && (
                  <div className="text-[13px] mt-1 leading-snug text-[var(--t-warn)]">The lower price won't move this forecast yet (see above).</div>
                )}
              </div>
            );
          })}
          {localEvents.length === 0 && posEvents.length === 0 && <div className={`text-[15px] text-center py-6 ${muted}`}>No events declared yet.</div>}
          {localEvents.map((ev, i) => {
            const cfg = typeLabel[ev.multibuy ? "multibuy" : ev.type] || { label: ev.type, color: "text-[var(--t-soft)]", bg: "bg-[var(--t-sunken)] border-[var(--t-line)]" };
            return (
              <div key={i} className={`border rounded-xl p-3 flex items-start justify-between gap-3 ${cfg.bg}`}>
                <div className="min-w-0">
                  <div className={`text-[15px] font-bold ${cfg.color}`}>{cfg.label}</div>
                  <div className={`text-[15px] mt-0.5 flex gap-3 flex-wrap ${"text-[var(--t-soft)]"}`}>
                    <span>{isoToDisplay(ev.date)}{ev.end_date && ev.end_date !== ev.date ? ` -> ${isoToDisplay(ev.end_date)}` : ""}</span>
                    {ev.new_price && <span>{`-> $${ev.new_price.toFixed(2)}`}</span>}
                    {ev.new_price && priceInert && (
                      <span title={priceResponse.text} className={"text-[var(--t-warn)]"}>no effect on forecast</span>
                    )}
                    {ev.multibuy && <span className={"text-[var(--t-accent)]"}>Buy {ev.multibuy.buy} get {ev.multibuy.get} free</span>}
                    {ev.discount_pct && <span className={"text-[var(--t-accent)]"}>{ev.multibuy ? `(${Math.round(ev.discount_pct)}% off per unit)` : `${ev.discount_pct}% off`}</span>}
                    {ev.label && <span className={`${muted} italic`}>{ev.label}</span>}
                  </div>
                  {eventNotes(ev).map((n, j) => (
                    <div key={j} className={`text-[13px] mt-1 leading-snug ${n.tone === "warn" ? "text-[var(--t-warn)]" : muted}`}>{n.text}</div>
                  ))}
                </div>
                <button onClick={() => removeEvent(i)} className={`h-5 w-5 rounded flex items-center justify-center shrink-0 transition-colors ${"bg-[var(--t-panel)] hover:bg-[var(--t-bad-soft)] text-[var(--t-dim)] hover:text-[var(--t-bad)]"}`}>
                  <svg className="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" /></svg>
                </button>
              </div>
            );
          })}
        </div>

        {adding ? (
          <div className={addPanel}>
            <div className={`text-[15px] font-bold ${"text-[var(--t-soft)]"}`}>{typeLabel[adding]?.label}</div>
            <div className="grid grid-cols-2 gap-3">
              <div><label className={labelClass}>{adding === "promotion" || adding === "multibuy" ? "Start" : "Effective Date"}</label><input type="date" value={form.date || ""} onChange={e => setForm(p => ({...p, date: e.target.value}))} className={input} /></div>
              {(adding === "price_change_temporary" || adding === "promotion" || adding === "multibuy") && <div><label className={labelClass}>End Date</label><input type="date" value={form.end_date || ""} onChange={e => setForm(p => ({...p, end_date: e.target.value}))} className={input} /></div>}
              {(adding === "price_change_permanent" || adding === "price_change_temporary") && <div><label className={labelClass}>New Price ($)</label><input type="number" step="0.01" value={form.new_price || ""} onChange={e => setForm(p => ({...p, new_price: e.target.value}))} className={input} placeholder="e.g. 19.99" /></div>}
              {adding === "multibuy" && <div><label className={labelClass}>Buy</label><input type="number" step="1" min="1" value={form.buy_qty ?? "1"} onChange={e => setForm(p => ({...p, buy_qty: e.target.value}))} className={input} /></div>}
              {adding === "multibuy" && <div><label className={labelClass}>Get free</label><input type="number" step="1" min="1" value={form.get_qty ?? "1"} onChange={e => setForm(p => ({...p, get_qty: e.target.value}))} className={input} /></div>}
              {adding === "promotion" && <div><label className={labelClass}>Discount % (optional)</label><input type="number" step="1" min="1" max="99" value={form.discount_pct || ""} onChange={e => setForm(p => ({...p, discount_pct: e.target.value}))} className={input} placeholder="e.g. 20" /></div>}
            </div>
            {/* Said BEFORE the change is saved, not discovered afterwards. */}
            {(adding === "price_change_permanent" || adding === "price_change_temporary") && priceInert && (
              <div className={`flex items-start gap-2 rounded-lg p-2.5 ${"bg-[var(--t-warn-soft)] border border-[var(--t-warn-line)]"}`}>
                <span className="text-[15px] shrink-0">ⓘ</span>
                <p className={`text-[14px] leading-relaxed ${"text-[var(--t-warn)]"}`}>{priceResponse.text}</p>
              </div>
            )}
            {/* What the forecast will actually use, said before saving. */}
            {adding === "promotion" && (
              <p className={`text-[13px] leading-relaxed ${muted}`}>A promotion counts as a price cut of its discount. With no discount it won't change the forecast.</p>
            )}
            {adding === "multibuy" && (() => {
              const b = parseInt(form.buy_qty || "1", 10), g = parseInt(form.get_qty || "1", 10);
              if (!(b > 0) || !(g > 0)) return null;
              const pct = Math.round(g / (b + g) * 100);
              return (
                <p className={`text-[13px] leading-relaxed ${muted}`}>
                  Buy {b} get {g} free is forecast as {pct}% off per unit{basePrice != null ? ` ($${(basePrice * b / (b + g)).toFixed(2)} each)` : ""}. Shoppers who only want one still pay full price, so the real lift may be smaller.
                </p>
              );
            })()}
            {(adding === "price_change_permanent" || adding === "price_change_temporary" || adding === "promotion" || adding === "multibuy") && (() => {
              const mb = adding === "multibuy" ? multibuyFields(parseInt(form.buy_qty || "1", 10) || 1, parseInt(form.get_qty || "1", 10) || 1) : null;
              const notes = eventNotes({ type: mb ? "promotion" : adding, date: form.date, end_date: form.end_date,
                new_price: form.new_price ? parseFloat(form.new_price) : null,
                discount_pct: mb ? mb.discount_pct : (form.discount_pct ? parseFloat(form.discount_pct) : null) });
              if (!notes.length) return null;
              return (
                <div className={`rounded-lg p-2.5 space-y-1 ${"bg-[var(--t-warn-soft)] border border-[var(--t-warn-line)]"}`}>
                  {notes.map((n, j) => (
                    <p key={j} className={`text-[14px] leading-relaxed ${"text-[var(--t-warn)]"}`}>{n.text}</p>
                  ))}
                </div>
              );
            })()}
            <div><label className={labelClass}>Label (optional)</label><input type="text" value={form.label || ""} onChange={e => setForm(p => ({...p, label: e.target.value}))} className={input} placeholder={adding === "promotion" ? "e.g. Black Friday, Summer Sale" : "e.g. Competitor match, Annual review"} /></div>
            <div className="flex gap-2 pt-1">
              <button onClick={addEvent} className="flex-1 bg-[var(--t-accent-soft)] hover:bg-[var(--t-accent-soft)] text-[var(--t-ink)] text-[15px] font-bold py-2 rounded-lg transition-colors">Add Event</button>
              <button onClick={() => { setAdding(null); setForm({}); }} className={`flex-1 text-[15px] py-2 rounded-lg transition-colors ${"bg-[var(--t-panel)] border border-[var(--t-line2)] hover:bg-[var(--t-sunken)] text-[var(--t-soft)]"}`}>Cancel</button>
            </div>
          </div>
        ) : nlMode ? (
          <div className={addPanel}>
            <div className={`text-[15px] font-bold ${"text-[var(--t-soft)]"}`}>Describe the event in plain English</div>
            <textarea
              value={nlText}
              onChange={e => setNlText(e.target.value)}
              placeholder='e.g. "Price drops to $12.99 starting next Monday" or "20% off promo from July 1 to July 7"'
              rows={3}
              className={`${input} resize-none`}
            />
            {nlError && <p className={`text-[14px] ${"text-[var(--t-bad)]"}`}>{nlError}</p>}
            {nlParsed && (
              <div className={`border rounded-xl p-3 space-y-1 ${typeLabel[nlParsed.type]?.bg || ("bg-[var(--t-sunken)] border-[var(--t-line)]")}`}>
                <div className={`text-[14px] font-bold ${typeLabel[nlParsed.type]?.color || ("text-[var(--t-soft)]")}`}>Parsed: {typeLabel[nlParsed.type]?.label || nlParsed.type}</div>
                <div className={`text-[15px] flex flex-wrap gap-3 ${"text-[var(--t-soft)]"}`}>
                  <span>{isoToDisplay(nlParsed.date)}{nlParsed.end_date ? ` → ${isoToDisplay(nlParsed.end_date)}` : ""}</span>
                  {nlParsed.new_price    && <span>→ ${parseFloat(nlParsed.new_price).toFixed(2)}</span>}
                  {nlParsed.discount_pct && <span>{nlParsed.discount_pct}% off</span>}
                  {nlParsed.label        && <span className={`italic ${"text-[var(--t-dim)]"}`}>{nlParsed.label}</span>}
                </div>
                <div className="flex gap-2 pt-1">
                  <button onClick={confirmNlEvent} className="flex-1 bg-[var(--t-accent-soft)] hover:bg-[var(--t-accent-soft)] text-[var(--t-ink)] text-[14px] font-bold py-1.5 rounded-lg transition-colors">Confirm & Add</button>
                  <button onClick={() => { setNlParsed(null); setNlError(null); }} className={`flex-1 text-[14px] py-1.5 rounded-lg transition-colors ${"bg-[var(--t-panel)] border border-[var(--t-line2)] hover:bg-[var(--t-sunken)] text-[var(--t-soft)]"}`}>Try again</button>
                </div>
              </div>
            )}
            {!nlParsed && (
              <div className="flex gap-2 pt-1">
                <button onClick={parseNlEvent} disabled={nlLoading || !nlText.trim()}
                  className="flex-1 bg-[var(--t-accent-soft)] hover:bg-[var(--t-accent-soft)] disabled:opacity-50 text-[var(--t-ink)] text-[15px] font-bold py-2 rounded-lg transition-colors flex items-center justify-center gap-1.5">
                  {nlLoading ? <><div className="h-2.5 w-2.5 border-2 border-[var(--t-line2)] border-t-transparent rounded-full animate-spin" />Parsing…</> : "Parse"}
                </button>
                <button onClick={() => { setNlMode(false); setNlText(""); setNlError(null); setNlParsed(null); }}
                  className={`flex-1 text-[15px] py-2 rounded-lg transition-colors ${"bg-[var(--t-panel)] border border-[var(--t-line2)] hover:bg-[var(--t-sunken)] text-[var(--t-soft)]"}`}>Cancel</button>
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-2">
            <div className={`text-[14px] uppercase tracking-widest font-bold ${muted}`}>Add Event</div>
            <div className="grid grid-cols-2 gap-2">
              <button onClick={() => setAdding("price_change_permanent")} className={`${typeLabel.price_change_permanent.bg} ${typeLabel.price_change_permanent.color} border hover:brightness-105 text-[14px] font-bold py-2.5 px-2 rounded-lg transition-colors text-center leading-tight`}>Permanent<br/>Price Change</button>
              <button onClick={() => setAdding("price_change_temporary")} className={`${typeLabel.price_change_temporary.bg} ${typeLabel.price_change_temporary.color} border hover:brightness-105 text-[14px] font-bold py-2.5 px-2 rounded-lg transition-colors text-center leading-tight`}>Temporary<br/>Price Change</button>
              <button onClick={() => setAdding("promotion")} className={`${typeLabel.promotion.bg} ${typeLabel.promotion.color} border hover:brightness-105 text-[14px] font-bold py-2.5 px-2 rounded-lg transition-colors text-center leading-tight`}>Promotion<br/>Window</button>
              <button onClick={() => { setAdding("multibuy"); setForm({ buy_qty: "1", get_qty: "1" }); }} className={`${typeLabel.multibuy.bg} ${typeLabel.multibuy.color} border hover:brightness-105 text-[14px] font-bold py-2.5 px-2 rounded-lg transition-colors text-center leading-tight`}>Multi-buy<br/>(buy X get Y)</button>
              <button onClick={() => setNlMode(true)}
                className={`col-span-2 border hover:brightness-105 text-[14px] font-bold py-2.5 px-2 rounded-lg transition-colors text-center leading-tight ${"bg-[var(--t-accent-soft)] border-[var(--t-accent-line)] text-[var(--t-accent)]"}`}>
                ✨ Describe it<br/>(AI parse)
              </button>
            </div>
          </div>
        )}
        </div>

        <div className={`flex gap-2 pt-4 border-t mt-4 shrink-0 ${"border-[var(--t-line)]"}`}>
          <button onClick={() => onSave(localEvents)} className="flex-1 bg-[var(--t-accent-soft)] hover:bg-[var(--t-accent-soft)] text-[var(--t-ink)] text-[15px] font-bold py-2.5 rounded-xl transition-colors">Save & Update Forecast</button>
          <button onClick={onClose} className={`px-4 text-[15px] font-bold py-2.5 rounded-xl transition-colors ${"bg-[var(--t-panel)] border border-[var(--t-line2)] hover:bg-[var(--t-sunken)] text-[var(--t-soft)]"}`}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

// ─── ASSIGN FOLDER MODAL ──────────────────────
export function AssignFolderModal({ skuId, skuName, folders, onAssign, onClose, lm }) {
  const panel = "bg-[var(--t-panel)] border border-[var(--t-line2)] rounded-2xl p-5 w-[300px] space-y-3 shadow-2xl";
  const title = "text-[15px] font-bold text-[var(--t-ink)]";
  const sub   = "text-[15px] text-[var(--t-dim)] mt-0.5 font-mono truncate";
  const btn   = lm
    ? "w-full flex items-center gap-2.5 px-3 py-2 rounded-lg bg-[var(--t-sunken)] hover:bg-[var(--t-accent-soft)] border border-[var(--t-line)] hover:border-[var(--t-accent-line)] transition-all text-left"
    : "w-full flex items-center gap-2.5 px-3 py-2 rounded-lg bg-[var(--t-line)] hover:bg-[var(--t-line)] border border-[var(--t-line2)] hover:border-[var(--t-line2)] transition-all text-left";
  const cancel = lm
    ? "w-full text-[15px] text-[var(--t-dim)] hover:text-[var(--t-soft)] border border-[var(--t-line)] rounded-lg py-1.5 transition-colors"
    : "w-full text-[14px] text-[var(--t-dim)] hover:text-[var(--t-soft)] border border-[var(--t-line)] rounded-lg py-1.5 transition-colors";
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
              <svg className={`h-4 w-4 shrink-0 ${"text-[var(--t-dim)]"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
              </svg>
              <span className={`text-[15px] truncate ${"text-[var(--t-soft)]"}`}>
                {f.parentId && <span className={"text-[var(--t-dim)]"}>{folders[f.parentId]?.name || "?"} / </span>}
                {f.name}
              </span>
              <span className={`ml-auto text-[14px] font-mono ${"text-[var(--t-dim)]"}`}>{f.skuIds.length}</span>
            </button>
          ))}
          {Object.keys(folders).length === 0 && (
            <p className={`text-[15px] text-center py-3 ${"text-[var(--t-dim)]"}`}>No folders yet. Create one first.</p>
          )}
        </div>
        <button onClick={onClose} className={cancel}>Cancel</button>
      </div>
    </div>
  );
}
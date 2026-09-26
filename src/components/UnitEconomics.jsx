import React, { useState, useMemo } from 'react';
import { terminal, fs, MONO, SANS, scrim} from '../lib/theme';
import { saveStorage } from '../lib/storage';

/* ─── UNIT ECONOMICS ──────────────────────────────────────────────────────────
 *
 * Two surfaces over one pair of numbers, because they are two different jobs.
 *
 *   UnitEconomicsCard — one product, on its own page. This is where you CHECK and
 *   CORRECT. It sits directly above Stockout Protection, whose copy has always
 *   said "add a unit cost to tune" while the only input lived in another tab.
 *
 *   CostsSheet — every product missing a cost, in one list. This is the job the
 *   Scorecard tab was actually doing for people: a fresh catalogue of 32 products
 *   with no costs is not 32 visits to 32 pages. Reached from the lines on Fleet
 *   and Backtest that already say "add unit costs" and, until now, went nowhere.
 *
 * PROVENANCE IS PART OF THE VALUE. A cost is worth different amounts of trust
 * depending on where it came from, and until now nothing recorded that: once a
 * number landed in skuParams it was indistinguishable from one typed by hand. So
 * every value carries a source, and the precedence is fixed and stated:
 *
 *   your own entry  >  a connected store  >  your sales file  >  nothing
 *
 * A hand-typed cost is never overwritten by a later sync. That rule already
 * existed in App.jsx; what is new is that the screen can now say so.
 */

export const SOURCES = {
  manual:  { label: "set by you",      tone: "amber" },
  shopify: { label: "Shopify",         tone: "green" },
  square:  { label: "Square",          tone: "green" },
  sheet:   { label: "your sales file", tone: "blue"  },
  price:   { label: "from sales data", tone: "grey"  },
  none:    { label: "not set",         tone: "grey"  },
};

/* Every live store, for the copy that distinguishes "synced from a store" from "read out
   of a spreadsheet". Listing them beats `!== "sheet"` so that a future source has to be
   named here deliberately rather than silently inheriting Shopify's wording. */
export const LIVE_SOURCES = new Set(["shopify", "square"]);

const toneOf = (T, tone) => ({
  amber: { fg: T.amber,   bg: `${T.amber}18`,  br: `${T.amber}44` },
  green: { fg: T.greenFg, bg: T.greenBg,       br: `${T.green}44` },
  blue:  { fg: T.blueFg,  bg: T.blueBg,        br: `${T.blue}44`  },
  grey:  { fg: T.dim,     bg: "transparent",   br: T.line         },
}[tone] || { fg: T.dim, bg: "transparent", br: T.line });

export function SourceChip({ source, T }) {
  const s = SOURCES[source] || SOURCES.none;
  const c = toneOf(T, s.tone);
  return (
    <span style={{ fontFamily: MONO, fontSize: fs.tick, letterSpacing: ".06em",
      textTransform: "uppercase", padding: "2px 6px", whiteSpace: "nowrap",
      background: c.bg, color: c.fg, border: `2px solid ${c.br}` }}>{s.label}</span>
  );
}

/** A unit price, with the original struck through when the POS reports a discount.
    `list` is the undiscounted shelf price; `current` what a customer pays today. */
export function PriceTag({ current, list, T, size, muted = false }) {
  const mono = { fontFamily: MONO, fontVariantNumeric: "tabular-nums" };
  const c = Number(current), l = Number(list);
  if (!(c > 0) && !(l > 0)) return <span style={{ ...mono, fontSize: size, color: T.faint }}>-</span>;
  const onSale = c > 0 && l > 0 && c < l - 0.005;
  if (!onSale) {
    return <span style={{ ...mono, fontSize: size, color: muted ? T.soft : T.ink }}>${(c > 0 ? c : l).toFixed(2)}</span>;
  }
  return (
    <span style={{ display: "inline-flex", alignItems: "baseline", gap: 6 }}>
      <s style={{ ...mono, fontSize: size, color: T.faint }}>${l.toFixed(2)}</s>
      <span style={{ ...mono, fontSize: size, fontWeight: 700, color: T.amber }}>${c.toFixed(2)}</span>
    </span>
  );
}

/** Margin per sale, and the grade the rest of the app already uses for it. */
export function marginOf(price, cost, fees) {
  const p = Number(price), c = Number(cost), f = Number(fees) || 0;
  if (!(p > 0) || !(c > 0)) return null;
  const per = p - c - f;
  const pct = (per / p) * 100;
  const grade = pct >= 45 ? "A" : pct >= 30 ? "B" : pct >= 15 ? "C" : pct > 0 ? "D" : "F";
  return { per, pct, grade, loss: per <= 0 };
}

// ─── the card, for one product ───────────────────────────────────────────────
export function UnitEconomicsCard({ params = {}, price, listPrice = null, livePrice = null,
                                   onChange, lm = false }) {
  const T = terminal(lm);
  const [editing, setEditing] = useState(false);
  const mono = { fontFamily: MONO, fontVariantNumeric: "tabular-nums" };
  const cap  = { fontSize: fs.cap, letterSpacing: ".07em", textTransform: "uppercase", color: T.dim, fontWeight: 600 };

  const cost = params.unitCost, fees = params.fees;
  const hasCost = cost !== "" && cost != null && Number(cost) > 0;
  const src = params.unitCostSource || (hasCost ? "manual" : "none");
  const feeSrc = (fees !== "" && fees != null && Number(fees) > 0) ? "manual" : "none";
  const m = marginOf(price, cost, fees);
  // The POS's own reading of the shelf, when there is one: the source chip says so, and a
  // discount shows the original struck through beside what customers pay today.
  const live = livePrice && (livePrice.currentPrice || livePrice.listPrice) ? livePrice : null;
  const regular = Number(listPrice) > 0 ? Number(listPrice) : null;
  const onSale = regular != null && Number(price) > 0 && Number(price) < regular - 0.005;
  const mReg = onSale ? marginOf(regular, cost, fees) : null;
  const fmtDay = (iso) => { try { return new Date(iso + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" }); } catch { return iso; } };

  const inp = { ...mono, width: 96, textAlign: "right", background: T.bg, color: T.ink,
    border: `2px solid ${T.line2}`, borderRadius: 2, padding: "5px 8px", fontSize: fs.row, outline: "none" };

  const Row = ({ label, children }) => (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
      gap: 8, padding: "6px 0", minHeight: 30 }}>
      <span style={{ fontSize: fs.body, color: T.soft }}>{label}</span>
      {children}
    </div>
  );

  return (
    <div style={{ background: T.panel, border: `2px solid ${T.line}`, padding: "13px 14px", fontFamily: SANS }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span style={cap}>Unit economics</span>
        <button onClick={() => setEditing(v => !v)}
          style={{ ...mono, background: "transparent", border: "none", color: T.amber,
            fontSize: fs.small, cursor: "pointer", padding: 0 }}>
          {editing ? "Done" : "Edit"}
        </button>
      </div>

      <div style={{ marginTop: 6 }}>
        <Row label="Selling price">
          <span style={{ display: "flex", alignItems: "center", gap: 7 }}>
            <SourceChip source={live && SOURCES[live.source] ? live.source : "price"} T={T} />
            <PriceTag current={price} list={regular} T={T} size={fs.num} />
          </span>
        </Row>
        {onSale && (
          <div style={{ fontSize: fs.small, color: T.amber, marginTop: -2, marginBottom: 4, lineHeight: 1.5, textAlign: "right" }}>
            {live?.discountName || "Discount"}{live?.discountPct ? ` · ${Math.round(live.discountPct)}% off` : ""}
            {live?.discountUntil ? ` · until ${fmtDay(live.discountUntil)}` : " · no end date set"}
          </div>
        )}

        <Row label="Unit cost">
          {editing ? (
            <input type="number" min="0" step="0.01" autoFocus placeholder="0.00"
              value={cost ?? ""} style={inp}
              onChange={e => onChange({ unitCost: e.target.value === "" ? "" : Number(e.target.value),
                                        unitCostSource: "manual" })} />
          ) : (
            <span style={{ display: "flex", alignItems: "center", gap: 7 }}>
              <SourceChip source={src} T={T} />
              {hasCost
                ? <span style={{ ...mono, fontSize: fs.num, color: T.ink }}>${Number(cost).toFixed(2)}</span>
                : <button onClick={() => setEditing(true)}
                    style={{ ...mono, background: "transparent", border: "none", color: T.amber,
                      fontSize: fs.num, cursor: "pointer", padding: 0 }}>Add cost</button>}
            </span>
          )}
        </Row>

        <Row label="Fees / unit">
          {editing ? (
            <input type="number" min="0" step="0.01" placeholder="0.00"
              value={fees ?? ""} style={inp}
              onChange={e => onChange({ fees: e.target.value === "" ? "" : Number(e.target.value) })} />
          ) : (
            <span style={{ display: "flex", alignItems: "center", gap: 7 }}>
              <SourceChip source={feeSrc} T={T} />
              <span style={{ ...mono, fontSize: fs.num, color: feeSrc === "none" ? T.faint : T.ink }}>
                ${Number(fees || 0).toFixed(2)}
              </span>
            </span>
          )}
        </Row>
      </div>

      <div style={{ borderTop: `2px solid ${T.line}`, marginTop: 4, paddingTop: 9,
        display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <span style={{ fontSize: fs.body, color: T.soft }}>Margin per sale</span>
        {m ? (
          <span style={{ display: "flex", alignItems: "baseline", gap: 7 }}>
            <span style={{ ...mono, fontSize: fs.num, fontWeight: 600, color: m.loss ? T.red : T.green }}>
              ${m.per.toFixed(2)}
            </span>
            <span style={{ ...mono, fontSize: fs.small, color: T.soft }}>{Math.round(m.pct)}%</span>
            <span style={{ ...mono, fontSize: fs.small, fontWeight: 700, padding: "1px 6px",
              background: m.loss ? T.redBg : T.greenBg, color: m.loss ? T.redFg : T.greenFg }}>{m.grade}</span>
          </span>
        ) : <span style={{ ...mono, fontSize: fs.num, color: T.faint }}>-</span>}
      </div>
      {mReg && (
        <div style={{ fontSize: fs.small, color: T.soft, marginTop: 4, textAlign: "right" }}>
          At the regular ${regular.toFixed(2)}: ${mReg.per.toFixed(2)} ({Math.round(mReg.pct)}%). Protection is sized on this.
        </div>
      )}

      {/* Why this card exists, said only when it matters. With a cost on file the
          protection tier is chosen from this product's own stockout-vs-holding cost;
          without one it falls back to the margin rule, and the page should say so
          rather than leave the tier looking equally well-founded either way. */}
      <div style={{ fontSize: fs.small, color: hasCost ? T.faint : T.amber, marginTop: 9, lineHeight: 1.6 }}>
        {!hasCost
          ? "Protection uses the margin rule until you add a cost. With one, this product's level is set from its own stockout cost vs. buffer holding cost."
          : LIVE_SOURCES.has(src) ? `Cost came from ${SOURCES[src].label}. Type over it any time; a later sync never overwrites your own entry.`
          : src === "sheet"   ? "Read from the Cost column of your sales file. Type over it any time; your own entry wins."
          : "Set by you. Nothing overwrites this."}
      </div>
    </div>
  );
}

// ─── the sheet, for the whole catalogue ──────────────────────────────────────
export function CostsSheet({ open, onClose, skuList = [], scorecardRows = [],
                             skuParams = {}, setSkuParams, lm = false }) {
  const T = terminal(lm);
  const [q, setQ] = useState("");
  const [onlyMissing, setOnlyMissing] = useState(true);
  const mono = { fontFamily: MONO, fontVariantNumeric: "tabular-nums" };
  const cap  = { fontSize: fs.cap, letterSpacing: ".07em", textTransform: "uppercase", color: T.dim, fontWeight: 600 };

  const priceBySku = useMemo(
    () => Object.fromEntries((scorecardRows || []).map(r => [r.skuId, r.regularPrice])), [scorecardRows]);
  const saleBySku = useMemo(
    () => Object.fromEntries((scorecardRows || []).filter(r => r.onPromoToday && r.promoPrice > 0)
      .map(r => [r.skuId, r.promoPrice])), [scorecardRows]);

  const rows = useMemo(() => {
    const list = (skuList || []).map(s => {
      const p = skuParams[s.id] || {};
      const cost = p.unitCost;
      return { id: s.id, name: s.name || s.id, cost, fees: p.fees,
               source: p.unitCostSource || (Number(cost) > 0 ? "manual" : "none"),
               price: priceBySku[s.id], missing: !(Number(cost) > 0) };
    });
    const term = q.trim().toLowerCase();
    return list
      .filter(r => !onlyMissing || r.missing)
      .filter(r => !term || `${r.name} ${r.id}`.toLowerCase().includes(term))
      // Missing first: that is the work. Within each group, keep catalogue order.
      .sort((a, b) => (b.missing ? 1 : 0) - (a.missing ? 1 : 0));
  }, [skuList, skuParams, priceBySku, q, onlyMissing]);

  const missingCount = (skuList || []).filter(s => !(Number(skuParams[s.id]?.unitCost) > 0)).length;

  if (!open) return null;

  const set = (id, patch) => setSkuParams(prev => {
    const next = { ...prev, [id]: { ...(prev[id] || {}), ...patch } };
    saveStorage("logitrack_params", next);
    return next;
  });

  const inp = { ...mono, width: "100%", textAlign: "right", background: T.bg, color: T.ink,
    border: `2px solid ${T.line2}`, borderRadius: 2, padding: "6px 8px", fontSize: fs.row,
    outline: "none", boxSizing: "border-box" };
  const GRID = "minmax(0,1fr) 96px 108px 104px 118px";

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 60, fontFamily: SANS }}>
      <div onClick={onClose} style={scrim(lm)} />
      <div style={{ position: "absolute", top: "5vh", left: "50%", transform: "translateX(-50%)",
        width: "min(880px, 94vw)", maxHeight: "90vh", display: "flex", flexDirection: "column",
        background: T.bg, color: T.ink, border: `2px solid ${T.line2}`, boxShadow: "0 24px 60px rgba(0,0,0,.5)" }}>

        <div style={{ padding: "18px 20px 14px", borderBottom: `2px solid ${T.line}` }}>
          <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16 }}>
            <div>
              <div style={{ fontSize: 22, fontWeight: 700, letterSpacing: "-.02em" }}>Costs &amp; fees</div>
              <div style={{ fontSize: fs.body, color: T.soft, marginTop: 5, maxWidth: 560, lineHeight: 1.6 }}>
                What you pay per unit, and any per-unit fee on top. These drive margin, profit grade
                and how hard each product is protected against running out.
              </div>
            </div>
            <button onClick={onClose} style={{ ...mono, background: "transparent", border: "none",
              color: T.soft, fontSize: 20, cursor: "pointer", padding: "0 4px", lineHeight: 1 }}>×</button>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 14, flexWrap: "wrap" }}>
            <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search products…"
              style={{ ...inp, textAlign: "left", width: 240, fontFamily: SANS }} />
            <button onClick={() => setOnlyMissing(v => !v)}
              style={{ ...mono, fontSize: fs.body, padding: "6px 11px", borderRadius: 2, cursor: "pointer",
                background: onlyMissing ? T.amber : "transparent", color: onlyMissing ? T.onFill : T.soft,
                border: `2px solid ${onlyMissing ? T.amber : T.line2}` }}>
              {onlyMissing ? `Missing a cost · ${missingCount}` : `Showing all · ${skuList.length}`}
            </button>
            <span style={{ marginLeft: "auto", fontSize: fs.body, color: T.soft }}>
              {missingCount === 0
                ? "Every product has a cost."
                : `${missingCount} of ${skuList.length} still need one.`}
            </span>
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: GRID, gap: "0 14px", padding: "9px 20px",
          ...cap, background: T.sunken, borderBottom: `2px solid ${T.line}` }}>
          <span>Product</span>
          <span style={{ textAlign: "right" }}>Price</span>
          <span style={{ textAlign: "right" }}>Unit cost</span>
          <span style={{ textAlign: "right" }}>Fees</span>
          <span style={{ textAlign: "right" }}>Margin</span>
        </div>

        <div style={{ overflowY: "auto", flex: 1 }}>
          {rows.length === 0 && (
            <div style={{ padding: "34px 20px", textAlign: "center", fontSize: fs.body, color: T.soft }}>
              {q ? `No product matches “${q}”.` : "Nothing missing a cost."}
            </div>
          )}
          {rows.map(r => {
            const m = marginOf(r.price, r.cost, r.fees);
            return (
              <div key={r.id} style={{ display: "grid", gridTemplateColumns: GRID, gap: "0 14px",
                alignItems: "center", padding: "9px 20px", borderBottom: `2px solid ${T.line}` }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: fs.row, fontWeight: 600, whiteSpace: "nowrap",
                    overflow: "hidden", textOverflow: "ellipsis" }}>{r.name}</div>
                  <div style={{ display: "flex", alignItems: "center", gap: 7, marginTop: 3 }}>
                    <span style={{ ...mono, fontSize: fs.small, color: T.faint }}>{r.id}</span>
                    {!r.missing && <SourceChip source={r.source} T={T} />}
                  </div>
                </div>
                <span style={{ textAlign: "right" }}>
                  <PriceTag current={saleBySku[r.id] ?? r.price} list={r.price} T={T} size={fs.row} muted />
                </span>
                <input type="number" min="0" step="0.01" placeholder="-" style={inp}
                  value={r.cost ?? ""}
                  onChange={e => set(r.id, { unitCost: e.target.value === "" ? "" : Number(e.target.value),
                                             unitCostSource: "manual" })} />
                <input type="number" min="0" step="0.01" placeholder="0.00" style={inp}
                  value={r.fees ?? ""}
                  onChange={e => set(r.id, { fees: e.target.value === "" ? "" : Number(e.target.value) })} />
                <span style={{ textAlign: "right" }}>
                  {m ? (
                    <span style={{ display: "inline-flex", alignItems: "baseline", gap: 6 }}>
                      <span style={{ ...mono, fontSize: fs.row, fontWeight: 600, color: m.loss ? T.red : T.green }}>
                        {Math.round(m.pct)}%
                      </span>
                      <span style={{ ...mono, fontSize: fs.small, fontWeight: 700, padding: "1px 5px",
                        background: m.loss ? T.redBg : T.greenBg, color: m.loss ? T.redFg : T.greenFg }}>{m.grade}</span>
                    </span>
                  ) : <span style={{ ...mono, fontSize: fs.row, color: T.faint }}>-</span>}
                </span>
              </div>
            );
          })}
        </div>

        <div style={{ padding: "12px 20px", borderTop: `2px solid ${T.line}`, background: T.sunken,
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 14, flexWrap: "wrap" }}>
          <span style={{ fontSize: fs.small, color: T.soft, lineHeight: 1.6, maxWidth: 560 }}>
            Saves as you type, and the backtest re-runs itself. A store sync or re-upload never
            overwrites a cost you type here.
          </span>
          <button onClick={onClose} style={{ ...mono, fontSize: fs.body, fontWeight: 600, padding: "8px 16px",
            borderRadius: 2, background: T.btnBg, color: T.btnFg, border: "2px solid transparent",
            cursor: "pointer" }}>Done</button>
        </div>
      </div>
    </div>
  );
}

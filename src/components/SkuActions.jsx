import React, { useEffect, useState } from 'react';
import { TIER_STYLES } from './common';
import { isoToDisplay } from '../lib/helpers';
import { API, fetchJson } from '../lib/api';

/* ─── SKU ACTION BAR ────────────────────────────────────────────────────────
 *
 * The row of buttons at the top of a product page. Each one is a tile with two lines:
 * what it opens, and the one fact worth knowing before you open it. Forecast health
 * used to be a strip of its own further down the page; it is now the colour of the
 * Forecast details tile, so the verdict is visible from the top without a click, and
 * the detail sits one click away where it belongs.
 */

const TONES = {
  good: { dot: "var(--t-good)", soft: "var(--t-good-soft)", line: "var(--t-good-line)", text: "var(--t-good)" },
  warn: { dot: "var(--t-warn)", soft: "var(--t-warn-soft)", line: "var(--t-warn-line)", text: "var(--t-warn)" },
  bad:  { dot: "var(--t-bad)",  soft: "var(--t-bad-soft)",  line: "var(--t-bad-line)",  text: "var(--t-bad)"  },
};

const STATUS_TONE = { FORECAST_ELIGIBLE: "good", LOW_CONFIDENCE: "warn", CONFLICTING: "warn", INSUFFICIENT: "bad" };
const DATA_TONE   = { GREEN: "good", YELLOW: "warn", ORANGE: "warn", RED: "bad" };
const DATA_LABEL  = { GREEN: "Strong data", YELLOW: "Moderate data", ORANGE: "Limited data", RED: "Thin data" };
const RANK = { good: 0, warn: 1, bad: 2 };

/** One colour for the whole forecast: the worse of its grade and its data strength.
 *  Noise is left out on purpose — a product that sells unevenly is not a broken
 *  forecast, and its wider band already says so. */
export function forecastHealth(mlData, statusCfg) {
  const a = STATUS_TONE[mlData?.status] || "bad";
  const b = DATA_TONE[mlData?.reliabilityColor] || "good";
  const tone = RANK[a] >= RANK[b] ? a : b;
  const word = { good: "Healthy", warn: "Use with care", bad: "Weak" }[tone];
  return { tone, word, grade: statusCfg?.label || "-", data: DATA_LABEL[mlData?.reliabilityColor] || "Strong data" };
}

function Tile({ onClick, disabled, title, label, count, sub, subTone, tone, icon, primary }) {
  const t = tone ? TONES[tone] : null;
  return (
    <button onClick={onClick} disabled={disabled} title={title}
      className="group text-left rounded-xl px-4 py-3 transition-colors disabled:opacity-60 min-w-0 hover:brightness-110"
      style={{
        background: t ? t.soft : primary ? "var(--t-btn-bg)" : "var(--t-panel)",
        borderStyle: "solid",
        borderWidth: 1,
        borderColor: t ? t.line : primary ? "transparent" : "var(--t-line2)",
        borderLeftWidth: t ? 4 : 1,
        borderLeftColor: t ? t.dot : primary ? "transparent" : "var(--t-line2)",
        color: primary ? "var(--t-btn-fg)" : "var(--t-ink)",
      }}>
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-2 min-w-0">
          {t ? <span className="h-2.5 w-2.5 rounded-full shrink-0" style={{ background: t.dot }} />
             : <span className="shrink-0 opacity-80">{icon}</span>}
          <span className="text-[15.5px] font-semibold truncate">{label}</span>
        </span>
        {count != null && (
          <span className="font-mono text-[14px] font-semibold px-2 py-0.5 rounded-md shrink-0"
            style={{ background: "var(--t-sunken)", color: "var(--t-soft)", border: "1px solid var(--t-line)" }}>
            {count}
          </span>
        )}
      </div>
      <div className="text-[13.5px] mt-1.5 leading-snug"
        style={{ color: subTone ? TONES[subTone].text : primary ? "var(--t-btn-fg)" : "var(--t-dim)",
                 opacity: primary ? 0.75 : 1 }}>
        {sub}
      </div>
    </button>
  );
}

const I = {
  price: <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.7} d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" /></svg>,
  events: <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.7} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>,
  truck: <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.7} d="M3 7h11v9H3zM14 10h4l3 3v3h-7M7.5 18.5a1.5 1.5 0 100-3 1.5 1.5 0 000 3zm10 0a1.5 1.5 0 100-3 1.5 1.5 0 000 3z" /></svg>,
  holiday: <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.7} d="M12 3v2m0 14v2m9-9h-2M5 12H3m15.364-6.364l-1.414 1.414M7.05 16.95l-1.414 1.414m12.728 0l-1.414-1.414M7.05 7.05L5.636 5.636M16 12a4 4 0 11-8 0 4 4 0 018 0z" /></svg>,
  export: <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" /></svg>,
};

export function SkuActionBar({ mlData, statusCfg, statusReason, hasOpenPO, openPO, exporting,
                               onForecastDetails, onPriceHistory, onHolidays, onEvents, onOrder, onExport }) {
  const h = forecastHealth(mlData, statusCfg);
  const nPrices = mlData.hasPrice ? (mlData.priceTiers?.length || 0) : 0;
  const onSale = !!mlData.priceDiscounted;
  const priceSub = !mlData.hasPrice ? "No price data"
    : onSale && mlData.currentPrice != null ? `On sale now · $${mlData.currentPrice.toFixed(2)}`
    : mlData.priceResponse?.applies ? "Moves the forecast"
    : "Not used in the forecast yet";
  const nEvents = mlData.events?.length || 0;
  const livePromo = (mlData.posEvents || []).some(e => e.type === "promotion");
  const eventSub = livePromo ? `Live discount${nEvents ? ` · ${nEvents} scheduled` : ""}`
    : nEvents ? `${nEvents} scheduled` : "None scheduled";
  const hol = (mlData.holidays?.effects || []).filter(e => e.status === "applied");
  const pctTxt = (p) => `${p > 0 ? "+" : ""}${p}%`;
  const holSub = !mlData.holidays ? "Checked once there's history"
    : hol.length ? (() => {
        const top = [...hol].sort((a, b) => Math.abs(b.pct) - Math.abs(a.pct))[0];
        return `${top.name.replace(" weekend", "")} ${pctTxt(top.pct)}${hol.length > 1 ? ` · +${hol.length - 1} more` : ""}`;
      })()
    : "No clear holiday effect";

  return (
    <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(165px, 1fr))" }}>
      <Tile onClick={onForecastDetails} tone={h.tone} label="Forecast details"
        title={`How this forecast was made, and how far to trust it${statusReason ? `. ${statusReason}` : ""}`}
        sub={`${h.grade} · ${h.data}`} subTone={h.tone} />
      <Tile onClick={onPriceHistory} icon={I.price} label="Price history" count={mlData.hasPrice ? nPrices : null}
        title="Every price this product has sold at, how long at each, and whether price moves the forecast"
        sub={priceSub} subTone={onSale ? "warn" : undefined} />
      <Tile onClick={onHolidays} icon={I.holiday} label="Holidays" count={mlData.holidays ? hol.length : null}
        title="Which holidays and sales periods move this product, year by year, from its own sales history"
        sub={holSub} />
      <Tile onClick={onEvents} icon={I.events} label="Events" count={nEvents || null}
        title="Planned price changes and promotions" sub={eventSub} subTone={livePromo ? "warn" : undefined} />
      <Tile onClick={onOrder} icon={I.truck} label={hasOpenPO ? "Order in transit" : "Mark order in transit"}
        title={hasOpenPO ? "Edit or clear the order on its way" : "Record an order you've placed so it counts toward stock"}
        sub={hasOpenPO ? `${(openPO?.qty ?? 0).toLocaleString()} units on the way` : "Nothing on the way"}
        subTone={hasOpenPO ? "good" : undefined} />
      <Tile onClick={onExport} disabled={!!exporting} icon={I.export} label={exporting ? "Preparing…" : "Export"} primary
        title="Download this SKU's forecast, daily projection and monthly breakdown as Excel"
        sub="Excel workbook" />
    </div>
  );
}

/* ─── PRICE HISTORY ─────────────────────────────────────────────────────────
 * Every price this product has sold at on its own (days where different customers
 * paid different prices are counted separately and never as a price), how long it
 * sold there, how much that is worth as evidence, and whether price is allowed to
 * move the forecast at all. */
export function PriceHistoryDrawer({ open, onClose, mlData }) {
  const [changes, setChanges] = useState(null);
  useEffect(() => {
    if (!open || !mlData?.skuId) return;
    let dead = false;
    setChanges(null);
    fetchJson(`${API}/api/price-history?sku=${encodeURIComponent(mlData.skuId)}&days=1500`, undefined, "Price history")
      .then(d => { if (!dead) setChanges(d?.series || []); });
    return () => { dead = true; };
  }, [open, mlData?.skuId]);
  if (!open) return null;

  const levels = [...(mlData.priceTiers || [])].sort((a, b) => b.days - a.days);
  const pr = mlData.priceResponse || {};
  const applies = !!pr.applies;
  const current = mlData.currentPrice ?? mlData.lastPrice;
  const list = mlData.listPrice;
  const onSale = !!mlData.priceDiscounted && list != null && current != null && current < list - 0.005;
  const live = mlData.livePrice;
  const src = live?.source ? live.source[0].toUpperCase() + live.source.slice(1) : null;
  const refPrice = list ?? current;
  const muted = { color: "var(--t-dim)" };
  const Section = ({ title, children }) => (
    <div className="mb-6">
      <div className="text-[13px] font-semibold uppercase tracking-widest mb-2.5" style={muted}>{title}</div>
      {children}
    </div>
  );
  const card = { background: "var(--t-sunken)", border: "1px solid var(--t-line)" };

  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-[1px]" onClick={onClose} />
      <aside className="absolute right-0 top-0 h-full w-full max-w-md overflow-y-auto border-l shadow-2xl"
        style={{ background: "var(--t-panel)", borderColor: "var(--t-line)", color: "var(--t-ink)" }}>
        <div className="sticky top-0 z-10 flex items-center justify-between px-6 py-4 border-b"
          style={{ background: "var(--t-panel)", borderColor: "var(--t-line)" }}>
          <div>
            <div className="text-[16.5px] font-bold">Price history</div>
            <div className="text-[14px]" style={muted}>{mlData.skuName || mlData.skuId}</div>
          </div>
          <button onClick={onClose} className="rounded-lg px-2 py-1 text-lg leading-none" style={muted}>×</button>
        </div>

        <div className="px-6 py-6">
          {!mlData.hasPrice ? (
            <div className="rounded-xl p-4 text-[15px] leading-relaxed" style={{ ...card, color: "var(--t-soft)" }}>
              No price data yet for this product. There's no history to show, and price changes
              can't affect its forecast.
            </div>
          ) : (<>
            <Section title="Price today">
              <div className="rounded-xl p-4" style={card}>
                <div className="flex items-baseline gap-3">
                  {onSale && <s className="font-mono text-[17px]" style={muted}>${list.toFixed(2)}</s>}
                  <span className="font-mono text-[26px] font-semibold"
                    style={{ color: onSale ? "var(--t-warn)" : "var(--t-ink)" }}>
                    {current != null ? `$${current.toFixed(2)}` : "-"}
                  </span>
                </div>
                <div className="text-[14px] mt-1.5 leading-relaxed" style={{ color: "var(--t-soft)" }}>
                  {onSale
                    ? <>{live?.discountName || "Discount"}{live?.discountPct ? ` · ${Math.round(live.discountPct)}% off` : ""}
                        {live?.discountUntil ? ` · until ${isoToDisplay(live.discountUntil)}` : " · no end date set"}</>
                    : src ? `Read from ${src}, checked every hour.` : "The last price a day sold at, from your sales."}
                </div>
              </div>
            </Section>

            <Section title="Does price move the forecast?">
              <div className="rounded-xl p-4 border"
                style={{ background: applies ? "var(--t-good-soft)" : "var(--t-warn-soft)",
                         borderColor: applies ? "var(--t-good-line)" : "var(--t-warn-line)" }}>
                <div className="text-[15px] font-semibold mb-1"
                  style={{ color: applies ? "var(--t-good)" : "var(--t-warn)" }}>
                  {applies ? "Yes" : "Not yet"}
                </div>
                <p className="text-[14.5px] leading-relaxed" style={{ color: applies ? "var(--t-good)" : "var(--t-warn)" }}>
                  {(pr.text || "").replace(/^This price change won't move the forecast\.\s*/, "")
                    .replace(", so this change shifts the forecast.", ".")
                    .replace(", and this change shifts the forecast to match.", ".")}
                </p>
                {applies && mlData.priceTrainedMin != null && mlData.priceSafeMin != null && (
                  <p className="text-[13.5px] leading-relaxed mt-2" style={{ color: "var(--t-soft)" }}>
                    Known range ${mlData.priceTrainedMin.toFixed(2)} to ${mlData.priceTrainedMax.toFixed(2)} ·
                    extrapolated down to ${mlData.priceSafeMin.toFixed(2)} and up to ${mlData.priceSafeMax.toFixed(2)}.
                  </p>
                )}
              </div>
            </Section>

            <Section title={`Prices detected · ${levels.length}`}>
              <div className="rounded-xl overflow-hidden" style={card}>
                <div className="grid px-4 py-2 text-[12.5px] font-semibold uppercase tracking-wider"
                  style={{ gridTemplateColumns: "1fr 90px 120px", ...muted, borderBottom: "1px solid var(--t-line)" }}>
                  <span>Price</span><span className="text-right">Days</span><span className="text-right">Reliability</span>
                </div>
                {levels.map((t, i) => {
                  const ts = TIER_STYLES[t.quality] || TIER_STYLES.insufficient;
                  const isNow = refPrice != null && Math.abs(t.price - refPrice) < 0.015;
                  const pct = refPrice ? Math.round((t.price - refPrice) / refPrice * 100) : null;
                  return (
                    <div key={i} className="grid items-center px-4 py-3"
                      style={{ gridTemplateColumns: "1fr 90px 120px", borderTop: i ? "1px solid var(--t-line)" : "none" }}>
                      <span className="flex items-center gap-2">
                        <span className="font-mono text-[15.5px] font-semibold">${t.price.toFixed(2)}</span>
                        {isNow ? <span className="text-[12px] font-semibold px-1.5 py-0.5 rounded"
                                   style={{ background: "var(--t-accent-soft)", color: "var(--t-accent)" }}>regular</span>
                               : pct != null && <span className="font-mono text-[12.5px]" style={muted}>{pct > 0 ? "+" : ""}{pct}%</span>}
                      </span>
                      <span className="font-mono text-[15px] text-right">{t.days.toLocaleString()}</span>
                      <span className={`text-right text-[14px] font-semibold ${ts.tc()}`}>{ts.label}</span>
                    </div>
                  );
                })}
              </div>
              <p className="text-[13px] mt-2.5 leading-relaxed" style={muted}>
                Reliable 90+ days · Moderate 60 to 89 · Weak 30 to 59 · Insufficient under 30. A price
                counts toward the forecast after 30 days. Price moves the forecast once two prices reach that.
              </p>
              {mlData.priceMixedDays > 0 && (
                <p className="text-[13px] mt-2 leading-relaxed" style={{ color: "var(--t-soft)" }}>
                  {mlData.priceMixedDays} day{mlData.priceMixedDays === 1 ? "" : "s"} sold at more than one
                  price (some customers paid less, e.g. a register discount). Those days don't count toward
                  any price.
                </p>
              )}
            </Section>

            <Section title="Changes over time">
              {changes == null ? (
                <div className="text-[14px]" style={muted}>Loading…</div>
              ) : changes.length <= 1 ? (
                <div className="text-[14px]" style={{ color: "var(--t-soft)" }}>
                  {changes.length ? `Unchanged at $${changes[0].price.toFixed(2)} since ${isoToDisplay(changes[0].date)}.` : "No changes on record."}
                </div>
              ) : (
                <div className="rounded-xl overflow-hidden" style={card}>
                  {[...changes].reverse().slice(0, 40).map((c, i) => (
                    <div key={i} className="flex items-center justify-between px-4 py-2.5"
                      style={{ borderTop: i ? "1px solid var(--t-line)" : "none" }}>
                      <span className="text-[14px]" style={{ color: "var(--t-soft)" }}>{isoToDisplay(c.date)}</span>
                      <span className="flex items-center gap-3">
                        {c.changePct != null && (
                          <span className="font-mono text-[13px]"
                            style={{ color: c.changePct < 0 ? "var(--t-warn)" : "var(--t-good)" }}>
                            {c.changePct > 0 ? "+" : ""}{c.changePct}%
                          </span>
                        )}
                        <span className="font-mono text-[15px] font-semibold">${c.price.toFixed(2)}</span>
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Section>
          </>)}
        </div>
      </aside>
    </div>
  );
}


/* ─── HOLIDAYS ──────────────────────────────────────────────────────────────
 * Every holiday and sales period is checked automatically in this product's own sales
 * history; nobody has to remember which ones matter. This shows what each past year
 * actually did, the lift the forecast uses (day by day where the days differ), where it
 * came from, and when it next applies. A period can be switched off for this product
 * (a one-off you know about); upcoming Prime Day dates are added here too and apply to
 * every product. Periods this product hasn't been through yet aren't listed. */
const fmtD = (iso, opts = { month: "short", day: "numeric" }) => {
  const d = new Date(`${iso}T12:00:00Z`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString("en-US", { ...opts, timeZone: "UTC" });
};
const fmtRange = (a, b) => (a === b ? fmtD(a) : `${fmtD(a)} to ${fmtD(b)}`);
const wd = (iso) => fmtD(iso, { weekday: "short" });
const signed = (p) => `${p > 0 ? "+" : ""}${p}%`;

/* Defined out here, not inside the drawer: a component declared inside another is a new
   type on every render, so React would remount it and an input inside would lose focus
   after each keystroke. */
function HSection({ title, children, right }) {
  return (
    <div className="mb-6">
      <div className="flex items-center mb-2.5">
        <div className="text-[13px] font-semibold uppercase tracking-widest" style={{ color: "var(--t-dim)" }}>{title}</div>
        {right && <div className="ml-auto">{right}</div>}
      </div>
      {children}
    </div>
  );
}

function HYears({ r }) {
  if (!(r.years || []).length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {r.years.map((y, i) => (
        <span key={i} className="font-mono text-[12.5px] px-1.5 py-0.5 rounded"
          style={{ background: "var(--t-panel)", border: "1px solid var(--t-line)", color: "var(--t-soft)" }}
          title={`${y.units} sold vs about ${y.ordinary} on ordinary days`}>
          {y.start.slice(0, 4)}: {signed(y.pct)} <span style={{ color: "var(--t-dim)" }}>({y.units} vs {y.ordinary})</span>
        </span>
      ))}
    </div>
  );
}

export function HolidaysDrawer({ open, onClose, mlData, onChanged }) {
  const [cal, setCal] = useState(null);           // GET /api/holidays
  const [busy, setBusy] = useState(null);         // key being switched, or "store"
  const [showQuiet, setShowQuiet] = useState(false);
  const [prime, setPrime] = useState({ start: "", end: "" });
  const [err, setErr] = useState(null);
  const [downloading, setDownloading] = useState(false);

  const load = () => fetchJson(`${API}/api/holidays`, undefined, "Holidays").then(d => { if (d) setCal(d); return d; });
  useEffect(() => { if (open) { setErr(null); load(); } }, [open]);   // eslint-disable-line react-hooks/exhaustive-deps
  if (!open) return null;

  const rep = mlData.holidays;
  const rows = rep?.effects || [];
  const by = (st) => rows.filter(r => r.status === st);
  const applied = by("applied"), quiet = [...by("none"), ...by("discounted")], off = by("off");
  const ignored = rows.filter(r => r.status === "off").map(r => r.key);
  const refitting = cal?.refit?.status === "running";
  const muted = { color: "var(--t-dim)" };
  const card = { background: "var(--t-sunken)", border: "1px solid var(--t-line)" };
  const inp = { background: "var(--t-bg)", border: "1px solid var(--t-line2)", color: "var(--t-ink)" };
  const linkBtn = "text-[13.5px] font-semibold underline underline-offset-2 disabled:opacity-50";

  const setSwitch = async (key, on) => {
    setBusy(key); setErr(null);
    const next = on ? ignored.filter(k => k !== key) : [...ignored, key];
    const res = await fetchJson(`${API}/api/skus/${encodeURIComponent(mlData.skuId)}/holidays`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ignored: next }),
    }, "Switching a holiday");
    setBusy(null);
    if (res) onChanged();
  };

  const saveStore = async (patch) => {
    setBusy("store"); setErr(null);
    const res = await fetchJson(`${API}/api/holidays`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ custom: cal?.settings?.custom || [], dates: cal?.settings?.dates || {}, ...patch }),
    }, "Saving holidays");
    if (!res) { setBusy(null); return; }
    let d = await load();
    while (d?.refit?.status === "running") {
      await new Promise(r => setTimeout(r, 3000));
      d = await load();
    }
    setBusy(null);
    onChanged();
  };

  const primeRow = (cal?.catalog || []).find(r => r.key === "prime_day");
  const addPrime = () => {
    if (!prime.start) { setErr("Pick the first day of Prime Day."); return; }
    const end = prime.end || prime.start;
    if (end < prime.start) { setErr("The end date is before the start date."); return; }
    const dates = { ...(cal?.settings?.dates || {}) };
    dates.prime_day = [...(dates.prime_day || []), [prime.start, end]];
    setPrime({ start: "", end: "" });
    saveStore({ dates });
  };

  const download = async () => {
    setDownloading(true);
    try {
      const r = await fetch(`${API}/api/holidays/report`);
      if (!r.ok) throw new Error();
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `logitrack_holidays_${new Date().toISOString().slice(0, 10)}.xlsx`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    } catch { setErr("The report couldn't be downloaded. Is the backend running?"); }
    setDownloading(false);
  };


  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-[1px]" onClick={onClose} />
      <aside className="absolute right-0 top-0 h-full w-full max-w-lg overflow-y-auto border-l shadow-2xl"
        style={{ background: "var(--t-panel)", borderColor: "var(--t-line)", color: "var(--t-ink)" }}>
        <div className="sticky top-0 z-10 flex items-center justify-between px-6 py-4 border-b"
          style={{ background: "var(--t-panel)", borderColor: "var(--t-line)" }}>
          <div>
            <div className="text-[16.5px] font-bold">Holidays</div>
            <div className="text-[14px]" style={muted}>{mlData.skuName || mlData.skuId}</div>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={download} disabled={downloading}
              className="rounded-lg px-3 py-1.5 text-[13.5px] font-semibold border disabled:opacity-60"
              style={{ borderColor: "var(--t-line2)", color: "var(--t-soft)" }}
              title="Every product against every holiday, year by year, as Excel">
              {downloading ? "Preparing…" : "Download report"}
            </button>
            <button onClick={onClose} className="rounded-lg px-2 py-1 text-lg leading-none" style={muted}>×</button>
          </div>
        </div>

        <div className="px-6 py-6">
          <p className="text-[14.5px] leading-relaxed mb-5" style={{ color: "var(--t-soft)" }}>
            {rep ? `${rep.checked} holidays and sales periods are checked automatically in this product's sales history. Only clear effects move the forecast.`
                 : "Holidays are checked once this product has sales history."}
          </p>
          {(err || refitting || busy === "store") && (
            <div className="rounded-xl px-4 py-2.5 mb-5 text-[14px] border"
              style={err ? { background: "var(--t-bad-soft)", borderColor: "var(--t-bad-line)", color: "var(--t-bad)" }
                         : { background: "var(--t-sunken)", borderColor: "var(--t-line)", color: "var(--t-soft)" }}>
              {err || "Updating every product's forecast. This takes a minute or two."}
            </div>
          )}

          <HSection title={`Moves this product · ${applied.length}`}>
            {applied.length === 0 ? (
              <div className="rounded-xl p-4 text-[14.5px]" style={{ ...card, color: "var(--t-soft)" }}>
                No holiday shows a clear effect yet. Its dates are forecast like any other day.
              </div>
            ) : (
              <div className="rounded-xl overflow-hidden" style={card}>
                {applied.map((r, i) => (
                  <div key={r.key} className="px-4 py-3" style={{ borderTop: i ? "1px solid var(--t-line)" : "none" }}>
                    <div className="flex items-baseline gap-2">
                      <span className="text-[15.5px] font-semibold">{r.name}</span>
                      <span className="ml-auto font-mono text-[16px] font-semibold"
                        style={{ color: r.pct >= 0 ? "var(--t-good)" : "var(--t-warn)" }}>{signed(r.pct)}</span>
                    </div>
                    <div className="text-[13.5px] mt-0.5 leading-relaxed" style={muted}>
                      {r.text}{r.next ? ` Next: ${fmtRange(r.next.start, r.next.end)}.` : ""}
                    </div>
                    <HYears r={r} />
                    {(r.days || []).length > 1 && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {r.days.map(d => (
                          <span key={d.date} className="text-[12.5px] px-1.5 py-0.5 rounded"
                            style={{ background: "var(--t-good-soft)", color: "var(--t-good)" }}>
                            {wd(d.date)} {fmtD(d.date)} <span className="font-mono">{signed(d.pct)}</span>
                          </span>
                        ))}
                      </div>
                    )}
                    <button className={`${linkBtn} mt-2`} style={muted} disabled={!!busy}
                      onClick={() => setSwitch(r.key, false)}>
                      {busy === r.key ? "Switching off…" : "Switch off for this product"}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </HSection>

          {quiet.length > 0 && (
            <HSection title={`Checked, no clear effect · ${quiet.length}`}
              right={<button className={linkBtn} style={muted} onClick={() => setShowQuiet(v => !v)}>{showQuiet ? "Hide" : "Show"}</button>}>
              {showQuiet ? (<>
                <p className="text-[13.5px] leading-relaxed mb-2.5" style={muted}>
                  On these dates its past years stay within what chance explains on this many sales, so they're
                  forecast as ordinary days. Any sizeable past swing is covered by extra safety stock instead.
                </p>
                <div className="rounded-xl overflow-hidden" style={card}>
                  {quiet.map((r, i) => (
                    <div key={r.key} className="px-4 py-2.5" style={{ borderTop: i ? "1px solid var(--t-line)" : "none" }}>
                      <div className="flex items-baseline gap-2">
                        <span className="text-[14.5px] font-semibold">{r.name}</span>
                        <button className={`${linkBtn} ml-auto`} style={muted} disabled={!!busy}
                          onClick={() => setSwitch(r.key, false)}>{busy === r.key ? "…" : "Switch off"}</button>
                      </div>
                      {r.status === "discounted" && (
                        <div className="text-[13px] mt-0.5 leading-relaxed" style={muted}>{r.text}</div>
                      )}
                      <HYears r={r} />
                    </div>
                  ))}
                </div>
              </>) : (
                <div className="text-[14px] leading-relaxed" style={{ color: "var(--t-soft)" }}>
                  {quiet.map(r => r.name).join(", ")}.
                </div>
              )}
            </HSection>
          )}

          {off.length > 0 && (
            <HSection title={`Switched off · ${off.length}`}>
              <div className="rounded-xl overflow-hidden" style={card}>
                {off.map((r, i) => (
                  <div key={r.key} className="flex items-center px-4 py-2.5" style={{ borderTop: i ? "1px solid var(--t-line)" : "none" }}>
                    <span className="text-[14.5px]">{r.name}</span>
                    <button className={`${linkBtn} ml-auto`} style={{ color: "var(--t-accent)" }} disabled={!!busy}
                      onClick={() => setSwitch(r.key, true)}>{busy === r.key ? "…" : "Switch back on"}</button>
                  </div>
                ))}
              </div>
            </HSection>
          )}

          {primeRow && (
            <HSection title="Prime Day dates">
              <div className="text-[13.5px] leading-relaxed mb-2.5" style={{ color: primeRow.needsDate ? "var(--t-warn)" : "var(--t-soft)" }}>
                {primeRow.needsDate
                  ? "The next Prime Day hasn't been announced yet, so it isn't in the forecast. Add it when Amazon does."
                  : `Next: ${fmtRange(primeRow.next.start, primeRow.next.end)}.`}
              </div>
              <div className="grid gap-2" style={{ gridTemplateColumns: "1fr 1fr auto" }}>
                <input type="date" value={prime.start} onChange={e => setPrime(p => ({ ...p, start: e.target.value }))}
                  className="rounded-lg px-3 py-2 text-[14px] outline-none" style={inp} />
                <input type="date" value={prime.end} onChange={e => setPrime(p => ({ ...p, end: e.target.value }))}
                  className="rounded-lg px-3 py-2 text-[14px] outline-none" style={inp} />
                <button onClick={addPrime} disabled={!!busy || refitting}
                  className="rounded-lg px-3 py-2 text-[14px] font-semibold border disabled:opacity-60"
                  style={{ borderColor: "var(--t-line2)", color: "var(--t-soft)" }}>Add</button>
              </div>
            </HSection>
          )}

          {(rep?.notes || []).map((n, i) => (
            <p key={i} className="text-[13px] leading-relaxed mt-2" style={muted}>{n}</p>
          ))}
        </div>
      </aside>
    </div>
  );
}

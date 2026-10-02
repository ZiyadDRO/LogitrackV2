import React, { useEffect, useMemo, useState } from 'react';
import { terminal, fs, MONO, SANS, scrim } from '../lib/theme';
import { fetchJson } from '../lib/api';
import { todayStr } from '../lib/helpers';

/* CLOSED DAYS — the days the store is shut.
 *
 * A closed day isn't a bad sales day, it's a day that didn't happen. Marked here, it's
 * taken out of everything the forecast learns from (this works backwards: marking last
 * Christmas refits every product without it) and forecast at zero when it comes round.
 *
 * Click a day to mark it; shift-click marks every day since the last one clicked. Days
 * the whole store sold nothing are outlined as suggestions, never marked on their own:
 * a quiet day is not assumed to be a closed one. Holidays the store always closes on can
 * be switched on once for every year. */

const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parse = s => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const monthStart = d => new Date(d.getFullYear(), d.getMonth(), 1);
const addMonths = (d, n) => new Date(d.getFullYear(), d.getMonth() + n, 1);
const longDate = s => parse(s).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
const monthName = d => d.toLocaleDateString("en-US", { month: "long", year: "numeric" });

/* The store's time zone decides which day every sale, stock reading and "today" belongs
   to (see engine_statsforecast/store_clock.py). US zones first; stored as IANA names so
   daylight saving is handled (Eastern is EST in winter and EDT in summer). */
const ZONES = [
  ["America/New_York", "Eastern Time (ET)"],
  ["America/Chicago", "Central Time (CT)"],
  ["America/Denver", "Mountain Time (MT)"],
  ["America/Phoenix", "Arizona (MST, no daylight saving)"],
  ["America/Los_Angeles", "Pacific Time (PT)"],
  ["America/Anchorage", "Alaska Time (AKT)"],
  ["Pacific/Honolulu", "Hawaii Time (HT)"],
  ["America/Puerto_Rico", "Atlantic Time (Puerto Rico)"],
];
const zoneLabel = z => (ZONES.find(([k]) => k === z) || [null, z])[1];
const SOURCE_TEXT = { square: "from your Square location", shopify: "from your Shopify store",
                      store: "from your store", default: "default" };

export function ClosedDaysButton({ onClick, count = 0, lm = false }) {
  const T = terminal(lm);
  return (
    <button onClick={onClick}
      title="Days the store is closed. They're left out of what the forecast learns from and forecast at zero."
      style={{ display: "inline-flex", alignItems: "center", gap: 8, fontFamily: SANS, fontSize: 15,
        fontWeight: 500, padding: "7px 12px 7px 10px", borderRadius: 0, cursor: "pointer",
        background: T.sunken, color: T.ink, border: `2px dashed ${T.line2}`, whiteSpace: "nowrap" }}>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke={T.amber} strokeWidth="2"
        strokeLinecap="square" aria-hidden="true">
        <rect x="3" y="5" width="18" height="16" />
        <path d="M3 10h18M8 3v4M16 3v4M9 14l6 5M15 14l-6 5" />
      </svg>
      Closed days
      {count > 0 && (
        <span style={{ fontFamily: MONO, fontSize: 13, fontWeight: 600, padding: "1px 6px",
          background: T.amber, color: T.onFill }}>{count}</span>
      )}
    </button>
  );
}

export function ClosedDaysSheet({ open, onClose, api, lm = false, onChanged }) {
  const T = terminal(lm);
  const mono = { fontFamily: MONO, fontVariantNumeric: "tabular-nums" };
  const cap = { fontSize: fs.cap, letterSpacing: ".07em", textTransform: "uppercase", color: T.dim, fontWeight: 600 };

  const [data, setData] = useState(null);
  const [dates, setDates] = useState(new Set());
  const [yearly, setYearly] = useState(new Set());
  const [month, setMonth] = useState(() => addMonths(monthStart(new Date()), -1));
  const [anchor, setAnchor] = useState(null);
  const [status, setStatus] = useState(null);   // null | "saving" | "refitting" | "done"
  const [err, setErr] = useState(null);
  const [showAllSugg, setShowAllSugg] = useState(false);
  // "" = automatic (the store's own zone, else Eastern); otherwise the zone picked here.
  const [tz, setTz] = useState("");

  const load = () => fetchJson(`${api}/api/closed-days`, undefined, "Closed days").then(d => {
    if (d) {
      setData(d);
      setDates(new Set(d.settings.dates));
      setYearly(new Set(d.settings.yearly));
      setTz(d.timezone?.source === "set" ? d.timezone.zone : "");
    }
    return d;
  });
  useEffect(() => {
    if (!open) return;
    setErr(null); setStatus(null); setAnchor(null); setShowAllSugg(false);
    setMonth(addMonths(monthStart(new Date()), -1));
    load();
  }, [open]);   // eslint-disable-line react-hooks/exhaustive-deps

  const yearlyDay = useMemo(() => {
    const m = {};
    for (const o of data?.yearlyOptions || []) {
      if (!yearly.has(o.key)) continue;
      for (const d of data.yearlyDates?.[o.key] || []) m[d] = o.name;
    }
    return m;
  }, [data, yearly]);
  const sugg = useMemo(() => new Set((data?.suggestions || []).filter(d => !dates.has(d) && !yearlyDay[d])),
    [data, dates, yearlyDay]);

  if (!open) return null;

  const todayIso = data?.timezone?.today || todayStr();   // the store's today
  const tzSaved = data?.timezone?.source === "set" ? data.timezone.zone : "";
  const tzDirty = !!data && tz !== tzSaved;
  const autoZone = data?.timezone?.detected || data?.timezone?.default || "America/New_York";
  const autoSource = data?.timezone?.detected ? (SOURCE_TEXT[data.timezone.detectedFrom] || SOURCE_TEXT.store) : SOURCE_TEXT.default;
  const dirty = data && (JSON.stringify([...dates].sort()) !== JSON.stringify(data.settings.dates)
    || JSON.stringify((data.yearlyOptions || []).map(o => o.key).filter(k => yearly.has(k)))
       !== JSON.stringify(data.settings.yearly)
    || tzDirty);
  const busy = status === "saving" || status === "refitting";

  const toggle = (d, shift) => {
    if (busy || yearlyDay[d]) return;
    setStatus(null);
    setDates(prev => {
      const next = new Set(prev);
      if (shift && anchor && anchor !== d) {
        // Every day between the last one clicked and this one takes this one's new state.
        const on = !prev.has(d);
        let a = parse(anchor), b = parse(d);
        if (a > b) [a, b] = [b, a];
        for (let x = new Date(a); x <= b; x.setDate(x.getDate() + 1)) {
          const k = iso(x);
          if (yearlyDay[k]) continue;
          if (on) next.add(k); else next.delete(k);
        }
      } else if (next.has(d)) next.delete(d);
      else next.add(d);
      return next;
    });
    setAnchor(d);
  };
  const markAllSuggested = () => { setStatus(null); setDates(prev => new Set([...prev, ...sugg])); };
  const setYear = (k, on) => { setStatus(null); setYearly(prev => { const n = new Set(prev); on ? n.add(k) : n.delete(k); return n; }); };

  const save = async () => {
    setErr(null); setStatus("saving");
    const body = { dates: [...dates].sort(), yearly: (data.yearlyOptions || []).map(o => o.key).filter(k => yearly.has(k)),
                   ...(tzDirty ? { timezone: tz || null } : {}) };
    const res = await fetchJson(`${api}/api/closed-days`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }, "Saving closed days");
    if (!res) { setStatus(null); setErr("Couldn't save. Is the backend running?"); return; }
    setData(res);
    setTz(res.timezone?.source === "set" ? res.timezone.zone : "");
    if (res.refitting) {
      setStatus("refitting");
      let d = res;
      while (d?.refit?.status === "running") {
        await new Promise(r => setTimeout(r, 3000));
        d = await fetchJson(`${api}/api/closed-days`, undefined, "Closed days");
      }
      if (d) setData(d);
      if (d?.refit?.status === "error") { setStatus(null); setErr(`Saved, but updating the forecasts failed: ${d.refit.error}`); return; }
    }
    setStatus("done");
    onChanged && onChanged();
  };

  /* ── one month ── */
  const Month = ({ start }) => {
    const first = start.getDay();
    const n = new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate();
    const cells = [];
    for (let i = 0; i < first; i++) cells.push(null);
    for (let d = 1; d <= n; d++) cells.push(iso(new Date(start.getFullYear(), start.getMonth(), d)));
    const hFirst = data?.history?.first, hLast = data?.history?.last;
    return (
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: fs.row, fontWeight: 600, marginBottom: 8 }}>{monthName(start)}</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(7, minmax(0,1fr))", gap: 3 }}>
          {DOW.map(w => <div key={w} style={{ ...cap, fontSize: 12.5, textAlign: "center", padding: "2px 0" }}>{w.slice(0, 2)}</div>)}
          {cells.map((d, i) => {
            if (!d) return <div key={`b${i}`} />;
            const closed = dates.has(d), every = yearlyDay[d], isSugg = sugg.has(d);
            const outside = hFirst && hLast && d < hFirst && d < todayIso;
            const isToday = d === todayIso;
            const fill = closed || every;
            return (
              <button key={d} onClick={e => toggle(d, e.shiftKey)} disabled={busy}
                title={every ? `Closed every year: ${every}` : closed ? `${longDate(d)}: closed (click to reopen)`
                  : isSugg ? `${longDate(d)}: nothing sold anywhere in the store. Closed?`
                  : `${longDate(d)}: click to mark closed`}
                style={{ ...mono, height: 38, fontSize: 14.5, fontWeight: fill ? 700 : 500, borderRadius: 0,
                  cursor: every || busy ? "default" : "pointer", position: "relative",
                  color: fill ? T.onFill : outside ? T.faint : T.ink,
                  background: every
                    ? `repeating-linear-gradient(135deg, ${T.amber} 0 6px, ${T.over} 6px 9px)`
                    : closed ? T.amber : isSugg ? T.amberSoft : "transparent",
                  border: isSugg && !fill ? `2px dashed ${T.amber}`
                    : isToday ? `2px solid ${T.ink}` : `2px solid ${fill ? "transparent" : T.line}` }}>
                {Number(d.slice(8))}
              </button>
            );
          })}
        </div>
      </div>
    );
  };

  const suggList = [...sugg].sort();
  const shownSugg = showAllSugg ? suggList : suggList.slice(-10);
  const hints = (data?.yearlyHints || []).filter(k => !yearly.has(k));
  const nameOf = k => (data?.yearlyOptions || []).find(o => o.key === k)?.name || k;
  const navBtn = { ...mono, fontSize: 15, padding: "5px 11px", borderRadius: 0, cursor: "pointer",
    background: "transparent", color: T.soft, border: `2px solid ${T.line2}` };

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 60, fontFamily: SANS }}>
      <div onClick={busy ? undefined : onClose} style={scrim(lm)} />
      <div style={{ position: "absolute", top: "4vh", left: "50%", transform: "translateX(-50%)",
        width: "min(900px, 95vw)", maxHeight: "92vh", display: "flex", flexDirection: "column",
        background: T.bg, color: T.ink, border: `2px solid ${T.line2}`, boxShadow: "0 24px 60px rgba(0,0,0,.5)" }}>

        {/* header */}
        <div style={{ padding: "18px 20px 14px", borderBottom: `2px solid ${T.line}` }}>
          <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16 }}>
            <div>
              <div style={{ fontSize: 22, fontWeight: 700, letterSpacing: "-.02em" }}>Closed days</div>
              <div style={{ fontSize: fs.body, color: T.soft, marginTop: 5, maxWidth: 640, lineHeight: 1.6 }}>
                Days the store is shut. They're taken out of everything the forecast learns from, past ones
                included, and forecast at zero when they come round. Click a day to mark it; shift-click to
                mark a run of days.
              </div>
            </div>
            <button onClick={onClose} disabled={busy} style={{ ...mono, background: "transparent", border: "none",
              color: T.soft, fontSize: 20, cursor: busy ? "default" : "pointer", padding: "0 4px", lineHeight: 1 }}>×</button>
          </div>
        </div>

        <div style={{ overflowY: "auto", padding: "16px 20px 6px" }}>
          {!data ? <div style={{ color: T.soft, fontSize: fs.body }}>Loading…</div> : (<>

          {/* the store's time zone */}
          <div style={{ ...cap, marginBottom: 8 }}>Store time zone</div>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, marginBottom: 6 }}>
            <select value={tz} disabled={busy} onChange={e => { setStatus(null); setTz(e.target.value); }}
              style={{ fontFamily: SANS, fontSize: fs.body, padding: "6px 8px", borderRadius: 0,
                background: T.panel, color: T.ink, border: `2px solid ${T.line2}` }}>
              <option value="">Automatic: {zoneLabel(autoZone)} ({autoSource})</option>
              {ZONES.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              {tz && !ZONES.some(([k]) => k === tz) && <option value={tz}>{tz}</option>}
            </select>
          </div>
          <div style={{ fontSize: fs.body, color: T.soft, marginBottom: 16, lineHeight: 1.6 }}>
            Decides which day each sale, stock reading and order belongs to, and when "today" starts.
            {data.timezone?.source === "set" ? ` Set by you: ${zoneLabel(data.timezone.zone)}.` : ""}
            {tzDirty ? " Saving updates every forecast." : ""}
          </div>

          {/* every year */}
          <div style={{ ...cap, marginBottom: 8 }}>Closed every year</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 16 }}>
            {(data.yearlyOptions || []).map(o => {
              const on = yearly.has(o.key);
              return (
                <button key={o.key} onClick={() => setYear(o.key, !on)} disabled={busy}
                  title={o.next ? `Next: ${longDate(o.next)}` : undefined}
                  style={{ fontFamily: SANS, fontSize: fs.body, padding: "6px 11px", borderRadius: 0, cursor: "pointer",
                    background: on ? T.amber : "transparent", color: on ? T.onFill : T.soft,
                    border: `2px solid ${on ? T.amber : T.line2}`, fontWeight: on ? 600 : 500 }}>
                  {on ? "✓ " : ""}{o.name}
                </button>
              );
            })}
          </div>

          {/* suggestions */}
          {(suggList.length > 0 || hints.length > 0) && (
            <div style={{ border: `2px dashed ${T.amber}`, background: T.amberSoft, padding: "12px 14px", marginBottom: 16 }}>
              <div style={{ fontSize: fs.body, color: T.ink, lineHeight: 1.55 }}>
                {suggList.length > 0 && (<>
                  <b>{suggList.length} day{suggList.length === 1 ? "" : "s"}</b> in your history had no sales anywhere in the
                  store, so the store was probably closed. They're outlined on the calendar. Nothing is marked until you say so.
                </>)}
              </div>
              {hints.length > 0 && (
                <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, marginTop: 10 }}>
                  <span style={{ fontSize: fs.small, color: T.soft }}>Closed on these in every year of your history:</span>
                  {hints.map(k => (
                    <button key={k} onClick={() => setYear(k, true)} disabled={busy}
                      style={{ fontFamily: SANS, fontSize: fs.small, padding: "4px 9px", borderRadius: 0, cursor: "pointer",
                        background: T.bg, color: T.ink, border: `2px solid ${T.amber}` }}>
                      + {nameOf(k)}, every year
                    </button>
                  ))}
                </div>
              )}
              {suggList.length > 0 && (
                <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, marginTop: 10 }}>
                  {shownSugg.map(d => (
                    <button key={d} onClick={() => setMonth(addMonths(monthStart(parse(d)), 0))}
                      title="Show this month"
                      style={{ ...mono, fontSize: 13, padding: "3px 7px", borderRadius: 0, cursor: "pointer",
                        background: "transparent", color: T.soft, border: `1px solid ${T.line2}` }}>
                      {longDate(d)}
                    </button>
                  ))}
                  {suggList.length > shownSugg.length && (
                    <button onClick={() => setShowAllSugg(true)} style={{ fontSize: 13, background: "none", border: "none",
                      color: T.soft, textDecoration: "underline", cursor: "pointer" }}>
                      +{suggList.length - shownSugg.length} earlier
                    </button>
                  )}
                  <button onClick={markAllSuggested} disabled={busy}
                    style={{ marginLeft: "auto", fontFamily: SANS, fontSize: fs.small, fontWeight: 600, padding: "5px 11px",
                      borderRadius: 0, cursor: "pointer", background: T.btnBg, color: T.btnFg, border: "2px solid transparent" }}>
                    Mark all {suggList.length} closed
                  </button>
                </div>
              )}
            </div>
          )}

          {/* calendar */}
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
            <button style={navBtn} onClick={() => setMonth(m => addMonths(m, -12))} title="Back a year">«</button>
            <button style={navBtn} onClick={() => setMonth(m => addMonths(m, -1))} title="Back a month">‹</button>
            <button style={navBtn} onClick={() => setMonth(addMonths(monthStart(new Date()), -1))}>Today</button>
            <button style={navBtn} onClick={() => setMonth(m => addMonths(m, 1))} title="Forward a month">›</button>
            <button style={navBtn} onClick={() => setMonth(m => addMonths(m, 12))} title="Forward a year">»</button>
            <div style={{ marginLeft: "auto", display: "flex", gap: 14, fontSize: fs.small, color: T.soft, flexWrap: "wrap" }}>
              <span><i style={{ display: "inline-block", width: 12, height: 12, background: T.amber, verticalAlign: -1, marginRight: 5 }} />Closed</span>
              <span><i style={{ display: "inline-block", width: 12, height: 12, verticalAlign: -1, marginRight: 5,
                background: `repeating-linear-gradient(135deg, ${T.amber} 0 4px, ${T.over} 4px 6px)` }} />Every year</span>
              <span><i style={{ display: "inline-block", width: 12, height: 12, verticalAlign: -1, marginRight: 5,
                border: `2px dashed ${T.amber}`, boxSizing: "border-box" }} />No sales anywhere</span>
            </div>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0,1fr))", gap: 22, marginBottom: 12 }}>
            <Month start={month} />
            <Month start={addMonths(month, 1)} />
          </div>
          </>)}
        </div>

        {/* footer */}
        <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "12px 20px",
          borderTop: `2px solid ${T.line}`, background: T.sunken, flexWrap: "wrap" }}>
          <div style={{ fontSize: fs.body, color: err ? T.red : T.soft, minWidth: 0, flex: 1 }}>
            {err ? err
              : status === "saving" ? "Saving…"
              : status === "refitting" ? "Updating every product's forecast. This can take a few minutes."
              : status === "done" ? "Saved. Every forecast is up to date."
              : `${dates.size} day${dates.size === 1 ? "" : "s"} marked`
                + (yearly.size ? ` · closed every year on ${[...(data?.yearlyOptions || [])].filter(o => yearly.has(o.key)).map(o => o.name).join(", ")}` : "")
                + (dirty ? " · not saved yet" : "")}
          </div>
          <button onClick={onClose} disabled={busy}
            style={{ fontFamily: SANS, fontSize: fs.body, padding: "8px 14px", borderRadius: 0, cursor: busy ? "default" : "pointer",
              background: "transparent", color: T.soft, border: `2px solid ${T.line2}` }}>
            {status === "done" || !dirty ? "Close" : "Cancel"}
          </button>
          <button onClick={save} disabled={!dirty || busy}
            style={{ fontFamily: SANS, fontSize: fs.body, fontWeight: 600, padding: "8px 14px", borderRadius: 0,
              cursor: !dirty || busy ? "default" : "pointer", opacity: !dirty || busy ? 0.55 : 1,
              background: T.btnBg, color: T.btnFg, border: "2px solid transparent" }}>
            {busy ? "Updating…" : "Save & update forecasts"}
          </button>
        </div>
      </div>
    </div>
  );
}

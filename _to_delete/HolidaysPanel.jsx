import React, { useState, useEffect, useRef, useMemo } from 'react';
import { terminal, MONO, SANS } from '../lib/theme';
import { fetchJson } from '../lib/api';

/**
 * Holidays: which promotional periods this store cares about.
 *
 * The calendar only sets the DATES. How much each holiday moves a product is learned
 * from sales history (its own past years first, similar products when it has none), so
 * ticking a holiday that doesn't matter to a product simply learns "no effect" for it.
 * The store type is a starting preset, not a rule: every tick stays editable.
 *
 * Saving refits every product in the background. The page polls until it's done, then
 * tells the app so the fleet, product pages and backtest pick up the new forecasts.
 */

const fmtDay = (iso) => {
  const d = new Date(`${iso}T12:00:00Z`);
  return Number.isNaN(d.getTime()) ? iso
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
};
const fmtShort = (iso) => {
  const d = new Date(`${iso}T12:00:00Z`);
  return Number.isNaN(d.getTime()) ? iso
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
};
const fmtRange = (a, b) => (a === b ? fmtDay(a) : `${fmtShort(a)} to ${fmtDay(b)}`);
const sameSet = (a, b) => a.length === b.length && a.every(x => b.includes(x));

export default function HolidaysPanel({ api, lm = false, onSaved = () => {} }) {
  const T = terminal(lm);
  const mono = { fontFamily: MONO, fontVariantNumeric: "tabular-nums" };
  const cap = { fontSize: 13, letterSpacing: ".07em", textTransform: "uppercase", color: T.dim, fontWeight: 600 };
  const panel = { background: T.panel, border: `2px solid ${T.line}`, boxShadow: T.shadow };
  const btnGhost = { ...mono, fontSize: 15, fontWeight: 500, padding: "7px 12px", borderRadius: 2,
    background: "transparent", color: T.soft, border: `2px solid ${T.line2}`, whiteSpace: "nowrap", cursor: "pointer" };
  const btnSolid = { ...btnGhost, background: T.btnBg, color: T.btnFg, border: "2px solid transparent", fontWeight: 600 };
  const btnMini = { ...mono, fontSize: 14, fontWeight: 600, padding: "4px 8px", borderRadius: 2,
    background: "transparent", color: T.soft, border: `2px solid ${T.line2}`, cursor: "pointer" };
  const inp = { ...mono, background: T.bg, border: `2px solid ${T.line2}`, borderRadius: 2,
    padding: "6px 8px", fontSize: 15, color: T.ink, outline: "none", boxSizing: "border-box" };

  const [data, setData] = useState(null);          // last GET
  const [draft, setDraft] = useState(null);        // what's on screen
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);
  const [openDates, setOpenDates] = useState(null); // table holiday whose dates are expanded
  const [newDate, setNewDate] = useState({ start: "", end: "" });
  const [custom, setCustom] = useState({ name: "", start: "", end: "", yearly: true });
  const poll = useRef(null);
  const wasRunning = useRef(false);

  const load = async () => {
    const d = await fetchJson(`${api}/api/holidays`, undefined, "Loading holidays");
    if (!d) return null;
    setData(d);
    return d;
  };

  useEffect(() => {
    load().then(d => { if (d) setDraft(JSON.parse(JSON.stringify(d.settings))); });
    return () => clearInterval(poll.current);
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  // Poll while a refit runs; when it finishes, let the app refresh everything.
  const watch = () => {
    clearInterval(poll.current);
    wasRunning.current = true;
    poll.current = setInterval(async () => {
      const d = await load();
      if (!d || d.refit?.status === "running") return;
      clearInterval(poll.current);
      if (wasRunning.current) { wasRunning.current = false; onSaved(d.refit?.status === "done"); }
    }, 3000);
  };
  useEffect(() => { if (data?.refit?.status === "running" && !poll.current) watch(); }, [data]);   // eslint-disable-line react-hooks/exhaustive-deps

  const saved = data?.settings;
  const dirty = useMemo(() => !!(draft && saved) && JSON.stringify(draft) !== JSON.stringify(saved), [draft, saved]);

  if (!data || !draft) {
    return <div style={{ background: T.bg, color: T.dim, padding: 24, fontFamily: SANS, flex: 1 }}>Loading holidays…</div>;
  }

  const running = data.refit?.status === "running";
  const selected = draft.selected || [];
  const presetOf = (sel) => Object.entries(data.storeTypes).find(([, v]) => sameSet(v.keys, sel))?.[0] || null;
  const shownType = draft.storeType && draft.storeType !== "custom" && sameSet(data.storeTypes[draft.storeType]?.keys || [], selected)
    ? draft.storeType : presetOf(selected);

  const setSelected = (keys) => setDraft(d => ({ ...d, selected: keys, storeType: presetOf(keys) || (keys.length ? "custom" : d.storeType) }));
  const toggle = (k) => setSelected(selected.includes(k) ? selected.filter(x => x !== k) : [...selected, k]);
  const pickType = (t) => setDraft(d => ({ ...d, storeType: t, selected: [...data.storeTypes[t].keys] }));

  const save = async () => {
    setSaving(true); setErr(null);
    const res = await fetchJson(`${api}/api/holidays`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(draft),
    }, "Saving holidays");
    setSaving(false);
    if (!res) { setErr("Not saved. Check the message at the bottom of the screen."); return; }
    setDraft(JSON.parse(JSON.stringify(res.settings)));
    const d = await load();
    if (d) setDraft(JSON.parse(JSON.stringify(d.settings)));
    if (res.refitting) watch();
  };

  const addTableDate = (key) => {
    if (!newDate.start) return;
    const end = newDate.end || newDate.start;
    if (end < newDate.start) { setErr("The end date is before the start date."); return; }
    setErr(null);
    setDraft(d => ({ ...d, dates: { ...(d.dates || {}), [key]: [...((d.dates || {})[key] || []), [newDate.start, end]] } }));
    setNewDate({ start: "", end: "" });
  };
  const removeTableDate = (key, i) => setDraft(d => {
    const rows = [...((d.dates || {})[key] || [])]; rows.splice(i, 1);
    const dates = { ...(d.dates || {}) };
    if (rows.length) dates[key] = rows; else delete dates[key];
    return { ...d, dates };
  });

  const addCustom = () => {
    if (!custom.name.trim() || !custom.start) { setErr("A custom period needs a name and a start date."); return; }
    const end = custom.end || custom.start;
    if (end < custom.start) { setErr("The end date is before the start date."); return; }
    setErr(null);
    const existing = (draft.custom || []).find(c => c.name.trim().toLowerCase() === custom.name.trim().toLowerCase());
    if (existing) {
      setDraft(d => ({ ...d, custom: d.custom.map(c => c === existing ? { ...c, ranges: [...c.ranges, [custom.start, end]] } : c) }));
    } else {
      setDraft(d => ({ ...d, custom: [...(d.custom || []), { name: custom.name.trim(), ranges: [[custom.start, end]], yearly: custom.yearly, enabled: true }] }));
    }
    setCustom({ name: "", start: "", end: "", yearly: custom.yearly });
  };
  const updCustom = (i, patch) => setDraft(d => ({ ...d, custom: d.custom.map((c, j) => j === i ? { ...c, ...patch } : c) }));
  const delCustom = (i) => setDraft(d => ({ ...d, custom: d.custom.filter((_, j) => j !== i) }));

  const isSaved = (key) => !!saved && ((saved.selected || []).includes(key)
    || (saved.custom || []).some(c => c.id === key && c.enabled !== false));
  const learnedChip = (key, isOn) => {
    const l = data.learned?.[key];
    const pill = (text, color, bg) => (
      <span style={{ ...mono, fontSize: 13.5, fontWeight: 600, padding: "2px 7px", borderRadius: 2, color, background: bg, whiteSpace: "nowrap" }}>{text}</span>
    );
    if (!isOn) return null;
    if (!isSaved(key)) return pill("Save to learn", T.dim, T.sunken);
    if (!l) return pill("Not learned yet", T.dim, T.sunken);
    if (!l.products) return pill("No clear effect", T.dim, T.sunken);
    const t = l.typicalPct;
    const tone = t >= 0 ? [T.greenFg, T.greenBg] : [T.overFg, T.overBg];
    return pill(`${t > 0 ? "+" : ""}${t}% typical`, ...tone);
  };
  const learnedText = (key) => {
    const l = data.learned?.[key];
    if (!l || !isSaved(key)) return null;
    if (!l.products) return l.skipped
      ? "Past ones were always discounted, so the lift can't be told apart from the deal yet."
      : "No product shows a clear change on these dates.";
    const range = l.lowPct !== l.highPct ? ` (${l.lowPct > 0 ? "+" : ""}${l.lowPct}% to ${l.highPct > 0 ? "+" : ""}${l.highPct}%)` : "";
    const src = l.peers ? `, ${l.peers} borrowed from similar products` : "";
    return `Moves ${l.products} of ${data.products} products${range}${src}.`;
  };

  const card = (active) => ({ ...panel, padding: "14px 16px", cursor: "pointer", textAlign: "left", flex: "1 1 200px",
    borderColor: active ? T.amber : T.line, background: active ? T.amberSoft : T.panel, color: T.ink, fontFamily: SANS });

  return (
    <div style={{ background: T.bg, color: T.ink, minHeight: "100%", padding: "20px 24px 36px", fontFamily: SANS, flex: 1, overflowY: "auto" }}>
      <style>{`.hol-row:hover{background:${T.sunken};}`}</style>

      {/* ── header ─────────────────────────────────────────────────────── */}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16, marginBottom: 14, flexWrap: "wrap" }}>
        <div style={{ maxWidth: 720 }}>
          <div style={{ fontSize: 28, fontWeight: 700, letterSpacing: "-.03em" }}>Holidays</div>
          <div style={{ fontSize: 15.5, color: T.soft, marginTop: 4, lineHeight: 1.5 }}>
            Pick the periods that matter to your store. The calendar sets the dates, and how much each one moves sales is learned from your history.
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
          {dirty && !saving && (
            <button style={btnGhost} onClick={() => { setDraft(JSON.parse(JSON.stringify(saved))); setErr(null); }}>Undo changes</button>
          )}
          <button style={{ ...btnSolid, opacity: (!dirty || saving || running) ? .5 : 1, cursor: (!dirty || saving || running) ? "default" : "pointer" }}
            disabled={!dirty || saving || running} onClick={save}>
            {saving ? "Saving…" : "Save and update forecasts"}
          </button>
        </div>
      </div>

      {/* ── status ─────────────────────────────────────────────────────── */}
      {(running || err || data.refit?.status === "error" || (!data.configured && !dirty)) && (
        <div style={{ ...panel, padding: "10px 14px", marginBottom: 16, fontSize: 15, lineHeight: 1.5,
          borderColor: err || data.refit?.status === "error" ? T.red : running ? T.blue : T.amber,
          color: err || data.refit?.status === "error" ? T.redFg : T.soft }}>
          {err ? err
            : data.refit?.status === "error" ? `Forecasts couldn't be updated: ${data.refit.error}`
            : running ? "Updating every forecast with your holidays. This takes a minute or two, and the rest of the app keeps working."
            : "No holidays set up yet, so none are counted. Pick your store type to start."}
        </div>
      )}
      {!running && !err && data.refit?.status === "done" && !dirty && data.refit?.finishedAt && (Date.now() / 1000 - data.refit.finishedAt) < 120 && (
        <div style={{ ...panel, padding: "10px 14px", marginBottom: 16, fontSize: 15, borderColor: T.green, color: T.greenFg }}>
          Forecasts updated.
        </div>
      )}

      {/* ── 1. store type ──────────────────────────────────────────────── */}
      <div style={{ ...cap, marginBottom: 8 }}>1 · Your store</div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 22 }}>
        {Object.entries(data.storeTypes).map(([k, v]) => (
          <button key={k} style={card(shownType === k)} onClick={() => pickType(k)}>
            <div style={{ fontSize: 16.5, fontWeight: 650 }}>{v.name}</div>
            <div style={{ fontSize: 14, color: T.dim, marginTop: 3 }}>{v.keys.length} holidays ticked to start</div>
          </button>
        ))}
        <div style={{ ...panel, padding: "14px 16px", flex: "1 1 200px", borderStyle: "dashed", color: T.dim, fontSize: 14, lineHeight: 1.5 }}>
          {shownType ? "Change any tick below to make it your own." : selected.length ? `Your own mix: ${selected.length} ticked.` : "Nothing ticked yet."}
        </div>
      </div>

      {/* ── 2. the list ────────────────────────────────────────────────── */}
      <div style={{ ...cap, marginBottom: 8 }}>2 · Holidays to count</div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))", gap: 14, marginBottom: 22 }}>
        {data.groups.map(g => {
          const rows = data.catalog.filter(r => r.group === g);
          const allOn = rows.every(r => selected.includes(r.key));
          return (
            <div key={g} style={{ ...panel }}>
              <div style={{ display: "flex", alignItems: "center", padding: "10px 14px", borderBottom: `2px solid ${T.line}`, background: T.sunken }}>
                <div style={{ fontSize: 16, fontWeight: 650 }}>{g}</div>
                <button style={{ ...btnMini, marginLeft: "auto" }}
                  onClick={() => setSelected(allOn ? selected.filter(k => !rows.some(r => r.key === k))
                    : [...selected, ...rows.map(r => r.key).filter(k => !selected.includes(k))])}>
                  {allOn ? "Untick all" : "Tick all"}
                </button>
              </div>
              {rows.map(r => {
                const on = selected.includes(r.key);
                const extra = (draft.dates || {})[r.key] || [];
                const txt = learnedText(r.key);
                return (
                  <div key={r.key} className="hol-row" style={{ padding: "10px 14px", borderTop: `1px solid ${T.line}` }}>
                    <label style={{ display: "flex", alignItems: "flex-start", gap: 10, cursor: "pointer" }}>
                      <input type="checkbox" checked={on} onChange={() => toggle(r.key)}
                        style={{ width: 17, height: 17, marginTop: 3, accentColor: T.amber, cursor: "pointer" }} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                          <span style={{ fontSize: 15.5, fontWeight: 600, color: on ? T.ink : T.soft }}>{r.name}</span>
                          {learnedChip(r.key, on)}
                        </div>
                        <div style={{ fontSize: 14, color: T.dim, marginTop: 2, lineHeight: 1.45 }}>
                          {r.description}{" "}
                          {r.next ? <span style={mono}>Next: {fmtRange(r.next.start, r.next.end)}.</span>
                            : r.table ? <span style={{ color: T.overFg }}>Next date not announced yet. Add it below when it is.</span> : null}
                        </div>
                        {on && txt && <div style={{ fontSize: 14, color: T.soft, marginTop: 3, lineHeight: 1.45 }}>{txt}</div>}
                      </div>
                    </label>
                    {r.table && (
                      <div style={{ marginLeft: 27, marginTop: 6 }}>
                        <button style={btnMini} onClick={() => { setOpenDates(openDates === r.key ? null : r.key); setNewDate({ start: "", end: "" }); }}>
                          {openDates === r.key ? "Hide dates" : `Dates (${(r.dates || []).length})`}
                        </button>
                        {openDates === r.key && (
                          <div style={{ marginTop: 8 }}>
                            <div style={{ ...mono, fontSize: 14, color: T.soft, lineHeight: 1.7, columns: 2 }}>
                              {(r.dates || []).map((d, i) => <div key={i}>{fmtRange(d.start, d.end)}</div>)}
                            </div>
                            {extra.length > 0 && (
                              <div style={{ marginTop: 6 }}>
                                {extra.map(([a, b], i) => (
                                  <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 14, color: T.ink }}>
                                    <span style={mono}>{fmtRange(a, b)}</span><span style={{ color: T.dim }}>added by you</span>
                                    <button style={{ ...btnMini, padding: "1px 6px" }} onClick={() => removeTableDate(r.key, i)}>Remove</button>
                                  </div>
                                ))}
                              </div>
                            )}
                            <div style={{ display: "flex", gap: 6, alignItems: "center", marginTop: 8, flexWrap: "wrap" }}>
                              <input type="date" value={newDate.start} onChange={e => setNewDate(n => ({ ...n, start: e.target.value }))} style={inp} />
                              <span style={{ color: T.dim, fontSize: 14 }}>to</span>
                              <input type="date" value={newDate.end} onChange={e => setNewDate(n => ({ ...n, end: e.target.value }))} style={inp} />
                              <button style={btnMini} onClick={() => addTableDate(r.key)}>Add date</button>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>

      {/* ── 3. custom periods ──────────────────────────────────────────── */}
      <div style={{ ...cap, marginBottom: 8 }}>3 · Your own periods</div>
      <div style={{ ...panel, marginBottom: 18 }}>
        <div style={{ padding: "10px 14px", fontSize: 14.5, color: T.dim, borderBottom: `2px solid ${T.line}`, lineHeight: 1.5 }}>
          An anniversary sale, a local festival, a college move-in week. Add past dates too: that's what the lift is learned from.
        </div>
        {(draft.custom || []).length === 0 && (
          <div style={{ padding: "12px 14px", fontSize: 14.5, color: T.faint }}>None yet.</div>
        )}
        {(draft.custom || []).map((c, i) => (
          <div key={c.id || i} className="hol-row" style={{ padding: "10px 14px", borderTop: i ? `1px solid ${T.line}` : "none" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              <input type="checkbox" checked={c.enabled !== false} onChange={() => updCustom(i, { enabled: c.enabled === false })}
                style={{ width: 17, height: 17, accentColor: T.amber, cursor: "pointer" }} />
              <span style={{ fontSize: 15.5, fontWeight: 600 }}>{c.name}</span>
              {c.id && learnedChip(c.id, c.enabled !== false)}
              <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 14, color: T.soft, marginLeft: "auto", cursor: "pointer" }}>
                <input type="checkbox" checked={!!c.yearly} onChange={() => updCustom(i, { yearly: !c.yearly })} style={{ accentColor: T.amber }} />
                Same dates every year
              </label>
              <button style={btnMini} onClick={() => delCustom(i)}>Delete</button>
            </div>
            <div style={{ ...mono, fontSize: 14, color: T.soft, marginTop: 4, marginLeft: 27, display: "flex", gap: 6, flexWrap: "wrap" }}>
              {c.ranges.map(([a, b], j) => (
                <span key={j} style={{ padding: "1px 6px", border: `1px solid ${T.line2}`, borderRadius: 2, display: "inline-flex", gap: 6, alignItems: "center" }}>
                  {fmtRange(a, b)}
                  {c.ranges.length > 1 && (
                    <button style={{ background: "none", border: "none", color: T.dim, cursor: "pointer", padding: 0 }}
                      title="Remove this date" onClick={() => updCustom(i, { ranges: c.ranges.filter((_, k) => k !== j) })}>×</button>
                  )}
                </span>
              ))}
            </div>
            {c.id && learnedText(c.id) && <div style={{ fontSize: 14, color: T.soft, marginTop: 3, marginLeft: 27 }}>{learnedText(c.id)}</div>}
          </div>
        ))}
        <div style={{ padding: "12px 14px", borderTop: `2px solid ${T.line}`, background: T.sunken, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <input placeholder="Name, e.g. Anniversary sale" value={custom.name} onChange={e => setCustom(c => ({ ...c, name: e.target.value }))}
            style={{ ...inp, fontFamily: SANS, width: 240 }} />
          <input type="date" value={custom.start} onChange={e => setCustom(c => ({ ...c, start: e.target.value }))} style={inp} />
          <span style={{ color: T.dim, fontSize: 14 }}>to</span>
          <input type="date" value={custom.end} onChange={e => setCustom(c => ({ ...c, end: e.target.value }))} style={inp} />
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 14, color: T.soft, cursor: "pointer" }}>
            <input type="checkbox" checked={custom.yearly} onChange={() => setCustom(c => ({ ...c, yearly: !c.yearly }))} style={{ accentColor: T.amber }} />
            Every year
          </label>
          <button style={btnMini} onClick={addCustom}>Add</button>
          <span style={{ fontSize: 13.5, color: T.faint }}>Same name again adds another date to it.</span>
        </div>
      </div>

      {/* ── how it works ───────────────────────────────────────────────── */}
      <div style={{ ...panel, padding: "12px 16px", fontSize: 14.5, color: T.soft, lineHeight: 1.6 }}>
        <div style={{ fontWeight: 650, color: T.ink, marginBottom: 4 }}>How holidays are counted</div>
        <ul style={{ margin: 0, paddingLeft: 18, listStyle: "disc" }}>
          <li>Each product's lift is measured from its past years, against the ordinary days around them.</li>
          <li>A product with no past years of its own borrows from similar products.</li>
          <li>Clear, repeated spikes count fully. A noisy one on a few sales counts for less.</li>
          <li>Discounts are taken out first, so enter the deal as an event and the holiday is still counted once.</li>
          <li>Holiday days count as ordinary days when the everyday level and season are learned, so a spike never inflates ordinary weeks.</li>
          <li>When two ticked periods overlap, each day counts once: a holiday's main day first, then the shorter period.</li>
        </ul>
      </div>
    </div>
  );
}
